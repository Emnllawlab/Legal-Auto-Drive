"""各格式文件转 Markdown 纯文本，供脱敏管线消费。

输出仅保留文字和粗略结构（标题/表格/分页），不追求完美排版。
支持：.docx / .doc / .pdf（含扫描件）/ 图片。
"""
from pathlib import Path
import re
import subprocess
import tempfile

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

# ── PDF/OCR 软换行重排 ──────────────────────────────────────────────────────
# PDF 文字层按视觉宽度（~38字）硬换行，会把实体劈成两行：「鑫\n达公司」「有限\n公司」
# 「李\n志强」，导致正则/NER/一致性回扫全部失配。这里把被宽度截断的行重新拼回。
_SENT_END = "。！？!?；;…"                 # 句末标点：保留换行
_CLOSERS  = "”’」』）)】》＞>"               # 句末标点后的收尾引号/括号
_LIST_HEAD = re.compile(
    r'^(?:[一二三四五六七八九十]+[、.．]|（[一二三四五六七八九十\d]+）|\([一二三四五六七八九十\d]+\)'
    r'|\d+[、.．]|第[一二三四五六七八九十百\d]+[条章节款项]|[•·\-*])')


def _dewrap_cjk(text: str, min_wrap: int = 18) -> str:
    """把按宽度硬换行的中文文本重排：续行拼回上一行，真正的段落/句末/标题保留换行。

    判定：上一缓冲行未以句末标点结尾、且长度≥min_wrap（说明是被宽度截断的长行），
    则当前行视为续行拼接；否则刷新成独立行。拉丁字母交界处补空格，中文交界不补。
    """
    out, buf = [], ""

    def flush():
        nonlocal buf
        if buf:
            out.append(buf)
            buf = ""

    for raw in text.split("\n"):
        ln = raw.strip()
        if not ln:
            flush(); out.append(""); continue
        if ln == "---":
            flush(); out.append("---"); continue
        if not buf:
            buf = ln; continue
        last = buf[-1]
        ends_sentence = last in _SENT_END or (last in _CLOSERS and len(buf) >= 2 and buf[-2] in _SENT_END)
        # 上一行很短（标题/署名/列表项）或已到句末 → 不拼接；当前行是列表/条款抬头 → 另起
        if ends_sentence or len(buf) < min_wrap or _LIST_HEAD.match(ln):
            flush(); buf = ln
        elif re.search(r'[A-Za-z0-9]$', buf) and re.match(r'[A-Za-z0-9]', ln):
            buf += " " + ln               # 拉丁词边界补空格
        else:
            buf += ln                     # 中文边界直接拼
    flush()
    return "\n".join(out)


# ── DOCX ──────────────────────────────────────────────────────────────────

def _heading_level(style_name: str) -> int:
    name = (style_name or "").lower()
    for i in range(1, 7):
        if f"heading {i}" in name or f"标题 {i}" in name or f"标题{i}" in name:
            return i
    return 0


def _table_to_md(table) -> str:
    rows = []
    for i, row in enumerate(table.rows):
        cells = [c.text.replace("\n", " ").replace("|", "｜").strip() for c in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("|" + " --- |" * len(cells))
    return "\n".join(rows)


def docx_to_md(path: str) -> str:
    from docx import Document
    doc = Document(path)
    parts = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        lvl = _heading_level(para.style.name)
        parts.append(f"{'#' * lvl} {text}" if lvl else text)
    for table in doc.tables:
        md = _table_to_md(table)
        if md:
            parts.append(md)
    return "\n\n".join(parts)


# ── DOC (老版 Word，macOS textutil) ────────────────────────────────────────

def doc_to_md(path: str) -> str:
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            r = subprocess.run(
                ["textutil", "-convert", "txt", "-output", tmpdir, path],
                capture_output=True, timeout=30,
            )
            if r.returncode != 0:
                return ""
            stem = Path(path).stem
            out = Path(tmpdir) / f"{stem}.txt"
            if not out.exists():
                txts = list(Path(tmpdir).glob("*.txt"))
                out = txts[0] if txts else None
            if not out or not out.exists():
                return ""
            return out.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


# ── PDF ───────────────────────────────────────────────────────────────────

def _ocr_page(page, ocr_engine, zoom: float = 2.0) -> str:
    import io
    import fitz
    import numpy as np
    from PIL import Image
    from .ocr import lines_to_text
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    arr = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB"))
    text, _ = lines_to_text(ocr_engine.run(arr))
    return text.strip()


def pdf_to_md(path: str, ocr_engine=None) -> str:
    import fitz
    doc = fitz.open(path)
    _ocr = [ocr_engine]

    def get_ocr():
        if _ocr[0] is None:
            from .ocr import make_ocr
            _ocr[0] = make_ocr()
        return _ocr[0]

    pages = []
    for page in doc:
        text = page.get_text("text").strip()
        if not text:
            text = _ocr_page(page, get_ocr())
        if text:
            pages.append(_dewrap_cjk(text))      # 重排宽度硬换行，防实体跨行被劈开
    doc.close()
    return "\n\n---\n\n".join(pages)


# ── 图片 ──────────────────────────────────────────────────────────────────

def image_to_md(path: str, ocr_engine=None) -> str:
    if ocr_engine is None:
        from .ocr import make_ocr
        ocr_engine = make_ocr()
    import numpy as np
    from PIL import Image
    from .ocr import lines_to_text
    arr = np.array(Image.open(path).convert("RGB"))
    text, _ = lines_to_text(ocr_engine.run(arr))
    return text.strip()


# ── 统一入口 ──────────────────────────────────────────────────────────────

def to_markdown(path: str, ocr_engine=None) -> str:
    """把文件转成 Markdown 纯文本。不支持的格式返回空字符串。"""
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return docx_to_md(path)
    if ext == ".doc":
        return doc_to_md(path)
    if ext == ".pdf":
        return pdf_to_md(path, ocr_engine=ocr_engine)
    if ext in _IMAGE_EXT:
        return image_to_md(path, ocr_engine=ocr_engine)
    return ""
