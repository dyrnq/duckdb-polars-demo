"""窗口函数专场：frame 的默认值、并列行、时间滑窗、EXCLUDE、动态宽度、命名窗口、QUALIFY 与窗口代价。

窗口函数是「看着一样、算出来不一样」的重灾区：

* DuckDB 不带 frame 的 `OVER (ORDER BY x)` 默认是 **RANGE**（并列行一起算），
  而 Polars 的 `cum_sum()` 永远是 **ROWS**（逐行累计）—— 排序键有并列值时两边分叉。
* 更坑的是：DuckDB 的默认值会**随排序键个数变**。单键是 RANGE，
  一旦写成 `ORDER BY x, ord`（多键），RANGE 表达不了，SQL 就悄悄退化成 ROWS，
  结果跟着变，语句还不报错。
* Polars 侧没有 frame 概念，只有 `cum_sum()` / `shift()` / `rolling_*()` 这一族表达式。
* **时间滑窗的左端点两边不一样**：DuckDB 的 RANGE 是闭区间 [t-Δ, t]，
  Polars 的 `rolling_*_by` 默认 `closed='right'`，也就是左开区间 (t-Δ, t] ——
  正好落在左端点上的那一行，Polars 会少算。
* DuckDB 的 frame 偏移**可以引用列**：`ROWS BETWEEN w PRECEDING AND CURRENT ROW`
  里的 `w` 是一列，每行的窗口宽度可以不一样；Polars 的 `rolling_*` 只吃标量。
* 小数当窗口宽度**不报错**，但取整跟着列的类型走（DOUBLE 五成双、DECIMAL 逢五远离零）。

每个场景都拿同一份小数据并排打印两个引擎的结果，最后给出等价写法。

用法：
    uv run python src/window_demo.py
    uv run python src/window_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import datetime
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 一份故意带并列值的小数据：排序键 x 里有重复。
# ord 是唯一的并列打破键 —— 有了它，行序才是确定的，才能逐值对账。
SMALL = pl.DataFrame(
    {
        "g": ["a", "a", "a", "a", "b", "b", "b"],
        "x": [1, 2, 2, 3, 2, 2, 5],
        "ord": [1, 2, 3, 4, 5, 6, 7],
        "v": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0],
    }
)

# 给时间滑窗用的小表：日期中间有**空档**（01-02 → 01-05 隔了 3 天），
# 末尾 01-06 有两行并列 —— 空档让「窗口真的会收缩」看得见，
# 并列让「RANGE 把同伴一起算进来」看得见。
TIMEY = pl.DataFrame(
    {
        "d": [
            datetime.date(2024, 1, 1),
            datetime.date(2024, 1, 2),
            datetime.date(2024, 1, 5),
            datetime.date(2024, 1, 6),
            datetime.date(2024, 1, 6),
            datetime.date(2024, 1, 10),
        ],
        "v": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
    }
)


# 每行一个窗口宽度：w 是「这一行自己的窗口要回看几行」。
# 用来试 DuckDB 里「frame 偏移引用列」这件事。
WIDTHY = pl.DataFrame(
    {
        "ord": [1, 2, 3, 4, 5, 6],
        "v": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        "w": [0, 1, 2, 1, 3, 0],
    }
)

# 小数宽度专用：前面 20 行是垫片（宽度 0），小数行全放在最后 ——
# 这样窗口左端不会被表头截断，量出来的长度就是真实偏移。
# f 是 DOUBLE，fm 是同样数值的 DECIMAL —— 用来比「同一个数、两种类型」。
FRACY = pl.DataFrame(
    {
        "ord": list(range(1, 21)) + list(range(21, 29)),
        "v": [0.0] * 20 + [1.0] * 8,
        "f": [0.0] * 20 + [0.5, 1.5, 2.5, 3.5, 4.5, 0.6, 1.4, 2.4],
    }
).with_columns(pl.col("f").cast(pl.Decimal(10, 1)).alias("fm"))


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("w", SMALL)
    con.register("tw", TIMEY)
    con.register("wy", WIDTHY)
    con.register("fy", FRACY)
    return con


def duck_window(con: duckdb.DuckDBPyConnection, order_by: str, frame: str = "") -> list[float]:
    """跑一条窗口查询，取出窗口列的值（按 x, ord 输出，行序确定）。"""
    rows = con.sql(
        f"SELECT sum(v) OVER (ORDER BY {order_by} {frame}) AS s FROM w ORDER BY x, ord"
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- 1. 默认 frame


def scenario_default_frame(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 默认 frame 是 RANGE 还是 ROWS？（并列行摊上事）")

    print("同一份数据，只改 ORDER BY 里写几个排序键，窗口结果就变了：\n")

    single_default = duck_window(con, "x")
    single_range = duck_window(con, "x", "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")
    double_default = duck_window(con, "x, ord")
    double_rows = duck_window(con, "x, ord", "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")

    polars_rows = (
        SMALL.with_columns(pl.col("v").cum_sum().over(order_by=["x", "ord"]).alias("s"))
        .sort("x", "ord")["s"]
        .to_list()
    )

    print(f"  DuckDB  ORDER BY x        （单键，不带 frame）: {single_default}")
    print(f"  DuckDB  ORDER BY x        显式 RANGE        : {single_range}")
    print(f"  DuckDB  ORDER BY x, ord   （双键，不带 frame）: {double_default}")
    print(f"  DuckDB  ORDER BY x, ord   显式 ROWS        : {double_rows}")
    print(f"  Polars  cum_sum().over(order_by=[x, ord])  : {polars_rows}")

    print(f"\n  单键默认值 == 显式 RANGE：{'✅' if single_default == single_range else '❌'}")
    print(f"  双键默认值 == 显式 ROWS ：{'✅' if double_default == double_rows else '❌'}")
    print(f"  Polars cum_sum == DuckDB ROWS：{'✅' if polars_rows == double_rows else '❌'}")

    print("\n要点一：DuckDB 不带 frame 的窗口默认是 RANGE —— 排序键相同的「并列行」")
    print("  会一起算进当前行的窗口（peers included）。这里 x=2 有 4 行，")
    print("  这 4 行看到的都是 10+20+30+50+60 = 170。")
    print("  Polars 的 cum_sum() 是逐行累计（等价于 ROWS frame）：第二行看到 10+20=30。")
    print("  → 同样的「累计和」，DuckDB 默认 = " + str(single_default))
    print("    Polars = " + str(polars_rows) + "，排序键没有并列值时才碰巧一致。")

    print("\n要点二：DuckDB 的默认值会随排序键个数变。")
    print("  RANGE frame 只能配单个 ORDER BY 表达式，配多个会直接报错（见场景 2），")
    print("  于是「ORDER BY x」和「ORDER BY x, ord」这两种写法，窗口结果不同 ——")
    print("  后者退化成 ROWS。给排序加一个并列打破键，累计口径就悄悄变了。")

    print("\n所以：想跟 Polars 对齐，SQL 里必须显式写")
    print("  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")
    print("别依赖默认值 —— 它既取决于引擎版本，也取决于你写了一个还是几个排序键。")

    # 顺手展示另一个 Polars 侧的坑：cum_sum() 按当前行序算，sort 之后不会重算
    print("\n顺手一个 Polars 侧的坑（同样不报错）：")
    naive = SMALL.with_columns(pl.col("v").cum_sum().alias("p")).sort("x", "ord")
    print(
        "  cum_sum() 不加 over，按当前行序累计:",
        SMALL.with_columns(pl.col("v").cum_sum())["v"].to_list(),
    )
    print("  然后再 sort('x','ord')，值不会重算 :", naive["p"].to_list())
    print("  → 排序只是搬动了带着旧值走的行，累计量已经对不上了。")


# ---------------------------------------------------------------- 2. frame 的种类


def scenario_frame_kinds(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · frame 有哪些：ROWS / RANGE / GROUPS")

    print("单排序键 ORDER BY x，三种 frame 各是什么口径：\n")
    for label, frame in [
        (
            "ROWS  BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        ),
        (
            "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        ),
        (
            "GROUPS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            "GROUPS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
        ),
        ("ROWS  BETWEEN 1 PRECEDING AND CURRENT ROW", "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW"),
        ("RANGE BETWEEN 1 PRECEDING AND CURRENT ROW", "RANGE BETWEEN 1 PRECEDING AND CURRENT ROW"),
        (
            "GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW",
            "GROUPS BETWEEN 1 PRECEDING AND CURRENT ROW",
        ),
    ]:
        print(f"  DuckDB {label:<54} -> {duck_window(con, 'x', frame)}")

    print("\n口径差别：")
    print("  ROWS   —— 按行号数。1 PRECEDING = 前一行 + 当前行")
    print("  RANGE  —— 按值比。1 PRECEDING = 值落在 [当前值-1, 当前值] 里的所有行")
    print("  GROUPS —— 按组数。1 PRECEDING = 前一组 + 当前组（并列行算一组）")
    print("\n  所以 RANGE 1 PRECEDING 那行，最后一个 x=5 只看到自己（值域 [4,5]），")
    print("  而 ROWS 1 PRECEDING 那行看到的是前一行 + 当前行 —— 两种「前一行」完全不同。")

    print("\n带偏移的 RANGE（1 PRECEDING）只能配单个排序键，配两个直接报错：")
    try:
        duck_window(con, "x, ord", "RANGE BETWEEN 1 PRECEDING AND CURRENT ROW")
        print("  ORDER BY x, ord + RANGE 1 PRECEDING：允许")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"  ORDER BY x, ord + RANGE 1 PRECEDING：{detail}")

    print("\n不带偏移的 RANGE（UNBOUNDED PRECEDING .. CURRENT ROW）配多个排序键是允许的，")
    print("但它的效果等同于 ROWS —— DuckDB 直接按行号算：")
    range_unbounded = duck_window(
        con, "x, ord", "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
    )
    rows_unbounded = duck_window(con, "x, ord", "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")
    print(f"  ORDER BY x, ord + RANGE UNBOUNDED : {range_unbounded}")
    print(f"  ORDER BY x, ord + ROWS  UNBOUNDED : {rows_unbounded}")

    print("\nPolars 侧对应的是表达式家族，而不是 frame 子句：")
    print("  cum_sum()                 -> 逐行累计（ROWS / UNBOUNDED PRECEDING）")
    print("  shift(n).over(...)        -> 相对位移（对应 lag/lead）")
    print(
        "  rolling_*(...).over(...)  -> 固定行数窗口"
        "（对应 ROWS BETWEEN n PRECEDING AND n FOLLOWING）"
    )
    print("  RANGE / GROUPS            -> 没有现成表达式，得自己拼（见场景 6）")


# ---------------------------------------------------------------- 3. 时间滑动窗口


def scenario_time_sliding_window(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 时间滑动窗口（RANGE + INTERVAL）")

    print("「最近 N 天」这种口径，SQL 里对应的是 RANGE frame 配 INTERVAL 偏移：\n")
    print("  RANGE BETWEEN INTERVAL 2 DAY PRECEDING AND CURRENT ROW")
    print("  —— 窗口不是「最近 N 行」，是「时间落在 [当前时间-2天, 当前时间] 里的行」。\n")

    duck_df = pl.from_arrow(
        con.sql(
            """
            SELECT d, v,
                   sum(v) OVER (ORDER BY d
                       RANGE BETWEEN INTERVAL 2 DAY PRECEDING AND CURRENT ROW) AS r2,
                   sum(v) OVER (ORDER BY d
                       RANGE BETWEEN INTERVAL 4 DAY PRECEDING AND CURRENT ROW) AS r4,
                   sum(v) OVER (ORDER BY d, v
                       ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS rowwise
            FROM tw
            ORDER BY d, v
            """
        ).arrow()
    )

    polars_df = TIMEY.with_columns(
        pl.col("v").rolling_sum_by("d", "2d", min_samples=1).alias("p2"),
        pl.col("v").rolling_sum_by("d", "4d", min_samples=1).alias("p4"),
        pl.col("v").rolling_sum_by("d", "4d", min_samples=1, closed="both").alias("p4_both"),
        pl.col("v").rolling_sum(2, min_samples=1).alias("prow"),
    ).sort("d", "v")

    common.print_frame(duck_df, "DuckDB（RANGE 2天 / RANGE 4天 / ROWS 前一行）", max_rows=10)
    common.print_frame(
        polars_df, "Polars（rolling_sum_by 2d / 4d / 4d+closed='both'）", max_rows=10
    )

    d_r2 = duck_df["r2"].to_list()
    d_r4 = duck_df["r4"].to_list()
    d_row = duck_df["rowwise"].to_list()
    p_r2 = polars_df["p2"].to_list()
    p_r4 = polars_df["p4"].to_list()
    p_r4_both = polars_df["p4_both"].to_list()
    p_row = polars_df["prow"].to_list()

    print("\n逐值对账：")
    print(f"  2 天窗口   DuckDB {d_r2}")
    print(f"             Polars {p_r2}   一致：{'✅' if d_r2 == p_r2 else '❌'}")
    print(f"  4 天窗口   DuckDB {d_r4}")
    print(f"             Polars {p_r4}   一致：{'✅' if d_r4 == p_r4 else '❌'}")
    print(f"  前一行     DuckDB {d_row}")
    print(f"             Polars {p_row}   一致：{'✅' if d_row == p_row else '❌'}")

    print("\n要点一：窗口是**真的会收缩**的，不是「最近 N 条」。")
    print("  01-05 那行往前 2 天是 01-03，01-01 和 01-02 都掉出窗口了 → 只剩自己（3）。")
    print("  换成按行数算（ROWS 前一行）就完全不是一回事了：窗口里始终有人。")

    print("\n要点二：时间滑窗的**左端点两边不一样** —— 这是本场景唯一的分叉。")
    print("  DuckDB 的 RANGE 是闭区间 [t-Δ, t]：01-05 往前 4 天正好是 01-01，")
    print("  01-01 在窗口里（1+2+3 = 6）。")
    print("  Polars 的 rolling_sum_by 默认 closed='right'，是左开区间 (t-Δ, t]：")
    print("  正好落在左端点上的 01-01 被排除（只剩 2+3 = 5）。")
    print(
        f"  加 closed='both' 之后 Polars 给出 {p_r4_both}，与 DuckDB 一致："
        f"{'✅' if d_r4 == p_r4_both else '❌'}"
    )

    print("\n  → 差一行，而且不报错：只有当某行**正好**落在 t-Δ 上时才会差。")
    print("    窗口越宽、数据越稀疏，这种行越常见 —— 所以它是那种「小数据看不出、")
    print("    上线才发现」的坑。DuckDB 这边要跟 Polars 对齐，只能靠 Polars 加 closed='both'。")

    print("\nRANGE frame 的类型限制（报错的有两个，能跑的那个有前提）：")
    for label, sql in [
        (
            "RANGE + 多个排序键",
            "SELECT sum(v) OVER (ORDER BY d, v RANGE BETWEEN INTERVAL 2 DAY PRECEDING "
            "AND CURRENT ROW) FROM tw",
        ),
        (
            "DATE 列 + 整数偏移",
            "SELECT sum(v) OVER (ORDER BY d RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) FROM tw",
        ),
        (
            "DATE 列 + 小数偏移",
            "SELECT sum(v) OVER (ORDER BY d RANGE BETWEEN 2.5 PRECEDING AND CURRENT ROW) FROM tw",
        ),
        (
            "数值列 + INTERVAL 偏移",
            "SELECT sum(v) OVER (ORDER BY v RANGE BETWEEN INTERVAL 2 DAY PRECEDING "
            "AND CURRENT ROW) FROM tw",
        ),
    ]:
        try:
            con.execute(sql).fetchall()
            print(f"  {label:22}: 允许")
        except Exception as exc:
            print(f"  {label:22}: {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\n  所以时间滑窗只能配**一个**排序键（想加并列打破键就不行了）；")
    print("  DATE 列配整数偏移是**允许**的，而且这个整数被当成「天」——")
    print("  RANGE 2 PRECEDING 和 RANGE INTERVAL 2 DAY PRECEDING 算出来一模一样。")
    print("  但小数偏移直接炸（DECIMAL 减不了 DATE），数值列配 INTERVAL 也炸。")


# ---------------------------------------------------------------- 4. EXCLUDE


def scenario_exclude(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · EXCLUDE 子句：把窗口里的某些行踢掉")

    print("SQL:2011 里的 frame 排除子句，DuckDB 四种全支持：")
    print("  EXCLUDE CURRENT ROW / EXCLUDE GROUP / EXCLUDE TIES / EXCLUDE NO OTHERS\n")

    base = "PARTITION BY g ORDER BY x ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
    duck_df = pl.from_arrow(
        con.sql(
            f"""
            SELECT g, x, ord, v,
                   sum(v) OVER ({base})                      AS base,
                   sum(v) OVER ({base} EXCLUDE CURRENT ROW)  AS ex_current,
                   sum(v) OVER ({base} EXCLUDE GROUP)        AS ex_group,
                   sum(v) OVER ({base} EXCLUDE TIES)         AS ex_ties
            FROM w
            ORDER BY g, x, ord
            """
        ).arrow()
    )

    common.print_frame(duck_df, "DuckDB 四种 EXCLUDE", max_rows=10)

    d_base = duck_df["base"].to_list()
    d_cur = duck_df["ex_current"].to_list()
    d_group = duck_df["ex_group"].to_list()
    d_ties = duck_df["ex_ties"].to_list()

    print("\n看 g='a' 这一组（x = 1,2,2,3，v = 10,20,30,40，全组和 100）：")
    print(f"  基准（全都在窗口里）        : {d_base[:4]}")
    print(f"  EXCLUDE CURRENT ROW（踢自己）: {d_cur[:4]}")
    print(f"  EXCLUDE GROUP（踢整组）      : {d_group[:4]}")
    print(f"  EXCLUDE TIES（踢同值的同伴） : {d_ties[:4]}")
    print()
    print("  x=2 那两行（v=20、v=30）是同伴，一组的和是 50：")
    print("    EXCLUDE GROUP  → 100-50 = 50（两行都只剩另外两组）")
    print("    EXCLUDE TIES   → 100-30 = 70（踢掉同伴 30，保留自己 20）")
    print("    EXCLUDE CURRENT→ 100-20 = 80（只踢自己）")
    print("  x=1 和 x=3 没有同伴：EXCLUDE GROUP 和 EXCLUDE CURRENT 一样，")
    print("  EXCLUDE TIES 什么都不踢（没同伴可踢）。")

    print("\nPolars 侧**没有** EXCLUDE —— 但这三种都能拼出来：")
    polars_current = SMALL.with_columns(
        (pl.col("v").sum().over("g") - pl.col("v")).alias("ex_current")
    ).sort("g", "x", "ord")
    grouped = SMALL.group_by(["g", "x"], maintain_order=True).agg(pl.col("v").sum().alias("gv"))
    polars_group = (
        SMALL.join(grouped.select("g", "x", "gv"), on=["g", "x"], how="left")
        .with_columns((pl.col("v").sum().over("g") - pl.col("gv")).alias("ex_group"))
        .sort("g", "x", "ord")
    )
    polars_ties = polars_group.with_columns((pl.col("ex_group") + pl.col("v")).alias("ex_ties"))

    p_cur = polars_current["ex_current"].to_list()
    p_group = polars_group["ex_group"].to_list()
    p_ties = polars_ties["ex_ties"].to_list()
    print(f"  EXCLUDE CURRENT ROW：sum().over('g') - 自己                     → {p_cur}")
    print(f"  EXCLUDE GROUP      ：sum().over('g') - 同组和（group_by + join）→ {p_group}")
    print(f"  EXCLUDE TIES       ：EXCLUDE GROUP + 自己                      → {p_ties}")
    print(
        f"\n  与 DuckDB 一致：CURRENT {'✅' if d_cur == p_cur else '❌'}"
        f"  GROUP {'✅' if d_group == p_group else '❌'}"
        f"  TIES {'✅' if d_ties == p_ties else '❌'}"
    )

    print("\n  → DuckDB 一行写完；Polars 要自己 group_by 再 join 回来，多一次聚合 + 一次 join。")
    print("    这就是「有没有这个语法」的差别：不是能不能算，是要多绕两步。")

    print("\n一个语法限制：EXCLUDE 不能挂在命名窗口后面 ——")
    try:
        con.sql(
            "SELECT sum(v) OVER (w EXCLUDE CURRENT ROW) FROM w WINDOW w AS (ORDER BY x)"
        ).fetchall()
        print("  允许")
    except Exception as exc:
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("  窗口规格得整段写出来，命名窗口只能用在「不带 EXCLUDE」的地方。")


# ---------------------------------------------------------------- 5. 动态窗口宽度


def scenario_dynamic_frame(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 窗口宽度可以是参数、表达式、子查询，甚至「每行一个宽度」")

    print("窗口宽度不一定是写死的常量。DuckDB 这几种都实测能跑：\n")

    def run(frame_expr: str, params: list[int] | None = None) -> list[float]:
        sql = (
            "SELECT sum(v) OVER (ORDER BY ord ROWS BETWEEN "
            f"{frame_expr} PRECEDING AND CURRENT ROW) FROM w ORDER BY ord"
        )
        if params is None:
            return [r[0] for r in con.execute(sql).fetchall()]
        return [r[0] for r in con.execute(sql, params).fetchall()]

    const2 = run("2")
    print(f"  常量 2 PRECEDING              : {const2}")
    for p in (1, 2, 3):
        got = run("?", [p])
        same = got == run(str(p))
        print(
            f"  ? PRECEDING，参数传 {p}         : {got}   与写死 {p} 一致：{'✅' if same else '❌'}"
        )
    print(
        f"  表达式 (2+1) PRECEDING         : {run('(2+1)')}   与写死 3 一致："
        f"{'✅' if run('(2+1)') == run('3') else '❌'}"
    )
    print(
        f"  子查询 (SELECT 3) PRECEDING    : {run('(SELECT 3)')}   与写死 3 一致："
        f"{'✅' if run('(SELECT 3)') == run('3') else '❌'}"
    )

    print("\n→ DuckDB 真的在算这几个表达式，不是当成常量糊弄过去的：")
    print("  换参数就换窗口宽度，表达式和子查询都按值算出了对应的行数。")
    print("\n实用价值：窗口宽度可以从配置、参数或表里来，")
    print("  不用拼 SQL 字符串 —— 拼字符串是注入的口子，也是口径失控的源头。")
    print("  （Polars 侧 `rolling_sum(window_size)` 本来就是个普通参数，天然没有这个问题。）")

    # ---------------------------------------------------------------- 每行一个宽度
    print("\n更狠的一条：偏移可以直接引用**列** —— 每行的窗口宽度都不一样。\n")

    width_df = pl.from_arrow(
        con.sql(
            """
            SELECT ord, v, w,
                   sum(v) OVER (ORDER BY ord ROWS BETWEEN w PRECEDING AND CURRENT ROW) AS win,
                   list(v) OVER (ORDER BY ord ROWS BETWEEN w PRECEDING AND CURRENT ROW) AS win_rows,
                   sum(v) OVER (ORDER BY ord ROWS BETWEEN w PRECEDING AND w FOLLOWING) AS win2
            FROM wy
            ORDER BY ord
            """
        ).arrow()
    )
    common.print_frame(width_df, "DuckDB：偏移引用列 w（每行自己的宽度）", max_rows=10)

    d_win = width_df["win"].to_list()
    d_win2 = width_df["win2"].to_list()
    print("\n  每行窗口（w 是这一行要回看几行）：")
    for ordv, v, w, wr in zip(
        width_df["ord"].to_list(),
        width_df["v"].to_list(),
        width_df["w"].to_list(),
        width_df["win_rows"].to_list(),
        strict=True,
    ):
        print(f"    ord={ordv} v={v:5} w={w} → 窗口 {wr} 和 {sum(wr)}")
    print(f"  两端都能引用列（w PRECEDING AND w FOLLOWING）: {d_win2}")

    print("\n  → 同一句 SQL，每行的窗口边界都不同：w=1 的那行只看自己 + 前一行，")
    print("    w=3 的那行看前 4 行。这是「每行一个窗口」的原生写法。")

    print("\nPolars 侧 `rolling_*` 的窗口大小必须是**标量**，传列直接报错：")
    try:
        WIDTHY.with_columns(pl.col("v").rolling_sum(pl.col("w"), min_samples=1).alias("r"))
        print("    竟然能跑")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("  但能自己拼：前缀和 + 按行 gather（窗口 = 前缀和差）")
    manual = (
        WIDTHY.with_row_index("i")
        .with_columns(pl.col("v").cum_sum().alias("cs"))
        .with_columns(pl.col("cs").shift(1).fill_null(0.0).alias("pre"))
        .with_columns((pl.col("i") - pl.col("w")).clip(lower_bound=0).alias("start"))
        .with_columns((pl.col("cs") - pl.col("pre").gather(pl.col("start"))).alias("manual"))
    )
    p_manual = manual["manual"].to_list()
    print(f"    Polars 手拼 : {p_manual}")
    print(f"    DuckDB      : {d_win}   一致：{'✅' if p_manual == d_win else '❌'}")
    print("  → 一样能算出来，但 DuckDB 一行、Polars 要前缀和 + 索引 gather 四步。")

    # ---------------------------------------------------------------- 边界与坑
    print("\n两个边界（一个报错，一个静默）：")

    print("\n  1) 负数宽度 → 直接报错：")
    neg = WIDTHY.with_columns(pl.Series("w", [-1, 1, 2, 1, 3, 0]))
    con.register("wy_neg", neg)
    try:
        con.execute(
            "SELECT sum(v) OVER (ORDER BY ord ROWS BETWEEN w PRECEDING AND CURRENT ROW) FROM wy_neg"
        ).fetchall()
        print("    竟然能跑")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\n  2) 小数宽度 → 不报错，但取整口径跟着**列的类型**走：")
    # 窗口必须在子查询里算完再过滤 —— WHERE 先于窗口函数生效，
    # 直接在外层过滤会把垫片行剔掉，窗口左端被截断，量出来的偏移是假的。
    frac_rows = con.execute(
        """
        SELECT f, win_d, win_m, cast_d, cast_m FROM (
            SELECT ord, f,
                   list(v) OVER (ORDER BY ord ROWS BETWEEN f  PRECEDING AND CURRENT ROW) AS win_d,
                   list(v) OVER (ORDER BY ord ROWS BETWEEN fm PRECEDING AND CURRENT ROW) AS win_m,
                   CAST(f  AS INTEGER) AS cast_d,
                   CAST(fm AS INTEGER) AS cast_m
            FROM fy
        )
        WHERE f <> 0
        ORDER BY ord
        """
    ).fetchall()
    print("      f      DOUBLE 偏移  DECIMAL 偏移  CAST(DOUBLE)  CAST(DECIMAL)")
    measured = []
    for f, win_d, win_m, cast_d, cast_m in frac_rows:
        d_off = len(win_d) - 1
        m_off = len(win_m) - 1
        measured.append((f, d_off, m_off, cast_d, cast_m))
        print(f"      {f:<6} {d_off:^10} {m_off:^12} {cast_d:^13} {cast_m:^13}")
    same_d = all(d == cd for _, d, _, cd, _ in measured)
    same_m = all(m == cm for _, _, m, _, cm in measured)
    diff = [f for f, d, m, _, _ in measured if d != m]
    print(
        f"\n    实得偏移 == CAST(同类型列) ?  DOUBLE：{'✅' if same_d else '❌'}"
        f"   DECIMAL：{'✅' if same_m else '❌'}"
    )
    print(f"    同一个数值，DOUBLE 列和 DECIMAL 列的偏移不一致：{diff}")

    half = [(f, d, m) for f, d, m, _, _ in measured if f % 1 == 0.5]
    print("\n    → DuckDB 的 DOUBLE→INTEGER 是「四舍六入五成双」（round half to even）：")
    print("      " + "、".join(f"{f}→{d}" for f, d, _ in half))
    print("      DECIMAL→INTEGER 是「四舍五入、逢五远离零」：")
    print("      " + "、".join(f"{f}→{m}" for f, _, m in half))
    print("      frame 偏移跟着列的类型走 —— 同一个 2.5，写成 DOUBLE 列窗口少一行，")
    print("      写成 DECIMAL 列多一行，**两边都不报错**。")
    print("    实践上：别拿小数当窗口宽度；要拿，先 CAST 成 INTEGER")
    print("      （CAST 和 frame 偏移用的是同一套转换规则，cast 完就没有歧义了）。")


# ---------------------------------------------------------------- 6. 分区窗口


def scenario_partition(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 分区窗口：累计到每个分区重新开始")

    duck_rows = con.sql(
        """
        SELECT g, x, ord, v,
               sum(v) OVER (PARTITION BY g ORDER BY x) AS r_range,
               sum(v) OVER (PARTITION BY g ORDER BY x, ord ROWS UNBOUNDED PRECEDING) AS r_rows
        FROM w
        ORDER BY g, x, ord
        """
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck_rows)

    # Polars 侧 RANGE 的配方：先按 (分区, 排序键) 把并列行合成一组，再逐组累计，
    # 最后把组级结果贴回原始行
    grouped = (
        SMALL.group_by(["g", "x"], maintain_order=True)
        .agg(pl.col("v").sum().alias("v"))
        .with_columns(pl.col("v").cum_sum().over("g", order_by="x").alias("p_range"))
    )
    polars_df = (
        SMALL.join(grouped.select("g", "x", "p_range"), on=["g", "x"], how="left")
        .with_columns(pl.col("v").cum_sum().over("g", order_by=["x", "ord"]).alias("p_rows"))
        .sort("g", "x", "ord")
    )

    common.print_frame(duck_df, "DuckDB RANGE / ROWS（分区累计）", max_rows=10)
    common.print_frame(polars_df, "Polars 两种口径", max_rows=10)

    print("\nDuckDB 分区 g='a' 的 x 是 1,2,2,3 —— 又是并列值，RANGE 与 ROWS 分叉：")
    print("  RANGE：x=2 的两行都看到 10+20+30 = 60")
    print("  ROWS ：第二行 10+20=30，第三行 10+20+30=60")

    duck_range = duck_df["r_range"].to_list()
    duck_rows_v = duck_df["r_rows"].to_list()
    p_range = polars_df["p_range"].to_list()
    p_rows = polars_df["p_rows"].to_list()

    print(f"\nDuckDB RANGE : {duck_range}")
    print(f"Polars RANGE : {p_range}   一致：{'✅' if duck_range == p_range else '❌'}")
    print(f"DuckDB ROWS  : {duck_rows_v}")
    print(f"Polars ROWS  : {p_rows}   一致：{'✅' if duck_rows_v == p_rows else '❌'}")

    print("\nPolars 侧 RANGE 的配方：先 group_by(分区, 排序键) 把并列行合成一组，")
    print("再对组求和、逐组累计，最后贴回原始行 —— 同一组里的值相等，累计点落在哪一行都一样。")
    print("代价是多一次聚合 + 一次 join；DuckDB 的 RANGE 是一等公民，不用绕。")

    print("\n注意 Polars 的 over(..., order_by=...)：不写 order_by 时窗口内顺序未定义，")
    print("      累计类的表达式（cum_sum）会直接算错，且不报错。")


# ---------------------------------------------------------------- 7. 命名窗口


def scenario_named_window(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 命名窗口：SQL 能复用，Polars 只能把表达式抄两遍")

    duck_df = pl.from_arrow(
        con.sql(
            """
            SELECT g, x, ord,
                   sum(v) OVER w AS running,
                   rank() OVER w AS rnk
            FROM w
            WINDOW w AS (PARTITION BY g ORDER BY x, ord)
            ORDER BY g, x, ord
            """
        ).arrow()
    )

    polars_df = SMALL.with_columns(
        pl.col("v").cum_sum().over("g", order_by=["x", "ord"]).alias("running"),
        pl.col("x").rank("min").over("g", order_by=["x", "ord"]).alias("rnk"),
    ).sort("g", "x", "ord")

    common.print_frame(duck_df, "DuckDB 命名窗口 w", max_rows=10)
    common.print_frame(polars_df, "Polars 两个表达式各写一遍", max_rows=10)

    duck_running = duck_df["running"].to_list()
    pol_running = polars_df["running"].to_list()
    duck_rnk = duck_df["rnk"].to_list()
    pol_rnk = polars_df["rnk"].to_list()
    print(f"\n累计和：DuckDB {duck_running}")
    print(f"        Polars {pol_running}   一致：{'✅' if duck_running == pol_running else '❌'}")
    print(f"名次  ：DuckDB {duck_rnk}")
    print(f"        Polars {pol_rnk}   一致：{'✅' if duck_rnk == pol_rnk else '❌'}")

    print("\nDuckDB 的 WINDOW 子句把「分区 + 排序」命名一次，后面可以反复引用：")
    print("  sum(v) OVER w、rank() OVER w，两个窗口函数共用一套 frame，口径改一处就够。")
    print("Polars 没有命名窗口 —— 想共用就得把 over('g', order_by=[...]) 抄两遍，")
    print("改口径的时候很容易只改一处，然后两边悄悄对不上了。")

    print("\n→ **累计和对上了，名次没对上** —— 这是第二个坑：")
    print("  DuckDB 的 `rank()` 是**按排序键整体数名次**。w 里写的是 `ORDER BY x, ord`，")
    print("  (x, ord) 是**一个**排序键：x 相同但 ord 不同的两行**不算并列**，")
    print("  所以名次是 1, 2, 3, 4。")
    print("  Polars 的 `rank('min')` 是**按被排名的那个值**算名次：")
    print("  x=2 的两行并列同名次 → 1, 2, 2, 4。")
    print()
    print("  也就是说：**DuckDB 的 rank 是「行序的名次」，Polars 的 rank 是「值的名次」。**")
    print("  排序键只有一列时两者一致；一旦加了并列打破键就分叉 ——")
    print("  而「加个唯一 tiebreaker」恰恰是最常见的做法。")
    print()
    print("  顺带对齐一下三种 rank 口径：")
    print("    SQL rank()       = 并列同名次、后面跳号  → Polars rank('min')")
    print("    SQL dense_rank() = 并列同名次、不跳号    → Polars rank('dense')")
    print("    SQL row_number() = 每行一个号            → Polars cum_count()")
    print("  Polars 的 rank() **默认值是 'average'**（并列取平均名次），")
    print("  DuckDB **没有**对应的函数 —— 别用默认值。")

    print("\nDuckDB 侧的写法位置（WINDOW 关键字在 FROM 之后）：")
    print("  FROM w")
    print("  WINDOW w AS (PARTITION BY g ORDER BY x, ord)")

    try:
        con.sql(
            "SELECT sum(v) OVER (w ROWS UNBOUNDED PRECEDING) FROM w WINDOW w AS (ORDER BY x)"
        ).fetchall()
        print("  引用命名窗口时再补 frame：允许")
    except Exception as exc:
        print(f"  引用命名窗口时再补 frame：{type(exc).__name__}: {str(exc).splitlines()[0]}")


# ---------------------------------------------------------------- 8. QUALIFY


def scenario_qualify(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · QUALIFY：SQL 能写，Polars 要多走一步")

    duck_df = pl.from_arrow(
        con.sql(
            """
            SELECT g, x, ord, v
            FROM w
            QUALIFY row_number() OVER (PARTITION BY g ORDER BY v DESC, ord) = 1
            ORDER BY g
            """
        ).arrow()
    )

    polars_df = (
        SMALL.with_columns(pl.col("v").rank("ordinal", descending=True).over("g").alias("rn"))
        .filter(pl.col("rn") == 1)
        .select("g", "x", "ord", "v")
        .sort("g")
    )

    common.print_frame(duck_df, "DuckDB QUALIFY", max_rows=10)
    common.print_frame(polars_df, "Polars with_columns + filter", max_rows=10)

    ok, detail = common.compare_frames(duck_df, polars_df, ["g"])
    print(f"\n两边结果等价：{'✅' if ok else '❌'} {detail}")

    print("\nDuckDB 的 QUALIFY 是「窗口算完立刻过滤」的一行写法。")
    print("Polars 没有 QUALIFY 关键字，但同样的效果有两种写法：")
    print("  · with_columns 造出窗口列，再 filter —— 两步，列名可以复用")
    print("  · 直接在 filter 的条件里写窗口表达式 —— 一步，能用")
    for label, fn in [
        (
            "filter 里直接写窗口表达式",
            lambda: SMALL.filter(pl.col("v").rank("ordinal").over("g") == 1),
        ),
        (
            "with_columns 造列再 filter",
            lambda: SMALL.with_columns(pl.col("v").rank("ordinal").over("g").alias("rn")).filter(
                pl.col("rn") == 1
            ),
        ),
        ("filter 里引用还没造出来的列", lambda: SMALL.filter(pl.col("rn") == 1)),
    ]:
        try:
            res = fn()
            print(f"  {label:<28}: 允许（{res.height} 行）")
        except Exception as exc:
            print(f"  {label:<28}: {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\n  → 差别不在能不能算，而在 DuckDB 把「窗口结果直接当过滤条件」做成了语法；")
    print("    Polars 靠表达式的求值顺序，效果一样，但写法和报错方式不一样。")


# ---------------------------------------------------------------- 9. LAG / LEAD


def scenario_lag_lead(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 9 · LAG / LEAD 与 shift")

    duck_df = pl.from_arrow(
        con.sql(
            """
            SELECT g, x, ord,
                   lag(x)  OVER (PARTITION BY g ORDER BY x, ord) AS lag_x,
                   lead(x) OVER (PARTITION BY g ORDER BY x, ord) AS lead_x
            FROM w
            ORDER BY g, x, ord
            """
        ).arrow()
    )

    polars_df = (
        SMALL.with_columns(
            pl.col("x").shift(1).over("g", order_by=["x", "ord"]).alias("lag_x"),
            pl.col("x").shift(-1).over("g", order_by=["x", "ord"]).alias("lead_x"),
        )
        .select("g", "x", "ord", "lag_x", "lead_x")
        .sort("g", "x", "ord")
    )

    common.print_frame(duck_df, "DuckDB lag/lead", max_rows=10)
    common.print_frame(polars_df, "Polars shift(1)/shift(-1)", max_rows=10)

    ok, detail = common.compare_frames(duck_df, polars_df, ["g", "x", "ord"])
    print(f"\n两边结果等价：{'✅' if ok else '❌'} {detail}")

    print("\n对应关系：DuckDB lag(x, n) ≈ Polars shift(n)；DuckDB lead(x, n) ≈ Polars shift(-n)。")
    print("边界都填 NULL，offset 默认都是 1。")

    print("\n一个容易踩的点：DuckDB 的 lag/lead 是窗口函数，必须带 OVER，否则报错；")
    print("Polars 的 shift() 不加 over() 则是在整张表上移位，不分区 —— 语义完全不同但不报错。")
    whole = SMALL.with_columns(pl.col("x").shift(1).alias("lag_x")).sort("g", "x", "ord")
    print("  Polars shift(1) 不加 over：", whole["lag_x"].to_list())


# ---------------------------------------------------------------- 10. 窗口变宽的代价


def scenario_frame_width_cost(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 10 · 窗口变宽，代价怎么变")

    print(f"数据：{rows} 行（365 个不同的日期，v = 行号 % 977）")
    print(f"机器：{common.describe_machine()}")

    days = pl.Series([i % 365 for i in range(rows)], dtype=pl.Int32)
    df = (
        pl.DataFrame({"v": [float(i % 977) for i in range(rows)]})
        .with_columns((pl.lit(datetime.datetime(2024, 1, 1)) + pl.duration(days=days)).alias("ts"))
        .select("ts", "v")
    )

    # 输入先物化：Parquet 落盘、DuckDB 建表、Polars 读进内存，都不计入计时
    tmpdir = Path(tempfile.mkdtemp(prefix="window_demo_w_"))
    src = tmpdir / "ts.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE winw AS SELECT * FROM read_parquet(?)", [str(src)])
    # Polars 侧按 ts 排好序 —— rolling_* 走物理序，不会自己排
    polars_src = pl.read_parquet(src).sort("ts")

    def duck_rows_sql(width: int) -> str:
        return f"""
            SELECT sum(s) FROM (
                SELECT avg(v) OVER (ORDER BY ts
                    ROWS BETWEEN {width - 1} PRECEDING AND CURRENT ROW) AS s
                FROM winw
            )
        """

    def duck_range_sql(width: int) -> str:
        return f"""
            SELECT sum(s) FROM (
                SELECT avg(v) OVER (ORDER BY ts RANGE BETWEEN INTERVAL {width} DAY PRECEDING
                    AND CURRENT ROW) AS s
                FROM winw
            )
        """

    widths = (10, 100, 1000)
    duck_rows_ms: dict[int, float] = {}
    duck_range_ms: dict[int, float] = {}
    polars_ms: dict[int, float] = {}

    for width in widths:
        # 先跑一次把计划编译好，不计入计时
        con.execute(duck_rows_sql(width)).fetchone()
        con.execute(duck_range_sql(width)).fetchone()
        polars_src.select(pl.col("v").rolling_mean(width, min_samples=1).sum())

        d_rows: list[float] = []
        d_range: list[float] = []
        p_times: list[float] = []
        # 交错跑：谁先跑谁不吃亏
        for _ in range(repeat):
            t0 = time.perf_counter()
            con.execute(duck_rows_sql(width)).fetchone()
            d_rows.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            con.execute(duck_range_sql(width)).fetchone()
            d_range.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            polars_src.select(pl.col("v").rolling_mean(width, min_samples=1).sum())
            p_times.append(time.perf_counter() - t0)

        duck_rows_ms[width] = statistics.median(d_rows) * 1000
        duck_range_ms[width] = statistics.median(d_range) * 1000
        polars_ms[width] = statistics.median(p_times) * 1000

    print(f"\n{'窗口宽度':<12}{'DuckDB ROWS':>16}{'DuckDB RANGE':>16}{'Polars':>14}")
    print("-" * 58)
    for width in widths:
        print(
            f"{width:<12}{duck_rows_ms[width]:>13.1f} ms{duck_range_ms[width]:>13.1f} ms"
            f"{polars_ms[width]:>11.1f} ms"
        )

    narrow, wide = widths[0], widths[-1]
    rows_growth = duck_rows_ms[wide] / duck_rows_ms[narrow]
    range_growth = duck_range_ms[wide] / duck_range_ms[narrow]
    polars_growth = polars_ms[wide] / polars_ms[narrow]
    factor = wide // narrow
    distinct_days = len({i % 365 for i in range(rows)})
    print(f"\n窗口宽 {factor} 倍（{narrow} → {wide}）之后：")
    print(f"  DuckDB ROWS  慢了 {rows_growth:.2f}×")
    print(f"  DuckDB RANGE 慢了 {range_growth:.2f}×")
    print(f"  Polars       慢了 {polars_growth:.2f}×")
    print(
        f"\n→ DuckDB 的 ROWS 窗口**不随宽度线性变慢**：宽 {factor} 倍，只慢了 {rows_growth:.2f}×。"
    )
    print("  朴素实现每行都要把窗口里的值重新加一遍（O(n×w)），")
    print("  DuckDB 对滑动窗口做了优化，所以代价主要花在排序和物化上，不是乘窗口宽度。")
    print(
        "  Polars 的 rolling_mean 同理（宽 100 倍只慢了 "
        f"{polars_growth:.2f}×），而且它走物理序、不排序，所以基数更低。"
    )
    print("\n  DuckDB 的 RANGE 是另一个口径 —— 它的宽度单位是**天**，不是行：")
    print(f"    表里只有 {distinct_days} 个不同日期，宽度 {wide} 天时窗口已经覆盖整张表，")
    print(f"    于是每一行都要扫全表（O(n×w)），慢了 {range_growth:.2f}× 很正常。")
    print("    拿 RANGE 的数字去和 ROWS 比大小没有意义 —— 两边窗口里装的行数根本不是一回事。")

    print("\n口径：输入已物化（Parquet 落盘、DuckDB 建表、Polars 读进内存），")
    print("      计时的只有「窗口计算」这一段；同一宽度下三个实现**交错**跑，")
    print(f"      各 {repeat} 轮取中位数。共享开发机，单次数字会抖，看趋势。")
    print("      DuckDB 的窗口要自己排 ORDER BY 的键，Polars 的 rolling_* 走物理序不排 ——")
    print("      所以 DuckDB 这边多付了一份排序钱（Polars 侧已预先按 ts 排好）。")
    print("      DuckDB ROWS 和 Polars rolling_mean 都是「最近 N **行**」口径，可以直接比；")
    print("      DuckDB RANGE 是「最近 N **天**」，单列出来。")


# ---------------------------------------------------------------- 11. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 11 · 窗口函数的代价（两个引擎做同一件事）")

    print(f"数据：{rows} 行（region 分区、order_id 排序的累计销售额）")
    print(f"机器：{common.describe_machine()}")

    regions = ["华东", "华北", "华南", "西南", "华中"]
    df = pl.DataFrame(
        {
            "region": [regions[i % 5] for i in range(rows)],
            "order_id": list(range(rows)),
            "revenue": [float(i % 977) for i in range(rows)],
        },
        schema={"region": pl.String, "order_id": pl.Int64, "revenue": pl.Float64},
    )

    # 输入先物化：Parquet 落盘、DuckDB 建表、Polars 读进内存，都不计入计时
    tmpdir = Path(tempfile.mkdtemp(prefix="window_demo_"))
    src = tmpdir / "orders.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE win AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    duck_sql = """
        SELECT region, order_id,
               sum(revenue) OVER (PARTITION BY region ORDER BY order_id) AS running
        FROM win
    """

    def duck_run() -> pl.DataFrame:
        rel = con.sql(duck_sql)
        try:
            return pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def polars_run() -> pl.DataFrame:
        return polars_src.with_columns(
            pl.col("revenue").cum_sum().over("region", order_by="order_id").alias("running")
        )

    # 先跑一次把计划编译好，不计入计时
    duck_run()
    polars_run()

    # **交错**地跑：固定「先 DuckDB 再 Polars」的话，谁先跑谁要付「第一次」的
    # 冷启动代价，这份代价会被算进那一边。交错着跑，两边轮流当第一个，噪声摊平。
    duck_times: list[float] = []
    polars_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        duck_run()
        duck_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        polars_run()
        polars_times.append(time.perf_counter() - t0)

    duck_med = statistics.median(duck_times)
    polars_med = statistics.median(polars_times)

    print(f"\n{'引擎':<10}{'中位数 (ms)':>14}{'最快 (ms)':>14}")
    print("-" * 38)
    print(f"{'DuckDB':<10}{duck_med * 1000:>14.1f}{min(duck_times) * 1000:>14.1f}")
    print(f"{'Polars':<10}{polars_med * 1000:>14.1f}{min(polars_times) * 1000:>14.1f}")
    if polars_med > 0:
        print(f"\nDuckDB / Polars = {duck_med / polars_med:.2f}×（中位数口径）")

    print("\n口径：输入已物化（Parquet 落盘、DuckDB 建表、Polars 读进内存），")
    print("      计时的只有「窗口计算 + 结果物化」这一段；")
    print(f"      两边**交错**跑 {repeat} 轮（避免「谁先跑谁吃亏」）。")
    print("      共享开发机，单次数字会抖，看趋势。")
    print("窗口函数贵的主因是排序：分区越大，要排的键越多。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的窗口函数差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：{SMALL.height} 行小表（排序键有并列值）")

    con = connect()
    try:
        scenario_default_frame(con)
        scenario_frame_kinds(con)
        scenario_time_sliding_window(con)
        scenario_exclude(con)
        scenario_dynamic_frame(con)
        scenario_partition(con)
        scenario_named_window(con)
        scenario_qualify(con)
        scenario_lag_lead(con)
        scenario_frame_width_cost(con, args.rows, args.repeat)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
