#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p trade_output
ACCOUNT="${ACCOUNT:-200000}"
PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
source .venv/bin/activate
python trade_manager.py \
  --action advise \
  --portfolio "$PORTFOLIO" \
  --state trade_state.csv \
  --signals-out output \
  --config config.example.yml \
  --out trade_output \
  --account "$ACCOUNT" \
  --sync
