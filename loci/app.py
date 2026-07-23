#!/usr/bin/env python3
"""Loci — Flask 后端"""

import os, re, sys, json, time, yaml, signal, subprocess, threading
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)

WS  = Path(__file__).resolve().parent.parent
AF  = WS / "auto_filer"
BIN = WS / "bin"

# ── 脱敏引擎（已捆绑进 loci/desensitize/，自包含）──────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from desensitize import (Registry, QwenNER, make_ocr, desensitize_tree,
                             analyze_text, apply_review, to_markdown)
    _DESENS_OK, _DESENS_ERR = True, ""
except Exception as _e:                       # 引擎缺失不影响 Loci 其余功能
    _DESENS_OK, _DESENS_ERR = False, str(_e)

# 全案脱敏后台任务状态（单用户本地，单任务即可）
_DESENS_JOB = {"running": False, "case": "", "i": 0, "total": 0,
               "current": "", "done": False, "report": None, "error": None, "dst": ""}


def _run_desensitize(src, dst, password):
    g = _DESENS_JOB
    try:
        reg = Registry(lang="zh")
        ner = QwenNER(model="qwen2.5:7b", lang="zh")   # 复用本机 Ollama
        ocr = make_ocr()

        def prog(i, total, rel, res):
            g.update(i=i, total=total, current=rel)

        g["report"] = desensitize_tree(src, dst, reg=reg, ner=ner, ocr_engine=ocr,
                                       vault_password=password, progress=prog)
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"], g["done"] = False, True

# 程序类型 → 场景（用于场景硬隔离）
PROC_SCENARIO = {
    "civil": "中国民事", "arbitration": "仲裁", "criminal": "中国刑事",
    "us_civil": "美国民事", "mixed": "刑民交叉",
}
SCENARIOS = ["中国民事", "仲裁", "中国刑事", "美国民事", "刑民交叉"]

# ── 环境加载 ──────────────────────────────────────────────────
def _load_env():
    raw = {}
    env_file = WS / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip().strip('"\'')
    env = {}
    for k, v in raw.items():
        env[k] = re.sub(r'\$\{?(\w+)\}?',
                        lambda m: raw.get(m.group(1), os.environ.get(m.group(1), m.group(0))), v)
    return env

def _paths():
    e = _load_env()
    return {
        "workspace": str(WS),
        "inbox":     e.get("INBOX_DIR",   str(WS / "inbox")),
        "cases":     e.get("CASES_DIR",   str(WS / "cases")),
        "logs":      e.get("LOG_DIR",     str(WS / "logs")),
    }

def _config():
    return yaml.safe_load((AF / "config.yaml").read_text(encoding="utf-8"))

def _pid():
    pf = AF / ".filer.pid"
    if pf.exists():
        try:
            pid = int(pf.read_text().strip())
            os.kill(pid, 0)
            return pid
        except Exception:
            pass
    return None

def _log_file():
    return Path(_paths()["logs"]) / f"filer_{datetime.now().strftime('%Y%m')}.log"

# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    paths = _paths()
    pid   = _pid()
    inbox = Path(paths["inbox"])
    files = []
    if inbox.exists():
        files = [f for f in inbox.iterdir()
                 if f.is_file() and not f.name.startswith(".")
                 and f.name not in {"README.txt", "README.md", ".gitkeep"}]
    cfg = _config()
    return jsonify({
        "daemon":      {"running": pid is not None, "pid": pid},
        "inbox_count": len(files),
        "case_count":  len(cfg.get("cases", [])),
        "paths":       paths,
    })

@app.route("/api/daemon/start", methods=["POST"])
def daemon_start():
    if _pid():
        return jsonify({"ok": True, "msg": "already running"})
    subprocess.Popen(
        ["python3", str(AF / "auto_filer.py"), "--daemon", "-v"],
        cwd=str(AF), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1.2)
    pid = _pid()
    return jsonify({"ok": bool(pid), "pid": pid})

@app.route("/api/daemon/stop", methods=["POST"])
def daemon_stop():
    pid = _pid()
    if not pid:
        return jsonify({"ok": True})
    try:
        os.kill(pid, signal.SIGTERM)
        (AF / ".filer.pid").unlink(missing_ok=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/once", methods=["POST"])
def once():
    dry = (request.json or {}).get("dry_run", False)
    cmd = ["python3", str(AF / "auto_filer.py"), "--once", "-v"]
    if dry:
        cmd.append("--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(AF), timeout=120)
    return jsonify({"ok": r.returncode == 0, "output": (r.stdout + r.stderr).strip()})

@app.route("/api/cases")
def cases():
    return jsonify({"cases": _config().get("cases", [])})

@app.route("/api/cases/<key>/desensitize", methods=["POST"])
def desensitize_case(key):
    """整案脱敏：镜像生成"脱敏版"案件空间（cases_脱敏/案件名/…，归类层次一致）。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    if _DESENS_JOB["running"]:
        return jsonify({"ok": False, "error": "已有脱敏任务在运行"})
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    src = Path(case.get("cases_dir") or paths["cases"]) / case["name"]
    if not src.exists():
        return jsonify({"ok": False, "error": "案件文件夹不存在"})
    dst = Path(paths["cases"]).parent / "cases_脱敏" / case["name"]
    _DESENS_JOB.update(running=True, done=False, case=case["name"], i=0, total=0,
                       current="", report=None, error=None, dst=str(dst))
    threading.Thread(target=_run_desensitize,
                     args=(str(src), str(dst), key), daemon=True).start()
    return jsonify({"ok": True, "dst": str(dst)})

@app.route("/api/desensitize/status")
def desensitize_status():
    return jsonify(_DESENS_JOB)


# ══════════════════════════════════════════════════════════════
# 脱敏工作台：单文件精细复核（识别→人工删补→左右预览→导出）
# 会话级 Registry 按案缓存，保证同一案件内跨文件化名一致；
# NER / OCR 惰性单例，连不上 Ollama 时自动降级为纯正则。
# ══════════════════════════════════════════════════════════════
_DESENS_REG = {}                       # case_key -> Registry
_DESENS_LAZY = {"ner": None, "ocr": None}   # 惰性单例（None=未初始化, False=不可用）

# 可提取文本的类型：文档走 to_markdown，图片/扫描件走 OCR
_DESENS_DOC = {".txt", ".md", ".docx", ".doc", ".pdf"}
_DESENS_IMG = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
_DESENS_EXT = _DESENS_DOC | _DESENS_IMG


def _get_registry(key):
    reg = _DESENS_REG.get(key)
    if reg is None:
        reg = _DESENS_REG[key] = Registry(lang="zh")
    return reg


def _get_ner():
    """惰性拿 NER 单例；连不上 Ollama 返回 None（调用方降级为纯正则）。"""
    if _DESENS_LAZY["ner"] is None:
        try:
            _DESENS_LAZY["ner"] = QwenNER(model="qwen2.5:7b", lang="zh")
        except Exception:
            _DESENS_LAZY["ner"] = False
    return _DESENS_LAZY["ner"] or None


def _get_ocr():
    """惰性拿 OCR 单例；不可用返回 None。"""
    if _DESENS_LAZY["ocr"] is None:
        try:
            _DESENS_LAZY["ocr"] = make_ocr()
        except Exception:
            _DESENS_LAZY["ocr"] = False
    return _DESENS_LAZY["ocr"] or None


def _resolve_case_file(key, rel):
    """把 (案件 key, 相对路径) 解析成绝对路径，并防目录穿越。"""
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return None, None, "案件不存在"
    root = (Path(case.get("cases_dir") or paths["cases"]) / case["name"]).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return None, None, "非法路径"
    if not target.exists() or not target.is_file():
        return None, None, "文件不存在"
    return case, target, None


@app.route("/api/desensitize/files")
def desensitize_wb_files():
    """列出某案可脱敏的文件（扁平化 + 相对路径 + 可提取类型过滤）。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    key = request.args.get("case", "")
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    root = Path(case.get("cases_dir") or paths["cases"]) / case["name"]
    if not root.exists():
        return jsonify({"ok": True, "exists": False, "files": [], "name": case["name"]})
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in _DESENS_EXT:
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        st = p.stat()
        files.append({"rel": str(rel), "name": p.name, "size": st.st_size,
                      "ext": p.suffix.lower().lstrip("."),
                      "dir": str(rel.parent) if str(rel.parent) != "." else ""})
    return jsonify({"ok": True, "exists": True, "files": files, "name": case["name"]})


@app.route("/api/desensitize/analyze", methods=["POST"])
def desensitize_wb_analyze():
    """识别一段文本/一个文件的敏感信息，返回原文 + span 列表（不提交注册表）。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    key = data.get("case", "")
    text = data.get("text")            # 直接传文本（粘贴模式）优先
    if text is None:
        case, path, err = _resolve_case_file(key, data.get("rel", ""))
        if err:
            return jsonify({"ok": False, "error": err})
        try:
            ext = path.suffix.lower()
            if ext in (".txt", ".md"):        # 纯文本无需提取，直接读
                text = path.read_text(encoding="utf-8", errors="ignore")
            else:
                ocr = _get_ocr() if ext in _DESENS_IMG else None
                text = to_markdown(str(path), ocr_engine=ocr)
        except Exception as e:
            return jsonify({"ok": False, "error": f"文本提取失败：{type(e).__name__}: {e}"})
    if not (text or "").strip():
        return jsonify({"ok": True, "text": "", "spans": [], "ner": False,
                        "msg": "未提取到文本内容"})
    reg = _get_registry(key or "_paste")
    ner = _get_ner()
    try:
        spans = analyze_text(text, reg, ner=ner)
    except Exception:
        ner = None
        spans = analyze_text(text, reg, ner=None)   # NER 失败→纯正则兜底
    return jsonify({"ok": True, "text": text, "spans": spans,
                    "ner": ner is not None, "count": len(spans)})


@app.route("/api/desensitize/apply", methods=["POST"])
def desensitize_wb_apply():
    """按人工复核后的 span 清单脱敏；mode=alias|blackout|asterisk；可选导出到磁盘。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    key = data.get("case", "")
    text = data.get("text", "")
    spans = data.get("spans", [])
    mode = data.get("mode", "alias")
    if mode not in ("alias", "blackout", "asterisk"):
        mode = "alias"
    reg = _get_registry(key or "_paste")
    try:
        masked, applied = apply_review(text, spans, reg, mode=mode)
    except Exception as e:
        return jsonify({"ok": False, "error": f"脱敏失败：{type(e).__name__}: {e}"})
    saved = ""
    if data.get("save") and data.get("rel"):
        case, path, err = _resolve_case_file(key, data["rel"])
        if not err:
            out_root = Path(_paths()["cases"]).parent / "cases_脱敏工作台" / case["name"]
            rel = Path(data["rel"])
            dst = out_root / rel.parent / f"{rel.stem}.脱敏.md"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(masked, encoding="utf-8")
            saved = str(dst)
    pending = [{"alias": e.alias, "canonical": e.canonical, "category": e.category}
               for e in reg.pending_review()]
    return jsonify({"ok": True, "masked": masked, "applied": applied,
                    "saved": saved, "mode": mode, "pending": pending})


@app.route("/api/desensitize/reset", methods=["POST"])
def desensitize_wb_reset():
    """清空某案的会话级 Registry（重新开始，化名重新编号）。"""
    key = (request.json or {}).get("case", "") or "_paste"
    _DESENS_REG.pop(key, None)
    return jsonify({"ok": True})

@app.route("/api/inbox")
def inbox_list():
    inbox = Path(_paths()["inbox"])
    files = []
    if inbox.exists():
        for f in sorted(inbox.iterdir()):
            if f.is_file() and not f.name.startswith(".") \
               and f.name not in {"README.txt", "README.md", ".gitkeep"}:
                st = f.stat()
                files.append({
                    "name":  f.name,
                    "size":  st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
                    "ext":   f.suffix.lower().lstrip(".") or "file",
                })
    return jsonify({"files": files})

@app.route("/api/log")
def log_tail():
    lf = _log_file()
    lines = []
    if lf.exists():
        lines = lf.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
    return jsonify({"lines": lines})

@app.route("/api/meta")
def meta():
    """供"投放预填"使用：案件列表 + 文件夹分类体系（来自规则 target）。"""
    cfg = _config()
    cases = [{"key": c.get("key", ""), "name": c.get("name", ""),
              "scenario": c.get("scenario", ""),
              "party_us": c.get("party_us", []), "party_op": c.get("party_op", []),
              "party_third": c.get("party_third", [])}
             for c in cfg.get("cases", []) if c.get("status") != "closed"]
    targets, seen = [], set()
    for r in cfg.get("rules", []):
        t = r.get("target", "")
        if t and t not in seen:
            seen.add(t)
            targets.append(t)
    return jsonify({"cases": cases, "targets": sorted(targets)})

@app.route("/api/inbox/upload", methods=["POST"])
def upload_to_inbox():
    inbox = Path(_paths()["inbox"])
    inbox.mkdir(parents=True, exist_ok=True)
    files = request.files.getlist("files")

    # 人工预填元数据：metas=逐个数组（与 files 同序）；meta=整批共用（向后兼容）
    def _clean(d):
        return {k: v for k, v in (d or {}).items() if v} if isinstance(d, dict) else {}
    metas = None
    if request.form.get("metas"):
        try:
            metas = json.loads(request.form["metas"])
        except Exception:
            metas = None
    override = {}
    if request.form.get("meta"):
        try:
            override = _clean(json.loads(request.form["meta"]))
        except Exception:
            override = {}

    saved, errors = [], []
    for idx, f in enumerate(files):
        name = f.filename or "upload"
        dest = inbox / name
        if dest.exists():
            stem, suffix = Path(name).stem, Path(name).suffix
            i = 1
            while dest.exists():
                dest = inbox / f"{stem}_{i}{suffix}"
                i += 1
        try:
            f.save(str(dest))
            saved.append(dest.name)
            ov = _clean(metas[idx]) if (metas and idx < len(metas)) else override
            if ov:
                sidecar = inbox / f".{dest.name}.loci.json"
                sidecar.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            errors.append(str(e))
    return jsonify({"ok": True, "saved": saved, "count": len(saved),
                    "errors": errors})

@app.route("/api/cases/open", methods=["POST"])
def open_case_folder():
    key   = (request.json or {}).get("key", "")
    cfg   = _config()
    paths = _paths()
    case  = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    folder = Path(case.get("cases_dir") or paths["cases"]) / case["name"]
    if not folder.exists():
        return jsonify({"ok": False, "error": f"文件夹不存在：{folder}"})
    subprocess.run(["open", str(folder)])
    return jsonify({"ok": True})

@app.route("/api/cases/<key>/files")
def case_files(key):
    cfg   = _config()
    paths = _paths()
    case  = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    folder = Path(case.get("cases_dir") or paths["cases"]) / case["name"]
    if not folder.exists():
        return jsonify({"ok": True, "exists": False, "path": str(folder)})

    SKIP = {".DS_Store", "Thumbs.db", ".gitkeep", "desktop.ini"}

    def scan(path, depth=0):
        items = []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return items
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP:
                continue
            if e.is_dir():
                children = scan(e, depth + 1) if depth < 5 else []
                items.append({"type": "dir", "name": e.name, "children": children})
            else:
                st = e.stat()
                items.append({
                    "type": "file",
                    "name": e.name,
                    "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%y-%m-%d"),
                })
        return items

    tree = scan(folder)
    total = sum(1 for f in folder.rglob("*")
                if f.is_file() and not f.name.startswith(".") and f.name not in SKIP)
    return jsonify({"ok": True, "exists": True, "path": str(folder),
                    "name": case["name"], "tree": tree, "total_files": total})

@app.route("/api/cases/<key>/folders")
def case_folders(key):
    """返回案件磁盘上真实存在的全部子文件夹（相对路径，所有层级）——供手工预填选目标用。"""
    cfg   = _config()
    paths = _paths()
    case  = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在", "folders": []})
    root = Path(case.get("cases_dir") or paths["cases"]) / case["name"]
    folders = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if not p.is_dir():
                continue
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            folders.append(str(rel))
    return jsonify({"ok": True, "exists": root.exists(), "folders": folders})

def _archive_log_append(entry):
    try:
        log_dir = Path(_paths()["logs"]); log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
        with open(log_dir / "archive_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

@app.route("/api/review")
def review_queue():
    """待办复核：待确认来源 + 未识别案件(_未分类) + 低置信命名的文件。"""
    cfg = _config()
    paths = _paths()
    items, seen = [], set()
    # 1) 待确认来源（各案件 _待确认来源 下，需人工选我方/对方）
    for c in cfg.get("cases", []):
        root = Path(c.get("cases_dir") or paths["cases"]) / c.get("name", "") / "_待确认来源"
        if not root.exists():
            continue
        for f in sorted(root.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                info = {}
                pend = root / f".{f.name}.pending.json"
                if pend.exists():
                    try:
                        info = json.loads(pend.read_text(encoding="utf-8"))
                    except Exception:
                        info = {}
                items.append({"name": f.name, "path": str(f), "reason": "待确认来源",
                              "loc": c.get("name", "") + "/_待确认来源",
                              "need_source": True, "case_key": c.get("key"),
                              "pending_target": info.get("pending_target", "")})
                seen.add(str(f))
    # 2) _未分类
    uncat = Path(paths["cases"]) / "_未分类"
    if uncat.exists():
        for f in sorted(uncat.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                items.append({"name": f.name, "path": str(f), "reason": "未识别案件",
                              "loc": "_未分类/" + str(f.relative_to(uncat).parent)})
                seen.add(str(f))
    log = Path(paths["logs"]) / "archive_log.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines()[-800:]:
            try:
                e = json.loads(line)
            except Exception:
                continue
            p = e.get("to")
            if (e.get("dt_conf", 1.0) < 0.5 and e.get("case_key") and p
                    and p not in seen and Path(p).exists()):
                items.append({"name": e.get("new_name"), "path": p,
                              "reason": f"命名依据弱（{e.get('dt_src')}）",
                              "loc": (e.get("case") or "") + "/" + (e.get("target") or "")})
                seen.add(p)
    return jsonify({"items": items, "count": len(items)})

@app.route("/api/review/reclassify", methods=["POST"])
def review_reclassify():
    data = request.json or {}
    src = Path(data.get("path", ""))
    if not src.exists():
        return jsonify({"ok": False, "error": "文件不存在（可能已被移动）"})
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == data.get("case_key")), None)
    if not case:
        return jsonify({"ok": False, "error": "请选择案件"})
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"ok": False, "error": "请选择目标文件夹"})
    new_name = (data.get("new_name") or src.name).strip()
    new_name = re.sub(r'[\\/:*?"<>|：]', '', new_name)
    dest_dir = Path(case.get("cases_dir") or paths["cases"]) / case["name"] / target
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / new_name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(new_name).stem}_{i}{Path(new_name).suffix}"
        i += 1
    try:
        import shutil
        shutil.move(str(src), str(dest))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    _archive_log_append({"action": "reclassify", "original_name": src.name,
                         "from": str(src), "new_name": dest.name, "to": str(dest),
                         "case": case["name"], "case_key": case["key"],
                         "target": target, "manual": True, "dt_conf": 1.0,
                         "dt_src": "manual", "needs_review": False})
    return jsonify({"ok": True, "to": str(dest)})

@app.route("/api/review/reprocess_uncat", methods=["POST"])
def reprocess_uncat():
    """把 _未分类 里的所有文件移回 inbox，触发 --once 重新归类。"""
    import shutil
    paths = _paths()
    inbox = Path(paths["inbox"])
    inbox.mkdir(parents=True, exist_ok=True)
    uncat = Path(paths["cases"]) / "_未分类"
    moved = 0
    if uncat.exists():
        for f in sorted(uncat.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                dest = inbox / f.name
                i = 1
                while dest.exists():
                    dest = inbox / f"{f.stem}_{i}{f.suffix}"
                    i += 1
                shutil.move(str(f), str(dest))
                moved += 1
            except Exception:
                pass
    subprocess.Popen(
        ["python3", str(AF / "auto_filer.py"), "--once"],
        cwd=str(WS), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return jsonify({"ok": True, "moved": moved,
                    "msg": f"已移回收件箱 {moved} 个文件，正在后台重新归类…"})

@app.route("/api/review/confirm_source", methods=["POST"])
def confirm_source():
    """确认来源后，把挂起在 _待确认来源 的文件归到我方/对方证据。"""
    import shutil
    data = request.json or {}
    src  = Path(data.get("path", ""))
    side = data.get("side")  # "我方证据" / "对方证据"
    if not src.exists():
        return jsonify({"ok": False, "error": "文件不存在（可能已被移动）"})
    if side not in ("我方证据", "对方证据"):
        return jsonify({"ok": False, "error": "请选择来源（我方/对方）"})
    pend = src.parent / f".{src.name}.pending.json"
    info = {}
    if pend.exists():
        try:
            info = json.loads(pend.read_text(encoding="utf-8"))
        except Exception:
            info = {}
    pending_target = info.get("pending_target") or data.get("pending_target", "")
    if "{side}" not in pending_target:
        return jsonify({"ok": False, "error": "缺少候选目标，无法确认"})
    target = pending_target.replace("{side}", side)
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == info.get("case_key")), None)
    if not case:
        return jsonify({"ok": False, "error": "找不到对应案件"})
    dest_dir = Path(case.get("cases_dir") or paths["cases"]) / case["name"] / target
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    i = 1
    while dest.exists():
        dest = dest_dir / f"{Path(src.name).stem}_{i}{Path(src.name).suffix}"
        i += 1
    try:
        shutil.move(str(src), str(dest))
        if pend.exists():
            pend.unlink()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    _archive_log_append({"action": "confirm_source", "original_name": src.name,
                         "new_name": dest.name, "to": str(dest), "case": case["name"],
                         "case_key": case["key"], "target": target,
                         "source": "ours" if side == "我方证据" else "opposing",
                         "manual": True, "needs_review": False})
    return jsonify({"ok": True, "to": str(dest)})

@app.route("/api/cases/update", methods=["POST"])
def update_case():
    data     = request.json or {}
    key      = data.get("key", "").strip()
    cfg_path = AF / "config.yaml"
    cfg      = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    case     = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": f"案件 {key!r} 不存在"})

    if "keywords" in data:
        case["keywords"] = data["keywords"]
    for pf in ("party_us", "party_op", "party_court", "party_third"):
        if pf in data:
            vals = [v.strip() for v in (data[pf] or []) if v and v.strip()]
            if vals:
                case[pf] = vals
            elif pf in case:
                del case[pf]
    case_number = (data.get("case_number") or "").strip()
    if case_number:
        case["case_number"] = case_number
    elif "case_number" in case:
        del case["case_number"]
    scenario = (data.get("scenario") or "").strip()
    if scenario:
        case["scenario"] = scenario
    elif "scenario" in case:
        del case["scenario"]
    cases_dir = (data.get("cases_dir") or "").strip()
    if cases_dir:
        case["cases_dir"] = cases_dir
    elif "cases_dir" in case:
        del case["cases_dir"]

    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120),
        encoding="utf-8"
    )
    return jsonify({"ok": True})

@app.route("/api/cases/status", methods=["POST"])
def case_status():
    data = request.json or {}
    key = (data.get("key") or "").strip()
    status = data.get("status")  # "active" / "closed"
    cfg_path = AF / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": f"案件 {key!r} 不存在"})
    if status == "closed":
        case["status"] = "closed"
    elif "status" in case:
        del case["status"]
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120),
        encoding="utf-8"
    )
    return jsonify({"ok": True})

@app.route("/api/rules")
def get_rules():
    return jsonify({"rules": _config().get("rules", [])})

@app.route("/api/rules", methods=["POST"])
def save_rules():
    cfg_path = AF / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["rules"] = request.json.get("rules", [])
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False,
                  sort_keys=False, width=120),
        encoding="utf-8"
    )
    return jsonify({"ok": True})

@app.route("/api/intake", methods=["POST"])
def intake():
    sys.path.insert(0, str(BIN))
    from intake import (create_folders, append_case_to_config,
                        write_meta, extract_keywords, update_env_cases_dir)
    data = request.json or {}
    try:
        cases_dir = Path(data.get("cases_dir") or _paths()["cases"])
        base = cases_dir / data["case_name"]
        if base.exists():
            return jsonify({"ok": False, "error": f"文件夹已存在：{base}"})

        # 简称(=案件 key=文件名前缀) 校验：非空、文件名安全、唯一
        abbr = (data.get("abbr") or "").strip()
        if not abbr:
            return jsonify({"ok": False, "error": "请填写案件简称"})
        if re.search(r'[\\/:*?"<>|：\s]', abbr):
            return jsonify({"ok": False, "error": f"简称不能含空格或 / \\ : * ? 等字符：{abbr!r}"})
        existing = {c.get("key") for c in _config().get("cases", [])}
        if abbr in existing:
            return jsonify({"ok": False, "error": f"简称 {abbr!r} 已被占用，请换一个"})

        raw   = data.get("raw_text", "")
        kws   = extract_keywords(raw) if raw.strip() else []
        if abbr and abbr not in kws:
            kws.insert(0, abbr)
        data["keywords"]      = kws
        data["parties_other"] = data.get("parties_other", [])

        create_folders(base, data["proc_type"], data["role"])
        if not append_case_to_config(AF / "config.yaml", abbr, data["case_name"], kws):
            return jsonify({"ok": False, "error": f"写入配置失败：简称 {abbr!r} 可能已存在"})
        write_meta(base, data)

        # 写入场景(用于场景硬隔离) + 案号
        scenario = PROC_SCENARIO.get(data.get("proc_type"))
        case_number = (data.get("case_number") or "").strip()
        if scenario or case_number:
            _cfg = yaml.safe_load((AF / "config.yaml").read_text(encoding="utf-8"))
            _case = next((c for c in _cfg.get("cases", []) if c.get("key") == abbr), None)
            if _case:
                if scenario:
                    _case["scenario"] = scenario
                if case_number:
                    _case["case_number"] = case_number
                (AF / "config.yaml").write_text(
                    yaml.dump(_cfg, allow_unicode=True, default_flow_style=False,
                              sort_keys=False, width=120),
                    encoding="utf-8"
                )

        if data.get("update_env"):
            update_env_cases_dir(WS / ".env", str(cases_dir))

        return jsonify({"ok": True, "path": str(base), "keywords": kws})
    except Exception as e:
        import traceback
        return jsonify({"ok": False, "error": str(e), "trace": traceback.format_exc()})

if __name__ == "__main__":
    app.run(port=5081, debug=True, use_reloader=False)
