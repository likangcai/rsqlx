# sqlalchemy-rsqlx 方言设计方案

版本：v1（对应 rsqlx 0.9.x → 下一大版本）
日期：2026-11-21

## 0. 目标、边界与不可破坏项

| 项 | 结论 |
|---|---|
| rsqlx 核心定位 | 无框架依赖的纯异步驱动；只保留连接池、查询、事务、类型映射、异常体系 |
| 核心禁止事项 | 不引入 SQLAlchemy / Django / ORM 抽象；不统一占位符；不做 SQL 字符串重写；不改变 `list[dict]` 默认输出 |
| 方言包 | `sqlalchemy-rsqlx`，独立打包发布，可放同仓库子目录 `sqlalchemy_rsqlx/` |
| 核心对 SQLAlchemy 的依赖 | **零**；核心包不声明、不安装 SQLAlchemy |
| Django | 本次不实现；仅保留适配边界，文档中说明后续路径 |
| 异步桥接 | 使用 SQLAlchemy 既有事实协议：`sqlalchemy.util.concurrency.await_only`（greenlet），不发明新标准，不调用 `asyncio.run()` |

## 1. 方言包结构

```
sqlalchemy_rsqlx/
├── pyproject.toml                  # 独立发布：deps = rsqlx>=0.9.3, SQLAlchemy>=2.0
├── README.md                       # 安装 / DSN / 支持矩阵 / 限制 / 与 asyncpg 差异
├── DESIGN.md                       # 指向 docs/SQLALCHEMY_DIALECT_DESIGN.md
├── sqlalchemy_rsqlx/
│   ├── __init__.py                 # __version__
│   ├── dbapi.py                    # DBAPI 兼容层（异常类 + connection/cursor/adapter）
│   ├── base.py                     # RsqlxAsyncDialectMixin：通用 on_connect / 参数映射 / 异常映射
│   ├── postgresql.py               # PGDialect_rsqlx + module-level `dialect = ...`
│   ├── mysql.py                    # MySQLDialect_rsqlx
│   ├── sqlite.py                   # SQLiteDialect_rsqlx
│   └── types.py                    # JSON / UUID / ARRAY 的 bind/result processor 修正
├── tests/
│   ├── conftest.py                 # 数据库可用性探测、env-gated fixtures
│   ├── test_sqlite.py              # 最小闭环：CRUD / 事务 / 批量 / 类型往返
│   ├── test_postgresql.py          # $n 占位符 / 数组 / UUID / JSON
│   ├── test_mysql.py               # ? 占位符 / last_insert_id / executemany
│   ├── test_placeholders.py        # text(":name") → 原生占位符专项
│   ├── test_output_format.py       # Row 属性访问 + 序列访问
│   ├── test_exceptions.py          # 异常映射 / code 保留
│   └── test_perf_smoke.py          # 事件循环不被阻塞的冒烟基准
└── examples/fastapi_minimal.py     # FastAPI + SQLAlchemy 2.0 async + rsqlx
```

`pyproject.toml` 注册 entry point（SQLAlchemy 用 `PluginLoader("sqlalchemy.dialects")` 自动发现）：

```toml
[project.entry-points."sqlalchemy.dialects"]
"postgresql.rsqlx" = "sqlalchemy_rsqlx.postgresql:PGDialect_rsqlx"
"mysql.rsqlx"      = "sqlalchemy_rsqlx.mysql:MySQLDialect_rsqlx"
"sqlite.rsqlx"     = "sqlalchemy_rsqlx.sqlite:SQLiteDialect_rsqlx"
```

用户提供 `postgresql+rsqlx://...` 时，SQLAlchemy 解析出 `drivername="postgresql+rsqlx"`，
经 `PluginLoader` 查找 `postgresql.rsqlx` 入口并加载 `PGDialect_rsqlx`，无需手工 `registry.register()`。

## 2. 适配器类图

```
                    SQLAlchemy AsyncEngine
                              │  greenlet_spawn / await_only
                              ▼
        ┌─────────────────────────────────────────┐
        │ AsyncAdapt_rsqlx_dbapi                  │  ← 模块级入口，暴露 DBAPI 异常类
        │   paramstyle: numeric_dollar | qmark | qmark │
        │   connect(url, **opts) -> Connection    │
        └──────────────┬──────────────────────────┘
                       │ await_only(rsqlx.connect(...))
                       ▼
        ┌─────────────────────────────────────────┐
        │ AsyncAdapt_rsqlx_connection             │  每个实例 = 1 个 rsqlx.Pool(1 conn)
        │   await_ = staticmethod(await_only)     │
        │   _pool: rsqlx.Pool                     │
        │   _tx:   rsqlx.Transaction | None       │
        │   cursor() / commit() / rollback() / close() │
        └──────────────┬──────────────────────────┘
                       │
                       ▼
        ┌─────────────────────────────────────────┐
        │ AsyncAdapt_rsqlx_cursor                 │
        │   description / rowcount / lastrowid    │
        │   execute() / executemany()             │  ← await_(pool|tx.execute*)
        │   fetchone() / fetchmany() / fetchall() │  ← 本地 deque，无额外 IO
        └──────────────┬──────────────────────────┘
                       │ rsqlx 原生 API
                       ▼
        rsqlx.Pool / rsqlx.Transaction  (Rust + Tokio)
```

设计取向：**一个 DBAPI connection 对应一个 `rsqlx.Pool(max_connections=1)`**。
理由：DBAPI 的 `close()` 必须释放该连接的物理资源；若多 DBAPI connection 共享一个 rsqlx Pool，
`close()` 会误关全局池。嵌套池的代价是每次语句多一次池内借还，但换来正确的生命周期语义。
SQLAlchemy 侧用 `AsyncAdaptedQueuePool`（文件库/PG/MySQL）或 `StaticPool`（SQLite `:memory:`）
管理 DBAPI connection 数量，这与 rsqlx 内部单连接池各司其职。

## 3. 事件循环桥接策略（最大风险项）

**规则**

1. 所有 coroutine 必须通过 `sqlalchemy.util.concurrency.await_only` 驱动，
   它把控制权切回 SQLAlchemy async engine 所在 greenlet（事件循环正在其中运行）。
2. 禁止 `asyncio.run()`、`loop.run_until_complete()`、`asyncio.get_event_loop().run_until_complete()`。
3. 禁止在 `await_only` 内部再创建事件循环；`rsqlx` 的 Tokio runtime 是独立全局 runtime，
   与 Python 事件循环无共享，不会互相阻塞。
4. `dbapi.connect()` 是同步函数，内部调用 `await_only(rsqlx.connect(...))`（与 aiosqlite/asyncpg 同构）。
5. 仅在显式 `create_async_engine` + `async with engine.connect()` / `run_sync()` 的 greenlet 上下文中可用；
   直接同步使用方言适配器属未定义行为，文档中明确说明。
6. 保留 `AsyncAdaptFallback_rsqlx_connection`（`await_fallback`）供 `async_fallback=True` 的同步场景，
   但不在推荐路径中。

**死锁防线**

- `await_only` 在 SQLAlchemy 的 greenlet 外调用会抛 `InvalidRequestError`（greenlet 未提供），
  测试需覆盖「错误上下文调用时报清晰错误」。
- `Pool.begin()` 是 coroutine，必须用 `await_` 驱动，不能用 `async with await pool.begin()` 直接写在适配器里。

## 4. 连接池参数映射表

| SQLAlchemy 参数 | rsqlx 参数 | 处理方式 |
|---|---|---|
| `pool_size` | — | 由 SQLAlchemy `AsyncAdaptedQueuePool` 控制 DBAPI connection 数量；不传给 rsqlx |
| `max_overflow` | — | 同上 |
| `pool_timeout` | `acquire_timeout` | 传入 `rsqlx.connect(acquire_timeout=...)`，单位秒 |
| `pool_recycle` | `max_lifetime` | 传入 `rsqlx.connect(max_lifetime=...)`，单位秒 |
| `pool_pre_ping` | — | SQLAlchemy 层执行 `SELECT 1`；rsqlx 不参与 |
| `pool_use_lifo` / `pool_reset_on_return` | — | SQLAlchemy 层语义，忽略并文档说明 |
| `connect_args` | 原样透传 | 允许的键：`min_connections` / `max_connections` / `acquire_timeout` / `idle_timeout` / `max_lifetime`；其它键抛 `ArgumentError` |
| `max_connections`（connect_args） | `max_connections` | 默认强制为 `1`；用户显式设置 >1 时告警（嵌套池），不禁止 |
| `echo` / `echo_pool` | — | SQLAlchemy 层，正常生效 |
| `isolation_level` | — | 见第 5 节；`AUTOCOMMIT` 特殊处理 |

不支持的 URL query 参数：未知键一律 `ArgumentError`，避免静默误用。

## 5. 事务语义

| SQLAlchemy 动作 | 适配器行为 |
|---|---|
| `Connection.begin()` / 首次 `execute()`（autobegin） | 惰性 `await_(pool.begin())` 得到 `rsqlx.Transaction`，存 `_tx` |
| `cursor.execute()` | 若 `_tx` 存在 → `await_(_tx.execute(sql, params))`；否则 `await_(pool.execute(...))`（AUTOCOMMIT） |
| `Connection.commit()` | `await_(_tx.commit())`，清空 `_tx`；无事务时 no-op |
| `Connection.rollback()` | `await_(_tx.rollback())`，清空 `_tx`；无事务时 no-op |
| `DBAPI connection.close()` | 若有未结束事务先 rollback；再 `await_(pool.close())` |
| `async with engine.begin()` 异常退出 | SQLAlchemy 调用 rollback → 映射到 `tx.rollback()` |
| rsqlx 事务二次 commit/rollback | rsqlx 抛 `InterfaceError` → 映射为 DBAPI `ProgrammingError` |

每次 commit/rollback 后 `_tx = None`，下次执行重新 `begin()`，符合 rsqlx Transaction 一次性语义。
`execute_many` 走同一个 `_tx`/`pool`，保证批量与单条语句处于同一事务上下文。

## 6. 占位符与 paramstyle 映射表（硬约束）

| 方言 | SQLAlchemy `paramstyle` | SQLAlchemy 生成 | rsqlx 接收 | 适配器是否重写 SQL |
|---|---|---|---|---|
| `postgresql.rsqlx` | `numeric_dollar` | `$1, $2, ...` | `$1, $2, ...` | **否** |
| `mysql.rsqlx` | `qmark` | `?` | `?` | **否** |
| `sqlite.rsqlx` | `qmark` | `?` | `?` | **否** |

- SQLAlchemy 的 `"numeric"` paramstyle 生成 `:1`/`:2`，只有 `"numeric_dollar"` 生成
  `$1`/`$2`。用户所说“PostgreSQL 对应 numeric”指编号占位符语义，实际声明采用
  `numeric_dollar`，这一点在本表明确。
- `text("... :name ...")` 由 SQLAlchemy 编译阶段按 `paramstyle` 转换；适配器只接收最终 SQL 与参数。
- 专项测试断言：传给 `cursor.execute` 的 `operation` 字符串中，PG 不含 `:name` 且为 `$n`；
  MySQL/SQLite 为 `?`；且适配器中不存在任何 `.replace()` 类 SQL 处理。
- 注意 rsqlx 核心 `execute_many` 的多值 INSERT 改写只识别 `?`。PG 方言下 SQLAlchemy 生成 `$n`，
  会退化为逐条执行；这是**已知限制**，不通过适配器字符串重写来“优化”，
  可在方言中提供 `insertmanyvalues` 支持（SQLAlchemy 自带多值 INSERT 编译）来规避。

## 7. 输出格式转换策略与 row_factory 评估

### 7.1 现状

- 核心 `fetch()` → `list[dict]`，键为 `col.name()`，无重复列名处理。
- 没有 tuple / namedtuple 输出；`row_to_dict` 是唯一行构造路径。
- SQLAlchemy 期望行是序列（可用 `[0]` 访问）且支持属性访问（`.name`）。

### 7.2 三个方案

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| A. 不改核心 | 适配器 `tuple(row.values())` 转换 | 核心零改动、零回归风险、迭代最快 | 每行 O(列数) 的 Python 层拷贝；大结果集有可观开销 |
| B. 核心加 `row_factory` | `fetch*(..., row_factory="dict"｜"tuple"｜"namedtuple")`，默认 dict | 方言直接用 tuple；通用驱动能力；可实测性能 | 需改 Rust 并在热路径引入分支；namedtuple 需动态类缓存 |
| C. 核心只加 tuple | 仅 `"dict"` / `"tuple"` | 折中，避开 namedtuple 复杂度 | 不满足用户列出的 namedtuple 模式 |

### 7.3 决策：核心实现方案 B，适配器采用方案 A 的转换

阶段 1 已在核心实现 `row_factory`（`dict` 默认 / `tuple` / `namedtuple`），
默认路径代码结构未变，只在每次调用解析一次枚举。基准（debug 构建，1000 行 × 30 次中位）
`dict` 7.87ms、`tuple` 7.54ms、`namedtuple` 11.76ms；默认路径额外开销仅为一次枚举比较，
满足闸门要求。

**但方言适配器实际使用 `row_factory="dict"` 再在 Python 层转 `tuple(row.values())`**，原因：

1. SQLAlchemy 的 `Row._mapping` 依赖 `cursor.description` 中的**原始列名**。
   `tuple` 模式不携带列名；`namedtuple` 模式要求列名是合法 Python 标识符，
   `SELECT COUNT(*)`、`SELECT 1 AS "a-b"` 这类列名会直接抛 `ValueError`。
2. 核心随后实现了 `Pool.describe()` / `Transaction.describe()`（基于 `prepare()`，
   不启用 sqlx `offline` feature）；空结果时由**当前 target**（事务或池）取列名。
   注意不能在事务占用唯一连接时从池 `describe()`，那会等待 30s 超时。
3. dict→tuple 的每行成本实测约 0.3ms/1000 行，远小于正确性与兼容性风险。

若后续要进一步降低转换开销，可让方言切换到 `row_factory="tuple"`：
`describe()` 已能提供列名，届时需重跑占位符与输出格式专项测试。

## 8. 类型映射表

| SQLAlchemy 类型 | rsqlx 绑定值 | rsqlx 返回值 | 方言处理 |
|---|---|---|---|
| String / Text / Unicode | `str` | `str` | 直通 |
| Integer / SmallInteger / BigInteger | `int` | `int` | 直通 |
| Numeric / Decimal | `Decimal` | `Decimal`（PG/MySQL），SQLite 为 `str` | SQLite 加 `result_processor(Decimal)` |
| Float / Real | `float` | `float` | 直通 |
| Boolean | `bool` | `bool` | 直通 |
| Date / DateTime / Time | `date`/`datetime`/`time` | 同名对象；PG `TIMESTAMPTZ` 为 aware | 直通 |
| LargeBinary | `bytes` | `bytes` | 直通 |
| JSON / JSONB | dict/list → rsqlx JSON | 自动解码为 dict/list | 覆盖 SQLAlchemy 默认 JSON 序列化，改为原生对象传递 |
| UUID（PG） | `uuid.UUID` | `uuid.UUID` | 直通 |
| ARRAY（PG） | `list` | `list`（保留 None） | 直通 |
| INTERVAL / INET / range / 自定义 | 不支持 | 核心抛 `InterfaceError` | 映射为 DBAPI `NotSupportedError`，文档列出 |

核心已覆盖的类型见 `src/backend.rs` 的 `pg_value_to_py` / `mysql_value_to_py` / `sqlite_value_to_py`。
SQLite 无原生 Decimal/UUID，绑定时转字符串、读回为字符串，由方言的 result_processor 补正。

## 9. 异常映射表

DBAPI 异常类在 `sqlalchemy_rsqlx/dbapi.py` 定义：`Error` / `InterfaceError` / `DatabaseError` /
`IntegrityError` / `DataError` / `OperationalError` / `ProgrammingError` / `NotSupportedError`。

| rsqlx 异常 | DBAPI 异常 | 备注 |
|---|---|---|
| `InterfaceError`（含事务二次使用） | `ProgrammingError` | API 误用 |
| `DatabaseError` | 按 code 分类 | 见下 |
| `OperationalError` | `OperationalError` | IO / worker |
| `RowNotFound` | 不抛异常 | cursor.fetchone 返回 None |
| `PoolTimedOut` | `OperationalError` | 获取连接超时 |
| `PoolClosed` | `OperationalError` | 池已关闭 |
| `MigrateError` | `DatabaseError` | 迁移失败 |

`DatabaseError` 细分（code 从异常消息 `(code: XXXX)` 正则提取，并挂到异常对象 `.code`）：
- PG SQLSTATE `23xxx` → `IntegrityError`；`22xxx` → `DataError`；`42xxx` → `ProgrammingError`
- MySQL `1062`/`1451`/`1452` → `IntegrityError`；`1406` → `DataError`；`1064` → `ProgrammingError`
- SQLite `19` → `IntegrityError`；`1` → `ProgrammingError`；`5` → `OperationalError`
- 其余 → `DatabaseError`

根因：核心目前只把错误码拼进消息，异常对象无结构化属性。阶段 6 评估给核心异常增加 `.code` 属性，
在不破坏现有 `str(e)` 的前提下暴露它；未实现前由适配器解析消息。

## 10. DSN 解析与对齐

| SQLAlchemy URL | rsqlx DSN |
|---|---|
| `postgresql+rsqlx://u:p@h:5432/db?sslmode=require` | `postgres://u:p@h:5432/db?sslmode=require` |
| `mysql+rsqlx://u:p@h:3306/db?ssl-mode=disabled` | `mysql://u:p@h:3306/db?ssl-mode=disabled` |
| `sqlite+rsqlx:///abs/path.db` | `sqlite:/abs/path.db` |
| `sqlite+rsqlx:///:memory:` | `sqlite::memory:` |

实现：`create_connect_args(url)` 把 `drivername` 归一化为 `postgresql`/`mysql`/`sqlite` 后
`render_as_string(hide_password=False)`，再做 scheme 替换（`postgresql:` → `postgres:`）。
密码由 SQLAlchemy 解码后由 `render_as_string` 正确转义，不做手工字符串拼接。
`pool_*` 参数从 query 中提取并按第 4 节映射，剩余 query 原样保留给 rsqlx。

## 11. 测试计划

| 类别 | 覆盖 | 验收点 |
|---|---|---|
| 三库 CRUD（SQLite 必跑，PG/MySQL env-gated） | `create_async_engine` → `begin` → `create table` → insert → select | 结果正确、无死锁 |
| 事务 | `engine.begin()` 提交；异常回滚；手动 `commit`/`rollback` | 数据状态与预期一致 |
| 批量 | `executemany` / `insert().values([...])` | 行数正确、last_insert_id（MySQL/SQLite） |
| 类型往返 | datetime/Decimal/UUID/JSON/bytes/PG 数组 | 往返值相等 |
| 占位符专项 | `text("... :name ...")` 三库 | 生成 `$n` / `?`，适配器未重写 |
| 输出格式专项 | `result.scalars()` / `row.name` / `row[0]` | 属性与位置访问都正确 |
| 异常映射 | 唯一键冲突 / 语法错误 / 连接关闭 | 异常类正确、`.code` 保留 |
| FastAPI | 最小示例可启动并跑一次请求 | 实际 HTTP 请求返回 |
| 性能冒烟 | 10 个并发 `pg_sleep(0.05)` 类查询 | 事件循环 tick 间隔不被显著拉长 |

数据库服务不可用时，对应测试明确 skip 并标注；不以 mock 冒充真实事务验证。

## 12. 分阶段实施计划

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| 1 | 核心 `row_factory` 实现 + 测试 + 基准闸门 | 已完成：默认路径回归 ≤3%；tuple/namedtuple 可用；`describe()` 未实现（适配器改用 dict→tuple） |
| 2 | 包骨架 + SQLite 方言闭环 | SQLite 全套测试通过 |
| 3 | PostgreSQL 方言 | `$n`/数组/UUID/JSON 测试通过 |
| 4 | MySQL 方言 | `?`/last_insert_id/批量测试通过 |
| 5 | entry point / 打包 / CI / 文档 / FastAPI 示例 / 性能冒烟 | 安装后无需 register 即可用 |
| 6 | 核心最小增强审查（`.code`、`describe()`、池统计）与最终验收 | 无框架依赖、无占位符重写 |

## 13. 阶段 6：核心最小增强审查结论

| 候选增强 | 结论 | 理由 |
|---|---|---|
| 异常对象增加结构化 `.code` / `.sqlstate` | **暂不实现** | 适配层已从 `(code: XXXX)` 消息正则提取并挂到 DBAPI 异常 `.code`；三库测试通过。核心改动会触碰异常构造器与所有映射分支，收益不足 |
| `describe(sql) -> list[str]` | **已实现** | `Pool.describe()` 与 `Transaction.describe()` 基于 `prepare()`，不启用额外 sqlx feature；解决空结果集 `description=None` 导致的 `ResourceClosedError` |
| 连接池统计暴露 | **无需改动** | 核心 `Pool` 已有 `size` / `num_idle` / `is_closed` 同步 getter，方言通过 `get_driver_connection()` 可取到 `Pool` |
| 核心 SQL 占位符统一 | **明确禁止** | 与硬约束冲突；由方言 `paramstyle` 解决 |
| 核心依赖 SQLAlchemy / Django | **明确禁止** | 核心包 `pyproject.toml` 依赖列表未变 |

最终：核心仅新增 `row_factory`，未引入任何框架依赖、未添加 SQL 字符串重写逻辑；
默认 `list[dict]` 行为由原有测试与新增 row_factory 测试共同守护。

## 14. 已识别风险与对策

| 风险 | 对策 |
|---|---|
| `await_only` 上下文错误导致死锁/报错 | 严格 greenlet 桥接；测试覆盖错误上下文；不用 `asyncio.run()` |
| 空结果集 `description` 缺失 | 已解决：核心 `describe()` 在空结果时补列名；适配器必须在当前 target（事务或池）上调用，避免在事务占用唯一连接时从池获取而超时 |
| rsqlx 事务一次性语义与 SQLAlchemy 生命周期不一致 | 每次 commit/rollback 后置 `None`，下次惰性重建 |
| PG `execute_many` 不为 `$n` 做批量改写 | 方言层用 SQLAlchemy `insertmanyvalues`，不在适配器改 SQL |
| JSON 双重序列化 | 方言覆盖 bind/result processor，使用原生对象 |
| 默认 dict 输出被破坏 | `row_factory` 默认 `None`，全套现有测试回归 |
| 核心被框架污染 | 核心不 import 任何框架；方言包独立发布 |
