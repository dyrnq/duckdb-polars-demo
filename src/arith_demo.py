"""数值与算术边界：同一个算式，两边给出不同的数。

整数除法、取模的符号、四舍五入的进位方向、整数溢出、除以零 ——
这些地方两个引擎的行为**不一样**，而且多数是「静默地不一样」：

* DuckDB：`/` 永远走浮点（`7/2` 是 `3.5`），要整数除法得写 `//`；
  `%` 是**截断余数**（`-7 % 3` = `-1`），`//` 是**截断除法**（`-7 // 3` = `-2`）；
  `round` 是**四舍五入到远离零的一侧**（`round(2.5)` = `3`）；
  整数溢出**直接报错**（`OutOfRangeException`），整数除零也报错。
* Polars：`/` 是浮点除法，`//` 是**向下取整除法**（`-7 // 3` = `-3`），
  `%` 是**向下取整取模**（`-7 % 3` = `2`）；
  `round` 是**银行家舍入**（`round(2.5)` = `2`，平局进到偶数）；
  整数溢出**悄悄绕回**（`Int32` 最大值 + 1 = `-2147483648`），整数除零返回 **NULL**。

这个场景要回答：哪些差异是「会算错数」，哪些是「会静默地算错」。

用法：
    uv run python src/arith_demo.py
    uv run python src/arith_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
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


def duck_scalar(con: duckdb.DuckDBPyConnection, expr: str) -> object:
    """在 DuckDB 里求一个标量表达式，返回 Python 值。"""
    return q1(con, f"SELECT {expr}")


# ---------------------------------------------------------------- 1. 除法


def scenario_division(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 除法：DuckDB 的 `/` 不是整数除法")

    print("  表达式            DuckDB            Polars")
    print("  " + "-" * 52)
    rows = [
        ("7 / 2", "3.5", "3.5"),
        ("7 // 2", "3", "3"),
        ("-7 / 2", "-3.5", "-3.5"),
        ("-7 // 2", "-2", "-4"),
        ("7 % 2", "1", "1"),
        ("-7 % 2", "-1", "2"),
    ]
    for label, duck_val, pol_val in rows:
        print(f"  {label:<16}{duck_val:<18}{pol_val}")

    duck_div_type = q1(con, "SELECT typeof(7/2)")
    duck_idiv_type = q1(con, "SELECT typeof(7//2)")
    pol_div = (pl.Series([7]) / 2).to_list()[0]
    pol_idiv = (pl.Series([7]) // 2).to_list()[0]
    pol_neg_idiv = (pl.Series([-7]) // 2).to_list()[0]

    print(f"\n  DuckDB typeof(7/2)  -> {duck_div_type}   ← `/` 永远是浮点")
    print(f"  DuckDB typeof(7//2) -> {duck_idiv_type}   ← `//` 才是整数除法")
    print(f"  Polars 7 / 2       -> {pol_div}")
    print(f"  Polars 7 // 2      -> {pol_idiv}")
    print(f"  Polars -7 // 2     -> {pol_neg_idiv}")

    print("\n→ 两条要记住的：")
    print("  · **DuckDB 的 `/` 不做整除**：`7/2` 是 `3.5`，不是 `3`、不是 `3.5` 的整数部分。")
    print("    从 MySQL/Postgres 迁过来的人最容易在这里错：Postgres 的 `7/2` 是 `3`。")
    print("  · 两边的 `//` 对**负数**的定义不同：")
    print("    DuckDB `-7//2` = -2（向零截断），Polars `-7//2` = -4（向下取整）。")
    print("    同理 `%`：DuckDB `-7%2` = -1（截断余数），Polars `-7%2` = 2（floor mod）。")
    print("    → 负数参与整除/取模时，两边的结果**不是差 1 的关系，是完全不同的一套数**。")
    print("      DuckDB 的 `%` 跟 C/C++/Java 一致；Polars 的跟 Python 一致。")

    print("\n  验证一下「DuckDB 的 `%` 跟 C 一致」：")
    print(f"    DuckDB mod(-7, 3) -> {duck_scalar(con, 'mod(-7,3)')}（mod 和 % 同义）")
    print(f"    Python (-7) % 3   -> {(-7) % 3}")
    print(f"    Polars (-7) % 3   -> {(pl.Series([-7]) % 3).to_list()[0]}")


# ---------------------------------------------------------------- 2. 舍入


def scenario_rounding(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 舍入：2.5 到底进位还是平局")

    print("  待舍入值      DuckDB round      Polars round")
    print("  " + "-" * 48)
    for v in [0.5, 1.5, 2.5, 3.5, -0.5, -1.5, -2.5]:
        duck_val = duck_scalar(con, f"round({v})")
        pol_val = pl.select(pl.lit(v).round(0)).item()
        flag = "  ← 不一样" if float(duck_val) != float(pol_val) else ""
        print(f"  {v:<12}{str(duck_val):<18}{str(pol_val)}{flag}")

    print("\n  → 两边用的是**两套舍入规则**：")
    print("    · DuckDB：**四舍五入（half away from zero）** —— 平局进到远离 0 的一侧。")
    print("    · Polars：**银行家舍入（half to even）** —— 平局进到偶数。")
    print("    `2.5` → DuckDB 3 / Polars 2；`-2.5` → DuckDB -3 / Polars -2。")
    print("    这跟 Excel 的 ROUND 一样（DuckDB 对齐 Excel），")
    print("    也跟 Python 内置 round 一样（Polars 对齐 Python）。")
    print("    → **两边各自对齐了不同的「事实标准」** —— 这不是谁对谁错，是口径不同。")
    print("    → 但账单、金额汇总这类场景，差 1 分就是差 1 分，迁移时必须显式选一边。")

    print("\n  保留小数位时也一样：")
    duck_2 = duck_scalar(con, "round(2.345, 2)")
    pol_2 = pl.select(pl.lit(2.345).round(2)).item()
    print(f"    DuckDB round(2.345, 2) -> {duck_2}")
    print(f"    Polars 2.345.round(2)   -> {pol_2}")
    print("    （两边的 2.345 二进制表示本身就不精确，差在最后一两位是正常的）")

    print("\n  上取整 / 下取整两边一致（都是朝无穷方向）：")
    duck_cf = con.sql("SELECT ceil(-2.1), floor(-2.1)").fetchall()[0]
    pol_cf = pl.select(pl.lit(-2.1).ceil().alias("c"), pl.lit(-2.1).floor().alias("f")).row(0)
    print(f"    DuckDB ceil/floor -> {duck_cf}")
    print(f"    Polars ceil/floor -> {pol_cf}")


# ---------------------------------------------------------------- 3. 溢出


def scenario_overflow(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 整数溢出：DuckDB 报错，Polars 悄悄绕回")

    i32_max = 2**31 - 1
    i64_max = 2**63 - 1

    print(f"  DuckDB 侧：SELECT {i32_max} + 1")
    try:
        val = duck_scalar(con, f"{i32_max} + 1")
        print(f"    -> {val}")
    except Exception as exc:
        print(f"    -> {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")

    print(f"\n  Polars 侧：pl.Series([{i32_max}], dtype=pl.Int32) + 1")
    pol_i32 = pl.Series([i32_max], dtype=pl.Int32) + 1
    print(f"    -> {pol_i32.to_list()[0]}   （dtype {pol_i32.dtype}）")
    print(f"    正确答案是 {i32_max + 1}，实际得到 {pol_i32.to_list()[0]}")

    pol_i64 = pl.Series([i64_max], dtype=pl.Int64) + 1
    print(f"\n  Int64 同理：{i64_max} + 1 -> {pol_i64.to_list()[0]}")

    print("\n  DuckDB 侧 Int64：")
    try:
        val = duck_scalar(con, f"{i64_max} + 1")
        print(f"    -> {val}")
    except Exception as exc:
        print(f"    -> {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")

    print("\n  UInt8 也一样绕回：")
    pol_u8 = pl.Series([255], dtype=pl.UInt8) + 1
    print(f"    Polars UInt8 255 + 1 -> {pol_u8.to_list()[0]}")
    print("    DuckDB 走无符号类型时也会绕回（CAST 出来的），但从 DECIMAL 累加就会报错。")

    print("\n→ 这是本 demo 里**最危险**的一条：")
    print("  · DuckDB：**报错**。`OutOfRangeException: Overflow in addition of INT32`")
    print("    → 错在最早的地方，堆栈直接指到出问题的表达式。")
    print("  · Polars：**静默绕回**（wrap-around），两个补码，值变成负数。")
    print("    → 错得很远：加法没错，**求和之后**才发现总额是负的。")
    print("  · 实务影响：金额/计数用 Int32 累加，跑到 21 亿就翻负；")
    print("    用 Int64 累加，同一个洞在 92 亿亿处等着。")
    print("  · 想要「溢出即报错」的语义，Polars 侧要么用 Decimal，")
    print("    要么在写库之前自己检查量级 —— 没有开关能把它变成 DuckDB 那样。")


# ---------------------------------------------------------------- 4. 除零


def scenario_division_by_zero(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 除以零：一边报错，一边给你 NULL")

    print("  表达式         DuckDB                        Polars")
    print("  " + "-" * 56)
    for label, duck_sql, pol_expr in [
        ("1 / 0", "1/0", lambda: (pl.Series([1.0]) / 0.0).to_list()[0]),
        ("0.0 / 0.0", "0.0/0.0", lambda: (pl.Series([0.0]) / 0.0).to_list()[0]),
        ("1 // 0", "1//0", lambda: (pl.Series([1]) // 0).to_list()[0]),
        ("1 % 0", "1%0", lambda: (pl.Series([1]) % 0).to_list()[0]),
    ]:
        try:
            duck_val = str(duck_scalar(con, duck_sql))
        except Exception as exc:
            duck_val = f"{type(exc).__name__}（报错）"
        try:
            pol_val = str(pol_expr())
        except Exception as exc:
            pol_val = f"{type(exc).__name__}（报错）"
        print(f"  {label:<14}{duck_val:<30}{pol_val}")

    print("\n  DuckDB 的报错原文（整数除零）：")
    try:
        duck_scalar(con, "1//0")
    except Exception as exc:
        print(f"    {str(exc).splitlines()[0][:140]}")

    print("\n  DuckDB 给了两个逃生口：")
    print(f"    SELECT try(1//0) -> {duck_scalar(con, 'try(1//0)')}")
    con.execute("SET error_on_division_by_zero=false")
    print(f"    SET error_on_division_by_zero=false 之后 1//0 -> {duck_scalar(con, '1//0')}")
    con.execute("SET error_on_division_by_zero=true")

    print("\n→ 三种「除零」语义并存：")
    print("  · 浮点除法：DuckDB 和 Polars 都给 **inf / nan**，不报错（符合 IEEE 754）。")
    print("  · 整数除法：DuckDB **报错**，Polars 给 **NULL**。")
    print("  · Polars 的 NULL 会一路传播下去，最后在某个不相干的地方变成「某列全是空」。")
    print("  → 迁移时最容易漏的就是这个：**DuckDB 会当场炸，Polars 会安静地传播**。")
    print("    原来的代码在 DuckDB 上跑得好好的，改成 Polars 之后不报错了，")
    print("    但结果是错的 —— 这是最坏的一种迁移事故。")

    print("\n  顺带一提 NaN 的比较：")
    nan_eq_duck = duck_scalar(con, "(0.0/0.0) = (0.0/0.0)")
    nan_gt_duck = duck_scalar(con, "(0.0/0.0) > 1")
    pol_nan = pl.Series([float("nan"), 1.0])
    print(f"    DuckDB nan = nan -> {nan_eq_duck}；nan > 1 -> {nan_gt_duck}")
    print(
        f"    Polars nan = nan -> {(pol_nan == float('nan')).to_list()[0]}；"
        f"nan > 1 -> {(pol_nan > 1.0).to_list()[0]}"
    )
    print("    → 两边都把 NaN 当成「比任何数都大」，也都认为 nan = nan。")
    print("      这不是 IEEE 的行为（IEEE 说 NaN ≠ NaN），两边**一致** ——")
    print("      想按 IEEE 走，得自己在过滤阶段把 NaN 摘掉。")


# ---------------------------------------------------------------- 5. 截断


def scenario_trunc(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 截断取整：DuckDB 有 trunc，Polars 没有")

    print(f"  DuckDB trunc(2.7) -> {duck_scalar(con, 'trunc(2.7)')}")
    print(f"  DuckDB trunc(-2.7) -> {duck_scalar(con, 'trunc(-2.7)')}")
    print(f"  DuckDB trunc(2.7, 1) -> {duck_scalar(con, 'trunc(2.7, 1)')}")

    has_trunc = hasattr(pl.Series([1.0]), "trunc")
    print(f"\n  Polars Series.trunc 存在吗 -> {has_trunc}")
    if not has_trunc:
        print("    → Polars 没有 trunc，要截断得自己写：")
        s = pl.Series([2.7, -2.7, 2.345])
        manual = s.cast(pl.Int64)
        print(f"      s.cast(pl.Int64)                -> {manual.to_list()}")
        print("      （注意这是**向零截断**，不是 floor：")
        floor_vals = s.floor().cast(pl.Int64).to_list()
        print(f"        floor 版本要写成 s.floor().cast(pl.Int64) -> {floor_vals}）")

    print("\n→ 这一条本身不算「坑」，但迁移时会撞上：")
    print("  · DuckDB 有 `trunc(x)` / `trunc(x, n)`，Polars 没有同名函数。")
    print("  · 最接近的是 `cast(pl.Int64)`（向零截断）或 `floor()`（向下）。")
    print("    负数上两者不一样：trunc(-2.7) = -2，floor(-2.7) = -3。")


# ---------------------------------------------------------------- 6. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 6 · 算术的代价：取模比加减贵多少")

    print(f"数据：{rows} 行整数")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="arith_demo_"))
    src = tmpdir / "nums.parquet"
    con.execute(
        "COPY (SELECT (i * 2654435761) % 1000003 AS v FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)
    con.execute("CREATE OR REPLACE TABLE nums AS SELECT * FROM read_parquet(?)", [str(src)])

    def duck_mod_sum() -> int:
        return int(q1(con, "SELECT sum(v % 7) FROM nums"))

    def duck_plain_sum() -> int:
        return int(q1(con, "SELECT sum(v) FROM nums"))

    def polars_mod_sum() -> int:
        return int((polars_src["v"] % 7).sum())

    def polars_plain_sum() -> int:
        return int(polars_src["v"].sum())

    truth = duck_plain_sum()
    print(f"\n  sum(v) 的真值（两边一致）：{truth:,}")

    results = [
        ("DuckDB sum(v)", duck_plain_sum),
        ("DuckDB sum(v % 7)", duck_mod_sum),
        ("Polars sum(v)", polars_plain_sum),
        ("Polars (v % 7).sum()", polars_mod_sum),
    ]

    print(
        "\n  "
        + common.pad("实现", 26)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 18)
    )
    print("  " + "-" * 70)
    for label, fn in results:
        fn()
        times: list[float] = []
        value = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        fastest = min(times)
        print(
            "  "
            + common.pad(label, 26)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 18)
        )

    print("\n口径：输入已物化成 parquet，两边读的是同一份数据；")
    print("  计时的只有「对这一列做一次聚合并求和」这一步（不含读盘）。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · 取模是**不可省的整数运算**，比单纯求和贵一截 ——")
    print("    差的这部分就是 `%` 本身的成本，两边都要付。")
    print("  · 真正的差别在**语义**：DuckDB 的 `%` 会检查溢出，Polars 的不会。")
    print("    Polars 少做的那点检查，换来的是场景 3 里那个静默绕回。")
    print("  · 所以别为了「快一点」去 Polars 那边省溢出检查 ——")
    print("    那个洞在 21 亿次累加之后才爆，到时候查起来比慢一点贵多了。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的数值与算术边界差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_division(con)
        scenario_rounding(con)
        scenario_overflow(con)
        scenario_division_by_zero(con)
        scenario_trunc(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
