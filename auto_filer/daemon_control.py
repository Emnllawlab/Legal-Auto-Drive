#!/usr/bin/env python3
"""Single lifecycle controller for the auto_filer daemon.

The Flask UI and the shell entry point both use this module.  The controller
lock serializes lifecycle transitions; the daemon additionally holds the
instance lock for its entire lifetime.  A PID is never sufficient evidence
for a stop operation: a ready state, a self-consistent command fingerprint,
and the held instance lock are required first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

try:  # package import
    from .safety import FileLock, LockBusy
except ImportError:  # direct ``python daemon_control.py`` use
    from safety import FileLock, LockBusy


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_PID_MODE = 0o600
_DEFAULT_TIMEOUT = 5.0
_POLL_INTERVAL = 0.03
_LAUNCHING = "launching"
_STALE_LAUNCHING = "stale-launching"
_STARTING = "starting"
_READY = "ready"
_STOP_REQUESTED = "stop_requested"
_PROCESS_IDENTITY_TIMEOUT = 0.75
_LAUNCH_RESERVATION_TTL = 30.0
_STALE_RESERVATION_GRACE = 2.0


class DaemonAlreadyRunning(RuntimeError):
    """Another process owns the lifetime instance lock."""


@dataclass(frozen=True)
class DaemonPaths:
    control_dir: Path

    @property
    def lifecycle_lock(self) -> Path:
        return self.control_dir / ".filer.lifecycle.lock"

    @property
    def instance_lock(self) -> Path:
        return self.control_dir / ".filer.instance.lock"

    @property
    def state(self) -> Path:
        return self.control_dir / ".filer.state.json"

    @property
    def pid(self) -> Path:
        return self.control_dir / ".filer.pid"


@dataclass
class DaemonStatus:
    running: bool
    ready: bool
    pid: Optional[int] = None
    state: Optional[dict[str, Any]] = None
    detail: str = "stopped"

    def as_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "ready": self.ready,
            "starting": self.detail in {_LAUNCHING, _STARTING},
            "pid": self.pid,
            "detail": self.detail,
            "state": self.state,
        }


@dataclass
class StartResult:
    pid: Optional[int] = None
    already_running: bool = False
    process: Any = None
    error: Optional[str] = None
    gate_blocked: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.pid is not None


@dataclass
class StopResult:
    ok: bool
    error: Optional[str] = None


def paths_for(control_dir: os.PathLike[str] | str) -> DaemonPaths:
    return DaemonPaths(Path(control_dir).resolve(strict=False))


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, _PID_MODE)
    except FileNotFoundError:
        pass
    except OSError:
        # The file contents and the process lock are still the identity
        # checks.  A best-effort chmod preserves operation on restrictive
        # filesystems without widening permissions.
        pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_dotenv(path: os.PathLike[str] | str) -> dict[str, str]:
    """Read simple KEY=value entries without executing shell syntax."""
    env_path = Path(path)
    raw: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return raw
    except (OSError, UnicodeError):
        return raw
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            continue
        raw[key] = _unquote(value)
    return raw


def _expand_env_value(value: str, raw: Mapping[str, str], environ: Mapping[str, str]) -> str:
    current = value
    for _ in range(12):
        changed = False

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            name = match.group(1) or match.group(2)
            replacement = raw.get(name, environ.get(name, match.group(0)))
            if replacement != match.group(0):
                changed = True
            return replacement

        expanded = _VAR_REF.sub(replace, current)
        current = expanded
        if not changed:
            break
    return current


def env_value(control_dir: os.PathLike[str] | str, key: str, default: str = "") -> str:
    if not _ENV_KEY.fullmatch(key):
        raise ValueError(f"非法环境变量名: {key!r}")
    root = Path(control_dir).resolve(strict=False).parent
    raw = read_dotenv(root / ".env")
    # A real process environment is the normal explicit override, but the
    # file is parsed as data rather than sourced as shell code.
    value = os.environ.get(key, raw.get(key, default))
    return _expand_env_value(value, raw, os.environ)


def unattended_filing_enabled(control_dir: os.PathLike[str] | str) -> bool:
    return env_value(control_dir, "ALLOW_UNATTENDED_FILING", "").strip().lower() == "true"


def unattended_gate_error() -> str:
    return (
        "无人值守守护已锁定：默认仅人工确认。请在私有 .env 中明确设置 "
        "ALLOW_UNATTENDED_FILING=true，并在充分验收后再启用。"
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, _PID_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _chmod_private(path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _read_state(paths: DaemonPaths) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(paths.state.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_pid_file(paths: DaemonPaths) -> Optional[int]:
    try:
        value = paths.pid.read_text(encoding="ascii").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    if not value.isdigit():
        return None
    pid = int(value)
    return pid if pid > 0 else None


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _remove_state(paths: DaemonPaths, expected_instance_id: Optional[str] = None) -> None:
    state = _read_state(paths)
    if expected_instance_id is not None:
        if not state or state.get("instance_id") != expected_instance_id:
            return
    state_pid = state.get("pid") if state else None
    _remove_file(paths.state)
    if state_pid is not None and _read_pid_file(paths) == state_pid:
        _remove_file(paths.pid)


def _remove_launch_reservation(
    paths: DaemonPaths,
    expected_pid: Optional[int] = None,
    expected_launch_nonce: Optional[str] = None,
) -> None:
    state = _read_state(paths)
    if not state or state.get("status") != _LAUNCHING:
        return
    if expected_pid is not None and _reservation_child_pid(state) != expected_pid:
        return
    if expected_launch_nonce is not None and state.get("launch_nonce") != expected_launch_nonce:
        return
    _remove_state(paths, expected_instance_id=state.get("instance_id"))


def _normalize_command(command: Sequence[object] | Mapping[str, object]) -> dict[str, Any]:
    if isinstance(command, Mapping):
        script = str(command.get("script", ""))
        args = [str(item) for item in (command.get("args") or [])]
        return {
            "script": str(Path(script).resolve(strict=False)) if script else "",
            "args": args,
        }
    argv = [str(item) for item in command]
    script_index = next(
        (index for index, item in enumerate(argv) if Path(item).name == "auto_filer.py"),
        None,
    )
    if script_index is None:
        # A command without our script must never normalize to its last
        # argument.  That fallback could make an unrelated process look like
        # the daemon when its argv happened to have the same suffix.
        return {"script": "", "args": argv}
    script = argv[script_index]
    return {
        "script": str(Path(script).resolve(strict=False)),
        "args": argv[script_index + 1:],
    }


def command_fingerprint(command: Sequence[object] | Mapping[str, object]) -> str:
    normalized = _normalize_command(command)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_argv(pid: int) -> Optional[list[str]]:
    """Read argv without trusting a state-file PID by itself."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
    except (FileNotFoundError, OSError):
        raw = b""
    if raw:
        try:
            argv = [item.decode("utf-8", errors="strict") for item in raw.split(b"\0") if item]
        except UnicodeError:
            argv = []
        if argv:
            return argv

    # macOS has no /proc.  ``ps`` is only an identity observation; it is never
    # used as a signal mechanism.
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=_PROCESS_IDENTITY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command_line = completed.stdout.strip()
    if completed.returncode != 0 or not command_line:
        return None
    try:
        return shlex.split(command_line)
    except ValueError:
        return None


def _is_python_executable(value: str) -> bool:
    """Recognize the interpreter prefix shown by macOS ``ps``."""
    return re.fullmatch(
        r"python(?:\d+(?:\.\d+)*)?", Path(value).name, re.IGNORECASE
    ) is not None


def _process_command_matches(pid: int, command: Mapping[str, Any]) -> bool:
    actual = _process_argv(pid)
    if not actual:
        return False
    try:
        script_index = next(
            (index for index, item in enumerate(actual) if Path(item).name == "auto_filer.py"),
            None,
        )
        if script_index is None or script_index == 0:
            return False
        # The observed argv must be a Python script invocation.  In
        # particular, do not accept ``python -c ... auto_filer.py ...`` or a
        # different script with auto_filer.py merely passed as an argument.
        if not _is_python_executable(actual[0]):
            return False
        if any(not item.startswith("-") for item in actual[1:script_index]):
            return False
        if any(
            item == "-" or item in {"-c", "-m"}
            or item.startswith(("-c", "-m"))
            for item in actual[1:script_index]
        ):
            return False
        return _normalize_command(actual) == _normalize_command(command)
    except (TypeError, ValueError, OSError):
        return False


def _process_identity(pid: int, command: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_command(command)
    return {
        "pid": pid,
        "command": normalized,
        "command_fingerprint": command_fingerprint(normalized),
    }


def _instance_lock_held(paths: DaemonPaths) -> bool:
    probe = FileLock(paths.instance_lock, blocking=False)
    try:
        probe.acquire()
    except LockBusy:
        return True
    else:
        probe.release()
        return False


@contextmanager
def lifecycle_lock(control_dir: os.PathLike[str] | str) -> Iterator[DaemonPaths]:
    paths = paths_for(control_dir)
    paths.control_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(paths.lifecycle_lock, blocking=True)
    with lock:
        _chmod_private(paths.lifecycle_lock)
        yield paths


def _valid_ready_state(paths: DaemonPaths, state: Optional[dict[str, Any]]) -> bool:
    return _valid_daemon_state(paths, state, _READY)


def _valid_daemon_state(
    paths: DaemonPaths,
    state: Optional[dict[str, Any]],
    expected_status: str,
) -> bool:
    if not state or state.get("status") != expected_status:
        return False
    if state.get("control_dir") != str(paths.control_dir):
        return False
    pid = state.get("pid")
    instance_id = state.get("instance_id")
    command = state.get("command")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(instance_id, str) or not instance_id:
        return False
    if not isinstance(command, dict) or command.get("script") != str(paths.control_dir / "auto_filer.py"):
        return False
    args = command.get("args")
    if not isinstance(args, list) or "--daemon" not in args or "--once" in args:
        return False
    if state.get("command_fingerprint") != command_fingerprint(command):
        return False
    process_identity = state.get("process_identity")
    if not isinstance(process_identity, dict):
        return False
    if process_identity.get("pid") != pid:
        return False
    if process_identity.get("command") != command:
        return False
    if process_identity.get("command_fingerprint") != state.get("command_fingerprint"):
        return False
    # State metadata is not proof that the PID still belongs to this daemon.
    # Observe argv before reporting ready/starting or writing a stop marker.
    if not _process_command_matches(pid, command):
        return False
    # The PID file is a second piece of the private identity record.  It is
    # written before the state record, so a state that passes this check is
    # never observed with an unrelated PID file during publication.
    return _read_pid_file(paths) == pid


def _reservation_child_pid(state: Optional[Mapping[str, Any]]) -> Optional[int]:
    if not state:
        return None
    child_identity = state.get("child_identity")
    if not isinstance(child_identity, dict):
        return None
    child_pid = child_identity.get("pid")
    return child_pid if isinstance(child_pid, int) and child_pid > 0 else None


def _valid_launch_reservation(paths: DaemonPaths, state: Optional[dict[str, Any]]) -> bool:
    if not state or state.get("status") != _LAUNCHING:
        return False
    if state.get("control_dir") != str(paths.control_dir):
        return False
    instance_id = state.get("instance_id")
    launch_nonce = state.get("launch_nonce")
    launcher_pid = state.get("launcher_pid")
    if not isinstance(instance_id, str) or not instance_id:
        return False
    if not isinstance(launch_nonce, str) or not launch_nonce or launch_nonce != instance_id:
        return False
    if not isinstance(launcher_pid, int) or launcher_pid <= 0:
        return False
    if state.get("launcher_nonce") != launch_nonce:
        return False
    command = state.get("command")
    if not isinstance(command, dict) or command.get("script") != str(paths.control_dir / "auto_filer.py"):
        return False
    args = command.get("args")
    if not isinstance(args, list) or "--daemon" not in args or "--once" in args:
        return False
    fingerprint = state.get("command_fingerprint")
    if fingerprint != command_fingerprint(command):
        return False
    child_identity = state.get("child_identity")
    if not isinstance(child_identity, dict):
        return False
    if child_identity.get("launch_nonce") != launch_nonce:
        return False
    if child_identity.get("command") != command:
        return False
    if child_identity.get("command_fingerprint") != fingerprint:
        return False
    child_pid = _reservation_child_pid(state)
    state_pid = state.get("pid")
    if state_pid is not None:
        if not isinstance(state_pid, int) or state_pid <= 0 or child_pid != state_pid:
            return False
        if _read_pid_file(paths) != state_pid:
            return False
    elif child_pid is not None or _read_pid_file(paths) is not None:
        return False
    return True


def _launch_reservation_is_live(
    paths: DaemonPaths,
    state: Optional[dict[str, Any]],
) -> bool:
    """Return whether a launch reservation still has the expected processes.

    This function only observes process state.  Its callers may discard stale
    metadata, but must never turn these PIDs into signal targets.
    """
    if not _valid_launch_reservation(paths, state):
        return False
    assert state is not None  # narrowed by _valid_launch_reservation
    launcher_pid = state.get("launcher_pid")
    child_pid = _reservation_child_pid(state)
    command = state.get("command")
    launching_at = state.get("launching_at")
    if (
        not isinstance(launcher_pid, int)
        or not isinstance(child_pid, int)
        or not isinstance(command, dict)
        or isinstance(launching_at, bool)
        or not isinstance(launching_at, (int, float))
    ):
        return False
    if time.time() - float(launching_at) > _LAUNCH_RESERVATION_TTL:
        return False
    if not _pid_alive(launcher_pid) or not _pid_alive(child_pid):
        return False
    return _process_command_matches(child_pid, command)


def _launch_reservation_child_is_live(
    paths: DaemonPaths,
    state: Optional[dict[str, Any]],
) -> bool:
    """Observe a reservation child without applying the reservation TTL.

    The TTL says that the launcher stopped making progress; it does not prove
    that the child disappeared.  Keeping this observation separate prevents a
    stale reservation from being deleted while its verified child is still
    able to acquire the instance lock.
    """
    if not _valid_launch_reservation(paths, state):
        return False
    assert state is not None
    child_pid = _reservation_child_pid(state)
    command = state.get("command")
    return (
        isinstance(child_pid, int)
        and isinstance(command, dict)
        and _pid_alive(child_pid)
        and _process_command_matches(child_pid, command)
    )


def _launch_reservation_child_status(
    paths: DaemonPaths,
    state: Optional[dict[str, Any]],
) -> str:
    """Classify a reservation child without treating unknown as dead.

    ``unknown`` is important on macOS when process inspection is temporarily
    unavailable.  It is a fail-closed state: metadata is retained and a new
    child is not launched until the existing reservation can be disproved.
    """
    if not _valid_launch_reservation(paths, state):
        return "invalid"
    assert state is not None
    child_pid = _reservation_child_pid(state)
    command = state.get("command")
    if not isinstance(child_pid, int) or not isinstance(command, dict):
        return "invalid"
    if not _pid_alive(child_pid):
        return "dead"
    if _process_argv(child_pid) is None:
        return "unknown"
    return "live" if _process_command_matches(child_pid, command) else "mismatch"


def _launch_reservation_age(state: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not state:
        return None
    launched_at = state.get("launching_at")
    if isinstance(launched_at, bool) or not isinstance(launched_at, (int, float)):
        return None
    return max(0.0, time.time() - float(launched_at))


def _terminate_verified_reservation_child(
    paths: DaemonPaths,
    reservation: Mapping[str, Any],
    timeout: float,
) -> Optional[str]:
    """Terminate an unowned stale child only after identity revalidation.

    The signal target is never derived from a PID alone.  The lifecycle lock
    freezes the reservation while the command identity is checked and the
    signal is sent, preventing the child from transitioning to a different
    state between those operations.
    """
    child_pid = _reservation_child_pid(reservation)
    command = reservation.get("command")
    if not isinstance(child_pid, int) or not isinstance(command, dict):
        return "stale reservation child identity 无效，未发送信号"
    with lifecycle_lock(paths.control_dir):
        current = _read_state(paths)
        if (
            not current
            or current.get("status") != _LAUNCHING
            or not _same_launch_identity(current, reservation)
            or _instance_lock_held(paths)
            or not _process_command_matches(child_pid, command)
        ):
            return "stale reservation 状态或进程身份无法验证，未发送信号"
        try:
            os.kill(child_pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        except OSError as exc:
            return f"回收 stale reservation 子进程失败：{type(exc).__name__}: {exc}"

    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        if not _pid_alive(child_pid):
            return None
        time.sleep(_POLL_INTERVAL)

    with lifecycle_lock(paths.control_dir):
        current = _read_state(paths)
        if (
            current
            and current.get("status") == _LAUNCHING
            and _same_launch_identity(current, reservation)
            and not _instance_lock_held(paths)
            and _process_command_matches(child_pid, command)
        ):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                return None
            except OSError as exc:
                return f"强制回收 stale reservation 子进程失败：{type(exc).__name__}: {exc}"
        else:
            return "stale reservation 状态或进程身份发生变化，未发送强制信号"

    if _pid_alive(child_pid):
        return "stale reservation 子进程无法回收"
    return None


def _launch_id_from_state(state: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not state:
        return None
    launch_id = state.get("launch_id") or state.get("launch_nonce") or state.get("instance_id")
    return launch_id if isinstance(launch_id, str) and launch_id else None


def _state_identity(state: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the complete identity that a cooperative stop may target."""
    if not state:
        return None
    instance_id = state.get("instance_id")
    launch_id = _launch_id_from_state(state)
    pid = state.get("pid")
    fingerprint = state.get("command_fingerprint")
    if (
        not isinstance(instance_id, str)
        or not instance_id
        or not isinstance(launch_id, str)
        or not launch_id
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(fingerprint, str)
        or not fingerprint
    ):
        return None
    return {
        "instance_id": instance_id,
        "launch_id": launch_id,
        "pid": pid,
        "fingerprint": fingerprint,
    }


def _stop_identity(state: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    identity = _state_identity(state)
    if identity is None:
        return None
    return {
        "instance_id": identity["instance_id"],
        "pid": identity["pid"],
        "command_fingerprint": identity["fingerprint"],
    }


def _same_identity(
    state: Optional[Mapping[str, Any]],
    identity: Optional[Mapping[str, Any]],
) -> bool:
    current = _state_identity(state)
    if current is None or identity is None:
        return False
    return all(
        current.get(key) == identity.get(key)
        for key in ("instance_id", "launch_id", "pid", "fingerprint")
    )


def _stop_marker_for_state(state: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    marker = state.get(_STOP_REQUESTED) if state else None
    identity = _stop_identity(state)
    if not isinstance(marker, Mapping) or identity is None:
        return None
    if any(marker.get(key) != identity.get(key) for key in identity):
        return None
    return dict(marker)


def _stop_marker_matches_state(
    state: Optional[Mapping[str, Any]],
    marker: object,
) -> bool:
    if not isinstance(marker, Mapping):
        return False
    identity = _stop_identity(state)
    return identity is not None and all(
        marker.get(key) == identity.get(key) for key in identity
    )


def _inspect_unlocked(paths: DaemonPaths) -> DaemonStatus:
    state = _read_state(paths)
    if _valid_launch_reservation(paths, state):
        if not _launch_reservation_is_live(paths, state):
            return DaemonStatus(
                False,
                False,
                pid=_reservation_child_pid(state),
                detail=_STALE_LAUNCHING,
                state=state,
            )
        return DaemonStatus(
            False,
            False,
            pid=_reservation_child_pid(state),
            detail=_LAUNCHING,
            state=state,
        )
    held = _instance_lock_held(paths)
    if not held:
        return DaemonStatus(
            False, False, detail="stopped" if state is None else "stale-state", state=state,
        )
    if not state:
        return DaemonStatus(False, False, detail=_STARTING, state=None)
    if _valid_daemon_state(paths, state, _STARTING):
        pid = state["pid"]
        if not _pid_alive(pid):
            return DaemonStatus(False, False, pid=pid, detail="dead-state", state=state)
        return DaemonStatus(False, False, pid=pid, detail=_STARTING, state=state)
    if not _valid_ready_state(paths, state):
        return DaemonStatus(False, False, detail="invalid-state", state=state)
    pid = state["pid"]
    if not _pid_alive(pid):
        return DaemonStatus(False, False, detail="dead-state", state=state)
    return DaemonStatus(True, True, pid=pid, detail="ready", state=state)


def _cleanup_stale_unlocked(paths: DaemonPaths) -> None:
    # Never signal a PID here.  If the stale reservation still has a verified
    # child, leave an exact-identity stop marker for that child instead of
    # deleting metadata and allowing a second daemon to race it.
    if not _instance_lock_held(paths):
        state = _read_state(paths)
        child_status = _launch_reservation_child_status(paths, state)
        if child_status in {"live", "unknown"}:
            if child_status == "live":
                identity = _state_identity(state)
                if identity is not None:
                    _request_stop_locked(paths, expected_identity=identity)
            # Unknown process identity is deliberately retained.  Removing it
            # would permit a duplicate daemon while the original cannot be
            # disproved.
            return
        _remove_state(paths)
        if _read_pid_file(paths) is not None:
            _remove_file(paths.pid)


def daemon_status(control_dir: os.PathLike[str] | str) -> DaemonStatus:
    with lifecycle_lock(control_dir) as paths:
        reap_error = _reap_owned_child(paths)
        status = _inspect_unlocked(paths)
        if (
            reap_error is None
            and not status.running
            and status.detail in {"stale-state", "dead-state", "invalid-state", _STALE_LAUNCHING}
        ):
            _cleanup_stale_unlocked(paths)
        return status


class DaemonInstance:
    """Lifetime lock and identity publisher for one daemon process."""

    def __init__(
        self,
        paths: DaemonPaths,
        lock: FileLock,
        instance_id: str,
        *,
        launch_id: Optional[str] = None,
        stop_requested: Optional[Mapping[str, Any]] = None,
    ):
        self.paths = paths
        self.lock = lock
        self.instance_id = instance_id
        self.launch_id = launch_id or instance_id
        self.stop_requested = dict(stop_requested) if isinstance(stop_requested, Mapping) else None
        self._closed = False
        self._published = False
        self._command_fingerprint: Optional[str] = None

    def _publish_locked(
        self,
        status: str,
        command: Sequence[object] | Mapping[str, object],
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("daemon instance lock 已释放")
        normalized = _normalize_command(command)
        fingerprint = command_fingerprint(normalized)
        state = {
            "schema": 1,
            "status": status,
            "pid": os.getpid(),
            "instance_id": self.instance_id,
            "control_dir": str(self.paths.control_dir),
            "launch_id": self.launch_id,
            "command": normalized,
            "command_fingerprint": fingerprint,
            "process_identity": _process_identity(os.getpid(), normalized),
            f"{status}_at": time.time(),
        }
        existing = _read_state(self.paths)
        marker = _stop_marker_for_state(existing)
        if marker is None and self.stop_requested is not None:
            candidate = self.stop_requested
            if all(candidate.get(key) == state.get(key) for key in (
                "instance_id", "launch_id", "pid", "fingerprint",
            )):
                marker = dict(candidate)
        if marker is not None:
            state[_STOP_REQUESTED] = marker
        # Publish the PID record first.  A reader that observes the state
        # record therefore also observes the matching private PID file.
        _atomic_write_text(self.paths.pid, f"{state['pid']}\n")
        try:
            _atomic_write_json(self.paths.state, state)
        except Exception:
            _remove_file(self.paths.pid)
            raise
        self._published = True
        self._command_fingerprint = fingerprint
        return state

    def _publish(
        self,
        status: str,
        command: Sequence[object] | Mapping[str, object],
    ) -> dict[str, Any]:
        with lifecycle_lock(self.paths.control_dir):
            return self._publish_locked(status, command)

    def publish_starting(self, command: Sequence[object] | Mapping[str, object]) -> dict[str, Any]:
        """Publish stoppable identity immediately after taking instance lock."""
        return self._publish(_STARTING, command)

    def publish_ready(self, command: Sequence[object] | Mapping[str, object]) -> dict[str, Any]:
        return self._publish(_READY, command)

    def should_stop_requested(self) -> bool:
        """Return true only for a stop marker matching this exact instance."""
        if self._closed:
            return False
        with lifecycle_lock(self.paths.control_dir):
            state = _read_state(self.paths)
            identity = _state_identity(state)
            if identity is None or identity["instance_id"] != self.instance_id:
                return False
            if identity["launch_id"] != self.launch_id or identity["pid"] != os.getpid():
                return False
            if (
                self._command_fingerprint is not None
                and identity["fingerprint"] != self._command_fingerprint
            ):
                return False
            return _stop_marker_matches_state(state, state.get(_STOP_REQUESTED))

    def close(self) -> None:
        if self._closed:
            return
        try:
            with lifecycle_lock(self.paths.control_dir):
                state = _read_state(self.paths)
                if state and state.get("instance_id") == self.instance_id:
                    state_pid = state.get("pid")
                    _remove_state(self.paths, expected_instance_id=self.instance_id)
                    if state_pid == os.getpid() and _read_pid_file(self.paths) is not None:
                        _remove_file(self.paths.pid)
                elif self._published is False and _read_pid_file(self.paths) == os.getpid():
                    _remove_file(self.paths.pid)
        finally:
            self.lock.release()
            self._closed = True

    def __enter__(self) -> "DaemonInstance":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()


def acquire_daemon_instance(
    control_dir: os.PathLike[str] | str,
    *,
    command: Optional[Sequence[object] | Mapping[str, object]] = None,
) -> DaemonInstance:
    """Acquire the lifetime lock and optionally publish starting identity."""
    with lifecycle_lock(control_dir) as paths:
        lock = FileLock(paths.instance_lock, blocking=False)
        try:
            lock.acquire()
        except LockBusy as exc:
            raise DaemonAlreadyRunning("归档守护已由其他进程持有 instance lock") from exc
        instance_id: Optional[str] = None
        try:
            _chmod_private(paths.instance_lock)
            launch_state = _read_state(paths)
            launch_matches = (
                command is not None
                and _valid_launch_reservation(paths, launch_state)
                and _reservation_child_pid(launch_state) == os.getpid()
                and launch_state.get("command_fingerprint") == command_fingerprint(command)
            )
            if launch_matches:
                instance_id = str(launch_state["instance_id"])
                launch_id = _launch_id_from_state(launch_state)
                stop_requested = _stop_marker_for_state(launch_state)
            else:
                # The successful non-blocking acquire proves that no other daemon
                # owns the instance lock, so stale metadata can be removed before
                # this instance publishes its own starting state.
                _remove_state(paths)
                if _read_pid_file(paths) is not None:
                    _remove_file(paths.pid)
                instance_id = secrets.token_hex(16)
                launch_id = instance_id
                stop_requested = None
            instance = DaemonInstance(
                paths,
                lock,
                instance_id,
                launch_id=launch_id,
                stop_requested=stop_requested,
            )
            if command is not None:
                # Keep this inside the lifecycle critical section.  Once the
                # instance lock is held, stop/status can never see a live
                # daemon without at least a verifiable starting state.
                instance._publish_locked(_STARTING, command)
            return instance
        except Exception:
            # We are still inside lifecycle_lock.  Do not call instance.close()
            # here because it would try to acquire the same lifecycle lock.
            # Remove only state/pid records that this acquisition published,
            # then always release the instance lock before propagating error.
            try:
                state = _read_state(paths)
                if (
                    instance_id is not None
                    and state is not None
                    and state.get("instance_id") == instance_id
                ):
                    _remove_state(paths, expected_instance_id=instance_id)
                if _read_pid_file(paths) == os.getpid():
                    _remove_file(paths.pid)
            finally:
                lock.release()
            raise


_OWNED_CHILDREN: dict[Path, Any] = {}


def _child_key(control_dir: os.PathLike[str] | str) -> Path:
    return paths_for(control_dir).control_dir


def _drop_owned_child(paths: DaemonPaths, proc: Any) -> None:
    if _OWNED_CHILDREN.get(paths.control_dir) is proc:
        _OWNED_CHILDREN.pop(paths.control_dir, None)


def _reap_owned_child(paths: DaemonPaths) -> Optional[str]:
    proc = _OWNED_CHILDREN.get(paths.control_dir)
    if proc is None:
        return None
    try:
        if proc.poll() is None:
            return None
        proc.wait()
    except Exception as exc:
        # Ownership is deliberately retained until wait() succeeds.  A
        # failed reap must remain visible to the next lifecycle operation.
        return f"回收守护子进程失败：{type(exc).__name__}: {exc}"
    _drop_owned_child(paths, proc)
    return None


def _wait_child(proc: Any, timeout: float) -> bool:
    try:
        proc.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


def _terminate_owned_child(proc: Any, timeout: float) -> Optional[str]:
    try:
        if proc.poll() is None:
            proc.terminate()
        if not _wait_child(proc, timeout):
            proc.kill()
            if not _wait_child(proc, timeout):
                return "已创建的守护子进程无法回收"
        return None
    except Exception as exc:
        return f"回收守护子进程失败：{type(exc).__name__}: {exc}"


def _wait_until_ready(
    paths: DaemonPaths,
    timeout: float,
    proc: Any = None,
    reservation: Optional[Mapping[str, Any]] = None,
) -> DaemonStatus:
    deadline = time.monotonic() + max(0.0, timeout)
    last = _inspect_unlocked(paths)
    while time.monotonic() <= deadline:
        last = _inspect_unlocked(paths)
        if reservation is not None and _launch_cancel_requested(paths, reservation):
            return last
        if last.ready:
            return last
        if proc is not None and proc.poll() is not None:
            return last
        time.sleep(_POLL_INTERVAL)
    return last


def _same_launch_identity(
    state: Optional[Mapping[str, Any]],
    reservation: Optional[Mapping[str, Any]],
) -> bool:
    if not state or not reservation:
        return False
    state_identity = _state_identity(state)
    if state_identity is None:
        return False
    reservation_identity = _state_identity(reservation)
    if reservation_identity is None:
        instance_id = reservation.get("instance_id") or reservation.get("launch_nonce")
        launch_id = reservation.get("launch_id") or reservation.get("launch_nonce") or instance_id
        child_pid = reservation.get("pid") or reservation.get("child_pid")
        if child_pid is None:
            child_pid = _reservation_child_pid(reservation)
        fingerprint = reservation.get("fingerprint") or reservation.get("command_fingerprint")
        if (
            not isinstance(instance_id, str)
            or not isinstance(launch_id, str)
            or not isinstance(child_pid, int)
            or not isinstance(fingerprint, str)
        ):
            return False
        reservation_identity = {
            "instance_id": instance_id,
            "launch_id": launch_id,
            "pid": child_pid,
            "fingerprint": fingerprint,
        }
    return all(
        state_identity.get(key) == reservation_identity.get(key)
        for key in ("instance_id", "launch_id", "pid", "fingerprint")
    )


def _launch_cancel_requested(
    paths: DaemonPaths,
    reservation: Optional[Mapping[str, Any]],
) -> bool:
    state = _read_state(paths)
    return bool(
        state
        and _stop_marker_matches_state(state, state.get(_STOP_REQUESTED))
        and _same_launch_identity(state, reservation)
    )


def _request_stop_locked(
    paths: DaemonPaths,
    expected_identity: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    state = _read_state(paths)
    if not state:
        return None
    status = state.get("status")
    valid = (
        _valid_launch_reservation(paths, state)
        if status == _LAUNCHING
        else status in {_STARTING, _READY}
        and _valid_daemon_state(paths, state, status)
    )
    identity = _state_identity(state)
    if not valid or identity is None:
        return None
    if expected_identity is not None and any(
        identity.get(key) != expected_identity.get(key)
        for key in ("instance_id", "launch_id", "pid", "fingerprint")
    ):
        return None
    marker = state.get(_STOP_REQUESTED)
    if marker is not None:
        if not _stop_marker_matches_state(state, marker):
            return None
        return state
    updated = dict(state)
    updated[_STOP_REQUESTED] = _stop_identity(state)
    _atomic_write_json(paths.state, updated)
    return updated


def _publish_launch_reservation(
    paths: DaemonPaths,
    pid: int,
    command: Sequence[object],
    *,
    launch_nonce: Optional[str] = None,
) -> dict[str, Any]:
    normalized = _normalize_command(command)
    fingerprint = command_fingerprint(normalized)
    nonce = launch_nonce or secrets.token_hex(16)
    state = {
        "schema": 1,
        "status": _LAUNCHING,
        "pid": pid,
        "instance_id": nonce,
        "launch_id": nonce,
        "launch_nonce": nonce,
        "launcher_pid": os.getpid(),
        "launcher_nonce": nonce,
        "control_dir": str(paths.control_dir),
        "command": normalized,
        "command_fingerprint": fingerprint,
        "child_identity": {
            "pid": pid,
            "command": normalized,
            "command_fingerprint": fingerprint,
            "launch_nonce": nonce,
        },
        "launching_at": time.time(),
    }
    _atomic_write_text(paths.pid, f"{pid}\n")
    try:
        _atomic_write_json(paths.state, state)
    except Exception:
        _remove_file(paths.pid)
        raise
    return state


def start_daemon(
    control_dir: os.PathLike[str] | str,
    *,
    python_executable: Optional[os.PathLike[str] | str] = None,
    cwd: Optional[os.PathLike[str] | str] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    popen_factory: Any = subprocess.Popen,
    _stale_recovery_attempted: bool = False,
) -> StartResult:
    """Start at most one daemon and wait for its ready state."""
    control_path = paths_for(control_dir).control_dir
    if not unattended_filing_enabled(control_path):
        return StartResult(error=unattended_gate_error(), gate_blocked=True)

    child = None
    reservation: Optional[dict[str, Any]] = None
    reclaim_reservation: Optional[dict[str, Any]] = None
    publish_error: Optional[str] = None
    with lifecycle_lock(control_path) as paths:
        reap_error = _reap_owned_child(paths)
        if reap_error is not None:
            return StartResult(error=reap_error)
        current = _inspect_unlocked(paths)
        if current.running:
            return StartResult(pid=current.pid, already_running=True)

        owned = _OWNED_CHILDREN.get(paths.control_dir)
        owned_is_live = owned is not None and owned.poll() is None
        stale_child_status = (
            _launch_reservation_child_status(paths, current.state)
            if current.detail == _STALE_LAUNCHING
            else "dead"
        )
        stale_child_is_live = stale_child_status == "live"
        stale_child_is_unknown = stale_child_status == "unknown"
        stale_child_should_reclaim = (
            stale_child_is_live
            and _launch_reservation_age(current.state) is not None
            and _launch_reservation_age(current.state)
            > _LAUNCH_RESERVATION_TTL + _STALE_RESERVATION_GRACE
            and _stop_marker_for_state(current.state) is not None
        )
        stale_reservation_active = (
            current.detail == _STALE_LAUNCHING
            and (stale_child_is_live or stale_child_is_unknown)
        )
        existing_start = (
            current.detail in {_STARTING, "launching"}
            or _instance_lock_held(paths)
            or owned_is_live
            or stale_reservation_active
        )

        # A launch reservation closes the small interval between Popen and
        # the daemon taking instance lock.  A second caller waits for that
        # child rather than creating a competing instance.
        if not existing_start:
            _cleanup_stale_unlocked(paths)
            interpreter = str(Path(python_executable or sys.executable).resolve(strict=False))
            command = [interpreter, str(paths.control_dir / "auto_filer.py"), "--daemon", "-v"]
            try:
                child = popen_factory(
                    command,
                    cwd=str(cwd or paths.control_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return StartResult(error=f"守护进程启动失败：{type(exc).__name__}: {exc}")
            try:
                reservation = _publish_launch_reservation(paths, child.pid, command)
            except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
                publish_error = f"守护进程启动状态发布失败：{type(exc).__name__}: {exc}"
            _OWNED_CHILDREN[paths.control_dir] = child
        elif (
            current.detail in {_LAUNCHING, _STALE_LAUNCHING}
            and _valid_launch_reservation(paths, current.state)
        ):
            reservation = current.state
            if current.detail == _STALE_LAUNCHING and stale_reservation_active:
                identity = _state_identity(current.state)
                if identity is not None:
                    _request_stop_locked(paths, expected_identity=identity)
                    reservation = _read_state(paths)
                    if stale_child_should_reclaim:
                        reclaim_reservation = reservation

    if reclaim_reservation is not None:
        if _stale_recovery_attempted:
            return StartResult(error="stale reservation 回收后仍未建立干净的启动状态")
        owned = _OWNED_CHILDREN.get(control_path)
        child_pid = _reservation_child_pid(reclaim_reservation)
        if owned is not None and getattr(owned, "pid", None) == child_pid:
            error = _terminate_owned_child(owned, min(timeout, 1.0))
            if error:
                return StartResult(error=error)
            _drop_owned_child(paths_for(control_path), owned)
        else:
            error = _terminate_verified_reservation_child(
                paths_for(control_path), reclaim_reservation, min(timeout, 1.0)
            )
            if error:
                return StartResult(error=error)
        with lifecycle_lock(control_path) as paths:
            current = _read_state(paths)
            if (
                current
                and current.get("status") == _LAUNCHING
                and _same_launch_identity(current, reclaim_reservation)
                and not _instance_lock_held(paths)
            ):
                _remove_state(paths, expected_instance_id=current.get("instance_id"))
        # Retry once after the stale child has been safely reaped.  The retry
        # observes a clean control plane and may publish a fresh reservation.
        return start_daemon(
            control_path,
            python_executable=python_executable,
            cwd=cwd,
            timeout=timeout,
            popen_factory=popen_factory,
            _stale_recovery_attempted=True,
        )

    if publish_error is not None:
        reap_error = _terminate_owned_child(child, min(timeout, 1.0))
        if reap_error is not None:
            return StartResult(error=f"{publish_error}；{reap_error}")
        with lifecycle_lock(control_path) as paths:
            _drop_owned_child(paths, child)
            state = _read_state(paths)
            if state and _same_launch_identity(state, reservation) is False:
                _remove_launch_reservation(paths, expected_pid=getattr(child, "pid", None))
        return StartResult(error=publish_error)

    paths = paths_for(control_path)
    status = _wait_until_ready(paths, timeout, child, reservation)
    cancelled = reservation is not None and _launch_cancel_requested(paths, reservation)
    if status.running and not cancelled:
        # If an independently started daemon won the race, the child we
        # launched must be reaped and is never a reason to kill the winner.
        if child is not None and status.pid != getattr(child, "pid", None):
            reap_error = _terminate_owned_child(child, min(timeout, 1.0))
            if reap_error is not None:
                return StartResult(error=reap_error)
            _drop_owned_child(paths, child)
            return StartResult(pid=status.pid, already_running=True)
        return StartResult(pid=status.pid, process=child)

    if child is not None:
        reap_error = _terminate_owned_child(child, min(timeout, 1.0))
        if reap_error is not None:
            return StartResult(error=reap_error)
        with lifecycle_lock(control_path) as locked_paths:
            _drop_owned_child(locked_paths, child)
            state = _read_state(locked_paths)
            if (
                state
                and reservation is not None
                and _same_launch_identity(state, reservation)
                and not _instance_lock_held(locked_paths)
            ):
                _remove_state(locked_paths, expected_instance_id=state.get("instance_id"))
            elif (
                state
                and state.get("status") == _LAUNCHING
                and state.get("pid") == getattr(child, "pid", None)
                and not _instance_lock_held(locked_paths)
            ):
                _remove_state(locked_paths, expected_instance_id=state.get("instance_id"))
    # A competing start may have published just after the last poll.
    with lifecycle_lock(control_path) as locked_paths:
        winner = _inspect_unlocked(locked_paths)
        if winner.running and not cancelled:
            return StartResult(pid=winner.pid, already_running=True)
    if cancelled:
        return StartResult(error="守护进程启动已被停止请求取消")
    detail = "守护进程未在期限内发布 ready state" if child is not None else "已有守护进程启动未在期限内就绪"
    return StartResult(error=detail)


def stop_daemon(
    control_dir: os.PathLike[str] | str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> StopResult:
    """Stop one verified daemon without converting a state PID into a signal.

    A child started by this process is terminated through its owning Popen
    object.  An instance owned by another process is stopped by writing an
    exact-identity ``stop_requested`` marker under the lifecycle lock and
    waiting for that instance to clean itself up.  A launching reservation is
    allowed to transition only into that same instance, so a later ready state
    cannot be missed or confused with a new daemon.
    """
    control_path = paths_for(control_dir).control_dir
    deadline = time.monotonic() + max(0.0, timeout)
    target_identity: Optional[dict[str, Any]] = None
    last_error = "守护进程未在期限内真正退出，state/instance lock 已保留"

    while True:
        if time.monotonic() > deadline:
            return StopResult(False, last_error)

        owned_to_stop = None
        wait_for_transition = False

        with lifecycle_lock(control_path) as paths:
            reap_error = _reap_owned_child(paths)
            if reap_error is not None:
                return StopResult(False, reap_error)
            status = _inspect_unlocked(paths)
            state = status.state
            lock_held = _instance_lock_held(paths)

            if target_identity is None:
                if status.detail == _STALE_LAUNCHING and state:
                    child_status = _launch_reservation_child_status(paths, state)
                    if child_status in {"dead", "mismatch", "invalid"}:
                        _cleanup_stale_unlocked(paths)
                        return StopResult(True)
                    target_identity = _state_identity(state)
                    if target_identity is None:
                        return StopResult(False, "停止目标身份无效，未发送信号")
                elif status.detail in {_LAUNCHING, _STARTING, _READY} and state:
                    target_identity = _state_identity(state)
                    if target_identity is None:
                        return StopResult(False, "停止目标身份无效，未发送信号")
                elif not lock_held:
                    _cleanup_stale_unlocked(paths)
                    return StopResult(True)
                else:
                    wait_for_transition = True

            if target_identity is not None and not wait_for_transition:
                active = status.detail in {_LAUNCHING, _STARTING, _READY, _STALE_LAUNCHING}
                same_target = _same_identity(state, target_identity)

                if active and not same_target:
                    return StopResult(False, "停止期间守护进程身份发生变化，未发送信号")

                if active and same_target:
                    owned = _OWNED_CHILDREN.get(paths.control_dir)
                    if (
                        owned is not None
                        and getattr(owned, "pid", None) == target_identity["pid"]
                    ):
                        owned_to_stop = owned
                    elif _request_stop_locked(paths, expected_identity=target_identity) is None:
                        return StopResult(False, "停止目标身份已变化，未发送信号")
                    else:
                        wait_for_transition = True
                elif not lock_held:
                    # The target exited or its reservation was removed.  Only
                    # metadata is cleaned here; no PID is ever signalled.
                    _cleanup_stale_unlocked(paths)
                    return StopResult(True)
                else:
                    wait_for_transition = True

        if owned_to_stop is not None:
            remaining = max(0.05, deadline - time.monotonic())
            error = _terminate_owned_child(owned_to_stop, min(remaining, 1.0))
            if error:
                return StopResult(False, error)
            _drop_owned_child(paths_for(control_path), owned_to_stop)

        if time.monotonic() > deadline:
            return StopResult(False, last_error)
        # An unowned instance observes stop_requested and releases its own
        # state and instance lock.  Never signal the PID from state.
        time.sleep(_POLL_INTERVAL)


def _print_status(control_dir: Path, as_json: bool) -> int:
    status = daemon_status(control_dir)
    if as_json:
        print(json.dumps(status.as_dict(), ensure_ascii=False, sort_keys=True))
    elif status.running:
        print(f"✅ 守护进程运行中 (PID: {status.pid}, ready)")
    elif status.detail in {_STARTING, "launching"}:
        print("⏳ 守护进程正在启动，尚未 ready")
    else:
        print("⚪ 守护进程未运行")
    return 0


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="auto_filer daemon lifecycle controller")
    parser.add_argument("command", choices=("start", "stop", "status", "env-value"))
    parser.add_argument("--control-dir", default=str(Path(__file__).parent))
    parser.add_argument("--python", dest="python_executable")
    parser.add_argument("--cwd")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--key")
    parser.add_argument("--default", default="")
    args = parser.parse_args(argv)
    control_dir = Path(args.control_dir).resolve(strict=False)

    if args.command == "env-value":
        if not args.key:
            parser.error("env-value 需要 --key")
        print(env_value(control_dir, args.key, args.default))
        return 0
    if args.command == "status":
        return _print_status(control_dir, args.as_json)
    if args.command == "start":
        result = start_daemon(
            control_dir,
            python_executable=args.python_executable,
            cwd=args.cwd,
            timeout=args.timeout,
        )
        if not result.ok:
            print(f"❌ {result.error}", file=sys.stderr)
            return 2 if result.gate_blocked else 1
        if result.already_running:
            print(f"⚠ 已在运行 (PID: {result.pid})")
        else:
            print(f"✅ 守护进程已启动 (PID: {result.pid}, ready)")
        return 0

    result = stop_daemon(control_dir, timeout=args.timeout)
    if not result.ok:
        print(f"❌ {result.error}", file=sys.stderr)
        return 1
    print("🛑 守护进程已停止" if result.error is None else result.error)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
