"""临时跑一条 SQL 看结果。

为什么不直接用 `duckdb data/demo.duckdb`？
DuckDB 2.x 写出的存储格式是 v999，而系统里装的 duckdb CLI（1.5.3）只认 v64~v68，
直接打开会报 "Trying to read a database file with version number 999"。
与其卡在 CLI 版本上，不如用 Python 侧的 DuckDB（和本项目用的同一个版本）。

用法：
    uv run python src/sql.py "SELECT region, count(*) FROM orders GROUP BY 1"
    uv run python src/sql.py --source parquet "SELECT * FROM users LIMIT 5"
"""

from __future__ import annotations

import argparse

import duckdb
import polars as pl

import common


def _sql_literal(path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def connect(source: str) -> duckdb.DuckDBPyConnection:
    """source=duckdb 打开物化文件；source=parquet 用内存库 + Parquet 视图。"""
    if source == "duckdb":
        if not common.DUCKDB_FILE.exists():
            raise FileNotFoundError(
                f"{common.DUCKDB_FILE} 不存在，请先运行: uv run python src/gen_data.py"
            )
        return duckdb.connect(str(common.DUCKDB_FILE), read_only=True)

    con = duckdb.connect()
    orders = _sql_literal(common.ORDERS_PARQUET)
    users = _sql_literal(common.USERS_PARQUET)
    con.execute(f"CREATE OR REPLACE VIEW orders AS SELECT * FROM read_parquet({orders})")
    con.execute(f"CREATE OR REPLACE VIEW users AS SELECT * FROM read_parquet({users})")
    return con


def main() -> None:
    parser = argparse.ArgumentParser(description="跑一条 SQL")
    parser.add_argument("sql", help="要执行的 SQL")
    parser.add_argument(
        "--source",
        choices=["parquet", "duckdb"],
        default="duckdb",
        help="duckdb=读 data/demo.duckdb（默认）；parquet=直接扫 Parquet",
    )
    parser.add_argument("--rows", type=int, default=30, help="最多显示多少行")
    args = parser.parse_args()

    con = connect(args.source)
    try:
        df = pl.from_arrow(con.sql(args.sql).to_arrow_table())
    finally:
        con.close()

    common.print_frame(df.head(args.rows), f"SQL · {args.sql[:60]}", max_rows=args.rows)
    if df.height > args.rows:
        print(f"\n（共 {df.height} 行，只显示前 {args.rows} 行）")


if __name__ == "__main__":
    main()
