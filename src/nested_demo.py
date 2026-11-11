"""嵌套类型：LIST / STRUCT / MAP 的对应关系，外加 DuckDB 2.0 的 lambda 破坏性变更。

* DuckDB：三种嵌套类型都是**一等公民** —— 字面量直接写进 SQL（`[1,2,3]` / `{'a': 1}` /
  `MAP{'k': v}`），几十个 `list_` / `map_` / `struct_` 函数（脚本里现算），
  lambda 还能内联进表达式。
* Polars：LIST / STRUCT 是**列的 dtype**（`List(Int64)` / `Struct({...})`），
  MAP 要拿 dict + 显式 dtype 构造；函数挂在 `.list` / `.struct` / `.map` 命名空间上，
  写法是「列上的方法」，不是 SQL 表达式。
* DuckDB 2.0 **废弃了 lambda 箭头语法**：`x -> x*2` 直接报 `BinderException`，
  必须写成 `lambda x: x*2`。

这个场景要回答：嵌套类型能不能一一对应，迁移的时候具体会卡在哪几个地方。

用法：
    uv run python src/nested_demo.py
    uv run python src/nested_demo.py --rows 5000000 --repeat 5
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

# 一个 MAP 字面量，好几个场景都要用（SQL 里的 map 键是字符串）
MAP_SQL = "MAP{'k': 1, 'j': 2}"


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- 1. 类型对照


def scenario_types(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 三种嵌套类型：一边是字面量，一边是 dtype")

    lit = con.sql("SELECT [1, 2, 3], {'a': 1, 'b': 'x'}, MAP{'k': 1, 'j': 2}").fetchall()[0]
    types = con.sql("SELECT typeof([1,2,3]), typeof({'a':1}), typeof(MAP{'k':1})").fetchall()[0]

    print("  DuckDB 侧 —— 直接写进 SQL：")
    print(f"    SELECT [1,2,3]          -> {lit[0]}")
    print(f"    SELECT {{'a':1,'b':'x'}}  -> {lit[1]}")
    print(f"    SELECT MAP{{'k':1,'j':2}} -> {lit[2]}")
    print()
    print("  对应的类型：")
    for name, t in zip(("LIST", "STRUCT", "MAP"), types, strict=True):
        print(f"    {common.pad(name, 8)}{t}")

    pol_list = pl.Series("v", [[1, 2, 3]], dtype=pl.List(pl.Int64))
    pol_struct = pl.Series(
        "v", [{"a": 1, "b": "x"}], dtype=pl.Struct({"a": pl.Int64, "b": pl.String})
    )
    pol_map = pl.Series("v", [{"k": 1, "j": 2}], dtype=pl.Map(pl.String, pl.Int64))

    print()
    print("  Polars 侧 —— 靠 dtype 声明，构造写法：")
    print("    pl.Series('v', [[1, 2, 3]], dtype=pl.List(pl.Int64))")
    print("    pl.Series('v', [{'a': 1, 'b': 'x'}],")
    print("                dtype=pl.Struct({'a': pl.Int64, 'b': pl.String}))")
    print("    pl.Series('v', [{'k': 1, 'j': 2}], dtype=pl.Map(pl.String, pl.Int64))")
    print()
    print("  得到的 dtype：")
    for name, s in zip(("LIST", "STRUCT", "MAP"), (pol_list, pol_struct, pol_map), strict=True):
        print(f"    {common.pad(name, 8)}{s.dtype}")

    print()
    print(
        "  "
        + common.pad("类型", 8)
        + common.pad("DuckDB 字面量", 26)
        + common.pad("DuckDB 类型", 30)
        + common.pad("Polars dtype", 34)
    )
    print("  " + "-" * 96)
    rows = [
        ("LIST", "[1, 2, 3]", types[0], str(pol_list.dtype)),
        ("STRUCT", "{'a': 1, 'b': 'x'}", types[1], str(pol_struct.dtype)),
        ("MAP", "MAP{'k': 1, 'j': 2}", types[2], str(pol_map.dtype)),
    ]
    for name, literal, dt, pd in rows:
        print("  " + common.pad(name, 8) + common.pad(literal, 26) + common.pad(dt, 30) + pd)

    print("\n→ 三种类型**都能对上**，但「怎么写」完全不同：")
    print("  · DuckDB：类型是**值的语法** —— 写个 `[1,2,3]` 就是 LIST，列的类型由值推出来。")
    print("    `typeof()` 能随时问出来。")
    print("  · Polars：类型是**列的声明** —— 必须在构造的时候就把 dtype 说清楚，")
    print("    而且**没有 `typeof()` 这种运行时反查**的入口（只有 `df.schema`）。")
    print("  · MAP 两边都最别扭：DuckDB 得写 `MAP{...}`；")
    print("    Polars **光给一个 list of dict 是不够的**，必须补 `dtype=pl.Map(...)`，")
    print("    否则 Polars 会把它当成普通 struct 列，而不是 map 列。")

    # Polars 少写 dtype 会变成什么
    no_dtype = pl.Series("v", [{"k": 1, "j": 2}])
    print()
    print(f"    少写 dtype 的后果：polars Series([{{'k':1}}]).dtype -> {no_dtype.dtype}")
    print("    （是 Struct，不是 Map —— 后面 `.map.keys()` 这类方法直接就用不了）")


# ---------------------------------------------------------------- 2. LIST 函数


def scenario_list(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · LIST 函数：能对上的那部分，和对不上的那部分")

    n_nested = q1(
        con,
        "SELECT count(DISTINCT function_name) FROM duckdb_functions() "
        "WHERE function_name LIKE 'list_%' OR function_name LIKE 'map_%' "
        "OR function_name LIKE 'struct_%'",
    )
    print(
        f"  DuckDB 有 {n_nested} 个 list_/map_/struct_ 函数；Polars 把它们收进 `.list` 命名空间。"
    )
    print("  常用的几个：")
    print()
    print("  " + common.pad("DuckDB", 30) + common.pad("Polars", 30) + common.pad("说明", 34))
    print("  " + "-" * 92)
    pairs = [
        ("list_sum(l)", "list.sum()", "空列表：NULL vs 0"),
        ("list_avg(l)", "list.mean()", ""),
        ("list_max(l)", "list.max()", ""),
        ("list_len(l) / len(l)", "list.len()", "DuckDB 还有 len() 简写"),
        ("list_sort(l)", "list.sort()", ""),
        ("list_distinct(l)", "list.unique()", ""),
        ("list_concat(a, b)", "list.concat(a, b)", ""),
        ("list_slice(l, o, n)", "list.slice(o, n)", "DuckDB 不支持负索引"),
        ("list_contains(l, x)", "list.contains(x)", "NULL 语义不同"),
        ("list_filter(l, lambda)", "list.filter(expr)", "Polars 收的是表达式"),
        ("list_transform(l, lambda)", "list.eval(expr)", "Polars 收的是表达式"),
        ("list_reduce(l, lambda)", "（没有）", "Polars 要自己 fold"),
        ("list_position(l, x)", "（没有）", ""),
    ]
    for duck, pol, note in pairs:
        print("  " + common.pad(duck, 30) + common.pad(pol, 30) + note)

    print("\n  三处对不上，先看第一处 —— 求和的入口不一样：")
    df = pl.DataFrame({"v": [[1, 2, 3], [4, 5]]})
    try:
        df["v"].sum()
    except Exception as exc:
        print(f"    df['v'].sum()          -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print(f"    df['v'].list.sum()      -> {df['v'].list.sum().to_list()}")
    print("    → `Series.sum()` 是**整列**求和，撞上 list dtype 直接报 InvalidOperationError；")
    print("      `Series.list.sum()` 才是「逐个 list 求和」。")
    print("      这两个名字只差一个 `.list`，报错信息却完全没提 list —— 最容易敲错的一个。")

    print("\n  第二处 —— 空列表：")
    duck_empty = q1(con, "SELECT list_sum([]::INT[])")
    pol_empty = pl.Series("v", [[]], dtype=pl.List(pl.Int64)).list.sum().to_list()[0]
    print(f"    DuckDB list_sum([])   -> {duck_empty}")
    print(f"    Polars list.sum()([]) -> {pol_empty}")
    print("    → DuckDB 认「空列表的和是未定义」，给 NULL；Polars 给 0。")
    print("      同一批数据，只要有一个 list 是空的，两边的总和就会差一个值 ——")
    print("      而且 DuckDB 那边是 NULL（**不是 0**），聚合时会把整列污染掉。")

    print("\n  第三处 —— NULL 元素：")
    duck_has_null = q1(con, "SELECT list_contains([1, NULL, 3], NULL)")
    pol_has_null = pl.Series([[1, None, 3]]).list.contains(None).to_list()[0]
    print(f"    DuckDB list_contains([1,NULL,3], NULL) -> {duck_has_null}")
    print(f"    Polars list.contains(None)             -> {pol_has_null}")
    print("    → DuckDB 走三值逻辑：含 NULL 的列表问「有没有 NULL」得到 NULL（UNKNOWN）；")
    print("      Polars 直接答 True。")

    print("\n  顺带一个 DuckDB 独有的能力：lambda 能内联进 SQL 表达式。")
    duck_xform = "SELECT list_transform([1,2,3], lambda x: x*2)"
    duck_filter = "SELECT list_filter([1,2,3,4], lambda x: x%2=0)"
    print(f"    {duck_xform} -> {q1(con, duck_xform)}")
    print(f"    {duck_filter} -> {q1(con, duck_filter)}")
    print("    Polars 侧的等价物是**表达式**（`pl.element()`），不是 lambda：")
    expr = pl.Series([[1, 2, 3, 4]], dtype=pl.List(pl.Int64))
    pol_eval = expr.list.eval(pl.element() * 2).to_list()[0]
    pol_filter = expr.list.filter(pl.element() % 2 == 0).to_list()[0]
    print(f"    list.eval(pl.element() * 2)         -> {pol_eval}")
    print(f"    list.filter(pl.element() % 2 == 0)  -> {pol_filter}")

    print("\n→ 这一段要记住的：")
    print("  · **函数名能对上，语义对不上**：`list_sum` vs `list.sum()` 在空列表上分道扬镳。")
    print("  · Polars 没有 `list_reduce` / `list_position` 这类函数，")
    print("    对应的活要么自己写，要么退回到 `explode` 之后用普通算子。")
    print("  · DuckDB 的 lambda 是**SQL 表达式的一部分**，能直接嵌进 `CASE WHEN`；")
    print("    Polars 的 `list.eval` 只能作用在列上，条件逻辑得在外面套一层。")


# ---------------------------------------------------------------- 3. lambda 变更


def scenario_lambda(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · DuckDB 2.0 的 lambda 箭头语法：1.x 迁 2.x 必踩")

    print("  DuckDB 1.x 的写法：")
    print("    SELECT list_transform([1,2,3], x -> x*2)")
    print()
    print("  DuckDB 2.0 上跑这一句，报错原文如下：")
    try:
        con.sql("SELECT list_transform([1,2,3], x -> x*2)").fetchall()
    except Exception as exc:
        print(f"    {type(exc).__name__}:")
        for line in str(exc).splitlines():
            print(f"      {line}")

    print()
    print("  正确写法是 **lambda 关键字 + 冒号**：")
    ok = con.sql("SELECT list_transform([1,2,3], lambda x: x*2)").fetchall()[0][0]
    print(f"    SELECT list_transform([1,2,3], lambda x: x*2) -> {ok}")

    print()
    print("  lambda 还能带第二个参数（下标），这在 Polars 里没有对应物：")
    with_idx = con.sql("SELECT list_transform([1,2,3], lambda x, i: x + i)").fetchall()[0][0]
    print(f"    SELECT list_transform([1,2,3], lambda x, i: x + i) -> {with_idx}")

    print()
    print("  DuckDB 留了一个后门 —— 临时改设置就能继续用旧语法：")
    con.execute("SET lambda_syntax='ENABLE_SINGLE_ARROW'")
    old = con.sql("SELECT list_transform([1,2,3], x -> x*2)").fetchall()[0][0]
    print(f"    SET lambda_syntax='ENABLE_SINGLE_ARROW' 之后 -> {old}")
    con.execute("SET lambda_syntax='DEFAULT'")

    print("\n→ 这一条是**纯粹的破坏性变更**，跟语义对不对没关系：")
    print("  · DuckDB 1.x 写的 `x -> x*2`，在 2.0 上是**编译期就炸**，")
    print("    不是「跑出来结果不对」，而是这条 SQL 根本过不了 binder。")
    print("    → 好处是不会静默出错；坏处是升级当天所有用到 lambda 的查询全挂。")
    print("  · 改法是机械的：`->` 换成 `lambda ... :`，一个查询一个查询地改。")
    print("    `SET lambda_syntax='ENABLE_SINGLE_ARROW'` 只是应急，")
    print("    报错原文里也说了「before DuckDB's next release」。")
    print("  · Polars 侧**没有这个问题**：它压根没有 lambda 语法，")
    print("    对应物是 `pl.element()` 表达式 —— 从 DuckDB 迁过去反而是简化。")


# ---------------------------------------------------------------- 4. STRUCT


def scenario_struct(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · STRUCT：字段访问和「拆成列」")

    con.execute(
        "CREATE OR REPLACE TABLE st AS SELECT * FROM (VALUES"
        " (1, {'a': 10, 'b': 'x'}), (2, {'a': 20, 'b': 'y'})"
        ") t(id, s)"
    )

    extract_sql = "SELECT struct_extract(s, 'a') FROM st WHERE id=1"
    print("  DuckDB 侧：")
    print(f"    SELECT s FROM st          -> {q1(con, 'SELECT s FROM st WHERE id=1')}")
    print(f"    SELECT s.a FROM st        -> {q1(con, 'SELECT s.a FROM st WHERE id=1')}")
    print(f"    SELECT id, s.* FROM st    -> {con.sql('SELECT id, s.* FROM st').fetchall()}")
    print(f"    SELECT id, unnest(s)      -> {con.sql('SELECT id, unnest(s) FROM st').fetchall()}")
    print(f"    struct_pack(x := 1, y := 2) -> {q1(con, 'SELECT struct_pack(x := 1, y := 2)')}")
    print(f"    struct_extract(s, 'a')    -> {q1(con, extract_sql)}")

    print()
    print("  Polars 侧（同样这两行数据）：")
    pdf = pl.DataFrame({"id": [1, 2], "s": [{"a": 10, "b": "x"}, {"a": 20, "b": "y"}]})
    print(f"    df['s'].struct.field('a')      -> {pdf['s'].struct.field('a').to_list()}")
    print("    df.select(pl.col('s').struct.unnest()) ->")
    print(f"      {pdf.select(pl.col('s').struct.unnest()).to_dicts()}")
    print(f"    df.unnest('s')                 -> {pdf.unnest('s').to_dicts()}")

    print()
    print("  拆成列之后对账（两边拆出来的字段值必须一样）：")
    duck_wide = con.sql("SELECT id, s.a AS a, s.b AS b FROM st ORDER BY id").pl()
    pol_wide = pdf.select(
        pl.col("id"),
        pl.col("s").struct.field("a").alias("a"),
        pl.col("s").struct.field("b").alias("b"),
    )
    same, reason = common.compare_frames(duck_wide, pol_wide, ["id"])
    verdict = "✅ 一致" if same else "❌ 不一致"
    print(f"    DuckDB s.a / s.b  vs  Polars struct.field -> {verdict}：{reason}")

    print()
    print("  DuckDB 的 `unnest(s)` 拆出来是**两列**，不是两个字段 ——")
    print(
        f"    SELECT id, unnest(s) FROM st -> {con.sql('SELECT id, unnest(s) FROM st').fetchall()}"
    )
    print("    所以 DuckDB 里 `unnest` 对 struct 和对 list 是同一个动词，行为却是两套：")
    print("    SELECT id, unnest(tags) FROM t -> （list：行数会变，见场景 7）")

    print("\n→ 一一对应，但「拆开」这一步的语义不同：")
    print("  · DuckDB 的 `unnest(struct)` 把 struct 摊平成**列**（列数变多，行数不变）；")
    print("    对 list 用 `unnest` 是把一行**炸成多行**（行数变多，列数不变）。")
    print("    同一个词，两种语义 —— 写 SQL 的时候特别容易看串。")
    print("  · Polars 分得很清：`struct.unnest()` 摊平成列，`list.explode()` 炸成多行。")
    print("    拆多个 struct 列时，Polars 把字段名**直接铺开**，不加前缀 ——")
    print("    两个 struct 有同名字段时会撞车，得自己 alias。")


# ---------------------------------------------------------------- 5. MAP


def scenario_map(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · MAP：键值访问，以及「Polars 的 MAP 得显式声明」")

    m = q1(con, f"SELECT {MAP_SQL}")
    keys_sql = f"SELECT map_keys({MAP_SQL})"
    values_sql = f"SELECT map_values({MAP_SQL})"
    extract_sql = f"SELECT map_extract({MAP_SQL}, 'k')"
    contains_sql = f"SELECT map_contains({MAP_SQL}, 'k')"
    entries_sql = f"SELECT map_entries({MAP_SQL})"
    print("  DuckDB 侧：")
    print(f"    MAP{{'k':1,'j':2}}       -> {m}")
    print(f"    map_keys({MAP_SQL})     -> {q1(con, keys_sql)}")
    print(f"    map_values({MAP_SQL})   -> {q1(con, values_sql)}")
    print(f"    map_extract(M, 'k')      -> {q1(con, extract_sql)}")
    print(f"    map_contains(M, 'k')     -> {q1(con, contains_sql)}")
    print(f"    map_entries({MAP_SQL})   -> {q1(con, entries_sql)}")
    print()
    print("  Polars 侧（注意 dtype 必须显式写）：")
    pmap = pl.Series("m", [{"k": 1, "j": 2}], dtype=pl.Map(pl.String, pl.Int64))
    print(f"    .map.keys()        -> {pmap.map.keys().to_list()}")
    print(f"    .map.values()      -> {pmap.map.values().to_list()}")
    print(f"    .map.get('k')      -> {pmap.map.get('k').to_list()}")
    print(f"    .map.get('zz')     -> {pmap.map.get('zz').to_list()}   ← 取不存在的键给 NULL")
    print(f"    .map.len()         -> {pmap.map.len().to_list()}")
    print(f"    .map.contains_key('k') -> {pmap.map.contains_key('k').to_list()}")
    print(f"    .map.entries()     -> {pmap.map.entries().to_list()}")

    print()
    print("  `map_entries` 两边的形状一模一样，可以直接对账：")
    duck_entries = con.sql(f"SELECT map_entries({MAP_SQL}) AS entries").pl()
    pol_entries = pmap.map.entries().to_frame("entries")
    same, reason = common.compare_frames(duck_entries, pol_entries, [])
    verdict = "✅ 一致" if same else "❌ 不一致"
    print(f"    DuckDB map_entries vs Polars .map.entries() -> {verdict}：{reason}")

    print()
    print("  键的顺序：两边都按**插入顺序**，不是排序后的顺序。")
    order_sql = "MAP{'b': 1, 'a': 2, 'c': 3}"
    print(f"    DuckDB map_keys({order_sql}) -> {q1(con, f'SELECT map_keys({order_sql})')}")
    print("    （有些数据库的 MAP 是按键排序的，DuckDB 不是 —— 依赖顺序的代码迁移时要留意）")

    print()
    print("  Polars 少写 dtype 会怎样：")
    wrong = pl.Series("m", [{"k": 1, "j": 2}])
    print(f"    pl.Series([{{'k':1}}]) 的 dtype -> {wrong.dtype}")
    try:
        wrong.map.keys()
    except Exception as exc:
        msg = str(exc).splitlines()[0]
        print(f"    再调 .map.keys() -> {type(exc).__name__}: {msg}")
    print("    → 是 Struct 不是 Map，`.map.keys()` 这类方法**全部用不了**。")
    print("      DuckDB 那边写 `MAP{...}` 就一定是 map，没有这个坑。")

    print("\n→ MAP 是三个嵌套类型里**最难对应**的一个：")
    print("  · DuckDB：MAP 是独立的一等类型，`map_keys` / `map_values` / `map_entries` 一整套。")
    print("  · Polars：MAP 是个**半成品** —— `.map` 命名空间只有 6 个方法，")
    print("    没有 `map_concat`、没有 `map_from_entries`、没有 `map_contains_value` 这种；")
    print("    实际干活时更常见的做法是**根本不用 Map dtype，直接拆成两列平行数组**。")
    print("  · 更实际的迁移路径：把 `MAP` 当成「键列 + 值列」拆开存，")
    print("    两边都能用普通算子处理，比在两边各维护一套 map API 划算。")


# ---------------------------------------------------------------- 6. Parquet 往返


def scenario_parquet(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 嵌套类型在 Parquet 里的往返：三种类型都活下来了")

    tmpdir = Path(tempfile.mkdtemp(prefix="nested_demo_"))
    duck_out = tmpdir / "from_duckdb.parquet"
    pol_out = tmpdir / "from_polars.parquet"

    con.execute(
        "COPY (SELECT 1 AS id, [10, 20] AS tags, {'a': 1, 'b': 2} AS st,"
        " MAP{'x': 1, 'y': 2} AS m) TO ? (FORMAT PARQUET)",
        [str(duck_out)],
    )
    back = con.sql("SELECT * FROM read_parquet(?)", params=[str(duck_out)]).fetchall()[0]
    print("  DuckDB 写出去再读回来：")
    print(f"    id={back[0]}  tags={back[1]}  st={back[2]}  m={back[3]}")
    print("    → 三种嵌套类型**原样回来**，list 的元素类型、struct 的字段名、map 的键值都没丢。")

    print()
    print("  同一个文件用 Polars 读：")
    pdf = pl.read_parquet(duck_out)
    for name, dtype in pdf.schema.items():
        print(f"    {common.pad(name, 6)}{dtype}")
    print(f"    tags 这一列的值：{pdf['tags'].to_list()}")

    print()
    print("  反方向：Polars 写一份三种类型都有的，交给 DuckDB 读：")
    pol_series = pl.Series("m", [{"x": 1, "y": 2}], dtype=pl.Map(pl.String, pl.Int64))
    pl.DataFrame(
        {
            "id": [1],
            "tags": [[10, 20]],
            "st": [{"a": 1, "b": "x"}],
            "m": pol_series,
        }
    ).write_parquet(pol_out)
    pol_back = con.sql("SELECT * FROM read_parquet(?)", params=[str(pol_out)]).fetchall()[0]
    print(f"    id={pol_back[0]}  tags={pol_back[1]}  st={pol_back[2]}  m={pol_back[3]}")
    desc = con.sql("DESCRIBE SELECT * FROM read_parquet(?)", params=[str(pol_out)]).fetchall()
    for row in desc:
        print(f"    列 {common.pad(row[0], 6)}DuckDB 读成 {row[1]}")

    print()
    print("  往返之后对账（值必须完全一样）：")
    duck_side = con.sql("SELECT id, tags, st FROM read_parquet(?)", params=[str(pol_out)]).pl()
    pol_side = pl.read_parquet(pol_out).select("id", "tags", "st")
    same, reason = common.compare_frames(duck_side, pol_side, ["id"])
    verdict = "✅ 一致" if same else "❌ 不一致"
    print(f"    DuckDB 读 Polars 写的文件 vs Polars 自己读 -> {verdict}：{reason}")

    print()
    print("  MAP 也一样能往返（这一步容易以为不行，其实行）：")
    duck_map = con.sql("SELECT m FROM read_parquet(?)", params=[str(pol_out)]).fetchall()[0][0]
    pol_map = pl.read_parquet(pol_out)["m"].to_list()[0]
    print(f"    DuckDB 读到的 map -> {duck_map}")
    print(f"    Polars 读到的 map -> {pol_map}")

    print("\n→ 这一段是好消息：")
    print("  · LIST / STRUCT / MAP 三种类型，**两个方向都能完整往返**，")
    print("    之前那些「DuckDB 是字面量、Polars 是 dtype」的差异，在文件格式这一层全部消失。")
    print("  · 所以嵌套类型迁移的最优解常常是：**别在内存里折腾类型，直接落 parquet 再读**。")
    print("  · 唯一要留意的是**元素类型**：DuckDB 写的是 INT32，Polars 读回来也是 Int32，")
    print("    但两边自己造数时的默认类型可能不同（Int64），对账前先看一眼 `schema`。")


# ---------------------------------------------------------------- 7. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 代价：把一个 list 列炸开要花多少时间")

    print(f"数据：{rows} 行，每行一个 5 元素的 list 列")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="nested_demo_"))
    src = tmpdir / "lists.parquet"
    con.execute(
        "COPY (SELECT i AS id, [i, i+1, i+2, i+3, i+4]::BIGINT[] AS tags"
        " FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)

    def duck_explode() -> int:
        sql = "SELECT count(*) FROM (SELECT id, unnest(tags) FROM read_parquet(?))"
        return int(con.sql(sql, params=[str(src)]).fetchall()[0][0])

    def polars_explode() -> int:
        return int(polars_src.explode("tags").height)

    truth = duck_explode()
    print(f"\n  炸开之后的行数（两边一致）：{truth:,}")

    results = [
        ("DuckDB unnest(tags)", duck_explode),
        ("Polars explode(tags)", polars_explode),
    ]

    print(
        "\n  "
        + common.pad("实现", 26)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("炸开行数", 18)
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

    print("\n口径：数据已物化成 parquet，两个引擎读的是**同一个文件**；")
    print("  计时的这一步包含「读 parquet + 解码 list 列 + 炸开 + 计数」的全过程，")
    print("  不包含造数。DuckDB 侧走 `unnest()`（SQL 里的行展开），")
    print("  Polars 侧走 `explode()`（DataFrame 方法），做的**是同一件事**：")
    print("  把每行的 list 拆成多行并数出总行数。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · 炸开 list 列是**行数放大**操作：每个 list 有几个元素，结果表就长几倍。")
    print("    这跟宽表 join 是同一类风险 —— 中间结果可能比输入大一个量级。")
    print("  · 两边的绝对耗时受 list 长度、编码方式、并行度影响，")
    print("    但**产出必须一样**（表里炸开行数两列相同），否则说明口径没对齐。")
    print("  · 如果只是想「每个 list 的元素和」，别炸开 ——")
    print("    DuckDB 的 `list_sum()` 和 Polars 的 `list.sum()` 都是**一趟扫完**，")
    print("    不产生放大后的中间结果（代价见场景 2 的空列表语义差异）。")
    print("  · 真要炸开，两边都得盯着中间结果占内存：")
    print("    DuckDB 内存不够会**落盘**继续跑，Polars 可能直接 OOM。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的嵌套类型差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_types(con)
        scenario_list(con)
        scenario_lambda(con)
        scenario_struct(con)
        scenario_map(con)
        scenario_parquet(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
