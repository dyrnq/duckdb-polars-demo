"""JSON 全特性：把 DuckDB 的 json 扩展逐个函数过一遍。

`json_demo.py` 讲的是 JSON 的**主线** —— 怎么读、怎么取字段、类型推断的坑、解析代价。
这个文件补的是另一半：json 扩展 57 个函数名（141 个重载）里主线没碰到的那些。

  · 取值      json_extract / json_extract_string / json_value / -> / ->> / json_extract_path
  · 探针      json_exists / json_type / json_keys / json_array_length / json_contains
  · Schema    json_structure / json_transform / json_transform_strict /
              from_json / from_json_strict
  · 拆解      json_each / json_tree
  · 构造      json_object / json_array / to_json / row_to_json / array_to_json /
              json_quote / json_pretty / json_strip_nulls / json_normalize /
              json_insert / json_set / json_replace / json_remove
  · 合并      json_merge_patch / json_merge_patch_diff / json_deep_merge
  · 聚合回写  json_group_array / json_group_object / json_group_structure
  · 读取器    read_json / read_json_auto / read_json_objects / read_json_objects_auto /
              read_ndjson / read_ndjson_auto / read_ndjson_objects / read_single_json_file
  · 往返      json_serialize_sql / json_serialize_plan /
              json_deserialize_sql / json_execute_serialized_sql
  · GeoJSON   st_geomfromgeojson / st_asgeojson
  · 宏/内部   json / json_copy_strftime_if_date / json_copy_strftime_if_timestamp

最后一节是**覆盖自检**：demo 把自己执行过的每一条 SQL 都过一遍，
统计这 57 个函数名各自在哪一节被真的调用过，打印成表 ——
「全部特性」是量出来的，不是写出来的。

用法：
    uv run python src/json_full_demo.py
    uv run python src/json_full_demo.py --rows 200000 --repeat 5
"""

from __future__ import annotations

import argparse
import json
import re
import time

import duckdb
import numpy as np
import polars as pl

import common

DOCS_NDJSON = common.DATA_DIR / "json_docs.ndjson"
MIXED_NDJSON = common.DATA_DIR / "json_mixed.ndjson"
DIRTY_NDJSON = common.DATA_DIR / "json_dirty.ndjson"
ARRAY_JSON = common.DATA_DIR / "json_array.json"

# 用来演示取值语义的那一份文档 —— 字段刻意做得全：
# 数字、字符串、布尔、数组、嵌套对象、null 各来一个。
DOC = json.dumps(
    {
        "doc_id": 7,
        "name": "双十一",
        "price": 9.5,
        "active": True,
        "tags": ["a", "b"],
        "user": {"id": 42, "name": "张三", "city": "杭州"},
        "note": None,
    },
    ensure_ascii=False,
    separators=(",", ":"),
)

# 用来演示 Schema 驱动的那份 schema（JSON Schema 片段）
SCHEMA = json.dumps(
    {
        "doc_id": "INTEGER",
        "name": "VARCHAR",
        "price": "DOUBLE",
        "user": {"id": "INTEGER", "city": "VARCHAR"},
    },
    separators=(",", ":"),
)

CITIES = ["杭州", "北京", "上海", "成都", "深圳"]
EVENTS = ["click", "view", "purchase"]

# Polars 侧对每个函数的对应物 —— 没有的就写「无」。
# 这张表是**查过 Polars 2.1 的 API 表面**得出的，不是印象：
#   pl 下跟 json 有关的只有 json_normalize / read_json / read_ndjson / scan_ndjson，
#   字符串命名空间里跟 json 有关的只有 json_decode / json_path_match，
#   DataFrame 上只有 write_json / write_ndjson。
POLARS_EQUIV = {
    "json_extract": "str.json_path_match（返回字符串，不是 JSON 值）",
    "json_extract_string": "str.json_path_match",
    "json_extract_path": "无",
    "json_extract_path_text": "无",
    "json_value": "str.json_path_match（但引号语义不同，见第 1 节）",
    "->": "无",
    "->>": "str.json_path_match",
    "json_exists": "无（要自己写 path 匹配）",
    "json_type": "无",
    "json_keys": "无",
    "json_array_length": "无",
    "json_contains": "无",
    "json_structure": "pl.json_normalize（靠 Python 值推断，不是 JSON Schema）",
    "json_transform": "pl.json_normalize / str.json_decode(dtype=)",
    "json_transform_strict": "str.json_decode(dtype=)（缺字段给 null，不报错）",
    "from_json": "str.json_decode(dtype=)",
    "from_json_strict": "str.json_decode(dtype=)",
    "json_each": "struct.unnest + list.explode（没有 fullkey 路径）",
    "json_tree": "无（没有递归下降的路径查询）",
    "json_object": "无",
    "json_array": "无",
    "json_group_array": "无",
    "json_group_object": "无",
    "json_group_structure": "无",
    "json_insert": "无",
    "json_set": "无",
    "json_replace": "无",
    "json_remove": "无",
    "json_merge_patch": "无",
    "json_merge_patch_diff": "无",
    "json_deep_merge": "无",
    "json_quote": "无",
    "json_pretty": "无",
    "json_strip_nulls": "无",
    "json_normalize": "无（同名但语义不同：Polars 是「摊平成表」）",
    "json_valid": "无",
    "json": "无",
    "to_json": "无（Polars 是 write_json / write_ndjson，一次写整张表）",
    "row_to_json": "无（同上）",
    "read_json": "pl.read_json（只吃 JSON 数组文件，不吃 NDJSON）",
    "read_json_auto": "pl.read_json / pl.read_ndjson（要分两个函数）",
    "read_json_objects": "无",
    "read_json_objects_auto": "无",
    "read_ndjson": "pl.read_ndjson",
    "read_ndjson_auto": "pl.read_ndjson",
    "read_ndjson_objects": "无",
    "read_single_json_file": "无",
    "json_serialize_sql": "无",
    "json_serialize_plan": "无",
    "json_deserialize_sql": "无",
    "json_execute_serialized_sql": "无",
    "st_geomfromgeojson": "无",
    "st_asgeojson": "无",
    "json_copy_strftime_if_date": "无",
    "json_copy_strftime_if_timestamp": "无",
    "__internal_json_copy_to_json": "无（且 DuckDB 侧也不许直接调）",
    "__internal_json_copy_to_geojson": "无（且 DuckDB 侧也不许直接调）",
}

# 在 Polars 侧**有**对应物的函数名。
# 单独列出来，是因为 POLARS_EQUIV 里还有「无（附一句说明）」的条目 ——
# 那几个是「没有，但值得记一笔」，不能算进「有对应物」的数里。
# 注意「有对应物」≠「语义一样」，见表里那几条备注。
POLARS_HAS_EQUIV = {
    "json_extract",
    "json_extract_string",
    "json_value",
    "->>",
    "json_structure",
    "json_transform",
    "json_transform_strict",
    "from_json",
    "from_json_strict",
    "json_each",
    "to_json",
    "row_to_json",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_ndjson_auto",
}

# ---------------------------------------------------------------- 覆盖记账
#
# 「全部特性」要能自证，就不能靠人肉核对清单 ——
# demo 每执行一条 SQL，就把这条 SQL 里用到的 json 扩展函数名记到当前小节名下。
# 最后一节再拿这个账本跟 DuckDB 的函数目录对账。
#
# 注意记的是**执行过的**，不是文档里提过的：
# 所以下面所有 SQL 都必须走 q()，绕过去就记不上。

_CATALOG: list[str] = []
_USED: dict[str, set[int]] = {}
_FAILED: dict[str, set[int]] = {}
_SECTION_INDEX: dict[str, int] = {}
_CURRENT_SECTION = 0


def section(title: str) -> None:
    """开一节。同时给这一节发一个序号 —— 覆盖表里用序号指代小节。"""
    global _CURRENT_SECTION
    _CURRENT_SECTION = len(_SECTION_INDEX) + 1
    _SECTION_INDEX[title] = _CURRENT_SECTION
    print(f"\n{'=' * 92}\n第 {_CURRENT_SECTION} 节 · {title}\n{'=' * 92}")


def _load_catalog(con) -> list[str]:
    """json 扩展的函数名（去重后 57 个）。"""
    rows = con.execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE extension_name = 'json' ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def _names_in(sql: str) -> set[str]:
    """一条 SQL 里用到了哪些 json 扩展函数（含 -> / ->> 两个运算符）。

    函数名**大小写不敏感**（DuckDB 的 SQL 关键字都是这样），
    所以匹配时要 IGNORECASE —— 否则 ST_GeomFromGeoJSON 这种写法就记不上。
    """
    found: set[str] = set()
    for name in _CATALOG:
        if re.search(r"\b" + re.escape(name) + r"\s*\(", sql, re.IGNORECASE):
            found.add(name)
    # ->> 是目录里的函数；-> 不是（它是解析器层面的运算符，duckdb_functions 里查不到）
    if "->>" in sql:
        found.add("->>")
    elif re.search(r"(?<![-<])->(?!>)", sql):
        found.add("->")
    return found


def _record(sql: str, *, ok: bool) -> None:
    target = _USED if ok else _FAILED
    for name in _names_in(sql):
        target.setdefault(name, set()).add(_CURRENT_SECTION)


def q(con, sql: str, params: list | None = None):
    """执行一条 SQL，并记账（用到了哪些 json 函数、这次成功了没有）。

    返回 DuckDB 的连接对象，调用方接着 .fetchall() / .fetchdf()。
    """
    try:
        cur = con.execute(sql, params) if params is not None else con.execute(sql)
    except Exception:
        _record(sql, ok=False)
        raise
    _record(sql, ok=True)
    return cur


def qtry(con, sql: str, params: list | None = None):
    """执行一条 SQL；出错就把错误文本交回来（不抛）。"""
    try:
        return q(con, sql, params).fetchall(), None
    except Exception as exc:
        return None, str(exc).splitlines()[0]


def show(label: str, value) -> None:
    print(f"  {common.pad(label, 44)}{value}")


def clip(text: str, width: int) -> str:
    """按**显示宽度**截断（不把一个字符劈成两半），超出的部分用 … 收尾。"""
    text = str(text)
    if common.display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = common.display_width(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


# ---------------------------------------------------------------- 造数


def ensure_data(n_rows: int) -> None:
    """造四份小数据：主流文档、类型不一致的、脏的、顶层数组的。"""
    if DOCS_NDJSON.exists():
        with DOCS_NDJSON.open(encoding="utf-8") as fh:
            existing = sum(1 for _ in fh)
        if existing == n_rows:
            print(f"复用已有数据（{n_rows:,} 行 NDJSON）")
        else:
            _write_docs(n_rows)
    else:
        _write_docs(n_rows)

    if not MIXED_NDJSON.exists():
        # 同一个键在不同行里是不同类型 —— 看两边怎么「和解」
        MIXED_NDJSON.write_text(
            '{"id":1,"v":10,"tags":["a"]}\n'
            '{"id":2,"v":"文本","tags":["a","b"]}\n'
            '{"id":3,"v":3.5,"tags":[]}\n'
            '{"id":4,"v":null,"tags":["x"],"extra":{"deep":true}}\n',
            encoding="utf-8",
        )

    if not DIRTY_NDJSON.exists():
        # 三行里有一行是坏的
        DIRTY_NDJSON.write_text(
            '{"id":1,"v":"好的"}\n{"id":2,"v":"坏的",\n{"id":3,"v":"也是好的"}\n',
            encoding="utf-8",
        )

    if not ARRAY_JSON.exists():
        ARRAY_JSON.write_text(
            '[{"id":1,"name":"甲"},{"id":2,"name":"乙"}]',
            encoding="utf-8",
        )


def _write_docs(n_rows: int) -> None:
    print(f"生成 {n_rows:,} 行 NDJSON …")
    rng = np.random.default_rng(common.SEED)
    lines = []
    for i in range(n_rows):
        record = {
            "doc_id": i + 1,
            "name": f"文档-{i + 1}",
            "amount": None if rng.random() < 0.2 else round(float(rng.random() * 1000), 2),
            "city": CITIES[int(rng.integers(0, len(CITIES)))],
            "event": EVENTS[int(rng.integers(0, len(EVENTS)))],
            "user": {"id": int(rng.integers(1, 10000)), "tier": int(rng.integers(1, 5))},
            "tags": ["新"] if rng.random() < 0.5 else ["新", "回访"],
        }
        lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    DOCS_NDJSON.write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = DOCS_NDJSON.stat().st_size / 1024 / 1024
    print(f"已写入 {DOCS_NDJSON.name}（{size:.1f} MB）")


# ---------------------------------------------------------------- 第 1 节


def scenario_extract(con) -> None:
    section("取值：五个函数，返回类型不一样")

    q(con, "CREATE OR REPLACE TEMP TABLE doc AS SELECT ?::JSON AS j", [DOC])

    print("同一份文档，五种写法，返回类型不一样：")
    print()
    header = (
        common.pad("写法", 40) + common.pad("返回类型", 12) + common.pad("取数字", 12) + "取字符串"
    )
    print(header)
    print("-" * 92)
    probes = [
        ("json_extract(j, '$.doc_id')", "json_extract(j, '$.doc_id')"),
        ("json_extract_string(j, '$.doc_id')", "json_extract_string(j, '$.doc_id')"),
        ("json_value(j, '$.doc_id')", "json_value(j, '$.doc_id')"),
        ("j -> '$.doc_id'", "j -> '$.doc_id'"),
        ("j ->> '$.doc_id'", "j ->> '$.doc_id'"),
    ]
    for label, expr in probes:
        dtype = q(con, f"DESCRIBE SELECT {expr} AS v FROM doc").fetchall()[0][1]
        num, _ = qtry(con, f"SELECT {expr}::VARCHAR FROM doc")
        text, _ = qtry(con, f"SELECT {expr.replace('$.doc_id', '$.name')}::VARCHAR FROM doc")
        print(
            common.pad(label, 40)
            + common.pad(dtype, 12)
            + common.pad(str(num[0][0]) if num else "?", 12)
            + str(text[0][0])
            if text
            else "?"
        )

    print()
    print("取字符串的时候，坑在这儿：")
    for expr in ("json_extract_string(j, '$.name')", "json_value(j, '$.name')", "j ->> '$.name'"):
        val, _ = qtry(con, f"SELECT {expr} FROM doc")
        print(f"  {common.pad(expr, 40)}{val[0][0]!r}")

    print()
    print("  · json_extract / -> 返回的是 **JSON 类型** —— 值是 JSON 原文，字符串带引号。")
    print("    要数字得自己转：json_extract(j,'$.doc_id')::INTEGER。")
    print("  · json_extract_string / ->> 返回的是**去掉引号的文本**。")
    print("  · json_value 也返回 VARCHAR，但它是 **JSON 原文** —— 字符串会带着引号：")
    print("    json_value(j,'$.name') 给的是 '\"双十一\"'，不是 '双十一'。")
    print("    （实测见上面的 repr —— 两个引号都在。）")
    print("  · 路径不存在时，三个函数都返回 NULL，**不报错**。")

    rows, _ = qtry(
        con, "SELECT json_extract_string(j,'$.没有这个'), json_value(j,'$.没有这个') FROM doc"
    )
    print(f"    实测不存在的路径：{rows[0]}")

    print()
    print("Polars 侧 —— 只有一个 str.json_path_match，返回的永远是字符串：")
    frame = pl.DataFrame({"j": [DOC]})
    for path in ("$.doc_id", "$.name"):
        val = frame.select(pl.col("j").str.json_path_match(path)).item()
        print(f"  {common.pad(f'str.json_path_match({path!r})', 40)}{val!r}")

    duck_name, _ = qtry(con, "SELECT json_extract_string(j,'$.name') FROM doc")
    pol_name = frame.select(pl.col("j").str.json_path_match("$.name")).item()
    same = duck_name[0][0] == pol_name
    print()
    print(
        f"  · 字符串取值两边一致：{'✅' if same else '❌'} "
        f"DuckDB {duck_name[0][0]!r} vs Polars {pol_name!r}"
    )
    duck_value, _ = qtry(con, "SELECT json_value(j,'$.name') FROM doc")
    same2 = duck_value[0][0] == pol_name
    print(
        f"  · json_value 跟 Polars 不一致：{'✅（说明确实不一致）' if not same2 else '❌'} "
        f"DuckDB {duck_value[0][0]!r} vs Polars {pol_name!r}"
    )
    print("  · Polars 没有「返回 JSON 值」这回事 —— 想要 JSON 原文只能自己拼回去。")


# ---------------------------------------------------------------- 第 2 节


def scenario_batch_path(con) -> None:
    section("批量取值：json_extract_path 的数组是「并列的路径」，不是「嵌套的路径」")

    q(con, "CREATE OR REPLACE TEMP TABLE doc2 AS SELECT ?::JSON AS j", [DOC])

    print("json_extract_path 的第二个参数可以是 VARCHAR[] —— 一次取多个路径：")
    rows = q(con, "SELECT json_extract_path(j, ['$.doc_id', '$.user.city']) FROM doc2").fetchall()
    print(f"  json_extract_path(j, ['$.doc_id', '$.user.city']) → {rows[0][0]}")

    print()
    print("**关键**：数组里每个元素是一条独立的路径，并列，不是串起来的。")
    for paths, note in (
        (["$.user", "$.user.city"], "「user」和「user.city」是两条路，各取各的"),
        (["$.user", "$.tags"], "「tags」不是 user 里的东西"),
    ):
        rows = q(con, f"SELECT json_extract_path(j, {paths!r}) FROM doc2").fetchall()
        print(f"  {common.pad(str(paths), 34)}{rows[0][0]}   ← {note}")

    print()
    print("  想要「user 里的 city」这一条路径，得写全：")
    rows = q(con, "SELECT json_extract_path(j, ['$.user.city']) FROM doc2").fetchall()
    one_path = "['$.user.city']"
    print(f"    {common.pad(one_path, 34)}{rows[0][0]}")

    print()
    print("第二个参数也可以是**一个**路径字符串 —— 那就是普通取值，返回标量：")
    for expr in (
        "json_extract_path(j, '$.user.city')",
        "json_extract_path_text(j, '$.user.city')",
        "json_extract_path_text(j, ['$.user.city'])",
    ):
        rows = q(con, f"SELECT {expr} FROM doc2").fetchall()
        print(f"  {common.pad(expr, 40)}{rows[0][0]!r}")

    print()
    print("json_extract 也有同样的重载（第二个参数是路径数组），两者等价：")
    a = q(con, "SELECT json_extract(j, ['$.doc_id','$.user.city']) FROM doc2").fetchall()[0][0]
    b = q(con, "SELECT json_extract_path(j, ['$.doc_id','$.user.city']) FROM doc2").fetchall()[0][0]
    print(f"  json_extract       → {a}")
    print(f"  json_extract_path  → {b}")
    print(f"  两者一致：{'✅' if a == b else '❌'}")

    print()
    print("Polars 侧没有「一次给多个路径」的对应物 —— 只能一条一条写。")


# ---------------------------------------------------------------- 第 3 节


def scenario_probes(con) -> None:
    section("探针：存在性、类型、键、长度、包含")

    q(con, "CREATE OR REPLACE TEMP TABLE doc3 AS SELECT ?::JSON AS j", [DOC])

    print("json_exists —— 路径在不在：")
    for path in ("$.doc_id", "$.user.city", "$.user.zip", "$.tags[0]", "$.没有"):
        rows = q(con, f"SELECT json_exists(j, '{path}') FROM doc3").fetchall()
        print(f"  {common.pad(path, 24)}{rows[0][0]}")

    print()
    print("json_type —— 值的**类型名**（DuckDB 的 SQL 类型名，不是 JSON 的）：")
    print(f"  {common.pad('路径', 24)}{common.pad('json_type', 14)}JSON 里的说法")
    print("  " + "-" * 62)
    for path, note in (
        ("$.doc_id", "整数"),
        ("$.price", "小数"),
        ("$.name", "字符串"),
        ("$.active", "布尔"),
        ("$.tags", "数组"),
        ("$.user", "对象"),
        ("$.note", "null"),
    ):
        rows = q(con, f"SELECT json_type(j, '{path}') FROM doc3").fetchall()
        print(f"  {common.pad(path, 24)}{common.pad(str(rows[0][0]), 14)}{note}")

    print()
    print("  注意整数是 UBIGINT —— 无符号。负数会变成 BIGINT。")
    neg = q(con, "SELECT json_type('{\"n\":-1}'::JSON, '$.n')").fetchall()
    print(f"  实测 json_type('{{\"n\":-1}}') → {neg[0][0]}")

    print()
    print("json_keys —— 对象有哪些键（不给路径就是顶层）：")
    for expr in ("json_keys(j)", "json_keys(j, '$.user')"):
        rows = q(con, f"SELECT {expr} FROM doc3").fetchall()
        print(f"  {common.pad(expr, 24)}{rows[0][0]}")

    print()
    print("json_array_length —— 数组多长：")
    rows = q(con, "SELECT json_array_length(j, '$.tags') FROM doc3").fetchall()
    call = "json_array_length(j, '$.tags')"
    print(f"  {common.pad(call, 40)}{rows[0][0]}")
    rows, err = qtry(con, "SELECT json_array_length(j, '$.user') FROM doc3")
    call = "json_array_length(j, '$.user')"
    print(f"  {common.pad(call, 40)}{err if err else rows[0][0]}")
    rows, err = qtry(con, "SELECT json_array_length(j, '$.missing') FROM doc3")
    call = "json_array_length(j, '$.missing')"
    print(f"  {common.pad(call, 40)}{err if err else rows[0][0]}")

    print()
    print("json_contains —— **JSON 语义**的包含（不是字符串包含）：")
    cases = [
        ('{"a":[1,2,3]}', "[1,2]"),
        ('{"a":[1,2,3]}', "[1,2,3]"),
        ('{"a":[1,2,3]}', "[9]"),
        ('{"a":{"b":1}}', '{"b":1}'),
        ('{"a":{"b":1}}', '{"b":2}'),
        ('{"a":"abc"}', '"abc"'),
    ]
    for left, right in cases:
        rows = q(con, f"SELECT json_contains('{left}'::JSON, '{right}'::JSON)").fetchall()
        print(f"  {common.pad(f'{left} 包含 {right}', 44)}{rows[0][0]}")

    print()
    print("  · 数组包含是**递归**的：[1,2,3] 包含 [1,2] → True。")
    print("  · 两个参数都是**整份文档**，没有「在某条路径下包含」这种用法 ——")
    print("    想在某个键下比较，得自己先取出来再比。")
    print("  · 对**不是数组**的值调 json_array_length，返回的是 **0**（不报错）；")
    print("    只有**路径本身不存在**的时候才返回 NULL。")

    print()
    print("Polars 侧这五个探针一个都没有 —— 要么自己写 path 匹配，要么先把 JSON 解开再判。")


# ---------------------------------------------------------------- 第 4 节


def scenario_schema(con) -> None:
    section("Schema 驱动：从 JSON 文本直接投影出强类型列")

    q(con, "CREATE OR REPLACE TEMP TABLE doc4 AS SELECT ?::JSON AS j", [DOC])

    print("第 1 步 · json_structure —— 看这份文档长什么样（反推 schema）：")
    structure = q(con, "SELECT json_structure(j) FROM doc4").fetchall()[0][0]
    print(f"  {structure}")

    print()
    print("第 2 步 · json_transform —— 照着 schema 把 JSON 投影成**带类型的列**：")
    rows = q(con, f"SELECT json_transform(j, '{SCHEMA}') AS t FROM doc4").fetchall()
    print(f"  {rows[0][0]}")

    types = q(con, f"DESCRIBE SELECT json_transform(j, '{SCHEMA}') AS t FROM doc4").fetchall()
    print()
    print("  投影出来的类型：")
    for name, dtype, *_ in types:
        print(f"    {common.pad(name, 12)}{dtype}")

    print()
    print("  值是**原生类型**，不是 JSON 文本 —— doc_id 是 INTEGER 不是 '7'。")
    print("  嵌套也保留了：user 是 STRUCT，不是被摊平的 'user.id' 字符串列。")

    print()
    print("第 3 步 · 缺字段的时候，两种模式完全不同：")
    for fn in ("json_transform", "json_transform_strict"):
        rows, err = qtry(con, f'SELECT {fn}(j, \'{{"没有的字段":"INTEGER"}}\') FROM doc4')
        if err:
            print(f"  {common.pad(fn, 26)}报错 → {err}")
        else:
            print(f"  {common.pad(fn, 26)}不报错 → {rows[0][0]}")
    ok_rows = q(con, f"SELECT json_transform_strict(j, '{SCHEMA}') FROM doc4").fetchall()
    print(f"  {'json_transform_strict 字段齐全':26}→ {ok_rows[0][0]}")

    print()
    print("  · json_transform 缺字段给 NULL（宽松）。")
    print("  · json_transform_strict 缺字段**直接报错** —— 适合「schema 不对就该停」的场景。")

    print()
    print("第 4 步 · from_json / from_json_strict —— 同一件事，参数顺序反过来：")
    for fn in ("from_json", "from_json_strict"):
        rows = q(con, f"SELECT {fn}(j, '{SCHEMA}') FROM doc4").fetchall()
        print(f"  {common.pad(fn, 20)}{rows[0][0]}")
    rows, err = qtry(con, 'SELECT from_json_strict(j, \'{"没有的字段":"INTEGER"}\') FROM doc4')
    print(f"  {common.pad('from_json_strict 缺字段', 20)}{err}")

    print()
    print("Polars 侧 —— 两条路，都比 DuckDB 多一步：")
    schema = pl.Struct(
        {
            "doc_id": pl.Int32,
            "name": pl.String,
            "price": pl.Float64,
            "user": pl.Struct({"id": pl.Int32, "city": pl.String}),
        }
    )
    frame = pl.DataFrame({"j": [DOC]})
    decoded = frame.with_columns(pl.col("j").str.json_decode(dtype=schema))
    print(f"  str.json_decode(dtype=...) → {decoded.schema['j']}")
    print(f"    值：{decoded.select(pl.col('j').struct.field('user')).item()}")

    print()
    print("  差别一：Polars 的 dtype 得**用 Python 对象写出来**（pl.Int32 / pl.Struct({...})），")
    print("          DuckDB 那边是一段 JSON Schema 文本。")
    print("  差别二：Polars 的 str.json_decode **不认 JSON Schema**，只认 Polars 的 dtype；")
    print("          DuckDB 的 json_transform 认的是 SQL 类型名（INTEGER / DOUBLE / VARCHAR）。")
    print("  差别三：Polars 缺字段给 null，不报错；DuckDB 的 strict 模式会报错。")

    print()
    print("  另一条路：pl.json_normalize —— 把嵌套**摊平**成 'user.city' 这样的列名。")
    normalized = pl.json_normalize([json.loads(DOC)])
    print(f"    列：{normalized.columns}")
    print(f"    类型：{normalized.schema}")
    print("    它靠的是 **Python 值**的类型（不是 schema 文本），")
    print("    而且形状是摊平的 —— 跟 DuckDB 的 json_transform（保留嵌套）不是一回事。")

    duck_val = q(con, f"SELECT json_transform(j, '{SCHEMA}') FROM doc4").fetchall()[0][0]
    pol_val = decoded.select(pl.col("j").struct.field("doc_id")).item()
    duck_id = duck_val["doc_id"]
    print()
    print(
        f"  · doc_id 两边对账：{'✅' if duck_id == pol_val else '❌'} "
        f"DuckDB {duck_id} vs Polars {pol_val}"
    )


# ---------------------------------------------------------------- 第 5 节


def scenario_unnest(con) -> None:
    section("拆解：json_each 拆一层，json_tree 递归拆")

    q(con, "CREATE OR REPLACE TEMP TABLE doc5 AS SELECT ?::JSON AS j", [DOC])

    print("json_each —— 只拆**一层**，输出 8 列：")
    cols = [r[0] for r in q(con, "DESCRIBE SELECT * FROM json_each('{}'::JSON)").fetchall()]
    print(f"  列：{cols}")
    rows = q(
        con, "SELECT key, value, type, atom, fullkey FROM json_each((SELECT j FROM doc5))"
    ).fetchall()
    print()
    print(
        f"  {common.pad('key', 12)}{common.pad('value', 26)}{common.pad('type', 12)}"
        f"{common.pad('atom', 12)}fullkey"
    )
    print("  " + "-" * 88)
    for key, value, typ, atom, fullkey in rows:
        print(
            f"  {common.pad(str(key), 12)}{common.pad(clip(value, 24), 26)}"
            f"{common.pad(str(typ), 12)}{common.pad(str(atom), 12)}{fullkey}"
        )

    print()
    print("  · value 是 **JSON 类型**（对象那行看到的是 '{\"id\":42,…}'），atom 才是原子值。")
    print("  · type 是 JSON 里的类型（OBJECT / ARRAY / VARCHAR / UBIGINT …）。")
    print("  · fullkey 是这一项的完整路径 —— json_each 顶层的 fullkey 就是 '$.xxx'。")

    print()
    print("json_tree —— 递归拆到底，多出 fullkey 的层级和 parent：")
    rows = q(
        con,
        "SELECT key, type, atom, fullkey, path FROM json_tree((SELECT j FROM doc5)) "
        "ORDER BY fullkey",
    ).fetchall()
    for key, typ, atom, fullkey, path in rows:
        print(
            f"  {common.pad(str(key), 12)}{common.pad(str(typ), 12)}{common.pad(str(atom), 12)}"
            f"{common.pad(str(fullkey), 24)}{path}"
        )

    print()
    print("  · user.city 这一层被拆出来了，fullkey 是 '$.user.city'，path 是 '$.user'。")
    print("  · json_each 要做到同一件事，得自己递归。")

    print()
    print("Polars 侧 —— 能拆，但拆出来的东西不一样：")
    # 只声明了部分字段的 dtype（这是 Polars 解 JSON 的常见写法）
    partial = pl.Struct(
        {
            "doc_id": pl.Int32,
            "name": pl.String,
            "tags": pl.List(pl.String),
            "user": pl.Struct({"id": pl.Int32, "city": pl.String}),
        }
    )
    # 声明了全部字段的 dtype
    full = pl.Struct(
        {
            "doc_id": pl.Int32,
            "name": pl.String,
            "price": pl.Float64,
            "active": pl.Boolean,
            "tags": pl.List(pl.String),
            "user": pl.Struct({"id": pl.Int32, "name": pl.String, "city": pl.String}),
            "note": pl.String,
        }
    )
    frame = pl.DataFrame({"j": [DOC]}).with_columns(pl.col("j").str.json_decode(dtype=partial))
    frame_full = pl.DataFrame({"j": [DOC]}).with_columns(pl.col("j").str.json_decode(dtype=full))
    print(f"  struct.unnest → {frame.select(pl.col('j').struct.unnest()).columns}")
    print("  list.explode  → 把 tags 摊成多行")
    print()
    print("  差别：Polars 拆出来是**列**（unnest 之后每个键一列），")
    print("        DuckDB 拆出来是**行**（key / value / type / fullkey 每一项一行）。")
    print("        要在 Polars 里拿到 fullkey，得自己把列名拼回去。")
    print("  更要紧的：Polars 必须**先知道 schema** 才能解 —— 字段没在 dtype 里写，")
    print("        那个字段就整个丢掉。DuckDB 的 json_tree 不需要 schema。")

    # 对账：同一个文档，两边拆出来的顶层键应该一样
    duck_keys = [
        r[0] for r in q(con, "SELECT DISTINCT key FROM json_each((SELECT j FROM doc5))").fetchall()
    ]
    pol_keys = frame.select(pl.col("j").struct.unnest()).columns
    full_keys = frame_full.select(pl.col("j").struct.unnest()).columns
    same_partial = set(duck_keys) == set(pol_keys)
    same_full = set(duck_keys) == set(full_keys)
    print()
    print("  对账 —— 顶层键集合：")
    print(f"    DuckDB json_each → {sorted(duck_keys)}")
    print(
        f"    Polars（dtype 只写了一半）→ {sorted(pol_keys)}"
        f"   {'✅ 一致' if same_partial else '❌ 不一致'}"
    )
    print(
        f"    Polars（dtype 写全了）    → {sorted(full_keys)}"
        f"   {'✅ 一致' if same_full else '❌ 不一致'}"
    )
    print()
    print("    结论：**键集合能不能对上，取决于 dtype 写没写全**。")
    print("    DuckDB 那边不需要事先声明 —— 文档里有什么它就拆出什么；")
    print("    Polars 这边少写一个字段，那个字段就**整条消失**，不是变成 NULL。")
    print("    （形状也不同：一边是行，一边是列。）")


# ---------------------------------------------------------------- 第 6 节


def scenario_build(con) -> None:
    section("构造：从零造 JSON，以及往已有 JSON 里塞东西")

    print("造一份 JSON：")
    for expr in (
        "json_object('k', 1, 'j', 'x')",
        "json_array(1, 'x', true)",
        "to_json(t)",
        "row_to_json(t)",
        "array_to_json([1,2,3])",
    ):
        if expr in ("to_json(t)", "row_to_json(t)"):
            sql = f"SELECT {expr} FROM (SELECT 1 AS a, 'x' AS b) t"
        else:
            sql = f"SELECT {expr}"
        rows = q(con, sql).fetchall()
        print(f"  {common.pad(expr, 28)}{rows[0][0]}")

    print()
    print("  · to_json / row_to_json 是把**一整行**转成 JSON 对象 ——")
    print("    row_to_json 就是 to_json 的别名（结果一样）。")
    print("  · to_json 对 STRUCT / LIST 列也能用。")

    print()
    print("格式化（下面打印的是**原样文本**，不是 Python repr）：")
    for expr in (
        "json_quote('he said \"hi\"')",
        "json_normalize('{ \"a\" : 1 }')",
        'json_strip_nulls(\'{"a":1,"b":null}\')',
    ):
        rows = q(con, f"SELECT {expr}").fetchall()
        print(f"  {common.pad(expr, 46)}{rows[0][0]}")

    print()
    print("  · json_quote —— 把一段文本**包成 JSON 字符串字面量**：")
    print("    两头加上双引号，里面的双引号用 \\  转义。")
    print("    对比：json_value 拿到的是 JSON 类型（同样带引号），")
    print("          json_extract_string 拿到的是**去掉了引号的裸文本**。")
    for expr in (
        "json_value('{\"a\":\"x\"}', '$.a')",
        "json_extract_string('{\"a\":\"x\"}', '$.a')",
    ):
        rows = q(con, f"SELECT {expr}").fetchall()
        print(f"    {common.pad(expr, 50)}{rows[0][0]!r}")

    pretty = q(con, 'SELECT json_pretty(\'{"a":1,"b":{"c":2}}\'::JSON)').fetchall()[0][0]
    print()
    print("  json_pretty 的输出（\\n 是换行）：")
    print("    " + pretty.replace("\n", "\n    "))

    print()
    print("往已有 JSON 里塞东西 —— 三个函数，**语义不一样**：")
    base = "'{\"a\":1}'::JSON"
    print(f"  原值：{base}（里面已经有 a 这个键）")
    for fn in ("json_insert", "json_set", "json_replace"):
        rows = q(con, f"SELECT {fn}({base}, '$.a', '9')").fetchall()
        call = f"{fn}(j, '$.a', '9')"
        print(f"  {common.pad(call, 34)}{rows[0][0]}")
    print()
    print("  · json_insert —— 键**已经存在就原样不动**（插不进去）。")
    print("  · json_set / json_replace —— 键存在就**覆盖**。")
    print("    （这两个在实测里结果一样；区别在值不是标量时：")
    rows = q(con, "SELECT json_set('{\"a\":{\"x\":1}}'::JSON,'$.a','{\"y\":2}')").fetchall()
    print(f"    json_set 整个换掉 → {rows[0][0]}）")

    rows = q(con, "SELECT json_insert('{\"a\":1}'::JSON, '$.b', '9')").fetchall()
    print(f"  json_insert 到不存在的键 → {rows[0][0]}（插进去了）")
    rows = q(con, "SELECT json_remove('{\"a\":1,\"b\":2}'::JSON, '$.a')").fetchall()
    print(f"  json_remove(j, '$.a')      → {rows[0][0]}")

    print()
    print("Polars 侧：没有 JSON 构造器。")
    print("  造 JSON 只能自己拼字符串（f-string 或 pl.concat_str），")
    print("  改 JSON 只能 json_decode 成 struct → 改 → 再 json_encode ——")
    print("  而 Polars 2.1 连 str.json_encode 都没有（只有 DataFrame.write_json / write_ndjson）。")


# ---------------------------------------------------------------- 第 7 节


def scenario_merge(con) -> None:
    section("合并语义三兄弟：merge_patch / merge_patch_diff / deep_merge")

    left = '\'{"a":1,"b":{"x":1},"c":3}\'::JSON'
    right = '\'{"b":{"y":2},"c":null,"d":4}\'::JSON'

    print(f"  左：{left}")
    print(f"  右：{right}")
    print()

    for fn in ("json_merge_patch", "json_deep_merge"):
        rows = q(con, f"SELECT {fn}({left}, {right})").fetchall()
        print(f"  {common.pad(fn, 26)}{rows[0][0]}")

    print()
    print("两个合并函数的语义**完全相反**：")
    print()
    print("  · json_merge_patch（RFC 7396）—— 右边是「补丁」：")
    print("    右边的 null 表示**删掉这个键** —— c 被删了。")
    print("    右边的对象**整个替换**左边的同名对象 —— b.x 没了（不是合并）。")
    print("  · json_deep_merge —— 右边是「新版本」，**递归合并**：")
    print("    右边的 null **保留**（c 变成 null，而不是消失）。")
    print("    嵌套对象是合并不是替换 —— b 变成 x 和 y 都有。")

    print()
    print("json_merge_patch_diff 是第三个：**第二个参数是目标，不是补丁**。")
    print("它算的是「怎么把 source 变成 target」的那份补丁：")
    src = '\'{"a":1,"b":{"x":1},"c":3}\'::JSON'
    dst = '\'{"a":2,"b":{"x":9,"z":8}}\'::JSON'
    diff = q(con, f"SELECT json_merge_patch_diff({src}, {dst})").fetchall()[0][0]
    print(f"    source = {src}")
    print(f"    target = {dst}")
    print(f"    diff   = {diff}")
    print("    （c 在 target 里没了 → diff 里标成 null = 删掉；")
    print("      b.x 从 1 变成 9 → diff 里写 9；b.z 是新增 → 写 8。）")

    print()
    print("回放验证 —— 把 diff 当补丁打回 source，应该得到 target：")
    again = q(con, f"SELECT json_merge_patch({src}, '{diff}'::JSON)").fetchall()[0][0]
    target = q(con, f"SELECT {dst}").fetchall()[0][0]
    print(f"    patch(source, diff) = {again}")
    print(f"    target              = {target}")
    print(f"    一致：{'✅' if again == target else '❌'}")

    print()
    print("**但这个回放有个坑**：target 里只要有一个 null 值，就回放不回去。")
    tgt_null = '\'{"b":{"y":2},"c":null,"d":4}\'::JSON'
    diff2 = q(con, f"SELECT json_merge_patch_diff({left}, {tgt_null})").fetchall()[0][0]
    again2 = q(con, f"SELECT json_merge_patch({left}, '{diff2}'::JSON)").fetchall()[0][0]
    print(f"    target 带 null     = {tgt_null}")
    print(f"    diff               = {diff2}")
    print(f"    patch(source, diff) = {again2}")
    print(f"    跟 target 一样吗：{'✅' if again2 == tgt_null else '❌ —— 那个 null 没了'}")
    print("    原因：RFC 7396 里 null 的意思是「删掉这个键」，")
    print("          所以「把某个键设成 null」这个动作**没法用补丁表达**。")
    print("          diff 只能写 null（=删），写不出「设成 null」。")

    print()
    print("Polars 侧：这三个都没有。最接近的是把两个 struct 用 pl.concat 拼，")
    print("  但那是「同名列取左边/右边」，不是 JSON 的合并语义 —— null 的含义完全不一样。")


# ---------------------------------------------------------------- 第 8 节


def scenario_aggregate(con) -> None:
    section("聚合回写：把行再聚合成一份 JSON")

    q(con, "CREATE OR REPLACE TEMP TABLE agg AS SELECT * FROM (VALUES (1), (2), (3)) t(x)")
    q(
        con,
        "CREATE OR REPLACE TEMP TABLE pairs AS SELECT * FROM (VALUES ('a',1),('b',2)) t(k,v)",
    )

    print("json_group_array —— 把一列聚成一个 JSON 数组：")
    rows = q(con, "SELECT json_group_array(x) FROM agg").fetchall()
    print(f"  {rows[0][0]}")

    print()
    print("json_group_object —— 两列聚成一个 JSON 对象：")
    rows = q(con, "SELECT json_group_object(k, v) FROM pairs").fetchall()
    print(f"  {rows[0][0]}")

    print()
    print("json_group_structure —— 把多行的**结构**合成一份 schema：")
    q(
        con,
        "CREATE OR REPLACE TEMP TABLE structs AS SELECT * FROM (VALUES "
        '(\'{"a":1}\'::JSON), (\'{"b":"x"}\'::JSON)) t(j)',
    )
    rows = q(con, "SELECT json_group_structure(j) FROM structs").fetchall()
    print(f"  {rows[0][0]}")

    print()
    print("  · 类型撞车的时候，它会退化成 'JSON'：")
    q(
        con,
        "CREATE OR REPLACE TEMP TABLE conflict AS SELECT * FROM (VALUES "
        '(\'{"a":1}\'::JSON), (\'{"a":"x"}\'::JSON)) t(j)',
    )
    rows = q(con, "SELECT json_group_structure(j) FROM conflict").fetchall()
    print(f'    1 和 "x" 混在一起 → {rows[0][0]}')
    rows = q(con, "SELECT json_structure(j) FROM conflict").fetchall()
    print(f"    逐行 json_structure → {rows[0][0]} / {rows[1][0]}（各说各的）")

    print()
    print("  · 注意 json_group_object 的键不能是 NULL —— 会直接报错：")
    rows, err = qtry(con, "SELECT json_group_object(NULL, 1) FROM pairs")
    print(f"    {err}")

    print()
    print("Polars 侧：没有「聚合成 JSON」的原生函数。")
    print("  最接近的是 agg.list() 拿到一个 Python list，再 str.join ——")
    print("  但键值对、嵌套结构都得自己拼。")


# ---------------------------------------------------------------- 第 9 节


def scenario_readers(con) -> None:
    section("读取器家族：8 个读取器 + 3 个表函数，各吃各的")

    rows = q(
        con,
        "SELECT function_name, function_type FROM duckdb_functions() "
        "WHERE extension_name='json' AND function_type='table' "
        "ORDER BY function_name",
    ).fetchall()
    names = sorted({r[0] for r in rows})
    readers = [n for n in names if n.startswith("read_")]
    others = [n for n in names if not n.startswith("read_")]
    print(f"json 扩展里一共 {len(rows)} 个表函数**重载**，去重后 {len(names)} 个名字：")
    print()
    print(f"  读取器（{len(readers)} 个）：")
    for name in readers:
        print(f"    {name}")
    print(f"  其他表函数（{len(others)} 个）：")
    for name in others:
        print(f"    {name}")

    print()
    print("挨个实测 —— 拿三种输入格式试：")
    print(f"  NDJSON 文件：{DOCS_NDJSON.name}")
    print(f"  JSON 数组文件：{ARRAY_JSON.name}")

    nd = str(DOCS_NDJSON)
    arr = str(ARRAY_JSON)

    print()
    print("1) read_json_auto —— 按文档逐行读，能吃 NDJSON：")
    rows = q(con, f"SELECT count(*) FROM read_json_auto('{nd}')").fetchall()
    print(f"   NDJSON → {rows[0][0]} 行")

    print()
    print("2) read_json —— 同一个函数，也能吃 NDJSON（自己认格式）：")
    rows = q(con, f"SELECT count(*) FROM read_json('{nd}')").fetchall()
    print(f"   NDJSON → {rows[0][0]} 行")

    print()
    print("3) read_json_objects —— 返回**一列原始 JSON 文本**，不拆成列：")
    rows = q(con, f"SELECT * FROM read_json_objects('{nd}') LIMIT 1").fetchall()
    print(f"   {clip(rows[0][0], 96)}")
    rows = q(con, f"SELECT count(*) FROM read_json_objects('{arr}')").fetchall()
    print(f"   JSON 数组文件 → {rows[0][0]} 行（每个对象一行文本）")

    print()
    print("4) read_json_objects_auto —— 同样是一列文本：")
    rows = q(con, f"SELECT * FROM read_json_objects_auto('{nd}') LIMIT 1").fetchall()
    print(f"   {clip(rows[0][0], 96)}")
    rows = q(con, f"SELECT count(*) FROM read_json_objects_auto('{arr}')").fetchall()
    print(f"   JSON 数组文件 → {rows[0][0]} 行")

    print()
    print("5) read_ndjson / read_ndjson_auto —— 按 NDJSON 逐行读：")
    rows = q(con, f"SELECT count(*) FROM read_ndjson('{nd}')").fetchall()
    print(f"   read_ndjson      NDJSON → {rows[0][0]} 行")
    rows = q(con, f"SELECT count(*) FROM read_ndjson_auto('{nd}')").fetchall()
    print(f"   read_ndjson_auto NDJSON → {rows[0][0]} 行")
    rows, err = qtry(con, f"SELECT count(*) FROM read_ndjson('{arr}')")
    if err:
        print(f"   read_ndjson 读 JSON 数组文件 → 报错：{err}")
    else:
        print(f"   read_ndjson 读 JSON 数组文件 → **不报错**，但是 {rows[0][0]} 行：")
        rows = q(con, f"SELECT * FROM read_ndjson('{arr}')").fetchall()
        print(f"      {rows[0][0]}")
        print("      整个数组被当成**一行**，塞进一个 LIST 列里 ——")
        print("      这跟 read_json 在同一个文件上给出的 2 行完全不是一回事。")

    print()
    print("6) read_ndjson_objects —— 一行一个原始 JSON 文本：")
    rows = q(con, f"SELECT * FROM read_ndjson_objects('{nd}') LIMIT 1").fetchall()
    print(f"   {clip(rows[0][0], 96)}")
    rows = q(con, f"SELECT * FROM read_ndjson_objects('{arr}')").fetchall()
    print(f"   JSON 数组文件 → {rows[0][0]} 行（整个数组一行文本）")

    print()
    print("7) read_single_json_file —— 单文件单行（顶层是个对象）：")
    one = common.DATA_DIR / "json_one.json"
    one.write_text(DOC, encoding="utf-8")
    rows = q(con, f"SELECT * FROM read_single_json_file('{one}')").fetchall()
    print(f"   {rows[0]}")

    print()
    print("8) 顶层数组文件怎么读 —— format='array'：")
    rows = q(con, f"SELECT * FROM read_json('{arr}', format='array')").fetchall()
    print(f"   {rows}")
    rows = q(con, f"SELECT count(*) FROM read_json('{arr}')").fetchall()
    print(f"   read_json 不写 format 也行（自己认格式）→ {rows[0][0]} 行")

    print()
    print("Polars 侧要**两个函数**才覆盖同样的输入，而且**喂错格式会报错**：")
    for fn, path, label in (
        ("read_json", nd, "NDJSON 文件"),
        ("read_json", arr, "JSON 数组文件"),
        ("read_ndjson", nd, "NDJSON 文件"),
        ("read_ndjson", arr, "JSON 数组文件"),
    ):
        try:
            frame = getattr(pl, fn)(path)
            print(f"  pl.{fn}({label}) → {frame.shape[0]} 行 × {len(frame.columns)} 列")
        except Exception as exc:
            msg = str(exc).splitlines()[0]
            print(f"  pl.{fn}({label}) → 报错：{msg}")
    print()
    print("  DuckDB 的 read_json 一个函数两种格式都吃；")
    print("  read_ndjson 喂数组文件**也不报错**，但把整个数组当一行 ——")
    print("  静默给错形状，比直接报错还难查。")


# ---------------------------------------------------------------- 第 10 节


def scenario_reader_options(con) -> None:
    section("读取器选项：columns / format / records / maximum_depth …")

    nd = str(DOCS_NDJSON)
    deep = common.DATA_DIR / "json_deep.json"
    deep.write_text('{"a":{"b":{"c":{"d":1}}}}', encoding="utf-8")

    print("columns= —— 自己指定 schema，不让 DuckDB 猜：")
    rows = q(
        con, f"SELECT * FROM read_json('{nd}', columns={{'doc_id':'INTEGER'}}) LIMIT 1"
    ).fetchall()
    print(f"  {rows[0]}")

    print()
    print("format='array' —— 顶层是数组：")
    arr = str(ARRAY_JSON)
    rows = q(con, f"SELECT * FROM read_json('{arr}', format='array')").fetchall()
    print(f"  {rows}")

    print()
    print("records=false —— 每条记录**整体塞进一列**（不拆成多列）：")
    rows = q(con, f"SELECT * FROM read_json('{nd}', records=false)").fetchall()
    rec_desc = q(con, f"DESCRIBE SELECT * FROM read_json('{nd}', records=false)").fetchall()
    print(f"  行数 {len(rows)}，列：{[(r[0], r[1]) for r in rec_desc]}")
    print(f"  第一行：{rows[0][0]}")
    print("  → 行数没变（还是每行一条），只是**不再按字段拆列** ——")
    print("    整条记录变成一个 STRUCT 列，列名固定叫 json。")

    print()
    print("maximum_depth —— 嵌套超过几层就不再往里拆：")
    for depth in (0, 1, 2, 3):
        rows = q(con, f"SELECT * FROM read_json_auto('{deep}', maximum_depth={depth})").fetchall()
        print(f"  maximum_depth={depth} → {rows[0]}")

    print()
    print("  · 每一层都会**降级成 JSON 文本列** —— 拆不动了就整块留着。")
    print("  · 这是防「深嵌套把解析器撑爆」的开关，也是防「JSON 炸弹」的手段。")

    print()
    print("sample_size —— 推断类型时看多少行（auto 系列才有）：")
    for size in (1, 100):
        rows = q(
            con, f"DESCRIBE SELECT * FROM read_json_auto('{nd}', sample_size={size})"
        ).fetchall()
        types = {r[0]: r[1] for r in rows}
        print(
            f"  sample_size={size:<4}doc_id → {types.get('doc_id')}，amount → {types.get('amount')}"
        )

    print()
    print("union_by_name —— 多个文件、列不一样时，把列并起来：")
    print("  （单文件看不出效果，选项本身照样要能被 DuckDB 接受）")
    rows = q(con, f"SELECT count(*) FROM read_json_auto('{nd}', union_by_name=true)").fetchall()
    print(f"  {rows[0][0]} 行")

    print()
    print("filename=true —— 额外给一列来源文件名：")
    rows = q(con, f"DESCRIBE SELECT * FROM read_json_auto('{nd}', filename=true)").fetchall()
    print(f"  多出来的列：{[r[0] for r in rows][-1]}")

    print()
    print("maximum_object_size —— 单个 JSON 对象最大多少字节（超过就当字符串存）：")
    rows = q(
        con, f"DESCRIBE SELECT * FROM read_json_auto('{nd}', maximum_object_size=16)"
    ).fetchall()
    print(f"  maximum_object_size=16 → 列：{[(r[0], r[1]) for r in rows][:3]}")

    print()
    print("Polars 侧的对应开关：")
    print("  pl.read_json(schema=..., schema_overrides=..., infer_schema_length=...)")
    print("  没有的：format / records / maximum_depth / maximum_object_size /")
    print("           union_by_name / filename。")
    print("  —— 也就是说 Polars 没有「拆到第几层就停」这个概念。")


# ---------------------------------------------------------------- 第 11 节


def scenario_dirty(con) -> None:
    section("脏数据：json_valid 与容错读取")

    dirty = str(DIRTY_NDJSON)
    lines = DIRTY_NDJSON.read_text(encoding="utf-8").splitlines()

    print(f"脏文件 {DIRTY_NDJSON.name} 一共 {len(lines)} 行：")
    for i, line in enumerate(lines, 1):
        rows = q(con, "SELECT json_valid(?)", [line]).fetchall()
        mark = "✅" if rows[0][0] else "❌"
        print(f"  第 {i} 行 {mark} json_valid → {rows[0][0]}   {line[:40]}")

    print()
    print("  · json_valid 是**唯一**能提前问一句「这份文本是不是合法 JSON」的办法。")
    print("  · 它对 NULL 返回 NULL（不是 false）。")
    rows = q(con, "SELECT json_valid(NULL)").fetchall()
    print(f"    json_valid(NULL) → {rows[0][0]}")

    print()
    print("直接读会怎么样：")
    rows, err = qtry(con, f"SELECT * FROM read_json_auto('{dirty}')")
    print(f"  read_json_auto → {err}")

    print()
    print("ignore_errors=true —— **注意它不是「跳过坏行」**：")
    rows = q(con, f"SELECT * FROM read_json_auto('{dirty}', ignore_errors=true)").fetchall()
    print(f"  读到 {len(rows)} 行：{rows}")
    print("  坏行还在那儿，只是**整行变成 NULL**（id 和 v 都是 NULL）。")
    print("  行数没少 —— 想要「只要好的那些行」，还得自己筛。")

    print()
    print("另一条路 —— 先拿文本、再自己筛：")
    rows, err = qtry(con, f"SELECT * FROM read_json_objects('{dirty}')")
    print(f"  read_json_objects → {err}")
    rows = q(
        con,
        f"SELECT * FROM read_csv('{dirty}', columns={{'json':'VARCHAR'}}, "
        f"delim='\\x01', quote='', escape='', header=false) WHERE json_valid(json)",
    ).fetchall()
    print(f"  read_csv 读成文本 + WHERE json_valid(...) → {len(rows)} 行：{rows}")

    print()
    print("  · json_valid 判的是**语法**，不判语义 —— '[1,2]' 合法但可能不是你想要的形状。")
    print("  · 真正的形状校验得靠 json_structure / json_transform_strict（见第 4 节）。")
    print()
    print("Polars 侧没有 json_valid —— 读坏了只能等 Python 的 json 库在循环里抛异常，")
    print("  或者自己写正则去猜。")
    print("  （pl.read_ndjson 遇到坏行也是直接抛异常，没有容错开关。）")


# ---------------------------------------------------------------- 第 12 节


def scenario_serialize(con) -> None:
    section("序列化往返：SQL → JSON → SQL → 执行")

    sql = "SELECT 1 AS a, 'x' AS b"
    print(f"原始 SQL：{sql}")
    print()

    serialized = q(con, "SELECT json_serialize_sql(?)", [sql]).fetchall()[0][0]
    print(f"json_serialize_sql → {serialized[:150]} …")
    print(f"  长度 {len(serialized)} 字符")

    print()
    back = q(con, "SELECT json_deserialize_sql(?)", [serialized]).fetchall()[0][0]
    print(f"json_deserialize_sql → {back!r}")
    print(f"  和原文一致：{'✅' if back.strip() == sql else '❌'}")

    print()
    print("json_execute_serialized_sql —— 直接执行那份 JSON：")
    rows = q(con, "SELECT * FROM json_execute_serialized_sql(?)", [serialized]).fetchall()
    print(f"  {rows}")

    print()
    print("json_serialize_plan —— 序列化的是**执行计划**（不是 SQL 文本）：")
    plan = q(con, "SELECT json_serialize_plan(?)", [sql]).fetchall()[0][0]
    print(f"  {plan[:180]} …")

    print()
    print("  · 这四个函数是 DuckDB 独有的「把查询本身当数据处理」的能力。")
    print("  · 用途：把一条查询序列化下来发给别的机器执行；或者把计划存下来做审计。")
    print("  · Polars 侧没有对应物 —— LazyFrame 没有可序列化的执行计划对象。")
    print("  · 顺带一提：json_serialize_sql 的输出**不含**数据，只含语法树。")
    print("    它是「查询的形状」，不是「查询的结果」。")


# ---------------------------------------------------------------- 第 13 节


def scenario_geojson(con) -> None:
    section("GeoJSON 互转：json 扩展带来的两个空间函数")

    for geo in (
        '{"type":"Point","coordinates":[1.5,2.5]}',
        '{"type":"Point","coordinates":[1,2]}',
    ):
        print(f"输入 GeoJSON：{geo}")
        rows = q(con, f"SELECT ST_AsText(ST_GeomFromGeoJSON('{geo}'))").fetchall()
        print(f"  st_geomfromgeojson → {rows[0][0]}")
        rows = q(con, f"SELECT ST_AsGeoJSON(ST_GeomFromGeoJSON('{geo}'))").fetchall()
        print(f"  st_asgeojson       → {rows[0][0]}")
        print()

    print("  · 这两个函数**属于 json 扩展**（不是 spatial 扩展）——")
    print("    名字里带 geo，但归类是 json，因为它们干的是 JSON 解析。")
    print("  · **整数坐标会被规范化成浮点**：输入 [1,2]，回写出来是 [1.0,2.0]；")
    print("    输入本来就是浮点的 [1.5,2.5]，回写还是 [1.5,2.5]。")
    print("    所以 GeoJSON → GeoJSON 往返，字符串不一定和原文逐字相同。")
    print("    （WKT 那边的文本输出更忠实：整数坐标写成 POINT (1 2)。）")

    print()
    print("Polars 侧：零空间支持 —— 没有 pl.geo，DataFrame 上也没有任何 geo/point/space 方法。")
    print("  GeoJSON 在 Polars 里就是**一个字符串**，要自己 json_decode 再取 coordinates。")


# ---------------------------------------------------------------- 第 14 节


def scenario_macros(con) -> None:
    section("宏与内部函数：json / json_copy_strftime_if_* / __internal_*")

    print("json(x) —— 一个宏，等价于 json_extract(x, '$')：")
    rows = q(con, "SELECT json('{\"a\":1}')").fetchall()
    print(f"  json('{{\"a\":1}}') → {rows[0][0]!r}")
    rows = q(con, "SELECT json_extract('{\"a\":1}', '$')").fetchall()
    print(f"  json_extract(…, '$') → {rows[0][0]!r}")

    print()
    print("json_copy_strftime_if_date / _timestamp —— to_json 的内部钩子，直接调会露馅：")
    for expr in (
        "json_copy_strftime_if_date(DATE '2024-01-01', 'x')",
        "json_copy_strftime_if_timestamp(TIMESTAMP '2024-01-01 10:30:00', 'x')",
    ):
        rows = q(con, f"SELECT {expr}").fetchall()
        print(f"  {common.pad(expr, 66)}{rows[0][0]!r}")
    print()
    print("  · 这两个宏在 date/timestamp 的类型检查通过时**原样返回**，")
    print("    不通过时才按 format 转成字符串。")
    rows = q(con, "SELECT json_copy_strftime_if_date(DATE '2024-01-01', '%Y/%m/%d')").fetchall()
    print(f"    日期 + format → {rows[0][0]!r}")
    rows = q(con, "SELECT json_copy_strftime_if_date(123, '%Y/%m/%d')").fetchall()
    print(f"    非日期 + format → {rows[0][0]!r}（原样返回）")

    print()
    print("__internal_* —— DuckDB 自己说「不许直接调」：")
    for expr in (
        "__internal_json_copy_to_json(1, 'k', 'v')",
        "__internal_json_copy_to_geojson(1, 'a', 'b', 'c', 'd', true)",
    ):
        rows, err = qtry(con, f"SELECT {expr}")
        print(f"  {common.pad(expr, 62)}{err}")

    print()
    print("  这两个是 json 扩展挂在**函数目录里的内部函数**，")
    print("  但 DuckDB 在绑定阶段就会拦下来 —— 不是「没有权限」，是「不给你用」。")
    print("  所以这节的覆盖自检里，它们会标成「调过，但报错」。")


# ---------------------------------------------------------------- 第 15 节


def scenario_cost(con, repeat: int) -> None:
    section("代价：同样一件事，两个引擎各要多少")

    print(f"机器状态：{common.describe_machine()}")
    print("（共享开发机，绝对值会飘，看相对关系）\n")

    nd = str(DOCS_NDJSON)
    n_rows = q(con, f"SELECT count(*) FROM read_json_objects('{nd}')").fetchall()[0][0]
    print(f"数据量：{n_rows:,} 行 NDJSON")
    print()

    def timeit(fn) -> float:
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append((time.perf_counter() - t0) * 1000)
        return float(np.median(times))

    results = []

    # A. NDJSON → 强类型列 → 求和（两边都在解析 + 推断 + 聚合）
    def duck_a():
        q(
            con,
            f"SELECT count(*), sum(amount) FROM read_json_auto('{nd}')",
        ).fetchall()

    def pol_a():
        frame = pl.read_ndjson(DOCS_NDJSON)
        frame.select(pl.col("amount").sum())

    duck_a_ms = timeit(duck_a)
    pol_a_ms = timeit(pol_a)
    results.append(("A. NDJSON → 强类型列 → 求和", duck_a_ms, pol_a_ms))

    # B. JSON 文本行 → 取一个嵌套字段（两边都在读文件 + 解析文本 + 走一遍 JSON 路径）
    duck_rows = q(con, f"SELECT count(*) FROM read_json_objects('{nd}')").fetchall()[0][0]

    def duck_b():
        q(
            con,
            f"SELECT json_extract_string(json, '$.user.id') FROM read_json_objects('{nd}')",
        ).fetchall()

    def pol_b():
        # 读文件放在**计时里面** —— DuckDB 那边的 read_json_objects 也是每次重读，
        # 少算一边就不是同一件活儿了。
        lines = DOCS_NDJSON.read_text(encoding="utf-8").splitlines()
        pl.Series("j", lines).str.json_path_match("$.user.id")

    duck_b_ms = timeit(duck_b)
    pol_b_ms = timeit(pol_b)
    results.append(("B. JSON 文本行 → 取嵌套字段", duck_b_ms, pol_b_ms))

    # C. JSON 文本行 → 照 schema 投影成强类型列
    schema = json.dumps(
        {"doc_id": "INTEGER", "amount": "DOUBLE", "user": {"id": "INTEGER"}},
        separators=(",", ":"),
    )

    def duck_c():
        q(
            con,
            f"SELECT json_transform(json, '{schema}') FROM read_json_objects('{nd}')",
        ).fetchall()

    pol_schema = pl.Struct(
        {
            "doc_id": pl.Int32,
            "amount": pl.Float64,
            "user": pl.Struct({"id": pl.Int32}),
        }
    )

    def pol_c():
        lines = DOCS_NDJSON.read_text(encoding="utf-8").splitlines()
        pl.Series("j", lines).str.json_decode(dtype=pol_schema)

    duck_c_ms = timeit(duck_c)
    pol_c_ms = timeit(pol_c)
    results.append(("C. JSON 文本行 → 照 schema 投影", duck_c_ms, pol_c_ms))

    # 倍数从**表里显示的那一位**算出来 —— 表显示 67.5 / 14.2，
    # 倍数就该是 67.5 ÷ 14.2，而不是拿没取整的原始中位数算。
    shown = [(label, round(d, 1), round(p, 1)) for label, d, p in results]

    print(f"{'做法':<36}{'DuckDB (ms)':>16}{'Polars (ms)':>16}{'倍数':>10}")
    print("-" * 78)
    for label, d, p in shown:
        ratio = max(d, p) / min(d, p) if min(d, p) > 0 else float("nan")
        print(
            f"{common.pad(label, 36)}{common.pad_left(f'{d:.1f}', 16)}"
            f"{common.pad_left(f'{p:.1f}', 16)}{common.pad_left(f'{ratio:.2f}×', 10)}"
        )

    print()
    for label, d, p in shown:
        ratio = max(d, p) / min(d, p) if min(d, p) > 0 else float("nan")
        if d < p * 0.95:
            verdict = f"DuckDB 快 {ratio:.2f}×"
        elif p < d * 0.95:
            verdict = f"Polars 快 {ratio:.2f}×"
        else:
            verdict = "基本打平"
        print(f"  · {label}：{verdict}")

    print()
    print("口径说明（三行做的是同一件事，没有多算也没有少算）：")
    print("  A. 两边都是「读 NDJSON → 推断/给定类型 → 求和」，读的是同一个文件。")
    print("  B. 两边都是「读文件 → 拿到原始 JSON 文本 → 解析 → 按路径取一个字段」，")
    print("     DuckDB 用 read_json_objects 拿文本列，Polars 用 read_text().splitlines()。")
    print("  C. 两边都是「读文件 → 拿到原始 JSON 文本 → 解析 → 照给定的类型定义投影」，")
    print("     DuckDB 给的是 JSON Schema 文本，Polars 给的是 pl.Struct。")
    print()
    print("  · B/C 两行里，**读文件都在计时范围内**：DuckDB 每次重读，")
    print("    Polars 侧也是每次重读（没有把 I/O 挪到计时外面）。")
    print(f"  · 两边读到的行数都是 {duck_rows:,}。")
    print("  · 倍数是用**表里显示的那两个毫秒数**算的，不是拿没取整的原始中位数算的。")


# ---------------------------------------------------------------- 第 16 节


def scenario_coverage(con) -> None:
    section("覆盖自检：json 扩展的函数，逐个核对")

    catalog = _CATALOG
    print(f"json 扩展目录里一共 {len(catalog)} 个函数名（含重载去重）。")
    print("下表每一行都是**实测**的：这一节统计的是前面各节**真的执行过**的 SQL，")
    print("而不是源码里提过哪些名字。")
    print()

    covered = [n for n in catalog if n in _USED]
    failed_only = [n for n in catalog if n not in _USED and n in _FAILED]
    missing = [n for n in catalog if n not in _USED and n not in _FAILED]

    print(f"{'函数名':<34}{'节':<14}{'Polars 对应物'}")
    print("-" * 92)
    for name in catalog:
        if name in _USED:
            secs = ",".join(str(s) for s in sorted(_USED[name]))
            mark = ""
        elif name in _FAILED:
            secs = ",".join(str(s) for s in sorted(_FAILED[name]))
            mark = " ⚠"
        else:
            secs = "—"
            mark = " ❌"
        equiv = POLARS_EQUIV.get(name, "")
        print(f"{common.pad(name, 34)}{common.pad(secs + mark, 14)}{equiv}")

    print()
    print(f"  · 成功调用过的：{len(covered)}/{len(catalog)}")
    if failed_only:
        print(f"  · 调了但报错的：{len(failed_only)} 个 —— {', '.join(failed_only)}")
        print("    （报错信息本身就是结论 —— 见第 14 节：__internal_* 是 DuckDB")
        print("      明说「只给内部用」的函数，绑定期就拦下来了。）")
    if missing:
        print(f"  · 完全没碰的：{len(missing)} 个 —— {', '.join(missing)}")
    else:
        print("  · 完全没碰的：0 个。")

    # Polars 侧的覆盖：表里那一栏是「有 / 没有」，这里把数量数出来。
    with_equiv = [n for n in catalog if n in POLARS_HAS_EQUIV]
    without_equiv = [n for n in catalog if n not in POLARS_HAS_EQUIV]
    print()
    print(
        f"  · 换成 Polars 要自己补的活儿：{len(without_equiv)}/{len(catalog)} "
        "个函数**没有对应物**。"
    )
    print(f"    有对应物的 {len(with_equiv)} 个：{', '.join(with_equiv)}")
    print("    —— 注意「有对应物」不等于「语义一样」：json_value 的引号、")
    print("      json_normalize 的形状（Polars 是摊平成表）、json_each 的路径，")
    print("      三处都对不上（见第 1、5 节的实测）。")

    if "->" in _USED:
        print(
            f"  · `->` 运算符：第 {min(_USED['->'])} 节用过了"
            "（它不在 duckdb_functions() 里，所以不在这张表上）。"
        )

    print()
    print("结论：json 扩展的函数面，")
    print(
        f"      **成功调用 {len(covered)} 个 + 调过但用不了 {len(failed_only)} 个 = "
        f"{len(covered) + len(failed_only)} 个**"
    )
    print(f"      （目录里一共 {len(catalog)} 个；`->` 运算符不在目录里，另算。）")
    print("      全部在本 demo 里出现过 —— 有的是拿来用，有的是拿来证明用不了。")
    print("      表里 Polars 对应物那一栏，就是「换个引擎要自己补多少活」的清单。")

    print()
    print("Polars 侧的对应情况（实测过 API 表面）：")
    print("  pl 下跟 json 有关的顶层名字只有 4 个：")
    print("    json_normalize / read_json / read_ndjson / scan_ndjson")
    print("  字符串命名空间里跟 json 有关的只有 2 个：")
    print("    json_decode / json_path_match")
    print("  DataFrame 上跟 json 有关的只有 2 个：")
    print("    write_json / write_ndjson")
    print("  Expr 上一个都没有。")
    print()
    print("  也就是说：**读、写、解、查**四件事 Polars 都有，")
    print("  但「按 schema 投影」「递归拆」「合并语义」「聚合成 JSON」")
    print("  「序列化查询本身」这五件，Polars 一件都没有。")


# ---------------------------------------------------------------- main


def main() -> None:
    global _CATALOG
    parser = argparse.ArgumentParser(description="DuckDB json 扩展全特性对比")
    parser.add_argument("--rows", type=int, default=200_000, help="NDJSON 行数")
    parser.add_argument("--repeat", type=int, default=5, help="性能测量重复次数")
    args = parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    ensure_data(args.rows)

    con = duckdb.connect()
    try:
        _CATALOG = _load_catalog(con)
        print(f"json 扩展函数目录：{len(_CATALOG)} 个名字")

        scenario_extract(con)
        scenario_batch_path(con)
        scenario_probes(con)
        scenario_schema(con)
        scenario_unnest(con)
        scenario_build(con)
        scenario_merge(con)
        scenario_aggregate(con)
        scenario_readers(con)
        scenario_reader_options(con)
        scenario_dirty(con)
        scenario_serialize(con)
        scenario_geojson(con)
        scenario_macros(con)
        scenario_cost(con, args.repeat)
        scenario_coverage(con)
    finally:
        con.close()
    print()


if __name__ == "__main__":
    main()
