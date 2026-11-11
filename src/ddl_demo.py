"""视图、宏与持久化对象：DuckDB 有「有名字、能落盘」的数据库对象，Polars 没有。

要点：
* DuckDB：`CREATE VIEW` / `CREATE MACRO` / `CREATE SEQUENCE` / `CREATE TRIGGER`
  是**数据库级**的命名对象 —— 存在 `.duckdb` 文件里，换个连接、换个进程再打开还在。
  但**视图不是物化的**：它存的是一段 SQL，查询时展开重算。
  DuckDB 2.0 **没有 `CREATE MATERIALIZED VIEW`**（ParserException）。
* Polars：`LazyFrame` 是「内存里的惰性计划」，可以当视图用，但**没有名字、不落盘**；
  「宏」对应的是 Python 函数；序列、触发器一律没有。

这个场景要回答：迁到 Polars 之后，DuckDB 里那些「建一次、用很多次」的对象
各自换成什么，以及哪些是 Polars 根本接不住的。

用法：
    uv run python src/ddl_demo.py
    uv run python src/ddl_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import math
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl
from duckdb.sqltypes import DOUBLE

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """连一个数据库文件（给 None 就是内存库）。"""
    if db_path is None:
        return duckdb.connect()
    return duckdb.connect(str(db_path))


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def seed(con: duckdb.DuckDBPyConnection) -> None:
    """造一张小小的销售表（场景 1～7 共用）。"""
    con.execute("CREATE TABLE sales (id INTEGER, region VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO sales VALUES "
        "(1,'华东',120.0), (2,'华北',80.0), (3,'华东',200.0), "
        "(4,'华南',150.5), (5,'华北',60.0), (6,'华东',310.0)"
    )


# ---------------------------------------------------------------- 1. 视图


def scenario_view(db_path: Path) -> None:
    section("场景 1 · 视图：有名字、存进 .duckdb 文件，重连之后还在")

    con = connect(db_path)
    seed(con)
    con.execute("CREATE VIEW v_big AS SELECT id, region, amount FROM sales WHERE amount >= 100")

    print("  建完视图立刻查：")
    for row in con.sql("SELECT * FROM v_big ORDER BY id").fetchall():
        print(f"    {row}")

    rows = con.sql(
        "SELECT sql FROM duckdb_views() WHERE view_name = 'v_big' AND internal = false"
    ).fetchall()
    print(f"\n  视图的定义存在系统表 duckdb_views() 里：\n    {rows[0][0]}")
    con.close()

    # ---- 重连：换个连接打开同一个文件
    con2 = connect(db_path)
    after = con2.sql("SELECT * FROM v_big ORDER BY id").fetchall()
    print("\n  关掉连接、重新打开同一个 .duckdb 文件，再查：")
    for row in after:
        print(f"    {row}")
    n = q1(
        con2,
        "SELECT count(*) FROM duckdb_views() WHERE view_name = 'v_big' AND internal = false",
    )
    print(f"    duckdb_views() 里还有 {n} 条同名视图 —— 视图是**数据库级**的，不是连接级的。")
    con2.close()

    # ---- Polars 侧
    df = pl.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "region": ["华东", "华北", "华东", "华南", "华北", "华东"],
            "amount": [120.0, 80.0, 200.0, 150.5, 60.0, 310.0],
        }
    )
    lf = df.lazy().filter(pl.col("amount") >= 100)
    print("\n  Polars 侧最接近「视图」的是 LazyFrame：")
    print("    lf = df.lazy().filter(pl.col('amount') >= 100)")
    print(f"    lf.collect() -> {lf.collect().height} 行")
    print("    但它跟 DuckDB 的视图差着三样东西：**没有名字、只在内存里、进程退出就没了**。")
    print("    （Polars 2.1 有 `LazyFrame.serialize()`，能存下的是**执行计划**，")
    print("      不是一段你能读懂、能改的 SQL 文本，也没法在数据库里注册。）")

    print("\n→ 两条要记住的：")
    print("  · DuckDB 的 `CREATE VIEW` 是**持久对象**：写进 `.duckdb` 文件，重连还在。")
    print("    对账：上面两次查询拿到的是同一批行，只是隔了一次关库重开。")
    print("  · Polars 没有「命名对象」这个概念。`LazyFrame` 是个变量名，不是对象名；")
    print("    想留下来只能自己写文件（parquet / IPC），那是**数据**，不是**视图**。")


# ---------------------------------------------------------------- 2. 视图是配方


def scenario_view_is_recipe(db_path: Path) -> None:
    section("场景 2 · 反直觉：视图存的是「配方」不是「结果」，EXPLAIN 里没有 VIEW 节点")

    con = connect(db_path)

    plan_view = con.sql("EXPLAIN SELECT * FROM v_big").fetchall()[0][1]
    plan_base = con.sql(
        "EXPLAIN SELECT id, region, amount FROM sales WHERE amount >= 100"
    ).fetchall()[0][1]

    print("  查视图 v_big 的执行计划：")
    print(plan_view)
    print("\n  把视图的 SQL 原样展开、手写一遍同样的查询，再看计划：")
    print(plan_base)

    print(f"\n  两段计划**逐字符相同** -> {plan_view == plan_base}")
    print("  注意计划里有一行 `Filters: amount >= 100.0` ——")
    print("  视图里那句 WHERE 被**原样贴进**了查询计划，压根没有「查一个叫 v_big 的东西」这一步。")

    n_scan = plan_view.count("Seq Scan")
    n_view = plan_view.upper().count("VIEW")
    print(f"\n  计划里的节点数：Seq Scan {n_scan} 个，提到 VIEW 的地方 {n_view} 处。")

    con.close()

    print("\n→ 这一条是本 demo 里最反直觉的：")
    print("  · **DuckDB 的视图不是「存下来的结果」，是「存下来的 SQL 文本」。**")
    print("    每次查它，底下的表都要重新扫一遍 —— 视图**不省任何计算**，只省打字。")
    print("  · 所以「建了视图查询就快了」是错觉：")
    print("    DuckDB 里快的是**物化成一张真表**（场景 5），不是视图。")
    print("  · Polars 的 LazyFrame 恰恰相反：它是**惰性计划**，collect 之前一行都不算，")
    print("    但也没有名字 —— 两边都是「配方」，DuckDB 的配方能存进文件，Polars 的不能。")


# ---------------------------------------------------------------- 3. 宏


def scenario_macro(db_path: Path) -> None:
    section("场景 3 · 宏：带参数的 SQL 函数，同样能落盘")

    con = connect(db_path)
    con.execute("CREATE MACRO add_tax(x, rate) AS x * (1 + rate)")
    con.execute(
        "CREATE MACRO tbl_top(n) AS TABLE "
        "SELECT id, region, amount FROM sales ORDER BY amount DESC LIMIT n"
    )
    con.execute("CREATE MACRO add_bonus(x, bonus := 10) AS x + bonus")

    print("  标量宏（带参数、带默认参数）：")
    print(f"    SELECT add_tax(100, 0.1)     -> {q1(con, 'SELECT add_tax(100, 0.1)')}")
    bonus_default = q1(con, "SELECT add_bonus(5)")
    print(f"    SELECT add_bonus(5)         -> {bonus_default}  （用默认的 bonus=10）")
    print(f"    SELECT add_bonus(5, 99)     -> {q1(con, 'SELECT add_bonus(5, 99)')}")

    print("\n  表宏（返回一张表，可以像子查询一样用）：")
    for row in con.sql("SELECT * FROM tbl_top(2)").fetchall():
        print(f"    {row}")
    joined = con.sql("SELECT count(*) FROM sales s JOIN tbl_top(2) t ON s.id = t.id").fetchall()
    print(f"    能直接参与 JOIN -> {joined[0][0]} 行")

    print("\n  宏在系统表里长这样（duckdb_functions()，function_type 区分宏和普通函数）：")
    rows = con.sql(
        "SELECT function_name, function_type, parameters, macro_definition "
        "FROM duckdb_functions() "
        "WHERE function_name IN ('add_tax', 'tbl_top', 'add_bonus')"
    ).fetchall()
    for name, ftype, params, body in rows:
        print(
            f"    {common.pad(str(name), 12)}{common.pad(str(ftype), 13)}"
            f"{common.pad(str(params), 22)}{body}"
        )
    print("    （DuckDB 2.0 没有 duckdb_macros() 这张表 —— 宏混在 duckdb_functions() 里，")
    print("      用 function_type = 'macro' / 'table_macro' 认出来。）")
    con.close()

    # ---- 重连
    con2 = connect(db_path)
    print("\n  重连之后：")
    print(f"    SELECT add_tax(100, 0.1) -> {q1(con2, 'SELECT add_tax(100, 0.1)')}")
    print(f"    tbl_top(2) 行数          -> {q1(con2, 'SELECT count(*) FROM tbl_top(2)')}")
    con2.close()

    print("\n→ 两条要记住的：")
    print("  · DuckDB 的宏是**数据库级**的：跨连接、跨进程都在，而且能带类型、能返回表。")
    print("  · Polars 对应的是**普通 Python 函数**：")
    print("    `def add_tax(x, rate): return x * (1 + rate)`。")
    print("    区别是：Python 函数活在一个进程里，没有名字、不能被 SQL 文本引用；")
    print("    而 DuckDB 的宏写在 `.duckdb` 文件里，任何连接都能 `add_tax(...)`。")


# ---------------------------------------------------------------- 4. 序列


def scenario_sequence(db_path: Path) -> None:
    section("场景 4 · 序列：`nextval` 的号是连续的，跨连接接着往下发")

    con = connect(db_path)
    con.execute("CREATE SEQUENCE seq START 1")
    first = con.sql("SELECT nextval('seq')").fetchall()[0][0]
    second = con.sql("SELECT nextval('seq')").fetchall()[0][0]
    print(f"  同一个连接里连发两个号：{first}, {second}")

    rows = con.sql(
        "SELECT sequence_name, start_value, increment_by, last_value FROM duckdb_sequences()"
    ).fetchall()
    print("\n  duckdb_sequences() 里能看到它：")
    for name, start, inc, last in rows:
        print(f"    name={name}  start={start}  increment={inc}  last_value={last}")

    con.close()

    con2 = connect(db_path)
    third = con2.sql("SELECT nextval('seq')").fetchall()[0][0]
    print(f"\n  关掉连接、重连，再 nextval 一次 -> {third}")
    print("  → 序列**不是连接级的**，号是存在数据库文件里的，重连不会从头开始。")
    con2.close()

    print("\n  Polars 侧：")
    print("    没有序列，也没有 `nextval`。")
    print("    要一列连续 ID，常见两种做法：")
    print("      · `df.with_row_index('n')` —— 给当前**已有的行**编个号（从 0 开始）")
    print("      · `pl.int_range(1, n+1)` —— 现造一列号")
    idx = pl.DataFrame({"a": [10, 20, 30]}).with_row_index("n")
    print(f"      df.with_row_index('n') -> {idx['n'].to_list()}")
    print("    但它们都只是**当场算出来的一个列**，不是数据库对象：")
    print("    没有「下一个号」的概念，也就没有 `nextval('seq')` 这种取号方式。")

    print("\n→ 一条要记住的：")
    print("  · DuckDB 的 `CREATE SEQUENCE` 是一个**持久化的计数器**，")
    print("    它的状态（发到第几个了）跟着 `.duckdb` 文件走。")
    print("  · 这是「数据库级对象」和「进程里的一个变量」最干净的一组对照 ——")
    print("    一个 `nextval` 的差别，底下是两套完全不同的状态管理模型。")


# ---------------------------------------------------------------- 5. 物化视图


def scenario_no_materialized_view(db_path: Path) -> None:
    section("场景 5 · DuckDB 2.0 没有物化视图：替代方案是「物化成一张真表」")

    con = connect(db_path)
    print("  试一下 DuckDB 2.0 最有名的那个东西：")
    try:
        con.execute("CREATE MATERIALIZED VIEW mv AS SELECT count(*) c FROM sales")
        print("    -> 居然成功了？")
    except Exception as exc:
        print(f"    -> [{type(exc).__name__}]")
        for line in str(exc).splitlines():
            print(f"       {line}")

    print("\n  也就是说：2.0 的解析器在 `MATERIALIZED` 这个词上就断了，")
    print(
        "  根本不是「不支持」而是「不认识」—— 连 `CREATE MATERIALIZED VIEW` 这串 token 都不是语法。"
    )

    print("\n  替代方案一：直接物化成一张真表（CTAS）")
    con.execute(
        "CREATE TABLE mv AS SELECT region, count(*) AS cnt, sum(amount) AS total "
        "FROM sales GROUP BY region"
    )
    for row in con.sql("SELECT * FROM mv ORDER BY region").fetchall():
        print(f"    {row}")
    rows = con.sql("SELECT table_name FROM duckdb_tables() WHERE table_name = 'mv'").fetchall()
    print(f"    duckdb_tables() 里它就是一张普通表：{rows[0][0]}")
    print("    （DuckDB 没有「这是物化视图」的标记 —— 物化之后视图和表就分不出来了，")
    print("      你得自己记住「这张表是定时刷新的」，或者干脆起个定时任务重建它。）")

    print("\n  替代方案二：物化成 parquet 文件，定时覆盖写")
    out = db_path.parent / "mv.parquet"
    con.execute(
        "COPY (SELECT region, count(*) AS cnt, sum(amount) AS total "
        "FROM sales GROUP BY region) TO ? (FORMAT PARQUET)",
        [str(out)],
    )
    print(f"    写出了 {out.name}，{out.stat().st_size} 字节\n")
    print("    刷新就是重新 COPY 一次（`COPY ... TO` 是覆盖写，不是追加）。")

    print("\n→ 两条要记住的：")
    print("  · **DuckDB 2.0 没有 `CREATE MATERIALIZED VIEW`** ——")
    print("    从 Postgres / SQLite 迁过来的人第一反应就是找它，找不到。")
    print("    替代品是 `CREATE TABLE ... AS SELECT`（一张普通表）或者 `COPY ... TO parquet`，")
    print("    刷新逻辑要自己写。")
    print("  · Polars 侧连「视图」都没有，物化就是**写一个 parquet 文件**：")
    print("    `df.write_parquet(path)`。能力上更少，但路径上没有惊喜 ——")
    print("    你不会误以为存在某个自动刷新的对象。")

    con.close()


# ---------------------------------------------------------------- 6. 触发器


def scenario_trigger(db_path: Path) -> None:
    section("场景 6 · 反直觉：DuckDB 2.0 其实**有**触发器 —— 但限制很多")

    con = connect(db_path)
    con.execute("CREATE TABLE audit (n INTEGER)")

    print("  能建的那种（语句级、AFTER）：")
    con.execute(
        "CREATE TRIGGER tg_insert AFTER INSERT ON sales "
        "FOR EACH STATEMENT INSERT INTO audit VALUES (1)"
    )
    print("    CREATE TRIGGER tg_insert AFTER INSERT ON sales FOR EACH STATEMENT ... -> 成功")

    con.execute("INSERT INTO sales VALUES (7,'西南',90.0)")
    print(f"    插一行之后 audit 表里有 {q1(con, 'SELECT count(*) FROM audit')} 条")

    print("\n  不能建的那种（行级、BEFORE）：")
    try:
        con.execute(
            "CREATE TRIGGER tg_row BEFORE INSERT ON sales FOR EACH ROW INSERT INTO audit VALUES (2)"
        )
        print("    -> 成功？")
    except Exception as exc:
        print(f"    -> [{type(exc).__name__}] {str(exc).splitlines()[0]}")

    print("\n  触发器也能在系统表里看到：")
    for row in con.sql(
        "SELECT trigger_name, action_timing, event_manipulation, for_each FROM duckdb_triggers()"
    ).fetchall():
        print(f"    {row}")

    con.close()

    con2 = connect(db_path)
    n = q1(con2, "SELECT count(*) FROM duckdb_triggers()")
    print(f"\n  重连之后 duckdb_triggers() 里还有 {n} 条 —— 也是数据库级的持久对象。")
    con2.close()

    print("\n  Polars 侧：没有触发器，也没有等价物。")
    print("    想在「写入时」做点什么，只能自己在代码里串起来：")
    print("      `df.write_parquet(p); audit.write_parquet(a)`")
    print("    没有「声明式」这一层 —— 触发器的价值恰恰在于它是数据库自己保证的。")

    print("\n→ 一条要记住的：")
    print("  · **DuckDB 2.0 是有 `CREATE TRIGGER` 的**（`AFTER ... FOR EACH STATEMENT` 可用），")
    print("    但 `BEFORE ... FOR EACH ROW` 会直接抛 `NotImplementedException`。")
    print("  · 所以「DuckDB 不支持触发器」这句话在 2.0 上是**不准确**的 ——")
    print("    准确的说法是：**只实现了触发器的一个子集**。")
    print("  · Polars 连这个子集都没有。这不是「功能少一点」，是整个「数据库替你执行」")
    print("    这件事在 Polars 里不存在。")


# ---------------------------------------------------------------- 7. 持久化边界


def scenario_persistence_boundary(db_path: Path) -> None:
    section("场景 7 · 持久化的边界：宏能落盘，Python UDF 不能")

    con = connect(db_path)

    def py_double(x: float) -> float:
        return x * 2

    con.create_function("py_double", py_double, [DOUBLE], DOUBLE)
    print("  注册一个 Python UDF（连接级）：")
    print(f"    SELECT py_double(21.0) -> {q1(con, 'SELECT py_double(21.0)')}")
    rows = con.sql(
        "SELECT function_name, function_type FROM duckdb_functions() "
        "WHERE function_name = 'py_double'"
    ).fetchall()
    print(f"    duckdb_functions() 里：{rows[0][0]} / function_type = {rows[0][1]}")

    con.close()

    con2 = connect(db_path)
    print("\n  关掉连接、重连同一个文件：")
    try:
        print(f"    SELECT py_double(21.0) -> {q1(con2, 'SELECT py_double(21.0)')}")
    except Exception as exc:
        print(f"    SELECT py_double(21.0) -> [{type(exc).__name__}] {str(exc).splitlines()[0]}")
    print(f"    SELECT add_tax(100, 0.1) -> {q1(con2, 'SELECT add_tax(100, 0.1)')}  （宏还在）")
    con2.close()

    print("\n  归纳一下这条边界：")
    print("    " + common.pad("对象", 28) + common.pad("落在哪", 22) + "重连后还在吗")
    print("    " + "-" * 62)
    for obj, where, alive in [
        ("CREATE TABLE / VIEW", ".duckdb 文件", "在"),
        ("CREATE MACRO / SEQUENCE", ".duckdb 文件", "在"),
        ("CREATE TRIGGER", ".duckdb 文件", "在"),
        ("Python UDF", "连接内存", "不在"),
        ("SET memory_limit / threads", "连接内存", "不在"),
    ]:
        print("    " + common.pad(obj, 28) + common.pad(where, 22) + alive)

    print("\n→ 两条要记住的：")
    print("  · 「数据库级」和「连接级」的分界线在 DuckDB 里非常清楚：")
    print("    **写进 catalog 的东西跨进程，写进连接状态的东西不跨进程。**")
    print("  · 选宏还是选 Python UDF，就看你想让它活多久：")
    print("    · 宏 —— 纯 SQL 表达式，跨进程、跨连接都能用，别的工具（CLI、DBeaver）也能调。")
    print("    · Python UDF —— 能做宏做不了的事（调 HTTP、读文件、跑正则引擎），")
    print("      但它绑在这个进程上；而且 DuckDB 对 Python UDF 是**逐行**调用的，")
    print("      慢得不是一个量级 —— 这才是它真正的代价，不是「不能持久化」。")


# ---------------------------------------------------------------- 8. 性能


def scenario_performance(rows: int, repeat: int) -> None:
    section("场景 8 · 代价：视图每次重算，物化算一次")

    print(f"数据：{rows} 行（造一张宽一点的表，让聚合真的有活干）")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="ddl_demo_"))
    src = tmpdir / "base.parquet"
    con0 = connect()
    con0.execute(
        "COPY (SELECT i % 7 AS region, i % 1000 AS bucket, i::DOUBLE * 1.5 AS amt, "
        "i % 97 AS qty FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    con0.close()
    print(f"物化成 parquet：{src.stat().st_size / 1e6:.1f} MB")

    agg = (
        "SELECT region, count(*) AS cnt, sum(amt) AS total "
        "FROM base WHERE amt > 100 GROUP BY region ORDER BY region"
    )

    db_path = tmpdir / "perf.duckdb"
    con = connect(db_path)
    con.execute("CREATE TABLE base AS SELECT * FROM read_parquet(?)", [str(src)])

    def timed(fn, reps: int) -> tuple[float, object]:
        fn()
        times: list[float] = []
        value = None
        for _ in range(reps):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        return statistics.median(times), value

    # ---- DuckDB：视图
    t_def_view, _ = timed(lambda: con.execute("CREATE OR REPLACE VIEW v AS " + agg), 3)
    t_view, duck_view_rows = timed(lambda: con.sql("SELECT * FROM v").fetchall(), repeat)

    # ---- DuckDB：物化成真表
    t_def_mat, _ = timed(lambda: con.execute("CREATE OR REPLACE TABLE mt AS " + agg), 3)
    t_mat, duck_mat_rows = timed(lambda: con.sql("SELECT * FROM mt").fetchall(), repeat)

    # ---- Polars：LazyFrame（= 视图的对应物）
    lf = (
        pl.scan_parquet(src)
        .filter(pl.col("amt") > 100)
        .group_by("region")
        .agg(pl.len().alias("cnt"), pl.col("amt").sum().alias("total"))
        .sort("region")
    )
    t_def_lf, _ = timed(lambda: lf, 3)
    t_lf, polars_lf = timed(lambda: lf.collect(), repeat)

    # ---- Polars：物化成 parquet（= 真表的对应物）
    mat_path = tmpdir / "mat.parquet"
    t_def_pmat, _ = timed(lambda: lf.collect().write_parquet(mat_path), 3)
    t_pmat, polars_pmat = timed(lambda: pl.read_parquet(mat_path), repeat)

    rows_out = [
        ("DuckDB 视图", t_def_view, t_view),
        ("DuckDB 物化表 (CTAS)", t_def_mat, t_mat),
        ("Polars LazyFrame", t_def_lf, t_lf),
        ("Polars 物化 parquet", t_def_pmat, t_pmat),
    ]
    print(
        "\n  "
        + common.pad("实现", 24)
        + common.pad_left("定义成本 (ms)", 15)
        + common.pad_left("单次查询 (ms)", 15)
        + common.pad_left("N 次总计 (ms)", 15)
    )
    print("  " + "-" * 70)
    for label, t_def, t_use in rows_out:
        total = t_def + t_use * repeat
        print(
            "  "
            + common.pad(label, 24)
            + common.pad_left(f"{t_def * 1000:.1f}", 15)
            + common.pad_left(f"{t_use * 1000:.1f}", 15)
            + common.pad_left(f"{total * 1000:.1f}", 15)
        )

    # ---- 对账：四种实现算出来的必须是同一个结果
    schema = [("region", pl.Int64), ("cnt", pl.Int64), ("total", pl.Float64)]
    duck_view_df = pl.DataFrame(duck_view_rows, schema=schema, orient="row")
    duck_mat_df = pl.DataFrame(duck_mat_rows, schema=schema, orient="row")
    ok1, why1 = common.compare_frames(duck_view_df, duck_mat_df, ["region"])
    ok2, why2 = common.compare_frames(duck_view_df, polars_lf, ["region"])
    ok3, why3 = common.compare_frames(duck_view_df, polars_pmat, ["region"])
    print(f"\n  对账（{repeat} 次重复后取最后一次结果）：")
    print(f"    DuckDB 视图 vs DuckDB 物化表 -> {'✅' if ok1 else '❌'} {why1}")
    print(f"    DuckDB 视图 vs Polars LazyFrame -> {'✅' if ok2 else '❌'} {why2}")
    print(f"    DuckDB 视图 vs Polars 物化 parquet -> {'✅' if ok3 else '❌'} {why3}")

    # ---- 回本点
    save_per_query = t_view - t_mat
    break_even = math.ceil(t_def_mat / save_per_query) if save_per_query > 0 else 0
    print(f"\n  回本点：物化表定义一次要 {t_def_mat * 1000:.1f} ms，")
    print(f"  每次查询比视图省 {save_per_query * 1000:.1f} ms ——")
    print(f"  查 {break_even} 次以后，物化才比视图划算。")

    con.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n口径：输入是同一份 parquet，四种实现算的是同一个聚合")
    print("  （按 region 分组、过滤 amt > 100、算条数和求和），结果已对账一致。")
    print("  「定义成本」= 建一次对象的时间；「单次查询」= 拿到结果一次的时间，")
    print("  两边都**包含读 parquet**（DuckDB 走 read_parquet，Polars 走 scan_parquet）。")
    print("  「N 次总计」= 定义成本 + 单次查询 × 重复次数，取中位数。")
    print("  Polars LazyFrame 的「定义成本」接近 0，是因为它本来就是惰性的 ——")
    print("  表达式只被记下来，一行数据都还没算。")
    print("  DuckDB 的物化表存在 .duckdb 文件里、Polars 的物化写在 parquet 文件里 ——")
    print("  两者都是「落盘的物化」，是对等的；DuckDB 物化表还带一次从库里读回的开销。")
    print("解读：")
    print("  · **视图不省计算，只省打字。** 视图那一行的「单次查询」")
    print("    跟 LazyFrame 那一行是同一量级 —— 两边都是每次把底层重新扫一遍。")
    print("  · 真正省时间的是**物化**：把「扫 + 过滤 + 聚合」做一次存下来，")
    print("    之后每次查询只剩下读那个已经很小的结果。")
    print("  · 所以「要不要物化」是个纯算术题：物化成本除以每次省下的时间，")
    print("    就是上面那个回本点。查得少就别物化，查得多就值得。")
    print("  · DuckDB 2.0 没有 `CREATE MATERIALIZED VIEW` 这条命令，")
    print("    但上面这四行数字说明：**这件事你本来就得自己做** ——")
    print("    在别的数据库里你靠一条 DDL 声明，在 DuckDB 里你靠一张真表。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的视图/宏/持久化对象差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    tmpdir = Path(tempfile.mkdtemp(prefix="ddl_demo_db_"))
    db_path = tmpdir / "demo.duckdb"
    try:
        scenario_view(db_path)
        scenario_view_is_recipe(db_path)
        scenario_macro(db_path)
        scenario_sequence(db_path)
        scenario_no_materialized_view(db_path)
        scenario_trigger(db_path)
        scenario_persistence_boundary(db_path)
        scenario_performance(args.rows, args.repeat)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()


if __name__ == "__main__":
    main()
