"""时间戳与日期的边界：两个引擎各自能表示到哪一年，越界时谁报错、报什么错。

`tz_demo.py` 讲的是**精度和时区** —— 值算出来对不对。
这个 demo 讲的是**边界** —— 值大到某个程度之后会发生什么。

边界问题的麻烦在于：**它平时不出事，出事的时候已经晚了。**

  · 一条 `TIMESTAMP` 存到 294247 年，SQL 跑得好好的，
    换到 Python 手上却变成了一列**字符串**（不报错，只是类型悄悄变了）。
  · 同一个越界的值，Polars 直接抛一个 **Rust PanicException** ——
    它继承的是 `BaseException`，**不是 `Exception`**，
    所以 `except Exception:` 那一层根本接不住它。
  · 纳秒在 DuckDB 里**真的存住了**（`epoch_ns` 能取回 1577836800123456789），
    但转成 Python 对象时按微秒截断 —— 存的是 9 位，看的是 6 位。

脚本把两个引擎的边界摆出来逐条对账：

  1. 各类型能表示到哪一年：上界精确到单位，下界说明出处。
  2. **「2262-04-11 溢出」说的是哪个类型** —— 不是 TIMESTAMP，是 TIMESTAMP_NS。
  3. 越界时的失败方式：DuckDB 抛 ConversionError，Polars 抛 PanicException。
  4. Python 端的**静默降级**：`datetime` 悄悄变成 `str`。
  5. Parquet 往返：极端值读得进来，但**一打印就炸**。
  6. 纳秒存住了，显示不出来。
  7. naive 的纪元换算：两边**完全一致**（这条是好消息）。
  8. 运算溢出：越界不只发生在写入，发生在**算**的时候。

用法：
    uv run python src/bounds_demo.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import duckdb
import polars as pl

import common

OUT_DIR = common.DATA_DIR / "bounds_demo"

# ---------------------------------------------------------------- 边界字面量
#
# 每一项是 (DuckDB 类型, 内部计数方式, 能存进去的最后一个值, 再走一步的值)。
# 下面每个都会**现场验一遍** —— 万一哪天 DuckDB 换了实现，这里会打印 ❌，
# 而不是继续拿一张过期的表糊弄人。
BOUNDS = [
    ("TIMESTAMP", "int64 微秒", "294247-01-10 04:00:54.775806", "294247-01-10 04:00:54.775807"),
    ("TIMESTAMP_MS", "int64 毫秒", "294247-01-10 04:00:54.775", "294247-01-10 04:00:54.776"),
    ("TIMESTAMP_S", "int64 秒", "294247-01-10 04:00:54", "294247-01-10 04:00:55"),
    (
        "TIMESTAMP_NS",
        "int64 纳秒",
        "2262-04-11 23:47:16.854775806",
        "2262-04-11 23:47:16.854775807",
    ),
    ("DATE", "int32 天", "5881580-07-10", "5881580-07-11"),
]

INT64_MAX = 2**63 - 1
INT32_MAX = 2**31 - 1


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def _q(text: str) -> str:
    """把一段文本变成 SQL 字符串字面量（单引号要转义成两个）。"""
    return "'" + text.replace("'", "''") + "'"


def _one(con: duckdb.DuckDBPyConnection, sql: str):
    """执行一条只返回一行的 SQL，取第一个值。

    走 `execute` 而不是 `con.sql(...)`：relation 会一直占着连接直到被 GC。
    """
    res = con.execute(sql)
    row = res.fetchone()
    res.fetchall()
    return row[0]


def _text(con: duckdb.DuckDBPyConnection, sql: str) -> str:
    """取一条 SQL 的第一列，转成字符串 —— 避开越界值取回 Python 对象那条路。"""
    return str(_one(con, sql))


def _try(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[str, str]:
    """跑一条 SQL，失败返回 (错误类型, 错误信息)，成功返回 ("", 值)。"""
    try:
        res = con.execute(sql)
        row = res.fetchone()
        res.fetchall()
        return "", str(row[0])
    except Exception as exc:
        return type(exc).__name__, str(exc).splitlines()[0][:130]


@contextlib.contextmanager
def _quiet_stderr():
    """把 fd 2 临时接到 /dev/null —— Polars 的 Rust panic hook 会往那儿吐 backtrace。"""
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)


# ---------------------------------------------------------------- Polars 子进程
#
# Polars 的时间越界会抛 **PanicException** —— 它继承 `BaseException`，
# 不是 `Exception`。在同一个进程里跑，那句 panic 会把整个 demo 的输出搅乱
# （Rust 的 backtrace 直接打到 stderr）。所以每个 case 单独开一个子进程，
# 干净地拿到「到底抛了什么、except Exception 抓不抓得住」。
#
# 注意下面这段是**字符串**（子进程里 exec 的源码），不是本文件的代码 ——
# 所以 ruff 不会去 lint 它，BaseException 那个 except 也不会被 B036 拦。

_POLARS_WORKER = r'''
import json
import sys

import polars as pl


def case_us_max():
    s = pl.Series([2**63 - 1], dtype=pl.Int64).cast(pl.Datetime("us"))
    return s.to_list()


def case_us_min():
    s = pl.Series([-(2**63)], dtype=pl.Int64).cast(pl.Datetime("us"))
    return s.to_list()


def case_ns_max():
    s = pl.Series([2**63 - 1], dtype=pl.Int64).cast(pl.Datetime("ns"))
    return s.to_list()


def case_date_max():
    s = pl.Series([2**31 - 1], dtype=pl.Int32).cast(pl.Date)
    return s.to_list()


def case_date_min():
    s = pl.Series([-(2**31)], dtype=pl.Int32).cast(pl.Date)
    return s.to_list()


def case_str_year_10000():
    return pl.Series(["10000-01-01"]).str.to_datetime("%Y-%m-%d").to_list()


def case_uncaught():
    """`except Exception` 抓不抓得住？—— 这就是整个 demo 的重点。"""
    caught = "no"
    try:
        try:
            case_us_max()
        except Exception:
            caught = "yes"
    except BaseException as exc:
        return f"穿过了 except Exception，最后被 {type(exc).__name__} 接住"
    return f"被 except Exception 接住了（caught={caught}）"


CASES = {
    "us_max": case_us_max,
    "us_min": case_us_min,
    "ns_max": case_ns_max,
    "date_max": case_date_max,
    "date_min": case_date_min,
    "str_year_10000": case_str_year_10000,
    "uncaught": case_uncaught,
}


def main():
    name = sys.argv[1]
    fn = CASES[name]
    out = {"case": name}
    try:
        out["kind"] = "ok"
        out["value"] = repr(fn())
    except Exception as exc:
        out["kind"] = "exception"
        out["exc"] = type(exc).__name__
        out["msg"] = str(exc).splitlines()[0][:160]
        out["is_exception"] = isinstance(exc, Exception)
        out["mro"] = [c.__name__ for c in type(exc).__mro__]
    except BaseException as exc:
        out["kind"] = "base_exception"
        out["exc"] = type(exc).__name__
        out["msg"] = str(exc).splitlines()[0][:160]
        out["is_exception"] = isinstance(exc, Exception)
        out["mro"] = [c.__name__ for c in type(exc).__mro__]
    print(json.dumps(out, ensure_ascii=False))


main()
'''


def _polars_case(case: str) -> dict:
    """在子进程里跑一个 Polars 越界 case，把结果拿回来。"""
    proc = subprocess.run(
        [sys.executable, "-c", _POLARS_WORKER, case],
        capture_output=True,
        text=True,
        timeout=120,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    return {
        "kind": "crashed",
        "exc": "—",
        "msg": (proc.stderr.strip().splitlines() or ["（无输出）"])[-1][:160],
        "is_exception": False,
        "mro": [],
    }


# ---------------------------------------------------------------- 场景 1：范围


def scenario_bounds(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 1 · 各类型能表示到哪一年（实测）")
    print("  DuckDB 的时间类型全是**整数计数**，不是日历 ——")
    print("  TIMESTAMP 数微秒、TIMESTAMP_MS 数毫秒、TIMESTAMP_S 数秒、TIMESTAMP_NS 数纳秒，")
    print("  DATE 数天。计数用 int64（DATE 用 int32），所以上界就是「int64 能数到哪」。")
    print()

    header = ("DuckDB 类型", "内部计数", "能存进去的最后一个值", "再走一步（必须报错）")
    widths = [16, 12, 38, 40]
    print(
        "  "
        + common.pad(header[0], widths[0])
        + common.pad(header[1], widths[1])
        + common.pad(header[2], widths[2])
        + header[3]
    )
    print("  " + "-" * 92)

    for typ, unit, last, one_more in BOUNDS:
        err_type, _err_val = _try(con, f"SELECT {_q(last)}::{typ}")
        mark = "✅" if err_type == "" else "❌"
        over_type, _over_msg = _try(con, f"SELECT {_q(one_more)}::{typ}")
        step = over_type if over_type else "❌ 没报错?!"
        print(
            "  "
            + common.pad(f"{typ} {mark}", widths[0])
            + common.pad(unit, widths[1])
            + common.pad(last, widths[2])
            + step
        )

    print()
    print("  「再走一步」的报错全文：")
    for typ, _unit, _last, one_more in BOUNDS:
        _err_type, err_msg = _try(con, f"SELECT {_q(one_more)}::{typ}")
        print(f"    {typ:12s} {err_msg}")

    print()
    print("  换算成 epoch 计数，这些上界就是「整型类型的最大值减一」")
    print("  （最大值那一格留给了 infinity / NaT）：")
    for label, sql, unit in [
        (
            "epoch_us(TIMESTAMP 上界)",
            f"SELECT epoch_us({_q('294247-01-10 04:00:54.775806')}::TIMESTAMP)",
            "微秒",
        ),
        (
            "epoch_ns(TIMESTAMP_NS 上界)",
            f"SELECT epoch_ns({_q('2262-04-11 23:47:16.854775806')}::TIMESTAMP_NS)",
            "纳秒",
        ),
        (
            "date_diff(TIMESTAMP DATE 上界)",
            "SELECT date_diff('day', DATE '1970-01-01', DATE '5881580-07-10')",
            "天",
        ),
    ]:
        print(f"    {label:34s} = {_one(con, sql):>20}  ({unit})")
    print(f"    int64 最大值 = {INT64_MAX}，int32 最大值 = {INT32_MAX}")

    print()
    print("  下界这边：TIMESTAMP_NS 的下界正好是 int64 纳秒的**负端**：")
    lo_ns = _one(con, f"SELECT epoch_ns({_q('1677-09-21 00:12:43.145224192')}::TIMESTAMP_NS)")
    print(f"    epoch_ns('1677-09-21 00:12:43.145224192'::TIMESTAMP_NS) = {lo_ns}")
    print("  TIMESTAMP / DATE 的下界在公元前几千年（DuckDB 内部按「天」计数，")
    print("  跟 Unix 纪年对不上）。**溢出的故事全在上界**，下界这里不展开。")


# ---------------------------------------------------------------- 场景 2：2262


def scenario_2262(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 2 · 「2262-04-11 溢出」说的是哪个类型")
    print("  「2262 年 4 月 11 日」是 **int64 纳秒**的上界 ——")
    print("  也就是说它属于 `TIMESTAMP_NS`，不属于 `TIMESTAMP`。")
    print("  DuckDB 的 `TIMESTAMP` 是**微秒**，能一路数到 294247 年。")
    print()

    print("  实测 DuckDB：")
    print(
        "    TIMESTAMP_NS 上界 → "
        + _text(con, f"SELECT ({_q('2262-04-11 23:47:16.854775806')}::TIMESTAMP_NS)::VARCHAR")
    )
    print(
        "    TIMESTAMP    上界 → "
        + _text(con, f"SELECT ({_q('294247-01-10 04:00:54.775806')}::TIMESTAMP)::VARCHAR")
    )
    print()
    print("    两者差了几十万年 —— 差的不是「年」，是**一个单位**：微秒 vs 纳秒。")
    print()

    print("  Polars 侧：`Datetime('ns')` 的上界是 **2262-04-11 23:47:16.854775**，")
    print("  而不是 2262-04-11 23:59:59.999999999 ——")
    print("  因为转成 Python 对象时 Python 的 datetime 只到**微秒**。")
    print()

    ns_max = _polars_case("ns_max")
    us_max = _polars_case("us_max")
    print(f"    Datetime('ns') 装 int64 最大值 → {ns_max.get('value')}")
    print(f"    Datetime('us') 装 int64 最大值 → {us_max.get('msg', us_max.get('value'))}")
    print()
    print("  → 所以「2262 溢出」这个说法，**只在 ns 这一档成立**：")
    print("    · DuckDB 的 TIMESTAMP_NS 上界 = 2262-04-11 23:47:16.854775806")
    print("    · Polars 的 Datetime('ns') 上界 = 2262-04-11 23:47:16.854775")
    print("    · 两边的 `TIMESTAMP` / `Datetime('us')` 都远够不着 2262")


# ---------------------------------------------------------------- 场景 3：失败方式


def scenario_failure_mode(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 3 · 越界时谁报错 —— 这条比边界本身更要命")
    print(
        "  DuckDB 越界 → `ConversionError`，是普通的 `Exception` 子类，`except Exception` 接得住。"
    )
    print("  Polars 越界 → **PanicException**，继承的是 `BaseException`**而不是 `Exception`** ——")
    print("  外面那层 `except Exception:` **接不住它**，会直接穿透到更外层。")
    print()

    print("  DuckDB 侧：")
    err_type, err_msg = _try(con, f"SELECT {_q('294247-01-10 04:00:54.775807')}::TIMESTAMP")
    caught = ""
    try:
        con.execute(f"SELECT {_q('294247-01-10 04:00:54.775807')}::TIMESTAMP").fetchall()
    except Exception as exc:
        caught = f"✅ 被 except Exception 接住了（{type(exc).__name__}）"
    print(f"    越界报错：{err_type}: {err_msg}")
    print(f"    {caught}")
    print()

    print("  Polars 侧（每个 case 单独开子进程跑）：")
    cases = [
        ("us_max", "Datetime('us') ← int64 最大值"),
        ("us_min", "Datetime('us') ← int64 最小值"),
        ("ns_max", "Datetime('ns') ← int64 最大值"),
        ("date_max", "Date ← int32 最大值"),
        ("date_min", "Date ← int32 最小值"),
        ("str_year_10000", "str.to_datetime('10000-01-01')"),
    ]
    for name, label in cases:
        r = _polars_case(name)
        if r["kind"] == "ok":
            detail = f"✅ 成了 → {r['value']}"
        else:
            mro = " < ".join(r.get("mro", []))
            detail = (
                f"❌ {r.get('exc')}: {r.get('msg')}\n"
                f"          继承链 {mro}   isinstance(e, Exception) = {r.get('is_exception')}"
            )
        print(f"    {label:38s}")
        print(f"          {detail}")
    print()

    uncaught = _polars_case("uncaught")
    print("  重点：Polars 这边 `except Exception` 到底接不接得住？")
    print(f"    {uncaught.get('value')}")
    print()
    print("  → **接不住**。PanicException 直接继承 `BaseException`，")
    print("    `except Exception:` 那一层形同虚设，异常会一路冒到最外层。")
    print("    写 `try/except Exception` 兜底的代码，在这里是**兜不住的**。")
    print()
    print("  顺带一提：Polars 的 `cast` 本身**是成功的** ——")
    print("  `pl.Series([2**63-1]).cast(pl.Datetime('us'))` 拿到的是一个合法 Series，")
    print("  炸在**转成 Python 对象**那一步（`to_list()` / `s[0]` / `print(df)`）。")
    print("  也就是说：数据一直躺在那儿没事，直到有人想把它看一眼。")


# ---------------------------------------------------------------- 场景 4：静默降级


def scenario_silent_downgrade(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 4 · DuckDB 在 Python 端的静默降级：datetime 悄悄变成 str")
    print("  DuckDB 的值域比 Python 的 `datetime` 大得多（294247 年 vs 9999 年）。")
    print("  值超出 Python 能表示的范围时，**DuckDB 的 Python 驱动不报错** ——")
    print("  它把这一列**降级成字符串**返回。")
    print()

    rows = [
        ("TIMESTAMP '1999-01-01 00:00:00'", "正常年份"),
        ("TIMESTAMP '294247-01-10 04:00:54.775806'", "上界"),
        ("DATE '1999-01-01'", "正常年份"),
        ("DATE '5881580-07-10'", "上界"),
        ("DATE '-4713-01-01'", "公元前"),
    ]
    for sql, label in rows:
        res = con.execute(f"SELECT {sql}")
        row = res.fetchone()
        res.fetchall()
        got = row[0]
        tname = type(got).__name__
        mark = "✅" if tname in ("datetime", "date") else "⚠️"
        print(f"  {mark} {sql:44s} {label:8s} → {tname:8s} {got!r}")
    print()
    print("  → 类型是**按列**变的：一列里只要有一个越界值，")
    print("    整列在 Python 侧就是 `str`，后面 `.dt.year` 之类全部 AttributeError。")
    print()

    print("  走 Arrow 通道就不一样了 —— Arrow 表本身是好的，")
    print("  但把它**转成 Python 对象**（`to_pylist()`）时直接抛 OverflowError，不静默：")
    for sql in [
        f"SELECT {_q('294247-01-10 04:00:54.775806')}::TIMESTAMP",
        f"SELECT {_q('5881580-07-10')}::DATE",
    ]:
        rel = con.sql(sql)
        try:
            tbl = rel.to_arrow_table()
            schema = str(tbl.schema)
            try:
                tbl.to_pylist()
                detail = "没报错"
            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc).splitlines()[0][:60]}"
            print(f"    {sql[7:48]:42s} Arrow schema = {schema}")
            print(f"    {'':42s} 转 Python → {detail}")
        finally:
            rel.close()
    print()
    print("  → **同一个值，两条通道两种行为**：DuckDB 自己的 Python 驱动降级成 str（静默），")
    print("    Arrow 转 Python 对象直接报错（吵但安全）。")
    print("    值在引擎里始终是对的 —— 出问题的永远是 Python 这一端。")


# ---------------------------------------------------------------- 场景 5：Parquet 往返


def scenario_parquet_roundtrip(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 5 · Parquet 往返：读得进来，一打印就炸")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "extreme.parquet"

    con.execute(
        f"COPY (SELECT {_q('294247-01-10 04:00:54.775806')}::TIMESTAMP AS t) "
        f"TO '{path}' (FORMAT PARQUET)"
    )
    read_back = _text(con, "SELECT t FROM " + _q(str(path)))
    print(f"  DuckDB 写了一个上界值 → {path.name}")
    print(f"    DuckDB 自己读回来：{read_back}")
    print()

    df = pl.read_parquet(path)
    print(f"  Polars 读 parquet：**成功了**，dtype 也对：{df.schema}")
    print()
    for label, fn in [
        ("print(df)", lambda: str(df)),
        ("df['t'].to_list()", lambda: df["t"].to_list()),
    ]:
        # Rust 的 panic hook 会往 stderr 吐一坨 backtrace，把 demo 的输出搅乱。
        # 这里把 fd 2 临时接到 /dev/null —— 异常本身下面照样打印。
        with _quiet_stderr():
            try:
                fn()
                detail = "没出错"
            except BaseException as exc:  # noqa: B036 —— 这里就是要接 PanicException
                detail = f"{type(exc).__name__}: {str(exc).splitlines()[0][:70]}"
        print(f"    {label:20s} → {detail}")
    print()
    print("  → 读是读进来了，**炸在「看一眼」上**。")
    print("    数据在 Polars 内部是好的（int64 微秒），")
    print("    但只要转成 Python 对象或者打印出来，就 panic。")
    print()
    print("  这就是「边界值」最麻烦的地方：")
    print("    写入端不报错 → 管道一路畅通 → 到了展示/导出那一环才炸。")
    print()

    print("  对照组 —— 正常值走一遍同样的流程：")
    ok_path = OUT_DIR / "normal.parquet"
    con.execute(
        f"COPY (SELECT TIMESTAMP '2020-01-01 00:00:00' AS t) TO '{ok_path}' (FORMAT PARQUET)"
    )
    df_ok = pl.read_parquet(ok_path)
    print(f"    {df_ok.to_dicts()}")


# ---------------------------------------------------------------- 场景 6：纳秒


def scenario_nanosecond(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 6 · 纳秒存住了，显示不出来")
    print("  「纳秒在引擎里存不存得住」和「你能不能看到纳秒」是两件事。")
    print()

    lit = "2020-01-01 00:00:00.123456789"
    ns_lit = _q(lit) + "::TIMESTAMP_NS"
    epoch_ns = _one(con, "SELECT epoch_ns(" + ns_lit + ")")
    epoch_us = _one(con, "SELECT epoch_us(" + ns_lit + ")")
    as_us = _text(con, "SELECT (" + ns_lit + ")::TIMESTAMP")
    as_str = _text(con, "SELECT (" + ns_lit + ")::VARCHAR")
    print(f"  DuckDB 存进去 {lit}：")
    print(f"    epoch_ns(TIMESTAMP_NS)  = {epoch_ns}")
    print(f"    epoch_us(TIMESTAMP_NS)  = {epoch_us}")
    print(f"    ::TIMESTAMP（转微秒）   = {as_us}")
    print(f"    ::VARCHAR（显示）        = {as_str}")
    print()
    print("  → epoch_ns 取回了 1577836800123456789 —— **纳秒真的存住了**。")
    print("    转成 TIMESTAMP（微秒）是**四舍五入**（.123456789 → .123457）。")
    print()

    print("  Polars 侧：")
    s_ns = pl.Series([1577836800123456789], dtype=pl.Int64).cast(pl.Datetime("ns"))
    print(f"    Datetime('ns') 装进去     = {s_ns.to_list()[0]}   ← 转 Python 时截断到微秒")
    print(f"    内部 int64 纳秒还在        = {s_ns.cast(pl.Int64).to_list()[0]}   ← 原样还在")
    as_us_series = s_ns.cast(pl.Datetime("us")).to_list()[0]
    print(f"    cast 到 Datetime('us')    = {as_us_series}   ← 这时才真的舍了")
    print()
    print("  → 两边都是「显示到微秒」，但原因不同：")
    print("    · DuckDB 的 ::TIMESTAMP 是**引擎里四舍五入**的，纳秒真的丢了；")
    print("    · Polars 的 Datetime('ns') 是**转 Python 时截断**的，")
    print("      内部 int64 纳秒还在 —— cast 回 Int64 取出来照样是 …789。")
    print()
    print("  所以「拿 ns 做 key」在两边都得自己算：")
    print("    DuckDB 端用 `epoch_ns(...)`；")
    print("    Polars 端 `cast(pl.Int64)`（ns 列）或 `cast(pl.Datetime('us'))`（统一到微秒）。")


# ---------------------------------------------------------------- 场景 7：naive 纪元


def scenario_naive_epoch(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 7 · naive 的纪元换算：两边完全一致")
    print("  「naive 时间戳按哪个时区算 epoch」是常见的坑。")
    print("  实测结论：**两边一样**，都按 UTC 算 —— 分歧不在这里。")
    print()

    naive = pl.DataFrame({"t": [datetime(2020, 1, 1)]})
    aware = pl.DataFrame({"t": [datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=8)))]})
    pol_naive = naive.with_columns(pl.col("t").dt.epoch("ms").alias("e"))["e"].to_list()[0]
    pol_aware = aware.with_columns(pl.col("t").dt.epoch("ms").alias("e"))["e"].to_list()[0]
    duck_naive = _one(con, "SELECT epoch_ms(TIMESTAMP '2020-01-01 00:00:00')")
    duck_aware = _one(con, "SELECT epoch_ms(TIMESTAMPTZ '2020-01-01 00:00:00+08')")

    print("  naive 的 2020-01-01 00:00:00（不带时区）：")
    print(f"    DuckDB  epoch_ms(TIMESTAMP)  = {duck_naive}")
    print(f"    Polars  dt.epoch('ms')       = {pol_naive}")
    print(f"    → {'✅ 一致' if duck_naive == pol_naive else '❌ 不一致'} —— 两边都按 UTC 算")
    print()
    print("  aware 的 2020-01-01 00:00:00+08:00：")
    print(f"    DuckDB  epoch_ms(TIMESTAMPTZ) = {duck_aware}")
    print(f"    Polars  dt.epoch('ms')       = {pol_aware}")
    print(f"    → {'✅ 一致' if duck_aware == pol_aware else '❌ 不一致'} —— 两边都按真实瞬间算")
    print()
    print("  → 这条是**好消息**：naive 当 UTC 处理，两边不打架。")
    print("    真正会分叉的是「naive 存进去、aware 读出来」这种混用")
    print("    （DuckDB 侧靠会话时区，Polars 侧靠 dtype 的 time_zone），")
    print("    那是 `tz_demo.py` 的内容，不在这里重复。")
    print()

    print("  顺带看一眼 epoch 往回转（naive 也当 UTC）：")
    back = _text(con, "SELECT epoch_ms(1577836800000)::TIMESTAMP")
    pol_back = pl.Series([1577836800000], dtype=pl.Int64).cast(pl.Datetime("ms")).to_list()[0]
    print(f"    DuckDB  epoch_ms(1577836800000)::TIMESTAMP = {back}")
    print(f"    Polars  Int64 → Datetime('ms')              = {pol_back}")
    print(f"    → {'✅ 一致' if str(back)[:10] == str(pol_back)[:10] else '❌ 不一致'}")


# ---------------------------------------------------------------- 场景 8：运算溢出


def scenario_arithmetic_overflow(con: duckdb.DuckDBPyConnection) -> None:
    section("场景 8 · 越界不只发生在「写入」，发生在「算」的时候")
    print("  表里存的都是合法值，一句算术就把结果推出了类型能表示的范围 ——")
    print("  这时候报不报错、报什么错，才是真正影响生产的那种。")
    print()

    cases = [
        (
            "TIMESTAMP 上界 + 1 微秒",
            "SELECT TIMESTAMP '294247-01-10 04:00:54.775806' + INTERVAL 1 MICROSECOND",
        ),
        (
            "DATE 上界 + 1 天",
            "SELECT DATE '5881580-07-10' + INTERVAL 1 DAY",
        ),
        (
            "DATE 上界 + 100 年",
            "SELECT DATE '5881580-07-10' + INTERVAL 100 YEAR",
        ),
        (
            "TIMESTAMP_NS 上界 + 1 纳秒",
            "SELECT '2262-04-11 23:47:16.854775806'::TIMESTAMP_NS + INTERVAL 1 NANOSECOND",
        ),
        (
            "9999-12-31 + 1 天（对照组）",
            "SELECT TIMESTAMP '9999-12-31 23:59:59' + INTERVAL 1 DAY",
        ),
    ]
    for label, sql in cases:
        err_type, err_msg = _try(con, sql)
        what = f"报错：{err_msg}" if err_type else f"正常：{err_msg}"
        print(f"  {label:34s} → {what}")
    print()
    print("  → 对照组那行值得注意：")
    print("    `9999-12-31 + 1 天` = **10000-01-01 23:59:59**，DuckDB 算得出来，")
    print("    但 **Python 自己**的 datetime 到 9999-12-31 就到头了")
    print("    （实测 `datetime(10000, 1, 1)` → ValueError: year 10000 is out of range）。")
    print("    引擎能算出来的值，Python 端接不住 —— 这就是边界问题的全貌。")
    print()

    print("  DuckDB 里 make_date 的越界行为又是另一回事（**不报错，静默给别的值**）：")
    for n in [2147483647, 2147483646]:
        _err_type, err_msg = _try(con, f"SELECT make_date({n})")
        print(f"    make_date({n}) → {err_msg}")


# ---------------------------------------------------------------- 汇总


def summary() -> None:
    section("小结")
    print("  1. **「2262-04-11 溢出」是 ns 的事，不是 TIMESTAMP 的事。**")
    print("     DuckDB 的 TIMESTAMP 是 int64 微秒 → 上界 294247-01-10 04:00:54.775806；")
    print("     TIMESTAMP_NS 才是 2262-04-11 23:47:16.854775806。")
    print("     Polars 的 Datetime('ns') 上界是 2262-04-11 23:47:16.854775（微秒分辨率）。")
    print("  2. **越界时的失败方式不一样，而且 Polars 那一边更危险**：")
    print("     DuckDB 抛 `ConversionError`（`Exception` 子类，接得住）；")
    print("     Polars 抛 `PanicException`，继承的是 **`BaseException`** ——")
    print("     `except Exception:` **接不住**。写 try/except 兜底的代码在这里兜不住。")
    print("  3. Polars 的 panic 发生在**转 Python 对象**那一步，不是 cast 那一步：")
    print("     `cast` 成功，Series 一直好着，直到有人 `print()` 它。")
    print("  4. **DuckDB 在 Python 端会静默降级**：超出 Python `datetime` 范围的值")
    print("     拿回来是 `str` 而不是 `datetime` —— 不报错，类型变了。")
    print("     走 Arrow 通道则直接 `OverflowError`。")
    print("  5. **Parquet 是这条链上最安静的一环**：DuckDB 写出的上界值，")
    print("     Polars 读得进来（dtype 还对），打印才炸。")
    print("  6. **纳秒在引擎里存得住**（`epoch_ns` 能取回 …789），")
    print("     但 DuckDB 转 TIMESTAMP 是四舍五入，Polars 转 Python 是截断 ——")
    print("     拿纳秒当 key，两边得自己算。")
    print("  7. **naive 的纪元换算两边一致**（都按 UTC）—— 这条不用操心。")
    print("  8. **运算也会溢出**：`上界 + 1 微秒` 报 `Date and time not in timestamp range`；")
    print("     而 `make_date()` 越界是**静默给个别的值**，不报错。")


# ---------------------------------------------------------------- 入口


def main() -> int:
    print(f"DuckDB {duckdb.__version__} · Polars {pl.__version__}")
    print(f"机器状态：{common.describe_machine()}")

    con = duckdb.connect()
    scenario_bounds(con)
    scenario_2262(con)
    scenario_failure_mode(con)
    scenario_silent_downgrade(con)
    scenario_parquet_roundtrip(con)
    scenario_nanosecond(con)
    scenario_naive_epoch(con)
    scenario_arithmetic_overflow(con)
    con.close()
    summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
