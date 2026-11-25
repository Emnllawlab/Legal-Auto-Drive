#!/usr/bin/env python3
"""
涉外诉讼材料自动归档守护进程 v2.2
─────────────────────────────────
支持: docx / 文字PDF / 扫描版PDF(OCR) / 图片(OCR) / txt / xlsx
全程本地运行，不上传任何文件。

命名规范（遵循中伦工作指引 v1.0）：
  格式：YYMMDD 案件简称 文书类型 vX X.ext
  日期：六位收件日期（文件放入收件箱当天 ≈ 收件日，非文书落款日）
  版本：v0 1（过程稿）/ v1 0（提交版）/ vfinal（签署版）/ 无（法院/仲裁机构来文）
  注意：版本号中间空格，不加点（v0 1 ✓，v0.1 ✗）
"""

import os, sys, re, json, shutil, time, yaml, logging, argparse, subprocess, hashlib, signal, threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

try:  # package import
    from .safety import FileLock, LockBusy, SafetyError, resolve_under, validate_relative_target
    from .domain import runtime_case_view
    from .ledger import FilingLedger
    from .config_io import set_private_umask, tighten_private_permissions
    from .daemon_control import (
        DaemonAlreadyRunning, acquire_daemon_instance, unattended_filing_enabled,
    )
except ImportError:  # direct script import used by the legacy CLI/tests
    from safety import FileLock, LockBusy, SafetyError, resolve_under, validate_relative_target
    from domain import runtime_case_view
    from ledger import FilingLedger
    from config_io import set_private_umask, tighten_private_permissions
    from daemon_control import (
        DaemonAlreadyRunning, acquire_daemon_instance, unattended_filing_enabled,
    )
try:
    from .decision import (build_filing_decision, SOURCE_OPTIONS,
                           UNRESOLVED_SOURCE_STATUSES, _keyword_matches)
except ImportError:
    from decision import (build_filing_decision, SOURCE_OPTIONS,
                          UNRESOLVED_SOURCE_STATUSES, _keyword_matches)
try:
    from .state_db import DecisionStore
except ImportError:  # legacy copied CLI bundles may not include optional state DB
    try:
        from state_db import DecisionStore
    except ImportError:
        DecisionStore = None

# ============================================================
# 环境变量加载
# ============================================================
def load_env() -> dict:
    import os as _os, re as _re
    env = {}
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        raw = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                raw[k] = v
        for k, v in raw.items():
            def _repl(m):
                vn = m.group(1) or m.group(2)
                return raw.get(vn, _os.environ.get(vn, m.group(0)))
            env[k] = _re.sub(r'\$\{(\w+)\}|\$(\w+)', _repl, v)
    return env

ENV = load_env()

# ============================================================
# OCR / PDF 文本提取  —— 纯 Python，无需系统工具
#   文字提取: PyMuPDF(fitz)；OCR: rapidocr-onnxruntime（离线），回退 tesseract
# ============================================================
_RAPIDOCR = None  # 懒加载单例

def _get_rapidocr():
    global _RAPIDOCR
    if _RAPIDOCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPIDOCR = RapidOCR()
        except Exception:
            _RAPIDOCR = False
    return _RAPIDOCR or None

def _ocr_available() -> bool:
    return _get_rapidocr() is not None or shutil.which("tesseract") is not None

def _ocr_bytes(img_bytes: bytes) -> str:
    """对一张图片（PNG/JPG 字节）做 OCR，返回文本。"""
    eng = _get_rapidocr()
    if eng:
        try:
            res, _ = eng(img_bytes)
            if res:
                return "\n".join(line[1] for line in res)
        except Exception:
            pass
    # 回退：系统 tesseract
    if shutil.which("tesseract"):
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
                tf.write(img_bytes); tf.flush()
                r = subprocess.run(
                    ["tesseract", tf.name, "stdout", "-l", "chi_sim+eng", "--psm", "3"],
                    capture_output=True, text=True, timeout=60
                )
                return r.stdout
        except Exception:
            pass
    return ""

def _ocr_image(image_path: str, lang: str = "chi_sim+eng") -> str:
    try:
        with open(image_path, "rb") as f:
            return _ocr_bytes(f.read())
    except Exception:
        return ""

def _pdf_extract_text(file_path: Path, max_pages: int = 3) -> str:
    """用 PyMuPDF 直接提取 PDF 文字层（文字版 PDF）。"""
    try:
        import fitz
        doc = fitz.open(str(file_path))
        parts = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            parts.append(page.get_text())
        doc.close()
        return "\n".join(parts)
    except Exception:
        return ""

def _ocr_pdf_scanned(file_path: Path, max_pages: int = 3) -> str:
    """扫描版 PDF：用 PyMuPDF 把每页渲染成图片再 OCR。"""
    if not _ocr_available():
        return ""
    try:
        import fitz
        doc = fitz.open(str(file_path))
        parts = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=200)
            txt = _ocr_bytes(pix.tobytes("png"))
            if txt.strip():
                parts.append(txt)
        doc.close()
        return "\n".join(parts)
    except Exception:
        return ""

# ============================================================
# 文件内容提取 — OCR → Markdown 统一管道
# ============================================================

def _text_to_markdown(text: str) -> str:
    """将原始 OCR/纯文本格式化为最简 Markdown：首行短标题升为 H1。"""
    lines = [l.rstrip() for l in (text or "").splitlines()]
    out = []
    heading_done = False
    for line in lines:
        if not heading_done and line.strip():
            s = line.strip()
            # 首行：长度合理且不像句子中段 → 视为文书标题
            if len(s) <= 60 and not s.endswith(('，', ',', '；', ';')):
                out.append(f"# {s}")
                heading_done = True
                continue
            heading_done = True
        out.append(line)
    return "\n".join(out)


def _docx_to_markdown(file_path: Path) -> str:
    """DOCX → Markdown，保留标题层级与表格结构。"""
    try:
        from docx import Document
        from docx.text.paragraph import Paragraph
        from docx.table import Table
        doc = Document(str(file_path))
        parts = []
        heading_promoted = False
        for block in doc.element.body:
            tag = block.tag.split('}')[-1] if '}' in block.tag else block.tag
            if tag == 'p':
                para = Paragraph(block, doc)
                text = para.text.strip()
                if not text:
                    continue
                sname = (para.style.name or "").lower()
                if "heading 1" in sname or sname == "title":
                    parts.append(f"# {text}")
                    heading_promoted = True
                elif "heading 2" in sname:
                    parts.append(f"## {text}")
                    heading_promoted = True
                elif "heading 3" in sname:
                    parts.append(f"### {text}")
                else:
                    # 首行短段落（≤80字）且还没有 H1 → 视为文书标题
                    if not heading_promoted and len(text) <= 80:
                        parts.append(f"# {text}")
                        heading_promoted = True
                    else:
                        parts.append(text)
            elif tag == 'tbl':
                tbl = Table(block, doc)
                for i, row in enumerate(tbl.rows):
                    cells = [c.text.strip().replace('\n', ' ') for c in row.cells]
                    parts.append("| " + " | ".join(cells) + " |")
                    if i == 0:
                        parts.append("|" + "|".join(["---"] * len(cells)) + "|")
        return "\n\n".join(parts)
    except Exception:
        return ""


def _excel_to_markdown(file_path: Path, max_rows: int = 60) -> str:
    """Excel → Markdown 表格（每个 sheet 一节）。"""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(file_path), read_only=True, data_only=True)
        parts = []
        for ws in list(wb.worksheets)[:3]:
            parts.append(f"## {ws.title}")
            rows_data = []
            for row in ws.iter_rows(values_only=True, max_row=max_rows):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows_data.append(cells)
            if rows_data:
                parts.append("| " + " | ".join(rows_data[0]) + " |")
                parts.append("|" + "|".join(["---"] * len(rows_data[0])) + "|")
                for row in rows_data[1:]:
                    parts.append("| " + " | ".join(row) + " |")
        return "\n\n".join(parts)
    except Exception:
        return ""


def extract_markdown(file_path: Path, max_pages: int = 5, max_chars: int = 8000,
                     tail_pages: int = 2) -> str:
    """文件 → 结构化 Markdown（统一预处理入口）。
    文字版 PDF：pymupdf4llm，提取首 max_pages 页 + 末 tail_pages 页（签名/盖章在尾页）。
    扫描版 PDF：rapidocr → _text_to_markdown（尾页 OCR 耗时，暂仅提取首页）。
    DOCX：保留 Heading 层级与表格；Excel：Markdown 表格；图片：OCR → Markdown。
    全程本地，不上传。
    """
    ext = file_path.suffix.lower()
    md = ""
    try:
        if ext == ".pdf":
            try:
                import pymupdf4llm, fitz as _fitz
                doc_info = _fitz.open(str(file_path))
                total = len(doc_info)
                doc_info.close()
                head_list = list(range(min(max_pages, total)))
                tail_start = max(max_pages, total - tail_pages)
                tail_list = list(range(tail_start, total)) if tail_pages > 0 else []
                pages = sorted(set(head_list + tail_list))
                md = pymupdf4llm.to_markdown(str(file_path), pages=pages)
            except Exception:
                md = ""
            # pymupdf4llm 返回空 → 扫描版 PDF，走 OCR（仅首页，尾页 OCR 成本高）
            if not md.strip():
                raw = _ocr_pdf_scanned(file_path, max_pages=max_pages)
                md = _text_to_markdown(raw)

        elif ext == ".docx":
            md = _docx_to_markdown(file_path)
            if not md.strip():
                raw = _pdf_extract_text(file_path, max_pages=max_pages)
                md = _text_to_markdown(raw)

        elif ext == ".doc":
            # 老版 Word：用 LibreOffice 转 docx 后再提取
            try:
                import subprocess, tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    subprocess.run(
                        ["soffice", "--headless", "--convert-to", "docx",
                         "--outdir", tmp, str(file_path)],
                        capture_output=True, timeout=60,
                    )
                    converted = Path(tmp) / (file_path.stem + ".docx")
                    if converted.exists():
                        md = _docx_to_markdown(converted)
            except Exception:
                pass
            if not md.strip():
                # 最终兜底：antiword 或 strings 提取纯文本
                try:
                    r = subprocess.run(["antiword", str(file_path)],
                                       capture_output=True, timeout=30)
                    md = _text_to_markdown(r.stdout.decode("utf-8", errors="ignore"))
                except Exception:
                    pass

        elif ext in (".xlsx", ".xls"):
            md = _excel_to_markdown(file_path)

        elif ext in (".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".heic", ".heif"):
            raw = _ocr_image(str(file_path))
            md = _text_to_markdown(raw)

        elif ext == ".md":
            md = file_path.read_text(encoding="utf-8", errors="ignore")

        elif ext in (".txt", ".csv"):
            md = _text_to_markdown(file_path.read_text(encoding="utf-8", errors="ignore"))

    except Exception:
        pass

    # 智能截断：超出上限时保留首尾，防止尾页签名/盖章被截掉
    if len(md) > max_chars:
        tail_keep = min(3000, max_chars // 5)
        head_keep = max_chars - tail_keep - 20
        return md[:head_keep] + "\n…（中间内容略）…\n" + md[-tail_keep:]
    return md


def extract_title_from_markdown(md: str) -> str:
    """从 Markdown 中取文书标题（第一个 H1 或 H2）。"""
    for line in (md or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s.startswith("## "):
            return s[3:].strip()
    return ""

# ============================================================
# 智能命名引擎
# ============================================================
class SmartNamer:
    """
    遵循中伦工作指引命名规范：
    格式：YYMMDD 案件简称 文书类型 vX X
    """

    # 案号模式（仅用于内容识别，不放入文件名）
    CASE_NUM_PATTERNS = [
        re.compile(r'[（(]\s*(\d{4})\s*[）)]\s*([一-鿿]{1,6})\s*(\d+)\s*[一-鿿]{1,4}\s*(\d+)\s*号'),
        re.compile(r'(\d{4,})\s*号'),
    ]

    # 文书标题识别模式 — 按程序类型分组
    TITLE_PATTERNS = [
        # ── 民事诉讼文书 ──
        re.compile(r'(民事起诉状|民事反诉状|民事答辩状|民事上诉状|民事再审申请书)'),
        re.compile(r'(行政起诉状|行政上诉状|行政答辩状|行政复议申请书)'),
        re.compile(r'(代理词|辩护词|质证意见|证据目录|证据清单|证据说明)'),
        re.compile(r'(裁定书|判决书|调解书|决定书|通知书|传票|裁决书)'),
        re.compile(r'(法律意见书|尽职调查报告|法律备忘录|备忘录)'),
        re.compile(r'(管辖权异议申请书|财产保全申请书|证据保全申请书|先予执行申请书)'),
        re.compile(r'(鉴定申请书|回避申请书|撤诉申请书|延期举证申请书)'),
        re.compile(r'(追加当事人申请书|证人出庭申请书|调查令申请书)'),
        re.compile(r'(执行申请书|执行异议申请书|案外人执行异议申请书)'),
        re.compile(r'(再审申请书|申诉书)'),
        # ── 仲裁文书 ──
        re.compile(r'(仲裁申请书|仲裁请求书|仲裁答辩书|仲裁反请求申请书)'),
        re.compile(r'(仲裁裁决书|部分裁决书|终局裁决书|同意裁决书)'),
        re.compile(r'(撤裁申请书|承认执行仲裁裁决申请书|临时措施申请书)'),
        re.compile(r'(Statement of Claim|Statement of Defense|Notice of Arbitration|Request for Arbitration)',
                   re.IGNORECASE),
        re.compile(r'(Procedural Order|Terms of Reference|Witness Statement|Expert Report)',
                   re.IGNORECASE),
        re.compile(r'(Memorial|Counter-Memorial|Reply Memorial|Rejoinder)', re.IGNORECASE),
        re.compile(r'(Partial Award|Final Award|Consent Award|Interim Award)', re.IGNORECASE),
        # ── 刑事程序文书 ──
        re.compile(r'(刑事报案书|刑事控告书|举报书|报案证明)'),
        re.compile(r'(立案决定书|不予立案通知书|立案告知书)'),
        re.compile(r'(拘留通知书|逮捕通知书|逮捕决定书|逮捕证)'),
        re.compile(r'(取保候审申请书|取保候审决定书|羁押必要性审查申请书|变更强制措施申请书)'),
        re.compile(r'(搜查令|搜查证|扣押通知书|扣押决定书|冻结通知书|冻结决定书)'),
        re.compile(r'(起诉书|补充起诉书|变更起诉书|不起诉决定书)'),
        re.compile(r'(辩护意见书|无罪辩护意见书|量刑意见书)'),
        re.compile(r'(刑事上诉状|刑事抗诉书|刑事申诉书)'),
        re.compile(r'(减刑申请书|假释申请书|暂予监外执行申请书)'),
        re.compile(r'(非法证据排除申请书|刑事附带民事起诉状)'),
        # ── 合同协议 ──
        re.compile(r'(购销合同|采购合同|销售合同|供货合同|买卖合同)'),
        re.compile(r'(服务合同|服务协议|劳务合同|委托合同|承揽合同)'),
        re.compile(r'(保密协议|不竞争协议|劳动合同|租赁合同|租赁协议)'),
        re.compile(r'(合作协议|框架协议|补充协议|变更协议|股权转让协议|投资协议)'),
        # ── 函件 ──
        re.compile(r'(律师函|催告函|告知函|警告函|声明函|催款函|索赔函)'),
        re.compile(r'(确认函|询问函|回复函|通知函|邀请函|商务函)'),
        # ── 报告/证明 ──
        re.compile(r'(司法鉴定意见书|专家意见书|技术分析报告|鉴定报告|检测报告|检验报告)'),
        re.compile(r'(工作报告|调研报告|可行性报告|评估报告|审计报告)'),
        re.compile(r'(证明书|证明函|在职证明|公证书|认证书)'),
        re.compile(r'(会议纪要|会议记录|议事录)'),
        # ── 财务 ──
        re.compile(r'(增值税发票|专用发票|普通发票|形式发票|商业发票)'),
        re.compile(r'(报销单|对账单|结算单|工时记录|账单)'),
        # ── 目录/清单 ──
        re.compile(r'(材料目录|文件清单|资料清单|附件清单|证据目录)'),
    ]

    # 美国诉讼 PDF 的首页通常先出现法院抬头、律所地址和案号，真正的文书
    # 标题可能在数十行之后。这里只识别“文种行”，不把法院名称或律所名称
    # 当作标题。匹配范围保持在前 4,000 字，避免正文中引用其他文书时误命中。
    US_TITLE_PATTERNS = [
        re.compile(r"\b(?:FIRST|SECOND|THIRD|AMENDED|VERIFIED|CIVIL|CLASS ACTION|"
                   r"CONSOLIDATED|SUPPLEMENTAL){0,3}\s*COMPLAINT\b", re.IGNORECASE),
        re.compile(r"\bANSWER(?:\s+TO\s+(?:THE\s+)?COMPLAINT)?\b", re.IGNORECASE),
        re.compile(r"\bAFFIRMATIVE DEFENSES\b|\bCOUNTERCLAIMS?\b|\bCROSS-CLAIMS?\b",
                   re.IGNORECASE),
        re.compile(r"\bMOTION\s+(?:TO|FOR|IN)\b|\bNOTICE\s+OF\s+MOTION\b",
                   re.IGNORECASE),
        re.compile(r"\b(?:MEMORANDUM|BRIEF)\s+(?:OF\s+LAW\s+)?(?:IN\s+SUPPORT|"
                   r"IN\s+OPPOSITION|IN\s+REPLY)\b", re.IGNORECASE),
        re.compile(r"\b(?:OPPOSITION|RESPONSE)\s+TO\s+.+MOTION\b|"
                   r"\bREPLY\s+(?:IN\s+SUPPORT|BRIEF|MEMORANDUM)\b", re.IGNORECASE),
        re.compile(r"^(?:PROPOSED\s+)?(?:ORDER|JUDGMENT|VERDICT)(?:\b|\s)",
                   re.IGNORECASE),
        re.compile(r"\bNOTICE\s+OF\s+APPEAL\b|\bAPPELLATE\s+BRIEF\b",
                   re.IGNORECASE),
        re.compile(r"^(?:EXHIBIT|DECLARATION|AFFIDAVIT|DEPOSITION\s+TRANSCRIPT)\b",
                   re.IGNORECASE),
        re.compile(r"\b(?:INITIAL\s+DISCLOSURES|INTERROGATORIES|REQUESTS?\s+FOR\s+"
                   r"PRODUCTION|REQUESTS?\s+FOR\s+ADMISSION|PRIVILEGE\s+LOG)\b",
                   re.IGNORECASE),
    ]
    US_BOILERPLATE = re.compile(
        r"^(?:IN\s+THE\s+)?UNITED\s+STATES\s+DISTRICT\s+COURT\b|"
        r"^UNITED\s+STATES\s+COURT\s+OF\s+APPEALS\b|"
        r"^SUPERIOR\s+COURT\b|^SUPREME\s+COURT\b|"
        r"^CASE\s+(?:NO\.?|NUMBER)\b|^CIVIL\s+ACTION\s+(?:NO\.?|NUMBER)\b",
        re.IGNORECASE,
    )

    @classmethod
    def extract_us_title(cls, text: str) -> Optional[str]:
        """Return the strongest substantive US-litigation title line."""
        candidates: list[tuple[int, int, str]] = []
        for index, raw in enumerate((text or "")[:4000].splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            was_emphasized = raw.startswith(("#", "**", "__"))
            line = re.sub(r"^[#>*_\s]+|[#>*_\s]+$", "", raw)
            line = re.sub(r"\s+", " ", line).strip(" |:-")
            # PDF running footer, e.g. "ANSWER TO COMPLAINT 2. (No. CV...)".
            line = re.sub(r"\s+\d+\.?\s*\((?:Case\s+)?No\..*$", "", line,
                          flags=re.IGNORECASE).strip()
            if not 3 <= len(line) <= 190 or cls.US_BOILERPLATE.search(line):
                continue
            if re.search(r"\bCase\s+(?:No\.?|Number)\b|\bDocument\s+\d+\b", line,
                         re.IGNORECASE):
                continue
            matches = [pat for pat in cls.US_TITLE_PATTERNS if pat.search(line)]
            if not matches:
                continue
            letters = [ch for ch in line if ch.isalpha()]
            upper_ratio = (sum(ch.isupper() for ch in letters) / len(letters)) if letters else 0
            starts_with_type = bool(re.match(
                r"^(?:PLAINTIFFS?'?|DEFENDANTS?'?|PETITIONERS?'?|RESPONDENTS?'?)?\s*"
                r"(?:FIRST\s+|SECOND\s+|AMENDED\s+|VERIFIED\s+|CIVIL\s+)*"
                r"(?:COMPLAINT|ANSWER|MOTION|NOTICE|ORDER|JUDGMENT|VERDICT|EXHIBIT|"
                r"DECLARATION|AFFIDAVIT|MEMORANDUM|BRIEF|OPPOSITION|REPLY|"
                r"INTERROGATORIES|REQUESTS?)\b", line, re.IGNORECASE))
            # Bold/all-caps and type-leading lines are much more likely to be
            # document titles than a sentence in the pleading body.
            score = len(matches) * 35 + (35 if was_emphasized else 0)
            score += 30 if upper_ratio >= 0.65 else 0
            score += 25 if starts_with_type else 0
            score -= min(index, 80) // 8
            candidates.append((score, -index, line))
        if not candidates:
            return None
        title = max(candidates)[2]
        return title[:180].rstrip(" .,:;-")

    @classmethod
    def extract_title(cls, text: str) -> Optional[str]:
        """从文本开头提取中、美及仲裁文书的实质标题。"""
        if not text:
            return None
        head = text[:1200]
        for pat in cls.TITLE_PATTERNS:
            m = pat.search(head)
            if m:
                return m.group(1)
        return cls.extract_us_title(text)

    @classmethod
    def extract_heading(cls, text: str, max_len: int = 45) -> Optional[str]:
        """从文本头部提取首个有效短行作为标题兜底"""
        if not text:
            return None
        # 跳过：纯数字/日期行、以及"案号/文号/委托书号"等元信息行（不是文书标题）
        meta_label = re.compile(r'(案号|案件编号|文号|委托书号|鉴定委托书号|卷宗号|编号)\s*[：:]')
        doc_number = re.compile(r'[（(]\s*\d{4}\s*[）)].{0,10}\d+\s*号')
        for line in text[:1000].splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r'^[\d\s\-/:.年月日（）()]+$', line):
                continue
            if meta_label.search(line) or doc_number.search(line):
                continue
            if 2 <= len(line) <= max_len:
                return line
        return None

    @classmethod
    def extract_version(cls, filename: str) -> Optional[str]:
        """
        从原始文件名提取版本号，规范化为中伦格式（空格分隔，无点）。
        支持识别：v0 1, v0.1, v0_1, v1 0marked, vfinal, 盖章版, 签署版
        """
        stem = Path(filename).stem
        # 盖章版/签署版 → 提交版，映射为 v1 0
        if re.search(r'(盖章版|签署版|盖章|正式签署)', stem):
            return "v1 0"
        # 中伦格式：v0 1, v1 0, v0 1 marked, v0 1 clean, vfinal
        m = re.search(r'\b(vfinal|v\d+[\s_]\d+(?:\s*(?:marked|clean))?)\b', stem, re.IGNORECASE)
        if m:
            return m.group(1).lower().replace('_', ' ')
        # 带点格式：v0.1, v1.0 → 转为 v0 1, v1 0
        m = re.search(r'\b[vV](\d+)\.(\d+)(marked|clean)?\b', stem)
        if m:
            suffix = f" {m.group(3).lower()}" if m.group(3) else ""
            return f"v{m.group(1)} {m.group(2)}{suffix}"
        # 单数字：v1, v2 → v1 0
        m = re.search(r'\b[vV](\d+)\b', stem)
        if m:
            return f"v{m.group(1)} 0"
        return None

    # ── 命名拆成两层：① 抽取结构化字段 ② 套模板格式化 ──────────────
    @classmethod
    def extract_naming_fields(
        cls,
        content_text: str,
        filename: str,
        mtime: Optional[datetime] = None,
        doc_type: Optional[str] = None,
        rename_pattern: Optional[str] = None,
        target: str = "",
        force_version: Optional[str] = None,
        party: Optional[str] = None,
        batch: Optional[int] = None,
        translation: bool = False,
        source: Optional[str] = None,
    ) -> dict:
        """
        抽取层：产出命名所需的结构化字段 {date, party, doc_type, batch, version}。
        来源不限——人工预填、关键词目录、未来的 LLM 都可以填这些字段。

        文书类型优先级：
          手工 doc_type  >  目录规范名 rename_pattern  >  OCR标题  >  文件名兜底
        把"目录规范名"放在"OCR标题"之前——命中规则即用统一规范名（如"起诉状"），
        避免把案号/抬头行误当标题（这是"案号当文件名"那类问题的根因）。

        party  = 诉讼主体缩写（手工选，如"示例公司"），auto 模式不臆测、留空。
        batch  = 组次（多次提交的证据等），2→（二）3→（三）…
        """
        date_dt = mtime or datetime.now()

        # 文书类型按"置信度分级"取值（dt_conf 越高越可靠）：
        #   手工(1.0) > 规范文书标题(0.9,命中法定文书模式,如"对方有律师"的规范文书)
        #   > 目录规范名(0.7,命中关键词规则) > 首行兜底(0.4,易混入案号已过滤) > 文件名(0.2)
        if doc_type:
            dtype, dt_conf, dt_src = doc_type, 1.0, "manual"
        else:
            title = cls.extract_title(content_text)          # 高置信：命中法定文书标题模式
            heading = None if title else cls.extract_heading(content_text)
            if title:
                dtype, dt_conf, dt_src = title, 0.9, "doc_title"
            elif rename_pattern:
                dtype, dt_conf, dt_src = rename_pattern, 0.7, "catalog"
            elif heading:
                dtype, dt_conf, dt_src = heading, 0.4, "heading"
            else:
                stem = Path(filename).stem
                clean = re.sub(r'^\d{6,8}[\s\-_]*', '', stem)
                clean = re.sub(r'[\s\-_]*v\S+\s*$', '', clean, flags=re.IGNORECASE).strip()
                dtype = clean[:20] if clean else "文档"
                dt_conf, dt_src = 0.2, "filename"

        # 版本：仅我方材料需区分（对方/法院/仲裁文件不加版本）
        ver = force_version or cls.extract_version(filename)
        if source in ("opposing", "court"):
            ver = None
        elif not ver:
            if "过程稿" in target or "草稿" in target:
                ver = "v0 1"
            elif "提交版" in target:
                ver = "v1 0"
            elif "内部定稿" in target:
                ver = "v0 9"
        return {"date": date_dt, "party": party or None,
                "doc_type": dtype, "batch": batch, "version": ver,
                "translation": bool(translation),
                "dt_conf": dt_conf, "dt_src": dt_src}

    @staticmethod
    def format_filename(case_key: Optional[str], fields: dict, ext: str) -> str:
        """
        格式化层：纯模板、确定性、可单测。
        格式：YYMMDD 案件简称 主体 文书类型（组次） vX X.ext（中伦工作指引）
        """
        parts = [fields["date"].strftime("%y%m%d")]
        if case_key:
            parts.append(case_key)
        if fields.get("party"):
            parts.append(fields["party"])
        if fields.get("doc_type"):
            tok = fields["doc_type"] + _batch_suffix(fields.get("batch"))
            if fields.get("translation"):
                tok += "（译）"
            parts.append(tok)
        if fields.get("version"):
            parts.append(fields["version"])
        name = " ".join(filter(None, parts)) + ext
        name = re.sub(r' {2,}', ' ', name)
        name = re.sub(r'[\\/:*?"<>|：]', '', name)   # 去非法字符（含全角冒号，保留中文括号）
        return name

    @classmethod
    def build_name(
        cls,
        content_text: str,
        filename: str,
        case_key: Optional[str],
        ext: str,
        mtime: Optional[datetime] = None,
        doc_type: Optional[str] = None,
        rename_pattern: Optional[str] = None,
        target: str = "",
        force_version: Optional[str] = None,
        party: Optional[str] = None,
        batch: Optional[int] = None,
        translation: bool = False,
    ) -> str:
        """兼容旧接口 = 抽取层 + 格式化层 的组合。"""
        fields = cls.extract_naming_fields(
            content_text, filename, mtime=mtime, doc_type=doc_type,
            rename_pattern=rename_pattern, target=target, force_version=force_version,
            party=party, batch=batch, translation=translation,
        )
        return cls.format_filename(case_key, fields, ext)

    @classmethod
    def is_significant_filename(cls, filename: str) -> bool:
        stem = Path(filename).stem.lower()
        meaningless = {
            'img', 'image', 'dsc', 'photo', 'pic', 'screenshot', '截图', '微信图片',
            'document', 'doc', '新建', '未命名', 'untitled', 'new', 'scan', '扫描',
            'fax', 'page', 'export', 'download',
        }
        if re.match(r'^[\d\-_\s]+$', stem):
            return False
        for mw in meaningless:
            if stem.startswith(mw) or stem == mw:
                return False
        return True

# ============================================================
# 匹配引擎
# ============================================================
def score_case_candidates(filename: str, content_text: str, cases: list) -> list:
    """生成可解释的案件候选评分；低分或接近分数不自动归档。"""
    filename_l = (filename or "").lower()
    text = content_text or ""
    combined = (filename + " " + text).lower()
    header = text[:1800].lower()
    combined_nospace = re.sub(r'[\s　\-—_/]', '', combined)
    candidates = []

    for idx, case in enumerate(cases):
        if case.get("status") == "closed":
            continue
        score, reasons = 0, []
        numbers = case.get("case_number", "")
        numbers = list(numbers.values()) if isinstance(numbers, dict) else ([numbers] if numbers else [])
        for value in numbers:
            norm = re.sub(r'[\s　\-—_/]', '', str(value).lower())
            if norm and norm in combined_nospace:
                score += 10000
                reasons.append(f"案号命中：{value}")
                break

        parties = []
        for key in ("party_us", "party_op", "party_third", "party_court"):
            value = case.get(key, [])
            parties.extend(value if isinstance(value, list) else [value])
        seen = set()
        for value in parties:
            term = str(value or "").strip()
            tl = term.lower()
            if len(term) < 2 or tl in seen:
                continue
            seen.add(tl)
            if tl in filename_l:
                score += min(180, 70 + len(term) * 4)
                reasons.append(f"文件名主体：{term}")
            elif tl in header:
                score += min(140, 45 + len(term) * 3)
                reasons.append(f"首页主体：{term}")
            elif tl in combined:
                score += min(45, 8 + len(term))
                reasons.append(f"正文提及：{term}")

        for value in case.get("keywords", []) or []:
            term = str(value or "").strip()
            tl = term.lower()
            if len(term) < 2:
                continue
            if tl in filename_l:
                score += min(90, 35 + len(term) * 2)
                reasons.append(f"文件名关键词：{term}")
            elif tl in header:
                score += min(60, 25 + len(term))
                reasons.append(f"首页关键词：{term}")
            elif tl in combined:
                score += min(20, 4 + len(term) // 2)
                reasons.append(f"正文关键词：{term}")

        court_terms = []
        for key in ("court_first", "court_second", "party_court",
                    "court_first_aliases", "court_second_aliases"):
            value = case.get(key, "")
            court_terms.extend(value if isinstance(value, list) else [value])
        if any(str(value or "").lower() in header for value in court_terms
               if len(str(value or "")) >= 3):
            score += 35
            reasons.append("首页法院/仲裁机构")

        if score:
            candidates.append({"index": idx, "key": case.get("key"),
                               "name": case.get("name"), "score": score,
                               "reasons": reasons})
    return sorted(candidates, key=lambda x: (-x["score"], x["index"]))


def match_case(filename: str, content_text: str, cases: list) -> Tuple[Optional[str], Optional[str]]:
    """纯规则案件匹配：案号确定；其余情况必须达到分数和领先差距阈值。"""
    candidates = score_case_candidates(filename, content_text, cases)
    if not candidates:
        return None, None
    top = candidates[0]
    second = candidates[1] if len(candidates) > 1 else None
    if top["score"] < 60 or (second and top["score"] < 10000
                              and top["score"] - second["score"] < 20):
        return None, None
    return top["key"], top["name"]


SOURCE_LABELS = {
    "ours": "我方", "opposing": "对方", "court": "法院",
    "tribunal": "仲裁机构", "third_party": "第三方",
    "expert": "专家/鉴定机构", "police": "公安机关",
    "prosecutor": "检察机关", "regulator": "监管机构",
    "internal": "内部材料",
}

# 落款日期：YYYY年M月D日 / YYYY.M.D / YYYY/M/D / YYYYMMDD
_SIG_DATE_RE = re.compile(
    r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?'
    r'|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}'
)

# 页码行匹配：纯数字行，可选前后破折号/空格，如 "3" / "- 3 -" / "第3页"
_PAGENUM_RE = re.compile(r'^[\-\s—]*(?:第\s*)?(\d{1,4})(?:\s*页)?[\-\s—]*$')

# 落款提交角色词：出现即说明这是提交方的签名栏
_SIG_ROLE_RE = re.compile(
    r'提交人|举证人|上诉人|申请人|再审申请人|原告|被告'
    r'|诉讼代理人|委托代理人|代理律师|代理人|具状人|作成人'
)

# 法院落款专属词：只可能出现在法院发文的署名栏
_COURT_SIG_RE = re.compile(r'审判长|审判员|书记员|仲裁员|首席仲裁员|合议庭成员')


def _detect_source_from_signature(tail: str, case_cfg: dict) -> Optional[str]:
    """
    落款块识别：在文末约 800 字中，逐行扫描：
      A) 出现审判长/书记员等法院专属落款词 + 附近有日期 → court
      B) 出现提交角色词（上诉人/举证人/…）+ 同行或紧邻 2 行内有当事人名 + 附近有日期 → ours/opposing

    设计原则：
    - 不用法院名称判断来源（"此致 XX法院"是收文方，不是发文方）
    - 角色词 + 当事人名须在 2 行内共现，防止正文抬头里列出双方当事人时误触发
    - 日期窗口放宽到 4 行，因为落款日期常与具名分行书写
    """
    if not tail:
        return None

    lines = [l.strip() for l in tail.splitlines() if l.strip()]
    if not lines:
        return None

    party_us = [p for p in (case_cfg.get("party_us") or []) if p]
    party_op = [p for p in (case_cfg.get("party_op") or []) if p]

    DATE_WIN = 4   # 日期在锚定行前后 4 行内
    NAME_WIN = 1   # 当事人名在角色词行紧邻 1 行内（防封面列出双方后误触发）

    def _is_sig_date_line(ln: str) -> bool:
        """独立落款日期行：日期以外的非空白字符 < 8 个。
        区分"2025年5月22日"（落款）和"本院定于2024年3月19日开庭"（正文嵌入）。"""
        m = _SIG_DATE_RE.search(ln)
        if not m:
            return False
        rest = re.sub(r'[\s　\-—_。，、()（）]+', '', ln[:m.start()] + ln[m.end():])
        return len(rest) < 8

    def _has_sig_date(ctx_lines: list) -> bool:
        return any(_is_sig_date_line(l) for l in ctx_lines)

    for i, line in enumerate(lines):
        # ── A. 法院专属落款词（审判长/书记员等）──────────────────────
        if _COURT_SIG_RE.search(line):
            if _has_sig_date(lines[max(0, i - DATE_WIN): i + DATE_WIN + 1]):
                return "court"
            continue

        # ── B. 提交角色词（上诉人/举证人/原告/被告/代理人…）─────────
        if not _SIG_ROLE_RE.search(line):
            continue

        # 当事人名须在角色词行紧邻 2 行内（本行 ± 2），而非整个窗口
        name_ctx = lines[max(0, i - NAME_WIN): i + NAME_WIN + 1]
        name_txt = "\n".join(name_ctx)

        # 日期须是独立落款日期行（不是正文嵌入日期）
        if not _has_sig_date(lines[max(0, i - DATE_WIN): i + DATE_WIN + 1]):
            continue

        us_present = any(p in name_txt for p in party_us)
        op_present = any(p in name_txt for p in party_op)

        # 双方都出现 → 是正文抬头双方列表，不是签名块，跳过
        if us_present and op_present:
            continue

        if us_present:
            return "ours"
        if op_present:
            return "opposing"

    return None


def detect_source(content_text: str, case_cfg: dict) -> Optional[str]:
    """
    判断文件来源：法院 / 我方 / 对方。
    优先级：① 落款块识别（最高可靠）→ ② 文首/文尾 court 自称词 → ③ 文首申请方位置
    """
    if not content_text:
        return None
    t = content_text

    # ① 落款块识别（文末约 800 字）
    sig = _detect_source_from_signature(t[-800:], case_cfg)
    if sig:
        return sig

    # ① 落款块识别（文首约 800 字）：证据汇编签名页在首页时适用；短文件头尾重叠不重复扫
    if len(t) > 800:
        sig = _detect_source_from_signature(t[:800], case_cfg)
        if sig:
            return sig

    # ② 法院自称词：只看文首 300 字 + 文尾 300 字，避免正文引用"本院"误判
    #    当事人起草文书时常引用"（原）本院认为…"，不能全文扫描
    court_markers = ["本院", "本庭", "本仲裁庭", "本委员会", "审判长", "审判员",
                     "书记员", "合议庭", "仲裁庭认为", "如不服本"]
    zone = t[:300] + t[-300:]
    if any(m in zone for m in court_markers):
        return "court"
    # 纯规则识别法院主动发文：通知/传票/判决/裁定等文种与法院机关词在首页共现时，
    # 即使没有“本院/审判员”落款，也可确定为法院来源；普通代理文书仅提及法院不会满足。
    court_doc = re.search(r'开庭通知|开庭传票|传票|判决书|裁定书|调解书|受理通知|立案通知|缴费通知', t[:500])
    if court_doc and re.search(r'(?:人民法院|法院|仲裁委员会|仲裁庭)', t[:500]):
        return "court"
    head_title = t[:40]
    for p in (case_cfg.get("party_court") or []):
        if p and p in head_title:
            return "court"

    # ③ 文首申请方位置（原告/申请人/上诉人槽位）
    party_us = [p for p in (case_cfg.get("party_us") or []) if p]
    party_op = [p for p in (case_cfg.get("party_op") or []) if p]
    if not party_us and not party_op:
        return None

    head = t[:1500]
    claimant_roles = ["原告", "申请人", "上诉人", "再审申请人", "申请执行人",
                      "claimant", "petitioner", "plaintiff"]

    def in_claimant_slot(names: list) -> bool:
        for role in claimant_roles:
            for m in re.finditer(r"(?<!被)" + re.escape(role) + r"[：:\s]{0,3}([^\s　，,；;。：:、（(]{1,25})",
                                  head, re.IGNORECASE):
                if any(n.lower() in m.group(1).lower() for n in names):
                    return True
        return False

    us_c = in_claimant_slot(party_us) if party_us else False
    op_c = in_claimant_slot(party_op) if party_op else False
    if us_c and not op_c:
        return "ours"
    if op_c and not us_c:
        return "opposing"
    return None


def _detect_source_from_sigpage(file_path: Path, case_cfg: dict) -> Optional[str]:
    """
    页码序列定位签字末页（④ 层兜底）。

    策略：逐页读取 PDF 末行，找到最长连续递增整数页码序列（如 1-2-3-…-N）；
    该序列最后一页通常是主体文书的提交签字页（后附证据材料另有编页）。
    提取该页全文，送入 _detect_source_from_signature 识别来源。

    触发场景：大型证据汇编 PDF，签名页位于主文末尾（非文件首/尾 800 字内），
    被后续证据材料"淹没"导致 detect_source 文首/文尾扫描失效。
    """
    if not file_path or file_path.suffix.lower() != ".pdf":
        return None
    try:
        import fitz
        doc = fitz.open(str(file_path))
        total = len(doc)
        if total < 4:          # 短文件已由 head/tail 覆盖
            doc.close()
            return None

        best_len = 0
        best_text = None
        cur_len = 0
        cur_expected = 1
        cur_text = None

        scan_limit = min(total, 60)  # 最多扫前 60 页，避免超大文件耗时
        for i in range(scan_limit):
            page = doc[i]
            text = page.get_text()
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if not lines:
                continue
            m = _PAGENUM_RE.match(lines[-1])
            if m:
                num = int(m.group(1))
                if num == cur_expected:
                    cur_len += 1
                    cur_expected += 1
                    cur_text = text
                    if cur_len > best_len:
                        best_len = cur_len
                        best_text = cur_text
                elif num == 1:
                    # 新序列开始（换一份附件）
                    cur_len = 1
                    cur_expected = 2
                    cur_text = text
                else:
                    cur_len = 0
                    cur_expected = 1
                    cur_text = None
            else:
                # 无页码行 → 序列打断
                if cur_len > best_len:
                    best_len = cur_len
                    best_text = cur_text
                cur_len = 0
                cur_expected = 1
                cur_text = None

        doc.close()
        if best_len >= 2 and best_text:
            return _detect_source_from_signature(best_text, case_cfg)
    except Exception:
        pass
    return None


def _source_from_filename(filename: str, case_cfg: dict) -> Optional[str]:
    """
    文件名来源兜底：当内容为空（OCR失败/扫描件）时，从文件名本身的当事人全名推断来源。
    仅当文件名里只有一方当事人出现时才下判断；双方都有、或文件名包含案件标题格式 → None。
    注意：只匹配 party_us/op 中的全名（缩写无法可靠区分，不做猜测）。
    """
    stem = Path(filename).stem

    # 案件标题格式（"XX v. YY" / "XX v YY" / "XX vs YY"）→ 无法从文件名判断来源
    case_key = (case_cfg or {}).get("key", "")
    if case_key and case_key in stem:
        return None
    if re.search(r'\bvs?\.?\s', stem, re.IGNORECASE):
        return None

    party_us    = [p for p in (case_cfg.get("party_us")    or []) if p]
    party_op    = [p for p in (case_cfg.get("party_op")    or []) if p]
    party_court = [p for p in (case_cfg.get("party_court") or []) if p]

    us_hit    = any(p in stem for p in party_us)
    op_hit    = any(p in stem for p in party_op)
    court_hit = any(p in stem for p in party_court)

    if court_hit and not us_hit and not op_hit:
        return "court"
    if us_hit and not op_hit:
        return "ours"
    if op_hit and not us_hit:
        return "opposing"
    return None


# 案件场景 → 允许匹配的规则场景集合（含"通用"共享类）
_SCENARIO_ALLOW = {
    "中国民事": {"中国民事", "通用"},
    "仲裁":     {"仲裁", "通用"},
    "中国刑事": {"中国刑事", "通用"},
    "美国民事": {"美国民事", "通用"},
    "刑民交叉": {"中国民事", "中国刑事", "通用"},
}

def _allowed_scenarios(case_cfg: dict):
    """案件已声明场景 → 返回允许的规则场景集合；未声明 → None（不过滤）。"""
    return _SCENARIO_ALLOW.get((case_cfg or {}).get("scenario"))


def _title_key_match(filename: str, rules: list, allowed_sc) -> Optional[dict]:
    """只对文件名 stem 做 title_keywords 精准匹配，用于 LLM 前的高置信度预检。
    与 map_to_target 对齐：取最长匹配关键词对应的规则（越具体越优先），而非顺序首中。"""
    stem = Path(filename).stem.lower()
    best_rule, best_len = None, 0
    for rule in rules:
        if allowed_sc is not None and rule.get("scenario", "通用") not in allowed_sc:
            continue
        for kw in rule.get("title_keywords", []):
            if _keyword_matches(str(kw), stem) and len(kw) > best_len:
                best_len = len(kw)
                best_rule = rule
    return best_rule


import functools

@functools.lru_cache(maxsize=1)
def _load_doctype_map() -> list:
    """加载 doc_type_map.yaml，进程内缓存（文件不变则只读一次）。"""
    p = Path(__file__).parent / "doc_type_map.yaml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return tuple(data.get("mappings", []))  # tuple 使 lru_cache 可哈希
    except Exception:
        return ()


def _doctype_map_lookup(doc_type: str, source: str) -> Optional[str]:
    """doc_type × source → target 路径，未命中返回 None。
    匹配顺序：① 精确（大小写不敏感）② 单向包含（alias ⊆ dt，且 alias 长度 ≥ dt 的 55%）。
    长度保护防止"鉴定意见书"误触发"关于鉴定意见书的异议"——后者应回落 LLM Stage2。
    source 优先级：ours/opposing/court → _default 兜底。"""
    mappings = _load_doctype_map()
    if not mappings or not doc_type:
        return None
    dt = doc_type.strip().lower()
    min_ratio = 0.55  # alias 须达到 dt 长度的 55%，过短的别名不做包含匹配

    def _resolve(entry):
        sm = entry.get("source_map", {})
        return sm.get(source) or sm.get("_default")

    # ① 精确匹配
    for entry in mappings:
        if any(a.strip().lower() == dt for a in entry["aliases"]):
            return _resolve(entry)
    # ② 单向包含匹配：alias 是 dt 的子串，且足够长（避免短别名误触发长描述字符串）
    for entry in mappings:
        if any(
            (al := a.strip().lower()) in dt and len(al) >= len(dt) * min_ratio
            for a in entry["aliases"]
        ):
            return _resolve(entry)
    return None


def match_rule(
    filename: str,
    content_text: str,
    rules: list,
    extracted_title: Optional[str] = None,
    source: Optional[str] = None,
    allowed_scenarios=None,
) -> Optional[dict]:
    """纯规则评分匹配：遍历全部候选，按标题/文件名/正文/来源综合评分。"""
    combined = (filename + " " + content_text).lower()
    title_lower = (extracted_title or "").lower()
    filename_lower = filename.lower()
    ranked = []
    for order, rule in enumerate(rules):
        # 场景硬隔离：跳过不属于本案场景的规则（如 US 案件跳过中文诉讼规则）
        if allowed_scenarios is not None:
            if rule.get("scenario", "通用") not in allowed_scenarios:
                continue

        score = 0
        reasons = []
        for kw in rule.get("keywords", []):
            k = str(kw).lower().strip()
            if not k:
                continue
            if _keyword_matches(k, filename_lower):
                score += min(70, 25 + len(k) * 2)
                reasons.append(f"文件名关键词：{kw}")
            elif _keyword_matches(k, title_lower):
                score += min(100, 45 + len(k) * 3)
                reasons.append(f"标题关键词：{kw}")
            elif _keyword_matches(k, combined):
                score += min(28, 5 + len(k))
                reasons.append(f"正文关键词：{kw}")

        for kw in rule.get("title_keywords", []):
            k = str(kw).lower().strip()
            if _keyword_matches(k, title_lower):
                score += min(120, 60 + len(k) * 3)
                reasons.append(f"文书标题：{kw}")
            elif _keyword_matches(k, filename_lower):
                score += min(80, 35 + len(k) * 2)
                reasons.append(f"文件名标题：{kw}")

        kw_match = score > 0

        ft = rule.get("file_types")
        if ft and kw_match:
            if Path(filename).suffix.lower() not in [t.lower() for t in ft]:
                kw_match = False

        ck = rule.get("content_keywords")
        if ck and kw_match and content_text:
            matched_ck = [kw for kw in ck if _keyword_matches(str(kw), content_text)]
            if not matched_ck:
                kw_match = False
            else:
                score += min(35, 12 * len(matched_ck))
                reasons.append("正文限定词：" + "、".join(map(str, matched_ck[:3])))

        # 来源过滤（软）：仅当"已确定来源"且"规则声明了来源"且两者不一致时才排除；
        # 来源无法确定（source 为 None）时不阻断，规则照常按关键词匹配。
        rsrc = rule.get("source")
        if rsrc and source and kw_match and rsrc != source:
            kw_match = False

        if kw_match:
            if rsrc and source == rsrc:
                score += 30
                reasons.append(f"来源匹配：{source}")
            ranked.append((score, order, rule, reasons))

    if not ranked:
        return None
    ranked.sort(key=lambda x: (-x[0], x[1]))
    # 将命中依据挂到规则副本上，供日志和前台解释，不改变 YAML 原对象。
    best_score, _order, best_rule, reasons = ranked[0]
    result = dict(best_rule)
    result["_match_score"] = best_score
    result["_match_reasons"] = reasons
    return result

# ============================================================
# 手工预填辅助
# ============================================================
_STAGE_WORDS = {"过程稿", "提交版", "签署版", "定稿", "内部定稿", "草稿", "终稿", "正式版"}

def _doctype_from_target(target: str) -> str:
    """从目标文件夹路径取"文书类型"用于命名，跳过"过程稿/提交版"等阶段段。
    例：04_诉讼文书/起诉状/过程稿 → 起诉状；05_法院来文/开庭通知 → 开庭通知"""
    parts = [p for p in (target or "").split("/") if p]
    for seg in reversed(parts):
        if seg not in _STAGE_WORDS:
            return re.sub(r'^\d+[_-]', '', seg) if len(parts) == 1 else seg
    return parts[-1] if parts else ""

def _parse_recv_date(s: str):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%y%m%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None

def _date_from_filename(filename: str) -> Optional[datetime]:
    """从文件名提取日期：优先 YYYYMMDD，次 YYMMDD；返回 None 表示未找到。"""
    stem = Path(filename).stem
    # YYYYMMDD
    m = re.search(r'(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)', stem)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # YYMMDD — 6 digits, century heuristic: 00-30 → 20xx, 31-99 → 19xx
    m = re.search(r'(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)', stem)
    if m:
        yy = int(m.group(1))
        century = 2000 if yy <= 30 else 1900
        try:
            return datetime(century + yy, int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _pdf_creation_date(path: Path) -> Optional[datetime]:
    """从 PDF metadata 提取创建时间；失败则返回 None。"""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        import fitz
        doc = fitz.open(str(path))
        raw = (doc.metadata or {}).get("creationDate", "") or ""
        doc.close()
        m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", raw)
        if m:
            return datetime(*[int(x) for x in m.groups()])
    except Exception:
        pass
    return None


# 合规文件名特征：含版本标注，无需重命名
_COMPLIANT_VERSION_RE = re.compile(
    r'\b(vfinal|v\d+[\s_\.]\d+(\s*(marked|clean))?)\b', re.IGNORECASE
)

def is_compliant_filename(filename: str) -> bool:
    """文件名已含合规版本标注（vfinal / v0 1 / v1 0 marked / v0 1 clean 等），无需重命名。"""
    stem = Path(filename).stem
    return bool(_COMPLIANT_VERSION_RE.search(stem))


# 送达附随文件关键词：与主文件同归一个文件夹
_DELIVERY_KEYWORDS = [
    "邮寄面单", "ems面单", "sf面单", "顺丰面单", "快递面单", "物流面单",
    "快递单", "邮单", "送达记录", "签收记录", "ems签收", "快递签收",
    "物流记录", "送达凭证", "回执", "收件回执",
]
_DELIVERY_RE = re.compile(
    "|".join(re.escape(k) for k in _DELIVERY_KEYWORDS), re.IGNORECASE
)

def _strip_delivery_suffix(stem: str) -> Optional[str]:
    """从文件名茎中去除送达关键词，返回剩余主体名；不含送达词则返回 None。"""
    if not _DELIVERY_RE.search(stem):
        return None
    # 去掉关键词及其前后的分隔符
    clean = _DELIVERY_RE.sub("", stem)
    clean = re.sub(r'[\s\-_–—]+', ' ', clean).strip(" -_")
    return clean if clean else None

_CN_NUM = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

def _batch_suffix(n) -> str:
    """组次后缀：1/空 → ''，2 → '（二）'，3 → '（三）' … 用于多次提交的证据等。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 1:
        return ""
    if n <= 10:
        return f"（{_CN_NUM[n]}）"
    return f"（{n}）"

def _court_abbr(full: str) -> str:
    """法院/仲裁机构名缩写（进文件名用），与前端 courtAbbr 保持一致。
    例：上海市浦东新区人民法院 → 浦东新区法院"""
    s = re.sub(r'人民法院$', '法院', full or '')
    s = re.sub(r'人民检察院$', '检察院', s)
    s = re.sub(r'^(北京市|上海市|天津市|重庆市)', '', s)
    s = re.sub(r'^.{2,3}省', '', s)
    s = re.sub(r'^.{2,4}(自治区|特别行政区)', '', s)
    return s.strip() or (full or '')

def _naming_candidates(case_cfg: dict, detail_key: str, flat_key: str) -> list:
    """命名主体候选：优先用当事人明细里的显式简称(aliases)，其次正式名(name)，
    绝不用曾用名(former)——避免文件名用上过时的名字。无明细时退回平铺名单。"""
    det = case_cfg.get(detail_key)
    if isinstance(det, list) and det:
        aliases, names = [], []
        for r in det:
            aliases += [a for a in (r.get("aliases") or []) if a]
            if r.get("name"):
                names.append(r["name"])
        pool = aliases or names          # 有简称优先用简称池，否则用正式名池
        if pool:
            return pool
    return [p for p in (case_cfg.get(flat_key) or []) if p]


def _source_label(source: str, case_cfg: dict) -> Optional[str]:
    """由来源选出最短的当事人缩写，用于文件名的'来源'字段。"""
    if not source or source == "unknown":
        return None
    if source == "ours":
        candidates = _naming_candidates(case_cfg, "party_us_detail", "party_us")
        return min(candidates, key=len) if candidates else "我方"
    if source == "opposing":
        candidates = _naming_candidates(case_cfg, "party_op_detail", "party_op")
        return min(candidates, key=len) if candidates else "对方"
    if source == "court":
        candidates = [p for p in (case_cfg.get("party_court") or []) if p]
        return min((_court_abbr(c) for c in candidates), key=len) if candidates else "法院"
    return None

def _party_to_source(party: str, case_cfg: dict):
    """由所选来源主体反推来源：我方→ours，对方→opposing，法院/仲裁机构→court。"""
    if not party:
        return None
    if party in (case_cfg.get("party_us") or []):
        return "ours"
    if party in (case_cfg.get("party_op") or []):
        return "opposing"
    for e in (case_cfg.get("party_court") or []):
        if party == e or party == _court_abbr(e):
            return "court"
    return None

def _file_hash(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _find_duplicate(dest_dir: Path, src: Path):
    """目标文件夹里是否已有"内容完全相同"的文件（不看文件名）。先比大小再比哈希。"""
    if not dest_dir.exists():
        return None
    try:
        src_size = src.stat().st_size
    except OSError:
        return None
    src_hash = None
    for ex in dest_dir.iterdir():
        if ex.is_file() and not ex.name.startswith(".") and ex.stat().st_size == src_size:
            if src_hash is None:
                src_hash = _file_hash(src)
            try:
                if _file_hash(ex) == src_hash:
                    return ex
            except OSError:
                continue
    return None

def _auto_batch(dest_dir: Path, doc_type: str, ext: str) -> Optional[int]:
    """扫描目标文件夹：若已有同类型文件则返回下一组次编号（2、3…），否则返回 None。"""
    if not dest_dir.exists() or not doc_type:
        return None
    count = sum(
        1 for f in dest_dir.iterdir()
        if f.is_file() and not f.name.startswith(".") and doc_type in f.stem
        and f.suffix.lower() == ext.lower()
    )
    return count + 1 if count >= 1 else None

def _append_archive_log(config: dict, entry: dict):
    """追加一条归档映射日志（原名→新名，可追溯/回滚）。失败不影响归档。"""
    try:
        log_dir = Path(config.get("global", {}).get("log_dir") or "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
        with open(log_dir / "archive_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _upload_audit_fields(override: dict) -> dict:
    """Return non-content upload context that should survive sidecar deletion."""
    if not isinstance(override, dict):
        return {}
    return {
        key: override[key]
        for key in ("batch_id", "original_relative_path", "context_path", "stage")
        if override.get(key)
    }


def _remove_created_links(metadata: Optional[dict], destination: Path) -> None:
    """Remove only newly-created links that still point at this destination."""
    if not isinstance(metadata, dict):
        return
    links = metadata.get("links", [])
    if not isinstance(links, list):
        return
    try:
        destination_resolved = Path(destination).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return

    for raw_link in links:
        if not isinstance(raw_link, str) or not raw_link:
            continue
        try:
            link = Path(raw_link).expanduser()
            if not link.is_absolute() or not link.is_symlink():
                continue
            if link.resolve(strict=True) != destination_resolved:
                continue
            os.unlink(str(link))
        except (OSError, RuntimeError, ValueError):
            continue


def _record_ledger(config: dict, source: Path, destination: Path, *, action: str,
                   metadata: Optional[dict] = None) -> Optional[str]:
    """Record a completed move; roll it back if the ledger cannot commit."""
    try:
        log_dir = Path(config.get("global", {}).get("log_dir") or "logs")
        return FilingLedger(log_dir / "filing_ledger.sqlite3").record_move(
            source, destination, action=action, metadata=metadata or {})
    except Exception:
        _remove_created_links(metadata, destination)
        try:
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(destination), str(source))
        except Exception:
            pass
        return None


def _associated_case_root(case_cfg: dict, config: dict) -> Path:
    """Resolve a linked case's actual root, honoring an explicit import path."""
    import_path = case_cfg.get("import_path")
    if import_path and str(import_path).strip():
        return Path(import_path).expanduser().resolve()
    return (Path(case_cfg.get("cases_dir") or config["global"]["case_root"])
            / case_cfg["name"]).expanduser().resolve()

# ============================================================
# 分层归档：把旧的"扁平类别 target"映射成"审级/阶段 + 类别"
# 仅对 config 里标了 layout: staged / staged_arb 的新案件生效；
# 存量案件（无 layout）走原逻辑、完全不受影响。
# ============================================================
_CASEWIDE_TOPS = {
    "00_签约与委托", "01_案件资料", "02_法律研究", "09_跨境协作",
    "10_客户沟通", "10_会见与沟通", "11_账单与工时", "12_内部工作文件", "13_结案归档",
    "_待分类", "_待确认来源", "_重复",
}
_STAGE_FOLDER = {
    "一审": "03_一审", "二审": "04_二审", "再审": "05_再审", "执行": "06_执行",
    "诉前": "03_一审", "仲裁": "03_仲裁",
}
# 诉讼文书里"自带审级"的文种 → 固定阶段（不看当前阶段）
_CIVIL_FIXED_DOC = {"起诉状": "03_一审", "上诉状": "04_二审",
                    "上诉答辩状": "04_二审", "再审申请书": "05_再审"}

# ── 刑事分层（layout: staged_crim）：阶段名 → 目录；与 intake._create_criminal_staged 对齐 ──
_CRIM_STAGE_TOPS = {"03_侦查阶段", "04_审查起诉阶段", "05_一审", "06_二审", "07_再审与申诉", "08_执行阶段"}
_CRIM_STAGE_FOLDER = {
    "侦查": "03_侦查阶段", "审查起诉": "04_审查起诉阶段", "一审": "05_一审",
    "二审": "06_二审", "再审": "07_再审与申诉", "申诉": "07_再审与申诉", "执行": "08_执行阶段",
}
# config 规则里刑事仍产出旧的 CR_ 逻辑 target → 映射到新分层物理目录
_CRIM_LEGACY = {
    "CR_侦查阶段/立案文件": "03_侦查阶段/侦查机关文书/立案与传唤",
    "CR_侦查阶段/搜查扣押冻结": "03_侦查阶段/侦查机关文书/搜查扣押冻结",
    "CR_侦查阶段/侦查笔录": "03_侦查阶段/侦查机关文书",
    "CR_侦查阶段/强制措施": "03_侦查阶段/强制措施",
    "CR_侦查阶段/取保候审": "03_侦查阶段/强制措施/取保候审",
    "CR_侦查阶段/报案控告材料": "03_侦查阶段/报案控告与立案监督",
    "CR_侦查阶段/阅卷材料": "04_审查起诉阶段/阅卷材料",
    "CR_侦查阶段/退补侦查意见": "04_审查起诉阶段/辩护意见与申请",
    "CR_审查起诉阶段/起诉书": "04_审查起诉阶段/起诉文书/起诉书",
    "CR_审查起诉阶段/不起诉材料": "04_审查起诉阶段/起诉文书/不起诉决定书",
    "CR_审查起诉阶段/辩护申请": "04_审查起诉阶段/辩护意见与申请",
    "CR_审判阶段/辩护词": "05_一审/辩护文书/辩护词",
    "CR_审判阶段/非法证据排除申请": "05_一审/辩护文书/非法证据排除申请",
    "CR_审判阶段/量刑意见": "05_一审/量刑/量刑意见",
    "CR_审判阶段/附带民事诉讼": "05_一审/附带民事诉讼",
    "CR_执行阶段/减刑假释申请": "08_执行阶段/减刑假释",
    "08_判决与执行/上诉文件": "06_二审/上诉与抗诉/上诉状",
    # CR_上诉申诉 需按 上诉/抗诉 vs 申诉 分流 → 见 _map_crim_target
}
# 自带阶段的刑事文种（按正文关键词判，优先于"当前阶段"）
_CRIM_FIXED_DOC = [
    (r"减刑|假释|监外执行|收监|交付执行|执行通知书", "08_执行阶段"),
    (r"申诉书|再审", "07_再审与申诉"),
    (r"上诉状|抗诉", "06_二审"),
    (r"起诉书|量刑建议|不起诉|审查起诉|认罪认罚|退回补充侦查", "04_审查起诉阶段"),
    (r"拘留|逮捕|取保候审|监视居住|羁押必要性|立案决定|侦查", "03_侦查阶段"),
]


def _stage_from_casenum_crim(text, case_numbers):
    """刑事：先比对登记的各阶段案号，再按 刑初/刑终/刑再/刑执 字样判阶段。"""
    text = text or ""
    if isinstance(case_numbers, dict):
        for k, cn in case_numbers.items():
            if cn and str(cn).strip() and str(cn).strip() in text:
                return _CRIM_STAGE_FOLDER.get(k)
    if re.search(r'刑终|终字第', text):             return "06_二审"
    if re.search(r'刑再|刑申|再字第|审监', text):     return "07_再审与申诉"
    if re.search(r'刑执|执字第', text):             return "08_执行阶段"
    if re.search(r'刑初|初字第', text):             return "05_一审"
    return None


def _map_crim_target(target: str, stage: str, content: str = ""):
    """把扁平/旧CR_/兜底 target 映射到刑事分层目录；已是分层目录则原样透传。"""
    if target in _CRIM_LEGACY:               # 旧 CR_ 逻辑目标 → 新分层
        return _CRIM_LEGACY[target]
    parts = target.split("/")
    top, rest = parts[0], "/".join(parts[1:])
    if top in _CRIM_STAGE_TOPS:              # LLM 已挑到真实分层目录 → 原样
        return target
    if top == "CR_上诉申诉":                  # 上诉/抗诉→二审；申诉→再审（按正文分流，不看 target 名）
        c = content or ""
        if re.search(r'申诉|申请再审|再审', c):   # 申诉优先（申诉书正文必有"申诉/再审"）
            return "07_再审与申诉/申诉材料/申诉书"
        return "06_二审/上诉与抗诉/上诉状"
    if top.startswith("CR_"):                # 其余 CR_ 大类兜底到对应阶段
        if "侦查" in top:       return "03_侦查阶段/阶段性材料"
        if "审查起诉" in top:   return "04_审查起诉阶段"
        if "审判" in top:       return "05_一审"
        if "执行" in top:       return "08_执行阶段"
    doc = rest.split("/")[0] if rest else top
    for pat, st in _CRIM_FIXED_DOC:          # 自带阶段的文种优先
        if re.search(pat, target):
            if top == "05_庭审":             return f"{st}/庭审/{rest}" if rest else f"{st}/庭审"
            if "判决" in doc or "裁定" in doc:  return f"{st}/判决裁定"
            return st
    if top == "03_证据":                     # 证据 → 阅卷（审查起诉起）/侦查阶段材料
        return "03_侦查阶段/阶段性材料" if stage == "03_侦查阶段" else f"{stage}/阅卷材料"
    if top == "05_庭审":
        return f"{stage}/庭审/{rest}" if rest else f"{stage}/庭审"
    if top == "06_专家工作":
        return "05_一审/专家与鉴定"
    if top in ("04_诉讼文书", "08_判决与执行"):
        if "判决" in doc or "裁定" in doc:     # 判决裁定只存在于审判/执行阶段
            st = stage if stage in ("05_一审", "06_二审", "07_再审与申诉") else "05_一审"
            return f"{st}/判决裁定"
        return stage
    return None


def _stage_from_casenum(text: str, case_numbers) -> Optional[str]:
    """从正文里的案号推断阶段：先比对案件登记的各审级案号，再按 民初/民终/民申/执 字样判。"""
    text = text or ""
    if isinstance(case_numbers, dict):
        for k, cn in case_numbers.items():
            if cn and str(cn).strip() and str(cn).strip() in text:
                return _STAGE_FOLDER.get(k)
    if re.search(r'民终|行终|刑终|终字第', text):        return "04_二审"
    if re.search(r'民申|行申|民再|刑再|审监|再字第', text): return "05_再审"
    if re.search(r'执字第|\）\s*执|\)\s*执|执恢|执行裁定|执异', text): return "06_执行"
    if re.search(r'民初|行初|刑初|初字第', text):        return "03_一审"
    return None


def _map_civil_target(target: str, stage: str) -> Optional[str]:
    parts = target.split("/")
    top, rest = parts[0], "/".join(parts[1:])
    if target.startswith("00_签约与委托/授权委托书"):
        return f"{stage}/授权委托与所函/授权委托书"
    if top == "03_证据":
        return f"{stage}/证据/{rest}" if rest else f"{stage}/证据"
    if top == "04_诉讼文书":
        doc = rest.split("/")[0] if rest else ""
        if doc == "法院来文":
            return f"{stage}/法院来文"
        st = _CIVIL_FIXED_DOC.get(doc, stage)
        return f"{st}/诉讼文书/{rest}" if rest else f"{st}/诉讼文书"
    if top == "05_庭审":
        return f"{stage}/庭审/{rest}" if rest else f"{stage}/庭审"
    if top == "06_专家工作":
        return f"{stage}/专家工作/{rest}" if rest else f"{stage}/专家工作"
    if top == "07_和解与调解":
        return f"{stage}/和解调解/{rest}" if rest else f"{stage}/和解调解"
    if top == "08_判决与执行":
        doc = rest.split("/")[0] if rest else ""
        if "执行" in doc:                       # 执行申请书 / 执行异议
            return f"06_执行/执行文书/{doc}"
        if "上诉" in doc:
            return "04_二审/诉讼文书/上诉状"
        return f"{stage}/判决裁定"               # 判决书/裁定书
    return None


def _map_arb_target(target: str) -> Optional[str]:
    parts = target.split("/")
    top, rest = parts[0], "/".join(parts[1:])
    if target.startswith("00_签约与委托/授权委托书"):
        return "03_仲裁/授权委托与所函/授权委托书"
    if top == "03_证据":
        return f"03_仲裁/证据/{rest}" if rest else "03_仲裁/证据"
    if top == "04_仲裁文书":
        if rest.startswith("仲裁机构函件"):
            return "03_仲裁/仲裁机构函件"
        return f"03_仲裁/仲裁文书/{rest}" if rest else "03_仲裁/仲裁文书"
    if top == "05_庭审":
        return f"03_仲裁/庭审/{rest}" if rest else "03_仲裁/庭审"
    if top == "06_专家工作":
        return f"03_仲裁/专家工作/{rest}" if rest else "03_仲裁/专家工作"
    if top == "07_和解与调解":
        return f"03_仲裁/和解调解/{rest}" if rest else "03_仲裁/和解调解"
    if top == "08_裁决与执行":
        doc = rest.split("/")[0] if rest else ""
        if "裁决" in doc:                    return "03_仲裁/裁决"
        if "撤裁" in doc:                    return "04_司法审查/撤裁申请"
        if "不予执行" in doc:                return "04_司法审查/不予执行"
        if "承认" in doc or "执行申请" in doc: return "04_司法审查/承认与执行申请"
        if "执行" in doc:                    return f"05_执行/执行文书/{doc}"
        return "04_司法审查"
    return None


# ── 紧凑目录映射 v2 ─────────────────────────────────────────
# 规则中的具体文书 target 继续作为“逻辑分类”；物理目录只保留材料大类。
# 先保存旧映射，供存量 layout: staged 案件继续使用。
_map_civil_target_legacy = _map_civil_target
_map_arb_target_legacy = _map_arb_target
_map_crim_target_legacy = _map_crim_target

_COMPACT_LAYOUTS = {"staged_compact", "staged_arb_compact", "staged_crim_compact",
                    "staged_mixed_compact"}

def _compact_common_target(target: str) -> str:
    if not target:
        return target
    # 版本由文件名表达，不再建立“过程稿/内部定稿/提交版”目录。
    target = re.sub(r'/(?:过程稿|内部定稿|提交版)$', '', target)
    if target.startswith("01_案件资料/客户提供材料/"):
        return "01_案件资料/客户提供材料"
    if target.startswith("12_内部工作文件/案件策略/"):
        return "12_内部工作文件/案件策略"
    if target.startswith("09_跨境协作/境外诉讼仲裁/"):
        return "09_跨境协作/境外诉讼仲裁"
    return target


def _map_civil_target(target: str, stage: str) -> Optional[str]:
    target = _compact_common_target(target)
    parts = target.split("/")
    top, rest = parts[0], "/".join(parts[1:])
    if target.startswith("00_签约与委托/授权委托书"):
        return f"{stage}/授权委托与所函"
    if top == "03_证据":
        if "我方证据" in rest:
            return f"{stage}/证据/我方证据"
        if "对方证据" in rest:
            return f"{stage}/证据/对方证据"
        return f"{stage}/证据/质证与交换"
    if top == "04_诉讼文书":
        doc = rest.split("/")[0] if rest else ""
        if doc in ("法院来文", "送达文件"):
            return f"{stage}/法院文书"
        return f"{stage}/诉讼文书"
    if top == "05_庭审":
        return f"{stage}/庭审"
    if top == "06_专家工作":
        return f"{stage}/专家工作"
    if top == "07_和解与调解":
        return f"{stage}/和解调解"
    if top == "08_判决与执行":
        doc = rest.split("/")[0] if rest else ""
        if "执行" in doc:
            return f"06_执行/执行文书"
        if "上诉" in doc:
            return "04_二审/诉讼文书"
        return f"{stage}/判决裁定"
    return target


def _map_arb_target(target: str) -> Optional[str]:
    target = _compact_common_target(target)
    parts = target.split("/")
    top, rest = parts[0], "/".join(parts[1:])
    if target.startswith("00_签约与委托/授权委托书"):
        return "03_仲裁/授权委托与所函"
    if top == "03_证据":
        if "我方证据" in rest:
            return "03_仲裁/证据/我方证据"
        if "对方证据" in rest:
            return "03_仲裁/证据/对方证据"
        return "03_仲裁/证据/质证与交换"
    if top == "04_仲裁文书":
        if "机构函件" in rest:
            return "03_仲裁/仲裁机构文书"
        return "03_仲裁/仲裁文书"
    if top == "05_庭审": return "03_仲裁/庭审"
    if top == "06_专家工作": return "03_仲裁/专家工作"
    if top == "07_和解与调解": return "03_仲裁/和解调解"
    if top == "08_裁决与执行":
        doc = rest.split("/")[0] if rest else ""
        if "裁决" in doc: return "03_仲裁/裁决"
        if "撤裁" in doc or "不予执行" in doc or "承认" in doc:
            return "04_司法审查/申请与答辩"
        return "05_执行/执行文书"
    return target


def _map_crim_target(target: str, stage: str, content: str = ""):
    target = _compact_common_target(target)
    # 先按具体刑事文种纠正阶段，再归入该阶段的大类。
    c = (content or "") + " " + target
    if re.search(r"减刑|假释|监外执行|收监|交付执行|执行通知书", c):
        return "08_执行阶段/减刑假释与变更执行"
    if re.search(r"申诉书|申请再审|再审", c):
        return "07_再审与申诉/申诉与再审材料"
    if re.search(r"上诉状|抗诉", c):
        return "06_二审/上诉与抗诉"
    if re.search(r"起诉书|量刑建议|不起诉|审查起诉|认罪认罚|退回补充侦查", c):
        return "04_审查起诉阶段/检察机关文书"
    if re.search(r"拘留|逮捕|取保候审|监视居住|羁押必要性|立案决定|侦查", c):
        return "03_侦查阶段/机关文书"
    if target.startswith("03_证据"):
        return f"{stage}/证据材料"
    if target.startswith("05_庭审"):
        return f"{stage}/庭审"
    if target.startswith("06_专家工作"):
        return "05_一审/证据与鉴定"
    if target.startswith("04_诉讼文书") or target.startswith("08_判决与执行"):
        return f"{stage}/法院文书" if "判决" in c or "裁定" in c else f"{stage}/辩护与代理材料"
    if target.startswith("CR_"):
        if "侦查" in target: return "03_侦查阶段/律师材料"
        if "审查起诉" in target: return "04_审查起诉阶段/律师材料"
        if "审判" in target: return "05_一审/辩护与代理材料"
        if "执行" in target: return "08_执行阶段/执行机关文书"
    return target


def _map_us_target(target: str) -> str:
    target = _compact_common_target(target)
    parts = target.split("/")
    if len(parts) < 2:
        return target
    top, rest = parts[0], parts[1:]
    if top == "01_Case Materials" and rest[0] in ("Client-Provided Materials", "Attorney-Gathered Materials"):
        return f"{top}/{rest[0]}"
    if top in ("03_Opposing Party Filings", "04_Our Filings", "{us_party_filings}"):
        if rest[0].startswith("Pleadings"): bucket = "Pleadings"
        elif rest[0].startswith("Motions"): bucket = "Motions & Briefs"
        elif rest[0].startswith("Discovery"): bucket = "Discovery"
        else: bucket = "Pretrial Submissions"
        return f"{top}/{bucket}"
    if top == "05_Discovery & Doc Review": return f"{top}/{rest[0]}"
    if top == "06_Experts": return f"{top}/{rest[0]}"
    if top == "07_Trial": return f"{top}/{rest[0]}"
    if top == "08_Court Documents":
        if rest[0] in ("Summons & Service", "Hearing Notices", "Case Management Order", "Court Orders"):
            return f"{top}/Orders & Notices" if rest[0] != "Summons & Service" else f"{top}/Service & Summons"
        return f"{top}/{rest[0]}"
    if top == "09_Settlement":
        return f"{top}/Negotiations" if rest[0] == "Settlement Negotiations" else f"{top}/Agreements & Dismissal"
    if top == "10_Appeal":
        if rest[0] == "Appellate Briefs": return f"{top}/Appellate Briefs"
        if rest[0] in ("Notice of Appeal", "Record on Appeal"): return f"{top}/Notice & Record"
        return f"{top}/Oral Argument"
    if top == "11_Cross-Border":
        if rest[0] == "Foreign Counsel Correspondence": return f"{top}/Foreign Counsel"
        if rest[0] == "Foreign Law Opinions": return f"{top}/Foreign Law"
        return f"{top}/{rest[0]}"
    if top == "12_Client Communications": return f"{top}/Email & Drafting" if rest[0] in ("Email Correspondence", "Email Summaries") else f"{top}/Meetings & Reports"
    if top == "13_Billing & Time": return f"{top}/Time Records" if rest[0] == "Time Records" else f"{top}/Expenses & Invoices"
    if top == "14_Internal Work Files": return f"{top}/Case Strategy" if rest[0] == "Case Strategy" else f"{top}/Work Log & Templates"
    return target


def _apply_stage(case_cfg: dict, target: str, content_text: str = "",
                 ov_stage: str = None) -> str:
    """把扁平 target 转成分层 target。仅 layout: staged* 生效；否则原样返回。"""
    layout = (case_cfg or {}).get("layout", "") or ""
    if not target:
        return target
    if layout == "us_civil":
        return _map_us_target(target)
    if not layout.startswith("staged"):
        return _compact_common_target(target)
    compact = layout in _COMPACT_LAYOUTS
    if compact:
        target = _compact_common_target(target)
        top = target.split("/")[0]
    crim = layout.startswith("staged_crim")
    top = target.split("/")[0]
    # 案件级目录：原样。例外：民事/仲裁的 00 授权委托书下沉到各审级；刑事保持案件级。
    if top in _CASEWIDE_TOPS:
        if crim or not target.startswith("00_签约与委托/授权委托书"):
            return target
    if layout == "staged_mixed_compact" and top in {
        "CR_侦查阶段", "CR_审查起诉阶段", "CR_审判阶段", "CR_上诉申诉", "CR_执行阶段",
    }:
        stage = (_stage_from_casenum_crim(content_text, case_cfg.get("case_numbers"))
                 or _CRIM_STAGE_FOLDER.get(case_cfg.get("stage"))
                 or "03_侦查阶段")
        mapper = _map_crim_target if compact else _map_crim_target_legacy
        return mapper(target, stage, content_text) or target
    if crim:
        # 刑事：定阶段（手动 > 案号 > 案件当前阶段 > 默认侦查）；自带阶段文种在 _map 内纠正
        stage = ((_CRIM_STAGE_FOLDER.get(ov_stage) if ov_stage else None)
                 or _stage_from_casenum_crim(content_text, case_cfg.get("case_numbers"))
                 or _CRIM_STAGE_FOLDER.get(case_cfg.get("stage"))
                 or "03_侦查阶段")
        return _map_crim_target(target, stage, content_text) or target
    if layout.startswith("staged_arb"):
        mapper = _map_arb_target if compact else _map_arb_target_legacy
        return mapper(target) or target
    # 民事：定阶段（手动 > 案号 > 案件当前阶段 > 默认一审）；自带审级的文种在 _map 内再纠正
    stage = ((_STAGE_FOLDER.get(ov_stage) if ov_stage else None)
             or _stage_from_casenum(content_text, case_cfg.get("case_numbers"))
             or _STAGE_FOLDER.get(case_cfg.get("stage"))
             or "03_一审")
    mapper = _map_civil_target if compact else _map_civil_target_legacy
    return mapper(target, stage) or target


# ── 层级序号：把 core 目标路径解析成真实(带 A -/1. 前缀)目录；与未编号存量案件兼容 ──
_LVL_PREFIX = re.compile(r'^(?:\d{2}_|[A-Z]{1,2} - |\d+\. )')


def _core_seg(name: str) -> str:
    return _LVL_PREFIX.sub("", name)


def _decorate_new(parent: Path, core: str, depth: int) -> str:
    """新建缺失目录时按层级加序号；同级现有目录未编号则不加（兼容存量案件）。"""
    if depth == 0 or core.startswith("_"):
        return core
    sibs = [c.name for c in parent.iterdir() if c.is_dir()] if parent.exists() else []
    if depth == 1:
        lettered = [s for s in sibs if re.match(r'^[A-Z]{1,2} - ', s)]
        if sibs and not lettered:
            return core
        return f"{chr(65 + len(lettered))} - {core}"
    if depth == 2:
        numbered = [s for s in sibs if re.match(r'^\d+\. ', s)]
        if sibs and not numbered:
            return core
        return f"{len(numbered) + 1}. {core}"
    return core


def _resolve_dest(base: Path, relpath: str) -> Path:
    """core 目标路径 → 真实目录路径（前缀无关匹配；缺失则按层级前缀新建）。"""
    validate_relative_target(relpath, "目标目录")
    base = Path(base).expanduser().resolve()
    cur = base
    for depth, seg in enumerate(relpath.split("/")):
        core = _core_seg(seg)
        match = None
        if cur.exists():
            for ch in sorted(cur.iterdir()):
                if ch.is_dir() and _core_seg(ch.name) == core:
                    match = ch
                    break
        cur = match if match is not None else (cur / _decorate_new(cur, core, depth))
    return resolve_under(base, cur)


# ============================================================
# 单文件处理
# ============================================================
def _process_file_unlocked(
    file_path: Path, config: dict, logger: logging.Logger, dry_run: bool = False
) -> Optional[str]:
    if not file_path.is_file():
        return None
    filename = file_path.name
    ext = file_path.suffix.lower()

    skip_names = {"README.txt", "README.md", "说明.txt", ".DS_Store", "Thumbs.db"}
    skip_exts = {".tmp", ".lock", ".crdownload", ".part", ".~lock", ".icloud"}
    if filename in skip_names or ext in skip_exts or filename.startswith("~") or filename.startswith("."):
        return None

    logger.info(f"📄 处理: {filename}")

    # 合规文件名 & 送达附随文件检测（早期，影响后续命名决策）
    _is_compliant = is_compliant_filename(filename)
    _delivery_base = _strip_delivery_suffix(file_path.stem)  # 非 None → 送达附随文件

    # 人工预填（sidecar）——人工优先，留空字段才走自动识别
    override = {}
    sidecar = file_path.parent / f".{filename}.loci.json"
    if sidecar.exists():
        try:
            override = json.loads(sidecar.read_text(encoding="utf-8")) or {}
        except Exception:
            override = {}
        if override:
            logger.info(f"   ✍ 投放上下文: {override}")

    # 手工已指定 案件+类型 → 无需读内容，跳过 OCR（手工为主路径，省去等待）
    fully_manual = bool(override.get("case_key") and override.get("target"))
    if fully_manual:
        content_text = ""
        extracted_title = None
        logger.info("   ⏭ 已手工指定案件+类型，跳过内容识别/OCR")
    else:
        _llm_cfg_early = config.get("llm", {})
        content_text = extract_markdown(
            file_path,
            max_chars=int(_llm_cfg_early.get("max_chars", 8000)),
            tail_pages=2,
        )
        if content_text.strip():
            # Keep document text in memory for classification only; verbose
            # logs must never contain material正文.
            logger.debug("   已提取材料文本（正文不写入日志）")
        # 标题：优先从 Markdown H1/H2 提取，回落到 SmartNamer 正则模式
        # Prefer a recognized legal-document title over a promoted metadata
        # heading (for example an案号 on the first line); the decision preview
        # and the move must therefore see the same document candidate.
        extracted_title = (SmartNamer.extract_title(content_text)
                           or extract_title_from_markdown(content_text)
                           or SmartNamer.extract_heading(content_text))
        if extracted_title:
            logger.debug(f"   标题: {extracted_title}")

    # Phase 2 decision pass: produce a conservative, explainable snapshot
    # before the legacy path builder runs.  This pass never touches the file.
    try:
        decision_preview = build_filing_decision(
            filename, content_text, config.get("cases", []), config.get("rules", []),
            extracted_title=extracted_title, manual=override,
            received_at=override.get("date"),
        )
        try:
            log_dir = Path(config.get("global", {}).get("log_dir") or "logs")
            decision_preview = DecisionStore(
                log_dir.expanduser().resolve().parent / "state" / "loci.db"
            ).save(decision_preview)
        except Exception as store_exc:
            # The snapshot improves auditability but must not expose document
            # text or stop legacy offline filing if its local store is unavailable.
            logger.warning("   ⚠ 决策快照未保存：%s", type(store_exc).__name__)
    except Exception as exc:
        decision_preview = {}
        logger.debug(f"   决策预分析跳过：{exc}")

    # ① 案件：人工预填 > 本地规则评分；无法确定时挂起人工复核。
    ov_case = override.get("case_key")
    if ov_case:
        case_cfg = next((c for c in config.get("cases", []) if c.get("key") == ov_case), {})
        case_key, case_name = (ov_case, case_cfg.get("name")) if case_cfg else (None, None)
        logger.info(f"   🏷 案件(人工): {case_name or '⚠ 预填案件不存在 → _未分类'}")
    else:
        selected = (decision_preview.get("matter") or {}).get("selected")
        case_status = (decision_preview.get("matter") or {}).get("status")
        if selected and case_status == "selected":
            case_key, case_name = selected.get("key"), selected.get("name")
        else:
            # Unknown/close case candidates must not be resolved by config
            # order.  They remain in _未分类 for explicit case selection.
            case_key, case_name = None, None
        case_cfg = next((c for c in config.get("cases", []) if c.get("key") == case_key), {})
        if case_key:
            logger.info(f"   🏷 案件: {case_name}")
        else:
            logger.info("   🏷 ⚠ 案件未达到确定阈值 → _未分类（待人工选择）")

    # 送达附随文件：用主文件名兜底识别案件
    if not case_key and _delivery_base:
        case_key, case_name = match_case(_delivery_base, "", config.get("cases", []))
        case_cfg = next((c for c in config.get("cases", []) if c.get("key") == case_key), {}) if case_key else {}
        if case_key:
            logger.info(f"   🏷 案件(随主文件): {case_name}")

    # 诉讼主体（手工选的当事人缩写，用于命名；并可反推来源）
    party = override.get("party") or None

    # ③ 来源：Decision 是自动分析与实际移动共用的唯一事实源。
    # 人工 target 本身不构成来源事实；只有明确的 manual.source 才能越过
    # 全局来源门控。旧版签名页/文件名兜底不在这里另行覆盖 preview。
    decision_source = decision_preview.get("source") or {}
    source = (decision_source.get("kind")
              if decision_source.get("status") in {"inferred", "manual"}
              else None)
    if source:
        tag = "(人工)" if decision_source.get("status") == "manual" else ""
        logger.info(f"   📨 来源{tag}: {SOURCE_LABELS.get(source, source)}")
    # 来源已知时自动取当事人缩写作为命名主体（override.party 优先，未填则自动推）
    if not party and source:
        party = _source_label(source, case_cfg)

    # ② 文件类型：人工预填 > 本地规则评分 > 待分类
    #    场景硬隔离：按本案场景过滤规则，避免跨场景误归（如 US 文件被中文规则截胡）
    allowed_sc = _allowed_scenarios(case_cfg)
    ov_target = override.get("target")
    llm_doc_type  = None
    # Despite the legacy variable name this is a local, deterministic title
    # extraction result.  It is used only after the decision engine confirms
    # the semantic document candidate.
    llm_title     = extracted_title or None
    rule          = None
    target_subdir = None
    fname_doc_type = None   # doc_type从文件名精准匹配中提取，优先于内容识别

    if ov_target:
        target_subdir = ov_target
        logger.info(f"   📂 归类(人工): {target_subdir}")
    else:
        # 送达附随文件：以主文件名规则确定目标文件夹（与主文件同归）
        if _delivery_base and not target_subdir:
            _del_fake_name = _delivery_base + ext
            del_rule = (_title_key_match(_del_fake_name, config.get("rules", []), allowed_sc)
                        or match_rule(_delivery_base, _delivery_base,
                                      config.get("rules", []), _delivery_base, source, allowed_sc))
            if del_rule:
                target_subdir = del_rule["target"]
                _is_compliant = True  # 送达附随保留原名
                logger.info(f"   📎 送达附随 → 随主文件归入 {target_subdir}")

        # 文件名 title_keywords 精准预检 —— 优先于 LLM，防止高置信度规则被 LLM 误覆盖
        pre_rule = _title_key_match(filename, config.get("rules", []), allowed_sc)
        _from_title_match = False
        if pre_rule and not _delivery_base:
            # 文件名命中只作为候选和命名提示，不再绕过全文规则评分。
            rule = pre_rule
            _from_title_match = True
            logger.info(f"   🔎 文件名候选: {pre_rule['target']}（继续进行规则评分）")
            # 从文件名里提取到的最具体的文书类型名，用于命名（优先于内容title抽取）
            _stem_lower = Path(filename).stem.lower()
            # 取最长匹配的 title_keyword（越长越具体，防止"申请书"盖过"变更诉讼请求申请书"）
            _matched_kw = max(
                (kw for kw in pre_rule.get("title_keywords", []) if kw.lower() in _stem_lower),
                key=len,
                default=None,
            )
            _rp = pre_rule.get("rename_pattern") or ""
            # 取更具体（更长）的那个：文件名匹配词 vs 规则规范名
            fname_doc_type = _rp if len(_rp) > len(_matched_kw or "") else _matched_kw

        if not target_subdir:
            rule = match_rule(filename, content_text, config.get("rules", []),
                              extracted_title, source, allowed_sc)
            if rule:
                target_subdir = rule["target"]
                logger.info(f"   📂 归类(规则评分 {rule.get('_match_score', 0)}): {target_subdir}")
                if rule.get("_match_reasons"):
                    logger.info("   ↳ 依据: " + "；".join(rule["_match_reasons"][:5]))
            else:
                target_subdir = config.get("fallback", {}).get("target", "_待分类")
                logger.info(f"   📂 未识别 → {target_subdir}（进待办复核）")

        # The side-effect-free decision is the final logical preview.  Keep
        # the legacy matcher above for naming hints and compatibility, but do
        # not let it choose a different destination from the preview.
        ddoc = (decision_preview.get("document_type") or {})
        selected_ddoc = ddoc.get("selected")
        ddest = decision_preview.get("destination") or {}
        d_matter = (decision_preview.get("matter") or {}).get("selected") or {}
        decision_matches_case = bool(case_key and d_matter.get("key") == case_key)
        if decision_matches_case or (not case_key and not d_matter):
            candidate_target = ((selected_ddoc or {}).get("target")
                                or ddest.get("pending_target")
                                or ddest.get("relative")
                                or config.get("fallback", {}).get("target", "_待分类"))
            if selected_ddoc:
                selected_rule = next((r for r in config.get("rules", [])
                                      if r.get("target") == selected_ddoc.get("target")), None)
                # A neutral US placeholder deliberately has no side-specific
                # YAML rule.  Do not retain the earlier first-in-config rule,
                # otherwise its "(Opposing)" rename pattern leaks into a
                # source-unknown or our-side filename.
                if (selected_rule is None or pre_rule is None
                        or pre_rule.get("target") != selected_rule.get("target")):
                    fname_doc_type = None
                rule = selected_rule
                llm_doc_type = selected_ddoc.get("name") or llm_doc_type
            elif ddoc.get("status") != "selected":
                rule = None
                fname_doc_type = None

            source_unresolved = (decision_source.get("status")
                                 in UNRESOLVED_SOURCE_STATUSES)
            target_subdir = ddest.get("relative") or "_待分类"
            pending_target = ddest.get("pending_target") if source_unresolved else None
            if case_key and source_unresolved:
                pending_target = pending_target or candidate_target
                pending_target = (pending_target.replace("我方证据", "{side}")
                                  .replace("对方证据", "{side}"))
                target_subdir = "_待确认来源"
                logger.info(f"   ❓ 来源未确定 → 挂起 _待确认来源（确认后归: {pending_target}）")
            elif not case_key:
                # Unknown matters remain in _未分类 even when source is also
                # unresolved; source confirmation needs a known matter.
                target_subdir = "_未分类"
                pending_target = None
            elif ddoc.get("status") == "selected":
                logger.info(f"   🛡 决策预览目标：{target_subdir}")
            else:
                logger.info("   🛡 文书性质证据不足或存在交叉候选 → 待分类复核")
        else:
            # A delivery-suffix fallback can identify a matter after the
            # initial preview.  Preserve that legacy path, but still enforce
            # the same global source gate conservatively.
            candidate_target = target_subdir or config.get("fallback", {}).get("target", "_待分类")
            source_unresolved = (decision_source.get("status")
                                 in UNRESOLVED_SOURCE_STATUSES
                                 or not decision_source)
            pending_target = None
            if case_key and source_unresolved:
                pending_target = (candidate_target.replace("我方证据", "{side}")
                                  .replace("对方证据", "{side}"))
                target_subdir = "_待确认来源"
                logger.info(f"   ❓ 来源未确定 → 挂起 _待确认来源（确认后归: {pending_target}）")

    # The manual-target branch does not enter the legacy matcher above, but it
    # is still subject to the same source gate.  In particular, target alone
    # must not be treated as a source override.
    if ov_target:
        ddoc = (decision_preview.get("document_type") or {})
        selected_ddoc = ddoc.get("selected") or {}
        ddest = decision_preview.get("destination") or {}
        candidate_target = (selected_ddoc.get("target") or ov_target)
        llm_doc_type = selected_ddoc.get("name") or override.get("doc_type") or llm_doc_type
        source_unresolved = (decision_source.get("status")
                             in UNRESOLVED_SOURCE_STATUSES)
        if not case_key:
            target_subdir = "_未分类"
            pending_target = None
        elif source_unresolved:
            pending_target = (ddest.get("pending_target") or candidate_target)
            pending_target = (pending_target.replace("我方证据", "{side}")
                              .replace("对方证据", "{side}"))
            target_subdir = "_待确认来源"
            logger.info(f"   ❓ 来源未确定 → 挂起 _待确认来源（确认后归: {pending_target}）")
        else:
            target_subdir = ddest.get("relative") or candidate_target
            pending_target = None

    # 旧版 LLM preflight sidecar 不参与 Lite 版归档决策，避免历史模型结果污染当前规则结果。
    _pf_mismatch = False
    _pf_sidecar  = file_path.parent / f".{filename}.loci.preflight.json"

    # ── 分层归档：把类别 target 映射成「审级/阶段 + 类别」（仅 layout: staged* 的新案件）──
    _ov_stage = override.get("stage") if isinstance(override, dict) else None
    _pre = target_subdir
    target_subdir = _apply_stage(case_cfg, target_subdir, content_text, _ov_stage)
    if pending_target:
        pending_target = _apply_stage(case_cfg, pending_target, content_text, _ov_stage)
    if target_subdir != _pre:
        logger.info(f"   🧭 分层归位: {_pre} → {target_subdir}")

    # Keep the document type that produced the candidate target.  This is
    # metadata for the review UI; it must not be replaced by the holding
    # folder name after the source gate changes the physical destination.
    _decision_doc = ((decision_preview.get("document_type") or {}).get("selected")
                     or {})
    original_doc_type = (override.get("doc_type")
                         or _decision_doc.get("name")
                         or llm_doc_type
                         or "")

    case_root = Path(case_cfg.get("cases_dir") or config["global"]["case_root"])
    case_folder = (case_root / case_name) if case_name else (case_root / "_未分类")
    dest_dir = _resolve_dest(case_folder, target_subdir)   # core → 真实(带层级序号)目录

    # 真重复检测：目标文件夹已有内容完全相同的文件 → 不重复归档，移入 _重复 隔离
    dup = _find_duplicate(dest_dir, file_path)
    if dup:
        logger.info(f"   ♻ 重复文件（内容同 {dup.name}）")
        if dry_run:
            logger.info(f"   🔍 [DRY-RUN] → 重复，将移入 _重复")
            return None
        qdir = case_folder / "_重复"
        qdir.mkdir(parents=True, exist_ok=True)
        qdest = qdir / filename
        i = 1
        while qdest.exists():
            qdest = qdir / f"{Path(filename).stem}_{i}{Path(filename).suffix}"
            i += 1
        shutil.move(str(file_path), str(qdest))
        txid = _record_ledger(
            config, file_path, qdest, action="duplicate",
            metadata={"case_key": case_key,
                      "decision_id": decision_preview.get("decision_id"),
                      "duplicate_of": str(dup),
                      **_upload_audit_fields(override)},
        )
        if not txid:
            logger.error("   ❌ 事务账本写入失败，已尝试回滚重复文件移动")
            return None
        if sidecar.exists():
            sidecar.unlink(missing_ok=True)
        _append_archive_log(config, {"action": "duplicate", "transaction_id": txid,
                                     "original_name": filename,
                                     "from": str(file_path), "to": str(qdest),
                                     "duplicate_of": str(dup), "case": case_name or "_未分类",
                                     "case_key": case_key, "needs_review": True,
                                     **_upload_audit_fields(override)})
        logger.info(f"   ♻ → {qdest}（原件已有，移入 _重复 待你确认）")
        return str(qdest)

    new_name = original_name = filename
    dt_conf, dt_src = 1.0, "n/a"

    if _is_compliant:
        logger.info(f"   ✔ 合规文件名，保留原名归档")

    if config["global"].get("rename_files") and not _is_compliant:
        naming_config = config.get("naming", {})
        mode = naming_config.get("mode", "smart")

        if mode == "smart":
            mtime = _parse_recv_date(override.get("date")) or \
                    _date_from_filename(filename) or \
                    _pdf_creation_date(file_path) or \
                    datetime.fromtimestamp(file_path.stat().st_mtime)
            # 文书名：自定义 > 文件名精准匹配 > LLM 原始标题 > LLM 规范类型 > 由目标文件夹推出
            # fname_doc_type 优先于内容识别：文件名已明确说明文书类型时，不允许内容抽取覆盖
            doc_type = override.get("doc_type") or fname_doc_type or llm_title or llm_doc_type \
                       or (_doctype_from_target(ov_target) if ov_target else None)
            # 同类型自动组次：扫描目标文件夹
            auto_batch = override.get("batch") or _auto_batch(dest_dir, doc_type or "", ext)
            # 两层：① 抽取结构化字段（含置信度）② 套模板格式化
            fields = SmartNamer.extract_naming_fields(
                content_text or "", filename, mtime=mtime, doc_type=doc_type,
                rename_pattern=(rule.get("rename_pattern") if rule else None),
                target=target_subdir, force_version=override.get("version"),
                party=party, batch=auto_batch,
                translation=bool(override.get("translation")),
                source=source,
            )
            dt_conf, dt_src = fields.get("dt_conf", 1.0), fields.get("dt_src")
            if dt_conf < 0.5:
                logger.info(f"   ⚠ 命名依据较弱（{dt_src}）→ "
                            f"暂用「{fields['doc_type']}」，建议人工确认（后续可由 LLM 理解内容定名）")
            new_name = SmartNamer.format_filename(case_key, fields, ext)
        elif mode == "simple":
            # 简单模式保持旧逻辑，但日期也改为 YYMMDD
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            date_str = mtime.strftime("%y%m%d") + " "
            rp = rule.get("rename_pattern") if rule else None
            new_name = f"{date_str}{rp}{ext}" if rp else filename
        else:
            new_name = filename

        # 去重
        counter = 1
        while (dest_dir / new_name).exists():
            stem = Path(new_name).stem
            base = re.sub(r'_\d+$', '', stem)
            new_name = f"{base}_{counter}{ext}"
            counter += 1

        if new_name != original_name:
            logger.info(f"   ✏ 命名: {original_name} → {new_name}")

    dest_path = dest_dir / new_name

    if dry_run:
        logger.info(f"   🔍 [DRY-RUN] → {dest_path}")
        return str(dest_path)

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest_path))

    # 跨案文件：主文件移动成功后、写入同一归档事务前建立快捷方式。
    # 只有 os.symlink 成功创建的路径才进入事务 metadata，已有路径绝不覆盖。
    cross_case_links = []
    for okey in (override.get("also_cases") or []):
        if okey == case_key:
            continue
        ocase = next((c for c in config.get("cases", []) if c.get("key") == okey), None)
        if not ocase:
            continue
        try:
            oroot = _associated_case_root(ocase, config) / "_跨案引用"
            oroot.mkdir(parents=True, exist_ok=True)
            link = oroot / dest_path.name
            if os.path.lexists(str(link)):
                logger.info(f"   ⏭ 跨案引用目标已存在，跳过（{okey}）：{link}")
                continue
            os.symlink(str(dest_path), str(link))
            cross_case_links.append(str(link))
            logger.info(f"   🔗 跨案引用 → {okey}/_跨案引用/{dest_path.name}")
        except Exception as e:
            logger.info(f"   ⚠ 跨案引用失败（{okey}）：{e}")

    txid = _record_ledger(config, file_path, dest_path, action="archive",
                          metadata={"case_key": case_key, "target": target_subdir,
                                    "decision_id": decision_preview.get("decision_id"),
                                    "source": source, "pending": bool(pending_target),
                                    "links": cross_case_links,
                                    **_upload_audit_fields(override)})
    if not txid:
        logger.error("   ❌ 事务账本写入失败，已尝试回滚归档移动")
        return None
    if sidecar.exists():
        sidecar.unlink(missing_ok=True)
    if _pf_sidecar.exists():
        _pf_sidecar.unlink(missing_ok=True)
    logger.info(f"   ✅ → {dest_path}")

    # 挂起待确认来源：写 sidecar 记录候选目标，供前台确认来源后归位
    if pending_target:
        ps = dest_path.parent / f".{dest_path.name}.pending.json"
        ps.write_text(json.dumps({"pending_target": pending_target,
                                  "case_key": case_key,
                                  "doc_type": original_doc_type,
                                  "original_doc_type": original_doc_type,
                                  "source_required": True,
                                  "source_options": list(SOURCE_OPTIONS),
                                  "case": case_name,
                                  **_upload_audit_fields(override)},
                                 ensure_ascii=False), encoding="utf-8")

    _append_archive_log(config, {
        "original_name": original_name,
        "transaction_id": txid,
        "from": str(file_path),
        "new_name": new_name,
        "to": str(dest_path),
        "case": case_name or "_未分类",
        "case_key": case_key,
        "source": source,
        "target": target_subdir,
        "rule_score": rule.get("_match_score") if rule else None,
        "rule_reasons": rule.get("_match_reasons", []) if rule else [],
        "manual": any(override.get(key) for key in (
            "case_key", "target", "doc_type", "source", "party", "batch",
            "version", "translation", "also_cases", "stage",
        )),
        "dt_conf": dt_conf,
        "dt_src": dt_src,
        "needs_review": (case_name is None) or (dt_conf < 0.5) or bool(pending_target) or _pf_mismatch,
        "needs_source": bool(pending_target),
        "llm_rule_mismatch": _pf_mismatch,
        **_upload_audit_fields(override),
    })

    return str(dest_path)


def process_file(
    file_path: Path, config: dict, logger: logging.Logger, dry_run: bool = False
) -> Optional[str]:
    """跨进程串行处理同一收件箱文件，避免 daemon/once 重复搬运。"""
    file_path = Path(file_path)
    lock_key = hashlib.sha256(str(file_path.resolve(strict=False)).encode("utf-8")).hexdigest()
    lock_path = file_path.parent / ".loci_locks" / f"{lock_key}.lock"
    try:
        with FileLock(lock_path, blocking=False):
            return _process_file_unlocked(file_path, config, logger, dry_run)
    except LockBusy:
        logger.warning(f"   ⏳ 已由另一归档进程处理，跳过: {file_path.name}")
        return None


# ============================================================
# Daemon 收件箱摄取
# ============================================================
_DAEMON_STABILITY_INTERVAL = 0.2
_DAEMON_STABILITY_TIMEOUT = 10.0
_DAEMON_MAX_ATTEMPTS = 3
_DAEMON_MAX_STABILITY_TIMEOUT = 60.0
_DAEMON_TEMP_SUFFIXES = {".part", ".tmp"}
_DAEMON_SIDECAR_SUFFIXES = (".loci.json", ".pending.json")


def _is_intake_candidate(file_path: Path) -> bool:
    """Return whether *file_path* is a regular file eligible for daemon intake."""
    try:
        file_path = Path(file_path)
        name = file_path.name
        lower_name = name.lower()
        if (not name or name.startswith(".")
                or lower_name.endswith(_DAEMON_SIDECAR_SUFFIXES)
                or file_path.suffix.lower() in _DAEMON_TEMP_SUFFIXES):
            return False
        return file_path.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _wait_for_file_stable(
    file_path: Path,
    *,
    sample_interval: float = _DAEMON_STABILITY_INTERVAL,
    timeout: float = _DAEMON_STABILITY_TIMEOUT,
    stable_samples: int = 2,
    clock=None,
    sleeper=None,
) -> bool:
    """Wait for at least two equal size/mtime samples within a finite timeout."""
    file_path = Path(file_path)
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    try:
        interval = max(0.001, float(sample_interval))
    except (TypeError, ValueError):
        interval = _DAEMON_STABILITY_INTERVAL
    try:
        wait_timeout = min(
            max(0.0, float(timeout)), _DAEMON_MAX_STABILITY_TIMEOUT
        )
    except (TypeError, ValueError):
        wait_timeout = _DAEMON_STABILITY_TIMEOUT
    try:
        required_samples = max(2, int(stable_samples))
    except (TypeError, ValueError):
        required_samples = 2

    deadline = clock() + wait_timeout
    previous = None
    unchanged = 0
    while True:
        try:
            file_stat = file_path.stat()
        except (OSError, RuntimeError, ValueError):
            return False

        signature = (
            file_stat.st_size,
            getattr(file_stat, "st_mtime_ns", file_stat.st_mtime),
        )
        if signature == previous:
            unchanged += 1
        else:
            previous = signature
            unchanged = 1
        if unchanged >= required_samples:
            return True

        remaining = deadline - clock()
        if remaining <= 0:
            return False
        # Never sleep beyond the deadline; a zero/negative test interval still
        # gets a small bounded yield rather than a busy infinite loop.
        sleeper(min(interval, remaining))


def _process_intake_file(
    file_path: Path,
    config: dict,
    logger: logging.Logger,
    *,
    process_func=None,
    stability_interval: float = _DAEMON_STABILITY_INTERVAL,
    stability_timeout: float = _DAEMON_STABILITY_TIMEOUT,
    max_attempts: int = _DAEMON_MAX_ATTEMPTS,
) -> Optional[str]:
    """Stabilize and process one intake file with bounded exception retries."""
    file_path = Path(file_path)
    if not _is_intake_candidate(file_path):
        return None

    try:
        attempts = max(1, int(max_attempts))
    except (TypeError, ValueError):
        attempts = _DAEMON_MAX_ATTEMPTS
    processor = process_func or process_file

    for attempt in range(1, attempts + 1):
        if not _is_intake_candidate(file_path):
            return None
        if not _wait_for_file_stable(
            file_path,
            sample_interval=stability_interval,
            timeout=stability_timeout,
        ):
            if attempt < attempts:
                logger.warning(
                    "⏳ %s: 文件尚未稳定，第 %d/%d 次等待后重试",
                    file_path.name, attempt, attempts,
                )
                continue
            logger.error(
                "❌ %s: 文件在有界等待时间内未稳定，放弃本次处理",
                file_path.name,
            )
            return None

        try:
            return processor(file_path, config, logger, False)
        except Exception as exc:
            if attempt < attempts:
                logger.warning(
                    "⚠ %s: 第 %d/%d 次处理失败，将重试：%s",
                    file_path.name, attempt, attempts, exc,
                )
            else:
                logger.error(
                    "❌ %s: 处理失败（已尝试 %d 次）：%s",
                    file_path.name, attempts, exc,
                )
    return None


def _intake_path_key(file_path: Path) -> str:
    """Use one canonical key for event and startup-scan de-duplication."""
    try:
        return str(Path(file_path).resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return os.path.abspath(os.fspath(file_path))


class _DaemonIntake:
    """Coordinate watchdog callbacks and startup scanning for one inbox."""

    def __init__(
        self,
        config: dict,
        logger: logging.Logger,
        *,
        process_func=None,
        stability_interval: float = _DAEMON_STABILITY_INTERVAL,
        stability_timeout: float = _DAEMON_STABILITY_TIMEOUT,
        max_attempts: int = _DAEMON_MAX_ATTEMPTS,
    ):
        self.config = config
        self.logger = logger
        self.process_func = process_func or process_file
        self.stability_interval = stability_interval
        self.stability_timeout = stability_timeout
        self.max_attempts = max_attempts
        self.pending = set()
        self._pending_lock = threading.Lock()

    def submit(self, file_path: Path) -> bool:
        """Claim and process a candidate, releasing the claim in all cases."""
        file_path = Path(file_path)
        if not _is_intake_candidate(file_path):
            return False

        key = _intake_path_key(file_path)
        with self._pending_lock:
            if key in self.pending:
                return False
            self.pending.add(key)
        try:
            _process_intake_file(
                file_path,
                self.config,
                self.logger,
                process_func=self.process_func,
                stability_interval=self.stability_interval,
                stability_timeout=self.stability_timeout,
                max_attempts=self.max_attempts,
            )
            return True
        finally:
            with self._pending_lock:
                self.pending.discard(key)


def _make_daemon_event_handler(file_system_event_handler, intake: _DaemonIntake):
    """Build a watchdog handler while keeping watchdog an optional import."""
    class Handler(file_system_event_handler):
        def __init__(self):
            super().__init__()

        def on_created(self, event):
            if not getattr(event, "is_directory", False):
                intake.submit(event.src_path)

        def on_moved(self, event):
            if not getattr(event, "is_directory", False):
                # For moves, only the destination is an inbox candidate.
                intake.submit(event.dest_path)

    return Handler()


def _scan_inbox(inbox: Path, intake: _DaemonIntake) -> int:
    """Submit ordinary files already present after the observer is running."""
    try:
        files = sorted(
            (path for path in Path(inbox).iterdir()
             if _is_intake_candidate(path)),
            key=lambda path: path.name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        intake.logger.error("❌ 启动扫描收件箱失败：%s", exc)
        return 0

    accepted = 0
    for file_path in files:
        if intake.submit(file_path):
            accepted += 1
    return accepted

# ============================================================
# CLI
# ============================================================
def main():
    set_private_umask()
    parser = argparse.ArgumentParser(description="涉外诉讼材料自动归档 v2.2")
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="单次处理")
    parser.add_argument("--daemon", action="store_true", help="守护模式")
    parser.add_argument("--dry-run", action="store_true", help="试运行（不移动文件）")
    parser.add_argument("--init", action="store_true", help="初始化收件箱")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--workspace", metavar="DIR",
                        help="工作区根目录（自动推导 inbox/cases/logs）")
    parser.add_argument("--inbox", metavar="DIR",
                        help="收件箱路径（覆盖 .env/config）")
    parser.add_argument("--cases-dir", metavar="DIR", dest="cases_dir",
                        help="案件文件夹根路径（覆盖 .env/config）")
    parser.add_argument("--log-dir", metavar="DIR", dest="log_dir",
                        help="日志目录（覆盖 .env/config）")
    args = parser.parse_args()

    # This gate is enforced inside the daemon executable as well as by the
    # GUI/shell controllers.  A direct ``auto_filer.py --daemon`` invocation
    # must never silently turn on unattended filing.
    control_dir = Path(__file__).resolve().parent
    if args.daemon and not unattended_filing_enabled(control_dir):
        print(
            "❌ 无人值守守护已锁定：请在私有 .env 中明确设置 "
            "ALLOW_UNATTENDED_FILING=true 后再启动。",
            file=sys.stderr,
        )
        return 2

    config_path = args.config or str(Path(__file__).parent / "config.yaml")
    if not Path(config_path).exists():
        config_path = str(Path(__file__).parent.parent / "auto_filer" / "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # Compatibility adapter: canonical matter records are authoritative for
    # archive dimensions while legacy rule fields remain available to the
    # existing engine during migration.
    config = config or {}
    config["cases"] = [runtime_case_view(case) for case in config.get("cases", [])]

    # 第一层：.env 覆盖
    for k_env, k_cfg in [
        ("INBOX_DIR", "inbox_dir"), ("CASES_DIR", "case_root"),
        ("LOG_DIR", "log_dir"), ("ARCHIVE_MODE", "mode")
    ]:
        if ENV.get(k_env):
            config["global"][k_cfg] = ENV[k_env]
    if ENV.get("RENAME_FILES"):
        config["global"]["rename_files"] = ENV["RENAME_FILES"].lower() == "true"

    # 第二层：CLI 参数覆盖（最高优先级）
    if args.workspace:
        ws = Path(args.workspace).resolve()
        config["global"]["inbox_dir"] = str(ws / "inbox")
        config["global"]["case_root"] = str(ws / "cases")
        config["global"]["log_dir"]   = str(ws / "logs")
    if args.inbox:
        config["global"]["inbox_dir"] = str(Path(args.inbox).resolve())
    if args.cases_dir:
        config["global"]["case_root"] = str(Path(args.cases_dir).resolve())
    if args.log_dir:
        config["global"]["log_dir"] = str(Path(args.log_dir).resolve())

    # 相对路径 → 绝对路径
    script_dir = Path(__file__).parent.resolve()
    for key in ["inbox_dir", "case_root", "log_dir"]:
        p = Path(config["global"][key])
        if not p.is_absolute():
            config["global"][key] = str(script_dir / p)

    # 日志配置
    log_dir = Path(config["global"]["log_dir"])
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.getLogger("auto_filer").error(
            "日志目录无法创建，安全停止：%s（%s）", log_dir, type(exc).__name__)
        raise
    logger = logging.getLogger("auto_filer")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    fh = logging.FileHandler(log_dir / f"filer_{datetime.now().strftime('%Y%m')}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(ch)
    tighten_private_permissions(
        env_path=Path(__file__).parent.parent / ".env",
        config_path=Path(config_path),
        logs_dir=log_dir,
        logger=logger,
    )

    inbox = Path(config["global"]["inbox_dir"])

    if args.init:
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "README.txt").write_text(
            "═══════════════════════════════════════════════════\n"
            "  材料归档收件箱 — 丢文件到这里自动归档\n"
            "═══════════════════════════════════════════════════\n\n"
            "支持格式: docx / PDF / 图片(JPG/PNG) / Excel / txt\n"
            "扫描版 PDF 和照片会自动 OCR 识别\n"
            "命名规范: YYMMDD 案件简称 文书类型 vX X\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
            encoding="utf-8"
        )
        logger.info(f"✅ 收件箱就绪: {inbox}")
        return

    if args.once or (not args.daemon):
        logger.info("🔧 单次处理模式\n")
        inbox.mkdir(parents=True, exist_ok=True)
        files = [f for f in inbox.iterdir() if f.is_file()]
        if not files:
            logger.info("📭 收件箱为空")
            return
        logger.info(f"📬 {len(files)} 个文件待处理\n" + "=" * 50)
        ok = skip = 0
        for f in sorted(files):
            try:
                if process_file(f, config, logger, args.dry_run):
                    ok += 1
                else:
                    skip += 1
            except Exception as e:
                logger.error(f"   ❌ {f.name}: {e}")
                skip += 1
        logger.info("=" * 50 + f"\n📊 {ok} 成功 / {skip} 跳过 / {len(files)} 总计")

    elif args.daemon:
        instance = None
        obs = None
        stop_event = threading.Event()

        try:
            try:
                # The daemon owns the lifetime lock and publishes a verifiable
                # starting record before any slow optional startup work.  This
                # makes cross-process stop safe even while readiness is pending.
                instance = acquire_daemon_instance(control_dir, command=sys.argv)
                if instance.should_stop_requested():
                    # Do not return from the daemon branch here.  Keep the
                    # cleanup ownership in the surrounding finally block so
                    # the instance lock and state files are always released.
                    stop_event.set()
            except DaemonAlreadyRunning as exc:
                logger.error("❌ %s", exc)
                return 2

            # A stop marker can race with instance acquisition.  Keep this
            # fast path inside the same try/finally instead of returning from
            # the daemon branch before the common cleanup below.
            if not stop_event.is_set():
                def _request_stop(_signum, _frame):
                    stop_event.set()

                signal.signal(signal.SIGTERM, _request_stop)
                signal.signal(signal.SIGINT, _request_stop)

                try:
                    from watchdog.observers import Observer
                    from watchdog.events import FileSystemEventHandler
                except ImportError:
                    logger.error("❌ watchdog 未安装: pip3 install watchdog")
                    return 1

                inbox.mkdir(parents=True, exist_ok=True)
                logger.info(f"👀 监听收件箱: {inbox}\n   Ctrl+C 停止\n")
                intake = _DaemonIntake(config, logger)
                obs = Observer()
                obs.schedule(
                    _make_daemon_event_handler(FileSystemEventHandler, intake),
                    str(inbox),
                    recursive=False,
                )
                # Publish readiness only after the watchdog observer is running.
                obs.start()
                instance.publish_ready(sys.argv)
                # Files copied before daemon startup do not emit a new event;
                # scan only after the observer is live so the shared pending
                # set closes the scan/watchdog race.
                _scan_inbox(inbox, intake)
                while not stop_event.wait(1.0):
                    if instance.should_stop_requested():
                        break
        except KeyboardInterrupt:
            logger.info("\n🛑 已停止")
        except Exception as exc:
            logger.error("❌ 守护进程启动失败：%s: %s", type(exc).__name__, exc)
            return 1
        finally:
            try:
                if obs is not None:
                    obs.stop()
                    obs.join()
            finally:
                if instance is not None:
                    instance.close()
        logger.info("\n🛑 已停止")

    return 0

if __name__ == "__main__":
    raise SystemExit(main() or 0)
