#!/bin/bash
# 快速启动 - 双击运行或拖到终端
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/bin/filer.sh" start
echo "收件箱路径:"
grep INBOX_DIR "$SCRIPT_DIR/.env" 2>/dev/null || echo "$SCRIPT_DIR/inbox"
