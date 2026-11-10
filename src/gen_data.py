"""生成可复现的演示数据。

产出：
  data/orders.parquet  事实表（订单）
  data/users.parquet   维表（用户）
  data/demo.duckdb     DuckDB 物化版本，方便用 CLI 交互探索

固定随机种子 + 固定时间窗口 => 每次生成的数据完全一致，跨机器可复现。
"""

from __future__ import annotations

import argparse
from datetime import datetime

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import common


def _to_ns(dt: datetime) -> int:
    """朴素 datetime -> 纳秒时间戳（按 UTC 解释，两个引擎读到的值一致）。"""
    return int(np.datetime64(dt, "ns").astype(np.int64))


def _sql_literal(path) -> str:
    """把路径安全地转成 SQL 字符串字面量。"""
    return "'" + str(path).replace("'", "''") + "'"


def build_users(n_users: int, rng: np.random.Generator) -> pa.Table:
    """用户维表：注册时间、国家、会员等级。"""
    user_id = np.arange(1, n_users + 1, dtype=np.int64)

    # 注册时间：数据起点往前推 0~900 天
    signup_days = rng.integers(0, 900, size=n_users)
    signup_date = (np.datetime64(common.DATA_START) - signup_days.astype("timedelta64[D]")).astype(
        "datetime64[ns]"
    )

    country = rng.choice(common.COUNTRIES, size=n_users, p=common.COUNTRY_WEIGHTS)
    tier = rng.choice(common.TIERS, size=n_users, p=common.TIER_WEIGHTS)

    return pa.table(
        {
            "user_id": pa.array(user_id),
            "signup_date": pa.array(signup_date),
            "country": pa.array(country),
            "tier": pa.array(tier),
        }
    )


def build_orders(n_rows: int, n_users: int, rng: np.random.Generator) -> pa.Table:
    """订单事实表：用户、类目、区域、渠道、数量、单价、退货标记。"""
    # 用户分布：幂律，制造少量高频用户（更接近真实电商）
    weights = 1.0 / np.arange(1, n_users + 1) ** 1.1
    weights /= weights.sum()
    user_id = rng.choice(np.arange(1, n_users + 1, dtype=np.int64), size=n_rows, p=weights)

    category = rng.choice(common.CATEGORIES, size=n_rows, p=common.CATEGORY_WEIGHTS)
    region = rng.choice(common.REGIONS, size=n_rows, p=common.REGION_WEIGHTS)
    channel = rng.choice(common.CHANNELS, size=n_rows, p=common.CHANNEL_WEIGHTS)

    quantity = rng.poisson(2.5, size=n_rows) + 1
    unit_price = np.round(rng.lognormal(mean=3.0, sigma=0.6, size=n_rows), 2)
    revenue = np.round(quantity * unit_price, 2)

    # 下单时间：均匀铺满整个数据跨度
    ts = rng.integers(_to_ns(common.DATA_START), _to_ns(common.DATA_END), size=n_rows)
    order_ts = ts.astype("datetime64[ns]")

    # 退货率与渠道相关
    return_p = np.array(common.CHANNEL_RETURN_RATE)[[common.CHANNELS.index(c) for c in channel]]
    is_returned = rng.random(n_rows) < return_p

    return pa.table(
        {
            "order_id": pa.array(np.arange(1, n_rows + 1, dtype=np.int64)),
            "user_id": pa.array(user_id),
            "order_ts": pa.array(order_ts),
            "region": pa.array(region),
            "category": pa.array(category),
            "channel": pa.array(channel),
            "quantity": pa.array(quantity.astype(np.int32)),
            "unit_price": pa.array(unit_price),
            "revenue": pa.array(revenue),
            "is_returned": pa.array(is_returned),
        }
    )


def write_duckdb(orders: pa.Table, users: pa.Table) -> None:
    """把两份 Parquet 物化进一个 DuckDB 文件，供 CLI 交互使用。"""
    # 先删旧文件：CREATE OR REPLACE TABLE 会复用/追加块，重复造数会让文件越来越大
    common.DUCKDB_FILE.unlink(missing_ok=True)
    con = duckdb.connect(str(common.DUCKDB_FILE))
    try:
        orders_path = _sql_literal(common.ORDERS_PARQUET)
        users_path = _sql_literal(common.USERS_PARQUET)
        con.execute(f"CREATE OR REPLACE TABLE orders AS SELECT * FROM read_parquet({orders_path})")
        con.execute(f"CREATE OR REPLACE TABLE users AS SELECT * FROM read_parquet({users_path})")
        con.execute("CHECKPOINT")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 DuckDB / Polars 对比用的演示数据")
    parser.add_argument("--rows", type=int, default=common.DEFAULT_ROWS, help="订单行数")
    parser.add_argument("--users", type=int, default=common.DEFAULT_USERS, help="用户数")
    parser.add_argument("--no-duckdb", action="store_true", help="跳过生成 demo.duckdb")
    args = parser.parse_args()

    rng = np.random.default_rng(common.SEED)

    common.DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"生成订单 {args.rows:,} 行 …")
    orders = build_orders(args.rows, args.users, rng)
    pq.write_table(orders, common.ORDERS_PARQUET, compression="zstd")

    print(f"生成用户 {args.users:,} 行 …")
    users = build_users(args.users, rng)
    pq.write_table(users, common.USERS_PARQUET, compression="zstd")

    if not args.no_duckdb:
        print("物化 demo.duckdb …")
        write_duckdb(orders, users)

    print("\n完成：")
    for path in (common.ORDERS_PARQUET, common.USERS_PARQUET, common.DUCKDB_FILE):
        if path.exists():
            size_mb = path.stat().st_size / 1024 / 1024
            print(f"  {path.relative_to(common.PROJECT_ROOT)}  ({size_mb:.1f} MB)")
    print(f"\n分析窗口：[{common.WINDOW_START}, {common.WINDOW_END})")


if __name__ == "__main__":
    main()
