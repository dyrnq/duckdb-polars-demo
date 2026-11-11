"""全文本搜索（FTS）：DuckDB 有扩展，Polars 完全没有 —— 一条真正的能力差距。

DuckDB 有一个 `fts` 扩展，能给一张表建**倒排索引**，然后用 BM25 打分做全文检索：

    PRAGMA create_fts_index('docs', 'id', 'content');
    SELECT id, fts_main_docs.match_bm25(id, 'parquet') AS score
    FROM docs
    WHERE fts_main_docs.match_bm25(id, 'parquet') IS NOT NULL
    ORDER BY score DESC;

**Polars 没有任何等价物** —— 不是「功能弱一点」，是**根本没有这一层**：
没有倒排索引、没有 BM25、没有文档频率统计、没有分词器。想要全文检索，只能自己写：

* `str.contains(...)` / `str.contains_any(...)` —— 逐行扫，命中就完事，没有排序；
* `str.split` + `explode` + `group_by` —— 自己造倒排表，能查但每次都要重建。

这个 demo 默认**不联网**：DuckDB 的扩展要下载，不装就跑不了。
所以默认跑的是「不装扩展也能说清楚」的那部分：

1. 两边**能用的 API 清单**对比 —— 实测，不装扩展也不联网；
2. Polars 侧的「硬扫」替代方案 —— 实测它能做什么、不能做什么；
3. 加 `--extension` 才会真的装扩展、跑 BM25、两边比结果和代价。

用法：
    uv run python src/fts_demo.py
    uv run python src/fts_demo.py --extension --rows 20000
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

# ---------------------------------------------------------------- 语料

# 一小份中英混排的文档
DOCS = [
    (1, "DuckDB 是进程内分析型数据库，支持向量化和多核并行执行"),
    (2, "Polars 是 Rust 写的列式 dataframe 引擎，速度快，内存友好"),
    (3, "DuckDB 和 Polars 都能直接读 Parquet 文件，不需要先转格式"),
    (4, "全文检索需要倒排索引，BM25 是最经典的排序算法之一"),
    (5, "SQL 的相关子查询在 DuckDB 里会被优化器改写成连接"),
    (6, "Parquet 是列式存储格式，压缩率高，只读需要的列"),
    (7, "BM25 比 TF-IDF 好在考虑了文档长度和逆文档频率"),
    (8, "数据倾斜会让分组聚合和连接变慢，需要提前治理"),
]

# 查询词：单个英文词 / 英文词 + 中文 / 纯中文
QUERIES = ["parquet", "duckdb 连接", "全文检索"]

# 「FTS 相关」的关键词 —— 用来在两边的 API 清单里找
FTS_KEYWORDS = ("bm25", "fts", "token", "idf", "index", "invert")


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def make_docs(rows: int) -> pl.DataFrame:
    """造一份可扩展的语料：小的时候用手写的，大的时候复制到目标行数。"""
    base = pl.DataFrame(
        {
            "id": [d[0] for d in DOCS],
            "content": [d[1] for d in DOCS],
        },
        schema={"id": pl.Int64, "content": pl.String},
    )
    if rows <= base.height:
        return base.head(rows)
    reps = (rows - base.height) // base.height + 1
    big = pl.concat([base] * reps, how="vertical")
    return big.head(rows).with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias("id"))


def polars_string_functions() -> list[str]:
    """Polars 字符串命名空间里的全部方法名（实测，不猜）。"""
    return sorted(n for n in dir(pl.col("a").str) if not n.startswith("_"))


# ---------------------------------------------------------------- 1. API 清单


def scenario_capability_inventory() -> None:
    section("场景 1 · 两边能用的 API 清单（实测）")

    con = duckdb.connect()
    try:
        ext_rows = con.sql(
            "SELECT extension_name, installed, install_mode, description "
            "FROM duckdb_extensions() WHERE extension_name IN ('fts', 'icu', 'json') "
            "ORDER BY extension_name"
        ).fetchall()
        # 没装 fts 扩展的时候，duckdb_functions() 里一个 FTS 相关函数都没有
        duck_fts_funcs = con.sql(
            "SELECT function_name FROM duckdb_functions() "
            "WHERE function_name ILIKE '%bm25%' OR function_name ILIKE '%fts%' "
            "OR function_name ILIKE '%token%' OR function_name ILIKE '%tf_idf%' "
            "ORDER BY function_name"
        ).fetchall()
    finally:
        con.close()

    print("  DuckDB 侧 —— `duckdb_extensions()` 自带这张元数据表，查它不需要联网：")
    print()
    print(
        "  "
        + common.pad("扩展", 10)
        + common.pad("已安装", 12)
        + common.pad("安装方式", 20)
        + "说明"
    )
    print("  " + "-" * 96)
    for name, installed, mode, desc in ext_rows:
        print(
            "  "
            + common.pad(str(name), 10)
            + common.pad(str(installed), 12)
            + common.pad(str(mode), 20)
            + str(desc)[:50]
        )
    print()

    fts_ext = [r for r in ext_rows if r[0] == "fts"]
    if fts_ext:
        _, installed, mode, _ = fts_ext[0]
        print(f"  → **fts 扩展：installed = {installed}，install_mode = {mode}**")
        print("    `NOT_INSTALLED` 意味着要用得先 `INSTALL fts` 下载，这一步要联网。")
        print("    对照组 `icu` / `json` 是 `STATICALLY_LINKED` —— 编译进二进制里，装都不用装。")
    print()
    print(f"  现在 `duckdb_functions()` 里跟 FTS 相关的函数有 **{len(duck_fts_funcs)}** 个：")
    print(f"    {duck_fts_funcs or '（空 —— 扩展没装，函数也就没有）'}")
    print("    装了扩展之后才有 `match_bm25` 这类函数。")
    print()

    pol_funcs = polars_string_functions()
    pol_fts = [n for n in pol_funcs if any(k in n.lower() for k in FTS_KEYWORDS)]
    print(f"  Polars 侧 —— 字符串命名空间一共 **{len(pol_funcs)}** 个方法，")
    print(f"  其中跟 FTS 相关（{'/'.join(FTS_KEYWORDS)}）的有 **{len(pol_fts)}** 个：")
    print(f"    {pol_fts or '（空）'}")
    print()
    print("  全部字符串方法：")
    line = "    "
    for i, name in enumerate(pol_funcs, 1):
        line += f"{name}  "
        if i % 8 == 0:
            print(line)
            line = "    "
    if line.strip():
        print(line)
    print()
    print("  → **Polars 侧一个都没有**：没有倒排索引、没有 BM25、没有 TF-IDF、")
    print("    没有分词器、没有「文档 → 词」这种倒排表的现成结构。")
    print("    DuckDB 那边是「装个扩展就有」，Polars 这边是「自己从零写」。")
    print()
    print("  **本 demo 默认不联网** —— 扩展那部分要加 `--extension` 才跑：")
    print("    uv run python src/fts_demo.py --extension")


# ---------------------------------------------------------------- 2. Polars 的硬扫


def scenario_polars_scan() -> None:
    section("场景 2 · Polars 侧只有「硬扫」：能搜到，不能排序")

    docs = make_docs(8)
    print("  语料（8 篇）：")
    for r in docs.rows():
        print(f"    {r[0]}  {r[1]}")
    print()

    print("  ① `str.contains` —— 「有没有这个词」")
    raw = docs.filter(pl.col("content").str.contains("parquet")).select("id")
    low = docs.filter(pl.col("content").str.to_lowercase().str.contains("parquet")).select("id")
    print(f"     str.contains('parquet')                 -> id {raw['id'].to_list()}")
    print(f"     str.to_lowercase().str.contains('parquet') -> id {low['id'].to_list()}")
    print("     **两个坑**：")
    print(
        "       · 默认**大小写敏感** —— 语料里写的是 `Parquet`，"
        "`contains('parquet')` 一条都不命中；"
    )
    print("       · 参数是**正则**不是字面量 —— `contains('a.b')` 会连 `axb` 一起命中，")
    print("         要按字面匹配得加 `literal=True`。")
    print()

    print("  ② `str.contains_any` —— 「这几个词里有没有一个」（OR 语义）：")
    any_hit = docs.filter(pl.col("content").str.contains_any(["parquet", "BM25"])).select("id")
    any_low = docs.filter(
        pl.col("content").str.to_lowercase().str.contains_any(["parquet", "bm25"])
    ).select("id")
    print(f"     contains_any(['parquet','BM25'])                 -> id {any_hit['id'].to_list()}")
    print(f"     to_lowercase().contains_any(['parquet','bm25'])  -> id {any_low['id'].to_list()}")
    print("     这个函数没有 `literal` 参数 —— 它是**字面子串**匹配，")
    print("     所以 `['c++']` 这种含正则元字符的词也不会出问题。")
    print()

    print("  ③ 自己倒排 —— `split` + `explode` + `group_by`：")
    inverted = (
        docs.with_columns(
            pl.col("content")
            .str.to_lowercase()
            .str.replace_all("[^\\w]+", " ")
            .str.split(" ")
            .alias("words")
        )
        .explode("words")
        .filter(pl.col("words") != "")
        .group_by("words")
        .agg(pl.col("id").unique().sort().alias("ids"))
    )
    print("     词 → 文档 id 的倒排表（只看英文词，中文那部分见下）：")
    for r in inverted.filter(pl.col("words").str.contains("^[a-z0-9]+$")).sort("words").rows():
        print(f"       {common.pad(str(r[0]), 12)} -> {r[1]}")
    print()
    print("     然后就能「按词查」：")
    for word in ("parquet", "bm25", "duckdb"):
        rows = inverted.filter(pl.col("words") == word)["ids"].to_list()
        print(f"       {common.pad(word, 12)} -> {rows[0] if rows else []}")
    print()
    print("     **中文这里露馅了**：正则是按**非单词字符**切的，中文之间没有空格，")
    print("     所以一整串中文变成了**一个词**：")
    cjk = [
        r[0]
        for r in inverted.filter(pl.col("words").str.contains("^[\\u4e00-\\u9fff]"))
        .sort("words")
        .rows()
    ]
    for w in cjk[:3]:
        print(f"       {w}")
    print("     → 不做分词的话，倒排表对中文基本没用。")
    print("       DuckDB 的 fts 扩展自带分词器，Polars 侧要自己接分词器，")
    print("       或者用 `jieba` / `pkuseg` 这类外部库先切好。")
    print()

    print("  → **能搜到，但少了三样东西**：")
    print("    ① **没有排序**：BM25 会给每篇文档一个分数，排序后「最相关的在前」；")
    print("       硬扫只能给一个布尔列，想要排序就得自己定规则。")
    print("    ② **没有打分**：BM25 考虑词频、逆文档频率、文档长度；")
    print("       倒排表里只有「哪些文档含这个词」，没有权重。")
    print("    ③ **多词查询不是 AND**：`contains_any` 是 OR 语义，")
    print("       真要 AND 得自己过滤后取交集。")
    print()
    print("  还有一样 DuckDB 有、Polars 没有的东西：**索引**。")
    print("    DuckDB 的 fts 扩展在表上建的是**倒排索引**，查一个词是 O(命中数)；")
    print("    Polars 的 `str.contains` 是**每行扫一遍**，O(总行数)。")
    print("    语料小的时候没差别，语料大了就是两个数量级的差距（见场景 4）。")


# ---------------------------------------------------------------- 3. 装上扩展之后


def scenario_with_extension(rows: int, repeat: int) -> None:
    section("场景 3 · 装上 fts 扩展之后：BM25 打分 + 倒排索引")

    con = duckdb.connect()
    try:
        con.execute("INSTALL fts; LOAD fts;")
    except Exception as exc:  # 离线 / 版本不支持 / 没权限都会走到这
        print(f"  当前环境不可用：{str(exc).splitlines()[0][:120]}")
        print("  （这一节需要联网下载扩展；本项目的其余部分不依赖它。）")
        con.close()
        return

    # 装上之后 DuckDB 这边有什么
    duck_fts_funcs = con.sql(
        "SELECT function_name FROM duckdb_functions() "
        "WHERE function_name ILIKE '%bm25%' OR function_name ILIKE '%fts%' "
        "OR function_name ILIKE '%token%' OR function_name ILIKE '%tf_idf%' "
        "ORDER BY function_name"
    ).fetchall()
    print(f"  装上之后，`duckdb_functions()` 里跟 FTS 相关的函数有 {len(duck_fts_funcs)} 个：")
    for (name,) in duck_fts_funcs:
        print(f"    {name}")
    print()

    with tempfile.TemporaryDirectory(prefix="fts_demo_") as tmp:
        pq = Path(tmp) / "docs.parquet"
        docs = make_docs(rows)
        docs.write_parquet(pq)
        con.execute("CREATE TABLE docs AS SELECT * FROM read_parquet(?)", [str(pq)])

        print(f"  语料：{docs.height:,} 篇（存成 parquet 再读回来，走的是真实路径）")
        print()

        print("  建倒排索引：")
        print("    PRAGMA create_fts_index('docs', 'id', 'content');")
        t0 = time.perf_counter()
        con.execute("PRAGMA create_fts_index('docs', 'id', 'content')")
        print(f"    建索引耗时 {time.perf_counter() - t0:.2f} 秒")
        print()

        print("  BM25 查询（打分 + 按分数排序）：")
        for query in QUERIES:
            sql = (
                "SELECT id, content, fts_main_docs.match_bm25(id, ?) AS score "
                "FROM docs WHERE fts_main_docs.match_bm25(id, ?) IS NOT NULL "
                "ORDER BY score DESC LIMIT 3"
            )
            hits = con.execute(sql, [query, query]).fetchall()
            print(f"    查询 {query!r}：")
            if not hits:
                print("      （没有命中）")
            for rid, content, score in hits:
                print(f"      id={rid}  score={score:.4f}  {str(content)[:36]}")
        print()

        print("  Polars 侧同样的三个查询 —— 只有布尔命中，没有分数：")
        for query in QUERIES:
            first = query.split()[0]
            hits = docs.filter(pl.col("content").str.to_lowercase().str.contains(first.lower()))
            ids = hits["id"].to_list()[:8]
            print(f"    查询 {query!r}：命中 id {ids}（**没有分数，也没有排序**）")
        print()

        print("  → **能力差距摆在这里**：")
        print("    DuckDB 给了「相关度」，Polars 只能给「命中/没命中」。")
        print("    排序、top-N、阈值过滤 —— 全都建立在这个分数上。")

        # ---- 代价：BM25 vs 硬扫
        print()
        print(f"  代价：{docs.height:,} 篇文档上，BM25 查询和硬扫各要多久")
        print(f"  机器：{common.describe_machine()}")
        print()

        def duck_bm25() -> int:
            return len(
                con.execute(
                    "SELECT id FROM docs WHERE fts_main_docs.match_bm25(id, ?) IS NOT NULL",
                    ["parquet"],
                ).fetchall()
            )

        def pol_contains() -> int:
            return docs.filter(pl.col("content").str.to_lowercase().str.contains("parquet")).height

        def pol_inverted() -> int:
            return (
                docs.with_columns(
                    pl.col("content")
                    .str.to_lowercase()
                    .str.replace_all("[^\\w]+", " ")
                    .str.split(" ")
                    .alias("words")
                )
                .explode("words")
                .filter(pl.col("words") != "")
                .group_by("words")
                .agg(pl.col("id").unique().alias("ids"))
                .filter(pl.col("words") == "parquet")
                .height
            )

        results = [
            ("DuckDB  fts  BM25（带打分）", duck_bm25),
            ("Polars  str.contains（硬扫）", pol_contains),
            ("Polars  split+explode+group_by", pol_inverted),
        ]
        print(
            "  "
            + common.pad("写法", 30)
            + common.pad_left("中位数 (ms)", 14)
            + common.pad_left("最快 (ms)", 12)
        )
        print("  " + "-" * 56)
        for label, fn in results:
            fn()  # 预热
            times: list[float] = []
            for _ in range(repeat):
                t0 = time.perf_counter()
                fn()
                times.append(time.perf_counter() - t0)
            print(
                "  "
                + common.pad(label, 30)
                + common.pad_left(f"{statistics.median(times) * 1000:.1f}", 14)
                + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            )
        print()
        print("  口径：三行都是「找出正文里含 `parquet` 的文档」，返回命中条数；")
        print("  数据都在内存里，计时不含读盘，重复次数取中位数。")
        print("  **建索引的时间不计入** —— 它是一次性成本，上面单独打了。")
        print("  注意这不是纯粹的引擎速度对比：DuckDB 那边多算了 BM25 分数，")
        print("  Polars 这边只判断命中与否 —— 这正是「能力差 + 代价差」的总账。")
        print()
        print("  → 语料越大，差距越大：倒排索引是 O(命中数)，硬扫是 O(总行数 × 文本长度)。")
        print("    但也要说清楚：**Polars 侧的硬扫在小语料上完全够用**，")
        print("    而且没有索引要维护、没有索引要重建 ——")
        print("    语料频繁变动的时候，硬扫反而更省心。")

    con.close()


# ---------------------------------------------------------------- 4. 没有扩展时


def scenario_without_extension(rows: int, repeat: int) -> None:
    section("场景 4 · 不装扩展的话，Polars 侧的硬扫有多贵")

    docs = make_docs(rows)
    print(f"  语料：{docs.height:,} 篇")
    print(f"  机器：{common.describe_machine()}")
    print()

    def pol_contains() -> int:
        return docs.filter(pl.col("content").str.to_lowercase().str.contains("parquet")).height

    def pol_inverted() -> int:
        return (
            docs.with_columns(
                pl.col("content")
                .str.to_lowercase()
                .str.replace_all("[^\\w]+", " ")
                .str.split(" ")
                .alias("words")
            )
            .explode("words")
            .filter(pl.col("words") != "")
            .group_by("words")
            .agg(pl.col("id").unique().alias("ids"))
            .filter(pl.col("words") == "parquet")
            .height
        )

    results = [
        ("Polars  str.contains（硬扫）", pol_contains),
        ("Polars  split+explode+group_by（倒排）", pol_inverted),
    ]
    print(
        "  "
        + common.pad("写法", 40)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
    )
    print("  " + "-" * 66)
    for label, fn in results:
        fn()  # 预热
        times: list[float] = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        print(
            "  "
            + common.pad(label, 40)
            + common.pad_left(f"{statistics.median(times) * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
        )
    print()
    print("  口径：两个都是「找出正文里含 `parquet` 的文档」，返回命中条数；")
    print("  数据都在内存里，计时不含读盘，重复次数取中位数。")
    print("  注意倒排那种写法**每次查询都要重建整张倒排表** ——")
    print("  DuckDB 的 fts 扩展是**建一次、查很多次**，这是它真正的价值。")
    print()
    print("  → 所以在没有扩展的 Polars 侧，全文检索的现实选择只有三个：")
    print("    ① 语料不大 → `str.contains` 硬扫，简单够用；")
    print("    ② 语料大且查得频繁 → **自己把倒排表物化成一张表**")
    print("       （或者干脆换到有 FTS 的引擎）；")
    print("    ③ 需要相关度排序 → DuckDB 的 BM25 是现成的，")
    print("       Polars 侧得自己实现 TF-IDF / BM25。")
    print()
    print("  想看 DuckDB 那边的真实数字，加上 --extension：")
    print("    uv run python src/fts_demo.py --extension")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的全文本搜索能力差距演示")
    parser.add_argument(
        "--extension",
        action="store_true",
        help="额外尝试安装 duckdb 的 fts 扩展（需要联网下载）",
    )
    parser.add_argument("--rows", type=int, default=50_000, help="语料行数")
    parser.add_argument("--repeat", type=int, default=5, help="代价场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    scenario_capability_inventory()
    scenario_polars_scan()
    if args.extension:
        scenario_with_extension(args.rows, args.repeat)
    else:
        section("场景 3 · 装上 fts 扩展之后：BM25 打分 + 倒排索引")
        print("跳过（默认不联网下载扩展）。加 --extension 可以试装：")
        print("  uv run python src/fts_demo.py --extension")
        print()
    scenario_without_extension(args.rows, args.repeat)

    print()


if __name__ == "__main__":
    main()
