"""调引擎与改配置：CALL、表函数、会话设置 —— DuckDB 是连接级的，Polars 是进程级的。

`object_demo.py` 讲了「CREATE 出来的东西」，这一层是**引擎本身怎么被调用、怎么被调**：

    DuckDB  FROM range(10)      表函数当数据源
            CALL pragma_version() 表函数当语句
            SET threads=2        连接级配置，170 个开关

    Polars  pl.select(pl.int_range(10))
            pl.sql("select ...")  自带的 SQL 引擎
            POLARS_MAX_THREADS=2  进程级配置，环境变量

三个必须记住的实测结论：

> **① 表函数是「零行输入的表」—— 不建表也能产生数据。**
>
> `FROM range(3)`、`FROM generate_series(1,3)`、`FROM unnest([1,2])`、
> `FROM glob('*.csv')` 都是表函数：`CALL` 和 `FROM` 是同一个函数的两种位置。

> **② Polars 也有 SQL，但不是 DuckDB 的 SQL。**
>
> `pl.sql()` / `pl.SQLContext` 里 `create table` 是**真建表**（`show tables` 能查到），
> 但全局 `pl.sql()` **每次都是新上下文** —— 上一步建的表，下一步查不到。
> 而且方言不同：`insert` 不支持（"statement type is not supported"），
> `current_setting()` 不支持（"unsupported function"）。

> **③ 配置的生效范围不一样：DuckDB 是连接级，Polars 是进程级。**
>
> DuckDB `SET threads=2` 只影响**这个连接**（另一个连接还是默认值），
> `RESET` 能还原，`current_setting()` 能读回来 —— 170 个设置都能查。
> Polars 的执行配置是**环境变量**，且**必须在 import polars 之前设**：
> 运行中改 `os.environ`，`pl.thread_pool_size()` 纹丝不动。

用法：
    uv run python src/config_demo.py
    uv run python src/config_demo.py --rows 200000 --repeat 3
"""

from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


# ---------------------------------------------------------------- 场景 1：表函数


def scenario_table_functions(con: duckdb.DuckDBPyConnection, work: Path) -> None:
    section("场景 1 · 表函数：不建表也能产生数据")

    (work / "a.csv").write_text("x,y\n1,2\n3,4\n")
    (work / "b.csv").write_text("x,y\n5,6\n")
    # 先建一张表，好让 duckdb_tables() 这个表函数有东西可列
    con.execute("CREATE TABLE sample (x INTEGER, y INTEGER)")
    con.execute("INSERT INTO sample VALUES (1, 2), (3, 4)")

    print("  DuckDB 的表函数 —— 直接放在 FROM 后面：")
    print()
    cases: list[tuple[str, str]] = [
        ("range(3)", "0,1,2"),
        ("generate_series(1, 5, 2)", "1,3,5"),
        ("unnest([1, 2])", "1,2"),
        ("query('select 42 as z')", "42"),
        ("glob('*.csv')", "文件名列表"),
        ("read_csv('a.csv')", "读文件"),
        ("duckdb_tables()", "catalog 里的表"),
    ]
    for expr, desc in cases:
        sql = expr.replace("'*.csv'", f"'{work}/*.csv'").replace("'a.csv'", f"'{work}/a.csv'")
        try:
            rows = con.execute(f"SELECT * FROM {sql}").fetchall()
            shown = str(rows)
            if len(shown) > 46:
                shown = shown[:43] + "..."
        except Exception as exc:
            shown = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        print("    " + common.pad(expr, 26) + common.pad(desc, 16) + "-> " + shown)
    print()
    print("    → 这些是**表函数**：不需要先建表，输入零行，直接产生一张表。")
    print("      `glob()` 给文件列表、`read_csv()` 直接读文件、`duckdb_tables()` 给 catalog。")
    print()

    print("  Polars 侧对应的写法（不是表函数，是表达式 / 函数）：")
    print()
    pol_cases: list[tuple[str, object]] = [
        ("pl.select(pl.int_range(3))", lambda: pl.select(pl.int_range(3)).to_series().to_list()),
        (
            "pl.col('a').explode()",
            lambda: pl.DataFrame({"a": [1, 2]}).select(pl.col("a").explode()).to_dicts(),
        ),
        ("pl.scan_csv('a.csv').collect()", lambda: pl.scan_csv(work / "a.csv").collect().shape),
    ]
    for label, fn in pol_cases:
        try:
            print(f"    {label:<34} -> {str(fn())}")
        except Exception as exc:
            print(f"    {label:<34} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()
    print("    → 功能上大体对得上（造序列 / 拆数组 / 读文件），")
    print("      但**形态不一样**：DuckDB 的表函数能进 FROM、能被规划器当成一张表；")
    print("      Polars 侧是表达式，不能出现在 FROM 位置。")
    print()
    print(
        f"    pl.glob 存在吗：{hasattr(pl, 'glob')}"
        "（DuckDB 有 `FROM glob('*.csv')`，Polars 侧没有同名函数）"
    )


# ---------------------------------------------------------------- 场景 2：CALL


def scenario_call(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · CALL：同一个表函数，放到语句位置")

    print("  表函数在 `FROM` 后面是「当表用」，在 `CALL` 后面是「当语句执行」：")
    print()
    for stmt in (
        "CALL range(3)",
        "CALL pragma_version()",
        "CALL unnest([1, 2])",
        "CALL duckdb_tables()",
    ):
        try:
            rows = con.execute(stmt).fetchall()
            shown = str(rows)
            if len(shown) > 58:
                shown = shown[:55] + "..."
            print(f"    {stmt:<28} -> {shown}")
        except Exception as exc:
            print(f"    {stmt:<28} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()

    print("  不存在的话报什么：")
    for stmt in ("CALL dbstat('t')", "CALL no_such_function()", "CALL pragma_database_list()"):
        try:
            rows = con.execute(stmt).fetchall()
            print(f"    {stmt:<32} -> 允许（{len(rows)} 行）")
        except Exception as exc:
            print(f"    {stmt:<32} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()
    print("    → 报的是 **CatalogException**，说的是「表函数不存在」——")
    print("      不是「语法错误」。说明 CALL 走的是 catalog 解析，跟 FROM 位置是同一条路。")
    print()

    print("  Polars 侧：没有 `CALL` 这个语句。")
    call_hits = [a for a in dir(pl) if a.lower() == "call"]
    print(f"    pl 里含 'call' 的顶层符号：{call_hits or '（一个都没有）'}")
    print("    → Polars 没有「语句位置调表函数」这个概念，")
    print("      能调的东西都是 Python 函数（`pl.scan_csv` / `pl.read_parquet` / ...）。")


# ---------------------------------------------------------------- 场景 3：Polars 的 SQL


def scenario_polars_sql() -> None:
    section("场景 3 · Polars 自带的 SQL：能写，但不是 DuckDB 的 SQL")

    print("  `pl.SQLContext` —— 表**真建得出来**：")
    print()
    ctx = pl.SQLContext()
    ctx.register("df", pl.DataFrame({"a": [1, 2], "b": [3, 4]}))
    created = ctx.execute("create table t as select 1 as x").collect().to_dicts()
    shown = ctx.execute("show tables").collect().to_dicts()
    rows_t = ctx.execute("select * from t").collect().to_dicts()
    print(f"    ctx.execute('create table t as select 1 as x') -> {created}")
    print(f"    ctx.execute('show tables')                     -> {shown}")
    print(f"    ctx.execute('select * from t')                 -> {rows_t}")
    print()
    print("    → 表建出来了，`show tables` 能查到 —— 这不是「回了个字符串」而已。")
    print()

    print("  但全局 `pl.sql()` **每次都是新上下文**：")
    print()
    pl.sql("create table g as select 1 as x").collect()
    try:
        pl.sql("select * from g").collect()
        print("    上一步建的表，下一步查得到")
    except Exception as exc:
        print("    pl.sql('create table g ...') 之后再 pl.sql('select * from g')")
        print(f"      -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("    → 全局的 `pl.sql()` 是**一次性**的：每次调用都新建一个空上下文。")
    print("      想让表活下来，必须自己攥着一个 `SQLContext` 对象。")
    print()

    print("  方言也不一样 —— 逐个试 DuckDB 常见的写法：")
    print()
    for stmt in (
        "select 42",
        "insert into t values (2)",
        "drop table t",
        "show tables",
        "select current_setting('threads')",
        "pragma version",
        "describe select 1",
    ):
        try:
            rows = pl.sql(stmt).collect().to_dicts()
            print(f"    {stmt:<38} -> OK {str(rows)[:40]}")
        except Exception as exc:
            print(f"    {stmt:<38} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print()
    print("    → `insert` 不支持、`current_setting` 不支持、`pragma` 不认；")
    print("      `describe select 1` 给的是**逻辑计划**，不是字段描述。")
    print("      所以「Polars 有 SQL」这句话只在「能跑 SELECT」这个层面成立。")
    print()

    print("  跟 DuckDB 的差别摆一起：")
    print()
    rows = [
        (
            "表存在哪",
            "catalog 里，`duckdb_tables()` 查得到",
            "`SQLContext` 对象里，`show tables` 查得到",
        ),
        ("生命周期", "跟着连接 / 跟着数据库文件", "跟着那个 Python 对象"),
        ("全局入口", "`con.sql()`，连接级", "`pl.sql()` 存在，但每次都是新上下文"),
        ("DML", "insert / update / delete 都支持", "只支持 select / create table"),
        ("系统函数", "170 个设置 + 几百个函数", "少很多，`current_setting` 就没有"),
    ]
    print("    " + common.pad("维度", 14) + common.pad("DuckDB", 40) + "Polars")
    print("    " + "-" * 74)
    for dim, duck, pol in rows:
        print("    " + common.pad(dim, 14) + common.pad(duck, 40) + pol)
    print()


# ---------------------------------------------------------------- 场景 4：配置


def scenario_config(con: duckdb.DuckDBPyConnection, rows: int, repeat: int) -> None:
    section("场景 4 · 配置：连接级 vs 进程级")

    total = con.execute("SELECT count(*) FROM duckdb_settings()").fetchall()[0][0]
    print(f"  DuckDB 一共有 **{total} 个设置**，全部能列出来：")
    print()
    for name in (
        "threads",
        "memory_limit",
        "preserve_insertion_order",
        "default_null_order",
        "TimeZone",
    ):
        row = con.execute(
            f"SELECT value, description FROM duckdb_settings() WHERE name = '{name}'"
        ).fetchall()
        if row:
            desc = str(row[0][1])
            print(f"    {common.pad(name, 24)}{common.pad(str(row[0][0]), 18)}{desc[:32]}")
    print()

    print("  `SET` 是**连接级**的：")
    print()
    con.execute("SET threads=2")
    mine = con.execute("SELECT current_setting('threads')").fetchall()[0][0]
    other_con = duckdb.connect()
    theirs = other_con.execute("SELECT current_setting('threads')").fetchall()[0][0]
    other_con.close()
    print(f"    本连接 SET threads=2 之后 current_setting('threads') -> {mine}")
    print(f"    另一个连接读到的是                                  -> {theirs}")
    print("    → 另一个连接**不受影响** —— 配置跟着连接走，不跟着进程走。")
    print()
    con.execute("RESET threads")
    restored = con.execute("SELECT current_setting('threads')").fetchall()[0][0]
    print(f"    RESET threads 之后 -> {restored}")
    print("    → 还能还原回默认值。")
    print()

    print("  但校验很松：")
    print()
    for stmt in ("SET threads=999", "SET threads=-5", "SET no_such_setting=1"):
        try:
            con.execute(stmt)
            got = con.execute("SELECT current_setting('threads')").fetchall()[0][0]
            print(f"    {stmt:<26} -> 接受，current_setting 现在是 {got}")
        except Exception as exc:
            print(f"    {stmt:<26} -> {type(exc).__name__}: {str(exc).splitlines()[0]}")
    con.execute("RESET threads")
    print()
    print("    → 不认识的设置会报错（CatalogException），")
    print("      但**上界不校验**：`threads=999` 照单全收，当前设置里就写着 999；")
    print('      只有负数这种明显离谱的才被拦（"Must have at least 1 thread!"）。')
    print()

    print("  Polars 侧：执行配置是**进程启动时**的环境变量")
    print()
    before = pl.thread_pool_size()
    os.environ["POLARS_MAX_THREADS"] = "2"
    after = pl.thread_pool_size()
    print(f"    改之前 pl.thread_pool_size() -> {before}")
    print("    运行中改 os.environ['POLARS_MAX_THREADS'] = '2'")
    print(f"    改之后 pl.thread_pool_size() -> {after}")
    del os.environ["POLARS_MAX_THREADS"]
    print("    → **纹丝不动**。线程池在 import polars 的时候就定好了。")
    print()

    env = {**os.environ, "POLARS_MAX_THREADS": "2"}
    proc = subprocess.run(
        [sys.executable, "-c", "import polars as pl; print(pl.thread_pool_size())"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    child_out = proc.stdout.strip() or proc.stderr.strip()[:60]
    print(f"    换个进程、启动时就设上：子进程输出 -> {child_out}")
    print("    → **启动前设才有效**。这是两种完全不同的配置模型：")
    print("      DuckDB 连接级（运行时可改、可读回、可还原），")
    print("      Polars 进程级（启动时定死，运行时改不动，也读不回配置本身）。")
    print()

    print("  Polars 的 `pl.Config` 是什么？")
    print()
    print(f"    {pl.Config.__doc__.strip().splitlines()[0] if pl.Config.__doc__ else ''}")
    names = [a for a in dir(pl.Config) if a.startswith("set_")]
    print(f"    它有 {len(names)} 个 setter，全是这一类：")
    print(f"      {', '.join(names[:10])}")
    print(f"      {', '.join(names[10:])}")
    print()
    print("    → 全是**表格打印格式**（列宽、小数位、对齐方式……），")
    print("      跟执行性能没关系。真正管执行的那个（线程数）藏在环境变量里，")
    print("      而且没有对应的「读回当前配置」接口 ——")
    print(f"      唯一的窗口是 `pl.thread_pool_size()`（现在 {pl.thread_pool_size()}）。")
    print()
    print("    pl 里有 current_setting / pragma / set_config 吗：")
    for name in ("current_setting", "pragma", "set_config"):
        print(f"      pl.{name:<16} -> {hasattr(pl, name)}")
    print()

    print("  设置 threads 到底有没有用？拿一个能并行的聚合试试：")
    print()
    con.execute("CREATE TABLE cfg AS SELECT range AS x, range % 97 AS k FROM range(?)", [rows])

    sql = "SELECT k, count(*) FROM cfg GROUP BY k"

    def run_once() -> float:
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        return (time.perf_counter() - t0) * 1000

    # 预热：两个线程数各跑一次，编译计划不算进计时
    for th in (1, 8):
        con.execute(f"SET threads={th}")
        run_once()

    # **交错**地跑：固定「先 1 线程再 8 线程」的话，谁先跑谁要付「第一次」的
    # 冷启动代价，这份代价会被算进那一边。交错着跑，两边轮流当第一个，噪声摊平。
    one_times: list[float] = []
    eight_times: list[float] = []
    for _ in range(repeat):
        con.execute("SET threads=1")
        one_times.append(run_once())
        con.execute("SET threads=8")
        eight_times.append(run_once())

    one = statistics.median(one_times)
    eight = statistics.median(eight_times)
    one_min, eight_min = min(one_times), min(eight_times)
    con.execute("RESET threads")
    print(f"    {rows:,} 行 group by（{repeat} 轮，交错跑）：")
    print(f"      threads=1 -> 中位 {one:.2f} ms（最小 {one_min:.2f}）")
    print(f"      threads=8 -> 中位 {eight:.2f} ms（最小 {eight_min:.2f}）")
    print(f"      比值 {one / eight:.2f}×")
    if one / eight > 1.1:
        print("    → 设置是**真的生效**的，不是摆设。")
    else:
        print("    → 这个负载太小，**看不出差别** —— 开关生效与否得拿更重的查询验。")
    print("      DuckDB 把它做成了连接级的开关，")
    print("      Polars 那边的对应物在环境变量里、而且只能在启动时定。")


# ---------------------------------------------------------------- 场景 5：小结


def scenario_conclusion() -> None:
    section("小结：调引擎与改配置，谁有什么")

    rows = [
        ("表函数（FROM 位置）", "有：`range` / `generate_series` 等", "有对应函数，但不是表函数"),
        ("表函数（CALL 位置）", "有：`CALL range(3)`", "❌ 没有 `CALL` 概念"),
        ("自己写 SQL", "`con.sql(...)`，完整方言", "`pl.sql()`，**方言小得多**"),
        ("SQL 里建表", "建在 catalog 里，别的语句能看见", "`SQLContext` 里能建；全局建完就丢"),
        ("SQL 里 INSERT", "支持", '❌ "statement type is not supported"'),
        ("系统函数 / 设置", "几百个函数 + 170 个设置", "少很多，`current_setting` 就没有"),
        ("配置的作用域", "**连接级**：别的连接不受影响", "**进程级**：整个进程共享"),
        ("配置什么时候能改", "运行时随时改，`RESET` 能还原", "**只能启动时**（环境变量）"),
        ("配置能不能读回", "能：`current_setting()` 能读", "只有 `pl.thread_pool_size()`"),
    ]

    print("  " + common.pad("能力", 22) + common.pad("DuckDB", 40) + "Polars")
    print("  " + "-" * 74)
    for cap, duck, pol in rows:
        print("  " + common.pad(cap, 22) + common.pad(duck, 40) + pol)
    print()

    print("  三个必须记住的实测结论：")
    print()
    print("    ① **表函数是「零行输入的表」。**")
    print("       `FROM` 位置和 `CALL` 位置是同一个函数的两种用法，")
    print("       引擎报的是 CatalogException（表函数不存在），不是语法错误。")
    print()
    print("    ② **Polars 也有 SQL，但不是 DuckDB 的 SQL。**")
    print("       `SQLContext` 里 `create table` 是真建表；")
    print("       但全局 `pl.sql()` 每次都是新上下文，建完就丢；")
    print("       `insert` 不支持、`current_setting` 不支持。")
    print("       所以「Polars 支持 SQL」这句话，得看是哪个意思。")
    print()
    print("    ③ **配置的作用域和时机都不一样。**")
    print("       DuckDB：连接级，运行时可改、可读回、可 RESET，170 个都能查；")
    print("       Polars：进程级，环境变量，**启动时定死**，运行时改不动。")
    print("       这一条在「一个进程要跑多个不同配置的查询」时最要命 ——")
    print("       DuckDB 开两个连接就行，Polars 只能起两个进程。")
    print()
    print("  实务建议：")
    print("    · 造序列、拆数组、读文件 → DuckDB 写成 `FROM range(...)` 一行；")
    print("      Polars 侧写成表达式，注意它不能出现在 FROM 位置；")
    print("    · 要写 SQL 逻辑 → DuckDB 侧是完整方言；")
    print("      Polars 侧的 `pl.sql()` 只当「跑个 SELECT」用，别指望它有 catalog；")
    print("    · 要按连接调参 → DuckDB 的 `SET`；")
    print("      Polars 侧只能改环境变量**并重启进程** —— 这条要写进部署脚本里。")


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的引擎调用与配置差异演示")
    parser.add_argument("--rows", type=int, default=200_000, help="配置场景的行数")
    parser.add_argument("--repeat", type=int, default=3, help="配置场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"Python {sys.version.split()[0]}")
    print(f"机器：{common.describe_machine()}")

    work = Path(tempfile.mkdtemp(prefix="config_demo_"))
    print(f"实验目录：{work}")

    try:
        con = duckdb.connect()
        scenario_table_functions(con, work)
        scenario_call(con)
        scenario_polars_sql()
        scenario_config(con, args.rows, args.repeat)
        scenario_conclusion()
        con.close()
    finally:
        for path in sorted(work.rglob("*"), reverse=True):
            if path.is_file():
                os.unlink(path)
            elif path.is_dir():
                path.rmdir()
        work.rmdir()

    print()
    print("  （实验目录已清理）")


if __name__ == "__main__":
    main()
