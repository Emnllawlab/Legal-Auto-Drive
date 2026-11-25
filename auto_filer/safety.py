"""Shared local filesystem safety helpers.

These helpers are intentionally small and dependency-free so both Flask
frontends and the archive worker enforce the same rules.  They do not make a
path safe by rewriting it: invalid user input is rejected.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported runtime is macOS/Linux
    fcntl = None


MAX_CONTENT_LENGTH = 250 * 1024 * 1024
MAX_UPLOAD_FILES = 100


class SafetyError(ValueError):
    """Raised when a client-controlled name or path is outside the policy."""


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_control_chars(value: str, field: str) -> None:
    if any(ord(ch) < 32 for ch in value):
        raise SafetyError(f"{field} 含非法控制字符")


def _reject_drive_or_absolute(value: str, field: str) -> None:
    candidate = Path(value)
    windows = PureWindowsPath(value)
    if candidate.is_absolute() or windows.is_absolute() or windows.drive:
        raise SafetyError(f"{field} 不得为绝对路径")


def validate_basename(value: str, field: str = "文件名") -> str:
    """Validate a single filename component without sanitizing it."""
    if not isinstance(value, str) or not value or value.strip() == "":
        raise SafetyError(f"{field}不能为空")
    if value in (".", ".."):
        raise SafetyError(f"{field}不能是 . 或 ..")
    if "\x00" in value:
        raise SafetyError(f"{field}不能包含 NUL")
    if "/" in value or "\\" in value:
        raise SafetyError(f"{field}只能是单一文件名，不能包含路径分隔符")
    _reject_control_chars(value, field)
    _reject_drive_or_absolute(value, field)
    return value


def validate_case_name(value: str, field: str = "案件名称") -> str:
    """Validate a case name that must remain one safe directory component."""
    value = validate_basename(value, field)
    if value.startswith("."):
        raise SafetyError(f"{field}不能以 . 开头")
    return value


def validate_relative_target(value: str, field: str = "目标目录") -> str:
    """Validate a slash-separated relative directory target.

    Empty segments, dot segments, Windows separators/drives and NUL are all
    rejected.  The returned value is the original validated string.
    """
    if not isinstance(value, str) or not value or value.strip() == "":
        raise SafetyError(f"{field}不能为空")
    if "\x00" in value:
        raise SafetyError(f"{field}不能包含 NUL")
    if "\\" in value:
        raise SafetyError(f"{field}不能包含反斜线")
    _reject_control_chars(value, field)
    _reject_drive_or_absolute(value, field)
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SafetyError(f"{field}含空段或非法段")
    return value


def validate_relative_file_path(value: str, field: str = "原始相对路径") -> str:
    """Validate a browser-supplied relative file path for audit metadata.

    The path is never used as the upload destination: uploaded files still go
    into the flat inbox.  Keeping the validated relative path lets Loci retain
    useful batch/folder context without allowing directory traversal.
    """
    if not isinstance(value, str) or not value or value.strip() == "":
        raise SafetyError(f"{field}不能为空")
    if len(value) > 1024:
        raise SafetyError(f"{field}过长")
    if "\x00" in value:
        raise SafetyError(f"{field}不能包含 NUL")
    if "\\" in value:
        raise SafetyError(f"{field}不能包含反斜线")
    _reject_control_chars(value, field)
    _reject_drive_or_absolute(value, field)
    parts = value.split("/")
    if len(parts) > 24:
        raise SafetyError(f"{field}层级过深")
    if any(part in ("", ".", "..") for part in parts):
        raise SafetyError(f"{field}含空段或非法段")
    for part in parts:
        validate_basename(part, field)
    return "/".join(parts)


def resolve_under(root: os.PathLike | str, candidate: os.PathLike | str) -> Path:
    """Resolve a path, including a not-yet-created leaf, under ``root``."""
    root_resolved = Path(root).expanduser().resolve()
    candidate_resolved = Path(candidate).expanduser().resolve(strict=False)
    if candidate_resolved != root_resolved and not is_relative_to(candidate_resolved, root_resolved):
        raise SafetyError("路径不在允许目录内")
    return candidate_resolved


def safe_case_root(root: os.PathLike | str, case_name: str) -> Path:
    validate_case_name(case_name)
    return resolve_under(root, Path(root) / case_name)


def safe_target_path(root: os.PathLike | str, target: str) -> Path:
    validate_relative_target(target)
    return resolve_under(root, Path(root).joinpath(*target.split("/")))


def existing_regular_file(path: os.PathLike | str,
                          allowed_roots: Iterable[os.PathLike | str]) -> Path:
    """Return a resolved existing regular file under one of ``allowed_roots``.

    Final symlinks are rejected.  Intermediate symlinks are resolved and must
    still land below an allowed root, which prevents symlink escape.
    """
    try:
        raw = Path(path)
        if "\x00" in str(path):
            raise SafetyError("路径不能包含 NUL")
        if raw.is_symlink():
            raise SafetyError("不允许操作符号链接")
        resolved = raw.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SafetyError("路径无效") from exc

    roots = [Path(r).expanduser().resolve() for r in allowed_roots]
    if not any(is_relative_to(resolved, root) and resolved != root for root in roots):
        raise SafetyError("文件不在允许目录内")
    if not raw.exists() or not resolved.exists():
        raise FileNotFoundError(str(path))
    try:
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise SafetyError("只允许操作现有普通文件")
    except OSError as exc:
        raise SafetyError("无法检查文件") from exc
    return resolved


def open_unique_upload(root: os.PathLike | str, filename: str):
    """Open a new, non-overwriting upload file and return ``(file, path)``.

    ``O_EXCL`` closes the check-then-create race between concurrent uploads and
    also refuses to replace an existing symlink.
    """
    filename = validate_basename(filename, "上传文件名")
    root_resolved = Path(root).expanduser().resolve()
    root_resolved.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(0, 100000):
        candidate_name = filename if index == 0 else f"{stem}_{index}{suffix}"
        candidate = resolve_under(root_resolved, root_resolved / candidate_name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(candidate), flags, 0o600)
        except FileExistsError:
            continue
        return os.fdopen(fd, "wb"), candidate
    raise SafetyError("无法为上传文件分配不冲突的文件名")


def save_upload(file_storage, root: os.PathLike | str, filename: str):
    """Save a Flask ``FileStorage`` without overwriting an existing file."""
    stream, path = open_unique_upload(root, filename)
    try:
        file_storage.save(stream)
        stream.flush()
        os.fsync(stream.fileno())
    except Exception:
        try:
            stream.close()
        finally:
            path.unlink(missing_ok=True)
        raise
    else:
        stream.close()
    return path


class LockBusy(RuntimeError):
    """A non-blocking process lock is already held by another process."""


class FileLock:
    """Small fcntl-backed lock usable as a context manager."""

    def __init__(self, path: os.PathLike | str, blocking: bool = True):
        self.path = Path(path)
        self.blocking = blocking
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+")
        if fcntl is None:  # pragma: no cover - supported platform has fcntl
            return self
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self._handle.fileno(), flags)
        except OSError as exc:
            if self._handle:
                self._handle.close()
                self._handle = None
            if not self.blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(f"锁已被其他进程占用：{self.path}") from exc
            raise
        return self

    def release(self):
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
