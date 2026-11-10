"""时间轴与序列生成：造一串时间点、补齐缺失的时间段、按时间粒度聚合。

时间类的活儿最容易出「看起来对、其实差一天」的错，这一场专门盯这些：

* **端点算不算**：DuckDB 的 `generate_series` 包含终点，Polars 的 `int_range` 不包含
  （`date_range` 又包含）—— 同一个「1 到 5」三个引擎三种答案。
* **时间类型**：DuckDB 有 DATE / TIMESTAMP / TIMESTAMPTZ / INTERVAL 四套，
  Polars 是 Date / Datetime / Datetime(time_zone) / Duration。
* **补齐缺失的时间段**：DuckDB 是「生成完整轴 + LEFT JOIN」；Polars 有现成的 `upsample`，
  但它只补**中间**的空档，不补两头。
* **截断**：`dt.month_start()` 名字叫 start，却**不清零时分秒**。
* **月份加减**：`pl.duration()` 根本没有 months 参数，月份得用 `dt.offset_by()`。

用法：
    uv run python src/timeseries_demo.py
    uv run python src/timeseries_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- 1. 端点


def scenario_endpoints(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 序列的端点算不算 —— 同一句「1 到 5」，三种答案")

    print("DuckDB generate_series（返回 LIST）：")
    for args in ["1, 5, 1", "1, 5, 2", "1, 6, 2"]:
        rows = con.sql(f"SELECT generate_series({args})").fetchall()
        print(f"  generate_series({args}) -> {rows[0][0]}")

    print("\nDuckDB range（表函数，语义不同）：")
    range_rows = [r[0] for r in con.sql("SELECT * FROM range(1, 5, 1)").fetchall()]
    print(f"  SELECT * FROM range(1, 5, 1) -> {range_rows}")
    print("  → range 的**终点不包含**，generate_series 的**终点包含**。")

    print("\nPolars int_range（终点不包含）：")
    for args in ["(1, 6)", "(1, 6, 2)", "(1, 7, 2)"]:
        vals = pl.select(eval(f"pl.int_range{args}")).to_series().to_list()
        print(f"  pl.int_range{args} -> {vals}")

    print("\nPolars date_range（终点包含）：")
    vals = pl.select(pl.date_range(date(2024, 1, 1), date(2024, 1, 5), "1d")).to_series().to_list()
    print(f"  pl.date_range(2024-01-01, 2024-01-05, '1d') -> {vals}")

    print("\n→ 三条要记住的：")
    print("  · DuckDB 内部就有两套：generate_series 含终点，range 不含。")
    print("  · Polars 的 int_range 不含终点，date_range 含终点 —— 同一套 API 里两个口径。")
    print("  · 迁移时最容易漏的就是这个差一：polars 的 int_range(1, 5) 只有 4 个数。")
    print("    要和 DuckDB 的 generate_series(1, 5, 1) 对齐，得写 int_range(1, 6)。")


# ---------------------------------------------------------------- 2. 时间类型


def scenario_types(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 两边各有哪些时间类型")

    duck_types = con.sql(
        "SELECT typeof(DATE '2024-01-01'), typeof(TIMESTAMP '2024-01-01'), "
        "typeof(TIMESTAMP WITH TIME ZONE '2024-01-01'), typeof(INTERVAL 1 DAY)"
    ).fetchall()[0]
    print(f"  DuckDB: DATE={duck_types[0]}  TIMESTAMP={duck_types[1]}")
    print(f"          TIMESTAMPTZ={duck_types[2]}  INTERVAL={duck_types[3]}")

    print("\n  Polars:")
    for label, dtype in [
        ("Date", pl.Date),
        ("Datetime", pl.Datetime("us")),
        ("Datetime(ns)", pl.Datetime("ns")),
        ("Datetime(us, tz)", pl.Datetime("us", "Asia/Shanghai")),
        ("Duration", pl.Duration),
    ]:
        print(f"    {label:18} -> {pl.Series([], dtype=dtype).dtype}")

    print("\nDuckDB 的 generate_series 在时间上有个坑：起点是 DATE 时，结果是 TIMESTAMP。")
    rows = con.sql(
        "SELECT typeof(day) FROM (SELECT day FROM generate_series("
        "DATE '2024-01-01', DATE '2024-01-03', INTERVAL 1 DAY) AS g(day)) LIMIT 1"
    ).fetchall()
    print(f"  generate_series(DATE, DATE, INTERVAL) 的结果类型 -> {rows[0][0]}")
    print("  → 和 DATE 列 JOIN 时会走隐式类型转换，能对上但不报错，别以为两边类型一样。")

    print(
        "\n时区：DuckDB 的 TIMESTAMPTZ 存的是 UTC 瞬时点，Polars 的 Datetime(tz) 存的是带偏移的值。"
    )
    utc = con.sql("SELECT (TIMESTAMPTZ '2024-01-01 08:00:00+08') AT TIME ZONE 'UTC'").fetchall()[0][
        0
    ]
    sh = con.sql(
        "SELECT (TIMESTAMPTZ '2024-01-01 08:00:00+08') AT TIME ZONE 'Asia/Shanghai'"
    ).fetchall()[0][0]
    print(f"  DuckDB AT TIME ZONE 'UTC'            -> {utc}")
    print(f"  DuckDB AT TIME ZONE 'Asia/Shanghai'  -> {sh}")
    naive = pl.Series([datetime(2024, 1, 1, 0, 0, 0)], dtype=pl.Datetime("us"))
    utc_aware = naive.dt.replace_time_zone("UTC")
    print(f"  Polars replace_time_zone('UTC')      -> {utc_aware.to_list()[0]}")
    shanghai = utc_aware.dt.convert_time_zone("Asia/Shanghai").to_list()[0]
    print(f"  Polars convert_time_zone('Asia/Shanghai') -> {shanghai}")
    print("  → 同一个瞬时点，两边都能换算；DuckDB 叫 AT TIME ZONE，Polars 叫 convert_time_zone。")


# ---------------------------------------------------------------- 3. 补齐缺失时间段


def scenario_fill_gaps(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 补齐缺失的时间段：upsample 只补「中间」")

    df = pl.DataFrame(
        {
            "day": pl.Series([date(2024, 1, 1), date(2024, 1, 4)], dtype=pl.Date),
            "v": [1, 4],
        }
    )
    print(f"原始数据：{df.to_dicts()}\n")

    print("DuckDB：生成完整轴 + LEFT JOIN（轴的范围是自己定的，想多长都行）")
    con.execute("CREATE TABLE ts (day DATE, v INTEGER)")
    con.execute("INSERT INTO ts VALUES ('2024-01-01',1),('2024-01-04',4)")
    rows = con.sql(
        """
        SELECT g.day, t.v
        FROM generate_series(DATE '2024-01-01', DATE '2024-01-05', INTERVAL 1 DAY) g(day)
        LEFT JOIN ts t ON t.day = g.day
        ORDER BY g.day
        """
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}  v={r[1]}")

    print("\nPolars upsample（现成的补齐函数）")
    up = df.upsample(time_column="day", every="1d").sort("day")
    for r in up.to_dicts():
        print(f"  {r['day']}  v={r['v']}")
    print(f"  → 行数 {up.height}：只补了 01-01 到 01-04 **中间**的空档，")
    print("    最后一格 01-05 没有 —— 数据里没有，upsample 也不造。")

    print("\nPolars 想补到 01-05，只能自己 join 一条完整轴：")
    axis = pl.DataFrame(
        {"day": pl.select(pl.date_range(date(2024, 1, 1), date(2024, 1, 5), "1d")).to_series()}
    )
    filled = axis.join(df, on="day", how="left").sort("day")
    for r in filled.to_dicts():
        print(f"  {r['day']}  v={r['v']}")
    print(f"  → 行数 {filled.height}，和 DuckDB 上面那段一致。")

    print("\n→ 两条路：Polars 的 upsample 更快（不用自己造轴），")
    print("  但「补齐的时间轴范围」得自己定；DuckDB 那边范围本来就是 SQL 的一部分。")


# ---------------------------------------------------------------- 4. 截断


def scenario_truncate(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 按粒度截断：month_start 的名字骗人")

    ts = "2024-03-15 13:45:30"
    duck_month = q1(con, f"SELECT date_trunc('month', TIMESTAMP '{ts}')")
    duck_day = q1(con, f"SELECT date_trunc('day', TIMESTAMP '{ts}')")
    print(f"  DuckDB date_trunc('month', {ts}) -> {duck_month}")
    print(f"  DuckDB date_trunc('day',   {ts}) -> {duck_day}")

    df = pl.DataFrame(
        {"x": pl.Series([datetime(2024, 3, 15, 13, 45, 30)], dtype=pl.Datetime("us"))}
    )
    pol_month = df.select(pl.col("x").dt.truncate("1mo"))["x"].to_list()[0]
    pol_day = df.select(pl.col("x").dt.truncate("1d"))["x"].to_list()[0]
    pol_month_start = df.select(pl.col("x").dt.month_start())["x"].to_list()[0]
    print(f"  Polars dt.truncate('1mo')            -> {pol_month}")
    print(f"  Polars dt.truncate('1d')             -> {pol_day}")
    print(f"  Polars dt.month_start()              -> {pol_month_start}   ← 时分秒还在！")

    print("\n→ dt.truncate 和 dt.month_start 不是一回事：")
    print("  truncate 会把时分秒清零；month_start 只把日期挪到 1 号，时间原样保留。")
    print("  要「当月第一天零点」，得用 truncate('1mo')，别用 month_start。")

    print("\nDate 列上截断，返回类型还不一样：")
    duck_date_trunc = con.sql(
        "SELECT date_trunc('month', DATE '2024-03-15'), "
        "typeof(date_trunc('month', DATE '2024-03-15'))"
    ).fetchall()[0]
    pol_date_trunc = (
        pl.DataFrame({"x": pl.Series([date(2024, 3, 15)], dtype=pl.Date)})
        .select(pl.col("x").dt.truncate("1mo"))["x"]
        .to_list()[0]
    )
    print(
        f"  DuckDB date_trunc('month', DATE) -> {duck_date_trunc[0]}（类型 {duck_date_trunc[1]}）"
    )
    print(f"  Polars dt.truncate('1mo') on Date -> {pol_date_trunc}（类型 Date）")
    print("  → DuckDB 把 DATE 提升成了 TIMESTAMP，Polars 保持 Date。")
    print("    拿结果去跟 DATE 列 JOIN 时行为会不一样（一个要隐式转换，一个正好匹配）。")


# ---------------------------------------------------------------- 5. 间隔加减


def scenario_intervals(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 间隔加减：月份在 Polars 里不是 Duration")

    base = "2024-03-15 13:45:30"
    print(f"起点 {base}\n")
    duck_day = q1(con, "SELECT TIMESTAMP '2024-03-15 13:45:30' + INTERVAL 1 DAY")
    duck_month = q1(con, "SELECT TIMESTAMP '2024-03-15 13:45:30' + INTERVAL 1 MONTH")
    print(f"  DuckDB TIMESTAMP + INTERVAL 1 DAY   -> {duck_day}")
    print(f"  DuckDB TIMESTAMP + INTERVAL 1 MONTH -> {duck_month}")

    df = pl.DataFrame(
        {"x": pl.Series([datetime(2024, 3, 15, 13, 45, 30)], dtype=pl.Datetime("us"))}
    )
    plus_day = df.select(pl.col("x") + pl.duration(days=1))["x"].to_list()[0]
    print(f"  Polars x + pl.duration(days=1)      -> {plus_day}")
    try:
        df.select(pl.col("x") + pl.duration(months=1))
        print("  Polars x + pl.duration(months=1)   -> 成功（不符合预期）")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"  Polars x + pl.duration(months=1)   -> {detail}")
    offset_month = df.select(pl.col("x").dt.offset_by("1mo"))["x"].to_list()[0]
    print(f"  Polars x.dt.offset_by('1mo')        -> {offset_month}")

    print("\n→ 「加一个月」在 Polars 里必须走 dt.offset_by()，")
    print("  因为 Duration 只有天/时/分/秒这些固定单位，月份长度不固定，进不了 Duration。")
    print("  DuckDB 的 INTERVAL 1 MONTH 直接就是日历月。")

    print("\n两点之差：")
    duck_diff = q1(con, "SELECT date_diff('day', DATE '2024-01-01', DATE '2024-03-01')")
    print(f"  DuckDB date_diff('day', '2024-01-01', '2024-03-01') -> {duck_diff}")
    diff_df = pl.DataFrame(
        {
            "a": pl.Series([datetime(2024, 1, 1)], dtype=pl.Datetime("us")),
            "b": pl.Series([datetime(2024, 3, 1)], dtype=pl.Datetime("us")),
        }
    )
    pol_days = diff_df.select((pl.col("b") - pl.col("a")).dt.total_days())["b"].to_list()[0]
    print(f"  Polars (b - a).dt.total_days()                        -> {pol_days}")


# ---------------------------------------------------------------- 6. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 6 · 时间轴的代价")

    print(f"数据：{rows} 行事件（每行一个时间戳）")
    print(f"机器：{common.describe_machine()}")

    base = datetime(2024, 1, 1)
    df = pl.DataFrame(
        {
            "ts": pl.Series(
                [base + timedelta(hours=i) for i in range(rows)], dtype=pl.Datetime("us")
            ),
            "v": [float(i % 100) for i in range(rows)],
        }
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="timeseries_demo_"))
    src = tmpdir / "events.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE events AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    def duck_run() -> pl.DataFrame:
        rel = con.sql(
            "SELECT date_trunc('day', ts) AS day, count(*) AS cnt, sum(v) AS total "
            "FROM events GROUP BY day ORDER BY day"
        )
        try:
            return pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def polars_run() -> pl.DataFrame:
        return (
            polars_src.group_by(pl.col("ts").dt.truncate("1d"))
            .agg(
                pl.len().alias("cnt"),
                pl.col("v").sum().alias("total"),
            )
            .rename({"ts": "day"})
            .sort("day")
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

    print("\n【按天聚合】")
    print(
        "\n"
        + common.pad("引擎", 12)
        + common.pad_left("中位数 (ms)", 16)
        + common.pad_left("最快 (ms)", 16)
    )
    print("-" * 44)
    print(
        common.pad("DuckDB", 12)
        + common.pad_left(f"{duck_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(duck_times) * 1000:.1f}", 16)
    )
    print(
        common.pad("Polars", 12)
        + common.pad_left(f"{polars_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(polars_times) * 1000:.1f}", 16)
    )
    if polars_med > 0:
        print(f"\nDuckDB / Polars = {duck_med / polars_med:.2f}×（中位数口径）")

    # 生成时间轴本身的代价
    axis_rows = 1_000_000
    stop = base + timedelta(hours=axis_rows - 1)

    def duck_axis() -> int:
        return int(
            con.sql(
                f"SELECT count(*) FROM generate_series(TIMESTAMP '{base}', "
                f"TIMESTAMP '{stop}', INTERVAL 1 HOUR)"
            ).fetchall()[0][0]
        )

    def polars_axis() -> int:
        return pl.select(pl.datetime_range(base, stop, "1h")).height

    duck_axis()
    polars_axis()
    duck_axis_times: list[float] = []
    polars_axis_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        duck_axis()
        duck_axis_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        polars_axis()
        polars_axis_times.append(time.perf_counter() - t0)

    duck_axis_med = statistics.median(duck_axis_times)
    polars_axis_med = statistics.median(polars_axis_times)
    print(f"\n【生成 {axis_rows} 个小时点的时间轴】")
    print(
        "\n"
        + common.pad("引擎", 12)
        + common.pad_left("中位数 (ms)", 16)
        + common.pad_left("最快 (ms)", 16)
    )
    print("-" * 44)
    print(
        common.pad("DuckDB", 12)
        + common.pad_left(f"{duck_axis_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(duck_axis_times) * 1000:.1f}", 16)
    )
    print(
        common.pad("Polars", 12)
        + common.pad_left(f"{polars_axis_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(polars_axis_times) * 1000:.1f}", 16)
    )
    if polars_axis_med > 0:
        print(f"\nDuckDB / Polars = {duck_axis_med / polars_axis_med:.2f}×（中位数口径）")

    print("\n口径：输入已物化，计时的只有「按天聚合 + 结果物化」和「生成时间轴」。")
    print("注意 DuckDB 的 generate_series 是**列表函数**，聚合时还得再 unnest 一次，")
    print("  这里为了两边做同一件事，用 count(*) 只测生成本身的成本。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的时间轴与序列生成差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_endpoints(con)
        scenario_types(con)
        scenario_fill_gaps(con)
        scenario_truncate(con)
        scenario_intervals(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
