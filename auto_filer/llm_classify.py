"""
本地 LLM 两段式归档判定（级联第②层）。全本地离线，Ollama 不可用则返回 None。

为什么两段：小模型在一步里"读正文+选文件夹"时，会被正文里【提到/引用】的词带偏
（如证据清单里提到"购销合同"→误选合同文件夹）。拆成两段后准确率大幅提升：
  ① llm_doctype()     —— 只读正文，判定"这份文件本身是什么文书"（模型擅长，不带偏）
  ② llm_pick_folder() —— 只拿"文书类型"去选文件夹（看不到正文杂词，不被带偏）
"""
import json
import urllib.request


def _head_tail(text: str, max_chars: int, tail: int = 4000) -> str:
    """长文件取首尾：前(max_chars-tail)字 + 末tail字，保留签名/盖章信息。"""
    if len(text) <= max_chars:
        return text
    head = max_chars - tail
    return text[:head] + "\n…（中间内容略）…\n" + text[-tail:]

_SCHEMA_DT = {
    "type": "object",
    "properties": {
        "doc_type":   {"type": "string"},
        "title":      {"type": "string"},
        "source":     {"type": "string", "enum": ["court", "ours", "opposing", "unknown"]},
        "confidence": {"type": "number"},
    },
    "required": ["doc_type", "confidence"],
}
_SCHEMA_PICK = {
    "type": "object",
    "properties": {
        "target":     {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["target", "confidence"],
}


def _ollama_chat(prompt: str, model: str, host: str, schema, timeout: int = 60,
                 system: str = None) -> str:
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps({
            "model": model,
            "messages": msgs,
            "stream": False,
            "format": schema,            # 结构化输出（JSON Schema 约束）
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 500, "num_ctx": 32768},
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def llm_doctype(content_text: str, filename: str, llm_cfg: dict, case_cfg: dict = None):
    """① 只判文书类型 → {doc_type, source, confidence} 或 None。"""
    if not llm_cfg.get("enabled") or not (content_text or "").strip():
        return None
    party_hint = ""
    if case_cfg:
        us = case_cfg.get("party_us") or []
        op = case_cfg.get("party_op") or []
        if us or op:
            party_hint = (
                f"\n当事人信息（用于判断来源）：我方={','.join(us)}；对方={','.join(op)}\n"
                "来源判断规则：\n"
                "  · 文件名/正文中提交方/盖章方/署名方是我方 → source=ours；\n"
                "  · 提交方/盖章方/署名方是对方 → source=opposing；\n"
                "  · 法院/仲裁机构出具（传票/通知/判决/裁定）→ source=court；\n"
                "  · 邮单/EMS单/快递单 → source 与所寄送的母文件一致（寄件方即来源方）。\n"
            )
    prompt = (
        "你是法律文书识别助手。判断这份文件【本身】是什么文书（依据它的标题与整体性质），"
        "给出规范文书名、来源、置信度。\n"
        "⚠ 不要被文件中【提到或引用】的其他材料名称带偏：\n"
        "  · 证据清单里提到'购销合同'，它本身是【证据目录】；\n"
        "  · 鉴定申请书里提到'委托书'，它本身是【鉴定申请书】（≠授权委托书）；\n"
        "  · 判决书、裁定书是【裁判文书】。\n"
        "doc_type：规范文书名，**用文件本身的语言**——中文文件用中文名（起诉状、民事判决书、"
        "证据目录、财产保全申请书），英文文件用英文名（Complaint、Motion for Summary Judgment、"
        "Notice of Deposition、Deposition Transcript、Settlement Agreement、Expert Report）。\n"
        "title：如文件正文有具体完整标题，原样抓取（如'关于损失鉴定异议的申请书'）；"
        "若无明确标题或与 doc_type 相同则留空字符串。\n"
        "source：court（法院/仲裁机构）/ ours（我方）/ opposing（对方）/ unknown。\n"
        f"{party_hint}"
        '严格只输出 JSON：{"doc_type":"<文书名>","title":"<原始标题或空>","source":"<来源>","confidence":<0-1>}\n\n'
        f"文件名：{filename}\n"
        f"文件内容：\n{_head_tail(content_text, int(llm_cfg.get('max_chars', 40000)))}"
    )
    try:
        r = json.loads(_ollama_chat(prompt, llm_cfg.get("model", "qwen2.5:7b"),
                                    llm_cfg.get("host", "http://localhost:11434"),
                                    _SCHEMA_DT, int(llm_cfg.get("timeout", 60))))
    except Exception:
        return None
    dt = (r.get("doc_type") or "").strip()
    if not dt:
        return None
    src = r.get("source") if r.get("source") in ("court", "ours", "opposing") else None
    title = (r.get("title") or "").strip()
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0.6))))
    except (TypeError, ValueError):
        conf = 0.6
    return {"doc_type": dt, "title": title or None, "source": src, "confidence": conf}


def llm_pick_folder(doc_type: str, source: str, folder_catalog: list, llm_cfg: dict,
                    descriptions: dict = None):
    """② 拿文书类型选文件夹 → {target, confidence} 或 None。
    清单放在提示最前面（稳定前缀，便于 Ollama 缓存→后续更快）；文书类型放最后。"""
    if not folder_catalog:
        return None
    descriptions = descriptions or {}
    catalog = "\n".join(
        (f"- {t}　// {descriptions[t]}" if descriptions.get(t) else f"- {t}")
        for t in folder_catalog
    )
    # 清单放 system（稳定前缀→Ollama 缓存，后续更快）；文书类型放 user（模型聚焦回答）
    system = (
        "你是法律案件材料归档助手。下面是候选文件夹清单（含用途说明）：\n"
        f"{catalog}\n\n"
        "规则：只按用户给的【文书类型】选用途最匹配的一个文件夹；"
        "若候选有'我方/对方'之分，按来源选（opposing=对方，ours=我方，court=法院出具）；"
        "只能原样返回清单中的路径，不得新造。"
        '严格只输出 JSON：{"target":"<清单中的路径>","confidence":<0-1>}'
    )
    prompt = f"文书类型：{doc_type}\n来源：{source or 'unknown'}\n请选文件夹。"
    try:
        r = json.loads(_ollama_chat(prompt, llm_cfg.get("model", "qwen2.5:7b"),
                                    llm_cfg.get("host", "http://localhost:11434"),
                                    _SCHEMA_PICK, int(llm_cfg.get("timeout", 60)), system))
    except Exception:
        return None
    # 枚举校验（去斜杠/空格容错）
    raw_t = (r.get("target") or "").strip().strip("/").strip()
    if raw_t in folder_catalog:
        target = raw_t
    else:
        nmap = {c.strip().strip("/").replace(" ", ""): c for c in folder_catalog}
        target = nmap.get(raw_t.replace(" ", ""))
    if not target:
        return None
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0.6))))
    except (TypeError, ValueError):
        conf = 0.6
    return {"target": target, "confidence": conf}


def llm_case_match(content_text: str, filename: str, cases: list, llm_cfg: dict):
    """案件识别：关键词未命中时，LLM 从活跃案件清单中选一个。
    返回 {case_key, confidence} 或 None。
    case profile 来自 config.yaml 的 name / keywords / parties(可选) / subject(可选)。
    """
    if not llm_cfg.get("enabled") or not (content_text or "").strip():
        return None
    active = [c for c in (cases or []) if c.get("status") != "closed"]
    if not active:
        return None

    # 构建案件清单（给 LLM 的参考）
    lines = []
    for c in active:
        seg = [f"key={c['key']}", f"名称={c.get('name', '')}"]
        if c.get("parties"):
            seg.append("当事人=" + "/".join(c["parties"]))
        if c.get("subject"):
            seg.append("争议=" + c["subject"])
        if c.get("cause_of_action"):
            seg.append("诉因=" + "/".join(c["cause_of_action"]))
        kws = c.get("keywords", [])
        if kws:
            seg.append("关键词=" + ",".join(str(k) for k in kws[:6]))
        lines.append("- " + "；".join(seg))
    case_list = "\n".join(lines)

    _SCHEMA_CASE = {
        "type": "object",
        "properties": {
            "case_key":   {"type": ["string", "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["case_key", "confidence"],
    }

    prompt = (
        "你是法律案件归档助手。下列是当前活跃案件：\n"
        f"{case_list}\n\n"
        "根据文件名和内容，判断该文件属于哪个案件。\n"
        "无法确定时，返回 case_key=null 并 confidence<0.5。\n"
        '只输出 JSON：{"case_key":"<key或null>","confidence":<0-1>}\n\n'
        f"文件名：{filename}\n"
        f"内容节选：\n{_head_tail(content_text, int(llm_cfg.get('max_chars', 40000)))}"
    )
    try:
        r = json.loads(_ollama_chat(
            prompt,
            llm_cfg.get("model", "qwen2.5:7b"),
            llm_cfg.get("host", "http://localhost:11434"),
            _SCHEMA_CASE,
            int(llm_cfg.get("timeout", 60)),
        ))
    except Exception:
        return None

    ck = (r.get("case_key") or "").strip()
    if not ck or ck.lower() == "null":
        return None
    valid = {c["key"] for c in active}
    if ck not in valid:
        return None
    try:
        conf = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return {"case_key": ck, "confidence": conf}


def llm_classify(content_text: str, filename: str, folder_catalog: list, llm_cfg: dict,
                 descriptions: dict = None, case_cfg: dict = None):
    """两段式编排 → {doc_type, source, target, confidence} 或 None。
    target 可能为 None（第②段没选中清单）——此时调用方可用 doc_type 走规则映射兜底。"""
    if not llm_cfg.get("enabled") or not (content_text or "").strip():
        return None
    dt = llm_doctype(content_text, filename, llm_cfg, case_cfg)
    if not dt:
        return None
    out = {"doc_type": dt["doc_type"], "source": dt["source"],
           "confidence": dt["confidence"], "target": None}
    pick = llm_pick_folder(dt["doc_type"], dt["source"], folder_catalog, llm_cfg, descriptions)
    if pick:
        out["target"] = pick["target"]
        out["confidence"] = min(dt["confidence"], pick["confidence"])
    return out
