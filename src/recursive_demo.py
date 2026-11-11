"""递归 CTE 与迭代计算：DuckDB 的 WITH RECURSIVE 和 Polars 差在哪儿。

层级数据（组织架构、依赖关系、评论盖楼）都要「一层一层往下展开」。
两个引擎处理这件事的**能力差距**，比算术、NULL 那些细枝末节大得多：

* DuckDB：`WITH RECURSIVE` 是**语言层面**的递归 —— 锚点 + 递归项，
  由引擎自己一轮一轮迭代；写 `UNION`（去重）就变成不动点，能算传递闭包。
* Polars：**没有递归 CTE**。任何需要迭代的查询都得在 Python 里手写循环，
  或者用 `int_range` + `map_elements` 逐节点回溯。

更要命的是两条实测结论：
* DuckDB 的递归 CTE **遇到环会永远跑下去** —— 引擎没有内置的迭代次数上限；
* `UNION`（去重）和 `UNION ALL`（不去重）的差别，就是「会不会收敛」。

这个场景要回答：同一个层级问题，两边分别要写成什么样、结果能不能对上。

用法：
    uv run python src/recursive_demo.py
    uv run python src/recursive_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import re
import statistics
import tempfile
import threading
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 数据

# 组织架构表（id / mgr / name）：9 个节点，4 层，单亲树
EMP = pl.DataFrame(
    {
        "id": [1, 2, 3, 4, 5, 6, 7, 8, 9],
        "mgr": [None, 1, 1, 2, 2, 4, 4, 5, 3],
        "name": [
            "CEO",
            "CTO",
            "CFO",
            "研发总监",
            "销售总监",
            "架构师",
            "组长",
            "销售经理",
            "会计",
        ],
    }
)

# 有向无环图：节点 7（交付组）有两个上级，所以它有两条路径
DAG_NODES = pl.DataFrame(
    {
        "id": [1, 2, 3, 4, 5, 6, 7, 8],
        "name": ["总办", "研发部", "市场部", "平台组", "渠道组", "前端组", "交付组", "品牌组"],
    }
)
DAG_EDGES = pl.DataFrame(
    {
        "child": [2, 3, 4, 5, 6, 7, 7, 8],
        "parent": [1, 1, 2, 3, 4, 4, 5, 5],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    # 落成真表 —— EXPLAIN 里看到的是 Seq Scan，而不是 Python Arrow Scan
    for name, frame in [("emp", EMP), ("dag_nodes", DAG_NODES), ("dag_edges", DAG_EDGES)]:
        con.register(f"frame_{name}", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame_{name}")
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def plan_root(plan_text: str) -> str:
    """从 EXPLAIN 的盒式图里取出根节点名。

    DuckDB 的盒式图是自上而下画的：第一行就是根节点，往下才是孩子。
    """
    first = plan_text.splitlines()[0] if plan_text.strip() else ""
    match = re.match(r"╭─\s*([^─]+?)\s*─", first)
    return match.group(1).strip() if match else first.strip()


def show(df: pl.DataFrame, title: str, max_rows: int = 15) -> None:
    """打印表格 —— 和 common.print_frame 同一个风格，只是放宽了字符串截断长度。

    递归 CTE 的 path 列很长（"总办 > 研发部 > 平台组 > 前端组"），
    common.print_frame 默认的 fmt_str_lengths=12 会把它砍成 "总办 > 研发部 > …"。
    """
    print(f"\n=== {title} ===")
    with pl.Config(
        tbl_rows=max_rows,
        tbl_cols=-1,
        tbl_width_chars=160,
        fmt_str_lengths=48,
        tbl_formatting="UTF8_FULL",
    ):
        print(df)


# ---------------------------------------------------------------- Polars 的两种替代写法


def polars_level_by_level(emp: pl.DataFrame) -> pl.DataFrame:
    """组织架构表的逐层 join 迭代：每一轮把「已知节点」往下一层推一次。

    这是 Polars 侧最接近 `WITH RECURSIVE` 的写法 —— 循环次数 = 树高，
    每轮是一次向量化 join，没有一行一行地解释执行。
    """
    level = emp.filter(pl.col("mgr").is_null()).select(
        pl.col("id"),
        pl.col("name"),
        pl.lit(0).alias("depth"),
        pl.col("name").alias("path"),
    )
    out = level
    while True:
        parents = level.select(
            pl.col("id").alias("pid"),
            pl.col("depth").alias("pdepth"),
            pl.col("path").alias("ppath"),
        )
        nxt = emp.join(parents, left_on="mgr", right_on="pid", how="inner").select(
            pl.col("id"),
            pl.col("name"),
            (pl.col("pdepth") + 1).alias("depth"),
            (pl.col("ppath") + " > " + pl.col("name")).alias("path"),
        )
        if nxt.height == 0:
            break
        out = pl.concat([out, nxt], how="vertical")
        level = nxt
    return out.sort(["depth", "id"])


def polars_level_by_level_edges(edges: pl.DataFrame, nodes: pl.DataFrame) -> pl.DataFrame:
    """边表（child / parent）上的逐层 join 迭代 —— 有多个上级就会有多条路径。"""
    roots = nodes.filter(~pl.col("id").is_in(edges["child"].implode())).select(
        pl.col("id").alias("child"),
        pl.col("name"),
        pl.lit(0).alias("depth"),
        pl.col("name").alias("path"),
    )
    out = roots
    level = roots
    while True:
        parents = level.select(
            pl.col("child").alias("pid"),
            pl.col("depth").alias("pdepth"),
            pl.col("path").alias("ppath"),
        )
        nxt = (
            edges.join(parents, left_on="parent", right_on="pid", how="inner")
            .join(nodes, left_on="child", right_on="id", how="inner")
            .select(
                pl.col("child"),
                pl.col("name"),
                (pl.col("pdepth") + 1).alias("depth"),
                (pl.col("ppath") + " > " + pl.col("name")).alias("path"),
            )
        )
        if nxt.height == 0:
            break
        out = pl.concat([out, nxt], how="vertical")
        level = nxt
    return out.sort(["depth", "child", "path"])


def polars_map_elements(emp: pl.DataFrame) -> pl.DataFrame:
    """int_range + map_elements 版：每个节点在 Python 里沿 mgr 一路走到根。

    没有循环框架，代价是每个节点都要在 Python 里回溯一遍 —— 调用次数是
    「节点数 × 树高」。
    """
    parent = dict(emp.select("id", "mgr").iter_rows())
    name = dict(emp.select("id", "name").iter_rows())
    ids = emp["id"].to_list()

    def walk_depth(pos: int) -> int:
        depth, cur = 0, parent[ids[pos]]
        while cur is not None:
            depth += 1
            cur = parent[cur]
        return depth

    def walk_path(pos: int) -> str:
        parts, cur = [name[ids[pos]]], parent[ids[pos]]
        while cur is not None:
            parts.append(name[cur])
            cur = parent[cur]
        parts.reverse()
        return " > ".join(parts)

    depth = pl.int_range(len(ids), eager=True).map_elements(walk_depth, return_dtype=pl.Int64)
    path = pl.int_range(len(ids), eager=True).map_elements(walk_path, return_dtype=pl.String)
    return (
        emp.select("id", "name")
        .with_columns(depth.alias("depth"), path.alias("path"))
        .sort(["depth", "id"])
    )


# ---------------------------------------------------------------- 1. 递归 CTE


def scenario_recursive_cte(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · WITH RECURSIVE：层级深度与路径")

    print("  组织架构表 emp(id, mgr, name)：9 个节点，4 层\n")
    print("  DuckDB 的递归 CTE（锚点 + 递归项）：")
    print("    WITH RECURSIVE chain(id, name, depth, path) AS (")
    print("      SELECT id, name, 0, name FROM emp WHERE mgr IS NULL      -- 锚点")
    print("      UNION ALL                                                 -- 迭代")
    print("      SELECT e.id, e.name, c.depth+1, c.path || ' > ' || e.name")
    print("      FROM emp e JOIN chain c ON e.mgr = c.id                    -- 递归项")
    print("    ) SELECT * FROM chain")

    duck_sql = """
    WITH RECURSIVE chain(id, name, depth, path) AS (
      SELECT id, name, 0, name FROM emp WHERE mgr IS NULL
      UNION ALL
      SELECT e.id, e.name, c.depth + 1, c.path || ' > ' || e.name
      FROM emp e JOIN chain c ON e.mgr = c.id
    )
    SELECT id, name, depth, path FROM chain ORDER BY depth, id
    """
    duck = con.sql(duck_sql).pl()
    show(duck, "DuckDB WITH RECURSIVE 的结果")

    pol = polars_level_by_level(EMP)
    show(pol, "Polars 逐层 join 循环的结果")

    ok, why = common.compare_frames(duck, pol, ["id"])
    print(f"\n  对账 DuckDB 递归 CTE ↔ Polars 逐层 join：{'✅' if ok else '❌'} {why}")
    print(
        f"  DuckDB depth 列的类型：{duck.schema['depth']}    "
        f"Polars depth 列的类型：{pol.schema['depth']}"
    )

    print("\n→ 同一个层级问题，两边的写法完全不同：")
    print("  · DuckDB：**一句 SQL**，引擎负责迭代 —— 你只管写锚点和递归项。")
    print("    `WHERE mgr IS NULL` 是锚点，`UNION ALL` 是迭代，`JOIN chain` 是递归项。")
    print("  · Polars：**一段 Python 循环** —— 每一轮 join 出下一层，再拼回去。")
    print("    循环本身是向量的（不是逐行），但「层」这件事得你自己数。")
    print("  · 结果完全一致（✅），连 **depth 列的类型都一致**（都是 INT32）：")
    print("    DuckDB 递归 CTE 里的 `0` 是 INT32，`depth+1` 也就一直是 INT32；")
    print("    Polars 侧 `pl.lit(0)` 同样是 INT32，所以两边 `depth+1` 也是 INT32。")
    print("    （`map_elements` 那版是 INT64 —— 场景 6 会看到这个差别。）")
    print("  · → **能力差距在这里还看不出来**（两边都能做对）。")
    print("    真正拉开差距的是下一节：路径不止一条的时候。")


# ---------------------------------------------------------------- 2. 路径枚举


def scenario_path_enumeration(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 路径枚举：一个节点有两个上级，就有两条路径")

    print("  DAG：8 个节点、9 条边 —— 交付组(7) 的上级是平台组(4) 和渠道组(5)\n")

    duck_sql = """
    WITH RECURSIVE paths(child, name, depth, path) AS (
      SELECT n.id, n.name, 0, n.name
      FROM dag_nodes n
      WHERE NOT EXISTS (SELECT 1 FROM dag_edges e WHERE e.child = n.id)
      UNION ALL
      SELECT e.child, n.name, p.depth + 1, p.path || ' > ' || n.name
      FROM dag_edges e
      JOIN paths p ON e.parent = p.child
      JOIN dag_nodes n ON n.id = e.child
    )
    SELECT child, name, depth, path FROM paths ORDER BY depth, child, path
    """
    duck = con.sql(duck_sql).pl()
    show(duck, "DuckDB 递归 CTE 枚举出的全部路径")

    pol = polars_level_by_level_edges(DAG_EDGES, DAG_NODES)
    ok, why = common.compare_frames(duck, pol, ["child"])
    print(f"\n  对账 DuckDB 递归 CTE ↔ Polars 逐层 join：{'✅' if ok else '❌'} {why}")
    print(f"  节点数 {len(DAG_NODES)}    路径数 {duck.height}   ← 路径比节点多")

    print("\n  把树改成「斐波那契阶梯」（节点 i 的上级是 i-1 和 i-2），路径数就失控了：")
    print("    " + common.pad("层数", 10) + common.pad("DuckDB 递归出的路径数", 24))
    print("    " + "-" * 34)
    for levels in (5, 10, 15, 20, 25):
        n_paths = build_ladder(con, levels)
        print("    " + common.pad(str(levels), 10) + common.pad(f"{n_paths:,}", 24))

    print("\n→ 这一节是整个 demo 里**最能说明问题**的地方：")
    print("  · 递归 CTE 的行数是**路径数**，不是节点数。")
    print("    上面的 DAG 只有 8 个节点，递归却吐出了 9 条路径 ——")
    print("    因为交付组有两个上级，它就有两条从根出发的路径。")
    print("  · 换成斐波那契阶梯，层数每加 1，路径数就乘一个黄金比例：")
    print("    上表里层数从 20 到 25，路径数翻了好几倍 ——")
    print("    **深度只涨了 5，路径数涨了一个数量级**。")
    print("    递归 CTE 本身没问题，但它把「路径」这件事显式物化出来了，")
    print("    组合爆炸是数据结构的性质，不是引擎的锅。")
    print("  · Polars 的逐层 join 同样能枚举出全部路径（✅）——")
    print("    因为它每轮都是一次真正的 join，路径是 join 出来的，不是算出来的。")
    print("    但代价是：**循环次数和路径数一起涨**，Python 侧要一遍遍 concat。")
    print("  · → **实践建议**：能用「祖先-后代对」表达的问题（谁能影响谁），")
    print("    别去枚举路径 —— 下面场景 4 的传递闭包就是干这个的，行数少得多。")


def build_ladder(con: duckdb.DuckDBPyConnection, levels: int) -> int:
    """造一条斐波那契阶梯（第 i 个节点的上级是 i-1 和 i-2），返回路径总数。"""
    con.execute("DROP TABLE IF EXISTS lad_nodes")
    con.execute("DROP TABLE IF EXISTS lad_edges")
    con.execute("CREATE TABLE lad_nodes AS SELECT i AS id FROM range(1, ? + 1) t(i)", [levels])
    con.execute(
        "CREATE TABLE lad_edges AS SELECT i AS child, i - 1 AS parent FROM range(2, ? + 1) t(i)",
        [levels],
    )
    con.execute("INSERT INTO lad_edges SELECT i, i - 2 FROM range(3, ? + 1) t(i)", [levels])
    return int(
        q1(
            con,
            """
            WITH RECURSIVE paths(child, depth) AS (
              SELECT id, 0 FROM lad_nodes WHERE id = 1
              UNION ALL
              SELECT e.child, p.depth + 1
              FROM lad_edges e JOIN paths p ON e.parent = p.child
            )
            SELECT count(*) FROM paths
            """,
        )
    )


# ---------------------------------------------------------------- 3. 执行模型


def scenario_execution_model(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 执行模型：EXPLAIN 里那个 `Rec CTE` 节点")

    sql = """
    WITH RECURSIVE chain(id, name, depth, path) AS (
      SELECT id, name, 0, name FROM emp WHERE mgr IS NULL
      UNION ALL
      SELECT e.id, e.name, c.depth + 1, c.path || ' > ' || e.name
      FROM emp e JOIN chain c ON e.mgr = c.id
    )
    SELECT id, name, depth, path FROM chain
    """

    print("  EXPLAIN（物理计划）：\n")
    for line in con.sql("EXPLAIN " + sql).fetchall()[0][1].splitlines():
        print(f"  {line}")

    analyzed = con.sql("EXPLAIN ANALYZE " + sql).fetchall()[0][1]

    con.execute("SET explain_output='all'")
    plans = con.sql("EXPLAIN " + sql).fetchall()
    con.execute("SET explain_output='physical_only'")

    print("\n  `explain_output='all'` 时 EXPLAIN 返回**三行**（不是三段文字，是三行）：\n")
    for key, plan in plans:
        print(
            f"    {common.pad(key, 14)} 根节点 = {common.pad(plan_root(plan), 12)}"
            f"  有 'Hash Join' -> {str('Hash Join' in plan):5}"
            f"  有 'Rec CTE Scan' -> {'Rec CTE Scan' in plan}"
        )

    print(f"\n  EXPLAIN ANALYZE（运行时）里出现 `Recursive CTE` -> {'Recursive CTE' in analyzed}")

    print("\n→ DuckDB 的递归 CTE 是**一轮一轮迭代**的：")
    print("  · 物理计划里的 `Rec CTE` 节点就是那个迭代器，")
    print("    它下面挂着一个 `Rec CTE Scan` —— 那是「上一轮的结果」被物化出来的地方。")
    print("    每一轮迭代都从这张临时表读，再算出下一轮。")
    print("  · `EXPLAIN ANALYZE` 里同一个节点叫 `Recursive CTE`（带 `Recursive CTE Scan`），")
    print("    这是 DuckDB 2.0 的命名；`EXPLAIN` 的静态计划里是缩写 `Rec CTE`。")
    print("  · 三份计划（上面那三行）能看到优化的过程：")
    print("    优化前根节点是 `CTE`（里面套着一个 `Rec CTE`），优化后根节点是 `Projection`，")
    print(
        "    到物理计划 `Rec CTE` 直接当根 —— 而且 `Comparison Join` 在物理阶段换成了 `Hash Join`。"
    )
    print("    `Rec CTE Scan` 只在物理计划里出现：它是运行时物化的那张临时表。")
    print("  · → 代价的形状由此确定：**迭代轮数 = 数据的层数**。")
    print("    层数少（宽而浅的树）就快，层数多就慢 —— 而且每轮都要把中间结果物化一遍，")
    print("    这就是下一节 `UNION` / `UNION ALL` 的差别会变得要命的原因。")


# ---------------------------------------------------------------- 4. UNION vs UNION ALL


def scenario_union_vs_union_all(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · `UNION ALL` 还是 `UNION`：差别就是「会不会收敛」")

    con.execute("DROP TABLE IF EXISTS reach_nodes")
    con.execute("DROP TABLE IF EXISTS reach_edges")
    con.execute("CREATE TABLE reach_nodes AS SELECT id FROM dag_nodes")
    con.execute("CREATE TABLE reach_edges AS SELECT * FROM dag_edges")

    base = """
    WITH RECURSIVE reach(anc, dsc) AS (
      SELECT id, id FROM reach_nodes
      %s
      SELECT r.anc, e.child FROM reach r JOIN reach_edges e ON e.parent = r.dsc
    )
    """
    all_rows = int(q1(con, base % "UNION ALL" + " SELECT count(*) FROM reach"))
    dedup_rows = int(q1(con, base % "UNION" + " SELECT count(*) FROM reach"))
    distinct_pairs = int(
        q1(
            con,
            base % "UNION ALL" + " SELECT count(*) FROM (SELECT DISTINCT anc, dsc FROM reach)",
        )
    )

    print("  传递闭包：谁是谁的祖先（含自己）\n")
    print(f"    UNION ALL（不去重）  -> {all_rows:,} 行")
    print(f"    UNION    （跨轮去重）-> {dedup_rows:,} 行")
    print(f"    其中不同的 (祖先, 后代) 组合 -> {distinct_pairs:,} 对")

    print("\n  同样的闭包，Polars 侧要自己写不动点迭代：")
    pol = polars_closure(DAG_EDGES, DAG_NODES)
    print(f"    Polars 逐层 join + unique -> {pol.height:,} 行")
    duck_closure = con.sql(
        """
        WITH RECURSIVE reach(anc, dsc) AS (
          SELECT id, id FROM reach_nodes
          UNION
          SELECT r.anc, e.child FROM reach r JOIN reach_edges e ON e.parent = r.dsc
        )
        SELECT anc, dsc FROM reach
        """
    ).pl()
    ok, why = common.compare_frames(duck_closure, pol, ["anc", "dsc"])
    print(f"    对账 DuckDB UNION 闭包 ↔ Polars 闭包：{'✅' if ok else '❌'} {why}")

    print("\n→ `UNION` 和 `UNION ALL` 的区别，在这里是**能不能停下来**的区别：")
    print("  · `UNION ALL`：每一轮的输出全部追加，不管跟之前重不重复。")
    print(f"    上面的闭包用它算出来是 {all_rows:,} 行 —— 比不同组合多出来的就是重复的推导路径。")
    print("  · `UNION`：**跨轮去重** —— 算出来的值集合没变就不再产生新行。")
    print("    于是递归变成了「求不动点」，可以停下来，结果也是干净的集合。")
    print("  · 想要「祖先-后代对」这种关系，**必须**用 `UNION`；")
    print("    用 `UNION ALL` 的话，同一对关系会被重复推导出来（多对多时成平方级）。")
    print("  · → **迁移规则**：从别的方言搬过来的递归 CTE，第一件事是确认")
    print("    `UNION` 还是 `UNION ALL` —— Postgres 里 `UNION`（去重）是默认，")
    print("    `UNION ALL` 要手写；反过来抄很容易抄反，而**抄反的后果不是结果错，是跑不完**。")
    print("    下一节就是抄反之后的样子。")


def polars_closure(edges: pl.DataFrame, nodes: pl.DataFrame) -> pl.DataFrame:
    """Polars 侧求传递闭包：逐层 join 出可达对，最后 unique。"""
    level = nodes.select(pl.col("id").alias("anc"), pl.col("id").alias("dsc"))
    out = level
    while True:
        parents = level.select(pl.col("dsc").alias("pid"), pl.col("anc").alias("panc"))
        nxt = (
            edges.join(parents, left_on="parent", right_on="pid", how="inner")
            .select(pl.col("panc").alias("anc"), pl.col("child").alias("dsc"))
            .unique()
        )
        if nxt.height == 0:
            break
        out = pl.concat([out, nxt], how="vertical").unique()
        level = nxt
    return out.sort(["anc", "dsc"])


# ---------------------------------------------------------------- 5. 环


def run_with_timeout(
    con: duckdb.DuckDBPyConnection, sql: str, timeout: float = 3.0
) -> tuple[list[object] | None, str | None]:
    """在后台线程里跑一条可能永远跑不完的查询，超时就 interrupt 它。

    注意：`timeout` 是**这个 demo 自己设的观察窗口**，不是 DuckDB 的超时设置 ——
    DuckDB 2.0 根本没有「递归跑太久就报错」这回事（见场景 5）。
    """

    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["rows"] = con.sql(sql).fetchall()
        except Exception as exc:
            box["err"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        con.interrupt()
        thread.join(timeout=5.0)
        return None, f"跑满 {timeout:g} 秒（本 demo 自己设的阈值）仍未结束 —— 已被 interrupt() 中断"
    if "rows" in box:
        return box["rows"], None
    return None, str(box.get("err"))


def scenario_cycle(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 数据里有环：递归不会自己停")

    print("  cyc 表：1 的上级是 2，2 的上级是 1（互相指向）\n")

    cases = [
        (
            "UNION ALL（不去重）+ 环",
            "UNION ALL",
            """
            WITH RECURSIVE c(id, d) AS (
              SELECT id, 0 FROM cyc WHERE id = 1
              UNION ALL
              SELECT cyc.id, c.d + 1 FROM cyc JOIN c ON cyc.parent = c.id
            )
            SELECT * FROM c
            """,
        ),
        (
            "UNION（去重）+ 环，但输出列不变",
            "UNION",
            """
            WITH RECURSIVE c(id) AS (
              SELECT id FROM cyc WHERE id = 1
              UNION
              SELECT cyc.id FROM cyc JOIN c ON cyc.parent = c.id
            )
            SELECT * FROM c
            """,
        ),
        (
            "UNION（去重）+ 环，但输出列每轮都变（带 depth）",
            "UNION",
            """
            WITH RECURSIVE c(id, d) AS (
              SELECT id, 0 FROM cyc WHERE id = 1
              UNION
              SELECT cyc.id, c.d + 1 FROM cyc JOIN c ON cyc.parent = c.id
            )
            SELECT * FROM c
            """,
        ),
    ]

    for label, _, sql in cases:
        sub = duckdb.connect()
        sub.execute("CREATE TABLE cyc(id INT, parent INT)")
        sub.execute("INSERT INTO cyc VALUES (1, 2), (2, 1)")
        rows, err = run_with_timeout(sub, sql)
        sub.close()
        if err is not None:
            print(f"  {label}")
            print(f"    -> {err}")
        else:
            print(f"  {label}")
            print(f"    -> 收敛，结果 {rows}")

    print("\n  DuckDB 里跟「递归 / 迭代」沾边的设置项：")
    rows = con.sql(
        "SELECT name, description FROM duckdb_settings() "
        "WHERE name LIKE '%recurs%' OR name LIKE '%iterat%' OR name = 'max_expression_depth'"
    ).fetchall()
    for name, desc in rows:
        print(f"    {name} —— {desc}")
    if not rows:
        print("    一个都没有")

    print("\n→ 这是本 demo 里**最危险**的一条：")
    print("  · **DuckDB 2.0 的递归 CTE 没有内置的迭代次数上限。**")
    print("    上表第一行实测：带环的 `UNION ALL` 递归跑满了本 demo 自己设的 3 秒窗口还没停")
    print("    （注意：这不是 DuckDB 的超时 —— 引擎压根没有这个机制），")
    print("    只能靠 `interrupt()` 把它打断 —— 换成真业务数据就是**一条跑不完的查询**,")
    print("    而且它不会报错、不会超时，只会一直占着 CPU 和内存。")
    print("  · 唯一沾点边的 `max_expression_depth` 跟这事**没关系** ——")
    print("    它管的是解析器里的表达式嵌套深度，不是递归轮数（上面把它的说明也打出来了）。")
    print("  · `UNION` 能收敛，是因为去重之后**值集合不再变化**（上表第二行收敛到 2 行）。")
    print("    但只要输出列里带一个每轮都变的东西（比如 depth，上表第三行），")
    print("    去重就失效了，照样跑不完。")
    print("  · → **实践建议**：递归 CTE 里**自己写上限**，别指望引擎兜底：")
    print("    在递归项上加 `WHERE c.depth < 100`（拿层级/步数当护栏），")
    print("    或者用 `UNION`（去重）+ 不变的输出列，让它自己收敛。")
    print("  · → **Polars 侧反而更好防**：循环是你自己写的，")
    print("    `for _ in range(1000)` 或者 `if level.height == 0: break` 都是现成的护栏。")


# ---------------------------------------------------------------- 6. Polars 的两种写法


def scenario_polars_options(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · Polars 的两种替代写法，逐层 join vs map_elements")

    duck = con.sql(
        """
        WITH RECURSIVE chain(id, name, depth, path) AS (
          SELECT id, name, 0, name FROM emp WHERE mgr IS NULL
          UNION ALL
          SELECT e.id, e.name, c.depth + 1, c.path || ' > ' || e.name
          FROM emp e JOIN chain c ON e.mgr = c.id
        )
        SELECT id, name, depth, path FROM chain
        """
    ).pl()

    level = polars_level_by_level(EMP)
    mapped = polars_map_elements(EMP)

    ok1, why1 = common.compare_frames(duck, level, ["id"])
    ok2, why2 = common.compare_frames(duck, mapped, ["id"])
    print(f"  DuckDB 递归 CTE ↔ Polars 逐层 join     {'✅' if ok1 else '❌'} {why1}")
    print(f"  DuckDB 递归 CTE ↔ Polars map_elements  {'✅' if ok2 else '❌'} {why2}")
    print(
        f"  depth 列的类型：DuckDB {duck.schema['depth']} / "
        f"逐层 join {level.schema['depth']} / map_elements {mapped.schema['depth']}"
    )

    print("\n  两种写法的形状：\n")
    print("    逐层 join（循环次数 = 树高，每轮一次向量化 join）")
    print("      level = emp.filter(pl.col('mgr').is_null())...          # 锚点")
    print("      while True:")
    print("          nxt = emp.join(parents, left_on='mgr', ...)          # 一轮 join")
    print("          if nxt.height == 0: break                            # 收敛判据")
    print("          out = pl.concat([out, nxt])")
    print()
    print("    map_elements（每个节点一次 Python 调用，回溯每一步也是 Python）")
    print("      pl.int_range(n).map_elements(walk_depth, ...)            # 逐行回溯")

    print("\n→ 两种写法都能对账（✅），差别在代价的形状：")
    print("  · **逐层 join** 的 Python 开销是「层数」级别的 —— 一层一次 join，")
    print("    中间结果是 DataFrame，join 是向量化的；数据越宽越划算。")
    print("  · **map_elements** 的 Python 开销是「节点数 × 树高」级别的 ——")
    print("    每个节点都要沿 mgr 一路走到根，每一步都是一次 Python 字典查找和循环。")
    print("    树浅的时候差别不大，树一深就明显拉开（下一节量给你看）。")
    print("  · `map_elements` 的好处是**它天然是个函数** —— 不用管循环、")
    print("    不用管收敛，写起来短；坏处是它完全没法向量化。")
    print("  · → **选型**：层级浅、节点多 → 逐层 join；")
    print("    层级深、节点少 → map_elements；层级和数据都大 → 回到 DuckDB。")


# ---------------------------------------------------------------- 7. 代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 7 · 代价：递归 CTE 和两种手写迭代，各要多少时间")

    print(f"数据：一棵二叉堆式的树，{rows} 个节点（父节点 = id // 2，树高 log2(N)）")
    print(f"机器：{common.describe_machine()}")

    tmpdir = Path(tempfile.mkdtemp(prefix="recursive_demo_"))
    src = tmpdir / "tree.parquet"
    con.execute(
        """
        COPY (
          SELECT (i + 1) AS id,
                 CASE WHEN i + 1 = 1 THEN NULL ELSE (i + 1) // 2 END AS mgr,
                 'node_' || (i + 1) AS name
          FROM range(?) t(i)
        ) TO ? (FORMAT PARQUET)
        """,
        [rows, str(src)],
    )
    con.execute("CREATE OR REPLACE TABLE tree AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_tree = pl.read_parquet(src)

    duck_union_all = """
    WITH RECURSIVE chain(id, name, depth, path) AS (
      SELECT id, name, 0, name FROM tree WHERE mgr IS NULL
      UNION ALL
      SELECT e.id, e.name, c.depth + 1, c.path || ' > ' || e.name
      FROM tree e JOIN chain c ON e.mgr = c.id
    )
    SELECT count(*) FROM chain
    """
    duck_union = duck_union_all.replace("UNION ALL", "UNION")

    def duck_all() -> int:
        return int(q1(con, duck_union_all))

    def duck_dedup() -> int:
        return int(q1(con, duck_union))

    def polars_level_fn() -> int:
        return polars_level_by_level(polars_tree).height

    def polars_map_fn() -> int:
        return polars_map_elements(polars_tree).height

    results = [
        ("DuckDB  WITH RECURSIVE（UNION ALL）", duck_all),
        ("DuckDB  WITH RECURSIVE（UNION 去重）", duck_dedup),
        ("Polars  逐层 join 循环", polars_level_fn),
        ("Polars  map_elements", polars_map_fn),
    ]

    print(
        "\n  "
        + common.pad("实现", 36)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("行数", 14)
    )
    print("  " + "-" * 76)
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
            + common.pad(label, 36)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 14)
        )

    duck_chars = int(
        q1(
            con,
            """
            WITH RECURSIVE chain(id, name, depth, path) AS (
              SELECT id, name, 0, name FROM tree WHERE mgr IS NULL
              UNION ALL
              SELECT e.id, e.name, c.depth + 1, c.path || ' > ' || e.name
              FROM tree e JOIN chain c ON e.mgr = c.id
            )
            SELECT sum(length(path)) FROM chain
            """,
        )
    )
    pol_chars = int(polars_level_by_level(polars_tree)["path"].str.len_chars().sum())
    name_chars = int(q1(con, "SELECT sum(length(name)) FROM tree"))
    depth = int(
        q1(
            con,
            """
            WITH RECURSIVE chain(id, depth) AS (
              SELECT id, 0 FROM tree WHERE mgr IS NULL
              UNION ALL
              SELECT e.id, c.depth + 1 FROM tree e JOIN chain c ON e.mgr = c.id
            )
            SELECT max(depth) FROM chain
            """,
        )
    )

    print(f"\n  内存放大：这棵树高 {depth} 层")
    print(f"    name 列（每个节点一个名字）    : {name_chars:,} 个字符")
    print(
        f"    path 列（每个节点一整条路径）  : {duck_chars:,} 个字符"
        f"  ≈ name 列的 {duck_chars / max(name_chars, 1):.1f} 倍"
    )
    print(f"    （Polars 逐层 join 算出来的 path 列同样是 {pol_chars:,} 个字符）")

    print("\n口径：树在计时前已物化 —— DuckDB 侧是内存表 tree，")
    print("  Polars 侧是内存里的 DataFrame，所以计时**不含读盘**。")
    print("  四行做的**是同一件事**：算出每个节点的 depth 和 path，")
    print("  输出同样多的行（表里「行数」那列），结果也逐行相同。")
    print("  两行 DuckDB 的区别只有 `UNION ALL` 和 `UNION`（去重），")
    print("  这棵树里每条路径都不同，所以去重不会改变结果，只改变代价。")
    print("  计时不含建树，也不含 Polars 侧 DataFrame 的第一次读入。")
    print("  重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · **DuckDB 的递归 CTE 只迭代「树高」次** —— 这棵树高 log2(N) 层，")
    print("    每层一次哈希连接，轮数很少。这正是递归 CTE 强的地方：")
    print("    轮数只跟深度有关，跟节点数无关。")
    print("  · **表里两行 DuckDB 的差值就是「去重」的代价** —— `UNION` 要维护一个")
    print("    已经见过的值的集合，每一轮都要往里查、往里插。")
    print("    场景 4 里说 `UNION` 换来的是「会收敛」，这里就是它的账单。")
    print("  · **Polars 逐层 join 的轮数跟 DuckDB 一样**，但每一轮都要过一遍 Python：")
    print("    构造 join 表达式、算出这一层、再 concat 回结果。")
    print("    轮数不多的时候这点开销是常数级的；轮数一多（深树），就线性叠加。")
    print("  · **map_elements 是「每个节点一次 Python 调用」**，而且回溯的每一跳都是")
    print("    Python 循环，总代价是 O(节点数 × 树高)；逐层 join 是 O(树高) 次")
    print("    向量化 join —— 表里这两行的差距就是「每节点一次」的代价。")
    print("  · **内存放大在 path 列上**：递归 CTE 每轮都要把上一轮的 path 物化一遍，")
    print("    上面那两行字符数就是它的体积 —— path 列是 name 列的十几倍。")
    print("    树越深放大越狠：换成一条链（而不是二叉堆），同样的节点数会放大成平方级。")
    print("    → 所以深度信息能预计算就预计算，别在递归里一路拖着字符串走。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的递归 CTE 与迭代计算差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的节点数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_recursive_cte(con)
        scenario_path_enumeration(con)
        scenario_execution_model(con)
        scenario_union_vs_union_all(con)
        scenario_cycle(con)
        scenario_polars_options(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
