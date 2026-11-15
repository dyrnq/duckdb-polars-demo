"""MERGE INTO：一条语句里的「改、插、删」，和 Polars 侧的手工复刻。

DuckDB 有 ``MERGE INTO``（一条语句干三件事：更新匹配的行、插入没匹配上的行、
删掉源里已经没有的目标行）；Polars 2.1 **没有** MERGE —— 它的
``DataFrame.update`` 只能干「更新匹配的行」这一件，剩下两件得自己拼。

这个 demo 分两层：

1. **能力对账** —— 同一个意图，两边各写一遍，结果逐行比对。
2. **语义对账** —— 两边**写法不同、结果相同**的那些地方，是「能对齐」的；
   但有一批地方**根本对不齐**：重复键、NULL 键、条件分支、原子性。
   这几处不是「写法不同」，是**语义不同**。

跟 dml_demo.py 的关系：那个 demo 讲的是「UPDATE / DELETE / INSERT / upsert」
这些单条语句；这个 demo 讲的是**一条语句同时做三件事**的时候，
两个引擎的行为差在哪 —— 尤其差在**错误的时候报不报错**。

用法：
    uv run python src/merge_demo.py
    uv run python src/merge_demo.py --rows 200000 --repeat 5
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 目标表 / 源表的列 —— 两个引擎用同一套列名，比对才有的比
TARGET_COLUMNS = ["id", "v", "note"]
SOURCE_COLUMNS = ["id", "v"]


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ---------------------------------------------------------------- 小工具


def make_tables(con: duckdb.DuckDBPyConnection, target: list[tuple], source: list[tuple]) -> None:
    con.execute("CREATE OR REPLACE TABLE tgt(id INT, v INT, note VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE src(id INT, v INT)")
    for row in target:
        con.execute("INSERT INTO tgt VALUES (?, ?, ?)", list(row))
    for row in source:
        con.execute("INSERT INTO src VALUES (?, ?)", list(row))


def duck_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()


def to_frame(rows: list[tuple], columns: list[str]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=columns, orient="row")


def show_rows(rows: list[tuple], title: str) -> None:
    common.print_frame(to_frame(rows, TARGET_COLUMNS), title, max_rows=12)


def show_frame(df: pl.DataFrame, title: str) -> None:
    # 按 (id, v) 排 —— 只按 id 排的话，两行同一个 id 的先后是随机的
    keys = [c for c in ("id", "v") if c in df.columns]
    common.print_frame(df.sort(keys, nulls_last=True), title, max_rows=12)


def show_source(rows: list[tuple], title: str) -> None:
    common.print_frame(
        to_frame(rows, SOURCE_COLUMNS).sort("id", nulls_last=True), title, max_rows=12
    )


def fill_missing_columns(df: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """anti join 只保留左边的列 —— 目标表里源没有的列得补成 NULL。"""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        df = df.with_columns([pl.lit(None).alias(c) for c in missing])
    return df.select(columns)


# ---------------------------------------------------------------- Polars 的手工复刻


def polars_merge(
    target: pl.DataFrame, source: pl.DataFrame, delete_missing: bool = False
) -> pl.DataFrame:
    """Polars 侧没有 MERGE —— 最接近的写法。

    ``update(other, on='id', how='full')`` 一步干两件事：
    更新键匹配上的行，**并把 other 里键对不上的行插进来**
    （插进来的行，目标表里没写的列一律是 NULL —— 跟 DuckDB 的
    ``INSERT (id, v) VALUES (...)`` 一个意思）。

    删「源里已经没有的目标行」得另外做一次 anti join。
    """
    result = target.update(source, on="id", how="full")
    if delete_missing:
        gone = target.join(source, on="id", how="anti").select("id")
        result = result.join(gone, on="id", how="anti")
    return result


# ---------------------------------------------------------------- 各节


def scenario_basic(con: duckdb.DuckDBPyConnection) -> None:
    section("第 1 节 · 一条 MERGE 干两件事：更新匹配 + 插入没匹配")

    target = [(1, 10, "a"), (2, 20, "b"), (3, 30, "c")]
    source = [(2, 200), (4, 400)]

    print("  目标表 tgt：")
    show_rows(target, "tgt（合并前）")
    print()
    print("  源表 src：")
    show_source(source, "src（合并前）")
    print()
    print("  DuckDB 的写法：")
    print()
    print("    MERGE INTO tgt USING src ON tgt.id = src.id")
    print("    WHEN MATCHED THEN UPDATE SET v = src.v")
    print("    WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)")
    print()

    make_tables(con, target, source)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    duck = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")
    show_rows(duck, "DuckDB 合并后")

    print()
    print("  Polars 的写法（手工复刻）：")
    print()
    print("    result = target.update(source, on='id', how='full')")
    print()
    tgt_df = to_frame(target, TARGET_COLUMNS)
    src_df = to_frame(source, SOURCE_COLUMNS)
    polars = polars_merge(tgt_df, src_df)
    show_frame(polars, "Polars 合并后")

    ok, detail = common.compare_frames(
        to_frame(duck, TARGET_COLUMNS), polars, keys=["id"]
    )
    print()
    print(f"  逐行对账：{'✅ 两侧完全一致' if ok else '❌ 不一致'} —— {detail}")
    print()
    print("  → 一条 SQL 对一条 `update` 调用。**`how='full'` 是关键**：")
    print("    `how='left'` 只更新匹配的行（源里多出来的键会被丢掉），")
    print("    `how='full'` 才把没匹配上的行插进来 —— 这才等价于 MERGE。")
    print()


def scenario_delete(con: duckdb.DuckDBPyConnection) -> None:
    section("第 2 节 · 第三件事：删掉源里已经没有的目标行")

    target = [(1, 10, "a"), (2, 20, "b"), (3, 30, "c")]
    source = [(2, 200)]

    print("  DuckDB 的写法：")
    print()
    print("    MERGE INTO tgt USING src ON tgt.id = src.id")
    print("    WHEN MATCHED THEN UPDATE SET v = src.v")
    print("    WHEN NOT MATCHED BY SOURCE THEN DELETE")
    print()
    print("  `WHEN NOT MATCHED BY SOURCE` 是 DuckDB 独有的分支 ——")
    print("  「目标行在源里找不到对应的」就叫 NOT MATCHED BY SOURCE。")
    print()

    make_tables(con, target, source)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED BY SOURCE THEN DELETE"
    )
    duck = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")
    show_rows(duck, "DuckDB 合并后")

    tgt_df = to_frame(target, TARGET_COLUMNS)
    src_df = to_frame(source, SOURCE_COLUMNS)
    polars = polars_merge(tgt_df, src_df, delete_missing=True)
    print()
    show_frame(polars, "Polars 合并后")

    ok, detail = common.compare_frames(
        to_frame(duck, TARGET_COLUMNS), polars, keys=["id"]
    )
    print()
    print(f"  逐行对账：{'✅ 两侧完全一致' if ok else '❌ 不一致'} —— {detail}")
    print()

    print("  删的到底是哪些行？再看一个「删 + 插」同时发生的：")
    print()
    target2 = [(1, 10, "a"), (2, 20, "b")]
    source2 = [(3, 300)]
    make_tables(con, target2, source2)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v) "
        "WHEN NOT MATCHED BY SOURCE THEN DELETE"
    )
    duck2 = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")
    show_rows(duck2, "DuckDB 合并后")
    polars2 = polars_merge(
        to_frame(target2, TARGET_COLUMNS), to_frame(source2, SOURCE_COLUMNS), delete_missing=True
    )
    print()
    show_frame(polars2, "Polars 合并后")
    ok2, detail2 = common.compare_frames(
        to_frame(duck2, TARGET_COLUMNS), polars2, keys=["id"]
    )
    print()
    print(f"  逐行对账：{'✅ 两侧完全一致' if ok2 else '❌ 不一致'} —— {detail2}")
    print()
    print("  → **删掉的是「合并之前就存在、而源里没有」的目标行**（id=1、id=2）。")
    print("    这一轮刚插进来的行（id=3）**不受影响** —— MERGE 操作的是")
    print("    合并开始那一刻的目标表快照，不是合并过程中的中间状态。")
    print("    Polars 侧的 anti join 天然也是这个语义（拿合并前的 tgt 去 anti）。")
    print()


def scenario_conditional(con: duckdb.DuckDBPyConnection) -> None:
    section("第 3 节 · 条件分支：WHEN MATCHED AND")

    target = [(1, 10, "a"), (2, 20, "b"), (3, 30, "c")]
    source = [(2, 200), (3, 300)]

    print("  DuckDB 的写法：")
    print()
    print("    MERGE INTO tgt USING src ON tgt.id = src.id")
    print("    WHEN MATCHED AND src.v > 250 THEN UPDATE SET v = src.v")
    print("    WHEN MATCHED THEN UPDATE SET note = 'kept'")
    print("    WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)")
    print()
    print("  规则是「**第一个满足的分支赢**」：")
    print("    · id=3（src.v=300 > 250）→ 走第一个分支 → v 改成 300；")
    print("    · id=2（src.v=200，不 > 250）→ 第一个分支不满足 → 落到第二个分支")
    print("      → note='kept'，v 保持 20。")
    print()

    make_tables(con, target, source)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED AND src.v > 250 THEN UPDATE SET v = src.v "
        "WHEN MATCHED THEN UPDATE SET note = 'kept' "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    duck = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")
    show_rows(duck, "DuckDB 合并后")

    print()
    print("  Polars 侧**没有**这个能力 —— 得把源拆成两拨，各走各的分支，再拼回去：")
    print()
    print("    hi  = source.filter(pl.col('v') > 250)     # 命中第一个分支")
    print("    lo  = source.filter(pl.col('v') <= 250)    # 落到第二个分支")
    print("    res  = target.update(hi, on='id', how='full')")
    print("    res  = res.update(lo.select('id').with_columns(")
    print("                 pl.lit('kept').alias('note')), on='id', how='left')")
    print("    new_lo = lo.join(target, on='id', how='anti')   # lo 里的新键还得插")
    print("    res  = pl.concat([res, new_lo.select(target.columns)], how='vertical_relaxed')")
    print()
    print("  注意 `lo` 那一步为什么只 `select('id')`：")
    print("  `update` 会把「另一个 frame 里同名同列」的值全盖过去 ——")
    print("  如果把 `v` 也带上，id=2 的 v 会被改成 200，")
    print("  而 DuckDB 的第二个分支只改 `note`，v 应该保持 20。")
    print()

    tgt_df = to_frame(target, TARGET_COLUMNS)
    src_df = to_frame(source, SOURCE_COLUMNS)
    hi = src_df.filter(pl.col("v") > 250)
    lo = src_df.filter(pl.col("v") <= 250)
    polars = tgt_df.update(hi, on="id", how="full")
    polars = polars.update(
        lo.select("id").with_columns(pl.lit("kept").alias("note")), on="id", how="left"
    )
    new_lo = fill_missing_columns(lo.join(tgt_df, on="id", how="anti"), TARGET_COLUMNS)
    polars = pl.concat([polars, new_lo], how="vertical_relaxed")
    show_frame(polars, "Polars 合并后")

    ok, detail = common.compare_frames(
        to_frame(duck, TARGET_COLUMNS), polars, keys=["id"]
    )
    print()
    print(f"  逐行对账：{'✅ 两侧完全一致' if ok else '❌ 不一致'} —— {detail}")
    print()
    print("  → 结果能对齐，但**代价是写法**：DuckDB 一行 `WHEN MATCHED AND`，")
    print("    Polars 侧要拆源、分支处理、再拼回去 —— 每一步都得自己保证语义。")
    print("    分支一多（「A 改这个、B 改那个、都不匹配就插」），")
    print("    这套拼法就没有可读性可言了。")
    print()


def scenario_returning(con: duckdb.DuckDBPyConnection) -> None:
    section("第 4 节 · RETURNING：改了什么，直接返回")

    target = [(1, 10, "a"), (2, 20, "b")]
    source = [(2, 200), (4, 400)]

    make_tables(con, target, source)
    returned = con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v) "
        "RETURNING id, v"
    ).fetchall()
    final = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")

    print("  DuckDB 的写法（末尾加 RETURNING）：")
    print()
    print("    MERGE INTO tgt USING src ON tgt.id = src.id")
    print("    WHEN MATCHED THEN UPDATE SET v = src.v")
    print("    WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)")
    print("    RETURNING id, v")
    print()
    print("  RETURNING 返回的是**受影响的那几行**（更新后的新值 + 插入的行）：")
    print()
    show_source(returned, "RETURNING 返回")
    show_rows(final, "合并后的 tgt（对账用）")
    print()
    print("  → 不用再查一遍表就知道改了什么 —— 这是 MERGE 相对")
    print("    「UPDATE 一次 + INSERT 一次 + 再 SELECT 一次」最直接的优势。")
    print()
    print("  Polars 侧：`update` 返回的是**整个结果表**，不是「被改动的行」。")
    print("  想知道到底改了哪些行，得自己拿合并前后做差集 ——")
    print("  又是一段 DuckDB 那边一句话就能写完的代码。")
    print()


def scenario_duplicate_keys(con: duckdb.DuckDBPyConnection) -> None:
    section("第 5 节 · 重复键：两个引擎，两种错法")

    print("  源表里有**两行同一个键**，目标表里也有这个键 —— 该怎么办？")
    print()

    # (a) 重复键命中「更新」分支
    target = [(1, 10, "a"), (2, 20, "b")]
    source = [(2, 200), (2, 300)]
    make_tables(con, target, source)
    print("  (a) 重复键命中**已有目标行**（走 UPDATE 分支）")
    print()
    print("      目标 id=2 只能被改一次 —— 源里那两行 id=2 都想改它。")
    print()
    try:
        con.execute(
            "MERGE INTO tgt USING src ON tgt.id = src.id "
            "WHEN MATCHED THEN UPDATE SET v = src.v "
            "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
        )
        duck = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id")
        show_rows(duck, "DuckDB 合并后")
    except Exception as exc:
        print(f"      DuckDB → {type(exc).__name__}：")
        print(f"        {exc}")
        print()
        print("      → **DuckDB 报错**：一个目标行不能被影响两次。")
        print("        源不唯一的时候，这句话是救命的 ——")
        print("        换句话说，DuckDB 认「源必须唯一」这条规则。")
    print()

    tgt_df = to_frame(target, TARGET_COLUMNS)
    src_df = to_frame(source, SOURCE_COLUMNS)
    polars = polars_merge(tgt_df, src_df)
    show_frame(polars, "Polars 合并后")
    print()
    print("      → **Polars 静默复制了目标行**：id=2 出现了两次（v=200 和 v=300）。")
    print("        不报错、不警告，行数从 2 变成 3 —— 数据就这么坏掉了。")
    print("        Polars 自己的 docstring 写了：")
    print("        「'left' will keep all rows from the left table; **rows may be")
    print("        duplicated** if multiple rows in the right frame match the left row's key.」")
    print()

    # (b) 重复键落到「插入」分支
    print("  (b) 重复键落到**插入**分支（目标表里没有这个键）")
    print()
    target2 = [(1, 10, "a")]
    source2 = [(7, 700), (7, 800)]
    make_tables(con, target2, source2)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    duck2 = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id, v")
    show_rows(duck2, "DuckDB 合并后")
    polars2 = polars_merge(to_frame(target2, TARGET_COLUMNS), to_frame(source2, SOURCE_COLUMNS))
    print()
    show_frame(polars2, "Polars 合并后")
    print()
    print("      → **两个引擎都插了两行 id=7，而且都不报错。**")
    print("        DuckDB 上一节那个「不能影响同一行两次」的检查，")
    print("        **只管更新分支，不管插入分支** —— 源里两行都没匹配上，")
    print("        目标表的两行都是「新增」，不冲突。")
    print()

    # (c) 靠主键兜底
    con.execute("CREATE OR REPLACE TABLE tgt_pk(id INT PRIMARY KEY, v INT, note VARCHAR)")
    con.execute("INSERT INTO tgt_pk VALUES (1, 10, 'a')")
    try:
        con.execute(
            "MERGE INTO tgt_pk USING src ON tgt_pk.id = src.id "
            "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
        )
    except Exception as exc:
        print("      目标表换成带 PRIMARY KEY 的 —— 这次被约束拦住了：")
        print(f"        {type(exc).__name__}: {exc}")
        print()
        print("      → 所以「源必须唯一」这条规则，**DuckDB 靠约束兜底**，")
        print("        不是靠 MERGE 自己查。目标表没有主键的话，两行照样插进去。")
    print()


def scenario_null_keys(con: duckdb.DuckDBPyConnection) -> None:
    section("第 6 节 · NULL 键：两边一致，但手工拼的那条路走不通")

    target = [(1, 10, "a"), (None, 99, "n")]
    source = [(None, 55), (2, 200)]

    make_tables(con, target, source)
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    duck = duck_rows(con, "SELECT id, v, note FROM tgt ORDER BY id, v")
    show_rows(duck, "DuckDB 合并后")

    tgt_df = to_frame(target, TARGET_COLUMNS)
    src_df = to_frame(source, SOURCE_COLUMNS)
    polars = polars_merge(tgt_df, src_df)
    print()
    show_frame(polars, "Polars 合并后")

    ok, detail = common.compare_frames(
        to_frame(duck, TARGET_COLUMNS), polars, keys=["id", "v"]
    )
    print()
    print(f"  逐行对账：{'✅ 两侧完全一致' if ok else '❌ 不一致'} —— {detail}")
    print()
    print("  → **NULL 键算「没匹配」** —— 两边一致，都把源里那行 id=NULL 插了进去，")
    print("    于是目标表里出现了**两行 id=NULL**（原来那行 + 新插那行）。")
    print("    这不是 bug，是 `=` 的三值逻辑：NULL = NULL 不是 TRUE。")
    print()

    print("  但手工拼的那条路有个坑 —— 源里那一列**全是 NULL** 时，")
    print("  Polars 会把它推断成 `Null` 类型，跟目标表的 `Int64` 对不上：")
    print()
    a = pl.DataFrame({"id": [None], "v": [1]})
    b = pl.DataFrame({"id": [1], "v": [2]})
    print(f"    左边的 dtype = {a.schema['id']}，右边 = {b.schema['id']}")
    try:
        a.join(b, on="id", how="anti")
    except Exception as exc:
        print(f"    {type(exc).__name__}: {exc}")
    print()
    print("  → `SchemaError`：一边是 `null` 一边是 `i64`，join 直接拒绝。")
    print("    DuckDB 那条路没有这个问题 —— SQL 里 NULL 就是个值，类型照旧是 INT。")
    print("    也就是说：**DuckDB 能吃的输入，Polars 的手工拼法可能吃不下** ——")
    print("    而且它报的是**类型错**，不是「你的键有 NULL」，")
    print("    读错误消息的人很难往「键不能是 NULL」上想。")
    print()


def scenario_insert_columns(con: duckdb.DuckDBPyConnection) -> None:
    section("第 7 节 · 插入的列：不给全就报错")

    make_tables(con, [(1, 10, "a")], [(2, 200)])
    print("  目标表 tgt 有 3 列（id, v, note）。插入的时候：")
    print()
    for sql in [
        "WHEN NOT MATCHED THEN INSERT VALUES (src.id, src.v)",
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)",
    ]:
        try:
            con.execute(f"MERGE INTO tgt USING src ON tgt.id = src.id {sql}")
            print(f"    ✅ {sql}")
            print("       → 插入的行：note 取默认值 NULL")
        except Exception as exc:
            print(f"    ❌ {sql}")
            print(f"       → {type(exc).__name__}: {exc}")
    print()
    print("  → 两种写法：")
    print("    · `INSERT VALUES (...)` —— **必须给全所有列**（不给就 Binder Error）；")
    print("    · `INSERT (id, v) VALUES (...)` —— 点名要哪几列，没点的取默认值。")
    print("  Polars 侧的 `update(how='full')` 插进来的行，")
    print("  目标表里源没有的列**一律是 NULL** —— 相当于第二种写法。")
    print()


def scenario_atomicity(con: duckdb.DuckDBPyConnection) -> None:
    section("第 8 节 · 原子性：一条语句 vs 三步操作")

    make_tables(con, [(1, 10, "a"), (2, 20, "b")], [(2, 200), (3, 300)])
    con.execute("BEGIN")
    con.execute(
        "MERGE INTO tgt USING src ON tgt.id = src.id "
        "WHEN MATCHED THEN UPDATE SET v = src.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
    )
    mid = duck_rows(con, "SELECT id, v FROM tgt ORDER BY id")
    con.execute("ROLLBACK")
    after = duck_rows(con, "SELECT id, v FROM tgt ORDER BY id")

    print("  DuckDB：MERGE 是**一条语句**，跑在事务里：")
    print()
    print(f"    合并后（未提交）：{mid}")
    print(f"    ROLLBACK 之后    ：{after}")
    print()
    print("  → 撤销得干干净净 —— 因为它就是一条语句，没有中间状态。")
    print()
    print("  Polars 侧：手工复刻是**三步**（update → anti join → concat），")
    print("  中间任何一步失败，前两步的结果都已经产出了；")
    print("  而且 Polars **没有事务**这个概念 ——")
    print("  真要原子性，得自己写临时文件再原子替换（那是另一回事）。")
    print()


def scenario_cost(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("第 9 节 · 代价：一条语句和一次调用，各要多久")

    workdir = Path(tempfile.mkdtemp(prefix="merge_demo_"))
    try:
        dbfile = workdir / "merge_demo.duckdb"
        con2 = duckdb.connect(str(dbfile))
        con2.execute(
            "CREATE TABLE tgt(id INT, v INT, note VARCHAR);"
            "CREATE TABLE src(id INT, v INT);"
        )
        con2.execute(
            "INSERT INTO tgt SELECT i, i * 10, 'n' FROM range(?) t(i)", [rows]
        )
        # 源：一半的键命中目标（0 .. half-1），另一半是新键（rows .. rows+half-1）
        half = rows // 2
        con2.execute(
            "INSERT INTO src SELECT i, i * 100 FROM range(?) t(i) "
            "UNION ALL SELECT i, i * 100 FROM range(?, ?) t(i)",
            [half, rows, rows + half],
        )

        target = pl.DataFrame(
            {
                "id": pl.Series(range(rows), dtype=pl.Int64),
                "v": pl.Series([i * 10 for i in range(rows)], dtype=pl.Int64),
                "note": pl.Series(["n"] * rows, dtype=pl.String),
            }
        )
        src_ids = list(range(half)) + list(range(rows, rows + half))
        source = pl.DataFrame(
            {
                "id": pl.Series(src_ids, dtype=pl.Int64),
                "v": pl.Series([i * 100 for i in src_ids], dtype=pl.Int64),
            }
        )

        def duck_merge() -> None:
            con2.execute(
                "MERGE INTO tgt USING src ON tgt.id = src.id "
                "WHEN MATCHED THEN UPDATE SET v = src.v "
                "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (src.id, src.v)"
            )

        def duck_two_stmt() -> None:
            con2.execute("UPDATE tgt SET v = src.v FROM src WHERE tgt.id = src.id")
            con2.execute(
                "INSERT INTO tgt SELECT src.id, src.v, NULL FROM src "
                "WHERE src.id NOT IN (SELECT id FROM tgt)"
            )

        def polars_full() -> pl.DataFrame:
            return target.update(source, on="id", how="full")

        def polars_manual() -> pl.DataFrame:
            updated = target.update(source, on="id", how="left")
            ins = fill_missing_columns(source.join(target, on="id", how="anti"), TARGET_COLUMNS)
            return pl.concat([updated, ins], how="vertical_relaxed")

        # 每项后面那个布尔值 = 测之前要不要把目标表复位
        cases = [
            ("DuckDB  MERGE INTO（一条语句）", duck_merge, True),
            ("DuckDB  UPDATE ... FROM + INSERT（两条语句）", duck_two_stmt, True),
            ("Polars  update(how='full')（一次调用）", polars_full, False),
            ("Polars  手工三步（update + anti join + concat）", polars_manual, False),
        ]

        def reset_target() -> None:
            """把目标表恢复成合并前的状态 —— **不计时**。

            不恢复的话，第二轮 MERGE 就没东西可插了（第一轮已经插进去），
            每一轮测的就不是同一件事。
            """
            con2.execute("DELETE FROM tgt WHERE id >= ?", [rows])
            con2.execute("UPDATE tgt SET v = id * 10")

        def timed(fn, needs_reset: bool) -> list[float]:
            out = []
            for _ in range(repeat):
                if needs_reset:
                    reset_target()
                t0 = time.perf_counter()
                fn()
                out.append((time.perf_counter() - t0) * 1000)
            return out

        print(f"  目标表 {rows:,} 行 / 源表 {rows:,} 行（一半命中、一半是新键），")
        print(f"  重复 {repeat} 次，**串行**跑；机器：{common.describe_machine()}")
        print()
        print("  每次测之前都把目标表**恢复成合并前的状态**（恢复动作不计时）——")
        print("  不然第二轮 MERGE 就没东西可插了，每轮测的就不是同一件事。")
        print()
        print("  " + common.pad("写法", 54) + common.pad("中位数 (ms)", 14) + "最快 (ms)")
        print("  " + "-" * 86)
        for name, fn, needs_reset in cases:
            samples = timed(fn, needs_reset)
            print(
                "  "
                + common.pad(name, 54)
                + common.pad(f"{statistics.median(samples):.1f}", 14)
                + f"{min(samples):.1f}"
            )
        print()

        # 代价只有在「算出来的是同一件事」时才有意义 —— 先对账
        reset_target()
        duck_merge()
        duck_result = to_frame(
            duck_rows(con2, "SELECT id, v, note FROM tgt ORDER BY id, v"), TARGET_COLUMNS
        )
        polars_full_result = polars_full().sort(["id", "v"], nulls_last=True)
        polars_manual_result = polars_manual().sort(["id", "v"], nulls_last=True)
        print("  先对账 —— 代价只有在「算的是同一件事」时才有意义：")
        print()
        for label, frame in [
            ("Polars  update(how='full')", polars_full_result),
            ("Polars  手工三步", polars_manual_result),
        ]:
            ok, detail = common.compare_frames(duck_result, frame, keys=["id", "v"])
            print(f"    {label} vs DuckDB MERGE：{'✅ 一致' if ok else '❌ 不一致'} —— {detail}")
        print()

        print("  ⚠️ **这两个数字不能直接比大小**：")
        print()
        print("    · DuckDB 的 MERGE 跑在一张**真实的表**上，包含写回（含 WAL）；")
        print("      Polars 这边只是**算出一个新的 DataFrame**，一个字节都没落盘。")
        print("    · 真正该比的是「同一件事」：")
        print("      DuckDB 那边 `UPDATE ... FROM` + `INSERT`（两条语句）")
        print("      和 Polars 这边的手工三步 —— 那才是同一个东西的两种写法。")
        print()
        con2.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB 的 MERGE INTO 与 Polars 侧手工复刻的逐行对账"
    )
    parser.add_argument("--rows", type=int, default=200_000, help="代价场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    con = duckdb.connect()
    try:
        scenario_basic(con)
        scenario_delete(con)
        scenario_conditional(con)
        scenario_returning(con)
        scenario_duplicate_keys(con)
        scenario_null_keys(con)
        scenario_insert_columns(con)
        scenario_atomicity(con)
        scenario_cost(con, args.rows, args.repeat)
    finally:
        con.close()

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  1. **能对齐的部分**：更新匹配、插入没匹配的、删除源里已经没有的 —— ")
    print("     DuckDB 一条 `MERGE INTO`，Polars 一次 `update(how='full')` 加一次")
    print("     anti join，结果逐行一致（前面三节都是 ✅）。")
    print()
    print("  2. **DuckDB 多出来的能力**：`WHEN NOT MATCHED BY SOURCE`（删）、")
    print("     `WHEN MATCHED AND`（条件分支）、`RETURNING`（返回受影响行）——")
    print("     Polars 侧都得自己拼，条件分支尤其要把源拆开再拼回去。")
    print()
    print("  3. **对不齐的部分 ①：重复键。**")
    print("     DuckDB 在**更新**分支上报错（一个目标行不能被影响两次）；")
    print("     Polars 静默把目标行复制成两行。")
    print("     但在**插入**分支上两个引擎都插两行、都不报错 ——")
    print("     DuckDB 的检查不管插入分支，靠主键约束兜底。")
    print()
    print("  4. **对不齐的部分 ②：NULL 键。** 结果两边一致（NULL 算没匹配，")
    print("     目标表里会出现两行 id=NULL），但 Polars 的手工拼法在")
    print("     「键列全是 NULL」时会报 `SchemaError` —— 而且报的是类型错，")
    print("     很难让人联想到「键不能是 NULL」。")
    print()
    print("  5. **原子性**：DuckDB 的 MERGE 是一条语句，事务里 ROLLBACK 撤得干净；")
    print("     Polars 的手工复刻是三步，中间失败就停在半路，而且没有事务。")
    print()
    print("  → 一句话：**MERGE 的价值不在「快」，在「一条语句把三件事绑在一起」** ——")
    print("    绑在一起才有原子性，才不用自己判断「哪些该更新、哪些该插、哪些该删」。")
    print()


if __name__ == "__main__":
    main()
