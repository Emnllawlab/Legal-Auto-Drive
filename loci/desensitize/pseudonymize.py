"""第 3 层：化名一致性 + 加密映射表 + 可逆回填。

- 一致性：项目级 Registry 实体注册表，同一真实实体跨文档复用同一化名。
- 简称归并：公司"核心名相互包含 + 唯一候选"才自动并；多候选标 ambiguous，
  交人工 manual_link() 或 Qwen coref 钩子，绝不猜（错并两家公司=法律事故）。
- 可逆 token：语义项用 甲公司/某甲（抗 AI 改写、可读），结构化项用 〔PHONE1〕。
- 加密金库：AES-256-GCM，密钥 = scrypt(口令 + 机器码)，换机器即解不开。
"""
import re
import uuid
import os
import json
from dataclasses import dataclass, field
from typing import Optional, Callable

from .model import canon, Span
from .rules import _strip_co_lead


def _clean_co_text(s):
    """剥离公司名前导称谓/语境词，用于干净的 canonical（影响回填质量）。"""
    g, _ = _strip_co_lead(s.strip(), 0)
    return g or s


GANZHI = "甲乙丙丁戊己庚辛壬癸"
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 语义实体 -> 可读化名（AI 当专有名词，保留率高）。按 locale 切换。
ALIAS_ZH = {
    "COMPANY": lambda n: f"{GANZHI[n]}公司" if n < 10 else f"第{n + 1}公司",
    "PERSON": lambda n: f"某{GANZHI[n]}" if n < 10 else f"自然人{n + 1}",
    "ADDRESS": lambda n: f"某地{n + 1}",
}
ALIAS_EN = {
    "COMPANY": lambda n: f"Company {_LETTERS[n]}" if n < 26 else f"Company #{n + 1}",
    "PERSON": lambda n: f"Person {_LETTERS[n]}" if n < 26 else f"Person #{n + 1}",
    "ADDRESS": lambda n: f"[Address {n + 1}]",
}
ALIAS = ALIAS_ZH                      # 向后兼容默认引用

# 结构化项 -> 可逆 token（括号极少被 AI 篡改）
STRUCTURED = {"ID_CARD", "USCC", "BANKCARD", "PHONE", "EMAIL", "PLATE",
              "CASE_NO", "MONEY", "SECURITIES", "WECHAT", "SSN", "EIN", "OTHER",
              # 新增证件 / 账户 / 网络标识 / 坐标：均为结构化，走可逆 token
              "PASSPORT", "HK_MO_TW", "DRIVER_LICENSE", "SOCIAL_SEC",
              "QQ", "BIRTHDAY", "IP", "MAC", "IMEI", "GPS"}


def struct_token(cat, n, lang="zh"):
    return f"[{cat}_{n + 1}]" if lang == "en" else f"〔{cat}{n + 1}〕"


# ---------- 归一化 ----------
def _norm(s):
    return s.strip().replace("（", "(").replace("）", ")").replace(" ", "").replace("　", "")


_CO_PREFIX = re.compile(r'^(被告人?|原告|上诉人|被上诉人|申请人|被申请人|第三人|[甲乙丙]方)')
_CO_NOISE = re.compile(r'\([^)]*\)|股份|有限|责任|公司|集团|企业')
_PERSON_ROLE = re.compile(r'(律师|法官|先生|女士|同志|经理|董事长|代理人)$')

# 英文 locale 归一
_CO_PREFIX_EN = re.compile(r'^(?:the\s+|plaintiff\s+|defendant\s+)', re.IGNORECASE)
_CO_NOISE_EN = re.compile(
    r'\([^)]*\)|[,.]|\b(?:Inc|LLC|Ltd|Limited|Corp|Corporation|Company|Co|LP|LLP|PLC|GmbH)\b\.?',
    re.IGNORECASE)
_PERSON_ROLE_EN = re.compile(r'^(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+', re.IGNORECASE)


def _has_cjk(s):
    return bool(re.search(r'[一-鿿]', s))


def _use_en(s, lang):
    """该串是否走英文归一：显式 en，或 both 模式下不含中文的串。"""
    return lang == "en" or (lang == "both" and not _has_cjk(s))


def _co_key(s, lang="zh"):
    if _use_en(s, lang):
        return _CO_NOISE_EN.sub('', _CO_PREFIX_EN.sub('', _norm(s))).strip().lower()
    return _CO_NOISE.sub('', _CO_PREFIX.sub('', _norm(s)))


def _person_key(s, lang="zh"):
    if _use_en(s, lang):
        return _PERSON_ROLE_EN.sub('', _norm(s)).strip().lower()
    return _PERSON_ROLE.sub('', _norm(s))


# 公司"可搜索词干"：只剥尾部括号+后缀，保留中间字（用于在原文里 rescan，
# 容忍 OCR 把尾部读丢/截断，如"…工业企业管理（上海）有限公司"→"…工业企业管理"）
_CO_TAIL = re.compile(
    r'(（[^）]*）|\([^)]*\)|股份有限公司|有限责任公司|有限公司|有限合伙|合伙企业'
    r'|分公司|股份|有限|责任|公司|集团|企业|事务所)+$'
)


def _co_stem(s):
    return _CO_TAIL.sub('', s.strip())


def _lcs_len(a, b):
    """最长公共子串长度（用于公司名模糊匹配，容忍 OCR 误字/截断）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _co_fuzzy_match(stem_a, stem_b, min_abs=4, min_ratio=0.6):
    """两公司词干是否"基本同名"：公共子串够长且占比够高才算。

    保守阈值：容忍 OCR 把"上海"读成"土海"、前缀污染、跨行截断；
    又不会因为两家公司碰巧共享 4 字（如同省同行业）就错并。
    """
    if len(stem_a) < min_abs or len(stem_b) < min_abs:
        return False
    l = _lcs_len(stem_a, stem_b)
    return l >= min_abs and l >= min_ratio * min(len(stem_a), len(stem_b))


# ---------- 实体 ----------
@dataclass
class Entity:
    eid: str
    category: str
    canonical: str                 # 回填用：迄今最完整的真实表述
    key: str
    alias: str
    surfaces: set = field(default_factory=set)   # 归一后的串，用于匹配
    raw: set = field(default_factory=set)        # 原样串，用于跨文档 rescan
    ambiguous: bool = False


class Registry:
    """项目级实体注册表：保证跨文档化名一致。"""

    def __init__(self, coref: Optional[Callable] = None, lang="zh"):
        self.by_cat: dict = {}
        self._count: dict = {}
        self.lang = lang                      # "zh" | "en"：切换化名方案与归一规则
        # 可选 Qwen 共指消解钩子: (cat, text, candidates) -> Entity | None
        self.coref = coref

    def _alias_for(self, cat):
        n = self._count.get(cat, 0)
        self._count[cat] = n + 1
        if cat in STRUCTURED:
            return struct_token(cat, n, self.lang)
        scheme = ALIAS_EN if self.lang == "en" else ALIAS_ZH
        default = (lambda i: f"[{cat}_{i + 1}]") if self.lang == "en" else (lambda i: f"〔{cat}{i + 1}〕")
        return scheme.get(cat, default)(n)

    def _new(self, cat, text, key, ambiguous=False):
        canon_text = _clean_co_text(text) if cat == "COMPANY" else text
        e = Entity(uuid.uuid4().hex[:8], cat, canon_text, key, self._alias_for(cat),
                   surfaces={_norm(text)}, raw={text}, ambiguous=ambiguous)
        self.by_cat.setdefault(cat, []).append(e)
        return e

    def _absorb(self, e, text):
        e.surfaces.add(_norm(text))
        e.raw.add(text)
        cand = _clean_co_text(text) if e.category == "COMPANY" else text
        if len(cand) > len(e.canonical):
            e.canonical = cand
        return e

    def surface_spans(self, text):
        """在文本里找所有已知实体的原样表述，生成 Span。

        保证跨文档一致：一旦某实体进过注册表，后续文档即使 NER 漏掉该 mention，
        也会被这步确定性命中并归并到同一化名。
        """
        min_len = 3 if self.lang == "en" else 2
        out = []
        for ents in self.by_cat.values():
            for e in ents:
                needles = set(e.raw)
                needles.update(_norm(raw) for raw in e.raw)
                # 公司额外用"词干"兜底：只剥尾部后缀、保留中间字，容忍 OCR 把
                # 后缀读丢或被印章截断（"…工业企业管理（上海）有限公司"→"…工业企业管理"）
                if e.category == "COMPANY":
                    stem = _co_stem(e.canonical)
                    if len(stem) >= 4:
                        needles.add(stem)
                for raw in needles:
                    if len(raw) < min_len:
                        continue
                    for m in re.finditer(re.escape(raw), text):
                        out.append(Span(m.start(), m.end(), e.category, raw, "regex", 1.0))
        return out

    def resolve(self, label, text, explicit_alias: bool = False) -> Entity:
        cat = canon(label)
        nt = _norm(text)
        bucket = self.by_cat.get(cat, [])
        # 1) 精确：同一表述（含结构化项相同号码）直接复用
        for e in bucket:
            if nt in e.surfaces:
                return e
        # 2) 公司归并：只有明确标记为别名的命中才允许核心名归并。
        # 模糊相似永远不自动合并：不同公司共用化名会改变法律事实，
        # 应进入人工 link，而不是由相似度替用户作身份判断。
        if cat == "COMPANY":
            k = _co_key(text, self.lang)
            cands = []
            if explicit_alias:
                for e in bucket:
                    if k and e.key and (k in e.key or e.key in k):
                        cands.append(e)
            if len(cands) == 1:
                return self._absorb(cands[0], text)
            if len(cands) > 1:
                hit = self.coref(cat, text, cands) if self.coref else None
                return self._absorb(hit, text) if hit else self._new(cat, text, k, ambiguous=True)
            return self._new(cat, text, k)
        # 3) 人名/其他：默认仅精确；张律师↔张伟 交给 coref 或人工 link
        key = _person_key(text, self.lang) if cat == "PERSON" else nt
        if self.coref:
            hit = self.coref(cat, text, bucket)
            if hit:
                return self._absorb(hit, text)
        return self._new(cat, text, key)

    def manual_link(self, alias_a, alias_b):
        """人工复核：强制合并两个化名（用于 ambiguous 项或 NER coref 结果）。"""
        a, b = self._find(alias_a), self._find(alias_b)
        if a and b and a is not b:
            a.surfaces |= b.surfaces
            a.raw |= b.raw
            if len(b.canonical) > len(a.canonical):
                a.canonical = b.canonical
            a.ambiguous = False
            self.by_cat[b.category].remove(b)
            return a
        return None

    def _find(self, alias):
        return next((e for es in self.by_cat.values() for e in es if e.alias == alias), None)

    def pending_review(self):
        """返回需人工确认的 ambiguous 实体。"""
        return [e for es in self.by_cat.values() for e in es if e.ambiguous]

    def export(self):
        return [
            {"eid": e.eid, "category": e.category, "alias": e.alias,
             "canonical": e.canonical, "surfaces": sorted(e.surfaces),
             "ambiguous": e.ambiguous}
            for es in self.by_cat.values() for e in es
        ]


# ---------- 替换 / 回填 ----------
def plan(text, spans, reg: Registry):
    """解析每个 span 的化名，返回带偏移的替换计划（不改文本）。

    左到右解析以保证 Registry 化名分配顺序稳定。docx 原位改写、纯文本替换
    都基于这份计划，避免逻辑分叉。
    """
    reps = []
    for s in sorted(spans, key=lambda x: x.start):
        e = reg.resolve(s.label, s.text, explicit_alias=(s.source == "alias"))
        reps.append({"start": s.start, "end": s.end, "alias": e.alias,
                     "original": s.text, "label": s.label,
                     "fragments": s.fragments})
    return reps


def pseudonymize(text, spans, reg: Registry):
    """纯文本脱敏：从右往左套用替换计划，偏移不失效。"""
    reps = plan(text, spans, reg)
    operations = []
    for r in reps:
        fragments = r.get("fragments") or ((r["start"], r["end"]),)
        # 连续实体正常替换；跨页/断行实体只在第一片段写入化名，
        # 后续片段清空，避免同一实体在输出中重复出现多个化名。
        for index, (start, end) in enumerate(fragments):
            operations.append((start, end, r["alias"] if index == 0 else ""))
    for start, end, replacement in sorted(operations, reverse=True):
        text = text[:start] + replacement + text[end:]
    applied = [{"alias": r["alias"], "original": r["original"], "label": r["label"]}
               for r in reps]
    return text, applied


# ---------- 遮盖式渲染（对外交付：黑框 / 星号） ----------
# 化名（alias）保留可读+可逆，适合内部喂 AI；黑框/星号不可逆、纯粹隐去，
# 适合对外交付、交法院。三者共用同一批 span，只是替换文本不同。
_BLOCK = "█"          # 全角实心块，对标律隐盾"黑框遮盖"
_STAR = "＊"          # 全角星号，视觉对齐中文正文


def _mask_str(original: str, mode: str) -> str:
    """按原文长度生成等宽遮盖串（下限 2，避免单字号被一眼看穿长度）。"""
    n = max(2, len(original))
    return (_BLOCK if mode == "blackout" else _STAR) * n


def render(text, spans, reg: Registry, mode: str = "alias"):
    """统一脱敏渲染入口：mode 决定替换文本。

    mode="alias"    -> 化名/结构化 token（走 Registry，跨文档一致、可逆）
    mode="blackout" -> 黑框遮盖（不入 Registry，不可逆）
    mode="asterisk" -> 星号遮盖（不入 Registry，不可逆）

    返回 (masked_text, applied)。applied[i]["alias"] 为实际写入串，
    original 为被替换原文，供 UI 展示与（alias 模式下）金库回填。
    """
    if mode == "alias":
        return pseudonymize(text, spans, reg)
    reps, applied = [], []
    for s in spans:
        fragments = s.fragments or ((s.start, s.end),)
        reps.append({"fragments": fragments, "rep": _mask_str(s.text, mode)})
        applied.append({"alias": _mask_str(s.text, mode),
                        "original": s.text, "label": s.label})
    operations = []
    for r in reps:
        for start, end in r["fragments"]:
            operations.append((start, end, _mask_str(text[start:end], mode)))
    for start, end, replacement in sorted(operations, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, applied


def build_restore_map(reg: Registry):
    return {e.alias: e.canonical for es in reg.by_cat.values() for e in es}


def restore(ai_text, restore_map):
    """化名/token -> canonical，最长优先避免子串误替。"""
    if not restore_map:
        return ai_text
    keys = sorted(restore_map, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(k) for k in keys))
    return pat.sub(lambda m: restore_map[m.group()], ai_text)


# ============================================================
# 加密金库：AES-256-GCM，密钥 = scrypt(口令 + 机器码)
# 需要 pip install cryptography（延迟导入，核心脱敏不依赖它）
# ============================================================
_AAD = b"lawbot-map-v1"


def _derive(password, machine_code, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive((password + "::" + machine_code).encode())


def vault_save(path, mapping: list, password, machine_code=""):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive(password, machine_code, salt)
    ct = AESGCM(key).encrypt(nonce, json.dumps(mapping, ensure_ascii=False).encode(), _AAD)
    with open(path, "wb") as f:
        f.write(salt + nonce + ct)              # 16 + 12 + 密文


def vault_load(path, password, machine_code=""):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = open(path, "rb").read()
    salt, nonce, ct = raw[:16], raw[16:28], raw[28:]
    key = _derive(password, machine_code, salt)
    return json.loads(AESGCM(key).decrypt(nonce, ct, _AAD).decode())  # 口令/机器码错 -> 抛异常
