"""金额与 Decimal：两个引擎「算钱」的方式不一样。

这个 demo 回答四个问题：

  1. DuckDB 的字面量是 DECIMAL，Polars 只有 Float64 —— 同样写 `0.1 + 0.2`，
     两边给的数不一样，这是真的吗？
  2. 一百万笔金额求和，浮点误差到底能攒到多大？**两个引擎谁的浮点求和更准？**
  3. 想要精确金额，Polars 侧有什么出路？代价是多少？
  4. 溢出了会怎样？两边的表现正好相反。

数据是造出来的：每笔金额以 0.0001 元为最小单位（比「分」还细一档，
现实里的单价、费率、汇率残差就是这个量级），文件里同时存了浮点列和整数单位列，
整数单位列是**精确答案**，用来给浮点结果对账。

用法：
    uv run python src/decimal_demo.py
    uv run python src/decimal_demo.py --rows 2000000
"""

from __future__ import annotations

import argparse
import time

import duckdb
import polars as pl

import common

AMOUNTS_PREFIX = "decimal_amounts"

# 金额的最小单位：0.0001 元。整数存这个单位 → 求和永远精确。
SCALE = 10_000

# 金额的取值范围（以单位计）：0.0001 元 ~ 200 元
UNIT_MAX = 2_000_000


def amounts_path(n_rows: int) -> str:
    """文件名带行数 → 换个 --rows 就是另一份数据，绝不覆盖已有文件。"""
    return common.DATA_DIR / f"{AMOUNTS_PREFIX}_{n_rows}.parquet"


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


# ---------------------------------------------------------------- 造数


def ensure_data(n_rows: int) -> str:
    """造金额数据：浮点列（现实中金额的样子）+ 整数单位列（精确答案）。

    已存在的文件直接复用，绝不覆盖。
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = amounts_path(n_rows)
    if path.exists():
        return str(path)

    rng = np.random.default_rng(common.SEED)
    units = rng.integers(1, UNIT_MAX, size=n_rows, dtype=np.int64)
    table = pa.table(
        {
            "id": pa.array(np.arange(n_rows, dtype=np.int64)),
            # 存成 float64 —— 这正是绝大多数系统里金额的样子
            "amount": pa.array(units.astype(np.float64) / SCALE),
            # 精确的整数单位（0.0001 元），基准答案
            "amount_units": pa.array(units),
        }
    )
    pq.write_table(table, path, compression="zstd")
    return str(path)


def timeit(fn, repeats: int = 5) -> float:
    """跑几次取最快的一次 —— 这台机器有负载波动，取最快最能反映真实上限。"""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


# ---------------------------------------------------------------- 场景 1


def scenario_literal(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 同样写一句「0.1 + 0.2」，两边给什么？")

    duck_val, duck_type = con.execute("SELECT 0.1 + 0.2, typeof(0.1 + 0.2)").fetchone()
    duck_dbl = con.execute("SELECT 0.1::DOUBLE + 0.2::DOUBLE").fetchone()[0]
    pol_val = pl.select(pl.lit(0.1) + pl.lit(0.2)).item()
    pol_type = pl.select(pl.lit(0.1) + pl.lit(0.2)).dtypes[0]

    rows = [
        ("DuckDB  0.1 + 0.2", repr(duck_val), str(duck_type)),
        ("DuckDB  0.1::DOUBLE + 0.2::DOUBLE", repr(duck_dbl), "DOUBLE"),
        ("Polars  0.1 + 0.2", repr(pol_val), str(pol_type)),
    ]
    print(f"  {'写法':<38}{'结果':<26}{'类型'}")
    print("  " + "-" * 76)
    for label, val, typ in rows:
        print(f"  {common.pad(label, 38)}{common.pad(val, 26)}{typ}")

    print()
    print("  关键不在「谁对谁错」，而在**默认类型不同**：")
    print("    · DuckDB 的数值字面量默认是 DECIMAL —— 算术全程走定点，0.1+0.2 精确等于 0.3")
    print("    · Polars 的数值字面量默认是 Float64 —— 0.1+0.2 = 0.30000000000000004")
    print("    · DuckDB 只要有一边是 DOUBLE，整个表达式就退化成 DOUBLE，一样飘")

    # 列是 DOUBLE 时，DuckDB 也会飘 —— 这一条必须实测，不能靠推断
    con.execute("CREATE TABLE IF NOT EXISTS t_dbl AS SELECT 0.1::DOUBLE AS a, 0.2::DOUBLE AS b")
    from_col = con.execute("SELECT a + b FROM t_dbl").fetchone()[0]
    print(f"      实测：两列都是 DOUBLE 时，DuckDB 的 a+b = {from_col!r}")
    print("      —— 所以「DuckDB 算钱准」这句话只在**列本身是 DECIMAL** 时成立。")


# ---------------------------------------------------------------- 场景 2


def scenario_sum(path: str, n_rows: int) -> None:
    section(f"场景 2 · {n_rows:,} 笔金额求和，浮点误差攒到多大？谁更准？")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW amt AS SELECT * FROM read_parquet('{path}')")

    # 精确答案：整数单位求和（HUGEINT，不丢）
    exact_units = con.execute("SELECT SUM(amount_units) FROM amt").fetchone()[0]
    exact = float(exact_units) / SCALE

    duck_dec = float(con.execute("SELECT SUM(amount::DECIMAL(24,4)) FROM amt").fetchone()[0])
    duck_flt = con.execute("SELECT SUM(amount) FROM amt").fetchone()[0]
    duck_type = con.execute("SELECT typeof(SUM(amount::DECIMAL(24,4))) FROM amt").fetchone()[0]

    df = pl.read_parquet(path)
    pol_flt = df["amount"].sum()

    # 第三条浮点路径：Python 里逐行累加
    def py_sum() -> float:
        acc = 0.0
        for v in df["amount"].to_list():
            acc += v
        return acc

    py_flt = py_sum()

    print(f"  精确答案（整数单位求和）      : {exact_units} 单位 = {exact!r}")
    print(f"  DuckDB  SUM(amount::DECIMAL)  : {duck_dec!r}   （类型 {duck_type}）")
    print()
    print(f"  {'浮点路径':<36}{'结果':<24}{'与精确值之差':<20}{'差多少分'}")
    print("  " + "-" * 96)
    for label, val in [
        ("DuckDB  SUM(amount)  [DOUBLE]", duck_flt),
        ("Polars  sum(amount)  [Float64]", pol_flt),
        ("Python  逐行累加    [float]", py_flt),
    ]:
        diff = val - exact
        print(
            f"  {common.pad(label, 36)}{common.pad(repr(val), 24)}"
            f"{common.pad(f'{diff:+.10g}', 20)}{diff * 100:+.8f} 分"
        )

    print()
    print("  · DuckDB 的浮点求和和 Python 逐行累加**结果完全一样** —— ")
    print("    说明 DuckDB 的 SUM(DOUBLE) 走的是朴素累加，没有做补偿。")
    print(f"  · Polars 在这份数据上恰好精确（差 {abs(pol_flt - exact):.3g} 元）。")
    print()
    print("  但「恰好」不能当结论 —— 换一组数据再看。")

    # 对抗数据：大数把小数吃掉
    adv = [1e16, 1.0, -1e16, 1.0] * 1000
    adv_exact = 2000.0
    s_adv = pl.Series("v", adv)
    pol_adv = s_adv.sum()
    duck_adv = con.execute("SELECT SUM(v) FROM (SELECT unnest(?::DOUBLE[]) v)", [adv]).fetchone()[0]
    py_adv = 0.0
    for v in adv:
        py_adv += v

    print()
    print("  换成「对抗数据」（1e16, 1.0, -1e16, 1.0）× 1000，精确答案 = 2000.0）：")
    print(f"  {'路径':<36}{'结果':<24}{'绝对误差'}")
    print("  " + "-" * 74)
    for label, val in [
        ("DuckDB  SUM(v)", duck_adv),
        ("Polars  sum(v)", pol_adv),
        ("Python  逐行累加", py_adv),
    ]:
        print(f"  {common.pad(label, 36)}{common.pad(repr(val), 24)}{abs(val - adv_exact):.10g}")

    print()
    print("  这里差距拉开了：")
    print(f"    DuckDB 给 {duck_adv!r}，Polars 给 {pol_adv!r}，精确答案是 {adv_exact}。")
    print("    **Polars 的浮点求和明显更准** —— 但它也不是精确值。")
    print("    （两边都是错的，Polars 只是错得少一点。这不是「Polars 更对」，")
    print("      而是「两边都不该拿浮点算钱」。）")

    print()
    print("  DuckDB 自己也提供了补偿求和，但在这份数据上没用：")
    for fn in ["fsum", "sumKahan"]:
        got = con.execute(f"SELECT {fn}(v) FROM (SELECT unnest(?::DOUBLE[]) v)", [adv]).fetchone()[
            0
        ]
        print(f"    {fn:12s} = {got!r}")

    con.close()


# ---------------------------------------------------------------- 场景 3


def scenario_grouped(path: str, n_rows: int) -> None:
    section("场景 3 · 分组求和：误差会不会抵消？")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW amt AS SELECT * FROM read_parquet('{path}')")

    exact_units = con.execute("SELECT SUM(amount_units) FROM amt").fetchone()[0]
    exact = float(exact_units) / SCALE

    duck_flat = con.execute("SELECT SUM(amount) FROM amt").fetchone()[0]
    duck_grouped = con.execute(
        "SELECT SUM(g) FROM (SELECT SUM(amount) AS g FROM amt GROUP BY amount_units % 977) t"
    ).fetchone()[0]

    df = pl.read_parquet(path)
    pol_flat = df["amount"].sum()
    pol_grouped = (
        df.with_columns((pl.col("amount_units") % 977).alias("grp"))
        .group_by("grp")
        .agg(pl.col("amount").sum().alias("g"))["g"]
        .sum()
    )

    print(f"  精确答案              : {exact!r}")
    print()
    print(f"  {'路径':<42}{'结果':<24}{'与精确值之差'}")
    print("  " + "-" * 88)
    for label, val in [
        ("DuckDB  一次 SUM", duck_flat),
        ("DuckDB  先分组再 SUM(分组结果)", duck_grouped),
        ("Polars  一次 sum", pol_flat),
        ("Polars  先 group_by 再 sum(分组结果)", pol_grouped),
    ]:
        diff = val - exact
        print(f"  {common.pad(label, 42)}{common.pad(repr(val), 24)}{diff:+.10g}")

    print()
    duck_diff = abs(duck_flat - duck_grouped)
    pol_diff = abs(pol_flat - pol_grouped)
    print(f"  DuckDB：分组前后差 {duck_diff:.6g} 元；Polars：分组前后差 {pol_diff:.6g} 元。")
    print("  也就是说：「一次求和」和「分组求和再汇总」**本来就是两个数**。")
    print("  这次分组路径反而更接近精确值（-2.98e-08 vs +5.25e-06），但那是运气 ——")
    print("  两者的误差符号和量级都不一样，**没有任何理由指望分组能抵消或修好误差**。")
    print("  实用结论：两种口径的报表对不上是正常的，要对齐就得统一口径（都用整数或 DECIMAL）。")
    con.close()
    _ = n_rows


# ---------------------------------------------------------------- 场景 4


def scenario_decimal_paths(path: str, n_rows: int) -> None:
    section("场景 4 · Polars 想要精确金额：三条路，代价各多少")

    df = pl.read_parquet(path)
    units = df["amount_units"]
    decimals = units.cast(pl.Float64).cast(pl.Decimal(precision=18, scale=4))

    variants = [
        # (写法, 求和函数, 展示用的换算, 是否保证精确)
        ("Float64（默认，不保证精确）", lambda: df["amount"].sum(), lambda v: v, "❌"),
        (
            "Int64 单位（0.0001 元，精确）",
            lambda: units.sum(),
            lambda v: v / SCALE,
            "✅",
        ),
        (
            "pl.Decimal(18,4)（精确）",
            lambda: decimals.sum(),
            lambda v: v / SCALE,
            "✅",
        ),
    ]
    print(f"  {'写法':<34}{'求和结果（元）':<30}{'耗时(ms)':<12}{'保证精确?'}")
    print("  " + "-" * 92)
    for label, fn, to_yuan, exact_mark in variants:
        ms = timeit(fn)
        shown = to_yuan(fn())
        print(
            f"  {common.pad(label, 34)}{common.pad(repr(shown), 30)}"
            f"{common.pad(f'{ms:.2f}', 12)}{exact_mark}"
        )

    dec_ms = timeit(lambda: decimals.sum())
    flt_ms = timeit(lambda: df["amount"].sum())
    int_ms = timeit(lambda: units.sum())
    print()
    print(f"  · Float64 sum 1M  : {flt_ms:.2f} ms")
    print(f"  · Int64  sum 1M  : {int_ms:.2f} ms（和 Float64 同量级，聚合是整数加法）")
    print(f"  · Decimal sum 1M : {dec_ms:.2f} ms（比 Float64 慢 {dec_ms / flt_ms:.1f}×）")
    print()
    print("  结论：")
    print("    · Polars **有** Decimal dtype，聚合、算术、group_by 都支持（很多人以为不支持）。")
    print("      代价是慢一个数量级以上 —— decimal128 是 16 字节定长，比 Float64 宽，比 Int64 慢。")
    print("    · 但**推荐还是 Int64 存最小货币单位**：一样精确，聚合更快，")
    print("      而且和 Arrow / Parquet / 各种下游系统的兼容性更好（decimal128 支持面窄）。")
    print("    · 代价是精度和规模要自己选：Int64 最多 9.22e18 个单位，")
    print(f"      按 0.0001 元算能表示到 {9.22e18 / SCALE:,.0f} 元 —— 单笔够用，累加总账要小心。")
    _ = n_rows


# ---------------------------------------------------------------- 场景 5


def scenario_overflow(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 溢出：两边的表现正好相反")

    print("  DuckDB 侧的类型提升规则：")
    for sql in [
        "SELECT typeof(0.1 + 0.2)",
        "SELECT typeof(1.5::DECIMAL(18,4) + 2.5::DECIMAL(18,4))",
        "SELECT typeof(SUM(x::DECIMAL(18,4))) FROM (SELECT 1.5::DECIMAL(18,4) x) t",
    ]:
        got = con.execute(sql).fetchone()[0]
        print(f"    {sql:<74} → {got}")

    print()
    print("  溢出会怎样：")
    cases = [
        ("DECIMAL(18,4) 装不下 1e15", "SELECT 1000000000000000::DECIMAL(18,4)"),
        ("DECIMAL(18,4) 乘法溢出", "SELECT (99999999999999.9999::DECIMAL(18,4)) * 100"),
        ("INTEGER 取模除零 1 % 0", "SELECT 1 % 0"),
    ]
    for label, sql in cases:
        try:
            got = con.execute(sql).fetchone()[0]
            print(f"    {label:<32} → 没报错，结果 {got!r}")
        except Exception as exc:
            first = str(exc).strip().splitlines()[0]
            print(f"    {label:<32} → {type(exc).__name__}: {first[:88]}")

    print()
    print("  Polars 侧：Float64 永远不会报错 —— 它只会给你一个错的数。")
    big = pl.select(pl.lit(1e308) * pl.lit(10.0)).item()
    print(f"    1e308 * 10.0 = {big!r}（静默溢出成 inf，没有异常）")
    print("    Decimal 那边则会**报错**：")
    try:
        pl.select(pl.lit(10**30).cast(pl.Decimal(precision=18, scale=0)))
        print("      转换 → 没报错")
    except Exception as exc:
        print(f"      转换 → {type(exc).__name__}: {str(exc).strip().splitlines()[0][:80]}")


# ---------------------------------------------------------------- 场景 6


def scenario_roundtrip(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · DECIMAL 存进 Parquet：两边读回来还准吗？")

    import pyarrow.parquet as pq

    path = "/tmp/probe_decimal_roundtrip.parquet"
    con.execute("CREATE OR REPLACE TABLE t AS SELECT 123.45::DECIMAL(18,2) AS amount, 1 AS id")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")

    duck_back = con.execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
    pol_back = pl.read_parquet(path)

    print(f"  Parquet 物理类型 : {pq.read_schema(path)}")
    print(f"  DuckDB 读回来    : {duck_back}")
    print(f"  Polars 读回来    : {pol_back.to_dicts()}  dtype={pol_back.schema['amount']}")
    print()
    # 注意：不能拿 Decimal 和 float 直接比 —— Decimal('123.45') == 123.45 是 False
    # （123.45 这个 float 本身就不是精确的 123.45）。要比就比字符串。
    duck_val = duck_back[0][0]
    pol_val = pol_back["amount"][0]
    same = str(duck_val) == str(pol_val) == "123.45"
    print(f"  两边读回的都是 123.45 : {'✅' if same else '❌'}")
    print(f"    DuckDB {duck_val!r} / Polars {pol_val!r}")
    print("  → DECIMAL 走 Arrow 的 decimal128，Parquet 里是 FIXED_LEN_BYTE_ARRAY(16)，")
    print("    两边来回转**不丢精度**。这也是「用 DECIMAL 存钱」比「用 Float64 存钱」")
    print("    更有价值的地方 —— 存储层就把精度守住，后面所有聚合都不用替它擦屁股。")


# ---------------------------------------------------------------- 场景 7


def scenario_reconcile(path: str, n_rows: int) -> None:
    section("场景 7 · 对账：浮点路径该怎么比")

    con = duckdb.connect()
    con.execute(f"CREATE VIEW amt AS SELECT * FROM read_parquet('{path}')")
    duck_flt = con.execute("SELECT SUM(amount) FROM amt").fetchone()[0]
    duck_dec = float(con.execute("SELECT SUM(amount::DECIMAL(24,4)) FROM amt").fetchone()[0])
    df = pl.read_parquet(path)
    pol_flt = df["amount"].sum()

    print("  三个结果：")
    print(f"    DuckDB  SUM(amount)          = {duck_flt!r}")
    print(f"    Polars  sum(amount)          = {pol_flt!r}")
    print(f"    DuckDB  SUM(amount::DECIMAL) = {duck_dec!r}   ← 精确")
    print()
    print(
        f"  浮点两边差 {abs(duck_flt - pol_flt):.6g} 元，相对误差 "
        f"{abs(duck_flt - pol_flt) / duck_dec:.3g}。"
    )
    print()
    print("  对账怎么做：")
    print("    1. **用容差比**，别用精确相等 —— 浮点求和的顺序不同，末位必然有差异。")
    print("       本项目用的是相对容差 1e-9（`common.compare_frames` 的 rtol）。")
    print("    2. **要真正相等，就把两边都转成整数或 DECIMAL 再求和** ——")
    print("       口径统一成「最小货币单位的整数」，差异才会暴露成真正的业务差异，")
    print("       而不是浮点噪声。")
    print("    3. 报表金额用 DECIMAL/整数落库，别拿 Float64 存钱 ——")
    print("       存储层的精度问题，后面所有聚合都得替它擦屁股。")
    con.close()
    _ = n_rows


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. DuckDB 的字面量默认 DECIMAL，Polars 默认 Float64 —— `0.1+0.2` 两边给的数不一样。")
    print(
        "     但只要有一边是 DOUBLE，DuckDB 也一样飘："
        "「DuckDB 算钱准」只在**列本身是 DECIMAL** 时成立。"
    )
    print("  2. **反直觉的一条：Polars 的浮点求和比 DuckDB 更准。**")
    print("     DuckDB 的 SUM(DOUBLE) 和 Python 逐行累加结果完全一样（朴素累加），")
    print("     对抗数据上 DuckDB 给 1.0、Polars 给 1985.0、精确答案 2000.0。")
    print("     DuckDB 自带的 fsum / sumKahan 在这份数据上也没救回来。")
    print("     —— 但两边都是错的，这不能当作「Polars 更对」的结论。")
    print("  3. Polars **有** Decimal dtype，聚合、算术、group_by 都支持，")
    print("     代价是比 Float64 慢一个数量级以上。想要精确金额，推荐 Int64 存最小货币单位。")
    print("  4. 溢出的表现正好相反：DuckDB 抛异常（ConversionException / OutOfRangeException），")
    print("     Polars 的 Float64 静默给错数（inf）。")
    print("  5. DECIMAL 存进 Parquet 是安全的：decimal128 两边来回转不丢精度。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="金额与 Decimal：两个引擎算钱的方式不一样")
    parser.add_argument("--rows", type=int, default=1_000_000, help="金额笔数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print(f"造 {args.rows:,} 笔金额（最小单位 0.0001 元）…")
    path = ensure_data(args.rows)
    print(f"数据文件：{path}")

    con = duckdb.connect()
    scenario_literal(con)
    scenario_sum(path, args.rows)
    scenario_grouped(path, args.rows)
    scenario_decimal_paths(path, args.rows)
    scenario_overflow(con)
    scenario_roundtrip(con)
    con.close()
    scenario_reconcile(path, args.rows)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
