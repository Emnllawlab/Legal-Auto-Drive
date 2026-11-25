"""Canonical matter/proceeding/party model used during the Loci migration.

The current application stores matter records as flat dictionaries in
``config.yaml``.  This module provides a loss-minimising adapter around that
format.  It deliberately does not write files or move documents; callers can
use the canonical representation while the legacy archive engine remains
unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


SCENARIO_ALIASES = {
    "通用": "通用",
    "generic": "通用",
    "中国民事": "中国民事",
    "民事": "中国民事",
    "civil": "中国民事",
    "中国诉讼": "中国民事",
    "仲裁": "仲裁",
    "arbitration": "仲裁",
    "中国刑事": "中国刑事",
    "刑事": "中国刑事",
    "criminal": "中国刑事",
    "美国民事": "美国民事",
    "美国诉讼": "美国民事",
    "us_civil": "美国民事",
    "刑民交叉": "刑民交叉",
    "mixed": "刑民交叉",
}
VALID_SCENARIOS = frozenset(SCENARIO_ALIASES.values())


class DomainError(ValueError):
    """Raised when a matter record cannot be represented safely."""


def normalize_scenario(value: str | None) -> str:
    value = str(value or "通用").strip()
    try:
        return SCENARIO_ALIASES[value]
    except KeyError as exc:
        raise DomainError(f"未知程序场景：{value}") from exc


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = _text(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _list_value(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _case_numbers(value: Any) -> list[str]:
    if isinstance(value, dict):
        values = value.values()
    else:
        values = _list_value(value)
    numbers: list[str] = []
    for item in values:
        if isinstance(item, dict):
            numbers.extend(_case_numbers(item.get("case_number") or item.get("number")))
        else:
            numbers.append(_text(item))
    return _unique(numbers)


@dataclass
class Party:
    id: str = ""
    name: str = ""
    file_alias: str = ""
    aliases: list[str] = field(default_factory=list)
    side: str = "unknown"
    procedural_roles: list[str] = field(default_factory=list)
    source_priority: int | None = None

    @classmethod
    def from_value(cls, value: Any, *, side: str = "unknown") -> "Party":
        if isinstance(value, dict):
            return cls(
                id=_text(value.get("id")),
                name=_text(value.get("name") or value.get("party") or value.get("value")),
                file_alias=_text(value.get("file_alias") or value.get("alias")),
                aliases=_unique(_list_value(value.get("aliases"))),
                side=_text(value.get("side")) or side,
                procedural_roles=_unique(_list_value(value.get("procedural_roles") or value.get("roles"))),
                source_priority=value.get("source_priority"),
            )
        return cls(name=_text(value), side=side)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Proceeding:
    id: str = ""
    jurisdiction: str = ""
    scenario: str = "通用"
    stage: str = ""
    tribunal: str = ""
    case_numbers: list[str] = field(default_factory=list)
    status: str = "active"

    def __post_init__(self) -> None:
        self.scenario = normalize_scenario(self.scenario)
        self.case_numbers = _unique(self.case_numbers)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Matter:
    schema_version: int = 3
    id: str = ""
    key: str = ""
    name: str = ""
    file_abbr: str = ""
    root_path: str = ""
    status: str = "active"
    cause: str = ""
    role: str = ""
    parties: list[Party] = field(default_factory=list)
    proceedings: list[Proceeding] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    layout: str = ""

    @classmethod
    def from_legacy_case(cls, case: dict[str, Any]) -> "Matter":
        if not isinstance(case, dict):
            raise DomainError("案件记录必须是对象")
        key = _text(case.get("key"))
        name = _text(case.get("name"))
        if not key or not name:
            raise DomainError("案件必须包含 key 和 name")

        scenario = normalize_scenario(case.get("scenario"))
        parties: list[Party] = []
        for field_name, side in (
            ("party_us", "our"),
            ("party_op", "opposing"),
            ("party_third", "third_party"),
            ("party_court", "court"),
        ):
            for value in _list_value(case.get(field_name)):
                party = Party.from_value(value, side=side)
                if party.name:
                    parties.append(party)

        raw_proceedings = case.get("proceedings")
        proceedings: list[Proceeding] = []
        if isinstance(raw_proceedings, list):
            for item in raw_proceedings:
                if not isinstance(item, dict):
                    raise DomainError("proceedings 中每一项必须是对象")
                proceedings.append(Proceeding(
                    id=_text(item.get("id")),
                    jurisdiction=_text(item.get("jurisdiction")),
                    scenario=item.get("scenario") or scenario,
                    stage=_text(item.get("stage")),
                    tribunal=_text(item.get("tribunal")),
                    case_numbers=_case_numbers(item.get("case_numbers") or item.get("case_number")),
                    status=_text(item.get("status")) or "active",
                ))
        elif isinstance(case.get("case_numbers"), dict):
            for stage, number in case["case_numbers"].items():
                proceedings.append(Proceeding(scenario=scenario, stage=_text(stage), case_numbers=_case_numbers(number)))

        legacy_numbers = _case_numbers(case.get("case_number"))
        if not proceedings:
            proceedings = [Proceeding(
                jurisdiction=_text(case.get("jurisdiction")),
                scenario=scenario,
                stage=_text(case.get("stage")),
                tribunal=_text(case.get("tribunal") or case.get("court")),
                case_numbers=legacy_numbers,
            )]
        elif legacy_numbers:
            known = {number for proc in proceedings for number in proc.case_numbers}
            additions = [number for number in legacy_numbers if number not in known]
            if additions:
                proceedings[0].case_numbers.extend(additions)

        root_path = _text(case.get("root_path") or case.get("cases_dir"))
        if root_path and not Path(root_path).expanduser().is_absolute():
            raise DomainError("root_path/cases_dir 必须是绝对路径")

        return cls(
            id=_text(case.get("id") or case.get("matter_id")),
            key=key,
            name=name,
            file_abbr=_text(case.get("file_abbr") or case.get("abbr") or key),
            root_path=root_path,
            status=_text(case.get("status")) or "active",
            cause=_text(case.get("cause")),
            role=_text(case.get("role")),
            parties=parties,
            proceedings=proceedings,
            keywords=_unique(_list_value(case.get("keywords"))),
            layout=_text(case.get("layout")),
        )

    @classmethod
    def from_canonical_dict(cls, value: dict[str, Any]) -> "Matter":
        """Load the staged ``loci_matter`` shape without consulting legacy fields."""
        if not isinstance(value, dict):
            raise DomainError("loci_matter 必须是对象")
        parties = [Party.from_value(item) for item in _list_value(value.get("parties"))]
        proceedings = []
        for item in _list_value(value.get("proceedings")):
            if not isinstance(item, dict):
                raise DomainError("loci_matter.proceedings 中每一项必须是对象")
            proceedings.append(Proceeding(
                id=_text(item.get("id")),
                jurisdiction=_text(item.get("jurisdiction")),
                scenario=item.get("scenario") or "通用",
                stage=_text(item.get("stage")),
                tribunal=_text(item.get("tribunal")),
                case_numbers=_case_numbers(item.get("case_numbers") or item.get("case_number")),
                status=_text(item.get("status")) or "active",
            ))
        return cls(
            schema_version=int(value.get("schema_version", 3)),
            id=_text(value.get("id")),
            key=_text(value.get("key")),
            name=_text(value.get("name")),
            file_abbr=_text(value.get("file_abbr") or value.get("key")),
            root_path=_text(value.get("root_path")),
            status=_text(value.get("status")) or "active",
            cause=_text(value.get("cause")),
            role=_text(value.get("role")),
            parties=[party for party in parties if party.name],
            proceedings=proceedings or [Proceeding(scenario="通用")],
            keywords=_unique(_list_value(value.get("keywords"))),
            layout=_text(value.get("layout")),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value

    def to_legacy_view(self) -> dict[str, Any]:
        """Return fields understood by the current archive engine."""
        groups = {"our": [], "opposing": [], "third_party": [], "court": []}
        for party in self.parties:
            groups.setdefault(party.side, []).append(party.name)
        case_numbers = [number for proc in self.proceedings for number in proc.case_numbers]
        result = {
            "key": self.key,
            "name": self.name,
            "file_abbr": self.file_abbr,
            "cases_dir": self.root_path,
            "scenario": self.proceedings[0].scenario if self.proceedings else "通用",
            "role": self.role,
            "cause": self.cause,
            "keywords": list(self.keywords),
            "party_us": groups["our"],
            "party_op": groups["opposing"],
            "party_third": groups["third_party"],
            "party_court": groups["court"],
            "case_number": case_numbers[0] if len(case_numbers) == 1 else case_numbers,
            "case_numbers": case_numbers,
            "proceedings": [proc.to_dict() for proc in self.proceedings],
        }
        return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def canonicalize_case(case: dict[str, Any]) -> dict[str, Any]:
    """Convert one legacy case into the new, serialisable representation."""
    return Matter.from_legacy_case(case).to_dict()


def runtime_case_view(case: dict[str, Any]) -> dict[str, Any]:
    """Expose a canonical record to legacy callers during the transition.

    Extra legacy fields are retained because the old rule engine still uses
    fields such as court aliases and import paths.  Canonical fields win only
    for the dimensions represented by ``loci_matter``.
    """
    if not isinstance(case, dict) or not isinstance(case.get("loci_matter"), dict):
        return case
    matter = Matter.from_canonical_dict(case["loci_matter"])
    result = dict(case)
    result.update(matter.to_legacy_view())
    result["loci_matter"] = case["loci_matter"]
    return result
