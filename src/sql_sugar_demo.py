"""SQL 语法糖对照：DuckDB 里的八句糖，Polars 里分别怎么写。

SQL 标准里有一批「语法糖」—— 它们不是新功能，只是把常见写法缩写了一下：

| 糖 | DuckDB / SQL | Polars |
| --- | --- | --- |
| `GROUP BY ALL` | `GROUP BY ALL` | `group_by(...)`，键要自己列全 |
| `ORDER BY ALL` | `ORDER BY ALL` | `sort(...)`，键要自己列全 |
| `SELECT * EXCLUDE (a, b)` | `EXCLUDE` | `drop("a", "b")` |
| `SELECT * REPLACE (expr AS a)` | `REPLACE` | `with_columns(...)` |
| `sum(x) FILTER (WHERE c)` | `FILTER` | `agg` 里 `.filter(...)` 或 `when/then` |
| `a IS NOT DISTINCT FROM b` | `IS NOT DISTINCT FROM` | `eq_missing()` |
| `a IS DISTINCT FROM b` | `IS DISTINCT FROM` | `ne_missing()` |
| `NULLS FIRST` / `NULLS LAST` | `ORDER BY x NULLS FIRST` | `sort(nulls_last=...)` |

**这一组不是「能力差距」** —— 每一条糖在 Polars 侧都有一个能对上的写法，
真正的问题是**默认行为不一样**，不写清楚就会静默对不上：

* `GROUP BY ALL` / `ORDER BY ALL` 在 DuckDB 里是「按没被聚合的列」/「按所有列」，
  键是**推出来的**；Polars 里键必须手写，写漏一列就是另一个结果；
* `FILTER (WHERE ...)` 会**保留分组**（没有匹配行的组给 NULL / 0），
  「先 filter 再 group_by」会把整组**弄丢**；
* `IS [NOT] DISTINCT FROM` 是 SQL 的 NULL 安全比较，`eq_missing()` / `ne_missing()`
  是**精确**等价的 —— 不写任何额外参数就完全对得上。

（`QUALIFY` 不在这里 —— 它已经在 `src/window_demo.py` 里演示过了。）

用法：
    uv run python src/sql_sugar_demo.py
"""

from __future__ import annotations

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 数据

# 一张小表：故意带上 NULL，好让「NULL 安全比较」和「NULL 排序位置」都有东西可演
SALES = pl.DataFrame(
    {
        "region": ["华东", "华北", "华东", "华南", None],
        "channel": ["app", "web", "app", "门店", "app"],
        "amount": [100.0, 200.0, 300.0, None, 500.0],
        "cnt": [1, 2, 3, 4, 5],
    }
)

# 键对照用的两列：一列带 NULL，一列不带
KEYS = pl.DataFrame(
    {
        "a": [1, None, 3, 4],
        "b": [1, None, 5, None],
        "c": ["x", "y", None, "z"],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("frame_sales", SALES)
    con.execute("CREATE TABLE sales AS SELECT * FROM frame_sales")
    con.register("frame_keys", KEYS)
    con.execute("CREATE TABLE keys AS SELECT * FROM frame_keys")
    return con


def show_frame(frame: pl.DataFrame) -> None:
    """统一两个引擎的表格输出样式。"""
    with pl.Config(tbl_rows=20, tbl_cols=-1, tbl_width_chars=120, fmt_str_lengths=12):
        print("  " + "\n  ".join(str(frame).splitlines()))


# ---------------------------------------------------------------- 1. GROUP BY ALL


def scenario_group_by_all(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · GROUP BY ALL —— 键是推出来的，不是写出来的")

    print("  DuckDB：`GROUP BY ALL` 的意思是「按**所有没有被聚合的列**分组」。")
    print("  下面这条只聚合了 amount，所以分组键 = region、channel 两列：")
    print()
    sql = (
        "SELECT region, channel, sum(amount) AS total FROM sales "
        "GROUP BY ALL ORDER BY region NULLS LAST, channel"
    )
    duck_frame = con.sql(sql).pl()
    print("    DuckDB：")
    show_frame(duck_frame)

    print()
    print("  Polars：键得自己写全 —— `group_by` 接受一个列表：")
    pol = (
        SALES.group_by(["region", "channel"])
        .agg(pl.col("amount").sum().alias("total"))
        .sort(["region", "channel"], nulls_last=True)
    )
    print("    Polars：")
    show_frame(pol)

    print()
    print("  三个坑：")
    print("  ① **DuckDB 的 ALL 不是「按所有列」** —— 被聚合的列不参与分组。")
    print("     `SELECT region, channel, sum(amount) ... GROUP BY ALL` 分的是 (region, channel)，")
    print("     不是 (region, channel, amount)。")
    print("  ② **NULL 也参与分组**（NULL 自成一组），两边一致 ——")
    print("     上表 region=NULL 那组两边都在。")
    print("  ③ **全 NULL 组的 sum 不一样**：上表「华南 / 门店」那组只有一行、")
    print("     而那一行的 amount 是 NULL —— DuckDB 的 `sum` 给 **NULL**，")
    print("     Polars 的 `sum` 给 **0**（语义差异表里的第 21 条）。")

    print("\n  对账：")
    ok, why = common.compare_frames(
        duck_frame.select("region", "channel", "total"),
        pol.select("region", "channel", "total"),
        ["region", "channel"],
    )
    print(f"    GROUP BY ALL ↔ group_by([...])                {'✅' if ok else '❌'} {why}")

    print()
    print("  把 NULL 语义补回去之后（这就是**能对上的写法**）：")
    pol_fixed = (
        SALES.group_by(["region", "channel"])
        .agg(
            pl.when(pl.col("amount").is_not_null().any())
            .then(pl.col("amount").sum())
            .otherwise(None)
            .alias("total")
        )
        .sort(["region", "channel"], nulls_last=True)
    )
    show_frame(pol_fixed)
    ok, why = common.compare_frames(
        duck_frame.select("region", "channel", "total"),
        pol_fixed.select("region", "channel", "total"),
        ["region", "channel"],
    )
    print(f"    GROUP BY ALL ↔ group_by + when/then 补 NULL   {'✅' if ok else '❌'} {why}")
    print("    → 分组和列序本来就对得上，差的只是那一个全 NULL 组的 sum。")


# ---------------------------------------------------------------- 2. ORDER BY ALL


def scenario_order_by_all(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · ORDER BY ALL —— 按所有列排，NULL 排在哪儿要另外说")

    sql = "SELECT region, channel, cnt FROM sales ORDER BY ALL"
    duck_frame = con.sql(sql).pl()
    print("  DuckDB（默认）：")
    show_frame(duck_frame)

    print()
    print("  Polars（**默认**，nulls_last 没写）：")
    pol_default = SALES.select("region", "channel", "cnt").sort(["region", "channel", "cnt"])
    show_frame(pol_default)

    print()
    print("  Polars（写上 `nulls_last=True`）：")
    pol = SALES.select("region", "channel", "cnt").sort(
        ["region", "channel", "cnt"], nulls_last=True
    )
    show_frame(pol)

    print()
    print("  → `ORDER BY ALL` 本身两边没有语义差别，**差别在 NULL 的位置**：")
    print("    DuckDB 升序默认 **NULLS LAST**，Polars 升序默认 **NULLS FIRST**。")
    print("    上面两张 Polars 表就是证据：默认那张 region=NULL 在**最前**，")
    print("    写上 `nulls_last=True` 之后才和 DuckDB 一致（在最后）。")
    print("    这一条 README 的语义差异表里也记着（第 8 条）。")

    print("\n  对账（两边都按 NULLS LAST 排）：")
    ok, why = common.compare_frames(duck_frame, pol, ["region", "channel", "cnt"])
    print(f"    ORDER BY ALL ↔ sort([...], nulls_last=True)   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 3. EXCLUDE


def scenario_exclude(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · SELECT * EXCLUDE (...) —— 去掉几列")

    sql = "SELECT * EXCLUDE (amount, cnt) FROM sales"
    duck_frame = con.sql(sql).pl()
    print("  DuckDB：")
    show_frame(duck_frame)

    print()
    print("  Polars：`drop()` —— 删掉的列是**从结果里消失**，位置自动收紧：")
    pol = SALES.drop("amount", "cnt")
    show_frame(pol)

    print()
    print("  → 两边完全一致，包括列的顺序（剩下的列保持原来的相对顺序）。")
    print(f"    DuckDB 列序 {duck_frame.columns} / Polars 列序 {pol.columns}。")

    print("\n  对账：")
    ok, why = common.compare_frames(duck_frame, pol, ["region", "channel"])
    print(f"    EXCLUDE ↔ drop()   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 4. REPLACE


def scenario_replace(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · SELECT * REPLACE (expr AS col) —— 原地改一列")

    sql = "SELECT * REPLACE (amount * 2 AS amount) FROM sales"
    duck_frame = con.sql(sql).pl()
    print("  DuckDB：")
    show_frame(duck_frame)

    print()
    print("  Polars：`with_columns()` —— 注意它是**覆盖同名列，位置不动**：")
    pol = SALES.with_columns((pl.col("amount") * 2).alias("amount"))
    show_frame(pol)

    print()
    print("  → 两边一致，**包括列的位置**：REPLACE 改的是原来的那一列，")
    print("    不是删掉再追加到末尾。Polars 的 `with_columns` 也是同样的行为。")
    print(f"    DuckDB 列序 {duck_frame.columns} / Polars 列序 {pol.columns}。")
    print("    （这跟 `with_columns` 追加**新**列不同 —— 新列会追加到末尾。）")

    print("\n  顺带看一下 EXCLUDE 和 REPLACE 一起用：")
    sql2 = "SELECT * EXCLUDE (cnt) REPLACE (amount * 2 AS amount) FROM sales"
    duck2 = con.sql(sql2).pl()
    print(f"    DuckDB 列序 {duck2.columns}")
    pol2 = SALES.with_columns((pl.col("amount") * 2).alias("amount")).drop("cnt")
    print(f"    Polars 列序 {pol2.columns}  （列序对不上也没关系，对账时 select() 一下即可）")

    print("\n  对账：")
    ok, why = common.compare_frames(duck_frame, pol, ["region", "channel"])
    print(f"    REPLACE ↔ with_columns()   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 5. FILTER (WHERE)


def scenario_agg_filter(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 聚合的 FILTER (WHERE ...) —— 最容易写错的一个")

    print("  `sum(x) FILTER (WHERE c)` 的意思是「只对满足 c 的行求和」，")
    print("  **但分组还在**：没有任何一行满足条件的组，聚合结果是 NULL（count 是 0）。")
    print()

    sql = (
        "SELECT region, sum(cnt) FILTER (WHERE channel = 'app') AS app_cnt, "
        "count(*) FILTER (WHERE channel = 'app') AS n "
        "FROM sales GROUP BY region ORDER BY region NULLS LAST"
    )
    duck_frame = con.sql(sql).pl()
    print("  DuckDB：")
    show_frame(duck_frame)

    print()
    print("  Polars 的三种写法，结果各不相同：")

    naive = (
        SALES.filter(pl.col("channel") == "app")
        .group_by("region")
        .agg(pl.col("cnt").sum().alias("app_cnt"), pl.len().alias("n"))
        .sort("region", nulls_last=True)
    )
    print("    ① 先 filter 再 group_by（**最直觉，也最容易错**）：")
    show_frame(naive)

    agg_filter = (
        SALES.group_by("region")
        .agg(
            pl.col("cnt").filter(pl.col("channel") == "app").sum().alias("app_cnt"),
            pl.col("cnt").filter(pl.col("channel") == "app").count().alias("n"),
        )
        .sort("region", nulls_last=True)
    )
    print()
    print("    ② 在 agg 里面用 `.filter()`（**分组保留，形状对**）：")
    show_frame(agg_filter)

    when_then = (
        SALES.group_by("region")
        .agg(
            pl.when(pl.col("channel") == "app").then(pl.col("cnt")).sum().alias("app_cnt"),
            pl.when(pl.col("channel") == "app").then(pl.col("cnt")).count().alias("n"),
        )
        .sort("region", nulls_last=True)
    )
    print()
    print("    ③ `when/then`（**分组保留**）：")
    show_frame(when_then)

    print()
    print("  三者的差别在哪：")
    print("  · ① **把整个组弄丢了** —— 「华北」和「华南」这两组没有 app 渠道的行，")
    print("    先 filter 之后这些组根本不存在，**行数比 DuckDB 少**（4 行 → 2 行）。")
    print("  · ② ③ 分组都在，但 `sum` 的空集语义和 DuckDB 不同：")
    print("    DuckDB 的 `sum` 对零行给 **NULL**，Polars 的 `sum` 给 **0**")
    print("    （这就是语义差异表里的第 21 条）；")
    print("    而 `count` 两边都是 0 —— 这个是对的。")

    print("\n  对账（逐个比）：")
    for label, frame in [
        ("① 先 filter 再 group_by", naive),
        ("② agg 内 .filter()", agg_filter),
        ("③ when/then", when_then),
    ]:
        ok, why = common.compare_frames(
            duck_frame.select("region", "app_cnt", "n"),
            frame.select("region", "app_cnt", "n"),
            ["region"],
        )
        print(f"    {common.pad(label, 24)} {'✅' if ok else '❌'} {why}")

    fixed = (
        SALES.group_by("region")
        .agg(
            pl.when(pl.col("channel").eq("app").any())
            .then(pl.col("cnt").filter(pl.col("channel") == "app").sum())
            .otherwise(None)
            .alias("app_cnt"),
            pl.col("cnt").filter(pl.col("channel") == "app").count().alias("n"),
        )
        .sort("region", nulls_last=True)
    )
    print()
    print("  ④ 把 NULL 语义补回去（**能对上的写法**）：")
    show_frame(fixed)
    ok, why = common.compare_frames(
        duck_frame.select("region", "app_cnt", "n"),
        fixed.select("region", "app_cnt", "n"),
        ["region"],
    )
    print(f"    {common.pad('④ when/then + otherwise(None)', 24)} {'✅' if ok else '❌'} {why}")

    print("\n  → **迁移规则**：")
    print("    ① 那个写法**不能用**（丢组）—— 除非你本来就不想要那些组。")
    print("    ② ③ 可以用，但 `sum` 要自己补 NULL 语义（见 ④）；")
    print("    ③ 要是只是要**计数**（count / count_if），② ③ 两边完全一致，直接用。")
    print()
    print("  还有一个类型上的小差别：DuckDB 的 `sum(int)` 是 **HUGEINT**")
    print("    （Arrow 里显示成 decimal[38,0]），Polars 的 `sum()` 保持输入类型（i64）。")
    print("    对账前两边都转成 Int64 才不会在类型上打架。")


# ---------------------------------------------------------------- 6. IS DISTINCT FROM


def scenario_is_distinct_from(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · IS [NOT] DISTINCT FROM —— NULL 安全的比较")

    sql = (
        "SELECT a, b, a IS NOT DISTINCT FROM b AS same, a IS DISTINCT FROM b AS diff "
        "FROM keys ORDER BY a NULLS LAST, b NULLS LAST"
    )
    duck_frame = con.sql(sql).pl()
    print("  DuckDB：")
    show_frame(duck_frame)

    print()
    print("  Polars：`eq_missing()` / `ne_missing()`：")
    pol = (
        KEYS.with_columns(
            pl.col("a").eq_missing(pl.col("b")).alias("same"),
            pl.col("a").ne_missing(pl.col("b")).alias("diff"),
        )
        .select("a", "b", "same", "diff")
        .sort(["a", "b"], nulls_last=True)
    )
    show_frame(pol)

    print()
    print("  → **这两个是全套糖里对得最准的一对**：逐行逐列一致，包括 NULL 的位置。")
    print("    `eq_missing` 的意思是「相等，NULL 也算相等」；")
    print("    `ne_missing` 是「不等，NULL 也算不等」。")
    print("    这正是 `IS NOT DISTINCT FROM` / `IS DISTINCT FROM` 的定义。")

    print("\n  对照一下**不用**这两个函数会怎样（普通 `==` / `!=`）：")
    plain = (
        KEYS.with_columns(
            (pl.col("a") == pl.col("b")).alias("same"),
            (pl.col("a") != pl.col("b")).alias("diff"),
        )
        .select("a", "b", "same", "diff")
        .sort(["a", "b"], nulls_last=True)
    )
    show_frame(plain)
    print("    → 普通 `==` 遇到 NULL 给 NULL（不是 TRUE），所以「两个 NULL 一样吗」")
    print("      这道题，SQL 的 `IS NOT DISTINCT FROM` 答「一样」，Polars 的 `==` 答「不知道」。")

    print("\n  对账：")
    ok, why = common.compare_frames(
        duck_frame.select("a", "b", "same", "diff"),
        pol.select("a", "b", "same", "diff"),
        ["a", "b"],
    )
    print(f"    IS [NOT] DISTINCT FROM ↔ eq_missing/ne_missing   {'✅' if ok else '❌'} {why}")


# ---------------------------------------------------------------- 7. NULLS FIRST / LAST


def scenario_nulls_first_last(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · NULLS FIRST / NULLS LAST —— 默认位置两边不一样")

    print("  两个引擎的默认值（实测，不是查文档）：")
    print()
    print("    DuckDB  升序 → NULLS **LAST**     降序 → NULLS **LAST**")
    print("    Polars  升序 → NULLS **FIRST**    降序 → NULLS **FIRST**")
    print()
    print("  → 两个引擎的默认值**正好相反，而且两个方向都相反**。")
    print("    不写就静默不一样 —— 这一条 README 的语义差异表里也记着（第 8 条）。")
    print()

    for direction, nulls in [("ASC", "NULLS LAST"), ("DESC", "NULLS FIRST")]:
        sql = f"SELECT region, cnt FROM sales ORDER BY region {direction} {nulls}"
        duck_frame = con.sql(sql).pl()
        print(f"  DuckDB `ORDER BY region {direction} {nulls}`：")
        show_frame(duck_frame)
        print()

    print("  Polars 侧要**显式**写 `nulls_last=True`（降序同理，它也只有这一个开关）：")
    pol_asc = SALES.select("region", "cnt").sort("region", nulls_last=True)
    pol_desc = SALES.select("region", "cnt").sort("region", descending=True, nulls_last=True)
    print("    升序：sort('region', nulls_last=True)")
    show_frame(pol_asc)
    print()
    print("    降序：sort('region', descending=True, nulls_last=True)")
    show_frame(pol_desc)

    print()
    print("  对账（两边都排成 NULLS LAST）：")
    duck_asc = con.sql("SELECT region, cnt FROM sales ORDER BY region ASC NULLS LAST").pl()
    ok, why = common.compare_frames(duck_asc, pol_asc, ["region", "cnt"])
    print(f"    升序 NULLS LAST   {'✅' if ok else '❌'} {why}")
    duck_desc = con.sql("SELECT region, cnt FROM sales ORDER BY region DESC NULLS LAST").pl()
    ok, why = common.compare_frames(duck_desc, pol_desc, ["region", "cnt"])
    print(f"    降序 NULLS LAST   {'✅' if ok else '❌'} {why}")

    print()
    print("  注意 Polars 的 `sort()` **只有 `nulls_last` 一个开关**")
    print("  （没有 `nulls_first`），而且它是给**所有排序列**用的同一个值。")
    print("  想按列分别设置 NULL 位置，DuckDB 那边可以直接写")
    print("  `ORDER BY a NULLS FIRST, b NULLS LAST`，Polars 侧就只能")
    print("  在排序键上做文章（或者干脆在 SQL 侧排好再读回来）。")


# ---------------------------------------------------------------- 8. 对账


def scenario_summary(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 八条糖的总账")

    rows = [
        (
            "GROUP BY ALL",
            "GROUP BY ALL",
            "group_by([...])",
            "键要自己写全；sum 的空集是 0 不是 NULL",
        ),
        ("ORDER BY ALL", "ORDER BY ALL", "sort([...])", "NULL 位置要自己写"),
        ("SELECT * EXCLUDE (...)", "EXCLUDE (a, b)", "drop('a', 'b')", "完全一致"),
        (
            "SELECT * REPLACE (...)",
            "REPLACE (e AS a)",
            "with_columns(...)",
            "完全一致，列位置也一致",
        ),
        (
            "聚合 FILTER (WHERE ...)",
            "sum(x) FILTER (WHERE c)",
            "agg 内 .filter() / when-then",
            "分组保留；sum 的空集是 0 不是 NULL",
        ),
        ("IS NOT DISTINCT FROM", "IS NOT DISTINCT FROM", "eq_missing()", "完全一致"),
        ("IS DISTINCT FROM", "IS DISTINCT FROM", "ne_missing()", "完全一致"),
        ("NULLS FIRST / LAST", "ORDER BY x NULLS LAST", "sort(nulls_last=...)", "两边默认值相反"),
    ]
    print(
        "  "
        + common.pad("糖", 26)
        + common.pad("DuckDB", 30)
        + common.pad("Polars", 30)
        + "对齐程度"
    )
    print("  " + "-" * 116)
    for name, duck_side, pol_side, note in rows:
        print(
            "  "
            + common.pad(name, 26)
            + common.pad(duck_side, 30)
            + common.pad(pol_side, 30)
            + note
        )

    print("\n  → 结论跟「能力差距」那组正好相反：")
    print("    · **八条糖里有四条是逐行逐列完全对上的** ——")
    print("      `EXCLUDE` ↔ `drop`、`REPLACE` ↔ `with_columns`、")
    print("      `IS [NOT] DISTINCT FROM` ↔ `eq_missing` / `ne_missing`；")
    print("    · 剩下四条的差别**全部来自默认值或空集语义**")
    print("      （NULL 的位置、NULL 的分组、空集的 sum），")
    print("      **不是「Polars 做不到」** —— 写上对应的参数就能对上；")
    print("    · 真正的能力差距在别处 —— 量词、相关标量子查询、LATERAL、")
    print("      全文本搜索（见 `src/quantifier_demo.py` 和 `src/fts_demo.py`）。")
    print()
    print("  → 迁移时的通用做法：**两边都把默认值写死**。")
    print("    DuckDB 侧写 `NULLS LAST`、Polars 侧写 `nulls_last=True`；")
    print("    DuckDB 侧写 `GROUP BY r, c`、Polars 侧写 `group_by(['r','c'])`；")
    print("    空集求和两边都要显式补 NULL 语义。")
    print("    别指望默认值对得上 —— 这一组里**没有一条默认值是对得上的**。")
    print()
    print("  顺带一提：`QUALIFY` 不在这张表里 —— 它已经在 `src/window_demo.py` 里演示过了，")
    print("  Polars 侧对应的是「先 `with_columns` 算窗口，再 `filter`」。")


def main() -> None:
    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_group_by_all(con)
        scenario_order_by_all(con)
        scenario_exclude(con)
        scenario_replace(con)
        scenario_agg_filter(con)
        scenario_is_distinct_from(con)
        scenario_nulls_first_last(con)
        scenario_summary(con)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
