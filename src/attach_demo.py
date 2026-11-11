"""ATTACH / 多库 / 跨库查询 + catalog 内省：DuckDB 有「数据库」概念，Polars 没有。

DuckDB 的一个 `.duckdb` 文件是一个**独立的数据库**，可以挂到另一个连接上：

    ATTACH 'sales.duckdb' AS sales;
    ATTACH 'orders.duckdb' AS orders_db;

挂上之后就能直接跨库 join，写法跟单库一模一样：

    SELECT s.name, o.amount
    FROM sales.customers s
    JOIN orders_db.orders o ON s.id = o.id;

而且 DuckDB 自带一整套 catalog 内省（`SHOW TABLES` / `INFORMATION_SCHEMA` /
`duckdb_tables()` / `duckdb_schemas()` / `duckdb_columns()` …）。

**Polars 完全没有这一层** —— 没有「数据库」这个概念，没有目录，
`SHOW TABLES` / `INFORMATION_SCHEMA` 一个都没有。
Polars 的做法是「一个目录 = 一张虚拟表」：

    pl.scan_parquet("data/customers/*.parquet")

这个 demo 分两半：
  · 前半：**多库** —— ATTACH、跨库 join、跨 schema 限定名、只读挂载；
  · 后半：**catalog 内省** —— DuckDB 的三层内省接口各能看到什么，
    以及 Polars 侧对应的东西是什么（或者压根没有）。

顺带纠正一个常见的误解：`ATTACH` **不能**挂 parquet 文件。
DuckDB 2.0 只认 duckdb / sqlite / postgres / mysql 这几种存储类型。

用法：
    uv run python src/attach_demo.py
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import duckdb
import polars as pl

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def build_databases(work: Path) -> dict[str, Path]:
    """造三个库：sales（带一个 prod schema）、orders_db、readonly_db。"""
    paths = {
        "sales": work / "sales.duckdb",
        "orders": work / "orders.duckdb",
        "readonly": work / "readonly.duckdb",
    }

    con = duckdb.connect(str(paths["sales"]))
    con.execute(
        "CREATE TABLE customers AS SELECT i AS id, '客户' || i AS name FROM range(100) t(i)"
    )
    con.execute("CREATE SCHEMA prod")
    con.execute("CREATE TABLE prod.orders AS SELECT i AS id, i * 10 AS amount FROM range(100) t(i)")
    con.execute("CREATE VIEW vip AS SELECT * FROM customers WHERE id < 10")
    con.close()

    con = duckdb.connect(str(paths["orders"]))
    con.execute("CREATE TABLE orders AS SELECT i AS id, i * 10 AS amount FROM range(100) t(i)")
    con.close()

    con = duckdb.connect(str(paths["readonly"]))
    con.execute("CREATE TABLE settings AS SELECT 'v1' AS version")
    con.close()

    return paths


# ---------------------------------------------------------------- 场景 1


def scenario_attach(paths: dict[str, Path]) -> None:
    section("场景 1 · ATTACH：把别的库挂进来")

    con = duckdb.connect()
    print("  造了三个库：sales.duckdb（含 prod schema 和一个视图）、orders.duckdb、readonly.duckdb")
    print()
    print("  在一个**空的**连接上把它们挂进来：")
    for alias, key in (("sales", "sales"), ("orders_db", "orders")):
        con.execute(f"ATTACH '{paths[key]}' AS {alias}")
        print(f"    ATTACH '{paths[key].name}' AS {alias}")

    print()
    print("  挂上之后 `duckdb_databases()` 长这样：")
    print()
    print(
        "  "
        + common.pad("库名", 14)
        + common.pad("路径", 26)
        + common.pad("类型", 10)
        + common.pad("只读", 8)
        + "internal"
    )
    print("  " + "-" * 74)
    for name, path, dtype, ro, internal in con.execute(
        "SELECT database_name, path, type, readonly, internal "
        "FROM duckdb_databases() ORDER BY database_name"
    ).fetchall():
        shown = Path(path).name if path else "(内存 / 无文件)"
        print(
            "  "
            + common.pad(name, 14)
            + common.pad(shown, 26)
            + common.pad(dtype, 10)
            + common.pad("是" if ro else "否", 8)
            + ("是" if internal else "否")
        )

    print()
    print("  → `system` / `temp` 是 DuckDB 自带的（internal = 是），`memory` 是内存库。")
    print("    自己挂上来的 sales / orders_db 是 external 的（internal = 否）。")

    con.close()


# ---------------------------------------------------------------- 场景 2


def scenario_cross_db(paths: dict[str, Path]) -> None:
    section("场景 2 · 跨库查询：三段式限定名")

    con = duckdb.connect()
    con.execute(f"ATTACH '{paths['sales']}' AS sales")
    con.execute(f"ATTACH '{paths['orders']}' AS orders_db")

    print("  跨库 join —— 写起来跟单库没区别，只是表名多了个库前缀：")
    print()
    rows = con.execute(
        "SELECT s.name, o.amount "
        "FROM sales.customers s "
        "JOIN orders_db.orders o ON s.id = o.id "
        "ORDER BY s.id LIMIT 5"
    ).fetchall()
    print("    SELECT s.name, o.amount")
    print("    FROM sales.customers s")
    print("    JOIN orders_db.orders o ON s.id = o.id")
    print()
    print("    结果：")
    for r in rows:
        print(f"      {r}")

    n = con.execute(
        "SELECT count(*) FROM sales.customers s JOIN orders_db.orders o ON s.id = o.id"
    ).fetchone()[0]
    print()
    print(f"    两库各 100 行，按 id 对上 → {n} 行。")

    print()
    print("  跨 schema 也能写 —— `库.模式.表` 三段全上：")
    for q, label in [
        ("SELECT count(*) FROM sales.customers", "库.表（sales.main.customers）"),
        ("SELECT count(*) FROM sales.prod.orders", "库.模式.表（sales.prod.orders）"),
        ("SELECT count(*) FROM sales.vip", "库.视图（sales.vip）"),
    ]:
        n = con.execute(q).fetchone()[0]
        print(f"    {label:38s} → {n} 行")

    print()
    print("  → **限定名跟单库一模一样**，只是多了一段前缀 ——")
    print("    迁移的时候不需要为「多库」改任何查询写法。")

    con.close()


# ---------------------------------------------------------------- 场景 3


def scenario_attach_options(paths: dict[str, Path]) -> None:
    section("场景 3 · ATTACH 的选项：READ_ONLY、TYPE")

    con = duckdb.connect()

    print("  选项一：READ_ONLY")
    con.execute(f"ATTACH '{paths['readonly']}' AS ro (READ_ONLY)")
    ro = con.execute(
        "SELECT database_name, readonly FROM duckdb_databases() WHERE database_name = 'ro'"
    ).fetchone()
    print(f"    ATTACH ... (READ_ONLY) → duckdb_databases() 里 readonly = {ro[1]}")
    try:
        con.execute("INSERT INTO ro.settings VALUES ('v2')")
        print("    往只读库里写 → 居然成功了？")
    except Exception as exc:
        msg = str(exc).splitlines()[0]
        if len(msg) > 118:
            msg = msg[:118] + "…"
        print(f"    往只读库里写 → {type(exc).__name__}: {msg}")
    print()
    print("  → 只读是**引擎层**保证的：不是「你小心点别写」，是写不了。")

    print()
    print("  选项二：TYPE —— 挂什么类型的存储")
    print()
    print("  " + common.pad("TYPE", 12) + common.pad("结果", 12) + "说明")
    print("  " + "-" * 74)
    for t, note in [
        ("duckdb", "DuckDB 自己的文件（.duckdb）"),
        ("sqlite", "SQLite 文件"),
        ("postgres", "需要真实的连接串（本机没连）"),
        ("mysql", "需要真实的连接串（本机没连）"),
        ("parquet", "**不支持**"),
        ("csv", "需要 csv 扩展（下载被挡，见 fts_demo）"),
    ]:
        # ATTACH 一个不存在的 duckdb/sqlite 文件**会把它创建出来** ——
        # 所以探测路径必须落在临时目录里，不能用相对路径把文件丢在仓库根目录。
        probe_path = paths["sales"].parent / f"probe_{t}"
        probe = duckdb.connect()
        try:
            probe.execute(f"ATTACH '{probe_path}' AS p_{t} (TYPE {t})")
            verdict = "✅ 接受"
        except Exception as exc:
            msg = str(exc)
            if "Unrecognized storage type" in msg:
                verdict = "❌ 不支持"
            elif "Extension" in msg:
                verdict = "⚠️ 要扩展"
            else:
                verdict = "⚠️ 连不上"
        finally:
            probe.close()
            if probe_path.exists():
                probe_path.unlink()
        print("  " + common.pad(t, 12) + common.pad(verdict, 12) + note)

    print()
    print("  → **`ATTACH` 不能挂 parquet。**")
    print("    parquet 在 DuckDB 里是「读的时候的格式」，不是「一个库」——")
    print("    要读 parquet 直接 `read_parquet('x.parquet')` 就行（见 `parquet_meta_demo.py`）。")
    print("    同一个文件也不能挂两次（见下一节）。")

    con.close()


# ---------------------------------------------------------------- 场景 4


def scenario_attach_twice(paths: dict[str, Path]) -> None:
    section("场景 4 · 同一个文件不能 ATTACH 两次")

    con = duckdb.connect()
    con.execute(f"ATTACH '{paths['sales']}' AS sales")
    print("  第一次 ATTACH 成功（别名 sales）。")
    print()
    for alias in ("warehouse", "sales"):
        try:
            con.execute(f"ATTACH '{paths['sales']}' AS {alias}")
            print(f"  再 ATTACH 一次（别名 {alias}）→ ✅ 居然成功了")
        except Exception as exc:
            print(f"  再 ATTACH 一次（别名 {alias}）→ {type(exc).__name__}:")
            print(f"    {str(exc).splitlines()[0]}")
    con.close()
    print()
    print("  → DuckDB 对同一个文件只保留**一个 handle**，别名换成什么都一样。")
    print("    所以「同一进程里同时挂一份读写、一份只读」这种写法是不行的。")
    print("    （另一个进程读同一个文件被挡，是同一个根因 —— 见 `concurrency_demo.py`。）")


# ---------------------------------------------------------------- 场景 5


def scenario_catalog(paths: dict[str, Path]) -> None:
    section("场景 5 · catalog 内省：三层接口，各能看到什么")

    con = duckdb.connect()
    con.execute(f"ATTACH '{paths['sales']}' AS sales")
    con.execute(f"ATTACH '{paths['orders']}' AS orders_db")

    print("  DuckDB 的内省接口分三层，一层比一层底层：")
    print()
    print("  ① SQL 标准写法")
    print()
    print("  " + common.pad("语句", 34) + common.pad("看到几行", 10) + "覆盖范围")
    print("  " + "-" * 74)
    for stmt, scope in [
        ("SHOW TABLES", "**当前 schema + 当前库的 main**（当前库是 memory，没建表 → 0 行）"),
        ("SHOW TABLES FROM sales", "指定库的**所有** schema（3 张表/视图）"),
        ("SHOW ALL TABLES", "**所有挂上来的库**（4 张表/视图）"),
        ("SHOW DATABASES", "所有 external 库（memory / orders_db / sales）"),
        ("DESCRIBE sales.customers", "指定表的三段式限定名"),
        ("DESCRIBE sales.prod.orders", "跨 schema 也能 DESCRIBE"),
    ]:
        rows = con.execute(stmt).fetchall()
        print("  " + common.pad(stmt, 34) + common.pad(str(len(rows)), 10) + scope)

    print()
    print("    `USE` 之后再看 `SHOW TABLES` —— 它的范围跟着「当前库 + 当前 schema」走：")
    for use_stmt in ("USE sales", "USE sales.prod", "USE orders_db", "USE memory"):
        con.execute(use_stmt)
        names = sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
        print(f"      {use_stmt:18s} → {len(names)} 行：{names}")
    con.execute("USE memory")
    print()
    print("    对照 `SHOW TABLES FROM sales`（指定库的**所有** schema）：")
    names = sorted(r[0] for r in con.execute("SHOW TABLES FROM sales").fetchall())
    print(f"      SHOW TABLES FROM sales → {len(names)} 行：{names}")
    print()
    print("    → `SHOW TABLES` = **当前 schema 的表 + 当前库的 main schema**")
    print("      （`USE sales.prod` 时看到的是 prod.orders **加上** main 的 customers / vip）")
    print("      `SHOW TABLES FROM <库>` 才是那个库的**所有** schema；")
    print("      `SHOW ALL TABLES` 才是**所有挂上来的库**。")

    print()
    print("  ② INFORMATION_SCHEMA（SQL 标准的那套）")
    print()
    for stmt, note in [
        ("INFORMATION_SCHEMA.tables", "表和视图"),
        ("INFORMATION_SCHEMA.columns", "列"),
        ("INFORMATION_SCHEMA.schemata", "模式"),
    ]:
        rows = con.execute(f"SELECT count(*) FROM {stmt}").fetchall()
        print(f"    {stmt:32s} → {rows[0][0]:>3} 行（{note}）")
    print()
    user_rows = con.execute(
        "SELECT table_catalog, table_schema, table_name FROM INFORMATION_SCHEMA.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        "ORDER BY 1, 2, 3"
    ).fetchall()
    print("    剔掉系统表之后，INFORMATION_SCHEMA.tables 里剩下：")
    for r in user_rows:
        print(f"      {r}")
    print()
    print("    → **table_catalog 里是各个库的名字** ——")
    print("      `INFORMATION_SCHEMA` **是跨库的**。")
    print()
    print("      这一条反直觉：Postgres 的 information_schema 只描述当前库，")
    print("      DuckDB 这里却是所有挂上来的库都进来了。")
    print("      从 Postgres 迁过来的人会以为「查 information_schema 就等于查当前库」，")
    print("      在 DuckDB 这里会**多看到一堆别的库的表** ——")
    print("      好消息是不会漏，坏消息是过滤条件得自己加。")

    print()
    print("  ③ duckdb_*() 表函数（DuckDB 自己的接口，这一层也跨库，还多一个 internal 标记）")
    print()
    print("  " + common.pad("表函数", 24) + common.pad("跨库吗", 10) + "能拿到什么")
    print("  " + "-" * 74)
    for fn, cross, what in [
        ("duckdb_databases()", "✅", "所有库 + 路径 + 只读标记 + internal 标记"),
        ("duckdb_schemas()", "✅", "库 + 模式（含 main / prod）+ internal 标记"),
        ("duckdb_tables()", "✅", "库 + 模式 + 表名 + 行数估计"),
        ("duckdb_columns()", "✅", "库 + 表 + 列名 + 序号 + 类型"),
        ("duckdb_views()", "✅", "库 + 视图 + 定义 SQL"),
        ("duckdb_indexes()", "✅", "库 + 索引（见 index_demo.py）"),
    ]:
        print("  " + common.pad(fn, 24) + common.pad(cross, 10) + what)

    print()
    print("  `duckdb_tables()` 的实际内容（注意：视图不在这里，在 duckdb_views()）：")
    print()
    print("  " + common.pad("库", 14) + common.pad("模式", 10) + common.pad("表", 14) + "行数估计")
    print("  " + "-" * 74)
    for db_name, schema, table, size in con.execute(
        "SELECT database_name, schema_name, table_name, estimated_size "
        "FROM duckdb_tables() ORDER BY 1, 2, 3"
    ).fetchall():
        print(
            "  "
            + common.pad(db_name, 14)
            + common.pad(schema, 10)
            + common.pad(table, 14)
            + str(size)
        )
    view_rows = con.execute(
        "SELECT database_name, schema_name, view_name FROM duckdb_views() "
        "WHERE database_name NOT IN ('system') ORDER BY 1, 2, 3"
    ).fetchall()
    for db_name, schema, view in view_rows:
        print(
            "  "
            + common.pad(db_name, 14)
            + common.pad(schema, 10)
            + common.pad(view, 14)
            + "（视图）"
        )

    print()
    print("  → **三层接口的覆盖面不一样，这是最容易踩的地方**：")
    print("    · `SHOW TABLES` 只看**当前 schema + 当前库的 main**；")
    print("      `SHOW TABLES FROM <库>` 是那个库的所有 schema；")
    print("      `SHOW ALL TABLES` 才跨库；")
    print("    · `INFORMATION_SCHEMA` **也是跨库的**（这点跟 Postgres 不一样）；")
    print("    · `duckdb_*()` 跨库，还多给一个 `internal` 标记")
    print("      —— 用它把 system / temp 这些内置库滤掉最方便。")
    pg_count = con.execute(
        "SELECT count(*) FROM duckdb_views() WHERE schema_name = 'pg_catalog'"
    ).fetchone()[0]
    print()
    print("  附：DuckDB 还带了一整套 **pg_catalog 兼容层**")
    print(f"    （`pg_catalog.pg_class` / `pg_tables` / `pg_attribute` … 共 {pg_count} 个视图），")
    print("    好让 Postgres 的工具能直接连上来 ——")
    print("    `information_schema` 本身也是拿这些视图拼出来的。")

    con.close()


# ---------------------------------------------------------------- 场景 6


def scenario_polars(work: Path) -> None:
    section("场景 6 · Polars 侧：没有「库」这个概念")

    print("  Polars 里找「数据库」这一层的东西，找到的是：")
    print()
    checks = [
        ("SHOW TABLES", hasattr(pl, "show_tables"), "要枚举库里有哪些表"),
        ("INFORMATION_SCHEMA", hasattr(pl, "information_schema"), "SQL 标准那套元数据"),
        ("SHOW DATABASES", hasattr(pl, "show_databases"), "要枚举有哪些库"),
        ("ATTACH", hasattr(pl, "attach"), "把别的库挂进来"),
    ]
    print("  " + common.pad("想要的东西", 22) + common.pad("Polars 有吗", 12) + "Polars 实际的做法")
    print("  " + "-" * 74)
    for name, has, _ in checks:
        alt = {
            "SHOW TABLES": "读目录：`scan_parquet('dir/*.parquet')`",
            "INFORMATION_SCHEMA": "读 schema：`read_parquet_schema()`",
            "SHOW DATABASES": "自己维护一份目录清单",
            "ATTACH": "没有等价物 —— 直接读多个路径",
        }[name]
        print("  " + common.pad(name, 22) + common.pad("✅ 有" if has else "❌ 没有", 12) + alt)

    print()
    print("  「describe」这个词在 Polars 里的命中：")
    print()
    names = [n for n in dir(pl) if "describe" in n.lower()]
    print(f"    polars **顶层**命名空间里带 describe 的：{names or '（一个都没有）'}")
    print("    `pl.describe` 不存在（AttributeError）。")
    print("    只有 `DataFrame.describe()` —— 那是**列的统计摘要**")
    print("    （列名 / dtype / 计数 / …），跟「列出库里有哪些表」完全是两回事。")
    print("    搜 'describe' 命中的是它，不是 catalog。")

    print()
    print("  Polars 的「库」实际长什么样：一个目录 = 一张虚拟表。")
    print()
    d = work / "customers"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        pl.DataFrame(
            {"id": range(i * 10, (i + 1) * 10), "name": [f"n{j}" for j in range(10)]}
        ).write_parquet(d / f"part{i}.parquet")
    lf = pl.scan_parquet(str(d / "*.parquet"))
    print(f"    目录里有 {len(list(d.glob('*.parquet')))} 个 parquet 文件")
    print(f"    scan_parquet('{d.name}/*.parquet') → 惰性帧，collect 出来 {lf.collect().height} 行")
    print()
    print("    这个东西扮演的就是「表」的角色：")
    print("      · 能查、能 join、能聚合；")
    print("      · schema 靠所有文件的 schema 推出来，不一致就报错；")
    print("      · **没有名字** —— 你只能靠路径指认它，")
    print("        所以 Polars 里没有「列出所有表」这种事可做。")

    print()
    print("  还有一个理论上能用的：`pl.read_database(query, connection)`，")
    print("  但它要靠 connectorx 去连真正的 SQL 数据库，")
    print("  跟 DuckDB 文件不是一回事（本机也没装 connectorx）。")
    try:
        import connectorx  # noqa: F401

        print("    （本机装了 connectorx）")
    except ImportError:
        print("    （本机没装 connectorx，实测调用会 ImportError）")


# ---------------------------------------------------------------- 场景 7


def scenario_conclusion() -> None:
    section("小结：多库与内省")

    print("  " + common.pad("维度", 24) + common.pad("DuckDB", 30) + "Polars")
    print("  " + "-" * 74)
    rows = [
        ("「数据库」这个概念", "有（一个 .duckdb 文件就是一个库）", "没有，只有文件和目录"),
        ("把别的库挂进来", "ATTACH 'x.duckdb' AS sales", "没有等价物"),
        ("跨库 join", "直接写 sales.t JOIN orders_db.u", "读多个路径后自己拼"),
        ("跨 schema 限定名", "库.模式.表 三段全支持", "没有模式的概念"),
        ("枚举有哪些表", "SHOW ALL TABLES / duckdb_tables()", "只能自己扫目录"),
        ("SQL 标准元数据", "INFORMATION_SCHEMA（跨库，跟 Postgres 不同）", "没有"),
        ("只读挂载", "ATTACH ... (READ_ONLY)，引擎层禁止写", "没有这层"),
        ("挂 parquet 当库", "❌ 不支持", "本来就是它唯一的输入格式"),
    ]
    for dim, duck, pol in rows:
        print("  " + common.pad(dim, 24) + common.pad(duck, 30) + pol)

    print()
    print("  → 一句话：**DuckDB 有「库」这个抽象层，Polars 只有「文件」。**")
    print()
    print("  这不是「谁更先进」的问题，是**任务形态不同**：")
    print("    · DuckDB 是数据库 —— 业务库要能被多个下游同时连、")
    print("      要能只读挂载、要能查元数据；")
    print("    · Polars 是文件处理器 —— 数据在 parquet 里，")
    print("      「目录 = 表」够用了，加一层「库」反而是负担。")
    print()
    print("  实务上的取舍：")
    print("    · 要「一个库多个下游读」→ DuckDB（但记住并发演示里的那条：")
    print("      **只读挂载的前提是没有别的进程正在写**）；")
    print("    · 要「一堆 parquet 上并行跑」→ Polars + 目录（`partition_demo.py`）；")
    print("    · 要「跨库 join」→ DuckDB 一次 ATTACH 搞定；")
    print("      Polars 侧就是读两个路径再 join，或者干脆让 DuckDB 去 join。")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="ATTACH / 多库 / 跨库查询 + catalog 内省")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器：{common.describe_machine()}")

    work = Path(tempfile.mkdtemp(prefix="attach_demo_"))
    print(f"实验目录：{work}")

    try:
        paths = build_databases(work)
        scenario_attach(paths)
        scenario_cross_db(paths)
        scenario_attach_options(paths)
        scenario_attach_twice(paths)
        scenario_catalog(paths)
        scenario_polars(work)
        scenario_conclusion()
    finally:
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
