"""Atomic, cross-process-safe YAML configuration updates."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

try:  # package import
    from .safety import FileLock
except ImportError:  # legacy direct import after adding auto_filer/ to sys.path
    from safety import FileLock


_BACKUP_LIMIT = 3
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700


def set_private_umask() -> int:
    """Set the process umask used for local case-control-plane files.

    The previous mask is returned for callers/tests that need to observe the
    change.  We intentionally do not restore it during normal application
    startup: worker threads and child processes must inherit the private mask.
    """
    return os.umask(0o077)


def _security_log(logger, level: int, message: str, *args) -> None:
    (logger or logging.getLogger(__name__)).log(level, message, *args)


def _tighten_regular_file(path: Path, label: str, logger) -> bool:
    """Make one existing, non-symlink regular file private.

    Permission failures are deliberately non-fatal for startup, but are
    surfaced to the supplied logger.  Skipping symlinks prevents a log/config
    path from changing permissions on an unrelated target (including case
    material).
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        _security_log(logger, logging.WARNING, "%s 不存在，跳过权限收紧：%s", label, path)
        return False
    except OSError as exc:
        _security_log(logger, logging.WARNING,
                      "%s 无法检查权限，安全跳过：%s（%s）",
                      label, path, type(exc).__name__)
        return False
    if stat.S_ISLNK(info.st_mode):
        _security_log(logger, logging.WARNING, "%s 是符号链接，安全跳过：%s", label, path)
        return False
    if not stat.S_ISREG(info.st_mode):
        _security_log(logger, logging.WARNING, "%s 不是普通文件，安全跳过：%s", label, path)
        return False
    try:
        os.chmod(str(path), _PRIVATE_FILE_MODE)
        return True
    except OSError as exc:
        _security_log(logger, logging.WARNING,
                      "%s 权限收紧失败，安全跳过：%s（%s）",
                      label, path, type(exc).__name__)
        return False


def tighten_private_permissions(env_path=None, config_path=None, logs_dir=None,
                                logger=None):
    """Tighten only control-plane paths; never recurse into case materials.

    ``logs_dir`` is inspected one level deep.  All regular log files (SQLite
    included) become 0600, while the directory itself becomes 0700.  Missing
    paths and permission errors are reported and skipped so a launch cannot
    silently claim that its privacy boundary was applied.
    """
    tightened = []

    if env_path is not None:
        path = Path(env_path).expanduser()
        if _tighten_regular_file(path, ".env", logger):
            tightened.append(path)

    if config_path is not None:
        path = Path(config_path).expanduser()
        if _tighten_regular_file(path, "活动配置", logger):
            tightened.append(path)
        # Existing legacy and timestamped config backups are control-plane
        # files too.  Do not follow symlinks and do not inspect any other
        # directory contents.
        try:
            backups = sorted(path.parent.glob(path.name + ".bak*"))
        except OSError as exc:
            backups = []
            _security_log(logger, logging.WARNING,
                          "配置备份目录无法检查，安全跳过：%s（%s）",
                          path.parent, type(exc).__name__)
        for backup in backups:
            if _tighten_regular_file(backup, "配置备份", logger):
                tightened.append(backup)

    if logs_dir is not None:
        path = Path(logs_dir).expanduser()
        try:
            info = path.lstat()
        except FileNotFoundError:
            _security_log(logger, logging.WARNING, "日志目录不存在，跳过权限收紧：%s", path)
            return tightened
        except OSError as exc:
            _security_log(logger, logging.WARNING,
                          "日志目录无法检查，安全跳过：%s（%s）",
                          path, type(exc).__name__)
            return tightened
        if stat.S_ISLNK(info.st_mode):
            _security_log(logger, logging.WARNING, "日志目录是符号链接，安全跳过：%s", path)
            return tightened
        if not stat.S_ISDIR(info.st_mode):
            _security_log(logger, logging.WARNING, "日志路径不是目录，安全跳过：%s", path)
            return tightened
        try:
            os.chmod(str(path), _PRIVATE_DIR_MODE)
            tightened.append(path)
        except OSError as exc:
            _security_log(logger, logging.WARNING,
                          "日志目录权限收紧失败，安全跳过：%s（%s）",
                          path, type(exc).__name__)
        try:
            entries = list(path.iterdir())
        except OSError as exc:
            _security_log(logger, logging.WARNING,
                          "日志文件列表无法读取，安全跳过：%s（%s）",
                          path, type(exc).__name__)
            return tightened
        for entry in entries:
            if _tighten_regular_file(entry, "日志文件", logger):
                tightened.append(entry)

    return tightened


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _read_unlocked(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return default if data is None else data


def load_yaml(path: os.PathLike | str, default=None):
    path = Path(path)
    with FileLock(_lock_path(path), blocking=True):
        return _read_unlocked(path, default)


def _prune_backups(path: Path, limit: int) -> None:
    backups = sorted(path.parent.glob(path.name + ".bak.*"),
                     key=lambda p: p.stat().st_mtime_ns if p.exists() else 0,
                     reverse=True)
    # A retained backup may predate the private-file policy.  Tighten it while
    # it is already in the small, known backup set; never follow symlinks.
    for backup in backups:
        if backup.is_symlink():
            continue
        try:
            if backup.is_file():
                os.chmod(str(backup), _PRIVATE_FILE_MODE)
        except OSError:
            pass
    for old in backups[limit:]:
        try:
            old.unlink()
        except OSError:
            pass


def _atomic_replace_unlocked(path: Path, text: str, backup_limit: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = path.with_name(path.name + f".bak.{stamp}")
        # A copy is intentional: the old configuration remains recoverable
        # even after os.replace swaps in the new inode.
        backup.write_bytes(path.read_bytes())
        os.chmod(str(backup), _PRIVATE_FILE_MODE)
        with backup.open("rb") as fh:
            os.fsync(fh.fileno())
        _prune_backups(path, backup_limit)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        # Do not inherit a wider mode from an existing configuration.  This
        # is explicit in addition to umask so direct library callers are safe.
        os.chmod(tmp_name, _PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def dump_yaml(path: os.PathLike | str, data, backup_limit: int = _BACKUP_LIMIT) -> None:
    path = Path(path)
    text = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                     sort_keys=False, width=120)
    with FileLock(_lock_path(path), blocking=True):
        _atomic_replace_unlocked(path, text, backup_limit)


def update_yaml(path: os.PathLike | str, mutator: Callable, default=None,
                backup_limit: int = _BACKUP_LIMIT):
    """Read, mutate and atomically replace one YAML document under one lock."""
    path = Path(path)
    with FileLock(_lock_path(path), blocking=True):
        data = _read_unlocked(path, default)
        result = mutator(data)
        if result is not None:
            data = result
        text = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                         sort_keys=False, width=120)
        _atomic_replace_unlocked(path, text, backup_limit)
        return data
