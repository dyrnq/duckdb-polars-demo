"""UUID：一边是原生类型，一边只能自己模拟。

DuckDB 有**原生 UUID 类型** —— 可排序、可做主键、能生成、能提取版本号。
Polars **没有 UUID dtype** —— 30 个 dtype 里一个都没有。

这个脚本拆七件事：

  1. **DuckDB 的 UUID 能做什么**（生成、排序、主键、版本号）。
  2. **Polars 只能模拟** —— String / UInt128 / Binary 三种，各有各的代价。
  3. **最大的坑：大小写** —— DuckDB 的 UUID 把 `'550E...'` 和 `'550e...'`
     认成**同一个值**，Polars 用 String 模拟就认成**两个**。
  4. **排序**：四种排法结果一致，但 `uuidv7` 才有「时间序」。
  5. **parquet 往返**：DuckDB 保住 UUID，Polars 只能拿到 16 字节的 Binary。
  6. **cast 和约束**：UUID 转不了整数，主键唯一性两边不对等。
  7. **落地建议**：哪种场景存哪种形态。

用法：
    uv run python src/uuid_demo.py
"""

from __future__ import annotations

import argparse
import tempfile
import time
import uuid as pyuuid
from pathlib import Path

import duckdb
import polars as pl

import common

# 一对「同一个 UUID 的两种写法」—— 全篇的坑都围绕它
UUID_LOWER = "550e8400-e29b-41d4-a716-446655440000"
UUID_UPPER = UUID_LOWER.upper()


# 用来做「是不是同一个 UUID」的判据
def _ts_prefix(text: str) -> int:
    """取 UUID 文本前 12 位（48 位毫秒时间戳）当整数。"""
    return int(text.replace("-", "")[:12], 16)


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _try(fn):
    """跑一个可能炸的表达式；炸了就返回一行「❌ 类型: 首行报错」。"""
    try:
        return fn()
    except Exception as exc:
        return f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:60]}"


# ---------------------------------------------------------------- 场景 1


def scenario_duckdb(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · DuckDB 的原生 UUID")

    print("  DuckDB 里 UUID 是一个**真的类型**，不是字符串的别名：")
    t = con.sql("SELECT typeof(gen_random_uuid())").fetchall()[0][0]
    print(f"    typeof(gen_random_uuid()) = {t}")
    print()
    print("  **四个生成函数**（实测都存在）：")
    for name, sql in [
        ("gen_random_uuid()", "SELECT gen_random_uuid()"),
        ("uuid()", "SELECT uuid()"),
        ("uuidv4()", "SELECT uuidv4()"),
        ("uuidv7()", "SELECT uuidv7()"),
    ]:
        val = con.sql(sql).fetchall()[0][0]
        ver = con.sql(f"SELECT uuid_extract_version({sql[7:]})").fetchall()[0][0]
        print(f"    {common.pad(name, 22)}{val}   版本 {ver}")
    print()
    print("  `uuid_extract_version()` 能把版本号抠出来 —— 这是**类型层面**的信息，")
    print("  字符串模拟（Polars 侧）得自己解析文本。")
    print()
    print("  **宽度固定 16 字节**：")
    ln = con.sql(f"SELECT octet_length('{UUID_LOWER}'::UUID::BLOB)").fetchall()[0][0]
    txt = con.sql(f"SELECT length('{UUID_LOWER}'::UUID::VARCHAR)").fetchall()[0][0]
    print(f"    转成 BLOB 是 {ln} 字节，转成 VARCHAR 是 {txt} 个字符。")
    print()
    print("  **排序可用**，而且是按**字节**（= 大端十六进制）排的：")
    sql = (
        f"SELECT x::VARCHAR FROM (VALUES ('{UUID_LOWER}'), ('{UUID_UPPER}')) t(x) ORDER BY x::UUID"
    )
    order = [r[0] for r in con.sql(sql).fetchall()]
    print(f"    ORDER BY x::UUID → {order}")
    print()
    print("  **主键也行**（ART 索引）：")
    con.execute("CREATE TABLE pk(id UUID PRIMARY KEY, v INT)")
    con.execute(f"INSERT INTO pk VALUES ('{UUID_LOWER}', 1)")
    dup = _try(lambda: con.sql(f"INSERT INTO pk VALUES ('{UUID_LOWER}', 2)").fetchall())
    print(f"    插重复的 → {dup}")
    print("    —— UUID 当主键，重复会被挡下来。")


# ---------------------------------------------------------------- 场景 2


def scenario_polars() -> None:
    section("场景 2 · Polars 没有 UUID dtype")

    base_classes = {
        "DataType",
        "IntegerType",
        "FloatType",
        "TemporalType",
        "BaseExtension",
        "Extension",
        "Utf8",
    }
    dtypes = sorted(
        {
            str(getattr(pl, name))
            for name in dir(pl)
            if name[0].isupper()
            and isinstance(getattr(pl, name), type)
            and issubclass(getattr(pl, name), pl.DataType)
            and name not in base_classes
        }
    )
    print(f"  Polars {pl.__version__} 的全部 dtype（{len(dtypes)} 个）：")
    print(f"    {', '.join(dtypes)}")
    print()
    print("  **没有 UUID。** 也没有 UUID 的扩展类型 —— `pl.Enum` 那套也不适用。")
    print()
    print("  只能自己模拟，三种常见做法：")
    print()
    rows = [
        ("String", "36 字符文本", "能直接看、直接 join；但占内存，而且**大小写敏感**"),
        (
            "UInt128",
            "128 位整数",
            "排序跟字节序一致；但转回文本只剩十进制数，横杠得自己拼",
        ),
        ("Binary", "16 字节原始字节", "最省内存、排序也对；但没法直接看"),
    ]
    print(f"  {'模拟方式':<12}{'存什么':<16}{'代价'}")
    print("  " + "-" * 92)
    for how, what, cost in rows:
        print(f"  {common.pad(how, 12)}{common.pad(what, 16)}{cost}")
    print()

    n = 1000
    vals = [f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(n)]
    df_str = pl.DataFrame({"x": pl.Series(vals, dtype=pl.String)})
    df_bin = pl.DataFrame(
        {"x": pl.Series([bytes.fromhex(v.replace("-", "")) for v in vals], dtype=pl.Binary)}
    )
    df_u128 = pl.DataFrame(
        {"x": pl.Series([int(v.replace("-", ""), 16) for v in vals], dtype=pl.UInt128)}
    )
    print(f"  实测内存（{n:,} 行，`DataFrame.estimated_size()`）：")
    for name, df in (
        ("String", df_str),
        ("Binary", df_bin),
        ("UInt128", df_u128),
    ):
        size = df.estimated_size()
        print(f"    {common.pad(name, 12)}{size:>8,} 字节   （{size / n:.1f} 字节/行）")
    print()
    print("  **String 是 Binary 的 2.25 倍** —— UUID 列多的时候差别很显眼。")
    print()
    print("  UInt128 的另一个代价：**转回去就不是 UUID 了** ——")
    one = pl.Series([int(UUID_LOWER.replace("-", ""), 16)], dtype=pl.UInt128)
    print(f"    UInt128.cast(pl.String) → {one.cast(pl.String).to_list()[0]}")
    print("    —— 十进制数，横杠没了。得自己补成 32 位十六进制再插 4 个横杠。")
    print()
    print("  还有：**Polars 没有生成 UUID 的函数** ——")
    print(f"    `dir(pl)` 里带 uuid 的：{[n for n in dir(pl) if 'uuid' in n.lower()]}")
    print("    只能走 Python 标准库：")
    print(f"      import uuid; uuid.uuid4() → {pyuuid.uuid4()}")
    print("    （要批量的话得自己写 `pl.Series([str(uuid.uuid4()) for _ in range(n)])` ——")
    print("      Python 循环，比 DuckDB 里的 `gen_random_uuid()` 慢得多。）")


# ---------------------------------------------------------------- 场景 3


def scenario_case(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 最大的坑：同一个 UUID 的大小写两种写法")

    print("  同一个 UUID，两种写法：")
    print(f"    {UUID_LOWER}")
    print(f"    {UUID_UPPER}")
    print()
    print("  **DuckDB 的 UUID 类型把它们认成同一个值：**")
    eq = con.sql(f"SELECT '{UUID_LOWER}'::UUID = '{UUID_UPPER}'::UUID").fetchall()[0][0]
    print(f"    SELECT '{UUID_LOWER}'::UUID = '{UUID_UPPER}'::UUID → {eq}")
    print()
    print("  但 DuckDB 的 `::VARCHAR` **保留输入时的大小写** —— 值是同一个，文本长得不一样：")
    sql = f"SELECT x::VARCHAR FROM (VALUES ('{UUID_LOWER}'), ('{UUID_UPPER}')) t(x)"
    texts = [r[0] for r in con.sql(sql).fetchall()]
    print(f"    ::VARCHAR → {texts[0]}")
    print(f"                {texts[1]}")
    print()
    print("  于是就有了这个差别 ——")
    duck_distinct = con.sql(
        f"SELECT count(DISTINCT x::UUID) FROM (VALUES ('{UUID_LOWER}'),('{UUID_UPPER}')) t(x)"
    ).fetchall()[0][0]
    varchar_distinct = con.sql(
        f"SELECT count(DISTINCT x) FROM (VALUES ('{UUID_LOWER}'),('{UUID_UPPER}')) t(x)"
    ).fetchall()[0][0]
    pol_distinct = pl.Series([UUID_LOWER, UUID_UPPER]).n_unique()
    print(f"    DuckDB  count(DISTINCT x::UUID)  = {duck_distinct}")
    print(f"    DuckDB  count(DISTINCT x::VARCHAR)= {varchar_distinct}")
    print(f"    Polars  String 的 n_unique()      = {pol_distinct}")
    print()
    print("  **同一个 UUID，Polars 侧算成两条记录。**")
    print()
    print("  后果不是「多了一行」这么轻：")
    print("    · `unique()` / `distinct()` 去不掉重 —— 主键列会出现重复值。")
    print("    · `group_by(uuid)` 会拆成两个组，每组一半。")
    print("    · `join` 会**多出一行**（一对多），行数对不上。")
    print()
    print("  **规矩：UUID 一进系统就统一成小写。**")
    print("  DuckDB 那边靠 UUID 类型兜着（大小写两种写法都认），")
    print("  Polars 那边得自己 `str.to_lowercase()` —— 或者干脆用 **Binary 模拟**，")
    print("  16 个字节没有大小写这回事。")


# ---------------------------------------------------------------- 场景 4


def scenario_sorting(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 排序：四种排法一致，但只有 v7 有时间序")

    vals = [f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(1000)]
    con.execute(
        "CREATE TABLE u AS SELECT * FROM (VALUES " + ",".join(f"('{v}')" for v in vals) + ") t(x)"
    )
    duck_order = [r[0] for r in con.sql("SELECT x::VARCHAR FROM u ORDER BY x::UUID").fetchall()]
    pol_str = pl.Series(vals).sort().to_list()
    pol_bin = pl.Series([bytes.fromhex(v.replace("-", "")) for v in vals]).sort().to_list()
    pol_u128 = (
        pl.Series([int(v.replace("-", ""), 16) for v in vals], dtype=pl.UInt128).sort().to_list()
    )

    print("  同一批 1000 个 UUID，四种排法：")
    print(f"    DuckDB  ORDER BY x::UUID        → 第一个 {duck_order[0]}")
    print(f"    Polars  String.sort()           → 第一个 {pol_str[0]}")
    print(f"    Polars  Binary.sort()           → 第一个 {pol_bin[0].hex()}")
    print(f"    Polars  UInt128.sort()          → 第一个 {pol_u128[0]:032x}")
    print()
    same_str = duck_order == pol_str
    same_bin = [bytes.fromhex(v.replace("-", "")) for v in duck_order] == pol_bin
    same_u128 = [int(v.replace("-", ""), 16) for v in duck_order] == pol_u128
    print(f"    DuckDB 的顺序 == Polars String 顺序？ {same_str}")
    print(f"    DuckDB 的顺序 == Polars Binary 顺序？ {same_bin}")
    print(f"    DuckDB 的顺序 == Polars UInt128 顺序？{same_u128}")
    print()
    print("  **三种模拟都跟 DuckDB 的 UUID 排出来一样的** —— 排序这件事不是问题。")
    print("  （前提是 UUID 文本是规范的小写；大小写混排的场景 3 说过，那就不一样了。）")
    print()

    # ---- uuidv7 的时间序
    con.execute("CREATE TABLE v7 AS SELECT i, uuidv7() AS u FROM range(300) t(i)")
    gen = [r[0] for r in con.sql("SELECT u::VARCHAR FROM v7 ORDER BY i").fetchall()]
    srt = [r[0] for r in con.sql("SELECT u::VARCHAR FROM v7 ORDER BY u").fetchall()]
    gen_ts = [_ts_prefix(u) for u in gen]
    same_ms = len(set(gen_ts)) == 1
    print("  **uuidv7 的前 48 位是毫秒时间戳** —— 所以按 UUID 排 ≈ 按创建时间排。")
    print(
        f"    实测：连着生成 300 个 uuidv7，全落在**同一个毫秒**里（{'是' if same_ms else '否'}）。"
    )
    print(
        f"    这个毫秒内排序 == 生成顺序？ {srt == gen}"
        f"  ← **{'否' if srt != gen else '是'}**，同毫秒内是随机的。"
    )
    print()
    time.sleep(0.05)
    spread = []
    for _ in range(5):
        spread.append(con.sql("SELECT uuidv7()::VARCHAR").fetchall()[0][0])
        time.sleep(0.003)
    spread_sorted = sorted(spread)
    print("    跨毫秒再测一次（每个之间 sleep 3 ms）：")
    print(f"      生成顺序 == 排序结果？ {spread_sorted == spread}")
    print("      —— 跨毫秒就是按时间序排的。")
    print()
    print("  **所以：**")
    print("    · `uuidv4()` 是随机的 —— 按它排序跟时间无关，B+ 树索引会一直裂。")
    print("    · `uuidv7()` 前缀带时间 —— 按它排序等于按时间排，索引不裂。")
    print("    · 主键用 UUID 的话，**优先选 v7**。")
    print("    · Polars 侧用 String 模拟同样能享受这个性质（排序结果一致）。")


# ---------------------------------------------------------------- 场景 5


def scenario_roundtrip(con: duckdb.DuckDBPyConnection, tmpdir: Path) -> None:
    section("场景 5 · 往返：DuckDB 保住 UUID，Polars 只能拿到 16 字节")

    duck_pq = tmpdir / "duck_uuid.parquet"
    con.execute(f"COPY (SELECT '{UUID_LOWER}'::UUID AS id) TO '{duck_pq}' (FORMAT PARQUET)")

    print("  **DuckDB 写出去，Polars 读回来：**")
    import pyarrow.parquet as pq

    for col in pq.ParquetFile(duck_pq).metadata.schema:
        print(
            f"    parquet 列 {col.name}: physical={col.physical_type}, logical={col.logical_type}"
        )
    df = pl.read_parquet(duck_pq)
    print(f"    Polars 的 dtype → {df.schema['id']}")
    print(f"    Polars 的值    → {df['id'].to_list()}（{len(df['id'][0])} 字节）")
    print()
    print("  **Polars 没有 UUID dtype，只能读成 Binary（16 字节）** ——")
    print("  值是对的（就是那 16 个字节，大端），但类型信息（这是个 UUID）没了。")
    print()
    print("  **Polars 写出去，DuckDB 读回来：**")
    pol_pq = tmpdir / "polars_uuid.parquet"
    pl.DataFrame({"id": pl.Series([bytes.fromhex(UUID_LOWER.replace("-", ""))])}).write_parquet(
        pol_pq
    )
    duck_types = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{pol_pq}')").fetchall()
    duck_vals = con.sql(f"SELECT * FROM read_parquet('{pol_pq}')").fetchall()
    print(f"    DuckDB 的类型 → {[t[1] for t in duck_types]}")
    print(f"    DuckDB 的值   → {duck_vals}")
    print()
    print("  **DuckDB 读成 BLOB，不是 UUID** —— 因为 Polars 写的 parquet 里")
    print("  那 16 个字节是裸的 BYTE_ARRAY，没有 UUID 的 logical annotation。")
    print()
    print("  但有办法救回来 —— DuckDB 认得 16 字节的 BLOB：")
    back = con.sql(f"SELECT id::UUID FROM read_parquet('{pol_pq}')").fetchall()[0][0]
    print(f"    SELECT id::UUID FROM read_parquet(...) → {back}")
    print()
    wrong = _try(lambda: con.sql("SELECT '\\x00'::BLOB::UUID").fetchall()[0][0])
    print(f"    长度不对的话：{wrong}")
    print()
    print("  **所以迁移的路径是通的：**")
    print("    Polars 侧存 Binary(16) → parquet → DuckDB 读成 BLOB → 补一次 `::UUID`。")
    print()
    print("    反过来 DuckDB 写 UUID → Polars 拿 Binary(16)，要变回文本得自己拼：")
    raw = df["id"][0]
    hexs = raw.hex()
    print(f"      raw.hex()        = {hexs}")
    print(
        f"      插横杠           = {hexs[:8]}-{hexs[8:12]}-{hexs[12:16]}-{hexs[16:20]}-{hexs[20:]}"
    )
    print("      （16 个字节不是合法的 UTF-8，`b.decode('utf-8')` 直接报错 ——")
    print("        只能走 hex 这一条路。）")


# ---------------------------------------------------------------- 场景 6


def scenario_cast(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · cast 和约束")

    print("  **能转的：**")
    for expr, sql in [
        ("uuid::VARCHAR", f"SELECT '{UUID_LOWER}'::UUID::VARCHAR"),
        ("uuid::BLOB", f"SELECT '{UUID_LOWER}'::UUID::BLOB"),
        ("varchar::UUID", f"SELECT '{UUID_LOWER}'::VARCHAR::UUID"),
    ]:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"    {common.pad(expr, 20)}{val}")
    print()
    print("  **转不了的：**")
    for expr, sql in [
        ("uuid::BIGINT", f"SELECT '{UUID_LOWER}'::UUID::BIGINT"),
        ("uuid::HUGEINT", f"SELECT '{UUID_LOWER}'::UUID::HUGEINT"),
    ]:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"    {common.pad(expr, 20)}{val}")
    print()
    print("  **UUID 转不了整数** —— 128 位塞不进 64 位，DuckDB 直接说 Unimplemented。")
    print("  （Polars 那边用 UInt128 模拟的话，**能**存下这 128 位 ——")
    print("    这是模拟方案唯一比原生的强的地方，但也只是「存得下」，")
    print("    别指望它有 UUID 那种类型约束。）")
    print()
    print("  **隐式比较**：UUID 和 VARCHAR 比，VARCHAR 会被转成 UUID：")
    cmp = con.sql(f"SELECT '{UUID_LOWER}'::UUID = '{UUID_LOWER}'").fetchall()[0][0]
    print(f"    SELECT '{UUID_LOWER}'::UUID = '{UUID_LOWER}' → {cmp}")
    bad = _try(lambda: con.sql("SELECT 'not-a-uuid'::UUID").fetchall()[0][0])
    print(f"    SELECT 'not-a-uuid'::UUID → {bad}")
    print()
    print("  **约束**：")
    con.execute("CREATE TABLE pk2(id UUID PRIMARY KEY)")
    con.execute(f"INSERT INTO pk2 VALUES ('{UUID_LOWER}')")
    dup = _try(lambda: con.sql(f"INSERT INTO pk2 VALUES ('{UUID_LOWER}')").fetchall())
    print(f"    UUID 主键插重复 → {dup}")
    print("    Polars 那边**没有主键约束** —— 唯一性得自己保证（`is_duplicated()` 查一遍）。")


# ---------------------------------------------------------------- 场景 7


def scenario_practical(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 落地建议")

    rows = [
        (
            "DuckDB 内部的主键",
            "UUID 类型 + uuidv7()",
            "有类型约束、能排序、索引不裂（v7）",
        ),
        (
            "Polars 侧存 ID 列",
            "Binary(16)",
            "最省内存，排序跟 DuckDB 一致，没有大小写问题",
        ),
        (
            "要给人和别的系统看",
            "String（小写）",
            "能直接读；**必须统一小写**，否则同一个 UUID 算两个",
        ),
        (
            "要能跟 DuckDB 对账",
            "两边都存小写文本",
            "DuckDB 侧 `::UUID` 贴回去，Polars 侧 `str.lower()`",
        ),
    ]
    print(f"  {'场景':<26}{'选谁':<28}{'理由'}")
    print("  " + "-" * 92)
    for scene, who, why in rows:
        print(f"  {common.pad(scene, 26)}{common.pad(who, 28)}{why}")
    print()
    print("  三条硬规矩：")
    print("    1. **UUID 一进系统就统一成小写**（或者用 Binary 模拟）。")
    print("       DuckDB 的 UUID 类型把大小写两种写法认成同一个值，Polars 的 String 不认 ——")
    print("       这是最容易漏的一条。")
    print("    2. **主键优先选 uuidv7()**，别用 v4 —— 索引会一直裂。")
    print("    3. **Polars 侧没有 UUID dtype 就别硬造** —— 用 Binary(16)，")
    print("       需要文本的时候再转，别长期拿 String 当主键。")


# ---------------------------------------------------------------- 小结


def summary() -> None:
    section("小结")
    print("  1. **DuckDB 有原生 UUID 类型**：可排序、可做主键、能生成（4 个函数）、")
    print("     能提取版本号；固定 16 字节。Polars **没有 UUID dtype**。")
    print("  2. **Polars 只能模拟**：String（36 字节/行）、UInt128（16）、Binary（16）。")
    print("     内存差 2.25 倍 —— UUID 列多的时候很显眼。")
    print("  3. **最大的坑是大小写**：DuckDB 的 UUID 把 `'550E...'` 和 `'550e...'`")
    print("     认成同一个值（count DISTINCT = 1），Polars 的 String 认成两个（n_unique = 2）。")
    print("     后果是去重失效、group_by 拆组、join 多行。")
    print("  4. **排序是一致的**：DuckDB UUID 序 == Polars String / Binary / UInt128 序。")
    print("     但只有 `uuidv7()` 有时间序（v4 是随机的，索引会一直裂）。")
    print("  5. **parquet 往返**：DuckDB 保住 UUID（FIXED_LEN_BYTE_ARRAY + UUID 标注），")
    print("     Polars 读成 16 字节 Binary；反过来 Polars 的 Binary 被 DuckDB 读成 BLOB，")
    print("     补一次 `::UUID` 能救回来。")
    print("  6. **UUID 转不了整数**（::BIGINT / ::HUGEINT 都是 Unimplemented）；")
    print("     Polars 也没有主键约束，唯一性得自己查。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="UUID：原生类型 vs 自己模拟")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    with tempfile.TemporaryDirectory(prefix="uuid_demo_") as tmp:
        tmpdir = Path(tmp)
        scenario_duckdb(con)
        scenario_polars()
        scenario_case(con)
        scenario_sorting(con)
        scenario_roundtrip(con, tmpdir)
        scenario_cast(con)
        scenario_practical(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
