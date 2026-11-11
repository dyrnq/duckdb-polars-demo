# duckdb-polars-demo

[![CI](https://github.com/dyrnq/duckdb-polars-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/dyrnq/duckdb-polars-demo/actions/workflows/ci.yml)

用同一份数据，对比 **DuckDB** 与 **Polars** 两种分析引擎在 Python 侧的用法、语义差异与运行性能。

每条查询都有两份实现（DuckDB/SQL 与 Polars/DataFrame），**基准测试除了计时，还会逐值校验两个引擎的输出是否等价** —— 只比快慢而不对账是没有意义的。

## 项目目标

| 关注点 | 说明 |
| --- | --- |
| 用法差异 | SQL（声明式）与 DataFrame API（命令式）如何表达同一个分析任务 |
| 执行模型 | DuckDB 向量化执行 + SQL 优化器 vs. Polars 惰性查询 + 流式执行 |
| 语义差异 | 排名、聚合类型、窗口过滤等地方两个引擎的默认行为是否一致 |
| 并行 | 多线程扩展性与并行效率；线程数变了结果是否还一致 |
| 存储与索引 | 数据布局、索引的适用边界与代价 —— 点查快不快到底取决于什么 |
| 金额精度 | 浮点求和会错到什么程度；想要精确金额该用哪种类型 |
| 时间与时区 | naive / aware、DST、session 时区 —— 跨时区算错的几种方式 |
| 写入与事务 | 覆盖还是追加、原子性、事务与并发读写 |
| 文件布局 | 多文件与分区表：schema 冲突、分区裁剪、往返保真 |
| 性能 | 相同数据规模下的耗时对比；结果等价性校验 |
| 语义覆盖面 | 窗口 / 聚合 / 集合运算 / 正则 / 增删改 / Schema / 时间轴 / 近似计算 —— 每个都有一份实测对账 |
| 形状与能力 | 多维聚合 / 透视重塑 / 嵌套类型 / 查询计划 / 采样 / 约束 / DDL / 内存 —— 引擎能做什么、不能做什么 |
| 边界与容错 | 算术边界 / 存在性查询 / 递归计算 / 坏数据 —— 出错时是「炸掉」还是「安静地算错」 |

## 环境要求

- Python **3.13**（`.python-version` 已锁定；`requires-python = ">=3.11"`）
- [uv](https://docs.astral.sh/uv/) 0.13+ —— 依赖管理与虚拟环境
- DuckDB CLI（可选，用于交互式探索）
- Linux / macOS

实测环境：Python 3.13.5、DuckDB 2.0.0、Polars 2.1.0、8 核 x86_64 Linux。

两个平台都能跑，但有几处「量机器状态」的代码要换实现 —— `/proc` 是 Linux 独有的：

| 测什么 | Linux | macOS |
| --- | --- | --- |
| 机器负载 load1 | `/proc/loadavg` | `os.getloadavg()`（底层是 sysctl） |
| 子进程峰值内存 | `/proc/self/status` 的 `VmHWM` | `resource.getrusage` 的 `ru_maxrss`（那边单位是**字节**，不是 KiB） |
| 进程当前 RSS | `/proc/self/statm` | 同上，退回到峰值 |
| 物理内存 | `/proc/meminfo` 的 `MemTotal` | `os.sysconf("SC_PHYS_PAGES")` |

负载读不到时（比如在容器里、`/proc` 没挂载），demo 会**明说「读不到」**，
而不是报一个假的 0.0 —— 「量不到」和「机器很闲」是两回事，
后者会直接影响那些性能结论到底能不能信。

## 快速开始

```bash
uv sync                              # 建 .venv，装 duckdb / polars / pyarrow / numpy
uv run python src/gen_data.py        # 造数（100 万订单 + 2 万用户）
uv run python src/duckdb_demo.py     # 跑 DuckDB 实现
uv run python src/polars_demo.py     # 跑 Polars 实现
uv run python src/benchmark.py       # 计时 + 等价性校验
uv run python src/interop.py         # 互操作演示：同一份 Parquet + Arrow 零拷贝接力
uv run python src/dialect_demo.py    # 方言差异专场：NULL 排序 / NaN / 透视表 / 递归 CTE …
uv run python src/streaming_demo.py  # 流式 vs 物化：内存随数据量怎么变
uv run python src/udf_demo.py        # 自定义函数：两边「Python UDF」代价差很多
uv run python src/scaling_demo.py    # 线程扩展性：1/2/4/8 线程各跑一遍，看加速比与并行效率
uv run python src/asof_demo.py       # ASOF JOIN：时间序列 join 的语义差异（并列 / 排序 / tolerance）
uv run python src/json_demo.py       # JSON 与嵌套类型：类型推断、精度损失、STRUCT/LIST 互转
uv run python src/index_demo.py      # 索引与点查：ART 索引的作用范围与代价，以及 Polars 的点查方案
uv run python src/decimal_demo.py    # 金额与 Decimal：0.1+0.2、百万笔求和、谁更准
uv run python src/null_demo.py       # NULL 与三值逻辑：=、JOIN、SUM、NOT IN 的空洞
uv run python src/join_demo.py       # JOIN 全景：七种连接、输出列结构、重复键放大行数
uv run python src/write_demo.py      # 写入路径与事务：覆盖还是追加、原子替换、ACID
uv run python src/csv_demo.py        # CSV 读写：类型推断、采样窗口、往返保真
uv run python src/tz_demo.py         # 日期时间与时区：naive/aware、夏令时、Parquet 往返
uv run python src/order_demo.py      # 排序稳定性：NULL 位置、并列顺序、LIMIT 取到不同的行
uv run python src/partition_demo.py  # 多文件与分区表：glob、schema 冲突、分区裁剪
```

一键跑完整流程：

```bash
scripts/bench.sh 1000000 5           # 参数：订单行数、重复次数
```

### 演示脚本一览

| 脚本 | 讲什么 |
| --- | --- |
| `gen_data.py` | 造可复现的测试数据 |
| `duckdb_demo.py` / `polars_demo.py` | 同一条分析任务的两种写法 |
| `benchmark.py` | 计时 **并且** 逐值校验两个引擎的输出等价 |
| `interop.py` | Parquet / Arrow 零拷贝接力，两个方向都能通 |
| `dialect_demo.py` | 8 个「静默算错」的语义差异场景 |
| `streaming_demo.py` | 流式消费的内存是 O(1)，一次性物化是 O(n) |
| `udf_demo.py` | 同样叫 UDF，DuckDB 逐行、Polars 按批，代价差很多 |
| `scaling_demo.py` | 线程扩展性：加速比、并行效率、轮间波动，以及线程数变了结果是否还一致 |
| `asof_demo.py` | ASOF JOIN：INNER/LEFT 语义、并列时间戳、排序要求、容忍窗口 |
| `json_demo.py` | JSON 与嵌套类型：类型推断差异、大整数精度、STRUCT/LIST 互转、解析代价 |
| `index_demo.py` | 索引与点查：ART 索引的作用范围与代价，以及 Polars 没有索引时的点查办法 |
| `decimal_demo.py` | 金额与 Decimal：字面量类型、浮点求和误差有多大、想要精确金额的三条路 |
| `null_demo.py` | NULL 与三值逻辑：`=`、连接键、`SUM`、`COUNT(DISTINCT)`、`NOT IN` 里的空洞 |
| `join_demo.py` | JOIN 全景：七种连接行数一致，但**输出列结构**不同；重复键放大行数 |
| `write_demo.py` | 写入路径与事务：覆盖还是追加、原子替换、ACID 与读写并发 |
| `csv_demo.py` | CSV 读写：类型推断、采样窗口、坏行、往返保真 |
| `tz_demo.py` | 日期时间与时区：精度、会话时区、夏令时、Parquet 往返 |
| `order_demo.py` | 排序稳定性：NULL/NaN 位置、并列顺序不确定、LIMIT 会取到不同的行 |
| `partition_demo.py` | 多文件与分区表：glob 读法、schema 冲突、分区裁剪、分区写 |
| `window_demo.py` | 窗口函数：默认 frame 是 RANGE 还是 ROWS、frame 家族、命名窗口、QUALIFY、LAG/LEAD |
| `agg_demo.py` | 聚合函数语义：分位数插值、众数、arg_max 并列、字符串聚合顺序、整数求和溢出 |
| `setop_demo.py` | 集合运算与去重：EXCEPT/INTERSECT 里的 NULL、UNION、去重保留哪一行 |
| `regex_demo.py` | 正则与字符串：分组编号、整串 vs 子串、替换、切分、位置、中文与 Emoji 的字节/字符 |
| `dml_demo.py` | 增删改：UPDATE/DELETE/INSERT、upsert、子查询快照、RETURNING、事务 |
| `schema_demo.py` | Schema 演进：ALTER TABLE、标识符大小写、类型转换、读不一致的 schema、拼表 |
| `timeseries_demo.py` | 时间轴与序列生成：端点语义、时间类型、补齐空档、截断、间隔加减 |
| `approx_demo.py` | 近似计算 vs 精确：HLL 误差方向与量级、近似分位数、histogram、值不值 |
| `arith_demo.py` | 数值与算术边界：除法语义、取模符号、舍入规则、整数溢出、除以零 |
| `groupsets_demo.py` | 多维聚合：GROUPING SETS / ROLLUP / CUBE，以及 Polars 侧的手工复刻 |
| `pivot_demo.py` | 透视与重塑：PIVOT / UNPIVOT 的形状、缺失组合填充、往返保真 |
| `nested_demo.py` | 嵌套类型：LIST / STRUCT / MAP 与 DuckDB 2.0 的 lambda 语法变更 |
| `plan_demo.py` | 查询计划：EXPLAIN 的两种形状、投影裁剪与过滤下推 |
| `sample_demo.py` | 采样与随机：system / bernoulli / reservoir、种子可复现性 |
| `constraint_demo.py` | 约束与完整性：PK / FK / CHECK / NOT NULL、序列 |
| `ddl_demo.py` | 视图、宏与持久化对象：CREATE VIEW / MACRO / SEQUENCE，物化视图的缺席 |
| `memory_demo.py` | 内存上限与落盘：memory_limit、temp_directory、流式引擎 |
| `exists_demo.py` | 存在性查询与半连接：EXISTS / IN / NOT IN 与 semi / anti join |
| `recursive_demo.py` | 递归 CTE 与迭代计算：WITH RECURSIVE 与 Polars 侧的替代写法 |
| `fault_demo.py` | 容错模式与坏数据：ignore_errors / store_errors / try_cast |
| `sql.py` | 临时跑一条 SQL（绕开 duckdb CLI 的版本问题） |

其他开关：

```bash
uv run python src/duckdb_demo.py --explain     # 打印 DuckDB 执行计划
uv run python src/polars_demo.py --explain     # 打印 Polars 优化后计划
uv run python src/benchmark.py --source duckdb # DuckDB 读物化后的 .duckdb 文件
uv run python src/benchmark.py --json out.json # 结果写成 JSON
uv run python src/streaming_demo.py --rows 8000000  # 换更大的数据量
uv run python src/udf_demo.py --rows 200000        # 换更小的数据量
uv run python src/interop.py --extension           # 试装 duckdb 的 polars 扩展（要联网）
uv run python src/scaling_demo.py --rows 8000000 --threads 1,2,4,8
uv run python src/asof_demo.py --trades 800000 --quotes 400000
uv run python src/json_demo.py --rows 500000
uv run python src/index_demo.py --rows 8000000 --rounds 5
uv run python src/decimal_demo.py --rows 200000     # 换更小的金额笔数
uv run python src/order_demo.py --rows 500000       # 换更小的排序样本
uv run python src/partition_demo.py --files 500     # 换更多的小文件
uv run python src/join_demo.py --repeat 3          # 换更少的重复次数
uv run python src/write_demo.py --repeat 3
uv run python src/tz_demo.py --repeat 3
uv run python src/window_demo.py --rows 2000000 --repeat 5
uv run python src/agg_demo.py --rows 2000000 --repeat 5
uv run python src/setop_demo.py --rows 2000000 --repeat 5
uv run python src/regex_demo.py --rows 1000000 --repeat 5
uv run python src/dml_demo.py --rows 2000000 --repeat 5
uv run python src/schema_demo.py --rows 2000000 --repeat 5
uv run python src/timeseries_demo.py --rows 2000000 --repeat 5
uv run python src/approx_demo.py --rows 10000000 --repeat 5
uv run python src/arith_demo.py --rows 5000000 --repeat 5
uv run python src/groupsets_demo.py --rows 2000000 --repeat 5
uv run python src/pivot_demo.py --rows 2000000 --repeat 5
uv run python src/nested_demo.py --rows 2000000 --repeat 5
uv run python src/plan_demo.py --rows 2000000 --repeat 5
uv run python src/sample_demo.py --rows 5000000 --repeat 5
uv run python src/constraint_demo.py --rows 2000000 --repeat 5
uv run python src/ddl_demo.py --rows 2000000 --repeat 5
uv run python src/memory_demo.py --rows 2000000 --repeat 5
uv run python src/exists_demo.py --rows 2000000 --repeat 5
uv run python src/recursive_demo.py --rows 2000000 --repeat 5
uv run python src/fault_demo.py --rows 2000000 --repeat 5
```

用 DuckDB 交互式探索数据：

```bash
uv run python src/sql.py "SELECT region, count(*) AS n FROM orders GROUP BY 1 ORDER BY n DESC"
uv run python src/sql.py --source parquet "SELECT * FROM users LIMIT 5"
```

> **别直接用 `duckdb data/demo.duckdb`**：DuckDB 2.x 写出的存储格式是 v999，而系统里装的
> duckdb CLI（1.5.3）只认 v64~v68，会报 `Trying to read a database file with version number 999`。
> 用 `src/sql.py` 走 Python 侧的 DuckDB，版本和项目里其他地方一致，不会有这个问题。

## CI

`.github/workflows/ci.yml` 在 **Linux 和 macOS 两个平台**上跑同一套流程（matrix 分开，`fail-fast: false`）：

1. **装环境**：`astral-sh/setup-uv` 按 `.python-version` 装 Python 3.13，`uv sync --locked` 按 `uv.lock` 装依赖 —— 锁文件跟 `pyproject.toml` 对不上就直接失败，不让它悄悄漂。
2. **lint**：`ruff format --check` + `ruff check`（跟本仓库的要求一致）。
3. **跑全部 demo**：`scripts/ci_demos.sh` 把 `src/` 下每个 demo 都跑一遍，任何一个非零退出码就判失败。
4. **存档**：每个 demo 的完整输出（`.out`，含性能表和逐值对账的结论）连同汇总表 `SUMMARY.md` 上传成 artifact，保留 14 天；汇总表同时贴到 CI 页面，不用下载 artifact 就看得到哪个 demo 挂了。

CI 里的规模是**特意调小**的（`--rows 200000 --repeat 3`）—— CI 要的是「跑通 + 存档」，不是测性能。想看真实数字在本地跑 `scripts/bench.sh 1000000 5`。

本地可以原样复现 CI 跑的每一行：

```bash
bash scripts/ci_demos.sh 200000 3 ci-out    # 输出目录默认 ci-out/
```

`ci_demos.sh` 负责把每个 demo 的规模开关对上（`--rows` / `--repeat` / `--repeats` / `--rounds` / `--files` / `--trades` / `--quotes` —— 每个 demo 叫的名字都不一样），并且**所有 demo 都会跑完**再汇总：这样 CI 挂掉时，artifact 里照样能看到其它 demo 的输出，而不只是第一个失败的那个。

两个平台各存各的 artifact（`demo-output-ubuntu-latest` / `demo-output-macos-latest`），别混着看 —— macOS 上没有 `/proc`，机器状态的读法不一样（`os.getloadavg()` / `ru_maxrss` / `os.sysconf`），报出来的负载数字跟 Linux 本来就不可比。

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

### 各 demo 自己的数据集

后面的 demo 各带一份自己的数据（都是脚本自己造的，重复跑同一个规模就直接复用）：

| 文件 | 规模 | 列 | 用来演示 |
| --- | --- | --- | --- |
| `scale_orders.parquet` / `scale_users.parquet` | 400 万订单 / 5 万用户 | 与 `orders`/`users` 同构 | 线程扩展性（量足够大才看得出线程差异） |
| `asof_trades.parquet` | 40 万笔成交 | `symbol`、`ts`、`qty` | ASOF JOIN |
| `asof_quotes.parquet` | 20 万条报价 | `symbol`、`ts`、`px` | ASOF JOIN（单独一份是刻意的：时间序列 join 的右表要能乱序，才能测出排序要求的坑） |
| `json_events.ndjson` | 20 万行 | `event_id`、`user_id`、`event_type`、`ts`、`amount`、`props`（含嵌套 STRUCT/LIST） | JSON 与嵌套类型 |
| `json_bigint.ndjson` | 3 行 | 超大整数 | JSON 大整数精度 |
| `index_customers_sorted_*.parquet` | 500 万行 | `customer_id`、`name`、`region`、`balance` | 索引与点查（**按主键排序**） |
| `index_customers_shuf_*.parquet` | 500 万行 | 同上（**整行随机置换**） | 索引与点查（对照组） |
| `decimal_amounts_1000000.parquet` | 100 万笔金额 | `id`、`amount`（最小单位 0.0001 元）、`amount_units`（整数最小单位，**精确答案的基准列**） | 金额与 Decimal |
| `write_demo/` | 每次运行产出（目录**不清空**，旧文件会越攒越多） | 各种写入产物 | 写入路径与事务（覆盖/原子替换/压缩） |
| `csv_demo/` | 每次运行覆盖同名文件 | 各种 CSV 样本（含坏行、超采样窗口的值） | CSV 读写与往返保真 |
| `tz_demo/` | 每次运行覆盖同名文件 | `duckdb_ts.parquet`、`polars_ts.parquet`（两边各写一份时间戳） | 日期时间与时区（Parquet 往返） |

`index_customers_*` 的文件名里带行数：换个 `--rows` 就是另一份文件，**永远不覆盖已有数据**。

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
| 区域 × 类目 Top-N | 59.4 | 30.5 | Polars 1.95× | ✅ |
| 每日收入趋势 | 44.8 | 27.3 | Polars 1.64× | ✅ |
| 高价值客户 Top 20 | 45.9 | 43.1 | Polars 1.07× | ✅ |

**场景 B：DuckDB 读物化后的 `demo.duckdb`，Polars 仍读 Parquet**

| 查询 | DuckDB(ms) | Polars(ms) | 更快 | 等价 |
| --- | --- | --- | --- | --- |
| 区域 × 类目 Top-N | 13.9 | 33.4 | DuckDB 2.40× | ✅ |
| 每日收入趋势 | 9.1 | 31.8 | DuckDB 3.49× | ✅ |
| 高价值客户 Top 20 | 13.0 | 56.1 | DuckDB 4.32× | ✅ |

**结论**：直接扫 Parquet 时 Polars 更快；但 DuckDB 读自己的存储格式快得多 —— **存储格式带来的差异（2.4~4.3×）大于引擎本身的差异（1.1~2.0×）**。选型时先决定存储格式，再谈用什么引擎。

另外注意：这个量级下两个引擎都在 10~60 ms，噪声足以让快慢关系在不同轮次翻转（Top 20 那条查询在多次运行里两边都赢过）。
两张表里的 **Polars 那一列是两次独立运行**测的同一个查询（读同一份 parquet），数字照样会跟着机器负载浮动 ——
同一轮里 Top 20 测出过 43 ms，也测出过 56 ms。**单次测量没有意义**，本项目默认重复 5 次取中位数，并保留 `--json` 便于做多轮统计。

## 语义差异（踩坑记录）

两个引擎默认行为并不一致，这是本项目最有价值的部分。以下每一条都在代码里跑过、对过账，
要么给出了显式对齐的写法，要么明确标出「没有对齐办法、只能靠约定避开」：

| # | 差异 | DuckDB | Polars | 对齐方式 |
| --- | --- | --- | --- | --- |
| 1 | 并列名次 | `RANK()` 取最小名次（1,2,2,4） | `rank()` 默认 `average`（1,2,2.5,4） | `rank(method="min")` |
| 2 | 整数求和类型 | `SUM(int32)` → HUGEINT（Arrow 里是 `decimal[38,0]`） | `sum()` 保持输入类型（Int32 → Int32） | 两边都转 `Int64` / `::BIGINT` |
| 3 | 计数类型 | `COUNT(*)` → BIGINT | `pl.len()` → UInt32 | 两边都转 Int64 |
| 4 | 窗口过滤 | 有 `QUALIFY` 子句 | 没有等价语法 | 先 `with_columns` 算窗口，再 `filter` |
| 5 | 浮点求和顺序 | 并行 + 向量化，顺序不保证 | 同左 | `SUM` 末位可能有 1e-9 级差异，校验用容差（`rtol=1e-9`）而非精确相等 |
| 6 | DuckDB 2.0 Python API | `con.execute(sql).arrow()` 已移除 | —— | 用 `con.sql(query, params).to_arrow_table()`；参数绑定受支持，且是 relation（每次读取都重新执行），不走 DB-API 就不会多一次行拷贝 |
| 7 | 存储格式版本 | 2.x 写 v999，1.5.3 的 CLI 只认 v64~v68 | —— | 探索数据走 `src/sql.py`（同一个 Python DuckDB），别指望老 CLI |
| 8 | NULL 排序位置 | 升序默认 NULLS **LAST** | 默认 NULLS **FIRST** | Polars `sort(nulls_last=True)` |
| 9 | 数值字面量类型 | `CASE WHEN … THEN 100.0` → **decimal[4,1]** | `pl.lit(100.0)` → Float64 | DuckDB 侧 `::DOUBLE` |
| 10 | 「Python UDF」的粒度 | `type='native'` **逐行**调用 | `map_batches` **按批**给数据 | DuckDB 用 `type='arrow'`，且函数体优先用 numpy |
| 11 | ASOF JOIN 的默认连接 | `ASOF JOIN` 是 **INNER**，匹配不上的行直接消失 | `join_asof` 是**左连接**语义，匹配不上给 null | DuckDB 侧写 `ASOF LEFT JOIN` |
| 12 | ASOF 并列时间戳 | 取右表物理顺序的**第一条** | backward 取命中点往前扫到的**最后一条** | 右表加确定性 tie-break 键，或保证 (symbol, ts) 唯一 |
| 13 | ASOF 输入有序性 | 不要求，引擎自己排 | 要求有序，但**带 `by` 分组时只警告不报错** | Polars 侧永远先 `sort(["symbol","ts"])` |
| 14 | ASOF 容忍窗口 | 没有 `tolerance` 参数 | 原生 `tolerance=` 参数 | DuckDB 侧用 `ASOF LEFT JOIN` + ON 里加不等式 |
| 15 | JSON 时间戳推断 | 认出 ISO-8601 → **TIMESTAMP** | 保持 **String** | Polars 侧 `str.to_datetime()` |
| 16 | JSON 超大整数 | 没有 UHUGEINT，退化成 **DOUBLE**（精度丢失） | 推断成 **Int128**，不丢 | 对账要拿原始 JSON 文本比，别比读进来的列 |
| 17 | 取嵌套字段 | `json_extract` 返回 **JSON** 类型、`json_value` 返回 **VARCHAR** | 只能整份 `str.json_decode(dtype=...)` | DuckDB 侧要数字得自己 `::DOUBLE` |
| 18 | 索引 | 有 ART 索引 | 没有索引 | Polars 靠 Parquet row group 统计剪枝 + 向量化过滤 |
| 19 | 执行计划里看不看得出用了索引 | `EXPLAIN` 只渲染 "Seq Scan"，要看真相得用 `EXPLAIN ANALYZE` | —— | 判断索引有没有生效只能看 `EXPLAIN ANALYZE` 的 Row Groups Scanned |
| 20 | `is_in` 的入参类型 | —— | 传**同 dtype** 的 `pl.Series` 会告警（2.1 废弃警告） | 用 `pl.Series(...).implode()` 或 Python list |
| 21 | 全 NULL 求和 | `SUM(v)` → **NULL** | `sum()` → **0** | 空集的和，两边定义不同；对账前先对齐 |
| 22 | `COUNT(DISTINCT)` 里的 NULL | **不计入** | `n_unique()` **计入** | Polars 用 `drop_nulls().n_unique()` |
| 23 | `NOT IN (…, NULL)` | 返回**空集** | `~is_in` 不这样 | DuckDB 侧改用 `NOT EXISTS` |
| 24 | 连接键为 NULL | 默认**不匹配** | 默认**不匹配** | `IS NOT DISTINCT FROM` / `nulls_equal=True` |
| 25 | 整数除以零 | **抛异常** | 给 **null** | DuckDB 侧用 `TRY(...)` |
| 26 | 浮点求和精度 | 朴素累加（和 Python 逐行累加**结果完全一样**） | 偏序合并，**明显更准** | 两边都别拿浮点算钱 |
| 27 | 数值溢出 | **抛异常**（Conversion / OutOfRange） | Float64 静默给 `inf` | Polars 的 Decimal 会报错，Float64 不会 |
| 28 | JOIN 的输出列 | `ON` 出**两个**同名列，`USING` 才合并成一个 | 永远一个；`how='right'` 把右表列放前面；`how='full'` / `how='cross'` 多一列 `k_right` | DuckDB 侧写 `USING (k)` |
| 29 | 连接键类型不一致 | 把字符串侧**偷偷转成整数**后继续连 | **SchemaError**，干脆不连 | 连接前先把两侧类型对齐 |
| 30 | 写同一个路径 | **静默覆盖** | **静默覆盖** | 两边一样：增量场景要自己防 |
| 31 | 覆盖已有文件的原子性 | 先写 `tmp_*` 再 rename（**原子替换**） | 直接写目标路径 | Polars 侧自己写临时文件再 rename |
| 32 | 事务 | 有 ACID（`BEGIN`/`ROLLBACK`/WAL/MVCC 快照隔离） | **没有事务这个概念** | Polars 侧靠「写临时文件 + 原子替换」 |
| 33 | 默认压缩 | SNAPPY | **ZSTD**（同一份数据小 19%） | 显式指定 `COMPRESSION ZSTD` / `compression='snappy'` |
| 34 | CSV 里的日期 | **自动识别**成 DATE / TIMESTAMP | 默认当 **String** | Polars 开 `try_parse_dates=True` |
| 35 | CSV 里的前导零 | 保守地当 **字符串** | 推成**整数**，前导零没了 | Polars 用 `schema_overrides` |
| 36 | CSV 采样窗口外的类型冲突 | **静默改数**（1.5 被读成 2） | **直接报错** | DuckDB `sample_size=-1` / Polars `infer_schema_length=None` |
| 37 | naive 与 aware 直接比较 | 按**会话时区**静默补齐，答案随时区变 | **SchemaError** | 报错的那一边更安全 |
| 38 | 夏令时的空档 / 歧义时刻 | **静默选一个**（往后挪 1 小时 / 选 EST） | 默认**报错** | Polars 用 `ambiguous=` / `non_existent=` |
| 39 | 「贴标签」与「换显示」 | 都叫 `AT TIME ZONE`，靠输入类型区分方向 | `replace_time_zone` / `convert_time_zone` | 照名字翻译必错 |
| 40 | 截断 vs 四舍五入 | `time_bucket` 是**向下取** | `dt.truncate` 向下取，**`dt.round` 是四舍五入** | 别把 `dt.round` 当 `time_bucket` |
| 41 | 星期几编号 | `dayofweek` 0=周日 | `dt.weekday()` 1=周一（ISO） | 用 DuckDB 的 `isodow` 对齐 |
| 42 | 排序稳定性 | 并行 `ORDER BY` **不保证稳定**（2 线程起实测会变） | `sort` 默认 `maintain_order=False` | DuckDB 加唯一 tiebreaker；Polars 开 `maintain_order=True` |
| 43 | 多文件类型不一致 | 自动提升（int64 + float64 → DOUBLE） | **SchemaError** | Polars 用 `cast_options` 放行 |
| 44 | 多文件列名不一致 | 默认报错，`union_by_name=true` 能并 | `extra_columns='ignore'` + `missing_columns='insert'` | 两个开关要**一起**开 |
| 45 | Hive 分区目录 | 传 glob **自动认**分区列 | 传 glob **不认**，得传目录或 `hive_partitioning=True` | 路径写法决定结果里有没有分区列 |
| 46 | 分区值当字符串过滤 | 隐式转换掉，不报错 | **报类型错** | Polars 用 `pl.lit(date(...))` |
| 47 | 写分区 | **不把分区列写进文件**（读时从路径补） | **把分区列留在文件里** | 两边目录名都是 hive 写法，能互相读 |
| 48 | 分支求值（`CASE WHEN` / `when/then`） | **只算命中的那个分支** | **每个分支都算一遍**，最后才挑对的那条 | 结果两边一致，差的是代价；分支越贵越吃亏（Polars 没有开关能关） |
| 49 | 窗口默认 frame | 不带 frame 默认 **RANGE**（并列行一起进窗口） | `cum_sum()` 是 **ROWS** 口径 | SQL 里显式写 `ROWS BETWEEN …` |
| 50 | 窗口默认 frame 随排序键个数变 | `ORDER BY x` 是 RANGE；`ORDER BY x, ord` 退化成 **ROWS**（RANGE 配多键直接报错） | 只有 ROWS 口径 | 别依赖默认值，显式写 frame |
| 51 | frame 家族 | ROWS / RANGE / **GROUPS** | 只有 ROWS 口径的表达式 | RANGE / GROUPS 得自己 group_by + join 拼 |
| 52 | 命名窗口 | `WINDOW w AS (…)` 可复用 | 没有 | Polars 把 `over(...)` 抄两遍 |
| 53 | 分位数插值 | `quantile_cont` = linear；`quantile_disc` = 取实际值 | `quantile()` 默认 **nearest / higher** | Polars 显式 `interpolation="linear"` |
| 54 | 分位数跟着列类型走 | DECIMAL 列上插值结果被**截断**回精度（1.75 → 1.7） | 不受此影响 | 两边都转 DOUBLE 再对账 |
| 55 | 众数 | `mode()` 返回**一个**标量 | `mode()` 返回**所有**并列众数（顺序还不保证） | Polars `.mode().first()` |
| 56 | arg_max 并列 | 碰到第一个就返回（**不保证**） | `get(arg_max)` 同口径（**也不保证**） | 两边都自己加唯一的 tie-break 键 |
| 57 | 字符串聚合顺序 | `string_agg` 不带 ORDER BY **不保证顺序** | `implode()` 保证输入顺序 | DuckDB 侧写 `ORDER BY`，或两边都排序 |
| 58 | 整数求和溢出 | BIGINT 的 sum 升到 **HUGEINT** | 保持 Int64，**静默回绕**成负数 | Polars 侧先转 Int128 / Decimal |
| 59 | entropy | 以 2 为底的**标准香农熵** | 同名函数算的是别的东西（随数值变，常数列也不为 0） | 两个同名函数不可互换 |
| 60 | 集合运算里的 NULL | `EXCEPT`/`INTERSECT` 里 **NULL 相等** | **没有** difference / intersection | Polars 用 `join(..., nulls_equal=True)` |
| 61 | 去重保留哪一行 | `DISTINCT ON` 依赖外层 ORDER BY，没写就任意 | `unique(subset=, keep="first")` 取当前行序第一行 | 先 sort 再 unique |
| 62 | 正则不匹配时的表示 | `regexp_extract` 返回**空串** | `str.extract` 返回 **null** | 判空要按各自的写法写 |
| 63 | 整串匹配 | `regexp_full_match` | **没有** `str.fullmatch` | Polars 侧加 `^` `$` 锚定 |
| 64 | 正则切分的默认值 | `string_split`（字面量）/ `regexp_split_to_array`（正则）分开 | `str.split` 默认 **literal=True** | Polars 侧显式 `literal=False` |
| 65 | 正则位置查找 | `strpos` 只认**字面量**，没有 `regexp_instr` | `str.find` 走正则，返回**字节偏移 0 起数** | 位置口径不同（字符 1 起 vs 字节 0 起），别直接相减 |
| 66 | LIKE | 有 `LIKE` / `ILIKE` | **没有** LIKE，只能用正则 | Polars 用正则 + `(?i)` 模拟 ILIKE |
| 67 | 改数据 | `UPDATE`/`DELETE`/`INSERT` **原地**改 | DataFrame **不可变**，每次返回新对象 | Polars 侧自己拼 join / concat |
| 68 | Polars 的 update | —— | `DataFrame.update` **unstable**，重复键会**复制行** | 别在生产代码里依赖它 |
| 69 | upsert | `ON CONFLICT DO UPDATE` / `DO NOTHING` | 没有 | Polars 侧 anti join + coalesce 手工拼 |
| 70 | 子查询更新 | 按**更新前的快照**算一次（无 Halloween 问题） | 纯表达式，天然没有 | —— |
| 71 | RETURNING / 事务 | 有 `RETURNING`、`BEGIN`/`COMMIT`/`ROLLBACK` | **没有事务这个概念** | Polars 侧自己套「临时文件 + 原子替换」 |
| 72 | 改表结构 | 完整 `ALTER TABLE`（加/删/改名/改类型） | 没有，改 schema = 造新 DataFrame | —— |
| 73 | 标识符大小写 | **不敏感**（`SELECT ID` 查得到 `id`） | **敏感** | 两边各记一份字段名 |
| 74 | 类型转换失败 | 默认**报错**，`TRY_CAST` 才放宽 | 默认**报错**，`cast(strict=False)` 才放宽 | 两边默认都是报错（这是好事） |
| 75 | 读 schema 不一致的 parquet | 少列**报错**；多列**静默丢掉** | 两种都报错 | DuckDB `union_by_name=true` / Polars `missing_columns='insert'` |
| 76 | 拼表 | `UNION ALL` 自动提升类型 | `concat` 默认**严格** | Polars 用 `how="diagonal_relaxed"` |
| 77 | 序列端点 | `generate_series` **含**终点；`range` 不含 | `int_range` 不含；`date_range` **含** | `int_range(1, 6)` 才等于 `generate_series(1, 5, 1)` |
| 78 | generate_series 的时间类型 | 起点是 DATE 时结果是 **TIMESTAMP** | `date_range` 返回 **Date** | —— |
| 79 | 补齐缺失时间段 | 自己生成完整轴 + LEFT JOIN | `upsample` **只补中间**，不补两端 | Polars 侧要补两端得自己 join |
| 80 | 按月截断 | `date_trunc('month', …)` 清零时分秒 | `dt.truncate('1mo')` 同上，但 `dt.month_start()` **保留时分秒** | 别拿 month_start 当 truncate |
| 81 | 加一个月 | `INTERVAL 1 MONTH` 就是日历月 | `pl.duration()` **没有 months** | Polars 侧用 `dt.offset_by("1mo")` |
| 82 | 近似 distinct | `approx_count_distinct`（HLL），本轮**偏低** | `approx_n_unique`（HLL），本轮**偏高** | 误差方向不固定，别比大小 |
| 83 | 近似分位数 | `approx_quantile`（t-digest） | `approx_quantile`（KLL / REQ，可选算法） | 随机 sketch，每次运行结果都不同 |
| 84 | 负数整除与取模 | `//` **向零截断**（`-7//3` = -2）；`%` 是**截断余数**（`-7%3` = -1），跟 C 一致 | `//` **向下取整**（`-7//3` = -3）；`%` 是 **floor mod**（`-7%3` = 2），跟 Python 一致 | 两边各自对齐了不同的「事实标准」，负数参与时结果**不是差 1 的关系** |
| 85 | 除法不是整除 | `7/2` = **3.5**（`/` 永远浮点，`typeof` 是 DOUBLE） | `7/2` = 3.5 | 两边一致；但 DuckDB **不**像 Postgres 那样整除，要整数除法得写 `//` |
| 86 | 舍入规则 | `round` **四舍五入远离零**（`round(2.5)` = 3，`round(-2.5)` = -3），对齐 Excel | `round` **银行家舍入**（`round(2.5)` = 2，`round(-2.5)` = -2），对齐 Python | 账单场景必须显式选一边，差 1 分就是差 1 分 |
| 87 | 整数溢出 | **报错** `OutOfRangeException: Overflow in addition of INT32` | **静默绕回**（Int32 最大值 + 1 = -2147483648） | Polars 侧改用 Decimal，或在写库前自己检查量级 |
| 88 | 整数除零 | **报错** `InvalidInputException`（`try()` / `SET error_on_division_by_zero=false` 可变 NULL） | 返回 **NULL**，不报错 | DuckDB 当场炸、Polars 安静传播 —— 迁移时最容易漏的一类 |
| 89 | 截断取整 | 有 `trunc(x)` / `trunc(x, n)` | **没有**同名函数 | `cast(pl.Int64)`（向零截断）或 `floor()`（向下），负数上两者不同 |
| 90 | NaN 的比较 | `nan = nan` 为 True，`nan > 1` 为 True | 同左 | 两边一致，但都**不是** IEEE 语义（IEEE 说 NaN ≠ NaN） |
| 91 | 多维小计 | `ROLLUP` / `CUBE` / `GROUPING SETS` 全支持 | **三个都没有** | Polars 侧只能两次 `group_by` + `concat` 手工拼 |
| 92 | 小计的占位 NULL | `GROUPING(g)` 能区分「占位」和「真 NULL」 | **没有 `GROUPING()`** | 信息在拼 `concat` 时就丢了，只能自己加一列 `level` 标记 |
| 93 | PIVOT 的缺失组合 | 填 **NULL** | `sum` 填 **0**、`max` 填 NULL；**没有 `fill_value` 参数** | 语义不同：`WHERE 服饰 IS NOT NULL` 两边给出不同的地区集合 |
| 94 | PIVOT 的行序 | **不保证**（实测连跑 20 次出现 20 种顺序） | `maintain_order=True`，稳定 | DuckDB 侧对账前必须先 `ORDER BY` |
| 95 | 多列 PIVOT 的列名 | `a_x`（下划线拼接） | `{"a","x"}`（元组字符串）；`column_naming='combine'` 是 `v_{"a","x"}` | 只能自己 `rename` |
| 96 | UNPIVOT 的 NULL | 简化语法**丢 NULL**（12 格 → 7 行）；`UNPIVOT INCLUDE NULLS` 才留 | **永远全留** | DuckDB 侧先看用的是哪种语法 |
| 97 | `ignore_errors` | **丢整行**（实测 8 行物理数据只剩 7 行） | **保留行、只把坏字段置 NULL**（仍是 8 行） | Polars 侧要再丢一次空行才能对上 |
| 98 | 坏行记账 | `store_rejects=true` + `reject_errors` 表（行号 / 列名 / 原始行 / 错误类型） | **没有** | 想记账只能用 DuckDB |
| 99 | 脏 CSV 的类型推断 | 嗅探到脏值会**把整列降成 VARCHAR**，不报错 | 同样降成 String，不报错 | 两边一致但危险 —— 显式给 schema |
| 100 | 嵌套类型的构造方式 | **字面量**（`SELECT [1,2,3]`），类型由值推出来 | **dtype 声明**（`dtype=pl.List(pl.Int64)`） | 少写 dtype → Polars 把它当 Struct，`.map.keys()` 全不能用 |
| 101 | MAP 的 dtype | `MAP{...}` 字面量，一定是 map | 光给 list of dict **不够**，必须 `dtype=pl.Map(...)` | 漏写就是 Struct，`.map.*` 全部报 InvalidOperationError |
| 102 | 空列表求和 | `list_sum([])` → **NULL** | `list.sum()` → **0** | 差一个值不是差一个数：DuckDB 给的是 NULL，聚合时污染整列 |
| 103 | 列表里的 NULL 成员判定 | `list_contains([1,NULL,3], NULL)` → **NULL** | `list.contains(None)` → **True** | 三值逻辑 vs 布尔；Polars 把 UNKNOWN 压成了 True |
| 104 | lambda 语法 | DuckDB 2.0 **废弃 `x ->`**，必须 `lambda x: x*2`，否则 binder 报错 | 没有 lambda，对应物是 `pl.element()` | 1.x 迁 2.0 是**编译期就炸**；逃生口 `SET lambda_syntax='ENABLE_SINGLE_ARROW'` |
| 105 | 列表归约函数 | 有 `list_reduce` / `list_position` | **两个都没有** | 自己 fold，或者退回 `explode` 之后用普通算子 |
| 106 | EXPLAIN 默认给什么 | **物理计划**（盒式图，默认 `physical_only`） | **优化后**的算子树（缩进树，`optimized=True` 是默认） | Polars 想看优化前要显式 `optimized=False`；DuckDB 要改 `PRAGMA explain_output` |
| 107 | EXPLAIN 会不会执行 | `EXPLAIN` 不执行，`EXPLAIN ANALYZE` **会执行** | `explain()` 不执行 | 大表上别随手 ANALYZE —— 它是真的把查询跑一遍 |
| 108 | 运行时指标 | profiling JSON 有 `io.total_bytes_read`（实测查 2 列 = 查 12 列的 **7.1%**） | **一个都没有** | Polars 只能自己套 `time.perf_counter()`，拿不到引擎内部数字 |
| 109 | 算子的算法选择 | 计划里写明 `Perfect Hash Group By` / `Hash Join` / `Piecewise Merge Join` | 永远只写 `AGGREGATE` / `INNER JOIN` | 「会不会退化」这个问题，只有 DuckDB 的计划能回答 |
| 110 | 逐节点计时 | —— | `profile()` 在 2.0 **已移除**；`show_graph()` 还要 matplotlib | Polars 侧能看的只有 `explain()` 那几行文本 |
| 111 | 默认采样方法 | `USING SAMPLE` 默认 **system（块采样）** | `sample()` 是 **reservoir** 口径 | 小表上 DuckDB 只给 **0 行或全量**，中间值一个都不出现 |
| 112 | 采样行数准不准 | system 波动大（10 万行请求 10%，实测 **4,096 ~ 20,480**）；reservoir **精确命中** | 精确命中 | 想要确定行数只能写 `reservoir`，别用默认值 |
| 113 | 采样 + 离散行数 | `bernoulli(100 ROWS)` → **ParserException** | `sample(n=100)` 正常 | bernoulli 只能按比例；报错原文里直接点名了替代方案 |
| 114 | 随机种子 | `setseed()` 只管 `random()`，**管不住采样** | `seed=` 跟着调用走 | DuckDB 采样要复现得写 `USING SAMPLE 10% (bernoulli, 42)` |
| 115 | 随机函数 | 只有 `random()`，**没有 `rand()` 这个别名** | **一个都没有**（只能退回 numpy） | 随机数从「引擎内部」搬到了「Python 进程里」，能做的事少一截 |
| 116 | 约束 | PK / UNIQUE / NOT NULL / CHECK / FK **五种全支持**，写入时引擎强制执行 | **零个 assert API**，只有 `is_duplicated` / `null_count` / `n_unique` 这些原语 | 迁移时最危险的不是 API 对不上，是「少写一行，坏数据静默进去」 |
| 117 | 约束的持久性 | 写在 `.duckdb` 文件里，**换进程换连接都还在** | 检查是**一次函数调用**，这次调了下次忘了就凭空消失 | DuckDB 一次投入长期生效；Polars 每个写入点都要重复投入 |
| 118 | 序列 | `CREATE SEQUENCE` + `nextval()`，跨连接全局唯一，能当列默认值 | **没有序列**；`with_row_index()` 只是**当批次的行号**（从 0 开始、每次重数） | 重跑一次批次 id 全变 → 外键、增量同步、幂等写入全乱 |
| 119 | `NOT IN` 遇 NULL | **一行都不剩**（三值逻辑 UNKNOWN，WHERE 只放行 TRUE） | `~is_in()` 正常返回（UNKNOWN 被压成 FALSE） | `NOT IN (子查询)` **一律改 `NOT EXISTS`**；加 `IS NOT NULL` 补救无效（NULL 在右表） |
| 120 | 成员判定的三值逻辑 | IN 在 SELECT 列表里给 TRUE / FALSE / **NULL** | 只给 TRUE / FALSE | 需要 UNKNOWN 得自己补 —— Polars 侧没有现成的三值逻辑 |
| 121 | 计划里的连接类型 | `EXISTS`→SEMI、`NOT EXISTS`→ANTI、`IN` 在 WHERE→SEMI、在 SELECT 列表→**MARK** | 没有对应物 | 同一个 `IN` 放 WHERE 和放 SELECT 列表，**改写结果不一样** |
| 122 | 递归 | `WITH RECURSIVE` 一句 SQL | **没有**，只能逐层 join 或 `map_elements` | `map_elements` 版的 depth 是 **Int64**，DuckDB 递归 CTE 是 **Int32** |
| 123 | 递归的收敛 | `UNION`（去重）= 求不动点，**会停**；`UNION ALL` 不会停 | 循环自己数轮数 | 从 Postgres 抄过来默认是 `UNION`；**抄反了的后果不是结果错，是跑不完** |
| 124 | 递归的迭代上限 | **没有内置上限**（`max_expression_depth` 跟这事无关） | 循环是自己的，`range(1000)` / `break` 都是现成护栏 | DuckDB 侧必须自己在递归项里写 `WHERE depth < N` |
| 125 | 命名持久对象 | 视图 / 宏 / 序列 / 触发器都是**数据库级**，写进 `.duckdb` 文件 | **没有「命名对象」这个概念** | Polars 的 LazyFrame 只是个变量名，不是对象名 |
| 126 | 视图存的是什么 | 存 **SQL 文本**（不是结果），EXPLAIN 里**没有 VIEW 节点** | 存**执行计划** | 两边都是「配方」，但 DuckDB 的配方能落盘，Polars 的不能 |
| 127 | 物化视图 | **没有** `CREATE MATERIALIZED VIEW`（ParserException 在 `MATERIALIZED` 这个 token 上） | 没有（只能 `write_parquet`） | 替代品是 CTAS 或定时覆盖写 parquet，刷新逻辑自己写 |
| 128 | 触发器 | **有**，但只实现了子集：`AFTER ... FOR EACH STATEMENT` 可用，`BEFORE ... FOR EACH ROW` → NotImplementedException | **没有** | 「DuckDB 不支持触发器」这句话在 2.0 上不准确 —— 是「只实现了子集」 |
| 129 | Python UDF 的持久性 | **连接级**，重连后 `CatalogException`（宏还在，UDF 没了） | 进程内的普通函数 | 跨进程要活就用宏；UDF 的真正代价是 DuckDB 对它**逐行**调用 |
| 130 | 内存上限 | `SET memory_limit='50MB'`（回显成 **47.6 MiB**，DuckDB 认十进制） | **没有任何旋钮**（`pl.Config` 里带 mem 的设置项 = `[]`） | Polars 侧得自己套 cgroup / 容器限额 |
| 131 | 溢写落盘 | `SET temp_directory='...'`，超了自动落盘（临时文件**用完即删**） | 没有 | Polars 只能靠流式引擎**降低峰值**，但降不了上限 |
| 132 | 超限时的失败方式 | `OutOfMemoryException`（进程内可捕获，报错里带解法，**连接还能接着用**） | **没有内存异常类**，真不够时 Rust runtime abort 整个进程 | 「跑一半被 kill」在生产环境里最难查 |
| 133 | 线程数与内存上限 | `memory_limit` 是全局预算，但**每线程各自持有缓冲区** → 8 线程反而 OOM，4 线程能跑完 | —— | 内存不够先调 `SET threads`，不是先调大上限（DuckDB 自己的报错第一条建议就是这个） |
| 134 | `memory_limit` 的作用域 | **连接级**，新连接完全不受影响（连文件库重开也不记得） | —— | 同一个 Python 进程 `duckdb.connect()` 两次 = 两个互不相关的世界 |

> 第 6 条的坑：`con.execute(sql)` 在 2.0 里返回的是连接对象而不是结果，`.arrow()` 已经不存在；退而用 `con.to_arrow_table()` 会警告 "makes an extra copy of the rows" —— 那次拷贝会让基准测试不公平地惩罚 DuckDB。

第 8~10 条在下面三节里展开：`方言差异专场`、`流式处理与内存`、`自定义函数（UDF）`；
第 11~14 条在 `ASOF JOIN（时间序列 join）`、第 15~17 条在 `JSON 与嵌套类型`、第 18~20 条在 `索引与点查` 里展开；
第 21~25 条在 `NULL 与三值逻辑`、第 26~27 条在 `金额与 Decimal`、第 28~29 条在 `JOIN 全景`、
第 30~33 条在 `写入路径与事务`、第 34~36 条在 `CSV 读写与往返保真`、第 37~41 条在 `日期时间与时区`、
第 42 条在 `排序稳定性`、第 43~47 条在 `多文件与分区表`、第 48 条在 `自定义函数（UDF）` 里展开。
第 49~83 条在 `窗口函数`、`聚合函数语义`、`集合运算与去重`、`正则与字符串`、`增删改（DML）`、
`Schema 演进`、`时间轴与序列生成`、`近似计算 vs 精确` 里展开；
第 84~90 条在 `数值与算术边界` 里展开；第 91~92 条在 `多维聚合：GROUPING SETS / ROLLUP / CUBE`、
第 93~96 条在 `透视与重塑：PIVOT / UNPIVOT`、第 97~99 条在 `容错模式与坏数据` 里展开；
第 100~105 条在 `嵌套类型：LIST / STRUCT / MAP`、第 106~110 条在 `查询计划：EXPLAIN 与投影裁剪`、
第 111~115 条在 `采样与随机`、第 116~118 条在 `约束与完整性`、
第 119~121 条在 `存在性查询与半连接`、第 122~124 条在 `递归 CTE 与迭代计算`、
第 125~129 条在 `视图、宏与持久化对象`、第 130~134 条在 `内存上限与落盘` 里展开。

两边都能把过滤和投影下推到 Parquet 扫描，用 `--explain` 可以直接看到：

- DuckDB：`Table Scan → Filters: (order_ts >= …) AND NOT is_returned`，且 Hash Join 会生成 `Dynamic Filters` 反向优化扫描
- Polars：`Parquet SCAN → SELECTION: (col("is_returned").not() & (…))`，投影从 10 列裁到 7 列

## 互操作：两个引擎能通用到什么程度

`uv run python src/interop.py` 把这件事拆成 6 步跑一遍。结论：

| 层面 | 通用的部分 | 不通用的部分 |
| --- | --- | --- |
| 磁盘格式 | Parquet / CSV / JSON / Arrow IPC —— 两边都能读 | `demo.duckdb` 只有 DuckDB 能开；DuckDB 还能读 SQLite/Postgres/S3 等，Polars 有 connectorx 等，两边的独占能力不重叠 |
| 内存格式 | **Arrow** —— Polars 的内部模型就是 Arrow，DuckDB 的结果集能直接物化成 Arrow Table | 无 |
| 引擎私有对象 | 能互相转换：DuckDB→Arrow→Polars，Polars→`con.register()`→DuckDB 表 | 直接用 SQL 查对方的 DataFrame 需要 DuckDB 的 `polars` 扩展（联网下载，本项目默认不装） |

实测要点：

- 两个引擎读同一个 `data/orders.parquet`，schema 一致、`equals()` 为 True —— 这是「比谁快」能站得住脚的前提
- DuckDB → Arrow → Polars 转换后，**Arrow 底层 buffer 地址不变**（脚本里直接比对了地址），没有 Python 层的行拷贝
- 接力流水线成立：DuckDB 做谓词下推 + 并行聚合 → Arrow → Polars 算窗口函数收尾，结果与纯 DuckDB、纯 Polars 三者逐值一致 ✅

```python
# 两个方向都成立
pl.from_arrow(con.sql("SELECT ...").to_arrow_table())  # DuckDB -> Polars
con.register("t", polars_df)  # Polars -> DuckDB
```

但**格式通用 ≠ 语义通用**：默认类型和名次规则仍要显式对齐（见上面第 2、3、7 条）。选型时的实用结论是：想让两个引擎都能碰的数据存 Parquet 最安全；引擎私有的 `.duckdb` 一旦落盘就把引擎锁死了。

## 方言差异专场

`uv run python src/dialect_demo.py` 用同一份 6 行小表（故意含 NULL 和 NaN），并排跑 8 个场景。
这些差异**不会报错**，只会静默地算错 —— 正是最容易踩的坑。

| # | 场景 | DuckDB | Polars | 对齐方式 |
| --- | --- | --- | --- | --- |
| 1 | NULL 排序位置 | 升序默认 NULLS **LAST** | 默认 NULLS **FIRST** | Polars `sort(nulls_last=True)` |
| 2 | NaN vs NULL | NaN 不是 NULL，且**污染 SUM** | 同左 | 两边都用 `isnan()` / `is_not_nan()` 过滤 |
| 3 | 日期截断单位 | `date_trunc('month', …)` | `dt.truncate('1mo')` | 单位名不同；`'1month'` 会报错，`'m'` 是**分钟**不是月 |
| 4 | 正则分组编号 | `regexp_extract(s, p, 1)` | `str.extract(p, 1)` | 编号口径一致（1 = 第一个括号组） |
| 5 | 透视表缺失组合 | 填 **NULL** | 填 **0**，且 NULL 类别会造出一个叫 `"null"` 的列 | Polars 侧 `fill_null(0)` |
| 6 | 按组取第一条 | `DISTINCT ON (region)` | 无等价语法 | `sort` + `unique(subset=, keep="first")` |
| 7 | 递归 CTE | `WITH RECURSIVE` 支持 | **不支持** | 只能用 `int_range` 展开，或做不到 |
| 8 | 改数据 | `UPDATE` / `DELETE` 原地改 | 无原地修改 | `with_columns` + `filter` 返回新表 |
| 9 | 窗口默认 frame | `ORDER BY x` 不带 frame 默认 RANGE | `cum_sum()` 是 ROWS 口径 | SQL 显式写 `ROWS BETWEEN …` |
| 10 | 集合运算里的 NULL | `EXCEPT`/`INTERSECT` 中 NULL 相等 | 无对应函数 | `join(..., nulls_equal=True)` |
| 11 | 序列端点 | `generate_series` 含 / `range` 不含 | `int_range` 不含 / `date_range` 含 | `int_range(1, 6)` |
| 12 | 正则切分的默认值 | `string_split` 字面量 | `str.split` 默认字面量 | Polars 显式 `literal=False` |
| 13 | 正则替换 | `regexp_replace` 默认只换第一个（`'g'` 全局） | `str.replace` 默认只换第一个（`replace_all` 全局） | 两边默认一致 |
| 14 | 字符串聚合顺序 | `string_agg` 无 ORDER BY 时不保证 | `implode()` 保证输入顺序 | DuckDB 加 `ORDER BY` |
| 15 | 整数求和 | BIGINT sum → HUGEINT | Int64 sum 溢出静默回绕 | 先转 Int128 / Decimal |
| 16 | 分位数插值 | `quantile_cont` = linear | `quantile()` 默认 nearest | `interpolation="linear"` |
| 17 | 近似 distinct | HLL | HLL | 误差方向不固定 |
| 18 | 多维小计 | `ROLLUP` / `CUBE` / `GROUPING SETS` | 三个都没有 | 两次 `group_by` + `concat` 手工拼 |
| 19 | 小计的占位 NULL | `GROUPING()` 可区分占位与真 NULL | 没有 | 自己加一列 `level` 标记 |
| 20 | PIVOT 的缺失组合 | 填 **NULL** | 填 **0**（`sum`） | 语义不同，`IS NOT NULL` 过滤结果不同 |
| 21 | PIVOT 的行序 | 不保证 | 保证 | DuckDB 侧 `ORDER BY` |
| 22 | UNPIVOT 的 NULL | 简化语法丢 / `INCLUDE NULLS` 留 | 全留 | DuckDB 侧用 `INCLUDE NULLS` |
| 23 | `ignore_errors` | 丢整行 | 坏字段置 NULL，行还在 | Polars 侧再丢一次空行 |
| 24 | 坏行记账 | `reject_errors` 表 | 没有 | —— |
| 25 | `NOT IN` 的 NULL | 右表有一个 NULL 就**返回空集** | `~is_in()` 正常返回 | 改成 `NOT EXISTS` |
| 26 | 约束 | PK / UNIQUE / NOT NULL / CHECK / FK **五种全支持** | **零个 assert API** | 自己用 `is_duplicated` 等原语拼 |
| 27 | 序列 | `CREATE SEQUENCE` + `nextval()` | 没有 | 手工维护 offset（一致性没引擎兜底） |
| 28 | 递归 | `WITH RECURSIVE` | 没有 | 逐层 join / `map_elements` |
| 29 | 命名持久对象 | 视图 / 宏 / 序列 / 触发器都是**数据库级** | **没有这个概念** | —— |
| 30 | 物化视图 | **没有** `CREATE MATERIALIZED VIEW` | 没有 | CTAS 或定时覆盖写 parquet |
| 31 | 内存上限 | `SET memory_limit='50MB'` | **没有** | 自己套 cgroup / 容器限额 |
| 32 | 默认采样 | **system（块采样）**，小表上非 0 即全 | reservoir，精确命中 | DuckDB 侧显式写 `reservoir` |
| 33 | 随机函数 | `random()`（**没有 `rand()`**） | **一个都没有** | 退回 numpy |
| 34 | lambda | 2.0 废弃 `x ->`，必须 `lambda x:` | 没有 lambda | 对应物是 `pl.element()` |
| 35 | EXPLAIN 默认给什么 | **物理计划** | **优化后**的算子树 | —— |
| 36 | 流式 collect | —— | `streaming=True` 在 2.0 **已移除** | `collect(engine="streaming")` |

第 2 条还有个细节：Polars 的 `is_nan()` 对 NULL 返回 `null`（不是 `false`），别拿它直接当条件用。

## 流式处理与内存

`uv run python src/streaming_demo.py` 把同一件事在两个数据规模上各跑一遍，量「内存里同时驻留了多少数据」。

实测（400 万行，本机）：

| 方式 | 引擎 | 100 万行 | 400 万行 | 倍数 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 物化 | DuckDB | 27.1 MB | 108.2 MB | **4.00×** | O(n) |
| 物化 | Polars | 19.1 MB | 76.3 MB | **4.00×** | O(n) |
| 流式 | DuckDB | 3.3 MB | 2.7 MB | 0.81× | O(1) |
| 流式 | Polars | 1.9 MB | 1.9 MB | 1.00× | O(1) |

数据翻 4 倍，物化的驻留量精确地翻 4 倍，流式的几乎不动 —— 这就是「有没有物化」的区别。
四种做法的 `SUM(revenue)` 逐值一致 ✅。

两个引擎的流式写出（DuckDB `COPY (…) TO` / Polars `sink_parquet()`）产出的文件也一致 ✅。

> **量内存的两个坑**（脚本注释里也写了）：
> 1. Linux 上子进程的 `ru_maxrss` 会**继承父进程的峰值** —— vfork/posix_spawn 短暂共享父进程的 `mm`，
>    `signal->maxrss` 会带着父进程的历史峰值；写 `/proc/self/clear_refs` 也重置不掉。
> 2. RSS **差值**会被分配器 arena 的历史高水位污染，跨进程不可比（DuckDB 的 buffer manager、
>    Polars 的 arena 各有各的脾气，实测能出现「数据更多、内存反而更少」）。
>
> 所以主指标用的是**对象自己报的字节数**（Arrow Table 的 `nbytes`、Polars 的 `estimated_size()`），
> 确定性可复现；RSS 只作为参考值附在旁边。

## 自定义函数（UDF）

`uv run python src/udf_demo.py` 用同一个自定义规则，两边各写几种，量「算这条规则」的代价（输入已物化，不含扫描 Parquet 的时间）。

实测（**100 万行** = 脚本默认数据量，本机，两组规则）：

| 引擎 | 写法 | 实现细节 | 轻量规则 | 计算密集规则 |
| --- | --- | --- | --- | --- |
| DuckDB | 原生 SQL | `CASE WHEN` | **23.8 ms** | **14.3 ms** |
| DuckDB | Python UDF | `type='native'`（**逐行**） | 310 ms | 335 ms |
| DuckDB | Python UDF | `type='arrow'`（批）+ pyarrow.compute | 566 ms | 1027 ms |
| DuckDB | Python UDF | `type='arrow'`（批）+ **numpy** | 127 ms | 130 ms |
| Polars | 原生表达式 | `when/then` | **8.1 ms** | 60 ms |
| Polars | `map_batches` | 批内**逐行** Python | 189 ms | 286 ms |
| Polars | `map_batches` | 批内**向量化** | 9.2 ms | 61 ms |

七种写法结果逐值一致 ✅。同一份脚本连跑几次，这张表里**原生那两行浮动最大**
（DuckDB 原生 SQL 在 14~32 ms 之间跳、Polars 原生表达式在 6~10 ms 之间跳 —— 机器是共享的），
UDF 那几行很稳。

五个反直觉的发现：

1. **同样叫「Python UDF」，两个引擎的默认粒度不一样。** DuckDB 的 `type='native'` 是**逐行**调用
   （每行跨一次 Python/C 边界），Polars 的 `map_batches` 拿到的**是整批**。
2. **DuckDB 的 arrow UDF 是批式接口，但函数体怎么写决定成败** —— 同一批数据，
   numpy 函数体比 pyarrow.compute 函数体快 **4×（轻量）~ 8×（计算密集）**
   （多次运行落在 3×~8×）。原因是 `pc.if_else` 在 ChunkedArray 上做标量广播很慢，
   numpy 走的是连续内存。
3. **批式接口不一定比逐行快。** 轻量规则下，用 pyarrow.compute 写的 arrow UDF（566 ms）
   甚至比逐行 UDF（310 ms）还慢。
4. **「原生」的赢家随规则变。** 轻量规则 Polars 原生表达式更快（8.1 ms vs 23.8 ms），
   计算密集规则反过来 DuckDB 原生 SQL 更快（14.3 ms vs 60 ms）—— 原因见下一条。
5. **分支求值：两个引擎选对分支的结果一样，但算的遍数不一样。**
   构造一条规则，每个分支里都塞一次 `power()`（脚本第 3 部分实测）：

   | 引擎 | 单分支 | 四分支 | 四分支 ÷ 单分支 |
   | --- | --- | --- | --- |
   | DuckDB `CASE WHEN` | 8.1 ms | 11.5 ms | **1.4×** |
   | Polars `when/then` | 19.7 ms | 99.6 ms | **5.1×** |

   DuckDB 的四分支几乎没变贵（真按四遍算的话，比例应该 ≥ 4×）—— 它按行选分支；
   Polars 那边四个分支**全算了一遍**才挑对的那条。
   两个引擎选中的分支结果完全一致（脚本里跟 NumPy 逐行算的基准对过账，最大偏差 0.00e+00），
   差的只是**算几遍** —— 分支越多、每支越贵，差距越大。

选型口诀：

- 规则能改写成原生表达式 —— **永远用原生**（比逐行 UDF 快一个量级）
- 只能用 Python 写 —— 用**批式**接口（DuckDB `type='arrow'` / Polars `map_batches`）
- DuckDB 的 arrow UDF，函数体优先用 **numpy**，别用 pyarrow.compute
- 批内别退回逐行 Python —— 那是把批式接口用废了

顺带一个类型坑：DuckDB 的 `CASE WHEN` 里 `100.0` 是 **DECIMAL 字面量**，整列会变成
`decimal[4,1]`，和 UDF 返回的 DOUBLE 对不上 —— 需要 `::DOUBLE` 对齐。

## 线程扩展性

`uv run python src/scaling_demo.py` 在同一份 400 万行数据上，用 1/2/4/8 线程各跑三条查询。
每个「引擎 × 线程数」组合放进独立子进程，再把所有组合**交错跑若干轮**（每轮把所有组合测一遍），
最后分别给两个口径：「各轮中位数的中位数」和「所有轮次里的最快一次」。

机器状态：8 核，load1 在 6.9~13.0 之间飘 —— 这是台共享开发机，数字只能看相对关系。

**口径一：各轮中位数的中位数（ms）**

| 查询 | 引擎 | 1 线程 | 2 线程 | 4 线程 | 8 线程 | 8 线程加速比 |
| --- | --- | --- | --- | --- | --- | --- |
| 区域 × 类目 Top-N | DuckDB | 254.9 | 265.9 | 158.7 | 154.7 | 1.65× |
| 区域 × 类目 Top-N | Polars | 203.8 | 178.7 | 167.0 | 85.7 | **2.38×** |
| 每日收入趋势 | DuckDB | 205.1 | 213.4 | 134.7 | 143.8 | 1.43× |
| 每日收入趋势 | Polars | 265.2 | 150.5 | 136.4 | 43.3 | **6.12×** |
| 高价值客户 Top 20 | DuckDB | 247.3 | 250.6 | 145.9 | 129.5 | 1.91× |
| 高价值客户 Top 20 | Polars | 259.5 | 227.6 | 164.1 | 57.6 | **4.51×** |

**口径二：所有轮次里的最快一次（ms）** —— 更接近「机器空闲时」的上限

| 查询 | 引擎 | 1 线程 | 2 线程 | 4 线程 | 8 线程 | 8 线程加速比 |
| --- | --- | --- | --- | --- | --- | --- |
| 区域 × 类目 Top-N | DuckDB | 179.3 | 100.7 | 60.3 | 61.2 | 2.93× |
| 区域 × 类目 Top-N | Polars | 155.4 | 79.7 | 49.0 | 41.4 | 3.75× |
| 每日收入趋势 | DuckDB | 150.6 | 84.9 | 72.5 | 50.2 | 3.00× |
| 每日收入趋势 | Polars | 137.8 | 71.4 | 43.9 | 38.7 | 3.56× |
| 高价值客户 Top 20 | DuckDB | 165.3 | 96.9 | 98.5 | 65.8 | 2.51× |
| 高价值客户 Top 20 | Polars | 188.7 | 103.8 | 59.0 | 51.3 | 3.68× |

结论：

- **Polars 的扩展性更好。** 三个查询的 8 线程加速比（中位数口径）Polars 2.38× / 6.12× / 4.51×，
  DuckDB 1.65× / 1.43× / 1.91×；最快一次口径也全部是 Polars 赢（3.75× / 3.56× / 3.68× vs 2.93× / 3.00× / 2.51×）。
- **两个引擎在 2 线程时几乎都没收益**（DuckDB 0.96~0.99×，Polars 1.14~1.76×）—— 这个量级的查询不是被线程数卡住的，
  瓶颈在扫描和聚合本身，加线程只能互相抢 CPU。
- **并行效率掉得厉害**（效率 = 加速比 / 线程数）：8 线程时 DuckDB 只有 18%~24%，Polars 30%~76%。
  线程不是加得越多越好。
- **轮间波动比加速比本身还大**：同一组合各轮中位数的 max/min 最高到 4.54×（Polars 每日收入趋势 8 线程）。
  在这台机器上，「8 线程比 1 线程快 6 倍」这种结论说到底是噪声 —— 想看真实上限得在机器空闲时重测。
- **负载 > 核数时测到的加速比是保守值**：多线程引擎在抢不到 CPU 时最吃亏，线程越多亏得越多。

结果一致性：六个「查询 × 引擎」组合在 1/2/4/8 线程下结果**完全一致**（SUM 相对差 ≤ 8.53e-15）。
并行执行不改变结果，但会改变浮点求和的**顺序**，所以跨线程数比较 SUM 时末位有 1e-9 级差异 —— 别当成 bug。

## ASOF JOIN（时间序列 join）

`uv run python src/asof_demo.py` 用同一份数据（40 万笔成交 × 20 万条报价），
把「拿成交时刻之前最近的一条报价」这件事在两个引擎里各写一遍，再逐值对账。

四个语义差异（都是静默的错数据，不报错）：

| # | 差异 | DuckDB | Polars | 对齐方式 |
| --- | --- | --- | --- | --- |
| 1 | 默认连接类型 | `ASOF JOIN` 是 **INNER** —— 匹配不上的行直接消失 | `join_asof` 是**左连接** —— 匹配不上给 null，行还在 | DuckDB 侧写 `ASOF LEFT JOIN` |
| 2 | 并列时间戳 | 取右表物理顺序的**第一条**（10.0） | backward 取命中点往前扫到的**最后一条**（11.0） | 右表加确定性 tie-break 键，或保证 (symbol, ts) 唯一 |
| 3 | 输入要不要先排序 | 不要求，引擎自己排 | 要求有序，但**带 `by` 分组时只警告不报错** | Polars 侧永远先 `sort(["symbol","ts"])` |
| 4 | 容忍窗口 | 没有 `tolerance` 参数 | 原生 `tolerance=` 参数 | DuckDB 侧用 `ASOF LEFT JOIN` + ON 里加不等式 |

第 1 条最容易踩：5 行的成交里有一笔早于所有报价，DuckDB 侧**根本不返回这一行**（5 行 vs 6 行）。
写成 `ASOF LEFT JOIN` 之后两边逐值完全一致 ✅。

第 3 条是雷区 —— 报价表倒着排（最新的在最前面）时：

| 引擎 | 结果 |
| --- | --- |
| DuckDB | 5 行，px = `[10.0, 10.0, 20.0, 20.0, 30.0]`（引擎自己排，结果不变） |
| Polars 不排序 | 6 行，px = `[None, None, None, 20.0, 20.0, 30.0]` |
| Polars 排序后 | 6 行，px = `[None, 10.0, 10.0, 20.0, 20.0, 30.0]` ✅ |

**顺序错了，但 Polars 照样返回了结果** —— 没有报错，只有结果不对。`join_asof` 在**不带** `by` 时会检查顺序（不有序直接抛错），
一旦用了 `by` 分组（真实场景基本都要用），检查就只剩一条警告。

第 4 条：容忍窗口 = 60 秒时，两边都是 `[10.0, 12.0, None]`（最后一笔距离上一条报价 90 秒 > 60 秒 → null）。
边界是闭区间 —— 差值正好等于 tolerance 时仍然匹配。DuckDB 不加条件会给 `[10.0, 12.0, 12.0]`。

大数据对账：40 万行 × 4 列逐值一致 ✅（这份数据里成交时间戳刻意和报价错开了 500 微秒，不存在并列；
场景 2 说明了有并列时两边会选到不同的行）。

性能（40 万成交 × 20 万报价，本机，load1 = 10.5）：

| 做法 | 耗时 (ms，中位数) |
| --- | --- |
| DuckDB ASOF JOIN（端到端，含扫 parquet） | 83.1 |
| Polars `join_asof`（只算 join，输入已排序） | **15.1** |
| Polars 读 parquet + 排序 | 51.0 |
| Polars 端到端（读 + 排 + join） | 71.3 |

- 只看 join 这一步：Polars 15 ms vs DuckDB 83 ms，快 5.5×。
- 但 DuckDB 那 83 ms 里**已经包含了把右表排好**的成本；Polars 端到端 71 ms 比「只算 join」多了 56 ms，
  这 56 ms 全部是「Polars 必须自己排序」的代价 —— DuckDB 一次 ASOF JOIN 就把它藏起来了。
- 结论：**Polars 的 ASOF JOIN 引擎更快，DuckDB 的写法更省心**（不用管排序，也不用管 INNER/LEFT 的默认差异）。

## JSON 与嵌套类型

`uv run python src/json_demo.py` 用同一份 20 万行 NDJSON，比两个引擎读同一份 JSON 之后的行为。

**类型推断：6 列里 5 列一致**，唯一不一致的是 `ts`：

| 列 | DuckDB | Polars |
| --- | --- | --- |
| `event_id` / `user_id` | BIGINT | Int64 |
| `event_type` | VARCHAR | String |
| `amount` | DOUBLE | Float64 |
| `props` | `STRUCT("source" VARCHAR, campaign STRUCT(id BIGINT, "name" VARCHAR), "value" DOUBLE, flags VARCHAR[])` | `Struct{source=String, campaign=Struct{id=Int64, name=String}, value=Float64, flags=List(String)}` |
| `ts` | **TIMESTAMP** | **String** |

`ts` 的差异不是 bug，是两边「顺手多做了」的事：DuckDB 的 JSON 读取器认出 ISO-8601 字符串直接推成 TIMESTAMP，
Polars 的 `read_ndjson` 只做 JSON 类型映射，字符串就是字符串。对齐办法：Polars 侧 `str.to_datetime()`，
DuckDB 侧 `read_json(..., timestampformat=…)` 或直接 `::TIMESTAMP`。
不对齐的代价：按 `ts` 过滤/排序时 Polars 侧是在**比字符串**，格式一旦不统一（比如有人写成 `2024-1-5`）排序结果就是错的，而且不报错。

**大整数：两边读出来的数不是同一个数。**

| 原始值 | DuckDB | Polars |
| --- | --- | --- |
| 1 | 1.0 | 1 |
| 99999999999999999999 | **1e+20** | 99999999999999999999 |
| 12345678901234567890 | **1.2345678901234567e+19** | 12345678901234567890 |

DuckDB 的 JSON 读取器没有 UHUGEINT，超大整数会退化成 DOUBLE，**精度直接没了**；Polars 推断成 Int128，一个不丢。
对账时要拿「原始 JSON 文本」比，别拿读进来的列比。

**取嵌套字段：两边的写法完全不同。** DuckDB 有 JSON 路径：

- `json_extract(j, '$.props.value')` 返回的是 **JSON 类型**，不是数字 —— 值是**原文**（字符串会带引号，比如 `"双十一"`），要数字得自己 `::DOUBLE`。
- `json_value(j, '$.props.value')` 看起来该给个类型，其实**返回 VARCHAR**，`'9.5'` 还是字符串。
- `->` 返回 JSON，`->>` 返回 VARCHAR，分别等价于 `json_extract` / `json_extract_string`。
- 路径不存在时三个函数都返回 NULL，不报错。

Polars 没有「JSON 路径取值」，只有「整份解码」，而且 `str.json_decode` 的 `dtype` 在 2.1 里是**必填参数**：

```python
pl.col("j").str.json_decode(dtype=pl.Struct({...}))   # 忘了 dtype -> TypeError
pl.col("props").struct.field("campaign").struct.field("name")   # 不支持 'a.b.c' 点号路径
```

**嵌套类型（STRUCT / LIST）两边是对齐的** —— 在 Arrow 上是标准格式，DuckDB → Arrow → Polars、
Polars → `con.register()` → DuckDB 两个方向来回转都不丢信息、不丢类型。
真正有差异的是「JSON 文本」这一层。

**代价：JSON 文本列，每抽一个字段就重新解析一次。**

| 做法 | 耗时 |
| --- | --- |
| DuckDB 抽 1 个字段 | 120 ms |
| DuckDB 抽 3 个字段 | 398 ms（每多抽一个字段 ≈ **+139 ms**，基本线性） |
| Polars 整份解码成 struct（20 万行） | 409 ms |
| Polars 解码之后取 3 个字段 | **8.0 ms** |

DuckDB 的 `json_extract` 是「一行一次解析」，抽 N 个字段就解析 N 次；Polars 是「整份解码成 struct」，
解码一次之后取字段是纯内存操作。所以**数据在磁盘上就是 JSON 文本时，DuckDB 侧最好先把需要的字段抽出来落成列**，
别在下游反复抽。

读一份 NDJSON 谁快（load1 = 9.9）：DuckDB `read_json_auto` 298.3 ms vs Polars `read_ndjson` 253.4 ms —— **Polars 快 1.2×**。
两边读出来的行数一致（200,000 / 200,000），`amount` 求和一致（34996936.3200002 vs 34996936.319999985）。

## 索引与点查

`uv run python src/index_demo.py` 用同一批 500 万行客户数据造**两份**：一份按主键排序，一份随机打乱行序
（整行置换，两份文件里同一个 `customer_id` 的其余列完全一样，可以对账）。
为什么要两份：Parquet 的 row group 自带 min/max 统计，排好序时谓词一推就能整组跳过；
「有没有索引」的收益会被这个因素完全盖住，所以必须分开造。

**场景 1：点查（`customer_id = 3,000,000`）**

| 数据布局 | 实现 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- | --- |
| 排序 | DuckDB 表 · 无索引 | 1.40 | 1.02 |
| 排序 | DuckDB 表 · 有 ART 索引 | 1.44 | 1.19 |
| 排序 | Polars · 扫 Parquet | 6.43 | 6.13 |
| 排序 | Polars · 已在内存 | 2.67 | 1.76 |
| 打乱 | DuckDB 表 · 无索引 | 3.72 | 2.89 |
| 打乱 | DuckDB 表 · 有 ART 索引 | **1.29** | **1.00** |
| 打乱 | Polars · 扫 Parquet | 42.24 | 35.43 |
| 打乱 | Polars · 已在内存 | 2.53 | 2.26 |

八个变体结果完全一致 ✅。三条结论：

1. **索引的收益，取决于数据排没排序。** 排好序：DuckDB 1.40 ms（无索引）vs 1.44 ms（有索引）—— 没差别，
   row group 统计已经把范围缩到一组了；打乱后：3.72 ms → 1.29 ms（**2.9×**）—— 这才是索引真正的用武之地。
2. **排不排序，对 Polars 的影响比 DuckDB 还大**：扫 Parquet 6.43 ms → 42.24 ms（**6.6×**）。
3. **但 Polars 的慢不是「没索引」的锅**：数据已经在内存里时 Polars 2.53 ms vs DuckDB 1.29 ms ——
   差距在「读 Parquet」这一层，不在「点查」这一层。

所以别拿「DuckDB 有索引、Polars 没有」当结论：排好序时 DuckDB **无索引** 1.40 ms 就已经比 Polars 扫文件 6.43 ms 快 4.6×。
真正的工程动作是**按点查的键排序落盘**，而不是想办法造索引。

**场景 2：索引的适用边界**（实测出来的，不是文档抄的）

| 过滤条件 | 实际算子 | 扫了几个 row group | 走索引了吗 |
| --- | --- | --- | --- |
| 等值 `= K` | Index Scan | 0/50 | ✅ 是 |
| `IN`（不连续的 3 个值） | Index Scan | 0/50 | ✅ 是 |
| `IN`（连续的 3 个值） | Sequential Scan | 50/50 | — 否 |
| 范围 `>= K`（中部，命中 200 万行） | Sequential Scan | 50/50 | — 否 |
| 范围 `> n-1000`（尾部，命中 1000 行） | Index Scan | 0/50 | ✅ 是 |
| `BETWEEN` 中部 1000 行 | Sequential Scan | 50/50 | — 否 |
| `BETWEEN` 尾部 1000 行 | Index Scan | 0/50 | ✅ 是 |
| `BETWEEN` 尾部 10000 行 | Sequential Scan | 50/50 | — 否 |
| 列上的表达式 `+ 0` | Index Scan | 0/50 | ✅ 是 |
| 列上的表达式 `abs()` | Index Scan | 0/50 | ✅ 是 |
| 转成字符串比较 `::VARCHAR` | Sequential Scan | 50/50 | — 否 |
| `OR` 两个等值 | Sequential Scan | 50/50 | — 否 |
| 低基数列等值 | Sequential Scan | 50/50 | — 否 |

- **等值一定走索引**：`= K`、不连续的 `IN` 都会走 ART 索引。
- **范围不一定**：只有范围落在主键空间的**两端**、且命中行数很少（实测阈值在 5000 行左右，500 万行表 ≈ 0.1%）时才走索引。
  同样 1000 行，落在尾部走索引，落在中部却全表扫 —— 优化器只能靠 min/max 统计估选择性，中部的区间它估不准。
- **连续的 `IN` 会被改写成范围**：`IN (K, K+1, K+2)` → `>= K AND <= K+2`，于是按范围的规则判断，索引就丢了。
  不连续的值（`IN (K, K+7, K+100)`）保持等值形式，索引还在。
- 常量表达式会被折叠（`customer_id + 0 = K` → `customer_id = K`），照样能用索引；但 `::VARCHAR` 就彻底用不了了。
- `OR` 两个等值、在低基数列上建索引，都不走索引（后者是另一个大坑：低基数列的索引既救不了点查，还白白拖慢写入）。

**`EXPLAIN` 和 `EXPLAIN ANALYZE` 对索引的结论是相反的**：

```
EXPLAIN（逻辑计划）        EXPLAIN ANALYZE（实际执行）
│ Type: Sequential Scan   │ Type: Index Scan
│ Filters: customer_id…   │ Row Groups Scanned: 0 / 50
```

想确认索引到底用没用上，**只能看 `EXPLAIN ANALYZE` 的「Row Groups Scanned」**。

**场景 3：索引的代价**（500 万行单列 ART 索引）

| 项 | 数值 |
| --- | --- |
| 建索引耗时 | 565 ms |
| 库文件体积 | 78.5 MB → 117.5 MB（+39.0 MB，+50%） |
| 插入 50 万行 | 无索引 137 ms vs 有索引 312 ms（**2.3× 慢**） |

索引是**写放大**：每插一行都要维护索引。批量灌数据的场景，正确做法是「先灌完再建索引」，而不是边写边维护。

**场景 4：Polars 没有索引，点查的几种写法**（同一份内存里的 DataFrame）

| 写法 | 中位数 (ms) | 最快 (ms) | 命中行数 |
| --- | --- | --- | --- |
| 全表过滤 `filter`（1 个键） | 2.32 | 1.91 | 1 |
| 全表过滤 `is_in`（100 个键） | 13.71 | 8.99 | 100 |
| 哈希 join semi（100 个键） | 7.62 | 6.99 | 100 |
| 手工二分（100 个键，含建索引） | 341.19 | 272.86 | 100 |
| 手工二分（100 个键，复用索引） | 12.66 | 12.28 | 100 |
| 哈希 join semi（1000 个键） | 8.12 | 5.24 | 1,000 |
| 全表过滤 `is_in`（1000 个键） | 6.47 | 4.65 | 1,000 |
| 手工二分（1000 个键，复用索引） | 137.68 | 132.57 | 1,000 |

八种写法结果一致 ✅。取舍：

- 查 1 个键 → `filter` 最省事。Polars 没有索引可建，别硬造。
- 查 N 个键 → `is_in` 和哈希 join 一个量级（8~14 ms）—— 都是「把键集做成哈希表 + 扫一遍主键列」，成本大头是那一次扫描。
- 手工二分 → 查 100 键 13 ms、查 1000 键 138 ms，**随键数线性增长**。原因很直接：
  `is_in` / join 是 Rust 里向量化的，`search_sorted` 是 Python 循环；而且前面还有一次性建索引的成本（排序 500 万行约 329 ms）。
  👉 所以在 Polars 里手工造索引是**亏的**：要查很多键就直接 join。

顺带一个 Polars 2.1 的坑 —— `is_in` 传什么参数，三种写法结果一样但告警不同：

| 写法 | 命中 | 废弃警告 |
| --- | --- | --- |
| `is_in(python list)` | ✅ | 无 |
| `is_in(pl.Series)` | ✅ | ⚠ **有** |
| `is_in(pl.Series.implode())` | ✅ | 无 |

脚本里用 `pl.Series(...).implode()`：语义明确，且不告警。

**场景 5：真正决定点查快慢的，是数据布局**

| 文件 | row group 总数 | 统计能命中的组数 | 必须真读的比例 |
| --- | --- | --- | --- |
| 排好序 | 25 | 1 | **4%** |
| 打乱 | 25 | 25 | **100%** |

Parquet 每个 row group 自带主键列的 min/max 统计，谓词一推就能整组跳过。**Polars 没有索引，
但靠 Parquet 自带的统计拿到了和索引一样的效果。** 数据排好序 → 一个 row group 只覆盖一段主键区间 → 25 组里只读 1 组；
数据打乱 → 每组的区间都是 `[1, N]` → 25 组全得读。

DuckDB 的 `CREATE INDEX` 干的其实是同一件事 —— 排序落盘 = 免费索引。

## 金额与 Decimal

`uv run python src/decimal_demo.py` 用 100 万笔金额（最小单位 0.0001 元）比两个引擎算钱的行为。

**0.1 + 0.2：两边的默认类型就不同**

| 写法 | 结果 | 类型 |
| --- | --- | --- |
| DuckDB `0.1 + 0.2` | `0.3` | DECIMAL(3,1) |
| DuckDB `0.1::DOUBLE + 0.2::DOUBLE` | 0.30000000000000004 | DOUBLE |
| Polars `0.1 + 0.2` | 0.30000000000000004 | Float64 |

DuckDB 的数值字面量默认是 DECIMAL —— 算术全程走定点；Polars 默认 Float64。
**但只要有一边是 DOUBLE，DuckDB 也一样飘**（实测两列都是 DOUBLE 时 `a+b = 0.30000000000000004`）——
「DuckDB 算钱准」只在**列本身是 DECIMAL** 时成立。

**百万笔求和：反直觉的一条 —— Polars 的浮点求和更准**

| 路径 | 结果 | 与精确值之差 |
| --- | --- | --- |
| 精确答案（整数单位求和） | 99938829.8636 | 0 |
| DuckDB `SUM(amount::DECIMAL)` | 99938829.8636 | 0 |
| DuckDB `SUM(amount)`（DOUBLE） | 99938829.86360525 | +5.25e-06 元 |
| Polars `sum(amount)`（Float64） | 99938829.8636 | **0** |
| Python 逐行累加 | 99938829.86360525 | +5.25e-06 元 |

DuckDB 的浮点求和和 Python 逐行累加**结果完全一样** —— 说明它走的是朴素累加，没有做补偿。

**但「恰好精确」不能当结论。** 换成对抗数据（`(1e16, 1.0, -1e16, 1.0) × 1000`，精确答案 2000.0）：

| 路径 | 结果 | 绝对误差 |
| --- | --- | --- |
| DuckDB `SUM(v)` | 1.0 | 1999 |
| Polars `sum(v)` | 1985.0 | 15 |
| DuckDB `fsum` / `sumKahan` | 1.0 | 1999 |

Polars 明显更准 —— 但**两边都是错的**，Polars 只是错得少一点。
这不是「Polars 更对」的结论，而是「两边都不该拿浮点算钱」。

**分组求和 vs 一次求和：本来就是两个数**

| 路径 | 结果 | 与精确值之差 |
| --- | --- | --- |
| 精确答案（整数单位求和） | 99938829.8636 | 0 |
| DuckDB 一次 `SUM` | 99938829.86360525 | +5.25e-06 |
| DuckDB 先分组再汇总 | 99938829.86359999 | -1.49e-08 |
| Polars 一次 `sum` | 99938829.8636 | 0 |
| Polars 先 `group_by` 再汇总 | 99938829.86360002 | +1.49e-08 |

分组前后 DuckDB 差 5.26e-06 元、Polars 差 1.49e-08 元 —— **「一次求和」和「分组求和再汇总」本来就是两个数**。
这次分组路径反而更接近精确值，那是运气：两者的误差符号和量级都不一样，
**没有任何理由指望分组能抵消或修好误差**。
实用结论：两种口径的报表对不上是正常的，要对齐就得统一口径（都用整数或 DECIMAL）。

**Polars 想要精确金额：三条路**

| 写法 | 求和结果（元） | 耗时 | 保证精确 |
| --- | --- | --- | --- |
| Float64（默认） | 99938829.8636 | 0.20 ms | ❌ |
| Int64 存最小货币单位 | 99938829.8636 | 0.20 ms | ✅ |
| `pl.Decimal(18,4)` | 99938829.8636 | 5.37 ms | ✅ |

Polars **有** Decimal dtype，聚合、算术、group_by 都支持（很多人以为不支持），代价是慢约 26×。
推荐还是 Int64 存最小货币单位：一样精确，聚合更快，和 Arrow / Parquet / 下游系统的兼容性也更好。

**溢出的表现正好相反**：DuckDB 抛 `ConversionException` / `OutOfRangeException`；Polars 的 Float64 静默给 `inf`。

**DECIMAL 存进 Parquet 是安全的**：物理类型 `decimal128(18,2)`，两边来回转不丢精度 ✅。

对账口径：浮点求和顺序不同，末位必然有差异 —— 用相对容差 1e-9 比（`common.compare_frames` 的 `rtol`），别用精确相等。

## NULL 与三值逻辑

`uv run python src/null_demo.py` 用同一张 6 行小表（NULL 放在关键位置）比两边的三值逻辑。

**1. `NULL = NULL` 是 UNKNOWN，不是 TRUE** —— 两边一致，过滤时 UNKNOWN 和 FALSE 一样被丢掉。
找「null 安全相等」：DuckDB `IS NOT DISTINCT FROM`，Polars `(a == b).fill_null(True)`（**别忘了 fill_null**）。

**2. 连接键为 NULL：两边默认都不匹配**（SQL 标准行为）。
留的口子：DuckDB `ON e.k IS NOT DISTINCT FROM r.k`，Polars `join(nulls_equal=True)`
（Polars 2.x 里这个参数叫 `nulls_equal`，2.0 之前叫 `join_nulls`，传旧名字直接报 `ArgumentRemovedError`）。

**3. 聚合函数遇到 NULL：有一处两边不一样**

| 指标 | DuckDB | Polars | 一致？ |
| --- | --- | --- | --- |
| `COUNT(*)` | 1 | 1 | ✅ |
| `COUNT(v)` | 0 | 0 | ✅ |
| `SUM(v)` | **NULL** | **0** | ❌ |
| `AVG(v)` | NULL | NULL | ✅ |

SQL 里 SUM 无行 = NULL，Polars 里 sum 无值 = 0 —— 这是本 demo 唯一一个两边默认行为直接冲突的聚合函数，
对账时「拿两个引擎的 SUM 相减」会算出 NULL 还是 0，取决于你怎么写。

**4. `COUNT(DISTINCT)` 算不算 NULL**：SQL 标准忽略 NULL；Polars 的 `n_unique()` 把 NULL 当成一个值（3 vs 2）—— **静默**的差异。
对齐：Polars 用 `drop_nulls().n_unique()`。

**5. `GROUP BY` 里的 NULL 自成一组** —— 两边一致，不和其他值混在一起。

**6. `NOT IN` 里有 NULL：DuckDB 返回空集，Polars 不是**

| 写法 | DuckDB | Polars |
| --- | --- | --- |
| `k IN (1, NULL)` | `[1, 4]` | `[1, 4]` |
| `k NOT IN (1, NULL)` | **`[]`** | `[2, 6]` |
| `k NOT IN (1)` | `[2, 6]` | `[2, 6]` |

`NOT IN (1, NULL)` 展开成 `k <> 1 AND k <> NULL`，第二个条件对任何行都是 UNKNOWN —— **一行都不返回**。
「查不在白名单里的」用 `NOT IN`，一旦白名单里有 NULL，整个查询静默返回空。两边都不报错 —— 这正是最危险的地方。
对齐办法：DuckDB 侧改用 `NOT EXISTS (SELECT 1 FROM 白名单 WHERE k = 白名单.k)`，或者先把白名单里的 NULL 过滤掉。

**7. 除以零**：DuckDB 默认报错（`TRY(...)` 或关掉开关可以拿到 NULL），Polars 整数除给 null、浮点除给 inf。

**8. 函数名**：DuckDB 有 `COALESCE` / `IFNULL`，**没有 `NVL`**（别按 Oracle 的习惯写）；Polars 对应 `coalesce(...)` / `fill_null(...)`。

## JOIN 全景

`uv run python src/join_demo.py` 用两张带 NULL 键的小表，再加一份真实数据（100 万订单 × 2 万用户）。

**七种连接的行数全部一致** ✅ —— 连接语义本身两边一样：

| 连接类型 | DuckDB 行数 | Polars 行数 |
| --- | --- | --- |
| INNER / LEFT / RIGHT / FULL | 2 / 4 / 4 / 6 | 2 / 4 / 4 / 6 |
| SEMI / ANTI | 2 / 2 | 2 / 2 |
| CROSS（无连接键，笛卡尔积） | 12 | 12 |

**但输出列的结构不一样 —— 这是最容易忽略的坑**

| 引擎 / 写法 | 列 |
| --- | --- |
| DuckDB `ON` | `['k', 'lv', 'k', 'rv']` ← **两个 k** |
| DuckDB `USING` | `['k', 'lv', 'rv']` |
| DuckDB `CROSS JOIN` | `['k', 'lv', 'k', 'rv']` ← 同样两个 k |
| Polars `inner` / `left` | `['k', 'lv', 'rv']` |
| Polars `right` | `['lv', 'k', 'rv']` ← 右表列排在前面 |
| Polars `full` | `['k', 'lv', 'k_right', 'rv']` ← 多一列 |
| Polars `cross` | `['k', 'lv', 'k_right', 'rv']` ← 跟 `full` 同一个套路 |

FULL OUTER 的**行内容**其实一致（6 行逐行对得上）：DuckDB 的 `USING` 把两边的键合并进一列 `k`，Polars 保留两列 ——
匹配上的行 `k` 和 `k_right` 相同，只在左边出现的行 `k_right` 是 NULL，反之亦然。**是同一行数据，只是列结构不同。**

这些差异在**按列名取数**时不会报错，只会让结果悄悄少一列或多一列。
顺带一提：字符串列的 Arrow 类型也不一样 —— DuckDB 导出 `string`（32 位偏移），Polars 导出 `large_string`（64 位偏移）。

**连接键类型不一致：一个偷偷转，一个直接拒绝**

左边 k 是整数 `[1,2,3]`，右边 k 是字符串 `['1','02','2']`：

- DuckDB → **成功**，比较时把**字符串那一侧转成了整数**（数值优先于字符串）
- Polars → `SchemaError: datatypes of join keys don't match`，干脆不连

危险在 DuckDB 这一侧，而且不是「少匹配」，是「**多匹配**」：`'02'` 和 `'2'` 转成整数后**都变成 2**，
左边的 k=2 于是同时匹配上右边的两行 —— **行数被悄悄放大**。
如果字符串那侧有转不成整数的值（`'abc'`），DuckDB 要到**连接时**才抛 `ConversionException` —— 脏数据到这一步才暴露。

选型建议：**连接键的类型必须先对齐**，不要指望引擎帮你转。

**重复键：连接会把行数放大，金额跟着翻倍**

左表 1 行 k=1、金额 10，右表 k=1 出现了两次 → 连接后 2 行、`SUM` 变成 20。
两边行为一致，但这正是要小心的地方：**两个引擎都会老老实实地放大，谁也不会提醒你**。
「订单表 join 用户维表」一旦维表的 user_id 重复，每笔金额都会被算两遍。
防御：连接前检查右表键的唯一性；或者用半连接（`SEMI` / `how='semi'`）做存在性检查 —— 半连接不放大行数。

**真实数据上的性能（100 万订单 × 2 万用户，按 tier 聚合）**

| 口径 | DuckDB | Polars |
| --- | --- | --- |
| 端到端（读 parquet + 连接 + 聚合） | 32.9 ms | 83.0 ms |
| 内存内（连接 + 聚合） | 9.5 ms | 21.8 ms |

结果对账 ✅ 一致（4 个 tier；销售额是 Float64，两边累加顺序不同，末位有 1e-14 量级的差异 —— 见 `decimal_demo.py`）。

## 写入路径与事务

`uv run python src/write_demo.py` 比「写出去」这件事的两边差异。

**1. 写同一个路径：两边都是静默覆盖；覆盖写的安全性还不一样**

DuckDB 第一次 199 字节、第二次 199 字节；Polars 第一次 579 字节、第二次 579 字节 —— 都是覆盖，**都不是追加**，也都不会提示你。
增量场景里这是坑：以为在追加，其实把上一批数据冲掉了。

写失败时两边都保住了旧文件（错误发生在真正写文件之前）。
但**「覆盖写」的安全性不一样**（实测写入过程中目录里出现过的文件名）：

| 引擎 | 覆盖已有文件时目录里出现 | 语义 |
| --- | --- | --- |
| DuckDB | `['tmp_x.parquet', 'x.parquet']` | 先写临时文件再 rename —— **原子替换** |
| Polars | `['x.parquet']` | 直接写目标路径 |

DuckDB 的读者要么看到完整的旧文件，要么看到完整的新文件；Polars 写到一半进程被杀，会留下一个半截的 parquet。
（补充：DuckDB 写**新文件**时也是直接写，临时文件只在「替换已有文件」时才用 —— 「第一次写」和「覆盖写」的安全性并不完全一样。）

**2. 事务：DuckDB 有，Polars 没有**

DuckDB（`.duckdb` 文件，单连接）：`BEGIN` + `INSERT` 后 2 行 → `ROLLBACK` 后 1 行 → 再 `COMMIT` 后 2 行。**真的完整的 ACID 事务**，还带 WAL 文件。

DuckDB 还有一个 Polars 没有的能力：**读写并发**。连接 A 开一个事务插入一行（不提交），连接 B 同时读 ——
B 读到 2 行（提交前的快照，没被 A 的未提交写入挡住）。MVCC 快照隔离：读者看到一致的历史版本，不会被写者阻塞。
这对「一边持续落库、一边跑分析」的场景很关键。

Polars 的 `DataFrame` / `LazyFrame` **没有事务 API** —— 没有 BEGIN / COMMIT / ROLLBACK。
能做的只是把几个 DataFrame 在内存里 `pl.concat` 起来，那是「拼数据」不是「事务」：没有隔离级别，没有回滚点，
更没有和文件系统写入绑定的原子性。要在 Polars 侧做出同等效果，只能靠「写新文件 + 原子替换 + 读者重试」。

**3. 类型往返**：纳秒时间戳不丢（DuckDB 2.0 有 `TIMESTAMP_NS`），Decimal 无损（decimal128）。
两处要留意：DuckDB 的 `TIMESTAMP` 本身就是**微秒**精度（Polars 是纳秒，parquet 里各存各的）；
`TIMESTAMPTZ` 落到 Polars 会被**规范化成 UTC**，原来的 +08 偏移没了。

**4. 写一百万行 parquet**

| 口径 | 写法 | 耗时 | 文件大小 |
| --- | --- | --- | --- |
| 端到端 | DuckDB `COPY (SELECT … FROM read_parquet) TO` | 211.2 ms | 21,069,324 B |
| 端到端 | Polars `read_parquet + write_parquet` | 125.7 ms | 17,140,018 B |
| 内存内 | DuckDB `COPY orders_mem TO` | 90.5 ms | 21,068,853 B |
| 内存内 | Polars `DataFrame.write_parquet` | 71.1 ms | 17,140,018 B |
| 内存内 | Polars `LazyFrame.sink_parquet` | **65.3 ms** | 17,140,018 B |

文件内部结构也不同：

| 文件 | 压缩 | row group 数 | 每组行数 | 大小 |
| --- | --- | --- | --- | --- |
| DuckDB COPY | SNAPPY | 9 | 122,880 | 21,068,853 B |
| Polars write_parquet | ZSTD | 8 | 125,000 | 17,140,018 B |
| Polars sink_parquet | ZSTD | 8 | 125,000 | 17,140,018 B |

原因很直白：**默认压缩算法不一样** —— DuckDB 默认 SNAPPY，Polars 默认 ZSTD，ZSTD 在这份数据上压得更狠（小约 19%）。
row group 的切法也不同（DuckDB 按 122880 行 = 内部块大小的倍数，Polars 按 125000 行）——
row group 是并行读的最小单位，切法会影响点查和扫描的并行度。
要对齐就显式指定：DuckDB `COMPRESSION ZSTD`，Polars `write_parquet(compression='snappy')`。

## CSV 读写与往返保真

`uv run python src/csv_demo.py` 比两边读同一份 CSV、写同一份 CSV 的行为。

**1. 类型推断：9 列里 4 列不一致**

| 列里的内容 | DuckDB | Polars |
| --- | --- | --- |
| 日期 `2024-01-01` | **date32[day]** | String |
| 时间戳 `10:20:30` | **timestamp[us]** | String |
| ISO 带 Z | **timestamp[us, tz=…]** | String |
| 布尔 `true/false` | bool | Boolean |
| 前导零 `007` | **string** | Int64 |
| 整列都是空 | string | String |

两处后果最严重：

① **日期和时间戳：DuckDB 自动识别，Polars 默认不识别。** Polars 要显式开 `try_parse_dates=True`；
DuckDB 想关掉自动识别反而得手动指定列类型 —— 它的默认行为偏向「尽量解析」。

② **前导零：DuckDB 保守地当字符串，Polars 推成整数。** `'007'` → DuckDB `'007'`、Polars `7` ——
邮政编码、身份证、订单号、门店编码读进来就毁了，而且**不会报错**。
修法：Polars 侧显式 `schema_overrides={'v': pl.String}`。

**2. 采样窗口：类型推断只看了一部分数据**

两个引擎都只采样一部分行做推断（DuckDB `sample_size = 20480` 行，Polars `infer_schema_length = 10000` 行），
区别在于**窗口之外的值怎么处理**：

| 浮点出现的位置 | DuckDB | Polars |
| --- | --- | --- |
| 第 20001 行（窗口内） | 推断成 double，读对 | 推断成 i64 后**直接报错** |
| 第 30001 行（窗口外） | 推断成 int64，**1.5 被静默读成 2** | **直接报错** |

DuckDB 那个「1.5 → 2」在真实数据里就是金额被四舍五入掉了 —— 不报错、不警告，数据就是错的。
**报错是好事，安静地改数才是坏事。**
修法：DuckDB `read_csv(path, sample_size=-1)`，Polars `read_csv(path, infer_schema_length=None)`；或者干脆两边都显式指定 schema。

**3. 坏行：列数不齐时两边各是什么表现**

文件是 `a,b,c` 表头 + 一行只有 2 列 + 一行 3 列：

- DuckDB 的自动嗅探**把分隔符猜错了**（猜成 `|`），于是把表头整行当成一个列名，数据全部错位，而且**一声不吭**
- Polars 补 NULL，结果是对的

两边都有「跳过坏行」的开关（`ignore_errors=true`），但那是在你已经知道有坏行时才用的 —— 默认行为才是要防的那个。
最省心的解法：**固定 schema，别让引擎猜**（DuckDB `columns={…}, auto_detect=false` / Polars `schema=…`）。

**4. 写出：同样的数据，两边写出来的字节一样吗？**

**逐字节相同** ✅ —— 含分隔符/引号/换行的字段用双引号包起来、内部 `"` 转义成 `""`、
浮点用最短往返表示（`0.1+0.2` 写成 `0.30000000000000004`）、NULL 写成空字段。

但**默认值相同不代表可以互换** —— 只要有一边改了分隔符或 NULL 表示，另一边的默认读取就对不上。写和读要用同一套约定。

**5. 往返保真：写出去的字节一样，读回来却不一样**

写出去的 CSV：`'code,day,amt\n007,2024-01-01,10.5\n008,2024-02-01,\n10086,2024-03-01,0.30000000000000004\n'`

| 列 | DuckDB | Polars（默认） | 一致？ |
| --- | --- | --- | --- |
| `code` | `'007'`（字符串） | `7`（整数） | ❌ 前导零丢了 |
| `day` | `date` 对象 | `'2024-01-01'`（字符串） | ❌ 要显式开 `try_parse_dates` |
| `amt` | `0.30000000000000004` | `0.30000000000000004` | ✅ |

**CSV 的字节层面两边完全一致，但读回来的语义不一致。**
往返保真不能只看「文件写出来对不对」，还得看「读回来还是不是同一批数据」。

## 日期时间与时区

`uv run python src/tz_demo.py` 比两边的时间类型、时区语义和夏令时处理。

**1. 类型是两维的**：精度（DATE / µs / ns）× 时区（naive / aware）。
DuckDB 用三个类型名（`TIMESTAMP` / `TIMESTAMP_NS` / `TIMESTAMPTZ`）表达，Polars 用 `Datetime(time_unit, time_zone)` 一个 dtype。

**2. 精度：往微秒转是四舍五入，不是截断**

`TIMESTAMP_NS '2024-01-01 00:00:00.123456789'` 转成 `TIMESTAMP` → `2024-01-01 00:00:00.123457`；
`.999999999` 转过去直接变成 `00:00:01`。DuckDB 的 `TIMESTAMP` 是微秒精度，要保纳秒就用 `TIMESTAMP_NS`（Polars 侧是 `Datetime('ns')`）。

**3. 会话时区会改变 TIMESTAMPTZ 的显示文本，epoch 不变**

| 会话时区 | 显示 | epoch_ms |
| --- | --- | --- |
| Asia/Shanghai | 2024-01-01 12:00:00+08 | 1704081600000 |
| UTC | 2024-01-01 04:00:00+00 | 1704081600000 |
| America/New_York | 2023-12-31 23:00:00-05 | 1704081600000 |

瞬间没变，变的只是显示。但导出成文本 CSV 时导出的是**显示值** —— 同一份数据在不同机器上导出来是不一样的。
另外 `epoch_ms()` 永远按 UTC 解释（与会话时区无关），`to_timestamp()` 返回 TIMESTAMPTZ（按会话时区显示）。

**4. naive 和 aware 直接比较：答案取决于会话时区**

| 会话时区 | naive = aware |
| --- | --- |
| Asia/Shanghai | **True** |
| UTC | **False** |
| America/New_York | **False** |

同一句 SQL，换个会话时区结论直接反过来，而且**不报错**。Polars 直接抛 `SchemaError`。
这不是「谁更对」的问题 —— 是 DuckDB 会在你换机器、换连接池配置后悄悄改掉比较结果，而 Polars 会当场拦下来。

**5.「贴标签」「换显示」是两件事**

- 贴标签：认为这个本地时间**属于**某个时区 → DuckDB `AT TIME ZONE`（作用在 naive 上）/ Polars `replace_time_zone`
- 换显示：瞬间不变，换个时区显示 → DuckDB `AT TIME ZONE`（作用在 aware 上）/ Polars `convert_time_zone`

**名字一样，方向相反** —— DuckDB 靠输入类型区分，Polars 拆成两个方法。照名字翻译最容易错的就是这里。
（贴标签之后两边的瞬间对得上：epoch 1704128400000 ✅）

**6. 夏令时：一个静默选，一个直接报错**

| 情况 | DuckDB | Polars |
| --- | --- | --- |
| 02:30 **不存在**（2024-03-10，纽约） | 静默**往后挪 1 小时** | `ComputeError`，可用 `non_existent='null'` |
| 01:30 **出现两次**（2024-11-03，纽约） | 静默选**第二次**（EST） | `ComputeError`，可用 `ambiguous='earliest'/'latest'` |

静默的那一边在跨时区的排班、账期、日志对齐里会直接算错；报错的那一边至少不会悄悄错。

**7. Parquet 往返：DuckDB 一律写成 UTC，Polars 保留自己的时区**

| 写的一方 | 写出的物理类型 |
| --- | --- |
| DuckDB（aware） | `timestamp[us, tz=UTC]` |
| Polars（aware） | `timestamp[us, tz=Asia/Shanghai]` |

瞬间都保住了，丢的是「原来是在哪个时区记的」—— 想留就自己多存一列。

顺带一个 DuckDB 的环境依赖：直接把 `TIMESTAMPTZ` 转成 Python 对象需要 **pytz**
（没装会报 `InvalidInputException: Required module 'pytz' failed to import`）；走 Arrow 那条路不需要。Polars 用标准库 `zoneinfo`，没这个依赖。

**8. 截断与分桶：哪个方法对得上哪个**

按天 / 按周（周一起点）/ 按月，两边的 `date_trunc` ↔ `dt.truncate` 结果完全一致 ✅。
一个容易踩的坑：Polars 的 `dt.round` **不是** `time_bucket` 的对应物 —— round 是四舍五入到最近的桶，time_bucket 是向下取整：

```
10:53 落桶：  DuckDB time_bucket(15m) → 10:45
              Polars dt.truncate('15m') → 10:45
              Polars dt.round('15m')     → 11:00
```

星期几的编号两边不一样：Polars `dt.weekday()` 是 ISO（1=周一），DuckDB `dayofweek` 是 0=周日 ——
**只有周日不同**（0 vs 7），这种「大部分对、偶尔错」的差异最容易混过测试（用 DuckDB 的 `isodow` 对齐）。

**9. 运算：月末对齐两边完全一致，真正分叉的是「加一天」**

月末对齐（2024-01-31 加一个月）：naive / aware 四个结果**全部是 2024-02-29**，两边一致 ✅。

夏令时那天「加一天」（2024-03-09 12:00 纽约）：

| 写法 | 结果 |
| --- | --- |
| DuckDB `TIMESTAMPTZ + INTERVAL '1 day'` | epoch 1710090000000（绝对 +24 小时） |
| Polars `+ pl.duration(days=1)` | 2024-03-10 13:00:00-04:00（同样绝对 +24 小时）✅ 一致 |
| Polars `+ dt.offset_by('1d')` | 2024-03-10 **12:00**:00-04:00（日历加一天） |

Polars 的 `+ pl.duration(...)` 是绝对时长、`dt.offset_by(...)` 是日历加减，普通日子一样，跨夏令时差一小时。

**10. 一百万行真实数据**

时间过滤 + 按天分组，182 行 × 3 列逐值一致 ✅。

| 写法 | 耗时 |
| --- | --- |
| DuckDB（`order_ts` 是 timestamp[ns]） | 27.3 ms |
| DuckDB（另存一份 timestamp[us] 的 parquet） | 18.7 ms |
| Polars（timestamp[ns]） | **18.7 ms** |

差在哪：DuckDB 的 `date_trunc` 输出是微秒，作用在 `TIMESTAMP_NS` 列上要先**收窄**；Polars 的 `dt.truncate` 直接在 ns 上做。
所以 DuckDB 在 ns 上比在 us 上慢的那一截（27.3 → 18.7 ms），就是这个收窄的代价。

口径提醒：Polars 的过滤字面量必须显式 cast 成 ns，否则它会把 ns 列降到 us 再比较，边界行的归属可能变；
DuckDB 的 `TIMESTAMP '...'` 会自动加宽到 `TIMESTAMP_NS`，没这个问题。

## 排序稳定性

`uv run python src/order_demo.py` 用 100 万行、只有 1000 个不同排序键的数据（并列极多），看两个引擎的排序到底稳不稳。

**1. NULL 排在哪：两边默认位置相反**

| 引擎 | 升序 | 降序 |
| --- | --- | --- |
| DuckDB 默认 | `1 2 3 NULL NULL` | `3 2 1 NULL NULL` |
| Polars 默认 | `NULL NULL 1 2 3` | `NULL NULL 3 2 1` |
| Polars `nulls_last=True` | `1 2 3 NULL NULL` | `3 2 1 NULL NULL` |

DuckDB 的 `default_null_order = NULLS_LAST`（**升序降序都空最后**）；Polars 的 `nulls_last` 默认 False（**升序降序都空最前**）。
降序时差得更远：DuckDB 空值仍在最后，Polars 空值跑到最前。

**2. NaN 排在哪：它不是 NULL，是最大的数** —— 两边一致：NaN 排在所有数字后面，NULL 再后面。
而且**两边都认为 `NaN = NaN`**（不像 IEEE 754），所以 `x != x` 判断不了 NaN，要用 `isnan()` / `is_nan()`。

**3. 排序稳不稳：同一个查询，重复执行答案可能不一样**

100 万行、排序键只有 1000 个不同取值。看 k=0 的那批行的相对顺序，连跑 5 次 → **跑出了好几种不同的顺序**
（具体几种每次跑都不同，实测见过 2 种、也见过 4 种）。

线程数的影响（每个线程数单独测 3 次）：

| threads | 结果 |
| --- | --- |
| 1 | ✅ 稳定（串行排序，顺序固定） |
| 2 / 4 / 8 | ⚠️ **不保证** —— 实测有时碰巧稳、有时一次跑出 3~4 种结果 |

单线程走串行排序，顺序就是插入顺序；**2 线程起就走并行排序**，合并顺序取决于各线程什么时候把结果交上来 —— 每次都可能不同。
线程数越多，合并的批次越多，撞出不同顺序的机会也越多 —— 但**多线程不一定每次都不稳**，所以别拿「某次跑出来是稳的」当保证。
（这和 `scaling_demo.py` 里「线程数变了结果是否还一致」是同一个根因。）

Polars 侧：`sort()` 的 `maintain_order` 默认 **False**。单列排序实测恰好是稳的，**多列排序就不稳了**；
而 `maintain_order=True` 实测**没有可测量的额外开销**。
DuckDB 的 `preserve_insertion_order` **管不了这个** —— 它管的是表里行的存放顺序，不是并列的排序顺序。

**4. 后果：LIMIT 会取到不同的行**

百万行表 `ORDER BY k LIMIT 5` 连跑 6 次 → **跑出了多种结果**（实测见过 3 种、也见过 4 种）。
生产里长这样：榜单刷新一次第 10 名换人；翻页时第 21 行永远看不到或者出现两次；「取前 100 条导出」导出的内容每次不一样。
**两个引擎都不报错** —— 这就是它危险的地方。

小榜单上两边一致 —— 但那是**碰巧**（DuckDB 给的 id 顺序恰好也是插入顺序）。

**5. 分页：OFFSET 落在并列档内部时，两边取到的不是同一批行**

40 行、分数每 4 行一档，取 `OFFSET 22 LIMIT 10`：

```
DuckDB  → [20, 22, 24, 25, 26, 27, 28, 29, 30, 31]
Polars  → [22, 23, 24, 25, 26, 27, 28, 29, 30, 31]
```

**6. 字符串和中文排序：两边都是二进制序**（比 UTF-8 字节，不是本地化排序）

`'Apple' < 'apple'`（大写在前，和 MySQL 的 `utf8mb4_general_ci` 不同）；中文按 Unicode 码位排（`'上'=U+4E0A < '北'=U+5317`），**不是拼音**。
这一条两边一致 —— 值得记一笔：**一致的地方不用管，不一致的地方才要命**。

**7. 性能，以及「排序稳定」的代价**

百万行全排序 DuckDB 18.8 ms / Polars 17.6 ms（`maintain_order=True` 17.8 ms）；
Top-N 走专门的 top-N 算子（2.2 ms / 1.7 ms），比全排序快得多 —— 「要前 10 条就别全排」两边都成立。

修法只有一个：**排序键要能唯一确定顺序**。

- DuckDB：**没有开关**能让 `ORDER BY` 变稳，只能自己加唯一 tiebreaker（`ORDER BY k, p`）
- Polars：`sort(..., maintain_order=True)`，实测没有额外开销，直接开着

## 多文件与分区表

`uv run python src/partition_demo.py` 用脚本自己造的 parquet 目录（`dt=…/part-0.parquet` 这种 hive 结构），比两个引擎读多文件、写分区。

**1. 读多个文件：两边都支持 glob，用法一致** ✅（200 个文件、20000 行，行数与 `sum(v)` 都对得上）。
DuckDB 的 `read_parquet` 只接**一个**参数（字符串或列表），多文件要写 `read_parquet(['a', 'b'])`。

**2. 文件读取顺序：DuckDB 靠一个开关保序，Polars 没有开关**

DuckDB 的 `preserve_insertion_order`（默认 True）关掉之后，前 5 行的 id 不再是 `[0, 1, 2, 3, 4]`，
而是**换成别的文件里的行**（具体是哪个文件、哪几行，每次跑都不一样 —— 实测见过 `[4000, …]`、`[12000, …]`、`[72000, …]`）。
**Polars 没有这个开关**，顺序固定按文件名。依赖行序的查询，两边都要显式 `ORDER BY`。

**3. 各文件的列类型不一致：一个自动统一，一个直接报错**

一个文件的 `v` 是 float64、另一个是 int64：

- DuckDB → **自动统一成 DOUBLE**，两个文件都读进来了 —— 整数列会悄悄变成浮点，金额和 ID 要当心
- Polars → `SchemaError: data type mismatch for column v`，以第一个文件的 schema 为准，后面的对不上就中止

Polars 想放行得显式说：`scan_parquet(..., cast_options=pl.ScanCastOptions(integer_cast='allow-float'))`。

**4. 各文件的列名不一致：合并的开关两边都有，但名字不一样**

| 引擎 | 默认 | 放行方式 |
| --- | --- | --- |
| DuckDB | 报错（`schema mismatch in glob`） | `union_by_name=true` → 列并起来，缺的填 NULL |
| Polars | 报错（`extra column in file outside of expected schema`） | `extra_columns='ignore'` **和** `missing_columns='insert'` 两个一起开 |

**5. 没有匹配文件时两边都报错**（不是返回空表）—— 「上游还没写完文件」这种场景，任务会直接失败，而不是安静地返回 0 行。

**6. Hive 分区目录：Polars 传 glob 不认分区列**

同一个目录：

| 写法 | 结果 |
| --- | --- |
| DuckDB `read_parquet('{root}/*/*.parquet')` | 3 行，列 = `id, v, dt`（自动认出分区列 dt） |
| Polars `scan_parquet('{root}/*/*.parquet')` | 3 行，列 = `id, v` ← **没有 dt** |
| Polars `scan_parquet('{root}')`（传目录） | 3 行，列 = `id, v, dt` |
| Polars `scan_parquet(..., hive_partitioning=True)` | 3 行，列 = `id, v, dt` |

**同一个目录，路径写法不同，结果的列就不一样** —— 漏掉这个开关，`WHERE dt = ...` 会直接报「列不存在」。

**7. 分区值的类型：两边都把 `2024-01-01` 推成日期，但过滤方式不同**

- DuckDB `WHERE dt='2024-01-02'` → 隐式转换掉，**不报错**
- Polars `WHERE dt='2024-01-02'` → `InvalidOperationError: cannot compare 'date/datetime/time' to a string value`，得写 `pl.col('dt') == pl.lit(date(2024, 1, 2))`

另外目录名写成 `dt=20240101` 时 DuckDB 会推成 **BIGINT** 而不是 DATE —— 目录名格式决定列类型。

**8. 分区裁剪：两边都会裁，EXPLAIN 里看得见**

```
-- DuckDB
EXPLAIN SELECT * FROM read_parquet('{root}/*/*.parquet') WHERE dt='2024-01-02'
--   Read Parquet
--   File Filters: dt = '2024-01-02'::DATE
--   Scanning Files: 1/3          ← 只开了一个文件

-- Polars
Parquet SCAN [/tmp/.../dt=2024-01-02/part-0.parquet]   ← SCAN 里只剩那一个文件
```

**不相关的分区根本没被打开** —— 这是分区表最值钱的能力。
但**分区裁剪只对「恰好等于分区值」的过滤有效**：写成 `WHERE dt >= DATE '2024-01-02'` 只能裁掉整个前缀，中间的分区照样要读。

**9. 写分区：文件里留不留分区列、文件名怎么起**

| 引擎 | 目录 / 文件名 | 文件里的列 |
| --- | --- | --- |
| DuckDB `COPY … (FORMAT PARQUET, PARTITION_BY (dt))` | `dt=2024-01-01/data_0.parquet` | `['id', 'v']` ← **没有 dt** |
| Polars `write_parquet(dir, partition_by=['dt'])` | `dt=2024-01-01/00000000.parquet` | `['id', 'dt', 'v']` ← **有 dt** |

两边目录名都是 hive 写法，**能互相读**，但**分区列的类型跟着「谁写的」变**：

- Polars 写的目录：文件里的 dt 是 String，Polars 自己读回来是 String，**DuckDB 读回来却是 DATE**（路径上的值盖过了文件里的字符串列）
- DuckDB 写的目录：文件里根本没有 dt，两边都从路径补出来 —— DuckDB 给 DATE，Polars 给 Date

跨引擎写分区时，分区值统一用「yyyy-MM-dd」这种两边都认的格式最省事。

**10. 小文件场景：瓶颈是文件数，不是引擎**

同样 20000 行，200 个文件 vs 1 个文件：

| | DuckDB | Polars |
| --- | --- | --- |
| 200 个小文件 | 7.5 ms | 4.5 ms |
| 1 个大文件 | 1.5 ms | 0.9 ms |

两个引擎都因为「少开 199 个文件」快了一截（**5.0×**）。
这一档 Polars 快约 1.7×，但绝对时间都在个位数毫秒 —— 数据量再大一些差距就被摊薄，别拿这个当选型依据。
**真要提速就合并文件或改用分区表，别指望换个引擎能解决。**

## 窗口函数

`src/window_demo.py`。DuckDB 的 `OVER (...)` 和 Polars 的 `.over(...)` 看着像，语义上却有三处默认不一致 —— 而且都不是「报错」，是「安静地算出另一个数」。

### 1. 默认 frame 是 RANGE 还是 ROWS

DuckDB 不带 frame 的窗口**默认是 RANGE** —— 排序键相同的并列行会一起算进当前行的窗口。
Polars 的 `cum_sum()` 是逐行累计，等价于 ROWS。

7 行小表，`x` 有并列（`x=2` 有 4 行）：

| 写法 | 累计和 |
| --- | --- |
| DuckDB `ORDER BY x`（单键，不带 frame） | `[10.0, 170.0, 170.0, 170.0, 170.0, 210.0, 280.0]` |
| DuckDB `ORDER BY x` 显式 RANGE | 同上 |
| DuckDB `ORDER BY x, ord`（双键，不带 frame） | `[10.0, 30.0, 60.0, 110.0, 170.0, 210.0, 280.0]` |
| DuckDB `ORDER BY x, ord` 显式 ROWS | 同上 |
| Polars `cum_sum().over(order_by=[x, ord])` | 同上 |

`x=2` 那 4 行在 DuckDB 侧看到的都是 `10+20+30+50+60 = 170`（并列行一起进窗口）；
Polars 那边第二行看到的是 `10+20 = 30`。

**更隐蔽的一点：DuckDB 的默认值会随排序键个数变。**
RANGE frame 只能配单个 ORDER BY 表达式，配多个直接报 `Binder Error: RANGE frames must have only one ORDER BY expression`，
于是「ORDER BY x」和「ORDER BY x, ord」的结果不同 —— 后者退化成 ROWS。
给排序加一个并列打破键，累计口径就悄悄变了。

> **想跟 Polars 对齐，SQL 里必须显式写 `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`。**
> 别依赖默认值 —— 它既取决于引擎版本，也取决于你写了一个还是几个排序键。

Polars 侧还有一个同样不报错的坑：`cum_sum()` 不加 `over` 时按当前行序累计，
之后 `sort('x','ord')` 只是搬动带着旧值走的行，累计量不会重算：

```
cum_sum() 不加 over，按当前行序累计: [10.0, 30.0, 60.0, 100.0, 150.0, 210.0, 280.0]
然后再 sort('x','ord')，值不会重算 : [10.0, 30.0, 60.0, 150.0, 210.0, 100.0, 280.0]
```

### 2. frame 家族：ROWS / RANGE / GROUPS

DuckDB 三种 frame 都有；Polars 侧只有 ROWS 口径的表达式。

| frame | 含义 | `1 PRECEDING` 的实测结果 |
| --- | --- | --- |
| `ROWS` | 按行号数 | `[10.0, 30.0, 50.0, 80.0, 110.0, 100.0, 110.0]` |
| `RANGE` | 按值比（`[当前值-1, 当前值]` 里的所有行） | `[10.0, 170.0, 170.0, 170.0, 170.0, 200.0, 70.0]` |
| `GROUPS` | 按组数（并列行算一组） | `[10.0, 170.0, 170.0, 170.0, 170.0, 200.0, 110.0]` |

Polars 侧对应的是表达式家族，而不是 frame 子句：

| Polars 表达式 | 对应 SQL |
| --- | --- |
| `cum_sum()` | `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` |
| `shift(n).over(...)` | `lag(x, n)` / `lead(x, n)` |
| `rolling_*(...).over(...)` | `ROWS BETWEEN n PRECEDING AND n FOLLOWING` |
| RANGE / GROUPS | **没有现成表达式**，得自己拼 |

### 3. 分区窗口，以及 Polars 侧怎么拼出 RANGE

分区累计两边都能做，口径对齐后结果一致（✅）。分区里有并列值时 RANGE 与 ROWS 照样分叉 ——
分区 `a` 的 `x` 是 1,2,2,3：

| | 实测结果 |
| --- | --- |
| DuckDB RANGE | `x=2` 的两行都看到 `10+20+30 = 60` |
| DuckDB ROWS | 第二行 `10+20=30`，第三行 `10+20+30=60` |

Polars 侧 RANGE 的配方：先 `group_by(分区, 排序键)` 把并列行合成一组 → 对组求和 → 逐组累计 → 贴回原始行。
同一组里的值相等，累计点落在哪一行都一样。代价是多一次聚合 + 一次 join；DuckDB 的 RANGE 是一等公民，不用绕。

另外：Polars 的 `over(..., order_by=...)` **不写 `order_by` 时窗口内顺序未定义**，
累计类表达式（`cum_sum`）会直接算错，且不报错。

### 4. 命名窗口

DuckDB 的 `WINDOW w AS (PARTITION BY g ORDER BY x, ord)` 把「分区 + 排序」命名一次，
后面 `sum(v) OVER w`、`rank() OVER w` 反复引用，共用一套 frame，口径改一处就够。

Polars 没有命名窗口 —— 想共用就得把 `over('g', order_by=[...])` 抄两遍，
改口径时很容易只改一处，然后两边悄悄对不上了。

顺带对齐一下 rank：SQL 的 `rank()` 是「并列同名次、后面跳号」（= RANK），Polars 对应 `rank('min')`；
`rank('ordinal')` 是「每行一个号」，对应 `row_number()`。

### 5. QUALIFY

| | DuckDB | Polars |
| --- | --- | --- |
| 写法 | `QUALIFY rank() OVER w = 1` | `with_columns(...)` 再 `filter(...)` |
| 结果 | 2 行 × 4 列 | 2 行 × 4 列 ✅ 一致 |

Polars 有两种等价写法：`with_columns` 造出窗口列再 `filter`（两步，列名可复用），
或直接在 `filter` 的条件里写窗口表达式（一步，能用）。
但在 `filter` 里引用还没造出来的列会报 `ColumnNotFoundError: unable to find column "rn"`。

> 差别不在能不能算，而在 DuckDB 把「窗口结果直接当过滤条件」做成了语法；
> Polars 靠表达式的求值顺序，效果一样，写法和报错方式不一样。

### 6. LAG / LEAD 与 shift

| | DuckDB | Polars |
| --- | --- | --- |
| 往前看一行 | `lag(x)` | `shift(1)` |
| 往后看一行 | `lead(x)` | `shift(-1)` |
| 边界 | 填 NULL | 填 NULL |

7 行 × 5 列全部一致（✅）。

一个容易踩的点：DuckDB 的 `lag`/`lead` 是窗口函数，**必须带 OVER**，否则报错；
Polars 的 `shift()` 不加 `over()` 是在整张表上移位，不分区 —— 语义完全不同但不报错：

```
Polars shift(1) 不加 over： [None, 1, 2, 2, 3, 2, 2]
```

### 7. 窗口函数的代价

100 万行（region 分区、order_id 排序的累计销售额）：

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB | 102.8 | 88.4 |
| Polars | **31.8** | 29.4 |

Polars 快 **3.23×**。口径：输入已物化（Parquet 落盘、DuckDB 建表、Polars 读进内存），
计时的只有「窗口计算 + 结果物化」这一段。窗口函数贵的主因是排序：分区越大，要排的键越多。

## 聚合函数语义

`src/agg_demo.py`。同名聚合函数在两个引擎里返回不同类型、不同数量、甚至不同数值的都有 —— 而且**基本都不报错**。

### 1. 分位数：插值方式不一样，中位数对不上

样本 `[1.0, 2.0, 3.0, 4.0]`：

| 分位 | DuckDB `cont` | DuckDB `disc` | Polars 默认 | Polars `linear` |
| --- | --- | --- | --- | --- |
| 0.25 | 1.75 | 1.0 | 2.0 | 1.75 |
| 0.5 | **2.5** | 2.0 | **3.0** | 2.5 |
| 0.75 | 3.25 | 3.0 | 3.0 | 3.25 |

同一个 q=0.5，DuckDB `quantile_cont` = 2.5，Polars `quantile` = 3.0 —— **直接对账会挂**。
Polars 侧要么显式写 `interpolation="linear"`（对上 `cont`），要么 `interpolation="lower"`（对上 `disc`）。
Polars 支持的插值：`linear` / `lower` / `higher` / `nearest` / `midpoint`。

更隐蔽的一个：**DuckDB 的 `quantile_cont` 会跟着列的类型变**。

| 列类型 | q=0.25 / 0.5 / 0.75 |
| --- | --- |
| `DOUBLE` | `[1.75, 2.5, 3.25]`（线性插值，和 numpy 一致） |
| `DECIMAL(4,1)` | `[1.7, 2.5, 3.2]` ← 插值结果被**截断**回列的精度（scale=1） |

同样的数，只是列的类型不同，25% 与 75% 分位点就各差了 0.05 —— 是**截断**不是四舍五入。

### 2. 众数 mode：一个值，还是所有并列值

| 数据 | DuckDB `mode(x)` | Polars `mode()` |
| --- | --- | --- |
| `(1,1,2)` | `Decimal('1.0')` | `[1.0]` |
| `(1,2,3)`（次数全相同） | `Decimal('1.0')`（随便挑了一个） | `[3.0, 2.0, 1.0]`（全都算众数） |

DuckDB 的 `mode()` 永远返回一个标量；Polars 的 `mode()` 返回一个 Series，装着所有出现次数最多的值，
而且**顺序不保证**（本次是 `[3.0, 2.0, 1.0]`）。
想要 DuckDB 的口径得自己收窄（`mode().first()`），但「挑哪一个」仍然是不确定的。

### 3. arg_max / arg_min：并列时选中哪一行，没保证

`s` 与 `v` 两列，`v` 最大的有并列（`'b'` 和 `'a'` 都是 10.0）：

| 写法 | 结果 |
| --- | --- |
| DuckDB `arg_max(s, v)` | `'b'` |
| Polars `get(arg_max)` | `'b'` |
| Polars `sort_by(v).last()` | `'b'` |

这次两边选的是同一个（都是「碰到第一个就返回」），但**这不是规范保证** —— 输入顺序换成 `('a','b')`，两边都变成 `'a'`。

要稳定就得自己加一个并列打破键，但两个引擎的「打破」方向是**相反**的：

| 写法 | 结果 |
| --- | --- |
| DuckDB `arg_max(s, v ORDER BY ord)` | `'b'`（并列里取排序后的第一行） |
| DuckDB `arg_max(s, v ORDER BY ord DESC)` | `'a'`（取最后一行） |
| Polars `sort_by(['v','ord']).last()` | `'a'`（取最后一行） |

### 4. 字符串聚合：拼出来的顺序不一样

`s = ['b', 'a', 'c']`：

| 写法 | 结果 |
| --- | --- |
| DuckDB `string_agg(s, ',')` | `'b,a,c'`（顺序**未定义**） |
| DuckDB `string_agg(s, ',' ORDER BY s)` | `'a,b,c'`（确定） |
| Polars `implode().list.join(',')` | `'b,a,c'`（保持输入顺序） |

DuckDB 不带 `ORDER BY` 时不保证顺序（SQL 规范就是这样，实际输出取决于执行计划），Polars 的 `implode()` 保证按输入顺序。
拿两边的结果直接比字符串会挂 —— 除非 DuckDB 侧也写 `ORDER BY`，或者两边都排序。

注意 Polars 2.x 的 `implode()` 聚成的是**一个列表**（`['b','a','c']`），不是拼好的字符串 —— 要拿到字符串还得再 `.list.join(',')` 一层。

### 5. 整数求和：DuckDB 升精度，Polars 静默回绕

两个 `4611686018427387904` 相加（结果 `9223372036854775808`，超过 Int64 上限 `9223372036854775807`）：

| 写法 | 结果 | |
| --- | --- | --- |
| DuckDB `sum(x)` | `9223372036854775808`（类型 HUGEINT） | ✅ 对 |
| Polars `sum()` | `-9223372036854775808` | ❌ **静默回绕成了负数** |

DuckDB 把 BIGINT 的 `sum()` 升到 HUGEINT（128 位），所以不会溢出；Polars 的 `sum()` 保持 Int64，溢出后**回绕**，不报错、不警告。
金额类整数求和，Polars 侧要么先转 Int128/Decimal，要么自己检查量级。

### 6. 谁有哪些聚合函数

名单对照（两个引擎都真的调了一遍，不是照着文档抄的）：

| 函数 | DuckDB | Polars |
| --- | --- | --- |
| `product` / 乘积 | ✅ 6.0 | ✅ 6.0 |
| `entropy` / 熵 | ✅ 1.584962500721156 | ✅ **1.0114042647073518** ← 对不上 |
| `kurtosis` / 峰度 | ✅ None | ✅ -1.5 |
| `skewness` / 偏度 | ✅ 0.0 | ✅ 0.0（但 Polars 叫 `skew`） |
| `corr` / 相关系数 | ✅ 0.9979487157886733 | ✅ 0.9979487157886733 |
| `regr_slope` / 回归斜率 | ✅ 0.4426229508196721 | ✅ 0.4426229508196722（参数顺序是反的） |
| `bit_and` / 按位与 | ✅ 0 | ❌ AttributeError |

**`entropy` 是同名不同义的典型**：DuckDB 的是标准的以 2 为底的香农熵（`[1,2,3]` 的理论值 1.584963 ✅）；
Polars 的 `entropy([1,1,1,1])` = 1.386294（常数列的香农熵应该是 **0**），`entropy([0,0,1])` 直接出 **NaN**，
`entropy([7,8,9])` = 1.093390 —— 同样的频次分布换一组数值结果就变。
它算的不是「值频次分布的熵」，两个同名函数不可互换，混用会得到对不上的数。

协方差要当心：Polars 的 `pl.cov` 是**总体**口径（对上 DuckDB 的 `covar_pop`），样本口径得自己乘 `n/(n-1)`。
DuckDB 独有 `regr_*` 这一整族回归统计，Polars 侧没有现成函数。

### 7. 聚合的代价

100 万行，两边做同一件事（count/sum/avg/中位数）：

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB | **13.5** | 12.9 |
| Polars | 20.3 | 19.9 |

DuckDB 快约 **1.5×**。口径：输入已物化，计时的只有「分组聚合 + 结果物化」；
中位数的插值口径按上面的结论调成一致。

## 集合运算与去重

`src/setop_demo.py`。DuckDB 有 `EXCEPT` / `INTERSECT` / `UNION`，Polars 2.x **一个都没有** —— 得自己拼，而拼的时候 NULL 的处理是最容易出错的地方。

### 1. EXCEPT / INTERSECT：NULL 参与比较，而且 NULL 相等

`a = [1, 2, NULL]`，`b = [1, NULL, 3]`：

| 写法 | 结果 |
| --- | --- |
| DuckDB `EXCEPT` | `[(2,)]` |
| DuckDB `INTERSECT` | `[(NULL,), (1,)]` |
| DuckDB `EXCEPT ALL` | `[(2,)]` |
| DuckDB `INTERSECT ALL` | `[(1,), (NULL,)]` |

为什么是这个结果：a 里有一个 NULL，b 里也有一个 NULL。集合运算比较的是「值是否相同」，
DuckDB 在这里把两个 NULL 判成相同，所以 NULL 从 a 的结果里被减掉了（`EXCEPT` 里看不到它），又被算进了交集。
对比普通比较：`NULL = NULL` 是 UNKNOWN（不是 true），`IS NOT DISTINCT FROM NULL` 才是「按值判等」—— 集合运算走的是后者那套。

Polars 侧根本没有对应方法（`difference` / `intersection` / `symmetric_difference` / `set_difference` 全部 `False`）。

### 2. Polars 侧怎么补出 EXCEPT / INTERSECT

差集（a 里有、b 里没有的行）：

| 写法 | 结果 | 对得上 DuckDB 吗 |
| --- | --- | --- |
| `join(how="anti")` | `[2, None]` | ❌ |
| `filter(~is_in(...))` | `[2]` | ❌ |
| `join(how="anti", nulls_equal=True)` | `[2]` | ✅ |

交集（两边都有的行）：

| 写法 | 结果 | 对得上 DuckDB 吗 |
| --- | --- | --- |
| `join(how="semi")` | `[1]` | ❌ |
| `filter(is_in(...))` | `[1]` | ❌ |
| `join(how="semi", nulls_equal=True)` | `[1, None]` | ✅ |

三个写法给出三个不同的答案 —— 因为它们对 NULL 的处理各不相同：

- **anti join（默认）**：NULL 不等于 NULL，a 里的 NULL 留在结果里
- **is_in**：NULL 那一行算出来是 null，`filter` 把它当假值丢掉
- **nulls_equal=True**：NULL 当普通值比，NULL 被减掉 —— 这个才对得上 DuckDB

> **Polars 侧要用 `join(..., nulls_equal=True)` 才能复刻 DuckDB 的集合运算语义。**

### 3. UNION 与 UNION ALL：去重时 NULL 怎么算

| 写法 | 结果 |
| --- | --- |
| DuckDB `UNION` | `[(NULL,), (2,), (1,), (3,)]` |
| DuckDB `UNION ALL` | `[(1,), (2,), (NULL,), (1,), (NULL,), (3,)]` |
| Polars `concat().unique()` | `[1, 2, 3, None]` |
| Polars `concat()` | `[1, 2, None, 1, None, 3]` |

两边去重结果一致（✅，排序后比）。`UNION ALL` 两边都是 6 行（✅），重复的 NULL 也都保留。
但如果 Polars 侧忘了 `unique()`，NULL 就会多出一个。

列名不一致时 DuckDB 报 `Binder Error: Referenced column "y" not found in FROM clause!`，
Polars 的 `concat` 要求列名一致，否则报 schema mismatch。

### 4. 按列去重：保留哪一行，没有保证

| 写法 | 结果 |
| --- | --- |
| DuckDB `DISTINCT ON (k) ORDER BY k, v DESC` | `a→20.0`, `b→40.0`（确定） |
| DuckDB `DISTINCT ON (k)`（没有 ORDER BY） | `b→30.0`, `a→10.0`（**留哪行是任意的**） |
| Polars `unique(subset=['k'], keep='first')` | `a→10.0`, `b→30.0`（取当前行序第一行） |

DuckDB 的 `DISTINCT ON` 依赖外层 ORDER BY 决定留哪一行；Polars 的 `unique(subset=..., keep="first")` 留的是**当前行序**里的第一行，
所以必须先 `sort` 到想要的顺序，再 `unique`。

去重的其它口味：

| 写法 | 结果 |
| --- | --- |
| Polars `unique(keep='first')` | `['a', 'b']` |
| Polars `unique(keep='last')` | `['a', 'b']` |
| Polars `unique(keep='none')` | `['b']`（把重复行全删了） |
| DuckDB `SELECT DISTINCT` | 只保留一份（没有 `keep='none'` 这种口味） |

Polars 的 `unique()` 默认**不保证保留原顺序**（`maintain_order` 默认 False），
本次 first/last 两次恰好都返回 `['a','b']`，但那是运气不是保证 —— 要保留得显式写 `maintain_order=True`。
DuckDB 侧对应写法是 `GROUP BY k` + `min()/max()`，或者窗口函数 `row_number()`。

### 5. 集合运算的代价

左右各 100 万行，右表是左表去掉一些行：

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB `EXCEPT` | 39.5 | 38.7 |
| Polars anti join | **36.6** | 32.6 |

Polars 快 **1.08×** —— 差距很小。口径：输入已物化，计时的只有「集合运算 + 结果物化」。
注意 `EXCEPT` 内部要去重，而 anti join 不用 —— 两边干的活并不完全一样。

## 正则与字符串

`src/regex_demo.py`。两个引擎的正则都是 RE2 风格（没有回溯、线性时间），所以**不支持的东西是一样的**；
真正的差异全在**默认值**上 —— 默认值不一样，又都不报错。

### 1. 两边都不支持的前瞻与反向引用（这点反而是一致的）

| 正则 | 说明 | DuckDB | Polars |
| --- | --- | --- | --- |
| `a(?=b)` | 前瞻：后面必须跟着 b | ❌ `InvalidInputException` | ❌ `ComputeError` |
| `a(?!b)` | 否定前瞻 | ❌ `InvalidInputException` | ❌ `ComputeError` |
| `(a)\1` | 反向引用 | ❌ `InvalidInputException` | ❌ `ComputeError` |
| `(?<=a)b` | 后顾 | ❌ `InvalidInputException` | ❌ `ComputeError` |
| `(?i)ABC` | 内联标志：忽略大小写 | ✅ | ✅ |
| `[[:alpha:]]+` | POSIX 字符类 | ✅ | ✅ |

报错类型不一样，结论一样：前后瞻、后顾、反向引用一律不支持。

### 2. regexp_extract / str.extract：分组编号一致，但「匹配不上」不一样

两边的**分组编号口径一致**：0 = 整个匹配，1 = 第一个括号组（样例 8 行里匹配上 1 行，组 1 两边一致 ✅）。

但匹配不上时表示法不同：

| | 匹配不上时 |
| --- | --- |
| DuckDB `regexp_extract` | `('', '')` —— **空字符串** |
| Polars `str.extract` | `{'g1': None, 'g9': None}` —— **null** |

> 写 `= ''` 和写 `IS NULL` 会得到相反的结果。
> 另外 DuckDB 的组号越界（这里是 9）也返回空串而不是报错。

「找全部」：DuckDB `regexp_extract_all('a1b2c3')` → `['1','2','3']`；
Polars `str.extract_all` → `[['1','2','3']]`（**列表列**，多包了一层）。

### 3. 「整串匹配」和「子串匹配」

样本 `'ORD-2024-001'`，模式 `[A-Z]{3}-[0-9]{4}-[0-9]{3}`：

| 写法 | `'ORD-2024-001'` | `'xxORD-2024-001yy'` |
| --- | --- | --- |
| DuckDB `regexp_full_match` | True | **False** |
| DuckDB `regexp_matches`（子串） | True | True |
| Polars `str.contains`（子串） | True | True |
| Polars `str.contains("^…$")` | True | **False** |

**Polars 2.1 没有 `str.fullmatch`**（`hasattr` 实测 `False`）—— 整串匹配只能自己加 `^` 和 `$` 锚定。
DuckDB 没有 `regexp_contains` 这种「只判断不返回」的名字，用 `regexp_matches`（部分）或 `regexp_full_match`（整串）。

### 4. 替换：只换第一个，还是全换

样本 `'a1b2c3'`，模式 `[0-9]`，替换成 `#`：

| 写法 | 结果 |
| --- | --- |
| DuckDB `regexp_replace`（默认） | `'a#b2c3'`（只换第一个） |
| DuckDB `regexp_replace(..., 'g')` | `'a#b#c#'` |
| Polars `str.replace`（默认 `n=1`） | `'a#b2c3'` |
| Polars `str.replace_all` | `'a#b#c#'` |

默认行为两边一致，但开关的位置不同：DuckDB 在第 4 个参数里传 `'g'`，Polars 用另一个函数名。

「按正则」与「按字面量」：

| 写法 | 结果 |
| --- | --- |
| DuckDB `replace()`（字面量） | `'a-c'` |
| DuckDB `regexp_replace()`（正则） | `'-.c'` |
| Polars `str.replace(literal=True)` | `'a-c'` |
| Polars `str.replace`（默认 = **正则**） | `'-.c'` |

DuckDB 是两个函数分开（`replace` / `regexp_replace`），Polars 是**一个函数 + 默认走正则** —— 照名字翻译会串。

### 5. 切分与位置查找

样本 `'a1b2c'`，按 `[0-9]` 切分：

| 写法 | 结果 |
| --- | --- |
| DuckDB `string_split()` | `['a1b2c']`（字面量） |
| DuckDB `regexp_split_to_array()` | `['a','b','c']`（正则） |
| Polars `str.split()` | `['a1b2c']`（**字面量！**） |
| Polars `str.split(literal=False)` | `['a','b','c']`（正则） |

> **这是最容易踩的一个**：Polars 的 `str.split` 默认 `literal=True`，
> 拿 `'[0-9]'` 去切等于找一个叫 `'[0-9]'` 的子串，切不动。

找位置：

| 写法 | 结果 |
| --- | --- |
| DuckDB `strpos('ORD-2024-001','2024')` | `5`（字面量，**1 起数**） |
| DuckDB `strpos('ORD-2024-001','[0-9]{4}')` | `0`（字面量找 `'[0-9]{4}'`，找不到） |
| DuckDB `regexp_instr` | **不存在**（`CatalogException`） |
| Polars `str.find('[0-9]{4}')` | `4`（**字节偏移，0 起数**） |

DuckDB 没有正则版的位置查找；Polars 的 `str.find` 走正则但返回字节偏移。差的不只是 1，中文场景还差一个字节/字符。

### 6. LIKE：通配符与转义

| 值 | DuckDB `LIKE 'a_c'` | DuckDB `LIKE 'a%c'` | DuckDB `ILIKE 'abc'` |
| --- | --- | --- | --- |
| `'abc'` | ✅ | ✅ | ❌ |
| `'a_c'` | ✅ | ✅ | — |
| `'a%c'` | ❌ | ✅ | — |
| `'ABC'` | ❌ | ❌ | ✅ |

`LIKE` 的 `'_'` 匹配**恰好一个字符**（`'abc'` 也匹配 `'a_c'`）。DuckDB 用 `ESCAPE '\'` 转义下划线。
**Polars 没有 LIKE**，常用正则代替 —— 但正则的 `.` 也只匹配一个字符（不是多字节字符），中文上要小心。
大小写：Polars 侧没有 `ILIKE`，只能用 `(?i)` 正则。

### 7. 中文、全角、Emoji：按字符还是按字节

DuckDB `length()` / `strlen()` 与 Polars `str.len_chars()` / `str.len_bytes()` **完全一致**（✅ 8 行全对）：

| 文本 | 字符数 | 字节数 |
| --- | --- | --- |
| `'emoji 😀'` | 7 | 10 |
| `'全角ＡＢＣ'` | 5 | 15 |
| `'小米手机'` | 4 | 12 |
| `'Apple iPhone 15'` | 15 | 15 |

一个 Emoji 在字符口径下算 1 个字符、UTF-8 下算 4 个字节；Python 自己的 `len()` 也是字符数。

**但位置口径不一样（这条最容易错）**：

| 写法 | 结果 |
| --- | --- |
| DuckDB `strpos('订单1','1')` | `3`（第几个**字符**，1 起数） |
| Polars `str.find('1')` | `6`（第几个**字节**，0 起数） |

切片两边倒是都按字符：`substr('订单😀', 2, 2)` 与 `str.slice(1, 3)` 都得到 `'单😀'`。

### 8. 正则的代价

50 万行订单号：

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB | 46.5 | 44.2 |
| Polars | **27.3** | 27.2 |

Polars 快 **1.71×**。口径：输入已物化，计时的只有「正则提取 + 结果物化」。

## 增删改（DML）

`src/dml_demo.py`。DuckDB 是「表」，Polars 是「不可变的 DataFrame」——
这不只是写法差异，它决定了能不能事务、能不能 upsert、能不能原子地改数据。

### 1. 两种数据模型

| | DuckDB | Polars |
| --- | --- | --- |
| 改数据 | `UPDATE` 直接改表，**状态持久** | `with_columns` 返回**新对象**，原对象一点没变 |
| 事务 | 有（`BEGIN`/`COMMIT`/`ROLLBACK`） | **没有事务这个概念** |
| upsert | `ON CONFLICT DO UPDATE` | 没有 |
| 插入单行 | `INSERT` | 没有 `insert_row` |

Polars 侧的 `DataFrame.update` 存在，但 docstring 里写着 **unstable**。

### 2. Polars 的 `DataFrame.update`：一个静默改坏数据的坑

| 坑 | 实测 |
| --- | --- |
| 更新方里有**重复键** | 行数从 4 变成 **5**，id=2 出现了两次 |
| 更新方里有 df 中**不存在的键** | 被**静默忽略**（左连接语义，不是报错） |
| API 稳定性 | docstring 里出现 `unstable` |

DuckDB 那边不会出现这种行数变化：`UPDATE` 只改已有的行，不会复制行；想插新行必须走 `INSERT`，
主键冲突会被约束直接拒掉（`Constraint Error: Duplicate key "id: 3" violates primary key constraint.`）。

### 3. DELETE 和 INSERT：SQL 有语句，Polars 得拼

| 引擎 | 写法 | 结果 |
| --- | --- | --- |
| DuckDB | `DELETE FROM sales WHERE id=3` | 剩 id 1,2,4 |
| DuckDB | `INSERT INTO sales VALUES (...)` | 插回 4 行 |
| Polars | `join(how="anti")` | 剩 id 1,2,4 |
| Polars | `concat` | 插后 5 行 |

两边的结构要求也一致：Polars `concat` 要求列完全一致（名字和类型都要一致），
否则报 `ShapeError: unable to append to a DataFrame of width 3 with a DataFrame of width 2`；
DuckDB 的 `INSERT` 报 `Binder Error: table "sales" has 3 columns but 1 values were supplied`。

### 4. upsert：ON CONFLICT DO UPDATE vs 手工拼

| 写法 | 结果 |
| --- | --- |
| DuckDB `INSERT ... ON CONFLICT DO UPDATE` | `[(1,10.0),(2,200.0),(3,300.0),(4,400.0)]` |
| Polars 手工拼（anti join + 左连接 coalesce） | 同上 ✅ |

`DO NOTHING` 口味（DuckDB 有，Polars 拼起来更绕）：

| 写法 | 结果 |
| --- | --- |
| DuckDB `ON CONFLICT DO NOTHING` | `[(1,10.0),(2,20.0)]` |
| Polars 拼（保留原表 + 只并入新键） | 同上 ✅ |

> `DO NOTHING` 的语义是「**冲突的保留旧值，新的照插**」，不是「整批不插」—— 用 `unique(keep='first')` 凑是凑不出来的。

DuckDB 还有一条 Polars 完全没有的路：`UPDATE ... FROM`（拿另一张表来改），
实测 `UPDATE u ... FROM adj` 得到 `[(1,110.0),(2,200.0),(3,600.0),(4,400.0)]`。
Polars 侧只能自己 join 出要改的行再 update。

### 5. UPDATE 里的子查询：按更新前的快照算一次

| | 值 |
| --- | --- |
| 更新前 `sum(v)` | 499500 |
| 更新后 `sum(v)` | 499999500 |
| 若每行都加上更新前的和 | `499500 + 1000 × 499500 = 499999500` |
| 实测 | ✅ **等于该值** |

这是「**Halloween 问题**」：经典的 `UPDATE ... SET x = x + (SELECT sum(x) ...)` 会因为边算边改而算错。
DuckDB 在这里按更新前的快照算一次，所以结果是对的。
Polars 侧没有 UPDATE 语句，等价写法是纯表达式，天然没有这个问题：`(s + s.sum()).sum()` = 499999500。

### 6. RETURNING 和事务

`RETURNING` 把改动过的行直接返回，不用再查一遍：

| 写法 | 结果 |
| --- | --- |
| DuckDB `UPDATE ... RETURNING` | `[(1, 106.0)]`（只返回**被改动**的行） |
| DuckDB `DELETE ... RETURNING` | `[(4, 'delta')]` |
| Polars | 所有操作都返回新 DataFrame，天然就是「RETURNING」—— 但语义不同 |

事务实测：

| 序列 | 结果 |
| --- | --- |
| `BEGIN` → `UPDATE` → `ROLLBACK` | `[(1,)]`（改动被丢掉） |
| `BEGIN` → `UPDATE` → `COMMIT` | `[(3,)]`（改动生效） |

Polars 侧带 commit/rollback/transaction 的方法：`[]`。
Polars 的 `LazyFrame` 有 collect/写文件，但那是「**物化**」，不是「事务」——
需要原子性就得自己在外面套（写临时文件 → 原子替换）。

### 7. 改完之后行序还在不在

DuckDB `UPDATE` 10 万行后前 5 行的 `i` = `[0, 1, 2, 3, 4]`（行序保持住了 ✅）。
但 SQL 本身不承诺行序 —— 换个查询计划、换个版本都可能变，所以正式查询一律要带 `ORDER BY`。

Polars 侧结论不一样：行序是数据的一部分，但要小心哪些算子会重排。

| 算子 | 行序 |
| --- | --- |
| 原始 | `['b','a','b']` |
| `group_by` 之后 | `['a','b']` ← **重排了** |
| `update` 之后 | `['b','a','b']` ← 保持 |

> Polars 里「行序」不是查询的副产品，而是要自己用 `sort` 固定的。
> `join` 默认也不保证行序，要固定得显式写 `maintain_order="left"`。

### 8. 批量 UPDATE 的代价

100 万行，其中 1/3 会被改：

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB `UPDATE` | 11.5 | 11.0 |
| Polars 表达式 | **4.3** | 4.0 |

**这两个数字不能直接比大小**：

- DuckDB 的 `UPDATE` 包含**写回存储**：读旧块 → 改 → 写新块（含 WAL）
- Polars 这边只是算出一个新 DataFrame，**没有写回任何东西**
- 表的重建（`CREATE OR REPLACE TABLE`）在计时之外

真正该比的量是「同样的一件事」，而不是「两种模型各自的代价」：
DuckDB 写完的数据在磁盘上，Polars 的结果只在内存里。

## Schema 演进

`src/schema_demo.py`。改 schema 在两个引擎里是两件完全不同的事：DuckDB 改的是磁盘上的表，Polars 造的是内存里的新对象。

### 1. ALTER TABLE vs 造新表

| 操作 | DuckDB | Polars |
| --- | --- | --- |
| 加列 | `ALTER TABLE t ADD COLUMN amount DOUBLE DEFAULT 0` | `with_columns(pl.col('id') * 10).alias('id10')` |
| 删列 | `ALTER TABLE t DROP COLUMN amount` | `drop('name')` |
| 改名 | `ALTER TABLE t RENAME COLUMN name TO label` | `rename({'name': 'label'})` |
| 改类型 | `ALTER TABLE t ALTER COLUMN label TYPE VARCHAR` | `with_columns(pl.col('id').cast(pl.Int64))` |

DuckDB 改完表就落盘了，进程退出数据还在；Polars 的 DataFrame 只活在内存里，
除非 `write_parquet` / `write_database`。Polars 侧的 `alter` 类 API：`[]`（一个都没有）。

### 2. 标识符大小写：DuckDB 不敏感，Polars 敏感

| 写法 | 结果 |
| --- | --- |
| DuckDB `SELECT ID, NAME FROM c`（表建的是 `Id`/`Name`） | ✅ 查得到 |
| Polars `df.select("ID")`（列名是 `Id`） | ❌ `ColumnNotFoundError` |
| Polars `df.select("Id")` | ✅ |

DuckDB 的不敏感只针对**标识符**（表名、列名），字符串常量照常区分大小写：`SELECT 'ID' = 'id'` → `False`。

> 迁移时最容易踩的一类：上游给的字段名是 `User_ID` / `user_id` / `USER_ID`，
> DuckDB 一视同仁，Polars 一字不差。写 SQL 和写 Polars 表达式时得**各自记一份**。

### 3. 类型转换失败：报错还是填 NULL

| 写法 | 行为 |
| --- | --- |
| DuckDB `'abc'::INTEGER` | ❌ 报错 |
| DuckDB `TRY_CAST('abc' AS INTEGER)` | ✅ → NULL |
| Polars `cast(pl.Int64)`（默认 strict=True） | ❌ 报错 |
| Polars `cast(pl.Int64, strict=False)` | ✅ → NULL |

两边的「宽松转换」开关都在，但**默认都是报错** —— 这是好事，静默变 NULL 更容易坑人。

### 4. 读一批 schema 不一致的 parquet

三种情况 × 四种配置：

| 情况 | DuckDB 默认 | DuckDB `union_by_name=true` | Polars 默认 | Polars `missing_columns='insert'` |
| --- | --- | --- | --- | --- |
| 后一个文件**少**一列 | ❌ `InvalidInputException` | ✅ 缺的填 NULL | ❌ 报错 | ✅ 缺的填 NULL |
| 后一个文件**多**一列 | ⚠️ **静默丢掉**多出来的那列 | ✅ 并起来 | ❌ 报错 | ❌ 仍报错 |
| 同一列**类型**不同 | ❌ `ConversionException` | ✅ 提升成 VARCHAR | ❌ 报错 | ❌ 仍报错 |

三条要记住的：

1. DuckDB 默认遇到「少一列」会报错，但遇到「多一列」是**静默丢掉**多出来的那列 —— 最危险的不是报错，是不报错。
2. Polars 默认两种情况都报错，并且错误信息会提示该用哪个参数。
3. 两边的「放宽」开关**不是同一个**：DuckDB 是 `union_by_name=true`（一个开关管缺列 + 多列 + 类型），
   Polars 是 `missing_columns='insert'`（只管缺列；多列和类型不一致仍然报错）。

### 5. 拼表：Polars 的 concat 默认严格，DuckDB 会隐式转类型

| 情况 | DuckDB | Polars 默认 | Polars 放宽 |
| --- | --- | --- | --- |
| `INTEGER` + `DOUBLE` | ✅ 自动提升到 DOUBLE | ❌ 报错 | `how="diagonal_relaxed"` ✅ |
| 列名不同 | ✅ 取第一个 SELECT 的列名 | ❌ `schema mismatch` | `how="diagonal"` ✅（缺的填 NULL） |

### 6. 改 schema 的代价

100 万行两列表：

| 操作 | 中位数 (ms) |
| --- | --- |
| DuckDB `ALTER ADD COLUMN` | 5.62 |
| DuckDB `ALTER DROP COLUMN` | **0.37** |
| DuckDB `ALTER COLUMN TYPE` | 5.01 |
| Polars 加常量列 `pl.lit` | **0.11** |
| Polars 加计算列 `v*2` | 1.40 |
| Polars `cast` 一列 | 0.09 |
| Polars `drop` 一列 | 0.09 |

Polars 加常量列比加计算列快 **13.0×**，因为 `pl.lit` 是**广播**，底层只存一个标量。

**这两组数字量的不是同一件事**：DuckDB 量的是「持久化改表」（要写元数据、写 WAL，改类型还要重写整列），
Polars 量的是「内存里造一个新表」（不落盘）。

## 时间轴与序列生成

`src/timeseries_demo.py`。生成序列、补齐空档、按粒度截断 —— 这几件事两个引擎都有 API，
但**端点算不算、类型是什么、覆盖到哪**，默认值全不一样。

### 1. 序列的端点算不算 —— 同一句「1 到 5」，三种答案

| 写法 | 终点包含？ | `1` 到 `5` 的结果 |
| --- | --- | --- |
| DuckDB `generate_series(1, 5, 1)` | ✅ **含** | `[1,2,3,4,5]` |
| DuckDB `range(1, 5, 1)` | ❌ 不含 | `[1,2,3,4]` |
| Polars `int_range(1, 6)` | ❌ 不含 | `[1,2,3,4,5]` |
| Polars `date_range(2024-01-01, 2024-01-05, '1d')` | ✅ **含** | 5 个日期 |

三条要记住的：

- DuckDB 内部就有两套：`generate_series` 含终点，`range` 不含。
- Polars 的 `int_range` 不含终点，`date_range` 含终点 —— **同一套 API 里两个口径**。
- 迁移时最容易漏的就是这个差一：`int_range(1, 5)` 只有 4 个数，
  要和 DuckDB 的 `generate_series(1, 5, 1)` 对齐，得写 `int_range(1, 6)`。

### 2. 两边各有哪些时间类型

| DuckDB | Polars |
| --- | --- |
| `DATE` | `Date` |
| `TIMESTAMP` | `Datetime(time_unit='us'\|'ns', time_zone=None)` |
| `TIMESTAMP WITH TIME ZONE` | `Datetime(time_unit='us', time_zone='Asia/Shanghai')` |
| `INTERVAL` | `Duration(time_unit='us')` |

DuckDB 的 `generate_series` 在时间上有个坑：**起点是 DATE 时结果是 TIMESTAMP**（不是 DATE）。
和 DATE 列 JOIN 时会走隐式类型转换，能对上但不报错 —— 别以为两边类型一样。

时区换算（同一个瞬时点，两边都能换算）：

| 写法 | 结果 |
| --- | --- |
| DuckDB `AT TIME ZONE 'UTC'` | `2024-01-01 00:00:00` |
| DuckDB `AT TIME ZONE 'Asia/Shanghai'` | `2024-01-01 08:00:00` |
| Polars `replace_time_zone('UTC')` | `2024-01-01 00:00:00+00:00` |
| Polars `convert_time_zone('Asia/Shanghai')` | `2024-01-01 08:00:00+08:00` |

DuckDB 叫 `AT TIME ZONE`，Polars 叫 `convert_time_zone`；`replace_time_zone` 是「贴标签」，`convert_time_zone` 才是「换显示」。

### 3. 补齐缺失的时间段：upsample 只补「中间」

原始数据只有 `2024-01-01`（v=1）和 `2024-01-04`（v=4）：

| 写法 | 补出来的范围 | 行数 |
| --- | --- | --- |
| DuckDB 生成完整轴 + LEFT JOIN | 01-01 ~ **01-05** | 5 |
| Polars `upsample` | 01-01 ~ **01-04** | **4** |
| Polars 自己 join 一条完整轴 | 01-01 ~ 01-05 | 5 ✅ |

`upsample` 只补了 01-01 到 01-04 **中间**的空档，最后一格 01-05 没有 —— 数据里没有，`upsample` 也不造。

> 两条路：Polars 的 `upsample` 更快（不用自己造轴），但「补齐的时间轴范围」得自己定；
> DuckDB 那边范围本来就是 SQL 的一部分。

### 4. 按粒度截断：month_start 的名字骗人

起点 `2024-03-15 13:45:30`：

| 写法 | 结果 |
| --- | --- |
| DuckDB `date_trunc('month', …)` | `2024-03-01 00:00:00` |
| DuckDB `date_trunc('day', …)` | `2024-03-15 00:00:00` |
| Polars `dt.truncate('1mo')` | `2024-03-01 00:00:00` |
| Polars `dt.truncate('1d')` | `2024-03-15 00:00:00` |
| Polars `dt.month_start()` | `2024-03-01 13:45:30` ← **时分秒还在！** |

`truncate` 会把时分秒清零；`month_start` 只把日期挪到 1 号，时间原样保留。
要「当月第一天零点」得用 `truncate('1mo')`，**别用 `month_start`**。

Date 列上截断，返回类型还不一样：

| | 结果 | 类型 |
| --- | --- | --- |
| DuckDB `date_trunc('month', DATE)` | `2024-03-01 00:00:00` | **TIMESTAMP**（DATE 被提升） |
| Polars `dt.truncate('1mo')` on Date | `2024-03-01` | **Date** |

拿结果去跟 DATE 列 JOIN 时行为会不一样（一个要隐式转换，一个正好匹配）。

### 5. 间隔加减：月份在 Polars 里不是 Duration

起点 `2024-03-15 13:45:30`：

| 写法 | 结果 |
| --- | --- |
| DuckDB `TIMESTAMP + INTERVAL 1 DAY` | `2024-03-16 13:45:30` |
| DuckDB `TIMESTAMP + INTERVAL 1 MONTH` | `2024-04-15 13:45:30` |
| Polars `x + pl.duration(days=1)` | `2024-03-16 13:45:30` |
| Polars `x + pl.duration(months=1)` | ❌ `TypeError: duration() got an unexpected keyword argument 'months'` |
| Polars `x.dt.offset_by('1mo')` | `2024-04-15 13:45:30` |

> 「加一个月」在 Polars 里必须走 `dt.offset_by()` —— Duration 只有天/时/分/秒这些固定单位，
> 月份长度不固定，进不了 Duration。DuckDB 的 `INTERVAL 1 MONTH` 直接就是日历月。

两点之差两边一致：`date_diff('day', '2024-01-01', '2024-03-01')` = 60，
`(b - a).dt.total_days()` = 60。

### 6. 时间轴的代价

100 万行事件（每行一个时间戳）：

**按天聚合**

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB | **10.5** | 9.9 |
| Polars | 24.7 | 22.0 |

DuckDB 快约 **2.3×**（DuckDB / Polars = 0.43×）。

**生成 100 万个小时点的时间轴**

| 引擎 | 中位数 (ms) | 最快 (ms) |
| --- | --- | --- |
| DuckDB `generate_series` | 17.6 | 17.3 |
| Polars `datetime_range` | **4.0** | 3.8 |

Polars 快 **4.45×**。口径：输入已物化，计时的只有「按天聚合 + 结果物化」和「生成时间轴」。
注意 DuckDB 的 `generate_series` 是**列表函数**，聚合时还得再 `unnest` 一次，
这里为了两边做同一件事，用 `count(*)` 只测生成本身的成本。

## 近似计算 vs 精确

`src/approx_demo.py`。两个引擎都有 HyperLogLog（distinct 计数）和分位数 sketches。
省内存的代价是「**答案不保证对**」—— 而且错的方向不固定。

### 1. 同一个问题，两个答案

100 万行、全部互不相同 → 正确答案就是 1,000,000：

| 写法 | 结果 | 误差 |
| --- | --- | --- |
| DuckDB `count(DISTINCT id)` | 1,000,000 | 精确 |
| DuckDB `approx_count_distinct(id)` | 982,272 | **-1.77%**（低估） |
| Polars `n_unique()` | 1,000,000 | 精确 |
| Polars `approx_n_unique()` | 1,008,747 | **+0.87%**（高估） |

两个引擎的近似值都不是「精确值的小数版本」，而是**另一个数**。
HLL 是概率结构，**没有「只多不少」的保证** —— 别指望近似值是个上界。

### 2. 误差有多大：换个基数再看一遍

| 基数 | DuckDB 近似值 | Polars 近似值 |
| --- | --- | --- |
| 1,000 | 978（-2.20%） | 1,002（+0.20%） |
| 100,000 | 99,467（-0.53%） | 99,974（-0.03%） |
| 1,000,000 | 982,272（-1.77%） | 1,008,747（+0.87%） |

结论不是「谁更准」，而是：

- 两个引擎的近似 distinct 误差都在**百分之几以内**，量级差不多。
- **方向不固定**：这次 DuckDB 低估、Polars 高估，换个基数可能反过来。
- **误差不随基数单调变小** —— 「基数够大就准」是常见印象，这三组数据不支持。

> 拿近似值做「是否超过某个阈值」的判断要留余量；
> 拿它和另一个近似值比大小则更危险（两个误差可能不同向）。

### 3. 近似分位数：误差是「值」上的，不是「排名」上的

| 分位 | 精确值 | DuckDB 近似 | Polars 近似 |
| --- | --- | --- | --- |
| 0.01 | 10000.0 | 9999 | 9996 |
| 0.1 | 99999.9 | 100000 | 100256 |
| 0.5 | 499999.5 | 504556 | 500075 |
| 0.9 | 899999.1 | 899301 | 900031 |
| 0.99 | 989999.0 | 990000 | 989996 |

近似分位数返回的是一个**具体的值**，但这个值可能不在原始数据里
（DuckDB 的 t-digest 会插值，Polars 的 KLL 返回采样出来的原值）。

这一轮的分位数最大误差：DuckDB **-0.91%**，Polars **-0.26%**；
同一批数据上 distinct 计数的误差是 **-1.77%**。

**sketch 是随机的**：同一个查询连跑两次，数字也会变。
所以别拿两次运行的近似值对账，也别指望某个分位一定比另一个准。

Polars 单独提供 `req_lo` / `req_hi` 两种算法，是给尾部用的 ——
默认的 `auto` 会在分位落在 `[0.05, 0.95]` 之外时自动切算法。

### 4. 分布统计：histogram 和 value_counts

| | DuckDB `histogram(b)` | Polars `value_counts()` |
| --- | --- | --- |
| 返回 | MAP：`{桶: 计数}` | 两列的表：值 + 计数 |
| 本次 | 10 个桶，各 10000 | 10 行，各 10000 |

两边都能给出**精确**的频数分布（DuckDB 的 `histogram` 在高基数时才是抽样近似）。
Polars 没有 `histogram` 函数，但 `value_counts` + `group_by` 一样能用。

想要「精确的 top-k」就别用近似函数 —— 近似函数省的是**内存**，不是时间。

### 5. 值不值：省了多少时间，换了多少精度

500 万行，值域 250 万（每个值平均出现两次），真实基数 2,500,000：

| 实现 | 中位数 (ms) | 结果 | 误差 |
| --- | --- | --- | --- |
| DuckDB `count(DISTINCT)` | 54.7 | 2,500,000 | +0.00% |
| DuckDB `approx_count_distinct` | **4.6** | 2,416,105 | **-3.36%** |
| Polars `n_unique()` | 55.7 | 2,500,000 | +0.00% |
| Polars `approx_n_unique()` | 28.3 | 2,518,495 | +0.74% |

DuckDB 的近似计数比精确快约 **11.9×**，Polars 的快约 **2.0×**。

- 精确计数的成本随**基数**（不是行数）增长，内存放不下就得退化。
- 近似计数是常数内存，代价是答案有误差。
- 两个引擎的近似实现不同（都是 HLL，但参数和修正策略不一样），误差方向也不同 —— 别把两个近似值直接比大小。
- 真要精确，就把基数控制在能放下的范围，别拿近似值凑。

## 数值与算术边界

`src/arith_demo.py`。同一个算式，两个引擎给出**不同的数** —— 而且多数是「静默地不一样」。
这一节讲的是：哪些差异会**算错数**，哪些会**安静地算错**。

### 1. 除法：DuckDB 的 `/` 不是整数除法

| 表达式 | DuckDB | Polars |
| --- | --- | --- |
| `7 / 2` | 3.5 | 3.5 |
| `7 // 2` | 3 | 3 |
| `-7 / 2` | -3.5 | -3.5 |
| `-7 // 2` | **-3** | **-4** |
| `7 % 2` | 1 | 1 |
| `-7 % 2` | **-1** | **1** |

`SELECT typeof(7/2)` 是 **DOUBLE**、`typeof(7//2)` 是 **INTEGER** ——
DuckDB 的 `/` 永远是浮点。从 Postgres/MySQL 迁过来的人最容易在这里错：
Postgres 的 `7/2` 是 `3`，DuckDB 是 `3.5`。

两边的 `//` 和 `%` 对**负数**的定义也不同：

- DuckDB 向**零截断**（跟 C/C++/Java 一致）：`-7//3` = -2，`-7%3` = -1
- Polars 向**下取整**（跟 Python 一致）：`-7//3` = -3，`-7%3` = 2

→ 负数参与整除/取模时，两边的结果**不是差 1 的关系，是完全不同的一套数**。

### 2. 舍入：2.5 到底进位还是平局

| 待舍入值 | DuckDB `round` | Polars `round` |
| --- | --- | --- |
| 0.5 | 1 | **0** |
| 1.5 | 2 | 2 |
| 2.5 | **3** | **2** |
| 3.5 | 4 | 4 |
| -0.5 | -1 | **-0** |
| -1.5 | -2 | -2 |
| -2.5 | **-3** | **-2** |

两边用的是**两套舍入规则**：DuckDB 是**四舍五入（half away from zero）**，
Polars 是**银行家舍入（half to even）**。前者对齐 Excel 的 `ROUND`，后者对齐 Python 内置 `round`。

→ 这不是谁对谁错，是**两边各自对齐了不同的「事实标准」**。
但账单、金额汇总这类场景，差 1 分就是差 1 分 —— 迁移时必须显式选一边。

上取整 / 下取整两边一致（`ceil(-2.1)` = -2，`floor(-2.1)` = -3）。

### 3. 整数溢出：DuckDB 报错，Polars 悄悄绕回

```
DuckDB: SELECT 2147483647 + 1
  -> OutOfRangeException: Out of Range Error: Overflow in addition of INT32 (2147483647 + 1)!

Polars: pl.Series([2147483647], dtype=pl.Int32) + 1
  -> -2147483648        （正确答案 2147483648）
```

Int64 同理（`9223372036854775807 + 1` → `-9223372036854775808`），UInt8 也一样（255 + 1 → 0）。

这是本 demo 里**最危险**的一条：

- DuckDB **报错** —— 错在最早的地方，堆栈直接指到出问题的表达式。
- Polars **静默绕回**（wrap-around）—— 错得很远：加法没错，**求和之后**才发现总额是负的。

实务影响：金额/计数用 Int32 累加，跑到 21 亿就翻负；用 Int64 累加，同一个洞在更远处等着。
想要「溢出即报错」的语义，Polars 侧要么改用 Decimal，要么在写库之前自己检查量级 —— 没有开关能把它变成 DuckDB 那样。

### 4. 除以零：一边报错，一边给你 NULL

| 表达式 | DuckDB | Polars |
| --- | --- | --- |
| `1 / 0` | inf | inf |
| `0.0 / 0.0` | nan | nan |
| `1 // 0` | **InvalidInputException（报错）** | **None** |
| `1 % 0` | **InvalidInputException（报错）** | **None** |

DuckDB 的报错原文：

> Invalid Input Error: Division by zero in expression (1 // 0). Use TRY(...) to return NULL for
> this expression, or SET error_on_division_by_zero=false to return NULL for all divisions by zero.

→ 三种「除零」语义并存：浮点除法两边都给 inf/nan（符合 IEEE 754）；
**整数除法 DuckDB 报错，Polars 给 NULL**。
Polars 的 NULL 会一路传播，最后在某个不相干的地方变成「某列全是空」。

迁移时最容易漏的就是这个：**DuckDB 会当场炸，Polars 会安静地传播** ——
代码在 DuckDB 上跑得好好的，改成 Polars 之后不报错了，但结果是错的。

顺带一提 NaN：两边都认为 `nan = nan` 为 True、`nan > 1` 为 True —— 一致，但**都不是** IEEE 语义。

### 5. 截断取整：DuckDB 有 trunc，Polars 没有

DuckDB 有 `trunc(x)` / `trunc(x, n)`（`trunc(2.7)` = 2，`trunc(-2.7)` = -2）；
Polars **没有**同名函数。最接近的是 `cast(pl.Int64)`（**向零**截断）或 `floor()`（向下）——
负数上两者不一样（`floor(-2.7)` = -3）。

### 6. 算术的代价：取模比加减贵多少

200 万行整数（`sum(v)` 的真值两边一致）：

| 实现 | 中位数 (ms) | 最快 (ms) | 结果 |
| --- | --- | --- | --- |
| DuckDB `sum(v)` | 1.3 | 1.3 | 1,000,001,983,749 |
| DuckDB `sum(v % 7)` | 4.0 | 3.3 | 5,999,986 |
| Polars `sum(v)` | 1.0 | 0.8 | 1,000,001,983,749 |
| Polars `(v % 7).sum()` | 7.5 | 7.5 | 5,999,986 |


口径：输入已物化成 parquet，两边读的是同一份数据；计时的只有「对这一列做一次聚合并求和」
这一步（不含读盘）。重复次数取中位数，机器是共享开发机，单次数字会有抖动。

解读：

- 取模是**不可省的整数运算**，比单纯求和贵一截 —— 差的这部分就是 `%` 本身的成本，两边都要付。
- 真正的差别在**语义**：DuckDB 的 `%` 会检查溢出，Polars 不会。
  Polars 少做的那点检查，换来的是场景 3 里那个静默绕回。
- 所以别为了「快一点」去 Polars 那边省溢出检查 —— 那个洞在 21 亿次累加之后才爆，
  到时候查起来比慢一点贵多了。

## 多维聚合：GROUPING SETS / ROLLUP / CUBE

`src/groupsets_demo.py`。DuckDB 原生支持多维小计，**Polars 完全没有** ——
`group_by` 只有一层，想要「明细 + 小计 + 总计」得自己拼。

### 1. 先把共同语言定下来

同样的 `GROUP BY region` / `group_by('region')`，两边结果**完全一致**（对账 ✅ 3 行 × 3 列）。
连类型都有小差别（DuckDB 的 `sum(INTEGER)` 是 HUGEINT，落到 Arrow 是 DECIMAL；Polars 保持 Int64），
但数值口径一样。

所以下面所有差异，**都只来自「多级」这两个字**，不是聚合本身的事。

### 2. ROLLUP：一条 SQL 出三层小计

`GROUP BY ROLLUP(region, category)` 在 7 行明细上输出 **11 行** = 7 行明细 + 3 行地区小计 + 1 行总计，
用到的 grouping set 组合是 **3 种**：`[(region, category), (region), ()]`。

- n 个维度列 → **n+1** 个 grouping set（1 列 2 个、2 列 3 个、3 列 4 个）。
- **输出行数由数据决定**（这里是 11 行），不是跟数据无关的固定值。
- **小计行靠 NULL 占位**：地区小计那行 `category` 是 NULL，总计那行两列都是 NULL。

### 3. CUBE：比 ROLLUP 多出来的那一层交叉小计

`CUBE(region, category)` 是 **4** 个 grouping set（`(a,b)`、`(a)`、`(b)`、`()`），
n 个维度列 → **2^n** 个 set。同一张表输出 **15 行** —— 比 ROLLUP 多的 4 行是「品类小计」：
`region` 是 NULL、`category` 有值（每个品类全国总共卖了多少）。ROLLUP 里根本没有这一层。

行数会膨胀：明细 7 行 → ROLLUP 11 行 → CUBE 15 行。维度越多，CUBE 的组数**指数级增长**
（3 个维度 8 组、4 个维度 16 组）—— 所以报表里很少直接用 CUBE，而是先想清楚要哪几组。

### 4. GROUPING SETS：自己点名要哪几组

`GROUP BY GROUPING SETS ((region), (category))` 只要「地区小计」和「品类小计」，
**不要明细、不要总计** —— 输出 7 行、2 组。

它是 ROLLUP / CUBE 的**底层原语**（ROLLUP 就是特殊的 GROUPING SETS）。学会这一种，三种就都懂了。

### 5. GROUPING()：占位 NULL 和真 NULL，长得一模一样

这是本 demo 里**最反直觉**的一条。造一张 `region` 和 `category` 都真有 NULL 的表：

| 层级 | region | category | amount |
| --- | --- | --- | --- |
| 明细 | 华东 | 数码 | 100 |
| 明细 | NULL | 数码 | 50 |
| 明细 | NULL | NULL | 7 |
| 地区小计 | 华东 | NULL | 100 |
| 地区小计 | NULL | NULL | 57 |
| 总计 | NULL | NULL | 157 |

**三行的 region 和 category 都是 NULL**，分别是明细行、地区小计、总计 —— 光看数据分不出来。

用 `WHERE region IS NULL` 去捞小计行更糟：会捞出 **4 行**（2 行明细 + 1 行地区小计 + 1 行总计），
**该排除的明细行一条没排掉**。

`GROUPING(g)` 才能分清（返回 BIGINT 的 0/1）：

| 层级 | `grouping(region)` | `grouping(category)` |
| --- | --- | --- |
| 明细 | 0 | 0 |
| 地区小计 | 0 | 1 |
| 总计 | 1 | 1 |

→ `GROUPING(g)` 回答的是「这行的 g 是**占位符**吗」，不是「g 是不是 NULL」。
真的 NULL 值 → `GROUPING()` = 0（它就是个普通的分组）；小计占位 → `GROUPING()` = 1。

**Polars 没有 `GROUPING()`**。这不是「少个函数」的问题，是**信息本身丢了** —— 占位信息在拼 `concat`
的那一刻就没了，只能自己额外带一列 `level` 标记。

### 6. Polars 侧：手工复刻 ROLLUP

```python
cols     = ["region", "category", "amount"]
detail   = df.group_by(["region", "category"]).agg(pl.col("amount").sum())
subtotal = (df.group_by("region").agg(pl.col("amount").sum())
              .with_columns(pl.lit(None, dtype=pl.String).alias("category")))
total    = (df.select(pl.col("amount").sum().alias("amount"))
              .with_columns(pl.lit(None, dtype=pl.String).alias("region"))
              .with_columns(pl.lit(None, dtype=pl.String).alias("category")))
result   = pl.concat([detail.select(cols), subtotal.select(cols), total.select(cols)],
                      how="vertical")
```

对账上是过的（✅ 11 行 × 3 列全部一致），但代价是：**两次聚合 + 一次 concat + 手工补占位列**，
而且**丢掉了一维信息**（哪些 NULL 是占位）。

「DuckDB 一行 SQL vs Polars 十五行 Python」不是玩笑 —— 每加一个维度，Polars 要多一次 `group_by`。

### 7. 代价：多级小计两边各要多久

| 实现 | 中位数 (ms) | 最快 (ms) | 结果行数 |
| --- | --- | --- | --- |
| DuckDB `GROUP BY region, category` | 17.1 | 16.1 | 30 |
| Polars `group_by(region, category)` | 8.2 | 8.1 | 30 |
| DuckDB `GROUP BY ROLLUP(...)` | 18.2 | 15.0 | 36 |
| Polars 手工复刻 ROLLUP | 14.1 | 13.4 | 36 |

口径：输入已物化成 parquet，两边读的是同一份数据；
计时的只有「分组聚合 + 拿到完整结果表」这一步（不含读盘）。
两边最终产出的都是同一个 `pl.DataFrame`（DuckDB 那边走 Arrow 转换，
Polars 那边是原生的），产出的行数也已经对过账。
前两行做的是**同一件事**（只要明细），可以直接比大小；
后两行产出的**结果集也相同**，但**过程不同**：
DuckDB 一条 SQL 一次扫完，Polars 侧要两次 `group_by` + 一次 `concat`，
所以后两行比的是「为了同样的结果各要付出多少」，不是「同样的活谁快」。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。

**别拿 ROLLUP 的两行去比「算法谁更优」** —— 一个是原生能力，一个是手工模拟，
测的是「要不要自己实现」，不是「实现得好不好」。

真正该记住的是场景 5 那条：**省下来的代码，省不掉信息**。
Polars 侧就算跑得赢，也一样分不出「占位 NULL」和「真 NULL」。

## 透视与重塑：PIVOT / UNPIVOT

`src/pivot_demo.py`。两边都有原生语法，但**输出的东西不是同一个东西** ——
差异不止在列名，还在「没有数据」这件事怎么表示。

### 1. 形状：列名一样，顺序不一样，行序更不一样

| | DuckDB | Polars |
| --- | --- | --- |
| 列名 | 键的**值**：`数码`、`家居`…… | 同左 ✅ |
| 列顺序 | 按键值排序 | 按数据里的出现顺序 |
| 行序 | **不保证** | `maintain_order=True`，是稳的 |

DuckDB 的 `PIVOT` 里没有 `ORDER BY`，行序由聚合算子决定 —— 实测同一连接**连跑 20 次就出现了
20 种不同的地区顺序**。Polars 加 `sort_columns=True` 就能把列序跟 DuckDB 对上。

→ **DuckDB 侧任何对账都必须先 ORDER BY**（或者排序后再比），否则对出来的差异全是假的。

### 2. 缺失组合：一个填 NULL，一个填 0

| 写法 | 「华东 × 服饰」（数据里没有这个组合） |
| --- | --- |
| DuckDB `USING sum(amount)` | **NULL** |
| Polars `aggregate_function="sum"` | **0** |
| Polars `aggregate_function="max"` | NULL |

3 × 4 = 12 个格子里，**DuckDB 有 5 个空格子，Polars 一个都没有**。

而且 **Polars 的 `pivot()` 没有 `fill_value` 参数** —— 填充值是聚合函数的副产品：
`sum` 填 0，`max` / `first` 填 NULL，你选不了。

这带来的后果不是显示层的差别，是**语义的差别**：

```
WHERE 服饰 IS NOT NULL
  DuckDB: ['华北']
  Polars: ['华东', '华北', '华南']
```

在 DuckDB 那边这个条件是「这个地区卖过服饰」，在 Polars 那边是「这个地区有服饰这一行」——
后者永远为真。报表里「有销量的地区」这种筛选，两边会给出**不同的地区集合**。

### 3. 列的集合：两边都能钉死

DuckDB 用 `PIVOT ... ON category IN (数码, 家居, 服饰, 食品, 美妆)`，
Polars 用 `pivot(on_columns=[...])` —— 都能让「数据里没出现过的美妆」也占一列。

**但列齐了不等于值齐了**：上面那个对账 ❌ 的原因就是「美妆」那一列，DuckDB 给 NULL、Polars 给 0。

### 4. 多列 pivot：列名格式根本对不上

| | 列名 |
| --- | --- |
| DuckDB | `a_x`、`a_y`、`b_x`、`b_y` |
| Polars | `{"a","x"}`、`{"a","y"}`、`{"b","x"}`、`{"b","y"}` |
| Polars `column_naming='combine'` | `v_{"a","x"}`（值名 + 元组） |

这不是能靠参数对齐的 —— Polars 没有「用下划线拼接元组」这个选项，要对上只能自己 `rename`。

**多列 pivot 是「列名格式」这条差异第一次露头的地方**：单列 pivot 时两边列名一样，
多列 pivot 时两边各写各的 → 迁移时凡是用了多列 pivot，**输出表的列名大概率要改**。

### 5. UNPIVOT：DuckDB 自己有两种语法，NULL 行为相反

| 写法 | 12 个格子变成几行 |
| --- | --- |
| DuckDB 简化语法 `UNPIVOT t ON ... INTO NAME c VALUE v` | **7 行**（丢掉 NULL 行） |
| DuckDB 标准语法 `FROM t UNPIVOT INCLUDE NULLS (...)` | **12 行** |
| Polars `df.unpivot(...)` | **12 行** |

简化语法**连 `INCLUDE NULLS` 都写不了**：

```
ParserException: Parser Error: syntax error at or near "INCLUDE"
```

→ **口径要对齐，先看语法**：DuckDB 侧写哪个语法决定了 NULL 的去留；
Polars 侧没有开关，永远是「全留」。

### 6. 往返：PIVOT → UNPIVOT 能不能还原

起点是 7 行的长表：

| | 往返后的行数 | 对账 |
| --- | --- | --- |
| DuckDB（简化 UNPIVOT） | 7 行 | ✅ 与原始长表一致 |
| DuckDB（`INCLUDE NULLS`） | 12 行 | 多出来的 5 行是 **NULL** |
| Polars | 12 行 | ❌ 多出来的 5 行是 **0** |

**两边的往返都不完全无损，但丢的东西不一样**：

- DuckDB（简化 UNPIVOT）把缺失的组合**直接扔了** —— 看起来还原了，
  其实是因为它把「没有这条数据」和「这条数据是 0」混为一谈了。
- Polars 把缺失的组合**变成 0 留下来** —— 凭空多出几行零。
- 真正正确的是 DuckDB 的 `INCLUDE NULLS`：12 行里 7 行有值、5 行是 NULL，信息一点没丢。

→ **「填 0」这个默认值是有代价的**：它把三值逻辑（NULL / 0 / 有值）压成了两值逻辑，
往返一次之后你就再也分不清「没数据」和「数据是 0」了。
这是本 demo 里最隐蔽的一条 —— **它不报错，只是默默改了你的数据**。

### 7. 代价：宽表 pivot 的代价

| 实现 | 中位数 (ms) | 最快 (ms) | 结果行数 |
| --- | --- | --- | --- |
| DuckDB `PIVOT`（列集合跟着数据） | 81.5 | 75.4 | 200 |
| Polars `pivot`（列集合跟着数据） | 66.9 | 53.3 | 200 |
| DuckDB `PIVOT ... IN (12 个品类)` | 68.2 | 65.7 | 200 |
| Polars `pivot(on_columns=12 个品类)` | 39.8 | 30.3 | 200 |

口径：输入已物化成 parquet，两边读的是同一份数据；
计时的只有「长表 → 宽表」这一步（不含读盘）；
两边最终产出的都是同一个 `pl.DataFrame`（DuckDB 那边走 Arrow 转换）。
**但两边的输出不是同一个东西**：DuckDB 的缺失组合是 NULL、Polars 的是 0，
所以这四个数字**只能比耗时，不能比结果**，也没有对账可言。
前两行是同一件事（列集合跟着数据走），后两行是同一件事（列集合钉死），
各自内部可比，跨组不可比。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。

**真正该记住的不是谁快，而是这四个数字不能拿来比大小**：
两边输出的内容不一样（NULL vs 0），场景 2 已经证明这会一路影响到 `IS NOT NULL` 过滤和往返。
**口径不对齐，性能数字再好看也没意义。**

## 容错模式与坏数据

`src/fault_demo.py`。同一个开关名 `ignore_errors`，两边**读出来的行数不一样** ——
而且两边都不报任何错。

### 1. 一份脏 CSV 有四种坏法

| 坏法 | 例子 | 属于哪一类 |
| --- | --- | --- |
| 类型不对 | `amount = N/A` | 字段能解析，类型转不过去 |
| 空值 | `amount` 是空的 | 两边都读成 NULL，不算错 |
| 字段数不对 | 多一列 / 少两列 | 行本身的结构不合法 |
| 引号没闭合 | `"引号没闭合` | 解析器在行中间就乱了 |

**这四种错误，容错开关不是一把统一的钥匙。**

### 2. 不给 schema：两边都「悄悄降级」

DuckDB 的 `sniff_csv` 和 Polars 的类型推断**都**把 `amount` 降成文本，
两边**都没报错** —— 但这一列已经不是整数了：

```
DuckDB sum(amount) -> BinderException: No function matches ... 'sum(VARCHAR)'
Polars  sum(amount) -> InvalidOperationError: `sum` operation not supported for dtype `str`
```

一致，但一致得很危险。脏数据上正确的做法是**显式给 schema**，别让推断替你做决定。

### 3. 给了 schema：两边各报各的错

| | 报错 |
| --- | --- |
| DuckDB | `ConversionException`：**第几行** + **原始行长什么样** |
| Polars | `ComputeError`：**哪一列** + **文件里的第几个字节**，行号要自己数 |

实务上的差别：几百万行的文件，DuckDB 的报错能直接定位到行；Polars 只能定位到列，剩下的得自己二分查找。

### 4. ignore_errors：一边跳行，一边置空

同一份 8 行物理数据的 CSV：

| 行数口径 | DuckDB | Polars |
| --- | --- | --- |
| 文件里的物理行数 | 8 | 8 |
| 引擎自己报的「行数」 | `count(*)` → 8 | `height` → 8 |
| **你实际拿到的行数** | **7** | **8** |
| 那一行坏数据 | 整行丢掉 | 保留，`amount` 变成 null |

- **DuckDB 的 `ignore_errors` 是「丢整行」**：坏行连同它其他字段一起消失。
- **Polars 的 `ignore_errors` 是「把坏字段置 NULL」**：行还在。

顺带一个更坑的：**DuckDB 侧的 `count(*)` 单独查走的是 CSV 解析器的物理行数口径**。
实测 `SELECT *` 只有 7 行，`count(*)` 却报 8；加上 `WHERE amount IS NOT NULL` 才是 7。
（这条只对「字段解析成功、类型转换失败」的行成立 —— 字段数不对的行不计入。）
Polars 没这个问题，`df.height` 就是真的行数。

### 5. 对账：Polars 补一刀，两边就对上了

在 Polars 侧再丢一次空行（7 行），跟 DuckDB 对账 **✅ 7 行 × 4 列全部一致**。

但这个补丁有两个前提：

- **这一列本来不能有合法的 NULL** —— Polars 把「值写坏了」和「值本来就是空的」都变成 null，之后分不出来。
- **补的这一刀是额外的开销**（场景 8 量了）。

→ 换句话说：**Polars 的容错是「保数据」，DuckDB 的容错是「保数据 + 记账」**。
想要记账就用 DuckDB 的 `reject_errors`；想要两边结果一样，就得自己补刀。

### 6. 坏行去哪了：store_rejects 与 reject_errors

DuckDB 侧的参数叫 **`store_rejects`**（不是 `store_errors`），
坏行记在 **`reject_errors`** 表里，带行号、列名、原始行、错误类型：

| 行号 | 列名 | error_type |
| --- | --- | --- |
| 3 | None | `TOO MANY COLUMNS` |
| 6 | note | `MISSING COLUMNS` |
| 7 | note | `UNQUOTED VALUE` |

`error_type` 是枚举，7 个取值：`CAST`、`MISSING COLUMNS`、`TOO MANY COLUMNS`、`UNQUOTED VALUE`、
`LINE SIZE OVER MAXIMUM`、`INVALID ENCODING`、`INVALID STATE`。

两个要注意的：

- 这个表在同一个连接里是**累加**的 —— 实测读之前 3 行，再读一次之后 6 行。
  想知道「这一批文件坏了多少行」要用 `rejects_scan` / `scan_id` 过滤，别直接 `count(*)`。
- `SET errors_as_json=true` 只改**抛出来的异常消息**的格式，
  不改表里的 `error_message`（仍是人读文本，如 `Expected Number of Columns: 4 Found: 5`）。
  想要机器可读，得自己解析 `error_type` 那一列。

### 7. 容错的边界

| 情况 | DuckDB | Polars |
| --- | --- | --- |
| 字段数不对 | `ignore_errors` **能兜住**，坏行全记进 `reject_errors` | 照样抛 `ComputeError: found more fields than defined in 'Schema'` |
| 想绕过 | —— | `truncate_ragged_lines=True`，但语义是**截断**（扔掉多出来的值继续读），不是跳过这一行 |
| 引号没闭合 | 记进 `reject_errors` | 解析器级错误，两个开关都救不了 |
| 文件不存在 | `IOException` | `FileNotFoundError` |

→ **容错开关管的是「行内容」，管不了「文件找不到」** —— 加 `ignore_errors` 都不变。

实务上：脏数据能靠 `ignore_errors` 兜住，行格式不统一（列数不一致）得靠上游统一。

### 8. 代价：跳坏行 vs 全量读入

| 实现 | 中位数 (ms) | 最快 (ms) | 读出行数 | amount 求和 |
| --- | --- | --- | --- | --- |
| DuckDB 容错读（流式聚合） | 62.2 | 54.3 | 1,980,000 | 198,000,000,000,000 |
| DuckDB 容错读（CTAS 物化） | 118.1 | 93.5 | 1,980,000 | 198,000,000,000,000 |
| Polars 容错读（`read_csv` 物化） | 60.0 | 51.2 | 2,000,000 | 198,000,000,000,000 |
| Polars 容错读 + 丢空行 | 78.2 | 74.3 | 1,980,000 | 198,000,000,000,000 |

口径：

- 两边读的是**同一个** CSV 文件、同一份 schema、都把 amount 转成整数、
  都用容错模式、最后都做一次求和 —— 同一件事。
  计时的**包含读盘和解析**。文件在第一轮预热后已经在 OS 页缓存里，两边读同一份缓存。
- **四行的执行模型不一样，绝对值不直接可比**，要比的是同组内部的差：
  - 第 1 行是 DuckDB 的「查询式」用法：`read_csv` 流式扫描 + 聚合，不物化整表。
  - 第 2 行是「把 CSV 导成表」的用法：CTAS 落成一张真表，再聚合。
  - 第 3、4 行是 Polars 的「读成 DataFrame」用法：eager 模式必然物化。
  - 第 1 行 vs 第 3 行：一个是流式聚合、一个是物化成 DataFrame 再算，
    **这两个数字不能直接比大小**；真正该比的是第 2 行 vs 第 3 行（都物化）。
  - 第 3 行 vs 第 4 行：多了一遍全列过滤 —— 输出跟 DuckDB 对齐要付的代价。
- 重复次数取中位数，机器是共享开发机，单次数字会有抖动。

- **汇总值一样，行数不一样**：坏行在两边都不贡献数值，所以 `sum` 完全相同；
  但 DuckDB 少了几行、Polars 一行没少 —— **查聚合看不出问题，查行数就看出来了**。
- **那 1% 的坏行，在时间上根本不是主角** —— 真正的差距在两个引擎解析/物化的实现上。
- → 所以这个语义差异**靠 benchmark 是发现不了的，只能靠对账**。
- **「少读 1% 的行」也不等于「更快」**：省掉的是「把坏行放进结果」这一步，
  行还是要解析、还是要判定坏不坏。

## 嵌套类型：LIST / STRUCT / MAP

`src/nested_demo.py`。三种嵌套类型两边都能用，但**「怎么写」完全不同** ——
DuckDB 的类型是**值的语法**，Polars 的类型是**列的声明**。

### 1. 一边是字面量，一边是 dtype

| | DuckDB | Polars |
| --- | --- | --- |
| LIST | `[1, 2, 3]` → `INTEGER[]` | `dtype=pl.List(pl.Int64)` → `List(Int64)` |
| STRUCT | `{'a': 1, 'b': 'x'}` → `STRUCT(a INTEGER)` | `dtype=pl.Struct({...})` → `Struct({'a': Int64, ...})` |
| MAP | `MAP{'k': 1, 'j': 2}` → `MAP(VARCHAR, INTEGER)` | `dtype=pl.Map(pl.String, pl.Int64)` → `Map(String, Int64)` |

DuckDB 写个 `[1,2,3]` 就是 LIST，类型由值推出来，`typeof()` 能随时问。
Polars 必须在构造时就把 dtype 说清楚，而且**没有 `typeof()` 这种运行时反查**的入口。

MAP 两边都最别扭：Polars **光给一个 list of dict 是不够的**，
少写 `dtype=pl.Map(...)` 的话它会变成 Struct，之后 `.map.keys()` 直接报
``InvalidOperationError: `map.keys` requires a Map dtype, got `struct[2]```。

### 2. LIST 函数：能对上的那部分，和对不上的那部分

DuckDB 2.0 里有 **92** 个 `list_` / `map_` / `struct_` 开头的函数（运行时现算）；
Polars 把它们收进 `.list` 命名空间。常用的：

| DuckDB | Polars | 说明 |
| --- | --- | --- |
| `list_sum(l)` | `list.sum()` | 空列表：NULL vs 0 |
| `list_avg(l)` | `list.mean()` | |
| `list_len(l)` / `len(l)` | `list.len()` | DuckDB 还有 `len()` 简写 |
| `list_distinct(l)` | `list.unique()` | |
| `list_slice(l, o, n)` | `list.slice(o, n)` | DuckDB 不支持负索引 |
| `list_contains(l, x)` | `list.contains(x)` | NULL 语义不同 |
| `list_transform(l, lambda)` | `list.eval(expr)` | Polars 收的是**表达式** |
| `list_reduce(l, lambda)` | （没有） | Polars 要自己 fold |

三处对不上：

**求和的入口不一样**（最容易敲错的一个）：

```
df['v'].sum()      -> InvalidOperationError: `sum` operation not supported for dtype `list[i64]`
df['v'].list.sum() -> [6, 9]
```

`Series.sum()` 是**整列**求和，撞上 list dtype 直接报错；`Series.list.sum()` 才是「逐个 list 求和」。
这两个名字只差一个 `.list`，报错信息却完全没提 list。

**空列表**：DuckDB `list_sum([])` → **NULL**；Polars `list.sum()` → **0**。
只要有一个 list 是空的，两边的总和就会差一个值 —— 而且 DuckDB 那边是 NULL（**不是 0**），聚合时会把整列污染掉。

**NULL 元素**：DuckDB `list_contains([1,NULL,3], NULL)` → **NULL**（三值逻辑）；
Polars `list.contains(None)` → **True**。

DuckDB 还有个 Polars 没有的能力：lambda 能**内联进 SQL 表达式**。
Polars 的对应物是 `pl.element()` 表达式 —— `list.eval(pl.element() * 2)`，
但它只能作用在列上，条件逻辑得在外面套一层。

### 3. DuckDB 2.0 的 lambda 箭头语法：1.x 迁 2.x 必踩

DuckDB 1.x 的写法 `SELECT list_transform([1,2,3], x -> x*2)` 在 2.0 上是**编译期就炸**：

```
BinderException:
  Binder Error: Deprecated lambda arrow (->) detected. Please transition to the new lambda
  syntax, i.e.., lambda x, i: x + i, before DuckDB's next release.
  Use SET lambda_syntax='ENABLE_SINGLE_ARROW' to revert to the deprecated behavior.
```

正确写法是 **`lambda` 关键字 + 冒号**：`lambda x: x*2`。
还能带第二个参数（下标）：`lambda x, i: x + i` —— 这在 Polars 里没有对应物。

`SET lambda_syntax='ENABLE_SINGLE_ARROW'` 是 DuckDB 留的后门，但报错原文自己也说了
「before DuckDB's next release」。

→ 这一条是**纯粹的破坏性变更**，跟语义对不对没关系：好处是不会静默出错；
坏处是升级当天所有用到 lambda 的查询全挂。改法是机械的，`->` 换成 `lambda ... :`。

Polars 侧**没有这个问题** —— 它压根没有 lambda 语法，从 DuckDB 迁过去反而是简化。

### 4. STRUCT：字段访问和「拆成列」

DuckDB：`s.a` 取字段、`s.*` 摊平成列、`unnest(s)` 摊平成列、`struct_pack(x := 1)` 造结构体。
Polars：`df['s'].struct.field('a')` 取字段、`df.unnest('s')` 摊平成列。

拆成列之后对账 **✅ 2 行 × 3 列全部一致**。

但 `unnest` **一个词两种语义**：

- DuckDB `unnest(struct)` 摊平成**列**（行数不变），`unnest(list)` 炸成**多行**（行数变多）——
  同一个动词，行为两套，写 SQL 时特别容易看串。
- Polars 分得很清：`struct.unnest()` / `list.explode()`。

拆多个 struct 列时，Polars 把字段名**直接铺开，不加前缀** —— 两个 struct 有同名字段时会撞车。

### 5. MAP：键值访问，以及「Polars 的 MAP 得显式声明」

DuckDB 一整套 `map_keys` / `map_values` / `map_extract` / `map_contains` / `map_entries`；
Polars 的 `.map` 命名空间只有 6 个方法，没有 `map_concat`、没有 `map_from_entries`。

`map_entries` 两边形状一模一样，可以直接对账 **✅ 1 行 × 1 列全部一致**。

**键序两边都是插入顺序**，不是排序后的顺序 —— 有些数据库的 MAP 是按键排序的，DuckDB 不是。

→ MAP 是三个嵌套类型里**最难对应**的一个。更实际的迁移路径是把 MAP 当成
「键列 + 值列」拆开存，两边都能用普通算子处理。

### 6. 嵌套类型在 Parquet 里的往返：三种类型都活下来了

这是好消息：LIST / STRUCT / MAP **两个方向都能完整往返**
（DuckDB 写 → Polars 读、Polars 写 → DuckDB 读），对账 **✅ 1 行 × 3 列全部一致**，
MAP 也一样（DuckDB 读回来是真正的 `MAP(VARCHAR, BIGINT)`）。

→ 之前那些「DuckDB 是字面量、Polars 是 dtype」的差异，在文件格式这一层全部消失。
**嵌套类型迁移的最优解常常是：别在内存里折腾类型，直接落 parquet 再读。**
唯一要留意的是**元素类型**（DuckDB 写的 INT32 对上 Polars 的 Int32，但两边自己造数时默认可能不同）。

### 7. 代价：把一个 list 列炸开要花多少时间

| 实现 | 中位数 (ms) | 最快 (ms) | 炸开行数 |
| --- | --- | --- | --- |
| DuckDB `unnest(tags)` | 26.2 | 26.0 | 10,000,000 |
| Polars `explode(tags)` | 93.9 | 93.3 | 10,000,000 |

口径：数据已物化成 parquet，两个引擎读的是**同一个文件**；
计时的这一步包含「读 parquet + 解码 list 列 + 炸开 + 计数」的全过程，不包含造数。
DuckDB 侧走 `unnest()`（SQL 里的行展开），Polars 侧走 `explode()`（DataFrame 方法），
做的**是同一件事**：把每行的 list 拆成多行并数出总行数。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。

炸开 list 列是**行数放大**操作 —— 每个 list 有几个元素，结果表就长几倍。
跟宽表 join 是同一类风险：中间结果可能比输入大一个量级。

如果只是想「每个 list 的元素和」，别炸开 —— DuckDB 的 `list_sum()` 和 Polars 的 `list.sum()`
都是一趟扫完，不产生放大后的中间结果。

## 查询计划：EXPLAIN 与投影裁剪

`src/plan_demo.py`。两个引擎都有计划，但**计划没法对读**，而且**只有一边能证明它做了什么**。

### 1. 同一个查询，两边的 EXPLAIN 长得完全不一样

DuckDB 给的是**盒式图**（默认就是**物理计划**）：

```
╭─ Projection ──────────────────────╮
│ Projections: CAST(#0 AS BIGINT),  │
│              #1                   │
╰─────────────────┬─────────────────╯
╭─ Perfect Hash Group By ───────────╮
│ Groups: #0                        │
│ Aggregates: sum(#1)               │
╰─────────────────┬─────────────────╯
...
╭─ Seq Scan ──────┴─────────────────╮
│ Table: memory.main.t              │
│ Projections: g, v                 │
│ Filters: id < 500                 │
╰───────────────────────────────────╯
```

Polars 给的是**缩进树**（画的是**算子树**，没有物理算子这一层）：

```
AGGREGATE[maintain_order: false]
  [col("v").sum().alias("sum(v)")] BY [col("g")]
  FROM
  simple π 2/2 ["g", "v"]
    FILTER col("id") < 500
    FROM
      simple π 3/3 ["g", "v", "id"]
        DF ["id", "g", "v"]; PROJECT */3 COLUMNS
```

两边的计划**没有一一对应的节点**：DuckDB 的 `Projection` 在 Polars 里是 `π`；
DuckDB 把过滤塞进扫描节点（`Filters: id < 500`），Polars 在内存表上是独立的一行 `FILTER`。

→ **别指望两边能对照着读**。计划长得像不算数，**结果能对账才算数**（脚本里每次都配一次对账）。

### 2. 投影裁剪：两边都裁，但只有一边能证明

12 列宽表、10 万行，只查 2 列：

| | 计划里写的 | 实际读盘 |
| --- | --- | --- |
| DuckDB | `Projections: id, v` | **953,441 字节** |
| DuckDB（查全 12 列） | 全部 12 列 | **13,435,510 字节**（只查 2 列是它的 **7.1%**） |
| Polars | `PROJECT 2/12 COLUMNS` | **没有数字可给** |

Polars 侧 `explain()` 只给计划文本，没有 IO 计数器；`collect()` 返回的 DataFrame
也不带任何元数据。

→ 那句 `PROJECT 2/12` 是一句**承诺**，不是一个**观测**。
调优的时候 DuckDB 能直接拿「读盘字节数」当反馈信号，Polars 只能靠「改代码再测一遍」间接验证。

### 3. 过滤下推：过滤被塞进了哪一步

DuckDB 优化**前**的计划里，过滤是一个独立的 `Filter` 节点；优化**后**它被塞进了扫描节点
（`Projections` 和 `Filters` 写在同一个 `Read Parquet` 节点里）—— 少一次中间结果物化。

Polars 优化前是独立的 `FILTER`，优化后变成扫描节点里的 `SELECTION:` 一行。语义一样，**画法**不一样。

### 4. 两边的默认值不一样：看计划的前提不同

- Polars `explain()` 的默认就是**优化后**的（`optimized=True`），想看优化前必须显式写 `optimized=False`。
- DuckDB 的默认是 **`physical_only`**（直接给物理计划），
  `PRAGMA explain_output='all'` 能一次拿到三份：未优化 / 已优化 / 物理。

→ **「计划」这个词在两边指的不是一回事**，看计划不能跨引擎比对。

### 5. 聚合：DuckDB 会告诉你它选了哪种算法，Polars 不会

| 分组键 | DuckDB | Polars |
| --- | --- | --- |
| 小值域整数（7 组） | `Perfect Hash Group By` | `AGGREGATE[maintain_order: false]` |
| 大值域整数（10 万组） | `Hash Group By` | `AGGREGATE[maintain_order: false]` |
| 字符串键（50 组） | `Hash Group By` | `AGGREGATE[maintain_order: false]` |

DuckDB 的计划**暴露了算法选择**，而**选哪个取决于数据的值，不取决于 schema**。
Polars 三种数据给出**字面上完全一样**的节点名 —— 你看不出它内部会走哪条路。

顺带注意 `maintain_order: false`：**两边聚合结果的行序都不保证**，想要稳定行序都得显式 `ORDER BY`。

### 6. Join：节点名完全对不上

| | DuckDB | Polars |
| --- | --- | --- |
| 等值连接 | `Hash Join` | `INNER JOIN:` |
| 非等值连接 | `Piecewise Merge Join` | `INNER JOIN:` |
| 笛卡尔积 | `Cross Product` | —— |

DuckDB 里 LEFT / INNER / SEMI 是同一个节点加一行 `Join Type:`，Polars 里是三个不同的节点名。
但 DuckDB 一定会告诉你**用的什么算法**，Polars 只写 `INNER JOIN` ——
底下是 hash join 还是 merge join，计划里一个字都没有。

→ **「看计划判断 join 会不会退化」这件事，只有 DuckDB 做得到。**

### 7. 前提：谁有计划、计划里有什么、看不到什么

- **Polars 只有 LazyFrame 有计划** —— eager 的 DataFrame 是已经算完的结果，没有计划可言
  （想看得先 `.lazy()`）。DuckDB 没这个区分，SQL 本来就是惰性的。
- **DuckDB 的 `EXPLAIN` 不执行查询，`EXPLAIN ANALYZE` 会执行** ——
  后者会给出 `Total Time` / `Data Read` / `Row Groups Scanned`，别在大表上随手用。
- Polars 侧两个想「看细节」的入口在 2.x 都没了：`profile()` 在 2.0 被**移除**；
  `show_graph()` 还在，但要额外的 matplotlib。
- **Polars 没有任何运行时指标**：`collect()` 返回一个光秃秃的 DataFrame，
  拿不到「读了几个字节」「扫了多少行」这种引擎内部数字。

→ DuckDB 的 EXPLAIN 是一条**完整的诊断链**（计划 → 三种粒度 → 实测 → 机器读得懂的 JSON），
Polars 的 EXPLAIN 是**一段文本**。DuckDB 能一路查到底，Polars 只能在「计划对不对」这一层打转。

### 8. 代价：投影裁剪到底值多少时间

| 实现 | 中位数 (ms) | 最快 (ms) | 结果 |
| --- | --- | --- | --- |
| DuckDB 只读 1 列 | 12.1 | 10.7 | 2,999,998,500,000 |
| DuckDB 读全部 12 列 | 147.8 | 131.8 | 17,475,795,107,475 |
| Polars 只读 1 列 | 14.1 | 12.7 | 2,999,998,500,000 |
| Polars 读全部 12 列 | 178.0 | 156.2 | 17,475,795,107,475 |

口径：数据已物化成 parquet，两个引擎读的是**同一个文件**（268,644,289 字节）；
「只读 1 列」和「读全部 12 列」在两边指的是**同一件事**：
把要读的列读出来做聚合（数值列求和、字符串列求长度和），区别只在读几列。
计时包含「打开文件 + 读 parquet + 解码 + 聚合」的全过程。
DuckDB 侧每次都重新 parse + bind SQL；Polars 侧每次都重新 scan + collect，
两边都是「从零跑一遍这条查询」的口径。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。

差距的大小取决于「被丢掉的列」有多宽 —— 一列窄的 int 和一列宽的 string，省下来的字节数差着量级。
所以**列的顺序和选列**在宽表上比想象中值钱。

反过来，「我只 select 了几列」这句话在两个引擎里都是**真的** ——
不需要担心 `select *` 把整张表拖下来，前提是别在后面又 `SELECT *` 回去。

## 采样与随机

`src/sample_demo.py`。DuckDB 的 `USING SAMPLE` **默认不是逐行抽样**，Polars 2.1 **连随机函数都没有**。

### 1. 默认采样在小表上返回 0 行 —— 或者全部

1000 行的表请求 10%，同一句 `SELECT count(*) FROM small USING SAMPLE 10%` 跑 100 次：

| 采样行数 | 出现次数 |
| --- | --- |
| 0 | **85** |
| 1,000 | 15 |

**中间值一个都没有出现过** —— 「100 行」这个正确答案根本不在结果里。

换几个比例（每个跑 20 次）：

| 比例 | 结果 |
| --- | --- |
| 10% | 0 行 ×17、1,000 行 ×3 |
| 50% | 0 行 ×10、1,000 行 ×10 |
| 90% | 0 行 ×3、1,000 行 ×17 |
| 100% | 1,000 行 ×20 |

Polars 侧同样的操作**永远给 100 行**（`floor(1000 × 0.1)`）。

→ DuckDB 的默认方法是 **system（块采样）**：1000 行的表只有一个数据块，
整块被选中就是 1000 行，没被选中就是 0 行 —— **必然二选一，不是偶发**。
报表里写「抽 10% 看趋势」，在 DuckDB 上可能拿到 0 行、也可能拿到全量，
而且这两种情况在**同一句 SQL** 下会随机切换，谁也没法从代码上看出区别。

### 2. 三种采样方法：想要确切行数只有一种能给你

10 万行表上请求 10%（目标 1 万），各跑 20 次：

| 采样方法 | 实测行数区间 | 偏差 |
| --- | --- | --- |
| `system`（默认） | 4,096 ~ 20,480 | **+104.8%** |
| `bernoulli` | 9,775 ~ 10,186 | +1.9% |
| `reservoir` | **10,000 ~ 10,000** | **+0.0%** |

1000 行的小表上：

| 采样方法 | 实测行数 | 精确命中目标 100？ |
| --- | --- | --- |
| `system` | 0 ~ 1,000 | ❌ 非 0 即全 |
| `bernoulli` | 87 ~ 109 | ❌ 按二项分布抖动 |
| `reservoir` | 100 ~ 100 | ✅ 每次都是 100 |

- **system**：按数据块抽样，**你没法拿它的行数做任何推断**。
- **bernoulli**：逐行独立掷骰子，行数服从二项分布（σ = √(n·p·(1-p))）。
  1000 行取 10% 时 σ ≈ 9.5（**±28%**），10 万行时 σ ≈ 95（±2.8%）——
  **样本越小行数越不准**，这是统计学不是 DuckDB 的毛病。
- **reservoir**：蓄水池算法，**每次都精确命中目标行数**，是三个里唯一能拿来做「抽 N 行看」的。

Polars 的 `sample(fraction=...)` 属于 reservoir 这一类 → **两边对齐的口径是 reservoir，
不是 DuckDB 的默认值**。

### 3. system 抽的是连续片段，不是分散的随机行

10 万行表（id 就是行号）上实测：

| 采样方法 | 采到的行数 | 连续段数 | 最大间隙 |
| --- | --- | --- | --- |
| `system` | 12,288 | **5** | **30,721** |
| `bernoulli` | 10,066 | 9,054 | 90 |

system 采到的 12,288 行只由 **5 段连续区间**拼成（2,048 / 2,048 / 4,096 / 2,048 / 2,048 行），
段与段之间的间隙高达 30,721 行。bernoulli 采出来的行散在整个表上。

→ 这条比「行数不准」更危险，因为**它不报错**。
把表看成「按时间先后追加的日志表」，system 采到的每一段都是**一个连续的时间窗口**，
中间整段的时间数据一个都没抽到 —— 比例是对的（10%），但抽到的不是「均匀撒在时间轴上的 10%」。

**做统计推断/画分布图，必须显式写 `bernoulli` 或 `reservoir`。**

### 4. 按行数采样

| 写法 | 结果 |
| --- | --- |
| `USING SAMPLE 10 ROWS` | ✅ 10 行（但不保证精确，走默认 system） |
| `USING SAMPLE reservoir(100 ROWS)` | ✅ 100 行 |
| `USING SAMPLE bernoulli(100 ROWS)` | ❌ **ParserException** |
| `df.sample(n=10, seed=42)` | ✅ 10 行（永远精确） |

bernoulli 拒绝离散行数，报错原文就摆在明面上：

> Sample method Bernoulli cannot be used with a discrete sample count, either switch to reservoir
> sampling or use a sample_size

取整规则也不同：999 行取 10%，DuckDB `reservoir` 给 **100** 行（round），Polars 给 **99** 行（floor）。
差一行不致命，但**做行数对账时**得先统一口径。

### 5. 可复现性：`setseed` 管不住采样

| 写法 | 两次是否一致 |
| --- | --- |
| DuckDB `setseed(0.42)` + `random()` | ✅ 一致 |
| DuckDB `setseed(0.42)` + `USING SAMPLE 10%` | ❌ **不一致** |
| DuckDB `USING SAMPLE 10% (bernoulli, 42)` | ✅ 一致（连选中的 id 集合都一样） |
| Polars `df.sample(seed=42)` | ✅ 一致 |

→ 想要「可复现」，两边的钥匙**不在同一个地方**：

- DuckDB 的 `SELECT setseed(0.42)` 作用在 **`random()` 这类随机函数**上，
  对 `USING SAMPLE` **完全无效**。采样要复现，种子得写在**方法后面** ——
  这是 2.0 才有的语法，1.x 没有。
- Polars 的种子直接跟着调用走（`df.sample(..., seed=42)`），
  另有全局的 `pl.set_random_seed(42)`，管的是 shuffle 之类。

迁移时最容易漏的是 DuckDB 这条：**照抄 `setseed` 以为整个查询可复现**，
结果采样那一段每次都不一样 —— 而且它自己不会告诉你。

### 6. 随机函数：Polars 2.1 里一个都没有

| | DuckDB | Polars 2.1 |
| --- | --- | --- |
| `random()` | ✅ | ❌ 表达式层和 Series 层都没有 |
| `rand()` | ❌ `CatalogException: Scalar Function with name rand does not exist!` | ❌ |

Polars 侧想生成随机数只能退回 numpy：`np.random.default_rng(42).random(1000)`。

分布本身两边一致（20 万个 [0,1) 均匀数分 10 个桶，逐桶对账 ✅ 容差 5%），
差别在**能不能在查询内部用**：

- DuckDB 把随机数做成**一等公民**：`ORDER BY random()`、`CASE WHEN random() < 0.1` 都能写，
  整个查询在引擎内部完成。
- Polars 做不到「一行 SQL 里的随机条件」这种写法。

→ **随机数从「引擎内部」搬到了「Python 进程里」**，能做的事一下子少了一截。

### 7. 代价：采样并没有比全表扫描便宜多少

| 实现 | 中位数 (ms) | 最快 (ms) | 结果 | 性质 |
| --- | --- | --- | --- | --- |
| DuckDB `count(*)`（元数据） | 0.6 | 0.6 | 2,000,000 行 | 不扫表 |
| DuckDB `count(*) WHERE v>0` | 1.2 | 1.1 | 1,999,998 行 | 基准 |
| DuckDB `system 10%` | 0.8 | 0.7 | 208,896 行 | 采样 |
| DuckDB `bernoulli 10%` | 3.7 | 3.7 | 199,553 行 | 采样 |
| DuckDB `reservoir 10%` | 29.6 | 26.4 | 200,000 行 | 采样 |
| Polars `filter(v>0).height` | 0.9 | 0.9 | 1,999,998 行 | 基准 |
| Polars `sample(fraction=0.1)` | 8.2 | 7.9 | 200,000 行 | 采样 |

口径：输入已物化成 parquet，两边读的是**同一个文件**；
DuckDB 侧先 `CREATE TABLE ... AS SELECT * FROM read_parquet(...)`，
Polars 侧先 `pl.read_parquet(...)` —— 数据都已驻留内存。
计时的只有「扫描 / 采样」这一步，**不含读盘**。
两边的「真扫全表」基准是**同一件事**：扫完所有行、过滤 v>0、计数
（DuckDB 是 `count(*) WHERE v>0`，Polars 是 `filter(v>0).height`），两边结果相同。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。
⚠️ 两边做的**不是同一件事**：DuckDB 侧采样在引擎内部完成（采完直接在引擎里聚合），
Polars 侧的 `sample()` 会**真的构造出一个新的 DataFrame**（把 10% 的行物化到 Python 侧）。
所以跨引擎的那两行**不能直接比大小**；真正该比的是**同一引擎内部**
「采样」与「真扫全表」的比值。

- DuckDB 的 `count(*)`（不带 WHERE）走**元数据优化**，根本不扫表 ——
  所以它那一行**不能当成「全表扫描」的基准**，真基准是带 WHERE 的那一行。
- system 采样是全场最快的：没被选中的块直接跳过，连数据都不用读 ——
  这就是场景 1/3 那两个坑的来源，**便宜是拿「统计上不可信」换的**。
- bernoulli 要逐行掷骰子，reservoir 还要维护一个跟样本一样大的蓄水池 ——
  在 DuckDB 侧这两个方法**比同引擎的全表扫描还贵**。
  → **最精确的那个方法（reservoir）反而最贵**，精确性是要付钱的。

→ 所以「先采样再过滤」并不能加速查询：采样的开销和读全表是同一个量级，
唯一例外是 system，而它恰恰是统计上不可信的那个。
**在 DuckDB 里采样是「给下游一个更小的数据集」的手段，不是查询加速手段。**
真想少读数据，条件过滤（`WHERE`）配合 zone map 才是正道。

## 约束与完整性

`src/constraint_demo.py`。DuckDB 的约束是**声明式的、写在 DDL 里、跟着文件走**；
Polars 2.1 **没有约束这个概念** —— 不是「不一样」，是「压根没有」。

### 1. DuckDB 会当场拒绝：五种约束的真实报错原文

| 约束 | 报错 |
| --- | --- |
| PRIMARY KEY | `Duplicate key "id: 1" violates primary key constraint.` |
| UNIQUE | `Duplicate key "v: 10" violates unique constraint.` |
| NOT NULL | `NOT NULL constraint failed: c_nn.v` |
| CHECK | `CHECK constraint failed on table "c_ck" with expression CHECK((v > 0))` |
| FOREIGN KEY | `Violates foreign key constraint because key "id: 999" does not exist in the referenced table` |

**报错里带着具体的键值**：主键冲突告诉你哪个键、值是多少；CHECK 失败会把**表达式本身**打出来
（不用回去翻 DDL）；NOT NULL 精确到列名，外键精确到缺失的键。

关键在于：**这些错误发生在写入的那一刻** —— 坏数据根本没有落盘的可能，
也就没有「下游发现数据不对」的那一天。UPDATE 也一样被拦（不是只在 INSERT 时看）。

约束登记在系统表里查得到（`duckdb_constraints()`）。

### 2. Polars 2.1 里一个约束 API 都没有

```
df.assert_unique()      -> AttributeError: 'DataFrame' object has no attribute 'assert_unique'
df.assert_no_nulls()    -> AttributeError: ...
df.assert_null_count()  -> AttributeError: ...
df.validate()           -> AttributeError: ...
```

DataFrame / Series / Expr 上带 `assert` 的方法：**一个都没有**。

Polars 给的是**检查用的原语**，不是**断言**：`is_duplicated()` / `null_count()` / `n_unique`。
把它们拼成约束是你自己的活儿，失败时报的是**你自己抛的异常**
（脚本里是 `ValueError: 主键冲突：id = [2, 2, 3, 3, 3]`）——
报错内容、报错时机、要不要报错，全都归你，也就意味着**可能压根不报错**。

对账：两边各自找出「哪些 id 重复了」，结果 **✅ 2 行 × 2 列全部一致**
→ **查坏行的能力两边都有**（算法一样，结果一样）；差的是**谁替你查、什么时候查**。

一个容易踩的坑：`is_duplicated()` 在 DataFrame 和 Series 上含义不同 ——
`df.is_duplicated()` 判的是**整行**重复（表里 id 明明重复了，它却全 False），
`df['id'].is_duplicated()` 才是判这一列。

→ 迁移时最危险的不是「API 名字对不上」，是**「少写一行代码，坏数据就静默进去了」**。

### 3. DuckDB 的约束活在文件里，Polars 的检查活在一次调用里

DuckDB 侧：约束写进 `.duckdb` 文件，**换个连接照样拦**（换进程、换机器也一样）。

Polars 侧：检查只是**你调了一次的那个函数**。脚本里演示了第二条**没有调用检查**的写入路径 ——
坏数据就这么进去了。

→ 差别的本质是**约束存在哪一层**：

- DuckDB 的约束在**存储层**，跟着文件走，你没法「绕过」它（除非故意去改表定义）。
- Polars 的检查在**调用层**，这次调用了、下次忘了，约束就凭空消失，**没有任何痕迹**。

实务上的差别：**DuckDB 是一次投入、长期生效**（写进 DDL 就行）；
**Polars 是每个写入点都要重复投入**，而且加漏了没人知道。
表有 5 个写入路径，你就要写 5 遍检查 —— 忘了第 5 遍，第 5 个路径就开始产坏数据。

### 4. 序列：DuckDB 的 nextval vs Polars 的 with_row_index

DuckDB 侧：

```sql
SELECT nextval('s')                -- 1
SELECT nextval('s'), nextval('s')  -- 2, 3
SELECT currval('s')                -- 3
CREATE SEQUENCE s_step START 10 INCREMENT 5   -- 10, 15, 20 …
CREATE SEQUENCE s_cyc START 1 MINVALUE 1 MAXVALUE 3 CYCLE
```

序列还能直接当列的默认值（`DEFAULT nextval('s')`）—— 插入的人**根本不需要知道 id 这件事存在**。
而且它是**跨连接**的：连接 1 取到 100 → 关掉 → 重连后取到 101。

Polars 侧**没有序列**。最接近的两样东西 `df.with_row_index('id')` 和 `pl.int_range(0, df.height)`
给的都是**当批次的行号**：从 0 开始、每次调用都从头数 —— 既不全局唯一，也不持久。

→ 差别不在「能不能生成 ID」，在「**谁来保证不重复**」。
DuckDB 的 `nextval()` 是**引擎保证的全局唯一**；Polars 侧要全局唯一就得自己维护 offset，
而那个 offset 的一致性**完全没有引擎替你兜底**。

迁移时最容易出错的一步：把 `DEFAULT nextval('s')` 换成 `with_row_index()`，
看着「id 有了」，但**重跑一次批次 id 就全变了** —— 外键、增量同步、幂等写入全靠 id。

### 5. 代价：约束的钱花在索引上，不在检查上

写入侧（每次先 DELETE 再 INSERT，计时只覆盖 INSERT）：

| 实现 | 中位数 (ms) | 最快 (ms) | 结果 |
| --- | --- | --- | --- |
| DuckDB INSERT 无约束 | 12.5 | 12.0 | 2,000,000 行 |
| DuckDB INSERT + NOT NULL | 12.0 | 11.2 | 2,000,000 行 |
| DuckDB INSERT + CHECK | 13.5 | 12.6 | 2,000,000 行 |
| DuckDB INSERT + **PRIMARY KEY** | **862.7** | 861.2 | 2,000,000 行 |
| Polars `write_parquet`（无校验） | 37.5 | 36.7 | 2,000,000 行 |
| Polars `write_parquet` + 查唯一性 | 141.6 | 134.6 | 重复 id 0 个 |

写入侧的计时**包含读 parquet**：INSERT 的语句体里就是 `read_parquet(...)`，
所以 DuckDB 那几行的数字 = 读盘 + 写入 + 约束校验（+ 索引维护）。
Polars 那两行 = 写一个 parquet 文件（数据早就读进内存了，**不含读盘**）。
每次 INSERT 前都先 DELETE（不计时），保证表是空表、约束检查不被已有数据干扰。

查询侧（同一个查询，跑在有约束和没约束的表上）：

| 查询 | 无约束 (ms) | PRIMARY KEY (ms) |
| --- | --- | --- |
| `count(*)` | 0.61 | 0.59 |
| `sum(v)` | 1.08 | 1.01 |
| `where id = ?` | 0.78 | 0.76 |
| `group by id` | 608.80 | 642.31 |

查询侧的计时不含读盘（表已经在 DuckDB 里了）。

四张表的列名、列类型、列顺序完全一致，**只差约束子句**（脚本里把 `CREATE TABLE` 语句打出来可自核）。

- **NOT NULL 和 CHECK 几乎是免费的** —— 只是逐行比一个条件，没有额外的数据结构要维护。
- **PRIMARY KEY 贵一大截**，贵在**索引**上，不在「检查重复」上：每插一行都要更新一棵 ART 索引树。

→ 「加约束」的真实成本模型是：**要不要索引**，而不是「要不要检查」。
只想要「数据别写错」→ NOT NULL + CHECK 几乎不要钱；
想要「能按 id 快速查、能防重复」才需要付 PRIMARY KEY 的钱。

⚠️ Polars 那两行跟 DuckDB 那几行**不是同一件事**：
DuckDB 的 INSERT 是「往**带约束的表**里写」，约束校验和索引维护都在计时里；
Polars 的 `write_parquet` 是「写一个文件」，**没有任何校验**，
第二行只是额外跑了一次**事后**的唯一性查询（不在写入路径上）。
所以这两组的数字**不能直接比大小**。

查询侧基本不受影响（`count(*)` / `sum(v)` / 点查 / group by 在有约束和没约束的表上耗时基本一样）——
**约束是写入时的代价，不是查询时的代价**。

### 6. 能力对照

| 能力 | DuckDB | Polars 2.1 |
| --- | --- | --- |
| 主键 / 唯一 / 非空 / CHECK / 外键 | ✅ 建表时声明 | ❌ 无 API，手工拼 |
| 写入时强制检查 | ✅ 引擎自动 | ❌ 得自己记得调 |
| 约束随文件持久化 | ✅ 换进程也在 | ❌ 检查是一次调用 |
| 全局唯一自增 ID | ✅ `CREATE SEQUENCE` | ❌ 无，只能手工 offset |
| 坏行定位 | ✅ 报错带键值 | ✅ 手工查（结果一致） |

→ 实务上的选择很明确：

- 数据要落库、要跨进程共享、要长期可靠 → **用 DuckDB 管约束**，把 Polars 当计算层。
- 数据只在内存里转一圈、错了重跑就行 → Polars 自己拼检查也够用，
  但要接受「检查只在你记得写的时候才存在」。

最后一句提醒：约束的价值不在「拦住一次坏数据」，在于**让坏数据连数据库都进不去**。
这个差别，只有真正的存储层才给得起。

## 存在性查询与半连接

`src/exists_demo.py`。这一段的结论只有一句，但它是 SQL 里最容易出事的一处：
**`NOT IN` 和 `NOT EXISTS` 在有 NULL 时不是一回事**。

### 1. 先把三件事对齐

左表 `l`（id = 1, 2, 3）、右表 `r`（id = 2, 4）：

| 语义 | DuckDB | Polars | 结果 |
| --- | --- | --- | --- |
| 在右表里找得到 | `WHERE EXISTS (...)` | `join(how='semi')` | `[2]` |
| 在右表里找不到 | `WHERE NOT EXISTS (...)` | `join(how='anti')` | `[1, 3]` |
| 成员判定（不筛行） | `id IN (SELECT id FROM r)` | `is_in(r['id'])` | `[False, True, False]` |

- **半连接只保留左表的列** —— 两边都一样，右表的列拿不到，要带过来得再 JOIN 一次。
- **半连接不会放大行数** —— 右表键重复时，普通 JOIN 会把左表每行放大到 3 行，
  半连接则每行最多出现一次。做「存在性判断」时这是必须的语义。
- **成员判定不筛行**，它给每一行打一个 TRUE / FALSE / NULL 标记。

### 2. 右表混进一个 NULL，`NOT IN` 一行都不剩

左表 `l`：id = 1, 2, 3, NULL；右表 `r`：id = 2, 4, **NULL**（只多了这一个 NULL）：

| 写法 | 结果 |
| --- | --- |
| `WHERE id NOT IN (SELECT id FROM r)` | `[]` |
| `WHERE NOT EXISTS (SELECT 1 FROM r WHERE r.id = l.id)` | `[1, 3, NULL]` |
| `WHERE id IN (SELECT id FROM r)` | `[2]` |
| `WHERE id IS NOT NULL AND id NOT IN (SELECT id FROM r)` | `[]` |
| `WHERE id NOT IN (SELECT id FROM r WHERE id IS NOT NULL)` | `[1, 3]` |

- **`NOT IN` 塌成空集**：右表里只要有**一个** NULL，`x NOT IN (2, 4, NULL)` 在三值逻辑下
  既不是 TRUE 也不是 FALSE，是 UNKNOWN；`WHERE` 只放行 TRUE → 一行都不剩。
  注意它连左表那行 id=NULL 也没放过。
- **`IN` 反而没事**：`x IN (2, 4, NULL)` 对没匹配上的 x 是 UNKNOWN，被 WHERE 滤掉；
  有匹配的还是 TRUE 照样留下 —— 命中的行一个不少。问题**只出在 `NOT IN`**（以及 `NOT = ANY`）。
- **「加个 `id IS NOT NULL` 补救」救不回来**：NULL 在**右表**里，滤左表没用。
  真能用的只有两条：改成 `NOT EXISTS`，或者把右表的 NULL 显式滤掉。
- Polars 的 `join(how='anti')` 对齐的是 **NOT EXISTS**（实测 `[1, 3, NULL]`）；
  `filter(~is_in(r['id']))` 给 `[1, 3]`，**不等价于 NOT IN**。

### 3. 三值逻辑在 SELECT 列表里长什么样

左表 id = 1, 2, 3, NULL，右表 id = 2, 4, NULL：

| id | IN | NOT IN | ANY | ALL |
| --- | --- | --- | --- | --- |
| 1 | NULL | NULL | NULL | FALSE |
| 2 | TRUE | FALSE | TRUE | FALSE |
| 3 | NULL | NULL | NULL | FALSE |
| NULL | NULL | NULL | NULL | NULL |

- 判定结果是 **TRUE / FALSE / NULL（UNKNOWN）** 三种。右表带 NULL 时，
  没匹配上的行拿到的不是 FALSE 而是 NULL。
- **`WHERE` 里 NULL 判定和 FALSE 判定没区别**（都被滤掉），
  **但在 `SELECT` 列表里区别很大**：换成 `CASE WHEN ... THEN 1 ELSE 0 END`
  会把 UNKNOWN 当成 0 算进去；而 `count(m)` 又会把 NULL 直接跳过 ——
  实测 `count(*)`=4、`count(m)`=1、`sum(CASE WHEN m THEN 1 ELSE 0 END)`=1，
  同一列、换个聚合方式数字就变了。
- `ANY` 是「存在一个相等」，`ALL` 是「全部相等」：右表为空时 `ANY` 是 FALSE、`ALL` 是 TRUE
  （空集上 ALL 恒真），而 `NOT IN` 相反 —— 空子查询时它把所有行都放出来。
- `id = ALL (...)` 在右表带 NULL 时几乎全是 NULL，因为「NULL = NULL」在 SQL 里不是 TRUE。

### 4. 执行计划：相关子查询被改写成了半连接

| 写法 | DuckDB 改写出来的连接类型 |
| --- | --- |
| `WHERE EXISTS (...)` | `Join Type: SEMI` |
| `WHERE NOT EXISTS (...)` | `Join Type: ANTI` |
| `WHERE id IN (子查询)` | `Join Type: SEMI` |
| `WHERE id NOT IN (子查询)` | `Join Type: MARK` |
| `SELECT` 列表里的 `id IN (子查询)` | `Join Type: MARK` |

DuckDB 2.0 里相关子查询**不是**逐行执行的相关循环：优化前是 `Dependent Join`，
优化后变成 `Comparison Join` + `Join Type: SEMI`，物理计划才落成 `Hash Join`。

- 同一个 `IN`，放在 `WHERE` 里是 SEMI、放在 `SELECT` 列表里是 MARK ——
  差别不是写法上的洁癖，是「要不要保留 UNKNOWN」决定的。
- `MARK` 出现在 `NOT IN` 时还多一个 `NOT SUBQUERY` 过滤器。
- 注意节点名是 `Join Type: SEMI` 这种**连接类型**，不是一个叫 `MARK_JOIN` 的独立算子。

### 5. Polars 的 `is_in` 不复现 SQL 的三值逻辑

| id | DuckDB `IN` | Polars `is_in` |
| --- | --- | --- |
| 1 | NULL | FALSE |
| 2 | TRUE | TRUE |
| 3 | NULL | FALSE |
| NULL | NULL | NULL |

对账结果（左边 DuckDB，右边 Polars）：

| 对照 | 结果 |
| --- | --- |
| `EXISTS` ↔ `join(how='semi')` | ✅ 1 行 × 1 列全部一致 |
| `NOT EXISTS` ↔ `join(how='anti')` | ✅ 2 行 × 1 列全部一致 |
| 带 NULL 的 `NOT EXISTS` ↔ `anti` | ✅ 3 行 × 1 列全部一致 |
| 带 NULL 的 `NOT IN` ↔ `~is_in` | ❌ 行数不一致：duckdb=0, polars=2 |

- **半连接这一族两边完全对齐**，带不带 NULL 都一致。迁移时用半连接对半连接可以放心。
- **`NOT IN` 这一族对不上**：Polars 的 `is_in` 只回答「在不在」，把 UNKNOWN 直接压成了 FALSE。
- → **迁移规则**：`NOT IN (子查询)` 一律改成 `NOT EXISTS`；
  `IN (子查询)` 在 `WHERE` 里两边等价，但要拿布尔列（放 `SELECT` 列表里）
  就得自己补 UNKNOWN —— Polars 侧没有现成的三值逻辑。
- Polars 2.x 里 `is_in` 对「同 dtype 的 Series」会发 DeprecationWarning，
  要写 `r['id'].implode()` 才回到旧行为。

### 6. 代价：五种写「在不在」的方式

数据：左表 200,000 行，右表约十分之一（每 10 个 id 命中 1 个）。

| 实现 | 中位数 (ms) | 最快 (ms) | 命中行数 |
| --- | --- | --- | --- |
| DuckDB `EXISTS` 相关子查询 | 10.4 | 9.7 | 200,000 |
| DuckDB `IN (子查询)` | 9.7 | 9.5 | 200,000 |
| DuckDB `SEMI JOIN` | 10.8 | 9.1 | 200,000 |
| Polars `join(how='semi')` | 9.8 | 7.9 | 200,000 |
| Polars `filter(is_in(...))` | 5.7 | 4.8 | 200,000 |

口径：两个 parquet 在计时前已物化 —— DuckDB 侧是内存表 `big_l` / `big_r`，
Polars 侧是内存里的 DataFrame，所以计时**不含读盘**。
五行做的**是同一件事**：数出左表里有多少行能在右表找到对应的 id，
右表在两边都是同一份数据（约左表的十分之一），没有 NULL。
DuckDB 侧的子查询每次都要重新扫右表建哈希表，Polars 侧的 `is_in` 每次
也要把右表打包成 list —— 两边都付了「准备右表」这笔钱，账是平的。

解读：

- **DuckDB 那三行的差距很小** —— 优化器已经把 `EXISTS` 和 `IN` 都改写成了同一个半连接，
  `EXISTS` 并没有「多跑一遍子查询」。如果你的 EXPLAIN 里 `EXISTS` 真的落成了
  `Dependent Join`，那才要担心。
- 所以在 DuckDB 上，写 `EXISTS` 还是写显式 `SEMI JOIN` **不影响性能**，只影响可读性 ——
  显式写 `SEMI JOIN` 能让「这里是半连接、不会放大行数」这件事写进 SQL 里。
- **Polars 那两行走的不是一条路**：`join(how='semi')` 是哈希半连接，
  要把右表建成哈希表、再按左表的行序吐出来；`is_in` 是「右表做成一个集合、逐行查表」
  —— 它不做连接，只做成员判定，所以不用管列、也不用管行序。
- 真正的结论不是「谁快」，而是**两边都要先把右表压成一份可查的结构**；
  而且它是**每次查询都要重建**的 —— DuckDB 侧重建的是哈希表，Polars 侧重建的是那个集合。

## 递归 CTE 与迭代计算

`src/recursive_demo.py`。DuckDB 有 `WITH RECURSIVE`，Polars 没有 ——
但**这不是「一个能做、一个不能做」的差别**，而是「谁来数层数」的差别。

### 1. 层级遍历：一句 SQL vs 一段 Python 循环

组织架构表（9 个节点、4 层），DuckDB 的写法：

```sql
WITH RECURSIVE chain(id, name, depth, path) AS (
  SELECT id, name, 0, name FROM emp WHERE mgr IS NULL      -- 锚点
  UNION ALL                                                 -- 迭代
  SELECT e.id, e.name, c.depth+1, c.path || ' > ' || e.name
  FROM emp e JOIN chain c ON e.mgr = c.id                    -- 递归项
) SELECT * FROM chain
```

Polars 侧是一段 Python 循环：每一轮 join 出下一层，再拼回去。

对账：✅ **9 行 × 4 列全部一致**，连 `depth` 列的类型都一致（两边都是 INT32）。

→ 能力差距在这里还看不出来（两边都能做对）。真正拉开差距的是下一节：路径不止一条的时候。

### 2. 路径枚举：递归的行数是路径数，不是节点数

DAG（8 个节点、9 条边，交付组有两个上级）→ 递归吐出了 **9 条路径**。

换成「斐波那契阶梯」（节点 i 的上级是 i-1 和 i-2）：

| 层数 | 递归出的路径数 |
| --- | --- |
| 5 | 12 |
| 10 | 143 |
| 15 | 1,596 |
| 20 | 17,710 |
| 25 | 196,417 |

→ 深度只涨了 5，路径数涨了一个数量级。递归 CTE 本身没问题，
但它把「路径」这件事**显式物化**出来了，组合爆炸是数据结构的性质，不是引擎的锅。

Polars 的逐层 join 同样能枚举出全部路径（✅），但代价是**循环次数和路径数一起涨**，
Python 侧要一遍遍 concat。

→ **实践建议**：能用「祖先-后代对」表达的问题（谁能影响谁），
别去枚举路径 —— 场景 4 的传递闭包就是干这个的，行数少得多。

### 3. 执行模型：EXPLAIN 里那个 `Rec CTE` 节点

物理计划里有一个 `Rec CTE` 节点，它下面挂着一个 `Rec CTE Scan` ——
那是「上一轮的结果」被物化出来的地方，每一轮迭代都从这张临时表读，再算出下一轮。

`EXPLAIN ANALYZE` 里同一个节点叫 `Recursive CTE`（带 `Recursive CTE Scan`）；
静态计划里是缩写 `Rec CTE`。

`explain_output='all'` 给三份计划，能看到优化的过程：
优化前根节点是 `CTE`（里面套着一个 `Rec CTE`），优化后根节点是 `Projection`，
到物理计划 `Rec CTE` 直接当根 —— 而且 `Comparison Join` 在物理阶段换成了 `Hash Join`。
`Rec CTE Scan` 只在物理计划里出现：它是运行时物化的那张临时表。

→ 代价的形状由此确定：**迭代轮数 = 数据的层数**。
层数少（宽而浅的树）就快，层数多就慢 —— 而且每轮都要把中间结果物化一遍。

### 4. `UNION ALL` 还是 `UNION`：差别就是「会不会收敛」

传递闭包（谁是谁的祖先，含自己）：

| 写法 | 行数 |
| --- | --- |
| `UNION ALL`（不去重） | 26 |
| `UNION`（跨轮去重） | 25 |

其中不同的 (祖先, 后代) 组合有 25 对 —— `UNION ALL` 多出来的 1 行就是重复的推导路径。
对账 DuckDB `UNION` 闭包 ↔ Polars 闭包：✅ **25 行 × 2 列全部一致**。

→ `UNION` / `UNION ALL` 的区别在这里是**能不能停下来**的区别：

- `UNION ALL`：每轮输出全部追加，不管跟之前重不重复。
- `UNION`：**跨轮去重** —— 算出来的值集合没变就不再产生新行，
  于是递归变成了「求不动点」，可以停下来。

→ **迁移规则**：从别的方言搬过来的递归 CTE，第一件事是确认 `UNION` 还是 `UNION ALL` ——
Postgres 里 `UNION`（去重）是默认，`UNION ALL` 要手写；反过来抄很容易抄反，
而**抄反的后果不是结果错，是跑不完**。

### 5. 反直觉：数据里有环时，递归不会自己停

| 情况 | 实测 |
| --- | --- |
| `UNION ALL` + 环 | 跑满 3 秒阈值仍未结束，被 `interrupt()` 中断 |
| `UNION`（去重）+ 环，输出列不变 | 收敛，结果 `[(1,), (2,)]` |
| `UNION`（去重）+ 环，输出列每轮都变（带 depth） | 跑满 3 秒阈值仍未结束，被 `interrupt()` 中断 |

→ 这是本 demo 里**最危险**的一条：

- **DuckDB 2.0 的递归 CTE 没有内置的迭代次数上限。**
  上面第一行实测：带环的 `UNION ALL` 递归跑满了本 demo 自己设的 3 秒窗口还没停
  （这不是 DuckDB 的超时 —— 引擎压根没有这个机制）。
  换成真业务数据就是**一条跑不完的查询**，而且它不会报错、不会超时，
  只会一直占着 CPU 和内存。
- 唯一沾点边的 `max_expression_depth` 跟这事**没关系** ——
  它管的是解析器里的表达式嵌套深度，不是递归轮数。
- `UNION` 能收敛，是因为去重之后**值集合不再变化**。
  但只要输出列里带一个每轮都变的东西（比如 depth），去重就失效了，照样跑不完。
- → **实践建议**：递归 CTE 里**自己写上限** —— 在递归项上加 `WHERE c.depth < 100`
  （拿层级/步数当护栏），或者用 `UNION`（去重）+ 不变的输出列，让它自己收敛。
- Polars 侧反而更好防：循环是你自己写的，`for _ in range(1000)` 或者
  `if level.height == 0: break` 都是现成的护栏。

### 6. Polars 的两种替代写法

对账：逐层 join ✅ 9 行 × 4 列全部一致；`map_elements` ✅ 9 行 × 4 列全部一致。
`depth` 列的类型：DuckDB Int32 / 逐层 join Int32 / **`map_elements` Int64**。

- **逐层 join**：Python 开销是「层数」级别的 —— 一层一次向量化 join，中间结果是 DataFrame。
- **`map_elements`**：Python 开销是「节点数 × 树高」级别的 ——
  每个节点都要沿 mgr 一路走到根，每一步都是一次 Python 字典查找和循环。
  好处是它**天然是个函数**，不用管循环、不用管收敛；坏处是完全没法向量化。
- → **选型**：层级浅、节点多 → 逐层 join；层级深、节点少 → `map_elements`；
  层级和数据都大 → 回到 DuckDB。

### 7. 代价

| 实现 | 中位数 (ms) | 最快 (ms) | 行数 |
| --- | --- | --- | --- |
| DuckDB `WITH RECURSIVE`（UNION ALL） | 64.3 | 59.4 | 2,000,000 |
| DuckDB `WITH RECURSIVE`（UNION 去重） | 585.4 | 513.0 | 2,000,000 |
| Polars 逐层 join 循环 | 379.6 | 325.0 | 2,000,000 |
| Polars `map_elements` | 8372.4 | 8301.2 | 2,000,000 |

内存放大：这棵树高 20 层

| 列 | 字符数 |
| --- | --- |
| `name`（每个节点一个名字） | 22,888,896 |
| `path`（每个节点一整条路径） | 454,530,759 ≈ `name` 的 **19.9 倍** |

（Polars 逐层 join 算出来的 `path` 列同样是 454,530,759 个字符。）

口径：树在计时前已物化 —— DuckDB 侧是内存表 `tree`，
Polars 侧是内存里的 DataFrame，所以计时**不含读盘**。
四行做的**是同一件事**：算出每个节点的 depth 和 path，
输出同样多的行（表里「行数」那列），结果也逐行相同。
两行 DuckDB 的区别只有 `UNION ALL` 和 `UNION`（去重），
这棵树里每条路径都不同，所以去重不会改变结果，只改变代价。
计时不含建树，也不含 Polars 侧 DataFrame 的第一次读入。
重复次数取中位数，机器是共享开发机，单次数字会有抖动。

解读：

- **DuckDB 的递归 CTE 只迭代「树高」次** —— 轮数只跟深度有关，跟节点数无关。
- **两行 DuckDB 的差值就是「去重」的代价** —— `UNION` 要维护一个已经见过的值的集合，
  每一轮都要往里查、往里插。场景 4 里说 `UNION` 换来的是「会收敛」，这里就是它的账单。
- **Polars 逐层 join 的轮数跟 DuckDB 一样**，但每一轮都要过一遍 Python。
- **`map_elements` 是「每个节点一次 Python 调用」**，总代价是 O(节点数 × 树高)；
  逐层 join 是 O(树高) 次向量化 join —— 两行的差距就是「每节点一次」的代价。
- **内存放大在 path 列上**：递归 CTE 每轮都要把上一轮的 path 物化一遍，
  树越深放大越狠（换成一条链会放大成平方级）。
  → 深度信息能预计算就预计算，别在递归里一路拖着字符串走。

## 视图、宏与持久化对象

`src/ddl_demo.py`。这一段问的是「哪些东西活得比一个进程长」——
DuckDB 的答案和 Polars 的答案几乎不重叠。

### 1. 视图：存进 `.duckdb` 文件，重连之后还在

```sql
CREATE VIEW v_big AS SELECT id, region, amount FROM sales WHERE (amount >= 100);
```

关掉连接、重新打开同一个 `.duckdb` 文件，再查 —— 结果一致，
`duckdb_views()` 里还有同名视图：**视图是数据库级的，不是连接级的**。

Polars 侧最接近「视图」的是 LazyFrame，但它跟 DuckDB 的视图差着三样东西：
**没有名字、只在内存里、进程退出就没了**。
（Polars 2.1 有 `LazyFrame.serialize()`，能存下的是**执行计划**，
不是一段你能读懂、能改的 SQL 文本，也没法在数据库里注册。）

### 2. 反直觉：视图存的是「配方」不是「结果」

查 `v_big` 的执行计划，再把视图的 SQL 原样展开手写一遍，看两次的计划 ——
**两段计划逐字符相同**（`True`），计划里提到 VIEW 的地方 **0 处**，
只有一行 `Filters: amount >= 100.0` —— 视图里那句 WHERE 被**原样贴进**了查询计划。

→ **DuckDB 的视图不是「存下来的结果」，是「存下来的 SQL 文本」。**
每次查它，底下的表都要重新扫一遍 —— 视图**不省任何计算**，只省打字。
所以「建了视图查询就快了」是错觉：DuckDB 里快的是**物化成一张真表**（场景 5）。

Polars 的 LazyFrame 恰恰相反：它是**惰性计划**，collect 之前一行都不算，
但也没有名字 —— 两边都是「配方」，DuckDB 的配方能存进文件，Polars 的不能。

### 3. 宏：带参数的 SQL 函数，同样能落盘

| 宏 | 形态 | 实测 |
| --- | --- | --- |
| `add_tax(x, rate)` | 标量宏 | `add_tax(100, 0.1)` → 110.0 |
| `add_bonus(x, bonus=10)` | 标量宏 + 默认参数 | `add_bonus(5)` → 15，`add_bonus(5, 99)` → 104 |
| `tbl_top(n)` | 表宏（返回一张表） | 能直接参与 JOIN |

宏混在 `duckdb_functions()` 里，用 `function_type` 区分（`macro` / `table_macro`）——
DuckDB 2.0 没有 `duckdb_macros()` 这张表。重连之后宏还在。

Polars 对应的是**普通 Python 函数**：区别是 Python 函数活在一个进程里，
没有名字、不能被 SQL 文本引用；而 DuckDB 的宏写在 `.duckdb` 文件里，任何连接都能调。

### 4. 序列：`nextval` 的号是连续的，跨连接接着往下发

同一个连接里连发两个号：1, 2；`duckdb_sequences()` 里 `last_value=2`。
关掉连接、重连，再 `nextval` 一次 → **3**（号存在数据库文件里，重连不会从头开始）。

Polars 侧没有序列，也没有 `nextval`。要一列连续 ID 的常见两种做法
（`df.with_row_index('n')`、`pl.int_range(1, n+1)`）都只是**当场算出来的一个列**，
不是数据库对象：没有「下一个号」的概念。

### 5. DuckDB 2.0 没有物化视图

```
CREATE MATERIALIZED VIEW mv AS SELECT count(*) c FROM sales
-> ParserException: Parser Error: syntax error at or near "MATERIALIZED"
```

解析器在 `MATERIALIZED` 这个词上就断了 —— 不是「不支持」而是「不认识」，
连这串 token 都不是语法。

替代方案：直接物化成一张真表（CTAS）或者 `COPY ... TO parquet`（覆盖写，不是追加），
刷新逻辑要自己写。DuckDB 没有「这是物化视图」的标记 —— 物化之后视图和表就分不出来了。

### 6. 反直觉：DuckDB 2.0 其实**有**触发器 —— 但只实现了子集

| 触发器 | 结果 |
| --- | --- |
| `AFTER INSERT ... FOR EACH STATEMENT` | ✅ 能建，插一行后 audit 表里有 1 条 |
| `BEFORE ... FOR EACH ROW` | ❌ `NotImplementedException: BEFORE FOR EACH ROW triggers are not yet supported` |

触发器也能在 `duckdb_triggers()` 里看到，重连之后还在 —— 也是数据库级的持久对象。

→ 所以「DuckDB 不支持触发器」这句话在 2.0 上是**不准确**的 ——
准确的说法是：**只实现了触发器的一个子集**。
Polars 连这个子集都没有 —— 不是「功能少一点」，是整个「数据库替你执行」这件事在 Polars 里不存在。

### 7. 持久化的边界

| 对象 | 落在哪 | 重连后还在吗 |
| --- | --- | --- |
| `CREATE TABLE` / `VIEW` | `.duckdb` 文件 | 在 |
| `CREATE MACRO` / `SEQUENCE` | `.duckdb` 文件 | 在 |
| `CREATE TRIGGER` | `.duckdb` 文件 | 在 |
| Python UDF | 连接内存 | **不在**（重连后 `CatalogException: Scalar Function with name py_double does not exist!`） |
| `SET memory_limit` / `threads` | 连接内存 | **不在** |

→ **写进 catalog 的东西跨进程，写进连接状态的东西不跨进程。**

选宏还是选 Python UDF，就看你想让它活多久：宏是纯 SQL 表达式，跨进程跨连接都能用，
别的工具（CLI、DBeaver）也能调；Python UDF 能做宏做不了的事（调 HTTP、读文件、跑正则引擎），
但它绑在这个进程上 —— 而且 DuckDB 对 Python UDF 是**逐行**调用的，
慢得不是一个量级 —— 这才是它真正的代价，不是「不能持久化」。

### 8. 代价：视图每次重算，物化算一次

| 实现 | 定义成本 (ms) | 单次查询 (ms) | N 次总计 (ms) |
| --- | --- | --- | --- |
| DuckDB 视图 | 0.6 | 9.6 | 48.7 |
| DuckDB 物化表 (CTAS) | 9.1 | 0.6 | 12.2 |
| Polars LazyFrame | 0.0 | 14.2 | 70.8 |
| Polars 物化 parquet | 15.1 | 0.5 | 17.7 |

对账（5 次重复后取最后一次结果）：DuckDB 视图 vs DuckDB 物化表 / Polars LazyFrame /
Polars 物化 parquet —— 三次都是 **✅ 7 行 × 3 列全部一致**。

**回本点**：物化表定义一次要 9.1 ms，每次查询比视图省 9.0 ms ——
查 2 次以后，物化才比视图划算。

口径：输入是同一份 parquet，四种实现算的是同一个聚合
（按 region 分组、过滤 amt > 100、算条数和求和），结果已对账一致。
「定义成本」= 建一次对象的时间；「单次查询」= 拿到结果一次的时间，
两边都**包含读 parquet**。「N 次总计」= 定义成本 + 单次查询 × 重复次数，取中位数。
Polars LazyFrame 的「定义成本」接近 0，是因为它本来就是惰性的 ——
表达式只被记下来，一行数据都还没算。
DuckDB 的物化表存在 `.duckdb` 文件里、Polars 的物化写在 parquet 文件里 —— 两者是对等的。

解读：

- **视图不省计算，只省打字。** 视图那一行的「单次查询」跟 LazyFrame 那一行是同一量级。
- 真正省时间的是**物化**：把「扫 + 过滤 + 聚合」做一次存下来，之后每次查询只剩下读结果。
- 所以「要不要物化」是个纯算术题：**物化成本 ÷ 每次省下的时间 = 回本点**。
  查得少就别物化，查得多就值得。
- DuckDB 2.0 没有 `CREATE MATERIALIZED VIEW` 这条命令，
  但上面这四行数字说明：**这件事你本来就得自己做** ——
  在别的数据库里你靠一条 DDL 声明，在 DuckDB 里你靠一张真表。

## 内存上限与落盘

`src/memory_demo.py`。这一段是**两边最不对等**的地方：
DuckDB 有一套能兑现的内存预算机制，Polars 压根没有这套机制。

### 1. `memory_limit` 是个预算，不是一堵墙

刚连上时的默认值：`current_setting('memory_limit')` → **100.3 GiB**，
物理内存 125.5 GiB —— 默认上限就是**物理内存的 80%**。

顺带一提，默认的 `temp_directory` 是 **`.tmp`** —— 也就是**当前工作目录**。
不改它，DuckDB 落盘的时候就会往你正在跑脚本的那个目录里写临时文件。

`SET memory_limit='50MB'` 之后回显成 **47.6 MiB** —— DuckDB 认的是十进制：
50 MB = 50,000,000 字节 = 47.7 MiB。「我设了 50MB」在内部其实是 47.6 MiB，
差的那点不是误差，是**单位制**。

这个上限管的是**缓冲区管理器**的总预算（扫描缓冲、哈希表、排序的临时空间）。
超了 DuckDB 有两条路：往磁盘上溢写，或者报错。

→ 它**不是**「不许超过」的硬墙 —— 它是「超了以后怎么办」的开关。

（另外：DuckDB 2.0 里没有 `PRAGMA memory_limit`，只有 `current_setting`；
`PRAGMA temp_directory` 同样不存在，别按老文档写。）

### 2. 落盘：预算不够时，中间结果写到 temp_directory

能落盘的窗口其实很窄 —— 上限太小，缓冲区管理器的固定开销就把预算吃光了，直接 OOM；
上限太大，中间状态又装得下，压根不用落盘。脚本里是**试**出来的（每个组合都真跑一遍）：

| 上限 | 4 线程 | 8 线程 |
| --- | --- | --- |
| 10MB | OOM | OOM |
| 20MB | OOM | OOM |
| 30MB | 跑完，落盘 56,786,944 字节 | OOM |
| 40MB | 跑完，落盘 52,396,032 字节 | OOM |
| 50MB | 跑完，落盘 29,130,752 字节 | OOM |
| 60MB | 跑完，落盘 27,885,568 字节 | 跑完，落盘 48,824,320 字节 |
| 80MB | 跑完，落盘 16,187,392 字节 | 跑完，落盘 37,388,288 字节 |
| 100MB | 跑完，落盘 3,112,960 字节 | 跑完，落盘 10,584,064 字节 |

临时文件长这样（文件名里那个数字是**进程 PID**）：

```
duckdb_temp_902737_12_owner
duckdb_temp_902737_12_storage_DEFAULT-0.tmp
duckdb_temp_902737_12_storage_S128K-0.tmp
```

**查询跑完之后再看目录** —— 数据文件（`*.tmp`）都删了，只剩一个 0 字节的 `_owner` 锁文件。
临时文件是**用完即删**的：「有没有落盘」只能靠运行期间观察，事后去目录里找是找不到的。
（想从 SQL 侧看，`duckdb_temporary_files()` 有 `path` / `size` 两列 ——
但查询结束再查它就是空的，同一张表，区别只在查的时刻。）

### 3. 反直觉：线程一多，同样上限直接 OOM

同样的查询、同样的 50MB 上限，只改线程数：

```
threads=8 -> OutOfMemoryException
  Out of Memory Error: failed to allocate data of size 4.0 KiB (47.8 MiB/47.6 MiB used)
    Possible solutions:
    * Reducing the number of threads (SET threads=X)
    * Disabling insertion-order preservation (SET preserve_insertion_order=false)
    * Increasing the memory limit (SET memory_limit='...GB')
```

→ **DuckDB 的 `memory_limit` 是全局预算，但每个线程各自持有缓冲区。**
线程越多，单线程能分到的就越少 —— 同一个上限下反而更容易直接 OOM。
注意 8 线程那次：**先落了 8,024,064 字节的盘，然后才撞上限** ——
落盘只是把失败往后推了推，并没有救回来。DuckDB 自己的报错第一条建议就是降线程数。

→ 所以「内存不够就调大 memory_limit」不是唯一解，**先把 `SET threads` 调小**往往就够了。

### 4. 反直觉：`memory_limit` 是**连接级**的

| 连接 | `memory_limit` |
| --- | --- |
| 连接 A（内存库） | 47.6 MiB |
| 连接 B（新建的内存库） | 100.3 GiB（**完全没受影响**） |
| 连接 C（文件库） | 47.6 MiB |
| 连接 D（重开同一个文件） | 100.3 GiB |

文件库也不记这个设置：它是**连接状态**，不写进 `.duckdb` 文件。
这跟视图、宏不一样 —— 对照表：

| 设置 / 对象 | 存在哪 | 换个连接还有吗 |
| --- | --- | --- |
| `CREATE VIEW` / `MACRO` | `.duckdb` 文件 | 在 |
| `SET memory_limit` | 连接状态 | 不在 |
| `SET threads` | 连接状态 | 不在 |

→ 实务上的后果：同一个 Python 进程里 `duckdb.connect()` 两次就是两个互不相关的世界；
多个进程各连同一个 `.duckdb` 文件，各自的 memory_limit 互不影响 ——
这既是好事（不会互相踩），也是坏事（你以为设了，其实是各设各的）。

### 5. Polars 的流式引擎：`collect(engine="streaming")`

```
lf.collect(streaming=True)
-> ArgumentRemovedError: the argument 'streaming' ... was removed in version 2.0.
   Use `engine="streaming"` instead.
```

而且 **2.x 的默认引擎已经是流式的**（1.x 默认是内存引擎）。
实测判据是抓 Polars 的 verbose 日志：流式引擎会把聚合**拆成子图并行跑**
（`running group-by in subgraph`），内存引擎是一把梭（`keys/aggregates are not partitionable`）：

| 写法 | 拆子图 | 一把梭 |
| --- | --- | --- |
| 默认 `collect()` | True | False |
| `engine="streaming"` | True | False |
| `engine="in-memory"` | False | True |

流式的意思是**一批一批**往外吐：`collect_batches(chunk_size=20000)` → 5 批，
每批 `[20000, 20000, 20000, 20000, 20000]` 行。
（对比 DuckDB：DuckDB 没有「批次」这个概念，它是一条流水线，内存不够就往 `temp_directory` 溢写。）

→ 流式引擎是 Polars **唯一**的省内存手段 —— 但它**不是上限**：
它让峰值更低，可它没有「不许超过 X MB」这个开关。

### 6. 谁在管内存：峰值 RSS 实测

每个实现单独跑在独立子进程里，用 `/proc/self/status` 的 `VmHWM` 报它自己占了多少
（不能用 `ru_maxrss`：Linux 上它会被子进程从父进程继承，测出来会一排相同；
macOS 没有 `/proc`，那边才退回 `ru_maxrss`）。
基线（只 import 引擎）：DuckDB 66.0 MB · Polars 55.8 MB。

| 实现 | 峰值 RSS (MB) | 减去基线 | 结果行数 |
| --- | --- | --- | --- |
| DuckDB 不限内存 | 271.2 | 205.2 | 100,000 |
| DuckDB 50MB 上限（落盘） | 156.0 | 90.0 | 100,000 |
| Polars in-memory | 265.7 | 209.9 | 100,000 |
| Polars streaming | 251.2 | 195.4 | 100,000 |

- DuckDB 侧的关键对照是前两行：**同一个引擎、同一个查询，只把上限从「不设」改成那个试出来的值** ——
  多出来的内存从 205.2 MB 降到 90.0 MB，上限确实在起作用。
- 但**别把它当成「进程 RSS 的上限」**：它管的是 DuckDB 自己记账的那部分
  （扫描缓冲、哈希表、排序空间），进程里还有引擎常驻开销和分配器碎片。
  它是**引擎账本的上限**，不是操作系统账本的上限。
- Polars 侧：换流式引擎确实省了一点 —— 但**省的是峰值，不是上限**，
  而且不管选哪个引擎，都**没有任何一个旋钮**能把它钉在某个数上。

实测枚举（Polars 2.1）：

- `pl.Config` 里名字带 `mem` 的设置项 → `[]`
- `pl.exceptions` 里名字带 `memory` 的异常 → `[]`

一个都没有。Polars 的内存是 Python 和 Rust 运行时直接向系统要的，
中间**没有一层「记账 + 拦截」**。

| 能力 | DuckDB | Polars |
| --- | --- | --- |
| 内存上限 | `SET memory_limit='50MB'` | 没有 |
| 溢写落盘 | `SET temp_directory='...'` | 没有 |
| 批大小 | 随上限自动调整 | `set_streaming_chunk_size()` |
| 超限时的异常 | `OutOfMemoryException` | 没有 |

两边的**失败方式**完全不同：

- DuckDB：失败发生在**进程内**，是一个能被 try/except 接住的 `OutOfMemoryException`，
  报错里写着「Reducing the number of threads」「Increasing the memory limit」——
  失败是**可诊断、可修复**的，连接还能接着用。
- Polars：没有上限，也就没有「预算超了」的判定点。真到分配不出来的时候，
  是 Rust 运行时 abort 掉整个进程（或者被系统的 OOM killer 干掉），
  Python 侧什么都拿不到 —— 没有异常、没有 traceback、没有部分结果。

→ **DuckDB 的内存上限是一套能兑现的机制，Polars 压根没有这套机制。**
迁过去之前得自己加一层：用 cgroup / 容器限额把进程框住，
否则「跑一半被 kill」这种事在生产环境里最难查。

### 7. 代价：落盘比全放内存慢多少

| 实现 | 中位数 (ms) | 最快 (ms) | 结果行数 |
| --- | --- | --- | --- |
| DuckDB 不限内存 | 79.6 | 76.1 | 100,000 |
| DuckDB 50MB 上限（落盘） | 165.2 | 160.6 | 100,000 |
| Polars in-memory | 41.9 | 39.7 | 100,000 |
| Polars streaming | 35.7 | 32.8 | 100,000 |

对账（两个引擎对同一份 parquet 做的同一个聚合，逐组比对）：
**✅ 100000 行 × 2 列全部一致**。
（落盘那一行跑了 5 次，期间最多写了 35,848,192 字节到 `temp_directory`。）

口径：四个实现跑的是**同一个聚合**（10 万个分组、每组求和），读的是**同一份 parquet**，
结果的行数一样（上面的对账已确认）。计时的范围是「从 parquet 读进来到拿到最终结果」，
**包含读盘**。DuckDB 两行都固定 4 线程 —— 两行之间只差一个 `memory_limit`，
所以它们可以直接比大小。Polars 两行用的是引擎默认线程池；跨引擎只能看量级，别当成严格对比。

解读：

- 落盘的代价就是**多了一轮磁盘 I/O**：中间状态本来在内存里，现在要写出去再读回来。
  差距的大小取决于溢写了多少 —— 溢写得多差距就大；工作集本来就不大的话，差距接近零。
- 所以「把 memory_limit 调小」是有代价的，但这个代价是**可预期的**：
  它换来的是「不管机器内存多小都能跑完」。
- DuckDB 和 Polars 在这里没有本质差别 —— 两边都能在低内存下跑完。
  真正的差别在**失败的那一侧**（场景 6）：DuckDB 给你一个带解法的异常，
  Polars 那边连一个内存异常类型都没有，进程被带走你就什么也拿不到了。

## 目录结构

```
duckdb-polars-demo/
├── README.md
├── pyproject.toml          # uv 项目定义与依赖
├── .python-version         # 锁定 Python 3.13
├── .github/
│   └── workflows/
│       └── ci.yml          # CI：Linux + macOS matrix，跑全部 demo 并把输出存成 artifact
├── data/                   # 生成的数据（.gitignore 排除，用脚本重新生成）
│   ├── orders.parquet
│   ├── users.parquet
│   ├── demo.duckdb
│   ├── big_orders.parquet      # streaming_demo.py 的大数据（按需生成）
│   ├── stream_*.parquet        # streaming_demo.py 的流式写出产物
│   ├── scale_orders.parquet    # scaling_demo.py 的大数据（按需生成）
│   ├── scale_users.parquet
│   ├── asof_trades.parquet     # asof_demo.py：成交
│   ├── asof_quotes.parquet     # asof_demo.py：报价
│   ├── json_events.ndjson      # json_demo.py：事件流
│   ├── json_bigint.ndjson      # json_demo.py：大整数样本
│   ├── index_customers_sorted_*.parquet  # index_demo.py：按主键排序
│   ├── index_customers_shuf_*.parquet    # index_demo.py：打乱行序
│   ├── decimal_amounts_*.parquet         # decimal_demo.py：金额样本
│   ├── write_demo/                       # write_demo.py：写入产物
│   ├── csv_demo/                         # csv_demo.py：CSV 样本与产物
│   └── tz_demo/                          # tz_demo.py：两边各写一份时间戳
├── src/
│   ├── common.py           # 唯一事实来源：口径常量 + 结果等价性校验 + 终端排版
│   ├── gen_data.py         # 造数
│   ├── duckdb_demo.py      # DuckDB / SQL 实现
│   ├── polars_demo.py      # Polars / DataFrame 实现
│   ├── sql.py              # 临时跑一条 SQL（不依赖 duckdb CLI 的版本）
│   ├── interop.py          # 互操作演示：Parquet / Arrow 接力 / 类型对齐
│   ├── dialect_demo.py     # 方言差异专场：8 个静默算错的场景
│   ├── streaming_demo.py   # 流式 vs 物化：内存随数据量怎么变
│   ├── udf_demo.py         # 自定义函数：两边的 UDF 代价差很多
│   ├── scaling_demo.py     # 线程扩展性：加速比 / 并行效率 / 轮间波动
│   ├── asof_demo.py        # ASOF JOIN：时间序列 join 的语义差异
│   ├── json_demo.py        # JSON 与嵌套类型：类型推断 / 精度 / 互转
│   ├── index_demo.py       # 索引与点查：ART 索引与无索引的点查
│   ├── decimal_demo.py     # 金额与 Decimal：浮点求和误差与精确金额的三条路
│   ├── null_demo.py        # NULL 与三值逻辑：= / JOIN / SUM / COUNT(DISTINCT) / NOT IN
│   ├── join_demo.py        # JOIN 全景：七种连接、输出列结构、重复键放大
│   ├── write_demo.py       # 写入路径与事务：覆盖 / 原子替换 / ACID
│   ├── csv_demo.py         # CSV 读写：类型推断 / 采样窗口 / 往返保真
│   ├── tz_demo.py          # 日期时间与时区：精度 / 会话时区 / 夏令时 / Parquet 往返
│   ├── order_demo.py       # 排序稳定性：NULL 位置 / 并列顺序 / LIMIT 后果
│   ├── partition_demo.py   # 多文件与分区表：glob / schema 冲突 / 分区裁剪
│   ├── window_demo.py      # 窗口函数：默认 frame / frame 家族 / 命名窗口 / QUALIFY
│   ├── agg_demo.py         # 聚合语义：分位数插值 / 众数 / arg_max 并列 / 整数溢出
│   ├── setop_demo.py       # 集合运算与去重：NULL 参与比较 / UNION / DISTINCT ON
│   ├── regex_demo.py       # 正则与字符串：分组编号 / 切分 / 位置 / 字节与字符
│   ├── dml_demo.py         # 增删改：UPDATE / upsert / 子查询快照 / 事务
│   ├── schema_demo.py      # Schema 演进：ALTER TABLE / 大小写 / 读不一致的 schema
│   ├── timeseries_demo.py  # 时间轴与序列生成：端点语义 / 补齐空档 / 截断 / 间隔
│   ├── approx_demo.py      # 近似 vs 精确：HLL 误差 / 近似分位数 / 值不值
│   ├── arith_demo.py       # 算术边界：除法 / 取模符号 / 舍入 / 溢出 / 除零
│   ├── groupsets_demo.py   # 多维聚合：GROUPING SETS / ROLLUP / CUBE
│   ├── pivot_demo.py       # 透视与重塑：PIVOT / UNPIVOT
│   ├── nested_demo.py      # 嵌套类型：LIST / STRUCT / MAP / lambda 语法变更
│   ├── plan_demo.py        # 查询计划：EXPLAIN 与投影裁剪
│   ├── sample_demo.py      # 采样与随机：三种采样方法 / 种子
│   ├── constraint_demo.py  # 约束与完整性：PK / FK / CHECK / 序列
│   ├── ddl_demo.py         # 视图、宏与持久化对象：VIEW / MACRO / SEQUENCE
│   ├── memory_demo.py      # 内存上限与落盘：memory_limit / temp_directory
│   ├── exists_demo.py      # 存在性查询与半连接：EXISTS / IN / NOT IN
│   ├── recursive_demo.py   # 递归 CTE 与迭代计算：WITH RECURSIVE
│   ├── fault_demo.py       # 容错模式与坏数据：ignore_errors / store_errors
│   └── benchmark.py        # 计时 + 等价性校验
└── scripts/
    ├── bench.sh            # 一键跑完整流程
    └── ci_demos.sh         # CI 用的批量跑法：跑全部 demo，出汇总表
```

## 设计约定

- **唯一事实来源**：时间窗口、维度取值、列顺序、比较主键全部集中在 `src/common.py`，两个实现共用，避免口径漂移。
- **结果先对账再比快慢**：`common.compare_frames()` 先按主键排序消除行序差异，再逐列比对（浮点用相对/绝对容差，其余列要求完全相等，含 NULL 位置；NaN 与 NaN 视为相等，因为 `math.isclose(nan, nan)` 是 False）。任一查询不一致，基准测试以退出码 1 结束。
- **计时口径**：只覆盖查询执行与结果物化，不含进程启动和连接建立；默认预热 1 次（Polars 的惰性计划在首次 `collect()` 时才编译）。UDF 对比先把输入物化，量的是「算这条规则」的代价，不含扫描 Parquet 的时间。
- **内存口径**：不用 RSS 差值下结论（子进程会继承父进程峰值、分配器 arena 会污染差值），改用对象自己报的字节数（`nbytes` / `estimated_size()`），确定性可复现。
- **输入先物化再计时**：性能对比前先把输入准备好（Parquet 落盘、DataFrame 物化成表），别把「读文件」和「算」混在同一个数字里；两者确实有先后依赖时（ASOF JOIN 的排序）就分别列出来。
- **交错轮次**：共享机器上负载会漂，所以把所有「引擎 × 线程数 × 查询」的组合**交错跑若干轮**（每轮把全部组合测一遍），而不是「一个组合测完再测下一个」。同时给出「各轮中位数的中位数」和「所有轮次里的最快一次」两个口径。
- **结论跟着测量走**：数字和叙事冲突时改叙事，不改数字；脚本每次都打印当时的机器状态（核数 + load1），方便判断数字是不是在有竞争时测到的。
- **可复现**：造数固定随机种子，基准测试打印 Python / 引擎版本、CPU 核数、平台与进程峰值 RSS。
- **演示脚本都要能独立跑通**：每个脚本自己造数、自己对账，退出码非 0 就是有问题。
- **临时数据用 `tempfile.mkdtemp()`**：新 demo 需要现造文件（多文件、分区目录、CSV 样本）时一律落到系统临时目录，用完即弃 —— **绝不覆盖 `data/` 下已有的文件**，也不做无条件的 `rm -rf`。
- **DuckDB 连接要读干净**：一次 `execute()` 之后必须把结果 `fetchall()` 取完，否则连接上留着未完成的结果，下一次查询会抛 `ResourceInUseException`；用 `con.sql()` 拿到的 relation 要显式 `close()`。
- **DuckDB 的 TIMESTAMPTZ 需要 pytz**：直接取成 Python 对象会报 `Required module 'pytz' failed to import`；走 Arrow 那条路不需要，Polars 用标准库 `zoneinfo` 也没这个依赖。

## 参考资料

- DuckDB 文档：<https://duckdb.org/docs/>
- Polars 文档：<https://docs.pola.rs/>
- DuckDB 与 Arrow 互操作：<https://duckdb.org/docs/guides/python/sql_on_arrow>
- DuckDB ASOF JOIN：<https://duckdb.org/docs/guides/sql_features/asof_join>
- DuckDB JSON：<https://duckdb.org/docs/stable/data/json/overview>
- Polars `join_asof`：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join_asof.html>
- Polars `str.json_decode`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.str.json_decode.html>
- DuckDB DECIMAL：<https://duckdb.org/docs/stable/sql/data_types/numeric>
- DuckDB 时间类型：<https://duckdb.org/docs/stable/sql/data_types/timestamp>
- DuckDB 时区：<https://duckdb.org/docs/stable/guides/timezones>
- DuckDB 多文件读取：<https://duckdb.org/docs/stable/data/parquet/overview>
- DuckDB Hive 分区：<https://duckdb.org/docs/stable/data/parquet/hive_partitioning>
- DuckDB CSV 读取：<https://duckdb.org/docs/stable/data/csv/overview>
- DuckDB 事务：<https://duckdb.org/docs/stable/connect/concepts>
- Polars `scan_parquet`：<https://docs.pola.rs/api/python/stable/reference/polars/api/polars.scan_parquet.html>
- Polars `replace_time_zone` / `convert_time_zone`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.dt.replace_time_zone.html>
- Polars `read_csv`：<https://docs.pola.rs/api/python/stable/reference/polars/api/polars.read_csv.html>
- Polars `write_parquet`：<https://docs.pola.rs/api/python/stable/reference/polars/api/polars.DataFrame.write_parquet.html>
- Polars `sort`（`maintain_order`）：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.sort.html>
- Polars `n_unique`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.n_unique.html>
- DuckDB 窗口函数：<https://duckdb.org/docs/stable/sql/functions/window_functions>
- DuckDB 正则表达式：<https://duckdb.org/docs/stable/sql/functions/regular_expressions>
- DuckDB 集合运算：<https://duckdb.org/docs/stable/sql/query_syntax/setops>
- DuckDB ALTER TABLE：<https://duckdb.org/docs/stable/sql/statements/alter>
- DuckDB 近似聚合函数：<https://duckdb.org/docs/stable/sql/functions/aggregate_functions>
- Polars `over`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.over.html>
- Polars `upsample`：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.upsample.html>
- Polars `approx_quantile`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.approx_quantile.html>
- DuckDB GROUPING SETS：<https://duckdb.org/docs/stable/sql/query_syntax/groupby>
- DuckDB PIVOT：<https://duckdb.org/docs/stable/sql/statements/pivot>
- DuckDB 嵌套类型：<https://duckdb.org/docs/stable/sql/data_types/struct>
- DuckDB 宏：<https://duckdb.org/docs/stable/sql/macro>
- DuckDB 约束：<https://duckdb.org/docs/stable/sql/constraints>
- DuckDB 采样：<https://duckdb.org/docs/stable/sql/samples>
- DuckDB 内存管理：<https://duckdb.org/docs/stable/guides/performance/how_to_tune_workloads>
- DuckDB `WITH RECURSIVE`：<https://duckdb.org/docs/stable/sql/query_syntax/with>
- DuckDB 算术函数：<https://duckdb.org/docs/stable/sql/functions/math>
- Polars `pivot` / `unpivot`：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.pivot.html>
- Polars `LazyFrame.explain`：<https://docs.pola.rs/api/python/stable/reference/lazyframe/api/polars.LazyFrame.explain.html>
- Polars `sample`：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.sample.html>
- Polars `List` 命名空间：<https://docs.pola.rs/api/python/stable/reference/series/api/polars.Series.list.html>
- Polars `join`（semi / anti）：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join.html>
