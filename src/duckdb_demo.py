"""DuckDB / SQL 实现。

三条查询与分析任务一一对应（口径见 common.py）：
  1. 区域 × 类目 汇总 + 区域内 Top-N 排名
  2. 每日收入趋势
  3. 高价值客户 Top 20

数据源有两种模式：
  --source parquet  直接对 Parquet 做零拷贝扫描（默认）
  --source duckdb   读取已物化的 demo.duckdb 表
"""

from __future__ import annotations

import argparse

import duckdb
import pyarrow as pa

import common

SQL_REGION_CATEGORY = """
WITH filtered AS (
    SELECT
        o.region,
        o.category,
        o.order_ts::DATE AS order_date,
        o.quantity,
        o.revenue,
        o.user_id
    FROM v_orders o
    JOIN v_users u USING (user_id)
    WHERE o.order_ts >= $start
      AND o.order_ts <  $end
      AND NOT o.is_returned
)
SELECT
    region,
    category,
    COUNT(*)                AS order_cnt,
    SUM(quantity)::BIGINT   AS units,
    SUM(revenue)            AS revenue,
    AVG(revenue)            AS avg_order_value,
    SUM(revenue) / SUM(SUM(revenue)) OVER (PARTITION BY region) AS revenue_share_in_region,
    RANK() OVER (PARTITION BY region ORDER BY SUM(revenue) DESC) AS rank_in_region
FROM filtered
GROUP BY region, category
QUALIFY rank_in_region <= $top_n
ORDER BY region, rank_in_region
"""

SQL_DAILY_REVENUE = """
WITH filtered AS (
    SELECT o.order_ts::DATE AS order_date, o.quantity, o.revenue
    FROM v_orders o
    JOIN v_users u USING (user_id)
    WHERE o.order_ts >= $start
      AND o.order_ts <  $end
      AND NOT o.is_returned
)
SELECT
    order_date,
    COUNT(*)          AS order_cnt,
    SUM(quantity)::BIGINT AS units,
    SUM(revenue)  AS revenue,
    AVG(revenue)  AS avg_order_value
FROM filtered
GROUP BY order_date
ORDER BY order_date
"""

SQL_TOP_CUSTOMERS = """
WITH filtered AS (
    SELECT o.user_id, o.order_ts, o.revenue, u.country, u.tier
    FROM v_orders o
    JOIN v_users u USING (user_id)
    WHERE o.order_ts >= $start
      AND o.order_ts <  $end
      AND NOT o.is_returned
)
SELECT
    user_id,
    country,
    tier,
    COUNT(*)             AS order_cnt,
    SUM(revenue)         AS revenue,
    MIN(order_ts)::DATE  AS first_order_date,
    MAX(order_ts)::DATE  AS last_order_date
FROM filtered
GROUP BY user_id, country, tier
ORDER BY revenue DESC, user_id
LIMIT $limit
"""


def _sql_literal(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def setup(con: duckdb.DuckDBPyConnection, source: str = "parquet") -> duckdb.DuckDBPyConnection:
    """建立 v_orders / v_users 两个视图，两个引擎读的是同一份数据。"""
    if source == "parquet":
        orders = _sql_literal(common.ORDERS_PARQUET)
        users = _sql_literal(common.USERS_PARQUET)
        con.execute(f"CREATE OR REPLACE VIEW v_orders AS SELECT * FROM read_parquet({orders})")
        con.execute(f"CREATE OR REPLACE VIEW v_users AS SELECT * FROM read_parquet({users})")
    elif source == "duckdb":
        con.execute("CREATE OR REPLACE VIEW v_orders AS SELECT * FROM main.orders")
        con.execute("CREATE OR REPLACE VIEW v_users AS SELECT * FROM main.users")
    else:  # pragma: no cover - argparse 已限制取值
        raise ValueError(f"未知 source: {source}")
    return con


def connect(source: str = "parquet") -> duckdb.DuckDBPyConnection:
    """建连接。duckdb 模式直接打开 demo.duckdb 文件，parquet 模式用内存库。"""
    if source == "duckdb":
        if not common.DUCKDB_FILE.exists():
            raise FileNotFoundError(
                f"{common.DUCKDB_FILE} 不存在，请先运行: uv run python src/gen_data.py"
            )
        con = duckdb.connect(str(common.DUCKDB_FILE))
    else:
        con = duckdb.connect()
    return setup(con, source)


def query_region_category(con: duckdb.DuckDBPyConnection) -> pa.Table:
    return con.sql(
        SQL_REGION_CATEGORY,
        params={
            "start": common.WINDOW_START,
            "end": common.WINDOW_END,
            "top_n": common.TOP_N_PER_REGION,
        },
    ).to_arrow_table()


def query_daily_revenue(con: duckdb.DuckDBPyConnection) -> pa.Table:
    return con.sql(
        SQL_DAILY_REVENUE, params={"start": common.WINDOW_START, "end": common.WINDOW_END}
    ).to_arrow_table()


def query_top_customers(con: duckdb.DuckDBPyConnection) -> pa.Table:
    return con.sql(
        SQL_TOP_CUSTOMERS,
        params={
            "start": common.WINDOW_START,
            "end": common.WINDOW_END,
            "limit": common.TOP_CUSTOMERS,
        },
    ).to_arrow_table()


def explain(con: duckdb.DuckDBPyConnection) -> None:
    """打印 DuckDB 优化器生成的执行计划。"""
    con.execute(
        "EXPLAIN ANALYZE " + SQL_REGION_CATEGORY,
        {"start": common.WINDOW_START, "end": common.WINDOW_END, "top_n": common.TOP_N_PER_REGION},
    )
    rows = con.fetchall()
    print("\n=== DuckDB 执行计划（区域 × 类目 Top-N）===")
    for row in rows:
        for line in str(row[-1]).splitlines():
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB / SQL 实现")
    parser.add_argument(
        "--source",
        choices=["parquet", "duckdb"],
        default="parquet",
        help="parquet=直接扫描 Parquet（默认）；duckdb=读取物化后的 demo.duckdb",
    )
    parser.add_argument("--explain", action="store_true", help="额外打印执行计划")
    args = parser.parse_args()

    con = connect(args.source)
    try:
        region = query_region_category(con)
        daily = query_daily_revenue(con)
        customers = query_top_customers(con)
    finally:
        con.close()

    import polars as pl

    common.print_frame(pl.from_arrow(region), "DuckDB · 区域 × 类目 Top-N")
    common.print_frame(pl.from_arrow(daily), "DuckDB · 每日收入趋势", max_rows=10)
    common.print_frame(pl.from_arrow(customers), "DuckDB · 高价值客户 Top 20")

    if args.explain:
        con = connect(args.source)
        try:
            explain(con)
        finally:
            con.close()


if __name__ == "__main__":
    main()
