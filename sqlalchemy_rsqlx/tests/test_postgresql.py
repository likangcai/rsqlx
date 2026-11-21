"""PostgreSQL dialect tests.  Live tests require RSQLX_TEST_PG_URL."""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
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
        await engine.dispose()

    run(main())
