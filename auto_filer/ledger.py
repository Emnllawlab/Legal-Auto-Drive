"""Small local SQLite ledger for reversible filing transactions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FilingLedger:
    def __init__(self, path: str | Path):
        raw_path = Path(path).expanduser()
        if raw_path.is_symlink():
            raise LedgerError(f"拒绝使用符号链接台账：{raw_path}")
        self.path = raw_path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tighten_db_permissions()
        self._init_db()

    def _tighten_db_permissions(self):
        """Keep the ledger private, including when an old DB was wider."""
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"无法检查台账权限：{self.path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise LedgerError(f"拒绝使用符号链接台账：{self.path}")
        if not stat.S_ISREG(info.st_mode):
            raise LedgerError(f"台账路径不是普通文件：{self.path}")
        try:
            os.chmod(str(self.path), 0o600)
        except OSError as exc:
            raise LedgerError(f"无法收紧台账权限：{self.path}") from exc

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            # sqlite3 creates a missing file during connect; chmod again here
            # so both newly created and pre-existing ledgers are covered.
            self._tighten_db_permissions()
        except Exception:
            conn.close()
            raise
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                committed_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id TEXT NOT NULL REFERENCES transactions(id),
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                sha256 TEXT,
                original_name TEXT,
                new_name TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_moves_tx ON moves(transaction_id);
            """)

    def snapshot_sidecars(self, source: str | Path) -> list[dict[str, Any]]:
        """Capture intake sidecars before their source file is moved.

        The snapshot is intentionally embedded in transaction metadata so an
        undo never depends on a sidecar still being next to the archived file.
        Symlinks and non-regular sidecars are rejected rather than followed.
        """
        source = Path(source).expanduser()
        result = []
        for suffix in (".loci.json", ".pending.json"):
            path = source.parent / f".{source.name}{suffix}"
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise LedgerError(f"无法读取上传 sidecar：{path}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise LedgerError(f"拒绝使用符号链接 sidecar：{path}")
            if not stat.S_ISREG(info.st_mode):
                raise LedgerError(f"sidecar 不是普通文件：{path}")
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise LedgerError(f"无法保存上传 sidecar：{path}") from exc
            result.append({
                "path": str(path.resolve(strict=False)),
                "content_b64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": stat.S_IMODE(info.st_mode),
            })
        return result

    def record_move(self, source: str | Path, destination: str | Path, *,
                    action: str = "archive", metadata: dict[str, Any] | None = None) -> str:
        source = str(Path(source).expanduser().resolve(strict=False))
        destination_path = Path(destination).expanduser().resolve(strict=False)
        txid = uuid.uuid4().hex
        digest = sha256_file(destination_path) if destination_path.is_file() else None
        metadata = dict(metadata or {})
        metadata.setdefault("decision_id", None)
        metadata.setdefault("batch_id", None)
        metadata.setdefault("rule_version", None)
        metadata.setdefault("source_path", source)
        metadata.setdefault("destination_path", str(destination_path))
        metadata.setdefault("original_name", Path(source).name)
        metadata.setdefault("new_name", destination_path.name)
        metadata.setdefault("sha256", digest)
        metadata.setdefault("source_exists_after", Path(source).exists())
        if "sidecars" not in metadata:
            metadata["sidecars"] = self.snapshot_sidecars(source)
        with self._connect() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("INSERT INTO transactions(id, action, status, created_at, committed_at, metadata) VALUES(?,?,?,?,?,?)",
                         (txid, action, "committed", now, now, json.dumps(metadata, ensure_ascii=False)))
            conn.execute("INSERT INTO moves(transaction_id, source, destination, sha256, original_name, new_name) VALUES(?,?,?,?,?,?)",
                         (txid, source, str(destination_path), digest, Path(source).name, destination_path.name))
        return txid

    def list_transactions(self, limit: int = 50, *, undoable: bool = False,
                          batch_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE (? = 0 OR status = 'committed') "
                "ORDER BY created_at DESC LIMIT ?",
                (1 if undoable else 0, int(limit)),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    metadata = json.loads(item.get("metadata") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                item["metadata_fields"] = metadata
                for field in ("decision_id", "batch_id", "rule_version", "source_path",
                              "destination_path", "original_name", "new_name", "sha256"):
                    item[field] = metadata.get(field)
                if batch_id is None or item.get("batch_id") == batch_id:
                    result.append(item)
            return result

    def undo(self, transaction_id: str, allowed_roots: list[str | Path] | None = None,
             *, dry_run: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
            if not tx:
                raise LedgerError("事务不存在")
            if tx["status"] == "undone":
                raise LedgerError("事务已撤销")
            moves = conn.execute("SELECT * FROM moves WHERE transaction_id = ? ORDER BY id DESC", (transaction_id,)).fetchall()
            if not moves:
                raise LedgerError("事务没有文件操作")
            roots = [Path(root).expanduser().resolve() for root in (allowed_roots or [])]

            try:
                metadata = json.loads(tx["metadata"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerError("事务元数据无效，拒绝撤销") from exc
            if not isinstance(metadata, dict):
                raise LedgerError("事务元数据无效，拒绝撤销")
            raw_sidecars = metadata.get("sidecars", [])
            if raw_sidecars is None:
                raw_sidecars = []
            if not isinstance(raw_sidecars, list):
                raise LedgerError("事务 sidecar 元数据无效，拒绝撤销")
            raw_links = metadata.get("links", [])
            if raw_links is None:
                raw_links = []
            if not isinstance(raw_links, list):
                raise LedgerError("事务链接元数据无效，拒绝撤销")

            links: list[Path] = []
            seen_links: set[str] = set()
            for raw_link in raw_links:
                if not isinstance(raw_link, str) or not raw_link:
                    raise LedgerError("事务链接元数据无效，拒绝撤销")
                try:
                    link = Path(raw_link).expanduser()
                    if not link.is_absolute():
                        raise LedgerError("事务链接路径必须是绝对路径")
                    link_key = str(link)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise LedgerError("事务链接路径无效，拒绝撤销") from exc
                if link_key in seen_links:
                    continue
                seen_links.add(link_key)
                links.append(link)

            def under_allowed(path: Path) -> bool:
                if not roots:
                    return True
                resolved = path.expanduser().resolve(strict=False)
                return any(resolved == root or root in resolved.parents for root in roots)

            def link_under_allowed(path: Path) -> bool:
                """Check the link's location without following its final leaf."""
                if not roots:
                    return True
                parent = path.parent.expanduser().resolve(strict=False)
                return any(parent == root or root in parent.parents for root in roots)

            sidecars: list[tuple[Path, bytes, int]] = []
            seen_sidecars: set[str] = set()
            for raw_sidecar in raw_sidecars:
                if not isinstance(raw_sidecar, dict):
                    raise LedgerError("事务 sidecar 元数据无效，拒绝撤销")
                raw_path = raw_sidecar.get("path")
                encoded = raw_sidecar.get("content_b64")
                if not isinstance(raw_path, str) or not raw_path:
                    raise LedgerError("事务 sidecar 路径无效，拒绝撤销")
                if not isinstance(encoded, str):
                    raise LedgerError("事务 sidecar 内容无效，拒绝撤销")
                try:
                    sidecar = Path(raw_path).expanduser()
                    if not sidecar.is_absolute() or not under_allowed(sidecar):
                        raise LedgerError("事务 sidecar 不在当前工作区允许范围内")
                    sidecar_key = str(sidecar)
                    if sidecar_key in seen_sidecars:
                        raise LedgerError("事务含有重复 sidecar")
                    seen_sidecars.add(sidecar_key)
                    content = base64.b64decode(encoded.encode("ascii"), validate=True)
                    expected_hash = raw_sidecar.get("sha256")
                    if expected_hash and hashlib.sha256(content).hexdigest() != expected_hash:
                        raise LedgerError("事务 sidecar 内容校验失败，拒绝撤销")
                    mode = int(raw_sidecar.get("mode") or 0o600)
                    mode &= 0o777
                    if not mode:
                        mode = 0o600
                except LedgerError:
                    raise
                except (ValueError, TypeError, OSError, UnicodeError) as exc:
                    raise LedgerError("事务 sidecar 内容无效，拒绝撤销") from exc
                if os.path.lexists(str(sidecar)):
                    # During an automatic rollback the source file may have
                    # been moved back before its sidecars were cleaned.  An
                    # identical regular sidecar is already restored; accept
                    # it without replacing it.  Any changed, symlinked, or
                    # non-regular entry remains a hard safety failure.
                    try:
                        info = sidecar.lstat()
                        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                                or hashlib.sha256(sidecar.read_bytes()).hexdigest()
                                != (raw_sidecar.get("sha256") or "")):
                            raise LedgerError(f"sidecar 原位置已有不同文件，拒绝覆盖：{sidecar}")
                    except LedgerError:
                        raise
                    except OSError as exc:
                        raise LedgerError(f"无法校验已有 sidecar：{sidecar}") from exc
                    continue
                sidecars.append((sidecar, content, mode))

            for move in moves:
                src = Path(move["source"])
                dest = Path(move["destination"])
                if not under_allowed(src) or not under_allowed(dest):
                    raise LedgerError("事务路径不在当前工作区允许范围内")
                if os.path.lexists(str(src)) and src.is_symlink():
                    raise LedgerError(f"原位置是符号链接，拒绝撤销：{src}")
                if dest.is_symlink():
                    raise LedgerError(f"目标位置是符号链接，拒绝撤销：{dest}")
                if not dest.exists():
                    raise LedgerError(f"目标文件不存在，无法撤销：{dest}")
                if src.exists():
                    raise LedgerError(f"原位置已有文件，拒绝覆盖：{src}")
                if move["sha256"] and sha256_file(dest) != move["sha256"]:
                    raise LedgerError(f"目标文件内容已变化，拒绝撤销：{dest}")

            destination_paths = []
            for move in moves:
                try:
                    destination_paths.append(Path(move["destination"]).resolve(strict=False))
                except (OSError, RuntimeError, ValueError) as exc:
                    raise LedgerError("事务目标路径无效，拒绝撤销") from exc

            link_records: list[tuple[Path, str]] = []
            for link in links:
                try:
                    if not link_under_allowed(link):
                        raise LedgerError(f"跨案链接不在当前工作区允许范围内：{link}")
                    if not link.is_symlink():
                        raise LedgerError(f"跨案链接已不存在或不是符号链接：{link}")
                    resolved_target = link.resolve(strict=True)
                    if resolved_target not in destination_paths:
                        raise LedgerError(f"跨案链接指向非本事务目标，拒绝撤销：{link}")
                    link_target = os.readlink(str(link))
                except LedgerError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    raise LedgerError(f"无法验证跨案链接，拒绝撤销：{link}") from exc
                link_records.append((link, link_target))

            if dry_run:
                return {"id": transaction_id, "status": "validated",
                        "moves": len(moves), "links": len(link_records),
                        "sidecars": len(sidecars)}

            removed_links: list[tuple[Path, str]] = []

            def recreate_links() -> None:
                """Best-effort recreation without replacing any existing path."""
                for link, link_target in removed_links:
                    try:
                        if os.path.lexists(str(link)):
                            continue
                        os.symlink(link_target, str(link))
                    except (OSError, RuntimeError, ValueError):
                        continue

            try:
                for link, link_target in link_records:
                    # Re-check immediately before unlinking so a changed link is
                    # never removed after the all-links validation pass.
                    if not link.is_symlink() or link.resolve(strict=True) not in destination_paths:
                        raise LedgerError(f"跨案链接已变化，拒绝撤销：{link}")
                    os.unlink(str(link))
                    removed_links.append((link, link_target))
            except LedgerError:
                recreate_links()
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                recreate_links()
                raise LedgerError("删除跨案链接失败，拒绝撤销") from exc

            restored_sidecars: list[Path] = []
            moved_back = False
            try:
                for move in moves:
                    src, dest = Path(move["source"]), Path(move["destination"])
                    src.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(dest), str(src))
                moved_back = True
                for sidecar, content, mode in sidecars:
                    sidecar.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(str(sidecar), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                  mode)
                    try:
                        os.fchmod(fd, mode)
                        with os.fdopen(fd, "wb") as handle:
                            fd = None
                            handle.write(content)
                            handle.flush()
                            os.fsync(handle.fileno())
                            os.fchmod(handle.fileno(), mode)
                    finally:
                        if fd is not None:
                            os.close(fd)
                    restored_sidecars.append(sidecar)
            except (OSError, RuntimeError, ValueError) as exc:
                for sidecar in reversed(restored_sidecars):
                    try:
                        sidecar.unlink(missing_ok=True)
                    except OSError:
                        pass
                if moved_back:
                    try:
                        for move in reversed(moves):
                            src, dest = Path(move["source"]), Path(move["destination"])
                            if src.exists() and not os.path.lexists(str(dest)):
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                os.replace(str(src), str(dest))
                    except (OSError, RuntimeError, ValueError):
                        pass
                recreate_links()
                raise LedgerError("恢复主文件或 sidecar 失败，已尝试恢复事务") from exc
            conn.execute("UPDATE transactions SET status = ?, committed_at = ? WHERE id = ?",
                         ("undone", datetime.now().isoformat(timespec="seconds"), transaction_id))
        return {"id": transaction_id, "status": "undone", "moves": len(moves),
                "links_removed": len(removed_links),
                "sidecars_restored": len(restored_sidecars)}

    def undo_batch(self, batch_id: str,
                   allowed_roots: list[str | Path] | None = None) -> dict[str, Any]:
        """Preflight every committed transaction before changing any file."""
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            raise LedgerError("批次编号不能为空")
        transactions = self.list_transactions(limit=10000, undoable=True,
                                               batch_id=batch_id)
        if not transactions:
            raise LedgerError("可撤销批次不存在")
        ids = [item["id"] for item in transactions]
        # This pass performs all target/hash/original-path/link validation while
        # no file has yet been moved.  It prevents the common partial-batch undo.
        for txid in ids:
            self.undo(txid, allowed_roots=allowed_roots, dry_run=True)
        results = []
        for txid in ids:
            results.append(self.undo(txid, allowed_roots=allowed_roots))
        return {"batch_id": batch_id, "status": "undone",
                "transactions": results, "count": len(results)}
