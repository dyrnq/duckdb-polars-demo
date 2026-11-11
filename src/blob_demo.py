r"""BLOB / 二进制：字节、编码、base64，和「字符串到底是不是字节」。

二进制数据两边都有，但**默认行为不一样**，踩的坑也完全不一样：

  · DuckDB 把 VARCHAR 和 BLOB 分得很开 —— BLOB 是「字节」，VARCHAR 是「文本」，
    两套转换函数（`encode`/`decode` 一套，`::BLOB` cast 另一套），语义还不一样。
  · Polars 侧是 `String` 和 `Binary` 两个 dtype，base64 在 `.bin` 命名空间里，
    而且工具函数比 DuckDB 全（`size` / `contains` / `get` / `slice`）。

这个脚本拆六件事：

  1. **BLOB 字面量的 `\x` 陷阱** —— `'\xDEADBEEF'::BLOB` 不是 4 个字节，是 7 个。
  2. **四个编解码函数分别干什么** —— 尤其 `encode`/`decode` **不是** base64。
  3. **VARCHAR ↔ BLOB 的 cast 是「ASCII + 十六进制转义」通道**，不是 UTF-8。
  4. **长度、比较、排序** 用的度量不一样。
  5. **parquet 往返**保得住字节，但 VARCHAR 和 BLOB 的物理类型长得一样。
  6. **落地建议**：二进制该存成什么。

用法：
    uv run python src/blob_demo.py
"""

from __future__ import annotations

import argparse
import base64
import tempfile
from pathlib import Path

import duckdb
import polars as pl

import common


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _try(fn):
    """跑一个可能炸的表达式；炸了就返回一行「❌ 类型: 首行报错」。

    报错信息统一截到 60 个字符 —— 表格宽度有限，而且报错的前半句
    一般就够说明是什么问题了。
    """
    try:
        return fn()
    except Exception as exc:
        return f"❌ {type(exc).__name__}: {str(exc).splitlines()[0][:60]}"


# ---------------------------------------------------------------- 场景 1


def scenario_literal(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · BLOB 字面量的 \\x 陷阱")

    print("  DuckDB 的 BLOB 字面量写成 `'\\xDEADBEEF'::BLOB`。")
    print("  直觉上它应该是 4 个字节（DE AD BE EF）。**不是。**")
    print()

    blob = con.sql(r"SELECT '\xDEADBEEF'::BLOB").fetchall()[0][0]
    n = con.sql(r"SELECT octet_length('\xDEADBEEF'::BLOB)").fetchall()[0][0]
    h = con.sql(r"SELECT hex('\xDEADBEEF'::BLOB)").fetchall()[0][0]

    print(f"    值       = {blob!r}")
    print(f"    字节数   = {n}")
    print(f"    hex      = {h}")
    print()
    print("  **7 个字节。** 原因是 DuckDB 的转义规则：`\\x` 后面只跟**两个**十六进制位。")
    print("  所以 `\\xDE` 变成了 1 个字节 0xDE，剩下 `ADBEEF` 是 6 个普通 ASCII 字符。")
    print(f"  hex 看得更清楚：{h} —— 前两位 DE 是那个字节，后面 41 44 42 45 45 46 是 'ADBEEF'。")
    print()
    print("  要 4 个字节，得把每个字节都写全：")
    ok = con.sql(r"SELECT '\xDE\xAD\xBE\xEF'::BLOB").fetchall()[0][0]
    ok_n = con.sql(r"SELECT octet_length('\xDE\xAD\xBE\xEF'::BLOB)").fetchall()[0][0]
    print(f"    '\\xDE\\xAD\\xBE\\xEF'::BLOB → {ok!r}（{ok_n} 字节）")
    print()
    print("  这是从别的系统搬 SQL 过来最容易踩的一个 ——")
    print("  **别拿 `hex()` 的输出长度去推字节数，反过来也一样。**")
    print("  靠谱的办法是 `octet_length()`，或者直接看 `hex()` 的位数（位数 ÷ 2）。")
    print()
    print("  顺便：VARCHAR 和 BLOB 在 DuckDB 里是**两个不同的类型**：")
    t1 = con.sql("SELECT typeof('abc'::BLOB)").fetchall()[0][0]
    t2 = con.sql("SELECT typeof('abc')").fetchall()[0][0]
    print(f"    typeof('abc'::BLOB) = {t1}    typeof('abc') = {t2}")
    print("    Polars 那边对应的是 `Binary` 和 `String` —— 也是两个 dtype，")
    print("    但**没有强制**：Polars 的 `cast` 可以来回转，DuckDB 的 cast 有条件。")


# ---------------------------------------------------------------- 场景 2


def scenario_codecs(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 四个编解码函数，别搞混")

    rows = [
        ("encode(VARCHAR) → BLOB", "UTF-8 编码", r"SELECT encode('中文')"),
        ("decode(BLOB) → VARCHAR", "UTF-8 解码", r"SELECT decode('\xE4\xB8\xAD'::BLOB)"),
        ("base64(BLOB) → VARCHAR", "base64 编码", r"SELECT base64('hello'::BLOB)"),
        ("to_base64(BLOB) → VARCHAR", "同上（别名）", r"SELECT to_base64('hello'::BLOB)"),
        ("from_base64(VARCHAR) → BLOB", "base64 解码", r"SELECT from_base64('aGVsbG8=')"),
        ("hex(BLOB) → VARCHAR", "十六进制（大写）", r"SELECT hex('\x00\xFF'::BLOB)"),
        ("unhex(VARCHAR) → BLOB", "十六进制解码", r"SELECT unhex('00FF')"),
        ("from_hex(VARCHAR) → BLOB", "同上（别名）", r"SELECT from_hex('00FF')"),
    ]
    print(f"  {'函数':<34}{'干什么':<22}{'结果'}")
    print("  " + "-" * 92)
    for name, what, sql in rows:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"  {common.pad(name, 34)}{common.pad(what, 22)}{val}")
    print()

    print("  **最大的坑：`encode` / `decode` 在 DuckDB 里是 UTF-8 编解码，不是 base64。**")
    print("  别的系统（Python 的 `base64.b64decode`、Java 的 `Base64`、")
    print("  PostgreSQL 的 `decode(x, 'base64)`）里 `decode` 经常指 base64 解码 ——")
    print("  在 DuckDB 里 `decode('aGVsbG8=')` 返回的是**原样的字符串**，")
    dec = con.sql("SELECT decode('aGVsbG8=')").fetchall()[0][0]
    print(f"  实际是 {dec!r}。要 base64 解码得用 `from_base64()`。")
    print()

    b = pl.Series([b"abc", b"\x00\xff"])
    print("  另一头，**Polars 的 base64 在 `.bin` 命名空间里**（不在 `.str` 里）：")
    b64_enc = b.bin.encode("base64").to_list()
    hex_enc = b.bin.encode("hex").to_list()
    print(f"    Series([b'abc', b'\\x00\\xff']).bin.encode('base64') → {b64_enc}")
    print(f"    Series([b'abc', b'\\x00\\xff']).bin.encode('hex')     → {hex_enc}")
    print()
    print("  但 API 形状跟 DuckDB 不一样，两点要注意：")
    print("    1. **`.bin.decode()` 只吃 Binary 输入** —— base64 文本得先 cast 成 Binary：")
    b64s = pl.Series(["aGVsbG8="]).cast(pl.Binary)
    decoded = b64s.bin.decode("base64").to_list()
    print(f"       pl.Series(['aGVsbG8=']).cast(pl.Binary).bin.decode('base64') → {decoded}")
    print("       直接 `pl.Series(['aGVsbG8=']).bin.decode('base64')` 会报 SchemaError。")
    print("    2. **hex 大小写不一样**：Polars 给小写，DuckDB 给大写。")
    duck_hex = con.sql(r"SELECT hex('\x00\xFF'::BLOB)").fetchall()[0][0]
    print(f"       DuckDB hex → {duck_hex}    Polars bin.encode('hex') → {hex_enc[1]}")
    print()
    print("  base64 的**内容**倒是一致的（两边都跟 Python 标准库对得上）：")
    raw = b"\x00\xff"
    duck_b64 = con.sql(r"SELECT to_base64('\x00\xFF'::BLOB)").fetchall()[0][0]
    pol_b64 = pl.Series([raw]).bin.encode("base64")[0]
    py_b64 = base64.b64encode(raw).decode()
    print(f"    DuckDB to_base64    → {duck_b64}")
    print(f"    Polars bin.encode   → {pol_b64}")
    print(f"    Python b64encode    → {py_b64}")
    print()
    print("  还有一点：base64 里的 `=` 补位符在 URL 里要换成 `%3D`，")
    print("  两边都没有内置的 URL-safe base64 —— 得自己 replace。")


# ---------------------------------------------------------------- 场景 3


def scenario_cast_channel(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · VARCHAR ↔ BLOB 的 cast 是另一套语义")

    print("  DuckDB 里 **cast 和 encode/decode 是两条不同的通道**，语义不一样。")
    print("  cast 那条通道的规则实测下来是：**只吃 ASCII，而且把 `\\xNN` 当转义解开**。")
    print()

    rows = [
        ("'abc'::VARCHAR::BLOB", "纯 ASCII", r"SELECT 'abc'::VARCHAR::BLOB"),
        ("'\\x41'::VARCHAR::BLOB", "字面量带 \\x 转义", r"SELECT '\x41'::VARCHAR::BLOB"),
        ("'中文'::VARCHAR::BLOB", "**非 ASCII**", r"SELECT '中文'::VARCHAR::BLOB"),
        ("'a\\nb'::VARCHAR::BLOB", "反斜杠但不是 \\x", r"SELECT 'a\nb'::VARCHAR::BLOB"),
        ("'\\xFF'::BLOB::VARCHAR", "BLOB → VARCHAR", r"SELECT '\xFF'::BLOB::VARCHAR"),
        (
            "'\\xE4\\xB8\\xAD'::BLOB::VARCHAR",
            "BLOB → VARCHAR（UTF-8 字节）",
            r"SELECT '\xE4\xB8\xAD'::BLOB::VARCHAR",
        ),
    ]
    print(f"  {'表达式':<38}{'说明':<26}{'结果'}")
    print("  " + "-" * 100)
    for expr, what, sql in rows:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"  {common.pad(expr, 38)}{common.pad(what, 26)}{val}")
    print()

    print("  三条要记住的：")
    print("    1. **非 ASCII 的 VARCHAR 转不了 BLOB** ——")
    print("       `encode('中文')` 可以（得到 6 个 UTF-8 字节），`::BLOB` 不行。")
    print("    2. **BLOB 转 VARCHAR 不是 UTF-8 解码** —— 每个非 ASCII 字节被转成")
    print("       `\\xNN` 四个字符。所以 `length(blob::VARCHAR)` **不等于** `octet_length(blob)`：")
    ln = con.sql(r"SELECT length('\xFF'::BLOB::VARCHAR)").fetchall()[0][0]
    oc = con.sql(r"SELECT octet_length('\xFF'::BLOB)").fetchall()[0][0]
    print(f"         length('\\xFF'::BLOB::VARCHAR) = {ln}，octet_length('\\xFF'::BLOB) = {oc}")
    print("       真要 UTF-8 解码，用 `decode(blob)`。")
    print("    3. **cast 通道会把 `\\xNN` 解开** ——")
    print("       所以 `'\\x41'::VARCHAR::BLOB` 是 1 个字节 `A`，不是 4 个字符 `\\x41`。")
    print()

    print("  **Polars 这边完全不一样：Binary → String 是硬解，非法 UTF-8 直接报错。**")
    bin_series = pl.Series([b"abc", b"\x00\xff"])
    ok_str = _try(lambda: bin_series.cast(pl.String).to_list())
    print(f"    Binary([b'abc', b'\\x00\\xff']).cast(pl.String) → {ok_str}")
    print("    （DuckDB 那边同样的字节是**转义**成 '\\x00\\xff'，不报错 ——")
    print("      一个当错误处理，一个当数据处理，迁过去很容易对不上。）")
    print()
    print("  而且 Polars 的 `strict=False` 也救不了：")
    lenient = _try(lambda: bin_series.cast(pl.String, strict=False).to_list())
    print(f"    cast(pl.String, strict=False) → {lenient}")
    print("    —— 还是 ComputeError。**非法 UTF-8 的 Binary 在 Polars 里转不成字符串。**")
    print("    真要转，得自己写：`b.decode('utf-8', errors='replace')`。")


# ---------------------------------------------------------------- 场景 4


def scenario_measure(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 长度、比较、排序：量的不是同一件事")

    print("  **长度**：DuckDB 没有 `length(BLOB)`，只有 `octet_length(BLOB)`。")
    rows = [
        ("octet_length('abc'::BLOB)", "字节数", r"SELECT octet_length('abc'::BLOB)"),
        ("length('abc')", "字符数", r"SELECT length('abc')"),
        ("length('中文')", "字符数（不是字节数）", r"SELECT length('中文')"),
        (
            "octet_length('中文'::VARCHAR::BLOB)",
            "这条会报错（见场景 3）",
            r"SELECT octet_length('中文'::VARCHAR::BLOB)",
        ),
        (
            "length('\\xFF'::BLOB::VARCHAR)",
            "转义后的字符数",
            r"SELECT length('\xFF'::BLOB::VARCHAR)",
        ),
    ]
    print(f"  {'表达式':<44}{'含义':<24}{'结果'}")
    print("  " + "-" * 100)
    for expr, what, sql in rows:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"  {common.pad(expr, 44)}{common.pad(what, 24)}{val}")
    print()
    print("  Polars 那边：`.str.len_bytes()` 只吃 String，Binary 得用 `.bin.size()`。")
    b = pl.Series([b"abc", b"\x00\xff"])
    print(f"    Series([b'abc', b'\\x00\\xff']).bin.size() → {b.bin.size().to_list()}")
    print(f"    .str.len_bytes() → {_try(lambda: b.str.len_bytes().to_list())}")
    print()

    print("  **比较和排序**：BLOB 按**字节**比，VARCHAR 按字符比。")
    cmp_rows = [
        ("'A'::BLOB < 'B'::BLOB", r"SELECT 'A'::BLOB < 'B'::BLOB"),
        ("'\\x00'::BLOB < '\\x01'::BLOB", r"SELECT '\x00'::BLOB < '\x01'::BLOB"),
        ("'abc'::BLOB = 'abc'", r"SELECT 'abc'::BLOB = 'abc'"),
        ("'\\xFF'::BLOB = '\\xFF'", r"SELECT '\xFF'::BLOB = '\xFF'"),
    ]
    print(f"  {'表达式':<44}{'结果'}")
    print("  " + "-" * 92)
    for expr, sql in cmp_rows:
        val = _try(lambda s=sql: con.sql(s).fetchall()[0][0])
        print(f"  {common.pad(expr, 44)}{val}")
    print()
    print("  注意 `'\\xFF'::BLOB = '\\xFF'` 这一行：VARCHAR 那边的 `\\xFF` 是 4 个字符，")
    print("  转成 BLOB 时又被解成 1 个字节 0xFF，所以两边**相等**。")
    print("  这条链路（VARCHAR → BLOB）一路都在解转义 —— 记住它，别当成「取字节」。")
    print()
    print("  **排序**：")
    duck_order = [
        r[0]
        for r in con.sql(
            r"SELECT x FROM (VALUES ('\x42'::BLOB), ('\x01'::BLOB), ('\x41'::BLOB)) t(x) ORDER BY x"
        ).fetchall()
    ]
    pol_order = pl.Series([b"\x42", b"\x01", b"\x41"]).sort().to_list()
    print(f"    DuckDB  ORDER BY x → {duck_order}")
    print(f"    Polars  .sort()     → {pol_order}")
    print("    —— 两边都是**字节序**（0x01 < 0x41 < 0x42），这一条是对齐的。")
    print()
    print("  **但两边的「二进制工具函数」覆盖范围不一样：**")
    has_c = _try(
        lambda: con.sql(r"SELECT contains('\x414243'::BLOB, '\x42'::BLOB)").fetchall()[0][0]
    )
    has_s = _try(lambda: con.sql(r"SELECT substring('\x414243'::BLOB, 2, 2)").fetchall()[0][0])
    print(f"    DuckDB  contains(blob, blob) → {has_c}")
    print(f"    DuckDB  substring(blob, 2, 2) → {has_s}")
    print("    —— DuckDB 的 BLOB 上**没有**这两个，得先 `to_base64()` 或 `hex()`")
    print("       落到 VARCHAR 上再处理。")
    print()
    bb = pl.Series([b"ABC", b"\x00\xff"])
    needle = pl.Series([b"\x00"])
    print("    Polars 反过来，`.bin` 命名空间里有 DuckDB 没有的：")
    print(f"      .bin.size()            → {bb.bin.size().to_list()}")
    print(f"      .bin.contains(b'\\x00') → {bb.bin.contains(needle).to_list()}")
    print(f"      .bin.get(0)            → {bb.bin.get(0).to_list()}")
    print(f"      .bin.slice(0, 1)       → {bb.bin.slice(0, 1).to_list()}")
    print("    —— **Polars 的二进制工具比 DuckDB 全**。二进制处理的活写在这边更顺，")
    print("       DuckDB 那边得自己拿 hex / base64 绕一圈。")


# ---------------------------------------------------------------- 场景 5


def scenario_roundtrip(con: duckdb.DuckDBPyConnection, tmpdir: Path) -> None:
    section("场景 5 · 往返：parquet 里 BLOB 和 VARCHAR 都长得像")

    df = pl.DataFrame({"x": pl.Series([b"abc", b"\x00\xff"])})
    pol_pq = tmpdir / "polars_blob.parquet"
    df.write_parquet(pol_pq)

    duck_pq = tmpdir / "duck_blob.parquet"
    con.execute(f"COPY (SELECT 'abc'::BLOB AS b, 'abc' AS s) TO '{duck_pq}' (FORMAT PARQUET)")

    print("  Polars 的 Binary 写出去，DuckDB 读回来：")
    duck_types = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{pol_pq}')").fetchall()
    duck_vals = con.sql(f"SELECT * FROM read_parquet('{pol_pq}')").fetchall()
    print(f"    DuckDB 的类型 → {[t[1] for t in duck_types]}")
    print(f"    DuckDB 的值   → {duck_vals}")
    print()

    print("  DuckDB 的 BLOB 写出去，Polars 读回来：")
    back = pl.read_parquet(duck_pq)
    print(f"    Polars 的 schema → {back.schema}")
    print(f"    Polars 的值      → {back['b'].to_list()}")
    print()

    print("  **两边都保得住二进制** —— 这是整个 demo 里对齐得最好的一环：")
    print("  值和类型语义都保得住，只有类型名不一样。")
    print()
    print("  但 parquet 的物理类型值得看一眼：")
    import pyarrow.parquet as pq

    sch = pq.ParquetFile(duck_pq).metadata.schema
    for col in sch:
        print(f"    列 {col.name}: physical={col.physical_type}, logical={col.logical_type}")
    print()
    print("  **VARCHAR 和 BLOB 在 parquet 里是同一个物理类型（BYTE_ARRAY）**，")
    print("  区别只在 logical annotation 上：VARCHAR 带 `String`，BLOB **什么都不带**。")
    print("  DuckDB 读回来靠这个 annotation 还原成 BLOB 还是 VARCHAR。")
    print("  别的引擎（比如 Polars）不看 annotation —— 它一律读成 `Binary`。")
    print("  所以：**DuckDB 写的 BLOB 列，Polars 读回来是 Binary**（值对，类型名不一样）。")


# ---------------------------------------------------------------- 场景 6


def scenario_practical(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 落地建议")

    rows = [
        (
            "存文件的原始字节",
            "DuckDB BLOB / Polars Binary",
            "两边 parquet 往返都保得住；别经过 VARCHAR",
        ),
        (
            "存 base64 文本",
            "VARCHAR",
            "省事，但体积 +33%；要转回字节得自己解",
        ),
        (
            "存十六进制文本",
            "VARCHAR",
            "体积 +100%，但能直接 diff、能塞进 JSON",
        ),
        (
            "存 UTF-8 文本",
            "VARCHAR / Polars String",
            "**别用 BLOB 装** —— DuckDB 的 cast 通道不吃非 ASCII",
        ),
    ]
    print(f"  {'场景':<26}{'选谁':<32}{'理由'}")
    print("  " + "-" * 92)
    for scene, who, why in rows:
        print(f"  {common.pad(scene, 26)}{common.pad(who, 32)}{why}")
    print()
    print("  四条硬规矩：")
    print("    1. **别把 `\\x` 当「十六进制字符串」用** —— 它只吃两位，")
    print("       `\\xDEADBEEF` 是 7 个字节不是 4 个。")
    print("    2. **`encode`/`decode` 是 UTF-8，`base64`/`from_base64` 才是 base64。**")
    print("       Polars 的 base64 在 `.bin` 里，hex 大小写还跟 DuckDB 相反。")
    print("    3. **DuckDB 的 VARCHAR→BLOB cast 只吃 ASCII**，而且会解 `\\xNN` 转义；")
    print("       要真字节用 `encode()`，要真文本用 `decode()`。")
    print("    4. **长度别搞混** —— `octet_length` 是字节，`length` 是字符。")
    print("       BLOB 转出来的 VARCHAR 长度和字节数对不上（转义占了 4 个字符）。")


# ---------------------------------------------------------------- 小结


def summary() -> None:
    section("小结")
    print("  1. **BLOB 字面量的 `\\x` 只吃两位十六进制** ——")
    print("     `'\\xDEADBEEF'::BLOB` 是 7 个字节（0xDE + 'ADBEEF'），不是 4 个。")
    print("  2. **`encode`/`decode` 在 DuckDB 里是 UTF-8 编解码**，不是 base64；")
    print("     base64 的是 `base64()`/`to_base64()`/`from_base64()`。")
    print("     Polars 的 base64 在 `.bin` 命名空间里，解码前要先 cast 成 Binary；")
    print("     两边 hex 的大小写还相反（Polars 小写、DuckDB 大写）。")
    print("  3. **cast 通道和 encode/decode 通道语义不同**：")
    print("     `::BLOB` 只吃 ASCII、还会解 `\\xNN` 转义；")
    print("     `::VARCHAR` 把非 ASCII 字节转义成 `\\xNN`，长度对不上字节数。")
    print("  4. **Polars 的 Binary→String 是硬解**：非法 UTF-8 直接 ComputeError，")
    print("     DuckDB 那边是转义不报错 —— 同一个坏字节，两边一个当错一个当数据。")
    print(
        "  5. **长度有两个**：`octet_length` 是字节，`length` 是字符；DuckDB 没有 `length(BLOB)`。"
    )
    print("     Polars 的 Binary 用 `.bin.size()`（`.str.len_bytes()` 只吃 String）。")
    print("     反过来 Polars 的 `.bin.contains/get/slice` 是 DuckDB 的 BLOB 上没有的。")
    print("  6. **parquet 往返两边都保得住二进制**，但 VARCHAR 和 BLOB 的物理类型")
    print("     都是 BYTE_ARRAY，区别只在 logical annotation —— 别的引擎一律读成 Binary。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="BLOB / 二进制与 base64")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    with tempfile.TemporaryDirectory(prefix="blob_demo_") as tmp:
        tmpdir = Path(tmp)
        scenario_literal(con)
        scenario_codecs(con)
        scenario_cast_channel(con)
        scenario_measure(con)
        scenario_roundtrip(con, tmpdir)
        scenario_practical(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
