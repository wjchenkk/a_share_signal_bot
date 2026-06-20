#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p position_output
ACCOUNT="${ACCOUNT:-200000}"
PORTFOLIO="${PORTFOLIO:-portfolio.csv}"
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python position_monitor.py \
  --portfolio "$PORTFOLIO" \
  --config config.example.yml \
  --out position_output \
  --account "$ACCOUNT" \
  --window-only \
  > position_output/last_position_run.log 2>&1
cat position_output/latest_position_message.txt
