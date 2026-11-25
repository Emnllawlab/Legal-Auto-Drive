import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .docx_io import desensitize_docx
from .pdf_io import desensitize_pdf
from .pseudonymize import Registry
from .to_markdown import pdf_to_md


class FormatIOTests(unittest.TestCase):
    """验证保留原格式的 PDF/DOCX 路径也服从统一识别政策。"""

    def test_docx_honors_category_filter_and_english_rules(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "in.docx", Path(td) / "out.docx"
            doc = Document()
            doc.add_paragraph("Counsel Example Counsel; email counsel@example.invalid")
            doc.save(src)

            n = desensitize_docx(src, dst, Registry(lang="both"),
                                 lang="both", cats={"EMAIL"})
            self.assertGreaterEqual(n, 1)
            out = Document(dst)
            text = "\n".join(p.text for p in out.paragraphs)
            self.assertIn("Example Counsel", text)
            self.assertNotIn("counsel@example.invalid", text)

    def test_pdf_honors_category_filter_and_english_rules(self):
        import fitz

        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "in.pdf", Path(td) / "out.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Counsel Example Counsel; email counsel@example.invalid")
            doc.save(src)
            doc.close()

            n = desensitize_pdf(src, dst, Registry(lang="both"),
                                lang="both", cats={"EMAIL"})
            self.assertGreaterEqual(n, 1)
            out = fitz.open(dst)
            text = "\n".join(p.get_text() for p in out)
            out.close()
            self.assertIn("Example Counsel", text)
            self.assertNotIn("counsel@example.invalid", text)

    def test_pdf_word_blocks_cover_entity_split_across_lines(self):
        import fitz

        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / "wrapped.pdf", Path(td) / "wrapped_out.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Counsel Example Counsel; email counsel@")
            page.insert_text((72, 90), "example.invalid")
            doc.save(src)
            doc.close()

            n = desensitize_pdf(src, dst, Registry(lang="both"),
                                lang="both", cats={"EMAIL"})
            out = fitz.open(dst)
            text = "\n".join(p.get_text() for p in out)
            out.close()
            self.assertGreaterEqual(n, 1)
            self.assertNotIn("counsel@", text)
            self.assertNotIn("example.invalid", text)
            self.assertEqual(text.count("〔EMAIL1〕"), 1)

    def test_pdf_with_repeated_court_header_uses_ocr_for_sparse_text_layer(self):
        """页眉文字层存在时，正文扫描图像仍应触发逐页 OCR。"""
        import fitz
        import importlib

        markdown_module = importlib.import_module(".to_markdown", package=__package__)

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "scanned-court-pdf.pdf"
            doc = fitz.open()
            for page_no in range(1, 3):
                page = doc.new_page()
                page.insert_text(
                    (72, 72),
                    f"Case 3:20-cv-00152-SLG Document 1 Filed 06/25/20 "
                    f"Page {page_no} of 2",
                )
            doc.save(src)
            doc.close()

            with patch.object(
                markdown_module,
                "_ocr_page",
                side_effect=["Complaint body page one", "Plaintiff body page two"],
            ) as ocr:
                text = pdf_to_md(str(src), ocr_engine=object())

            self.assertIn("Complaint body page one", text)
            self.assertIn("Plaintiff body page two", text)
            self.assertEqual(ocr.call_count, 2)

    def test_manual_span_is_applied_even_when_rule_does_not_detect_it(self):
        from .pipeline import apply_review

        text = "本材料中的特殊主体为星河项目公司。"
        start = text.index("星河项目公司")
        masked, applied = apply_review(
            text,
            [{"start": start, "end": start + len("星河项目公司"), "label": "COMPANY"}],
            Registry(lang="both"),
            mode="blackout",
        )
        self.assertEqual(len(applied), 1)
        self.assertNotIn("星河项目公司", masked)
        self.assertIn("█", masked)


if __name__ == "__main__":
    unittest.main()
