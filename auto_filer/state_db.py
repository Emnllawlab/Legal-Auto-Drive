"""Private SQLite storage for frozen filing decisions.

The decision engine remains pure.  This small store only freezes the exact
preview shown to the user so a later commit cannot silently re-run changed
rules or select a different candidate.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class DecisionStoreError(RuntimeError):
    pass


class DecisionStore:
    def __init__(self, path: str | Path):
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise DecisionStoreError(f"拒绝使用符号链接决策库：{raw}")
        self.path = raw.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise DecisionStoreError(f"拒绝使用符号链接决策库目录：{self.path.parent}")
        try:
            os.chmod(str(self.path.parent), 0o700)
        except OSError as exc:
            raise DecisionStoreError(f"无法收紧决策库目录权限：{self.path.parent}") from exc
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            os.chmod(str(self.path), 0o600)
        except OSError as exc:
            conn.close()
            raise DecisionStoreError(f"无法收紧决策库权限：{self.path}") from exc
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS filing_batches (
                    batch_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    expected_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS filing_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES filing_batches(batch_id),
                    decision_id TEXT,
                    source_path TEXT NOT NULL,
                    destination_path TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    sidecars TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, source_path)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_items_batch "
                "ON filing_batch_items(batch_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_batch_items_decision "
                "ON filing_batch_items(decision_id)"
            )

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DecisionStoreError("决策必须是对象")
        decision_id = str(payload.get("decision_id") or uuid.uuid4().hex)
        frozen = json.loads(json.dumps({**payload, "decision_id": decision_id},
                                       ensure_ascii=False))
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO decisions(id,status,payload,created_at,committed_at) "
                "VALUES(?,?,?,?,NULL)",
                (decision_id, frozen.get("status", "pending_review"),
                 json.dumps(frozen, ensure_ascii=False), now),
            )
        return frozen

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (str(decision_id),)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DecisionStoreError("冻结决策内容损坏") from exc
        if not isinstance(payload, dict):
            raise DecisionStoreError("冻结决策格式无效")
        payload["decision_id"] = row["id"]
        payload["stored_status"] = row["status"]
        return payload

    def mark_committed(self, decision_id: str) -> None:
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE decisions SET status='committed', committed_at=? "
                "WHERE id=? AND status != 'committed'",
                (datetime.now().isoformat(timespec="seconds"), str(decision_id)),
            ).rowcount
        if not changed:
            raise DecisionStoreError("决策不存在或已提交")

    def restore_status(self, decision_id: str, status: str) -> None:
        """Restore a frozen decision to the status it had before commit."""
        decision_id = str(decision_id or "").strip()
        status = str(status or "").strip()
        if not decision_id or not status or status == "committed":
            raise DecisionStoreError("恢复决策状态参数无效")
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE decisions SET status=?, committed_at=NULL WHERE id=?",
                (status, decision_id),
            ).rowcount
        if not changed:
            raise DecisionStoreError("决策不存在，无法恢复状态")

    @staticmethod
    def _validate_batch_id(batch_id: str) -> str:
        batch_id = str(batch_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", batch_id):
            raise DecisionStoreError("批次编号格式无效")
        return batch_id

    @staticmethod
    def _json_object(value, message: str) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise DecisionStoreError(message)
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _json_list(value, message: str) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            raise DecisionStoreError(message)
        return json.loads(json.dumps(value, ensure_ascii=False))

    def register_batch(self, batch_id: str, *, expected_count: int = 0,
                       metadata: dict | None = None,
                       status: str = "staged") -> dict:
        """Create a server-owned batch or extend an in-progress upload batch."""
        batch_id = self._validate_batch_id(batch_id)
        try:
            expected_count = int(expected_count)
        except (TypeError, ValueError) as exc:
            raise DecisionStoreError("批次数量格式无效") from exc
        if expected_count < 0 or expected_count > 10000:
            raise DecisionStoreError("批次数量超出允许范围")
        status = str(status or "staged").strip()
        if status not in {"staged", "ready", "committing", "failed",
                          "needs_recovery", "committed", "undone"}:
            raise DecisionStoreError("批次状态无效")
        metadata = self._json_object(metadata, "批次元数据格式无效")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM filing_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row:
                if row["status"] in {"committed", "undone", "needs_recovery"}:
                    raise DecisionStoreError("批次已结束或需要恢复，不能重复注册")
                current = int(row["expected_count"] or 0)
                count = max(current, expected_count)
                conn.execute(
                    "UPDATE filing_batches SET expected_count=?, updated_at=? "
                    "WHERE batch_id=?",
                    (count, now, batch_id),
                )
            else:
                conn.execute(
                    "INSERT INTO filing_batches "
                    "(batch_id,status,expected_count,error,metadata,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, status, expected_count, None,
                     json.dumps(metadata, ensure_ascii=False), now, now),
                )
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict | None:
        batch_id = self._validate_batch_id(batch_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM filing_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["metadata_fields"] = json.loads(item.get("metadata") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["metadata_fields"] = {}
        if not isinstance(item["metadata_fields"], dict):
            item["metadata_fields"] = {}
        return item

    def list_batch_items(self, batch_id: str) -> list[dict]:
        batch_id = self._validate_batch_id(batch_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM filing_batch_items WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field, default in (("sidecars", []), ("metadata", {})):
                try:
                    parsed = json.loads(item.get(field) or ("[]" if field == "sidecars" else "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = default
                if not isinstance(parsed, type(default)):
                    parsed = default
                item[field + "_fields"] = parsed
            result.append(item)
        return result

    def register_batch_items(self, batch_id: str, items: list[dict]) -> list[dict]:
        """Register source entries produced by stage-only upload."""
        batch_id = self._validate_batch_id(batch_id)
        if not isinstance(items, list) or not items:
            raise DecisionStoreError("批次条目不能为空")
        now = datetime.now().isoformat(timespec="seconds")
        normalized = []
        seen = set()
        for raw in items:
            if not isinstance(raw, dict):
                raise DecisionStoreError("批次条目格式无效")
            source = str(raw.get("source_path") or "").strip()
            if not source or not Path(source).is_absolute():
                raise DecisionStoreError("批次源路径必须是绝对路径")
            if source in seen:
                raise DecisionStoreError("批次含有重复源路径")
            seen.add(source)
            decision_id = raw.get("decision_id")
            decision_id = str(decision_id).strip() if decision_id else None
            destination = raw.get("destination_path")
            destination = str(destination).strip() if destination else None
            status = str(raw.get("status") or "staged").strip()
            if status not in {"staged", "ready", "committing", "committed",
                              "failed", "rolled_back", "needs_recovery", "undone"}:
                raise DecisionStoreError("批次条目状态无效")
            sidecars = self._json_list(raw.get("sidecars", []), "批次 sidecar 数据无效")
            metadata = self._json_object(raw.get("metadata", {}), "批次条目元数据无效")
            normalized.append((decision_id, source, destination, status,
                               raw.get("error"), sidecars, metadata))

        with self._connect() as conn:
            batch = conn.execute(
                "SELECT status FROM filing_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not batch:
                raise DecisionStoreError("批次尚未注册")
            if batch["status"] in {"committed", "undone", "needs_recovery"}:
                raise DecisionStoreError("批次已结束或需要恢复，不能写入条目")
            for decision_id, source, destination, status, error, sidecars, metadata in normalized:
                existing = conn.execute(
                    "SELECT * FROM filing_batch_items WHERE batch_id=? AND source_path=?",
                    (batch_id, source),
                ).fetchone()
                if existing:
                    if existing["decision_id"] and decision_id and existing["decision_id"] != decision_id:
                        raise DecisionStoreError("批次源条目已绑定其它决策")
                    conn.execute(
                        "UPDATE filing_batch_items SET decision_id=COALESCE(?,decision_id), "
                        "destination_path=COALESCE(?,destination_path), status=?, error=?, "
                        "sidecars=?, metadata=?, updated_at=? WHERE id=?",
                        (decision_id, destination, status, error,
                         json.dumps(sidecars, ensure_ascii=False),
                         json.dumps(metadata, ensure_ascii=False), now, existing["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO filing_batch_items "
                        "(batch_id,decision_id,source_path,destination_path,status,error,sidecars,metadata,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (batch_id, decision_id, source, destination, status, error,
                         json.dumps(sidecars, ensure_ascii=False),
                         json.dumps(metadata, ensure_ascii=False), now, now),
                    )
            count = conn.execute(
                "SELECT COUNT(*) FROM filing_batch_items WHERE batch_id=?", (batch_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE filing_batches SET expected_count=?, updated_at=? WHERE batch_id=?",
                (count, now, batch_id),
            )
        return self.list_batch_items(batch_id)

    def find_batch_item(self, batch_id: str, *, source_path: str | None = None,
                        decision_id: str | None = None) -> dict | None:
        batch_id = self._validate_batch_id(batch_id)
        clauses = ["batch_id=?"]
        values: list[str] = [batch_id]
        if source_path is not None:
            clauses.append("source_path=?")
            values.append(str(source_path))
        if decision_id is not None:
            clauses.append("decision_id=?")
            values.append(str(decision_id))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM filing_batch_items WHERE " + " AND ".join(clauses) +
                " ORDER BY id LIMIT 1", values,
            ).fetchone()
        return dict(row) if row else None

    def find_decision_batch(self, decision_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT batch_id FROM filing_batch_items WHERE decision_id=? "
                "ORDER BY id DESC LIMIT 1", (str(decision_id),)
            ).fetchone()
        return row["batch_id"] if row else None

    def update_batch(self, batch_id: str, *, status: str | None = None,
                     error: str | None = None) -> dict:
        batch_id = self._validate_batch_id(batch_id)
        if status is not None:
            status = str(status).strip()
            if status not in {"staged", "ready", "committing", "failed",
                              "needs_recovery", "committed", "undone"}:
                raise DecisionStoreError("批次状态无效")
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM filing_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not row:
                raise DecisionStoreError("批次不存在")
            conn.execute(
                "UPDATE filing_batches SET status=COALESCE(?,status), error=?, updated_at=? "
                "WHERE batch_id=?", (status, error, now, batch_id),
            )
        return self.get_batch(batch_id) or {}

    def update_batch_item(self, batch_id: str, *, source_path: str | None = None,
                          decision_id: str | None = None, status: str | None = None,
                          destination_path: str | None = None,
                          error: str | None = None) -> dict:
        batch_id = self._validate_batch_id(batch_id)
        if source_path is None and decision_id is None:
            raise DecisionStoreError("批次条目缺少定位字段")
        if status is not None:
            status = str(status).strip()
            if status not in {"staged", "ready", "committing", "committed",
                              "failed", "rolled_back", "needs_recovery", "undone"}:
                raise DecisionStoreError("批次条目状态无效")
        clauses = ["batch_id=?"]
        where_values: list[str | None] = [batch_id]
        if source_path is not None:
            clauses.append("source_path=?")
            where_values.append(str(source_path))
        if decision_id is not None:
            clauses.append("decision_id=?")
            where_values.append(str(decision_id))
        now = datetime.now().isoformat(timespec="seconds")
        assignments = ["status=COALESCE(?,status)", "destination_path=COALESCE(?,destination_path)",
                       "error=?", "updated_at=?"]
        assignment_values = [status, destination_path, error, now]
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE filing_batch_items SET " + ", ".join(assignments) +
                " WHERE " + " AND ".join(clauses), assignment_values + where_values,
            ).rowcount
        if not changed:
            raise DecisionStoreError("批次条目不存在")
        item = self.find_batch_item(batch_id, source_path=source_path,
                                    decision_id=decision_id)
        return item or {}
