#!/usr/bin/env python3
"""案件工作区可视化前台 — 本地 Web 控制台"""

import os, sys, re, json, subprocess, shutil, threading, tempfile
import urllib.request as _urllib_req
from pathlib import Path
from datetime import datetime

import html as _html_mod
import mimetypes

import yaml
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file, abort

sys.path.insert(0, str(Path(__file__).parent.parent / "auto_filer"))
sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
try:
    from intake import create_folders as _create_folders
    _HAS_INTAKE = True
except Exception:
    _HAS_INTAKE = False

_SCENARIO_PROC = {
    "中国民事": "civil",
    "中国仲裁": "arbitration",
    "中国刑事": "criminal",
    "美国民事": "us_civil",
    "美国仲裁": "us_civil",
}

app = Flask(__name__, template_folder="templates", static_folder="static")
WS_DIR = Path(__file__).parent.parent.resolve()
AF_DIR = WS_DIR / "auto_filer"


def _load_env():
    env = {}
    env_file = WS_DIR / ".env"
    if env_file.exists():
        raw = {}
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip().strip('"').strip("'")
        for k, v in raw.items():
            env[k] = os.path.expandvars(
                v.replace("$CASE_WORKSPACE_ROOT", raw.get("CASE_WORKSPACE_ROOT", ""))
            )
    return env


ENV = _load_env()
INBOX = Path(ENV.get("INBOX_DIR", WS_DIR / "inbox"))
CASES = Path(ENV.get("CASES_DIR", WS_DIR / "cases"))
LOGS = Path(ENV.get("LOG_DIR", WS_DIR / "logs"))


def _fmt_size(n):
    if n < 1024: return f"{n}B"
    if n < 1048576: return f"{n/1024:.1f}K"
    return f"{n/1048576:.1f}M"


def _inbox_files():
    if not INBOX.exists():
        return []
    return [f for f in INBOX.iterdir()
            if f.is_file() and not f.name.startswith(".") and f.name != "README.txt"]


def _load_config():
    p = AF_DIR / "config.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def _case_meta_map():
    return {c.get("name", ""): c for c in _load_config().get("cases", [])}


def _case_dir(case_name: str) -> Path:
    """解析案件目录：导入案件优先用登记时记下的真实文件夹路径 import_path
    （因为显示名可被改，不能靠 cases_dir/<name> 反推）；否则配置里设了 cases_dir
    用自定义目录，再否则默认 CASES/<name>。列表/树/脱敏/归档等才能正确定位。"""
    m = _case_meta_map().get(case_name, {})
    ip = (m.get("import_path") or "").strip()
    if ip:
        return Path(ip)
    cdir = (m.get("cases_dir") or "").strip()
    return (Path(cdir) / case_name) if cdir else (CASES / case_name)


# 层级序号：核心名匹配（与 intake/auto_filer 一致），归档移动前把 core 路径解析成真实目录
_LVL_PREFIX_RE = re.compile(r'^(?:\d{2}_|[A-Z]{1,2} - |\d+\. )')


def _core_seg(s: str) -> str:
    return _LVL_PREFIX_RE.sub("", s)


def _detect_layout(src: Path) -> str:
    """探测导入案件的目录结构，若与某审级分层模板吻合则返回对应 layout，否则返回 ""。
    只有明确匹配才标注——避免给结构不同的老案硬套分层、导致归档乱建目录。"""
    try:
        segs = set()
        for d in src.rglob("*"):
            if d.is_dir():
                segs.add(_core_seg(d.name))
                if len(segs) > 400:
                    break
    except Exception:
        return ""
    hit = lambda *keys: sum(1 for k in keys if any(k in s for s in segs))
    # 刑事：侦查/审查起诉/会见 是强特征
    if hit("侦查阶段", "审查起诉", "会见") >= 2 or hit("审查起诉阶段", "取保候审", "辩护") >= 2:
        return "staged_crim"
    # 仲裁：仲裁+裁决/司法审查
    if hit("仲裁") >= 1 and hit("裁决", "仲裁申请", "司法审查", "撤裁") >= 1:
        return "staged_arb"
    # 民事：一审/二审（审级）
    if hit("一审", "二审", "再审") >= 2:
        return "staged"
    return ""


def _resolve_case_dest(case_dir: Path, subdir: str) -> Path:
    """把 core 子目录路径解析成真实(带层级序号)目录；复用 auto_filer 前缀无关解析，失败兜底。"""
    if not subdir:
        return case_dir
    try:
        from auto_filer import _resolve_dest
        return _resolve_dest(case_dir, subdir)
    except Exception:
        return case_dir / subdir


def _case_folder_catalog(case_dir: Path, max_n: int = 120) -> list:
    """枚举案件的真实子文件夹（去层级序号的核心相对路径），作为"智能归位"候选清单。
    跳过隐藏目录和以 _ 开头的系统目录（_待分类/_待确认来源/_重复）。"""
    out = []
    for d in sorted(case_dir.rglob("*")):
        if not d.is_dir():
            continue
        rel = d.relative_to(case_dir)
        if any(p.startswith(".") or p.startswith("_") for p in rel.parts):
            continue
        out.append("/".join(_core_seg(s) for s in rel.parts))   # 去序号 → 核心名
        if len(out) >= max_n:
            break
    return out


def _owning_case(p: Path):
    """找出文件 p 属于哪个已登记案件，返回 (case_name, case_dir)。取最长匹配（最具体）。"""
    rp = p.resolve()
    best = (None, None, -1)
    for nm in _case_meta_map():
        if not nm:
            continue
        cd = _case_dir(nm).resolve()
        if _is_relative_to(rp, cd) and len(str(cd)) > best[2]:
            best = (nm, cd, len(str(cd)))
    return best[0], best[1]


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    pid_file = AF_DIR / ".filer.pid"
    running = False
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            pass

    return jsonify({
        "daemon_running": running,
        "daemon_pid": pid,
        "inbox_count": len(_inbox_files()),
        "case_count": sum(
            1 for d in CASES.iterdir()
            if d.is_dir() and not d.name.startswith(("_", "."))
        ) if CASES.exists() else 0,
    })


@app.route("/api/open", methods=["POST"])
def api_open():
    data = request.json or {}
    path = data.get("path", "")
    reveal = bool(data.get("reveal", False))  # True = Reveal in Finder; False = open with default app
    if not path:
        return jsonify({"error": "no path"}), 400
    p = Path(path)
    if not _path_allowed(p):
        return jsonify({"error": "path not in workspace"}), 403
    try:
        if reveal:
            subprocess.run(["open", "-R", str(p)], timeout=5, check=False)
        else:
            subprocess.run(["open", str(p)], timeout=5, check=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inbox")
def api_inbox():
    files = []
    for f in sorted(_inbox_files(), key=lambda x: x.stat().st_mtime, reverse=True):
        st = f.stat()
        files.append({
            "name": f.name,
            "size": _fmt_size(st.st_size),
            "ext": f.suffix.lower().lstrip(".") or "?",
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
            "path": str(f),
        })
    return jsonify({"files": files, "count": len(files)})


def _build_case_entry(d: Path, m: dict) -> dict:
    """把一个案件目录 + 其配置元数据组装成前端用的案件 dict。"""
    file_count = sum(
        1 for f in d.rglob("*")
        if f.is_file() and not f.name.startswith(".")
    )
    try:
        last_mod = max(
            f.stat().st_mtime for f in d.rglob("*")
            if f.is_file() and not f.name.startswith(".")
        )
        last_mod_str = datetime.fromtimestamp(last_mod).strftime("%Y-%m-%d")
    except (ValueError, StopIteration):
        last_mod_str = ""

    def _primary_names(detail_key, flat_key):
        detail = m.get(detail_key) or []
        if detail and isinstance(detail[0], dict):
            return [p["name"] for p in detail if p.get("name")]
        flat = m.get(flat_key) or []
        return [flat[0]] if flat else []

    return {
        # 导入案件的显示名可与文件夹名不同：优先用配置里的 name（= 各处定位用的 key），
        # 否则用文件夹名。保证前端拿到的 name 能被 _case_dir 正确反解到真实目录。
        "name": m.get("name") or d.name,
        "path": str(d),
        "files": file_count,
        "last_modified": last_mod_str,
        "status": m.get("status", "active"),
        "scenario": m.get("scenario", ""),
        "stage": m.get("stage", ""),
        "layout": m.get("layout", ""),
        "subject": m.get("subject", ""),
        "cause_of_action": m.get("cause_of_action") or [],
        "party_us": m.get("party_us") or [],
        "party_us_primary": _primary_names("party_us_detail", "party_us"),
        "party_op": m.get("party_op") or [],
        "party_op_primary": _primary_names("party_op_detail", "party_op"),
        "party_court": m.get("party_court") or [],
        "court_first": m.get("court_first", ""),
        "case_number": m.get("case_number", ""),
        "key": m.get("key", d.name),
        "keywords": m.get("keywords") or [],
        "our_firm": m.get("our_firm", ""),
        "our_attorneys": m.get("our_attorneys") or [],
        "amount": m.get("amount", ""),
    }


@app.route("/api/cases")
def api_cases():
    meta = _case_meta_map()
    case_list = []
    seen = set()

    # ① 默认 cases/ 目录下的案件
    if CASES.exists():
        for d in sorted(CASES.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            case_list.append(_build_case_entry(d, meta.get(d.name, {})))
            seen.add(d.name)

    # ② 存放在自定义目录（cases_dir）下、或导入登记（import_path）的案件——
    #    否则选了别的保存路径 / 改了显示名的导入案件就看不到
    for nm, m in meta.items():
        if not nm or nm in seen:
            continue
        ip = (m.get("import_path") or "").strip()
        cdir = (m.get("cases_dir") or "").strip()
        if ip:
            d = Path(ip)                 # 导入案件：用登记的真实文件夹
        elif cdir:
            d = Path(cdir) / nm
        else:
            continue
        if d.is_dir():
            case_list.append(_build_case_entry(d, m))
            seen.add(nm)

    case_list.sort(key=lambda c: c["name"])
    return jsonify({"cases": case_list})


# ── 卷宗结构预览：不建实际案件，在临时目录跑真实建卷逻辑再 walk 成树 ──
_FOLDER_TREE_CACHE = {}


def _dir_to_tree(base):
    """把目录 walk 成嵌套 [{name, children:[...]}]（按名排序，忽略 .gitkeep 占位文件）。"""
    out = []
    for c in sorted([x for x in base.iterdir() if x.is_dir()], key=lambda x: x.name):
        out.append({"name": c.name, "children": _dir_to_tree(c)})
    return out


@app.route("/api/folder-template")
def api_folder_template():
    """预览某类案件将创建的卷宗目录层次（不依赖实际案件）。

    在临时目录里跑与真实建卷完全相同的 _create_folders，再 walk 成树后删除临时目录，
    保证「预览 = 真实建卷」。结果按 (proc_type, role) 缓存。
    """
    if not _HAS_INTAKE:
        return jsonify({"ok": False, "error": "建卷模块未加载"}), 503
    scenario = request.args.get("scenario", "中国民事")
    role = request.args.get("role", "") or "plaintiff"
    proc_type = _SCENARIO_PROC.get(scenario, "civil")
    key = f"{proc_type}|{role}"
    if key not in _FOLDER_TREE_CACHE:
        tmp = Path(tempfile.mkdtemp(prefix="zl_tpl_"))
        try:
            _create_folders(tmp, proc_type, role)
            _FOLDER_TREE_CACHE[key] = _dir_to_tree(tmp)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return jsonify({"ok": False, "error": f"生成模板失败：{e}"}), 500
        shutil.rmtree(tmp, ignore_errors=True)
    tree = _FOLDER_TREE_CACHE[key]
    total = 0
    stack = list(tree)
    while stack:
        n = stack.pop(); total += 1; stack.extend(n["children"])
    return jsonify({"ok": True, "scenario": scenario, "proc_type": proc_type,
                    "role": role, "count": total, "tree": tree})


# 各类案件的可选阶段（与 auto_filer._STAGE_FOLDER 对齐，保证切换后归档能落到对应审级目录）
_STAGE_CHOICES = {
    "civil":       [["一审", "一审"], ["二审", "二审"], ["再审", "再审"], ["执行", "执行"]],
    "us_civil":    [["一审", "一审"], ["二审", "二审"], ["再审", "再审"], ["执行", "执行"]],
    "arbitration": [["仲裁", "仲裁"], ["司法审查", "司法审查"], ["执行", "执行"]],
    "criminal":    [["侦查", "侦查"], ["审查起诉", "审查起诉"], ["一审", "一审"],
                    ["二审", "二审"], ["再审", "再审申诉"], ["执行", "执行"]],
}


@app.route("/api/case/stage-options")
def api_case_stage_options():
    """返回某案件可切换的阶段清单（按案件类型）。criminal/导入/存量无分层的返回空。"""
    name = (request.args.get("name") or "").strip()
    m = _case_meta_map().get(name, {})
    proc = _SCENARIO_PROC.get(m.get("scenario", ""), "")
    staged = m.get("layout") in ("staged", "staged_arb", "staged_crim")
    choices = _STAGE_CHOICES.get(proc, []) if staged else []
    return jsonify({"ok": True, "name": name, "current": m.get("stage", ""),
                    "staged": bool(staged and choices), "choices": choices})


@app.route("/api/case/stage", methods=["POST"])
def api_case_stage():
    """推进/切换案件"当前阶段"（一审→二审→…），可同时补录该阶段案号。

    只改 config.yaml 里该案的 stage / case_number；卷宗目录（staged 布局各审级已预建）不动。
    切换后，未自带审级的通用材料（证据/法院来文/往来函件）自动归到新阶段目录。
    """
    data = request.json or {}
    name = (data.get("name") or "").strip()
    stage = (data.get("stage") or "").strip()
    cn = (data.get("case_number") or "").strip()
    if not name or not stage:
        return jsonify({"ok": False, "error": "缺少案件名或阶段"}), 400
    cfg_path = AF_DIR / "config.yaml"
    if not cfg_path.exists():
        return jsonify({"ok": False, "error": "config.yaml 不存在"}), 404
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    entry = next((c for c in cfg.get("cases", []) if c.get("name") == name), None)
    if entry is None:
        return jsonify({"ok": False, "error": "未找到该案件（可能是导入/存量案件，未登记到 config）"}), 404
    entry["stage"] = stage
    if cn:                                   # 补录该阶段案号（键=阶段名，供按案号判阶段）
        existing = entry.get("case_number")
        if isinstance(existing, dict):
            existing[stage] = cn
        elif existing:                       # 旧的单串案号（通常是初始审级）→ 升级为按阶段字典
            entry["case_number"] = ({"一审": existing, stage: cn}
                                    if stage != "一审" else {stage: cn})
        else:
            entry["case_number"] = {stage: cn}
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False, width=120),
        encoding="utf-8")
    return jsonify({"ok": True, "name": name, "stage": stage,
                    "case_number": entry.get("case_number")})


@app.route("/api/case/create", methods=["POST"])
def api_case_create():
    data = request.json or {}

    # 导入模式：登记一个已存在的案件文件夹，保留其原有结构，不新建、不铺子目录。
    import_existing = bool(data.get("import_existing"))
    import_path = (data.get("import_path") or "").strip()

    if import_existing:
        if not import_path:
            return jsonify({"error": "请选择要导入的案件文件夹"}), 400
        src = Path(import_path)
        if not src.is_dir():
            return jsonify({"error": "所选文件夹不存在"}), 404
        name = (data.get("name") or src.name).strip()    # 默认用文件夹名，允许改显示名
        if name in _case_meta_map():
            return jsonify({"error": "已有同名案件登记，请改用其它名称"}), 409
    else:
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"error": "案件名称不能为空"}), 400
        # 检查真正的目标目录：自定义路径或默认 cases/
        _custom = (data.get("cases_dir") or "").strip()
        _target = (Path(_custom) / name) if _custom else (CASES / name)
        if _target.exists():
            return jsonify({"error": "案件文件夹已存在"}), 409

    # Build case_number from instance fields
    cn_raw = {k: v.strip() for k, v in (data.get("case_numbers") or {}).items() if v.strip()}
    if len(cn_raw) == 0:
        case_number = None
    elif len(cn_raw) == 1:
        case_number = list(cn_raw.values())[0]
    else:
        case_number = cn_raw

    def csv_list(val):
        if not val:
            return []
        if isinstance(val, list):
            items = []
            for v in val:
                items.extend(re.split(r'[,，、；;\s]+', v))
            return [v.strip() for v in items if v.strip()]
        return [v.strip() for v in re.split(r'[,，、；;\s]+', str(val)) if v.strip()]

    # Auto-generate key from party names (shorter than name[:20])
    def _auto_key(name_str, parties_us, parties_op):
        if parties_us and parties_op:
            us = min(parties_us, key=len) if parties_us else ""
            op = min(parties_op, key=len) if parties_op else ""
            return f"{us} v. {op}"[:30]
        # Fallback: strip common suffixes and truncate
        k = re.sub(r'(有限公司|股份有限公司|（中国）|中国区|集团|控股)', '', name_str)
        return k[:15]

    # ── 各方当事人（支持每方多个）────────────────────────────────────────────
    def _collect_parties(list_key, name_key, alias_key, id_key):
        """返回 (平铺名单[名+别名+曾用名], 明细列表[{name,aliases?,former?,id?}])。
        曾用名与别名一样并入平铺识别名单（新旧名都能识别/脱敏），明细里单独存。
        新版前端传 *_list 数组；旧字段(单当事人)作兼容回退。"""
        rows = data.get(list_key)
        if not isinstance(rows, list) or not rows:
            rows = [{"name": data.get(name_key, ""),
                     "aliases": data.get(alias_key, ""),
                     "id": data.get(id_key, "")}]
        flat, details = [], []
        for r in rows:
            nm  = (r.get("name") or "").strip()
            aka = csv_list(r.get("aliases", ""))
            former = csv_list(r.get("former", ""))
            rid = (r.get("id") or "").strip()
            if not (nm or aka or former or rid):
                continue
            if nm:
                flat.append(nm)
            flat.extend(aka)
            flat.extend(former)          # 曾用名也进识别名单
            if nm:
                d = {"name": nm}
                if aka: d["aliases"] = aka
                if former: d["former"] = former
                if rid: d["id"] = rid
                details.append(d)
        return flat, details

    parties_us, us_details = _collect_parties("party_us_list", "party_us", "party_us_aliases", "party_us_id")
    parties_op, op_details = _collect_parties("party_op_list", "party_op", "party_op_aliases", "party_op_id")

    # ── 法院 ──────────────────────────────────────────────────────────────────
    court1      = (data.get("court_first") or "").strip()
    court1_aka  = csv_list(data.get("court_first_aliases", ""))
    court2      = (data.get("court_second") or "").strip()
    court2_aka  = csv_list(data.get("court_second_aliases", ""))
    party_court = ([court1] if court1 else []) + court1_aka + ([court2] if court2 else []) + court2_aka

    raw_key = (data.get("key") or "").strip()
    if not raw_key:
        raw_key = _auto_key(name, parties_us, parties_op)

    # Resolve cases_dir：导入=已存在文件夹的父目录；新建=路径选择器或默认 CASES
    if import_existing:
        custom_dir = str(Path(import_path).parent)
        case_root = Path(custom_dir)
    else:
        custom_dir = (data.get("cases_dir") or "").strip()
        case_root = Path(custom_dir) if custom_dir else CASES

    entry = {
        "key": raw_key,
        "name": name,
        "scenario": data.get("scenario", "中国民事"),
        "status": data.get("status", "active"),
    }
    stage = (data.get("stage") or "").strip()
    if stage:
        entry["stage"] = stage
    # 分层归档标记：新建的民事/仲裁/刑事案件按审级/阶段建目录并归档；导入案件保留原结构（不标）
    if not import_existing:
        _pt = _SCENARIO_PROC.get(entry.get("scenario", ""), "civil")
        if _pt == "civil":
            entry["layout"] = "staged"
        elif _pt == "arbitration":
            entry["layout"] = "staged_arb"
        elif _pt == "criminal":
            entry["layout"] = "staged_crim"
    if import_existing:
        entry["imported"] = True
        entry["import_path"] = str(src)     # 真实文件夹，改显示名后仍能定位
        # 探测原结构：若与审级分层模板吻合，标注 layout，让后续自动归档也走分层
        _det = _detect_layout(src)
        if _det:
            entry["layout"] = _det
    if custom_dir:
        entry["cases_dir"] = custom_dir
    if case_number:
        entry["case_number"] = case_number
    if data.get("subject"):
        entry["subject"] = data["subject"].strip()
    if data.get("amount"):
        entry["amount"] = data["amount"].strip()

    # 当事人（平铺列表用于识别，detail 用于展示/未来编辑）
    if parties_us:
        entry["party_us"] = parties_us
        if us_details: entry["party_us_detail"] = us_details
    if parties_op:
        entry["party_op"] = parties_op
        if op_details: entry["party_op_detail"] = op_details

    # 法院
    if party_court:
        entry["party_court"] = party_court
    if court1:
        entry["court_first"] = court1
        if court1_aka: entry["court_first_aliases"] = court1_aka
    if court2:
        entry["court_second"] = court2
        if court2_aka: entry["court_second_aliases"] = court2_aka

    # 代理信息
    if data.get("our_firm"):
        entry["our_firm"] = data["our_firm"].strip()
    attorneys = csv_list(data.get("our_attorneys", ""))
    if attorneys:
        entry["our_attorneys"] = attorneys

    for field in ["cause_of_action", "keywords"]:
        val = csv_list(data.get(field, ""))
        if val:
            entry[field] = val

    # Update config.yaml
    cfg_path = AF_DIR / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    cfg.setdefault("cases", []).append(entry)
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    # 导入模式：文件夹已存在，原样保留，不铺标准子目录。新建模式才建目录+脚手架。
    if import_existing:
        return jsonify({"ok": True, "name": name, "imported": True})

    case_dir = case_root / name
    case_dir.mkdir(parents=True, exist_ok=True)
    if _HAS_INTAKE:
        proc_type = _SCENARIO_PROC.get(entry.get("scenario", ""), "civil")
        role = data.get("role", "plaintiff")
        try:
            _create_folders(case_dir, proc_type, role)
        except Exception as e:
            app.logger.warning(f"create_folders failed: {e}")
    return jsonify({"ok": True, "name": name})


@app.route("/api/case/<path:case_name>/tree")
def api_case_tree(case_name):
    case_dir = _case_dir(case_name)
    if not case_dir.exists():
        return jsonify({"error": "not found"}), 404
    # nonempty=1：剪掉不含任何文件的空目录（分层脚手架有大量空文件夹，问答/脱敏选文件时无意义）
    nonempty = request.args.get("nonempty") in ("1", "true", "yes")

    def build_tree(path, depth=0):
        if depth > 4:
            return None
        node = {"name": path.name, "type": "dir", "path": str(path), "children": []}
        try:
            for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
                if item.name.startswith("."):          # .gitkeep 等占位/隐藏文件跳过
                    continue
                if item.is_dir():
                    child = build_tree(item, depth + 1)
                    if child:
                        node["children"].append(child)
                else:
                    node["children"].append({
                        "name": item.name,
                        "type": "file",
                        "size": _fmt_size(item.stat().st_size),
                        "path": str(item),
                    })
        except PermissionError:
            pass
        # 剪枝：子树里没有任何文件（只剩空目录/占位）→ 丢弃；根目录保留（depth 0）
        if nonempty and depth > 0 and not node["children"]:
            return None
        return node

    return jsonify(build_tree(case_dir))


@app.route("/api/log")
def api_log():
    jsonl = LOGS / "archive_log.jsonl"
    entries = []
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    # Newest first, last 80
    return jsonify({"entries": list(reversed(entries[-80:]))})


@app.route("/api/open-log", methods=["POST"])
def api_open_log():
    """在系统默认编辑器中打开归档日志，供人工修改。"""
    jsonl = LOGS / "archive_log.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not jsonl.exists():
        jsonl.write_text("", encoding="utf-8")
    try:
        subprocess.Popen(["open", str(jsonl)])
        return jsonify({"ok": True, "path": str(jsonl)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/clear-log", methods=["POST"])
def api_clear_log():
    """清空归档日志。为安全起见，清空前先把现有日志备份到 logs/archive_log.<时间戳>.bak。"""
    jsonl = LOGS / "archive_log.jsonl"
    backup = None
    try:
        if jsonl.exists() and jsonl.stat().st_size > 0:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak = LOGS / f"archive_log.{ts}.bak"
            bak.write_text(jsonl.read_text(encoding="utf-8", errors="ignore"),
                           encoding="utf-8")
            backup = str(bak)
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        jsonl.write_text("", encoding="utf-8")
        return jsonify({"ok": True, "backup": backup})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _preview_safe_path(path_str: str) -> Path:
    """Return Path if allowed; raise 403 otherwise."""
    if not path_str:
        abort(400)
    p = Path(path_str).resolve()
    if not _path_allowed(p):
        abort(403)
    if not p.exists() or not p.is_file():
        abort(404)
    return p

def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _custom_case_roots() -> list:
    """配置里登记的自定义 cases_dir 目录——这些目录下的案件文件也属"工作区内"，
    否则选了自定义保存路径（如 OneDrive）的案件，预览/重命名/删除/打开会 403。"""
    roots = []
    for c in _load_config().get("cases", []):
        cdir = (c.get("cases_dir") or "").strip()
        if cdir:
            try:
                roots.append(Path(cdir).resolve())
            except Exception:
                pass
    return roots


_EXTRA_ALLOWED_ROOTS: set = set()   # 用户在脱敏工具里选的输出目录，允许预览/在 Finder 显示


def _allowed_roots() -> list:
    return ([WS_DIR.resolve(), INBOX.resolve()] + _custom_case_roots()
            + [Path(r) for r in _EXTRA_ALLOWED_ROOTS])


def _path_allowed(p) -> bool:
    """路径是否落在任一允许的根目录内（默认工作区 + 收件箱 + 自定义案件目录）。"""
    try:
        rp = Path(p).resolve()
    except Exception:
        return False
    return any(_is_relative_to(rp, r) for r in _allowed_roots())

def _wrap_html(body_html: str, extra_style: str = "") -> str:
    return (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>body{{margin:0;font-family:-apple-system,sans-serif;font-size:13px;"
        f"line-height:1.65;color:#222;padding:32px 40px;max-width:860px}}"
        f"h1,h2,h3{{font-weight:600;margin:1.2em 0 .5em}}p{{margin:.5em 0}}"
        f"table{{border-collapse:collapse;width:100%;margin:12px 0}}"
        f"th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:12px}}"
        f"th{{background:#f5f5f5;font-weight:600}}"
        f"{extra_style}</style></head><body>{body_html}</body></html>"
    )

def _docx_preview_html(p: Path) -> str:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        doc = Document(str(p))
        parts = []

        def run_html(run):
            t = _html_mod.escape(run.text)
            if run.bold:   t = f"<b>{t}</b>"
            if run.italic: t = f"<i>{t}</i>"
            return t

        for block in doc.element.body:
            tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag
            if tag == "p":
                from docx.text.paragraph import Paragraph
                para = Paragraph(block, doc)
                text = "".join(run_html(r) for r in para.runs)
                if not text.strip():
                    parts.append("<br>")
                    continue
                sn = (para.style.name or "") if para.style else ""
                if   "Heading 1" in sn: parts.append(f"<h1>{text}</h1>")
                elif "Heading 2" in sn: parts.append(f"<h2>{text}</h2>")
                elif "Heading 3" in sn: parts.append(f"<h3>{text}</h3>")
                else:                   parts.append(f"<p>{text}</p>")
            elif tag == "tbl":
                from docx.table import Table
                tbl = Table(block, doc)
                rows_html = []
                for ri, row in enumerate(tbl.rows):
                    cells = "".join(
                        f"<{'th' if ri==0 else 'td'}>{_html_mod.escape(c.text)}</{('th' if ri==0 else 'td')}>"
                        for c in row.cells
                    )
                    rows_html.append(f"<tr>{cells}</tr>")
                parts.append(f"<table>{''.join(rows_html)}</table>")

        return _wrap_html("".join(parts))
    except Exception as e:
        return _wrap_html(f"<p style='color:red'>无法预览：{_html_mod.escape(str(e))}</p>")

def _xlsx_preview_html(p: Path) -> str:
    try:
        import pandas as pd
        sheets = pd.read_excel(str(p), sheet_name=None, nrows=300)
        parts = []
        for name, df in sheets.items():
            parts.append(f"<h3 style='margin:24px 0 8px;font-size:13px;color:#555'>{_html_mod.escape(str(name))}</h3>")
            parts.append(df.fillna("").to_html(index=False, border=0))
        return _wrap_html("".join(parts))
    except Exception as e:
        return _wrap_html(f"<p style='color:red'>无法预览：{_html_mod.escape(str(e))}</p>")


def _serve_file_preview(p):
    """按扩展名返回文件预览（pdf/图片直传，docx/xlsx/文本转 HTML）。供预览端点与收件箱预览共用。"""
    ext = p.suffix.lower()

    # ── PDF ──────────────────────────────────────────────────────────────────
    if ext == ".pdf":
        return send_file(str(p), mimetype="application/pdf",
                         download_name=p.name, as_attachment=False)

    # ── 图片 ─────────────────────────────────────────────────────────────────
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"):
        mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        return send_file(str(p), mimetype=mime)

    # ── DOCX / DOC ───────────────────────────────────────────────────────────
    if ext == ".docx":
        html = _docx_preview_html(p)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    if ext == ".doc":
        return _wrap_html("<p style='color:#888'>DOC 格式需转换耗时较长，请直接在 Word 中打开。</p>"), \
               200, {"Content-Type": "text/html; charset=utf-8"}

    # ── XLSX / XLS ───────────────────────────────────────────────────────────
    if ext in (".xlsx", ".xls"):
        html = _xlsx_preview_html(p)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    # ── 纯文本 ────────────────────────────────────────────────────────────────
    if ext in (".txt", ".md", ".csv", ".log", ".json", ".yaml", ".yml"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            text = "(无法读取)"
        escaped = _html_mod.escape(text)
        body = f"<pre style='white-space:pre-wrap;word-break:break-word;font-size:12px;font-family:monospace'>{escaped}</pre>"
        return _wrap_html(body), 200, {"Content-Type": "text/html; charset=utf-8"}

    # ── 不支持 ────────────────────────────────────────────────────────────────
    return _wrap_html(f"<p style='color:#888;padding:40px 0'>不支持预览 {_html_mod.escape(ext)} 格式</p>"), \
           200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/preview")
def api_preview():
    """本地文件预览端点（仅限工作区内部文件）。"""
    return _serve_file_preview(_preview_safe_path(request.args.get("path", "")))


@app.route("/api/inbox-file")
def api_inbox_file():
    """预览收件箱里的文件（按文件名，防目录穿越）——供归类弹窗右侧核查用。"""
    name = request.args.get("name", "")
    p = (INBOX / name).resolve()
    if p.parent != INBOX.resolve() or not p.exists() or not p.is_file():
        abort(404)
    return _serve_file_preview(p)


@app.route("/api/browse-directory", methods=["POST"])
def api_browse_directory():
    """弹原生文件夹对话框，返回所选路径。

    必须用 pywebview 的 create_file_dialog：它会把对话框 marshal 到主 GUI 线程。
    之前用 tkinter(tk.Tk) 是从 Flask 工作线程里跑 Cocoa，与 pywebview 抢 macOS
    主线程 → 进程闪退。浏览器模式（无原生窗口）回退到前端手动输入路径。
    """
    initial = (request.json or {}).get("initial") or str(CASES)
    try:
        import webview
        wins = getattr(webview, "windows", None)
        if wins:
            res = wins[0].create_file_dialog(webview.FOLDER_DIALOG, directory=initial)
            if res:
                path = res[0] if isinstance(res, (list, tuple)) else res
                return jsonify({"ok": True, "path": path})
            return jsonify({"ok": False, "path": ""})        # 用户取消
    except Exception as e:
        return jsonify({"ok": False, "path": "", "manual": True, "error": str(e)})
    # pywebview 不可用（浏览器模式）→ 让前端提示手动输入
    return jsonify({"ok": False, "path": "", "manual": True})


# ── LLM Preflight（异步，上传后立即后台运行）───────────────────────────────
_preflight_cache: dict = {}   # filename → result dict
_preflight_lock  = threading.Lock()

def _run_preflight(filename: str, filepath: Path) -> None:
    """后台线程：OCR + Qwen 首读，结果写入内存缓存和 preflight sidecar。"""
    try:
        from llm_reader import llm_preflight, map_to_target
        cfg_path = AF_DIR / "config.yaml"
        rules = []
        try:
            rules = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}).get("rules", [])
        except Exception:
            pass
        result = llm_preflight(str(filepath), rules)
    except Exception as e:
        result = {"ok": False, "error": str(e), "method": "error"}

    # 写 preflight sidecar（供 auto_filer 归档时对比）
    try:
        sidecar = filepath.parent / f".{filename}.loci.preflight.json"
        sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    with _preflight_lock:
        _preflight_cache[filename] = result


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "files" not in request.files:
        return jsonify({"error": "no files"}), 400
    uploaded = []
    for f in request.files.getlist("files"):
        if f.filename:
            fp = INBOX / f.filename
            f.save(str(fp))
            uploaded.append(f.filename)
            with _preflight_lock:
                _preflight_cache.pop(f.filename, None)
    return jsonify({"uploaded": uploaded, "count": len(uploaded)})


@app.route("/api/preflight/trigger/<path:filename>", methods=["POST"])
def api_preflight_trigger(filename: str):
    """用户选择智能识别后，按需启动 LLM 首读线程。"""
    filepath = INBOX / filename
    if not filepath.is_file():
        return jsonify({"ok": False, "error": "文件不在收件箱"})
    with _preflight_lock:
        _preflight_cache.pop(filename, None)
    threading.Thread(target=_run_preflight, args=(filename, filepath), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/preflight/<path:filename>")
def api_preflight(filename: str):
    """查询 LLM 首读结果。status: pending | done"""
    with _preflight_lock:
        result = _preflight_cache.get(filename)
    if result is None:
        return jsonify({"status": "pending"})
    return jsonify({"status": "done", "result": result})


@app.route("/api/classify-predict/<path:filename>")
def api_classify_predict(filename: str):
    """预测某收件箱文件的归类（案件 + 目标文件夹 + 文书类型），只预测不落盘。
    供「智能识别」预填表单、让用户最后确认再归档。"""
    p = (INBOX / filename).resolve()
    if p.parent != INBOX.resolve() or not p.exists() or not p.is_file():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    cfg   = _load_config()
    rules = cfg.get("rules", [])
    cases = cfg.get("cases", [])
    from llm_reader import extract_text, llm_preflight
    try:
        text, _ = extract_text(str(p))
    except Exception:
        text = ""
    # ① 案件匹配（复用归档引擎的 match_case：案号优先、再关键词）
    case_key = None
    try:
        import sys as _sys
        if str(AF_DIR) not in _sys.path:
            _sys.path.insert(0, str(AF_DIR))
        from auto_filer import match_case
        res = match_case(filename, text or "", cases)
        case_key = res[0] if res else None
    except Exception:
        pass
    # ② 目标文件夹 / 文书类型：优先已缓存的 LLM 首读，否则现算
    with _preflight_lock:
        pf = _preflight_cache.get(filename)
    if not (pf and pf.get("ok")):
        try:
            pf = llm_preflight(str(p), rules)
        except Exception:
            pf = {}
    return jsonify({"ok": True,
                    "case_key": case_key,
                    "target":  (pf or {}).get("llm_target"),
                    "doctype": (pf or {}).get("文件性质")})


@app.route("/api/classify-options")
def api_classify_options():
    """归类弹窗选项：案件列表 + 中国民事文件夹/类型树。"""
    cfg_path = AF_DIR / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}

    cases = [{"key": c.get("key", ""), "name": c.get("name", "")}
             for c in cfg.get("cases", []) if c.get("key")]

    rules = cfg.get("rules", [])
    cats: dict = {}
    for r in rules:
        sc = r.get("scenario", "通用")
        if sc not in ("通用", "中国民事"):
            continue
        target = r.get("target", "")
        rp = r.get("rename_pattern", "")
        if not target or not rp:
            continue
        top = target.split("/")[0]
        if top not in cats:
            label = re.sub(r"^\d+_", "", top)
            cats[top] = {"key": top, "label": label, "types": []}
        existing = {t["rp"] for t in cats[top]["types"]}
        if rp not in existing:
            cats[top]["types"].append({"target": target, "label": rp, "rp": rp})

    return jsonify({"cases": cases, "categories": list(cats.values())})


@app.route("/api/case-folders")
def api_case_folders():
    """某案件的真实文件夹（按 大类→子目录 分组），供归类弹窗按该案场景/结构出选项，
    解决刑事/仲裁/美国案件被套用民事文件夹的问题。target 保留 L1 序号、去 L2/L3 序号，
    落位时由 _resolve_case_dest 前缀无关解析回真实(带序号)目录。"""
    key = (request.args.get("key") or "").strip()
    name = (request.args.get("name") or "").strip()
    if key and not name:
        for c in _load_config().get("cases", []):
            if c.get("key") == key:
                name = c.get("name", ""); break
    if not name:
        return jsonify({"ok": False, "categories": []})
    cd = _case_dir(name)
    if not cd.is_dir():
        return jsonify({"ok": False, "categories": []})
    cats: dict = {}
    for p in sorted(cd.rglob("*")):
        if not p.is_dir():
            continue
        rel = p.relative_to(cd)
        if any(s.startswith((".", "_")) for s in rel.parts):
            continue
        top_raw = rel.parts[0]                       # 03_一审（保留 L1 序号供落位解析）
        cats.setdefault(top_raw, {"key": top_raw, "label": _core_seg(top_raw), "types": []})
        if len(rel.parts) < 2:                       # 仅顶层：作为大类桶，暂无可落"类型"
            continue
        parts = [top_raw] + [_core_seg(s) for s in rel.parts[1:]]  # 去 L2/L3 序号
        target = "/".join(parts)
        sub = "/".join(parts[1:])
        if not any(t["target"] == target for t in cats[top_raw]["types"]):
            cats[top_raw]["types"].append({"target": target, "label": sub, "rp": parts[-1]})
    return jsonify({"ok": True, "categories": list(cats.values())})


@app.route("/api/sidecar", methods=["POST"])
def api_sidecar():
    """为收件箱文件写入 .loci.json sidecar，指定归类参数。"""
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    if not (INBOX / filename).exists():
        return jsonify({"ok": False, "error": "文件不在收件箱"}), 404

    payload = {k: data[k] for k in ("case_key", "source", "target", "doc_type", "name")
               if data.get(k)}
    (INBOX / f".{filename}.loci.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"ok": True})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    def generate():
        inbox = _inbox_files()
        total = len(inbox)

        def sse(obj):
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

        yield sse({"type": "start", "total": total, "files": [f.name for f in inbox]})

        if total == 0:
            yield sse({"type": "done", "done": 0, "total": 0, "msg": "收件箱为空"})
            return

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(AF_DIR / "auto_filer.py"), "--once"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(AF_DIR),
            )
        except Exception as e:
            yield sse({"type": "error", "msg": str(e)})
            return

        done = 0
        cur_file = ""
        cur_status = "processing"  # processing | success | duplicate | pending | error
        cur_to = ""
        cur_target = ""
        seen_first = False

        for raw in proc.stdout:
            raw = raw.rstrip()
            if not raw:
                continue
            msg = raw.split(" | ", 1)[1].strip() if " | " in raw else raw.strip()

            if "📄 处理:" in msg:
                # Finish previous file
                if seen_first and cur_file:
                    done += 1
                    yield sse({"type": "file_done", "filename": cur_file,
                               "status": cur_status, "to": cur_to, "target": cur_target})
                seen_first = True
                m = re.search(r"处理:\s*(.+)$", msg)
                cur_file = m.group(1).strip() if m else msg
                cur_status = "processing"
                cur_to = cur_target = ""
                yield sse({"type": "file_start", "filename": cur_file})

            elif "♻ 重复文件" in msg:
                cur_status = "duplicate"

            elif "❓ 来源未确定" in msg or "_待确认来源" in msg:
                cur_status = "pending"

            elif "❌" in msg and cur_status == "processing":
                cur_status = "error"

            elif "✅ →" in msg:
                m = re.search(r"✅\s*→\s*(.+)$", msg)
                if m: cur_to = m.group(1).strip()
                cur_status = "success"

            elif "🔍 [DRY-RUN] →" in msg:
                m = re.search(r"→\s*(.+)$", msg)
                if m: cur_to = m.group(1).strip()
                if cur_status == "processing":
                    cur_status = "success"  # dry-run is still considered a "success" for display

            elif "♻ →" in msg:
                m = re.search(r"→\s*(.+?)（", msg)
                if m: cur_to = m.group(1).strip()

            elif "归类" in msg:
                m = re.search(r"归类[^：:]*[：:]\s*([^\s（(【\n]+)", msg)
                if m: cur_target = m.group(1)

            elif "映射表命中:" in msg:
                m = re.search(r"命中:\s*([^\s（(【\n]+)", msg)
                if m: cur_target = m.group(1)

            elif "📊" in raw:
                if seen_first and cur_file:
                    done += 1
                    yield sse({"type": "file_done", "filename": cur_file,
                               "status": cur_status, "to": cur_to, "target": cur_target})
                    cur_file = ""
                sm = re.search(r"(\d+)\s*成功\s*/\s*(\d+)\s*跳过\s*/\s*(\d+)\s*总计", raw)
                if sm:
                    yield sse({"type": "done", "done": int(sm.group(1)) + int(sm.group(2)),
                               "total": int(sm.group(3))})
                else:
                    yield sse({"type": "done", "done": done, "total": total})
                return

        proc.wait()
        if seen_first and cur_file:
            done += 1
            yield sse({"type": "file_done", "filename": cur_file,
                       "status": cur_status, "to": cur_to, "target": cur_target})
        yield sse({"type": "done", "done": done, "total": total})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/log/reposition", methods=["POST"])
def api_log_reposition():
    """移动/重命名已归档文件，并同步更新日志记录中的路径。"""
    data = request.json or {}
    from_path = (data.get("from") or "").strip()
    new_dir  = (data.get("new_dir") or "").strip()
    new_name = (data.get("new_name") or "").strip()

    if not from_path:
        return jsonify({"error": "缺少源路径"}), 400
    src = Path(from_path)
    if not src.exists():
        return jsonify({"error": f"文件不存在：{from_path}"}), 404

    dest_dir  = Path(new_dir) if new_dir else src.parent
    dest_name = new_name if new_name else src.name
    dest = dest_dir / dest_name

    if dest != src:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Patch the JSONL log entry in-place
    jsonl = LOGS / "archive_log.jsonl"
    if jsonl.exists():
        try:
            lines = jsonl.read_text(encoding="utf-8").splitlines()
            patched = []
            for line in lines:
                if not line.strip():
                    patched.append(line)
                    continue
                try:
                    e = json.loads(line)
                    if e.get("to") == from_path:
                        e["to"] = str(dest)
                        if new_name:
                            e["new_name"] = dest_name
                        line = json.dumps(e, ensure_ascii=False)
                except Exception:
                    pass
                patched.append(line)
            jsonl.write_text("\n".join(patched) + "\n", encoding="utf-8")
        except Exception:
            pass

    return jsonify({"ok": True, "path": str(dest)})


@app.route("/api/pending")
def api_pending():
    """Collect all files sitting in _待确认来源 across every case."""
    items = []
    if not CASES.exists():
        return jsonify({"items": [], "count": 0})
    for case_dir in sorted(CASES.iterdir()):
        if not case_dir.is_dir() or case_dir.name.startswith(("_", ".")):
            continue
        pending_dir = case_dir / "_待确认来源"
        if not pending_dir.exists():
            continue
        for f in sorted(pending_dir.iterdir(), key=lambda x: x.stat().st_mtime):
            if f.name.startswith(".") or f.suffix.lower() == ".json":
                continue
            sidecar = pending_dir / f".{f.name}.pending.json"
            meta = {}
            if sidecar.exists():
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    pass
            items.append({
                "name": f.name,
                "path": str(f),
                "case": meta.get("case_key") or meta.get("case") or case_dir.name,
                "doc_type": meta.get("doc_type", ""),
                "pending_target": meta.get("pending_target", ""),
                "ext": f.suffix.lower().lstrip(".") or "?",
                "size": _fmt_size(f.stat().st_size),
            })
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/suggest-folder", methods=["POST"])
def api_suggest_folder():
    """智能归位(A档)：给定文件，建议它归入【所属案件已有的真实文件夹】之一。
    只建议、不移动。前端拿到 target/confidence 后由用户一键确认（走 /api/pending/resolve）。"""
    p = _preview_safe_path((request.json or {}).get("path", ""))
    case_name, case_dir = _owning_case(p)
    if not case_dir:
        return jsonify({"ok": False, "error": "该文件不在任何已登记案件目录内"}), 400
    catalog = _case_folder_catalog(case_dir)
    if not catalog:
        return jsonify({"ok": False, "error": "该案件没有可归入的子文件夹"}), 400

    llm_cfg = _load_config().get("llm", {})
    if not llm_cfg.get("enabled"):
        return jsonify({"ok": False, "error": "LLM 未启用（config.yaml → llm.enabled）"}), 400
    try:
        from llm_reader import extract_text
        from llm_classify import llm_doctype, llm_pick_folder
    except Exception as e:
        return jsonify({"ok": False, "error": f"分类模块不可用：{e}"}), 500

    try:
        text, _ = extract_text(str(p))
    except Exception:
        text = ""
    case_cfg = _case_meta_map().get(case_name, {})
    dt = llm_doctype(text, p.name, llm_cfg, case_cfg) or {}
    doc_type = dt.get("doc_type", "")
    source = dt.get("source")
    pick = llm_pick_folder(doc_type, source, catalog, llm_cfg) if doc_type else None

    # 文件当前所在的相对子目录（用于提示"已在X / 建议移到Y"）
    try:
        cur = p.parent.resolve().relative_to(case_dir.resolve()).as_posix()
        cur = "" if cur == "." else cur
    except ValueError:
        cur = ""

    return jsonify({
        "ok": True,
        "case": case_name,
        "name": p.name,
        "doc_type": doc_type,
        "source": source,
        "target": (pick or {}).get("target"),
        "confidence": round((pick or {}).get("confidence", 0) or 0, 2),
        "current": cur,
        "catalog": catalog,
    })


@app.route("/api/pending/resolve", methods=["POST"])
def api_pending_resolve():
    """Manually classify a pending file: move to target path with custom filename."""
    data = request.json or {}
    src_path  = (data.get("path") or "").strip()
    case_name = (data.get("case") or "").strip()
    dest_sub  = (data.get("dest_subdir") or "").strip()
    new_name  = (data.get("name") or "").strip()

    if not src_path or not case_name or not dest_sub or not new_name:
        return jsonify({"ok": False, "error": "参数不完整"}), 400

    src = Path(src_path)
    if not src.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404

    case_dir = _case_dir(case_name)
    if not case_dir.exists():
        return jsonify({"ok": False, "error": "案件不存在"}), 404

    dest_dir = _resolve_case_dest(case_dir, dest_sub)   # core → 真实(带层级序号)目录
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / new_name
    if dest.exists():
        return jsonify({"ok": False, "error": f"目标文件已存在：{new_name}"}), 409

    try:
        shutil.move(str(src), str(dest))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Remove sidecar
    sidecar = src.parent / f".{src.name}.pending.json"
    if sidecar.exists():
        try:
            sidecar.unlink()
        except Exception:
            pass

    # Append to archive log
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": "resolved",
        "case": case_name,
        "original_name": src.name,
        "archived_name": new_name,
        "to": str(dest),
        "manual": True,
    }
    log_file = LOGS / "archive_log.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return jsonify({"ok": True, "dest": str(dest)})


@app.route("/api/daemon/start", methods=["POST"])
def api_daemon_start():
    try:
        result = subprocess.run(
            ["bash", str(WS_DIR / "bin/filer.sh"), "start"],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({"output": result.stdout, "success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daemon/stop", methods=["POST"])
def api_daemon_stop():
    try:
        result = subprocess.run(
            ["bash", str(WS_DIR / "bin/filer.sh"), "stop"],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({"output": result.stdout, "success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Loci 文件问答（本地 Qwen，SSE 流式）────────────────────────────────────
_OLLAMA_BASE = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_CHAT_URL   = _OLLAMA_BASE + "/api/chat"
_OLLAMA_CHAT_MODEL = os.environ.get("OLLAMA_PREFLIGHT_MODEL", "qwen2.5:7b")
_loci_text_cache: dict = {}  # abs_path → (text, mtime_ns)

# ── RAG ───────────────────────────────────────────────────────────────────────
_loci_rag = None
_loci_rag_lock = threading.Lock()

def _get_rag():
    global _loci_rag
    if _loci_rag is None:
        with _loci_rag_lock:
            if _loci_rag is None:
                try:
                    from rag import LociRAG
                    _loci_rag = LociRAG(str(WS_DIR / "vectors"))
                except Exception:
                    pass
    return _loci_rag

def _loci_extract(p: Path) -> str:
    key = str(p)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return ""
    cached = _loci_text_cache.get(key)
    if cached and cached[1] == mtime:
        return cached[0]
    try:
        from llm_reader import extract_text
        text, _ = extract_text(str(p))
    except Exception:
        text = ""
    _loci_text_cache[key] = (text, mtime)
    return text


_LOCI_EXTRACT_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".eml", ".csv"}

# ── 智能问答/索引专用的【整份】提取（llm_reader 那套是为归类首读设计的，硬砍到 5 页/5000 字，
#    用于问答会导致"第6页起的条款模型根本看不到"）。改用脱敏模块 to_markdown：全页 OCR、
#    不设字数上限、含 PDF 软换行重排。结果按 mtime 缓存，避免重复 OCR。
_loci_full_cache: dict = {}
_LOCI_EXTRACT_VER = "3"          # 提取逻辑版本；bump 它可让全部旧索引失效重建（3=切 PaddleOCR）
_loci_ocr_engine = None


def _get_loci_ocr():
    global _loci_ocr_engine
    if _loci_ocr_engine is None:
        try:
            _loci_ocr_engine = make_ocr()
        except Exception:
            _loci_ocr_engine = False
    return _loci_ocr_engine or None


def _loci_extract_full(p: Path) -> str:
    """整份提取：不砍页数/字数，扫描件逐页 OCR。失败退回 llm_reader（至少有前几页）。"""
    key = str(p)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return ""
    cached = _loci_full_cache.get(key)
    if cached and cached[1] == mtime:
        return cached[0]
    text = ""
    try:
        text = to_markdown(str(p), ocr_engine=_get_loci_ocr()) or ""
    except Exception:
        text = ""
    if len((text or "").strip()) < 50:       # 全文提取失败/过短 → 退回原提取器
        text = _loci_extract(p)
    _loci_full_cache[key] = (text, mtime)
    return text
_LOCI_SYSTEM_BASE = (
    "你是一个专为法律工作者设计的本地 AI 助理，运行在用户电脑上，"
    "所有数据完全本地处理，不会上传到任何外部服务。\n"
)
# 整文精读上限：仅【小文档】才喂全文（本身就快、且完整）；稍大就走【向量检索】——
# 换 bge-m3 后中文检索可靠，只喂相关条款(几千字)即可准确命中，每问 ~5-10s，远快于整文。
# 8000 拼接字符 ≈ 5-6 页；再大就检索。想更偏"整文精读(准但慢)"可调大此值。
_LOCI_FULL_BUDGET = 8000


@app.route("/api/loci-index", methods=["POST"])
def api_loci_index():
    """将文件/文件夹/上传文本切块嵌入，存入向量库。返回 collection_id 供后续检索。"""
    rag = _get_rag()
    if rag is None:
        return jsonify({"error": "RAG 不可用：chromadb 未安装或 nomic-embed-text 未就绪"}), 503

    data       = request.json or {}
    ctx_type   = data.get("type", "file")
    path_str   = data.get("path", "")
    extra_text = data.get("extra_text", "")
    extra_name = data.get("extra_name", "上传文件")

    try:
        if ctx_type == "upload":
            if not extra_text:
                return jsonify({"error": "no text"}), 400
            cid, n = rag.index_text(extra_text, source=extra_name)
            return jsonify({"collection_id": cid, "chunks": n, "cached": False})

        elif ctx_type == "file":
            p = _preview_safe_path(path_str)
            text = _loci_extract_full(p)          # 整份提取（全页 OCR，不砍 5 页/5000 字）
            if not text:
                return jsonify({"error": "无法提取文件内容"}), 400
            # mtime 掺入提取版本号：旧的 5000 字上限索引会因版本不符而被强制重建
            fresh_key = f"{p.stat().st_mtime_ns}-x{_LOCI_EXTRACT_VER}"
            cid, n, cached = rag.index_file(str(p), text, fresh_key)
            return jsonify({"collection_id": cid, "chunks": n, "cached": cached})

        elif ctx_type == "folder":
            folder = Path(path_str).resolve()
            if not _path_allowed(folder):
                abort(403)
            if not folder.is_dir():
                abort(404)
            files = sorted(
                f for f in folder.rglob("*")
                if f.is_file() and f.suffix.lower() in _LOCI_EXTRACT_EXTS
                and not f.name.startswith(".")
            )[:20]
            file_texts = {str(ff.relative_to(folder)): t
                          for ff in files if (t := _loci_extract(ff))}
            if not file_texts:
                return jsonify({"error": "文件夹中无可提取内容"}), 400
            mtime = max((f.stat().st_mtime_ns for f in files if f.exists()), default=0)
            cid, n, cached = rag.index_folder(path_str, file_texts, mtime)
            return jsonify({"collection_id": cid, "chunks": n, "cached": cached})

        return jsonify({"error": "unknown type"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/loci-upload", methods=["POST"])
def api_loci_upload():
    """接收上传文件，提取文本供 Loci 对话使用（文件内容不落盘于案件目录）。"""
    import tempfile, os as _os
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    orig_name = f.filename or "upload"
    suffix = Path(orig_name).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name
    try:
        from llm_reader import extract_text
        text, _ = extract_text(tmp_path)
    except Exception:
        text = ""
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass
    if not text.strip():
        return jsonify({"error": "无法提取文件内容，请确认格式是否受支持（支持 PDF / Word / TXT / MD / Excel / EML）"}), 400
    return jsonify({"text": text[:8000], "name": orig_name})


@app.route("/api/loci-chat", methods=["POST"])
def api_loci_chat():
    """与 Loci(Qwen) 多轮对话，SSE 流式返回。
    上下文三选一：path（单文件）/ folder（目录批量）/ extra_text（上传文件文本）。
    三者均不传则进入通用问答模式。
    """
    data          = request.json or {}
    path_str      = data.get("path", "")
    folder_str    = data.get("folder", "")
    extra_text    = data.get("extra_text", "")
    extra_name    = data.get("extra_name", "")
    collection_id = data.get("collection_id", "")
    ctx_label     = data.get("ctx_label", "材料")
    messages      = data.get("messages", [])

    if collection_id:
        rag = _get_rag()
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # 整文精读优先：文档不超窗口就把全文喂进去（几十页合同的"总结/逐条梳理"才完整、不漏条款）；
        # 只有超长文档才退回向量检索取最相关片段。全文取自已嵌入的切块，避免重复 OCR/解析。
        all_chunks = rag.get_all(collection_id) if rag else []
        full = "\n".join(all_chunks)
        if all_chunks and len(full) <= _LOCI_FULL_BUDGET:
            system_content = (
                _LOCI_SYSTEM_BASE
                + f"以下是《{ctx_label}》的全文：\n\n{full}\n\n"
                + "以上是完整全文，请通读后准确、完整地回答。"
                + "若用户询问某个条款/约定（如违约责任、付款、保密、争议解决等），"
                + "请在全文中逐条查找并【原文引用】对应条款（含条号/标题），不要改写、不要说找不到——"
                + "除非全文确实没有；若有多处相关，一并列出。"
                + "若要求总结，须覆盖全文要点、逐条列明关键条款、金额、日期、当事人与权利义务，"
                + "不要遗漏、不要泛泛而谈、不要编造。"
            )
        else:
            chunks = rag.retrieve(collection_id, question, n=16) if rag else []
            if chunks:
                context = "\n\n---\n\n".join(chunks)
                long_note = (
                    "\n\n（注：该文档较长，以上为按你当前问题语义检索出的最相关片段、并非全文。"
                    "若你想要整体概览或全文总结，检索结果可能不完整——请就具体问题分别提问，"
                    "或告诉我需要重点看哪一部分。）" if all_chunks else ""
                )
                system_content = (
                    _LOCI_SYSTEM_BASE
                    + f"以下是与用户问题最相关的文件片段（来自「{ctx_label}」，已通过语义检索筛选，共 {len(chunks)} 段）：\n\n"
                    + context + long_note
                    + "\n\n请基于以上内容准确回答，涉及具体条款/数字时逐一列明。如内容不足以回答，请如实告知，不要编造。"
                )
            else:
                system_content = _LOCI_SYSTEM_BASE + "你可以帮助用户分析法律文件、回答法律问题，或协助起草法律文书。"

    elif path_str:
        p = _preview_safe_path(path_str)
        text = _loci_extract_full(p)          # 整份提取（全页 OCR，不砍 5 页/5000 字）
        if not text:
            return jsonify({"error": "无法提取文件内容，请确认格式是否受支持"}), 400
        system_content = (
            _LOCI_SYSTEM_BASE
            + "你正在帮助用户分析以下文件：\n\n"
            + f"文件名：{p.name}\n文件内容：\n{text[:20000]}\n\n"
            + "请基于文件全文、准确专业地回答。总结时抓住关键事实、金额、日期、主体与法律关系，"
            + "不要泛泛而谈；涉及具体条款/数字时逐一列明。如内容不足以回答，请如实告知，不要编造。"
        )
    elif folder_str:
        folder = Path(folder_str).resolve()
        if not _path_allowed(folder):
            abort(403)
        if not folder.is_dir():
            abort(404)
        all_files = sorted(
            f for f in folder.rglob("*")
            if f.is_file()
            and f.suffix.lower() in _LOCI_EXTRACT_EXTS
            and not f.name.startswith(".")
        )[:20]
        parts, total = [], 0
        for ff in all_files:
            t = _loci_extract(ff)
            if not t:
                continue
            rel = ff.relative_to(folder)
            excerpt = t[:3000]
            parts.append(f"--- {rel} ---\n{excerpt}")
            total += len(excerpt)
            if total > 13000:
                break
        if not parts:
            return jsonify({"error": "文件夹中无可提取内容"}), 400
        system_content = (
            _LOCI_SYSTEM_BASE
            + f"你正在帮助用户分析文件夹「{folder.name}」中的材料（共 {len(parts)} 份）：\n\n"
            + "\n\n".join(parts)
            + "\n\n请以简洁、专业的语气回答用户关于这些材料的问题。如内容不足以回答，请如实告知。"
        )
    elif extra_text:
        system_content = (
            _LOCI_SYSTEM_BASE
            + f"你正在帮助用户分析以下材料（文件名：{extra_name or '上传文件'}）：\n\n"
            + extra_text
            + "\n\n请以简洁、专业的语气回答用户关于这份材料的问题。如内容不足以回答，请如实告知。"
        )
    else:
        system_content = (
            _LOCI_SYSTEM_BASE
            + "你可以帮助用户分析法律文件、回答法律问题，或协助起草法律文书。"
        )
    full_messages = [{"role": "system", "content": system_content}] + messages

    payload = json.dumps({
        "model": _OLLAMA_CHAT_MODEL,
        "messages": full_messages,
        "stream": True,
        # num_ctx=16384：检索模式下上下文只有几千字（相关条款），16k 绰绰有余且 KV cache 更省、
        # 首字更快。仍能容纳小文档整文精读(≤8000字)。（默认 2048 会截断内容→别用。）
        "options": {"temperature": 0.3, "num_predict": 1536, "num_ctx": 16384},
    }).encode()

    def generate():
        try:
            req = _urllib_req.Request(
                _OLLAMA_CHAT_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with _urllib_req.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("message", {}).get("content", "")
                        done  = chunk.get("done", False)
                        yield f"data: {json.dumps({'t': token, 'done': done})}\n\n"
                        if done:
                            break
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 脱敏引擎（复用 loci/desensitize/，自包含）────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "loci"))
try:
    from desensitize import (
        Registry, QwenNER, make_ocr, health_check, canon,
        desensitize_tree, desensitize_files, desensitize_md_file,
        to_markdown, analyze_text, apply_review, vault_save, vault_load,
        render as _dsz_render, restore as _dsz_restore, build_restore_map,
    )
    _DESENS_OK, _DESENS_ERR = True, ""
except Exception as _de:
    _DESENS_OK, _DESENS_ERR = False, str(_de)

_DESENS_CASE_JOB: dict = {
    "running": False, "done": False, "case": "", "i": 0, "total": 0,
    "current": "", "report": None, "error": None, "dst": "",
}
_DESENS_FILE_JOB: dict = {
    "running": False, "done": False, "output": "", "name": "", "error": None,
}
_DESENS_PACK_JOB: dict = {
    "running": False, "done": False, "i": 0, "total": 0, "current": "",
    "report": None, "error": None, "dst": "",
    "cancel": False, "items": [], "failed": [],
}
# 重试上下文（含 Registry/NER 等不可 JSON 序列化对象，单独存，不进 status 响应）
_PACK_CTX: dict = {}
_DESENS_SUPPORTED = {".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}


# 脱敏范围可选类别（归一类别）。前端勾选哪些就脱敏哪些；缺省=全部。
_DESENS_ALL_CATS = {"PERSON", "COMPANY", "ADDRESS", "PHONE", "EMAIL", "WECHAT",
                    "ID_CARD", "USCC", "BANKCARD", "SECURITIES", "PLATE",
                    "SSN", "EIN", "MONEY", "CASE_NO"}


def _desens_out_parent(out_dir: str) -> Path:
    """脱敏包的保存父目录：用户在脱敏工具里选了路径就用它，否则默认 cases_脱敏/。"""
    out_dir = (out_dir or "").strip()
    if out_dir:
        try:
            p = Path(out_dir).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            _EXTRA_ALLOWED_ROOTS.add(str(p.resolve()))   # 允许之后预览/在 Finder 显示
            return p
        except Exception:
            pass
    return CASES.parent / "cases_脱敏"


def _parse_cats(data):
    """请求里的 cats（要脱敏的类别列表）→ set；缺省/空 → None=全部脱敏。"""
    c = data.get("cats")
    if not c:
        return None
    cats = {x for x in c if x in _DESENS_ALL_CATS}
    return cats or None


def _run_desensitize_case(src: Path, dst: Path, lang: str, cats=None) -> None:
    g = _DESENS_CASE_JOB
    try:
        reg = Registry(lang=lang)
        ner = QwenNER(model="qwen2.5:7b", lang=lang)
        ocr = make_ocr()

        def prog(i, total, rel, _res):
            g.update(i=i, total=total, current=str(rel))

        g["report"] = desensitize_tree(
            str(src), str(dst), reg=reg, ner=ner, ocr_engine=ocr, lang=lang,
            progress=prog, cats=cats, allow=_load_allow(),
        )
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"] = False
        g["done"]    = True


def _run_desensitize_file(src: Path, dst: Path, lang: str, cats=None) -> None:
    g = _DESENS_FILE_JOB
    try:
        reg = Registry(lang=lang)
        ner = QwenNER(model="qwen2.5:7b", lang=lang)
        desensitize_md_file(str(src), str(dst), reg=reg, ner=ner, cats=cats, allow=_load_allow())
        g["output"] = str(dst)
        g["name"]   = dst.name
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"] = False
        g["done"]    = True


@app.route("/api/case/<path:case_name>/desensitize", methods=["POST"])
def api_case_desensitize(case_name):
    """整案脱敏：后台线程，镜像生成 cases_脱敏/{case_name}/ 树。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    if _DESENS_CASE_JOB["running"]:
        return jsonify({"ok": False, "error": "已有整案脱敏任务运行中，请等待完成"})
    data = request.json or {}
    lang = data.get("lang", "zh")
    cats = _parse_cats(data)
    src = _case_dir(case_name)
    if not src.exists():
        return jsonify({"ok": False, "error": "案件文件夹不存在"})
    dst = CASES.parent / "cases_脱敏" / case_name
    _DESENS_CASE_JOB.update(
        running=True, done=False, case=case_name,
        i=0, total=0, current="", report=None, error=None, dst=str(dst),
    )
    threading.Thread(target=_run_desensitize_case, args=(src, dst, lang, cats), daemon=True).start()
    return jsonify({"ok": True, "dst": str(dst)})


@app.route("/api/desensitize-file", methods=["POST"])
def api_desensitize_file():
    """单文件脱敏：后台线程，先转 Markdown 再脱敏，输出 {stem}_脱敏.md 至同目录。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    if _DESENS_FILE_JOB["running"]:
        return jsonify({"ok": False, "error": "已有文件脱敏任务运行中"})
    data = request.json or {}
    lang = data.get("lang", "zh")
    cats = _parse_cats(data)
    p = _preview_safe_path(data.get("path", ""))
    if p.suffix.lower() not in _DESENS_SUPPORTED:
        return jsonify({"ok": False, "error": f"不支持格式 {p.suffix}，支持 docx/doc/pdf/图片"}), 400
    dst = p.parent / f"{p.stem}_脱敏.md"
    _DESENS_FILE_JOB.update(running=True, done=False, output="", name="", error=None)
    threading.Thread(target=_run_desensitize_file, args=(p, dst, lang, cats), daemon=True).start()
    return jsonify({"ok": True})


def _pack_progress(i, total, name, res):
    g = _DESENS_PACK_JOB
    g.update(i=i, total=total, current=str(name))
    status = "skip" if res == "skip" else ("error" if res == "error" else "ok")
    item = {"name": str(name), "status": status}
    if isinstance(res, int):
        item["hits"] = res
    g["items"].append(item)


def _run_desensitize_pack(files: list, dst: Path, lang: str, vault_password: str, cats=None) -> None:
    g = _DESENS_PACK_JOB
    try:
        reg = Registry(lang=lang)
        ner = QwenNER(model="qwen2.5:7b", lang=lang)
        ocr = make_ocr()
        used_names: set = set()
        # 存重试上下文（同一 reg/used_names → 重试失败件时化名仍一致）
        _PACK_CTX.update(reg=reg, ner=ner, ocr=ocr, lang=lang, cats=cats,
                         pw=vault_password, dst=str(dst), used_names=used_names,
                         all_files=list(files))

        rep = desensitize_files(
            files, str(dst), reg=reg, ner=ner, ocr_engine=ocr,
            lang=lang, vault_password=vault_password or None, progress=_pack_progress,
            cats=cats, should_cancel=lambda: g["cancel"], used_names=used_names,
            allow=_load_allow(),
        )
        g["report"] = rep
        _record_pack_failed(rep)
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"] = False
        g["done"]    = True


def _record_pack_failed(rep: dict) -> None:
    """从报告里把失败文件名映射回完整路径，存进 ctx 供重试。"""
    g = _DESENS_PACK_JOB
    err_names = {e.get("file") for e in (rep.get("errors") or [])}
    g["failed"] = sorted(err_names)
    allf = _PACK_CTX.get("all_files") or []
    _PACK_CTX["failed_paths"] = [p for p in allf if Path(p).name in err_names and Path(p).exists()]


def _run_desensitize_retry() -> None:
    """只重跑失败的文件，复用同一 Registry/used_names，结果并入既有报告与 vault。"""
    g = _DESENS_PACK_JOB
    ctx = _PACK_CTX
    try:
        failed = ctx.get("failed_paths") or []
        rep2 = desensitize_files(
            failed, ctx["dst"], reg=ctx["reg"], ner=ctx["ner"], ocr_engine=ctx["ocr"],
            lang=ctx["lang"], vault_password=(ctx.get("pw") or None),
            progress=_pack_progress, cats=ctx.get("cats"),
            should_cancel=lambda: g["cancel"], used_names=ctx["used_names"],
            allow=_load_allow(),
        )
        # 并入既有报告
        rep = g.get("report") or {}
        rep["processed"] = rep.get("processed", 0) + rep2.get("processed", 0)
        rep["hits"] = rep.get("hits", 0) + rep2.get("hits", 0)
        rep["outputs"] = (rep.get("outputs") or []) + rep2.get("outputs", [])
        rep["errors"] = rep2.get("errors", [])          # 仅保留本轮仍失败的
        rep["entities"] = rep2.get("entities", rep.get("entities", 0))
        g["report"] = rep
        _record_pack_failed(rep)
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"] = False
        g["done"]    = True


@app.route("/api/desensitize-pack", methods=["POST"])
def api_desensitize_pack():
    """勾选多文件 → 扁平脱敏包：后台线程，共用一套化名，输出至 cases_脱敏/_包_{时间戳}/。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    if _DESENS_PACK_JOB["running"]:
        return jsonify({"ok": False, "error": "已有脱敏打包任务运行中，请等待完成"})
    data = request.json or {}
    lang = data.get("lang", "zh")
    cats = _parse_cats(data)
    vault_password = (data.get("vault_password") or "").strip()
    raw_files = data.get("files") or []
    if not raw_files:
        return jsonify({"ok": False, "error": "未选择任何文件"}), 400

    files, bad = [], []
    for f in raw_files:
        try:
            files.append(_preview_safe_path(f))
        except Exception:
            bad.append(f)
    if bad:
        return jsonify({"ok": False, "error": f"{len(bad)} 个文件路径越界或不存在"}), 403

    stamp = datetime.now().strftime("%y%m%d_%H%M%S")
    dst = _desens_out_parent(data.get("out_dir")) / f"_包_{stamp}"
    _DESENS_PACK_JOB.update(
        running=True, done=False, i=0, total=len(files),
        current="", report=None, error=None, dst=str(dst),
        cancel=False, items=[], failed=[],
    )
    threading.Thread(
        target=_run_desensitize_pack,
        args=([str(p) for p in files], dst, lang, vault_password, cats),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "dst": str(dst), "count": len(files)})


@app.route("/api/desensitize-pack/cancel", methods=["POST"])
def api_desensitize_pack_cancel():
    """请求取消当前脱敏打包：置 cancel 标志，循环会在下一个文件前停止，已处理结果保留。"""
    if not _DESENS_PACK_JOB["running"]:
        return jsonify({"ok": False, "error": "当前没有运行中的任务"})
    _DESENS_PACK_JOB["cancel"] = True
    return jsonify({"ok": True})


@app.route("/api/desensitize-pack/retry", methods=["POST"])
def api_desensitize_pack_retry():
    """只重跑上一次打包里失败的文件（复用同一套化名）。"""
    if _DESENS_PACK_JOB["running"]:
        return jsonify({"ok": False, "error": "已有任务运行中"})
    if not _PACK_CTX.get("failed_paths"):
        return jsonify({"ok": False, "error": "没有可重试的失败文件（或原文件已不存在）"})
    n = len(_PACK_CTX["failed_paths"])
    _DESENS_PACK_JOB.update(running=True, done=False, cancel=False,
                            i=0, total=n, current="", error=None, items=[])
    threading.Thread(target=_run_desensitize_retry, daemon=True).start()
    return jsonify({"ok": True, "count": n})


def _run_desensitize_upload(files: list, dst: Path, lang: str,
                            vault_password: str, stage: Path, cats=None) -> None:
    """跑完上传脱敏后清掉暂存的原始件（敏感原件不留在工作区）。"""
    try:
        _run_desensitize_pack(files, dst, lang, vault_password, cats)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


@app.route("/api/desensitize-upload", methods=["POST"])
def api_desensitize_upload():
    """上传任意本机文件直接脱敏（不入案件库）：暂存→脱敏打包→删暂存原件。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    if _DESENS_PACK_JOB["running"]:
        return jsonify({"ok": False, "error": "已有脱敏打包任务运行中，请等待完成"})
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "未上传任何文件"}), 400
    lang = request.form.get("lang", "zh")
    vault_password = (request.form.get("vault_password") or "").strip()
    cats_raw = request.form.get("cats", "")
    cats = _parse_cats({"cats": [c for c in cats_raw.split(",") if c]})

    stamp = datetime.now().strftime("%y%m%d_%H%M%S")
    stage = WS_DIR / "_脱敏上传" / stamp
    stage.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    for f in files:
        if not f.filename:
            continue
        name = Path(f.filename).name          # 防目录穿越
        if Path(name).suffix.lower() not in _DESENS_SUPPORTED:
            rejected.append(name)
            continue
        fp = stage / name
        f.save(str(fp))
        saved.append(str(fp))
    if not saved:
        shutil.rmtree(stage, ignore_errors=True)
        return jsonify({"ok": False, "error": "无受支持格式文件（支持 docx/doc/pdf/图片）"}), 400

    dst = _desens_out_parent(request.form.get("out_dir")) / f"_上传包_{stamp}"
    _DESENS_PACK_JOB.update(
        running=True, done=False, i=0, total=len(saved),
        current="", report=None, error=None, dst=str(dst),
        cancel=False, items=[], failed=[],
    )
    threading.Thread(
        target=_run_desensitize_upload,
        args=(saved, dst, lang, vault_password, stage, cats),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "dst": str(dst), "count": len(saved), "rejected": rejected})


@app.route("/api/desensitize/status")
def api_desensitize_status():
    return jsonify({"case": _DESENS_CASE_JOB, "file": _DESENS_FILE_JOB,
                    "pack": _DESENS_PACK_JOB,
                    "engine_ok": _DESENS_OK, "engine_err": _DESENS_ERR})


# ── 复核/纠错闸门：单文件"先识别→人工增删改→预览即所得→导出" ───────────────
@app.route("/api/desensitize-analyze", methods=["POST"])
def api_desensitize_analyze():
    """复核第一步：提取文本 + 检测候选实体（只识别不提交），供前端勾选/补录。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    p = _preview_safe_path(data.get("path", ""))
    lang = data.get("lang", "zh")
    cats = _parse_cats(data)
    try:
        if p.suffix.lower() in (".txt", ".md"):      # 纯文本无需提取，直接读
            text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            text = to_markdown(str(p), ocr_engine=make_ocr())
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取失败：{e}"}), 500
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "无法从该文件提取文本"}), 400
    ner = None
    try:
        if health_check():
            ner = QwenNER(model="qwen2.5:7b", lang=lang)
    except Exception:
        ner = None
    spans = analyze_text(text, Registry(lang=lang), ner=ner, lang=lang, allow=_load_allow())
    if cats is not None:
        spans = [s for s in spans if canon(s["label"]) in cats]
    return jsonify({"ok": True, "text": text, "spans": spans, "ner": ner is not None})


@app.route("/api/desensitize-apply", methods=["POST"])
def api_desensitize_apply():
    """复核第二步：按用户最终确认的 span 清单脱敏。dry_run=预览不落盘；否则写 {stem}_脱敏.md + vault。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    text = data.get("text", "")
    spans = data.get("spans") or []
    lang = data.get("lang", "zh")
    mode = data.get("mode", "alias")          # alias=化名可逆 | blackout=黑框 | asterisk=星号
    if mode not in ("alias", "blackout", "asterisk"):
        mode = "alias"
    if not text:
        return jsonify({"ok": False, "error": "缺少文本"}), 400
    reg = Registry(lang=lang)
    masked, applied = apply_review(text, spans, reg, mode=mode)
    if data.get("dry_run"):
        return jsonify({"ok": True, "masked": masked, "count": len(applied), "mode": mode})
    p = _preview_safe_path(data.get("path", ""))
    pw = (data.get("vault_password") or "").strip()
    dst = p.parent / f"{p.stem}_脱敏.md"
    dst.write_text(masked, encoding="utf-8")
    # 仅化名模式可逆，才生成映射表金库；黑框/星号不可逆，无映射可存
    if pw and mode == "alias":
        vault_save(str(p.parent / f"{p.stem}_脱敏映射表.vault"), reg.export(), password=pw)
    return jsonify({"ok": True, "output": str(dst), "count": len(applied), "mode": mode})


# ══════════════════════════════════════════════════════════════
# 脱敏工作流会话：一份 Registry 贯穿 上传→自动→改对照表/加别名→重脱敏→手动→还原→导出
# 全程 alias 可逆（对照表+还原的基础）；导出时可另选涂黑/星号（不可逆，对外交付）。
# ══════════════════════════════════════════════════════════════
import uuid as _uuid
import time as _time
_DESENS_WF: dict = {}          # sid -> {reg,text,name,lang,manual,detected,masked,cats,ts}
_WF_MAX_SESS = 12              # 最多保留的脱敏会话数（LRU）
_WF_TTL_SEC = 30 * 60         # 会话最长空闲时间（秒）
_wf_ner_engine = None


def _wf_touch(sess):
    """标记会话最近使用时间。"""
    if sess is not None:
        sess["ts"] = _time.time()
    return sess


def _wf_prune():
    """清理超时/超量的旧脱敏会话，防止 _DESENS_WF 只增不减占内存。"""
    now = _time.time()
    # 1) 超过 TTL 未用的直接丢弃
    for sid in [s for s, v in _DESENS_WF.items()
                if now - v.get("ts", now) > _WF_TTL_SEC]:
        _DESENS_WF.pop(sid, None)
    # 2) 仍超量则按最近使用时间淘汰最旧的
    if len(_DESENS_WF) > _WF_MAX_SESS:
        for sid, _ in sorted(_DESENS_WF.items(),
                             key=lambda kv: kv[1].get("ts", 0))[:len(_DESENS_WF) - _WF_MAX_SESS]:
            _DESENS_WF.pop(sid, None)
_WF_IMG_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def _get_wf_ner():
    global _wf_ner_engine
    if _wf_ner_engine is None:
        try:
            _wf_ner_engine = QwenNER(model="qwen2.5:7b", lang="zh") if health_check() else False
        except Exception:
            _wf_ner_engine = False
    return _wf_ner_engine or None


def _wf_norm(s):
    return (s or "").strip().replace("（", "(").replace("）", ")").replace(" ", "").replace("　", "")


def _wf_extract(p):
    ext = p.suffix.lower()
    if ext in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="ignore")
    ocr = _get_loci_ocr() if ext in _WF_IMG_EXT else None
    return to_markdown(str(p), ocr_engine=ocr) or ""


def _wf_mapping(reg):
    """对照表：每个实体 = 化名 / 原名 / 已登记别名(原样surfaces) / 类别。"""
    rows = []
    for es in reg.by_cat.values():
        for e in es:
            rows.append({"alias": e.alias, "canonical": e.canonical, "category": e.category,
                         "aliases": sorted(x for x in e.raw), "ambiguous": e.ambiguous})
    rows.sort(key=lambda r: r["alias"])
    return rows


def _wf_detect(sess):
    """检测 span：首轮带 NER 并缓存；之后只补 regex+已知别名（含手动加的别名），快。返回 dict spans。"""
    text, reg = sess["text"], sess["reg"]
    if sess.get("detected") is None:
        ner = _get_wf_ner()
        try:
            det = analyze_text(text, reg, ner=ner, lang=sess["lang"], allow=_load_allow())
        except Exception:
            det = analyze_text(text, reg, ner=None, lang=sess["lang"], allow=_load_allow())
        sess["detected"] = det
    else:
        try:
            more = analyze_text(text, reg, ner=None, lang=sess["lang"], allow=_load_allow())
        except Exception:
            more = []
        seen = {(s["start"], s["end"]) for s in sess["detected"]}
        for s in more:
            if (s["start"], s["end"]) not in seen:
                sess["detected"].append(s)
    det = sess["detected"]
    if sess.get("cats") is not None:
        det = [s for s in det if canon(s["label"]) in sess["cats"]]
    result = det + sess.get("manual", [])
    rm = sess.get("removed")            # 用户判为误识别的表述：整会话不再脱敏
    if rm:
        result = [s for s in result if _wf_norm(s["text"]) not in rm]
    return result


def _wf_render(sess):
    """alias 模式渲染并提交进 reg，更新 sess['masked']。

    两遍：第一遍登记实体（含「以下简称X」声明的简称）；第二遍用已登记的全部表述
    回扫全文，补上 NER 漏掉的简称/别名的其余出现（否则声明处脱敏、正文里却漏标）。
    """
    text, reg = sess["text"], sess["reg"]
    _wf_seed(sess)                                       # 先登记案件当事人为已知主体
    spans = _wf_detect(sess)
    apply_review(text, spans, reg, mode="alias")        # 第一遍：入册
    rm = sess.get("removed")
    extra = [{"start": s.start, "end": s.end, "text": s.text, "label": s.label}
             for s in reg.surface_spans(text)]
    if rm:
        extra = [s for s in extra if _wf_norm(s["text"]) not in rm]
    masked, _ = apply_review(text, spans + extra, reg, mode="alias")
    sess["masked"] = masked
    return masked


def _wf_state(sess):
    return {"masked": sess.get("masked", ""), "text": sess["text"],
            "name": sess.get("name", ""), "mapping": _wf_mapping(sess["reg"])}


# ── 打通「案件当事人信息」→ 脱敏（升级项 C）─────────────────────────────────
_CO_SUFFIX_RE = re.compile(
    r'(公司|有限|集团|厂|店|中心|事务所|银行|合作社|研究院|学院|大学|医院|协会|'
    r'基金会|控股|企业|工厂|商行|超市|酒店|(?:Inc|LLC|Ltd|Corp|GmbH|Co\.?))', re.I)
_IDCARD_RE = re.compile(r'^\d{17}[\dXx]$')
_USCC_RE   = re.compile(r'^[0-9A-HJ-NP-RTUWXY]{18}$')   # 统一社会信用代码


def _party_label(name: str) -> str:
    return "COMPANY" if _CO_SUFFIX_RE.search(name or "") else "PERSON"


def _id_label(rid: str) -> str:
    rid = (rid or "").strip()
    if _IDCARD_RE.match(rid):
        return "ID_CARD"
    if _USCC_RE.match(rid) and any(c.isalpha() for c in rid):
        return "USCC"
    return ""


def _build_case_seed(meta: dict) -> dict:
    """从案件登记信息构造脱敏种子：每方当事人=一组同化名的表述(全称+别名+曾用名)，
    ID(身份证/统一码)按类型单独脱敏。有 detail 用 detail(带分组)，否则退回平铺名单。"""
    parties, ids = [], []
    used_detail = False
    for dkey, fkey in (("party_us_detail", "party_us"), ("party_op_detail", "party_op")):
        det = meta.get(dkey)
        if isinstance(det, list) and det:
            used_detail = True
            for r in det:
                nm = (r.get("name") or "").strip()
                surf = [nm] + list(r.get("aliases", []) or []) + list(r.get("former", []) or [])
                surf = [s.strip() for s in surf if s and s.strip()]
                if surf:
                    parties.append({"label": _party_label(nm or surf[0]), "surfaces": surf})
                rid = (r.get("id") or "").strip()
                lab = _id_label(rid)
                if rid and lab:
                    ids.append({"label": lab, "value": rid})
    if not used_detail:      # 无 detail：平铺名单里每个名字各自成组（无法分组，但保证脱敏）
        for fkey in ("party_us", "party_op"):
            for nm in (meta.get(fkey) or []):
                nm = (nm or "").strip()
                if nm:
                    parties.append({"label": _party_label(nm), "surfaces": [nm]})
    return {"parties": parties, "ids": ids}


def _wf_seed(sess):
    """把案件当事人种子登记进 reg（保证当事人=主体、同一方共用化名）。
    幂等：reg.resolve 按表述去重复用，重复调用不会重复建实体。受脱敏范围 cats 约束。"""
    seed = sess.get("seed")
    if not seed:
        return
    reg = sess["reg"]
    cats = sess.get("cats")            # None=全部；否则集合
    ok = lambda lab: (cats is None) or (lab in cats)
    for g in seed.get("parties", []):
        surf = [s for s in g.get("surfaces", []) if s]
        if not surf or not ok(g["label"]):
            continue
        e = reg.resolve(g["label"], surf[0])     # 主表述建/取实体
        for s in surf[1:]:
            reg._absorb(e, s)                     # 其余表述并入同一化名
    for i in seed.get("ids", []):
        if i.get("value") and ok(i["label"]):
            reg.resolve(i["label"], i["value"])


def _seed_from_request(d, path_hint=""):
    """按请求里的 case_key / case_name，或文件所在案件目录，取该案的当事人种子。"""
    meta = None
    key = (d.get("case_key") or "").strip()
    nm  = (d.get("case_name") or "").strip()
    cases = _load_config().get("cases", [])
    if nm:
        meta = next((c for c in cases if c.get("name") == nm), None)
    if meta is None and key:
        meta = next((c for c in cases if c.get("key") == key), None)
    if meta is None and path_hint:      # 从文件路径反查所属案件
        try:
            pp = Path(path_hint).resolve()
            for c in cases:
                cd = _case_dir(c.get("name", "")).resolve()
                if _is_relative_to(pp, cd):
                    meta = c; break
        except Exception:
            pass
    return _build_case_seed(meta) if meta else None


@app.route("/api/desens-wf/start", methods=["POST"])
def api_wf_start():
    """开一个脱敏会话：上传文件 / 案件-收件箱文件(path) / 直接文本，提取文本。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR}), 503
    lang, name, text = "zh", "", ""
    seed_arg, path_hint = {}, ""
    f = request.files.get("file")
    if f:
        name = f.filename or "上传文件"
        lang = request.form.get("lang", "zh")
        seed_arg = {"case_key": request.form.get("case_key", ""),
                    "case_name": request.form.get("case_name", "")}
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(name).suffix) as tmp:
            f.save(tmp.name); tp = Path(tmp.name)
        try:
            text = _wf_extract(tp)
        finally:
            tp.unlink(missing_ok=True)
    else:
        d = request.json or {}
        lang = d.get("lang", "zh")
        seed_arg = d
        if d.get("text") is not None:
            text, name = d["text"], d.get("name", "粘贴文本")
        elif d.get("path"):
            p = _preview_safe_path(d["path"]); name = p.name; path_hint = str(p)
            text = _wf_extract(p)
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "未能从该文件提取到文本"}), 400
    _wf_prune()                # 开新会话前先清理超时/超量的旧会话
    seed = _seed_from_request(seed_arg, path_hint)   # 案件当事人→脱敏种子（升级项 C）
    sid = _uuid.uuid4().hex[:12]
    _DESENS_WF[sid] = {"reg": Registry(lang=lang), "text": text, "name": name,
                       "lang": lang, "manual": [], "detected": None, "masked": "", "cats": None,
                       "ts": _time.time(), "seed": seed}
    n_seed = len(seed["parties"]) + len(seed["ids"]) if seed else 0
    return jsonify({"ok": True, "sid": sid, "text": text, "name": name, "seeded": n_seed})


@app.route("/api/desens-wf/auto", methods=["POST"])
def api_wf_auto():
    """自动脱敏（按脱敏范围 cats）——重置会话、全新识别 + alias 脱敏。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效，请重新上传"}), 404
    c = d.get("cats")
    sess["cats"] = ({x for x in c if x in _DESENS_ALL_CATS} or None) if c else None
    sess["reg"] = Registry(lang=sess["lang"]); sess["detected"] = None
    sess["manual"] = []; sess["removed"] = set()
    _wf_render(sess)
    return jsonify({"ok": True, **_wf_state(sess)})


@app.route("/api/desens-wf/remove", methods=["POST"])
def api_wf_remove():
    """删除一个误识别项：把它的全部表述加入本会话忽略集、从注册表移除，再重脱敏。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    reg = sess["reg"]
    rm = sess.setdefault("removed", set())
    alias = d.get("alias", "")
    e = reg._find(alias)
    if e:
        for s in list(e.raw) + list(e.surfaces) + [e.canonical]:
            if s:
                rm.add(_wf_norm(s))
        for cat in list(reg.by_cat):        # 从注册表摘除该实体
            reg.by_cat[cat] = [x for x in reg.by_cat[cat] if x.alias != alias]
    if d.get("text"):                       # 兜底：按文本忽略
        rm.add(_wf_norm(d["text"]))
    sess["manual"] = [m for m in sess.get("manual", []) if _wf_norm(m["text"]) not in rm]
    _wf_render(sess)
    return jsonify({"ok": True, **_wf_state(sess)})


@app.route("/api/desens-wf/add-alias", methods=["POST"])
def api_wf_add_alias():
    """给某脱敏项(化名)增加别名/规则（如 公司A 追加"纽约川普""川普公司"），并立即重脱敏。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    e = sess["reg"]._find(d.get("alias", ""))
    if not e:
        return jsonify({"ok": False, "error": "未找到该脱敏项"}), 404
    for s in (d.get("surfaces") or []):
        s = (s or "").strip()
        if s:
            e.raw.add(s); e.surfaces.add(_wf_norm(s))
    # 原名(canonical)=还原时替回的真名：可显式指定，否则取最长的登记别名（通常即全称）
    cn = (d.get("canonical") or "").strip()
    e.canonical = cn if cn else max([e.canonical] + list(e.raw), key=len)
    _wf_render(sess)
    return jsonify({"ok": True, **_wf_state(sess)})


@app.route("/api/desens-wf/remask", methods=["POST"])
def api_wf_remask():
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    _wf_render(sess)
    return jsonify({"ok": True, **_wf_state(sess)})


@app.route("/api/desens-wf/add-manual", methods=["POST"])
def api_wf_add_manual():
    """在预览里手动补一处脱敏：选区 + 类别；可归到已有化名(alias)沿用同一化名。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    try:
        a, b = int(d["start"]), int(d["end"])
    except Exception:
        return jsonify({"ok": False, "error": "无效选区"}), 400
    frag = sess["text"][a:b]
    if not frag.strip():
        return jsonify({"ok": False, "error": "选区为空"}), 400
    label = d.get("label", "OTHER")
    alias = d.get("alias")
    if alias:
        e = sess["reg"]._find(alias)
        if e:
            e.raw.add(frag); e.surfaces.add(_wf_norm(frag)); label = e.category
    # 一处划选 = 全文同一表述都补脱敏（与自动脱敏/化名一致性保持一致）；
    # 单字片段有误伤风险，只脱选中那一处。
    manual = sess.setdefault("manual", [])
    existing = {(m["start"], m["end"]) for m in manual}
    if len(frag) >= 2:
        spots = [(mm.start(), mm.end()) for mm in re.finditer(re.escape(frag), sess["text"])]
    else:
        spots = [(a, b)]
    for s0, e0 in spots:
        if (s0, e0) not in existing:
            manual.append({"start": s0, "end": e0, "text": frag, "label": label})
    _wf_render(sess)
    return jsonify({"ok": True, **_wf_state(sess)})


@app.route("/api/desens-wf/restore", methods=["POST"])
def api_wf_restore():
    """基于对照表把脱敏文本还原。返回 脱敏文本 与 还原文本 供上下对照。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    rmap = build_restore_map(sess["reg"])
    masked = d.get("masked") or sess.get("masked", "")
    return jsonify({"ok": True, "masked": masked, "restored": _dsz_restore(masked, rmap),
                    "mapping": _wf_mapping(sess["reg"])})


@app.route("/api/desens-wf/restore-file", methods=["POST"])
def api_wf_restore_file():
    """独立还原回填：上传脱敏后的文件 + 导出时的映射表(.vault)+口令 → 把化名替回真名。

    也支持不传 vault 而带 sid，用当前会话的映射还原（等价于会话内还原对照）。
    """
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR}), 503
    # ① 取脱敏文本（文件优先，其次粘贴文本）
    text, name = "", "脱敏文件"
    f = request.files.get("file")
    if f:
        name = f.filename or "脱敏文件"
        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(name).suffix) as tmp:
            f.save(tmp.name); tp = Path(tmp.name)
        try:
            text = _wf_extract(tp)
        finally:
            tp.unlink(missing_ok=True)
    else:
        text = request.form.get("text") or ""
        name = "粘贴文本"
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "未能从该文件提取到文本"}), 400
    # ② 构造 化名→真名 还原表：优先用上传的 .vault，其次用当前会话
    rmap, src = {}, ""
    v = request.files.get("vault")
    if v and (v.filename or ""):
        pw = request.form.get("password", "")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".vault") as vt:
            v.save(vt.name); vp = Path(vt.name)
        try:
            mapping = vault_load(str(vp), pw)
        except Exception:
            return jsonify({"ok": False, "error": "映射表口令错误或文件损坏，无法解密"}), 400
        finally:
            vp.unlink(missing_ok=True)
        rmap = {r["alias"]: r["canonical"] for r in mapping
                if r.get("alias") and r.get("canonical")}
        src = "vault"
    else:
        sess = _wf_touch(_DESENS_WF.get(request.form.get("sid")))
        if sess:
            rmap = build_restore_map(sess["reg"]); src = "session"
    if not rmap:
        return jsonify({"ok": False, "error": "缺少映射表：请上传导出时生成的「_脱敏映射表.vault」并填口令"}), 400
    restored = _dsz_restore(text, rmap)
    return jsonify({"ok": True, "masked": text, "restored": restored,
                    "name": name, "count": len(rmap), "src": src})


@app.route("/api/desens-wf/export", methods=["POST"])
def api_wf_export():
    """导出脱敏文件（+alias 模式下的加密映射表）。mode: alias|blackout|asterisk。"""
    d = request.json or {}
    sess = _wf_touch(_DESENS_WF.get(d.get("sid")))
    if not sess:
        return jsonify({"ok": False, "error": "会话已失效"}), 404
    mode = d.get("mode", "alias")
    if mode not in ("alias", "blackout", "asterisk"):
        mode = "alias"
    spans = _wf_detect(sess)
    masked, applied = apply_review(sess["text"], spans, sess["reg"], mode=mode)
    out_parent = _desens_out_parent(d.get("out_dir", ""))
    stem = Path(sess.get("name") or "脱敏").stem
    dst = out_parent / f"{stem}_脱敏.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(masked, encoding="utf-8")
    pw = (d.get("vault_password") or "").strip()
    if pw and mode == "alias":
        vault_save(str(dst.parent / f"{stem}_脱敏映射表.vault"), sess["reg"].export(), password=pw)
    return jsonify({"ok": True, "output": str(dst), "count": len(applied), "mode": mode})


# ── 误报白名单（"永不脱敏"的词，全工作区生效，跨案件收敛误报）─────────────
_ALLOW_FILE = AF_DIR / "desens_allowlist.json"


def _desens_alnorm(t: str) -> str:
    return (t or "").strip().replace(" ", "").replace("　", "").lower()


def _load_allow_raw() -> list:
    try:
        v = json.loads(_ALLOW_FILE.read_text(encoding="utf-8"))
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _load_allow() -> set:
    return {_desens_alnorm(t) for t in _load_allow_raw() if t.strip()}


def _save_allow(terms: list) -> None:
    _ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ALLOW_FILE.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")


@app.route("/api/desens-allowlist")
def api_desens_allowlist():
    return jsonify({"ok": True, "terms": _load_allow_raw()})


@app.route("/api/desens-allowlist/add", methods=["POST"])
def api_desens_allowlist_add():
    t = ((request.json or {}).get("term") or "").strip()
    if not t:
        return jsonify({"ok": False, "error": "空词"}), 400
    terms = _load_allow_raw()
    if t not in terms:
        terms.append(t)
        _save_allow(terms)
    return jsonify({"ok": True, "terms": terms})


@app.route("/api/desens-allowlist/remove", methods=["POST"])
def api_desens_allowlist_remove():
    t = ((request.json or {}).get("term") or "").strip()
    terms = [x for x in _load_allow_raw() if x != t]
    _save_allow(terms)
    return jsonify({"ok": True, "terms": terms})


# ── 虚拟分类：给案件文件打文书类别标签，不搬动文件 ─────────────────────────
# 标准类别（系统 taxonomy 顶层），作为 llm_pick_folder 的候选清单，跨案件一致
_VIRTUAL_CATS = [
    "00_签约与委托", "01_案件资料", "02_法律研究", "03_证据", "04_诉讼仲裁文书",
    "05_庭审", "06_专家工作", "07_和解与调解", "08_裁决与执行",
    "10_客户沟通", "11_账单与工时", "12_内部工作文件", "13_结案归档",
]
_VIRTUAL_CAT_DESC = {
    "00_签约与委托": "委托代理协议、授权委托书、利益冲突、报价、接案",
    "01_案件资料": "客户提供的原始材料、事实梳理、合同/往来函件等基础资料",
    "02_法律研究": "法律检索、研究备忘录、类案、法律法规",
    "03_证据": "证据材料、证据清单/目录、举证质证",
    "04_诉讼仲裁文书": "起诉状/答辩状/代理意见/申请书/仲裁文书等正式书状",
    "05_庭审": "开庭传票/笔录、庭审提纲、庭审思维导图、发问提纲",
    "06_专家工作": "鉴定、专家辅助人、技术分析",
    "07_和解与调解": "和解协议、调解、谈判方案",
    "08_裁决与执行": "判决书/裁定书/裁决书、执行相关",
    "10_客户沟通": "往来邮件、阶段汇报、会议纪要、风险提示",
    "11_账单与工时": "账单、工时、费用",
    "12_内部工作文件": "内部策略、工作底稿、模板、脚本/工具",
    "13_结案归档": "结案报告、归档清单",
}
_DOC_EXTS = {".pdf", ".docx", ".doc", ".md", ".txt", ".rtf", ".pptx", ".ppt",
             ".xlsx", ".xls", ".eml"}
_CLASSIFY_JOB: dict = {
    "running": False, "done": False, "case": "", "i": 0, "total": 0,
    "current": "", "error": None,
}


def _classify_sidecar(case_dir: Path) -> Path:
    return case_dir / ".loci_classify.json"


def _run_classify_index(case_name: str, case_dir: Path) -> None:
    g = _CLASSIFY_JOB
    try:
        from llm_reader import extract_text
        from llm_classify import llm_doctype, llm_pick_folder
        llm_cfg = _load_config().get("llm", {})
        case_cfg = _case_meta_map().get(case_name, {})
        files = [f for f in sorted(case_dir.rglob("*"))
                 if f.is_file() and not f.name.startswith(".")]
        g["total"] = len(files)
        items = []
        for i, f in enumerate(files, 1):
            g.update(i=i, current=f.name)
            rel = f.relative_to(case_dir).as_posix()
            ext = f.suffix.lower()
            cat, dt, conf = "其他", "", 0.0
            if ext in _DOC_EXTS and llm_cfg.get("enabled"):
                try:
                    text, _ = extract_text(str(f))
                except Exception:
                    text = ""
                if text.strip():
                    d = llm_doctype(text, f.name, llm_cfg, case_cfg) or {}
                    dt = d.get("doc_type", "") or ""
                    if dt:
                        pick = llm_pick_folder(dt, d.get("source"), _VIRTUAL_CATS,
                                               llm_cfg, descriptions=_VIRTUAL_CAT_DESC)
                        if pick:
                            cat, conf = pick["target"], pick["confidence"]
            items.append({"path": str(f), "rel": rel, "name": f.name,
                          "ext": ext.lstrip("."), "doc_type": dt,
                          "category": cat, "confidence": round(conf, 2)})
        _classify_sidecar(case_dir).write_text(
            json.dumps({"case": case_name, "ts": datetime.now().isoformat(timespec="seconds"),
                        "items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:
        g["error"] = f"{type(e).__name__}: {e}"
    finally:
        g["running"] = False
        g["done"] = True


@app.route("/api/case/<path:case_name>/classify", methods=["POST"])
def api_case_classify(case_name):
    """启动虚拟分类：扫描案件所有文件→打文书类别标签→写 .loci_classify.json，不搬动文件。"""
    if _CLASSIFY_JOB["running"]:
        return jsonify({"ok": False, "error": "已有分类任务运行中，请等待完成"})
    case_dir = _case_dir(case_name)
    if not case_dir.is_dir():
        return jsonify({"ok": False, "error": "案件不存在"}), 404
    _CLASSIFY_JOB.update(running=True, done=False, case=case_name,
                         i=0, total=0, current="", error=None)
    threading.Thread(target=_run_classify_index, args=(case_name, case_dir),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/classify/status")
def api_classify_status():
    return jsonify(_CLASSIFY_JOB)


@app.route("/api/case/<path:case_name>/classified")
def api_case_classified(case_name):
    """读取已存的虚拟分类，按类别分组返回（文件原地不动，只给指针）。"""
    case_dir = _case_dir(case_name)
    sc = _classify_sidecar(case_dir)
    if not sc.exists():
        return jsonify({"ok": True, "exists": False, "groups": []})
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    groups: dict = {}
    for it in data.get("items", []):
        groups.setdefault(it.get("category", "其他"), []).append(it)
    order = _VIRTUAL_CATS + ["其他"]
    grouped = [{"category": c, "files": groups[c]}
               for c in order if c in groups]
    return jsonify({"ok": True, "exists": True, "ts": data.get("ts", ""),
                    "total": len(data.get("items", [])), "groups": grouped})


# ── 法院文书送达下载（折叠原 court_proxy.py；下载直接落到案件目录，不经浏览器）──
# 隐私说明：仅调用法院官方送达系统(zxfw.court.gov.cn)、用短信里的送达令牌拉取你自己
# 案件的官方文书并存到本地案件夹——是"把官方文书拉进来"，不向任何第三方/云上传当事人材料。
_COURT_API = "https://zxfw.court.gov.cn/yzw/yzw-zxfw-sdfw/api/v1/sdfw/getWsListBySdbhNew"
_COURT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _court_fetch_one(sdbh, qdbh, sdsin):
    import urllib.request, ssl
    body = json.dumps({"sdbh": sdbh, "qdbh": qdbh, "sdsin": sdsin}).encode()
    req = urllib.request.Request(
        _COURT_API, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _COURT_UA})
    with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 200 or not data.get("success"):
        raise RuntimeError(data.get("msg") or "法院 API 返回错误")
    return data.get("data") or []


@app.route("/api/court/fetch", methods=["POST"])
def api_court_fetch():
    """按送达链接参数(sdbh/qdbh/sdsin)调法院 API，返回按法院分组的文书清单。"""
    links = (request.json or {}).get("links") or []
    if not links:
        return jsonify({"ok": False, "error": "未提供有效链接"}), 400
    groups, errors = [], []
    for lk in links:
        try:
            files = _court_fetch_one(lk.get("sdbh", ""), lk.get("qdbh", ""), lk.get("sdsin", ""))
        except Exception as e:
            errors.append({"url": lk.get("rawUrl", ""), "error": str(e)})
            continue
        if not files:
            continue
        out = [{"wjlj": f.get("wjlj", ""),
                "filename": (f.get("c_wsmc") or "document") + "." + (f.get("c_wjgs") or "pdf").lower(),
                "ext": (f.get("c_wjgs") or "pdf").lower(),
                "court": f.get("c_fymc", ""), "c_wsbh": f.get("c_wsbh", "")} for f in files]
        groups.append({"label": files[0].get("c_fymc") or "未知法院", "files": out})
    return jsonify({"ok": True, "groups": groups, "errors": errors})


@app.route("/api/court/save", methods=["POST"])
def api_court_save():
    """把选中的文书服务端下载并存入指定案件目录（避开 pywebview 无法用浏览器下载/目录选择）。"""
    import urllib.request, ssl
    data = request.json or {}
    files = data.get("files") or []
    case = (data.get("case") or "").strip()
    subdir = (data.get("subdir") or "").strip().strip("/")
    save_dir = (data.get("save_dir") or "").strip()      # 自定义保存路径（原生对话框所选）
    if not files:
        return jsonify({"ok": False, "error": "未选择文书"}), 400
    if save_dir:                                          # 优先：直接存到用户所选文件夹
        dest_dir = Path(save_dir)
        if not dest_dir.is_absolute():
            return jsonify({"ok": False, "error": "保存路径需为绝对路径"}), 400
    else:
        if not case:
            return jsonify({"ok": False, "error": "缺少目标案件或保存路径"}), 400
        case_dir = _case_dir(case)
        if not case_dir.is_dir():
            return jsonify({"ok": False, "error": "案件不存在"}), 404
        dest_dir = _resolve_case_dest(case_dir, subdir) if subdir else case_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    ctx = ssl.create_default_context()
    saved, failed = [], []
    for f in files:
        url = f.get("url") or f.get("wjlj") or ""
        name = Path((f.get("filename") or "document.pdf")).name
        if not url:
            failed.append(name); continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _COURT_UA}, method="GET")
            with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
                blob = resp.read()
            target = dest_dir / name
            stem, ext, k = target.stem, target.suffix, 1
            while target.exists():
                target = dest_dir / f"{stem}（{k}）{ext}"; k += 1
            target.write_bytes(blob)
            saved.append(target.name)
        except Exception:
            failed.append(name)
    return jsonify({"ok": True, "saved": saved, "failed": failed, "dest": str(dest_dir)})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    results = []
    if CASES.exists():
        for case_dir in CASES.iterdir():
            if not case_dir.is_dir() or case_dir.name.startswith(("_", ".")):
                continue
            for f in case_dir.rglob("*"):
                if f.is_file() and not f.name.startswith(".") and q.lower() in f.name.lower():
                    results.append({
                        "case": case_dir.name,
                        "path": str(f.relative_to(case_dir)),
                        "full_path": str(f),
                        "name": f.name,
                    })
                    if len(results) >= 60:
                        break
            if len(results) >= 60:
                break
    return jsonify({"results": results, "query": q})


def _set_case_status(case_name, status):
    cfg_path = AF_DIR / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    found = False
    for c in cfg.get("cases", []):
        if c.get("name") == case_name:
            c["status"] = status
            found = True
            break
    if not found:
        cfg.setdefault("cases", []).append({"name": case_name, "key": case_name[:20], "status": status})
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return jsonify({"ok": True})


@app.route("/api/case/<path:case_name>/rebuild-folders", methods=["POST"])
def api_case_rebuild_folders(case_name):
    if not _HAS_INTAKE:
        return jsonify({"error": "intake.py not available"}), 500
    data = request.json or {}
    proc_type = _SCENARIO_PROC.get(data.get("scenario", ""), "civil")
    role = data.get("role", "plaintiff")
    case_dir = _case_dir(case_name)
    if not case_dir.exists():
        return jsonify({"error": "案件文件夹不存在"}), 404
    try:
        _create_folders(case_dir, proc_type, role)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/case/<path:case_name>/reset", methods=["POST"])
def api_case_reset(case_name):
    """清空案件文件夹内全部内容，然后重建空目录结构。"""
    data = request.json or {}
    case_dir = _case_dir(case_name)
    if not case_dir.exists() or not case_dir.is_dir():
        return jsonify({"error": "案件文件夹不存在"}), 404

    # 删除案件文件夹内所有子项（保留文件夹本身）
    errors = []
    for item in list(case_dir.iterdir()):
        try:
            if item.is_dir():
                shutil.rmtree(str(item))
            else:
                item.unlink()
        except Exception as e:
            errors.append(f"{item.name}: {e}")
    if errors:
        return jsonify({"error": "部分文件删除失败: " + "; ".join(errors[:3])}), 500

    # 重建空文件夹结构
    if _HAS_INTAKE:
        proc_type = _SCENARIO_PROC.get(data.get("scenario", ""), "civil")
        role = data.get("role", "plaintiff")
        try:
            _create_folders(case_dir, proc_type, role)
        except Exception as e:
            return jsonify({"ok": True, "warn": f"文件夹重建失败：{e}"})

    return jsonify({"ok": True})


@app.route("/api/case/<path:case_name>/archive", methods=["POST"])
def api_case_archive(case_name):
    return _set_case_status(case_name, "closed")


@app.route("/api/case/<path:case_name>/unarchive", methods=["POST"])
def api_case_unarchive(case_name):
    return _set_case_status(case_name, "active")


@app.route("/api/case/<path:case_name>/delete", methods=["POST"])
def api_case_delete(case_name):
    data = request.json or {}
    delete_folder = data.get("delete_folder", False)

    # 1) 从 config 移除登记（若有）
    cfg_path = AF_DIR / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["cases"] = [c for c in cfg.get("cases", []) if c.get("name") != case_name]
    cfg_path.write_text(
        yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    # 2) 处理文件夹
    case_dir = _case_dir(case_name)
    moved_to = None
    if case_dir.exists() and case_dir.is_dir():
        if delete_folder:
            try:
                shutil.rmtree(str(case_dir))
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        elif case_dir.resolve().parent == CASES.resolve():
            # 不删文件，但该文件夹在默认 cases/ 下——若原地保留，/api/cases 的磁盘扫描
            # 会把它当作案件反复列出（"删不掉"）。移入隐藏的 cases/_已移除/ 隔离：
            # 退出工作区、文件完整保留、可随时找回或手动彻底删除。
            try:
                holding = CASES / "_已移除"
                holding.mkdir(parents=True, exist_ok=True)
                dest = holding / case_dir.name
                i = 1
                while dest.exists():
                    dest = holding / f"{case_dir.name}_{i}"
                    i += 1
                shutil.move(str(case_dir), str(dest))
                moved_to = str(dest)
            except Exception as e:
                return jsonify({"error": f"移出工作区失败：{e}"}), 500

    return jsonify({"ok": True, "moved_to": moved_to})


@app.route("/api/file/delete", methods=["POST"])
def api_file_delete():
    path = (request.json or {}).get("path", "")
    if not path:
        return jsonify({"error": "no path"}), 400
    p = Path(path)
    if not _path_allowed(p):
        return jsonify({"error": "path not in workspace"}), 403
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        if p.is_dir():
            shutil.rmtree(str(p))
        else:
            p.unlink()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/file/rename", methods=["POST"])
def api_file_rename():
    data = request.json or {}
    path = data.get("path", "")
    new_name = data.get("new_name", "").strip()
    if not path or not new_name:
        return jsonify({"error": "missing path or name"}), 400
    if "/" in new_name or "\\" in new_name:
        return jsonify({"error": "文件名不能包含路径分隔符"}), 400
    p = Path(path)
    if not _path_allowed(p):
        return jsonify({"error": "path not in workspace"}), 403
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    new_path = p.parent / new_name
    if new_path.exists():
        return jsonify({"error": "同名文件已存在"}), 409
    try:
        p.rename(new_path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5080, debug=False, threaded=True)
