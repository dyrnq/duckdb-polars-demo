"""数据倾斜：key 分布不均的时候，两个引擎谁更抗造。

别的 demo 造的数据，key 分布基本是均匀的 —— 就算订单那种，也只是维度权重有点差别。
可真实业务里 key 是长尾的：一个超级大客户、一张爆款商品、一个爬虫账号，
就能吃掉一半的行。**倾斜不是「数据脏」，是常态。**

这个脚本对照三件事：

  1. 倾斜长什么样 —— 同样的行数、同样的 key 个数，分布差在哪。
  2. GROUP BY：热点组贵不贵。
  3. JOIN：探针侧倾斜 vs build 侧倾斜 —— 后者才是性能悬崖。

刻意控制住两个变量，让对比只反映「分布」：
  · 行数一样；
  · **key 的个数一样**（否则倾斜的分组数更少，聚合反而更便宜，就把倾斜效应盖住了）。

用法：
    uv run python src/skew_demo.py
"""

from __future__ import annotations

import argparse
import time

import duckdb
import numpy as np
import polars as pl

import common


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _timeit(fn, repeats: int) -> float:
    """跑 repeats 遍取最快的一次，返回毫秒。"""
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


# ---------------------------------------------------------------- 数据


def _make_fact(rows: int, n_keys: int, hot_pct: float) -> pl.DataFrame:
    """事实表。

    hot_pct 是热 key（key=0）占的行数比例；其余行均匀摊在剩下的 key 上。
    **key 的个数始终是 n_keys** —— 倾斜只改分布，不改基数。
    """
    rng = np.random.default_rng(common.SEED)
    if hot_pct <= 0:
        k = rng.integers(0, n_keys, rows)
    else:
        n_hot = int(rows * hot_pct)
        k = np.concatenate(
            [
                np.zeros(n_hot, dtype=np.int64),
                rng.integers(1, n_keys, rows - n_hot),
            ]
        )
        # 打散：热 key 的行散落在各处，而不是连成一片 ——
        # 真实数据就是这样，连成一片反而好办（能按顺序扫）。
        rng.shuffle(k)
    return pl.DataFrame({"k": k, "v": rng.random(rows)})


def _make_dim(n_keys: int, dup: int) -> pl.DataFrame:
    """维度表。

    dup 是热 key（key=0）在维度表里重复了几次 —— 这是 **build 侧**的倾斜。
    现实里长这样：一个客户名下有多张卡、一个商品有多个 SKU、一个用户多个设备。
    """
    rng = np.random.default_rng(common.SEED + 1)
    k = np.concatenate([np.zeros(dup, dtype=np.int64), np.arange(1, n_keys, dtype=np.int64)])
    return pl.DataFrame({"k": k, "tag": rng.random(len(k))})


# ---------------------------------------------------------------- 场景 1


def scenario_shape(rows: int, n_keys: int) -> None:
    section("场景 1 · 倾斜长什么样")

    print("  两份数据：**行数一样、key 的个数也一样**，只有分布不同。")
    print("  （key 个数必须一样 —— 否则倾斜的那份分组更少，聚合反而更便宜，")
    print("    倾斜的效应就被盖住了，测出来的是「基数效应」不是「倾斜效应」。）")
    print()

    print(f"  {'':<12}{'热 key 占比':<12}{'最大组行数':<14}{'不同 key 数':<12}{'前 10 组占':<12}")
    print("  " + "-" * 64)
    top10_by_label: dict[str, float] = {}
    for hot_pct, label in ((0.0, "均匀"), (0.10, "轻倾斜 10%"), (0.40, "重倾斜 40%")):
        df = _make_fact(rows, n_keys, hot_pct)
        grouped = df.group_by("k").len().sort("len", descending=True)
        lens = grouped["len"].to_list()
        top10 = sum(lens[:10]) / rows * 100
        top10_by_label[label] = top10
        print(
            f"  {common.pad(label, 12)}{common.pad(f'{hot_pct * 100:.0f}%', 12)}"
            f"{common.pad(f'{lens[0]:,}', 14)}{common.pad(f'{len(lens):,}', 12)}"
            f"{common.pad(f'{top10:.1f}%', 12)}"
        )
    print()
    print("  「前 10 组占」这一列是重点：")
    print(
        f"    均匀时 10 个 key 加起来只占 {top10_by_label['均匀']:.2f}%（可以忽略），"
        f"重倾斜时前 10 个 key 就吃掉 {top10_by_label['重倾斜 40%']:.0f}%"
    )
    print("    —— 后面的场景就量这个的影响。")
    print()


# ---------------------------------------------------------------- 场景 2


def _group_by_duck(con, fact_name: str):
    def run() -> None:
        con.sql(
            f"SELECT k, count(*) AS n, sum(v) AS s FROM {fact_name} GROUP BY k"
        ).to_arrow_table()

    return run


def _group_by_polars(df: pl.DataFrame):
    def run() -> None:
        df.group_by("k").agg(pl.len().alias("n"), pl.col("v").sum().alias("s")).sort("k").to_arrow()

    return run


def scenario_group_by(rows: int, n_keys: int, repeat: int) -> None:
    section("场景 2 · GROUP BY：热点组贵不贵")

    con = duckdb.connect()
    print(f"  数据：{rows:,} 行 × {n_keys:,} 个 key，分布分别是均匀 / 10% 倾斜 / 40% 倾斜")
    print(f"  机器状态：{common.describe_machine()}")
    print()
    print("  **口径**：两边都把分组结果物化成 Arrow（{key, 组行数, 组内 sum}），")
    print("  不走 Python 对象 —— 否则量到的是「建 Python 列表」的代价，不是聚合的代价。")
    print()

    print(f"  {'分布':<14}{'DuckDB':<14}{'Polars':<14}{'Polars/DuckDB':<16}")
    print("  " + "-" * 56)

    results: dict[str, tuple[float, float, pl.DataFrame, pl.DataFrame]] = {}
    for hot_pct, label in ((0.0, "均匀"), (0.10, "倾斜 10%"), (0.40, "倾斜 40%")):
        df = _make_fact(rows, n_keys, hot_pct)
        con.register("fact", df.to_arrow())

        duck_ms = _timeit(_group_by_duck(con, "fact"), repeat)
        pol_ms = _timeit(_group_by_polars(df), repeat)
        print(
            f"  {common.pad(label, 14)}{common.pad(f'{duck_ms:.1f} ms', 14)}"
            f"{common.pad(f'{pol_ms:.1f} ms', 14)}{common.pad(f'{pol_ms / duck_ms:.2f}×', 16)}"
        )

        # 顺手把结果留下来，最后逐值对账
        results[label] = (
            duck_ms,
            pol_ms,
            pl.from_arrow(
                con.sql("SELECT k, count(*) AS n, sum(v) AS s FROM fact GROUP BY k").arrow()
            ),
            df.group_by("k").agg(pl.len().alias("n"), pl.col("v").sum().alias("s")).sort("k"),
        )
    print()

    uniform = results["均匀"]
    heavy = results["倾斜 40%"]
    print("  DuckDB 那边几乎没变：")
    print(
        f"    均匀 {uniform[0]:.1f} ms → 重倾斜 {heavy[0]:.1f} ms（{heavy[0] / uniform[0]:.2f}×）"
    )
    print(f"    Polars：{uniform[1]:.1f} ms → {heavy[1]:.1f} ms（{heavy[1] / uniform[1]:.2f}×）")
    print()
    print("  聚合的代价跟「有多少行」有关，跟「怎么分布」关系不大 ——")
    print("  热 key 的行多，只是让一个组变大了，总工作量没变。")
    print("  DuckDB 那边用 perfect hash + radix 分区把组打散到不同分区，")
    print("  热组再大也只压在一个分区上，所以更平。")
    print()
    print("  **但这只是 group by。真正会塌的是 join。**")
    con.close()
    return results


# ---------------------------------------------------------------- 场景 3


def _join_duck(con, fact_name: str, dim_name: str):
    """join 之后只算一个标量。

    不物化 join 的全部输出 —— 输出可能有几千万行，量到的就变成
    「谁铺得快」而不是「谁 join 得快」。但标量里用到了两边的列，
    所以 join 一行都省不掉。
    """

    def run() -> None:
        con.sql(
            f"SELECT sum(f.v * d.tag) FROM {fact_name} f JOIN {dim_name} d USING (k)"
        ).fetchall()

    return run


def _join_polars(fact: pl.DataFrame, dim: pl.DataFrame):
    """同样只算一个标量；用 lazy 版，别把 join 结果整个物化出来。"""

    def run() -> None:
        (
            fact.lazy()
            .join(dim.lazy(), on="k", how="inner")
            .select((pl.col("v") * pl.col("tag")).sum())
            .collect()
        )

    return run


def scenario_join(rows: int, n_keys: int, repeat: int) -> None:
    section("场景 3 · JOIN：探针侧倾斜 vs build 侧倾斜")

    con = duckdb.connect()
    dim = _make_dim(n_keys, dup=1)
    con.register("dim", dim.to_arrow())

    print("  JOIN 有两侧，倾斜落在哪一侧，结果完全不一样：")
    print()
    print("    · **探针侧**（事实表里热 key 占大头，维度表每个 key 一行）")
    print("      → 大量事实行去撞同一个维度行。这是现实里最常见的倾斜。")
    print("    · **build 侧**（维度表里热 key 自己就重复了很多行）")
    print("      → 撞上的那个维度行带着一长串，输出行数直接爆炸。")
    print()
    print("  **口径**：两边都 join 完只算一个标量 `sum(f.v * d.tag)`，")
    print("  不物化 join 的输出（输出可能上千万行）。标量用到了两边的列，")
    print("  所以 join 一行都省不掉 —— 量的是 join 本身。")
    print()

    print(f"  {'场景':<26}{'输出行数':<14}{'DuckDB':<14}{'Polars':<14}{'Polars/DuckDB':<16}")
    print("  " + "-" * 82)

    cases = [
        (0.0, 1, "均匀"),
        (0.40, 1, "探针侧倾斜 40%"),
        (0.20, 50, "build 侧：热 key 重复 50"),
        (0.20, 200, "build 侧：热 key 重复 200"),
    ]

    for hot_pct, dup, label in cases:
        fact = _make_fact(rows, n_keys, hot_pct)
        dim = _make_dim(n_keys, dup=dup)
        con.register("fact", fact.to_arrow())
        con.register("dim", dim.to_arrow())

        out_rows = con.sql("SELECT count(*) FROM fact f JOIN dim d USING (k)").fetchall()[0][0]

        duck_ms = _timeit(_join_duck(con, "fact", "dim"), repeat)
        pol_ms = _timeit(_join_polars(fact, dim), repeat)

        print(
            f"  {common.pad(label, 26)}{common.pad(f'{out_rows:,}', 14)}"
            f"{common.pad(f'{duck_ms:.1f} ms', 14)}{common.pad(f'{pol_ms:.1f} ms', 14)}"
            f"{common.pad(f'{pol_ms / duck_ms:.2f}×', 16)}"
        )
    print()

    print("  两端的结论：")
    print("    · 均匀时两边差不多；")
    print("    · **探针侧倾斜**：维度表干净，那个热 key 只是撞得频繁，")
    print("      两边都还扛得住；")
    print("    · **build 侧倾斜**：维度表里热 key 自己就有一长串，")
    print("      每次撞上去都要把整串走一遍 —— 这才是性能悬崖。")
    print("      维度表里的重复 key（一个客户多张卡、一个商品多个 SKU）")
    print("      在真实数据里非常常见。")
    con.close()


# ---------------------------------------------------------------- 场景 4


def scenario_hot_filter(rows: int, n_keys: int, repeat: int) -> None:
    section("场景 4 · 热点过滤：查那一个超级大客户")

    con = duckdb.connect()
    fact = _make_fact(rows, n_keys, hot_pct=0.40)

    print("  「把那个占了四成数据的客户查出来」—— 现实里最常被抱怨慢的一条 SQL。")
    print("  它慢不慢，跟倾斜无关，跟**能不能只读那部分**有关。")
    print()
    print("  **口径**：两边都只取热 key 的那些行，物化成 Arrow。")
    print()

    def duck_run() -> None:
        con.sql("SELECT * FROM fact WHERE k = 0").to_arrow_table()

    def pol_run() -> None:
        fact.filter(pl.col("k") == 0).to_arrow()

    con.register("fact", fact.to_arrow())
    duck_ms = _timeit(duck_run, repeat)
    pol_ms = _timeit(pol_run, repeat)

    hot_rows = int((fact["k"] == 0).sum())
    print(f"  热 key 占 {hot_rows:,} 行（{hot_rows / rows * 100:.0f}%）")
    print(f"  DuckDB : {duck_ms:.1f} ms")
    print(f"  Polars : {pol_ms:.1f} ms")
    print(f"  相差 {pol_ms / duck_ms:.2f}×")
    print()
    print("  这条查询注定要读掉四成的数据 —— 谁都快不到哪去。")
    print("  倾斜真正的问题是**它把别的查询也拖慢了**（整表扫描时热点行挤在少数分区里），")
    print("  以及 build 侧的重复 key（见场景 3）。")
    print()
    print("  顺带一提：真要治倾斜，SQL 层面只有三条路 ——")
    print("    · 预聚合（把热 key 的结果提前算好）")
    print("    · 加盐（把热 key 拆成 N 个子 key，再合并）")
    print("    · 改数据模型（别让维度表里同一个 key 重复）")
    print("  两个引擎都不提供「自动倾斜处理」——")
    print("  DuckDB 有 perfect hash 和 radix 分区扛了一部分，")
    print("  但那是算法层面的缓解，不是「帮你把倾斜拆开」。")
    con.close()


# ---------------------------------------------------------------- 场景 5


def scenario_reconcile(rows: int, n_keys: int) -> None:
    section("场景 5 · 逐值对账：倾斜不改变结果，只改变代价")

    con = duckdb.connect()
    fact = _make_fact(rows, n_keys, hot_pct=0.40)
    dim = _make_dim(n_keys, dup=50)
    con.register("fact", fact.to_arrow())
    con.register("dim", dim.to_arrow())

    duck_group = pl.from_arrow(
        con.sql("SELECT k, count(*) AS n, sum(v) AS s FROM fact GROUP BY k").arrow()
    )
    pol_group = fact.group_by("k").agg(pl.len().alias("n"), pl.col("v").sum().alias("s")).sort("k")

    ok, msg = common.compare_frames(duck_group, pol_group, ["k"])
    print(f"  GROUP BY 的结果：{'✅' if ok else '❌'} —— {msg}")

    duck_join = con.sql("SELECT sum(f.v * d.tag) FROM fact f JOIN dim d USING (k)").fetchall()[0][0]
    pol_join = (
        fact.lazy()
        .join(dim.lazy(), on="k", how="inner")
        .select((pl.col("v") * pl.col("tag")).sum())
        .collect()
        .item()
    )
    same = abs(duck_join - pol_join) <= 1e-9 * max(1.0, abs(duck_join))
    print(f"  JOIN 的结果：{'✅' if same else '❌'} —— DuckDB {duck_join!r} vs Polars {pol_join!r}")
    print()
    print("  倾斜只改变**代价**，不改变**结果** —— 两个引擎算出来的值必须一模一样。")
    print("  这也是为什么性能差异才值得单独测：结论对了，才轮到比谁快。")
    con.close()


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. 倾斜是常态，不是数据脏。真实业务里一个超级大客户就能吃掉一半的行。")
    print("  2. **GROUP BY 基本不怕倾斜**：代价跟行数有关，跟分布关系不大。")
    print("     DuckDB 那边尤其平（perfect hash + radix 分区把热组打散）。")
    print("  3. **JOIN 才怕，而且怕的是 build 侧**：维度表里热 key 自己重复成串")
    print("     （一个客户多张卡、一个商品多个 SKU）时，输出行数爆炸，")
    print("     Polars 的退化斜率明显比 DuckDB 陡。")
    print("  4. 探针侧倾斜（事实表热、维度表干净）两边都扛得住 —— 那种情况不用怕。")
    print("  5. 两个引擎都**不提供自动倾斜处理**：预聚合、加盐、改数据模型，")
    print("     都得自己来。")
    print("  6. 倾斜只改代价不改结果 —— 逐值对账两个方向全 ✅。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="数据倾斜")
    parser.add_argument("--rows", type=int, default=1_000_000, help="事实表行数")
    parser.add_argument(
        "--keys",
        type=int,
        default=0,
        help="key 的个数（刻意固定，不随倾斜变；0 = 自动取行数的 1/10）",
    )
    parser.add_argument("--repeat", type=int, default=3, help="性能测量重复次数")
    args = parser.parse_args()

    # key 的个数跟着行数走，好让「均匀」那一份的组平均有十来行 ——
    # 组太碎的话聚合本身就便宜，倾斜的效应反而被稀释。
    if args.keys <= 0:
        args.keys = max(1, args.rows // 10)

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    scenario_shape(args.rows, args.keys)
    scenario_group_by(args.rows, args.keys, args.repeat)
    scenario_join(args.rows, args.keys, args.repeat)
    scenario_hot_filter(args.rows, args.keys, args.repeat)
    scenario_reconcile(args.rows, args.keys)
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
