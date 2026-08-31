# rsqlx

[English](README.md) | **中文**

Rust 驱动的 Python 异步数据库客户端，底层基于 [sqlx](https://github.com/launchbadge/sqlx)，通过 PyO3 以原生扩展的形式暴露给 Python。思路和 orjson 一致——核心逻辑用 Rust 写，对外是一个 `.pyd`/`.so` 模块。

所有查询跑在一个全局的多线程 Tokio runtime 上，等待数据库返回期间释放 GIL，所以多线程 / 多协程场景下能真正并行。

> 作者: Yingzi <yingzilkq@163.com>  
> 仓库: https://gitee.com/yingzi_shadow/rsqlx  
> 协议: MIT OR Apache-2.0

## 功能一览

- 三库统一接口: PostgreSQL、MySQL、SQLite，一个 `connect()` 搞定
- 连接池: 支持 min/max connections、acquire timeout、idle timeout、max lifetime
- 事务: `async with` 语法，异常自动回滚
- 批量写入: `execute_many` 对 INSERT 语句自动改写为多值 INSERT，性能比逐条快两个数量级
- 原生查询: `execute_raw` / `fetch_raw` 走 COM_QUERY 协议，支持存储过程、DDL 等预编译协议不支持的语句
- 迁移: 兼容 sqlx 的 `<N>_<name>.up.sql` 迁移文件格式
- 类型映射: datetime、Decimal、UUID、JSON、bytes、PG 数组等全双向转换
- 异常体系: `rsqlx.Error` 为基类，下分 DatabaseError、InterfaceError、OperationalError 等

## 安装

预编译 wheel 覆盖三大平台:

| 平台 | 架构 |
|------|------|
| Linux (glibc ≥ 2.28) | x86_64, aarch64 |
| Windows 10+ | x86_64, ARM64 |
| macOS 11+ | x86_64, arm64 |

每个 (平台, 架构) 组合对应一个 wheel，兼容 CPython 3.9–3.13。

TLS 用的是 rustls + ring（纯 Rust 实现），不依赖系统的 OpenSSL 或 Schannel。SQLite 通过 libsqlite3-sys 静态链接进 wheel，用户侧零系统依赖。

```bash
pip install rsqlx
```

从源码构建需要 Rust 工具链和 [maturin](https://github.com/PyO3/maturin):

```bash
pip install maturin
maturin build --release -o dist
pip install dist/rsqlx-*.whl

# 开发模式，直接装进当前 venv:
maturin develop --release
```

## 快速上手

三个数据库共用同一套 `connect()` / `Pool` / `Transaction` API。连接字符串遵循 sqlx 约定：

```python
# SQLite:     "sqlite:app.db"（文件）或 "sqlite::memory:"（内存）  —— 占位符 ?
# PostgreSQL: "postgres://user:pass@localhost:5432/db"          —— 占位符 $1, $2, ...
# MySQL:      "mysql://user:pass@localhost:3306/db?ssl-mode=disabled" —— 占位符 ?
#             （若服务端 TLS 与 rustls 不兼容，加上 ?ssl-mode=disabled）
import asyncio
import rsqlx
```

### SQLite

```python
import asyncio
import rsqlx

async def main():
    pool = await rsqlx.connect("sqlite::memory:", max_connections=5)

    await pool.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, age INT)"
    )

    # execute 返回 ExecuteResult(rows_affected, last_insert_id)
    res = await pool.execute("INSERT INTO users (name, age) VALUES (?, ?)", ["Alice", 30])
    print(res.rows_affected, res.last_insert_id)            # 1 1

    # fetch 返回 list[dict]
    rows = await pool.fetch("SELECT * FROM users WHERE age > ?", [18])
    print(rows)                                            # [{'id': 1, 'name': 'Alice', 'age': 30}]

    # 单行辅助方法
    user  = await pool.fetch_one("SELECT * FROM users WHERE id = ?", [1])        # 无行时抛 RowNotFound
    maybe = await pool.fetch_optional("SELECT * FROM users WHERE id = ?", [99])  # 无行返回 None

    # 事务：正常退出自动提交，异常自动回滚
    async with await pool.begin() as tx:
        await tx.execute("INSERT INTO users (name, age) VALUES (?, ?)", ["Bob", 25])

    await pool.close()

asyncio.run(main())
```

### PostgreSQL

```python
import asyncio
import rsqlx

async def main():
    pool = await rsqlx.connect("postgres://user:pass@localhost:5432/db", max_connections=5)

    await pool.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id SERIAL PRIMARY KEY, name TEXT, age INT, tags TEXT[])"
    )

    await pool.execute("INSERT INTO users (name, age) VALUES ($1, $2)", ["Alice", 30])

    # 纯标量 list 自动绑定为 PG 原生数组（保留 None 元素）
    await pool.execute(
        "INSERT INTO users (name, age, tags) VALUES ($1, $2, $3)",
        ["Bob", 25, ["python", "rust", None]],
    )

    user = await pool.fetch_one("SELECT * FROM users WHERE id = $1", [1])
    print(user["tags"])                                    # ['python', 'rust', None]

    # 批量写入：单行 INSERT 自动改写为多值 INSERT
    await pool.execute_many(
        "INSERT INTO users (name, age) VALUES ($1, $2)",
        [["Carol", 28], ["Dave", 40]],
    )

    await pool.close()

asyncio.run(main())
```

### MySQL

```python
import asyncio
import rsqlx

async def main():
    # 若服务端 TLS 与 rustls 不兼容（MySQL 8 默认配置常见），加 ?ssl-mode=disabled
    pool = await rsqlx.connect(
        "mysql://user:pass@localhost:3306/db?ssl-mode=disabled", max_connections=5
    )

    await pool.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), age INT)"
    )

    await pool.execute("INSERT INTO users (name, age) VALUES (?, ?)", ["Alice", 30])
    rows = await pool.fetch("SELECT * FROM users WHERE age > ?", [18])

    # 存储过程、多语句脚本等预编译协议不支持的语句用原生协议 execute_raw / fetch_raw
    await pool.execute_raw("DROP PROCEDURE IF EXISTS sp_hello")
    await pool.execute_raw(
        "CREATE PROCEDURE sp_hello(IN n VARCHAR(64)) BEGIN SELECT n; END"
    )
    await pool.execute("CALL sp_hello(?)", ["world"])

    await pool.close()

asyncio.run(main())
```

### 迁移

```python
# 执行目录下的 sqlx 风格迁移文件（只应用一次，幂等）:
#   migrations/0001_init.up.sql
#   migrations/0002_seed.up.sql
await pool.migrate("migrations")
```

### 占位符

跟着数据库走: PostgreSQL 用 `$1, $2, ...`，MySQL 和 SQLite 用 `?`。批量 INSERT 时 rsqlx 会自动把 `?` 改写为对应格式。

## API 速查

### `rsqlx.connect(url, *, min_connections=None, max_connections=None, acquire_timeout=None, idle_timeout=None, max_lifetime=None) -> Pool`

超时参数单位为秒（`float`）。

### Pool

| 方法 | 说明 |
|------|------|
| `await fetch(sql, params=None) -> list[dict]` | 返回所有行 |
| `await fetch_one(sql, params=None) -> dict` | 返回一行，无行时抛 `RowNotFound` |
| `await fetch_optional(sql, params=None) -> dict | None` | 返回一行或 None |
| `await execute(sql, params=None) -> ExecuteResult` | 执行语句，返回影响行数和 last_insert_id |
| `await execute_many(sql, params) -> ExecuteResult` | 批量执行，INSERT 自动优化为多值 |
| `await execute_raw(sql)` | 原生查询协议（无参数绑定），用于存储过程等 |
| `await fetch_raw(sql) -> list[dict]` | 原生查询协议带返回行 |
| `await begin() -> Transaction` | 开启事务 |
| `await migrate(path)` | 执行迁移目录中的 SQL 文件 |
| `await close()` | 关闭连接池 |
| `size` / `num_idle` / `is_closed` | 连接池状态 |

Pool 和 Transaction 都支持 `async with`。

### 参数类型

支持 None、bool、int、float、str、bytes/bytearray、datetime（naive 或 aware）、date、time、Decimal、UUID、dict/list/tuple（转 JSON）。超过 64 位的整数自动降级为 Decimal。纯标量的 list/tuple 在 PG 上绑定为原生数组，含 dict 或嵌套结构的绑定为 JSON。

### 返回值类型映射

| 数据库类型 | Python 类型 |
|-----------|------------|
| BOOL / BOOLEAN | `bool` |
| INT2/INT4/INT8, TINYINT..BIGINT (含 UNSIGNED), INTEGER | `int` |
| FLOAT4/FLOAT8, FLOAT/DOUBLE, REAL | `float` |
| NUMERIC / DECIMAL | `decimal.Decimal`（SQLite 存为 TEXT） |
| TEXT, VARCHAR, CHAR, NAME, ENUM, SET | `str` |
| BYTEA, BINARY/BLOB 系列 | `bytes` |
| JSON / JSONB | 自动解析为 `dict` / `list` / 标量 |
| UUID (PG) | `uuid.UUID` |
| DATE | `datetime.date` |
| TIME | `datetime.time` |
| TIMESTAMP / DATETIME | naive `datetime.datetime` |
| TIMESTAMPTZ | aware `datetime.datetime`（带时区） |
| PG 数组 | `list`（保留 None 元素） |

不支持的类型（PG 的 INET、INTERVAL、range、自定义类型等）会抛 `InterfaceError`，可以在 SQL 里 `cast(col as text)` 取回字符串。

### 异常

```
rsqlx.Error
├── InterfaceError        # 类型不支持、解码失败、API 误用
├── DatabaseError         # 服务端报错（语法错误、约束冲突等）
├── OperationalError      # IO 错误、worker 崩溃、后台任务失败
├── RowNotFound           # fetch_one 无行返回
├── PoolTimedOut          # 从连接池获取连接超时
├── PoolClosed            # 操作了已关闭的连接池
└── MigrateError          # 迁移执行失败
```

## 限制说明

- 必须在 asyncio 事件循环中使用（标准的 `asyncio.run` / `async def`）
- sqlx 的编译期 `query!` 宏是 Rust 编译期特性，Python 侧只提供运行时查询
- 取消协程（如 `asyncio.timeout`）会取消 await，但已发到数据库的查询会在后台跑完
- MySQL 连接如果服务端默认用 DHE cipher（如 MySQL 8.0.16），rustls 不支持 DHE，需要在 URL 里加 `?ssl-mode=disabled`

## 从 pymysql 迁移

| pymysql | rsqlx |
|---------|-------|
| `pymysql.connect(host=..., user=..., password=...)` | `await rsqlx.connect("mysql://user:pass@host/db")` |
| `cursor.execute(sql, (a, b))` | `await pool.execute(sql, [a, b])` |
| `cursor.fetchone()` | `await pool.fetch_one(sql, [a, b])` |
| `cursor.fetchall()` | `await pool.fetch(sql, [a, b])` |
| `cursor.executemany(sql, args)` | `await pool.execute_many(sql, args)` |
| `conn.begin() / conn.commit() / conn.rollback()` | `async with await pool.begin() as tx: ...` |
| `conn.insert_id()` | `result.last_insert_id` |
| `conn.affected_rows` | `result.rows_affected` |
| `cursor.callproc(name, args)` | `await pool.execute("CALL name(?)", args)` (DDL 用 `execute_raw`) |
| `paramstyle = 'pyformat'` (%s) | `?` (qmark) |
| `DictCursor` | 默认返回 dict |
| 错误: `pymysql.IntegrityError` | `rsqlx.DatabaseError` |

主要改动: 同步改异步（`def` → `async def`，调用处加 `await`），`%s` 占位符改 `?`。

## 版本同步: 跟踪 sqlx 上游更新

rsqlx 的核心依赖是 sqlx，上游发布新版本时你可能想同步。下面是具体的操作流程。

### 1. 检查 sqlx 新版本

```bash
# 查看 crates.io 上的最新版本
cargo search sqlx

# 或者去 GitHub 看 release notes
# https://github.com/launchbadge/sqlx/releases
```

### 2. 更新 Cargo.toml 中的 sqlx 版本号

打开 `Cargo.toml`，修改 `sqlx` 的版本号:

```toml
[dependencies]
# 比如 0.8.6 升到 0.9.0
sqlx = { version = "0.9", default-features = false, features = [
    "runtime-tokio",
    "tls-rustls-ring",
    "postgres",
    "mysql",
    "sqlite",
    "chrono",
    "uuid",
    "rust_decimal",
    "migrate",
    "json",
] }
```

### 3. 检查 MSRV（最低支持 Rust 版本）

sqlx 每个 major/minor 版本可能调整 MSRV。去 sqlx 的 `Cargo.toml` 或 release notes 看 `rust-version` 字段。如果新版本要求的 Rust 比你当前高:

```bash
# 更新 Rust 工具链
rustup update stable
# 或装指定版本
rustup install 1.94.0
rustup override set 1.94.0
```

### 4. 检查 feature 名称变化

sqlx 偶尔会重命名 feature。对照新版本的 `Cargo.toml`（在 [crates.io](https://crates.io/crates/sqlx) 或 [GitHub](https://github.com/launchbadge/sqlx/blob/main/sqlx/Cargo.toml) 上可查），确认以下 feature 仍然存在:

- `runtime-tokio` — 异步 runtime
- `tls-rustls-ring` — TLS 后端（旧版可能叫 `tls-rustls`）
- `postgres` / `mysql` / `sqlite` — 三个数据库驱动
- `chrono` / `uuid` / `rust_decimal` / `json` — 类型支持
- `migrate` — 迁移功能

如果 feature 被拆分或重命名，编译时会报 `unknown feature`，按报错改就行。

### 5. 编译验证

```bash
cargo check
```

常见的破坏性变更:

- **API 签名变化**: sqlx 内部重构 trait 方法签名。看编译错误，对照 sqlx 的 CHANGELOG 调整 `src/backend.rs` 里的 trait bound 和类型引用。
- **类型信息 API 变化**: sqlx 调整 `TypeInfo` / `Column` trait 的方法。影响 `backend.rs` 中的行解码逻辑（`col.type_info().name()` 等）。
- **Query 泛型参数变化**: sqlx 0.7 到 0.8 把 `Query<'q, DB>` 改成了 `Query<'q, DB, A>` 加了 Arguments 泛型。类似的变化看编译错误处理。
- **新增数据库类型**: sqlx 新增类型支持时，`backend.rs` 的解码分支可能需要补上对应类型名。

### 6. 跑测试验证

```bash
# SQLite 不需要外部服务，先跑这个
python -m pytest tests/test_sqlite.py -v

# PostgreSQL (需要起一个 PG 实例)
$env:RSQLX_TEST_PG_URL = "postgres://postgres:pass@127.0.0.1:5432/postgres"
python tests/verify_pg.py

# MySQL (需要起一个 MySQL 实例)
$env:RSQLX_TEST_MYSQL_URL = "mysql://root:pass@127.0.0.1:3306/testdb?ssl-mode=disabled"
python tests/verify_mysql.py
```

### 7. 是否需要下载 sqlx 源码？

**大多数情况下不需要。** sqlx 发布到 crates.io 的就是完整源码，`cargo build` 会自动下载编译。你只需要改 `Cargo.toml` 里的版本号。

需要看源码的场景:
- 调试类型映射问题时（看 `sqlx-postgres/src/types/` 下的具体类型实现）
- sqlx API 变化大、编译错误看不懂时（看 trait 定义和方法签名）
- 贡献上游修复时

源码在 cargo registry 缓存里，路径类似:

```
~/.cargo/registry/src/index.crates.io-*/sqlx-<version>/
```

也可以直接从 GitHub clone:

```bash
git clone --branch v0.9.0 https://github.com/launchbadge/sqlx.git
# 查看特定文件
cat sqlx/sqlx-postgres/src/types/info.rs
```

### 8. 更新 rsqlx 自身版本号

同步 sqlx 版本后，建议 rsqlx 也升版本:

- sqlx patch 升级（0.8.5 → 0.8.6）: rsqlx patch 升（0.1.0 → 0.1.1）
- sqlx minor 升级（0.8 → 0.9）: rsqlx minor 升（0.1 → 0.2）

改 `Cargo.toml` 和 `pyproject.toml` 里的 `version`，然后:

```bash
git tag v0.2.0
git push origin v0.2.0
# GitHub Actions 会自动构建三平台 wheel 并发布到 PyPI
```

### 同步检查清单

升 sqlx 版本时逐项确认:

- [ ] `Cargo.toml` 里 sqlx 版本号已更新
- [ ] `rustup` 的 Rust 版本 ≥ sqlx 新版 MSRV
- [ ] `cargo check` 通过（feature 名称、API 签名）
- [ ] `tests/test_sqlite.py` 全过
- [ ] `tests/verify_pg.py` 全过
- [ ] `tests/verify_mysql.py` 全过
- [ ] 类型映射表（`backend.rs` 中的 `pg_value_to_py` / `mysql_value_to_py` / `sqlite_value_to_py`）和 sqlx 新版的类型名一致
- [ ] `Cargo.toml` 和 `pyproject.toml` 的 rsqlx 版本号已更新
- [ ] CI `.github/workflows/` 中的 Rust 版本 ≥ 新 MSRV（如需要改 `dtolnay/rust-toolchain` 的版本）

## 文档

| 文档 | 内容 |
|------|------|
| [README_CN.md](README_CN.md) | 本文件：功能介绍、安装、API 速查、类型映射、pymysql 迁移 |
| [README.md](README.md) | English version |
| [DEVELOPMENT.md](DEVELOPMENT.md) | 实现原理、踩坑记录、maturin 打包、PyPI 发布流程、用户安装 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

想了解 rsqlx 内部是怎么实现的、或者要发布到 PyPI，看 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 项目结构

```
rsqlx/
├── Cargo.toml              # Rust 依赖和构建配置
├── pyproject.toml          # Python 包元数据 + maturin 构建
├── Dockerfile              # Linux 从零构建验证
├── README.md               # English
├── README_CN.md            # 你正在看的这个
├── DEVELOPMENT.md          # 实现原理 + 打包发布指南
├── CHANGELOG.md            # 版本变更记录
├── .github/workflows/
│   ├── build.yml           # 三平台 wheel 构建 + PyPI 发布
│   └── tests.yml           # 三平台功能验证 (SQLite + PG + MySQL)
├── src/
│   ├── lib.rs              # 模块注册、异常定义、connect()
│   ├── pool.rs             # Pool 类: CRUD、事务、迁移、execute_raw
│   ├── transaction.rs      # Transaction 类: 事务内操作
│   ├── backend.rs          # 三库统一: 参数绑定、行解码、批量 INSERT
│   ├── params.rs           # Python 参数 → Rust PyParam 转换
│   ├── error.rs            # sqlx::Error → Python 异常映射
│   └── runtime.rs          # 全局 Tokio runtime + GIL 释放
└── tests/
    ├── test_sqlite.py      # SQLite 独立测试 (16 项)
    ├── verify_pg.py        # PG 交叉验证 vs psycopg2 (53 项)
    ├── verify_mysql.py     # MySQL 交叉验证 vs pymysql (50 项)
    └── bench_pymysql_vs_rsqlx.py  # 功能覆盖 + 性能基准
```

## 许可协议

rsqlx 采用 **双许可**，你可以任选其一使用：

- **MIT License** — 见 [`LICENSE-MIT`](LICENSE-MIT)
- **Apache License, Version 2.0** — 见 [`LICENSE-APACHE`](LICENSE-APACHE)

你可以根据 **任一份** 协议的条款来使用、复制、修改、合并、发布、分发、
再授权和/或销售本软件的副本。两份协议**不要求同时遵守**——选你觉得方便、
且符合你下游义务的那一份即可。

| | MIT | Apache-2.0 |
|---|---|---|
| 类型 | 宽松许可 | 宽松许可 |
| 专利授权 | 无 | 有（明确授予） |
| 修改声明 | 不要求 | 修改的文件需标注 |
| NOTICE 保留 | 不要求 | 存在 `NOTICE` 文件时需保留 |

只要最简单的条款选 **MIT**；想要明确的专利授权选 **Apache-2.0**。

Copyright (C) rsqlx Contributors。除非另有说明，所有贡献也以相同的双许可条款授权。
