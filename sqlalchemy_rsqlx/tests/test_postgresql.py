"""PostgreSQL dialect tests.  Live tests require RSQLX_TEST_PG_URL."""

import asyncio
import datetime as dt
import decimal
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from sqlalchemy_rsqlx.base import build_connect_args

PG_URL = os.getenv("RSQLX_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="RSQLX_TEST_PG_URL not set")


def run(coro):
    return asyncio.run(coro)


def test_dialect_registered():
    dialect = registry.load("postgresql.rsqlx")
    assert dialect.__name__ == "PGDialect_rsqlx"
    assert dialect.paramstyle == "numeric_dollar"


def test_dsn_translation():
    url = make_url("postgresql+rsqlx://u:p@h:5432/db?sslmode=require")
    args, opts = build_connect_args(url)
    assert args == ["postgres://u:p@h:5432/db?sslmode=require"]
    assert opts["max_connections"] == 1


def test_placeholder_translation():
    dialect = registry.load("postgresql.rsqlx")()
    compiled = text("SELECT :a AS a, :b AS b").compile(dialect=dialect)
    assert str(compiled) == "SELECT $1 AS a, $2 AS b"


@needs_pg
def test_crud_types_and_transactions():
    async def main():
        engine = create_async_engine(PG_URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS rsqlx_dialect_t"))
            await conn.execute(
                text(
                    "CREATE TABLE rsqlx_dialect_t ("
                    "id SERIAL PRIMARY KEY, name TEXT, tags TEXT[], meta JSONB)"
                )
            )
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO rsqlx_dialect_t (name, tags, meta) VALUES (:n, :t, :m)"),
                {"n": "alice", "t": ["a", "b"], "m": {"k": 1}},
            )
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT name, tags, meta FROM rsqlx_dialect_t"))).one()
            assert row.name == "alice"
            assert row.tags == ["a", "b"]
            assert row.meta == {"k": 1}
            # 输出格式专项：属性访问 + 序列访问 + 映射访问一致
            assert row[0] == "alice" and row._mapping["name"] == "alice"
            assert set(row._mapping) == {"name", "tags", "meta"}
        await engine.dispose()

    run(main())


@needs_pg
def test_transaction_rollback_and_batch_and_types():
    async def main():
        engine = create_async_engine(PG_URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS rsqlx_dialect_types"))
            await conn.execute(
                text(
                    "CREATE TABLE rsqlx_dialect_types ("
                    "id SERIAL PRIMARY KEY, uid UUID UNIQUE, amount NUMERIC(12,2), "
                    "payload JSONB, blob BYTEA, created TIMESTAMP)"
                )
            )

        # 事务提交 + 批量插入
        uid = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO rsqlx_dialect_types "
                    "(uid, amount, payload, blob, created) VALUES "
                    "(:u, :a, :p, :b, :c)"
                ),
                [
                    {"u": uuid.uuid4(), "a": decimal.Decimal("12.34"), "p": {"k": i},
                     "b": b"abc", "c": dt.datetime(2026, 1, 2, 3, 4, 5)}
                    for i in range(3)
                ],
            )

        # 事务回滚
        with pytest.raises(RuntimeError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO rsqlx_dialect_types (uid, amount) VALUES (:u, :a)"),
                    {"u": uuid.uuid4(), "a": decimal.Decimal("99.99")},
                )
                raise RuntimeError("force rollback")

        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM rsqlx_dialect_types ORDER BY id"))).all()
            assert len(rows) == 3
            assert isinstance(rows[0].uid, uuid.UUID)
            assert isinstance(rows[0].amount, decimal.Decimal)
            assert rows[0].payload == {"k": 0}
            assert rows[0].blob == b"abc"
            assert rows[0].created == dt.datetime(2026, 1, 2, 3, 4, 5)

        # 异常映射：复用已插入的 uid 触发唯一约束冲突
        async with engine.connect() as conn:
            existing_uid = (await conn.execute(text("SELECT uid FROM rsqlx_dialect_types LIMIT 1"))).scalar()
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO rsqlx_dialect_types (uid, amount) VALUES (:u, :a)"),
                    {"u": existing_uid, "a": decimal.Decimal("1.00")},
                )
        with pytest.raises(ProgrammingError):
            async with engine.begin() as conn:
                await conn.execute(text("SELECT * FROM no_such_table_pg"))

        await engine.dispose()

    run(main())
