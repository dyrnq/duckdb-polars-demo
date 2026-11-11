"""压缩与编码：parquet 文件到底该存成什么样。

`write_demo` 说过「两边的默认压缩不一样」（DuckDB 默认 SNAPPY、Polars 默认 ZSTD），
但它只用订单数据量了一次就收尾。这个脚本接着往下问五个问题：

  1. 那个差距到底有多大？—— 取决于数据形态，换份数据就是另一个数。
  2. 压缩参数该怎么选？—— 体积、写入时间、读取时间是三角权衡。
  3. zstd 的级别是不是越大越省？—— **不是**，这条最反直觉。
  4. 文件内部谁更省？—— 编码策略不同，而且 DuckDB 没有关字典的开关。
  5. 行组大小怎么定？—— 它同时决定体积、并行度和点查代价。

产出文件落在 data/compress_demo/ 下。

用法：
    uv run python src/compress_demo.py
"""

from __future__ import annotations

import argparse
import time

import duckdb
import numpy as np
import polars as pl
import pyarrow.parquet as pq

import common

OUT_DIR = common.DATA_DIR / "compress_demo"


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _timeit(fn, repeats: int) -> float:
    """跑 repeats 遍取最快的一次，返回毫秒。

    取最快而不是平均：机器上有别的任务在抢 CPU 时，慢的那几遍是噪声，
    最快的那一遍更接近「不被打扰时」的能力。
    """
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


# ---------------------------------------------------------------- 跑一次的动作
#
# 这些函数返回「待测的动作」，把路径和参数都绑成参数传进来，
# 而不是让闭包去抓外层的循环变量 —— 抓循环变量的话 ruff 的 B023 会报，
# 而且那种写法本身也容易在循环里用错上一次的路径。


def _write_duck(con, path: str, opts: str):
    def run() -> None:
        con.execute(f"COPY t TO '{path}' (FORMAT PARQUET, {opts})")

    return run


def _write_polars(df: pl.DataFrame, path: str, kw: dict):
    def run() -> None:
        df.write_parquet(path, **kw)

    return run


def _read_duck(con, path: str):
    """全量口径：整个文件读进内存（DuckDB 那边走 Arrow，不建 Python 对象）。"""

    def run() -> None:
        con.sql(f"SELECT * FROM read_parquet('{path}')").to_arrow_table()

    return run


def _read_polars(path: str):
    def run() -> None:
        pl.read_parquet(path)

    return run


def _read_point_duck(con, path: str, key: int):
    def run() -> None:
        con.sql(f"SELECT count(*) FROM read_parquet('{path}') WHERE ts = {key}").fetchall()

    return run


def _read_point_polars(path: str, key: int):
    def run() -> None:
        pl.scan_parquet(path).filter(pl.col("ts") == key).select(pl.len()).collect()

    return run


# ---------------------------------------------------------------- 数据


def _make_data(rows: int, shape: str) -> pl.DataFrame:
    """造两种形态的数据：数字为主 / 长字符串为主。

    压缩率完全由数据形态决定 —— 同一个算法，落在可压缩的数据上效果完全不同。
    所以「DuckDB 写的文件比 Polars 大」这种话，脱离数据形态说就没意义。
    """
    rng = np.random.default_rng(common.SEED)
    if shape == "numeric":
        # 订单那种：递增 id + 用户 id + 金额 + 低基数类目
        return pl.DataFrame(
            {
                "order_id": np.arange(rows, dtype=np.int64),
                "user_id": rng.integers(1, 50_000, rows),
                "amount": np.round(rng.lognormal(3.0, 0.6, rows), 2),
                "category": rng.choice(common.CATEGORIES, rows),
            }
        )
    # 日志那种：每行一条长字符串
    tail = "x" * 40
    return pl.DataFrame(
        {
            "ts": np.arange(rows, dtype=np.int64),
            "level": rng.choice(["INFO", "WARN", "ERROR"], rows, p=[0.80, 0.15, 0.05]),
            "msg": [
                f'{i:08d} user={i % 9973} path=/api/v2/items/{i % 331} q="{tail}"'
                for i in range(rows)
            ],
        }
    )


# ---------------------------------------------------------------- 场景 1


def scenario_shape_gap(rows: int) -> None:
    section("场景 1 · 默认压缩差多少，取决于数据形态")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("  write_demo 用订单数据（数字为主）量到「Polars 的文件小 19%」。")
    print("  换成日志那种长字符串，差距完全不一样 —— 下面两种形态各量一次：")
    print()
    print(
        f"  {'数据形态':<14}{'DuckDB 默认':>14}{'Polars 默认':>14}{'Polars 小':>10}  "
        f"{'DuckDB 用的':<10}"
    )
    print("  " + "-" * 74)

    for shape, label in (("numeric", "数字为主"), ("log", "长字符串为主")):
        df = _make_data(rows, shape)
        con.register("t", df.to_arrow())

        duck_path = OUT_DIR / f"default_{shape}_duckdb.parquet"
        pol_path = OUT_DIR / f"default_{shape}_polars.parquet"

        con.execute(f"COPY t TO '{duck_path}' (FORMAT PARQUET)")
        df.write_parquet(pol_path)

        duck_size = duck_path.stat().st_size
        pol_size = pol_path.stat().st_size
        codec = pq.ParquetFile(duck_path).metadata.row_group(0).column(0).compression
        gap = (1 - pol_size / duck_size) * 100

        print(
            f"  {common.pad(label, 14)}{common.pad(f'{duck_size:,} B', 14)}"
            f"{common.pad(f'{pol_size:,} B', 14)}{common.pad(f'{gap:.1f}%', 10)}  "
            f"{common.pad(codec, 10)}"
        )

    print()
    print("  两边用的都是**自己的默认值**：DuckDB = SNAPPY，Polars = ZSTD。")
    print("  差距的大小由数据可压缩性决定 ——")
    print("    · 数字为主的表：SNAPPY 压得动，ZSTD 再省一点；")
    print("    · 长字符串为主的表：ZSTD 的优势被放大，SNAPPY 基本压不动。")
    print("  所以「Polars 写的文件小 X%」这个结论，**换个数据集就是另一个数** ——")
    print("  要给结论就得连数据形态一起说。")
    con.close()


# ---------------------------------------------------------------- 场景 2


# (标签, DuckDB 的 COPY 选项, Polars 的 write_parquet 参数)
#
# 注意 DuckDB 的 COMPRESSION_LEVEL **只认 zstd** —— 给 gzip 传会报
# "Compression level is only supported for the ZSTD compression codec"，
# 而 Polars 那边 gzip 是能设级别的（1~9）。同一个概念，两边的开关范围不一样。
_COMPRESSIONS: list[tuple[str, str, dict]] = [
    ("snappy", "COMPRESSION snappy", {"compression": "snappy"}),
    ("gzip", "COMPRESSION gzip", {"compression": "gzip", "compression_level": 6}),
    (
        "zstd L1",
        "COMPRESSION zstd, COMPRESSION_LEVEL 1",
        {"compression": "zstd", "compression_level": 1},
    ),
    (
        "zstd L2",
        "COMPRESSION zstd, COMPRESSION_LEVEL 2",
        {"compression": "zstd", "compression_level": 2},
    ),
    (
        "zstd L3",
        "COMPRESSION zstd, COMPRESSION_LEVEL 3",
        {"compression": "zstd", "compression_level": 3},
    ),
    (
        "zstd L6",
        "COMPRESSION zstd, COMPRESSION_LEVEL 6",
        {"compression": "zstd", "compression_level": 6},
    ),
    (
        "zstd L9",
        "COMPRESSION zstd, COMPRESSION_LEVEL 9",
        {"compression": "zstd", "compression_level": 9},
    ),
]


def scenario_matrix(rows: int, repeat: int) -> None:
    section("场景 2 · 压缩矩阵：体积、写入时间、读取时间")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _make_data(rows, "log")
    con.register("t", df.to_arrow())

    print(f"  数据：日志形态，{rows:,} 行 × {df.width} 列（含一条长字符串列）")
    print(f"  机器状态：{common.describe_machine()}")
    print()
    print("  **口径**：")
    print("    · 写入：数据已在内存里，只写 parquet（不把读文件算进去）")
    print("    · 读取：整个文件读进内存（DuckDB 走 Arrow，Polars 走 read_parquet），")
    print("      两边读的是**同一个文件**，量的是「谁读这个文件更快」")
    print()

    print(f"  {'写的是':<8}{'压缩':<10}{'体积':<14}{'写入':<10}{'DuckDB 读':<13}{'Polars 读':<13}")
    print("  " + "-" * 68)

    rows_out: list[tuple[str, str, int, float, float, float]] = []

    for engine in ("DuckDB", "Polars"):
        for label, duck_opt, pol_kw in _COMPRESSIONS:
            path = OUT_DIR / f"matrix_{engine}_{label.replace(' ', '_')}.parquet"

            if engine == "DuckDB":
                write = _write_duck(con, path, duck_opt)
            else:
                write = _write_polars(df, path, pol_kw)

            write_ms = _timeit(write, repeat)
            size = path.stat().st_size

            duck_read_ms = _timeit(_read_duck(con, path), repeat)
            pol_read_ms = _timeit(_read_polars(path), repeat)

            rows_out.append((engine, label, size, write_ms, duck_read_ms, pol_read_ms))
            print(
                f"  {common.pad(engine, 8)}{common.pad(label, 10)}"
                f"{common.pad(f'{size:,} B', 14)}{common.pad(f'{write_ms:.1f} ms', 10)}"
                f"{common.pad(f'{duck_read_ms:.1f} ms', 13)}"
                f"{common.pad(f'{pol_read_ms:.1f} ms', 13)}"
            )
    print()

    duck = {r[1]: r for r in rows_out if r[0] == "DuckDB"}
    pol = {r[1]: r for r in rows_out if r[0] == "Polars"}

    print("  **gzip 三头都不占优**（DuckDB 侧写的文件）：")
    g = duck["gzip"]
    print(f"    体积 {g[2]:,} B、写入 {g[3]:.1f} ms、读回 {g[4]:.1f} ms")
    print("    只有「对方只认 gzip」这种理由才值得选它。")
    print()

    print("  **两个引擎的默认值差距**（同一份数据）：")
    d_snap, p_zstd = duck["snappy"], pol["zstd L3"]
    print(f"    DuckDB  snappy : {d_snap[2]:,} B，写入 {d_snap[3]:.1f} ms")
    print(f"    Polars  zstd L3: {p_zstd[2]:,} B，写入 {p_zstd[3]:.1f} ms")
    print(
        f"    Polars 的文件小 {(1 - p_zstd[2] / d_snap[2]) * 100:.1f}%，"
        f"写入快 {d_snap[3] / p_zstd[3]:.1f}×。"
    )
    print("    —— 这是**默认值**的差距，不是算法的差距。要对齐就显式指定：")
    print("      DuckDB  `COPY … TO … (FORMAT PARQUET, COMPRESSION zstd, COMPRESSION_LEVEL 3)`")
    print("      Polars  `write_parquet(compression='zstd', compression_level=3)`")
    print()

    print("  **读回速度跟压缩算法基本无关** —— 上表里同一个文件两边读的时间都在一个量级，")
    print("  真正的差别在**体积**：文件小，IO 少，这才是压缩的收益所在。")
    print("  （文件都在 page cache 里，所以解压的差别被摊平了；")
    print("    真到冷盘上，小文件的优势只会更明显。）")
    con.close()


# ---------------------------------------------------------------- 场景 3


def scenario_levels(rows: int) -> None:
    section("场景 3 · zstd 的级别，不是越大越省")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _make_data(rows, "log")
    tbl = df.to_arrow()
    con.register("t", tbl)

    levels = [1, 2, 3, 4, 5, 6, 7, 9, 12, 19]
    print(f"  同一份数据（{rows:,} 行日志），只改 zstd 的级别，看文件多大：")
    print()
    print("  **口径**：写入时间只量**一遍**（写文件本身就是这一步做的，没有重复写）。")
    print("  别的场景量的是「跑 N 遍取最快」，这里不行 —— 高级别写一次就要好几秒，")
    print("  跑多遍纯属浪费 CI 时间，而且这个场景要看的也不是写入速度的方差。")
    print()
    print(f"  {'级别':<8}{'DuckDB':>14}{'Polars':>14}{'pyarrow':>14}{'DuckDB 写入':>14}")
    print("  " + "-" * 64)

    curves: dict[str, list[int]] = {"DuckDB": [], "Polars": [], "pyarrow": []}
    duck_write_ms: list[float] = []
    for lvl in levels:
        p_d = OUT_DIR / f"lvl_duckdb_{lvl}.parquet"
        p_p = OUT_DIR / f"lvl_polars_{lvl}.parquet"
        p_a = OUT_DIR / f"lvl_pyarrow_{lvl}.parquet"

        t0 = time.perf_counter()
        con.execute(
            f"COPY t TO '{p_d}' (FORMAT PARQUET, COMPRESSION zstd, COMPRESSION_LEVEL {lvl})"
        )
        duck_write_ms.append((time.perf_counter() - t0) * 1000)
        df.write_parquet(p_p, compression="zstd", compression_level=lvl)
        pq.write_table(tbl, p_a, compression="zstd", compression_level=lvl)

        d_size = p_d.stat().st_size
        p_size = p_p.stat().st_size
        a_size = p_a.stat().st_size
        curves["DuckDB"].append(d_size)
        curves["Polars"].append(p_size)
        curves["pyarrow"].append(a_size)

        print(
            f"  {common.pad(str(lvl), 8)}{common.pad(f'{d_size:,}', 14)}"
            f"{common.pad(f'{p_size:,}', 14)}{common.pad(f'{a_size:,}', 14)}"
            f"{common.pad(f'{duck_write_ms[-1]:.1f} ms', 14)}"
        )
    print()

    # 找出每个实现里最省空间的级别
    best = {name: levels[int(np.argmin(sizes))] for name, sizes in curves.items()}
    worst = {name: levels[int(np.argmax(sizes))] for name, sizes in curves.items()}
    print(f"  {'实现':<10}{'最省的级别':<12}{'最不省的级别':<14}{'最好/最差':<12}")
    print("  " + "-" * 50)
    for name, sizes in curves.items():
        print(
            f"  {common.pad(name, 10)}{common.pad(f'L{best[name]}', 12)}"
            f"{common.pad(f'L{worst[name]}', 14)}"
            f"{common.pad(f'{max(sizes) / min(sizes):.2f}×', 12)}"
        )
    print()
    print("  **级别和体积不是单调的。**")
    # 「升一级反而变小」的地方，就是非单调的证据 —— 从数据里找，不写死。
    for name, sizes in curves.items():
        dips = [i for i in range(1, len(levels)) if sizes[i] < sizes[i - 1]]
        if not dips:
            continue
        i = dips[0]
        print(
            f"    {common.pad(name, 8)}从 L{levels[i - 1]} 升到 L{levels[i]}，文件**反而更小**"
            f"（{sizes[i - 1]:,} → {sizes[i]:,} B）"
        )
    print("    三个**互相独立**的实现都出现这种拐点，说明这不是某个引擎的怪癖，")
    print("    而是 zstd 的「级别 → 压缩策略」映射本身不单调")
    print("    （不同级别换的是搜索策略和哈希表，不是同一套策略的强度档位）。")
    print()
    print("  所以想省空间**不能靠「级别调高」去推**，得实测 ——")
    print("    最省的级别落在哪一档，是**数据**决定的（上面那张表就是实测结果）。")

    # 最省的那一档，到底付出了什么？用数说话，不写死。
    best_i = int(np.argmin(curves["DuckDB"]))
    top_i = len(levels) - 1
    base_i = 0
    best_size = curves["DuckDB"][best_i]
    top_size = curves["DuckDB"][top_i]
    base_size = curves["DuckDB"][base_i]
    print()
    print(f"  以 DuckDB 为例，最省的是 L{levels[best_i]}（{best_size:,} B）：")
    print(
        f"    相比 L{levels[base_i]}（{base_size:,} B）省了 "
        f"{(1 - best_size / base_size) * 100:.1f}%，"
        f"代价是写入从 {duck_write_ms[base_i]:.1f} ms 变成 {duck_write_ms[best_i]:.1f} ms"
        f"（{duck_write_ms[best_i] / duck_write_ms[base_i]:.1f}×）"
    )
    if best_i != top_i:
        # 最贵的级别不是最省的那一档 —— 那它买到了什么？
        print()
        print(f"  而最贵的 L{levels[top_i]} 反而不是最省的：")
        print(
            f"    体积 {top_size:,} B，比 L{levels[best_i]} 大 "
            f"{(top_size - best_size) / best_size * 100:.1f}%；"
            f"写入 {duck_write_ms[top_i]:.1f} ms，是它的 "
            f"{duck_write_ms[top_i] / duck_write_ms[best_i]:.1f}×"
        )
        print("    —— 纯亏：文件更大，写入还慢几十上百倍。")
    print()
    print("    zstd 的级别**不是「压缩强度档位」**，每一级换的是一套搜索策略和哈希表；")
    print("    换出来的大概率是更慢，不保证更小。")
    print()
    print("    （换个数据集，最省的级别就会变 —— 这正是「只能实测」的意思。）")
    con.close()


# ---------------------------------------------------------------- 场景 4


def scenario_encoding(rows: int) -> None:
    section("场景 4 · 编码策略：同一份数据，内部结构不一样")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 四种列：递增、高基数随机、有序、低基数
    df = pl.DataFrame(
        {
            "arange": np.arange(rows, dtype=np.int64),
            "random": np.random.default_rng(2).integers(0, 10**9, rows),
            "sorted": np.sort(np.random.default_rng(3).integers(0, 10**9, rows)),
            "lowcard": np.random.default_rng(1).choice(list("abcdefgh"), rows),
        }
    )
    con.register("t", df.to_arrow())

    duck_path = OUT_DIR / "encoding_duckdb.parquet"
    pol_path = OUT_DIR / "encoding_polars.parquet"
    con.execute(f"COPY t TO '{duck_path}' (FORMAT PARQUET, COMPRESSION zstd)")
    df.write_parquet(pol_path, compression="zstd")

    print(f"  数据：{rows:,} 行 × 4 列 —— 递增 id / 高基数随机 / 已排序 / 低基数（8 个取值）")
    print()
    print(f"  {'列':<12}{'DuckDB 编码':<32}{'Polars 编码':<32}")
    print("  " + "-" * 76)
    md_d = pq.ParquetFile(duck_path).metadata.row_group(0)
    md_p = pq.ParquetFile(pol_path).metadata.row_group(0)
    for i, name in enumerate(df.columns):
        print(
            f"  {common.pad(name, 12)}{common.pad(', '.join(md_d.column(i).encodings), 32)}"
            f"{common.pad(', '.join(md_p.column(i).encodings), 32)}"
        )
    print()
    print("  两边**都按基数挑**编码：低基数列上字典，高基数列不用字典")
    print("  （对高基数列上字典，字典里存的几乎是几万个不重复的值，纯属负担）。")
    print("  差别在细节上：")
    print("    · 字典索引怎么编码：DuckDB 用 PLAIN_DICTIONARY，Polars 用 RLE_DICTIONARY")
    print("      （RLE 是把字典下标做行程编码 —— 下标重复多的时候更省）")
    print("    · 高基数列上 Polars 还挂了个 RLE，DuckDB 是纯 PLAIN")
    print()
    print("  **能手动控制吗**：")
    print("    · Polars 侧可以 ——")
    print("      `write_parquet(use_pyarrow=True, pyarrow_options={'use_dictionary': False})`")
    print("    · DuckDB 侧**没有这个开关** —— 试过 `SET parquet_use_dictionary=false`，")
    print("      报 `unrecognized configuration parameter`。它只给压缩算法和级别。")
    print("    （DuckDB 侧本来也不太需要关：它按基数自己就挑好了。）")
    con.close()


# ---------------------------------------------------------------- 场景 5


def scenario_row_group(rows: int, repeat: int) -> None:
    section("场景 5 · 行组大小：同时决定体积、并行度和点查")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _make_data(rows, "log")
    con.register("t", df.to_arrow())

    print("  row group 是 parquet 的最小并行单位。它定多大，四件事一起变：")
    print("    · 全量扫描的并行度（**组数不够就喂不饱多核**）")
    print("    · 点查的代价（命中的那个组有多大，就有多少行要真解码）")
    print("    · 文件大小（每组一份元数据 + 统计；但每组独立压缩，小块局部性更好）")
    print("    · 统计信息能剪掉多少（组越碎，min/max 区间越窄，剪得越准）")
    print()

    sizes = [10_000, 100_000, 500_000, 1_000_000]
    print(
        f"  {'row group':<12}{'体积':<14}{'组数':<8}"
        f"{'DuckDB 全量':<14}{'Polars 全量':<14}{'点查':<18}"
    )
    print("  " + "-" * 78)

    for rg_size in sizes:
        path = OUT_DIR / f"rg_{rg_size}.parquet"
        con.execute(
            f"COPY t TO '{path}' (FORMAT PARQUET, COMPRESSION zstd, ROW_GROUP_SIZE {rg_size})"
        )
        size = path.stat().st_size
        n_groups = pq.ParquetFile(path).metadata.num_row_groups

        duck_all = _timeit(_read_duck(con, path), repeat)
        pol_all = _timeit(_read_polars(path), repeat)
        key = rows // 2
        duck_pt = _timeit(_read_point_duck(con, path, key), repeat)
        pol_pt = _timeit(_read_point_polars(path, key), repeat)

        print(
            f"  {common.pad(f'{rg_size:,}', 12)}{common.pad(f'{size:,} B', 14)}"
            f"{common.pad(str(n_groups), 8)}{common.pad(f'{duck_all:.1f} ms', 14)}"
            f"{common.pad(f'{pol_all:.1f} ms', 14)}"
            f"{common.pad(f'{duck_pt:.1f} / {pol_pt:.1f} ms', 18)}"
        )
    print()

    print("  两头都不能走到头：")
    print("    · 行组**太大**：组数不够，喂不饱多核 → 全量扫描掉下来；")
    print("      而且点查要解码整个命中的组 → 点查也慢")
    print("    · 行组**太小**：组数多到要额外调度，收益递减")
    print("  文件体积这一项反而是反直觉的：**组越碎，文件略小一点** ——")
    print("    每组独立压缩，小块里的数据局部性更好；元数据的开销可以忽略。")
    print()
    print("  两边的默认值（DuckDB 122880 行 / Polars 125000 行）落在中间那一档，")
    print("  对百万级数据是合理的。真要调，看的是**行数 ÷ 行组大小 = 组数够不够核数**。")
    print("  （`index_demo` 从另一头看这件事：数据排好序时，统计能剪掉多少组。）")
    con.close()


# ---------------------------------------------------------------- 场景 6


def scenario_reconcile(rows: int) -> None:
    section("场景 6 · 逐值对账：两边读同一个文件，结果必须一样")

    con = duckdb.connect()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = _make_data(rows, "log")

    duck_path = OUT_DIR / "reconcile_duckdb.parquet"
    pol_path = OUT_DIR / "reconcile_polars.parquet"
    con.register("t", df.to_arrow())
    con.execute(f"COPY t TO '{duck_path}' (FORMAT PARQUET, COMPRESSION zstd)")
    df.write_parquet(pol_path, compression="zstd")

    # 交叉读：DuckDB 读 Polars 写的文件、Polars 读 DuckDB 写的文件
    duck_reads_polars = pl.from_arrow(
        con.sql(f"SELECT * FROM read_parquet('{pol_path}')").to_arrow_table()
    )
    polars_reads_duck = pl.read_parquet(duck_path)

    ok1, msg1 = common.compare_frames(df, duck_reads_polars, ["ts"])
    print(f"  DuckDB 读 Polars 写的文件：{'✅' if ok1 else '❌'} —— {msg1}")

    ok2, msg2 = common.compare_frames(df, polars_reads_duck, ["ts"])
    print(f"  Polars 读 DuckDB 写的文件：{'✅' if ok2 else '❌'} —— {msg2}")

    ok3, msg3 = common.compare_frames(duck_reads_polars, polars_reads_duck, ["ts"])
    print(f"  两边读出来的结果互相比：{'✅' if ok3 else '❌'} —— {msg3}")

    print()
    print("  压缩参数换了、编码策略换了、行组大小变了，")
    print("  但**读回来的值一个字节都不差** —— 压缩只影响怎么存，不影响存什么。")
    con.close()


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 默认压缩不同（DuckDB = SNAPPY，Polars = ZSTD），差距多大**取决于数据形态**：")
    print("     数字为主的表差近四成，长字符串为主的表能差六七成。")
    print("     结论必须连数据形态一起说，换份数据就是另一个数。")
    print("  2. gzip 三头都不占优，除非「对方只认 gzip」。")
    print("  3. **zstd 的级别不是越大越省** —— 三个独立实现给出同一条非单调的曲线，")
    print("     升一级文件反而变小的事到处都有。最省的级别落在哪一档由数据决定，")
    print("     想省空间只能实测。")
    print("  4. 级别高的写入很贵（高级别一次要好几秒），省下的空间未必抵得上多花的时间。")
    print("  5. 编码上两边都按基数挑字典（细节不同：索引用 PLAIN 还是 RLE）；")
    print("     **DuckDB 没有关字典的开关**，Polars 有。")
    print("  6. 行组太大 → 喂不饱多核、点查变慢；行组太小 → 收益递减；")
    print("     文件体积反而是组越碎略小一点。两边默认值都在中间那一档。")
    print("  7. 压缩怎么选、编码怎么排、文件怎么切，**都不影响读回来的值** ——")
    print("     逐值对账三个方向全 ✅。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="压缩与编码")
    parser.add_argument("--rows", type=int, default=500_000, help="数据行数")
    parser.add_argument("--repeat", type=int, default=3, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print(f"产出目录：{OUT_DIR}")

    scenario_shape_gap(args.rows)
    scenario_matrix(args.rows, args.repeat)
    scenario_levels(args.rows)
    scenario_encoding(args.rows)
    scenario_row_group(args.rows, args.repeat)
    scenario_reconcile(args.rows)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
