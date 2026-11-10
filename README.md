# duckdb-polars-demo

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
```

用 DuckDB 交互式探索数据：

```bash
uv run python src/sql.py "SELECT region, count(*) AS n FROM orders GROUP BY 1 ORDER BY n DESC"
uv run python src/sql.py --source parquet "SELECT * FROM users LIMIT 5"
```

> **别直接用 `duckdb data/demo.duckdb`**：DuckDB 2.x 写出的存储格式是 v999，而系统里装的
> duckdb CLI（1.5.3）只认 v64~v68，会报 `Trying to read a database file with version number 999`。
> 用 `src/sql.py` 走 Python 侧的 DuckDB，版本和项目里其他地方一致，不会有这个问题。

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

> 第 6 条的坑：`con.execute(sql)` 在 2.0 里返回的是连接对象而不是结果，`.arrow()` 已经不存在；退而用 `con.to_arrow_table()` 会警告 "makes an extra copy of the rows" —— 那次拷贝会让基准测试不公平地惩罚 DuckDB。

第 8~10 条在下面三节里展开：`方言差异专场`、`流式处理与内存`、`自定义函数（UDF）`；
第 11~14 条在 `ASOF JOIN（时间序列 join）`、第 15~17 条在 `JSON 与嵌套类型`、第 18~20 条在 `索引与点查` 里展开；
第 21~25 条在 `NULL 与三值逻辑`、第 26~27 条在 `金额与 Decimal`、第 28~29 条在 `JOIN 全景`、
第 30~33 条在 `写入路径与事务`、第 34~36 条在 `CSV 读写与往返保真`、第 37~41 条在 `日期时间与时区`、
第 42 条在 `排序稳定性`、第 43~47 条在 `多文件与分区表`、第 48 条在 `自定义函数（UDF）` 里展开。

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

## 目录结构

```
duckdb-polars-demo/
├── README.md
├── pyproject.toml          # uv 项目定义与依赖
├── .python-version         # 锁定 Python 3.13
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
│   └── benchmark.py        # 计时 + 等价性校验
└── scripts/
    └── bench.sh            # 一键跑完整流程
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
