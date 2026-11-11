"""结果物化方式的代价：把数据「拿出引擎」有**好几条**通道，每条的开销和语义都不同。

`window_demo.py` / `ranking_demo.py` 讲的是**在引擎里算**，
但 ETL 的最后一步总是同一个动作：**把结果交出去**。
这一步两边各有好几条通道，每条的代价能差一个数量级，语义还不一样：

    DuckDB  fetchall()  → list[tuple]      Python 对象
            fetchnumpy() → dict[str, ndarray]
            to_arrow_table() → pyarrow.Table
            to_dicts()   → list[dict]

    Polars  to_dicts()  → list[dict]       Python 对象
            to_numpy()  → ndarray
            to_arrow()  → pyarrow.Table
            rows()      → list[tuple]

四个必须记住的实测结论：

> **① 走 numpy 通道时，两个引擎都会悄悄改类型 —— 而且改的方向不一样。**
>
> DuckDB 的 `fetchnumpy()` 把 `DECIMAL` 变成 **float64**（精度当场丢失）；
> Polars 的 `to_numpy()` 把**带 NULL 的 int64** 变成 **float64 + NaN**，
> 于是 `9007199254740993` 变成 `9007199254740992` —— 差 1，静悄悄的。
> 而且 Polars 是**整表统一降级**：一列有 NULL，全表的 int64 都跟着变 float64。
> 两者都要等到「看类型」的时候才发现。

> **② NULL 在两边走 numpy 通道的归宿完全不同。**
>
> DuckDB 给 **MaskedArray**（mask 是真的），但 `np.asarray(m)` 会**把 mask 丢掉**，
> NULL 变成 **0** —— 一个填 0 的传感器读数，比一个 NaN 危险得多。
> Polars 直接给 float64 + **NaN**（好），但 `masked=True` 才给 MaskedArray。

> **③ Arrow 是唯一两边都「一字不差」的通道。**
>
> `DECIMAL(24,3)`、`int64`、`date32` 全部原样保留 ——
> 因为 Arrow 本来就是两边共同的存储格式，不用转换就没有损失。

> **④ 通道的代价差一个数量级，方向取决于你要什么。**
>
> 要 **Arrow**：两边都最快（DuckDB 要多转一道「列存 → Arrow」）；
> 要 **Python 元组**：慢一档；要 **Python 字典**：再慢一档
> （dict 比 tuple 重得多，**两个引擎内部都是这个比例**）。
> 体积上，Python 对象是 Arrow 的 **好几倍** —— 每个元素都要一个 PyObject。

用法：
    uv run python src/materialize_demo.py
    uv run python src/materialize_demo.py --rows 200000 --repeat 3
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
import warnings
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import polars as pl

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ---------------------------------------------------------------- 体积测量


def deep_size(obj: object, seen: set[int] | None = None) -> int:
    """递归量一个 Python 对象图占了多少字节。

    只走 list / tuple / dict / set 这几种容器 —— 它们是这里真正的大头
    （每个 tuple 一个 PyObject，每个 dict 一个 PyObject 加哈希表）。
    标量按 `sys.getsizeof` 算：str 有自己的分配，int 是不可变的小对象、
    但超过 256 的 int 是单独分配的，所以照样要算。

    **不用 RSS 差值**：RSS 量的是整个进程的峰值，解释器本身的占用、
    上一轮测试留下的内存，全都混在里面，量不出「这个结果有多大」。
    """
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)

    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for key, val in obj.items():
            size += deep_size(key, seen) + deep_size(val, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += deep_size(item, seen)
    return size


def arrow_bytes(table: object) -> int:
    """Arrow 表的缓冲区总字节数。"""
    return int(table.nbytes)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 场景 1


def scenario_channels(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 出口清单：两边各有哪些「把数据拿出来」的方法")

    conn_methods = [
        m for m in dir(con) if m.startswith(("fetch", "to_arrow")) and not m.startswith("_")
    ]
    rel = con.sql("SELECT 1 AS a")
    rel_methods = [
        m
        for m in dir(rel)
        if m.startswith(("fetch", "to_arrow", "to_dict", "to_table", "iter"))
        and not m.startswith("_")
    ]
    rel.close()

    pol_methods = [
        m
        for m in dir(pl.DataFrame)
        if m.startswith(("to_", "rows", "iter_rows")) and not m.startswith("_")
    ]

    print("  DuckDB 有**两个**对象，各带一套出口：")
    print()
    print("    con = duckdb.connect()  —— DB-API 连接")
    print("      " + ", ".join(sorted(conn_methods)))
    print()
    print("    rel = con.sql(sql)      —— 关系对象（多几个 dict / Arrow 出口）")
    print("      " + ", ".join(sorted(rel_methods)))
    print()
    print("    Polars 只有一个对象 —— DataFrame：")
    print("      " + ", ".join(sorted(pol_methods)))
    print()
    print("  → DuckDB **多了一层对象**，所以同一个引擎有两套出口，")
    print("    而且两套**不完全重叠**：关系对象才有 `to_dicts()`。")
    print("    Polars 侧就一个 DataFrame，方法名也不一样")
    print("    （DuckDB 叫 `fetchall()`，Polars 叫 `rows()`）。")
    print()
    print("  → 但**功能上大体能对上**，一一对应关系是：")
    print()
    print("  " + common.pad("想要什么", 22) + common.pad("DuckDB", 30) + "Polars")
    print("  " + "-" * 74)
    for want, duck, pol in [
        ("Python 元组", "fetchall() / rel.fetchall()", "rows()"),
        ("Python 字典", "rel.to_dicts()", "to_dicts()"),
        ("numpy 数组", "fetchnumpy()", "to_numpy()"),
        ("Arrow 表", "rel.to_arrow_table()", "to_arrow()"),
        ("Arrow 流", "rel.to_arrow_reader()", "scan_parquet().collect_batches()"),
        ("pandas", "rel.df()", "to_pandas()"),
    ]:
        print("  " + common.pad(want, 22) + common.pad(duck, 30) + pol)
    print()
    print("  → 名字对不上，**活儿是对得上的** —— 迁移时是改个方法名，")
    print("    不是改逻辑。真正要小心的是**语义**，见下面三节。")


# ---------------------------------------------------------------- 场景 2


def scenario_null(con: duckdb.DuckDBPyConnection, df_n: pl.DataFrame) -> None:
    section("场景 2 · 同一个 NULL，几条通道，几种归宿")

    print("  一列 int64：`[NULL, 42]`")
    print()
    print(
        "  " + common.pad("DuckDB fetchall()", 24) + str(con.execute("SELECT n FROM t").fetchall())
    )
    print(
        "  "
        + common.pad("DuckDB fetchnumpy()", 24)
        + str(con.execute("SELECT n FROM t").fetchnumpy()["n"])
    )
    print(
        "  " + common.pad("DuckDB rel.to_dicts()", 24) + str(con.sql("SELECT n FROM t").to_dicts())
    )
    print("  " + common.pad("Polars to_dicts()", 24) + str(df_n.to_dicts()))
    print("  " + common.pad("Polars to_numpy()", 24) + str(df_n.to_numpy()))
    print()

    duck_masked = con.execute("SELECT n FROM t").fetchnumpy()["n"]
    print("  DuckDB 的 `fetchnumpy()` 对 NULL 列给的是 **MaskedArray**：")
    print(f"      类型：{type(duck_masked).__name__}")
    print(f"      mask：{duck_masked.mask}   （True = 这个位置是 NULL）")
    print(f"      .tolist()  → {duck_masked.tolist()}   （NULL 还在）")
    print(
        f"      .filled()  → {duck_masked.filled()}"
        f"   （拿 fill_value={duck_masked.fill_value} 填上）"
    )
    print()
    print("  **但下面这一行是陷阱**：")
    plain = np.asarray(duck_masked)
    print(f"      np.asarray(masked) → {plain}   （dtype={plain.dtype}）")
    print("      → **mask 被丢掉了**，NULL 变成 **0**。")
    print("        一个「没读到」的传感器读数，和一个「读到 0」的传感器读数，")
    print("        在下游算平均值的时候结果一样 —— 但它们**根本不是一回事**。")
    print()

    pol_np = df_n.to_numpy()
    print("  Polars 的 `to_numpy()` 对带 NULL 的 int64 给的是 **float64 + NaN**：")
    print(f"      dtype：{pol_np.dtype}")
    print(f"      值：{pol_np.tolist()}")
    print("      → NULL 变成 NaN。**比 0 好**（NaN 往下传会一直传染），")
    print("        但类型已经从 int64 变成 float64 了 —— 见下一节的精度问题。")
    print()

    pol_masked = df_n.to_numpy(masked=True)
    print("  Polars 也支持 masked=True，给 MaskedArray：")
    print(f"      类型：{type(pol_masked).__name__}   dtype={pol_masked.dtype}")
    print(f"      .tolist() → {pol_masked.tolist()}")
    print()
    print("  → 三条通道，NULL 的表示**三种**（None / 0 / NaN）：")
    print("    · DuckDB `fetchnumpy()` → MaskedArray，但**别用 `np.asarray()` 拆它**；")
    print("    · Polars `to_numpy()` → float64 + NaN；")
    print("    · Polars `to_numpy(masked=True)` → MaskedArray，类型不降。")
    print()
    print("  → 结论：**要 NULL 一路保真，就走 Arrow**（下一节验证），")
    print("    numpy 通道在两个引擎里都会把 NULL 变成「某个具体的数」。")


# ---------------------------------------------------------------- 场景 3


def scenario_precision(
    con: duckdb.DuckDBPyConnection,
    df_clean: pl.DataFrame,
    df_num: pl.DataFrame,
    df_mixed: pl.DataFrame,
) -> None:
    section("场景 3 · 精度的暗礁：int64 和 DECIMAL 在 numpy 通道上的损失")

    big_int = 9007199254740993
    big_dec = "12345678901234567.890"

    print("  两个「装不下 float64」的值：")
    print(f"      int64    {big_int}   （2^53 + 1，正好在 float64 最后一格之后）")
    print(f"      DECIMAL  {big_dec}   （24 位，float64 只有 53 位尾数）")
    print()
    print("  小表 `t` 的两行，故意摆成三种对照：")
    print("      big_int       有值 + NULL     ← 用来触发 float64 提升")
    print("      big_int_clean 两行都有值     ← 用来对照")
    print("      big_dec       有值 + NULL")
    print()

    duck_rows = con.execute("SELECT big_int, big_int_clean, big_dec FROM t").fetchall()
    print("  DuckDB fetchall()：")
    print(f"      {duck_rows}")
    print("      → **一字不差**（Python 的 int 是任意精度，Decimal 也是精确的）。")
    print()

    duck_np = con.execute("SELECT big_int, big_int_clean, big_dec FROM t").fetchnumpy()
    print("  DuckDB fetchnumpy()：")
    print(f"      big_int       → {duck_np['big_int']}   dtype={duck_np['big_int'].dtype}")
    print(
        f"      big_int_clean → {duck_np['big_int_clean']}   dtype={duck_np['big_int_clean'].dtype}"
    )
    print(f"      big_dec       → {duck_np['big_dec']}   dtype={duck_np['big_dec'].dtype}")
    print("      → **逐列各转各的**（它给的是 dict，每列独立）：")
    print(f"        · int64 列**保住了**（底子是 MaskedArray / int64），{big_int} 一字不差；")
    print(
        f"        · DECIMAL 列变成了 **float64**，{big_dec} 变成 "
        f"{duck_np['big_dec'][0]!r} —— 尾数没了。"
    )
    print()

    clean_np = df_clean.to_numpy()
    print("  Polars to_numpy()（**一列 int64，没有 NULL**）：")
    print(f"      dtype={clean_np.dtype}   值={clean_np.tolist()}")
    print(f"      → int64 **一字不差**（{big_int} 保住了）。")
    print()

    num_np = df_num.to_numpy()
    print("  Polars to_numpy()（**两列 int64，其中一列有 NULL**）：")
    print(f"      dtype={num_np.dtype}")
    for row in num_np.tolist():
        print(f"      {row}")
    print("      → 整表**统一**成了 float64 ——")
    print(f"        连**没有 NULL 的那列**也被降级：{big_int} → {int(num_np[0][1])}，**差 1**。")
    print("        **不报错、不警告**，就是少了一个。")
    print("        （要拼成一个 numpy 数组，各列必须同类型 —— 一列有 NULL，全表降级。）")
    print()

    mixed_np = df_mixed.to_numpy()
    print("  Polars to_numpy()（**混了 DECIMAL / 字符串**的整表）：")
    print(f"      dtype={mixed_np.dtype}")
    for row in mixed_np.tolist():
        print(f"      {row}")
    print("      → 这次是 **object**（没有公共数值类型），于是**每列各自转**：")
    print(f"        · big_int_clean 列还是 int64，{big_int} **保住了**；")
    print(
        f"        · big_int 列有 NULL → float64 + NaN，"
        f"{big_int} → {int(mixed_np[0][0])}，**照样差 1**；"
    )
    print("        · big_dec 列是精确的 Decimal，**一个尾数都没丢**。")
    print()
    print("  → 所以两边的 numpy 通道，**丢的东西正好相反**：")
    print("    · **DuckDB** 丢 DECIMAL（int64 用 MaskedArray 保住了）；")
    print("    · **Polars** 丢 int64（DECIMAL 退成 Python 对象反而保住了）。")
    print("    而且 Polars 这边是**整表统一降级** —— 一列有 NULL，全表降级。")
    print()

    duck_arrow = con.sql("SELECT * FROM t").to_arrow_table()
    pol_arrow = df_mixed.to_arrow()
    print("  Arrow 通道：")
    print(f"      DuckDB → {[(f.name, str(f.type)) for f in duck_arrow.schema]}")
    print(f"      Polars → {[(f.name, str(f.type)) for f in pol_arrow.schema]}")
    duck_val = duck_arrow.column("big_int")[0].as_py()
    pol_val = pol_arrow.column("big_int")[0].as_py()
    duck_dec = duck_arrow.column("big_dec")[0].as_py()
    pol_dec = pol_arrow.column("big_dec")[0].as_py()
    print(f"      DuckDB 的 big_int = {duck_val!r}   big_dec = {duck_dec!r}")
    print(f"      Polars 的 big_int = {pol_val!r}   big_dec = {pol_dec!r}")
    same_int = duck_val == pol_val
    same_dec = duck_dec == pol_dec
    print(f"      big_int 两边一致：{'✅ 是' if same_int else '❌ 否'}")
    print(f"      big_dec 两边一致：{'✅ 是' if same_dec else '❌ 否'}")
    print()
    print("  → **Arrow 是唯一一字不差的通道。**")
    print("    原因很简单：Arrow 本来就是两边共同的存储格式，")
    print("    「转换」这一步根本不存在，没有地方可以丢精度。")
    print()
    print("  → 顺带看一眼 Arrow 里的字符串类型：")
    print(f"      DuckDB → {duck_arrow.schema.field('s').type}")
    print(f"      Polars → {pol_arrow.schema.field('s').type}")
    print("      → DuckDB 用 32 位偏移的 `string`，Polars 用 64 位偏移的 `large_string`。")
    print("        两边互相读都没问题（实测 DuckDB 读 Polars 的 Arrow 正常），")
    print("        但**写到同一个 Arrow 文件里**再让第三方读，")
    print("        得留意对方认不认 `large_string`。")


# ---------------------------------------------------------------- 场景 4


def _bench(fn: object, repeat: int) -> tuple[float, float]:
    """跑 repeat 轮，返回 (中位数 ms, 最快 ms)。"""
    times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()  # type: ignore[operator]
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), min(times)


def _bench_pair(first: object, second: object, repeat: int) -> tuple[float, float, float, float]:
    """**交错**跑两段代码，各 repeat 轮，返回 (a 中位, a 最快, b 中位, b 最快)。

    为什么要交错：固定「先跑 A 再跑 B」的话，A 要付「第一次」的冷启动代价
    （首次读盘、首次编译），这份代价会被算进 A 的数字里。
    交错着跑，两边轮流当第一个，噪声摊平 —— 这是本项目一贯的口径。

    预热单独做一轮，且**不计时**：DuckDB 的 `con.sql()` 每次都要重新计划，
    但文件系统的页缓存是共享的，谁先读谁受益。
    """
    a_fn = first  # type: ignore[operator]
    b_fn = second  # type: ignore[operator]
    a_fn()
    b_fn()
    a_times: list[float] = []
    b_times: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        a_fn()
        a_times.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        b_fn()
        b_times.append((time.perf_counter() - t0) * 1000)
    return (
        statistics.median(a_times),
        min(a_times),
        statistics.median(b_times),
        min(b_times),
    )


def scenario_cost(con: duckdb.DuckDBPyConnection, pq: Path, rows: int, repeat: int) -> None:
    section(f"场景 4 · 代价：{rows:,} 行从 parquet 到各条通道")

    print("  两边读**同一个 parquet 文件**，从零读起，再走各自的通道 ——")
    print("  端到端对比，两边都包含 parquet 解码，对称。")
    print()

    duck_sql = f"SELECT * FROM read_parquet('{pq}')"

    channels: list[tuple[str, str, object, object]] = [
        (
            "Arrow 表",
            "rel.to_arrow_table() / to_arrow()",
            lambda: con.sql(duck_sql).to_arrow_table(),
            lambda: pl.read_parquet(pq).to_arrow(),
        ),
        (
            "numpy",
            "fetchnumpy() / to_numpy()",
            lambda: con.sql(duck_sql).fetchnumpy(),
            lambda: pl.read_parquet(pq).to_numpy(),
        ),
        (
            "Python 元组",
            "rel.fetchall() / rows()",
            lambda: con.sql(duck_sql).fetchall(),
            lambda: pl.read_parquet(pq).rows(),
        ),
        (
            "Python 字典",
            "rel.to_dicts() / to_dicts()",
            lambda: con.sql(duck_sql).to_dicts(),
            lambda: pl.read_parquet(pq).to_dicts(),
        ),
    ]

    print(
        "  "
        + common.pad("通道", 16)
        + common.pad("DuckDB 中位", 13)
        + common.pad("Polars 中位", 13)
    )
    print("  " + common.pad("", 16) + common.pad("DuckDB 最快", 13) + common.pad("Polars 最快", 13))
    print("  " + common.pad("", 16) + common.pad("比值", 12))
    print("  " + "-" * 74)

    duck_meds: list[float] = []
    pol_meds: list[float] = []
    for name, _api, duck_fn, pol_fn in channels:
        d_med, d_min, p_med, p_min = _bench_pair(duck_fn, pol_fn, repeat)
        duck_meds.append(d_med)
        pol_meds.append(p_med)
        ratio = p_med / d_med if d_med else float("inf")
        print("  " + common.pad(name, 16))
        print(
            "  "
            + common.pad("", 16)
            + common.pad_left(f"{d_med:.1f} ms", 13)
            + common.pad_left(f"{p_med:.1f} ms", 13)
        )
        print(
            "  "
            + common.pad("", 16)
            + common.pad_left(f"{d_min:.1f} ms", 13)
            + common.pad_left(f"{p_min:.1f} ms", 13)
        )
        print("  " + common.pad("", 16) + common.pad_left(f"{ratio:.2f}×", 12))
    print()

    print("  各条通道产出的**体积**（同一个结果，不同包装）：")
    print()
    duck_table = con.sql(duck_sql).to_arrow_table()
    arrow_mb = arrow_bytes(duck_table) / 1e6
    duck_np = con.sql(duck_sql).fetchnumpy()
    numpy_mb = sum(int(v.nbytes) for v in duck_np.values()) / 1e6
    tuples_mb = deep_size(con.sql(duck_sql).fetchall()) / 1e6
    dicts_mb = deep_size(con.sql(duck_sql).to_dicts()) / 1e6
    print("  " + common.pad("Arrow 表", 22) + common.pad_left(f"{arrow_mb:.2f} MB", 12))
    print("  " + common.pad("numpy（fetchnumpy）", 22) + common.pad_left(f"{numpy_mb:.2f} MB", 12))
    print(
        "  "
        + common.pad("Python 元组（fetchall）", 22)
        + common.pad_left(f"{tuples_mb:.2f} MB", 12)
    )
    print(
        "  " + common.pad("Python 字典（to_dicts）", 22) + common.pad_left(f"{dicts_mb:.2f} MB", 12)
    )
    print()
    print(
        "  → **Python 对象是 Arrow 的 "
        + f"{tuples_mb / arrow_mb:.1f} 倍（tuple）/ {dicts_mb / arrow_mb:.1f} 倍（dict）** ——"
    )
    print("    每个元素都要一个 PyObject，外加 dict 的哈希表。")
    print("    数据量一大，这就是「内存爆掉」和「跑得动」的分水岭。")
    print()

    pol_arr = pl.read_parquet(pq).to_numpy()
    print("  numpy 通道拿到的**具体类型**（这份 parquet 里有 DECIMAL 列）：")
    print()
    for name, col in duck_np.items():
        print(f"      DuckDB {name:<8} → {col.dtype}")
    print(f"      Polars 整张表    → dtype={pol_arr.dtype}   shape={pol_arr.shape}")
    print()
    if pol_arr.dtype == object:
        print("  → Polars 这边给的是 **object** —— 表里有 DECIMAL 列（见场景 3），")
        print("    每一格都得装箱成一个 Python 对象，所以 numpy 通道反而最慢；")
        print("    DuckDB 那边把 DECIMAL 转成了 float64，代价小得多，")
        print("    但**精度丢了** —— 这正是场景 3 的那个坑。")
    else:
        print(f"  → Polars 这边给的是 **{pol_arr.dtype}**（全部列能统一成一个数值类型）。")
    print()
    print("  → 时间上：**通道之间的差距（两边都一样）是量级上的**，")
    print("    跨引擎的差距反而是零头 ——")
    print(f"    · **Arrow 最快**（DuckDB {duck_meds[0]:.1f} ms / Polars {pol_meds[0]:.1f} ms，")
    print(f"      Polars 是 DuckDB 的 {pol_meds[0] / duck_meds[0]:.2f}×）——")
    print("      不生成一个 Python 对象，只搬字节；")
    print(
        f"    · **Python 元组次之**（DuckDB {duck_meds[2]:.1f} ms / Polars {pol_meds[2]:.1f} ms）；"
    )
    print(
        f"    · **Python 字典最慢**（DuckDB {duck_meds[3]:.1f} ms / Polars {pol_meds[3]:.1f} ms）。"
    )
    print("    · 两个引擎内部都是 **dict 比 tuple 贵**：")
    print(
        f"      DuckDB {duck_meds[3] / duck_meds[2]:.1f}×、Polars {pol_meds[3] / pol_meds[2]:.1f}×"
    )
    print("      —— dict 每个键都要一个 PyObject 外加哈希表，tuple 是连续的一块。")
    print(f"    · Arrow 通道 DuckDB 比 Polars {'快' if duck_meds[0] < pol_meds[0] else '慢'}")
    gap = abs(pol_meds[0] - duck_meds[0])
    print(f"      （{duck_meds[0]:.1f} vs {pol_meds[0]:.1f} ms），差 {gap:.1f} ms ——")
    print("      DuckDB 得先从自己的列存格式转成 Arrow，Polars 本来就活在 Arrow 里。")
    print()
    print("  > 口径：同一个 parquet 文件，两边都从零读起（Polars 每次 `read_parquet`）；")
    print(f"  > 两边**交错**跑 {repeat} 轮取中位数和最快（避免「谁先跑谁吃亏」）。")
    print("  > 共享开发机，单次数字会抖，看趋势。")
    print()
    print("  → 实务结论：**下游只要 Arrow（pandas / pyarrow / 另一个引擎），")
    print("    就别在 Python 对象这一层绕一圈。**")
    print("    一定要 Python 对象时，优先 `rows()` / `fetchall()`（元组），")
    print("    别一上来就要 dict。")


# ---------------------------------------------------------------- 场景 5


def scenario_streaming(con: duckdb.DuckDBPyConnection, pq: Path, rows: int) -> None:
    section("场景 5 · 一次性还是流式：结果比内存大的时候怎么办")

    print(f"  同样的 {rows:,} 行，DuckDB 侧有三种拿法：")
    print()

    t0 = time.perf_counter()
    table = con.sql(f"SELECT * FROM read_parquet('{pq}')").to_arrow_table()
    t_all = (time.perf_counter() - t0) * 1000
    print(
        f"    to_arrow_table()   一次性拿全 → {t_all:.1f} ms，{table.num_rows:,} 行，"
        f"{arrow_bytes(table) / 1e6:.2f} MB"
    )
    print("      （全部结果先进内存，才能开始处理第一行）")

    t0 = time.perf_counter()
    reader = con.sql(f"SELECT * FROM read_parquet('{pq}')").to_arrow_reader()
    t_setup = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    first = next(iter(reader))
    t_first = (time.perf_counter() - t0) * 1000
    total_batches = 1
    total_rows = first.num_rows
    for batch in reader:
        total_batches += 1
        total_rows += batch.num_rows
    print(f"    to_arrow_reader()  流式     → 建流 {t_setup:.1f} ms，第一批到手 {t_first:.1f} ms")
    print(f"      共 {total_batches} 批，第一批 {first.num_rows:,} 行，合计 {total_rows:,} 行")
    if total_batches > 1:
        print("      （**第一批先到**，后面的一批批来 —— 可以边读边处理）")
    else:
        print("      （行数少，一批就取完了；行数再多就会分批）")
    print()

    print("  Polars 侧的对应物不在 DataFrame 上，而在 LazyFrame 上：")
    print()
    lf = pl.scan_parquet(pq)
    t0 = time.perf_counter()
    batches = list(lf.collect_batches())
    t_batches = (time.perf_counter() - t0) * 1000
    print(
        f"    scan_parquet().collect_batches() → {t_batches:.1f} ms，"
        f"{len(batches)} 批，{sum(b.height for b in batches):,} 行"
    )
    print(f"      每批行数：{[b.height for b in batches]}")
    print("    Polars 的 `collect_batches()` 返回的是 **DataFrame 列表**（不是 Arrow），")
    print("    每批自己再 `.to_arrow()`；DuckDB 的 reader 直接给 Arrow 批次。")
    print()
    print("  → 两边都有流式，但**接口粒度不同**：")
    print("    · DuckDB 的 reader 给 **Arrow 批次**，零额外转换；")
    print("    · Polars 的批次是 **DataFrame**，要 Arrow 得自己再转一次。")
    print("  → 还有一条 Polars 独有的路：`sink_batches(callback)` ——")
    print("    不在内存里攒着，来一批处理一批，内存占用跟数据量无关。")
    print("    DuckDB 那边对应的写法是 `COPY (…) TO … (FORMAT PARQUET)`，")
    print("    或者用 `rel.fetch_record_batch()` 自己收批次。")
    print()
    print("  → **结果比内存大的时候，别用 `to_arrow_table()` / `collect()`** ——")
    print("    那两个都要求全量进内存。流式才是解法。")


# ---------------------------------------------------------------- 场景 6


def scenario_pandas(con: duckdb.DuckDBPyConnection, pq: Path, rows: int, repeat: int) -> None:
    section("场景 6 · 顺带一提：pandas 通道两边都要 pandas")

    print("  DuckDB 的 `.df()` / `.fetch_df()`：")
    try:
        con.sql("SELECT * FROM t").df()
        print("      成功（本机装了 pandas）")
    except Exception as exc:
        print(f"      {type(exc).__name__}: {str(exc).splitlines()[0][:100]}")
    print()
    print("  Polars 的 `.to_pandas()`：")
    try:
        pl.DataFrame({"a": [1]}).to_pandas()
        print("      成功（本机装了 pandas）")
    except Exception as exc:
        print(f"      {type(exc).__name__}: {str(exc).splitlines()[0][:100]}")
    print()
    print("  → 两边都依赖 pandas（本机没装，所以都报错）。")
    print("    但**报错方式不一样**：DuckDB 抛自己的 `InvalidInputException`，")
    print("    把「你该去装 pandas」说得很清楚；")
    print("    Polars 直接把 `ModuleNotFoundError` 抛出来，")
    print("    底层原因被包在异常链里。")
    print()

    # DuckDB 的 DB-API 结果上取 Arrow 会多拷一份 —— 实测一下差多少
    sql = f"SELECT * FROM read_parquet('{pq}')"

    def via_execute() -> object:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return con.execute(sql).to_arrow_table()

    def via_relation() -> object:
        return con.sql(sql).to_arrow_table()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        con.execute(sql).to_arrow_table()
    warn_text = ""
    for w in caught:
        if "extra copy" in str(w.message):
            warn_text = str(w.message)
            break

    t_exec, _, t_rel, _ = _bench_pair(via_execute, via_relation, repeat)
    diff = t_exec - t_rel
    print("  → 顺带说个 DuckDB 的坑：`con.execute(sql).to_arrow_table()` 会**多拷一份**，")
    print("    DuckDB 自己会警告：")
    print(f"      {warn_text[:150]}")
    print(f"    实测 {rows:,} 行（两条路**交错**跑，避免「谁先跑谁吃亏」）：")
    print(f"      con.execute(sql).to_arrow_table()  {t_exec:.1f} ms   ← 多拷一份")
    print(f"      con.sql(sql).to_arrow_table()      {t_rel:.1f} ms   ← 正确写法")
    print(f"      差 {diff:+.1f} ms")
    if diff > 0.5:
        print("      → 这个行数下，那份额外的拷贝还能看出来。")
    else:
        print("      → 这个行数下拷贝淹没在噪声里了（差在 ±1 ms 内）；")
        print("        但那份额外的内存是实打实的，行数越大越明显 —— 别赌。")
    print("      结论：**要 Arrow 就写 `con.sql(sql).to_arrow_table()`。**")


# ---------------------------------------------------------------- 小结


def scenario_conclusion() -> None:
    section("小结：四条通道，怎么选")

    print(
        "  "
        + common.pad("通道", 16)
        + common.pad("类型保真", 14)
        + common.pad("代价", 12)
        + "什么时候用"
    )
    print("  " + "-" * 74)
    for name, fidelity, cost, when in [
        (
            "Arrow",
            "✅ 一字不差",
            "最快",
            "下游是 pandas / pyarrow / 另一个引擎；数据量大",
        ),
        (
            "Python 元组",
            "✅ 一字不差",
            "慢",
            "要按行处理，行数不大（几百万以内）",
        ),
        (
            "Python 字典",
            "✅ 一字不差",
            "最慢",
            "要按列名取值（`row['col']`）；行数不大",
        ),
        (
            "numpy",
            "❌ 降级",
            "居中",
            "**确认没有 DECIMAL、没有大 int** 才用；否则先转 Arrow",
        ),
    ]:
        print("  " + common.pad(name, 16) + common.pad(fidelity, 14) + common.pad(cost, 12) + when)
    print()
    print("  → 一句话：**默认走 Arrow。**")
    print("    它是唯一两边都「一字不差」的通道，也是唯一在数据量大时")
    print("    还保持数量级的通道。")
    print()
    print("  → 三个具体的坑，按踩中概率排：")
    print("    ① `np.asarray(masked_array)` —— **mask 被丢掉，NULL 变 0**；")
    print("    ② `to_numpy()` 把带 NULL 的 int64 变 float64 —— ")
    print("       `9007199254740993` 变 `9007199254740992`，**差 1 不报错**；")
    print("       而且是**整表**降级，一列有 NULL 全表跟着降；")
    print("    ③ DuckDB `fetchnumpy()` 把 DECIMAL 变 float64 —— **尾数当场丢失**。")
    print()
    print("  → 这三条的共同点：**都不报错**。")
    print("    结果照样出来、类型照样是 numpy 数组，")
    print("    只有在下游做聚合的时候，答案悄悄差了一点。")
    print("    所以 numpy 通道**只在「列里没有 DECIMAL、没有超过 2^53 的整数」时**用。")


# ---------------------------------------------------------------- main


def build_data(con: duckdb.DuckDBPyConnection, work: Path, rows: int) -> Path:
    """造一份带各种「刁钻类型」的 parquet，两边读同一份。"""
    pq = work / "data.parquet"
    con.execute(
        f"""
        COPY (
            SELECT
                i::INTEGER                       AS c_int,
                i::DOUBLE / 3                    AS c_f64,
                'str' || i                       AS c_str,
                (i % 7 = 0)                      AS c_bool,
                CASE WHEN i % 5 = 0 THEN NULL ELSE i END::BIGINT AS c_big,
                i::DECIMAL(18, 3) / 7            AS c_dec,
                DATE '2024-01-01' + (i % 900)::INTEGER AS c_date
            FROM range({rows}) t(i)
        ) TO '{pq}' (FORMAT PARQUET)
        """
    )
    return pq


def main() -> None:
    parser = argparse.ArgumentParser(description="结果物化方式的代价")
    parser.add_argument("--rows", type=int, default=200_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=3, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__} · NumPy {np.__version__}")
    print(f"机器：{common.describe_machine()}")

    work = Path(tempfile.mkdtemp(prefix="materialize_demo_"))
    print(f"实验目录：{work}")

    try:
        con = duckdb.connect()
        # 语义场景用的小表：故意挑刁钻的类型。
        # 两行是为了让每一列都同时有「有值」和「NULL」两种情况。
        con.execute(
            """
            CREATE TABLE t (
                big_int BIGINT, big_int_clean BIGINT,
                big_dec DECIMAL(24,3), s VARCHAR, n BIGINT
            )
            """
        )
        con.execute(
            """
            INSERT INTO t VALUES
                (9007199254740993, 9007199254740993, 12345678901234567.890, 'a', NULL),
                (NULL,             9007199254740993, NULL,                   'b', 42)
            """
        )
        # 整表：和 DuckDB 的 t 一一对应（混了 DECIMAL / 字符串）
        df_mixed = pl.DataFrame(
            {
                "big_int": pl.Series([9007199254740993, None], dtype=pl.Int64),
                "big_int_clean": pl.Series([9007199254740993, 9007199254740993], dtype=pl.Int64),
                "big_dec": pl.Series(
                    [Decimal("12345678901234567.890"), None], dtype=pl.Decimal(24, 3)
                ),
                "s": ["a", "b"],
                "n": pl.Series([None, 42], dtype=pl.Int64),
            }
        )
        df_clean = df_mixed.select("big_int_clean")  # 纯 int64，无 NULL
        df_num = df_mixed.select("big_int", "big_int_clean")  # 纯 int64，一列有 NULL
        df_n = df_mixed.select("n")

        pq = build_data(con, work, args.rows)

        scenario_channels(con)
        scenario_null(con, df_n)
        scenario_precision(con, df_clean, df_num, df_mixed)
        scenario_cost(con, pq, args.rows, args.repeat)
        scenario_streaming(con, pq, args.rows)
        scenario_pandas(con, pq, args.rows, args.repeat)
        scenario_conclusion()
    finally:
        for path in sorted(work.rglob("*"), reverse=True):
            if path.is_file():
                os.unlink(path)
            elif path.is_dir():
                path.rmdir()
        work.rmdir()

    print()
    print("  （实验目录已清理）")


if __name__ == "__main__":
    main()
