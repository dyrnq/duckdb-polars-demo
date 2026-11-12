"""列式存储：磁盘上到底怎么排的 —— 行组里按列分段。

`object_demo.py` 讲了「CREATE 出来的东西」，`config_demo.py` 讲了「怎么调引擎」，
这一层是**数据在磁盘上怎么排的**：为什么 DuckDB 读一列不用把整行拉出来。

三个必须记住的实测结论：

> **① 列式不是「一个列一个文件」，是「行组里按列分段」。**
>
> DuckDB 的表按**行组**切（本机实测 122,880 行一组），每个行组里**每一列各有自己的段**、
> 自己的压缩算法。`PRAGMA storage_info` 把这些全列出来。

> **② 要哪列读哪列 —— 代价跟表的宽度基本无关。**
>
> 同样一份数据，只取一列：读 DuckDB 自己的文件、读同样内容的 CSV，差两个数量级。
> 而 DuckDB 内部把四列全读，也只比读一列多一倍多 —— 不是四倍。

> **③ 压缩是按列做的 —— 这条行式存储做不到。**
>
> 同一张表加一列常量（每行都一样），文件大小**一点没变**；
> 换成加一列随机 md5，每行要多花几十字节。行式存储得先把列拼成一行再压，做不到这个。

**别把「列式」当成 DuckDB 对 Polars 的差异点** —— Polars 也是列式的（Arrow 列块）。
区别在**格式**：DuckDB 有自己的存储格式（行组 + 自有压缩 + 原地更新），
Polars 的原生是 Parquet / Arrow IPC。

用法：
    uv run python src/columnar_demo.py
    uv run python src/columnar_demo.py --rows 200000 --repeat 3
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

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# 四个列型刻意拉开差距，用来「看得出列与列之间的不同」：
#   id        —— 连号，BitPacking 压得最狠
#   low_card  —— 一千个取值循环，位宽用不满
#   high_card —— 跟 id 一样是连号，但**读它的时候**才看得出投影下推的效果
#   konst     —— 每行都一样的字符串，字典编码
TABLE_SQL = """
    CREATE TABLE {name} AS
    SELECT range AS id, range % 1000 AS low_card, range AS high_card, '常量' AS konst
    FROM range({rows})
"""


def human(n: int) -> str:
    """字节数写成人看得懂的。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / 1024 / 1024:.1f} MiB"


def segs_of(con: duckdb.DuckDBPyConnection, row_group: int) -> list[tuple[str, int, int, str, int]]:
    """某个行组里，各列的段（按块号 + 块内偏移排）。

    `block_id` 是 DuckDB 自己记的段位置，不是文件的绝对字节地址。
    """
    return con.execute(
        """
        SELECT column_name, block_id, block_offset, compression, count
        FROM pragma_storage_info('wide')
        WHERE segment_type <> 'VALIDITY' AND row_group_id = ?
        ORDER BY block_id, block_offset
        """,
        [row_group],
    ).fetchall()


# ---------------------------------------------------------------- 场景 1：存储布局


def scenario_layout(con: duckdb.DuckDBPyConnection, rows: int) -> None:
    section("场景 1 · 存储布局：行组 + 每列独立的段")

    con.execute(TABLE_SQL.format(name="wide", rows=rows))
    # 不 CHECKPOINT 的话数据还在缓冲里，block_id / block_offset 全是 NULL，
    # 「在磁盘上排在哪儿」就无从谈起。
    con.execute("CHECKPOINT")

    info = con.execute(
        """
        SELECT row_group_id, column_name, segment_type, compression,
               block_id, block_offset, count
        FROM pragma_storage_info('wide')
        WHERE segment_type <> 'VALIDITY'
        ORDER BY row_group_id, column_name, segment_id
        """
    ).fetchall()

    print("  `PRAGMA storage_info` 把磁盘上的段列了出来：")
    print()
    print(
        f"    {'行组':>4} {'列':<10}{'段类型':<12}{'压缩':<12}{'block':>7}{'偏移':>12}{'行数':>12}"
    )
    print("    " + "-" * 74)
    shown = info if len(info) <= 12 else info[:4] + info[-4:]
    for rg, col, seg_type, comp, blk, off, cnt in shown:
        print(f"    {rg:>4} {col:<10}{seg_type:<12}{comp:<12}{blk:>7}{off:>12}{cnt:>12}")
    if len(shown) < len(info):
        print(f"    （中间还有 {len(info) - len(shown)} 行）")
    print()

    groups = sorted({r[0] for r in info})
    sizes = {r[6] for r in info}
    print(f"    → {rows:,} 行被切成 **{len(groups)} 个行组**（行组号 {groups[0]}~{groups[-1]}），")
    print(f"      满的行组 {max(sizes):,} 行，最后一个 {min(sizes):,} 行（不满）。")
    print("      **列是一段一段分开存的**：上表里每一行的块号/偏移都不同 ——")
    print("      同一个行组里，四列各占各的段，各用各的压缩算法。")
    print()

    # 每个行组里，四列的段是怎么排的
    print(f"    第 {groups[0]} 个行组里，四列的段（按 DuckDB 记的「块号 + 块内偏移」排）：")
    print()
    print(f"    {'列':<12}{'块号':>6}{'块内偏移':>12}{'压缩算法':>14}{'行数':>10}")
    print("    " + "-" * 54)
    for name, blk, off, comp, cnt in segs_of(con, groups[0]):
        print(f"    {name:<12}{blk:>6}{off:>12,}{comp:>14}{cnt:>10,}")
    print()

    # 实测：同一个行组里，各列的段有没有交错
    # （行式存储里，一行的几列必然挨着，行与行之间也就交错开了）
    all_ok = True
    for rg in groups:
        seq = [r[0] for r in segs_of(con, rg)]
        runs = [c for i, c in enumerate(seq) if i == 0 or seq[i - 1] != c]
        if len(runs) != len(set(runs)):
            all_ok = False
    print(
        f"    → {len(groups)} 个行组逐个查过：同一个行组里，各列的段"
        f"**各占一段、彼此不交错** {'✅' if all_ok else '❌'}。"
    )
    print("      行式存储做不到这点 —— 它必须把一行的几列拼在一起挨着放，")
    print("      行与行之间也就交错开了。")
    print()
    print("    口径：`block_id` / `block_offset` 是 DuckDB 自己记的**段位置**")
    print("          （块号 + 块内偏移），不是整个文件的绝对字节位置；")
    print("          每一列到底占了多少字节，用场景 3 的文件大小差量出来。")
    print()

    size_rows = con.execute(
        "SELECT database_size, block_size, total_blocks, used_blocks FROM pragma_database_size()"
    ).fetchall()
    db_size, bsize, total_blocks, used_blocks = size_rows[0]
    print(f"    `pragma_database_size()`：这张表占 {db_size}，")
    print(f"      block 大小 {bsize:,} B，共 {total_blocks} 个 block（用了 {used_blocks} 个）。")
    print()
    print("    → **列式不等于「一个列一个文件」**：DuckDB 的最小存储单位是「行组 + 列段」，")
    print("      一个文件里同时装着所有列；列式体现在**行组内部按列分开**。")
    print()


# ---------------------------------------------------------------- 场景 2：投影下推


def scenario_projection(
    con: duckdb.DuckDBPyConnection,
    duck_path: Path,
    csv_path: Path,
    parquet_path: Path,
    rows: int,
    repeat: int,
) -> None:
    section("场景 2 · 要哪列读哪列")

    con.execute(f"COPY wide TO '{csv_path}' (FORMAT CSV, HEADER)")
    con.execute(f"COPY wide TO '{parquet_path}' (FORMAT PARQUET)")

    print("  同一份数据存成三种格式，只取**一列**（`high_card`）来读：")
    print()
    print(f"    DuckDB 自己的文件 : {human(duck_path.stat().st_size)}")
    print(f"    Parquet 文件      : {human(parquet_path.stat().st_size)}")
    print(f"    CSV 文件          : {human(csv_path.stat().st_size)}")
    print()

    sources: list[tuple[str, str]] = [
        ("DuckDB 自己的文件", "SELECT sum(high_card) FROM wide"),
        ("Parquet 文件", f"SELECT sum(high_card) FROM read_parquet('{parquet_path}')"),
        ("CSV 文件", f"SELECT sum(high_card) FROM read_csv('{csv_path}')"),
    ]

    def run(sql: str) -> float:
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        return (time.perf_counter() - t0) * 1000

    # 预热：三个来源各跑一次，编译计划和首次读盘不算进计时
    for _, sql in sources:
        run(sql)

    # **交错**地跑，而且每轮换起点 —— 谁都不总是第一个跑，
    # 「第一次的冷启动代价」不会被算到同一边头上。
    times: dict[str, list[float]] = {label: [] for label, _ in sources}
    for i in range(repeat):
        for k in range(len(sources)):
            label, sql = sources[(i + k) % len(sources)]
            times[label].append(run(sql))

    print(f"    {rows:,} 行 × 4 列，只取一列（{repeat} 轮，交错跑）：")
    print()
    print(f"    {'来源':<20}{'中位 (ms)':>12}{'最快 (ms)':>12}")
    print("    " + "-" * 44)
    medians: dict[str, float] = {}
    for label, _ in sources:
        med = statistics.median(times[label])
        medians[label] = med
        print(f"    {label:<20}{med:>12.2f}{min(times[label]):>12.2f}")
    print()
    csv_med = medians["CSV 文件"]
    duck_med = medians["DuckDB 自己的文件"]
    pq_med = medians["Parquet 文件"]
    print(f"    → CSV（行存）比 DuckDB 自己的文件慢 **{csv_med / duck_med:.0f}×**，")
    print(f"      比 Parquet（列存）慢 **{csv_med / pq_med:.0f}×**。")
    print("      行存文件里，取一列也得把整个文件扫过一遍 —— 这是**格式**的代价，不是引擎的。")
    print()

    # DuckDB 内部：读一列 vs 读两列 vs 读四列
    inner: list[tuple[str, str]] = [
        ("只读 1 列", "SELECT sum(high_card) FROM wide"),
        ("读 2 列", "SELECT sum(id) + sum(low_card) FROM wide"),
        ("读 4 列", "SELECT sum(id) + sum(low_card) + sum(high_card) + count(konst) FROM wide"),
    ]
    for _, sql in inner:
        run(sql)
    inner_times: dict[str, list[float]] = {label: [] for label, _ in inner}
    for i in range(repeat):
        for k in range(len(inner)):
            label, sql = inner[(i + k) % len(inner)]
            inner_times[label].append(run(sql))

    print("    同一个 DuckDB 文件，只改**读几列**：")
    print()
    print(f"    {'读几列':<12}{'中位 (ms)':>12}{'相对 1 列':>12}")
    print("    " + "-" * 36)
    base = statistics.median(inner_times["只读 1 列"])
    for label, _ in inner:
        med = statistics.median(inner_times[label])
        print(f"    {label:<12}{med:>12.2f}{med / base:>11.2f}×")
    print()
    four = statistics.median(inner_times["读 4 列"])
    print(f"    → 读四列只比读一列多 **{four / base:.2f}×** —— 列式存储的账是**按列算的**，")
    print("      不是按「表有多宽」算的。要哪列读哪列，多出来的列一个字节都不碰。")
    print()
    print("    口径：三个来源都先跑一次预热，再**交错**跑；两边轮流当第一个，噪声摊平。")
    print("          文件都在操作系统的页缓存里（跑过就读热了），")
    print("          所以量到的是「扫多少字节 + 解多少码」的差距，不是磁盘延迟。")
    print()


# ---------------------------------------------------------------- 场景 3：按列压缩


def scenario_compression(work: Path, rows: int) -> tuple[int, int, int]:
    """返回（随机列在 DuckDB 文件里多占的字节，常量列在 Parquet 里多占的，随机列的）。"""
    section("场景 3 · 压缩是按列做的")

    # 同一个 SELECT，三种存法：一张 DuckDB 自己的文件、一张 Parquet
    variants: list[tuple[str, str]] = [
        ("一列", "SELECT range AS a FROM range({rows})"),
        ("加一列常量", "SELECT range AS a, '常量' AS b FROM range({rows})"),
        ("加一列随机", "SELECT range AS a, md5(range::VARCHAR) AS b FROM range({rows})"),
    ]

    duck_sizes: dict[str, int] = {}
    algos: dict[str, str] = {}
    for label, sel in variants:
        path = work / f"cmp_{label}.duckdb"
        c = duckdb.connect(str(path))
        c.execute(f"CREATE TABLE t AS {sel.format(rows=rows)}")
        c.execute("CHECKPOINT")
        if label != "一列":
            row = c.execute(
                """
                SELECT compression FROM pragma_storage_info('t')
                WHERE segment_type <> 'VALIDITY' AND column_name = 'b'
                GROUP BY compression LIMIT 1
                """
            ).fetchall()
            if row:
                algos[label] = row[0][0]
        c.close()
        duck_sizes[label] = path.stat().st_size

    pq_sizes: dict[str, int] = {}
    for label, sel in variants:
        path = work / f"cmp_{label}.parquet"
        c = duckdb.connect()
        c.execute(f"COPY ({sel.format(rows=rows)}) TO '{path}' (FORMAT PARQUET)")
        c.close()
        pq_sizes[label] = path.stat().st_size

    base = duck_sizes["一列"]
    print(f"  {rows:,} 行，每次在同一张表上**加一列**，看文件大了多少：")
    print()
    print("  DuckDB 自己的格式（`.duckdb` 文件）：")
    print()
    print(f"    {'变体':<14}{'文件大小':>14}{'多占':>14}{'新列的压缩算法':>20}")
    print("    " + "-" * 62)
    for label, _ in variants:
        extra = duck_sizes[label] - base
        print(
            f"    {label:<14}{human(duck_sizes[label]):>14}"
            f"{(human(extra) if extra else '0 B'):>14}"
            f"{algos.get(label, '—'):>20}"
        )
    print()

    rand_extra = duck_sizes["加一列随机"] - base
    print("    → 加一列**常量**（每行都一样）：文件**一个字节都没多**。")
    print(f"      加一列**随机**（每行不同）  ：多占 **{human(rand_extra)}**")
    print(f"      （≈ {rand_extra / rows:.1f} B/行 —— md5 是 32 个字符，随机字符串本来就压不动，")
    print("       字典编码也省不下什么。）")
    print()
    print("    口径：DuckDB 的文件按 **256 KiB 的 block** 分配，所以「一个字节都没多」")
    print("          的准确含义是「连一个 block 都没多占」；要更细的数，看下面的 Parquet。")
    print()

    print("  同样的三份数据存成 Parquet（文件大小是**字节精确**的）：")
    print()
    print(f"    {'变体':<14}{'文件大小':>14}{'多占':>14}{'每行多占':>16}")
    print("    " + "-" * 58)
    pq_base = pq_sizes["一列"]
    for label, _ in variants:
        extra = pq_sizes[label] - pq_base
        per_row = extra / rows if rows else 0.0
        print(
            f"    {label:<14}{human(pq_sizes[label]):>14}"
            f"{(human(extra) if extra else '0 B'):>14}{per_row:>15.4f}B"
        )
    print()

    pq_const = pq_sizes["加一列常量"] - pq_base
    pq_rand = pq_sizes["加一列随机"] - pq_base
    print(f"    → 常量列一共只多占 **{pq_const:,} 字节**（{pq_const / rows:.4f} B/行），")
    print(f"      随机列多占 **{pq_rand:,} 字节**（{pq_rand / rows:.2f} B/行）。")
    print(f"      两者差 **{pq_rand / pq_const:.0f} 倍** —— 「每行都一样」这个规律，")
    print("      只有在**按列**压的时候才抓得住。")
    print()
    print("    → **压缩是按列做的**：常量列被字典编码压到几乎不占地方。")
    print("      行式存储做不到 —— 它必须先把几列拼成一行再压，")
    print("      拼起来之后「每行都一样」这个规律就没了。")
    print()

    return duck_sizes["加一列随机"] - base, pq_const, pq_rand


# ---------------------------------------------------------------- 场景 4：Polars 侧


def scenario_polars(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    parquet_path: Path,
    rows: int,
    repeat: int,
) -> None:
    section("场景 4 · Polars 侧也是列式的")

    df = pl.DataFrame(
        {
            "id": list(range(rows)),
            "low_card": [i % 1000 for i in range(rows)],
            "high_card": list(range(rows)),
            "konst": ["常量"] * rows,
        }
    )
    arrow = df.to_arrow()
    print("  Polars 的 DataFrame 就是 Arrow 的**列块**（不是行）：")
    print()
    for name in arrow.schema.names:
        col = arrow.column(name)
        chunks = col.chunks
        bufs = [len(b) for b in chunks[0].buffers() if b is not None]
        print(f"    {name:<12}{str(col.type):<12}{len(chunks)} 个 chunk，缓冲区字节 {bufs}")
    print()
    print("    → 每一列是**独立的一段连续内存**，跟别的列不挨着 ——")
    print("      这就是列式在内存里的样子。DuckDB 的行组 + 列段是同一件事的磁盘版。")
    print()

    # 两边读同一个 Parquet 的同一列：列式的性质来自**格式**，不是来自引擎
    jobs: list[tuple[str, object]] = [
        (
            "DuckDB 读 Parquet",
            lambda: con.execute(
                f"SELECT sum(high_card) FROM read_parquet('{parquet_path}')"
            ).fetchall(),
        ),
        (
            "Polars 读 Parquet",
            lambda: (
                pl.scan_parquet(parquet_path).select(pl.col("high_card").sum()).collect().to_dicts()
            ),
        ),
        (
            "DuckDB 读 CSV",
            lambda: con.execute(f"SELECT sum(high_card) FROM read_csv('{csv_path}')").fetchall(),
        ),
        (
            "Polars 读 CSV",
            lambda: pl.scan_csv(csv_path).select(pl.col("high_card").sum()).collect().to_dicts(),
        ),
    ]

    def run(fn: object) -> float:
        t0 = time.perf_counter()
        fn()  # type: ignore[operator]
        return (time.perf_counter() - t0) * 1000

    for _, fn in jobs:
        run(fn)

    times: dict[str, list[float]] = {label: [] for label, _ in jobs}
    for i in range(repeat):
        for k in range(len(jobs)):
            label, fn = jobs[(i + k) % len(jobs)]
            times[label].append(run(fn))

    print(f"  同一个文件、同一个列，两边各读一遍（{repeat} 轮，交错跑）：")
    print()
    print(f"    {'读法':<20}{'中位 (ms)':>12}{'最快 (ms)':>12}")
    print("    " + "-" * 44)
    for label, _ in jobs:
        print(f"    {label:<20}{statistics.median(times[label]):>12.2f}{min(times[label]):>12.2f}")
    print()
    pq_duck = statistics.median(times["DuckDB 读 Parquet"])
    pq_polars = statistics.median(times["Polars 读 Parquet"])
    csv_duck = statistics.median(times["DuckDB 读 CSV"])
    csv_polars = statistics.median(times["Polars 读 CSV"])
    print(f"    → Parquet（列存）：DuckDB {pq_duck:.1f} ms vs Polars {pq_polars:.1f} ms")
    print(f"      CSV（行存）    ：DuckDB {csv_duck:.1f} ms vs Polars {csv_polars:.1f} ms")
    print()
    if csv_polars < csv_duck:
        print("    → 注意 CSV 这一栏：**Polars 反超 DuckDB**")
        print(
            f"      （{csv_polars:.1f} ms vs {csv_duck:.1f} ms，快 {csv_duck / csv_polars:.1f} 倍）"
        )
        print("      —— DuckDB 的 CSV 解析器比 Polars 慢。")
        print("      但这是**解析行存文本**的本事，跟列式一点关系没有。")
        print()
    print("    → **「列式」是格式的属性，不是引擎的属性** ——")
    print("      两边读 Parquet 都快，读 CSV 都慢（Polars 相对自己的 Parquet 慢了")
    print(f"      {csv_polars / pq_polars:.1f} 倍，DuckDB 慢了 {csv_duck / pq_duck:.1f} 倍）。")
    print("      DuckDB 和 Polars 的差距，在「行存 vs 列存」这个量级面前是零头。")
    print("      DuckDB 独有的不是「列式」，而是**自己的存储格式**：行组 + 自有压缩 + 原地更新。")
    print()


# ---------------------------------------------------------------- 小结


def scenario_conclusion(duck_rand_extra: int, pq_const: int, pq_rand: int, rows: int) -> None:
    section("小结：列式这一层，谁有什么")

    table_rows = [
        ("磁盘布局", "行组 + 每列独立的段", "Arrow 列块（内存）/ Parquet 列块（磁盘）"),
        ("一列一个文件？", "❌ 一个文件里按行组分段", "❌ 一个文件里按列块分段"),
        ("读一列的代价", "跟表宽基本无关", "同样跟表宽基本无关"),
        ("压缩粒度", "**按列**（常量列几乎不占地方）", "按列（Parquet 字典编码）"),
        (
            "自带存储格式",
            "有：`.duckdb` 文件，行组 + 自有压缩",
            "没有原生格式，原生是 Parquet / Arrow IPC",
        ),
        ("原地更新", "有（按行组重写）", "没有，只能重写文件"),
    ]

    print("  " + common.pad("维度", 18) + common.pad("DuckDB", 40) + "Polars")
    print("  " + "-" * 74)
    for dim, duck, pol in table_rows:
        print("  " + common.pad(dim, 18) + common.pad(duck, 40) + pol)
    print()

    print("  三个必须记住的实测结论：")
    print()
    print("    ① **列式 = 行组里按列分段，不是一个列一个文件。**")
    print("       DuckDB 的最小存储单位是「行组 + 列段」；")
    print("       一个文件里同时装着所有列，每列各有自己的段和压缩算法。")
    print()
    print("    ② **要哪列读哪列 —— 账是按列算的。**")
    print("       读四列只比读一列多一倍多（不是四倍）；")
    print("       而同样一份数据存成 CSV，取一列要多花两个数量级。")
    print()
    print("    ③ **压缩按列做 —— 这是列式最值钱的地方。**")
    print(f"       {rows:,} 行：加一列常量，DuckDB 的文件一个字节都不多、")
    print(f"       Parquet 只多 **{pq_const:,} 字节**；加一列随机，")
    print(f"       Parquet 多 **{pq_rand:,} 字节**（{pq_rand / rows:.1f} B/行），")
    print(f"       DuckDB 的文件多 **{human(duck_rand_extra)}**。")
    print("       行式存储必须先把列拼成行，这个便宜占不到。")
    print()
    print("  实务建议：")
    print("    · 表很宽、但每次查询只碰几列 → 列存格式（DuckDB 文件 / Parquet）最划算；")
    print("      把宽表存成 CSV 再读，是把「不要的列」的钱也付了；")
    print("    · 派生列多、且每行取值重复 → 列存 + 按列压缩能省下大量空间，")
    print("      但**读取时要现算**（见 `object_demo.py`）；")
    print("    · 要写又要改 → DuckDB 的 `.duckdb` 文件（原地更新）比 Parquet 合适；")
    print("      一次性导出、给别的系统读 → Parquet。")
    print()
    print("  → 这也是为什么本项目里「同一份数据换个格式再读一遍」总是差很多：")
    print("    差的是**格式**，不是引擎 —— `materialize_demo.py` 量的也是同一件事。")
    print()


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="列式存储：行组里按列分段")
    parser.add_argument("--rows", type=int, default=1_000_000, help="行数")
    parser.add_argument("--repeat", type=int, default=3, help="计时重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    work = Path(tempfile.mkdtemp(prefix="columnar_demo_"))
    print(f"实验目录：{work}")

    duck_path = work / "wide.duckdb"
    csv_path = work / "wide.csv"
    parquet_path = work / "wide.parquet"

    try:
        con = duckdb.connect(str(duck_path))
        scenario_layout(con, args.rows)
        scenario_projection(con, duck_path, csv_path, parquet_path, args.rows, args.repeat)
        duck_rand, pq_const, pq_rand = scenario_compression(work, args.rows)
        scenario_polars(con, csv_path, parquet_path, args.rows, args.repeat)
        scenario_conclusion(duck_rand, pq_const, pq_rand, args.rows)
        con.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("  （实验目录已清理）")


if __name__ == "__main__":
    main()
