#!/bin/bash
# ============================================================
# 新案件初始化  v2.3
# ─────────────────────────────────────────────────────────
# 用法:
#   bash new_case.sh "年度-客户简称-案由"
#
# 可选参数:
#   --type   civil | arbitration | criminal | us_civil | mixed
#   --role   角色（见下表）
#   --party-us   我方当事人名称
#   --party-op   对方当事人名称
#
# 角色说明:
#   civil:       plaintiff（原告）| defendant（被告）| third-party（第三人）
#   arbitration: claimant（申请人）| respondent（被申请人）
#   criminal:    complainant（报案人/被害人一方）| suspect（犯罪嫌疑人/被告人一方）
#   us_civil:    plaintiff | defendant | third-party
#   mixed:       分别指定民事和刑事角色
#
# 示例:
#   bash new_case.sh "2026-激光华科-产品责任纠纷" --type civil --role defendant
#   bash new_case.sh "2026-ABC-合同仲裁" --type arbitration --role claimant
#   bash new_case.sh "2026-张某-刑事辩护" --type criminal --role suspect
#   bash new_case.sh "2026-ProjectEast-产品诉讼US" --type us_civil --role defendant
#   bash new_case.sh "2026-某企业-刑民交叉" --type mixed
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$WS_DIR/.env" ]; then
  source "$WS_DIR/.env"
fi
CASES_DIR="${CASES_DIR:-$WS_DIR/cases}"

# ── 参数解析 ─────────────────────────────────────────────
CASE_NAME="${1:-}"
CASE_TYPE=""
CASE_ROLE=""
PARTY_US=""
PARTY_OP=""

shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --type)    CASE_TYPE="$2"; shift 2 ;;
    --role)    CASE_ROLE="$2"; shift 2 ;;
    --party-us) PARTY_US="$2"; shift 2 ;;
    --party-op) PARTY_OP="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [ -z "$CASE_NAME" ]; then
  echo "用法: bash new_case.sh \"[年度]-[客户简称]-[案由关键词]\" [--type TYPE] [--role ROLE]"
  echo "示例: bash new_case.sh \"2026-激光华科-产品责任纠纷\" --type civil --role defendant"
  exit 1
fi

BASE="$CASES_DIR/$CASE_NAME"
if [ -d "$BASE" ]; then
  echo "⚠ 案件文件夹已存在: $BASE"
  exit 1
fi

# ── 交互式询问（参数未指定时）────────────────────────────

ask_type() {
  echo ""
  echo "程序类型:"
  echo "  1) civil        中国民事诉讼"
  echo "  2) arbitration  仲裁（国内/国际）"
  echo "  3) criminal     中国刑事程序"
  echo "  4) us_civil     美国民事诉讼（Federal / State Court）"
  echo "  5) mixed        刑民交叉 / 多程序并行"
  printf "请选择 [1-5，默认1]: "
  read -r choice
  case "${choice:-1}" in
    1|civil)       CASE_TYPE="civil" ;;
    2|arbitration) CASE_TYPE="arbitration" ;;
    3|criminal)    CASE_TYPE="criminal" ;;
    4|us_civil)    CASE_TYPE="us_civil" ;;
    5|mixed)       CASE_TYPE="mixed" ;;
    *)             CASE_TYPE="civil" ;;
  esac
}

ask_role_civil() {
  echo ""
  echo "我方角色（民事）:"
  echo "  1) plaintiff    原告"
  echo "  2) defendant    被告（含反诉可能）"
  echo "  3) third-party  第三人"
  printf "请选择 [1-3，默认1]: "
  read -r choice
  case "${choice:-1}" in
    1|plaintiff)   CASE_ROLE="plaintiff" ;;
    2|defendant)   CASE_ROLE="defendant" ;;
    3|third-party) CASE_ROLE="third-party" ;;
    *)             CASE_ROLE="plaintiff" ;;
  esac
}

ask_role_arbitration() {
  echo ""
  echo "我方角色（仲裁）:"
  echo "  1) claimant     申请人"
  echo "  2) respondent   被申请人（含反请求可能）"
  printf "请选择 [1-2，默认1]: "
  read -r choice
  case "${choice:-1}" in
    1|claimant)   CASE_ROLE="claimant" ;;
    2|respondent) CASE_ROLE="respondent" ;;
    *)            CASE_ROLE="claimant" ;;
  esac
}

ask_role_criminal() {
  echo ""
  echo "我方角色（刑事）:"
  echo "  1) suspect      犯罪嫌疑人/被告人一方（辩护）"
  echo "  2) complainant  被害人/报案人一方（代理）"
  printf "请选择 [1-2，默认1]: "
  read -r choice
  case "${choice:-1}" in
    1|suspect)    CASE_ROLE="suspect" ;;
    2|complainant) CASE_ROLE="complainant" ;;
    *)            CASE_ROLE="suspect" ;;
  esac
}

[ -z "$CASE_TYPE" ] && ask_type

if [ -z "$CASE_ROLE" ]; then
  case "$CASE_TYPE" in
    civil|us_civil) ask_role_civil ;;
    arbitration) ask_role_arbitration ;;
    criminal)    ask_role_criminal ;;
    mixed)
      echo ""
      echo "刑民交叉案件将创建完整的民事+刑事双轨目录结构。"
      echo "请分别指定两套程序中的角色："
      ask_role_civil
      CIVIL_ROLE="$CASE_ROLE"
      ask_role_criminal
      CRIMINAL_ROLE="$CASE_ROLE"
      CASE_ROLE="${CIVIL_ROLE}+${CRIMINAL_ROLE}"
      ;;
  esac
fi

# ── 目录建立工具 ──────────────────────────────────────────
mkcd() { mkdir -p "$1"; }

echo ""
echo "📁 创建案件: $CASE_NAME"
echo "   程序类型: $CASE_TYPE  |  我方角色: $CASE_ROLE"

# ══════════════════════════════════════════════════════════
# 基础结构（所有类型共用）
# ══════════════════════════════════════════════════════════
_create_base() {
  mkcd "$BASE/00_签约与委托/委托代理协议"
  mkcd "$BASE/00_签约与委托/授权委托书"
  mkcd "$BASE/00_签约与委托/利益冲突检索"
  mkcd "$BASE/00_签约与委托/接案笔录"
  mkcd "$BASE/00_签约与委托/报价方案"
  mkcd "$BASE/00_签约与委托/主体信息"
  mkcd "$BASE/00_签约与委托/案件大事记"

  mkcd "$BASE/01_案件资料/客户提供材料/合同文件"
  mkcd "$BASE/01_案件资料/客户提供材料/往来函件"
  mkcd "$BASE/01_案件资料/客户提供材料/技术文件"
  mkcd "$BASE/01_案件资料/事实梳理"
  mkcd "$BASE/01_案件资料/现场勘验"

  mkcd "$BASE/02_法律研究/法律法规"
  mkcd "$BASE/02_法律研究/类案检索"
  mkcd "$BASE/02_法律研究/研究备忘录"

  mkcd "$BASE/03_证据/我方证据/证据目录"
  mkcd "$BASE/03_证据/我方证据/证据文件"
  mkcd "$BASE/03_证据/对方证据/证据目录"
  mkcd "$BASE/03_证据/对方证据/证据文件"
  mkcd "$BASE/03_证据/质证意见/过程稿"
  mkcd "$BASE/03_证据/质证意见/内部定稿"
  mkcd "$BASE/03_证据/质证意见/提交版"
  mkcd "$BASE/03_证据/证据交换记录"
  mkcd "$BASE/03_证据/证人陈述"

  mkcd "$BASE/09_跨境协作/翻译文件"
  mkcd "$BASE/09_跨境协作/外国律师协作"
  mkcd "$BASE/09_跨境协作/境外诉讼仲裁/US"

  mkcd "$BASE/10_客户沟通/会议纪要"
  mkcd "$BASE/10_客户沟通/阶段汇报"
  mkcd "$BASE/10_客户沟通/邮件摘要"
  mkcd "$BASE/10_客户沟通/风险提示"

  mkcd "$BASE/11_账单与工时/账单记录"
  mkcd "$BASE/11_账单与工时/工时记录"
  mkcd "$BASE/11_账单与工时/差旅费用"

  mkcd "$BASE/12_内部工作文件/案件策略/过程稿"
  mkcd "$BASE/12_内部工作文件/案件策略/内部定稿"
  mkcd "$BASE/12_内部工作文件/工作日志"
  mkcd "$BASE/12_内部工作文件/模板与格式"

  mkcd "$BASE/13_结案归档/内部归档"
  mkcd "$BASE/13_结案归档/客户交付"

  mkcd "$BASE/_待分类"
}

# ══════════════════════════════════════════════════════════
# 民事诉讼结构（含角色差异）
# ══════════════════════════════════════════════════════════
_create_civil() {
  local role="${1:-plaintiff}"

  mkcd "$BASE/06_专家工作/司法鉴定"
  mkcd "$BASE/06_专家工作/专家辅助人"
  mkcd "$BASE/06_专家工作/技术分析"

  mkcd "$BASE/07_和解与调解/和解方案/过程稿"
  mkcd "$BASE/07_和解与调解/和解方案/内部定稿"
  mkcd "$BASE/07_和解与调解/和解方案/提交版"
  mkcd "$BASE/07_和解与调解/调解笔录"

  mkcd "$BASE/05_庭审/庭前准备/庭审提纲/过程稿"
  mkcd "$BASE/05_庭审/庭前准备/可视化材料"
  mkcd "$BASE/05_庭审/庭审记录/律师自记笔录"

  # 共有文书
  mkcd "$BASE/04_诉讼文书/代理词/过程稿"
  mkcd "$BASE/04_诉讼文书/代理词/内部定稿"
  mkcd "$BASE/04_诉讼文书/代理词/提交版"
  mkcd "$BASE/04_诉讼文书/保全申请/过程稿"
  mkcd "$BASE/04_诉讼文书/保全申请/提交版"
  mkcd "$BASE/04_诉讼文书/程序申请/过程稿"
  mkcd "$BASE/04_诉讼文书/程序申请/提交版"
  mkcd "$BASE/04_诉讼文书/法院来文"

  # 上诉 / 再审（所有角色均可能涉及）
  mkcd "$BASE/04_诉讼文书/上诉状/过程稿"
  mkcd "$BASE/04_诉讼文书/上诉状/内部定稿"
  mkcd "$BASE/04_诉讼文书/上诉状/提交版"
  mkcd "$BASE/04_诉讼文书/再审申请书/过程稿"
  mkcd "$BASE/04_诉讼文书/再审申请书/提交版"

  mkcd "$BASE/08_判决与执行/判决书"
  mkcd "$BASE/08_判决与执行/上诉文件"
  mkcd "$BASE/08_判决与执行/执行异议/过程稿"
  mkcd "$BASE/08_判决与执行/执行异议/提交版"

  case "$role" in
    plaintiff)
      mkcd "$BASE/04_诉讼文书/起诉状/过程稿"
      mkcd "$BASE/04_诉讼文书/起诉状/内部定稿"
      mkcd "$BASE/04_诉讼文书/起诉状/提交版"
      mkcd "$BASE/08_判决与执行/执行申请书/过程稿"
      mkcd "$BASE/08_判决与执行/执行申请书/提交版"
      ;;
    defendant)
      mkcd "$BASE/04_诉讼文书/答辩状/过程稿"
      mkcd "$BASE/04_诉讼文书/答辩状/内部定稿"
      mkcd "$BASE/04_诉讼文书/答辩状/提交版"
      # 反诉（被告方可能提出）
      mkcd "$BASE/04_诉讼文书/反诉状/过程稿"
      mkcd "$BASE/04_诉讼文书/反诉状/内部定稿"
      mkcd "$BASE/04_诉讼文书/反诉状/提交版"
      ;;
    third-party)
      mkcd "$BASE/04_诉讼文书/第三人意见书/过程稿"
      mkcd "$BASE/04_诉讼文书/第三人意见书/内部定稿"
      mkcd "$BASE/04_诉讼文书/第三人意见书/提交版"
      ;;
  esac
}

# ══════════════════════════════════════════════════════════
# 仲裁结构（含角色差异）
# ══════════════════════════════════════════════════════════
_create_arbitration() {
  local role="${1:-claimant}"

  mkcd "$BASE/06_专家工作/司法鉴定"
  mkcd "$BASE/06_专家工作/专家辅助人"
  mkcd "$BASE/06_专家工作/技术分析"

  mkcd "$BASE/07_和解与调解/和解方案/过程稿"
  mkcd "$BASE/07_和解与调解/和解方案/提交版"
  mkcd "$BASE/07_和解与调解/调解笔录"

  mkcd "$BASE/05_庭审/庭前准备/庭审提纲/过程稿"
  mkcd "$BASE/05_庭审/庭前准备/可视化材料"
  mkcd "$BASE/05_庭审/庭审记录/律师自记笔录"

  # 共有仲裁文书
  mkcd "$BASE/04_仲裁文书/代理词与陈述/过程稿"
  mkcd "$BASE/04_仲裁文书/代理词与陈述/内部定稿"
  mkcd "$BASE/04_仲裁文书/代理词与陈述/提交版"
  mkcd "$BASE/04_仲裁文书/程序申请/过程稿"
  mkcd "$BASE/04_仲裁文书/程序申请/提交版"
  mkcd "$BASE/04_仲裁文书/临时措施申请/过程稿"
  mkcd "$BASE/04_仲裁文书/临时措施申请/提交版"
  mkcd "$BASE/04_仲裁文书/仲裁机构函件"

  mkcd "$BASE/08_裁决与执行/仲裁裁决书"
  mkcd "$BASE/08_裁决与执行/撤裁申请/过程稿"
  mkcd "$BASE/08_裁决与执行/撤裁申请/提交版"
  mkcd "$BASE/08_裁决与执行/承认执行申请/过程稿"
  mkcd "$BASE/08_裁决与执行/承认执行申请/提交版"

  case "$role" in
    claimant)
      mkcd "$BASE/04_仲裁文书/仲裁申请书/过程稿"
      mkcd "$BASE/04_仲裁文书/仲裁申请书/内部定稿"
      mkcd "$BASE/04_仲裁文书/仲裁申请书/提交版"
      ;;
    respondent)
      mkcd "$BASE/04_仲裁文书/仲裁答辩书/过程稿"
      mkcd "$BASE/04_仲裁文书/仲裁答辩书/内部定稿"
      mkcd "$BASE/04_仲裁文书/仲裁答辩书/提交版"
      # 反请求（被申请人可能提出）
      mkcd "$BASE/04_仲裁文书/仲裁反请求/过程稿"
      mkcd "$BASE/04_仲裁文书/仲裁反请求/内部定稿"
      mkcd "$BASE/04_仲裁文书/仲裁反请求/提交版"
      ;;
  esac
}

# ══════════════════════════════════════════════════════════
# 刑事程序结构（角色差异显著）
# ══════════════════════════════════════════════════════════
_create_criminal() {
  local role="${1:-suspect}"

  mkcd "$BASE/06_专家工作/司法鉴定"
  mkcd "$BASE/06_专家工作/技术分析"

  # 侦查阶段（共有）
  mkcd "$BASE/CR_侦查阶段/立案文件"
  mkcd "$BASE/CR_侦查阶段/强制措施"
  mkcd "$BASE/CR_侦查阶段/侦查笔录"
  mkcd "$BASE/CR_侦查阶段/搜查扣押冻结"
  mkcd "$BASE/CR_侦查阶段/退补侦查意见"

  # 审查起诉（共有）
  mkcd "$BASE/CR_审查起诉阶段/起诉书"
  mkcd "$BASE/CR_审查起诉阶段/不起诉材料"

  # 审判（共有）
  mkcd "$BASE/CR_审判阶段/庭审准备/庭审提纲/过程稿"
  mkcd "$BASE/CR_审判阶段/庭审记录"
  mkcd "$BASE/CR_审判阶段/附带民事诉讼/起诉状"
  mkcd "$BASE/CR_审判阶段/附带民事诉讼/答辩状"

  # 判决执行（共有）
  mkcd "$BASE/CR_判决与执行/刑事判决书"

  # 上诉申诉（共有）
  mkcd "$BASE/CR_上诉申诉/上诉状/过程稿"
  mkcd "$BASE/CR_上诉申诉/上诉状/提交版"
  mkcd "$BASE/CR_上诉申诉/申诉书/过程稿"
  mkcd "$BASE/CR_上诉申诉/申诉书/提交版"

  case "$role" in
    suspect)
      # 犯罪嫌疑人/被告人一方 — 辩护律师
      mkcd "$BASE/CR_侦查阶段/取保候审/申请书/过程稿"
      mkcd "$BASE/CR_侦查阶段/取保候审/申请书/提交版"
      mkcd "$BASE/CR_侦查阶段/取保候审/决定书"
      mkcd "$BASE/CR_侦查阶段/取保候审/担保文件"
      mkcd "$BASE/CR_侦查阶段/羁押必要性审查申请"
      mkcd "$BASE/CR_侦查阶段/阅卷材料"
      mkcd "$BASE/CR_审查起诉阶段/辩护申请/过程稿"
      mkcd "$BASE/CR_审查起诉阶段/辩护申请/提交版"
      mkcd "$BASE/CR_审判阶段/辩护词/过程稿"
      mkcd "$BASE/CR_审判阶段/辩护词/内部定稿"
      mkcd "$BASE/CR_审判阶段/辩护词/提交版"
      mkcd "$BASE/CR_审判阶段/量刑意见/过程稿"
      mkcd "$BASE/CR_审判阶段/量刑意见/提交版"
      mkcd "$BASE/CR_审判阶段/非法证据排除申请/过程稿"
      mkcd "$BASE/CR_审判阶段/非法证据排除申请/提交版"
      mkcd "$BASE/CR_执行阶段/减刑假释申请/过程稿"
      mkcd "$BASE/CR_执行阶段/减刑假释申请/提交版"
      ;;
    complainant)
      # 被害人/报案人一方 — 代理律师
      mkcd "$BASE/CR_侦查阶段/报案控告材料"
      mkcd "$BASE/CR_审查起诉阶段/追诉意见书/过程稿"
      mkcd "$BASE/CR_审查起诉阶段/不起诉异议/过程稿"
      mkcd "$BASE/CR_审查起诉阶段/不起诉异议/提交版"
      mkcd "$BASE/CR_审判阶段/附带民事诉讼/代理词/过程稿"
      mkcd "$BASE/CR_审判阶段/附带民事诉讼/代理词/提交版"
      ;;
  esac
}

# ══════════════════════════════════════════════════════════
# 美国民事诉讼结构（Federal / State Court）
# ══════════════════════════════════════════════════════════
_create_us_civil() {
  local role="${1:-plaintiff}"

  # Discovery
  mkcd "$BASE/US_Discovery/Initial_Disclosures"
  mkcd "$BASE/US_Discovery/Interrogatories/Ours"
  mkcd "$BASE/US_Discovery/Interrogatories/Theirs"
  mkcd "$BASE/US_Discovery/Requests_for_Production/Ours"
  mkcd "$BASE/US_Discovery/Requests_for_Production/Theirs"
  mkcd "$BASE/US_Discovery/Requests_for_Admission/Ours"
  mkcd "$BASE/US_Discovery/Requests_for_Admission/Theirs"
  mkcd "$BASE/US_Discovery/Depositions/Notices"
  mkcd "$BASE/US_Discovery/Depositions/Transcripts"
  mkcd "$BASE/US_Discovery/Subpoenas"
  mkcd "$BASE/US_Discovery/Document_Production"

  # Pleadings（共有）
  mkcd "$BASE/US_Pleadings/Motions/Motion_to_Dismiss/Draft"
  mkcd "$BASE/US_Pleadings/Motions/Motion_for_Summary_Judgment/Draft"
  mkcd "$BASE/US_Pleadings/Motions/TRO_Preliminary_Injunction/Draft"
  mkcd "$BASE/US_Pleadings/Motions/Other_Motions/Draft"
  mkcd "$BASE/US_Pleadings/Opposition_Briefs/Draft"
  mkcd "$BASE/US_Pleadings/Reply_Briefs/Draft"
  mkcd "$BASE/US_Pleadings/Court_Orders"
  mkcd "$BASE/US_Pleadings/Stipulations"

  # Expert
  mkcd "$BASE/US_Expert/Expert_Reports"
  mkcd "$BASE/US_Expert/Rebuttal_Reports"
  mkcd "$BASE/US_Expert/CVs_and_Quals"

  # Trial
  mkcd "$BASE/US_Trial/Trial_Brief/Draft"
  mkcd "$BASE/US_Trial/Witness_List"
  mkcd "$BASE/US_Trial/Exhibit_List"
  mkcd "$BASE/US_Trial/Jury_Instructions"
  mkcd "$BASE/US_Trial/Pretrial_Order"
  mkcd "$BASE/US_Trial/Transcripts"

  # Judgment & Appeal
  mkcd "$BASE/US_Judgment_Appeal/Verdict_Judgment"
  mkcd "$BASE/US_Judgment_Appeal/Notice_of_Appeal"
  mkcd "$BASE/US_Judgment_Appeal/Appellate_Briefs/Opening/Draft"
  mkcd "$BASE/US_Judgment_Appeal/Appellate_Briefs/Opposition/Draft"
  mkcd "$BASE/US_Judgment_Appeal/Appellate_Briefs/Reply/Draft"

  # Settlement
  mkcd "$BASE/US_Settlement/Settlement_Agreement"
  mkcd "$BASE/US_Settlement/Mediation"

  case "$role" in
    plaintiff)
      mkcd "$BASE/US_Pleadings/Complaint/Draft"
      mkcd "$BASE/US_Judgment_Appeal/Enforcement"
      ;;
    defendant)
      mkcd "$BASE/US_Pleadings/Answer_Counterclaim/Draft"
      ;;
    third-party)
      mkcd "$BASE/US_Pleadings/Third_Party_Response/Draft"
      ;;
  esac
}

# ══════════════════════════════════════════════════════════
# 执行对应模板
# ══════════════════════════════════════════════════════════
_create_base

case "$CASE_TYPE" in
  civil)
    _create_civil "$CASE_ROLE"
    ;;
  arbitration)
    _create_arbitration "$CASE_ROLE"
    ;;
  criminal)
    _create_criminal "$CASE_ROLE"
    ;;
  us_civil)
    _create_us_civil "$CASE_ROLE"
    ;;
  mixed)
    # 民事和刑事双轨并行
    CIVIL_ROLE=$(echo "$CASE_ROLE" | cut -d'+' -f1)
    CRIMINAL_ROLE=$(echo "$CASE_ROLE" | cut -d'+' -f2)
    _create_civil "${CIVIL_ROLE:-plaintiff}"
    _create_criminal "${CRIMINAL_ROLE:-suspect}"
    # 刑民交叉专属协调文件夹
    mkcd "$BASE/XX_刑民交叉协调/程序衔接分析"
    mkcd "$BASE/XX_刑民交叉协调/先刑后民策略"
    mkcd "$BASE/XX_刑民交叉协调/证据共享目录"
    ;;
  *)
    _create_civil "plaintiff"
    ;;
esac



# ══════════════════════════════════════════════════════════
# 案件元数据文件
# ══════════════════════════════════════════════════════════
TODAY=$(date +%Y-%m-%d)

# 角色中文说明
_role_zh() {
  case "$1" in
    plaintiff)   echo "原告方" ;;
    defendant)   echo "被告方（含反诉可能）" ;;
    third-party) echo "第三人" ;;
    claimant)    echo "仲裁申请人" ;;
    respondent)  echo "仲裁被申请人（含反请求可能）" ;;
    suspect)     echo "犯罪嫌疑人/被告人方（辩护）" ;;
    complainant) echo "被害人/报案人方（代理）" ;;
    *)           echo "$1" ;;
  esac
}

# 程序中文说明
_type_zh() {
  case "$1" in
    civil)       echo "民事诉讼" ;;
    arbitration) echo "仲裁（国内/国际）" ;;
    criminal)    echo "刑事程序" ;;
    us_civil)    echo "美国民事诉讼（Federal / State Court）" ;;
    mixed)       echo "刑民交叉（多程序并行）" ;;
    *)           echo "$1" ;;
  esac
}

CIVIL_ROLE_ZH=""
CRIMINAL_ROLE_ZH=""
if [ "$CASE_TYPE" = "mixed" ]; then
  CR=$(echo "$CASE_ROLE" | cut -d'+' -f1)
  CRIMI=$(echo "$CASE_ROLE" | cut -d'+' -f2)
  CIVIL_ROLE_ZH="民事: $(_role_zh $CR)"
  CRIMINAL_ROLE_ZH="刑事: $(_role_zh $CRIMI)"
fi

cat > "$BASE/case_meta.yaml" <<YAML
# 案件元数据 — 自动生成，请补全后保存
# =====================================================
name: "$CASE_NAME"
created: "$TODAY"

procedure:
  type: "$CASE_TYPE"           # civil | arbitration | criminal | mixed
  type_zh: "$(_type_zh $CASE_TYPE)"

role:
  code: "$CASE_ROLE"
  description: "$(_role_zh $CASE_ROLE)"

parties:
  our_client: "${PARTY_US:-请填写我方当事人名称}"
  opposing:   "${PARTY_OP:-请填写对方当事人名称}"

# 补充说明（如有特殊情况请在此记录）
notes: ""

# 重要日期（收到委托、立案、重要节点等）
key_dates:
  retainer:   ""   # 收到委托日期
  filing:     ""   # 立案/受理日期
  next_deadline: "" # 下一个重要期限

# 案件简称（用于文件命名，建议4字以内）
# 示例：激光华科 / ProjectEast / 张某刑案
file_abbr: ""

# 关联案件（如刑民交叉时填写并案信息）
related_cases: []
YAML

# .gitkeep 占位（保留空目录）
find "$BASE" -type d -empty -exec touch {}/.gitkeep \; 2>/dev/null || true

# ── 输出结果 ─────────────────────────────────────────────
echo ""
echo "✅ 案件文件夹已创建: $BASE"
echo "   程序类型: $(_type_zh $CASE_TYPE)"
echo "   我方角色: $(_role_zh $CASE_ROLE)"
echo ""
echo "下一步:"
echo "  1. 编辑 $BASE/case_meta.yaml 补全当事人信息"
echo "  2. 在 auto_filer/config.yaml 的 cases 列表中添加本案关键词"
echo "     （key 建议与 case_meta.yaml 中的 file_abbr 保持一致）"
echo "  3. 开始往 inbox 丢文件 → 自动归档"

if command -v tree &>/dev/null; then
  echo ""
  tree "$BASE" --dirsfirst -L 2
fi
