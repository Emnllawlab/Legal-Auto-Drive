"""第 2 层：正则命中 × Qwen NER 命中的区间合并 / 去重。

两阶段：
  阶段 1 同类并集去重——同一归一类别下重叠/近邻的命中合并成最宽区间，
         标签取该组 TRUST 最高者（COMPANY 胜 COMPANY_SUFFIX，MOBILE 胜 TEL）。
  阶段 2 异类优先级"涂色"裁剪——按可信度从高到低占位，低可信项只保留
         未被占用的子区间。过校验位的结构化项(95-100)不会被 NER 覆盖。
输出：零重叠、标签正确、可直接交给脱敏替换。
"""
from bisect import insort

from .model import Span, canon


# 可信度：越大越优先；异类冲突时高者独占争议区间
TRUST = {
    "ID_CARD": 100, "USCC": 100, "BANKCARD": 95,        # 过校验位的结构化项最高
    "SSN": 96, "EIN": 92,                                # 英文 locale 结构化项
    "EMAIL": 90, "MOBILE": 88, "PLATE": 88, "CASE_NO": 85, "DOC_NO": 85, "TEL": 80,
    "IMEI": 92, "IP": 90, "MAC": 90,                     # 结构化网络/设备标识
    "PASSPORT": 85, "HK_MO_TW": 85, "DRIVER_LICENSE": 84, "SOCIAL_SEC": 84,  # 关键词门槛的证件
    "GPS": 82, "QQ": 70, "BIRTHDAY": 55,                 # 坐标/账号/日期
    "WECHAT": 70, "MONEY": 60, "MONEY_CN": 60, "SECURITIES": 50,
    "PERSON": 78, "COMPANY": 75, "ADDRESS": 72,          # NER 语义项居中
    "NAME_ANON": 45, "COMPANY_SUFFIX": 40, "ADDRESS_RE": 38,  # 弱正则兜底最低
}
DEFAULT_TRUST = 30

CAT_GAP = {"ADDRESS": 2}                 # 同类并集允许的相邻间隙（NER 常把地址切两段）
MIN_LEN = {"PHONE": 7, "BANKCARD": 12, "ID_CARD": 15}
DEFAULT_MIN_LEN = 2


def _trust(label):
    return TRUST.get(label, DEFAULT_TRUST)


def _min_len(cat):
    return MIN_LEN.get(cat, DEFAULT_MIN_LEN)


# ---------- 阶段 1：同类并集去重 ----------
def _merge_same_category(spans, text):
    buckets = {}
    for s in spans:
        buckets.setdefault(canon(s.label), []).append(s)

    merged = []
    for cat, group in buckets.items():
        group.sort(key=lambda x: (x.start, -x.end))
        gap = CAT_GAP.get(cat, 0)
        cur = None
        for s in group:
            if cur and s.start <= cur["end"] + gap:
                cur["end"] = max(cur["end"], s.end)
                cur["best"] = max(cur["best"], s,
                                  key=lambda x: (_trust(x.label), x.score))
                cur["spans"].append(s)
            else:
                if cur:
                    merged.append(_finalize(cur, text))
                cur = {"start": s.start, "end": s.end, "best": s, "spans": [s]}
        if cur:
            merged.append(_finalize(cur, text))
    return merged


def _finalize(cur, text):
    b = cur["best"]
    fragments = _merge_fragments(
        fragment
        for s in cur.get("spans", [b])
        for fragment in (s.fragments or ((s.start, s.end),))
    )
    display = "".join(text[a:b] for a, b in fragments)
    return Span(cur["start"], cur["end"], b.label,
                display, b.source, b.score, tuple(fragments))


def _merge_fragments(fragments):
    out = []
    for start, end in sorted(fragments):
        if not out or start > out[-1][1]:
            out.append([start, end])
        else:
            out[-1][1] = max(out[-1][1], end)
    return [(a, b) for a, b in out]


# ---------- 阶段 2：异类优先级涂色裁剪 ----------
def _resolve_cross_category(spans, text):
    spans.sort(key=lambda x: (-_trust(x.label), -(x.end - x.start)))
    occupied = []       # 已接受的 (start,end)，始终有序、互不重叠
    result = []
    for s in spans:
        for fs, fe in _free_intervals(s.start, s.end, occupied):
            # MIN_LEN 只用来丢弃"被裁剪后的碎片"，绝不否决完整命中
            trimmed = not (fs == s.start and fe == s.end)
            if trimmed and fe - fs < _min_len(canon(s.label)):
                continue
            fragments = []
            for a, b in s.fragments or ((s.start, s.end),):
                left, right = max(a, fs), min(b, fe)
                if left < right:
                    fragments.append((left, right))
            fragments = _merge_fragments(fragments or [(fs, fe)])
            display = "".join(text[a:b] for a, b in fragments)
            result.append(Span(fs, fe, s.label, display, s.source, s.score,
                               tuple(fragments)))
            insort(occupied, (fs, fe))
    result.sort(key=lambda x: x.start)
    return result


def _free_intervals(start, end, occupied):
    """返回 [start,end) 中未被 occupied 覆盖的子区间。"""
    free, cur = [], start
    for s, e in occupied:
        if e <= cur:
            continue
        if s >= end:
            break
        if s > cur:
            free.append((cur, min(s, end)))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        free.append((cur, end))
    return free


def merge_spans(regex_hits, ner_hits, text, ner_score_threshold: float = 0.5):
    """主入口：传入正则命中与 NER 命中，返回无重叠的最终 Span 列表。"""
    ner_hits = [s for s in ner_hits if s.score >= ner_score_threshold]
    stage1 = _merge_same_category(list(regex_hits) + list(ner_hits), text)
    return _resolve_cross_category(stage1, text)
