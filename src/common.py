"""两个引擎共用的口径：数据规模、时间窗口、字段定义、结果比较工具。

这个模块是「唯一事实来源」：造数、SQL 实现、Polars 实现、基准测试都从这里取常量，
避免三个地方各写一份口径导致对比结果失真。
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime
from pathlib import Path

import polars as pl

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ORDERS_PARQUET = DATA_DIR / "orders.parquet"
USERS_PARQUET = DATA_DIR / "users.parquet"
DUCKDB_FILE = DATA_DIR / "demo.duckdb"

# ---------------------------------------------------------------- 造数参数

SEED = 42
DEFAULT_ROWS = 1_000_000
DEFAULT_USERS = 20_000

# 数据的完整时间跨度（比分析窗口大，让 WHERE 过滤真正做功）
DATA_START = datetime(2023, 7, 1)
DATA_END = datetime(2025, 6, 30, 23, 59, 59)

# 分析窗口：左闭右开 [WINDOW_START, WINDOW_END)
WINDOW_START = datetime(2024, 1, 1)
WINDOW_END = datetime(2024, 7, 1)

# ---------------------------------------------------------------- 维度取值

REGIONS = ["华东", "华北", "华南", "西南", "华中"]
REGION_WEIGHTS = [0.32, 0.22, 0.20, 0.13, 0.13]

CATEGORIES = ["数码", "家居", "服饰", "食品", "美妆", "运动"]
CATEGORY_WEIGHTS = [0.18, 0.16, 0.22, 0.20, 0.14, 0.10]

CHANNELS = ["app", "web", "小程序", "门店"]
CHANNEL_WEIGHTS = [0.40, 0.28, 0.22, 0.10]

COUNTRIES = ["中国", "日本", "韩国", "新加坡", "马来西亚"]
COUNTRY_WEIGHTS = [0.72, 0.10, 0.08, 0.06, 0.04]

TIERS = ["普通", "白银", "黄金", "钻石"]
TIER_WEIGHTS = [0.55, 0.25, 0.15, 0.05]

# 渠道相关的退货率：门店/小程序退货率明显更高
CHANNEL_RETURN_RATE = [0.05, 0.07, 0.12, 0.18]

# ---------------------------------------------------------------- 分析口径

TOP_N_PER_REGION = 3
TOP_CUSTOMERS = 20

# 结果列顺序 —— 两个引擎必须完全一致，否则等价性校验无法进行
REGION_CATEGORY_COLUMNS = [
    "region",
    "category",
    "order_cnt",
    "units",
    "revenue",
    "avg_order_value",
    "revenue_share_in_region",
    "rank_in_region",
]

DAILY_COLUMNS = ["order_date", "order_cnt", "units", "revenue", "avg_order_value"]

CUSTOMER_COLUMNS = [
    "user_id",
    "country",
    "tier",
    "order_cnt",
    "revenue",
    "first_order_date",
    "last_order_date",
]

# 每个查询的比较主键（用于排序后再逐值比对）
QUERY_KEYS = {
    "region_category": ["region", "rank_in_region"],
    "daily_revenue": ["order_date"],
    "top_customers": ["user_id"],
}


# ---------------------------------------------------------------- 结果比较


def compare_frames(
    left: pl.DataFrame,
    right: pl.DataFrame,
    keys: list[str],
    *,
    rtol: float = 1e-9,
    atol: float = 1e-6,
) -> tuple[bool, str]:
    """比较两个引擎的输出是否等价。

    先按 keys 排序消除行序差异，再逐列比对：浮点列用相对/绝对容差，
    其余列要求完全相等（含 NULL 位置）。

    返回 (是否等价, 失败原因)。
    """
    if left.columns != right.columns:
        return False, f"列不一致:\n  duckdb = {left.columns}\n  polars = {right.columns}"
    if left.height != right.height:
        return False, f"行数不一致: duckdb={left.height}, polars={right.height}"

    lhs = left.sort(keys)
    rhs = right.sort(keys)

    for col in left.columns:
        lvals = lhs[col].to_list()
        rvals = rhs[col].to_list()
        is_float = lhs.schema[col] in (pl.Float32, pl.Float64)
        for i, (a, b) in enumerate(zip(lvals, rvals, strict=True)):
            if a is None or b is None:
                if a is not b:
                    return False, f"列 `{col}` 第 {i} 行 NULL 不一致: {a} vs {b}"
                continue
            if is_float:
                # NaN == NaN 视为相等（math.isclose(NaN, NaN) 是 False）
                if isinstance(a, float) and isinstance(b, float):
                    if math.isnan(a) and math.isnan(b):
                        continue
                if not math.isclose(a, b, rel_tol=rtol, abs_tol=atol):
                    return False, f"列 `{col}` 第 {i} 行数值不一致: {a!r} vs {b!r}"
            elif a != b:
                return False, f"列 `{col}` 第 {i} 行不一致: {a!r} vs {b!r}"

    return True, f"{left.height} 行 × {len(left.columns)} 列全部一致"


# ---------------------------------------------------------------- 机器状态


def load1() -> float:
    """1 分钟平均负载（/proc/loadavg 的第一个数）。"""
    with open("/proc/loadavg", encoding="utf-8") as fh:
        return float(fh.read().split()[0])


def describe_machine() -> str:
    """机器负载的一句话描述 —— 这台是共享开发机，数字被谁压着必须一起看。"""
    import os

    cpus = os.cpu_count() or 1
    ld = load1()
    if ld > cpus:
        return f"{cpus} 核，load1 = {ld:.1f}（> 核数 → 机器正被别人占着，数字是「有竞争时」的）"
    if ld > cpus * 0.6:
        return f"{cpus} 核，load1 = {ld:.1f}（偏高，测量结果会偏保守）"
    return f"{cpus} 核，load1 = {ld:.1f}（空闲）"


# ---------------------------------------------------------------- 输出格式化


def print_frame(df: pl.DataFrame, title: str, max_rows: int = 20) -> None:
    """统一两个引擎的表格输出样式。"""
    print(f"\n=== {title} ===")
    with pl.Config(
        tbl_rows=max_rows,
        tbl_cols=-1,
        tbl_width_chars=120,
        fmt_str_lengths=12,
        tbl_formatting="UTF8_FULL",
    ):
        print(df)


def format_ms(seconds: float) -> str:
    return f"{seconds * 1000:.1f}"


# ---------------------------------------------------------------- 终端排版
# 中文是全角字符，str.ljust 按字符数补位会导致列错位，这里按显示宽度计算。


def display_width(text: str) -> int:
    """字符串在等宽终端里占的列数（全角字符算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    return text + " " * max(0, width - display_width(text))


def pad_left(text: str, width: int) -> str:
    """按显示宽度右对齐补空格。"""
    return " " * max(0, width - display_width(text)) + text
