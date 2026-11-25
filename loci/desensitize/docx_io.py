"""docx 原位改写：脱敏的同时保留原排版（字体/加粗/表格/页眉页脚）。

难点是 Word 把一句话拆成多个 run，敏感实体常跨 run。本模块：
1. 把段落的所有 run 拼成段文本，记录每个 run 的字符区间；
2. 在段文本上跑识别管线，得到带偏移的替换计划；
3. 把替换写回 run：化名整体放进 span 起始所在的 run，跨到的其余 run 删除被覆盖字符。
   → 化名继承起始 run 的格式（法律文书可接受），其余格式原样保留。

覆盖范围：正文段落、表格（含嵌套）、页眉页脚。
需要 pip install python-docx（延迟导入）。
"""
from .pipeline import analyze_text, spans_from_dicts
from .pseudonymize import plan, Registry


def _iter_paragraphs(container):
    """递归产出容器（Document / _Cell / header / footer）内的所有段落。"""
    for p in container.paragraphs:
        yield p
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from _iter_paragraphs(cell)


def _all_paragraphs(doc):
    yield from _iter_paragraphs(doc)
    for section in doc.sections:
        for hf in (section.header, section.footer,
                   section.first_page_header, section.first_page_footer,
                   section.even_page_header, section.even_page_footer):
            yield from _iter_paragraphs(hf)


def _apply_to_runs(runs, text, reps):
    """把替换计划写回 run，保留各 run 原格式。reps 为 plan() 的输出。"""
    # 先记录每个 run 编辑前的字符区间
    bounds, pos = [], 0
    for r in runs:
        n = len(r.text)
        bounds.append((pos, pos + n))
        pos += n

    reps = sorted(reps, key=lambda x: x["start"])
    for idx, run in enumerate(runs):
        rs, re_ = bounds[idx]
        if rs == re_:
            continue
        pieces, cur = [], rs
        for rep in reps:
            s, e = rep["start"], rep["end"]
            if e <= rs or s >= re_:          # 与本 run 无交集
                continue
            if s > cur:                       # span 前未覆盖的原文
                pieces.append(text[cur:min(s, re_)])
            if rs <= s < re_:                 # span 起点落在本 run → 放化名
                pieces.append(rep["alias"])
            cur = max(cur, min(e, re_))       # 跳过被覆盖部分
            if cur >= re_:
                break
        if cur < re_:
            pieces.append(text[cur:re_])
        run.text = "".join(pieces)


def _process_paragraph(para, reg, ner, fallback, lang, cats=None, allow=None):
    runs = para.runs
    text = "".join(r.text for r in runs)
    if not text.strip():
        return 0
    detected = analyze_text(text, reg, ner=ner, fallback=fallback,
                            lang=lang, cats=cats, allow=allow, register=True)
    spans = spans_from_dicts(text, detected)
    if not spans:
        return 0
    _apply_to_runs(runs, text, plan(text, spans, reg))
    return len(spans)


def desensitize_docx(in_path, out_path, reg: Registry, ner=None,
                     lang="both", cats=None, allow=None):
    """脱敏一个 .docx，保留排版写到 out_path。复用 reg 保证跨文档化名一致。

    返回命中实体总数。ner 为 QwenNER 实例则按段调用；为 None 则纯正则+兜底。
    """
    from docx import Document
    doc = Document(in_path)
    fallback = ner is None
    total = 0
    for para in _all_paragraphs(doc):
        total += _process_paragraph(para, reg, ner, fallback, lang, cats, allow)
    doc.save(out_path)
    return total
