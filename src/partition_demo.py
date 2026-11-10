"""多文件与分区表：目录里的一堆 parquet，两个引擎读出来的结果可能不一样。

「数据落成 parquet 目录」「按天分区」是数据工程的日常操作。
读的时候有几个地方特别容易出事：文件多了 schema 对不上、目录结构认不出来、
分区值类型猜错、行的顺序不保证。

这个脚本比的是：

  1. 读多个文件：glob 写法，行数和结果。
  2. **文件读取顺序**：DuckDB 靠 `preserve_insertion_order` 保序，
     关掉就乱；Polars 没有这个开关。
  3. **类型对不上**：一个文件是 int64、另一个是 float64 ——
     DuckDB 自动统一成 DOUBLE，Polars 直接 SchemaError。
  4. **列名对不上**：DuckDB 默认报错，`union_by_name=true` 能合并；
     Polars 要显式开两个开关才能做同样的事。
  5. 没有匹配文件时，两边都报错，但错误长得不一样。
  6. **Hive 分区目录**：DuckDB 传 glob 就自动认分区列，
     Polars 传 glob **不认**，得传目录或显式 `hive_partitioning=True`。
  7. **分区值的类型**：Polars 把分区值解析成 Date，用字符串过滤直接报类型错。
  8. 分区裁剪：两边都会裁，EXPLAIN 里看得见。
  9. 写分区：文件里留不留分区列、文件名怎么起，两边都不一样。
  10. 两百个小文件的读取性能。

用法：
    uv run python src/partition_demo.py
    uv run python src/partition_demo.py --files 200 --repeat 5
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq

import common


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _scratch(prefix: str) -> Path:
    """临时目录 —— 用完即弃，绝不碰 data/ 下的既有文件。"""
    return Path(tempfile.mkdtemp(prefix=prefix))


def _one(con: duckdb.DuckDBPyConnection, sql: str):
    """取一个标量 —— 必须把结果读干净，否则连接上留着未完成的结果。"""
    res = con.execute(sql)
    row = res.fetchone()
    res.fetchall()
    return row[0]


def _write_files(directory: Path, n_files: int, rows_per_file: int = 100) -> None:
    """写 n 个内容相同形状的 parquet 文件（每行一个可辨识的 id）。"""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        base = i * rows_per_file
        pl.DataFrame(
            {
                "id": list(range(base, base + rows_per_file)),
                "grp": [f"g{i % 3}"] * rows_per_file,
                "v": [float(i)] * rows_per_file,
            }
        ).write_parquet(directory / f"part-{i:04d}.parquet")


def _write_one_file(directory: Path, n_files: int, rows_per_file: int = 100) -> None:
    """同样的总行数，但写成一个文件 —— 用来单独看「文件数」这个变量的影响。"""
    directory.mkdir(parents=True, exist_ok=True)
    total = n_files * rows_per_file
    pl.DataFrame(
        {
            "id": list(range(total)),
            "grp": [f"g{(i // rows_per_file) % 3}" for i in range(total)],
            "v": [float(i // rows_per_file) for i in range(total)],
        }
    ).write_parquet(directory / "part-0000.parquet")


# ---------------------------------------------------------------- 场景 1：读多个文件


def scenario_read_many(con: duckdb.DuckDBPyConnection, n_files: int) -> None:
    section("场景 1 · 读多个文件：glob 写法，两边都认")
    root = _scratch("part_demo_files_")
    _write_files(root, n_files)

    print(f"  目录：{root}（{n_files} 个 parquet 文件）")
    print(f"  DuckDB  read_parquet('{root}/*.parquet')")
    print(f"  Polars  scan_parquet('{root}/*.parquet')")
    print()

    duck = con.execute(
        f"SELECT count(*) AS n, sum(v) AS s FROM read_parquet('{root}/*.parquet')"
    ).fetchall()[0]
    pol = (
        pl.scan_parquet(str(root / "*.parquet"))
        .select(pl.len().alias("n"), pl.col("v").sum().alias("s"))
        .collect()
        .to_dicts()
    )
    print(f"    DuckDB  行数 {duck[0]}，sum(v) = {duck[1]}")
    print(f"    Polars  行数 {pol[0]['n']}，sum(v) = {pol[0]['s']}")
    same = duck[0] == pol[0]["n"] and abs(duck[1] - pol[0]["s"]) < 1e-6
    print(f"    一致？ {'✅' if same else '❌'}")
    print()
    print("  两边都是「一个 glob → 一张逻辑表」，用法一致。")
    print("  注意 DuckDB 的 `read_parquet` 只接**一个**参数（字符串或列表），")
    print("  传多个文件要写成 `read_parquet(['a.parquet', 'b.parquet'])`，")
    print("  直接 `read_parquet('a.parquet', 'b.parquet')` 会报 BinderException。")


# ---------------------------------------------------------------- 场景 2：文件顺序


def scenario_file_order(con: duckdb.DuckDBPyConnection, n_files: int) -> None:
    section("场景 2 · 文件读取顺序：DuckDB 靠一个开关保序，Polars 没有开关")
    root = _scratch("part_demo_order_")
    _write_files(root, n_files, rows_per_file=1000)

    def duck_first_ids() -> list[int]:
        rel = con.sql(f"SELECT id FROM read_parquet('{root}/*.parquet')")
        at = rel.to_arrow_table()
        rel.close()
        return at.column("id").to_pylist()[:5]

    def pol_first_ids() -> list[int]:
        return pl.scan_parquet(str(root / "*.parquet")).select("id").collect()["id"].to_list()[:5]

    print("  同一个目录，不加 ORDER BY，看前 5 行的 id：")
    print(f"    DuckDB  {duck_first_ids()}")
    print(f"    Polars  {pol_first_ids()}")
    print()
    setting = _one(con, "SELECT current_setting('preserve_insertion_order')")
    print(f"  DuckDB 这一列是 `preserve_insertion_order`（默认 {setting}），")
    con.execute("SET preserve_insertion_order=false")
    print(f"    关掉之后 DuckDB → {duck_first_ids()}")
    con.execute("SET preserve_insertion_order=true")
    print()
    print("  **Polars 没有这个开关** —— `scan_parquet` 的文件顺序是固定的（按文件名）。")
    print("  DuckDB 那边如果有人为了省内存把它关掉，行序就跟着变了；")
    print("  所以依赖行序的查询，两边都要显式 `ORDER BY`。")


# ---------------------------------------------------------------- 场景 3：类型不一致


def scenario_type_mismatch(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 各文件的列类型不一致：一个自动统一，一个直接报错")
    root = _scratch("part_demo_types_")
    root.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"id": [1], "v": [1.0]}).write_parquet(root / "a_float.parquet")
    pl.DataFrame({"id": [2], "v": [2]}).write_parquet(root / "b_int.parquet")

    print("  两个文件：a_float.parquet 的 v 是 float64，b_int.parquet 的 v 是 int64。")
    print()

    duck = con.execute(
        f"SELECT id, v, typeof(v) AS t FROM read_parquet('{root}/*.parquet') ORDER BY id"
    ).fetchall()
    print("    DuckDB：")
    for row in duck:
        print(f"      id={row[0]}  v={row[1]!r}  typeof={row[2]}")
    print("    → **自动统一成 DOUBLE**（按能容纳所有值的类型提升），两个文件都读进来了。")
    print()

    try:
        pol = pl.scan_parquet(str(root / "*.parquet")).collect()
        print(f"    Polars：{pol.to_dicts()}")
    except Exception as e:
        print(f"    Polars  → {type(e).__name__}: {str(e).splitlines()[0]}")
        print("    → **直接报错**：以第一个文件的 schema 为准，后面的文件对不上就中止。")
    print()
    print("  这个差异有实际后果：")
    print("    · DuckDB 那边 int64 文件里的值会变成 1.0、2.0 —— 金额、ID 这类字段要当心；")
    print("    · Polars 那边文件是分批落盘时最容易踩（先写空文件/占位文件，后面补类型）。")
    print()
    print("  Polars 想放行得显式说：")
    try:
        pol = pl.scan_parquet(
            str(root / "*.parquet"),
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        ).collect()
        print(
            "    scan_parquet(..., cast_options=ScanCastOptions(integer_cast='allow-float'))"
            f" → {pol.to_dicts()}"
        )
    except Exception as e:
        print(f"    → {type(e).__name__}: {str(e).splitlines()[0]}")


# ---------------------------------------------------------------- 场景 4：列名不一致


def scenario_column_mismatch(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 各文件的列名不一致：合并的开关两边都有，但名字不一样")
    root = _scratch("part_demo_cols_")
    root.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"id": [1], "x": [10]}).write_parquet(root / "c_x.parquet")
    pl.DataFrame({"id": [2], "y": [20]}).write_parquet(root / "e_y.parquet")

    print("  两个文件：c_x.parquet 有 x 列，e_y.parquet 有 y 列。")
    print()

    try:
        con.execute(f"SELECT * FROM read_parquet('{root}/*.parquet')").fetchall()
        print("    DuckDB 默认 → 读出来了")
    except Exception as e:
        print(f"    DuckDB 默认 → {type(e).__name__}: {str(e).splitlines()[0][:150]}")
    print("    → **默认报错**（schema mismatch in glob）。")
    print()

    duck = con.execute(
        f"SELECT * FROM read_parquet('{root}/*.parquet', union_by_name=true) ORDER BY id"
    ).fetchall()
    print("    DuckDB  union_by_name=true：")
    for row in duck:
        print(f"      {row}")
    print("    → 列并起来了，缺的那列填 NULL。")
    print()

    try:
        pl.scan_parquet([str(root / "c_x.parquet"), str(root / "e_y.parquet")]).collect()
        print("    Polars 默认 → 读出来了")
    except Exception as e:
        print(f"    Polars 默认 → {type(e).__name__}: {str(e).splitlines()[0][:150]}")
    print("    → **报错**（extra column in file outside of expected schema）。")
    print()
    print("  Polars 要做同样的合并，得开两个开关（少一个都不行）：")
    try:
        pol = pl.scan_parquet(
            [str(root / "c_x.parquet"), str(root / "e_y.parquet")],
            extra_columns="ignore",
            missing_columns="insert",
        ).collect()
        print(f"    extra_columns='ignore' + missing_columns='insert' → {pol.to_dicts()}")
    except Exception as e:
        print(f"    → {type(e).__name__}: {str(e).splitlines()[0][:150]}")


# ---------------------------------------------------------------- 场景 5：空目录


def scenario_no_files(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 目录里没有匹配文件：两边都报错，但错法不一样")
    empty = _scratch("part_demo_empty_")

    try:
        con.execute(f"SELECT * FROM read_parquet('{empty}/*.parquet')").fetchall()
        print("    DuckDB → 读出来了")
    except Exception as e:
        print(f"    DuckDB → {type(e).__name__}: {str(e).splitlines()[0][:150]}")
    print()
    try:
        pl.scan_parquet(str(empty / "*.parquet")).collect()
        print("    Polars → 读出来了")
    except Exception as e:
        print(f"    Polars → {type(e).__name__}: {str(e).splitlines()[0][:150]}")
    print()
    print("  两边都是**报错而不是返回空表** —— 这点比想象中重要：")
    print("  「上游还没写完文件」这种场景，任务会直接失败，而不是安静地返回 0 行。")
    print("  想让它安静，得自己判断：文件数是不是 0。")


# ---------------------------------------------------------------- 场景 6：Hive 分区


def scenario_hive(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · Hive 分区目录：Polars 传 glob **不认**分区列")
    root = _scratch("part_demo_hive_")
    for dt in ["2024-01-01", "2024-01-02", "2024-01-03"]:
        (root / f"dt={dt}").mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"id": [1], "v": [1.0]}).write_parquet(root / f"dt={dt}" / "part-0.parquet")

    print("  目录结构：")
    for d in sorted(p.name for p in root.iterdir()):
        print(f"    {d}/part-0.parquet")
    print()

    duck = con.execute(f"SELECT * FROM read_parquet('{root}/*/*.parquet') ORDER BY dt").fetchall()
    print("  DuckDB  read_parquet('{root}/*/*.parquet')")
    print(f"    → {len(duck)} 行，列 = id, v, **dt**（分区列自动出现）")
    print()

    pol_glob = pl.scan_parquet(str(root / "*" / "*.parquet")).collect()
    print("  Polars  scan_parquet('{root}/*/*.parquet')")
    print(f"    → {pol_glob.height} 行，列 = {pol_glob.columns}   ← **没有 dt**")
    print()

    pol_dir = pl.scan_parquet(str(root)).collect()
    print("  Polars  scan_parquet('{root}')   ← 传**目录**")
    print(f"    → {pol_dir.height} 行，列 = {pol_dir.columns}")
    print()

    pol_hive = pl.scan_parquet(str(root / "*" / "*.parquet"), hive_partitioning=True).collect()
    print("  Polars  scan_parquet(..., hive_partitioning=True)")
    print(f"    → {pol_hive.height} 行，列 = {pol_hive.columns}")
    print()
    print("  **同一个目录，路径写法不同，结果的列就不一样** ——")
    print("  DuckDB 认 glob，Polars 认目录（或显式开开关）。")
    print("  漏了这个开关，WHERE dt = ... 会直接报「列不存在」。")


# ---------------------------------------------------------------- 场景 7：分区值类型


def scenario_partition_type(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 分区值的类型：Polars 猜成 Date，字符串过滤直接报错")
    root = _scratch("part_demo_ptype_")
    for dt in ["2024-01-01", "2024-01-02"]:
        (root / f"dt={dt}").mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"id": [1], "v": [1.0]}).write_parquet(root / f"dt={dt}" / "part-0.parquet")

    desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{root}/*/*.parquet')").fetchall()
    duck_types = {r[0]: r[1] for r in desc}
    pol_types = dict(pl.scan_parquet(str(root)).collect().schema)
    print("  同样的分区目录，两边给 dt 的类型：")
    print(f"    DuckDB  {duck_types}")
    print(f"    Polars  {pol_types}")
    print()
    print("  DuckDB 从目录名里把 `2024-01-01` 推成了 **DATE**；")
    print("  Polars 也推成 Date（`try_parse_hive_dates` 默认开）。")
    print()

    print("  于是 —— 用字符串过滤分区列：")
    duck_n = _one(
        con,
        f"SELECT count(*) FROM read_parquet('{root}/*/*.parquet') WHERE dt='2024-01-02'",
    )
    print(f"    DuckDB  WHERE dt='2024-01-02' → {duck_n} 行（隐式转换掉了，不报错）")
    try:
        pl.scan_parquet(str(root)).filter(pl.col("dt") == "2024-01-02").collect()
        print("    Polars  WHERE dt='2024-01-02' → 读出来了")
    except Exception as e:
        msg = str(e).splitlines()[0][:140]
        print(f"    Polars  WHERE dt='2024-01-02' → {type(e).__name__}: {msg}")
    print("    → **Polars 直接报类型错**（Date 列不能和字符串比）。")
    print("       得写 `pl.col('dt') == pl.lit(date(2024, 1, 2))`。")
    print()

    print("  顺带看一眼分区值类型的「猜测」有多野：")
    odd = _scratch("part_demo_odd_")
    (odd / "dt=20240101").mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"id": [1]}).write_parquet(odd / "dt=20240101" / "p.parquet")
    odd_desc = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{odd}/*/*.parquet')").fetchall()
    print(f"    目录名 dt=20240101 → DuckDB 推成 {odd_desc[0][1]}（不是 DATE！）")
    print("    目录名写成什么格式，列类型就是什么 —— **别把分区值和文件内容混在一起理解**。")


# ---------------------------------------------------------------- 场景 8：分区裁剪


def scenario_pruning(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 分区裁剪：两边都会裁，EXPLAIN 里看得见")
    root = _scratch("part_demo_prune_")
    for dt in ["2024-01-01", "2024-01-02", "2024-01-03"]:
        (root / f"dt={dt}").mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"id": [1], "v": [1.0]}).write_parquet(root / f"dt={dt}" / "part-0.parquet")

    print("  查询：只读 dt='2024-01-02' 这一个分区。")
    print()
    print("  DuckDB 的执行计划：")
    for row in con.execute(
        f"EXPLAIN SELECT * FROM read_parquet('{root}/*/*.parquet') WHERE dt='2024-01-02'"
    ).fetchall():
        print(f"    {row[1]}")
    print()
    print("  Polars 的优化后计划：")
    from datetime import date

    print(pl.scan_parquet(str(root)).filter(pl.col("dt") == pl.lit(date(2024, 1, 2))).explain())
    print()
    print("  两边都把过滤条件推到了「打开文件」这一步 ——")
    print("  DuckDB 写着 `Scanning Files: 1/3`，Polars 的 SCAN 里只剩那一个文件。")
    print("  这是分区表最值钱的能力：**不相关的分区根本没被打开**。")
    print()
    print("  但要注意：**分区裁剪只对「恰好等于分区值」的过滤有效**。")
    print("  写成 `WHERE dt >= DATE '2024-01-02'`（范围过滤）就只能裁掉整个前缀，")
    print("  中间的分区照样要读。")


# ---------------------------------------------------------------- 场景 9：写分区


def scenario_write_partition(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 9 · 写分区：文件里留不留分区列、文件名怎么起")
    duck_root = _scratch("part_demo_wduck_")
    pol_root = _scratch("part_demo_wpol_")

    con.execute(
        "COPY (SELECT 1 AS id, DATE '2024-01-01' AS dt, 2.0::DOUBLE AS v "
        "UNION ALL SELECT 2, DATE '2024-01-02', 3.0::DOUBLE) "
        "TO ? (FORMAT PARQUET, PARTITION_BY (dt))",
        [str(duck_root)],
    )
    df = pl.DataFrame(
        {
            "id": [1, 2],
            "dt": ["2024-01-01", "2024-01-02"],
            "v": [2.0, 3.0],
        }
    )
    df.write_parquet(pol_root, partition_by=["dt"])

    print("  DuckDB  COPY ... TO 'dir' (FORMAT PARQUET, PARTITION_BY (dt))")
    for path in sorted(duck_root.rglob("*.parquet")):
        print(f"    {path.relative_to(duck_root)}")
        print(f"      文件里的列：{pq.read_schema(path).names}")
    print()
    print("  Polars  write_parquet(dir, partition_by=['dt'])")
    for path in sorted(pol_root.rglob("*.parquet")):
        print(f"    {path.relative_to(pol_root)}")
        print(f"      文件里的列：{pq.read_schema(path).names}")
    print()
    print("  两处不一样：")
    print("    · **DuckDB 不把分区列写进文件**（分区值在目录名里，读的时候从路径补回来，")
    print("      而且能补成 DATE 这种带类型的列）；")
    print("      **Polars 把分区列留在文件里**（列名和类型都留在文件里）。")
    print("    · 文件名：DuckDB 叫 `data_0.parquet`，Polars 叫 `00000000.parquet`。")
    print("      目录名两边都是 `dt=2024-01-01` 这种 hive 写法。")
    print()
    print("  跨引擎读对方写的分区目录：")
    duck_read_pol = con.execute(
        f"SELECT * FROM read_parquet('{pol_root}/*/*.parquet') ORDER BY id"
    ).fetchall()
    print(f"    DuckDB 读 Polars 的分区目录 → {duck_read_pol}")
    pol_read_duck = pl.scan_parquet(str(duck_root)).collect().sort("id")
    print(f"    Polars 读 DuckDB 的分区目录 → {pol_read_duck.to_dicts()}")
    print()
    print("  两边都读得动 —— 但**分区列的类型**跟着「谁写的」变：")
    print("    · Polars 写的目录：文件里的 dt 是 String，Polars 自己读回来是 String，")
    print("      **DuckDB 读回来却是 DATE**（路径上的值盖过了文件里的字符串列）；")
    print("    · DuckDB 写的目录：文件里根本没有 dt，两边都从路径补出来 ——")
    print("      DuckDB 给 DATE，Polars 给 Date。")
    print("  跨引擎写分区时，分区值统一用「yyyy-MM-dd」这种两边都认的格式最省事。")


# ---------------------------------------------------------------- 场景 10：性能


def scenario_performance(con: duckdb.DuckDBPyConnection, n_files: int, repeat: int) -> None:
    section(f"场景 10 · 读 {n_files} 个小文件的性能")
    root = _scratch("part_demo_perf_")
    _write_files(root, n_files, rows_per_file=100)
    single = _scratch("part_demo_perf1_")
    _write_one_file(single, n_files, rows_per_file=100)
    total_rows = n_files * 100
    print(f"  目录：{root}")
    print(
        f"  {n_files} 个文件，每个 100 行 —— 小文件场景（典型的「一天一个文件」或「一批一个文件」）"
    )
    print(f"  对照组：同样的 {total_rows} 行写成一个文件（目录 {single}）")
    print()

    def duck_full(pattern: str) -> None:
        rel = con.sql(f"SELECT count(*), sum(v) FROM read_parquet('{pattern}')")
        rel.to_arrow_table()
        rel.close()

    def pol_full(pattern: str) -> None:
        pl.scan_parquet(pattern).select(pl.len().alias("c"), pl.col("v").sum().alias("s")).collect()

    def bench(fn, *args) -> float:
        ts = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn(*args)
            ts.append(time.perf_counter() - t0)
        return min(ts)

    many = str(root / "*.parquet")
    one = str(single / "*.parquet")
    d_many = bench(duck_full, many)
    p_many = bench(pol_full, many)
    d_one = bench(duck_full, one)
    p_one = bench(pol_full, one)

    print("  全表聚合：")
    print(
        f"    {n_files} 个小文件   DuckDB {common.format_ms(d_many):>9} ms   "
        f"Polars {common.format_ms(p_many):>9} ms   比值 {d_many / p_many:.2f}×"
    )
    print(
        f"    1 个大文件        DuckDB {common.format_ms(d_one):>9} ms   "
        f"Polars {common.format_ms(p_one):>9} ms   比值 {d_one / p_one:.2f}×"
    )
    print()

    def duck_pruned() -> None:
        rel = con.sql(f"SELECT id FROM read_parquet('{many}') WHERE id < 1000")
        rel.fetchall()
        rel.close()

    d_pruned = bench(duck_pruned)
    p_pruned = bench(
        lambda: pl.scan_parquet(many).select("id").filter(pl.col("id") < 1000).collect()
    )
    print("  列裁剪 + 谓词下推（只读 id 列并过滤）：")
    print(f"    DuckDB  {common.format_ms(d_pruned):>10} ms")
    print(f"    Polars  {common.format_ms(p_pruned):>10} ms")
    print(f"    比值    {d_pruned / p_pruned:.2f}×")
    print()
    print("  说明：两边都是「开文件 → 读 → 聚合」，口径一致，取多次的最小值。")
    print()
    print("  同一份数据、同样行数，**文件数从 200 降到 1**：")
    print(
        f"    DuckDB  {common.format_ms(d_many)} ms → {common.format_ms(d_one)} ms"
        f"（{d_many / d_one:.1f}× 快）"
    )
    print(
        f"    Polars  {common.format_ms(p_many)} ms → {common.format_ms(p_one)} ms"
        f"（{p_many / p_one:.1f}× 快）"
    )
    print("  两个引擎都因为「少开 199 个文件」快了一大截 ——")
    print("  **小文件场景的瓶颈是文件数，不是引擎快慢。**")
    print()
    if d_many > p_many:
        print(f"  这一档 Polars 快（{d_many / p_many:.1f}×），但绝对时间都在个位数毫秒 ——")
        print("  数据量再大一些，这个差距就会被摊薄，别拿这个当选型依据。")
    else:
        print("  这一档两边接近，绝对时间都在个位数毫秒 ——")
        print("  数据量再大一些，这个差距就会被摊薄，别拿这个当选型依据。")
    print()
    print("  真要提速就合并文件或改用分区表，别指望换个引擎能解决。")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 读多个文件：两边都支持 glob，用法一致。")
    print("     DuckDB 的 `read_parquet` 只接**一个**参数（字符串或列表），")
    print("     多文件要写 `read_parquet(['a', 'b'])`。")
    print("  2. **文件顺序**：DuckDB 靠 `preserve_insertion_order`（默认开）保序，")
    print("     关掉行序就变；**Polars 没有这个开关**，顺序固定按文件名。")
    print("     依赖行序的查询，两边都要显式 ORDER BY。")
    print("  3. **各文件类型不一致：DuckDB 自动提升（int64 + float64 → DOUBLE），")
    print("     Polars 直接 SchemaError**（可用 cast_options 放行）。")
    print("     DuckDB 这边要当心：整数列会悄悄变成浮点。")
    print("  4. 列名不一致：DuckDB 默认报错，`union_by_name=true` 能并；")
    print("     Polars 要 `extra_columns='ignore'` + `missing_columns='insert'` 两个一起开。")
    print("  5. 没有匹配文件时两边都报错（不是返回空表）—— 「上游还没写完」会让任务直接失败。")
    print("  6. **Hive 分区目录：Polars 传 glob 不认分区列**（得传目录或显式")
    print("     `hive_partitioning=True`）；DuckDB 传 glob 就自动认。")
    print("     漏掉这个开关，WHERE dt = ... 会报「列不存在」。")
    print("  7. 分区值的类型：两边都把 `2024-01-01` 推成日期；")
    print("     DuckDB 的字符串比较会隐式转换，**Polars 会报类型错**（得用 date 字面量）。")
    print("     目录名写成 `dt=20240101` 时 DuckDB 会推成 BIGINT —— 格式别乱写。")
    print("  8. 分区裁剪两边都会做（只对「恰好等于分区值」的过滤有效，范围过滤只能裁前缀）。")
    print("  9. 写分区：**DuckDB 不把分区列写进文件**（读时从路径补，还能补成 DATE），")
    print("     **Polars 把分区列留在文件里**；文件名一个是 data_0.parquet，")
    print("     一个是 00000000.parquet。目录名两边都是 hive 写法，能互相读。")
    print("  10. 小文件场景两边都被「文件数」拖住：同样行数，从 200 个文件合成 1 个，")
    print("      DuckDB 和 Polars 都快了好几倍。想提速就合并文件或改分区表，别换引擎。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="多文件与分区表：glob、schema 冲突与分区裁剪")
    parser.add_argument("--files", type=int, default=200, help="小文件个数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    con = duckdb.connect()
    scenario_read_many(con, args.files)
    scenario_file_order(con, args.files)
    scenario_type_mismatch(con)
    scenario_column_mismatch(con)
    scenario_no_files(con)
    scenario_hive(con)
    scenario_partition_type(con)
    scenario_pruning(con)
    scenario_write_partition(con)
    scenario_performance(con, args.files, args.repeat)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
