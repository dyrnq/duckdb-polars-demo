"""线程扩展性：两个引擎在 1/2/4/8 线程下各快多少。

README 里把「执行模型」列进了项目目标，但一直没量过并行。这一节补上：
同一条查询，线程数从 1 加到 N，耗时怎么变、加速比是多少、效率掉多少。

为什么要起子进程：
  · Polars 的线程池大小在 **import 时**就定死了（POLARS_MAX_THREADS 环境变量），
    进程内改不了 —— 所以父进程用 env 把 POLARS_MAX_THREADS 传给子进程。
  · DuckDB 用 `SET threads=N` 就行，但它和 Polars 放同一个子进程跑更公平：
    每次测量都是全新进程，不受前一次运行的内存/缓存状态影响。

为什么要多轮交错：
  这是一台**共享的开发机**，旁边随时有人在编译（实测时 load1 = 16，机器只有 8 核）。
  如果串行地测完 1 线程再测 2 线程，谁赶上高负载谁就背锅，
  测出来的「加速比」其实是负载漂移，不是引擎的并行能力。
  所以每一轮都把所有 (引擎, 线程数) 组合过一遍，跑若干轮后取中位数 ——
  负载漂移对所有组合一视同仁，差异才是引擎自己的。

顺带查一件事：**线程数变了，浮点求和的顺序也会变**，
所以同一个查询在不同线程数下的 SUM 可能差在末位 —— 脚本会把这差多少直接打出来。

用法：
    uv run python src/scaling_demo.py
    uv run python src/scaling_demo.py --rows 4000000 --threads 1,2,4,8
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

import common

JSON_MARKER = "@@RESULT@@"
SCALE_ORDERS = common.DATA_DIR / "scale_orders.parquet"
SCALE_USERS = common.DATA_DIR / "scale_users.parquet"

QUERY_NAMES = ("region_category", "daily_revenue", "top_customers")
QUERY_LABELS = {
    "region_category": "区域 × 类目 Top-N",
    "daily_revenue": "每日收入趋势",
    "top_customers": "高价值客户 Top 20",
}
ENGINES = ("duckdb", "polars")


# ---------------------------------------------------------------- 机器状态

load1 = common.load1
describe_machine = common.describe_machine


# ---------------------------------------------------------------- 造数


def ensure_data(rows: int, users: int) -> None:
    """按需生成放大版数据（比 100 万行更容易看出线程差异）。"""
    if SCALE_ORDERS.exists() and SCALE_USERS.exists():
        import polars as pl

        existing = pl.scan_parquet(SCALE_ORDERS).select(pl.len()).collect().item()
        if existing == rows:
            print(f"复用已有数据（{rows:,} 行）")
            return

    print(f"生成 {rows:,} 行放大版数据 …")
    import pyarrow.parquet as pq

    import gen_data

    rng = np.random.default_rng(common.SEED)
    pq.write_table(gen_data.build_orders(rows, users, rng), SCALE_ORDERS, compression="zstd")
    pq.write_table(gen_data.build_users(users, rng), SCALE_USERS, compression="zstd")
    size = SCALE_ORDERS.stat().st_size / 1024 / 1024
    print(f"已写入 {SCALE_ORDERS.name}（{size:.1f} MB）")


# ---------------------------------------------------------------- 子进程


def run_queries(engine: str, threads: int, repeat: int) -> dict:
    """在一个进程里跑完三条查询，返回每条的耗时中位数和结果。"""
    import duckdb
    import polars as pl

    import duckdb_demo

    if engine == "duckdb":
        con = duckdb.connect()
        con.execute(f"SET threads={threads}")
        con.execute(
            f"CREATE OR REPLACE VIEW v_orders AS SELECT * FROM read_parquet('{SCALE_ORDERS}')"
        )
        con.execute(
            f"CREATE OR REPLACE VIEW v_users AS SELECT * FROM read_parquet('{SCALE_USERS}')"
        )
        runners = {
            "region_category": lambda: duckdb_demo.query_region_category(con),
            "daily_revenue": lambda: duckdb_demo.query_daily_revenue(con),
            "top_customers": lambda: duckdb_demo.query_top_customers(con),
        }
    else:
        # 和 DuckDB 侧读同一份放大版数据（polars_demo.load() 读的是 100 万行那份）
        lf = pl.scan_parquet(SCALE_ORDERS).join(
            pl.scan_parquet(SCALE_USERS), on="user_id", how="inner"
        )
        runners = {
            "region_category": lambda: _polars_query(lf, "region_category"),
            "daily_revenue": lambda: _polars_query(lf, "daily_revenue"),
            "top_customers": lambda: _polars_query(lf, "top_customers"),
        }

    out = {"engine": engine, "threads": threads, "queries": {}}
    for name, fn in runners.items():
        # 预热一次：Polars 的惰性计划在首次 collect 时才编译
        fn()
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            res = fn()
            times.append(time.perf_counter() - t0)
        frame = res if isinstance(res, pl.DataFrame) else pl.from_arrow(res)
        out["queries"][name] = {
            "median_ms": statistics.median(times) * 1000,
            "min_ms": min(times) * 1000,
            "rows": frame.height,
            "revenue_sum": float(frame["revenue"].sum()),
            "data": canonical(frame),
        }
    if engine == "duckdb":
        con.close()
    return out


def _polars_query(lf, name: str):
    """跑一条 Polars 侧的查询（惰性计划 + collect）。"""
    import polars_demo

    return {
        "region_category": polars_demo.query_region_category,
        "daily_revenue": polars_demo.query_daily_revenue,
        "top_customers": polars_demo.query_top_customers,
    }[name](lf)


def _canon(value):
    """把值转成能安全 JSON 序列化的形式（日期转 ISO 字符串）。"""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def canonical(frame) -> dict:
    """把结果转成 {columns, rows} 的可 JSON 序列化结构。

    日期转 ISO 字符串 —— 否则 json.dumps 会直接报错。
    父进程再还原成 DataFrame 做逐值比对。
    """
    columns = frame.columns
    rows = [[_canon(v) for v in row] for row in frame.rows()]
    return {"columns": columns, "rows": rows}


def worker(engine: str, threads: int, repeat: int) -> None:
    print(JSON_MARKER + json.dumps(run_queries(engine, threads, repeat), ensure_ascii=False))


# ---------------------------------------------------------------- 父进程


def run_worker(engine: str, threads: int, repeat: int) -> dict:
    """起一个子进程跑指定线程数。

    关键：POLARS_MAX_THREADS 必须在 Python 启动前就在环境里 ——
    Polars 在 import 时按它建线程池，进程内再设环境变量已经晚了。
    """
    env = dict(os.environ)
    env["POLARS_MAX_THREADS"] = str(threads)
    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            engine,
            "--worker-threads",
            str(threads),
            "--repeat",
            str(repeat),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"子进程 {engine}/{threads} 失败：\n{proc.stderr[-2000:]}")
    for line in proc.stdout.splitlines():
        if line.startswith(JSON_MARKER):
            return json.loads(line[len(JSON_MARKER) :])
    raise RuntimeError(f"子进程 {engine}/{threads} 没返回结果：\n{proc.stdout[-2000:]}")


def aggregate(rounds: list[dict]) -> dict:
    """把多轮结果合成每个 (引擎, 线程数) 的一组统计量。

    中位数：各轮中位数的中位数（抗单轮异常）
    最小值：所有轮里的最快一次（最接近「机器空闲时」的上限）
    """
    combos = list(rounds[0].keys())
    out = {}
    for combo in combos:
        queries = {}
        for qname in QUERY_NAMES:
            medians = [r[combo]["queries"][qname]["median_ms"] for r in rounds]
            mins = [r[combo]["queries"][qname]["min_ms"] for r in rounds]
            queries[qname] = {
                "median_ms": statistics.median(medians),
                "min_ms": min(mins),
                "rounds_ms": medians,
                "data": rounds[0][combo]["queries"][qname]["data"],
                "revenue_sum": rounds[0][combo]["queries"][qname]["revenue_sum"],
            }
        out[combo] = {"queries": queries}
    return out


def restore(data: dict):
    """把子进程回传的 {columns, rows} 还原成 DataFrame。"""
    import polars as pl

    cols = data["columns"]
    rows = data["rows"]
    return pl.DataFrame({col: [row[i] for row in rows] for i, col in enumerate(cols)})


# ---------------------------------------------------------------- 报告


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    """按显示宽度打印一张表（中文是全角，用 common.pad 对齐）。"""
    print(common.pad("", 92).rstrip())
    print("".join(common.pad(h, w) for h, w in zip(headers, widths, strict=True)))
    print("-" * 92)
    for row in rows:
        print("".join(common.pad(c, w) for c, w in zip(row, widths, strict=True)))


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的线程扩展性对比")
    parser.add_argument("--rows", type=int, default=4_000_000, help="数据行数")
    parser.add_argument("--users", type=int, default=50_000, help="用户数")
    parser.add_argument("--threads", type=str, default="1,2,4,8", help="要测的线程数，逗号分隔")
    parser.add_argument("--repeat", type=int, default=5, help="每个子进程内每条查询重复次数")
    parser.add_argument("--rounds", type=int, default=3, help="交错轮数（每轮把所有组合测一遍）")
    parser.add_argument("--worker", type=str, default=None, help="内部使用")
    parser.add_argument("--worker-threads", type=int, default=0, help="内部使用")
    args = parser.parse_args()

    if args.worker:
        worker(args.worker, args.worker_threads, args.repeat)
        return

    thread_list = [int(t) for t in args.threads.split(",") if t.strip()]
    import duckdb
    import polars as pl

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{describe_machine()}")
    ensure_data(args.rows, args.users)

    combos = [(engine, t) for engine in ENGINES for t in thread_list]
    rounds: list[dict] = []
    for r in range(args.rounds):
        row = {}
        for engine, threads in combos:
            row[(engine, threads)] = run_worker(engine, threads, args.repeat)
        rounds.append(row)
        print(f"  第 {r + 1}/{args.rounds} 轮完成（当时 load1 = {load1():.1f}）")

    agg = aggregate(rounds)

    # ---------------------------------------------------------------- 表 1
    print()
    print("=" * 92)
    print("耗时（ms，各轮中位数的中位数）")
    print("=" * 92)
    widths = [22, 9] + [12] * len(thread_list)
    headers = ["查询", "引擎"] + [f"{t} 线程" for t in thread_list]
    rows = []
    for qname in QUERY_NAMES:
        for engine in ENGINES:
            cells = [f"{agg[(engine, t)]['queries'][qname]['median_ms']:.1f}" for t in thread_list]
            rows.append([QUERY_LABELS[qname], engine, *cells])
        rows.append(["", "", *([""] * len(thread_list))])
    table(headers, rows, widths)

    # ---------------------------------------------------------------- 表 1b
    print()
    print("=" * 92)
    print("耗时（ms，所有轮次里的最快一次）—— 最接近「机器空闲时」的上限")
    print("=" * 92)
    rows = []
    for qname in QUERY_NAMES:
        for engine in ENGINES:
            cells = [f"{agg[(engine, t)]['queries'][qname]['min_ms']:.1f}" for t in thread_list]
            rows.append([QUERY_LABELS[qname], engine, *cells])
        rows.append(["", "", *([""] * len(thread_list))])
    table(headers, rows, widths)

    # ---------------------------------------------------------------- 表 2
    print()
    print("=" * 92)
    print("加速比（相对 1 线程）与并行效率")
    print("=" * 92)
    widths2 = [22, 9] + [14] * len(thread_list[1:])
    headers2 = ["查询", "引擎"] + [f"{t} 线程" for t in thread_list[1:]]
    rows = []
    for qname in QUERY_NAMES:
        for engine in ENGINES:
            base = agg[(engine, thread_list[0])]["queries"][qname]["median_ms"]
            cells = []
            for t in thread_list[1:]:
                cur = agg[(engine, t)]["queries"][qname]["median_ms"]
                speedup = base / cur if cur > 0 else 0.0
                cells.append(f"{speedup:.2f}x / {speedup / t * 100:.0f}%")
            rows.append([QUERY_LABELS[qname], engine, *cells])
        rows.append(["", "", *([""] * len(thread_list[1:]))])
    table(headers2, rows, widths2)

    # ---------------------------------------------------------------- 表 3
    print()
    print("=" * 92)
    print("轮间波动（同一组合各轮中位数的 max/min，越接近 1 越稳）")
    print("=" * 92)
    widths3 = [22, 9] + [12] * len(thread_list)
    rows = []
    for qname in QUERY_NAMES:
        for engine in ENGINES:
            cells = []
            for t in thread_list:
                vals = agg[(engine, t)]["queries"][qname]["rounds_ms"]
                lo, hi = min(vals), max(vals)
                cells.append(f"{hi / lo:.2f}x")
            rows.append([QUERY_LABELS[qname], engine, *cells])
        rows.append(["", "", *([""] * len(thread_list))])
    table(headers, rows, widths3)

    # ---------------------------------------------------------------- 结果一致性
    print()
    print("=" * 92)
    print("结果一致性：线程数变了，结果还一样吗")
    print("=" * 92)
    for engine in ENGINES:
        for qname in QUERY_NAMES:
            base_t = thread_list[0]
            base = restore(agg[(engine, base_t)]["queries"][qname]["data"])
            same = True
            max_gap = 0.0
            for row in rounds:
                for t in thread_list:
                    other = restore(row[(engine, t)]["queries"][qname]["data"])
                    ok, _info = common.compare_frames(
                        other, base, common.QUERY_KEYS[qname], rtol=1e-12, atol=1e-6
                    )
                    same = same and ok
                    a = row[(engine, t)]["queries"][qname]["revenue_sum"]
                    b = row[(engine, base_t)]["queries"][qname]["revenue_sum"]
                    if b:
                        max_gap = max(max_gap, abs(a - b) / abs(b))
            mark = "✅ 完全一致" if same else "❌ 有差异"
            extra = "" if max_gap == 0 else f"（SUM 相对差 {max_gap:.2e}）"
            print(f"  {common.pad(engine, 8)}{common.pad(QUERY_LABELS[qname], 22)}{mark}{extra}")

    # ---------------------------------------------------------------- 小结
    print()
    print("=" * 92)
    print("小结")
    print("=" * 92)
    last_t = thread_list[-1]
    for qname in QUERY_NAMES:
        d_base = agg[("duckdb", thread_list[0])]["queries"][qname]["median_ms"]
        d_last = agg[("duckdb", last_t)]["queries"][qname]["median_ms"]
        p_base = agg[("polars", thread_list[0])]["queries"][qname]["median_ms"]
        p_last = agg[("polars", last_t)]["queries"][qname]["median_ms"]
        d_sp, p_sp = d_base / d_last, p_base / p_last
        # 最快一次算出来的加速比 —— 抗轮间抖动，但可能偏乐观
        d_min_base = agg[("duckdb", thread_list[0])]["queries"][qname]["min_ms"]
        d_min_last = agg[("duckdb", last_t)]["queries"][qname]["min_ms"]
        p_min_base = agg[("polars", thread_list[0])]["queries"][qname]["min_ms"]
        p_min_last = agg[("polars", last_t)]["queries"][qname]["min_ms"]
        d_sp_min = d_min_base / d_min_last
        p_sp_min = p_min_base / p_min_last
        winner = "polars" if p_sp > d_sp else "duckdb"
        print(
            f"  {common.pad(QUERY_LABELS[qname], 22)}"
            f"{last_t} 线程加速比（中位数/最快）：duckdb {d_sp:.2f}×/{d_sp_min:.2f}× "
            f"polars {p_sp:.2f}×/{p_sp_min:.2f}×  → {winner} 扩展性更好"
        )
    print()
    print("  · 每个引擎的「甜点线程数」（按最快一次算，各查询分别看谁最快）：")
    for engine in ENGINES:
        wins: dict[int, int] = {}
        for qname in QUERY_NAMES:
            best_t = min(
                thread_list,
                key=lambda t: agg[(engine, t)]["queries"][qname]["min_ms"],  # noqa: B023
            )
            wins[best_t] = wins.get(best_t, 0) + 1
        best_t = max(wins, key=lambda t: wins[t])
        detail = "，".join(f"{t} 线程快 {c} 条" for t, c in sorted(wins.items()))
        print(f"    {common.pad(engine, 8)}{best_t} 线程（{detail}）")
    print()
    print(f"  · 机器状态：{describe_machine()}")
    if load1() > (os.cpu_count() or 1):
        print("    ⚠ 负载 > 核数：这些加速比是「和别人抢 CPU 时」测到的。")
        print("      多线程引擎在这种条件下最吃亏（线程越多，抢不到的时间越多），")
        print("      想看引擎真实上限，等机器空闲时再跑一遍。")
    print("  · 「加速比 / 效率」两个数一起看：效率 = 加速比 / 线程数，")
    print("    100% 说明完美线性，掉得厉害说明这一层没吃满多核")
    print("  · 并行执行不改变结果，但会改变浮点求和的**顺序** ——")
    print("    所以跨线程数比较 SUM 时，末位可能有 1e-9 级别的差异，别当成 bug")
    print()


if __name__ == "__main__":
    main()
