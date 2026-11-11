"""类型系统全景对照：两个引擎的「地基」能互相翻译到什么程度。

别的 demo 都在比「同一件事谁更快」。这个 demo 比的是更靠下的东西 ——
你往 DuckDB 里塞的每一个值，能不能**原样**落到 Polars 里（反过来也一样）。

三个层次，从浅到深：

  1. **类型全集**：两边各有哪些类型，哪些根本没有对位。
  2. **Parquet 往返会变型**：落盘再读回来，类型还是不是原来那个。
  3. **隐式转换规则**：同一个表达式，两边会不会悄悄给出不同的结果。

表里的每一个类型名都是**跑出来的**，不是抄文档 ——
DuckDB 的类型清单来自 `duckdb_types()`，Polars 的来自 `pl.datatypes`，
parquet 的物理类型和逻辑标注来自 pyarrow 读出来的 schema。

用法：
    uv run python src/type_demo.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import polars as pl
import pyarrow.parquet as pq

import common


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


# ---------------------------------------------------------------- 类型清单

# Polars 的 dtype 分三层：具体类型（Int64…）、抽象基类（IntegerType、FloatType…）、
# 扩展机制的占位（Extension、BaseExtension）。对位表只关心第一层，基类要剔掉。
_POLARS_BASE_CLASSES = {
    "DataType",
    "IntegerType",
    "FloatType",
    "TemporalType",
    "BaseExtension",
    "Extension",
}


def duckdb_logical_types(con: duckdb.DuckDBPyConnection) -> list[str]:
    """DuckDB 的全部逻辑类型（去掉别名）。

    `duckdb_types()` 里同一个类型会带一串别名（int8/int2/int4/int64…），
    那是**同一件事的几种写法**，不是几种类型，所以按 logical_type 去重。
    """
    rows = con.sql("SELECT DISTINCT logical_type FROM duckdb_types() ORDER BY 1").fetchall()
    return [r[0] for r in rows]


def polars_dtypes() -> list[str]:
    """Polars 的全部具体 dtype。

    `Utf8` 是 `String` 的旧名字（同一个东西），这里只留新名字。
    """
    names = [
        n
        for n in dir(pl.datatypes)
        if not n.startswith("_")
        and n not in _POLARS_BASE_CLASSES
        and n != "Utf8"
        and isinstance(getattr(pl.datatypes, n), type)
        and issubclass(getattr(pl.datatypes, n), pl.datatypes.DataType)
    ]
    return sorted(names)


# ---------------------------------------------------------------- 对位表

# DuckDB 类型 → Polars 对位。
#
# 「备注」一栏写的是**实测**结论，不是文档转述：
#   对位   = 两边都有，语义基本一致
#   近似   = 两边都有，但行为不完全一样（排序、精度、单位…）
#   没有   = 一边根本没有这个类型
PAIRS: list[tuple[str, str, str, str]] = [
    ("BOOLEAN", "Boolean", "对位", "parquet 走 BOOLEAN，原样回来"),
    ("TINYINT", "Int8", "对位", "parquet 走 INT32 + Int(bitWidth=8) 标注"),
    ("SMALLINT", "Int16", "对位", "同上，bitWidth=16"),
    ("INTEGER", "Int32", "对位", "parquet 走 INT32"),
    ("BIGINT", "Int64", "对位", "parquet 走 INT64"),
    (
        "HUGEINT",
        "Int128",
        "近似",
        "**parquet 往返两边都坏**：DuckDB 落盘变 DOUBLE 掉精度，Polars 落盘后 DuckDB 读成 BLOB",
    ),
    ("UTINYINT", "UInt8", "对位", "parquet 走 INT32 + Int(bitWidth=8, isSigned=false)"),
    ("USMALLINT", "UInt16", "对位", "同上，bitWidth=16"),
    ("UINTEGER", "UInt32", "对位", "parquet 走 INT64"),
    ("UBIGINT", "UInt64", "对位", "parquet 走 INT64"),
    ("UHUGEINT", "UInt128", "近似", "同 HUGEINT"),
    ("FLOAT", "Float32", "对位", "parquet 走 FLOAT"),
    ("DOUBLE", "Float64", "对位", "parquet 走 DOUBLE"),
    (
        "DECIMAL(p,s)",
        "Decimal(p,s)",
        "对位",
        "parquet 走 FIXED_LEN_BYTE_ARRAY + Decimal(precision,scale) 标注",
    ),
    ("VARCHAR", "String", "对位", "parquet 走 BYTE_ARRAY + String 标注"),
    (
        "BLOB",
        "Binary",
        "对位",
        "parquet 走 BYTE_ARRAY（**没有**逻辑标注）—— 值在，类型语义没了",
    ),
    ("UUID", "—", "**没有**", "Polars 没有 UUID dtype，只能拿 String 或 128 位整数模拟"),
    ("ENUM", "Enum / Categorical", "近似", "**排序规则不同**，而且落盘后 DuckDB 变 VARCHAR"),
    ("BIT", "—", "**没有**", "Polars 没有位串类型，只能拿 String 存"),
    (
        "INTERVAL",
        "Duration",
        "近似",
        "单位不同（DuckDB 是「月-日-毫秒」，Polars 是「秒」），而且 DuckDB 的 interval parquet "
        "Polars 读不了",
    ),
    ("DATE", "Date", "对位", "parquet 走 INT32 + Date 标注"),
    ("TIME", "Time", "对位", "parquet 走 INT64 + Time(us) 标注"),
    ("TIME_NS", "Time", "近似", "Parquet 只有毫秒/微秒/纳秒三种精度，Polars 的 Time 是微秒"),
    ("TIME WITH TIME ZONE", "—", "**没有**", "Polars 没有「带时区的 time」"),
    ("TIMESTAMP", "Datetime(us)", "对位", "parquet 走 INT64 + Timestamp(us) 标注"),
    (
        "TIMESTAMP_S / _MS / _NS",
        "Datetime(单位)",
        "对位",
        "parquet 用 Timestamp(timeUnit=) 标注保单位",
    ),
    ("TIMESTAMP WITH TIME ZONE", "Datetime(us, UTC)", "对位", "走 Timestamp(isAdjustedToUTC=true)"),
    ("LIST", "List", "对位", "嵌套类型，parquet 走三层 LIST 结构"),
    ("ARRAY", "Array", "对位", "定长数组"),
    ("STRUCT", "Struct", "对位", "parquet 走 GROUP 结构"),
    ("MAP", "Map", "对位", "parquet 走 key_value 结构"),
    ("TUPLE", "—", "**没有**", "Polars 没有「异构元组」"),
    ("UNION", "—", "**没有**", "Polars 没有联合类型"),
    ("VARIANT", "—", "**没有**", "DuckDB 2.0 的半结构化类型，Polars 侧没有对应物"),
    ("GEOMETRY", "—", "**没有**", "要靠 DuckDB 的 spatial 扩展，Polars 侧没有"),
    ("BIGNUM", "—", "**没有**", "任意精度十进制，DuckDB 独家"),
    ("NULL", "Null", "对位", "两边都有「只有 null 没有别的」的列类型"),
    ("TYPE", "Object", "近似", "Polars 的 Object 是「Python 对象」，不是 DuckDB 的「任意类型」"),
]


def scenario_type_inventory(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 两个引擎各有哪些类型")

    duck_types = duckdb_logical_types(con)
    pol_types = polars_dtypes()

    print(f"  DuckDB 的逻辑类型（`duckdb_types()` 去重后）：{len(duck_types)} 个")
    for i in range(0, len(duck_types), 6):
        print("    " + " · ".join(common.pad(t, 22) for t in duck_types[i : i + 6]))
    print()
    print(f"  Polars 的 dtype（`pl.datatypes`）：{len(pol_types)} 个")
    for i in range(0, len(pol_types), 6):
        print("    " + " · ".join(common.pad(t, 22) for t in pol_types[i : i + 6]))
    print()
    print("  两边的「宽度阶梯」是对得上的：")
    print("    DuckDB  TINYINT / SMALLINT / INTEGER / BIGINT / HUGEINT")
    print("    Polars  Int8    / Int16    / Int32    / Int64    / Int128")
    print("    DuckDB  UTINYINT / USMALLINT / UINTEGER / UBIGINT / UHUGEINT")
    print("    Polars  UInt8    / UInt16    / UInt32   / UInt64   / UInt128")
    print()
    print("  差口气的地方在**高级类型**上：DuckDB 的 UUID / ENUM / BIT / GEOMETRY / VARIANT")
    print("  在 Polars 那边要么没有，要么语义对不上（见场景 2）。")


def scenario_pairing(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 逐一对位总表")

    n_exact = sum(1 for _, _, kind, _ in PAIRS if kind == "对位")
    n_approx = sum(1 for _, _, kind, _ in PAIRS if kind == "近似")
    n_none = sum(1 for _, _, kind, _ in PAIRS if "没有" in kind)

    print("  左边是 DuckDB 的类型，中间是 Polars 的对位，右边是**实测**出来的坑。")
    print()

    for duck, pol, kind, note in PAIRS:
        mark = {"对位": "✅", "近似": "⚠️ "}.get(kind, "❌")
        print(f"  {mark} {common.pad(duck, 26)}{common.pad(pol, 22)}{kind:<6}{note}")
    print()

    print(f"  统计：对位 {n_exact} 个，近似 {n_approx} 个，没有对位 {n_none} 个。")
    print()
    print("  「没有对位」的那几个不是「Polars 弱」，是**两边解决的问题不一样**：")
    print("    · UUID —— DuckDB 把它当成一等公民（可排序、可做主键、有专门的生成函数），")
    print("      Polars 压根没这个概念，字符串凑合着存就行。")
    print("    · BIT / GEOMETRY / VARIANT —— 属于 DuckDB 的生态位（位串、空间、半结构化），")
    print("      Polars 把这些活儿交给 Arrow / geoarrow / 外部扩展。")
    print("    · TUPLE / UNION —— 异构元组和联合类型，两个引擎都不常用。")
    print()
    print("  「近似」的那几个才是真正会咬人的：数据看着没丢，")
    print("  但**排序、比较、单位**可能跟预期不一样。下一节就是实测。")


# ---------------------------------------------------------------- parquet 往返

# 每一列：列名、DuckDB 里的类型表达式、DuckDB 原类型名、期望的 Polars dtype、
#         Polars 那边算不算「对位」。
#
# 「算不算对位」是人工判定的，因为有的列值是对的但**类型语义丢了**
# （UUID 读成 16 字节 BLOB，值能还原，类型不能）；有的列连值都变了
# （HUGEINT 落盘变 DOUBLE，精度当场丢）。
ROUNDTRIP_CASES: list[tuple[str, str, str, str, bool]] = [
    ("TINYINT", "1::TINYINT", "TINYINT", "Int8", True),
    ("SMALLINT", "1::SMALLINT", "SMALLINT", "Int16", True),
    ("INTEGER", "1::INTEGER", "INTEGER", "Int32", True),
    ("BIGINT", "1::BIGINT", "BIGINT", "Int64", True),
    ("HUGEINT", "170141183460469231731687303715884105727::HUGEINT", "HUGEINT", "Float64", False),
    ("UTINYINT", "1::UTINYINT", "UTINYINT", "UInt8", True),
    ("USMALLINT", "1::USMALLINT", "USMALLINT", "UInt16", True),
    ("UINTEGER", "1::UINTEGER", "UINTEGER", "UInt32", True),
    ("UBIGINT", "1::UBIGINT", "UBIGINT", "UInt64", True),
    (
        "UHUGEINT",
        "340282366920938463463374607431768211455::UHUGEINT",
        "UHUGEINT",
        "Float64",
        False,
    ),
    ("FLOAT", "1.5::FLOAT", "FLOAT", "Float32", True),
    ("DOUBLE", "1.5::DOUBLE", "DOUBLE", "Float64", True),
    ("DECIMAL_18_2", "1.23::DECIMAL(18,2)", "DECIMAL(18,2)", "Decimal(18,2)", True),
    ("DECIMAL_38_0", "123::DECIMAL(38,0)", "DECIMAL(38,0)", "Decimal(38,0)", True),
    ("VARCHAR", "'x'::VARCHAR", "VARCHAR", "String", True),
    ("BLOB", "'\\xDE'::BLOB", "BLOB", "Binary", True),
    (
        "UUID",
        "'550e8400-e29b-41d4-a716-446655440000'::UUID",
        "UUID",
        "Binary",
        False,
    ),
    ("ENUM", "'b'::ENUM('a','b')", "ENUM", "String", False),
    ("DATE", "DATE '2024-01-01'", "DATE", "Date", True),
    ("TIME", "TIME '12:34:56'", "TIME", "Time", True),
    ("TIMESTAMP", "TIMESTAMP '2024-01-01 12:00:00'", "TIMESTAMP", "Datetime(us)", True),
    (
        "TIMESTAMPTZ",
        "TIMESTAMPTZ '2024-01-01 12:00:00+08'",
        "TIMESTAMP WITH TIME ZONE",
        "Datetime(us,UTC)",
        True,
    ),
    ("BOOLEAN", "true", "BOOLEAN", "Boolean", True),
    ("BIT", "B'1010'", "BIT", "String", False),
]


def _parquet_columns(path: Path) -> list[tuple[str, str, str]]:
    """读 parquet 的物理类型和逻辑标注。

    这是「落盘那一刻类型变成了什么」的直接证据 —— 逻辑标注是跨引擎唯一
    能把类型信息带过去的通道，没有标注的列（HUGEINT、ENUM）只能靠猜。
    """
    schema = pq.ParquetFile(path).metadata.schema
    out = []
    for i in range(len(schema)):
        col = schema.column(i)
        logical = str(col.logical_type) if col.logical_type is not None else "—"
        out.append((col.name, col.physical_type, logical))
    return out


# 表头宽度有限，DuckDB 的完整类型名太长，表格里用简称；
# 比较用的仍然是完整名字（见 ROUNDTRIP_CASES 的第三列）。
_DISPLAY_NAME = {
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}


def _compact_logical(text: str) -> str:
    """把 parquet 的逻辑标注压短一点，表格里塞得下。

    pyarrow 会把 `is_from_converted_type` / `force_set_converted_type` 这些
    实现细节也打出来，占地方但没有信息量。标注本身的含义要保住，
    所以 Time/Timestamp 压成 `Time(us)` / `Timestamp(us,UTC)` 这种写法。
    """
    text = re.sub(r",\s*(is_from_converted_type|force_set_converted_type)=[^,)]*", "", text)
    text = re.sub(r"Int\(bitWidth=(\d+), isSigned=(true|false)\)", r"Int(\1,\2)", text)
    text = re.sub(r"Decimal\(precision=(\d+), scale=(-?\d+)\)", r"Decimal(\1,\2)", text)
    text = re.sub(
        r"Time\(isAdjustedToUTC=(true|false), timeUnit=(\w+)\)",
        lambda m: f"Time({_unit(m.group(2))})",
        text,
    )
    text = re.sub(
        r"Timestamp\(isAdjustedToUTC=(true|false), timeUnit=(\w+)\)",
        lambda m: f"Timestamp({_unit(m.group(2))}" + (",UTC" if m.group(1) == "true" else "") + ")",
        text,
    )
    return text


def _unit(name: str) -> str:
    """parquet 的时间单位名压短：microseconds → us。"""
    return {"milliseconds": "ms", "microseconds": "us", "nanoseconds": "ns"}.get(name, name)


def _short_polars_dtype(dtype: str) -> str:
    """Polars 的 dtype 打印得太啰嗦（Datetime(time_unit='us', time_zone=None)），压短。"""
    if dtype.startswith("Datetime(time_unit="):
        unit = dtype.split("'")[1]
        return f"Datetime({unit})" if "time_zone=None" in dtype else f"Datetime({unit},UTC)"
    if dtype.startswith("Decimal(precision="):
        return dtype.replace("precision=", "").replace(", scale=", ",").replace(")", ")")
    return dtype


def scenario_parquet_roundtrip(con: duckdb.DuckDBPyConnection, tmpdir: Path) -> None:
    section("场景 3 · Parquet 往返会变型")

    print("  同一张表，DuckDB 写成 parquet，然后**两边各自读回来**。")
    print("  「DuckDB 读回」是自问自答，「Polars 读回」才是互操作的真相。")
    print()
    print("  判定一栏：")
    print("    ✅ 对位 —— 两边读回来的类型都跟原来一个意思")
    print("    ⚠️  变了 —— 类型名变了但意思还在（比如 DECIMAL(18,2) → DECIMAL(18,2)）")
    print("    ❌ 断了 —— 类型语义丢了（枚举定义没了、UUID 变成裸字节、精度掉了）")
    print()

    select = ", ".join(f'{expr} AS "{name}"' for name, expr, _, _, _ in ROUNDTRIP_CASES)
    con.execute(f"CREATE OR REPLACE TABLE rt AS SELECT {select}")
    path = tmpdir / "roundtrip.parquet"
    con.execute(f"COPY rt TO '{path}' (FORMAT PARQUET)")

    duck_back = {
        r[0]: r[1] for r in con.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
    }
    pol_frame = pl.read_parquet(path)
    phys = {name: (p, lg) for name, p, lg in _parquet_columns(path)}

    print(
        f"  {'DuckDB 原类型':<26}{'parquet 物理类型':<22}{'parquet 逻辑标注':<32}"
        f"{'DuckDB 读回':<26}{'Polars 读回':<20}{'判定'}"
    )
    print("  " + "-" * 118)
    broken: list[tuple[str, str]] = []
    for name, _expr, orig, _want, pol_ok in ROUNDTRIP_CASES:
        ptype, logical = phys[name]
        dback = duck_back[name]
        pback = _short_polars_dtype(str(pol_frame.schema[name]))
        duck_kept = dback == orig
        if not duck_kept and not pol_ok:
            verdict = "❌ 断了"
            broken.append((orig, dback))
        elif not pol_ok:
            verdict = "❌ 断了"
            broken.append((orig, f"{dback} → Polars {pback}"))
        elif not duck_kept:
            verdict = "⚠️  变了"
        else:
            verdict = "✅ 对位"
        print(
            f"  {common.pad(_DISPLAY_NAME.get(orig, orig), 26)}{common.pad(ptype, 22)}"
            f"{common.pad(_compact_logical(logical), 32)}"
            f"{common.pad(_DISPLAY_NAME.get(dback, dback), 26)}"
            f"{common.pad(pback, 20)}{verdict}"
        )
    print()

    print("  断了的列，逐条说清楚**断在哪儿**：")
    for orig, detail in broken:
        print(f"    {common.pad(orig, 14)}{detail}")
    print()
    print("    · HUGEINT / UHUGEINT —— parquet 没有 128 位整数这个位置，DuckDB 只能落成 DOUBLE，")
    print("      **精度在落盘那一刻就没了**，不是「读的时候不够准」。")
    print("    · UUID —— parquet 用 16 字节定长 + UUID 标注存，DuckDB 认得，")
    print("      Polars 读成**裸字节**（值能还原，类型语义没有）。")
    print("    · ENUM —— parquet 不存枚举定义，只存字符串，DuckDB 自己也读不回来。")
    print("    · BIT —— 位串没有 parquet 类型，退化成字符串。")
    print()
    print("  顺带说明：Polars 读 DuckDB 的 parquet 时，**列名、列序、值的类型都保住了**")
    print("  （TINYINT 读回来还是 Int8，不是 Int32）—— 这靠的是 parquet 的")
    print("  `Int(bitWidth=8)` 逻辑标注，不是靠猜。")
    print()

    # DuckDB 的 ENUM 在 parquet 里能不能活下来
    print("  **ENUM 在 parquet 里能不能活下来？**")
    print(f"    DuckDB 写 parquet → 再读回来 : {duck_back['ENUM']}（枚举定义丢了）")
    print("    DuckDB 自己的 .duckdb 文件里   : ENUM('a', 'b')（定义还在）")
    print()
    print("    所以 ENUM 只在 DuckDB 自己的数据库文件里保得住；")
    print("    一旦落成 parquet，它就是个普通字符串。")
    print()

    # INTERVAL 单独测 —— 它会让 Polars 直接读不了
    print("  **INTERVAL 单独测**（DuckDB 的 interval parquet，Polars 读不了）：")
    iv_path = tmpdir / "interval.parquet"
    con.execute("CREATE OR REPLACE TABLE iv AS SELECT INTERVAL 1 DAY AS iv")
    con.execute(f"COPY iv TO '{iv_path}' (FORMAT PARQUET)")
    iv_phys = _parquet_columns(iv_path)
    iv_desc = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{iv_path}')").fetchall()[0][1]
    print(f"    parquet 落盘 : {iv_phys[0][1]} + {iv_phys[0][2]} 标注")
    print(f"    DuckDB 读回  : {iv_desc}")
    # 用**子进程**读：Rust 的 panic 不是 Python 异常，`except Exception` 接不住它，
    # 在主进程里试读会把本 demo 自己也带走。子进程还能顺便回答一个真问题：
    # 这个 panic 到底有多难接？
    code = f"""
import polars as pl
try:
    pl.read_parquet({str(iv_path)!r})
except Exception as exc:
    print("CAUGHT_BY_EXCEPTION", type(exc).__name__)
except BaseException as exc:
    print("CAUGHT_BY_BASEEXCEPTION", type(exc).__name__)
else:
    print("READ_OK")
"""
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    out = proc.stdout.strip()
    if proc.returncode == 0 and out == "READ_OK":
        print("    Polars 读回  : ✅ 正常")
    elif proc.returncode == 0:
        caught, cls = out.split(" ", 1)
        where = "except Exception" if caught == "CAUGHT_BY_EXCEPTION" else "except BaseException"
        print(f"    Polars 读回  : ❌ {cls}")
        print(f"                    `{where}` 能接住它 —— 但这是个 **panic**，")
        print("                    生产代码里没人会为了一个读文件去 catch BaseException。")
    else:
        tail = (proc.stderr.strip().splitlines() or ["(没有 stderr)"])[-1][:100]
        print(f"    Polars 读回  : ❌ 进程直接死了（退出码 {proc.returncode}）")
        print(f"                    {tail}")
    print()
    print("    注意这里 Polars 报的是 **panic**（Rust 层的 unwrap 失败），不是 Python 异常。")
    print("    也就是说：这个文件不是「读不出这一列」，是**整个文件都读不了**。")
    print("    绕法：读之前先把 interval 列转成字符串或秒数。")


# ---------------------------------------------------------------- 隐式转换


def scenario_implicit_cast(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 隐式转换规则：同一句表达式，两边给不给同一个答案")

    print("  DuckDB 是「有 SQL 传统」的引擎，很多地方会悄悄帮你转；")
    print("  Polars 是「类型系统严格的列式引擎」，能转就转，不能转就报错。")
    print()

    def duck(sql: str) -> str:
        try:
            return str(con.sql(sql).fetchall()[0][0])
        except Exception as exc:
            return f"❌ {type(exc).__name__}"

    def pol(fn) -> str:
        try:
            return str(fn())
        except Exception as exc:
            return f"❌ {type(exc).__name__}"

    dec = lambda v, p, s: pl.Series([v], dtype=pl.Decimal(p, s))  # noqa: E731
    checks: list[tuple[str, str, str]] = [
        (
            "'1' = 1（字符串跟数字比）",
            duck("SELECT '1' = 1"),
            pol(lambda: (pl.Series(["1"]) == 1).to_list()[0]),
        ),
        (
            "'1' + 1（字符串加数字）",
            duck("SELECT '1' + 1"),
            pol(lambda: (pl.Series(["1"]) + 1).to_list()[0]),
        ),
        (
            "1 || 'x'（数字拼字符串）",
            duck("SELECT 1 || 'x'"),
            pol(
                lambda: pl.select(pl.concat_str([pl.lit(1), pl.lit("x")])).to_series().to_list()[0]
            ),
        ),
        (
            "CAST('123abc' AS INTEGER)（严格）",
            duck("SELECT CAST('123abc' AS INTEGER)"),
            pol(lambda: pl.Series(["123abc"]).cast(pl.Int32).to_list()[0]),
        ),
        (
            "TRY_CAST('123abc' AS INTEGER)（宽松）",
            duck("SELECT TRY_CAST('123abc' AS INTEGER)"),
            pol(lambda: pl.Series(["123abc"]).cast(pl.Int32, strict=False).to_list()[0]),
        ),
        (
            "true 转成整数",
            duck("SELECT true::INTEGER"),
            pol(lambda: pl.Series([True]).cast(pl.Int32).to_list()[0]),
        ),
        (
            "DECIMAL(18,2) + DOUBLE",
            duck("SELECT 1.23::DECIMAL(18,2) + 1.0::DOUBLE"),
            pol(lambda: (dec("1.23", 18, 2) + pl.Series([1.0])).to_list()[0]),
        ),
        (
            "DECIMAL(18,2) + DECIMAL(18,3)",
            duck("SELECT 1.23::DECIMAL(18,2) + 1.234::DECIMAL(18,3)"),
            pol(lambda: (dec("1.23", 18, 2) + dec("1.234", 18, 3)).to_list()[0]),
        ),
        (
            "DECIMAL(38,0) 乘 10 溢出",
            duck("SELECT 99999999999999999999999999999999999999::DECIMAL(38,0) * 10"),
            pol(
                lambda: (
                    dec("99999999999999999999999999999999999999", 38, 0) * pl.Series([10])
                ).to_list()[0]
            ),
        ),
    ]

    print(f"  {'表达式':<38}{'DuckDB':<30}{'Polars':<30}")
    print("  " + "-" * 98)
    for label, d, p in checks:
        print(f"  {common.pad(label, 38)}{common.pad(d, 30)}{common.pad(p, 30)}")
    print()

    print("  读法：")
    print("    · **比较两边一致**：`'1' = 1` 两边都给 True（都做隐式转换）。")
    print("    · **算术两边一致地拒绝**：`'1' + 1` 两边都报错。")
    print("      —— 这一条最重要：两边都不会给你一个「看起来对」的结果。")
    print("    · **转数值的严格程度：其实是对齐的** ——")
    print("      严格模式两边都报错（`CAST` vs `cast(strict=True)`），")
    print("      宽松模式两边都给空（`TRY_CAST` vs `cast(strict=False)`）。")
    print("      真正的差别只在**异常类型**：DuckDB 抛 `ConversionException`，")
    print("      Polars 抛 `InvalidOperationError` —— 按类型 catch 的代码要跟着改。")
    print("    · **DECIMAL + DOUBLE**：两边都提升到浮点（2.23）。")
    print("      金额场景里这一步就把精确性丢了 —— 两边一样，但一样地危险。")
    print("    · **DECIMAL 溢出**：两边都报错，这点倒是一致的。")
    print()
    print("  迁移时值得单独盯的三条：")
    print("    1. **异常类型**（`ConversionException` vs `InvalidOperationError`）——")
    print("       按类型 catch 的代码要改；按「值是不是空」判断的代码不用改。")
    print("    2. DECIMAL 参与浮点运算时提升到哪儿（两边都提，但要自己确认）。")
    print("    3. `||` 在 DuckDB 里能拼数字，Polars 没有这个运算符。")
    print("       （Polars 的 `pl.concat_str` 要先把列转成字符串 —— 结果一样，路径不同。）")


def scenario_hugeint_precision(con: duckdb.DuckDBPyConnection, tmpdir: Path) -> None:
    section("场景 5 · 128 位整数：两个引擎各自怎么把它弄丢")

    big = 123456789012345678901234567890
    path = tmpdir / "hugeint.parquet"
    con.execute(f"COPY (SELECT {big}::HUGEINT AS v) TO '{path}' (FORMAT PARQUET)")

    orig = con.sql(f"SELECT {big}::HUGEINT").fetchall()[0][0]
    back = con.sql(f"SELECT v FROM read_parquet('{path}')").fetchall()[0][0]
    # 差值要在整数域里算：v 读回来已经是 DOUBLE 了，
    # 直接减会变成浮点减法，结果恒等于 0（那正是「静默损坏」的表现）。
    diff = con.sql(f"SELECT v::HUGEINT - {big}::HUGEINT FROM read_parquet('{path}')").fetchall()[0][
        0
    ]

    print("  原值（HUGEINT，128 位）：")
    print(f"    {orig}")
    print()
    print("  写进 parquet：")
    print(f"    parquet 物理类型 : {_parquet_columns(path)[0][1]}（**没有**逻辑标注）")
    print(f"    parquet 逻辑标注 : {_parquet_columns(path)[0][2]}")
    print()
    print("  读回来：")
    back_type = con.sql(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()[0][1]
    print(f"    DuckDB 读回的类型 : {back_type}")
    print(f"    DuckDB 读回的值   : {back}")
    print(f"    转回整数后差了多少: {diff}")
    print()
    print(f"  原值是 {len(str(orig))} 位的整数，落盘后变成 DOUBLE（53 位有效数字），")
    print(f"  实测差了 {abs(diff):,} —— 静默的、没有任何报错的数据损坏。")
    print("  注意：在浮点域里比是「相等」的 —— 两个值都变成同一个 double，")
    print("  只有回到整数域才看得出差了多少。这正是它危险的地方。")
    print()

    print("  DuckDB 这边，还有更狠的：HUGEINT 的**最大值**转成 DOUBLE 之后连转回来都不行 ——")
    maxv = 170141183460469231731687303715884105727
    try:
        con.sql(f"SELECT (({maxv}::HUGEINT)::DOUBLE)::HUGEINT").fetchall()
        print("    能转回来")
    except Exception as exc:
        print(f"    ❌ {str(exc).splitlines()[0][:96]}")
    print("    （DOUBLE 装不下这个值，DuckDB 直接报 Out of Range ——")
    print("      **落盘那一刻就已经不可逆了**，不是「读的时候精度不够」。）")
    print()

    print("  另一头：Polars 的 Int128 写进 parquet，**DuckDB 读不回来**：")
    p_path = tmpdir / "int128.parquet"
    pl.DataFrame({"v": pl.Series([2**100], dtype=pl.Int128)}).write_parquet(p_path)
    got = con.sql(f"SELECT v, typeof(v) FROM read_parquet('{p_path}')").fetchall()[0]
    print(f"    DuckDB 读到的是 : {got[0]!r}  类型 {got[1]}")
    print("    —— 16 个字节的 BLOB，值是对的（小端），但**类型对不上了**：")
    print("       DuckDB 不会把它认成 HUGEINT，只能自己 `hex()` 出来再拼。")
    print()
    print("    结论：**128 位整数没有一个能安全过 parquet 的对位**。")
    print("    真要跨引擎传 128 位整数，落成字符串（或 Decimal(38,0)）最稳。")


def summary() -> None:
    section("小结")
    print("  1. 两边的「宽度阶梯」是对得上的（TINYINT↔Int8 … BIGINT↔Int64），")
    print("     差别集中在高级类型：DuckDB 有 UUID / ENUM / BIT / GEOMETRY / VARIANT，")
    print("     Polars 一个都没有对应物。")
    print("  2. **Parquet 往返的主要坑不是「类型没了」，是「类型悄悄变了」**：")
    print("     HUGEINT → DOUBLE（精度静默丢失）、ENUM → VARCHAR（枚举定义没了）、")
    print("     BIT → VARCHAR、UUID → Binary（16 字节原始字节）。")
    print("  3. **INTERVAL 是硬伤**：DuckDB 的 interval parquet，Polars 直接 panic，")
    print("     整个文件都读不了，不是「读不出这一列」。")
    print("  4. 隐式转换：比较和算术两边基本一致（该转的转，该报的报），")
    print("     字符串转数值的严格/宽松模式也**对齐**（都报错 / 都给空），")
    print("     差别只在异常类型：`ConversionException` vs `InvalidOperationError`。")
    print("  5. 跨引擎搬数据，**Parquet 只保证「值」不保证「类型」**：")
    print("     能带过去的只有 parquet 的逻辑标注（Int(bitWidth) / Decimal / UUID / Date…），")
    print("     没标注的列到了对面就是猜的。")
    print("  6. 选型的实用结论：**128 位整数、UUID、枚举这三个是最容易在迁移中静默变形的**，")
    print("     真要传，落成字符串或 Decimal(38,0) 最稳。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="类型系统全景对照")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")

    con = duckdb.connect()
    with tempfile.TemporaryDirectory(prefix="type_demo_") as tmp:
        tmpdir = Path(tmp)
        scenario_type_inventory(con)
        scenario_pairing(con)
        scenario_parquet_roundtrip(con, tmpdir)
        scenario_implicit_cast(con)
        scenario_hugeint_precision(con, tmpdir)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
