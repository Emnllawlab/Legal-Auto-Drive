#!/usr/bin/env python3
"""Loci — Flask 后端"""

import ipaddress, logging, os, re, sys, json, time, yaml, signal, subprocess, threading, shutil, uuid
from contextlib import contextmanager
import tempfile
from urllib.parse import urlsplit
from pathlib import Path
from datetime import datetime
from typing import Optional
from flask import Flask, jsonify, request, render_template, send_file
from werkzeug.exceptions import RequestEntityTooLarge

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024
app.config["APP_PORT"] = 5081

WS  = Path(__file__).resolve().parent.parent
AF  = WS / "auto_filer"
BIN = WS / "bin"
sys.path.insert(0, str(WS))
from auto_filer.config_io import (
    load_yaml, set_private_umask, tighten_private_permissions, update_yaml,
)
from auto_filer.domain import canonicalize_case, runtime_case_view
from auto_filer.ledger import FilingLedger, LedgerError, sha256_file
from auto_filer.state_db import DecisionStore, DecisionStoreError
from auto_filer.decision import build_filing_decision
from auto_filer.decision import SOURCE_OPTIONS
from auto_filer.rule_governance import (
    append_rule_history, load_rule_history, rules_digest, validate_rules,
)
from auto_filer import daemon_control as _daemon_control
from auto_filer.safety import (
    MAX_UPLOAD_FILES, FileLock, SafetyError, existing_regular_file, is_relative_to,
    safe_case_root, safe_target_path, save_upload, validate_basename,
    validate_case_name, validate_relative_target, validate_relative_file_path,
)


@app.before_request
def _enforce_local_request_boundary():
    """Only accept local callers and same-origin browser requests."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    peer = request.remote_addr or ""
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = peer.lower() == "localhost"
    if not is_loopback:
        return jsonify({"ok": False, "error": "仅允许本机回环地址访问"}), 403

    expected_port = int(app.config["APP_PORT"])
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return jsonify({"ok": False, "error": "来源地址无效"}), 403
        if host not in {"localhost", "127.0.0.1", "::1"} or port != expected_port:
            return jsonify({"ok": False, "error": "请求来源不是本地同源页面"}), 403
    return None


@app.errorhandler(RequestEntityTooLarge)
def _request_too_large(_error):
    return jsonify({"ok": False, "error": "上传请求超过 250 MB 限制"}), 413

# ── 脱敏引擎（已捆绑进 loci/desensitize/，自包含）──────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from desensitize import (Registry, make_ocr, desensitize_tree,
                             analyze_text, apply_review, to_markdown,
                             REDACTION_POLICIES, resolve_categories)
    _DESENS_OK, _DESENS_ERR = True, ""
except Exception as _e:                       # 引擎缺失不影响 Loci 其余功能
    _DESENS_OK, _DESENS_ERR = False, str(_e)

# 全案脱敏后台任务状态（单用户本地，单任务即可）
_DESENS_JOB = {"running": False, "case": "", "i": 0, "total": 0,
               "current": "", "done": False, "report": None, "error": None, "dst": ""}


def _redact_secret(value, secret):
    """Copy a task value without exposing a supplied vault secret."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[已隐藏]")
    if isinstance(value, dict):
        return {
            key: _redact_secret(item, secret)
            for key, item in value.items()
            if str(key).lower() not in {"password", "vault_password"}
        }
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret(item, secret) for item in value)
    return value


def _run_desensitize(src, dst, password, policy="strict"):
    g = _DESENS_JOB
    try:
        # 案件材料可能中英混排；整案任务不能因为默认中文模式而漏掉
        # complaint / plaintiff / email 等英文敏感信息。
        reg = Registry(lang="both")
        # Loci Lite 默认不启动或依赖 LLM；脱敏仍可使用规则、校验位和 OCR。
        ner = None
        ocr = make_ocr()

        def prog(i, total, rel, res):
            g.update(i=i, total=total, current=_redact_secret(rel, password))

        report = desensitize_tree(
            src, dst, reg=reg, ner=ner, ocr_engine=ocr,
            vault_password=password, progress=prog, cats=policy, lang="both",
        )
        g["report"] = _redact_secret(report, password)
    except Exception as e:
        g["error"] = _redact_secret(f"{type(e).__name__}: {e}", password)
    finally:
        g["running"], g["done"] = False, True
        password = None


def _run_desensitize_with_secret(src, dst, secret_box, policy="strict"):
    """Take the password out of the thread argument container immediately."""
    password = secret_box.pop() if secret_box else None
    try:
        _run_desensitize(src, dst, password, policy)
    finally:
        password = None
        secret_box.clear()

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


def _prepare_startup_security():
    """Apply the Loci control-plane privacy boundary at process startup."""
    set_private_umask()
    logger = logging.getLogger("loci.security")
    try:
        logs_dir = _paths()["logs"]
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("环境文件无法读取，仍继续收紧默认控制面权限（%s）",
                       type(exc).__name__)
        logs_dir = str(WS / "logs")
    return tighten_private_permissions(
        env_path=WS / ".env",
        config_path=AF / "config.yaml",
        logs_dir=logs_dir,
        logger=logger,
    )

def _config():
    cfg = load_yaml(AF / "config.yaml", default={}) or {}
    cfg["cases"] = [runtime_case_view(case) for case in cfg.get("cases", [])]
    return cfg


def _case_path(case: dict, paths: dict) -> Path:
    import_path = (case.get("import_path") or "").strip()
    if import_path:
        return Path(import_path).expanduser().resolve()
    return (Path(case.get("cases_dir") or paths["cases"]) / case.get("name", "")).resolve()


_STAGE_PROC_TYPES = {
    "中国民事": "civil",
    "中国刑事": "criminal",
    "仲裁": "arbitration",
    "美国民事": "us_civil",
    "刑民交叉": "mixed",
}
_STAGE_DEFAULT_ROLES = {
    "civil": "plaintiff",
    "criminal": "suspect",
    "arbitration": "claimant",
    "us_civil": "plaintiff",
    "mixed": "plaintiff+suspect",
}

_UPLOAD_META_FIELDS = {
    "case_key", "target", "doc_type", "source", "date", "party", "batch",
    "version", "translation", "notes", "also_cases", "stage", "country",
    "procedure",
}
_UPLOAD_STRING_LIMITS = {
    "case_key": 120, "target": 500, "doc_type": 180, "source": 40,
    "date": 10, "party": 180, "version": 40, "notes": 1200,
    "stage": 80, "country": 20, "procedure": 40,
}
_UPLOAD_STAGES = {
    "诉前", "侦查", "审查起诉", "一审", "二审", "再审", "申诉", "执行",
    "仲裁", "司法审查", "Pleadings", "Discovery", "Trial", "Appeal",
}
_UPLOAD_COUNTRIES = {"cn", "us"}
_UPLOAD_PROCEDURES = {"civil", "arb", "arbitration", "criminal", "us", "us_civil", "mixed"}


def _present_upload_values(value):
    """Drop blank HTML form values while retaining booleans and zero safely."""
    if not isinstance(value, dict):
        return value
    return {
        key: item for key, item in value.items()
        if item is not None and item != "" and item != []
    }


def _validate_upload_meta(raw, cases: list[dict]) -> dict:
    """Validate human-provided filing facts before any upload is persisted."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SafetyError("投放信息必须是对象")
    unknown = sorted(set(raw) - _UPLOAD_META_FIELDS)
    if unknown:
        raise SafetyError("投放信息含未知字段：" + "、".join(unknown))
    value = _present_upload_values(raw)
    result = {}
    case_keys = {str(case.get("key", "")) for case in cases if case.get("key")}

    for key, item in value.items():
        if key in {"translation", "batch", "also_cases"}:
            continue
        if not isinstance(item, str):
            raise SafetyError(f"{key} 格式无效")
        item = item.strip()
        if len(item) > _UPLOAD_STRING_LIMITS[key]:
            raise SafetyError(f"{key} 内容过长")
        if any(ord(ch) < 32 for ch in item):
            raise SafetyError(f"{key} 含非法控制字符")
        result[key] = item

    if result.get("case_key") and result["case_key"] not in case_keys:
        raise SafetyError("所属案件不存在或不可用")
    if result.get("target"):
        validate_relative_target(result["target"], "目标文件夹")
    if result.get("source") and result["source"] not in SOURCE_OPTIONS:
        raise SafetyError("文件来源不在允许选项中")
    if result.get("date"):
        try:
            datetime.strptime(result["date"], "%Y-%m-%d")
        except ValueError as exc:
            raise SafetyError("收件日期必须为 YYYY-MM-DD") from exc
    if result.get("stage") and result["stage"] not in _UPLOAD_STAGES:
        raise SafetyError("程序阶段不在允许选项中")
    if result.get("country") and result["country"] not in _UPLOAD_COUNTRIES:
        raise SafetyError("国家/地区不在允许选项中")
    if result.get("procedure") and result["procedure"] not in _UPLOAD_PROCEDURES:
        raise SafetyError("程序类型不在允许选项中")

    if "batch" in value:
        batch = value["batch"]
        if isinstance(batch, bool):
            raise SafetyError("组次格式无效")
        try:
            batch = int(batch)
        except (TypeError, ValueError) as exc:
            raise SafetyError("组次必须为数字") from exc
        if batch < 2 or batch > 99:
            raise SafetyError("组次必须在 2–99 之间")
        result["batch"] = batch

    if "translation" in value:
        if not isinstance(value["translation"], bool):
            raise SafetyError("翻译件标记格式无效")
        result["translation"] = value["translation"]

    if "also_cases" in value:
        linked = value["also_cases"]
        if not isinstance(linked, list) or any(not isinstance(x, str) for x in linked):
            raise SafetyError("关联案件格式无效")
        linked = list(dict.fromkeys(x.strip() for x in linked if x.strip()))
        if len(linked) > 20 or any(x not in case_keys for x in linked):
            raise SafetyError("关联案件包含不存在的案件或数量过多")
        result["also_cases"] = linked
    return result


def _cleanup_created_stage_dirs(root: Path, created, existing_dirs=None) -> None:
    """Remove only newly-created, still-empty stage directories after rollback."""
    root = Path(root).resolve()
    existing_dirs = existing_dirs or set()
    rel_paths = []
    seen = set()
    for value in created or []:
        try:
            rel = Path(str(value))
        except (TypeError, ValueError):
            continue
        if rel.is_absolute() or not rel.parts or rel in seen:
            continue
        seen.add(rel)
        rel_paths.append(rel)

    for rel in sorted(rel_paths, key=lambda item: (len(item.parts), item.as_posix()),
                      reverse=True):
        candidate = root / rel
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if relative == Path(".") or relative in existing_dirs:
            continue
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            entries = list(candidate.iterdir())
            if not entries:
                candidate.rmdir()
                continue
            if len(entries) != 1:
                continue
            marker = entries[0]
            if marker.name != ".gitkeep" or marker.is_symlink() or not marker.is_file():
                continue
            marker.unlink()
            candidate.rmdir()
        except (OSError, RuntimeError):
            continue


def _remove_intake_staging(staging: Optional[Path], parent: Path,
                           prefix: str) -> Optional[str]:
    """Remove only the staging directory created by the current intake.

    The caller supplies the exact path returned by ``mkdtemp``.  Before using
    ``rmtree`` we verify its name, parent, and non-symlink status so a cleanup
    failure cannot turn into deletion of an unrelated directory.
    """
    if staging is None:
        return None
    try:
        candidate = Path(staging)
        expected_parent = Path(parent).expanduser().resolve()
        if not candidate.is_absolute():
            return "staging 路径不是绝对路径"
        if not candidate.name.startswith(prefix):
            return "staging 路径名称校验失败"
        if candidate.parent.resolve() != expected_parent:
            return "staging 路径不在本次案件根目录同级"
        if candidate.is_symlink():
            return "staging 路径变为符号链接，拒绝清理"
        resolved = candidate.resolve(strict=False)
        if resolved != candidate or resolved.parent != expected_parent:
            return "staging 路径解析后越界"
        if not candidate.exists() or not candidate.is_dir():
            return None
        shutil.rmtree(candidate)
        return None
    except Exception as exc:
        return f"清理 staging 失败：{type(exc).__name__}: {exc}"


def _case_roots(cfg: dict, paths: dict) -> list[Path]:
    roots = [Path(paths["inbox"]).expanduser().resolve(),
             (Path(paths["cases"]) / "_未分类").expanduser().resolve()]
    for case in cfg.get("cases", []):
        try:
            root = _case_path(case, paths)
            roots.extend((root, root / "_待确认来源"))
        except (OSError, RuntimeError, ValueError):
            continue
    return roots


def _pending_roots(cfg: dict, paths: dict) -> list[Path]:
    roots = []
    for case in cfg.get("cases", []):
        try:
            roots.append(_case_path(case, paths) / "_待确认来源")
        except (OSError, RuntimeError, ValueError):
            continue
    return roots


def _safe_case_dest(case: dict, paths: dict, target: str) -> Path:
    root = _case_path(case, paths)
    return safe_target_path(root, target)

_DAEMON_START_TIMEOUT = 5.0


def _daemon_snapshot() -> dict:
    """Return the safe, UI-facing lifecycle fields from the shared controller."""
    status = _daemon_control.daemon_status(AF)
    return {
        "running": bool(status.running),
        "ready": bool(status.ready),
        "detail": status.detail,
        "pid": status.pid,
    }


def _pid() -> Optional[int]:
    """Return a PID only for a ready, identity-validated daemon."""
    daemon = _daemon_snapshot()
    return daemon["pid"] if daemon["running"] else None


def _start_daemon():
    """Use the shared lifecycle controller; it owns no duplicated PID logic."""
    result = _daemon_control.start_daemon(
        AF,
        python_executable=sys.executable,
        cwd=AF,
        timeout=_DAEMON_START_TIMEOUT,
    )
    return result.pid, result.already_running, result.error


def _stop_daemon():
    result = _daemon_control.stop_daemon(AF, timeout=_DAEMON_START_TIMEOUT)
    return result.ok, result.error

def _log_file():
    return Path(_paths()["logs"]) / f"filer_{datetime.now().strftime('%Y%m')}.log"

# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    paths = _paths()
    try:
        daemon = _daemon_snapshot()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"守护进程状态读取失败：{exc}"}), 500
    inbox = Path(paths["inbox"])
    files = []
    if inbox.exists():
        files = [f for f in inbox.iterdir()
                 if f.is_file() and not f.name.startswith(".")
                 and f.name not in {"README.txt", "README.md", ".gitkeep"}]
    cfg = _config()
    return jsonify({
        "daemon":      daemon,
        "inbox_count": len(files),
        "case_count":  len(cfg.get("cases", [])),
        "paths":       paths,
    })

@app.route("/api/daemon/start", methods=["POST"])
def daemon_start():
    if not _daemon_control.unattended_filing_enabled(AF):
        return jsonify({
            "ok": False,
            "error": (
                "无人值守守护已锁定：默认仅人工确认。请在私有 .env 中明确设置 "
                "ALLOW_UNATTENDED_FILING=true，并在充分验收后再启用；前端确认不能绕过此门禁。"
            ),
        }), 409
    pid, already_running, error = _start_daemon()
    if error:
        return jsonify({"ok": False, "error": error}), 500
    body = {"ok": True, "pid": pid}
    if already_running:
        body["msg"] = "already running"
    return jsonify(body)

@app.route("/api/daemon/stop", methods=["POST"])
def daemon_stop():
    ok, error = _stop_daemon()
    if not ok:
        return jsonify({"ok": False, "error": error or "守护进程停止失败"}), 500
    return jsonify({"ok": True})

@app.route("/api/once", methods=["POST"])
def once():
    dry = (request.json or {}).get("dry_run", False)
    cmd = [sys.executable, str(AF / "auto_filer.py"), "--once", "-v"]
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
    src = _case_path(case, paths)
    if not src.exists():
        return jsonify({"ok": False, "error": "案件文件夹不存在"})
    dst = Path(paths["cases"]).parent / "cases_脱敏" / case["name"]
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求格式无效"}), 400
    policy = data.get("policy", "strict")
    if policy not in REDACTION_POLICIES:
        return jsonify({"ok": False, "error": "未知脱敏策略"}), 400

    if "vault_password" not in data:
        vault_password = None
    else:
        vault_password = data.get("vault_password")
        if not isinstance(vault_password, str):
            return jsonify({"ok": False, "error": "vault_password 必须是字符串"}), 400
        if len(vault_password) < 12:
            return jsonify({"ok": False, "error": "vault_password 至少需要 12 个字符"}), 400
        identifiers = []
        for value in (key, case.get("abbr"), case.get("key"), case.get("case_number")):
            if isinstance(value, str) and value:
                identifiers.append(value)
        normalized_password = vault_password.strip().casefold()
        if any(
            vault_password == value
            or normalized_password == value.strip().casefold()
            for value in identifiers
        ):
            return jsonify({"ok": False, "error": "不得使用案件 key、简称或案号作为 vault_password"}), 400

    # Keep the secret in one short-lived mutable container.  The worker pops
    # it before doing any work, so the Thread argument tuple retains only the
    # empty container rather than another password reference.
    secret_box = [vault_password]
    safe_case = _redact_secret(case["name"], vault_password)
    safe_dst = _redact_secret(str(dst), vault_password)
    _DESENS_JOB.update(running=True, done=False, case=safe_case, i=0, total=0,
                       current="", report=None, error=None, dst=safe_dst)
    try:
        threading.Thread(
            target=_run_desensitize_with_secret,
            args=(str(src), str(dst), secret_box, policy),
            daemon=True,
        ).start()
    except Exception:
        secret_box.clear()
        vault_password = None
        _DESENS_JOB.update(running=False, done=True, error="脱敏任务启动失败")
        return jsonify({"ok": False, "error": "脱敏任务启动失败"}), 500
    data = None
    vault_password = None
    return jsonify({"ok": True, "dst": safe_dst})

@app.route("/api/desensitize/status")
def desensitize_status():
    return jsonify(_DESENS_JOB)


@app.route("/api/desensitize/policies")
def desensitize_policies():
    """返回可选脱敏政策，供前台/外部脚本显示策略说明。"""
    return jsonify({"policies": [
        {"key": "strict", "name": "严格保密",
         "description": "包括案号、文号和金额在内的全部识别类别"},
        {"key": "public_legal", "name": "公开法律材料",
         "description": "保留案号、文号和金额，仅处理主体、联系方式及身份信息"},
    ]})


# ══════════════════════════════════════════════════════════════
# 脱敏工作台：单文件精细复核（识别→人工删补→左右预览→导出）
# 会话级 Registry 按案缓存，保证同一案件内跨文件化名一致；
# NER / OCR 惰性单例，连不上 Ollama 时自动降级为纯正则。
# ══════════════════════════════════════════════════════════════
_DESENS_REG = {}                       # case_key -> Registry
_DESENS_LAZY = {"ner": None, "ocr": None}   # 惰性单例（None=未初始化, False=不可用）
_DESENS_FILE_SELECTIONS = {}           # token -> {path, created}
_DESENS_OUTPUT_SELECTIONS = {}         # token -> {path, created}
_DESENS_SELECTION_TTL = 30 * 60        # 访达选择令牌只在当前会话短暂有效
_DESENS_SELECTION_LIMIT = 32

# 可提取文本的类型：文档走 to_markdown，图片/扫描件走 OCR
_DESENS_DOC = {".txt", ".md", ".docx", ".doc", ".pdf"}
_DESENS_IMG = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
_DESENS_EXT = _DESENS_DOC | _DESENS_IMG


def _get_registry(key):
    reg = _DESENS_REG.get(key)
    if reg is None:
        # 工作台明确支持中英混排；若使用 zh，英文诉状会出现“正文已读取
        # 但命中 0 处”的假阴性，随后自然也不会产生任何脱敏结果。
        reg = _DESENS_REG[key] = Registry(lang="both")
    return reg


def _get_ner():
    """Loci Lite 不启动本地 LLM；调用方使用规则层。"""
    return None


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
    try:
        rel = validate_relative_file_path(rel, "材料相对路径")
    except (SafetyError, TypeError) as exc:
        return None, None, str(exc)
    root = _case_path(case, paths)
    try:
        target = existing_regular_file(root / rel, [root])
    except FileNotFoundError:
        return None, None, "文件不存在"
    except SafetyError as exc:
        return None, None, str(exc)
    return case, target, None


def _prune_desens_selections():
    """清理过期的 Finder 选择令牌，避免长期持有本机路径。"""
    cutoff = time.time() - _DESENS_SELECTION_TTL
    for registry in (_DESENS_FILE_SELECTIONS, _DESENS_OUTPUT_SELECTIONS):
        stale = [token for token, item in registry.items()
                 if item.get("created", 0) < cutoff]
        for token in stale:
            registry.pop(token, None)
        while len(registry) > _DESENS_SELECTION_LIMIT:
            oldest = min(registry, key=lambda token: registry[token].get("created", 0))
            registry.pop(oldest, None)


def _put_desens_selection(registry, path: Path) -> str:
    _prune_desens_selections()
    token = uuid.uuid4().hex
    registry[token] = {"path": path.resolve(), "created": time.time()}
    return token


def _get_desens_selection(registry, token, label="文件"):
    _prune_desens_selections()
    if not isinstance(token, str) or not token or token not in registry:
        return None, f"{label}选择已失效，请重新从访达选择"
    item = registry[token]
    path = Path(item["path"])
    try:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return None, f"{label}已不存在或不是普通文件，请重新选择"
        return path.resolve(), None
    except (OSError, RuntimeError, ValueError):
        return None, f"无法读取所选{label}，请重新选择"


def _get_desens_output(token):
    _prune_desens_selections()
    if not isinstance(token, str) or not token or token not in _DESENS_OUTPUT_SELECTIONS:
        return None, "输出工作区选择已失效，请重新选择"
    root = Path(_DESENS_OUTPUT_SELECTIONS[token]["path"])
    try:
        if root.is_symlink() or not root.exists() or not root.is_dir():
            return None, "输出工作区不存在或不是文件夹，请重新选择"
        return root.resolve(), None
    except (OSError, RuntimeError, ValueError):
        return None, "无法访问输出工作区，请重新选择"


def _choose_from_finder(kind):
    """通过 macOS Finder 对话框选择一个源文件或输出文件夹。"""
    if shutil.which("osascript") is None:
        return None, False, "当前系统没有可用的 macOS 访达选择器"
    if kind == "file":
        script = 'POSIX path of (choose file with prompt "选择待脱敏文件")'
    else:
        script = 'POSIX path of (choose folder with prompt "选择脱敏输出工作区")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, False, f"访达选择器启动失败：{type(exc).__name__}"
    if result.returncode != 0:
        # AppleScript 的取消操作返回 -128；其他非零结果要反馈真实错误，
        # 否则前台看起来像“什么都没发生”。
        detail = (result.stderr or "").strip()
        cancelled = result.returncode == 1 and ("-128" in detail or "cancel" in detail.lower())
        if cancelled:
            return None, True, "用户取消选择"
        return None, False, f"访达选择器返回错误：{detail or result.returncode}"
    value = (result.stdout or "").strip()
    if not value:
        return None, True, "用户取消选择"
    try:
        path = Path(value).expanduser().resolve()
        if kind == "file":
            if path.is_symlink() or not path.is_file():
                return None, False, "所选对象不是普通文件"
            if path.suffix.lower() not in _DESENS_EXT:
                supported = "、".join(sorted(_DESENS_EXT))
                return None, False, f"暂不支持该文件格式；当前支持：{supported}"
        elif path.is_symlink() or not path.is_dir():
            return None, False, "所选对象不是文件夹"
        return path, False, None
    except (OSError, RuntimeError, ValueError) as exc:
        return None, False, f"所选路径不可用：{type(exc).__name__}"


def _resolve_desens_source(data):
    """解析工作台选定的文件：访达令牌优先，其次是案件相对路径。"""
    selection_id = data.get("selection_id", "")
    if selection_id:
        path, err = _get_desens_selection(_DESENS_FILE_SELECTIONS, selection_id)
        return None, path, err
    key = data.get("case", "")
    case, path, err = _resolve_case_file(key, data.get("rel", ""))
    return case, path, err


def _resolve_workbench_categories(data):
    """解析工作台的脱敏范围；custom 由前台传入明确类别集合。"""
    policy = data.get("policy", "strict")
    if policy in REDACTION_POLICIES:
        return policy, None
    if policy != "custom":
        return None, "未知脱敏范围，请重新选择"
    raw = data.get("categories")
    if not isinstance(raw, list):
        return None, "自定义脱敏范围无效，请至少选择一个类别"
    try:
        categories = resolve_categories(raw)
    except (TypeError, ValueError) as exc:
        return None, f"自定义脱敏范围无效：{exc}"
    known = set(REDACTION_POLICIES["strict"])
    categories &= known
    return categories, None


def _friendly_desensitize_error(exc):
    """将底层 PDF/原生扩展错误转换为前台可执行的短提示。"""
    detail = str(exc)
    if "incompatible architecture" in detail or "dlopen" in detail:
        return (
            "PDF 读取失败：当前启动 Loci 的 Python 与 PDF 依赖架构不一致。"
            "请关闭已有 Loci，运行 setup.sh 创建项目虚拟环境，"
            "再使用 .venv/bin/python loci/run.py 启动；不要使用 Rosetta/x86_64 Python。"
        )
    return (
        f"文档读取失败（{type(exc).__name__}）。"
        "请确认文件未损坏、当前用户有读取权限，并从项目启动入口重试。"
    )


def _next_desens_output_path(root: Path, source_name: str) -> Path:
    """在用户选定工作区生成不覆盖旧文件的脱敏 Markdown 文件名。"""
    name = Path(source_name or "脱敏结果").name
    stem = Path(name).stem or "脱敏结果"
    try:
        validate_basename(stem, "源文件名")
    except SafetyError:
        stem = "脱敏结果"
    for index in range(10000):
        suffix = "" if index == 0 else f"_{index}"
        candidate = root / f"{stem}{suffix}.脱敏.md"
        if not candidate.exists():
            return candidate
    raise SafetyError("输出工作区中无法生成不重复的文件名")


@app.route("/api/desensitize/pick-file", methods=["POST"])
def desensitize_pick_file():
    """打开 macOS 访达文件选择器，用户明确选择一个待脱敏源文件。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    path, cancelled, error = _choose_from_finder("file")
    if cancelled:
        return jsonify({"ok": False, "cancelled": True, "error": error})
    if error:
        return jsonify({"ok": False, "error": error})
    token = _put_desens_selection(_DESENS_FILE_SELECTIONS, path)
    return jsonify({"ok": True, "selection_id": token, "name": path.name,
                    "ext": path.suffix.lower().lstrip("."),
                    "location": str(path.parent)})


@app.route("/api/desensitize/pick-output", methods=["POST"])
def desensitize_pick_output():
    """打开 macOS 访达文件夹选择器，用户明确选择脱敏输出工作区。"""
    path, cancelled, error = _choose_from_finder("folder")
    if cancelled:
        return jsonify({"ok": False, "cancelled": True, "error": error})
    if error:
        return jsonify({"ok": False, "error": error})
    token = _put_desens_selection(_DESENS_OUTPUT_SELECTIONS, path)
    return jsonify({"ok": True, "output_id": token, "name": path.name,
                    "location": str(path)})


@app.route("/api/desensitize/open-file", methods=["POST"])
def desensitize_open_file():
    """在 Finder 中显示当前选定的脱敏源文件。"""
    data = request.json or {}
    _case, path, error = _resolve_desens_source(data)
    if error:
        return jsonify({"ok": False, "error": error})
    if shutil.which("open") is None:
        return jsonify({"ok": False, "error": "当前系统没有可用的 Finder 打开命令"})
    result = subprocess.run(["open", "-R", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        # 某些 macOS 会对带特殊字符或云盘占位文件的 -R 返回失败；
        # 回退到打开所在目录，仍然让用户能在访达中定位源文件。
        fallback = subprocess.run(
            ["open", "-a", "Finder", str(path.parent)],
            capture_output=True, text=True,
        )
        if fallback.returncode != 0:
            detail = (fallback.stderr or result.stderr or "无法打开 Finder").strip()
            return jsonify({"ok": False, "error": detail})
    return jsonify({"ok": True})


@app.route("/api/desensitize/files")
def desensitize_wb_files():
    """列出某案可脱敏的文件。

    返回扫描警告而不是把权限错误静默转换成空列表。案件目录经常位于
    OneDrive 或外接盘，前台需要明确告诉用户是“没有材料”还是“当前进程
    无法读取目录”。
    """
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    key = request.args.get("case", "")
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    root = _case_path(case, paths)
    if not root.exists():
        return jsonify({"ok": True, "exists": False, "files": [], "name": case["name"]})
    if not root.is_dir():
        return jsonify({"ok": True, "exists": False, "files": [], "name": case["name"],
                        "warnings": ["案件路径不是文件夹"]})

    files, warnings, unsupported = [], [], 0

    def on_walk_error(exc):
        location = getattr(exc, "filename", None) or str(root)
        warnings.append(f"无法读取目录：{location}")

    # os.walk 的 onerror 能把 OneDrive 的权限/按需下载错误显式传回前台；
    # followlinks=False 且额外跳过符号链接，避免把案件清单带到目录外。
    for base, dirs, names in os.walk(root, topdown=True, onerror=on_walk_error,
                                     followlinks=False):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and not (Path(base) / d).is_symlink()
        )
        for name in sorted(names):
            if name.startswith("."):
                continue
            p = Path(base) / name
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                suffix = p.suffix.lower()
                if suffix not in _DESENS_EXT:
                    unsupported += 1
                    continue
                rel = p.relative_to(root)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                st = p.stat()
                files.append({
                    "rel": rel.as_posix(), "name": p.name, "size": st.st_size,
                    "ext": suffix.lstrip("."),
                    "dir": rel.parent.as_posix() if str(rel.parent) != "." else "",
                })
            except (OSError, ValueError) as exc:
                warnings.append(f"无法读取文件：{name}（{type(exc).__name__}）")

    return jsonify({"ok": True, "exists": True, "files": files, "name": case["name"],
                    "warnings": warnings[:20], "unsupported": unsupported,
                    "supported_extensions": sorted(_DESENS_EXT)})


@app.route("/api/desensitize/analyze", methods=["POST"])
def desensitize_wb_analyze():
    """识别一段文本/一个文件的敏感信息，返回原文 + span 列表（不提交注册表）。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    key = data.get("case", "")
    text = data.get("text")            # 直接传文本（粘贴模式）优先
    source_name = ""
    if text is None:
        case, path, err = _resolve_desens_source(data)
        if err:
            return jsonify({"ok": False, "error": err})
        source_name = path.name
        try:
            ext = path.suffix.lower()
            if ext in (".txt", ".md"):        # 纯文本无需提取，直接读
                text = path.read_text(encoding="utf-8", errors="ignore")
            else:
                # PDF 可能是扫描件；把 OCR 适配器传入统一提取器，先走文本层，
                # 无文本层时再走本地 OCR。独立文件与案件文件共用这一入口。
                ocr = _get_ocr() if ext in (_DESENS_IMG | {".pdf"}) else None
                text = to_markdown(str(path), ocr_engine=ocr)
        except Exception as e:
            return jsonify({"ok": False, "error": f"文本提取失败：{_friendly_desensitize_error(e)}"})
    if not (text or "").strip():
        return jsonify({"ok": True, "text": "", "spans": [], "ner": False,
                        "msg": "未提取到文本内容。该文件可能是扫描件、旧版格式或内容位于文本框/图片中，请确认文件可读取，或先转为 PDF/DOCX 后重试。",
                        "source_name": source_name})
    registry_key = key or (f"_file:{data.get('selection_id')}"
                           if data.get("selection_id") else "_paste")
    reg = _get_registry(registry_key)
    ner = _get_ner()
    policy, policy_error = _resolve_workbench_categories(data)
    if policy_error:
        return jsonify({"ok": False, "error": policy_error})
    try:
        spans = analyze_text(text, reg, ner=ner, cats=policy)
    except Exception:
        ner = None
        try:
            spans = analyze_text(text, reg, ner=None, cats=policy)   # NER 失败→纯正则兜底
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "text": text, "spans": spans,
                    "ner": ner is not None, "count": len(spans),
                    "source_name": source_name})


@app.route("/api/desensitize/apply", methods=["POST"])
def desensitize_wb_apply():
    """按人工复核后的 span 清单脱敏；用户明确选择工作区后才写盘。"""
    if not _DESENS_OK:
        return jsonify({"ok": False, "error": "脱敏引擎未就绪：" + _DESENS_ERR})
    data = request.json or {}
    key = data.get("case", "")
    text = data.get("text", "")
    spans = data.get("spans", [])
    mode = data.get("mode", "alias")
    if mode not in ("alias", "blackout", "asterisk"):
        mode = "alias"
    registry_key = key or (f"_file:{data.get('selection_id')}"
                           if data.get("selection_id") else "_paste")
    reg = _get_registry(registry_key)
    try:
        masked, applied = apply_review(text, spans, reg, mode=mode)
    except Exception as e:
        return jsonify({"ok": False, "error": f"脱敏失败：{type(e).__name__}: {e}"})
    saved = ""
    if data.get("save"):
        out_root, output_error = _get_desens_output(data.get("output_id", ""))
        if output_error:
            return jsonify({"ok": False, "error": f"无法导出：{output_error}"})

        source_name = data.get("source_name", "")
        if data.get("selection_id") or data.get("rel"):
            _case, source_path, source_error = _resolve_desens_source(data)
            if source_error:
                return jsonify({"ok": False, "error": f"无法导出：{source_error}"})
            source_name = source_path.name
        try:
            dst = _next_desens_output_path(out_root, source_name)
            dst.write_text(masked, encoding="utf-8")
        except (OSError, SafetyError) as exc:
            return jsonify({"ok": False, "error": f"写入脱敏工作区失败：{exc}"})
        saved = str(dst)
    pending = [{"alias": e.alias, "canonical": e.canonical, "category": e.category}
               for e in reg.pending_review()]
    return jsonify({"ok": True, "masked": masked, "applied": applied,
                    "saved": saved, "mode": mode, "pending": pending})


@app.route("/api/desensitize/reset", methods=["POST"])
def desensitize_wb_reset():
    """清空某案的会话级 Registry（重新开始，化名重新编号）。"""
    data = request.json or {}
    key = data.get("case", "")
    if not key and data.get("selection_id"):
        key = f"_file:{data['selection_id']}"
    key = key or "_paste"
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


def _staging_root() -> Path:
    return Path(_paths()["inbox"]).expanduser().resolve() / ".loci-staging"


def _staging_batch_path(batch_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", str(batch_id or "")):
        raise SafetyError("暂存批次编号无效")
    root = _staging_root()
    candidate = (root / str(batch_id)).resolve(strict=False)
    if candidate.parent != root or candidate == root:
        raise SafetyError("暂存批次路径无效")
    return candidate


def _staging_files(batch: Path) -> list[Path]:
    if batch.is_symlink() or not batch.is_dir():
        return []
    result = []
    for path in sorted(batch.iterdir(), key=lambda item: item.name.lower()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in {".part", ".tmp"}:
            continue
        result.append(path)
    return result


@app.route("/api/inbox/staging")
def staging_list():
    root = _staging_root()
    items = []
    if root.exists() and not root.is_symlink() and root.is_dir():
        for batch in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            files = _staging_files(batch)
            if not files:
                continue
            for path in files:
                stat = path.stat()
                items.append({
                    "batch_id": batch.name,
                    "name": path.name,
                    "path": str(path),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%m-%d %H:%M"),
                    "sha256": sha256_file(path),
                })
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/inbox/staging/file")
def staging_file():
    try:
        path = existing_regular_file(request.args.get("path", ""), [_staging_root()])
        if (path.name.startswith(".")
                or path.suffix.lower() in {".part", ".tmp"}):
            raise SafetyError("暂存文件无效")
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "暂存文件不存在"}), 404
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    response = send_file(str(path), as_attachment=False, download_name=path.name,
                         max_age=0, conditional=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/inbox/staging/discard", methods=["POST"])
def staging_discard():
    batch_id = (request.json or {}).get("batch_id", "")
    try:
        batch = _staging_batch_path(batch_id)
        root = _staging_root()
        if batch.is_symlink():
            return jsonify({"ok": False, "error": "拒绝删除符号链接暂存批次"}), 400
        if not batch.exists():
            return jsonify({"ok": True, "count": 0})
        if batch.parent != root or not batch.is_dir():
            return jsonify({"ok": False, "error": "暂存批次路径无效"}), 400
        for path in batch.iterdir():
            if path.is_symlink() or not path.is_file():
                return jsonify({"ok": False, "error": "暂存批次含有非普通文件，拒绝删除"}), 400
        shutil.rmtree(str(batch))
        return jsonify({"ok": True, "count": 1})
    except (SafetyError, OSError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"放弃暂存失败：{exc}"}), 400

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
              "party_third": c.get("party_third", []),
              "party_court": c.get("party_court", [])}
             for c in cfg.get("cases", []) if c.get("status") != "closed"]
    targets, seen = [], set()
    for r in cfg.get("rules", []):
        t = r.get("target", "")
        if t and t not in seen:
            seen.add(t)
            targets.append(t)
    return jsonify({"cases": cases, "targets": sorted(targets)})


def _apply_decision_stage(decision: dict, cfg: dict, content: str, manual: dict) -> dict:
    """Make a decision-card target match the physical stage-aware layout."""
    destination = decision.get("destination") or {}
    target = (destination.get("relative") or "").strip()
    if not target or target.startswith("_"):
        return decision
    matter = (decision.get("matter") or {}).get("selected") or {}
    case = next((item for item in cfg.get("cases", [])
                 if item.get("key") == matter.get("key")), None)
    if not case:
        return decision
    try:
        from auto_filer.auto_filer import _apply_stage
        override_stage = manual.get("stage") or case.get("stage")
        mapped = _apply_stage(case, target, content or "", override_stage)
        if mapped:
            destination["relative"] = mapped
        pending = destination.get("pending_target")
        if pending and not str(pending).startswith("_"):
            destination["pending_target"] = _apply_stage(
                case, pending, content or "", override_stage)
        decision["destination"] = destination
    except (ImportError, OSError, TypeError, ValueError):
        # A decision preview remains inspectable if the legacy mapping module
        # is unavailable; the commit path will still validate the target.
        pass
    return decision


@app.route("/api/decision/preview", methods=["POST"])
@app.route("/api/decisions/preview", methods=["POST"])
def decision_preview():
    """Return a side-effect-free filing decision for the upload wizard."""
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少文件名"}), 400
    cfg = _config()
    manual = {
        key: data.get(key) for key in ("case_key", "target", "source", "doc_type", "stage")
        if data.get(key)
    }
    try:
        decision = build_filing_decision(
            filename, data.get("text") or "", cfg.get("cases", []), cfg.get("rules", []),
            extracted_title=data.get("title"), manual=manual,
            received_at=data.get("received_at"),
        )
        decision = _apply_decision_stage(decision, cfg, data.get("text") or "", manual)
        if data.get("sha256"):
            decision["file"]["sha256"] = str(data["sha256"])
        decision = _decision_store().save(decision)
        return jsonify({"ok": True, "decision": decision})
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/decision/analyze-file", methods=["POST"])
def decision_analyze_file():
    """Extract text locally and return a decision preview without saving the file."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "缺少待分析文件"}), 400
    try:
        manual = json.loads(request.form.get("meta", "{}") or "{}")
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "预填信息格式无效"}), 400
    try:
        try:
            from auto_filer import extract_markdown, extract_title_from_markdown, SmartNamer
        except ImportError:
            from auto_filer.auto_filer import extract_markdown, extract_title_from_markdown, SmartNamer
        safe_name = validate_basename(upload.filename, "文件名")
        with tempfile.TemporaryDirectory(prefix="loci-analyze-") as tmp:
            path = Path(tmp) / safe_name
            upload.save(str(path))
            text = extract_markdown(path, max_chars=8000, tail_pages=2)
            title = (extract_title_from_markdown(text)
                     or SmartNamer.extract_title(text)
                     or SmartNamer.extract_heading(text))
            file_hash = sha256_file(path)
        cfg = _config()
        decision = build_filing_decision(
            safe_name, text, cfg.get("cases", []), cfg.get("rules", []),
            extracted_title=title, manual=manual,
            received_at=manual.get("date"),
        )
        decision = _apply_decision_stage(decision, cfg, text, manual)
        decision["file"]["content_chars"] = len(text)
        decision["file"]["extracted_title"] = title or ""
        decision["file"]["ocr_or_text"] = bool(text.strip())
        # The decision card is local-only and needs a bounded evidence view.
        # Keep the preview small; the full source remains on disk and is
        # never returned by the API.
        decision["file"]["text_preview"] = text[:4000]
        decision["file"]["sha256"] = file_hash
        decision = _decision_store().save(decision)
        return jsonify({"ok": True, "decision": decision})
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"本地分析失败：{exc}"}), 500


_UPLOAD_TEMP_PREFIX = ".loci-upload-"
_UPLOAD_TEMP_SUFFIX = ".part"
_UPLOAD_PRIVATE_MODE = 0o600


def _write_upload_sidecar(path: Path, metadata: dict) -> None:
    """Write a complete private sidecar to an exclusive hidden temp path."""
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        _UPLOAD_PRIVATE_MODE,
    )
    try:
        os.fchmod(fd, _UPLOAD_PRIVATE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(metadata, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), _UPLOAD_PRIVATE_MODE)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _upload_destination_candidates(inbox: Path, filename: str, reserved: set[str]):
    """Yield non-existing file/sidecar pairs using the legacy suffix policy."""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(0, 100000):
        candidate_name = filename if index == 0 else f"{stem}_{index}{suffix}"
        if candidate_name in reserved:
            continue
        candidate = inbox / candidate_name
        sidecar = inbox / f".{candidate_name}.loci.json"
        # lexists also treats a dangling symlink as occupied.  The final
        # publication below repeats this check atomically with os.link().
        if os.path.lexists(str(candidate)) or os.path.lexists(str(sidecar)):
            continue
        yield candidate_name, candidate, sidecar
    raise SafetyError("无法为上传文件分配不冲突的文件名")


def _same_upload_inode(path: Path, owner: Path) -> bool:
    """Return whether ``path`` is still the inode created for this upload."""
    try:
        actual = os.lstat(str(path))
        expected = os.lstat(str(owner))
    except (FileNotFoundError, OSError):
        return False
    return (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)


def _unlink_owned_upload(path: Path, owner: Path) -> None:
    """Remove only a published link still backed by this request's temp inode."""
    if not _same_upload_inode(path, owner):
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _cleanup_upload_transaction(temp_files, temp_sidecars, published):
    """Best-effort rollback limited to paths/inodes created by this request."""
    errors = []
    for record in reversed(published):
        if record.get("file_published"):
            try:
                _unlink_owned_upload(record["final_file"], record["temp_file"])
            except Exception as exc:
                errors.append(f"清理最终文件失败：{type(exc).__name__}: {exc}")
        if record.get("sidecar_published"):
            try:
                _unlink_owned_upload(record["final_sidecar"], record["temp_sidecar"])
            except Exception as exc:
                errors.append(f"清理最终 sidecar 失败：{type(exc).__name__}: {exc}")

    for path in [*reversed(temp_sidecars), *reversed(temp_files)]:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"清理临时文件失败：{path.name}：{type(exc).__name__}: {exc}")
    return errors


def _publish_upload_pair(temp_file: Path, temp_sidecar: Path, inbox: Path,
                         original_name: str, reserved: set[str], published: list):
    """Publish one prepared upload without replacing any existing directory entry.

    Hard-linking within the inbox gives an atomic create-without-replace
    operation.  The temporary links remain until the whole request commits so
    rollback can compare inodes before unlinking anything.
    """
    for final_name, final_file, final_sidecar in _upload_destination_candidates(
            inbox, original_name, reserved):
        try:
            # The sidecar is made visible first.  It is hidden from the daemon,
            # but is already complete by the time the final file is created.
            os.link(str(temp_sidecar), str(final_sidecar))
        except FileExistsError:
            continue

        record = {
            "temp_file": temp_file,
            "temp_sidecar": temp_sidecar,
            "final_file": final_file,
            "final_sidecar": final_sidecar,
            "sidecar_published": True,
            "file_published": False,
        }
        published.append(record)
        try:
            os.link(str(temp_file), str(final_file))
        except FileExistsError:
            # A concurrent uploader won this candidate after the pre-check.
            # Remove only our sidecar link, then try the next suffix.
            _unlink_owned_upload(final_sidecar, temp_sidecar)
            published.pop()
            continue

        record["file_published"] = True
        reserved.add(final_name)
        return record

    raise SafetyError("无法为上传文件分配不冲突的文件名")


@app.route("/api/inbox/upload", methods=["POST"])
def upload_to_inbox():
    inbox_root = Path(_paths()["inbox"])
    inbox_root.mkdir(parents=True, exist_ok=True)
    stage_only = request.form.get("stage_only", "").lower() in {"1", "true", "yes"}
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "未上传任何文件"}), 400
    if len(files) > MAX_UPLOAD_FILES:
        return jsonify({"ok": False, "error": f"单次最多上传 {MAX_UPLOAD_FILES} 个文件"}), 400

    try:
        names = [validate_basename(f.filename, "上传文件名") for f in files]
    except SafetyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    def _json_field(name, default):
        raw = request.form.get(name)
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SafetyError(f"{name} 格式无效") from exc

    try:
        # batch_meta is the first-class shared context.  Legacy ``meta`` is
        # retained as a compatible alias for older clients.
        batch_raw = _json_field("batch_meta", None)
        if batch_raw is None:
            batch_raw = _json_field("meta", {})
        per_raw = _json_field("metas", [{} for _ in files])
        rel_raw = _json_field("relative_paths", names)
        if not isinstance(per_raw, list) or len(per_raw) != len(files):
            raise SafetyError("逐文件信息数量必须与文件数量一致")
        if not isinstance(rel_raw, list) or len(rel_raw) != len(files):
            raise SafetyError("相对路径数量必须与文件数量一致")

        cfg = _config()
        available_cases = [case for case in cfg.get("cases", []) if case.get("status") != "closed"]
        batch_meta = _validate_upload_meta(batch_raw, available_cases)
        relative_paths = [validate_relative_file_path(path) for path in rel_raw]

        supplied_batch_id = (request.form.get("batch_id") or "").strip()
        if supplied_batch_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", supplied_batch_id):
            raise SafetyError("批次编号格式无效")
        batch_id = supplied_batch_id or (
            datetime.now().strftime("B%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
        )
        # Phase 3 decision-card uploads stay below a hidden, batch-specific
        # directory until the frozen decision is committed.  Refuse a
        # pre-existing symlink at either level before mkdir can follow it.
        if stage_only:
            staging_root = inbox_root / ".loci-staging"
            if staging_root.is_symlink() or (staging_root.exists() and not staging_root.is_dir()):
                raise SafetyError("暂存根目录不是安全的普通文件夹")
            staging_root.mkdir(parents=True, exist_ok=True)
            if staging_root.is_symlink():
                raise SafetyError("暂存根目录不能是符号链接")
            inbox = staging_root / batch_id
            if inbox.is_symlink() or (inbox.exists() and not inbox.is_dir()):
                raise SafetyError("暂存批次目录不是安全的普通文件夹")
            inbox.mkdir(parents=True, exist_ok=True)
        else:
            inbox = inbox_root
            inbox.mkdir(parents=True, exist_ok=True)

        overrides = []
        for idx, raw in enumerate(per_raw):
            if not isinstance(raw, dict):
                raise SafetyError(f"第 {idx + 1} 个文件的投放信息格式无效")
            merged = {**batch_meta, **_present_upload_values(raw)}
            validated = _validate_upload_meta(merged, available_cases)
            rel = relative_paths[idx]
            validated["batch_id"] = batch_id
            validated["original_relative_path"] = rel
            parent = "/".join(rel.split("/")[:-1])
            if parent:
                validated["context_path"] = parent
            overrides.append(validated)
    except SafetyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    # Prepare every file and sidecar while both remain hidden.  Publishing is
    # deliberately a second phase so a later preparation failure cannot leave
    # an earlier upload visible to the daemon.
    temp_files, temp_sidecars, staged, published, saved = [], [], [], [], []
    try:
        for idx, upload in enumerate(files):
            token = uuid.uuid4().hex
            temp_file = save_upload(
                upload, inbox, f"{_UPLOAD_TEMP_PREFIX}{token}{_UPLOAD_TEMP_SUFFIX}"
            )
            temp_files.append(temp_file)
            temp_sidecar = inbox / f"{_UPLOAD_TEMP_PREFIX}{token}.loci.json.part"
            _write_upload_sidecar(temp_sidecar, overrides[idx])
            temp_sidecars.append(temp_sidecar)
            staged.append((temp_file, temp_sidecar, idx))

        reserved = set()
        for temp_file, temp_sidecar, idx in staged:
            record = _publish_upload_pair(
                temp_file, temp_sidecar, inbox, names[idx], reserved, published
            )
            saved.append({
                "name": record["final_file"].name,
                "path": str(record["final_file"]),
                "original_relative_path": overrides[idx]["original_relative_path"],
            })

        # Keep the final hard links, not the hidden staging names, after the
        # request commits.  Publication is already complete at this point;
        # failure to remove a hidden staging link must not turn a successful
        # batch into a rollback attempt that could leave published material
        # behind.  The daemon ignores these hidden names, so retaining one is
        # safe and recoverable for a later cleanup pass.
        for path in [*reversed(temp_sidecars), *reversed(temp_files)]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as exc:
        cleanup_errors = _cleanup_upload_transaction(
            temp_files, temp_sidecars, published
        )
        error = f"上传失败，已回滚本批次：{exc}"
        if cleanup_errors:
            error += "；" + "；".join(cleanup_errors)
        return jsonify({"ok": False, "error": error}), 500

    if stage_only:
        # The server owns the batch identity after publication.  The sidecar
        # contents are snapshotted into the private state/ledger layer before
        # any commit can delete them, so a later undo can restore the user's
        # intake choices without trusting the browser.
        try:
            store = _decision_store()
            store.register_batch(
                batch_id, expected_count=len(saved),
                metadata={"stage_only": True, "source": "loci-upload"},
            )
            batch_items = []
            for idx, item in enumerate(saved):
                source = Path(item["path"]).expanduser().resolve(strict=False)
                batch_items.append({
                    "source_path": str(source),
                    "status": "staged",
                    "metadata": overrides[idx],
                    "sidecars": _filing_ledger().snapshot_sidecars(source),
                })
            store.register_batch_items(batch_id, batch_items)
            store.update_batch(batch_id, status="ready")
        except (DecisionStoreError, LedgerError, OSError, TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"暂存批次登记失败：{exc}"}), 500

    return jsonify({"ok": True, "saved": saved, "count": len(saved),
                    "batch_id": batch_id, "staged": stage_only})

@app.route("/api/cases/open", methods=["POST"])
def open_case_folder():
    key   = (request.json or {}).get("key", "")
    cfg   = _config()
    paths = _paths()
    case  = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"})
    folder = _case_path(case, paths)
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
    folder = _case_path(case, paths)
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
    root = _case_path(case, paths)
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


def _filing_ledger():
    return FilingLedger(Path(_paths()["logs"]) / "filing_ledger.sqlite3")


def _decision_store():
    logs = Path(_paths()["logs"]).expanduser().resolve()
    return DecisionStore(logs.parent / "state" / "loci.db")


def _record_app_move(source: Path, destination: Path, *, action: str,
                     metadata: dict) -> str:
    """Record a manual move or put it back if the ledger cannot commit."""
    try:
        return _filing_ledger().record_move(source, destination, action=action,
                                            metadata=metadata)
    except Exception as exc:
        try:
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(str(destination), str(source))
        except Exception:
            pass
        raise LedgerError(f"事务账本写入失败，已尝试回滚：{exc}") from exc


def _commit_app_move(source: Path, destination: Path, *, action: str,
                     metadata: dict) -> str:
    """The single UI/API file-move boundary: move, then ledger, or rollback."""
    if os.path.lexists(str(destination)):
        raise LedgerError("destination_conflict")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        raise LedgerError(f"文件移动失败：{exc}") from exc
    return _record_app_move(source, destination, action=action, metadata=metadata)


def _normalize_commit_source(raw_path, cfg, paths) -> Path:
    """Resolve one visible source while keeping the existing workspace fence."""
    src = existing_regular_file(raw_path, _case_roots(cfg, paths))
    if (src.name.startswith(".") or src.suffix.lower() in {".part", ".tmp"}):
        raise SafetyError("不能提交隐藏 sidecar 或临时文件")
    return src


def _remove_intake_sidecars(source: Path) -> None:
    """Remove only the two sidecars owned by this intake source."""
    for sidecar in (
        source.parent / f".{source.name}.loci.json",
        source.parent / f".{source.name}.pending.json",
    ):
        try:
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
        except OSError:
            # The transaction ledger already contains a private backup.  A
            # failed cleanup must not make the visible document disappear.
            pass


def _cleanup_staging_batch(batch_id: str) -> None:
    """Remove an empty staging directory after a fully committed batch."""
    try:
        batch = _staging_batch_path(batch_id)
        root = _staging_root()
        if batch.parent == root and batch.is_dir() and not any(batch.iterdir()):
            batch.rmdir()
    except (OSError, SafetyError, TypeError, ValueError):
        pass


def _prepare_commit_record(decision: dict, raw_path, data: dict,
                           cfg: dict, paths: dict) -> dict:
    """Validate a frozen decision without changing any filesystem state."""
    if not decision:
        raise DecisionStoreError("冻结决策不存在")
    if decision.get("stored_status") == "committed":
        raise DecisionStoreError("该决策已经提交")
    if decision.get("risk") != "green" or decision.get("status") != "suggested":
        raise DecisionStoreError("决策仍需人工复核，不能直接提交")

    src = _normalize_commit_source(raw_path, cfg, paths)
    expected_hash = ((decision.get("file") or {}).get("sha256") or "").strip()
    if not expected_hash:
        raise DecisionStoreError("冻结决策缺少源文件哈希，请重新分析")
    if sha256_file(src) != expected_hash:
        raise DecisionStoreError("源文件内容已变化，请重新分析")

    matter = (decision.get("matter") or {}).get("selected") or {}
    case = next((item for item in cfg.get("cases", [])
                 if item.get("key") == matter.get("key")), None)
    destination = decision.get("destination") or {}
    target = (destination.get("relative") or "").strip()
    requested_target = (data.get("target") or "").strip()
    if requested_target and requested_target != target:
        raise DecisionStoreError("目标目录已冻结，请重新分析后提交")
    if destination.get("health") not in {"ok", "unknown"}:
        raise DecisionStoreError("path_unavailable")
    if (not case or not target or target.startswith("_")
            or destination.get("source_required")):
        raise DecisionStoreError("案件、来源或目标目录仍需人工确认")

    target = validate_relative_target(target)
    dest_dir = _safe_case_dest(case, paths, target)
    default_name = ((decision.get("naming") or {}).get("preview") or src.name)
    requested_name = (data.get("new_name") or "").strip()
    if requested_name and requested_name != Path(default_name).name:
        raise DecisionStoreError("文件名已冻结，请重新分析后提交")
    new_name = validate_basename(Path(default_name).name, "新文件名")
    dest = dest_dir / new_name
    if os.path.lexists(str(dest)):
        raise LedgerError("destination_conflict")

    # This must happen before the move: the source sidecars are deleted after
    # a successful commit, but undo must be independent of their presence.
    sidecars = _filing_ledger().snapshot_sidecars(src)
    return {
        "decision": decision,
        "source": src,
        "destination": dest,
        "case": case,
        "target": target,
        "sidecars": sidecars,
        "previous_status": decision.get("stored_status") or "suggested",
    }


def _batch_error(message: str, *, batch_id: str, phase: str,
                 committed_count: int = 0, status: int = 409,
                 can_undo: bool = False, rolled_back: bool = False):
    return jsonify({"ok": False, "error": message, "batch_id": batch_id,
                    "phase": phase, "committed_count": committed_count,
                    "can_undo": can_undo, "rolled_back": rolled_back}), status


def _bind_batch_decisions(store, batch_id: str, request_items: list,
                          batch_items: list) -> list[dict]:
    """Bind frozen decisions to server-registered staged source paths."""
    existing = {
        str(Path(item["source_path"]).expanduser().resolve(strict=False)): item
        for item in batch_items
    }
    if not request_items or not isinstance(request_items, list):
        raise DecisionStoreError("批次条目不能为空")
    bound = []
    seen = set()
    for raw in request_items:
        if not isinstance(raw, dict):
            raise DecisionStoreError("批次条目格式无效")
        decision_id = str(raw.get("decision_id") or "").strip()
        if not decision_id:
            raise DecisionStoreError("批次条目缺少冻结决策")
        source = str(Path(raw.get("path") or "").expanduser().resolve(strict=False))
        if source in seen:
            raise DecisionStoreError("批次含有重复源文件")
        seen.add(source)
        item = existing.get(source)
        if not item:
            raise DecisionStoreError("源文件不属于该服务端批次")
        if item.get("decision_id") and item.get("decision_id") != decision_id:
            raise DecisionStoreError("批次源文件已绑定其它冻结决策")
        metadata = item.get("metadata_fields") or {}
        metadata = {**metadata, "pre_commit_status": metadata.get("pre_commit_status", "suggested")}
        store.register_batch_items(batch_id, [{
            "source_path": source, "decision_id": decision_id,
            "destination_path": item.get("destination_path"),
            "status": item.get("status") or "ready",
            "sidecars": item.get("sidecars_fields") or [],
            "metadata": metadata,
        }])
        bound.append(source)
    return [item for item in store.list_batch_items(batch_id)
            if str(Path(item["source_path"]).expanduser().resolve(strict=False)) in seen]


def _commit_batch_internal(batch_id: str, request_items: list,
                           *, require_all: bool = True,
                           allow_create: bool = False):
    """Preflight and commit a whole decision batch as one state transition."""
    cfg, paths = _config(), _paths()
    store = _decision_store()
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", batch_id):
        return _batch_error("批次编号格式无效", batch_id=str(batch_id or ""), phase="preflight")
    try:
        batch = store.get_batch(batch_id)
        if not batch and allow_create:
            batch = store.register_batch(batch_id, expected_count=len(request_items),
                                         metadata={"source": "legacy-single-commit"})
            store.register_batch_items(batch_id, [{
                "source_path": str(Path(item.get("path") or "").expanduser().resolve(strict=False)),
                "decision_id": item.get("decision_id"), "status": "ready",
            } for item in request_items])
            batch = store.get_batch(batch_id)
        if not batch:
            return _batch_error("服务端批次不存在，请重新上传并分析", batch_id=batch_id,
                                phase="preflight", status=404)
        if batch.get("status") in {"committed", "undone", "needs_recovery", "committing"}:
            return _batch_error("批次当前状态不允许提交：" + str(batch.get("status")),
                                batch_id=batch_id, phase="preflight")
        registered = store.list_batch_items(batch_id)
        if require_all and len(request_items) != len(registered):
            return _batch_error("批次材料未完整提交，未移动任何文件", batch_id=batch_id,
                                phase="preflight")
        if not require_all:
            # Older clients staged files before the server batch registry was
            # introduced and then called the single-file endpoint repeatedly.
            # Bind such a source lazily, while still recording it server-side.
            known_sources = {
                str(Path(item["source_path"]).expanduser().resolve(strict=False))
                for item in registered
            }
            late_items = []
            for raw in request_items:
                source = _normalize_commit_source(raw.get("path"), cfg, paths)
                source_key = str(source)
                if source_key not in known_sources:
                    late_items.append({
                        "source_path": source_key,
                        "decision_id": raw.get("decision_id"),
                        "status": "ready",
                        "metadata": {"pre_commit_status": "suggested"},
                        "sidecars": _filing_ledger().snapshot_sidecars(source),
                    })
            if late_items:
                store.register_batch_items(batch_id, late_items)
                registered = store.list_batch_items(batch_id)
        bound = _bind_batch_decisions(store, batch_id, request_items, registered)
        if require_all and len(bound) != len(registered):
            return _batch_error("批次冻结决策数量不完整，未移动任何文件", batch_id=batch_id,
                                phase="preflight")
        request_by_source = {
            str(Path(item.get("path") or "").expanduser().resolve(strict=False)): item
            for item in request_items
        }
        records = []
        destinations = set()
        for item in bound:
            raw = request_by_source.get(str(Path(item["source_path"]).expanduser().resolve(strict=False)))
            if not raw:
                continue
            decision = store.get(raw.get("decision_id"))
            record = _prepare_commit_record(decision, raw.get("path"), raw, cfg, paths)
            destination_key = str(record["destination"].resolve(strict=False))
            if destination_key in destinations:
                raise LedgerError("批次内存在重复目标文件")
            destinations.add(destination_key)
            records.append(record)
        if require_all and len(records) != len(registered):
            raise DecisionStoreError("批次条目未全部绑定冻结决策")
        if not records:
            raise DecisionStoreError("批次没有可提交的材料")

        store.update_batch(batch_id, status="committing", error=None)
        committed = []
        try:
            for record in records:
                txid = _commit_app_move(
                    record["source"], record["destination"], action="decision_commit",
                    metadata={"decision_id": record["decision"]["decision_id"],
                              "batch_id": batch_id,
                              "case_key": (record["case"].get("key") or ""),
                              "target": record["target"],
                              "sidecars": record["sidecars"]},
                )
                record["transaction_id"] = txid
                committed.append(record)

            for record in committed:
                store.mark_committed(record["decision"]["decision_id"])
            for record in committed:
                store.update_batch_item(
                    batch_id, source_path=str(record["source"]), status="committed",
                    destination_path=str(record["destination"]), error=None,
                )
                _remove_intake_sidecars(record["source"])
                _archive_log_append({
                    "action": "decision_commit", "transaction_id": record["transaction_id"],
                    "decision_id": record["decision"]["decision_id"],
                    "batch_id": batch_id, "from": str(record["source"]),
                    "to": str(record["destination"]),
                    "case_key": record["case"].get("key"), "target": record["target"],
                })
            # The legacy single-file endpoint may be called repeatedly by old
            # clients for one batch.  Keep that server batch open so the next
            # single commit can bind another staged source; the new batch API
            # is the only path that closes the batch as a whole.
            status = "committed" if require_all else "ready"
            store.update_batch(batch_id, status=status, error=None)
            _cleanup_staging_batch(batch_id)
            response = {"ok": True, "batch_id": batch_id,
                        "status": status, "committed_count": len(committed),
                        "items": [{"decision_id": r["decision"]["decision_id"],
                                   "transaction_id": r["transaction_id"],
                                   "to": str(r["destination"])} for r in committed]}
            if len(committed) == 1:
                response["transaction_id"] = committed[0]["transaction_id"]
                response["to"] = str(committed[0]["destination"])
            return jsonify(response)
        except (LedgerError, DecisionStoreError, OSError) as exc:
            rollback_error = None
            if committed:
                try:
                    _filing_ledger().undo_batch(batch_id, allowed_roots=_case_roots(cfg, paths))
                except Exception as rollback_exc:
                    rollback_error = str(rollback_exc)
            for record in committed:
                try:
                    store.restore_status(record["decision"]["decision_id"],
                                         record["previous_status"])
                    store.update_batch_item(
                        batch_id, source_path=str(record["source"]),
                        status="rolled_back" if not rollback_error else "needs_recovery",
                        error=rollback_error,
                    )
                except (DecisionStoreError, OSError):
                    rollback_error = rollback_error or "无法恢复冻结决策状态"
            state = "needs_recovery" if rollback_error else "failed"
            try:
                store.update_batch(batch_id, status=state, error=str(exc))
            except DecisionStoreError:
                state = "needs_recovery"
            return _batch_error(str(exc), batch_id=batch_id,
                                phase="commit" if not rollback_error else "rollback",
                                committed_count=len(committed),
                                can_undo=bool(rollback_error),
                                rolled_back=not bool(rollback_error))
    except (DecisionStoreError, LedgerError, SafetyError, OSError, TypeError, ValueError) as exc:
        try:
            store.update_batch(batch_id, status="failed", error=str(exc))
        except Exception:
            pass
        return _batch_error(str(exc), batch_id=batch_id, phase="preflight")


@app.route("/api/decisions/batch/commit", methods=["POST"])
def commit_decision_batch():
    data = request.json or {}
    batch_id = str(data.get("batch_id") or "").strip()
    items = data.get("items")
    return _commit_batch_internal(batch_id, items, require_all=True, allow_create=False)


@app.route("/api/decisions/<decision_id>/commit", methods=["POST"])
def commit_decision(decision_id):
    data = request.json or {}
    batch_id = str(data.get("batch_id") or "").strip()
    if not batch_id:
        batch_id = "SINGLE-" + uuid.uuid4().hex[:24]
    return _commit_batch_internal(
        batch_id, [{"decision_id": decision_id, "path": data.get("path"),
                    "target": data.get("target"), "new_name": data.get("new_name")}],
        require_all=False, allow_create=True,
    )

@app.route("/api/review")
def review_queue():
    """待办复核：待确认来源 + 未识别案件(_未分类) + 低置信命名的文件。"""
    cfg = _config()
    paths = _paths()
    items, seen = [], set()
    # 1) 待确认来源（各案件 _待确认来源 下，需人工选我方/对方）
    for c in cfg.get("cases", []):
        root = _case_path(c, paths) / "_待确认来源"
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


def _is_review_queue_file(path: Path, cfg: dict, paths: dict) -> bool:
    """Keep the review reader scoped to files currently shown in the queue."""
    uncat = (Path(paths["cases"]) / "_未分类").expanduser().resolve()
    if is_relative_to(path, uncat):
        return True
    for root in _pending_roots(cfg, paths):
        if is_relative_to(path, root):
            return True
    log = Path(paths["logs"]) / "archive_log.jsonl"
    if not log.exists():
        return False
    try:
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines()[-800:]:
            entry = json.loads(line)
            candidate = entry.get("to")
            if (entry.get("dt_conf", 1.0) < 0.5 and candidate
                    and Path(candidate).expanduser().resolve(strict=False) == path):
                return True
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return False
    return False


@app.route("/api/review/file")
def review_file():
    """Read a review-queue file so the decision card can analyze it locally.

    The browser receives the bytes only for the current local review action;
    the endpoint never returns extracted text or a directory listing.  Hidden
    upload sidecars, temporary files, and the decision-card staging area are
    deliberately excluded even though the broader case roots are readable.
    """
    cfg, paths = _config(), _paths()
    try:
        path = existing_regular_file(request.args.get("path", ""),
                                     _case_roots(cfg, paths))
        if (path.name.startswith(".")
                or path.suffix.lower() in {".part", ".tmp"}
                or ".loci-staging" in path.parts):
            raise SafetyError("复核文件无效")
        if not _is_review_queue_file(path, cfg, paths):
            raise SafetyError("文件当前不在待复核队列中")
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "复核文件不存在（可能已被移动）"}), 404
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    response = send_file(str(path), as_attachment=False, download_name=path.name,
                         max_age=0, conditional=False)
    response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/api/review/reclassify", methods=["POST"])
def review_reclassify():
    data = request.json or {}
    cfg, paths = _config(), _paths()
    case = next((c for c in cfg.get("cases", []) if c.get("key") == data.get("case_key")), None)
    if not case:
        return jsonify({"ok": False, "error": "请选择案件"})
    try:
        src = existing_regular_file(data.get("path", ""), _case_roots(cfg, paths))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "文件不存在（可能已被移动）"}), 404
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if (src.name.startswith(".") or src.suffix.lower() in {".part", ".tmp"}
            or ".loci-staging" in src.parts):
        return jsonify({"ok": False, "error": "不能直接重新归类隐藏 sidecar、临时文件或 staging 文件"}), 400
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify({"ok": False, "error": "请选择目标文件夹"})
    try:
        target = validate_relative_target(target)
        new_name = validate_basename((data.get("new_name") or src.name).strip(), "新文件名")
        dest_dir = _safe_case_dest(case, paths, target)
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    dest = dest_dir / new_name
    if os.path.lexists(str(dest)):
        return jsonify({"ok": False, "error": "destination_conflict",
                        "destination": str(dest),
                        "message": "目标文件已存在，请选择组次、版本或保留副本"}), 409
    try:
        txid = _commit_app_move(
            src, dest, action="reclassify",
            metadata={"case_key": case["key"], "manual": True},
        )
    except LedgerError as exc:
        status = 409 if str(exc) == "destination_conflict" else 500
        return jsonify({"ok": False, "error": str(exc)}), status
    _archive_log_append({"action": "reclassify", "transaction_id": txid,
                         "original_name": src.name,
                         "from": str(src), "new_name": dest.name, "to": str(dest),
                         "case": case["name"], "case_key": case["key"],
                         "target": target, "manual": True, "dt_conf": 1.0,
                         "dt_src": "manual", "needs_review": False})
    return jsonify({"ok": True, "to": str(dest)})


@app.route("/api/transactions")
def filing_transactions():
    try:
        raw_undoable = (request.args.get("undoable") or "").lower()
        undoable = raw_undoable in {"1", "true", "yes"}
        batch_id = request.args.get("batch_id") or None
        return jsonify({"transactions": _filing_ledger().list_transactions(
            limit=100, undoable=undoable, batch_id=batch_id)})
    except Exception as exc:
        return jsonify({"transactions": [], "error": str(exc)}), 500


@app.route("/api/transactions/<txid>/undo", methods=["POST"])
def undo_filing_transaction(txid):
    cfg, paths = _config(), _paths()
    try:
        allowed = _case_roots(cfg, paths)
        ledger = _filing_ledger()
        try:
            transactions = ledger.list_transactions(limit=10000, undoable=True)
        except AttributeError:
            # Keep the small legacy ledger adapter used by integrations/tests
            # compatible with the richer decision-aware ledger.
            transactions = []
        current = next((item for item in transactions if item.get("id") == txid), None)
        result = ledger.undo(txid, allowed_roots=allowed)
        if current and current.get("decision_id"):
            try:
                store = _decision_store()
                batch_id = current.get("batch_id")
                batch_item = (store.find_batch_item(batch_id, decision_id=current["decision_id"])
                              if batch_id else None)
                old_status = ((batch_item or {}).get("metadata_fields") or {}).get(
                    "pre_commit_status") or "suggested"
                store.restore_status(current["decision_id"], old_status)
                if batch_item:
                    store.update_batch_item(batch_id, decision_id=current["decision_id"],
                                            status="undone", error=None)
            except DecisionStoreError as exc:
                return jsonify({"ok": False, "error": f"文件已恢复，但冻结决策状态恢复失败：{exc}"}), 500
        _archive_log_append({"action": "undo", "transaction_id": txid,
                             "needs_review": True})
        return jsonify({"ok": True, **result})
    except LedgerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/transactions/batches/<batch_id>/undo", methods=["POST"])
def undo_filing_batch(batch_id):
    cfg, paths = _config(), _paths()
    try:
        store = _decision_store()
        batch = None
        try:
            batch = store.get_batch(batch_id)
        except DecisionStoreError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if batch and batch.get("status") == "undone":
            return jsonify({"ok": False, "error": "批次已经撤销"}), 409
        transaction_rows = _filing_ledger().list_transactions(
            limit=10000, undoable=True, batch_id=batch_id
        )
        if batch and batch.get("status") not in {"committed", "failed", "needs_recovery", "ready"}:
            return jsonify({"ok": False, "error": "批次当前状态不允许撤销"}), 409
        # Legacy ledger-only batches remain undoable for backward compatibility,
        # but an unknown batch with no ledger entries can never target another
        # batch accidentally.
        if not batch and not transaction_rows:
            return jsonify({"ok": False, "error": "可撤销批次不存在"}), 409
        result = _filing_ledger().undo_batch(
            batch_id, allowed_roots=_case_roots(cfg, paths)
        )
        restore_errors = []
        for transaction in transaction_rows:
            decision_id = transaction.get("decision_id")
            if not decision_id:
                continue
            try:
                decision = store.get(decision_id)
                metadata = (decision or {}).get("metadata_fields") or {}
                batch_item = store.find_batch_item(batch_id, decision_id=decision_id) if batch else None
                item_metadata = (batch_item or {}).get("metadata_fields") or {}
                old_status = item_metadata.get("pre_commit_status") or metadata.get("pre_commit_status") or "suggested"
                store.restore_status(decision_id, old_status)
                if batch_item:
                    store.update_batch_item(batch_id, decision_id=decision_id,
                                            status="undone", error=None)
            except DecisionStoreError as exc:
                restore_errors.append(f"{decision_id}: {exc}")
        if batch:
            store.update_batch(batch_id,
                               status="undone" if not restore_errors else "needs_recovery",
                               error="；".join(restore_errors) if restore_errors else None)
        if restore_errors:
            return jsonify({"ok": False, "error": "文件已恢复，但决策状态恢复失败",
                            "details": restore_errors, **result}), 500
        _archive_log_append({"action": "undo_batch", "batch_id": batch_id,
                             "needs_review": True})
        return jsonify({"ok": True, **result})
    except LedgerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

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
        [sys.executable, str(AF / "auto_filer.py"), "--once"],
        cwd=str(WS), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return jsonify({"ok": True, "moved": moved,
                    "msg": f"已移回收件箱 {moved} 个文件，正在后台重新归类…"})

_CONFIRM_SOURCES = frozenset({
    "court", "tribunal", "ours", "opposing", "third_party", "expert",
    "police", "prosecutor", "regulator", "internal",
})
_LEGACY_SIDE_SOURCES = {"我方证据": "ours", "对方证据": "opposing"}
_SIDE_TARGETS = {"ours": "我方证据", "opposing": "对方证据"}
_US_PARTY_TARGETS = {
    "ours": "04_Our Filings",
    "opposing": "03_Opposing Party Filings",
}


@app.route("/api/review/confirm_source", methods=["POST"])
def confirm_source():
    """Confirm a source and move a pending file through the validated ledger path."""
    data = request.json or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "请求格式无效"}), 400

    raw_source = data.get("source")
    source = str(raw_source).strip().lower() if raw_source is not None else ""
    if not source:
        source = _LEGACY_SIDE_SOURCES.get(data.get("side", ""), "")
    if source not in _CONFIRM_SOURCES:
        return jsonify({
            "ok": False,
            "error": "来源无效，必须是 court/tribunal/ours/opposing/third_party/"
                     "expert/police/prosecutor/regulator/internal，或使用 legacy side",
        }), 400

    cfg, paths = _config(), _paths()
    try:
        src = existing_regular_file(data.get("path", ""), _pending_roots(cfg, paths))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "文件不存在（可能已被移动）"}), 404
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    pend = src.parent / f".{src.name}.pending.json"
    info = {}
    if pend.is_symlink():
        return jsonify({"ok": False, "error": "复核元数据不能是符号链接"}), 400
    if pend.exists():
        try:
            loaded = json.loads(pend.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                info = loaded
        except Exception:
            info = {}

    pending_value = info.get("pending_target") or data.get("pending_target", "")
    if pending_value is None:
        pending_value = ""
    if not isinstance(pending_value, str):
        return jsonify({"ok": False, "error": "候选目标格式无效"}), 400
    pending_target = pending_value.strip()

    # A fixed pending target is authoritative.  Side placeholders may only be
    # resolved to ours/opposing.  Court, expert and other sources must use an
    # explicitly selected target rather than being forced into a party branch.
    if "{us_party_filings}" in pending_target:
        if source in _US_PARTY_TARGETS:
            target = pending_target.replace("{us_party_filings}",
                                            _US_PARTY_TARGETS[source])
        else:
            target = data.get("target", "")
            if not isinstance(target, str) or not target.strip():
                return jsonify({
                    "ok": False,
                    "error": "该来源不能替换美国当事人文书目录，请另选 target",
                }), 400
            target = target.strip()
    elif "{side}" in pending_target:
        if source in _SIDE_TARGETS:
            target = pending_target.replace("{side}", _SIDE_TARGETS[source])
        else:
            target = data.get("target", "")
            if not isinstance(target, str) or not target.strip():
                return jsonify({
                    "ok": False,
                    "error": "该来源不能替换 {side}，请另选 target",
                }), 400
            target = target.strip()
    else:
        target = pending_target
        if not target:
            # A pending sidecar may be absent or intentionally carry no
            # candidate; an explicit, user-selected target is still allowed.
            target = data.get("target", "")
            if not isinstance(target, str):
                return jsonify({"ok": False, "error": "目标目录格式无效"}), 400
            target = target.strip()
        if not target:
            return jsonify({"ok": False, "error": "缺少候选目标，无法确认"}), 400

    case_key = info.get("case_key") or data.get("case_key")
    case = next((c for c in cfg.get("cases", []) if c.get("key") == case_key), None)
    if not case:
        return jsonify({"ok": False, "error": "找不到对应案件"})
    try:
        target = validate_relative_target(target)
        # _safe_case_dest delegates to _case_path, including import_path.
        dest_dir = _safe_case_dest(case, paths, target)
    except (SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    dest = dest_dir / src.name
    if os.path.lexists(str(dest)):
        return jsonify({"ok": False, "error": "destination_conflict",
                        "destination": str(dest),
                        "message": "目标文件已存在，请选择组次、版本或保留副本"}), 409
    try:
        txid = _commit_app_move(
            src, dest, action="confirm_source",
            metadata={"case_key": case["key"], "source": source,
                      "manual": True},
        )
        if pend.exists():
            pend.unlink()
    except LedgerError as exc:
        status = 409 if str(exc) == "destination_conflict" else 500
        return jsonify({"ok": False, "error": str(exc)}), status
    _archive_log_append({"action": "confirm_source", "transaction_id": txid,
                         "original_name": src.name,
                         "new_name": dest.name, "to": str(dest), "case": case["name"],
                         "case_key": case["key"], "target": target,
                         "source": source,
                         "manual": True, "needs_review": False})
    return jsonify({"ok": True, "to": str(dest), "source": source})

@app.route("/api/cases/update", methods=["POST"])
def update_case():
    data     = request.json or {}
    key      = data.get("key", "").strip()
    cfg_path = AF / "config.yaml"
    def _mutate(cfg):
        cfg = cfg or {}
        case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
        if not case:
            raise KeyError(key)
        if "keywords" in data:
            case["keywords"] = data["keywords"]
        for pf in ("party_us", "party_op", "party_court", "party_third"):
            if pf in data:
                vals = [v.strip() for v in (data[pf] or []) if v and v.strip()]
                if vals:
                    case[pf] = vals
                else:
                    case.pop(pf, None)
        for field in ("case_number", "scenario", "stage", "role", "cases_dir"):
            value = (data.get(field) or "").strip()
            if value:
                case[field] = value
            else:
                case.pop(field, None)
        if "cause" in data:
            cause = (data.get("cause") or "").strip()
            if cause:
                # Keep the legacy subject field synchronized with the canonical cause.
                case["cause"] = cause
                case["subject"] = cause
            else:
                case.pop("cause", None)
                case.pop("subject", None)
        if data.get("status") == "closed":
            case["status"] = "closed"
        else:
            case.pop("status", None)
        case["loci_matter"] = canonicalize_case(case)
        return cfg
    try:
        update_yaml(cfg_path, _mutate, default={})
    except KeyError:
        return jsonify({"ok": False, "error": f"案件 {key!r} 不存在"}), 404
    return jsonify({"ok": True})


_SCENARIO_PROC_TYPE = {
    "中国民事": "civil",
    "中国刑事": "criminal",
    "仲裁": "arbitration",
    "美国民事": "us_civil",
    "刑民交叉": "mixed",
}
_DEFAULT_ROLE = {
    "civil": "plaintiff",
    "criminal": "suspect",
    "arbitration": "claimant",
    "us_civil": "plaintiff",
    "mixed": "plaintiff+suspect",
}


def _case_dir_snapshot(root: Path) -> set:
    if not root.exists():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir()}


def _rollback_new_case_dirs(root: Path, created) -> list:
    """Remove only newly created empty directories; never touch user files."""
    skipped = []
    root = root.resolve()
    for rel in sorted(set(created), key=lambda value: value.count("/"), reverse=True):
        path = (root / rel).resolve(strict=False)
        if path == root or not is_relative_to(path, root) or path.is_symlink():
            skipped.append(rel)
            continue
        if not path.exists() or not path.is_dir():
            continue
        try:
            entries = list(path.iterdir())
            if all(entry.name == ".gitkeep" and entry.is_file() for entry in entries):
                for entry in entries:
                    entry.unlink()
                path.rmdir()
            else:
                skipped.append(rel)
        except OSError:
            skipped.append(rel)
    return skipped


@app.route("/api/cases/<key>/stages", methods=["POST"])
def add_case_stage(key):
    data = request.get_json(silent=True) or {}
    stage = data.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        return jsonify({"ok": False, "error": "请选择要创建的程序阶段"}), 400
    stage = stage.strip()
    cfg, paths = _config(), _paths()
    case = next((item for item in cfg.get("cases", []) if item.get("key") == key), None)
    if not case:
        return jsonify({"ok": False, "error": "案件不存在"}), 404
    proc_type = _SCENARIO_PROC_TYPE.get(case.get("scenario"))
    if not proc_type:
        return jsonify({"ok": False, "error": "案件场景未设置或不支持阶段目录"}), 400
    root = _case_path(case, paths)
    if not root.exists() or not root.is_dir():
        return jsonify({"ok": False, "error": f"案件文件夹不存在：{root}"}), 409

    sys.path.insert(0, str(BIN))
    from intake import create_folders

    before = _case_dir_snapshot(root)
    created = []
    try:
        create_folders(root, proc_type, case.get("role") or _DEFAULT_ROLE[proc_type], stage)
        after = _case_dir_snapshot(root)
        created = sorted(after - before, key=lambda value: (value.count("/"), value))

        def _record_stage(raw_cfg):
            raw_cfg = raw_cfg or {}
            raw_case = next((item for item in raw_cfg.get("cases", [])
                             if item.get("key") == key), None)
            if not raw_case:
                raise KeyError(key)
            raw_case["stage"] = stage
            stages = raw_case.get("stages") or []
            if not isinstance(stages, list):
                stages = [str(stages)]
            raw_case["stages"] = list(dict.fromkeys(
                [str(value).strip() for value in stages if str(value).strip()] + [stage]
            ))
            raw_case["loci_matter"] = canonicalize_case(raw_case)
            return raw_cfg

        update_yaml(AF / "config.yaml", _record_stage, default={})
    except KeyError:
        skipped = _rollback_new_case_dirs(root, created or (_case_dir_snapshot(root) - before))
        return jsonify({"ok": False, "error": "案件配置已变化，阶段创建已回滚",
                        "rollback_skipped": skipped}), 409
    except (OSError, SafetyError, TypeError, ValueError) as exc:
        skipped = _rollback_new_case_dirs(root, created or (_case_dir_snapshot(root) - before))
        return jsonify({"ok": False, "error": str(exc),
                        "rollback_skipped": skipped}), 400
    return jsonify({"ok": True, "stage": stage, "created": created,
                    "count": len(created)})

@app.route("/api/cases/status", methods=["POST"])
def case_status():
    data = request.json or {}
    key = (data.get("key") or "").strip()
    status = data.get("status")  # "active" / "closed"
    cfg_path = AF / "config.yaml"
    def _mutate(cfg):
        cfg = cfg or {}
        case = next((c for c in cfg.get("cases", []) if c.get("key") == key), None)
        if not case:
            raise KeyError(key)
        if status == "closed":
            case["status"] = "closed"
        else:
            case.pop("status", None)
        if "loci_matter" in case:
            case["loci_matter"] = canonicalize_case(case)
        return cfg
    try:
        update_yaml(cfg_path, _mutate, default={})
    except KeyError:
        return jsonify({"ok": False, "error": f"案件 {key!r} 不存在"}), 404
    return jsonify({"ok": True})

@app.route("/api/rules")
def get_rules():
    rules = _config().get("rules", [])
    validation = validate_rules(rules)
    return jsonify({"rules": rules, "validation": validation,
                    "digest": validation["digest"]})

@app.route("/api/rules", methods=["POST"])
def save_rules():
    cfg_path = AF / "config.yaml"
    payload = request.json or {}
    rules = payload.get("rules", [])
    base_digest = payload.get("base_digest")
    validation = validate_rules(rules)
    if not validation["ok"]:
        return jsonify({"ok": False, "error": "规则校验未通过",
                        "validation": validation}), 400
    current = load_yaml(cfg_path, default={}) or {}
    old_rules = current.get("rules", [])
    current_digest = rules_digest(old_rules)
    if base_digest and base_digest != current_digest:
        return jsonify({"ok": False, "conflict": True,
                        "error": "规则已被其他窗口或进程修改，请重新加载后再保存",
                        "current_digest": current_digest}), 409
    def _replace_rules(cfg):
        cfg = cfg or {}
        cfg["rules"] = rules
        return cfg
    try:
        update_yaml(cfg_path, _replace_rules, default={})
    except (SafetyError, TypeError, ValueError, OSError) as exc:
        return jsonify({"ok": False, "error": f"规则写入失败：{exc}"}), 400

    response = {"ok": True, "digest": validation["digest"],
                "validation": validation}
    try:
        append_rule_history(Path(_paths()["logs"]) / "rule_history.jsonl",
                            old_rules, rules, actor="loci")
    except (OSError, ValueError) as exc:
        # The config has already been safely written; surface the audit issue
        # instead of pretending version history was recorded.
        response["warning"] = f"规则已保存，但版本历史写入失败：{exc}"
    return jsonify(response)


@app.route("/api/rules/history")
def rule_history():
    entries = load_rule_history(Path(_paths()["logs"]) / "rule_history.jsonl")
    return jsonify({"entries": [
        {key: entry.get(key) for key in
         ("id", "timestamp", "actor", "old_digest", "digest", "count", "restored_from")
         if key in entry}
        for entry in entries
    ]})


@app.route("/api/rules/restore", methods=["POST"])
def restore_rule_version():
    version_id = (request.json or {}).get("id", "")
    if not version_id:
        return jsonify({"ok": False, "error": "缺少规则版本 ID"}), 400
    history_path = Path(_paths()["logs"]) / "rule_history.jsonl"
    entry = next((item for item in load_rule_history(history_path)
                  if item.get("id") == version_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "规则版本不存在或已超出历史保留范围"}), 404
    rules = entry.get("rules")
    validation = validate_rules(rules)
    if not validation["ok"]:
        return jsonify({"ok": False, "error": "历史版本校验未通过",
                        "validation": validation}), 400
    cfg_path = AF / "config.yaml"
    current = load_yaml(cfg_path, default={}) or {}
    old_rules = current.get("rules", [])
    try:
        update_yaml(cfg_path, lambda cfg: {**(cfg or {}), "rules": rules}, default={})
        append_rule_history(history_path, old_rules, rules, actor="loci",
                            restored_from=version_id)
    except (OSError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"恢复规则失败：{exc}"}), 400
    return jsonify({"ok": True, "digest": rules_digest(rules),
                    "validation": validation})


@app.route("/api/rules/sandbox", methods=["POST"])
def rule_sandbox():
    """Run a no-side-effect classification preview against current/editing rules."""
    data = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "请填写文件名"}), 400
    rules = data.get("rules")
    if rules is None:
        rules = _config().get("rules", [])
    validation = validate_rules(rules)
    if not validation["ok"]:
        return jsonify({"ok": False, "error": "当前规则不能用于沙盒测试",
                        "validation": validation}), 400
    manual = {key: data.get(key) for key in ("case_key", "target", "source", "doc_type")
              if data.get(key)}
    try:
        decision = build_filing_decision(
            filename, data.get("text") or "", _config().get("cases", []), rules,
            extracted_title=data.get("title"), manual=manual,
            received_at=data.get("received_at"),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "decision": decision,
                    "validation": validation})


_INTAKE_ROLES = {
    "civil": frozenset({"plaintiff", "defendant", "third-party"}),
    "us_civil": frozenset({"plaintiff", "defendant", "third-party"}),
    "arbitration": frozenset({"claimant", "respondent"}),
    "criminal": frozenset({"suspect", "complainant"}),
}


def _validate_intake_payload(raw_data: dict, cfg: dict, paths: dict):
    """Validate and normalize all intake inputs before creating staging."""
    if not isinstance(raw_data, dict):
        raise TypeError("请求格式无效")
    data = dict(raw_data)

    raw_case_name = data.get("case_name") or ""
    if not isinstance(raw_case_name, str):
        raise TypeError("案件名称必须是字符串")
    case_name = validate_case_name(raw_case_name.strip())
    data["case_name"] = case_name

    raw_cases_dir = data.get("cases_dir") or paths["cases"]
    cases_dir = Path(raw_cases_dir).expanduser().resolve()
    if cases_dir.exists() and not cases_dir.is_dir():
        raise SafetyError("案件根目录不是文件夹")
    base = safe_case_root(cases_dir, case_name)
    # lexists also rejects a dangling symlink at the final destination.
    if os.path.lexists(str(base)):
        raise FileExistsError(f"文件夹已存在：{base}")

    raw_abbr = data.get("abbr") or ""
    if not isinstance(raw_abbr, str):
        raise TypeError("案件简称必须是字符串")
    abbr = validate_basename(raw_abbr.strip(), "案件简称")
    if re.search(r"[\\/:*?\"<>|：\s]", abbr):
        raise SafetyError(f"简称不能含空格或 / \\ : * ? 等字符：{abbr!r}")
    data["abbr"] = abbr

    cases = (cfg or {}).get("cases", [])
    if cases is None:
        cases = []
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise ValueError("配置中的 cases 必须是对象列表")
    if any(case.get("key") == abbr for case in cases):
        raise FileExistsError(f"简称 {abbr!r} 已被占用，请换一个")

    proc_type = data.get("proc_type")
    if proc_type not in set(_INTAKE_ROLES) | {"mixed"}:
        raise ValueError("请选择有效的程序类型")
    role = data.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("请选择有效的案件角色")
    role = role.strip()
    if proc_type == "mixed":
        parts = role.split("+")
        if (len(parts) != 2 or parts[0] not in _INTAKE_ROLES["civil"]
                or parts[1] not in _INTAKE_ROLES["criminal"]):
            raise ValueError("刑民交叉案件角色格式无效")
    elif role not in _INTAKE_ROLES[proc_type]:
        raise ValueError("案件角色与程序类型不匹配")
    data["role"] = role

    for field in ("cause", "stage", "notes", "case_number"):
        value = data.get(field) or ""
        if not isinstance(value, str):
            raise TypeError(f"{field} 必须是字符串")
        data[field] = value.strip()
    for field in ("parties_us", "parties_op", "parties_other"):
        value = data.get(field)
        if value is None:
            value = []
        if not isinstance(value, list):
            raise TypeError(f"{field} 必须是列表")
        if any(not isinstance(item, str) for item in value):
            raise TypeError(f"{field} 中的主体必须是字符串")
        data[field] = list(value)

    raw_text = data.get("raw_text") or ""
    if not isinstance(raw_text, str):
        raise TypeError("raw_text 必须是字符串")
    data["parties_other"] = data["parties_other"]
    return data, cases_dir, base, raw_text, abbr


def _intake_case_entry(data: dict, cases_dir: Path, key: str, keywords: list) -> dict:
    entry = {"key": key, "name": data["case_name"], "keywords": list(keywords)}
    scenario = PROC_SCENARIO.get(data.get("proc_type"))
    if scenario:
        entry["scenario"] = scenario
    entry["layout"] = {
        "civil": "staged_compact", "arbitration": "staged_arb_compact",
        "criminal": "staged_crim_compact", "mixed": "staged_mixed_compact",
        "us_civil": "us_civil",
    }.get(data.get("proc_type"), "")
    if data.get("case_number"):
        entry["case_number"] = data["case_number"]
    entry["party_us"] = list(data.get("parties_us") or [])
    entry["party_op"] = list(data.get("parties_op") or [])
    entry["party_third"] = list(data.get("parties_other") or [])
    entry["cause"] = data.get("cause", "")
    entry["stage"] = data.get("stage", "")
    entry["role"] = data.get("role", "")
    entry["cases_dir"] = str(cases_dir)
    entry["loci_matter"] = canonicalize_case(entry)
    return entry


@app.route("/api/intake/preview", methods=["POST"])
def intake_preview():
    """Build a side-effect-free preview of a new matter."""
    sys.path.insert(0, str(BIN))
    from intake import create_folders, extract_keywords
    data = request.get_json(silent=True) or {}
    try:
        if not isinstance(data, dict):
            raise TypeError("请求格式无效")
        normalized, cases_dir, base, raw, abbr = _validate_intake_payload(
            data, _config(), _paths())
        keywords = extract_keywords(raw) if raw.strip() else []
        if abbr and abbr not in keywords:
            keywords.insert(0, abbr)
        # Exercise the real stage-aware builder in a disposable directory.
        # No config, case root, or case_meta.yaml is touched by preview.
        with tempfile.TemporaryDirectory(prefix="loci-intake-preview-") as tmp:
            preview_root = Path(tmp) / "matter"
            create_folders(preview_root, normalized["proc_type"],
                           normalized["role"], normalized.get("stage"))
            folders = sorted(
                str(path.relative_to(preview_root))
                for path in preview_root.rglob("*")
                if path.is_dir()
            )
        path_health = (
            "available" if cases_dir.exists() and cases_dir.is_dir()
            else "will_create"
        )
        return jsonify({
            "ok": True,
            "case": {
                "name": normalized["case_name"],
                "abbr": abbr,
                "case_number": normalized.get("case_number", ""),
                "scenario": PROC_SCENARIO.get(normalized.get("proc_type"), ""),
                "role": normalized["role"],
                "stage": normalized.get("stage", ""),
            },
            "path": str(base),
            "cases_dir": str(cases_dir),
            "path_health": path_health,
            "keywords": keywords,
            "folders": folders,
            "folder_count": len(folders),
            "message": "仅生成预览，尚未写入配置或创建案件目录",
        })
    except FileExistsError as exc:
        return jsonify({"ok": False, "error": str(exc), "conflict": True}), 409
    except (OSError, SafetyError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/intake", methods=["POST"])
def intake():
    sys.path.insert(0, str(BIN))
    from intake import create_folders, write_meta, extract_keywords, update_env_cases_dir
    data = request.json or {}
    staging = None
    staging_parent = None
    staging_prefix = ""
    config_added = False

    def _rollback_intake() -> list[str]:
        details = []
        if config_added:
            try:
                def _remove(cfg):
                    cfg = cfg or {}
                    if isinstance(cfg, dict) and isinstance(cfg.get("cases"), list):
                        cfg["cases"] = [
                            case for case in cfg["cases"]
                            if not (isinstance(case, dict) and case.get("key") == abbr)
                        ]
                    return cfg
                update_yaml(AF / "config.yaml", _remove, default={})
            except Exception as cleanup_exc:
                details.append(
                    f"配置回滚失败：{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        staging_error = _remove_intake_staging(staging, staging_parent, staging_prefix)
        if staging_error:
            details.append(staging_error)
        return details

    try:
        if not isinstance(data, dict):
            raise TypeError("请求格式无效")
        data, cases_dir, base, raw, abbr = _validate_intake_payload(
            data, _config(), _paths())
        kws = extract_keywords(raw) if raw.strip() else []
        if abbr and abbr not in kws:
            kws.insert(0, abbr)
        data["keywords"] = kws

        # The temporary directory is hidden, unique to this request, and is
        # created beside the eventual case root so os.replace is atomic.
        cases_dir.mkdir(parents=True, exist_ok=True)
        staging_parent = cases_dir
        case_name = data["case_name"]
        staging_prefix = f".{case_name}.staging-"
        staging = Path(tempfile.mkdtemp(prefix=staging_prefix, dir=str(cases_dir)))
        if staging.parent.resolve() != cases_dir.resolve() or not staging.name.startswith(staging_prefix):
            raise SafetyError("staging 路径校验失败")

        create_folders(staging, data["proc_type"], data["role"], data.get("stage"))
        write_meta(staging, data)

        def _append(cfg):
            nonlocal config_added
            cfg = cfg or {}
            if not isinstance(cfg, dict):
                raise ValueError("配置根节点必须是对象")
            cfg.setdefault("cases", [])
            if not isinstance(cfg["cases"], list):
                raise ValueError("配置中的 cases 必须是列表")
            if any(c.get("key") == abbr for c in cfg["cases"]):
                raise ValueError(f"简称 {abbr!r} 可能已存在")
            entry = _intake_case_entry(data, cases_dir, abbr, kws)
            cfg["cases"].append(entry)
            config_added = True
            return cfg

        update_yaml(AF / "config.yaml", _append, default={})
        if not config_added:
            raise RuntimeError("配置更新未确认新增本次案件")

        # Refuse a destination that appeared while the transaction was being
        # prepared; never replace an existing case root.
        if os.path.lexists(str(base)):
            raise FileExistsError(f"文件夹在建案期间出现：{base}")
        os.replace(str(staging), str(base))
        staging = None

        env_updated = None
        env_warning = None
        if data.get("update_env"):
            env_updated = False
            try:
                update_env_cases_dir(WS / ".env", str(cases_dir))
                env_updated = True
            except Exception as exc:
                env_warning = (
                    "案件已成功创建，但默认 CASES_DIR 更新失败；请手动检查 .env："
                    f" {type(exc).__name__}: {exc}"
                )

        result = {"ok": True, "path": str(base), "keywords": kws,
                  "env_updated": env_updated}
        if env_warning:
            result["warning"] = env_warning
        return jsonify(result)
    except FileExistsError as e:
        details = [str(e)] + _rollback_intake()
        return jsonify({"ok": False, "error": "；".join(details)}), 409
    except (SafetyError, TypeError, ValueError) as e:
        details = [str(e)] + _rollback_intake()
        return jsonify({"ok": False, "error": "；".join(details)}), 400
    except Exception as e:
        details = [str(e)] + _rollback_intake()
        return jsonify({"ok": False, "error": "；".join(details)}), 500

if __name__ == "__main__":
    _prepare_startup_security()
    app.run(host="127.0.0.1", port=5081, debug=False, use_reloader=False)
