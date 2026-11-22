# rsqlx 与上游 sqlx 类型支持对照

本文档对照 **sqlx 0.9.0**（上游 Rust 库）与 **rsqlx 0.9.3**（Python 异步驱动）的类型支持。
rsqlx 是 sqlx 在 Python 侧的异步应用，不是 sqlx 源码的 fork；类型差异主要来自
Python 动态类型与 Rust 静态类型的映射边界，而非 sqlx 能力缺失。

图例：

- ✅ 完全支持
- ⚠️ 部分支持（精度、时区或语义有损失）
- ❌ 不支持（运行期明确抛 `InterfaceError`，提示用 `cast(col as text)`）
- ⛔ 未暴露（Rust 生态概念或产品范围外，不构成 rsqlx 缺陷）
- ➕ rsqlx 额外提供的便利能力

---

## 1. PostgreSQL

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| BOOL | `bool` | → `bool` | ✅ |
| INT2 / INT4 / INT8 | i16 / i32 / i64 | → `int` | ✅ |
| `"CHAR"`（单字节） | i8 | → `str`（落入字符串分支） | ⚠️ |
| OID | `Oid` | → `int` | ✅ |
| FLOAT4 / FLOAT8 | f32 / f64 | → `float` | ✅ |
| NUMERIC | BigDecimal + rust_decimal | rust_decimal 优先，超范围回退 BigDecimal → Python `Decimal`（任意精度） | ✅ |
| TEXT / VARCHAR / CHAR / NAME / BPCHAR / UNKNOWN | String | → `str` | ✅ |
| CITEXT | `Citext` | → `str`（需安装 citext 扩展） | ✅ |
| BYTEA | `Vec<u8>` | → `bytes` | ✅ |
| VOID | `()` | → `None` | ✅ |
| UUID | `Uuid` | → `uuid.UUID`；UUID 对象可绑定 | ✅ |
| JSON / JSONB | `Json<T>` / `Value` / `RawValue` | → `dict` / `list` / 标量；绑定 dict 为单个 jsonb | ✅ |
| JSONB[] | `Vec<Json<T>>` | 读取 → `list`；写入用 `rsqlx.JsonArray([...])` | ✅（见第 4 节） |
| DATE / TIME / TIMESTAMP / TIMESTAMPTZ | chrono / time 类型 | → `date` / `time` / naive / aware `datetime` | ✅ |
| TIMETZ | `PgTimeTz` | → `str`（`HH:MM:SS+HH:MM`，保留时区） | ✅ |
| INTERVAL | `PgInterval` | → `dict{months, days, microseconds}`（无损） | ✅ |
| MONEY | `PgMoney` | → `Decimal`（按 2 位小数） | ⚠️ |
| INET / CIDR | ipnet | → `str`（IpNet Display，如 `192.168.1.1/32`） | ✅ |
| MACADDR | mac_address | → `str` | ✅ |
| BIT / VARBIT | bit_vec | → `str`（位串 `'1010'`） | ✅ |
| 几何（POINT / LINE / LSEG / BOX / PATH / POLYGON / CIRCLE 等） | PgCube 等 | 明确报错 | ❌ |
| HSTORE / LTREE / LQUERY | PgHstore / PgLTree / PgLQuery | 明确报错 | ❌ |
| range（INT4/8RANGE、TSRANGE、TSTZRANGE、DATERANGE、NUMRANGE） | PgRange<T> | 解码 → `rsqlx.Range`；绑定用 `rsqlx.Range(start, end, include_start, include_end)` | ✅ |
| domain / composite / enum / 匿名 record | `#[derive(Type)]` | 自定义 type_name 落入 default，明确报错 | ❌ |
| 元素数组（BOOL[]/INTn[]/FLOATn[]/TEXT[]/BYTEA[]/DATE[]/TIME[]/TIMESTAMP[]/TIMESTAMPTZ[]/NUMERIC[]/UUID[]/JSON[]/JSONB[]） | 原生数组 | 全部支持解码；标量列表可绑定为原生数组 | ✅ |
| 其它元素数组（OID[]/CITEXT[]/INET[]/MACADDR[]/BIT[]/INTERVAL[]/MONEY[]/range[]/几何[]/ltree[]/domain[]） | 原生数组 | 明确报错 | ❌ |
| 无类型 NULL 绑定 | 需显式类型 | `PgUntypedNull`（OID 705）让服务端推断，行为类似 psycopg2 | ➕ |

## 2. MySQL

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| BOOL / TINYINT(1) | bool | → `bool` | ✅ |
| 有符号 / 无符号整数（TINY/SMALL/MEDIUM/INT/BIG，含 UNSIGNED） | i8..i64 / u8..u64 | → `int` | ✅ |
| YEAR | Year | → `int` | ✅ |
| FLOAT / DOUBLE | f32 / f64 | → `float` | ✅ |
| DECIMAL | BigDecimal + rust_decimal | 仅 rust_decimal → `Decimal` | ⚠️ |
| CHAR / VARCHAR / TEXT 系列 / ENUM / SET | String | → `str` | ✅ |
| BIT | u8 / u64 | → `int`（本版本修复过解码 bug） | ✅ |
| BINARY / VARBINARY / BLOB 系列 | `Vec<u8>` | → `bytes` | ✅ |
| JSON | `Json<T>` / `Value` | → `dict` / `list` / 标量 | ✅ |
| DATE / DATETIME | NaiveDate / NaiveDateTime | → `date` / naive `datetime`；零日期经 `Option` 得 `None` | ✅ |
| TIME（含微秒、负数、±838:59:59） | `MySqlTime` 全范围 | 仅 `NaiveTime`（time-of-day）；超范围/负值报错 | ⚠️ |
| TIMESTAMP | `DateTime<Utc/Local>` | 解为 `DateTime<Utc>` 后转 naive → **时区丢失** | ⚠️ |
| UUID | `Uuid` ↔ BINARY(16)；Hyphenated ↔ CHAR(36) | 绑定 ✅（二进制）；解码 BINARY 只返回 `bytes`，**不还原 `uuid.UUID`** | ⚠️ |
| INET4 / INET6（MariaDB） | Ipv4Addr / Ipv6Addr | 明确报错 | ❌ |
| GEOMETRY | 上游无 Rust 解码器 | 明确报 `unsupported MySQL type 'GEOMETRY'` | ❌ |
| 原生数组 | MySQL 无此类型 | Python 列表绑定为单个 JSON 数组值 | ➕ |
| Arc / Box / Rc / Cow / Text 适配器 | 测试覆盖 | 无 Python 对应概念 | ⛔ |

## 3. SQLite

SQLite 侧按 **runtime storage class** 分发，declared type 仅用于日期时间嗅探。

| 类型族 | sqlx 0.9.0 | rsqlx | 状态 |
|---|---|---|---|
| NULL | `Option<T>` | → `None` | ✅ |
| BOOLEAN | bool | runtime `INTEGER` → `int`（非 `bool`） | ✅ |
| 整数族 | i8..i64 / u8..u32，u64 仅解码 | runtime `INTEGER` → `i64`；u64 超 i64 上限不可解 | ⚠️ |
| REAL / FLOAT | f32 / f64 | → `float` | ✅ |
| TEXT | String / &str | → `str` | ✅ |
| BLOB | `Vec<u8>` | → `bytes` | ✅ |
| DATETIME / TIMESTAMP | TEXT / INTEGER(unixepoch) / REAL(julianday) 三种格式 | 仅 declared ∈ {DATETIME, TIMESTAMP} 且 runtime=TEXT 时尝试解码；INTEGER/REAL 存的时间戳返回 `int`/`float` | ⚠️ |
| DATE / TIME | TEXT | declared=DATE/TIME 且 runtime=TEXT 时尝试；解析失败回退 `str` | ⚠️ |
| 时区感知 datetime | RFC3339 | 绑定时 `naive_local()` → **时区丢失**；解码无 aware 分支 | ⚠️ |
| JSON | `Json<T>` ↔ TEXT | 解码侧无 JSON 分支：TEXT 原样返回 `str`，不解析为 `dict`/`list` | ⚠️ |
| UUID | `Uuid` ↔ BLOB/TEXT | 绑定 → 字符串 ✅；解码 BLOB/TEXT → `bytes`/`str`，**不还原 `uuid.UUID`** | ⚠️ |
| NUMERIC / Decimal | 上游刻意不支持 | 绑定 Decimal → TEXT；解码回 `str` | ➕/⚠️ |
| 数组 | 无 | Python 列表绑定为 JSON 文本 | ➕ |
| BString（bstr feature） | `BString` ↔ BLOB | 以 `bytes` 返回 | ⛔/✅ |
| Arc / Box / Rc / Cow / Text 适配器 | 测试覆盖 | 无 Python 对应概念 | ⛔ |

## 4. `rsqlx.JsonArray` 使用说明

Python 没有静态类型来区分"单个 JSON 数组值"和"由 JSON 值组成的数组"，因此 rsqlx
保持向后兼容的默认行为，同时提供显式包装器以对齐 sqlx 的 `Vec<Json<T>>` 能力。

| Python 值 | 绑定为 | 目标列 |
|---|---|---|
| `{"k": 1}` | 单个 jsonb 对象 | `JSONB` |
| `[{"k": 1}, {"k": 2}]` | **单个 jsonb 数组值**（默认，行为不变） | `JSONB` |
| `[1, 2, 3]` | 原生 `INT4[]`（纯标量列表） | `INT4[]` / `NUMERIC[]` |
| `rsqlx.JsonArray([{"k": 1}, {"k": 2}, None])` | 原生 `JSONB[]` | `JSONB[]` |

```python
import rsqlx

# PostgreSQL JSONB[] 列
await pool.execute("CREATE TEMP TABLE docs (items JSONB[])")
await pool.execute(
    "INSERT INTO docs VALUES ($1)",
    [rsqlx.JsonArray([{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None])],
)
rows = await pool.fetch("SELECT items FROM docs")
# rows == [{"items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None]}]
```

- `JsonArray` 接受任意可 JSON 序列化元素（dict / list / 标量 / `None`），嵌套结构原样保留
- MySQL / SQLite 无原生 JSON 数组类型，`JsonArray` 退化为普通 JSON 数组值
- 读取 `JSONB[]` / `JSON[]` 列无需包装器，rsqlx 自动解码为 `list`（保留 `None`）
- 上游依据：`sqlx-postgres/src/types/json.rs` 中 `impl<T> PgHasArrayType for Json<T>`

### 4.1 `rsqlx.Range` — PostgreSQL 区间类型

```python
import rsqlx

r = rsqlx.Range(1, 10, include_start=True, include_end=False)   # [1, 10)
await pool.execute("INSERT INTO t (r) VALUES ($1)", [r])

row = await pool.fetch_one("SELECT r FROM t")
assert row["r"].start == 1 and row["r"].end == 10
assert row["r"].include_start is True and row["r"].include_end is False
```

- `start=None` / `end=None` 表示无界（unbounded），如 `rsqlx.Range(None, 100, include_end=True)`
- 支持的元素类型：`int`（INT4RANGE/INT8RANGE）、`Decimal`（NUMRANGE）、`date`（DATERANGE）、
  `datetime`（TSRANGE）、aware `datetime`（TSTZRANGE）——绑定时按元素值推断区间类型
- 读取任意上述 range 列会自动解码为 `rsqlx.Range`（含边界标记与无界信息）
- **离散区间规范化**：PostgreSQL 会把整数/日期区间的闭端点规范化（如 `[10,20]` → `[10,21)`），
  这是 PG 标准行为，`rsqlx` 如实返回规范化后的值
- MySQL / SQLite 无原生区间类型；传入 `rsqlx.Range` 会绑定为 NULL（不应在这些库使用）
- 上游依据：`sqlx-postgres/src/types/range.rs` 的 `PgRange<T>` + `std::ops::Bound<T>`

## 5. 通用约束

- 不支持的列类型统一抛 `rsqlx.InterfaceError`，消息形如
  `unsupported PostgreSQL type 'INET' for column 'x'; cast(col as text) to retrieve as string`
- 绑定侧仅接受白名单类型；不支持的 Python 类型抛 `TypeError`
- 超大 Python int（> 64 位）自动降级为 `Decimal`
- `NUMERIC` / `DECIMAL` 使用 `rust_decimal`（约 96 位精度），超出范围的更高精度值不支持

---

## 6. 差异根因与补齐可行性

差异并非来自"sqlx 做不到"，而是四类原因叠加。按可补齐性排序：

### 6.1 根因 A：sqlx 可选 feature 未启用（**本版本已补齐**）

sqlx 把若干类型放在**默认关闭的 feature** 后面。本版本已在 `Cargo.toml` 启用
`bigdecimal`、`ipnet`、`mac_address`、`bit-vec`、`time`，并在 `backend.rs` 加了解码分支：

| sqlx feature | 对应类型 | rsqlx 现状 |
|---|---|---|
| `bigdecimal` | 任意精度 NUMERIC | rust_decimal 优先，超范围回退 BigDecimal → `Decimal` |
| `ipnet` | PG INET / CIDR | → `str`（IpNet Display） |
| `mac_address` | PG MACADDR | → `str` |
| `bit-vec` | PG BIT / VARBIT | → `str`（位串） |
| `time` | PG TIMETZ | → `str`（带时区） |

**代价**：新增 5 个 Rust 依赖（`bigdecimal`、`ipnet`、`mac_address`、`bit-vec`、`time`），
wheel 体积与编译时间增加；全部为纯 Rust crate，不引入系统依赖，不破坏零系统依赖承诺。
OID / CITEXT / INTERVAL / MONEY 使用 sqlx 内置类型，无需额外 feature。

### 6.2 根因 B：Python 类型系统无直接对应（部分支持，可语义化补齐）

| 类型 | sqlx 表示 | Python 困境 |
|---|---|---|
| PG `INTERVAL` | `PgInterval { months: i32, days: i32, microseconds: i64 }` | `datetime.timedelta` **无"月"字段**（月长不固定，无法无损转换） |
| PG range（6 种） | `PgRange<T>` + `Bound<T>` | **已解决**：新增 `rsqlx.Range(start, end, include_start, include_end)` 显式类型表达开闭区间与无界 |
| PG composite / domain / enum / record | `#[derive(Type)]` 的 Rust 结构体 | Python 侧没有列类型元数据，无法自动构造对应对象 |
| MySQL `TIME` 全范围 | `MySqlTime`（负数、±838:59:59、微秒） | `datetime.time` 只表示 time-of-day（0–23:59:59） |
| 单字节 `"CHAR"` | `i8` | 返回 `str` 是 Python 直觉选择 |

**能否补齐**：可以做"有损但合理"的映射，但无法无损：
- `INTERVAL` → 自定义 `rsqlx.Interval(months, days, microseconds)` 或返回 `timedelta`（丢失月）
- range → `tuple` 或自定义 `rsqlx.Range` 类
- MySQL TIME → `timedelta` 而非 `time`（可表达负数和超 24 小时）

**建议**：这是 API 设计决策，不是实现障碍。若用户场景需要，可新增 `rsqlx.Interval` /
`rsqlx.Range` 等类型，与 `JsonArray` 同一模式（显式包装、默认行为不变）。

### 6.3 根因 C：运行时信息不足导致不可逆（无法精确补齐）

| 类型 | 问题 |
|---|---|
| MySQL / SQLite UUID 解码 | 存储为 `BINARY(16)` / `TEXT` / `BLOB`，rsqlx 按 storage class 解码为 `bytes`/`str`，**无法判断它原本是不是 UUID** |
| MySQL `TIMESTAMP` 时区 | MySQL TIMESTAMP 本身不存时区，sqlx 用 UTC 解码后 rsqlx 转 naive 与 pymysql 行为一致；时区信息在服务端已不存在 |
| SQLite DATETIME 以 INTEGER/REAL 存储 | unixepoch / julianday 是数字，无法与普通整数/浮点区分；rsqlx 只在 declared type 为 DATETIME 且 runtime=TEXT 时尝试解码 |
| SQLite JSON 解码 | JSON 存为 TEXT，无法区分普通字符串与 JSON 文本；强行解析会误伤 |

**能否补齐**：**不能无损补齐**——信息在存储层已丢失。可行方向：
- 提供可选的 `row_factory`/连接参数让用户声明"这列是 UUID/JSON"
- 这是适配层（SQLAlchemy Dialect 的 `TypeDecorator`）的职责，核心不应猜测

这与项目硬约束一致：**核心只做无损、明确的类型映射；语义还原交给适配层**。

### 6.4 根因 D：上游本身也不支持或属 Rust 生态概念（不适用）

| 类型 | 说明 |
|---|---|
| MySQL `GEOMETRY` | sqlx 0.9 本身无 Rust 解码器；rsqlx 明确报错已是最佳行为 |
| Arc / Box / Rc / Cow / Text 适配器 | Rust 所有权/智能指针概念，Python 无对应物 |
| SQLite 数组 | SQLite 没有数组类型 |

这类差异不构成 rsqlx 缺陷，无需补齐。

### 6.5 补齐优先级建议

| 优先级 | 项目 | 根因 | 补齐方式 | 风险 |
|---|---|---|---|---|
| ✅ 已完成 | PG `bigdecimal` 高精度 NUMERIC | A | 启用 feature + rust_decimal 优先/回退 BigDecimal | 低（API 不变） |
| ✅ 已完成 | PG INET / CIDR | A | 启用 `ipnet` + 映射 `str` | 中（新增依赖） |
| ✅ 已完成 | PG MACADDR / BIT / TIMETZ / CITEXT / OID / MONEY / INTERVAL | A | 启用 feature + 简单映射 | 低-中 |
| P1 | MySQL TIME 全范围 | B | 返回 `timedelta` | 中（改变现有 TIME 返回类型，需评估兼容性） |
| ✅ 已完成 | PG range | B | 新增 `rsqlx.Range` 类型，支持绑定与 6 种 range 解码 | 中（新 API） |
| P2 | PG 几何 / HSTORE / LTREE / domain / composite | A/B | 需启用更多 feature + 设计 Python 表示 | 高（收益低，需求少见） |
| P3 | SQLite JSON 自动解析 | C | 不补齐，用 `TypeDecorator` | — |
| — | MySQL/SQLite UUID 还原 | C | 不补齐，用 `TypeDecorator` | — |
| — | MySQL GEOMETRY / Rust 适配器 | D | 不补齐 | — |

**结论**：rsqlx 与上游的类型差异中，约一半可通过启用 sqlx feature 和新增解码分支补齐
（根因 A、B），另一半是信息不可逆或产品范围外（根因 C、D），不应在核心层强行补齐。
`JsonArray` 已经是"新增显式类型对齐上游能力"的成功范式，后续可按同一模式推进。
