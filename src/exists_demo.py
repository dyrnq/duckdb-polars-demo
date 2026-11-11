"""存在性查询与半连接：EXISTS / IN / NOT IN 与 semi / anti join 的语义差。

「这个 id 在另一张表里吗」—— 这大概是 SQL 里写得最多的一种查询。
两个引擎都能做，但**做出来的结果不总是一样**，而且不一样的时候谁都不报错。

* DuckDB：`EXISTS` / `IN` / `NOT IN` / `ANY` / `ALL` 都是标准 SQL 的三值逻辑；
  相关子查询会被优化器改写成**半连接**（`Join Type: SEMI` / `ANTI` / `MARK`）。
* Polars：`join(how="semi")` / `join(how="anti")` 就是半连接 / 反半连接；
  `Series.is_in()` 是 IN 的近似物 —— 它**不复现 SQL 的三值逻辑**。

这个场景要回答：右表里混进一个 NULL，`NOT IN` 为什么会悄悄返回空集，
以及哪些写法是安全的。

用法：
    uv run python src/exists_demo.py
    uv run python src/exists_demo.py --rows 5000000 --repeat 5
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

# ---------------------------------------------------------------- 数据

# 左表 4 行，最后一行的 id 是 NULL
LEFT = pl.DataFrame({"id": [1, 2, 3, None], "v": ["甲", "乙", "丙", "空"]})

# 右表 3 行，最后一行的 id 是 NULL —— 这一个 NULL 就是本 demo 的主角
RIGHT = pl.DataFrame({"id": [2, 4, None], "tag": ["x", "y", "z"]})

# 干净版：两边都没有 NULL
CLEAN_LEFT = pl.DataFrame({"id": [1, 2, 3], "v": ["甲", "乙", "丙"]})
CLEAN_RIGHT = pl.DataFrame({"id": [2, 4], "tag": ["x", "y"]})

# 右表键有重复 —— 用来看半连接会不会放大行数
DUP_RIGHT = pl.DataFrame({"id": [2, 2, 2, 4], "tag": ["x1", "x2", "x3", "y"]})


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # 落成真表 —— 这样 EXPLAIN 里看到的是 Seq Scan，而不是 Python Arrow Scan
    for name, frame in [
        ("l", LEFT),
        ("r", RIGHT),
        ("clean_l", CLEAN_LEFT),
        ("clean_r", CLEAN_RIGHT),
        ("dup_r", DUP_RIGHT),
    ]:
        con.register(f"frame_{name}", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame_{name}")
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def ids(con: duckdb.DuckDBPyConnection, sql: str) -> list[object]:
    """跑一条查询，把第一列的所有值取成列表（保留 None）。"""
    return [row[0] for row in con.sql(sql).fetchall()]


def join_types(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    """从 EXPLAIN 的盒式图里抽出改写后的连接类型。"""
    plan = con.sql(sql).explain()
    out = []
    for line in plan.splitlines():
        if "Join Type:" in line:
            out.append(line.split("Join Type:")[1].split("│")[0].strip())
    return out


# ---------------------------------------------------------------- 1. 共同语言


def scenario_vocabulary(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 共同语言：半连接、反半连接、成员判定")

    print("  左表 l：id = 1, 2, 3        右表 r：id = 2, 4")
    print("  （这一节的两张表都没有 NULL，先把两边的词汇对齐）")

    print("\n  ① 「在右表里找得到」 —— 半连接")
    print("     DuckDB : WHERE EXISTS (SELECT 1 FROM r WHERE r.id = l.id)")
    duck_hit = ids(
        con,
        "SELECT id FROM clean_l WHERE EXISTS (SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)",
    )
    pol_hit = CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="semi")["id"].to_list()
    print("     Polars : clean_l.join(clean_r, on='id', how='semi')")
    print(f"     结果   : DuckDB {duck_hit}    Polars {pol_hit}")

    print("\n  ② 「在右表里找不到」 —— 反半连接")
    print("     DuckDB : WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.id = l.id)")
    duck_miss = ids(
        con,
        "SELECT id FROM clean_l WHERE NOT EXISTS "
        "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)",
    )
    pol_miss = CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="anti")["id"].to_list()
    print("     Polars : clean_l.join(clean_r, on='id', how='anti')")
    print(f"     结果   : DuckDB {duck_miss}    Polars {pol_miss}")

    print("\n  ③ 「成员判定」 —— 返回布尔列，不筛行")
    print("     DuckDB : SELECT id, id IN (SELECT id FROM r) AS m FROM l")
    duck_flag = con.sql(
        "SELECT id, id IN (SELECT id FROM clean_r) AS m FROM clean_l ORDER BY id"
    ).fetchall()
    pol_flag = (
        CLEAN_LEFT.select("id")
        .with_columns(pl.col("id").is_in(CLEAN_RIGHT["id"].implode()).alias("m"))
        .sort("id")
        .rows()
    )
    print("     Polars : l.with_columns(pl.col('id').is_in(r['id']))")
    print(f"     结果   : DuckDB {duck_flag}")
    print(f"              Polars {pol_flag}")

    print("\n  ④ 右表的键有重复时（r.id = 2 出现三次）：")
    fanout = ids(con, "SELECT clean_l.id FROM clean_l JOIN dup_r ON clean_l.id = dup_r.id")
    semi = ids(con, "SELECT clean_l.id FROM clean_l SEMI JOIN dup_r ON clean_l.id = dup_r.id")
    pol_fanout = CLEAN_LEFT.join(DUP_RIGHT, on="id", how="inner").height
    pol_semi = CLEAN_LEFT.join(DUP_RIGHT, on="id", how="semi").height
    print(f"     普通 JOIN   DuckDB {len(fanout)} 行 / Polars {pol_fanout} 行   ← 键重复把行放大了")
    print(f"     半连接      DuckDB {len(semi)} 行 / Polars {pol_semi} 行   ← 每行左表最多出一次")

    print("\n→ 先把两边的词汇对齐（三件事）：")
    print("  · **半连接只保留左表的列**：DuckDB 的 `SEMI JOIN`、Polars 的 `how='semi'` 都是这样。")
    print("    右表的列不会出现在结果里 —— 想带过来得自己再 JOIN 一次。")
    print("  · **半连接不会放大行数**：右表键重复时，普通 JOIN 每匹配一次就多一行，")
    print("    半连接则每个左表行最多出现一次。做「存在性判断」时这是必须的语义。")
    print("  · **成员判定（IN / is_in）不筛行**，它给每一行打一个 TRUE / FALSE / NULL 标记。")
    print("    这一族的语义差异最大 —— 下一节就是它咬人的地方。")


# ---------------------------------------------------------------- 2. NOT IN 与 NULL


def scenario_not_in_null(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 右表混进一个 NULL，`NOT IN` 一行都不剩")

    print("  左表 l：id = 1, 2, 3, NULL")
    print("  右表 r：id = 2, 4, NULL      ← 只是右表多了这一个 NULL")

    rows = [
        (
            "WHERE id NOT IN (SELECT id FROM r)",
            "SELECT id FROM l WHERE id NOT IN (SELECT id FROM r)",
        ),
        (
            "WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.id = l.id)",
            "SELECT id FROM l WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.id = l.id)",
        ),
        (
            "WHERE id IN (SELECT id FROM r)",
            "SELECT id FROM l WHERE id IN (SELECT id FROM r)",
        ),
        (
            "WHERE id IS NOT NULL AND id NOT IN (SELECT id FROM r)",
            "SELECT id FROM l WHERE id IS NOT NULL AND id NOT IN (SELECT id FROM r)",
        ),
        (
            "WHERE id NOT IN (SELECT id FROM r WHERE id IS NOT NULL)",
            "SELECT id FROM l WHERE id NOT IN (SELECT id FROM r WHERE id IS NOT NULL)",
        ),
    ]
    for label, sql in rows:
        got = ids(con, sql)
        print(f"    {label}")
        print(f"      -> {got}")

    print("\n  Polars 侧同样的问题：")
    pol_anti = LEFT.join(RIGHT, on="id", how="anti")["id"].to_list()
    pol_not_in = LEFT.filter(~pl.col("id").is_in(RIGHT["id"].implode()))["id"].to_list()
    print(f"    join(how='anti')            -> {pol_anti}   ← 等价于 NOT EXISTS")
    print(f"    filter(~is_in(r['id']))     -> {pol_not_in}   ← 不等价于 NOT IN")

    print("\n→ 这是本 demo 里**最值得记住的一条**：")
    print("  · 右表里只要有**一个** NULL，`x NOT IN (2, 4, NULL)` 在三值逻辑下")
    print("    既不是 TRUE 也不是 FALSE，是 UNKNOWN；`WHERE` 只放行 TRUE → **一行都不剩**。")
    print("    上表第一行实测就是空集 —— 注意它连左表那行 id=NULL 也没放过。")
    print("  · `IN` 反而没事：`x IN (2, 4, NULL)` 对没匹配上的 x 是 UNKNOWN，")
    print("    被 WHERE 滤掉；有匹配的还是 TRUE 照样留下 —— 命中的行一个不少。")
    print("    所以问题**只出在 `NOT IN`**（以及 `NOT = ANY`）上。")
    print("  · 「加个 `id IS NOT NULL` 补救」也救不回来：")
    print("    上表第四行实测仍是空集 —— 因为 NULL 在**右表**里，滤的是左表没用。")
    print("  · 两种真能用的写法：")
    print("    ① `NOT EXISTS`（上表第二行）—— 三值逻辑里 NOT UNKNOWN 还是 UNKNOWN，")
    print("      但 EXISTS 只看「有没有行」，答案是布尔值，不会有 UNKNOWN；")
    print("    ② 把右表的 NULL 显式滤掉（`WHERE id IS NOT NULL`，上表第五行）。")
    print("  · Polars 的 `join(how='anti')` 对齐的是 **NOT EXISTS**，不是 NOT IN；")
    print("    `is_in()` 更进一步，它连 UNKNOWN 都不产生（下一节细说）。")


# ---------------------------------------------------------------- 3. 三值逻辑


def scenario_three_valued(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 三值逻辑的边界：IN / ANY / ALL 在 SELECT 列表里长什么样")

    print("  左表 l：id = 1, 2, 3, NULL      右表 r：id = 2, 4, NULL\n")
    sql = """
    SELECT id,
           id IN (SELECT id FROM r)          AS in_v,
           id NOT IN (SELECT id FROM r)      AS not_in_v,
           id = ANY (SELECT id FROM r)       AS any_v,
           id = ALL (SELECT id FROM r)       AS all_v
    FROM l ORDER BY id NULLS LAST
    """
    rows = con.sql(sql).fetchall()
    head = (
        common.pad("id", 8)
        + common.pad("IN", 11)
        + common.pad("NOT IN", 11)
        + common.pad("ANY", 11)
        + common.pad("ALL", 11)
    )
    print("  " + head)
    print("  " + "-" * 52)
    for r in rows:
        print(
            "  "
            + common.pad(str(r[0]), 8)
            + common.pad(str(r[1]), 11)
            + common.pad(str(r[2]), 11)
            + common.pad(str(r[3]), 11)
            + common.pad(str(r[4]), 11)
        )

    print("\n  空集上的 ANY / ALL：")
    any_empty = q1(con, "SELECT 1 = ANY(SELECT id FROM r WHERE 1=0)")
    all_empty = q1(con, "SELECT 1 = ALL(SELECT id FROM r WHERE 1=0)")
    print(f"    1 = ANY(SELECT id FROM r WHERE 1=0) -> {any_empty}")
    print(f"    1 = ALL(SELECT id FROM r WHERE 1=0) -> {all_empty}")
    print(
        f"    id NOT IN (SELECT id FROM r WHERE 1=0) -> 行数 "
        f"{q1(con, 'SELECT count(*) FROM l WHERE id NOT IN (SELECT id FROM r WHERE 1=0)')}"
    )

    agg = con.sql(
        "SELECT count(*), count(id IN (SELECT id FROM r)), "
        "sum(CASE WHEN id IN (SELECT id FROM r) THEN 1 ELSE 0 END) FROM l"
    ).fetchall()[0]
    print(
        f"\n  同一列、不同的聚合方式：count(*)={agg[0]}  "
        f"count(m)={agg[1]}  sum(CASE WHEN m THEN 1 ELSE 0 END)={agg[2]}"
    )

    print("\n→ 三值逻辑不是「DuckDB 的怪癖」，是 SQL 标准，两个引擎都要面对：")
    print("  · 判定结果是 **TRUE / FALSE / NULL（UNKNOWN）** 三种，不是两种。")
    print("    右表带 NULL 时，没匹配上的行拿到的不是 FALSE 而是 NULL。")
    print("  · `WHERE` 只放行 TRUE —— 所以 NULL 判定和 FALSE 判定在 `WHERE` 里没区别，")
    print("    **但在 `SELECT` 列表里区别很大**：上面 `IN` 那一列里 id=1 和 id=3 是 None，")
    print("    换成 `CASE WHEN ... THEN 1 ELSE 0 END` 就会把 UNKNOWN 当成 0 算进去；")
    print("    而 `count(m)` 又会把 NULL 直接跳过 —— 上面那行三个数字里，")
    print("    `count(*)` 统计的是全部行，另两个统计的是「判定为真的行」，")
    print("    同一列、换个聚合方式，数字就变了。")
    print("  · `ANY` 是「存在一个相等」，`ALL` 是「全部相等」：")
    print("    右表为空时 `ANY` 是 FALSE、`ALL` 是 TRUE（空集上 ALL 恒真）。")
    print("    `NOT IN` 则相反 —— 空子查询时它把所有行都放出来。")
    print("  · `id = ALL (...)` 在右表带 NULL 时几乎全是 NULL，")
    print("    因为「NULL = NULL」在 SQL 里不是 TRUE —— 这条比 `IN` 更难用。")


# ---------------------------------------------------------------- 4. 执行计划


def scenario_plan(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 执行计划：相关子查询被改写成了半连接")

    print("  同一个 `EXISTS` 的盒式图（EXPLAIN）：")
    print()
    for line in (
        con.sql(
            "EXPLAIN SELECT id FROM clean_l WHERE EXISTS "
            "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)"
        )
        .fetchall()[0][1]
        .splitlines()
    ):
        print(f"  {line}")
    print()

    print("  五种写法，改写出来的连接类型不一样：")
    queries = [
        (
            "WHERE EXISTS (...)",
            "SELECT id FROM clean_l WHERE EXISTS "
            "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)",
        ),
        (
            "WHERE NOT EXISTS (...)",
            "SELECT id FROM clean_l WHERE NOT EXISTS "
            "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)",
        ),
        ("WHERE id IN (子查询)", "SELECT id FROM clean_l WHERE id IN (SELECT id FROM clean_r)"),
        (
            "WHERE id NOT IN (子查询)",
            "SELECT id FROM clean_l WHERE id NOT IN (SELECT id FROM clean_r)",
        ),
        (
            "SELECT 列表里的 id IN (子查询)",
            "SELECT id, id IN (SELECT id FROM clean_r) AS m FROM clean_l",
        ),
    ]
    for label, sql in queries:
        print(f"    {common.pad(label, 30)} -> Join Type: {', '.join(join_types(con, sql))}")

    con.execute("SET explain_output='all'")
    plans = con.sql(
        "EXPLAIN SELECT id FROM clean_l WHERE EXISTS "
        "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)"
    ).fetchall()
    con.execute("SET explain_output='physical_only'")
    print("\n  `explain_output='all'` 时 EXPLAIN 返回三行（优化前 / 优化后 / 物理）：\n")
    for key, plan in plans:
        node = (
            "Dependent Join"
            if "Dependent Join" in plan
            else (
                "Comparison Join"
                if "Comparison Join" in plan
                else ("Hash Join" if "Hash Join" in plan else "（没有连接节点）")
            )
        )
        jt = [
            ln.split("Join Type:")[1].split("│")[0].strip()
            for ln in plan.splitlines()
            if "Join Type:" in ln
        ]
        print(
            f"    {common.pad(key, 14)} 连接节点 = {common.pad(node, 16)}"
            f"  Join Type = {', '.join(jt) if jt else '（无）'}"
        )

    print("\n→ DuckDB 2.0 里，相关子查询**不是**逐行执行的相关循环：")
    print("  · 优化前的计划里，EXISTS 是一个 `Dependent Join`（外层每行跑一次内层）；")
    print("    优化后变成 `Comparison Join` + `Join Type: SEMI`，到物理计划才落成 `Hash Join`；")
    print("    `Join Type` 决定它是哪一种半连接：")
    print("    · `SEMI`  —— 出现在 `WHERE EXISTS` 和 `WHERE id IN (子查询)`；")
    print("    · `ANTI`  —— 出现在 `WHERE NOT EXISTS`；")
    print("    · `MARK`  —— 出现在 `WHERE id NOT IN (子查询)`（多一个 `NOT SUBQUERY` 过滤器），")
    print("      以及出现在 **`SELECT` 列表里**的 `IN`：那里必须保留三值逻辑，")
    print("      光靠「有没有匹配上」这个布尔标记表达不出来。")
    print("  · 同一个 `IN`，放在 `WHERE` 里是 SEMI、放在 `SELECT` 列表里是 MARK ——")
    print("    差别不是写法上的洁癖，是「要不要保留 UNKNOWN」决定的。")
    print("  · 注意节点名是 `Join Type: SEMI` 这种**连接类型**，")
    print("    不是一个叫 `MARK_JOIN` 的独立算子（那是别的版本/别的引擎的说法）。")


# ---------------------------------------------------------------- 5. Polars 与对账


def scenario_polars(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · Polars 的 is_in 不复现 SQL 的三值逻辑")

    print("  左表 l：id = 1, 2, 3, NULL      右表 r：id = 2, 4, NULL\n")
    duck_rows = con.sql(
        "SELECT id, id IN (SELECT id FROM r) AS m FROM l ORDER BY id NULLS LAST"
    ).fetchall()
    pol = LEFT.with_columns(pl.col("id").is_in(RIGHT["id"].implode()).alias("m")).sort(
        "id", nulls_last=True
    )
    pol_rows = pol.rows()
    print("  " + common.pad("id", 8) + common.pad("DuckDB IN", 16) + common.pad("Polars is_in", 16))
    print("  " + "-" * 40)
    for d, p in zip(duck_rows, pol_rows, strict=True):
        flag = "  ← 不一样" if (d[1] is None) != (p[2] is None) else ""
        print(
            "  "
            + common.pad(str(d[0]), 8)
            + common.pad(str(d[1]), 16)
            + common.pad(str(p[2]), 16)
            + flag
        )

    print("\n  列保留的差别：")
    semi_cols = CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="semi").columns
    anti_cols = CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="anti").columns
    duck_semi_cols = list(
        con.sql("SELECT * FROM clean_l SEMI JOIN clean_r ON clean_l.id = clean_r.id").columns
    )
    print(f"    DuckDB SEMI JOIN 的列  : {duck_semi_cols}")
    print(f"    Polars how='semi' 的列 : {semi_cols}")
    print(f"    Polars how='anti' 的列 : {anti_cols}")
    print("    → 两边都只留左表的列，右表的列拿不到（要自己再 JOIN 一次）。")

    print("\n  对账（左边 DuckDB，右边 Polars）：")
    ok, why = common.compare_frames(
        con.sql(
            "SELECT id FROM clean_l WHERE EXISTS "
            "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)"
        ).pl(),
        CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="semi").select("id"),
        ["id"],
    )
    print(f"    EXISTS  ↔ join(how='semi')    {'✅' if ok else '❌'} {why}")
    ok, why = common.compare_frames(
        con.sql(
            "SELECT id FROM clean_l WHERE NOT EXISTS "
            "(SELECT 1 FROM clean_r WHERE clean_r.id = clean_l.id)"
        ).pl(),
        CLEAN_LEFT.join(CLEAN_RIGHT, on="id", how="anti").select("id"),
        ["id"],
    )
    print(f"    NOT EXISTS ↔ join(how='anti')  {'✅' if ok else '❌'} {why}")
    ok, why = common.compare_frames(
        con.sql("SELECT id FROM l WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.id = l.id)").pl(),
        LEFT.join(RIGHT, on="id", how="anti").select("id"),
        ["id"],
    )
    print(f"    带 NULL 的 NOT EXISTS ↔ anti   {'✅' if ok else '❌'} {why}")

    duck_not_in = con.sql("SELECT id FROM l WHERE id NOT IN (SELECT id FROM r)").pl()
    pol_not_in = LEFT.filter(~pl.col("id").is_in(RIGHT["id"].implode())).select("id")
    ok, why = common.compare_frames(duck_not_in, pol_not_in, ["id"])
    print(f"    带 NULL 的 NOT IN ↔ ~is_in      {'✅' if ok else '❌'} {why}")

    print("\n→ 对账结果说明了两件事：")
    print("  · **半连接这一族两边完全对齐**：EXISTS ↔ semi、NOT EXISTS ↔ anti，")
    print("    带不带 NULL 都一致（✅）。迁移时用半连接对半连接，可以放心。")
    print("  · **`NOT IN` 这一族对不上**（❌）：DuckDB 空集，Polars 两行。")
    print("    因为 Polars 的 `is_in` 只回答「在不在」，把 UNKNOWN 直接压成了 FALSE；")
    print("    上表里 id=1 和 id=3 两行，SQL 说 UNKNOWN，Polars 说 FALSE。")
    print("  · → **迁移规则**：`NOT IN (子查询)` 一律改成 `NOT EXISTS`；")
    print("    `IN (子查询)` 在 `WHERE` 里两边等价，但要拿布尔列（放 SELECT 列表里）")
    print("    就得自己补 UNKNOWN —— Polars 侧没有现成的三值逻辑。")
    print("  · → Polars 的 `is_in` 在 2.x 里对「同 dtype 的 Series」会发 DeprecationWarning，")
    print("    要写 `r['id'].implode()` 才回到旧行为。")


# ---------------------------------------------------------------- 6. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 6 · 代价：五种写「在不在」的方式，各要多少时间")

    print(f"数据：左表 {rows} 行，右表约十分之一（每 10 个 id 命中 1 个）")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="exists_demo_"))
    left_src = tmpdir / "left.parquet"
    right_src = tmpdir / "right.parquet"
    con.execute(
        "COPY (SELECT i AS id, i % 97 AS grp FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(left_src)],
    )
    con.execute(
        "COPY (SELECT i * 10 AS id FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows // 10 + 10, str(right_src)],
    )
    con.execute("CREATE OR REPLACE TABLE big_l AS SELECT * FROM read_parquet(?)", [str(left_src)])
    con.execute("CREATE OR REPLACE TABLE big_r AS SELECT * FROM read_parquet(?)", [str(right_src)])
    polars_l = pl.read_parquet(left_src)
    polars_r = pl.read_parquet(right_src)

    def duck_exists() -> int:
        return int(
            q1(
                con,
                "SELECT count(*) FROM big_l WHERE EXISTS "
                "(SELECT 1 FROM big_r WHERE big_r.id = big_l.id)",
            )
        )

    def duck_in() -> int:
        return int(q1(con, "SELECT count(*) FROM big_l WHERE id IN (SELECT id FROM big_r)"))

    def duck_semi() -> int:
        return int(q1(con, "SELECT count(*) FROM big_l SEMI JOIN big_r ON big_l.id = big_r.id"))

    def polars_semi() -> int:
        return polars_l.join(polars_r, on="id", how="semi").height

    def polars_is_in() -> int:
        return polars_l.filter(pl.col("id").is_in(polars_r["id"].implode())).height

    results = [
        ("DuckDB  EXISTS 相关子查询", duck_exists),
        ("DuckDB  IN (子查询)", duck_in),
        ("DuckDB  SEMI JOIN", duck_semi),
        ("Polars  join(how='semi')", polars_semi),
        ("Polars  filter(is_in(...))", polars_is_in),
    ]

    print(
        "\n  "
        + common.pad("实现", 30)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("命中行数", 14)
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
            + common.pad(label, 30)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 14)
        )

    print("\n口径：两个 parquet 在计时前已物化 —— DuckDB 侧是内存表 big_l / big_r，")
    print("  Polars 侧是内存里的 DataFrame，所以计时**不含读盘**。")
    print("  五行做的**是同一件事**：数出左表里有多少行能在右表找到对应的 id，")
    print("  右表在两边都是同一份数据（约左表的十分之一），没有 NULL。")
    print("  DuckDB 侧的子查询每次都要重新扫右表建哈希表，Polars 侧的 is_in 每次")
    print("  也要把右表打包成 list —— 两边都付了「准备右表」这笔钱，账是平的。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · **DuckDB 那三行的差距很小** —— 因为优化器已经把 EXISTS 和 IN")
    print("    都改写成了同一个半连接（场景 4 的盒式图），EXISTS 并没有「多跑一遍子查询」。")
    print("    如果你的 EXPLAIN 里 EXISTS 真的落成了 `Dependent Join`，那才要担心。")
    print("  · 所以在 DuckDB 上，**写 EXISTS 还是写显式 SEMI JOIN，不影响性能**，")
    print("    只影响可读性 —— 显式写 SEMI JOIN 能让人一眼看出这是半连接，")
    print("    也让「这里是半连接、不会放大行数」这件事写进 SQL 里。")
    print("  · **Polars 那两行走的不是一条路**：`join(how='semi')` 是哈希半连接，")
    print("    要把右表建成哈希表、再按左表的行序吐出来；`is_in` 是「右表做成一个集合、")
    print("    逐行查表」—— 它不做连接，只做成员判定，所以不用管列、也不用管行序。")
    print("    哪个更划算，取决于你只是要判定，还是要带着列一起带出来。")
    print("  · 真正的结论不是「谁快」，而是**两边都要先把右表压成一份可查的结构**；")
    print("    右表越大，这份结构的构建成本越显眼 —— 而且它是**每次查询都要重建**的，")
    print("    DuckDB 侧重建的是哈希表，Polars 侧重建的是那个集合。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的存在性查询与半连接差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_vocabulary(con)
        scenario_not_in_null(con)
        scenario_three_valued(con)
        scenario_plan(con)
        scenario_polars(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
