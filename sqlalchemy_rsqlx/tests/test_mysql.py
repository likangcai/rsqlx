"""MySQL dialect tests.  Live tests require RSQLX_TEST_MYSQL_URL."""

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sqlalchemy_rsqlx.base import build_connect_args

MYSQL_URL = os.getenv("RSQLX_TEST_MYSQL_URL")
needs_mysql = pytest.mark.skipif(not MYSQL_URL, reason="RSQLX_TEST_MYSQL_URL not set")


def run(coro):
    return asyncio.run(coro)


def test_dialect_registered():
    dialect = registry.load("mysql.rsqlx")
    assert dialect.__name__ == "MySQLDialect_rsqlx"
    assert dialect.paramstyle == "qmark"


def test_dsn_translation():
    url = make_url("mysql+rsqlx://u:p@h:3306/db?ssl-mode=disabled")
    args, opts = build_connect_args(url)
    assert args == ["mysql://u:p@h:3306/db?ssl-mode=disabled"]
    assert opts["max_connections"] == 1


def test_placeholder_translation():
    dialect = registry.load("mysql.rsqlx")()
    compiled = text("SELECT :a AS a, :b AS b").compile(dialect=dialect)
    assert str(compiled) == "SELECT ? AS a, ? AS b"


@needs_mysql
def test_crud_last_insert_id_and_batch():
    async def main():
        engine = create_async_engine(MYSQL_URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS rsqlx_dialect_t"))
            await conn.execute(
                text(
                    "CREATE TABLE rsqlx_dialect_t ("
                    "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64))"
                )
            )
        async with engine.begin() as conn:
            result = await conn.execute(
                text("INSERT INTO rsqlx_dialect_t (name) VALUES (:n)"), {"n": "alice"}
            )
            assert result.lastrowid is not None
            await conn.execute(
                text("INSERT INTO rsqlx_dialect_t (name) VALUES (:n)"),
                [{"n": "bob"}, {"n": "carol"}],
            )
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT COUNT(*) FROM rsqlx_dialect_t"))).scalar()
            assert count == 3
        await engine.dispose()

    run(main())
