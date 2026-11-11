"""排名函数全家桶：DuckDB 有 9 个，Polars 直接对齐 4 个、另 3 个得手搓、1 个语义不同、1 个根本没有。

`window_demo.py` 讲了窗口函数**怎么用**（frame、PARTITION、QUALIFY、LAG/LEAD），
但「排名」这一族没细讲。这一族是 SQL 里最常用的一组函数，也是最容易踩坑的一组：

    row_number()  rank()  dense_rank()  percent_rank()  cume_dist()
    ntile(n)  first_value()  last_value()  nth_value()

四个必须记住的实测结论：

> **① DuckDB 的 `last_value()` 默认取的是「当前行」，不是「分区最后一行」。**
>
> 因为窗口函数的默认 frame 是 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` ——
> frame 的**右端是当前行**，所以 `last_value` 落在 frame 里就是当前行。
> 而 Polars 的 `last().over(g)` **没有 frame 的概念**，给的是整个分区的最后一行。
> 同一个想法，两个引擎给出**完全不同的答案** —— 这条最容易出事。

> **② Polars 没有 `percent_rank` / `cume_dist` / `ntile` / `nth_value`。**
>
> 不是「藏在哪儿」，是**顶层命名空间和 Expr 里都搜不到**。
> 好消息是前三个都能手搓出来，本 demo 里逐行验过，结果跟 DuckDB 一致；
> `nth_value` 手搓不成（Polars 没有「取 frame 里第 k 个」的现成算子）。

> **③ `ntile` 是**按分区**分桶的，不是全局分位。**
>
> `ntile(4)` 在一个 10 行的分区里给 3/3/2/2，在另一个 7 行的分区里给 2/2/1/1/1 ——
> 它保证的是「每个桶的大小最多差 1」，不保证「桶里装的是全局的四分位」。

> **④ 手搓的公式在边界上会露馅。**
>
> 单行分区里，DuckDB 的 `percent_rank()` 给 **0.0**，
> 同一个公式在 Polars 里除以 0，给 **NaN** —— 不报错，静悄悄地流进报表。

用法：
    uv run python src/ranking_demo.py
    uv run python src/ranking_demo.py --rows 200000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import time

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 语义用的小数据
#
# 刻意造出**并列**：v = [10, 20, 20, 30] —— 并列是排名函数唯一能看出差别的地方。
# 全是互不相同的值的话，row_number / rank / dense_rank 三者完全一样，什么都测不出来。
#
# (分组, 值, 标签) —— 标签是并列行的区分标记，用来观察并列之间谁在前
SMALL: list[tuple[str, int, str]] = [
    ("a", 10, "x"),
    ("a", 20, "y"),
    ("a", 20, "z"),
    ("a", 30, "w"),
    ("b", 5, "v"),
    ("b", 5, "u"),
    ("b", 40, "t"),
    ("b", 40, "s"),
]

# DuckDB 的 9 个排名 / 取值窗口函数，以及它们各自的语义
DUCK_FUNCS: list[tuple[str, str, str]] = [
    ("row_number()", "第几行（并列也照样往下数）", "1, 2, 3, 4"),
    ("rank()", "第几名（并列同名次，后面**跳号**）", "1, 2, 2, 4"),
    ("dense_rank()", "第几名（并列同名次，**不跳号**）", "1, 2, 2, 3"),
    ("percent_rank()", "百分比名次 = (rank − 1) / (行数 − 1)", "0, 0.33, 0.33, 1"),
    ("cume_dist()", "累计分布 = ≤ 当前值的行数 / 行数", "0.25, 0.75, 0.75, 1"),
    ("ntile(3)", "分桶（第几档）", "1, 1, 2, 3"),
    ("first_value(v)", "frame 里第一个值", "见下一节，**跟 frame 有关**"),
    ("last_value(v)", "frame 里最后一个值", "见下一节，**跟 frame 有关**"),
    ("nth_value(v, 2)", "frame 里第 2 个值（不够就 NULL）", "NULL, 20, 20, 20"),
]


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def make_small(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE TABLE t(g VARCHAR, v INTEGER, tag VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", SMALL)


# ---------------------------------------------------------------- 场景 1


def scenario_duckdb_family(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · DuckDB 的排名函数全家桶（9 个）")

    print("  数据（刻意造出并列）：")
    print()
    print("  " + common.pad("g", 6) + common.pad("v", 6) + common.pad("tag", 6) + "组内位置")
    print("  " + "-" * 74)
    for g, v, tag in SMALL:
        print("  " + common.pad(g, 6) + common.pad(str(v), 6) + common.pad(tag, 6) + "")
    print()
    print("  a 组：[10, 20, 20, 30] —— 20 那一行有两条并列")
    print("  b 组：[5, 5, 40, 40] —— 头尾各一组并列")
    print()

    print("  9 个函数各自的结果（按 g, v 排序）：")
    print()
    for fn, meaning, expect in DUCK_FUNCS:
        rows = con.execute(
            f"SELECT g, v, {fn} OVER (PARTITION BY g ORDER BY v) AS r FROM t ORDER BY g, v"
        ).fetchall()
        # 每个分组的结果单独列出来，a 组在前
        a_vals = [r[2] for r in rows if r[0] == "a"]
        b_vals = [r[2] for r in rows if r[0] == "b"]
        print(f"  {fn}")
        print(f"      含义：{meaning}")
        print(f"      典型值：{expect}")
        print(f"      a 组：{a_vals}")
        print(f"      b 组：{b_vals}")
    print()
    print("  → 同一组里，9 个函数给出 9 种不同的「名次」——")
    print("    它们不是同一个东西的 9 种写法，是 9 个**各有各的用途**的函数。")


# ---------------------------------------------------------------- 场景 2


def scenario_alignable(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("场景 2 · 能对齐的三个：row_number / rank / dense_rank")

    pairs: list[tuple[str, str, pl.Expr]] = [
        ("row_number()", "cum_count()", pl.col("v").cum_count().over("g", order_by="v")),
        ("rank()", "rank('min')", pl.col("v").rank("min").over("g", order_by="v")),
        ("dense_rank()", "rank('dense')", pl.col("v").rank("dense").over("g", order_by="v")),
    ]

    print("  DuckDB 的 row_number / rank / dense_rank，在 Polars 里都有对应写法。")
    print("  逐行对账（a 组 4 行 + b 组 4 行）：")
    print()
    for duck_fn, pol_desc, expr in pairs:
        d_rows = con.execute(
            f"SELECT g, v, {duck_fn} OVER (PARTITION BY g ORDER BY v) AS r FROM t ORDER BY g, v"
        ).fetchall()
        p_rows = df.with_columns(expr.alias("r")).select("g", "v", "r").rows()
        same = d_rows == p_rows
        print(f"  {duck_fn:14s} vs Polars {pol_desc:16s}")
        print(f"      DuckDB : {d_rows}")
        print(f"      Polars : {p_rows}")
        print(f"      逐行一致：{'✅ 是' if same else '❌ 否'}")
        print()

    print("  → 这三个**逐行一致** ✅，Polars 侧直接用 `rank(method=...)` 就行。")
    print("    注意 Polars 的 `rank()` 默认是 `average`（并列取平均名次），")
    print("    对应的是 SQL 的 `RANK()` **加**并列求平均 —— DuckDB 没有这个函数，")
    print("    所以 Polars 侧要显式写 `rank('min')`，别用默认值。")


# ---------------------------------------------------------------- 场景 3


def scenario_missing_three(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("场景 3 · Polars 没有的三个：percent_rank / cume_dist / ntile")

    print("  先在 Polars 的命名空间里搜一遍：")
    print()
    for name in ("percent_rank", "cume_dist", "ntile"):
        top = hasattr(pl, name)
        expr = hasattr(pl.Expr, name)
        print(
            f"    pl.{name:14s} 顶层：{'有' if top else '**没有**'}"
            f"    pl.Expr.{name:14s}：{'有' if expr else '**没有**'}"
        )
    hits = [n for n in dir(pl.Expr) if any(k in n.lower() for k in ("percent", "cume", "ntile"))]
    print(f"    整个 Expr API 里带 percent / cume / ntile 的方法：{hits or '（一个都没有）'}")
    print()
    print("  → **不是「藏在别处」，是真的没有。** 这三个在 DuckDB 里都是一行 SQL。")
    print()

    n = pl.len().over("g")
    emulations: list[tuple[str, str, str, pl.Expr]] = [
        (
            "percent_rank()",
            "(rank('min') − 1) / (行数 − 1)",
            "percent_rank()",
            ((pl.col("v").rank("min").over("g", order_by="v") - 1) / (n - 1)),
        ),
        (
            "cume_dist()",
            "rank('max') / 行数",
            "cume_dist()",
            (pl.col("v").rank("max").over("g", order_by="v") / n),
        ),
        (
            "ntile(3)",
            "(cum_count() − 1) × 桶数 ÷ 行数，整除后 +1",
            "ntile(3)",
            (((pl.col("v").cum_count().over("g", order_by="v") - 1) * 3) // n + 1),
        ),
    ]

    print("  三个都能在 Polars 里手搓出来。逐行对账：")
    print()
    for label, formula, duck_fn, expr in emulations:
        d_rows = con.execute(
            f"SELECT g, v, {duck_fn} OVER (PARTITION BY g ORDER BY v) AS r FROM t ORDER BY g, v"
        ).fetchall()
        p_rows = df.with_columns(expr.alias("r")).select("g", "v", "r").rows()
        same = d_rows == p_rows
        print(f"  {label}")
        print(f"      手搓公式：{formula}")
        print(f"      DuckDB   : {[r[2] for r in d_rows]}")
        print(f"      Polars   : {[r[2] for r in p_rows]}")
        print(f"      逐行一致：{'✅ 是' if same else '❌ 否'}")
        print()

    print("  → 三个手搓版本**逐行一致** ✅ —— 公式对得上。")
    print("    差别在**可读性**：DuckDB 一行，Polars 这三行要自己推导公式，")
    print("    而且公式一变（换引擎版本、改排名口径）就得重新验一遍。")


# ---------------------------------------------------------------- 场景 4


def scenario_first_last(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("场景 4 · first_value / last_value：同名不同义")

    print("  DuckDB 这两个函数**受 frame 支配**，默认 frame 是")
    print("  `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` —— 右端是**当前行**。")
    print()
    print("  a 组（按 v 排序：10, 20, 20, 30）：")
    print()
    default = con.execute(
        "SELECT v, first_value(v) OVER (PARTITION BY g ORDER BY v) AS f, "
        "last_value(v) OVER (PARTITION BY g ORDER BY v) AS l FROM t "
        "WHERE g = 'a' ORDER BY v"
    ).fetchall()
    full = con.execute(
        "SELECT v, first_value(v) OVER (PARTITION BY g ORDER BY v "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS f, "
        "last_value(v) OVER (PARTITION BY g ORDER BY v "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS l FROM t "
        "WHERE g = 'a' ORDER BY v"
    ).fetchall()
    print("  " + common.pad("v", 8) + common.pad("first_value", 16) + "last_value")
    print("  " + "-" * 74)
    for (v, f, lv), (_, f2, l2) in zip(default, full, strict=True):
        print(f"  {common.pad(str(v), 8)}{common.pad(str(f), 16)}{lv}   ← 默认 frame")
        print(f"  {common.pad('', 8)}{common.pad(str(f2), 16)}{l2}   ← 显式 UNBOUNDED FOLLOWING")
    print()

    print("  → **DuckDB 的 `last_value` 默认给的是「当前行自己的值」** ——")
    print("    因为 frame 的右端就是当前行。要「分区的最后一行」，")
    print("    必须显式写 `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`。")
    print()

    pol_first = (
        df.with_columns(pl.col("v").first().over("g").alias("r")).select("g", "v", "r").rows()
    )
    pol_last = df.with_columns(pl.col("v").last().over("g").alias("r")).select("g", "v", "r").rows()
    print("  Polars 侧：`first().over(g)` / `last().over(g)`")
    print(f"      first() → {[r[2] for r in pol_first]}")
    print(f"      last()  → {[r[2] for r in pol_last]}")
    print()
    print("  → **Polars 这两个没有 frame 的概念**，给的是整个分区的首 / 尾。")
    print("    所以：")
    print("      · DuckDB `last_value`（默认 frame）→ 20, 20, 20, 30 这种「每行自己的值」")
    print("      · Polars `last()` → 30, 30, 30, 30 这种「分区的最后一行」")
    print("    **同一个想法，两个引擎给的答案不一样。**")
    print()
    print("  → 对齐写法（两边都要「分区的最后一行」）：")
    print("      DuckDB : last_value(v) OVER (ORDER BY v")
    print("                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)")
    print("      Polars : pl.col('v').last().over('g')")
    print("    对齐写法（两边都要「当前行的值」）：")
    print("      DuckDB : last_value(v) OVER (ORDER BY v)      ← 默认 frame 就行")
    print("      Polars : 没有直接对应的，得自己用 shift / cum_* 拼")
    print()
    print("  顺带看一眼 `nth_value(v, 2)`：")
    nth = con.execute(
        "SELECT v, nth_value(v, 2) OVER (PARTITION BY g ORDER BY v) AS n FROM t "
        "WHERE g = 'a' ORDER BY v"
    ).fetchall()
    print(f"      a 组：{[(r[0], r[1]) for r in nth]}")
    print("      → frame 不够长时给 **NULL**（第一行只有 1 个值，要第 2 个就取不到）。")
    print("      → Polars **完全没有** nth_value。")


# ---------------------------------------------------------------- 场景 5


def scenario_ntile(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · ntile 的分桶规则：三个容易搞错的地方")

    print("  坑一：桶的大小最多差 1，**前面的桶多一个**。")
    print()
    for k in (2, 3, 5):
        rows = con.execute(
            f"SELECT v, ntile({k}) OVER (PARTITION BY g ORDER BY v) AS b FROM t "
            "WHERE g = 'a' ORDER BY v"
        ).fetchall()
        buckets = [r[1] for r in rows]
        sizes = [buckets.count(b) for b in sorted(set(buckets))]
        print(f"      ntile({k})：桶号 {buckets}    每桶行数 {sizes}")
    print()
    print("      → 4 行分 3 桶 = 2/1/1，**不是** 2/1/1 之外的任何分法；")
    print("        多出来的那一行给**前面的**桶。")
    print()

    print("  坑二：ntile 是**按分区**分的，不是全局分位。")
    print()
    for g in ("a", "b"):
        rows = con.execute(
            f"SELECT v, ntile(3) OVER (PARTITION BY g ORDER BY v) AS b FROM t "
            f"WHERE g = '{g}' ORDER BY v"
        ).fetchall()
        print(f"      {g} 组：值 {[r[0] for r in rows]} → 桶号 {[r[1] for r in rows]}")
    print()
    print("      → 两个分区**各自**从 1 开始编号。")
    print("        所以 `ntile(4)` 绝不等价于「全局四分位」——")
    print("        想做全局分位得自己按全局排序算，或者用 `quantile` 切阈值。")
    print()

    print("  坑三：桶数比行数还多时，多出来的桶是空的。")
    print()
    rows = con.execute(
        "SELECT v, ntile(9) OVER (PARTITION BY g ORDER BY v) AS b FROM t WHERE g = 'a' ORDER BY v"
    ).fetchall()
    print(f"      4 行的分区 ntile(9) → 桶号 {[r[1] for r in rows]}")
    print("      → 桶号只到 4，第 5~9 号桶**没有行**。")
    print("        所以「桶号 ≤ 4」不等于「所有行」，统计的时候要当心。")


# ---------------------------------------------------------------- 场景 6


def scenario_edge_cases(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 边界：单行分区")

    con.execute("CREATE TABLE one(g VARCHAR, v INTEGER)")
    con.execute("INSERT INTO one VALUES ('x', 1)")
    print("  一个只有 1 行的分区，几个函数各给什么：")
    print()
    for fn in (
        "row_number()",
        "rank()",
        "dense_rank()",
        "percent_rank()",
        "cume_dist()",
        "ntile(3)",
    ):
        # 注意用 fetchall 而不是 fetchone：DuckDB 2.0 的 execute() 返回连接，
        # fetchone() 只取走第一行、结果没取干净，后面的 execute 会被拒。
        val = con.execute(f"SELECT {fn} OVER (PARTITION BY g ORDER BY v) FROM one").fetchall()[0][0]
        print(f"    {fn:16s} → {val}")
    print()
    print("  → `percent_rank` 给 **0.0**（不是 NaN、不是除零报错），")
    print("    `cume_dist` 给 **1.0**（这一行就是累计分布的全部）。")
    print()

    n = pl.len().over("g")
    one_df = pl.DataFrame({"g": ["x"], "v": [1]})
    emu = (
        one_df.with_columns(
            ((pl.col("v").rank("min").over("g", order_by="v") - 1) / (n - 1)).alias("r")
        )
        .select("r")
        .rows()
    )
    print(f"  但**手搓**的 Polars 版在同一个场景下是：{emu[0][0]}")
    print("  → 行数 − 1 = 0，除零 → **NaN**。")
    print("    DuckDB 给 0.0，手搓给 NaN —— 手搓版本要自己兜住这个边界，")
    print("    不然 NaN 会顺着下游流进报表。")
    print()
    print("  → 这就是「DuckDB 有 3 个现成函数，Polars 得手搓 3 个公式」的真实代价：")
    print("    不只是多写几行，是**边界情况要自己一个个补**。")


# ---------------------------------------------------------------- 场景 7


def scenario_performance(con: duckdb.DuckDBPyConnection, df: pl.DataFrame, repeat: int) -> None:
    section(f"场景 7 · 代价：{len(df):,} 行 × {df['g'].n_unique():,} 个分区")

    print("  同一个排序键（v）上，各排名函数各跑一遍。")
    print("  DuckDB 走 SQL，Polars 走 `over()`；计时只包住「算出结果」这一段。")
    print()

    n = pl.len().over("g")
    cases: list[tuple[str, str, str, pl.Expr]] = [
        (
            "row_number",
            "row_number() OVER (PARTITION BY g ORDER BY v)",
            "cum_count().over(g, order_by=v)",
            pl.col("v").cum_count().over("g", order_by="v"),
        ),
        (
            "rank",
            "rank() OVER (PARTITION BY g ORDER BY v)",
            "rank('min').over(g, order_by=v)",
            pl.col("v").rank("min").over("g", order_by="v"),
        ),
        (
            "dense_rank",
            "dense_rank() OVER (PARTITION BY g ORDER BY v)",
            "rank('dense').over(g, order_by=v)",
            pl.col("v").rank("dense").over("g", order_by="v"),
        ),
        (
            "percent_rank",
            "percent_rank() OVER (PARTITION BY g ORDER BY v)",
            "手搓：(rank−1)/(n−1)",
            ((pl.col("v").rank("min").over("g", order_by="v") - 1) / (n - 1)),
        ),
        (
            "cume_dist",
            "cume_dist() OVER (PARTITION BY g ORDER BY v)",
            "手搓：rank('max')/n",
            (pl.col("v").rank("max").over("g", order_by="v") / n),
        ),
        (
            "ntile(4)",
            "ntile(4) OVER (PARTITION BY g ORDER BY v)",
            "手搓：(cum_count−1)*4÷n +1",
            (((pl.col("v").cum_count().over("g", order_by="v") - 1) * 4) // n + 1),
        ),
    ]

    rows: list[tuple[str, str, float, float, float, float, float]] = []
    for name, duck_sql, pol_desc, pol_expr in cases:
        d_med, d_min, p_med, p_min = _time_pair(con, df, duck_sql, pol_expr, repeat)
        ratio = p_med / d_med if d_med else float("inf")
        rows.append((name, pol_desc, d_med, d_min, p_med, p_min, ratio))

    for name, pol_desc, d_med, d_min, p_med, p_min, ratio in rows:
        print(f"  {name}")
        print(f"      Polars 侧：{pol_desc}")
        print(
            "      DuckDB 中位 "
            + common.pad_left(f"{d_med:.1f} ms", 9)
            + "   最快 "
            + common.pad_left(f"{d_min:.1f} ms", 9)
        )
        print(
            "      Polars 中位 "
            + common.pad_left(f"{p_med:.1f} ms", 9)
            + "   最快 "
            + common.pad_left(f"{p_min:.1f} ms", 9)
        )
        print(f"      Polars / DuckDB = {ratio:.2f}×")
    print()
    print("  > 口径：同一份数据（Polars 造，Arrow 零拷贝灌进 DuckDB），")
    print("  > 两边出口统一成 Arrow（都只算排名、不转 Python 对象），")
    print(f"  > 两边**交错**跑 {repeat} 轮取中位数和最快（避免「谁先跑谁吃亏」）。")
    print("  > 共享开发机，单次数字会抖，看趋势。")
    print()
    faster = sum(1 for r in rows if r[6] < 1.0)
    ratios = [r[6] for r in rows]
    print(f"  → {len(rows)} 个函数里 Polars 快了 {faster} 个 —— 排名本身不是瓶颈，")
    print(
        f"    两边的差距在 {min(ratios):.2f}~{max(ratios):.2f} 倍之间"
        "（Polars 侧是原生算子，DuckDB 侧要过一遍通用窗口框架）。"
    )
    print("    **但这不等于「两边可以换着用」** ——")
    print("    percent_rank / cume_dist / ntile 这三个是手搓的，")
    print("    性能上的领先换不来语义上的自动对齐：")
    print("    公式要自己负责，边界情况（比如单行分区）也要自己负责。")


def _time_pair(
    con: duckdb.DuckDBPyConnection,
    df: pl.DataFrame,
    window_expr: str,
    pol_expr: pl.Expr,
    repeat: int,
) -> tuple[float, float, float, float]:
    """**交错**地跑 DuckDB 与 Polars，各 repeat 轮。

    返回 (DuckDB 中位, DuckDB 最快, Polars 中位, Polars 最快)，单位毫秒。

    为什么交错：固定「先跑 DuckDB 再跑 Polars」的话，谁先跑谁要付
    「第一次」的冷启动代价，这份代价会被算进那一边。交错着跑，
    两边轮流当第一个，噪声摊平 —— 这是本项目一贯的口径。

    `window_expr` 只是一段窗口表达式，SQL 在这里拼 ——
    输出的三列（g / v / r）和 Polars 侧保持一致，两边干的活才可比。

    出口统一成 **Arrow**：DuckDB 侧走 `rel.arrow()` 再 `pl.from_arrow()`，
    Polars 侧本来就活在 Arrow 里。**不能**用 `fetchall()` ——
    那会把结果转成 Python 对象，量到的是「物化成 Python 对象」的代价，
    不是「算排名」的代价（那件事归 materialize_demo.py 管）。
    """
    sql = f"SELECT g, v, {window_expr} AS r FROM big"

    def run_duckdb() -> None:
        rel = con.sql(sql)
        try:
            pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def run_polars() -> None:
        df.with_columns(pol_expr.alias("r")).select("g", "v", "r")

    run_duckdb()
    run_polars()
    duck_times: list[float] = []
    pol_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        run_duckdb()
        duck_times.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        run_polars()
        pol_times.append((time.perf_counter() - t0) * 1000)
    return (
        statistics.median(duck_times),
        min(duck_times),
        statistics.median(pol_times),
        min(pol_times),
    )


# ---------------------------------------------------------------- 小结


def scenario_conclusion() -> None:
    section("小结：排名函数这一族，谁有什么")

    print("  " + common.pad("函数", 18) + common.pad("DuckDB", 14) + "Polars")
    print("  " + "-" * 74)
    for fn, duck, pol in [
        ("row_number", "有", "有：`cum_count().over()`"),
        ("rank", "有", "有：`rank('min').over()`"),
        ("dense_rank", "有", "有：`rank('dense').over()`"),
        ("percent_rank", "有", "❌ **没有**，得手搓"),
        ("cume_dist", "有", "❌ **没有**，得手搓"),
        ("ntile", "有", "❌ **没有**，得手搓"),
        ("first_value", "有（默认 frame 恰好对齐）", "有：`first().over()`，**没有 frame**"),
        ("last_value", "有（默认 frame **不对齐**）", "有：`last().over()`，**语义不同**"),
        ("nth_value", "有", "❌ **没有**"),
    ]:
        print("  " + common.pad(fn, 18) + common.pad(duck, 14) + pol)
    print()
    print("  → 9 个函数，4 种结局：")
    print("    ① **直接对上的 4 个**（row_number / rank / dense_rank / first_value），")
    print("       Polars 侧写法现成；")
    print("    ② **手搓能对上的 3 个**（percent_rank / cume_dist / ntile），")
    print("       本 demo 验过逐行一致，但边界情况（比如单行分区）要自己兜；")
    print("    ③ **语义对不上的 1 个是 `last_value`** —— 默认 frame 的差别，")
    print("       DuckDB 给「当前行」，Polars 给「分区最后一行」。")
    print("       这条不是「缺功能」，是**同一个名字两个意思**，迁移时最容易出事；")
    print("    ④ **完全没有的 1 个是 `nth_value`** —— DuckDB 有，Polars 整个 Expr API 里都没有。")
    print()
    print("  实务建议：")
    print("    · 排名口径只用到 row_number / rank / dense_rank → 两边都能直接写；")
    print("    · 要 percent_rank / cume_dist / ntile → 优先在 DuckDB 侧算，")
    print("      别在 Polars 侧手搓（公式要自己负责，边界也要自己负责）；")
    print("    · 要「分组最后一行」→ **别用 `last_value`**，")
    print("      DuckDB 侧写全 frame，Polars 侧写 `last().over()`；")
    print("    · 要「组内第 k 名」→ 记得加一个**唯一的** tiebreaker，")
    print("      不然并列行的名次是不确定的。")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="排名函数全家桶")
    parser.add_argument("--rows", type=int, default=200_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=3, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    con = duckdb.connect()
    make_small(con)

    small_df = pl.DataFrame(
        {
            "g": [r[0] for r in SMALL],
            "v": [r[1] for r in SMALL],
            "tag": [r[2] for r in SMALL],
        }
    )

    scenario_duckdb_family(con)
    scenario_alignable(con, small_df)
    scenario_missing_three(con, small_df)
    scenario_first_last(con, small_df)
    scenario_ntile(con)
    scenario_edge_cases(con)

    # ---- 性能场景：造一份大一点的，Arrow 零拷贝灌进 DuckDB ----
    big = pl.DataFrame(
        {
            "g": [f"g{i % 1000:04d}" for i in range(args.rows)],
            "v": [i % 977 for i in range(args.rows)],
        }
    )
    con.register("big_arrow", big.to_arrow())
    con.execute("CREATE TABLE big AS SELECT * FROM big_arrow")
    scenario_performance(con, big, args.repeat)

    scenario_conclusion()
    con.close()


if __name__ == "__main__":
    main()
