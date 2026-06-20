#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${PYTHON:-}" ]; then
  py="$PYTHON"
elif [ -x .venv/bin/python ]; then
  py=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py="python3"
else
  echo "未找到可用 Python 解释器" >&2
  exit 127
fi

"$py" -m unittest discover -s tests -p 'test_*.py'
"$py" -m compileall -q main.py backtest.py backtest_position_strategy.py hot_pool.py portfolio_manager.py position_monitor.py trade_manager.py
if [ -d a_share_signal_bot ]; then
  "$py" -m compileall -q a_share_signal_bot
fi
