"""流式处理 vs 一次性物化：内存里到底同时存着多少数据。

核心问题：**处理一份大结果集时，内存占用随数据量怎么变？**
  · 一次性物化（collect / to_arrow_table）  整个结果集同时驻留  —— O(n)
  · 流式消费（逐批处理）                    同时只有一批驻留    —— O(1)

所以这里把同一件事在两个数据规模上各跑一遍，看「驻留数据量」涨了几倍。

两个引擎对应的流式接口：
  DuckDB  rel.to_arrow_reader() -> read_next_batch()   （取代已废弃的 fetch_record_batch）
  Polars  scan_parquet(...).collect_batches(chunk_size=)

每个任务都在**独立子进程**里跑。关于内存测量方式，有两个坑，注释里写清楚了：
  1. ru_maxrss 在 Linux 上会从父进程继承下来，不能直接用
  2. RSS 差值会被分配器 arena 的历史峰值污染，跨进程不可比
所以主指标用的是「驻留字节数」（从 Arrow/Polars 对象直接读出来，确定性可复现），
RSS 只作为参考值附在旁边。

用法：
    uv run python src/streaming_demo.py                 # 默认最大 400 万行
    uv run python src/streaming_demo.py --rows 8000000  # 更大的数据
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

import duckdb
import numpy as np
import polars as pl
import pyarrow.compute as pc

import common

BIG_PARQUET = common.DATA_DIR / "big_orders.parquet"
OUT_DUCKDB = common.DATA_DIR / "stream_duckdb.parquet"
OUT_POLARS = common.DATA_DIR / "stream_polars.parquet"

JSON_MARKER = "@@RESULT@@"
COLUMNS = "region, category, revenue"
CHUNK_SIZE = 100_000  # Polars collect_batches 的批大小


# ---------------------------------------------------------------- 内存测量


def current_rss_mb() -> float:
    """当前 RSS（不是高水位）。Linux 走 /proc，其他平台退回到峰值。"""
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            pages = int(fh.read().split()[1])
        return pages * resource.getpagesize() / 1024 / 1024
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1024


class RssSampler:
    """后台线程采样本进程的 RSS，记录峰值。

    为什么不直接用 ru_maxrss：
      · 子进程 spawn 用的是 vfork/posix_spawn，短暂共享父进程的 mm，
        signal->maxrss 里带着父进程的历史峰值 —— 量到的全是父进程的数。
      · 写 /proc/self/clear_refs 也重置不掉（它只重置 mm->hiwater_rss）。
    为什么不只看 RSS 差值：
      · 分配器（malloc arena）一旦涨上去就不还给内核，于是「差值」量的其实是
        arena 的历史高水位，不是这一轮真正驻留的数据量，跨进程不可比。
    所以主指标用对象自己报的字节数，RSS 仅作参考。
    """

    def __init__(self, interval: float = 0.002) -> None:
        self.interval = interval
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.peak = max(self.peak, current_rss_mb())

    def __enter__(self) -> RssSampler:
        self.peak = current_rss_mb()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self.peak = max(self.peak, current_rss_mb())


def emit(result: dict) -> None:
    """子进程只往 stdout 打一行 JSON，其余输出走 stderr。"""
    print(JSON_MARKER + json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------- 造数


def ensure_big_data(rows: int, users: int) -> None:
    """缓存式造数：文件已存在且行数对得上就跳过。"""
    if BIG_PARQUET.exists():
        try:
            existing = pl.scan_parquet(BIG_PARQUET).select(pl.len()).collect().item()
            if existing == rows:
                print(f"复用已有数据 {BIG_PARQUET.name}（{rows:,} 行）")
                return
        except Exception:
            pass

    print(f"生成 {rows:,} 行大数据 …")
    import pyarrow.parquet as pq

    import gen_data

    rng = np.random.default_rng(common.SEED)
    table = gen_data.build_orders(rows, users, rng)
    # row_group_size 调小：让 Parquet 的分块粒度可见，不被默认的 1M 行行组掩盖
    pq.write_table(table, BIG_PARQUET, compression="zstd", row_group_size=100_000)
    size = BIG_PARQUET.stat().st_size / 1024 / 1024
    print(f"已写入 {BIG_PARQUET.name}（{size:.1f} MB，row group 10 万行）")


# ---------------------------------------------------------------- 四个任务
# 每个任务只管做事，计时和内存统计由 run_task 统一包一层。
# 每个任务返回三个关键量：处理行数、同时驻留的字节数、revenue 合计。


def task_duckdb_stream(rows: int, limit: int) -> dict:
    """DuckDB 流式消费：Arrow RecordBatchReader，边拉边算。"""
    con = duckdb.connect()
    reader = con.sql(
        f"SELECT {COLUMNS} FROM read_parquet('{BIG_PARQUET}') LIMIT {limit}"
    ).to_arrow_reader()

    total = 0.0
    n_rows = 0
    n_batches = 0
    max_live_bytes = 0
    max_batch_rows = 0
    while True:
        try:
            batch = reader.read_next_batch()
        except StopIteration:  # 流读完了
            break
        if batch is None:
            break
        n_batches += 1
        n_rows += batch.num_rows
        max_batch_rows = max(max_batch_rows, batch.num_rows)
        # 同时只持有这一批 —— 处理完就放掉
        max_live_bytes = max(max_live_bytes, batch.nbytes)
        # 用 Arrow 算子求和，不要 to_pylist()（那会把整批拉成 Python 对象）
        total += float(pc.sum(batch.column("revenue")).as_py())
    con.close()
    return {
        "method": "rel.to_arrow_reader() → read_next_batch()",
        "rows": n_rows,
        "n_batches": n_batches,
        "max_batch_rows": max_batch_rows,
        "live_bytes": max_live_bytes,
        "revenue_sum": total,
    }


def task_polars_stream(rows: int, limit: int) -> dict:
    """Polars 流式消费：collect_batches 产出 chunk。"""
    total = 0.0
    n_rows = 0
    n_batches = 0
    max_live_bytes = 0
    max_batch_rows = 0
    lf = pl.scan_parquet(BIG_PARQUET).select("region", "category", "revenue").limit(limit)
    for chunk in lf.collect_batches(chunk_size=CHUNK_SIZE):
        n_batches += 1
        n_rows += chunk.height
        max_batch_rows = max(max_batch_rows, chunk.height)
        max_live_bytes = max(max_live_bytes, chunk.estimated_size())
        total += float(chunk["revenue"].sum())
    return {
        "method": f"scan_parquet().collect_batches(chunk_size={CHUNK_SIZE})",
        "rows": n_rows,
        "n_batches": n_batches,
        "max_batch_rows": max_batch_rows,
        "live_bytes": max_live_bytes,
        "revenue_sum": total,
    }


def task_duckdb_materialize(rows: int, limit: int) -> dict:
    """DuckDB 物化：to_arrow_table 把整个结果集塞进内存。"""
    con = duckdb.connect()
    tbl = con.sql(
        f"SELECT {COLUMNS} FROM read_parquet('{BIG_PARQUET}') LIMIT {limit}"
    ).to_arrow_table()
    n = tbl.num_rows
    total = float(pc.sum(tbl.column("revenue")).as_py())
    live = tbl.nbytes  # Arrow Table 里所有 buffer 的字节数
    con.close()
    return {
        "method": "rel.to_arrow_table()",
        "rows": n,
        "n_batches": 1,
        "max_batch_rows": n,
        "live_bytes": live,
        "revenue_sum": total,
    }


def task_polars_materialize(rows: int, limit: int) -> dict:
    """Polars 物化：collect() 把整个结果集塞进内存。"""
    df = pl.scan_parquet(BIG_PARQUET).select("region", "category", "revenue").limit(limit).collect()
    return {
        "method": "scan_parquet().collect()",
        "rows": df.height,
        "n_batches": 1,
        "max_batch_rows": df.height,
        "live_bytes": df.estimated_size(),
        "revenue_sum": float(df["revenue"].sum()),
    }


def task_duckdb_write(rows: int, limit: int) -> dict:
    """DuckDB 流式写出：COPY ... TO，结果集不落进 Python 内存。"""
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT {COLUMNS} FROM read_parquet('{BIG_PARQUET}') LIMIT {limit}) "
        f"TO '{OUT_DUCKDB}' (FORMAT PARQUET)"
    )
    con.close()
    return {
        "method": "COPY (…) TO … (FORMAT PARQUET)",
        "out_file": str(OUT_DUCKDB),
        "out_bytes": OUT_DUCKDB.stat().st_size,
    }


def task_polars_write(rows: int, limit: int) -> dict:
    """Polars 流式写出：sink_parquet，边算边写。"""
    (
        pl.scan_parquet(BIG_PARQUET)
        .select("region", "category", "revenue")
        .limit(limit)
        .sink_parquet(OUT_POLARS)
    )
    return {
        "method": "scan_parquet().select().sink_parquet()",
        "out_file": str(OUT_POLARS),
        "out_bytes": OUT_POLARS.stat().st_size,
    }


TASKS = {
    "duckdb_stream": ("流式", "DuckDB", task_duckdb_stream),
    "polars_stream": ("流式", "Polars", task_polars_stream),
    "duckdb_materialize": ("物化", "DuckDB", task_duckdb_materialize),
    "polars_materialize": ("物化", "Polars", task_polars_materialize),
    "duckdb_write": ("流式写出", "DuckDB", task_duckdb_write),
    "polars_write": ("流式写出", "Polars", task_polars_write),
}


def run_task(name: str, rows: int, limit: int) -> dict:
    """跑一个任务，统一记上耗时和内存。"""
    mode, engine, fn = TASKS[name]
    baseline = current_rss_mb()
    with RssSampler() as sampler:
        t0 = time.perf_counter()
        result = fn(rows, limit)
        elapsed = time.perf_counter() - t0
    return {
        "mode": mode,
        "engine": engine,
        "limit": limit,
        "seconds": elapsed,
        "peak_rss_mb": sampler.peak,
        "rss_baseline_mb": baseline,
        "rss_delta_mb": max(0.0, sampler.peak - baseline),
        **result,
    }


# ---------------------------------------------------------------- 父进程


def run_worker(task: str, rows: int, limit: int) -> dict:
    """起一个子进程跑任务，解析它回传的 JSON。"""
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            task,
            "--rows",
            str(rows),
            "--limit",
            str(limit),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"子进程 {task} 失败：\n{proc.stderr[-2000:]}")
    for line in proc.stdout.splitlines():
        if line.startswith(JSON_MARKER):
            return json.loads(line[len(JSON_MARKER) :])
    raise RuntimeError(f"子进程 {task} 没返回结果：\n{proc.stdout[-2000:]}")


def mib(n: int) -> float:
    return n / 1024 / 1024


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的流式处理与内存对比")
    parser.add_argument("--rows", type=int, default=4_000_000, help="测试数据总行数")
    parser.add_argument("--users", type=int, default=50_000, help="用户数")
    parser.add_argument("--worker", type=str, default=None, help="内部使用：在子进程里跑单个任务")
    parser.add_argument("--limit", type=int, default=0, help="内部使用：处理多少行")
    args = parser.parse_args()

    if args.worker:
        emit(run_task(args.worker, args.rows, args.limit))
        return

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    ensure_big_data(args.rows, args.users)

    sizes = [s for s in (args.rows // 4, args.rows) if s > 0]

    print("\n每个任务在独立子进程里跑 …")
    results = []
    for limit in sizes:
        for task in ("duckdb_stream", "polars_stream", "duckdb_materialize", "polars_materialize"):
            results.append(run_worker(task, args.rows, limit))

    # ---------------------------------------------------------------- 表 1
    print("\n" + "=" * 92)
    print("内存里同时驻留的数据量")
    print("=" * 92)
    print(
        f"{'方式':<8}{'引擎':<9}{'行数':>12}{'驻留数据量(MB)':>16}{'批数':>8}"
        f"{'单批行数':>10}{'峰值RSS(MB)':>12}{'耗时(s)':>9}"
    )
    print("-" * 92)
    for r in results:
        print(
            f"{r['mode']:<8}{r['engine']:<9}{r['rows']:>12,}{mib(r['live_bytes']):>16.1f}"
            f"{r['n_batches']:>8}{r['max_batch_rows']:>10,}{r['peak_rss_mb']:>12.0f}{r['seconds']:>9.2f}"
        )

    # ---------------------------------------------------------------- 表 2
    print("\n" + "=" * 92)
    print(f"数据量 ×{sizes[-1] / sizes[0]:.1f} 时，驻留数据量涨了几倍")
    print("=" * 92)
    print(f"{'方式':<8}{'引擎':<9}{'小规模(MB)':>12}{'大规模(MB)':>12}{'倍数':>9}   判定")
    for mode in ("流式", "物化"):
        for engine in ("DuckDB", "Polars"):
            small = next(
                (
                    r
                    for r in results
                    if r["mode"] == mode and r["engine"] == engine and r["limit"] == sizes[0]
                ),
                None,
            )
            large = next(
                (
                    r
                    for r in results
                    if r["mode"] == mode and r["engine"] == engine and r["limit"] == sizes[-1]
                ),
                None,
            )
            if not small or not large or small["live_bytes"] <= 0:
                continue
            ratio = large["live_bytes"] / small["live_bytes"]
            verdict = "几乎不变（O(1)）" if ratio < 1.5 else "跟着数据涨（O(n)）"
            print(
                f"{mode:<8}{engine:<9}{mib(small['live_bytes']):>12.1f}"
                f"{mib(large['live_bytes']):>12.1f}{ratio:>9.2f}×  {verdict}"
            )

    # ---------------------------------------------------------------- 一致性
    print("\n" + "=" * 92)
    print("四种做法的结果是否一致")
    print("=" * 92)
    by_key = {(r["mode"], r["engine"], r["limit"]): r for r in results}
    for limit in sizes:
        got = [
            by_key.get((m, e, limit))
            for m, e in (
                ("流式", "DuckDB"),
                ("流式", "Polars"),
                ("物化", "DuckDB"),
                ("物化", "Polars"),
            )
        ]
        if not all(got):
            continue
        sums = [g["revenue_sum"] for g in got]
        same = all(abs(s - sums[0]) <= 1e-6 * max(abs(sums[0]), 1.0) for s in sums)
        print(
            f"{limit:>10,} 行：SUM(revenue) = {sums[0]:,.2f}  四种做法一致 {'✅' if same else '❌'}"
        )

    # ---------------------------------------------------------------- 流式写出
    print("\n" + "=" * 92)
    print("流式写出：结果集不进内存，直接落盘")
    print("=" * 92)
    writes = [run_worker(t, args.rows, args.rows) for t in ("duckdb_write", "polars_write")]
    print(f"{'引擎':<9}{'耗时(s)':>9}{'峰值RSS(MB)':>13}{'产出(MB)':>11}   方法")
    for w in writes:
        print(
            f"{w['engine']:<9}{w['seconds']:>9.2f}{w['peak_rss_mb']:>13.0f}"
            f"{mib(w['out_bytes']):>11.1f}   {w['method']}"
        )

    # 两个引擎写出来的文件，内容应该一致
    duck_sum = pl.scan_parquet(OUT_DUCKDB).select(pl.col("revenue").sum()).collect().item()
    polars_sum = pl.scan_parquet(OUT_POLARS).select(pl.col("revenue").sum()).collect().item()
    duck_rows = pl.scan_parquet(OUT_DUCKDB).select(pl.len()).collect().item()
    polars_rows = pl.scan_parquet(OUT_POLARS).select(pl.len()).collect().item()
    same = duck_rows == polars_rows and abs(duck_sum - polars_sum) <= 1e-6 * max(abs(duck_sum), 1.0)
    print(
        f"\n两个引擎写出的文件：行数 {duck_rows:,} / {polars_rows:,}，"
        f"SUM(revenue) 一致 {'✅' if same else '❌'}"
    )

    # ---------------------------------------------------------------- 小结
    print("\n" + "=" * 92)
    print("要点")
    print("=" * 92)
    print("  · 一次性物化（collect / to_arrow_table）的内存是 O(n)，数据翻倍内存也翻倍")
    print("  · 流式消费（to_arrow_reader / collect_batches）同时只驻留一批，是 O(1)")
    print("  · 数据大到装不下时，区别不在「哪个引擎快」，而在「你有没有物化」")
    print("  · DuckDB 的 to_arrow_reader() 已取代废弃的 fetch_record_batch()")
    print("  · DuckDB 侧还能 COPY (…) TO 直接流式写 Parquet；Polars 侧是 sink_parquet()")
    print()
    print("  关于「峰值RSS」这一列：它含引擎自身的缓冲池和分配器开销")
    print("  （DuckDB 有自己的 buffer manager，Polars 有自己的 arena），")
    print("  跨进程也不可比 —— 所以判断内存占用请看「驻留数据量」那一列。")
    print()


if __name__ == "__main__":
    main()
