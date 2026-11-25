#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
cd "$SCRIPT_DIR"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || true)"
  if [ -z "$PYTHON" ]; then
    echo "未找到 Python 解释器，请先运行: bash \"$SCRIPT_DIR/setup.sh\"" >&2
    exit 1
  fi
  echo "提示：未找到项目虚拟环境，暂使用系统 Python：$PYTHON" >&2
  echo "      建议先运行: bash \"$SCRIPT_DIR/setup.sh\"" >&2
fi
exec "$PYTHON" "$SCRIPT_DIR/loci/run.py"
