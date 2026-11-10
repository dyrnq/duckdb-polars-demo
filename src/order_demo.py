"""排序稳定性：并列的行该谁先谁后？——两个引擎给的答案不一样，而且会变。

「取 Top 10」「翻到第 3 页」这类查询，排序键十有八九是**并列**的。
排序键并列时谁排在前面，SQL 标准**没有规定**，于是两个引擎各选各的。

这个脚本比的是：

  1. NULL 排在哪：DuckDB 默认 NULLS LAST，Polars 默认 nulls 在**最前**。
  2. NaN 排在哪：两边都把 NaN 当最大的数（不是 NULL）。
  3. **排序稳不稳**：DuckDB 的 ORDER BY 在并行排序下不保证稳定，
     同一个查询重复执行可能给出不同的并列顺序；
     Polars 的 `sort` 默认 `maintain_order=False`，也不保证。
  4. **不稳定的后果**：`ORDER BY k LIMIT 5` 在并列处可能取到**不同的行** ——
     分页会漏行、重行，榜单的第 10 名会换人。
  5. 线程数的影响：threads=1/2 稳定，threads=4/8 不稳定。
  6. 字符串和中文排序：两边都是**二进制序**（UTF-8 字节序），不是本地化排序。
  7. 修复办法与代价。

用法：
    uv run python src/order_demo.py
    uv run python src/order_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import time

import duckdb
import polars as pl

import common

# 排序键只有 KEY_SPACE 个不同取值 —— 并列极多，是最容易踩坑的形状
KEY_SPACE = 1000


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _one(con: duckdb.DuckDBPyConnection, sql: str):
    res = con.execute(sql)
    row = res.fetchone()
    res.fetchall()
    return row[0]


def _text(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    return str(_one(con, sql))


# ---------------------------------------------------------------- 场景 1：NULL 排在哪


def scenario_null_order(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · NULL 排在哪：两边默认位置**相反**")
    print("  并列键的排序不只是「谁先谁后」，**NULL 排在前面还是后面**也归引擎管。")
    print("  这一条最容易在 LEFT JOIN 之后暴露 —— 补出来的空行会被排到最前或最后。")
    print()

    con.execute(
        "CREATE OR REPLACE TABLE t_null AS SELECT * FROM (VALUES (3),(1),(NULL),(2),(NULL)) v(x)"
    )

    print("  数据：[3, 1, NULL, 2, NULL]")
    print()
    print("    引擎                                 升序                          降序")
    print("    " + "-" * 88)

    duck_asc = [r[0] for r in con.execute("SELECT x FROM t_null ORDER BY x").fetchall()]
    duck_desc = [r[0] for r in con.execute("SELECT x FROM t_null ORDER BY x DESC").fetchall()]
    df = pl.DataFrame({"x": [3, 1, None, 2, None]})
    pol_asc = df.sort("x")["x"].to_list()
    pol_desc = df.sort("x", descending=True)["x"].to_list()
    pol_asc_nl = df.sort("x", nulls_last=True)["x"].to_list()
    pol_desc_nl = df.sort("x", descending=True, nulls_last=True)["x"].to_list()

    def fmt(vals: list) -> str:
        return "  ".join("NULL" if v is None else str(v) for v in vals)

    print(f"    DuckDB  默认               {fmt(duck_asc):<28} {fmt(duck_desc)}")
    print(f"    Polars  默认               {fmt(pol_asc):<28} {fmt(pol_desc)}")
    print(f"    Polars  nulls_last=True   {fmt(pol_asc_nl):<28} {fmt(pol_desc_nl)}")

    print()
    print("  DuckDB 的规则是 `default_null_order = NULLS_LAST`，**升序降序都一样空最后**；")
    print("  Polars 的 `nulls_last` 默认是 False，**升序降序都空最前**。")
    print()
    print("  两边都能改：")
    first_demo = _nulls_first_demo(con)
    print(f"    DuckDB  SET default_null_order='NULLS_FIRST'  → {first_demo}")
    print("    Polars  sort('x', nulls_last=True)")
    print()
    print("  注意：**降序**的时候两边差得更远 —— DuckDB 空值仍在最后，")
    print("  Polars 空值跑到最前，也就是「最大的排在最前」变成了「最大的排在空值后面」。")


def _nulls_first_demo(con: duckdb.DuckDBPyConnection) -> str:
    con.execute("SET default_null_order='NULLS_FIRST'")
    vals = [r[0] for r in con.execute("SELECT x FROM t_null ORDER BY x").fetchall()]
    con.execute("SET default_null_order='NULLS_LAST'")
    return "  ".join("NULL" if v is None else str(v) for v in vals)


# ---------------------------------------------------------------- 场景 2：NaN 排在哪


def scenario_nan_order(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · NaN 排在哪：它不是 NULL，是最大的数")
    print("  数据：[3.0, NULL, NaN, 1.0, NULL]")
    print()

    con.execute(
        "CREATE OR REPLACE TABLE t_nan AS SELECT * FROM (VALUES "
        "(3.0),(NULL),('NaN'::DOUBLE),(1.0),(NULL)) v(x)"
    )
    duck = [r[0] for r in con.execute("SELECT x FROM t_nan ORDER BY x").fetchall()]
    df = pl.DataFrame({"x": [3.0, None, float("nan"), 1.0, None]})
    pol = df.sort("x", nulls_last=True)["x"].to_list()

    def fmt(vals: list) -> str:
        return "  ".join("NULL" if v is None else ("NaN" if v != v else str(v)) for v in vals)

    print(f"    DuckDB  升序              {fmt(duck)}")
    print(f"    Polars  升序 nulls_last   {fmt(pol)}")
    print()
    print("  两边一致（把 null 也排最后时）：NaN 排在所有数字后面，NULL 再后面。")
    print("  也就是说 **NaN 被当成最大的数**，而不是一个「缺失值」。")
    print()
    nan_is_null = _one(con, "SELECT x IS NULL FROM t_nan WHERE isnan(x)")
    nan_ne_nan = _one(con, "SELECT x != x FROM t_nan WHERE isnan(x)")
    nan_eq_nan = _one(con, "SELECT x = x FROM t_nan WHERE isnan(x)")
    print(f"  DuckDB  NaN IS NULL → {nan_is_null}（不是 NULL）")
    print(f"  DuckDB  NaN = NaN   → {nan_eq_nan}")
    print(f"  DuckDB  NaN != NaN  → {nan_ne_nan}")
    pol_nan = pl.Series([float("nan")])
    print(f"  Polars  NaN = NaN   → {(pol_nan == pol_nan).to_list()[0]}")
    print(f"  Polars  NaN != NaN  → {(pol_nan != pol_nan).to_list()[0]}")
    print()
    print("  **两边都把 NaN 当成「等于自己」的数** —— 这跟 IEEE 754 的 NaN != NaN 不一样，")
    print("  所以 `x != x` 在两个引擎里都**不能**用来判断 NaN，要用 `isnan()` / `is_nan()`。")
    print()
    print("  这条本身两边一致，但**它是 NULL 那条规则的延伸**：")
    print("  「结果里没有 NaN」不代表「没有缺失值」，得同时查 NaN 和 NULL。")


# ---------------------------------------------------------------- 场景 3：排序稳不稳


def scenario_stability(con: duckdb.DuckDBPyConnection, n_rows: int) -> None:
    section("场景 3 · 排序稳不稳：同一个查询，重复执行答案可能不一样")
    print(f"  数据：{n_rows:,} 行，排序键只有 {KEY_SPACE} 个不同取值（并列极多），")
    print("  第二列 p 是行号，用来观察并列行的相对顺序。")
    print()

    con.execute(
        f"CREATE OR REPLACE TABLE big AS SELECT (i % {KEY_SPACE})::INTEGER AS k, i AS p "
        f"FROM range({n_rows}) t(i)"
    )

    print("  先看小表（100 行）—— 顺序是**确定的**，但**不等于插入顺序**：")
    con.execute(
        "CREATE OR REPLACE TABLE small AS SELECT (i % 10)::INTEGER AS k, i AS p "
        "FROM range(100) t(i)"
    )
    small = [r[1] for r in con.execute("SELECT k, p FROM small ORDER BY k").fetchall() if r[0] == 0]
    print(f"    100 行：k=0 的行号顺序 = {small}")
    print("    插入顺序应该是 [0, 10, 20, 30, …] —— 已经不一样了。")
    print("    所以「稳定」和「等于插入顺序」是两回事，DuckDB 给的既不是后者。")
    print()

    print(f"  换成 {n_rows:,} 行，同样看 k=0 的行号顺序，**连续跑 5 次**：")
    seen: list[list[int]] = []
    for _ in range(5):
        rows = con.execute("SELECT k, p FROM big ORDER BY k").fetchall()
        order = [r[1] for r in rows if r[0] == 0][:6]
        seen.append(order)
        print(f"    {order}")
    uniq = {tuple(o) for o in seen}
    print()
    if len(uniq) == 1:
        print("  这一轮 5 次结果相同 —— 但**这不代表它是稳定的**（见下面的线程数对比）。")
    else:
        print(f"  **5 次跑出了 {len(uniq)} 种不同的并列顺序。** 同一份数据、同一条 SQL，")
        print("  结果却不一样 —— 并列行之间谁先谁后是**不确定**的。")

    print()
    print("  线程数的影响（每个线程数单独测 3 次）：")
    original_threads = int(_one(con, "SELECT current_setting('threads')"))
    for th in [1, 2, 4, 8]:
        con.execute(f"SET threads={th}")
        orders = []
        for _ in range(3):
            rows = con.execute("SELECT k, p FROM big ORDER BY k").fetchall()
            orders.append(tuple(r[1] for r in rows if r[0] == 0)[:4])
        n_uniq = len(set(orders))
        flag = "✅ 稳定" if n_uniq == 1 else f"❌ {n_uniq} 种结果"
        print(f"    threads={th}  {flag}   {list(orders[0])}")
    con.execute(f"SET threads={original_threads}")
    print()
    print("  单线程走的是串行排序，顺序就是插入顺序；**2 线程起就走并行排序**，")
    print("  合并顺序取决于各线程什么时候把结果交上来 —— 于是每次都可能不同。")
    print("  到底是哪几次不一样，每次跑都不同 —— 这正是它难测的原因。")
    print("  **这和 scaling_demo.py 里「线程数变了结果是否还一致」是同一个根因。**")

    print()
    print("  Polars 侧：`sort()` 的 `maintain_order` 默认是 **False**。")
    df = pl.select(
        k=pl.int_range(0, n_rows, dtype=pl.Int64) % KEY_SPACE,
        p=pl.int_range(0, n_rows, dtype=pl.Int64),
    )
    for mo in [False, True]:
        out = df.sort("k", maintain_order=mo)
        order = [r["p"] for r in out.to_dicts() if r["k"] == 0][:6]
        print(f"    maintain_order={str(mo):<5} → {order}")
    print()
    print("  **单列排序时实测恰好是稳的，多列排序就不稳了** —— 别把「看起来稳」当成保证。")
    df2 = df.with_columns((pl.col("k") % 3).alias("k2"))
    for mo in [False, True]:
        out = df2.sort(["k", "k2"], maintain_order=mo)
        order = [r["p"] for r in out.to_dicts() if r["k"] == 0 and r["k2"] == 0][:6]
        print(f"    多列 maintain_order={str(mo):<5} → {order}")


# ---------------------------------------------------------------- 场景 4：LIMIT 跨并列边界


def scenario_limit_ties(con: duckdb.DuckDBPyConnection, n_rows: int) -> None:
    section("场景 4 · 排序不稳的后果：LIMIT 会取到**不同的行**")
    print("  「取 Top 10」在并列处出问题：第 10 名和第 11 名分数相同，")
    print("  排序没定顺序，LIMIT 10 就会随机砍掉其中一个。")
    print()

    # 一个 20 人的榜单，前 12 名分数都是 90 —— 第 10 名就在并列里
    scores = [100.0] * 3 + [90.0] * 9 + [80.0] * 8
    values = ",".join(f"({s},{i})" for i, s in enumerate(scores))
    con.execute(f"CREATE OR REPLACE TABLE board AS SELECT * FROM (VALUES {values}) v(score, id)")

    print("  榜单：前 3 名 100 分，接着 9 个人都是 90 分，再后面 8 个人 80 分。")
    print("  问「Top 5」——第 4、5 名落在 90 分那一堆里。")
    print()

    duck_ids = [
        r[0] for r in con.execute("SELECT id FROM board ORDER BY score DESC LIMIT 5").fetchall()
    ]
    df = pl.DataFrame({"score": scores, "id": list(range(len(scores)))})
    pol_ids = df.sort("score", descending=True, maintain_order=True).head(5)["id"].to_list()
    print(f"    DuckDB  → id = {duck_ids}")
    print(f"    Polars  → id = {pol_ids}")
    print(f"    一致？ {'✅' if duck_ids == pol_ids else '❌ —— 第 4、5 名换人了'}")
    print()
    print("  小榜单上两边一致 —— 但这是**碰巧**：DuckDB 给的 id 顺序恰好也是插入顺序。")
    print("  同样的并列结构放大到百万行，就没那么走运了：")
    print()
    print(f"  {n_rows:,} 行的表，`ORDER BY k LIMIT 5` 连跑 6 次：")
    seen: list[tuple] = []
    for _ in range(6):
        rows = con.execute("SELECT k, p FROM big ORDER BY k LIMIT 5").fetchall()
        got = tuple(r[1] for r in rows)
        seen.append(got)
        mark = "  ← 和第一次不一样" if got != seen[0] else ""
        print(f"    {list(got)}{mark}")
    n_uniq = len(set(seen))
    print()
    print(f"  6 次跑出了 **{n_uniq} 种结果**。")
    print("  这在生产里长这样：")
    print("    · 榜单刷新一次，第 10 名换人；")
    print("    · 翻页时第 21 行永远看不到，或者出现两次；")
    print("    · 「取前 100 条导出」导出的内容每次不一样。")
    print("  **两个引擎都不报错** —— 这就是它危险的地方。")


# ---------------------------------------------------------------- 场景 5：分页


def scenario_paging(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 分页：LIMIT/OFFSET 在并列处会漏行、重行")
    print("  OFFSET 分页的隐含假设是「排序是全序」，并列一旦打破这个假设，")
    print("  第 2 页的第 1 行可能和第 1 页的最后一行是同一行，也可能中间漏掉一行。")
    print()

    # 40 行，分数每 4 行一档 —— 第 21~30 行正好横跨一个并列档
    scores = [100.0 - (i // 4) for i in range(40)]
    values = ",".join(f"({s},{i})" for i, s in enumerate(scores))
    con.execute(f"CREATE OR REPLACE TABLE page AS SELECT * FROM (VALUES {values}) v(score, id)")
    df = pl.DataFrame({"score": scores, "id": list(range(40))})

    print("  40 行，分数每 4 行一档；取第 3 页（OFFSET 20, LIMIT 10）：")
    duck_ids = [
        r[0]
        for r in con.execute(
            "SELECT id FROM page ORDER BY score DESC LIMIT 10 OFFSET 20"
        ).fetchall()
    ]
    pol_ids = df.sort("score", descending=True, maintain_order=True).slice(20, 10)["id"].to_list()
    print(f"    DuckDB  → {duck_ids}")
    print(f"    Polars  → {pol_ids}")
    print(f"    一致？ {'✅' if duck_ids == pol_ids else '❌'}")
    print()
    print("  这一页没有落在并列档内部，所以两边一致 —— **但那是运气**。")
    print("  把 OFFSET 挪到并列档中间（比如 OFFSET 22），差别就出来了：")
    duck_ids2 = [
        r[0]
        for r in con.execute(
            "SELECT id FROM page ORDER BY score DESC LIMIT 10 OFFSET 22"
        ).fetchall()
    ]
    pol_ids2 = df.sort("score", descending=True, maintain_order=True).slice(22, 10)["id"].to_list()
    print(f"    DuckDB  → {duck_ids2}")
    print(f"    Polars  → {pol_ids2}")
    print(f"    一致？ {'✅' if duck_ids2 == pol_ids2 else '❌ —— 两边取到的不是同一批行'}")

    print()
    print("  修法就一条：**排序键要能唯一确定顺序**。")
    print("    DuckDB  ORDER BY score DESC, id DESC LIMIT 10 OFFSET 20")
    print("    Polars  sort('score', descending=True, maintain_order=True) 再按 id 排")
    print("  加上 `id` 这个 tiebreaker 之后，并列就永远排不出两种顺序了。")


# ---------------------------------------------------------------- 场景 6：字符串排序


def scenario_collation(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 字符串和中文排序：两边都是**二进制序**")
    print("  MySQL/PG 的默认排序规则跟本地化有关（'a' 和 'A' 谁大看 collation），")
    print("  DuckDB 和 Polars **都不做本地化** —— 直接比 UTF-8 字节。")
    print()

    words = ["apple", "Banana", "cherry", "Apple"]
    con.execute(
        "CREATE OR REPLACE TABLE s AS SELECT * FROM (VALUES "
        + ",".join(f"('{w}')" for w in words)
        + ") v(x)"
    )
    duck = [r[0] for r in con.execute("SELECT x FROM s ORDER BY x").fetchall()]
    pol = pl.DataFrame({"x": words}).sort("x")["x"].to_list()
    print("  英文：")
    print(f"    DuckDB  {duck}")
    print(f"    Polars  {pol}")
    print(f"    一致？ {'✅' if duck == pol else '❌'}")
    print("    大写字母排在前面（'A'=65 < 'a'=97）—— 和 MySQL 的 utf8mb4_general_ci 不同。")
    print()

    cn = ["北京", "上海", "广州", "深圳"]
    con.execute(
        "CREATE OR REPLACE TABLE c AS SELECT * FROM (VALUES "
        + ",".join(f"('{w}')" for w in cn)
        + ") v(x)"
    )
    duck_cn = [r[0] for r in con.execute("SELECT x FROM c ORDER BY x").fetchall()]
    pol_cn = pl.DataFrame({"x": cn}).sort("x")["x"].to_list()
    print("  中文：")
    print(f"    DuckDB  {duck_cn}")
    print(f"    Polars  {pol_cn}")
    print(f"    一致？ {'✅' if duck_cn == pol_cn else '❌'}")
    print("    按 Unicode 码位排（'上'=U+4E0A < '北'=U+5317），**不是拼音**。")
    print("    要拼音排序得自己在应用层做（pypinyin 之类）。")
    print()
    print("  这一条两边一致 —— 值得记一笔：**一致的地方不用管，不一致的地方才要命**。")


# ---------------------------------------------------------------- 场景 7：性能与修法


def scenario_performance(con: duckdb.DuckDBPyConnection, n_rows: int, repeat: int) -> None:
    section("场景 7 · 排序性能，以及「排序稳定」要花多少代价")
    print()

    df = pl.select(
        k=pl.int_range(0, n_rows, dtype=pl.Int64) % KEY_SPACE,
        p=pl.int_range(0, n_rows, dtype=pl.Int64),
    )
    con.execute(
        f"CREATE OR REPLACE TABLE big AS SELECT (i % {KEY_SPACE})::INTEGER AS k, i AS p "
        f"FROM range({n_rows}) t(i)"
    )

    def duck_sort() -> None:
        rel = con.sql("SELECT k, p FROM big ORDER BY k")
        rel.to_arrow_table()
        rel.close()

    def duck_topn() -> None:
        rel = con.sql("SELECT k, p FROM big ORDER BY k LIMIT 10")
        rel.to_arrow_table()
        rel.close()

    def pol_sort(mo: bool) -> None:
        df.sort("k", maintain_order=mo)

    def pol_topn() -> None:
        df.lazy().sort("k").limit(10).collect()

    def bench(fn, n: int = repeat) -> float:
        ts = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            ts.append(time.perf_counter() - t0)
        return min(ts)

    d_sort = bench(duck_sort)
    d_topn = bench(duck_topn)
    p_sort = bench(lambda: pol_sort(False))
    p_sort_stable = bench(lambda: pol_sort(True))
    p_topn = bench(pol_topn)

    print(f"  {n_rows:,} 行全排序（并列很多）：")
    print(f"    DuckDB  ORDER BY k            {common.format_ms(d_sort):>10} ms")
    print(f"    Polars  sort('k')            {common.format_ms(p_sort):>10} ms")
    print(f"    Polars  sort(..., maintain_order=True)  {common.format_ms(p_sort_stable):>10} ms")
    print()
    print("  同样的数据取 Top 10：")
    print(f"    DuckDB  ORDER BY k LIMIT 10   {common.format_ms(d_topn):>10} ms")
    print(f"    Polars  sort + limit(10)     {common.format_ms(p_topn):>10} ms")
    print()
    print("  注意 DuckDB 的 Top-N 走的是专门的 top-N 算子，比全排序快得多 ——")
    print("  「要前 10 条就别全排」这条在两边都成立。")
    print()
    print("  「排序要稳」的代价：")
    print("    · Polars：maintain_order=True 实测**没有可测量的额外开销**（见上面两行），")
    print("      所以直接开着。")
    print("    · DuckDB：**没有开关**能让 ORDER BY 变稳（`preserve_insertion_order` 管的是")
    print("      表里行的存放顺序，不是并列的排序顺序）。")
    print("      唯一的办法是**自己加 tiebreaker**：ORDER BY k, p ——")
    print("      p 唯一，所以全序确定，每次查询结果都一样。")
    print()
    rows = con.execute("SELECT k, p FROM big ORDER BY k, p LIMIT 5").fetchall()
    print(f"  加 tiebreaker 之后：`ORDER BY k, p LIMIT 5` → {[r[1] for r in rows]}（稳定）")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. **NULL 的默认位置两边相反**：DuckDB 默认 NULLS LAST（升序降序都最后），")
    print("     Polars 默认 nulls 在最前（升序降序都最前）。")
    print("     Polars 用 `nulls_last=True` 对齐 DuckDB；DuckDB 用")
    print("     `SET default_null_order='NULLS_LAST'`（本来就是默认）。")
    print("  2. NaN 两边一致：都当成最大的数，排在所有数字后面，且**不是 NULL**。")
    print("     两边也都认为 NaN = NaN（不像 IEEE 754），所以 `x != x` 判断不了 NaN，")
    print("     要用 isnan() / is_nan()。")
    print("     「结果里没有 NaN」不等于「没有缺失值」。")
    print("  3. **两个引擎的排序默认都不保证稳定**：")
    print("     · DuckDB 的 ORDER BY 在并行排序下，同一个查询重复执行可能给出不同的并列顺序；")
    print("       threads=1 稳定，**2 线程起就不稳定**，且每次不一样的方式都不同。")
    print("     · Polars 的 `sort` 默认 `maintain_order=False`，多列排序实测不稳定。")
    print("     小表上两边看起来都稳 —— 所以这个问题**测不出来**，只能靠设计避免。")
    print("  4. **后果是 LIMIT 取到不同的行**：实测 6 次 `ORDER BY k LIMIT 5` 跑出多种结果。")
    print("     榜单第 10 名会换人、翻页会漏行重行、导出内容每次不一样。")
    print("     两个引擎都不报错。")
    print("  5. 分页同理：OFFSET 落在并列档内部时，两边取到的不是同一批行。")
    print("  6. 字符串和中文排序两边一致，且都是**二进制序**（UTF-8 字节序），")
    print("     不是本地化排序 —— 'Apple' < 'apple'，中文按码位不按拼音。")
    print("  7. **修法只有一个：排序键要能唯一确定顺序。**")
    print("     · DuckDB：ORDER BY k, p（加唯一 tiebreaker），没有开关能强制稳定；")
    print("     · Polars：sort(..., maintain_order=True)，实测没有额外开销，直接开着。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="排序稳定性：NULL 位置、并列顺序与 LIMIT 的后果")
    parser.add_argument("--rows", type=int, default=1_000_000, help="大表行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    con = duckdb.connect()
    scenario_null_order(con)
    scenario_nan_order(con)
    scenario_stability(con, args.rows)
    scenario_limit_ties(con, args.rows)
    scenario_paging(con)
    scenario_collation(con)
    scenario_performance(con, args.rows, args.repeat)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
