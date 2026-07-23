#!/bin/bash
# ============================================================
# 涉外诉讼案件工作区 一键部署脚本
# 用法: bash setup.sh
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
OK="${GREEN}✅${NC}"; WARN="${YELLOW}⚠${NC}"; ERR="${RED}❌${NC}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WS_DIR="$SCRIPT_DIR"

echo "================================================"
echo "  涉外诉讼案件工作区 - 一键部署"
echo "================================================"
echo ""

# ── 1. 检测系统 ──
echo "🔍 系统检测..."
OS="unknown"
case "$(uname -s)" in
  Darwin) OS="macOS" ;;
  Linux)  OS="Linux" ;;
esac
echo "   操作系统: $OS"
echo ""

# ── 2. 创建 .env ──
if [ ! -f "$WS_DIR/.env" ]; then
  echo "📝 创建配置文件 .env..."
  cp "$WS_DIR/.env.example" "$WS_DIR/.env"

  # 替换为绝对路径
  ABS_WS_DIR="$(cd "$WS_DIR" && pwd)"
  if [ "$OS" = "macOS" ]; then
    sed -i '' "s|__WORKSPACE_ROOT__|$ABS_WS_DIR|g" "$WS_DIR/.env"
  else
    sed -i "s|__WORKSPACE_ROOT__|$ABS_WS_DIR|g" "$WS_DIR/.env"
  fi

  echo "$OK .env 已创建，默认工作区: ${GREEN}$ABS_WS_DIR${NC}"
  echo "   如需修改路径或名称，编辑 .env 文件"
else
  echo "$OK .env 已存在，跳过"
fi
echo ""

# ── 3. 加载配置 ──
source "$WS_DIR/.env"

# ── 4. 创建目录结构 ──
echo "📁 创建目录结构..."
mkdir -p "$INBOX_DIR"
mkdir -p "$CASES_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$CASES_DIR/_未分类/_待分类"
echo "$OK 收件箱: $INBOX_DIR"
echo "$OK 案件目录: $CASES_DIR"
echo "$OK 日志目录: $LOG_DIR"

# 收件箱 README
cat > "$INBOX_DIR/README.txt" << 'INBOX_README'
═══════════════════════════════════════
  材料归档收件箱
═══════════════════════════════════════

把需要归档的案卷材料拖到这个文件夹，
系统会自动：
  1. 识别所属案件
  2. 归类到对应子目录
  3. 重命名为标准格式

支持文件类型:
  • Word 文档 (.docx) — 读文字内容
  • PDF (文字型 + 扫描版) — pdftotext + OCR
  • 图片 (.jpg .png .tiff) — OCR 识别
  • Excel (.xlsx) — 读单元格文字
  • 纯文本 (.txt .md) — 读文件内容

提示:
  • 文件命名尽量含中文关键词更容易匹配
  • 照片/扫描件会自动 OCR，无需手动命名
  • 文件移动后收件箱会变空，不用手动清理
  • 归到 _未分类 或 _待分类 的文件需手动归位

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INBOX_README

echo "$OK 收件箱说明文件已创建"
echo ""

# ── 5. Python 依赖 ──
echo "🐍 检查 Python 依赖..."
PYTHON=""
for py in python3.11 python3.10 python3; do
  if command -v "$py" &>/dev/null; then
    PYTHON="$py"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "$ERR Python3 未安装，请先安装: brew install python"
  exit 1
fi
echo "   Python: $PYTHON ($($PYTHON --version))"

_missing_pip() {
  ! $PYTHON -c "import $1" 2>/dev/null
}

NEED_INSTALL=""
for mod in yaml watchdog docx openpyxl pytesseract Pillow; do
  if _missing_pip "$mod"; then
    NEED_INSTALL="$NEED_INSTALL $mod"
  fi
done

if [ -n "$NEED_INSTALL" ]; then
  echo "   安装缺失的 Python 包:$NEED_INSTALL"
  $PYTHON -m pip install --break-system-packages -q $NEED_INSTALL 2>&1 | tail -1
fi
echo "$OK Python 依赖就绪"
echo ""

# ── 6. 系统依赖 (brew/macOS) ──
echo "🔧 系统依赖检测..."
MISSING_SYS=""

check_cmd() {
  if ! command -v "$1" &>/dev/null; then
    MISSING_SYS="$MISSING_SYS $2"
    echo "   ${WARN} 缺少: $2 ($1)"
  fi
}

check_cmd tesseract "tesseract"
check_cmd pdftotext "poppler"

if [ -n "$MISSING_SYS" ]; then
  echo ""
  if [ "$OS" = "macOS" ]; then
    if command -v brew &>/dev/null; then
      echo "   运行: brew install$MISSING_SYS"
      read -p "   是否自动安装? (y/n) " -n 1 -r
      echo
      if [[ $REPLY =~ ^[Yy]$ ]]; then
        brew install$MISSING_SYS
        echo "$OK 系统依赖安装完成"
      fi
    else
      echo "   ${WARN} Homebrew 未安装，请手动安装:"
      echo "   brew install$MISSING_SYS"
    fi
  elif [ "$OS" = "Linux" ]; then
    echo "   运行: sudo apt-get install -y$MISSING_SYS"
  fi
else
  echo "$OK 系统依赖就绪 (tesseract + poppler)"
fi

# tesseract 中文包
if command -v tesseract &>/dev/null; then
  if ! tesseract --list-langs 2>/dev/null | grep -q chi_sim; then
    echo "   ${WARN} tesseract 缺少中文语言包"
    if [ "$OS" = "macOS" ] && command -v brew &>/dev/null; then
      brew install tesseract-lang 2>/dev/null || true
    fi
  else
    echo "$OK tesseract 中文包就绪 (chi_sim+eng)"
  fi
fi
echo ""

# ── 7. 可执行权限 ──
chmod +x "$WS_DIR/bin/new_case.sh" 2>/dev/null || true
chmod +x "$WS_DIR/bin/filer.sh" 2>/dev/null || true
echo "$OK 脚本权限设置完成"
echo ""

# ── 8. launchd (仅 macOS) ──
if [ "$OS" = "macOS" ]; then
  echo "🍎 macOS launchd 配置..."
  PLIST_SRC="$WS_DIR/config/com.zl.autofiler.plist"
  PLIST_DST="$HOME/Library/LaunchAgents/com.zl.autofiler.plist"

  # 替换路径占位符
  PYTHON_PATH=$(which "$PYTHON")
  sed \
    -e "s|__PYTHON_PATH__|$PYTHON_PATH|g" \
    -e "s|__AF_DIR__|$WS_DIR/auto_filer|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$PLIST_SRC" > "$PLIST_DST"

  echo "$OK launchd plist 已安装: $PLIST_DST"
  echo ""
  echo "   开机自启: launchctl load $PLIST_DST"
  echo "   手动启动: launchctl start com.zl.autofiler"
  echo "   停止服务: launchctl stop com.zl.autofiler"
  echo "   卸载服务: launchctl unload $PLIST_DST"
fi
echo ""

# ── 9. 快速启动脚本 ──
if [ ! -f "$WS_DIR/quick_start.sh" ]; then
  cat > "$WS_DIR/quick_start.sh" << 'QUICKSTART'
#!/bin/bash
# 快速启动 - 双击运行或拖到终端
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/bin/filer.sh" start
echo "收件箱路径:"
grep INBOX_DIR "$SCRIPT_DIR/.env" 2>/dev/null || echo "$SCRIPT_DIR/inbox"
QUICKSTART
  chmod +x "$WS_DIR/quick_start.sh"
fi
echo "$OK 快速启动脚本: quick_start.sh"
echo ""

# ── 10. 完成 ──
echo "================================================"
echo "  ${GREEN}部署完成！${NC}"
echo "================================================"
echo ""
echo "📋 常用命令:"
echo "   bash bin/new_case.sh \"2026-客户-案由\"   新建案件"
echo "   bash bin/filer.sh start                  启动归档守护"
echo "   bash bin/filer.sh once                   单次归档 inbox 文件"
echo "   bash bin/filer.sh dry-run                试运行(看归到哪)"
echo "   bash bin/filer.sh status                 查看状态"
echo "   bash bin/filer.sh log                    查看日志"
echo ""
echo "📂 收件箱路径: $INBOX_DIR"
echo "   把文件丢进去就行。支持 docx/PDF/图片(OCR)/Excel"
echo ""
echo "⚙  配置文件:"
echo "   .env                   工作区配置"
echo "   auto_filer/config.yaml 案件+规则(添加新案件改这里)"
echo ""
echo "💡 首次使用建议:"
echo "   1. 编辑 auto_filer/config.yaml，补全你的案件列表"
echo "   2. bash bin/new_case.sh \"案件名\" 创建案件文件夹"
echo "   3. 丢几个文件到 inbox，跑 bash bin/filer.sh dry-run 试试"
echo "   4. 确认无误后，bash bin/filer.sh start 启用守护"
echo ""
