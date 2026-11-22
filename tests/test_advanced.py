"""Advanced driver features: isolation levels, COPY, LISTEN/NOTIFY, cursors, ping."""

import asyncio
import os

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


PG = native(PG_URL)
MY = native(MY_URL)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- isolation levels
@needs_pg
def test_pg_isolation_levels():
    async def main():
        p = await rsqlx.connect(PG, max_connections=4)
        await p.execute("DROP TABLE IF EXISTS adv_iso")
        await p.execute("CREATE TABLE adv_iso (id INT, v TEXT)")

        tx = await p.begin_with("BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY")
        try:
            with pytest.raises(rsqlx.DatabaseError):
                await tx.execute("INSERT INTO adv_iso VALUES (1, 'x')")
        finally:
            await tx.rollback()

        tx2 = await p.begin_with("BEGIN ISOLATION LEVEL READ COMMITTED")
        await tx2.execute("INSERT INTO adv_iso VALUES (2, 'ok')")
        await tx2.commit()
        assert (await p.fetch_one("SELECT COUNT(*) AS n FROM adv_iso"))["n"] == 1
        await p.close()

    run(main())


@needs_mysql
def test_mysql_isolation_level():
    async def main():
        p = await rsqlx.connect(MY, max_connections=4)
        await p.execute("DROP TABLE IF EXISTS adv_iso_my")
        await p.execute("CREATE TABLE adv_iso_my (id INT, v TEXT)")
        # MySQL: SET SESSION must precede START TRANSACTION (server limitation)
        await p.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        tx = await p.begin()
        await tx.execute("INSERT INTO adv_iso_my VALUES (1, 'ok')")
        await tx.commit()
        assert (await p.fetch_one("SELECT COUNT(*) AS n FROM adv_iso_my"))["n"] == 1
        await p.close()

    run(main())


# ---------------------------------------------------------------- COPY
@needs_pg
def test_pg_copy_in_out():
    async def main():
        p = await rsqlx.connect(PG, max_connections=4)
        await p.execute("DROP TABLE IF EXISTS adv_copy")
        await p.execute("CREATE TABLE adv_copy (id INT, name TEXT)")

        # COPY FROM STDIN (CSV format, the default)
        csv = b"1,alice\n2,bob\n3,carol\n"
        n = await p.copy_in("COPY adv_copy FROM STDIN WITH (FORMAT csv)", csv)
        assert n == 3
        assert (await p.fetch_one("SELECT COUNT(*) AS n FROM adv_copy"))["n"] == 3

        # COPY TO STDOUT returns raw byte chunks
        chunks = await p.copy_out("COPY adv_copy TO STDOUT WITH (FORMAT csv)")
        assert isinstance(chunks, list) and all(isinstance(c, bytes) for c in chunks)
        out = b"".join(chunks).decode()
        assert "alice" in out and "bob" in out and "carol" in out

        # Non-PG pools reject COPY
        await p.close()

    run(main())


def test_copy_not_supported_on_sqlite():
    async def main():
        p = await rsqlx.connect("sqlite::memory:")
        with pytest.raises(NotImplementedError):
            await p.copy_in("COPY t FROM STDIN", b"")
        with pytest.raises(NotImplementedError):
            await p.copy_out("COPY t TO STDOUT")
        await p.close()

    run(main())


# ---------------------------------------------------------------- LISTEN/NOTIFY
@needs_pg
def test_pg_listen_notify():
    async def main():
        listener_pool = await rsqlx.connect(PG, max_connections=2)
        sender = await rsqlx.connect(PG, max_connections=2)

        listener = await listener_pool.listen()
        await listener.listen("rsqlx_test_chan")

        # Use pg_notify to avoid NOTIFY's identifier parsing
        await sender.execute(
            "SELECT pg_notify('rsqlx_test_chan', 'hello world')"
        )
        channel, payload, pid = await asyncio.wait_for(listener.recv(), timeout=5)
        assert channel == "rsqlx_test_chan"
        assert payload == "hello world"
        assert isinstance(pid, int) and pid > 0

        await listener.close()
        await sender.close()
        await listener_pool.close()

    run(main())


# ---------------------------------------------------------------- server-side cursor
@needs_pg
def test_pg_server_cursor():
    async def main():
        p = await rsqlx.connect(PG, max_connections=4)
        await p.execute("DROP TABLE IF EXISTS adv_cursor")
        await p.execute("CREATE TABLE adv_cursor (id INT, v TEXT)")
        await p.execute_many(
            "INSERT INTO adv_cursor VALUES ($1, $2)",
            [[i, f"v{i}"] for i in range(25)],
        )

        cur = await p.cursor("SELECT id, v FROM adv_cursor ORDER BY id", batch_size=7)
        seen = []
        while True:
            rows = await cur.fetch_batch()
            if not rows:
                break
            seen.extend(r["id"] for r in rows)
        await cur.close()
        assert seen == list(range(25))
        await p.close()

    run(main())


# ---------------------------------------------------------------- ping
def test_ping_all_backends():
    async def main():
        p = await rsqlx.connect("sqlite::memory:")
        await p.ping()
        await p.close()
        assert p.is_closed

    run(main())


@needs_pg
def test_ping_pg():
    async def main():
        p = await rsqlx.connect(PG, max_connections=2)
        await p.ping()
        await p.close()

    run(main())
