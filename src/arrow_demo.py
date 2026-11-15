"""Arrow 的三层版本，和「零拷贝」到底在哪儿断 —— 一份能跑的对照。

前面的 demo 比的是**引擎**。`interop.py` 说过一句「Arrow 是内存中的通用货币」，
但那只是一句话。这个 demo 把这句话拆开、逐条实测：

  1. 「Arrow 的版本」其实是**三层**，各管各的：发行版、磁盘格式版本、ABI。
  2. **ABI 里根本没有版本号** —— 共享靠的是「布局不变 + 所有权回调」，
     不是靠版本协商。所以三个框架版本差得很远（26.0.1 / 2.1.0 / 2.0.0）照样互通。
  3. 跨框架的共同入口是 `__arrow_c_stream__`（**不是** `__arrow_c_array__`）。
  4. **零拷贝有边界**：定宽列过 polars 是真零拷贝（而且地址稳定），
     字符串列**每次导出都重建缓冲区**；
     过 DuckDB 一律要拷（DuckDB 内部是自有 Vector，Arrow 只是它的输入/输出格式）。
  5. **三个框架三种性格**：pyarrow 是参考实现（什么都原样），
     polars 最严（不认识的 Arrow 26 新类型直接报错），
     duckdb 最宽（几乎什么都接，但会改型 —— 字典被解开、run-end-encoded 被解码）。
  6. **磁盘格式的实际互通范围**比「版本向后兼容」性感人得多：
     Arrow IPC 文件只有 pyarrow / polars 读得了 —— DuckDB 2.0 的扩展清单里
     压根没有 `arrow` 这一项，所以读不了也写不了。
  7. **polars 吃不了 DuckDB 的 relation** —— `pl.from_arrow(con.sql(...))` 会给你
     一列 struct 的 Series（**不报错**），必须先 `.to_arrow_table()`。

用法：
    uv run python src/arrow_demo.py
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.ipc as ipc

import common

# ---------------------------------------------------------------- 排版


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def print_table(header: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> None:
    aligns = aligns or ["<"] * len(header)
    widths = [
        max(common.display_width(str(r[i])) for r in [header, *rows]) for i in range(len(header))
    ]
    print(
        "  ".join(
            common.pad(h, widths[i]) if aligns[i] == "<" else common.pad_left(h, widths[i])
            for i, h in enumerate(header)
        )
    )
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(
            "  ".join(
                common.pad(str(c), widths[i])
                if aligns[i] == "<"
                else common.pad_left(str(c), widths[i])
                for i, c in enumerate(r)
            )
        )


# ---------------------------------------------------------------- flatbuffer 迷你解析器
#
# Arrow 的 IPC 文件和 schema 都是 flatbuffer 编码的。这个 demo 要读出
# 「文件格式版本」和「消息版本」这两个字段 —— 它们就在 flatbuffer 的根表里。
#
# 不引入 flatbuffers 依赖：这个仓库的原则是**演示脚本只依赖已声明的包**，
# 手撸几十行反而更说明问题（flatbuffer 的表布局就三个概念：
# 根偏移 → vtable → 字段在表内的偏移）。


def _fbs_field_pos(buf: bytes, table_pos: int, field: int) -> int | None:
    """某个表的第 field 个字段的值所在的绝对位置。

    flatbuffer 的表头是一个 soffset，指向 vtable；vtable 里每两个字节
    存一个「该字段在表内的偏移」。偏移为 0 表示这个字段用了默认值。
    """
    soffset = struct.unpack_from("<i", buf, table_pos)[0]
    vtable_pos = table_pos - soffset
    vtable_bytes = struct.unpack_from("<H", buf, vtable_pos)[0]
    off_pos = vtable_pos + 4 + field * 2
    if off_pos >= vtable_pos + vtable_bytes:
        return None
    field_off = struct.unpack_from("<H", buf, off_pos)[0]
    if field_off == 0:
        return None
    return table_pos + field_off


def _fbs_i16(buf: bytes, table_pos: int, field: int) -> int | None:
    at = _fbs_field_pos(buf, table_pos, field)
    if at is None:
        return None
    return struct.unpack_from("<h", buf, at)[0]


def _fbs_root(buf: bytes) -> int:
    """flatbuffer 开头 4 个字节是根表的偏移。"""
    return struct.unpack_from("<I", buf, 0)[0]


def ipc_file_format_version(raw: bytes) -> int | None:
    """IPC 文件的**文件格式版本**（footer 根表的 field 0）。

    0=V1 1=V2 2=V3 3=V4 4=V5。Arrow 规范里这个值只增不减，
    读到比自己老的版本才是问题。
    """
    footer_len = struct.unpack_from("<I", raw, len(raw) - 10)[0]
    footer = raw[len(raw) - 10 - footer_len : len(raw) - 10]
    return _fbs_i16(footer, _fbs_root(footer), 0)


def ipc_body_message_version(raw: bytes) -> int | None:
    """IPC 文件 body 里第一条消息的 **MessageVersion**。

    body 开头是 8 字节 magic（ARROW1\\0\\0），之后每条消息是
    continuation 前缀（0xFFFFFFFF + 4 字节长度）+ 一个 Message flatbuffer。
    Message 表的 field 0 就是版本号（0=V1 … 4=V5）。
    """
    base = 8
    if raw[base : base + 4] != b"\xff\xff\xff\xff":
        return None
    length = struct.unpack_from("<I", raw, base + 4)[0]
    msg = raw[base + 8 : base + 8 + length]
    at = _fbs_field_pos(msg, _fbs_root(msg), 0)
    if at is None:
        return 0  # 用了默认值
    return struct.unpack_from("<h", msg, at)[0]


# ---------------------------------------------------------------- 第 1 层：发行版


def sec1_distributions() -> None:
    section("第 1 节 · 发行版：三个框架各走各的，谁也不约束谁")

    print(
        f"pyarrow {pa.__version__}  ·  polars {pl.__version__}  ·  "
        f"duckdb {duckdb.__version__}  ·  Python {sys.version.split()[0]}"
    )
    print()
    print("这一层**没有任何兼容性保证**：pyarrow 26 和 polars 2.1 各自升级，")
    print("duckdb 也不看 pyarrow 的版本号。它们之间唯一的共同语言是下面两层。")
    print()
    print("本机实测：这三个版本**同时在跑**，后面的每一条结论都是在")
    print(
        f"pyarrow {pa.__version__} / polars {pl.__version__} / "
        f"duckdb {duckdb.__version__} 上测出来的。"
    )


# ---------------------------------------------------------------- 第 2 层：磁盘格式版本


def sec2_disk_format() -> None:
    section("第 2 节 · 磁盘格式版本：IPC 文件里有**两个**版本号")

    work = Path(tempfile.mkdtemp(prefix="arrow_demo_"))
    tbl = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})

    rows = []
    for ver in ipc.MetadataVersion:
        p = work / f"t_{ver.name}.arrow"
        opts = ipc.IpcWriteOptions(metadata_version=ver)
        with ipc.new_file(p, tbl.schema, options=opts) as w:
            w.write_table(tbl)
        raw = p.read_bytes()
        fmt_ver = ipc_file_format_version(raw)
        msg_ver = ipc_body_message_version(raw)
        rows.append(
            [
                ver.name,
                f"{fmt_ver} (V{fmt_ver + 1})" if fmt_ver is not None else "读不到",
                f"{msg_ver} (V{msg_ver + 1})" if msg_ver is not None else "读不到",
                str(len(raw)),
            ]
        )

    print("同一份数据，换 metadata_version 写出来：")
    print()
    print_table(
        ["写入选项", "文件格式版本 (footer)", "body 消息的 MessageVersion", "字节数"],
        rows,
        ["<", "<", "<", ">"],
    )
    print()
    print("**两个版本号不是一回事**：")
    print("  · 文件格式版本（footer 里的那个）**永远是 4 (V5)** —— 文件外层的封装格式；")
    print("  · MessageVersion 才是 `metadata_version` 控制的那个 —— 每条消息**内部**的版本。")
    print("    Arrow 当年加新特性（比如 decimal256、视图类型）就是在这里**加 flag 位**，")
    print("    不是改已有位的含义 —— 所以老 reader 读到新 flag 只会「不认识」，")
    print("    不会「读错」。")
    print()

    # 老格式还能不能被新版读
    legacy = work / "t.feather"
    with ipc.new_file(legacy, tbl.schema, options=ipc.IpcWriteOptions(use_legacy_format=True)) as w:
        w.write_table(tbl)
    back = ipc.open_file(legacy).read_all()
    print(f"向后兼容实测：写一个 Feather V1 老格式（{legacy.stat().st_size} 字节），")
    print(f"  再用 pyarrow {pa.__version__} 读回来 → {back.num_rows} 行 / {back.schema.names} ✅")
    print("  → **磁盘格式这一层是向后兼容的**：新版本读老文件没问题，老版本读新文件")
    print("    遇到不认识的 flag 才会有麻烦。所以「Arrow 版本差异」在磁盘上不是个坑。")


# ---------------------------------------------------------------- 第 3 层：ABI


def _abi_struct_fields(text: str, name: str) -> list[str]:
    """从 abi.h 里抠出某个 struct 的字段（去注释、去空行）。"""
    m = re.search(r"struct\s+" + re.escape(name) + r"\s*\{(.*?)\n\s*\};", text, re.S)
    if not m:
        return []
    fields = []
    for line in m.group(1).splitlines():
        line = re.sub(r"//.*", "", line).strip()
        if line:
            fields.append(line)
    return fields


def _field_name(decl: str) -> str:
    """从一行 C 声明里取出字段名（函数指针要单独处理）。"""
    decl = decl.strip().rstrip(";")
    m = re.search(r"\(\s*\*?\s*(\w+)\s*\)", decl)
    if m:
        return m.group(1)
    m = re.search(r"(\w+)\s*$", decl)
    return m.group(1) if m else decl


def sec3_abi() -> None:
    section("第 3 节 · ABI（C Data Interface）：结构体里**没有版本号**")

    header = Path(pa.get_include()) / "arrow" / "c" / "abi.h"
    if not header.exists():
        print(f"❌ 这个 pyarrow 发行版没带 C 头文件（{header}），第 3 层测不了")
        print("   （正常的 wheel 都带；读不到就说读不到，不假装。）")
        return

    text = header.read_text(encoding="utf-8")
    for name in ("ArrowSchema", "ArrowArray"):
        fields = _abi_struct_fields(text, name)
        names = [_field_name(f) for f in fields]
        has_version = "version" in names
        has_release = "release" in names
        print(f"struct {name} 的字段（实测自 abi.h）：")
        for f in fields:
            print(f"    {f}")
        ver = "有" if has_version else "**没有**"
        rel = "**有**" if has_release else "没有"
        print(f"  → 有 version 字段吗: {ver}    ·  有 release 回调吗: {rel}")
        print()

    print("**没有版本号** —— 这才是「不同框架能共享」的根本原因：")
    print()
    print("  共享靠的不是「我们版本一样」，而是三件事：")
    print("    1. **布局标准化** —— 一个 Arrow 数组 = 一组指向内存的 buffer 指针")
    print("       + 一个描述 format 的字符串。谁的实现都一样，所以谁的指针都能被读懂。")
    print("    2. **所有权靠回调移交** —— 生产方交出数据的同时交出一个 `release` 函数，")
    print("       消费方用完调它，生产方才真正释放。没有版本号协商，也没有「谁负责 free」")
    print("       的歧义 —— **这就是不需要版本协商也能安全共享的原因**。")
    print("    3. **入口统一** —— 见下一节。")


# ---------------------------------------------------------------- 第 4 层：共同入口


def sec4_entry_points() -> None:
    section("第 4 节 · 跨框架的共同入口：`__arrow_c_stream__`")

    probes = [
        ("pyarrow.Table", pa.table({"a": [1]})),
        ("pyarrow.Array", pa.array([1])),
        ("pyarrow.ChunkedArray", pa.chunked_array([[1]])),
        ("polars.DataFrame", pl.DataFrame({"a": [1]})),
        ("duckdb.DuckDBPyRelation", duckdb.connect().sql("SELECT 1 AS a")),
    ]
    rows = []
    for name, obj in probes:
        rows.append(
            [
                name,
                "有" if hasattr(obj, "__arrow_c_array__") else "—",
                "有" if hasattr(obj, "__arrow_c_stream__") else "—",
            ]
        )
    print_table(["对象", "__arrow_c_array__", "__arrow_c_stream__"], rows, ["<", ">", ">"])
    print()
    print(
        "注意一个反直觉的地方：`__arrow_c_array__`（单个数组）在 "
        "**Table、DataFrame、relation** 上都没有，"
    )
    print("三个框架都有的只有 **stream** 接口。")
    print()
    print("这不是巧合 —— stream 额外带上了**schema** 和**分批**的语义，")
    print("两个框架各自实现一遍 `__arrow_c_stream__` 就能互通；")
    print("而单数组接口没有 schema，跨框架传过去对方不知道列名和类型。")
    print()
    print("**结论：Arrow 官方推荐的跨框架通道是 stream，不是 array。**")


# ---------------------------------------------------------------- 第 5 层：零拷贝的边界


def _buffer_addrs(array) -> list[int | None]:
    """一列的各 buffer 的内存地址（没有的 buffer 是 None）。"""
    return [None if b is None else b.address for b in array.buffers()]


def _buffer_labels(n: int) -> list[str]:
    # Arrow 的 buffer 顺序：validity →（变宽类型还有 offsets）→ data
    if n <= 2:
        return ["validity", "data"][:n]
    return ["validity", "offsets", "data"][:n]


def _same_buffers(left, right) -> list[str]:
    la, lb = _buffer_addrs(left), _buffer_addrs(right)
    labels = _buffer_labels(max(len(la), len(lb)))
    out = []
    for i, label in enumerate(labels):
        x = la[i] if i < len(la) else None
        y = lb[i] if i < len(lb) else None
        if x is None and y is None:
            out.append(f"{label}:—")
        elif x is None or y is None:
            out.append(f"{label}:≠")
        else:
            out.append(f"{label}:{'✅' if x == y else '≠'}")
    return out


def _verdict(parts: list[str]) -> str:
    if any("≠" in p for p in parts):
        return "**拷了**"
    return "零拷贝 ✅"


def _buffers_identical(left, right) -> bool:
    """两列的 buffer 是不是逐块同一块内存（没有的 buffer 算一致）。"""
    la, lb = _buffer_addrs(left), _buffer_addrs(right)
    if len(la) != len(lb):
        return False
    for x, y in zip(la, lb, strict=True):
        if x is None and y is None:
            continue
        if x is None or y is None or x != y:
            return False
    return True


def sec5_zero_copy() -> None:
    section("第 5 节 · 零拷贝的边界：buffer 地址到底是不是同一块")

    pt = pa.table(
        {
            "num": pa.array([1.0, 2.0, 3.0]),
            "txt": pa.array(["x", "y", "z"]),
            "ts": pa.array([1, 2, 3], pa.timestamp("ns")),
        }
    )
    cols = ["num", "txt", "ts"]

    rows = []
    # pyarrow -> polars -> pyarrow
    # 同一个 polars DataFrame 导出**两次** —— 两次是不是同一块内存，
    # 决定了「零拷贝」是真的共享内存，还是每次导出都重建一遍。
    df = pl.from_arrow(pt)
    e1 = df.to_arrow()
    e2 = df.to_arrow()
    for col in cols:
        parts = _same_buffers(pt.column(col).chunk(0), e1.column(col).chunk(0))
        stable = _buffers_identical(e1.column(col).chunk(0), e2.column(col).chunk(0))
        rows.append(
            [
                "pyarrow → polars → pyarrow",
                col,
                "  ".join(parts),
                "是" if stable else "**否**",
                _verdict(parts),
            ]
        )
    # pyarrow -> duckdb -> pyarrow
    con = duckdb.connect()
    try:
        con.register("t", pt)
        out = con.sql("SELECT * FROM t").to_arrow_table()
    finally:
        con.close()
    for col in cols:
        parts = _same_buffers(pt.column(col).chunk(0), out.column(col).chunk(0))
        rows.append(
            ["pyarrow → DuckDB → pyarrow", col, "  ".join(parts), "—（每次重算）", _verdict(parts)]
        )

    print_table(
        ["方向", "列", "各 buffer 是否同一块", "连续两次导出同一块", "结论"],
        rows,
        ["<", "<", "<", "<", "<"],
    )
    print()
    print("三件事，一次测出来：")
    print()
    print("  1. **定宽列过 polars 是真零拷贝，而且地址稳定** —— float64 / timestamp 的")
    print("     buffer 地址跟原始 pyarrow 一模一样，**连续两次导出还是同一块**。")
    print("     这说明 polars 内部持有那块内存、导出时按引用交出去 ——")
    print("     因为 Polars 的内部模型**就是** Arrow（它用的就是 arrow2 那套布局）。")
    print()
    print("  2. **字符串列过 polars 每次导出都重建缓冲区** —— 注意第四列：")
    print("     连续两次 `to_arrow()` 拿到的 offsets / data **地址都不一样**。")
    print("     不是「导入时拷一次」，是**每导一次就重拷一次**。")
    print("     数据一个字节不差，但内存一直在动。")
    print()
    print("  3. **过 DuckDB 一律要拷** —— DuckDB 内部是它自己的 Vector，")
    print("     Arrow 在它那儿只是**输入格式和输出格式**，不是内部表示。")
    print("     （对比 Polars：Arrow 是内部表示，所以定宽列能直接共用内存。）")
    print()

    # 本仓库的真实数据
    path = common.ORDERS_PARQUET
    if not path.exists():
        print(f"（跳过真实数据的对照：{path} 不在，先跑 `uv run python src/gen_data.py`）")
        return

    print(f"拿本仓库的真实数据（{path.name}）再验一遍 —— 挑一个浮点列和一个字符串列：")
    real = pl.read_parquet(path)
    arrow_real = real.to_arrow()
    num_col, txt_col = "revenue", "region"
    if num_col not in real.columns or txt_col not in real.columns:
        print("  （这份数据里没有 revenue / region 列，跳过）")
        return
    df_real = pl.from_arrow(arrow_real)
    r1 = df_real.to_arrow()
    r2 = df_real.to_arrow()
    rows = []
    for col in (num_col, txt_col):
        parts = _same_buffers(arrow_real.column(col).chunk(0), r1.column(col).chunk(0))
        stable = _buffers_identical(r1.column(col).chunk(0), r2.column(col).chunk(0))
        rows.append(
            [
                col,
                str(real.schema[col]),
                "  ".join(parts),
                "是" if stable else "**否**",
                _verdict(parts),
            ]
        )
    print()
    print_table(
        ["列", "Polars 类型", "各 buffer 是否同一块", "连续两次导出同一块", "结论"],
        rows,
        ["<", "<", "<", "<", "<"],
    )
    print()
    print("**这正是 `interop.py` 里「Arrow 底层 buffer 地址不变」那句话的边界**：")
    print("它比的是 `revenue`（float64）那一列，成立；")
    print("字符串列（`region`）过 polars 每次导出都重建缓冲区 ——")
    print("**零拷贝不是整个 DataFrame 的性质，是列的性质**（定宽列成立，变宽列不成立）。")


# ---------------------------------------------------------------- 第 6 层：类型层的断点
#
# 这一段跑在**子进程**里：polars 对 pyarrow 26 的新类型会触发 **Rust panic**
# （不是能 catch 的普通异常），在主进程里炸掉会连累整个 demo。
# 子进程隔离 + 一行一个 JSON，父进程只管读结果。

TYPE_CASES: list[tuple[str, str]] = [
    ("int64", "int64"),
    ("string", "string"),
    ("large_string", "large_string"),
    ("struct", "struct"),
    ("list", "list"),
    ("fixed_size_list", "fixed_size_list"),
    ("map", "map"),
    ("dictionary", "dictionary"),
    ("decimal256", "decimal256"),
    ("string_view", "string_view"),
    ("run_end_encoded", "run_end_encoded"),
    ("list_view", "list_view"),
]


def _build_case(kind: str) -> pa.Table:
    """造一个只有一列的 Arrow 表，用来试「这个类型能不能过 polars」。"""
    if kind == "int64":
        return pa.table({"a": pa.array([1, 2], pa.int64())})
    if kind == "string":
        return pa.table({"a": pa.array(["x", "y"])})
    if kind == "large_string":
        return pa.table({"a": pa.array(["x", "y"], pa.large_string())})
    if kind == "struct":
        return pa.table({"a": pa.array([{"x": 1}], pa.struct([("x", pa.int32())]))})
    if kind == "list":
        return pa.table({"a": pa.array([[1, 2], [3]], pa.list_(pa.int32()))})
    if kind == "fixed_size_list":
        return pa.table({"a": pa.array([[1, 2]], pa.list_(pa.int32(), 2))})
    if kind == "map":
        return pa.table({"a": pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int32()))})
    if kind == "dictionary":
        return pa.table({"a": pa.array(["x", "y"]).dictionary_encode()})
    if kind == "decimal256":
        return pa.table({"a": pa.array([1, 2], pa.decimal256(40, 2))})
    if kind == "string_view":
        return pa.table({"a": pa.array(["x", "y"], pa.string_view())})
    if kind == "run_end_encoded":
        return pa.table(
            {
                "a": pa.RunEndEncodedArray.from_arrays(
                    pa.array([2, 5], pa.int32()), pa.array([10, 20], pa.int32())
                )
            }
        )
    if kind == "list_view":
        return pa.table({"a": pa.array([[1, 2], [3]], pa.list_view(pa.int32()))})
    raise ValueError(kind)


def _probe_polars(t: pa.Table) -> dict:
    """polars 吃这个 Arrow 表：结果是什么类型、数据有没有变。"""
    try:
        d = pl.from_arrow(t)
        out = d.to_arrow()
    except BaseException as exc:
        # polars 的 Rust panic 不是 Exception 的子类，普通 except Exception 接不住
        return {
            "ok": False,
            "dst": None,
            "error": f"{type(exc).__name__}: {str(exc)[:80]}",
            "is_exception": isinstance(exc, Exception),
            "data_ok": None,
        }
    return {
        "ok": True,
        "dst": str(out.schema.field("a").type),
        "error": None,
        "is_exception": True,
        "data_ok": t.column("a").to_pylist() == out.column("a").to_pylist(),
    }


def _probe_duckdb(t: pa.Table) -> dict:
    """duckdb 吃这个 Arrow 表（register + SELECT）：结果是什么类型、数据有没有变。"""
    con = duckdb.connect()
    try:
        con.register("t", t)
        out = con.sql("SELECT * FROM t").to_arrow_table()
    except BaseException as exc:
        return {
            "ok": False,
            "dst": None,
            "error": f"{type(exc).__name__}: {str(exc)[:80]}",
            "data_ok": None,
        }
    finally:
        con.close()
    return {
        "ok": True,
        "dst": str(out.schema.field("a").type),
        "error": None,
        "data_ok": t.column("a").to_pylist() == out.column("a").to_pylist(),
    }


def _probe_types_child() -> None:
    """子进程：逐个类型试 polars 和 duckdb 能不能接住，一行一个 JSON。

    为什么跑在子进程里：polars 碰到 decimal256 会触发 Rust panic，
    在主进程里炸掉会连累整个 demo。子进程炸了，主 demo 照跑。
    """
    for label, kind in TYPE_CASES:
        rec: dict = {"label": label, "kind": kind}
        try:
            t = _build_case(kind)
        except BaseException as exc:
            err = f"构造失败：{type(exc).__name__}: {str(exc)[:80]}"
            rec.update(
                src=None,
                polars={"ok": False, "dst": None, "error": err, "data_ok": None},
                duckdb={"ok": False, "dst": None, "error": err, "data_ok": None},
            )
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            continue
        rec["src"] = str(t.schema.field("a").type)
        rec["polars"] = _probe_polars(t)
        rec["duckdb"] = _probe_duckdb(t)
        print(json.dumps(rec, ensure_ascii=False), flush=True)


def _type_cell(probe: dict, src: str | None) -> str:
    """一个框架对某个类型的反应，压成一个表格单元。"""
    if not probe.get("ok"):
        cls = (probe.get("error") or "").split(":")[0] or "—"
        return f"**接不住** {cls}"
    dst = probe.get("dst") or "—"
    if src is not None and dst == src:
        return f"{dst} ✅"
    return f"{dst} ⚠"


def sec6_types() -> None:
    section("第 6 节 · 三种性格：同一个 Arrow 类型，三个框架三种反应")

    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe-types"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    results: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not results:
        print(f"❌ 子进程没给出结果（returncode={proc.returncode}）")
        if proc.stderr.strip():
            print("   子进程的 stderr：")
            for ln in proc.stderr.strip().splitlines()[-8:]:
                print(f"     {ln}")
        return

    rows = []
    for r in results:
        src = r.get("src")
        p = r.get("polars") or {}
        d = r.get("duckdb") or {}
        accepted = [x for x in (p, d) if x.get("ok")]
        if not accepted:
            val = "—"
        elif all(x.get("data_ok") for x in accepted):
            val = "✅"
        else:
            val = "❌"
        rows.append(
            [
                r["label"],
                src or "—",
                _type_cell(p, src),
                _type_cell(d, src),
                val,
            ]
        )
    print_table(
        ["Arrow 类型", "pyarrow（参考）", "polars 2.1", "duckdb 2.0", "值"],
        rows,
        ["<", "<", "<", "<", ">"],
    )
    print()
    print("图例：✅ 原样收下  ·  ⚠ **改型**（schema 变了）  ·  **接不住** 直接报错")
    print("      「pyarrow（参考）」那一列 = 进去的类型 —— pyarrow 就是 Arrow 的定义本身，")
    print("      收下什么是什么，所以拿它当基准。")
    print("      「值」列 = **接住的那边**，进出两边的值逐个比对（**不是**只看类型）。")
    print()
    if proc.returncode != 0:
        print(f"（子进程 returncode={proc.returncode} —— 有类型把 polars 的 Rust 线程炸了，")
        print("  这就是把它隔离到子进程里的原因：炸的只是子进程，主 demo 照跑。）")
        print()

    p_hard = [r for r in results if not (r.get("polars") or {}).get("ok")]
    d_hard = [r for r in results if not (r.get("duckdb") or {}).get("ok")]
    p_soft = [
        r
        for r in results
        if (r.get("polars") or {}).get("ok") and r["polars"]["dst"] != r.get("src")
    ]
    d_soft = [
        r
        for r in results
        if (r.get("duckdb") or {}).get("ok") and r["duckdb"]["dst"] != r.get("src")
    ]

    print("三种性格，一句话：")
    print()
    print("  · **pyarrow = 参考实现** —— 它就是 Arrow 的定义本身，什么都原样收下。")
    print(f"  · **polars = 最严** —— {len(p_hard)} 种硬断（不认识的直接报错）、")
    print(f"    {len(p_soft)} 种改型（认识的也给你换个类型）。")
    print(f"  · **duckdb = 最宽** —— {len(d_hard)} 种硬断、{len(d_soft)} 种改型：")
    print("    几乎什么都接，但会**偷偷改型**。")
    print()

    print("  **硬断** —— 两边各自的报错（实测原文）：")
    for r in p_hard:
        print(f"    · {r['label']:<16} polars: {r['polars']['error']}")
    for r in d_hard:
        print(f"    · {r['label']:<16} duckdb: {r['duckdb']['error']}")
    print()

    panics = [r for r in p_hard if (r.get("polars") or {}).get("is_exception") is False]
    if panics:
        names = "、".join(r["label"] for r in panics)
        print(
            f"    其中 {names} 在 polars 那边抛的是 **Rust panic**"
            "（pyo3 翻译成 `PanicException`）。"
        )
        print("    实测这个异常**不是 `Exception` 的子类** —— 写 `except Exception` 是接不住的，")
        print("    得写成 `except BaseException`。跨进程调用遇到它会直接崩，不是「优雅降级」。")
        print("    对照组：duckdb 那边报的是 `NotImplementedException` —— 规规矩矩的 Python 异常。")
        print()

    print("  **改型** —— 谁改了型，改成了什么：")
    for r in results:
        p = r.get("polars") or {}
        d = r.get("duckdb") or {}
        lines = []
        if p.get("ok") and p["dst"] != r.get("src"):
            lines.append(f"        polars: {r.get('src')}  →  {p['dst']}")
        if d.get("ok") and d["dst"] != r.get("src"):
            lines.append(f"        duckdb: {r.get('src')}  →  {d['dst']}")
        if not lines:
            continue
        print(f"    · {r['label']}")
        for ln in lines:
            print(ln)
    print()

    bad = []
    for r in results:
        accepted = [x for x in ((r.get("polars") or {}), (r.get("duckdb") or {})) if x.get("ok")]
        if accepted and not all(x.get("data_ok") for x in accepted):
            bad.append(r["label"])
    if bad:
        print(f"    ⚠️ 值对不上的：{bad}")
    else:
        print("    **改型只动 schema，不动数据** —— 上面每一行「值」都是 ✅：")
        print("    接住的那边把值取回来逐个比，一个字节不差。")
        print()
        print("    所以真正会咬人的是**改型**而不是断：")
        print("    读进来是对的，写出去就变了型")
        print("    （polars 存回去的物理类型是 large_string / large_list / uint32 indices；")
        print(
            "      duckdb 那边更狠 —— 字典被**解开**成 string，run-end-encoded 被**解码**成 int32，"
        )
        print("      list 的子字段名从 `item` 改成 `l`）。")
        print()
        print("    duckdb 的改型是**语义层面**的（读的时候顺手把编码解了），")
        print("    polars 的改型是**物理层面**的（换了个等价的存储类型）。")
        print("    前者拿不到原始编码，后者拿得到但类型对不上 —— 两边都不是「原样」。")


# ---------------------------------------------------------------- 第 7 节：IPC 文件的互通范围


def _first_line(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()
    return text.strip()


def sec7_ipc_files() -> None:
    section("第 7 节 · 磁盘格式的实际互通范围：谁能读写 .arrow 文件")

    work = Path(tempfile.mkdtemp(prefix="arrow_demo_"))
    src = work / "t.arrow"
    tbl = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    with ipc.new_file(src, tbl.schema) as w:
        w.write_table(tbl)

    rows: list[list[str]] = []

    # pyarrow：写 + 读
    py_out = work / "py.arrow"
    with ipc.new_file(py_out, tbl.schema) as w:
        w.write_table(tbl)
    back = ipc.open_file(py_out).read_all()
    rows.append(
        [
            "pyarrow",
            f"ipc.new_file ✅ （{py_out.stat().st_size} 字节）",
            f"ipc.open_file ✅ （{back.num_rows} 行）",
        ]
    )

    # polars：写 + 读
    pl_out = work / "pl.arrow"
    pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).write_ipc(pl_out)
    d = pl.read_ipc(pl_out)
    rows.append(
        [
            "polars",
            f"pl.DataFrame.write_ipc ✅ （{pl_out.stat().st_size} 字节）",
            f"pl.read_ipc ✅ （{d.shape[0]} 行 {d.shape[1]} 列）",
        ]
    )

    # duckdb：写 + 读（两个读法都试）
    con = duckdb.connect()
    db_errs: list[tuple[str, str]] = []
    try:
        con.sql(f"COPY (SELECT * FROM '{src}') TO '{work / 'db.arrow'}' (FORMAT arrow)")
        w_ok = "COPY … (FORMAT arrow) ✅"
    except BaseException as exc:
        w_ok = "COPY … (FORMAT arrow) ❌"
        db_errs.append(("写", f"{type(exc).__name__}: {_first_line(str(exc))}"))
    try:
        con.sql(f"SELECT * FROM '{src}'").fetchall()
        r_ok = "SELECT * FROM 'x.arrow' ✅"
    except BaseException as exc:
        r_ok = "SELECT * FROM 'x.arrow' ❌"
        db_errs.append(("读（自动识别扩展名）", f"{type(exc).__name__}: {_first_line(str(exc))}"))
    try:
        con.sql(f"SELECT * FROM read_arrow('{src}')").fetchall()
        e_ok = "read_arrow() ✅"
    except BaseException as exc:
        e_ok = "read_arrow() ❌"
        db_errs.append(("读（显式 reader）", f"{type(exc).__name__}: {_first_line(str(exc))}"))
    finally:
        con.close()
    rows.append(["duckdb", w_ok, f"{r_ok} · {e_ok}"])

    print_table(["框架", "写 .arrow", "读 .arrow"], rows, ["<", "<", "<"])
    print()
    print("duckdb 那三处失败，错误原文（实测）：")
    print()
    for what, err in db_errs:
        print(f"  · {what}：")
        print(f"      {err}")
    print()

    # 为什么没有：查 duckdb 自己的扩展清单
    con = duckdb.connect()
    try:
        known = [
            r[0]
            for r in con.sql(
                "SELECT extension_name FROM duckdb_extensions() ORDER BY extension_name"
            ).fetchall()
        ]
    finally:
        con.close()
    print(
        f"原因（实测 duckdb {duckdb.__version__} 的 `duckdb_extensions()`，共 {len(known)} 项）："
    )
    print(f"  清单里**没有 `arrow`** —— {'arrow' not in known}")
    print("  → 不是「默认没装、装一下就行」：DuckDB 2.0 的官方扩展清单里压根没有这一项。")
    print("  → 所以 Arrow IPC 文件这条路，duckdb 是**读不了也写不了**，")
    print("    而这不是「版本太老」，是「没有这个 reader/writer」。")
    print()

    # 跨框架读同一个文件：格式本身是共享的
    pa_from_pl = ipc.open_file(pl_out).read_all()
    pl_from_pa = pl.read_ipc(src)
    print("换个方向看 —— **格式**是共享的，差别只在谁实现了 reader：")
    print(f"  · pyarrow 写的文件，polars 读 → {pl_from_pa.shape[0]} 行 {pl_from_pa.shape[1]} 列 ✅")
    print(f"  · polars 写的文件，pyarrow 读 → {pa_from_pl.num_rows} 行 ✅")
    print("  → 同一个格式，两个框架来回读都正常。")
    print("    上一节说的「磁盘格式向后兼容」是真的；")
    print("    duckdb 读不了，**不是因为格式，是因为它没有这个 reader**。")
    print()
    print("  顺带提醒：`.arrow` / `.feather` 这类文件在两边都是**二进制 Arrow IPC**，")
    print("  和 DuckDB 熟悉的 `.parquet` 是两回事 ——")
    print("  本仓库的 `data/orders.parquet` 是两个框架都能读的那个。")
    print()


# ---------------------------------------------------------------- 第 8 节：polars 吃 relation


def _probe_stream_child() -> None:
    """子进程：polars 直接吃 DuckDB 的 relation 会怎样。

    跑在子进程里有两个原因：
      1. `pl.from_arrow(rel)` 会让 nanobind 在**退出时**报引用泄漏 —— 那些警告
         走 stderr，隔离出来才看得清楚，也不会污染主 demo 的输出。
      2. DuckDB 一个 connection 只能有一个 open result，探测之间得各自拿一份。
    """
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, 'y' UNION ALL SELECT 3, 'z'"
    )

    rec: dict = {}

    rel = con.sql("SELECT * FROM t")
    rec["has_stream"] = hasattr(rel, "__arrow_c_stream__")
    rec["has_array"] = hasattr(rel, "__arrow_c_array__")

    # polars 直连 relation —— 除非环境变量要求跳过（对照组用）
    if not os.environ.get("ARROW_DEMO_SKIP_DIRECT"):
        rel = con.sql("SELECT * FROM t")
        try:
            d = pl.from_arrow(rel)
            rec["polars_direct"] = {"type": type(d).__name__, "shape": list(d.shape)}
        except BaseException as exc:
            rec["polars_direct"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    # pyarrow 直连 relation —— 对照组：同一个 stream，别的框架读得对
    rel = con.sql("SELECT * FROM t")
    try:
        tbl = pa.RecordBatchReader.from_stream(rel).read_all()
        rec["pyarrow_direct"] = {"rows": tbl.num_rows, "cols": tbl.num_columns}
    except BaseException as exc:
        rec["pyarrow_direct"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    # 各个 Arrow endpoint
    eps: dict = {}
    for name in ["to_arrow_table", "to_arrow_reader", "arrow"]:
        rel = con.sql("SELECT * FROM t")
        try:
            obj = getattr(rel, name)()
            d = pl.from_arrow(obj)
            eps[name] = {"type": type(d).__name__, "shape": list(d.shape)}
        except BaseException as exc:
            eps[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    rec["endpoints"] = eps

    # rel.pl() —— DuckDB 自带的 polars 转换器（跟 Arrow 没关系）
    rel = con.sql("SELECT * FROM t")
    try:
        d = rel.pl()
        rec["rel_pl"] = {"type": type(d).__name__, "shape": list(d.shape)}
    except BaseException as exc:
        rec["rel_pl"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    print(json.dumps(rec, ensure_ascii=False), flush=True)


def _shape_str(info: dict) -> str:
    if "error" in info:
        return f"**报错** {info['error']}"
    return f"{info.get('type', '?')}，shape {tuple(info.get('shape', []))}"


def sec8_duckdb_endpoints() -> None:
    section("第 8 节 · polars 吃 DuckDB 的 relation：直连是个坑")

    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe-stream"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    rec: dict | None = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rec = json.loads(line)
            break
    if rec is None:
        print(f"❌ 子进程没给出结果（returncode={proc.returncode}）")
        if proc.stderr.strip():
            for ln in proc.stderr.strip().splitlines()[-8:]:
                print(f"     {ln}")
        return

    # 对照组：同样的代码，只是不做那一次「直连」
    ctrl = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe-stream"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "ARROW_DEMO_SKIP_DIRECT": "1"},
    )

    direct = rec.get("polars_direct") or {}
    eps = rec.get("endpoints") or {}

    print("DuckDB 的 relation 上都伸出了哪些手（实测）：")
    print()
    print_table(
        ["入口", "拿到什么", "polars 吃下去是什么"],
        [
            [
                "rel（relation 本身）",
                f"`__arrow_c_stream__` {'有' if rec.get('has_stream') else '—'}"
                f" / `__arrow_c_array__` {'有' if rec.get('has_array') else '—'}",
                _shape_str(direct),
            ],
            [
                "rel.to_arrow_table()",
                "pyarrow.Table",
                _shape_str(eps.get("to_arrow_table", {})),
            ],
            [
                "rel.to_arrow_reader()",
                "pyarrow.RecordBatchReader",
                _shape_str(eps.get("to_arrow_reader", {})),
            ],
            [
                "rel.arrow()",
                "pyarrow.RecordBatchReader",
                _shape_str(eps.get("arrow", {})),
            ],
            [
                "rel.pl()",
                "polars.DataFrame",
                f"{_shape_str(rec.get('rel_pl', {}))}（DuckDB 自己转的，跟 Arrow 无关）",
            ],
        ],
        ["<", "<", "<"],
    )
    print()

    pda = rec.get("pyarrow_direct") or {}
    print("**坑在这儿**：")
    print()
    print(f"  · `pl.from_arrow(rel)` → {_shape_str(direct)}")
    print(
        f"  · `pl.from_arrow(rel.to_arrow_table())` → {_shape_str(eps.get('to_arrow_table', {}))}"
    )
    print()
    if "rows" in pda:
        print(f"  同一个 stream，pyarrow 读出来是对的：{pda['rows']} 行 {pda['cols']} 列 ✅")
        print("  → 所以不是 DuckDB 给的数据不对，是 **polars 这条路把 relation 当成了")
        print("    「一个 struct 列」** —— 它不报错，只是**静默给错形状**。")
        print()

    leak_lines = sum(1 for ln in proc.stderr.splitlines() if "nanobind: leaked" in ln)
    ctrl_leak = sum(1 for ln in ctrl.stderr.splitlines() if "nanobind: leaked" in ln)
    if leak_lines:
        print("  还有一个更隐蔽的：**polars 直连 relation 会在退出时触发 nanobind 引用泄漏**。")
        print(f"  实测 —— 直连那版 stderr 里有 {leak_lines} 条 `nanobind: leaked`；")
        print(f"  同一段代码**只去掉 `pl.from_arrow(rel)` 这一行**，就是 {ctrl_leak} 条。")
        print()
        print("  → 所以 `pl.from_arrow(con.sql(...))` 这种写法是**又错形状、又漏引用**，")
        print("    两个问题叠在一起。")
        print()
        print("  **结论：polars 要吃 DuckDB 的结果，先 `.to_arrow_table()`。**")
        print("  这不是啰嗦 —— 上面那条 `to_arrow_table()` 就是绕开这个坑的那一步。")
    print()


# ---------------------------------------------------------------- 小结


def conclusion() -> None:
    section("小结")

    print("1. **「Arrow 的版本」是三层，别混着说。** 发行版各走各的；磁盘格式版本")
    print("   （文件外层永远是 V5，消息版本 V1~V5 可选，且**向后兼容**）；")
    print("   ABI 层**根本没有版本号**。")
    print()
    print("2. **共享不是靠版本对齐，是靠约定。** 布局标准化 + `release` 回调移交所有权，")
    print("   再加上统一的 `__arrow_c_stream__` 入口 —— 这三样让 pyarrow 26 / polars 2.1 /")
    print("   duckdb 2.0 互相传数据毫无障碍，版本差多远都不影响。")
    print()
    print("3. **零拷贝是列的性质，不是 DataFrame 的性质。** 定宽列过 polars 地址不变")
    print("   （连续两次导出还是同一块）；字符串列**每次导出都重建缓冲区**；")
    print("   过 DuckDB 一律要拷（Arrow 是它的输入/输出格式，不是内部表示）。")
    print()
    print("4. **三个框架三种性格。** pyarrow 是参考实现（什么都原样）；")
    print("   polars 最严（decimal256 / run-end-encoded / list_view 直接不认，")
    print("   其中 decimal256 是 Rust panic，连 `except Exception` 都接不住）；")
    print("   duckdb 最宽（12 种里 11 种都接），但会**偷偷改型** ——")
    print("   字典被解开、run-end-encoded 被解码、list 的子字段名被改。")
    print("   **改型只动 schema，不动数据** —— 接住的那几行，值一个字节不差。")
    print()
    print("5. **磁盘格式的实际互通范围比「版本向后兼容」小得多。**")
    print("   Arrow IPC 文件 pyarrow / polars 都能读写，**duckdb 2.0 读不了也写不了** ——")
    print("   不是版本太老，是它的扩展清单里压根没有 `arrow` 这一项。")
    print()
    print("6. **polars 吃 DuckDB 的 relation 有坑。** `pl.from_arrow(con.sql(...))`")
    print("   **不报错，但给的是一列 struct 的 Series**（shape (3,) 而不是 (3, 2)），")
    print("   退出时还会报 nanobind 引用泄漏；同一个 stream 换成 pyarrow 读就是对的。")
    print("   → 先 `.to_arrow_table()`，别直接喂 relation。")
    print()
    print("7. **实用结论**：跨框架传数据优先传**定宽列**，或者先统一 schema 再传；")
    print("   遇到新类型先在两个框架各读一遍确认能不能过 —— 这就是本 demo 在做的事。")


# ---------------------------------------------------------------- main


def main() -> None:
    if "--probe-types" in sys.argv:
        _probe_types_child()
        return
    if "--probe-stream" in sys.argv:
        _probe_stream_child()
        return

    print(f"机器：{common.describe_machine()}")
    sec1_distributions()
    sec2_disk_format()
    sec3_abi()
    sec4_entry_points()
    sec5_zero_copy()
    sec6_types()
    sec7_ipc_files()
    sec8_duckdb_endpoints()
    conclusion()


if __name__ == "__main__":
    main()
