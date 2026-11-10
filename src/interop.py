"""DuckDB 与 Polars 的互操作：同一份 Parquet、Arrow 零拷贝接力、类型对齐。

把三件事串成一条可执行的链路：

  1. 同一份 Parquet，两个引擎读出的数据完全一致 —— 这是「比谁快」的前提
  2. Arrow 是内存中的通用货币：DuckDB -> Polars、Polars -> DuckDB 都能接力，
     中间不落盘、不做 Python 层拷贝
  3. 文件格式通用 ≠ 语义通用：两边的默认类型和名次规则不一样，必须显式对齐

用法：
    uv run python src/interop.py
"""

from __future__ import annotations

import argparse

import duckdb
import polars as pl
import pyarrow as pa

import common
import duckdb_demo


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------------------------------------------------------------- 第 1 步


def step1_same_parquet() -> None:
    """两个引擎读同一个 Parquet 文件，比对 schema 和内容。"""
    section("第 1 步 · 同一份 Parquet，两个引擎读到同一份数据")

    polars_df = pl.read_parquet(common.ORDERS_PARQUET)
    duck_tbl = (
        duckdb_demo.connect()
        .sql(f"SELECT * FROM read_parquet('{common.ORDERS_PARQUET}')")
        .to_arrow_table()
    )
    duck_df = pl.from_arrow(duck_tbl)

    print(f"Polars 直读 : {polars_df.shape}")
    print(f"DuckDB 读取 : {duck_tbl.num_rows} 行 × {duck_tbl.num_columns} 列")
    print(f"schema 一致 : {duck_df.schema == polars_df.schema}")
    print(f"内容完全相等 : {duck_df.equals(polars_df)}")
    print("\n两个引擎读的是同一个文件、同一个字节 —— 后面比快慢才站得住脚。")


# ---------------------------------------------------------------- 第 2 步


def step2_duckdb_to_polars() -> None:
    """DuckDB 的结果集 -> Arrow -> Polars，不落盘。"""
    section("第 2 步 · DuckDB -> Arrow -> Polars（不落盘）")

    con = duckdb_demo.connect()
    try:
        duck_tbl = con.sql(
            """
            SELECT region, category, COUNT(*) AS order_cnt, SUM(revenue) AS revenue
            FROM v_orders
            WHERE order_ts >= $start AND order_ts < $end AND NOT is_returned
            GROUP BY region, category
            """,
            params={"start": common.WINDOW_START, "end": common.WINDOW_END},
        ).to_arrow_table()
    finally:
        con.close()

    handed_over = pl.from_arrow(duck_tbl)

    print(f"DuckDB 产出 Arrow Table : {duck_tbl.num_rows} 行 × {duck_tbl.num_columns} 列")
    print(f"Polars 接住后           : {handed_over.shape}")
    print(f"列类型                  : {handed_over.schema}")

    # 证明没有做 Python 层的行拷贝：Arrow 的底层 buffer 地址在两侧是同一块
    arrow_buffers = duck_tbl.column("revenue").chunk(0).buffers()
    polars_buffers = handed_over.to_arrow().column("revenue").chunk(0).buffers()
    same_buffer = arrow_buffers[1] == polars_buffers[1]
    print(f"\nArrow buffer 地址一致（零拷贝）: {same_buffer}")

    common.print_frame(handed_over.sort("revenue", descending=True).head(5), "接住后的前 5 行")


# ---------------------------------------------------------------- 第 3 步


def step3_polars_to_duckdb() -> None:
    """Polars 的 DataFrame -> Arrow -> DuckDB 表，用 SQL 接着查。"""
    section("第 3 步 · Polars -> Arrow -> DuckDB（把 DataFrame 变成可 SQL 查询的表）")

    pl_df = pl.DataFrame(
        {
            "region": ["华东", "华北", "华南", "西南", "华中"],
            "revenue": [2.694e7, 1.853e7, 1.671e7, 1.098e7, 1.093e7],
            "orders": [320286, 220350, 199013, 130408, 129943],
        }
    )

    con = duckdb.connect()
    try:
        # register 接受 Polars DataFrame，也可以接受 pa.Table
        con.register("polars_regions", pl_df)
        result = con.sql(
            """
            SELECT region, revenue, orders,
                   revenue / SUM(revenue) OVER () AS share,
                   RANK() OVER (ORDER BY revenue DESC) AS rk
            FROM polars_regions
            ORDER BY rk
            """
        ).to_arrow_table()
    finally:
        con.close()

    print("Polars 里的 DataFrame：")
    common.print_frame(pl_df, "输入（Polars）", max_rows=5)
    print("\n同一份数据，在 DuckDB 里用 SQL 接着算窗口函数：")
    common.print_frame(pl.from_arrow(result), "输出（DuckDB SQL）", max_rows=5)


# ---------------------------------------------------------------- 第 4 步


def step4_pipeline_handoff() -> None:
    """流水线接力：DuckDB 做过滤聚合，Polars 收尾算窗口。"""
    section("第 4 步 · 流水线接力：DuckDB 过滤聚合 -> Polars 算窗口 -> 收尾")

    con = duckdb_demo.connect()
    try:
        # 前半段：DuckDB 擅长的一侧 —— 谓词下推 + 并行聚合
        duck_tbl = con.sql(
            """
            SELECT region, category, COUNT(*) AS order_cnt,
                   SUM(quantity)::BIGINT AS units, SUM(revenue) AS revenue,
                   AVG(revenue) AS avg_order_value
            FROM v_orders
            WHERE order_ts >= $start AND order_ts < $end AND NOT is_returned
            GROUP BY region, category
            """,
            params={"start": common.WINDOW_START, "end": common.WINDOW_END},
        ).to_arrow_table()
    finally:
        con.close()

    # 后半段：Polars 擅长的一侧 —— 窗口函数表达起来更短
    handoff = pl.from_arrow(duck_tbl)
    final = (
        handoff.with_columns(
            (pl.col("revenue") / pl.col("revenue").sum().over("region")).alias(
                "revenue_share_in_region"
            )
        )
        .with_columns(
            pl.col("revenue")
            .rank(method="min", descending=True)
            .over("region")
            .cast(pl.Int64)
            .alias("rank_in_region")
        )
        .filter(pl.col("rank_in_region") <= common.TOP_N_PER_REGION)
        .sort(["region", "rank_in_region"])
        .select(common.REGION_CATEGORY_COLUMNS)
    )

    # 同一件事，纯 DuckDB 做完（作为对照）
    con = duckdb_demo.connect()
    try:
        pure_duck = pl.from_arrow(duckdb_demo.query_region_category(con))
    finally:
        con.close()
    pure_polars = polars_demo_query_region_category()

    ok_mixed, detail_mixed = common.compare_frames(
        final, pure_duck, common.QUERY_KEYS["region_category"]
    )
    ok_pure, detail_pure = common.compare_frames(
        pure_polars, pure_duck, common.QUERY_KEYS["region_category"]
    )

    print(f"接力结果   == 纯 DuckDB : {'✅' if ok_mixed else '❌'} {detail_mixed}")
    print(f"纯 Polars  == 纯 DuckDB : {'✅' if ok_pure else '❌'} {detail_pure}")
    common.print_frame(final, "接力产出的最终结果", max_rows=6)


def polars_demo_query_region_category() -> pl.DataFrame:
    """纯 Polars 一条龙算完同样的结果（用于对照）。"""
    import polars_demo

    return polars_demo.query_region_category(polars_demo.load())


# ---------------------------------------------------------------- 第 5 步


def step5_type_alignment() -> None:
    """格式通用不等于语义通用：两边默认类型不一样。"""
    section("第 5 步 · 格式通用 ≠ 语义通用：默认类型的差异")

    con = duckdb_demo.connect()
    try:
        duck_tbl = con.sql(
            """
            SELECT region, category, COUNT(*) AS order_cnt, SUM(quantity) AS units
            FROM v_orders
            GROUP BY region, category
            LIMIT 3
            """
        ).to_arrow_table()
    finally:
        con.close()

    polars_df = (
        pl.scan_parquet(common.ORDERS_PARQUET)
        .group_by(["region", "category"])
        .agg(pl.len().alias("order_cnt"), pl.col("quantity").sum().alias("units"))
        .collect()
    )

    print("同一段聚合，两边的默认类型：")
    duck_types = dict(
        zip(duck_tbl.schema.names, [str(t) for t in duck_tbl.schema.types], strict=True)
    )
    print(f"  DuckDB : {duck_types}")
    print(f"  Polars : {dict(polars_df.schema)}")
    print("\n  COUNT(*)    DuckDB=BIGINT(int64)          Polars=pl.len()=UInt32")
    print("  SUM(int32)  DuckDB=HUGEINT(decimal[38,0])  Polars=sum()=Int32（保持输入类型）")
    print("\n  对齐办法：两边都显式转 Int64（DuckDB ::BIGINT / Polars .cast(pl.Int64)）。")

    print("\n还有一个语义差异 —— 并列名次：")
    print("  DuckDB RANK()        1, 2, 2, 4   （并列取最小名次）")
    print("  Polars rank() 默认   1, 2, 2.5, 4 （average）")
    print("  对齐办法：Polars 侧用 rank(method='min')。")


# ---------------------------------------------------------------- 第 6 步


def step6_optional_extension() -> None:
    """可选：DuckDB 的 polars 扩展，能直接用 SQL 查 Polars DataFrame。

    需要联网下载扩展，所以默认不跑 —— 显式加 --extension 才触发。
    """
    section("第 6 步 · 可选：duckdb 的 polars 扩展")

    try:
        con = duckdb.connect()
        con.execute("INSTALL polars; LOAD polars;")
    except Exception as exc:  # 离线环境 / 版本不支持都会走到这
        print(f"当前环境不可用：{str(exc).splitlines()[0][:120]}")
        print("\n装上之后就能用 SQL 直接查一个 Polars DataFrame，连 Arrow 转换都省了。")
        print("本项目的其余部分不依赖它。")
        return

    print("扩展可用。")
    print("（具体用法随扩展版本变化，查官方文档：https://duckdb.org/docs/extensions/polars）")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的互操作演示")
    parser.add_argument(
        "--extension",
        action="store_true",
        help="额外尝试安装 duckdb 的 polars 扩展（需要联网下载）",
    )
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__} · PyArrow {pa.__version__}")

    step1_same_parquet()
    step2_duckdb_to_polars()
    step3_polars_to_duckdb()
    step4_pipeline_handoff()
    step5_type_alignment()
    if args.extension:
        step6_optional_extension()
    else:
        section("第 6 步 · 可选：duckdb 的 polars 扩展")
        print("跳过（默认不联网下载扩展）。加 --extension 可以试装：")
        print("  uv run python src/interop.py --extension")

    print()


if __name__ == "__main__":
    main()
