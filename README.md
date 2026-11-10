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
uv run python src/interop.py         # 互操作演示：同一份 Parquet + Arrow 零拷贝接力
uv run python src/dialect_demo.py    # 方言差异专场：NULL 排序 / NaN / 透视表 / 递归 CTE …
uv run python src/streaming_demo.py  # 流式 vs 物化：内存随数据量怎么变
uv run python src/udf_demo.py        # 自定义函数：两边「Python UDF」代价差很多
uv run python src/scaling_demo.py    # 线程扩展性：1/2/4/8 线程各跑一遍，看加速比与并行效率
uv run python src/asof_demo.py       # ASOF JOIN：时间序列 join 的语义差异（并列 / 排序 / tolerance）
uv run python src/json_demo.py       # JSON 与嵌套类型：类型推断、精度损失、STRUCT/LIST 互转
uv run python src/index_demo.py      # 索引与点查：ART 索引的作用范围与代价，以及 Polars 的点查方案
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

后面的四个 demo 各带一份自己的数据（都是脚本自己造的，重复跑同一个规模就直接复用）：

| 文件 | 规模 | 列 | 用来演示 |
| --- | --- | --- | --- |
| `scale_orders.parquet` / `scale_users.parquet` | 400 万订单 / 5 万用户 | 与 `orders`/`users` 同构 | 线程扩展性（量足够大才看得出线程差异） |
| `asof_trades.parquet` | 40 万笔成交 | `symbol`、`ts`、`qty` | ASOF JOIN |
| `asof_quotes.parquet` | 20 万条报价 | `symbol`、`ts`、`px` | ASOF JOIN（单独一份是刻意的：时间序列 join 的右表要能乱序，才能测出排序要求的坑） |
| `json_events.ndjson` | 20 万行 | `event_id`、`user_id`、`event_type`、`ts`、`amount`、`props`（含嵌套 STRUCT/LIST） | JSON 与嵌套类型 |
| `json_bigint.ndjson` | 3 行 | 超大整数 | JSON 大整数精度 |
| `index_customers_sorted_*.parquet` | 500 万行 | `customer_id`、`name`、`region`、`balance` | 索引与点查（**按主键排序**） |
| `index_customers_shuf_*.parquet` | 500 万行 | 同上（**整行随机置换**） | 索引与点查（对照组） |

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

> 第 6 条的坑：`con.execute(sql)` 在 2.0 里返回的是连接对象而不是结果，`.arrow()` 已经不存在；退而用 `con.to_arrow_table()` 会警告 "makes an extra copy of the rows" —— 那次拷贝会让基准测试不公平地惩罚 DuckDB。

第 8~10 条在下面三节里展开：`方言差异专场`、`流式处理与内存`、`自定义函数（UDF）`；
第 11~14 条在 `ASOF JOIN（时间序列 join）`、第 15~17 条在 `JSON 与嵌套类型`、第 18~20 条在 `索引与点查` 里展开。

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

实测（20 万行，本机，两组规则）：

| 引擎 | 写法 | 实现细节 | 轻量规则 | 计算密集规则 |
| --- | --- | --- | --- | --- |
| DuckDB | 原生 SQL | `CASE WHEN` | **7.3 ms** | **7.6 ms** |
| DuckDB | Python UDF | `type='native'`（**逐行**） | 44 ms | 60 ms |
| DuckDB | Python UDF | `type='arrow'`（批）+ pyarrow.compute | 62 ms | 144 ms |
| DuckDB | Python UDF | `type='arrow'`（批）+ **numpy** | 23 ms | 21 ms |
| Polars | 原生表达式 | `when/then` | **2.2 ms** | 7.5 ms |
| Polars | `map_batches` | 批内**逐行** Python | 35 ms | 53 ms |
| Polars | `map_batches` | 批内**向量化** | 1.5 ms | 8.2 ms |

七种写法结果逐值一致 ✅。三个反直觉的发现：

1. **同样叫「Python UDF」，两个引擎的默认粒度不一样。** DuckDB 的 `type='native'` 是**逐行**调用
   （每行跨一次 Python/C 边界），Polars 的 `map_batches` 拿到的**是整批**。
2. **DuckDB 的 arrow UDF 是批式接口，但函数体怎么写决定成败** —— 同一批数据，
   numpy 函数体比 pyarrow.compute 函数体快 **2.8×~6.8×**。原因是 `pc.if_else` 在
   ChunkedArray 上做标量广播很慢，numpy 走的是连续内存。
3. **批式接口不一定比逐行快。** 轻量规则下，用 pyarrow.compute 写的 arrow UDF（62 ms）
   甚至比逐行 UDF（44 ms）还慢。

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
│   └── index_customers_shuf_*.parquet    # index_demo.py：打乱行序
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

## 参考资料

- DuckDB 文档：<https://duckdb.org/docs/>
- Polars 文档：<https://docs.pola.rs/>
- DuckDB 与 Arrow 互操作：<https://duckdb.org/docs/guides/python/sql_on_arrow>
- DuckDB ASOF JOIN：<https://duckdb.org/docs/guides/sql_features/asof_join>
- DuckDB JSON：<https://duckdb.org/docs/stable/data/json/overview>
- Polars `join_asof`：<https://docs.pola.rs/api/python/stable/reference/dataframe/api/polars.DataFrame.join_asof.html>
- Polars `str.json_decode`：<https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.str.json_decode.html>
