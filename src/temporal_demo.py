"""时间分桶与滚动窗口：同一句「每分钟一桶」，两边怎么才能对得上。

DuckDB 的 `time_bucket` 和 Polars 的 `group_by_dynamic` 都能把时间戳分桶，
默认参数下结果**完全一样** —— 但 Polars 那一边多了一堆旋钮：

    closed  = 'left' / 'right' / 'both' / 'none'   ← 闭哪一边
    label   = 'left' / 'right' / 'datapoint'       ← 桶用哪个端点命名
    every   = 窗口之间的步长
    period  = 窗口的宽度

这四个旋钮里，**有三个会静默改数**：

    closed='both'  边界上的行被两个桶各算一遍 → 总量变大
    closed='none'  装不进完整窗口的行被丢掉   → 总量变小
    every > period 窗口之间留空档，中间的行不属于任何桶 → 总量变小
    every < period 窗口重叠，同一行被算进几个桶 → 总量变大

没有一个会报错 —— 桶还在，行还在，就是总数不对了。
DuckDB 的 `time_bucket` 只有一种语义，没有这些旋钮。

跟 timeseries_demo 的关系：那个 demo 讲的是「时间轴本身怎么生成、
怎么补齐空档」；这个 demo 讲的是「**同一批时间戳，两边怎么分桶**」。

用法：
    uv run python src/temporal_demo.py
    uv run python src/temporal_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import time

import duckdb
import polars as pl

import common

# 小数据集：故意把点打在桶边界上 —— 边界正是闭区间语义的分水岭
SMALL_EVENTS = [
    ("2024-01-01 00:00:00", 0),
    ("2024-01-01 00:00:30", 1),
    ("2024-01-01 00:01:00", 2),
    ("2024-01-01 00:01:30", 3),
    ("2024-01-01 00:02:00", 4),
    ("2024-01-01 00:07:00", 5),
]
# 合计 = 0+1+2+3+4+5 = 15 —— 后面每一节都拿它当「正确总量」的基准
SMALL_TOTAL = 15


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def load_small(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """把小数据集灌进 DuckDB，返回同内容的 Polars DataFrame（已按时间排好）。"""
    con.execute("CREATE TABLE ev(t TIMESTAMP, v INTEGER)")
    con.executemany(
        "INSERT INTO ev VALUES (?, ?)",
        [(dt.datetime.fromisoformat(ts), v) for ts, v in SMALL_EVENTS],
    )
    df = pl.DataFrame(
        {
            "t": [dt.datetime.fromisoformat(ts) for ts, _ in SMALL_EVENTS],
            "v": [v for _, v in SMALL_EVENTS],
        }
    ).sort("t")
    return df


# ---------------------------------------------------------------- 分桶


def duck_bucket(con: duckdb.DuckDBPyConnection, interval: str = "1 minute") -> list[tuple]:
    """DuckDB：time_bucket + GROUP BY。只有一种语义，没有 closed / label 这种旋钮。"""
    rows = con.sql(
        f"SELECT time_bucket(INTERVAL {interval}, t) AS b, sum(v) AS s, count(*) AS n "
        f"FROM ev GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def polars_bucket(
    df: pl.DataFrame,
    every: str = "1m",
    period: str | None = None,
    closed: str = "left",
    label: str = "left",
) -> list[tuple]:
    """Polars：group_by_dynamic。旋钮全在这一行上。"""
    out = (
        df.sort("t")
        .group_by_dynamic("t", every=every, period=period, closed=closed, label=label)
        .agg(pl.col("v").sum().alias("s"), pl.len().alias("n"))
        .sort("t")
    )
    return [(r[0], r[1], r[2]) for r in out.rows()]


def polars_bucket_members(
    df: pl.DataFrame,
    every: str = "1m",
    period: str | None = None,
    closed: str = "left",
    label: str = "left",
) -> list[tuple]:
    """同上，但把桶里**具体装了哪些 v** 也带出来 —— 判断「谁被丢掉了」靠它。"""
    out = (
        df.sort("t")
        .group_by_dynamic("t", every=every, period=period, closed=closed, label=label)
        .agg(pl.col("v").sum().alias("s"), pl.col("v").alias("members"))
        .sort("t")
    )
    return [(r[0], r[1], r[2]) for r in out.rows()]


def show_buckets(title: str, buckets: list[tuple]) -> None:
    print(f"  {title}")
    for b, s, n in buckets:
        print(f"    {b.strftime('%H:%M')}   sum={s:<4} 行数={n}")
    print(f"    合计 sum = {sum(s for _, s, _ in buckets)}")


# ---------------------------------------------------------------- 场景


def scenario_baseline(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 1 节 · 默认参数：两边一模一样")

    print("  DuckDB 的写法：")
    print("    SELECT time_bucket(INTERVAL 1 MINUTE, t) AS b, sum(v) FROM ev GROUP BY 1")
    print()
    print("  Polars 的写法：")
    print("    df.group_by_dynamic('t', every='1m', period='1m').agg(pl.col('v').sum())")
    print()
    duck = duck_bucket(con)
    pol = polars_bucket(df)
    show_buckets("DuckDB  time_bucket(INTERVAL 1 MINUTE)", duck)
    print()
    show_buckets("Polars  group_by_dynamic(every='1m', period='1m')", pol)

    same = duck == pol
    print()
    print(f"  → 逐桶对账：{'✅ 完全一致（' + str(len(duck)) + ' 个桶）' if same else '❌ 不一致'}")
    print("    这是好消息：**默认参数下两边是对得上的。**")
    print("    接下来的几节讲的是「改了旋钮之后会发生什么」。")


def scenario_closed_label(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 2 节 · closed × label：八种组合，只有一格和 DuckDB 对得上")

    duck = duck_bucket(con)
    print("  同一批数据，DuckDB 那一列是基准。Polars 换 closed / label：")
    print()
    print(
        "    "
        + common.pad("closed", 8)
        + common.pad("label", 8)
        + common.pad("桶数", 8)
        + common.pad("合计 sum", 12)
        + "和 DuckDB 一样？"
    )
    print("    " + "-" * 66)
    for closed in ("left", "right", "both", "none"):
        for label in ("left", "right"):
            try:
                got = polars_bucket(df, closed=closed, label=label)
            except Exception as exc:
                print(f"    {closed:6s}  {label:6s}  报错  {str(exc)[:36]}")
                continue
            total = sum(s for _, s, _ in got)
            if got == duck:
                mark = "✅ 和 DuckDB 完全一致"
            elif total != SMALL_TOTAL:
                mark = "❌ 总量变了（静默改数）"
            else:
                mark = "❌ 桶名不同（桶里的行一样）"
            print(
                "    "
                + common.pad(closed, 8)
                + common.pad(label, 8)
                + common.pad(str(len(got)), 8)
                + common.pad(str(total), 12)
                + mark
            )
    print()
    print("  → 默认那组（closed=left, label=left）和 DuckDB 一模一样。")
    print("    换任何一格，桶的**归属**就变了 —— 有的是总量变了（静默改数），")
    print("    有的只是桶的**名字**变了（label=right，桶内容没变）。")
    print()
    print("  → 特别注意 `label`：它只改桶的名字（用左端点还是右端点命名），")
    print("    **不改桶里装了什么**。看着像变了，其实只是换了个标签 ——")
    print("    但如果下游拿桶名去 join，这就是实打实的对不上。")


def scenario_double_count(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 3 节 · 静默改数 ①：closed='both' 把边界行算两遍")

    duck = duck_bucket(con)
    both = polars_bucket(df, closed="both", label="left")

    show_buckets("DuckDB（基准）", duck)
    print()
    show_buckets("Polars  closed='both'", both)
    print()
    d_total = sum(s for _, s, _ in duck)
    b_total = sum(s for _, s, _ in both)
    print(f"  → DuckDB 合计 {d_total}，Polars 合计 {b_total} —— 差了 {b_total - d_total}。")
    print()
    print("  为什么？看 00:01:00 这一行：")
    print("    · closed='left'  时它只属于 [00:01, 00:02) 这一个桶；")
    print("    · closed='both' 时它同时属于 [00:00, 00:01] 和 [00:01, 00:02] ——")
    print("      **两个桶各算它一次**。")
    print()
    print("  → 而且多出来一个桶：closed='left' 没有 00:06 这一桶，")
    print("    closed='both' 有（因为 00:07:00 那行同时是 00:06 桶的上界）。")
    print("    → **行还是那些行，桶多了一个，总数变大了，没有任何报错。**")


def scenario_drop_rows(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 4 节 · 静默改数 ②：closed='none' 把装不下窗口的行丢掉")

    duck = duck_bucket(con)
    none = polars_bucket(df, closed="none", label="left")

    show_buckets("DuckDB（基准）", duck)
    print()
    show_buckets("Polars  closed='none'", none)
    print()
    d_total = sum(s for _, s, _ in duck)
    n_total = sum(s for _, s, _ in none)
    print(f"  → DuckDB 合计 {d_total}，Polars 合计 {n_total} —— 少了 {d_total - n_total}。")
    print()
    members = polars_bucket_members(df, closed="none")
    kept: list[int] = []
    for _, _, vals in members:
        kept.extend(vals)
    print("  丢的是哪些行？")
    for ts, v in SMALL_EVENTS:
        mark = "进了桶" if v in kept else "❌ 谁的桶都没进"
        print(f"    {ts}  v={v}  {mark}")
    print()
    dropped = [v for v in range(len(SMALL_EVENTS)) if v not in kept]
    print("  → closed='none' 的意思是「两个端点都不算」，于是**落在桶边界上的那几行**")
    print("    00:00:00、00:01:00、00:02:00、00:07:00 全都不属于任何窗口，被静默丢掉")
    print(f"    （丢掉 {len(dropped)} 行，v = {dropped}）。")
    print("    → 这是最危险的一种错法：**不报错，而且剩下的那几个桶看着都正常** ——")
    print(f"      DuckDB {len(duck)} 个桶 → Polars 只剩 {len(none)} 个")
    print("      （00:02 和 00:07 直接没了），")
    print(f"      {len(SMALL_EVENTS)} 行只剩 {len(kept)} 行。")
    print("      **光看剩下的桶根本看不出来少了行**，只能对总量。")
    print("    → DuckDB 没有这个旋钮，也就没有这个坑。")


def scenario_every_period(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 5 节 · every / period：步长 ≠ 宽度，要么丢行要么重复计")

    print("  Polars 的窗口是这么排的（官方 docstring 的原话）：")
    print("    [start, start + period)")
    print("    [start + every,  start + every  + period)")
    print("    [start + 2*every, start + 2*every + period)")
    print()
    print("  也就是：**every 是步长，period 是宽度**（period 默认等于 every）。")
    print("  两个相等时就是普通分桶；一旦不等，就分两种情况：")
    print()

    dense = pl.DataFrame(
        {
            "t": [dt.datetime(2024, 1, 1) + dt.timedelta(seconds=30 * i) for i in range(40)],
            "v": list(range(40)),
        }
    )
    true_total = sum(range(40))
    print(f"  数据：40 条，每 30 秒一条，跨 20 分钟，真实总量 = {true_total}")
    print()
    print(
        "    "
        + common.pad("every", 8)
        + common.pad("period", 10)
        + common.pad("窗口数", 10)
        + common.pad("合计", 10)
        + "关系"
    )
    print("    " + "-" * 64)
    for every, period in (("1m", "1m"), ("2m", "1m"), ("1m", "2m"), ("5m", "1m")):
        got = polars_bucket(dense, every=every, period=period)
        total = sum(s for _, s, _ in got)
        if every == period:
            rel = "等宽不重叠 ✅"
        elif period > every:
            rel = "宽度 > 步长 → **重叠**，同一行被算进几个桶"
        else:
            rel = "步长 > 宽度 → **有空档**，中间的行谁都不属于"
        print(
            "    "
            + common.pad(every, 8)
            + common.pad(period, 10)
            + common.pad(str(len(got)), 10)
            + common.pad(str(total), 10)
            + rel
        )
    print()
    print("  → every='5m', period='1m'：每 5 分钟才开一个 1 分钟的窗口，")
    print("    中间 4 分钟的行**不属于任何桶** —— 合计从 780 掉到 124。")
    print("  → every='1m', period='2m'：每个窗口宽 2 分钟、每分钟开一个 ——")
    print("    窗口重叠，同一行被算两遍，合计翻倍。")
    print()
    print("  → DuckDB 的 `time_bucket` 只有一种语义：等宽、不重叠。")
    print("    它没有 every / period 这一对旋钮，所以也没有这两种坑。")
    print("    想要重叠窗口，DuckDB 得用窗口函数自己写（下一节）。")


def scenario_window_frame(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 6 节 · DuckDB 侧的对应物：窗口函数，但方向相反")

    print("  Polars 的重叠窗口（every='1m', period='2m'）是**向前看**：")
    print("    每个桶 = [t, t + 2 分钟)")
    print()
    print("  DuckDB 没有这个函数，最接近的是窗口函数的 RANGE frame ——")
    print("    但它是**向后看**的：")
    print()
    rows = con.sql(
        "SELECT t, v, sum(v) OVER (ORDER BY t RANGE BETWEEN INTERVAL 1 MINUTE PRECEDING "
        "AND CURRENT ROW) FROM ev ORDER BY t"
    ).fetchall()
    print("    SELECT t, sum(v) OVER (ORDER BY t")
    print("                     RANGE BETWEEN INTERVAL 1 MINUTE PRECEDING AND CURRENT ROW)")
    print()
    print("    t                  v    sum")
    for t, v, s in rows:
        print(f"    {t.strftime('%H:%M:%S')}  {v:<4} {s}")
    print()
    print("  → 两个都是「一段时间的滑动和」，但：")
    print("    · Polars 的 group_by_dynamic 是**向前**（含未来），")
    print("      DuckDB 的 RANGE frame 是**向后**（含过去）；")
    print("    · Polars 的结果里时间戳是**窗口的起点**，")
    print("      DuckDB 的结果里时间戳是**那一行自己的时间**。")
    print()
    print("  → 所以「Polars 的重叠窗口」在 DuckDB 侧没有一一对应的写法 ——")
    print("    得自己用窗口函数 + LEAD/LAG 拼。")
    print("    **能力上有，但直觉得多。**")


def scenario_rolling(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> None:
    section("第 7 节 · 滚动窗口：Polars 没有 by=，边界行也给得不一样")

    print("  Polars 2.1 的 rolling_mean 签名：")
    print("    rolling_mean(window_size, weights=None, *, min_samples=None, center=False)")
    print()
    print("  → **没有 `by=` 参数** —— 也就是说「按时间列滚动」（每分钟一个窗口，")
    print("    而不是每 N 行一个窗口）在 Polars 侧没有直接对应物，")
    print("    只能拿 `group_by_dynamic` + `rolling` 自己拼。")
    print()

    vals = [1, 2, 3, 4, 5]
    con.execute("CREATE TABLE roll(v INTEGER)")
    con.executemany("INSERT INTO roll VALUES (?)", [(v,) for v in vals])
    duck_rows = con.sql(
        "SELECT v, avg(v) OVER (ORDER BY v ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) "
        "FROM roll ORDER BY v"
    ).fetchall()
    pol_rows = (
        pl.DataFrame({"v": vals})
        .select(pl.col("v"), pl.col("v").rolling_mean(2).alias("rm"))
        .rows()
    )
    print("  同一条查询（每行看自己 + 前一行）：")
    print()
    print("    v    DuckDB avg()      Polars rolling_mean(2)")
    for (v, d), (_, p) in zip(duck_rows, pol_rows, strict=True):
        mark = "" if (p is None and d == p) or (p is not None and abs(p - d) < 1e-9) else "  ❌"
        print(f"    {v}    {str(d):<17} {str(p)}{mark}")
    print()
    print("  → **前几行不一样**：DuckDB 的窗口函数把「不满一窗」的部分也算进去")
    print("    （第一行只有它自己，avg = 1.0）；")
    print("    Polars 默认 `min_samples=window_size`，不满一窗直接给 NULL。")
    print()
    print("  → 补一句：Polars 想跟 DuckDB 对齐，得显式写 `min_samples=1`。")
    print("    写成 `rolling_mean(2)` 的人，拿到的是「开头几行是 NULL」的版本 ——")
    print("    而 NULL 往下游一传，`avg()` 和 `sum()` 的结果就全变了。")


def scenario_ewm(con: duckdb.DuckDBPyConnection) -> None:
    section("第 8 节 · 指数加权移动平均：Polars 有，DuckDB 根本没这个函数")

    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    con.execute("CREATE TABLE ew(v DOUBLE)")
    con.executemany("INSERT INTO ew VALUES (?)", [(v,) for v in vals])
    pol = (
        pl.DataFrame({"v": vals})
        .select(pl.col("v").ewm_mean(alpha=0.5).alias("e"))
        .to_series()
        .to_list()
    )
    print("  Polars：")
    print("    pl.col('v').ewm_mean(alpha=0.5)")
    print()
    print("    v    ewm_mean(alpha=0.5)")
    for v, e in zip(vals, pol, strict=True):
        print(f"    {v:<5} {e:.6f}")
    print()
    try:
        con.sql("SELECT ewm_mean(v) FROM ew").fetchall()
        print("  DuckDB：有 ewm_mean ✅")
    except Exception as exc:
        print(f"  DuckDB：❌ {str(exc).splitlines()[0]}")
    print()
    print("  → DuckDB 2.0 **压根没有指数加权移动平均**这个函数")
    print("    （`ewm_mean` / `ewm_std` 都没有）。")
    print("    要算只能自己写递归 CTE，或者退化成普通移动平均。")
    print()
    print("  → 这是个**一边倒**的能力差：Polars 有、DuckDB 没有。")
    print("    和前面几节「两边都有但语义不同」是两种完全不同的问题 ——")
    print("    后面这种是「移植不过去」，前面那种是「移植过去会算错」。")


def scenario_month_edge(con: duckdb.DuckDBPyConnection) -> None:
    section("第 9 节 · 月末边界：这一条两边居然是一致的")

    dates = ["2024-01-15", "2024-01-31", "2024-02-29", "2024-03-31"]
    con.execute("CREATE TABLE m(d DATE, v INTEGER)")
    con.executemany(
        "INSERT INTO m VALUES (?, ?)",
        [(dt.date.fromisoformat(d), i + 1) for i, d in enumerate(dates)],
    )
    duck = con.sql(
        "SELECT time_bucket(INTERVAL 1 MONTH, d) AS b, sum(v) FROM m GROUP BY 1 ORDER BY 1"
    ).fetchall()
    mdf = pl.DataFrame(
        {
            "d": [dt.date.fromisoformat(x) for x in dates],
            "v": list(range(1, len(dates) + 1)),
        }
    ).sort("d")
    pol = (
        mdf.group_by_dynamic("d", every="1mo", period="1mo", closed="left", label="left")
        .agg(pl.col("v").sum().alias("s"))
        .sort("d")
        .rows()
    )
    print("  日期：2024-01-15、2024-01-31、2024-02-29、2024-03-31")
    print()
    print("    DuckDB time_bucket(INTERVAL 1 MONTH)")
    for b, s in duck:
        print(f"      {b}   sum={s}")
    print()
    print("    Polars group_by_dynamic(every='1mo')")
    for b, s in pol:
        print(f"      {b}   sum={s}")
    print()
    same = [(b, s) for b, s in duck] == [(b, s) for b, s in pol]
    print(f"  → {'✅ 完全一致' if same else '❌ 不一致'}。")
    print("    「1 月 31 日归到 1 月、2 月 29 日归到 2 月」—— 两边都这么干。")
    print("    闰年和月末这一类边界，日历型分桶反而是安全的。")
    print("    （真正会出事的是前面那几节：**闭区间**和**步长/宽度**。）")


def scenario_timing(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("第 10 节 · 代价：分桶各要多久")

    # 两个规模：小的是大的十分之一。
    # 只测一个规模容易把「刚好是这样」当成结论 —— 分桶这活儿两边的优劣会随规模换位，
    # 所以两个都测，结论也让数字说话（下面按实测结果选措辞，不写死）。
    small = max(rows // 10, 1)
    for name, n in (("ev_small", small), ("ev_big", rows)):
        con.execute(
            f"CREATE OR REPLACE TABLE {name} AS SELECT TIMESTAMP '2024-01-01' "
            f"+ INTERVAL (i % 2000000) SECOND AS t, i AS v FROM range({n}) t(i)"
        )

    def duck_bucket(table: str) -> int:
        # 两边做**同一件事**：切桶 + 求和 + 把结果物化出来。
        # （早先这里只数了桶数没求和，和 Polars 侧不是一件活儿 —— 那样比出来的是假的。）
        got = con.sql(
            f"SELECT time_bucket(INTERVAL 1 MINUTE, t) AS b, sum(v) FROM {table} GROUP BY 1"
        ).fetchall()
        return len(got)

    def pol_bucket(df: pl.DataFrame) -> int:
        return (
            df.sort("t")
            .group_by_dynamic("t", every="1m", period="1m")
            .agg(pl.col("v").sum())
            .height
        )

    print("  两边跑的是同一批数据（时间戳均匀铺在 2,000,000 秒 ≈ 23 天里），")
    print("  做的是同一件事：**切桶 + 求和 + 把结果物化**。")
    print()

    results: dict[str, dict[str, float]] = {}
    dfs: dict[str, pl.DataFrame] = {}

    for label, table in (("小规模", "ev_small"), ("大规模", "ev_big")):
        n = small if table == "ev_small" else rows
        # 同一批数据喂给 Polars —— 用 Arrow 过一道，不经过 pandas
        df = pl.from_arrow(con.sql(f"SELECT t, v FROM {table}").arrow())
        dfs[label] = df

        duck_bucket(table)  # 预热
        pol_bucket(df)

        duck_times: list[float] = []
        pol_times: list[float] = []
        d_buckets = p_buckets = 0
        for _ in range(repeat):
            t0 = time.perf_counter()
            d_buckets = duck_bucket(table)
            duck_times.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            p_buckets = pol_bucket(df)
            pol_times.append((time.perf_counter() - t0) * 1000)

        duck_med = statistics.median(duck_times)
        pol_med = statistics.median(pol_times)
        results[label] = {
            "rows": n,
            "duck": duck_med,
            "pol": pol_med,
            "duck_min": min(duck_times),
            "pol_min": min(pol_times),
        }

        print(f"  {label}（{n:,} 行）")
        print()
        print("    实现                     桶数        中位数 (ms)   最快 (ms)")
        print("    " + "-" * 70)
        print(
            f"    DuckDB time_bucket       {d_buckets:<11} {duck_med:<13.1f} {min(duck_times):.1f}"
        )
        print(f"    Polars group_by_dynamic  {p_buckets:<11} {pol_med:<13.1f} {min(pol_times):.1f}")
        print()
        if d_buckets == p_buckets:
            print(
                f"    → 桶数一致（{d_buckets} vs {p_buckets}）—— "
                "分桶结果本身没有差异，同样的时间戳切出同样的桶。"
            )
        else:
            print(f"    → 桶数不一致（{d_buckets} vs {p_buckets}）—— 分桶结果本身就有差异。")
        if duck_med < pol_med:
            print(
                f"    → **这一档 DuckDB 更快**：{duck_med:.1f} ms vs {pol_med:.1f} ms"
                f"（DuckDB 是 Polars 的 {duck_med / pol_med:.2f}×）"
            )
        else:
            print(
                f"    → **这一档 Polars 更快**：{pol_med:.1f} ms vs {duck_med:.1f} ms"
                f"（Polars 是 DuckDB 的 {pol_med / duck_med:.2f}×）"
            )
        print()

    def median_ms(fn) -> float:
        fn()  # 预热
        out = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t0) * 1000)
        return statistics.median(out)

    df_big = dfs["大规模"]

    def duck_count_all() -> None:
        con.sql("SELECT count(*) FROM ev_big").fetchall()

    def duck_bucket_only() -> None:
        con.sql("SELECT count(time_bucket(INTERVAL 1 MINUTE, t)) FROM ev_big").fetchall()

    def duck_bucket_group() -> None:
        con.sql(
            "SELECT time_bucket(INTERVAL 1 MINUTE, t) AS b, sum(v) FROM ev_big GROUP BY 1"
        ).fetchall()

    breakdown = [
        ("DuckDB  count(*)（全表，不分桶）", median_ms(duck_count_all)),
        ("DuckDB  只算 time_bucket，不聚合", median_ms(duck_bucket_only)),
        ("DuckDB  time_bucket + GROUP BY + sum", median_ms(duck_bucket_group)),
        ("Polars  只 sort，不分桶", median_ms(lambda: df_big.sort("t").height)),
        ("Polars  group_by_dynamic + agg", median_ms(lambda: pol_bucket(df_big))),
    ]

    print("  但快慢这件事得拆开看 —— 把两边各自拆成几步（大规模）：")
    print()
    print("    " + common.pad("步骤", 44) + "中位数 (ms)")
    print("    " + "-" * 58)
    for name, ms in breakdown:
        print("    " + common.pad(name, 44) + f"{ms:.2f}")
    print()

    duck_only = breakdown[1][1]
    duck_group = breakdown[2][1]
    pol_sort = breakdown[3][1]
    pol_full = breakdown[4][1]
    print("  → DuckDB 那一侧：切桶这一步几乎不花时间")
    print(f"    （count(*) {breakdown[0][1]:.2f} ms → 只算 time_bucket {duck_only:.2f} ms），")
    print(f"    大头在后面的哈希聚合：time_bucket + GROUP BY + sum {duck_group:.2f} ms，")
    print(f"    是「只算 time_bucket」的 {duck_group / duck_only:.1f}×。")
    print("  → Polars 那一侧：group_by_dynamic 要求输入**已排序**，")
    print(f"    光 sort 就 {pol_sort:.2f} ms —— 占它整个 group_by_dynamic + agg")
    print(f"    （{pol_full:.2f} ms）的 {pol_sort / pol_full * 100:.0f}%。")
    print()

    small_duck_wins = results["小规模"]["duck"] < results["小规模"]["pol"]
    big_duck_wins = results["大规模"]["duck"] < results["大规模"]["pol"]
    if small_duck_wins != big_duck_wins:
        win_small = "DuckDB" if small_duck_wins else "Polars"
        win_big = "DuckDB" if big_duck_wins else "Polars"
        print(
            f"  → **规模变大，两边的优劣会换位**：{win_small} 在 "
            f"{results['小规模']['rows']:,} 行上快，"
            f"{win_big} 在 {results['大规模']['rows']:,} 行上快。"
        )
        print("    所以「分桶谁快」没有单一答案 —— 得说清楚是多大数据量。")
    else:
        win = "DuckDB" if small_duck_wins else "Polars"
        print(f"  → 两个规模上都是 **{win} 更快**。")
    print()

    print("  → 顺带一提：**分桶的语义对不对，比分桶快不快重要得多**。")
    print("    前面那几节的 closed / every / period 改一个参数，总量就变了，")
    print("    而这一节里两个实现都是对的 —— 快的那个不一定对，")
    print("    对的那个也不一定快。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB 与 Polars 的时间分桶与滚动窗口：闭区间、步长/宽度、边界"
    )
    parser.add_argument("--rows", type=int, default=1_000_000, help="计时场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="计时场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    con = duckdb.connect()
    try:
        df = load_small(con)
        scenario_baseline(con, df)
        scenario_closed_label(con, df)
        scenario_double_count(con, df)
        scenario_drop_rows(con, df)
        scenario_every_period(con, df)
        scenario_window_frame(con, df)
        scenario_rolling(con, df)
        scenario_ewm(con)
        scenario_month_edge(con)
        scenario_timing(con, args.rows, args.repeat)
    finally:
        con.close()

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  1. **默认参数下两边完全一致** —— `time_bucket(INTERVAL 1 MINUTE, t)`")
    print("     和 `group_by_dynamic(every='1m', period='1m')` 逐桶对得上。")
    print()
    print("  2. **closed='both' 会把边界行算两遍**（总量 15 → 26），")
    print("     **closed='none' 会把装不下窗口的行丢掉**（总量 15 → 4，")
    print("     桶也从 4 个变成 2 个）。两个都不报错 —— 看表格看不出来少了行。")
    print()
    print("  3. **every 是步长、period 是宽度**：步长 > 宽度会留空档（丢行），")
    print("     宽度 > 步长会重叠（重复计）。DuckDB 没有这两个旋钮。")
    print()
    print("  4. **label 只改桶的名字**，不改桶里装了什么 —— 但下游拿桶名去 join")
    print("     的话，这就是实打实的对不上。")
    print()
    print("  5. **滚动窗口**：Polars 2.1 的 `rolling_mean` 没有 `by=`（按时间列滚动")
    print("     没有直接对应物）；而且默认 `min_samples=window_size`，")
    print("     开头几行给 NULL，DuckDB 的窗口函数给的是部分窗口的平均值。")
    print()
    print("  6. **DuckDB 没有 `ewm_mean`** —— 指数加权移动平均是 Polars 独有的能力，")
    print("     DuckDB 侧得自己写递归 CTE。")
    print()
    print("  7. **月末 / 闰年这类日历边界两边倒是一致的** —— 真正会出事的是")
    print("     闭区间和步长/宽度这两组旋钮。")
    print()
    print("  → 一句话：**Polars 的分桶器更灵活，灵活的两个旋钮是坑。**")
    print("    跨引擎要一致，就把参数写死（closed='left', label='left', every=period），")
    print("    别依赖默认值 —— 默认值可能会变，钉死的参数不会。")
    print()


if __name__ == "__main__":
    main()
