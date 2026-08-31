# Changelog

本项目记录 rsqlx 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [0.9.0] - 2026-08-31

对齐上游 sqlx 版本号，便于长期维护与对照。

### 变更

- 依赖 `sqlx` 0.8.6 → 0.9.0；rsqlx 自身版本号同步对齐为 `0.9.0`（与上游 sqlx 主版本号保持一致，方便版本对照与问题排查）
- 适配 sqlx 0.9 的破坏性 API 变更：
  - `Database::Arguments` 关联类型移除生命周期参数（`Arguments<'q>` → `Arguments`）
  - `Arguments` 与 `IntoArguments` trait 移除生命周期参数
  - `query_builder::Separated` 减少一个生命周期参数
  - `query()` / `raw_sql()` 新增 `SqlSafeStr` 约束，运行时动态 SQL 改用 `sqlx::AssertSqlSafe` 包裹
- 功能不变：三库统一接口、参数/行类型映射、批量多值 INSERT、事务、迁移、原生查询等保持原有行为

### 构建与 CI

- 构建系统：CI 改用 manylinux / musllinux Docker 容器构建 Linux wheel，容器内自带 glibc/musl 运行时与交叉编译工具链（`aarch64-linux-gnu-gcc` 等），不再依赖 `zig`；glibc 目标同时打 `manylinux_2_28` 与 `manylinux_2_17` 双标签以兼容新旧发行版，musl 目标补全 Python 3.9–3.13 覆盖（兼容 Alpine / 多数 Docker 镜像）
- 测试：拆分 Linux（含 PostgreSQL / MySQL Docker 服务）与 Windows / macOS（仅 SQLite）测试 job；所有平台先在 venv 内安装依赖再运行，修复 `maturin develop` 与 `pytest` 找不到解释器的问题

## [0.1.1] - 2026-08-31

许可布局对齐上游 sqlx。

### 变更

- 删除单独的 `LICENSE` 索引文件，仅保留 `LICENSE-MIT` + `LICENSE-APACHE` 两份（与 sqlx 仓库同构）
- 版权署名改为 `Copyright (C) rsqlx Contributors`，并注明 `Portions of this work Copyright (C) SQLx Contributors`
- 7 个 `src/*.rs` 源码 SPDX 头署名同步更新
- `pyproject.toml` 的 `license-files` 移除已删除的 `LICENSE`

## [0.1.0] - 2026-08

首个可用版本。

### 许可协议

- 双许可: 用户可任选 **MIT** 或 **Apache-2.0**（全文见 `LICENSE-MIT` / `LICENSE-APACHE`）
- 许可证文件布局与上游 sqlx 一致：仅 `LICENSE-MIT` + `LICENSE-APACHE` 两份，无单独 `LICENSE` 索引文件
- 版权署名 `Copyright (C) rsqlx Contributors`，并注明 `Portions of this work Copyright (C) SQLx Contributors`（rsqlx 基于 sqlx）
- 源码文件头部带 SPDX 标识 `SPDX-License-Identifier: MIT OR Apache-2.0`
- `pyproject.toml` 声明 `license = "MIT OR Apache-2.0"` 并打包两份 LICENSE 文件进 wheel

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
