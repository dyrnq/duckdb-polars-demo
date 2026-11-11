"""容错模式与坏数据：同一个脏 CSV，两个引擎读出两份不一样的结果。

要点：
* DuckDB：`read_csv(..., ignore_errors=true)` **整行跳过**坏行；
  `store_rejects=true` 把坏行收集到 `reject_errors` 表（行号、列名、原始行、错误类型）；
  `SET errors_as_json=true` 让**抛出来的**报错变成 JSON。
* Polars：`read_csv(..., ignore_errors=True)` **保留**坏行，把出错的字段置成 NULL；
  它只兜得住「字段解析失败」，字段数不对（多列/少列）、引号没闭合照样抛异常。

这个场景要回答：两个引擎的「容错开关」各自容的是什么错，
以及在脏数据上写出来的两份结果，什么时候会对不上、差多少。

用法：
    uv run python src/fault_demo.py
    uv run python src/fault_demo.py --rows 5000000 --repeat 5
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path

import duckdb
import polars as pl

import common

# 两边读同一份 schema —— 不显式给 schema 的话，auto-detect 会把整列降级成文本，
# 后面就比不出「谁跳行、谁置空」了。
DUCK_COLUMNS = "{'id':'INTEGER','name':'VARCHAR','amount':'INTEGER','note':'VARCHAR'}"
POLARS_SCHEMA = {
    "id": pl.Int64,
    "name": pl.String,
    "amount": pl.Int64,
    "note": pl.String,
}

# 字段整齐，只有一处「类型不对」—— 专门用来比两边的容错语义
ORDERS_CSV = """id,name,amount,note
1,alice,100,ok
2,bob,N/A,金额不是数字
3,carol,300,ok
4,dave,400,ok
5,eve,500,ok
6,frank,600,ok
7,grace,700,ok
8,heidi,800,ok
"""

# 字段数不对 + 空值 + 引号没闭合
BROKEN_CSV = """id,name,amount,note
1,alice,100,ok
2,bob,200,ok,多了一个字段
3,carol,300,ok
4,dave,,金额是空值
5,eve,500
6,frank,600,"引号没闭合
"""


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def q1(con: duckdb.DuckDBPyConnection, sql: str) -> object:
    """跑一条查询取第一行的第一列（Relation 上没有 fetchone）。"""
    rows = con.sql(sql).fetchall()
    return rows[0][0] if rows else None


def duck_csv(path: Path, extra: str = "") -> str:
    """拼一条 read_csv 的 SQL：显式 schema，不让嗅探器插手。"""
    return f"read_csv('{path}', columns={DUCK_COLUMNS}, header=true, auto_detect=false{extra})"


def duck_csv_auto(path: Path) -> str:
    """不带 schema 的 read_csv —— 让嗅探器自己定型。"""
    return f"read_csv('{path}')"


def show_csv(path: Path, marks: dict[int, str]) -> None:
    """把 CSV 按行号打出来，坏的那几行带标注。"""
    print(f"  {path.name}：")
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        note = marks.get(i, "")
        print(f"    行{i:<3}{line}" + (f"   ← {note}" if note else ""))


def exc_head(exc: Exception, n: int = 6) -> list[str]:
    return str(exc).splitlines()[:n]


# ---------------------------------------------------------------- 1. 脏数据


def scenario_bad_data(orders: Path, broken: Path) -> None:
    section("场景 1 · 一份脏 CSV 长什么样：四种坏法")

    show_csv(orders, {3: "amount 不是数字"})
    print()
    show_csv(
        broken,
        {
            3: "多了一个字段",
            5: "amount 是空值",
            6: "少了两个字段",
            7: "引号没闭合",
        },
    )

    print("\n→ 先把「坏」拆开看，后面每个引擎在这四种坏法上表现都不一样：")
    print("  · **类型不对**（amount = N/A）：字段能解析，类型转不过去。")
    print("  · **空值**（amount 是空的）：两边都读成 NULL，这不算错。")
    print("  · **字段数不对**（多一列 / 少一列）：行本身的结构就不合法。")
    print("  · **引号没闭合**：解析器在行中间就乱了，后面的行会被卷进去。")
    print("  重点是：**这四种错误，容错开关不是一把统一的钥匙。**")


# ---------------------------------------------------------------- 2. 嗅探


def scenario_auto_detect(con: duckdb.DuckDBPyConnection, orders: Path) -> None:
    section("场景 2 · 不给 schema：两边都「悄悄降级」，谁都不报错")

    print("  DuckDB sniff_csv 给出的列类型：")
    cols = q1(con, f"SELECT Columns FROM sniff_csv('{orders}')")
    for col in cols:
        print(f"    {col['name']:<8}{col['type']}")

    duck_type = q1(con, f"SELECT typeof(amount) FROM {duck_csv_auto(orders)} LIMIT 1")
    pol_type = pl.read_csv(orders).schema["amount"]
    print(f"\n  DuckDB 读出来的 amount 类型 -> {duck_type}")
    print(f"  Polars 读出来的 amount 类型 -> {pol_type}")

    print("\n  两边都把 amount 降级成了文本 —— 文件里那个 N/A 让整列都不再是整数，")
    print("  但两个引擎都没报任何错。接下来要在这一列上做点事，才知道降级的代价：")
    try:
        q1(con, f"SELECT sum(amount) FROM {duck_csv_auto(orders)}")
    except Exception as exc:
        print(f"    DuckDB sum(amount) -> {type(exc).__name__}: {str(exc).splitlines()[0][:76]}")
    try:
        pl.read_csv(orders)["amount"].sum()
    except Exception as exc:
        print(f"    Polars  sum(amount) -> {type(exc).__name__}: {str(exc).splitlines()[0][:76]}")

    print("\n→ 这一条两边**一致**，但一致得很危险：")
    print("  · DuckDB 的嗅探器看到 N/A，Polars 的推断器看到 N/A，两边都把 amount 降级成文本。")
    print(
        "  · 谁都没报错，但这一列已经不是整数了 —— 下游的聚合会炸，而炸的是「用法」不是「数据」。"
    )
    print("  · 所以脏数据上正确的做法是**显式给 schema**，别让推断替你做决定。")
    print("    接下来的场景都显式给 schema。")


# ---------------------------------------------------------------- 3. 严格模式


def scenario_strict(con: duckdb.DuckDBPyConnection, orders: Path) -> None:
    section("场景 3 · 给了 schema：两边各报各的错")

    print("  DuckDB read_csv(columns={... 'amount':'INTEGER'})")
    try:
        con.sql(f"SELECT * FROM {duck_csv(orders)}").fetchall()
    except Exception as exc:
        print(f"    {type(exc).__name__}:")
        for line in exc_head(exc, 4):
            print(f"      {line}")

    print("\n  Polars read_csv(schema={'amount': pl.Int64})")
    try:
        pl.read_csv(orders, schema=POLARS_SCHEMA)
    except Exception as exc:
        print(f"    {type(exc).__name__}:")
        for line in exc_head(exc, 4):
            print(f"      {line}")

    print("\n→ 两边都是「整份文件读失败」，但报错的信息量不在一个级别：")
    print("  · DuckDB：`duckdb.ConversionException`，直接告诉你**第几行**、**原始行长什么样**。")
    print(
        "  · Polars：`polars.exceptions.ComputeError`，告诉你**哪一列**、**文件里的第几个字节**，"
    )
    print("    原始值也带上了，但行号要自己数。")
    print("  · 实务上的差别：一份几百万行的文件，DuckDB 的报错能直接定位到行；")
    print("    Polars 的报错只能定位到列，剩下的还得自己二分查找。")


# ---------------------------------------------------------------- 4. 容错读


def scenario_ignore_errors(con: duckdb.DuckDBPyConnection, orders: Path) -> None:
    section("场景 4 · ignore_errors：一边跳行，一边置空")

    duck_df = con.sql(f"SELECT * FROM {duck_csv(orders, ', ignore_errors=true')}").pl()
    pol_df = pl.read_csv(orders, schema=POLARS_SCHEMA, ignore_errors=True)

    common.print_frame(duck_df, "DuckDB read_csv(ignore_errors=true)")
    common.print_frame(pol_df, "Polars read_csv(ignore_errors=True)")

    duck_count = q1(con, f"SELECT count(*) FROM {duck_csv(orders, ', ignore_errors=true')}")
    physical = len(ORDERS_CSV.strip().splitlines()) - 1

    print("\n  " + common.pad("行数口径", 26) + common.pad("DuckDB", 24) + "Polars")
    print("  " + "-" * 74)
    rows = [
        ("文件里的物理行数", str(physical), str(physical)),
        ("引擎自己报的「行数」", f"count(*) → {duck_count}", f"height → {pol_df.height}"),
        ("你实际拿到的行数", str(duck_df.height), str(pol_df.height)),
        ("那一行坏数据", "整行丢掉", "保留，amount 变成 null"),
    ]
    for label, duck_val, pol_val in rows:
        print("  " + common.pad(label, 26) + common.pad(duck_val, 24) + pol_val)

    print("\n  顺带一个更坑的：DuckDB 侧的 count(*) 单独查，走的是 CSV 解析器的物理行数口径。")
    print(f"  上面那行 count(*) = {duck_count} 是「文件里有几行」，不是「你拿到了几行」。")
    print(f"  实测：SELECT * 只有 {duck_df.height} 行，count(*) 却报 {duck_count}。")
    print(
        "  （这条只对「字段解析成功、类型转换失败」的行成立 —— 场景 7 那种字段数不对的行不计入。）"
    )
    print("  换个查法就正常了 —— 加一个 WHERE 或者再加一个聚合函数：")
    where_count = q1(
        con,
        f"SELECT count(*) FROM {duck_csv(orders, ', ignore_errors=true')} WHERE amount IS NOT NULL",
    )
    both_row = con.sql(
        f"SELECT count(*), sum(amount) FROM {duck_csv(orders, ', ignore_errors=true')}"
    ).fetchall()[0]
    print(f"    SELECT count(*) FROM read_csv(...) WHERE amount IS NOT NULL -> {where_count}")
    print(f"    SELECT count(*), sum(amount) FROM read_csv(...) -> {both_row[0]}, {both_row[1]}")
    print("  （Polars 这边没有这个问题：df.height 就是真的行数。）")

    print("\n→ 三条要记住的：")
    print("  · **DuckDB 的 ignore_errors 是「丢整行」**：坏行连同它其他字段一起消失，")
    print(f"    实测 {physical} 行物理数据只剩 {duck_df.height} 行。")
    print("  · **Polars 的 ignore_errors 是「把坏字段置 NULL」**：行还在，")
    print(f"    实测还是 {pol_df.height} 行，只是第 2 行的 amount 变成了 null。")
    print(f"  · 同一个开关名，两边读出来的**行数不一样**（{duck_df.height} vs {pol_df.height}）——")
    print("    后面凡是数行数的地方，两边的答案直接就对不上，而且不报任何错。")


# ---------------------------------------------------------------- 5. 对账


def scenario_reconcile(con: duckdb.DuckDBPyConnection, orders: Path) -> None:
    section("场景 5 · 对账：Polars 补一刀，两边就对上了")

    duck_df = con.sql(f"SELECT * FROM {duck_csv(orders, ', ignore_errors=true')}").pl()
    pol_raw = pl.read_csv(orders, schema=POLARS_SCHEMA, ignore_errors=True)
    pol_fixed = pol_raw.filter(pl.col("amount").is_not_null())

    print(f"  DuckDB ignore_errors            -> {duck_df.height} 行")
    print(f"  Polars ignore_errors            -> {pol_raw.height} 行（第 2 行 amount = null）")
    print(f"  Polars ignore_errors + 丢空行   -> {pol_fixed.height} 行")

    ok, why = common.compare_frames(duck_df, pol_fixed, ["id"])
    print(f"\n  {'✅' if ok else '❌'} DuckDB vs Polars(补一刀)：{why}")

    print("\n→ 这个补丁能用，但有两个前提：")
    print("  · **这一列本来不能有合法的 NULL**。Polars 把「值写坏了」和「值本来就是空的」")
    print("    都变成 null，之后就分不出来了 —— 而 DuckDB 的 reject_errors 表能告诉你第几行坏了。")
    print("  · **补的这一刀是额外的开销**（场景 8 会量一下）。")
    print("  → 换句话说：Polars 的容错是「保数据」，DuckDB 的容错是「保数据 + 记账」。")
    print("    想要记账，就用 DuckDB 的 reject_errors；想要两边结果一样，就得自己补刀。")


# ---------------------------------------------------------------- 6. 坏行记录


def scenario_rejects(con: duckdb.DuckDBPyConnection, orders: Path, broken: Path) -> None:
    section("场景 6 · 坏行去哪了：DuckDB 的 store_rejects 与 reject_errors 表")

    con.execute("DROP TABLE IF EXISTS reject_errors")
    con.execute("DROP TABLE IF EXISTS reject_scans")

    duck_df = con.sql(f"SELECT * FROM {duck_csv(broken, ', store_rejects=true')}").pl()
    print(f"  read_csv(..., store_rejects=true) 读出 {duck_df.height} 行，")
    print("  坏行记在 reject_errors 表里：")

    rej = con.sql(
        "SELECT line, column_name, error_type, csv_line FROM reject_errors ORDER BY line"
    ).fetchall()
    print(
        "\n  "
        + common.pad("行号", 6)
        + common.pad("列名", 12)
        + common.pad("错误类型", 20)
        + "原始行"
    )
    print("  " + "-" * 74)
    for line, column_name, error_type, csv_line in rej:
        print(
            "  "
            + common.pad(str(line), 6)
            + common.pad(str(column_name), 12)
            + common.pad(str(error_type), 20)
            + str(csv_line).replace("\n", "\\n")
        )

    print("\n  reject_errors 表的列：")
    for row in con.sql("DESCRIBE reject_errors").fetchall():
        print(f"    {row[0]:<20}{row[1]}")

    etype = con.sql(
        "SELECT data_type FROM duckdb_columns() "
        "WHERE table_name='reject_errors' AND column_name='error_type'"
    ).fetchall()[0][0]
    print(f"\n  error_type 的全部取值（{etype}）：")
    values = con.sql(f"SELECT unnest(enum_range(NULL::{etype}))").fetchall()
    for i in range(0, len(values), 2):
        chunk = values[i : i + 2]
        print("    " + "  ".join(common.pad(v[0], 22) for v in chunk))

    print("\n  注意这个表是**累加**的 —— 在同一个连接里再读一次，它不会覆盖，只会往上加：")
    before = q1(con, "SELECT count(*) FROM reject_errors")
    con.sql(f"SELECT * FROM {duck_csv(broken, ', store_rejects=true')}").fetchall()
    after = q1(con, "SELECT count(*) FROM reject_errors")
    print(f"    读之前 {before} 行，再读一次之后 {after} 行")
    print("    → 想知道「这一批文件坏了多少行」，用 rejects_scan / scan_id 过滤，别直接 count。")

    print("\n  报错本身也可以变成 JSON（这是另一个开关）：")
    con.execute("SET errors_as_json=true")
    try:
        con.sql(f"SELECT * FROM {duck_csv(orders)}").fetchall()
    except Exception as exc:
        message = str(exc).replace("\n", " ")
        print(f"    {type(exc).__name__}: {message[:190]}")
    con.execute("SET errors_as_json=false")
    still_text = q1(con, "SELECT error_message FROM reject_errors LIMIT 1")
    print(f"    但 reject_errors.error_message 还是人读的文本：{str(still_text)[:70]}")

    print("\n→ 两条要记住的：")
    print("  · DuckDB 侧的参数叫 **`store_rejects`**（不是 store_errors），")
    print("    收坏行的表叫 **`reject_errors`**，里面记着行号、列名、原始行、错误类型。")
    print("  · `SET errors_as_json=true` 只改**抛出来的异常消息**的格式，")
    print("    不会改 reject_errors 表里的 error_message —— 想要机器可读，")
    print("    得自己解析 error_type 那一列（它是枚举，不是自由文本）。")


# ---------------------------------------------------------------- 7. 边界


def scenario_limits(con: duckdb.DuckDBPyConnection, broken: Path, tmpdir: Path) -> None:
    section("场景 7 · 容错的边界：字段数不对、引号没闭合、文件不存在")

    print("  同一个 broken.csv，字段数不对（多一列 / 少一列）+ 末行引号没闭合。")

    print("\n  DuckDB 严格模式：")
    try:
        con.sql(f"SELECT * FROM {duck_csv(broken)}").fetchall()
    except Exception as exc:
        for line in exc_head(exc, 5):
            print(f"    {line}")

    duck_df = con.sql(f"SELECT * FROM {duck_csv(broken, ', ignore_errors=true')}").pl()
    physical = len(BROKEN_CSV.strip().splitlines()) - 1
    dropped = physical - duck_df.height
    print(f"\n  DuckDB ignore_errors=true -> 读出 {duck_df.height} 行")
    print(f"  （{physical} 行里坏了 {dropped} 行，全丢）")
    for row in duck_df.to_dicts():
        print(f"    {row}")

    print("\n  Polars ignore_errors=True：")
    try:
        pl.read_csv(broken, schema=POLARS_SCHEMA, ignore_errors=True)
    except Exception as exc:
        for line in exc_head(exc, 3):
            print(f"    {line}")

    print("\n  按提示加上 truncate_ragged_lines=True，它又撞上引号没闭合：")
    try:
        pl.read_csv(broken, schema=POLARS_SCHEMA, truncate_ragged_lines=True, ignore_errors=True)
    except Exception as exc:
        for line in exc_head(exc, 3):
            print(f"    {line}")

    missing = tmpdir / "nope.csv"
    print("\n  文件根本不存在的时候：")
    try:
        con.sql(f"SELECT * FROM read_csv('{missing}')").fetchall()
    except Exception as exc:
        print(f"    DuckDB -> {type(exc).__name__}: {str(exc).splitlines()[0][:88]}")
    try:
        con.sql(f"SELECT * FROM read_csv('{missing}', ignore_errors=true)").fetchall()
    except Exception as exc:
        head = f"{type(exc).__name__}: {str(exc).splitlines()[0][:60]}"
        print(f"    DuckDB 加 ignore_errors 之后 -> {head}")
    try:
        pl.read_csv(missing)
    except Exception as exc:
        print(f"    Polars -> {type(exc).__name__}: {str(exc).splitlines()[0][:88]}")
    try:
        pl.read_csv(missing, ignore_errors=True)
    except Exception as exc:
        head = f"{type(exc).__name__}: {str(exc).splitlines()[0][:60]}"
        print(f"    Polars 加 ignore_errors 之后 -> {head}")

    print("\n→ 两条要记住的：")
    print("  · **Polars 的 ignore_errors 只兜得住「字段解析失败」**：字段数不对照样抛异常，")
    print("    而 truncate_ragged_lines 是**截断**（把多出来的值扔掉继续读），不是跳过这一行。")
    print("    引号没闭合更是解析器级别的错，两个开关都救不了。")
    print("  · **容错开关管的是「行内容」，管不了「文件找不到」**：")
    print("    DuckDB 报 IOException，Polars 报 FileNotFoundError，加 ignore_errors 都不变。")
    print("  · 实务上：脏数据能靠 ignore_errors 兜住，行格式不统一（列数不一致）得靠上游统一 ——")
    print("    DuckDB 的 reject_errors 会把 TOO MANY COLUMNS / MISSING COLUMNS 记给你，")
    print("    Polars 这边只能自己写前置校验。")


# ---------------------------------------------------------------- 8. 代价


def scenario_performance(
    con: duckdb.DuckDBPyConnection, tmpdir: Path, rows: int, repeat: int
) -> None:
    section("场景 8 · 代价：跳坏行 vs 全量读入")

    print(f"数据：{rows} 行 CSV，其中每 100 行有 1 行 amount 写成了 N/A")
    print(f"机器：{common.describe_machine()}")

    src = tmpdir / "big.csv"
    con.execute(
        "COPY (SELECT i AS id, 'name_' || i AS name, "
        "CASE WHEN i % 100 = 0 THEN 'N/A' ELSE CAST(i * 100 AS VARCHAR) END AS amount, "
        "'ok' AS note FROM range(?) t(i)) TO ? (FORMAT CSV, HEADER)",
        [rows, str(src)],
    )

    def duck_stream() -> tuple[int, int]:
        r = con.sql(
            f"SELECT count(*), sum(amount) FROM {duck_csv(src, ', ignore_errors=true')}"
        ).fetchall()[0]
        return int(r[0]), int(r[1])

    def duck_materialized() -> tuple[int, int]:
        con.execute(
            f"CREATE OR REPLACE TABLE perf AS SELECT * FROM {duck_csv(src, ', ignore_errors=true')}"
        )
        r = con.sql("SELECT count(*), sum(amount) FROM perf").fetchall()[0]
        return int(r[0]), int(r[1])

    def polars_tolerant() -> tuple[int, int]:
        df = pl.read_csv(src, schema=POLARS_SCHEMA, ignore_errors=True)
        return df.height, int(df["amount"].sum())

    def polars_tolerant_drop() -> tuple[int, int]:
        df = pl.read_csv(src, schema=POLARS_SCHEMA, ignore_errors=True)
        df = df.filter(pl.col("amount").is_not_null())
        return df.height, int(df["amount"].sum())

    results = [
        ("DuckDB 容错读（流式聚合）", duck_stream),
        ("DuckDB 容错读（CTAS 物化）", duck_materialized),
        ("Polars 容错读（read_csv 物化）", polars_tolerant),
        ("Polars 容错读 + 丢空行", polars_tolerant_drop),
    ]

    print(
        "\n  "
        + common.pad("实现", 30)
        + common.pad_left("中位数 (ms)", 12)
        + common.pad_left("最快 (ms)", 11)
        + common.pad_left("读出行数", 11)
        + common.pad_left("amount 求和", 20)
    )
    print("  " + "-" * 84)
    for label, fn in results:
        fn()
        times: list[float] = []
        value: tuple[int, int] = (0, 0)
        for _ in range(repeat):
            t0 = time.perf_counter()
            value = fn()
            times.append(time.perf_counter() - t0)
        med = statistics.median(times)
        fastest = min(times)
        print(
            "  "
            + common.pad(label, 30)
            + common.pad_left(f"{med * 1000:.1f}", 12)
            + common.pad_left(f"{fastest * 1000:.1f}", 11)
            + common.pad_left(f"{value[0]:,}", 11)
            + common.pad_left(f"{value[1]:,}", 20)
        )

    print("\n口径：")
    print("  · 两边读的是**同一个** CSV 文件（tmpdir 里现造的那一份）、同一份 schema、")
    print("    都把 amount 转成整数、都用容错模式、最后都做一次求和 —— 同一件事。")
    print("  · 计时的**包含读盘和解析**。文件在第一轮预热后已经在 OS 页缓存里，两边读同一份缓存。")
    print("  · **四行的执行模型不一样，绝对值不直接可比**，要比的是同组内部的差：")
    print("    · 第 1 行是 DuckDB 的「查询式」用法：read_csv 流式扫描 + 聚合，不物化整表。")
    print("    · 第 2 行是「把 CSV 导成表」的用法：CTAS 落成一张真表（行数从表上数，")
    print("      不会踩场景 4 里 count(*) 那个坑），再聚合。")
    print("    · 第 3、4 行是 Polars 的「读成 DataFrame」用法：eager 模式必然物化。")
    print("    · 第 1 行 vs 第 3 行：一个是流式聚合、一个是物化成 DataFrame 再算，")
    print("      **这两个数字不能直接比大小**；真正该比的是第 2 行 vs 第 3 行（都物化）。")
    print("    · 第 3 行 vs 第 4 行：多了一遍全列过滤 —— 输出跟 DuckDB 对齐要付的代价。")
    print("  · 第 1 行的 count(*) 是跟 sum(amount) 一起查的（单独查会走 CSV 的物理行数口径）。")
    print("  · 重复次数取中位数，机器是共享开发机，单次数字会有抖动。")
    print("\n解读：")
    print("  · **汇总值一样，行数不一样**：坏行在两边都不贡献数值，所以 sum 完全相同；")
    print("    但 DuckDB 少了几行、Polars 一行没少 —— 查聚合看不出问题，查行数就看出来了。")
    print("  · **容错那 1% 的坏行，在时间上根本不是主角**：真正的差距在两个引擎解析/物化的实现上，")
    print("    不在「跳掉 1% 的行」还是「把 1% 的行置成 NULL」上。")
    print("  · → 所以这个语义差异**靠 benchmark 是发现不了的，只能靠对账** ——")
    print("    这正是场景 5 那一步存在的理由。")
    print("  · **「少读 1% 的行」也不等于「更快」**：DuckDB 省掉的是「把坏行放进结果」这一步，")
    print("    行还是要解析、还是要判定坏不坏 —— 省下的量级比判定的量级小得多。")
    print("  · **唯一一笔「为了对齐语义」付的钱**是第 4 行那一刀：多一遍全列过滤，")
    print("    换来跟 DuckDB 一样的行数。")
    print("  · 实务上的选择：Polars 想要跟 DuckDB 一样的行数，就得自己补那一刀；")
    print("    不补的话，就把「行数」这个指标从验收清单里划掉 —— 它两边本来就不一样。")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的容错模式与坏数据处理差异演示")
    parser.add_argument("--rows", type=int, default=2_000_000, help="性能场景的行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能场景重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    tmpdir = Path(tempfile.mkdtemp(prefix="fault_demo_"))
    orders = tmpdir / "orders.csv"
    orders.write_text(ORDERS_CSV, encoding="utf-8")
    broken = tmpdir / "broken.csv"
    broken.write_text(BROKEN_CSV, encoding="utf-8")

    con = connect()
    try:
        scenario_bad_data(orders, broken)
        scenario_auto_detect(con, orders)
        scenario_strict(con, orders)
        scenario_ignore_errors(con, orders)
        scenario_reconcile(con, orders)
        scenario_rejects(con, orders, broken)
        scenario_limits(con, broken, tmpdir)
        scenario_performance(con, tmpdir, args.rows, args.repeat)
    finally:
        con.close()

    print()


if __name__ == "__main__":
    main()
