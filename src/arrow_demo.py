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
  5. **类型层会断**：pyarrow 26 的新类型（decimal256 / run-end-encoded / list_view）
     polars 2.1 直接不认；`string` / `list` / `map` / `dictionary` 会被悄悄改型。

用法：
    uv run python src/arrow_demo.py
"""

from __future__ import annotations

import json
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


def layer1_distributions() -> None:
    section("第 1 层 · 发行版：三个框架各走各的，谁也不约束谁")

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


def layer2_disk_format() -> None:
    section("第 2 层 · 磁盘格式版本：IPC 文件里有**两个**版本号")

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


def layer3_abi() -> None:
    section("第 3 层 · ABI（C Data Interface）：结构体里**没有版本号**")

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


def layer4_entry_points() -> None:
    section("第 4 层 · 跨框架的共同入口：`__arrow_c_stream__`")

    probes = [
        ("pyarrow.Table", pa.table({"a": [1]})),
        ("pyarrow.Array", pa.array([1])),
        ("pyarrow.ChunkedArray", pa.chunked_array([[1]])),
        ("polars.DataFrame", pl.DataFrame({"a": [1]})),
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
        "注意一个反直觉的地方：`__arrow_c_array__`（单个数组）在 **Table 和 DataFrame 上都没有**，"
    )
    print("只有 **stream** 接口是两边都有的。")
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


def layer5_zero_copy() -> None:
    section("第 5 层 · 零拷贝的边界：buffer 地址到底是不是同一块")

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


def _probe_types_child() -> None:
    """子进程：逐个类型试 polars 能不能接住，一行一个 JSON。"""
    for label, kind in TYPE_CASES:
        rec: dict = {"label": label, "kind": kind}
        try:
            t = _build_case(kind)
        except BaseException as exc:
            rec.update(
                ok=False,
                src=None,
                dst=None,
                error=f"构造失败：{type(exc).__name__}: {str(exc)[:80]}",
            )
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            continue
        rec["src"] = str(t.schema.field("a").type)
        try:
            d = pl.from_arrow(t)
            rec.update(ok=True, dst=str(d.to_arrow().schema.field("a").type), error=None)
        except BaseException as exc:
            # polars 的 Rust panic 不是 Exception 的子类，普通 except Exception 接不住
            rec.update(
                ok=False,
                dst=None,
                error=f"{type(exc).__name__}: {str(exc)[:80]}",
                is_exception=isinstance(exc, Exception),
            )
        print(json.dumps(rec, ensure_ascii=False), flush=True)


def layer6_types() -> None:
    section("第 6 层 · 类型层的断点：跨框架时类型会断、会改")

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
        if not r.get("ok"):
            err = r.get("error", "")
            short = err.split(":")[0] if err else "—"
            rows.append([r["label"], "**接不住**", r.get("src") or "—", "—", short])
        elif r.get("src") == r.get("dst"):
            rows.append([r["label"], "接住", r["src"], r["dst"], "原样 ✅"])
        else:
            rows.append([r["label"], "接住", r["src"], r["dst"], "**改型了** ⚠️"])
    print_table(
        ["Arrow 类型", "polars", "进去的类型", "出来的类型", "备注"],
        rows,
        ["<", "<", "<", "<", "<"],
    )
    print()
    if proc.returncode != 0:
        print(f"（子进程 returncode={proc.returncode} —— 有类型把 polars 的 Rust 线程炸了，")
        print("  这就是把它隔离到子进程里的原因：炸的只是子进程，主 demo 照跑。）")
        print()

    hard = [r for r in results if not r.get("ok")]
    soft = [r for r in results if r.get("ok") and r.get("src") != r.get("dst")]
    print(f"两类断法（实测 {len(hard)} 种硬断、{len(soft)} 种改型）：")
    print()
    print("  **硬断** —— polars 2.1 根本不认这些 Arrow 26 的新类型：")
    for r in hard:
        print(f"    · {r['label']:<16} {r.get('error')}")
    print()
    panics = [r for r in hard if r.get("is_exception") is False]
    if panics:
        names = "、".join(r["label"] for r in panics)
        print(f"    其中 {names} 抛的是 **Rust panic**（pyo3 翻译成 `PanicException`）。")
        print("    实测这个异常**不是 `Exception` 的子类** —— 写 `except Exception` 是接不住的，")
        print("    得写成 `except BaseException`。跨进程调用遇到它会直接崩，不是「优雅降级」。")
        print()
    print("  **改型** —— 数据一个字节不差，但 schema 变了：")
    for r in soft:
        print(f"    · {r['label']:<16} {r['src']}  →  {r['dst']}")
    print()
    print("    这类最容易出事：**读进来是对的，写出去就变了型**")
    print("    （polars 存回去的物理类型是 large_string / large_list / uint32 indices）。")


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
    print("4. **类型会断，也会悄悄改。** pyarrow 26 的 decimal256 / run-end-encoded /")
    print("   list_view 在 polars 2.1 直接不认（其中 decimal256 是 Rust panic，")
    print("   连 `except Exception` 都接不住）；")
    print("   `string`/`list`/`map`/`dictionary` 会被改型 —— 数据对，schema 不对。")
    print()
    print("5. **实用结论**：跨框架传数据优先传**定宽列**，或者先统一 schema 再传；")
    print("   遇到新类型先在两个框架各读一遍确认能不能过 —— 这就是本 demo 在做的事。")


# ---------------------------------------------------------------- main


def main() -> None:
    if "--probe-types" in sys.argv:
        _probe_types_child()
        return

    print(f"机器：{common.describe_machine()}")
    layer1_distributions()
    layer2_disk_format()
    layer3_abi()
    layer4_entry_points()
    layer5_zero_copy()
    layer6_types()
    conclusion()


if __name__ == "__main__":
    main()
