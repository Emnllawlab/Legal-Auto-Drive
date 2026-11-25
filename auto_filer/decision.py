"""Conservative, explainable filing decisions.

This module is deliberately side-effect free.  It only produces candidates,
evidence, a destination preview and a risk state.  Moving or renaming a file
belongs to the transaction layer and must happen after human confirmation.
"""

from __future__ import annotations

import re
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:  # package import from tests/Loci
    from .domain import runtime_case_view
except ImportError:  # direct import when auto_filer.py runs as a script
    from domain import runtime_case_view


SOURCE_LABELS = {
    "ours": "我方",
    "opposing": "对方",
    "court": "法院",
    "tribunal": "仲裁机构",
    "third_party": "第三方",
    "expert": "专家/鉴定机构",
    "police": "公安机关",
    "prosecutor": "检察机关",
    "regulator": "监管机构",
    "internal": "内部材料",
}
SOURCE_SENSITIVE = ("我方证据", "对方证据", "我方材料", "对方材料")
US_PARTY_PREFIXES = ("03_Opposing Party Filings", "04_Our Filings")
US_PARTY_PLACEHOLDER = "{us_party_filings}"
SOURCE_OPTIONS = (
    "ours", "opposing", "court", "tribunal", "third_party", "expert",
    "police", "prosecutor", "regulator", "internal",
)
UNRESOLVED_SOURCE_STATUSES = {"unknown", "ambiguous"}


def _values(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _case_numbers(case: dict) -> list[str]:
    values: list[Any] = []
    for field in ("case_number", "case_numbers"):
        value = case.get(field)
        if isinstance(value, dict):
            values.extend(value.values())
        else:
            values.extend(_values(value))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = _text(value)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _party_values(case: dict) -> list[tuple[str, str, list[str]]]:
    result = []
    for field, source in (("party_us", "ours"), ("party_op", "opposing"),
                          ("party_third", "third_party"), ("party_court", "court")):
        for value in _values(case.get(field)):
            if isinstance(value, dict):
                name = _text(value.get("name") or value.get("party"))
                aliases = [_text(value.get("file_alias") or value.get("alias"))]
                aliases += [_text(v) for v in _values(value.get("aliases"))]
            else:
                name = _text(value)
                aliases = []
            aliases = [v for v in dict.fromkeys([name] + aliases) if len(v) >= 2]
            if name:
                result.append((name, source, aliases))
    return result


def _compact(value: str) -> str:
    return re.sub(r"[\s　\-—_/]", "", value.lower())


def _contains(term: str, value: str) -> bool:
    return bool(term) and term.lower() in value.lower()


def _keyword_matches(term: str, value: str) -> bool:
    """Match rule tokens without allowing ASCII terms inside other words.

    Chinese terms intentionally retain substring matching.  For English
    tokens this prevents examples such as ``NDA`` matching ``DEFENDANT`` and
    ``RFP`` matching an unrelated longer identifier.
    """
    term = _text(term)
    if not term:
        return False
    prefix = r"(?<![A-Za-z0-9])" if term[0].isascii() and term[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9])" if term[-1].isascii() and term[-1].isalnum() else ""
    return bool(re.search(prefix + re.escape(term) + suffix, value or "", re.IGNORECASE))


def _pattern_matches(pattern: Any, value: str) -> bool:
    """Match a configured literal or regex without making bad rules fatal."""
    pattern = _text(pattern)
    if not pattern:
        return False
    try:
        return bool(re.search(pattern, value or "", re.IGNORECASE))
    except re.error:
        return _keyword_matches(pattern, value or "")


def _rule_values(rule: dict, field: str) -> list[str]:
    return [_text(value) for value in _values(rule.get(field)) if _text(value)]


def _case_candidates(filename: str, content: str, cases: Iterable[dict]) -> list[dict]:
    filename_l = filename.lower()
    head = (content or "")[:1800].lower()
    body = (content or "").lower()
    compact = _compact(filename + " " + content)
    ranked = []
    for index, raw_case in enumerate(cases):
        case = runtime_case_view(raw_case)
        if case.get("status") == "closed":
            continue
        score = 0
        evidence: list[str] = []
        numbers = _case_numbers(case)
        number_hits = [number for number in numbers if _compact(number) in compact]
        if number_hits:
            score += 1000
            evidence.append("案号命中：" + "、".join(number_hits[:3]))

        key = _text(case.get("key"))
        if key and len(key) >= 2 and key.lower() in filename_l:
            score += 220
            evidence.append(f"文件名案件简称：{key}")

        party_hits = []
        for name, _source, aliases in _party_values(case):
            matched = next((alias for alias in aliases if _contains(alias, filename_l)), None)
            if matched:
                score += min(240, 150 + len(matched) * 3)
                party_hits.append(f"文件名主体：{matched}")
                continue
            matched = next((alias for alias in aliases if _contains(alias, head)), None)
            if matched:
                score += min(130, 75 + len(matched) * 2)
                party_hits.append(f"首页主体：{matched}")
                continue
            # Body-only mentions are intentionally weak.  They cannot select a
            # case when another candidate has a title/header hit.
            matched = next((alias for alias in aliases if _contains(alias, body)), None)
            if matched:
                score += min(18, 5 + len(matched) // 3)
                party_hits.append(f"正文提及：{matched}")
        evidence.extend(party_hits[:5])

        keyword_hits = []
        for keyword in _values(case.get("keywords")):
            keyword = _text(keyword)
            if len(keyword) < 2:
                continue
            if _keyword_matches(keyword, filename_l):
                score += min(100, 50 + len(keyword) * 2)
                keyword_hits.append(f"文件名关键词：{keyword}")
            elif _keyword_matches(keyword, head):
                score += min(50, 20 + len(keyword))
                keyword_hits.append(f"首页关键词：{keyword}")
            elif _keyword_matches(keyword, body):
                score += min(10, 3 + len(keyword) // 2)
        evidence.extend(keyword_hits[:4])
        if score:
            ranked.append({"index": index, "key": case.get("key"),
                           "name": case.get("name"), "score": score,
                           "evidence": evidence, "case": case})
    return sorted(ranked, key=lambda item: (-item["score"], item["index"]))


def _select(ranked: list[dict], minimum: int, margin: int) -> tuple[dict | None, str]:
    if not ranked or ranked[0]["score"] < minimum:
        return None, "unknown"
    if len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] < margin:
        return None, "ambiguous"
    return ranked[0], "selected"


def _us_party_pending_target(target: str) -> str:
    """Collapse a US party filing to a source-neutral review destination."""
    parts = _text(target).split("/")
    if not parts or parts[0] not in US_PARTY_PREFIXES:
        return _text(target)
    rest = parts[1:]
    if not rest:
        return US_PARTY_PLACEHOLDER
    first = rest[0]
    if first.startswith("Pleadings"):
        bucket = "Pleadings"
    elif first.startswith("Motions"):
        bucket = "Motions & Briefs"
    elif first.startswith("Discovery"):
        bucket = "Discovery"
    else:
        bucket = "Pretrial Submissions"
    return f"{US_PARTY_PLACEHOLDER}/{bucket}"


def _rule_candidates(filename: str, content: str, title: str | None,
                     rules: Iterable[dict], allowed_scenarios: set[str] | None,
                     source: str | None) -> list[dict]:
    filename_l = filename.lower()
    title_l = (title or "").lower()
    content_l = (content or "").lower()
    extension = Path(filename).suffix.lower()
    searchable = "\n".join((filename_l, title_l, content_l))
    ranked = []
    for index, rule in enumerate(rules):
        if allowed_scenarios is not None and rule.get("scenario", "通用") not in allowed_scenarios:
            continue
        rule_source = _text(rule.get("source"))
        raw_target = _text(rule.get("target"))
        source_unknown_us_party = (
            rule_source and (source or "unknown") == "unknown"
            and rule.get("scenario") == "美国民事"
            and raw_target.startswith(US_PARTY_PREFIXES)
        )
        # While source is unresolved, US party rules may still identify the
        # semantic document class, but only through a neutral placeholder.
        # All other source-constrained rules remain unavailable.
        if rule_source and rule_source != (source or "unknown") and not source_unknown_us_party:
            continue

        allowed_types = _rule_values(rule, "file_types")
        if allowed_types:
            allowed_types = {
                value.lower() if value.startswith(".") else "." + value.lower()
                for value in allowed_types
            }
            if extension not in allowed_types:
                continue

        negative_contexts = _rule_values(rule, "negative_contexts")
        if any(_pattern_matches(pattern, searchable) for pattern in negative_contexts):
            continue

        required_any = _rule_values(rule, "required_any")
        if required_any and not any(_pattern_matches(pattern, searchable)
                                    for pattern in required_any):
            continue

        score = 0
        content_score = 0
        evidence = []
        for keyword in _values(rule.get("title_keywords")):
            keyword = _text(keyword)
            if _keyword_matches(keyword, title_l):
                # Exact title matches outrank a shorter generic token such as
                # "Exhibit" inside "Exhibit List".
                score += 190 if keyword.lower() == title_l.strip() else 150
                evidence.append(f"标题：{keyword}")
            elif _keyword_matches(keyword, filename_l):
                score += 105
                evidence.append(f"文件名标题：{keyword}")
        for keyword in _values(rule.get("keywords")):
            keyword = _text(keyword)
            if not keyword:
                continue
            if _keyword_matches(keyword, filename_l):
                score += min(100, 55 + len(keyword) * 2)
                evidence.append(f"文件名关键词：{keyword}")
            elif _keyword_matches(keyword, title_l):
                score += min(90, 50 + len(keyword) * 2)
                evidence.append(f"标题关键词：{keyword}")
        for keyword in _values(rule.get("content_keywords")):
            keyword = _text(keyword)
            if _keyword_matches(keyword, content_l):
                content_score += min(25, 8 + len(keyword))
                evidence.append(f"正文特征：{keyword}")
        for pattern in _rule_values(rule, "support_patterns"):
            if _pattern_matches(pattern, searchable):
                score += 35
                evidence.append(f"辅助特征：{pattern}")
        # Body-only signals are capped.  A document cannot be classified just
        # because it mentions several procedural terms in its narrative.
        score += min(content_score, 35)
        if score:
            target = (_us_party_pending_target(raw_target)
                      if source_unknown_us_party else raw_target)
            ranked.append({"index": index, "target": target,
                           "name": rule.get("name") or raw_target.rsplit("/", 1)[-1],
                           "score": score, "evidence": evidence[:6],
                           "priority": int(rule.get("priority", 0) or 0),
                           "source_sensitive": (US_PARTY_PLACEHOLDER in target
                                                or any(token in target for token in SOURCE_SENSITIVE)),
                           "source": source})
    # Our/opposing rule pairs become the same neutral candidate while source
    # is unknown.  Deduplicate them so an exact tie does not create a false
    # document-type ambiguity.
    deduped: dict[tuple[str, str], dict] = {}
    for item in ranked:
        key = (item["target"], _text(item.get("name")).lower())
        old = deduped.get(key)
        if old is None or item["score"] > old["score"]:
            deduped[key] = item
        elif item["score"] == old["score"]:
            old["evidence"] = list(dict.fromkeys(old["evidence"] + item["evidence"]))[:6]
    return sorted(deduped.values(),
                  key=lambda item: (-item["score"], -item["priority"],
                                    -len(item["target"]), item["index"]))


def _procedural_role_sources(case: dict) -> dict[str, str]:
    """Map US caption roles to ours/opposing from the matter-side role."""
    role = _text(case.get("role")).lower().replace("_", "-")
    claimant_side = {"plaintiff", "claimant", "petitioner", "applicant", "原告", "申请人"}
    respondent_side = {"defendant", "respondent", "被告", "被申请人"}
    if role in claimant_side:
        return {
            "plaintiff": "ours", "plaintiffs": "ours", "petitioner": "ours",
            "petitioners": "ours", "applicant": "ours", "applicants": "ours",
            "claimant": "ours", "claimants": "ours", "defendant": "opposing",
            "defendants": "opposing", "respondent": "opposing", "respondents": "opposing",
        }
    if role in respondent_side:
        return {
            "plaintiff": "opposing", "plaintiffs": "opposing", "petitioner": "opposing",
            "petitioners": "opposing", "applicant": "opposing", "applicants": "opposing",
            "claimant": "opposing", "claimants": "opposing", "defendant": "ours",
            "defendants": "ours", "respondent": "ours", "respondents": "ours",
        }
    return {}


def infer_source(content: str, filename: str, case: dict | None,
                 manual_source: str | None = None,
                 extracted_title: str | None = None) -> dict:
    """Infer issuer conservatively; party mentions alone never establish source."""
    if manual_source in SOURCE_OPTIONS:
        return {"kind": manual_source, "status": "manual", "confidence": 1.0,
                "evidence": ["人工选择来源"]}
    case = case or {}
    text = (content or "")
    zones = [text[:2200], text[-1800:]]
    scores = {"ours": 0, "opposing": 0, "court": 0}
    evidence: dict[str, list[str]] = {key: [] for key in scores}
    parties = _party_values(case)
    for zone in zones:
        if re.search(r"审判长|审判员|书记员|合议庭成员|仲裁员|本裁决", zone):
            scores["court"] += 80
            evidence["court"].append("法院/仲裁机构落款特征")
        if re.search(r"人民法院|仲裁委员会|仲裁庭", zone) and re.search(r"判决书|裁定书|调解书|通知书|传票", zone):
            scores["court"] += 45
            evidence["court"].append("机构名称与机构文书标题共现")
        for name, side, aliases in parties:
            if side not in scores:
                continue
            for alias in aliases:
                # The name must be near an issuer/submission expression.  A
                # name in the factual narrative or party list is not enough.
                pattern = rf"(?:{re.escape(alias)})[^。；\n]{{0,45}}(?:提交|出具|签发|具状|落款|代理人|委托人)"
                reverse = rf"(?:提交|出具|签发|具状|落款|代理人|委托人)[^。；\n]{{0,45}}(?:{re.escape(alias)})"
                if re.search(pattern, zone, re.IGNORECASE) or re.search(reverse, zone, re.IGNORECASE):
                    scores[side] += 70
                    evidence[side].append(f"{alias} 与提交/落款特征近邻")
                    break

    is_us = case.get("scenario") == "美国民事" or case.get("layout") == "us_civil"
    if is_us:
        role_sources = _procedural_role_sources(case)
        head = text[:4000]
        tail = text[-2500:]
        filename_l = filename.lower()

        def add(kind: str | None, points: int, reason: str) -> None:
            if kind in scores:
                scores[kind] += points
                evidence[kind].append(reason)

        # A plain filename such as "Order.pdf" is not enough: it may be a
        # party's proposed order.  Court self-expressions and judge/clerk
        # signatures are treated as strong issuer evidence.
        if re.search(r"\b(?:THE\s+COURT\s+HEREBY\s+ORDERS|IT\s+IS\s+(?:HEREBY\s+)?"
                     r"ORDERED|SO\s+ORDERED|BY\s+THE\s+COURT)\b", text, re.IGNORECASE):
            add("court", 140, "法院命令自称语")
        if re.search(r"\bSigned\s+by\s+(?:District\s+|Magistrate\s+)?Judge\b|"
                     r"\bUnited\s+States\s+(?:District|Magistrate)\s+Judge\b|"
                     r"\bClerk\s+of\s+Court\b", tail, re.IGNORECASE):
            add("court", 120, "法官/书记官签署特征")
        if (re.search(r"^(?:\*\*)?(?:ORDER|JUDGMENT)(?:\*\*)?$", head,
                      re.IGNORECASE | re.MULTILINE)
                and re.search(r"\bThe\s+Court\s+(?:shall|finds|orders|concludes|directs)\b",
                              text, re.IGNORECASE)
                and not re.search(r"\bPROPOSED\s+ORDER\b", head, re.IGNORECASE)):
            add("court", 100, "法院文书标题与裁判措辞共现")

        role_pattern = r"plaintiffs?|defendants?|petitioners?|respondents?|applicants?|claimants?"

        # The role at the beginning of the substantive title identifies the
        # filer.  A later phrase such as "to Plaintiff's Complaint" does not.
        title_clean = re.sub(r"^[#>*_\s]+|[#>*_\s]+$", "", extracted_title or "")
        title_role = re.match(rf"^({role_pattern})\b", title_clean, re.IGNORECASE)
        if title_role:
            side = role_sources.get(title_role.group(1).lower())
            add(side, 140, f"文书标题提交角色：{title_role.group(1)}")

        # Filing verbs identify the grammatical actor.  The expression is
        # kept within one sentence/line so references to opposing counsel in
        # a service certificate cannot compete with the actual filer.
        actor_patterns = [
            rf"\b({role_pattern})(?:-in-limitation)?\b[^.\n;]{{0,140}}\bhereby\s+"
            rf"(?:moves?|files?|submits?|petitions?|responds?|requests?)\b",
            rf"\b(?:now\s+comes?)\s+(?:the\s+)?({role_pattern})\b[^.\n;]{{0,160}}"
            rf"\b(?:moves?|files?|submits?|petitions?|responds?|requests?)\b",
        ]
        for pattern in actor_patterns:
            for match in re.finditer(pattern, head, re.IGNORECASE):
                side = role_sources.get(match.group(1).lower())
                add(side, 120, f"提交行为主语：{match.group(0)[:70]}")

        # Counsel-role labels are accepted only in header/signature lines,
        # not everywhere in the body.  This excludes narrative references
        # such as "spoke with counsel for Defendants".
        nonempty = [line.strip() for line in text.splitlines() if line.strip()]
        header_signature_lines = nonempty[:25] + nonempty[-30:]
        counsel_line = re.compile(
            rf"^(?:attorneys?|counsel)\s+for\s+(?:the\s+)?({role_pattern})"
            rf"(?:-in-limitation)?\b", re.IGNORECASE)
        for line in header_signature_lines:
            clean = re.sub(r"^[#>*_\s]+|[#>*_\s]+$", "", line)
            match = counsel_line.search(clean)
            if match:
                side = role_sources.get(match.group(1).lower())
                add(side, 95, f"页眉/签名栏律师角色：{clean[:70]}")

        # Complaint and Answer have stable filing roles in ordinary US civil
        # procedure.  Counterclaims and third-party complaints are excluded.
        file_complaint = bool(re.search(
            r"(?:^|[_\s-])complaint(?:[._\s-]|$)", filename_l, re.IGNORECASE))
        title_complaint = ("complaint" in title_clean.lower()
                           and not re.search(r"answer|motion|opposition|reply|counterclaim|"
                                             r"third[- ]party|cross[- ]claim",
                                             title_clean, re.IGNORECASE))
        if file_complaint or title_complaint:
            add(role_sources.get("plaintiff"), 75, "普通 Complaint 的程序提交角色")
        file_answer = bool(re.search(
            r"(?:^|[_\s-])answer(?:[._\s-]|$)", filename_l, re.IGNORECASE))
        title_answer = bool(re.search(r"\banswer\b", title_clean, re.IGNORECASE))
        if file_answer or title_answer:
            add(role_sources.get("defendant"), 75, "Answer 的程序提交角色")
        if ("answer" in filename_l
                and len(re.findall(r"\bDefendants?\s+(?:admit|deny|lack\s+knowledge)",
                                   head, re.IGNORECASE)) >= 2):
            add(role_sources.get("defendant"), 85, "Answer 正文的承认/否认结构")

        # Named parties still require a nearby English filing/signature verb;
        # a caption listing both sides does not establish source.
        english_issuer = r"hereby\s+(?:files?|moves?|submits?|petitions?|requests?)"
        for _name, side, aliases in parties:
            if side not in scores:
                continue
            for alias in aliases:
                near = rf"(?:{re.escape(alias)})[^\n.;]{{0,90}}(?:{english_issuer})|"
                near += rf"(?:{english_issuer})[^\n.;]{{0,90}}(?:{re.escape(alias)})"
                if re.search(near, head + "\n" + tail, re.IGNORECASE):
                    add(side, 90, f"{alias} 与英文提交/签署特征近邻")
                    break
    ordered = sorted(scores.items(), key=lambda item: -item[1])
    if ordered[0][1] < 60:
        return {"kind": "unknown", "status": "unknown", "confidence": 0.0,
                "evidence": [], "candidates": ordered}
    if len(ordered) > 1 and ordered[0][1] - ordered[1][1] < 25:
        return {"kind": "unknown", "status": "ambiguous", "confidence": 0.0,
                "evidence": evidence[ordered[0][0]] + evidence[ordered[1][0]],
                "candidates": ordered,
                "conflict": {"kind": "source_conflict", "candidates": ordered[:2]}}
    kind = ordered[0][0]
    return {"kind": kind, "status": "inferred", "confidence": min(0.99, ordered[0][1] / 100),
            "evidence": evidence[kind], "candidates": ordered}


def _allowed_scenarios(case: dict | None) -> set[str] | None:
    scenario = (case or {}).get("scenario")
    return {
        "中国民事": {"中国民事", "通用"}, "仲裁": {"仲裁", "通用"},
        "中国刑事": {"中国刑事", "通用"}, "美国民事": {"美国民事", "通用"},
        "刑民交叉": {"中国民事", "中国刑事", "通用"},
    }.get(scenario)


def _date_value(received_at: Any) -> str:
    if isinstance(received_at, datetime):
        return received_at.strftime("%y%m%d")
    if received_at:
        try:
            return datetime.fromisoformat(str(received_at)).strftime("%y%m%d")
        except ValueError:
            pass
    return datetime.now().strftime("%y%m%d")


_FIRM_VERSION_RE = r"v(?:0 1|1 0|0 9|final)"


def is_compliant_filename(filename: str, *, case_abbr: str | None = None,
                           doc_type: str | None = None) -> bool:
    """Validate the Loci firm filename shape without judging document content.

    The date is the six-digit receipt date.  Version tokens use spaces, e.g.
    ``v1 0``; the legacy ``v1.0`` form is deliberately not compliant.
    """
    try:
        name = Path(filename).name
        stem = Path(name).stem
    except (TypeError, ValueError, OSError):
        return False
    if not re.match(r"^\d{6}\s+\S+\s+.+$", stem):
        return False
    tokens = stem.split()
    for index, token in enumerate(tokens[2:], start=2):
        if not token.lower().startswith("v"):
            continue
        version_tail = " ".join(tokens[index:])
        # A version-looking suffix must use the firm's exact vocabulary;
        # arbitrary v2 0/v1.0 tokens are not allowed to masquerade as a
        # compliant filename.  Ordinary document titles without a version
        # remain valid (court/institution materials commonly omit one).
        if re.match(r"^v(?:\d|final)", version_tail, re.IGNORECASE):
            if not re.fullmatch(_FIRM_VERSION_RE, version_tail, re.IGNORECASE):
                return False
    if case_abbr and not re.search(rf"(?:^|\s){re.escape(case_abbr)}(?:\s|$)", stem):
        return False
    if doc_type and doc_type not in stem:
        return False
    return True


def _naming_preview(case: dict | None, doc: dict | None, source: dict,
                    filename: str, received_at: Any, manual: dict | None = None) -> dict:
    manual = manual or {}
    ext = Path(filename).suffix
    abbr = _text((case or {}).get("file_abbr") or (case or {}).get("key"))
    doc_type = _text((doc or {}).get("name") or (doc or {}).get("target")) or "待确认文书"
    source_label = _text(manual.get("party")) or SOURCE_LABELS.get(source.get("kind"), "")
    group = manual.get("batch")
    if group:
        try:
            group_num = int(group)
        except (TypeError, ValueError):
            group_num = 0
        if group_num:
            group_suffix = {2: "（二）", 3: "（三）", 4: "（四）", 5: "（五）",
                            6: "（六）", 7: "（七）", 8: "（八）"}.get(group_num, f"（{group_num}）")
            doc_type += group_suffix
    if manual.get("translation"):
        doc_type += " 译"
    fields = {"date": _date_value(received_at), "matter": abbr,
              "document_type": doc_type, "source": source_label, "extension": ext,
              "group": group, "version": _text(manual.get("version")) or None,
              "translation": bool(manual.get("translation"))}
    parts = [fields["date"], abbr, doc_type]
    if source_label:
        parts.insert(2, source_label)
    if fields["version"]:
        parts.append(fields["version"])
    preview = " ".join(p for p in parts if p) + ext
    filename_evidence = [str(item).lower() for item in (doc or {}).get("evidence", [])]
    type_source = ("filename" if any("文件名" in item for item in filename_evidence)
                   else "title" if any("标题" in item for item in filename_evidence)
                   else "rule_or_content")
    return {
        "fields": {**fields, "received_date": fields["date"],
                   "document_date": None},
        "preview": preview,
        "valid": bool(abbr and doc_type),
        "template_valid": is_compliant_filename(preview, case_abbr=abbr,
                                                  doc_type=doc_type),
        "filename_inference": type_source == "filename",
        "document_type_source": type_source,
    }


def _source_pending_target(target: str) -> str:
    """Return the original candidate target with a source placeholder.

    The placeholder is deliberately applied only to the candidate kept for
    review.  The actual destination is always the case-level holding folder,
    so a source-unknown document cannot reach a business directory first.
    """
    target = _text(target) or "_待分类"
    target = target.replace("我方证据", "{side}").replace("对方证据", "{side}")
    return _us_party_pending_target(target)


def build_filing_decision(filename: str, content: str, cases: list[dict],
                          rules: list[dict], *, extracted_title: str | None = None,
                          manual: dict | None = None, received_at: Any = None,
                          source: str | None = None) -> dict:
    """Return a JSON-compatible decision without touching the filesystem."""
    manual = manual or {}
    case_ranked = _case_candidates(filename, content, cases)
    selected_case, case_status = _select(case_ranked, minimum=60, margin=20)
    if manual.get("case_key"):
        selected_case = next((item for item in case_ranked if item["key"] == manual["case_key"]), None)
        if not selected_case:
            selected_case = next((
                {"key": case.get("key"), "name": case.get("name"), "score": 0,
                 "evidence": ["人工选择案件"], "case": runtime_case_view(case)}
                for case in cases if case.get("key") == manual["case_key"]), None)
        case_status = "manual" if selected_case else "unknown"
    selected_case_cfg = selected_case.get("case") if selected_case else None

    source_result = infer_source(content, filename, selected_case_cfg,
                                 manual_source=manual.get("source") or source,
                                 extracted_title=extracted_title)
    if manual.get("party"):
        source_result["party"] = _text(manual["party"])
        source_result.setdefault("evidence", []).append("人工选择来源主体")
    ranked_rules = _rule_candidates(filename, content, extracted_title, rules,
                                    _allowed_scenarios(selected_case_cfg), source_result["kind"])
    selected_doc, doc_status = _select(ranked_rules, minimum=50, margin=15)
    if manual.get("target"):
        selected_doc = {"target": manual["target"], "name": manual.get("doc_type") or manual["target"].rsplit("/", 1)[-1],
                        "score": 1000, "evidence": ["人工选择归档目录"],
                        "source_sensitive": any(token in manual["target"] for token in SOURCE_SENSITIVE)}
        doc_status = "manual"

    # Source is a global gate.  It applies to every document target, not only
    # evidence folders.  A manual source is represented by status=manual in
    # infer_source(), so an explicit human override is the only bypass.
    candidate_target = _text(selected_doc.get("target")) if selected_doc else "_待分类"
    source_unresolved = source_result.get("status") in UNRESOLVED_SOURCE_STATUSES
    pending_target = _source_pending_target(candidate_target) if source_unresolved else None
    target = candidate_target
    if selected_case and source_unresolved:
        target = "_待确认来源"
    if not selected_case:
        # Matter uncertainty has precedence over source routing: without a
        # selected case there is nowhere safe to ask the source question.
        target = "_未分类"

    case_root = _text((selected_case_cfg or {}).get("cases_dir"))
    if not selected_case_cfg or not case_root:
        path_health = "unknown"
    else:
        root_path = Path(case_root).expanduser()
        if not root_path.exists():
            path_health = "missing"
        elif not root_path.is_dir():
            path_health = "not_directory"
        else:
            path_health = "ok" if os.access(str(root_path), os.W_OK) else "not_writable"

    risk = "green"
    if source_unresolved:
        risk = "red"
    elif case_status in {"unknown", "ambiguous"} or doc_status in {"unknown", "ambiguous"}:
        risk = "red"

    case_margin = (case_ranked[0]["score"] - case_ranked[1]["score"]
                   if len(case_ranked) > 1 else None)
    doc_margin = (ranked_rules[0]["score"] - ranked_rules[1]["score"]
                  if len(ranked_rules) > 1 else None)
    decision = {
        "decision_id": None,
        "status": "pending_review" if risk != "green" else "suggested",
        "file": {"original_name": filename, "received_at": str(received_at or ""),
                 "sha256": None},
        "matter": {"selected": {"key": selected_case["key"], "name": selected_case["name"]} if selected_case else None,
                   "status": case_status, "lead_margin": case_margin,
                   "conflict": {"kind": "matter_conflict", "candidates": case_ranked[:2]}
                   if case_status == "ambiguous" else None,
                   "candidates": [
                       {key: value for key, value in item.items() if key != "case"}
                       for item in case_ranked[:5]
                   ]},
        "document_type": {"selected": {key: value for key, value in selected_doc.items() if key not in {"case", "source"}} if selected_doc else None,
                          "status": doc_status, "lead_margin": doc_margin,
                          "conflict": {"kind": "document_type_conflict", "candidates": ranked_rules[:2]}
                          if doc_status == "ambiguous" else None,
                          "candidates": ranked_rules[:8]},
        "source": source_result,
        "destination": {"relative": target, "pending_target": pending_target,
                         "case_root": case_root, "health": path_health,
                         "source_required": source_unresolved,
                         "source_options": list(SOURCE_OPTIONS)},
        "naming": _naming_preview(selected_case_cfg, selected_doc, source_result, filename, received_at, manual),
        "risk": risk,
        "manual_fields": [key for key in ("case_key", "target", "source", "doc_type", "party", "date", "batch", "version", "translation", "stage") if manual.get(key)],
        # Preserve the exact human facts used for this frozen preview.  The
        # UI can then detect a changed selection and require re-analysis
        # instead of committing an old decision against new form values.
        "manual_values": {key: manual.get(key) for key in ("case_key", "target", "source", "doc_type", "party", "date", "batch", "version", "translation", "stage")
                          if manual.get(key)},
    }
    return decision
