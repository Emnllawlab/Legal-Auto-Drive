#!/usr/bin/env python3
"""
案件冷启动访谈  v1.0
─────────────────────────────────────────────────
用法:  python3 bin/intake.py
       bash  bin/filer.sh intake

流程:
  1. 逐步收集案件信息（中英混排）
  2. 粘贴文本自动提取关键词
  3. 生成预览，确认后一键写入 config.yaml + 建立目录 + 生成 case_meta.yaml
"""

import os, re, sys, textwrap
from pathlib import Path
from datetime import datetime

# ── ANSI 颜色 ──────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"
BLUE   = "\033[34m"
GRAY   = "\033[90m"

def c(text, *codes): return "".join(codes) + str(text) + RESET
def hr(char="─", width=56): return c(char * width, DIM)
def step_header(n, total, title):
    print(f"\n{c(f'[{n}/{total}]', CYAN, BOLD)} {c(title, BOLD)}")
    print(hr())

# ── 路径与配置加载 ──────────────────────────────────────────
def _resolve_env_vars(val, env_map):
    """展开 $VAR 和 ${VAR} 引用"""
    def repl(m):
        k = m.group(1) or m.group(2)
        return env_map.get(k, os.environ.get(k, m.group(0)))
    return re.sub(r'\$\{(\w+)\}|\$(\w+)', repl, val)

def load_workspace():
    ws = Path(__file__).resolve().parent.parent
    env_path = ws / ".env"
    cfg_path = ws / "auto_filer" / "config.yaml"

    raw = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip().strip('"\'')
    env = {k: _resolve_env_vars(v, raw) for k, v in raw.items()}

    cases_dir = env.get("CASES_DIR", "")
    if not cases_dir or not Path(cases_dir).is_absolute():
        cases_dir = str(ws / (cases_dir.lstrip("./") or "cases"))

    return ws, cfg_path, env_path, Path(cases_dir)


# ── 关键词提取 ──────────────────────────────────────────────
_ZH_STOP = {
    '的','了','是','在','有','和','与','及','或','这','那','也','都','但',
    '因','为','对','从','由','到','被','将','把','让','使','以','其','各',
    '此','该','并','等','如','中','不','已','我','你','他','她','它','们',
    '就','所','当','后','前','内','外','上','下','一','二','三','四','五',
    '六','七','八','九','十','可以','需要','应该','包括','通过','相关',
    '情况','问题','方面','进行','采用','提供','具有','作为','根据','按照',
    '依据','关于','针对','有关','此次','本次','双方','三方','申请','提交',
}
_EN_STOP = {
    'the','a','an','and','or','but','in','on','at','to','for','of','with',
    'by','from','is','are','was','were','be','been','have','has','had',
    'do','does','did','will','would','could','should','may','might',
    'this','that','these','those','it','its','we','our','you','your',
    'they','their','he','his','she','her','as','if','so','no','not',
}

def extract_keywords(text: str) -> list:
    tokens = re.split(r'[\s,;，；、。！？!?\n\r\t（）()【】\[\]「」《》\-—]+', text)
    seen, result = set(), []
    for tok in tokens:
        tok = tok.strip('\'"\'\'""„"')
        if not tok or tok in seen:
            continue
        seen.add(tok)
        zh = len(re.findall(r'[一-鿿]', tok))
        if zh > 0:
            if len(tok) >= 2 and tok not in _ZH_STOP:
                result.append(tok)
        else:
            if len(tok) >= 2 and tok.lower() not in _EN_STOP and not re.match(r'^\d+$', tok):
                result.append(tok)
    return result

# ── 用户输入辅助 ────────────────────────────────────────────
def ask(prompt, default=None, required=True):
    hint = f" {c(f'[默认: {default}]', GRAY)}" if default else ""
    while True:
        val = input(f"  {prompt}{hint}: ").strip()
        if not val and default is not None:
            return default
        if val or not required:
            return val or ""

def ask_choice(options, default=1):
    """显示带编号的选项，返回选中的 key"""
    for i, (key, label) in enumerate(options, 1):
        marker = c("▶", GREEN) if i == default else " "
        print(f"  {marker} {c(str(i), YELLOW)}  {label}")
    while True:
        raw = input(f"  请选择 [1-{len(options)}，默认{default}]: ").strip()
        if not raw:
            return options[default - 1][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(c("  ⚠ 请输入有效编号", RED))

def ask_multiline(prompt, end_token="END"):
    print(f"  {prompt}")
    print(c(f"  （支持直接粘贴，完成后在新行输入 {end_token} 回车结束）", GRAY))
    lines = []
    while True:
        try:
            line = input("  > ")
        except EOFError:
            break
        if line.strip().upper() == end_token:
            break
        lines.append(line)
    return "\n".join(lines)

def ask_parties(label):
    print(f"  {label}{c('（每行一个，空行结束）', GRAY)}:")
    items = []
    while True:
        val = input("  > ").strip()
        if not val:
            break
        items.append(val)
    return items

# ── 建立目录结构（Python 版） ────────────────────────────────
_COLLECT = None       # 非 None 时 _m 只收集 relpath 不真正建（供 create_folders 统一编号）


def _m(base, rel):
    if _COLLECT is not None:
        _COLLECT.append(str(rel))
        return
    Path(base, rel).mkdir(parents=True, exist_ok=True)


# ── 层级序号：L1 保留原样(已有 NN_/系统目录) | L2 → "A - " | L3 → "1. " ──
_LVL_PREFIX = re.compile(r'^(?:\d{2}_|[A-Z] - |\d+\. )')


def _core_name(name: str) -> str:
    """去掉层级序号前缀，得到"核心名"（用于归档时前缀无关地匹配真实目录）。"""
    return _LVL_PREFIX.sub("", name)


def _number_relpaths(dirs):
    """把有序的 core relpath 列表转成带层级序号的 relpath。
    同一父目录下的子目录按"首次出现顺序"依次编号；跨多条 relpath 保持一致。"""
    assigned, counter, out = {}, {}, []
    for rel in dirs:
        parent, new_segs = "", []
        for depth, seg in enumerate(rel.split("/")):
            core = _core_name(seg)
            key = (parent, depth, core)
            num_seg = assigned.get(key)
            if num_seg is None:
                if depth == 1 and not core.startswith("_"):
                    idx = counter.get((parent, 1), 0)
                    num_seg = f"{chr(65 + idx)} - {core}" if idx < 26 else f"{chr(65 + idx // 26 - 1)}{chr(65 + idx % 26)} - {core}"
                    counter[(parent, 1)] = idx + 1
                elif depth == 2 and not core.startswith("_"):
                    idx = counter.get((parent, 2), 0)
                    num_seg = f"{idx + 1}. {core}"
                    counter[(parent, 2)] = idx + 1
                else:                              # L1 / L4+ / 系统目录：原样
                    num_seg = seg
                assigned[key] = num_seg
            new_segs.append(num_seg)
            parent = "/".join(new_segs)
        out.append("/".join(new_segs))
    return out


# ══════════════════════════════════════════════════════════════
# 分层脚手架（民事/仲裁按审级/阶段分）——2026-07 新增
# 案件级目录（跨审级共用）+ 各阶段一套办案目录。
# ══════════════════════════════════════════════════════════════
def _create_base_staged(base):
    """案件级（跨审级共用）目录：签约委托、案件资料、法律研究、跨境、沟通、账单、内部、结案。
    注意：授权委托书与 03_证据 已下沉到各阶段，这里不再建。"""
    dirs = [
        "00_签约与委托/委托代理协议",
        "00_签约与委托/利益冲突检索",
        "00_签约与委托/接案笔录",
        "00_签约与委托/报价方案",
        "00_签约与委托/主体信息",
        "00_签约与委托/案件大事记",
        "01_案件资料/客户提供材料/合同文件",
        "01_案件资料/客户提供材料/往来函件",
        "01_案件资料/客户提供材料/技术文件",
        "01_案件资料/事实梳理",
        "01_案件资料/现场勘验",
        "02_法律研究/法律法规",
        "02_法律研究/类案检索",
        "02_法律研究/研究备忘录",
        "09_跨境协作/翻译文件",
        "09_跨境协作/外国律师协作",
        "09_跨境协作/境外诉讼仲裁/US",
        "10_客户沟通/会议纪要",
        "10_客户沟通/阶段汇报",
        "10_客户沟通/邮件摘要",
        "10_客户沟通/往来邮件/我方拟发",
        "10_客户沟通/往来邮件/收发存档",
        "10_客户沟通/风险提示",
        "11_账单与工时/账单记录",
        "11_账单与工时/工时记录",
        "11_账单与工时/差旅费用",
        "12_内部工作文件/案件策略",
        "12_内部工作文件/工作日志",
        "12_内部工作文件/模板与格式",
        "13_结案归档/内部归档",
        "13_结案归档/客户交付",
        "_待分类",
    ]
    for d in dirs:
        _m(base, d)


def _civil_stage_dirs(stage, susong_docs):
    """一个诉讼审级（一审/二审/再审）下的通用办案目录 + 该审级的诉讼文书条目。"""
    d = [
        f"{stage}/授权委托与所函/授权委托书",
        f"{stage}/授权委托与所函/律所函",
        f"{stage}/法院来文",
        f"{stage}/证据/我方证据",
        f"{stage}/证据/对方证据",
        f"{stage}/证据/质证意见",
        f"{stage}/证据/证据交换记录",
        f"{stage}/证据/证人陈述",
        f"{stage}/庭审/庭前准备",
        f"{stage}/庭审/庭审记录",
        f"{stage}/专家工作/司法鉴定",
        f"{stage}/专家工作/专家辅助人",
        f"{stage}/专家工作/技术分析",
        f"{stage}/和解调解/和解方案",
        f"{stage}/和解调解/调解笔录",
        f"{stage}/判决裁定",
    ]
    for doc in susong_docs:
        d.append(f"{stage}/诉讼文书/{doc}")
    return d


def _create_civil_staged(base, role):
    """民事诉讼分层：案件级 + 一审/二审/再审/执行 各一套。"""
    _create_base_staged(base)
    common = ["代理词", "程序申请", "保全申请"]
    first_role = {
        "plaintiff": ["起诉状"],
        "defendant": ["答辩状", "反诉状"],
        "third-party": ["第三人意见书"],
    }.get(role, ["起诉状"])
    # 阶段带数字前缀，保证在 Finder 里排在 02_法律研究 之后、行政类目录之前（审级优先、醒目）
    dirs = []
    dirs += _civil_stage_dirs("03_一审", first_role + common)
    dirs += _civil_stage_dirs("04_二审", ["上诉状", "上诉答辩状", "代理词", "程序申请"])
    dirs += _civil_stage_dirs("05_再审", ["再审申请书", "代理词"])
    # 06_执行 阶段（自成一套）
    dirs += [
        "06_执行/授权委托与所函/授权委托书",
        "06_执行/授权委托与所函/律所函",
        "06_执行/执行文书/执行申请书",
        "06_执行/执行文书/执行异议",
        "06_执行/执行文书/参与分配",
        "06_执行/法院来文",
        "06_执行/证据",
        "06_执行/执行裁定",
    ]
    for d in dirs:
        _m(base, d)


def _create_arbitration_staged(base, role):
    """仲裁分层：案件级 + 仲裁 / 司法审查（撤裁·不予执行·承认执行）/ 执行。"""
    _create_base_staged(base)
    arb_role = {
        "claimant": ["仲裁申请书"],
        "respondent": ["仲裁答辩书", "仲裁反请求"],
    }.get(role, ["仲裁申请书"])
    # 阶段带数字前缀，保证审级/阶段排在 02_法律研究 之后、行政类目录之前
    dirs = [
        "03_仲裁/授权委托与所函/授权委托书",
        "03_仲裁/授权委托与所函/律所函",
        "03_仲裁/仲裁机构函件",
        "03_仲裁/证据/我方证据",
        "03_仲裁/证据/对方证据",
        "03_仲裁/证据/质证意见",
        "03_仲裁/证据/证据交换记录",
        "03_仲裁/证据/证人陈述",
        "03_仲裁/庭审/庭前准备",
        "03_仲裁/庭审/庭审记录",
        "03_仲裁/专家工作/司法鉴定",
        "03_仲裁/专家工作/专家辅助人",
        "03_仲裁/专家工作/技术分析",
        "03_仲裁/和解调解/和解方案",
        "03_仲裁/和解调解/调解笔录",
        "03_仲裁/裁决",
        "04_司法审查/撤裁申请",
        "04_司法审查/不予执行",
        "04_司法审查/承认与执行申请",
        "04_司法审查/法院来文",
        "05_执行/执行文书/执行申请书",
        "05_执行/执行文书/执行异议",
        "05_执行/执行裁定",
        "05_执行/法院来文",
    ]
    for doc in arb_role + ["代理词", "程序申请", "临时措施申请"]:
        dirs.append(f"03_仲裁/仲裁文书/{doc}")
    for d in dirs:
        _m(base, d)


def _create_criminal_staged(base, role):
    """刑事分层：案件级（含"会见"）+ 侦查/审查起诉/一审/二审/再审申诉/执行 各一套。
    role: suspect=辩护（默认）| complainant=被害人代理。"""
    victim = (role == "complainant")
    docword = "代理文书" if victim else "辩护文书"
    strategy = "代理策略" if victim else "辩护策略"
    # 案件级（跨阶段共用）：会见是刑事高频动作，单列 10_会见与沟通
    base_dirs = [
        "00_签约与委托/委托代理协议", "00_签约与委托/授权委托书",
        "00_签约与委托/利益冲突检索", "00_签约与委托/接案笔录",
        "00_签约与委托/主体信息", "00_签约与委托/案件大事记",
        "01_案件资料/客户提供材料", "01_案件资料/事实梳理",
        "01_案件资料/人物关系", "01_案件资料/时间线",
        "02_法律研究/法律法规", "02_法律研究/类案检索",
        "02_法律研究/量刑检索", "02_法律研究/研究备忘录",
        "09_跨境协作/翻译文件", "09_跨境协作/外国律师协作",
        "10_会见与沟通/会见笔录", "10_会见与沟通/会见预约与批准",
        "10_会见与沟通/家属沟通", "10_会见与沟通/阶段汇报", "10_会见与沟通/风险提示",
        "11_账单与工时/账单记录", "11_账单与工时/工时记录", "11_账单与工时/差旅费用",
        f"12_内部工作文件/{strategy}", "12_内部工作文件/工作日志", "12_内部工作文件/模板与格式",
        "13_结案归档/内部归档", "13_结案归档/客户交付",
        "_待分类",
    ]
    # 03 侦查阶段
    stage_dirs = [
        "03_侦查阶段/强制措施/拘留", "03_侦查阶段/强制措施/逮捕",
        "03_侦查阶段/强制措施/监视居住", "03_侦查阶段/强制措施/取保候审",
        "03_侦查阶段/强制措施/羁押必要性审查",
        "03_侦查阶段/侦查机关文书/立案与传唤", "03_侦查阶段/侦查机关文书/搜查扣押冻结",
        "03_侦查阶段/侦查机关文书/鉴定与告知",
        "03_侦查阶段/阶段性材料",
        ("03_侦查阶段/报案控告与立案监督" if victim else "03_侦查阶段/辩护意见与申请"),
        # 04 审查起诉阶段（阅卷是核心）
        "04_审查起诉阶段/阅卷材料/纸质卷宗", "04_审查起诉阶段/阅卷材料/电子卷宗",
        "04_审查起诉阶段/阅卷材料/音视频光盘",
        "04_审查起诉阶段/起诉文书/起诉书", "04_审查起诉阶段/起诉文书/量刑建议书",
        "04_审查起诉阶段/起诉文书/不起诉决定书",
        "04_审查起诉阶段/证据分析/证据目录", "04_审查起诉阶段/证据分析/质证准备",
        # 05 一审
        "05_一审/阅卷材料",
        "05_一审/庭审/庭前会议", "05_一审/庭审/庭审提纲", "05_一审/庭审/发问提纲",
        "05_一审/庭审/举证质证方案", "05_一审/庭审/庭审记录",
        "05_一审/专家与鉴定/重新鉴定申请", "05_一审/专家与鉴定/专家辅助人",
        "05_一审/附带民事诉讼/起诉与答辩", "05_一审/附带民事诉讼/证据", "05_一审/附带民事诉讼/调解",
        "05_一审/法院来文", "05_一审/判决裁定",
        # 06 二审
        "06_二审/上诉与抗诉/上诉状", "06_二审/上诉与抗诉/抗诉应对",
        "06_二审/阅卷补充", f"06_二审/{docword}", "06_二审/庭审", "06_二审/判决裁定",
        # 07 再审与申诉
        "07_再审与申诉/申诉材料/申诉书",
        f"07_再审与申诉/申诉材料/{'再审辩护词' if not victim else '再审代理意见'}",
        "07_再审与申诉/判决裁定",
        # 08 执行阶段
        "08_执行阶段/交付执行", "08_执行阶段/减刑假释", "08_执行阶段/暂予监外执行",
        "08_执行阶段/财产刑执行/罚金", "08_执行阶段/财产刑执行/没收",
    ]
    # 角色相关：审查起诉 + 一审文书
    if victim:
        stage_dirs += [
            "04_审查起诉阶段/追诉与不起诉异议",
            "05_一审/代理文书/被害人代理意见", "05_一审/代理文书/量刑建议",
        ]
    else:
        stage_dirs += [
            "04_审查起诉阶段/辩护意见与申请",
            "04_审查起诉阶段/认罪认罚/具结书", "04_审查起诉阶段/认罪认罚/量刑协商",
            "05_一审/辩护文书/辩护词", "05_一审/辩护文书/质证意见",
            "05_一审/辩护文书/非法证据排除申请", "05_一审/辩护文书/管辖异议与回避",
            "05_一审/辩护文书/调取证据与证人出庭申请",
            "05_一审/量刑/量刑意见", "05_一审/量刑/量刑证据",
        ]
    for d in base_dirs + stage_dirs:
        _m(base, d)


def _create_base(base):
    dirs = [
        "00_签约与委托/委托代理协议",
        "00_签约与委托/授权委托书",
        "00_签约与委托/利益冲突检索",
        "00_签约与委托/接案笔录",
        "00_签约与委托/报价方案",
        "00_签约与委托/主体信息",
        "00_签约与委托/案件大事记",
        "01_案件资料/客户提供材料/合同文件",
        "01_案件资料/客户提供材料/往来函件",
        "01_案件资料/客户提供材料/技术文件",
        "01_案件资料/事实梳理",
        "01_案件资料/现场勘验",
        "02_法律研究/法律法规",
        "02_法律研究/类案检索",
        "02_法律研究/研究备忘录",
        "03_证据/我方证据",
        "03_证据/对方证据",
        "03_证据/质证意见",
        "03_证据/质证意见",
        "03_证据/质证意见",
        "03_证据/证据交换记录",
        "03_证据/证人陈述",
        "09_跨境协作/翻译文件",
        "09_跨境协作/外国律师协作",
        "09_跨境协作/境外诉讼仲裁/US",
        "10_客户沟通/会议纪要",
        "10_客户沟通/阶段汇报",
        "10_客户沟通/邮件摘要",
        "10_客户沟通/往来邮件/我方拟发",
        "10_客户沟通/往来邮件/收发存档",
        "10_客户沟通/风险提示",
        "11_账单与工时/账单记录",
        "11_账单与工时/工时记录",
        "11_账单与工时/差旅费用",
        "12_内部工作文件/案件策略",
        "12_内部工作文件/案件策略",
        "12_内部工作文件/工作日志",
        "12_内部工作文件/模板与格式",
        "13_结案归档/内部归档",
        "13_结案归档/客户交付",
        "_待分类",
    ]
    for d in dirs:
        _m(base, d)

def _create_civil(base, role):
    common = [
        "04_诉讼文书/代理词",     "04_诉讼文书/代理词",   "04_诉讼文书/代理词",
        "04_诉讼文书/保全申请",    "04_诉讼文书/保全申请",
        "04_诉讼文书/程序申请",    "04_诉讼文书/程序申请",
        "04_诉讼文书/法院来文",
        "04_诉讼文书/上诉状",      "04_诉讼文书/上诉状",   "04_诉讼文书/上诉状",
        "04_诉讼文书/再审申请书",  "04_诉讼文书/再审申请书",
        "05_庭审/庭前准备/庭审提纲", "05_庭审/庭前准备/可视化材料",
        "05_庭审/庭审记录/律师自记笔录",
        "06_专家工作/司法鉴定",  "06_专家工作/专家辅助人",  "06_专家工作/技术分析",
        "07_和解与调解/和解方案",  "07_和解与调解/和解方案",
        "07_和解与调解/和解方案",  "07_和解与调解/调解笔录",
        "08_判决与执行/判决书",  "08_判决与执行/上诉文件",
        "08_判决与执行/执行异议",  "08_判决与执行/执行异议",
    ]
    role_dirs = {
        "plaintiff": [
            "04_诉讼文书/起诉状", "04_诉讼文书/起诉状", "04_诉讼文书/起诉状",
            "08_判决与执行/执行申请书", "08_判决与执行/执行申请书",
        ],
        "defendant": [
            "04_诉讼文书/答辩状",  "04_诉讼文书/答辩状",  "04_诉讼文书/答辩状",
            "04_诉讼文书/反诉状",  "04_诉讼文书/反诉状",  "04_诉讼文书/反诉状",
        ],
        "third-party": [
            "04_诉讼文书/第三人意见书", "04_诉讼文书/第三人意见书",
            "04_诉讼文书/第三人意见书",
        ],
    }
    for d in common + role_dirs.get(role, role_dirs["plaintiff"]):
        _m(base, d)

def _create_arbitration(base, role):
    common = [
        "04_仲裁文书/代理词与陈述",  "04_仲裁文书/代理词与陈述",
        "04_仲裁文书/代理词与陈述",
        "04_仲裁文书/程序申请",       "04_仲裁文书/程序申请",
        "04_仲裁文书/临时措施申请",    "04_仲裁文书/临时措施申请",
        "04_仲裁文书/仲裁机构函件",
        "05_庭审/庭前准备/庭审提纲",  "05_庭审/庭前准备/可视化材料",
        "05_庭审/庭审记录/律师自记笔录",
        "06_专家工作/司法鉴定",  "06_专家工作/专家辅助人",  "06_专家工作/技术分析",
        "07_和解与调解/和解方案",      "07_和解与调解/和解方案",
        "07_和解与调解/调解笔录",
        "08_裁决与执行/仲裁裁决书",
        "08_裁决与执行/撤裁申请",      "08_裁决与执行/撤裁申请",
        "08_裁决与执行/承认执行申请",  "08_裁决与执行/承认执行申请",
    ]
    role_dirs = {
        "claimant": [
            "04_仲裁文书/仲裁申请书", "04_仲裁文书/仲裁申请书",
            "04_仲裁文书/仲裁申请书",
        ],
        "respondent": [
            "04_仲裁文书/仲裁答辩书",   "04_仲裁文书/仲裁答辩书",
            "04_仲裁文书/仲裁答辩书",
            "04_仲裁文书/仲裁反请求",    "04_仲裁文书/仲裁反请求",
            "04_仲裁文书/仲裁反请求",
        ],
    }
    for d in common + role_dirs.get(role, role_dirs["claimant"]):
        _m(base, d)

def _create_criminal(base, role):
    common = [
        "CR_侦查阶段/立案文件",
        "CR_侦查阶段/强制措施",
        "CR_侦查阶段/侦查笔录",
        "CR_侦查阶段/搜查扣押冻结",
        "CR_侦查阶段/退补侦查意见",
        "CR_审查起诉阶段/起诉书",
        "CR_审查起诉阶段/不起诉材料",
        "CR_审判阶段/庭审准备/庭审提纲",
        "CR_审判阶段/庭审记录",
        "CR_审判阶段/附带民事诉讼/起诉状",
        "CR_审判阶段/附带民事诉讼/答辩状",
        "CR_判决与执行/刑事判决书",
        "CR_上诉申诉/上诉状", "CR_上诉申诉/上诉状",
        "CR_上诉申诉/申诉书", "CR_上诉申诉/申诉书",
        "06_专家工作/司法鉴定", "06_专家工作/技术分析",
    ]
    role_dirs = {
        "suspect": [
            "CR_侦查阶段/取保候审/申请书", "CR_侦查阶段/取保候审/申请书",
            "CR_侦查阶段/取保候审/决定书",         "CR_侦查阶段/取保候审/担保文件",
            "CR_侦查阶段/羁押必要性审查申请",
            "CR_侦查阶段/阅卷材料",
            "CR_审查起诉阶段/辩护申请",     "CR_审查起诉阶段/辩护申请",
            "CR_审判阶段/辩护词",            "CR_审判阶段/辩护词",
            "CR_审判阶段/辩护词",
            "CR_审判阶段/量刑意见",          "CR_审判阶段/量刑意见",
            "CR_审判阶段/非法证据排除申请",  "CR_审判阶段/非法证据排除申请",
            "CR_执行阶段/减刑假释申请",      "CR_执行阶段/减刑假释申请",
        ],
        "complainant": [
            "CR_侦查阶段/报案控告材料",
            "CR_审查起诉阶段/追诉意见书",
            "CR_审查起诉阶段/不起诉异议",   "CR_审查起诉阶段/不起诉异议",
            "CR_审判阶段/附带民事诉讼/代理词",
            "CR_审判阶段/附带民事诉讼/代理词",
        ],
    }
    for d in common + role_dirs.get(role, role_dirs["suspect"]):
        _m(base, d)

def _create_us_civil(base, role):
    # 美国诉讼文件夹结构（与 folder_guide.yaml 的美国民事一致；含 03_对方 / 04_我方 拆分）
    common = [
        "00_Engagement/Authorization",
        "00_Engagement/Conflict Check",
        "00_Engagement/Engagement Agreement",
        "00_Engagement/Intake Notes",
        "00_Engagement/Party Information",
        "00_Engagement/Proposal & Pitch",
        "01_Case Materials/Attorney-Gathered Materials/Corporate Records",
        "01_Case Materials/Attorney-Gathered Materials/Evidence",
        "01_Case Materials/Client-Provided Materials/Contracts & Agreements",
        "01_Case Materials/Client-Provided Materials/Correspondence",
        "01_Case Materials/Client-Provided Materials/Technical Documents",
        "01_Case Materials/Fact Analysis",
        "01_Case Materials/Site Inspection",
        "02_Legal Research/Case Chronology",
        "02_Legal Research/Case Law Search",
        "02_Legal Research/Research Memos",
        "02_Legal Research/Statutes & Case Law",
        "03_Opposing Party Filings/Discovery Responses/Initial Disclosures",
        "03_Opposing Party Filings/Discovery Responses/Interrogatory Responses",
        "03_Opposing Party Filings/Discovery Responses/Privilege Log",
        "03_Opposing Party Filings/Discovery Responses/RFA Responses",
        "03_Opposing Party Filings/Discovery Responses/RFP Responses",
        "03_Opposing Party Filings/Motions & Briefs/Memorandum in Support",
        "03_Opposing Party Filings/Motions & Briefs/Motion Papers",
        "03_Opposing Party Filings/Motions & Briefs/Opposition Brief",
        "03_Opposing Party Filings/Motions & Briefs/Reply Brief",
        "03_Opposing Party Filings/Pleadings/Amended Pleading",
        "03_Opposing Party Filings/Pleadings/Answer",
        "03_Opposing Party Filings/Pleadings/Complaint",
        "03_Opposing Party Filings/Pleadings/Counterclaim",
        "03_Opposing Party Filings/Pretrial Submissions/Exhibit List",
        "03_Opposing Party Filings/Pretrial Submissions/Motions in Limine",
        "03_Opposing Party Filings/Pretrial Submissions/Pretrial Memorandum",
        "03_Opposing Party Filings/Pretrial Submissions/Witness List",
        "04_Our Filings/Discovery — Responses/Interrogatory Responses",
        "04_Our Filings/Discovery — Responses/Privilege Log",
        "04_Our Filings/Discovery — Responses/RFA Responses",
        "04_Our Filings/Discovery — Responses/RFP Responses",
        "04_Our Filings/Discovery — Sent/Initial Disclosures",
        "04_Our Filings/Discovery — Sent/Interrogatories",
        "04_Our Filings/Discovery — Sent/Requests for Admission (RFAs)",
        "04_Our Filings/Discovery — Sent/Requests for Production (RFPs)",
        "04_Our Filings/Motions & Briefs/Memorandum in Support",
        "04_Our Filings/Motions & Briefs/Motion Papers",
        "04_Our Filings/Motions & Briefs/Opposition Brief",
        "04_Our Filings/Motions & Briefs/Reply Brief",
        "04_Our Filings/Pleadings/Amended Pleading",
        "04_Our Filings/Pleadings/Answer",
        "04_Our Filings/Pleadings/Complaint",
        "04_Our Filings/Pleadings/Counterclaim",
        "04_Our Filings/Pretrial Submissions/Exhibit List",
        "04_Our Filings/Pretrial Submissions/Motions in Limine",
        "04_Our Filings/Pretrial Submissions/Pretrial Memorandum",
        "04_Our Filings/Pretrial Submissions/Witness List",
        "05_Discovery & Doc Review/Depositions/Deposition Outline",
        "05_Discovery & Doc Review/Depositions/Deposition Summary",
        "05_Discovery & Doc Review/Depositions/Deposition Transcript",
        "05_Discovery & Doc Review/Depositions/Notice of Deposition",
        "05_Discovery & Doc Review/Document Collection/Collection Log",
        "05_Discovery & Doc Review/Document Collection/Custodian List",
        "05_Discovery & Doc Review/Document Review/Hot Documents",
        "05_Discovery & Doc Review/Document Review/Privilege Log (Master)",
        "05_Discovery & Doc Review/Document Review/Review Protocol",
        "05_Discovery & Doc Review/Legal Hold/Custodian Acknowledgment",
        "05_Discovery & Doc Review/Legal Hold/Hold Notice",
        "06_Experts/Opposing Experts/Expert Deposition Transcript",
        "06_Experts/Opposing Experts/Expert Report",
        "06_Experts/Opposing Experts/Qualifications & CV",
        "06_Experts/Opposing Experts/Rebuttal Report",
        "06_Experts/Our Experts/Expert Deposition Transcript",
        "06_Experts/Our Experts/Expert Report",
        "06_Experts/Our Experts/Qualifications & CV",
        "06_Experts/Our Experts/Rebuttal Report",
        "07_Trial/Trial Preparation/Closing Argument",
        "07_Trial/Trial Preparation/Jury Instructions",
        "07_Trial/Trial Preparation/Opening Statement",
        "07_Trial/Trial Preparation/Trial Demonstratives",
        "07_Trial/Trial Preparation/Trial Outline",
        "07_Trial/Trial Record/Attorney Trial Notes",
        "07_Trial/Trial Record/Trial Transcript",
        "08_Court Documents/Case Management Order",
        "08_Court Documents/Court Orders",
        "08_Court Documents/Hearing Notices",
        "08_Court Documents/Judgment",
        "08_Court Documents/Summons & Service",
        "09_Settlement/Dismissal Papers",
        "09_Settlement/Settlement Agreement",
        "09_Settlement/Settlement Negotiations",
        "10_Appeal/Appellate Briefs/Appellant's Brief",
        "10_Appeal/Appellate Briefs/Appellee's Brief",
        "10_Appeal/Appellate Briefs/Reply Brief",
        "10_Appeal/Notice of Appeal",
        "10_Appeal/Oral Argument Materials",
        "10_Appeal/Record on Appeal",
        "11_Cross-Border/China-Related Materials",
        "11_Cross-Border/Foreign Counsel Correspondence",
        "11_Cross-Border/Foreign Law Opinions",
        "12_Client Communications/Document Request Lists",
        "12_Client Communications/Email Correspondence/Drafts — Outgoing",
        "12_Client Communications/Email Correspondence/Sent & Received Archive",
        "12_Client Communications/Email Summaries",
        "12_Client Communications/Meeting Minutes",
        "12_Client Communications/Status Reports",
        "12_Client Communications/Strategy Memos to Client",
        "13_Billing & Time/Expense Reports",
        "13_Billing & Time/Invoices",
        "13_Billing & Time/Time Records",
        "14_Internal Work Files/Case Strategy",
        "14_Internal Work Files/Internal Emails",
        "14_Internal Work Files/Templates & Forms",
        "14_Internal Work Files/Work Log",
        "15_Case Closing/Client File Transfer",
        "15_Case Closing/Internal Closing",
    ]
    for d in common:
        _m(base, d)


def create_folders(base: Path, proc_type: str, role: str):
    """先收集全部目录(core relpath)，统一加层级序号(L2='A - '/L3='1. ')再建，适用于所有案件。"""
    global _COLLECT
    _COLLECT = []
    try:
        if proc_type == "us_civil":
            _create_us_civil(base, role)
            _m(base, "_待分类")
        elif proc_type == "civil":
            _create_civil_staged(base, role)
        elif proc_type == "arbitration":
            _create_arbitration_staged(base, role)
        elif proc_type == "criminal":
            _create_criminal_staged(base, role)   # 分层：侦查/审查起诉/一审/二审/再审/执行
        else:
            _create_base(base)
            if proc_type == "mixed":
                civil_role, crim_role = (role + "+suspect").split("+")[:2]
                _create_civil(base, civil_role)
                _create_criminal(base, crim_role)
                for d in ["XX_刑民交叉协调/程序衔接分析",
                          "XX_刑民交叉协调/先刑后民策略",
                          "XX_刑民交叉协调/证据共享目录"]:
                    _m(base, d)
        collected = _COLLECT
    finally:
        _COLLECT = None
    # 统一编号后建目录
    for rel in _number_relpaths(collected):
        Path(base, rel).mkdir(parents=True, exist_ok=True)
    # 占位文件
    for d in base.rglob("*"):
        if d.is_dir() and not any(d.iterdir()):
            (d / ".gitkeep").touch()

# ── config.yaml 追加 ────────────────────────────────────────
def append_case_to_config(cfg_path: Path, key: str, name: str, keywords: list):
    """把新案件追加进 config.yaml 的 cases 列表（YAML 往返，稳健）。
    简称(key)重复时返回 False，不写入。"""
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("cases", [])
    if any(c.get("key") == key for c in cfg["cases"]):
        return False  # 简称已存在
    cfg["cases"].append({"key": key, "name": name, "keywords": list(keywords)})
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120),
        encoding="utf-8",
    )
    return True

# ── case_meta.yaml ──────────────────────────────────────────
def write_meta(base: Path, data: dict):
    today = datetime.now().strftime("%Y-%m-%d")
    role_zh = {
        "plaintiff": "原告方", "defendant": "被告方（含反诉可能）",
        "third-party": "第三人", "claimant": "仲裁申请人",
        "respondent": "仲裁被申请人（含反请求可能）",
        "suspect": "犯罪嫌疑人/被告人方（辩护）",
        "complainant": "被害人/报案人方（代理）",
    }
    type_zh = {
        "civil": "民事诉讼", "arbitration": "仲裁（国内/国际）",
        "criminal": "刑事程序", "mixed": "刑民交叉（多程序并行）",
        "us_civil": "美国民事诉讼（Federal / State Court）",
    }
    role_label = role_zh.get(data["role"], data["role"])
    if "+" in data["role"]:
        parts = data["role"].split("+")
        role_label = f'{role_zh.get(parts[0], parts[0])} + {role_zh.get(parts[1], parts[1])}'

    meta = f"""\
# 案件元数据 — {today} 由 intake.py 生成
# =====================================================
name: "{data['case_name']}"
created: "{today}"

procedure:
  type: "{data['proc_type']}"
  type_zh: "{type_zh.get(data['proc_type'], data['proc_type'])}"

role:
  code: "{data['role']}"
  description: "{role_label}"

parties:
  our_client: {_yaml_str_list(data['parties_us'])}
  opposing:   {_yaml_str_list(data['parties_op'])}
  others:     {_yaml_str_list(data['parties_other'])}

case_info:
  cause_of_action: "{data['cause']}"
  current_stage:   "{data['stage']}"

file_abbr: "{data['abbr']}"
keywords:  [{', '.join('"' + k + '"' for k in data['keywords'])}]

notes: "{data['notes']}"

key_dates:
  retainer:      ""
  filing:        ""
  next_deadline: ""

related_cases: []
"""
    (base / "case_meta.yaml").write_text(meta, encoding="utf-8")

def _yaml_str_list(items):
    if not items:
        return '["（待填写）"]'
    return "[" + ", ".join(f'"{i}"' for i in items) + "]"

# ── .env 路径更新 ────────────────────────────────────────────
def update_env_cases_dir(env_path: Path, new_path: str):
    content = env_path.read_text(encoding="utf-8")
    content = re.sub(r'^CASES_DIR=.*$', f'CASES_DIR="{new_path}"', content, flags=re.MULTILINE)
    env_path.write_text(content, encoding="utf-8")

# ── 预览渲染 ────────────────────────────────────────────────
_PROC_ZH = {
    "civil": "民事诉讼", "arbitration": "仲裁（国内/国际）",
    "criminal": "刑事程序", "mixed": "刑民交叉",
    "us_civil": "美国民事诉讼（Federal / State Court）",
}
_ROLE_ZH = {
    "plaintiff": "原告方", "defendant": "被告方（含反诉）",
    "third-party": "第三人", "claimant": "仲裁申请人",
    "respondent": "仲裁被申请人（含反请求）",
    "suspect": "犯罪嫌疑人/被告人方（辩护）",
    "complainant": "被害人/报案人方（代理）",
}

def show_preview(data: dict, cases_dir: Path):
    base = cases_dir / data["case_name"]
    abbr = data["abbr"]
    today_short = datetime.now().strftime("%y%m%d")

    print(f"\n{c('─' * 56, DIM)}")
    print(c("  预 览", BOLD, CYAN))
    print(c('─' * 56, DIM))

    # 案件基本信息
    print(f"\n  {c('案件信息', BOLD)}")
    print(f"    文件夹名称:  {data['case_name']}")
    print(f"    文件命名简称: {c(abbr, YELLOW)}")
    print(f"    程序类型:    {_PROC_ZH.get(data['proc_type'], data['proc_type'])}")
    proc_role = data["role"]
    if "+" in proc_role:
        parts = proc_role.split("+")
        role_zh = f"{_ROLE_ZH.get(parts[0], parts[0])} + {_ROLE_ZH.get(parts[1], parts[1])}"
    else:
        role_zh = _ROLE_ZH.get(proc_role, proc_role)
    print(f"    我方角色:    {role_zh}")
    print(f"    案由:        {data['cause'] or '（未填写）'}")
    print(f"    当前阶段:    {data['stage'] or '（未填写）'}")

    # 当事人
    print(f"\n  {c('当事人', BOLD)}")
    _show_party("我方主体", data["parties_us"])
    _show_party("对方主体", data["parties_op"])
    if data["parties_other"]:
        _show_party("其他当事人", data["parties_other"])

    # 归档路径
    print(f"\n  {c('归档路径', BOLD)}")
    print(f"    {c(str(base), CYAN)}")

    # config.yaml 追加预览
    print(f"\n  {c('将追加到 config.yaml → cases:', BOLD)}")
    kw_str = ", ".join(f'"{k}"' for k in data["keywords"])
    print(c(f"""    - key: "{abbr}"
      name: "{data['case_name']}"
      keywords: [{kw_str}]""", YELLOW))

    # 命名示例
    print(f"\n  {c('文件命名示例', BOLD)}")
    examples = _naming_examples(data["proc_type"], data["role"], abbr, today_short)
    for ex in examples:
        print(f"    {c(ex, GREEN)}")

    print(c('\n─' * 56, DIM))

def _show_party(label, items):
    if items:
        print(f"    {label}:  {' / '.join(items)}")
    else:
        print(f"    {label}:  {c('（待填写）', GRAY)}")

def _naming_examples(proc_type, role, abbr, date):
    ex = [f"{date} {abbr} 会议纪要 v0 1.docx"]
    if proc_type == "civil":
        if role == "plaintiff":
            ex = [f"{date} {abbr} 民事起诉状 v0 1.docx",
                  f"{date} {abbr} 代理词 v0 1.docx",
                  f"{date} {abbr} 判决书.pdf"]
        elif role == "defendant":
            ex = [f"{date} {abbr} 民事答辩状 v0 1.docx",
                  f"{date} {abbr} 民事反诉状 v0 1.docx",
                  f"{date} {abbr} 裁定书.pdf"]
        else:
            ex = [f"{date} {abbr} 第三人意见书 v0 1.docx",
                  f"{date} {abbr} 代理词 v0 1.docx"]
    elif proc_type == "arbitration":
        if role == "claimant":
            ex = [f"{date} {abbr} 仲裁申请书 v0 1.docx",
                  f"{date} {abbr} Statement of Claim v0 1.docx",
                  f"{date} {abbr} 仲裁裁决书.pdf"]
        else:
            ex = [f"{date} {abbr} 仲裁答辩书 v0 1.docx",
                  f"{date} {abbr} 仲裁反请求申请书 v0 1.docx"]
    elif proc_type == "criminal":
        if role == "suspect":
            ex = [f"{date} {abbr} 取保候审申请书 v0 1.docx",
                  f"{date} {abbr} 辩护词 v0 1.docx",
                  f"{date} {abbr} 起诉书.pdf"]
        else:
            ex = [f"{date} {abbr} 刑事控告书 v0 1.docx",
                  f"{date} {abbr} 追诉意见书 v0 1.docx"]
    elif proc_type == "mixed":
        ex = [f"{date} {abbr} 民事答辩状 v0 1.docx",
              f"{date} {abbr} 辩护词 v0 1.docx",
              f"{date} {abbr} 刑事判决书.pdf"]
    elif proc_type == "us_civil":
        if role == "plaintiff":
            ex = [f"{date} {abbr} Complaint v0 1.docx",
                  f"{date} {abbr} Motion for Summary Judgment v0 1.docx",
                  f"{date} {abbr} Court Order.pdf"]
        elif role == "defendant":
            ex = [f"{date} {abbr} Answer and Counterclaim v0 1.docx",
                  f"{date} {abbr} Motion to Dismiss v0 1.docx",
                  f"{date} {abbr} Scheduling Order.pdf"]
        else:
            ex = [f"{date} {abbr} Third-Party Response v0 1.docx",
                  f"{date} {abbr} Court Order.pdf"]
    return ex

# ── 主流程 ──────────────────────────────────────────────────
def main():
    ws, cfg_path, env_path, default_cases_dir = load_workspace()
    TOTAL = 8

    print(f"\n{c('═' * 56, CYAN, BOLD)}")
    print(c("  案件冷启动访谈  —  ZL 案件工作区", BOLD))
    print(c("  填写信息后将自动生成目录结构 + 归档配置", DIM))
    print(c('═' * 56, CYAN, BOLD))

    data = {}

    # ── 1. 案件名称与简称 ────────────────────────────────────
    step_header(1, TOTAL, "案件名称与文件简称")
    print(c("  完整名称用于创建文件夹，格式建议：年度-客户简称-案由", GRAY))
    data["case_name"] = ask("完整名称（例: 2026-激光华科-产品责任纠纷）")
    print(c("  文件命名简称：全团队统一，≤6字/字符，用于所有文件名前缀", GRAY))
    print(c("  争议类 → 原被告姓名组合（如: 激光华科）; 交易类 → 项目代号（如: ProjectEast）", GRAY))
    data["abbr"] = ask("文件命名简称")

    # ── 2. 程序类型 ──────────────────────────────────────────
    step_header(2, TOTAL, "程序类型")
    data["proc_type"] = ask_choice([
        ("civil",       "中国民事诉讼"),
        ("arbitration", "仲裁（国内 / 国际）"),
        ("criminal",    "中国刑事程序"),
        ("us_civil",    "美国民事诉讼（Federal / State Court）"),
        ("mixed",       "刑民交叉（多程序并行）"),
    ])

    # ── 3. 我方角色 ──────────────────────────────────────────
    step_header(3, TOTAL, "我方角色")
    pt = data["proc_type"]
    if pt in ("civil", "us_civil"):
        data["role"] = ask_choice([
            ("plaintiff",   "Plaintiff / 原告方"),
            ("defendant",   "Defendant / 被告方（含反诉可能）"),
            ("third-party", "Third-party / 第三人"),
        ])
    elif pt == "arbitration":
        data["role"] = ask_choice([
            ("claimant",   "仲裁申请人"),
            ("respondent", "仲裁被申请人（含反请求可能）"),
        ])
    elif pt == "criminal":
        data["role"] = ask_choice([
            ("suspect",     "犯罪嫌疑人 / 被告人方（辩护）"),
            ("complainant", "被害人 / 报案人方（代理）"),
        ])
    else:  # mixed
        print(c("  刑民交叉案件需分别确认两套程序中的角色：", GRAY))
        print(f"\n  {c('民事程序角色:', BOLD)}")
        civil_role = ask_choice([
            ("plaintiff", "原告方"), ("defendant", "被告方"), ("third-party", "第三人"),
        ])
        print(f"\n  {c('刑事程序角色:', BOLD)}")
        crim_role = ask_choice([
            ("suspect", "犯罪嫌疑人/被告人方（辩护）"),
            ("complainant", "被害人/报案人方（代理）"),
        ])
        data["role"] = f"{civil_role}+{crim_role}"

    # ── 4. 案件基本信息 ──────────────────────────────────────
    step_header(4, TOTAL, "案件基本信息")
    data["cause"] = ask("案由（例: 产品责任纠纷 / 合同违约 / 知识产权侵权）", required=False)
    data["stage"] = ask("当前所处阶段（例: 一审庭前 / 仲裁答辩 / 侦查阶段）", required=False)

    # ── 5. 当事人信息 ────────────────────────────────────────
    step_header(5, TOTAL, "当事人信息")
    data["parties_us"]    = ask_parties("我方代表主体")
    data["parties_op"]    = ask_parties("对方主体")
    data["parties_other"] = ask_parties("其他当事人（第三人/担保方等，如无留空）")

    # ── 6. 识别关键词 ────────────────────────────────────────
    step_header(6, TOTAL, "案件识别关键词")
    print(c("  用于自动归档时识别本案文件（当事人名称、案号、项目代号等）", GRAY))
    raw_text = ask_multiline("请粘贴相关文本，或直接输入关键词（逗号/空格分隔）")

    candidates = extract_keywords(raw_text) if raw_text.strip() else []
    if not candidates:
        print(c("  ⚠ 未提取到关键词，请手动输入：", YELLOW))
        manual = ask("手动输入关键词（逗号分隔）", required=False)
        candidates = [k.strip() for k in re.split(r'[,，\s]+', manual) if k.strip()]
    else:
        print(f"\n  {c('提取到的候选关键词：', BOLD)}")
        for i, kw in enumerate(candidates, 1):
            print(f"    {c(str(i), YELLOW)} {kw}")
        print()
        action = ask(
            "全部使用？直接回车确认；输入要删除的编号（空格分隔）；或输入 r 重新输入",
            default="",
            required=False,
        ).strip().lower()
        if action == "r":
            manual = ask("手动输入关键词（逗号分隔）")
            candidates = [k.strip() for k in re.split(r'[,，\s]+', manual) if k.strip()]
        elif action:
            remove_idx = {int(x) - 1 for x in action.split() if x.isdigit()}
            candidates = [k for i, k in enumerate(candidates) if i not in remove_idx]

    # 确保 abbr 也作为关键词（如果不重复）
    if data["abbr"] and data["abbr"] not in candidates:
        candidates.insert(0, data["abbr"])
    data["keywords"] = candidates

    # ── 7. 归档路径 ──────────────────────────────────────────
    step_header(7, TOTAL, "归档存储路径")
    print(f"  当前默认路径: {c(str(default_cases_dir), CYAN)}")
    print(c("  如需改为 OneDrive / iCloud / NAS 等路径，请直接粘贴目录路径", GRAY))
    custom = ask("自定义路径（直接回车使用默认）", default="", required=False).strip()

    if custom:
        cases_dir = Path(custom).expanduser().resolve()
        print(f"  将归档到: {c(str(cases_dir), CYAN)}")
        update_default = ask(
            "是否将此路径设为新的默认归档路径？（更新 .env）[y/N]",
            default="n", required=False
        ).strip().lower()
        data["update_env"] = update_default in ("y", "yes")
    else:
        cases_dir = default_cases_dir
        data["update_env"] = False
    data["cases_dir"] = cases_dir

    # ── 8. 备注 ─────────────────────────────────────────────
    step_header(8, TOTAL, "备注（可选）")
    data["notes"] = ask("补充说明（案件特殊情况、注意事项等）", default="", required=False)

    # ── 预览 ─────────────────────────────────────────────────
    show_preview(data, cases_dir)

    # ── 确认 ─────────────────────────────────────────────────
    confirm = ask(
        f"\n  {c('确认执行？', BOLD)} 输入 Y 创建，N 取消，E 重新填写",
        default="Y", required=False
    ).strip().upper()

    if confirm == "E":
        print(c("  重启访谈...", YELLOW))
        main()
        return
    if confirm not in ("Y", ""):
        print(c("  已取消。", RED))
        return

    # ── 执行 ─────────────────────────────────────────────────
    base = cases_dir / data["case_name"]
    if base.exists():
        print(c(f"  ⚠ 文件夹已存在: {base}", RED))
        over = ask("  是否继续（仅补充缺失目录，不覆盖已有文件）? [y/N]", default="n").lower()
        if over not in ("y", "yes"):
            return

    print(f"\n  {c('正在创建目录结构...', YELLOW)}")
    create_folders(base, data["proc_type"], data["role"])
    print(f"  {c('✅ 目录结构已建立', GREEN)}")

    print(f"  {c('正在更新 config.yaml...', YELLOW)}")
    ok = append_case_to_config(cfg_path, data["abbr"], data["case_name"], data["keywords"])
    if ok:
        print(f"  {c('✅ config.yaml 已更新', GREEN)}")
    else:
        print(c("  ⚠ config.yaml 写入失败（未找到插入位置），请手动添加案件条目", RED))

    write_meta(base, data)
    print(f"  {c('✅ case_meta.yaml 已生成', GREEN)}")

    if data.get("update_env") and env_path.exists():
        update_env_cases_dir(env_path, str(cases_dir))
        print(f"  {c('✅ .env CASES_DIR 已更新 → ' + str(cases_dir), GREEN)}")

    # ── 完成摘要 ─────────────────────────────────────────────
    print(f"\n{c('═' * 56, GREEN, BOLD)}")
    print(c("  ✅  案件初始化完成", BOLD))
    print(c('═' * 56, GREEN, BOLD))
    print(f"\n  案件目录:  {c(str(base), CYAN)}")
    print(f"  下一步:")
    print(f"    1. 检查并完善 {c('case_meta.yaml', YELLOW)}（当事人、关键期限等）")
    print(f"    2. 将文件拖入 {c('inbox/', YELLOW)} 自动归档识别")
    print(f"    3. 守护进程: {c('bash filer.sh start', YELLOW)}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{c('  已退出', RED)}")
        sys.exit(0)
