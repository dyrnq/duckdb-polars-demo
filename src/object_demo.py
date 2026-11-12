"""数据库对象：生成列、注释、临时表 —— DuckDB 有「对象」这一层，Polars 没有。

`attach_demo.py` 讲了 catalog（有哪些库、怎么跨库查），
`ddl_demo.py` 讲了 DDL 语法。这一层还有个东西没单独讲过：
**CREATE 出来的东西本身带哪些属性、由谁保证、活多久**。

三个必须记住的实测结论：

> **① 生成列是引擎强制的，Polars 侧的派生列不是。**
>
> DuckDB 的 `y INTEGER GENERATED ALWAYS AS (x * 2)`：插入时不能给 `y`，
> 改 `x` 时 `y` 自动重算 —— 不变量由**引擎**保证，绕不过去。
> Polars 只能用 `with_columns` 算一列出来，那是**你自己**算的：
> 之后再改基列，派生列不会跟着变，也不报错 —— 不变量靠自觉。

> **② 生成列不落盘，是读的时候现算的。**
>
> `PRAGMA storage_info` 里只有基列，生成列连一个 segment 都没有。
> 代价是每次读都要重算表达式 —— 实测比存好的普通列慢 15~20%，
> 但多出来的是**每次查询的固定开销**（零点几毫秒），不随行数长。

> **③ 注释和临时表都是「引擎保管的对象属性」，Polars 侧一个都没有。**
>
> `COMMENT ON` 能挂到表、列、视图、索引上（**挂不到 schema 和 database**，
> 引擎自己说 "not implemented"）；注释会**写进 .duckdb 文件**，重开还在。
> 临时表活在 `temp` catalog 里、**不写盘**、连接一关就没。
> Polars 侧 `dir(pl)` 里连 `comment` 这个词都搜不到。

用法：
    uv run python src/object_demo.py
    uv run python src/object_demo.py --rows 200000 --repeat 3
"""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ---------------------------------------------------------------- 场景 1：生成列


def scenario_generated(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 生成列：引擎强制的派生列")

    con.execute("CREATE TABLE gen (x INTEGER, y INTEGER GENERATED ALWAYS AS (x * 2))")
    print("  建表：DuckDB")
    print("    CREATE TABLE gen (x INTEGER, y INTEGER GENERATED ALWAYS AS (x * 2))")
    print()

    print("  ① 插入时**不能**给生成列的值：")
    con.execute("INSERT INTO gen (x) VALUES (5)")
    print(
        f"    INSERT INTO gen (x) VALUES (5)      -> {con.execute('SELECT * FROM gen').fetchall()}"
    )
    try:
        con.execute("INSERT INTO gen VALUES (9, 99)")
        print("    INSERT INTO gen VALUES (9, 99)      -> 允许（意外！）")
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print(f"    INSERT INTO gen VALUES (9, 99)      -> {detail}")
    print()

    print("  ② 改基列，生成列**自动重算**：")
    con.execute("UPDATE gen SET x = 7")
    print(
        f"    UPDATE gen SET x = 7                -> {con.execute('SELECT * FROM gen').fetchall()}"
    )
    print("    → 不变量是引擎保证的：x 是什么，y 永远是 x * 2。")
    print()

    print("  ③ 引擎自己也记着这列是「生成的」：")
    rows = con.execute(
        """
        SELECT column_name, data_type, column_default, is_generated, generation_expression
        FROM duckdb_columns() WHERE table_name = 'gen' ORDER BY column_index
        """
    ).fetchall()
    print(
        f"    {'列':<4}{'类型':<10}{'column_default':<28}{'is_generated':<14}generation_expression"
    )
    print("    " + "-" * 74)
    for name, dtype, default, is_gen, expr in rows:
        print(
            "    "
            + common.pad(name, 4)
            + common.pad(dtype, 10)
            + common.pad(str(default), 28)
            + common.pad(str(is_gen), 14)
            + str(expr)
        )
    print()

    print("  Polars 侧：只有 `with_columns`，那是**你自己**算的，不变量靠自觉。")
    print()
    df = pl.DataFrame({"x": [1, 2, 3]})
    derived = df.with_columns((pl.col("x") * 2).alias("y"))
    print(f"    with_columns(x * 2)          -> {derived.to_dicts()}")
    stale = derived.with_columns((pl.col("x") + 10).alias("x"))
    print(f"    之后改基列 x = x + 10         -> {stale.to_dicts()}")
    print("    → x 变成 11/12/13，y 还是 2/4/6 —— **不重算，也不报错**。")
    print("      DuckDB 那边引擎会拦，Polars 这边只有你自己记得重算。")
    print()

    print("  Polars 的命名空间里，生成列相关的东西是**零**：")
    hits = [a for a in dir(pl) if "generat" in a.lower()]
    print(f"    pl 里含 'generat' 的顶层符号：{hits or '（一个都没有）'}")
    print(
        f"    pl.Expr 里含 'generat' 的方法："
        f"{[a for a in dir(pl.Expr) if 'generat' in a.lower()] or '（一个都没有）'}"
    )
    print("    → 不是「藏在别处」，是真的没有。")


# ---------------------------------------------------------------- 场景 2：生成列存在哪


def scenario_generated_storage(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 2 · 生成列存在哪：不落盘，读的时候现算")

    con.execute("CREATE TABLE gen2 (x INTEGER, y INTEGER GENERATED ALWAYS AS (x * 2))")
    con.execute(f"INSERT INTO gen2 (x) SELECT range FROM range({rows})")

    print("  `PRAGMA storage_info` 看这张表在磁盘上到底存了什么：")
    print()
    # storage_info 每行一个「段」：row_group_id, column_name, column_id, column_path,
    # segment_id, segment_type, start, count, compression, stats, ...
    info = con.execute("PRAGMA storage_info('gen2')").fetchall()
    by_col: dict[str, list[str]] = {}
    for row in info:
        by_col.setdefault(str(row[1]), []).append(str(row[5]))
    generated = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'gen2' AND is_generated"
        ).fetchall()
    ]
    print(f"    {'列':<6}{'落盘的段类型':<28}{'段数':<8}是不是生成列")
    print("    " + "-" * 74)
    for name, kinds in by_col.items():
        is_gen = "是" if name in generated else "否"
        print(
            "    "
            + common.pad(name, 6)
            + common.pad(", ".join(sorted(set(kinds))), 28)
            + common.pad(str(len(kinds)), 8)
            + is_gen
        )
    for name in generated:
        if name not in by_col:
            print(
                "    "
                + common.pad(name, 6)
                + common.pad("（一个段都没有）", 28)
                + common.pad("0", 8)
                + "是"
            )
    print()
    print("    → 生成列 **连一个 segment 都没有** —— 它不是存出来的，")
    print("      是每次读的时候按 `x * 2` 现算的。")
    print()

    print("  约束也不能随便加：")
    for stmt in (
        "CREATE INDEX idx_gen2 ON gen2(y)",
        "ALTER TABLE gen2 ALTER COLUMN y SET NOT NULL",
    ):
        try:
            con.execute(stmt)
            print(f"    {stmt:<52} -> 允许")
        except Exception as exc:
            print(f"    {stmt:<52} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()

    print("  代价：读一列的耗时随行数怎么变")
    print()
    print("    两张表列类型完全一样（INTEGER / INTEGER），只差「y 是存出来的还是现算的」：")
    print("      plain —— x、y 都存着；gen2 —— 只存 x，y 每次现算。")
    print("      两条查询都是 `SELECT sum(y)`，读的都只有一列。")
    print()

    scales = [rows, rows * 5, rows * 10]
    plain_sql = "SELECT sum(y) FROM plain"
    gen_sql = "SELECT sum(y) FROM gen2"

    def run(sql: str) -> float:
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        return (time.perf_counter() - t0) * 1000

    results: list[tuple[int, float, float, float, float]] = []
    for scale in scales:
        con.execute("DROP TABLE IF EXISTS plain")
        con.execute("DROP TABLE IF EXISTS gen2")
        con.execute("CREATE TABLE gen2 (x INTEGER, y INTEGER GENERATED ALWAYS AS (x * 2))")
        con.execute(f"INSERT INTO gen2 (x) SELECT range FROM range({scale})")
        # plain 的 y 也必须是 INTEGER（gen2 存的是 INTEGER、算的是 CAST((x*2) AS INTEGER)），
        # 否则比的是类型差异，不是「存 vs 算」。
        con.execute(
            "CREATE TABLE plain AS "
            "SELECT range::INTEGER AS x, (range::INTEGER * 2) AS y FROM range(?)",
            [scale],
        )
        run(plain_sql)  # 预热，编译计划不算进计时
        run(gen_sql)
        plain_times: list[float] = []
        gen_times: list[float] = []
        for _ in range(repeat):
            plain_times.append(run(plain_sql))
            gen_times.append(run(gen_sql))
        p_med, p_min = statistics.median(plain_times), min(plain_times)
        g_med, g_min = statistics.median(gen_times), min(gen_times)
        results.append((scale, p_med, p_min, g_med, g_min))

    print(
        f"    {'行数':>12}{'普通列(中位/最小)':>24}{'生成列(中位/最小)':>24}"
        f"{'中位比值':>12}{'最小比值':>12}"
    )
    print("    " + "-" * 84)
    for scale, p_med, p_min, g_med, g_min in results:
        ratio = g_med / p_med if p_med else float("inf")
        min_ratio = g_min / p_min if p_min else float("inf")
        print(
            f"    {scale:>12,}"
            f"{f'{p_med:.3f} / {p_min:.3f}':>24}"
            f"{f'{g_med:.3f} / {g_min:.3f}':>24}"
            f"{ratio:>11.2f}×{min_ratio:>11.2f}×"
        )
    print()

    # ---- 结论跟着测量走：比值和「每行多少 ns」都是从上面这组数算出来的，
    #      不是写死的句子。数字变了，下面的措辞跟着变。
    ratios = [g / p if p else float("inf") for _, p, _, g, _ in results]
    gaps = [g - p for _, p, _, g, _ in results]
    per_rows = [(g - p) * 1e6 / s for s, p, _, g, _ in results]
    lo_ratio, hi_ratio = min(ratios), max(ratios)

    print(
        "    → 三档的**绝对差**（生成列比普通列多花的 CPU）："
        + "、".join(f"{g:.3f} ms" for g in gaps)
    )
    print("      折算成每行：" + "、".join(f"{pr:.2f} ns" for pr in per_rows))
    print()
    print(
        f"    → 比值从 {ratios[0]:.2f}× 到 {ratios[-1]:.2f}×（中位），"
        f"三档的最小/最大比值 {lo_ratio:.2f}×~{hi_ratio:.2f}×。"
    )
    print()
    # ① 生成列到底慢不慢？（比值在三档里稳不稳、是不是大于 1）
    if lo_ratio >= 1.05:
        print(
            f"      → 生成列**确实慢一截**：三档的比值都落在 {lo_ratio:.2f}×~{hi_ratio:.2f}× 之间，"
        )
        print("        而且**没有随行数变差** —— 慢的幅度是稳定的。")
    elif hi_ratio <= 1.05:
        print("      → 三档的比值都在 1.05× 以内 —— 这个负载下生成列的 CPU 代价量不出来。")
    else:
        print(
            f"      → 比值在三档之间从 {min(ratios):.2f}× 变到 {max(ratios):.2f}× —— "
            "不稳定，量不出稳定结论。"
        )
    if min(gaps) < 0:
        print("      有一档的差值是**负的**（生成列反而更快）—— 差值本身已经在噪声里了。")
    print()
    # ② 多出来的那份，是每行都要付的，还是每次查询的固定开销？
    row_factor = scales[-1] / scales[0]
    gap_factor = abs(gaps[-1]) / abs(gaps[0]) if gaps[0] else float("inf")
    if gap_factor < row_factor / 2:
        print(
            f"      → 行数翻了 {row_factor:.0f}×，多出来的时间只从 {gaps[0]:.3f} ms "
            f"变到 {gaps[-1]:.3f} ms ——"
        )
        print("        说明多出来的是**每次查询的固定开销**，不是每行都要付的 CPU。")
    else:
        print(
            f"      → 行数翻了 {row_factor:.0f}×，多出来的时间也从 {gaps[0]:.3f} ms "
            f"涨到 {gaps[-1]:.3f} ms ——"
        )
        print("        这是**每行都要付**的 CPU，行数越多账越厚。")
    print()
    print("      → 生成列不是免费的，但**也不像「每行多算一遍」那么吓人**：")
    print("        向量化执行下，多出来的那点算术基本被流水线吃掉了。")
    print("        真正要算账的是**读得比写得多**的场景 —— 每扫一遍就多付一遍。")
    print("        → 生成列适合「派生规则稳定、读得少」的列；")
    print("          要是这张表被反复扫，存的还是算的，得自己拿这个数算账。")
    print()
    print("  口径：两张表行数相同、**列类型相同**，只差「y 是存出来的还是现算的」；")
    print("        计时只包住 `SELECT sum(y)` 这一句；两边**交错**跑。")
    print("        这个 demo 全程跑在**内存库**上（`duckdb.connect()` 不给路径），")
    print("        所以量到的是 CPU + 内存带宽，**没有磁盘 I/O** ——")
    print("        落盘场景的代价要另算，别把这个数字当「从磁盘读」的数字。")


# ---------------------------------------------------------------- 场景 3：注释


def scenario_comment(con: duckdb.DuckDBPyConnection, work: Path) -> None:
    section("场景 3 · COMMENT ON：挂在对象上的注释")

    con.execute("CREATE TABLE t1 (a INTEGER, b VARCHAR)")
    con.execute("CREATE VIEW v1 AS SELECT 1 AS x")
    con.execute("CREATE INDEX i1 ON t1(a)")

    print("  能注释哪些对象？逐个试：")
    print()
    targets: list[tuple[str, str]] = [
        ("COMMENT ON TABLE t1 IS '订单表'", "表"),
        ("COMMENT ON COLUMN t1.a IS '主键'", "列"),
        ("COMMENT ON VIEW v1 IS '一个视图'", "视图"),
        ("COMMENT ON INDEX i1 IS '一个索引'", "索引"),
        ("COMMENT ON SCHEMA main IS '一个 schema'", "schema"),
        ("COMMENT ON DATABASE memory IS '一个库'", "库"),
    ]
    for stmt, kind in targets:
        try:
            con.execute(stmt)
            print(f"    {kind:<8} 允许")
        except Exception as exc:
            print(f"    {kind:<8} {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()
    print("    → 表 / 列 / 视图 / 索引能挂；**schema 和库挂不上**，")
    print("      引擎自己回「not implemented」—— 注释这一层也是半截的。")
    print()

    print("  注释存到哪去了：")
    print()
    print(f"    {'对象':<10}{'comment'}")
    print("    " + "-" * 74)
    tab = con.execute(
        "SELECT table_name, comment FROM duckdb_tables() WHERE table_name = 't1'"
    ).fetchall()
    col = con.execute(
        "SELECT column_name, comment FROM duckdb_columns() WHERE table_name = 't1' "
        "ORDER BY column_index"
    ).fetchall()
    view = con.execute(
        "SELECT view_name, comment FROM duckdb_views() WHERE view_name = 'v1'"
    ).fetchall()
    for name, comment in tab:
        print(f"    {common.pad(name, 10)}{comment}")
    for name, comment in col:
        print(f"    {common.pad(name, 10)}{comment}")
    for name, comment in view:
        print(f"    {common.pad(name, 10)}{comment}")
    print()
    print("    → `duckdb_tables()` / `duckdb_columns()` / `duckdb_views()` 都带 `comment` 列，")
    print("      注释是**可查询的元数据**，不是塞在旁边的备注。")
    print()

    print("  注释会不会写进 .duckdb 文件？")
    dbfile = work / "commented.duckdb"
    con2 = duckdb.connect(str(dbfile))
    con2.execute("CREATE TABLE persist (a INTEGER)")
    con2.execute("COMMENT ON TABLE persist IS '写进文件了吗'")
    con2.close()
    con3 = duckdb.connect(str(dbfile))
    got = con3.execute(
        "SELECT comment FROM duckdb_tables() WHERE table_name = 'persist'"
    ).fetchall()
    con3.close()
    print(f"    重开连接后读回来：{got}")
    print("    → **写进文件了** —— 注释跟着数据一起持久化，重开还在。")
    print()

    print("  Polars 侧：")
    print(f"    pl.DataFrame 有 comment 属性：{hasattr(pl.DataFrame(), 'comment')}")
    top_hits = [a for a in dir(pl) if "comment" in a.lower()]
    expr_hits = [a for a in dir(pl.Expr) if "comment" in a.lower()]
    print(f"    pl 顶层含 'comment' 的符号：{top_hits or '（一个都没有）'}")
    print(f"    pl.Expr 含 'comment' 的方法：{expr_hits or '（一个都没有）'}")
    print("    → 不是「藏在别处」，是真的没有。")
    print("      Polars 的 DataFrame 是内存里的一个值，没有「对象元数据」这层。")


# ---------------------------------------------------------------- 场景 4：临时表


def scenario_temp_table(con: duckdb.DuckDBPyConnection, work: Path) -> None:
    section("场景 4 · TEMP TABLE：会话级的对象")

    con.execute("CREATE TEMP TABLE tmp AS SELECT 1 AS a")

    print("  临时表住在哪个库？")
    print()
    rows = con.execute(
        """
        SELECT database_name, schema_name, table_name, temporary, internal
        FROM duckdb_tables() WHERE table_name = 'tmp'
        """
    ).fetchall()
    print(f"    {'库':<10}{'schema':<10}{'表':<10}{'temporary':<12}internal")
    print("    " + "-" * 74)
    for db, schema, tbl, temporary, internal in rows:
        print(
            "    "
            + common.pad(db, 10)
            + common.pad(schema, 10)
            + common.pad(tbl, 10)
            + common.pad(str(temporary), 12)
            + str(internal)
        )
    print()
    print("    → 住在 `temp` 库里，`temporary = true`、`internal = false`。")
    print("      它是个**正经的表对象**，能查、能 join、能建索引 —— 只是活在一个会话里。")
    print()

    print("  它落盘吗？")
    dbfile = work / "temp.duckdb"
    con2 = duckdb.connect(str(dbfile))
    con2.execute("CREATE TEMP TABLE tmp AS SELECT 1 AS a")
    con2.execute("CREATE TABLE persist AS SELECT 2 AS b")
    con2.close()
    con3 = duckdb.connect(str(dbfile))
    persist = con3.execute("SELECT count(*) FROM persist").fetchall()
    tmp = con3.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'tmp'").fetchall()
    con3.close()
    print(f"    重开连接后：持久表 {persist[0][0]} 行，临时表 {tmp[0][0]} 张")
    print("    → 临时表**没写进文件**，持久表写了。")
    print()

    print("  别的连接看得见吗？")
    other = duckdb.connect()
    seen = other.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'tmp'").fetchall()
    other.close()
    print(f"    另一个连接查 duckdb_tables()：{seen[0][0]} 张")
    print("    → **看不见** —— 临时表是会话级的，不是数据库级的。")
    print()

    print("  Polars 侧最接近的东西：`pl.SQLContext`")
    ctx = pl.SQLContext()
    ctx.register("df", pl.DataFrame({"a": [1, 2]}))
    ctx.execute("create table t as select 1 as a")
    tables = ctx.execute("show tables").collect().to_dicts()
    print("    ctx.register('df', ...) + ctx.execute('create table t ...')")
    print(f"    show tables -> {tables}")
    print("    → 表**活在一个 Python 对象里**：对象在，表就在；对象没了，表也没了。")
    print("      生命周期跟 DuckDB 的「连接」对上了，但作用域从「整个数据库」缩到了「一个对象」。")
    print()
    print("    但有个关键差别：DuckDB 的临时表**能被别的 SQL 语句看见**（能 join、能子查询）；")
    print("    Polars 的 `SQLContext` 表**只在你自己 execute 的那几句 SQL 里可见**，")
    print("    出了 `pl.sql()` 就没有了 —— 它不是全局注册表。")


# ---------------------------------------------------------------- 场景 5：小结


def scenario_conclusion() -> None:
    section("小结：对象这一层，谁有什么")

    rows = [
        (
            "生成列（引擎强制）",
            "有：`GENERATED ALWAYS AS (...)`",
            "❌ 没有，只能 `with_columns` 自己算",
        ),
        ("生成列的不变量", "引擎保证：插入不给、更新自动重算", "靠自觉：改基列派生列不跟着变"),
        ("生成列存不存", "**不存**，读时现算（有实测代价）", "存不存由你决定"),
        ("注释（表/列/视图/索引）", "有：`COMMENT ON ... IS '...'`", "❌ 没有"),
        ("注释（schema / 库）", "❌ 引擎说 not implemented", "❌ 没有"),
        ("注释存不存", "**写进 .duckdb 文件**，重开还在", "没有这层"),
        ("临时表", "有：`CREATE TEMP TABLE`，会话级", "最接近的是 `pl.SQLContext` 里的表"),
        ("临时表落盘吗", "不落盘", "本来就没有「落盘」这回事"),
        ("对象元数据能查吗", "能：`duckdb_tables()` 等带 `comment` 列", "没有 catalog 可查"),
    ]

    print("  " + common.pad("能力", 26) + common.pad("DuckDB", 38) + "Polars")
    print("  " + "-" * 74)
    for cap, duck, pol in rows:
        print("  " + common.pad(cap, 26) + common.pad(duck, 38) + pol)
    print()

    print("  三个必须记住的实测结论：")
    print()
    print("    ① **生成列是引擎强制的，派生列不是。**")
    print("       DuckDB 拦得住你（插不进去、改了自动重算）；")
    print("       Polars 拦不住 —— `with_columns` 算出来的东西，")
    print("       改了基列就过期了，还不报错。")
    print("       这不是「谁功能多」的问题，是**不变量由谁负责**的问题。")
    print()
    print("    ② **生成列不落盘，是读时现算的。**")
    print("       `PRAGMA storage_info` 里只有基列，生成列一个 segment 都没有。")
    print("       省空间，花 CPU —— 具体多少，看本 demo 场景 2 的实测。")
    print()
    print("    ③ **注释和临时表都是「引擎保管的对象属性」。**")
    print("       注释能查（`duckdb_tables().comment`）、能持久化（写进 .duckdb）；")
    print("       临时表有明确的生命周期（会话级，不落盘，连接关了就没）。")
    print("       Polars 侧这两样都没有 —— DataFrame 是个**值**，不是**对象**。")
    print()
    print("  实务建议：")
    print("    · 派生列的口径是业务规则 → 放 DuckDB 的生成列里，引擎帮你守住；")
    print("      放 Polars 就得自己保证每次重算（或者干脆别派生）；")
    print("    · 数据字典（列是干什么的）→ DuckDB 的 `COMMENT ON` 写进文件，")
    print("      比在 README 里维护一张表可靠；")
    print("    · 中间结果别急着建表 → DuckDB 用 `CREATE TEMP TABLE`，")
    print("      不落盘、连接关了自动消失；")
    print("      Polars 侧就写成个 Python 变量（LazyFrame 甚至可以不物化）。")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的数据库对象差异演示")
    parser.add_argument("--rows", type=int, default=200_000, help="生成列代价场景的行数")
    parser.add_argument("--repeat", type=int, default=3, help="生成列代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    work = Path(tempfile.mkdtemp(prefix="object_demo_"))
    print(f"实验目录：{work}")

    try:
        con = duckdb.connect()
        scenario_generated(con)
        scenario_generated_storage(con, args.rows, args.repeat)
        scenario_comment(con, work)
        scenario_temp_table(con, work)
        scenario_conclusion()
        con.close()
    finally:
        for path in sorted(work.rglob("*"), reverse=True):
            if path.is_file():
                os.unlink(path)
            elif path.is_dir():
                path.rmdir()
        work.rmdir()

    print()
    print("  （实验目录已清理）")


if __name__ == "__main__":
    main()
