import unittest

from .normalization import normalize_text
from .pipeline import analyze_text, desensitize_text
from .pseudonymize import Registry


class NormalizationTest(unittest.TestCase):
    def test_fullwidth_and_internal_spaces(self):
        text = "委托人：张 三；电话：１６６－００００－１２３４；邮箱：quill．case＠example．invalid"
        spans = analyze_text(text, Registry(lang="both"), lang="both")
        values = {(s["text"], s["label"]) for s in spans}
        self.assertIn(("张三", "PERSON"), values)
        self.assertTrue(any(label in ("PHONE", "MOBILE") and "１６６" in value
                            for value, label in values))
        self.assertTrue(any(label == "EMAIL" and "quill" in value for value, label in values))

    def test_cross_page_entity_maps_to_fragments_without_masking_header(self):
        text = (
            "签约方示例精密制造（苏州）有\n\f"
            "——第2页/共8页｜机密——\n限公司，邮箱case.team@\n\f"
            "——第3页/共8页｜机密——\nexample.invalid"
        )
        spans = analyze_text(text, Registry(lang="both"), lang="both")
        company = next(s for s in spans if s["label"] in ("COMPANY", "COMPANY_SUFFIX"))
        email = next(s for s in spans if s["label"] == "EMAIL")
        self.assertGreaterEqual(len(company["fragments"]), 2)
        self.assertGreaterEqual(len(email["fragments"]), 2)
        masked, _ = desensitize_text(text, Registry(lang="both"), lang="both")
        self.assertNotIn("示例精密制造", masked)
        self.assertNotIn("case.team", masked)
        self.assertIn("第2页/共8页", masked)
        self.assertIn("第3页/共8页", masked)

    def test_category_filter_applies_to_registered_surface_rescan(self):
        reg = Registry(lang="zh")
        desensitize_text("委托人张三，身份证号110101199001011234。", reg, lang="zh")
        masked, _ = desensitize_text(
            "张三再次确认，身份证号110101199001011234。",
            reg, lang="zh", cats={"ID_CARD"},
        )
        self.assertIn("张三", masked)
        self.assertNotIn("110101199001011234", masked)

    def test_allowlist_applies_to_registered_surface_rescan(self):
        reg = Registry(lang="zh")
        desensitize_text("委托人张三。", reg, lang="zh")
        masked, _ = desensitize_text(
            "张三再次出现。", reg, lang="zh", allow={"张三"},
        )
        self.assertIn("张三", masked)

    def test_redaction_policy_controls_legal_references(self):
        text = "案号：（2026）沪0115民初421号；请求人民币8,765,432.10元；委托人张三。"
        strict = analyze_text(text, Registry(lang="zh"), lang="zh", cats="strict")
        public = analyze_text(text, Registry(lang="zh"), lang="zh", cats="public_legal")
        self.assertIn("CASE_NO", {s["label"] for s in strict})
        self.assertIn("MONEY", {s["label"] for s in strict})
        self.assertNotIn("CASE_NO", {s["label"] for s in public})
        self.assertNotIn("MONEY", {s["label"] for s in public})
        self.assertIn("PERSON", {s["label"] for s in public})


if __name__ == "__main__":
    unittest.main()
