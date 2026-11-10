"""集合运算与去重：EXCEPT / INTERSECT / UNION 的 NULL 语义，和「去重保留哪一行」。

SQL 的集合运算是三个引擎里最容易「静默算错」的一块：

* DuckDB 的 `EXCEPT` / `INTERSECT` 把 NULL 当**相等的值**参与比较，
  而「NULL 等于 NULL」在普通比较里是 UNKNOWN —— 两套规则并存，很容易记混。
* Polars 2.x **没有** `DataFrame.difference` / `intersection`，集合运算得自己拼。
* `UNION`（去重）与 `UNION ALL`（不去重）在 NULL 上的行为也不同。
* 「按某几列去重、保留哪一行」：DuckDB 的 `DISTINCT ON` 和 Polars 的 `unique(subset=)`
  在没有确定排序时，保留的行都是不确定的。

用法：
    uv run python src/setop_demo.py
    uv run python src/setop_demo.py --rows 1000000 --repeat 5
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

# 两份带 NULL 的小表：a 有 (1,2,NULL)，b 有 (1,NULL,3)
A = pl.DataFrame({"x": pl.Series([1, 2, None], dtype=pl.Int64)})
B = pl.DataFrame({"x": pl.Series([1, None, 3], dtype=pl.Int64)})


def sort_key(v):
    """排序键：NULL 排最前面，其余按值排 —— 用来对齐两边的结果。"""
    return (v is None, v)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("a", A)
    con.register("b", B)
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- 1. EXCEPT / INTERSECT


def scenario_except_intersect(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · EXCEPT / INTERSECT：NULL 参与比较，而且 NULL 相等")

    print(f"表 a = {A['x'].to_list()}")
    print(f"表 b = {B['x'].to_list()}\n")

    exc = con.sql("SELECT x FROM a EXCEPT SELECT x FROM b").fetchall()
    inter = con.sql("SELECT x FROM a INTERSECT SELECT x FROM b").fetchall()
    exc_all = con.sql("SELECT x FROM a EXCEPT ALL SELECT x FROM b").fetchall()
    inter_all = con.sql("SELECT x FROM a INTERSECT ALL SELECT x FROM b").fetchall()

    print(f"  DuckDB EXCEPT           -> {exc}")
    print(f"  DuckDB INTERSECT        -> {inter}")
    print(f"  DuckDB EXCEPT ALL       -> {exc_all}")
    print(f"  DuckDB INTERSECT ALL    -> {inter_all}")

    print("\n为什么是这个结果：a 里有一个 NULL，b 里也有一个 NULL。")
    print("  · 集合运算比较的是「值是否相同」，DuckDB 在这里把两个 NULL 判成相同，")
    print("    所以 NULL 从 a 的结果里被减掉了（EXCEPT 里看不到它），")
    print("    又被算进了交集（INTERSECT 里能看到它）。")
    print("  · 对比普通比较：`NULL = NULL` 是 UNKNOWN（不是 true），")
    print("    `IS NOT DISTINCT FROM NULL` 才是「按值判等」。集合运算走的是后者那套。")

    print("\nDuckDB 侧还有一对「按 NULL 判等」的写法：")
    null_eq = q1(con, "SELECT count(*) FROM a JOIN b USING (x) WHERE x IS NOT DISTINCT FROM NULL")
    plain_eq = q1(con, "SELECT count(*) FROM a JOIN b USING (x) WHERE x = NULL")
    print(f"  IS NOT DISTINCT FROM NULL 的匹配数 : {null_eq}")
    print(f"  = NULL 的匹配数（永远 UNKNOWN）   : {plain_eq}")

    print("\nPolars 侧：2.x 根本没有对应的方法。")
    for name in ["difference", "intersection", "symmetric_difference", "set_difference"]:
        print(f"  DataFrame.{name}: {hasattr(A, name)}")


# ---------------------------------------------------------------- 2. Polars 侧怎么补


def scenario_polars_ways(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · Polars 侧怎么补出 EXCEPT / INTERSECT")

    print("Polars 2.x 没有 difference / intersection，只能用 join / is_in 拼。")
    print("但拼的时候要自己决定 NULL 怎么办 —— 这是最容易出错的地方。\n")

    b_values = B["x"].to_list()

    print("差集（a 里有、b 里没有的行）：")
    anti = A.join(B, on="x", how="anti")
    is_in = A.filter(~pl.col("x").is_in(b_values))
    null_aware = A.join(B, on="x", how="anti", nulls_equal=True)
    duck_exc = [r[0] for r in con.sql("SELECT x FROM a EXCEPT SELECT x FROM b").fetchall()]

    print(f"  join(how='anti')                    -> {anti['x'].to_list()}")
    print(f"  filter(~is_in(...))                 -> {is_in['x'].to_list()}")
    print(f"  join(how='anti', nulls_equal=True) -> {null_aware['x'].to_list()}")
    print(f"  DuckDB EXCEPT                       -> {duck_exc}")
    print()
    anti_sorted = sorted(anti["x"].to_list(), key=sort_key)
    null_sorted = sorted(null_aware["x"].to_list(), key=sort_key)
    duck_sorted = sorted(duck_exc, key=sort_key)
    print(f"  anti join 与 DuckDB 一致：{'✅' if anti_sorted == duck_sorted else '❌'}")
    print(f"  nulls_equal=True 与 DuckDB 一致：{'✅' if null_sorted == duck_sorted else '❌'}")

    print("\n三个写法给出三个不同的答案 —— 因为它们对 NULL 的处理各不相同：")
    print("  · anti join（默认）：NULL 不等于 NULL，a 里的 NULL 留在结果里")
    print("  · is_in：NULL 那一行算出来是 null，filter 把它当假值丢掉")
    print("  · nulls_equal=True：NULL 当普通值比，NULL 被减掉 —— 这个才对得上 DuckDB")

    print("\n交集（两边都有的行）：")
    inter = A.join(B, on="x", how="semi")
    inter_null = A.join(B, on="x", how="semi", nulls_equal=True)
    is_in_inter = A.filter(pl.col("x").is_in(b_values))
    duck_inter = [r[0] for r in con.sql("SELECT x FROM a INTERSECT SELECT x FROM b").fetchall()]

    print(f"  join(how='semi')                    -> {inter['x'].to_list()}")
    print(f"  filter(is_in(...))                  -> {is_in_inter['x'].to_list()}")
    print(f"  join(how='semi', nulls_equal=True) -> {inter_null['x'].to_list()}")
    print(f"  DuckDB INTERSECT                    -> {duck_inter}")
    inter_sorted = sorted(inter_null["x"].to_list(), key=sort_key)
    duck_inter_sorted = sorted(duck_inter, key=sort_key)
    print(
        f"  nulls_equal=True 与 DuckDB 一致：{'✅' if inter_sorted == duck_inter_sorted else '❌'}"
    )
    print("\n→ 结论：Polars 侧要用 join(..., nulls_equal=True) 才能复刻 DuckDB 的集合运算语义。")


# ---------------------------------------------------------------- 3. UNION


def scenario_union(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · UNION 与 UNION ALL：去重时 NULL 怎么算")

    con.execute("CREATE TABLE u1 AS SELECT * FROM a")
    con.execute("CREATE TABLE u2 AS SELECT * FROM b")

    union = con.sql("SELECT x FROM u1 UNION SELECT x FROM u2").fetchall()
    union_all = con.sql("SELECT x FROM u1 UNION ALL SELECT x FROM u2").fetchall()
    pol_concat = pl.concat([A, B]).unique().sort("x", nulls_last=True)
    pol_concat_all = pl.concat([A, B])

    print(f"  DuckDB UNION      -> {union}")
    print(f"  DuckDB UNION ALL  -> {union_all}")
    print(f"  Polars concat().unique() -> {pol_concat['x'].to_list()}")
    print(f"  Polars concat()           -> {pol_concat_all['x'].to_list()}")

    duck_union = sorted((r[0] for r in union), key=sort_key)
    pol_union = sorted(pol_concat["x"].to_list(), key=sort_key)
    print(f"\n  两边去重结果一致（排序后比）：{'✅' if duck_union == pol_union else '❌'}")
    print("  （DuckDB 的 UNION 会把 NULL 合成一个，Polars 的 unique() 也是 ——")
    print("    但如果 Polars 侧忘了 unique()，NULL 就会多出一个）")

    print("\nUNION ALL 保留所有行，包括重复的 NULL：")
    print(
        f"  DuckDB UNION ALL 有 {len(union_all)} 行，Polars concat() 有 {pol_concat_all.height} 行"
    )
    print("  → 这两个是对得上的（本次）。")

    print("\n列数与列名：")
    try:
        con.sql("SELECT x FROM u1 UNION SELECT y FROM u2").fetchall()
        print("  列名不一致的 UNION：允许")
    except Exception as exc:
        print(f"  列名不一致的 UNION：{type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("  Polars concat 要求列名一致，否则会报 schema mismatch。")


# ---------------------------------------------------------------- 4. 去重：保留哪一行


def scenario_dedup_keep(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 按列去重：保留哪一行，没有保证")

    con.execute(
        """
        CREATE TABLE d AS SELECT * FROM (
            VALUES ('a', 1, 10.0), ('a', 1, 20.0), ('b', 2, 30.0), ('b', 2, 40.0)
        ) t(k, id, v)
        """
    )

    duck_ordered = con.sql(
        "SELECT DISTINCT ON (k) k, id, v FROM d ORDER BY k, v DESC"
    ).to_arrow_table()
    duck_plain = con.sql("SELECT DISTINCT ON (k) k, id, v FROM d").to_arrow_table()
    polars_unique = (
        pl.DataFrame(
            {
                "k": ["a", "a", "b", "b"],
                "id": [1, 1, 2, 2],
                "v": [10.0, 20.0, 30.0, 40.0],
            }
        )
        .unique(subset=["k"], keep="first")
        .sort("k")
    )

    common.print_frame(
        pl.from_arrow(duck_ordered), "DuckDB DISTINCT ON (k) ORDER BY k, v DESC", max_rows=10
    )
    common.print_frame(
        pl.from_arrow(duck_plain), "DuckDB DISTINCT ON (k)（没有 ORDER BY）", max_rows=10
    )
    common.print_frame(polars_unique, "Polars unique(subset=['k'], keep='first')", max_rows=10)

    print("\nDuckDB 的 DISTINCT ON 依赖外层 ORDER BY 决定留哪一行 ——")
    print("  有 ORDER BY 时结果确定（留 v 最大的那行）；没有 ORDER BY 时留哪行是任意的。")
    print("Polars 的 unique(subset=..., keep='first') 留的是**当前行序**里的第一行，")
    print("  所以必须先 sort 到想要的顺序，再 unique。")

    print("\n去重的其它口味：")
    frame = pl.DataFrame({"k": ["a", "a", "b"], "v": [1, 1, 2]})
    keep_first = frame.unique(subset=["k"], keep="first")["k"].to_list()
    keep_last = frame.unique(subset=["k"], keep="last")["k"].to_list()
    keep_none = frame.unique(subset=["k"], keep="none")["k"].to_list()
    print(f"  Polars unique(keep='first')  -> {keep_first}")
    print(f"  Polars unique(keep='last')   -> {keep_last}")
    print(f"  Polars unique(keep='none')   -> {keep_none}   （把重复行全删了）")
    print("  DuckDB 没有 keep='none' 这种口味 —— SELECT DISTINCT 只保留一份。")
    print("  DuckDB 侧对应写法：GROUP BY k + min()/max()，或者窗口函数 row_number()。")
    print()
    print("  注意 Polars 的 unique() 默认**不保证保留原顺序**（maintain_order 默认 False），")
    print(f"  本次 first/last 两次恰好都返回 {keep_first}，但那是运气不是保证。")
    print("  要保留原顺序得加 maintain_order=True：")
    ordered = frame.unique(subset=["k"], keep="first", maintain_order=True)["k"].to_list()
    print(f"    maintain_order=True -> {ordered}")


# ---------------------------------------------------------------- 5. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 5 · 集合运算的代价（两个引擎做同一件事）")

    print(f"数据：左右各 {rows} 行，右表是左表去掉一些行")
    print(f"机器：{common.describe_machine()}")

    df = pl.DataFrame(
        {
            "x": list(range(rows)),
            "v": [float(i % 977) for i in range(rows)],
        },
        schema={"x": pl.Int64, "v": pl.Float64},
    )
    right = df.filter(pl.col("x") % 3 != 0)

    tmpdir = Path(tempfile.mkdtemp(prefix="setop_demo_"))
    left_path = tmpdir / "left.parquet"
    right_path = tmpdir / "right.parquet"
    df.write_parquet(left_path)
    right.write_parquet(right_path)

    con.execute("CREATE TABLE l AS SELECT * FROM read_parquet(?)", [str(left_path)])
    con.execute("CREATE TABLE r AS SELECT * FROM read_parquet(?)", [str(right_path)])
    polars_left = pl.read_parquet(left_path)
    polars_right = pl.read_parquet(right_path)

    duck_sql = "SELECT x, v FROM l EXCEPT SELECT x, v FROM r"

    def duck_run() -> pl.DataFrame:
        rel = con.sql(duck_sql)
        try:
            return pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def polars_run() -> pl.DataFrame:
        return polars_left.join(polars_right, on=["x", "v"], how="anti")

    duck_run()
    polars_run()

    duck_times: list[float] = []
    polars_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        duck_run()
        duck_times.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        polars_run()
        polars_times.append(time.perf_counter() - t0)

    duck_med = statistics.median(duck_times)
    polars_med = statistics.median(polars_times)

    print(f"\n{'引擎':<26}{'中位数 (ms)':>14}{'最快 (ms)':>14}")
    print("-" * 54)
    print(f"{'DuckDB EXCEPT':<26}{duck_med * 1000:>14.1f}{min(duck_times) * 1000:>14.1f}")
    print(f"{'Polars anti join':<26}{polars_med * 1000:>14.1f}{min(polars_times) * 1000:>14.1f}")
    if polars_med > 0:
        print(f"\nDuckDB / Polars = {duck_med / polars_med:.2f}×（中位数口径）")

    print("\n口径：输入已物化，计时的只有「集合运算 + 结果物化」。")
    print("注意 EXCEPT 内部要去重，而 anti join 不用 —— 两边干的活并不完全一样。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的集合运算与去重差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：a = {A['x'].to_list()}，b = {B['x'].to_list()}")

    con = connect()
    try:
        scenario_except_intersect(con)
        scenario_polars_ways(con)
        scenario_union(con)
        scenario_dedup_keep(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
