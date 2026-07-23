#!/usr/bin/env python3
"""
本地 LLM 首读模块 — OCR + Qwen preflight
全程本地（Ollama），不上传任何内容到外部服务。
"""

import io, json, os, re, subprocess, tempfile, zipfile
import urllib.request
from pathlib import Path
from typing import Optional

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_PREFLIGHT_MODEL", "qwen2.5:7b")

_PROMPT = """你是法律文件分析助手。阅读以下文件内容，输出JSON（只输出JSON，不要输出任何其他内容）：
{{"事项":"一句话：该文件在案件中的核心作用","文件性质":"具体文件类型（如开庭传票/上诉状/授权委托书/鉴定意见书/质证意见/证据清单/异议书/邮寄记录/营业执照/问题清单/开庭汇报等）","来源方":"我方 或 对方 或 法院 或 内部","日期":"最重要的日期YYYY-MM-DD，无则null"}}

文件名：{filename}
内容：
{content}"""

_SOURCE_MAP = {"我方": "ours", "对方": "opposing", "法院": "court", "内部": "ours"}

_ocr_engine = None

def _get_ocr():
    """统一 OCR：优先 PaddleOCR（经脱敏模块适配器，与脱敏/问答同一套引擎）；
    不可用时退回 RapidOCR。返回对象统一暴露 .run(np_image) -> [(text, box), ...]。"""
    global _ocr_engine
    if _ocr_engine is None:
        import sys
        from pathlib import Path
        loci = Path(__file__).resolve().parent.parent / "loci"
        if str(loci) not in sys.path:
            sys.path.insert(0, str(loci))
        try:
            from desensitize.ocr import make_ocr
            _ocr_engine = make_ocr()          # 默认 Paddle（见 DESENSITIZE_OCR）
        except Exception:
            from rapidocr_onnxruntime import RapidOCR
            _raw = RapidOCR()

            class _RapidShim:                  # 退回：包一层，暴露统一 .run()
                def run(self, image):
                    out, _ = _raw(image)
                    res = []
                    for box, txt, _s in (out or []):
                        xs = [p[0] for p in box]; ys = [p[1] for p in box]
                        res.append((txt, (min(xs), min(ys), max(xs), max(ys))))
                    return res
            _ocr_engine = _RapidShim()
    return _ocr_engine


# ── 文字提取 ──────────────────────────────────────────────────────────────

def _extract_docx(path: str, max_chars: int = 5000) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", xml)
        return re.sub(r"\s+", " ", text).strip()[:max_chars]
    except Exception:
        return ""


def _extract_pdf_text(path: str, max_pages: int = 8, max_chars: int = 5000) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            parts = [p.extract_text() or "" for p in pdf.pages[:max_pages]]
        text = "\n".join(parts).strip()
        return text[:max_chars] if len(text) > 50 else ""
    except Exception:
        return ""


def _extract_pdf_ocr(path: str, max_pages: int = 5, max_chars: int = 5000) -> str:
    try:
        import fitz
        import numpy as np
        from PIL import Image

        ocr = _get_ocr()
        doc = fitz.open(path)
        mat = fitz.Matrix(180 / 72, 180 / 72)
        lines = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            lines.extend(txt for txt, _box in ocr.run(np.array(img)))
        text = "\n".join(lines)
        return text[:max_chars] if text.strip() else ""
    except Exception:
        return ""


def _extract_pptx(path: str, max_chars: int = 5000) -> str:
    """PPTX — 逐张幻灯片提取文字。"""
    try:
        import re as _re
        parts = []
        with zipfile.ZipFile(path) as z:
            slides = sorted(n for n in z.namelist()
                            if _re.match(r"ppt/slides/slide\d+\.xml", n))
            for name in slides:
                xml = z.read(name).decode("utf-8", errors="ignore")
                text = _re.sub(r"<[^>]+>", " ", xml)
                text = _re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts)[:max_chars]
    except Exception:
        return ""


def _extract_doc(path: str, max_chars: int = 5000) -> str:
    """老版 .doc — 用 macOS 内置 textutil 转 txt。"""
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
                # textutil sometimes writes filename without path component
                txts = list(Path(tmpdir).glob("*.txt"))
                out = txts[0] if txts else None
            if not out or not out.exists():
                return ""
            return out.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def _extract_xlsx(path: str, max_chars: int = 5000) -> str:
    """Excel 表格 — 读取各 sheet 的表头和前几行拼成文字摘要。"""
    try:
        import pandas as pd
        sheets = pd.read_excel(path, sheet_name=None, nrows=20, dtype=str)
        parts = []
        for name, df in sheets.items():
            df = df.fillna("")
            parts.append(f"[工作表: {name}]")
            parts.append(df.to_string(index=False, max_rows=20))
        text = "\n".join(parts)
        return text[:max_chars]
    except Exception:
        return ""


def _extract_eml(path: str, max_chars: int = 5000) -> str:
    """邮件 .eml — 提取发件人/收件人/主题/日期/正文。"""
    try:
        import email as _email
        import email.policy
        with open(path, "rb") as f:
            msg = _email.message_from_binary_file(f, policy=_email.policy.default)
        headers = (
            f"发件人: {msg.get('From', '')}\n"
            f"收件人: {msg.get('To', '')}\n"
            f"主题: {msg.get('Subject', '')}\n"
            f"日期: {msg.get('Date', '')}\n"
        )
        body_parts = []
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get_content_disposition() or "")
            if "attachment" in cd:
                body_parts.append(f"[附件: {part.get_filename() or '未知'}]")
                continue
            if ct == "text/plain":
                try:
                    body_parts.append(part.get_content())
                except Exception:
                    pass
        body = "\n".join(body_parts)
        return (headers + body)[:max_chars]
    except Exception:
        return ""


def _extract_image(path: str, max_chars: int = 5000) -> str:
    """图片 — 直接 OCR。"""
    try:
        import numpy as np
        from PIL import Image
        ocr = _get_ocr()
        img = Image.open(path).convert("RGB")
        lines = [txt for txt, _box in ocr.run(np.array(img))]
        if not lines:
            return ""
        text = "\n".join(lines)
        return text[:max_chars] if text.strip() else ""
    except Exception:
        return ""


def extract_text(path: str) -> tuple[str, str]:
    """返回 (text, method)。"""
    ext = path.lower().rsplit(".", 1)[-1]

    if ext == "docx":
        text = _extract_docx(path)
        return (text, "docx") if text else ("", "failed")

    if ext == "doc":
        text = _extract_doc(path)
        return (text, "doc") if text else ("", "failed")

    if ext == "pdf":
        text = _extract_pdf_text(path)
        if text:
            return text, "pdf_text"
        text = _extract_pdf_ocr(path)
        return (text, "pdf_ocr") if text else ("", "pdf_failed")

    if ext in ("xlsx", "xls"):
        text = _extract_xlsx(path)
        return (text, "xlsx") if text else ("", "failed")

    if ext in ("png", "jpg", "jpeg", "bmp", "tiff", "webp"):
        text = _extract_image(path)
        return (text, "image_ocr") if text else ("", "failed")

    if ext == "eml":
        text = _extract_eml(path)
        return (text, "eml") if text else ("", "failed")

    if ext == "pptx":
        text = _extract_pptx(path)
        return (text, "pptx") if text else ("", "failed")

    if ext == "ppt":
        text = _extract_doc(path)  # textutil 同样支持 .ppt
        return (text, "ppt") if text else ("", "failed")

    return "", "unsupported"


# ── Qwen 调用 ─────────────────────────────────────────────────────────────

def _call_qwen(filename: str, content: str, timeout: int = 120) -> Optional[dict]:
    prompt = _PROMPT.format(filename=filename, content=content[:4500])
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 256},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read()).get("response", "").strip()
    m = re.search(r"\{.*?\}", raw, re.DOTALL)
    return json.loads(m.group()) if m else None


# ── 规则映射 ──────────────────────────────────────────────────────────────

def map_to_target(doc_type: str, rules: list,
                  allowed_scenarios=("通用", "中国民事")) -> Optional[str]:
    """将 Qwen 返回的文件性质字符串映射到规则中的 target 文件夹。"""
    if not doc_type:
        return None
    needle = doc_type.lower()
    best_target, best_score = None, 0

    for rule in rules:
        sc = rule.get("scenario", "通用")
        if sc not in allowed_scenarios:
            continue
        rp = (rule.get("rename_pattern") or "").lower()
        if rp and rp in needle:
            if len(rp) > best_score:
                best_score = len(rp)
                best_target = rule.get("target")
        for kw in rule.get("title_keywords", []):
            kw_l = kw.lower()
            if kw_l and kw_l in needle and len(kw_l) > best_score:
                best_score = len(kw_l)
                best_target = rule.get("target")

    return best_target


# ── 公开接口 ──────────────────────────────────────────────────────────────

def llm_preflight(file_path: str, rules: Optional[list] = None) -> dict:
    """
    对单个文件运行 OCR + Qwen 首读，返回：
    {
        "ok": bool,
        "事项": str,
        "文件性质": str,
        "来源方": str,        # 我方/对方/法院/内部
        "source": str,        # ours/opposing/court（mapped）
        "日期": str | None,
        "llm_target": str | None,  # 映射到的归档目标文件夹
        "method": str,        # docx/pdf_text/pdf_ocr/failed
        "error": str | None,
    }
    """
    filename = Path(file_path).name
    text, method = extract_text(file_path)

    if not text:
        return {"ok": False, "method": method, "error": "无法提取文字"}

    try:
        result = _call_qwen(filename, text)
    except Exception as e:
        return {"ok": False, "method": method, "error": str(e)}

    if not result:
        return {"ok": False, "method": method, "error": "LLM 未返回 JSON"}

    doc_type  = result.get("文件性质", "")
    source_cn = result.get("来源方", "")
    llm_target = map_to_target(doc_type, rules or []) if rules else None

    return {
        "ok": True,
        "事项":    result.get("事项", ""),
        "文件性质": doc_type,
        "来源方":  source_cn,
        "source":  _SOURCE_MAP.get(source_cn, ""),
        "日期":    result.get("日期"),
        "llm_target": llm_target,
        "method":  method,
        "error":   None,
    }
