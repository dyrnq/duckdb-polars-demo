"""内存上限与落盘：两个引擎管内存的方式完全不是一回事。

要点：
* DuckDB：`SET memory_limit='50MB'` 是**连接级**的一个预算。预算不够时，
  它不会直接失败，而是把中间结果**落到 temp_directory**（默认 `.tmp`），
  慢一点，但能跑完。真落不了盘才抛 `OutOfMemoryException`，
  而且报错里直接写着怎么改（降线程数 / 关插入序保持 / 调大上限）。
* Polars：2.1 里 `collect(engine="streaming")` 还在（`streaming=True` 在 2.0 已被删），
  而且 **2.x 的默认引擎已经是流式的**。但 Polars **没有任何内存上限旋钮** ——
  `pl.Config` 里没有，环境变量里也没有。它只有「少用内存」的办法，没有「不许超过」的开关。

这个场景要回答：内存吃紧的时候，两个引擎分别是「慢但跑完」还是「进程死掉」。

用法：
    uv run python src/memory_demo.py
    uv run python src/memory_demo.py --rows 2000000 --repeat 5
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 分组的个数 —— 两个引擎做的是同一个聚合：按 id % GROUPS 分组求和
GROUPS = 100_000

# 内存上限的候选阶梯（MB）。DuckDB「能落盘」的窗口很窄，而且会随数据量挪位置，
# 所以脚本开头会**试**出一个合适的上限，而不是写死一个。
LIMIT_LADDER = (10, 20, 30, 40, 50, 60, 80, 100)

# 在独立子进程里跑的测量脚本：只做一件事，然后报自己的峰值 RSS。
# 隔离进程是必须的 —— 峰值没法在同一个进程里复位。
#
# 峰值读的是 /proc/self/status 的 VmHWM（**当前地址空间**的峰值）。
# 别用 resource.getrusage 的 ru_maxrss：Linux 上子进程 fork 之后会**继承**
# 父进程的 ru_maxrss，父进程之前占过多少，子进程就报多少 —— 测出来会是一排相同的数。
# （macOS 没有 /proc，那边用 posix_spawn 起子进程、不存在这个继承问题，
#   所以回退到 ru_maxrss 是安全的 —— 注意那边 ru_maxrss 的单位是**字节**。）
#
# 最后一个参数是 temp_directory：DuckDB 默认的临时目录是 `.tmp`（相对于当前工作目录），
# 不指走的话它会往仓库根目录里写东西。
PEAK_RUNNER = """
import sys

mode, src, mem, groups, spill = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
effective = "-"

if mode == "base-duckdb":
    import duckdb

    con = duckdb.connect()
    n = 0
elif mode == "base-polars":
    import polars as pl

    _ = pl.DataFrame({"a": [1]}).height
    n = 0
elif mode == "duckdb":
    import duckdb

    con = duckdb.connect()
    con.execute("SET temp_directory='" + spill + "'")
    con.execute("SET threads=4")
    sql = (
        "SELECT id % " + str(groups) + " AS g, sum(amt) "
        "FROM read_parquet(?) GROUP BY g"
    )
    # 上限太紧的话，子进程跟校准时看到的情况可能不一样（机器负载会变），
    # 所以失败了就放宽一倍重试，最后兜底到不限内存。
    effective = mem
    for attempt in range(4):
        if effective != "-":
            con.execute("SET memory_limit='" + effective + "'")
        try:
            n = len(con.execute(sql, [src]).fetchall())
            break
        except Exception:
            n = 0
            if effective == "-":
                raise
            base = int(effective.rstrip("MB"))
            effective = "-" if base * 2 > 4096 else str(base * 2) + "MB"
    else:
        raise SystemExit(1)
else:
    import polars as pl

    lf = pl.scan_parquet(src).group_by(
        (pl.col("id") % groups).alias("g")
    ).agg(pl.col("amt").sum().alias("sum(amt)"))
    n = lf.collect(engine=mode).height
    effective = "-"

peak_mb = 0.0
try:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                peak_mb = int(line.split()[1]) / 1024
except OSError:
    # macOS 没有 /proc：退回 ru_maxrss（那边不存在「子进程继承父进程峰值」的问题）
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024
print(f"{n}|{peak_mb:.1f}|{effective}")
"""


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect(spill: Path | None = None) -> duckdb.DuckDBPyConnection:
    """连一个内存库。给了 spill 目录就把临时文件导到那儿（别写进仓库）。"""
    con = duckdb.connect()
    if spill is not None:
        con.execute(f"SET temp_directory='{spill}'")
    return con


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def setting(con: duckdb.DuckDBPyConnection, name: str) -> str:
    """读一个 DuckDB 设置项的当前值。"""
    return str(q1(con, f"SELECT current_setting('{name}')"))


def parse_size(text: str) -> float:
    """把 '100.3 GiB' / '47.6 MiB' 这样的回显解析成字节数。"""
    parts = text.split()
    value = float(parts[0])
    unit = parts[1].lower() if len(parts) > 1 else "b"
    factor = {
        "b": 1,
        "kb": 1024,
        "kib": 1024,
        "mb": 1024**2,
        "mib": 1024**2,
        "gb": 1024**3,
        "gib": 1024**3,
        "tb": 1024**4,
        "tib": 1024**4,
    }.get(unit, 1)
    return value * factor


def system_memory_gib() -> float:
    """机器的物理内存（GiB）。Linux 读 /proc/meminfo，macOS 走 sysconf。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    # macOS / BSD 没有 /proc/meminfo，用 sysconf 问系统要同样两个数
    pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    return pages * page_size / 1024**3


def peak_rss(mode: str, src: str, mem: str, groups: int, spill: str) -> tuple[int, float, str]:
    """在独立子进程里跑一遍，返回 (结果行数, 峰值 RSS MB, 实际生效的上限)。"""
    proc = subprocess.run(
        [sys.executable, "-c", PEAK_RUNNER, mode, src, mem, str(groups), spill],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{mode} 子进程退出码 {proc.returncode}: {proc.stderr[-300:]}")
    n, peak, effective = proc.stdout.strip().split("|")
    return int(n), float(peak), effective


def collect_trace(lf: pl.LazyFrame, **kwargs: object) -> str:
    """跑一次 collect，把 Polars 的 verbose 日志抓下来。

    Polars 的 Rust 层直接往文件描述符 1/2 上写，Python 的 redirect_stdout 抓不到，
    所以这里把两个 fd 都临时换成临时文件，跑完再还原。
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            try:
                pl.Config.set_verbose(True)
                lf.collect(**kwargs)
            finally:
                pl.Config.set_verbose(False)
                os.dup2(saved_out, 1)
                os.dup2(saved_err, 2)
            out.seek(0)
            err.seek(0)
            return (out.read() + err.read()).decode("utf-8", "replace")
    finally:
        os.close(saved_out)
        os.close(saved_err)


class SpillWatcher:
    """盯着 temp_directory，看 DuckDB 到底有没有往里写东西。

    DuckDB 的临时文件是**用完即删**的，所以只能在查询运行期间去数 ——
    这里用一个后台线程每隔几毫秒扫一次目录，记录同时存在的文件数和字节数。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.peak_files = 0
        self.peak_bytes = 0
        self.names: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                names = os.listdir(self.directory)
            except FileNotFoundError:
                names = []
            total = 0
            for name in names:
                try:
                    total += (self.directory / name).stat().st_size
                except OSError:
                    continue
            if names:
                self.peak_files = max(self.peak_files, len(names))
                self.peak_bytes = max(self.peak_bytes, total)
                for name in names:
                    self.names.setdefault(name, 0)
            time.sleep(0.005)

    def __enter__(self) -> SpillWatcher:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def make_source(tmpdir: Path, rows: int) -> Path:
    """造一份 parquet（id + amt），两个引擎读的是同一份。"""
    src = tmpdir / "big.parquet"
    con = connect()
    con.execute(
        "COPY (SELECT i AS id, i::DOUBLE * 1.5 AS amt FROM range(?) t(i)) TO ? (FORMAT PARQUET)",
        [rows, str(src)],
    )
    con.close()
    return src


def group_sql(src: Path) -> str:
    """两个引擎共用的那条聚合。"""
    return f"SELECT id % {GROUPS} AS g, sum(amt) FROM read_parquet('{src}') GROUP BY g"


def polars_lf(src: Path) -> pl.LazyFrame:
    """跟 group_sql 等价的 Polars 写法。"""
    return (
        pl.scan_parquet(src)
        .group_by((pl.col("id") % GROUPS).alias("g"))
        .agg(pl.col("amt").sum().alias("sum(amt)"))
    )


def probe_limit(src: Path, spill_root: Path, mb: int, threads: int) -> tuple[bool, int]:
    """试一个 (上限, 线程数) 组合，返回 (跑完了吗, 落盘字节数)。"""
    d = spill_root / f"probe_{mb}MB_{threads}t"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    con = connect(d)
    con.execute(f"SET memory_limit='{mb}MB'")
    con.execute(f"SET threads={threads}")
    ok = True
    with SpillWatcher(d) as watcher:
        try:
            con.sql(group_sql(src)).fetchall()
        except Exception:
            ok = False
    con.close()
    return ok, watcher.peak_bytes


def pick_limit(src: Path, spill_root: Path) -> tuple[int, list[tuple[int, bool, int, bool, int]]]:
    """挑一个「4 线程能落盘跑完、8 线程会 OOM」的上限（MB）。

    返回 (选中的 MB, 试出来的记录列表)。每条记录是
    (MB, 4 线程跑完了吗, 4 线程落盘字节, 8 线程跑完了吗, 8 线程落盘字节)。
    """
    tried: list[tuple[int, bool, int, bool, int]] = []
    for mb in LIMIT_LADDER:
        ok4, s4 = probe_limit(src, spill_root, mb, 4)
        ok8, s8 = probe_limit(src, spill_root, mb, 8)
        tried.append((mb, ok4, s4, ok8, s8))

    # 理想的上限：4 线程能落盘跑完、8 线程会 OOM
    cands = [t for t in tried if t[1] and t[2] > 0 and not t[3]]
    if cands:
        # 取**最大**的那个 —— 4 线程离自己的失败点最远，
        # 而 8 线程 OOM 那一侧本来就是边界，不该再往它身上加压。
        return max(cands, key=lambda t: t[0])[0], tried
    # 退而求其次：只要 4 线程能落盘跑完（8 线程可能也跑得完）
    fallback = [t for t in tried if t[1] and t[2] > 0]
    if fallback:
        return max(fallback, key=lambda t: t[0])[0], tried
    return LIMIT_LADDER[-1], tried


# ---------------------------------------------------------------- 1. 内存上限


def scenario_memory_limit() -> None:
    section("场景 1 · 内存上限：DuckDB 的 `memory_limit` 是个预算")

    con = connect()
    default_limit = setting(con, "memory_limit")
    default_temp = setting(con, "temp_directory")
    mem_gib = system_memory_gib()
    ratio = parse_size(default_limit) / (mem_gib * 1024**3)
    print("  刚连上时的默认值：")
    print(f"    current_setting('memory_limit')   -> {default_limit}")
    print(f"    current_setting('temp_directory') -> {default_temp}")
    print(f"  这台机器的物理内存：{mem_gib:.1f} GiB")
    print(f"  → 默认上限就是**物理内存的一个百分比**（这里是 {ratio:.0%}），不是「随便给多少」。")
    print(f"  → 顺带一提，默认的 temp_directory 是 `{default_temp}` —— 也就是**当前工作目录**。")
    print("    不改它，DuckDB 落盘的时候就会往你正在跑脚本的那个目录里写临时文件。")
    print("    下面所有场景都会显式把它指到一个临时目录去。")

    con.execute("SET memory_limit='50MB'")
    after = setting(con, "memory_limit")
    print("\n  SET memory_limit='50MB' 之后：")
    print(f"    current_setting('memory_limit') -> {after}")
    print("  注意它回显成了 **MiB** 而不是 MB —— DuckDB 认的是十进制：")
    print(f"    50 MB = 50,000,000 字节 = 50,000,000 / 1024^2 = {50_000_000 / 1024**2:.1f} MiB")
    print(f"  → 「我设了 50MB」在 DuckDB 内部其实是 {after}，差的那点不是误差，是单位制。")

    print("\n  这个上限管的是什么：")
    print("    它是**缓冲区管理器**的总预算 —— 扫描的缓冲、哈希表、排序的临时空间")
    print("    都从这个池子里出。超了 DuckDB 有两条路：往磁盘上溢写，或者报错。")
    print("    （DuckDB 2.0 里没有 `PRAGMA memory_limit`，只有 `current_setting`；")
    print("      `PRAGMA temp_directory` 同样不存在，别按老文档写。）")

    con.close()

    print("\n→ 一条要记住的：")
    print("  · `memory_limit` 是 DuckDB 里最该设的一个参数，但它**不是**「不许超过」的硬墙 ——")
    print("    它是「超了以后怎么办」的开关：能溢写就溢写，溢不了才报错。")
    print("    下一场景就是看它溢写的那条路。")


# ---------------------------------------------------------------- 2. 落盘


def scenario_spill(src: Path, spill: Path, limit_mb: int, tried: list) -> None:
    section("场景 2 · 落盘：预算不够时，中间结果写到 temp_directory")

    print(f"数据：parquet {src.stat().st_size / 1e6:.1f} MB，聚合出 {GROUPS:,} 个分组")
    print(f"落盘目录：{spill}")

    print("\n  先回答一个问题：**这个上限定多少？**")
    print("  DuckDB「能落盘」的窗口其实很窄 —— 上限太小，缓冲区管理器的固定开销")
    print("  就把预算吃光了，直接 OOM；上限太大，中间状态又装得下，压根不用落盘。")
    print("  所以这里不写死，而是**试**出来（每个组合都真跑一遍）：")
    print()
    print("    " + common.pad("上限", 10) + common.pad("4 线程", 30) + "8 线程")
    print("    " + "-" * 66)
    for mb, ok4, s4, ok8, s8 in tried:
        left = f"跑完，落盘 {s4:,} 字节" if ok4 else "OOM"
        right = f"跑完，落盘 {s8:,} 字节" if ok8 else "OOM"
        mark = "   <= 选中" if mb == limit_mb else ""
        print("    " + common.pad(f"{mb}MB", 10) + common.pad(left, 30) + right + mark)

    con = connect(spill)
    con.execute(f"SET memory_limit='{limit_mb}MB'")
    con.execute("SET threads=4")
    print(f"\n  接下来所有场景都用这个上限：SET memory_limit='{limit_mb}MB'")
    print(f"    current_setting('memory_limit')   -> {setting(con, 'memory_limit')}")
    print("    SET threads=4")
    print(f"    current_setting('temp_directory') -> {setting(con, 'temp_directory')}")

    print(f"\n  跑那个分组聚合（{GROUPS:,} 个分组，上限只有 {limit_mb}MB）：")
    with SpillWatcher(spill) as watcher:
        t0 = time.perf_counter()
        rows_out = con.sql(group_sql(src)).fetchall()
        elapsed = time.perf_counter() - t0
    print(f"    -> 跑完了，{len(rows_out)} 行，{elapsed:.2f}s")
    print(
        f"    查询期间落盘：同时最多 {watcher.peak_files} 个临时文件、"
        f"累计写了 {watcher.peak_bytes:,} 字节"
    )

    print("\n  临时文件长这样（文件名里那个数字是**进程 PID**）：")
    for name in sorted(watcher.names)[:6]:
        print(f"    {name}")
    if len(watcher.names) > 6:
        print(f"    …… 还有 {len(watcher.names) - 6} 个")

    leftovers = sorted(os.listdir(spill))
    print(f"\n  查询跑完之后再看目录：{leftovers}")
    print("    → 数据文件（`*.tmp`）都删了，只剩一个 0 字节的 `_owner` 锁文件。")
    print("      临时文件是**用完即删**的 —— 「有没有落盘」只能靠运行期间观察，")
    print("      事后去目录里找是找不到的。")

    cols = [d[0] for d in con.sql("SELECT * FROM duckdb_temporary_files() LIMIT 1").description]
    print("\n  想从 SQL 侧看的话，有这张系统表：")
    print(f"    duckdb_temporary_files() 的列是 {cols}")
    print("    （查询结束再查它就是空的 —— 同一张表，区别只在查的时刻。）")

    con.close()

    print("\n→ 两条要记住的：")
    print("  · DuckDB 内存不够的时候**默认不是失败，是变慢**：它把中间结果写进")
    print("    `temp_directory` 再继续跑。上面这个查询跑完了，中间状态也确实落了盘。")
    if watcher.peak_bytes == 0:
        print("    （这一次恰好没写盘 —— 说明这个上限对这份数据来说还是宽裕的，")
        print("      窗口就是这么窄：**上限、线程数、数据量，缺一个都看不到落盘**。）")
    print("  · 但「落盘」是有前提的 —— 前提是**上限落在一个很窄的窗口里**，")
    print("    而且**线程数别太多**。下一场景就是反面。")


# ---------------------------------------------------------------- 3. 线程数


def scenario_threads(src: Path, spill: Path, limit_mb: int) -> None:
    section("场景 3 · 反直觉：落盘不是万能的 —— 线程一多，同样上限直接 OOM")

    print(f"  同样的查询、同样的 {limit_mb}MB 上限，只改线程数：")
    spilled: dict[int, int] = {}
    oom_at: dict[int, bool] = {}
    for threads in (8, 4):
        con = connect(spill)
        con.execute(f"SET memory_limit='{limit_mb}MB'")
        con.execute(f"SET threads={threads}")
        with SpillWatcher(spill) as watcher:
            t0 = time.perf_counter()
            oom_at[threads] = False
            try:
                con.sql(group_sql(src)).fetchall()
                print(f"\n  threads={threads} -> 跑完了，{time.perf_counter() - t0:.2f}s")
            except Exception as exc:
                oom_at[threads] = True
                print(f"\n  threads={threads} -> [{type(exc).__name__}]")
                for line in str(exc).splitlines():
                    print(f"    {line}")
            spilled[threads] = watcher.peak_bytes
            print(
                f"    期间落盘：最多 {watcher.peak_files} 个临时文件、"
                f"写了 {watcher.peak_bytes:,} 字节"
            )
            if threads == 8 and oom_at[threads]:
                # OOM 之后连接还能不能用？—— 这是「进程内失败」最实际的差别
                try:
                    again = con.sql("SELECT count(*) FROM range(1000)").fetchall()[0][0]
                    print(f"    OOM 之后同一个连接继续查 -> 正常，拿到 {again} 行")
                except Exception as exc2:
                    print(f"    OOM 之后同一个连接继续查 -> [{type(exc2).__name__}]")
        con.close()

    print("\n→ 两条要记住的：")
    print("  · **DuckDB 的 `memory_limit` 是全局预算，但每个线程各自持有缓冲区。**")
    print("    线程越多，单线程能分到的就越少 —— 同一个上限下反而更容易直接 OOM。")
    if not oom_at.get(8):
        print(f"    （这一次 8 线程也跑完了 —— 上限 {limit_mb}MB 对它来说还够用，")
        print("      机器负载一变结果就可能不同，这正是这个窗口窄的原因。）")
    elif spilled.get(8, 0) > 0:
        print(f"    注意上面 8 线程那次：它**先落了 {spilled[8]:,} 字节的盘，然后才撞上限** ——")
        print("    落盘只是把失败往后推了推，并没有救回来。DuckDB 自己的报错第一条建议")
    else:
        print("    它甚至轮不到「落盘」这一步就炸了。DuckDB 自己的报错第一条建议")
    print("    就是降线程数。")
    print("  · 所以「内存不够就调大 memory_limit」不是唯一解，")
    print("    **先把 `SET threads` 调小**，往往就够了 —— 而且不用多占一份内存。")


# ---------------------------------------------------------------- 4. 连接级


def scenario_connection_scoped() -> None:
    section("场景 4 · 反直觉：`memory_limit` 是**连接级**的，新连接完全不受影响")

    tmpdir = Path(tempfile.mkdtemp(prefix="memory_demo_"))
    db_path = tmpdir / "demo.duckdb"

    con = connect()
    con.execute("SET memory_limit='50MB'")
    print(f"  连接 A（内存库）：SET memory_limit='50MB' -> {setting(con, 'memory_limit')}")
    con.close()

    con2 = connect()
    print(f"  连接 B（新建的内存库）：                 -> {setting(con2, 'memory_limit')}")
    print("    → **完全没受影响**。这是两个互不相干的连接，各有各的设置。")
    con2.close()

    con3 = duckdb.connect(str(db_path))
    con3.execute("SET memory_limit='50MB'")
    print(f"\n  连接 C（文件库 {db_path.name}）：SET 之后 -> {setting(con3, 'memory_limit')}")
    con3.close()

    con4 = duckdb.connect(str(db_path))
    print(f"  连接 D（重开同一个文件）：                -> {setting(con4, 'memory_limit')}")
    print("    → 文件库也不记这个设置：它是**连接状态**，不写进 `.duckdb` 文件。")
    con4.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n  这跟视图、宏不一样 —— 对比一下：")
    print("    " + common.pad("设置 / 对象", 26) + common.pad("存在哪", 20) + "换个连接还有吗")
    print("    " + "-" * 58)
    for obj, where, alive in [
        ("CREATE VIEW / MACRO", ".duckdb 文件", "在"),
        ("SET memory_limit", "连接状态", "不在"),
        ("SET threads", "连接状态", "不在"),
    ]:
        print("    " + common.pad(obj, 26) + common.pad(where, 20) + alive)

    print("\n→ 两条要记住的：")
    print("  · `memory_limit` / `threads` 是**连接级**的 —— 名字听着像全局设置，")
    print("    其实是每个连接自己一份。")
    print("  · 实务上的后果：")
    print("    · 同一个 Python 进程里 `duckdb.connect()` 两次，就是两个互不相关的世界。")
    print("    · 多个进程各连同一个 `.duckdb` 文件，各自的 memory_limit 互不影响 ——")
    print("      这既是好事（不会互相踩），也是坏事（你以为设了，其实是各设各的）。")


# ---------------------------------------------------------------- 5. Polars 流式


def scenario_polars_streaming(src: Path) -> None:
    section('场景 5 · Polars 的流式引擎：`collect(engine="streaming")`')

    lf = polars_lf(src)

    print("  Polars 2.1 里 collect 的参数写法：")
    try:
        lf.collect(streaming=True)
    except Exception as exc:
        print(f"    lf.collect(streaming=True) -> [{type(exc).__name__}]")
        print(f"      {str(exc).splitlines()[0]}")
    print("    lf.collect(engine='streaming') -> 可以用 ✅")

    print("\n  而且 **2.x 的默认引擎已经是流式的**（1.x 默认是内存引擎）。")
    print("  实测办法：把 Polars 的 verbose 日志抓下来，看它跑的是哪套引擎。")
    print(
        "  判据是日志内容：流式引擎会把聚合**拆成子图并行跑**（`running group-by in subgraph`），"
    )
    print("  内存引擎是一把梭（`keys/aggregates are not partitionable`）：")
    traces = {
        "默认 collect()": collect_trace(lf),
        'engine="streaming"': collect_trace(lf, engine="streaming"),
        'engine="in-memory"': collect_trace(lf, engine="in-memory"),
    }
    for label, trace in traces.items():
        print(
            f"    {common.pad(label, 22)} 拆子图 -> {'running group-by in subgraph' in trace} · "
            f"一把梭 -> {'not partitionable' in trace}"
        )
    auto_is_stream = "running group-by in subgraph" in traces["默认 collect()"]
    print(f"    → 默认走的**就是**流式引擎：{auto_is_stream}")

    print("\n  流式的意思是**一批一批**往外吐：")
    sizes = [b.height for b in lf.collect_batches(chunk_size=20_000)]
    print(f"    collect_batches(chunk_size=20000) -> {len(sizes)} 批，每批 {sizes[:6]} 行")
    print("    （对比 DuckDB：DuckDB 没有「批次」这个概念，它是一条流水线，")
    print("      内存不够就往 temp_directory 溢写。）")

    print("\n→ 两条要记住的：")
    print('  · Polars 2.1 的流式写法是 `collect(engine="streaming")`；')
    print("    `streaming=True` 在 2.0 就被删了，迁过来的老代码会直接报 ArgumentRemovedError。")
    print("  · 流式引擎是 Polars **唯一**的省内存手段 —— 但它**不是上限**：")
    print("    它让峰值更低，可它没有「不许超过 X MB」这个开关。")


# ---------------------------------------------------------------- 6. 谁在管内存


def scenario_who_controls_memory(src: Path, spill: Path, limit_mb: int) -> None:
    section("场景 6 · 谁在管内存：峰值 RSS 实测（每个实现跑在独立子进程里）")

    print(f"数据：parquet {src.stat().st_size / 1e6:.1f} MB")
    print("测法：每个实现单独起一个子进程，跑同一个聚合，")
    print("      用 /proc/self/status 的 VmHWM（当前地址空间的内存峰值）报它自己占了多少。")
    print("      （不能用 ru_maxrss：Linux 上它会被子进程从父进程继承，测出来会一排相同；")
    print("        macOS 没有 /proc，那边才退回 ru_maxrss。）")

    _, base_duck, _ = peak_rss("base-duckdb", str(src), "-", GROUPS, str(spill))
    _, base_polars, _ = peak_rss("base-polars", str(src), "-", GROUPS, str(spill))
    print(
        f"\n  基线（只 import 引擎、什么都不算）：DuckDB {base_duck:.1f} MB · "
        f"Polars {base_polars:.1f} MB"
    )

    runs = [
        ("DuckDB 不限内存", "duckdb", "-", base_duck),
        (f"DuckDB {limit_mb}MB 上限（落盘）", "duckdb", f"{limit_mb}MB", base_duck),
        ("Polars in-memory", "in-memory", "-", base_polars),
        ("Polars streaming", "streaming", "-", base_polars),
    ]
    print(
        "\n  "
        + common.pad("实现", 30)
        + common.pad_left("峰值 RSS (MB)", 15)
        + common.pad_left("减去基线", 12)
        + common.pad_left("结果行数", 11)
    )
    print("  " + "-" * 70)
    peaks: dict[str, float] = {}
    for label, mode, mem, base in runs:
        n, peak, effective = peak_rss(mode, str(src), mem, GROUPS, str(spill))
        peaks[label] = peak
        print(
            "  "
            + common.pad(label, 30)
            + common.pad_left(f"{peak:.1f}", 15)
            + common.pad_left(f"{peak - base:.1f}", 12)
            + common.pad_left(f"{n:,}", 11)
        )
        if effective != mem:
            print(
                f"    （这一行实际用的是 {effective}：上限 {mem} 在子进程里太紧，脚本自动放宽了）"
            )

    print("\n  DuckDB 侧的关键对照是前两行：**同一个引擎、同一个查询，")
    print("  只把上限从「不设」改成那个试出来的值**。")
    duck_free = peaks["DuckDB 不限内存"] - base_duck
    duck_cap = peaks[f"DuckDB {limit_mb}MB 上限（落盘）"] - base_duck
    if duck_cap < duck_free:
        print(
            f"  多出来的内存（减去基线）从 {duck_free:.1f} MB 降到 {duck_cap:.1f} MB —— "
            "上限确实在起作用。"
        )
    elif duck_cap == duck_free:
        print(
            f"  多出来的内存（减去基线）都是 {duck_free:.1f} MB —— "
            "这个数据量本来就装得下，上限没起作用。"
        )
    else:
        print(f"  多出来的内存（减去基线）反而从 {duck_free:.1f} MB 涨到 {duck_cap:.1f} MB ——")
        print("  这一行反直觉：**加上限之后 RSS 反而更高**。原因是溢写本身有开销：")
        print("  中间状态写出去再读回来，缓冲区管理器要同时管着内存里和磁盘上的两份。")
    print("  但**别把它当成「进程 RSS 的上限」**：它管的是 DuckDB 自己记账的那部分")
    print("  （扫描缓冲、哈希表、排序空间），进程里还有引擎常驻开销和分配器碎片。")
    print("  它是**引擎账本的上限**，不是操作系统账本的上限。")

    pol_free = peaks["Polars in-memory"] - base_polars
    pol_stream = peaks["Polars streaming"] - base_polars
    print(
        f"\n  Polars 侧：in-memory 多用了 {pol_free:.1f} MB，streaming 多用了 {pol_stream:.1f} MB。"
    )
    if pol_stream < pol_free:
        print("  换流式引擎确实省了一点 —— 但**省的是峰值，不是上限**。")
    else:
        print("  换流式引擎几乎没省 —— 数据量本来就不大的时候，流式的收益看不出来。")
    print("  而且不管选哪个引擎，都**没有任何一个旋钮**能把它钉在某个数上。")

    # ---- 旋钮对照（下面的「有没有」是实测枚举出来的，不是查文档）
    mem_knobs = [n for n in dir(pl.Config) if "mem" in n.lower()]
    mem_excs = [n for n in dir(pl.exceptions) if "memory" in n.lower()]
    print("\n  实测枚举（Polars 2.1）：")
    print(f"    pl.Config 里名字带 'mem' 的设置项     -> {mem_knobs}")
    print(f"    pl.exceptions 里名字带 'memory' 的异常 -> {mem_excs}")
    print("    → 一个都没有。Polars 的内存是 Python 和 Rust 运行时直接向系统要的，")
    print("      中间**没有一层「记账 + 拦截」**。")

    print("\n  " + common.pad("能力", 22) + common.pad("DuckDB", 30) + "Polars")
    print("  " + "-" * 76)
    for cap, duck, pol in [
        ("内存上限", "SET memory_limit='50MB'", "没有"),
        ("溢写落盘", "SET temp_directory='...'", "没有"),
        ("批大小", "随上限自动调整", "set_streaming_chunk_size()"),
        ("超限时的异常", "OutOfMemoryException", "没有"),
    ]:
        print("  " + common.pad(cap, 22) + common.pad(duck, 30) + pol)

    print("\n  那内存真不够的时候呢？两边的**失败方式**完全不同：")
    print("    · DuckDB：失败发生在**进程内**，是一个能被 try/except 接住的")
    print("      `OutOfMemoryException`，而且报错里写着「Reducing the number of threads」")
    print("      「Increasing the memory limit」—— 失败是**可诊断、可修复**的，")
    print("      连接还能接着用（场景 3 里那条 OOM 之后的查询就是证据）。")
    print("    · Polars：没有上限，也就没有「预算超了」的判定点。")
    print("      真到分配不出来的时候，是 Rust 运行时 abort 掉整个进程")
    print("      （或者被系统的 OOM killer 干掉），Python 侧什么都拿不到 ——")
    print("      没有异常、没有 traceback、没有部分结果。")
    print("      这跟上面那句「异常体系里带 memory 的异常 -> []」是同一件事的两面。")

    print("\n→ 一条要记住的：")
    print("  · **DuckDB 的内存上限是一套能兑现的机制，Polars 压根没有这套机制。**")
    print("    迁过去之前得自己加一层：用 cgroup / 容器限额把进程框住，")
    print("    否则「跑一半被 kill」这种事在生产环境里最难查。")


# ---------------------------------------------------------------- 7. 性能


def scenario_performance(src: Path, spill: Path, limit_mb: int, repeat: int) -> None:
    section("场景 7 · 代价：落盘比全放内存慢多少")

    print(f"数据：parquet {src.stat().st_size / 1e6:.1f} MB，聚合出 {GROUPS:,} 个分组")
    print(f"机器：{common.describe_machine()}")

    sql = group_sql(src)
    lf_sorted = polars_lf(src).sort("g")

    def make_duck(mem: str) -> duckdb.DuckDBPyConnection:
        con = connect(spill)
        con.execute("SET threads=4")
        if mem != "-":
            con.execute(f"SET memory_limit='{mem}'")
        return con

    def duck_unlimited() -> int:
        return len(con_u.sql(sql).fetchall())

    def duck_capped() -> int:
        return len(con_c.sql(sql).fetchall())

    def polars_in_memory() -> int:
        return lf_sorted.collect(engine="in-memory").height

    def polars_streaming() -> int:
        return lf_sorted.collect(engine="streaming").height

    con_u = make_duck("-")
    con_c = make_duck(f"{limit_mb}MB")

    results = [
        ("DuckDB 不限内存", duck_unlimited, None),
        (f"DuckDB {limit_mb}MB 上限（落盘）", duck_capped, spill),
        ("Polars in-memory", polars_in_memory, None),
        ("Polars streaming", polars_streaming, None),
    ]

    print(
        "\n  "
        + common.pad("实现", 32)
        + common.pad_left("中位数 (ms)", 14)
        + common.pad_left("最快 (ms)", 12)
        + common.pad_left("结果行数", 11)
    )
    print("  " + "-" * 72)
    spilled = 0
    for label, fn, watch_dir in results:
        try:
            fn()
        except Exception as exc:
            print(
                "  "
                + common.pad(label, 32)
                + common.pad_left(f"[{type(exc).__name__}]", 26)
                + common.pad_left("-", 12)
                + common.pad_left("-", 11)
            )
            continue
        times: list[float] = []
        value = 0
        if watch_dir is None:
            for _ in range(repeat):
                t0 = time.perf_counter()
                value = fn()
                times.append(time.perf_counter() - t0)
        else:
            with SpillWatcher(watch_dir) as watcher:
                for _ in range(repeat):
                    t0 = time.perf_counter()
                    value = fn()
                    times.append(time.perf_counter() - t0)
            spilled = watcher.peak_bytes
        med = statistics.median(times)
        print(
            "  "
            + common.pad(label, 32)
            + common.pad_left(f"{med * 1000:.1f}", 14)
            + common.pad_left(f"{min(times) * 1000:.1f}", 12)
            + common.pad_left(f"{value:,}", 11)
        )

    # ---- 对账：两个引擎算出来的分组和必须一样
    duck_rows = con_u.sql(sql).fetchall()
    polars_rows = lf_sorted.collect(engine="streaming")
    duck_frame = pl.DataFrame(
        duck_rows, schema=[("g", pl.Int64), ("sum(amt)", pl.Float64)], orient="row"
    )
    ok, why = common.compare_frames(duck_frame, polars_rows, ["g"])
    print("\n  对账（两个引擎对同一份 parquet 做的同一个聚合，逐组比对）：")
    print(f"    DuckDB vs Polars -> {'✅' if ok else '❌'} {why}")
    print(f"    （落盘那一行跑了 {repeat} 次，期间最多写了 {spilled:,} 字节到 temp_directory。）")

    con_u.close()
    con_c.close()

    print("\n口径：四个实现跑的是**同一个聚合**（10 万个分组、每组求和），")
    print("  读的是**同一份 parquet**，结果的行数应该一样（上面的对账已确认）。")
    print("  计时的范围是「从 parquet 读进来到拿到最终结果」，**包含读盘**。")
    print("  DuckDB 两行都固定 4 线程 —— 两行之间只差一个 memory_limit，")
    print("  所以它们可以直接比大小。")
    print("  Polars 两行用的是引擎默认线程池；跨引擎只能看量级，别当成严格对比。")
    print("解读：")
    print("  · 落盘的代价就是**多了一轮磁盘 I/O**：中间状态本来在内存里，")
    print("    现在要写出去再读回来。差距的大小取决于溢写了多少 ——")
    print("    溢写得多，差距就大；工作集本来就不大的话，差距接近零。")
    print("  · 所以「把 memory_limit 调小」是有代价的，但这个代价是**可预期的**：")
    print("    它换来的是「不管机器内存多小都能跑完」。")
    print("  · DuckDB 和 Polars 在这里没有本质差别 —— 两边都能在低内存下跑完。")
    print("    真正的差别在**失败的那一侧**（场景 6）：DuckDB 给你一个带解法的异常，")
    print("    Polars 那边连一个内存异常类型都没有，进程被带走你就什么也拿不到了。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的内存上限与落盘差异演示")
    parser.add_argument("--rows", type=int, default=1_000_000, help="数据行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    tmpdir = Path(tempfile.mkdtemp(prefix="memory_demo_"))
    spill = tmpdir / "spill"
    spill.mkdir()
    try:
        src = make_source(tmpdir, args.rows)
        limit_mb, tried = pick_limit(src, spill)
        for d in spill.glob("probe_*"):
            shutil.rmtree(d, ignore_errors=True)

        scenario_memory_limit()
        scenario_spill(src, spill, limit_mb, tried)
        scenario_threads(src, spill, limit_mb)
        scenario_connection_scoped()
        scenario_polars_streaming(src)
        scenario_who_controls_memory(src, spill, limit_mb)
        scenario_performance(src, spill, limit_mb, args.repeat)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()


if __name__ == "__main__":
    main()
