"""数学函数语义：同名不同义 —— `log()` 在 DuckDB 是 log10，在 Polars 是自然对数。

数学函数是「最不容易出事」的那类差异：不报错、不改类型、不抛异常，
只是**算出来的数是另一个数**。迁移的时候一行 SQL 抄过去，
跑得通、结果看着还挺正常，等到对账才发现差了一个 ln(10) ≈ 2.3026 倍。

这个 demo 把两边**同名函数**逐个对齐，实测它们到底差在哪：

* `log(x)` —— **DuckDB 是 log10，Polars / Python / PostgreSQL 是自然对数**；
* `ln(x)` —— DuckDB 有，Polars 没有（因为 Polars 的 `log` 本身就是 ln）；
* `log(b, v)` —— DuckDB 有（以 b 为底），Polars 没有；
* `log10(x)` —— 两边都有，但**结果差 1 个 ULP**（`log10(1000)`：3.0 vs 2.9999999999999996）；
* `factorial` / `gamma` / `lgamma` / `log2` / `ln` —— DuckDB 有，Polars 一个都没有；
* `log1p` —— 反过来，Polars 有，DuckDB 没有。

同时也要说清楚**哪些是一样的**：定义域边界（log(0)、log(-1)、sqrt(-1)、
pow(-8, 1/3)）、溢出边界（exp(709) vs exp(710)）、三角函数（sin(pi) ≠ 0）
两边行为完全一致 —— 差异集中在**函数命名**上，不在**数学**上。

用法：
    uv run python src/math_demo.py
    uv run python src/math_demo.py --rows 2000000 --repeat 5
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


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def duck_scalar(con: duckdb.DuckDBPyConnection, expr: str) -> object:
    """在 DuckDB 里求一个标量表达式。"""
    return q1(con, f"SELECT {expr}")


def pol_scalar(expr: pl.Expr) -> object:
    """在 Polars 里求一个标量表达式。"""
    return pl.select(expr).item()


def fmt(v: object) -> str:
    """把浮点值打成人读的样子（nan / inf 要看得见）。"""
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
    return str(v)


def same(a: object, b: object) -> bool:
    """两个标量「算出来一样吗」：nan 跟 nan 算一样，inf 跟 inf 算一样。"""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
    return a == b


# ---------------------------------------------------------------- 1. log 的底数


def scenario_log_base(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · `log()` 的底数不一样：同名函数，静默算错")

    print("  DuckDB 文档里 log(x) 是 log10；Python / Polars / PostgreSQL 的 log(x) 是自然对数。")
    print("  两边都返回 DOUBLE，都不报错 —— 差别只在**那个数**。")
    print()

    cases: list[tuple[str, float]] = [
        ("1", 1.0),
        ("10", 10.0),
        ("100", 100.0),
        ("1000", 1000.0),
        ("exp(1)", math.e),
    ]

    print(
        "  "
        + common.pad("x", 10)
        + common.pad("DuckDB log(x)", 22)
        + common.pad("Polars log(x)", 22)
    )
    print("  " + "-" * 54)
    measured: list[tuple[str, float, float]] = []
    for label, x in cases:
        duck_val = float(duck_scalar(con, f"log({x!r})"))
        pol_val = float(pol_scalar(pl.lit(x).log()))
        measured.append((label, duck_val, pol_val))
        print(
            "  "
            + common.pad(label, 10)
            + common.pad(fmt(duck_val), 22)
            + common.pad(fmt(pol_val), 22)
            + ("← 不一样" if not same(duck_val, pol_val) else "← 一样")
        )
    print()

    # 把「对不对」定死：拿数学上确切的答案比
    print("  跟数学上确切的答案比（Python 的 math 库）：")
    print(
        "  "
        + common.pad("x", 10)
        + common.pad("应该是 log10(x)", 20)
        + common.pad("DuckDB 对吗", 14)
        + "Polars 对吗"
    )
    print("  " + "-" * 66)
    for label, x in cases:
        truth = math.log10(x)
        duck_val = float(duck_scalar(con, f"log({x!r})"))
        pol_val = float(pol_scalar(pl.lit(x).log()))
        d_ok = "✅ 是" if same(duck_val, truth) else "❌ 不是"
        p_ok = "✅ 是" if same(pol_val, truth) else "❌ 不是"
        print(
            "  " + common.pad(label, 10) + common.pad(fmt(truth), 20) + common.pad(d_ok, 14) + p_ok
        )
    print()

    ratio = measured[3][2] / measured[3][1]
    print("  → 结论：")
    print(f"    · DuckDB 的 `log(x)` **是 log10**：`log(1000)` = {measured[3][1]}。")
    print(f"    · Polars 的 `log(x)` **是自然对数**：`log(1000)` = {measured[3][2]}。")
    print(f"    · 两边相差 ln(10) ≈ {ratio:.4f} 倍 —— 一模一样的输入，出来两个数。")
    print()
    print("  **为什么这个坑特别难发现**：")
    print("    · 不报错：两边都是 DOUBLE，`log(x)` 都不会抛异常。")
    print("    · 不改类型：列还是浮点列，schema 对账也是通的。")
    print("    · 结果「看起来正常」：自然对数也是一条正常的曲线，")
    print("      画出来甚至更平滑，不去对照根本看不出错。")
    print("    · 错的方向一致：所有数都**偏小**（除了 x=1 附近），")
    print("      所以「阈值筛选」这类逻辑会系统性地多选中一批行。")
    print()
    print("  → 这是本 demo 里最该记住的一条：")
    print("    **DuckDB 的 `log` ≈ PostgreSQL 的 `log10`，不是 PostgreSQL 的 `log`。**")
    print("    从 Postgres 迁到 DuckDB（或者反过来）的人，")
    print("    会以为两边一致 —— 实测不一致。")
    print("    Polars 这边跟 Python 内置 `math.log` 一致，反而是「多数派」。")


# ---------------------------------------------------------------- 2. 迁移对照表


def scenario_migration_table(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 迁移对照表：DuckDB 的写法 ↔ Polars 的写法")

    rows: list[tuple[str, str, str, bool]] = [
        ("log(x)", "log10(x)", "log(x)", True),
        ("log10(x)", "log10(x)", "log10(x)", True),
        ("ln(x)", "ln(x)", "log(x)", True),
        ("log(b, v)", "以 b 为底的 v 的对数", "log(v) / log(b)", False),
        ("log2(x)", "log2(x)", "log(x) / log(2)", False),
        ("log1p(x)", "（没有）", "log1p(x)", False),
        ("cbrt(x)", "cbrt(x)", "cbrt(x)", True),
        ("factorial(n)", "factorial(n) → HUGEINT", "（没有）", False),
        ("gamma(x)", "gamma(x)", "（没有）", False),
        ("lgamma(x)", "lgamma(x)", "（没有）", False),
    ]

    print(
        "  "
        + common.pad("DuckDB 写法", 14)
        + common.pad("DuckDB 实际含义", 24)
        + common.pad("Polars 写法", 22)
        + common.pad("两边有同名函数吗", 20)
    )
    print("  " + "-" * 80)
    for duck_expr, meaning, pol_expr, has_same_name in rows:
        print(
            "  "
            + common.pad(duck_expr, 14)
            + common.pad(meaning, 24)
            + common.pad(pol_expr, 22)
            + ("✅ 有" if has_same_name else "❌ 没有")
        )
    print()

    # 逐个验一遍「迁移写法」是不是真的等价
    print("  逐个验一遍（拿 x=1000 / b=10 代入）：")
    checks = [
        (
            "DuckDB ln(1000)",
            duck_scalar(con, "ln(1000)"),
            "Polars log(1000)",
            pol_scalar(pl.lit(1000.0).log()),
        ),
        (
            "DuckDB log10(1000)",
            duck_scalar(con, "log10(1000)"),
            "Polars log10(1000)",
            pol_scalar(pl.lit(1000.0).log10()),
        ),
        (
            "DuckDB log(10, 1000)",
            duck_scalar(con, "log(10, 1000)"),
            "Polars log(1000)/log(10)",
            pol_scalar(pl.lit(1000.0).log() / pl.lit(10.0).log()),
        ),
        (
            "DuckDB log2(1024)",
            duck_scalar(con, "log2(1024)"),
            "Polars log(1024)/log(2)",
            pol_scalar(pl.lit(1024.0).log() / pl.lit(2.0).log()),
        ),
    ]
    for duck_name, duck_val, pol_name, pol_val in checks:
        ok = "✅ 一致" if same(float(duck_val), float(pol_val)) else "❌ 不一致"
        print(f"    {duck_name:22} = {fmt(duck_val):22} {pol_name:28} = {fmt(pol_val):22} {ok}")
    print()

    print("  → 三条要记住的：")
    print("    ① `log` 在 DuckDB 是 log10、在 Polars 是 ln —— **抄过去必错**。")
    print(
        "       DuckDB 的 `ln` 要写成 Polars 的 `log`；DuckDB 的 `log` 要写成 Polars 的 `log10`。"
    )
    print("    ② Polars **没有** `ln`（因为它的 `log` 本身就是 ln），")
    print("       也**没有**双参数的 `log(b, v)` —— 想按任意底数只能自己写除法。")
    print("    ③ 用除法凑出来的底数（`log(v)/log(b)`）**精度不如原生函数**，")
    print("       下面场景 3 就能看到差在哪。")
    print()
    print("  另外注意 DuckDB 的双参数 log 的**参数顺序**：")
    log_10_1000 = duck_scalar(con, "log(10, 1000)")
    log_1000_10 = duck_scalar(con, "log(1000, 10)")
    print(f"    DuckDB log(10, 1000)  = {log_10_1000}   ← 以 10 为底，1000 的对数 = 3")
    print(f"    DuckDB log(1000, 10)  = {log_1000_10}   ← 以 1000 为底，10 的对数 = 1/3")
    print("    这跟 PostgreSQL 的 `log(b, x)` 同序（先底数后真数），")
    print("    但跟数学课本上 `log_x(y)` 的写法是**反过来**的 —— 抄错一次就差了一个倒数。")


# ---------------------------------------------------------------- 3. 浮点精度


def scenario_float_precision(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 浮点精度：`log10(1000)` 不是精确的 3")

    print("  10 的整数次幂，理论上 log10 应该是整数。实测两边：")
    print()
    print(
        "  "
        + common.pad("x", 12)
        + common.pad("DuckDB log10(x)", 24)
        + common.pad("Polars log10(x)", 24)
        + common.pad("DuckDB 差", 14)
        + common.pad("Polars 差", 14)
    )
    print("  " + "-" * 88)
    for exp in range(1, 7):
        x = float(10**exp)
        duck_val = float(duck_scalar(con, f"log10({x!r})"))
        pol_val = float(pol_scalar(pl.lit(x).log10()))
        # 在 [2^k, 2^(k+1)) 区间里，双精度的最小间隔是 2^(k-52)
        ulp = 2.0 ** (math.floor(math.log2(exp)) - 52)
        d_ulp = abs(duck_val - exp) / ulp
        p_ulp = abs(pol_val - exp) / ulp
        print(
            "  "
            + common.pad(str(int(x)), 12)
            + common.pad(fmt(duck_val), 24)
            + common.pad(fmt(pol_val), 24)
            + common.pad(f"{d_ulp:.0f} ULP", 14)
            + common.pad(f"{p_ulp:.0f} ULP", 14)
        )
    print()
    print("  （ULP = unit in the last place，双精度在该数量级上的最小间隔）")
    print()

    duck_1000 = float(duck_scalar(con, "log10(1000)"))
    pol_1000 = float(pol_scalar(pl.lit(1000.0).log10()))
    pol_recipe = float(pol_scalar(pl.lit(1000.0).log() / pl.lit(10.0).log()))
    print("  → 差别在**最后一位**：")
    print(f"    DuckDB log10(1000)                 = {fmt(duck_1000)}")
    print(f"    Polars log10(1000)                 = {fmt(pol_1000)}")
    print(f"    Polars log(1000) / log(10)         = {fmt(pol_recipe)}")
    print()
    print("  注意 `log10(1000) = 3.0` 在 DuckDB 里是**真的等于 3.0**（不是约等于）：")
    print(f"    DuckDB  log10(1000) = 3.0  ->  相等判定 {duck_scalar(con, 'log10(1000) = 3.0')}")
    print(
        f"    Polars log10(1000) = 3.0  ->  相等判定 {pol_scalar(pl.lit(pol_1000) == pl.lit(3.0))}"
    )
    print()
    print("  **为什么这 1 个 ULP 在实务里会咬人**：")
    print("    · 「取整后再比」：DuckDB `floor(log10(x))` 给出 3，Polars 给出 2 ——")
    print("      一个是「千位」，一个是「百位」。")
    print("    · 「等值分组」：`GROUP BY log10(x)` 把本该同组的行分到了两组。")
    print("    · 「精确等于」的断言：`WHERE log10(x) = 3.0` 在两边命中行数不同。")
    print()

    duck_floor = q1(con, "SELECT floor(log10(1000))")
    pol_floor = pol_scalar((pl.lit(1000.0).log10()).floor())
    print(f"    实测 floor(log10(1000)): DuckDB = {duck_floor}   Polars = {pol_floor}   ← 不一样")
    print()

    print("  → 所以「两边数值一致」这句话要打个折扣：")
    print("    数学函数返回的是**近似值**，两个引擎用不同的算法/不同的舍入点，")
    print("    最后一位对不上是常态。")
    print("    **不要用浮点数学函数的结果做等值判断** —— 两个引擎都别这么用。")


# ---------------------------------------------------------------- 4. 定义域


def scenario_domain(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 定义域边界：log(0) / log(-1) / sqrt(-1) / pow(-8, 1/3)")

    cases = [
        ("log(0)", "log(0.0)", pl.lit(0.0).log()),
        ("log(-1)", "log(-1.0)", pl.lit(-1.0).log()),
        ("ln(0)", "ln(0.0)", pl.lit(0.0).log()),
        ("sqrt(-1)", "sqrt(-1.0)", pl.lit(-1.0).sqrt()),
        ("pow(-8, 1/3)", "pow(-8.0, 1.0/3.0)", pl.lit(-8.0).pow(1.0 / 3.0)),
        ("log2(0)", "log2(0.0)", pl.lit(0.0).log() / pl.lit(2.0).log()),
    ]

    print(
        "  "
        + common.pad("表达式", 16)
        + common.pad("DuckDB", 22)
        + common.pad("Polars", 22)
        + common.pad("一样吗", 12)
    )
    print("  " + "-" * 72)
    for label, duck_expr, pol_expr in cases:
        duck_val = duck_scalar(con, duck_expr)
        pol_val = pol_scalar(pol_expr)
        ok = "✅ 一样" if same(duck_val, pol_val) else "❌ 不一样"
        print(
            "  "
            + common.pad(label, 16)
            + common.pad(fmt(duck_val), 22)
            + common.pad(fmt(pol_val), 22)
            + common.pad(ok, 12)
        )
    print()

    print("  → 这里两边**完全一致**，都是 IEEE 754 的行为：")
    print("    · `log(0)` = **-inf**（不是报错，也不是 0）；")
    print("    · `log(-1)` / `sqrt(-1)` / `pow(负数, 分数)` = **nan**。")
    print()
    print("  顺带一个坑：`pow(-8, 1/3)` 在 Python 里不是 nan，是**复数**：")
    print(f"    Python (-8.0) ** (1/3)     ->  {(-8.0) ** (1.0 / 3.0)}")
    print(f"    Polars pow(-8, 1/3)        ->  {fmt(pol_scalar(pl.lit(-8.0).pow(1.0 / 3.0)))}")
    print(f"    DuckDB pow(-8, 1/3)        ->  {fmt(duck_scalar(con, 'pow(-8.0, 1.0/3.0)'))}")
    print("    → 同一个表达式，Python 给复数，两个引擎给 nan。")
    print("      负数的分数次幂要单独处理，别指望三个地方一致。")
    print()
    print("  → **nan 不会自己消失**，这点两边也一样：")
    print("    IEEE 754 说「NaN 参与任何比较都是 false」，但**两个引擎都没照 IEEE 做**：")
    nan_cmp = con.sql(
        "SELECT log(-1.0) > 0, log(-1.0) < 0, log(-1.0) = log(-1.0), log(-1.0) != log(-1.0)"
    ).fetchall()[0]
    nan_pol = pl.Series([-1.0]).log()
    print(f"    DuckDB  nan>0 / nan<0 / nan=nan / nan!=nan -> {tuple(nan_cmp)}")
    print(
        "    Polars  nan>0 / nan<0 / nan=nan / nan!=nan -> "
        f"{(nan_pol > 0.0).to_list()[0]} / {(nan_pol < 0.0).to_list()[0]} / "
        f"{(nan_pol == nan_pol).to_list()[0]} / {(nan_pol != nan_pol).to_list()[0]}"
    )
    print("    → 两边都把 NaN 当成「比任何数都大」，而且 `nan = nan` 判成 **True**。")
    print("    所以 `WHERE log(x) > 0` **不会**把 nan 行滤掉 —— 它们会留下来。")
    nan_rows_duck = q1(con, "SELECT count(*) FROM (SELECT log(-1.0) AS v) WHERE v > 0")
    nan_rows_pol = pol_scalar((pl.lit(-1.0).log() > 0.0).sum())
    print(
        f"    实测 `WHERE log(x) > 0` 命中行数：DuckDB {nan_rows_duck} · Polars {nan_rows_pol}"
        " —— 都不报错。"
    )
    print("    → 结论要反着记：**NaN 会「混进去」，而不是「被滤掉」**。")
    print("      想把它挡在外面，得显式写 `WHERE NOT isnan(x)`（DuckDB）")
    print("      / `WHERE ~pl.col('x').is_nan()`（Polars）。")


# ---------------------------------------------------------------- 5. 溢出


def scenario_overflow(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 溢出边界：exp(709) 还是个数，exp(710) 就 inf 了")

    print("  IEEE 754 双精度的最大值约 1.8e308。e^x 撞上它就在 x ≈ 709.78。")
    print()
    print(
        "  "
        + common.pad("表达式", 16)
        + common.pad("DuckDB", 24)
        + common.pad("Polars", 24)
        + common.pad("一样吗", 12)
    )
    print("  " + "-" * 76)
    for x in [700.0, 709.0, 709.7, 710.0, 1000.0, -800.0]:
        duck_val = duck_scalar(con, f"exp({x!r})")
        pol_val = pol_scalar(pl.lit(x).exp())
        ok = "✅ 一样" if same(duck_val, pol_val) else "❌ 不一样"
        print(
            "  "
            + common.pad(f"exp({x:g})", 16)
            + common.pad(fmt(duck_val), 24)
            + common.pad(fmt(pol_val), 24)
            + common.pad(ok, 12)
        )
    print()

    duck_709 = duck_scalar(con, "exp(709)")
    duck_710 = duck_scalar(con, "exp(710)")
    print("  → 两边一致：")
    print(f"    · exp(709) = {fmt(duck_709)} —— 还是个正常的有限数。")
    print(f"    · exp(710) = {fmt(duck_710)} —— 直接 inf。")
    print("    · exp(-800) = 0.0 —— 下溢到 0（不是 -inf）。")
    print()
    print("  **注意：inf 是「静默」的**。")
    print("    两边都不报错，inf 会顺着 + - * 传播，")
    print("    最后如果拿去算平均值，可能得到 nan 而不是报错。")
    print("    想让它炸，得自己检查：")
    print("      DuckDB:  `WHERE isfinite(x)`（还有 `isinf` / `isnan`）")
    print("      Polars:  `pl.col('x').is_finite()`（还有 `is_infinite` / `is_nan`）")
    print()

    # inf 传播的实测
    inf_sum_duck = q1(con, "SELECT sum(v) FROM (SELECT exp(710.0) AS v UNION ALL SELECT 1.0)")
    inf_sum_pol = pol_scalar(pl.Series([float("inf"), 1.0]).sum())
    print("  inf 传播实测（inf + 1）：")
    print(f"    DuckDB sum(inf, 1)  -> {fmt(inf_sum_duck)}")
    print(f"    Polars sum(inf, 1)  -> {fmt(inf_sum_pol)}")
    print()
    print("  → **两边一致**：都是 inf。")
    print("    真正的坑是它不报错 —— 一个溢出的中间值能把整列汇总带偏，")
    print("    而你在结果里只看到一个「特别大的数」，不会想到是 exp 溢出。")
    print()
    print("  DuckDB 的溢出和 Polars 的溢出**不是一回事**，别混：")
    print("    · 这里说的 inf 是**浮点溢出**（IEEE 754），两边行为一致。")
    print("    · 整数溢出是另一回事：DuckDB 报错，Polars 静默绕回 —— 那是 arith_demo 的场景 3。")
    print("    · DuckDB 还有第三种：`DOUBLE` 往 `DECIMAL` 转、或者超长的 `factorial`，")
    print("      走的是「报 Out of Range」的路子。三个引擎 × 三种溢出，别搞混。")


# ---------------------------------------------------------------- 6. 三角函数


def scenario_trig(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 三角函数与 π：两个引擎都算 sin(pi) ≠ 0")

    print("  π 是无理数，双精度只能存一个近似值。所以 sin(π) 算出来不是 0，")
    print("  而是一个**极小的数**。实测：")
    print()
    print(
        "  "
        + common.pad("表达式", 16)
        + common.pad("DuckDB", 24)
        + common.pad("Polars", 24)
        + common.pad("一样吗", 12)
    )
    print("  " + "-" * 76)
    cases = [
        ("sin(pi)", "sin(pi())", pl.lit(math.pi).sin()),
        ("cos(pi)", "cos(pi())", pl.lit(math.pi).cos()),
        ("tan(pi)", "tan(pi())", pl.lit(math.pi).tan()),
        ("sin(pi/2)", "sin(pi()/2)", pl.lit(math.pi / 2).sin()),
        ("cos(pi/2)", "cos(pi()/2)", pl.lit(math.pi / 2).cos()),
    ]
    for label, duck_expr, pol_expr in cases:
        duck_val = duck_scalar(con, duck_expr)
        pol_val = pol_scalar(pol_expr)
        ok = "✅ 一样" if same(duck_val, pol_val) else "❌ 不一样"
        print(
            "  "
            + common.pad(label, 16)
            + common.pad(fmt(duck_val), 24)
            + common.pad(fmt(pol_val), 24)
            + common.pad(ok, 12)
        )
    print()

    print("  → 两边**逐位相同**（sin/cos/tan 的实现一致），包括：")
    print("    · `sin(pi)` = 1.2246467991473532e-16 —— **不是 0**。")
    print("    · `cos(pi)` = -1.0 —— 正好是 -1，没有误差。")
    print("    · `tan(pi)` = -1.2246467991473532e-16。")
    print()
    print("  **实务影响**：`sin(pi) == 0` 这种判断永远不成立。")
    print("    要判断「角度在整数圈上」，得用容差：")
    duck_tol = q1(con, "SELECT abs(sin(pi())) < 1e-12")
    pol_tol = pol_scalar(pl.lit(math.pi).sin().abs() < 1e-12)
    print(f"    DuckDB abs(sin(pi())) < 1e-12 -> {duck_tol}")
    print(f"    Polars 同上                   -> {pol_tol}")
    print("    两边一致 —— 但两边**都**得这么写。")
    print()
    print("  → 所以三角函数这一族**没有语义差异**。")
    print("    差异集中在**函数命名**（log / ln / log2 / log1p）上，不在数学本身。")


# ---------------------------------------------------------------- 7. 函数集合


# 候选的数学函数名 —— 拿去两边各查一遍，看谁有谁没有。
# 不靠 `duckdb_functions()` 的 category 分组（DuckDB 2.0 的分类里
# 没有 'math' 这一类，只有 'numeric'/'string'/'text_similarity' 这些），
# 直接按名字查最实在。
MATH_NAMES = [
    "log",
    "log10",
    "ln",
    "log2",
    "log1p",
    "exp",
    "pow",
    "power",
    "sqrt",
    "cbrt",
    "factorial",
    "gamma",
    "lgamma",
    "degrees",
    "radians",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "atan2",
    "sinh",
    "cosh",
    "tanh",
    "erf",
    "erfc",
    "abs",
    "sign",
    "even",
    "trunc",
    "pi",
]


def scenario_duckdb_only(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 函数集合对不齐：factorial / gamma / log2 / ln …")

    placeholders = ", ".join(f"'{n}'" for n in MATH_NAMES)
    duck_funcs = {
        r[0]
        for r in con.sql(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            f"WHERE function_type = 'scalar' AND function_name IN ({placeholders})"
        ).fetchall()
    }
    pol_expr_methods = set(dir(pl.col("a")))

    print(f"  候选 {len(MATH_NAMES)} 个数学函数名，逐个查两边有没有：")
    print()
    print("  " + common.pad("函数名", 14) + common.pad("DuckDB", 12) + "Polars")
    print("  " + "-" * 44)
    duck_only: list[str] = []
    pol_only: list[str] = []
    for name in MATH_NAMES:
        in_duck = name in duck_funcs
        in_pol = name in pol_expr_methods
        if in_duck and not in_pol:
            duck_only.append(name)
        if in_pol and not in_duck:
            pol_only.append(name)
        d_mark = "✅ 有" if in_duck else "❌ 没有"
        p_mark = "✅ 有" if in_pol else "❌ 没有"
        print(f"  {common.pad(name, 14)}{common.pad(d_mark, 12)}{p_mark}")
    print()
    print(f"  DuckDB 独有（{len(duck_only)} 个）：{duck_only}")
    print(f"  Polars 独有（{len(pol_only)} 个）：{pol_only}")
    print(f"  两边都有：{len(MATH_NAMES) - len(duck_only) - len(pol_only)} 个")
    print()
    print("  → 值得注意：**Polars 有 `log1p`，DuckDB 没有** ——")
    print("    这次是 Polars 领先。DuckDB 侧要 log1p 得自己写 `ln(1+x)`。")
    print("    其余的缺口都是 DuckDB 领先，具体见上面那张表的「❌ 没有」行。")
    print()

    print("  DuckDB 这几个函数的实测结果：")
    for expr in [
        "factorial(0)",
        "factorial(20)",
        "factorial(21)",
        "gamma(0.5)",
        "gamma(0)",
        "lgamma(0.5)",
        "cbrt(-8)",
        "log2(1024)",
    ]:
        try:
            val = duck_scalar(con, expr)
            print(f"    {expr:18} -> {fmt(val)}")
        except Exception as exc:
            print(f"    {expr:18} -> {type(exc).__name__}: {str(exc).splitlines()[0][:70]}")
    print()

    print("  负数 factorial 会**报错**，不是返回 nan：")
    try:
        duck_scalar(con, "factorial(-1)")
    except Exception as exc:
        print(f"    factorial(-1) -> {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")
    print("    （Polars 侧连函数都没有，所以也就谈不上行为 —— 只能自己写，")
    print("      而 Python 的 math.factorial(-1) 同样是 ValueError。）")
    print()

    print("  `factorial` 的返回类型是 **HUGEINT**（不是 DOUBLE）：")
    ftype = q1(con, "SELECT typeof(factorial(20))")
    print(f"    typeof(factorial(20)) -> {ftype}")
    print("    20! = 2432902008176640000 —— 这个数用 DOUBLE 存会有精度损失，")
    print("    所以 DuckDB 用 128 位整数接住。Polars 侧要精确结果得用 Int64 + 自己算。")
    print()

    print("  `cbrt` 两边都有，而且对负数都给正确的实数立方根：")
    print(f"    DuckDB cbrt(-8)         -> {fmt(duck_scalar(con, 'cbrt(-8)'))}")
    print(f"    Polars cbrt(-8)         -> {fmt(pol_scalar(pl.lit(-8.0).cbrt()))}")
    print(f"    DuckDB pow(-8, 1.0/3.0) -> {fmt(duck_scalar(con, 'pow(-8.0, 1.0/3.0)'))}   ← nan")
    print(f"    Polars pow(-8, 1.0/3.0) -> {fmt(pol_scalar(pl.lit(-8.0).pow(1.0 / 3.0)))}   ← nan")
    print("    → 差别不在 cbrt，在**别拿 pow(x, 1/3) 当立方根** —— 两边都给 nan。")
    print("      真要在 Polars 侧用 pow 拼，得写成 `sign(x) * pow(abs(x), 1/3)`。")
    print()

    duck_scalar_count = q1(
        con,
        "SELECT count(DISTINCT function_name) FROM duckdb_functions() WHERE function_type='scalar'",
    )
    pol_math_count = len([n for n in pol_expr_methods if n in MATH_NAMES])
    print("  → 结论：这不是「谁强谁弱」，是**函数集合不同**。")
    print(f"    DuckDB 那边有 {duck_scalar_count} 个标量函数，")
    print(
        f"    上表这 {len(MATH_NAMES)} 个候选里，"
        f"DuckDB 有 {len(duck_funcs)} 个、Polars 有 {pol_math_count} 个。"
    )
    print("    但差的不是「多几个」，是**缺的那几个正好是日常会用到的那种**")
    print("    （log2、ln、factorial、gamma、atan2 这些）。")
    print("    迁移时按上表逐个改写就行 —— 缺的都能用初等函数拼出来，")
    print("    只是要多写一行，而且精度要自己负责。")


# ---------------------------------------------------------------- 8. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 8 · 代价：跑得一样快，所以更不会有人发现算错了")

    print(f"数据：{rows:,} 行正数（对数才有意义）")
    print(f"机器：{common.describe_machine()}")
    print()

    tmpdir = Path(tempfile.mkdtemp(prefix="math_demo_"))
    src = tmpdir / "vals.parquet"
    con.execute(
        "COPY (SELECT 1.0 + (i % 1000003) / 7.0 AS v FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)
    con.execute("CREATE OR REPLACE TABLE vals AS SELECT * FROM read_parquet(?)", [str(src)])

    def duck_log10() -> float:
        return float(q1(con, "SELECT sum(log10(v)) FROM vals"))

    def duck_log() -> float:
        # DuckDB 的 log 是 log10
        return float(q1(con, "SELECT sum(log(v)) FROM vals"))

    def duck_ln() -> float:
        return float(q1(con, "SELECT sum(ln(v)) FROM vals"))

    def pol_log10() -> float:
        return float(polars_src["v"].log10().sum())

    def pol_log() -> float:
        # Polars 的 log 是自然对数
        return float(polars_src["v"].log().sum())

    results = [
        ("DuckDB  log10(v)", duck_log10),
        ("DuckDB  log(v)   ← 其实是 log10", duck_log),
        ("DuckDB  ln(v)", duck_ln),
        ("Polars  log10(v)", pol_log10),
        ("Polars  log(v)   ← 其实是自然对数", pol_log),
    ]

    print(
        "  "
        + common.pad("实现", 34)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 20)
    )
    print("  " + "-" * 80)
    for label, fn in results:
        fn()
        times: list[float] = []
        value = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        fastest = min(times)
        print(
            "  "
            + common.pad(label, 34)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:.4f}", 20)
        )
    print()

    print("  口径：输入已物化成 parquet，两边读同一份数据；")
    print("  计时的只有「对这一列做对数再求和」这一步（不含读盘），重复取中位数。")
    print()
    print("  → **耗时看不出任何区别** —— 对数都是硬件级的一两条指令。")
    print("    也就是说：")
    print("    · 性能监控、跑批时长、profile 上**没有任何线索**提示你算错了；")
    print("    · 唯一能发现问题的，只有「拿已知答案对账」这一件事。")
    print()
    print("  最后两行的**结果**才是重点：")
    print("    DuckDB 的 `log(v)` 和 `log10(v)` 结果相同，Polars 的 `log(v)` 跟它们差 ln(10) 倍。")
    print("    性能表上这两行长得一模一样 —— 这正是「静默算错」的可怕之处。")
    print()
    print("  → 实务建议：**迁移时把数学函数列成一张对照表逐个核**，")
    print("    不要靠「跑起来没报错」来判断迁移对了。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的数学函数语义差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    try:
        scenario_log_base(con)
        scenario_migration_table(con)
        scenario_float_precision(con)
        scenario_domain(con)
        scenario_overflow(con)
        scenario_trig(con)
        scenario_duckdb_only(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  ✅ 两边一致的：定义域边界（log(0)=-inf、log(-1)=nan、sqrt(-1)=nan）、")
    print("     浮点溢出边界（exp(709) 有限、exp(710)=inf）、三角函数（sin(pi)≠0）。")
    print("  ❌ 不一致的：**`log` 的底数**（DuckDB log10 / Polars ln）、")
    print("     `log10` 的最后一位（3.0 vs 2.9999999999999996）、")
    print("     `ln` / `log2` / `log1p` / `factorial` / `gamma` 的有无。")
    print()
    print("  → 记住这一条就够：**DuckDB 的 log 是 log10，Polars 的 log 是 ln。**")
    print("    其余的差异都能靠这张对照表逐个改写解决。")
    print()


if __name__ == "__main__":
    main()
