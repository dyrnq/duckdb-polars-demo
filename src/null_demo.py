"""NULL 语义与三值逻辑：SQL 的 NULL 不是「空值」，是「不知道」。

`dialect_demo.py` 已经覆盖了 NULL 排序位置和 NaN vs NULL。
这个脚本专门挖 SQL 三值逻辑（TRUE / FALSE / UNKNOWN）带来的静默差异 ——

  1. `NULL = NULL` 是 UNKNOWN，不是 TRUE —— 那么「找相等的行」该怎么写？
  2. 连接键为 NULL 时，两边会不会匹配上？
  3. 聚合函数遇到 NULL 会怎样？`SUM` 全 NULL 一边给 NULL、一边给 0。
  4. `COUNT(DISTINCT)` 算不算 NULL？Polars 的 `n_unique()` 算。
  5. `GROUP BY` 里的 NULL 归到哪一组？
  6. `NOT IN (1, NULL)` 为什么返回空集，而 Polars 的 `~is_in` 不是？
  7. 除以零：报错还是给 inf？

数据是一张 6 行的小表，NULL 都故意放在关键位置。

用法：
    uv run python src/null_demo.py
"""

from __future__ import annotations

import argparse

import duckdb
import polars as pl

import common

# 一份故意带 NULL 的小表：连接键、分组键、数值列都有 NULL
EDGE = pl.DataFrame(
    {
        "id": [1, 2, 3, 4, 5, 6],
        "k": [1, 2, None, 1, None, 3],
        "grp": ["a", None, "a", "b", None, "b"],
        "v": [10, 20, None, 40, None, 60],
    }
)

# 右表：连接键同样带 NULL，而且右表的非键列**不是** NULL，
# 这样才能分辨「没匹配上」和「匹配上了但值是 NULL」
RIGHT = pl.DataFrame(
    {
        "k": [1, None, 4],
        "rv": ["A", "NULL-KEY", "D"],
    }
)


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def connect() -> duckdb.DuckDBPyConnection:
    """DuckDB 侧直接注册同一份 Arrow 表 —— 两边看的是同一份数据。"""
    con = duckdb.connect()
    con.register("edge", EDGE)
    con.register("right_t", RIGHT)
    return con


# ---------------------------------------------------------------- 场景 1


def scenario_null_equality(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · NULL = NULL 是 UNKNOWN，不是 TRUE")

    duck_one = con.execute("SELECT NULL = NULL").fetchone()[0]
    duck_cnt = con.execute("SELECT COUNT(*) FROM edge WHERE k = NULL").fetchone()[0]
    duck_isnull = con.execute("SELECT COUNT(*) FROM edge WHERE k IS NULL").fetchone()[0]

    pol_expr = EDGE.select((pl.col("k") == pl.col("k")).sum()).item()
    pol_filtered = EDGE.filter(pl.col("k") == pl.col("k")).height
    pol_isnull = EDGE.filter(pl.col("k").is_null()).height

    print(f"  DuckDB  SELECT NULL = NULL              → {duck_one!r}（UNKNOWN，不是 TRUE）")
    print(f"  Polars  (k == k) 求和                    → {pol_expr}（True 记 1，null 记 0）")
    print()
    print(f"  {'写法':<38}{'结果'}")
    print("  " + "-" * 60)
    print(f"  {common.pad('DuckDB  WHERE k = NULL 的行数', 38)}{duck_cnt}")
    print(f"  {common.pad('Polars  filter(k == k) 的行数', 38)}{pol_filtered}")
    print(f"  {common.pad('DuckDB  WHERE k IS NULL 的行数', 38)}{duck_isnull}")
    print(f"  {common.pad('Polars  filter(k.is_null()) 的行数', 38)}{pol_isnull}")
    print()
    print("  两边一致：`=` 遇到 NULL 给 UNKNOWN，过滤时 UNKNOWN 和 FALSE 一样被丢掉。")
    print("  想找「两列都相等」的行，得用「null 安全相等」：")
    print()
    duck_safe = con.execute("SELECT COUNT(*) FROM edge WHERE k IS NOT DISTINCT FROM k").fetchone()[
        0
    ]
    pol_safe = EDGE.filter((pl.col("k") == pl.col("k")).fill_null(True)).height
    print(f"    DuckDB  k IS NOT DISTINCT FROM k        → {duck_safe} 行")
    print(f"    Polars  (k == k).fill_null(True)        → {pol_safe} 行")
    print()
    print("  Polars 没有 `IS NOT DISTINCT FROM` 这种运算符，")
    print("  `fill_null(True)` 是等价写法 —— 但**别忘了 fill_null**，")
    print("  否则 null == null 还是 null，过滤时照样被丢掉。")


# ---------------------------------------------------------------- 场景 2


def scenario_join_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 连接键为 NULL：会匹配上吗？")

    duck_rows = con.execute(
        "SELECT e.id, e.k, r.rv FROM edge e LEFT JOIN right_t r ON e.k = r.k ORDER BY e.id"
    ).fetchall()
    pol_rows = EDGE.join(RIGHT, on="k", how="left").sort("id").to_dicts()

    print("  DuckDB  LEFT JOIN（ON e.k = r.k）：")
    for row in duck_rows:
        print(f"    id={row[0]}  k={row[1]!r}  rv={row[2]!r}")
    print()
    print("  Polars  join(how='left')：")
    for row in pol_rows:
        print(f"    id={row['id']}  k={row['k']!r}  rv={row['rv']!r}")
    print()
    print("  右表有一行 k=NULL, rv='NULL-KEY' —— 它有没有被匹配上？")
    duck_matched = con.execute(
        "SELECT COUNT(*) FROM edge e JOIN right_t r ON e.k = r.k WHERE r.rv = 'NULL-KEY'"
    ).fetchone()[0]
    pol_matched = EDGE.join(RIGHT, on="k", how="inner").filter(pl.col("rv") == "NULL-KEY").height
    print(f"    DuckDB  匹配到 rv='NULL-KEY' 的行数 : {duck_matched}")
    print(f"    Polars  匹配到 rv='NULL-KEY' 的行数 : {pol_matched}")
    print()
    print("  结论：**两边默认都不匹配** —— NULL 键不参与连接（SQL 标准行为）。")
    print("  但两边都留了「让 NULL 也能匹配」的口子：")
    duck_nulls_equal = con.execute(
        "SELECT COUNT(*) FROM edge e JOIN right_t r ON e.k IS NOT DISTINCT FROM r.k"
    ).fetchone()[0]
    pol_nulls_equal = EDGE.join(RIGHT, on="k", how="inner", nulls_equal=True).height
    print(f"    DuckDB  ON e.k IS NOT DISTINCT FROM r.k  → {duck_nulls_equal} 行")
    print(f"    Polars  join(nulls_equal=True)             → {pol_nulls_equal} 行")
    print()
    print("  （注意 Polars 2.x 里这个参数叫 `nulls_equal`，2.0 之前叫 `join_nulls`，")
    print("    传旧名字会直接报 ArgumentRemovedError。）")


# ---------------------------------------------------------------- 场景 3


def scenario_agg_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 聚合函数遇到 NULL：有一处两边不一样")

    con.execute("CREATE OR REPLACE TABLE all_null AS SELECT * FROM (VALUES (NULL::INT)) t(v)")
    all_null_polars = pl.DataFrame({"v": [None]}, schema={"v": pl.Int64})

    duck = con.execute(
        "SELECT COUNT(*) AS n_all, COUNT(v) AS n_v, SUM(v) AS s, AVG(v) AS a FROM all_null"
    ).fetchone()
    pol = all_null_polars.select(
        pl.len().alias("n_all"),
        pl.col("v").count().alias("n_v"),
        pl.col("v").sum().alias("s"),
        pl.col("v").mean().alias("a"),
    ).to_dicts()[0]

    print("  表里只有一行 v=NULL：")
    print(f"  {'指标':<20}{'DuckDB':<16}{'Polars':<16}{'一致?'}")
    print("  " + "-" * 62)
    rows = [
        ("COUNT(*)", duck[0], pol["n_all"]),
        ("COUNT(v)", duck[1], pol["n_v"]),
        ("SUM(v)", duck[2], pol["s"]),
        ("AVG(v)", duck[3], pol["a"]),
    ]
    for label, a, b in rows:
        mark = "✅" if a == b else "❌ **不一致**"
        print(f"  {common.pad(label, 20)}{common.pad(str(a), 16)}{common.pad(str(b), 16)}{mark}")
    print()
    print("  · COUNT(*) / COUNT(v) / AVG(v) 两边一致。")
    print("  · **SUM(v) 不一致：DuckDB 给 NULL，Polars 给 0。**")
    print("    这不是 bug，是两边对「空集的和」的定义不同 —— SQL 里 SUM 无行 = NULL，")
    print("    Polars 里 sum 无值 = 0。")
    print()
    print("    对账时这一条会直接咬人：拿两个引擎的 SUM 相减，")
    print("    一边是 NULL 一边是 0，差值算出来是 NULL 还是 0，取决于你怎么写。")
    print()
    print("    空表也一样：")
    duck_empty = con.execute("SELECT SUM(v) FROM (SELECT NULL::INT v WHERE FALSE) t").fetchone()[0]
    pol_empty = (
        pl.DataFrame({"v": []}, schema={"v": pl.Int64})
        .select(pl.col("v").sum().alias("s"))
        .to_dicts()[0]["s"]
    )
    print(f"      DuckDB  空表 SUM(v) = {duck_empty!r}")
    print(f"      Polars  空表 sum(v) = {pol_empty!r}")


# ---------------------------------------------------------------- 场景 4


def scenario_count_distinct(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · COUNT(DISTINCT) 算不算 NULL？")

    duck = con.execute(
        "SELECT COUNT(DISTINCT v) AS n FROM (VALUES (1),(NULL),(NULL),(2)) t(v)"
    ).fetchone()[0]
    pol_nunique = pl.Series("v", [1, None, None, 2]).n_unique()
    pol_dropped = pl.Series("v", [1, None, None, 2]).drop_nulls().n_unique()

    print("  数据：[1, None, None, 2]")
    print(f"    DuckDB  COUNT(DISTINCT v)              → {duck}   （NULL 不算）")
    print(f"    Polars  n_unique()                     → {pol_nunique}   （**NULL 算进去了**）")
    print(f"    Polars  drop_nulls().n_unique()        → {pol_dropped}   （等价于 SQL）")
    print()
    print("  SQL 标准里 COUNT(DISTINCT) 忽略 NULL；Polars 的 n_unique() 把 NULL 当成一个值。")
    print("  这是**静默**的差异 —— 两边都能跑，都给一个数，但数不一样。")
    print()
    print("  对齐方式：Polars 侧用 `drop_nulls().n_unique()`，")
    print("  或者用 `pl.col('v').n_unique() - (pl.col('v').null_count() > 0).sum()`。")


# ---------------------------------------------------------------- 场景 5


def scenario_group_by_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · GROUP BY 里的 NULL 归到哪一组？")

    duck = con.execute(
        "SELECT grp, COUNT(*) AS n FROM edge GROUP BY grp ORDER BY grp NULLS FIRST"
    ).fetchall()
    pol = EDGE.group_by("grp").agg(pl.len().alias("n")).sort("grp", nulls_last=False).to_dicts()
    print("  DuckDB  GROUP BY grp：")
    for row in duck:
        print(f"    grp={row[0]!r}  n={row[1]}")
    print()
    print("  Polars  group_by('grp')：")
    for row in pol:
        print(f"    grp={row['grp']!r}  n={row['n']}")
    print()
    print("  两边一致：NULL 自成一组，不和其他值混在一起。")
    print(
        "  （这也是为什么上面 4 张 demo 里 group_by 之后要显式定序 —— "
        "NULL 组排在哪儿取决于各自的默认。)"
    )


# ---------------------------------------------------------------- 场景 6


def scenario_not_in_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · NOT IN 里有 NULL：DuckDB 返回空集，Polars 不是")

    duck_in = con.execute("SELECT id FROM edge WHERE k IN (1, NULL) ORDER BY id").fetchall()
    duck_not_in = con.execute("SELECT id FROM edge WHERE k NOT IN (1, NULL) ORDER BY id").fetchall()
    duck_not_in_plain = con.execute("SELECT id FROM edge WHERE k NOT IN (1) ORDER BY id").fetchall()

    pol_in = EDGE.filter(pl.col("k").is_in([1, None])).sort("id")["id"].to_list()
    pol_not_in = EDGE.filter(~pl.col("k").is_in([1, None])).sort("id")["id"].to_list()
    pol_not_in_plain = EDGE.filter(~pl.col("k").is_in([1])).sort("id")["id"].to_list()

    print(f"  {'写法':<44}{'DuckDB':<14}{'Polars'}")
    print("  " + "-" * 74)
    for label, a, b in [
        ("k IN (1, NULL)", duck_in, pol_in),
        ("k NOT IN (1, NULL)", duck_not_in, pol_not_in),
        ("k NOT IN (1)", duck_not_in_plain, pol_not_in_plain),
    ]:
        av = [r[0] for r in a] if a else []
        print(f"  {common.pad(label, 44)}{common.pad(str(av), 14)}{b}")
    print()
    print("  `NOT IN (1, NULL)` 在 SQL 里展开成 `k <> 1 AND k <> NULL`，")
    print("  第二个条件对任何行都是 UNKNOWN —— 于是**一行都不返回**。")
    print("  这就是三值逻辑最经典的坑：")
    print("    「查不在白名单里的」用 NOT IN，一旦白名单里有 NULL，整个查询静默返回空。")
    print()
    print("  Polars 的 `~is_in` 是集合取反的语义，不走三值逻辑，所以给了不同的结果。")
    print("  两边都**不报错** —— 这正是最危险的地方。")
    print()
    print("  对齐办法：")
    print("    · DuckDB 侧用 `NOT EXISTS (SELECT 1 FROM 白名单 WHERE k = 白名单.k)`，")
    print("      或者干脆先把白名单里的 NULL 过滤掉。")
    print("    · Polars 侧若想复刻 SQL 语义，得显式处理 NULL ——")
    print("      `is_in` 结果为 null 的那些行也要一并去掉。")


# ---------------------------------------------------------------- 场景 7


def scenario_division_by_zero(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 除以零：一个报错，一个给 inf")

    cases = [
        ("1 // 0（整数除）", "SELECT 1 // 0"),
        ("1 / 0（浮点除）", "SELECT 1 / 0"),
        ("TRY(1 // 0)", "SELECT TRY(1 // 0)"),
    ]
    print("  DuckDB：")
    for label, sql in cases:
        try:
            got = con.execute(sql).fetchone()[0]
            print(f"    {label:<22} → {got!r}")
        except Exception as exc:
            print(
                f"    {label:<22} → {type(exc).__name__}: {str(exc).strip().splitlines()[0][:70]}"
            )
    con.execute("SET error_on_division_by_zero=false")
    got = con.execute("SELECT 1 // 0").fetchone()[0]
    print(f"    {'SET error_on…=false 后':<22} → {got!r}")
    con.execute("SET error_on_division_by_zero=true")

    print()
    print("  Polars：")
    print(f"    {'1 // 0（整数除）':<22} → {pl.select(pl.lit(1) // pl.lit(0)).item()!r}")
    print(f"    {'1.0 / 0.0（浮点除）':<22} → {pl.select(pl.lit(1.0) / pl.lit(0.0)).item()!r}")
    print()
    print("  三种结果都存在：报错（DuckDB 默认）、NULL（两边都有办法拿到）、inf（浮点除）。")
    print("  DuckDB 的 `TRY(...)` 和 Polars 的默认行为都指向 NULL —— 这是最容易对齐的一种。")


# ---------------------------------------------------------------- 场景 8


def scenario_coalesce(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · COALESCE / IFNULL：名字对不上")

    duck = con.execute("SELECT COALESCE(NULL, 5), IFNULL(NULL, 5)").fetchone()
    try:
        con.execute("SELECT NVL(NULL, 5)").fetchone()
        nvl = "支持"
    except Exception:
        nvl = "**不支持**（DuckDB 2.0 没有这个函数）"
    pol_coalesce = pl.select(pl.coalesce([pl.lit(None), pl.lit(5)])).item()
    pol_fill = pl.select(pl.lit(None).fill_null(5)).item()

    print(f"  {'写法':<32}{'结果'}")
    print("  " + "-" * 52)
    print(f"  {common.pad('DuckDB  COALESCE(NULL, 5)', 32)}{duck[0]}")
    print(f"  {common.pad('DuckDB  IFNULL(NULL, 5)', 32)}{duck[1]}")
    print(f"  {common.pad('DuckDB  NVL(NULL, 5)', 32)}{nvl}")
    print(f"  {common.pad('Polars  coalesce([NULL, 5])', 32)}{pol_coalesce}")
    print(f"  {common.pad('Polars  lit(None).fill_null(5)', 32)}{pol_fill}")
    print()
    print("  DuckDB 有 COALESCE 和 IFNULL，**没有 NVL**（别按 Oracle 的习惯写）。")
    print("  Polars 对应的是 `coalesce(...)`（多个值）和 `fill_null(...)`（单列填充）。")
    print()
    print("  一个细节：Polars 的 fill_null 对「整列都是 NULL」的列，")
    print("  类型是推断出来的 —— 给 0 就变 Int64，给 0.0 就变 Float64，")
    print("  和 DuckDB 里列本身的类型是两回事。")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. `NULL = NULL` 是 UNKNOWN 不是 TRUE，两边一致；过滤时 UNKNOWN 和 FALSE 一样被丢掉。")
    print(
        "     找「null 安全相等」：DuckDB `IS NOT DISTINCT FROM`，Polars `(a==b).fill_null(True)`。"
    )
    print("  2. 连接键为 NULL 时**两边默认都不匹配**；DuckDB 用 `IS NOT DISTINCT FROM`，")
    print("     Polars 用 `nulls_equal=True`（2.0 之前叫 join_nulls，已被移除）。")
    print("  3. **SUM 全 NULL：DuckDB 给 NULL，Polars 给 0。** 空表也一样。")
    print("     这是本 demo 里唯一一个两边默认行为直接冲突的聚合函数。")
    print(
        "  4. `COUNT(DISTINCT)` 忽略 NULL，Polars 的 `n_unique()` 不忽略 —— "
        "两边都给一个数，但数不一样。"
    )
    print("  5. GROUP BY 里的 NULL 自成一组，两边一致。")
    print("  6. `NOT IN (1, NULL)` 在 SQL 里返回**空集**，Polars 的 `~is_in` 不这样 —— ")
    print("     「查不在白名单里的」这类查询，两边的写法必须显式对齐。")
    print("  7. 除零：DuckDB 默认报错（`TRY()` 或关掉开关可以拿到 NULL），")
    print("     Polars 的整数除给 NULL、浮点除给 inf。")
    print("  8. DuckDB 有 COALESCE / IFNULL，**没有 NVL**；Polars 对应 coalesce / fill_null。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="NULL 语义与三值逻辑：两个引擎的静默差异")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print()
    print("  左边这张表（6 行，NULL 都放在关键位置）：")
    common.print_frame(EDGE, "edge")

    con = connect()
    scenario_null_equality(con)
    scenario_join_null(con)
    scenario_agg_null(con)
    scenario_count_distinct(con)
    scenario_group_by_null(con)
    scenario_not_in_null(con)
    scenario_division_by_zero(con)
    scenario_coalesce(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
