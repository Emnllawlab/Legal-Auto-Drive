#!/bin/bash
# ============================================================
# 材料归档守护进程控制
#
# start/stop/status all go through auto_filer/daemon_control.py.  Keeping
# lifecycle decisions in one Python module makes the GUI and this CLI share
# the same lock, state validation, readiness wait, and stop/reap behavior.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
WS_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
AF_DIR="$WS_DIR/auto_filer"
PYTHON="$WS_DIR/.venv/bin/python"

_python_path() {
  if [ -x "$PYTHON" ]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  # 兼容尚未运行 setup.sh 的开发环境；正式启动仍优先使用 .venv，
  # 由 loci/run.py 负责拒绝 Rosetta/x86_64 进程。
  local system_python
  system_python="$(command -v python3 || true)"
  if [ -n "$system_python" ]; then
    echo "提示：未找到项目虚拟环境，暂使用系统 Python：$system_python" >&2
    echo "      建议先运行: bash \"$WS_DIR/setup.sh\"" >&2
    printf '%s\n' "$system_python"
    return 0
  fi
  echo "未找到 Python 解释器。请先运行: bash \"$WS_DIR/setup.sh\"" >&2
  return 1
}

_python() {
  local python_path
  python_path="$(_python_path)" || return 1
  "$python_path" "$@"
}

_control() {
  _python "$AF_DIR/daemon_control.py" --control-dir "$AF_DIR" "$@"
}

_env_value() {
  local key="$1"
  local fallback="$2"
  _control env-value --key "$key" --default "$fallback"
}

_inbox_dir() { _env_value INBOX_DIR "$WS_DIR/inbox"; }
_cases_dir() { _env_value CASES_DIR "$WS_DIR/cases"; }
_log_dir() { _env_value LOG_DIR "$WS_DIR/logs"; }

start() {
  _control start --python "$PYTHON" --cwd "$AF_DIR" --timeout 5
}

stop() {
  # daemon_control validates state+fingerprint+instance lock and waits for
  # actual process exit; it does not remove metadata on timeout.
  _control stop --timeout 5
}

status() {
  _control status
  local inbox
  inbox="$(_inbox_dir)"
  local cases
  cases="$(_cases_dir)"
  local pending
  pending=$(find "$inbox" -type f ! -name 'README*' ! -name '.gitkeep' \
    ! -name '.DS_Store' 2>/dev/null | wc -l | tr -d ' ')
  echo "📬 收件箱待处理: $pending 个文件"
  echo "📁 案件目录: $cases"
}

log() {
  local log_dir
  log_dir="$(_log_dir)"
  local log_file="$log_dir/filer_$(date +%Y%m).log"
  if [ -f "$log_file" ]; then
    tail -40 "$log_file"
  else
    echo "⚠ 暂无日志"
  fi
}

inbox_open() {
  local inbox
  inbox="$(_inbox_dir)"
  mkdir -p "$inbox"
  if [[ "${OSTYPE:-}" == "darwin"* ]]; then
    open "$inbox"
  else
    echo "收件箱: $inbox"
    ls -la "$inbox"
  fi
}

CMD="${1:-status}"
shift || true

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
    echo "启动 Loci → http://localhost:5081"
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
