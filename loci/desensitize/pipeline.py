"""端到端串联：正则抽取 -> 与 NER 融合 -> 化名替换。

Qwen NER 在这里以"传入 ner_hits（Span 列表）"的形式接入，
保持核心管线与具体模型解耦。把 Qwen 输出的 {type,text,span,score}
转成 Span(source="ner") 喂进来即可。
"""
from pathlib import Path

from .rules import extract
from .rules_en import extract_en
from .merge import merge_spans
from .pseudonymize import (
    Registry, pseudonymize, render, plan, restore, build_restore_map,
    vault_save, vault_load,
)
from .model import Span, canon
from .normalization import normalize_text

__all__ = [
    "Span", "Registry", "desensitize_text", "analyze_text", "apply_review",
    "restore", "build_restore_map", "vault_save", "vault_load",
    "_ner_chunked", "desensitize_md_file", "resolve_categories",
]


_ALL_CATEGORIES = frozenset({
    "COMPANY", "PERSON", "ADDRESS", "PHONE", "EMAIL", "ID_CARD", "USCC",
    "BANKCARD", "PLATE", "CASE_NO", "MONEY", "SECURITIES", "WECHAT", "SSN",
    "EIN", "PASSPORT", "HK_MO_TW", "DRIVER_LICENSE", "SOCIAL_SEC", "QQ",
    "BIRTHDAY", "IP", "MAC", "IMEI", "GPS", "OTHER",
})
# 案件文件对外发送时通常需要严格保护案号/金额；如果文书本身属于公开法律材料，
# 可选 public_legal，保留主体/联系方式等敏感信息但不处理案号、文号和金额。
REDACTION_POLICIES = {
    "strict": _ALL_CATEGORIES,
    "public_legal": _ALL_CATEGORIES - {"CASE_NO", "MONEY"},
}


def spans_from_dicts(text, items):
    """把 analyze_text 的 JSON span 恢复为内部 Span，供原格式 I/O 使用。"""
    return [Span(int(item["start"]), int(item["end"]), item.get("label", "OTHER"),
                 item.get("text", text[int(item["start"]):int(item["end"])]),
                 item.get("source", "regex"), float(item.get("score", 1.0)),
                 tuple(tuple(x) for x in item.get("fragments", ())))
            for item in items]


def _alnorm(t):
    """白名单匹配用的归一：去空白、转小写（中文不受影响）。"""
    return (t or "").strip().replace(" ", "").replace("　", "").lower()


def resolve_categories(cats=None):
    """解析类别集合或策略名，统一 canonical label，避免各 I/O 路径口径不同。"""
    if cats is None:
        return None
    if isinstance(cats, str):
        try:
            return set(REDACTION_POLICIES[cats])
        except KeyError as exc:
            raise ValueError(f"未知脱敏策略：{cats}") from exc
    return {canon(c) for c in cats}


def _eligible_span(s, cats=None, allow=None):
    """统一过滤所有来源的命中，包括 Registry 回扫结果。

    过去 cats/allow 只过滤了本轮 regex/NER，已注册实体在 surface_spans 回扫时
    仍可能重新出现，导致用户明明关闭了某类或加入白名单，第二遍又被脱敏。
    过滤必须发生在 merge 前，且对三类来源一致。
    """
    if cats is not None and canon(s.label) not in cats:
        return False
    if allow and _alnorm(s.text) in allow:
        return False
    return True


def desensitize_text(text, reg: Registry, ner_hits=None, ner=None,
                     fallback=None, ner_score_threshold=0.5, lang=None, cats=None, allow=None):
    """对一段文本脱敏，复用传入的 reg 以保证跨文档化名一致。

    NER 来源三选一（按优先级）：
      1. ner_hits：直接传入已算好的 Span 列表；
      2. ner：传入一个 QwenNER 实例，内部调用 ner.extract(text)；
      3. 都不传：纯正则 + 兜底正则（fallback 自动开），不依赖模型。

    lang: "zh" 用中文正则，"en" 用英文正则；None 则跟随 reg.lang。
    返回 (masked_text, applied)。applied 为本段实际替换明细。
    """
    if lang is None:
        lang = getattr(reg, "lang", "zh")
    cats = resolve_categories(cats)
    if allow:
        allow = {_alnorm(x) for x in allow}
    normalized = normalize_text(text)
    work_text = normalized.text
    if ner_hits is None and ner is not None:
        ner_hits = _ner_chunked(ner, work_text)
    no_model = ner_hits is None and ner is None
    if fallback is None:
        fallback = no_model        # 没有模型时用兜底正则补人名/公司/地址召回
    if lang == "both":             # 中英混排：两套正则都跑，merge_spans 会去重叠
        regex_hits = extract(work_text, fallback=fallback) + extract_en(work_text, fallback=fallback)
    else:
        extractor = extract_en if lang == "en" else extract
        regex_hits = extractor(work_text, fallback=fallback)
    # 脱敏范围过滤：cats 为"要脱敏的归一类别"集合，None=全部。在入册前剔除，
    # 这样被保留的类别（如合同金额 MONEY）既不替换、也不进注册表/回扫。
    regex_hits = [s for s in regex_hits if _eligible_span(s, cats, allow)]
    ner_hits = [s for s in (ner_hits or []) if _eligible_span(s, cats, allow)]
    known_hits = [s for s in reg.surface_spans(work_text)
                  if _eligible_span(s, cats, allow)]
    # 第一遍：抽取 + 已知实体 rescan（跨文档一致），先把本文实体全部登记入册
    spans = merge_spans(regex_hits + known_hits, ner_hits or [],
                        work_text, ner_score_threshold)
    spans = [normalized.map_span(s) for s in spans]
    plan(text, spans, reg)
    # 第二遍：实体已入册，再回扫全文——捕捉同一文档内只被命中一次的名字/简称的
    # 其余 mention（如「被告：李志强」只锚到一处，正文里的「李志强」也要一并替换）
    known_hits = [s for s in reg.surface_spans(work_text)
                  if _eligible_span(s, cats, allow)]
    spans = merge_spans(regex_hits + known_hits, ner_hits or [],
                        work_text, ner_score_threshold)
    spans = [normalized.map_span(s) for s in spans]
    return pseudonymize(text, spans, reg)


def analyze_text(text, reg: Registry, ner=None, lang=None,
                 fallback=None, ner_score_threshold=0.5, allow=None,
                 register=False, cats=None):
    """人工复核第一步：只识别、不提交注册表。

    返回检测到的 span 列表 [{start,end,text,label}]，供 UI 高亮、删误标、补录。
    allow：用户标过"永不脱敏"的词集合，识别阶段就过滤掉，不再让用户重复纠错。
    """
    if lang is None:
        lang = getattr(reg, "lang", "zh")
    cats = resolve_categories(cats)
    if allow:
        allow = {_alnorm(x) for x in allow}
    if fallback is None:
        fallback = ner is None
    normalized = normalize_text(text)
    work_text = normalized.text
    # NER 必须和 regex 使用同一份标准化视图；否则全角/空格/跨页导致的长度变化
    # 会把 NER 偏移错映射到邻近字符。
    ner_hits = _ner_chunked(ner, work_text) if ner else []
    if lang == "both":
        hits = extract(work_text, fallback=fallback) + extract_en(work_text, fallback=fallback)
    else:
        extractor = extract_en if lang == "en" else extract
        hits = extractor(work_text, fallback=fallback)
    hits = [s for s in hits if _eligible_span(s, cats, allow)]
    hits = hits + [s for s in reg.surface_spans(work_text)
                   if _eligible_span(s, cats, allow)]
    spans = merge_spans(hits, ner_hits, work_text, ner_score_threshold)
    spans = [normalized.map_span(s) for s in spans]
    if register:
        # 评测/实际脱敏模式：先登记强命中，再回扫同文档重复表面值；
        # 前台预览默认 register=False，保持“只识别、不修改 Registry”的语义。
        plan(text, spans, reg)
        spans = merge_spans(
            hits + [s for s in reg.surface_spans(work_text)
                    if _eligible_span(s, cats, allow)],
            ner_hits or [], work_text, ner_score_threshold,
        )
        spans = [normalized.map_span(s) for s in spans]
    out = [{"start": s.start, "end": s.end, "text": s.text, "label": s.label,
            "source": s.source, "score": round(float(s.score), 3),
            "low_conf": _is_low_conf(s), "fragments": list(s.fragments)}
           for s in spans]
    if allow:
        out = [s for s in out if _alnorm(s["text"]) not in allow]
    return out


# NER 语义命中且置信度偏低 -> 前端醒目高亮为「待人工确认」。
# 过校验位/关键词门槛的正则命中（score=1.0, source=regex）永远视为高置信。
_LOW_CONF_NER = 0.8


def _is_low_conf(s) -> bool:
    return s.source == "ner" and float(s.score) < _LOW_CONF_NER


def _ner_chunked(ner, text: str, chunk_size: int = 2500) -> list:
    """分块对长文本跑 NER，把块内偏移加上块起始位置还原到全文坐标。

    以最近的换行符为分块边界，避免在实体中间截断。
    """
    if len(text) <= chunk_size:
        return ner.extract(text)
    all_spans = []
    pos = 0
    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        if end < len(text):
            nl = text.rfind('\n', pos, end)
            if nl > pos:
                end = nl + 1
        chunk = text[pos:end]
        for s in ner.extract(chunk):
            all_spans.append(Span(s.start + pos, s.end + pos,
                                  s.label, s.text, s.source, s.score))
        pos = end
    return all_spans


def desensitize_md_file(src, dst, reg: Registry, ner=None, ocr_engine=None, lang=None, cats=None, allow=None):
    """把 src 转成 Markdown、全文脱敏、写到 dst（应为 .md）。返回命中数。

    与 desensitize_docx/pdf/image 相比的优势：
    - 先把全文转为干净纯文本，无 run 分裂、无 PDF 坐标映射问题；
    - NER 以 ~2500 字块跑全文，Qwen 获得更大上下文；
    - 输出 Markdown 便于人工复核。
    """
    from .to_markdown import to_markdown
    md = to_markdown(str(src), ocr_engine=ocr_engine)
    if not md.strip():
        return 0
    # 让端到端管线在标准化视图上调用 NER，再由 normalization.py 映射回 Markdown 原文。
    masked, applied = desensitize_text(md, reg, ner=ner, lang=lang, cats=cats, allow=allow)
    _dst = Path(dst)
    _dst.parent.mkdir(parents=True, exist_ok=True)
    _dst.write_text(masked, encoding="utf-8")
    return len(applied)


def apply_review(text, spans, reg: Registry, mode="alias"):
    """人工复核第二步：按用户编辑后的 span 清单脱敏并提交注册表。

    spans 为 [{start,end,text,label}]（已删误标/已补录）。
    mode: "alias"（化名，入册可逆）| "blackout"（黑框）| "asterisk"（星号）。
    黑框/星号不入册、不可逆——对外交付用。返回 (masked, applied)。
    """
    objs = [Span(int(s["start"]), int(s["end"]), s.get("label", "OTHER"),
                 text[int(s["start"]):int(s["end"])], "manual") for s in spans]
    clean = merge_spans(objs, [], text)        # 去重/裁剪用户可能的重叠补录
    return render(text, clean, reg, mode)
