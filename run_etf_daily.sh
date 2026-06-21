#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

ACCOUNT="${ACCOUNT:-200000}"
ETF_POOL="${ETF_POOL:-etf_pool.csv}"
ETF_OUT="${ETF_OUT:-etf_output}"
CONFIG="${CONFIG:-config.example.yml}"
REFRESH="${REFRESH:-0}"
ETF_PORTFOLIO="${ETF_PORTFOLIO:-etf_portfolio.csv}"

mkdir -p "$ETF_OUT"

if [ ! -f "$ETF_POOL" ]; then
  echo "ETF池文件不存在：$ETF_POOL" >&2
  echo "可先执行：cp etf_pool_sample.csv etf_pool.csv，或设置 ETF_POOL=/path/to/etf_pool.csv" >&2
  exit 1
fi

if [ -x .venv/bin/python ]; then
  py=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py="python3"
else
  echo "未找到可用 Python 解释器" >&2
  exit 127
fi

refresh_args=()
if [ "$REFRESH" = "1" ] || [ "$REFRESH" = "true" ] || [ "$REFRESH" = "TRUE" ]; then
  refresh_args=(--refresh)
fi

"$py" etf_strategy.py \
  --pool "$ETF_POOL" \
  --config "$CONFIG" \
  --out "$ETF_OUT" \
  --account "$ACCOUNT" \
  "${refresh_args[@]}" \
  > "$ETF_OUT/last_etf_strategy_daily_run.log" 2>&1

"$py" etf_rotation.py \
  --mode rotate \
  --pool "$ETF_POOL" \
  --config "$CONFIG" \
  --out "$ETF_OUT" \
  --account "$ACCOUNT" \
  "${refresh_args[@]}" \
  > "$ETF_OUT/last_etf_rotation_daily_run.log" 2>&1

trade_args=(
  etf_trade_manager.py
  --portfolio "$ETF_PORTFOLIO"
  --targets "$ETF_OUT/latest_etf_rotation_positions_raw.csv"
  --candidates "$ETF_OUT/latest_etf_rotation_candidates_raw.csv"
  --config "$CONFIG"
  --out "$ETF_OUT"
  --account "$ACCOUNT"
)
if [ -n "${ETF_REBALANCE:-}" ]; then
  trade_args+=(--rebalance "$ETF_REBALANCE")
fi
if [ "${ETF_FORCE_REBALANCE:-0}" = "1" ]; then
  trade_args+=(--force-rebalance)
fi

"$py" "${trade_args[@]}" \
  > "$ETF_OUT/last_etf_trade_daily_run.log" 2>&1

{
  echo "==== ETF买点信号 ===="
  cat "$ETF_OUT/latest_etf_message.txt"
  echo
  echo "==== ETF轮动目标 ===="
  cat "$ETF_OUT/latest_etf_rotation_message.txt"
  echo
  echo "==== ETF持仓/调仓计划 ===="
  cat "$ETF_OUT/latest_etf_trade_plan.txt"
}
