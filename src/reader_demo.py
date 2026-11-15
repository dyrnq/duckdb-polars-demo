"""读 parquet 到底谁快 —— 四个 reader 的一份对照，**口径决定一切**。

前面的 demo 比的都是「引擎」：同样的 SQL，DuckDB 和 Polars 谁快。
这一层往下一格：**同一个 parquet 文件，用不同的 reader 读，能差多少？**

本机实测（8 核，100 万行 × 10 列，ZSTD，1 个行组）：

    reader         冷进程      热进程(2)   热进程(3)
    carquet          65.6         50.4        44.6
    Polars           76.9         48.8        48.9
    pyarrow          99.0         35.3        36.7
    DuckDB→Arrow    115.2         97.8        97.7

三个必须记住的实测结论：

> **① 口径不对，数字能差 40 多倍。**
>
> 同一个文件、同一份数据，最快的一次 34 ms、最慢的一次 1543 ms（`fetchall()`）。
> 差在哪：冷进程还是热进程、读整表还是读一列、DuckDB 用哪个物化端点。
> **报一个「读 parquet 花了 X ms」而不说口径，等于没报。**

> **② 「读 parquet 慢」经常是物化慢，不是读慢。**
>
> DuckDB 的 parquet 扫描恒定 52 ms（线程数从 1 加到 8 一点没变 ——
> 它的扫描是**按行组并行**的，这份文件只有 1 个行组，用不上），
> 后面还串着「收集结果 92 ms + 转 Arrow 10 ms」。
> 它的 reader 不慢，**它的产物管道长**。

> **③ 没有永远的赢家 —— 行组数决定谁能用上并行。**
>
> 1 个行组的文件：carquet 最快，Polars 紧随其后。
> 拆成 8 个行组：Polars 用上了行组级并行（快近 3 倍），DuckDB 也跟上，
> 而单线程 SIMD 的 carquet 因为文件变大了反而变慢。
> **选 reader 之前先看你的文件有几个行组。**

carquet 是**可选依赖**（第三方纯 C 的 Parquet 库，MIT）：

    uv sync --group demo      # 装上它
    uv run python src/reader_demo.py

没装就跳过它那一行，其余照跑 —— 跟 `fts_demo.py` 装不装扩展一个道理。
CI 跑的是默认依赖（没装 carquet），所以 CI 的输出里只有三个 reader。

用法：
    uv run python src/reader_demo.py
    uv run python src/reader_demo.py --repeat 5
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

import common

# ---------------------------------------------------------------- reader 清单
#
# 每个 reader 读同一个文件、产出「一张物化好的内存表」。
# 端点不统一是事实：pyarrow 产 Arrow Table，Polars 产 DataFrame，
# DuckDB 产自己的结果集（这里取它的 Arrow 导出），
# carquet 产自己的 Table（可零拷贝交给 polars / pyarrow / DuckDB）。

READERS = [
    ("DuckDB", "duckdb"),
    ("Polars", "polars"),
    ("pyarrow", "pyarrow"),
    ("carquet", "carquet"),  # 可选，缺了自动跳过
]

# DuckDB 的几个物化端点 —— 同一份数据，物化成不同的东西，代价差一个数量级
DUCKDB_ENDPOINTS = [
    (".arrow()（惰性流，数据还没解码）", "arrow_lazy"),
    ("sql().to_arrow_table()", "sql_arrow"),
    ("execute().to_arrow_table()", "execute_arrow"),
    (".pl()（交 polars）", "pl"),
    ("fetchall()（python 元组）", "fetchall"),
]

# DuckDB 对 execute() 这条路会喊一句「会多拷一份，建议用 sql()」——
# 但本机实测它反而更快（见场景 3）。这条警告会污染 demo 的输出，压掉它。
warnings.filterwarnings("ignore", message=".*Arrow output on a DB-API result.*")

# DuckDB 的连接复用同一个：连接初始化不是「读文件」的成本，
# 它单独在场景 4 里量。
_CON: duckdb.DuckDBPyConnection | None = None


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def has_carquet() -> bool:
    return importlib.util.find_spec("carquet") is not None


def reader_version(label: str) -> str:
    if label == "DuckDB":
        return duckdb.__version__
    if label == "Polars":
        return pl.__version__
    if label == "pyarrow":
        return pa.__version__
    import carquet

    return getattr(carquet, "__version__", "?")


# ---------------------------------------------------------------- 读


def prepare(reader: str) -> None:
    """在计时**之外**把 reader 准备好（目前只有 DuckDB 需要建连接）。"""
    global _CON
    if reader == "duckdb" and _CON is None:
        _CON = duckdb.connect()


def read_once(reader: str, path: str, mode: str = "all") -> object:
    """用指定 reader 读一次，产出物化好的内存表。

    mode="all" 读全部列；mode="one" 只读 region 一列（投影下推）。
    """
    sql_all = f"SELECT * FROM read_parquet('{path}')"
    sql_one = f"SELECT region FROM read_parquet('{path}')"

    if reader == "duckdb":
        assert _CON is not None
        return _CON.execute(sql_all if mode == "all" else sql_one).to_arrow_table()
    if reader == "polars":
        if mode == "one":
            return pl.read_parquet(path, columns=["region"])
        return pl.read_parquet(path)
    if reader == "pyarrow":
        if mode == "one":
            return pq.read_table(path, columns=["region"])
        return pq.read_table(path)
    if reader == "carquet":
        import carquet as cq

        if mode == "one":
            return cq.read_table(path, columns=["region"])
        return cq.read_table(path)
    raise ValueError(f"unknown reader {reader}")


def to_polars(obj: object) -> pl.DataFrame:
    """把各 reader 的产物统一成 polars DataFrame，才能逐列对账。"""
    if isinstance(obj, pl.DataFrame):
        return obj
    if isinstance(obj, pa.Table):
        return pl.from_arrow(obj)
    import carquet as cq

    if isinstance(obj, cq.Table):
        return pl.DataFrame(obj)
    raise TypeError(f"unexpected type {type(obj)}")


def same_data(a: pl.DataFrame, b: pl.DataFrame) -> bool:
    """逐列比对两个 reader 的读结果。"""
    if a.columns != b.columns or a.height != b.height:
        return False
    for c in a.columns:
        x, y = a[c], b[c]
        if x.dtype in (pl.String, pl.Boolean, pl.Int32, pl.Int64, pl.Datetime, pl.Decimal):
            if x.to_list() != y.to_list():
                return False
        elif abs(float(x.sum()) - float(y.sum())) > 1e-6:
            return False
    return True


# ---------------------------------------------------------------- 冷进程测量
#
# 为什么必须另起进程：同一个进程里重复读同一个文件，各 reader 都会吃到自己的
# 内部缓存 —— pyarrow 实测首读 98 ms、第二次 36 ms，差的不是 IO，是缓存命中。
# 「一进程一读」测的才是「这个程序第一次打开这个文件要多久」。


def run_cold(reader: str, path: Path, mode: str = "all", repeat: int = 3) -> list[float]:
    """每次都起一个新进程读一次，返回每次的毫秒数。"""
    times = []
    for _ in range(repeat):
        out = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--once", reader, str(path), mode],
            capture_output=True,
            text=True,
            check=True,
        )
        times.append(json.loads(out.stdout.strip().splitlines()[-1])["ms"])
    return times


def run_cold_once_here(reader: str, path: str, mode: str = "all") -> None:
    """--once 模式：读一次，报时间，退出。"""
    prepare(reader)  # 建连接不算「读」，排除在计时之外
    t0 = time.perf_counter()
    obj = read_once(reader, path, mode)
    ms = (time.perf_counter() - t0) * 1000
    rows = obj.num_rows if hasattr(obj, "num_rows") else obj.height
    print(json.dumps({"ms": ms, "rows": rows}))


# ---------------------------------------------------------------- 表


def print_table(header: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> None:
    aligns = aligns or ["<"] * len(header)
    widths = [
        max(common.display_width(str(r[i])) for r in [header, *rows]) for i in range(len(header))
    ]
    line = "  ".join(
        common.pad(h, widths[i]) if aligns[i] == "<" else common.pad_left(h, widths[i])
        for i, h in enumerate(header)
    )
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(
            "  ".join(
                common.pad(str(c), widths[i])
                if aligns[i] == "<"
                else common.pad_left(str(c), widths[i])
                for i, c in enumerate(r)
            )
        )


def median(xs: list[float]) -> float:
    return statistics.median(xs)


def ab_arrow_channels(path: Path, repeat: int = 3) -> tuple[float, float]:
    """交错测 DuckDB 的两条 Arrow 通道：execute() vs sql()。

    交错着跑（两条路轮流当第一个），免得「谁先跑谁占便宜」。
    返回 (execute 中位, sql 中位)，单位 ms。
    """
    sql = f"SELECT * FROM read_parquet('{path}')"
    prepare("duckdb")
    assert _CON is not None
    con = _CON

    def via_execute() -> object:
        return con.execute(sql).to_arrow_table()

    def via_relation() -> object:
        return con.sql(sql).to_arrow_table()

    via_execute()
    via_relation()
    a: list[float] = []
    b: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        via_execute()
        a.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        via_relation()
        b.append((time.perf_counter() - t0) * 1000)
    return median(a), median(b)


# ---------------------------------------------------------------- 场景 1


def scenario_readers(path: Path, repeat: int) -> None:
    section("1. 四个 reader 读同一份数据")
    md = pq.ParquetFile(path).metadata
    print(f"文件：{path}")
    print(
        f"  {md.num_rows:,} 行 × {md.num_columns} 列，"
        f"{md.num_row_groups} 个行组，"
        f"{path.stat().st_size / 1024 / 1024:.1f} MB"
    )
    rg = md.row_group(0)
    codecs = sorted({rg.column(i).compression for i in range(rg.num_columns)})
    print(f"  压缩：{', '.join(codecs)}")

    ref = pl.from_arrow(pq.read_table(path))  # 对账的基准

    rows: list[list[str]] = []
    for label, reader in READERS:
        if reader == "carquet" and not has_carquet():
            rows.append([label, "—", "—", "—", "未安装，跳过"])
            continue

        cold = run_cold(reader, path, "all", repeat)
        warm = []
        for _ in range(3):
            prepare(reader)
            t0 = time.perf_counter()
            obj = read_once(reader, str(path), "all")
            warm.append((time.perf_counter() - t0) * 1000)
            del obj

        same = "✅" if same_data(ref, to_polars(read_once(reader, str(path), "all"))) else "❌"
        rows.append([label, f"{median(cold):.1f}", f"{warm[1]:.1f}", f"{warm[2]:.1f}", same])

    print_table(
        ["reader", "冷进程(中位)", "热进程第2次", "热进程第3次", "对账"],
        rows,
        ["<", ">", ">", ">", "<"],
    )
    print()
    print("  口径：")
    print("    · 冷进程 = 每次测量起一个新进程、只读一次（避免缓存命中），取中位数")
    print("    · 热进程 = 同一个进程里连读三次，这里列第 2、3 次")
    print("    · 端点 = 各自「物化好的内存表」（DuckDB 取 Arrow 导出）")
    print("    · 对账 = 跟 pyarrow 读出来的逐列比（数值列比和，字符串/时间戳列逐值比）")


# ---------------------------------------------------------------- 场景 2


def scenario_row_groups(path: Path) -> None:
    section("2. 行组数决定谁能用上并行")
    md = pq.ParquetFile(path).metadata
    src_rgs = md.num_row_groups

    work = Path(tempfile.mkdtemp(prefix="reader_demo_"))
    try:
        multi = work / "multi_rg.parquet"
        rows = md.num_rows
        rg_size = max(1, rows // 8)
        tbl = pq.read_table(path)
        pq.write_table(tbl, multi, compression="zstd", row_group_size=rg_size)
        del tbl
        multi_rgs = pq.ParquetFile(multi).metadata.num_row_groups

        print("同一份数据，两种切法：")
        print(
            f"  · 原文件   {src_rgs} 个行组，"
            f"{path.stat().st_size / 1024 / 1024:.1f} MB（{path.name}）"
        )
        print(
            f"  · 重切一份 {multi_rgs} 个行组（每组 {rg_size:,} 行），"
            f"{multi.stat().st_size / 1024 / 1024:.1f} MB"
        )

        rows_out = []
        for label, reader in READERS:
            if reader == "carquet" and not has_carquet():
                rows_out.append([label, "—", "—", "—"])
                continue
            # 两个文件**交错**着读，顺序每轮对调 —— 免得机器负载的波动
            # 整个砸在其中一边（这台机器是共享的，load1 常年在 2 上下）。
            one: list[float] = []
            two: list[float] = []
            for r in range(5):
                pairs = [(path, one), (multi, two)]
                if r % 2:
                    pairs.reverse()
                for target, bucket in pairs:
                    prepare(reader)
                    t0 = time.perf_counter()
                    obj = read_once(reader, str(target), "all")
                    bucket.append((time.perf_counter() - t0) * 1000)
                    del obj
            t_one, t_two = median(one), median(two)
            gain = t_one / t_two
            if gain > 1.05:
                verdict = f"快 {gain:.1f}×"
            elif gain < 0.95:
                verdict = f"慢 {1 / gain:.1f}×"
            else:
                verdict = "没变"
            rows_out.append([label, f"{t_one:.1f}", f"{t_two:.1f}", verdict])

        print()
        print_table(
            ["reader", f"{src_rgs} 行组 (ms)", f"{multi_rgs} 行组 (ms)", "拆开后"],
            rows_out,
            ["<", ">", ">", ">"],
        )
        print()
        print("  读法：两个文件**交错**着读，各 5 轮取中位数（热页缓存）——")
        print("  这台机器是共享的，load1 常年在 2 上下，不交错的话负载尖峰会整个砸在其中一边。")
        print("  关键看最后一列 —— 拆成多个行组之后谁变快了：")
        print("    · 变快 = 它的并行粒度是**行组**，行组一多就吃上多核")
        print("    · 没变 = 它是单线程 SIMD（carquet），并行帮不上忙")
        print("    · 反倒变慢 = 多读的字节把省下来的时间吃掉了")
        print()
        print("  ⚠️ 注意文件变大了：ZSTD 是**按行组独立压缩**的，行组切碎就没有冗余可压。")
        print(
            "     所以拆行组本质是**拿文件体积换并行**：多核的 reader 净赚，单线程的 reader 净亏。"
        )
        print("     上面那一列是「多付了 24% 的字节之后」的净结果，不是纯粹的并行收益。")

        # 行组数不只决定「谁能并行」，还决定「同一份数据哪条通道快」。
        print()
        print("同一个 DuckDB 查询的两条 Arrow 通道，方向也被行组数决定：")
        ch_rows = []
        for target, tag in ((path, f"{src_rgs} 个行组"), (multi, f"{multi_rgs} 个行组")):
            ex, sl = ab_arrow_channels(target)
            if ex < sl:
                verdict = f"execute() 快 {sl / ex:.2f}×"
            else:
                verdict = f"sql() 快 {ex / sl:.2f}×"
            ch_rows.append([tag, f"{ex:.1f}", f"{sl:.1f}", verdict])
        print_table(
            ["文件", "execute() (ms)", "sql() (ms)", "谁快"],
            ch_rows,
            ["<", ">", ">", "<"],
        )
        print()
        print("  DuckDB 自己会警告：execute() 这条路「makes an extra copy of the rows」，")
        print("  让你改用 `sql()`。**但那说的是内存，不是时间** ——")
        print("  时间上哪条路快取决于文件的行组数：1 个行组时 execute() 反而更快，")
        print("  8 个行组时 sql() 追回来（倍数见上面那张表）。两边的**结果完全一样**。")
        print("  → **别照着警告改代码，也别照着某一次测量改代码。**")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 场景 3


def scenario_duckdb(path: Path) -> None:
    section("3. DuckDB 慢在物化，不在读")

    prof_dir = Path(tempfile.mkdtemp(prefix="reader_demo_prof_"))
    try:
        prof = prof_dir / "prof.json"
        con = duckdb.connect()
        con.sql("PRAGMA enable_profiling='json'")
        con.sql(f"PRAGMA profiling_output='{prof}'")
        t0 = time.perf_counter()
        con.sql(f"SELECT * FROM read_parquet('{path}')").to_arrow_table()
        wall = (time.perf_counter() - t0) * 1000
        con.sql("PRAGMA disable_profiling")

        data = json.loads(prof.read_text(encoding="utf-8"))
        scan = collect = 0.0

        def walk(node: dict) -> None:
            nonlocal scan, collect
            if node.get("type") == "TABLE_SCAN":
                scan = node.get("timing", 0.0)
            if node.get("type") == "RESULT_COLLECTOR":
                collect = node.get("timing", 0.0)
            for child in node.get("children", []) or []:
                walk(child)

        for node in data.get("operator", []):
            walk(node)

        arrow_ms = wall - (scan + collect) * 1000
        print("DuckDB 自己的 profiler 拆的账（`SELECT * FROM read_parquet(...)` → Arrow）：")
        print()
        print_table(
            ["阶段", "耗时 (ms)", "占比"],
            [
                [
                    "扫 parquet（TABLE_SCAN）",
                    f"{scan * 1000:.1f}",
                    f"{scan * 1000 / wall * 100:.0f}%",
                ],
                [
                    "收集结果（RESULT_COLLECTOR）",
                    f"{collect * 1000:.1f}",
                    f"{collect * 1000 / wall * 100:.0f}%",
                ],
                ["转 Arrow（墙钟减去前两项）", f"{arrow_ms:.1f}", f"{arrow_ms / wall * 100:.0f}%"],
                ["合计墙钟", f"{wall:.1f}", "100%"],
            ],
            ["<", ">", ">"],
        )
        print()
        print("  **扫 parquet 只占三成** —— 剩下七成是「把结果变成你要的那个东西」。")

        print()
        print("同一个 DuckDB 查询，物化成不同的东西，代价差一个数量级：")
        rows = []
        for label, key in DUCKDB_ENDPOINTS:
            times = []
            for _ in range(3):
                prepare("duckdb")
                t0 = time.perf_counter()
                obj = endpoint_read(key, str(path))
                times.append((time.perf_counter() - t0) * 1000)
                del obj
            rows.append([label, f"{median(times):.1f}"])
        print_table(["物化端点", "耗时 (ms)"], rows, ["<", ">"])
        print()
        print(
            "  · `.arrow()` 是**惰性**的（返回 RecordBatchReader），数据还没解码 ——"
            " 不能拿它报「读文件花了多久」"
        )
        print(
            "  · `fetchall()` 是纯 python 元组，一百万行要 1500 ms —— 下游要 python 对象就是这个价"
        )
        print("  · `execute().to_arrow_table()` 上面那条 DuckDB 自己的警告说它「会多拷一份」，")
        print("    本机实测（在这份 1 个行组的文件上）它反而更快 —— 警告讲的是**内存**不是时间，")
        print("    而且方向会随行组数翻转（见场景 2 的实测）")
    finally:
        shutil.rmtree(prof_dir, ignore_errors=True)


def endpoint_read(key: str, path: str) -> object:
    sql = f"SELECT * FROM read_parquet('{path}')"
    assert _CON is not None
    if key == "arrow_lazy":
        return _CON.sql(sql).arrow()
    if key == "sql_arrow":
        return _CON.sql(sql).to_arrow_table()
    if key == "execute_arrow":
        return _CON.execute(sql).to_arrow_table()
    if key == "pl":
        return _CON.sql(sql).pl()
    if key == "fetchall":
        return _CON.execute(sql).fetchall()
    raise ValueError(key)


# ---------------------------------------------------------------- 场景 4


def scenario_init(path: Path) -> None:
    section("4. 一次性初始化：新进程里第一次用，各付多少")
    rows = []
    for label, reader in READERS:
        if reader == "carquet" and not has_carquet():
            rows.append([label, "—", "—", "—"])
            continue
        init, read = [], []
        for _ in range(3):
            out = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--init", reader, str(path)],
                capture_output=True,
                text=True,
                check=True,
            )
            d = json.loads(out.stdout.strip().splitlines()[-1])
            init.append(d["init_ms"])
            read.append(d["read_ms"])
        rows.append(
            [
                label,
                f"{median(init):.1f}",
                f"{median(read):.1f}",
                f"{median(init) + median(read):.1f}",
            ]
        )
    print_table(["reader", "初始化 (ms)", "之后的读 (ms)", "合计 (ms)"], rows, ["<", ">", ">", ">"])
    print()
    print("  读一个只有 1 万行的小文件时，这笔初始化开销会盖过读本身 ——")
    print("  **「第一次打开这个文件」和「第二个文件」不是一个量级的成本。**")


def run_init_here(reader: str, path: str) -> None:
    """--init 模式：先付一次初始化，再读一次，两个时间分开报。"""
    t0 = time.perf_counter()
    if reader == "duckdb":
        con = duckdb.connect()
        con.execute("SELECT 1").fetchall()
    elif reader == "polars":
        pl.DataFrame({"x": [1]})
    elif reader == "pyarrow":
        pq.read_metadata(path)  # 只读 footer，顺带把 Arrow 的线程池建起来
    elif reader == "carquet":
        import carquet as cq

        cq.read_metadata(path)
    init_ms = (time.perf_counter() - t0) * 1000

    prepare(reader)
    t1 = time.perf_counter()
    read_once(reader, path, "all")
    read_ms = (time.perf_counter() - t1) * 1000
    print(json.dumps({"init_ms": init_ms, "read_ms": read_ms}))


# ---------------------------------------------------------------- 结论


def conclusion() -> None:
    section("结论")
    print(
        """1. **报「读 parquet 花了 X ms」之前，先把口径说清楚。**
   同一个文件、同一份数据，四个 reader、几个端点、冷热两种进程状态，
   实测跨度 34 ms ~ 1543 ms —— 差 40 多倍。口径就是结论的一部分。

2. **「读慢」和「物化慢」是两回事。**
   DuckDB 的 parquet 扫描只占三成，剩下七成在「收集结果 + 转 Arrow」。
   下游要的是 Arrow 还是 python 元组，决定的是这七成里的一大半。

3. **行组数是选 reader 之前必须看的第一个数。**
   1 个行组的文件，单线程 SIMD 的 carquet 最快；
   拆成 8 个行组，Polars 的行组级并行就吃上了（快 2~3 倍），DuckDB 也跟上，
   单线程的 carquet 反倒慢了 —— 因为 ZSTD 按行组独立压缩，
   **拆行组是拿文件体积换并行**（15.1 MB → 18.8 MB，多核的净赚、单线程的净亏）。

4. **「列式」还是那个结论，但更精确了一层。**
   差的不只是格式（Parquet vs CSV vs .duckdb），
   还有 **reader** —— 同一个格式，换个 reader 能差 3 倍。
   引擎之间的比较（前面的 demo）只是这个问题的一半。

5. **carquet 是可选加彩，不是必须。**
   它的强项（mmap 零拷贝）在 ZSTD + 字典编码的文件上吃不到；
   它是第三方 0.x 的纯 C 库，进依赖要掂量。
   真正值得抄的是它提醒的那件事：**reader 本身是个可以选的部件。**
"""
    )


# ---------------------------------------------------------------- main


def main() -> None:
    if "--once" in sys.argv:
        i = sys.argv.index("--once")
        run_cold_once_here(
            sys.argv[i + 1],
            sys.argv[i + 2],
            sys.argv[i + 3] if len(sys.argv) > i + 3 else "all",
        )
        return
    if "--init" in sys.argv:
        i = sys.argv.index("--init")
        run_init_here(sys.argv[i + 1], sys.argv[i + 2])
        return

    parser = argparse.ArgumentParser(description="读 parquet 的四个 reader 对照")
    parser.add_argument("--repeat", type=int, default=3, help="冷进程测量的重复次数（取中位数）")
    args = parser.parse_args()

    print(f"机器：{common.describe_machine()}")
    versions = " · ".join(
        f"{label} {reader_version(label)}"
        for label, _ in READERS
        if not (label == "carquet" and not has_carquet())
    )
    print(f"版本：{versions}")
    if not has_carquet():
        print("carquet：未安装（可选依赖，`uv sync --group demo` 可装上）—— 下面跳过它那一行")

    path = common.ORDERS_PARQUET
    if not path.exists():
        print(f"\n❌ 找不到 {path}，先跑 `uv run python src/gen_data.py`")
        sys.exit(1)

    scenario_readers(path, args.repeat)
    scenario_row_groups(path)
    scenario_duckdb(path)
    scenario_init(path)
    conclusion()


if __name__ == "__main__":
    main()
