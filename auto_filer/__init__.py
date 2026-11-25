"""Compatibility package for the legacy ``auto_filer.py`` engine.

Historically the engine was imported as a top-level module after adding the
``auto_filer`` directory to ``sys.path``.  The Phase 2 decision/ledger modules
make the directory importable as a package as well; re-exporting the legacy
engine keeps both import styles working during migration.
"""

import sys
import hashlib
from pathlib import Path

from . import safety as _safety

# Keep the historical top-level ``safety`` import and the package import
# pointing at the same module, otherwise SafetyError identity differs when
# old and new modules are loaded in one test process.
sys.modules.setdefault("safety", _safety)

from .auto_filer import *  # noqa: F401,F403,E402
from .auto_filer import _process_file_unlocked, _resolve_dest  # legacy helpers


def process_file(file_path, config, logger, dry_run=False):
    """Package-style compatibility wrapper with patchable worker function."""
    file_path = Path(file_path)
    lock_key = hashlib.sha256(str(file_path.resolve(strict=False)).encode("utf-8")).hexdigest()
    lock_path = file_path.parent / ".loci_locks" / f"{lock_key}.lock"
    try:
        with _safety.FileLock(lock_path, blocking=False):
            return _process_file_unlocked(file_path, config, logger, dry_run)
    except _safety.LockBusy:
        logger.warning(f"   ⏳ 已由另一归档进程处理，跳过: {file_path.name}")
        return None
