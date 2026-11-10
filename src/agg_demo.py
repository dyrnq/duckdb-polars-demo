"""聚合函数专场：插值方式、众数、并列时选谁、字符串拼接的顺序、整数溢出。

聚合函数看起来是「同一个名字、同一个意思」，但两边在细节上分叉：

* 分位数：DuckDB 的 `quantile_cont` 是线性插值，Polars 的 `quantile` 默认是**就近取整** ——
  同一组数据，同一个中位数，一个是 2.5 一个是 3.0。
* 众数：DuckDB 的 `mode()` 返回**一个**值（并列时随便挑），Polars 的 `mode()` 返回**所有**并列值。
* 字符串拼接：DuckDB 的 `string_agg` 不带 ORDER BY 时顺序不定，Polars 的 `implode` 保持输入顺序。
* 整数求和：DuckDB 的 `sum(BIGINT)` 升到 HUGEINT 不溢出，Polars 的 `sum(Int64)` **静默回绕**。

用法：
    uv run python src/agg_demo.py
    uv run python src/agg_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import math
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 一组小数 + 一个并列的字符串，用来卡聚合函数的细节
SMALL = pl.DataFrame(
    {
        "k": ["a", "a", "b", "b", "b", "c"],
        "x": [1.0, 2.0, 2.0, 3.0, 5.0, 5.0],
        "s": ["b", "a", "c", "a", "b", "a"],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("agg", SMALL)
    return con


# ---------------------------------------------------------------- 1. 分位数


def scenario_quantile(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 分位数：插值方式不一样，中位数对不上")

    vals = [1.0, 2.0, 3.0, 4.0]
    con.execute("CREATE TABLE qd AS SELECT unnest([1.0,2.0,3.0,4.0]::DOUBLE[]) AS x")
    con.execute("CREATE TABLE qdec AS SELECT unnest([1.0,2.0,3.0,4.0]::DECIMAL(4,1)[]) AS x")

    print(f"样本：{vals}")
    print(
        f"{'分位':<8}{'DuckDB cont':>14}{'DuckDB disc':>14}{'Polars 默认':>14}{'Polars linear':>15}"
    )
    print("-" * 65)
    series = pl.Series(vals)
    for q in [0.25, 0.5, 0.75]:
        cont, disc = con.execute(
            f"SELECT quantile_cont(x, {q}), quantile_disc(x, {q}) FROM qd"
        ).fetchone()
        print(
            f"q={q:<6}{cont:>14}{disc:>14}"
            f"{series.quantile(q):>14}{series.quantile(q, interpolation='linear'):>15}"
        )

    print("\n同一个 q=0.5：")
    cont, disc = con.execute(
        "SELECT quantile_cont(x, 0.5), quantile_disc(x, 0.5) FROM qd"
    ).fetchone()
    print(f"  DuckDB quantile_cont = {cont}   （线性插值）")
    print(f"  DuckDB quantile_disc = {disc}   （直接取一个实际值）")
    print(f"  Polars quantile 默认 = {series.quantile(0.5)}   （就近取整，等于 higher）")
    q_linear = series.quantile(0.5, interpolation="linear")
    q_lower = series.quantile(0.5, interpolation="lower")
    print(f"  Polars quantile linear = {q_linear}   （对得上 DuckDB 的 cont）")
    print(f"  Polars quantile lower  = {q_lower}   （对得上 DuckDB 的 disc）")

    print("\nPolars 的 quantile 支持这几种插值：")
    for interp in ["linear", "lower", "higher", "nearest", "midpoint"]:
        vals_q = [series.quantile(q, interpolation=interp) for q in [0.25, 0.5, 0.75]]
        print(f"  {interp:<9} -> {vals_q}")

    print("\n对账口径（这条最要紧）：")
    print(f"  DuckDB quantile_cont(0.5) = {cont}")
    print(f"  Polars quantile(0.5)      = {series.quantile(0.5)}")
    print(f"  两者相等：{'✅' if cont == series.quantile(0.5) else '❌ —— 直接对账会挂'}")
    print("  → Polars 侧要么显式写 interpolation='linear'，要么对账时按 DuckDB 的口径写。")

    print("\n再补一个更隐蔽的：DuckDB 的 quantile_cont 会跟着列的类型变。")
    for tbl, label in [("qd", "DOUBLE"), ("qdec", "DECIMAL(4,1)")]:
        got = [
            con.execute(f"SELECT quantile_cont(x, {q}) FROM {tbl}").fetchone()[0]
            for q in [0.25, 0.5, 0.75]
        ]
        print(f"  {label:<14} -> {got}")
    print("  DOUBLE 上是 [1.75, 2.5, 3.25]（线性插值，和 numpy 一致）；")
    print("  DECIMAL(4,1) 上变成 [1.7, 2.5, 3.2] —— 插值结果被截断回列的精度（scale=1），")
    print("  1.75 → 1.7、3.25 → 3.2，是**截断**不是四舍五入。")
    print("  同样的数，只是列的类型不同，25% 与 75% 分位点就各差了 0.05。")


# ---------------------------------------------------------------- 2. 众数


def scenario_mode(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 众数 mode：一个值，还是所有并列值")

    con.execute("CREATE TABLE m1 AS SELECT * FROM (VALUES (1.0),(1.0),(2.0)) t(x)")
    con.execute("CREATE TABLE m2 AS SELECT * FROM (VALUES (1.0),(2.0),(3.0)) t(x)")

    duck_tie = con.execute("SELECT mode(x) FROM m1").fetchone()[0]
    duck_all = con.execute("SELECT mode(x) FROM m2").fetchone()[0]
    pol_tie = pl.Series([1.0, 1.0, 2.0]).mode().to_list()
    pol_all = pl.Series([1.0, 2.0, 3.0]).mode().to_list()

    print("数据 (1,1,2)：")
    print(f"  DuckDB mode(x) -> {duck_tie!r}      （一个值）")
    print(f"  Polars mode()  -> {pol_tie}   （所有并列值）")
    print("\n数据 (1,2,3)（每个值出现次数都一样）：")
    print(f"  DuckDB mode(x) -> {duck_all!r}      （随便挑了一个）")
    print(f"  Polars mode()  -> {pol_all}   （全都算众数）")

    print("\n差异：DuckDB 的 mode() 永远返回一个标量；Polars 的 mode() 返回一个 Series，")
    print("      里面装着所有出现次数最多的值 —— 全部值出现次数都相同时，它把整个集合都给你。")
    print(f"      而且 Polars 返回的顺序不保证有序（本次是 {pol_all}）。")
    print("\n想要 DuckDB 的口径，Polars 侧得自己收窄：")
    print(
        f"  mode().first() / .sort().first() -> {pl.Series([1.0, 1.0, 2.0]).mode().sort().first()}"
    )
    print("  但「挑哪一个」本身仍然是不确定的 —— 跟下面 arg_max 是一回事。")


# ---------------------------------------------------------------- 3. arg_max / arg_min


def scenario_arg_max(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · arg_max / arg_min：并列时选中哪一行，没保证")

    con.execute("CREATE TABLE am AS SELECT * FROM (VALUES ('b',10.0),('a',10.0),('c',5.0)) t(s,v)")

    duck_max = con.execute("SELECT arg_max(s, v) FROM am").fetchone()[0]
    duck_min = con.execute("SELECT arg_min(s, v) FROM am").fetchone()[0]
    df = pl.DataFrame({"s": ["b", "a", "c"], "v": [10.0, 10.0, 5.0]})
    pol_max = df.select(pl.col("s").get(pl.col("v").arg_max())).to_series().to_list()[0]
    pol_min = df.select(pl.col("s").get(pl.col("v").arg_min())).to_series().to_list()[0]
    pol_sort = df.select(pl.col("s").sort_by("v").last()).to_series().to_list()[0]

    print("数据：s 与 v 两列，v 最大的有并列（'b' 和 'a' 都是 10.0）")
    print(f"  DuckDB arg_max(s, v) -> {duck_max!r}")
    print(f"  DuckDB arg_min(s, v) -> {duck_min!r}")
    print(f"  Polars get(arg_max)  -> {pol_max!r}")
    print(f"  Polars get(arg_min)  -> {pol_min!r}")
    print(f"  Polars sort_by(v).last() -> {pol_sort!r}")

    print("\n两个引擎这次选的是同一个（都是「碰到第一个就返回」），但这**不是规范保证**：")
    for order in [("b", "a"), ("a", "b")]:
        con.execute("DROP TABLE IF EXISTS am2")
        con.execute(
            f"CREATE TABLE am2 AS SELECT * FROM (VALUES ('{order[0]}',10.0),"
            f"('{order[1]}',10.0)) t(s,v)"
        )
        d = con.execute("SELECT arg_max(s, v) FROM am2").fetchone()[0]
        frame = pl.DataFrame({"s": list(order), "v": [10.0, 10.0]})
        p = frame.select(pl.col("s").get(pl.col("v").arg_max())).to_series().to_list()[0]
        print(f"  输入顺序 {order} -> DuckDB 选 {d!r}，Polars 选 {p!r}")

    print("\n→ 行序一变，选中的行就变。要稳定，就得自己加一个并列打破键 ——")
    print("  但两个引擎的「打破」方向是相反的：")
    con.execute("DROP TABLE IF EXISTS am3")
    con.execute("CREATE TABLE am3 AS SELECT * FROM (VALUES ('b',10.0,1),('a',10.0,2)) t(s,v,ord)")
    duck_asc = con.execute("SELECT arg_max(s, v ORDER BY ord) FROM am3").fetchone()[0]
    duck_desc = con.execute("SELECT arg_max(s, v ORDER BY ord DESC) FROM am3").fetchone()[0]
    frame = pl.DataFrame({"s": ["b", "a"], "v": [10.0, 10.0], "ord": [1, 2]})
    pol_last = frame.select(pl.col("s").sort_by("v", "ord").last()).to_series().to_list()[0]
    print(f"  DuckDB arg_max(s, v ORDER BY ord)      -> {duck_asc!r}   （并列里取排序后的第一行）")
    print(f"  DuckDB arg_max(s, v ORDER BY ord DESC) -> {duck_desc!r}   （取最后一行）")
    print(f"  Polars  sort_by(['v','ord']).last()    -> {pol_last!r}   （取最后一行）")
    print(f"  DuckDB(ORDER BY ord DESC) 与 Polars 一致：{'✅' if duck_desc == pol_last else '❌'}")


# ---------------------------------------------------------------- 4. 字符串聚合


def scenario_string_agg(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 字符串聚合：拼出来的顺序不一样")

    con.execute("CREATE TABLE sa AS SELECT * FROM (VALUES ('b'),('a'),('c')) t(s)")

    duck_plain = con.execute("SELECT string_agg(s, ',') FROM sa").fetchone()[0]
    duck_sorted = con.execute("SELECT string_agg(s, ',' ORDER BY s) FROM sa").fetchone()[0]
    frame = pl.DataFrame({"s": ["b", "a", "c"]})
    pol_implode = frame.select(pl.col("s").implode().list.join(",")).item()
    pol_sorted = frame.sort("s").select(pl.col("s").implode().list.join(",")).item()

    print("数据：s = ['b', 'a', 'c']")
    print(f"  DuckDB string_agg(s, ',')            -> {duck_plain!r}   （顺序未定义）")
    print(f"  DuckDB string_agg(s, ',' ORDER BY s) -> {duck_sorted!r}   （确定）")
    print(f"  Polars implode().list.join(',')      -> {pol_implode!r}   （保持输入顺序）")
    print(f"  Polars sort('s') 后再 implode        -> {pol_sorted!r}   （确定）")
    print("\n  注意 Polars 2.x 的 implode() 聚成的是**一个列表**（['b','a','c']），")
    print("  不是拼好的字符串 —— 要拿到字符串还得再 .list.join(',') 一层。")

    print("\n差异：DuckDB 的 string_agg 不带 ORDER BY 时**不保证顺序**（SQL 规范就是这样，")
    print("      实际输出取决于执行计划），Polars 的 implode() 保证按输入顺序。")
    print("      拿两边的结果直接比字符串会挂 —— 除非 DuckDB 侧也写 ORDER BY，或者两边都排序。")

    print("\n对齐方式：DuckDB 侧写 string_agg(s, ',' ORDER BY s)，")
    print("         或两边都先按 s 排序再拼。")


# ---------------------------------------------------------------- 5. 整数求和溢出


def scenario_sum_overflow(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 整数求和：DuckDB 升精度，Polars 静默回绕")

    big = 2**62
    con.execute(
        "CREATE TABLE ovf AS SELECT * FROM "
        "(VALUES (4611686018427387904),(4611686018427387904)) t(x)"
    )

    duck_sum, duck_type = con.execute("SELECT sum(x), typeof(sum(x)) FROM ovf").fetchone()
    pol_sum = pl.Series([big, big], dtype=pl.Int64).sum()

    print(f"两个 {big} 相加（结果 {big * 2}，超过 Int64 上限 {2**63 - 1}）")
    print(f"  DuckDB sum(x)  -> {duck_sum}   类型 {duck_type}")
    print(f"  Polars sum()   -> {pol_sum}")
    print(f"  正确答案是 {big * 2}")
    print(f"  DuckDB 对：{'✅' if duck_sum == big * 2 else '❌'}")
    print(f"  Polars 对：{'✅' if pol_sum == big * 2 else '❌ —— 静默回绕成了负数'}")

    print("\nDuckDB 把 BIGINT 的 sum() 升到 HUGEINT（128 位），所以不会溢出；")
    print("Polars 的 sum() 保持 Int64，溢出后**回绕**，不报错、不警告，结果是个负数。")
    print("→ 金额类整数求和，Polars 侧要么先转 Int128/Decimal，要么自己检查量级。")


# ---------------------------------------------------------------- 6. 覆盖差异


def scenario_coverage(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 谁有哪些聚合函数（两边的名单不一样）")

    con.execute("CREATE TABLE cv AS SELECT * FROM (VALUES (1.0,2.0),(2.0,4.0),(3.0,6.5)) v(x,y)")
    duck_corr = con.execute("SELECT corr(x, y) FROM cv").fetchone()[0]
    duck_cov_pop = con.execute("SELECT covar_pop(x, y) FROM cv").fetchone()[0]
    duck_cov_samp = con.execute("SELECT covar_samp(x, y) FROM cv").fetchone()[0]
    pol_corr = pl.select(pl.corr(pl.Series([1.0, 2.0, 3.0]), pl.Series([2.0, 4.0, 6.5]))).item()
    pol_cov = pl.select(pl.cov(pl.Series([1.0, 2.0, 3.0]), pl.Series([2.0, 4.0, 6.5]))).item()

    print("相关系数与协方差：")
    print(f"  DuckDB corr(x,y)    = {duck_corr}")
    print(f"  Polars  pl.corr(x,y) = {pol_corr}")
    print(f"  一致：{'✅' if abs(duck_corr - pol_corr) < 1e-12 else '❌'}")
    print(f"  DuckDB covar_pop    = {duck_cov_pop}   （总体）")
    print(f"  DuckDB covar_samp   = {duck_cov_samp}   （样本）")
    print(f"  Polars  pl.cov      = {pol_cov}")
    print("  → 协方差要当心：Polars 的 pl.cov 是**总体**口径（对上 DuckDB 的 covar_pop），")
    print("    想要样本口径得自己乘 n/(n-1)。")

    print("\n名单对照（两个引擎都真的调一遍，不是照着文档抄的）：")

    xs = pl.Series([1.0, 2.0, 3.0])
    ys = pl.Series([2.0, 4.0, 6.5])
    frame = pl.DataFrame({"x": xs, "y": ys})

    # entropy 单独拎出来说：两边同名，口径却不是一回事
    def shannon2(values: list[float]) -> float:
        """以 2 为底的香农熵（理论值，用来对照实测）。"""
        counts: dict[float, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        n = len(values)
        total = 0.0
        for c in counts.values():
            p = c / n
            total += -p * math.log2(p)
        return total

    sample = [1.0, 2.0, 3.0]
    con.execute("DROP TABLE IF EXISTS ent")
    con.execute("CREATE TABLE ent AS SELECT unnest([1.0,2.0,3.0]::DOUBLE[]) AS x")
    duck_ent = con.execute("SELECT entropy(x) FROM ent").fetchone()[0]
    pol_ent = pl.select(pl.Series(sample).entropy()).item()
    const_ent = pl.select(pl.Series([1.0, 1.0, 1.0, 1.0]).entropy()).item()
    zero_ent = pl.select(pl.Series([0.0, 0.0, 1.0]).entropy()).item()
    other_ent = pl.select(pl.Series([7.0, 8.0, 9.0]).entropy()).item()

    print("  entropy（同名，口径不同）：")
    theory = shannon2(sample)
    print(
        f"    DuckDB entropy([1,2,3]) = {duck_ent:.6f}   —— 以 2 为底的香农熵，理论值 {theory:.6f}"
    )
    print(f"    Polars entropy([1,2,3]) = {pol_ent:.6f}   —— 对不上")
    print(f"    Polars entropy([1,1,1,1]) = {const_ent:.6f}   —— 常数列的香农熵应该是 0")
    print(f"    Polars entropy([0,0,1]) = {zero_ent}   —— 含 0.0 时直接出 NaN")
    print(f"    Polars entropy([7,8,9]) = {other_ent:.6f}   —— 同样的频次分布，换一组数值结果就变")
    print("    → Polars 的 entropy 算的不是「值频次分布的熵」（它随数值本身变），")
    print("      看着像是按哈希值算的。DuckDB 的是标准的以 2 为底的香农熵。")
    print("      两个同名函数不可互换，混用会得到对不上的数。\n")

    checks = [
        (
            "product / 乘积",
            "SELECT product(x) FROM cv",
            lambda: frame.select(pl.col("x").product()).item(),
        ),
        (
            "entropy / 熵",
            "SELECT entropy(x) FROM cv",
            lambda: frame.select(pl.col("x").entropy()).item(),
        ),
        (
            "kurtosis / 峰度",
            "SELECT kurtosis(x) FROM cv",
            lambda: frame.select(pl.col("x").kurtosis()).item(),
        ),
        (
            "skewness / 偏度",
            "SELECT skewness(x) FROM cv",
            lambda: frame.select(pl.col("x").skew()).item(),
        ),
        ("corr / 相关系数", "SELECT corr(x, y) FROM cv", lambda: pl.select(pl.corr(xs, ys)).item()),
        (
            "regr_slope / 回归斜率",
            "SELECT regr_slope(x, y) FROM cv",
            lambda: frame.select(
                ((pl.col("y") - pl.col("y").mean()) * (pl.col("x") - pl.col("x").mean())).sum()
                / ((pl.col("y") - pl.col("y").mean()) ** 2).sum()
            ).item(),
        ),
        (
            "bit_and / 按位与",
            "SELECT bit_and(x::INT) FROM cv",
            lambda: frame.select(pl.col("x").cast(pl.Int64).bit_and()).item(),
        ),
        (
            "histogram / 直方图",
            "SELECT histogram(x) FROM cv",
            lambda: frame.group_by("x").len().height,
        ),
        (
            "approx_count_distinct / 近似去重计数",
            "SELECT approx_count_distinct(x) FROM cv",
            lambda: frame.select(pl.col("x").approx_n_unique()).item(),
        ),
    ]

    print(f"  {'函数':<30}{'DuckDB':<26}{'Polars':<26}")
    print("  " + "-" * 82)
    for label, duck_sql, pol_fn in checks:
        try:
            duck_val = con.execute(duck_sql).fetchone()[0]
            duck_cell = f"✅ {duck_val}"
        except Exception:
            duck_cell = "❌ 没有"
        try:
            pol_val = pol_fn()
            pol_cell = f"✅ {pol_val}"
        except Exception as exc:
            pol_cell = f"❌ {type(exc).__name__}"
        print(f"  {label:<30}{duck_cell[:25]:<26}{pol_cell[:25]:<26}")

    print("\nDuckDB 独有：regr_* 这一整族回归统计（斜率、截距、R²），Polars 侧没有现成的函数。")
    print(
        "Polars 侧名字对不上的：偏度叫 skew（DuckDB 叫 skewness），"
        "乘积叫 product（DuckDB 也叫 product）。"
    )
    print(
        "两边都有但口径要确认的：histogram（Polars 没有直方图函数，只能 group_by + len 手工拼）；"
    )
    print("  regr_slope 的参数顺序也是反的 —— DuckDB 是 regr_slope(y, x)，即「x 对 y 回归」，")
    print("  跟 SQL 里 y = a + b*x 的直觉相反，手写公式时容易写错。")


# ---------------------------------------------------------------- 7. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 聚合的代价（两个引擎做同一件事）")

    print(f"数据：{rows} 行")
    print(f"机器：{common.describe_machine()}")

    df = pl.DataFrame(
        {
            "region": [f"r{i % 20}" for i in range(rows)],
            "category": [f"c{i % 50}" for i in range(rows)],
            "revenue": [float(i % 977) for i in range(rows)],
        },
        schema={"region": pl.String, "category": pl.String, "revenue": pl.Float64},
    )

    # 输入先物化，不计入计时
    tmpdir = Path(tempfile.mkdtemp(prefix="agg_demo_"))
    src = tmpdir / "orders.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE agg_t AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    duck_sql = """
        SELECT region, category,
               count(*) AS n,
               sum(revenue) AS total,
               avg(revenue) AS mean,
               quantile_cont(revenue, 0.5) AS median
        FROM agg_t
        GROUP BY region, category
    """

    def duck_run() -> pl.DataFrame:
        rel = con.sql(duck_sql)
        try:
            return pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def polars_run() -> pl.DataFrame:
        return polars_src.group_by(["region", "category"]).agg(
            pl.len().alias("n"),
            pl.col("revenue").sum().alias("total"),
            pl.col("revenue").mean().alias("mean"),
            pl.col("revenue").quantile(0.5, interpolation="linear").alias("median"),
        )

    duck_run()
    polars_run()

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

    print("\n口径：输入已物化，计时的只有「分组聚合 + 结果物化」。")
    print("两边做的活是一样的（count/sum/avg/中位数），只有中位数的插值口径按上面调成一致。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的聚合函数差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：{SMALL.height} 行小表")

    con = connect()
    try:
        scenario_quantile(con)
        scenario_mode(con)
        scenario_arg_max(con)
        scenario_string_agg(con)
        scenario_sum_overflow(con)
        scenario_coverage(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
