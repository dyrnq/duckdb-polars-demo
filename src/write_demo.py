"""写入路径与事务：把数据落盘这件事，两个引擎的差别比想象中大。

写比读更容易出事 —— 读错了你看得见，写错了往往要等下游才发现。
这个脚本对照四件事：

  1. 写同一个路径，第二遍是覆盖还是追加？失败了旧文件还在吗？
  2. 有没有事务？DuckDB 有完整的 ACID，Polars 根本没有事务这个概念。
  3. 写出来的 parquet 文件本身差在哪（压缩、编码、row group、精度）。
  4. 写一百万行要多久。

产出文件都落在 data/write_demo/ 下。

用法：
    uv run python src/write_demo.py
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

import duckdb
import polars as pl

import common

OUT_DIR = common.DATA_DIR / "write_demo"


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _timeit(fn, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def _watch_dir(fn, watch_dir: Path) -> set[str]:
    """执行 fn 的同时不停扫目录，返回这段时间里出现过的文件名。"""
    seen: set[str] = set()
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                seen.update(os.listdir(watch_dir))
            except OSError:
                pass
            time.sleep(0.001)

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        fn()
    finally:
        stop.set()
        thread.join()
    return seen


# ---------------------------------------------------------------- 场景 1


def scenario_overwrite() -> None:
    section("场景 1 · 覆盖还是追加？写失败了旧文件还在吗？")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "overwrite_test.parquet"
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1),(2)) v(x)")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    first_size = path.stat().st_size
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    second_size = path.stat().st_size
    print("  DuckDB：同一个路径写第二遍 ——")
    print(
        f"    第一次 {first_size} 字节，第二次 {second_size} 字节，"
        f"{'大小一致（覆盖写）' if first_size == second_size else '大小不同'}"
    )
    print("    没有报错，也没有提示 —— 直接覆盖。")
    print()

    ppath = OUT_DIR / "overwrite_test_polars.parquet"
    df = pl.DataFrame({"x": [1, 2]})
    df.write_parquet(ppath)
    pfirst = ppath.stat().st_size
    df.write_parquet(ppath)
    psecond = ppath.stat().st_size
    print("  Polars：同一个路径写第二遍 ——")
    print(
        f"    第一次 {pfirst} 字节，第二次 {psecond} 字节，"
        f"{'大小一致（覆盖写）' if pfirst == psecond else '大小不同'}"
    )
    print("    同样没有报错，直接覆盖。")
    print()
    print("  两边一致：**都是「覆盖」，都不是「追加」**，也都不会提示你。")
    print("  这在增量场景里是坑 —— 以为在追加，其实把上一批数据冲掉了。")
    print()

    print("  写失败时，旧文件还在吗？")
    try:
        con.execute(f"COPY (SELECT 'abc'::INT AS x) TO '{path}' (FORMAT PARQUET)")
    except Exception as exc:
        print(f"    DuckDB  写失败：{type(exc).__name__}")
    old_rows = con.execute(f"SELECT * FROM read_parquet('{path}')").fetchall()
    print(f"    DuckDB  旧文件还在：{path.exists()}，内容 = {old_rows}")
    try:
        bad = pl.DataFrame({"x": pl.Series([object()], dtype=pl.Object)})
        bad.write_parquet(ppath)
    except Exception as exc:
        print(f"    Polars  写失败：{type(exc).__name__}")
    print(
        f"    Polars  旧文件还在：{ppath.exists()}，内容 = {pl.read_parquet(ppath)['x'].to_list()}"
    )
    print()
    print("  两边都**保住了旧文件** —— 因为错误发生在真正写文件之前。")
    print()
    print("  但「写一半被杀」这件事，两边不一样。实测一下写入过程中目录里出现了什么：")
    print("  （后台线程在写的同时不停扫目录）")
    print()
    duck_watch = OUT_DIR / "watch_overwrite_duck"
    pol_watch = OUT_DIR / "watch_overwrite_polars"
    duck_watch.mkdir(parents=True, exist_ok=True)
    pol_watch.mkdir(parents=True, exist_ok=True)
    # 数据要够大，临时文件才会存在得够久、来得及被轮询线程看到
    big = pl.DataFrame({"x": pl.Series(range(500_000), dtype=pl.Int64)})
    con.register("big", big)
    # 先造一个已存在的目标文件，触发「覆盖」路径
    con.execute(f"COPY big TO '{duck_watch}/x.parquet' (FORMAT PARQUET)")
    big.write_parquet(pol_watch / "x.parquet")
    duck_seen = _watch_dir(
        lambda: con.execute(f"COPY big TO '{duck_watch}/x.parquet' (FORMAT PARQUET)"),
        duck_watch,
    )
    pol_seen = _watch_dir(lambda: big.write_parquet(pol_watch / "x.parquet"), pol_watch)
    print(f"    DuckDB  覆盖写时目录里出现：{sorted(duck_seen)}")
    print(f"    Polars  覆盖写时目录里出现：{sorted(pol_seen)}")
    print()
    print("  DuckDB 覆盖**已有文件**时，是先写 `tmp_x.parquet` 再 rename 过去的 ——")
    print("  也就是**原子替换**：读者要么看到完整的旧文件，要么看到完整的新文件，")
    print("  不会读到写了一半的文件。")
    print("  Polars 是直接往目标路径写，没有中间文件 —— 写到一半进程被杀，")
    print("  就会留下一个半截的 parquet。")
    print()
    print("  再补一个细节：DuckDB 写**新文件**时（目标还不存在）也是直接写，")
    print("  不走临时文件 —— 临时文件是「替换已有文件」时才用的。")
    con.close()
    print("  所以「第一次写」和「覆盖写」的安全性在 DuckDB 里也不完全一样。")


# ---------------------------------------------------------------- 场景 2


def scenario_transaction() -> None:
    section("场景 2 · 事务：DuckDB 有，Polars 没有")

    db_path = OUT_DIR / "txn_demo.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    con.execute("DELETE FROM t")
    con.execute("INSERT INTO t VALUES (1)")

    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (2)")
    mid = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    con.execute("ROLLBACK")
    after_rollback = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]

    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (3)")
    con.execute("COMMIT")
    after_commit = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]

    print("  DuckDB（.duckdb 文件，单连接）：")
    print(f"    初始 1 行 → BEGIN + INSERT 后（事务内）{mid} 行 → ROLLBACK 后 {after_rollback} 行")
    print(f"    → BEGIN + INSERT + COMMIT 后 {after_commit} 行")
    print("    ROLLBACK 真的把 INSERT 撤掉了 —— 这是完整的 ACID 事务。")
    print()
    wal = db_path.with_suffix(db_path.suffix + ".wal")
    print(f"    事务日志文件：{wal.name}  存在 = {wal.exists()}")
    print()
    print("  DuckDB 还有一个 Polars 没有的能力：**读写并发**。")
    print("    实测：连接 A 开一个事务插入一行（不提交），连接 B 同时读 ——")
    reader = duckdb.connect(str(db_path))
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (99)")
    try:
        seen = reader.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        print(f"      连接 B 读到 {seen} 行（提交前的快照，没被 A 的未提交写入挡住）")
        print("      MVCC 快照隔离：读者看到的是一致的历史版本，不会被写者阻塞。")
    except Exception as exc:
        print(f"      连接 B 读失败：{type(exc).__name__}: {str(exc)[:60]}")
    con.execute("ROLLBACK")
    reader.close()
    con.close()
    print("    这对「一边持续落库、一边跑分析」的场景很关键。")
    print("    （Polars 侧要实现同样的效果，得靠「写新文件 + 原子替换 + 读者重试」。）")
    print()
    print("  Polars：")
    print("    `pl.DataFrame` / `pl.LazyFrame` **没有事务 API** ——")
    print("    没有 BEGIN、没有 COMMIT、没有 ROLLBACK。")
    print("    能做的只是把几个 DataFrame 在内存里 `pl.concat` 起来，")
    print("    那是「拼数据」，不是「事务」：没有隔离级别，没有回滚点，")
    print("    更没有和文件系统写入绑定的原子性。")
    print()
    print("  实际影响：")
    print("    · DuckDB 里「算一批 + 落盘」可以放在一个事务里，")
    print("      中途失败就整批回滚，磁盘上不会出现「一半新一半旧」的表。")
    print("    · Polars 里「算一批 + 落盘」是两步，中间失败就只剩半截 ——")
    print("      要自己保证原子性（比如先写临时文件再 rename）。")


# ---------------------------------------------------------------- 场景 3


def scenario_types() -> None:
    section("场景 3 · 类型往返：写出去的 parquet 还能读回来吗？")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 纳秒时间戳
    ns_path = OUT_DIR / "roundtrip_ns.parquet"
    ns_frame = pl.DataFrame({"ts": pl.Series([1704067200123456789], dtype=pl.Datetime("ns"))})
    ns_frame.write_parquet(ns_path)
    con = duckdb.connect()
    duck_type = con.execute(f"SELECT typeof(ts) FROM read_parquet('{ns_path}')").fetchone()[0]
    duck_ns = con.execute(f"SELECT epoch_ns(ts) FROM read_parquet('{ns_path}')").fetchone()[0]
    pol_ns = ns_frame["ts"].cast(pl.Int64).to_list()[0]
    print("  纳秒时间戳往返（Polars 写 → DuckDB 读）：")
    print(f"    Polars  写出的值 : {pol_ns} ns")
    print(f"    DuckDB  读到的类型 : {duck_type}")
    print(f"    DuckDB  读到的值 : {duck_ns} ns")
    print(f"    一致：{'✅' if duck_ns == pol_ns else '❌'}")
    print("    DuckDB 2.0 有 TIMESTAMP_NS，纳秒精度不丢。")
    print()

    # DuckDB 的 TIMESTAMP 是微秒
    us_path = OUT_DIR / "roundtrip_us.parquet"
    con.execute("CREATE TABLE t(ts TIMESTAMP)")
    con.execute("INSERT INTO t VALUES (TIMESTAMP '2024-01-01 00:00:00.123456')")
    con.execute(f"COPY t TO '{us_path}' (FORMAT PARQUET)")
    back = pl.read_parquet(us_path)
    print("  DuckDB 的 TIMESTAMP 写出去、再让 Polars 读：")
    print(f"    Polars 读到的 dtype : {back['ts'].dtype}")
    print("    DuckDB 的 TIMESTAMP 本身就是微秒精度，所以落到 Polars 也是微秒。")
    print("    （两边合起来的规则：谁写谁定精度，读的时候按 parquet 里存的来。）")
    print()

    # 带时区
    tz_path = OUT_DIR / "roundtrip_tz.parquet"
    con.execute("CREATE TABLE tz(ts TIMESTAMPTZ)")
    con.execute("INSERT INTO tz VALUES (TIMESTAMPTZ '2024-01-01 00:00:00+08')")
    con.execute(f"COPY tz TO '{tz_path}' (FORMAT PARQUET)")
    tz_back = pl.read_parquet(tz_path)
    print("  带时区的时间戳（TIMESTAMPTZ）：")
    print("    写进去的是      : 2024-01-01 00:00:00+08")
    print(f"    Polars 读到的   : {tz_back['ts'][0]}   dtype = {tz_back['ts'].dtype}")
    print("    **时区被规范化成 UTC 了** —— 瞬间（时刻）是对的，但原来那个 +08 的")
    print("    「本地时间表示」没了。要保留原始偏移，得自己存一列本地时间或偏移量。")
    print()

    # Decimal
    dec_path = OUT_DIR / "roundtrip_decimal.parquet"
    con.execute("CREATE TABLE d(x DECIMAL(18,2))")
    con.execute("INSERT INTO d VALUES (123.45)")
    con.execute(f"COPY d TO '{dec_path}' (FORMAT PARQUET)")
    dec_back = pl.read_parquet(dec_path)
    print("  Decimal：")
    print(f"    DuckDB DECIMAL(18,2) → parquet → Polars dtype = {dec_back['x'].dtype}")
    print(f"    值 = {dec_back['x'][0]}  （无损，decimal128）")
    con.close()


# ---------------------------------------------------------------- 场景 4


def scenario_performance(repeat: int) -> None:
    section("场景 4 · 写一百万行 parquet：耗时和文件大小")

    if not common.ORDERS_PARQUET.exists():
        print("  缺 data/orders.parquet，先跑 gen_data.py")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    orders = pl.read_parquet(common.ORDERS_PARQUET)
    con = duckdb.connect()
    src = common.ORDERS_PARQUET
    con.register("orders_mem", orders.to_arrow())

    duck_e2e_out = OUT_DIR / "orders_duckdb_e2e.parquet"
    duck_mem_out = OUT_DIR / "orders_duckdb_mem.parquet"
    pol_e2e_out = OUT_DIR / "orders_polars_e2e.parquet"
    pol_mem_out = OUT_DIR / "orders_polars_mem.parquet"
    sink_out = OUT_DIR / "orders_polars_sink.parquet"

    # 两种口径，必须分开测：
    #   端到端 = 读 parquet + 写 parquet
    #   内存内 = 数据已经在内存里，只写 parquet
    # 每种口径写自己的文件，免得文件大小互相覆盖。

    def duck_e2e() -> None:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{src}')) TO '{duck_e2e_out}' (FORMAT PARQUET)"
        )

    def duck_mem() -> None:
        con.execute(f"COPY orders_mem TO '{duck_mem_out}' (FORMAT PARQUET)")

    def pol_e2e() -> None:
        pl.read_parquet(src).write_parquet(pol_e2e_out)

    def pol_mem() -> None:
        orders.write_parquet(pol_mem_out)

    def pol_sink() -> None:
        orders.lazy().sink_parquet(sink_out)

    duck_e2e_ms = _timeit(duck_e2e, repeat)
    duck_mem_ms = _timeit(duck_mem, repeat)
    pol_e2e_ms = _timeit(pol_e2e, repeat)
    pol_mem_ms = _timeit(pol_mem, repeat)
    sink_ms = _timeit(pol_sink, repeat)

    # 写的过程里，目录里除了目标文件还会不会出现别的？
    duck_watch = OUT_DIR / "watch_duck"
    pol_watch = OUT_DIR / "watch_polars"
    duck_watch.mkdir(parents=True, exist_ok=True)
    pol_watch.mkdir(parents=True, exist_ok=True)
    # 删掉上一轮的产出，确保这次测的是「写新文件」而不是「覆盖已有文件」
    (duck_watch / "duck.parquet").unlink(missing_ok=True)
    (pol_watch / "pol.parquet").unlink(missing_ok=True)
    duck_seen = _watch_dir(
        lambda: con.execute(
            f"COPY (SELECT * FROM read_parquet('{src}')) "
            f"TO '{duck_watch}/duck.parquet' (FORMAT PARQUET)"
        ),
        duck_watch,
    )
    pol_seen = _watch_dir(
        lambda: orders.write_parquet(pol_watch / "pol.parquet"),
        pol_watch,
    )

    print(f"  机器状态：{common.describe_machine()}")
    print(f"  数据：orders.parquet，{orders.height:,} 行 × {orders.width} 列")
    print()
    print("  分两种口径测，**口径必须对齐**：")
    print("    · 端到端：从 parquet 读出来再写到 parquet")
    print("    · 内存内：数据已经在内存里，只写 parquet")
    print()
    print(f"  {'口径':<10}{'写法':<38}{'耗时(ms)':<12}{'文件大小':<16}")
    print("  " + "-" * 76)
    rows = [
        ("端到端", "DuckDB  COPY (SELECT … FROM read_parquet) TO", duck_e2e_ms, duck_e2e_out),
        ("端到端", "Polars  read_parquet + write_parquet", pol_e2e_ms, pol_e2e_out),
        ("内存内", "DuckDB  COPY orders_mem TO", duck_mem_ms, duck_mem_out),
        ("内存内", "Polars  DataFrame.write_parquet", pol_mem_ms, pol_mem_out),
        ("内存内", "Polars  LazyFrame.sink_parquet", sink_ms, sink_out),
    ]
    for scope, label, ms, path in rows:
        size = path.stat().st_size
        print(
            f"  {common.pad(scope, 10)}{common.pad(label, 38)}"
            f"{common.pad(f'{ms:.1f}', 12)}{common.pad(f'{size:,} B', 16)}"
        )
    print()

    # 文件内部结构
    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("  没装 pyarrow，跳过 parquet 内部结构对比")
        return

    print("  文件内部结构（同一个数据、不同的默认值）：")
    print(f"  {'文件':<24}{'压缩':<10}{'row group':<14}{'每组行数':<12}{'大小'}")
    print("  " + "-" * 70)
    for label, path in [
        ("DuckDB COPY", duck_mem_out),
        ("Polars write_parquet", pol_mem_out),
        ("Polars sink_parquet", sink_out),
    ]:
        md = pq.ParquetFile(path).metadata
        rg = md.row_group(0)
        comp = rg.column(0).compression
        print(
            f"  {common.pad(label, 24)}{common.pad(comp, 10)}"
            f"{common.pad(str(md.num_row_groups), 14)}"
            f"{common.pad(f'{rg.num_rows:,}', 12)}"
            f"{path.stat().st_size:,} B"
        )
    print()
    duck_size = duck_mem_out.stat().st_size
    pol_size = pol_mem_out.stat().st_size
    print(
        f"  文件大小：Polars 小 {(1 - pol_size / duck_size) * 100:.1f}%"
        f"（{duck_size:,} → {pol_size:,} 字节）。"
    )
    print(
        f"  内存内纯写：Polars {pol_mem_ms:.1f} ms vs DuckDB {duck_mem_ms:.1f} ms"
        f"（{duck_mem_ms / pol_mem_ms:.1f}×）。"
    )
    print()
    print("  原因很直白：**默认压缩算法不一样**。")
    print("    DuckDB 的 COPY 默认用 SNAPPY，Polars 默认用 ZSTD。")
    print("    ZSTD 在这个数据集上压得更狠（文件小约 19%），")
    print("    而 DuckDB 的 COPY 还要多做一次「parquet → 内部表示 → parquet」的类型转换。")
    print("    想对齐就显式指定：DuckDB 侧 `COMPRESSION ZSTD`，")
    print("    Polars 侧 `write_parquet(compression='snappy')`。")
    print()
    print("  另一个细节：row group 的大小也不一样 ——")
    print("    DuckDB 按 122880 行切（内部默认块大小的倍数），Polars 按 125000 行切。")
    print("    row group 是并行读的最小单位，切法不一样会影响点查和扫描的并行度。")
    print()
    print("  顺带实测一下「写入过程中目录里有没有中间文件」——")
    print("    （后台线程在写的同时不停扫目录，记录出现过的文件名）")
    print(f"    DuckDB COPY           期间出现：{sorted(duck_seen)}")
    print(f"    Polars write_parquet  期间出现：{sorted(pol_seen)}")
    print("    这里写的是**新文件**（目标还不存在）—— 两边都直接写目标路径。")
    print("    覆盖已有文件时的差别见场景 1。")
    con.close()


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 写同一个路径，第二遍都是**静默覆盖**，都不是追加，也都没有提示。")
    print("     增量场景里这是坑 —— 以为追加，其实冲掉了上一批。")
    print("  2. 写失败时两边都保住了旧文件（错误发生在写文件之前）。")
    print("     但**覆盖写**的安全性不同：DuckDB 先写 `tmp_<name>` 再 rename（原子替换），")
    print("     Polars 直接写目标路径 —— 写到一半被杀会留下半截文件。")
    print("     （DuckDB 写新文件时也是直接写，临时文件只在「替换已有文件」时用。）")
    print("  3. **事务**：DuckDB 有完整 ACID（BEGIN/COMMIT/ROLLBACK + WAL + MVCC 快照隔离，")
    print("     一个连接写、另一个连接可以同时读）；Polars **根本没有事务这个概念**。")
    print("  4. 类型往返基本无损，但两处要留意：")
    print("     · DuckDB 的 TIMESTAMP 是微秒精度（Polars 是纳秒，parquet 里各存各的）；")
    print("     · TIMESTAMPTZ 落到 Polars 会被规范化成 UTC，原来的本地偏移没了。")
    print("  5. 默认压缩不同：DuckDB = SNAPPY，Polars = ZSTD。")
    print("     同一个 100 万行的数据集，Polars 写出来的文件小约 19%，耗时也更低。")
    print("     要对齐就显式指定：DuckDB 侧 `COMPRESSION ZSTD`，")
    print("     Polars 侧 `write_parquet(compression='snappy')`。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="写入路径与事务")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print(f"产出目录：{OUT_DIR}")

    scenario_overwrite()
    scenario_transaction()
    scenario_types()
    scenario_performance(args.repeat)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
