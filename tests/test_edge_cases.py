"""Deep edge-case coverage aligned with upstream sqlx test dimensions.

Covers: PG extended arrays and unsupported-type error paths, MySQL type edge
cases (BIT/ENUM/SET/GEOMETRY/zero dates), fine-grained constraint errors,
migration semantics and pool extreme parameters.

Live PG/MySQL tests require RSQLX_TEST_PG_URL / RSQLX_TEST_MYSQL_URL.
"""

import asyncio
import datetime as dt
import decimal
import os
import tempfile
import uuid

import pytest

import rsqlx

PG_URL = os.environ.get("RSQLX_TEST_PG_URL")
MY_URL = os.environ.get("RSQLX_TEST_MYSQL_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="RSQLX_TEST_PG_URL not set")
needs_mysql = pytest.mark.skipif(not MY_URL, reason="RSQLX_TEST_MYSQL_URL not set")


def native(url):
    if not url:
        return url
    return url.replace("postgresql+rsqlx://", "postgres://", 1).replace(
        "mysql+rsqlx://", "mysql://", 1
    )


PG_DSN = native(PG_URL)
MY_DSN = native(MY_URL)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- PG extended arrays
@needs_pg
def test_pg_extended_arrays():
    async def main():
        p = await rsqlx.connect(PG_DSN, max_connections=2)
        await p.execute("DROP TABLE IF EXISTS edge_pg_arrays")
        await p.execute(
            "CREATE TABLE edge_pg_arrays ("
            "uids UUID[], days DATE[], times TIME[], ts TIMESTAMP[], "
            "tstz TIMESTAMPTZ[], nums NUMERIC[], bytes BYTEA[])"
        )
        uid1, uid2 = uuid.uuid4(), uuid.uuid4()
        await p.execute(
            "INSERT INTO edge_pg_arrays VALUES ($1,$2,$3,$4,$5,$6,$7)",
            [
                [uid1, None, uid2],
                [dt.date(2026, 1, 1), None],
                [dt.time(1, 2, 3)],
                [dt.datetime(2026, 1, 2, 3, 4, 5)],
                [dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)],
                [decimal.Decimal("1.5"), None],
                [b"\x01\x02", None],
            ],
        )
        row = await p.fetch_one("SELECT * FROM edge_pg_arrays")
        assert row["uids"] == [uid1, None, uid2]
        assert row["days"] == [dt.date(2026, 1, 1), None]
        assert row["times"] == [dt.time(1, 2, 3)]
        assert row["ts"] == [dt.datetime(2026, 1, 2, 3, 4, 5)]
        assert row["tstz"][0].tzinfo is not None
        assert row["nums"] == [decimal.Decimal("1.5"), None]
        assert row["bytes"] == [b"\x01\x02", None]

        # JSON/JSONB arrays can be read (decoder supports them) even though a
        # plain Python list of dicts binds as one jsonb value; use the explicit
        # JsonArray wrapper (test_pg_json_array_binding) to write JSONB[].
        jrow = await p.fetch_one(
            "SELECT ARRAY['{\"a\":1}'::jsonb, NULL] AS jbs, "
            "ARRAY['{\"b\":2}'::json] AS js"
        )
        assert jrow["jbs"] == [{"a": 1}, None]
        assert jrow["js"] == [{"b": 2}]
        await p.close()

    run(main())


@needs_pg
def test_pg_json_array_binding():
    """JsonArray binds as JSONB[]; default list[dict] stays a single jsonb value."""
    async def main():
        p = await rsqlx.connect(PG_DSN, max_connections=2)
        await p.execute("DROP TABLE IF EXISTS edge_pg_json")
        await p.execute("CREATE TABLE edge_pg_json (id INT, arr JSONB[], single JSONB)")

        # Default behaviour is unchanged: list[dict] -> one jsonb value.
        await p.execute(
            "INSERT INTO edge_pg_json (id, single) VALUES ($1, $2)",
            [1, [{"a": 1}, {"b": 2}]],
        )
        assert (await p.fetch_one("SELECT single FROM edge_pg_json WHERE id=1"))["single"] == [
            {"a": 1},
            {"b": 2},
        ]

        # Explicit JsonArray -> native JSONB[] array.
        assert repr(rsqlx.JsonArray([{"a": 1}])) == "JsonArray(1 items)"
        await p.execute(
            "INSERT INTO edge_pg_json (id, arr) VALUES ($1, $2)",
            [2, rsqlx.JsonArray([{"a": 1}, {"b": 2}, None])],
        )
        assert (await p.fetch_one("SELECT arr FROM edge_pg_json WHERE id=2"))["arr"] == [
            {"a": 1},
            {"b": 2},
            None,
        ]

        # Nested JSON values inside the array are preserved.
        await p.execute(
            "INSERT INTO edge_pg_json (id, arr) VALUES ($1, $2)",
            [3, rsqlx.JsonArray([{"nested": {"x": 1}}, [1, 2, 3]])],
        )
        assert (await p.fetch_one("SELECT arr FROM edge_pg_json WHERE id=3"))["arr"] == [
            {"nested": {"x": 1}},
            [1, 2, 3],
        ]
        await p.close()

    run(main())


@needs_pg
def test_pg_unsupported_types_error():
    async def main():
        p = await rsqlx.connect(PG_DSN, max_connections=2)
        cases = [
            ("SELECT '1.2.3.4'::inet AS v", "INET"),
            ("SELECT interval '1 day' AS v", "INTERVAL"),
            ("SELECT int4range(1, 5) AS v", "INT4RANGE"),
            ("SELECT point(1, 2) AS v", "POINT"),
            ("SELECT 12.34::money AS v", "MONEY"),
        ]
        for sql, label in cases:
            with pytest.raises(rsqlx.InterfaceError):
                await p.fetch_one(sql)
        await p.close()

    run(main())


@needs_pg
def test_pg_constraint_errors():
    async def main():
        p = await rsqlx.connect(PG_DSN, max_connections=2)
        await p.execute("DROP TABLE IF EXISTS edge_pg_constraints")
        await p.execute("DROP TABLE IF EXISTS edge_pg_parent")
        await p.execute("CREATE TABLE edge_pg_parent (id INT PRIMARY KEY)")
        await p.execute(
            "CREATE TABLE edge_pg_constraints ("
            "id INT PRIMARY KEY, parent_id INT REFERENCES edge_pg_parent(id), "
            "name TEXT NOT NULL, score INT CHECK (score >= 0))"
        )
        await p.execute("INSERT INTO edge_pg_parent VALUES (1)")

        # NOT NULL
        with pytest.raises(rsqlx.DatabaseError):
            await p.execute("INSERT INTO edge_pg_constraints (id, score) VALUES (1, 1)")
        # CHECK
        with pytest.raises(rsqlx.DatabaseError):
            await p.execute(
                "INSERT INTO edge_pg_constraints (id, name, score) VALUES (2, 'x', -1)"
            )
        # FK
        with pytest.raises(rsqlx.DatabaseError):
            await p.execute(
                "INSERT INTO edge_pg_constraints (id, name, parent_id) VALUES (3, 'x', 999)"
            )
        # parameter count mismatch -> server-side protocol error (08P01)
        with pytest.raises(rsqlx.DatabaseError):
            await p.execute(
                "INSERT INTO edge_pg_constraints (id, name, score) VALUES ($1, $2, $3)", [1]
            )
        await p.close()

    run(main())


# ---------------------------------------------------------------- MySQL edge types
@needs_mysql
def test_mysql_edge_types():
    async def main():
        p = await rsqlx.connect(MY_DSN, max_connections=1)
        # MySQL 8 strict mode rejects zero dates by default. Verify rsqlx
        # surfaces that as a DatabaseError rather than crashing.
        await p.execute("DROP TABLE IF EXISTS edge_my_types")
        await p.execute(
            "CREATE TABLE edge_my_types ("
            "id INT PRIMARY KEY, bit_col BIT(8), enum_col ENUM('a','b'), "
            "set_col SET('x','y'), zdate DATE, ztime TIME)"
        )
        # Insert a valid row; zero dates are verified below under strict mode.
        await p.execute(
            "INSERT INTO edge_my_types VALUES (1, b'10101010', 'a', 'x,y', "
            "'2026-01-02', '00:00:00')"
        )
        row = await p.fetch_one("SELECT * FROM edge_my_types")
        assert row["bit_col"] == 0xAA  # sqlx decodes MySQL BIT as an unsigned integer
        assert row["enum_col"] == "a"
        assert row["set_col"] == "x,y"
        assert isinstance(row["zdate"], dt.date)
        assert isinstance(row["ztime"], dt.time)

        with pytest.raises(rsqlx.DatabaseError):
            await p.execute(
                "INSERT INTO edge_my_types VALUES (2, b'1', 'a', 'x', '0000-00-00', '00:00:00')"
            )

        # GEOMETRY has no sqlx decoder -> clear unsupported-type error.
        await p.execute("DROP TABLE IF EXISTS edge_my_geom")
        await p.execute("CREATE TABLE edge_my_geom (g GEOMETRY)")
        await p.execute("INSERT INTO edge_my_geom VALUES (ST_GeomFromText('POINT(1 2)'))")
        with pytest.raises(rsqlx.InterfaceError, match="unsupported MySQL type"):
            await p.fetch_one("SELECT g FROM edge_my_geom")
        await p.execute("DROP TABLE IF EXISTS edge_my_geom")
        await p.close()

    run(main())


@needs_mysql
def test_mysql_constraint_errors():
    async def main():
        p = await rsqlx.connect(MY_DSN, max_connections=2, acquire_timeout=5)
        await p.execute("DROP TABLE IF EXISTS edge_my_constraints")
        await p.execute(
            "CREATE TABLE edge_my_constraints ("
            "id INT PRIMARY KEY, name TEXT NOT NULL, score INT)"
        )
        with pytest.raises(rsqlx.DatabaseError):
            await p.execute("INSERT INTO edge_my_constraints (id, score) VALUES (1, 1)")
        # Parameter-count mismatch raises InterfaceError. NOTE: on MySQL this
        # leaves one pooled connection busy (sqlx prepare-stage behavior), so
        # use a second connection for cleanup and do not call close() on the
        # affected pool — see README "Known limitations".
        with pytest.raises(rsqlx.InterfaceError):
            await p.execute("INSERT INTO edge_my_constraints VALUES (?, ?, ?)", [1])
        assert p.num_idle <= 1

    run(main())


# ---------------------------------------------------------------- migration semantics
@needs_pg
def test_pg_migration_semantics():
    async def main():
        p = await rsqlx.connect(PG_DSN, max_connections=2)
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "0001_up.sql"), "w") as f:
            f.write("CREATE TABLE IF NOT EXISTS edge_mig (id INT);\n")
        await p.execute("DROP TABLE IF EXISTS _sqlx_migrations")
        await p.execute("DROP TABLE IF EXISTS edge_mig")
        await p.migrate(d)
        await p.migrate(d)  # idempotent
        row = await p.fetch_one("SELECT COUNT(*) AS n FROM edge_mig")
        assert row["n"] == 0
        await p.close()

    run(main())


# ---------------------------------------------------------------- pool extremes
def test_pool_min_connections_and_acquire_timeout():
    async def main():
        p = await rsqlx.connect(
            "sqlite::memory:", min_connections=2, max_connections=4, acquire_timeout=1.0
        )
        assert p.size >= 2
        await p.close()
        assert p.is_closed

        with pytest.raises(ValueError):
            await rsqlx.connect("sqlite::memory:", acquire_timeout=-1)

    run(main())


def test_pool_acquire_timeout_exceeded():
    async def main():
        # Hold the only connection inside a transaction, then try to acquire
        # another one with a short timeout -> PoolTimedOut.
        p = await rsqlx.connect("sqlite::memory:", max_connections=1, acquire_timeout=0.2)
        tx = await p.begin()
        with pytest.raises(rsqlx.PoolTimedOut):
            await p.execute("CREATE TABLE t (id INT)")
        await tx.rollback()
        await p.close()

    run(main())
