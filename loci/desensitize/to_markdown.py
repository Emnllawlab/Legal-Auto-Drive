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


def _pdfplumber_text(path: str):
    """尽量读取 PDF 的文字层，作为 PyMuPDF 不可加载时的轻量后备。"""
    try:
        import pdfplumber
    except Exception as exc:
        return "", exc
    try:
        with pdfplumber.open(path) as doc:
            pages = []
            for page in doc.pages:
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append(_dewrap_cjk(text))
            return "\n\n---\n\n".join(pages), None
    except Exception as exc:
        return "", exc


def _pdf_engine_error(exc: Exception) -> RuntimeError:
    """把原生扩展加载错误转成不暴露本机路径的可执行提示。"""
    detail = str(exc)
    if "incompatible architecture" in detail or "dlopen" in detail:
        return RuntimeError(
            "PDF 引擎加载失败：当前启动 Loci 的 Python 与 PDF 依赖架构不一致。"
            "请关闭已有 Loci，先在工作区运行 setup.sh 创建项目虚拟环境，"
            "再用 .venv/bin/python loci/run.py 启动；不要用 Rosetta/x86_64 Python 启动。"
        )
    return RuntimeError(
        f"PDF 引擎加载失败（{type(exc).__name__}）。"
        "请在工作区运行 setup.sh 后重启 Loci。"
    )


_PDF_PAGE_MARKER = re.compile(r"\bPage\s+\d+\s+of\s+\d+\b", re.IGNORECASE)


def _pdf_line_key(line: str) -> str:
    """生成用于识别重复页眉/页脚的行键。

    法院电子卷宗常把页眉作为独立文字层保留下来，而正文是扫描图像。
    页码每页不同，因此在比较前统一成占位符。
    """
    value = re.sub(r"\s+", " ", line.strip())
    value = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", "Page # of #", value,
                   flags=re.IGNORECASE)
    value = re.sub(r"\b\d+\s+of\s+\d+\b", "# of #", value)
    return value.casefold()


def _shared_pdf_lines(page_texts) -> set:
    """找出出现在至少半数页面的重复页眉/页脚行。"""
    counts = {}
    for text in page_texts:
        seen = set()
        for line in (text or "").splitlines():
            key = _pdf_line_key(line)
            # 过滤空行和过短的分隔符，避免把正文里的单个符号误删。
            if len(key) < 5 or key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
    threshold = max(2, (len(page_texts) + 1) // 2)
    return {key for key, count in counts.items() if count >= threshold}


def _remove_shared_pdf_lines(text: str, shared: set) -> str:
    lines = []
    for line in (text or "").splitlines():
        if _pdf_line_key(line) not in shared:
            lines.append(line.strip())
    return "\n".join(line for line in lines if line)


def pdf_to_md(path: str, ocr_engine=None) -> str:
    try:
        import fitz
    except Exception as exc:
        # 文字型 PDF 仍有机会通过纯 Python 的 pdfplumber 读取；扫描件则必须
        # 恢复可用的 PyMuPDF，才能渲染页面交给本地 OCR。
        fallback, fallback_error = _pdfplumber_text(path)
        if fallback.strip():
            return fallback
        raise _pdf_engine_error(exc) from (fallback_error or exc)
    doc = fitz.open(path)
    _ocr = [ocr_engine]
    ocr_failed = False

    def get_ocr():
        if _ocr[0] is None:
            from .ocr import make_ocr
            _ocr[0] = make_ocr()
        return _ocr[0]

    try:
        # 先完整读取文字层，再根据跨页面重复行判断哪些内容只是页眉/页脚。
        # 不能像旧逻辑一样以“是否有任意文字”为 OCR 开关，否则带法院页眉的
        # 扫描 PDF 会被误判为文字型 PDF，正文永远不会进入 OCR。
        page_data = [(page, page.get_text("text").strip()) for page in doc]
        shared = _shared_pdf_lines([text for _, text in page_data])
        pages = []
        for page, native_text in page_data:
            body_text = _remove_shared_pdf_lines(native_text, shared)
            candidate = body_text or native_text

            # 典型情形：文字层只有 “Case ... Page 1 of 7” 等法院页眉，正文在
            # 页面图像中。单页短文也允许按稀疏文字层触发 OCR；OCR 失败时保留
            # 已有文字，保证分析页面仍然可用。
            sparse = len(body_text.strip()) < 80
            has_page_marker = bool(_PDF_PAGE_MARKER.search(native_text))
            needs_ocr = sparse and (has_page_marker or len(native_text) < 120)
            if needs_ocr and not ocr_failed:
                try:
                    ocr_text = _ocr_page(page, get_ocr())
                except Exception:
                    ocr_text = ""
                    ocr_failed = True
                if ocr_text.strip():
                    candidate = ocr_text.strip()

            if candidate:
                pages.append(_dewrap_cjk(candidate))  # 防实体跨行被劈开
        return "\n\n---\n\n".join(pages)
    finally:
        doc.close()


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
