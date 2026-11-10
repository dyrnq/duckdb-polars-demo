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
uv run python src/interop.py         # 互操作演示：同一份 Parquet + Arrow 零拷贝接力
uv run python src/dialect_demo.py    # 方言差异专场：NULL 排序 / NaN / 透视表 / 递归 CTE …
uv run python src/streaming_demo.py  # 流式 vs 物化：内存随数据量怎么变
uv run python src/udf_demo.py        # 自定义函数：两边「Python UDF」代价差很多
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
| 2 | 整数求和类型 | `SUM(int32)` → HUGEINT（Arrow 里是 `decimal[38,0]`） | `sum()` 保持输入类型（Int32 → Int32） | 两边都转 `Int64` / `::BIGINT` |
| 3 | 计数类型 | `COUNT(*)` → BIGINT | `pl.len()` → UInt32 | 两边都转 Int64 |
| 4 | 窗口过滤 | 有 `QUALIFY` 子句 | 没有等价语法 | 先 `with_columns` 算窗口，再 `filter` |
| 5 | 浮点求和顺序 | 并行 + 向量化，顺序不保证 | 同左 | `SUM` 末位可能有 1e-9 级差异，校验用容差（`rtol=1e-9`）而非精确相等 |
| 6 | DuckDB 2.0 Python API | `con.execute(sql).arrow()` 已移除 | —— | 用 `con.sql(query, params).to_arrow_table()`；参数绑定受支持，且是 relation（每次读取都重新执行），不走 DB-API 就不会多一次行拷贝 |
| 7 | 存储格式版本 | 2.x 写 v999，1.5.3 的 CLI 只认 v64~v68 | —— | 探索数据走 `src/sql.py`（同一个 Python DuckDB），别指望老 CLI |
| 8 | NULL 排序位置 | 升序默认 NULLS **LAST** | 默认 NULLS **FIRST** | Polars `sort(nulls_last=True)` |
| 9 | 数值字面量类型 | `CASE WHEN … THEN 100.0` → **decimal[4,1]** | `pl.lit(100.0)` → Float64 | DuckDB 侧 `::DOUBLE` |
| 10 | 「Python UDF」的粒度 | `type='native'` **逐行**调用 | `map_batches` **按批**给数据 | DuckDB 用 `type='arrow'`，且函数体优先用 numpy |

> 第 6 条的坑：`con.execute(sql)` 在 2.0 里返回的是连接对象而不是结果，`.arrow()` 已经不存在；退而用 `con.to_arrow_table()` 会警告 "makes an extra copy of the rows" —— 那次拷贝会让基准测试不公平地惩罚 DuckDB。

第 8~10 条在下面三节里展开：`方言差异专场`、`流式处理与内存`、`自定义函数（UDF）`。

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
│   └── stream_*.parquet        # streaming_demo.py 的流式写出产物
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
│   └── benchmark.py        # 计时 + 等价性校验
└── scripts/
    └── bench.sh            # 一键跑完整流程
```

## 设计约定

- **唯一事实来源**：时间窗口、维度取值、列顺序、比较主键全部集中在 `src/common.py`，两个实现共用，避免口径漂移。
- **结果先对账再比快慢**：`common.compare_frames()` 先按主键排序消除行序差异，再逐列比对（浮点用相对/绝对容差，其余列要求完全相等，含 NULL 位置；NaN 与 NaN 视为相等，因为 `math.isclose(nan, nan)` 是 False）。任一查询不一致，基准测试以退出码 1 结束。
- **计时口径**：只覆盖查询执行与结果物化，不含进程启动和连接建立；默认预热 1 次（Polars 的惰性计划在首次 `collect()` 时才编译）。UDF 对比先把输入物化，量的是「算这条规则」的代价，不含扫描 Parquet 的时间。
- **内存口径**：不用 RSS 差值下结论（子进程会继承父进程峰值、分配器 arena 会污染差值），改用对象自己报的字节数（`nbytes` / `estimated_size()`），确定性可复现。
- **可复现**：造数固定随机种子，基准测试打印 Python / 引擎版本、CPU 核数、平台与进程峰值 RSS。
- **演示脚本都要能独立跑通**：每个脚本自己造数、自己对账，退出码非 0 就是有问题。

## 参考资料

- DuckDB 文档：<https://duckdb.org/docs/>
- Polars 文档：<https://docs.pola.rs/>
- DuckDB 与 Arrow 互操作：<https://duckdb.org/docs/guides/python/sql_on_arrow>
