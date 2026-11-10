r"""正则与字符串：两个引擎的正则引擎一样，但周边语义差很多。

* 两边的正则都是 RE2 风格（没有回溯、线性时间），**不支持前后瞻**（`a(?=b)`）、
  **不支持反向引用**（`(a)\1`）—— 写了就报错，这点反而是一致的。
* `regexp_extract` 的分组编号两边一致，但「匹配不上」时的返回值不一样。
* 「整串匹配」和「子串匹配」的工具函数位置不同：Polars 2.1 **没有** `str.fullmatch`。
* `str.replace` 只换第一个、`str.split` 默认按字面量切 —— 默认值不一样，踩坑很容易。
* DuckDB **没有**正则版的位置查找函数（`strpos` 只认字面量）。
* 中文场景：按字节还是按字符、LIKE 的 `_` 只占一个字符还是多字节。

用法：
    uv run python src/regex_demo.py
    uv run python src/regex_demo.py --rows 500000 --repeat 5
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

# 一份带中英文、数字、订单号的样本
SAMPLES = pl.DataFrame(
    {
        "code": [
            "ORD-2024-001",
            "ord-2023-777",
            "ORD_2024_003",
            "订单-2024-004",
            "AB12CD34",
            "A1B2",
            "x",
            "",
        ],
        "note": [
            "Apple iPhone 15",
            "小米手机",
            "width=100px",
            "tab\tseparated",
            "path/to/file.txt",
            "emoji 😀",
            "全角ＡＢＣ",
            "普通文本",
        ],
    }
)


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.register("s", SAMPLES)
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


# ---------------------------------------------------------------- 1. 不支持的正则


def scenario_unsupported(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 两边都不支持的前瞻与反向引用（这点反而是一致的）")

    patterns = [
        (r"a(?=b)", "前瞻：后面必须跟着 b"),
        (r"a(?!b)", "否定前瞻：后面不能是 b"),
        (r"(a)\1", "反向引用：重复上一个括号组"),
        (r"(?<=a)b", "后顾：前面必须是 a"),
        (r"(?i)ABC", "内联标志：忽略大小写"),
        (r"\d+", "数字"),
        (r"[[:alpha:]]+", "POSIX 字符类"),
    ]
    print(
        "  " + common.pad("正则", 20) + common.pad("说明", 26) + common.pad("DuckDB", 30) + "Polars"
    )
    print("  " + "-" * 96)
    for pat, desc in patterns:
        try:
            con.sql(f"SELECT regexp_matches('ab', '{pat}')").fetchall()
            duck = "✅ 支持"
        except Exception as exc:
            duck = f"❌ {type(exc).__name__}"
        try:
            pl.Series(["ab"]).str.contains(pat)
            pol = "✅ 支持"
        except Exception as exc:
            pol = f"❌ {type(exc).__name__}"
        print("  " + common.pad(pat, 20) + common.pad(desc, 26) + common.pad(duck, 30) + pol)

    print("\n两个引擎的正则都是 RE2 风格（没有回溯、线性时间），所以：")
    print("  · 前后瞻、后顾、反向引用一律不支持 —— 报错信息不一样，但结论一样")
    print("  · 内联标志 (?i) 两边都支持")
    print("  · POSIX 字符类 [[:alpha:]] 两边都支持")
    print("\n注意 DuckDB 的 regexp_matches 默认是**部分匹配**（找子串），")
    print("要整串匹配得用 regexp_full_match（场景 3 展开）。")


# ---------------------------------------------------------------- 2. 分组编号与提取


def scenario_extract(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · regexp_extract / str.extract：分组编号一致，但「匹配不上」不一样")

    pat = r"([A-Z]+)-([0-9]{4})-([0-9]+)"
    duck = con.sql(
        f"SELECT code, regexp_extract(code, '{pat}', 0) AS g0, "
        f"regexp_extract(code, '{pat}', 1) AS g1, "
        f"regexp_extract(code, '{pat}', 2) AS g2, "
        f"regexp_extract(code, '{pat}', 3) AS g3 "
        "FROM s ORDER BY code"
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck)

    polars_df = (
        SAMPLES.with_columns(
            pl.col("code").str.extract(pat, 0).alias("g0"),
            pl.col("code").str.extract(pat, 1).alias("g1"),
            pl.col("code").str.extract(pat, 2).alias("g2"),
            pl.col("code").str.extract(pat, 3).alias("g3"),
        )
        .select("code", "g0", "g1", "g2", "g3")
        .sort("code")
    )

    common.print_frame(duck_df, "DuckDB regexp_extract", max_rows=10)
    common.print_frame(polars_df, "Polars str.extract", max_rows=10)

    # 只在「两边都匹配上」的行上比 —— 匹配不上时两边的表示法不同（见下）。
    duck_map = dict(zip(duck_df["code"].to_list(), duck_df["g1"].to_list(), strict=True))
    matched_codes = [c for c in duck_map if duck_map[c] != ""]
    polars_map = dict(zip(polars_df["code"].to_list(), polars_df["g1"].to_list(), strict=True))
    same = all(duck_map[c] == polars_map[c] for c in matched_codes)
    print(f"\n  匹配上的行数：{len(matched_codes)}（样例共 {duck_df.height} 行）")
    print(f"  组 1（第一个括号组）在匹配行上两边一致：{'✅' if same else '❌'}")

    print("\n两边的分组编号口径一致：0 = 整个匹配，1 = 第一个括号组。")
    print("（dialect_demo 里已经对过这一条，这里再确认一次 —— 组号写错是最常见的坑。）")

    print("\n但「匹配不上」时两边的表示法不同：")
    duck_nomatch = con.sql(
        f"SELECT regexp_extract('abc', '{pat}', 1) AS g1, regexp_extract('abc', '{pat}', 9) AS g9"
    ).fetchall()[0]
    pol_nomatch = (
        pl.DataFrame({"c": ["abc"]})
        .select(
            pl.col("c").str.extract(pat, 1).alias("g1"),
            pl.col("c").str.extract(pat, 9).alias("g9"),
        )
        .to_dicts()[0]
    )
    print(f"  DuckDB 不匹配 -> {duck_nomatch}   （空字符串）")
    print(f"  Polars 不匹配 -> {pol_nomatch}")
    print("  → DuckDB 用空串占位，Polars 用 null。写 `= ''` 和写 `IS NULL` 会得到相反的结果。")
    print("  → 另外 DuckDB 的组号越界（这里是 9）也返回空串而不是报错。")

    print("\n「找全部」的能力：")
    try:
        rows = con.sql(
            "SELECT regexp_extract_all('a1b2c3', '[0-9]') AS all_digits FROM (SELECT 1)"
        ).fetchall()
        print(f"  DuckDB regexp_extract_all('a1b2c3') -> {rows[0][0]}")
    except Exception as exc:
        print(f"  DuckDB regexp_extract_all：{type(exc).__name__}: {str(exc).splitlines()[0]}")
    pol_extract_all = pl.Series(["a1b2c3"]).str.extract_all("[0-9]").to_list()
    print(f"  Polars str.extract_all        -> {pol_extract_all}")


# ---------------------------------------------------------------- 3. 整串 vs 子串


def scenario_fullmatch(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 「整串匹配」和「子串匹配」的工具函数")

    text = "ORD-2024-001"
    pat = r"[A-Z]{3}-[0-9]{4}-[0-9]{3}"

    duck_full = con.sql(
        f"SELECT regexp_full_match('{text}', '{pat}') AS full, "
        f"regexp_matches('{text}', '{pat}') AS part"
    ).fetchall()[0]
    pol_full = pl.Series([text]).str.contains(f"^{pat}$").to_list()[0]
    pol_part = pl.Series([text]).str.contains(pat).to_list()[0]

    print(f"样本 {text!r}，模式 {pat}")
    print(f"  DuckDB regexp_full_match  -> {duck_full[0]}")
    print(f"  DuckDB regexp_matches     -> {duck_full[1]}   （找子串）")
    print(f"  Polars  str.contains(锚定) -> {pol_full}")
    print(f"  Polars  str.contains       -> {pol_part}   （找子串）")

    print("\nDuckDB 没有 regexp_contains 这种「只判断不返回」的名字 ——")
    print("  用 regexp_matches（部分匹配）或 regexp_full_match（整串匹配）。")
    print("Polars 这边 str.contains 默认也是部分匹配，另有 str.starts_with / ends_with。")

    print(f"\nPolars 2.1 有 str.fullmatch 吗：{hasattr(pl.Series(['x']).str, 'fullmatch')}")
    print("  → 没有。Polars 侧要整串匹配只能自己加 ^ 和 $（Rust 正则里 ^ $ 锚定整串，")
    print("    且不像 Python 那样允许 $ 匹配末尾换行之前）。")

    print("\n子串匹配的陷阱（同一个模式，两种口径差很远）：")
    for t in ["ORD-2024-001", "xxORD-2024-001yy"]:
        d = con.sql(
            f"SELECT regexp_matches('{t}', '{pat}') AS part, "
            f"regexp_full_match('{t}', '{pat}') AS full"
        ).fetchall()[0]
        p = pl.Series([t]).str.contains(pat).to_list()[0]
        pf = pl.Series([t]).str.contains(f"^{pat}$").to_list()[0]
        print(f"  {t!r}: DuckDB 子串={d[0]} 整串={d[1]} | Polars 子串={p} 整串(锚定)={pf}")

    print("\n「找第一个」：")
    duck_first = q1(con, "SELECT regexp_extract('ORD-2024-001', '[0-9]{4}', 0)")
    pol_first = pl.Series(["ORD-2024-001"]).str.extract("[0-9]{4}", 0).to_list()[0]
    print(f"  DuckDB regexp_extract(找第一个匹配的子串) -> {duck_first!r}")
    print(f"  Polars  str.extract(找第一个匹配的子串) -> {pol_first!r}")


# ---------------------------------------------------------------- 4. 替换


def scenario_replace(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 替换：只换第一个，还是全换")

    text = "a1b2c3"
    pat = r"[0-9]"

    duck_first = q1(con, f"SELECT regexp_replace('{text}', '{pat}', '#')")
    duck_all = q1(con, f"SELECT regexp_replace('{text}', '{pat}', '#', 'g')")
    pol_first = pl.Series([text]).str.replace(pat, "#").to_list()[0]
    pol_all = pl.Series([text]).str.replace_all(pat, "#").to_list()[0]

    print(f"样本 {text!r}，模式 {pat}，替换成 '#'\n")
    print(f"  DuckDB regexp_replace(默认)     -> {duck_first!r}   （只换第一个）")
    print(f"  DuckDB regexp_replace(...,'g')  -> {duck_all!r}   （'g' 才是全局）")
    print(f"  Polars  str.replace(默认)        -> {pol_first!r}   （n=1，只换第一个）")
    print(f"  Polars  str.replace_all          -> {pol_all!r}")
    print("\n默认行为两边一致（都只换第一个），但开关的位置不同：")
    print("  DuckDB 在函数第 4 个参数里传 'g'；Polars 用另一个函数名 replace_all。")

    print("\n「按正则替换」与「按字面量替换」：")
    duck_literal = q1(con, "SELECT replace('a.c', '.', '-')")
    pol_literal = pl.Series(["a.c"]).str.replace(".", "-", literal=True).to_list()[0]
    duck_regex = q1(con, "SELECT regexp_replace('a.c', '.', '-')")
    pol_regex = pl.Series(["a.c"]).str.replace(".", "-").to_list()[0]
    print(f"  DuckDB replace()（字面量）        -> {duck_literal!r}")
    print(f"  DuckDB regexp_replace()（正则）   -> {duck_regex!r}")
    print(f"  Polars  str.replace(literal=True) -> {pol_literal!r}")
    print(f"  Polars  str.replace(默认=正则)    -> {pol_regex!r}")
    print("  → Polars 的 str.replace 默认就是正则，literal=True 才按字面量；")
    print("    DuckDB 则是两个函数分开（replace / regexp_replace）。")


# ---------------------------------------------------------------- 5. 切分与位置


def scenario_split_find(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 切分与位置查找：默认值不一样的地方")

    text = "a1b2c"
    pat = r"[0-9]"

    pol_split_default = pl.Series([text]).str.split(pat).to_list()[0]
    pol_split_regex = pl.Series([text]).str.split(pat, literal=False).to_list()[0]
    duck_split_literal = q1(con, f"SELECT string_split('{text}', '{pat}')")
    duck_split_regex = q1(con, f"SELECT regexp_split_to_array('{text}', '{pat}')")

    print(f"样本 {text!r}，按 {pat} 切分：\n")
    print(f"  DuckDB string_split()            -> {duck_split_literal}   （字面量）")
    print(f"  DuckDB regexp_split_to_array()   -> {duck_split_regex}   （正则）")
    print(f"  Polars  str.split()              -> {pol_split_default}   （字面量！）")
    print(f"  Polars  str.split(literal=False) -> {pol_split_regex}   （正则）")
    print("\n→ 这是最容易踩的一个：Polars 的 str.split 默认 literal=True，")
    print("  拿 '[0-9]' 去切等于找一个叫 '[0-9]' 的子串，切不动。")
    print("  DuckDB 也把两种分开成 string_split / regexp_split_to_array —— 这点是一致的。")

    print("\n找位置：")
    duck_strpos_literal = q1(con, "SELECT strpos('ORD-2024-001', '2024')")
    duck_strpos_regex = q1(con, "SELECT strpos('ORD-2024-001', '[0-9]{4}')")
    try:
        con.sql("SELECT regexp_instr('ORD-2024-001', '[0-9]{4}')").fetchall()
        duck_instr = "有"
    except Exception as exc:
        duck_instr = f"没有（{type(exc).__name__}）"
    pol_find = pl.Series(["ORD-2024-001"]).str.find(r"[0-9]{4}").to_list()[0]
    print(
        f"  DuckDB strpos('ORD-2024-001','2024')       -> {duck_strpos_literal}"
        "   （字面量，1 起数）"
    )
    print(
        f"  DuckDB strpos('ORD-2024-001','[0-9]{{4}}') -> {duck_strpos_regex}"
        "   （字面量找 '[0-9]{4}'，找不到）"
    )
    print(f"  DuckDB 正则版位置函数 regexp_instr        -> {duck_instr}")
    print(f"  Polars  str.find('[0-9]{{4}}')             -> {pol_find}   （字节偏移，0 起数）")
    print("\n→ DuckDB 没有正则版的位置查找：strpos 只认字面量，regexp_instr 不存在。")
    print("  Polars 的 str.find 走正则，返回的是**字节偏移**（0 起数），不是字符位置。")
    print("  差一个 1（1 起 vs 0 起），中文场景还差一个字节/字符。")

    print("\n匹配次数：")
    duck_cnt = q1(con, "SELECT len(regexp_extract_all('a1b2c3', '[0-9]'))")
    pol_cnt = pl.Series(["a1b2c3"]).str.count_matches(r"[0-9]").to_list()[0]
    print(f"  DuckDB len(regexp_extract_all(...)) -> {duck_cnt}")
    print(f"  Polars  str.count_matches(...)      -> {pol_cnt}")


# ---------------------------------------------------------------- 6. LIKE 与转义


def scenario_like(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · LIKE：通配符与转义")

    con.execute("CREATE TABLE lk AS SELECT * FROM (VALUES ('abc'),('a_c'),('a%c'),('ABC')) t(v)")
    print("数据：'abc'、'a_c'、'a%c'、'ABC'\n")

    duck_like = con.sql(
        "SELECT v, v LIKE 'a_c' AS like_any, v LIKE 'a\\_c' ESCAPE '\\' AS like_escaped "
        "FROM lk ORDER BY v"
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck_like)

    polars_df = (
        pl.DataFrame({"v": ["abc", "a_c", "a%c", "ABC"]})
        .with_columns(
            pl.col("v").str.contains("^a.c$").alias("like_any"),
            pl.col("v").str.contains("^a_c$").alias("like_escaped"),
        )
        .sort("v")
    )

    common.print_frame(duck_df, "DuckDB LIKE", max_rows=10)
    common.print_frame(polars_df, "Polars 用正则模拟（. 匹配任意一个字符）", max_rows=10)

    print("\nLIKE 的 '_' 匹配**恰好一个字符**：'abc' 匹配 'a_c'，'a_c' 也匹配。")
    print("DuckDB 用 ESCAPE '\\' 来转义下划线；Polars 没有 LIKE —— 常用正则代替，")
    print("但正则的 '.' 也只匹配一个字符（不是多字节字符），这点在中文上要小心。")

    print("\n大小写：")
    duck_ci = q1(con, "SELECT 'ABC' LIKE 'abc'")
    duck_ilike = q1(con, "SELECT 'ABC' ILIKE 'abc'")
    pol_ci = pl.Series(["ABC"]).str.contains("^abc$").to_list()[0]
    print(f"  DuckDB 'ABC' LIKE 'abc'   -> {duck_ci}")
    print(f"  DuckDB 'ABC' ILIKE 'abc'  -> {duck_ilike}")
    print(f"  Polars 正则 ^abc$         -> {pol_ci}   （正则默认区分大小写，跟 LIKE 一样）")
    print("  DuckDB 有 ILIKE（不区分大小写），Polars 没有对应物，只能用 (?i) 正则：")
    print(f"    Polars (?i) 正则 -> {pl.Series(['ABC']).str.contains('(?i)^abc$').to_list()[0]}")


# ---------------------------------------------------------------- 7. 中文与多字节


def scenario_unicode(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 中文、全角、Emoji：按字符还是按字节")

    print("样本：")
    for note in SAMPLES["note"].to_list():
        print(f"  {note!r}  字符数={len(note)} 字节数={len(note.encode('utf-8'))}")

    duck_len = con.sql(
        "SELECT note, length(note) AS len, strlen(note) AS bytes, length(upper(note)) AS up_len "
        "FROM s ORDER BY note"
    ).to_arrow_table()
    duck_df = pl.from_arrow(duck_len)

    polars_df = (
        SAMPLES.with_columns(
            pl.col("note").str.len_chars().alias("len"),
            pl.col("note").str.len_bytes().alias("bytes"),
            pl.col("note").str.to_uppercase().str.len_chars().alias("up_len"),
        )
        .select("note", "len", "bytes", "up_len")
        .sort("note")
    )

    common.print_frame(duck_df, "DuckDB length / strlen", max_rows=10)
    common.print_frame(polars_df, "Polars str.len_chars / str.len_bytes", max_rows=10)

    same = (
        duck_df["len"].to_list() == polars_df["len"].to_list()
        and duck_df["bytes"].to_list() == polars_df["bytes"].to_list()
    )
    print(f"\n  两边完全一致：{'✅' if same else '❌'}")
    print("  DuckDB 的 length() 数的是**字符**，strlen() 数的是**字节**；")
    print("  Polars 的 str.len_chars() / str.len_bytes() 是同一对概念。")

    print("\n全角字符的宽度：")
    full = "全角ＡＢＣ"
    duck_upper = q1(con, f"SELECT upper('{full}')")
    pol_upper = pl.Series([full]).str.to_uppercase().to_list()[0]
    print(f"  {full!r} -> DuckDB upper() = {duck_upper!r}（字符数 {len(duck_upper)}）")
    print(f"  {full!r} -> Polars 大写     = {pol_upper!r}（字符数 {len(pol_upper)}）")
    print("  全角字母没有大小写之分，两边都不会变 —— 但长度会变（字符数 vs 字节数）。")

    print("\nEmoji：")
    emoji = "emoji 😀"
    duck_emoji = con.sql(f"SELECT length('{emoji}') AS len, strlen('{emoji}') AS bytes").fetchall()[
        0
    ]
    pol_chars = pl.Series([emoji]).str.len_chars().item()
    pol_bytes = pl.Series([emoji]).str.len_bytes().item()
    print(f"  DuckDB  length={duck_emoji[0]} strlen={duck_emoji[1]}")
    print(f"  Polars  len_chars={pol_chars} len_bytes={pol_bytes}")
    print(
        f"  两边一致：{'✅' if (duck_emoji[0], duck_emoji[1]) == (pol_chars, pol_bytes) else '❌'}"
    )
    print("  → 一个 Emoji 在字符口径下算 1 个字符、UTF-8 下算 4 个字节；")
    print("    Python 自己的 len() 也是字符数，所以 len('😀') == 1。")

    print("\n位置口径不一样（这条最容易错）：")
    duck_pos = q1(con, "SELECT strpos('订单1', '1')")
    pol_pos = pl.Series(["订单1"]).str.find("1").to_list()[0]
    print(f"  DuckDB strpos('订单1','1') -> {duck_pos}   （第几个**字符**，1 起数）")
    print(f"  Polars  str.find('1')      -> {pol_pos}   （第几个**字节**，0 起数）")
    print("  → 同一段文本，DuckDB 给字符序号，Polars 给字节偏移；差的不只是 1。")

    print("\n切片两边倒是都按字符：")
    cn = "订单😀"
    duck_slice = q1(con, f"SELECT substr('{cn}', 2, 2)")
    pol_slice = pl.Series([cn]).str.slice(1, 3).to_list()[0]
    print(f"  {cn!r} DuckDB substr(2,2)   -> {duck_slice!r}")
    print(f"  {cn!r} Polars  str.slice(1,3) -> {pol_slice!r}")
    print("  → DuckDB 的 substr 和 Polars 的 str.slice 都是按字符切，口径一致。")


# ---------------------------------------------------------------- 8. 性能


def scenario_performance(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 8 · 正则的代价（两个引擎做同一件事）")

    print(f"数据：{rows} 行订单号")
    print(f"机器：{common.describe_machine()}")

    df = pl.DataFrame(
        {
            "code": [f"ORD-2024-{i % 1000:03d}" for i in range(rows)],
            "note": [f"订单 {i} 号" for i in range(rows)],
        },
        schema={"code": pl.String, "note": pl.String},
    )

    tmpdir = Path(tempfile.mkdtemp(prefix="regex_demo_"))
    src = tmpdir / "codes.parquet"
    df.write_parquet(src)
    con.execute("CREATE TABLE rc AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_src = pl.read_parquet(src)

    duck_sql = """
        SELECT code, regexp_extract(code, '([A-Z]+)-([0-9]{4})-([0-9]+)', 3) AS n
        FROM rc
    """

    def duck_run() -> pl.DataFrame:
        rel = con.sql(duck_sql)
        try:
            return pl.from_arrow(rel.arrow())
        finally:
            rel.close()

    def polars_run() -> pl.DataFrame:
        return polars_src.with_columns(
            pl.col("code").str.extract(r"([A-Z]+)-([0-9]{4})-([0-9]+)", 3).alias("n")
        )

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

    print(
        "\n"
        + common.pad("引擎", 12)
        + common.pad_left("中位数 (ms)", 16)
        + common.pad_left("最快 (ms)", 16)
    )
    print("-" * 44)
    print(
        common.pad("DuckDB", 12)
        + common.pad_left(f"{duck_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(duck_times) * 1000:.1f}", 16)
    )
    print(
        common.pad("Polars", 12)
        + common.pad_left(f"{polars_med * 1000:.1f}", 16)
        + common.pad_left(f"{min(polars_times) * 1000:.1f}", 16)
    )
    if polars_med > 0:
        print(f"\nDuckDB / Polars = {duck_med / polars_med:.2f}×（中位数口径）")

    print("\n口径：输入已物化，计时的只有「正则提取 + 结果物化」。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的正则与字符串差异演示")
    parser.add_argument("--rows", type=int, default=500_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"演示数据：{SAMPLES.height} 行小表（中英文混排）")

    con = connect()
    try:
        scenario_unsupported(con)
        scenario_extract(con)
        scenario_fullmatch(con)
        scenario_replace(con)
        scenario_split_find(con)
        scenario_like(con)
        scenario_unicode(con)
        scenario_performance(con, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
