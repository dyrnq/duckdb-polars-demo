"""查询计划：EXPLAIN 的两种长相，以及投影裁剪到底省了什么。

* DuckDB：`EXPLAIN` 打印**盒式图**，默认给的是**物理计划** ——
  能看到 `Perfect Hash Group By`、`Read Parquet` 这些真会跑起来的算子；
  `PRAGMA explain_output='all'` 一次能拿到「未优化 / 已优化 / 物理」三份计划。
* Polars：`LazyFrame.explain()` 返回**缩进树**，是算子树而不是物理算子；
  `optimized=False` 能看到优化前的样子（那时候投影裁剪还没发生）。
* 两边都做**投影裁剪**和**过滤下推**，但**证据强度差很多**：
  DuckDB 的 profiling JSON 里有 `io.total_bytes_read`，Polars 侧一个计数器都没有。

这个场景要回答：两个引擎的计划能不能对读，以及「计划好看」到底意味着什么。

用法：
    uv run python src/plan_demo.py
    uv run python src/plan_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 宽表除了 id / g / v 之外的「装饰列」—— 用来演示投影裁剪。
# 一半是数值、一半是宽字符串：压缩率低，这样「少读几列」才看得出差别。
WIDE_NUMERIC = [
    ("a", "i * 1.234567"),
    ("d", "i / 7.0"),
    ("f", "i * 3.14159"),
    ("i", "i * 2.71828"),
    ("j", "(i * 31) % 1009"),
]
WIDE_TEXT = [
    ("b", "md5(i::VARCHAR)"),
    ("c", "'c' || (i % 1000)::VARCHAR"),
    ("e", "md5((i + 1)::VARCHAR)"),
    ("h", "'h' || (i % 997)::VARCHAR || md5(i::VARCHAR)"),
]
WIDE_EXTRA_COLUMNS = WIDE_NUMERIC + WIDE_TEXT
WIDE_NUMERIC_NAMES = [name for name, _ in WIDE_NUMERIC]
WIDE_TEXT_NAMES = [name for name, _ in WIDE_TEXT]


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def make_wide_parquet(con: duckdb.DuckDBPyConnection, path: Path, rows: int) -> None:
    """造一张 12 列的宽表 parquet —— 每列都是真数据，投影裁剪才看得出差别。"""
    extras = ", ".join(f"{expr} AS {name}" for name, expr in WIDE_EXTRA_COLUMNS)
    con.execute(
        f"COPY (SELECT i AS id, i % 97 AS g, i * 1.5 AS v, {extras} FROM range(?) t(i))"
        " TO ? (FORMAT PARQUET)",
        [rows, str(path)],
    )


def duck_profiling(con: duckdb.DuckDBPyConnection, sql: str, params: list, out: Path) -> dict:
    """跑一条查询，把 DuckDB 的 profiling JSON 落盘再读回来（JSON 是异步写的）。"""
    con.execute("SET enable_profiling='json'")
    con.execute(f"SET profiling_output='{out}'")
    con.sql(sql, params=params).fetchall()
    con.execute("SET enable_profiling='no_output'")
    return json.loads(out.read_text(encoding="utf-8"))


def find_operator(node: dict, kind: str) -> dict | None:
    """在 DuckDB 的算子树里找一个指定类型的节点（比如 TABLE_SCAN）。"""
    if node.get("type") == kind:
        return node
    for child in node.get("children", []):
        found = find_operator(child, kind)
        if found is not None:
            return found
    return None


BOX_CHARS = " ╭╮╰╯│├┤┬┴─"


def plan_nodes(plan: str) -> list[str]:
    """从 DuckDB 的盒式图里抽出每个节点的标题（按从上到下的顺序）。"""
    titles = []
    for line in plan.splitlines():
        m = re.match(r"╭─\s*(.+?)\s*─+╮", line)
        if m:
            titles.append(m.group(1).strip(BOX_CHARS))
    return titles


def scan_extra_info(prof: dict) -> str:
    """把扫描节点的 Projections / Filters 摘出来，合成一行。"""
    for node in prof.get("operator", []):
        scan = find_operator(node, "TABLE_SCAN")
        if scan is None:
            continue
        info = scan.get("extra_info", {})
        parts = [f"Projections: {info.get('Projections')}"]
        if info.get("Filters"):
            parts.append(f"Filters: {info['Filters']}")
        return " | ".join(parts)
    return "（没找到扫描节点）"


# ---------------------------------------------------------------- 1. 两边的 EXPLAIN


def scenario_explain(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 同一个查询，两边的 EXPLAIN 长得完全不一样")

    con.execute(
        "CREATE OR REPLACE TABLE t AS"
        " SELECT i AS id, i % 7 AS g, i * 1.5 AS v FROM range(1000) t(i)"
    )

    sql = "SELECT g, sum(v) FROM t WHERE id < 500 GROUP BY g"

    lf = (
        pl.LazyFrame(
            {
                "id": list(range(1000)),
                "g": [i % 7 for i in range(1000)],
                "v": [i * 1.5 for i in range(1000)],
            }
        )
        .filter(pl.col("id") < 500)
        .group_by("g")
        .agg(pl.col("v").sum().alias("sum(v)"))
    )

    print("  同一个查询，两边各给一份计划。")
    print()
    print("  DuckDB —— con.sql(sql).explain()：")
    print()
    for line in con.sql(sql).explain().splitlines():
        print(f"    {line}")

    print()
    print("  Polars —— LazyFrame.explain()：")
    print()
    for line in lf.explain(optimized=True).splitlines():
        print(f"    {line}")

    print()
    print("  顺手确认两边的**结果**是一样的（计划长得像不算数）：")
    duck_side = con.sql(sql + " ORDER BY g").pl()
    pol_side = lf.sort("g").collect()
    same, reason = common.compare_frames(duck_side, pol_side, ["g"])
    verdict = "✅ 一致" if same else "❌ 不一致"
    print(f"    DuckDB SQL  vs  Polars 表达式 -> {verdict}：{reason}")

    print("\n→ 计划**没法对读**，但结果能对账：")
    print("  · DuckDB 给的是**盒式图**，而且默认就是**物理计划** ——")
    print("    `Perfect Hash Group By`、`Seq Scan` 都是真会跑起来的算子。")
    print("  · Polars 给的是**缩进树**，画的是**算子树**（AGGREGATE / FILTER / SELECT），")
    print("    没有物理算子这一层。")
    print("  · 两边的计划**没有一一对应的节点**：DuckDB 的 `Projection` 在 Polars 里是 `π`；")
    print("    DuckDB 把过滤塞进扫描节点（`Filters: id < 500`），")
    print("    Polars 在内存表上是独立的一行 `FILTER`（扫 parquet 时会写成 `SELECTION:`）。")
    print("  · 所以看计划这件事，**别指望两边能对照着读** ——")
    print("    只能问同一个问题：「这个查询会做哪些事」，然后各答各的。")
    print("  · 真正能对读的是**结果**，所以每次改写都得配一次对账（就像上面那次）。")


# ---------------------------------------------------------------- 2. 投影裁剪


def scenario_projection(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 投影裁剪：宽表只查 2 列，两边都裁，但只有一边能证明")

    tmpdir = Path(tempfile.mkdtemp(prefix="plan_demo_"))
    wide = tmpdir / "wide.parquet"
    make_wide_parquet(con, wide, 100_000)
    total_cols = 3 + len(WIDE_EXTRA_COLUMNS)
    print(f"数据：一张 {total_cols} 列的宽表，100000 行")
    print(f"文件大小：{wide.stat().st_size:,} 字节")
    print("查询：只取其中 2 列（id 和 v）")

    print()
    print("  DuckDB 的计划（`Read Parquet` 节点上写着读了哪几列）：")
    narrow_sql = "SELECT id, v FROM read_parquet(?) WHERE id < 50000"
    for line in con.sql(narrow_sql, params=[str(wide)]).explain().splitlines():
        print(f"    {line}")

    print()
    print("  Polars 的计划（`PROJECT 2/N COLUMNS`）：")
    lf = pl.scan_parquet(wide).filter(pl.col("id") < 50000).select("id", "v")
    for line in lf.explain(optimized=True).splitlines():
        print(f"    {line}")

    print()
    print("  两边**都**做了裁剪 —— 计划里都写着只读 2 列。")
    print("  但「只读 2 列」这件事，**只有 DuckDB 能拿数字证明**：")

    prof_narrow = duck_profiling(con, narrow_sql, [str(wide)], tmpdir / "p_narrow.json")
    wide_sql = "SELECT * FROM read_parquet(?) WHERE id < 50000"
    prof_wide = duck_profiling(con, wide_sql, [str(wide)], tmpdir / "p_wide.json")

    bytes_narrow = prof_narrow["io"]["total_bytes_read"]
    bytes_wide = prof_wide["io"]["total_bytes_read"]
    print()
    print("    DuckDB profiling JSON（查 2 列）：")
    print(f"      读了哪些列：{scan_extra_info(prof_narrow)}")
    print(f"      实际读盘：{bytes_narrow:,} 字节")
    print(f"    DuckDB profiling JSON（查全部 {total_cols} 列）：")
    print(f"      读了哪些列：{scan_extra_info(prof_wide)}")
    print(f"      实际读盘：{bytes_wide:,} 字节")
    print(f"      → 只查 2 列时读盘量是查全列的 {bytes_narrow / bytes_wide:.1%}")

    print()
    print("  Polars 侧没有对应的东西：")
    print("    LazyFrame.explain() 只给计划文本，没有 IO 计数器；")
    print("    `.collect()` 返回的 DataFrame 也不带任何元数据。")
    print("    想验证 Polars 真的只读了 2 列，只能自己造一个「只有那 2 列的文件」对比。")

    print("\n→ 这一段是两边**最像**的地方，也是**最容易被误读**的地方：")
    print("  · 两边都做投影裁剪：查 2 列就只读 2 列的 parquet column chunk，")
    print("    不读被丢掉的那几列 —— 这点两边没有争议。")
    print("  · 但 DuckDB 的 profiling JSON 能**告诉你数字**（读了几个字节、扫了多少行），")
    print("    Polars 只能告诉你「计划里写了 2 列」—— 这是一句**承诺**，不是一个**观测**。")
    print("  · 实务上的差别：调优的时候 DuckDB 能直接用「读盘字节数」当反馈信号，")
    print("    Polars 那边只能靠「改代码再测一遍」来间接验证。")


# ---------------------------------------------------------------- 3. 过滤下推


def scenario_filter_pushdown(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 过滤下推：过滤被塞进了哪一步")

    tmpdir = Path(tempfile.mkdtemp(prefix="plan_demo_"))
    wide = tmpdir / "wide.parquet"
    make_wide_parquet(con, wide, 100_000)

    sql = "SELECT id, v FROM read_parquet(?) WHERE id < 50000"

    print("  DuckDB 默认的物理计划 —— 过滤被塞进扫描节点：")
    for line in con.sql(sql, params=[str(wide)]).explain().splitlines():
        print(f"    {line}")

    print()
    print("  DuckDB 的「未优化」计划（`PRAGMA explain_output='all'` 的第一份）——")
    print("  这时候过滤是一个**独立节点**，还压在扫描上面：")
    con.execute("PRAGMA explain_output='all'")
    plans = con.sql(f"EXPLAIN {sql}", params=[str(wide)]).fetchall()
    for line in plans[0][1].splitlines():
        print(f"    {line}")
    print(f"    （`explain_output='all'` 一共给了 {len(plans)} 份计划：")
    print("      未优化 / 已优化 / 物理）")
    con.execute("PRAGMA explain_output='physical_only'")

    print()
    print("  Polars 的同一个查询，**优化前**：")
    lf = pl.scan_parquet(wide).filter(pl.col("id") < 50000).select("id", "v")
    for line in lf.explain(optimized=False).splitlines():
        print(f"    {line}")

    print()
    print("  Polars **优化后**：")
    for line in lf.explain(optimized=True).splitlines():
        print(f"    {line}")

    print("\n→ 两边都下推了，但**下推的样子不一样**：")
    print("  · DuckDB 把过滤和投影写在**同一个节点**里：")
    print("    `Projections: id, v` 和 `Filters: id < 50000` 都在 `Read Parquet` 下面。")
    print("    从优化前的计划能看到，过滤原本是一个独立的 `Filter` 节点，")
    print("    优化器把它**塞进了扫描节点** —— 少一次中间结果物化。")
    print("  · Polars 优化前是独立的 `FILTER` 节点，优化后变成扫描节点里的 `SELECTION:` 一行。")
    print("    语义一样，**画法**不一样。")
    print("  · 真正要看「过滤是不是真的在扫描时做掉的」，DuckDB 的 `EXPLAIN ANALYZE`")
    print("    会给出扫描节点的行数；Polars 这边没有对应物 ——")
    print("    它的 `ESTIMATED ROWS` 是**估算**，不是实测。")


# ---------------------------------------------------------------- 4. 优化开关


def scenario_optimized(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 什么时候才看得见优化结果：两边的默认值不一样")

    tmpdir = Path(tempfile.mkdtemp(prefix="plan_demo_"))
    wide = tmpdir / "wide.parquet"
    make_wide_parquet(con, wide, 100_000)

    lf = pl.scan_parquet(wide).filter(pl.col("id") < 50000).select("id", "v")

    print("  Polars：`explain()` 的默认值就是**优化后**的（`optimized=True` 是默认行为）。")
    same_default = lf.explain() == lf.explain(optimized=True)
    print(f"    lf.explain() == lf.explain(optimized=True) -> {same_default}")
    print("    所以想看优化**前**的样子，得显式写 optimized=False：")
    for line in lf.explain(optimized=False).splitlines():
        print(f"    {line}")

    print()
    print("  DuckDB：默认值是 `physical_only` —— 直接给物理计划。")
    con.execute("PRAGMA explain_output='all'")
    plans = con.sql(
        "EXPLAIN SELECT id, v FROM read_parquet(?) WHERE id < 50000", params=[str(wide)]
    ).fetchall()
    print(f"    `PRAGMA explain_output='all'` 一共给了 {len(plans)} 份计划：")
    for idx, (_key, body) in enumerate(plans):
        nodes = plan_nodes(body)
        print(f"      第 {idx + 1} 份：{' -> '.join(nodes)}")
    con.execute("PRAGMA explain_output='physical_only'")

    print()
    print("  DuckDB 还有一个 Polars 没有的开关：`PRAGMA explain_output` 还能取")
    print("  `optimized_only`（只看逻辑计划）和 `all`（三份一起给）。")

    print("\n→ 看计划的**前提**两边不一样：")
    print("  · Polars 默认给的就是优化后的结果 —— 想看优化前必须显式要。")
    print("  · DuckDB 默认给的是**物理**计划 —— 想看逻辑计划得改 PRAGMA。")
    print("  · 「计划」这个词在两边指的不是一回事：")
    print("    DuckDB 的默认 = 最终会跑的算子；Polars 的默认 = 优化后的算子树。")
    print("  · 结论：**看计划不能跨引擎比对**，只能在各自的语境里看。")


# ---------------------------------------------------------------- 5. 聚合算法


def scenario_aggregate(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 聚合：DuckDB 会告诉你它选了哪种算法，Polars 不会")

    con.execute(
        "CREATE OR REPLACE TABLE agg_t AS SELECT i AS id, i % 7 AS g,"
        " (i * 7919)::BIGINT AS wide_k, 'g' || (i % 50) AS s FROM range(100000) t(i)"
    )

    print("  同一个 GROUP BY，换不同的分组键，DuckDB 的计划就换了算法：")
    print()
    for label, sql in [
        ("小值域整数（7 组）", "SELECT g, count(*) FROM agg_t GROUP BY g"),
        ("大值域整数（10 万组）", "SELECT wide_k, count(*) FROM agg_t GROUP BY wide_k"),
        ("字符串键（50 组）", "SELECT s, count(*) FROM agg_t GROUP BY s"),
    ]:
        nodes = plan_nodes(con.sql(sql).explain())
        algo = next((n for n in nodes if "Group By" in n), "（没找到聚合节点）")
        print(f"    {common.pad(label, 26)}-> {algo}")

    print()
    print("  同样三个查询，Polars 的计划里**永远是同一个节点**：")
    for label, expr in [
        ("小值域整数（7 组）", pl.col("g")),
        ("大值域整数（10 万组）", pl.col("wide_k")),
        ("字符串键（50 组）", pl.col("s")),
    ]:
        lf = (
            pl.LazyFrame(
                {
                    "id": list(range(1000)),
                    "g": [i % 7 for i in range(1000)],
                    "wide_k": [i * 7919 for i in range(1000)],
                    "s": [f"g{i % 50}" for i in range(1000)],
                }
            )
            .group_by(expr)
            .agg(pl.len())
        )
        first = lf.explain(optimized=True).splitlines()[0]
        print(f"    {common.pad(label, 26)}-> {first}")

    print("\n→ 这一段是「两边根本对不上」的第一处：")
    print("  · DuckDB 的计划**暴露了算法选择**：分组键值域小的时候用 `Perfect Hash Group By`")
    print("    （直接开数组下标寻址），值域大或者键是字符串就退回普通的 `Hash Group By`。")
    print("    这两种算法的常数差得很远，而**选哪个取决于数据的值，不取决于 schema**。")
    print("  · Polars 的计划只有一个 `AGGREGATE[maintain_order: false]`，")
    print("    三种数据给出**字面上完全一样**的节点名 —— 你看不出它内部会走哪条路。")
    print("  · 顺带注意 `maintain_order: false`：Polars 默认**不保证**聚合结果的行序，")
    print("    DuckDB 的 `Perfect Hash Group By` 出来的顺序也是按 hash 槽位排的，不是排序后的。")
    print("    → 想要稳定行序，两边都得**显式 ORDER BY**，别指望默认。")
    print("  · 实用结论：DuckDB 的计划可以直接拿来判断「这次聚合会不会走慢路径」；")
    print("    Polars 的计划只能告诉你「会做一次聚合」，别的什么都看不出来。")


# ---------------------------------------------------------------- 6. Join


def scenario_join(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · Join：节点名完全对不上")

    con.execute("CREATE OR REPLACE TABLE a AS SELECT i AS id, i % 5 AS k FROM range(100) t(i)")
    con.execute("CREATE OR REPLACE TABLE b AS SELECT i AS id, i * 2 AS w FROM range(50) t(i)")

    print("  DuckDB 的 LEFT JOIN 计划：")
    for line in (
        con.sql("SELECT a.id, b.w FROM a LEFT JOIN b ON a.id = b.id").explain().splitlines()
    ):
        print(f"    {line}")

    print()
    print("  Polars 的 LEFT JOIN 计划：")
    la = pl.LazyFrame({"id": list(range(100)), "k": [i % 5 for i in range(100)]})
    lb = pl.LazyFrame({"id": list(range(50)), "w": [i * 2 for i in range(50)]})
    for line in la.join(lb, on="id", how="left").explain(optimized=True).splitlines():
        print(f"    {line}")

    print()
    print("  DuckDB 的 INNER JOIN 计划（节点名一样，差别在 `Join Type` 那一行）：")
    for line in con.sql("SELECT a.id, b.w FROM a JOIN b ON a.id = b.id").explain().splitlines():
        print(f"    {line}")

    print()
    print("  换个连接条件，DuckDB **连节点名都换了** —— 算法是它自己挑的：")
    for label, sql in [
        ("等值连接", "SELECT a.id, b.w FROM a JOIN b ON a.id = b.id"),
        ("非等值连接", "SELECT a.id, b.w FROM a JOIN b ON a.id > b.id"),
        ("笛卡尔积", "SELECT count(*) FROM a CROSS JOIN b"),
    ]:
        nodes = plan_nodes(con.sql(sql).explain())
        algo = next((n for n in nodes if "Join" in n or "Product" in n), "（没找到 join 节点）")
        print(f"    {common.pad(label, 16)}-> {algo}")

    print()
    print("  同样两种连接，Polars 的计划里：")
    la2 = pl.LazyFrame({"id": list(range(100)), "k": [i % 5 for i in range(100)]})
    lb2 = pl.LazyFrame({"id": list(range(50)), "w": [i * 2 for i in range(50)]})
    for label, frame in [
        ("等值连接", la2.join(lb2, on="id", how="inner")),
        ("非等值连接", la2.join(lb2, left_on="id", right_on="id", how="inner")),
    ]:
        first = frame.explain(optimized=True).splitlines()[0]
        print(f"    {common.pad(label, 16)}-> {first}")

    print("\n→ 这一段两边**结构上是能对上的**，只是名字不一样：")
    print("  · DuckDB 的 `Hash Join` + `Join Type: LEFT` 两行，")
    print("    对应 Polars 的 `LEFT JOIN` + `LEFT PLAN ON: [...]`。")
    print("    LEFT / INNER / SEMI 在 DuckDB 里是同一个节点加一行 `Join Type:`，")
    print("    在 Polars 里是三个不同的节点名。")
    print("  · 但 DuckDB 一定会告诉你**用的什么算法**（`Hash Join` / `Piecewise Merge Join` /")
    print("    `Cross Product`），Polars 只写 `INNER JOIN` / `LEFT JOIN` ——")
    print("    底下是 hash join 还是 merge join，计划里一个字都没有。")
    print("  · → 所以「看计划判断 join 会不会退化」这件事，**只有 DuckDB 做得到**。")


# ---------------------------------------------------------------- 7. 前提


def scenario_prereq(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 前提：谁有计划、计划里有什么、看不到什么")

    print("  第一件事：Polars **只有 LazyFrame 有计划**。")
    eager = pl.DataFrame({"id": [1, 2, 3], "v": [1.0, 2.0, 3.0]})
    print(f"    pl.DataFrame 有 explain 方法吗 -> {hasattr(eager, 'explain')}")
    print("    → eager 的 DataFrame 是**已经算完的结果**，没有计划可言。")
    print("      想看计划得先 `.lazy()`：")
    for line in eager.lazy().explain(optimized=True).splitlines():
        print(f"      {line}")
    print("    DuckDB 没有这个区分：`EXPLAIN` 的是 SQL，SQL 本来就是惰性的。")

    print()
    print("  第二件事：DuckDB 的 `EXPLAIN` **不执行**查询，`EXPLAIN ANALYZE` **会执行**。")
    tmpdir = Path(tempfile.mkdtemp(prefix="plan_demo_"))
    wide = tmpdir / "wide.parquet"
    make_wide_parquet(con, wide, 100_000)
    plain = con.sql("EXPLAIN SELECT sum(v) FROM read_parquet(?)", params=[str(wide)]).fetchall()[0][
        1
    ]
    analyzed = con.sql(
        "EXPLAIN ANALYZE SELECT sum(v) FROM read_parquet(?)", params=[str(wide)]
    ).fetchall()[0][1]
    print("    同一句 SQL，两种 EXPLAIN 的输出里有没有这些字：")
    for key in ("Total Time", "Data Read", "Row Groups Scanned"):
        has_plain = key in plain
        has_analyzed = key in analyzed
        print(
            f"      {common.pad(key, 20)}EXPLAIN: {has_plain!s:<5}  EXPLAIN ANALYZE: {has_analyzed}"
        )
    print("    → `EXPLAIN` 只做编译（给的是**估算**行数），`EXPLAIN ANALYZE`")
    print("      会**真的把查询跑一遍**再报实测值 —— 别在大表上随手用。")

    print()
    print("  第三件事：Polars 侧两个想「看细节」的入口在 2.x 都没了。")
    lf = pl.LazyFrame({"id": [1, 2, 3]}).filter(pl.col("id") > 1)
    print(f"    LazyFrame.profile 存在吗 -> {hasattr(lf, 'profile')}")
    print(f"    LazyFrame.show_graph 存在吗 -> {hasattr(lf, 'show_graph')}")
    try:
        lf.show_graph()
    except Exception as exc:
        print(f"    show_graph() 实际调用 -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("    → `profile()` 在 2.0 被**移除**了（流式引擎下逐节点计时会误导人）；")
    print("      `show_graph()` 还在，但还要额外的 matplotlib，本机没装。")
    print("    所以 Polars 这边能看的，就只有 `explain()` 出来的**那几行文本**。")

    print()
    print("  第四件事，也是最要命的一件：**Polars 没有任何运行时指标**。")
    prof = duck_profiling(
        con, "SELECT id, v FROM read_parquet(?)", [str(wide)], tmpdir / "prof.json"
    )
    print("    DuckDB 的 profiling JSON 里有这些：")
    print(f"      顶层键：{sorted(prof.keys())}")
    print(f"      io.total_bytes_read：{prof['io']['total_bytes_read']:,}")
    print("    Polars 侧：`collect()` 返回一个光秃秃的 DataFrame，没有任何元数据。")
    print("    → 想在 Polars 里做同样的事，只能自己在外面套 `time.perf_counter()`，")
    print("      拿不到「读了几个字节」「扫了多少行」这种引擎内部数字。")

    print("\n→ 这一段是两边**最不对等**的地方：")
    print("  · DuckDB 的 EXPLAIN 是一条**完整的诊断链**：")
    print("    计划（盒式图）→ 三种粒度的计划（PRAGMA）→ 实测（EXPLAIN ANALYZE）")
    print("    → 机器读得懂的指标（profiling JSON）。")
    print("  · Polars 的 EXPLAIN 是**一段文本**：`explain()` 给你缩进树，`optimized=False`")
    print("    给你优化前的版本，**到这儿就结束了**。")
    print("  · 所以「用计划定位问题」这件事：DuckDB 能一路查到底，")
    print("    Polars 只能在「计划对不对」这一层打转。")
    print("  · 好消息是 Polars 的计划够干净 —— 算子树一层就够看懂了；")
    print("    坏消息是它**不告诉你代价**，只告诉你结构。")


# ---------------------------------------------------------------- 8. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 8 · 代价：投影裁剪到底值多少时间")

    print(f"数据：{rows} 行 × {3 + len(WIDE_EXTRA_COLUMNS)} 列的宽表")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="plan_demo_"))
    src = tmpdir / "wide.parquet"
    make_wide_parquet(con, src, rows)
    print(f"文件大小：{src.stat().st_size:,} 字节")

    duck_narrow = "SELECT sum(v) FROM read_parquet(?)"
    duck_wide = (
        "SELECT sum(v) + "
        + " + ".join(f"sum({c})" for c in WIDE_NUMERIC_NAMES)
        + " + "
        + " + ".join(f"sum(length({c}))" for c in WIDE_TEXT_NAMES)
        + " FROM read_parquet(?)"
    )

    def pol_wide_expr() -> pl.Expr:
        expr = pl.col("v").sum()
        for name in WIDE_NUMERIC_NAMES:
            expr = expr + pl.col(name).sum()
        for name in WIDE_TEXT_NAMES:
            expr = expr + pl.col(name).str.len_chars().sum()
        return expr

    def duck_narrow_run() -> float:
        return float(con.sql(duck_narrow, params=[str(src)]).fetchall()[0][0])

    def duck_wide_run() -> float:
        return float(con.sql(duck_wide, params=[str(src)]).fetchall()[0][0])

    def pol_narrow_run() -> float:
        frame = pl.scan_parquet(src).select(pl.col("v").sum()).collect()
        return float(frame.item())

    def pol_wide_run() -> float:
        frame = pl.scan_parquet(src).select(pol_wide_expr()).collect()
        return float(frame.item())

    results = [
        ("DuckDB 只读 1 列", duck_narrow_run),
        ("DuckDB 读全部 12 列", duck_wide_run),
        ("Polars 只读 1 列", pol_narrow_run),
        ("Polars 读全部 12 列", pol_wide_run),
    ]

    print(
        "\n  "
        + common.pad("实现", 26)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 22)
    )
    print("  " + "-" * 74)
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
            + common.pad_left(f"{value:,.0f}", 22)
        )

    print("\n口径：数据已物化成 parquet，两个引擎读的是**同一个文件**；")
    print("  「只读 1 列」和「读全部 12 列」在两边指的是**同一件事**：")
    print("  把要读的列读出来做聚合（数值列求和、字符串列求长度和），区别只在读几列。")
    print("  计时包含「打开文件 + 读 parquet + 解码 + 聚合」的全过程。")
    print("  DuckDB 侧每次都重新 parse + bind SQL；Polars 侧每次都重新 scan + collect，")
    print("  两边都是「从零跑一遍这条查询」的口径。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · 这就是场景 2 那张 profiling JSON 的**时间版本**：")
    print("    少读几列，parquet 就少解几个 column chunk，少解的量直接体现在耗时上。")
    print("  · 差距的大小取决于「被丢掉的列」有多宽 —— 一列窄的 int 和一列宽的 string，")
    print("    省下来的字节数差着量级。所以**列的顺序和选列**在宽表上比想象中值钱。")
    print("  · 反过来说，「我只 select 了几列」这句话在两个引擎里都是**真的**，")
    print("    不需要担心 select * 把整张表拖下来 —— 前提是别在后面又 `SELECT *` 回去。")
    print("  · 真正会拖慢的是**列多、行多、还要排序/聚合**的组合：")
    print("    裁剪只省 IO，省不掉 CPU 上的那部分。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的查询计划差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_explain(con)
        scenario_projection(con)
        scenario_filter_pushdown(con)
        scenario_optimized(con)
        scenario_aggregate(con)
        scenario_join(con)
        scenario_prereq(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
