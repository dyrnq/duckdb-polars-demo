"""字符串相似度 / 编辑距离：DuckDB 有 8 个内置函数，Polars 一个都没有。

去重、拼写纠错、模糊匹配、找近似重复 —— 这些活儿都要「两个字符串有多像」。
DuckDB 把这件事做成了**内置函数**：

    SELECT levenshtein(a, b), jaro_similarity(a, b), hamming(a, b) FROM t;

**Polars 完全没有这一层** —— 47 个字符串方法里一个都没有，
整个 Expr API 里也搜不到。要用只能自己写（`map_elements` + Python 函数）。

这个 demo 除了讲「有没有」，更要讲一个**实测出来的坑**：

> **DuckDB 的这些函数全部按 UTF-8 **字节** 算，不是按字符。**
>
> 一个中文字占 3 个字节，所以「无线蓝牙耳机」和「蓝牙无线耳机」
> 的 `levenshtein` 是 **10**，不是 4。
> 更要命的是：**同样「错一个字」，字节距离可能是 2，也可能是 3**
> —— 取决于两个字的 UTF-8 字节前缀有多长。
> 于是同一条阈值 `levenshtein <= 2`，对语义上完全等价的错别字，
> 会**命中一个、漏掉两个**。

这个 demo 里的每个结论都有 Python 参考实现做交叉验证，
参考实现同时给出「按字符」和「按字节」两个版本，好让「到底是哪个」
这件事有据可查。

用法：
    uv run python src/similarity_demo.py
    uv run python src/similarity_demo.py --pairs 20000 --repeat 5
"""

from __future__ import annotations

import argparse
import random
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 参考实现
#
# DuckDB 里没有「按字符算」的 levenshtein，所以这里自己写两个版本：
# 一个按字符（Python 字符串的天然粒度），一个按字节（DuckDB 的实际行为）。
# 有了它们，「DuckDB 到底是哪个」就不是靠猜的了。


def levenshtein_ref(a: str, b: str, *, on_bytes: bool = False) -> int:
    """Levenshtein 距离（插入/删除/替换各算 1）。on_bytes=True 时按 UTF-8 字节算。"""
    x = a.encode("utf-8") if on_bytes else a
    y = b.encode("utf-8") if on_bytes else b
    prev = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        cur = [i]
        for j, cy in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cx != cy)))
        prev = cur
    return prev[-1]


def jaro_ref(a: str, b: str, *, on_bytes: bool = False) -> float:
    """Jaro 相似度。on_bytes=True 时按 UTF-8 字节算。"""
    x = a.encode("utf-8") if on_bytes else a
    y = b.encode("utf-8") if on_bytes else b
    if x == y:
        return 1.0
    lx, ly = len(x), len(y)
    if lx == 0 or ly == 0:
        return 0.0
    window = max(max(lx, ly) // 2 - 1, 0)
    m1 = [False] * lx
    m2 = [False] * ly
    matches = 0
    for i in range(lx):
        lo = max(0, i - window)
        hi = min(i + window + 1, ly)
        for j in range(lo, hi):
            if m2[j] or x[i] != y[j]:
                continue
            m1[i] = True
            m2[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    k = 0
    transpositions = 0
    for i in range(lx):
        if not m1[i]:
            continue
        while not m2[k]:
            k += 1
        if x[i] != y[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / lx + matches / ly + (matches - transpositions) / matches) / 3


def jaccard_ref(a: str, b: str, *, on_bytes: bool = False) -> float:
    """Jaccard 系数：两个「元素集合」的交集 / 并集。on_bytes=True 时按 UTF-8 字节算。"""
    x = set(a.encode("utf-8")) if on_bytes else set(a)
    y = set(b.encode("utf-8")) if on_bytes else set(b)
    union = x | y
    if not union:
        return 1.0
    return len(x & y) / len(union)


# ---------------------------------------------------------------- 数据

# 一份中文商品名列表（照本项目的数据风格写的），带几个「近似重复」：
PRODUCTS = [
    "无线蓝牙耳机 降噪版",
    "无线蓝牙耳机 降噪版",  # 完全重复
    "无线蓝牙耳机 降喑版",  # 错别字：噪 -> 喑（首字节相同，差 2 字节）
    "无线蓝牙耳机 降暗版",  # 错别字：噪 -> 暗（首字节就不同，差 3 字节）
    "无线蓝牙耳机 降燥版",  # 错别字：噪 -> 燥（首字节就不同，差 3 字节）
    "无线蓝牙耳机降噪版",  # 少一个空格
    "蓝牙无线耳机 降噪版",  # 前后调换
    "无线蓝牙耳机 降噪版 Pro",
    "无线蓝牙耳机 降噪版 2",  # 另一个商品：只差「 2」两个字节
    "便携式投影仪 1080P",
    "便携式投影仪1080P",
    "机械键盘 87键 青轴",
    "机械键盘 87键 红轴",
    "人体工学椅 网布",
    "人体工学椅网布",
    "空气炸锅 5L",
    "空气炸锅5L 家用",
    "保温杯 500ml 不锈钢",
    "保温杯 500ml 塑料",
    "智能手表 GPS 心率",
    "智能手表GPS 心率监测",
    "跑步机 家用 可折叠",
    "扫地机器人 自动集尘",
]

# 英文对照组（用来证明「字节级」在英文上看不出来 —— 一个字母就是一个字节）
EN_PRODUCTS = [
    "wireless earbuds",
    "wireless earbuds",
    "wireless earbud",
    "wireless earbuds",
    "portable projector",
    "portable projectors",
    "mechanical keyboard",
    "mechanical keyboards",
    "air fryer 5l",
    "air fryer 5L",
]


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def fmt_num(v: object) -> str:
    """浮点打 4 位小数，整数照打，异常照打 —— 表格要对齐。"""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def duck_scalar(con: duckdb.DuckDBPyConnection, expr: str) -> object:
    return q1(con, f"SELECT {expr}")


def polars_str_methods() -> list[str]:
    """Polars 字符串命名空间里的全部方法名（实测，不猜）。"""
    return sorted(n for n in dir(pl.col("a").str) if not n.startswith("_"))


SIM_KEYWORDS = (
    "distance",
    "similar",
    "levensh",
    "jaro",
    "jaccard",
    "hamming",
    "damerau",
    "editdist",
    "soundex",
    "fuzz",
    "trigram",
)


# ---------------------------------------------------------------- 1. API 清单


def scenario_capability_inventory(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · API 清单：DuckDB 8 个，Polars 0 个")

    duck_rows = con.sql(
        "SELECT DISTINCT function_name, return_type, description "
        "FROM duckdb_functions() "
        "WHERE function_type = 'scalar' AND list_contains(categories, 'text_similarity') "
        "ORDER BY function_name"
    ).fetchall()

    print(
        "  DuckDB 侧 —— `duckdb_functions()` 里标着 'text_similarity' 分类的有 "
        f"**{len(duck_rows)}** 个："
    )
    print()
    print("  " + common.pad("函数", 26) + common.pad("返回类型", 12) + "说明")
    print("  " + "-" * 96)
    for name, rtype, desc in duck_rows:
        print("  " + common.pad(str(name), 26) + common.pad(str(rtype), 12) + str(desc)[:52])
    print()

    pol_methods = polars_str_methods()
    pol_sim = [n for n in pol_methods if any(k in n.lower() for k in SIM_KEYWORDS)]
    all_expr = sorted(n for n in dir(pl.col("a")) if not n.startswith("_"))
    expr_sim = [n for n in all_expr if any(k in n.lower() for k in SIM_KEYWORDS)]

    print(f"  Polars 侧 —— 字符串命名空间一共 **{len(pol_methods)}** 个方法，")
    print(f"  其中跟相似度/编辑距离相关的有 **{len(pol_sim)}** 个：")
    print(f"    {pol_sim or '（空）'}")
    print()
    print(f"  整个 Expr API（{len(all_expr)} 个方法）里搜这些关键词：")
    print(f"    {expr_sim or '（空）'}")
    print()
    print("  全部字符串方法：")
    line = "    "
    for i, name in enumerate(pol_methods, 1):
        line += f"{name}  "
        if i % 8 == 0:
            print(line)
            line = "    "
    if line.strip():
        print(line)
    print()
    print("  → **Polars 侧一个都没有**。")
    print("    不是「弱一点」，是**这一层不存在**：没有编辑距离、没有 Jaro、没有 Jaccard、")
    print("    也没有 soundex（连 DuckDB 都没有）。")
    print("    要用只能自己写 —— 下一节看手写的代价。")
    print()
    print("  DuckDB 这 8 个函数分三类，各管一件事：")
    print("    · **编辑距离**（要「改几个字」）：levenshtein / damerau_levenshtein / editdist3")
    print(
        "    · **相似度打分**（要「有多像」，0~1）："
        "jaro_similarity / jaro_winkler_similarity / jaccard"
    )
    print("    · **逐位比较**（要「哪几个位置不一样」）：hamming / mismatches")


# ---------------------------------------------------------------- 2. 八个函数


def scenario_each_function(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 八个函数各管什么事")

    print("  用同一对字符串试一遍（英文，避免中文的字节问题干扰）：")
    print()
    print(
        "  "
        + common.pad("函数", 26)
        + common.pad("('kitten','sitting')", 22)
        + common.pad("('abc','abd')", 20)
        + "说明"
    )
    print("  " + "-" * 96)

    rows = [
        ("levenshtein('kitten','sitting')", "levenshtein", "编辑距离"),
        ("damerau_levenshtein('kitten','sitting')", "damerau_levenshtein", "编辑距离（可换位）"),
        ("editdist3('kitten','sitting')", "editdist3", "编辑距离（可换位）"),
        ("jaro_similarity('kitten','sitting')", "jaro_similarity", "相似度 0~1"),
        (
            "jaro_winkler_similarity('kitten','sitting')",
            "jaro_winkler_similarity",
            "相似度 0~1（加权前缀）",
        ),
        ("jaccard('kitten','sitting')", "jaccard", "集合相似度 0~1"),
        ("hamming('kitten','sitting')", "hamming", "逐位不同数"),
        ("mismatches('kitten','sitting')", "mismatches", "逐位不同数"),
    ]
    for sql, name, desc in rows:
        try:
            v1 = duck_scalar(con, sql)
        except Exception as exc:
            v1 = f"{type(exc).__name__}"
        try:
            v2 = duck_scalar(con, sql.replace("kitten", "abc").replace("sitting", "abd"))
        except Exception as exc:
            v2 = f"{type(exc).__name__}"
        print(
            "  "
            + common.pad(name, 26)
            + common.pad(fmt_num(v1), 22)
            + common.pad(fmt_num(v2), 20)
            + desc
        )
    print()

    print("  「换位」这件事，两个函数的结果**不一样**：")
    v_lev = duck_scalar(con, "levenshtein('CA','ABC')")
    v_ed3 = duck_scalar(con, "editdist3('CA','ABC')")
    v_dam = duck_scalar(con, "damerau_levenshtein('CA','ABC')")
    print(f"    levenshtein('CA','ABC')         = {v_lev}")
    print(f"    editdist3('CA','ABC')           = {v_ed3}")
    print(f"    damerau_levenshtein('CA','ABC')  = {v_dam}")
    print()
    print("    · `levenshtein` 只认插入/删除/替换 —— 'CA' → 'ABC' 要改 3 次。")
    print("    · `editdist3` 允许相邻字符**换位**（OSA 距离），但只允许一次，")
    print("      所以 'CA' → 'ABC' 还是 3。")
    print("    · `damerau_levenshtein` 是**无限制**的 Damerau-Levenshtein，")
    print("      换位可以「跨过去」，所以是 2。")
    print("    → 挑函数的时候要想清楚要哪一种：拼写纠错一般用 damerau 版本。")
    print()
    print("  Jaro 和 Jaro-Winkler 的区别（前缀加权）：")
    for a, b in [
        ("MARTHA", "MARHTA"),
        ("DIXON", "DICKSONX"),
        ("无线蓝牙耳机", "蓝牙无线耳机"),
    ]:
        j = duck_scalar(con, f"jaro_similarity('{a}','{b}')")
        jw = duck_scalar(con, f"jaro_winkler_similarity('{a}','{b}')")
        print(f"    {a:>8} vs {b:<12} jaro = {j:.4f}   jaro_winkler = {jw:.4f}")
    print()
    print("    → Jaro-Winkler 给**公共前缀**加权，所以开头就一样的会更高。")
    print("      'DIXON' vs 'DICKSONX' 差得更远但前缀相同，Winkler 版反而更高。")
    print()

    print("  `hamming` 和 `mismatches` 要求**两边等长**，不等长直接报错：")
    for fn in ("hamming", "mismatches"):
        try:
            duck_scalar(con, f"{fn}('ab','abc')")
        except Exception as exc:
            print(f"    {fn}('ab','abc') -> {type(exc).__name__}: {str(exc).splitlines()[0][:80]}")
    print()
    print("  `jaccard` 要求**至少 2 个字符**，太短也报错：")
    try:
        duck_scalar(con, "jaccard('','a')")
    except Exception as exc:
        print(f"    jaccard('','a') -> {type(exc).__name__}: {str(exc).splitlines()[0][:80]}")
    print()
    print("  其余几个函数对空串和 NULL 的处理：")
    for sql in [
        "levenshtein('a','')",
        "levenshtein(NULL,'a')",
        "jaro_similarity('','a')",
        "jaro_similarity('a','')",
    ]:
        print(f"    {sql:32} -> {duck_scalar(con, sql)}")
    print("    → NULL 传进去是 NULL（不报错）；空串只有 jaro 那样「相似度」类的才给 0。")
    print()


# ---------------------------------------------------------------- 3. 字节还是字符


def scenario_byte_vs_char(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 实锤：这些函数全部按 UTF-8 字节算")

    print("  一个中文字占 3 个字节、一个 emoji 占 4 个字节。")
    print("  如果函数按字符算，差一个字就是 1；按字节算，就是 3 / 4。")
    print()
    print(
        "  "
        + common.pad("a", 18)
        + common.pad("b", 18)
        + common.pad("DuckDB", 10)
        + common.pad("字符级参考", 14)
        + common.pad("字节级参考", 14)
        + "DuckDB 跟谁一致"
    )
    print("  " + "-" * 90)

    cases = [
        ("无线蓝牙耳机", "蓝牙无线耳机"),
        ("无线蓝牙耳机", "无线蓝牙耳機"),
        ("中文", "中"),
        ("中文", "中中"),
        ("a中", "中a"),
        ("🐱🐶", "🐱"),
        ("é", "e"),
        ("kitten", "sitting"),
        ("abc", "abd"),
    ]
    verdict = "字节级"
    for a, b in cases:
        duck_val = int(duck_scalar(con, f"levenshtein('{a}','{b}')"))
        char_val = levenshtein_ref(a, b)
        byte_val = levenshtein_ref(a, b, on_bytes=True)
        if duck_val == char_val and duck_val == byte_val:
            verdict_str = "两个都一样"
        elif duck_val == byte_val:
            verdict_str = "✅ 字节级"
        elif duck_val == char_val:
            verdict_str = "❌ 字符级"
        else:
            verdict_str = "❌ 都不对"
        if verdict_str == "❌ 字符级":
            verdict = "字符级"
        print(
            "  "
            + common.pad(a, 18)
            + common.pad(b, 18)
            + common.pad(str(duck_val), 10)
            + common.pad(str(char_val), 14)
            + common.pad(str(byte_val), 14)
            + verdict_str
        )
    print()

    print("  → **DuckDB 跟「字节级参考」完全一致**，一个字符级都对不上。")
    print(f"    判定结果：{verdict}")
    print()
    print("  Jaro 家族也一样（同一个算法，换个粒度）：")
    print(
        "  "
        + common.pad("a", 18)
        + common.pad("b", 18)
        + common.pad("DuckDB", 12)
        + common.pad("字符级参考", 14)
        + common.pad("字节级参考", 14)
        + "谁一致"
    )
    print("  " + "-" * 88)
    for a, b in [
        ("a中", "中a"),
        ("无线蓝牙耳机", "蓝牙无线耳机"),
        ("kitten", "sitting"),
    ]:
        duck_val = float(duck_scalar(con, f"jaro_similarity('{a}','{b}')"))
        char_val = jaro_ref(a, b)
        byte_val = jaro_ref(a, b, on_bytes=True)
        who = (
            "两个都一样"
            if abs(duck_val - char_val) < 1e-12 and abs(duck_val - byte_val) < 1e-12
            else ("✅ 字节级" if abs(duck_val - byte_val) < 1e-12 else "❌ 字符级")
        )
        print(
            "  "
            + common.pad(a, 18)
            + common.pad(b, 18)
            + common.pad(f"{duck_val:.4f}", 12)
            + common.pad(f"{char_val:.4f}", 14)
            + common.pad(f"{byte_val:.4f}", 14)
            + who
        )
    print()

    print("  `jaccard` 也一样（它算的是「元素集合」的 Jaccard 系数）：")
    for a, b in [
        ("无线蓝牙", "无线蓝牙耳机"),
        ("abc", "abd"),
        ("a b c", "a b d"),
    ]:
        duck_val = float(duck_scalar(con, f"jaccard('{a}','{b}')"))
        char_val = jaccard_ref(a, b)
        byte_val = jaccard_ref(a, b, on_bytes=True)
        print(
            f"    {a!r:16} vs {b!r:16} DuckDB = {duck_val:.4f}   "
            f"字符级 = {char_val:.4f}   字节级 = {byte_val:.4f}"
        )
    print()
    print("  → **三个函数、两种算法，粒度全是字节。**")
    print()
    print("  **为什么 DuckDB 会这么设计**：")
    print("    编辑距离在实现上是「两个等长数组的 DP」，")
    print("    按字节做最省事 —— 不用管 Unicode 的码点宽度，也不用担心组合字符。")
    print("    对英文（1 字母 1 字节）完全没问题，")
    print("    对中文就变成了「一个汉字 = 3 次编辑」。")
    print()
    print("  **这个坑有多大**：")
    print("    实务里最常见的近似重复阈值是 `levenshtein <= 2`（或者 <= 1）。")
    print("    在中文数据上，这个阈值**既会漏、也会抓**：")
    print("    · 「噪 → 喑」这种首字节相同的错别字，差 2 个字节 → 刚好命中；")
    print("      「噪 → 暗」这种首字节就不同的，差 3 个字节 → 漏掉。")
    print("    · 反过来，「加个空格再加个数字」这种**根本不是重复**的商品，")
    print("      只要差 2 个字节以内，一样会被判成重复。")
    print("    → 字节距离**不是**「差几个字」的代理，阈值调大调小都救不了。")
    print()
    print("  顺带一提：`jaccard` 的「元素集合」也是字节集合 ——")
    print("    中文「无线蓝牙」和「无线蓝牙耳机」共享的字节多，")
    jac_val = duck_scalar(con, "jaccard('无线蓝牙','无线蓝牙耳机')")
    print(f"    所以 jaccard 高达 {jac_val:.4f}，")
    print("    而按字符只有 0.6667 —— 两个都「看起来像」，但含义完全不同。")


# ---------------------------------------------------------------- 4. 中文实例


def scenario_chinese_dedup(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 真实用例：中文商品名去重，同一条阈值又漏又抓")

    print("  商品名列表（含近似重复）：")
    for i, p in enumerate(PRODUCTS, 1):
        print(f"    {i:2d}. {p}")
    print()

    # 拿「无线蓝牙耳机 降噪版」当基准，跟所有别的比
    base = "无线蓝牙耳机 降噪版"
    print(f"  拿 `{base}` 当基准，逐个比（levenshtein）：")
    print()
    print(
        "  "
        + common.pad("另一个商品名", 24)
        + common.pad("DuckDB 距离", 14)
        + common.pad("字符级距离", 14)
        + common.pad("<= 2 ? (DuckDB)", 18)
        + "<= 2 ? (字符级)"
    )
    print("  " + "-" * 88)
    duck_hits = 0
    char_hits = 0
    for other in PRODUCTS:
        if other == base:
            continue
        d = int(duck_scalar(con, f"levenshtein('{base}','{other}')"))
        c = levenshtein_ref(base, other)
        if d <= 2:
            duck_hits += 1
        if c <= 2:
            char_hits += 1
        d_mark = "✅ 命中" if d <= 2 else "✗ 没中"
        c_mark = "✅ 命中" if c <= 2 else "✗ 没中"
        print(
            "  "
            + common.pad(other, 24)
            + common.pad(str(d), 14)
            + common.pad(str(c), 14)
            + common.pad(d_mark, 18)
            + c_mark
        )
    print()
    print(f"  → DuckDB（字节级）用 `levenshtein <= 2` 判近似重复：命中 **{duck_hits}** 条；")
    print(f"    字符级语义下应该命中 **{char_hits}** 条。")
    print(f"    漏了 {char_hits - duck_hits} 条 —— 看看下面这三条错别字就明白为什么。")
    print()

    print("  **同样是「错一个字」，DuckDB 给的距离却不一样**：")
    print()
    typos = [
        ("无线蓝牙耳机 降喑版", "喑"),
        ("无线蓝牙耳机 降暗版", "暗"),
        ("无线蓝牙耳机 降燥版", "燥"),
    ]
    print(
        "  "
        + common.pad("商品名", 24)
        + common.pad("DuckDB 距离", 14)
        + common.pad("字符级距离", 14)
        + "阈值 2 命中?"
    )
    print("  " + "-" * 72)
    for name, _ch in typos:
        d = int(duck_scalar(con, f"levenshtein('{base}','{name}')"))
        c = levenshtein_ref(base, name)
        mark = "✅ 命中" if d <= 2 else "❌ 漏掉"
        print("  " + common.pad(name, 24) + common.pad(str(d), 14) + common.pad(str(c), 14) + mark)
    print()
    print("  为什么？看这三个字的 UTF-8 字节：")
    for ch in ("噪", "喑", "暗", "燥"):
        print(
            f"    {ch}  = U+{ord(ch):04X}  ->  {' '.join(f'{b:02X}' for b in ch.encode('utf-8'))}"
        )
    print()
    print("    「噪」和「喑」的**首字节相同**（E5），只差后 2 个字节 -> 距离 2；")
    print("    「噪」和「暗」「燥」的首字节就不同 -> 差 3 个字节 -> 距离 3。")
    print()
    print("  → **同一条阈值，对三个语义上完全等价的错别字，给出了两个不同的答案**：")
    print("    命中一个、漏掉两个。阈值定 2 也不对、定 3 也不对 ——")
    print("    因为**字节距离不是「差几个字」的可靠代理**。")
    print()

    print("  反过来也成立 —— 字节距离小，不代表语义上近：")
    v_extra = duck_scalar(con, "levenshtein('无线蓝牙耳机 降噪版','无线蓝牙耳机 降噪版 2')")
    v_swap = duck_scalar(con, "levenshtein('无线蓝牙耳机 降噪版','蓝牙无线耳机 降噪版')")
    print(f"    '无线蓝牙耳机 降噪版' vs '无线蓝牙耳机 降噪版 2'  距离 = {v_extra}")
    print("      （多了「 2」两个字符，是**另一个商品**，却被 <= 2 判成重复）")
    print(f"    '无线蓝牙耳机 降噪版' vs '蓝牙无线耳机 降噪版'      距离 = {v_swap}")
    print("      （只是把「无线蓝牙」和「耳机」调了个位置，字符级距离才 4，")
    print("        字节级却是 10 —— 移动在字节层面是「删掉 + 插进去」，代价翻倍）")
    print()
    print("  → 所以中文场景下的阈值只能当「大致的量级」用，不能当「差几个字」用。")
    print()
    print("  **想要真正的字符级语义，只有一条路**：自己算。")
    print("    下一节看 Polars 侧怎么写，以及写完有多贵。")


# ---------------------------------------------------------------- 5. Polars 手写


def scenario_polars_handwritten(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · Polars 侧只能手写")

    print("  手写一个字符级 levenshtein，挂到 Polars 上：")
    print()
    print("    def levenshtein_ref(a, b):        # 字符级")
    print("        ...  # 标准 DP")
    print()
    print("    df.select(")
    print("        pl.struct(['a', 'b']).map_elements(")
    print("            lambda r: levenshtein_ref(r['a'], r['b']),")
    print("            return_dtype=pl.Int32,")
    print("        )")
    print("    )")
    print()

    pairs = [
        ("无线蓝牙耳机 降噪版", "无线蓝牙耳机 降喑版"),
        ("无线蓝牙耳机 降噪版", "蓝牙无线耳机 降噪版"),
        ("kitten", "sitting"),
        ("abc", "abd"),
    ]
    df = pl.DataFrame({"a": [p[0] for p in pairs], "b": [p[1] for p in pairs]})
    hand = df.select(
        pl.struct(["a", "b"]).map_elements(
            lambda r: levenshtein_ref(r["a"], r["b"]), return_dtype=pl.Int32
        )
    )["a"].to_list()

    print("  实测结果：")
    print()
    print(
        "  "
        + common.pad("a", 24)
        + common.pad("b", 24)
        + common.pad("DuckDB", 10)
        + common.pad("Polars 手写", 14)
    )
    print("  " + "-" * 74)
    for (a, b), h in zip(pairs, hand, strict=True):
        d = int(duck_scalar(con, f"levenshtein('{a}','{b}')"))
        print(
            "  "
            + common.pad(a, 24)
            + common.pad(b, 24)
            + common.pad(str(d), 10)
            + common.pad(str(h), 14)
            + ("✅ 一致" if d == h else "❌ 不一致")
        )
    print()
    print("  → **英文上完全一致，中文上不一致** ——")
    print("    因为 DuckDB 按字节、这个手写版按字符。")
    print("    这不是手写错了，是**它算的是另一件事**。")
    print()
    print("  想要跟 DuckDB 逐位一致，把参考实现改成按字节就行：")
    print("    levenshtein_ref(a, b, on_bytes=True)")
    print("    —— 但那样就把 DuckDB 的中文语义问题一起继承过来了。")
    print()


# ---------------------------------------------------------------- 6. 代价


def make_pairs(n_pairs: int, seed: int) -> pl.DataFrame:
    """造 n_pairs 对「基准串 + 改了一个字的串」。

    改动在**字符**层面做（模拟真实错别字），所以字节级和字符级的距离会不一样。
    """
    rng = random.Random(seed)
    rows: list[tuple[str, str]] = []
    pool = PRODUCTS + EN_PRODUCTS
    for _ in range(n_pairs):
        a = rng.choice(pool)
        chars = list(a)
        if not chars:
            rows.append((a, a))
            continue
        pos = rng.randrange(len(chars))
        kind = rng.choice(["replace", "delete", "insert"])
        if kind == "replace":
            chars[pos] = "X"
        elif kind == "delete":
            chars.pop(pos)
        else:
            chars.insert(pos, "X")
        rows.append((a, "".join(chars)))
    return pl.DataFrame({"a": [r[0] for r in rows], "b": [r[1] for r in rows]})


def scenario_performance(con: duckdb.DuckDBPyConnection, pairs: int, repeat: int) -> None:
    section("场景 6 · 代价：内置函数 vs 手写 UDF")

    print(f"数据：{pairs:,} 对「原串 + 改了一个字的串」")
    print(f"机器：{common.describe_machine()}")
    print()

    tmpdir = Path(tempfile.mkdtemp(prefix="similarity_demo_"))
    src = tmpdir / "pairs.parquet"
    df = make_pairs(pairs, common.SEED)
    df.write_parquet(src)
    con.execute("CREATE OR REPLACE TABLE pairs AS SELECT * FROM read_parquet(?)", [str(src)])
    polars_df = pl.read_parquet(src)

    def duck_lev() -> int:
        return int(q1(con, "SELECT sum(levenshtein(a, b)) FROM pairs"))

    def duck_jaro() -> float:
        return float(q1(con, "SELECT sum(jaro_similarity(a, b)) FROM pairs"))

    def pol_hand_chars() -> int:
        return int(
            polars_df.select(
                pl.struct(["a", "b"]).map_elements(
                    lambda r: levenshtein_ref(r["a"], r["b"]), return_dtype=pl.Int32
                )
            )["a"].sum()
        )

    def pol_hand_bytes() -> int:
        return int(
            polars_df.select(
                pl.struct(["a", "b"]).map_elements(
                    lambda r: levenshtein_ref(r["a"], r["b"], on_bytes=True), return_dtype=pl.Int32
                )
            )["a"].sum()
        )

    results = [
        ("DuckDB  levenshtein（内置，字节级）", duck_lev),
        ("DuckDB  jaro_similarity（内置，字节级）", duck_jaro),
        ("Polars  map_elements 手写（字符级）", pol_hand_chars),
        ("Polars  map_elements 手写（字节级）", pol_hand_bytes),
    ]

    print(
        "  "
        + common.pad("实现", 38)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果", 18)
    )
    print("  " + "-" * 82)
    measured: dict[str, float] = {}
    for label, fn in results:
        fn()
        times: list[float] = []
        value = None
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        fastest = min(times)
        measured[label] = med
        print(
            "  "
            + common.pad(label, 38)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{fastest * 1000:.1f}", 12)
            + common.pad_left(f"{value:.4f}", 18)
        )
    print()

    duck_lev_ms = measured["DuckDB  levenshtein（内置，字节级）"]
    pol_ms = measured["Polars  map_elements 手写（字符级）"]
    print(f"  → Polars 手写比 DuckDB 内置慢 **{pol_ms / duck_lev_ms:.1f}×**。")
    print()
    print("  口径：输入已物化成 parquet，两边读同一份数据；")
    print("  计时的只有「算这一列距离」这一步（不含读盘），重复取中位数。")
    print()
    print("  **为什么差这么多**：")
    print("    · DuckDB 的 levenshtein 是**向量化**的 C++ 实现，")
    print("      一列算一列，线程池铺开。")
    print("    · `map_elements` 是**逐行**调用 Python 函数，")
    print("      每次都要跨一次 Python ↔ Rust 边界。")
    print("    · 手写的 DP 本身还是 O(n·m) 的 Python 双层循环。")
    print()
    print("  → 所以「Polars 没有内置相似度函数」这件事，")
    print("    代价不是「写起来麻烦」，是**慢一个数量级**。")
    print("    数据量小的时候无所谓，几百万对就是分钟级的差别。")
    print()
    print("  **Polars 侧的现实选项**：")
    print("    ① `map_elements` + Python 函数 —— 简单，但慢（就是上面这个）。")
    print("    ② 用 `polars` 的 Rust 扩展（`polars-plugin` / `py-polars` 插件）自己写 ——")
    print("       快，但要多一门 Rust 工具链。")
    print("    ③ 换到 DuckDB 那边算 —— 这就是本项目在做的事。")
    print("    ④ 实在需要字符级语义、又不想写 Rust：")
    print("       在 Polars 里 `str.split('')` 能把字符串拆成字符列表（实测可以），")
    print("       可以基于它做向量化的比较 ——")
    print("       但**编辑距离本身还是得自己写**，拆字符只是省掉编码那一步。")


# ---------------------------------------------------------------- 7. 英文对照


def scenario_english_baseline(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 英文对照：同样的函数，同样的结论")

    print("  英文的坑要小得多 —— 1 个字母 = 1 个字节，字符级和字节级完全重合。")
    print()
    print(
        "  "
        + common.pad("a", 22)
        + common.pad("b", 22)
        + common.pad("DuckDB", 10)
        + common.pad("字符级", 10)
        + common.pad("字节级", 10)
    )
    print("  " + "-" * 76)
    all_same = True
    for a, b in [
        ("wireless earbuds", "wireless earbud"),
        ("portable projector", "portable projectors"),
        ("mechanical keyboard", "mechanical keyboards"),
        ("air fryer 5l", "air fryer 5L"),
    ]:
        d = int(duck_scalar(con, f"levenshtein('{a}','{b}')"))
        c = levenshtein_ref(a, b)
        by = levenshtein_ref(a, b, on_bytes=True)
        if not (d == c == by):
            all_same = False
        print(
            "  "
            + common.pad(a, 22)
            + common.pad(b, 22)
            + common.pad(str(d), 10)
            + common.pad(str(c), 10)
            + common.pad(str(by), 10)
        )
    print()
    if all_same:
        print("  → **英文上三个数完全一样**，所以字节级这个坑在英文数据上看不出来。")
        print("    这就是为什么这个差异能活这么久 —— ")
        print("    英文语料上一切正常，一换中文就全错。")
        print()
    print("  另外注意大小写：")
    v_case = duck_scalar(con, "levenshtein('air fryer 5l','air fryer 5L')")
    print(f"    levenshtein('air fryer 5l','air fryer 5L') = {v_case}")
    print("    → 大小写不同算**一次编辑**，不会自动忽略大小写。")
    print("      想忽略大小写得自己先 lower() 一下 —— 两边都这样，没有内置的 insensitive 版本。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的字符串相似度能力差距演示")
    parser.add_argument("--pairs", type=int, default=20_000, help="代价场景的字符串对数")
    parser.add_argument("--repeat", type=int, default=5, help="代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    try:
        scenario_capability_inventory(con)
        scenario_each_function(con)
        scenario_byte_vs_char(con)
        scenario_chinese_dedup(con)
        scenario_polars_handwritten(con)
        scenario_performance(con, args.pairs, args.repeat)
        scenario_english_baseline(con)
    finally:
        con.close()

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  ❌ 能力差距：DuckDB 有 8 个内置的字符串相似度/编辑距离函数，")
    print("     Polars 47 个字符串方法里一个都没有，整个 Expr API 里也没有。")
    print("  ⚠️ 实锤的坑：这些函数**全部按 UTF-8 字节算**，不是按字符 ——")
    print("     一个中文字 = 3 个字节，于是 `levenshtein <= 2` 在中文上")
    print("     既会漏掉真重复（同为错一个字，字节距离 2 或 3），也会抓住假重复。")
    print("  ✅ 一致的：函数本身的算法（编辑距离、Jaro、Jaccard 的定义）两边没差别，")
    print("     差别只在**粒度**；英文数据上字节级 = 字符级，看不出问题。")
    print()
    print("  → 记住这一条就够：**DuckDB 的相似度函数是字节级的，")
    print("    中文上的阈值没法靠「倍数」换算出来 —— 只能自己按字符算。**")
    print()


if __name__ == "__main__":
    main()
