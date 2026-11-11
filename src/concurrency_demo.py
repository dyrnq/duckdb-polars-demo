"""并发 / 锁 / 多进程访问语义：DuckDB 是单写者模型，Polars 连锁都没有。

ETL 里最常见的事故之一是「多个进程同时往同一个 `.duckdb` 里写」。
这个 demo 讲清楚两边到底会怎样：

**DuckDB —— 一个文件一个写者，靠文件锁串行化**

    第二个进程碰同一个文件 → IOException:
      Could not set lock on file "...": Conflicting lock is held in
      /usr/bin/python3.13 (PID 1951917) by user bill.

    关键点：**`READ_ONLY` 绕不过这个锁。**
    别的进程正在写的时候，你用只读模式打开、或者 `ATTACH ... (READ_ONLY)`，
    一样是同一个 IOException —— 「我不写」不等于「我能读」。
    真正能共享的只有「两边都只读」。

    配套的还有两样东西：
      · **WAL**：进程被 kill 掉，`.wal` 留在那儿，下次打开自动回放，不丢数据。
      · **MVCC**：连接 1 没提交的 INSERT，连接 2 看不见。

**Polars —— 没有锁，也没有事务，只有一片文件系统**

    两个进程同时写同一个 parquet → **两个都成功**，没有报错、没有警告，
    也没有任何「谁覆盖了谁」的痕迹 —— 你只会拿到一份**不完整**的数据
    （整整一个分片没了），运气差的时候文件直接坏掉，连读都读不出来。
    写入期间读 → 要么 `ComputeError`，要么**直接 Rust panic**
    （`pyo3_runtime.PanicException`，连 `except Exception` 都接不住）。

所以两边的差别不是「谁更安全」，是**失败方式**：
    DuckDB 失败得很响（进程号都告诉你），Polars 失败得很安静（安静到你看不出来）。

用法：
    uv run python src/concurrency_demo.py
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# ---------------------------------------------------------------- 子进程脚本
#
# 这个 demo 的结论全靠「真的开另一个进程」才拿得到，
# 所以 worker 是一段独立的 Python 源码，跑的时候用 sys.executable 拉起来
# （不用 uv run —— 那会再套一层环境检查，慢而且没意义）。

# 拿锁的 worker：拿到锁就报一行「HELD <pid>」，然后按要求待着不动。
WORKER_LOCK = """
import os
import sys
import time

import duckdb

mode, db, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])

try:
    if mode == "connect_rw":
        con = duckdb.connect(db)
    elif mode == "connect_ro":
        con = duckdb.connect(db, read_only=True)
    elif mode == "attach_rw":
        con = duckdb.connect()
        con.execute("ATTACH '" + db + "' AS other")
    elif mode == "attach_ro":
        con = duckdb.connect()
        con.execute("ATTACH '" + db + "' AS other (READ_ONLY)")
    else:
        raise SystemExit("unknown mode: " + mode)
    print("HELD %d" % os.getpid(), flush=True)
    time.sleep(hold)
except Exception as exc:
    print("ERR %s: %s" % (type(exc).__name__, exc), flush=True)
"""

# 写完就自杀的 worker：模拟「机器被 kill、来不及 checkpoint」。
WORKER_CRASH = """
import os
import signal
import sys

import duckdb

db = sys.argv[1]
con = duckdb.connect(db)
con.execute("CREATE TABLE t AS SELECT i FROM range(100) t(i)")
con.execute("INSERT INTO t SELECT i FROM range(100,900) t(i)")
print("WROTE", flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""

# 并发写 parquet 的 worker：循环覆盖写同一个路径。
# 每个进程写**不同的行段**（ETL 里各写各的分片），这样数据丢失是可数的。
WORKER_POLARS_WRITE = """
import sys
import time

import polars as pl

path, marker, rounds, gap, rows, offset = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]),
    int(sys.argv[5]), int(sys.argv[6])
)
df = pl.DataFrame({"marker": [marker] * rows, "i": range(offset, offset + rows)})
for _ in range(rounds):
    df.write_parquet(path)
    time.sleep(gap)
print("OK", flush=True)
"""

# 写一半的 worker：先写一份完整的，再把文件截断成一半，停住不动等人来读。
WORKER_POLARS_HALF = """
import sys
import time

import polars as pl

path, hold = sys.argv[1], float(sys.argv[2])
df = pl.DataFrame({"marker": ["X"] * 1000, "i": range(1000)})
df.write_parquet(path)
with open(path, "rb") as f:
    data = f.read()
with open(path, "wb") as f:
    f.write(data[: len(data) // 2])
print("HALF", flush=True)
time.sleep(hold)
df.write_parquet(path)
print("OK", flush=True)
"""


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def run_worker(code: str, args: list[str], timeout: float = 60.0) -> tuple[str, str]:
    """跑一个 worker，返回 (第一行输出, 完整输出)。

    worker 的第一行是约定好的信号：
      「HELD <pid>」= 锁拿到了；「ERR ...」= 拿不到；「OK」= 正常做完。
    """
    proc = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (lines[0] if lines else ""), out


def spawn_holder(code: str, args: list[str]) -> tuple[subprocess.Popen[str], str]:
    """起一个会一直占着锁的 worker，等到它报「HELD」再返回。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    first = proc.stdout.readline().strip()
    return proc, first


def make_db(path: Path, rows: int = 100) -> None:
    """造一个带 .wal 的 .duckdb 文件。"""
    con = duckdb.connect(str(path))
    con.execute(f"CREATE TABLE t AS SELECT i FROM range({rows}) t(i)")
    con.execute(f"INSERT INTO t SELECT i FROM range({rows}, {rows * 3}) t(i)")
    con.close()


# ---------------------------------------------------------------- 场景 1


def scenario_lock_error(work: Path) -> None:
    section("场景 1 · DuckDB 的文件锁：第二个进程碰同一个文件会怎样")

    db = work / "orders.duckdb"
    make_db(db)

    print("  造好一个 .duckdb，让主进程先把它占住（读写模式）。")
    holder = duckdb.connect(str(db))
    print(f"  主进程 PID = {os.getpid()}，已经打开 {db.name}\n")

    print("  现在另一个进程来开同一个文件，各种方式都试一遍：")
    print()
    rows: list[tuple[str, str, str]] = []
    for label, mode in [
        ("读写 connect(db)", "connect_rw"),
        ("只读 connect(db, read_only=True)", "connect_ro"),
        ("ATTACH ... (读写)", "attach_rw"),
        ("ATTACH ... (READ_ONLY)", "attach_ro"),
    ]:
        first, _ = run_worker(WORKER_LOCK, [mode, str(db), "0"])
        if first.startswith("HELD"):
            verdict = "✅ 拿到了"
            detail = ""
        else:
            verdict = "❌ 被挡"
            detail = first[4:].split(": ", 1)[-1][:90]
        rows.append((label, verdict, detail))

    print("  " + common.pad("打开方式", 36) + common.pad("结果", 12) + "报错")
    print("  " + "-" * 74)
    for label, verdict, detail in rows:
        print("  " + common.pad(label, 36) + common.pad(verdict, 12) + detail)

    print()
    first, full = run_worker(WORKER_LOCK, ["connect_rw", str(db), "0"])
    print("  完整报错长这样：")
    for line in full.strip().splitlines():
        print(f"    {line}")

    m = re.search(r"Conflicting lock is held in .* \(PID (\d+)\)", full)
    if m:
        print()
        print(f"  → 报错里带着**持锁进程的 PID**（{m.group(1)}），还有用户名和文档链接。")
        print("    也就是说：抢不到锁的时候，你一眼就知道是谁占着 —— 这在排查线上事故时非常值钱。")

    holder.close()
    print()
    print("  → **注意「只读」那两行**：`connect(db, read_only=True)` 和")
    print("    `ATTACH ... (READ_ONLY)` 一样被挡 —— 「我不写」不等于「我能读」。")


# ---------------------------------------------------------------- 场景 2


def scenario_lock_matrix(work: Path, hold: float) -> None:
    section("场景 2 · 锁矩阵：到底什么能共享，什么不能")

    print("  把「谁在占锁」和「另一个进程想怎么开」交叉着测一遍：")
    print()

    combos: list[tuple[str, str, str, str]] = [
        ("读写", "读写", "connect_rw", "connect_rw"),
        ("读写", "只读", "connect_rw", "connect_ro"),
        ("读写", "ATTACH 只读", "connect_rw", "attach_ro"),
        ("读写", "ATTACH 读写", "connect_rw", "attach_rw"),
        ("只读", "读写", "connect_ro", "connect_rw"),
        ("只读", "只读", "connect_ro", "connect_ro"),
        ("只读", "ATTACH 只读", "connect_ro", "attach_ro"),
        ("只读", "ATTACH 读写", "connect_ro", "attach_rw"),
    ]

    print(
        "  " + common.pad("进程 A", 10) + common.pad("进程 B", 16) + common.pad("结果", 10) + "说明"
    )
    print("  " + "-" * 74)
    for a_label, b_label, a_mode, b_mode in combos:
        db = work / f"m_{a_mode}_{b_mode}.duckdb"
        make_db(db, rows=10)
        proc, first = spawn_holder(WORKER_LOCK, [a_mode, str(db), str(hold)])
        assert first.startswith("HELD"), f"持锁进程没拿到锁: {first}"
        try:
            b_first, _ = run_worker(WORKER_LOCK, [b_mode, str(db), "0"])
        finally:
            proc.terminate()
            proc.wait(timeout=30)

        if b_first.startswith("HELD"):
            verdict = "✅ 能开"
            note = "两边都只读 → 共享锁，不冲突"
        else:
            verdict = "❌ 冲突"
            note = "有人要写 → 独占锁，别人全挡住"
        print(
            "  "
            + common.pad(a_label, 10)
            + common.pad(b_label, 16)
            + common.pad(verdict, 10)
            + note
        )

    print()
    print("  → 规则一句话：**只要有一边想写，就上独占锁；全只读才共享。**")
    print("    这就是 DuckDB 的「单写者模型」—— 同一时刻一个文件只有一个写者。")
    print("    同一个进程里开两个连接不算「两个写者」，那是同一个写者。")

    # 同进程的两个连接
    db = work / "same_process.duckdb"
    make_db(db, rows=10)
    con1 = duckdb.connect(str(db))
    con2 = duckdb.connect(str(db))
    n = con2.execute("SELECT count(*) FROM t").fetchone()[0]
    print()
    print(f"  同一个进程开两个连接写同一个文件 → 都成功，第二个读到 {n} 行。")
    con1.close()
    con2.close()


# ---------------------------------------------------------------- 场景 3


def scenario_same_file_twice(work: Path) -> None:
    section("场景 3 · 同一个文件不能 ATTACH 两次（哪怕别名不同）")

    db = work / "twice.duckdb"
    make_db(db, rows=10)

    con = duckdb.connect()
    con.execute(f"ATTACH '{db}' AS sales")
    print("  第一次 ATTACH 成功（别名 sales）。")
    print()
    for alias, extra in [("warehouse", ""), ("sales", "")]:
        try:
            con.execute(f"ATTACH '{db}' AS {alias}{extra}")
            print(f"  再 ATTACH 一次（别名 {alias}）→ ✅ 居然成功了")
        except Exception as exc:
            print(f"  再 ATTACH 一次（别名 {alias}）→ {type(exc).__name__}:")
            print(f"    {str(exc).splitlines()[0]}")
    con.close()

    print()
    print("  → DuckDB 对同一个文件只保留**一个 handle**。")
    print("    所以「先 ATTACH 读写、同一进程再 ATTACH 只读看看」这种写法是行不通的 ——")
    print("    这跟场景 2 里「另一个进程只读打开」被挡，是同一个根因。")

    print()
    print("  附：同进程内两个 connect 却是可以的（场景 2 末尾测过）——")
    print("  因为 DuckDB 认出「同一个进程」，共享同一份内存里的实例。")


# ---------------------------------------------------------------- 场景 4


def scenario_wal(work: Path) -> None:
    section("场景 4 · WAL 与崩溃恢复：进程被 kill 了，数据还在")

    db = work / "crash.duckdb"
    wal = work / "crash.duckdb.wal"

    print("  起一个子进程：写 100 行、再插 800 行，然后 SIGKILL 自己")
    print("  （模拟机器被 kill —— 来不及 checkpoint，WAL 就留在盘上）。")
    print()
    first, _ = run_worker(WORKER_CRASH, [str(db)], timeout=60)
    print(f"  子进程最后一句：{first}")
    print()
    print(
        f"  .wal 文件还在吗：{'在' if wal.exists() else '不在'}"
        + (f"（{wal.stat().st_size} 字节）" if wal.exists() else "")
    )

    con = duckdb.connect(str(db))
    rows = con.execute("SELECT count(*) FROM t").fetchone()[0]
    print(f"  下次打开这个文件 —— 自动回放 WAL，读到 {rows} 行。")
    con.close()
    print(f"  回放之后 .wal 还在吗：{'在' if wal.exists() else '不在（已并进主文件）'}")
    print()
    print("  → **没丢数据。** 这是 WAL 的全部意义：")
    print("    写出去的要么在主文件里，要么在 WAL 里，下次打开一定在。")

    # 正常退出 → 自动 checkpoint → 不留 WAL
    db2 = work / "clean.duckdb"
    con2 = duckdb.connect(str(db2))
    con2.execute("CREATE TABLE t AS SELECT 1")
    con2.execute("INSERT INTO t SELECT 2")
    con2.close()
    print()
    print(f"  对照：正常退出（没被 kill）→ .wal 存在吗：{(work / 'clean.duckdb.wal').exists()}")
    print("    正常关闭时 DuckDB 会自动 checkpoint，把 WAL 并进主文件再删掉。")

    # disable_checkpoint_on_shutdown
    db3 = work / "nockpt.duckdb"
    con3 = duckdb.connect(str(db3))
    con3.execute("SET checkpoint_threshold = '1GB'")
    con3.execute("PRAGMA disable_checkpoint_on_shutdown")
    con3.execute("CREATE TABLE t2 AS SELECT 1")
    con3.execute("INSERT INTO t2 SELECT 2")
    con3.close()
    wal3 = work / "nockpt.duckdb.wal"
    print()
    print(
        f"  `PRAGMA disable_checkpoint_on_shutdown` + 正常退出 → .wal 存在吗："
        f"{wal3.exists()}" + (f"（{wal3.stat().st_size} 字节）" if wal3.exists() else "")
    )
    con4 = duckdb.connect(str(db3))
    n3 = con4.execute("SELECT count(*) FROM t2").fetchone()[0]
    con4.close()
    print(f"  下次打开照样读到 {n3} 行，回放完 WAL 才消失。")
    print()
    print("  → 这个 pragma 是给「批量写完之后自己控制 checkpoint 时机」用的。")


# ---------------------------------------------------------------- 场景 5


def scenario_isolation(work: Path) -> None:
    section("场景 5 · 事务隔离：没提交的数据，别人看不见")

    db = work / "mvcc.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t AS SELECT i FROM range(10) t(i)")

    other = duckdb.connect(str(db))
    print("  连接 1 开一个事务，插一行 i = 99999，但不提交。")
    con.execute("BEGIN TRANSACTION")
    con.execute("INSERT INTO t SELECT 99999")

    seen = other.execute("SELECT count(*) FROM t WHERE i = 99999").fetchone()[0]
    total = other.execute("SELECT count(*) FROM t").fetchone()[0]
    print(f"  连接 2 读到那行了吗：{seen} 行（看不见就是 0）")
    print(f"  连接 2 读到总行数：{total} 行（还是事务开始前的 10 行）")

    con.execute("ROLLBACK")
    after = other.execute("SELECT count(*) FROM t").fetchone()[0]
    print(f"  连接 1 回滚后，连接 2 读到总行数：{after} 行")

    con.execute("BEGIN TRANSACTION")
    con.execute("INSERT INTO t SELECT 99999")
    con.execute("COMMIT")
    after2 = other.execute("SELECT count(*) FROM t").fetchone()[0]
    seen2 = other.execute("SELECT count(*) FROM t WHERE i = 99999").fetchone()[0]
    print(f"  连接 1 提交后，连接 2 读到总行数：{after2} 行，那行：{seen2} 行")

    con.close()
    other.close()
    print()
    print("  → DuckDB 是 **MVCC**：读到的永远是「某一时刻的快照」。")
    print("    提交之前别人看不见，提交之后才看得见 —— 两边都不需要显式加锁。")
    print()
    print("  → **Polars 没有这一层。** DataFrame 是一次性的物化结果，")
    print("    没有「事务」「未提交」「隔离级别」这些概念；")
    print("    也没有一个「正在写」的表可以被别人读 —— 写就写文件，读就读文件。")


# ---------------------------------------------------------------- 场景 6


def scenario_polars(work: Path) -> None:
    section("场景 6 · Polars 侧：没有锁，只有文件")

    pq = work / "shared.parquet"

    print("  场景 A：两个进程同时覆盖写同一个 parquet 路径。")
    print("  模拟 ETL 的实际情况：worker A 写第 0~99 行，worker B 写第 100~199 行，")
    print("  两边循环覆盖写同一路径。")
    print()
    procs = []
    for marker, offset in (("A", 0), ("B", 100)):
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    WORKER_POLARS_WRITE,
                    str(pq),
                    marker,
                    "40",
                    "0.01",
                    "100",
                    str(offset),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        outs.append((p.returncode, out.strip()))

    for marker, (rc, out) in zip(("A", "B"), outs, strict=True):
        last = out.splitlines()[-1] if out.splitlines() else "(无输出)"
        print(f"    写 {marker} 段（{marker} 写 100 行）的进程：退出码 {rc}，最后一句：{last}")

    print()
    print("  → **两个进程都成功（退出码 0），没有一个人报错。**")
    print()
    # 最终文件有两种结局，都很糟：
    #   「读得出、但行数少一半」 —— 后写的那个整段把前一个覆盖了；
    #   「根本读不出」           —— 两个写进程的内容在文件里交错，footer 是碎的。
    # 到底落在哪一边取决于交错的具体时机，所以这里两种都如实报出来。
    try:
        final = pl.read_parquet(pq)
    except Exception as exc:
        print(
            f"    最终文件：**读不出来** —— {type(exc).__name__}: {str(exc).splitlines()[0][:110]}"
        )
        print("    文件本身被写坏了（两个写进程的内容在文件里交错，footer 是碎的）。")
        print()
        print("    本该有 200 行（A 段 100 行 + B 段 100 行），实际连读都读不出来 ——")
        print("    这次不是「少了一半」，是**整个文件报废**。")
        print("    而两个写进程**都认为自己成功了**（退出码 0）。")
    else:
        markers = sorted(final["marker"].unique().to_list())
        print(f"    最终文件：读得出来，{final.height} 行，标记 {markers}")
        print(f"    本该有 200 行（A 段 100 行 + B 段 100 行），实际只有 {final.height} 行 ——")
        print("    **整整一个分片没了**，而且没有异常、没有警告、没有任何「谁覆盖了谁」的痕迹。")
        print("    你只会拿到一份**不完整**的数据，而且不知道少了什么。")
    print()
    print("    （另一种结局同样存在：两个写进程交错写，文件直接坏掉，读都读不出来。")
    print("      这一段是竞态，落在哪一边取决于交错时机 —— 上面印的是本次实测值。）")
    print()
    print("    对照 DuckDB 的场景 1：第二个进程直接 IOException，")
    print("    报错里还带着持锁进程的 PID —— **失败得很响**。")

    print()
    print("  场景 B：一个进程在写，另一个进程同时读。")
    print()

    # B1 —— 确定性的那一半：文件确定是「写了一半」的状态。
    pq2 = work / "half_written.parquet"
    half_proc = subprocess.Popen(
        [sys.executable, "-c", WORKER_POLARS_HALF, str(pq2), "4"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert half_proc.stdout is not None
    half_proc.stdout.readline()  # 等它报 HALF —— 此时文件正好是截断的
    print("    B1：让写进程把文件截断成一半，停住不动，再来读。")
    try:
        _ = pl.read_parquet(pq2).height
        print("        读到 → 居然没报错？")
    except Exception as exc:
        print(f"        读到 → {type(exc).__name__}: {str(exc)[:130]}")
    half_proc.communicate(timeout=60)
    print(f"        等它写完再读 → {pl.read_parquet(pq2).height} 行（恢复正常）")
    print()
    print("    → **这是确定性的**：parquet 的 footer（头尾那几 KB）在写完之后才补齐，")
    print("      写一半的文件根本不是合法的 parquet —— 读的人必然报错。")

    # B2 —— 竞态的那一半：文件正在被覆盖写，比例随机器负载浮动。
    pq3 = work / "writing.parquet"
    pl.DataFrame({"marker": ["X"] * 200_000, "i": range(200_000)}).write_parquet(pq3)
    print("    B2：写进程正在反复覆盖写这个文件，读进程同时连读 40 次。")
    print("        （这一段是竞态，失败比例随机器负载浮动 —— 下面印的是本次实测值。）")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            WORKER_POLARS_WRITE,
            str(pq3),
            "Y",
            "60",
            "0.005",
            "200000",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(0.3)
    ok = 0
    err = 0
    panics = 0
    kinds: dict[str, int] = {}
    last_err = ""
    for _ in range(40):
        try:
            _ = pl.read_parquet(pq3).height
            ok += 1
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001
            # 这里故意用 BaseException：Polars 有一类失败是 Rust panic
            # （pyo3_runtime.PanicException），它**不是** Exception 的子类，
            # 普通的 `except Exception` 接不住 —— 这一条本身就是结论的一部分。
            err += 1
            name = type(exc).__name__
            kinds[name] = kinds.get(name, 0) + 1
            if not isinstance(exc, Exception):
                panics += 1
            last_err = f"{name}: {str(exc)[:110]}"
    proc.communicate(timeout=120)
    print(f"        结果：成功 {ok} 次，失败 {err} 次")
    print(f"        失败的类型分布：{kinds}")
    if last_err:
        print(f"        失败的样子：{last_err}")
    print()
    if panics:
        print(f"    → 其中 **{panics} 次是 Rust panic**（`pyo3_runtime.PanicException`），")
        print("      它**不是 `Exception` 的子类** —— `except Exception` 接不住，")
        print("      普通的错误处理会直接漏过去。stderr 上还会打一段 Rust 的：")
        print("        thread 'async-executor-N' panicked at crates/polars-io/.../byte_source.rs:")
        print("        assertion failed: range.end as u64 <= self.size")
        print()
    print("    → **没有锁，所以读到半截文件是常态。**")
    print("      而 DuckDB 这边，写进程持锁期间读进程会拿到明确的 IOException。")
    print("      不是「偶尔读到坏数据」，是**每次都明确失败** —— 这是两种失败方式的重要区别。")

    print()
    print("  → Polars 的正确做法（也是本项目 `write_demo.py` 讲的）：")
    print("    **先写临时文件，写完再原子替换** —— 读的人永远看到完整的旧版本或新版本，")
    print("    永远不会看到半截。")
    print("    但这是**你自己要写的事**：Polars 不会替你做，")
    print("    也没有任何机制提醒你没做。")


# ---------------------------------------------------------------- 场景 7


def scenario_conclusion() -> None:
    section("小结：并发的两套模型")

    print("  " + common.pad("维度", 22) + common.pad("DuckDB", 26) + "Polars")
    print("  " + "-" * 74)
    rows = [
        ("多个进程写同一个文件", "第二个 IOException（带持锁 PID）", "两个都成功，数据悄悄少一半"),
        ("只读连接能不能插队", "❌ 不能，同样被锁挡", "没有锁，随便读（可能读到半截）"),
        ("写一半的文件被读到", "读不到（写者持锁）", "ComputeError，偶尔 Rust panic"),
        ("崩溃后数据会丢吗", "不会，WAL 自动回放", "会，文件是半截的"),
        ("事务 / 隔离级别", "有（MVCC，看得见快照）", "没有"),
        ("要自己保证原子写", "不需要", "**需要**（写临时文件 + 原子替换）"),
    ]
    for dim, duck, pol in rows:
        print("  " + common.pad(dim, 22) + common.pad(duck, 26) + pol)

    print()
    print("  → 一句话：**DuckDB 用「拒绝」换正确，Polars 用「不设防」换灵活。**")
    print()
    print("  ETL 里多进程抢写同一个 .duckdb 的正确姿势：")
    print("    ① 一个进程写、别的进程等 —— 别并发写；")
    print("    ② 要并行，就各写各的文件，最后由一个进程合并；")
    print("    ③ 只读的进程可以用 `ATTACH ... (READ_ONLY)`，")
    print("       但**前提是没有别的进程正在写** —— 这一条最容易踩。")
    print()
    print("  Polars 侧的对应做法：")
    print("    · 每个进程写自己的一份 parquet，天然并行、互不干扰；")
    print("    · 要合并就读目录（`scan_parquet('dir/*.parquet')`，见 `partition_demo.py`）；")
    print("    · **永远不要让两个进程写同一个路径** —— 实测不会报错，")
    print("      只会让你的数据悄悄少一半（运气差时文件直接坏掉）。")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="并发 / 锁 / 多进程访问语义")
    parser.add_argument(
        "--hold",
        type=float,
        default=3.0,
        help="持锁进程待多久（秒）。默认 3。",
    )
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__}")
    print(f"机器：{common.describe_machine()}")
    print(f"临时目录：{tempfile.gettempdir()}")

    work = Path(tempfile.mkdtemp(prefix="concurrency_demo_"))
    print(f"实验目录：{work}")

    try:
        scenario_lock_error(work)
        scenario_lock_matrix(work, args.hold)
        scenario_same_file_twice(work)
        scenario_wal(work)
        scenario_isolation(work)
        scenario_polars(work)
        scenario_conclusion()
    finally:
        # 用完即弃，但只删自己建的那个目录。
        for path in sorted(work.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        work.rmdir()

    print()
    print("  （实验目录已清理）")


if __name__ == "__main__":
    main()
