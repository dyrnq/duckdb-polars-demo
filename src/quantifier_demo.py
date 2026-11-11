"""量词 ANY / ALL、标量子查询、LATERAL、UNNEST —— SQL 的一等公民，Polars 没有。

SQL 里有四件「一等公民」的事，在 Polars 的 DataFrame API 里**根本没有对应物**：

* **量词**：`x = ANY (子查询)` / `x > ALL (子查询)`；
* **相关标量子查询**：`SELECT (SELECT max(v) FROM s WHERE s.v < t.x) FROM t`；
* **LATERAL**：`FROM t, LATERAL (SELECT ... WHERE ... = t.x)`；
* **CROSS JOIN UNNEST**。

它们在 Polars 侧不是「难写」，是**没有等价物** —— 最接近的东西（`is_in`、
把子查询结果广播成标量、`group_by` + join、`explode()`）在**有 NULL 的时候语义不一样**。

这个 demo 的重点不是「Polars 弱」，而是**迁移时最容易踩的三个坑**：

1. `is_in` 只回答「在不在」，不回答 SQL 的 UNKNOWN —— `x = ANY(S)` 在 S 带 NULL 时
   是 UNKNOWN，`is_in` 给的是 FALSE；
2. `x > ALL(子查询)` 改写成 `x > 子查询.max()` —— 这个流传很广的做法
   **在子查询带 NULL 时会给出确定答案**，而 SQL 的答案是 UNKNOWN，
   两边对不上，而且 Polars 那边看着还挺像对的；
3. `explode()` 和 `UNNEST` 对空列表 / NULL 列表的处理不一样，
   而且 `with_columns(pl.col(...).explode())` 会**静默错位**。

用法：
    uv run python src/quantifier_demo.py
    uv run python src/quantifier_demo.py --rows 100000 --repeat 5
"""

from __future__ import annotations

import argparse
import bisect
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 数据

# 外层：被比较的那一列（x = NULL 那行是主角之一）
OUTER = pl.DataFrame({"x": [1, 2, 3, 5, None], "tag": ["甲", "乙", "丙", "丁", "空"]})

# 内层：量词作用的那个集合 —— **故意混进一个 NULL**
INNER = pl.DataFrame({"v": [2, None]})

# 干净版：内层没有 NULL
CLEAN_INNER = pl.DataFrame({"v": [2, 4]})

# 列表列：给 UNNEST / explode 用
LISTS = pl.DataFrame(
    {
        "id": [1, 2, 3, 4],
        "tags": [["a", "b"], ["c"], [], None],
        "other": [["x", "y", "z"], ["w"], [], None],
    },
    schema={"id": pl.Int64, "tags": pl.List(pl.String), "other": pl.List(pl.String)},
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def fmt(v: object) -> str:
    """把 SQL 的三值逻辑显示成人能读的样子。"""
    if v is None:
        return "NULL"
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    return str(v)


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, frame in [("t", OUTER), ("s", INNER), ("clean_s", CLEAN_INNER), ("lists", LISTS)]:
        con.register(f"frame_{name}", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame_{name}")
    return con


def rows_of(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    return con.sql(sql).fetchall()


# ---------------------------------------------------------------- Polars 侧的正确改写
#
# 这两个函数是本 demo 的「标准答案」：它们把 SQL 的三值逻辑老老实实实现出来，
# 后面用来跟 DuckDB 逐行对账。


def pol_eq_any(x: pl.Expr, sub: pl.Series) -> pl.Expr:
    """`x = ANY(S)` 的 Polars 改写。

    SQL 语义：
      · 命中任一非 NULL 元素      → TRUE
      · 没命中，但 S 里有 NULL    → NULL（UNKNOWN）
      · 没命中，且 S 里没有 NULL  → FALSE
      · S 为空                    → FALSE
    """
    if len(sub) == 0:
        return pl.lit(False)
    matched = x.is_in(sub.drop_nulls().implode())
    if sub.is_null().any():
        return pl.when(matched).then(pl.lit(True)).otherwise(pl.lit(None, dtype=pl.Boolean))
    return matched


def pol_gt_all(x: pl.Expr, sub: pl.Series) -> pl.Expr:
    """`x > ALL(S)` 的 Polars 改写。

    SQL 语义（三值逻辑里 FALSE 支配 UNKNOWN）：
      · 存在非 NULL 的 s ≥ x  → FALSE（有一个比较是 FALSE）
      · S 为空                 → TRUE（空集上的全称量词恒真）
      · S 里有 NULL            → UNKNOWN
      · 否则                   → TRUE
    """
    if len(sub) == 0:
        return pl.lit(True)
    if sub.is_null().all():
        return pl.lit(None, dtype=pl.Boolean)
    non_null = sub.drop_nulls()
    return (
        pl.when(x.is_null())
        .then(pl.lit(None, dtype=pl.Boolean))
        .when(x <= non_null.max())
        .then(pl.lit(False))
        .when(sub.is_null().any())
        .then(pl.lit(None, dtype=pl.Boolean))
        .otherwise(pl.lit(True))
    )


# ---------------------------------------------------------------- 1. 量词


def scenario_vocabulary(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 量词：SQL 的一等公民，六个形式")

    print("  量词就是把「对子集合里的每一个」写成一句话：")
    print()
    print("    x = ANY  (S)   ⇔  存在 s ∈ S，使 x = s")
    print("    x <> ANY (S)   ⇔  存在 s ∈ S，使 x <> s")
    print("    x = ALL  (S)   ⇔  对每个 s ∈ S，都有 x = s")
    print("    x <> ALL (S)   ⇔  对每个 s ∈ S，都有 x <> s")
    print("    x > ALL  (S)   ⇔  对每个 s ∈ S，都有 x > s")
    print("    x < ANY  (S)   ⇔  存在 s ∈ S，使 x < s")
    print()
    print("  外层 t：x = 1, 2, 3, 5, NULL")
    print("  内层 s：v = 2, NULL          ← 那个 NULL 是本 demo 的主角")
    print()

    quantifiers = [
        ("x = ANY (s)", "x = ANY(SELECT v FROM s)"),
        ("x <> ANY (s)", "x <> ANY(SELECT v FROM s)"),
        ("x = ALL (s)", "x = ALL(SELECT v FROM s)"),
        ("x <> ALL (s)", "x <> ALL(SELECT v FROM s)"),
        ("x > ALL (s)", "x > ALL(SELECT v FROM s)"),
        ("x < ANY (s)", "x < ANY(SELECT v FROM s)"),
    ]
    head = common.pad("量词", 16)
    for label in ("x=1", "x=2", "x=3", "x=5", "x=NULL"):
        head += common.pad(label, 8)
    print("  " + head)
    print("  " + "-" * 56)
    for label, expr in quantifiers:
        sql = f"SELECT x, {expr} FROM t ORDER BY x NULLS LAST"
        vals = [r[1] for r in rows_of(con, sql)]
        line = common.pad(label, 16)
        for v in vals:
            line += common.pad(fmt(v), 8)
        print("  " + line)

    print("\n  读这张表：")
    print("  · **命中就是 TRUE**：x=2 时 `x = ANY(s)` 是 TRUE（2 命中了），")
    print("    哪怕 s 里还有一个 NULL —— ANY 是「有一个成立就算成立」。")
    print("  · **没命中就是 NULL**：x=1、3、5 的 `= ANY` 全是 NULL ——")
    print("    因为「1 = 2」是 FALSE、「1 = NULL」是 UNKNOWN，")
    print("    ANY(FALSE, UNKNOWN) 还是 UNKNOWN。")
    print("  · **ALL 比 ANY 更容易变成 NULL**：`x = ALL(s)` 只有 x=2 那行是 TRUE，")
    print("    其余全是 NULL —— 只要 s 里有 NULL，ALL 就塌成 UNKNOWN。")

    print("\n  空集上的量词（SQL 标准的规定，不是 DuckDB 的怪癖）：")
    for label, sql in [
        ("1 = ANY(∅)", "SELECT 1 = ANY(SELECT v FROM s WHERE 1=0)"),
        ("1 = ALL(∅)", "SELECT 1 = ALL(SELECT v FROM s WHERE 1=0)"),
        ("1 > ALL(∅)", "SELECT 1 > ALL(SELECT v FROM s WHERE 1=0)"),
        ("1 > ANY(∅)", "SELECT 1 > ANY(SELECT v FROM s WHERE 1=0)"),
        ("NULL > ALL(∅)", "SELECT NULL > ALL(SELECT v FROM s WHERE 1=0)"),
    ]:
        got = rows_of(con, sql)[0][0]
        print(f"    {common.pad(label, 16)} -> {fmt(got)}")
    print("    → ANY 在空集上是 FALSE，ALL 在空集上是 TRUE；")
    print("      连 `NULL > ALL(∅)` 都是 TRUE —— 空集上的全称量词根本不看比较本身。")


# ---------------------------------------------------------------- 2. is_in 不是 ANY


def scenario_is_in(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · Polars 的 is_in 只回答「在不在」，不回答 UNKNOWN")

    sql = (
        "SELECT x, x = ANY(SELECT v FROM s) AS a, x <> ALL(SELECT v FROM s) AS b "
        "FROM t ORDER BY x NULLS LAST"
    )
    duck = rows_of(con, sql)

    pol = (
        OUTER.with_columns(
            pl.col("x").is_in(INNER["v"].implode()).alias("a"),
            (~pl.col("x").is_in(INNER["v"].implode())).alias("b"),
        )
        .select("x", "a", "b")
        .sort("x", nulls_last=True)
    )

    print(
        "  " + common.pad("x", 8) + common.pad("DuckDB = ANY", 16) + common.pad("Polars is_in", 16)
    )
    print("  " + "-" * 40)
    for d, p in zip(duck, pol.rows(), strict=True):
        flag = "  ← 不一样" if fmt(d[1]) != fmt(p[1]) else ""
        print(
            "  "
            + common.pad(fmt(d[0]), 8)
            + common.pad(fmt(d[1]), 16)
            + common.pad(fmt(p[1]), 16)
            + flag
        )

    print("\n  `<> ALL` 那一侧同样对不上：")
    print(
        "  "
        + common.pad("x", 8)
        + common.pad("DuckDB <> ALL", 16)
        + common.pad("Polars ~is_in", 16)
    )
    print("  " + "-" * 40)
    for d, p in zip(duck, pol.rows(), strict=True):
        print(
            "  " + common.pad(fmt(d[0]), 8) + common.pad(fmt(d[2]), 16) + common.pad(fmt(p[2]), 16)
        )

    print("\n→ 这不是「Polars 少了个功能」，是**两个引擎的答案模型不一样**：")
    print("  · DuckDB 走 SQL 标准的三值逻辑：TRUE / FALSE / **UNKNOWN**。")
    print("    上表里 x=1、3、5 的 `= ANY` 全是 UNKNOWN —— 因为 s 里有 NULL。")
    print("  · Polars 的 `is_in` 是**两值**的：它只问「在不在」，")
    print("    探针是 NULL 时给 NULL，**子集里有 NULL 一概不影响**。")
    print("    所以它把 UNKNOWN 直接压成了 FALSE。")
    print("  · 后果很具体：`WHERE x = ANY(s)` 在 DuckDB 里会**滤掉**那些 UNKNOWN 行，")
    print("    在 Polars 里 `filter(is_in(...))` 也会滤掉 FALSE 行 —— 这一处碰巧一样；")
    print("    但一旦这个判定进了 `SELECT` 列表（要的是布尔列，不是筛行），")
    print("    两边给的列就**逐行不同**。")
    print("  · → **迁移规则**：量词判定如果要留在结果里，")
    print("    DuckDB 侧先 `COALESCE(x = ANY(s), FALSE)` 把它压成两值，")
    print("    两边才对得上；要保留 UNKNOWN 就得自己维护三值（Polars 侧见场景 3）。")


# ---------------------------------------------------------------- 3. max/min 改写的坑


def scenario_max_min_rewrite(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 「改写成 max/min」这个流传很广的做法是有坑的")

    print("  常见的迁移建议：")
    print("    `x > ALL(子查询)`  ⟶  `x > (SELECT max(v) FROM 子查询)`")
    print("    `x = ALL(子查询)`  ⟶  `x = min(v) AND x = max(v)`")
    print("    `x = ANY(子查询)`  ⟶  `x IN (子查询)`")
    print()
    print("  这三条**只在子查询没有 NULL、且非空时成立**。带一个 NULL 就全塌：")
    print()

    sub = INNER["v"]
    sub_max = sub.max()
    sub_min = sub.min()

    cases = [
        (
            "x > ALL(s)，x = 3",
            "SELECT x > ALL(SELECT v FROM s) FROM t WHERE x = 3",
            lambda: OUTER.filter(pl.col("x") == 3).with_columns((pl.col("x") > sub_max).alias("r"))[
                "r"
            ][0],
        ),
        (
            "x = ALL(s)，x = 2",
            "SELECT x = ALL(SELECT v FROM s) FROM t WHERE x = 2",
            lambda: OUTER.filter(pl.col("x") == 2).with_columns(
                ((pl.col("x") == sub_min) & (pl.col("x") == sub_max)).alias("r")
            )["r"][0],
        ),
        (
            "x = ANY(s)，x = 1",
            "SELECT x = ANY(SELECT v FROM s) FROM t WHERE x = 1",
            lambda: OUTER.filter(pl.col("x") == 1).with_columns(
                pl.col("x").is_in(sub.implode()).alias("r")
            )["r"][0],
        ),
    ]
    print(
        "  "
        + common.pad("场景", 20)
        + common.pad("DuckDB", 12)
        + common.pad("Polars 直译", 14)
        + "对不对"
    )
    print("  " + "-" * 58)
    for label, sql, pol_fn in cases:
        duck_v = rows_of(con, sql)[0][0]
        pol_v = pol_fn()
        ok = fmt(duck_v) == fmt(pol_v)
        print(
            "  "
            + common.pad(label, 20)
            + common.pad(fmt(duck_v), 12)
            + common.pad(fmt(pol_v), 14)
            + ("✅" if ok else "❌ 不一致")
        )

    print()
    print("  三个错法各有各的错法：")
    print("  · `x > ALL` 直译成 `x > max()`：max **忽略 NULL**，")
    print("    所以 s=(2,NULL) 时 max=2，`3 > 2` 是 TRUE ——")
    print("    而 DuckDB 的答案是 UNKNOWN（因为 `3 > NULL` 是 UNKNOWN）。")
    print("    **一个确定答案换掉了一个 UNKNOWN**，两边从此对不上。")
    print("  · `x = ALL` 直译成 `x = min() AND x = max()`：s=(2,NULL) 时 min=max=2，")
    print("    `2 = 2 AND 2 = 2` 是 TRUE —— 而 DuckDB 的 `2 = ALL((2,NULL))` 是 UNKNOWN。")
    print("  · `x = ANY` 直译成 `is_in`：把 UNKNOWN 压成了 FALSE（场景 2 已经见过）。")

    print("\n  空集时又错一次 —— `ALL` 在空集上是 TRUE，`max(∅)` 是 NULL：")
    duck_empty = rows_of(con, "SELECT 1 > ALL(SELECT v FROM s WHERE 1=0)")[0][0]
    empty_max = pl.Series([], dtype=pl.Int64).max()
    pol_empty = OUTER.with_columns((pl.col("x") > pl.lit(empty_max, dtype=pl.Int64)).alias("r"))[
        "r"
    ][0]
    print(f"    DuckDB `1 > ALL(∅)`              -> {fmt(duck_empty)}")
    print(f"    Polars `1 > max(∅)`（max = {empty_max}）  -> {fmt(pol_empty)}   ❌ 不一致")

    print("\n  那怎么改才对？先补一条 SQL 三值逻辑的规矩：")
    print("    **AND 里面 FALSE 支配 UNKNOWN** —— FALSE ∧ UNKNOWN = FALSE。")
    print("  所以 `x > ALL(S)` 的答案是：")
    print()
    print("    存在非 NULL 的 s ≥ x  → FALSE（有一个比较是 FALSE，整句就 FALSE）")
    print("    S 为空                 → TRUE（空集上的全称量词恒真）")
    print("    S 里有 NULL（且没人 ≥ x）→ UNKNOWN")
    print("    否则                   → TRUE")
    print()
    print("  注意第二条：**「有 NULL 就一定是 UNKNOWN」是错的** ——")
    print("  如果同时还有一个 s ≥ x 让比较变成了 FALSE，结果是 FALSE，不是 UNKNOWN。")
    print("  上面 `x > ALL(s)，x = 1` 那行 DuckDB 给的就是 FALSE，不是 NULL。")
    print()
    print("  Polars 侧照着写（demo 里是 `pol_gt_all`）：")
    print("    pl.when(x.is_null())        -> NULL")
    print("    .when(x <= 非NULL的最大值)   -> FALSE")
    print("    .when(S 里有 NULL)          -> NULL")
    print("    .otherwise()                -> TRUE")

    print("\n  用正确改写再对一次账（左边 DuckDB，右边 Polars）：")
    for label, duck_sql, pol_expr in [
        (
            "x = ANY(s)",
            "SELECT x, x = ANY(SELECT v FROM s) AS v FROM t ORDER BY x NULLS LAST",
            pol_eq_any(pl.col("x"), sub),
        ),
        (
            "x > ALL(s)",
            "SELECT x, x > ALL(SELECT v FROM s) AS v FROM t ORDER BY x NULLS LAST",
            pol_gt_all(pl.col("x"), sub),
        ),
    ]:
        d = rows_of(con, duck_sql)
        p = (
            OUTER.with_columns(pol_expr.alias("v"))
            .select("x", "v")
            .sort("x", nulls_last=True)
            .rows()
        )
        same = all(fmt(a[1]) == fmt(b[1]) for a, b in zip(d, p, strict=True))
        print(f"    {common.pad(label, 14)} {'✅ 逐行一致' if same else '❌ 仍然不一致'}")
        for a, b in zip(d, p, strict=True):
            print(
                "      "
                + common.pad(f"x = {fmt(a[0])}", 14)
                + common.pad(f"DuckDB {fmt(a[1])}", 16)
                + f"Polars {fmt(b[1])}"
            )

    print("\n  干净子集（没有 NULL）时，粗改写是对的：")
    clean_sub = CLEAN_INNER["v"]
    d = rows_of(con, "SELECT x, x > ALL(SELECT v FROM clean_s) AS v FROM t ORDER BY x NULLS LAST")
    p = (
        OUTER.with_columns((pl.col("x") > clean_sub.max()).alias("v"))
        .select("x", "v")
        .sort("x", nulls_last=True)
        .rows()
    )
    same = all(fmt(a[1]) == fmt(b[1]) for a, b in zip(d, p, strict=True))
    print(f"    s=(2,4)：DuckDB `x > ALL` ↔ Polars `x > 4`  {'✅ 逐行一致' if same else '❌'}")
    print("    → 所以「max/min 改写」不是错的，是**有前提的**：")
    print("      前提是子查询不带 NULL 且非空。迁移前先确认这两条。")

    print("\n→ 小结（这一节的三条，迁移时直接照着做）：")
    print("  1. **量词判定要留在 SELECT 列表里 → 两边都压成两值**：")
    print("     DuckDB 侧 `COALESCE(x = ANY(s), FALSE)`，Polars 侧 `is_in`（本来就是两值）。")
    print("  2. **`x > ALL(s)` 的 max 改写只在 s 无 NULL 且非空时成立**；")
    print("     有 NULL 时要按三值逻辑分三种情况写：FALSE（有人 ≥ x）/ UNKNOWN / TRUE。")
    print("  3. **空集要单独处理**：SQL 的 ALL 在空集上是 TRUE，`max(∅)` 是 NULL。")


# ---------------------------------------------------------------- 4. 标量子查询


def scenario_scalar_subquery(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 标量子查询：相关的那一种，优化器会把它改写成 join")

    print("  不相关的标量子查询（每个外层行都拿到同一个值）：")
    rows = rows_of(con, "SELECT x, (SELECT max(v) FROM s) AS m FROM t ORDER BY x NULLS LAST")
    for r in rows:
        print(f"    x = {common.pad(fmt(r[0]), 8)} -> m = {r[1]}")
    print("    → DuckDB 只算一次；Polars 侧就是 `s['v'].max()` 广播成标量。")

    print("\n  **相关**标量子查询（内层引用外层的 x，每行结果不同）：")
    sql = "SELECT x, (SELECT max(v) FROM s WHERE s.v < t.x) AS m FROM t ORDER BY x NULLS LAST"
    rows = rows_of(con, sql)
    for r in rows:
        print(f"    x = {common.pad(fmt(r[0]), 8)} -> m = {r[1]}")

    print("\n  这条 SQL 在 DuckDB 里**不是**「外层每行跑一次内层」：")
    print("  EXPLAIN 出来的计划长这样（节选）：")
    plan = con.sql(f"EXPLAIN {sql}").fetchall()[0][1]
    lines = plan.splitlines()
    for i, line in enumerate(lines):
        if "CTE Name:" in line:
            print(f"    {line.strip()}")
        if "Nested Loop Join" in line:
            print(f"    {line.strip()}")
            for follow in lines[i + 1 : i + 6]:
                if "Join Type:" in follow or "Conditions:" in follow:
                    print(f"    {follow.strip()}")
    print()
    print("  → 优化器把相关标量子查询**改写成了一个 delim join**：")
    print("    ① 先建一个 `__duckdb_delim_N` 的 CTE，把外层的取值集合装进去；")
    print("    ② 拿它跟内层做一次 Nested Loop Join（条件就是那个相关条件 `v < x`）；")
    print("    ③ 再把结果聚合回标量。")
    print("    所以「相关子查询慢」是个过时经验 —— DuckDB 2.0 把它变成连接了。")

    print("\n  Polars 侧怎么写同一个查询：")
    grouped = (
        OUTER.join(INNER, how="cross")
        .filter(pl.col("v") < pl.col("x"))
        .group_by("x")
        .agg(pl.col("v").max().alias("m"))
    )
    pol_manual = OUTER.select("x").join(grouped, on="x", how="left").sort("x", nulls_last=True)
    print("    ① 手工 join（cross + filter + group_by，再 left join 回外层补 NULL）：")
    for r in pol_manual.rows():
        print(f"       x = {common.pad(fmt(r[0]), 8)} -> m = {r[1]}")

    print("\n    ② 如果查的是「不小于 x 的最近一行」，Polars 有专门的 `join_asof`：")
    asof = (
        OUTER.select("x")
        .sort("x")
        .join_asof(
            CLEAN_INNER.sort("v"),
            left_on="x",
            right_on="v",
            strategy="backward",
        )
        .sort("x", nulls_last=True)
    )
    for r in asof.rows():
        print(f"       x = {common.pad(fmt(r[0]), 8)} -> v = {r[1]}")
    print("       注意它匹配的是 `v <= x`（含等号），而 DuckDB 那个写的是 `v < x`（不含等号）。")
    print("       → **边界不一样**：x 正好等于某个 v 时，两边会对不上。")
    print("          DuckDB 侧写 `s.v <= t.x` 才和 join_asof 对齐。")

    print("\n  对账（左边 DuckDB，右边 Polars 的手工 join）：")
    duck_df = con.sql(sql).pl()
    ok, why = common.compare_frames(duck_df, pol_manual.select("x", "m"), ["x"])
    print(f"    相关标量子查询 ↔ cross join + group_by   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 5. LATERAL


def scenario_lateral(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · LATERAL：每行按另一列的值去查")

    print("  LATERAL 的意思是「右边这个子查询可以看到左边这一行的值」。")
    print("  它有两种口味，结果的行数完全不同：")
    print()

    print("  ① 子查询是聚合（count/max）—— 永远返回一行，外层行**不会消失**：")
    sql_agg = (
        "SELECT t.x, l.c FROM t, LATERAL (SELECT count(*) AS c FROM s WHERE s.v < t.x) l ORDER BY 1"
    )
    for r in rows_of(con, sql_agg):
        print(f"       x = {common.pad(fmt(r[0]), 8)} -> c = {r[1]}")
    print("       → x = NULL 那行拿到的是 **0**（不是 NULL）：")
    print("         `s.v < NULL` 没有一行为真，count 数出来是 0。")

    print("\n  ② 子查询不是聚合 —— 返回 0..N 行，外层行**会被丢掉**（像 INNER JOIN）：")
    sql_rows = "SELECT t.x, l.v FROM t, LATERAL (SELECT s.v FROM s WHERE s.v < t.x) l ORDER BY 1, 2"
    for r in rows_of(con, sql_rows):
        print(f"       x = {common.pad(fmt(r[0]), 8)} -> v = {r[1]}")
    print("       → x = 1 和 x = 2 没有匹配行，**这两行整个消失了**。")
    print("         这是 LATERAL 最容易吓人的地方：它到底是 LEFT 还是 INNER，")
    print("         取决于子查询返回几行。")

    print("\n  Polars 侧：**没有 LATERAL**。三种替代写法，各对应一个场景：")
    print()
    print("    ① `join_asof` —— 「取不小于/不大于的最近一行」这类形状，")
    print("       向量化、无 NULL 语义问题，是**首选**；")
    print("    ② `map_elements` / `map_batches` —— 逐行调 Python 函数，")
    print("       是 LATERAL 的**字面翻译**，能对上任何逻辑，代价是逐行进 Python；")
    print("    ③ 手工 join + group_by —— 把相关条件拆成普通连接，")
    print("       语义最容易对账，代价是连接本身。")

    print("\n  写法②长这样（每行拿 x 去 Python 有序列表里二分一次）：")
    vals = sorted(int(v) for v in INNER["v"].to_list() if v is not None)

    def lookup(x: int | None) -> int | None:
        if x is None:
            return None
        i = bisect.bisect_right(vals, x) - 1
        return vals[i] if i >= 0 else None

    pol_lateral = (
        OUTER.with_columns(pl.col("x").map_elements(lookup, return_dtype=pl.Int64).alias("m"))
        .select("x", "m")
        .sort("x", nulls_last=True)
    )
    for r in pol_lateral.rows():
        print(f"       x = {common.pad(fmt(r[0]), 8)} -> m = {r[1]}")
    print("       → 注意这里的边界：`bisect_right` 找的是 `v <= x`，")
    print("         要 `v < x` 得用 `bisect_left`。DuckDB 那条写的是 `<`。")

    print("\n  对账（左边 DuckDB 的 LATERAL，右边 Polars 的手工 join）：")
    duck_lateral = con.sql(
        "SELECT t.x AS x, l.c AS c FROM t, "
        "LATERAL (SELECT count(*) AS c FROM s WHERE s.v < t.x) l ORDER BY 1"
    ).pl()
    grouped = (
        OUTER.join(INNER, how="cross")
        .filter(pl.col("v") < pl.col("x"))
        .group_by("x")
        .agg(pl.len().alias("c"))
    )
    pol_manual = (
        OUTER.select("x")
        .join(grouped, on="x", how="left")
        .with_columns(pl.col("c").fill_null(0))
        .sort("x", nulls_last=True)
    )
    ok, why = common.compare_frames(duck_lateral, pol_manual.select("x", "c"), ["x"])
    print(f"    LATERAL(count) ↔ cross join + group_by   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 6. UNNEST


def scenario_unnest(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · CROSS JOIN UNNEST ↔ explode()")

    print("  lists 表：")
    print("    id=1  tags=['a','b']      other=['x','y','z']")
    print("    id=2  tags=['c']          other=['w']")
    print("    id=3  tags=[]             other=[]          ← 空列表")
    print("    id=4  tags=NULL           other=NULL        ← NULL 列表")
    print()

    print("  DuckDB：`UNNEST` 在 SELECT 列表里（也是 FROM 里最常见的形式）：")
    for r in rows_of(con, "SELECT id, unnest(tags) AS t FROM lists ORDER BY id, t"):
        print(f"    id = {common.pad(str(r[0]), 8)} -> t = {r[1]}")
    print("    → **id=3（空列表）和 id=4（NULL 列表）两行都消失了。**")

    print("\n  DuckDB：SELECT 列表里写**两个** UNNEST —— 它们是**并排拉链**，不是笛卡尔积：")
    for r in rows_of(
        con, "SELECT id, unnest(tags) AS a, unnest(other) AS b FROM lists ORDER BY id"
    ):
        print(f"    id = {common.pad(str(r[0]), 8)} -> a = {common.pad(str(r[1]), 8)} b = {r[2]}")
    print("    → id=1 的 tags 有 2 个、other 有 3 个：**按短的那个补 NULL**，")
    print("      拉成 3 行。DuckDB 这里不是「两个都展开」= 6 行。")

    print("\n  Polars 侧：`df.explode()`")
    for r in LISTS.explode("tags").sort("id").rows():
        print(f"    id = {common.pad(str(r[0]), 8)} -> t = {r[1]}")
    print("    → id=3（空列表）**同样消失**（两边一致 ✅）；")
    print("      但 id=4（NULL 列表）**留下来了，值是 NULL** —— DuckDB 那边这行没了 ❌")

    print("\n  Polars 侧：`with_columns(pl.col('tags').explode())` —— **这是个陷阱**")
    exploded = LISTS.with_columns(pl.col("tags").explode())
    print(
        f"    explode 之后的列本身：{LISTS.select(pl.col('tags').explode()).to_series().to_list()}"
    )
    for r in exploded.rows():
        print(f"    id = {common.pad(str(r[0]), 8)} -> tags = {r[1]}")
    print("    → 展开后的列有 4 个值（['a','b','c',None]），可是**行数还是 4**：")
    print("      Polars 把新列**按位置**贴回原表，于是 id=2 拿到了 'b'（其实是 id=1 的第二个值）！")
    print("      **不报错、不警告，只是数据悄悄错位了。**")
    print("      → 展开改变行数时，必须用 `df.explode(...)`（或 `.select(...)`），")
    print("        **绝不能塞进 `with_columns`**。")

    print("\n  对账：")
    duck_unnest = con.sql("SELECT id, unnest(tags) AS t FROM lists ORDER BY id, t").pl()
    pol_explode = LISTS.explode("tags").select("id", pl.col("tags").alias("t")).sort("id")
    ok, why = common.compare_frames(duck_unnest, pol_explode, ["id", "t"])
    print(f"    UNNEST ↔ explode()   {'✅' if ok else '❌'} {why}")
    print("    → 唯一的不一致就是 NULL 列表那一行（DuckDB 丢行，Polars 留 NULL）。")
    print("      迁移规则：Polars 侧在 explode 前先把 NULL 列表过滤掉 ——")
    print("      `df.filter(pl.col('tags').is_not_null()).explode('tags')`。")

    print("\n  顺带说一句：`UNNEST` 也能写在 FROM 里（等价于 CROSS JOIN LATERAL UNNEST）：")
    for r in rows_of(con, "SELECT l.id, s.t FROM lists l, UNNEST(l.tags) AS s(t) ORDER BY 1"):
        print(f"    id = {common.pad(str(r[0]), 8)} -> t = {r[1]}")
    print("    → 行数与 SELECT 列表里的 UNNEST 一样（空列表 / NULL 列表都丢行）。")


# ---------------------------------------------------------------- 7. 代价


def scenario_cost(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 代价：同一个「每行查一次」，六种写法各要多少时间")

    print(f"数据：外层 {rows:,} 行（x = 0..{rows - 1}），内层 1,000 行（v = 0, 13, 26, …）")
    print("任务：每行 x 算出 `max(v) WHERE v <= x` —— 六种写法做的是**同一件事**")
    print(f"机器：{common.describe_machine()}")
    print()

    tmpdir = Path(tempfile.mkdtemp(prefix="quantifier_demo_"))
    outer_pq = tmpdir / "outer.parquet"
    inner_pq = tmpdir / "inner.parquet"
    con.execute(
        "COPY (SELECT i AS x FROM range(?) t(i)) TO ? (FORMAT PARQUET)", [rows, str(outer_pq)]
    )
    con.execute(
        "COPY (SELECT (i * 13) % 1000 AS v FROM range(1000) t(i)) TO ? (FORMAT PARQUET)",
        [str(inner_pq)],
    )
    con.execute("CREATE OR REPLACE TABLE big_t AS SELECT * FROM read_parquet(?)", [str(outer_pq)])
    con.execute("CREATE OR REPLACE TABLE big_s AS SELECT * FROM read_parquet(?)", [str(inner_pq)])
    pol_outer = pl.read_parquet(outer_pq)
    pol_inner = pl.read_parquet(inner_pq)

    vals = sorted(int(v) for v in pol_inner["v"].to_list() if v is not None)

    def lookup(x: int) -> int | None:
        i = bisect.bisect_right(vals, x) - 1
        return vals[i] if i >= 0 else None

    def duck_correlated() -> int:
        return int(
            con.sql(
                "SELECT count((SELECT max(v) FROM big_s WHERE big_s.v <= big_t.x)) FROM big_t"
            ).fetchall()[0][0]
        )

    def duck_lateral() -> int:
        return int(
            con.sql(
                "SELECT count(l.m) FROM big_t, LATERAL "
                "(SELECT max(v) AS m FROM big_s WHERE big_s.v <= big_t.x) l"
            ).fetchall()[0][0]
        )

    def duck_join() -> int:
        return int(
            con.sql(
                "SELECT count(m) FROM big_t LEFT JOIN (SELECT big_t.x AS x, max(big_s.v) AS m "
                "FROM big_t JOIN big_s ON big_s.v <= big_t.x GROUP BY big_t.x) g ON g.x = big_t.x"
            ).fetchall()[0][0]
        )

    def pol_cross() -> int:
        return (
            pol_outer.join(pol_inner, how="cross")
            .filter(pl.col("v") <= pl.col("x"))
            .group_by("x")
            .agg(pl.col("v").max().alias("m"))
            .height
        )

    def pol_asof() -> int:
        return (
            pol_outer.sort("x")
            .join_asof(pol_inner.sort("v"), left_on="x", right_on="v", strategy="backward")
            .height
        )

    def pol_map() -> int:
        return pol_outer.with_columns(
            pl.col("x").map_elements(lookup, return_dtype=pl.Int64).alias("m")
        ).height

    results = [
        ("DuckDB  相关标量子查询", duck_correlated),
        ("DuckDB  LATERAL", duck_lateral),
        ("DuckDB  手工 join + group_by", duck_join),
        ("Polars  cross join + group_by", pol_cross),
        ("Polars  join_asof", pol_asof),
        ("Polars  map_elements（Python 逐行）", pol_map),
    ]

    print(
        "  "
        + common.pad("写法", 34)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
    )
    print("  " + "-" * 60)
    for label, fn in results:
        fn()
        times: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        print(
            "  "
            + common.pad(label, 34)
            + common.pad_left(f"{statistics.median(times) * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
        )

    print("\n  口径：两个 parquet 在计时前已物化 —— DuckDB 侧是内存表 big_t / big_s，")
    print("  Polars 侧是内存里的 DataFrame，所以计时**不含读盘**。")
    print("  六行做的是**同一件事**：每行 x 算出 max(v) where v <= x，")
    print("  用的也是同一个不等号（<=），所以结果逐行相同（下面单独对账）。")
    print("  内层表固定 1,000 行 —— 这不是「偷懒」，是因为这个查询的代价")
    print("  主要来自**范围连接**（每行 x 都要看一遍 v <= x 的候选），")
    print("  内层越大越接近平方级，两边都会慢得没法看。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")

    print("\n  六种写法的结果对账（都是 max(v) where v <= x）：")
    duck_res = con.sql(
        "SELECT x, (SELECT max(v) FROM big_s WHERE big_s.v <= big_t.x) AS m FROM big_t ORDER BY x"
    ).pl()
    cross_res = (
        pol_outer.join(pol_inner, how="cross")
        .filter(pl.col("v") <= pl.col("x"))
        .group_by("x")
        .agg(pl.col("v").max().alias("m"))
    )
    pol_cross_res = pol_outer.select("x").join(cross_res, on="x", how="left").sort("x")
    pol_asof_res = (
        pol_outer.sort("x")
        .join_asof(pol_inner.sort("v"), left_on="x", right_on="v", strategy="backward")
        .rename({"v": "m"})
        .sort("x")
    )
    pol_map_res = pol_outer.with_columns(
        pl.col("x").map_elements(lookup, return_dtype=pl.Int64).alias("m")
    ).sort("x")
    for label, frame in [
        ("Polars  cross join + group_by", pol_cross_res),
        ("Polars  join_asof", pol_asof_res),
        ("Polars  map_elements", pol_map_res),
    ]:
        ok, why = common.compare_frames(duck_res, frame.select("x", "m"), ["x"])
        print(f"    {common.pad(label, 32)} {'✅' if ok else '❌'} {why}")

    print("\n解读：")
    print("  · **DuckDB 的相关标量子查询并不慢** —— 优化器改写成 delim join 之后，")
    print("    它比「手工把相关条件拆成普通 join」**快好几倍**：")
    print("    拆开写的那条要先物化整个范围连接的结果，再聚合回去。")
    print("    → 迁移时**别把相关子查询手工拆开**，DuckDB 那边留着原样最好。")
    print("  · **LATERAL 和相关标量子查询基本同价**（DuckDB 把 LATERAL 也改写成连接了），")
    print("    所以「LATERAL 慢」也是个过时经验。")
    print("  · **Polars 的 cross join 走的是同一条路**（范围连接），")
    print("    代价是 DuckDB 手写 join 的一半左右 —— 因为 Polars 不用把结果再聚合回标量；")
    print("    但 `join_asof` 是**另一个算法**：它是排序归并，线性，")
    print("    所以快了**两个数量级以上** —— 代价是它只能算「最近的一行」，")
    print("    而且匹配的是 `<=`（DuckDB 那边写 `<` 的话边界对不上）。")
    print("  · **`map_elements` 是 LATERAL 的字面翻译**，慢在「每行进一次 Python」；")
    print("    但只要你的查找逻辑是「对有序数组二分」，它其实很划算 ——")
    print("    比 DuckDB 的范围连接快得多。")
    print("  · → 落到实践：**先看能不能改写成 join_asof / 排序归并**（最省），")
    print("    不行再手工 join + group_by（语义最稳），")
    print("    最后才是逐行进 Python（最灵活、最慢）。")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB 与 Polars 的量词/子查询/LATERAL/UNNEST 差异演示"
    )
    parser.add_argument("--rows", type=int, default=100_000, help="代价场景的外层行数")
    parser.add_argument("--repeat", type=int, default=5, help="代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_vocabulary(con)
        scenario_is_in(con)
        scenario_max_min_rewrite(con)
        scenario_scalar_subquery(con)
        scenario_lateral(con)
        scenario_unnest(con)
        scenario_cost(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
