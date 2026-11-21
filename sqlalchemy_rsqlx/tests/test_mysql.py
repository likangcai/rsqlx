"""MySQL dialect tests.  Live tests require RSQLX_TEST_MYSQL_URL."""

import asyncio
import datetime as dt
import decimal
import os

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import registry
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
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


@needs_mysql
def test_transaction_rollback_and_types():
    async def main():
        engine = create_async_engine(MYSQL_URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS rsqlx_dialect_types"))
            await conn.execute(
                text(
                    "CREATE TABLE rsqlx_dialect_types ("
                    "id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64) UNIQUE, "
                    "amount DECIMAL(12,2), payload JSON, payload_blob BLOB, created DATETIME)"
                )
            )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO rsqlx_dialect_types "
                    "(name, amount, payload, payload_blob, created) VALUES "
                    "(:n, :a, :p, :b, :c)"
                ),
                {
                    "n": "alice", "a": decimal.Decimal("56.78"), "p": {"k": 1},
                    "b": b"xyz", "c": dt.datetime(2026, 2, 3, 4, 5, 6),
                },
            )

        with pytest.raises(RuntimeError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO rsqlx_dialect_types (name) VALUES (:n)"), {"n": "rolled"}
                )
                raise RuntimeError("force rollback")

        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM rsqlx_dialect_types"))).all()
            assert len(rows) == 1
            assert rows[0].name == "alice"
            assert isinstance(rows[0].amount, decimal.Decimal)
            assert rows[0].payload == {"k": 1}
            assert rows[0].payload_blob == b"xyz"
            assert rows[0].created == dt.datetime(2026, 2, 3, 4, 5, 6)
            assert rows[0][0] == rows[0].id

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO rsqlx_dialect_types (name) VALUES (:n)"), {"n": "alice"}
                )
        with pytest.raises(ProgrammingError):
            async with engine.begin() as conn:
                await conn.execute(text("SELECT * FROM no_such_table_mysql"))

        await engine.dispose()

    run(main())
