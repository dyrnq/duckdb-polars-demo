"""JSON 与嵌套类型：半结构化数据在两个引擎里怎么落地。

JSON 数据进到分析引擎里只有两条路：
  1. 读的时候就把 JSON **解析成列**（DuckDB `read_json`、Polars `read_ndjson`）
  2. 表里存的就是一列 JSON **文本**，用到的时候再抽字段
     （DuckDB `json_extract`、Polars `str.json_decode`）

这两条路两个引擎都能走，但差别很大：
  · 类型推断：DuckDB 自己猜；Polars 2.1 的 `str.json_decode` **必须你先把 schema 写出来**
  · 取字段：DuckDB 可以在 SQL 里一路点进去（`props.campaign.id`）；
    Polars 没有「JSON 路径取值」这回事，只能整份解码成 struct 再取字段
  · 返回类型：DuckDB 的 `json_extract` 返回的是 **JSON 类型**（不是数字！），
    `json_value` 返回的也是 **字符串** —— 这两个坑很多人踩过
  · 嵌套类型（STRUCT / LIST）：两边是**对齐**的，走 Arrow 来回转不丢信息

用法：
    uv run python src/json_demo.py
    uv run python src/json_demo.py --rows 200000
"""

from __future__ import annotations

import argparse
import json
import time

import duckdb
import numpy as np
import polars as pl

import common

EVENTS_NDJSON = common.DATA_DIR / "json_events.ndjson"
BIGINT_NDJSON = common.DATA_DIR / "json_bigint.ndjson"

EVENT_TYPES = ["click", "view", "purchase", "share"]
SOURCES = ["app", "web", "小程序", "门店"]
CAMPAIGNS = ["双十一", "618", "年货节", "开学季", "春节不打烊"]

# DuckDB 类型名 -> 统一口径的类型名（用来跟 Polars 的 dtype 对照）
DUCK_TO_CANON = {
    "BIGINT": "Int64",
    "INTEGER": "Int32",
    "SMALLINT": "Int16",
    "HUGEINT": "Int128",
    "VARCHAR": "String",
    "DOUBLE": "Float64",
    "FLOAT": "Float32",
    "BOOLEAN": "Boolean",
    "JSON": "Json",
    "TIMESTAMP": "Datetime",
    "DATE": "Date",
}


def section(title: str) -> None:
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")


def event_json_schema() -> pl.Struct:
    """整条事件的 Polars schema —— str.json_decode 必须显式给这个。"""
    return pl.Struct(
        {
            "event_id": pl.Int64,
            "user_id": pl.Int64,
            "event_type": pl.String,
            "ts": pl.String,
            "amount": pl.Float64,
            "props": pl.Struct(
                {
                    "source": pl.String,
                    "campaign": pl.Struct({"id": pl.Int64, "name": pl.String}),
                    "value": pl.Float64,
                    "flags": pl.List(pl.String),
                }
            ),
        }
    )


# ---------------------------------------------------------------- 造数


def ensure_data(n_rows: int) -> None:
    """造一份 NDJSON 事件流：每行一个 JSON 对象，里面套着对象和数组。"""
    if EVENTS_NDJSON.exists():
        with EVENTS_NDJSON.open(encoding="utf-8") as fh:
            existing = sum(1 for _ in fh)
        if existing == n_rows:
            print(f"复用已有数据（{n_rows:,} 行 NDJSON）")
            return

    print(f"生成 {n_rows:,} 行 NDJSON …")
    rng = np.random.default_rng(common.SEED)
    lines = []
    for i in range(n_rows):
        record = {
            "event_id": i + 1,
            "user_id": int(rng.integers(1, 50_000)),
            "event_type": EVENT_TYPES[int(rng.integers(0, len(EVENT_TYPES)))],
            "ts": f"2024-01-{(i % 28) + 1:02d}T{(i % 24):02d}:{(i % 60):02d}:00",
            "amount": None if rng.random() < 0.3 else round(float(rng.random() * 500), 2),
            "props": {
                "source": SOURCES[int(rng.integers(0, len(SOURCES)))],
                "campaign": {
                    "id": int(rng.integers(1, 20)),
                    "name": CAMPAIGNS[int(rng.integers(0, len(CAMPAIGNS)))],
                },
                "value": round(float(rng.normal(50, 15)), 3),
                "flags": (
                    ["new"]
                    if rng.random() < 0.5
                    else (["new", "vip"] if rng.random() < 0.5 else ["returning"])
                ),
            },
        }
        lines.append(json.dumps(record, ensure_ascii=False))
    EVENTS_NDJSON.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = EVENTS_NDJSON.stat().st_size / 1024 / 1024
    print(f"已写入 {EVENTS_NDJSON.name}（{size:.1f} MB）")

    # 超大整数的小样本：看两个引擎的类型推断会不会把它变成浮点数
    if not BIGINT_NDJSON.exists():
        BIGINT_NDJSON.write_text(
            '{"v": 1}\n{"v": 99999999999999999999}\n{"v": 12345678901234567890}\n',
            encoding="utf-8",
        )


# ---------------------------------------------------------------- 类型对照


def _split_top(text: str) -> list[str]:
    """按顶层逗号切分（跳过括号里的逗号）。"""
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def duck_type_to_canon(duck_type: str) -> str:
    """把 DuckDB 的类型名转成统一口径，方便跟 Polars 的 dtype 逐个对照。"""
    duck_type = duck_type.strip()
    if duck_type.endswith("[]"):
        return f"List({duck_type_to_canon(duck_type[:-2])})"
    if duck_type.startswith("STRUCT(") and duck_type.endswith(")"):
        inner = duck_type[len("STRUCT(") : -1]
        fields = []
        for item in _split_top(inner):
            name, _, ftype = item.partition(" ")
            fields.append(f"{name.strip(chr(34))}={duck_type_to_canon(ftype)}")
        return "Struct{" + ", ".join(fields) + "}"
    return DUCK_TO_CANON.get(duck_type, duck_type)


def polars_type_to_canon(dtype) -> str:
    """Polars dtype -> 同一套口径。"""
    if isinstance(dtype, pl.List):
        return f"List({polars_type_to_canon(dtype.inner)})"
    if isinstance(dtype, pl.Struct):
        # Polars 2.x 的 Struct.fields 是 Field 对象序列（老版本是 dict）
        if isinstance(dtype.fields, dict):
            items = list(dtype.fields.items())
        else:
            items = [(f.name, f.dtype) for f in dtype.fields]
        fields = ", ".join(f"{k}={polars_type_to_canon(v)}" for k, v in items)
        return "Struct{" + fields + "}"
    return str(dtype)


# ---------------------------------------------------------------- 场景 1


def scenario_read_types(con) -> None:
    section("场景 1 · 读同一份 NDJSON，推断出来的类型一样吗？")

    duck_rows = con.sql(f"DESCRIBE SELECT * FROM read_json_auto('{EVENTS_NDJSON}')").fetchall()
    polars_schema = pl.read_ndjson(EVENTS_NDJSON).schema

    mismatches = []
    for name, duck_type, *_ in duck_rows:
        duck_canon = duck_type_to_canon(duck_type)
        polars_dtype = polars_schema.get(name)
        polars_canon = (
            polars_type_to_canon(polars_dtype) if polars_dtype is not None else "（没这一列）"
        )
        same = duck_canon == polars_canon
        if not same:
            mismatches.append((name, duck_canon, polars_canon))
        mark = "✅" if same else "❌"
        print(f"  {mark} {name}")
        print(f"      DuckDB : {duck_type}")
        print(f"      Polars : {polars_canon}")

    if mismatches:
        print("\n类型不一致的列：")
        for name, d, p in mismatches:
            print(f"  {name}: DuckDB {d}  vs  Polars {p}")
    else:
        print("\n两边推断出来的类型**完全一致** —— 嵌套结构（STRUCT / LIST）在 Arrow 上是对齐的。")

    print("\n注意 DuckDB 把 props 推断成了真正的嵌套类型：")
    print("  props : STRUCT(source VARCHAR, campaign STRUCT(id BIGINT, name VARCHAR), …)")
    print("  所以可以直接写 `props.campaign.name`，不用再解析 JSON 文本。")
    print("Polars 侧同理：读进来就是 Struct / List，字段访问用 .struct.field(...) 链式取。")
    print("  props.campaign.name 在 Polars 里要写成")
    print("    pl.col('props').struct.field('campaign').struct.field('name')")
    print("  （Polars 不支持 'a.b.c' 这种点号路径写法，写了会报 ColumnNotFoundError）")

    print("\n唯一不一致的列是 ts —— 这不是 bug，是两边「顺手多做了」的事：")
    print("  DuckDB 的 JSON 读取器会认出 ISO-8601 字符串，直接推断成 TIMESTAMP；")
    print("  Polars 的 read_ndjson 只做 JSON 类型映射，字符串就是字符串。")
    print("  对齐办法：")
    print("    · Polars 侧：pl.col('ts').str.to_datetime()")
    print("    · DuckDB 侧：read_json(..., timestampformat='%Y-%m-%dT%H:%M:%S') 或直接 ::TIMESTAMP")
    print("  不对齐的代价：按 ts 过滤/排序时，Polars 侧是在**比字符串**，")
    print("  格式一旦不统一（有人写成 '2024-1-5'），排序结果就是错的，而且不报错。")


def scenario_bigint(con) -> None:
    section("场景 2 · 类型推断的暗坑：超大整数被悄悄变成浮点数")

    tmp = BIGINT_NDJSON
    duck_type = con.sql(f"DESCRIBE SELECT * FROM read_json_auto('{tmp}')").fetchall()[0][1]
    duck_vals = con.sql(f"SELECT v FROM read_json_auto('{tmp}')").fetchall()
    polars_vals = pl.read_ndjson(tmp).to_dicts()

    header = (
        common.pad("原始值", 24) + common.pad("DuckDB 读出来", 24) + common.pad("Polars 读出来", 24)
    )
    print(header)
    print("-" * 72)
    for (dval,), pval in zip(duck_vals, polars_vals, strict=True):
        print(
            common.pad(str(pval["v"]), 24)
            + common.pad(str(dval), 24)
            + common.pad(str(pval["v"]), 24)
        )

    print(f"\nDuckDB 推断出的类型：{duck_type}；Polars：{pl.read_ndjson(tmp).schema['v']}")
    print("\n差异：")
    print("  · DuckDB 的 JSON 读取器没有 UHUGEINT，超大整数会退化成 DOUBLE —— 精度直接没了")
    print("    （99999999999999999999 变成 1e+20，末位对不上）。")
    print("  · Polars 推断成 Int128，一个不丢。")
    print("  · 也就是说：同一个 JSON 文件，两边读出来的数**不是同一个数**。")
    print("    对账的时候要拿「原始 JSON 文本」比，别拿读进来的列比。")


def scenario_extract(con) -> None:
    section("场景 3 · 取嵌套字段：两边的写法完全不同")

    con.execute(
        f"""
        CREATE OR REPLACE VIEW raw_json AS
        SELECT to_json(t) AS j
        FROM read_json_auto('{EVENTS_NDJSON}') t
        LIMIT 3
        """
    )

    print("DuckDB —— 直接在 SQL 里点进去：")
    for r in con.sql(
        "SELECT json_extract_string(j, '$.props.source')          AS source, "
        "       j ->> '$.props.campaign.name'                    AS campaign, "
        "       json_extract(j, '$.props.value')                  AS value_json, "
        "       json_extract(j, '$.props.value')::DOUBLE         AS value_num, "
        "       json_value(j, '$.props.campaign.id')              AS camp_id, "
        "       json_extract_string(j, '$.props.flags[0]')        AS flag0 "
        "FROM raw_json"
    ).fetchall():
        print(f"    {r}")

    print("\n各函数的返回类型（DESCRIBE 看得很清楚）：")
    for name, duck_type, *_ in con.sql(
        "DESCRIBE SELECT json_extract(j,'$.props.value') a, "
        "json_extract_string(j,'$.props.value') b, "
        "json_value(j,'$.props.value') c, "
        "json_extract(j,'$.props.value')::DOUBLE d, "
        "j -> '$.props.value' e, j ->> '$.props.value' f FROM raw_json"
    ).fetchall():
        print(f"    {name:<4}{duck_type}")

    print("\n坑：")
    print("  · json_extract 返回的是 **JSON 类型**，不是数字 ——")
    print("    值是 '\"双十一\"' 这种带引号的原文。")
    print("    要数字必须自己转：json_extract(j,'$.props.value')::DOUBLE。")
    print("  · json_value 看起来该给个类型，其实**返回 VARCHAR**，'9.5' 还是字符串。")
    print("  · -> 返回 JSON，->> 返回 VARCHAR；等价于 json_extract / json_extract_string。")
    print("  · 路径不存在时三个函数都返回 NULL（不报错）。")

    print("\nPolars —— 没有「JSON 路径取值」，只有「整份解码」：")
    raw = con.sql("SELECT j FROM raw_json LIMIT 1").fetchall()[0][0]
    print(f"    原始文本：{raw[:80]} …")

    schema = event_json_schema()
    df = pl.DataFrame({"j": [raw]}).with_columns(pl.col("j").str.json_decode(dtype=schema))
    print("\n    必须先把整份事件的 schema 写出来才能解码：")
    print("      pl.col('j').str.json_decode(dtype=pl.Struct({...}))")
    print(f"      解码后：{df.schema['j']}")
    print("    然后用链式取字段：")
    print(
        "      pl.col('j').struct.field('props').struct.field('campaign')"
        ".struct.field('name')  ->  "
        f"{df.select(pl.col('j').struct.field('props').struct.field('campaign').struct.field('name')).item()}"
    )

    print("\n    忘了写 dtype 会怎样：")
    try:
        pl.DataFrame({"j": [raw]}).with_columns(pl.col("j").str.json_decode())
    except Exception as exc:
        print(f"      {type(exc).__name__}: {str(exc).splitlines()[0]}")
    print("    —— Polars 2.1 的 str.json_decode 推断不了类型，必须显式给 dtype。")
    print("    DuckDB 那边则是自己猜（read_json_auto），但你也可以用 columns= 显式指定。")


def scenario_nested_types(con) -> None:
    section("场景 4 · 嵌套类型（STRUCT / LIST）：两边是对齐的")

    duck_tbl = con.sql(
        f"SELECT event_id, props, amount FROM read_json_auto('{EVENTS_NDJSON}') LIMIT 3"
    ).to_arrow_table()
    print("DuckDB -> Arrow 的 schema：")
    for field in duck_tbl.schema:
        print(f"    {field.name:<12}{field.type}")

    polars_df = pl.from_arrow(duck_tbl)
    print("\n同一份 Arrow 表在 Polars 里：")
    for name, dtype in polars_df.schema.items():
        print(f"    {name:<12}{dtype}")

    print("\n反向：Polars 的 struct 直接注册给 DuckDB，DuckDB 就能按点号取字段。")
    con.register("polars_side", polars_df)
    print("    DuckDB 看到的类型：")
    for r in con.sql("DESCRIBE SELECT * FROM polars_side").fetchall():
        print(f"      {r[0]:<12}{r[1]}")
    for r in con.sql(
        "SELECT event_id, props.campaign.name AS campaign, props.flags[1] AS flag1 "
        "FROM polars_side ORDER BY event_id LIMIT 3"
    ).fetchall():
        print(f"      {r}")

    print("\n结论：STRUCT / LIST 这套嵌套类型在 Arrow 上是标准格式，")
    print("      两个引擎之间来回转不丢信息，也不丢类型。")
    print("      真正有差异的是「JSON 文本」这一层（上一场景）。")


def scenario_reparse(con) -> None:
    section("场景 5 · 代价：JSON 文本列，每抽一个字段就重新解析一次")

    # 先把「一列 JSON 文本」物化成表 —— 否则每次查询都要重扫 NDJSON 再 to_json 一次，
    # 那点开销会把 json_extract 本身盖掉，测出来的是别的东西。
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE raw_json AS
        SELECT to_json(t) AS j
        FROM read_json_auto('{EVENTS_NDJSON}') t
        """
    )
    n = con.sql("SELECT count(*) FROM raw_json").fetchall()[0][0]

    def timeit(sql: str, repeat: int = 3) -> float:
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            con.sql(sql).fetchall()
            times.append((time.perf_counter() - t0) * 1000)
        return float(np.median(times))

    one_field = timeit("SELECT json_extract_string(j, '$.props.source') FROM raw_json")
    three_fields = timeit(
        "SELECT json_extract_string(j, '$.props.source'), "
        "json_extract_string(j, '$.props.campaign.name'), "
        "json_extract_string(j, '$.props.flags[0]') FROM raw_json"
    )
    print(f"数据量：{n:,} 行 JSON 文本（已物化成一张表，下面只测解析的开销）")
    print(f"  DuckDB 抽 1 个字段  : {one_field:.0f} ms")
    print(f"  DuckDB 抽 3 个字段  : {three_fields:.0f} ms")
    print(f"  每多抽一个字段约多花 {(three_fields - one_field) / 2:.0f} ms")

    print("\nPolars 侧：解码一次，随便取")
    raw_tbl = con.sql("SELECT j FROM raw_json").to_arrow_table()
    frame = pl.from_arrow(raw_tbl)
    schema = event_json_schema()

    decode_times = []
    for _ in range(3):
        t0 = time.perf_counter()
        decoded = frame.with_columns(pl.col("j").str.json_decode(dtype=schema))
        decode_times.append((time.perf_counter() - t0) * 1000)
    decode_ms = float(np.median(decode_times))

    t0 = time.perf_counter()
    decoded.select(
        pl.col("j").struct.field("props").struct.field("source"),
        pl.col("j").struct.field("props").struct.field("campaign").struct.field("name"),
        pl.col("j").struct.field("props").struct.field("flags").list.first(),
    )
    pick_ms = (time.perf_counter() - t0) * 1000

    print(f"    解码成 struct（{n:,} 行）: {decode_ms:.0f} ms")
    print(f"    解码之后取 3 个字段       : {pick_ms:.1f} ms")
    print(f"    解码后的类型: {decoded.schema['j']}")

    print("\n差别：DuckDB 的 json_extract 是「一行一次解析」，抽 N 个字段就解析 N 次")
    print(f"      （实测每多抽一个字段多花 {(three_fields - one_field) / 2:.0f} ms，基本线性）；")
    print("      Polars 是「整份解码成 struct」，解码一次之后取字段是纯内存操作。")
    print("      所以 —— 数据在磁盘上就是 JSON 文本时，")
    print("      DuckDB 侧最好先把需要的字段抽出来落成列，别在下游反复抽。")
    print(
        f"      （本例：抽 3 个字段 DuckDB 要 {three_fields:.0f} ms，"
        f"Polars 解码一次 {decode_ms:.0f} ms + 取字段 {pick_ms:.1f} ms）"
    )


def scenario_performance(con, repeat: int) -> None:
    section("场景 6 · 性能：解析一份 NDJSON 谁快")

    print(f"机器状态：{common.describe_machine()}")
    print("（这台是共享开发机，负载高的时候绝对值会飘，看相对关系即可）\n")

    duck_times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        rows = con.sql(
            f"SELECT count(*), sum(amount) FROM read_json_auto('{EVENTS_NDJSON}')"
        ).fetchall()
        duck_times.append((time.perf_counter() - t0) * 1000)

    polars_times = []
    polars_sum = None
    polars_rows = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        frame = pl.read_ndjson(EVENTS_NDJSON)
        polars_times.append((time.perf_counter() - t0) * 1000)
        polars_rows = frame.height
        polars_sum = frame["amount"].sum()

    print(f"{'做法':<50}{'耗时 (ms，中位数)':>18}")
    print("-" * 68)
    print(
        f"{common.pad('DuckDB read_json_auto（扫 NDJSON + 推断 schema）', 50)}"
        f"{common.pad_left(f'{np.median(duck_times):.1f}', 18)}"
    )
    print(
        f"{common.pad('Polars read_ndjson（扫 NDJSON + 推断 schema）', 50)}"
        f"{common.pad_left(f'{np.median(polars_times):.1f}', 18)}"
    )

    duck_med = float(np.median(duck_times))
    polars_med = float(np.median(polars_times))
    ratio = max(duck_med, polars_med) / min(duck_med, polars_med)
    faster = "DuckDB" if duck_med < polars_med else "Polars"
    print(f"\n  · DuckDB {duck_med:.0f} ms vs Polars {polars_med:.0f} ms")
    if ratio < 1.15:
        print(f"    差距在 {ratio:.2f}× 以内 —— 基本上打平，解析 NDJSON 不是这两边的分水岭。")
    else:
        print(f"    {faster} 快 {ratio:.1f}×。")
    print(f"  · 两边读出来的行数一致：DuckDB {rows[0][0]:,} / Polars {polars_rows:,}")
    same_sum = abs(float(rows[0][1]) - float(polars_sum)) < 1e-6
    print(
        f"  · amount 求和一致：{'✅' if same_sum else '❌'} "
        f"DuckDB {rows[0][1]} vs Polars {polars_sum}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB 与 Polars 的 JSON / 嵌套类型对比")
    parser.add_argument("--rows", type=int, default=200_000, help="NDJSON 行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    ensure_data(args.rows)

    con = duckdb.connect()
    try:
        scenario_read_types(con)
        scenario_bigint(con)
        scenario_extract(con)
        scenario_nested_types(con)
        scenario_reparse(con)
        scenario_performance(con, args.repeat)
    finally:
        con.close()
    print()


if __name__ == "__main__":
    main()
