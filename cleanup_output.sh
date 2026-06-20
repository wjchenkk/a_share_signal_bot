#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p output
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
python - <<'PY'
from pathlib import Path
import yaml
from main import DEFAULT_CONFIG, deep_merge, cleanup_output_dir
cfg = DEFAULT_CONFIG.copy()
p = Path('config.example.yml')
if p.exists():
    cfg = deep_merge(cfg, yaml.safe_load(p.read_text(encoding='utf-8')) or {})
removed = cleanup_output_dir('output', cfg)
print(f'output 清理完成，删除 {len(removed)} 个历史文件。')
PY
