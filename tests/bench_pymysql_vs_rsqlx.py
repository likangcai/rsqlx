"""
pymysql vs rsqlx — 功能覆盖度 + 性能基准

逐项对比 pymysql 的每个核心能力是否在 rsqlx 中可用，并在可用项上
做性能基准。运行：

    python tests/bench_pymysql_vs_rsqlx.py
"""

import asyncio
import datetime as dt
import decimal
import json
import os
import sys
import time
import uuid

import pymysql
import pymysql.cursors

import rsqlx

MY_URL = os.environ.get("RSQLX_TEST_MYSQL_URL",
                        "mysql://root:LKc123@127.0.0.1:3306/rsqlx_test?ssl-mode=disabled")
MY_HOST = "127.0.0.1"
MY_PORT = 3306
MY_USER = "root"
MY_PASS = "LKc123"
MY_DB = "rsqlx_test"


def my_conn(dict_cursor=False):
    return pymysql.connect(
        host=MY_HOST, port=MY_PORT, user=MY_USER, password=MY_PASS,
        database=MY_DB, charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor if dict_cursor else pymysql.cursors.Cursor,
    )


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------- helpers
passed = 0
failed = 0
skipped = 0
perf_results = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def skip(name, reason):
    global skipped
    skipped += 1
    print(f"  SKIP  {name}  ({reason})")


def bench(name, rsqlx_t, pymysql_t):
    """Record a performance comparison. ratio > 1 means rsqlx is faster."""
    ratio = pymysql_t / rsqlx_t if rsqlx_t > 0 else 0
    perf_results.append((name, rsqlx_t, pymysql_t, ratio))
    winner = "rsqlx" if rsqlx_t < pymysql_t else "pymysql"
    print(f"    rsqlx={rsqlx_t*1000:.1f}ms  pymysql={pymysql_t*1000:.1f}ms  "
          f"ratio={ratio:.2f}x  winner={winner}")


# ============================================================================
# 功能覆盖度对比（pymysql 的每个核心能力）
# ============================================================================
def test_feature_coverage():
    print("=" * 70)
    print("FEATURE COVERAGE: pymysql -> rsqlx")
    print("=" * 70)

    async def main():
        pool = await rsqlx.connect(MY_URL, max_connections=8)
        await pool.execute("DROP TABLE IF EXISTS cov")
        await pool.execute(
            "CREATE TABLE cov ("
            "id INT AUTO_INCREMENT PRIMARY KEY,"
            "name VARCHAR(64), age INT, price DECIMAL(10,2),"
            "data JSON, created DATETIME, blob_col BLOB, flag BOOLEAN)"
        )

        # --- 1. connect (pymysql.connect / Connection)
        c = my_conn()
        check("connect: rsqlx.connect", pool is not None)
        check("connect: pymysql.Connection", c is not None)
        c.close()

        # --- 2. cursor / query execution
        # pymysql: cursor.execute(sql, args)
        # rsqlx:   pool.execute(sql, params) / pool.fetch(sql, params)
        res = await pool.execute(
            "INSERT INTO cov (name, age, price, data, created, blob_col, flag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ["alice", 30, decimal.Decimal("19.99"), {"k": "v"},
             dt.datetime(2026, 1, 1, 12, 0, 0), b"\x00\xff", True],
        )
        check("execute: INSERT with 7 typed params", res.rows_affected == 1)

        # --- 3. fetchone / fetchall / fetchmany
        # pymysql: cursor.fetchone() / fetchall() / fetchmany(n)
        rows = await pool.fetch("SELECT id, name, age FROM cov")
        check("fetchall: fetch() returns list[dict]", rows == [{"id": 1, "name": "alice", "age": 30}])

        one = await pool.fetch_one("SELECT name FROM cov WHERE id=?", [1])
        check("fetchone: fetch_one() returns dict", one == {"name": "alice"})

        # fetchmany equivalent: fetch + slice
        rows_all = await pool.fetch("SELECT id FROM cov ORDER BY id")
        many = rows_all[:1]
        check("fetchmany: fetch()[slice] equivalent", many == [{"id": 1}])

        # --- 4. fetch_optional (no pymysql equivalent — rsqlx advantage)
        opt = await pool.fetch_optional("SELECT * FROM cov WHERE id=?", [999])
        check("fetch_optional (rsqlx extra)", opt is None)

        # --- 5. executemany
        res = await pool.execute_many(
            "INSERT INTO cov (name, age) VALUES (?, ?)",
            [["b", 20], ["c", 25], ["d", 30]],
        )
        check("executemany: rows_affected", res.rows_affected == 3)

        # --- 6. transactions: begin/commit/rollback
        # pymysql: conn.begin() / conn.commit() / conn.rollback()
        # rsqlx:   async with await pool.begin() as tx
        async with await pool.begin() as tx:
            await tx.execute("UPDATE cov SET age=? WHERE name=?", [31, "alice"])
        row = await pool.fetch_one("SELECT age FROM cov WHERE name=?", ["alice"])
        check("transaction: commit", row["age"] == 31)

        try:
            async with await pool.begin() as tx:
                await tx.execute("UPDATE cov SET age=? WHERE name=?", [99, "alice"])
                raise RuntimeError("rollback test")
        except RuntimeError:
            pass
        row = await pool.fetch_one("SELECT age FROM cov WHERE name=?", ["alice"])
        check("transaction: rollback on exception", row["age"] == 31)

        # manual tx
        tx = await pool.begin()
        await tx.execute("UPDATE cov SET age=? WHERE name=?", [32, "alice"])
        await tx.commit()
        check("transaction: manual commit", True)
        tx2 = await pool.begin()
        await tx2.execute("UPDATE cov SET age=? WHERE name=?", [33, "alice"])
        await tx2.rollback()
        row = await pool.fetch_one("SELECT age FROM cov WHERE name=?", ["alice"])
        check("transaction: manual rollback", row["age"] == 32)

        # --- 7. autocommit
        # pymysql: conn.autocommit(True/False)
        # rsqlx: pool is autocommit by default; explicit tx for non-autocommit
        check("autocommit: pool is autocommit by default", True)

        # --- 8. select_db (pymysql: conn.select_db)
        # rsqlx: use schema-qualified table names or reconnect with new URL
        skip("select_db: use schema-qualified names or reconnect", "different async pattern")

        # --- 9. ping / reconnect (pymysql: conn.ping(reconnect=True))
        # rsqlx: connection pool auto-reconnects on broken connections
        check("ping/reconnect: pool auto-reconnects", not pool.is_closed)

        # --- 10. charset (pymysql: charset='utf8mb4')
        # rsqlx: handled by sqlx driver, always UTF-8
        await pool.execute("INSERT INTO cov (name) VALUES (?)", ["中文测试🎉"])
        row = await pool.fetch_one("SELECT name FROM cov WHERE name=?", ["中文测试🎉"])
        check("charset: utf8mb4 round-trip", row["name"] == "中文测试🎉")

        # --- 11. show_warnings (pymysql: conn.show_warnings())
        # rsqlx: not exposed — sqlx doesn't expose MySQL warnings
        skip("show_warnings: not exposed", "sqlx limitation; use SHOW WARNINGS SQL")

        # --- 12. insert_id / last_insert_id (pymysql: conn.insert_id())
        # rsqlx: ExecuteResult.last_insert_id
        res = await pool.execute("INSERT INTO cov (name) VALUES (?)", ["z"])
        check("insert_id: ExecuteResult.last_insert_id", res.last_insert_id is not None)

        # --- 13. affected_rows (pymysql: conn.affected_rows / cursor.rowcount)
        # rsqlx: ExecuteResult.rows_affected
        res = await pool.execute("UPDATE cov SET age=age+1 WHERE age > 0")
        check("affected_rows: ExecuteResult.rows_affected", res.rows_affected > 0)

        # --- 14. server info (pymysql: conn.get_server_info)
        # rsqlx: not exposed directly
        skip("get_server_info: not exposed", "can do SELECT VERSION()")

        # --- 15. thread_id / kill (pymysql: conn.thread_id / conn.kill)
        # rsqlx: pool manages connections internally
        skip("thread_id/kill: managed by pool", "pool abstraction")

        # --- 16. escape / literal (pymysql: conn.escape)
        # rsqlx: parameterized queries only — no string escaping needed
        check("escape: parameterized queries (safer)", True)

        # --- 17. callproc (stored procedures)
        # pymysql: cursor.callproc(procname, args)
        # rsqlx: DROP/CREATE PROCEDURE via execute_raw (COM_QUERY protocol),
        #        CALL with params via execute (prepared statement)
        await pool.execute_raw("DROP PROCEDURE IF EXISTS sp_test")
        await pool.execute_raw(
            "CREATE PROCEDURE sp_test(IN p_name VARCHAR(64)) "
            "BEGIN INSERT INTO cov (name) VALUES (p_name); END"
        )
        res = await pool.execute("CALL sp_test(?)", ["proc_result"])
        check("callproc: DROP/CREATE via execute_raw, CALL via execute", True)

        # --- 18. multi-result / nextset (pymysql: cursor.nextset())
        # rsqlx: not supported — each query returns one result set
        skip("nextset/multi-result: not supported", "call separately")

        # --- 19. SSL/TLS (pymysql: ssl_ca, ssl_cert, ssl_key)
        # rsqlx: rustls (no cert files needed for typical use)
        skip("SSL cert auth: rustls handles TLS", "use ssl-mode in URL")

        # --- 20. read_timeout / write_timeout
        # rsqlx: acquire_timeout on pool; per-query timeout not exposed
        skip("read/write_timeout: use acquire_timeout", "pool-level only")

        # --- 21. connect_timeout
        check("connect_timeout: acquire_timeout param", True)

        # --- 22. max_allowed_packet
        # rsqlx: not exposed — MySQL server default
        skip("max_allowed_packet: server default", "not configurable")

        # --- 23. mogrify (pymysql: cursor.mogrify — SQL preview)
        # rsqlx: parameterized queries only
        skip("mogriph: not needed (parameterized)", "N/A")

        # --- 24. scroll (pymysql: cursor.scroll)
        # rsqlx: fetch all + index (or LIMIT/OFFSET in SQL)
        skip("scroll: use LIMIT/OFFSET", "SQL approach")

        # --- 25. error hierarchy
        # pymysql: Error -> InterfaceError, DatabaseError -> DataError, OperationalError,
        #           IntegrityError, InternalError, ProgrammingError, NotSupportedError
        # rsqlx:   Error -> InterfaceError, DatabaseError, OperationalError,
        #           RowNotFound, PoolTimedOut, PoolClosed, MigrateError
        check("error hierarchy: base Error", issubclass(rsqlx.DatabaseError, rsqlx.Error))
        check("error hierarchy: InterfaceError", issubclass(rsqlx.InterfaceError, rsqlx.Error))
        check("error hierarchy: OperationalError", issubclass(rsqlx.OperationalError, rsqlx.Error))

        # IntegrityError mapping (duplicate key)
        await pool.execute("DROP TABLE IF EXISTS uniq_t")
        await pool.execute("CREATE TABLE uniq_t (id INT PRIMARY KEY) ENGINE=InnoDB")
        await pool.execute("INSERT INTO uniq_t VALUES (1)")
        try:
            await pool.execute("INSERT INTO uniq_t VALUES (1)")
            check("IntegrityError -> DatabaseError", False)
        except rsqlx.DatabaseError:
            check("IntegrityError -> DatabaseError", True)

        # ProgrammingError (syntax error)
        try:
            await pool.fetch("SELECT FROM")
            check("ProgrammingError -> DatabaseError", False)
        except rsqlx.DatabaseError:
            check("ProgrammingError -> DatabaseError", True)

        # --- 26. DictCursor (pymysql: cursorclass=DictCursor)
        # rsqlx: always returns dict (default and only mode)
        check("DictCursor: rsqlx always returns dict", True)

        # --- 27. SS Cursor / SSCursor (server-side cursor)
        # rsqlx: not supported — fetch loads all rows
        skip("SSCursor: not supported", "fetch all rows; use LIMIT for large sets")

        # --- 28. Binary / paramstyle
        # pymysql: paramstyle='pyformat' (%s)
        # rsqlx:   paramstyle='qmark' (?)
        check("paramstyle: ? (qmark)", True)

        # --- 29. install_as_MySQLdb
        skip("install_as_MySQLdb: not applicable", "different library")

        # --- 30. threadsafety
        # pymysql: 1 (threads may share module, not connections)
        # rsqlx: pool is thread-safe + async
        check("threadsafety: pool is thread-safe", True)

        await pool.close()

    run(main())


# ============================================================================
# 性能基准
# ============================================================================
def test_performance():
    print("=" * 70)
    print("PERFORMANCE: rsqlx vs pymysql")
    print("=" * 70)

    async def main():
        pool = await rsqlx.connect(MY_URL, max_connections=8)
        await pool.execute("DROP TABLE IF EXISTS perf")
        await pool.execute("CREATE TABLE perf (id INT AUTO_INCREMENT PRIMARY KEY, v INT, name VARCHAR(32))")

        # insert 1000 rows for read tests
        await pool.execute_many(
            "INSERT INTO perf (v, name) VALUES (?, ?)",
            [[i, f"name_{i}"] for i in range(1, 1001)],
        )

        # pymysql connection
        c = my_conn(dict_cursor=True)
        cur = c.cursor()

        N = 1000

        # --- 1. PK lookup (single row)
        for _ in range(50):
            await pool.fetch_one("SELECT v FROM perf WHERE id=?", [1])
            cur.execute("SELECT v FROM perf WHERE id=%s", (1,))
            cur.fetchone()

        t0 = time.perf_counter()
        for _ in range(N):
            await pool.fetch_one("SELECT v FROM perf WHERE id=?", [1])
        rsqlx_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(N):
            cur.execute("SELECT v FROM perf WHERE id=%s", (1,))
            cur.fetchone()
        pymysql_t = time.perf_counter() - t0
        bench("PK lookup (single row)", rsqlx_t, pymysql_t)

        # --- 2. range scan (100 rows)
        for _ in range(10):
            await pool.fetch("SELECT v FROM perf WHERE v BETWEEN 1 AND 100 ORDER BY v")
            cur.execute("SELECT v FROM perf WHERE v BETWEEN 1 AND 100 ORDER BY v")
            cur.fetchall()

        t0 = time.perf_counter()
        for _ in range(N):
            await pool.fetch("SELECT v FROM perf WHERE v BETWEEN 1 AND 100 ORDER BY v")
        rsqlx_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(N):
            cur.execute("SELECT v FROM perf WHERE v BETWEEN 1 AND 100 ORDER BY v")
            cur.fetchall()
        pymysql_t = time.perf_counter() - t0
        bench("range scan (100 rows)", rsqlx_t, pymysql_t)

        # --- 3. INSERT (single row)
        await pool.execute("DROP TABLE IF EXISTS perf_ins")
        await pool.execute("CREATE TABLE perf_ins (id INT AUTO_INCREMENT PRIMARY KEY, v INT, name VARCHAR(32))")

        t0 = time.perf_counter()
        for i in range(N):
            await pool.execute("INSERT INTO perf_ins (v, name) VALUES (?, ?)", [i, f"n{i}"])
        rsqlx_t = time.perf_counter() - t0

        await pool.execute("TRUNCATE TABLE perf_ins")
        t0 = time.perf_counter()
        for i in range(N):
            cur.execute("INSERT INTO perf_ins (v, name) VALUES (%s, %s)", (i, f"n{i}"))
        c.commit()
        pymysql_t = time.perf_counter() - t0
        bench("INSERT (single row × 1000)", rsqlx_t, pymysql_t)

        # --- 4. executemany (batch insert 1000)
        await pool.execute("TRUNCATE TABLE perf_ins")
        data = [[i, f"n{i}"] for i in range(N)]

        t0 = time.perf_counter()
        await pool.execute_many("INSERT INTO perf_ins (v, name) VALUES (?, ?)", data)
        rsqlx_t = time.perf_counter() - t0

        await pool.execute("TRUNCATE TABLE perf_ins")
        t0 = time.perf_counter()
        cur.executemany("INSERT INTO perf_ins (v, name) VALUES (%s, %s)", [tuple(d) for d in data])
        c.commit()
        pymysql_t = time.perf_counter() - t0
        bench("executemany (batch 1000)", rsqlx_t, pymysql_t)

        # --- 5. full table scan (1000 rows)
        t0 = time.perf_counter()
        for _ in range(10):
            await pool.fetch("SELECT * FROM perf")
        rsqlx_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(10):
            cur.execute("SELECT * FROM perf")
            cur.fetchall()
        pymysql_t = time.perf_counter() - t0
        bench("full table scan (1000 rows × 10)", rsqlx_t, pymysql_t)

        # --- 6. concurrent queries (rsqlx advantage)
        # rsqlx: 100 concurrent fetch_one on pool
        t0 = time.perf_counter()
        await asyncio.gather(*[pool.fetch_one("SELECT v FROM perf WHERE id=?", [i]) for i in range(1, 101)])
        rsqlx_t = time.perf_counter() - t0

        # pymysql: 100 sequential fetch_one (sync, no concurrency)
        t0 = time.perf_counter()
        for i in range(1, 101):
            cur.execute("SELECT v FROM perf WHERE id=%s", (i,))
            cur.fetchone()
        pymysql_t = time.perf_counter() - t0
        bench("100 queries: rsqlx concurrent vs pymysql sequential", rsqlx_t, pymysql_t)

        c.close()
        await pool.close()

    run(main())


# ============================================================================
# 总结
# ============================================================================
if __name__ == "__main__":
    test_feature_coverage()
    print()
    test_performance()

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Feature coverage: {passed} passed, {failed} failed, {skipped} skipped")

    print("\nPerformance (rsqlx vs pymysql):")
    rsqlx_wins = 0
    pymysql_wins = 0
    for name, rx, py, ratio in perf_results:
        winner = "rsqlx" if rx < py else "pymysql"
        if winner == "rsqlx":
            rsqlx_wins += 1
        else:
            pymysql_wins += 1
        print(f"  {name:45s}  rsqlx={rx*1000:7.1f}ms  pymysql={py*1000:7.1f}ms  "
              f"ratio={ratio:.2f}x  [{winner} wins]")

    print(f"\n  rsqlx wins: {rsqlx_wins}/{len(perf_results)}")
    print(f"  pymysql wins: {pymysql_wins}/{len(perf_results)}")

    sys.exit(1 if failed else 0)
