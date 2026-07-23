"""英文合同的正则库 + 校验位（与中文 rules.py 并列的 en locale）。

复用语言无关的部分（邮箱、Luhn 信用卡）；新增英文专属：
SSN / EIN / 美式电话 / ABA routing(带校验) / $ 金额 / 公司后缀 / hereinafter 简称声明。
中文专属正则在英文文本里不会误触，故无需互斥。
"""
import re

from .model import Span
from .rules import RE_EMAIL, RE_BANKCARD, check_luhn, _near_keyword


# ---------- SSN（XXX-XX-XXXX，无校验位，做无效段排除）----------
RE_SSN = re.compile(r'(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)')


def check_ssn(area, group, serial) -> bool:
    a, g, s = int(area), int(group), int(serial)
    if a == 0 or a == 666 or a >= 900:      # 无效区号段
        return False
    return g != 0 and s != 0


# ---------- EIN（雇主识别号 XX-XXXXXXX）----------
RE_EIN = re.compile(r'(?<!\d)\d{2}-\d{7}(?!\d)')

# ---------- ABA routing（9 位，带 mod-10 加权校验）----------
RE_ROUTING = re.compile(r'(?<!\d)\d{9}(?!\d)')


def check_aba(num: str) -> bool:
    if len(num) != 9 or not num.isdigit():
        return False
    d = [int(c) for c in num]
    checksum = (3 * (d[0] + d[3] + d[6]) +
                7 * (d[1] + d[4] + d[7]) +
                1 * (d[2] + d[5] + d[8]))
    return checksum % 10 == 0


# ---------- 美式电话 ----------
RE_US_PHONE = re.compile(
    r'(?<!\d)(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?!\d)'
)

# ---------- $ 金额 ----------
RE_USD = re.compile(
    r'(?:US)?\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?'
    r'|\b\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s?(?:USD|dollars)\b',
    re.IGNORECASE,
)

# ---------- 公司后缀兜底 ----------
RE_COMPANY_EN = re.compile(
    r'\b(?:[A-Z][A-Za-z0-9&.,\'\-]*\s+){0,6}'
    r'[A-Z][A-Za-z0-9&.,\'\-]*\s*,?\s*'
    r'(?:Inc\.?|LLC|L\.L\.C\.|Ltd\.?|Limited|Corp\.?|Corporation|Company|Co\.|'
    r'LP|L\.P\.|LLP|PLC|GmbH|N\.V\.|S\.A\.)(?![A-Za-z])'
)

# ---------- 简称声明：(hereinafter "X") / (the "Company") ----------
RE_HEREINAFTER = re.compile(
    r'\(\s*(?:hereinafter|hereafter)'
    r'(?:\s+(?:referred to as|called|the))?\s*'
    r'["“\']?([A-Z][A-Za-z0-9 &.\-]{1,40}?)["”\']?\s*\)'
)
RE_DEFINED_TERM = re.compile(
    r'\(\s*(?:the\s+)?["“]([A-Z][A-Za-z0-9 &.\-]{1,40})["”]\s*\)'
)

# 地址兜底（美式：number street, city, ST ZIP）
RE_ADDRESS_EN = re.compile(
    r'\d{1,6}\s+(?:[A-Z][A-Za-z0-9.\-]*\s){1,5}'
    r'(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|Lane|Ln\.?|Drive|Dr\.?|Suite|Ste\.?)'
    r'(?:[A-Za-z0-9 ,.\-]*\b[A-Z]{2}\s+\d{5}(?:-\d{4})?)?'
)

_BANK_KW_EN = re.compile(r'account|routing|bank|ABA|IBAN', re.IGNORECASE)


def extract_en(text: str, fallback: bool = False) -> list:
    """英文文本的正则抽取，返回 source='regex' 的 Span。"""
    hits = []
    for m in RE_SSN.finditer(text):
        if check_ssn(*m.groups()):
            hits.append(Span(m.start(), m.end(), "SSN", m.group(), "regex"))
    for m in RE_EIN.finditer(text):
        hits.append(Span(m.start(), m.end(), "EIN", m.group(), "regex"))
    for m in RE_EMAIL.finditer(text):
        hits.append(Span(m.start(), m.end(), "EMAIL", m.group(), "regex"))
    for m in RE_US_PHONE.finditer(text):
        hits.append(Span(m.start(), m.end(), "PHONE", m.group(), "regex"))
    for m in RE_USD.finditer(text):
        hits.append(Span(m.start(), m.end(), "MONEY", m.group(), "regex"))
    # 信用卡：Luhn 通过即采纳
    for m in RE_BANKCARD.finditer(text):
        if check_luhn(m.group()):
            hits.append(Span(m.start(), m.end(), "BANKCARD", m.group(), "regex"))
    # ABA routing：校验通过，或附近有银行关键词
    for m in RE_ROUTING.finditer(text):
        if check_aba(m.group()) or _near_keyword(text, m.span(), _BANK_KW_EN):
            hits.append(Span(m.start(), m.end(), "BANKCARD", m.group(), "regex"))
    # 简称声明 -> COMPANY，后续在 Registry 归并到全称
    for pat in (RE_HEREINAFTER, RE_DEFINED_TERM):
        for m in pat.finditer(text):
            hits.append(Span(m.start(1), m.end(1), "COMPANY", m.group(1), "regex"))
    # 公司后缀（Inc/LLC/Ltd…）+ 美式地址：高精度（须含法律后缀/街道词），始终启用——
    # 与中文 RE_COMPANY_SUFFIX 对称，兜住 NER 漏标，避免英文公司名/地址整段泄漏。
    # （此前误置于 fallback 分支，有模型时反而不跑，是双语合同英文泄漏的根因。）
    for m in RE_COMPANY_EN.finditer(text):
        hits.append(Span(m.start(), m.end(), "COMPANY_SUFFIX", m.group().strip(), "regex"))
    for m in RE_ADDRESS_EN.finditer(text):
        hits.append(Span(m.start(), m.end(), "ADDRESS_RE", m.group().strip(), "regex"))
    return hits
