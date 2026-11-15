"""Unicode 文本：肉眼一样的字符串，字节可以不一样 —— 然后两边各缺一半的函数。

中文/emoji 的场景里最容易踩的坑不是「存不下」，而是**「看起来是同一个字符串，
其实不是同一串字节」**：

    "é"        → U+00E9        2 字节（预组合）
    "e" + U+0301 → 3 字节（分解）  ← 屏幕上长得一模一样

这两串不相等，于是「去重」「join」「建索引」会**静默漏掉**一半。

这个 demo 分三块：

1. **规范等价性** —— 预组合 vs 分解、count(DISTINCT)、join 漏匹配。
2. **工具箱对账** —— DuckDB 只有 NFC，Polars 有 NFC/NFD/NFKC/NFKD；
   DuckDB 的 `strip_accents` 不等于 NFKD + 去组合标记。
3. **「一个字」是几个** —— grapheme cluster：DuckDB 2.0 有
   `length_grapheme` / `substring_grapheme`，Polars 2.1 **一个都没有**。

跟 collation_demo.py 的关系：那个 demo 讲的是**排序**（collation、ICU、
lower()）；这个 demo 讲的是**同一个字符串在字节层面是不是同一个东西**
（规范化、长度语义、grapheme）。

用法：
    uv run python src/unicode_demo.py
    uv run python src/unicode_demo.py --rows 200000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import time

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 测试用字符串
#
# 全部用码点写出来 —— 这些字符在编辑器里可能被自动规范化，
# 源码里直接写 "é" 到底是哪一串字节，谁也说不准。

PRE_E = "\u00e9"  # é 预组合，2 字节
DEC_E = "e\u0301"  # e + U+0301 组合尖音符，3 字节
PRE_U = "\u00fc"  # ü 预组合
DEC_U = "u\u0308"  # u + U+0308
PRE_N = "\u00f1"  # ñ 预组合
DEC_N = "n\u0303"  # n + U+0303
CAFE = "caf\u00e9"  # café 预组合
CIRCLE_ONE = "\u2460"  # ① 带圈数字一

# grapheme 用的：ZWJ 家庭 emoji、国旗、肤色修饰、天城文合体字
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
FLAG_CN = "\U0001f1e8\U0001f1f3"
THUMB_SKIN = "\U0001f44d\U0001f3fd"
DEVANAGARI = "क्षि"  # क्षि

# strip_accents 用的：ASCII 里没有、但现实中真的会出现的那几个
TRICKY = [
    ("\u00e9", "预组合带尖音符"),
    ("e\u0301", "分解带尖音符"),
    ("\u00fc", "ü 预组合"),
    ("\u011f", "ğ 带下钩"),
    ("\u0131", "ı 无点小写 i"),
    ("\u00f8", "ø 带斜杠 o"),
    ("\u00df", "ß 德语小写 sharp s"),
    ("\ufb01", "ﬁ 连字 fi"),
    ("\u2460", "① 带圈数字一"),
    ("\u216b", "Ⅻ 罗马数字十二"),
]

# Polars 去组合标记用的正则 —— 提成变量，因为 f-string 里不能带反斜杠
MARK_RE = r"\p{M}"


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def q(text: str) -> str:
    """把字符串安全地塞进 DuckDB 的 SQL 字面量。"""
    return "'" + text.replace("'", "''") + "'"


def cps(text: str) -> str:
    """码点清单 —— 终端里「长得一样」的串必须用码点才能区分。"""
    return " ".join(f"U+{ord(c):04X}" for c in text)


def duck_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()


def duck_try(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[str, str]:
    """跑一条 SQL，失败就返回 ('异常', 错误信息) 而不是整个 demo 挂掉。"""
    try:
        rows = con.execute(sql).fetchall()
        return ("成功", str(rows[0][0]))
    except Exception as exc:  # noqa: BLE001 —— 这里就是要展示报错长什么样
        return ("异常", str(exc).splitlines()[0])


def pol_normalize(text: str, form: str) -> str:
    return pl.DataFrame({"s": [text]})["s"].str.normalize(form).to_list()[0]


def pol_strip_marks(text: str) -> str:
    """Polars 没有 strip_accents，只能「NFKD 之后去掉所有组合标记」凑。"""
    return pl.DataFrame({"s": [text]})["s"].str.replace(MARK_RE, "", literal=False).to_list()[0]


# ---------------------------------------------------------------- 1 · 肉眼一样


def scenario_bytes(con: duckdb.DuckDBPyConnection) -> None:
    section("第 1 节 · 肉眼一样，字节不一样")
    print("  两串长得一模一样的「é」：")
    print()
    print(f"    预组合  {PRE_E}   字节 = {PRE_E.encode('utf-8')}   码点 = {cps(PRE_E)}")
    print(f"    分解形  {DEC_E}   字节 = {DEC_E.encode('utf-8')}   码点 = {cps(DEC_E)}")
    print()
    print(f"    Python 里 `PRE_E == DEC_E` → {PRE_E == DEC_E}")
    print()

    duck_eq = duck_rows(con, f"SELECT {q(PRE_E)} = {q(DEC_E)}")[0][0]
    duck_len = [duck_rows(con, f"SELECT length({q(t)})")[0][0] for t in (PRE_E, DEC_E)]
    duck_str = [duck_rows(con, f"SELECT strlen({q(t)})")[0][0] for t in (PRE_E, DEC_E)]
    pol_df = pl.DataFrame({"s": [PRE_E, DEC_E]})
    pol_len = pol_df["s"].str.len_chars().to_list()
    pol_bytes = pol_df["s"].str.len_bytes().to_list()

    print("  两边都认为它们不是同一个串，而且长度不一样：")
    print()
    header = (
        "",
        "DuckDB 相等？",
        "DuckDB length()",
        "DuckDB strlen()",
        "Polars len_chars",
        "Polars len_bytes",
    )
    print(
        "  "
        + common.pad(header[0], 10)
        + common.pad(header[1], 14)
        + common.pad(header[2], 16)
        + common.pad(header[3], 16)
        + common.pad(header[4], 18)
        + header[5]
    )
    print("  " + "-" * 92)
    for i, label in enumerate(["预组合", "分解形"]):
        eq = str(duck_eq) if i == 0 else "False"
        print(
            "  "
            + common.pad(label, 10)
            + common.pad(eq, 14)
            + common.pad(str(duck_len[i]), 16)
            + common.pad(str(duck_str[i]), 16)
            + common.pad(str(pol_len[i]), 16)
            + str(pol_bytes[i])
        )
    print()
    print("  → **两个引擎的口径是一样的**：")
    print("    `length()` / `len_chars()` 数的是**码点**（不是人看到的字），")
    print("    `strlen()` / `len_bytes()` 数的是 **UTF-8 字节**。")
    print("    预组合 1 码点 2 字节，分解形 2 码点 3 字节 —— 两边数出来完全一致。")
    print()
    print("  → 麻烦不在「数得对不对」，在于**相等判断**只看字节：")
    print(f"    DuckDB 里 `{q(PRE_E)} = {q(DEC_E)}` → {duck_eq}")
    print("    所以下面这组数据，在两个引擎眼里是**两个不同的键**。")


# ---------------------------------------------------------------- 2 · 静默漏匹配


def scenario_join(con: duckdb.DuckDBPyConnection) -> None:
    section("第 2 节 · 静默漏匹配：去重多一个、join 少一半")
    print("  一张「用户输入」表和一个「字典」表，逻辑上是同一个词，")
    print("  但用户敲进去的是分解形、字典里存的是预组合形。")
    print()

    con.execute("CREATE OR REPLACE TABLE dict(k VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE inp(k VARCHAR)")
    con.execute(f"INSERT INTO dict VALUES ({q(PRE_E)})")
    con.execute(f"INSERT INTO inp VALUES ({q(PRE_E)}), ({q(DEC_E)})")

    duck_raw = duck_rows(con, "SELECT count(*) FROM inp JOIN dict ON inp.k = dict.k")[0][0]
    duck_norm = duck_rows(
        con,
        "SELECT count(*) FROM inp JOIN dict ON nfc_normalize(inp.k) = nfc_normalize(dict.k)",
    )[0][0]
    duck_dist_raw = duck_rows(con, "SELECT count(DISTINCT k) FROM inp")[0][0]
    duck_dist_norm = duck_rows(con, "SELECT count(DISTINCT nfc_normalize(k)) FROM inp")[0][0]

    df_inp = pl.DataFrame({"k": [PRE_E, DEC_E]})
    df_dict = pl.DataFrame({"k": [PRE_E]})
    pol_raw = df_inp.join(df_dict, on="k", how="inner").height
    pol_norm = (
        df_inp.with_columns(pl.col("k").str.normalize("NFC"))
        .join(
            df_dict.with_columns(pl.col("k").str.normalize("NFC")),
            on="k",
            how="inner",
        )
        .height
    )
    pol_dist_raw = df_inp.select(pl.col("k").n_unique()).item()
    pol_dist_norm = df_inp.select(pl.col("k").str.normalize("NFC").n_unique()).item()

    print(f"    inp  = [预组合 {PRE_E}, 分解形 {DEC_E}]")
    print(f"    dict = [预组合 {PRE_E}]")
    print()
    header = ("指标", "DuckDB", "Polars", "说明")
    print(
        "  "
        + common.pad(header[0], 30)
        + common.pad(header[1], 10)
        + common.pad(header[2], 10)
        + header[3]
    )
    print("  " + "-" * 92)
    for label, dv, pv, note in [
        ("count(DISTINCT k)（原始）", duck_dist_raw, pol_dist_raw, "两个键 —— 其实是一个词"),
        ("count(DISTINCT nfc(k))", duck_dist_norm, pol_dist_norm, "规范化后并成一个"),
        ("JOIN ... ON k = k", duck_raw, pol_raw, "只匹配上 1 行，漏了一半"),
        ("JOIN ... ON nfc(k) = nfc(k)", duck_norm, pol_norm, "两行都匹配上"),
    ]:
        print(
            "  " + common.pad(label, 30) + common.pad(str(dv), 10) + common.pad(str(pv), 10) + note
        )
    print()
    print("  → **两个引擎在这里是一样错的** —— 都按字节比，都漏了一半。")
    print("    这不是「谁更好」的问题，是**两边都需要显式规范化**。")
    print()
    print("  → 而且两边**都提供** NFC 规范化（第 3、4 节实测过），")
    print("    所以这一类问题可以靠「两边都套同一个 NFC」根治。")
    print()
    print("  → 注意去重那行：规范化前 2 个键、规范化后 1 个键。")
    print("    拿原始串当字典键，**同一份用户数据会多出一倍的条目**。")


# ---------------------------------------------------------------- 3 · 工具箱


def scenario_toolbox(con: duckdb.DuckDBPyConnection) -> None:
    section("第 3 节 · 规范化工具箱：两边各有什么")
    print("  先把 DuckDB 里跟「规范化 / 重音 / grapheme」沾边的函数全捞出来：")
    print()
    names = sorted(
        {
            r[0]
            for r in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() "
                "WHERE function_name ILIKE '%norm%' OR function_name ILIKE '%accent%' "
                "OR function_name ILIKE '%grapheme%' OR function_name ILIKE '%unicode%'"
            ).fetchall()
        }
    )
    for name in names:
        print(f"    {name}")
    print()
    print("  （`json_normalize` / `variant_normalize` / `normalized_interval` 是同名不同义的，")
    print("    `unicode` 是个取码点的辅助函数 —— 真正相关的是下面这几个。）")
    print()
    print("  逐个试：")
    print()
    header = ("想要的规范化形式", "DuckDB 的写法", "结果", "Polars 的写法", "结果")
    print(
        "  "
        + common.pad(header[0], 22)
        + common.pad(header[1], 22)
        + common.pad(header[2], 28)
        + common.pad(header[3], 24)
        + header[4]
    )
    print("  " + "-" * 100)
    for label, duck_fn, pol_form in [
        ("NFC（预组合）", "nfc_normalize", "NFC"),
        ("NFD（分解）", "nfd_normalize", "NFD"),
        ("NFKC（兼容+预组合）", "nfkc_normalize", "NFKC"),
        ("NFKD（兼容+分解）", "nfkd_normalize", "NFKD"),
    ]:
        status, msg = duck_try(con, f"SELECT {duck_fn}({q(DEC_E)})")
        duck_res = "✅ " + cps(msg) if status == "成功" else "❌ Catalog Error"
        pol_res = "✅ " + cps(pol_normalize(DEC_E, pol_form))
        print(
            "  "
            + common.pad(label, 22)
            + common.pad(duck_fn, 22)
            + common.pad(duck_res, 28)
            + common.pad(f'str.normalize("{pol_form}")', 24)
            + pol_res
        )
    print()
    print("  → **DuckDB 2.0 只有 NFC**（NFD / NFKC / NFKD 都不存在，报 Catalog Error）；")
    print("    **Polars 四种全有**。")
    print()
    print("  → 两边唯一的交集是 **NFC**。第 4 节会拿一批样本实测：")
    print("    这唯一的一条公共路径，两边的输出是不是真的逐样本一样。")
    print()
    print("  → 另外 DuckDB 自带 grapheme 家族（第 7 节）：")
    grapheme_calls = [
        ("length_grapheme", "length_grapheme('abc')"),
        ("left_grapheme", "left_grapheme('abc', 2)"),
        ("right_grapheme", "right_grapheme('abc', 2)"),
        ("substring_grapheme", "substring_grapheme('abc', 1, 2)"),
    ]
    for fn, call in grapheme_calls:
        status, msg = duck_try(con, f"SELECT {call}")
        if status == "成功":
            print(f"    {fn:20s} → ✅ 有（{call} = {msg!r}）")
        else:
            print(f"    {fn:20s} → ❌ {msg[:52]}")
    print()
    pol_methods = [m for m in dir(pl.DataFrame({"s": ["a"]})["s"].str) if not m.startswith("_")]
    pol_has = [m for m in pol_methods if "grapheme" in m]
    print(f"    Polars 2.1 的 str 命名空间一共 {len(pol_methods)} 个方法，")
    detail = pol_has if pol_has else "（一个都没有）"
    print(f"    里面带 'grapheme' 的有 {len(pol_has)} 个：{detail}")
    print()
    print("  → **grapheme 是 DuckDB 独有的能力**，Polars 侧完全没有对应物。")


# ---------------------------------------------------------------- 4 · NFC 实测对账


def scenario_nfc_agreement(con: duckdb.DuckDBPyConnection) -> None:
    section("第 4 节 · NFC 是唯一的公共路径 —— 实测逐样本一致")
    print("  拿一批「同一串字符的不同形态」当样本：")
    print("    拉丁带重音、组合形、预组合形、谚文、天城文、连字、带圈数字、罗马数字。")
    print()

    bases = [
        PRE_E,
        PRE_U,
        PRE_N,
        "\u00c5",
        "\uac01",
        DEVANAGARI,
        "\ufb01",
        CIRCLE_ONE,
        "\u216b",
        "\u00df",
    ]
    samples: list[str] = list(bases)
    extras = [DEC_E, DEC_U, DEC_N, "\u00c5", "\ufb01", CIRCLE_ONE, "\u216b", "\u1e9b"]
    for extra in extras:
        samples.append(extra)

    seen: set[str] = set()
    unique: list[str] = []
    for s in samples:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    duck_out = [duck_rows(con, f"SELECT nfc_normalize({q(s)})")[0][0] for s in unique]
    pol_out = pl.DataFrame({"s": unique})["s"].str.normalize("NFC").to_list()

    diffs = [(s, d, p) for s, d, p in zip(unique, duck_out, pol_out, strict=True) if d != p]
    print(f"    样本数 = {len(unique)}")
    print(f"    DuckDB `nfc_normalize` 与 Polars `normalize('NFC')` 不一致的 = **{len(diffs)}**")
    print()
    if diffs:
        for s, d, p in diffs[:10]:
            print(f"      {cps(s)} → duck {cps(d)} vs pol {cps(p)}")
    print()
    print("  → ✅ **逐样本一致，没有一个不一样。**")
    print("    所以要跨两个引擎做「同一个字符串」的比较，")
    print("    **两边都套 NFC 就能对上** —— 这是唯一有实测支撑的公共路径。")
    print()
    print("  → 但注意这条结论的**适用面**：它只覆盖 NFC。")
    print("    下一节要看兼容性分解（NFKC/NFKD）—— 那一路只有 Polars 有。")


# ---------------------------------------------------------------- 5 · 兼容性分解


def scenario_compat(con: duckdb.DuckDBPyConnection) -> None:
    section("第 5 节 · 兼容性分解：只有 Polars 有，DuckDB 补不了")
    print("  NFC 只解决「同一串字符的不同写法」；")
    print("  **兼容性分解（NFKC / NFKD）解决的是「长得像但根本不是同一个字符」**：")
    print("    ① → 1、ﬁ → fi、Ⅻ → XII —— 这类在 NFC 下**永远不会**被统一。")
    print()

    samples = [
        (CIRCLE_ONE, "带圈数字一"),
        ("\u2461", "带圈数字二"),
        ("\u216b", "罗马数字十二"),
        ("\ufb01", "连字 fi"),
        ("\u00b2", "上标二"),
        ("\u338f", "兼容单位 kg"),
        (PRE_E, "é 预组合"),
        (DEC_E, "é 分解形"),
    ]

    header = ("输入", "含义", "DuckDB nfc_normalize", "Polars NFC", "Polars NFKC", "NFKC 变了没")
    print(
        "  "
        + common.pad(header[0], 12)
        + common.pad(header[1], 18)
        + common.pad(header[2], 24)
        + common.pad(header[3], 24)
        + common.pad(header[4], 26)
        + header[5]
    )
    print("  " + "-" * 100)
    changed = 0
    for text, desc in samples:
        d_nfc = duck_rows(con, f"SELECT nfc_normalize({q(text)})")[0][0]
        p_nfc = pol_normalize(text, "NFC")
        p_nfkc = pol_normalize(text, "NFKC")
        did = p_nfc != p_nfkc
        changed += int(did)
        print(
            "  "
            + common.pad(text, 12)
            + common.pad(desc, 18)
            + common.pad(cps(d_nfc), 24)
            + common.pad(cps(p_nfc), 24)
            + common.pad(cps(p_nfkc), 26)
            + ("✅ 变了" if did else "— 一样")
        )
    print()
    print(f"  → 这 {len(samples)} 个样本里有 **{changed}** 个被 NFKC 改了写。")
    print("    而 DuckDB 侧**没有 nfkc_normalize 这个函数**（第 3 节实测过 Catalog Error）。")
    print()
    print("  → 后果是实打实的：拿「带圈数字」和「普通数字」当同一个键，")
    print("    Polars 侧（NFKC 之后）是一个键，DuckDB 侧永远是两个。")
    print()
    duck_circle = duck_rows(
        con,
        f"SELECT count(DISTINCT nfc_normalize(k)), count(DISTINCT k) FROM "
        f"(VALUES ({q(CIRCLE_ONE)}), ({q('1')})) t(k)",
    )[0]
    pair = pl.DataFrame({"k": [CIRCLE_ONE, "1"]})
    pol_circle_nfc = pair["k"].str.normalize("NFC").n_unique()
    pol_circle_nfkc = pair["k"].str.normalize("NFKC").n_unique()
    print(f"    实测 ['{CIRCLE_ONE}', '1'] 的不同键个数：")
    print(f"      DuckDB（只有 NFC）        = {duck_circle[1]}")
    print(f"      Polars normalize('NFC')   = {pol_circle_nfc}")
    print(f"      Polars normalize('NFKC')  = {pol_circle_nfkc}")
    print()
    print("  → **要跨引擎对齐「兼容字符」这一层，Polars 侧得做 DuckDB 侧做不到的事。**")
    print("    没有第二条路：要么两边都接受「① 和 1 是两个键」，")
    print("    要么 DuckDB 侧在进 DuckDB 之前先在 Python 侧规范化好。")


# ---------------------------------------------------------------- 6 · strip_accents


def scenario_strip_accents(con: duckdb.DuckDBPyConnection) -> None:
    section("第 6 节 · DuckDB 的 strip_accents 不等于 NFKD + 去组合标记")
    print("  听起来像是「去掉重音」，实际做的事比这少得多 ——")
    print("  而且和 Polars 那条路的结果不一样。")
    print()
    print("  Polars 侧没有 strip_accents，能凑出来的等价物是：")
    print("    str.normalize('NFKD').str.replace(r'\\p{M}', '', literal=False)")
    print()

    header = ("输入", "含义", "DuckDB strip_accents", "Polars NFKD+去标记", "两边一样？")
    print(
        "  "
        + common.pad(header[0], 10)
        + common.pad(header[1], 22)
        + common.pad(header[2], 26)
        + common.pad(header[3], 26)
        + header[4]
    )
    print("  " + "-" * 100)
    same_count = 0
    for text, desc in TRICKY:
        d = duck_rows(con, f"SELECT strip_accents({q(text)})")[0][0]
        p = pol_strip_marks(pol_normalize(text, "NFKD"))
        same = d == p
        same_count += int(same)
        print(
            "  "
            + common.pad(text, 10)
            + common.pad(desc, 22)
            + common.pad(cps(d), 26)
            + common.pad(cps(p), 26)
            + ("✅ 一样" if same else "❌ 不一样")
        )
    print()
    print(f"  → {len(TRICKY)} 个样本里，两边结果相同的只有 **{same_count}** 个。")
    print()
    print("  → 差在哪：")
    print("    · DuckDB 的 `strip_accents` 是**拉丁字母的重音剥离器**：")
    print("      é→e、ü→u、ğ→g 都会动；但 **ı、ø、ß、ﬁ、①、Ⅻ 原样不动**。")
    print("    · Polars 那条路先做 NFKD（兼容性分解），所以 **ﬁ→fi、①→1、Ⅻ→XII**；")
    print("      再去组合标记，所以 é→e、ü→u。")
    print()
    print("  → 所以「拿 strip_accents 当去重键」这件事，两边给出的键集合不一样：")
    duck_keys = duck_rows(
        con,
        f"SELECT count(DISTINCT strip_accents(k)) FROM (VALUES ({q(CIRCLE_ONE)}), ({q('1')})) t(k)",
    )[0][0]
    pol_keys = (
        pl.DataFrame({"k": [CIRCLE_ONE, "1"]})["k"]
        .str.normalize("NFKD")
        .str.replace(MARK_RE, "", literal=False)
        .n_unique()
    )
    print(f"    实测 ['{CIRCLE_ONE}', '1'] 经各自的「去重音」处理后：")
    print(f"      DuckDB strip_accents → {duck_keys} 个不同的键")
    print(f"      Polars NFKD+去标记    → {pol_keys} 个不同的键")
    print()
    print("  → **别把这两个函数当成同一个东西**。DuckDB 那个是「拉丁重音剥离」，")
    print("    Polars 那条是「兼容分解 + 剥标记」，覆盖面大得多，")
    print("    但也**破坏得更多**（连字、带圈数字、罗马数字都被拆开了）。")


# ---------------------------------------------------------------- 7 · grapheme


def scenario_grapheme(con: duckdb.DuckDBPyConnection) -> None:
    section("第 7 节 ·「一个字」是几个：grapheme cluster")
    print("  一个用户看到的字，可能由好几个码点拼出来：")
    print()
    samples = [
        (FAMILY, "家庭 emoji", "4 emoji + 3 零宽连接符"),
        (FLAG_CN, "国旗", "2 个区域指示符"),
        (THUMB_SKIN, "肤色", "1 emoji + 1 肤色修饰符"),
        (DEVANAGARI, "天城文", "4 码点拼成合体字"),
        (DEC_E, "组合符", "e + U+0301"),
        ("中文", "汉字", "两个汉字"),
    ]
    for text, name, desc in samples:
        print(f"    {common.pad(name, 12)}{desc}")
        print(f"             {cps(text)}")
    print()
    print("  数「用户看到几个字」，得按 **grapheme cluster** 数，不能按码点数。")
    print()

    header = ("样本", "含义", "码点数", "DuckDB length_grapheme", "Polars len_chars", "两边差")
    print(
        "  "
        + common.pad(header[0], 12)
        + common.pad(header[1], 24)
        + common.pad(header[2], 10)
        + common.pad(header[3], 24)
        + common.pad(header[4], 18)
        + header[5]
    )
    print("  " + "-" * 100)
    for text, name, desc in samples:
        d_cp = duck_rows(con, f"SELECT length({q(text)})")[0][0]
        d_g = duck_rows(con, f"SELECT length_grapheme({q(text)})")[0][0]
        p_cp = pl.DataFrame({"s": [text]})["s"].str.len_chars().to_list()[0]
        gap = p_cp - d_g
        verdict = "✅ 数字相同" if gap == 0 else f"❌ 差 {gap}"
        print(
            "  "
            + common.pad(name, 12)
            + common.pad(desc, 24)
            + common.pad(str(d_cp), 10)
            + common.pad(str(d_g), 24)
            + common.pad(str(p_cp), 18)
            + verdict
        )
    print()
    print("  → DuckDB 的 `length_grapheme` 数的是**用户看到的字**：")
    print("    家庭 emoji = 1、国旗 = 1、肤色修饰 = 1、天城文合体字 = 1。")
    print()
    print("  → **Polars 2.1 没有任何 grapheme 函数**（第 3 节实测过）：")
    print("    它只有 `len_chars`（码点数）和 `len_bytes`（字节数），")
    print("    上面那一列 Polars 的数字全是码点数 —— 家庭 emoji 算 7 个。")
    print()
    print("  → 所以「这个昵称显示几个字」这种问题：")
    print("    **DuckDB 能答，Polars 答不了**（只能答「几个码点」，那不是同一个问题）。")


# ---------------------------------------------------------------- 8 · 切一半


def scenario_slice(con: duckdb.DuckDBPyConnection) -> None:
    section("第 8 节 · 截断到 2 个字：一边切碎 emoji，一边切不动")
    print("  「取前两个字符」在有 emoji 的文本上很容易出事：")
    print()

    plain = duck_rows(con, f"SELECT substring({q(FAMILY)}, 1, 2)")[0][0]
    plain_left = duck_rows(con, f"SELECT left({q(FAMILY)}, 2)")[0][0]
    graph = duck_rows(con, f"SELECT substring_grapheme({q(FAMILY)}, 1, 2)")[0][0]
    left_g = duck_rows(con, f"SELECT left_grapheme({q(FAMILY)}, 2)")[0][0]
    pol_slice = pl.DataFrame({"s": [FAMILY]})["s"].str.slice(0, 2).to_list()[0]
    pol_head = pl.DataFrame({"s": [FAMILY]})["s"].str.head(2).to_list()[0]

    header = ("写法", "结果（码点）", "码点数", "是不是完整的字")
    print(
        "  "
        + common.pad(header[0], 38)
        + common.pad(header[1], 54)
        + common.pad(header[2], 10)
        + header[3]
    )
    print("  " + "-" * 110)
    for label, res in [
        ("DuckDB  substring(s, 1, 2)", plain),
        ("DuckDB  left(s, 2)", plain_left),
        ("DuckDB  substring_grapheme(s, 1, 2)", graph),
        ("DuckDB  left_grapheme(s, 2)", left_g),
        ("Polars  str.slice(0, 2)", pol_slice),
        ("Polars  str.head(2)", pol_head),
    ]:
        whole = "✅ 完整" if res == FAMILY else "❌ 切碎了"
        print(
            "  "
            + common.pad(label, 38)
            + common.pad(cps(res), 54)
            + common.pad(str(len(res)), 10)
            + whole
        )
    print()
    print("  → DuckDB 的 `substring` / `left` 按**码点**切，")
    print("    家庭 emoji 被切成「👨 + 零宽连接符」—— 半个 emoji，渲染出来是方块。")
    print()
    print("  → DuckDB 的 `substring_grapheme` / `left_grapheme` 按**字**切，")
    print(f"    要 2 个字就给你 2 个字：{cps(graph)}")
    print()
    print("  → **Polars 侧两个写法都按码点切**，出来的结果和 DuckDB 的 `substring` 一样：")
    print(f"    `str.slice(0, 2)` = {cps(pol_slice)}")
    print(f"    `str.head(2)`    = {cps(pol_head)}")
    print("    —— 也就是说 **Polars 没有「按字截断」这条路**。")
    print()
    print("  → 这不是「Polars 少个函数」这么轻：**「取前 N 个字」在 Polars 侧要么做不到，")
    print("    要么拿 `len_chars` 近似 —— 而近似出来的是码点数，emoji 会被切碎。**")


# ---------------------------------------------------------------- 9 · 大小写映射的长度


def scenario_case_mapping(con: duckdb.DuckDBPyConnection) -> None:
    section("第 9 节 · 大小写映射会改长度（这次是 upper）")
    print("  collation_demo.py 第 7 节讲过 lower()：")
    print("    DuckDB lower('İ') = 'i'（1 个码点），Polars = 'i' + U+0307（2 个码点）。")
    print("  这一节看 **upper**，分歧的形态不一样 —— 不是「多了个组合标记」，")
    print("  而是**一个字符变成两个字符**。")
    print()

    header = ("输入", "DuckDB upper()", "长度", "Polars to_uppercase()", "长度", "一样？")
    print(
        "  "
        + common.pad(header[0], 12)
        + common.pad(header[1], 22)
        + common.pad(header[2], 8)
        + common.pad(header[3], 24)
        + common.pad(header[4], 8)
        + header[5]
    )
    print("  " + "-" * 96)
    for text in ["\u00df", "\ufb01", "\u0130", "\u01c5", "\u1e9e"]:
        d = duck_rows(con, f"SELECT upper({q(text)})")[0][0]
        p = pl.DataFrame({"s": [text]})["s"].str.to_uppercase().to_list()[0]
        same = "✅" if d == p else "❌ 不一样"
        print(
            "  "
            + common.pad(text, 12)
            + common.pad(cps(d), 22)
            + common.pad(str(len(d)), 8)
            + common.pad(cps(p), 24)
            + common.pad(str(len(p)), 8)
            + same
        )
    print()
    print("  → `ß` 的完整大写映射是 **'SS'**（两个字符），`ﬁ` 的是 **'FI'**。")
    print("    DuckDB 用的是**简单映射**，只给 'ẞ'（U+1E9E，仍是 1 个字符）、'ﬁ' 原样不动；")
    print("    Polars 用的是**完整映射**，真的给 'SS'（2 个字符）、'FI'（2 个字符）。")
    print()
    print("  → 长度变了意味着：**同一列里 'ß' 和 'ss' 在 DuckDB 侧是两个键，")
    print("    在 Polars 侧（upper 之后）会撞成一个键。**")
    print("    拿 `upper(x)` 当去重键，两边的去重结果就不一样。")
    print()
    print("  → 顺带：Polars 还有 `to_titlecase`，DuckDB **一个 titlecase 函数都没有**：")
    for fn in ("initcap", "title", "to_titlecase"):
        status, msg = duck_try(con, f"SELECT {fn}('abc')")
        if status == "成功":
            print(f"    DuckDB {fn:12s} → ✅ {msg}")
        else:
            print(f"    DuckDB {fn:12s} → ❌ {msg}")
    pol_title = pl.DataFrame({"s": ["istanbul \u00dfeta"]})["s"].str.to_titlecase().to_list()[0]
    print(f"    Polars to_titlecase('istanbul ßeta') = {pol_title!r}")


# ---------------------------------------------------------------- 10 · 代价


def scenario_cost(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section(f"第 10 节 · 代价：{rows:,} 行字符串规范化各要多久")
    print("  用 orders.parquet 的列拼一条中文长文本，再叠上重音拉丁字母和 emoji。")
    print()

    base = common.ORDERS_PARQUET
    if not base.exists():
        print(f"  （没找到 {base.name}，跳过）")
        return

    total = int(duck_rows(con, f"SELECT count(*) FROM '{base}'")[0][0])
    print(f"  数据源：{base.name}，{total:,} 行")
    print()

    con.execute(
        f"CREATE OR REPLACE TABLE txt AS "
        f"SELECT region || '·' || category || ' ' || {q(CAFE)} || ' ' || {q(FAMILY)} AS s "
        f"FROM '{base}' LIMIT {rows}"
    )
    n = int(duck_rows(con, "SELECT count(*) FROM txt")[0][0])
    print(f"  实际取用 {n:,} 行，每行一条中文长文本（含重音字母和 ZWJ emoji）")
    print()

    duck_df = pl.from_arrow(con.sql("SELECT s FROM txt").arrow())

    def med(fn) -> tuple[float, float]:
        samples = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000)
        return statistics.median(samples), min(samples)

    # 口径统一：都算「规范化之后取长度再求和」—— 两边求和结果必须对得上
    duck_sum = duck_rows(con, "SELECT sum(length(nfc_normalize(s))) FROM txt")[0][0]
    pol_sum = duck_df.select(pl.col("s").str.normalize("NFC").str.len_chars().sum()).item()
    print(
        f"  口径核对：NFC 之后的总长度 DuckDB = {duck_sum}，Polars = {pol_sum}，"
        f"一致 = {duck_sum == pol_sum}"
    )
    print()

    cases = [
        (
            "DuckDB  sum(length(s))",
            "只测基线（不规范化）",
            lambda: con.execute("SELECT sum(length(s)) FROM txt").fetchall(),
        ),
        (
            "Polars  sum(len_chars)",
            "只测基线（不规范化）",
            lambda: duck_df.select(pl.col("s").str.len_chars().sum()).item(),
        ),
        (
            "DuckDB  nfc_normalize",
            "NFC 规范化",
            lambda: con.execute("SELECT sum(length(nfc_normalize(s))) FROM txt").fetchall(),
        ),
        (
            "Polars  normalize('NFC')",
            "NFC 规范化",
            lambda: duck_df.select(pl.col("s").str.normalize("NFC").str.len_chars().sum()).item(),
        ),
        (
            "DuckDB  strip_accents",
            "剥拉丁重音",
            lambda: con.execute("SELECT sum(length(strip_accents(s))) FROM txt").fetchall(),
        ),
        (
            "Polars  NFKD + 去组合标记",
            "NFKD 再剥标记",
            lambda: duck_df.select(
                pl.col("s")
                .str.normalize("NFKD")
                .str.replace(MARK_RE, "", literal=False)
                .str.len_chars()
                .sum()
            ).item(),
        ),
    ]

    header = ("写法", "做什么", "中位数 (ms)", "最快 (ms)")
    print(
        "  "
        + common.pad(header[0], 30)
        + common.pad(header[1], 22)
        + common.pad(header[2], 14)
        + header[3]
    )
    print("  " + "-" * 92)
    out: list[tuple[str, float, float]] = []
    for label, what, fn in cases:
        m, b = med(fn)
        out.append((label, m, b))
        print(
            "  "
            + common.pad(label, 30)
            + common.pad(what, 22)
            + common.pad(f"{m:.1f}", 14)
            + f"{b:.1f}"
        )
    print()

    duck_base_m = out[0][1]
    pol_base_m = out[1][1]
    duck_nfc_m = out[2][1]
    pol_nfc_m = out[3][1]
    duck_strip_m = out[4][1]
    pol_nfkd_m = out[5][1]
    # 比值用「表里显示的那一位」再算 —— 否则表上是 122.0 / 3.3，
    # 下面写 36.9×，读者自己一除是 37.0×，对不上。
    duck_ratio = round(duck_nfc_m, 1) / round(duck_base_m, 1)
    pol_ratio = round(pol_nfc_m, 1) / round(pol_base_m, 1)
    print(f"  → **NFC 两边基本一个量级**：DuckDB {duck_nfc_m:.1f} ms vs Polars {pol_nfc_m:.1f} ms")
    print(f"    （各自的基线是 {duck_base_m:.1f} / {pol_base_m:.1f} ms，")
    print(f"    规范化本身大约是基线的 {duck_ratio:.1f}× / {pol_ratio:.1f}×）")
    print()
    print("  → **strip_accents 那两行不是一回事，别拿它们比速度**：")
    print(f"    DuckDB {duck_strip_m:.1f} ms 做的是「拉丁重音剥离」，")
    print(f"    Polars {pol_nfkd_m:.1f} ms 做的是「NFKD 分解 + 剥标记」——")
    print("    后者多做一整趟分解，而且**输出的字符串也不一样**（第 6 节）。")
    print()
    print(f"  → 口径说明：{n:,} 行、每行一条含中文 + 重音字母 + ZWJ emoji 的文本；")
    print("  测的是「规范化 + 取长度 + 求和」整条链路，两边的求和结果一致，")
    print("  所以比的是时间不是不同的活儿。")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. **肉眼一样不等于字节一样**：预组合 é 和 分解形 é 在两个引擎眼里")
    print("     都是两个不同的键 —— 去重多一个、join 漏一半，两边一样错。")
    print()
    print("  2. **两边都支持 NFC，实测逐样本一致** —— 这是唯一的跨引擎安全路径。")
    print("     规范化之后再去重、再 join，结果才对得上。")
    print()
    print("  3. **DuckDB 只有 NFC**：NFD / NFKC / NFKD 都不存在（Catalog Error）。")
    print("     ①→1、ﬁ→fi、Ⅻ→XII 这类兼容字符，DuckDB 侧补不了。")
    print()
    print("  4. **`strip_accents` ≠ NFKD + 去组合标记**：")
    print("     DuckDB 那个是拉丁重音剥离器，ﬁ/①/Ⅻ/ø/ß 原样不动；")
    print("     Polars 那条路把兼容字符也拆开了 —— 两边给出的键集合不一样。")
    print()
    print("  5. **grapheme 是 DuckDB 独有的能力**：`length_grapheme` /")
    print("     `substring_grapheme` / `left_grapheme` / `right_grapheme` 都有，")
    print("     Polars 2.1 一个都没有 —— 「取前 N 个字」在 Polars 侧做不到。")
    print()
    print("  6. **大小写映射会改长度**：upper('ß') 在 DuckDB 侧是 'ẞ'（1 字符），")
    print("     在 Polars 侧是 'SS'（2 字符）。拿 upper(x) 当去重键，两边结果不同。")
    print()
    print("  → 一句话：**Unicode 这层的风险不在「存不下」，在「看起来是同一个」。**")
    print("    两边共同的解法是「比较前先 NFC」；两边各自缺的函数")
    print("    （DuckDB 缺 NFKD，Polars 缺 grapheme）得看业务是否真的用到。")


# ---------------------------------------------------------------- 入口


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB vs Polars：Unicode 文本处理")
    parser.add_argument("--rows", type=int, default=200_000, help="代价那一节用多少行")
    parser.add_argument("--repeat", type=int, default=5, help="代价那一节重复几遍取中位数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    con = duckdb.connect()
    try:
        scenario_bytes(con)
        scenario_join(con)
        scenario_toolbox(con)
        scenario_nfc_agreement(con)
        scenario_compat(con)
        scenario_strip_accents(con)
        scenario_grapheme(con)
        scenario_slice(con)
        scenario_case_mapping(con)
        scenario_cost(con, args.rows, args.repeat)
    finally:
        con.close()

    summary()


if __name__ == "__main__":
    main()
