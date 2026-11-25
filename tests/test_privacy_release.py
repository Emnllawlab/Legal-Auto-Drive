import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import loci.app as app_module
from auto_filer.config_io import (
    dump_yaml,
    set_private_umask,
    tighten_private_permissions,
    update_yaml,
)
from auto_filer.ledger import FilingLedger


def mode_of(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


class PrivacyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="luna-privacy-")
        self.root = Path(self.tmp.name)
        self.cases_dir = self.root / "cases"
        self.case_dir = self.cases_dir / "Synthetic Matter"
        self.logs_dir = self.root / "logs"
        self.cases_dir.mkdir()
        self.case_dir.mkdir()
        self.logs_dir.mkdir()
        self.case_dir.joinpath("placeholder.txt").write_text("synthetic", encoding="utf-8")
        self.case = {
            "key": "MATTERKEY",
            "abbr": "MATTERKEY",
            "name": "Synthetic Matter",
            "case_number": "（2026）沪01民初1234号",
            "cases_dir": str(self.cases_dir),
        }
        self.paths = {
            "workspace": str(self.root),
            "inbox": str(self.root / "inbox"),
            "cases": str(self.cases_dir),
            "logs": str(self.logs_dir),
        }
        self.patchers = [
            patch.object(app_module, "_DESENS_OK", True),
            patch.object(app_module, "_DESENS_ERR", ""),
            patch.object(app_module, "_config", return_value={"cases": [self.case]}),
            patch.object(app_module, "_paths", return_value=self.paths),
        ]
        for patcher in self.patchers:
            patcher.start()
        app_module._DESENS_JOB.update(
            running=False, case="", i=0, total=0, current="", done=False,
            report=None, error=None, dst="",
        )
        self.client = app_module.app.test_client()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        app_module._DESENS_JOB.update(
            running=False, case="", i=0, total=0, current="", done=False,
            report=None, error=None, dst="",
        )
        self.tmp.cleanup()

    def _post_desensitize(self, payload):
        return self.client.post("/api/cases/MATTERKEY/desensitize", json=payload)

    def test_missing_password_passes_none_to_batch(self):
        captured = {}
        done = threading.Event()

        def fake_tree(*args, **kwargs):
            captured["vault_password"] = kwargs.get("vault_password", "missing")
            done.set()
            return {"total": 0, "processed": 0, "hits": 0, "skipped": [], "errors": []}

        with patch.object(app_module, "Registry", return_value=object()), \
                patch.object(app_module, "make_ocr", return_value=None), \
                patch.object(app_module, "desensitize_tree", side_effect=fake_tree):
            response = self._post_desensitize({})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(done.wait(3), "脱敏 worker 未启动")

        self.assertIsNone(captured["vault_password"])
        self.assertNotIn("_脱敏映射表.vault", str(response.get_json()))
        self.assertNotIn("vault_password", json.dumps(app_module._DESENS_JOB, ensure_ascii=False))

    def test_weak_password_is_rejected_without_starting_worker(self):
        with patch.object(app_module.threading, "Thread") as thread:
            response = self._post_desensitize({"vault_password": "too-short"})

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("too-short", response.get_data(as_text=True))
        thread.assert_not_called()

    def test_identifier_cannot_be_used_as_strong_password(self):
        with patch.object(app_module.threading, "Thread") as thread:
            response = self._post_desensitize({"vault_password": self.case["case_number"]})

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(self.case["case_number"], response.get_data(as_text=True))
        thread.assert_not_called()

    def test_strong_password_reaches_worker_but_not_response_or_status(self):
        secret = "strong vault secret 123"
        captured = {}
        done = threading.Event()

        def fake_tree(*args, **kwargs):
            captured["vault_password"] = kwargs.get("vault_password")
            done.set()
            # Deliberately return a secret-shaped value to verify the task
            # status sanitizer does not expose it.
            return {"echo": secret, "vault_password": secret}

        with patch.object(app_module, "Registry", return_value=object()), \
                patch.object(app_module, "make_ocr", return_value=None), \
                patch.object(app_module, "desensitize_tree", side_effect=fake_tree):
            response = self._post_desensitize({"vault_password": secret})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(done.wait(3), "脱敏 worker 未完成")

        status = self.client.get("/api/desensitize/status")
        self.assertEqual(captured["vault_password"], secret)
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertNotIn(secret, status.get_data(as_text=True))
        self.assertNotIn(secret, json.dumps(app_module._DESENS_JOB, ensure_ascii=False))

    def test_config_and_backups_are_always_private(self):
        config = self.root / "config.yaml"
        dump_yaml(config, {"safe": True})
        self.assertEqual(mode_of(config), 0o600)

        os.chmod(str(config), 0o644)
        update_yaml(config, lambda data: {**(data or {}), "updated": True})
        backups = sorted(self.root.glob("config.yaml.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(mode_of(config), 0o600)
        self.assertEqual(mode_of(backups[0]), 0o600)

    def test_startup_hardening_does_not_recurse_into_materials(self):
        env_file = self.root / ".env"
        config = self.root / "config.yaml"
        env_file.write_text("LOG_DIR=logs\n", encoding="utf-8")
        config.write_text("safe: true\n", encoding="utf-8")
        log_file = self.logs_dir / "archive_log.jsonl"
        log_file.write_text("synthetic log\n", encoding="utf-8")
        nested_material = self.logs_dir / "nested" / "material.txt"
        nested_material.parent.mkdir()
        nested_material.write_text("synthetic material", encoding="utf-8")
        for path in (env_file, config, log_file, nested_material):
            os.chmod(str(path), 0o644)
        os.chmod(str(self.logs_dir), 0o755)

        tighten_private_permissions(env_file, config, self.logs_dir)

        self.assertEqual(mode_of(env_file), 0o600)
        self.assertEqual(mode_of(config), 0o600)
        self.assertEqual(mode_of(log_file), 0o600)
        self.assertEqual(mode_of(self.logs_dir), 0o700)
        self.assertEqual(mode_of(nested_material), 0o644)

    def test_ledger_is_private_when_created_and_reopened(self):
        db = self.logs_dir / "filing_ledger.sqlite3"
        FilingLedger(db)
        self.assertEqual(mode_of(db), 0o600)

        os.chmod(str(db), 0o644)
        FilingLedger(db)
        self.assertEqual(mode_of(db), 0o600)

    def test_private_umask_is_explicit_and_restored_in_test(self):
        original = os.umask(0o022)
        try:
            previous = set_private_umask()
            observed = os.umask(original)
            self.assertEqual(previous, 0o022)
            self.assertEqual(observed, 0o077)
        finally:
            os.umask(original)


if __name__ == "__main__":
    unittest.main()
