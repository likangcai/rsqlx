"""Smoke benchmark: async queries must not block the asyncio event loop."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SLOW_SQL = text(
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 120000) "
    "SELECT count(*) FROM c"
)


def test_event_loop_not_blocked():
    async def main():
        engine = create_async_engine("sqlite+rsqlx:///:memory:")
        loop = asyncio.get_running_loop()
        ticks = []

        async def ticker():
            while True:
                ticks.append(loop.time())
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)

        async def one_query():
            async with engine.connect() as conn:
                return (await conn.execute(SLOW_SQL)).scalar()

        results = await asyncio.gather(*[one_query() for _ in range(6)])
        await asyncio.sleep(0.05)
        task.cancel()

        assert all(r == 120000 for r in results)
        assert len(ticks) > 5
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert max(gaps) < 0.25, f"event loop stalled for {max(gaps):.3f}s"

        await engine.dispose()

    asyncio.run(main())
