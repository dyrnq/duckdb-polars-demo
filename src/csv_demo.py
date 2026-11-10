"""CSV 读写与往返保真：CSV 是最容易「看着没问题、其实数据已经坏了」的文件格式。

这个脚本对照四件事：

  1. **类型推断**：同一份 CSV，两个引擎推出来的类型不一样。
     日期要不要自动解析？带前导零的编号还算不算字符串？
  2. **采样窗口**：类型推断只看一部分数据，那一部分之外的值会怎样？
     —— 一个引擎静默改数，另一个直接报错。
  3. **坏行**：列数不齐、分隔符猜错，两边各是什么表现。
  4. **往返保真**：写出去的字节完全一样，读回来却不一样。

产出文件都落在 data/csv_demo/ 下。

用法：
    uv run python src/csv_demo.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import polars as pl

import common

OUT_DIR = common.DATA_DIR / "csv_demo"


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _write_text(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _duck_schema(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    try:
        rel = con.sql(f"SELECT * FROM read_csv_auto('{path}')")
        table = rel.to_arrow_table()
        rel.close()
        return str(table.schema.field("v").type)
    except Exception as exc:
        return f"ERR {type(exc).__name__}"


def _pol_schema(path: Path, **kwargs) -> str:
    try:
        return str(pl.read_csv(path, **kwargs).schema["v"])
    except Exception as exc:
        return f"ERR {type(exc).__name__}"


# Arrow 和 Polars 的类型名写法不一样，但语义相同的要算「一致」——
# 否则 bool/Boolean、int64/Int64 会被误报成不一致。
_TYPE_KIND = {
    "bool": "bool",
    "boolean": "bool",
    "int64": "int",
    "int32": "int",
    "double": "float",
    "float64": "float",
    "string": "str",
    "str": "str",
    "date32[day]": "date",
    "date": "date",
    "timestamp[us]": "timestamp",
    "timestamp[us,": "timestamp",
    "datetime(time_unit='us',": "timestamp",
}


def _kind(schema_text: str) -> str:
    """把类型名归一化到「语义类别」，用于判断两边是不是一回事。"""
    head = schema_text.strip().split(" ")[0].split(",")[0]
    return _TYPE_KIND.get(head.lower(), head.lower())


# ---------------------------------------------------------------- 场景 1


def scenario_inference(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 类型推断：同一份 CSV，两边推出来的类型不一样")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cases = {
        "日期 2024-01-01": "v\n2024-01-01\n2024-02-01\n",
        "时间戳 10:20:30": "v\n2024-01-01 10:20:30\n2024-02-01 11:20:30\n",
        "ISO 带 Z": "v\n2024-01-01T10:20:30Z\n2024-02-01T11:20:30Z\n",
        "布尔 true/false": "v\ntrue\nfalse\n",
        "布尔 TRUE/FALSE": "v\nTRUE\nFALSE\n",
        "0/1 当布尔": "v\n1\n0\n",
        "前导零 007": "v\n007\n008\n",
        "整数混浮点": "v\n1\n2.5\n",
        "整列都是空": "v\n\n\n",
    }
    print(f"  {'列里的内容':<22}{'DuckDB 推断':<36}{'Polars 推断':<24}")
    print("  " + "-" * 82)
    diffs = []
    for label, body in cases.items():
        path = OUT_DIR / "case.csv"
        _write_text(path, body)
        d = _duck_schema(con, path)
        p = _pol_schema(path)
        flag = "" if _kind(d) == _kind(p) else "  ← 不一致"
        print(f"  {common.pad(label, 22)}{common.pad(d, 36)}{common.pad(p, 24)}{flag}")
        if flag:
            diffs.append(label)
    print()
    print("  两处真正要紧的差别：")
    print()
    print("  ① 日期和时间戳：**DuckDB 自动识别，Polars 默认不识别**。")
    path = OUT_DIR / "case.csv"
    _write_text(path, "v\n2024-01-01\n2024-02-01\n")
    print(f"     DuckDB                        → {_duck_schema(con, path)}")
    print(f"     Polars（默认）                → {_pol_schema(path)}")
    print(f"     Polars（try_parse_dates=True）→ {_pol_schema(path, try_parse_dates=True)}")
    print("     想让 Polars 也认日期，必须显式开 `try_parse_dates=True`。")
    print("     反过来，**DuckDB 想关掉自动识别**得手动指定列类型 ——")
    print("     它的默认行为偏向「尽量解析」。")
    print()
    print("  ② 前导零：DuckDB 保守地当成字符串，Polars 推成整数。")
    path = OUT_DIR / "case.csv"
    _write_text(path, "v\n007\n008\n")
    duck_val = con.sql(f"SELECT v FROM read_csv_auto('{path}')").to_arrow_table().to_pylist()
    pol_val = pl.read_csv(path)["v"].to_list()
    print("     CSV 里写的是  '007' / '008'")
    print(f"     DuckDB 读成   {duck_val}   ← 字符串，前导零保住了")
    print(f"     Polars 读成   {pol_val}   ← **整数，前导零没了**")
    print("     邮政编码、身份证、订单号、门店编码 —— 只要带前导零，")
    print("     用 Polars 的默认推断读进来就全毁了，而且**不会报错**。")
    print("     修法：Polars 侧显式 `schema_overrides={'v': pl.String}`，")
    print("     或者 `infer_schema=False` + 自己指定 schema。")
    print()
    print(f"  （本次实测有 {len(diffs)} 列两边推断结果不一致，上面两条是后果最严重的。）")


# ---------------------------------------------------------------- 场景 2


def scenario_sampling(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 采样窗口：类型推断只看了一部分数据")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("  构造一份 3 万行的 CSV：前 3 万行是整数，最后一行是 1.5。")
    print()

    for offset in (20_000, 30_000):
        rows = ["v"] + [str(i) for i in range(offset)] + ["1.5"]
        path = OUT_DIR / f"sample_{offset}.csv"
        _write_text(path, "\n".join(rows) + "\n")
        print(f"  ── 浮点出现在第 {offset + 1} 行 ──")
        print(f"     DuckDB  推断类型：{_duck_schema(con, path)}")
        try:
            rel = con.sql(f"SELECT * FROM read_csv_auto('{path}')")
            table = rel.to_arrow_table()
            rel.close()
            last = table.to_pylist()[-1]
            print(f"     DuckDB  实际读到的最后一行：{last}")
            if last["v"] != 1.5:
                print(f"     ⚠️  **静默改数**：1.5 被读成了 {last['v']}！")
        except Exception as exc:
            print(f"     DuckDB  读取报错：{type(exc).__name__}: {str(exc).splitlines()[0][:70]}")
        try:
            pol = pl.read_csv(path)
            print(f"     Polars  读成：{pol['v'].to_list()[-1]}")
        except Exception as exc:
            print(f"     Polars  读取报错：{type(exc).__name__}: {str(exc).splitlines()[0][:70]}")
        print()

    print("  两个引擎都**只采样一部分行**来做类型推断：")
    print("    DuckDB  read_csv_auto 默认 sample_size = 20480 行")
    print("    Polars  read_csv 默认 infer_schema_length = 10000 行")
    print()
    print("  区别在于采样窗口之外的值怎么处理：")
    print("    · 采样窗口**之内**出现浮点（上面第一个用例）：")
    print("      DuckDB 推断成 double，Polars 推断成 i64 后**直接报错**。")
    print("    · 采样窗口**之外**才出现浮点（上面第二个用例）：")
    print("      DuckDB 推断成 int64，然后把 1.5 **静默读成了 2** —— ")
    print("      不报错、不警告，数据就是错的。")
    print("      Polars 同样报错（它的窗口更小，第 10001 行就炸了）。")
    print()
    print("  这个差别很要命：**报错是好事，安静地改数才是坏事**。")
    print("  DuckDB 那个「1.5 → 2」在真实数据里就是金额被四舍五入掉了。")
    print()
    print("  修法：")
    print("    DuckDB  read_csv(path, sample_size=-1)          # 全量采样")
    path = OUT_DIR / "sample_30000.csv"
    rel = con.sql(f"SELECT * FROM read_csv('{path}', sample_size=-1, auto_detect=true)")
    table = rel.to_arrow_table()
    rel.close()
    print(
        f"           改成 sample_size=-1 后 → {table.schema.field('v').type}，"
        f"末行 = {table.to_pylist()[-1]['v']}"
    )
    print("    Polars  read_csv(path, infer_schema_length=None) # 全量采样")
    try:
        pol = pl.read_csv(path, infer_schema_length=None)
        print(
            f"           改成 infer_schema_length=None 后 → {pol.schema['v']}，"
            f"末行 = {pol['v'].to_list()[-1]}"
        )
    except Exception as exc:
        print(f"           Polars 仍报错：{type(exc).__name__}")
    print()
    print("  或者干脆别猜：两边都显式指定 schema。")


# ---------------------------------------------------------------- 场景 3


def scenario_bad_rows(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 坏行：列数不齐的时候两边各是什么表现")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ragged = OUT_DIR / "ragged.csv"
    _write_text(ragged, "a,b,c\n1,2\n3,4,5\n")

    print("  文件内容：")
    print("    a,b,c")
    print("    1,2          ← 只有 2 列")
    print("    3,4,5        ← 3 列")
    print()

    rel = con.sql(f"SELECT * FROM read_csv_auto('{ragged}')")
    table = rel.to_arrow_table()
    rel.close()
    sniff = con.sql(f"SELECT Delimiter, HasHeader, Columns FROM sniff_csv('{ragged}')").fetchall()
    print("  DuckDB：")
    print(f"    sniff_csv 猜到的分隔符：{sniff[0][0]!r}   HasHeader={sniff[0][1]}")
    print(f"    读出来的列名：{table.schema.names}")
    print(f"    读出来的内容：{table.to_pylist()}")
    print("    ⚠️ **它把分隔符猜错了**（猜成 '|'），于是把表头整行当成一个列名，")
    print("       数据全部错位，而且**一声不吭**。")
    print()
    pol = pl.read_csv(ragged)
    print("  Polars：")
    print(f"    列名：{pol.columns}")
    print(f"    内容：{pol.to_dicts()}")
    print("    缺列补 NULL —— 结果是对的。")
    print()
    print("  两边都有「跳过坏行」的开关，但默认值不同：")
    print("    DuckDB  read_csv(path, ignore_errors=true)")
    print("    Polars  read_csv(path, ignore_errors=True)")
    print("  都是在**你已经知道有坏行**的时候才用 —— 默认行为才是你要防的那个。")
    print()
    print("  建议：**固定 schema，别让引擎猜**。")
    print("    DuckDB  read_csv(path, columns={'a': 'INTEGER', ...}, auto_detect=false)")
    print("    Polars  read_csv(path, schema=...)")
    print("  这样猜错分隔符、猜错类型的问题就都不存在了。")


# ---------------------------------------------------------------- 场景 4


def scenario_write(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · 写出：同样的数据，两边写出来的字节一样吗？")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "txt": ["含,逗号", '含"引号', "含\n换行", None],
            "num": [0.1 + 0.2, 1e-20, 123456789.123456789, None],
        }
    )
    pol_path = OUT_DIR / "written_polars.csv"
    duck_path = OUT_DIR / "written_duckdb.csv"
    frame.write_csv(pol_path)
    con.register("frame", frame)
    con.execute(f"COPY frame TO '{duck_path}' (FORMAT CSV, HEADER)")

    pol_bytes = pol_path.read_bytes()
    duck_bytes = duck_path.read_bytes()
    print("  Polars 写出来的内容：")
    print(f"    {pol_bytes.decode('utf-8')!r}")
    print("  DuckDB 写出来的内容：")
    print(f"    {duck_bytes.decode('utf-8')!r}")
    print()
    print(f"  逐字节相同：{'✅ 是' if pol_bytes == duck_bytes else '❌ 否'}")
    print()
    print("  值得记下的几个共同点：")
    print('    · 含分隔符/引号/换行的字段用双引号包起来，内部的 " 转义成 ""；')
    print("    · 浮点用最短往返表示（0.1+0.2 写成 0.30000000000000004）——")
    print("      这一条保证了浮点往返**无损**；")
    print("    · NULL 写成空字段（不是字符串 'NULL'）。")
    print()
    print("  可以改的地方：")
    print("    DuckDB  COPY … TO … (FORMAT CSV, HEADER, DELIMITER ';', NULL 'NULL', FORCE_QUOTE *)")
    print("    Polars  write_csv(path, separator=';', null_value='NULL', quote_all=True)")
    print()
    print("  注意：**默认值相同不代表可以互换** —— 只要有一边改了分隔符或 NULL 表示，")
    print("  另一边的默认读取就会对不上。写和读要用同一套约定。")


# ---------------------------------------------------------------- 场景 5


def scenario_roundtrip(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · 往返保真：写出去的字节一样，读回来却不一样")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "code": ["007", "008", "10086"],
            "day": ["2024-01-01", "2024-02-01", "2024-03-01"],
            "amt": [10.5, None, 0.1 + 0.2],
        }
    )
    pol_path = OUT_DIR / "rt_polars.csv"
    duck_path = OUT_DIR / "rt_duckdb.csv"
    frame.write_csv(pol_path)
    con.register("frame", frame)
    con.execute(f"COPY frame TO '{duck_path}' (FORMAT CSV, HEADER)")
    print("  写出去的 CSV（两边逐字节相同）：")
    print(f"    {pol_path.read_bytes().decode('utf-8')!r}")
    print()
    print("  读回来：")
    print()
    rel = con.sql(f"SELECT * FROM read_csv_auto('{duck_path}')")
    table = rel.to_arrow_table()
    rel.close()
    print("    DuckDB：")
    for row in table.to_pylist():
        print(f"      code={row['code']!r}  day={row['day']!r}  amt={row['amt']!r}")
    print("    Polars（默认）：")
    for row in pl.read_csv(pol_path).to_dicts():
        print(f"      code={row['code']!r}  day={row['day']!r}  amt={row['amt']!r}")
    print("    Polars（try_parse_dates=True）：")
    for row in pl.read_csv(pol_path, try_parse_dates=True).to_dicts():
        print(f"      code={row['code']!r}  day={row['day']!r}  amt={row['amt']!r}")
    print()
    print("  逐项对比：")
    print("    · code：DuckDB '007'（字符串）vs Polars 7（整数）—— **前导零丢了**；")
    print("    · day ：DuckDB date 对象 vs Polars 字符串 —— **要显式开 try_parse_dates**；")
    print("    · amt ：两边都是 0.30000000000000004 —— **浮点往返无损**，NULL 也对得上。")
    print()
    print("  结论：CSV 的**字节层面**两边完全一致，但**读回来的语义**不一致。")
    print("  往返保真不能只看「文件写出来对不对」，还得看「读回来还是不是同一批数据」。")
    print()
    print("  保真的做法：")
    print("    1. 编号类列显式指定字符串类型（两边都要写死）；")
    print("    2. 日期类列显式指定日期类型，或者接受字符串、别再解析；")
    print("    3. 金额用 Decimal / 整数分，别用浮点 —— 这条跟 decimal_demo.py 呼应。")
    print()
    print("  实测保真度：")
    duck_rows = table.to_pylist()
    pol_rows = pl.read_csv(pol_path, try_parse_dates=True).to_dicts()
    code_ok = all(
        str(d["code"]) == str(p["code"]) for d, p in zip(duck_rows, pol_rows, strict=True)
    )
    amt_ok = all(
        (d["amt"] is None and p["amt"] is None)
        or (d["amt"] is not None and p["amt"] is not None and d["amt"] == p["amt"])
        for d, p in zip(duck_rows, pol_rows, strict=True)
    )
    print(
        f"    code 列（字符串语义）：{'✅ 一致' if code_ok else '❌ 不一致 —— Polars 丢了前导零'}"
    )
    print(f"    amt  列（浮点数值）  ：{'✅ 一致' if amt_ok else '❌ 不一致'}")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. **类型推断不一样**：DuckDB 自动认日期/时间戳，Polars 默认不认")
    print("     （要开 `try_parse_dates=True`）。")
    print("  2. **带前导零的编号**：DuckDB 当字符串保住，Polars 推成整数 ——")
    print("     邮编、订单号、身份证读进来就毁了，而且不报错。")
    print("  3. **采样窗口之外的值**：两个引擎都只采样一部分行做推断。")
    print("     DuckDB 在窗口外遇到类型不一致会**静默改数**（1.5 → 2），")
    print("     Polars 会**直接报错**。要全量推断：DuckDB `sample_size=-1`，")
    print("     Polars `infer_schema_length=None`。")
    print("  4. **坏行**：DuckDB 的自动嗅探在参差文件上会把分隔符猜错，")
    print("     整行当表头、静默全错；Polars 补 NULL，结果是对的。")
    print("     固定 schema 是最省心的解法。")
    print("  5. **写出**：两边的默认输出逐字节相同（引号转义、浮点最短往返表示、")
    print("     NULL 写成空字段）。但默认值相同不代表能互换 —— 改了一边就要改另一边。")
    print("  6. **往返保真**：字节一样，语义不一样。浮点和 NULL 都能无损往返，")
    print("     字符串编号和日期列要靠显式指定类型才能保住。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    parser = argparse.ArgumentParser(description="CSV 读写与往返保真")
    parser.parse_args()

    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")
    print(f"产出目录：{OUT_DIR}")

    con = duckdb.connect()
    scenario_inference(con)
    scenario_sampling(con)
    scenario_bad_rows(con)
    scenario_write(con)
    scenario_roundtrip(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
