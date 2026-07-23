#!/usr/bin/env bash
# 从脱敏引擎开发主仓同步 desensitize/ 包到 Loci（捆绑副本，随 Loci 备份）。
# 用法：bash update_engine.sh            # 默认主仓 ~/Desktop/qwen-desensitize
#       bash update_engine.sh /path/to/qwen-desensitize   # 指定主仓
set -euo pipefail

DEV="${1:-$HOME/Desktop/qwen-desensitize}"
SRC="$DEV/desensitize"
DST="$(cd "$(dirname "$0")" && pwd)/desensitize"

[ -d "$SRC" ] || { echo "✗ 找不到引擎主仓：$SRC"; echo "  传入路径：bash update_engine.sh /path/to/qwen-desensitize"; exit 1; }

echo "同步 $SRC  →  $DST"
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' "$SRC/" "$DST/"

echo "✓ 已同步 $(ls -1 "$DST"/*.py | wc -l | tr -d ' ') 个模块"
python3 -c "import sys; sys.path.insert(0,'$(dirname "$DST")'); from desensitize import desensitize_tree; print('✓ 捆绑引擎可导入')" \
  2>/dev/null || echo "⚠ 导入校验失败，检查依赖：pip install -r requirements.txt"
echo "提示：随后 git add desensitize && git commit 即可把更新带进备份。"
