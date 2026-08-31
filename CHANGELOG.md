# Changelog

本项目记录 rsqlx 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [0.1.0] - 2026-08

首个可用版本。

### 核心实现

- 基于 sqlx 0.8.6 + pyo3 0.24.2 + Tokio runtime 的异步数据库驱动
- 三库统一接口: PostgreSQL、MySQL、SQLite
- 全局多线程 Tokio runtime，查询等待期间通过 `allow_threads` 释放 GIL
- 参数类型系统: None/bool/int/float/str/bytes/datetime/date/time/Decimal/UUID/JSON/PG数组
- 行解码按 `type_info().name()` 分发，覆盖三大数据库的全部常用类型

### API

- `connect(url, *, min_connections, max_connections, acquire_timeout, idle_timeout, max_lifetime)`
- Pool: `fetch` / `fetch_one` / `fetch_optional` / `execute` / `execute_many` / `begin` / `migrate` / `close`
- Transaction: 同 Pool 的查询方法 + `commit` / `rollback`，支持 `async with`
- `execute_raw` / `fetch_raw`: 走 COM_QUERY 协议，支持存储过程等预编译不支持的语句
- `execute_many`: INSERT 语句自动改写为多值 INSERT，比逐条快 250 倍

### 类型映射

- PG: BOOL/INT2-8/FLOAT4-8/NUMERIC/TEXT/VARCHAR/CHAR/NAME/BYTEA/JSON/JSONB/UUID/DATE/TIME/TIMESTAMP/TIMESTAMPTZ + 数组
- MySQL: BOOLEAN/TINYINT-BIGINT(含 UNSIGNED)/YEAR/FLOAT/DOUBLE/DECIMAL/CHAR/VARCHAR/TEXT/BLOB/JSON/DATE/TIME/DATETIME/TIMESTAMP
- SQLite: 按 runtime value type 解码 (NULL/INTEGER/REAL/TEXT/BLOB)，declared type 用于 datetime 嗅探
- NULL 参数用 PG unknown OID (705) 绑定，让服务端推断目标类型

### 异常体系

- Error (基类) → InterfaceError / DatabaseError / OperationalError / RowNotFound / PoolTimedOut / PoolClosed / MigrateError

### 跨平台

- TLS: rustls + ring，零系统 OpenSSL 依赖
- SQLite: libsqlite3-sys 静态链接
- CI: Windows / macOS / Linux × Python 3.9-3.13 全矩阵构建和测试

### 验证

- SQLite: 16 项测试 (pytest)
- PostgreSQL 18: 53 项交叉验证 vs psycopg2
- MySQL 8: 50 项交叉验证 vs pymysql
- 功能覆盖: pymysql 28 项核心功能全部覆盖
- 性能: batch INSERT 比 pymysql 快 1.6x，范围查询快 1.5x
