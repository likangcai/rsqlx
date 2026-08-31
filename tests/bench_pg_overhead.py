"""
PostgreSQL 性能分层分析：rsqlx vs psycopg2

核心问题：rsqlx 比 psycopg2 慢吗？慢在哪里？

测量方法：
  t(query) = fixed_overhead + per_row_cost * rows

通过测量 1/10/100/1000 行的查询耗时，用最小二乘拟合拆出两个分量：
  - fixed_overhead: 每次查询的固定开销（协程创建、线程切换、GIL 释放/重获、协议往返）
  - per_row_cost:   每行的解码开销（字节 → Python 对象）

这样能定量回答「什么场景下慢、什么场景下快」。

运行:
    set RSQLX_TEST_PG_URL=postgres://postgres:pass@127.0.0.1:5432/postgres
    python tests/bench_pg_overhead.py
"""

import asyncio
import os
import statistics
import sys
import time

import psycopg2
import psycopg2.extras

import rsqlx

PG_URL = os.environ.get("RSQLX_TEST_PG_URL")
if not PG_URL:
    print("Set RSQLX_TEST_PG_URL to run this benchmark.")
    sys.exit(0)

# 解析 URL 给 psycopg2 用
from urllib.parse import urlparse
_p = urlparse(PG_URL)
PG_KW = dict(
    host=_p.hostname or "127.0.0.1",
    port=_p.port or 5432,
    user=_p.username or "postgres",
    password=_p.password or "",
    dbname=_p.path.lstrip("/") or "postgres",
)

ROW_COUNTS = [1, 10, 100, 1000]
REPEATS = 300
WARMUP = 30


def fmt(sec):
    """格式化为 us"""
    return f"{sec * 1e6:8.1f}us"


# ---------------------------------------------------------------- 基线
async def bench_asyncio_baseline():
    """纯 Python asyncio 调度开销（无数据库），作为基线参考"""
    async def empty_coro():
        return 1

    for _ in range(200):
        await empty_coro()
    t0 = time.perf_counter()
    for _ in range(2000):
        await empty_coro()
    per_call = (time.perf_counter() - t0) / 2000

    # gather 开销
    async def gather_n(n):
        await asyncio.gather(*[empty_coro() for _ in range(n)])

    for _ in range(50):
        await gather_n(100)
    t0 = time.perf_counter()
    for _ in range(200):
        await gather_n(100)
    per_gather100 = (time.perf_counter() - t0) / 200
    return per_call, per_gather100


# ---------------------------------------------------------------- rsqlx
async def bench_rsqlx(pool, sql, params, n=REPEATS, warmup=WARMUP):
    for _ in range(warmup):
        await pool.fetch(sql, params)
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        await pool.fetch(sql, params)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


# ---------------------------------------------------------------- psycopg2
def bench_psycopg2(cur, sql, params, n=REPEATS, warmup=WARMUP):
    for _ in range(warmup):
        cur.execute(sql, params)
        cur.fetchall()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        cur.execute(sql, params)
        cur.fetchall()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def fit_line(xs, ys):
    """最小二乘拟合 y = a + b*x，返回 (a=fixed, b=per_row)"""
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return ys[0], 0.0
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return a, b


async def main():
    pool = await rsqlx.connect(PG_URL, max_connections=8)

    # 准备数据
    await pool.execute("DROP TABLE IF EXISTS bench_rows")
    await pool.execute(
        "CREATE TABLE bench_rows ("
        "id SERIAL PRIMARY KEY, name TEXT, v INT, price NUMERIC(10,2), ts TIMESTAMP)"
    )
    await pool.execute(
        "INSERT INTO bench_rows (name, v, price, ts) "
        "SELECT 'name_' || g, g, (g % 1000) / 4.0, now() "
        "FROM generate_series(1, 2000) g"
    )

    conn = psycopg2.connect(**PG_KW)
    psycopg2.extras.register_default_json(conn_or_curs=conn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("=" * 74)
    print("PostgreSQL 性能分层分析: rsqlx vs psycopg2")
    print("=" * 74)

    # ---- 1. asyncio 基线 ----
    per_coro, per_gather100 = await bench_asyncio_baseline()
    print("\n[基线] 纯 Python asyncio 开销")
    print(f"  空协程 await 一次           : {fmt(per_coro)}")
    print(f"  gather(100 个空协程) 一次    : {fmt(per_gather100)}")
    print("  (这是 async 框架本身的固定成本，rsqlx 每次查询都要付)")

    # ---- 2. 不同行数的单次查询耗时 ----
    print("\n[测量] 单次查询耗时 vs 返回行数")
    print(f"  {'行数':>6}  {'rsqlx':>12}  {'psycopg2':>12}  {'rsqlx/psycopg2':>16}")
    print("  " + "-" * 52)

    rsqlx_times, psyco_times = [], []
    for rows in ROW_COUNTS:
        sql = f"SELECT id, name, v, price, ts FROM bench_rows ORDER BY id LIMIT {rows}"
        t_r = await bench_rsqlx(pool, sql, None)
        t_p = bench_psycopg2(cur, sql, None)
        rsqlx_times.append(t_r)
        psyco_times.append(t_p)
        ratio = t_r / t_p
        winner = "rsqlx快" if ratio < 1 else "psycopg2快"
        print(f"  {rows:>6}  {fmt(t_r)}  {fmt(t_p)}  {ratio:>13.2f}x  {winner}")

    # ---- 3. 拟合拆分开销 ----
    fixed_r, per_row_r = fit_line(ROW_COUNTS, rsqlx_times)
    fixed_p, per_row_p = fit_line(ROW_COUNTS, psyco_times)

    print("\n[拆分] t = fixed_overhead + per_row_cost * rows")
    print(f"  {'':>14}  {'fixed(每次查询)':>18}  {'per_row(每行解码)':>20}")
    print("  " + "-" * 56)
    print(f"  {'rsqlx':<14}  {fmt(fixed_r):>18}  {fmt(per_row_r):>20}")
    print(f"  {'psycopg2':<14}  {fmt(fixed_p):>18}  {fmt(per_row_p):>20}")
    print()
    print(f"  固定开销  : rsqlx 是 psycopg2 的 {fixed_r / fixed_p:.2f}x")
    print(f"  每行开销  : rsqlx 是 psycopg2 的 {per_row_r / per_row_p:.2f}x")

    # 交叉点
    if per_row_r != per_row_p:
        breakeven = (fixed_r - fixed_p) / (per_row_p - per_row_r)
        print(f"  盈亏平衡点: 单次查询返回约 {breakeven:.0f} 行时两者耗时相同")
        print(f"              （< {breakeven:.0f} 行 psycopg2 快，> {breakeven:.0f} 行 rsqlx 快）")

    # ---- 4. 并发场景 ----
    print("\n[并发] 100 个查询")
    sql1 = "SELECT id, name, v, price, ts FROM bench_rows WHERE id = $1"

    async def concurrent(n):
        await asyncio.gather(*[
            pool.fetch_one("SELECT id, name, v, price, ts FROM bench_rows WHERE id = $1", [i])
            for i in range(1, n + 1)
        ])

    for _ in range(10):
        await concurrent(100)
    t0 = time.perf_counter()
    for _ in range(20):
        await concurrent(100)
    t_concurrent = (time.perf_counter() - t0) / 20

    def sequential(n):
        for i in range(1, n + 1):
            cur.execute("SELECT id, name, v, price, ts FROM bench_rows WHERE id = %s", (i,))
            cur.fetchone()

    for _ in range(10):
        sequential(100)
    t0 = time.perf_counter()
    for _ in range(20):
        sequential(100)
    t_sequential = (time.perf_counter() - t0) / 20

    print(f"  rsqlx    100 并发 (gather)   : {t_concurrent * 1e6:10.1f}us")
    print(f"  psycopg2 100 顺序            : {t_sequential * 1e6:10.1f}us")
    print(f"  比值                          : {t_concurrent / t_sequential:9.2f}x "
          f"({'rsqlx快' if t_concurrent < t_sequential else 'psycopg2快'})")

    # ---- 5. 纯 Python 开销占比 ----
    print("\n[归因] rsqlx 单次查询的开销构成（估算）")
    # 用最轻量的查询测 rsqlx 的总固定开销
    tiny_sql = "SELECT 1"
    t_tiny_r = await bench_rsqlx(pool, tiny_sql, None, n=500, warmup=100)
    t_tiny_p = bench_psycopg2(cur, "SELECT 1", None, n=500, warmup=100)
    print(f"  rsqlx    'SELECT 1'  单次      : {fmt(t_tiny_r)}")
    print(f"  psycopg2 'SELECT 1'  单次      : {fmt(t_tiny_p)}")
    print(f"  差额（rsqlx 的额外固定成本）    : {fmt(t_tiny_r - t_tiny_p)}")

    # ---- 5b. 跨线程 + 事件循环通知的开销基线 ----
    # rsqlx 每次查询: 事件循环线程 → tokio 线程执行 → 完成后跨线程唤醒事件循环。
    # 用 run_in_executor 模拟同等规模的「跨线程提交 + 等待返回 + 事件循环通知」，
    # 看这个模式本身值多少钱。
    loop = asyncio.get_running_loop()
    def noop():
        return None
    for _ in range(200):
        await loop.run_in_executor(None, noop)
    t0 = time.perf_counter()
    for _ in range(2000):
        await loop.run_in_executor(None, noop)
    t_executor = (time.perf_counter() - t0) / 2000
    print(f"\n  [基线] await loop.run_in_executor(noop)  : {fmt(t_executor)}")
    print(f"         跨线程提交 + 等结果 + 事件循环唤醒的成本")
    print(f"         asyncio 空协程（纯本地）: {fmt(per_coro)}")
    print(f"  → 说明: rsqlx 额外开销的大头不是 asyncio 协程本身（仅 {per_coro*1e6:.1f}us），")
    print(f"    而是「跨线程任务派发 + 事件循环唤醒」这个模式（约 {t_executor*1e6:.0f}us 量级）。")
    print(f"    psycopg2 是同步 C 调用，不走这个路径，所以没有这笔成本。")

    # ---- 5c. 慢查询并行等待能力 ----
    # 这是 rsqlx 的核心优势场景: 查询等待期间释放 GIL + 异步并发，
    # 多个查询的等待时间可以重叠。psycopg2 同步调用只能串行等待。
    print("\n[并发等待] 10 个 pg_sleep(0.1) 查询")
    sleep_sql = "SELECT pg_sleep(0.1)"

    async def rsqlx_sleeps(n):
        await asyncio.gather(*[pool.execute(sleep_sql, None) for _ in range(n)])

    for _ in range(2):
        await rsqlx_sleeps(10)
    t0 = time.perf_counter()
    for _ in range(3):
        await rsqlx_sleeps(10)
    t_r_sleep = (time.perf_counter() - t0) / 3

    def psyco_sleeps(n):
        for _ in range(n):
            cur.execute(sleep_sql)
            cur.fetchall()

    for _ in range(2):
        psyco_sleeps(10)
    t0 = time.perf_counter()
    for _ in range(3):
        psyco_sleeps(10)
    t_p_sleep = (time.perf_counter() - t0) / 3

    print(f"  rsqlx    10 个并发（等待重叠）: {t_r_sleep * 1e3:8.1f}ms")
    print(f"  psycopg2 10 个顺序（串行等待）: {t_p_sleep * 1e3:8.1f}ms")
    print(f"  加速比                        : {t_p_sleep / t_r_sleep:7.2f}x  ← rsqlx 的核心优势")

    # ---- 5d. 批量取 vs 逐行取 ----
    print("\n[批量 vs 逐行] 取 1000 行")
    t_batch = await bench_rsqlx(
        pool, "SELECT id, name, v, price, ts FROM bench_rows ORDER BY id LIMIT 1000",
        None, n=50, warmup=10)
    t_single_est = rsqlx_times[0] * 1000
    print(f"  一次 fetch 1000 行            : {t_batch * 1e3:8.2f}ms")
    print(f"  单行查询 × 1000 次            : {t_single_est * 1e3:8.2f}ms  (估算)")
    print(f"  批量取比分 1000 次单行快       : {t_single_est / t_batch:7.0f}x")
    print(f"  → 用 rsqlx 要避开「N+1 逐行查询」，尽量用 IN / JOIN / 批量 fetch")

    # ---- 6. 结论 ----
    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    print(f"1. 单行/少量行查询: psycopg2 快 {fixed_r / fixed_p:.1f}x")
    print(f"   原因: rsqlx 每次查询有 {fixed_r * 1e6:.0f}us 固定开销")
    print(f"         (协程创建 + tokio 线程切换 + GIL 释放/重获), psycopg2 只要 {fixed_p * 1e6:.0f}us")
    print(f"2. 多行查询: rsqlx 每行解码成本是 psycopg2 的 {per_row_r / per_row_p:.2f}x")
    if per_row_r < per_row_p:
        print(f"   → 行数超过约 {breakeven:.0f} 行时 rsqlx 反超（Rust 原生解码更快）")
    print(f"3. 并发: rsqlx 用 gather 可以真正并行，psycopg2 同步只能串行")
    print(f"   → 实测 {'rsqlx 快' if t_concurrent < t_sequential else 'psycopg2 快'} {max(t_concurrent, t_sequential) / min(t_concurrent, t_sequential):.1f}x")
    print()
    print("选型建议:")
    print("  - OLTP 点查（单行、高并发、低延迟）→ psycopg2 / asyncpg 更合适")
    print("  - 批量读写、分析查询、大结果集      → rsqlx 有优势")
    print("  - 需要在 async 应用里统一三库接口    → rsqlx（异步 + 三库统一是核心价值）")

    await pool.close()
    cur.close()
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
