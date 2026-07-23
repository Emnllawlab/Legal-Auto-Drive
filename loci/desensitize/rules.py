"""第 1 层：14 类敏感信息的正则库 + 校验位。

设计原则：能用正则 + 校验位的绝不交给模型（更快、更稳、更省）。
- 过校验位的结构化项（身份证/统一码/银行卡）误报基本归零；
- 高误报项（证券账号/微信号）必须命中上下文关键词才采纳；
- 人名/公司/地址只给"兜底正则"（fallback=True 时启用），主力是 Qwen NER。
"""
import re
import datetime

from .model import Span


# ============================================================
# 1. 身份证号（GB 11643-1999，18 位，mod 11-2 校验）
# ============================================================
RE_IDCARD = re.compile(
    r'(?<![0-9])'
    r'[1-9]\d{5}'
    r'(?:18|19|20)\d{2}'
    r'(?:0[1-9]|1[0-2])'
    r'(?:0[1-9]|[12]\d|3[01])'
    r'\d{3}'
    r'[0-9Xx]'
    r'(?![0-9])'
)


def check_idcard(code: str) -> bool:
    code = code.upper()
    if len(code) != 18:
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_map = "10X98765432"
    try:
        s = sum(int(code[i]) * weights[i] for i in range(17))
    except ValueError:
        return False
    if check_map[s % 11] != code[17]:
        return False
    try:
        datetime.date(int(code[6:10]), int(code[10:12]), int(code[12:14]))
    except ValueError:
        return False
    return True


# ============================================================
# 2. 统一社会信用代码（GB 32100-2015，18 位，mod 31 校验）
# ============================================================
USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"      # 31 字符，剔除 I O S V Z
USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]

RE_USCC = re.compile(
    r'(?<![0-9A-Z])'
    r'[0-9A-HJ-NPQRTUWXY]{2}'
    r'\d{6}'
    r'[0-9A-HJ-NPQRTUWXY]{9}'
    r'[0-9A-HJ-NPQRTUWXY]'
    r'(?![0-9A-Z])'
)


def check_uscc(code: str) -> bool:
    code = code.upper()
    if len(code) != 18 or any(c not in USCC_CHARS for c in code):
        return False
    s = sum(USCC_CHARS.index(code[i]) * USCC_WEIGHTS[i] for i in range(17))
    check = 31 - (s % 31)
    check = 0 if check == 31 else check
    return USCC_CHARS[check] == code[17]


# ============================================================
# 3. 银行卡号（16-19 位，Luhn 校验）
# ============================================================
RE_BANKCARD = re.compile(r'(?<![0-9])\d{16,19}(?![0-9])')


def check_luhn(num: str) -> bool:
    if not num.isdigit():
        return False
    total, alt = 0, False
    for ch in reversed(num):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# ============================================================
# 4-6. 手机 / 固话 / 邮箱
# ============================================================
RE_MOBILE = re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)')
RE_TEL = re.compile(r'(?<!\d)(?:0\d{2,3}-?)?\d{7,8}(?:-\d{1,5})?(?!\d)')
# 电话上下文关键词：裸 7~8 位固话与流水号/金额高度混淆，须靠关键词或区号连字符锚定
_PHONE_KW = re.compile(r'电话|手机|传真|座机|固话|联系方式|联系电话|拨打|致电|来电|专线|热线|Tel|Fax|Phone|Mobile', re.I)
# 号码后紧跟货币单位 → 是金额而非号码，别当电话（用户反馈：金额被误当电话脱敏）
_MONEY_TAIL = re.compile(r'^\s*(?:元|万元|亿元|万|亿|圆|块|美元|港元|欧元|日元|英镑)')
RE_EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

# ============================================================
# 7. 车牌号（普通 + 新能源 + 使领馆/港澳）
# ============================================================
_PROV = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领"
RE_PLATE = re.compile(
    rf'[{_PROV}][A-HJ-NP-Z]'
    r'(?:'
    r'[A-HJ-NP-Z0-9]{4}[A-HJ-NP-Z0-9挂学警港澳]'
    r'|'
    r'(?:[0-9]{5}[DF]|[DF][A-HJ-NP-Z0-9][0-9]{4})'
    r')'
)

# ============================================================
# 8. 案号 / 文号
# ============================================================
RE_CASENO = re.compile(
    r'[（(]\s*[一二三四五六七八九〇零\d]{4}\s*[）)]'
    r'[一-龥]{1,6}'
    r'\d{0,6}'
    r'[一-龥]{1,4}'
    r'\d{1,7}号'
)
RE_DOCNO = re.compile(r'[一-龥A-Z]{1,10}〔\d{4}〕\d{1,5}号')

# ============================================================
# 9. 金额（阿拉伯 + 中文大写）
# ============================================================
RE_MONEY_NUM = re.compile(
    r'(?:[¥￥$]|RMB|人民币)?\s*'
    r'(?:\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)'
    r'\s*(?:元|万元|亿元|万|亿|美元|港元|欧元)'
)
RE_MONEY_CN = re.compile(
    r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元][零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整]*(?:元|圆)(?:整)?'
)

# ============================================================
# 10-11. 证券/基金账号 + 微信号（强依赖上下文关键词）
# ============================================================
RE_SECURITIES = re.compile(r'(?<!\d)\d{10,12}(?!\d)')
_SEC_KW = re.compile(r'证券账户|证券账号|资金账号|基金账号|股东代码')

# 银行账号关键词：对公账户号不满足 Luhn，靠上下文兜底，避免漏标
_BANK_KW = re.compile(r'账户|账号|卡号|银行|开户')

# 合同/裁判文书里的简称声明：「（以下简称平安保险）」「下称"平安"」
# 这是金标准的别名声明，确定性提取，不依赖模型
RE_ABBREV = re.compile(
    r'(?:以下简称|下称|简称)[：:"「『]*'
    r'([一-龥A-Za-z0-9]{2,20})'
)

RE_WECHAT = re.compile(
    r'(?:微信号?|wechat|vx|v信|威信)[:：\s]*([a-zA-Z][a-zA-Z0-9_\-]{5,19})',
    re.IGNORECASE,
)

# ============================================================
# 12-14. 网络标识：IP / MAC / IMEI —— 结构自带高区分度，低误报
# ============================================================
# IPv4：逐段 0-255 校验，避免把版本号/普通小数串误命中
RE_IP = re.compile(
    r'(?<![\d.])'
    r'(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)'
    r'(?![\d.])'
)
RE_MAC = re.compile(
    r'(?<![0-9A-Fa-f:\-])(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:\-])'
)
# IMEI：15 位且过 Luhn（手机串号用 Luhn 校验）；否则须命中关键词
RE_IMEI = re.compile(r'(?<!\d)\d{15}(?!\d)')
_IMEI_KW = re.compile(r'IMEI|串号|设备识别码', re.IGNORECASE)

# ============================================================
# 15. GPS 经纬度（强依赖关键词，避免误伤普通小数对）
# ============================================================
RE_GPS = re.compile(
    r'(?:经纬度|坐标|定位|经度|纬度|GPS)[：:是为]?\s*'
    r'(\d{1,3}\.\d{4,}\s*[,，、\s]\s*\d{1,3}\.\d{4,})',
    re.IGNORECASE,
)

# ============================================================
# 角色锚定人名：法律文书里「角色：姓名」是金标准，确定性高，始终启用，
# 兜住 NER 漏标的代理人/律师/证人名（如「委托诉讼代理人：陈明远」）。
# 仅认 2-4 个汉字、且后接分隔符（，、。；（ 空格 等），避免吞入公司名/长串。
# ============================================================
RE_PERSON_ROLE = re.compile(
    r'(?:委托诉讼代理人|法定代表人|诉讼代理人|代理人|负责人|原告|被告人|被告|上诉人|被上诉人'
    r'|申请人|被申请人|第三人|证人|当事人|委托人|受托人|被害人|犯罪嫌疑人|辩护人)'
    r'[：:]\s*'
    r'([一-龥]{2,4})'
    r'(?=[，,。、；;）)\s（(]|$|男|女|，)'
)
# 匿名人名（王某/李某某）。排除「某」紧接机构后缀的情形（某厂/某公司/某银行——这是机构匿名，
# 不是人名，且会把前一个词的尾字吞进来，如「…公司与某厂」误吃成「司与某」）。
RE_NAME_ANON = re.compile(
    r'[一-龥]{1,2}某(?:某)?'
    r'(?![一-龥]?(?:公司|厂|银行|集团|中心|店|部|所|厅|站|局|院|校|行|贸易|科技|实业))')
# 名字部分允许中英文/数字/括号/连接符（如「XX（中国）有限公司」不被英文前缀截断）；
# 后缀按长优先匹配，避免「股份有限公司」只吃到「有限公司」。
# 注意：「银行」不再作通用后缀（否则「提交的银行」「伪造银行」都会误命中）——银行走高精度 RE_BANK。
RE_COMPANY_SUFFIX = re.compile(
    r'[一-龥A-Za-z0-9（）()·・&\-]{2,40}?'
    r'(?:股份有限公司|有限责任公司|有限合伙企业|有限合伙|有限公司|合伙企业|事务所|分公司|集团)'
)
# 真实银行名（高精度，避免把「提交的银行/伪造银行」误当主体）：
# 知名银行 或 X农商/城商/储蓄/村镇/合作银行，可带分/支行、营业部/厅。
_BANK_BRAND = (
    r'工商|农业|中国|建设|交通|邮政储蓄|招商|浦发|浦东发展|中信|光大|华夏|民生|广发|平安|'
    r'兴业|浙商|渤海|恒丰|北京|上海|江苏|南京|宁波|杭州|徽商|汉口|盛京|哈尔滨|锦州|大连|'
    r'天津|重庆|成都|长沙|贵阳|郑州|青岛|齐鲁|苏州|无锡|温州|台州|嘉兴|厦门|富滇|广州|'
    r'东莞|顺德|珠海|中原|晋商|甘肃|青海|宁夏|长安|西安|昆仑|华融|包商|营口|吉林|昆山')
_BANK_CORE = (
    r'(?:(?:中国)?(?:' + _BANK_BRAND + r')银行(?:股份有限公司)?'
    r'|[一-龥]{2,8}(?:农村商业|农商|城市商业|城商|村镇|商业|储蓄|合作)银行)'
    r'(?:[一-龥]{2,10}?(?:分行|支行|营业部|营业厅))?')
RE_BANK = re.compile(_BANK_CORE)
# 地址前常黏的诉讼角色 + 引导词，脱敏时不应吞进 mask（如「被告现住广东省…」）
_ADDR_LEAD = re.compile(
    r'^(?:被告人?|原告|第三人|上诉人|被上诉人|申请人|被申请人|证人|当事人)?'
    r'(?:住所地?|现住|家住|户籍地?|住址|地址|位于|坐落于?|住|在|于)?')
# 地址：省(可选) + 市(必需) + 区/县(可选) + 路/道(可选) + 门牌(号/室/栋…必需)。
# 修好直辖市/无省份地址（如「北京市朝阳区建国路88号」旧版不命中）；门牌收尾避免误吞法院/机关名。
RE_ADDRESS_RE = re.compile(
    r'(?:[一-龥]{2,7}(?:省|自治区|特别行政区))?'
    r'[一-龥]{2,7}(?:市|自治州|地区|盟)'
    r'(?:[一-龥]{2,7}(?:区|县|旗|市))?'
    r'(?:[一-龥]{0,8}(?:路|街|大道|大街|道|巷|弄|村|镇|乡|街道|花园|小区|广场|工业园|开发区))?'
    r'[一-龥\dA-Za-z]{0,12}?(?:号|弄|栋|幢|座|单元|室|层|楼|大厦|大楼|公寓|中心)'
)


# ============================================================
# 抽取入口
# ============================================================
_VALIDATED = {
    "ID_CARD": (RE_IDCARD, check_idcard),
}

# 统一社会信用代码关键词：校验失败但带关键词时仍兜底（防 OCR 误差导致漏标）
_USCC_KW = re.compile(r'统一社会信用代码|信用代码|纳税人识别号')

# 公司兜底正则常把称谓/语境词一并吞入，提取后需剥离，保证 canonical 干净
_ROLE_PREFIX = re.compile(r'^(被告人?|原告|上诉人|被上诉人|申请人|被申请人|第三人|债务人|债权人)')
# 扫描件 OCR 常把前面的动词/语境黏进公司名，逐层剥离
_CO_LEAD = re.compile(
    r'^(现委托上述受托人在|委托上述受托人在?|受托人在|委托人在|兹委托|现委托|委托人|受托人'
    r'|名称为|名称|简称为|简称|称为|名为|系指'
    r'|汇入|转入|存入|划入|打入|转账至?|汇至|支付至?|指定的?|位于|通过'
    r'|系|称|在|与|及|和|为|向|由|对|从)'
)


# 公司名前若黏入裁判/交易连接词（这些词几乎不出现在公司名内部），从最后一个处右切，
# 避免兜底正则把「…归还原告上海鑫达贸易有限公司」整段吞成一个公司。
_CO_RCUT = re.compile(
    r'^.*(?:归还|返还|支付|给付|判令|赔偿|清偿|出借方为?|借款方为?|收款方为?|付款方为?'
    r'|名为|称为|简称为?)')


def _cut_co_lead(g, start):
    """先按强连接词右切，去掉黏在公司名前的整句裁判用语。"""
    m = _CO_RCUT.match(g)
    if m and m.end() < len(g):
        return g[m.end():], start + m.end()
    return g, start


def _strip_co_lead(g, start):
    """逐层剥离公司名前的称谓/语境前缀，返回 (清洗后文本, 新起点)。"""
    g, start = _cut_co_lead(g, start)
    while True:
        m = _ROLE_PREFIX.match(g) or _CO_LEAD.match(g)
        if not m or m.end() == 0:
            return g, start
        g, start = g[m.end():], start + m.end()


# 公司名内部几乎不会出现的连接词/动词/语境词——从"最后一次"出现处右切，
# 剥掉黏进来的语境，只保留紧贴后缀的真正名字。
# 刻意排除会出现在公司名里的字（如 的→美的、为→华为、和→和记），只留多字短语与极罕见单字。
# 公司名内部几乎不会出现的连接词/动词/裁判语境词——从"最后一次"出现处右切，剥掉黏进来的语境。
# 覆盖判例引用串（"…诉被告X公司…案Y与Z公司…"）。刻意排除会出现在公司名里的字（的/为/和/有）。
_CO_INNER = re.compile(
    r'[、，,；;／/：:。！？]|'
    r'诉|案(?!例)|等|与|及|以及|或|同期|'                        # 引用/并列连接
    r'被告人?|原告|被上诉人|上诉人|被申请人|申请人|第三人|证人|当事人|甲方|乙方|丙方|'  # 诉讼角色
    r'解除|撤销|变更|涉及|关于|纠纷|提交|提供|伪造|虚构|冒充|'    # 动作/语境
    r'拟证明|证明|包含|收到|支付|付款|给付|归还|返还|判令|赔偿|清偿|'
    r'转账|转入|汇入|划入|存入|打入|股东|系指|位于|名下|抵押|质押|担保给|开立|开户')


def _cut_co_inner(g, start):
    """按公司名内部不该出现的连接词右切，去掉如「日按同期」「…提供的」等黏连语境。"""
    ms = list(_CO_INNER.finditer(g))
    if ms:
        cut = ms[-1].end()
        if 0 < cut < len(g):
            return g[cut:], start + cut
    return g, start


# 公共/基准机构：合同里常作利率基准或管辖法院出现，并非当事人主体，默认不脱敏
_PUBLIC_INST = {
    "中国人民银行", "全国银行间同业拆借中心", "中国银行业监督管理委员会",
    "中国证券监督管理委员会", "国家外汇管理局", "最高人民法院", "最高人民检察院",
    "中国人民银行同期", "人民法院", "人民检察院", "仲裁委员会",
}


def _is_public_inst(name):
    n = (name or "").strip().strip("（）()").lstrip("的")
    return n in _PUBLIC_INST


# ── 公司名归一化：把 NER/正则吞进来的过宽片段拆成多个干净公司名 ──
# 广义后缀（含裸「公司」，起点门槛降到 1 字，便于在长片段里逐个抠出）；
# 「银行」不放这里（太松），银行单独用 RE_BANK 高精度抠。
_CO_ANY = re.compile(
    r'[一-龥A-Za-z0-9（）()·・&]{1,40}?'
    r'(?:股份有限公司|有限责任公司|有限合伙企业|有限合伙|有限公司|合伙企业|事务所|'
    r'公司|集团|厂|工厂|证券|保险|信托|担保|租赁|中心|研究院|研究所|学院|大学|'
    r'医院|协会|基金会|合作社)')
# 结果里若只剩一个裸后缀（如「银行」「公司」），说明没抠到真正名字，丢弃
_BARE_SUFFIX = {
    "公司", "有限公司", "股份有限公司", "有限责任公司", "有限合伙", "合伙企业",
    "银行", "集团", "厂", "工厂", "中心", "事务所", "分公司", "证券", "保险",
    "信托", "担保", "租赁", "研究院", "研究所", "学院", "大学", "医院",
}
# 每个片段前的语境残留（动词/介词/助词），从左剥净
_CO_HEAD = re.compile(
    r'^(?:向|为|与|和|及|对|由|从|把|给|该|其|此|本|了|的|是|系|在|并|且|又|还|就|将|已|经|曾|则|即|拟|应|需|已经|包含了?)+')
_CO_EDGE = re.compile(r'^[^一-龥A-Za-z0-9]+|[^一-龥A-Za-z0-9]+$')


def _co_trim(g):
    """把单个片段清洗成干净公司名：内部右切 + 剥前缀语境 + 去首尾标点。"""
    g, _ = _cut_co_inner(g, 0)
    g = _CO_HEAD.sub("", g)
    g = _CO_EDGE.sub("", g)
    return g.strip()


def normalize_company_mention(mention):
    """把可能过宽的公司实体片段（NER/正则）拆成 1~N 个干净公司名。

    例：『丙公司的股东为戊公司』→ ['丙公司','戊公司']；
        『B公司向C公司转账的银行』→ ['B公司','C公司']（裸『银行』被丢）；
        『华为技术有限公司』/『美的集团』→ 原样（内部无连接词）。
    抠不到带后缀的名字时原样返回，避免丢召回。"""
    m = (mention or "").strip()
    if not m:
        return []
    found, seen = [], set()
    for rx in (_CO_ANY, RE_BANK):                       # 公司/机构 + 真实银行
        for mt in rx.finditer(m):
            g = _co_trim(mt.group())
            if len(g) >= 2 and g not in _BARE_SUFFIX and not _is_public_inst(g) and g not in seen:
                seen.add(g)
                found.append(g)
    if found:
        return found
    return [] if _is_public_inst(m) else [m]

# 带标签字段兜底：OCR 会把长串号码读花（0->O、漏字），击穿严格校验正则。
# 凡"字段关键词 + 紧随的 ≥8 位字母数字串"都按对应类别脱敏，不依赖精确格式。
# 长度门槛避免误伤（如"卡号尾号1234"不触发）。
_LABELED = [
    (re.compile(r'(?:统一社会信用代码|信用代码|纳税人识别号)[:：是为]?\s*([0-9A-Za-z]{8,30})'), "USCC"),
    (re.compile(r'(?:身份证号码|身份证号|身份证)[:：是为]?\s*([0-9A-Za-z]{8,20})'), "ID_CARD"),
    (re.compile(r'(?:银行卡号|银行账号|银行账户|开户账号|卡号|账号|账户)[:：是为]?\s*([0-9]{8,25})'), "BANKCARD"),
    # —— 新增证件/账户/社交类，全部关键词门槛，误报可控 ——
    # 护照：中国因私 E/G/D/S/P + 8 位，或外籍字母数字混排
    (re.compile(r'(?:护照号码?|护照)[:：是为]?\s*([A-Za-z]?\d{7,9}|[A-Za-z]{1,2}\d{6,8})'), "PASSPORT"),
    # 港澳台通行证 / 台胞证 / 回乡证
    (re.compile(r'(?:往来港澳通行证|往来台湾通行证|港澳通行证|台湾通行证|台胞证|回乡证|通行证号码?|通行证)[:：是为]?\s*([A-Za-z0-9]{8,11})'), "HK_MO_TW"),
    # 驾驶证（档案编号/证号，通常 12-18 位）
    (re.compile(r'(?:机动车)?(?:驾驶证|驾照)(?:档案编号|证号码?|号码?|号)?[:：是为]?\s*([0-9A-Za-z]{12,18})'), "DRIVER_LICENSE"),
    # 社保 / 公积金账号
    (re.compile(r'(?:社会保障号码?|社保卡号码?|社保卡号|社保号|公积金账号码?|公积金账户|公积金账号|公积金号)[:：是为]?\s*([0-9A-Za-z]{8,25})'), "SOCIAL_SEC"),
    # QQ / 扣扣
    (re.compile(r'(?:QQ|扣扣|企鹅号)(?:号码?|号)?[:：\s]*([1-9]\d{4,10})', re.IGNORECASE), "QQ"),
    # 出生日期（关键词门槛；身份证内的生日已被 ID_CARD 覆盖）
    (re.compile(r'(?:出生日期|出生于|生于)[：:是为]?\s*(\d{4}\s*[年./\-]\s*\d{1,2}\s*[月./\-]\s*\d{1,2}\s*日?)'), "BIRTHDAY"),
]
_REGEX_ONLY = {
    "EMAIL": RE_EMAIL,
    "PLATE": RE_PLATE, "CASE_NO": RE_CASENO, "DOC_NO": RE_DOCNO,
    "MONEY": RE_MONEY_NUM, "MONEY_CN": RE_MONEY_CN,
    "IP": RE_IP, "MAC": RE_MAC,          # 结构自带高区分度，无条件采纳
}   # 注：MOBILE/TEL 移到 extract() 里带上下文门控，避免金额/流水号被误当电话
# 仅在无模型时才开的"噪声较大"兜底（人名/地址）
# 仅无模型时才开的高噪兜底：匿名人名（X某）交给 NER 更稳。
# 地址已移到「始终启用」——NER 对地址召回不稳（判决书里常漏），结构化正则补召回。
_FALLBACK = {
    "NAME_ANON": RE_NAME_ANON,
}


def _near_keyword(text: str, span, pattern, window: int = 15) -> bool:
    s, e = span
    ctx = text[max(0, s - window): e + window]
    return bool(pattern.search(ctx))


def extract(text: str, fallback: bool = False) -> list:
    """返回 source='regex' 的 Span 列表（已过校验位 / 关键词门槛）。

    fallback=True 时追加人名/公司/地址的兜底正则，用于给 NER 补召回。
    """
    hits = []
    for label, (pat, validator) in _VALIDATED.items():
        for m in pat.finditer(text):
            if validator(m.group()):
                hits.append(Span(m.start(), m.end(), label, m.group(), "regex"))
    for label, pat in _REGEX_ONLY.items():
        for m in pat.finditer(text):
            hits.append(Span(m.start(), m.end(), label, m.group(), "regex"))
    # 手机号：高精度；但若号码后紧跟货币单位（如 13800000 元）则是金额尾数，跳过
    for m in RE_MOBILE.finditer(text):
        if _MONEY_TAIL.match(text[m.end():m.end() + 3]):
            continue
        hits.append(Span(m.start(), m.end(), "MOBILE", m.group(), "regex"))
    # 固话：裸 7~8 位数字与金额/流水号高度混淆 → 必须①含区号连字符/分机号 或 ②附近有电话关键词；
    # 且排除紧跟货币单位的情形，避免把金额识别成电话（用户反馈问题①）
    for m in RE_TEL.finditer(text):
        g = m.group()
        if _MONEY_TAIL.match(text[m.end():m.end() + 3]):
            continue
        if ("-" in g) or _near_keyword(text, m.span(), _PHONE_KW):
            hits.append(Span(m.start(), m.end(), "TEL", g, "regex"))
    # 统一社会信用代码：校验通过即采纳；否则需附近有关键词
    for m in RE_USCC.finditer(text):
        if check_uscc(m.group()) or _near_keyword(text, m.span(), _USCC_KW):
            hits.append(Span(m.start(), m.end(), "USCC", m.group(), "regex"))
    # 银行卡/账号：Luhn 通过即采纳；否则需附近有关键词（对公账户号不满足 Luhn）
    for m in RE_BANKCARD.finditer(text):
        if check_luhn(m.group()) or _near_keyword(text, m.span(), _BANK_KW):
            hits.append(Span(m.start(), m.end(), "BANKCARD", m.group(), "regex"))
    # 带标签字段兜底（OCR 击穿严格校验时仍能脱敏）
    for pat, label in _LABELED:
        for m in pat.finditer(text):
            hits.append(Span(m.start(1), m.end(1), label, m.group(1), "regex"))
    # 简称声明：把声明的简称作为 COMPANY 命中，后续在 Registry 里归并到全称
    for m in RE_ABBREV.finditer(text):
        hits.append(Span(m.start(1), m.end(1), "COMPANY", m.group(1), "regex"))
    # 角色锚定人名：「委托诉讼代理人：陈明远」等，确定性高，始终启用
    for m in RE_PERSON_ROLE.finditer(text):
        hits.append(Span(m.start(1), m.end(1), "PERSON", m.group(1), "regex"))
    # 微信号：正则自带关键词前缀
    for m in RE_WECHAT.finditer(text):
        hits.append(Span(m.start(1), m.end(1), "WECHAT", m.group(1), "regex"))
    # 证券账号：高误报，命中后还需附近有关键词
    for m in RE_SECURITIES.finditer(text):
        if _near_keyword(text, m.span(), _SEC_KW):
            hits.append(Span(m.start(), m.end(), "SECURITIES", m.group(), "regex"))
    # IMEI：过 Luhn 即采纳；否则须附近命中 IMEI/串号 关键词
    for m in RE_IMEI.finditer(text):
        if check_luhn(m.group()) or _near_keyword(text, m.span(), _IMEI_KW):
            hits.append(Span(m.start(), m.end(), "IMEI", m.group(), "regex"))
    # GPS 经纬度：正则自带关键词前缀，捕获组即坐标本体
    for m in RE_GPS.finditer(text):
        hits.append(Span(m.start(1), m.end(1), "GPS", m.group(1), "regex"))
    # 公司后缀：高精度（须含 有限公司/事务所 等后缀），始终启用——
    # 兜住 NER 漏标、OCR 跨行截断的公司名；剥离吞入的称谓前缀 + 内部连接词右切。
    for m in RE_COMPANY_SUFFIX.finditer(text):
        g, start = _strip_co_lead(m.group(), m.start())
        g, start = _cut_co_inner(g, start)      # 去「日按同期」「…提供的」等黏连
        g, start = _strip_co_lead(g, start)     # 再剥一次残留前缀
        if len(g) >= 4 and not _is_public_inst(g):
            hits.append(Span(start, start + len(g), "COMPANY_SUFFIX", g, "regex"))
    # 真实银行名（高精度）：知名银行/农商/城商等，避免「提交的银行」误命中
    for m in RE_BANK.finditer(text):
        g, start = _strip_co_lead(m.group(), m.start())
        g, start = _cut_co_inner(g, start)
        if len(g) >= 4 and not _is_public_inst(g):
            hits.append(Span(start, start + len(g), "COMPANY_SUFFIX", g, "regex"))
    # 地址：结构化正则始终启用（NER 对地址召回不稳，判决书里常整段漏标）。
    # 与 NER 命中重叠时 merge 里 ADDRESS(72) 胜 ADDRESS_RE(38)，边界取语义项。
    for m in RE_ADDRESS_RE.finditer(text):
        g, s0 = m.group(), m.start()
        lm = _ADDR_LEAD.match(g)            # 剥掉黏进来的「住/住所地/现住…」引导词
        if lm and lm.end() < len(g):
            g, s0 = g[lm.end():], s0 + lm.end()
        if len(g) >= 4:
            hits.append(Span(s0, s0 + len(g), "ADDRESS_RE", g, "regex"))
    # 噪声较大的人名/地址兜底：仅无模型时开
    if fallback:
        for label, pat in _FALLBACK.items():
            for m in pat.finditer(text):
                hits.append(Span(m.start(), m.end(), label, m.group(), "regex"))
    return hits
