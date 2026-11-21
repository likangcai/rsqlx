"""SQLite dialect minimal loop: CRUD, transactions, placeholders, output."""

import asyncio
import datetime as dt
import decimal

import pytest
from sqlalchemy import Column, Integer, String, Table, MetaData, text
from sqlalchemy.dialects import registry
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

URL = "sqlite+rsqlx:///:memory:"


def run(coro):
    return asyncio.run(coro)


def test_dialect_registered():
    dialect = registry.load("sqlite.rsqlx")
    assert dialect.__name__ == "SQLiteDialect_rsqlx"
    assert dialect.paramstyle == "qmark"


def test_crud_and_row_access():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
            )
            await conn.execute(
                text("INSERT INTO t (name, age) VALUES (:n, :a)"),
                [{"n": "alice", "a": 30}, {"n": "bob", "a": 20}],
            )

        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id, name, age FROM t ORDER BY id"))).all()
            assert [tuple(r) for r in rows] == [(1, "alice", 30), (2, "bob", 20)]
            assert rows[0].name == "alice"
            assert rows[0][1] == "alice"
            assert set(rows[0]._mapping.keys()) == {"id", "name", "age"}

            one = (await conn.execute(text("SELECT name FROM t WHERE age = :a"), {"a": 20})).scalar()
            assert one == "bob"

        await engine.dispose()

    run(main())


def test_transaction_commit_and_rollback():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))

        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": "committed"})

        with pytest.raises(RuntimeError):
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": "rolled"})
                raise RuntimeError("boom")

        async with engine.connect() as conn:
            values = (await conn.execute(text("SELECT v FROM t"))).scalars().all()
            assert values == ["committed"]

        await engine.dispose()

    run(main())


def test_executemany():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"))
            await conn.execute(
                text("INSERT INTO t (v) VALUES (:v)"),
                [{"v": i} for i in range(20)],
            )
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM t"))).scalar()
            assert count == 20
        await engine.dispose()

    run(main())


def test_placeholder_translation():
    stmt = text("SELECT :a AS a, :b AS b")
    compiled = stmt.compile(dialect=registry.load("sqlite.rsqlx")())
    assert str(compiled) == "SELECT ? AS a, ? AS b"


def test_type_roundtrip():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE t (d TEXT, n TEXT, ts DATETIME)")
            )
            await conn.execute(
                text("INSERT INTO t (d, n, ts) VALUES (:d, :n, :ts)"),
                {"d": "1.25", "n": "1", "ts": dt.datetime(2026, 1, 2, 3, 4, 5)},
            )
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT d, n, ts FROM t"))).one()
            assert row.d == "1.25"
            assert row.ts == dt.datetime(2026, 1, 2, 3, 4, 5)
        await engine.dispose()

    run(main())


def test_empty_result_description():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER, name TEXT)"))
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT id, name FROM t"))
            assert result.all() == []
            assert list(result.keys()) == ["id", "name"]
        await engine.dispose()

    run(main())


def test_exception_mapping():
    async def main():
        engine = create_async_engine(URL)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, u TEXT UNIQUE)"))
            await conn.execute(text("INSERT INTO t (u) VALUES ('x')"))
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO t (u) VALUES ('x')"))
        with pytest.raises(ProgrammingError):
            async with engine.begin() as conn:
                await conn.execute(text("SELECT * FROM no_such_table"))
        await engine.dispose()

    run(main())
