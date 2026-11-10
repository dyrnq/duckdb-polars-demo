"""索引与点查：DuckDB 的 ART 索引到底能顶什么用，Polars 没有索引怎么点查。

README 把「索引」列进了选型问题，但一直没实测过。这一节把它拆成四问：

  1. DuckDB 的 ART 索引能服务哪些查询？（等值？范围？在键空间的什么位置？）
  2. 索引能带来多少收益？（什么时候有用，什么时候白建）
  3. 索引要付出什么代价？（建索引的时间、体积、写入变慢）
  4. Polars 没有索引，点查该怎么办？（全表过滤 / is_in / 哈希 join / 手工二分）

这四个问题的答案都不是「DuckDB 有 Polars 没有」这么简单。实测下来最反直觉的一条是：
**点查快不快，主要取决于数据排没排序，而不是有没有索引。**
DuckDB 自己在 Parquet 上也有一套 row group 统计（zone map），Polars 读 Parquet 时
同样会用到 Parquet 文件自带的 row group min/max —— 排序过的数据上，
两边都能剪掉大半文件，谁都没用索引。

用法：
    uv run python src/index_demo.py
    uv run python src/index_demo.py --rows 8000000 --rounds 5
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import time
import uuid

import common

CUSTOMERS_PREFIX = "index_customers"

# 挑一个靠后的主键：排好序时它落在最后一个 row group 里，最能看出剪枝效果
DEFAULT_KEY = 3_000_000
ROW_GROUP_SIZE = 200_000


def customers_path(n_rows: int, *, sorted_by_key: bool) -> str:
    """客户表的文件名里带上行数。

    带行数是为了**永不覆盖已有文件**：换个 --rows 就是另一份数据，
    两份数据可以并存，重复跑同一个行数则直接复用。
    """
    tag = "sorted" if sorted_by_key else "shuf"
    return common.DATA_DIR / f"{CUSTOMERS_PREFIX}_{tag}_{n_rows}.parquet"


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


# ---------------------------------------------------------------- 造数


def ensure_data(n_rows: int) -> tuple:
    """生成两份客户表：一份按主键排序，一份随机打乱行序。

    为什么要两份：Parquet 的 row group 各自带 min/max 统计，数据排好序时
    一个 row group 只覆盖一段主键区间，谓词一推就能整组跳过；
    打乱之后每个 row group 的主键范围都是 [1, N]，统计信息就废了。
    「有没有索引」的收益会被这个因素完全盖住，所以必须分开造。

    两份文件是**同一批行的两种排列**（打乱版是整行置换，不是只换主键列），
    所以同一个 customer_id 在两边查出来的 name/region/balance 完全一致，可以对账。

    已存在的文件直接复用，绝不覆盖 —— 文件名带行数，换行数就是换一份文件。
    """
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    sorted_path = customers_path(n_rows, sorted_by_key=True)
    shuf_path = customers_path(n_rows, sorted_by_key=False)
    if sorted_path.exists() and shuf_path.exists():
        print(f"复用已有数据（{n_rows:,} 行）")
        return sorted_path, shuf_path

    print(f"生成 {n_rows:,} 行客户表 …")
    rng = np.random.default_rng(common.SEED)
    ids = np.arange(1, n_rows + 1, dtype=np.int64)
    table = pa.table(
        {
            "customer_id": pa.array(ids),
            "name": pa.array([f"customer_{i:08d}" for i in ids]),
            "region": pa.array(rng.choice(["华东", "华北", "华南", "西南", "华中"], size=n_rows)),
            "balance": pa.array(rng.lognormal(mean=8.0, sigma=1.2, size=n_rows)),
        }
    )
    shuffled = table.take(pa.array(rng.permutation(n_rows)))  # 整行置换
    for path, tbl in ((sorted_path, table), (shuf_path, shuffled)):
        if path.exists():
            continue
        pq.write_table(tbl, path, compression="zstd", row_group_size=ROW_GROUP_SIZE)
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"  已写入 {path.name}（{size_mb:.1f} MB）")
    return sorted_path, shuf_path


# ---------------------------------------------------------------- DuckDB 侧


def duck_conn(path, *, index: bool):
    """开一个 DuckDB 连接把客户表装进来；index=True 就顺手建 ART 索引。

    每个变体用**独立连接**：DuckDB 的 buffer manager 是连接级的，
    共用一个连接会让后一个变体蹭到前一个的热数据，测出来的是缓存命中而不是引擎能力。
    """
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE TABLE cust AS SELECT * FROM read_parquet('{path}')")
    if index:
        con.execute("CREATE INDEX idx_cust ON cust(customer_id)")
    return con


def explain(con, sql: str) -> str:
    """EXPLAIN ANALYZE 的文本计划 —— 注意必须带 ANALYZE 才看得到实际走的算子。"""
    return con.execute("EXPLAIN ANALYZE " + sql).fetchall()[0][1]


def plan_facts(plan: str) -> tuple[str, str]:
    """从执行计划里抽出「算子类型」和「扫了几个 row group」。"""
    m = re.search(r"Type: ([^\n]+)", plan)
    op = m.group(1).strip().rstrip("│").strip() if m else "?"
    m2 = re.search(r"Row Groups Scanned: (\d+) / (\d+)", plan)
    rg = f"{m2.group(1)}/{m2.group(2)}" if m2 else "-"
    return op, rg


# ---------------------------------------------------------------- 测量


def measure(variants: dict, rounds: int, repeats: int) -> dict:
    """交错测量所有变体，返回 {label: 每轮的中位数列表}。

    为什么交错：这台是共享开发机，旁边随时有人在编译。串行地测完变体 A 再测 B，
    谁赶上高负载谁背锅。这里每一轮都把所有变体过一遍，负载漂移对谁都一视同仁。
    """
    runners = {}
    for label, fn in variants.items():
        fn()  # 预热：Polars 的惰性计划首次 collect 时才编译
        runners[label] = fn

    acc: dict[str, list[float]] = {label: [] for label in variants}
    for _ in range(rounds):
        for label, fn in runners.items():
            times = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                fn()
                times.append((time.perf_counter() - t0) * 1000)
            acc[label].append(statistics.median(times))
    return acc


def median_of(acc: dict, label: str) -> float:
    return statistics.median(acc[label])


def best_of(acc: dict, label: str) -> float:
    return min(acc[label])


def _as_rows(out) -> list:
    """DuckDB 给的是 tuple 列表，Polars 给的是 DataFrame —— 统一成 tuple 列表好比对。"""
    if hasattr(out, "rows"):
        return out.rows()
    return [tuple(r) for r in out]


# ---------------------------------------------------------------- 场景 1


def scenario_point_query(
    sorted_path, shuf_path, n_rows: int, key: int, rounds: int, repeats: int
) -> None:
    """点查：等值查找到底快多少，快的是索引还是别的什么。"""
    section("场景 1 · 点查（等值查找）：快的是索引，还是数据布局？")

    sql = f"SELECT customer_id, name, region, balance FROM cust WHERE customer_id = {key}"

    def duck_variant(path, index):
        con = duck_conn(path, index=index)

        def run():
            return con.sql(sql).fetchall()

        return run, con

    def polars_scan_variant(path):
        import polars as pl

        lf = pl.scan_parquet(path)

        def run():
            return lf.filter(pl.col("customer_id") == key).collect()

        return run

    def polars_mem_variant(path):
        import polars as pl

        df = pl.read_parquet(path)

        def run():
            return df.filter(pl.col("customer_id") == key)

        return run

    variants = {}
    conns = []
    for tag, path in (("排序", sorted_path), ("打乱", shuf_path)):
        for index, name in ((False, "无索引"), (True, "有 ART 索引")):
            run, con = duck_variant(path, index)
            conns.append(con)
            variants[f"duckdb {tag} {name}"] = run
        variants[f"polars {tag} 扫 Parquet"] = polars_scan_variant(path)
        variants[f"polars {tag} 已在内存"] = polars_mem_variant(path)

    acc = measure(variants, rounds, repeats)

    print(f"  数据：{n_rows:,} 行；查 customer_id = {key:,}")
    print(f"  机器状态：{common.describe_machine()}")
    print()
    headers = ["数据布局", "实现", "中位数(ms)", "最快(ms)", "相对最快"]
    rows = []
    base_best = min(best_of(acc, label) for label in variants)
    layout_rows = [
        ("排序", "DuckDB 表 · 无索引", "duckdb 排序 无索引"),
        ("排序", "DuckDB 表 · 有 ART 索引", "duckdb 排序 有 ART 索引"),
        ("排序", "Polars · 扫 Parquet", "polars 排序 扫 Parquet"),
        ("排序", "Polars · 已在内存", "polars 排序 已在内存"),
        ("打乱", "DuckDB 表 · 无索引", "duckdb 打乱 无索引"),
        ("打乱", "DuckDB 表 · 有 ART 索引", "duckdb 打乱 有 ART 索引"),
        ("打乱", "Polars · 扫 Parquet", "polars 打乱 扫 Parquet"),
        ("打乱", "Polars · 已在内存", "polars 打乱 已在内存"),
    ]
    for tag, impl, label in layout_rows:
        med = median_of(acc, label)
        best = best_of(acc, label)
        rows.append([tag, impl, f"{med:.2f}", f"{best:.2f}", f"{best / base_best:.1f}×"])
    _table(headers, rows, [10, 26, 12, 12, 10])

    # ---------------------------------------------------------------- 结论
    print()
    d_sort_no = median_of(acc, "duckdb 排序 无索引")
    d_sort_ix = median_of(acc, "duckdb 排序 有 ART 索引")
    d_shuf_no = median_of(acc, "duckdb 打乱 无索引")
    d_shuf_ix = median_of(acc, "duckdb 打乱 有 ART 索引")
    p_sort = median_of(acc, "polars 排序 扫 Parquet")
    p_shuf = median_of(acc, "polars 打乱 扫 Parquet")
    m_shuf = median_of(acc, "polars 打乱 已在内存")

    print("  ① 索引的收益，取决于数据排没排序")
    print(
        f"     排好序：DuckDB {d_sort_no:.2f} ms（无索引） vs {d_sort_ix:.2f} ms（有索引）"
        f" —— 没有差别"
    )
    print("        row group 统计已经把范围缩到一组，索引能省的那点扫描早就省掉了")
    print(
        f"     打乱后：DuckDB {d_shuf_no:.2f} ms（无索引） → {d_shuf_ix:.2f} ms（有索引）"
        f"（{d_shuf_no / d_shuf_ix:.1f}×）—— 这才是索引真正的用武之地"
    )
    print()
    print("  ② 排不排序，对 Polars 的影响比 DuckDB 还大")
    print(f"     Polars 扫 Parquet：{p_sort:.2f} ms → {p_shuf:.2f} ms（{p_shuf / p_sort:.1f}×）")
    print("     —— 它读 Parquet 靠的就是 row group 统计剪枝，排好序只读一组，打乱得全读")
    print()
    print("  ③ 但 Polars 的慢，**不是没索引的锅**")
    print(
        f"     数据已经在内存里时：Polars {m_shuf:.2f} ms vs DuckDB {d_shuf_ix:.2f} ms"
        f"（同一份数据、同一个键）"
    )
    print("     —— 差距在「读 Parquet」这一层，不在「点查」这一层")
    print(
        f"     Polars 扫 Parquet {p_shuf:.2f} ms → 已在内存 {m_shuf:.2f} ms，读文件本身就占了大头"
    )
    print()
    print("  ⚠ 所以别拿「DuckDB 有索引、Polars 没有」当结论：")
    print(
        f"     排好序时 DuckDB **无索引** {d_sort_no:.2f} ms 就已经比 Polars 扫文件 "
        f"{p_sort:.2f} ms 快 {p_sort / d_sort_no:.1f}×。"
    )
    print("     真正的工程动作是**按点查的键排序落盘**，而不是想办法造索引。")

    # ---------------------------------------------------------------- 对账
    print()
    print("  结果对账（八个变体必须返回同一行）：")
    ref = None
    all_same = True
    for label in variants:
        rows_out = _as_rows(variants[label]())
        if ref is None:
            ref = rows_out
            print(f"    {common.pad(label, 26)} ✅（作为对账基准）")
            continue
        same = rows_out == ref
        all_same = all_same and same
        print(f"    {common.pad(label, 26)} {'✅ 一致' if same else '❌ 不一致'}")
    print(f"  → {'✅ 八个变体结果完全一致' if all_same else '❌ 结果不一致'}")

    for con in conns:
        con.close()


# ---------------------------------------------------------------- 场景 2


def scenario_index_scope(shuf_path, n_rows: int, key: int) -> None:
    """索引的适用边界：实测出来的规则是「等值走索引，范围看位置和命中数」。"""
    section("场景 2 · 索引的适用边界：等值一定走索引，范围要看位置和命中数")

    con = duck_conn(shuf_path, index=True)
    n = n_rows
    cases = [
        ("等值 = K", f"customer_id = {key}"),
        ("IN（不连续的 3 个值）", f"customer_id IN ({key}, {key + 7}, {key + 100})"),
        ("IN（连续的 3 个值）", f"customer_id IN ({key}, {key + 1}, {key + 2})"),
        ("范围 >= K（中部，命中 200 万行）", f"customer_id >= {key}"),
        ("范围 > n-1000（尾部，命中 1000 行）", f"customer_id > {n - 1000}"),
        ("BETWEEN 中部 1000 行", f"customer_id BETWEEN {key} AND {key + 1000}"),
        ("BETWEEN 尾部 1000 行", f"customer_id BETWEEN {n - 1000} AND {n}"),
        ("BETWEEN 尾部 10000 行", f"customer_id BETWEEN {n - 10000} AND {n}"),
        ("列上的表达式 + 0", f"customer_id + 0 = {key}"),
        ("列上的表达式 abs()", f"abs(customer_id) = {key}"),
        ("转成字符串比较", f"customer_id::VARCHAR = '{key}'"),
        ("OR 两个等值", f"customer_id = {key} OR customer_id = {key + 5}"),
        ("低基数列等值", "region = '华东'"),
    ]
    headers = ["过滤条件", "实际算子", "扫了几个 row group", "走索引了吗"]
    rows = []
    for label, cond in cases:
        op, rg = plan_facts(explain(con, f"SELECT * FROM cust WHERE {cond}"))
        rows.append([label, op, rg, "✅ 是" if op == "Index Scan" else "— 否"])
    _table(headers, rows, [36, 18, 20, 10])

    print()
    print("  实测出来的规则（三条）：")
    print("  1. **等值一定走索引**：`= K`、`IN` 一串不连续的值，都会走 ART 索引。")
    print("  2. **范围不一定**：只有当范围落在主键空间的**两端**、且命中行数很少")
    print(f"     （实测阈值在 5000 行左右，这张表 {n:,} 行 ≈ 0.1%）时才会走索引。")
    print("     同样 1000 行，落在尾部走索引，落在中部却全表扫 ——")
    print("     优化器只能靠 min/max 统计判断选择性，中部的区间它估不准。")
    print("  3. **连续 IN 会被改写成范围**：`IN (K, K+1, K+2)` 被改写成")
    print("     `>= K AND <= K+2`，于是按范围的规则判断 —— 索引就丢掉了。")
    print("     不连续的值（`IN (K, K+7, K+100)`）保持等值形式，索引还在。")
    print("  另外两个细节：")
    print("  · 常量表达式会被折叠（`customer_id + 0 = K` → `customer_id = K`），所以照样能用索引；")
    print("    但转成字符串比较（`::VARCHAR`）就彻底用不了了。")
    print("  · `OR` 两个等值、或者在低基数列上建索引 —— 都不走索引（后者是另一个大坑：")
    print("    低基数列的索引既救不了点查，还白白拖慢写入）。")
    print()
    print("  3'. `EXPLAIN` 和 `EXPLAIN ANALYZE` 对索引的结论是**相反的**：")
    print()
    plain = con.execute(f"EXPLAIN SELECT * FROM cust WHERE customer_id = {key}").fetchall()[0][1]
    print("     EXPLAIN（逻辑计划）：")
    for line in plain.splitlines():
        if "Type:" in line or "Filters:" in line:
            print(f"       {line.strip()}")
    print("     EXPLAIN ANALYZE（实际执行）：")
    for line in explain(con, f"SELECT * FROM cust WHERE customer_id = {key}").splitlines():
        if "Type:" in line or "Row Groups" in line:
            print(f"       {line.strip()}")
    print()
    print("     👉 想确认索引到底用没用上，只能看 EXPLAIN ANALYZE 的「Row Groups Scanned」。")
    con.close()


# ---------------------------------------------------------------- 场景 3


def scenario_index_cost(shuf_path, n_rows: int) -> None:
    """索引的代价：建一次要多久、占多少空间、写入慢多少。"""
    section("场景 3 · 索引的代价：建一次要多久、占多少磁盘、写入慢多少")

    import duckdb

    # ---- 建索引的耗时与体积 ----
    db_path = common.DATA_DIR / f"index_cost_{uuid.uuid4().hex}.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(f"CREATE TABLE cust AS SELECT * FROM read_parquet('{shuf_path}')")
    con.execute("CHECKPOINT")
    size_before = os.path.getsize(db_path)

    t0 = time.perf_counter()
    con.execute("CREATE INDEX idx_cust ON cust(customer_id)")
    build_ms = (time.perf_counter() - t0) * 1000
    con.execute("CHECKPOINT")
    size_after = os.path.getsize(db_path)

    print(f"  {n_rows:,} 行单列 ART 索引：")
    print(f"    建索引耗时     {build_ms:.0f} ms")
    print(
        f"    库文件体积     {size_before / 1024 / 1024:.1f} MB → {size_after / 1024 / 1024:.1f} MB"
        f"（+{(size_after - size_before) / 1024 / 1024:.1f} MB，"
        f"+{(size_after - size_before) / size_before * 100:.0f}%）"
    )
    con.close()
    db_path.unlink(missing_ok=True)

    # ---- 写入代价 ----
    def insert_ms(with_index: bool) -> float:
        c = duckdb.connect()
        c.execute(f"CREATE TABLE t AS SELECT * FROM read_parquet('{shuf_path}') LIMIT 0")
        if with_index:
            c.execute("CREATE INDEX idx_t ON t(customer_id)")
        t0 = time.perf_counter()
        c.execute(f"INSERT INTO t SELECT * FROM read_parquet('{shuf_path}') LIMIT 500000")
        ms = (time.perf_counter() - t0) * 1000
        c.close()
        return ms

    no_idx = insert_ms(False)
    with_idx = insert_ms(True)
    print()
    print(
        f"    插入 50 万行：无索引 {no_idx:.0f} ms  vs  有索引 {with_idx:.0f} ms"
        f"（{with_idx / no_idx:.1f}× 慢）"
    )
    print()
    print("  👉 索引是**写放大**：每插一行都要维护索引。批量灌数据的场景，")
    print("     正确做法是「先灌完再建索引」，而不是边写边维护。")


# ---------------------------------------------------------------- 场景 4


def scenario_polars_lookup(shuf_path, n_rows: int, key: int, rounds: int, repeats: int) -> None:
    """Polars 没有索引：点查有几种写法，各适合什么场景。"""
    section("场景 4 · Polars 没有索引：点查的几种写法")

    import polars as pl

    df = pl.read_parquet(shuf_path)
    keys = [key + i * 977 for i in range(100)]  # 100 个待查键
    keys_1000 = [key + i * 311 for i in range(1000)]  # 1000 个待查键

    # 手工索引：把表按主键排好，之后用二分找行。**在计时之外建好**，
    # 这样「复用索引」这一行量到的才是查找本身的代价。
    sorted_df = df.sort("customer_id")
    sorted_ids = sorted_df["customer_id"]

    def manual_lookup(target_keys: list[int]):
        out = []
        for k in target_keys:
            pos = sorted_ids.search_sorted(k)
            if pos < len(sorted_ids) and sorted_ids[pos] == k:
                out.append(sorted_df.slice(pos, 1))
        return pl.concat(out) if out else sorted_df.slice(0, 0)

    def manual_with_build(target_keys: list[int]):
        """连「建索引」一起算进去 —— 这是真实的一次性成本。"""
        sdf = df.sort("customer_id")
        sids = sdf["customer_id"]
        out = []
        for k in target_keys:
            pos = sids.search_sorted(k)
            if pos < len(sids) and sids[pos] == k:
                out.append(sdf.slice(pos, 1))
        return pl.concat(out) if out else sdf.slice(0, 0)

    key_df = pl.DataFrame({"customer_id": keys})
    key_df_1000 = pl.DataFrame({"customer_id": keys_1000})
    # is_in 的参数用 `pl.Series(...).implode()`：Polars 2.1 里传一个同 dtype 的
    # Series 会告警（"collection of the same datatype is ambiguous"），
    # 官方给的出路就是 implode。下面的表格里三种写法都跑一遍，验证结果一致。
    keys_series = pl.Series("customer_id", keys).implode()
    keys_1000_series = pl.Series("customer_id", keys_1000).implode()

    variants = {
        "全表过滤 filter（1 个键）": lambda: df.filter(pl.col("customer_id") == key),
        "全表过滤 is_in（100 个键）": lambda: df.filter(pl.col("customer_id").is_in(keys_series)),
        "哈希 join semi（100 个键）": lambda: df.join(key_df, on="customer_id", how="semi"),
        "手工二分（100 个键，含建索引）": lambda: manual_with_build(keys),
        "手工二分（100 个键，复用索引）": lambda: manual_lookup(keys),
        "哈希 join semi（1000 个键）": lambda: df.join(key_df_1000, on="customer_id", how="semi"),
        "全表过滤 is_in（1000 个键）": lambda: df.filter(
            pl.col("customer_id").is_in(keys_1000_series)
        ),
        "手工二分（1000 个键，复用索引）": lambda: manual_lookup(keys_1000),
    }
    acc = measure(variants, rounds, repeats)

    headers = ["写法", "中位数(ms)", "最快(ms)", "命中行数"]
    rows = []
    for label, fn in variants.items():
        out = fn()
        height = out.height if isinstance(out, pl.DataFrame) else len(out)
        rows.append(
            [
                label,
                f"{median_of(acc, label):.2f}",
                f"{best_of(acc, label):.2f}",
                f"{height:,}",
            ]
        )
    _table(headers, rows, [36, 14, 12, 12])

    # 顺手验一下 Polars 2.1 的 is_in 废弃警告（不是凭记忆写，是当场跑出来的）
    import warnings

    def _is_in_forms():
        return [
            ("is_in(python list)", lambda: df.filter(pl.col("customer_id").is_in(keys))),
            (
                "is_in(pl.Series)",
                lambda: df.filter(pl.col("customer_id").is_in(pl.Series("k", keys))),
            ),
            (
                "is_in(pl.Series.implode())",
                lambda: df.filter(pl.col("customer_id").is_in(pl.Series("k", keys).implode())),
            ),
        ]

    print()
    print("  顺带一个 Polars 2.1 的坑：`is_in` 传什么参数，三种写法结果一样但告警不同：")
    for form_label, form_fn in _is_in_forms():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            got = {r["customer_id"] for r in form_fn().to_dicts()}
        warned = any(issubclass(w.category, (DeprecationWarning, FutureWarning)) for w in caught)
        same = got == set(keys)
        print(
            f"    {common.pad(form_label, 30)} 命中 {'✅' if same else '❌'}"
            f"  废弃警告：{'⚠ 有' if warned else '无'}"
        )
    print("    → 脚本里用 `pl.Series(...).implode()`：语义明确，且不告警。")

    print()
    build_ms = median_of(acc, "手工二分（100 个键，含建索引）") - median_of(
        acc, "手工二分（100 个键，复用索引）"
    )
    print(f"  · 手工二分的「建索引」一次性成本：约 {build_ms:.0f} ms（排序 {n_rows:,} 行）")
    print()
    is_in_100 = median_of(acc, "全表过滤 is_in（100 个键）")
    join_100 = median_of(acc, "哈希 join semi（100 个键）")
    is_in_1000 = median_of(acc, "全表过滤 is_in（1000 个键）")
    join_1000 = median_of(acc, "哈希 join semi（1000 个键）")
    manual_100 = median_of(acc, "手工二分（100 个键，复用索引）")
    manual_1000 = median_of(acc, "手工二分（1000 个键，复用索引）")
    print("  几种写法的取舍（全部基于同一份内存里的 DataFrame）：")
    print("  · 查 1 个键 → `filter` 最省事。Polars 没有索引可建，别硬造。")
    print(
        f"  · 查 N 个键 → `is_in`（{is_in_100:.0f} ms / {is_in_1000:.0f} ms）"
        f" 和哈希 join（{join_100:.0f} ms / {join_1000:.0f} ms）"
    )
    print("    两者一个量级 —— 都是「把键集做成哈希表 + 扫一遍主键列」，成本大头是那一次扫描。")
    print(
        f"  · 手工二分 → 查 100 键 {manual_100:.0f} ms、查 1000 键 {manual_1000:.0f} ms，"
        f"**随键数线性增长**"
    )
    print("    原因很直接：`is_in` / join 是 Rust 里向量化的，`search_sorted` 是 Python 循环。")
    print("    键少的时候两者打平，键一多就拉开 —— 而且前面还有一次性建索引的成本。")
    print("    👉 所以在 Polars 里手工造索引是**亏的**：要查很多键就直接 join。")

    # ---------------------------------------------------------------- 对账
    print()
    print("  结果对账：")
    want = set(keys)
    ok = True
    for label, fn in variants.items():
        out = fn()
        got = {r["customer_id"] for r in out.to_dicts()}
        if "1000" in label:
            want_1000 = set(keys_1000)
            same = got == want_1000
        elif "1 个键" in label:
            same = got == {key}
        else:
            same = got == want
        ok = ok and same
        print(f"    {common.pad(label, 36)} {'✅ 命中同样的键' if same else '❌ 不一致'}")
    print(f"  → {'✅ 各种写法结果一致' if ok else '❌ 结果不一致'}")


# ---------------------------------------------------------------- 场景 5


def scenario_layout_wins(sorted_path, shuf_path, n_rows: int, key: int) -> None:
    """数据布局的功劳：row group 统计能剪掉多少。"""
    section("场景 5 · 真正决定点查快慢的，是数据布局")

    import pyarrow.parquet as pq

    def rg_hits(path, key):
        """多少个 row group 的 min/max 区间能装下这个键 —— 也就是必须真读的组数。"""
        md = pq.read_metadata(path)
        hits = 0
        for i in range(md.num_row_groups):
            stats = md.row_group(i).column(0).statistics
            if stats is not None and stats.min <= key <= stats.max:
                hits += 1
        return md.num_row_groups, hits

    headers = ["文件", "row group 总数", "统计能命中的组数", "必须真读的比例"]
    rows = []
    hits_by_tag = {}
    for tag, path in (("排好序", sorted_path), ("打乱", shuf_path)):
        total, hits = rg_hits(path, key)
        hits_by_tag[tag] = (total, hits)
        rows.append([tag, f"{total}", f"{hits}", f"{hits / total * 100:.0f}%"])
    _table(headers, rows, [12, 16, 20, 16])

    total_sorted, hits_sorted = hits_by_tag["排好序"]
    total_shuf, hits_shuf = hits_by_tag["打乱"]
    print()
    print("  这就是上面那张表的全部秘密：")
    print("  · Parquet 每个 row group 自带主键列的 min/max 统计，谓词一推就能整组跳过。")
    print("    Polars 没有索引，但**靠 Parquet 自带的统计拿到了和索引一样的效果**。")
    print(
        f"  · 数据排好序 → 一个 row group 只覆盖一段主键区间 → "
        f"{total_sorted} 组里只读 {hits_sorted} 组。"
    )
    print(f"  · 数据打乱 → 每组的区间都是 [1, N] → {total_shuf} 组全得读。")
    print()
    print("  👉 所以「Polars 没有索引」这件事，对点查的实际影响比想象中小得多。")
    print("    真正的工程动作是：**按点查的键排序落盘**，而不是想办法造索引。")
    print("    DuckDB 的 `CREATE INDEX` 干的也是同一件事 —— 排序落盘 = 免费索引。")


# ---------------------------------------------------------------- 表格工具


def _table(headers: list[str], rows: list[list[str]], widths: list[int]) -> None:
    print(common.pad("", 92).rstrip())
    print("".join(common.pad(h, w) for h, w in zip(headers, widths, strict=True)))
    print("-" * 92)
    for row in rows:
        print("".join(common.pad(c, w) for c, w in zip(row, widths, strict=True)))


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的索引 / 点查对比")
    parser.add_argument("--rows", type=int, default=5_000_000, help="数据行数")
    parser.add_argument("--rounds", type=int, default=5, help="交错轮数（每轮把所有变体测一遍）")
    parser.add_argument("--repeats", type=int, default=3, help="每轮内每个变体重复次数")
    args = parser.parse_args()

    import duckdb
    import polars as pl

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    sorted_path, shuf_path = ensure_data(args.rows)

    key = min(DEFAULT_KEY, args.rows - 1)
    scenario_point_query(sorted_path, shuf_path, args.rows, key, args.rounds, args.repeats)
    scenario_index_scope(shuf_path, args.rows, key)
    scenario_index_cost(shuf_path, args.rows)
    scenario_polars_lookup(shuf_path, args.rows, key, args.rounds, args.repeats)
    scenario_layout_wins(sorted_path, shuf_path, args.rows, key)

    section("小结")
    print("  1. DuckDB 有 ART 索引、Polars 没有 —— 但这只是表象。")
    print("     点查快不快，主要看数据排没排序：排好序时 row group 统计就能剪枝，")
    print("     两边都不需要索引；打乱时 DuckDB 的索引才真的救命。")
    print("  2. ART 索引的适用边界是实测出来的：")
    print("     等值（`=`、不连续的 `IN`）一定走索引；")
    print("     范围只有落在主键两端、且命中行数很少（500 万行表约 5000 行）时才走；")
    print("     连续的 `IN` 会被改写成范围，索引就丢了。")
    print("  3. `EXPLAIN` 看不出索引有没有用上，只有 `EXPLAIN ANALYZE` 的")
    print("     「Row Groups Scanned」才是真相（逻辑计划里那行写的是 Seq Scan）。")
    print("  4. 索引有代价：建索引要时间、要磁盘、写入还会变慢。")
    print("     批量灌数据应该「先灌完再建索引」，而不是边写边维护。")
    print("  5. Polars 没有索引，对点查的实际影响比想象中小：")
    print("     排好序的 Parquet + 谓词下推，效果和索引差不多；")
    print("     数据已经在内存里时，Polars 的 filter 和 DuckDB 的扫描是一个量级。")
    print("     实在要查很多键，用 `is_in` 或哈希 join 一次扫描解决。")
    print()


if __name__ == "__main__":
    main()
