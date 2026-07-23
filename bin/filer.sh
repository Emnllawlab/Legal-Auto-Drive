#!/bin/bash
# ============================================================
# 材料归档守护进程控制
# 用法:
#   bash filer.sh start      后台守护
#   bash filer.sh stop       停止
#   bash filer.sh once       单次处理
#   bash filer.sh dry-run    试运行
#   bash filer.sh status     状态
#   bash filer.sh log        查看日志
#   bash filer.sh inbox      打开收件箱(Finder)
#
# 自定义路径（可附在任何子命令后）:
#   --workspace DIR    工作区根目录（自动推导 inbox/cases/logs）
#   --inbox DIR        指定收件箱路径
#   --cases-dir DIR    指定案件存放路径
#   --log-dir DIR      指定日志路径
#
# 示例:
#   bash filer.sh once --workspace ~/Documents/MyWorkspace
#   bash filer.sh once --cases-dir /Volumes/NAS/cases
#   bash filer.sh dry-run --inbox ~/Downloads/待归档
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
AF_DIR="$WS_DIR/auto_filer"

if [ -f "$WS_DIR/.env" ]; then
  source "$WS_DIR/.env"
fi

PID_FILE="$AF_DIR/.filer.pid"
LOG_DIR="${LOG_DIR:-$WS_DIR/logs}"
INBOX="${INBOX_DIR:-$WS_DIR/inbox}"

_python() {
  if command -v python3.11 &>/dev/null; then
    python3.11 "$@"
  elif command -v python3 &>/dev/null; then
    python3 "$@"
  else
    echo "❌ Python3 未安装"
    exit 1
  fi
}

# 从参数中提取自定义路径标志（--workspace/--inbox/--cases-dir/--log-dir）
# 其余参数忽略（safety: 只传白名单 flag）
_extract_path_args() {
  local result=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --workspace|--inbox|--cases-dir|--log-dir)
        result+=("$1" "$2")
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  echo "${result[@]}"
}

start() {
  # start 不支持 --workspace 等参数（守护进程通过 .env 配置路径）
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "⚠ 已在运行 (PID: $(cat "$PID_FILE"))"
    exit 0
  fi
  echo "👀 启动归档守护..."
  cd "$AF_DIR"
  nohup _python auto_filer.py --daemon -v > /dev/null 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅ 守护进程已启动 (PID: $(cat "$PID_FILE"))"
    echo "   收件箱: $INBOX"
    echo "   丢文件进去就会自动归档"
    echo "   如需自定义路径，请修改 .env 后重启"
  else
    echo "❌ 启动失败"
    rm -f "$PID_FILE"
    exit 1
  fi
}

stop() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      kill "$PID"
      echo "🛑 已停止 (PID: $PID)"
    fi
    rm -f "$PID_FILE"
  else
    echo "⚪ 未运行"
  fi
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅ 守护进程运行中 (PID: $(cat "$PID_FILE"))"
  else
    echo "⚪ 守护进程未运行"
  fi
  pending=$(find "$INBOX" -type f ! -name 'README*' ! -name '.gitkeep' ! -name '.DS_Store' 2>/dev/null | wc -l | tr -d ' ')
  echo "📬 收件箱待处理: $pending 个文件"
  echo "📁 案件目录: ${CASES_DIR:-$WS_DIR/cases}"
}

log() {
  local lf="$LOG_DIR/filer_$(date +%Y%m).log"
  if [ -f "$lf" ]; then
    tail -40 "$lf"
  else
    echo "⚠ 暂无日志"
  fi
}

inbox_open() {
  mkdir -p "$INBOX"
  if [[ "$OSTYPE" == "darwin"* ]]; then
    open "$INBOX"
  else
    echo "收件箱: $INBOX"
    ls -la "$INBOX"
  fi
}

CMD="${1:-status}"
shift || true   # 移除第一个参数，剩余传给 python

case "$CMD" in
  start)
    start
    ;;
  stop)
    stop
    ;;
  status)
    status
    ;;
  once)
    cd "$AF_DIR"
    _python auto_filer.py --once -v "$@"
    ;;
  dry-run)
    cd "$AF_DIR"
    _python auto_filer.py --once --dry-run -v "$@"
    ;;
  log)
    log
    ;;
  inbox)
    inbox_open
    ;;
  intake)
    _python "$SCRIPT_DIR/intake.py"
    ;;
  dashboard)
    echo "🌐 启动可视化控制台 → http://localhost:5080"
    cd "$WS_DIR/dashboard" && _python dashboard.py
    ;;
  loci)
    echo "🗂 启动 Loci → http://localhost:5081"
    _python "$WS_DIR/loci/run.py"
    ;;
  *)
    echo "用法: bash filer.sh {start|stop|status|once|dry-run|log|inbox|intake|dashboard|loci}"
    echo ""
    echo "自定义路径（附在 once/dry-run 后）:"
    echo "  --workspace DIR    工作区根目录"
    echo "  --inbox DIR        收件箱路径"
    echo "  --cases-dir DIR    案件文件夹路径"
    echo "  --log-dir DIR      日志路径"
    exit 1
    ;;
esac
