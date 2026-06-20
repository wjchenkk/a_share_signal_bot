#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output trade_output
ACCOUNT="${ACCOUNT:-100000}"
PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
TRADE_MODE="${TRADE_MODE:-intraday}"
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python main.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out output \
  --account "$ACCOUNT" \
  --tail \
  --auto-prune \
  > output/last_run.log 2>&1
# 同步生成交易生命周期计划：T日信号 -> T+1买入计划；已有持仓 -> 止损/止盈/退出/加仓建议。
python trade_manager.py \
  --action all \
  --portfolio "$PORTFOLIO" \
  --state trade_state.csv \
  --signals-out output \
  --config config.example.yml \
  --out trade_output \
  --account "$ACCOUNT" \
  --mode "$TRADE_MODE" \
  --sync \
  > trade_output/last_tail_trade_manager_run.log 2>&1 || true
cat output/latest_message.txt
if [ -f trade_output/latest_trade_plan.txt ]; then
  printf '\n\n===== 持仓/交易计划 =====\n'
  cat trade_output/latest_trade_plan.txt
fi
