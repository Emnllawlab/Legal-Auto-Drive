"""Validation, versioning and sandbox helpers for Loci filing rules.

The production rule file is edited by people, so this module is intentionally
conservative: it rejects malformed rules before they reach the classifier,
warns about likely maintenance hazards, and keeps a JSONL snapshot history for
inspection and rollback.  It does not decide whether a legal taxonomy is
substantively correct; that remains a human review decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import
    from .safety import FileLock, validate_relative_target
except ImportError:  # direct import from a legacy script
    from safety import FileLock, validate_relative_target


KNOWN_SCENARIOS = {"通用", "中国民事", "仲裁", "中国刑事", "美国民事", "刑民交叉"}
VALID_SOURCES = {"court", "ours", "opposing"}
KEYWORD_FIELDS = ("keywords", "title_keywords", "content_keywords")


def _list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _stable_rules(rules: list[dict]) -> str:
    return json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rules_digest(rules: list[dict]) -> str:
    """Return a stable digest suitable for UI version labels and audit logs."""
    return hashlib.sha256(_stable_rules(rules).encode("utf-8")).hexdigest()


def validate_rules(rules: Any) -> dict:
    """Validate a rule list and return errors, warnings and summary statistics.

    Errors are blocking.  Warnings are deliberately non-blocking because the
    existing rule set contains broad-but-intentional catch-all rules.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(rules, list):
        return {"ok": False, "errors": ["rules 必须是数组"], "warnings": [],
                "stats": {"count": 0, "scenarios": {}, "targets": 0},
                "digest": rules_digest([])}

    seen: dict[tuple, int] = {}
    scenario_counts: dict[str, int] = {}
    target_count = 0
    empty_count = 0
    for index, rule in enumerate(rules, start=1):
        label = f"第 {index} 条规则"
        if not isinstance(rule, dict):
            errors.append(f"{label}必须是对象")
            continue

        target = _text(rule.get("target"))
        if not target:
            errors.append(f"{label}缺少 target（目标文件夹）")
        else:
            try:
                validate_relative_target(target, f"{label}的 target")
                target_count += 1
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))

        scenario = _text(rule.get("scenario")) or "通用"
        scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
        if scenario not in KNOWN_SCENARIOS:
            errors.append(f"{label}的 scenario 不受支持：{scenario}")

        source = _text(rule.get("source"))
        if source and source not in VALID_SOURCES:
            errors.append(f"{label}的 source 必须是 court / ours / opposing")

        normalized_fields: list[tuple[str, ...]] = []
        has_keyword = False
        for field in KEYWORD_FIELDS:
            value = rule.get(field)
            if value is None:
                values: list[Any] = []
            elif not isinstance(value, list):
                errors.append(f"{label}的 {field} 必须是数组")
                values = []
            else:
                values = value
            clean: list[str] = []
            for item in values:
                text = _text(item)
                if not text:
                    warnings.append(f"{label}的 {field} 含空关键词，将降低可维护性")
                    continue
                clean.append(text)
                has_keyword = True
                if len(text) == 1:
                    warnings.append(f"{label}含单字符关键词「{text}」，容易误命中")
            normalized_fields.append(tuple(sorted(set(clean), key=str.casefold)))

        if not has_keyword:
            empty_count += 1
            errors.append(f"{label}没有 keywords / title_keywords / content_keywords")

        signature = (scenario, target, source, *normalized_fields)
        if signature in seen:
            warnings.append(f"第 {index} 条规则与第 {seen[signature]} 条规则完全重复")
        else:
            seen[signature] = index

        if rule.get("content_keywords") and not rule.get("title_keywords") and not rule.get("keywords"):
            warnings.append(f"{label}只有正文关键词：正文提及不应单独决定文书性质")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {"count": len(rules), "scenarios": scenario_counts,
                   "targets": target_count, "empty_rules": empty_count},
        "digest": rules_digest(rules),
    }


def _history_path(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def append_rule_history(path: Path, old_rules: list[dict], new_rules: list[dict],
                        *, actor: str = "loci", restored_from: str | None = None) -> dict:
    """Append one complete new-rule snapshot to a JSONL audit file."""
    path = _history_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "old_digest": rules_digest(old_rules),
        "digest": rules_digest(new_rules),
        "count": len(new_rules),
        "rules": new_rules,
    }
    if restored_from:
        entry["restored_from"] = restored_from
    with FileLock(path.with_name(path.name + ".lock"), blocking=True):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            fh.flush()
    return entry


def load_rule_history(path: Path, limit: int = 50) -> list[dict]:
    """Return newest valid entries first, without exposing malformed lines."""
    path = _history_path(path)
    if not path.exists():
        return []
    entries: list[dict] = []
    with FileLock(path.with_name(path.name + ".lock"), blocking=True):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("rules"), list):
            entries.append(entry)
        if len(entries) >= max(1, min(int(limit), 200)):
            break
    return entries
