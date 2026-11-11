"""排序规则与中文排序：DuckDB 的 COLLATE vs Polars 的纯码点排序。

`order_demo.py` 讲的是**怎么写 ORDER BY**（NULLS FIRST、ORDER BY ALL、分页）。
这个 demo 讲的是**按什么规则排** —— 排序的规则本身。

排序规则这件事，平时看不出来，一碰到这几类数据就露馅：

  · **中文**：「上海」在「北京」前面还是后面？按拼音还是按码点？
    本项目的数据自带中文（region / category / channel），可以直接实测。
  · **大小写**：`Apple` 和 `apple` 谁在前面？它们算**同一个**吗？
  · **重音**：`École` 和 `école` 呢？

脚本把两边的排序摆出来逐条对账：

  1. 默认排序：两边**逐行一致**（DuckDB 的 UTF-8 字节序 ≡ Polars 的码点序）。
  2. 中文按码点还是按拼音 —— 用本项目自带的 region / category / channel 实测。
  3. DuckDB 自带 **144 个 ICU 排序规则**；zh_cn 排拼音，zh_tw 排笔画。
  4. COLLATE 不只管排序：`=`、`DISTINCT`、`GROUP BY` 全都跟着变。
  5. **列级 COLLATE**：建表时就定下比较语义 —— Polars 完全没有这个概念。
  6. Polars 侧的出路：`icu_sort_key` 能**一字不差**复现 ICU 的顺序。
  7. `lower()` 的 Unicode 细节：同一个 `İ`，两边生成**两个不同的去重键**。

用法：
    uv run python src/collation_demo.py
"""

from __future__ import annotations

import inspect
import time

import duckdb
import polars as pl

import common

# 一组故意混在一起的词：ASCII 大小写、带重音的、中文。
# 「Zebra」故意写两遍、「apple / Apple」故意大小写不同 ——
# 并列的时候排成什么样，是这个 demo 要看的。
WORDS = [
    "Zebra",
    "apple",
    "Apple",
    "banana",
    "ZZZ",
    "zebra",
    "zzz",
    "École",
    "école",
    "Ápple",
    "Äpple",
    "上海",
    "中文",
    "北京",
    "广州",
    "橙子",
    "深圳",
    "苹果",
    "香蕉",
]

# 用来演示「三种排法」的汉字：笔画数从 1 到 5 不等，拼音也各不同。
STROKE_SAMPLE = ["一", "丁", "丂", "七", "万", "丈", "三", "上", "下", "不", "世", "丘"]

# 用来演示 lower() 的 Unicode 分歧：土耳其 dotted capital I。
CASE_SAMPLES = [
    "ÄÖÜ",
    "ÉCOLE",
    "ΑΒΓ",
    "ПРИВЕТ",
    "İSTANBUL",
    "ǅ",
    "ﬁ",
    "İ",
    "ß",
    "ＡＢＣ",
]


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _q(text: str) -> str:
    """把一段文本变成 SQL 字符串字面量（单引号要转义成两个）。"""
    return "'" + text.replace("'", "''") + "'"


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    """跑一条 SQL，返回第一列（转成字符串，省得越界值取不回 Python 对象）。"""
    res = con.execute(sql)
    out = [str(r[0]) for r in res.fetchall()]
    return out


def _try(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[str, str]:
    """跑一条 SQL，失败返回 (错误类型, 错误信息)，成功返回 ("", 值)。"""
    try:
        res = con.execute(sql)
        row = res.fetchone()
        res.fetchall()
        return "", str(row[0])
    except Exception as exc:
        return type(exc).__name__, str(exc).splitlines()[0][:120]


def _duck_order(con: duckdb.DuckDBPyConnection, words: list[str], collate: str | None) -> list[str]:
    """按 DuckDB 的排序规则排一批词。"""
    values = ",".join("(" + _q(w) + ")" for w in words)
    sql = f"SELECT s FROM (VALUES {values}) t(s) ORDER BY s"
    if collate:
        sql += f' COLLATE "{collate}"'
    return _rows(con, sql)


# ---------------------------------------------------------------- 场景 1：默认排序


def scenario_default_order(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 默认排序：两边逐行一致")
    print("  DuckDB 的 VARCHAR 默认按 **UTF-8 字节序**排，")
    print("  Polars 的 `sort()` 按 **Unicode 码点序**排 ——")
    print("  这两个对 UTF-8 来说是**同一件事**（UTF-8 是保序编码），所以结果必须一样。")
    print()

    duck = _duck_order(con, WORDS, None)
    pol = pl.DataFrame({"s": WORDS}).sort("s")["s"].to_list()
    same = duck == pol
    print(f"  语料 {len(WORDS)} 个词（ASCII 大小写 + 带重音 + 中文）：")
    print(f"    DuckDB → {duck}")
    print(f"    Polars → {pol}")
    print(f"    → {'✅ 逐行一致' if same else '❌ 不一致'}")
    print()
    print("  注意几个细节：")
    print("    · 大写全排在小写前面（'Apple' < 'apple'）—— 码点序，不是什么「智能排序」；")
    print("    · 带重音的那一组整体排在 ASCII 之后（'École' 是 U+00E9 > 'z'），")
    print("      组内还是大写在前（'École' < 'école'）—— 重音符本身是一个码点；")
    print("    · 中文按**码点**排：'上海'(U+4E0A) < '中文'(U+4E2D) < '北京'(U+5317) < …")
    print()
    print("  → **默认排序两边没有分歧**，后面几节讲的是 DuckDB 多出来的那些能力。")


# ---------------------------------------------------------------- 场景 2：中文


def scenario_chinese(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 中文按码点还是按拼音 —— 用本项目自带的数据实测")
    print("  DuckDB 默认排中文 = 码点序，也就是**字典里没有的顺序**：")
    print("  「上海」在「北京」前面，因为「上」U+4E0A 比「北」U+5317 小。")
    print("  ICU 的 zh_cn 排的是**拼音**（běijīng < guǎngzhōu < píngguǒ < shànghǎi …）。")
    print()

    path = common.ORDERS_PARQUET
    if not path.exists():
        print(f"  （没找到 {path.name}，先跑 `uv run python src/gen_data.py`）")
        return

    print(f"  数据源：{path.name}")
    print()
    for col in ["region", "category", "channel"]:
        duck_def = _rows(con, f"SELECT DISTINCT {col} FROM '{path}' ORDER BY {col}")
        duck_pinyin = _rows(
            con, f"SELECT DISTINCT {col} FROM '{path}' ORDER BY {col} COLLATE \"zh_cn\""
        )
        df = pl.read_parquet(path)
        pol_def = sorted(df[col].unique().to_list())
        print(f"  {col}：")
        print(f"    DuckDB 默认（码点）  = {duck_def}")
        print(f"    DuckDB zh_cn（拼音）= {duck_pinyin}")
        print(f"    Polars 默认         = {pol_def}")
        print(f"    → 默认排序：{'✅ 两边一致' if duck_def == pol_def else '❌ 不一致'}")
        if duck_pinyin != duck_def:
            print("    → zh_cn 排序：⚠️ 和默认不一样（所以行序在两个引擎间会不同）")
        else:
            print("    → zh_cn 排序：一样")
        print()

    print("  → **默认排序两边一致**（上一节验过）；")
    print('    但只要有人加一句 `COLLATE "zh_cn"`，DuckDB 这边的行序就变了，')
    print("    而 **Polars 根本没有 `COLLATE` 这个概念** ——")
    print("    同一份数据、同一句需求，两个引擎给出的报表行顺序不一样。")
    print()
    print("  这就是「中文排序」真正的风险：不是排错，是**排的规则不一样**，")
    print("  而两边都不报错。")


# ---------------------------------------------------------------- 场景 3：ICU


def scenario_icu(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · DuckDB 自带 144 个 ICU 排序规则")
    print("  DuckDB 的 ICU 扩展是 **STATICALLY_LINKED**（编译进二进制），")
    print("  所以这些排序规则**装都不用装**，开箱就有。")
    print()

    names = _rows(con, "SELECT * FROM pragma_collations()")
    print(f"  `pragma_collations()` 返回 **{len(names)}** 个排序规则，样例：")
    print(f"    {names[:12]}")
    print()
    zh = [n for n in names if n.startswith("zh")]
    print(f"  中文相关的有 {len(zh)} 个：{zh}")
    print()

    print("  同一个词表，换排序规则 —— 中文部分的差别最直观：")
    print(f"    {'码点序':<14} {_duck_order(con, WORDS, None)}")
    print(f"    {'zh_cn（拼音）':<14} {_duck_order(con, WORDS, 'zh_cn')}")
    print(f"    {'zh_tw（笔画）':<14} {_duck_order(con, WORDS, 'zh_tw')}")
    print()

    print("  用一组笔画数递增的汉字看「笔画」是什么意思：")
    print(f"    码点序   = {_duck_order(con, STROKE_SAMPLE, None)}")
    print(f"    zh_cn    = {_duck_order(con, STROKE_SAMPLE, 'zh_cn')}   ← 拼音：bù dīng qiǎo …")
    print(f"    zh_tw    = {_duck_order(con, STROKE_SAMPLE, 'zh_tw')}   ← 笔画数：一(1) 丁(2) …")
    print()
    print("  → **同一个 collation 名字，选哪个会决定完全不同的顺序。**")
    print("    zh_cn 和 zh_tw 都是 ICU 的标准 locale，选错不会报错，只会给你错的顺序。")
    print()

    print("  DuckDB 内置的两个 collation（不是 ICU，是 DuckDB 自己实现的）：")
    for coll, desc in [("NOCASE", "大小写不敏感"), ("NOACCENT", "重音不敏感")]:
        order = _duck_order(con, WORDS, coll)
        print(f"    COLLATE {coll:9s}（{desc}）→ {order}")
    print()
    print("  两个坑：")
    _err_type, err_msg = _try(
        con, "SELECT s FROM (VALUES ('a')) t(s) ORDER BY s COLLATE NOCASE NOACCENT"
    )
    print(f"    · `COLLATE NOCASE NOACCENT` 写不出来 → {err_msg}")
    print("      （想要「大小写和重音都不敏感」，得用 ICU 的 locale，比如 en_US）")
    _err_type, err_msg = _try(
        con, "SELECT s FROM (VALUES ('a')) t(s) ORDER BY s COLLATE \"und-u-co-pinyin\""
    )
    print(f"    · ICU 只能用 locale 名，不能用完整的 Unicode locale id → {err_msg}")
    print()


# ---------------------------------------------------------------- 场景 4：COLLATE 的作用域


def scenario_scope(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · COLLATE 不只管排序：= / DISTINCT / GROUP BY 都跟着变")
    print("  这是最容易被忽略的一点 —— `COLLATE` 改的是**比较语义**，不只是顺序。")
    print()

    con.execute("CREATE TABLE cmp (s VARCHAR)")
    for word in ["apple", "Apple", "APPLE", "banana"]:
        con.execute(f"INSERT INTO cmp VALUES ({_q(word)})")
    print("  测试数据：apple / Apple / APPLE / banana")
    print()
    n_distinct = _rows(con, "SELECT count(DISTINCT s) FROM cmp")[0]
    n_distinct_nc = _rows(con, "SELECT count(DISTINCT s COLLATE NOCASE) FROM cmp")[0]
    distinct_nc = _rows(con, "SELECT DISTINCT s COLLATE NOCASE FROM cmp ORDER BY 1")
    n_eq = _rows(con, "SELECT count(*) FROM cmp WHERE s = " + _q("apple"))[0]
    n_eq_nc = _rows(con, "SELECT count(*) FROM cmp WHERE s COLLATE NOCASE = " + _q("apple"))[0]
    print(f"    count(DISTINCT s)                = {n_distinct}")
    print(f"    count(DISTINCT s COLLATE NOCASE) = {n_distinct_nc}")
    print(f"    DISTINCT s COLLATE NOCASE        = {distinct_nc}")
    print(f"    s = 'apple' 的行数（默认）        = {n_eq}")
    print(f"    s COLLATE NOCASE = 'apple'       = {n_eq_nc}")
    print()
    print("  → 加上 `COLLATE NOCASE` 之后，apple / Apple / APPLE **合并成了一个值**。")
    print("    这就是「大小写不敏感的唯一键」在 DuckDB 里的写法。")
    print()

    err_type, err_msg = _try(con, "SELECT s, count(*) FROM cmp GROUP BY s COLLATE NOCASE")
    print("  ⚠️ 但 `GROUP BY s COLLATE NOCASE` **写不出来**：")
    print(f"    {err_msg}")
    print("    （DuckDB 的 binder 认为分组键变了，于是抱怨「s 必须出现在 GROUP BY 里」。")
    print("      想按大小写不敏感分组，得自己写 `GROUP BY lower(s)`，或者用列级 COLLATE。）")
    print()

    print("  顺带看一下并列（大小写折叠后还一样的值）排成什么样：")
    con.execute("CREATE TABLE tie (id INT, s VARCHAR)")
    for i, word in enumerate(["b", "B", "a", "A", "C", "c"]):
        con.execute(f"INSERT INTO tie VALUES ({i}, {_q(word)})")
    tie_duck = _rows(con, "SELECT id FROM tie ORDER BY s COLLATE NOCASE")
    tie_pol = (
        pl.DataFrame({"id": [0, 1, 2, 3, 4, 5], "s": ["b", "B", "a", "A", "C", "c"]})
        .sort("s")
        .get_column("id")
        .to_list()
    )
    print(f"    DuckDB NOCASE 的 id 顺序 = {tie_duck}")
    print(f"    Polars 默认的 id 顺序   = {tie_pol}")
    print("    → DuckDB 的 NOCASE 是**稳定排序**：折叠后并列的值保持输入顺序。")
    print("      Polars 的 `sort()` 默认**不保证**稳定，要稳定得传 `maintain_order=True`。")
    print()


# ---------------------------------------------------------------- 场景 5：列级 COLLATE


def scenario_column_collation(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 列级 COLLATE：建表时就定下比较语义")
    print("  `COLLATE` 不只能写在 ORDER BY 后面 ——")
    print("  建表时就能给这一列定一个 collation，**这个列上所有的比较都按它来**。")
    print()

    con.execute("CREATE TABLE coll (s VARCHAR COLLATE NOCASE)")
    for word in ["apple", "Apple", "APPLE", "banana"]:
        con.execute(f"INSERT INTO coll VALUES ({_q(word)})")
    con.execute("CREATE TABLE plain (s VARCHAR)")
    for word in ["apple", "Apple", "APPLE", "banana"]:
        con.execute(f"INSERT INTO plain VALUES ({_q(word)})")

    for table, label in [("plain", "普通列"), ("coll", "COLLATE NOCASE 的列")]:
        n_all = _rows(con, "SELECT count(*) FROM " + table)[0]
        n_distinct = _rows(con, "SELECT count(DISTINCT s) FROM " + table)[0]
        n_eq = _rows(con, "SELECT count(*) FROM " + table + " WHERE s = " + _q("apple"))[0]
        n_groups = _rows(con, "SELECT count(*) FROM (SELECT s FROM " + table + " GROUP BY s) g")[0]
        ordered = _rows(con, "SELECT s FROM " + table + " ORDER BY s")
        print(f"  {label}：")
        print(f"    count(*)              = {n_all}")
        print(f"    count(DISTINCT s)     = {n_distinct}")
        print(f"    WHERE s = 'apple'     = {n_eq}")
        print(f"    GROUP BY s 的组数     = {n_groups}")
        print(f"    ORDER BY s            = {ordered}")
        print()

    print("  → 同一份数据，同样的四句话，**只有一列的声明不同**，结果全变了：")
    print("    `=` 匹配上了、distinct 合并了、分组合并了、排序变了。")
    print()
    print("  Polars 侧**完全没有这个概念** ——")
    print("    没有「列级 collation」，也没有 `COLLATE` 关键字。")
    print("    想要同样的效果，只能自己在每个查询里写 `lower(s)`，")
    print("    或者在读进来的那一刻就把列折叠好。")
    print()

    print("  JOIN 那一侧也一样（一边有 collation、一边没有）：")
    con.execute("CREATE TABLE other (k VARCHAR)")
    con.execute(f"INSERT INTO other VALUES ({_q('APPLE')})")
    n_join_coll = _rows(con, "SELECT count(*) FROM coll JOIN other ON coll.s = other.k")[0]
    n_join_plain = _rows(con, "SELECT count(*) FROM plain JOIN other ON plain.s = other.k")[0]
    print(f"    coll.s = other.k 的连接数 = {n_join_coll}")
    print(f"    plain.s = other.k 的连接数 = {n_join_plain}")
    print()
    print("  → 连接条件里**哪一列带 collation 会影响结果** ——")
    print("    这跟「NULL 参与连接」一样，是那种「不报错但结果不一样」的坑。")


# ---------------------------------------------------------------- 场景 6：Polars 的出路


def scenario_polars_way(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · Polars 侧的出路：icu_sort_key")
    print("  Polars 没有 `COLLATE`，但 DuckDB 把 ICU 的**排序键**算出来了 ——")
    print("  `icu_sort_key(str, collator)`。拿这个键去排，顺序能一字不差地复现。")
    print()

    keys = [_rows(con, f"SELECT icu_sort_key({_q(w)}, 'zh_cn')")[0] for w in WORDS]
    duck_icu = _duck_order(con, WORDS, "zh_cn")
    pol_by_key = (
        pl.DataFrame({"s": WORDS}).with_columns(pl.Series("k", keys)).sort("k")["s"].to_list()
    )
    print("  排序键长这样（ICU 的排序键是二进制串，不是可读的拼音）：")
    for word, key in list(zip(WORDS, keys, strict=True))[:6]:
        print(f"    {word:10s} → {key}")
    print()
    print(f'    DuckDB `COLLATE "zh_cn"` → {duck_icu}')
    print(f"    Polars 按 icu_sort_key 排   → {pol_by_key}")
    print(f"    → {'✅ 完全一致' if duck_icu == pol_by_key else '❌ 不一致'}")
    print()
    print("  所以 Polars 侧的路子是：**问 DuckDB 要排序键，自己排**。")
    print("  （这正是 DuckDB 与 Polars 混用时最实用的那招 ——")
    print("    排序规则住在 DuckDB 那边，结果在 Polars 这边用。）")
    print()

    print("  但这条路有个**必须知道的坑**：`icu_sort_key` 只认 **ICU 的 locale 名**。")
    print("  给它 DuckDB 自带的 `NOCASE` / `NOACCENT`，它**不报错** ——")
    print("  但也**不生效**，返回的是默认（root）排序键：")
    print()
    probe = ["a", "A", "École", "école", "Apple", "apple"]
    for coll in ["NOCASE", "NOACCENT", "und", "en_US", "zh_cn"]:
        keys = [_rows(con, f"SELECT icu_sort_key({_q(w)}, {_q(coll)})")[0] for w in probe]
        print(f"    icu_sort_key(_, {coll:9s}) → {keys}")
    print()
    print("    前四行的键**完全一样** —— 说明 NOCASE / NOACCENT 被当成了默认 collator。")
    print("    （ICU 那边没有叫 NOCASE 的 locale，`icu_sort_key` 就回退到 root。）")
    print()

    print("  于是按 NOCASE 键排出来的顺序，跟 `ORDER BY s COLLATE NOCASE` **对不上**：")
    nocase_words = ["Zebra", "apple", "Apple", "banana", "ZZZ", "zebra", "École", "école"]
    duck_nocase = _duck_order(con, nocase_words, "NOCASE")
    nc_keys = [_rows(con, f"SELECT icu_sort_key({_q(w)}, 'NOCASE')")[0] for w in nocase_words]
    pol_nc = (
        pl.DataFrame({"s": nocase_words})
        .with_columns(pl.Series("k", nc_keys))
        .sort("k")["s"]
        .to_list()
    )
    print(f"    DuckDB  ORDER BY s COLLATE NOCASE → {duck_nocase}")
    print(f"    Polars  按 icu_sort_key(_, 'NOCASE') → {pol_nc}")
    print(f"    → {'✅ 一致' if duck_nocase == pol_nc else '❌ 不一致 —— 这条路对 NOCASE 不管用'}")
    print()
    print("  → 结论要分清楚：")
    print("    · **ICU 的 locale（zh_cn / en_US …）→ 能一字不差复现**；")
    print("    · **DuckDB 自带的 NOCASE / NOACCENT → 复现不了**，")
    print("      而 `icu_sort_key` 还**不给你报错**（这是最坑的地方）。")
    print("    · 另外这条路要求旁边有个 DuckDB，不是纯 Polars 方案。")
    print()

    print("  Polars 自己的 `sort()` 到底有什么参数：")
    print(f"    Expr.sort       = {list(inspect.signature(pl.Expr.sort).parameters)[1:]}")
    print(f"    DataFrame.sort  = {list(inspect.signature(pl.DataFrame.sort).parameters)[1:]}")
    print("    → 没有 collation、没有 locale，**没有任何排序规则相关的参数**。")
    print()


# ---------------------------------------------------------------- 场景 7：lower()


def scenario_lower(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · 大小写折叠：lower() 两边不是同一个函数")
    print("  DuckDB 的 `lower()` 和 Polars 的 `str.to_lowercase()` 都认 Unicode，")
    print("  大部分字母的结果一样 —— 但**土耳其语的那个点 I** 上不一样，")
    print("  而且**长度都不一样**。")
    print()

    header = ("原文", "DuckDB lower()", "长度", "Polars to_lowercase()", "长度")
    print(
        "  "
        + common.pad(header[0], 12)
        + common.pad(header[1], 18)
        + common.pad(header[2], 6)
        + common.pad(header[3], 22)
        + common.pad(header[4], 6)
        + "  对比"
    )
    print("  " + "-" * 92)
    for text in CASE_SAMPLES:
        duck = _rows(con, f"SELECT lower({_q(text)})")[0]
        pol = pl.DataFrame({"s": [text]})["s"].str.to_lowercase().to_list()[0]
        same = "✅" if duck == pol else "❌ 不一样"
        print(
            "  "
            + common.pad(text, 12)
            + common.pad(duck, 18)
            + common.pad(str(len(duck)), 6)
            + common.pad(pol, 22)
            + common.pad(str(len(pol)), 6)
            + same
        )
    print()
    print("  → `İ`（U+0130，土耳其语的点大写 I）在两边变成了两个**不同的字符串**：")
    print("    DuckDB  lower('İ') = 'i'          （1 个码点）")
    print("    Polars  lower('İ') = 'i' + U+0307 （2 个码点，'i' 后面跟一个组合上点）")
    print()
    print("  这不只是「显示不一样」—— **长度不一样**。")
    print("  如果拿 `lower(x)` 当去重键：")
    print("    DuckDB 那边 'İstanbul' 和 'istanbul' 是**同一个键**；")
    print("    Polars 那边是两个**不同的键**。")
    print("    同一个 `İstanbul` 混在 `istanbul` 里，两边的去重结果就不一样。")
    print()
    duck_len = _rows(con, f"SELECT length(lower({_q('İSTANBUL')}))")[0]
    pol_len = pl.DataFrame({"s": ["İSTANBUL"]})["s"].str.to_lowercase().str.len_chars().to_list()[0]
    print(f"    实测 length(lower('İSTANBUL'))：DuckDB = {duck_len}，Polars = {pol_len}")
    print()
    print("  → 这就是「大小写规则」的真正含义：**它不只是排序时用的大小写不敏感**，")
    print("    还是**你拿来生成 key 的那个函数的语义**。两者必须对得上，否则去重结果不同。")


# ---------------------------------------------------------------- 场景 8：代价


def scenario_cost(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 排一次序要花多少钱")
    path = common.ORDERS_PARQUET
    if not path.exists():
        print(f"  （没找到 {path.name}，跳过）")
        return

    rows = int(_rows(con, f"SELECT count(*) FROM '{path}'")[0])
    n_distinct = int(_rows(con, f"SELECT count(DISTINCT region) FROM '{path}'")[0])
    print(f"  数据：{path.name}，{rows} 行，region 有 {n_distinct} 个不同值")
    print()

    def duck_ms(sql: str, repeat: int = 5) -> float:
        best = float("inf")
        for _ in range(repeat):
            t0 = time.perf_counter()
            con.execute(sql).fetchall()
            best = min(best, time.perf_counter() - t0)
        return best * 1000

    def pol_ms(repeat: int = 5) -> float:
        best = float("inf")
        for _ in range(repeat):
            t0 = time.perf_counter()
            pl.read_parquet(path).sort("region").get_column("region").to_list()
            best = min(best, time.perf_counter() - t0)
        return best * 1000

    def cross_engine_ms(repeat: int = 3) -> float:
        """Polars 想要拼音顺序，就得先问 DuckDB 要排序键 —— 这一整条路的代价。"""
        best = float("inf")
        for _ in range(repeat):
            t0 = time.perf_counter()
            rel = con.sql(f"SELECT region, icu_sort_key(region, 'zh_cn') AS k FROM '{path}'")
            tbl = rel.to_arrow_table()
            rel.close()
            pl.from_arrow(tbl).sort("k").get_column("region").to_list()
            best = min(best, time.perf_counter() - t0)
        return best * 1000

    duck_default = duck_ms(f"SELECT region FROM '{path}' ORDER BY region")
    duck_icu = duck_ms(f"SELECT region FROM '{path}' ORDER BY region COLLATE \"zh_cn\"")
    pol_default = pol_ms()
    cross = cross_engine_ms()

    header = ("写法", "用时 (ms)")
    print("  " + common.pad(header[0], 46) + header[1])
    print("  " + "-" * 92)
    for label, ms in [
        ("DuckDB  读 parquet + ORDER BY region（默认，码点）", duck_default),
        ('DuckDB  读 parquet + ORDER BY region COLLATE "zh_cn"', duck_icu),
        ('Polars  读 parquet + sort("region")（默认，码点）', pol_default),
        ("Polars  问 DuckDB 要排序键再自己排（拼音）", cross),
    ]:
        print(f"  {common.pad(label, 46)}{ms:>10.1f}")
    print()
    print(f"  → ICU 排序比默认排序贵 **{duck_icu / duck_default:.2f}×** —— 排序规则是**现成的**，")
    print("    代价只是排序时多算一个键。")
    print()
    print("  → **出乎意料的一点**：Polars 侧「问 DuckDB 要排序键再自己排」这条路")
    print(f"    （{cross:.1f} ms）**和纯 Polars 排序（{pol_default:.1f} ms）基本一样快**，")
    print(f"    甚至比 DuckDB 自己那句 `ORDER BY ... COLLATE`（{duck_icu:.1f} ms）还快。")
    print("    原因多半是 DuckDB → Arrow 的交接本身很便宜 —— 排序的活儿让给了 Polars。")
    print()
    print("  结论要按测出来的数字说：")
    print("    · **要拼音排序 → 用 DuckDB**：一句 ORDER BY，代价只有默认排序的 1.x 倍；")
    print("    · **Polars 侧也有一条不慢的路**：让 DuckDB 出排序键，Arrow 交接过来自己排；")
    print("    · **纯 Polars（完全不碰 DuckDB）→ 只能是码点序**，")
    print("      想拼音就得自己实现一套排序规则。")
    print()
    print(f"  口径：{rows} 行、region 只有 {n_distinct} 个不同值，")
    print("  所以这部分时间大头是**读 parquet**，排序本身占的比重不大 ——")
    print("  真实场景里「基数高的字符串列」上排序的占比会大得多。")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. **默认排序两边完全一致** —— DuckDB 的 UTF-8 字节序 ≡ Polars 的码点序，")
    print("     19 个中英混排的词逐行对得上。这一条不用操心。")
    print("  2. **中文默认按码点排**，不是拼音：「上海」在「北京」前面。")
    print("     本项目的 region / category / channel 三列实测：默认排序两边一致，")
    print('     换成 `COLLATE "zh_cn"` 顺序就变了 —— 而 **Polars 没有 COLLATE**。')
    print("  3. **DuckDB 自带 144 个 ICU 排序规则**（ICU 静态链接，不用装）。")
    print("     `zh_cn` 排拼音、`zh_tw` 排笔画 —— 选哪个会决定完全不同的顺序，")
    print("     选错不报错。")
    print("  4. **COLLATE 改的是比较语义，不只是顺序**：")
    print("     `=`、`DISTINCT`、`GROUP BY` 全跟着变 —— apple/Apple/APPLE 会合并成一个值。")
    print("     而 `GROUP BY s COLLATE NOCASE` 在 DuckDB 里**写不出来**（binder 报错）。")
    print("  5. **列级 COLLATE**（`VARCHAR COLLATE NOCASE`）让整列的比较语义变掉，")
    print("     连 JOIN 的连接结果都不一样 —— **Polars 完全没有这个概念**。")
    print("  6. **Polars 侧有出路**：`icu_sort_key()` 拿到的排序键，")
    print("     按它排能**一字不差**复现 DuckDB 的 ICU 顺序 —— 但要求旁边有个 DuckDB，")
    print("     而且**只对 ICU 的 locale 有效**：给它 `NOCASE` / `NOACCENT` 它**不报错**，")
    print("     但返回的是默认键，排出来的顺序跟 `COLLATE NOCASE` 对不上。")
    print("  7. **lower() 两边不是同一个函数**：")
    print("     `İ` 在 DuckDB 里变成 1 个码点的 'i'，在 Polars 里变成 2 个码点的 'i̇' ——")
    print("     **长度都不一样**，拿它当去重键会得到不同的结果。")
    print("  8. DuckDB 的 NOCASE 排序是**稳定排序**；Polars 的 `sort()` 默认不保证稳定，")
    print("     要稳定得传 `maintain_order=True`。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    con = duckdb.connect()
    scenario_default_order(con)
    scenario_chinese(con)
    scenario_icu(con)
    scenario_scope(con)
    scenario_column_collation(con)
    scenario_polars_way(con)
    scenario_lower(con)
    scenario_cost(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
