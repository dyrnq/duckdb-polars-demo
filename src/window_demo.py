"""窗口函数专场：frame 的默认值、并列行、命名窗口、QUALIFY 与窗口代价。

窗口函数是「看着一样、算出来不一样」的重灾区：

* DuckDB 不带 frame 的 `OVER (ORDER BY x)` 默认是 **RANGE**（并列行一起算），
  而 Polars 的 `cum_sum()` 永远是 **ROWS**（逐行累计）—— 排序键有并列值时两边分叉。
* 更坑的是：DuckDB 的默认值会**随排序键个数变**。单键是 RANGE，
  一旦写成 `ORDER BY x, ord`（多键），RANGE 表达不了，SQL 就悄悄退化成 ROWS，
  结果跟着变，语句还不报错。
* Polars 侧没有 frame 概念，只有 `cum_sum()` / `shift()` / `rolling_*()` 这一族表达式。

每个场景都拿同一份小数据并排打印两个引擎的结果，最后给出等价写法。

用法：
    uv run python src/window_demo.py
    uv run python src/window_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
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


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("w", SMALL)
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
    print("  RANGE / GROUPS            -> 没有现成表达式，得自己拼（见场景 3）")


# ---------------------------------------------------------------- 3. 分区窗口


def scenario_partition(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 分区窗口：累计到每个分区重新开始")

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


# ---------------------------------------------------------------- 4. 命名窗口


def scenario_named_window(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 命名窗口：SQL 能复用，Polars 只能把表达式抄两遍")

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


# ---------------------------------------------------------------- 5. QUALIFY


def scenario_qualify(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · QUALIFY：SQL 能写，Polars 要多走一步")

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


# ---------------------------------------------------------------- 6. LAG / LEAD


def scenario_lag_lead(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · LAG / LEAD 与 shift")

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


# ---------------------------------------------------------------- 7. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 窗口函数的代价（两个引擎做同一件事）")

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
        scenario_partition(con)
        scenario_named_window(con)
        scenario_qualify(con)
        scenario_lag_lead(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
