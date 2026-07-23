"""PDF 脱敏：真实涂改（不是画个黑框）。

PDF 文本是绝对定位、不能像 docx 那样回流，所以用 PyMuPDF 的 redaction：
apply_redactions() 会**真正删除底层文字**（不留可复制的原文），再按需叠加化名。
两种模式：
  - "alias"   ：白底覆盖 + 叠加化名（甲公司 / 〔PHONE1〕），适合喂给外部 AI。
  - "blackout"：黑框涂黑，适合对外披露版。

定位方式：在页面文本上跑识别管线得到"原文串+化名"，再用 page.search_for()
找几何位置——绕开"字符偏移→坐标"的映射难题，稳。
扫描件（无文本层）此处跳过，留给后续 OCR 接入。
需要 pip install pymupdf（延迟导入）。
"""
from .rules import extract
from .merge import merge_spans
from .pseudonymize import plan, Registry

_CJK_FONT = "china-s"      # PyMuPDF 内置简体中文字体，用于叠加化名


def _mostly_inside(inner, outer) -> bool:
    inter = inner & outer
    if inter.is_empty:
        return False
    return abs(inter) >= 0.8 * abs(inner)      # abs(Rect) = 面积（PyMuPDF 1.26）


def _dedup(items):
    """去掉被更大矩形基本包含的小矩形（简称常落在全称内部）。"""
    items = sorted(items, key=lambda it: -abs(it[0]))
    kept = []
    for rect, alias in items:
        if any(_mostly_inside(rect, k) for k, _ in kept):
            continue
        kept.append((rect, alias))
    return kept


def _draw_alias(page, rect, alias):
    # 用点锚定的 insert_text（不受框宽限制；化名短，溢出风险低），
    # textbox 会因化名比原文窄/宽而判定 overflow 静默不画。
    size = max(7.0, min(rect.height * 0.75, 12.0))
    point = (rect.x0, rect.y1 - 2)            # 贴近原文基线
    try:
        page.insert_text(point, alias, fontname=_CJK_FONT,
                         fontsize=size, color=(0, 0, 0))
    except Exception:
        page.insert_text(point, alias, fontsize=size, color=(0, 0, 0))


def _apply_items(page, items, mode):
    """对一页套用涂改：去重 -> redaction 抹除 -> 按需叠化名。"""
    if not items:
        return 0
    items = _dedup(items)
    fill = (0, 0, 0) if mode == "blackout" else (1, 1, 1)
    for rect, _ in items:
        page.add_redact_annot(rect, fill=fill)
    page.apply_redactions()                 # 真正抹除底层文字/图像
    if mode == "alias":
        for rect, alias in items:
            _draw_alias(page, rect, alias)
    return len(items)


def _reps_for(text, reg, ner, fallback):
    # 两遍：第一遍把实体登记进 Registry；第二遍 surface_spans 用已知实体
    # 把同页里 NER 漏掉的重复 mention（如被印章压住截断的公司名）补齐。
    ner_hits = ner.extract(text) if ner else []

    def build():
        hits = extract(text, fallback=fallback) + reg.surface_spans(text)
        return plan(text, merge_spans(hits, ner_hits, text), reg)

    build()
    return build()


def _text_items(page, reg, ner, fallback):
    """文本层页：search_for 定位原文串。"""
    items = []
    for rep in _reps_for(page.get_text("text"), reg, ner, fallback):
        for rect in page.search_for(rep["original"]):
            items.append((rect, rep["alias"]))
    return items


def _scanned_items(page, reg, ner, fallback, engine, zoom):
    """扫描页（无文本层）：渲染 -> OCR -> 像素框换算回 PDF 坐标。"""
    import io
    import fitz
    import numpy as np
    from PIL import Image
    from .ocr import lines_to_text, span_to_boxes

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    arr = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    full_text, ranges = lines_to_text(engine.run(arr))
    if not full_text.strip():
        return []
    ranges = [(ls, le, (x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom))
              for ls, le, (x0, y0, x1, y1) in ranges]
    items = []
    for rep in _reps_for(full_text, reg, ner, fallback):
        for box in span_to_boxes(rep["start"], rep["end"], ranges):
            items.append((fitz.Rect(*box), rep["alias"]))
    return items


def _is_scanned(page, thresh=0.6):
    """图像覆盖页面 ≥thresh 视为扫描件（原生文本 PDF 不会满页大图）。"""
    import fitz
    try:
        infos = page.get_image_info()
    except Exception:
        return False
    if not infos:
        return False
    page_area = abs(page.rect) or 1
    img_area = sum(abs(fitz.Rect(im["bbox"])) for im in infos)
    return img_area >= thresh * page_area


def _seal_items(page, zoom):
    """渲染整页检测红色印章，框换算回 PDF 坐标。"""
    import io
    import fitz
    import numpy as np
    from PIL import Image
    from .seal_detect import detect_seals

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    arr = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    return [(fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom), "〔印章〕")
            for (x0, y0, x1, y1) in detect_seals(arr)]


def desensitize_pdf(in_path, out_path, reg: Registry, ner=None, mode="alias",
                    ocr_engine=None, ocr_zoom=2.0, detect_seals=False):
    """脱敏 PDF，写到 out_path。复用 reg 保证跨文档/跨页化名一致。

    有文本层的页走 search_for；无文本层的扫描页，若传了 ocr_engine 则
    渲染+OCR 后涂改，否则跳过。detect_seals=True 时各页另检测红色印章。
    mode="alias" 叠化名 / "blackout" 涂黑。返回涂改区域总数。
    """
    import fitz
    doc = fitz.open(in_path)
    fallback = ner is None
    total = 0
    for page in doc:
        # 整页基本是图像的"扫描件"——即便带 OCR 文本层也强制走 OCR 像素路径，
        # 避免文本层坐标与可见图像错位导致涂改/叠加跑偏。
        scanned = _is_scanned(page)
        if not scanned and page.get_text("text").strip():
            items = _text_items(page, reg, ner, fallback)
        elif ocr_engine is not None:
            items = _scanned_items(page, reg, ner, fallback, ocr_engine, ocr_zoom)
        else:
            items = []
        if detect_seals:
            items += _seal_items(page, ocr_zoom)
        total += _apply_items(page, items, mode)
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    return total
