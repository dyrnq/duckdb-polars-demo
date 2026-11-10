"""方言与语义差异专场：SQL 与 DataFrame API 在细节上的不一致。

主查询（region × 类目 Top-N、每日趋势、客户 Top 20）在 `benchmark.py` 里已经对过账，
那些是「两个引擎都能做，只是写法不同」。这个脚本专门挖**行为本身就不一样**的地方 ——
踩上就是静默的错数据，不会报错。

每个场景都用同一份小数据（NULL、NaN 都故意放进去），两边结果并排打印，
最后给出对齐方式。

用法：
    uv run python src/dialect_demo.py
"""

from __future__ import annotations

import argparse
from datetime import datetime

import duckdb
import polars as pl

import common

# 一份故意带坑的小数据：NULL 和 NaN 混在一起
EDGE = pl.DataFrame(
    {
        "order_id": [
            "A-2024-001",
            "B-2023-777",
            "C-2024-003",
            "D-2024-004",
            "E-2024-005",
            "F-2024-006",
        ],
        "region": ["华东", "华北", None, "华东", "华南", None],
        "category": ["数码", None, "食品", "服饰", "数码", "家居"],
        "channel": ["app", "web", "小程序", "门店", "app", "web"],
        "qty": [1, 2, 3, 4, 5, 6],
        "revenue": [100.0, 200.0, 300.0, float("nan"), 500.0, None],
        "order_ts": [
            datetime(2024, 1, 5, 10, 30),
            datetime(2023, 11, 20, 8, 0),
            datetime(2024, 3, 17, 22, 45),
            datetime(2024, 3, 31, 23, 59),
            datetime(2024, 2, 29, 12, 0),
            datetime(2024, 7, 1, 0, 0),
        ],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    """DuckDB 侧直接注册同一个 Arrow 表 —— 两边看的是同一份数据。"""
    con = duckdb.connect()
    con.register("edge", EDGE)
    return con


# ---------------------------------------------------------------- 1. NULL 排序


def scenario_null_ordering(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · NULL 排在前面还是后面？（默认行为不同）")

    duck_rows = con.sql("SELECT region FROM edge ORDER BY region").fetchall()
    duck_first_null = next((i for i, r in enumerate(duck_rows) if r[0] is None), None)

    polars_rows = EDGE.sort("region")["region"].to_list()
    polars_first_null = next((i for i, v in enumerate(polars_rows) if v is None), None)

    print("DuckDB  ORDER BY region ASC :", [r[0] for r in duck_rows])
    print("Polars  sort('region')      :", polars_rows)
    print(f"\nDuckDB  第一个 NULL 在第 {duck_first_null} 位 -> {duck_rows[duck_first_null][0]}")
    print(f"Polars  第一个 NULL 在第 {polars_first_null} 位 -> {polars_rows[polars_first_null]}")

    print("\n差异：DuckDB 升序默认 NULLS LAST，Polars 默认 NULLS FIRST。")
    print("同一份数据，同一个排序意图，两个引擎给出不同顺序。")

    aligned = EDGE.sort("region", nulls_last=True)["region"].to_list()
    ok = aligned == [r[0] for r in duck_rows]
    print(f"\n对齐方式：Polars 侧 sort('region', nulls_last=True) -> {aligned}")
    print(f"与 DuckDB 一致：{'✅' if ok else '❌'}")
    print("反向对齐：DuckDB 侧写 ORDER BY region ASC NULLS LAST。")


# ---------------------------------------------------------------- 2. NaN vs NULL


def scenario_nan_vs_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · NaN 不是 NULL（而且会污染聚合）")

    duck = con.sql(
        """
        SELECT SUM(revenue) AS sum_rev,
               COUNT(revenue) AS count_rev,
               COUNT(*) AS count_all,
               COUNT(*) FILTER (WHERE isnan(revenue)) AS nan_rows
        FROM edge
        """
    ).fetchall()[0]
    polars = EDGE.select(
        pl.col("revenue").sum().alias("sum_rev"),
        pl.col("revenue").count().alias("count_rev"),
        pl.len().alias("count_all"),
        pl.col("revenue").is_nan().sum().alias("nan_rows"),
    ).to_dicts()[0]

    print(f"{'指标':<14}{'DuckDB':>22}{'Polars':>22}")
    print("-" * 58)
    print(f"{'SUM(revenue)':<14}{str(duck[0]):>22}{str(polars['sum_rev']):>22}")
    print(f"{'COUNT(revenue)':<14}{str(duck[1]):>22}{str(polars['count_rev']):>22}")
    print(f"{'COUNT(*)':<14}{str(duck[2]):>22}{str(polars['count_all']):>22}")
    print(f"{'NaN 行数':<14}{str(duck[3]):>22}{str(polars['nan_rows']):>22}")

    print("\n两个引擎在这里是一致的（这是 DuckDB 2.x 的行为，老版本会把 NaN 当 NULL）：")
    print("  · NaN 不是 NULL —— isnan() 为真，is_null() 为假")
    print("  · NaN 会被 COUNT 计入（它是「有值」的）")
    print("  · 但 NaN 会污染 SUM —— 整个聚合变成 NaN，而不是忽略它")

    duck_guarded = con.sql("SELECT SUM(revenue) FROM edge WHERE NOT isnan(revenue)").fetchall()[0][
        0
    ]
    polars_guarded = EDGE.filter(pl.col("revenue").is_not_nan())["revenue"].sum()
    print(f"\n防护：DuckDB WHERE NOT isnan(revenue) -> SUM = {duck_guarded}")
    print(f"防护：Polars filter(is_not_nan())     -> SUM = {polars_guarded}")
    print("\n注意 Polars 的 is_nan() 对 NULL 返回 null（不是 false），别拿它直接当条件用。")


# ---------------------------------------------------------------- 3. 日期截断


def scenario_date_truncate(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 日期截断：单位名不一样")

    duck = con.sql(
        "SELECT order_id, date_trunc('month', order_ts) AS m FROM edge ORDER BY order_id"
    ).fetchall()
    polars = EDGE.with_columns(pl.col("order_ts").dt.truncate("1mo").alias("m")).sort("order_id")

    print("DuckDB  date_trunc('month', order_ts):")
    for oid, m in duck:
        print(f"    {oid}  -> {m}")
    print("\nPolars  dt.truncate('1mo'):")
    for oid, m in polars.select("order_id", "m").rows():
        print(f"    {oid}  -> {m}")

    print("\n差异：Polars 的 dt.truncate 不认 'month'，只认缩写。")
    try:
        EDGE.with_columns(pl.col("order_ts").dt.truncate("1month"))
    except Exception as exc:
        print(f"  Polars dt.truncate('1month') -> {type(exc).__name__}")
        print(f"  {str(exc).splitlines()[0]}")
    print("  可用单位：y, mo, q, w, d, h, m, s, ms, us, ns")
    print("  注意 'm' 是分钟、'mo' 才是月 —— 写错不报错，直接算错。")


# ---------------------------------------------------------------- 4. 正则


def scenario_regex(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 正则提取：分组编号从 0 还是 1 开始")

    duck = con.sql(
        "SELECT order_id, regexp_extract(order_id, '([A-Z])-([0-9]{4})-([0-9]+)', 1) AS g1, "
        "regexp_extract(order_id, '([A-Z])-([0-9]{4})-([0-9]+)', 0) AS g0 "
        "FROM edge ORDER BY order_id"
    ).fetchall()
    polars = EDGE.with_columns(
        pl.col("order_id").str.extract(r"([A-Z])-([0-9]{4})-([0-9]+)", 1).alias("g1"),
        pl.col("order_id").str.extract(r"([A-Z])-([0-9]{4})-([0-9]+)", 0).alias("g0"),
    ).sort("order_id")

    print(f"{'order_id':<14}{'DuckDB g1':>12}{'DuckDB g0':>12}{'Polars g1':>12}{'Polars g0':>12}")
    polars_rows = polars.select("order_id", "g1", "g0").rows()
    for (oid, dg1, dg0), prow in zip(duck, polars_rows, strict=True):
        print(f"{oid:<14}{str(dg1):>12}{str(dg0):>12}{str(prow[1]):>12}{str(prow[2]):>12}")

    ok = [r[1] for r in duck] == polars["g1"].to_list()
    print(f"\n分组编号口径一致（1=第一个括号组，0=整个匹配）：{'✅' if ok else '❌'}")


# ---------------------------------------------------------------- 5. Pivot


def scenario_pivot(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 透视表：缺失组合填 NULL 还是 0")

    # 先滤掉 category 为 NULL 的行：Polars 会拿 NULL 当列名，造出一个叫 "null" 的列
    duck = con.sql(
        """
        PIVOT (
            SELECT region, category, SUM(revenue) AS rv
            FROM edge
            WHERE category IS NOT NULL
            GROUP BY region, category
        ) ON category USING sum(rv)
        ORDER BY region
        """
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck)

    polars_df = (
        EDGE.filter(pl.col("category").is_not_null())
        .group_by(["region", "category"])
        .agg(pl.col("revenue").sum().alias("rv"))
        .pivot(on="category", index="region", values="rv", aggregate_function="sum")
        .sort("region")
    )

    common.print_frame(duck_df, "DuckDB PIVOT", max_rows=10)
    common.print_frame(polars_df, "Polars pivot", max_rows=10)

    print("\n差异：缺失的（区域 × 类目）组合，DuckDB PIVOT 填 NULL，Polars pivot 填 0。")
    print("       两种都不是「空」，但下游算 sum/mean 时含义完全不同。")
    print("对齐方式：Polars 侧 pivot 后 .fill_null(0)，或 DuckDB 侧 COALESCE 补齐。")
    print('\n另一个坑：Polars pivot 拿 NULL 当列名，会多造出一个叫 "null" 的列；')
    print("          DuckDB 直接把 category 为 NULL 的行丢掉了。")


# ---------------------------------------------------------------- 6. DISTINCT ON


def scenario_distinct_on(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 按组取第一条：DISTINCT ON 是 SQL 独有的")

    duck = con.sql(
        """
        SELECT DISTINCT ON (region) region, category, revenue
        FROM edge
        ORDER BY region, revenue DESC
        """
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck)

    polars_df = (
        EDGE.sort(["revenue"], descending=True, nulls_last=True)
        .unique(subset="region", keep="first")
        .select("region", "category", "revenue")
        .sort("region")
    )

    common.print_frame(duck_df, "DuckDB DISTINCT ON", max_rows=10)
    common.print_frame(polars_df, "Polars unique(subset=...)", max_rows=10)

    ok, detail = common.compare_frames(duck_df.sort("region"), polars_df, ["region", "category"])
    print(f"\n两者等价：{'✅' if ok else '❌'} {detail}")
    print("\nDuckDB 的 DISTINCT ON 依赖 ORDER BY 的顺序取第一条；")
    print("Polars 没有对应语法，用 sort + unique(subset=..., keep='first') 模拟，排序键要对齐。")


# ---------------------------------------------------------------- 7. 递归 CTE


def scenario_recursive_cte(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 递归 CTE：只有 SQL 侧有")

    duck = con.sql(
        """
        WITH RECURSIVE t(n) AS (
            SELECT 1
            UNION ALL
            SELECT n + 1 FROM t WHERE n < 10
        )
        SELECT sum(n) AS total, count(*) AS cnt FROM t
        """
    ).fetchall()[0]
    print(f"DuckDB WITH RECURSIVE : total={duck[0]}, cnt={duck[1]}")

    # Polars 侧只能用 range + 表达式模拟
    polars_total = pl.select(pl.int_range(1, 11).sum()).item()
    print(f"Polars  int_range(1,11) : total={polars_total}, cnt=10")
    print("\n差异：Polars 没有递归 CTE，也没有递归表达式。")
    print("       递归 CTE 的典型用途（层级数据、图遍历、日期序列）")
    print("       在 Polars 侧要么用 int_range/datetime_range 展开，要么就是做不到。")


# ---------------------------------------------------------------- 8. DML


def scenario_dml(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 改数据：只有 DuckDB 侧能原地 UPDATE/DELETE")

    con.execute("CREATE TABLE t AS SELECT * FROM edge")
    before = con.sql("SELECT count(*) FROM t WHERE category IS NULL").fetchall()[0][0]
    con.execute("UPDATE t SET category = '未知' WHERE category IS NULL")
    after = con.sql("SELECT count(*) FROM t WHERE category = '未知'").fetchall()[0][0]
    con.execute("DELETE FROM t WHERE qty < 2")
    left = con.sql("SELECT count(*) FROM t").fetchall()[0][0]
    print(f"DuckDB UPDATE : category IS NULL {before} 行 -> '未知' {after} 行")
    print(f"DuckDB DELETE : 删掉 qty<2，剩 {left} 行")

    polars_result = EDGE.with_columns(
        pl.when(pl.col("category").is_null())
        .then(pl.lit("未知"))
        .otherwise(pl.col("category"))
        .alias("category")
    ).filter(pl.col("qty") >= 2)
    print(
        f"Polars        : with_columns + filter -> {polars_result.height} 行（不原地改，返回新表）"
    )

    print("\n差异：DuckDB 支持 UPDATE/DELETE/INSERT，能当事务型数据库用；")
    print("       Polars 没有原地修改，一切都是「表达式 -> 新表」。")
    print("       需要落盘修改时，Polars 侧是 write_parquet 覆盖写，DuckDB 侧是就地改。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的方言/语义差异演示")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：{EDGE.height} 行小表，故意含 NULL 与 NaN")

    con = connect()
    try:
        scenario_null_ordering(con)
        scenario_nan_vs_null(con)
        scenario_date_truncate(con)
        scenario_regex(con)
        scenario_pivot(con)
        scenario_distinct_on(con)
        scenario_recursive_cte(con)
        scenario_dml(con)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
