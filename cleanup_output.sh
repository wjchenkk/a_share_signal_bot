#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
  py=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  py="python3"
else
  echo "未找到可用 Python 解释器" >&2
  exit 127
fi

"$py" space_cleanup.py --config config.example.yml --out cleanup_output "$@"
