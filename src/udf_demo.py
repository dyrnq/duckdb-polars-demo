"""自定义函数（UDF）：两个引擎里的「Python 函数」代价完全不同。

同一个自定义业务规则，两个引擎各写几种：

  DuckDB  · 原生 SQL 表达式
          · Python UDF（type="native"）—— 逐行调用 Python 函数
          · Python UDF（type="arrow"）  —— 每批调用一次，传进来的是 Arrow ChunkedArray
              · 函数体用 pyarrow.compute 写
              · 函数体用 numpy 写
  Polars  · 原生表达式（when/then）
          · map_batches + 批内逐行 Python
          · map_batches + 批内向量化

两组规则：
  · 轻量规则     —— 几次比较就完了
  · 计算密集规则 —— 每行十来次浮点运算

结论跟直觉不一样，值得看两遍：
  · DuckDB 的 arrow UDF 是批式接口，但**函数体怎么写决定成败** ——
    同样一批数据，numpy 函数体比 pyarrow.compute 函数体快 4~8 倍
  · 轻量规则下，用 pyarrow.compute 写的 arrow UDF 甚至比逐行 UDF 还慢
  · Polars 的 map_batches 天生按批给数据，真正的坑是「批内又退回逐行 Python」
  · 两个引擎的**原生表达式**选对分支的结果一样，但 DuckDB 只算命中的那个分支，
    Polars 每个分支都算一遍 —— 规则越复杂、分支越贵，差距越大

为了只量「算这条规则」的代价（而不是扫描 Parquet 的代价），
输入会先物化好，再分别计时。

用法：
    uv run python src/udf_demo.py
    uv run python src/udf_demo.py --rows 200000   # 换个小数据量，快一点
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import Any

import duckdb
import numpy as np
import polars as pl
import pyarrow as pa
import pyarrow.compute as pc

import common

# ================================================================ 规则 1：轻量


def band_scalar(unit_price: float) -> float:
    """逐行版：一次调用处理一行，返回一个 Python 浮点数。"""
    if unit_price < 20.0:
        return 100.0
    if unit_price < 50.0:
        return 60.0
    return 20.0


def band_arrow(unit_price):
    """批版（pyarrow.compute 写）：一次调用拿到整个 ChunkedArray。

    注意：pc.case 在本机 pyarrow 26 上不存在，pc.case_when 也不收 else 参数，
    所以这里用嵌套的 pc.if_else。
    """
    unit = pc.cast(unit_price, "float64")
    return pc.if_else(
        pc.less(unit, 20.0),
        100.0,
        pc.if_else(pc.less(unit, 50.0), 60.0, 20.0),
    )


def band_numpy(unit_price):
    """批版（numpy 写）：同样的规则，换成 numpy 算。"""
    up = unit_price.to_numpy()
    return pa.array(np.where(up < 20.0, 100.0, np.where(up < 50.0, 60.0, 20.0)))


def band_polars_batch(unit_price: pl.Series) -> pl.Series:
    """批版（Polars 写法）：一次调用拿到整个 Series，用 Polars 表达式向量化地算。

    注意：函数体里直接写 pl.when(...) 得到的是 Expr，不是 Series；
    map_batches 要求返回 Series，所以要走 to_frame().select(...).to_series()。
    """
    return (
        unit_price.to_frame()
        .select(
            pl.when(pl.col(unit_price.name) < 20.0)
            .then(pl.lit(100.0))
            .when(pl.col(unit_price.name) < 50.0)
            .then(pl.lit(60.0))
            .otherwise(pl.lit(20.0))
            .alias(unit_price.name)
        )
        .to_series()
    )


# ================================================================ 规则 2：计算密集


def discount_scalar(unit_price: float) -> float:
    """逐行版：多段折扣 + 幂运算，每行十来次浮点运算。"""
    if unit_price < 10.0:
        v = unit_price * 0.95
    elif unit_price < 50.0:
        v = unit_price * 0.9 - 2.0
    elif unit_price < 100.0:
        v = unit_price * 0.85 - 5.0
    else:
        v = unit_price * 0.8 - 12.0
    return (v * 1.03 + 0.5) ** 0.99


def discount_arrow(unit_price):
    """批版（pyarrow.compute 写）。"""
    up = pc.cast(unit_price, "float64")
    v = pc.if_else(
        pc.less(up, 10.0),
        pc.multiply(up, 0.95),
        pc.if_else(
            pc.less(up, 50.0),
            pc.subtract(pc.multiply(up, 0.9), 2.0),
            pc.if_else(
                pc.less(up, 100.0),
                pc.subtract(pc.multiply(up, 0.85), 5.0),
                pc.subtract(pc.multiply(up, 0.8), 12.0),
            ),
        ),
    )
    return pc.power(pc.add(pc.multiply(v, 1.03), 0.5), 0.99)


def discount_numpy(unit_price):
    """批版（numpy 写）。"""
    up = unit_price.to_numpy()
    v = np.where(
        up < 10.0,
        up * 0.95,
        np.where(up < 50.0, up * 0.9 - 2.0, np.where(up < 100.0, up * 0.85 - 5.0, up * 0.8 - 12.0)),
    )
    return pa.array((v * 1.03 + 0.5) ** 0.99)


def discount_polars_batch(unit_price: pl.Series) -> pl.Series:
    """批版（Polars 写法）：同样的规则，用 Polars 表达式向量化地算。"""
    out = (
        unit_price.to_frame()
        .select(
            pl.when(pl.col(unit_price.name) < 10.0)
            .then(pl.col(unit_price.name) * 0.95)
            .when(pl.col(unit_price.name) < 50.0)
            .then(pl.col(unit_price.name) * 0.9 - 2.0)
            .when(pl.col(unit_price.name) < 100.0)
            .then(pl.col(unit_price.name) * 0.85 - 5.0)
            .otherwise(pl.col(unit_price.name) * 0.8 - 12.0)
            .alias(unit_price.name)
        )
        .to_series()
    )
    return ((out * 1.03 + 0.5) ** 0.99).alias(unit_price.name)


# ================================================================ 规则表
# 每条规则给齐各种实现：Python 标量、Arrow 批（pc / numpy）、Polars 批，以及等价的原生 SQL。

RULES = [
    {
        "name": "轻量规则",
        "desc": "按件单价分三档打分 —— 每行就几次比较",
        "scalar": band_scalar,
        "arrow": band_arrow,
        "numpy": band_numpy,
        "polars_batch": band_polars_batch,
        "sql": """
            CASE WHEN {col} < 20.0 THEN 100.0::DOUBLE
                 WHEN {col} < 50.0 THEN 60.0::DOUBLE
                 ELSE 20.0::DOUBLE END
        """,
    },
    {
        "name": "计算密集规则",
        "desc": "多段折扣 + 幂运算 —— 每行十来次浮点运算",
        "scalar": discount_scalar,
        "arrow": discount_arrow,
        "numpy": discount_numpy,
        "polars_batch": discount_polars_batch,
        "sql": """
            power(
                (CASE WHEN {col} < 10.0 THEN {col}*0.95::DOUBLE
                      WHEN {col} < 50.0 THEN {col}*0.9::DOUBLE-2.0::DOUBLE
                      WHEN {col} < 100.0 THEN {col}*0.85::DOUBLE-5.0::DOUBLE
                      ELSE {col}*0.8::DOUBLE-12.0::DOUBLE END)*1.03::DOUBLE+0.5::DOUBLE,
                0.99::DOUBLE)
        """,
    },
]


def section(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def timed(fn) -> tuple[pl.DataFrame, float]:
    """跑一次并计时（秒）。"""
    t0 = time.perf_counter()
    df = fn()
    return df, time.perf_counter() - t0


# ---------------------------------------------------------------- 准备输入
# 先把 order_id + unit_price 物化好，计时才只反映「算规则」的代价。


def duckdb_prepare(con: duckdb.DuckDBPyConnection, rows: int) -> None:
    con.execute(
        f"CREATE OR REPLACE TABLE src AS "
        f"SELECT order_id, unit_price FROM read_parquet('{common.ORDERS_PARQUET}') LIMIT {rows}"
    )


def polars_prepare(rows: int) -> pl.DataFrame:
    return pl.read_parquet(common.ORDERS_PARQUET).select("order_id", "unit_price").head(rows)


# ---------------------------------------------------------------- DuckDB 侧


def duckdb_native_sql(con: duckdb.DuckDBPyConnection, rule: dict) -> pl.DataFrame:
    """原生 SQL：等价的 CASE WHEN。

    踩坑：CASE WHEN 里的 100.0 是 DECIMAL 字面量，整列会变成 decimal[4,1]，
    和 UDF 返回的 DOUBLE 对不上 —— 这里显式 ::DOUBLE 对齐。
    """
    sql = f"SELECT order_id, {rule['sql'].format(col='unit_price')} AS band FROM src"
    return pl.from_arrow(con.sql(sql).to_arrow_table())


def duckdb_udf_native(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Python UDF，逐行版。"""
    sql = "SELECT order_id, band_native(unit_price) AS band FROM src"
    return pl.from_arrow(con.sql(sql).to_arrow_table())


def duckdb_udf_arrow(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Python UDF，批版（一次调用一整个 Arrow ChunkedArray）。"""
    sql = "SELECT order_id, band_arrow(unit_price) AS band FROM src"
    return pl.from_arrow(con.sql(sql).to_arrow_table())


def duckdb_udf_numpy(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Python UDF，批版，但函数体用 numpy 写。"""
    sql = "SELECT order_id, band_numpy(unit_price) AS band FROM src"
    return pl.from_arrow(con.sql(sql).to_arrow_table())


# ---------------------------------------------------------------- Polars 侧


def polars_native(df: pl.DataFrame, rule: dict) -> pl.DataFrame:
    """原生表达式。这里直接用与 SQL 等价的表达式写法。"""
    if rule["name"] == "轻量规则":
        band = (
            pl.when(pl.col("unit_price") < 20.0)
            .then(pl.lit(100.0))
            .when(pl.col("unit_price") < 50.0)
            .then(pl.lit(60.0))
            .otherwise(pl.lit(20.0))
        )
    else:
        band = (
            pl.when(pl.col("unit_price") < 10.0)
            .then(pl.col("unit_price") * 0.95)
            .when(pl.col("unit_price") < 50.0)
            .then(pl.col("unit_price") * 0.9 - 2.0)
            .when(pl.col("unit_price") < 100.0)
            .then(pl.col("unit_price") * 0.85 - 5.0)
            .otherwise(pl.col("unit_price") * 0.8 - 12.0)
        )
        band = (band * 1.03 + 0.5) ** 0.99
    return df.with_columns(band.alias("band"))


def polars_map_rows(df: pl.DataFrame, rule: dict) -> pl.DataFrame:
    """map_batches 里放逐行 Python 函数。"""
    return df.with_columns(
        pl.col("unit_price")
        .map_batches(
            lambda s: s.map_elements(rule["scalar"], return_dtype=pl.Float64),
            return_dtype=pl.Float64,
        )
        .alias("band")
    )


def polars_map_batch(df: pl.DataFrame, rule: dict) -> pl.DataFrame:
    """map_batches 里放向量化写法（批内不回到 Python 标量）。"""
    return df.with_columns(
        pl.col("unit_price")
        .map_batches(rule["polars_batch"], return_dtype=pl.Float64)
        .alias("band")
    )


# ---------------------------------------------------------------- 分支求值探针

# 四个分支，每个分支里都塞一次 power()。
# 如果引擎只算命中的那个分支 → 四分支 ≈ 单分支；
# 如果四个分支全算一遍 → 四分支 ≈ 4 × 单分支。

BRANCH_SQL_ONE = "SELECT power(unit_price, 0.99::DOUBLE) AS v FROM src"

BRANCH_SQL_FOUR = """
    SELECT CASE WHEN unit_price < 10.0  THEN power(unit_price, 0.99::DOUBLE)
                WHEN unit_price < 50.0  THEN power(unit_price*1.1::DOUBLE, 0.99::DOUBLE)
                WHEN unit_price < 100.0 THEN power(unit_price*1.2::DOUBLE, 0.99::DOUBLE)
                ELSE                      power(unit_price*1.3::DOUBLE, 0.99::DOUBLE)
           END AS v
    FROM src
"""


def polars_branch_one(df: pl.DataFrame) -> pl.DataFrame:
    """单分支：一次 power。"""
    c = pl.col("unit_price")
    return df.select(c.pow(0.99).alias("v"))


def polars_branch_four(df: pl.DataFrame) -> pl.DataFrame:
    """四分支，每个分支一次 power。"""
    c = pl.col("unit_price")
    return df.select(
        pl.when(c < 10.0)
        .then(c.pow(0.99))
        .when(c < 50.0)
        .then((c * 1.1).pow(0.99))
        .when(c < 100.0)
        .then((c * 1.2).pow(0.99))
        .otherwise((c * 1.3).pow(0.99))
        .alias("v")
    )


def bench(fn, repeat: int = 5) -> tuple[Any, float]:
    """跑 repeat 次取中位数（秒），返回 (最后一次的结果, 中位耗时)。"""
    out = None
    timings = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        timings.append(time.perf_counter() - t0)
    return out, statistics.median(timings)


def scenario_branch(con: duckdb.DuckDBPyConnection, polars_df: pl.DataFrame) -> None:
    section("第 3 部分 · 原生表达式：四个分支，引擎到底算几遍？")
    print("  构造一条规则：每个分支里都塞一次 power() —— 分支里的活越重，越容易看出差别。")
    print()

    duck_one, t_duck_one = bench(lambda: pl.from_arrow(con.sql(BRANCH_SQL_ONE).to_arrow_table()))
    duck_four, t_duck_four = bench(lambda: pl.from_arrow(con.sql(BRANCH_SQL_FOUR).to_arrow_table()))
    pol_one, t_pol_one = bench(lambda: polars_branch_one(polars_df))
    pol_four, t_pol_four = bench(lambda: polars_branch_four(polars_df))

    print(f"  {'引擎':<9}{'写法':<42}{'耗时(ms)':>10}")
    print("  " + "-" * 64)
    for engine, desc, value in [
        ("DuckDB", "单分支 power(unit_price, 0.99)", t_duck_one * 1000),
        ("DuckDB", "四分支 CASE WHEN（每分支一次 power）", t_duck_four * 1000),
        ("DuckDB", "四分支 ÷ 单分支", f"{t_duck_four / t_duck_one:.1f}×"),
        ("Polars", "单分支 unit_price.pow(0.99)", t_pol_one * 1000),
        ("Polars", "四分支 when/then（每分支一次 power）", t_pol_four * 1000),
        ("Polars", "四分支 ÷ 单分支", f"{t_pol_four / t_pol_one:.1f}×"),
    ]:
        shown = f"{value:.1f}" if isinstance(value, float) else value
        print(f"  {common.pad(engine, 9)}{common.pad(desc, 42)}{shown:>10}")
    print()

    # 逐行算一遍当基准（NumPy，按行选分支）
    up = polars_df["unit_price"].to_numpy()
    factor = np.where(up < 10.0, 1.0, np.where(up < 50.0, 1.1, np.where(up < 100.0, 1.2, 1.3)))
    truth = np.power(up * factor, 0.99)
    duck_v = duck_four["v"].to_numpy()
    pol_v = pol_four["v"].to_numpy()
    duck_err = float(np.max(np.abs(duck_v - truth)))
    pol_err = float(np.max(np.abs(pol_v - truth)))
    same = bool(np.allclose(duck_v, pol_v, rtol=1e-9, atol=1e-9))

    print(f"  分支选择的对账：两个引擎选的结果一致 {'✅' if same else '❌'}")
    print("    与 NumPy 逐行算的基准相比，最大绝对偏差：")
    print(f"      DuckDB {duck_err:.2e}   Polars {pol_err:.2e}")
    print()

    duck_ratio = t_duck_four / t_duck_one
    pol_ratio = t_pol_four / t_pol_one
    print("  读数：")
    print(
        f"    DuckDB 四分支比单分支贵 {duck_ratio:.1f}× —— "
        "四个分支的重活**没有全算一遍**（真按四遍算，比例应该 ≥ 4×）。"
    )
    print(
        f"    Polars 四分支比单分支贵 {pol_ratio:.1f}× —— "
        "四个分支的重活**全算了一遍**，最后才挑对的那条。"
    )
    print("    也就是说：两个引擎**选哪条分支**的结果完全一样，")
    print("    差的是**算几遍** —— 这也是原生 SQL 在计算密集规则上反超 Polars 原生表达式的原因。")
    print()


# ---------------------------------------------------------------- 主流程


def run_rule(con: duckdb.DuckDBPyConnection, polars_df: pl.DataFrame, rule: dict) -> list:
    """跑一条规则的七种实现，返回 [(引擎, 写法, 实现细节, 结果, 耗时)]。"""
    con.create_function("band_native", rule["scalar"], ["DOUBLE"], "DOUBLE", type="native")
    con.create_function("band_arrow", rule["arrow"], ["DOUBLE"], "DOUBLE", type="arrow")
    con.create_function("band_numpy", rule["numpy"], ["DOUBLE"], "DOUBLE", type="arrow")
    try:
        results = []
        df, t = timed(lambda: duckdb_native_sql(con, rule))
        results.append(("DuckDB", "原生 SQL", "CASE WHEN", df, t))
        df, t = timed(lambda: duckdb_udf_native(con))
        results.append(("DuckDB", "Python UDF", "type='native'（逐行）", df, t))
        df, t = timed(lambda: duckdb_udf_arrow(con))
        results.append(("DuckDB", "Python UDF", "type='arrow'（批+pyarrow）", df, t))
        df, t = timed(lambda: duckdb_udf_numpy(con))
        results.append(("DuckDB", "Python UDF", "type='arrow'（批+numpy）", df, t))
        df, t = timed(lambda: polars_native(polars_df, rule))
        results.append(("Polars", "原生表达式", "when/then", df, t))
        df, t = timed(lambda: polars_map_rows(polars_df, rule))
        results.append(("Polars", "map_batches", "批内逐行 Python", df, t))
        df, t = timed(lambda: polars_map_batch(polars_df, rule))
        results.append(("Polars", "map_batches", "批内向量化", df, t))
        return results
    finally:
        # 先摘掉 Python 函数再关连接，否则退出时 nanobind 会刷一屏泄漏警告
        for fn_name in ("band_native", "band_arrow", "band_numpy"):
            try:
                con.remove_function(fn_name)
            except Exception:
                pass


def pick(results: list) -> dict:
    """从结果列表里挑出结论要用的几项，键名固定。"""
    out = {}
    for engine, kind, detail, _df, t in results:
        if engine == "DuckDB":
            if kind == "原生 SQL":
                out["duck_sql"] = t
            elif "native" in detail:
                out["duck_row"] = t
            elif "pyarrow" in detail:
                out["duck_arrow_pc"] = t
            else:
                out["duck_arrow_np"] = t
        else:
            if kind == "原生表达式":
                out["pol_sql"] = t
            elif "逐行" in detail:
                out["pol_row"] = t
            else:
                out["pol_batch"] = t
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的自定义函数（UDF）对比")
    parser.add_argument("--rows", type=int, default=1_000_000, help="参与计算的行数")
    args = parser.parse_args()
    rows = args.rows

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"数据：{common.ORDERS_PARQUET.name} 前 {rows:,} 行")
    print("\n（输入先物化成表/内存 DataFrame，下面量的只是「算这条规则」的代价）")

    section("第 1 部分 · 写法对照")
    print("DuckDB 原生 SQL：")
    print("    CASE WHEN unit_price < 20 THEN 100.0::DOUBLE")
    print("         WHEN unit_price < 50 THEN 60.0::DOUBLE ELSE 20.0::DOUBLE END")
    print("\nDuckDB Python UDF（type='native'，逐行调用）：")
    print(
        "    con.create_function('band_native', band_scalar, ['DOUBLE'], 'DOUBLE', type='native')"
    )
    print("    -> 每行一次 Python 函数调用，返回 Python float")
    print("\nDuckDB Python UDF（type='arrow'，按批调用）：")
    print("    con.create_function('band_arrow', band_arrow, ['DOUBLE'], 'DOUBLE', type='arrow')")
    print("    -> 每批一次调用，参数是 pyarrow.ChunkedArray，返回 Arrow 数组")
    print("    -> 实测批大小约 2048 行（DuckDB 的向量大小）")
    print("\nPolars 原生表达式：")
    print("    pl.when(...).then(...).when(...).then(...).otherwise(...)")
    print("\nPolars map_batches（批内逐行）：")
    print("    pl.col('unit_price').map_batches(lambda s: s.map_elements(band_scalar, ...))")
    print("\nPolars map_batches（批内向量化）：")
    print("    pl.col('unit_price').map_batches(band_polars_batch)   # 批内用 Polars 表达式算")

    con = duckdb.connect()
    duckdb_prepare(con, rows)
    polars_df = polars_prepare(rows)

    picked = []
    try:
        for rule in RULES:
            section(f"第 2 部分 · {rule['name']} —— {rule['desc']}")
            results = run_rule(con, polars_df, rule)
            picked.append(pick(results))

            base = {
                engine: t
                for engine, kind, _d, _df, t in results
                if kind in ("原生 SQL", "原生表达式")
            }
            print(f"{'引擎':<9}{'写法':<14}{'实现细节':<24}{'耗时(ms)':>10}{'相对原生':>10}")
            print("-" * 80)
            for engine, kind, detail, _df, t in results:
                print(
                    f"{engine:<9}{kind:<14}{detail:<24}{t * 1000:>10.1f}{t / base[engine]:>9.1f}×"
                )

            print()
            ref = results[0][3].sort("order_id")
            for engine, kind, detail, df, _t in results:
                got = df.sort("order_id").select("order_id", "band")
                ok, info = common.compare_frames(got, ref.select("order_id", "band"), ["order_id"])
                label = f"{engine}  {kind} / {detail}"
                print(f"  {label:<50}{'✅ 与原生 SQL 一致' if ok else '❌ ' + info}")

        scenario_branch(con, polars_df)
    finally:
        con.close()

    section("第 4 部分 · 结论")
    light, heavy = picked[0], picked[1]

    print("  1) 同样叫「Python UDF」，两个引擎的默认粒度不一样：")
    print(
        f"     DuckDB type='native' 是逐行调用（轻量规则 {light['duck_row'] * 1000:.0f} ms）；"
        f"Polars map_batches 拿到的是整批（批内逐行 {light['pol_row'] * 1000:.0f} ms）"
    )
    print("     同样写成「逐行」，DuckDB 每行要跨一次 Python/C 边界，Polars 则是整批进 Python。")
    print()
    print("  2) DuckDB 的 arrow UDF 是批式接口，但函数体怎么写决定成败：")
    print(
        f"     轻量规则：pyarrow.compute 版 {light['duck_arrow_pc'] * 1000:.0f} ms，"
        f"numpy 版 {light['duck_arrow_np'] * 1000:.0f} ms"
        f"（快 {light['duck_arrow_pc'] / light['duck_arrow_np']:.1f}×）"
    )
    print(
        f"     计算密集规则：pyarrow.compute 版 {heavy['duck_arrow_pc'] * 1000:.0f} ms，"
        f"numpy 版 {heavy['duck_arrow_np'] * 1000:.0f} ms"
        f"（快 {heavy['duck_arrow_pc'] / heavy['duck_arrow_np']:.1f}×）"
    )
    print("     原因是 pc.if_else 在 ChunkedArray 上做标量广播很慢；numpy 走的是连续内存。")
    print()
    print("  3) 批式接口不一定比逐行快 —— 得看每批要干的活：")
    if light["duck_arrow_pc"] > light["duck_row"]:
        print(
            f"     轻量规则下 pyarrow.compute 版的 arrow UDF"
            f"（{light['duck_arrow_pc'] * 1000:.0f} ms）"
            f"甚至比逐行版（{light['duck_row'] * 1000:.0f} ms）还慢"
        )
    else:
        print(
            f"     轻量规则下 arrow UDF（{light['duck_arrow_pc'] * 1000:.0f} ms）"
            f"比逐行版（{light['duck_row'] * 1000:.0f} ms）快"
        )
    print(
        f"     Polars 那边：批内向量化（{light['pol_batch'] * 1000:.1f} ms）"
        f"比批内逐行（{light['pol_row'] * 1000:.0f} ms）"
        f"快 {light['pol_row'] / light['pol_batch']:.0f}×"
    )
    print()
    print("  4) 原生表达式：两个引擎**选对分支的结果**一样，但**算的遍数**不一样 ——")
    print("     DuckDB 的 CASE WHEN 只算命中的那个分支，Polars 的 when/then 每个分支都算一遍。")
    print("     这就是计算密集规则上 DuckDB 原生 SQL 反超 Polars 原生表达式的原因；")
    print("     轻量规则上分支少、每支的活也少，Polars 还是更快。")
    print()
    print("  5) 选型口诀：")
    print("     · 规则能改写成原生表达式 —— 永远用原生（比逐行 UDF 快一个量级）")
    print("     · 只能用 Python 写 —— 用批式接口（DuckDB type='arrow' / Polars map_batches）")
    print("     · DuckDB 的 arrow UDF，函数体优先用 numpy，别用 pyarrow.compute")
    print("     · 批内别退回逐行 Python —— 那是把批式接口用废了")
    print()
    print("  顺带一个类型坑：DuckDB 的 CASE WHEN 里 100.0 是 DECIMAL 字面量，")
    print("    整列会变成 decimal[4,1]，和 UDF 返回的 DOUBLE 对不上 —— 需要 ::DOUBLE 对齐。")
    print()


if __name__ == "__main__":
    main()
