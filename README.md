# rsqlx

**English** | [中文](README_CN.md)

Async PostgreSQL / MySQL / SQLite driver for Python, powered by Rust's
[sqlx](https://github.com/launchbadge/sqlx) and exposed as a native extension
via PyO3 — the same approach as `orjson`: Rust core, Python surface.

Every query runs on a shared multi-threaded Tokio runtime; the GIL is released
while waiting for the database, so concurrent tasks and threads run in parallel.

> Author: Yingzi <yingzilkq@163.com>  
> Source: https://github.com/likangcai/rsqlx  
> Mirror: https://gitee.com/yingzi_shadow/rsqlx  
> Built on: [sqlx](https://github.com/launchbadge/sqlx) by the SQLx contributors — rsqlx is a Python async application of sqlx, not a fork of its source code  
> License: MIT OR Apache-2.0

## Features

- One interface for PostgreSQL, MySQL and SQLite
- Connection pooling (min/max connections, acquire timeout, idle timeout, max lifetime)
- Transactions with `async with` (auto-commit on success, auto-rollback on exception)
- `execute_many` rewrites single-row INSERTs into a multi-row INSERT — two orders of magnitude faster
- `execute_raw` / `fetch_raw` use the COM_QUERY protocol for stored procedures and DDL that the prepared-statement protocol doesn't support
- Migrations (sqlx-compatible `<N>_<name>.up.sql` files)
- Full type mapping: datetime, Decimal, UUID, JSON, bytes, PG arrays
- Exception hierarchy rooted at `rsqlx.Error`

## Installation

Pre-built wheels:

| Platform | Architectures |
|----------|---------------|
| Linux (glibc ≥ 2.28，或 musl ≥ 1.2 / Alpine) | x86_64, aarch64 |
| Windows 10+ | x86_64, ARM64 |
| macOS 11+ | x86_64, arm64 |

Each (platform, arch) wheel works for CPython 3.9–3.13.

TLS: rustls + ring (pure Rust, no system OpenSSL). SQLite: statically linked via libsqlite3-sys. Zero system dependencies on the user side.

```bash
pip install rsqlx
```

Build from source (requires Rust and [maturin](https://github.com/PyO3/maturin)):

```bash
pip install maturin
maturin build --release -o dist
pip install dist/rsqlx-*.whl

# For local development:
maturin develop --release
```

## Quickstart

All three databases share the same `connect()` / `Pool` / `Transaction` API.
Connection strings follow the sqlx convention:

```python
# SQLite:     "sqlite:app.db" (file) or "sqlite::memory:" (in-memory) — placeholders: ?
# PostgreSQL: "postgres://user:pass@localhost:5432/db"               — placeholders: $1, $2, ...
# MySQL:      "mysql://user:pass@localhost:3306/db?ssl-mode=disabled" — placeholders: ?
#             (add ?ssl-mode=disabled when the server's TLS is incompatible with rustls)
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

    # execute returns ExecuteResult(rows_affected, last_insert_id)
    res = await pool.execute("INSERT INTO users (name, age) VALUES (?, ?)", ["Alice", 30])
    print(res.rows_affected, res.last_insert_id)            # 1 1

    # fetch returns list[dict]
    rows = await pool.fetch("SELECT * FROM users WHERE age > ?", [18])
    print(rows)                                            # [{'id': 1, 'name': 'Alice', 'age': 30}]

    # single-row helpers
    user  = await pool.fetch_one("SELECT * FROM users WHERE id = ?", [1])        # raises RowNotFound if no row
    maybe = await pool.fetch_optional("SELECT * FROM users WHERE id = ?", [99])  # None

    # transaction: commit on normal exit, rollback on exception
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

    # homogeneous scalar lists bind as native PG arrays (None elements preserved)
    await pool.execute(
        "INSERT INTO users (name, age, tags) VALUES ($1, $2, $3)",
        ["Bob", 25, ["python", "rust", None]],
    )

    user = await pool.fetch_one("SELECT * FROM users WHERE id = $1", [1])
    print(user["tags"])                                    # ['python', 'rust', None]

    # batch — single-row INSERT is auto-rewritten to a multi-row INSERT
    await pool.execute_many(
        "INSERT INTO users (name, age) VALUES ($1, $2)",
        [["Carol", 28], ["Dave", 40]],
    )

    # JSONB[] arrays: opt in explicitly (a plain list[dict] binds as one jsonb value)
    await pool.execute("CREATE TEMP TABLE _json_demo (docs JSONB[])")
    await pool.execute(
        "INSERT INTO _json_demo VALUES ($1)",
        [rsqlx.JsonArray([{"k": 1}, {"k": 2}, None])],
    )

    await pool.close()

asyncio.run(main())
```

### MySQL

```python
import asyncio
import rsqlx

async def main():
    # add ?ssl-mode=disabled when the server's TLS is incompatible with rustls
    # (common with MySQL 8 default cipher configuration)
    pool = await rsqlx.connect(
        "mysql://user:pass@localhost:3306/db?ssl-mode=disabled", max_connections=5
    )

    await pool.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), age INT)"
    )

    await pool.execute("INSERT INTO users (name, age) VALUES (?, ?)", ["Alice", 30])
    rows = await pool.fetch("SELECT * FROM users WHERE age > ?", [18])

    # stored procedures, multi-statement scripts and other non-prepared
    # statements use the raw protocol (execute_raw / fetch_raw)
    await pool.execute_raw("DROP PROCEDURE IF EXISTS sp_hello")
    await pool.execute_raw(
        "CREATE PROCEDURE sp_hello(IN n VARCHAR(64)) BEGIN SELECT n; END"
    )
    await pool.execute("CALL sp_hello(?)", ["world"])

    await pool.close()

asyncio.run(main())
```

### Migrations

```python
# Run sqlx-style migration files from a directory (applied once, then idempotent):
#   migrations/0001_init.up.sql
#   migrations/0002_seed.up.sql
await pool.migrate("migrations")
```

### Placeholders

Use the database's native style: `$1, $2, ...` for PostgreSQL, `?` for MySQL and SQLite. Multi-row INSERT rewrites `?` to the correct format automatically.

## API Reference

### `rsqlx.connect(url, *, min_connections=None, max_connections=None, acquire_timeout=None, idle_timeout=None, max_lifetime=None) -> Pool`

Timeout arguments are in seconds (`float`).

### Pool

| Method | Description |
|--------|-------------|
| `await fetch(sql, params=None, row_factory=None) -> list` | All rows; `row_factory` selects row type |
| `await fetch_one(sql, params=None, row_factory=None)` | One row; raises `RowNotFound` if none |
| `await fetch_optional(sql, params=None, row_factory=None)` | One row or None |
| `await execute(sql, params=None) -> ExecuteResult` | Returns rows_affected and last_insert_id |
| `await execute_many(sql, params) -> ExecuteResult` | Batch; INSERT auto-optimized to multi-row |
| `await execute_raw(sql)` | Raw query protocol (no parameter binding) — for stored procedures, DDL |
| `await fetch_raw(sql, row_factory=None)` | Raw query protocol with row results |
| `await describe(sql) -> list[str]` | Column names of a statement, without fetching rows |
| `await begin() -> Transaction` | Start a transaction |
| `await migrate(path)` | Run SQL migration files from a directory |
| `await close()` | Close the pool |
| `size` / `num_idle` / `is_closed` | Pool introspection |

Pool and Transaction support `async with`.

### Row factory

`fetch` / `fetch_one` / `fetch_optional` / `fetch_raw` (on both `Pool` and
`Transaction`) accept a keyword-only `row_factory`:

- `None` or `"dict"` (default): `dict` rows — the original `list[dict]` behaviour, unchanged
- `"tuple"`: positional tuples, marginally faster because no key strings are built
- `"namedtuple"`: `collections.namedtuple` rows with attributes named after columns

Invalid values raise `ValueError`. The default path is unchanged and
backward-compatible; the row factory is chosen once per call, not per row.

### Parameter Types

None, bool, int, float, str, bytes/bytearray, datetime (naive or aware), date, time, Decimal, UUID, dict/list/tuple (as JSON). Integers exceeding 64 bits degrade to Decimal. Homogeneous scalar lists/tuples bind as native PG arrays; lists containing dicts or nesting bind as a single JSON value. To bind a PostgreSQL `JSONB[]` array explicitly, use `rsqlx.JsonArray([...])` (on MySQL/SQLite it degrades to a plain JSON array value).

### Binding JSON arrays with `rsqlx.JsonArray`

Python has no static type to distinguish a *single JSON array value* from an
*array of JSON values*, so rsqlx keeps the backward-compatible default and
offers an explicit wrapper:

| Python value | Binds as | Target column |
|---|---|---|
| `{"k": 1}` | one jsonb object | `JSONB` |
| `[{"k": 1}, {"k": 2}]` | **one jsonb array value** (default, unchanged) | `JSONB` |
| `[1, 2, 3]` | native `INT4[]` (homogeneous scalars) | `INT4[]` / `NUMERIC[]` |
| `rsqlx.JsonArray([{"k": 1}, {"k": 2}, None])` | native `JSONB[]` | `JSONB[]` |

```python
import rsqlx

# PostgreSQL JSONB[] column
await pool.execute("CREATE TEMP TABLE docs (items JSONB[])")
await pool.execute(
    "INSERT INTO docs VALUES ($1)",
    [rsqlx.JsonArray([{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None])],
)
rows = await pool.fetch("SELECT items FROM docs")
# rows == [{"items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2}, None]}]
```

Notes:
- `JsonArray` accepts any JSON-serialisable items (dicts, lists, scalars,
  `None`); nested structures are preserved.
- On **MySQL / SQLite** there is no native JSON-array type, so `JsonArray`
  degrades to a plain JSON array value — identical to passing a plain
  `list[dict]`.
- Reading `JSONB[]` / `JSON[]` columns works without the wrapper: rsqlx decodes
  them into Python `list` (with `None` preserved).

This mirrors upstream sqlx's `Vec<Json<T>>` binding (`PgHasArrayType for
Json<T>`) while keeping rsqlx's existing `list[dict]` behaviour intact.

### Row Type Mapping

| Database type | Python type |
|---------------|-------------|
| BOOL / BOOLEAN | `bool` |
| INT2/INT4/INT8, TINYINT..BIGINT (+UNSIGNED), INTEGER | `int` |
| FLOAT4/FLOAT8, FLOAT/DOUBLE, REAL | `float` |
| NUMERIC / DECIMAL | `decimal.Decimal` (TEXT on SQLite) |
| TEXT, VARCHAR, CHAR, NAME, ENUM, SET | `str` |
| BYTEA, BINARY/BLOB variants | `bytes` |
| JSON / JSONB | auto-decoded (`dict` / `list` / scalars) |
| UUID (PG) | `uuid.UUID` |
| DATE | `datetime.date` |
| TIME | `datetime.time` |
| TIMESTAMP / DATETIME | naive `datetime.datetime` |
| TIMESTAMPTZ | aware `datetime.datetime` |
| PG OID | `int` |
| PG CITEXT | `str` |
| PG INET / CIDR / MACADDR / BIT / VARBIT / TIMETZ | `str` |
| PG INTERVAL | `dict{months, days, microseconds}` |
| PG MONEY | `decimal.Decimal` |
| PG arrays of the above | `list` (None elements preserved) |

Unsupported types (e.g. PG ranges, geometric types, custom domain/composite types,
MySQL GEOMETRY) raise `InterfaceError` — cast in SQL (`SELECT col::text`) to
retrieve as string. See [TYPE_SUPPORT_MATRIX.md](docs/TYPE_SUPPORT_MATRIX.md) for
the full type matrix and compatibility notes.

### Exceptions

```
rsqlx.Error
├── InterfaceError        # unsupported types, decode failures, API misuse
├── DatabaseError         # server errors (syntax, constraint violations)
├── OperationalError      # IO failures, worker crashes, background task errors
├── RowNotFound           # fetch_one returned no rows
├── PoolTimedOut          # timed out acquiring a connection
├── PoolClosed            # operation on a closed pool
└── MigrateError          # migration failure
```

## Limitations

- Requires an asyncio event loop (`asyncio.run` / `async def`)
- sqlx's compile-time `query!` macros are a Rust-only feature; rsqlx provides runtime-checked queries
- Canceling a coroutine cancels the await, but the in-flight query runs to completion on the runtime
- MySQL servers using DHE cipher suites (e.g. MySQL 8.0.16) are incompatible with rustls; add `?ssl-mode=disabled` to the connection URL
- **SQLCipher is not supported**: rsqlx statically links standard SQLite (not SQLCipher), so encrypted SQLite databases cannot be opened. Upstream sqlx has a `bundled-sqlcipher` feature, but enabling it would replace the bundled SQLite engine and require building vendored OpenSSL, which conflicts with rsqlx's zero-system-dependency guarantee and cross-platform CI. If you need SQLCipher, use an external encrypted database or a dedicated SQLCipher client
- **MySQL parameter-count mismatch**: passing the wrong number of parameters raises `rsqlx.InterfaceError`, but (due to sqlx's MySQL prepare-stage behavior) leaves one pooled connection busy. Calling `close()` on such a pool can hang. PostgreSQL and SQLite are unaffected. Always bind the correct number of parameters; this is treated as a user error in the SQL, not a driver defect

## Migrating from pymysql

| pymysql | rsqlx |
|---------|-------|
| `pymysql.connect(host=..., user=..., password=...)` | `await rsqlx.connect("mysql://user:pass@host/db")` |
| `cursor.execute(sql, (a, b))` | `await pool.execute(sql, [a, b])` |
| `cursor.fetchone()` | `await pool.fetch_one(sql, [a, b])` |
| `cursor.fetchall()` | `await pool.fetch(sql, [a, b])` |
| `cursor.executemany(sql, args)` | `await pool.execute_many(sql, args)` |
| `conn.begin() / commit() / rollback()` | `async with await pool.begin() as tx: ...` |
| `conn.insert_id()` | `result.last_insert_id` |
| `conn.affected_rows` | `result.rows_affected` |
| `cursor.callproc(name, args)` | `await pool.execute("CALL name(?)", args)` (DDL via `execute_raw`) |
| `paramstyle = 'pyformat'` (%s) | `?` (qmark) |
| `DictCursor` | dicts by default |
| `pymysql.IntegrityError` | `rsqlx.DatabaseError` |

Main change: sync → async (`def` → `async def`, add `await`), `%s` → `?`.

## Tracking sqlx Upstream Updates

rsqlx depends on sqlx. When a new version is released, here's how to sync.

### 1. Check for a new sqlx version

```bash
cargo search sqlx
# Or check release notes: https://github.com/launchbadge/sqlx/releases
```

### 2. Update the version in Cargo.toml

```toml
[dependencies]
sqlx = { version = "0.9", default-features = false, features = [
    "runtime-tokio", "tls-rustls-ring", "postgres", "mysql", "sqlite",
    "chrono", "uuid", "rust_decimal", "migrate", "json",
] }
```

### 3. Check MSRV

```bash
rustup update stable
# Or install a specific version:
rustup install 1.94.0
rustup override set 1.94.0
```

### 4. Check feature name changes

Compare the new sqlx `Cargo.toml` ([crates.io](https://crates.io/crates/sqlx) or [GitHub](https://github.com/launchbadge/sqlx/blob/main/sqlx/Cargo.toml)). If a feature is renamed, `cargo check` will report `unknown feature` — fix per the error.

### 5. Compile

```bash
cargo check
```

Common breaking changes:
- **API signature changes**: trait method refactors — adjust trait bounds and type references in `src/backend.rs`.
- **TypeInfo / Column API changes**: affects row decoding (`col.type_info().name()`).
- **Query generic parameter changes**: sqlx 0.7→0.8 added an `Arguments` generic to `Query<'q, DB, A>`.
- **New database types**: add decode branches in `backend.rs` for new type names.

### 6. Run tests

```bash
python -m pytest tests/test_sqlite.py -v

$env:RSQLX_TEST_PG_URL = "postgres://postgres:pass@127.0.0.1:5432/postgres"
python tests/verify_pg.py

$env:RSQLX_TEST_MYSQL_URL = "mysql://root:pass@127.0.0.1:3306/testdb?ssl-mode=disabled"
python tests/verify_mysql.py
```

### 7. Do I need to download sqlx source?

**Usually no.** `cargo build` fetches and compiles from crates.io automatically — just change the version number.

When you do need the source (debugging type mappings, understanding API changes):
- It's in the cargo registry cache: `~/.cargo/registry/src/index.crates.io-*/sqlx-<version>/`
- Or clone from GitHub: `git clone --branch v0.9.0 https://github.com/launchbadge/sqlx.git`

### 8. Bump rsqlx version

- sqlx patch (0.8.5 → 0.8.6): rsqlx patch (0.1.0 → 0.1.1)
- sqlx minor (0.8 → 0.9): rsqlx minor (0.1 → 0.2)

Only the major version needs to stay in sync with sqlx (currently `0.9` for both). Patch versions may differ — e.g. rsqlx `0.9.3` built on sqlx `0.9.0` is expected and does not need alignment. Keep `Cargo.toml`, `Cargo.lock`, `uv.lock` and `pyproject.toml` at the same rsqlx version.

Update `version` in `Cargo.toml` and `pyproject.toml`, then:

```bash
git tag v0.2.0
git push origin v0.2.0
# GitHub Actions builds and publishes wheels automatically
```

### Sync Checklist

- [ ] sqlx version updated in `Cargo.toml`
- [ ] Rust ≥ sqlx new MSRV
- [ ] `cargo check` passes (feature names, API signatures)
- [ ] `tests/test_sqlite.py` passes
- [ ] `tests/verify_pg.py` passes
- [ ] `tests/verify_mysql.py` passes
- [ ] Type mapping tables (`pg_value_to_py` / `mysql_value_to_py` / `sqlite_value_to_py` in `backend.rs`) match new sqlx type names
- [ ] rsqlx version bumped in `Cargo.toml` and `pyproject.toml`
- [ ] CI Rust version ≥ new MSRV (update `dtolnay/rust-toolchain` if needed)

## Documentation

| Document | Contents |
|----------|----------|
| [README.md](README.md) | English: features, install, API reference, migration from pymysql |
| [README_CN.md](README_CN.md) | 中文说明 |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Implementation internals, solved problems, packaging with maturin, PyPI publishing, installation |
| [TYPE_SUPPORT_MATRIX.md](docs/TYPE_SUPPORT_MATRIX.md) | rsqlx vs upstream sqlx type support by database |
| [SQLALCHEMY_DIALECT_DESIGN.md](docs/SQLALCHEMY_DIALECT_DESIGN.md) | SQLAlchemy 2.0 async dialect design |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

For implementation details or to publish a new release, see [DEVELOPMENT.md](DEVELOPMENT.md).

## Project Structure

```
rsqlx/
├── Cargo.toml              # Rust dependencies and build config
├── pyproject.toml          # Python package metadata + maturin backend
├── Dockerfile              # Linux build-from-scratch verification
├── README.md               # English (this file)
├── README_CN.md            # 中文说明
├── DEVELOPMENT.md          # Implementation + packaging/publishing guide
├── CHANGELOG.md            # Version history
├── .github/workflows/
│   ├── build.yml           # Three-platform wheel builds + PyPI publish
│   └── tests.yml           # Three-platform functional tests (SQLite + PG + MySQL)
├── src/
│   ├── lib.rs              # Module registration, exceptions, connect()
│   ├── pool.rs             # Pool: CRUD, transactions, migrate, execute_raw
│   ├── transaction.rs      # Transaction: in-tx operations
│   ├── backend.rs          # Unified: param binding, row decoding, batch INSERT
│   ├── params.rs           # Python → Rust PyParam conversion
│   ├── error.rs            # sqlx::Error → Python exception mapping
│   └── runtime.rs          # Global Tokio runtime + GIL release
└── tests/
    ├── test_sqlite.py      # SQLite standalone tests (16)
    ├── verify_pg.py        # PG cross-validation vs psycopg2 (53)
    ├── verify_mysql.py     # MySQL cross-validation vs pymysql (50)
    └── bench_pymysql_vs_rsqlx.py  # Feature coverage + performance benchmark
```

## License

rsqlx is **dual-licensed** under your choice of either:

- **MIT License** — see [`LICENSE-MIT`](LICENSE-MIT)
- **Apache License, Version 2.0** — see [`LICENSE-APACHE`](LICENSE-APACHE)

You may use, copy, modify, merge, publish, distribute, sublicense and/or sell
copies of this software under the terms of **either** license. You are not
required to comply with both — pick whichever fits your situation and any
downstream obligations.

| | MIT | Apache-2.0 |
|---|---|---|
| Type | Permissive | Permissive |
| Patent grant | No | Yes (explicit) |
| Change-notice | Not required | Required for modified files |
| NOTICE preservation | Not required | Required if a `NOTICE` file exists |

Pick **MIT** for the simplest terms; pick **Apache-2.0** if you want the
explicit patent grant.

Copyright (C) rsqlx Contributors. Unless stated otherwise, contributions are
dual-licensed under the same terms.

## Acknowledgements

rsqlx is built on top of **[sqlx](https://github.com/launchbadge/sqlx)** by the
SQLx contributors, and is dual-licensed under the same MIT OR Apache-2.0 terms.
Portions of this work are Copyright (C) SQLx Contributors (as noted in
`LICENSE-MIT` and `LICENSE-APACHE`). rsqlx is an independent Python async
application of sqlx — it is **not** a fork of the sqlx source tree and is not
affiliated with or endorsed by the SQLx project. Thanks to the SQLx team for
the excellent Rust SQL toolkit that makes this project possible.
