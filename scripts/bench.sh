#!/usr/bin/env bash
# 一键跑完整基准：造数 → 跑对比 → 输出结果
# 用法: scripts/bench.sh [订单行数] [重复次数]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ROWS="${1:-1000000}"
REPEAT="${2:-5}"

echo "### 造数（${ROWS} 行）"
uv run python src/gen_data.py --rows "$ROWS"

echo
echo "### DuckDB / Polars 对比（重复 ${REPEAT} 次）"
uv run python src/benchmark.py --repeat "$REPEAT"
