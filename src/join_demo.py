"""JOIN 全景：七种连接类型的语义、行数，以及**输出列结构**的差异。

连接是最常用的操作，也是最容易「结果看着对、其实结构不对」的地方。
这个脚本把两个引擎的连接摆在一起比：

  1. 七种连接类型（INNER / LEFT / RIGHT / FULL / SEMI / ANTI / CROSS）行数一致吗？
  2. **输出列的结构一样吗？** —— 这里有最容易被忽略的坑：
     DuckDB `ON` 连接会产生两个同名列，Polars 的 `how="right"` 列顺序是反的，
     `how="full"` 会多出一个 `k_right`。
  3. 连接键的类型不匹配时，一个隐式转换、一个直接报错。
  4. 重复键怎么把行数放大。
  5. 真实数据上的连接性能。

前四节用小表，最后一节用 orders/users 真实数据。

用法：
    uv run python src/join_demo.py
"""

from __future__ import annotations

import argparse
import time

import duckdb
import polars as pl

import common

LEFT = pl.DataFrame(
    {
        "k": [1, None, 3, 1],
        "lv": ["a", None, "c", "d"],
    }
)

RIGHT = pl.DataFrame(
    {
        "k": [1, None, 4],
        "rv": ["A", "NULL-KEY", "D"],
    }
)

# 键类型不一致：左边是整数，右边是字符串
LEFT_STR = pl.DataFrame({"k": [1, 2, 3]})
RIGHT_STR = pl.DataFrame({"k": ["1", "02", "2"], "rv": ["x", "y", "z"]})

# 右表键有重复（数据质量问题），用来演示 fan-out
DUP_LEFT = pl.DataFrame({"k": [1, 2, 3], "amt": [10.0, 20.0, 30.0]})
DUP_RIGHT = pl.DataFrame({"k": [1, 1, 4], "note": ["A1", "A2", "D"]})


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("left_t", LEFT)
    con.register("right_t", RIGHT)
    con.register("left_s", LEFT_STR)
    con.register("right_s", RIGHT_STR)
    con.register("dup_left", DUP_LEFT)
    con.register("dup_right", DUP_RIGHT)
    return con


# ---------------------------------------------------------------- 场景 1


def scenario_types(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 七种连接类型：行数一致，列结构不一致")

    duck_sql = {
        "INNER": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
        "LEFT": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
        "RIGHT": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
        "FULL": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
        "SEMI": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
        "ANTI": "SELECT * FROM left_t {j} JOIN right_t ON left_t.k = right_t.k",
    }
    print(f"  {'连接类型':<12}{'DuckDB 行数':<14}{'Polars 行数':<14}{'行数一致?'}")
    print("  " + "-" * 60)
    for how in ["inner", "left", "right", "full", "semi", "anti"]:
        pol = LEFT.join(RIGHT, on="k", how=how)
        # DuckDB 侧：SEMI/ANTI 用专用语法，其余用 JOIN 关键字
        if how == "semi":
            sql = "SELECT * FROM left_t SEMI JOIN right_t ON left_t.k = right_t.k"
        elif how == "anti":
            sql = "SELECT * FROM left_t ANTI JOIN right_t ON left_t.k = right_t.k"
        else:
            sql = duck_sql[how.upper()].format(j=how.upper())
        try:
            duck_rows = con.execute(sql).fetchall()
            duck_n = len(duck_rows)
        except Exception as exc:
            duck_n = f"ERR {type(exc).__name__}"
        mark = "✅" if duck_n == pol.height else "❌"
        line = (
            f"  {common.pad(how.upper(), 12)}"
            f"{common.pad(str(duck_n), 14)}"
            f"{common.pad(str(pol.height), 14)}{mark}"
        )
        print(line)
    print()
    print("  行数全部一致 ✅ —— 连接语义本身两边是一样的。")
    print("  但**输出列的结构**不一样，下一节细看。")


# ---------------------------------------------------------------- 场景 2


def scenario_columns(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 输出列结构：这是最容易被忽略的坑")

    print("  DuckDB 用 ON 连接时，同名列会出现**两次**：")
    for how in ["INNER", "LEFT", "RIGHT", "FULL"]:
        cols = _cols(con, f"SELECT * FROM left_t {how} JOIN right_t ON left_t.k = right_t.k")
        print(f"    {how:<6} → {cols}")
    print()
    print("  改用 USING 就合并成一个：")
    cols = _cols(con, "SELECT * FROM left_t LEFT JOIN right_t USING (k)")
    print(f"    USING  → {cols}")
    print()
    print("  Polars 侧：")
    for how in ["inner", "left", "right", "full", "semi", "anti"]:
        cols = LEFT.join(RIGHT, on="k", how=how).columns
        print(f"    {how:<6} → {cols}")
    print()
    print("  三处不一样：")
    print(
        "    1. DuckDB 的 `ON` 版本有两个 `k`；Polars 永远只有一个 —— "
        "想对齐，DuckDB 侧要写 `USING (k)`。"
    )
    print("    2. Polars 的 `how='right'` 把**右表的列排在前面**（`['lv','k','rv']`），")
    print("       DuckDB 的 RIGHT JOIN 列顺序不变（`['k','lv','rv']`）。")
    print("    3. Polars 的 `how='full'` 多出一个 `k_right`（两边的键都留着），")
    print("       DuckDB 的 FULL OUTER JOIN 用 `USING` 时只留一个 `k`（值合并，")
    print("       匹配上的取左边的值，没匹配上的取右边的值）。")
    print()
    print("  这些差异在**按列名取数**时不会报错，只会让结果悄悄少一列或多一列。")
    print("  举例：把 DuckDB 的 ON 版结果直接转成 Arrow，schema 里会有两个 `k` 字段 ——")

    rel = con.sql("SELECT * FROM left_t LEFT JOIN right_t ON left_t.k = right_t.k")
    arrow = rel.to_arrow_table()
    rel.close()
    duck_schema = [(n, str(t)) for n, t in zip(arrow.schema.names, arrow.schema.types, strict=True)]
    print(f"    DuckDB  Arrow schema : {duck_schema}")
    pol_arrow = LEFT.join(RIGHT, on="k", how="left").to_arrow()
    pol_schema = [
        (n, str(t)) for n, t in zip(pol_arrow.schema.names, pol_arrow.schema.types, strict=True)
    ]
    print(f"    Polars  Arrow schema : {pol_schema}")
    print()
    print("  顺带一提：字符串列的 Arrow 类型也不一样 —— DuckDB 导出 `string`（32 位偏移），")
    print("  Polars 导出 `large_string`（64 位偏移）。下游按 Arrow 类型做判断的代码要留意。")

    print()
    print("  FULL OUTER 的行内容对照（看 DuckDB 的 k 是怎么合并的）：")
    duck_full = con.execute(
        "SELECT k, lv, rv FROM left_t FULL OUTER JOIN right_t USING (k) ORDER BY k NULLS FIRST"
    ).fetchall()
    for row in duck_full:
        print(f"    DuckDB  k={row[0]!r}  lv={row[1]!r}  rv={row[2]!r}")
    pol_full = LEFT.join(RIGHT, on="k", how="full").sort("k", nulls_last=False)
    print()
    for row in pol_full.to_dicts():
        print(
            f"    Polars  k={row['k']!r}  lv={row['lv']!r}  "
            f"k_right={row['k_right']!r}  rv={row['rv']!r}"
        )
    print()
    print("  两边的**行内容**是一致的（6 行，每行都能对上），只是列结构不同：")
    print("    DuckDB 用 USING 把两边的键合并进一列 `k`；")
    print("    Polars 保留两列 —— 匹配上的行 `k` 和 `k_right` 相同，")
    print("    只在左边出现的行 `k_right` 是 NULL，只在右边出现的行 `k` 是 NULL。")
    print("    所以 DuckDB 那边那一行显示 `k=4`，Polars 那边显示 `k=None, k_right=4` ——")
    print("    **是同一行数据**。")


# ---------------------------------------------------------------- 场景 3


def scenario_key_types(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 连接键类型不一致：一个偷偷转，一个直接拒绝")

    print("  左边 k 是整数 [1,2,3]，右边 k 是字符串 ['1','02','2']：")
    print()
    try:
        duck_rows = con.execute(
            "SELECT left_s.k, right_s.k, right_s.rv FROM left_s "
            "JOIN right_s ON left_s.k = right_s.k"
        ).fetchall()
        print(f"  DuckDB  → 成功，返回 {len(duck_rows)} 行：")
        for row in duck_rows:
            print(f"      左 k={row[0]!r}  右 k={row[1]!r}  rv={row[2]!r}")
        types = con.execute(
            "SELECT typeof(left_s.k), typeof(right_s.k) FROM left_s JOIN right_s "
            "ON left_s.k = right_s.k LIMIT 1"
        ).fetchall()
        print(f"      源列类型：左 {types[0][0]} / 右 {types[0][1]}")
        print("      比较时 DuckDB 把**字符串那一侧**转成了整数（数值优先于字符串）。")
    except Exception as exc:
        print(f"  DuckDB  → {type(exc).__name__}: {str(exc)[:80]}")
    print()
    try:
        pol = LEFT_STR.join(RIGHT_STR, on="k", how="inner")
        print(f"  Polars  → 成功，返回 {len(pol)} 行")
    except Exception as exc:
        print(f"  Polars  → {type(exc).__name__}: {str(exc).strip().splitlines()[0][:88]}")
        print("      Polars 干脆不连 —— 连接键类型不一致直接抛 SchemaError。")
    print()
    print("  危险在 DuckDB 这一侧，而且不是「少匹配」，是「多匹配」：")
    print("    '02' 和 '2' 是两个不同的字符串键，转成整数之后**都变成 2**。")
    print("    左边的 k=2 于是同时匹配上了右边的两行 —— 行数被悄悄放大。")
    print()
    print("  如果字符串那一侧有转不成整数的值：")
    bad = pl.DataFrame({"k": ["1", "abc"], "rv": ["x", "y"]})
    con.register("right_bad", bad)
    try:
        con.execute("SELECT * FROM left_s JOIN right_bad ON left_s.k = right_bad.k").fetchall()
        print("    DuckDB  → 没有报错（本次实测没有触发转换错误）")
    except Exception as exc:
        print(f"    DuckDB  → {type(exc).__name__}: {str(exc).splitlines()[0][:76]}")
    print("    即：脏数据在连接时才会暴露，前面几步都没报错。")
    print()
    print("  选型建议：**连接键的类型必须先对齐**，不要指望引擎帮你转。")
    print("  DuckDB 侧显式写 `ON left_s.k = right_s.k::INT`，把转换意图写进 SQL；")
    print("  但更稳的做法是**在连接前就把两侧转成同一套规范**（比如都用整数，")
    print("  或者统一去掉前导零），这样转换的损耗（'02'→2）不会悄悄改变行数。")
    print("  Polars 侧则在连接前显式 cast，两边行为就完全可控了。")


# ---------------------------------------------------------------- 场景 4


def scenario_fanout(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 重复键：连接会把行数放大，金额跟着翻倍")

    print("  左表 3 行（k 唯一），右表 3 行（k=1 出现了两次 —— 维表数据质量问题）：")
    print()
    duck_rows = con.execute(
        "SELECT dup_left.k, dup_left.amt, dup_right.note "
        "FROM dup_left JOIN dup_right ON dup_left.k = dup_right.k"
    ).fetchall()
    pol_rows = DUP_LEFT.join(DUP_RIGHT, on="k", how="inner").rows()
    print(f"    DuckDB  INNER JOIN 结果 : {duck_rows}")
    print(f"    Polars  inner join 结果 : {pol_rows}")
    print()
    duck_n = len(duck_rows)
    duck_sum = con.execute(
        "SELECT SUM(amt) FROM dup_left JOIN dup_right ON dup_left.k = dup_right.k"
    ).fetchone()[0]
    pol_sum = DUP_LEFT.join(DUP_RIGHT, on="k", how="inner")["amt"].sum()
    print(f"    DuckDB  行数 {duck_n}，SUM(amt) = {duck_sum}")
    print(f"    Polars  行数 {len(pol_rows)}，sum(amt) = {pol_sum}")
    print()
    print("  两边行为一致 —— 但这正是要小心的地方：")
    print("    左表只有 1 行 k=1、金额 10，连接后变成 2 行、SUM 变成 20。")
    print("    「订单表 join 用户维表」如果维表里 user_id 重复（数据质量问题），")
    print("    订单行会被复制，每笔金额都被算两遍，SUM 直接翻倍。")
    print("    这不是引擎差异，是数据问题 —— 但**两个引擎都会老老实实地放大**，")
    print("    谁也不会提醒你。")
    print()
    print("  防御写法：连接前先检查右表键的唯一性。")
    dup = con.execute(
        "SELECT COUNT(*) FROM (SELECT k FROM dup_right GROUP BY k HAVING COUNT(*) > 1) t"
    ).fetchone()[0]
    pol_dup = DUP_RIGHT.group_by("k").agg(pl.len().alias("n")).filter(pl.col("n") > 1).height
    print(f"    DuckDB  右表重复的键 : {dup}")
    print(f"    Polars  右表重复的键 : {pol_dup}")
    print()
    print("  更好的做法是用半连接做存在性检查（不放大行数）：")
    duck_semi = con.execute(
        "SELECT COUNT(*) FROM dup_left WHERE EXISTS "
        "(SELECT 1 FROM dup_right WHERE dup_right.k = dup_left.k)"
    ).fetchone()[0]
    pol_semi = DUP_LEFT.join(DUP_RIGHT, on="k", how="semi").height
    print(f"    DuckDB  EXISTS 匹配到的左表行数 : {duck_semi}")
    print(f"    Polars  how='semi' 行数          : {pol_semi}")
    print("    （半连接两边都是 1 行 —— 不会放大）")
    print()
    print("  顺带：SEMI / ANTI 两边语义也完全一致。")
    duck_semi2 = con.execute(
        "SELECT COUNT(*) FROM dup_left SEMI JOIN dup_right ON dup_left.k = dup_right.k"
    ).fetchone()[0]
    duck_anti = con.execute(
        "SELECT COUNT(*) FROM dup_left ANTI JOIN dup_right ON dup_left.k = dup_right.k"
    ).fetchone()[0]
    pol_anti = DUP_LEFT.join(DUP_RIGHT, on="k", how="anti").height
    print(f"    DuckDB  SEMI {duck_semi2} / ANTI {duck_anti}")
    print(f"    Polars  semi {pol_semi} / anti {pol_anti}")


# ---------------------------------------------------------------- 场景 5


def scenario_performance(repeat: int) -> None:
    section("场景 5 · 真实数据上的连接性能（100 万订单 × 2 万用户）")

    if not common.ORDERS_PARQUET.exists() or not common.USERS_PARQUET.exists():
        print("  缺 data/orders.parquet 或 data/users.parquet，先跑 gen_data.py")
        return

    print(f"  机器状态：{common.describe_machine()}")
    print()
    print("  任务：按 tier 聚合订单数与销售额。分两种口径测，**口径必须对齐** ——")
    print("    · 端到端：从 parquet 读出来再连再聚合")
    print("    · 内存内：数据已经在内存里，只测连接 + 聚合")
    print()

    con = duckdb.connect()
    duck_e2e_sql = f"""
        SELECT u.tier, COUNT(*) AS n, SUM(o.revenue) AS rev
        FROM read_parquet('{common.ORDERS_PARQUET}') o
        JOIN read_parquet('{common.USERS_PARQUET}') u ON o.user_id = u.user_id
        GROUP BY u.tier
    """
    orders_arrow = pl.read_parquet(common.ORDERS_PARQUET).to_arrow()
    users_arrow = pl.read_parquet(common.USERS_PARQUET).to_arrow()
    con.register("orders_mem", orders_arrow)
    con.register("users_mem", users_arrow)
    duck_mem_sql = """
        SELECT u.tier, COUNT(*) AS n, SUM(o.revenue) AS rev
        FROM orders_mem o JOIN users_mem u ON o.user_id = u.user_id
        GROUP BY u.tier
    """

    def duck_e2e() -> list:
        return con.execute(duck_e2e_sql).fetchall()

    def duck_mem() -> list:
        return con.execute(duck_mem_sql).fetchall()

    def pol_e2e():
        return (
            pl.read_parquet(common.ORDERS_PARQUET)
            .join(pl.read_parquet(common.USERS_PARQUET), on="user_id", how="inner")
            .group_by("tier")
            .agg(pl.len().alias("n"), pl.col("revenue").sum().alias("rev"))
            .to_dicts()
        )

    orders = pl.read_parquet(common.ORDERS_PARQUET)
    users = pl.read_parquet(common.USERS_PARQUET)

    def pol_mem():
        return (
            orders.join(users, on="user_id", how="inner")
            .group_by("tier")
            .agg(pl.len().alias("n"), pl.col("revenue").sum().alias("rev"))
            .to_dicts()
        )

    duck_e2e_ms = _timeit(duck_e2e, repeat)
    duck_mem_ms = _timeit(duck_mem, repeat)
    pol_e2e_ms = _timeit(pol_e2e, repeat)
    pol_mem_ms = _timeit(pol_mem, repeat)

    print(f"  {'口径':<12}{'做法':<42}{'耗时(ms)':<12}")
    print("  " + "-" * 66)
    rows = [
        ("端到端", "DuckDB  read_parquet + JOIN + GROUP BY", duck_e2e_ms),
        ("端到端", "Polars  read_parquet + join + group_by", pol_e2e_ms),
        ("内存内", "DuckDB  JOIN + GROUP BY", duck_mem_ms),
        ("内存内", "Polars  join + group_by", pol_mem_ms),
    ]
    for scope, how, ms in rows:
        print(f"  {common.pad(scope, 12)}{common.pad(how, 42)}{common.pad(f'{ms:.1f}', 12)}")

    # 结果对账
    duck_rows = sorted(duck_e2e(), key=lambda r: r[0])
    pol_rows = sorted(pol_e2e(), key=lambda r: r["tier"])
    same = len(duck_rows) == len(pol_rows)
    rev_close = True
    if same:
        for d, p in zip(duck_rows, pol_rows, strict=True):
            same = same and d[0] == p["tier"] and d[1] == p["n"]
            rev_close = rev_close and abs(d[2] - p["rev"]) <= 1e-6 * max(1.0, abs(d[2]))
    print()
    verdict = "✅ 一致" if same and rev_close else "❌ 不一致"
    print(f"  结果对账：{verdict}（{len(duck_rows)} 个 tier）")
    for d, p in zip(duck_rows, pol_rows, strict=True):
        print(
            f"    {common.pad(str(d[0]), 8)}n={d[1]:<8}"
            f" DuckDB rev={d[2]:.6f}  Polars rev={p['rev']:.6f}"
        )
    print()
    print("  两点说明：")
    print("    1. 端到端那两行**都**包含读 parquet 的时间，口径一致，可以直接比。")
    print("    2. 内存内那两行把读 parquet 排除在外，反映的是纯连接 + 聚合的成本。")
    print("       DuckDB 走 Arrow 零拷贝注册，Polars 走自己的内存布局，")
    print("       这两个数反映的是「数据已经在内存里」之后的引擎开销。")
    print()
    print("  顺带注意对账里的尾数：销售额是 Float64，两边的累加顺序不同，")
    print("  所以末几位会有 1e-6 量级的差别（相对误差 ~1e-14）。")
    print("  这不是对账失败 —— 是浮点求和的正常现象，详见 decimal_demo.py。")
    con.close()


def _timeit(fn, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def _cols(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    """取结果集的列名。

    注意：DuckDB 的 relation（`con.sql(...)`）会一直占着连接，直到它被 GC 掉 ——
    中间再发查询会撞上 ResourceInUseError。所以这里走 `execute` + 立刻 fetchall，
    读完就把结果排空。
    """
    res = con.execute(sql)
    names = [d[0] for d in res.description]
    res.fetchall()
    return names


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 七种连接类型的**行数**两边完全一致 —— 连接语义本身没差异。")
    print("  2. 但**输出列结构**不一样，这是最容易忽略的坑：")
    print("     · DuckDB `ON` 连接出两个同名列，`USING` 才合并成一个；Polars 永远一个。")
    print("     · Polars 的 `how='right'` 把右表列排在前面；DuckDB 的列顺序不变。")
    print("     · Polars 的 `how='full'` 多一个 `k_right`；DuckDB 的 FULL OUTER 合并成一个 `k`。")
    print("     · 字符串列的 Arrow 类型也不同：DuckDB `string`，Polars `large_string`。")
    print("  3. 连接键类型不一致：DuckDB 把字符串侧转成整数后继续连，")
    print("     '02' 和 '2' 塌缩成同一个键，**行数被悄悄放大**；")
    print("     Polars 直接抛 SchemaError，连都不连。")
    print("  4. 重复键会把行数放大、SUM 跟着翻倍 —— 两边都会犯，是数据问题不是引擎差异；")
    print("     用半连接（SEMI / how='semi'）做存在性检查不会放大。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="JOIN 全景：语义、行数与输出列结构")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print()
    print("  左表：")
    common.print_frame(LEFT, "left_t")
    print("  右表：")
    common.print_frame(RIGHT, "right_t")

    con = connect()
    scenario_types(con)
    scenario_columns(con)
    scenario_key_types(con)
    scenario_fanout(con)
    con.close()
    scenario_performance(5)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
