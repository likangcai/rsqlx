# rsqlx 实现与发布指南

本文档记录 rsqlx 的**实现过程**（架构设计、关键技术决策、踩过的坑）以及**从打包到发布 PyPI 的完整操作流程**，供后续维护和新贡献者参考。

- [第一部分：实现原理](#第一部分实现原理)
- [第二部分：打包](#第二部分打包)
- [第三部分：发布到 PyPI](#第三部分发布到-pypi)
- [第四部分：用户安装](#第四部分用户安装)

---

# 第一部分：实现原理

## 1.1 目标与整体思路

想法来源：`orjson` 把 Rust 的高性能 JSON 库封装成 Python 原生扩展，用户 `pip install orjson` 就能用上 Rust 的速度。`rsqlx` 用同样的思路处理 SQL —— 用 Rust 的 [sqlx](https://github.com/launchbadge/sqlx) 做数据库核心，通过 [PyO3](https://github.com/PyO3/pyo3) 暴露成 Python 原生扩展模块（`.pyd` / `.so`）。

最终产物是一个**单个原生扩展模块** `rsqlx`，没有额外的 Python 源码层 —— 所有类、方法、异常都在 Rust 侧定义，和 orjson 一致。

```
Python:  import rsqlx; pool = await rsqlx.connect(...)
            ↓  (PyO3 生成的原生模块)
Rust:    sqlx::PgPool / MySqlPool / SqlitePool  +  Tokio runtime
            ↓  (数据库协议)
       PostgreSQL / MySQL / SQLite
```

## 1.2 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 数据库核心 | **sqlx 0.8.6** | 纯 Rust、异步、三库统一抽象、自带连接池和迁移 |
| Python 绑定 | **pyo3 0.24.2** | `experimental-async` 特性支持 `#[pymethods]` 里直接写 `async fn` |
| 异步运行时 | **tokio 1**（全局多线程） | sqlx 的 `runtime-tokio` 需要 tokio 上下文来跑定时器和 IO |
| TLS | **rustls + ring** | 纯 Rust，避免 Linux 上依赖系统 OpenSSL 开发包 |
| 构建工具 | **maturin** | 专门处理 PyO3 项目的打包和 wheel 生成 |

### 为什么不用 pyo3-asyncio？

最初的方案是用 `pyo3-asyncio` 做 Rust Future ↔ Python asyncio 的桥接。调研后发现：

- `pyo3-asyncio` 最新版本停在 **0.20**，只兼容 pyo3 0.20
- pyo3 0.20 **不支持 Python 3.13**（当前环境是 Python 3.13.5）
- 项目已基本停止维护，pyo3 官方从 0.22 起推荐内置的 coroutine 方案

所以改用 **pyo3 0.24 的 `experimental-async` 特性**，代价是这是一个"实验性"特性，但它的 async fn 支持已经相当完整（`&self` 接收器、取消处理、GIL 管理都有官方支持）。

## 1.3 核心架构

### 模块划分

```
src/
├── lib.rs          # 模块注册、异常定义、connect() 出口
├── pool.rs         # Pool 类：CRUD、事务入口、迁移、原生查询
├── transaction.rs  # Transaction 类：事务内操作 + commit/rollback
├── backend.rs      # 三库统一层：参数绑定、行解码、批量 INSERT 改写
├── params.rs       # Python 对象 → Rust PyParam 枚举
├── error.rs        # sqlx::Error → Python 异常映射
└── runtime.rs      # 全局 Tokio runtime + GIL 释放包装器
```

### 数据流：一次查询的完整旅程

以 `await pool.fetch("SELECT * FROM t WHERE id = $1", [1])` 为例：

```
1. Python 调用 fetch() ── pyo3 把 async fn 包装成 Python 协程对象
2. Python await 该协程 ── pyo3 coroutine 机制接管
3. 进入 async fn 主体（此时持有 GIL）
   ├─ params.rs: 把 [1] 转成 Vec<PyParam>（纯 Rust 值，不再依赖 Python 对象）
   ├─ runtime.rs: 把 sqlx 查询 future spawn 到全局 Tokio runtime
4. await 时通过 ReleaseGil 包装器 ── 释放 GIL
   ├─ Tokio 工作线程执行：构建 Query、绑定参数、发协议包、收响应
5. 查询完成，协程被唤醒，重新获取 GIL
6. backend.rs: 逐列解码（按 type_info().name() 分发）→ 构造 Python dict
7. 返回 list[dict] 给 Python
```

**关键点**：参数在步骤 3 就转成了纯 Rust 的 `PyParam` 枚举，所以步骤 4 在 Tokio 线程上执行时完全不碰 Python 对象，不需要 GIL。这是能安全释放 GIL 的前提。

### GIL 释放

pyo3 的 async fn **默认在 await 期间持有 GIL**（这是 `experimental-async` 的设计），如果不处理，慢查询会阻塞整个 Python 解释器。解决办法是 `runtime.rs` 里的 `ReleaseGil` 包装器：

```rust
impl<F> Future for ReleaseGil<F>
where F: Future + Unpin + Send, F::Output: Send {
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.get_mut();
        let waker = cx.waker();
        // 在 allow_threads 内部 poll，poll 期间释放 GIL
        Python::with_gil(|py| {
            py.allow_threads(|| Pin::new(&mut this.0).poll(&mut Context::from_waker(waker)))
        })
    }
}
```

实测效果：4 个耗时约 4.6 秒的慢查询并发执行时，事件循环里另一个每 20ms tick 一次的任务，最大间隔只有 101ms —— 事件循环没有被阻塞。

### 三库统一：`DbExt` trait

pyo3 的 `#[pyclass]` 不支持泛型，所以不能写 `Pool<DB>`。解决办法是定义一个 trait 把三个数据库的能力抽象出来，再用枚举分发：

```rust
pub trait DbExt: Database + Sized
where
    Self::Row: Send,
    Self::QueryResult: Send,
    for<'q> <Self as Database>::Arguments<'q>: sqlx::IntoArguments<'q, Self>,
{
    fn bind_param<'q>(q: Query<'q, Self, Self::Arguments<'q>>, p: &PyParam)
        -> Query<'q, Self, Self::Arguments<'q>>;
    fn push_bind(sep: &mut Separated<'_, '_, Self, &'static str>, p: &PyParam);
    fn row_to_dict(py: Python<'_>, row: &Self::Row) -> PyResult<Py<PyDict>>;
    fn result_summary(res: &Self::QueryResult) -> (u64, Option<i64>);
}
```

然后 `Pool` 内部持有一个枚举：

```rust
pub enum Backend {
    Pg(PgPool),
    MySql(MySqlPool),
    Sqlite(SqlitePool),
}
```

每个方法做一次 match 分发到泛型函数：

```rust
match &self.inner {
    Backend::Pg(p) => backend::pool_fetch_rows::<Postgres>(p.clone(), sql, params).await,
    Backend::MySql(p) => backend::pool_fetch_rows::<MySql>(p.clone(), sql, params).await,
    Backend::Sqlite(p) => backend::pool_fetch_rows::<Sqlite>(p.clone(), sql, params).await,
}
```

三个泛型参数约束里，`for<'c> &'c mut DB::Connection: Executor<'c, Database = DB>` 是必须的 —— 因为 sqlx 的 `Executor` impl 是**按驱动分别实现**的（在 `sqlx-postgres` / `sqlx-mysql` / `sqlx-sqlite` 各自的 crate 里），泛型代码里必须显式声明这个 bound，具体 DB 实例化时才能证明。

## 1.4 类型映射的实现

### 参数绑定（Python → 数据库）

Python 是动态类型，sqlx 是静态类型（`query.bind(value)` 要求 `T: Encode + Type<DB>`）。桥接方案是在 `params.rs` 里把所有 Python 参数先转成 Rust 的 `PyParam` 枚举：

```rust
pub enum PyParam {
    Null, Bool(bool), Int(i64), Float(f64), Str(String), Bytes(Vec<u8>),
    Date(NaiveDate), Time(NaiveTime), DateTime(NaiveDateTime),
    DateTimeTz(DateTime<FixedOffset>), Decimal(Decimal), Uuid(Uuid),
    Json(Value),        // dict / 嵌套结构
    Array(Vec<PyParam>), // 纯标量列表 → PG 原生数组
}
```

转换在 GIL 保护下一次性完成，之后就是纯 Rust 值。每个驱动的 `bind_param` 再把 `PyParam` 映射到该库支持的具体类型。

### 行解码（数据库 → Python）

sqlx 的 `Row` 是静态类型的，动态取值需要知道列的类型。做法是通过 `TypeInfo` 拿类型名字符串再分发：

```rust
for (i, col) in row.columns().iter().enumerate() {
    let type_name = col.type_info().name();   // 如 "INT4", "TIMESTAMPTZ", "INT4[]"
    let value = pg_value_to_py(py, row, i, type_name, col.name())?;
    dict.set_item(col.name(), value)?;
}
```

各驱动 `name()` 返回的字符串（从 sqlx 源码确认）：

- **PostgreSQL**: `"BOOL"` `"INT2"` `"INT4"` `"INT8"` `"FLOAT4"` `"FLOAT8"` `"NUMERIC"` `"TEXT"` `"VARCHAR"` `"CHAR"` `"NAME"` `"BYTEA"` `"JSON"` `"JSONB"` `"UUID"` `"DATE"` `"TIME"` `"TIMESTAMP"` `"TIMESTAMPTZ"`，数组加 `[]` 后缀
- **MySQL**: `"TINYINT"` `"TINYINT UNSIGNED"` `"BIGINT UNSIGNED"` `"DECIMAL"` `"JSON"` `"DATETIME"` `"TIMESTAMP"` `"YEAR"` 等（UNSIGNED 靠连接 flags 判断）
- **SQLite**: 按**运行时值类型**解码（`"NULL"` `"INTEGER"` `"REAL"` `"TEXT"` `"BLOB"`），因为 SQLite 是动态类型的；声明类型（decltype）只用于嗅探 DATETIME/DATE/TIME 列

## 1.5 踩过的坑与解法

这部分是开发过程中实际遇到并解决的问题，按类别整理。

### 坑 1：sqlx 0.8 的 `Query` 多了第三个泛型参数

**现象**：`sqlx::Query<'q, DB>` 编译报 "takes 2 generic arguments but 1 was supplied"。

**原因**：sqlx 0.7 → 0.8 把 `Query<'q, DB>` 改成了 `Query<'q, DB, Arguments>`。

**解法**：所有涉及 `Query` 的签名都要写完整：

```rust
Query<'q, DB, <DB as Database>::Arguments<'q>>
```

### 坑 2：泛型代码里的 `Executor` / `IntoArguments` bound

**现象**：泛型函数里 `q.fetch_all(&pool)` 报 trait bound 不满足。

**原因**：sqlx 的 `Executor` impl 是按驱动分开实现的，`Database: Database` 这个 bound 本身不蕴含 `&mut Connection: Executor`。`Database::Arguments` 也不自带 `IntoArguments`。

**解法**：在泛型函数上显式加 HRTB（`for<'c>` / `for<'q>`）：

```rust
where
    DB: DbExt,
    for<'q> <DB as Database>::Arguments<'q>: sqlx::IntoArguments<'q, DB>,
    for<'c> &'c mut <DB as Database>::Connection: sqlx::Executor<'c, Database = DB>,
```

注意 `Executor` 后面必须带 `Database = DB`，否则关联类型不匹配。

### 坑 3：PG 的 NULL 参数无法插入非 text 列

**现象**：`INSERT INTO t (age) VALUES ($1)` 传 `None` 时报 `column "age" is of type integer but expression is of type text (42804)`。

**原因**：sqlx 绑定 `Option::<String>::None` 时，给参数声明的 OID 是 text(25)。PG 不会把 text 类型的 NULL 隐式转换成 integer。而 psycopg2 / asyncpg 能工作是因为它们发的是 OID 0 或 705（unknown），让服务端根据目标列推断。

**解法**：自定义一个 `PgUntypedNull` 类型，声明 OID 705（PG 内置的 unknown 伪类型）：

```rust
struct PgUntypedNull;

impl sqlx::Type<Postgres> for PgUntypedNull {
    fn type_info() -> PgTypeInfo {
        PgTypeInfo::with_oid(Oid(705))   // unknown
    }
}

impl<'q> sqlx::Encode<'q, Postgres> for PgUntypedNull {
    fn produces(&self) -> Option<PgTypeInfo> { Some(Self::type_info()) }
    fn encode_by_ref(&self, _buf: &mut PgArgumentBuffer)
        -> Result<IsNull, BoxDynError> { Ok(IsNull::Yes) }
}
```

这样 `None` 可以绑定到任意类型的列，行为和 psycopg2 一致。

### 坑 4：Python list 被绑成 JSON，无法插入 PG 数组列

**现象**：`INSERT INTO t (tags) VALUES ($1)` 传 `["a", "b"]`（目标列 `TEXT[]`）报类型不匹配 —— 因为 list 被绑成了 jsonb。

**解法**：在 `params.rs` 里区分两种情况：

- 元素全是标量（None/bool/int/float/str/Decimal/UUID/datetime）→ `PyParam::Array`，PG 上绑定为 `Vec<Option<T>>`
- 含 dict 或嵌套 list → `PyParam::Json`，绑定为 `sqlx::types::Json<Value>`

数组元素类型由第一个非 None 元素推断：

```rust
Some(PyParam::Str(_)) => bind_array_t(q, items, |p| if let PyParam::Str(v) = p { Some(v.clone()) } else { None }),
```

混合类型（无法推断）时自动降级为 JSON。空数组绑定为 `Vec<Option<bool>>`（PG 需要知道数组元素类型，空数组只能靠约定）。

### 坑 5：`execute_many` 极慢（89 秒 vs pymysql 0.37 秒）

**现象**：批量插入 1000 行，rsqlx 要 89 秒，pymysql 只要 0.37 秒 —— 慢了 240 倍。

**原因**：原实现是循环 1000 次 prepared statement（prepare → bind → execute → close）。pymysql 的 `executemany` 会把多组参数拼成一条 `INSERT INTO t VALUES (...),(...),(...)`。

**解法**：在 `pool_execute_many` 里检测 INSERT 语句，自动改写成多值 INSERT：

```rust
fn parse_insert_for_batch(sql: &str) -> Option<(String, usize, String)> {
    // "INSERT INTO t (a, b) VALUES (?, ?)"  →  ("INSERT INTO t (a, b) VALUES", 2, "")
}
```

然后拼接 N 组占位符，把 `?` 按目标库改写（PG 需要 `$1, $2, ...`），一次性执行。

**效果**：89 秒 → **178ms**，提升 500 倍，比 pymysql 还快 1.6 倍。

注意：改写只针对 `INSERT ... VALUES (...)` 形式，其他语句（UPDATE/DELETE/非 INSERT）回退到循环执行，保证语义正确。

### 坑 6：存储过程等语句无法执行

**现象**：`DROP PROCEDURE` / `CREATE PROCEDURE` 报 `This command is not supported in the prepared statement protocol yet (HY000)`。

**原因**：MySQL 的预编译协议（COM_STMT_PREPARE）不支持这些 DDL。pymysql 用的是文本协议（COM_QUERY），所以没这问题。

**解法**：增加 `execute_raw` / `fetch_raw`，内部用 `sqlx::raw_sql()`（它的 `take_arguments()` 返回 `None`，会触发 sqlx 的 simple query 路径，走 COM_QUERY）：

```rust
pub async fn pool_execute_raw<DB>(pool: Pool<DB>, sql: String) -> PyResult<()> {
    crate::runtime::run_db_task(async move {
        sqlx::raw_sql(&sql).execute(&pool).await?;
        Ok::<(), sqlx::Error>(())
    }).await
}
```

代价是 `raw_sql` 不支持参数绑定，所以文档里明确警告不要往里面拼用户输入。

### 坑 7：MySQL YEAR / TIMESTAMP 类型解码失败

**现象**：
- `YEAR` 列用 `i16` 解码报 incompatible
- `TIMESTAMP` 列用 `NaiveDateTime` 解码报 incompatible

**原因**（查 sqlx-mysql 源码确认）：
- `NaiveDateTime` 的 `Type` impl 声明的是 `ColumnType::Datetime`，**不匹配** Timestamp
- `DateTime<Utc>` 的 `Type` impl 声明的是 `ColumnType::Timestamp`
- `i16` 只映射到 SmallInt，不覆盖 Year

**解法**：
```rust
"YEAR" => { let v: Option<u16> = opt!(row, i, u16); ... }   // u16 解码后转 i64
"DATETIME"  => Ok(pyv!(py, opt!(row, i, NaiveDateTime))),
"TIMESTAMP" => match opt!(row, i, DateTime<Utc>) {
    Some(dt) => Ok(pyv!(py, dt.naive_utc())),   // 转 naive，与 pymysql 行为一致
    None => Ok(py_none(py)),
},
```

### 坑 8：abi3 与 datetime 支持冲突

**现象**：加上 `pyo3/abi3-py39` feature 后，`PyDate` / `PyDateTime` / `PyTime` 导入失败，报 `#[cfg(not(Py_LIMITED_API))]` 相关错误。

**原因**：CPython 的 datetime C API **不在稳定 ABI（Limited API）里**，pyo3 在 abi3 模式下不提供这些类型。

**解法**：**放弃 abi3**，每个 Python 版本单独构建 wheel。代价是 CI 矩阵变大（3 平台 × 5 版本），但保住了完整的 datetime 类型映射能力 —— 对一个数据库驱动来说这是核心功能，不值得为省几个构建任务牺牲。

### 坑 9：rustls 与 MySQL 8.0 的 DHE cipher 不兼容

**现象**：连 MySQL 8.0.16 报 `HandshakeFailure`。

**原因**：MySQL 8.0.16 默认 cipher suite 是 `DHE-RSA-AES256-GCM-SHA384`，而 rustls **不支持 DHE**（Diffie-Hellman Ephemeral）密钥交换，只支持 ECDHE。

**解法**：连接 URL 里加 `?ssl-mode=disabled`。MySQL 的 `caching_sha2_password` 在非 TLS 下会用 RSA 公钥加密密码，安全性仍有保障。已在文档里说明。

（长期方案是在 MySQL 服务端配置 ECDHE cipher，或升级 MySQL 版本。）

### 坑 10：SQLite 在 Windows 上打不开新数据库文件

**现象**：`unable to open database file (code: 14)`。

**原因**：sqlx 的 SQLite 默认 `mode=rw`（不创建文件），而 Python 的 `sqlite3` 模块默认会创建。

**解法**：在 `connect()` 里给 SQLite URL 自动追加 `mode=rwc`（用户没显式指定 mode 时）：

```rust
if !url.contains("mode=") {
    url.push(if url.contains('?') { '&' } else { '?' });
    url.push_str("mode=rwc");
}
```

保持和 Python 标准库一致的行为，符合用户预期。

## 1.6 验证体系

写完代码后的验证分四层，共 **215 项**：

| 套件 | 项数 | 内容 |
|------|------|------|
| `tests/test_sqlite.py` | 18 | pytest 标准测试，不需要外部服务 |
| `tests/verify_pg.py` | 53 | **交叉验证**：每个操作同时用 rsqlx 和 psycopg2 跑一遍，对比结果是否一致 |
| `tests/verify_mysql.py` | 50 | 同上，对比 pymysql |
| `tests/test_api_friendly.py` | 94 | API 表面检查（命名/异常/类型）+ 三库完整功能流程 + 边界情况 |

交叉验证的思路值得说明：不是"断言 rsqlx 返回什么"，而是"rsqlx 的返回值必须和 psycopg2/pymysql 一致"。这样能发现类型映射的细微偏差（比如 JSON 是否自动解析、datetime 是否带时区）。

## 1.7 性能特征：rsqlx 比 psycopg2 慢吗？

**答案：单行点查慢，大结果集快，并发慢查询快得多。** 用 `tests/bench_pg_overhead.py` 可以复现下面的数据。

### 测量方法

把单次查询耗时拆成两个分量：

```
t(query) = fixed_overhead + per_row_cost × rows
```

测量 1 / 10 / 100 / 1000 行的查询耗时，用最小二乘拟合出 `fixed_overhead`（每次查询固定付的成本）和 `per_row_cost`（每行解码成本）。

### 实测数据（PostgreSQL 18，release 构建）

| 分量 | rsqlx | psycopg2 | 对比 |
|------|-------|----------|------|
| 固定开销（每次查询） | 279us | 114us | rsqlx 高 **2.4x** |
| 每行解码开销 | 3.9us | 7.8us | rsqlx 低 **0.5x**（快 2x） |

**盈亏平衡点：约 42 行**。单次查询返回行数小于 42 行时 psycopg2 快，大于 42 行时 rsqlx 快（1000 行时 rsqlx 快约 2 倍）。

### 固定开销花在哪了？

这是关键问题。分层测量的结果：

| 项目 | 耗时 |
|------|------|
| asyncio 空协程 `await` 一次 | **0.4us** |
| `await loop.run_in_executor(noop)`（跨线程派发 + 等结果 + 事件循环唤醒） | **149us** |
| rsqlx `SELECT 1` | 314us |
| psycopg2 `SELECT 1` | 67us |
| **差额** | **247us** |

结论：**额外的 247us 里，约 60%（149us）来自「跨线程任务派发 + 事件循环唤醒」这个模式本身**，而不是 asyncio 协程机制（后者只要 0.4us，可忽略）。

rsqlx 每次查询的完整链路：

```
asyncio 事件循环线程
  → 把 sqlx future spawn 到 tokio 线程池      ← 线程切换 + 任务队列
  → tokio 工作线程: 协议收发、解码
  → 完成后通过 call_soon_threadsafe 唤醒事件循环  ← 跨线程通知（加锁 + 队列）
  → 事件循环下次迭代 resume 协程，拿结果         ← 又一次调度延迟
```

psycopg2 是**同步 C 调用**，直接在调用线程里跑完 libpq 全流程返回，完全没有上面这条链路，所以它只付 67us 的"真实数据库往返"成本。

剩下的 ~98us 来自：
- GIL 释放/重获（`PyEval_SaveThread` / `PyEval_RestoreThread`）
- pyo3 coroutine 的 `send()` 协议开销（每次 await 至少两次 Python → C 调用）
- 参数转换（Python 对象 → `PyParam`）

### rsqlx 更快的地方

**1. 每行解码快 2 倍**（3.9us vs 7.8us）

Rust 原生解码 + 直接构造 Python dict，比 psycopg2 的 C 层 + Python 对象构造路径更短。所以大结果集 rsqlx 反超。

**2. 并发等待可以重叠，慢查询快 5 倍**

```
10 个 SELECT pg_sleep(0.1):
  rsqlx    10 个并发（等待重叠）:  215ms
  psycopg2 10 个顺序（串行等待）: 1077ms
  加速比: 5.0x
```

这是 rsqlx 真正的核心优势。查询等待期间释放 GIL 且异步挂起，多个查询的等待时间完全重叠；psycopg2 同步调用只能一个一个等。

**3. 批量取 vs 逐行取差 69 倍**

```
取 1000 行:
  一次 fetch 1000 行  :    4.2ms
  单行查询 × 1000 次  :  288.1ms
```

### 本质：一个 trade-off

rsqlx 用**「每次查询多付约 165us 固定成本」**换来了：

- 并发能力（多个查询等待重叠）
- 解码效率（每行省 3.9us）
- GIL 释放（不阻塞其他 Python 线程）

代价就是单行点查这种"固定开销主导"的场景会慢 2–4 倍。

### 选型建议

| 场景 | 推荐 |
|------|------|
| OLTP 单行点查、极致低延迟 | psycopg2 / asyncpg 更快 |
| 大结果集、报表、分析查询 | rsqlx 快（>42 行即反超） |
| 慢查询 + 高并发（多个查询可并行） | rsqlx 快 5x |
| 需要 async + 三库统一接口 | rsqlx（异步是硬需求，psycopg2 是同步的） |

### 使用 rsqlx 的性能建议

1. **避免 N+1 逐行查询** —— 用 `IN (...)`、`JOIN` 或一次 `fetch` 取回（快 69 倍）
2. **批量写用 `execute_many`** —— 自动改写成多值 INSERT，比逐条快两个数量级
3. **能并发就并发** —— 用 `asyncio.gather` 让查询等待重叠
4. **大结果集一次取** —— 让每行解码的优势覆盖固定开销

### 未来可优化方向

固定开销的大头是"跨线程派发 + 事件循环唤醒"。理论上可以：

- 对小查询 / 已就绪的结果，跳过 spawn 直接在事件循环线程 poll（省 149us，但会失去并行能力且可能阻塞事件循环）
- 批量合并唤醒通知（多个查询完成时一次性唤醒）

这两个都是 trade-off，需要按实际负载权衡。当前设计选择了"保证不阻塞事件循环"这一侧。

---

# 第二部分：打包

## 2.1 构建系统：maturin

`pyproject.toml` 声明 maturin 为构建后端：

```toml
[build-system]
requires = ["maturin>=1.7,<2"]
build-backend = "maturin"

[project]
name = "rsqlx"
version = "0.1.1"
requires-python = ">=3.9"

[tool.maturin]
module-name = "rsqlx"
features = ["pyo3/extension-module"]
```

`Cargo.toml` 里关键配置：

```toml
[lib]
name = "rsqlx"
crate-type = ["cdylib"]   # 动态库，不是 Rust 静态库

[profile.release]
lto = true                # 链接期优化，减小体积提升性能
codegen-units = 1         # 更好的优化，编译更慢
strip = true              # 去掉符号表
```

## 2.2 本地构建

```bash
# 安装 maturin
pip install maturin

# 构建 wheel（debug，编译快，用于开发迭代）
maturin build -o dist

# 构建 release wheel（LTO 优化，编译慢，用于发布）
maturin build --release -o dist

# 产物
ls dist/
# rsqlx-0.1.1-cp313-cp313-win_amd64.whl

# 安装到当前环境
pip install --force-reinstall --no-deps dist/rsqlx-*.whl

# 开发模式（直接装进当前 venv，改动后需重新执行）
maturin develop
maturin develop --release
```

wheel 命名规则 `rsqlx-0.1.1-cp313-cp313-win_amd64.whl`：
- `cp313` — CPython 3.13（非 abi3，所以版本绑定）
- `win_amd64` — 平台架构

## 2.3 多平台构建

原生扩展必须**每个平台单独构建**，不能像纯 Python 包那样一个 wheel 通吃。

### 本地交叉构建（Linux 构建 Linux aarch64）

```bash
# 用 zig 作为 C 工具链（sqlite 需要 C 编译器）
pip install ziglang
rustup target add aarch64-unknown-linux-gnu
maturin build --release --target aarch64-unknown-linux-gnu --manylinux 2_28 --zig
```

### CI 矩阵构建（推荐）

`.github/workflows/build.yml` 里定义了完整矩阵：

| 平台 | Runner | 目标三元组 | Python 版本 |
|------|--------|-----------|------------|
| Linux x86_64 | ubuntu-22.04 | `x86_64-unknown-linux-gnu` | 3.9–3.13 |
| Linux aarch64 | ubuntu-22.04 + zig | `aarch64-unknown-linux-gnu` | 3.13 |
| Windows x86_64 | windows-latest | `x86_64-pc-windows-msvc` | 3.9–3.13 |
| macOS x86_64 | macos-13 | `x86_64-apple-darwin` | 3.13 |
| macOS arm64 | macos-14 | `aarch64-apple-darwin` | 3.9–3.13 |

Linux 用 `--manylinux 2_28` 保证 glibc 兼容性（glibc ≥ 2.28 的系统都能装）。

### 关于 SQLite 的 C 依赖

`libsqlite3-sys` 会编译 C 源码（或链接系统 sqlite）。三大平台都需要 C 编译器：
- Windows: MSVC
- macOS: clang（Xcode Command Line Tools）
- Linux: gcc

这些都是标准工具链的一部分，CI runner 自带，本地开发一般也有。

## 2.4 检查 wheel 内容

```bash
# 查看 wheel 里有什么
python -m zipfile -l dist/rsqlx-*.whl

# 应该看到：
#   rsqlx/__init__.py        (maturin 生成的空壳)
#   rsqlx/rsqlx.cp313-win_amd64.pyd   (真正的原生模块)
#   rsqlx-0.1.1.dist-info/METADATA
#   rsqlx-0.1.1.dist-info/WHEEL

# 查看元数据
python -m zipfile -e dist/rsqlx-*.whl /tmp/wheel_check
cat /tmp/wheel_check/rsqlx-0.1.1.dist-info/METADATA
```

METADATA 里应该包含 `Requires-Python: >=3.9`、classifiers、作者信息 —— 这些都来自 `pyproject.toml`。

## 2.5 sdist（源码分发）

除了 wheel，还要发布 sdist，让用户在没有预编译 wheel 的平台（如 Alpine Linux musl、FreeBSD）上能从源码构建：

```bash
maturin sdist -o dist
# 产物: rsqlx-0.1.1.tar.gz（含 Rust 源码 + Cargo.toml + pyproject.toml）
```

用户装 sdist 时会本地编译（需要 Rust 工具链）。

---

# 第三部分：发布到 PyPI

## 3.1 前置准备

### 1. 注册 PyPI 账号

- 正式环境：https://pypi.org/account/register/
- 测试环境（建议先在这里练手）：https://test.pypi.org/account/register/

### 2. 检查包名是否可用

```bash
# 访问 https://pypi.org/project/rsqlx/
# 如果 404 说明名字可用；如果被占用需要换名（如 rsqlx-db）
```

包名被占用时，改 `pyproject.toml` 的 `name` 和 `Cargo.toml` 的 `[package].name`（两者应保持一致）。

### 3. 完善包元数据

发布前确认 `pyproject.toml` 里的信息准确（这些内容会显示在 PyPI 项目页）：

```toml
[project]
name = "rsqlx"
version = "0.1.1"
description = "..."              # PyPI 列表页显示的一句话
readme = "README.md"              # PyPI 项目页渲染的长文档
requires-python = ">=3.9"
license = { text = "MIT OR Apache-2.0" }
authors = [{ name = "Yingzi", email = "yingzilkq@163.com" }]
keywords = [...]
classifiers = [...]               # 平台/Python 版本/协议标记
```

## 3.2 方式一：GitHub Actions 自动发布（Trusted Publishing，推荐）

这是当前 `build.yml` 里配置好的方式，不需要保存任何密钥。

### 步骤 1：在 PyPI 上配置 Trusted Publisher

登录 PyPI → 进入你的项目（或点 "Publishing" 标签）→ 添加 publisher：

| 字段 | 值 |
|------|-----|
| PyPI Project Name | `rsqlx` |
| Owner | 你的 GitHub 用户名/组织 |
| Repository name | 仓库名 |
| Workflow name | `build.yml` |
| Environment name | `pypi` |

（如果是第一次发布、项目还不存在，需要先在 PyPI 上 "Create a pending project" 或用方式二先传一版。）

TestPyPI 同理：https://test.pypi.org/manage/project/rsqlx/settings/publishing/

### 步骤 2：确认 workflow 配置

`.github/workflows/build.yml` 的 publish job：

```yaml
publish:
  name: Publish to PyPI
  needs: [build-wheels, build-sdist]
  if: startsWith(github.ref, 'refs/tags/v')   # 只在 push tag 时触发
  runs-on: ubuntu-22.04
  environment: pypi                            # 对应 PyPI 上配置的 environment
  permissions:
    id-token: write                            # Trusted Publishing 必需
  steps:
    - uses: actions/download-artifact@v4
      with:
        path: dist
        merge-multiple: true
    - uses: pypa/gh-action-pypi-publish@release/v1
```

### 步骤 3：更新版本号

修改两个文件的 `version`（**必须同时改**）：

```toml
# Cargo.toml
[package]
version = "0.1.1"

# pyproject.toml
[project]
version = "0.1.1"
```

同时在 `CHANGELOG.md` 里记录本次变更。

### 步骤 4：打 tag 并推送

```bash
git add -A
git commit -m "Release v0.1.1"
git tag v0.1.1
git push origin main
git push origin v0.1.1      # ← 这个会触发发布
```

### 步骤 5：观察构建

```bash
# 或去 https://github.com/<owner>/<repo>/actions 看
```

所有平台的 wheel + sdist 构建完成后，publish job 会自动上传到 PyPI。

## 3.3 方式二：本地手动发布

适合首次发布（项目还不存在时）或 CI 出问题的应急。

### 用 API token

1. 在 PyPI 生成 token：Account settings → API tokens → Add API token（scope 选 "Entire account" 或指定项目）
2. 创建 `~/.pypirc`：

```ini
[pypi]
  username = __token__
  password = pypi-AgEIcHlwaS5vcmc...   # 你的 token

[testpypi]
  username = __token__
  password = pypi-AgENdGVzdC5weXBp...
```

3. 构建并上传：

```bash
# 先传 TestPyPI 验证
maturin build --release -o dist
maturin upload --repository testpypi dist/*

# 确认没问题后传正式 PyPI
maturin upload dist/*
```

`maturin upload` 内部用 twine，也可以用 twine 直接传：

```bash
pip install twine
python -m twine upload --repository testpypi dist/*
python -m twine upload dist/*
```

### 本地构建多平台 wheel

手动发布时需要本地（或用 CI）构建所有平台的 wheel 再一起上传：

```bash
# 本机平台
maturin build --release -o dist

# 交叉编译（Linux aarch64）
maturin build --release --target aarch64-unknown-linux-gnu --manylinux 2_28 --zig -o dist

# 最终 dist/ 下应该有各平台的 wheel + sdist
maturin upload dist/*
```

**注意**：macOS 和 Windows 的 wheel 无法在 Linux 上交叉编译，所以实际发布还是推荐用 CI（GitHub Actions 提供三大平台的 runner）。

## 3.4 版本管理规则

rsqlx 遵循 [语义化版本 2.0.0（SemVer）](https://semver.org/lang/zh-CN/)，版本号格式为 `MAJOR.MINOR.PATCH`：

| 版本位 | 名称 | 变更时机 | 兼容性承诺 |
|--------|------|----------|------------|
| X (MAJOR) | 主版本号 | 不兼容的 API 修改 | 不兼容旧版（重大变革） |
| Y (MINOR) | 次版本号 | 向下兼容的功能性新增 | 向下兼容（新功能） |
| Z (PATCH) | 修订号 | 向下兼容的问题修正 | 向下兼容（问题修复） |

具体迭代规则：

| 变更类型 | 版本规则 | 示例 |
|---------|---------|------|
| 向下兼容的 bug 修复、文档或许可补完 | patch +1 | 0.1.0 → 0.1.1 |
| 向下兼容的新 API、新数据库能力 | minor +1 | 0.1.1 → 0.2.0 |
| 破坏性 API 变更、不兼容修改 | major +1 | 0.1.1 → 1.0.0 |

> 当前处于 `0.y.z` 初始开发阶段：MAJOR 为 0 表示 API 尚未稳定，任何 MINOR 变更都可能包含破坏性改动。这与上游 sqlx 同为 `0.x` 策略一致。

`Cargo.toml`、`pyproject.toml`、`uv.lock` 三处版本号必须保持同步。

## 3.5 发布检查清单

发布前逐项确认：

- [ ] `Cargo.toml` 和 `pyproject.toml` 版本号已更新且一致
- [ ] `CHANGELOG.md` 已记录本次变更
- [ ] 本地 `cargo check` 通过
- [ ] `tests/test_sqlite.py` 全过
- [ ] `tests/verify_pg.py` 全过（需 PG 实例）
- [ ] `tests/verify_mysql.py` 全过（需 MySQL 实例）
- [ ] `tests/test_api_friendly.py` 全过
- [ ] `maturin build --release` 成功
- [ ] 用 `python -m zipfile -l` 检查 wheel 内容正确
- [ ] 在干净的 venv 里 `pip install dist/*.whl` 并跑一遍快速验证
- [ ] PyPI 包名未被占用（首次发布）
- [ ] Trusted Publisher 已在 PyPI 配置（方式一）
- [ ] git tag 已推送

## 3.6 发布后验证

```bash
# 等几分钟让 PyPI CDN 同步，然后
pip install rsqlx

python -c "import rsqlx; print(rsqlx.__version__)"

# 或者在完全隔离的环境里测
python -m venv /tmp/rsqlx_test
/tmp/rsqlx_test/Scripts/pip install rsqlx    # Windows
# source /tmp/rsqlx_test/bin/activate        # Linux/macOS
```

---

# 第四部分：用户安装

## 4.1 标准安装

```bash
pip install rsqlx
```

pip 会根据当前平台自动选择对应的 wheel：

| 用户环境 | 下载的 wheel |
|---------|-------------|
| Windows x86_64 + Python 3.13 | `rsqlx-0.1.1-cp313-cp313-win_amd64.whl` |
| Linux x86_64 + Python 3.12 | `rsqlx-0.1.1-cp312-cp312-manylinux_2_28_x86_64.whl` |
| macOS arm64 + Python 3.11 | `rsqlx-0.1.1-cp311-cp311-macosx_11_0_arm64.whl` |

如果**没有**匹配的 wheel（比如 Alpine Linux、FreeBSD、或 Python 3.14 而 wheel 只到 3.13），pip 会回退到 sdist，本地编译 —— 这需要用户装 Rust 工具链。

## 4.2 验证安装

```bash
python -c "
import asyncio, rsqlx

async def main():
    pool = await rsqlx.connect('sqlite::memory:')
    await pool.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)')
    await pool.execute('INSERT INTO t (name) VALUES (?)', ['hello'])
    print(await pool.fetch('SELECT * FROM t'))
    await pool.close()

asyncio.run(main())
"
```

预期输出：`[{'id': 1, 'name': 'hello'}]`

## 4.3 升级 / 卸载

```bash
pip install --upgrade rsqlx
pip uninstall rsqlx
```

## 4.4 安装常见问题

**Q: `pip install rsqlx` 报 "No matching distribution found"**

可能原因：
- Python 版本不在 3.9–3.13 范围 → 升级/降级 Python，或从源码构建
- 平台架构没有预编译 wheel → 从源码构建：`pip install rsqlx --no-binary :all:`（需要 Rust）
- PyPI 上还没发布该版本 → 检查 https://pypi.org/project/rsqlx/

**Q: 从源码构建很慢**

首次编译要下载并编译 sqlx、tokio、rustls 等依赖，release 模式 + LTO 可能要 5–15 分钟。用 debug 模式会快很多：

```bash
git clone https://gitee.com/yingzi_shadow/rsqlx.git
cd rsqlx
pip install maturin
maturin develop            # debug 模式
```

**Q: 连接 MySQL 报 HandshakeFailure**

MySQL 8.0 默认用 DHE cipher，rustls 不支持。加 `?ssl-mode=disabled`：

```python
await rsqlx.connect("mysql://root:pass@127.0.0.1:3306/db?ssl-mode=disabled")
```

**Q: SQLite 路径在 Windows 上要注意什么**

用正斜杠或 `pathlib`：

```python
from pathlib import Path
await rsqlx.connect(f"sqlite:{Path('data.db').as_posix()}")
```

## 4.5 从源码安装（开发者）

```bash
git clone https://gitee.com/yingzi_shadow/rsqlx.git
cd rsqlx

# 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# 安装构建工具和依赖
pip install maturin pytest psycopg2-binary pymysql

# 开发模式安装
maturin develop

# 跑测试
python -m pytest tests/test_sqlite.py -v
```

---

# 第五部分：许可证与贡献

rsqlx 采用 **MIT OR Apache-2.0 双许可**（和上游 sqlx 一致，sqlx 本身也是双许可）。

## 5.1 文件布局

```
LICENSE-MIT    # MIT License 全文
LICENSE-APACHE # Apache License 2.0 全文
```

仓库根目录只放这两份文件，**刻意与上游 sqlx 保持一致**（sqlx 仓库也是同名两份、没有单独的 `LICENSE` 索引文件）。原因：

- `LICENSE-MIT` / `LICENSE-APACHE` 是 Rust/cargo 生态约定俗成的文件名，cargo / PyPI / GitHub 都能自动识别
- 双许可下用户直接拿到两份全文，无需在单文件里翻找
- 与上游 sqlx 同构，方便长期对照与同步

## 5.2 源码 SPDX 头

每个 `src/*.rs` 文件顶部都带两行声明，便于许可证随源码分发（Apache-2.0 要求保留声明）：

```rust
// Copyright (C) rsqlx Contributors
// SPDX-License-Identifier: MIT OR Apache-2.0
```

新增 `.rs` 文件时务必带上这两行。其他语言（如有 `.py` 脚本）可加：

```python
# Copyright (C) rsqlx Contributors
# SPDX-License-Identifier: MIT OR Apache-2.0
```

## 5.3 pyproject.toml 声明

```toml
license = { text = "MIT OR Apache-2.0" }
license-files = ["LICENSE-MIT", "LICENSE-APACHE"]
```

`license-files` 确保 `maturin build` 把两份 LICENSE 文件都打进 wheel 的
`*.dist-info/` 目录，用户 `pip install` 后能在
`site-packages/rsqlx-0.1.1.dist-info/` 下看到。

## 5.4 贡献者许可

`LICENSE-MIT` / `LICENSE-APACHE` 文件末尾声明：除非贡献者另有说明，所有提交（Contribution）默认以
相同的双许可条款授权。这是 Apache-2.0 第 5 条的惯例表述，不要求贡献者签 CLA。

## 5.5 选哪份

| 想怎么用 | 选 |
|---------|-----|
| 最简单、无附加义务 | MIT |
| 需要明确的专利授权、企业合规 | Apache-2.0 |

两选一即可，不需要两份都遵守。

---

# 附录：常用命令速查

```bash
# ===== 开发 =====
cargo check                          # 快速编译检查（不链接）
cargo build                          # 编译（debug）
maturin develop                      # 构建并安装到当前 venv
maturin develop --release            # release 模式

# ===== 测试 =====
python -m pytest tests/test_sqlite.py -v          # SQLite（无需外部服务）
python tests/verify_pg.py                          # PG 交叉验证（需 PG）
python tests/verify_mysql.py                       # MySQL 交叉验证（需 MySQL）
python tests/test_api_friendly.py                  # API + 三库功能（94 项）
python tests/bench_pymysql_vs_rsqlx.py             # 性能基准

# ===== 打包 =====
maturin build --release -o dist      # 构建 release wheel
maturin sdist -o dist                # 构建 sdist
maturin build --release --target aarch64-unknown-linux-gnu --manylinux 2_28 --zig

# ===== 发布 =====
maturin upload --repository testpypi dist/*   # 传 TestPyPI
maturin upload dist/*                         # 传正式 PyPI
git tag v0.1.1 && git push origin v0.1.1      # 触发 CI 自动发布

# ===== 依赖维护 =====
cargo search sqlx                    # 查 sqlx 最新版
cargo update                         # 更新依赖（受 Cargo.toml 版本约束）
cargo tree | head -50                # 查看依赖树
```

---

# 附录：升级 sqlx 版本

详细流程见 [README.md](README.md) 的 "Tracking sqlx Upstream Updates" 章节。核心要点：

1. 改 `Cargo.toml` 里的 sqlx 版本号（**不需要手动下载源码**，cargo 会自动从 crates.io 拉取）
2. 确认 Rust 版本 ≥ 新 sqlx 的 MSRV（sqlx 0.9 要求 Rust 1.94）
3. `cargo check` —— 重点看 feature 名称、Trait 签名、泛型参数变化
4. 跑全部测试套件
5. 检查 `backend.rs` 里的类型名映射表和 sqlx 新版本一致

需要查看 sqlx 源码时（调试类型映射等），源码在 cargo 缓存里：

```
~/.cargo/registry/src/index.crates.io-*/sqlx-<version>/
```
