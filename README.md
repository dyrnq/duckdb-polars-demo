# duckdb-polars-demo

用同一份数据，对比 **DuckDB** 与 **Polars** 两种分析引擎在 Python 侧的用法、语义差异与运行性能。

> 状态：项目骨架阶段 —— README 与工程结构先行，示例代码待补充。

## 项目目标

| 关注点 | 说明 |
| --- | --- |
| 用法差异 | SQL（声明式）与 DataFrame API（命令式）如何表达同一个分析任务 |
| 执行模型 | DuckDB 向量化执行 + SQL 优化器 vs. Polars 惰性查询 + 流式执行 |
| 互操作 | 两个引擎能否直接读同一批 Parquet/CSV、结果如何互转 |
| 性能 | 在相同数据规模下的耗时与峰值内存对比 |

## 环境要求

- Python **3.13+**（当前环境 3.13.5）
- [uv](https://docs.astral.sh/uv/) 0.13+ —— 依赖管理与虚拟环境
- DuckDB CLI 1.5.3（可选，用于交互式对比）
- Linux / macOS

## 快速开始

```bash
# 安装依赖（创建 .venv 并安装 duckdb、polars、pyarrow）
uv sync

# 生成演示数据
uv run python src/gen_data.py

# 分别运行两个引擎的实现
uv run python src/duckdb_demo.py
uv run python src/polars_demo.py

# 跑性能对比（输出耗时与内存表）
uv run python src/benchmark.py
```

使用 DuckDB CLI 直接交互：

```bash
duckdb data/demo.duckdb
```

## 目录结构

```
duckdb-polars-demo/
├── README.md
├── pyproject.toml        # uv 项目定义与依赖
├── data/                 # 生成的数据（.gitignore 排除大文件）
│   └── demo.duckdb
├── src/
│   ├── gen_data.py       # 造数：生成可复现的 Parquet 测试数据
│   ├── common.py         # 两个引擎共用的口径（字段、聚合逻辑）
│   ├── duckdb_demo.py    # DuckDB / SQL 实现
│   ├── polars_demo.py    # Polars / DataFrame 实现
│   └── benchmark.py      # 性能对比与结果一致性校验
└── scripts/
    └── bench.sh          # 批量跑基准测试
```

## 设计约定

- **同一口径**：`common.py` 里定义指标口径，两个实现共用，避免因逻辑差异导致对比失真。
- **结果校验**：`benchmark.py` 会对两个引擎的输出做等价性断言（行数、聚合值、排序），不只比耗时。
- **可复现**：造数使用固定随机种子，基准测试记录 Python / 引擎版本与硬件信息。
- **单机内存数据**：规模以单机可跑为准（默认百万级行），重点看执行模型差异而非极限压测。

## 参考资料

- DuckDB 文档：<https://duckdb.org/docs/>
- Polars 文档：<https://docs.pola.rs/>
- DuckDB 与 Polars 互操作：<https://duckdb.org/docs/guides/python/sql_on_arrow>
