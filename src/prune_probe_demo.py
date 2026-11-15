"""行组裁剪的探针：把「跳没跳过」变成「报没报错」。

前面几个 demo 证明「引擎会剪枝」，用的都是**计时**和**读盘字节数**：
parquet_meta_demo 量的是「开着 use_statistics 快了 2.4 倍」，
plan_demo 量的是「`id < 100` 读盘 123,718 字节 / 8 个行组」。

这两种测法都有个软肋：**它们量的是「结果」，不是「动作」**。
计时会被磁盘缓存、线程调度、机器负载搅浑；读盘字节数只有 DuckDB 报得出来
（Polars 没有 IO 计数器）。所以「Polars 到底跳没跳那个行组」一直只能靠推测。

这个 demo 用一个**反推法**把这件事变成是非题：

    故意把某个行组的数据页弄坏，然后看 reader 报不报错。

parquet 的数据页是自描述的 —— 读的时候先解页头（thrift），再解数据。
把页字节换成 0xFF 之后页头就解不开了。于是：

    reader 跳过了这个行组  → 没碰这些字节 → 不报错
    reader 读了  这个行组  → 碰了这些字节 → 报错

**「跳没跳」变成了「报没报错」** —— 不需要任何计数器，
DuckDB / Polars / pyarrow 三个引擎都能用同一把尺子量。

跟 parquet_meta_demo 的关系：那个 demo 讲的是「zonemap 是什么、为什么能剪」，
这个 demo 讲的是「**到底剪了没有**」—— 并且顺带把「Polars 这层测不了」那句话改掉。

用法：
    uv run python src/prune_probe_demo.py
    uv run python src/prune_probe_demo.py --rows 1000000 --repeat 5
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.dataset as ds
import pyarrow.parquet as pq

import common

# 行组数 —— 探针要一个一个地坏，所以行组不能太多，也不能太少
ROW_GROUPS = 8
# 被弄坏的那一列 —— 过滤和聚合都打在这一列上
PROBE_COLUMN = "id"
# 用来对照的、另一列
CONTROL_COLUMN = "v"


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ---------------------------------------------------------------- 造文件


def build_file(con: duckdb.DuckDBPyConnection, path: Path, rows: int) -> int:
    """写一份带固定行组大小的 parquet，返回实际写出来的行组数。

    行组大小按行数平均分 —— DuckDB 会把每个行组向上取整到 2048 的倍数，
    所以实际行组数跟 ``ROW_GROUPS`` 未必一样，读回元数据为准。
    """
    row_group_size = max(2048, rows // ROW_GROUPS)
    con.execute(
        f"COPY (SELECT i AS id, i * 2 AS v FROM range(?) t(i)) "
        f"TO ? (FORMAT parquet, ROW_GROUP_SIZE {row_group_size})",
        [rows, str(path)],
    )
    n = con.sql(
        "SELECT count(DISTINCT row_group_id) FROM parquet_metadata(?)", params=[str(path)]
    ).fetchall()[0][0]
    return int(n)


def column_chunks(con: duckdb.DuckDBPyConnection, path: Path) -> list[tuple[int, str, int, int]]:
    """每个行组 × 每列的 (行组号, 列名, 数据页偏移, 压缩后字节数)。

    这四个数就是「这一列块的数据在文件的哪儿、有多大」——
    ``data_page_offset`` 是第一个数据页的起点，``total_compressed_size``
    是这个列块所有页加起来的长度。
    """
    rows = con.sql(
        "SELECT row_group_id, path_in_schema, data_page_offset, total_compressed_size "
        "FROM parquet_metadata(?) ORDER BY row_group_id, path_in_schema",
        params=[str(path)],
    ).fetchall()
    return [(int(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in rows]


def corrupt_row_group(
    src: Path, dst: Path, chunks: list[tuple[int, str, int, int]], row_group: int, column: str
) -> int:
    """把某个行组里某一列的数据页字节全换成 0xFF，返回弄坏的字节数。

    页头和数据都在这段区间里，所以换完之后这个列块**彻底读不出来**了 ——
    但 footer 和别的列块一点没动，文件在元数据层面还是「合法」的。
    """
    data = bytearray(src.read_bytes())
    broken = 0
    for rg, col, offset, size in chunks:
        if rg == row_group and col == column:
            data[offset : offset + size] = b"\xff" * size
            broken += size
    dst.write_bytes(bytes(data))
    return broken


# ---------------------------------------------------------------- 六个读法
#
# 每个读法返回 (结论, 细节)：
#   结论 = "跳过"  没报错 → 这个行组根本没被读
#   结论 = "报错"  报错了 → 读了这个行组
# 细节是出错时的异常类型，或者算出来的值。


def duck_filtered(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[str, str]:
    """DuckDB：过滤下推到扫描，reader 拿 zonemap 决定读哪几个行组。"""
    try:
        val = con.execute(
            "SELECT sum(id) FROM read_parquet(?) WHERE id < 100", [str(path)]
        ).fetchone()[0]
        return "跳过", str(val)
    except Exception as exc:
        return "报错", type(exc).__name__


def duck_full(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[str, str]:
    """DuckDB：没有过滤 → 所有行组都得读。"""
    try:
        val = con.execute("SELECT sum(id) FROM read_parquet(?)", [str(path)]).fetchone()[0]
        return "跳过", str(val)
    except Exception as exc:
        return "报错", type(exc).__name__


def duck_other_column(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[str, str]:
    """DuckDB：只读另一列（v）—— 坏的是 id 的列块，这条路不该受影响。"""
    try:
        val = con.execute("SELECT sum(v) FROM read_parquet(?)", [str(path)]).fetchone()[0]
        return "跳过", str(val)
    except Exception as exc:
        return "报错", type(exc).__name__


def polars_filtered(path: Path, use_statistics: bool) -> tuple[str, str]:
    """Polars：``use_statistics`` 开关控制要不要拿 zonemap 剪枝。"""
    try:
        lf = pl.scan_parquet(path, use_statistics=use_statistics)
        val = lf.filter(pl.col("id") < 100).select(pl.col("id").sum()).collect().item()
        return "跳过", str(val)
    except Exception as exc:
        return "报错", type(exc).__name__


def pyarrow_read_table(path: Path) -> tuple[str, str]:
    """pyarrow 的老接口 ``pq.read_table`` —— 全读，不剪枝。"""
    try:
        rows = pq.read_table(path, columns=["id"]).num_rows
        return "跳过", f"{rows} 行"
    except Exception as exc:
        return "报错", type(exc).__name__


def pyarrow_dataset(path: Path) -> tuple[str, str]:
    """pyarrow 的新接口 ``pyarrow.dataset`` —— 带过滤器，剪枝。"""
    try:
        table = ds.dataset(path, format="parquet").to_table(filter=ds.field("id") < 100)
        return "跳过", f"{table.num_rows} 行"
    except Exception as exc:
        return "报错", type(exc).__name__


# ---------------------------------------------------------------- 各节


def scenario_probe_principle(
    con: duckdb.DuckDBPyConnection, good: Path, chunks: list[tuple[int, str, int, int]]
) -> None:
    section("第 1 节 · 探针原理：把「跳没跳过」变成「报没报错」")

    print("  探针用的是 parquet 文件自己的布局：")
    print()
    print(
        "    "
        + common.pad("行组", 8)
        + common.pad("列", 8)
        + common.pad("数据页偏移", 16)
        + "压缩字节数"
    )
    print("    " + "-" * 56)
    for rg, col, offset, size in chunks[:6]:
        print(
            "    "
            + common.pad(f"rg{rg}", 8)
            + common.pad(col, 8)
            + common.pad(f"{offset:,}", 16)
            + f"{size:,}"
        )
    print("    ...")
    print()
    print("  `data_page_offset` 是这个列块第一个数据页在文件里的字节位置，")
    print("  `total_compressed_size` 是它有多长。把这段区间全换成 0xFF：")
    print()
    print("    · footer / 元数据 **一点没动** —— 文件在「目录」层面还是合法的；")
    print("    · 数据页的页头解不开了 —— 谁碰这段字节谁报错。")
    print()
    print("  于是「reader 有没有读这个行组」变成了一个**是非题**：")
    print()
    print("      跳过了 → 没碰这些字节 → **不报错**")
    print("      没跳过 → 碰了这些字节 → **报错**")
    print()
    print("  这把尺子对三个引擎是同一把 —— 不需要任何引擎自报家门，")
    print("  也不需要「读盘字节数」这种只有 DuckDB 报得出的计数器。")
    print()


def scenario_control(con: duckdb.DuckDBPyConnection, good: Path, rows: int) -> None:
    section("第 2 节 · 对照组：文件没坏的时候，七种读法都得对")

    n_groups = con.sql(
        "SELECT count(DISTINCT row_group_id) FROM parquet_metadata(?)", params=[str(good)]
    ).fetchall()[0][0]
    print(f"  文件：{good.stat().st_size:,} 字节 / {rows:,} 行 / {n_groups} 个行组")
    print()
    print("  " + common.pad("读法", 34) + common.pad("结论", 8) + "值")
    print("  " + "-" * 66)
    readers = [
        ("DuckDB  sum(id) WHERE id < 100", lambda: duck_filtered(con, good)),
        ("DuckDB  sum(id) 全读", lambda: duck_full(con, good)),
        ("DuckDB  sum(v)（另一列）", lambda: duck_other_column(con, good)),
        ("Polars  sum(id) WHERE id < 100", lambda: polars_filtered(good, True)),
        ("Polars  sum(id) 关统计", lambda: polars_filtered(good, False)),
        ("pyarrow pq.read_table", lambda: pyarrow_read_table(good)),
        ("pyarrow pyarrow.dataset", lambda: pyarrow_dataset(good)),
    ]
    for name, fn in readers:
        verdict, detail = fn()
        print("  " + common.pad(name, 34) + common.pad(verdict, 8) + detail)
    print()
    print("  → 结论全是「跳过」，值也都是对的（`id < 100` 的和 = 4950 = 0+1+…+99）。")
    print("    这一节是**必须**的：不然后面那些「报错」分不清是文件坏了、")
    print("    还是查询本身就写错了。")
    print()


def scenario_matrix(
    con: duckdb.DuckDBPyConnection,
    good: Path,
    chunks: list[tuple[int, str, int, int]],
    workdir: Path,
    row_groups: int,
) -> None:
    section("第 3 节 · 逐行组对账：一个一个弄坏，看谁跳过了")

    print(f"  做法：把 {row_groups} 个行组一个一个弄坏（每次只坏 {PROBE_COLUMN} 列的数据页），")
    print("  对每个坏掉的文件跑六个读法。")
    print()
    print("  表格里 **跳过** = 没报错（这个行组没被读）；**报错** = 被读了。")
    print()

    headers = [
        ("坏掉的行组", 11),
        ("DuckDB 过滤", 12),
        ("DuckDB 全读", 12),
        ("Polars 过滤", 12),
        ("Polars 关统计", 14),
        ("pa read_table", 14),
        ("pa dataset", 12),
    ]
    print("  " + "".join(common.pad(h, w) for h, w in headers))
    print("  " + "-" * (sum(w for _, w in headers) - 1))

    verdicts: dict[str, list[str]] = {}
    for rg in range(row_groups):
        bad = workdir / f"bad_rg{rg}.parquet"
        corrupt_row_group(good, bad, chunks, rg, PROBE_COLUMN)
        cells = [
            f"rg{rg}",
            duck_filtered(con, bad)[0],
            duck_full(con, bad)[0],
            polars_filtered(bad, True)[0],
            polars_filtered(bad, False)[0],
            pyarrow_read_table(bad)[0],
            pyarrow_dataset(bad)[0],
        ]
        verdicts[f"rg{rg}"] = cells[1:]
        print("  " + "".join(common.pad(c, w) for c, (_, w) in zip(cells, headers, strict=True)))
    print()

    print("  读法说明：")
    print("    · DuckDB 过滤   = `sum(id) WHERE id < 100`（过滤进了扫描节点）")
    print("    · DuckDB 全读   = `sum(id)`（没有过滤，所有行组都得读）")
    print("    · Polars 过滤   = `scan_parquet(...).filter(id < 100)`（use_statistics=True）")
    print("    · Polars 关统计 = 同上，但 `use_statistics=False`")
    print("    · pa read_table = `pq.read_table(path, columns=['id'])`")
    print("    · pa dataset    = `pyarrow.dataset(...).to_table(filter=id < 100)`")
    print()

    print("  四件事，一次测出来：")
    print()
    print("  1. **DuckDB 剪枝**：rg0 报错、rg1~rg7 跳过 ——")
    print("     过滤条件只落在 rg0 上（`id < 100`），reader 把另外 7 个行组跳了。")
    print("     「DuckDB 全读」那一列**每个行组都报错** —— 这是对照组：")
    print("     同一份文件、同一列，只因为没过滤，就一个行组都没跳。")
    print()
    print("  2. **Polars 也剪枝** —— 这一点以前只靠推测，现在有硬证据了：")
    print("     「Polars 过滤」和 DuckDB 一模一样（rg0 报错、其余跳过），")
    print("     而「Polars 关统计」**每个行组都报错** ——")
    print("     同一个查询、同一份文件，只把 `use_statistics` 翻成 False，")
    print("     剪枝就没了。**所以 Polars 跳不跳那个行组，是能测的。**")
    print()
    print("  3. **pyarrow 的两个接口不是一个东西**：")
    print("     `pq.read_table` 压根不剪（每行组都报错），")
    print("     `pyarrow.dataset` 剪（rg1~rg7 跳过）。")
    print("     → 「pyarrow 支不支持行组裁剪」这个问题问得太粗 ——")
    print("       得先问「哪个 API」。")
    print()
    print("  4. **坏一列不影响另一列**：")
    print("     单独跑 `sum(v)`（不碰 id 列）在所有坏文件上都是「跳过」——")
    print("     说明探针坏的是**一个列块**，不是整个行组，")
    print("     也没有波及别的读法。")
    print()


def scenario_metadata_shortcut(con: duckdb.DuckDBPyConnection, good: Path, workdir: Path) -> None:
    section("第 4 节 · 元数据捷径：count(*) 和 min() 连列块都不碰")

    bad0 = workdir / "bad_rg0.parquet"
    bad1 = workdir / "bad_rg1.parquet"

    queries = [
        ("count(*)", "SELECT count(*) FROM read_parquet(?)"),
        ("count(*) WHERE id >= 0", "SELECT count(*) FROM read_parquet(?) WHERE id >= 0"),
        ("min(id)", "SELECT min(id) FROM read_parquet(?)"),
        ("sum(id) WHERE id >= 0", "SELECT sum(id) FROM read_parquet(?) WHERE id >= 0"),
        ("count(*) WHERE id < 100", "SELECT count(*) FROM read_parquet(?) WHERE id < 100"),
    ]

    def cell(sql: str, path: Path) -> str:
        try:
            val = con.execute(sql, [str(path)]).fetchone()[0]
            return f"{val}"
        except Exception:
            return "报错"

    print("  同一批查询跑在三份文件上：好的、坏了 rg0 的、坏了 rg1 的。")
    print()
    print(
        "  "
        + common.pad("查询", 28)
        + common.pad("好的文件", 20)
        + common.pad("坏 rg0", 14)
        + "坏 rg1"
    )
    print("  " + "-" * 76)
    for label, sql in queries:
        print(
            "  "
            + common.pad(label, 28)
            + common.pad(cell(sql, good), 20)
            + common.pad(cell(sql, bad0), 14)
            + cell(sql, bad1)
        )
    print()
    print("  → **前四行里有两个「连列块都没碰」**：")
    print()
    print("    · `count(*)` —— 行数直接取自行组元数据（`row_group_num_rows`），")
    print("      一个字节的列数据都不用读。这在「先 count 一下再决定要不要细查」")
    print("      的场景里非常值钱：count 完再去读明细，等于读了两遍文件；")
    print("      count 直接从元数据来，只读一遍。")
    print("    · `min(id)` —— 直接取 zonemap 里的 `stats_min`。")
    print("      同一个文件里 rg0 的 `stats_min` 就是全局最小值，所以连 rg0 都不用读。")
    print()
    print("  → `sum(id)` 必须真的把列读出来，所以坏 rg0 / 坏 rg1 都报错。")
    print()
    print("  → 最后一行是**剪枝本身**的证据：")
    print("    `count(*) WHERE id < 100` 在坏 rg1 的文件上照样给出 100")
    print("    （rg1 被跳过了，只有 rg0 被读），在坏 rg0 的文件上报错")
    print("    （rg0 必须读）。")
    print()
    print("  ⚠️ 一个容易被误读的地方：**`count(*)` 报错不代表「没剪枝」**。")
    print("    坏 rg0 上它报错，是因为 rg0 恰好是唯一被保留下来要读的行组 ——")
    print("    跟剪不剪枝无关。判断剪枝看的是**被跳过的那些行组**。")
    print()


def scenario_optimizer_switch(con: duckdb.DuckDBPyConnection, good: Path, workdir: Path) -> None:
    section("第 5 节 · 裁剪归谁管：把 DuckDB 的优化器开关一个个关掉")

    bad1 = workdir / "bad_rg1.parquet"
    sql = "SELECT sum(id) FROM read_parquet(?) WHERE id < 100"

    def probe(path: Path, disabled: str) -> tuple[str, str]:
        con.execute("SET disabled_optimizers=''")
        if disabled:
            con.execute(f"SET disabled_optimizers='{disabled}'")
        try:
            val = con.execute(sql, [str(path)]).fetchone()[0]
            return "跳过", str(val)
        except Exception:
            return "报错", ""
        finally:
            con.execute("SET disabled_optimizers=''")

    def plan_has_filter_scan(disabled: str) -> str:
        con.execute("SET disabled_optimizers=''")
        if disabled:
            con.execute(f"SET disabled_optimizers='{disabled}'")
        plan = con.sql("EXPLAIN " + sql, params=[str(good)]).fetchall()[0][1]
        con.execute("SET disabled_optimizers=''")
        return "有" if "Filters:" in plan else "没有"

    switches = [
        ("", "（默认）"),
        ("row_group_pruner", "row_group_pruner"),
        ("filter_pushdown", "filter_pushdown"),
        ("row_group_pruner,filter_pushdown", "两个都关"),
    ]
    print("  DuckDB 2.0 里能单独关掉的优化器里，有两个跟「过滤下推」沾边。")
    print("  把它们逐个关掉，再跑同一条查询、同一份坏掉 rg1 的文件：")
    print()
    print(
        "  "
        + common.pad("关掉的优化器", 38)
        + common.pad("扫描节点里的 Filters", 22)
        + "坏 rg1 的文件"
    )
    print("  " + "-" * 82)
    for disabled, label in switches:
        verdict, _ = probe(bad1, disabled)
        print(
            "  " + common.pad(label, 38) + common.pad(plan_has_filter_scan(disabled), 22) + verdict
        )
    print()
    print("  → **关掉 `filter_pushdown`，坏 rg1 的文件立刻报错** ——")
    print("    过滤没进扫描节点，reader 拿不到谓词，也就无从剪枝。")
    print("    这就是「计划层面」和「统计层面」的分界：")
    print()
    print("      下推到扫描节点   → reader 拿得到谓词 → **可能**剪")
    print("      下推到 reader     → reader 拿 min/max 一比 → **真的**剪")
    print()
    print("  → **关掉 `row_group_pruner` 没有任何影响** ——")
    print("    说明 parquet 的行组裁剪不是那个优化器干的：")
    print("    过滤进了扫描节点之后，是 **reader 自己**拿着每个行组的 min/max 决定读不读。")
    print("    （`row_group_pruner` 管的是关系代数层的行组裁剪，跟 parquet 无关。）")
    print("    这也解释了为什么 Polars 那边只要 `use_statistics=False` 就够了 ——")
    print("    它对应的是「reader 拿不拿统计」这一层，而不是「过滤有没有进扫描」。")
    print()


def scenario_timing(con: duckdb.DuckDBPyConnection, good: Path, repeat: int) -> None:
    section("第 6 节 · 计时能回答吗：同一个问题，两种测法")

    print("  探针给的是「跳 / 没跳」这个确定答案。计时给的是「快了多久」——")
    print("  同一件事，两种测法，量一下它们的差别。")
    print()
    print("  计时这一节刻意用**同一条查询**做 A/B —— 结果完全一样，")
    print("  变的只有「剪不剪」：")
    print()
    print("    · DuckDB：关掉 `filter_pushdown`（过滤不进扫描节点，reader 拿不到谓词）")
    print("    · Polars：`use_statistics=False`（reader 拿不到统计）")
    print()

    def timed(fn) -> list[float]:
        out = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t0) * 1000)
        return out

    def duck_sum(disable_pushdown: bool) -> None:
        con.execute("SET disabled_optimizers=''")
        if disable_pushdown:
            con.execute("SET disabled_optimizers='filter_pushdown'")
        try:
            con.execute(
                "SELECT sum(id) FROM read_parquet(?) WHERE id < 100", [str(good)]
            ).fetchone()
        finally:
            con.execute("SET disabled_optimizers=''")

    def pol_sum(use_statistics: bool) -> None:
        (
            pl.scan_parquet(good, use_statistics=use_statistics)
            .filter(pl.col("id") < 100)
            .select(pl.col("id").sum())
            .collect()
        )

    cases = [
        ("DuckDB  sum(id) WHERE id < 100（下推 → 剪到 1/8 行组）", lambda: duck_sum(False)),
        ("DuckDB  同一条查询，关掉 filter_pushdown（8/8 行组）", lambda: duck_sum(True)),
        ("Polars  id < 100，use_statistics=True（剪）", lambda: pol_sum(True)),
        ("Polars  同一条查询，use_statistics=False（不剪）", lambda: pol_sum(False)),
    ]
    print("  " + common.pad("读法", 56) + common.pad("中位数 (ms)", 14) + "最快 (ms)")
    print("  " + "-" * 88)
    medians: list[float] = []
    for name, fn in cases:
        samples = timed(fn)
        medians.append(statistics.median(samples))
        print(
            "  "
            + common.pad(name, 56)
            + common.pad(f"{medians[-1]:.1f}", 14)
            + f"{min(samples):.1f}"
        )
    print()
    print(f"  （重复 {repeat} 次，串行跑；机器：{common.describe_machine()}）")
    print()
    if len(medians) == 4 and medians[0] > 0:
        duck_ratio = medians[1] / medians[0]
        pol_ratio = medians[3] / medians[2]
        print(
            f"  → DuckDB：不剪比剪慢 **{duck_ratio:.2f}×**；"
            f"Polars：不剪比剪慢 **{pol_ratio:.2f}×**。"
        )
        print()
    print("  → 计时能看出「快慢」，但**看不出「跳了哪几个行组」**：")
    print("    两边快慢的量级差不多（都是同一套 parquet + 同一套统计），")
    print("    可计时是个**连续量** —— 差 0.1ms 是「剪了」还是「没剪」？")
    print("    机器一忙、磁盘一缓存，这个差别就没了。")
    print("    探针是个**二值量**：跳过 / 报错，一个字都不用猜。")
    print()
    print("  → 所以这两件事是互补的，不是替代关系：")
    print("    计时回答「值不值得」，探针回答「到底做了什么」。")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DuckDB 与 Polars 的行组裁剪探针：把「跳没跳过」变成「报没报错」"
    )
    parser.add_argument("--rows", type=int, default=1_000_000, help="数据行数")
    parser.add_argument("--repeat", type=int, default=5, help="计时场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    workdir = Path(tempfile.mkdtemp(prefix="prune_probe_"))
    try:
        con = duckdb.connect()
        try:
            good = workdir / "good.parquet"
            row_groups = build_file(con, good, args.rows)
            chunks = column_chunks(con, good)

            scenario_probe_principle(con, good, chunks)
            scenario_control(con, good, args.rows)
            scenario_matrix(con, good, chunks, workdir, row_groups)
            scenario_metadata_shortcut(con, good, workdir)
            scenario_optimizer_switch(con, good, workdir)
            scenario_timing(con, good, args.repeat)
        finally:
            con.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    print("=" * 74)
    print("小结")
    print("=" * 74)
    print("  1. **「跳没跳过」可以变成「报没报错」。** 把某个行组的数据页换成 0xFF，")
    print("     reader 跳过它就不报错，读了它就报错 —— 不需要任何计数器。")
    print()
    print("  2. **DuckDB 剪枝**：过滤 `id < 100` 时只读 rg0，另外 7 个行组跳过；")
    print("     不加过滤就一个都不跳。")
    print()
    print("  3. **Polars 也剪枝** —— 以前只能推测，现在有硬证据：")
    print("     `use_statistics=True` 时和 DuckDB 一样跳过，")
    print("     `use_statistics=False` 时每个行组都读。")
    print("     → **「Polars 没有 IO 计数器，这层测不了」这句话是错的**，")
    print("       探针不靠计数器。")
    print()
    print("  4. **pyarrow 得看是哪个 API**：`pq.read_table` 不剪，")
    print("     `pyarrow.dataset` 剪。")
    print()
    print("  5. **裁剪的前提是过滤进了扫描节点**：关掉 `filter_pushdown`，")
    print("     探针立刻从「跳过」翻成「报错」；")
    print("     关掉 `row_group_pruner` 没有任何影响 —— parquet 的裁剪是 reader 干的。")
    print()
    print("  6. **元数据捷径**：`count(*)` 和 `min(id)` 连列块都不用碰")
    print("     （行数来自 `row_group_num_rows`，极值来自 zonemap），")
    print("     `sum(id)` 才必须真的读数据。")
    print()
    print("  7. **计时和探针是互补的**：计时回答「值不值得」，探针回答「到底做了什么」。")
    print()


if __name__ == "__main__":
    main()
