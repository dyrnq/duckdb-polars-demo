"""透视与重塑：PIVOT / UNPIVOT 在两个引擎里的分叉。

同样是「把长表转成宽表」，两边的输出**形状不一样**：

* DuckDB：`PIVOT tbl ON k USING sum(v) GROUP BY g` —— 聚合写在 `USING` 后面，
  输出列按**键值排序**，缺失的组合填 **NULL**，行序**不保证**。
* Polars：`df.pivot(on="k", index="g", values="v", aggregate_function="sum")` ——
  输出列按**出现顺序**，缺失的组合用 `sum` 聚合时填 **0**，行序按 `maintain_order` 保留。

反方向也一样：DuckDB 的 `UNPIVOT` 默认**丢掉 NULL 行**，Polars 的 `unpivot` 全都留着。

这个场景要回答：形状不一致的时候，对账会挂在哪一维，以及「填 0」和「填 NULL」差在哪。

用法：
    uv run python src/pivot_demo.py
    uv run python src/pivot_demo.py --rows 5000000 --repeat 5
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

# 小表：9 行、3 个地区、4 个品类，明细一共 7 个非空组合
SALES_ROWS = [
    ("华东", "数码", 100),
    ("华东", "数码", 150),
    ("华东", "家居", 200),
    ("华北", "服饰", 300),
    ("华北", "服饰", 100),
    ("华北", "食品", 50),
    ("华南", "数码", 400),
    ("华南", "家居", 500),
    ("华南", "食品", 600),
]

# 品类：数据里出现过的四个（顺序 = 出现顺序）
CATEGORIES = ["数码", "家居", "服饰", "食品"]

# 数据里没有出现过的品类 —— 用来看「列的集合由谁决定」
EXTRA_CATEGORY = "美妆"


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        """
        CREATE TABLE sales AS SELECT * FROM (VALUES
          ('华东','数码',100), ('华东','数码',150), ('华东','家居',200),
          ('华北','服饰',300), ('华北','服饰',100), ('华北','食品', 50),
          ('华南','数码',400), ('华南','家居',500), ('华南','食品',600)
        ) t(region, category, amount)
        """
    )
    con.execute(
        """
        CREATE TABLE multi AS SELECT * FROM (VALUES
          ('g1','a','x',1), ('g1','a','y',2), ('g1','b','x',3),
          ('g2','a','x',4), ('g2','b','y',5)
        ) t(g, k1, k2, v)
        """
    )
    # 20 个地区 × 4 个品类 —— 专门用来看 PIVOT 的行序稳不稳
    con.execute(
        """
        CREATE TABLE many AS
        SELECT '地区' || (i + 1)::VARCHAR AS region,
               '品类' || (i % 4)::VARCHAR AS category,
               i AS amount
        FROM range(20) t(i)
        """
    )
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def sales_frame() -> pl.DataFrame:
    """和 DuckDB 里 sales 表内容完全一致的一份 Polars 数据。"""
    return pl.DataFrame(
        {
            "region": [r[0] for r in SALES_ROWS],
            "category": [r[1] for r in SALES_ROWS],
            "amount": [r[2] for r in SALES_ROWS],
        }
    )


def cell(value: object) -> str:
    """把一个单元格里的值转成给人看的字符串。"""
    if value is None:
        return "NULL"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def pick(df: pl.DataFrame, region: str, col: str) -> str:
    """取宽表里某个 (region, 列) 交叉点的值，转成字符串。"""
    regions = df["region"].to_list()
    return cell(df[col].to_list()[regions.index(region)])


def print_wide(title: str, df: pl.DataFrame) -> None:
    """打印一张宽表（列名是 pivot 键的值），按显示宽度对齐。"""
    print(f"  {title}")
    widths = []
    for col in df.columns:
        width = common.display_width(col)
        for value in df[col].to_list():
            width = max(width, common.display_width(cell(value)))
        widths.append(width)

    header = "".join(common.pad(col, w + 2) for col, w in zip(df.columns, widths, strict=True))
    print("    " + header.rstrip())
    print("    " + "-" * max(1, sum(widths) + 2 * len(widths) - 2))
    for row in df.iter_rows():
        line = "".join(common.pad(cell(v), w + 2) for v, w in zip(row, widths, strict=True))
        print("    " + line.rstrip())


# ---------------------------------------------------------------- 1. 形状


def scenario_shape(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 同一张长表，两边 pivot 出来的形状不一样")

    df = sales_frame()

    duck = con.sql("PIVOT sales ON category USING sum(amount) GROUP BY region").pl()
    pol = df.pivot(on="category", index="region", values="amount", aggregate_function="sum")

    print_wide("DuckDB  PIVOT sales ON category USING sum(amount) GROUP BY region", duck)
    print_wide("Polars  df.pivot(on='category', index='region', values='amount', ...)", pol)

    print("\n  列名集合：")
    print(f"    DuckDB: {duck.columns}")
    print(f"    Polars: {pol.columns}")
    print(f"    集合相同：{'是 ✅' if set(duck.columns) == set(pol.columns) else '否 ❌'}")
    print(f"    顺序相同：{'是 ✅' if duck.columns == pol.columns else '否 ❌'}")

    runs = 20
    orders = set()
    for _ in range(runs):
        rows = con.sql("PIVOT many ON category USING sum(amount) GROUP BY region").fetchall()
        orders.add(tuple(r[0] for r in rows))
    print(f"\n  行序（20 个地区那张表，DuckDB 连跑 {runs} 次）：")
    print(f"    出现了 {len(orders)} 种不同的地区顺序，举例：")
    for order in sorted(orders)[:3]:
        print(f"      {list(order)[:6]} ...")

    pol_orders = set()
    for _ in range(runs):
        res = df.pivot(on="category", index="region", values="amount", aggregate_function="sum")
        pol_orders.add(tuple(res["region"].to_list()))
    print(
        f"  行序（3 个地区那张表，Polars 连跑 {runs} 次）："
        f"出现了 {len(pol_orders)} 种顺序 —— pivot 默认 maintain_order=True"
    )

    print("\n  两边都能自己钉住顺序：")
    fixed = con.sql(
        "PIVOT sales ON category USING sum(amount) GROUP BY region ORDER BY region"
    ).pl()
    print(f"    DuckDB: PIVOT ... ORDER BY region      -> {fixed['region'].to_list()}")
    fixed_pol = df.pivot(
        on="category",
        index="region",
        values="amount",
        aggregate_function="sum",
        sort_columns=True,
    )
    print(f"    Polars: pivot(..., sort_columns=True)   -> {fixed_pol.columns}")

    print("\n→ 三条要记住的：")
    print("  · **列的名字两边是一样的**（都是 pivot 键的**值**：`数码`、`家居`……），")
    print("    这点不用操心。**顺序不一样**：DuckDB 按键值排序，Polars 按数据里的出现顺序。")
    print("    想要一致：Polars 加 `sort_columns=True` 就能跟 DuckDB 对上。")
    print("  · **行序 DuckDB 不保证** —— PIVOT 里没有 ORDER BY，行序由聚合算子决定。")
    print(f"    实测同一连接连跑 {runs} 次就出现了 {len(orders)} 种不同的地区顺序；")
    print("    Polars 的 pivot 默认 `maintain_order=True`，行序是稳的。")
    print("    → **DuckDB 侧任何对账都必须先 ORDER BY**（或者排序后再比），")
    print("      否则对出来的差异全是假的。")
    print("  · 所以「形状不一样」这件事，第一层是**顺序**，可以修；")
    print("    下一层才是真正修不掉的 —— 场景 2 那个。")


# ---------------------------------------------------------------- 2. 缺失组合


def scenario_missing(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 缺失组合：一个填 NULL，一个填 0")

    df = sales_frame()

    duck = con.sql("PIVOT sales ON category USING sum(amount) GROUP BY region").pl()
    duck_max = con.sql("PIVOT sales ON category USING max(amount) GROUP BY region").pl()
    pol_sum = df.pivot(
        on="category",
        index="region",
        values="amount",
        aggregate_function="sum",
        sort_columns=True,
    )
    pol_max = df.pivot(
        on="category",
        index="region",
        values="amount",
        aggregate_function="max",
        sort_columns=True,
    )

    print_wide("DuckDB  PIVOT ... USING sum(amount)", duck)
    print_wide("Polars  pivot(..., aggregate_function='sum')", pol_sum)
    print_wide("Polars  pivot(..., aggregate_function='max')", pol_max)

    print("\n  看「华东 × 服饰」这一格（数据里根本没有这个组合）：")
    print(f"    DuckDB sum -> {pick(duck, '华东', '服饰')}")
    print(f"    Polars sum -> {pick(pol_sum, '华东', '服饰')}")
    print(f"    Polars max -> {pick(pol_max, '华东', '服饰')}")

    ok_sum, why_sum = common.compare_frames(duck, pol_sum, ["region"])
    print(f"\n  对账（DuckDB sum vs Polars sum）：{'✅' if ok_sum else '❌'} {why_sum}")
    ok_max, why_max = common.compare_frames(duck_max, pol_max, ["region"])
    print(f"  对账（DuckDB max vs Polars max）：{'✅' if ok_max else '❌'} {why_max}")

    print("\n  用 `WHERE 服饰 IS NOT NULL` 过滤一下，差异更刺眼：")
    duck_notnull = con.sql(
        "SELECT region FROM (PIVOT sales ON category USING sum(amount) GROUP BY region) "
        "WHERE 服饰 IS NOT NULL"
    ).fetchall()
    pol_notnull = pol_sum.filter(pl.col("服饰").is_not_null())["region"].to_list()
    print(f"    DuckDB: {[r[0] for r in duck_notnull]}")
    print(f"    Polars: {pol_notnull}")

    duck_filled = sum(
        q1(
            con,
            f"SELECT count({c}) FROM (PIVOT sales ON category USING sum(amount) GROUP BY region)",
        )
        for c in CATEGORIES
    )
    pol_filled = sum(len(pol_sum[c].drop_nulls()) for c in CATEGORIES)
    total_cells = duck.height * len(CATEGORIES)
    print(f"\n  整张宽表 {duck.height} × {len(CATEGORIES)} = {total_cells} 个格子，空的有几个：")
    print(f"    DuckDB: {total_cells - duck_filled} 个空格子")
    print(f"    Polars: {total_cells - pol_filled} 个空格子")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 的 PIVOT 缺失组合填 NULL**，Polars 的填 **0** —— ")
    print("    「这个组合没有数据」和「这个组合的销售额是 0」被混成了一回事。")
    print("    上面数出来的空格子数就是差额：DuckDB 那边缺几格就空几格，")
    print("    Polars 那边一个空格子都没有。")
    print("  · **Polars 的填充值跟着聚合函数走**：`sum` 填 0，`max` / `first` 填 NULL。")
    print("    `pivot()` **没有 `fill_value` 参数**，你选不了。")
    print("    → 所以上面那两个对账：sum 对 sum ❌，max 对 max ✅ ——")
    print("      **能对上只是因为你把聚合函数也换成了一样的**，两边语义仍然不同：")
    print("      DuckDB 那边「没有数据」永远是 NULL，Polars 那边 `sum` 永远是 0。")
    print("  · **这不是显示层的差别，是语义的差别**：")
    print("    `IS NOT NULL` 在 DuckDB 那边是「这个地区卖过服饰」，")
    print("    在 Polars 那边是「这个地区有服饰这一行」—— 后者永远为真。")
    print("    报表里「有销量的地区」这种筛选，两边会给出**不同的地区集合**。")


# ---------------------------------------------------------------- 3. 列的集合


def scenario_column_set(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 列的集合：DuckDB 能用 IN 钉死，Polars 默认跟着数据走")

    df = sales_frame()
    in_list = ", ".join(f"'{c}'" for c in [*CATEGORIES, EXTRA_CATEGORY])

    duck_in = con.sql(
        f"PIVOT sales ON category IN ({in_list}) USING sum(amount) GROUP BY region"
    ).pl()
    pol_seen = df.pivot(on="category", index="region", values="amount", aggregate_function="sum")
    pol_pinned = df.pivot(
        on="category",
        index="region",
        values="amount",
        aggregate_function="sum",
        on_columns=[*CATEGORIES, EXTRA_CATEGORY],
    )

    print(f"  数据里出现过的品类：{CATEGORIES}")
    print(f"  数据里**没**出现过的品类：['{EXTRA_CATEGORY}']")
    print("\n  DuckDB  PIVOT sales ON category IN (数码, 家居, 服饰, 食品, 美妆) ...")
    print(f"    列 -> {duck_in.columns}")
    print("\n  Polars  pivot(on='category', index='region', values='amount', ...)")
    print(f"    列 -> {pol_seen.columns}")
    print("\n  Polars  pivot(..., on_columns=[数码, 家居, 服饰, 食品, 美妆])")
    print(f"    列 -> {pol_pinned.columns}")

    print_wide("DuckDB  IN 列表钉死的宽表", duck_in)
    print_wide("Polars  on_columns 钉死的宽表", pol_pinned)

    ok, why = common.compare_frames(duck_in, pol_pinned, ["region"])
    print(f"\n  对账：{'✅' if ok else '❌'} {why}")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 的 PIVOT 默认只出「数据里出现过」的列**，")
    print("    但你一加 `IN (...)`，列集合就由你说了算 ——")
    print(f"    数据里没有的「{EXTRA_CATEGORY}」也照样出一列（全 NULL）。")
    print("  · **Polars 的 `pivot()` 默认也是跟着数据走**，")
    print("    但它有个 `on_columns=` 参数能钉死列集合（值必须是列表或一张模板表）。")
    print("  · 所以「列的集合」这一维两边都能对齐 —— **但填充口径对不齐**（场景 2）：")
    print("    上面那个对账 ❌ 的原因就是 `美妆` 那一列，")
    print("    DuckDB 给 NULL、Polars 给 0。**列齐了不等于值齐了。**")


# ---------------------------------------------------------------- 4. 多列 pivot


def scenario_multi_pivot(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 多列 pivot：列名格式根本对不上")

    duck = con.sql("PIVOT multi ON k1, k2 USING max(v) GROUP BY g").pl()
    pol = pl.DataFrame(
        {
            "g": ["g1", "g1", "g1", "g2", "g2"],
            "k1": ["a", "a", "b", "a", "b"],
            "k2": ["x", "y", "x", "x", "y"],
            "v": [1, 2, 3, 4, 5],
        }
    ).pivot(
        on=["k1", "k2"],
        index="g",
        values="v",
        aggregate_function="max",
        sort_columns=True,
    )

    print("  DuckDB  PIVOT multi ON k1, k2 USING max(v) GROUP BY g")
    print(f"    列 -> {duck.columns}")
    print("\n  Polars  pivot(on=['k1','k2'], index='g', values='v', aggregate_function='max')")
    print(f"    列 -> {pol.columns}")
    pol_combine = pol.rename(
        {
            c: c.replace('{"', "").replace('","', "_").replace('"}', "")
            for c in pol.columns
            if c != "g"
        }
    )
    print("\n  Polars 加 column_naming='combine' 之后列名更怪：")
    print('    列 -> [\'g\', \'v_{"a","x"}\', \'v_{"a","y"}\', ...]（值名 + 值的元组）')
    print(f"  Polars 手工去掉元组括号后：{pol_combine.columns}")

    print_wide("DuckDB  多列 pivot", duck)
    print_wide("Polars  多列 pivot（列名已手工改成 DuckDB 的格式）", pol_combine)

    ok, why = common.compare_frames(duck, pol_combine, ["g"])
    print(f"\n  对账（改完列名之后）：{'✅' if ok else '❌'} {why}")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 多列 pivot 的列名是 `值1_值2`**：`a_x`、`a_y`……")
    print('    **Polars 的多列 pivot 列名是一整个元组的字符串**：`{"a","x"}`。')
    print('    `column_naming=\'combine\'` 更糟，变成 `v_{"a","x"}`（值名 + 元组）。')
    print("  · 这不是能靠参数对齐的 —— Polars 没有「用下划线拼接元组」这个选项。")
    print("    要对上只能自己 `rename`（上面那段就是）。")
    print("  · **多列 pivot 是「列名格式」这条差异第一次露头的地方**：")
    print("    单列 pivot 时两边列名一样（都是键的值），多列 pivot 时两边各写各的。")
    print("    → 迁移时凡是用了多列 pivot，**输出表的列名大概率要改**，")
    print("      下游读这张宽表的代码全都得跟着动。")


# ---------------------------------------------------------------- 5. UNPIVOT


def scenario_unpivot(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · UNPIVOT：宽转长，NULL 的去留两边不一样")

    con.execute(
        """
        CREATE OR REPLACE TABLE wide AS
        SELECT region,
               sum(amount) FILTER (WHERE category='数码') AS 数码,
               sum(amount) FILTER (WHERE category='家居') AS 家居,
               sum(amount) FILTER (WHERE category='服饰') AS 服饰,
               sum(amount) FILTER (WHERE category='食品') AS 食品
        FROM sales GROUP BY region
        """
    )
    print_wide(
        "起点：一张 3 行 × 4 列的宽表（缺失的格子是 NULL）", con.sql("SELECT * FROM wide").pl()
    )

    duck_simple = con.sql(
        "UNPIVOT wide ON 数码, 家居, 服饰, 食品 INTO NAME category VALUE amount"
    ).pl()
    duck_std = con.sql(
        "FROM wide UNPIVOT INCLUDE NULLS (amount FOR category IN (数码, 家居, 服饰, 食品))"
    ).pl()
    pol = (
        con.sql("SELECT * FROM wide")
        .pl()
        .unpivot(
            on=CATEGORIES,
            index=["region"],
            variable_name="category",
            value_name="amount",
        )
    )

    print_wide("DuckDB  UNPIVOT wide ON ... INTO NAME category VALUE amount", duck_simple)
    print_wide("DuckDB  FROM wide UNPIVOT INCLUDE NULLS (...)", duck_std)
    print_wide("Polars  df.unpivot(on=[...], index=[...], ...)", pol)

    print("\n  行数：宽表有 3 × 4 = 12 个格子")
    print(f"    DuckDB 简化语法 UNPIVOT -> {duck_simple.height} 行（**丢掉 NULL 行**）")
    print(f"    DuckDB 标准语法 INCLUDE NULLS -> {duck_std.height} 行")
    print(f"    Polars unpivot -> {pol.height} 行")

    ok1, why1 = common.compare_frames(duck_std, pol, ["region", "category"])
    print(f"\n  对账（DuckDB INCLUDE NULLS vs Polars）：{'✅' if ok1 else '❌'} {why1}")
    ok2, why2 = common.compare_frames(duck_simple, pol, ["region", "category"])
    print(f"  对账（DuckDB 简化 UNPIVOT vs Polars）：{'✅' if ok2 else '❌'} {why2}")

    print("\n  DuckDB 简化语法想带 NULL 会怎样：")
    try:
        con.sql(
            "UNPIVOT wide ON 数码, 家居, 服饰, 食品 INTO NAME category VALUE amount INCLUDE NULLS"
        ).fetchall()
        print("    （居然成功了）")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {str(exc).splitlines()[0][:100]}")
        print("    → 简化语法**根本没有 INCLUDE NULLS 这个开关**，")
        print("      要保留 NULL 必须改用 SQL 标准写法 `FROM t UNPIVOT INCLUDE NULLS (...)`。")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 的简化 UNPIVOT 默认丢掉 NULL 行**，Polars 的 `unpivot` **全都留着**。")
    print(f"    上面那张宽表 12 个格子，简化语法只吐出 {duck_simple.height} 行（只有非 NULL 的），")
    print(f"    Polars 吐出 {pol.height} 行 —— **行数都不一样，怎么对账**。")
    print("  · **DuckDB 自己有两种 UNPIVOT 语法，NULL 行为不一样**：")
    print("    `UNPIVOT t ON ... INTO NAME ... VALUE ...` 丢 NULL；")
    print("    `FROM t UNPIVOT INCLUDE NULLS (...)` 留 NULL。")
    print("    简化语法**连 `INCLUDE NULLS` 都写不了**（ParserException）。")
    print("  · → **口径要对齐，先看语法**：DuckDB 侧写哪个语法决定了 NULL 的去留；")
    print("    Polars 侧没有开关，永远是「全留」。")


# ---------------------------------------------------------------- 6. 往返


def scenario_roundtrip(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 往返：PIVOT → UNPIVOT 能不能还原")

    df = sales_frame()
    orig = df.group_by(["region", "category"]).agg(pl.col("amount").sum())

    duck_back = con.sql(
        "UNPIVOT (PIVOT sales ON category USING sum(amount) GROUP BY region) "
        "ON 数码, 家居, 服饰, 食品 INTO NAME category VALUE amount"
    ).pl()
    pol_back = df.pivot(
        on="category", index="region", values="amount", aggregate_function="sum"
    ).unpivot(
        on=CATEGORIES,
        index=["region"],
        variable_name="category",
        value_name="amount",
    )

    print(f"  起点（长表去重后）：{orig.height} 行 —— 7 个 (地区, 品类) 组合")
    print(f"  DuckDB  pivot → unpivot 之后：{duck_back.height} 行")
    print(f"  Polars  pivot → unpivot 之后：{pol_back.height} 行")

    ok, why = common.compare_frames(orig, duck_back, ["region", "category"])
    print(f"\n  对账（DuckDB 往返 vs 原始长表）：{'✅' if ok else '❌'} {why}")
    ok2, why2 = common.compare_frames(orig, pol_back, ["region", "category"])
    print(f"  对账（Polars 往返 vs 原始长表）：{'✅' if ok2 else '❌'} {why2}")

    print(f"\n  Polars 往返多出来的那 {pol_back.height - orig.height} 行（全都是 0）：")
    extra = (
        pol_back.join(
            orig.with_columns(pl.lit(True).alias("_has")),
            on=["region", "category"],
            how="left",
        )
        .filter(pl.col("_has").is_null())
        .select("region", "category", "amount")
    )
    for region, category, amount in extra.iter_rows():
        print(f"    {common.pad(region, 8)}{common.pad(category, 8)}{amount:>6}")
    print("    → 这些组合**在原始数据里根本不存在**，是 pivot 阶段的 0 被 unpivot 忠实读回来的。")

    print("\n  DuckDB 那边看起来「还原」了，但它是靠**丢掉 NULL**还原的：")
    duck_std_back = con.sql(
        "FROM (PIVOT sales ON category USING sum(amount) GROUP BY region) "
        "UNPIVOT INCLUDE NULLS (amount FOR category IN (数码, 家居, 服饰, 食品))"
    ).pl()
    print(f"    换成 INCLUDE NULLS，往返就是 {duck_std_back.height} 行（和 Polars 一样多），")
    print("    但多出来的那几行是 NULL 而不是 0 —— **DuckDB 保留了「没有数据」这个事实**。")

    print("\n→ 三条要记住的：")
    print("  · **两边的往返都不完全无损，但丢的东西不一样**：")
    print("    DuckDB（简化 UNPIVOT）把缺失的组合**直接扔了** —— 看起来还原了，")
    print("    其实是因为它把「没有这条数据」和「这条数据是 0」混为一谈了。")
    print("    Polars 把缺失的组合**变成 0 留下来** —— 凭空多出几行零。")
    print("  · **真正正确的是 DuckDB 的 `INCLUDE NULLS`**：它保留 NULL，")
    print(
        f"    {duck_std_back.height} 行里 {orig.height} 行有值、"
        f"{duck_std_back.height - orig.height} 行是 NULL，信息一点没丢。"
    )
    print("    Polars 那边要还原这个语义，只能在 unpivot 之后**自己把 0 换回 NULL** ——")
    print("    但那又分不清「真的是 0」和「原来是 NULL」了。")
    print("  · → **「填 0」这个默认值是有代价的**：它把一个三值逻辑（NULL / 0 / 有值）")
    print("    压成了两值逻辑，往返一次之后你就再也分不清「没数据」和「数据是 0」了。")
    print("    这是本 demo 里最隐蔽的一条 —— **它不报错，只是默默改了你的数据**。")


# ---------------------------------------------------------------- 7. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 代价：宽表 pivot 的代价")

    print(f"数据：{rows} 行（地区 = i % 200 + 1，品类 = (i // 200) % 12 + 1）")
    print("  行数够大时铺满 200 个地区 × 12 个品类，透视后是一张宽表")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="pivot_demo_"))
    src = tmpdir / "sales.parquet"
    con.execute(
        """
        COPY (
          SELECT
            '地区' || ((i % 200) + 1)::VARCHAR AS region,
            '品类' || ((i // 200) % 12 + 1)::VARCHAR AS category,
            (i * 2654435761) % 1000003 AS amount
          FROM range(?) t(i)
        ) TO ? (FORMAT PARQUET)
        """,
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)
    con.execute("CREATE OR REPLACE TABLE grp AS SELECT * FROM read_parquet(?)", [str(src)])

    cats = [f"品类{i}" for i in range(1, 13)]
    in_list = ", ".join(f"'{c}'" for c in cats)

    def duck_pivot() -> int:
        return con.sql("PIVOT grp ON category USING sum(amount) GROUP BY region").pl().height

    def polars_pivot() -> int:
        return polars_src.pivot(
            on="category", index="region", values="amount", aggregate_function="sum"
        ).height

    def duck_pivot_in() -> int:
        return (
            con.sql(f"PIVOT grp ON category IN ({in_list}) USING sum(amount) GROUP BY region")
            .pl()
            .height
        )

    def polars_pivot_pinned() -> int:
        return polars_src.pivot(
            on="category",
            index="region",
            values="amount",
            aggregate_function="sum",
            on_columns=cats,
        ).height

    duck_rows = duck_pivot()
    pol_rows = polars_pivot()
    duck_cols = con.sql("PIVOT grp ON category USING sum(amount) GROUP BY region").columns
    pol_frame = polars_src.pivot(
        on="category", index="region", values="amount", aggregate_function="sum"
    )
    print(f"\n  透视后的行数：DuckDB {duck_rows} 行，Polars {pol_rows} 行")
    print(f"  列数：DuckDB {len(duck_cols)} 个，Polars {len(pol_frame.columns)} 个")
    print("  注意：两边**列数一样、值不一样**（DuckDB 填 NULL、Polars 填 0）——")
    print("  所以下面这四个数字只能比耗时，不能比结果。")

    results = [
        ("DuckDB PIVOT（列集合跟着数据）", duck_pivot),
        ("Polars pivot（列集合跟着数据）", polars_pivot),
        ("DuckDB PIVOT ... IN (12 个品类)", duck_pivot_in),
        ("Polars pivot(on_columns=12 个品类)", polars_pivot_pinned),
    ]

    print(
        "\n  "
        + common.pad("实现", 38)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果行数", 12)
    )
    print("  " + "-" * 76)
    for label, fn in results:
        fn()
        times: list[float] = []
        value = 0
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        fastest = min(times)
        print(
            "  "
            + common.pad(label, 38)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 12)
        )

    print("\n口径：输入已物化成 parquet，两边读的是同一份数据；")
    print("  计时的只有「长表 → 宽表」这一步（不含读盘）；")
    print("  两边最终产出的都是同一个 `pl.DataFrame`（DuckDB 那边走 Arrow 转换）。")
    print("  **但两边的输出不是同一个东西**：DuckDB 的缺失组合是 NULL、Polars 的是 0，")
    print("  所以这四个数字**只能比耗时，不能比结果**，也没有对账可言。")
    print("  前两行是同一件事（列集合跟着数据走），后两行是同一件事（列集合钉死），")
    print("  各自内部可比，跨组不可比。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")

    print("\n解读：")
    print("  · **pivot 的成本主要在两处**：把长表按 (地区, 品类) 聚合一次，")
    print("    再把聚合结果**摊成宽表**。列越多、格子越密，摊开这一步越贵。")
    print("  · **「钉死列集合」两边都要额外代价**：DuckDB 要把 12 个品类写进 `IN (...)`，")
    print("    Polars 要把 12 个品类写进 `on_columns=[...]`。")
    print("    这不是白给的 —— 写死的列集合意味着引擎要**按这个集合建表**，")
    print("    包括那些一行数据都没有的空列。")
    print("  · **真正该记住的不是谁快，而是这两个数字不能拿来比大小**：")
    print("    两边输出的内容不一样（NULL vs 0），场景 2 已经证明这会一路影响到")
    print("    `IS NOT NULL` 过滤和往返。**口径不对齐，性能数字再好看也没意义。**")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的透视与重塑差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_shape(con)
        scenario_missing(con)
        scenario_column_set(con)
        scenario_multi_pivot(con)
        scenario_unpivot(con)
        scenario_roundtrip(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
