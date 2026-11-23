# rsqlx 与 pymysql / psycopg2 功能差异

rsqlx 是 **asyncio 原生驱动**（Rust/sqlx + PyO3），不是同步 DBAPI 驱动。与 pymysql、
psycopg2 的差异中，很大一部分是"同步 DBAPI vs 异步驱动"的范式差异，而非能力缺失。
本文档区分三类：**范式差异（已有等价）**、**真实缺失**、**rsqlx 独有优势**。

对标版本：pymysql 1.2.0 / psycopg2-binary 2.9.12 / rsqlx 0.9.4。

---

## 1. 与 pymysql 对比（MySQL）

### 1.1 范式差异 / 已有等价

| pymysql | rsqlx 等价 |
|---|---|
| 同步 `cursor.execute()` → `fetchone/fetchmany` | `await pool.fetch/fetch_one/fetch_optional` |
| `DictCursor` / `SSCursor` / `SSDictCursor` | 每次调用的 `row_factory="dict"/"tuple"/"namedtuple"` |
| 连接级 `autocommit()` 开关 | 事务由 `async with pool.begin()` 显式管理；SQLAlchemy adapter 用 `isolation_level` 模拟 |
| `cursorclass` 参数 | `row_factory` 参数 |

### 1.2 真实缺失

| 功能 | pymysql 位置 | rsqlx 现状 | 是否建议补 |
|---|---|---|---|
| `ping(reconnect)` 带重连 | `connections.py:620` | 方言层 `ping()` 仅 `SELECT 1`，无重连 | 中：可补 `Pool.ping()`，重连语义需设计 |
| `escape` / `literal` / `escape_string` | `connections.py:530-559` | 无（强制参数绑定，更安全） | 低：与"不接受 SQL 拼接"的设计相悖 |
| `show_warnings` / `select_db` / `thread_id` / `get_server_info` | `connections.py:514-521,1127-1207` | 无 | 低：诊断类，可用 SQL 查询替代 |
| `cursor.scroll` / `nextset` / `mogrify` | `cursors.py:103,116,230` | 无（结果一次性返回） | 低 |
| `callproc` | `cursors.py:314` | 可用 `execute_raw("CALL ...")` 替代 | 已覆盖 |
| `conv` 自定义类型转换器 | `connections.py:122-125` | 映射固定（除 `JsonArray`） | 中：可扩展为绑定时 type hint |
| 显式 SSL 参数（SSLContext / ssl_ca/cert/key/verify_*） | `connections.py:134-145` | 仅 URL 查询串（如 `?ssl-mode=disabled`） | 中：可补 kwargs，但 URL 已能表达大部分场景 |
| `init_command` / `read_default_file` / `local_infile` / `defer_connect` | `connections.py:131,148,151,160` | 无 | 低 |

### 1.3 rsqlx 相对 pymysql 的优势

- 内置异步连接池（`min/max_connections`、`acquire_timeout`、`idle_timeout`、`max_lifetime`、`size/num_idle/is_closed`）；pymysql 无内置池
- 一套 `Pool` API 统一 MySQL / PostgreSQL / SQLite
- `execute_many` 自动把单行 INSERT 改写为多值 INSERT（性能快两个数量级）
- `execute_raw` / `fetch_raw` 走简单查询协议，支持存储过程与多语句
- 内置 `migrate()`（sqlx 迁移目录、幂等）
- `describe()` 只取列名不取行
- 异常细分（`RowNotFound` / `PoolTimedOut` / `PoolClosed` / `MigrateError`）
- SQLAlchemy 2.0 异步方言（`sqlalchemy_rsqlx`）

---

## 2. 与 psycopg2 对比（PostgreSQL）

### 2.1 范式差异 / 已有等价

| psycopg2 | rsqlx 等价 |
|---|---|
| `async=True` + `poll()` / `set_wait_callback` | rsqlx 本身就是 asyncio 驱动，无需轮询 |
| `ThreadedConnectionPool` / `SimpleConnectionPool` | 内置 `Pool`（含完整池参数与状态） |
| `RealDictCursor` / `NamedTupleCursor` | `row_factory="dict"/"namedtuple"` |
| `cursor.description` | `describe()` / 方言层 DBAPI `description` |
| `extras.execute_values` / `execute_batch` | `execute_many` 自动多值 INSERT 改写 |

### 2.2 真实缺失

| 功能 | psycopg2 | rsqlx 现状 | 是否建议补 |
|---|---|---|---|
| **COPY 协议**（`copy_from/copy_to/copy_expert`） | 高速批量导入导出 | 无 | 高：PG 场景常用，可用 `execute_raw("COPY ...")` 部分替代但无流式协议 |
| 服务端游标 `cursor(name=...)` | 流式 fetch | 无 | 中：大结果集场景有价值 |
| **异步通知** `notifies` / LISTEN/NOTIFY | 事件驱动 | 无 | 中：PG 特有，需与 asyncio 集成 |
| 会话/事务细粒度控制：`set_session`、`autocommit` 属性、隔离级别常量、`get_transaction_status`、`tpc_*`（两阶段提交） | 完整 | 仅 `begin/commit/rollback` | 中-高：企业事务场景 |
| 扩展类型系统：`register_type` / `register_adapter` / `new_type` / `register_hstore/composite/inet` | 可扩展 | 类型映射固定（除 `JsonArray`） | 中：自定义类型/领域类型场景 |
| SQL 组合器 `sql.SQL/Identifier/Literal/Composed` | 安全构造标识符 | 无 | 中：动态标识符场景（参数绑定无法替代） |
| 大对象 `lobject`、逻辑/物理复制 | 特殊场景 | 无 | 低 |
| `mogrify` / `callproc` | 辅助 | 无 / `execute_raw("CALL")` 替代 | 低 |

### 2.3 rsqlx 相对 psycopg2 的优势

- 真正异步（GIL 释放 + Tokio 多线程运行时），慢查询并发等待重叠（实测 5x 优于同步驱动）
- 内置连接池与池状态；psycopg2 需额外 `psycopg2.pool`
- 一套 API 统一三库
- `row_factory` 每次调用可选
- `execute_many` 自动多值 INSERT 改写
- `describe()`、`migrate()`、`execute_raw/fetch_raw`
- JSONB[] 显式绑定 `JsonArray`
- 与 sqlx 版本对齐的类型系统（含 OID/INET/CIDR/MACADDR/BIT/INTERVAL/任意精度 NUMERIC 等）

---

## 3. 测试覆盖的事实

- `verify_mysql.py`：33 项 check，覆盖连接、CRUD、类型对照、NULL、事务、批量、并发、错误、迁移、性能基线——**仅对照 pymysql 的功能子集**
- `verify_pg.py`：33 项 check，覆盖同类项目——**仅对照 psycopg2 的功能子集**
- 上述"真实缺失"项大多未被交叉验证覆盖，因为 rsqlx 没有对应 API

---

## 4. 补齐优先级建议

| 状态 | 功能 | 实现 |
|---|---|---|
| ✅ 已完成 | PG COPY 协议 | `pool.copy_in(sql, data)` / `pool.copy_out(sql)`，基于 `PgPoolCopyExt` |
| ✅ 已完成 | 显式事务隔离级别 | `pool.begin_with("BEGIN ISOLATION LEVEL ...")`（PG）/ MySQL 用 `SET SESSION` + `begin()` |
| ✅ 已完成 | LISTEN/NOTIFY | `listener = await pool.listen()` → `await listener.listen(ch)` / `await listener.recv()` / `await listener.close()` |
| ✅ 已完成 | 服务端游标 / 流式 fetch | `cur = await pool.cursor(sql, batch_size)` → `await cur.fetch_batch()` / `await cur.close()` |
| ✅ 已完成 | `Pool.ping()` |  acquire + `Connection::ping()`；sqlx 默认 acquire 前自动检测死连接并替换（等价重连） |
| P3 | SQL 标识符组合器 | 动态标识符（表名/列名）场景，参数绑定无法替代 |
| 不补 | `escape`/`mogrify`/`conv`/`register_type` | 与参数绑定优先、类型映射固定的设计相悖；可用 SQL cast 或适配层解决 |

### 实现说明

- **COPY**：`copy_in` 一次发送全部字节（CSV/text/binary），返回影响行数；`copy_out` 返回 `list[bytes]` 块。仅 PG 支持，其他库抛 `NotImplementedError`
- **隔离级别**：`begin_with` 的 statement 是**替代整个 BEGIN 语句**（非追加），需传完整 SQL。MySQL 8.0.16 不支持 `START TRANSACTION ISOLATION LEVEL` 单语句，用 `SET SESSION TRANSACTION ISOLATION LEVEL X` + 普通 `begin()`
- **LISTEN/NOTIFY**：`recv()` 返回 `(channel, payload, process_id)` tuple；监听器持有专用连接，用完必须 `await close()`
- **服务端游标**：内部先 `BEGIN` 再 `DECLARE CURSOR`（PG 要求），分批 `FETCH`，`await close()` 时 `CLOSE` + `COMMIT` 释放连接；用完必须关闭
- **ping**：sqlx pool 默认 `test_before_acquire(true)`，死连接自动剔除重建，因此 ping 无需显式重连逻辑

**结论**：rsqlx 在"驱动核心能力"（连接、查询、事务、类型、池、错误、批量、迁移、
COPY、通知、游标、隔离级别、ping）上已对齐甚至超越 pymysql/psycopg2。剩余差异集中
在 DBAPI 生态工具（转义、诊断、SQL 组合、自定义类型适配器），这些与 rsqlx 的参数绑定
优先、固定类型映射设计相悖，应由 SQLAlchemy `TypeDecorator` 等适配层解决。
