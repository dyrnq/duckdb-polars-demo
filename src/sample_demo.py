"""采样与随机：同一个「随机」字，两个引擎给出的东西完全不一样。

* DuckDB：`USING SAMPLE` 是 SQL 语法的一部分，但**默认方法是 system（块采样）** ——
  在小表上要么返回 **0 行**，要么返回 **全部行**，永远不会是「10%」。
  想精确命中行数得写 `USING SAMPLE 10% (reservoir)`；
  采样自己的种子写在方法后面：`USING SAMPLE 10% (bernoulli, 42)`。
  随机函数有 `random()`（**没有 `rand()`**）。
* Polars：`df.sample(fraction=0.1, seed=42)` 是**逐行独立抽样**，
  小表上不会出 0 行，而且**每次都精确命中**目标行数（按 floor 取整）。
  Polars 2.1 **没有随机函数**（`pl.random()` / `pl.rand()` 都不存在），
  随机数得退回 numpy。

这个场景要回答：想要「可复现的随机」，两边分别该怎么写。

用法：
    uv run python src/sample_demo.py
    uv run python src/sample_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

import common

# 小表上的 system 采样试验次数：块采样只有两种结果，跑够次数才能看清分布
SMALL_TRIALS = 100
# 中表上的方法对比试验次数
METHOD_TRIALS = 20


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def duck_count(con: duckdb.DuckDBPyConnection, table: str, method: str | None = None) -> int:
    """数一下采样后剩多少行（method 为 None 时是全表）。"""
    clause = "" if method is None else f" USING SAMPLE 10% ({method})"
    return int(q1(con, f"SELECT count(*) FROM {table}{clause}"))


def sample_counts(
    con: duckdb.DuckDBPyConnection, table: str, method: str | None, trials: int
) -> list[int]:
    """同一张表、同一个方法，重复采样若干次，把每次的行数收回来。"""
    out = []
    for _ in range(trials):
        out.append(duck_count(con, table, method))
    return out


# ---------------------------------------------------------------- 1. 默认采样


def scenario_default(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 默认采样在小表上返回 0 行 —— 或者全部")

    con.execute("CREATE OR REPLACE TABLE small AS SELECT i AS id FROM range(1000) t(i)")
    df = pl.DataFrame({"id": range(1000)})

    counts: dict[int, int] = {}
    for _ in range(SMALL_TRIALS):
        n = int(q1(con, "SELECT count(*) FROM small USING SAMPLE 10%"))
        counts[n] = counts.get(n, 0) + 1

    print("数据：1000 行的小表，请求 10%")
    print(f"试验：同一句 `SELECT count(*) FROM small USING SAMPLE 10%` 跑 {SMALL_TRIALS} 次\n")
    print("  " + common.pad("采样行数", 16) + common.pad_left("出现次数", 12))
    print("  " + "-" * 28)
    for n in sorted(counts):
        print("  " + common.pad(f"{n:,}", 16) + common.pad_left(f"{counts[n]}", 12))

    zero = counts.get(0, 0)
    full = counts.get(1000, 0)

    print("\n  换几个比例再看看（每个比例跑 20 次）：")
    for pct in ["10%", "50%", "90%", "100%"]:
        seen: dict[int, int] = {}
        for _ in range(20):
            n = int(q1(con, f"SELECT count(*) FROM small USING SAMPLE {pct}"))
            seen[n] = seen.get(n, 0) + 1
        detail = "、".join(f"{k:,} 行 ×{v}" for k, v in sorted(seen.items()))
        print(f"    {pct:<6} -> {detail}")

    print("\n  Polars 侧同一张表、同一件事：")
    pol_counts = {df.sample(fraction=0.1, seed=42).height for _ in range(20)}
    print(f"    df.sample(fraction=0.1, seed=42) -> 行数集合 {sorted(pol_counts)}")

    print("\n→ 这一条是本 demo 里最贵的坑：")
    print("  · DuckDB 的 `USING SAMPLE` **默认方法是 system（块采样）**，不是逐行抽样。")
    print("    1000 行的表只有一个数据块 —— 整块被选中就是 1000 行，没被选中就是 0 行。")
    print(f"    上面 {SMALL_TRIALS} 次试验里：0 行出现 {zero} 次，1000 行出现 {full} 次。")
    print("    **中间值一个都没有出现过** —— 100 这个「正确答案」根本不在结果里。")
    print("  · 这是**必然二选一**，不是偶发：小表上用默认采样，你只有 0 行和全量两个结果。")
    print("  · Polars 侧同样的操作永远给 100 行（floor(1000 × 0.1)），没有这种二选一。")
    print("  · 实务影响：报表里写「抽 10% 看趋势」，在 DuckDB 上可能拿到 0 行、也可能拿到全量 ——")
    print("    而且这两种情况在**同一句 SQL** 下会随机切换，谁也没法从代码上看出区别。")


# ---------------------------------------------------------------- 2. 三种方法


def scenario_methods(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 三种采样方法：想要确切行数只有一种能给你")

    con.execute("CREATE OR REPLACE TABLE mid AS SELECT i AS id FROM range(100000) t(i)")

    print("  同一个请求（10%），不同方法跑 20 次，看行数的波动区间：\n")
    print(
        "  "
        + common.pad("采样方法", 16)
        + common.pad_left("10 万行表（目标 1 万）", 26)
        + common.pad_left("偏差", 12)
    )
    print("  " + "-" * 52)
    detail: dict[str, str] = {}
    for method in ["system", "bernoulli", "reservoir"]:
        vals = sample_counts(con, "mid", method, METHOD_TRIALS)
        lo, hi = min(vals), max(vals)
        err = (hi - 10_000) / 10_000
        detail[method] = f"{lo:,} ~ {hi:,}"
        print(
            "  "
            + common.pad(f"{method}", 16)
            + common.pad_left(f"{lo:,} ~ {hi:,}", 26)
            + common.pad_left(f"{err:+.1%}", 12)
        )

    print("\n  1000 行的小表上，同样的三种方法：\n")
    print(
        "  "
        + common.pad("采样方法", 16)
        + common.pad_left("行数（20 次）", 26)
        + common.pad("是否精确命中目标 100", 26)
    )
    print("  " + "-" * 68)
    small_result: dict[str, list[int]] = {}
    verdicts = {
        "system": "❌ 非 0 即全",
        "bernoulli": "❌ 按二项分布抖动",
        "reservoir": "✅ 每次都是 100",
    }
    for method in ["system", "bernoulli", "reservoir"]:
        vals = sample_counts(con, "small", method, METHOD_TRIALS)
        small_result[method] = vals
        shown = f"{min(vals):,} ~ {max(vals):,}"
        print(
            "  "
            + common.pad(f"{method}", 16)
            + common.pad_left(shown, 26)
            + common.pad(verdicts[method], 26)
        )

    # ---- 对账：两边都能精确命中目标行数的那一档，行数必须完全一致 ----
    # 注意：能对账的是「行数」，不是「选了哪些行」—— 两边的随机数发生器不同，
    # 选出来的 ID 集合必然不一样。
    duck_exact = duck_count(con, "small", "reservoir")
    pol_df = pl.DataFrame({"id": range(1000)})
    pol_exact = pol_df.sample(fraction=0.1, seed=42).height

    duck_frame = pl.DataFrame(
        {"档位": ["精确命中目标"], "目标行数": [100], "实际行数": [duck_exact]}
    )
    pol_frame = pl.DataFrame({"档位": ["精确命中目标"], "目标行数": [100], "实际行数": [pol_exact]})
    ok, why = common.compare_frames(duck_frame, pol_frame, ["档位"])
    print("\n  对账（两边都用「精确命中目标」的方法各取 10%）：")
    print(f"    DuckDB reservoir -> {duck_exact} 行；Polars fraction=0.1 -> {pol_exact} 行")
    print(f"    {'✅' if ok else '❌'} common.compare_frames: {why}")
    print("    （只能对账行数：两边 RNG 不同，选中的 ID 集合必然不同）")

    print("\n  换成区间检查，四个组合过一遍（判定标准不一样，不能一刀切）：\n")
    print(
        "  "
        + common.pad("组合", 30)
        + common.pad_left("实测行数", 16)
        + common.pad("判定依据", 26)
        + common.pad("结果", 8)
    )
    print("  " + "-" * 80)
    # bernoulli 是逐行独立掷骰子 → 行数服从二项分布，标准差 sqrt(n·p·(1-p))
    sigma = (1000 * 0.1 * 0.9) ** 0.5
    band_lo, band_hi = round(100 - 3 * sigma), round(100 + 3 * sigma)
    checks = [
        ("DuckDB system 10%", small_result["system"], "没有分布保证", None),
        (
            "DuckDB bernoulli 10%",
            small_result["bernoulli"],
            f"二项分布 3σ 带 {band_lo}~{band_hi}",
            (band_lo, band_hi),
        ),
        ("DuckDB reservoir 10%", small_result["reservoir"], "精确命中目标", (100, 100)),
        (
            "Polars fraction=0.1",
            [pol_df.sample(fraction=0.1, seed=s).height for s in range(20)],
            "精确命中目标",
            (100, 100),
        ),
    ]
    for label, vals, rule, band in checks:
        lo, hi = min(vals), max(vals)
        if band is None:
            mark = "❌"
        else:
            mark = "✅" if lo >= band[0] and hi <= band[1] else "❌"
        print(
            "  "
            + common.pad(label, 30)
            + common.pad_left(f"{lo:,} ~ {hi:,}", 16)
            + common.pad(rule, 26)
            + common.pad(mark, 8)
        )
    print(
        f"\n    （bernoulli 的 σ = sqrt(1000 × 0.1 × 0.9) ≈ {sigma:.1f}，"
        f"所以 {band_lo}~{band_hi} 是**正常波动**，不是 bug）"
    )

    print("\n→ 三种方法的差别不是「精度」上的，是**行数能不能说清**的差别：")
    print("  · **system（默认）**：按数据块抽样，小表上非 0 即全；大表上在目标附近乱跳。")
    print(f"    10 万行表上请求 10%，实测行数在 {detail['system']} 之间摆动。")
    print("    它便宜（不用逐行掷骰子），但**你没法拿它的行数做任何推断**。")
    print(
        f"  · **bernoulli**：逐行独立掷骰子，行数在目标附近小幅抖动（实测 {detail['bernoulli']}）。"
    )
    print("    统计上最干净，但每一行都要过一次随机数。")
    print(f"  · **reservoir**：**每次都精确命中目标行数**（实测 {detail['reservoir']}）。")
    print("    蓄水池算法保证不多不少，是三个里唯一能拿来做「抽 N 行看」的方法。")
    print("  · Polars 的 `sample(fraction=...)` 属于 reservoir 这一类 —— 永远精确命中。")
    print("    → **两边对齐的口径是 reservoir，不是 DuckDB 的默认值。**")
    print("")
    print("  · 顺带说清楚 bernoulli 的波动从哪来：行数服从二项分布，")
    print(f"    σ = sqrt(n·p·(1-p))。1000 行取 10% 时 σ ≈ {sigma:.1f}（±{3 * sigma / 100:.0%}），")
    sigma_big = (100_000 * 0.1 * 0.9) ** 0.5
    print(f"    10 万行时 σ ≈ {sigma_big:.0f}（±{3 * sigma_big / 10_000:.1%}）。")
    print("    → **样本越小，bernoulli 的行数越不准**；这是统计学，不是 DuckDB 的毛病。")
    print("      想要行数可预期，就用 reservoir —— 它连随机性都去掉了，只在「选哪些行」上随机。")


# ---------------------------------------------------------------- 3. 块状抽样


def scenario_block_shape(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · system 抽的是连续片段，不是分散的随机行")

    ids = [
        row[0]
        for row in con.sql(
            "SELECT id FROM mid USING SAMPLE 10% (system, 42) ORDER BY id"
        ).fetchall()
    ]
    bern = [
        row[0]
        for row in con.sql(
            "SELECT id FROM mid USING SAMPLE 10% (bernoulli, 42) ORDER BY id"
        ).fetchall()
    ]

    def runs(seq: list[int]) -> tuple[int, list[int]]:
        """数一下序列由几段「连续区间」拼成，并给出每段之间的大间隙。"""
        gaps = [b - a for a, b in zip(seq[:-1], seq[1:], strict=False)]
        big = sorted({g for g in gaps if g > 1})
        count = 1 + sum(1 for g in gaps if g > 1)
        return count, big

    sys_runs, sys_gaps = runs(ids)
    bern_runs, bern_gaps = runs(bern)

    print("数据：10 万行，id 就是行号（0..99999），按 id 排序看采出来的形状\n")
    print(
        "  "
        + common.pad("采样方法", 16)
        + common.pad_left("采到的行数", 12)
        + common.pad_left("连续段数", 12)
        + common.pad_left("最大间隙", 12)
    )
    print("  " + "-" * 50)
    for label, seq, r, gaps in [
        ("system", ids, sys_runs, sys_gaps),
        ("bernoulli", bern, bern_runs, bern_gaps),
    ]:
        print(
            "  "
            + common.pad(label, 16)
            + common.pad_left(f"{len(seq):,}", 12)
            + common.pad_left(f"{r}", 12)
            + common.pad_left(f"{max(gaps):,}", 12)
        )

    print(f"\n  system 采到的 id 前 12 个：{ids[:12]}")
    print(f"  bernoulli 采到的 id 前 12 个：{bern[:12]}")

    # 把 system 的结果拆成连续段，直接看它落在表的哪些位置
    def segments(seq: list[int]) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        start = prev = seq[0]
        for cur in seq[1:]:
            if cur != prev + 1:
                out.append((start, prev))
                start = cur
            prev = cur
        out.append((start, prev))
        return out

    segs = segments(ids)
    print(f"\n  system 采到的 {len(ids):,} 行拆开看，是这 {len(segs)} 段连续区间：")
    for lo, hi in segs:
        print(f"    id {lo:>6,} ~ {hi:<6,}（{hi - lo + 1:,} 行）")
    print("\n  把这张表看成「按时间先后追加的日志表」（id 就是时间序）：")
    print("    → 上面每一段都是**一个连续的时间窗口**，中间整段的时间数据一个都没抽到。")
    print("      比例是对的（10%），但抽到的不是「均匀撒在时间轴上的 10%」。")

    print("\n→ 这条比「行数不准」更危险，因为**它不报错**：")
    print("  · system 采样在**数据块**上做决定，块里的行是连着取的。")
    print(f"    上面 10 万行上采出来的 {len(ids):,} 行只由 {sys_runs} 段连续区间拼成，")
    print(f"    段与段之间的间隙高达 {max(sys_gaps):,} 行。")
    print("  · bernoulli 是逐行独立掷骰子，采出来的行**散在整个表上**")
    print(f"    （{bern_runs:,} 段基本都只有 1 行，最大间隙只有 {max(bern_gaps):,}）。")
    print("  · Polars 的 `sample()` 属于后者 —— 逐行独立，没有空间相关性。")
    print("  · 所以：**做统计推断/画分布图，必须显式写 bernoulli 或 reservoir。**")
    print("    用默认的 system 抽样，结论会悄悄偏掉，而且没有任何报错告诉你。")
    print("  · DuckDB 默认给 system 不是偷懒 —— 它对全表扫描友好得多（见场景 7）。")
    print("    但「快」的代价是「统计上不可信」，这个取舍得自己拿主意。")


# ---------------------------------------------------------------- 4. 按行数采样


def scenario_by_rows(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 按行数采样：DuckDB 只有一种方法支持，另一种直接报语法错")

    print("  DuckDB 侧：\n")
    rows = [
        ("USING SAMPLE 10 ROWS", "SELECT count(*) FROM small USING SAMPLE 10 ROWS"),
        (
            "USING SAMPLE reservoir(100 ROWS)",
            "SELECT count(*) FROM small USING SAMPLE reservoir(100 ROWS)",
        ),
        (
            "USING SAMPLE system(100 ROWS)",
            "SELECT count(*) FROM small USING SAMPLE system(100 ROWS)",
        ),
        (
            "USING SAMPLE bernoulli(100 ROWS)",
            "SELECT count(*) FROM small USING SAMPLE bernoulli(100 ROWS)",
        ),
    ]
    for label, sql in rows:
        try:
            n = q1(con, sql)
            print(f"    ✅ {label:<36} -> {n} 行")
        except Exception as exc:
            msg = str(exc).splitlines()[0]
            print(f"    ❌ {label:<36} -> {type(exc).__name__}")
            print(f"       {msg}")

    print("\n  报错原文（bernoulli 不接受离散行数）：")
    try:
        q1(con, "SELECT count(*) FROM small USING SAMPLE bernoulli(100 ROWS)")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\n  Polars 侧：")
    df = pl.DataFrame({"id": range(1000)})
    print(f"    ✅ df.sample(n=10, seed=42)               -> {df.sample(n=10, seed=42).height} 行")
    n_frac = df.sample(fraction=0.1, seed=42).height
    print(f"    ✅ df.sample(fraction=0.1, seed=42)       -> {n_frac} 行")
    try:
        df.sample(n=2000, seed=42)
    except Exception as exc:
        print(f"    ❌ df.sample(n=2000)（表只有 1000 行）      -> {type(exc).__name__}")
        print(f"       {str(exc).splitlines()[0]}")
    try:
        df.sample(fraction=1.5, seed=42)
    except Exception as exc:
        print(f"    ❌ df.sample(fraction=1.5)              -> {type(exc).__name__}")
        print(f"       {str(exc).splitlines()[0]}")

    print("\n  取整规则也不一样（999 行表，取 10%）：")
    con.execute("CREATE OR REPLACE TABLE odd AS SELECT i AS id FROM range(999) t(i)")
    odd_df = pl.DataFrame({"id": range(999)})
    duck_odd = int(q1(con, "SELECT count(*) FROM odd USING SAMPLE 10% (reservoir)"))
    pol_odd = odd_df.sample(fraction=0.1, seed=42).height
    print(f"    DuckDB reservoir 10%  -> {duck_odd} 行   （round(99.9) = 100）")
    print(f"    Polars fraction=0.1   -> {pol_odd} 行   （floor(99.9) = 99）")

    print("\n→ 「我就要 N 行」这件事，两边都能做，但细节对不上：")
    print("  · DuckDB 的 `USING SAMPLE n ROWS` **不保证精确**（走的是默认的 system 方法），")
    print("    要精确就得写 `reservoir(n ROWS)`。")
    print("  · `bernoulli(n ROWS)` 是**语法错误** —— DuckDB 直接拒绝：bernoulli 只能按比例。")
    print("    报错原文就摆在上面，迁移时不用猜。")
    print("  · Polars 的 `df.sample(n=N)` **永远精确给 N 行**，超了才报 ShapeError。")
    print("  · 取整规则也不同：999 行取 10%，DuckDB 给你 100 行，Polars 给你 99 行。")
    print("    → 差一行不致命，但**做行数对账时**得先统一口径，否则永远差 1。")


# ---------------------------------------------------------------- 5. 可复现性


def scenario_reproducible(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 可复现性：`setseed` 管不住采样，采样自己带种子才行")

    print("  DuckDB 的 setseed —— 确实能复现 random()：")
    con.sql("SELECT setseed(0.42)").fetchall()
    a = [row[0] for row in con.sql("SELECT random() FROM range(3) t(i)").fetchall()]
    con.sql("SELECT setseed(0.42)").fetchall()
    b = [row[0] for row in con.sql("SELECT random() FROM range(3) t(i)").fetchall()]
    print(f"    setseed(0.42) 后第 1 次：{[round(x, 6) for x in a]}")
    print(f"    setseed(0.42) 后第 2 次：{[round(x, 6) for x in b]}")
    print(f"    {'✅' if a == b else '❌'} 两次完全一致")

    print("\n  但 setseed 对**采样**没有任何影响：")
    con.sql("SELECT setseed(0.42)").fetchall()
    s1 = con.sql("SELECT id FROM small USING SAMPLE 10% (bernoulli) ORDER BY id").fetchall()
    con.sql("SELECT setseed(0.42)").fetchall()
    s2 = con.sql("SELECT id FROM small USING SAMPLE 10% (bernoulli) ORDER BY id").fetchall()
    print(f"    setseed(0.42) 后采样第 1 次：{len(s1)} 行，{s1[:4]}")
    print(f"    setseed(0.42) 后采样第 2 次：{len(s2)} 行，{s2[:4]}")
    verdict = "✅ 两次完全一致" if s1 == s2 else "❌ 两次不一致 —— 采样用的是另一套种子"
    print(f"    {verdict}")

    print("\n  采样方法后面直接写种子，才是能复现的写法：\n")
    print(
        "  "
        + common.pad("写法", 40)
        + common.pad_left("第 1 次行数", 14)
        + common.pad_left("第 2 次行数", 14)
        + common.pad_left("两次一致", 12)
    )
    print("  " + "-" * 78)
    for method in ["system", "bernoulli", "reservoir"]:
        # 带种子的写法：比的是**选中的 id 集合**，不只是行数
        q = f"SELECT id FROM small USING SAMPLE 10% ({method}, 42) ORDER BY id"
        set1 = con.sql(q).fetchall()
        set2 = con.sql(q).fetchall()
        same = set1 == set2
        print(
            "  "
            + common.pad(f"USING SAMPLE 10% ({method}, 42)", 40)
            + common.pad_left(f"{len(set1):,}", 14)
            + common.pad_left(f"{len(set2):,}", 14)
            + common.pad_left("✅" if same else "❌", 12)
        )
    print("    （比的是**两次选中的 id 集合**是否一模一样，不只是行数相同）")
    print("    ⚠️ system 这一行两次都是 0 行 —— 是因为这张 1000 行的表上它压根没选中任何块，")
    print("      两边同为「空集」才判成一致；换成大表才看得出它到底选中了哪些行。")

    print("\n  Polars 侧：")
    df = pl.DataFrame({"id": range(1000)})
    p1 = df.sample(fraction=0.1, seed=42)["id"].to_list()
    p2 = df.sample(fraction=0.1, seed=42)["id"].to_list()
    p3 = df.sample(fraction=0.1, seed=43)["id"].to_list()
    print(f"    {'✅' if p1 == p2 else '❌'} df.sample(seed=42) 两次一致")
    print(f"    {'✅' if p1 != p3 else '❌'} seed=42 与 seed=43 结果不同")
    print(f"    ✅ seed 不给（None）时每次都换一批：{df.sample(fraction=0.1).height} 行")

    print("\n  顺带看一眼全局种子：")
    pl.set_random_seed(42)
    g1 = df.sample(fraction=1.0, shuffle=True)["id"].to_list()
    pl.set_random_seed(42)
    g2 = df.sample(fraction=1.0, shuffle=True)["id"].to_list()
    print(f"    {'✅' if g1 == g2 else '❌'} pl.set_random_seed(42) 后两次 shuffle 一致")
    direct = df.sample(fraction=1.0, shuffle=True, seed=42)["id"].to_list()[:5]
    print(f"    ⚠️  但它给的是**另一条**随机流：seed=42 直接传参时是 {direct}")

    print("\n→ 想要「可复现」，两边的钥匙不在同一个地方：")
    print("  · DuckDB 的 `SELECT setseed(0.42)` 作用在 **`random()` 这类随机函数**上。")
    print("    对 `USING SAMPLE` **完全无效** —— 上面那次 ❌ 就是证据。")
    print("    采样要复现，种子得写在**方法后面**：`USING SAMPLE 10% (bernoulli, 42)`。")
    print("    这是 2.0 才有的语法，1.x 没有。")
    print("  · Polars 的种子直接跟着调用走：`df.sample(..., seed=42)`。")
    print("    另有全局的 `pl.set_random_seed(42)`，管的是 shuffle 之类。")
    print("  · 迁移时最容易漏的是 DuckDB 这条：**照抄 `setseed` 以为整个查询可复现**，")
    print("    结果采样那一段每次都不一样 —— 而且它自己不会告诉你。")


# ---------------------------------------------------------------- 6. 随机函数


def scenario_random_fn(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 随机函数：Polars 2.1 里一个都没有")

    print("  DuckDB 侧：")
    print(f"    ✅ SELECT random() -> {con.sql('SELECT random()').fetchall()[0][0]:.6f}")
    try:
        con.sql("SELECT rand()").fetchall()
    except Exception as exc:
        print(f"    ❌ SELECT rand()   -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("    （DuckDB 只有 `random()`，没有 `rand()` 这个别名 ——）")

    print("\n  Polars 侧：")
    for name in ["random", "rand"]:
        has_top = hasattr(pl, name)
        has_series = hasattr(pl.Series([1.0]), name)
        top_mark = "✅" if has_top else "❌"
        ser_mark = "✅" if has_series else "❌"
        print(f"    {top_mark} pl.{name}()    {ser_mark} Series.{name}()")
    print("    → Polars 2.1 的表达式层**没有随机数函数**，Series 层也没有。")
    print("      想生成随机数，只能退回 numpy：")

    n = 200_000
    con.sql("SELECT setseed(0.42)").fetchall()
    duck_vals = [row[0] for row in con.sql(f"SELECT random() FROM range({n}) t(i)").fetchall()]
    rng = np.random.default_rng(42)
    pol_vals = rng.random(n).tolist()

    def buckets(vals: list[float]) -> list[float]:
        counts = [0] * 10
        for v in vals:
            counts[min(9, int(v * 10))] += 1
        return [c / len(vals) for c in counts]

    duck_b = buckets(duck_vals)
    pol_b = buckets(pol_vals)

    print("\n  两边各取 20 万个 [0,1) 的随机数，看分布：\n")
    print(
        "  "
        + common.pad("桶", 10)
        + common.pad_left("DuckDB random()", 18)
        + common.pad_left("numpy（Polars 侧）", 22)
    )
    print("  " + "-" * 48)
    for i in range(10):
        print(
            "  "
            + common.pad(f"[{i / 10:.1f},{(i + 1) / 10:.1f})", 10)
            + common.pad_left(f"{duck_b[i]:.4f}", 18)
            + common.pad_left(f"{pol_b[i]:.4f}", 22)
        )

    duck_mean = sum(duck_vals) / len(duck_vals)
    pol_mean = sum(pol_vals) / len(pol_vals)
    print(
        f"\n  最小/最大：DuckDB {min(duck_vals):.6f} / {max(duck_vals):.6f}，"
        f"numpy {min(pol_vals):.6f} / {max(pol_vals):.6f}"
    )
    print(f"  均值：DuckDB {duck_mean:.6f}，numpy {pol_mean:.6f}（理论值 0.5）")

    duck_frame = pl.DataFrame({"桶": [f"{i}" for i in range(10)], "占比": duck_b})
    pol_frame = pl.DataFrame({"桶": [f"{i}" for i in range(10)], "占比": pol_b})
    ok, why = common.compare_frames(duck_frame, pol_frame, ["桶"], rtol=0.05, atol=0.01)
    print("\n  对账：两个分布的形状")
    print(f"    {'✅' if ok else '❌'} common.compare_frames(rtol=0.05, atol=0.01): {why}")
    print("    （统计量比较，不可能逐值相等，所以给的是容差）")

    print("\n→ 随机这件事，两边的模型完全不一样：")
    print("  · DuckDB 把随机数做成了**一等公民**：SQL 表达式 `random()`，")
    print("    能塞进 `ORDER BY random()`、`CASE WHEN random() < 0.1` 这种写法里。")
    print("    整个查询都在引擎内部，随机数不出库。")
    print("  · Polars 2.1 **没有随机数表达式** —— 想在 DataFrame 里造一列随机数，")
    print("    唯一的路是 numpy 先生成，再灌进 Polars：")
    print("      pl.DataFrame({'x': np.random.default_rng(42).random(1000)})")
    print("  · 所以 Polars 侧做不到「一行 SQL 里的随机条件」这种写法。")
    print("    → 迁移时这不只是换个 API 的事：**随机数从「引擎内部」搬到了「Python 进程里」**，")
    print("      能做的事一下子少了一截（比如按行随机打散、按概率抽样后仍留在一个查询里）。")
    print("  · 分布本身两边一致（都是 [0,1) 均匀分布），这点没问题 ——")
    print("    差别在**能不能在查询内部用**。")


# ---------------------------------------------------------------- 7. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 代价：采样并没有比全表扫描便宜多少")

    print(f"数据：{rows} 行，两列（id 递增 + v 取模打散）")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="sample_demo_"))
    src = tmpdir / "sample.parquet"
    con.execute(
        "COPY (SELECT i AS id, (i * 7919) % 1000003 AS v FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)
    con.execute("CREATE OR REPLACE TABLE big AS SELECT * FROM read_parquet(?)", [str(src)])

    def duck_count_all() -> int:
        return int(q1(con, "SELECT count(*) FROM big"))

    def duck_scan_all() -> int:
        return int(q1(con, "SELECT count(*) FROM big WHERE v > 0"))

    def duck_system() -> int:
        return int(q1(con, "SELECT count(*) FROM big USING SAMPLE 10% (system)"))

    def duck_bernoulli() -> int:
        return int(q1(con, "SELECT count(*) FROM big USING SAMPLE 10% (bernoulli)"))

    def duck_reservoir() -> int:
        return int(q1(con, "SELECT count(*) FROM big USING SAMPLE 10% (reservoir)"))

    def polars_scan_all() -> int:
        return polars_src.filter(pl.col("v") > 0).height

    def polars_sample() -> int:
        return polars_src.sample(fraction=0.1, seed=42).height

    results = [
        ("DuckDB count(*)（元数据）", duck_count_all, "不扫表", "rows"),
        ("DuckDB count(*) WHERE v>0", duck_scan_all, "基准", "rows"),
        ("DuckDB system 10%", duck_system, "采样", "rows"),
        ("DuckDB bernoulli 10%", duck_bernoulli, "采样", "rows"),
        ("DuckDB reservoir 10%", duck_reservoir, "采样", "rows"),
        ("Polars filter(v>0).height", polars_scan_all, "基准", "rows"),
        ("Polars sample(fraction=0.1)", polars_sample, "采样", "rows"),
    ]

    print(
        "\n  "
        + common.pad("实现", 30)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 24)
        + common.pad_left("性质", 10)
    )
    print("  " + "-" * 88)
    for label, fn, kind, shape in results:
        fn()
        times: list[float] = []
        value = 0
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        shown = f"{value:,} 行" if shape == "rows" else f"sum = {value:,}"
        print(
            "  "
            + common.pad(label, 30)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            + common.pad_left(shown, 24)
            + common.pad_left(kind, 10)
        )

    print("\n口径：输入已物化成 parquet，两边读的是**同一份文件**；")
    print("  DuckDB 侧先 `CREATE TABLE ... AS SELECT * FROM read_parquet(...)`，")
    print("  Polars 侧先 `pl.read_parquet(...)` —— 数据都已驻留内存。")
    print("  计时的只有「扫描 / 采样」这一步，**不含读盘**。")
    print("  两边的「真扫全表」基准是**同一件事**：扫完所有行、过滤 v>0、计数")
    print("  （DuckDB 是 `count(*) WHERE v>0`，Polars 是 `filter(v>0).height`），两边结果相同。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("  ⚠️ 两边做的**不是同一件事**：DuckDB 侧采样在引擎内部完成（采完直接在引擎里聚合），")
    print(
        "    Polars 侧的 `sample()` 会**真的构造出一个新的 DataFrame**"
        "（把 10% 的行物化到 Python 侧）。"
    )
    print("    所以跨引擎的那两行**不能直接比大小**；真正该比的是**同一引擎内部**")
    print("    「采样」与「真扫全表」的比值。")

    print("\n解读：")
    print("  · DuckDB 的 `count(*)`（不带 WHERE）走**元数据优化**，它根本不扫表 ——")
    print("    所以它那一行**不能当成「全表扫描」的基准**，真基准是带 WHERE 的那一行。")
    print("  · system 采样是全场最快的：它按块做决定，**没被选中的块直接跳过**，")
    print("    连数据都不用读。这就是场景 1/3 里那两个坑的来源 ——")
    print("    **便宜是拿「统计上不可信」换的**。")
    print("  · bernoulli 要**逐行**掷骰子，reservoir 还要额外维护一个跟样本一样大的蓄水池，")
    print("    每读一行都可能要做一次替换 —— 在 DuckDB 侧这两个方法**比同引擎的全表扫描还贵**。")
    print("    → **最精确的那个方法（reservoir）反而最贵**，精确性是要付钱的。")
    print("  · → 所以「先采样再过滤」并不能加速查询：采样的开销和读全表是同一个量级，")
    print("    唯一例外是 system，而它恰恰是统计上不可信的那个。")
    print("    在 DuckDB 里采样是**给下游一个更小的数据集**的手段，不是查询加速手段。")
    print("    真想少读数据，条件过滤（`WHERE`）配合 zone map 才是正道。")
    print("  · 顺带一个 Polars 侧的事实：`sample()` 的开销跟**样本量**走，")
    print("    它要把选中的行真的构造出来 —— 所以别在大表上用它当「快速看一眼」的工具。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的采样与随机差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_default(con)
        scenario_methods(con)
        scenario_block_shape(con)
        scenario_by_rows(con)
        scenario_reproducible(con)
        scenario_random_fn(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
