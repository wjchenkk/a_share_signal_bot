#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backtest_output
ACCOUNT="${ACCOUNT:-200000}"
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python backtest.py \
  --pool stock_pool.csv \
  --config config.example.yml \
  --out backtest_output \
  --account "$ACCOUNT" \
  --years 2 \
  "$@"
