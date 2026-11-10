"""Schema 演进：改表结构、读写不一致的 schema、以及两边有多严格。

* DuckDB 有完整的 `ALTER TABLE`（加列 / 删列 / 改类型 / 改名）；Polars 没有，
  改 schema 的方式是「造一个新 DataFrame」。
* DuckDB 的**标识符不区分大小写**（`SELECT ID` 能查到 `id` 列），Polars **区分**。
* 类型转换失败：两边默认都**报错**，但 DuckDB 有 `TRY_CAST`、Polars 有 `cast(strict=False)`。
* 读一批 schema 不一致的 parquet：默认行为**不一样**，而且 DuckDB 默认会**静默丢列**。
* 拼表：Polars 的 `concat` 默认严格，DuckDB 的 `UNION ALL` 会做隐式类型转换。

用法：
    uv run python src/schema_demo.py
    uv run python src/schema_demo.py --rows 1000000 --repeat 5
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
    con = duckdb.connect()
    con.execute("CREATE TABLE t (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO t VALUES (1,'a'),(2,'b')")
    return con


# ---------------------------------------------------------------- 1. ALTER TABLE


def scenario_alter(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 改表结构：DuckDB 改表，Polars 造新表")

    print("DuckDB 的 ALTER TABLE：")
    steps = [
        "ALTER TABLE t ADD COLUMN amount DOUBLE DEFAULT 0",
        "ALTER TABLE t RENAME COLUMN name TO label",
        "ALTER TABLE t ALTER COLUMN label TYPE VARCHAR",
        "ALTER TABLE t DROP COLUMN amount",
    ]
    for sql in steps:
        try:
            con.execute(sql)
            print(f"  ✅ {sql}")
        except Exception as exc:
            print(f"  ❌ {sql} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print(f"  改完之后的 schema：{con.sql('SELECT * FROM t').to_arrow_table().schema}")

    print("\nPolars 没有 ALTER TABLE —— 对应的写法是：")
    df = pl.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    print("  加列：with_columns(pl.col('id') * 10).alias('id10')")
    print("  删列：drop('name')")
    print("  改名：rename({'name': 'label'})")
    print("  改类型：with_columns(pl.col('id').cast(pl.Int64))")
    print("  → 全部都是「返回一个新对象」，原来那个不动（和 dml_demo 一个道理）。")
    print(f"  DataFrame 上有没有 alter 之类的 API：{[n for n in dir(df) if 'alter' in n.lower()]}")

    print("\n一个实际差别：DuckDB 改完表就落盘了，Polars 改完还得自己写出去。")
    print("  DuckDB 的表在磁盘上，进程退出数据还在；")
    print("  Polars 的 DataFrame 只活在内存里，除非 write_parquet / write_database。")


# ---------------------------------------------------------------- 2. 大小写


def scenario_case(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 标识符大小写：DuckDB 不敏感，Polars 敏感")

    # 单独建一张表，别受场景 1 改名的影响
    con.execute("CREATE TABLE c (Id INTEGER, Name VARCHAR)")
    con.execute("INSERT INTO c VALUES (1, 'a'), (2, 'b')")

    try:
        rows = con.sql("SELECT ID, NAME FROM c ORDER BY ID").fetchall()
        print(f"  DuckDB SELECT ID, NAME FROM c -> {rows}")
        print("  → 建表时是 Id / Name，查询时写成 ID / NAME，照样查得到。")
    except Exception as exc:
        print(f"  DuckDB -> {type(exc).__name__}: {str(exc).splitlines()[0]}")

    df = pl.DataFrame({"Id": [1, 2], "Name": ["a", "b"]})
    try:
        df.select("ID")
        print("  Polars select('ID') -> 成功（不符合预期）")
    except Exception as exc:
        print(f"  Polars select('ID') -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print(f"  Polars select('Id')（大小写一致）-> {df.select('Id').to_dicts()}")

    print("\n→ 迁移时最容易踩的一类：上游给的字段名是 User_ID / user_id / USER_ID，")
    print("  DuckDB 一视同仁，Polars 一字不差。写 SQL 和写 Polars 表达式时得各自记一份。")

    print("\nDuckDB 的标识符不敏感到什么程度（字符串字面量仍然敏感）：")
    case_eq = con.sql("SELECT 'ID' = 'id'").fetchall()[0][0]
    print(f"  SELECT 'ID' = 'id' -> {case_eq}")
    print("  → 只有标识符（表名、列名）不敏感，字符串常量照常区分大小写。")


# ---------------------------------------------------------------- 3. 类型转换


def scenario_cast(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 类型转换失败：报错还是填 NULL")

    df = pl.DataFrame({"x": ["1", "abc", "3"]})

    try:
        df.with_columns(pl.col("x").cast(pl.Int64))
        print("  Polars cast 成功了（不符合预期）")
    except Exception as exc:
        print(
            f"  Polars cast（默认 strict=True）-> {type(exc).__name__}: {str(exc).splitlines()[0]}"
        )

    loose = df.with_columns(pl.col("x").cast(pl.Int64, strict=False))
    print(f"  Polars cast(strict=False)      -> {loose.to_dicts()}")

    try:
        con.sql("SELECT 'abc'::INTEGER").fetchall()
        print("  DuckDB ::INTEGER 成功了（不符合预期）")
    except Exception as exc:
        print(
            f"  DuckDB 'abc'::INTEGER          -> {type(exc).__name__}: {str(exc).splitlines()[0]}"
        )

    try_cast = con.sql("SELECT TRY_CAST('abc' AS INTEGER)").fetchall()[0][0]
    print(f"  DuckDB TRY_CAST('abc' AS INTEGER) -> {try_cast}")
    print(
        "\n→ 两边的「宽松转换」开关都在，但默认都是**报错**（这是好事，静默变 NULL 更容易坑人）。"
    )
    print("  DuckDB 的 TRY_CAST 对 NULL 输入也返回 NULL，Polars 的 strict=False 同理。")


# ---------------------------------------------------------------- 4. 读不一致的 schema


def scenario_read_mismatch(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 读一批 schema 不一致的文件：默认行为不一样")

    cases = [
        (
            "后一个文件**少**一列",
            [pl.DataFrame({"id": [1, 2], "v": [10.0, 20.0]}), pl.DataFrame({"id": [3]})],
        ),
        (
            "后一个文件**多**一列",
            [
                pl.DataFrame({"id": [1, 2], "v": [10.0, 20.0]}),
                pl.DataFrame({"id": [3], "v": [30.0], "extra": ["x"]}),
            ],
        ),
        (
            "同一列**类型**不同",
            [pl.DataFrame({"id": [1], "v": [10.0]}), pl.DataFrame({"id": [2], "v": ["x"]})],
        ),
    ]

    for label, frames in cases:
        print(f"\n  情况：{label}")
        tmpdir = Path(tempfile.mkdtemp(prefix="schema_demo_"))
        for i, frame in enumerate(frames):
            frame.write_parquet(tmpdir / f"f{i}.parquet")
        glob = str(tmpdir / "*.parquet")

        try:
            rows = con.sql(f"SELECT * FROM read_parquet('{glob}')").fetchall()
            duck_default = f"✅ {rows}"
        except Exception as exc:
            duck_default = f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        print(f"    DuckDB 默认          -> {duck_default}")

        try:
            rows = con.sql(f"SELECT * FROM read_parquet('{glob}', union_by_name=true)").fetchall()
            duck_union = f"✅ {rows}"
        except Exception as exc:
            duck_union = f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        print(f"    DuckDB union_by_name -> {duck_union}")

        try:
            rows = pl.read_parquet(glob).to_dicts()
            pol_default = f"✅ {rows}"
        except Exception as exc:
            pol_default = f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        print(f"    Polars 默认          -> {pol_default}")

        try:
            rows = pl.read_parquet(glob, missing_columns="insert").to_dicts()
            pol_insert = f"✅ {rows}"
        except Exception as exc:
            pol_insert = f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        print(f"    Polars missing_columns='insert' -> {pol_insert}")

    print("\n  三条要记住的：")
    print("  1. DuckDB 默认遇到「少一列」会报错，但遇到「多一列」是**静默丢掉**多出来的那列 ——")
    print("     最危险的不是报错，是不报错。")
    print("  2. Polars 默认两种情况都报错，并且错误信息会提示该用哪个参数。")
    print("  3. 两边的「放宽」开关不是同一个：")
    print("     DuckDB 是 union_by_name=true（一个开关管缺列 + 多列 + 类型），")
    print("     Polars 是 missing_columns='insert'（只管缺列；多列和类型不一致仍然报错）。")


# ---------------------------------------------------------------- 5. 拼表


def scenario_concat(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 拼表：Polars 的 concat 默认严格，DuckDB 会隐式转类型")

    x = pl.DataFrame({"id": [1], "v": [1]})
    y = pl.DataFrame({"id": [2], "v": [2.5]})
    try:
        pl.concat([x, y])
        print("  Polars concat 成功了（不符合预期）")
    except Exception as exc:
        print(
            f"  Polars concat（Int64 + Float64）-> {type(exc).__name__}: {str(exc).splitlines()[0]}"
        )
    relaxed = pl.concat([x, y], how="diagonal_relaxed")
    print(f"  Polars concat(how='diagonal_relaxed') -> {relaxed.to_dicts()}")

    duck_rows = con.sql("SELECT 1::INTEGER AS id UNION ALL SELECT 2.5::DOUBLE AS id").fetchall()
    print(f"  DuckDB UNION ALL（Int + Double）-> {duck_rows}")
    print("  → DuckDB 自动把两边提升到公共类型；Polars 默认报错，要显式说 relaxed。")

    print("\n列名不齐也一样：")
    a = pl.DataFrame({"id": [1], "v": [1.0]})
    b = pl.DataFrame({"id": [2], "w": [2.0]})
    try:
        pl.concat([a, b])
        print("  Polars concat 成功了（不符合预期）")
    except Exception as exc:
        print(f"  Polars concat（列名不同）-> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print(f"  Polars concat(how='diagonal') -> {pl.concat([a, b], how='diagonal').to_dicts()}")
    duck_diff_names = con.sql("SELECT 1 AS id UNION ALL SELECT 2 AS other").fetchall()
    print(f"  DuckDB UNION ALL（列名不同）-> {duck_diff_names}")
    print("  → DuckDB 取第一个 SELECT 的列名，Polars 的 diagonal 会把缺的填成 NULL。")


# ---------------------------------------------------------------- 6. 改 schema 的代价


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 6 · 改 schema 的代价")

    print(f"数据：{rows} 行两列表")
    print(f"机器：{common.describe_machine()}")

    df = pl.DataFrame(
        {
            "id": list(range(rows)),
            "v": [float(i) for i in range(rows)],
        },
        schema={"id": pl.Int64, "v": pl.Float64},
    )
    tmpdir = Path(tempfile.mkdtemp(prefix="schema_demo_"))
    src = tmpdir / "rows.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE perf_t AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    def timed(fn, setup=None) -> float:
        ts: list[float] = []
        for _ in range(repeat):
            if setup is not None:
                setup()
            t0 = time.perf_counter()
            fn()
            ts.append(time.perf_counter() - t0)
        return statistics.median(ts) * 1000

    drop_c = lambda: con.execute("ALTER TABLE perf_t DROP COLUMN IF EXISTS c")  # noqa: E731
    add_c = lambda: con.execute("ALTER TABLE perf_t ADD COLUMN IF NOT EXISTS c DOUBLE")  # noqa: E731
    to_int = lambda: con.execute("ALTER TABLE perf_t ALTER COLUMN id TYPE INTEGER")  # noqa: E731
    to_bigint = lambda: con.execute("ALTER TABLE perf_t ALTER COLUMN id TYPE BIGINT")  # noqa: E731

    duck_add = timed(lambda: con.execute("ALTER TABLE perf_t ADD COLUMN c DOUBLE"), setup=drop_c)
    duck_drop = timed(lambda: con.execute("ALTER TABLE perf_t DROP COLUMN c"), setup=add_c)
    duck_type = timed(to_bigint, setup=to_int)
    pol_const = timed(lambda: polars_src.with_columns(pl.lit(0.0).alias("c")))
    pol_calc = timed(lambda: polars_src.with_columns((pl.col("v") * 2).alias("c")))
    pol_cast = timed(lambda: polars_src.with_columns(pl.col("id").cast(pl.Int64)))
    pol_drop = timed(lambda: polars_src.drop("v"))

    print("\n" + common.pad("操作", 30) + common.pad_left("中位数 (ms)", 16))
    print("-" * 46)
    rows_out = [
        ("DuckDB ALTER ADD COLUMN", duck_add),
        ("DuckDB ALTER DROP COLUMN", duck_drop),
        ("DuckDB ALTER COLUMN TYPE", duck_type),
        ("Polars 加常量列 pl.lit", pol_const),
        ("Polars 加计算列 v*2", pol_calc),
        ("Polars cast 一列", pol_cast),
        ("Polars drop 一列", pol_drop),
    ]
    for label, ms in rows_out:
        print(common.pad(label, 30) + common.pad_left(f"{ms:.2f}", 16))

    print("\n口径与解读：")
    print("  · DuckDB 的 ALTER 是**改磁盘上的表**：要写元数据、WAL，改类型还要重写整列。")
    print("  · Polars 这边只是算出一个新对象，没有落盘。")
    if pol_const > 0:
        print(
            f"  · Polars 加常量列（{pol_const:.2f} ms）比加计算列（{pol_calc:.2f} ms）快 "
            f"{pol_calc / pol_const:.1f} 倍，因为 pl.lit 是**广播**，底层只存一个标量。"
        )
    print("  → 所以这两组数字量的不是同一件事：DuckDB 量的是「持久化改表」，")
    print("    Polars 量的是「内存里造一个新表」。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的 Schema 演进差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = connect()
    try:
        scenario_alter(con)
        scenario_case(con)
        scenario_cast(con)
        scenario_read_mismatch(con)
        scenario_concat(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
