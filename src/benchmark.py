"""DuckDB 与 Polars 的性能对比 + 结果等价性校验。

两件事：
  1. 计时：对每条查询重复执行若干次，取中位数 / 最小值
  2. 校验：比对两个引擎的输出，确认「快」的同时「算得一样」

计时只覆盖查询执行与结果物化，不含进程启动和连接建立；
Polars 的惰性计划在首次 collect 时才真正编译，因此预热次数默认 ≥1。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time

import duckdb
import polars as pl

import common
import duckdb_demo
import polars_demo


def peak_rss_mb() -> float:
    """进程峰值 RSS（MB）。ru_maxrss 是单调高水位，不会随查询结束回落。"""
    return common.ru_maxrss_mb()


def timeit(fn, repeat: int, warmup: int) -> list[float]:
    """预热后重复计时，返回每次耗时（秒）。"""
    for _ in range(warmup):
        fn()
    timings = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        timings.append(time.perf_counter() - t0)
    return timings


def build_tasks(source: str):
    """返回 [(名称, duckdb 调用, polars 调用)]。"""
    con = duckdb_demo.connect(source)
    lf = polars_demo.load()
    tasks = [
        (
            "区域 × 类目 Top-N",
            lambda: duckdb_demo.query_region_category(con),
            lambda: polars_demo.query_region_category(lf),
            "region_category",
        ),
        (
            "每日收入趋势",
            lambda: duckdb_demo.query_daily_revenue(con),
            lambda: polars_demo.query_daily_revenue(lf),
            "daily_revenue",
        ),
        (
            "高价值客户 Top 20",
            lambda: duckdb_demo.query_top_customers(con),
            lambda: polars_demo.query_top_customers(lf),
            "top_customers",
        ),
    ]
    return con, tasks


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB vs Polars 基准测试")
    parser.add_argument("--repeat", type=int, default=5, help="每条查询重复次数")
    parser.add_argument("--warmup", type=int, default=1, help="预热次数")
    parser.add_argument(
        "--source",
        choices=["parquet", "duckdb"],
        default="parquet",
        help="DuckDB 侧的数据源",
    )
    parser.add_argument("--json", type=str, default=None, help="把结果写成 JSON 文件")
    args = parser.parse_args()

    print("=== 环境 ===")
    print(f"  Python      {platform.python_version()}")
    print(f"  平台        {platform.platform()}")
    print(f"  CPU 核数    {os.cpu_count()}")
    print(f"  DuckDB      {duckdb.__version__}")
    print(f"  Polars      {pl.__version__}")
    print(f"  数据源      {args.source}")
    print(f"  重复/预热   {args.repeat}/{args.warmup}")

    con, tasks = build_tasks(args.source)
    report = []
    try:
        for name, duck_fn, polars_fn, key in tasks:
            print(f"\n>>> {name}")
            duck_times = timeit(duck_fn, args.repeat, args.warmup)
            polars_times = timeit(polars_fn, args.repeat, args.warmup)

            duck_med = statistics.median(duck_times)
            polars_med = statistics.median(polars_times)

            # 等价性校验
            duck_tbl = duck_fn()
            polars_df = polars_fn()
            duck_df = pl.from_arrow(duck_tbl)
            ok, detail = common.compare_frames(duck_df, polars_df, common.QUERY_KEYS[key])
            print(f"  结果等价    {'✅' if ok else '❌'} {detail}")

            duck_min = min(duck_times) * 1000
            polars_min = min(polars_times) * 1000
            print(f"  DuckDB      中位 {duck_med * 1000:8.1f} ms   最小 {duck_min:8.1f} ms")
            print(f"  Polars      中位 {polars_med * 1000:8.1f} ms   最小 {polars_min:8.1f} ms")
            winner = "DuckDB" if duck_med < polars_med else "Polars"
            speedup = max(duck_med, polars_med) / min(duck_med, polars_med)
            print(f"  更快        {winner}（{speedup:.2f}×）")

            report.append(
                {
                    "query": name,
                    "equivalent": ok,
                    "detail": detail,
                    "duckdb_median_ms": round(duck_med * 1000, 3),
                    "duckdb_min_ms": round(min(duck_times) * 1000, 3),
                    "polars_median_ms": round(polars_med * 1000, 3),
                    "polars_min_ms": round(min(polars_times) * 1000, 3),
                    "faster": winner,
                }
            )
    finally:
        con.close()

    print(f"\n=== 汇总（进程峰值 RSS {peak_rss_mb():.0f} MB）===")
    header = (
        common.pad("查询", 22)
        + common.pad_left("DuckDB(ms)", 13)
        + common.pad_left("Polars(ms)", 13)
        + common.pad_left("更快", 10)
        + common.pad_left("等价", 8)
    )
    print(header)
    print("-" * common.display_width(header))
    for row in report:
        print(
            common.pad(row["query"], 22)
            + common.pad_left(f"{row['duckdb_median_ms']:.1f}", 13)
            + common.pad_left(f"{row['polars_median_ms']:.1f}", 13)
            + common.pad_left(row["faster"], 10)
            + common.pad_left("✅" if row["equivalent"] else "❌", 8)
        )

    all_ok = all(r["equivalent"] for r in report)
    print(f"\n结果等价性：{'全部通过 ✅' if all_ok else '存在差异 ❌'}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"已写入 {args.json}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
