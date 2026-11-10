"""Polars / DataFrame（惰性 API）实现。

与 duckdb_demo.py 中的三条 SQL 查询一一对应，共享 common.py 里的口径。
数据源与 DuckDB 侧完全一致：同一批 Parquet 文件。

关于类型：为了让两边结果可以直接逐值比对，这里显式把计数/求和列转成 Int64，
与 DuckDB 的 BIGINT 对齐（Polars 默认的 pl.len() 是 UInt32，DuckDB 的 SUM(INT) 是 HUGEINT）。
"""

from __future__ import annotations

import argparse

import polars as pl

import common


def load() -> pl.LazyFrame:
    """惰性加载订单 + 用户。scan_parquet 只读元数据，谓词/投影下推。"""
    orders = pl.scan_parquet(common.ORDERS_PARQUET)
    users = pl.scan_parquet(common.USERS_PARQUET)
    return orders.join(users, on="user_id", how="inner")


def _filtered(lf: pl.LazyFrame) -> pl.LazyFrame:
    """统一的时间窗口 + 排除退货。"""
    return lf.filter(
        (pl.col("order_ts") >= common.WINDOW_START)
        & (pl.col("order_ts") < common.WINDOW_END)
        & (~pl.col("is_returned"))
    )


def plan_region_category(lf: pl.LazyFrame) -> pl.LazyFrame:
    """区域 × 类目 汇总 + 区域内 Top-N 排名。

    对应 DuckDB 的 RANK() OVER (... ORDER BY SUM(revenue) DESC)：
    Polars 的 rank 默认是 average（并列取平均名次），SQL 的 RANK() 是并列取最小名次，
    因此这里显式用 method="min" 才能和 SQL 对齐。
    """
    agg = (
        _filtered(lf)
        .group_by(["region", "category"])
        .agg(
            pl.len().cast(pl.Int64).alias("order_cnt"),
            pl.col("quantity").sum().cast(pl.Int64).alias("units"),
            pl.col("revenue").sum().alias("revenue"),
            pl.col("revenue").mean().alias("avg_order_value"),
        )
    )
    agg = agg.with_columns(
        (pl.col("revenue") / pl.col("revenue").sum().over("region")).alias(
            "revenue_share_in_region"
        )
    ).with_columns(
        pl.col("revenue")
        .rank(method="min", descending=True)
        .over("region")
        .cast(pl.Int64)
        .alias("rank_in_region")
    )
    return (
        agg.filter(pl.col("rank_in_region") <= common.TOP_N_PER_REGION)
        .sort(["region", "rank_in_region"])
        .select(common.REGION_CATEGORY_COLUMNS)
    )


def plan_daily_revenue(lf: pl.LazyFrame) -> pl.LazyFrame:
    """每日收入趋势。"""
    return (
        _filtered(lf)
        .with_columns(pl.col("order_ts").dt.date().alias("order_date"))
        .group_by("order_date")
        .agg(
            pl.len().cast(pl.Int64).alias("order_cnt"),
            pl.col("quantity").sum().cast(pl.Int64).alias("units"),
            pl.col("revenue").sum().alias("revenue"),
            pl.col("revenue").mean().alias("avg_order_value"),
        )
        .sort("order_date")
        .select(common.DAILY_COLUMNS)
    )


def plan_top_customers(lf: pl.LazyFrame) -> pl.LazyFrame:
    """高价值客户 Top 20。"""
    return (
        _filtered(lf)
        .group_by("user_id")
        .agg(
            pl.col("country").first().alias("country"),
            pl.col("tier").first().alias("tier"),
            pl.len().cast(pl.Int64).alias("order_cnt"),
            pl.col("revenue").sum().alias("revenue"),
            pl.col("order_ts").min().dt.date().alias("first_order_date"),
            pl.col("order_ts").max().dt.date().alias("last_order_date"),
        )
        .sort(["revenue", "user_id"], descending=[True, False])
        .head(common.TOP_CUSTOMERS)
        .select(common.CUSTOMER_COLUMNS)
    )


def query_region_category(lf: pl.LazyFrame) -> pl.DataFrame:
    return plan_region_category(lf).collect()


def query_daily_revenue(lf: pl.LazyFrame) -> pl.DataFrame:
    return plan_daily_revenue(lf).collect()


def query_top_customers(lf: pl.LazyFrame) -> pl.DataFrame:
    return plan_top_customers(lf).collect()


def explain(lf: pl.LazyFrame) -> None:
    """打印 Polars 的惰性执行计划（优化后）。"""
    print("\n=== Polars 执行计划（区域 × 类目 Top-N）===")
    print(plan_region_category(lf).explain(optimized=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Polars / DataFrame 实现")
    parser.add_argument("--explain", action="store_true", help="额外打印执行计划")
    args = parser.parse_args()

    lf = load()
    region = query_region_category(lf)
    daily = query_daily_revenue(lf)
    customers = query_top_customers(lf)

    common.print_frame(region, "Polars · 区域 × 类目 Top-N")
    common.print_frame(daily, "Polars · 每日收入趋势", max_rows=10)
    common.print_frame(customers, "Polars · 高价值客户 Top 20")

    if args.explain:
        explain(load())


if __name__ == "__main__":
    main()
