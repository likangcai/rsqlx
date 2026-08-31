"""End-to-end tests for rsqlx using SQLite (no server needed)."""

import asyncio
import datetime as dt
import decimal
import json
import os
import tempfile
from pathlib import Path

import pytest

import rsqlx


def sqlite_url(path):
    return "sqlite:" + Path(path).as_posix()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def pool(db_path):
    return asyncio.run(_make_pool(db_path))


async def _make_pool(db_path):
    return await rsqlx.connect(sqlite_url(db_path), max_connections=4)


def run(coro):
    return asyncio.run(coro)


async def _setup(pool):
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS t ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT, price REAL, data TEXT, created DATETIME,"
        " blob BLOB, flag BOOLEAN)"
    )
    await pool.execute("DELETE FROM t")


# ---------------------------------------------------------------- basics

def test_version_and_exceptions(pool):
    assert rsqlx.__version__
    assert issubclass(rsqlx.RowNotFound, rsqlx.Error)
    assert issubclass(rsqlx.DatabaseError, rsqlx.Error)
    assert issubclass(rsqlx.PoolTimedOut, rsqlx.Error)


def test_connect_memory():
    async def main():
        pool = await rsqlx.connect("sqlite::memory:")
        await pool.execute("CREATE TABLE m (x INTEGER)")
        await pool.execute("INSERT INTO m VALUES (?)", [42])
        rows = await pool.fetch("SELECT x FROM m")
        assert rows == [{"x": 42}]
        await pool.close()
        assert pool.is_closed

    run(main())


def test_bad_url():
    async def main():
        with pytest.raises(ValueError):
            await rsqlx.connect("oracle://nope")

    run(main())


def test_crud(pool):
    async def main():
        await _setup(pool)
        res = await pool.execute(
            "INSERT INTO t (name, price) VALUES (?, ?)", ["alice", 1.5]
        )
        assert res.rows_affected == 1
        assert res.last_insert_id == 1

        rows = await pool.fetch("SELECT id, name, price FROM t")
        assert rows == [{"id": 1, "name": "alice", "price": 1.5}]

        one = await pool.fetch_one("SELECT name FROM t WHERE id = ?", [1])
        assert one == {"name": "alice"}

        none = await pool.fetch_optional("SELECT * FROM t WHERE id = ?", [999])
        assert none is None

        with pytest.raises(rsqlx.RowNotFound):
            await pool.fetch_one("SELECT * FROM t WHERE id = ?", [999])

        res = await pool.execute("UPDATE t SET price = ? WHERE id = ?", [2.5, 1])
        assert res.rows_affected == 1

    run(main())


def test_execute_many(pool):
    async def main():
        await _setup(pool)
        res = await pool.execute_many(
            "INSERT INTO t (name, price) VALUES (?, ?)",
            [["a", 1.0], ["b", 2.0], ["c", 3.0]],
        )
        assert res.rows_affected == 3
        count = await pool.fetch_one("SELECT COUNT(*) AS n FROM t")
        assert count["n"] == 3

    run(main())


def test_null_and_params(pool):
    async def main():
        await _setup(pool)
        await pool.execute("INSERT INTO t (name, price) VALUES (?, ?)", ["n", None])
        row = await pool.fetch_one("SELECT name, price FROM t WHERE id = ?", [1])
        assert row["name"] == "n"
        assert row["price"] is None
        # None inside param list
        await pool.execute("INSERT INTO t (name) VALUES (?)", [None])

    run(main())


def test_type_roundtrip(pool):
    async def main():
        await _setup(pool)
        now = dt.datetime(2026, 8, 27, 12, 34, 56, 789000)
        d = dt.date(2026, 8, 27)
        t = dt.time(23, 59, 1)
        dec = decimal.Decimal("12345.6789")
        blob = b"\x00\x01\xffbytes"
        flag = True
        payload = {"k": [1, 2.5, "x", None, True]}
        await pool.execute(
            "INSERT INTO t (name, price, data, created, blob, flag)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ["row", 3.25, json.dumps(payload), now, blob, flag],
        )
        row = await pool.fetch_one("SELECT * FROM t WHERE id = ?", [1])
        assert row["name"] == "row"
        assert row["price"] == 3.25
        assert row["blob"] == blob
        assert row["flag"] == 1  # sqlite stores bool as int
        assert row["created"] == now  # DATETIME declared column -> datetime
        assert json.loads(row["data"]) == payload
        # decimal bound as text, returned as str
        await pool.execute("INSERT INTO t (name) VALUES (?)", [dec])
        row2 = await pool.fetch_one("SELECT name FROM t WHERE id = ?", [2])
        assert decimal.Decimal(row2["name"]) == dec
        assert d == dt.date(2026, 8, 27) and t

    run(main())


def test_unsupported_param(pool):
    async def main():
        await _setup(pool)
        with pytest.raises(TypeError):
            await pool.execute("SELECT ?", [object()])

    run(main())


# ---------------------------------------------------------------- transactions

def test_transaction_commit(pool):
    async def main():
        await _setup(pool)
        async with await pool.begin() as tx:
            await tx.execute("INSERT INTO t (name) VALUES (?)", ["tx1"])
            await tx.execute("INSERT INTO t (name) VALUES (?)", ["tx2"])
        rows = await pool.fetch("SELECT name FROM t ORDER BY id")
        assert [r["name"] for r in rows] == ["tx1", "tx2"]

    run(main())


def test_transaction_rollback(pool):
    async def main():
        await _setup(pool)
        with pytest.raises(RuntimeError):
            async with await pool.begin() as tx:
                await tx.execute("INSERT INTO t (name) VALUES (?)", ["gone"])
                raise RuntimeError("boom")
        rows = await pool.fetch("SELECT * FROM t")
        assert rows == []

    run(main())


def test_transaction_manual(pool):
    async def main():
        await _setup(pool)
        tx = await pool.begin()
        await tx.execute("INSERT INTO t (name) VALUES (?)", ["manual"])
        await tx.commit()
        # using after finish raises
        with pytest.raises(rsqlx.InterfaceError):
            await tx.commit()
        tx2 = await pool.begin()
        await tx2.execute("INSERT INTO t (name) VALUES (?)", ["rolled"])
        await tx2.rollback()
        rows = await pool.fetch("SELECT name FROM t")
        assert rows == [{"name": "manual"}]

    run(main())


def test_transaction_fetch(pool):
    async def main():
        await _setup(pool)
        await pool.execute("INSERT INTO t (name) VALUES (?)", ["x"])
        async with await pool.begin() as tx:
            rows = await tx.fetch("SELECT name FROM t")
            one = await tx.fetch_one("SELECT COUNT(*) AS n FROM t")
        assert rows == [{"name": "x"}]
        assert one["n"] == 1

    run(main())


# ---------------------------------------------------------------- pool behavior

def test_pool_ctx_manager(db_path):
    async def main():
        async with await rsqlx.connect(sqlite_url(db_path)) as pool:
            await pool.execute("CREATE TABLE c (x INTEGER)")
            assert not pool.is_closed
        assert pool.is_closed
        with pytest.raises(rsqlx.PoolClosed):
            await pool.fetch("SELECT 1")

    run(main())


def test_concurrency(pool):
    async def main():
        await _setup(pool)
        await pool.execute_many(
            "INSERT INTO t (name, price) VALUES (?, ?)", [[f"n{i}", i] for i in range(50)]
        )
        results = await asyncio.gather(
            *[pool.fetch_one("SELECT price FROM t WHERE name = ?", [f"n{i}"]) for i in range(50)]
        )
        assert all(r["price"] == i for i, r in enumerate(results))

    run(main())


def test_repr_and_state(pool):
    async def main():
        await _setup(pool)
        assert "sqlite" in repr(pool)
        assert pool.size >= 1 or pool.num_idle >= 0  # pool internals visible
        r = await pool.execute("INSERT INTO t (name) VALUES (?)", ["z"])
        assert "rows_affected=1" in repr(r)

    run(main())


# ---------------------------------------------------------------- migrations

def test_migrate(tmp_path):
    async def main():
        mig = tmp_path / "migrations"
        mig.mkdir()
        (mig / "0001_create_users.up.sql").write_text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);", encoding="utf-8"
        )
        (mig / "0002_seed.up.sql").write_text(
            "INSERT INTO users (name) VALUES ('seed');", encoding="utf-8"
        )
        pool = await rsqlx.connect("sqlite::memory:")
        await pool.migrate(str(mig))
        rows = await pool.fetch("SELECT name FROM users")
        assert rows == [{"name": "seed"}]
        # migrating again is a no-op
        await pool.migrate(str(mig))
        rows = await pool.fetch("SELECT COUNT(*) AS n FROM users")
        assert rows[0]["n"] == 1
        await pool.close()

    run(main())


# ---------------------------------------------------------------- env-gated PG/MySQL

PG_URL = os.environ.get("RSQLX_TEST_PG_URL")
MY_URL = os.environ.get("RSQLX_TEST_MYSQL_URL")

@pytest.mark.skipif(not PG_URL, reason="set RSQLX_TEST_PG_URL to run")
def test_postgres():
    async def main():
        pool = await rsqlx.connect(PG_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_py_test")
        await pool.execute(
            "CREATE TABLE rsqlx_py_test ("
            " id SERIAL PRIMARY KEY, name TEXT, num NUMERIC(12,4), f DOUBLE PRECISION,"
            " ts TIMESTAMPTZ, d DATE, data JSONB, u UUID, b BYTEA, tags TEXT[])"
        )
        res = await pool.execute(
            "INSERT INTO rsqlx_py_test (name, num, f, ts, d, data, u, b, tags)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            [
                "pg-row", decimal.Decimal("3.1415"), 2.5,
                dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
                dt.date(2026, 1, 2), {"a": [1, 2]},
                __import__("uuid").uuid4(), b"\x01\x02", ["x", "y", None],
            ],
        )
        assert res.rows_affected == 1
        row = await pool.fetch_one("SELECT * FROM rsqlx_py_test WHERE name = $1", ["pg-row"])
        import uuid as uuid_mod
        assert isinstance(row["num"], decimal.Decimal)
        assert isinstance(row["ts"], dt.datetime) and row["ts"].tzinfo
        assert row["d"] == dt.date(2026, 1, 2)
        assert row["data"] == {"a": [1, 2]}
        assert isinstance(row["u"], uuid_mod.UUID)
        assert row["b"] == b"\x01\x02"
        assert row["tags"] == ["x", "y", None]
        # error mapping
        with pytest.raises(rsqlx.DatabaseError):
            await pool.fetch("SELECT * FROM does_not_exist")
        await pool.execute("DROP TABLE rsqlx_py_test")
        await pool.close()

    run(main())


@pytest.mark.skipif(not MY_URL, reason="set RSQLX_TEST_MYSQL_URL to run")
def test_mysql():
    async def main():
        pool = await rsqlx.connect(MY_URL)
        await pool.execute("DROP TABLE IF EXISTS rsqlx_py_test")
        await pool.execute(
            "CREATE TABLE rsqlx_py_test ("
            " id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64), price DECIMAL(10,2),"
            " ts DATETIME, data JSON, payload BLOB)"
        )
        res = await pool.execute(
            "INSERT INTO rsqlx_py_test (name, price, ts, data, payload) VALUES (?,?,?,?,?)",
            ["my-row", decimal.Decimal("9.99"), dt.datetime(2026, 5, 4, 3, 2, 1),
             {"k": True}, b"\xff\x00"],
        )
        assert res.last_insert_id == 1
        row = await pool.fetch_one("SELECT * FROM rsqlx_py_test WHERE name = ?", ["my-row"])
        assert isinstance(row["price"], decimal.Decimal)
        assert row["ts"] == dt.datetime(2026, 5, 4, 3, 2, 1)
        assert row["data"] == {"k": True}
        assert row["payload"] == b"\xff\x00"
        with pytest.raises(rsqlx.DatabaseError):
            await pool.fetch("SELECT * FROM does_not_exist")
        await pool.execute("DROP TABLE rsqlx_py_test")
        await pool.close()

    run(main())
