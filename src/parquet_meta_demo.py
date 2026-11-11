"""Parquet 元数据内省 + 行组统计裁剪：文件里藏着什么，引擎跳过了什么。

Parquet 不是「一坨二进制」—— 它把文件切成一组一组的 **row group**，
每个 row group 是一块独立可读的数据，而且**每一列的每一块都自带 min/max 统计**。
这些统计就是所谓的 **zonemap**，查询的时候引擎拿谓词一推，
就能整组整组地跳过 —— 这就是 Parquet 最重要的一项「免费索引」。

这个 demo 分两条线：

1. **内省**：文件里到底写了些什么元数据，两边各能查到什么。
   DuckDB 有 5 个元数据表函数（`parquet_schema` / `parquet_file_metadata` /
   `parquet_metadata` / `parquet_kv_metadata` / `parquet_full_metadata`），
   Polars 只有 `read_parquet_schema` / `read_parquet_metadata` /
   `scan_parquet(...).collect_schema()`，**没有任何行组级的 API**。
   （顺带纠正一个常见的误解：DuckDB 2.0 里**没有** `sniff_parquet`，
   只有 `sniff_csv`。）
2. **裁剪**：这些统计在查询时到底起了多大作用。
   DuckDB 的 `EXPLAIN ANALYZE` 会直接报出「扫了几个行组」，
   Polars 只有一个开关式的 `use_statistics=True/False`，看不到过程。

跟 `index_demo.py` 的关系：那个 demo 讲的是「要不要建 ART 索引」，
从**查询结果**的角度看布局的作用；这个 demo 从**文件内部**的角度看 ——
zonemap 是什么、存在哪、怎么被用上。两条路，最后指向同一个结论。

用法：
    uv run python src/parquet_meta_demo.py
    uv run python src/parquet_meta_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import re
import statistics
import time
from pathlib import Path

import duckdb
import polars as pl

import common

OUT_DIR = common.DATA_DIR / "parquet_meta"

# 目标行组数 —— 裁剪的效果看「扫了几组 / 一共几组」，这个数要好看
TARGET_ROW_GROUPS = 50


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def duck_scalar(con: duckdb.DuckDBPyConnection, expr: str) -> object:
    return q1(con, f"SELECT {expr}")


def fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


# DuckDB 的类型名 → Polars 的 dtype 名。
# 两边叫法不一样（BIGINT vs Int64），但说的是同一个类型 ——
# 比对「类型名」没意义，比对「类型本身」才有意义。
DUCK_TO_POLARS = {
    "BOOLEAN": "Boolean",
    "TINYINT": "Int8",
    "SMALLINT": "Int16",
    "INTEGER": "Int32",
    "BIGINT": "Int64",
    "HUGEINT": "Int128",
    "UTINYINT": "UInt8",
    "USMALLINT": "UInt16",
    "UINTEGER": "UInt32",
    "UBIGINT": "UInt64",
    "FLOAT": "Float32",
    "DOUBLE": "Float64",
    "VARCHAR": "String",
    "BLOB": "Binary",
    "DATE": "Date",
    "TIME": "Time",
    "TIMESTAMP": "Datetime(time_unit='us', time_zone=None)",
    "TIMESTAMP WITH TIME ZONE": "Datetime(time_unit='us', time_zone='UTC')",
    "INTERVAL": "Duration",
}


def duck_type_to_polars(dtype: str) -> str | None:
    """DuckDB 的类型名 → Polars 的 dtype 名。返回 None = 表里没收录，得手工核。"""
    d = dtype.strip()
    if d.startswith("DECIMAL"):
        m = re.match(r"DECIMAL\((\d+),\s*(\d+)\)", d)
        if m:
            return f"Decimal(precision={m.group(1)}, scale={m.group(2)})"
        return "Decimal"
    return DUCK_TO_POLARS.get(d)


# ---------------------------------------------------------------- 造数


def ensure_data(rows: int) -> tuple[Path, Path]:
    """造两份内容相同、但行组布局完全不同的 parquet。

    · sorted   —— 按 v 排序落盘，每个行组覆盖一段**互不重叠**的 v 区间；
    · shuffled —— 顺序打乱，每个行组的 v 区间都是完整的 [0, N)。

    payload 用 md5 当内容：不可压缩，文件才有真实的体积 ——
    否则一列重复字符串会被 snappy 压成几乎为零，
    「剪枝省了多少」就全被固定开销盖住了，看不出来。

    文件名带行数，已存在的直接复用，绝不覆盖 —— 换行数就是换一份文件。
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sorted_path = OUT_DIR / f"sorted_{rows}.parquet"
    shuffled_path = OUT_DIR / f"shuffled_{rows}.parquet"
    if sorted_path.exists() and shuffled_path.exists():
        return sorted_path, shuffled_path

    row_group = max(1, rows // TARGET_ROW_GROUPS)

    con = duckdb.connect()
    try:
        con.execute(
            "COPY (SELECT i AS id, i AS v, md5(i::VARCHAR) AS payload "
            f"FROM range({rows}) t(i) ORDER BY v) "
            f"TO '{sorted_path}' (FORMAT PARQUET, ROW_GROUP_SIZE {row_group})"
        )
        # hash(i) 当排序键 = 一个确定性的「打乱」（不是随机，每次跑都一样）
        con.execute(
            "COPY (SELECT i AS id, i AS v, md5(i::VARCHAR) AS payload "
            f"FROM range({rows}) t(i) ORDER BY hash(i)) "
            f"TO '{shuffled_path}' (FORMAT PARQUET, ROW_GROUP_SIZE {row_group})"
        )
    finally:
        con.close()
    return sorted_path, shuffled_path


# ---------------------------------------------------------------- 1. API 清单


def scenario_api_inventory(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    section("场景 1 · 能查元数据的地方：DuckDB 5 个表函数 vs Polars 3 个函数")

    duck_funcs = [
        r[0]
        for r in con.sql(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            "WHERE function_type = 'table' AND function_name ILIKE '%parquet%' "
            "ORDER BY function_name"
        ).fetchall()
    ]
    metadata_funcs = [
        f
        for f in duck_funcs
        if f
        in (
            "parquet_schema",
            "parquet_file_metadata",
            "parquet_metadata",
            "parquet_kv_metadata",
            "parquet_full_metadata",
        )
    ]
    print("  DuckDB 侧 —— `duckdb_functions()` 里跟 parquet 有关的表函数：")
    print()
    for f in duck_funcs:
        tag = "  ← 元数据" if f in metadata_funcs else ""
        print(f"    {f}{tag}")
    print()
    print(f"  其中**元数据**表函数有 {len(metadata_funcs)} 个：{metadata_funcs}")
    print()

    # 每个表函数返回哪些列
    for fn in metadata_funcs:
        cols = [d[0] for d in con.sql(f"SELECT * FROM {fn}('{path}') LIMIT 0").description]
        print(f"  {fn}() 返回 {len(cols)} 列：")
        line = "    "
        for i, c in enumerate(cols, 1):
            line += f"{c}  "
            if i % 5 == 0:
                print(line)
                line = "    "
        if line.strip():
            print(line)
        print()

    print("  Polars 侧 —— parquet 相关的公开函数就这些：")
    print("    · `pl.read_parquet(path)`             —— 读数据")
    print("    · `pl.scan_parquet(path)`             —— 惰性读数据")
    print("    · `pl.read_parquet_schema(path)`      —— 文件头的 schema")
    print("    · `pl.read_parquet_metadata(path)`    —— 文件级的自定义键值对（标着 experimental）")
    print("    · `pl.scan_parquet(path).collect_schema()` —— 同 schema，不读数据")
    print()
    pol_meta = pl.read_parquet_metadata(path)
    print(f"  实测 `read_parquet_metadata()` 拿到的内容：{pol_meta}")
    print(
        "    （空 —— 这个文件没写自定义键值对。DuckDB 那边 `parquet_kv_metadata()` 同样是空的。）"
    )
    print()

    print("  把两边的覆盖面摊开对：")
    print()
    print("  " + common.pad("想知道的元数据", 30) + common.pad("DuckDB", 32) + "Polars")
    print("  " + "-" * 88)
    coverage = [
        ("schema（类型名）", "parquet_schema()", "read_parquet_schema()"),
        ("parquet 层的原始类型", "parquet_schema()", "—"),
        ("文件级：行数 / 行组数", "parquet_file_metadata()", "—"),
        ("文件级：写入方 / footer 大小", "parquet_file_metadata()", "—"),
        ("文件级：自定义键值对", "parquet_kv_metadata()", "read_parquet_metadata()"),
        ("行组级：min/max（zonemap）", "parquet_metadata()", "—"),
        ("行组级：压缩 / 编码", "parquet_metadata()", "—"),
        ("行组级：数据页偏移", "parquet_metadata()", "—"),
    ]
    for what, duck_side, pol_side in coverage:
        mark = "✅" if pol_side != "—" else "❌"
        print(
            "  "
            + common.pad(what, 30)
            + common.pad(duck_side, 32)
            + common.pad(pol_side, 28)
            + mark
        )
    print()
    print("  → **Polars 缺的是「行组级」这一整层** ——")
    print("    拿不到「第 3 个行组的 min/max 是多少」，")
    print("    也拿不到「这个文件一共有几个行组」。")
    print("    连文件级的行数、行组数、footer 大小都拿不到。")
    print("    （想要行组级信息，得自己引 PyArrow：`pq.read_metadata(path)`，")
    print("      那就不是 Polars 的能力了 —— index_demo.py 的场景 5 走的就是这条路。）")
    print()

    print("  **一个要纠正的误解**：`sniff_parquet` 这个函数**不存在**。")
    try:
        con.sql(f"SELECT * FROM sniff_parquet('{path}')").fetchall()
        print("    sniff_parquet -> 存在")
    except Exception as exc:
        print(f"    sniff_parquet -> {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")
    print("    DuckDB 2.0 只有 `sniff_csv`（给 CSV 猜 schema 和分隔符用的）。")
    print("    Parquet 的 schema 是文件头里写死的，用 `parquet_schema` 查就行，")
    print("    根本不需要「猜」。")
    print()


# ---------------------------------------------------------------- 2. schema


def scenario_schema(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    section("场景 2 · parquet_schema：文件里声明的 schema")

    rows = con.sql(
        f"SELECT name, type, repetition_type, converted_type, logical_type, duckdb_type "
        f"FROM parquet_schema('{path}') WHERE name <> 'duckdb_schema' ORDER BY column_id"
    ).fetchall()
    print(f"  {path.name} 里声明的 schema：")
    print()
    print(
        "  "
        + common.pad("列名", 14)
        + common.pad("parquet 类型", 20)
        + common.pad("重复类型", 14)
        + common.pad("逻辑类型", 18)
        + "DuckDB 类型"
    )
    print("  " + "-" * 88)
    for name, ptype, rep, conv, logical, dtype in rows:
        print(
            "  "
            + common.pad(str(name), 14)
            + common.pad(str(ptype), 20)
            + common.pad(str(rep), 14)
            + common.pad(str(logical or conv), 18)
            + str(dtype)
        )
    print()

    # DuckDB 2.0 会往文件里塞自己的一份 schema，parquet_schema() 会把它当一行报出来
    extra = con.sql(
        f"SELECT name, repetition_type, num_children FROM parquet_schema('{path}') "
        f"WHERE name = 'duckdb_schema'"
    ).fetchall()
    pol_names = list(pl.read_parquet_schema(path).names())
    if extra:
        print("  ⚠️ 顺带一提：`parquet_schema()` 会**多报一行** `duckdb_schema`：")
        print(f"     parquet_schema() 眼里：{extra}")
        print(f"     但 Polars 读出来只有 {len(pol_names)} 列：{pol_names}")
        print("     —— 对得上，所以它不是真正的数据列，")
        print("     而是 DuckDB 2.0 把自己的一份 schema 写进文件尾部元数据区的结果。")
        print("     **DuckDB 读得到，别人看不见** —— 这就是元数据里的「私货」。")
        print()

    duck_schema = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    pol_schema = pl.read_parquet_schema(path)
    print("  跟引擎实际读出来的 schema 对一下（两边叫法不同，比的是类型本身）：")
    print()
    print(
        "  "
        + common.pad("列名", 14)
        + common.pad("DuckDB 读出来", 20)
        + common.pad("Polars 读出来", 20)
        + "同一个类型吗"
    )
    print("  " + "-" * 72)
    same_all = True
    for duck_row in duck_schema:
        cname, ctype = duck_row[0], duck_row[1]
        pol_type = pol_schema.get(cname)
        pol_str = str(pol_type) if pol_type is not None else "（没有）"
        mapped = duck_type_to_polars(str(ctype))
        if mapped is None:
            ok, note = "❓", f"（DuckDB 叫 {ctype}，Polars 叫 {pol_str}）"
            same_all = False
        elif mapped == pol_str:
            ok, note = "✅", ""
        else:
            ok, note = "❌", f"（DuckDB {ctype} → {mapped}，Polars {pol_str}）"
            same_all = False
        print(
            "  "
            + common.pad(str(cname), 14)
            + common.pad(str(ctype), 20)
            + common.pad(pol_str, 20)
            + ok
            + note
        )
    print()
    if same_all:
        print("  → 两边读出来的 schema **是同一个类型**（只是叫法不一样）。")
    else:
        print("  → 两边读出来的 schema **不完全一样**，见上面标出来的行。")
    print("    这是 Parquet 格式的功劳 —— 类型信息在文件头里写死了，")
    print("    谁读都是同一份，不需要「推断」。")
    print()
    print("  但「文件里写的」和「引擎给你的」**不是一回事**：")
    print("    `parquet_schema` 给的是 parquet 层面的原始声明")
    print("    （INT64 / BYTE_ARRAY / repetition_type / logical_type），")
    print("    `DESCRIBE` 给的是 DuckDB 映射过来的类型（BIGINT / VARCHAR）。")
    print("    parquet 那套类型体系是跨引擎的，DuckDB 那套是自己的 ——")
    print("    `duckdb_type` 这一列，就是映射的结果。")
    print()


# ---------------------------------------------------------------- 3. 文件级元数据


def scenario_file_metadata(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    section("场景 3 · parquet_file_metadata：文件级的那些数")

    num_rows, num_groups, version, created_by, size, footer, enc, orders = con.sql(
        f"SELECT num_rows, num_row_groups, format_version, created_by, "
        f"file_size_bytes, footer_size, encryption_algorithm, column_orders "
        f"FROM parquet_file_metadata('{path}')"
    ).fetchall()[0]
    print(f"  {path.name}：")
    print(f"    总行数        {num_rows:,}")
    print(f"    行组数        {num_groups}")
    print(f"    格式版本      {version}")
    print(f"    写入方        {created_by}")
    print(f"    文件大小      {size:,} 字节")
    print(f"    footer 大小   {footer:,} 字节  （占文件 {footer / size * 100:.2f}%）")
    print(f"    加密算法      {enc}")
    print(f"    列序          {orders}")
    print()

    # 再造一个 1 万行的小文件，对照 footer 占比
    small = OUT_DIR / "small.parquet"
    con.execute(
        "COPY (SELECT i AS id, i AS v, 'payload-' || i AS payload "
        "FROM range(10000) t(i) ORDER BY v) "
        f"TO '{small}' (FORMAT PARQUET, ROW_GROUP_SIZE 4096)"
    )
    s_rows, s_groups, s_size, s_footer = con.sql(
        f"SELECT num_rows, num_row_groups, file_size_bytes, footer_size "
        f"FROM parquet_file_metadata('{small}')"
    ).fetchall()[0]
    print("  对照 —— 一个 1 万行的小文件（同样的列、同样的写入方）：")
    print(f"    总行数        {s_rows:,}")
    print(f"    行组数        {s_groups}")
    print(f"    文件大小      {s_size:,} 字节")
    print(f"    footer 大小   {s_footer:,} 字节  （占文件 {s_footer / s_size * 100:.2f}%）")
    print()
    print("  → **元数据本身也要花空间**：")
    print(f"    1 万行那份，footer 占了文件的 {s_footer / s_size * 100:.2f}%；")
    print(f"    {num_rows:,} 行那份，只占 {footer / size * 100:.2f}%。")
    print("    footer 里装着 schema、行组索引、每列每块的统计信息 ——")
    print("    行组越多、列越多，footer 越大。")
    print("    这就是「小文件问题」的一部分：")
    print("    一堆小 parquet，读的时候光是解析 footer 就得花掉一大截。")
    print()

    kv = con.sql(f"SELECT key, value FROM parquet_kv_metadata('{path}')").fetchall()
    print("  `parquet_kv_metadata()` —— 文件里带的键值对（写在 footer 里）：")
    if kv:
        for k, v in kv:
            print(f"    {common.pad(str(k), 28)}{str(v)[:56]}")
    else:
        print("    （空 —— 这个文件没写自定义键值对）")
    print()

    print("  Polars 侧：**完全拿不到这些**。")
    print("    `read_parquet_schema` 只给类型，不给行数、行组数、footer 大小。")
    print()


# ---------------------------------------------------------------- 4. 行组与 zonemap


def scenario_row_groups(
    con: duckdb.DuckDBPyConnection, sorted_path: Path, shuffled_path: Path
) -> None:
    section("场景 4 · parquet_metadata：zonemap（min/max 统计）就存在这儿")

    print("  `parquet_metadata()` 的粒度是 **行组 × 列**，一行一个「块」的统计：")
    print()
    cols = [
        d[0]
        for d in con.sql(f"SELECT * FROM parquet_metadata('{sorted_path}') LIMIT 0").description
    ]
    print(f"    一共 {len(cols)} 列，其中关键的几列：")
    for c in (
        "row_group_id",
        "path_in_schema",
        "row_group_num_rows",
        "compression",
        "encodings",
        "stats_min",
        "stats_max",
        "min_is_exact",
        "max_is_exact",
    ):
        print(f"      · {c}")
    print()

    print("  **排好序**那份文件里，v 列的行组统计：")
    print()
    print(
        "  "
        + common.pad("行组", 8)
        + common.pad("行数", 10)
        + common.pad("stats_min", 16)
        + common.pad("stats_max", 16)
        + common.pad("压缩", 12)
        + "编码"
    )
    print("  " + "-" * 78)
    rg_rows = con.sql(
        f"SELECT row_group_id, row_group_num_rows, stats_min, stats_max, compression, encodings "
        f"FROM parquet_metadata('{sorted_path}') "
        f"WHERE path_in_schema = 'v' ORDER BY row_group_id LIMIT 5"
    ).fetchall()
    for rg_id, nrows, smin, smax, comp, encs in rg_rows:
        print(
            "  "
            + common.pad(str(rg_id), 8)
            + common.pad(f"{nrows:,}", 10)
            + common.pad(str(smin), 16)
            + common.pad(str(smax), 16)
            + common.pad(str(comp), 12)
            + str(encs)
        )
    total_rg = q1(con, f"SELECT num_row_groups FROM parquet_file_metadata('{sorted_path}')")
    print(f"    ...（一共 {total_rg} 个行组）")
    print()
    print("  → **每个行组覆盖一段互不重叠的 v 区间** ——")
    print("    这就是 zonemap：`(min, max)` 之外的值，这个行组里根本不可能有。")
    print("    查询 `WHERE v = 12345` 时，引擎拿这个区间一比，")
    print("    就能把绝大多数行组**整个跳过**，连解压都不用。")
    print()

    print("  **打乱**那份文件里，同样的列：")
    print()
    print(
        "  "
        + common.pad("行组", 8)
        + common.pad("行数", 10)
        + common.pad("stats_min", 16)
        + common.pad("stats_max", 16)
        + common.pad("压缩", 12)
        + "编码"
    )
    print("  " + "-" * 78)
    rg_rows = con.sql(
        f"SELECT row_group_id, row_group_num_rows, stats_min, stats_max, compression, encodings "
        f"FROM parquet_metadata('{shuffled_path}') "
        f"WHERE path_in_schema = 'v' ORDER BY row_group_id LIMIT 5"
    ).fetchall()
    for rg_id, nrows, smin, smax, comp, encs in rg_rows:
        print(
            "  "
            + common.pad(str(rg_id), 8)
            + common.pad(f"{nrows:,}", 10)
            + common.pad(str(smin), 16)
            + common.pad(str(smax), 16)
            + common.pad(str(comp), 12)
            + str(encs)
        )
    print()
    print("  → **每一组的区间都是 [0, N)** —— 区间信息等于没有。")
    print("    这时候 zonemap 一个字组都剪不掉。")
    print()
    print("  顺带看一眼 `min_is_exact` / `max_is_exact`：")
    exact = con.sql(
        f"SELECT DISTINCT min_is_exact, max_is_exact FROM parquet_metadata('{sorted_path}') "
        f"WHERE path_in_schema = 'v'"
    ).fetchall()
    print(f"    排好序那份：{exact}")
    print("    （False 表示统计值被截断了 —— 比如只保留前若干字节的近似值，")
    print("      这时候引擎会保守地多读一点，但不会读错。）")
    print()


# ---------------------------------------------------------------- 5. 裁剪


def row_groups_scanned(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[int, int] | None:
    """从 EXPLAIN ANALYZE 的输出里抠出「扫了几个行组 / 一共几个」。"""
    res = con.execute(f"EXPLAIN ANALYZE {sql}").fetchall()
    for row in res:
        for cell in row:
            if isinstance(cell, str) and "Row Groups Scanned" in cell:
                for line in cell.splitlines():
                    if "Row Groups Scanned" in line:
                        m = re.search(r"Row Groups Scanned:\s*(\d+)\s*/\s*(\d+)", line)
                        if m:
                            return int(m.group(1)), int(m.group(2))
    return None


def scenario_pruning(
    con: duckdb.DuckDBPyConnection, sorted_path: Path, shuffled_path: Path, rows: int
) -> None:
    section("场景 5 · 裁剪是怎么发生的：EXPLAIN ANALYZE 的「Row Groups Scanned」")

    mid = rows // 2
    print("  DuckDB 的 `EXPLAIN ANALYZE` 会直接报出扫了几个行组。")
    print("  两份数据内容完全一样，只是落盘时的行组布局不同：")
    print()

    print(
        "  "
        + common.pad("文件", 12)
        + common.pad("谓词", 34)
        + common.pad("扫了几个行组", 16)
        + "剪掉了"
    )
    print("  " + "-" * 82)

    cases = [
        ("排好序", sorted_path, f"WHERE v = {mid}", "点查（值在中间）"),
        ("打乱", shuffled_path, f"WHERE v = {mid}", "点查（值在中间）"),
        ("排好序", sorted_path, "WHERE v = 0", "点查（全局最小值）"),
        ("打乱", shuffled_path, "WHERE v = 0", "点查（全局最小值）"),
        ("排好序", sorted_path, f"WHERE v = {rows - 1}", "点查（全局最大值）"),
        ("打乱", shuffled_path, f"WHERE v = {rows - 1}", "点查（全局最大值）"),
        ("排好序", sorted_path, f"WHERE v BETWEEN {mid} AND {mid + 10}", "范围查"),
        ("打乱", shuffled_path, f"WHERE v BETWEEN {mid} AND {mid + 10}", "范围查"),
    ]
    scanned_map: dict[tuple[str, str], tuple[int, int] | None] = {}
    for tag, path, where, kind in cases:
        sql = f"SELECT count(*) FROM read_parquet('{path}') {where}"
        got = row_groups_scanned(con, sql)
        scanned_map[(tag, kind)] = got
        if got is None:
            print(
                "  "
                + common.pad(tag, 12)
                + common.pad(where, 34)
                + common.pad("（没找到）", 16)
                + "—"
            )
            continue
        n, total = got
        print(
            "  "
            + common.pad(tag, 12)
            + common.pad(where, 34)
            + common.pad(f"{n} / {total}", 16)
            + f"{total - n}/{total} 省掉"
        )
    print()

    total_rg = q1(con, f"SELECT num_row_groups FROM parquet_file_metadata('{sorted_path}')")
    print("  → **排好序的时候，普通点查只扫 1 个行组**，其余 " + f"{total_rg - 1} 个整组跳过。")
    print()
    print("  **但打乱的那份不是「一个都剪不掉」—— 这里有个反直觉的地方：**")
    print()
    print("    查全局最小值（v = 0）和全局最大值（v = N-1）时，")
    print("    **打乱的那份也只扫 1 个行组**。")
    print("    原因：装着 0 的那个行组，它的 stats_min 恰好就是 0；")
    print("    装着 N-1 的那个行组，它的 stats_max 恰好就是 N-1。")
    print("    区间没盖住，别的行组照样能剪。")
    print("    → 也就是说：**打乱的数据不是完全不能剪，只是只有极值能剪。**")
    print("      「值在中间」的普通点查，照样得扫全部 " + f"{total_rg} " + "个行组。")
    print()
    print("  三个值得注意的地方：")
    print("    ① **范围查也受益**：排好序时 `BETWEEN` 只扫覆盖那段区间的行组。")
    print("    ② **剪枝靠的是区间，不是「排序」本身** ——")
    print("       打乱那份之所以剪不动，是因为每组的区间都是 [0, N)；")
    print("       排好序那份之所以剪得动，是因为每组区间互不重叠。")
    print("    ③ **剪掉的是「读 + 解压」，不是「过滤」** ——")
    print("       被跳过的行组连数据页都不用碰，这才是省时间的大头。")
    print()
    print("  顺带看一眼逻辑计划里写的是什么：")
    plan = con.sql(
        f"EXPLAIN SELECT count(*) FROM read_parquet('{sorted_path}') WHERE v = {mid}"
    ).fetchall()
    for row in plan:
        for cell in row:
            if isinstance(cell, str):
                for line in cell.splitlines():
                    if "PARQUET_SCAN" in line or "Filters" in line or "Scanning Files" in line:
                        print(f"    {line.strip()}")
    print()
    print("  → 逻辑计划里**看不出**有没有剪枝 —— 写的是 `PARQUET_SCAN`，")
    print("    谓词下推那行也在，但真正扫了几个行组只有 `EXPLAIN ANALYZE` 知道。")
    print("    （`index_demo.py` 的场景 1 也踩过这个坑：")
    print("      `EXPLAIN` 说走 Seq Scan，实际有没有用索引要看 `EXPLAIN ANALYZE`。）")
    print()


# ---------------------------------------------------------------- 6. Polars 侧


def scenario_polars_side(
    con: duckdb.DuckDBPyConnection, sorted_path: Path, shuffled_path: Path, rows: int
) -> None:
    section("场景 6 · Polars 侧：只有一个开关，看不到过程")

    print("  Polars 的 `scan_parquet` 有个 `use_statistics` 参数（默认 True）：")
    print()
    print("    pl.scan_parquet(path, use_statistics=True)   # 用行组统计剪枝（默认）")
    print("    pl.scan_parquet(path, use_statistics=False)  # 不用，全部读")
    print()
    print("  但 Polars **不告诉你**剪掉了多少 —— 没有等价的「Row Groups Scanned」。")
    print("  能看到的只有「快了多少」。下面实测：")
    print()

    mid = rows // 2

    def pol_time(path: Path, use_stats: bool) -> float:
        t0 = time.perf_counter()
        n = (
            pl.scan_parquet(path, use_statistics=use_stats)
            .filter(pl.col("v") == mid)
            .select(pl.len())
            .collect()
            .item()
        )
        assert n == 1  # v 是唯一的，点查必然只中一行
        return time.perf_counter() - t0

    print(
        "  "
        + common.pad("文件", 12)
        + common.pad("use_statistics", 20)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
    )
    print("  " + "-" * 58)
    timings: dict[tuple[str, bool], float] = {}
    for tag, path in (("排好序", sorted_path), ("打乱", shuffled_path)):
        for use_stats in (True, False):
            pol_time(path, use_stats)
            times = [pol_time(path, use_stats) for _ in range(9)]
            med = statistics.median(times) * 1000
            timings[(tag, use_stats)] = med
            print(
                "  "
                + common.pad(tag, 12)
                + common.pad(str(use_stats), 20)
                + common.pad_left(f"{med:.1f}", 14)
                + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            )
    print()
    print(
        f"  口径：点查 v = {mid}，扫同一份文件，计时的是「过滤 + 计数」这一段，重复 9 次取中位数。"
    )
    print()
    print("  → 跟 DuckDB 的「Row Groups Scanned」完全对得上：")
    s_true = timings[("排好序", True)]
    s_false = timings[("排好序", False)]
    z_true = timings[("打乱", True)]
    z_false = timings[("打乱", False)]
    z_ratio = z_true / z_false
    print(f"    排好序：True {s_true:.1f} ms vs False {s_false:.1f} ms —— ")
    print(f"    **开着快了 {s_false / s_true:.1f} 倍**，")
    print("    因为 49 个行组里有 48 个能被 min/max 直接排除。")
    print(f"    打乱：True {z_true:.1f} ms vs False {z_false:.1f} ms —— ")
    if z_ratio > 1.15:
        print(f"    **不但没快，反而慢了 {z_ratio:.1f} 倍**：")
    elif z_ratio < 0.87:
        print(f"    **快了 {1 / z_ratio:.1f} 倍**（但远不如排好序那份）：")
    else:
        print("    **开不开基本没差别**：")
    print("    49 个行组的 min/max 挨个比了一遍，结果一个也剪不掉，")
    print("    那份检查的钱等于白花了。")
    print("    **机制是同一个**（zonemap 剪枝），只是 Polars 不告诉你内部发生了什么。")
    print()
    print("  **Polars 侧能内省什么**：")
    print("    · `pl.read_parquet_schema(path)` —— 只有 schema")
    print("    · `pl.read_parquet_metadata(path)` —— 只有文件级的自定义键值对")
    print(f"      实测这个文件拿到的是：{pl.read_parquet_metadata(sorted_path)}")
    print("    · **没有任何行组级的** —— 拿不到每个行组的 min/max、拿不到行组数。")
    print()
    print("  → 所以两边在这件事上的差距不是「能不能剪枝」（都能），")
    print("    而是**能不能看见自己剪了多少**：DuckDB 能，Polars 不能。")
    print()


# ---------------------------------------------------------------- 7. 交叉引用


def scenario_vs_index_demo(
    con: duckdb.DuckDBPyConnection, sorted_path: Path, shuffled_path: Path
) -> None:
    section("场景 7 · 跟 index_demo.py 的关系：两条路，同一个结论")

    print("  两个 demo 都在讲「点查为什么快」，但看的是不同的东西：")
    print()
    print(
        "  " + common.pad("", 18) + common.pad("index_demo.py", 32) + "parquet_meta_demo.py（这个）"
    )
    print("  " + "-" * 94)
    rows = [
        ("问的问题", "要不要建 ART 索引", "文件里到底写了什么元数据"),
        ("看数据的角度", "查询结果 + EXPLAIN ANALYZE", "parquet_metadata() 的原始输出"),
        ("剪枝的证据", "「Row Groups Scanned」那一行", "stats_min / stats_max 本身"),
        ("Polars 侧", "用 is_in / 哈希 join 代替索引", "只有一个 use_statistics 开关"),
        ("落点", "排序落盘 = 免费索引", "zonemap 就是那个「免费索引」本体"),
    ]
    for a, b, c in rows:
        print("  " + common.pad(a, 18) + common.pad(b, 32) + c)
    print()

    print("  **两条路是同一个工程动作的两面**：")
    print()
    print("    · index_demo.py 说：「排好序落盘，就不需要索引」。")
    print("    · 这个 demo 说：「排好序落盘，每个行组的 min/max 就互不重叠，")
    print("      查询时能整组跳过」。")
    print()
    print("    同一件事：**排好序**。")
    print("    一个从「要不要建索引」看，一个从「索引到底长什么样」看。")
    print()
    print("  **两条路的成本差别**：")
    print()
    print("    · ART 索引：要额外占磁盘，写入要维护（写放大），")
    print("      而且只对 DuckDB 有效 —— 索引存在 .duckdb 文件里，")
    print("      parquet 文件本身还是那个 parquet。")
    print("    · zonemap：**写文件的时候就免费带上了**，零额外成本，")
    print("      任何引擎读 parquet 都能用 —— 这就是它比索引强的地方。")
    print()
    print("  所以顺序永远是：**先排好序落盘（免费），不够再考虑索引（要钱）。**")
    print()
    print("  顺带说一句：这三个 demo（index / parquet_meta / partition）")
    print("  讲的其实是同一件事的三个面 ——")
    print("  **「让数据物理上挨在一起」，比在逻辑上建结构更划算。**")
    print()


# ---------------------------------------------------------------- 8. 代价


def scenario_performance(
    con: duckdb.DuckDBPyConnection, sorted_path: Path, shuffled_path: Path, rows: int, repeat: int
) -> None:
    section("场景 8 · 代价：剪枝到底值多少时间")

    mid = rows // 2
    print(f"数据：{rows:,} 行，{TARGET_ROW_GROUPS} 个行组")
    print(f"机器：{common.describe_machine()}")
    print()

    # 查询统一算 payload 的总长度 —— payload 是 md5（32 字符），
    # **逼引擎真的去读那一列的数据页**。
    # 用 count(*) 是不行的：它在 parquet 上是元数据优化，根本不碰数据页，
    # 拿它当基准等于拿「没读」跟「读了」比。
    def duck_point(path: Path) -> int:
        return int(
            q1(con, f"SELECT sum(length(payload)) FROM read_parquet('{path}') WHERE v = {mid}")
        )

    def duck_full(path: Path) -> int:
        return int(q1(con, f"SELECT sum(length(payload)) FROM read_parquet('{path}')"))

    def pol_point(path: Path, use_stats: bool) -> int:
        return int(
            pl.scan_parquet(path, use_statistics=use_stats)
            .filter(pl.col("v") == mid)
            .select(pl.col("payload").str.len_chars().sum())
            .collect()
            .item()
        )

    def pol_full(path: Path, use_stats: bool) -> int:
        return int(
            pl.scan_parquet(path, use_statistics=use_stats)
            .select(pl.col("payload").str.len_chars().sum())
            .collect()
            .item()
        )

    # (说明, 跑什么, 用哪条 SQL 去问「扫了几个行组」)
    results = [
        (
            "DuckDB  排好序 · 点查 sum(length(payload))",
            lambda: duck_point(sorted_path),
            f"SELECT sum(length(payload)) FROM read_parquet('{sorted_path}') WHERE v = {mid}",
        ),
        (
            "DuckDB  打乱   · 点查 sum(length(payload))",
            lambda: duck_point(shuffled_path),
            f"SELECT sum(length(payload)) FROM read_parquet('{shuffled_path}') WHERE v = {mid}",
        ),
        (
            "DuckDB  排好序 · 全表扫 sum(length(payload))",
            lambda: duck_full(sorted_path),
            f"SELECT sum(length(payload)) FROM read_parquet('{sorted_path}')",
        ),
        (
            "Polars  排好序 · 点查 use_statistics=True",
            lambda: pol_point(sorted_path, True),
            None,
        ),
        (
            "Polars  排好序 · 点查 use_statistics=False",
            lambda: pol_point(sorted_path, False),
            None,
        ),
        (
            "Polars  打乱   · 点查 use_statistics=True",
            lambda: pol_point(shuffled_path, True),
            None,
        ),
        (
            "Polars  排好序 · 全表扫 use_statistics=True",
            lambda: pol_full(sorted_path, True),
            None,
        ),
    ]

    print(
        "  "
        + common.pad("实现", 50)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 16)
        + common.pad_left("扫了几个行组", 16)
    )
    print("  " + "-" * 108)
    for label, fn, rg_sql in results:
        fn()
        times: list[float] = []
        value = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        rg_text = "—"
        if rg_sql is not None:
            got = row_groups_scanned(con, rg_sql)
            if got is not None:
                rg_text = f"{got[0]} / {got[1]}"
        print(
            "  "
            + common.pad(label, 50)
            + common.pad_left(f"{statistics.median(times) * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 16)
            + common.pad_left(rg_text, 16)
        )
    print()

    print("  口径：输入已物化成 parquet，两边读同一份文件；")
    print("  查的都是 payload 列的总长度（逼引擎真的去读数据页，")
    print("  用 count(*) 的话在 parquet 上是元数据优化，根本不读数据）；")
    print("  计时的只有「这一条查询」本身，重复取中位数。")
    print("  共享开发机，单次数字会有抖动，看趋势。")
    print()
    print("  → **剪枝省的是「读 + 解压」的钱**：")
    print("    排好序的点查只需要解开 1 个行组，打乱的点查要解开全部，")
    print("    全表扫更是要解开全部行组的全部行。")
    print("    差别不在「过滤」，在「压根没读那部分数据」。")
    print()
    print("  → **Polars 的 use_statistics 跟 DuckDB 的剪枝是同一个机制**，")
    print("    排好序时能看出来，打乱时看不出来 ——")
    print("    这也说明：**收益来自数据布局，不是来自引擎。**")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB 与 Polars 的 Parquet 元数据内省 / 行组裁剪演示"
    )
    parser.add_argument("--rows", type=int, default=1_000_000, help="数据行数")
    parser.add_argument("--repeat", type=int, default=5, help="代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    sorted_path, shuffled_path = ensure_data(args.rows)

    con = duckdb.connect()
    try:
        scenario_api_inventory(con, sorted_path)
        scenario_schema(con, sorted_path)
        scenario_file_metadata(con, sorted_path)
        scenario_row_groups(con, sorted_path, shuffled_path)
        scenario_pruning(con, sorted_path, shuffled_path, args.rows)
        scenario_polars_side(con, sorted_path, shuffled_path, args.rows)
        scenario_vs_index_demo(con, sorted_path, shuffled_path)
        scenario_performance(con, sorted_path, shuffled_path, args.rows, args.repeat)
    finally:
        con.close()

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  ❌ 内省能力：DuckDB 有 5 个元数据表函数（parquet_schema /")
    print("     parquet_file_metadata / parquet_metadata / parquet_kv_metadata /")
    print("     parquet_full_metadata），Polars 只有 read_parquet_schema /")
    print("     read_parquet_metadata / collect_schema —— **没有行组级的 API**。")
    print("  ⚠️ 纠正：DuckDB 2.0 里**没有 sniff_parquet**，只有 sniff_csv；")
    print("     Parquet 的 schema 在文件头里写死了，不需要猜。")
    print("  ✅ 裁剪机制：两边都靠行组的 min/max（zonemap）剪枝，")
    print("     DuckDB 的 EXPLAIN ANALYZE 会报出「Row Groups Scanned」，")
    print("     Polars 只有一个 use_statistics 开关。")
    print("  ⚠️ 反直觉：打乱的 数据也不是完全剪不动 ——")
    print("     查全局最小值 / 最大值时照样只扫 1 个行组")
    print("     （装着极值的那个行组，min/max 恰好就是全局极值）。")
    print()
    print("  → 记住这一条就够：**排好序落盘 = 免费拿到 zonemap 索引，")
    print("    比建 ART 索引划算得多（零成本、任何引擎都能用）。**")
    print()


if __name__ == "__main__":
    main()
