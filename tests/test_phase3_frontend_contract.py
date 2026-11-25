import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Phase3FrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "loci/templates/index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "loci/static/app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "loci/static/style.css").read_text(encoding="utf-8")
        self.app = (ROOT / "loci/app.py").read_text(encoding="utf-8")
        self.decision = (ROOT / "auto_filer/decision.py").read_text(encoding="utf-8")

    def test_upload_is_preview_first_and_has_single_decision_card(self):
        self.assertIn('id="prefill-overlay"', self.html)
        self.assertIn('id="pf-decision"', self.html)
        self.assertIn('id="btn-analyze-prefill">预览本批次', self.html)
        self.assertIn("确认并提交", self.html)
        self.assertIn("稍后处理", self.html)
        self.assertIn("decision/analyze-file", self.js)
        self.assertIn("decisions/", self.js)
        self.assertIn("/api/inbox/upload", self.js)
        self.assertIn("stage_only", self.js)

    def test_decision_card_exposes_evidence_and_manual_fact_markers(self):
        for token in ("pf-text-preview", "pf-evidence-line", "pf-manual-mark",
                      "pf-decision-conflicts", "pf-candidate-block", "再次分析"):
            self.assertIn(token, self.js + self.css)
        self.assertIn("manual_values", self.decision)
        self.assertIn("decisionIsStale", self.js)

    def test_narrow_layout_has_decision_card_breakpoint(self):
        self.assertIn("@media (max-width: 760px)", self.css)
        self.assertIn(".pf-decision-layout", self.css)

    def test_staging_has_restore_and_discard_controls(self):
        for token in ("staging-panel", "staging-list", "恢复决策", "放弃",
                      "/api/inbox/staging", "/api/inbox/staging/file",
                      "/api/inbox/staging/discard"):
            self.assertIn(token, self.html + self.js)

    def test_review_reuses_the_decision_card(self):
        for token in ("查看决策", "openReviewDecision", "/api/review/file",
                      "batch_id: item.batch_id || 'review'"):
            self.assertIn(token, self.html + self.js)

    def test_intake_previews_and_limits_initial_stage_folders(self):
        for token in ("/api/intake/preview", "预览建案", "intakeStageField",
                      "只创建当前阶段", "创建所选阶段目录"):
            self.assertIn(token, self.html + self.js)

    def test_desensitize_workbench_has_explicit_case_file_selection(self):
        for token in ("id=\"desk-case\"", "id=\"desk-file\"",
                      "id=\"desk-refresh-files\"", "id=\"desk-file-status\"",
                      "/api/desensitize/files", "optgroup", "保留案件文件夹层级",
                      "目录有读取警告"):
            self.assertIn(token, self.html + self.js)

    def test_desensitize_workbench_requires_explicit_finder_source_and_output(self):
        for token in ("desk-pick-file", "从访达选择文件", "desk-open-file",
                      "/api/desensitize/open-file", "desk-pick-output",
                      "/api/desensitize/pick-output", "output_id",
                      "保存到工作区", "用户选择"):
            self.assertIn(token, self.html + self.js)

    def test_desensitize_workbench_has_explicit_route_split_and_custom_scope(self):
        for token in ("data-desk-route=\"case\"", "data-desk-route=\"standalone\"",
                      "案件内材料", "独立文件", "desk-case-source",
                      "desk-standalone-source", "自定义范围", "desk-scope-options",
                      "deskSelectedCategories", "categories", "desk-scope-all",
                      "scope-label", "身份与主体", "联系方式与设备", "财务与账户", "案件与程序"):
            self.assertIn(token, self.html + self.js)
        self.assertNotIn("全不选", self.html + self.js)
        # 显示标签应使用完整字符串；label[1] 会把“人名/公司”等截成单字。
        self.assertNotIn("label[1]", self.js)

    def test_cases_are_complete_cards_in_an_independent_scroll_viewport(self):
        self.assertIn('id="cases-list"', self.html)
        self.assertIn("#cases-list", self.css)
        self.assertIn("overflow-y: auto", self.css)
        self.assertIn("flex: 0 0 auto", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)

    def test_desensitize_preview_is_primary_surface_and_mixed_language_is_enabled(self):
        self.assertIn("#sec-desk", self.css)
        self.assertIn("min-height: 260px", self.css)
        self.assertIn('Registry(lang="both")', self.app)
        self.assertIn('lang="both"', self.app)

    def test_manual_text_selection_uses_stable_dom_offsets_and_preserves_range(self):
        for token in ("deskDomTextOffset", "deskCaptureSelection", "requestAnimationFrame",
                      "addEventListener('pointerup'", "addEventListener('keyup'",
                      "desk-picker').addEventListener('mousedown", "选区已失效",
                      "已补标："):
            self.assertIn(token, self.js)

    def test_native_webview_enables_text_selection(self):
        run_py = (ROOT / "loci" / "run.py").read_text(encoding="utf-8")
        self.assertIn("text_select=True", run_py)
        css = (ROOT / "loci" / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("-webkit-user-select: text", css)


if __name__ == "__main__":
    unittest.main()
