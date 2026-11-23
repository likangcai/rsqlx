# Changelog

本项目记录 rsqlx 的版本变更。格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [0.9.4] - 2026-11-23

### 新增

- `rsqlx.Range(start, end, include_start=True, include_end=False)`：显式 PostgreSQL 区间类型，绑定为 `PgRange<T>`（按元素推断 INT4RANGE/INT8RANGE/NUMRANGE/DATERANGE/TSRANGE/TSTZRANGE），range 列自动解码回 `rsqlx.Range`（含边界标记与无界信息）；MySQL/SQLite 无原生区间类型
- **补齐协议级高级功能**：`begin_with()` 显式事务隔离级别；PG `copy_in()`/`copy_out()` COPY 协议；PG `listen()` LISTEN/NOTIFY 监听器（`recv()` 返回 `(channel, payload, process_id)`）；PG `cursor()` 服务端游标（分批 `fetch_batch()`）；`ping()` 连接存活检测（sqlx 默认自动替换死连接）
- **启用 sqlx 可选类型 feature 并补齐 PG 类型**：`bigdecimal` / `ipnet` / `mac_address` / `bit-vec` / `time`。新增解码：OID→`int`、CITEXT→`str`、INET/CIDR→`str`、MACADDR→`str`、BIT/VARBIT→位串 `str`、TIMETZ→带时区 `str`、INTERVAL→`dict{months,days,microseconds}`（无损）、MONEY→`Decimal`。全部为纯 Rust 依赖，不引入系统依赖
- NUMERIC 解码改为 rust_decimal 优先、超范围回退 BigDecimal → Python `Decimal`，支持任意精度（原仅 ~96 位）

- `fetch` / `fetch_one` / `fetch_optional` / `fetch_raw` 增加可选 `row_factory` 参数，支持 `"dict"`（默认）、`"tuple"`、`"namedtuple"`；默认行为与 `list[dict]` 输出完全不变。为 SQLAlchemy 方言层提供 tuple 输出以避免 dict→tuple 转换开销
- `Pool.describe(sql)` / `Transaction.describe(sql)`：返回语句列名但不拉取数据行，供框架适配层构造 DBAPI `description`；基于 sqlx `prepare()`，不启用额外 feature，也不重写 SQL
- `rsqlx.JsonArray([...])`：显式将一组 JSON 值绑定为 PostgreSQL `JSONB[]`（mysql/sqlite 退化为普通 JSON 数组值）。普通 `list[dict]` 仍绑定为单个 jsonb 值，向后兼容；与上游 sqlx 的 `Vec<Json<T>>` 能力对齐
- 新增独立的 `sqlalchemy-rsqlx` 方言包子项目（SQLAlchemy 2.0 async，支持 PG / MySQL / SQLite）

### 修复与验证

- MySQL 方言补齐 `_detect_charset()` 与 `_extract_error_code()`，兼容 SQLAlchemy 2.0 方言初始化；错误码提取对 SQLSTATE 字符串（如 `42S02`）容错
- PG / MySQL 实库集成测试全部跑通（0 跳过）：覆盖 CRUD、事务提交/回滚、批量插入、datetime / Decimal / UUID / JSON / bytes 往返、`lastrowid`、异常映射与 `Row` 多方式访问
- 核心测试支持 `RSQLX_TEST_*_URL` 同时接受原生 DSN 与 SQLAlchemy `+rsqlx` URL 格式，消除两套测试的环境变量冲突
- CI 新增 `live-db` job，使用 PostgreSQL 18 / MySQL 8 service containers 跑实库集成测试
- 补充 `Pool.describe()` / `Transaction.describe()` 的三库行为测试（列名、别名、事务内描述），并将 `test_api_friendly.py` 纳入 `tests.yml` 的 Linux 与 Windows/macOS 两个 job
- 新增 `tests/test_edge_cases.py` 深度边界测试（8 项）：PG 扩展数组（UUID/DATE/TIME/TIMESTAMP/TIMESTAMPTZ/NUMERIC/BYTEA/JSONB）、不支持类型报错、约束错误细分、迁移幂等；MySQL BIT/ENUM/SET/零日期/GEOMETRY、约束错误；连接池极限参数与超时触发
- **修复 MySQL `BIT` 列解码错误**：此前 `BIT` 被误当作 bytes（`Vec<u8>`）解码，实际应按无符号整数（`u64`）读取，现返回 `int`
- **修复 MySQL `GEOMETRY` 列的误导性解码错误**：sqlx 无对应 Rust 解码器，现明确抛出 `unsupported MySQL type 'GEOMETRY'` 的 `InterfaceError`（列为已知限制，见 README）
- 记录 MySQL 参数个数不匹配会导致池连接占用、`close()` 可能挂起的已知限制（PG/SQLite 不受影响）
- 新增 `docs/TYPE_SUPPORT_MATRIX.md`：三库类型支持与上游 sqlx 0.9.0 的完整对照表（含 `JsonArray` 用法示例）、差异根因（feature 未启用 / Python 类型系统边界 / 运行时信息不可逆 / 不适用）与补齐优先级建议，并在 README 文档索引中链接
- 新增 `docs/DRIVER_FEATURE_GAP.md`：与 pymysql / psycopg2 的功能差异对照（范式差异 / 真实缺失 / rsqlx 独有优势）与补齐优先级，并在 README 文档索引中链接

### 文档与元数据

- 补充 GitHub 仓库地址 `https://github.com/likangcai/rsqlx`，Gitee 保留为镜像（README 中英版、`Cargo.toml`、`DEVELOPMENT.md`）
- `pyproject.toml` 新增 `[project.urls]`，PyPI 项目页展示 Homepage / Repository / Gitee 镜像链接
- 统一 `Cargo.toml` / `Cargo.lock` / `uv.lock` / `pyproject.toml` 版本号为 `0.9.4`，以便发布新版本让 PyPI 页面显示更新后的仓库链接。版本策略：主版本号与上游 sqlx 保持一致（同为 `0.9`），补丁版本允许有差异，不要求与 sqlx 严格对齐
- 最低 Python 版本从 3.9 提升到 3.10：CI 矩阵去掉 3.9（Ubuntu 26.04 的 `actions/setup-python` 无 3.9 预构建），`requires-python` 同步改为 `>=3.10`，README / DEVELOPMENT 文档一致更新
- 方言包依赖改为 `SQLAlchemy[asyncio]>=2.0.0,<3`，确保 greenlet 自动安装；兼容 SQLAlchemy 2.1 移除 `await_fallback` 的变更（回退到 `await_only`），已在 2.0.44 与 2.1.9 双版本跑通全部方言测试
- CI 的 macOS runner 从已退役的 `macos-14` / `macos-13` 升级到 `macos-15` / `macos-15-intel`（GitHub 于 2026-11-02 退役 macOS 14 镜像），`build.yml` / `tests.yml` 与文档同步更新

### 下版本计划（0.9.5）

- 恢复 macOS x86_64 Intel wheel 构建：`build.yml` 里 `macos-15-intel` + `x86_64-apple-darwin` 的 5 个 Python 版本条目目前在注释中（自 `45eae600` 起"临时禁用"，原 `macos-13` 已退役）。`macos-15-intel` 是当前 GitHub 官方支持的 Intel runner，构建脚本无需特殊处理；下版本取消注释并跑通完整构建矩阵后启用

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

- 构建系统：CI 改用 manylinux / musllinux Docker 容器构建 Linux wheel，不再依赖 `zig`。x86_64 在 `manylinux_2_28_x86_64` / `musllinux_1_2_x86_64` 容器内构建；**aarch64 改用原生 `ubuntu-24.04-arm` runner + `manylinux_2_28_aarch64` / `musllinux_1_2_aarch64` 容器**（避免从 x86_64 交叉编译时 maturin 误用 x86_64 Python 解释器导致 wheel 损坏）。glibc 目标打 `manylinux_2_28` 标签（CentOS 7 等 2.17 老系统已 EOL，未单独构建 2_17 wheel），musl 目标 Python 3.9–3.13 全覆盖（兼容 Alpine / 多数 Docker 镜像）
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
