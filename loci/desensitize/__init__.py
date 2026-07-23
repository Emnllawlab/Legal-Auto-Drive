"""Qwen 本地法律文档脱敏工具 —— 核心引擎。

四层：rules(正则+校验位) -> ner(Qwen语义) -> merge(span 融合)
      -> pseudonymize(化名一致性+加密回填)
输出：纯文本 / docx 保排版 / PDF 真实涂改 / 图片·扫描件 OCR 涂改。
"""
from .model import Span, canon
from .rules import extract
from .rules_en import extract_en
from .merge import merge_spans
from .pseudonymize import (
    Registry, pseudonymize, render, restore, build_restore_map,
    vault_save, vault_load,
)
from .pipeline import desensitize_text, analyze_text, apply_review, desensitize_md_file
from .to_markdown import to_markdown
from .ner import QwenNER, health_check
from .pseudonymize import plan
from .docx_io import desensitize_docx
from .pdf_io import desensitize_pdf
from .image_io import desensitize_image
from .ocr import RapidOCREngine, PaddleOCREngine, make_ocr
from .seal_detect import detect_seals
from .batch import desensitize_tree, desensitize_files

__all__ = [
    "Span", "canon", "extract", "extract_en", "merge_spans",
    "Registry", "pseudonymize", "render", "plan", "restore", "build_restore_map",
    "vault_save", "vault_load", "desensitize_text", "analyze_text", "apply_review",
    "QwenNER", "health_check",
    "desensitize_docx", "desensitize_pdf", "desensitize_image",
    "RapidOCREngine", "PaddleOCREngine", "make_ocr", "detect_seals",
    "desensitize_tree", "desensitize_files", "desensitize_md_file", "to_markdown",
]
