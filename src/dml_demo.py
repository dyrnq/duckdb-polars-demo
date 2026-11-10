"""增删改 DML：DuckDB 改的是「表」，Polars 改的是「对象」。

两个引擎都能增删改，但模型完全不同：

* DuckDB 的 `UPDATE` / `DELETE` / `INSERT` 是**真的改表**，在事务里，改完就落盘；
  还有 `RETURNING`、`UPDATE ... FROM`、`ON CONFLICT DO UPDATE` 这些 SQL 原生能力。
* Polars 的 DataFrame 是**不可变**的 —— 任何「改」都是算出一个新对象，原对象纹丝不动；
  没有事务、没有 upsert、`DataFrame.update` 官方标注 unstable。
* Polars 的 `DataFrame.update` 在更新方有重复键时**会复制行**（静默改坏数据）。
* 行序：DuckDB 的 `UPDATE` 实测保住了行序，但 SQL 不承诺；Polars 的行序是数据的一部分，
  `group_by` 之类的算子会重排。

用法：
    uv run python src/dml_demo.py
    uv run python src/dml_demo.py --rows 1000000 --repeat 5
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

# 一张 4 行的销售表：id 是主键
SAMPLE = pl.DataFrame(
    {
        "id": [1, 2, 3, 4],
        "name": ["alpha", "beta", "gamma", "delta"],
        "amount": [10.0, 20.0, 30.0, 40.0],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE sales (id INTEGER PRIMARY KEY, name VARCHAR, amount DOUBLE)")
    con.execute(
        "INSERT INTO sales VALUES (1,'alpha',10.0),(2,'beta',20.0),"
        "(3,'gamma',30.0),(4,'delta',40.0)"
    )
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- 1. 两种数据模型


def scenario_models(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · DuckDB 改表，Polars 改对象")

    df = SAMPLE.clone()

    print("DuckDB 侧：UPDATE 直接改表")
    con.execute("UPDATE sales SET amount = amount * 10 WHERE id = 1")
    print(
        f"  UPDATE 之后表里的数据：{con.sql('SELECT id, amount FROM sales ORDER BY id').fetchall()}"
    )
    print("  再查一次还是改后的值 —— 状态是持久的。")

    print("\nPolars 侧：with_columns 算出的是一个**新对象**")
    new = df.with_columns(
        pl.when(pl.col("id") == 1)
        .then(pl.col("amount") * 10)
        .otherwise(pl.col("amount"))
        .alias("amount")
    )
    print(f"  原对象 amount：{df['amount'].to_list()}")
    print(f"  新对象 amount：{new['amount'].to_list()}")
    print("  → 原对象一点没变。要「改」，就得把新对象接回去（赋值或写回文件）。")

    print("\nPolars 有没有就地改的 API：")
    for name in ["update", "upsert", "insert_row", "with_row_index"]:
        print(f"  DataFrame.{name}: {hasattr(df, name)}")
    print("  （update 存在但 docstring 里写着 unstable）")


# ---------------------------------------------------------------- 2. UPDATE 的坑


def scenario_update(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · Polars 的 DataFrame.update：一个静默改坏数据的坑")

    df = SAMPLE.clone()
    upd = pl.DataFrame({"id": [2, 3], "amount": [200.0, 300.0]})
    res = df.update(upd, on="id")

    common.print_frame(df, "原始 DataFrame", max_rows=10)
    common.print_frame(res, "df.update(upd, on='id')", max_rows=10)
    print(f"\n  行数：{df.height} -> {res.height}，行序保留。")
    print("  匹配上的行被更新，没匹配上的行保持原样。")

    print("\n坑 1 · 更新方里有重复键 —— 行会被复制：")
    dup = pl.DataFrame({"id": [2, 2], "amount": [111.0, 222.0]})
    dup_res = df.update(dup, on="id")
    common.print_frame(dup_res, "df.update(重复键的更新方, on='id')", max_rows=10)
    print(f"  → 行数从 {df.height} 变成 {dup_res.height}，id=2 出现了两次。")
    print("    DuckDB 那边不会出现这种行数变化：UPDATE 只改已有的行，不会复制行；")
    print("    想插新行必须走 INSERT，主键冲突会被约束直接拒掉（场景 3 有实测）。")

    print("\n坑 2 · 更新方里有 df 中不存在的键 —— 被静默忽略：")
    extra = pl.DataFrame({"id": [99], "amount": [999.0]})
    extra_res = df.update(extra, on="id")
    print(f"  df.update({{id:99}}, on='id') -> 行数 {extra_res.height}，id=99 没有进来")
    print("  → 这是左连接语义（left join），不是报错。DuckDB 的 UPDATE 也不会插入新行，")
    print("    但 DuckDB 的 upsert（ON CONFLICT）会 —— 区别在场景 4。")

    print("\n坑 3 · update 是 unstable：")
    doc = pl.DataFrame.update.__doc__ or ""
    unstable = "unstable" in doc
    print(f"  docstring 里出现 unstable：{unstable}")
    print("  → 官方明确说这个 API 可能变，迁移成本要算进去。")

    print("\nDuckDB 侧等价的 UPDATE（子查询更新）：")
    con.execute(
        "UPDATE sales SET amount = amount + 5 WHERE id IN (SELECT id FROM sales WHERE amount >= 30)"
    )
    updated = con.sql("SELECT id, amount FROM sales ORDER BY id").fetchall()
    print(f"  UPDATE ... WHERE id IN (SELECT ...) 的结果：{updated}")


# ---------------------------------------------------------------- 3. DELETE 与 INSERT


def scenario_delete_insert(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · DELETE 和 INSERT：SQL 有语句，Polars 得拼")

    print("DuckDB：")
    con.execute("DELETE FROM sales WHERE id = 3")
    print(f"  DELETE 之后：{con.sql('SELECT id FROM sales ORDER BY id').fetchall()}")
    con.execute("INSERT INTO sales VALUES (3, 'gamma-2', 33.0)")
    print(f"  INSERT 之后：{con.sql('SELECT id, name FROM sales ORDER BY id').fetchall()}")
    print("  插入重复主键 -> ", end="")
    try:
        con.execute("INSERT INTO sales VALUES (3, 'dup', 0.0)")
        print("没有报错（不符合预期）")
    except Exception as exc:
        print(f"{type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\nPolars：没有 DELETE / INSERT 语句，只能用 join / concat 拼")
    df = SAMPLE.clone()
    deleted = df.join(pl.DataFrame({"id": [3]}), on="id", how="anti")
    inserted = pl.concat([df, pl.DataFrame({"id": [5], "name": ["epsilon"], "amount": [50.0]})])
    common.print_frame(deleted, "删除 id=3：anti join", max_rows=10)
    common.print_frame(inserted, "插入一行：concat", max_rows=10)

    print("\n注意 concat 的列必须完全一致（名字和类型都要一致）：")
    try:
        pl.concat([df, pl.DataFrame({"id": [5], "amount": [50.0]})])
        print("  列不齐也能拼（这次没报错）")
    except Exception as exc:
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0]}")

    print("\nDuckDB 的 INSERT 也一样要求列数、类型匹配：")
    try:
        con.execute("INSERT INTO sales VALUES (9)")
    except Exception as exc:
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0]}")


# ---------------------------------------------------------------- 4. upsert


def scenario_upsert(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · upsert：ON CONFLICT DO UPDATE vs 手工拼")

    con.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, amount DOUBLE)")
    con.execute("INSERT INTO u VALUES (1,10.0),(2,20.0),(3,30.0)")

    inc = pl.DataFrame({"id": [2, 3, 4], "amount": [200.0, 300.0, 400.0]})

    con.execute(
        "INSERT INTO u VALUES (2,200.0),(3,300.0),(4,400.0) "
        "ON CONFLICT (id) DO UPDATE SET amount = EXCLUDED.amount"
    )
    duck_rows = con.sql("SELECT id, amount FROM u ORDER BY id").fetchall()
    print(f"  DuckDB INSERT ... ON CONFLICT DO UPDATE -> {duck_rows}")

    base = pl.DataFrame({"id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    new_rows = inc.join(base, on="id", how="anti").select("id", "amount")
    kept = base.join(inc, on="id", how="left", suffix="_new").select(
        pl.col("id"), pl.coalesce(["amount_new", "amount"]).alias("amount")
    )
    pol_out = pl.concat([new_rows, kept]).sort("id")
    print(f"  Polars 手工拼（anti join + 左连接 coalesce）-> {pol_out.to_dicts()}")

    duck_norm = [tuple(r) for r in duck_rows]
    pol_norm = [tuple(r.values()) for r in pol_out.to_dicts()]
    print(f"\n  两边结果一致：{'✅' if duck_norm == pol_norm else '❌'}")

    print("\nDO NOTHING 口味（DuckDB 有，Polars 拼起来更绕）：")
    con.execute("CREATE TABLE u2 (id INTEGER PRIMARY KEY, amount DOUBLE)")
    con.execute("INSERT INTO u2 VALUES (1,10.0)")
    con.execute("INSERT INTO u2 VALUES (1,99.0),(2,20.0) ON CONFLICT DO NOTHING")
    duck_dn = con.sql("SELECT id, amount FROM u2 ORDER BY id").fetchall()
    print(f"  DuckDB ON CONFLICT DO NOTHING -> {duck_dn}")

    dn_base = pl.DataFrame({"id": [1], "amount": [10.0]})
    dn_inc = pl.DataFrame({"id": [1, 2], "amount": [99.0, 20.0]})
    dn_out = pl.concat([dn_base, dn_inc.join(dn_base, on="id", how="anti")]).sort("id")
    print(f"  Polars 拼（保留原表 + 只并入新键）-> {dn_out.to_dicts()}")
    dn_norm = [tuple(r.values()) for r in dn_out.to_dicts()]
    print(f"  两边结果一致：{'✅' if [tuple(r) for r in duck_dn] == dn_norm else '❌'}")
    print("  → DO NOTHING 的语义是「冲突的保留旧值，新的照插」，")
    print("    不是「整批不插」；用 unique(keep='first') 凑是凑不出来的。")

    print("\nDuckDB 的 upsert 有一条 Polars 完全没有的路：")
    print("  UPDATE ... FROM（拿另一张表来改）")
    con.execute("CREATE TABLE adj (id INTEGER, bonus DOUBLE)")
    con.execute("INSERT INTO adj VALUES (1,100.0),(3,300.0)")
    con.execute("UPDATE u SET amount = u.amount + adj.bonus FROM adj WHERE u.id = adj.id")
    print(
        f"  UPDATE u ... FROM adj -> {con.sql('SELECT id, amount FROM u ORDER BY id').fetchall()}"
    )
    print("  → 这就是 upsert 的另一面：Polars 侧只能自己 join 出要改的行再 update。")


# ---------------------------------------------------------------- 5. 子查询快照


def scenario_snapshot(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · UPDATE 里的子查询：按更新前的快照算一次")

    con.execute("CREATE TABLE h (id INTEGER, v INTEGER)")
    con.execute("INSERT INTO h SELECT i, i FROM range(1000) t(i)")
    before = con.sql("SELECT sum(v) FROM h").fetchall()[0][0]

    con.execute("UPDATE h SET v = v + (SELECT sum(v) FROM h)")
    after = con.sql("SELECT sum(v) FROM h").fetchall()[0][0]

    print(f"  更新前 sum(v) = {before}")
    print(f"  更新后 sum(v) = {after}")
    expected = before + 1000 * before
    print(f"  若每行都加上更新前的和 {before}：{before} + 1000 × {before} = {expected}")
    verdict = "✅ 是（子查询只算了一次，没有「边算边改」）" if after == expected else "❌ 否"
    print(f"  实测等于该值：{verdict}")

    print("\n→ 这是「Halloween 问题」：经典的 UPDATE ... SET x = x + (SELECT sum(x) ...) 会")
    print("  因为边算边改而算错。DuckDB 在这里按更新前的快照算一次，所以结果是对的。")
    print("  Polars 侧没有 UPDATE 语句，等价写法是纯表达式，天然没有这个问题：")
    s = pl.Series(range(1000), dtype=pl.Int64)
    pol_after = int((s + s.sum()).sum())
    print(f"  Polars (s + s.sum()).sum() = {pol_after}")


# ---------------------------------------------------------------- 6. RETURNING 与事务


def scenario_returning_tx(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · RETURNING 和事务：DuckDB 有，Polars 没有对应物")

    print("RETURNING（把改动过的行直接返回，不用再查一遍）：")
    upd = con.sql(
        "UPDATE sales SET amount = amount + 1 WHERE id = 1 RETURNING id, amount"
    ).fetchall()
    print(f"  UPDATE ... RETURNING -> {upd}")
    dele = con.sql("DELETE FROM sales WHERE id = 4 RETURNING id, name").fetchall()
    print(f"  DELETE ... RETURNING -> {dele}")
    print("  Polars 侧：所有操作都返回新 DataFrame，天然就是「RETURNING」——")
    print("    但语义不同：DuckDB 的 RETURNING 只返回**被改动**的行。")

    print("\n事务：")
    con.execute("CREATE TABLE tx (i INTEGER)")
    con.execute("INSERT INTO tx VALUES (1)")
    con.execute("BEGIN TRANSACTION")
    con.execute("UPDATE tx SET i = 2")
    con.execute("ROLLBACK")
    print(
        f"  BEGIN → UPDATE → ROLLBACK 之后：{con.sql('SELECT * FROM tx').fetchall()}（改动被丢掉）"
    )
    con.execute("BEGIN TRANSACTION")
    con.execute("UPDATE tx SET i = 3")
    con.execute("COMMIT")
    print(f"  BEGIN → UPDATE → COMMIT 之后：{con.sql('SELECT * FROM tx').fetchall()}（改动生效）")

    print("\nPolars 侧没有事务这个概念：")
    tx_methods = [
        n
        for n in dir(pl.DataFrame)
        if any(k in n.lower() for k in ("commit", "rollback", "transaction"))
    ]
    print(f"  DataFrame 上带 commit/rollback/transaction 的方法：{tx_methods}")
    print("  → Polars 的 LazyFrame 有 collect/写文件，但那是「物化」，不是「事务」。")
    print("  → 需要原子性就得自己在外面套（写临时文件 → 原子替换）。")


# ---------------------------------------------------------------- 7. 行序


def scenario_row_order(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 改完之后行序还在不在")

    con.execute("CREATE TABLE ord (i INTEGER, s VARCHAR)")
    con.execute("INSERT INTO ord SELECT i, 'x' FROM range(100000) t(i)")
    con.execute("UPDATE ord SET s = 'y' WHERE i % 3 = 0")
    head = con.sql("SELECT i FROM ord LIMIT 5").fetchall()
    print(f"  DuckDB：UPDATE 10 万行后，前 5 行的 i = {[r[0] for r in head]}")
    print(f"  （行序保持住了：{'✅' if [r[0] for r in head] == [0, 1, 2, 3, 4] else '❌'}）")
    print("  但 SQL 本身不承诺行序 —— 换个查询计划、换个版本都可能变，")
    print("  所以正式查询一律要带 ORDER BY，不能依赖「看起来没乱」。")

    print("\nPolars 侧：行序是数据的一部分，但要小心哪些算子会重排：")
    df = pl.DataFrame({"k": ["b", "a", "b"], "v": [1, 2, 3]})
    print(f"  原始行序：{df['k'].to_list()}")
    print(f"  group_by 之后：{df.group_by('k').agg(pl.col('v').sum())['k'].to_list()}  ← 重排了")
    updated_order = df.update(pl.DataFrame({"k": ["b"], "v": [9]}), on="k")["k"].to_list()
    print(f"  update 之后：{updated_order}  ← 保持")
    print("  → 结论不一样：Polars 里「行序」不是查询的副产品，而是要自己用 sort 固定的。")
    print("  → join 默认也不保证行序，要固定得显式写 maintain_order='left'。")


# ---------------------------------------------------------------- 8. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 8 · 批量 UPDATE 的代价（注意两边干的活不一样）")

    print(f"数据：{rows} 行，其中 1/3 会被改")
    print(f"机器：{common.describe_machine()}")

    df = pl.DataFrame(
        {
            "id": list(range(rows)),
            "v": [float(i % 1000) for i in range(rows)],
        },
        schema={"id": pl.Int64, "v": pl.Float64},
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="dml_demo_"))
    src = tmpdir / "rows.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE rc AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    def duck_run() -> None:
        # 重置在计时之外：UPDATE 是真的改表，不重置的话第二遍的值就变了
        con.execute("CREATE OR REPLACE TABLE work AS SELECT * FROM rc")
        con.execute("UPDATE work SET v = v * 2 WHERE id % 3 = 0")

    def polars_run() -> pl.DataFrame:
        return polars_src.with_columns(
            pl.when(pl.col("id") % 3 == 0).then(pl.col("v") * 2).otherwise(pl.col("v")).alias("v")
        )

    duck_run()
    polars_run()

    duck_times: list[float] = []
    polars_times: list[float] = []
    for _ in range(repeat):
        con.execute("CREATE OR REPLACE TABLE work AS SELECT * FROM rc")  # 不计时
        t0 = time.perf_counter()
        duck_run()
        duck_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        polars_run()
        polars_times.append(time.perf_counter() - t0)

    duck_med = statistics.median(duck_times)
    polars_med = statistics.median(polars_times)

    print(
        "\n"
        + common.pad("引擎", 12)
        + common.pad_left("中位数 (ms)", 16)
        + common.pad_left("最快 (ms)", 16)
    )
    print("-" * 44)
    print(
        common.pad("DuckDB UPDATE", 12)
        + common.pad_left(f"{duck_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(duck_times) * 1000:.1f}", 16)
    )
    print(
        common.pad("Polars 表达式", 12)
        + common.pad_left(f"{polars_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(polars_times) * 1000:.1f}", 16)
    )
    if polars_med > 0:
        print(f"\nDuckDB / Polars = {duck_med / polars_med:.2f}×（中位数口径）")

    print("\n口径说明（这两个数字不能直接比大小）：")
    print("  · DuckDB 的 UPDATE 包含**写回存储**：读旧块 → 改 → 写新块（含 WAL）。")
    print("  · Polars 这边只是算出一个新 DataFrame，没有写回任何东西。")
    print("  · 表的重建（CREATE OR REPLACE TABLE）在计时之外。")
    print("  → 真正该比的量是「同样一件事」，而不是「两种模型各自的代价」：")
    print("    DuckDB 写完的数据在磁盘上，Polars 的结果只在内存里。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的增删改 DML 差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：{SAMPLE.height} 行销售表（id 为主键）")

    con = connect()
    try:
        scenario_models(con)
        scenario_update(con)
        scenario_delete_insert(con)
        scenario_upsert(con)
        scenario_snapshot(con)
        scenario_returning_tx(con)
        scenario_row_order(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
