"""Read-only taxonomy access for the staged Loci migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_PATH = Path(__file__).with_name("taxonomy.yaml")
CONFIG_PATH = Path(__file__).with_name("config.yaml")


class TaxonomyError(ValueError):
    pass


def load_taxonomy(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    validate_taxonomy(data)
    return data


def validate_taxonomy(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TaxonomyError("taxonomy 必须是对象")
    if data.get("schema_version") != 1:
        raise TaxonomyError("taxonomy schema_version 必须为 1")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, dict) or not scenarios:
        raise TaxonomyError("taxonomy.scenarios 不能为空")
    document_types = data.get("document_types")
    if not isinstance(document_types, list):
        raise TaxonomyError("taxonomy.document_types 必须是列表")
    ids: set[str] = set()
    for item in document_types:
        if not isinstance(item, dict) or not item.get("id") or not item.get("label"):
            raise TaxonomyError("每个 document_type 必须有 id 和 label")
        if item["id"] in ids:
            raise TaxonomyError(f"重复的 document_type id：{item['id']}")
        ids.add(item["id"])
        for scenario in item.get("scenarios", []):
            if scenario not in scenarios:
                raise TaxonomyError(f"document_type 使用未知场景：{scenario}")


def document_types_for_scenario(scenario: str, path: str | Path = DEFAULT_PATH) -> list[dict[str, Any]]:
    data = load_taxonomy(path)
    if scenario not in data["scenarios"]:
        raise TaxonomyError(f"未知场景：{scenario}")
    return [
        item for item in data["document_types"]
        if not item.get("scenarios") or scenario in item["scenarios"]
    ]


def load_legacy_rules(path: str | Path = CONFIG_PATH) -> list[dict[str, Any]]:
    """Read legacy rules without making them part of the new taxonomy."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    rules = data.get("rules", [])
    return rules if isinstance(rules, list) else []
