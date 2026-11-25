"""脱敏识别用的文本规范化与原文坐标映射。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .model import Span


_PAGE_NOISE = re.compile(
    r"^\s*(?:[-—_=·•|｜]+\s*)?"
    r"(?:第\s*\d+\s*页\s*(?:(?:/|／)\s*(?:共\s*)?|of\s*|共\s*)\d+\s*页?"
    r"|page\s+\d+\s+(?:of|/|／)\s*\d+)"
    r"(?:\s*[-—_=·•|｜].*)?\s*$", re.IGNORECASE)


def _is_cjk(ch: str) -> bool:
    return bool(ch and "\u3400" <= ch <= "\u9fff")


def _joinable_break(prev: str, cur: str) -> bool:
    if not prev or not cur:
        return False
    if (_is_cjk(prev) and _is_cjk(cur)) or (prev.isalnum() and cur.isalnum()):
        return True
    return prev in "@._-+" or cur in "@._-+"


@dataclass
class NormalizedText:
    text: str
    origins: list[tuple[int, int]]
    original: str

    def map_span(self, span: Span) -> Span:
        """映射回原文；分页/断行会产生多个原文片段。"""
        lo = max(0, min(len(self.origins), span.start))
        hi = max(lo, min(len(self.origins), span.end))
        positions: list[int] = []
        for start, end in self.origins[lo:hi]:
            positions.extend(range(start, end))
        if not positions:
            return span
        positions = sorted(set(positions))
        fragments: list[tuple[int, int]] = []
        start = prev = positions[0]
        for pos in positions[1:]:
            if pos != prev + 1:
                fragments.append((start, prev + 1))
                start = pos
            prev = pos
        fragments.append((start, prev + 1))
        display = "".join(self.original[a:b] for a, b in fragments)
        return Span(fragments[0][0], fragments[-1][1], span.label, display,
                    span.source, span.score, tuple(fragments))


def normalize_text(original: str) -> NormalizedText:
    """为识别生成标准化视图，不修改原文。"""
    original = original or ""
    out: list[str] = []
    origins: list[tuple[int, int]] = []
    i = 0
    line_start = True
    while i < len(original):
        ch = original[i]
        if line_start:
            line_end = original.find("\n", i)
            if line_end < 0:
                line_end = len(original)
            if _PAGE_NOISE.match(original[i:line_end]):
                # 页眉前的换行在识别视图中可能暂时形成一个占位空格；
                # 页眉被删除后，若实体在页眉两侧继续，应把该占位一并删除。
                while out and out[-1] == " ":
                    out.pop()
                    origins.pop()
                i = line_end + (1 if line_end < len(original) else 0)
                line_start = True
                continue
            line_start = False

        if ch in "\r\n\f":
            j = i + 1
            while j < len(original) and original[j] in "\r\n\f":
                j += 1
            prev = out[-1] if out else ""
            k = j
            while k < len(original) and original[k] in " \t\u3000":
                k += 1
            header_end = original.find("\n", k)
            if header_end < 0:
                header_end = len(original)
            if _PAGE_NOISE.match(original[k:header_end]):
                i = k
                line_start = True
                continue
            nxt = unicodedata.normalize("NFKC", original[k]) if k < len(original) else ""
            if _joinable_break(prev, nxt):
                i = k
                continue
            out.append(" ")
            origins.append((i, j))
            i = j
            line_start = True
            continue

        if ch in " \t\u3000":
            prev = out[-1] if out else ""
            j = i + 1
            while j < len(original) and original[j] in " \t\u3000":
                j += 1
            nxt = unicodedata.normalize("NFKC", original[j]) if j < len(original) else ""
            if _is_cjk(prev) and _is_cjk(nxt):
                i = j
                continue
            out.append(" ")
            origins.append((i, j))
            i = j
            continue

        normalized = unicodedata.normalize("NFKC", ch)
        for normalized_ch in normalized:
            out.append(normalized_ch)
            origins.append((i, i + 1))
        i += 1
        line_start = False
    return NormalizedText("".join(out), origins, original)
