#!/bin/bash
# ============================================================
# 新案件初始化
#
# 用法:
#   bash new_case.sh "年度-客户简称-案由"
#
# 可选参数:
#   --type   civil | arbitration | criminal | us_civil | mixed
#   --role   角色（见 bin/intake.py 的白名单）
#   --party-us 我方当事人名称
#   --party-op 对方当事人名称
#
# 目录创建、路径校验、交互式选择和 YAML 写入统一由 Python 完成。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
WS_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"

PYTHON="$WS_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "❌ 未找到项目虚拟环境解释器: $PYTHON" >&2
  echo "   请先运行: bash \"$WS_DIR/setup.sh\"" >&2
  exit 1
fi

exec "$PYTHON" "$WS_DIR/bin/intake.py" --new-case "$@"
