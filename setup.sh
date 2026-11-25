#!/bin/bash
# ============================================================
# Loci RC 最小安全安装
# 用法: bash setup.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
WS_DIR="$SCRIPT_DIR"
ENV_FILE="$WS_DIR/.env"
CONFIG_FILE="$WS_DIR/auto_filer/config.yaml"
ABS_WS_DIR="$WS_DIR"

echo "================================================"
echo "  Loci RC - 最小安全安装"
echo "================================================"

reject_symlink() {
  local path="$1"
  local label="$2"
  if [ -L "$path" ]; then
    echo "❌ 配置路径安全检查失败：$label 是符号链接，拒绝继续：$path" >&2
    echo "   请将其替换为预期位置中的普通文件或目录后重试。" >&2
    exit 1
  fi
}

assert_workspace_path_safe() {
  local path="$1"
  local label="$2"
  local current="$path"
  # Check every path component inside the workspace.  A symlink in a parent
  # directory is just as dangerous as a symlink at the final file path.
  while [ "$current" != "$WS_DIR" ] && [ "$current" != "/" ]; do
    reject_symlink "$current" "$label"
    current="$(dirname "$current")"
  done
  reject_symlink "$WS_DIR" "工作区目录"
}

assert_control_plane_paths_safe() {
  # SCRIPT_DIR is already physical (pwd -P), but keep the check explicit so
  # this boundary remains clear if path construction changes later.
  reject_symlink "$WS_DIR" "工作区目录"
  reject_symlink "$ENV_FILE" ".env"
  reject_symlink "$CONFIG_FILE" "auto_filer/config.yaml"
  reject_symlink "$WS_DIR/auto_filer" "auto_filer 配置目录"
  reject_symlink "$WS_DIR/.env.example" ".env.example"
  reject_symlink "$WS_DIR/auto_filer/config.example.yaml" \
    "auto_filer/config.example.yaml"
  assert_workspace_path_safe "$WS_DIR/requirements.txt" "requirements.txt 路径"
  assert_workspace_path_safe "$WS_DIR/.venv" ".venv 路径"
  assert_workspace_path_safe "$WS_DIR/.venv/bin" ".venv/bin 路径"
  assert_workspace_path_safe "$WS_DIR/bin" "bin 路径"
  assert_workspace_path_safe "$WS_DIR/bin/filer.sh" "bin/filer.sh 路径"
  assert_workspace_path_safe "$WS_DIR/launch.command" "launch.command 路径"
}

require_regular_config_file() {
  local path="$1"
  local label="$2"
  assert_control_plane_paths_safe
  if [ ! -f "$path" ]; then
    echo "❌ 配置文件安全检查失败：$label 不是普通文件或不存在：$path" >&2
    exit 1
  fi
}

replace_placeholder() {
  local file="$1"
  local expression="$2"
  assert_control_plane_paths_safe
  require_regular_config_file "$file" "配置文件"
  if [ "$(uname -s)" = "Darwin" ]; then
    sed -i '' "$expression" "$file"
  else
    sed -i "$expression" "$file"
  fi
}

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

# Read one KEY=value entry without evaluating the value.  In particular, a
# value such as "$(touch /tmp/marker)" remains data and is never executed.
read_env_value() {
  local wanted="$1"
  local line key_name raw=""
  require_regular_config_file "$ENV_FILE" ".env"
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim_whitespace "$line")"
    case "$line" in
      ""|\#*)
        continue
        ;;
      export[[:space:]]*)
        line="${line#export}"
        line="$(trim_whitespace "$line")"
        ;;
    esac
    case "$line" in
      *=*)
        key_name="$(trim_whitespace "${line%%=*}")"
        [ "$key_name" = "$wanted" ] || continue
        raw="$(trim_whitespace "${line#*=}")"
        ;;
      *)
        continue
        ;;
    esac

    if [ "${#raw}" -ge 2 ]; then
      case "$raw" in
        \"*\") raw="${raw:1:${#raw}-2}" ;;
        \'*\') raw="${raw:1:${#raw}-2}" ;;
      esac
    fi
    printf '%s\n' "$raw"
    return 0
  done < "$ENV_FILE"
  return 0
}

expand_workspace_refs() {
  local value="$1"
  local workspace_root="$2"
  value="${value//\$\{WORKSPACE\}/$ABS_WS_DIR}"
  value="${value//\$WORKSPACE/$ABS_WS_DIR}"
  value="${value//\$\{CASE_WORKSPACE_ROOT\}/$workspace_root}"
  value="${value//\$CASE_WORKSPACE_ROOT/$workspace_root}"
  printf '%s\n' "$value"
}

escaped_workspace="$(printf '%s' "$ABS_WS_DIR" | sed 's/[\\&|]/\\&/g')"

assert_control_plane_paths_safe

if [ ! -e "$ENV_FILE" ] && [ ! -L "$ENV_FILE" ]; then
  assert_control_plane_paths_safe
  cp "$WS_DIR/.env.example" "$ENV_FILE"
  replace_placeholder "$ENV_FILE" "s|__WORKSPACE_ROOT__|$escaped_workspace|g"
  echo "✅ 已创建 .env"
else
  require_regular_config_file "$ENV_FILE" ".env"
  echo "✅ .env 已存在，跳过覆盖"
fi

if [ ! -e "$CONFIG_FILE" ] && [ ! -L "$CONFIG_FILE" ]; then
  assert_control_plane_paths_safe
  cp "$WS_DIR/auto_filer/config.example.yaml" "$CONFIG_FILE"
  replace_placeholder "$CONFIG_FILE" "s|\${WORKSPACE}|$escaped_workspace|g"
  echo "✅ 已创建 auto_filer/config.yaml"
else
  require_regular_config_file "$CONFIG_FILE" "auto_filer/config.yaml"
  echo "✅ auto_filer/config.yaml 已存在，跳过覆盖"
fi

PYTHON_BIN=""
for candidate in "${PYTHON_BIN_OVERRIDE:-}" python3.11 python3.10 python3.9 python3; do
  if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "❌ 未找到 Python 3，请先安装 Python 3。" >&2
  exit 1
fi

VENV_DIR="$WS_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  echo "🐍 使用 $PYTHON_BIN 创建项目虚拟环境..."
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    echo "❌ 无法创建 $VENV_DIR，请确认 bootstrap Python 已安装 venv/ensurepip。" >&2
    echo "   可尝试: $PYTHON_BIN -m ensurepip --upgrade" >&2
    exit 1
  fi
else
  echo "✅ 项目虚拟环境已存在，复用 $VENV_DIR"
fi

assert_workspace_path_safe "$VENV_DIR" ".venv 路径"
assert_workspace_path_safe "$VENV_DIR/bin" ".venv/bin 路径"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "❌ 项目虚拟环境解释器缺失: $VENV_PYTHON" >&2
  echo "   请删除不完整的 .venv 后重新运行: bash \"$WS_DIR/setup.sh\"" >&2
  exit 1
fi

case_workspace_root="$(read_env_value CASE_WORKSPACE_ROOT)"
case_workspace_root="$(expand_workspace_refs "$case_workspace_root" "$ABS_WS_DIR")"
[ -n "$case_workspace_root" ] || case_workspace_root="$ABS_WS_DIR"

inbox_dir="$(read_env_value INBOX_DIR)"
inbox_dir="$(expand_workspace_refs "$inbox_dir" "$case_workspace_root")"
[ -n "$inbox_dir" ] || inbox_dir="$case_workspace_root/inbox"

cases_dir="$(read_env_value CASES_DIR)"
cases_dir="$(expand_workspace_refs "$cases_dir" "$case_workspace_root")"
[ -n "$cases_dir" ] || cases_dir="$case_workspace_root/cases"

log_dir="$(read_env_value LOG_DIR)"
log_dir="$(expand_workspace_refs "$log_dir" "$case_workspace_root")"
[ -n "$log_dir" ] || log_dir="$case_workspace_root/logs"

reject_symlink "$inbox_dir" "INBOX_DIR 目标目录"
reject_symlink "$cases_dir" "CASES_DIR 目标目录"
reject_symlink "$log_dir" "LOG_DIR 目标目录"

echo "📁 创建 Loci 工作区目录..."
mkdir -p "$inbox_dir" "$cases_dir" "$log_dir"
mkdir -p "$cases_dir/_未分类/_待分类"
reject_symlink "$inbox_dir" "INBOX_DIR 目标目录"
reject_symlink "$cases_dir" "CASES_DIR 目标目录"
reject_symlink "$log_dir" "LOG_DIR 目标目录"
assert_control_plane_paths_safe
require_regular_config_file "$ENV_FILE" ".env"
require_regular_config_file "$CONFIG_FILE" "auto_filer/config.yaml"
chmod 700 "$inbox_dir" "$cases_dir" "$log_dir"
chmod 600 "$ENV_FILE" "$CONFIG_FILE"
echo "✅ 收件箱: $inbox_dir"
echo "✅ 案件目录: $cases_dir"
echo "✅ 日志目录: $log_dir"

assert_workspace_path_safe "$WS_DIR/requirements.txt" "requirements.txt 路径"
if [ ! -f "$WS_DIR/requirements.txt" ]; then
  echo "❌ 根 requirements 不存在或不是普通文件：$WS_DIR/requirements.txt" >&2
  exit 1
fi
echo "🐍 使用 $VENV_PYTHON 安装根 requirements..."
"$VENV_PYTHON" -m pip install -r "$WS_DIR/requirements.txt"

assert_workspace_path_safe "$WS_DIR/bin/filer.sh" "bin/filer.sh 路径"
assert_workspace_path_safe "$WS_DIR/launch.command" "launch.command 路径"
chmod +x "$WS_DIR/bin/filer.sh" "$WS_DIR/launch.command" 2>/dev/null || true

echo ""
echo "✅ Loci RC 安装完成。"
echo ""
echo "启动 Loci 前台："
echo "  cd \"$WS_DIR\""
echo "  ./launch.command"
echo "  $VENV_PYTHON loci/run.py"
echo "  bash bin/filer.sh loci"
echo ""
echo "安全默认：归档建议模式、人工确认；无人值守守护默认锁定。"
echo "如完成充分验收并明确承担风险，再在私有 .env 中设置："
echo "  ALLOW_UNATTENDED_FILING=true"
echo ""
