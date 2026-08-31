"""Comprehensive MySQL verification: rsqlx vs pymysql, side by side.

Set the MySQL URL via the RSQLX_TEST_MYSQL_URL environment variable, e.g.:
    set RSQLX_TEST_MYSQL_URL=mysql://user:password@127.0.0.1:3306/testdb

Then run:  python tests\verify_mysql.py
"""

import asyncio
import datetime as dt
import decimal
import json
import os
import sys

import pymysql
import pymysql.cursors

import rsqlx

MY_URL = os.environ.get("RSQLX_TEST_MYSQL_URL")
if not MY_URL:
    print("Set RSQLX_TEST_MYSQL_URL to run MySQL verification.")
    sys.exit(0)

# parse the URL for pymysql (which uses separate kwargs)
# mysql://user:pass@host:port/db
from urllib.parse import urlparse
_p = urlparse(MY_URL)
MY_HOST = _p.hostname or "127.0.0.1"
MY_PORT = _p.port or 3306
MY_USER = _p.username or "root"
MY_PASS = _p.password or ""
MY_DB = _p.path.lstrip("/") or "mysql"


def my_conn():
    return pymysql.connect(
        host=MY_HOST, port=MY_PORT, user=MY_USER, password=MY_PASS,
        database=MY_DB, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, autocommit=True,
    )


def run(coro):
    return asyncio.run(coro)


passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def t_basic_crud():
    print("[basic CRUD]")
    async def main():
        pool = await rsqlx.connect(MY_URL, max_connections=5)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_basic")
        await pool.execute(
            "CREATE TABLE rsqlx_my_basic (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), age INT)"
        )
        res = await pool.execute("INSERT INTO rsqlx_my_basic (name, age) VALUES (?, ?)", ["alice", 30])
        check("insert rows_affected", res.rows_affected == 1, res.rows_affected)
        check("insert last_insert_id", res.last_insert_id == 1, res.last_insert_id)

        row = await pool.fetch_one("SELECT id, name, age FROM rsqlx_my_basic WHERE name=?", ["alice"])
        check("fetch_one dict", row == {"id": 1, "name": "alice", "age": 30}, row)

        rows = await pool.fetch("SELECT * FROM rsqlx_my_basic ORDER BY id")
        check("fetch list", rows == [row], rows)

        none = await pool.fetch_optional("SELECT * FROM rsqlx_my_basic WHERE id=?", [999])
        check("fetch_optional None", none is None)

        try:
            await pool.fetch_one("SELECT * FROM rsqlx_my_basic WHERE id=?", [999])
            check("fetch_one RowNotFound", False)
        except rsqlx.RowNotFound:
            check("fetch_one RowNotFound", True)

        up = await pool.execute("UPDATE rsqlx_my_basic SET age=? WHERE name=?", [31, "alice"])
        check("update rows_affected", up.rows_affected == 1, up.rows_affected)

        # pymysql cross-check
        c = my_conn(); cur = c.cursor()
        cur.execute("SELECT * FROM rsqlx_my_basic WHERE name=%s", ("alice",))
        ref = cur.fetchone()
        check("matches pymysql", ref == {"id": 1, "name": "alice", "age": 31}, ref)
        c.close()
        await pool.close()
    run(main())


def t_types():
    print("[type mapping]")
    async def main():
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_types")
        await pool.execute(
            "CREATE TABLE rsqlx_my_types ("
            " id INT AUTO_INCREMENT PRIMARY KEY,"
            " b BOOLEAN, ti TINYINT, tu TINYINT UNSIGNED,"
            " si SMALLINT, su SMALLINT UNSIGNED, ii INT, iu INT UNSIGNED,"
            " bi BIGINT, bu BIGINT UNSIGNED, yr YEAR,"
            " fl FLOAT, dl DOUBLE, num DECIMAL(10,4),"
            " ch CHAR(3), vc VARCHAR(16), txt TEXT,"
            " bl BLOB, j JSON,"
            " d DATE, tm TIME, dt DATETIME, ts TIMESTAMP NULL)"
        )
        naive = dt.datetime(2026, 8, 27, 12, 34, 56)
        d = dt.date(2026, 8, 27)
        tm = dt.time(12, 34, 56)
        dec = decimal.Decimal("123.4567")
        payload = {"k": [1, 2.5, "x", None, True], "nested": {"a": 1}}
        await pool.execute(
            "INSERT INTO rsqlx_my_types ("
            "b,ti,tu,si,su,ii,iu,bi,bu,yr,fl,dl,num,ch,vc,txt,bl,j,d,tm,dt,ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                True, 1, 200, -30000, 60000, 2000000, 4000000,
                9_000_000_000, 18_000_000_000, 2026,
                1.25, 2.5, dec, "abc", "vc", "hello text",
                b"\x00\x01\xff", payload, d, tm, naive, naive,
            ],
        )
        row = await pool.fetch_one("SELECT * FROM rsqlx_my_types ORDER BY id DESC LIMIT 1")

        c = my_conn(); cur = c.cursor()
        cur.execute("SELECT * FROM rsqlx_my_types ORDER BY id DESC LIMIT 1")
        ref = cur.fetchone(); c.close()

        checks = [
            ("boolean", row["b"] is True or row["b"] == 1),
            ("tinyint", row["ti"] == 1),
            ("tinyint unsigned", row["tu"] == 200),
            ("smallint", row["si"] == -30000),
            ("smallint unsigned", row["su"] == 60000),
            ("int", row["ii"] == 2000000),
            ("int unsigned", row["iu"] == 4000000),
            ("bigint", row["bi"] == 9_000_000_000),
            ("bigint unsigned", row["bu"] == 18_000_000_000),
            ("year", row["yr"] == 2026),
            ("float", abs(row["fl"] - 1.25) < 0.01),
            ("double", row["dl"] == 2.5),
            ("decimal Decimal", isinstance(row["num"], decimal.Decimal) and row["num"] == dec),
            ("char(3)", row["ch"] == "abc"),
            ("varchar", row["vc"] == "vc"),
            ("text", row["txt"] == "hello text"),
            ("blob", row["bl"] == b"\x00\x01\xff"),
            ("json", row["j"] == payload),
            ("date", row["d"] == d),
            ("time", row["tm"] == tm),
            ("datetime", row["dt"] == naive),
            ("blob matches pymysql", row["bl"] == ref["bl"]),
            ("json matches pymysql", row["j"] == (json.loads(ref["j"]) if ref["j"] is not None else None)),
            ("datetime matches pymysql", row["dt"] == ref["dt"]),
        ]
        for name, ok in checks:
            check(name, ok, f"rsqlx={row.get(name.split()[0])!r} pymysql={ref.get(name.split()[0])!r}")
        await pool.close()
    run(main())


def t_null():
    print("[NULL handling]")
    async def main():
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_null")
        await pool.execute("CREATE TABLE rsqlx_my_null (id INT AUTO_INCREMENT PRIMARY KEY, s VARCHAR(32), n INT, f DOUBLE, b BLOB, j JSON)")
        await pool.execute("INSERT INTO rsqlx_my_null (s,n,f,b,j) VALUES (?,?,?,?,?)", [None, None, None, None, None])
        row = await pool.fetch_one("SELECT s,n,f,b,j FROM rsqlx_my_null")
        check("all null", row == {"s": None, "n": None, "f": None, "b": None, "j": None}, row)
        await pool.execute("INSERT INTO rsqlx_my_null (s,n) VALUES (?,?)", ["x", None])
        row2 = await pool.fetch_one("SELECT s,n FROM rsqlx_my_null WHERE id=?", [2])
        check("mixed null", row2 == {"s": "x", "n": None}, row2)
        await pool.close()
    run(main())


def t_transactions():
    print("[transactions]")
    async def main():
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_tx")
        # MySQL DDL auto-commits; use a separate table
        await pool.execute("CREATE TABLE rsqlx_my_tx (id INT AUTO_INCREMENT PRIMARY KEY, v INT) ENGINE=InnoDB")
        await pool.execute("TRUNCATE TABLE rsqlx_my_tx")

        # commit on success
        async with await pool.begin() as tx:
            await tx.execute("INSERT INTO rsqlx_my_tx (v) VALUES (?)", [1])
            await tx.execute("INSERT INTO rsqlx_my_tx (v) VALUES (?)", [2])
        rows = await pool.fetch("SELECT v FROM rsqlx_my_tx ORDER BY v")
        check("tx commit", rows == [{"v": 1}, {"v": 2}], rows)

        # rollback on exception
        try:
            async with await pool.begin() as tx:
                await tx.execute("INSERT INTO rsqlx_my_tx (v) VALUES (?)", [99])
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        rows = await pool.fetch("SELECT v FROM rsqlx_my_tx ORDER BY v")
        check("tx rollback", rows == [{"v": 1}, {"v": 2}], rows)

        # manual commit/rollback
        tx = await pool.begin()
        await tx.execute("INSERT INTO rsqlx_my_tx (v) VALUES (?)", [3])
        await tx.commit()
        check("manual commit", True)
        try:
            await tx.execute("SELECT 1")
            check("tx after commit raises", False)
        except rsqlx.InterfaceError:
            check("tx after commit raises", True)

        tx2 = await pool.begin()
        await tx2.execute("INSERT INTO rsqlx_my_tx (v) VALUES (?)", [4])
        await tx2.rollback()
        rows = await pool.fetch("SELECT v FROM rsqlx_my_tx ORDER BY v")
        check("manual rollback", rows == [{"v": 1}, {"v": 2}, {"v": 3}], rows)
        await pool.close()
    run(main())


def t_execute_many():
    print("[execute_many]")
    async def main():
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_em")
        await pool.execute("CREATE TABLE rsqlx_my_em (id INT AUTO_INCREMENT PRIMARY KEY, v INT, name VARCHAR(16))")
        res = await pool.execute_many(
            "INSERT INTO rsqlx_my_em (v, name) VALUES (?, ?)",
            [[1, "a"], [2, "b"], [3, "c"], [4, None]],
        )
        check("execute_many rows_affected", res.rows_affected == 4, res.rows_affected)
        n = (await pool.fetch_one("SELECT COUNT(*) AS n FROM rsqlx_my_em"))["n"]
        check("execute_many inserted", n == 4, n)
        # pymysql executemany parity
        c = my_conn(); cur = c.cursor()
        cur.executemany("INSERT INTO rsqlx_my_em (v, name) VALUES (%s, %s)", [(5, "e"), (6, "f")])
        c.commit()
        c.close()
        n2 = (await pool.fetch_one("SELECT COUNT(*) AS n FROM rsqlx_my_em"))["n"]
        check("pymysql executemany parity", n2 == 6, n2)
        await pool.close()
    run(main())


def t_concurrency():
    print("[concurrency]")
    async def main():
        pool = await rsqlx.connect(MY_URL, max_connections=10)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_conc")
        await pool.execute("CREATE TABLE rsqlx_my_conc (id INT AUTO_INCREMENT PRIMARY KEY, v INT)")
        await pool.execute_many("INSERT INTO rsqlx_my_conc (v) VALUES (?)", [[i] for i in range(1, 101)])

        async def get(i):
            r = await pool.fetch_one("SELECT v FROM rsqlx_my_conc WHERE v=?", [i])
            return r["v"]

        results = await asyncio.gather(*[get(i) for i in range(1, 51)])
        check("50 concurrent fetch_one", results == list(range(1, 51)), results[:5])
        await pool.close()
    run(main())


def t_errors():
    print("[error mapping]")
    async def main():
        pool = await rsqlx.connect(MY_URL)
        try:
            await pool.fetch("SELECT FROM")
            check("syntax error raises", False)
        except rsqlx.DatabaseError as e:
            check("syntax error -> DatabaseError", "42000" in str(e) or "syntax" in str(e).lower() or "语法" in str(e), str(e))
        try:
            await pool.fetch("SELECT * FROM does_not_exist")
            check("missing table raises", False)
        except rsqlx.DatabaseError:
            check("missing table -> DatabaseError", True)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_err")
        await pool.execute("CREATE TABLE rsqlx_my_err (id INT PRIMARY KEY)")
        await pool.execute("INSERT INTO rsqlx_my_err VALUES (1)")
        try:
            await pool.execute("INSERT INTO rsqlx_my_err VALUES (1)")
            check("duplicate key raises", False)
        except rsqlx.DatabaseError as e:
            check("duplicate key -> DatabaseError", "1062" in str(e) or "23000" in str(e), str(e))
        await pool.close()
        try:
            await pool.fetch("SELECT 1")
            check("closed pool raises", False)
        except rsqlx.PoolClosed:
            check("closed pool -> PoolClosed", True)
    run(main())


def t_migrate():
    import tempfile
    from pathlib import Path
    print("[migrations]")
    async def main():
        mig = Path(tempfile.mkdtemp()) / "migrations"
        mig.mkdir()
        (mig / "0001_init.up.sql").write_text(
            "CREATE TABLE rsqlx_my_mig (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(32));", encoding="utf-8"
        )
        (mig / "0002_seed.up.sql").write_text(
            "INSERT INTO rsqlx_my_mig (name) VALUES ('seed1'), ('seed2');", encoding="utf-8"
        )
        (mig / "0003_col.up.sql").write_text(
            "ALTER TABLE rsqlx_my_mig ADD COLUMN note VARCHAR(64);", encoding="utf-8"
        )
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_mig")
        await pool.execute("DROP TABLE IF EXISTS _sqlx_migrations")
        await pool.migrate(str(mig))
        rows = await pool.fetch("SELECT name, note FROM rsqlx_my_mig ORDER BY id")
        check("migrate applied 3 scripts", rows == [{"name": "seed1", "note": None}, {"name": "seed2", "note": None}], rows)
        await pool.migrate(str(mig))
        n = (await pool.fetch_one("SELECT COUNT(*) AS n FROM rsqlx_my_mig"))["n"]
        check("migrate idempotent", n == 2, n)
        await pool.close()
    run(main())


def t_perf_vs_pymysql():
    import time
    print("[performance vs pymysql]")
    async def main():
        pool = await rsqlx.connect(MY_URL, max_connections=8)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_my_perf")
        await pool.execute("CREATE TABLE rsqlx_my_perf (id INT AUTO_INCREMENT PRIMARY KEY, v INT)")
        await pool.execute_many("INSERT INTO rsqlx_my_perf (v) VALUES (?)", [[i] for i in range(1, 1001)])

        # rsqlx
        for _ in range(50):
            await pool.fetch_one("SELECT v FROM rsqlx_my_perf WHERE v=?", [1])
        t0 = time.perf_counter()
        for _ in range(500):
            await pool.fetch_one("SELECT v FROM rsqlx_my_perf WHERE v=?", [1])
        rsqlx_t = time.perf_counter() - t0

        # pymysql (sync)
        c = my_conn(); cur = c.cursor()
        for _ in range(50):
            cur.execute("SELECT v FROM rsqlx_my_perf WHERE v=%s", (1,))
            cur.fetchone()
        t0 = time.perf_counter()
        for _ in range(500):
            cur.execute("SELECT v FROM rsqlx_my_perf WHERE v=%s", (1,))
            cur.fetchone()
        pymysql_t = time.perf_counter() - t0
        c.close()
        ratio = pymysql_t / rsqlx_t
        check(f"rsqlx within 3x of pymysql (rsqlx={rsqlx_t*1000:.0f}ms vs pymysql={pymysql_t*1000:.0f}ms, {ratio:.2f}x)",
              rsqlx_t < pymysql_t * 3, f"{ratio:.2f}x")
        await pool.close()
    run(main())


if __name__ == "__main__":
    t_basic_crud()
    t_types()
    t_null()
    t_transactions()
    t_execute_many()
    t_concurrency()
    t_errors()
    t_migrate()
    t_perf_vs_pymysql()
    print(f"\n=== MySQL: {passed} passed, {failed} failed ===")
    sys.exit(1 if failed else 0)
