# sqlalchemy-rsqlx

SQLAlchemy 2.0 async dialects for [rsqlx](https://github.com/likangcai/rsqlx) —
a Rust/Tokio async database driver exposed to Python through PyO3.

```bash
pip install sqlalchemy-rsqlx
```

`rsqlx` and `SQLAlchemy>=2.0` are installed automatically. The rsqlx core does
not depend on SQLAlchemy; this package is a separate distribution.

## Usage

```python
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

engine = create_async_engine("sqlite+rsqlx:///:memory:")
async with engine.begin() as conn:
    await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)"))
    await conn.execute(text("INSERT INTO t (name) VALUES (:n)"), {"n": "alice"})
async with engine.connect() as conn:
    print((await conn.execute(text("SELECT name FROM t"))).scalars().all())
await engine.dispose()
```

Supported URL forms:

```text
postgresql+rsqlx://user:pass@host:5432/db?sslmode=require
mysql+rsqlx://user:pass@host:3306/db?ssl-mode=disabled
sqlite+rsqlx:///relative.db
sqlite+rsqlx:////absolute/path.db
sqlite+rsqlx:///:memory:
```

Dialects are auto-discovered through the `sqlalchemy.dialects` entry point group;
no manual `registry.register()` is needed.

## Support matrix

| Feature | PostgreSQL | MySQL | SQLite |
|---|---|---|---|
| `create_async_engine` | yes | yes | yes |
| `text()` named params | `$1, $2` | `?` | `?` |
| Transactions (`begin`/`commit`/`rollback`) | yes | yes | yes |
| `executemany` | yes (loop fallback¹) | yes | yes |
| `lastrowid` | n/a | yes | yes |
| datetime / Decimal / UUID / JSON / bytes | yes | yes² | yes² |
| PG arrays | yes | n/a | n/a |
| Server-side cursors | no | no | no |

¹ rsqlx's multi-value INSERT rewrite recognises `?` only. SQLAlchemy emits `$n`
for PostgreSQL, so batch inserts fall back to a sequential loop at the driver.
Use SQLAlchemy's `insert().values([...])` for multi-row inserts. No SQL string
rewriting is performed in the adapter.

² SQLite has no native DECIMAL/UUID; rsqlx stores them as TEXT and returns
strings. This dialect does not auto-convert; use a SQLAlchemy `TypeDecorator`
if you need Decimal/UUID round-tripping on SQLite.

## Layering principle

- **rsqlx core** uses each database's native placeholders (`$n` / `?`) and
  returns `list[dict]` by default. It never rewrites SQL and has no framework
  dependency.
- **This dialect** declares `paramstyle` so SQLAlchemy converts `:name` into
  the native placeholder during compilation. The adapter passes the resulting
  SQL to rsqlx verbatim.
- Cross-framework portability is a concern of the adapter layer, not the core.

Detailed design: [`../docs/SQLALCHEMY_DIALECT_DESIGN.md`](../docs/SQLALCHEMY_DIALECT_DESIGN.md).

## Connection pool parameters

| SQLAlchemy option | Behaviour |
|---|---|
| `pool_size`, `max_overflow` | Control the SQLAlchemy pool of DBAPI connections; each owns one rsqlx pool with `max_connections=1` |
| `pool_timeout` | Mapped to rsqlx `acquire_timeout` (seconds) |
| `pool_recycle` | Mapped to rsqlx `max_lifetime` (seconds) |
| `pool_pre_ping` | Implemented by SQLAlchemy via `SELECT 1` |
| `connect_args` | Only `min_connections`, `max_connections`, `acquire_timeout`, `idle_timeout`, `max_lifetime` are accepted |
| `async_fallback=True` | Intended for sync contexts only; uses `await_fallback` on SQLAlchemy 2.0 and falls back to `await_only` on 2.1+ (where `await_fallback` was removed) |

Event-loop rule: all driver coroutines are awaited through SQLAlchemy's
`await_only` greenlet bridge. Calling `asyncio.run()` inside the adapter is
never done. Using this dialect outside a SQLAlchemy async engine context is
unsupported.

## Known limitations

- Empty SELECT results obtain column metadata via `rsqlx.describe()`; this adds
  one `prepare()` round trip only when a statement returns zero rows.
- `INTERVAL`, `INET`, ranges and custom PG types are not supported by the core
  and surface as `NotSupportedError`-equivalent DBAPI errors.
- Django integration is not included. A future adapter package can build on the
  same DBAPI-shaped boundary to target `django-async-backend` or `dj11.aorm`.
- SQLCipher-encrypted SQLite databases are not supported (the core links
  standard SQLite, not SQLCipher).

## Differences from `asyncpg` / `aiomysql`

| | sqlalchemy-rsqlx | asyncpg / aiomysql |
|---|---|---|
| Core | Rust + Tokio, single native extension, GIL released during I/O | Pure Python / C extension |
| Prepared statements | sqlx runtime queries; no `query!` macros | Driver-specific caching |
| System deps | rustls + static SQLite; none at runtime | asyncpg needs libpq-free pure Python; aiomysql pure Python |
| Row objects | `list[dict]` from core; converted by dialect | driver-native records |
| Placeholder control | dialect declares `paramstyle`; core untouched | driver-specific |

## Testing

```bash
pip install -e '.[test]'
pytest tests/
```

SQLite tests always run. PostgreSQL/MySQL live tests require
`RSQLX_TEST_PG_URL` / `RSQLX_TEST_MYSQL_URL` set to **SQLAlchemy URL format**
(with the `+rsqlx` driver suffix); otherwise they are skipped.

```powershell
# Windows PowerShell
$env:RSQLX_TEST_PG_URL    = "postgresql+rsqlx://user:pass@127.0.0.1:5432/db"
$env:RSQLX_TEST_MYSQL_URL = "mysql+rsqlx://user:pass@127.0.0.1:3306/db?ssl-mode=disabled"
python -m pytest tests/ -q
```

```bash
# Linux / macOS
export RSQLX_TEST_PG_URL="postgresql+rsqlx://user:pass@127.0.0.1:5432/db"
export RSQLX_TEST_MYSQL_URL="mysql+rsqlx://user:pass@127.0.0.1:3306/db?ssl-mode=disabled"
python -m pytest tests/ -q
```

Live tests cover: CRUD, transaction commit/rollback, batch insert,
datetime/Decimal/UUID/JSON/bytes round-trip, `lastrowid`, exception mapping
(`IntegrityError` / `ProgrammingError`), and `Row` attribute/index/mapping
access. DSN translation, dialect registration and placeholder compilation are
covered offline.

Note: rsqlx driver URLs (`postgres://`, `mysql://`) are NOT valid for
`RSQLX_TEST_*_URL` — SQLAlchemy would load the default sync driver instead of
the rsqlx dialect. Always include `+rsqlx`.
