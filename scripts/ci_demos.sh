#!/usr/bin/env bash
# CI 用的批量跑法：把所有 demo 跑一遍，输出存盘，最后出一份汇总表。
#
# 用法：bash scripts/ci_demos.sh [订单行数] [重复次数] [输出目录]
#   订单行数   给支持 --rows 的 demo 用（默认 200000）
#   重复次数   给支持 --repeat 的 demo 用（默认 3）
#   输出目录   每个 demo 一个 .out，另加一份 SUMMARY.md（默认 ci-out）
#
# 为什么单独写个脚本，而不是把命令直接列在 workflow 里：
#   每个 demo 的规模开关名字并不统一（--rows / --repeat / --repeats /
#   --rounds / --files / --trades / --quotes），把这层对应关系收在一处，
#   本地和 CI 跑的就是同一套东西 —— CI 挂了能在本地原样复现。
#
# 规模刻意比各 demo 的默认值小：CI 要的是「跑通 + 存档」，不是测性能。
# 想看真实数字在本地跑：scripts/bench.sh 1000000 5
#
# 退出码：只要有一个 demo 失败就返回非零。但**所有** demo 都会跑完，
#         这样 CI 失败时也能从 artifact 里看到其它 demo 的输出。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

ROWS="${1:-200000}"
REPEAT="${2:-3}"
OUT="${3:-ci-out}"

mkdir -p "$OUT" || exit 1
rm -f "$OUT"/*.out "$OUT"/summary.tsv "$OUT"/SUMMARY.md

# 下面每个 demo 都用 `uv run --no-sync`（省掉 40 次重复的环境检查）。
# 代价是虚拟环境必须已经存在 —— CI 里前面已经 uv sync 过了，
# 本地第一次跑就先装一次。
if [ ! -d .venv ]; then
  echo "### 没找到 .venv，先装依赖"
  uv sync --locked || exit 1
  echo
fi

# 每个 demo 的规模开关不一样，这里一一对应。
# 格式：脚本名|传给它的参数（空 = 用它自己的默认值）
# 不在表里的 demo 一律不跑 —— 刻意不写成「遍历 src/*.py」：
#   sql.py 是交互工具（要传一条 SQL），interop.py 的 --extension 要联网装扩展。
DEMOS=(
  "gen_data|--rows $ROWS"
  "duckdb_demo|"
  "polars_demo|"
  "benchmark|--repeat $REPEAT --warmup 1"
  "interop|"
  "dialect_demo|"
  "streaming_demo|--rows $ROWS"
  "udf_demo|--rows $ROWS"
  "scaling_demo|--rows $ROWS --repeat $REPEAT --rounds 1 --threads 1,2,4,8"
  "asof_demo|--trades $((ROWS / 2)) --quotes $((ROWS / 4)) --repeat $REPEAT"
  "json_demo|--rows $ROWS --repeat $REPEAT"
  "index_demo|--rows $ROWS --repeats $REPEAT --rounds 1"
  "decimal_demo|--rows $ROWS --repeat $REPEAT"
  "null_demo|"
  "join_demo|--repeat $REPEAT"
  "write_demo|--repeat $REPEAT"
  "csv_demo|"
  "tz_demo|--repeat $REPEAT"
  "order_demo|--rows $ROWS --repeat $REPEAT"
  "partition_demo|--files 20 --repeat $REPEAT"
  "window_demo|--rows $ROWS --repeat $REPEAT"
  "agg_demo|--rows $ROWS --repeat $REPEAT"
  "setop_demo|--rows $ROWS --repeat $REPEAT"
  "regex_demo|--rows $ROWS --repeat $REPEAT"
  "dml_demo|--rows $ROWS --repeat $REPEAT"
  "schema_demo|--rows $ROWS --repeat $REPEAT"
  "timeseries_demo|--rows $ROWS --repeat $REPEAT"
  "approx_demo|--rows $ROWS --repeat $REPEAT"
  "arith_demo|--rows $ROWS --repeat $REPEAT"
  "groupsets_demo|--rows $ROWS --repeat $REPEAT"
  "pivot_demo|--rows $ROWS --repeat $REPEAT"
  "nested_demo|--rows $ROWS --repeat $REPEAT"
  "plan_demo|--rows $ROWS --repeat $REPEAT"
  "sample_demo|--rows $ROWS --repeat $REPEAT"
  "constraint_demo|--rows $ROWS --repeat $REPEAT"
  "ddl_demo|--rows $ROWS --repeat $REPEAT"
  "memory_demo|--rows $ROWS --repeat $REPEAT"
  "exists_demo|--rows $ROWS --repeat $REPEAT"
  "recursive_demo|--rows $ROWS --repeat $REPEAT"
  "fault_demo|--rows $ROWS --repeat $REPEAT"
)

failed=0
total=0
: > "$OUT/summary.tsv"

echo "### 规模：--rows $ROWS · --repeat $REPEAT · 输出目录 $OUT"
echo

for entry in "${DEMOS[@]}"; do
  name="${entry%%|*}"
  args="${entry#*|}"
  script="src/${name}.py"

  if [ ! -f "$script" ]; then
    echo "SKIP  $name（文件不存在）"
    continue
  fi

  total=$((total + 1))
  t0=$(date +%s)
  # shellcheck disable=SC2086
  uv run --no-sync python "$script" $args > "$OUT/${name}.out" 2>&1
  rc=$?
  t1=$(date +%s)
  dur=$((t1 - t0))

  if [ "$rc" -eq 0 ]; then
    status="✅"
  else
    status="❌"
    failed=$((failed + 1))
  fi

  printf '%s\t%s\t%s\t%s\n' "$name" "$status" "$dur" "$rc" >> "$OUT/summary.tsv"
  printf '%-2s %-18s %5ss  (exit=%s)  -> %s\n' "$status" "$name" "$dur" "$rc" "$OUT/${name}.out"
done

# ---------------------------------------------------------------- 汇总表
#
# 「逐值对账结论」取的是输出里**带比较意味**的那些行（提到一致/等价/对账/相同/相等，
# 且带 ✅ 或 ❌）—— 各 demo 的结论行长得很不一样（「-> ✅」和「结果等价 ✅」两种都有），
# 所以按关键词挑，不按固定格式匹配。
#
# 注意：输出里的 ❌ **不一定是失败** —— 有些是 demo 故意演示的语义差异
# （「❌ 静默回绕成了负数」）。所以这一列只是线索，真正的成败看退出码。
{
  echo "## Demo 运行汇总"
  echo
  echo "| demo | 结果 | 用时 | 逐值对账结论 |"
  echo "| --- | --- | --- | --- |"
  while IFS=$'\t' read -r name status dur rc; do
    # -e 而不是直接给模式：模式以 - 开头时会被当成选项（GNU grep 和 ugrep 都会）
    verdicts=$(grep -h -e '✅' -e '❌' "$OUT/${name}.out" 2>/dev/null \
      | grep -E -e '一致|等价|对账|相同|相等' \
      | sed -e 's/^[[:space:]]*//' -e 's/|/\\|/g' \
      | awk '!seen[$0]++' \
      | head -2 \
      | awk 'NR>1 {printf " <br> "} {printf "%s", $0}')
    [ -n "$verdicts" ] || verdicts="—"
    # shellcheck disable=SC2016  # 反引号是 Markdown 的代码标记，不是命令替换
    printf '| `%s` | %s | %ss | %s |\n' "$name" "$status" "$dur" "$verdicts"
  done < "$OUT/summary.tsv"
  echo
  echo "**$((total - failed))/$total 个 demo 通过**（退出码为 0）。完整的输出在 artifact 里。"
  echo
  echo "> 「逐值对账结论」里的 ❌ **不一定是失败** —— 有些是 demo 故意演示的语义差异"
  echo ">（比如「❌ 直接对账会挂」「❌ 不一致 —— Polars 丢了前导零」）。"
  echo "> 真正的成败看「结果」那一列（退出码）。"
} > "$OUT/SUMMARY.md"

echo
if [ "$failed" -eq 0 ]; then
  echo "全部 $total 个 demo 通过。汇总表：$OUT/SUMMARY.md"
  exit 0
fi
echo "$failed/$total 个 demo 失败。汇总表：$OUT/SUMMARY.md"
exit 1
