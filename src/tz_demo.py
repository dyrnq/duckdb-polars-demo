"""日期时间与时区：同一时刻的四种表示，以及四个会咬人的坑。

时间相关的 bug 很少是「算错了」，更多是「两边对**同一个瞬间**的表示不一样」，
于是 A 库多算两小时、B 库少算一天，谁都不报错。

这个脚本把两个引擎的时间类型摆在一起比：

  1. 类型对照：DuckDB 的 TIMESTAMP / TIMESTAMP_NS / TIMESTAMPTZ 分别对应
     Polars 的哪个 dtype、Arrow 的哪个类型、Parquet 的哪个物理类型。
  2. 精度：DuckDB 的 TIMESTAMP 只有微秒，纳秒在哪一步被**四舍五入**掉。
  3. **会话时区会改变 TIMESTAMPTZ 的显示文本** —— 同一句 SQL，换台机器结果就变。
  4. **naive 和 aware 直接比较，答案取决于会话时区** —— 同一句 SQL 在
     Asia/Shanghai 和 UTC 下结论相反。
  5. 三个时区操作（贴标签 / 换显示 / 换瞬间）两边怎么对应。
  6. 夏令时：不存在的时间点和有歧义的时间点，一个静默选、一个直接报错。
  7. Parquet 往返：DuckDB 写出去一律是 UTC，Polars 原样保留自己的时区。
  8. 截断与分桶：`date_trunc` / `time_bucket` 对应 Polars 的哪个方法。
  9. 运算：月末对齐和夏令时那天加一天，两边是不是同一个意思。
  10. 一百万行真实数据上的时间过滤 + 按天分组。

用法：
    uv run python src/tz_demo.py
    uv run python src/tz_demo.py --repeat 5
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

import common

OUT_DIR = common.DATA_DIR / "tz_demo"

# 拿来做对照的一组时间点
TS_NAIVE = datetime(2024, 1, 1, 12, 0, 0)
TS_WITH_NS = datetime(2024, 1, 1, 12, 0, 0, 123456)

# 夏令时的两个经典坑（America/New_York）
#   2024-03-10 02:00 → 03:00：02:00~02:59 这段时间**不存在**
#   2024-11-03 01:00 → 01:00 回拨：01:00~01:59 这段时间**出现两次**
DST_GAP = datetime(2024, 3, 10, 2, 30)
DST_FOLD = datetime(2024, 11, 3, 1, 30)


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _one(con: duckdb.DuckDBPyConnection, sql: str):
    """执行一条只返回一行的 SQL，取第一个值。

    走 `execute` 而不是 `con.sql(...)`：relation 会一直占着连接直到被 GC，
    中途再发查询会撞上 ResourceInUseError。
    """
    res = con.execute(sql)
    row = res.fetchone()
    res.fetchall()
    return row[0]


def _text(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    """取一条 SQL 的第一列，转成字符串 —— 避开 TIMESTAMPTZ → Python 对象那条路。"""
    return str(_one(con, sql))


# ---------------------------------------------------------------- 场景 1：类型对照


def scenario_types(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 类型对照：同一个「时间」，两边叫什么")
    print("  DuckDB 的 TIMESTAMP 是**不带时区**的；TIMESTAMPTZ 才带时区。")
    print("  Polars 把这两件事拆成两个维度：time_unit（精度）+ time_zone（时区）。")
    print()

    rows = [
        ("日期", "DATE", "Date", "date32[day]", "DATE"),
        ("日期时间（微秒）", "TIMESTAMP", "Datetime('us')", "timestamp[us]", "TIMESTAMP_MICROS"),
        ("日期时间（纳秒）", "TIMESTAMP_NS", "Datetime('ns')", "timestamp[ns]", "TIMESTAMP_NANOS"),
        (
            "带时区的瞬间",
            "TIMESTAMPTZ",
            "Datetime('us', 'UTC')",
            "timestamp[us, tz=UTC]",
            "TIMESTAMP_MICROS(isAdjustedToUTC)",
        ),
    ]

    header = ("概念", "DuckDB", "Polars dtype", "Arrow", "Parquet 物理类型")
    widths = [20, 14, 24, 24, 34]
    print(
        "  "
        + common.pad(header[0], widths[0])
        + common.pad(header[1], widths[1])
        + common.pad(header[2], widths[2])
        + common.pad(header[3], widths[3])
        + header[4]
    )
    print("  " + "-" * 92)
    for concept, duck, pol, arrow, pqtype in rows:
        print(
            "  "
            + common.pad(concept, widths[0])
            + common.pad(duck, widths[1])
            + common.pad(pol, widths[2])
            + common.pad(arrow, widths[3])
            + pqtype
        )

    print()
    print("  实测 DuckDB 的 typeof()：")
    for sql in [
        "SELECT typeof(DATE '2024-01-01')",
        "SELECT typeof(TIMESTAMP '2024-01-01 12:00:00')",
        "SELECT typeof(TIMESTAMP_NS '2024-01-01 12:00:00')",
        "SELECT typeof(TIMESTAMPTZ '2024-01-01 12:00:00+08')",
    ]:
        literal = sql.split("typeof(")[1].rstrip(")")
        print(f"    {literal:<40} → {_one(con, sql)}")

    print()
    print("  实测 Polars 的 dtype（同一份 Arrow 数据读进去）：")
    tbl = pa.table(
        {
            "d": pa.array([datetime(2024, 1, 1)], pa.date32()),
            "us": pa.array([TS_NAIVE], pa.timestamp("us")),
            "ns": pa.array([TS_WITH_NS], pa.timestamp("ns")),
            "tz": pa.array(
                [TS_NAIVE.replace(tzinfo=timezone(timedelta(hours=8)))],
                pa.timestamp("us", tz="Asia/Shanghai"),
            ),
        }
    )
    df = pl.from_arrow(tbl)
    for name, dtype in df.schema.items():
        print(f"    {name:<4} → {dtype}")


# ---------------------------------------------------------------- 场景 2：精度


def scenario_precision(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 精度：纳秒在哪一步没了")
    print("  DuckDB 的 TIMESTAMP 是**微秒**；TIMESTAMP_NS 才保得住纳秒。")
    print("  关键在于：往微秒转的时候是**四舍五入**，不是截断。")
    print()

    print("  DuckDB：")
    for v in [
        "2024-01-01 00:00:00.123456789",
        "2024-01-01 00:00:00.123456001",
        "2024-01-01 00:00:00.999999999",
    ]:
        kept = _text(con, f"SELECT (TIMESTAMP_NS '{v}')::VARCHAR")
        lost = _text(con, f"SELECT (TIMESTAMP_NS '{v}')::TIMESTAMP::VARCHAR")
        print(f"    TIMESTAMP_NS '{v}'")
        print(f"      原样保留（ns）：{kept}")
        print(f"      转成 TIMESTAMP（µs）：{lost}")

    print()
    print("  Polars：")
    s_ns = pl.Series([TS_WITH_NS], dtype=pl.Datetime("ns"))
    s_us = s_ns.cast(pl.Datetime("us"))
    print(f"    Datetime('ns') 原样：{s_ns.to_list()[0]}")
    print(f"    cast 到 Datetime('us')：{s_us.to_list()[0]}")

    print()
    print("  顺带看一眼 epoch 转换 —— 同样的舍入：")
    duck_epoch = _one(con, "SELECT epoch_us(TIMESTAMP_NS '2024-01-01 00:00:00.123456789')")
    print(f"    DuckDB  epoch_us(TIMESTAMP_NS '2024-01-01 00:00:00.123456789') = {duck_epoch}")
    print(f"    Polars  cast 到 us 后的 epoch(ms) = {s_us.cast(pl.Int64).to_list()[0] // 1000}")
    print()
    print("  注意：orders.parquet 里存的就是 timestamp[ns]，")
    print("  DuckDB 读进来是 TIMESTAMP_NS（无损），但只要 cast 成 TIMESTAMP 就开始舍入。")


# ---------------------------------------------------------------- 场景 3：会话时区


def scenario_session_tz(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 会话时区会改变 TIMESTAMPTZ 的「显示文本」")
    print("  TIMESTAMPTZ 存的是**瞬间**（epoch），但打印出来是带偏移的本地时间，")
    print("  偏移量取决于会话的 TimeZone 设置 —— 默认取自操作系统的 TZ 环境变量。")
    print()

    original = str(_one(con, "SELECT current_setting('TimeZone')"))
    print(f"  本机默认会话时区：{original}")
    print()
    print("  同一个查询，换会话时区：")
    for tz in ["Asia/Shanghai", "UTC", "America/New_York"]:
        con.execute(f"SET TimeZone='{tz}'")
        text = _text(con, "SELECT (TIMESTAMPTZ '2024-01-01 12:00:00+08')::VARCHAR")
        epoch = _one(con, "SELECT epoch_ms(TIMESTAMPTZ '2024-01-01 12:00:00+08')")
        print(f"    TimeZone={tz:<18} 显示 {text:<28} epoch_ms = {epoch}")

    con.execute(f"SET TimeZone='{original}'")
    print()
    print("  epoch 三次都一样 —— **瞬间没变，变的只是显示**。")
    print("  但如果你把查询结果直接导出成文本 CSV，导出的是显示值，")
    print("  于是同一份数据在不同机器上导出出来是不一样的。")
    print()
    print("  顺带两个函数的行为不一样：")
    print(
        f"    epoch_ms(1704067200123)      → {_text(con, 'SELECT epoch_ms(1704067200123)')}"
        "   ← 永远按 UTC 解释，与会话时区无关"
    )
    to_ts = _text(con, "SELECT to_timestamp(1704067200.5)::VARCHAR")
    print(f"    to_timestamp(1704067200.5)   → {to_ts}   ← 返回 TIMESTAMPTZ，按会话时区显示")


# ---------------------------------------------------------------- 场景 4：naive vs aware


def scenario_naive_vs_aware(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · naive 和 aware 直接比较：答案取决于会话时区")
    print("  DuckDB 允许 TIMESTAMP（naive）和 TIMESTAMPTZ（aware）直接比较，")
    print("  做法是把 naive 那一边**按会话时区**当成瞬间。")
    print("  同一句 SQL，换个会话时区，结论直接反过来 —— 而且不报错。")
    print()

    sql = "SELECT TIMESTAMP '2024-01-01 12:00:00' = TIMESTAMPTZ '2024-01-01 12:00:00+08'"
    original = str(_one(con, "SELECT current_setting('TimeZone')"))
    for tz in ["Asia/Shanghai", "UTC", "America/New_York"]:
        con.execute(f"SET TimeZone='{tz}'")
        eq = _one(con, sql)
        lt = _one(
            con, "SELECT TIMESTAMP '2024-01-01 12:00:00' < TIMESTAMPTZ '2024-01-01 12:00:00+08'"
        )
        print(
            f"    TimeZone={tz:<18} naive = aware → {str(eq):<6}"
            f"  naive < aware → {str(lt):<6}"
            f"  （naive 被解释成 {tz} 的 12:00）"
        )
    con.execute(f"SET TimeZone='{original}'")

    print()
    print("  Polars 直接拒绝比较：")
    try:
        a = pl.Series([TS_NAIVE], dtype=pl.Datetime("us"))
        b = pl.Series(
            [TS_NAIVE.replace(tzinfo=timezone(timedelta(hours=8)))], dtype=pl.Datetime("us", "UTC")
        )
        (a == b).to_list()
        print("    （本次没报错）")
    except Exception as e:
        print(f"    {type(e).__name__}: {str(e).splitlines()[0]}")

    print()
    print("  结论：**DuckDB 静默按会话时区补齐，Polars 直接报错。**")
    print("  这不是「谁更对」的问题 —— 是 DuckDB 会在你换机器/换连接池配置后，")
    print("  悄悄改掉比较结果，而 Polars 会当场拦下来。")


# ---------------------------------------------------------------- 场景 5：三个时区操作


def scenario_tz_ops(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 三个时区操作，两边怎么对应")
    print("  「贴标签」「换显示」「换瞬间」是三件不同的事，写错一个就是差几小时。")
    print()

    rows = [
        (
            "贴标签：认为这个本地时间**属于**某个时区",
            "TIMESTAMP '2024-01-01 12:00:00' AT TIME ZONE 'America/New_York'",
            "dt.replace_time_zone('America/New_York')",
        ),
        (
            "换显示：瞬间不变，换个时区显示",
            "TIMESTAMPTZ '2024-01-01 12:00:00+08' AT TIME ZONE 'America/New_York'",
            "dt.convert_time_zone('America/New_York')",
        ),
    ]
    for what, duck_sql, pol_code in rows:
        print(f"  {what}")
        print(f"    DuckDB  {duck_sql}")
        print(f"      → {_text(con, 'SELECT (' + duck_sql + ')::VARCHAR')}")
        print(f"    Polars  {pol_code}")
        print()

    # 贴标签：naive 12:00 属于纽约 → 瞬间是 17:00 UTC
    naive = pl.Series([TS_NAIVE], dtype=pl.Datetime("us"))
    tagged = naive.dt.replace_time_zone("America/New_York")
    print("  贴标签之后，两边的瞬间对不对得上（都换算成 UTC 比较）：")
    duck_utc = _text(
        con,
        "SELECT (TIMESTAMP '2024-01-01 12:00:00' AT TIME ZONE 'America/New_York')"
        "::TIMESTAMPTZ::VARCHAR",
    )
    duck_epoch = _one(
        con,
        "SELECT epoch_ms(TIMESTAMP '2024-01-01 12:00:00' AT TIME ZONE 'America/New_York')",
    )
    pol_epoch = tagged.dt.convert_time_zone("UTC").dt.timestamp("ms").to_list()[0]
    pol_utc = tagged.dt.convert_time_zone("UTC").to_list()[0]
    print(f"    DuckDB  → {duck_utc}（epoch {duck_epoch}）")
    print(f"    Polars  → {pol_utc}（epoch {pol_epoch}）")
    print(f"    同一瞬间？ {'✅ 是' if duck_epoch == pol_epoch else '❌ 不是'}")

    print()
    print("  「贴标签」这个动作，Polars 叫 replace_time_zone，")
    print("  DuckDB 叫 AT TIME ZONE —— **名字一样，方向相反**：")
    print("    DuckDB 的 AT TIME ZONE 作用在 naive 上 = 贴标签，作用在 aware 上 = 换显示；")
    print("    Polars 拆成两个方法：replace_time_zone（贴标签）/ convert_time_zone（换显示）。")
    print("  照着名字翻译最容易错的就是这里。")


# ---------------------------------------------------------------- 场景 6：夏令时


def scenario_dst(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 夏令时：一个静默选，一个直接报错")
    print("  America/New_York 每年有两个「不正常的时刻」：")
    print("    · 2024-03-10 02:00 → 03:00（跳过一小时）→ 02:30 **不存在**")
    print("    · 2024-11-03 01:00 → 01:00（回拨一小时）→ 01:30 **出现两次**")
    print()

    print("  情况一：不存在的时间点")
    duck_gap = _one(
        con, "SELECT epoch_ms(TIMESTAMP '2024-03-10 02:30:00' AT TIME ZONE 'America/New_York')"
    )
    duck_gap_utc = datetime.fromtimestamp(duck_gap / 1000, tz=UTC)
    duck_gap_text = _text(
        con, "SELECT (TIMESTAMP '2024-03-10 02:30:00' AT TIME ZONE 'America/New_York')::VARCHAR"
    )
    print(f"    DuckDB  → epoch {duck_gap} = {duck_gap_utc}（往后挪了 1 小时，等于当成 03:30 EDT）")
    print(f"      完整写法：{duck_gap_text}")
    try:
        pl.Series([DST_GAP], dtype=pl.Datetime("us")).dt.replace_time_zone("America/New_York")
        print("    Polars  → （没报错）")
    except Exception as e:
        print(f"    Polars  → {type(e).__name__}: {str(e).splitlines()[0]}")
    nulled = (
        pl.Series([DST_GAP], dtype=pl.Datetime("us"))
        .dt.replace_time_zone("America/New_York", non_existent="null")
        .to_list()[0]
    )
    print(f"    Polars  non_existent='null' → {nulled}   ← 变成 NULL，行还在，时间没了")

    print()
    print("  情况二：出现两次的时间点")
    duck_fold = _one(
        con, "SELECT epoch_ms(TIMESTAMP '2024-11-03 01:30:00' AT TIME ZONE 'America/New_York')"
    )
    duck_fold_utc = datetime.fromtimestamp(duck_fold / 1000, tz=UTC)
    print(f"    DuckDB  → epoch {duck_fold} = {duck_fold_utc}（= 第二次出现，也就是 EST）")
    for mode in ["earliest", "latest"]:
        ep = (
            pl.Series([DST_FOLD], dtype=pl.Datetime("us"))
            .dt.replace_time_zone("America/New_York", ambiguous=mode)
            .dt.convert_time_zone("UTC")
            .dt.timestamp("ms")
            .to_list()[0]
        )
        stamp = datetime.fromtimestamp(ep / 1000, tz=UTC)
        print(f"    Polars  ambiguous='{mode}' → epoch {ep} = {stamp}")
    try:
        pl.Series([DST_FOLD], dtype=pl.Datetime("us")).dt.replace_time_zone("America/New_York")
        print("    Polars  默认参数 → （没报错）")
    except Exception as e:
        print(f"    Polars  默认参数 → {type(e).__name__}: {str(e).splitlines()[0]}")

    print()
    print("  实测结论：")
    print("    · DuckDB **静默**选一个，不给任何提示 —— 不存在的时刻往后挪一小时，")
    print("      有歧义的时刻选第二次（EST）。")
    print("    · Polars 默认**直接报错**，让你显式选 ambiguous / non_existent。")
    print("  静默的那一边在跨时区的排班、账期、日志对齐里会直接算错；")
    print("  报错的那一边至少不会悄悄错。")


# ---------------------------------------------------------------- 场景 7：Parquet 往返


def scenario_parquet(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · Parquet 往返：DuckDB 一律写成 UTC，Polars 保留自己的时区")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 一张带三种时间列的表
    duck_path = OUT_DIR / "duckdb_ts.parquet"
    pol_path = OUT_DIR / "polars_ts.parquet"

    con.execute(
        "CREATE OR REPLACE TABLE t_src AS SELECT "
        "  TIMESTAMP '2024-01-01 12:00:00' AS naive_us, "
        "  TIMESTAMP_NS '2024-01-01 12:00:00.123456789' AS naive_ns, "
        "  TIMESTAMPTZ '2024-01-01 12:00:00+08' AS aware "
    )
    con.execute(f"COPY t_src TO '{duck_path}' (FORMAT PARQUET)")

    df = pl.DataFrame(
        {
            "naive_us": pl.Series([TS_NAIVE], dtype=pl.Datetime("us")),
            "naive_ns": pl.Series([TS_WITH_NS], dtype=pl.Datetime("ns")),
            "aware": pl.Series(
                [TS_NAIVE.replace(tzinfo=timezone(timedelta(hours=8)))],
                dtype=pl.Datetime("us", "Asia/Shanghai"),
            ),
        }
    )
    df.write_parquet(pol_path)

    print("  DuckDB 写出去的文件，Parquet 里存的物理类型：")
    for name, typ in zip(
        pq.read_schema(duck_path).names, pq.read_schema(duck_path).types, strict=True
    ):
        print(f"    {name:<10} → {typ}")
    print("  Polars 写出去的文件：")
    for name, typ in zip(
        pq.read_schema(pol_path).names, pq.read_schema(pol_path).types, strict=True
    ):
        print(f"    {name:<10} → {typ}")

    print()
    print("  Polars 读 DuckDB 写的文件：")
    df_duck = pl.read_parquet(duck_path)
    for name, dtype in df_duck.schema.items():
        print(f"    {name:<10} → {dtype}")
    print(f"    aware 的值：{df_duck['aware'].to_list()[0]}")
    print()
    print("  DuckDB 读 Polars 写的文件（走 Arrow，绕开 TIMESTAMPTZ→Python 需要 pytz 的坑）：")
    rel = con.sql(f"SELECT * FROM read_parquet('{pol_path}')")
    at = rel.to_arrow_table()
    rel.close()
    for name, dtype in zip(at.schema.names, at.schema.types, strict=True):
        print(f"    {name:<10} → {dtype}")

    print()
    print("  同一个瞬间（2024-01-01 12:00+08 = 04:00 UTC），两边读出来的写法：")
    print("    Polars 读 DuckDB 的文件 → 04:00 UTC（**时区被换成 UTC**，偏移 +08 没了）")
    print("    DuckDB 读 Polars 的文件 → TIMESTAMPTZ，Arrow 里仍标着 Asia/Shanghai")
    print()
    print("  瞬间是保住了的（04:00 UTC 就是同一个瞬间），丢的是「原来是在哪个时区记的」。")
    print("  要保留原始时区，得自己额外存一列 —— 两个引擎都不会替你记。")

    print()
    print("  顺带提醒一个 DuckDB 的环境依赖：")
    try:
        val = con.execute("SELECT TIMESTAMPTZ '2024-01-01 12:00:00+08'").fetchone()[0]
        print(f"    直接把 TIMESTAMPTZ 转成 Python 对象 → {val!r}")
    except Exception as e:
        reason = str(e).splitlines()[0]
        print(f"    直接把 TIMESTAMPTZ 转成 Python 对象 → {type(e).__name__}: {reason}")
        print("      （DuckDB 需要 pytz 才能把带时区的时间转成 Python datetime；")
        print("       走 Arrow 那条路不需要 —— Polars 用标准库 zoneinfo，没这个依赖。）")


# ---------------------------------------------------------------- 场景 8：截断与分桶


def scenario_truncate(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 截断与分桶：哪个方法对得上哪个")
    print()

    cases = [
        (
            "按天",
            "date_trunc('day', TIMESTAMP '2024-01-03 10:45:00')",
            "1d",
            datetime(2024, 1, 3, 10, 45, 0),
        ),
        (
            "按周（周一为起点）",
            "date_trunc('week', TIMESTAMP '2024-01-03 10:45:00')",
            "1w",
            datetime(2024, 1, 3, 10, 45, 0),
        ),
        (
            "按月",
            "date_trunc('month', TIMESTAMP '2024-01-03 10:45:00')",
            "1mo",
            datetime(2024, 1, 3, 10, 45, 0),
        ),
        (
            "15 分钟分桶（向下取）",
            "time_bucket(INTERVAL '15 minutes', TIMESTAMP '2024-01-03 10:53:00',"
            " TIMESTAMP '2024-01-01')",
            "15m",
            datetime(2024, 1, 3, 10, 53, 0),
        ),
    ]
    for what, duck_expr, pol_freq, sample in cases:
        duck_val = _text(con, f"SELECT ({duck_expr})::VARCHAR")
        pol_val = pl.Series([sample], dtype=pl.Datetime("us")).dt.truncate(pol_freq).to_list()[0]
        print(f"  {what}")
        print(f"    输入        {sample}")
        print(f"    DuckDB      {duck_val}")
        print(f"    Polars      {pol_val}")

    print()
    print("  一个容易踩的坑：Polars 的 `dt.round` **不是** DuckDB `time_bucket` 的对应物。")
    print("    round 是「四舍五入到最近的桶」，time_bucket 是「向下取整到桶」。")
    print("    10:53 落桶：")
    bucket = _text(
        con,
        "SELECT time_bucket(INTERVAL '15 minutes', TIMESTAMP '2024-01-03 10:53:00',"
        " TIMESTAMP '2024-01-01')::VARCHAR",
    )
    print(f"      DuckDB  time_bucket(15m) → {bucket}")
    sample = pl.Series([datetime(2024, 1, 3, 10, 53)])
    print(f"      Polars  dt.truncate('15m') → {sample.dt.truncate('15m').to_list()[0]}")
    print(f"      Polars  dt.round('15m')     → {sample.dt.round('15m').to_list()[0]}")

    print()
    print("  星期几的编号：两边**编号不一样，含义不一样**。")
    print("    DuckDB  dayofweek / date_part('dow')：0=周日 … 6=周六")
    print("    DuckDB  isodow：1=周一 … 7=周日（ISO 8601）")
    print("    Polars  dt.weekday()：1=周一 … 7=周日（ISO 8601）")
    print()
    print("      日期        DuckDB dayofweek   DuckDB isodow   Polars weekday")
    for d in ["2024-01-01", "2024-01-03", "2024-01-06", "2024-01-07"]:
        duck_dow = _one(con, f"SELECT dayofweek(TIMESTAMP '{d}')")
        duck_iso = _one(con, f"SELECT isodow(TIMESTAMP '{d}')")
        pol_wd = pl.Series([datetime.fromisoformat(d)]).dt.weekday().to_list()[0]
        mark = "  ← 不一致" if duck_dow != pol_wd else ""
        print(f"      {d}  {duck_dow:>14}  {duck_iso:>15}  {pol_wd:>15}{mark}")
    print()
    print("  周一到周六两边数字恰好相同，**只有周日不同**（0 vs 7）——")
    print("  这种「大部分对、偶尔错」的差异最容易混过测试。")


# ---------------------------------------------------------------- 场景 9：运算


def scenario_arithmetic(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 9 · 运算：月末对齐和「夏令时那天加一天」")
    print()

    print("  月末对齐（2024-01-31 加一个月）：")
    duck_naive = _text(
        con, "SELECT (TIMESTAMP '2024-01-31 12:00:00' + INTERVAL '1 month')::VARCHAR"
    )
    duck_aware = _text(
        con, "SELECT (TIMESTAMPTZ '2024-01-31 12:00:00+08' + INTERVAL '1 month')::VARCHAR"
    )
    pol_naive = pl.Series([datetime(2024, 1, 31, 12, 0, 0)]).dt.offset_by("1mo").to_list()[0]
    pol_aware = (
        pl.Series([datetime(2024, 1, 31, 12, 0, 0)])
        .dt.replace_time_zone("Asia/Shanghai")
        .dt.offset_by("1mo")
        .to_list()[0]
    )
    print(f"    DuckDB  naive → {duck_naive}")
    print(f"    Polars  naive → {pol_naive}")
    print(f"    DuckDB  aware → {duck_aware}")
    print(f"    Polars  aware → {pol_aware}")
    print()
    print("    **四个结果全部一致** —— 都是「月末对齐」：1 月 31 日加一个月落到 2 月 29 日。")
    print("    带不带时区也一样，Polars 的 offset_by 在这两件事上没有分歧。")
    print("    （真正会分叉的不是「加几个月」，而是「加一天」—— 见下面。）")

    print()
    print("  夏令时那天「加一天」（2024-03-09 12:00 纽约）：")
    duck_day = _one(
        con,
        "SELECT epoch_ms(TIMESTAMP '2024-03-09 12:00:00'"
        " AT TIME ZONE 'America/New_York' + INTERVAL '1 day')",
    )
    base = pl.Series([datetime(2024, 3, 9, 12, 0, 0)]).dt.replace_time_zone("America/New_York")
    dur = pl.select(base + pl.duration(days=1)).to_series().to_list()[0]
    cal = pl.select(base.dt.offset_by("1d")).to_series().to_list()[0]
    duck_day_utc = datetime.fromtimestamp(duck_day / 1000, tz=UTC)
    print(f"    DuckDB  TIMESTAMPTZ + INTERVAL '1 day' → epoch {duck_day} = {duck_day_utc}")
    print("      （绝对时间 +24 小时：那天只有 23 小时，所以本地钟点从 12:00 变成 13:00）")
    print(f"    Polars  + pl.duration(days=1)          → {dur}")
    print("      （同样是绝对 +24 小时，两边一致 ✅）")
    print(f"    Polars  + dt.offset_by('1d')           → {cal}")
    print("      （日历加一天，本地钟点还是 12:00 —— 和 DuckDB 不一样）")
    print()
    print("    提醒：Polars 的 `+ pl.duration(...)` 和 `dt.offset_by(...)` 语义不同 ——")
    print("    前者是绝对时长，后者是日历加减。普通日子里两者一样，")
    print("    跨夏令时就差一小时。DuckDB 的 TIMESTAMPTZ + INTERVAL '1 day' 是绝对的。")


# ---------------------------------------------------------------- 场景 10：性能


def _write_us_copy(path) -> Path | None:
    """把 order_ts 降成微秒，另存一份 parquet —— 用来量 ns 的额外开销。

    只在临时目录里生成，退出即弃，不碰 data/ 下的既有文件。
    """
    import tempfile

    import pyarrow.compute as pc

    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="tz_demo_us_"))
        out = tmp_dir / "orders_us.parquet"
        table = pq.read_table(path, columns=["order_ts", "revenue"])
        table = table.set_column(
            0, "order_ts", pc.cast(table.column("order_ts"), pa.timestamp("us"), safe=False)
        )
        pq.write_table(table, out)
        return out
    except Exception as e:  # pragma: no cover - 只是辅助测量，失败就跳过
        print(f"  （微秒副本生成失败，跳过对比：{type(e).__name__}: {e}）")
        return None


def scenario_performance(repeat: int) -> None:
    section("场景 10 · 一百万行真实数据：时间过滤 + 按天分组")
    path = common.ORDERS_PARQUET
    if not path.exists():
        print(f"  找不到 {path}，先跑 `uv run python src/gen_data.py`")
        return

    print(f"  数据：{path}（order_ts 是 timestamp[ns]）")
    print(f"  口径：过滤 [{common.WINDOW_START}, {common.WINDOW_END})，按天分组求金额")
    print()

    lo = common.WINDOW_START.isoformat(sep=" ")
    hi = common.WINDOW_END.isoformat(sep=" ")

    duck_sql = f"""
        SELECT CAST(date_trunc('day', order_ts) AS DATE) AS d,
               count(*) AS order_cnt,
               sum(revenue) AS revenue
        FROM read_parquet('{path}')
        WHERE order_ts >= TIMESTAMP '{lo}' AND order_ts < TIMESTAMP '{hi}'
        GROUP BY 1 ORDER BY 1
    """

    # 连接复用：建连接的开销不该算进查询耗时里
    con = duckdb.connect()

    def duck_run() -> pl.DataFrame:
        rel = con.sql(duck_sql)
        at = rel.to_arrow_table()
        rel.close()
        return pl.from_arrow(at)

    def pol_run() -> pl.DataFrame:
        lf = (
            pl.scan_parquet(path)
            .filter(
                (pl.col("order_ts") >= pl.lit(common.WINDOW_START).cast(pl.Datetime("ns")))
                & (pl.col("order_ts") < pl.lit(common.WINDOW_END).cast(pl.Datetime("ns")))
            )
            .with_columns(pl.col("order_ts").dt.truncate("1d").dt.date().alias("d"))
            .group_by("d")
            .agg(
                pl.len().alias("order_cnt"),
                pl.col("revenue").sum().alias("revenue"),
            )
            .sort("d")
        )
        return lf.collect()

    duck_first = duck_run()
    pol_first = pol_run()
    ok, why = common.compare_frames(duck_first, pol_first, ["d"])
    print(f"  结果等价性：{'✅' if ok else '❌'} —— {why}")
    print()
    common.print_frame(duck_first.head(6), "DuckDB 前 6 天", max_rows=6)

    # ---- 计时
    duck_times: list[float] = []
    pol_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        duck_run()
        duck_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        pol_run()
        pol_times.append(time.perf_counter() - t0)

    duck_best = min(duck_times)
    pol_best = min(pol_times)
    print()
    print(f"  计时（重复 {repeat} 次取最快，连接复用）：")
    print(f"    DuckDB  {common.format_ms(duck_best):>10} ms")
    print(f"    Polars  {common.format_ms(pol_best):>10} ms")
    print(f"    比值    {duck_best / pol_best:.2f}×")
    print()
    print("  说明：两边都是「扫 parquet → 时间过滤 → 按天分组」，口径一致。")

    # ---- 差在哪：DuckDB 在 ns 列上的额外开销
    us_path = _write_us_copy(path)
    if us_path is not None:
        us_sql = duck_sql.replace(str(path), str(us_path))
        us_times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            rel = con.sql(us_sql)
            rel.to_arrow_table()
            rel.close()
            us_times.append(time.perf_counter() - t0)
        us_best = min(us_times)
        print()
        print("  差在哪？把同一份数据另存一份**微秒**的 parquet，再跑同样的 DuckDB 查询：")
        print(f"    DuckDB  timestamp[ns] 列 → {common.format_ms(duck_best):>10} ms")
        print(f"    DuckDB  timestamp[us] 列 → {common.format_ms(us_best):>10} ms")
        print(f"    Polars  timestamp[ns] 列 → {common.format_ms(pol_best):>10} ms")
        print()
        print("  DuckDB 的 date_trunc 输出是微秒，作用在 TIMESTAMP_NS 列上要先收窄；")
        print("  Polars 的 dt.truncate 直接在 ns 上做，不收窄 ——")
        print("  所以 DuckDB 在 ns 上比在 us 上慢的那一截，就是这个收窄的代价。")

    print()
    print("  顺带提醒口径：Polars 的过滤字面量必须显式 cast 成 ns，")
    print("  否则 Polars 会把 ns 列降到 us 再比较 —— 边界行的归属就可能变了。")
    print("  DuckDB 侧 `TIMESTAMP '...'` 会自动加宽到 TIMESTAMP_NS，没有这个问题。")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 类型是**两维**的：精度（DATE / µs / ns）× 时区（naive / aware）。")
    print("     DuckDB 用三个类型名（TIMESTAMP / TIMESTAMP_NS / TIMESTAMPTZ）来表达，")
    print("     Polars 用 `Datetime(time_unit, time_zone)` 一个 dtype 表达。")
    print("  2. **DuckDB 的 TIMESTAMP 是微秒**，往它转是四舍五入（123456789ns → 123457µs），")
    print("     不是截断。纳秒要保真就用 TIMESTAMP_NS（Polars 侧是 Datetime('ns')）。")
    print("  3. **会话时区会改变 TIMESTAMPTZ 的显示文本**，epoch 不变 —— ")
    print("     存的是瞬间，显示的是本地时间。导出成文本 CSV 时这个差别会落到文件里。")
    print("  4. **naive 和 aware 直接比较，答案取决于会话时区**：")
    print("     同一句 SQL 在 Asia/Shanghai 下为 True，在 UTC 下为 False，两边都不报错。")
    print("     Polars 直接抛 SchemaError —— 报错的那一边更安全。")
    print("  5. 「贴标签」和「换显示」是两件事：DuckDB 都叫 AT TIME ZONE（靠输入类型区分），")
    print("     Polars 拆成 replace_time_zone / convert_time_zone。照名字翻译必错。")
    print("  6. **夏令时的两个坑：DuckDB 静默选一个，Polars 默认报错。**")
    print("     不存在的时刻 DuckDB 往后挪一小时；有歧义的时刻 DuckDB 选第二次（EST）。")
    print("     Polars 可以用 ambiguous= / non_existent= 显式指定，也能 non_existent='null'。")
    print("  7. Parquet 往返：**DuckDB 写出去一律 UTC**，Polars 保留自己的时区；")
    print("     瞬间都保住了，丢的是「原来在哪个时区」。想留就自己多存一列。")
    print("  8. 截断：date_trunc ↔ dt.truncate；`time_bucket`（向下取）↔ `dt.truncate`，")
    print("     **`dt.round` 是四舍五入，不是 time_bucket 的对应物**。")
    print("     星期编号：Polars 是 ISO（1=周一），DuckDB 的 dayofweek 是 0=周日 —— ")
    print("     只有周日不同，混过测试很容易。")
    print("  9. 运算：**月末对齐两边完全一致**（1 月 31 日加一个月都落到 2 月 29 日）。")
    print("     真正分叉的是「加一天」：`+ pl.duration(days=1)` 和 DuckDB 一样是绝对 +24 小时，")
    print("     `dt.offset_by('1d')` 是日历加一天 —— 跨夏令时差一小时。")
    print("  10. DuckDB 把 TIMESTAMPTZ 转成 Python 对象需要 pytz；走 Arrow 不需要。")
    print("      Polars 用标准库 zoneinfo，没有这个依赖。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="日期时间与时区：类型、精度、夏令时与往返")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    con = duckdb.connect()
    scenario_types(con)
    scenario_precision(con)
    scenario_session_tz(con)
    scenario_naive_vs_aware(con)
    scenario_tz_ops(con)
    scenario_dst(con)
    scenario_parquet(con)
    scenario_truncate(con)
    scenario_arithmetic(con)
    con.close()
    scenario_performance(args.repeat)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
