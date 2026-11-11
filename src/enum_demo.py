"""ENUM / Categorical：三种「枚举」，三种脾气。

三个东西长得像，脾气完全不一样：

  · DuckDB 的 **ENUM** —— 类型的一部分，值域写死在类型里。
  · Polars 的 **Enum** —— 同上，类别列表固定，而且**拒绝**新值。
  · Polars 的 **Categorical** —— 只是一个「字符串 + 整数编码」的优化，
    类别列表**跟着数据走**，来什么收什么。

真正的坑有四个，这个脚本逐个拆：

  1. **排序**：DuckDB ENUM 和 Polars Enum 都按**物理码**排（声明顺序），
     Polars Categorical 按**字典序**排。同一批数据，三个引擎三种顺序。
  2. **cast**：DuckDB 的 ENUM **没有**转整数的办法；Polars 的 `.cat.physical()` 有。
  3. **未知值**：DuckDB 报错、Polars Enum 报错、Polars Categorical **默默多一个类别**。
  4. **物理码**：它是「字符串缓存里的序号」，同一个字符串换个进程就是另一个码。

另外还实测了一件事：**Polars 自己的 parquet / Arrow IPC 往返保得住 Enum 和
Categorical**（靠 Arrow 的字段元数据），但**跨引擎一律掉成普通字符串**。

用法：
    uv run python src/enum_demo.py
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
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


# 一组故意「声明顺序 ≠ 字典序」的值 ——
# 只有这样，才能把「按物理码排」和「按字符串排」区分开。
VALUES = ["b", "a", "c"]
DECLARED = ["b", "a", "c"]  # 声明顺序：b 在前，字典序里 b 在最后


# ---------------------------------------------------------------- 探针工具
#
# 物理码的分配跟「这个进程之前见过哪些字符串」有关（场景 4 会展开）。
# 所以要拿「确定的」那组数，就另开一个干净进程测。


def _proc_out(code: str) -> list[str]:
    """在另一个进程里跑一段代码，把 stdout 按行拆回来。"""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if proc.returncode != 0:
        return [f"❌ {proc.stderr.splitlines()[-1][:60]}"]
    return proc.stdout.strip().splitlines()


def _fresh_codes(values: list[str], kind: str = "categorical") -> list[int]:
    """在**全新进程**里 cast 一遍，返回那边的物理码。

    另开进程是为了拿「缓存是干净的」那组码 —— 本进程里的码取决于
    这个进程之前见过哪些字符串（场景 4 会把这件事展开）。
    """
    if kind == "enum":
        expr = f"pl.Series({values!r}, dtype=pl.Enum({values!r}))"
    else:
        expr = f"pl.Series({values!r}).cast(pl.Categorical)"
    out = _proc_out(f"import polars as pl\ns = {expr}\nprint(s.cat.physical().to_list())\n")
    return ast.literal_eval(out[0])


def _fresh_codes_seq(steps: list[list[str]]) -> list[list[int]]:
    """在同一个新进程里，按顺序 cast 若干组值，返回每组的物理码。

    每一步都留着上一步的对象 —— 缓存因此一直活着，新字符串的码接着往下排。
    """
    body = "import polars as pl\n"
    for i, vals in enumerate(steps):
        body += (
            f"s{i} = pl.Series({vals!r}).cast(pl.Categorical)\n"
            f"print(s{i}.cat.physical().to_list())\n"
        )
    return [ast.literal_eval(line) for line in _proc_out(body)]


def _fresh_codes_reset(first: list[str], second: list[str]) -> list[list[int]]:
    """在同一个新进程里 cast 两组值，中间让前一批对象被回收掉。"""
    body = (
        "import gc, polars as pl\n"
        "def c(v):\n"
        "    s = pl.Series(v).cast(pl.Categorical)\n"
        "    return s.cat.physical().to_list()\n"
        f"print(c({first!r}))\n"
        "gc.collect()\n"
        f"print(c({second!r}))\n"
    )
    return [ast.literal_eval(line) for line in _proc_out(body)]


def _fmt_codes(values: list[str], codes: list[int]) -> str:
    """把码数组还原成「值=码」的映射，方便人眼核对。

    不直接打印裸码数组 —— 光看 [0, 1, 2] 不知道哪个是哪个。
    """
    return ", ".join(f"{v}={c}" for v, c in zip(values, codes, strict=True))


# ---------------------------------------------------------------- 场景 1


def scenario_three_kinds(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 三种枚举长什么样")

    con.execute("CREATE OR REPLACE TABLE e AS SELECT * FROM (VALUES ('b'), ('a'), ('c')) v(x)")
    con.execute("CREATE OR REPLACE TABLE de AS SELECT x::ENUM('b','a','c') AS e FROM e")

    duck_type = con.sql("SELECT typeof(e) FROM de LIMIT 1").fetchall()[0][0]
    duck_values = [r[0] for r in con.sql("SELECT e FROM de").fetchall()]
    pol_enum = pl.Series(VALUES, dtype=pl.Enum(DECLARED))
    pol_cat = pl.Series(VALUES).cast(pl.Categorical)

    print(f"  {'引擎':<20}{'类型':<32}{'值':<24}{'类别 / 值域'}")
    print("  " + "-" * 100)
    print(
        f"  {common.pad('DuckDB', 20)}{common.pad(duck_type, 32)}"
        f"{common.pad(str(duck_values), 24)}{common.pad('b, a, c', 24)}"
    )
    print(
        f"  {common.pad('Polars Enum', 20)}{common.pad(str(pol_enum.dtype), 32)}"
        f"{common.pad(str(pol_enum.to_list()), 24)}"
        f"{common.pad(str(pol_enum.dtype.categories.to_list()), 24)}"
    )
    print(
        f"  {common.pad('Polars Categorical', 20)}{common.pad(str(pol_cat.dtype), 32)}"
        f"{common.pad(str(pol_cat.to_list()), 24)}"
        f"{common.pad(str(pol_cat.unique(maintain_order=True).to_list()), 24)}"
    )
    print()
    print("  注意 DuckDB 那一行的「类别 / 值域」：**b 在 a 前面** —— 那是类型里写的顺序。")
    print("  Polars Enum 的 categories 也是 `['b', 'a', 'c']`（声明顺序）。")
    print(
        "  Polars Categorical 的类别是**数据里出现的**（这里也是 b, a, c，但顺序跟着出现顺序走）。"
    )
    print()
    print("  三者的差别一句话：")
    print("    · DuckDB ENUM / Polars Enum —— **值域固定**，来了不在名单里的值就报错。")
    print("    · Polars Categorical —— **值域不固定**，来了就收下，给它编个新码。")
    print()
    print("  物理码（`.cat.physical()`，场景 3 展开）现在先摆在这。")
    print("  （在**干净的新进程**里测的，免得受本进程缓存状态的干扰）：")
    enum_codes = _fresh_codes(VALUES, kind="enum")
    cat_codes = _fresh_codes(VALUES)
    print(
        f"    Enum        → {enum_codes}（{_fmt_codes(VALUES, enum_codes)}）—— 按**声明顺序**编的"
    )
    print(
        f"    Categorical → {cat_codes}（{_fmt_codes(VALUES, cat_codes)}）"
        "—— 按**进程里第一次见到的顺序**编的"
    )


# ---------------------------------------------------------------- 场景 2


def scenario_sorting(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 排序：三种枚举，三种顺序（这是最大的坑）")

    duck_sorted = [r[0] for r in con.sql("SELECT e FROM de ORDER BY e").fetchall()]
    duck_sorted_str = [r[0] for r in con.sql("SELECT e FROM de ORDER BY e::VARCHAR").fetchall()]
    pol_enum = pl.Series(VALUES, dtype=pl.Enum(DECLARED))
    pol_cat = pl.Series(VALUES).cast(pl.Categorical)

    print(f"  数据：{VALUES}")
    print(f"  声明顺序 {DECLARED} ≠ 字典序 {sorted(DECLARED)} —— 这一条是全篇的关键")
    print()
    print(f"  {'怎么排':<46}{'结果'}")
    print("  " + "-" * 76)
    print(f"  {common.pad('DuckDB  ORDER BY e（ENUM）', 46)}{duck_sorted}")
    print(f"  {common.pad('DuckDB  ORDER BY e::VARCHAR（按字符串）', 46)}{duck_sorted_str}")
    print(f"  {common.pad('Polars  Enum.sort()', 46)}{pol_enum.sort().to_list()}")
    print(f"  {common.pad('Polars  Categorical.sort()', 46)}{pol_cat.sort().to_list()}")
    print(f"  {common.pad('Python  sorted(字符串)', 46)}{sorted(VALUES)}")
    print()
    print("  结论：")
    print("    · **DuckDB ENUM 按物理码排**，也就是类型里声明的顺序（b, a, c）。")
    print("      它不是按字符串排的 —— `ORDER BY e::VARCHAR` 才给字典序（a, b, c）。")
    print("    · **Polars Enum 也按物理码排**（b, a, c）—— 这一条跟 DuckDB 一致。")
    print("    · **Polars Categorical 按字典序排**（a, b, c）—— 跟上面两个都不一样。")
    print()
    print("  这意味着：**DuckDB 的 ENUM 和 Polars 的 Enum 排序结果一样**")
    print("  （都是物理码序），而 **Polars 的 Categorical 跟它们都不一样**（字典序）。")
    print("  迁移的时候按「名字像的」对，掉坑里 —— DuckDB ENUM ↔ Polars Enum 是一对，")
    print("  DuckDB ENUM ↔ Polars Categorical 不是。")
    print()
    print("  实践上的后果：")
    print("    · 用 ENUM 存「状态」「等级」这类**有业务顺序**的值（低 → 中 → 高），")
    print("      得**故意把声明顺序写成业务顺序**，这样 `ORDER BY` 才是你要的。")
    print("    · 同一批数据在 DuckDB（ENUM）和 Polars（Categorical）里排出来顺序不同，")
    print("      LIMIT 还会因此取到**不同的行**（跟 `order_demo.py` 里的并列排序一个道理）。")
    print()
    print("  还有一个容易忽略的：Polars 的 `group_by` 之后**不保证**按组键排序，")
    print("  DuckDB 的 `GROUP BY` 也不保证 —— 但 DuckDB 的 ENUM 分组键在")
    print("  `ORDER BY` 里会按物理码走，Polars 的 Categorical 会按字典序走。")
    print("  要对齐就两边都显式写排序规则，别靠默认值。")


# ---------------------------------------------------------------- 场景 3


def scenario_cast(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · cast：转字符串、转整数")

    duck_str = [r[0] for r in con.sql("SELECT e::VARCHAR FROM de").fetchall()]
    pol_enum = pl.Series(VALUES, dtype=pl.Enum(DECLARED))

    print(f"  {'转成什么':<40}{'DuckDB':<26}{'Polars'}")
    print("  " + "-" * 92)
    print(
        f"  {common.pad('字符串', 40)}{common.pad(str(duck_str), 26)}"
        f"{common.pad(str(pol_enum.cast(pl.String).to_list()), 26)}"
    )
    try:
        duck_int = [r[0] for r in con.sql("SELECT e::INTEGER FROM de").fetchall()]
    except Exception as exc:
        duck_int = f"❌ {str(exc).splitlines()[0][:24]}"
    try:
        pol_int = pol_enum.cast(pl.Int32).to_list()
    except Exception as exc:
        pol_int = f"❌ {str(exc).splitlines()[0][:24]}"
    print(
        f"  {common.pad('整数', 40)}{common.pad(str(duck_int), 26)}{common.pad(str(pol_int), 26)}"
    )
    print()

    print("  **DuckDB 的 ENUM 没有办法直接转成整数。**")
    print("    `e::INTEGER` 报的是「Could not convert string 'b' to INT32」——")
    print("    它想按字符串转，不是按物理码转。DuckDB 里能拿到的只有：")
    print(f"      `e::VARCHAR`      → {duck_str}")
    enum_range = con.sql("SELECT enum_range(NULL::ENUM('b','a','c'))").fetchall()[0][0]
    print(f"      `enum_range(...)` → {enum_range}（拿到的是全部候选值）")
    print("    想要物理码，得自己在外面数（`list_position(enum_range(...), e)`）。")
    print()
    print("  **Polars 侧有现成的**：`.cat.physical()` 直接给物理码。")
    enum_codes = _fresh_codes(VALUES, kind="enum")
    cat_codes = _fresh_codes(VALUES)
    print(f"    Enum        .cat.physical() → {enum_codes}（{_fmt_codes(VALUES, enum_codes)}）")
    print(f"    Categorical .cat.physical() → {cat_codes}（{_fmt_codes(VALUES, cat_codes)}）")
    print("    （Enum 的码按**声明顺序**编 —— 类别表写死了，码跟着表走；")
    print("      Categorical 的码按**这个进程第一次见到各个字符串的顺序**编 ——")
    print("      它的类别列表本来就是跟着数据走的。）")
    print("      这两组码**碰巧**一样：干净进程里 b, a, c 就是按这个顺序被见的。")
    print("      场景 4 会把「碰巧」这两个字拿掉 —— 码其实很不稳定。）")
    print()
    print("  所以两边「拿整数」的代价差得很远：")
    print("    · DuckDB：没有直接的办法，要绕 `list_position`。")
    print("    · Polars：一行 `.cat.physical()`。")
    print()
    print("  但反过来有个更实用的差别：**Polars 的 `cast(pl.Int32)` 直接报错**")
    print("  （提示你用 `.cat.physical()`），DuckDB 的 `::INTEGER` 报的是")
    print("  「转不了这个字符串」—— 两种报错的**含义不一样**，排查时容易看岔。")


# ---------------------------------------------------------------- 场景 4


def scenario_unknown_value(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 未知值：报错、报错、还是默默多一个类别")

    print("  往枚举里塞一个不在名单里的值（比如 'z'）：")
    print()

    try:
        con.execute("INSERT INTO de VALUES ('z')")
        duck_ins = "✅ 居然成功了"
    except Exception as exc:
        duck_ins = f"❌ {type(exc).__name__}"
    print(f"  {common.pad('DuckDB ENUM', 22)}{duck_ins}")
    print(f"      {str(con.sql('SELECT count(*) FROM de').fetchall()[0][0])} 行（没插进去）")

    try:
        pol_enum = pl.Series(["a", "z"], dtype=pl.Enum(["a", "b"])).to_list()
        pol_enum_res = f"✅ {pol_enum}"
    except Exception as exc:
        pol_enum_res = f"❌ {type(exc).__name__}"
    print(f"  {common.pad('Polars Enum', 22)}{pol_enum_res}")

    try:
        cat = pl.Series(["a", "z"]).cast(pl.Categorical)
        pol_cat_res = f"✅ {cat.to_list()}"
        cat_codes = cat.cat.physical().to_list()
    except Exception as exc:
        pol_cat_res = f"❌ {type(exc).__name__}"
        cat_codes = []
    print(f"  {common.pad('Polars Categorical', 22)}{pol_cat_res}")
    print(f"      物理码 {cat_codes} —— 'z' 被编了个新码 **{cat_codes[-1] if cat_codes else '?'}**")
    print()

    print("  三种处理方式，三种后果：")
    print("    · DuckDB ENUM —— **报错**，数据进不去。安全。")
    print("    · Polars Enum —— **报错**（InvalidOperationError），同样安全。")
    print("    · Polars Categorical —— **默默收下**，给它编一个新物理码。")
    print()
    print("  Categorical 这一条最危险：值看着没丢，但**类别列表变了**。")
    print("  如果你按物理码 join（`join` on 物理码、或者把码存成整数列），")
    print("  新类别会跟老类别对不上。")
    print()

    # ---- 物理码到底是什么
    print("  最隐蔽的一层：**物理码是「字符串缓存里的序号」，不是字符串的身份。**")
    print()
    seq = _fresh_codes_seq([VALUES, ["b"], ["z"], ["c", "b", "a"]])
    print("  在一个新进程里，按顺序 cast（每一步都留着上一步的对象）：")
    print(f"    ['b','a','c'] → 码 {seq[0]}  （{_fmt_codes(VALUES, seq[0])}）")
    print(f"    ['b']         → 码 {seq[1]}  （'b' 已经在缓存里了，位置不变）")
    print(f"    ['z']         → 码 {seq[2]}  （新字符串，接在缓存后面）")
    print(f"    ['c','b','a'] → 码 {seq[3]}  （**不是** [0, 1, 2]！'b' 早就占着 0，'a' 占着 1）")
    print()
    reset = _fresh_codes_reset(VALUES, ["z"])
    print("  同一个进程里，上一批对象被回收之后：")
    print(f"    先 cast ['b','a','c'] → 码 {reset[0]}")
    print(f"    再 cast ['z']         → 码 {reset[1]}  ← **从 0 重新开始**")
    print()
    print("  最后看跨进程 —— 同一个 'z'，两个进程给两个码：")
    print(f"    进程甲（只见过 z）        → {_fresh_codes(['z'])}")
    print(f"    进程乙（见过 a, b, c, z） → {_fresh_codes(['a', 'b', 'c', 'z'])}")
    print()
    print("  所以：**物理码只在「同一个进程、同一个缓存还活着」的时候有意义。**")
    print("  跨进程、跨文件就不能直接用了 —— 把物理码存进数据库当主键，")
    print("  等于存了一串随机数。")
    print("  （好消息：Polars 自己的 parquet / Arrow IPC 往返**保得住**码，")
    print("    因为类别列表跟着文件一起存了 —— 场景 5 会验证。）")


# ---------------------------------------------------------------- 场景 5


def scenario_roundtrip(con: duckdb.DuckDBPyConnection, tmpdir: Path) -> None:
    section("场景 5 · 往返：Polars 自己保得住，跨引擎保不住")

    df = pl.DataFrame(
        {
            "e": pl.Series(VALUES, dtype=pl.Enum(DECLARED)),
            "c": pl.Series(VALUES).cast(pl.Categorical),
        }
    )

    pq_path = tmpdir / "enum.parquet"
    ipc_path = tmpdir / "enum.arrow"
    df.write_parquet(pq_path)
    df.write_ipc(ipc_path)

    print("  Polars 自己写的文件，自己读回来：")
    print(f"    parquet 读回 → {pl.read_parquet(pq_path).schema}")
    print(f"    Arrow IPC 读回 → {pl.read_ipc(ipc_path).schema}")
    print()
    print("  **保住了。** 靠的是 Arrow 的字段元数据：")
    arrow = df.to_arrow()
    for field in arrow.schema:
        meta = field.metadata or {}
        keys = [k.decode() for k in meta]
        tag = meta.get(b"_PL_ENUM_VALUES2") or meta.get(b"_PL_CATEGORICAL2")
        print(f"    字段 {field.name}: Arrow 类型 = {field.type}")
        print(f"      字段元数据 = {keys} → {tag}")
    print()
    print("  所以「Arrow 往返掉成普通 String」这个说法**在 Polars 2.1 上不成立** ——")
    print("  只要 Arrow 的字段元数据一起走，Enum 和 Categorical 都活得好好的。")
    print()
    back = pl.read_parquet(pq_path)
    print(
        f"  连物理码都保住了：写出去 {df['c'].cat.physical().to_list()} → "
        f"读回来 {back['c'].cat.physical().to_list()}"
    )
    print()

    print("  但**跨引擎就不行了**：")
    duck_types = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{pq_path}')").fetchall()
    duck_rows = con.sql(f"SELECT * FROM read_parquet('{pq_path}')").fetchall()
    print(f"    DuckDB 读 Polars 的 parquet，类型 → {[t[1] for t in duck_types]}")
    print(f"    DuckDB 读到的值              → {duck_rows}")
    print()
    print("    **枚举定义整个没了**，DuckDB 看到的就是两个 VARCHAR。")
    print("    （DuckDB 自己写的 ENUM 也一样：写 parquet 时就变成 VARCHAR 了，")
    print("      读回来是 VARCHAR，连自己都不认识自己的枚举了。）")
    print()

    print("  顺带看一眼 DuckDB 的 .duckdb 文件：")
    db_path = tmpdir / "enum.duckdb"
    c2 = duckdb.connect(str(db_path))
    c2.execute("CREATE TABLE t(e ENUM('b','a','c'))")
    c2.execute("INSERT INTO t VALUES ('b')")
    c2.close()
    c3 = duckdb.connect(str(db_path))
    keep = c3.sql("DESCRIBE t").fetchall()[0][1]
    c3.close()
    print(f"    DuckDB 自己的数据库文件里 → {keep}（枚举定义还在）")
    print()
    print("  结论：**ENUM 只在 DuckDB 自己的数据库文件里保得住。**")
    print("  一旦落成 parquet（或者交给别的引擎），它就是个普通字符串 ——")
    print("  枚举的价值（值域约束 + 排序规则）全没了。")


# ---------------------------------------------------------------- 场景 6


def scenario_practical(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 落地建议：三种枚举各适合什么")

    print(f"  {'场景':<34}{'选谁':<18}{'理由'}")
    print("  " + "-" * 92)
    rows = [
        (
            "值域由业务定义、不许乱填",
            "DuckDB ENUM",
            "数据库层就挡住脏数据；Polars 侧要靠 Enum 才有同样的约束",
        ),
        (
            "两边都要用、要互相对账",
            "Polars Enum",
            "值域固定，两边对得上；DuckDB 侧对应 ENUM",
        ),
        (
            "只是分组键 / 关联键，想省内存",
            "Polars Categorical",
            "字典编码省内存，但**类别列表会跟着数据变**，不能当约束用",
        ),
        (
            "要过 parquet 给别的系统",
            "字符串",
            "枚举定义本来也过不去，不如一开始就用字符串 + 应用层校验",
        ),
    ]
    for scene, who, why in rows:
        print(f"  {common.pad(scene, 34)}{common.pad(who, 18)}{why}")
    print()
    print("  三条硬规矩：")
    print("    1. **别拿物理码当业务值用。** 物理码是存储细节，")
    print("       Categorical 每次读文件的码都可能不一样。")
    print("    2. **要排序就显式写排序规则。** DuckDB ENUM 按物理码、")
    print("       Polars Categorical 按字典序 —— 别靠默认。")
    print("    3. **过 parquet 前先想清楚枚举定义怎么办。**")
    print("       DuckDB 那边得自己存一份「类别清单」表，")
    print("       读回来之后用 `::ENUM(...)` 重新贴回去。")


# ---------------------------------------------------------------- 小结


def summary() -> None:
    section("小结")
    print("  1. **排序是最容易踩的坑**：DuckDB ENUM 和 Polars Enum 按物理码（声明顺序）排，")
    print("     Polars Categorical 按字典序排。三个引擎三种顺序。")
    print("  2. **cast 不对等**：DuckDB 的 ENUM 转不了整数（`::INTEGER` 报错），")
    print("     Polars 有 `.cat.physical()`；两边都能 `::VARCHAR` / `cast(pl.String)`。")
    print("  3. **未知值**：DuckDB ENUM 报错、Polars Enum 报错、")
    print("     **Polars Categorical 默默多一个类别**（并给它编新物理码）。")
    print("  4. **物理码是进程内的缓存序号**：同一个字符串换个进程就是另一个码")
    print("     （'z' 在两个进程里分别是 0 和 3），对象回收后码还会从 0 重排。")
    print("  5. **往返**：Polars 自己的 parquet / Arrow IPC 往返**保得住** Enum 和 Categorical")
    print("     （靠 Arrow 字段元数据 `_PL_ENUM_VALUES2` / `_PL_CATEGORICAL2`），")
    print("     连物理码都保得住；但**跨引擎一律掉成普通字符串**，")
    print("     DuckDB 自己的 ENUM 写 parquet 也一样掉。")
    print("  6. ENUM 只在 DuckDB 自己的 `.duckdb` 文件里保得住。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="ENUM / Categorical 的排序、cast、未知值与往返")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    with tempfile.TemporaryDirectory(prefix="enum_demo_") as tmp:
        tmpdir = Path(tmp)
        scenario_three_kinds(con)
        scenario_sorting(con)
        scenario_cast(con)
        scenario_unknown_value(con)
        scenario_roundtrip(con, tmpdir)
        scenario_practical(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
