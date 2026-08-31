"""
rsqlx API 友好性 + 功能完整性验证

从 Python 用户的视角检查:
1. API 命名是否符合 Python 惯例 (PEP 8)
2. 类型提示、docstring、repr 是否齐全
3. 异常语义是否合理
4. 三库的核心操作是否都能正常工作
5. 边界情况处理
"""

import asyncio
import datetime as dt
import decimal
import inspect
import json
import os
import sys
import traceback
import uuid

import rsqlx

# ---- helpers ----
PASSED = 0
FAILED = 0
WARNINGS = []


def ok(name, detail=""):
    global PASSED
    PASSED += 1
    print(f"  [PASS] {name}")


def fail(name, detail=""):
    global FAILED
    FAILED += 1
    print(f"  [FAIL] {name}  {detail}")


def warn(name, detail=""):
    WARNINGS.append((name, detail))
    print(f"  [WARN] {name}  {detail}")


def run(coro):
    return asyncio.run(coro)


# ============================================================================
# 1. API 表面检查: 命名、类型、docstring
# ============================================================================
def test_api_surface():
    print("\n=== 1. API Surface ===")

    # 模块级
    assert hasattr(rsqlx, "connect"), "missing connect()"
    assert hasattr(rsqlx, "Pool"), "missing Pool class"
    assert hasattr(rsqlx, "Transaction"), "missing Transaction class"
    assert hasattr(rsqlx, "ExecuteResult"), "missing ExecuteResult class"
    ok("module: connect, Pool, Transaction, ExecuteResult exist")

    # 异常层次
    assert issubclass(rsqlx.Error, Exception)
    for name in ["InterfaceError", "DatabaseError", "OperationalError",
                 "RowNotFound", "PoolTimedOut", "PoolClosed", "MigrateError"]:
        cls = getattr(rsqlx, name, None)
        assert cls is not None, f"missing exception {name}"
        assert issubclass(cls, rsqlx.Error), f"{name} not subclass of Error"
    ok("exception hierarchy: 7 subclasses of Error")

    # PEP 8 命名: 检查没有 camelCase 方法
    for method_name in dir(rsqlx.Pool):
        if method_name.startswith("_") and not method_name.startswith("__"):
            fail(f"Pool.{method_name} is private (underscore prefix)")
    ok("PEP 8: no public methods with underscore prefix on Pool")

    # __version__
    assert isinstance(rsqlx.__version__, str)
    ok(f"__version__ = {rsqlx.__version__}")

    # connect returns a coroutine (pyo3 async fn — not a Python-level coroutine function,
    # but calling it returns an awaitable)
    import asyncio
    coro = rsqlx.connect("sqlite::memory:")
    assert asyncio.iscoroutine(coro), "connect() does not return a coroutine"
    coro.close()  # don't actually run it
    ok("connect() returns a coroutine")

    # Pool methods return coroutines (pyo3 async fn wrappers)
    async_method_names = ["fetch", "fetch_one", "fetch_optional", "execute",
                          "execute_many", "begin", "migrate", "close",
                          "execute_raw", "fetch_raw", "__aenter__", "__aexit__"]
    for name in async_method_names:
        method = getattr(rsqlx.Pool, name, None)
        if method is None:
            fail(f"Pool.{name} missing")
    ok("Pool: all 12 async methods present")

    # sync getters — pyo3 的 #[getter] 生成的是 C 层 getset_descriptor，
    # 不是 Python 的 property 对象，所以用 hasattr(描述符协议) 检查
    sync_props = ["size", "num_idle", "is_closed"]
    for name in sync_props:
        prop = getattr(rsqlx.Pool, name, None)
        if prop is None:
            fail(f"Pool.{name} missing")
        elif not (hasattr(prop, "__get__") or isinstance(prop, property)):
            fail(f"Pool.{name} is not a descriptor")
    ok("Pool: size/num_idle/is_closed are sync getters (getset_descriptor)")

    # Transaction 方法
    tx_methods = ["fetch", "fetch_one", "fetch_optional", "execute",
                  "execute_many", "commit", "rollback",
                  "__aenter__", "__aexit__"]
    for name in tx_methods:
        method = getattr(rsqlx.Transaction, name, None)
        assert method is not None, f"Transaction.{name} missing"
    ok("Transaction: all methods present")

    # ExecuteResult
    for attr in ["rows_affected", "last_insert_id"]:
        assert hasattr(rsqlx.ExecuteResult, attr), f"ExecuteResult.{attr} missing"
    ok("ExecuteResult: rows_affected, last_insert_id")

    # repr 存在
    assert hasattr(rsqlx.Pool, "__repr__")
    assert hasattr(rsqlx.Transaction, "__repr__")
    assert hasattr(rsqlx.ExecuteResult, "__repr__")
    ok("repr defined on all classes")


# ============================================================================
# 2. SQLite 全功能验证
# ============================================================================
def test_sqlite():
    print("\n=== 2. SQLite ===")
    _test_database("sqlite::memory:", "sqlite")


def test_postgres():
    url = os.environ.get("RSQLX_TEST_PG_URL")
    if not url:
        print("\n=== 3. PostgreSQL (skipped: set RSQLX_TEST_PG_URL) ===")
        return
    print("\n=== 3. PostgreSQL ===")
    _test_database(url, "pg")


def test_mysql():
    url = os.environ.get("RSQLX_TEST_MYSQL_URL")
    if not url:
        print("\n=== 4. MySQL (skipped: set RSQLX_TEST_MYSQL_URL) ===")
        return
    print("\n=== 4. MySQL ===")
    _test_database(url, "mysql")


def _test_database(url, dbtype):
    """Run the full test suite against a specific database."""

    def ph(n):
        """Generate n placeholders: $1..$n for PG, ?,?,... for MySQL/SQLite."""
        if dbtype == "pg":
            return ", ".join(f"${i}" for i in range(1, n + 1))
        return ", ".join("?" * 1 for _ in range(n))

    placeholder = "$1" if dbtype == "pg" else "?"

    async def main():
        pool = await rsqlx.connect(url, max_connections=5)

        # --- 基本连接池状态 ---
        assert not pool.is_closed
        assert isinstance(pool.size, int)
        assert isinstance(pool.num_idle, int)
        assert "Pool(" in repr(pool)
        ok(f"{dbtype}: connect + repr = {repr(pool)}")

        # --- DDL ---
        await pool.execute("DROP TABLE IF EXISTS api_test")
        if dbtype == "pg":
            await pool.execute(
                "CREATE TABLE api_test ("
                "id SERIAL PRIMARY KEY, name TEXT, age INT, price NUMERIC(10,2), "
                "data JSONB, created TIMESTAMP, aware_ts TIMESTAMPTZ, "
                "blob_col BYTEA, uid UUID, tags TEXT[], nums NUMERIC[])"
            )
        elif dbtype == "mysql":
            await pool.execute(
                "CREATE TABLE api_test ("
                "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), age INT, "
                "price DECIMAL(10,2), data JSON, created DATETIME(6), aware_ts TIMESTAMP(6) NULL, "
                "blob_col BLOB, uid CHAR(36), tags JSON)"
            )
        else:
            await pool.execute(
                "CREATE TABLE api_test ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, age INT, "
                "price REAL, data TEXT, created DATETIME, aware_ts DATETIME, "
                "blob_col BLOB, uid TEXT, tags TEXT)"
            )
        ok(f"{dbtype}: DDL (CREATE TABLE)")

        # --- INSERT with all types ---
        now_naive = dt.datetime(2026, 8, 28, 10, 30, 0, 123456)
        now_aware = dt.datetime(2026, 8, 28, 10, 30, 0, 123456, tzinfo=dt.timezone.utc)
        dec = decimal.Decimal("42.50")
        payload = {"key": [1, 2.5, "x", None, True], "nested": {"a": 1}}
        blob = b"\x00\x01\xff\xfe"

        if dbtype == "pg":
            res = await pool.execute(
                "INSERT INTO api_test (name, age, price, data, created, aware_ts, "
                "blob_col, uid, tags, nums) "
                f"VALUES ({ph(10)})",
                ["alice", 30, dec, payload, now_naive, now_aware,
                 blob, uuid.uuid4(), ["tag1", "tag2", None], [dec, decimal.Decimal("0")]],
            )
        elif dbtype == "mysql":
            res = await pool.execute(
                "INSERT INTO api_test (name, age, price, data, created, aware_ts, "
                "blob_col, uid, tags) "
                f"VALUES ({ph(9)})",
                ["alice", 30, dec, payload, now_naive, now_aware,
                 blob, str(uuid.uuid4()), ["tag1", "tag2"]],
            )
        else:
            res = await pool.execute(
                "INSERT INTO api_test (name, age, price, data, created, aware_ts, "
                "blob_col, uid, tags) "
                f"VALUES ({ph(9)})",
                ["alice", 30, dec, json.dumps(payload), now_naive, now_aware,
                 blob, str(uuid.uuid4()), json.dumps(["tag1", "tag2"])],
            )
        assert res.rows_affected == 1
        assert isinstance(res.rows_affected, int)
        assert res.last_insert_id is not None if dbtype != "pg" else res.last_insert_id is None
        ok(f"{dbtype}: INSERT all types -> rows_affected={res.rows_affected}, last_insert_id={res.last_insert_id}")
        ok(f"{dbtype}: ExecuteResult repr = {repr(res)}")

        # --- SELECT all types ---
        row = await pool.fetch_one(f"SELECT * FROM api_test WHERE name = {placeholder}", ["alice"])
        assert row["name"] == "alice"
        assert row["age"] == 30
        assert isinstance(row["age"], int)
        assert isinstance(row["price"], decimal.Decimal) if dbtype != "sqlite" else True
        assert row["blob_col"] == blob
        assert isinstance(row["blob_col"], bytes)
        ok(f"{dbtype}: SELECT all types -> row keys = {list(row.keys())}")

        # datetime roundtrip
        if dbtype == "pg":
            assert row["created"] == now_naive and row["created"].tzinfo is None
            assert row["aware_ts"].tzinfo is not None
            assert isinstance(row["uid"], uuid.UUID)
            assert row["tags"] == ["tag1", "tag2", None]
            assert row["nums"] == [dec, decimal.Decimal("0")]
            ok(f"{dbtype}: PG-specific types (UUID, TIMESTAMPTZ, arrays) ✓")
        elif dbtype == "mysql":
            assert row["created"] == now_naive
            assert row["data"] == payload  # JSON auto-decoded
            ok(f"{dbtype}: MySQL JSON auto-decoded ✓")
        else:
            assert row["created"] == now_naive
            assert isinstance(row["data"], str)  # SQLite stores as TEXT
            ok(f"{dbtype}: SQLite TEXT for JSON ✓")

        # --- fetch (list) ---
        rows = await pool.fetch(f"SELECT id, name FROM api_test WHERE age > {placeholder}", [0])
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert isinstance(rows[0], dict)
        ok(f"{dbtype}: fetch() returns list[dict]")

        # --- fetch_optional ---
        opt = await pool.fetch_optional(f"SELECT * FROM api_test WHERE id = {placeholder}", [999])
        assert opt is None
        opt = await pool.fetch_optional(f"SELECT * FROM api_test WHERE id = {placeholder}", [1])
        assert opt is not None
        ok(f"{dbtype}: fetch_optional() None / dict")

        # --- fetch_one RowNotFound ---
        try:
            await pool.fetch_one(f"SELECT * FROM api_test WHERE id = {placeholder}", [999])
            fail(f"{dbtype}: fetch_one should raise RowNotFound")
        except rsqlx.RowNotFound:
            ok(f"{dbtype}: fetch_one raises RowNotFound")
        except Exception as e:
            fail(f"{dbtype}: fetch_one wrong exception {type(e).__name__}")

        # --- execute with no params ---
        rows = await pool.fetch(f"SELECT COUNT(*) AS cnt FROM api_test")
        assert rows[0]["cnt"] == 1
        ok(f"{dbtype}: execute without params")

        # --- NULL params ---
        await pool.execute(
            f"INSERT INTO api_test (name, age) VALUES ({ph(2)})",
            ["null_test", None],
        )
        row = await pool.fetch_one(f"SELECT age FROM api_test WHERE name = {placeholder}", ["null_test"])
        assert row["age"] is None
        ok(f"{dbtype}: NULL parameter binding")

        # --- execute_many (batch INSERT) ---
        if dbtype == "pg":
            await pool.execute("DROP TABLE IF EXISTS batch_test")
            await pool.execute("CREATE TABLE batch_test (id SERIAL, v INT, name TEXT)")
        elif dbtype == "mysql":
            await pool.execute("DROP TABLE IF EXISTS batch_test")
            await pool.execute("CREATE TABLE batch_test (id INT AUTO_INCREMENT PRIMARY KEY, v INT, name VARCHAR(32))")
        else:
            await pool.execute("DROP TABLE IF EXISTS batch_test")
            await pool.execute("CREATE TABLE batch_test (id INTEGER PRIMARY KEY AUTOINCREMENT, v INT, name TEXT)")

        res = await pool.execute_many(
            f"INSERT INTO batch_test (v, name) VALUES ({ph(2)})",
            [[i, f"item_{i}"] for i in range(100)],
        )
        assert res.rows_affected == 100
        cnt = await pool.fetch_one("SELECT COUNT(*) AS n FROM batch_test")
        assert cnt["n"] == 100
        ok(f"{dbtype}: execute_many batch 100 -> rows_affected={res.rows_affected}")

        # --- transactions ---
        async with await pool.begin() as tx:
            await tx.execute(
                f"INSERT INTO batch_test (v, name) VALUES ({ph(2)})",
                [999, "tx_row"],
            )
            row = await tx.fetch_one(f"SELECT name FROM batch_test WHERE v = {placeholder}", [999])
            assert row["name"] == "tx_row"
        # after commit, visible from pool
        row = await pool.fetch_one(f"SELECT name FROM batch_test WHERE v = {placeholder}", [999])
        assert row["name"] == "tx_row"
        ok(f"{dbtype}: transaction commit (tx sees own writes + visible after)")

        # rollback
        try:
            async with await pool.begin() as tx:
                await tx.execute(
                    f"INSERT INTO batch_test (v, name) VALUES ({ph(2)})",
                    [998, "rollback_row"],
                )
                raise ValueError("simulated error")
        except ValueError:
            pass
        opt = await pool.fetch_optional(f"SELECT * FROM batch_test WHERE v = {placeholder}", [998])
        assert opt is None
        ok(f"{dbtype}: transaction rollback on exception")

        # manual tx
        tx = await pool.begin()
        await tx.execute(f"INSERT INTO batch_test (v) VALUES ({placeholder})", [777])
        await tx.commit()
        assert (await pool.fetch_one(f"SELECT v FROM batch_test WHERE v = {placeholder}", [777]))["v"] == 777
        # reuse after commit -> error
        try:
            await tx.execute("SELECT 1")
            fail(f"{dbtype}: tx after commit should raise")
        except rsqlx.InterfaceError:
            ok(f"{dbtype}: transaction reuse after commit -> InterfaceError")
        except Exception as e:
            fail(f"{dbtype}: tx reuse wrong exception {type(e).__name__}")

        # --- error mapping ---
        # syntax error
        try:
            await pool.fetch("SELECT * FROM")
            fail(f"{dbtype}: syntax error should raise")
        except rsqlx.DatabaseError:
            ok(f"{dbtype}: syntax error -> DatabaseError")
        except Exception as e:
            fail(f"{dbtype}: syntax error -> {type(e).__name__} (expected DatabaseError)")

        # nonexistent table
        try:
            await pool.fetch("SELECT * FROM nonexistent_table_xyz")
            fail(f"{dbtype}: missing table should raise")
        except rsqlx.DatabaseError:
            ok(f"{dbtype}: missing table -> DatabaseError")
        except Exception as e:
            fail(f"{dbtype}: missing table -> {type(e).__name__}")

        # --- async context manager ---
        async with await rsqlx.connect(url, max_connections=2) as p2:
            await p2.execute(f"SELECT {placeholder}", [1])
            assert not p2.is_closed
        assert p2.is_closed
        try:
            await p2.fetch("SELECT 1")
            fail(f"{dbtype}: closed pool should raise")
        except rsqlx.PoolClosed:
            ok(f"{dbtype}: closed pool -> PoolClosed")
        except Exception as e:
            fail(f"{dbtype}: closed pool -> {type(e).__name__} (expected PoolClosed)")

        # --- concurrency ---
        await pool.execute_many(
            f"INSERT INTO batch_test (v, name) VALUES ({ph(2)})",
            [[i, f"conc_{i}"] for i in range(200, 300)],
        )
        results = await asyncio.gather(*[
            pool.fetch_one(f"SELECT name FROM batch_test WHERE v = {placeholder}", [i])
            for i in range(200, 210)
        ])
        assert all(r["name"] == f"conc_{i}" for i, r in enumerate(results, 200))
        ok(f"{dbtype}: 10 concurrent queries via asyncio.gather")

        # --- migrations ---
        import tempfile
        from pathlib import Path
        mig_dir = Path(tempfile.mkdtemp()) / "migrations"
        mig_dir.mkdir()
        if dbtype == "pg":
            (mig_dir / "0001_init.up.sql").write_text(
                "CREATE TABLE mig_test (id SERIAL, val TEXT);", encoding="utf-8")
            (mig_dir / "0002_seed.up.sql").write_text(
                "INSERT INTO mig_test (val) VALUES ('seed1'), ('seed2');", encoding="utf-8")
        elif dbtype == "mysql":
            (mig_dir / "0001_init.up.sql").write_text(
                "CREATE TABLE mig_test (id INT AUTO_INCREMENT PRIMARY KEY, val VARCHAR(32));", encoding="utf-8")
            (mig_dir / "0002_seed.up.sql").write_text(
                "INSERT INTO mig_test (val) VALUES ('seed1'), ('seed2');", encoding="utf-8")
        else:
            (mig_dir / "0001_init.up.sql").write_text(
                "CREATE TABLE mig_test (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT);", encoding="utf-8")
            (mig_dir / "0002_seed.up.sql").write_text(
                "INSERT INTO mig_test (val) VALUES ('seed1'), ('seed2');", encoding="utf-8")

        await pool.execute("DROP TABLE IF EXISTS mig_test")
        await pool.execute("DROP TABLE IF EXISTS _sqlx_migrations")
        await pool.migrate(str(mig_dir))
        rows = await pool.fetch("SELECT val FROM mig_test ORDER BY id")
        assert [r["val"] for r in rows] == ["seed1", "seed2"]
        # idempotent
        await pool.migrate(str(mig_dir))
        cnt = await pool.fetch_one("SELECT COUNT(*) AS n FROM mig_test")
        assert cnt["n"] == 2
        ok(f"{dbtype}: migrate (2 scripts, idempotent)")

        # --- execute_raw / fetch_raw (if applicable) ---
        if dbtype == "mysql":
            await pool.execute_raw("DROP PROCEDURE IF EXISTS sp_raw_test")
            await pool.execute_raw(
                "CREATE PROCEDURE sp_raw_test(IN p_val INT) "
                "BEGIN INSERT INTO batch_test (v) VALUES (p_val); END"
            )
            await pool.execute("CALL sp_raw_test(?)", [888])
            row = await pool.fetch_one("SELECT v FROM batch_test WHERE v = ?", [888])
            assert row["v"] == 888
            ok(f"{dbtype}: execute_raw (CREATE PROCEDURE) + CALL")
        elif dbtype == "pg":
            # PG also supports execute_raw for multi-statement scripts
            await pool.execute_raw("DROP TABLE IF EXISTS raw_test; CREATE TABLE raw_test (id INT, note TEXT);")
            await pool.execute_raw("INSERT INTO raw_test VALUES (1, 'hello'); INSERT INTO raw_test VALUES (2, 'world');")
            rows = await pool.fetch_raw("SELECT * FROM raw_test ORDER BY id")
            assert len(rows) == 2
            ok(f"{dbtype}: execute_raw (multi-statement) + fetch_raw")
        else:
            # SQLite
            await pool.execute_raw("CREATE TABLE IF NOT EXISTS raw_test (id INT, note TEXT);")
            await pool.execute_raw("INSERT INTO raw_test VALUES (1, 'a'); INSERT INTO raw_test VALUES (2, 'b');")
            rows = await pool.fetch_raw("SELECT * FROM raw_test ORDER BY id")
            assert len(rows) == 2
            ok(f"{dbtype}: execute_raw + fetch_raw")

        # --- param edge cases ---
        # empty params list
        rows = await pool.fetch("SELECT 1 AS x", None)
        assert rows[0]["x"] == 1
        ok(f"{dbtype}: params=None (no params)")

        # tuple params (not just list)
        res = await pool.execute(
            f"INSERT INTO batch_test (v, name) VALUES ({ph(2)})",
            (42, "tuple_param"),
        )
        assert res.rows_affected == 1
        ok(f"{dbtype}: tuple params (not just list)")

        # float param
        if dbtype == "pg":
            await pool.execute("DROP TABLE IF EXISTS float_test")
            await pool.execute("CREATE TABLE float_test (id SERIAL, v DOUBLE PRECISION)")
        elif dbtype == "mysql":
            await pool.execute("DROP TABLE IF EXISTS float_test")
            await pool.execute("CREATE TABLE float_test (id INT AUTO_INCREMENT PRIMARY KEY, v DOUBLE)")
        else:
            await pool.execute("DROP TABLE IF EXISTS float_test")
            await pool.execute("CREATE TABLE float_test (id INTEGER PRIMARY KEY AUTOINCREMENT, v REAL)")
        await pool.execute(f"INSERT INTO float_test (v) VALUES ({placeholder})", [3.14159265])
        row = await pool.fetch_one("SELECT v FROM float_test")
        assert abs(row["v"] - 3.14159265) < 1e-6
        ok(f"{dbtype}: float param roundtrip")

        # bool param
        if dbtype == "pg":
            await pool.execute("DROP TABLE IF EXISTS bool_test")
            await pool.execute("CREATE TABLE bool_test (id SERIAL, flag BOOLEAN)")
        elif dbtype == "mysql":
            await pool.execute("DROP TABLE IF EXISTS bool_test")
            await pool.execute("CREATE TABLE bool_test (id INT AUTO_INCREMENT PRIMARY KEY, flag BOOLEAN)")
        else:
            await pool.execute("DROP TABLE IF EXISTS bool_test")
            await pool.execute("CREATE TABLE bool_test (id INTEGER PRIMARY KEY AUTOINCREMENT, flag BOOLEAN)")
        await pool.execute(f"INSERT INTO bool_test (flag) VALUES ({placeholder})", [True])
        await pool.execute(f"INSERT INTO bool_test (flag) VALUES ({placeholder})", [False])
        rows = await pool.fetch("SELECT flag FROM bool_test ORDER BY id")
        ok(f"{dbtype}: bool params -> {rows}")

        await pool.close()
        assert pool.is_closed
        ok(f"{dbtype}: close + is_closed")

    try:
        run(main())
    except Exception as e:
        fail(f"{dbtype}: unhandled exception", f"{type(e).__name__}: {e}")
        traceback.print_exc()


# ============================================================================
# 3. API 友好性: 边界情况
# ============================================================================
def test_edge_cases():
    print("\n=== 5. Edge Cases ===")

    # connect with invalid URL
    async def test_bad_url():
        try:
            await rsqlx.connect("oracle://nope")
            fail("invalid URL should raise")
        except ValueError as e:
            ok(f"invalid URL scheme -> ValueError: {str(e)[:40]}")
        except Exception as e:
            fail(f"invalid URL -> {type(e).__name__} (expected ValueError)")

    run(test_bad_url())

    # negative timeout
    async def test_neg_timeout():
        try:
            await rsqlx.connect("sqlite::memory:", acquire_timeout=-1.0)
            fail("negative timeout should raise")
        except ValueError:
            ok("negative acquire_timeout -> ValueError")
        except Exception as e:
            fail(f"negative timeout -> {type(e).__name__}")

    run(test_neg_timeout())

    # unsupported param type
    async def test_bad_param():
        pool = await rsqlx.connect("sqlite::memory:")
        try:
            await pool.execute("SELECT ?", [object()])
            fail("unsupported param should raise")
        except TypeError as e:
            ok(f"unsupported param type -> TypeError: {str(e)[:40]}")
        except Exception as e:
            fail(f"unsupported param -> {type(e).__name__}")
        await pool.close()

    run(test_bad_param())

    # empty execute_many
    async def test_empty_many():
        pool = await rsqlx.connect("sqlite::memory:")
        await pool.execute("CREATE TABLE em (id INTEGER PRIMARY KEY AUTOINCREMENT, v INT)")
        res = await pool.execute_many("INSERT INTO em (v) VALUES (?)", [])
        assert res.rows_affected == 0
        ok(f"execute_many empty list -> rows_affected=0")
        await pool.close()

    run(test_empty_many())

    # pool context manager exit on exception
    async def test_ctx_exit_on_exc():
        try:
            async with await rsqlx.connect("sqlite::memory:") as pool:
                await pool.execute("CREATE TABLE x (v INT)")
                raise RuntimeError("ctx exit test")
        except RuntimeError:
            pass
        # pool should be closed after exception in ctx
        assert pool.is_closed
        ok("pool auto-closed on exception in async with")

    run(test_ctx_exit_on_exc())

    # large text
    async def test_large_text():
        pool = await rsqlx.connect("sqlite::memory:")
        await pool.execute("CREATE TABLE lt (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
        big = "x" * 100000
        await pool.execute("INSERT INTO lt (text) VALUES (?)", [big])
        row = await pool.fetch_one("SELECT text FROM lt")
        assert len(row["text"]) == 100000
        ok("large text (100KB) roundtrip")
        await pool.close()

    run(test_large_text())


# ============================================================================
# Main
# ============================================================================
if __name__ == "__main__":
    test_api_surface()
    test_sqlite()
    test_postgres()
    test_mysql()
    test_edge_cases()

    print(f"\n{'='*60}")
    print(f"TOTAL: {PASSED} passed, {FAILED} failed, {len(WARNINGS)} warnings")
    print(f"{'='*60}")
    sys.exit(1 if FAILED else 0)
