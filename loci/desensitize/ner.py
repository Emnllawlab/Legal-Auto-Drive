"""第 2 层语义识别：调用本地 Qwen（Ollama）做法律实体 NER。

只识别正则搞不定的 3 类语义实体：PERSON / COMPANY / ADDRESS。
结构化项（身份证/手机/银行卡/金额…）交给 rules.py，不浪费模型。

设计要点：
- 本地 localhost 调用，零外发，符合"本地离线"。仅用标准库 urllib。
- 不让模型数字符偏移（LLM 不可靠）：只要"实体文本+类型"，偏移由本模块
  在原文里 find 出来，并校验确为原文子串（丢弃幻觉）。
- 用 Ollama 的 JSON Schema 约束解码，强制结构化输出。
"""
import json
import re
import urllib.request
import urllib.error

from .model import Span
from .rules import normalize_company_mention, _is_public_inst

_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["PERSON", "COMPANY", "ADDRESS"]},
                    "text": {"type": "string"},
                },
                "required": ["type", "text"],
            },
        }
    },
    "required": ["entities"],
}

_SYSTEM_ZH = (
    "你是法律文档实体识别引擎。从用户给的文本中抽取需要脱敏的实体，只输出 JSON。\n"
    "类别定义：\n"
    "- PERSON：自然人真实姓名（当事人、代理人、法定代表人、证人等）。"
    "不要抽取『原告』『被告』『甲方』这类称谓本身，只抽真实姓名。\n"
    "- COMPANY：公司/机构/组织名称，全称与简称都要抽（如『中国平安保险股份有限公司』『平安保险』）。"
    "每家公司单独成条，不要把连接词/动词/语境并进实体——"
    "如『甲公司及乙公司』要拆成两条，『丙公司的股东为戊公司』要拆成两条，"
    "『某公司提供的银行』只抽『某公司』，不要抽『提供的银行』。"
    "『中国人民银行』『人民法院』等作利率基准或管辖机关出现时不要抽。\n"
    "- ADDRESS：具体地址（省市区街道门牌等），不含纯地名行政区。\n"
    "规则：text 必须是原文中原样出现的连续片段，不要改写、不要包含标点；"
    "同一实体多次出现只列一次；没有则返回空数组。"
)

_SYSTEM_EN = (
    "You are a legal-document entity recognizer. Extract entities that must be "
    "redacted from the user's text and output JSON only.\n"
    "Categories:\n"
    "- PERSON: real personal names (parties, counsel, signatories, witnesses). "
    "Do NOT extract role words like 'Plaintiff', 'Defendant', 'Buyer'; only real names.\n"
    "- COMPANY: company / organization names, both full and short forms "
    "(e.g. 'Acme Global Holdings, Inc.' and 'Acme').\n"
    "- ADDRESS: concrete street addresses (number, street, city, state, ZIP).\n"
    "Rules: each text must be a verbatim contiguous substring of the input, no "
    "rewriting, no surrounding punctuation; list each entity once; empty array if none."
)


_SYSTEM_BOTH = (
    "你是法律文档实体识别引擎，文本可能中英混排。从文本中抽取需要脱敏的实体，只输出 JSON。"
    "中文与英文实体都要抽。\n"
    "类别定义：\n"
    "- PERSON：自然人真实姓名（中文姓名或英文姓名，当事人、代理人、法定代表人、证人、签署人等）。"
    "不要抽取『原告』『被告』『甲方』『Plaintiff』『Defendant』这类称谓本身，只抽真实姓名。\n"
    "- COMPANY：公司/机构/组织名称（中英文均可），全称与简称都要抽"
    "（如『中国平安保险股份有限公司』『平安保险』『Acme Global Holdings, Inc.』『Acme』）。"
    "每家公司单独成条，不要把连接词/动词/语境并进实体——"
    "『甲公司及乙公司』拆两条，『丙公司的股东为戊公司』拆两条，"
    "『某公司提供的银行』只抽『某公司』；『中国人民银行』作利率基准出现时不抽。\n"
    "- ADDRESS：具体地址（中文省市区街道门牌，或英文 number/street/city/state/ZIP），不含纯行政区名。\n"
    "规则：text 必须是原文中原样出现的连续片段，不要改写、不要包含首尾标点；"
    "同一实体多次出现只列一次；没有则返回空数组。"
)


def _system_prompt(lang):
    if lang == "both":
        return _SYSTEM_BOTH
    return _SYSTEM_EN if lang == "en" else _SYSTEM_ZH


# 合同/诉讼"角色称谓"硬黑名单：模型常把定义条款里的角色词（Lessor/出租人…）误判为
# 人名/公司。prompt 里提醒不可靠，这里在解析后确定性剔除。英文小写匹配、去 the/前后空格。
_ROLE_STOP = {
    # 英文
    "lessor", "lessee", "licensor", "licensee", "buyer", "seller", "purchaser",
    "vendor", "borrower", "lender", "guarantor", "mortgagor", "mortgagee",
    "assignor", "assignee", "transferor", "transferee", "landlord", "tenant",
    "employer", "employee", "supplier", "customer", "contractor", "client",
    "disclosing party", "receiving party", "party", "parties", "party a", "party b",
    "plaintiff", "defendant", "claimant", "respondent", "appellant", "appellee",
    "the company", "the bank", "the parties", "obligor", "obligee",
    "indemnitor", "indemnitee", "warrantor", "pledgor", "pledgee",
    # 中文（NER 偶尔也会把这些当实体）
    "出租人", "承租人", "出借人", "借款人", "出借方", "借款方", "甲方", "乙方", "丙方",
    "买方", "卖方", "供方", "需方", "买受人", "出卖人", "债权人", "债务人", "保证人",
    "转让方", "受让方", "许可方", "被许可方", "出让方", "受让人", "发包人", "承包人",
    "出租方", "承租方", "委托方", "受托方", "雇主", "雇员", "原告", "被告", "申请人",
    "被申请人", "第三人", "上诉人", "被上诉人",
}


def _is_role_word(mention: str) -> bool:
    m = mention.strip().strip('"“”\'').lower()
    m = re.sub(r'^(?:the|each|either|both)\s+', '', m).strip()
    return m in _ROLE_STOP


class QwenNER:
    """本地 Qwen NER 客户端（默认走 Ollama）。"""

    def __init__(self, model="qwen2.5:7b",
                 base_url="http://localhost:11434",
                 timeout=180, temperature=0.0, lang="zh"):
        self.model = model
        self.url = base_url.rstrip("/") + "/api/chat"
        self.timeout = timeout
        self.temperature = temperature
        self.lang = lang

    def _call(self, text):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _system_prompt(self.lang)},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "format": _SCHEMA,
            "options": {"temperature": self.temperature},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["message"]["content"]

    def extract(self, text) -> list:
        """返回 source='ner' 的 Span 列表。模型不可用时抛异常由调用方决定降级。"""
        raw = self._call(text)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw, re.S)        # 兜底：从噪声里抠 JSON
            data = json.loads(m.group()) if m else {"entities": []}

        spans = []
        seen = set()
        for ent in data.get("entities", []):
            mention = (ent.get("text") or "").strip()
            label = ent.get("type")
            if not mention or label not in ("PERSON", "COMPANY", "ADDRESS"):
                continue
            if _is_role_word(mention):       # 角色称谓不是实体，丢弃（Lessor/出租人…）
                continue
            # 公司：模型常把「甲公司及乙公司」「丙公司的股东为戊公司」吞成一个实体，
            # 也常把「日按同期中国人民银行」这类语境黏进来 → 归一化拆成多个干净公司名。
            if label == "COMPANY":
                mentions = normalize_company_mention(mention)
            else:
                mentions = [mention]
            for men in mentions:
                if not men or (label == "COMPANY" and _is_public_inst(men)):
                    continue
                # 在原文里定位所有不重叠出现（校验确为原文子串，丢弃幻觉）
                for m in re.finditer(re.escape(men), text):
                    key = (m.start(), m.end())
                    if key in seen:
                        continue
                    seen.add(key)
                    spans.append(Span(m.start(), m.end(), label, men, "ner", 0.9))
        return spans


def health_check(base_url="http://localhost:11434", timeout=3) -> bool:
    """探测本地 Ollama 是否在线，便于无模型时降级到纯正则。"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False
