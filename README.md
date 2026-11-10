# duckdb-polars-demo

用同一份数据，对比 **DuckDB** 与 **Polars** 两种分析引擎在 Python 侧的用法、语义差异与运行性能。

每条查询都有两份实现（DuckDB/SQL 与 Polars/DataFrame），**基准测试除了计时，还会逐值校验两个引擎的输出是否等价** —— 只比快慢而不对账是没有意义的。

## 项目目标

| 关注点 | 说明 |
| --- | --- |
| 用法差异 | SQL（声明式）与 DataFrame API（命令式）如何表达同一个分析任务 |
| 执行模型 | DuckDB 向量化执行 + SQL 优化器 vs. Polars 惰性查询 + 流式执行 |
| 语义差异 | 排名、聚合类型、窗口过滤等地方两个引擎的默认行为是否一致 |
| 性能 | 相同数据规模下的耗时对比；结果等价性校验 |

## 环境要求

- Python **3.13**（`.python-version` 已锁定；`requires-python = ">=3.11"`）
- [uv](https://docs.astral.sh/uv/) 0.13+ —— 依赖管理与虚拟环境
- DuckDB CLI（可选，用于交互式探索）
- Linux / macOS

实测环境：Python 3.13.5、DuckDB 2.0.0、Polars 2.1.0、8 核 x86_64 Linux。

## 快速开始

```bash
uv sync                              # 建 .venv，装 duckdb / polars / pyarrow / numpy
uv run python src/gen_data.py        # 造数（100 万订单 + 2 万用户）
uv run python src/duckdb_demo.py     # 跑 DuckDB 实现
uv run python src/polars_demo.py     # 跑 Polars 实现
uv run python src/benchmark.py       # 计时 + 等价性校验
```

一键跑完整流程：

```bash
scripts/bench.sh 1000000 5           # 参数：订单行数、重复次数
```

其他开关：

```bash
uv run python src/duckdb_demo.py --explain     # 打印 DuckDB 执行计划
uv run python src/polars_demo.py --explain     # 打印 Polars 优化后计划
uv run python src/benchmark.py --source duckdb # DuckDB 读物化后的 .duckdb 文件
uv run python src/benchmark.py --json out.json # 结果写成 JSON
```

用 DuckDB CLI 交互探索：

```bash
duckdb data/demo.duckdb
```

## 数据模型

`src/gen_data.py` 用固定随机种子（`SEED = 42`）生成，数据跨机器可复现。

**orders**（事实表，100 万行）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `order_id` | i64 | 主键 |
| `user_id` | i64 | 外键 → users |
| `order_ts` | timestamp[ns] | 下单时间，落在 2023-07 ~ 2025-06 |
| `region` | str | 华东/华北/华南/西南/华中 |
| `category` | str | 数码/家居/服饰/食品/美妆/运动 |
| `channel` | str | app/web/小程序/门店 |
| `quantity` | i32 | 数量 |
| `unit_price` | f64 | 单价 |
| `revenue` | f64 | 金额 = quantity × unit_price |
| `is_returned` | bool | 退货标记，退货率与渠道相关（门店最高 18%） |

**users**（维表，2 万行）：`user_id`、`signup_date`、`country`、`tier`。

用户订单数服从幂律分布（少量高频用户）；数据时间跨度大于分析窗口，让 `WHERE` 过滤真正做功。

**分析窗口**：`[2024-01-01, 2024-07-01)`，左闭右开。

## 三条对照查询

| 查询 | DuckDB | Polars |
| --- | --- | --- |
| 区域 × 类目汇总 + 区域内 Top-3 | `GROUP BY` + `RANK() OVER` + `QUALIFY` | `group_by` + `rank().over()` + `filter` |
| 每日收入趋势 | `order_ts::DATE` + `GROUP BY` | `.dt.date()` + `group_by` |
| 高价值客户 Top 20 | `GROUP BY` + `ORDER BY … LIMIT` | `group_by` + `sort` + `head` |

完整 SQL 见 `src/duckdb_demo.py`，完整表达式链见 `src/polars_demo.py`。

## 基准结果

100 万订单 / 2 万用户，重复 5 次取中位数（本机实测，**仅供参考，硬件差异很大**）：

**场景 A：两边都直接扫描同一份 Parquet**

| 查询 | DuckDB(ms) | Polars(ms) | 更快 | 等价 |
| --- | --- | --- | --- | --- |
| 区域 × 类目 Top-N | 57.5 | 35.7 | Polars 1.61× | ✅ |
| 每日收入趋势 | 49.2 | 29.4 | Polars 1.67× | ✅ |
| 高价值客户 Top 20 | 56.4 | 45.2 | Polars 1.25× | ✅ |

**场景 B：DuckDB 读物化后的 `demo.duckdb`，Polars 仍读 Parquet**

| 查询 | DuckDB(ms) | Polars(ms) | 更快 | 等价 |
| --- | --- | --- | --- | --- |
| 区域 × 类目 Top-N | 17.8 | 36.8 | DuckDB 2.07× | ✅ |
| 每日收入趋势 | 10.2 | 30.0 | DuckDB 2.94× | ✅ |
| 高价值客户 Top 20 | 13.7 | 32.4 | DuckDB 2.37× | ✅ |

**结论**：直接扫 Parquet 时 Polars 更快；但 DuckDB 读自己的存储格式快得多 —— **存储格式带来的差异（2~3×）大于引擎本身的差异（1.2~1.7×）**。选型时先决定存储格式，再谈用什么引擎。

另外注意：这个量级下两个引擎都在 10~60 ms，噪声足以让快慢关系在不同轮次翻转（Top 20 那条查询在多次运行里两边都赢过）。**单次测量没有意义**，本项目默认重复 5 次取中位数，并保留 `--json` 便于做多轮统计。

## 语义差异（踩坑记录）

两个引擎默认行为并不一致，这是本项目最有价值的部分。以下每一条都在代码里做了显式对齐：

| # | 差异 | DuckDB | Polars | 对齐方式 |
| --- | --- | --- | --- | --- |
| 1 | 并列名次 | `RANK()` 取最小名次（1,2,2,4） | `rank()` 默认 `average`（1,2,2.5,4） | `rank(method="min")` |
| 2 | 整数求和类型 | `SUM(int32)` → HUGEINT（Arrow 里是 `decimal[38,0]`） | `sum()` → Int64 | 两边都转 `Int64` / `::BIGINT` |
| 3 | 计数类型 | `COUNT(*)` → BIGINT | `pl.len()` → UInt32 | 两边都转 Int64 |
| 4 | 窗口过滤 | 有 `QUALIFY` 子句 | 没有等价语法 | 先 `with_columns` 算窗口，再 `filter` |
| 5 | 浮点求和顺序 | 并行 + 向量化，顺序不保证 | 同左 | `SUM` 末位可能有 1e-9 级差异，校验用容差（`rtol=1e-9`）而非精确相等 |
| 6 | DuckDB 2.0 Python API | `con.execute(sql).arrow()` 已移除 | —— | 用 `con.sql(query, params).to_arrow_table()`；参数绑定受支持，且是 relation（每次读取都重新执行），不走 DB-API 就不会多一次行拷贝 |

> 第 6 条的坑：`con.execute(sql)` 在 2.0 里返回的是连接对象而不是结果，`.arrow()` 已经不存在；退而用 `con.to_arrow_table()` 会警告 "makes an extra copy of the rows" —— 那次拷贝会让基准测试不公平地惩罚 DuckDB。

两边都能把过滤和投影下推到 Parquet 扫描，用 `--explain` 可以直接看到：

- DuckDB：`Table Scan → Filters: (order_ts >= …) AND NOT is_returned`，且 Hash Join 会生成 `Dynamic Filters` 反向优化扫描
- Polars：`Parquet SCAN → SELECTION: (col("is_returned").not() & (…))`，投影从 10 列裁到 7 列

## 目录结构

```
duckdb-polars-demo/
├── README.md
├── pyproject.toml          # uv 项目定义与依赖
├── .python-version         # 锁定 Python 3.13
├── data/                   # 生成的数据（.gitignore 排除，用脚本重新生成）
│   ├── orders.parquet
│   ├── users.parquet
│   └── demo.duckdb
├── src/
│   ├── common.py           # 唯一事实来源：口径常量 + 结果等价性校验 + 终端排版
│   ├── gen_data.py         # 造数
│   ├── duckdb_demo.py      # DuckDB / SQL 实现
│   ├── polars_demo.py      # Polars / DataFrame 实现
│   └── benchmark.py        # 计时 + 等价性校验
└── scripts/
    └── bench.sh            # 一键跑完整流程
```

## 设计约定

- **唯一事实来源**：时间窗口、维度取值、列顺序、比较主键全部集中在 `src/common.py`，两个实现共用，避免口径漂移。
- **结果先对账再比快慢**：`common.compare_frames()` 先按主键排序消除行序差异，再逐列比对（浮点用相对/绝对容差，其余列要求完全相等，含 NULL 位置）。任一查询不一致，基准测试以退出码 1 结束。
- **计时口径**：只覆盖查询执行与结果物化，不含进程启动和连接建立；默认预热 1 次（Polars 的惰性计划在首次 `collect()` 时才编译）。
- **可复现**：造数固定随机种子，基准测试打印 Python / 引擎版本、CPU 核数、平台与进程峰值 RSS。

## 参考资料

- DuckDB 文档：<https://duckdb.org/docs/>
- Polars 文档：<https://docs.pola.rs/>
- DuckDB 与 Arrow 互操作：<https://duckdb.org/docs/guides/python/sql_on_arrow>
