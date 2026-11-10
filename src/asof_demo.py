"""ASOF JOIN：时间序列的「就近匹配」连接。

典型场景：每一笔成交（trade）要拿到**成交那一刻之前最近的一条**报价（quote）。
普通 JOIN 做不到 —— 你得先自己把每个 symbol 的报价时间轴算出来，
再用 `t.ts >= q.ts` 这种带不等式的条件去连（那是笛卡尔积级别的代价）。
ASOF JOIN 就是专门干这个的：右表按时间排序，逐条扫描、只保留「不晚于当前行」的最后一条。

两个引擎都能做，但**语义不完全一样**，而且不一样的几个点全是静默的：
  · DuckDB  `ASOF JOIN`  —— 右表不用排序，引擎自己排；并列时间戳取**第一条**
  · Polars  `join_asof`   —— 必须自己先排好；带 `by` 分组时它**不检查顺序**（只警告），
                              顺序错了照样返回结果，只是结果是错的；
                              并列时间戳取**最后一条**

用法：
    uv run python src/asof_demo.py
    uv run python src/asof_demo.py --trades 400000 --quotes 200000
"""

from __future__ import annotations

import argparse
import datetime
import time
import warnings

import duckdb
import numpy as np
import polars as pl

import common

TRADES_PARQUET = common.DATA_DIR / "asof_trades.parquet"
QUOTES_PARQUET = common.DATA_DIR / "asof_quotes.parquet"

SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD"]
BASE_TS = datetime.datetime(2024, 1, 1)


def section(title: str) -> None:
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")


# ---------------------------------------------------------------- 造数


def ensure_data(n_trades: int, n_quotes: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    """造成交/报价两份数据。

    时间戳刻意错开 500 微秒：成交**永远不会**和报价同一时刻，
    这样场景 1 的等价性检查不会被「并列时间戳」干扰（那个单独在场景 2 里看）。
    """
    if TRADES_PARQUET.exists() and QUOTES_PARQUET.exists():
        trades = pl.read_parquet(TRADES_PARQUET)
        quotes = pl.read_parquet(QUOTES_PARQUET)
        if len(trades) == n_trades and len(quotes) == n_quotes:
            print(f"复用已有数据（成交 {len(trades):,} 行 / 报价 {len(quotes):,} 行）")
            return trades, quotes

    print(f"生成成交 {n_trades:,} 行 / 报价 {n_quotes:,} 行 …")
    rng = np.random.default_rng(common.SEED)
    per_symbol_trades = max(1, n_trades // len(SYMBOLS))
    per_symbol_quotes = max(1, n_quotes // len(SYMBOLS))

    base = np.datetime64(BASE_TS, "us")
    trade_frames, quote_frames = [], []
    for symbol in SYMBOLS:
        # 报价：每 1 秒一条
        q_ts = base + (np.arange(per_symbol_quotes) * np.timedelta64(1, "s")).astype(
            "timedelta64[us]"
        )
        q_px = np.round(100 + rng.normal(0, 3, per_symbol_quotes).cumsum() * 0.1, 4)
        quote_frames.append(
            pl.DataFrame(
                {
                    "symbol": [symbol] * per_symbol_quotes,
                    "ts": q_ts,
                    "px": q_px,
                }
            )
        )

        # 成交：落在报价之间（+500 微秒），并且出现「早于第一条报价」的成交（没得可匹配）
        t_off = rng.integers(0, per_symbol_quotes, per_symbol_trades)
        t_ts = base + (t_off * np.timedelta64(1, "s") + np.timedelta64(500, "us")).astype(
            "timedelta64[us]"
        )
        trade_frames.append(
            pl.DataFrame(
                {
                    "symbol": [symbol] * per_symbol_trades,
                    "ts": t_ts,
                    "qty": rng.integers(1, 100, per_symbol_trades),
                }
            )
        )

    trades = pl.concat(trade_frames)
    quotes = pl.concat(quote_frames)
    trades.write_parquet(TRADES_PARQUET, compression="zstd")
    quotes.write_parquet(QUOTES_PARQUET, compression="zstd")
    return trades, quotes


# ---------------------------------------------------------------- 小样本（讲语义用）


def _tiny_tables(*, tie: bool = False) -> tuple[pl.DataFrame, pl.DataFrame]:
    """手写的小表：3 个 symbol。

    tie=False：报价时间戳唯一 —— 两个引擎必然选到同一行，用来看「基本等价性」。
    tie=True ：AAA 在同一时刻有两条报价 —— 用来看「并列时谁说了算」。
    """
    if tie:
        quotes = pl.DataFrame(
            {
                "symbol": ["AAA", "AAA", "AAA", "BBB", "CCC"],
                "ts": [
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                    datetime.datetime(2024, 1, 1, 9, 30, 0),  # 与上一行同一时刻（并列）
                    datetime.datetime(2024, 1, 1, 9, 31, 0),
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                ],
                "px": [10.0, 11.0, 12.0, 20.0, 30.0],
            }
        )
    else:
        quotes = pl.DataFrame(
            {
                "symbol": ["AAA", "AAA", "BBB", "CCC"],
                "ts": [
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                    datetime.datetime(2024, 1, 1, 9, 31, 0),
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                    datetime.datetime(2024, 1, 1, 9, 30, 0),
                ],
                "px": [10.0, 12.0, 20.0, 30.0],
            }
        )

    trades = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA", "BBB", "BBB", "CCC"],
            "ts": [
                datetime.datetime(2024, 1, 1, 9, 29, 0),  # 早于所有报价
                datetime.datetime(2024, 1, 1, 9, 30, 0),
                datetime.datetime(2024, 1, 1, 9, 30, 30),
                datetime.datetime(2024, 1, 1, 9, 30, 0),
                datetime.datetime(2024, 1, 1, 9, 31, 0),
                datetime.datetime(2024, 1, 1, 9, 35, 0),
            ],
            "qty": [1, 2, 3, 4, 5, 6],
        }
    )
    return trades, quotes


def _duck_asof(con, trades: pl.DataFrame, quotes: pl.DataFrame, left_join: bool) -> pl.DataFrame:
    """DuckDB 侧的 ASOF JOIN。"""
    con.register("t", trades)
    con.register("q", quotes)
    kind = "ASOF LEFT JOIN" if left_join else "ASOF JOIN"
    tbl = con.sql(
        f"""
        SELECT t.symbol, t.ts, t.qty, q.px, q.ts AS q_ts
        FROM t {kind} q
          ON t.symbol = q.symbol AND t.ts >= q.ts
        ORDER BY t.symbol, t.ts, t.qty
        """
    ).to_arrow_table()
    return pl.from_arrow(tbl)


def _polars_asof(trades: pl.DataFrame, quotes: pl.DataFrame, *, sort: bool = True) -> pl.DataFrame:
    """Polars 侧的 join_asof（backward = 取不晚于当前时刻的最后一条）。"""
    t = trades.sort(["symbol", "ts"]) if sort else trades
    q = quotes.sort(["symbol", "ts"]) if sort else quotes
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return t.join_asof(q, on="ts", by="symbol", strategy="backward")


# ---------------------------------------------------------------- 场景


def scenario_equivalence(con) -> None:
    section("场景 1 · 同一个语义，两种写法 —— 结果对得上吗")

    trades, quotes = _tiny_tables()
    duck = _duck_asof(con, trades, quotes, left_join=False)
    polars = _polars_asof(trades, quotes)

    common.print_frame(duck, "DuckDB ASOF JOIN", max_rows=10)
    common.print_frame(polars.select("symbol", "ts", "qty", "px"), "Polars join_asof", max_rows=10)

    print(f"\n行数：DuckDB {duck.height} 行 / Polars {polars.height} 行")
    print("\n差异一：DuckDB 的 ASOF JOIN 默认是 INNER JOIN —— 匹配不上的行直接没了。")
    print("        AAA 09:29:00 那笔成交早于所有报价，DuckDB 侧根本不会返回它。")
    print("        Polars 的 join_asof 本身是左连接语义，匹配不上就给 null，行还在。")
    print("        要对齐：DuckDB 侧写 ASOF LEFT JOIN。")

    # 把 DuckDB 也换成 LEFT JOIN 再比
    duck_left = _duck_asof(con, trades, quotes, left_join=True)
    duck_left = duck_left.select("symbol", "ts", "qty", "px").with_columns(
        pl.col("px").cast(pl.Float64)
    )
    polars_cmp = polars.select("symbol", "ts", "qty", "px").with_columns(
        pl.col("px").cast(pl.Float64)
    )
    ok, detail = common.compare_frames(
        duck_left.sort(["symbol", "ts", "qty"]),
        polars_cmp.sort(["symbol", "ts", "qty"]),
        ["symbol", "ts", "qty"],
    )
    print(f"\n差异二：两边都用左连接之后，逐值比对：{'✅ 一致' if ok else '❌ 不一致'} {detail}")
    print("        （前提：右表同一个 symbol 内的时间戳是唯一的。有并列时就不一样了，")
    print("          见下一个场景。）")


def scenario_tie_breaking(con) -> None:
    section("场景 2 · 并列时间戳：同一时刻两条报价，谁说了算？")

    trades, quotes = _tiny_tables(tie=True)
    # 只保留那笔正好落在并列时刻上的成交
    t = trades.filter(pl.col("ts") == datetime.datetime(2024, 1, 1, 9, 30, 0)).filter(
        pl.col("symbol") == "AAA"
    )
    q = quotes.filter(pl.col("symbol") == "AAA")

    duck = _duck_asof(con, t, q, left_join=False)
    polars = _polars_asof(t, q)

    print("报价表（AAA，同一时刻两条）：")
    for row in q.iter_rows(named=True):
        print(f"    {row['ts']}  px={row['px']}")

    print("\nDuckDB ASOF JOIN 匹配到的价格 :", duck["px"].to_list())
    print("Polars join_asof  匹配到的价格 :", polars["px"].to_list())

    same = duck["px"].to_list() == polars["px"].to_list()
    print(f"\n两边一致：{'✅' if same else '❌ 不一致'}")
    if not same:
        print("\n差异：报价表里同一时刻有两条记录时，")
        print("  DuckDB 取的是**第一条**（10.0），Polars backward 取的是**最后一条**（11.0）。")
        print("  两个引擎都没报错，返回的也都是「一条合法结果」—— 这就是静默的错数据。")
        print("\n  为什么：ASOF 要在并列里挑一条，挑谁没有统一标准。")
        print("  DuckDB 按右表物理顺序取第一条；Polars 的 backward 是从命中点往前扫，")
        print("  扫到的第一条就是排序后并列组里的最后一条。")
        print("\n  对齐办法：")
        print("    · 右表加一列确定性的 tie-break 键（比如序号），")
        print("      DuckDB 侧 ORDER BY 后再 ASOF，Polars 侧用同一套排序；")
        print("    · 或者干脆在右表侧保证 (symbol, ts) 唯一 —— 报价表按时间唯一是常见约束。")


def scenario_sortedness(con) -> None:
    section("场景 3 · 输入要不要先排序？（Polars 这边是雷区）")

    trades, quotes = _tiny_tables()
    # 故意把「更晚的报价」排在前面 —— 这正是「没排序」长什么样
    shuffled = quotes.sort("ts", descending=True)

    print("报价表倒着排（最新的在最前面）：")
    for row in shuffled.iter_rows(named=True):
        print(f"    {row['symbol']}  {row['ts']}  px={row['px']}")

    print("\n  DuckDB  : ASOF JOIN 不要求右表有序，引擎自己排 —— 结果不变。")
    duck = _duck_asof(con, trades, shuffled, left_join=False)
    print(f"            匹配到 {duck.height} 行，px = {duck['px'].to_list()}")

    print("\n  Polars  : join_asof 要求两边按时间键有序，但**带 by 分组时它不检查**。")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        wrong = _polars_asof(trades, shuffled, sort=False)
    for w in caught:
        print(f"            ⚠ {w.category.__name__}: {w.message}")

    right = _polars_asof(trades, quotes)
    print(f"\n            不排序的结果 px = {wrong['px'].to_list()}")
    print(f"            排序后的结果 px = {right['px'].to_list()}")
    same = wrong["px"].to_list() == right["px"].to_list()
    print(f"            两者相同：{'✅' if same else '❌ 不相同'}")

    if not same:
        print("\n            ❌ 顺序错了，但 Polars 照样返回了结果 —— 没有报错，只有结果不对。")
        print("            这是这个 demo 里最容易踩的一个坑：")
        print("              Polars 的 join_asof 在**不带** by 时会检查顺序（不有序直接抛错），")
        print("              一旦用了 by 分组（真实场景基本都要用），检查就只剩一条警告。")
    print('\n  对齐办法：Polars 侧永远先 sort —— `df.sort(["symbol", "ts"])`，')
    print("            并且把这个 sort 的耗时算进你的性能预算里（见场景 6）。")


def scenario_tolerance(con) -> None:
    section("场景 4 · 容忍窗口 tolerance：报价太旧了还认不认？")

    # 单独一份小表：报价停在 09:31，最后一笔成交在 09:32:30 —— 报价已经 90 秒没更新
    q = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "ts": [
                datetime.datetime(2024, 1, 1, 9, 30, 0),
                datetime.datetime(2024, 1, 1, 9, 31, 0),
            ],
            "px": [10.0, 12.0],
        }
    )
    t = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA"],
            "ts": [
                datetime.datetime(2024, 1, 1, 9, 30, 0),
                datetime.datetime(2024, 1, 1, 9, 31, 0),
                datetime.datetime(2024, 1, 1, 9, 32, 30),
            ],
            "qty": [1, 2, 3],
        }
    )

    tol = datetime.timedelta(seconds=60)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        polars_tol = t.sort(["symbol", "ts"]).join_asof(
            q.sort(["symbol", "ts"]),
            on="ts",
            by="symbol",
            strategy="backward",
            tolerance=tol,
        )

    print("成交时刻：09:30:00 / 09:31:00 / 09:32:30，最后一笔距离上一条报价已经 90 秒。")
    print("容忍窗口 = 60 秒：")
    print("  Polars tolerance=60s :", polars_tol["px"].to_list())
    print("    （最后一笔的上一条报价是 09:31:00，差 90 秒 > 60 秒 -> null）")

    # DuckDB 没有 tolerance 参数，只能在 ON 里手写不等式
    con.register("t", t)
    con.register("q", q)
    duck_tol = pl.from_arrow(
        con.sql(
            """
            SELECT t.symbol, t.ts, q.px, q.ts AS q_ts
            FROM t ASOF LEFT JOIN q
              ON t.symbol = q.symbol
             AND t.ts >= q.ts
             AND t.ts - q.ts <= INTERVAL 60 SECOND
            ORDER BY t.ts
            """
        ).to_arrow_table()
    )
    duck_all = _duck_asof(con, t, q, left_join=True)
    print(f"  DuckDB 手动加条件      : {duck_tol['px'].to_list()}")
    print(f"  DuckDB 不加条件        : {duck_all['px'].to_list()}")

    print("\n差异：")
    print("  · Polars 的 tolerance 是原生参数，写在 join_asof(...) 里，超窗就是 null。")
    print("  · DuckDB 的 ASOF JOIN 没有 tolerance 参数 —— 只能在 ON 里再加一个不等式，")
    print("    引擎会把它当成「匹配条件」一起用（匹配时就要求在窗口内）。")
    print("  · 想要「保留行、超窗留空」，DuckDB 侧要用 ASOF LEFT JOIN；")
    print("    写成普通 ASOF JOIN（INNER）的话，超窗的行会直接消失，不是留空。")
    print("  · 边界是闭区间：差值正好等于 tolerance 时仍然匹配（Polars 与 DuckDB 一致）。")


def scenario_performance(trades: pl.DataFrame, quotes: pl.DataFrame, repeat: int) -> None:
    section(f"场景 6 · 性能：谁快？（数据量 {len(trades):,} 成交 × {len(quotes):,} 报价）")

    print(f"机器状态：{common.describe_machine()}")
    print("（这台是共享开发机，负载高的时候绝对值会飘，看相对关系即可）\n")

    con = duckdb.connect()
    try:
        con.execute(
            f"CREATE OR REPLACE VIEW v_trades AS SELECT * FROM read_parquet('{TRADES_PARQUET}')"
        )
        con.execute(
            f"CREATE OR REPLACE VIEW v_quotes AS SELECT * FROM read_parquet('{QUOTES_PARQUET}')"
        )
        sql = """
            SELECT t.symbol, t.ts, t.qty, q.px, q.ts AS q_ts
            FROM v_trades t ASOF JOIN v_quotes q
              ON t.symbol = q.symbol AND t.ts >= q.ts
        """
        duck_times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            # to_arrow_table 而不是 fetchall：fetchall 会把 40 万行变成 Python 元组，
            # 那几百万个对象的开销会盖过 join 本身，两边就没法比了。
            duck_rows = con.sql(sql).to_arrow_table()
            duck_times.append((time.perf_counter() - t0) * 1000)
    finally:
        con.close()

    # Polars：纯 join（输入已经排好）
    t_sorted = trades.sort(["symbol", "ts"])
    q_sorted = quotes.sort(["symbol", "ts"])
    join_times = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(repeat):
            t0 = time.perf_counter()
            polars_rows = t_sorted.join_asof(q_sorted, on="ts", by="symbol", strategy="backward")
            join_times.append((time.perf_counter() - t0) * 1000)

    # Polars：完整链路（读 parquet + 排序 + join）—— 这才是实际写法
    full_times = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(repeat):
            t0 = time.perf_counter()
            tl = pl.read_parquet(TRADES_PARQUET)
            ql = pl.read_parquet(QUOTES_PARQUET)
            tl = tl.sort(["symbol", "ts"])
            ql = ql.sort(["symbol", "ts"])
            tl.join_asof(ql, on="ts", by="symbol", strategy="backward")
        full_times.append((time.perf_counter() - t0) * 1000)

    # Polars：单独的排序耗时
    sort_times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        pl.read_parquet(TRADES_PARQUET).sort(["symbol", "ts"])
        pl.read_parquet(QUOTES_PARQUET).sort(["symbol", "ts"])
        sort_times.append((time.perf_counter() - t0) * 1000)

    rows = [
        ["DuckDB ASOF JOIN（端到端，含扫 parquet）", f"{np.median(duck_times):.1f}"],
        ["Polars join_asof（只算 join，输入已排序）", f"{np.median(join_times):.1f}"],
        ["Polars 读 parquet + 排序", f"{np.median(sort_times):.1f}"],
        ["Polars 端到端（读 + 排 + join）", f"{np.median(full_times):.1f}"],
    ]
    print(f"{common.pad('做法', 44)}{common.pad_left('耗时 (ms，中位数)', 18)}")
    print("-" * 62)
    for label, value in rows:
        print(f"{common.pad(label, 44)}{common.pad_left(value, 18)}")

    duck_med = float(np.median(duck_times))
    polars_med = float(np.median(join_times))
    full_med = float(np.median(full_times))
    print(f"\n  · 只看 join 这一步：DuckDB {duck_med:.0f} ms vs Polars {polars_med:.0f} ms")
    print("    （DuckDB 那边还要自己排右表，排序开销已经算在 DuckDB 的耗时里）")
    overhead = full_med - polars_med
    print(f"  · Polars 端到端 {full_med:.0f} ms，比只算 join 多了 {overhead:.0f} ms ——")
    print("    这就是「Polars 必须自己排序」的代价。DuckDB 一次 ASOF JOIN 就把它藏起来了。")
    print(f"  · 行数：DuckDB {len(duck_rows):,} 行 / Polars {len(polars_rows):,} 行")
    print("    （ASOF JOIN 是 INNER 语义：早于第一条报价的成交两边都不会返回）")


def scenario_no_tie_check(trades: pl.DataFrame, quotes: pl.DataFrame) -> None:
    section("场景 5 · 大数据上对账：两边结果逐值比")

    con = duckdb.connect()
    try:
        duck = (
            _duck_asof(con, trades, quotes, left_join=False)
            .select("symbol", "ts", "qty", "px")
            .with_columns(pl.col("px").cast(pl.Float64))
        )
    finally:
        con.close()

    polars = (
        _polars_asof(trades, quotes)
        .select("symbol", "ts", "qty", "px")
        .with_columns(pl.col("px").cast(pl.Float64))
    )
    ok, detail = common.compare_frames(
        duck.sort(["symbol", "ts", "qty"]),
        polars.sort(["symbol", "ts", "qty"]),
        ["symbol", "ts", "qty"],
        rtol=1e-9,
    )
    print(f"DuckDB {len(duck):,} 行 vs Polars {len(polars):,} 行")
    print(f"逐值比对：{'✅ 一致' if ok else '❌ 不一致'} {detail}")
    if ok:
        print("\n在这份数据上两边完全一致 —— 因为成交时间戳刻意和报价错开了 500 微秒，")
        print("不存在并列。场景 2 说明：一旦有并列，两边会选到不同的行。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的 ASOF JOIN 对比")
    parser.add_argument("--trades", type=int, default=400_000, help="成交行数")
    parser.add_argument("--quotes", type=int, default=200_000, help="报价行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    trades, quotes = ensure_data(args.trades, args.quotes)

    con = duckdb.connect()
    try:
        scenario_equivalence(con)
        scenario_tie_breaking(con)
        scenario_sortedness(con)
        scenario_tolerance(con)
    finally:
        con.close()

    scenario_no_tie_check(trades, quotes)
    scenario_performance(trades, quotes, args.repeat)
    print()


if __name__ == "__main__":
    main()
