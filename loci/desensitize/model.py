"""共享数据模型与类别归一表。

rules / merge / pseudonymize 三个模块都依赖这里的 Span 与 canon，
保证标签体系全程一致。
"""
from dataclasses import dataclass


@dataclass
class Span:
    """一次敏感信息命中。

    start/end 为字符级偏移（end 不含），偏移基准是传入的那段文本
    （建议按段落 / 表格单元分块，span 用段内局部坐标）。
    """
    start: int
    end: int
    label: str            # 原始标签：ID_CARD / MOBILE / COMPANY / PERSON ...
    text: str
    source: str = "regex"  # "regex" | "ner"
    score: float = 1.0     # NER 置信度；regex 命中传 1.0
    fragments: tuple = ()  # 映射回原文后的不连续片段；空值表示连续区间


# 标签 -> 归一类别：决定"是否同类"（简称/全称、MOBILE/TEL 归同类）
CANON = {
    "ID_CARD": "ID_CARD", "USCC": "USCC", "BANKCARD": "BANKCARD",
    "EMAIL": "EMAIL", "MOBILE": "PHONE", "TEL": "PHONE",
    "PLATE": "PLATE", "CASE_NO": "CASE_NO", "DOC_NO": "CASE_NO",
    "MONEY": "MONEY", "MONEY_CN": "MONEY",
    "SECURITIES": "SECURITIES", "WECHAT": "WECHAT",
    "PERSON": "PERSON", "NAME_ANON": "PERSON",
    "COMPANY": "COMPANY", "COMPANY_SUFFIX": "COMPANY",
    "ADDRESS": "ADDRESS", "ADDRESS_RE": "ADDRESS",
    # 新增证件 / 账户 / 网络标识 / 其他（各自成类，token 化脱敏）
    "PASSPORT": "PASSPORT", "HK_MO_TW": "HK_MO_TW",
    "DRIVER_LICENSE": "DRIVER_LICENSE", "SOCIAL_SEC": "SOCIAL_SEC",
    "QQ": "QQ", "BIRTHDAY": "BIRTHDAY",
    "IP": "IP", "MAC": "MAC", "IMEI": "IMEI", "GPS": "GPS",
    # 英文 locale 专属
    "SSN": "SSN", "EIN": "EIN",
    # 人工复核手动补录的"其他"类
    "OTHER": "OTHER",
}


def canon(label: str) -> str:
    return CANON.get(label, label)
