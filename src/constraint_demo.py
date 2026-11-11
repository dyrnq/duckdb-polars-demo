"""约束与完整性：谁在替你把关坏数据。

* DuckDB：约束是**声明式**的 —— 写在建表语句里，引擎在**写入路径上**替你检查，
  报错精确到哪一列、哪一个键，约束还会**存进数据库文件**，
  换个连接、换个进程再写，坏数据照样被拦下。
  `CREATE SEQUENCE` + `nextval()` 给的是**全局唯一、跨连接安全**的自增 ID。
* Polars 2.1：**一个约束 API 都没有** —— `assert_unique()` / `assert_null_count()`
  这类方法在 2.1 里**根本不存在**（`AttributeError`）。
  想检查只能自己拿 `is_duplicated()` / `null_count()` 拼，
  而且**得你自己记得在每个写入点上调用** —— 它不在写入路径上。
  也没有序列，`with_row_index()` 给的是当批次的行号，不是全局自增。

这个场景要回答：「数据写不进去」和「数据写进去了才发现」差多少。

用法：
    uv run python src/constraint_demo.py
    uv run python src/constraint_demo.py --rows 5000000 --repeat 5
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


def duck_try(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    """跑一条 SQL，把成功结果或报错原文返回来（报错原文是本 demo 的主角）。"""
    try:
        rows = con.sql(sql).fetchall()
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    return f"✅ {rows}" if rows else "✅ 执行成功"


# ---------------------------------------------------------------- 1. 四种约束


def scenario_constraints(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · DuckDB 会当场拒绝：五种约束的真实报错原文")

    cases = [
        (
            "PRIMARY KEY",
            [
                "CREATE TABLE c_pk (id INTEGER PRIMARY KEY, v INTEGER)",
                "INSERT INTO c_pk VALUES (1, 10)",
                "INSERT INTO c_pk VALUES (1, 20)",
            ],
            "重复的主键",
        ),
        (
            "UNIQUE",
            [
                "CREATE TABLE c_uq (id INTEGER, v INTEGER UNIQUE)",
                "INSERT INTO c_uq VALUES (1, 10)",
                "INSERT INTO c_uq VALUES (2, 10)",
            ],
            "重复的唯一键",
        ),
        (
            "NOT NULL",
            [
                "CREATE TABLE c_nn (id INTEGER, v INTEGER NOT NULL)",
                "INSERT INTO c_nn VALUES (1, NULL)",
            ],
            "该非空的列给了 NULL",
        ),
        (
            "CHECK",
            [
                "CREATE TABLE c_ck (id INTEGER, v INTEGER CHECK (v > 0))",
                "INSERT INTO c_ck VALUES (1, 10)",
                "INSERT INTO c_ck VALUES (2, -5)",
            ],
            "不满足 CHECK 表达式",
        ),
        (
            "FOREIGN KEY",
            [
                "CREATE TABLE c_parent (id INTEGER PRIMARY KEY)",
                "INSERT INTO c_parent VALUES (1)",
                "CREATE TABLE c_child (id INTEGER, pid INTEGER REFERENCES c_parent(id))",
                "INSERT INTO c_child VALUES (1, 999)",
            ],
            "引用了不存在的父键",
        ),
    ]

    for name, sqls, why in cases:
        print(f"\n  【{name}】{why}")
        for sql in sqls:
            print(f"    {sql}")
            print(f"      -> {duck_try(con, sql)}")

    print("\n  DuckDB 把约束登记在系统表里，查得到：")
    rows = con.sql(
        "SELECT table_name, constraint_type, constraint_text "
        "FROM duckdb_constraints() ORDER BY table_name, constraint_type"
    ).fetchall()
    print("  " + common.pad("表名", 14) + common.pad("约束类型", 14) + common.pad("约束定义", 24))
    print("  " + "-" * 52)
    for table_name, ctype, ctext in rows:
        print("  " + common.pad(table_name, 14) + common.pad(ctype, 14) + common.pad(ctext, 24))

    print("\n  UPDATE 也一样被拦（不是只在 INSERT 时看）：")
    con.execute("CREATE TABLE c_up (id INTEGER PRIMARY KEY, v INTEGER)")
    con.execute("INSERT INTO c_up VALUES (1, 10), (2, 20)")
    up = duck_try(con, "UPDATE c_up SET id = 1 WHERE id = 2")
    print(f"    UPDATE 把 id=2 改成已存在的 id=1 -> {up}")

    print("\n→ 五种约束，五种报错，而且**报错里带着具体的键值**：")
    print('  · 主键冲突告诉你 `Duplicate key "id: 1"` —— 哪个键、值是多少，一目了然。')
    print("  · CHECK 失败会把**表达式本身**打出来（`CHECK((v > 0))`），不用回去翻 DDL。")
    print('  · NOT NULL 精确到列名（`c_nn.v`），外键精确到缺失的键（`key "id: 999"`）。')
    print("  · 关键在于：**这些错误发生在写入的那一刻**。")
    print("    坏数据根本没有落盘的可能，也就没有「下游发现数据不对」的那一天。")
    print("  · 对照：Polars 侧这一整段 API 都不存在（见场景 2）。")


# ---------------------------------------------------------------- 2. Polars 侧


def scenario_polars(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · Polars 2.1 里一个约束 API 都没有")

    df = pl.DataFrame({"id": [1, 2, 2, 3, 3, 3], "v": [10, 20, 21, 30, 31, 32]})

    print("  先看那些「听起来应该存在」的 API —— 它们在 Polars 2.1 里**全都不存在**：\n")
    for name in ["assert_unique", "assert_no_nulls", "assert_null_count", "validate"]:
        try:
            getattr(df, name)()
            print(f"    ✅ df.{name}() 存在")
        except Exception as exc:
            print(f"    ❌ df.{name}() -> {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\n  整个 DataFrame / Series / Expr 上带 assert 的方法：")
    for holder, label in [
        (pl.DataFrame, "DataFrame"),
        (pl.Series([1]), "Series"),
        (pl.Expr, "Expr"),
    ]:
        names = [a for a in dir(holder) if "assert" in a.lower()]
        print(f"    {label:<12} -> {names if names else '（一个都没有）'}")

    print("\n  那就自己拼。Polars 给的是**检查用的原语**，不是**断言**：")
    print("    df['id'].is_duplicated()  -> 哪些行是重复的")
    print("    df['v'].null_count()     -> 有几个 NULL")
    print("    df.height == df['id'].n_unique() -> 行数 == 唯一值数")

    dup = df["id"].is_duplicated()
    nulls = df["v"].null_count()
    print(f"\n    这张表：id 有 {int(dup.sum())} 行是重复的，v 有 {nulls} 个 NULL")

    # 手写一个「约束检查」的函数 —— 注意它返回什么、什么时候被调用，全由你决定
    def check_frame(frame: pl.DataFrame) -> None:
        """Polars 侧手写的唯一性 + 非空检查。DuckDB 那边是引擎自动做的。"""
        dups = frame["id"].is_duplicated()
        if dups.any():
            bad = frame.filter(dups)["id"].to_list()
            raise ValueError(f"主键冲突：id = {bad}")
        if frame["v"].null_count() > 0:
            raise ValueError(f"NOT NULL 失败：v 有 {frame['v'].null_count()} 个 NULL")

    print("\n  自己拼出来的检查，失败时报的是**你自己写的异常**：")
    try:
        check_frame(df)
    except Exception as exc:
        print(f"    {type(exc).__name__}: {exc}")

    # ---- 对账：两边各自找出「哪些 id 是重复的」，结果必须一致 ----
    con.execute(
        "CREATE OR REPLACE TABLE bad AS SELECT * FROM "
        "(VALUES (1,10),(2,20),(2,21),(3,30),(3,31),(3,32)) t(id,v)"
    )
    duck_bad = con.sql(
        "SELECT id, count(*) AS c FROM bad GROUP BY id HAVING count(*) > 1 ORDER BY id"
    ).pl()
    pol_bad = df.group_by("id").agg(pl.len().alias("c")).filter(pl.col("c") > 1).sort("id")
    ok, why = common.compare_frames(duck_bad, pol_bad, ["id"])
    print("\n  对账：两边各自找出「哪些 id 重复了」，结果必须一样")
    print(f"    DuckDB : {duck_bad.to_dicts()}")
    print(f"    Polars : {pol_bad.to_dicts()}")
    print(f"    {'✅' if ok else '❌'} common.compare_frames: {why}")
    print("    → 查坏行的**能力**两边都有（算法一样，结果一样）；")
    print("      差的是**谁替你查、什么时候查**。")

    print("\n  一个容易踩的坑：`is_duplicated()` 在 DataFrame 和 Series 上含义不同：")
    dup_df = df.is_duplicated()
    dup_ser = df["id"].is_duplicated()
    print(
        f"    df.is_duplicated()      -> 类型 {type(dup_df).__name__}，"
        f"值 {dup_df.to_list()}（判的是**整行**重复）"
    )
    print(f"    df['id'].is_duplicated() -> 值 {dup_ser.to_list()}（判的是**这一列**重复）")
    print("    → 表里 id 明明重复了，`df.is_duplicated()` 却全 False ——")
    print("      因为它是按**整行**去重的，不是按列。")

    print("\n→ 这一条是本 demo 的核心：")
    print("  · Polars 2.1 **没有约束这个概念** —— 不是「有但不一样」，是**压根没有**。")
    print("    `assert_unique` 这种名字连 `AttributeError` 都给你了，一眼就知道别找了。")
    print("  · 有的是**原语**（`is_duplicated` / `null_count` / `n_unique`），")
    print("    把它们拼成「约束」是你自己的活儿，而且**没有任何强制机制**。")
    print("  · 拼出来之后，异常也是**你自己抛的**（上面那个 `ValueError`），")
    print("    报错内容、报错时机、要不要报错，全都归你 —— 也就意味着**可能压根不报错**。")
    print(
        "  · → 迁移时最危险的不是「API 名字对不上」，是**「少写一行代码，坏数据就静默进去了」**。"
    )


# ---------------------------------------------------------------- 3. 存储层 vs 内存


def scenario_persistence(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · DuckDB 的约束活在文件里，Polars 的检查活在一次调用里")

    tmpdir = Path(tempfile.mkdtemp(prefix="constraint_demo_"))
    dbfile = tmpdir / "persist.duckdb"

    print("  DuckDB 侧：约束写进 .duckdb 文件，**换个连接照样拦**。\n")
    c1 = duckdb.connect(str(dbfile))
    c1.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, v INTEGER NOT NULL)")
    c1.execute("INSERT INTO p VALUES (1, 10)")
    c1.close()

    c2 = duckdb.connect(str(dbfile))
    print(f"    重连后插重复主键 -> {duck_try(c2, 'INSERT INTO p VALUES (1, 20)')}")
    print(f"    重连后插 NULL    -> {duck_try(c2, 'INSERT INTO p VALUES (2, NULL)')}")
    c2.close()

    print("\n  约束本身也持久化（系统表里查得到）：")
    c3 = duckdb.connect(str(dbfile))
    rows = c3.sql(
        "SELECT table_name, constraint_type, constraint_text FROM duckdb_constraints() "
        "ORDER BY constraint_type"
    ).fetchall()
    print("  " + common.pad("表名", 12) + common.pad("约束类型", 14) + common.pad("约束定义", 22))
    print("  " + "-" * 48)
    for table_name, ctype, ctext in rows:
        print("  " + common.pad(table_name, 12) + common.pad(ctype, 14) + common.pad(ctext, 22))
    c3.close()

    print("\n  Polars 侧：检查只是**你调了一次的那个函数**。\n")
    df = pl.DataFrame({"id": [1, 1, 2], "v": [10, 20, 30]})

    def check_once(frame: pl.DataFrame) -> None:
        """一个「检查」：跑一次，报一次错。跑完就结束了。"""
        if frame["id"].is_duplicated().any():
            raise ValueError("id 重复")

    try:
        check_once(df)
    except Exception as exc:
        print(f"    第 1 条写入路径：检查 → {type(exc).__name__}: {exc}")

    print("\n    但下面这条写入路径**没有调用检查** —— 于是坏数据就这么进去了：")
    df2 = df.with_row_index("batch")
    df2 = df2.with_columns(pl.lit("batch-B").alias("来源"))
    print(
        f"    第 2 条写入路径：直接写入 -> {df2.height} 行，"
        f"其中重复的 id 有 {int(df2['id'].is_duplicated().sum())} 行"
    )
    print("    → 检查没跑 = 约束不存在。DuckDB 那边不存在「这条路径忘了加约束」这种可能。")

    print("\n  换个角度：Polars 的 DataFrame 本身就是「没有 schema 约束」的数据结构。")
    typed = pl.DataFrame({"id": pl.Series([1, 2], dtype=pl.Int64)})
    print(f"    df['id'].dtype = {typed['id'].dtype}")
    print("    但这个 dtype **只管表示**，不管取值 ——")
    print("    构造一个 dtype 完全一致、值却重复的 DataFrame：")
    dup_df = pl.DataFrame({"id": pl.Series([1, 1], dtype=pl.Int64)})
    print(f"      dtype={dup_df['id'].dtype}，值={dup_df['id'].to_list()} —— 没人拦。")

    print("\n→ 差别的本质是**约束存在哪一层**：")
    print("  · DuckDB 的约束在**存储层**：它是表定义的一部分，跟着文件走。")
    print("    换连接、换进程、换机器（把 .duckdb 文件拷过去），约束都还在。")
    print("    你没法「绕过」它 —— 除非你故意去改表定义。")
    print("  · Polars 的检查在**调用层**：它是一次函数调用，函数存在你的代码里。")
    print("    这次调用了、下次忘了，约束就凭空消失，而且**没有任何痕迹**。")
    print("  · → 实务上的差别：")
    print("    DuckDB 的约束是**一次投入、长期生效**（写进 DDL 就行）；")
    print("    Polars 的约束是**每个写入点都要重复投入**（而且加漏了没人知道）。")
    print("    表有 5 个写入路径，你就要写 5 遍检查 —— 忘了第 5 遍，第 5 个路径就开始产坏数据。")


# ---------------------------------------------------------------- 4. 序列


def scenario_sequence(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 序列：DuckDB 的 nextval vs Polars 的 with_row_index")

    print("  DuckDB 侧：\n")
    con.execute("CREATE SEQUENCE s START 1 INCREMENT 1")
    for sql in [
        "SELECT nextval('s')",
        "SELECT nextval('s'), nextval('s')",
        "SELECT currval('s')",
    ]:
        print(f"    {sql:<34} -> {con.sql(sql).fetchall()}")

    print("\n  序列还能直接当列的默认值：\n")
    con.execute("CREATE TABLE auto (id INTEGER DEFAULT nextval('s'), v INTEGER)")
    con.execute("INSERT INTO auto (v) VALUES (1), (2), (3)")
    rows = con.sql("SELECT id, v FROM auto ORDER BY id").fetchall()
    for rid, v in rows:
        print(f"    自动生成的 id={rid}  v={v}")
    print("    → 插入的时候**根本不用管 id 这一列**，引擎自己发的号。")

    print("\n  序列的边界参数也能改：\n")
    for sql in [
        "CREATE SEQUENCE s_step START 10 INCREMENT 5",
        "SELECT nextval('s_step')",
        "SELECT nextval('s_step')",
        "CREATE SEQUENCE s_cyc START 1 MINVALUE 1 MAXVALUE 3 CYCLE",
        "SELECT nextval('s_cyc')",
        "SELECT nextval('s_cyc')",
    ]:
        print(f"    {sql:<52} -> {con.sql(sql).fetchall()}")

    print("\n  序列是**跨连接**的（不像 currval 那样只在当前连接里有意义）：")
    tmpdir = Path(tempfile.mkdtemp(prefix="constraint_demo_"))
    dbfile = tmpdir / "seq.duckdb"
    c1 = duckdb.connect(str(dbfile))
    c1.execute("CREATE SEQUENCE persist_seq START 100")
    first = c1.sql("SELECT nextval('persist_seq')").fetchall()[0][0]
    c1.close()
    c2 = duckdb.connect(str(dbfile))
    second = c2.sql("SELECT nextval('persist_seq')").fetchall()[0][0]
    c2.close()
    print(f"    连接 1 取到 {first} → 关掉 → 重连后取到 {second}（接着往下发，不重置）")

    print("\n  Polars 侧：**没有序列**。最接近的两样东西：\n")
    df = pl.DataFrame({"v": [10, 20, 30]})
    print(f"    df.with_row_index('id')      -> {df.with_row_index('id')['id'].to_list()}")
    print(f"    pl.int_range(0, df.height)   -> {pl.int_range(0, df.height, eager=True).to_list()}")
    print("    → 两个都是**当批次的行号**：从 0 开始、每次调用都从头数。")
    print("      它们既不是全局唯一的，也不持久 —— 下一次调用又从 0 开始。")

    print("\n  想要「全局唯一、跨批次不重复」的 ID，Polars 侧得自己想办法：\n")
    batches = [pl.DataFrame({"v": [1, 2]}), pl.DataFrame({"v": [3]})]
    offset = 0
    ids = []
    for b in batches:
        ids.append(pl.Series(range(offset, offset + b.height)))
        offset += b.height
    print(f"    手工维护一个 offset 跨批次累加 -> {ids[0].to_list()} + {ids[1].to_list()}")
    print("    但这个 offset **得你自己存**（存文件、存数据库、存内存变量都行），")
    print("    多进程、崩溃重启之后还能不能对得上，全靠你自己保证。")

    print("\n→ 序列这件事的差别不在「能不能生成 ID」，在「谁来保证不重复」：")
    print("  · DuckDB 的 `nextval()` 是**引擎保证的全局唯一**：序列活在 catalog 里，")
    print("    跨连接、跨进程、跨文件都不重号，重启后接着往下发。")
    print("    而且它能当列的默认值 —— 插入的人**根本不需要知道 id 这件事存在**。")
    print("  · Polars 侧没有这个东西。`with_row_index()` 给的是**这一批的行号**，")
    print("    跨批次就重了；要全局唯一就得自己维护 offset，而那个 offset 的一致性")
    print("    **完全没有引擎替你兜底**。")
    print("  · → 迁移时最容易出错的一步：把 `DEFAULT nextval('s')` 换成 `with_row_index()`，")
    print("    看着「id 有了」，但**重跑一次批次 id 就全变了** ——")
    print("    外键、增量同步、幂等写入全靠 id，id 一变，下游全乱。")


# ---------------------------------------------------------------- 5. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 5 · 代价：约束的钱花在索引上，不在检查上")

    print(f"数据：{rows} 行，两列（id 递增 + v 取模打散）")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="constraint_demo_"))
    src = tmpdir / "constraint.parquet"
    con.execute(
        "COPY (SELECT i AS id, (i * 7919) % 1000003 AS v FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    polars_src = pl.read_parquet(src)

    # 四张表：列名、列类型、列顺序完全一样，只差约束子句
    con.execute("CREATE TABLE t_none (id INTEGER, v INTEGER)")
    con.execute("CREATE TABLE t_pk (id INTEGER PRIMARY KEY, v INTEGER)")
    con.execute("CREATE TABLE t_nn (id INTEGER, v INTEGER NOT NULL)")
    con.execute("CREATE TABLE t_ck (id INTEGER, v INTEGER CHECK (v >= 0))")

    print("\n  四张表的结构（`duckdb_tables()` 里读出来的建表语句）：")
    for name, ddl in con.sql(
        "SELECT table_name, sql FROM duckdb_tables() "
        "WHERE table_name IN ('t_none','t_pk','t_nn','t_ck') ORDER BY table_name"
    ).fetchall():
        print(f"    {name:<8} {ddl}")

    def duck_insert(table: str) -> tuple[float, str]:
        """清空表（不计时）→ 插一遍（计时）→ 返回 (耗时, 结果描述)。"""
        con.execute(f"DELETE FROM {table}")
        t0 = time.perf_counter()
        con.execute(f"INSERT INTO {table} SELECT id, v FROM read_parquet(?)", [str(src)])
        elapsed = time.perf_counter() - t0
        return elapsed, f"{rows:,} 行"

    def bench_query(table: str, sql: str) -> float:
        con.sql(sql.replace("TBL", table)).fetchall()
        times: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            con.sql(sql.replace("TBL", table)).fetchall()
            times.append(time.perf_counter() - t0)
        return statistics.median(times)

    out = tmpdir / "polars_out.parquet"

    def polars_write_only() -> tuple[float, str]:
        t0 = time.perf_counter()
        polars_src.write_parquet(out)
        elapsed = time.perf_counter() - t0
        return elapsed, f"{polars_src.height:,} 行"

    def polars_write_then_check() -> tuple[float, str]:
        """Polars 侧最接近「带约束写入」的东西：写完之后自己查一遍唯一性。"""
        t0 = time.perf_counter()
        polars_src.write_parquet(out)
        dups = int(polars_src["id"].is_duplicated().sum())
        elapsed = time.perf_counter() - t0
        return elapsed, f"重复 id {dups} 个"

    print("\n  写入侧（每次先 DELETE 再 INSERT，计时只覆盖 INSERT）：\n")
    print(
        "  "
        + common.pad("实现", 34)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 20)
    )
    print("  " + "-" * 80)
    write_results = [
        ("DuckDB INSERT 无约束", lambda: duck_insert("t_none")),
        ("DuckDB INSERT + NOT NULL", lambda: duck_insert("t_nn")),
        ("DuckDB INSERT + CHECK", lambda: duck_insert("t_ck")),
        ("DuckDB INSERT + PRIMARY KEY", lambda: duck_insert("t_pk")),
        ("Polars write_parquet（无校验）", polars_write_only),
        ("Polars write_parquet + 查唯一性", polars_write_then_check),
    ]
    for label, fn in write_results:
        fn()
        times: list[float] = []
        shown = ""
        for _ in range(repeat):
            elapsed, shown = fn()
            times.append(elapsed)
        med = statistics.median(times)
        print(
            "  "
            + common.pad(label, 34)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            + common.pad_left(shown, 20)
        )

    print("\n  查询侧（同一个查询，跑在有约束和没约束的表上）：\n")
    print(
        "  "
        + common.pad("查询", 26)
        + common.pad_left("无约束 (ms)", 16)
        + common.pad_left("PRIMARY KEY (ms)", 20)
    )
    print("  " + "-" * 62)
    for label, sql in [
        ("count(*)", "SELECT count(*) FROM TBL"),
        ("sum(v)", "SELECT sum(v) FROM TBL"),
        ("where id = ?", "SELECT v FROM TBL WHERE id = 12345"),
        ("group by id", "SELECT id, count(*) FROM TBL GROUP BY id"),
    ]:
        plain = bench_query("t_none", sql)
        keyed = bench_query("t_pk", sql)
        print(
            "  "
            + common.pad(label, 26)
            + common.pad_left(f"{plain * 1000:.2f}", 16)
            + common.pad_left(f"{keyed * 1000:.2f}", 20)
        )

    print("\n口径：输入已物化成 parquet，两边读的是**同一份文件**；")
    print("  DuckDB 的四张表列名、列类型、列顺序完全一致，**只差约束子句**")
    print("  （`CREATE TABLE` 语句就在上面，可以自己核）。")
    print("  写入的计时**包含读 parquet**：INSERT 的语句体里就是 `read_parquet(...)`，")
    print("  所以 DuckDB 那几行的数字 = 读盘 + 写入 + 约束校验（+ 索引维护）。")
    print("  Polars 那两行 = 写一个 parquet 文件（数据早就读进内存了，**不含读盘**）。")
    print("  每次 INSERT 前都先 DELETE（不计时），保证表是空表、约束检查不被已有数据干扰。")
    print("  查询侧的计时不含读盘（表已经在 DuckDB 里了）。")
    print("  ⚠️ **Polars 那两行跟 DuckDB 那几行不是同一件事**：")
    print("    DuckDB 的 INSERT 是「往**带约束的表**里写」，约束校验和索引维护都在计时里；")
    print("    Polars 的 write_parquet 是「写一个文件」，**没有任何校验**，")
    print("    第二行只是额外跑了一次**事后**的唯一性查询（不在写入路径上）。")
    print("    所以这两组的数字**不能直接比大小**。")
    print("\n解读：")
    print("  · **NOT NULL 和 CHECK 几乎是免费的** —— 它们只是逐行比一个条件，")
    print("    没有额外的数据结构要维护。")
    print("  · **PRIMARY KEY 贵一大截**，贵在**索引**上，不在「检查重复」上：")
    print("    每插一行都要更新一棵 ART 索引树。这笔钱你省不掉 ——")
    print("    有主键就意味着有索引，而索引正是插入时的主要开销。")
    print("  · 所以「加约束」这件事的真实成本模型是：**要不要索引**，而不是「要不要检查」。")
    print("    只想要「数据别写错」的话，NOT NULL + CHECK 几乎不要钱，")
    print("    想要「能按 id 快速查、能防重复」才需要付 PRIMARY KEY 的钱。")
    print("  · 查询侧基本不受影响（看上面那张表）—— 约束是**写入时**的代价，")
    print("    不是查询时的代价。")
    print("  · Polars 那两行更便宜，但便宜是有代价的：**校验不在写入路径上**。")
    print("    坏数据先落盘、再由一个「事后」的查询发现 ——")
    print("    而这个查询得**每次都记得写**。DuckDB 那边是写一次 DDL，永久生效。")


# ---------------------------------------------------------------- 6. 结论


def scenario_summary(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 收尾：约束是「数据正确性」的事，不是「查询性能」的事")

    print("  把两边的能力摆在一起：\n")
    print("  " + common.pad("能力", 26) + common.pad("DuckDB", 26) + common.pad("Polars 2.1", 26))
    print("  " + "-" * 78)
    rows = [
        ("主键 / 唯一", "✅ 建表时声明", "❌ 无 API，手工拼"),
        ("非空约束", "✅ 建表时声明", "❌ 无 API，手工拼"),
        ("CHECK 表达式", "✅ 建表时声明", "❌ 无 API，手工拼"),
        ("外键约束", "✅ 建表时声明", "❌ 无 API，手工拼"),
        ("写入时强制检查", "✅ 引擎自动", "❌ 得自己记得调"),
        ("约束随文件持久化", "✅ 换进程也在", "❌ 检查是一次调用"),
        ("全局唯一自增 ID", "✅ CREATE SEQUENCE", "❌ 无，只能手工 offset"),
        ("坏行定位", "✅ 报错带键值", "✅ 手工查（结果一致）"),
    ]
    for cap, duck_side, pol_side in rows:
        print("  " + common.pad(cap, 26) + common.pad(duck_side, 26) + common.pad(pol_side, 26))

    print("\n  最后再确认一次「约束不是查询性能的事」：")
    print("    场景 5 里带 PRIMARY KEY 的表和没约束的表，查询耗时基本一样。")
    print("    约束拦的是**写进去的坏数据**，不是让查询变慢 ——")
    print("    恰恰相反：**有主键的表还能顺带拿到索引**，某些查询反而更快。")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 的约束是声明式的**：写在 DDL 里，引擎在写入路径上替你执行，")
    print("    而且跟着数据库文件走 —— 一次投入，永久生效。")
    print("  · **Polars 2.1 没有约束这个概念**（不是「不一样」，是「没有」）：")
    print("    只有 `is_duplicated()` / `null_count()` 这些**原语**，")
    print("    把它们拼成约束是你自己的活儿，而且**不会有人提醒你漏了哪条写入路径**。")
    print("  · **Polars 的序列也不存在**：`with_row_index()` 给的是当批次的行号，")
    print("    不是全局唯一、跨批次不重复的自增 ID。")
    print("  · → 实务上的选择很明确：")
    print("    · 数据要落库、要跨进程共享、要长期可靠 → **用 DuckDB 管约束**，")
    print("      把 Polars 当计算层。")
    print("    · 数据只在内存里转一圈、错了重跑就行 → Polars 自己拼检查也够用，")
    print("      但要接受「检查只在你记得写的时候才存在」。")
    print("  · → 最后一句提醒：约束的价值不在「拦住一次坏数据」，")
    print("    在于**让坏数据连数据库都进不去**。这个差别，只有真正的存储层才给得起。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的约束与完整性差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_constraints(con)
        scenario_polars(con)
        scenario_persistence(con)
        scenario_sequence(con)
        scenario_performance(con, args.rows, args.repeat)
        scenario_summary(con)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
