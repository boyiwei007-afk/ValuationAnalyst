from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from valuationagent.schemas.models import (
    ChatMessage,
    RunEvent,
    RunRecord,
    RunStatus,
    ValuationOutput,
    ValuationRequest,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, default=str)


class SQLiteRunStore:
    """Small local run store. Secrets are deliberately kept out of this class."""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir = self.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "valuationagent.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS research_sessions (
                    session_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    session_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_documents (
                    session_id TEXT NOT NULL, file_id TEXT NOT NULL,
                    blocks_json TEXT NOT NULL, PRIMARY KEY(session_id, file_id)
                );
                CREATE TABLE IF NOT EXISTS lineage (
                    run_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, parent_id TEXT,
                    revision INTEGER NOT NULL, reason TEXT,
                    UNIQUE(root_id, revision)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    root_id TEXT NOT NULL, cache_key TEXT NOT NULL, run_id TEXT NOT NULL,
                    tool TEXT NOT NULL, input_json TEXT NOT NULL, output_json TEXT NOT NULL,
                    PRIMARY KEY(root_id, cache_key)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT,
                    review_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_type TEXT,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_run(
        self,
        run_id: str,
        request: ValuationRequest,
        *,
        parent_id: str | None = None,
        reason: str | None = None,
    ) -> RunRecord:
        now = _utc_now()
        parent = self.get_run(parent_id) if parent_id else None
        root_id = (parent.root_run_id or parent.run_id) if parent else run_id
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM lineage WHERE root_id=?",
                (root_id,),
            ).fetchone()[0]
            if parent and revision == 1:
                connection.execute(
                    "INSERT OR IGNORE INTO lineage VALUES(?,?,?,?,?)",
                    (parent.run_id, root_id, None, 1, None),
                )
                revision = 2
            connection.execute(
                "INSERT INTO runs(run_id,status,request_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    RunStatus.CREATED.value,
                    request.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO lineage VALUES(?,?,?,?,?)",
                (run_id, root_id, parent_id, revision, reason),
            )
        return self.get_run(run_id)

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: ValuationOutput | None = None,
        error: dict[str, Any] | None = None,
        review: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status=?, result_json=?,
                    error_json=?, review_json=?, updated_at=?
                WHERE run_id=?
                """,
                (
                    status.value,
                    _json(result) if result is not None else None,
                    _json(error) if error is not None else None,
                    _json(review) if review is not None else None,
                    _utc_now().isoformat(),
                    run_id,
                ),
            )
            if connection.total_changes == 0:
                raise KeyError(run_id)

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            lineage = connection.execute(
                "SELECT * FROM lineage WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord(
            run_id=row["run_id"],
            status=row["status"],
            request=ValuationRequest.model_validate_json(row["request_json"]),
            input_hash=hashlib.sha256(row["request_json"].encode("utf-8")).hexdigest(),
            result=ValuationOutput.model_validate_json(row["result_json"])
            if row["result_json"]
            else None,
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            review=json.loads(row["review_json"]) if row["review_json"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            root_run_id=lineage["root_id"] if lineage else run_id,
            parent_run_id=lineage["parent_id"] if lineage else None,
            revision=lineage["revision"] if lineage else 1,
            revision_reason=lineage["reason"] if lineage else None,
            workflow_version="0.5.0" if lineage else "0.1.0",
        )

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [self.get_run(row["run_id"]) for row in rows]

    def append_event(self, run_id: str, **values: Any) -> RunEvent:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            event = RunEvent(run_id=run_id, sequence=sequence, **values)
            connection.execute(
                "INSERT INTO events(run_id,sequence,event_json) VALUES(?,?,?)",
                (run_id, sequence, event.model_dump_json()),
            )
        return event

    def list_events(self, run_id: str, after: int = 0) -> list[RunEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_json FROM events WHERE run_id=? AND sequence>? ORDER BY sequence",
                (run_id, after),
            ).fetchall()
        return [RunEvent.model_validate_json(row["event_json"]) for row in rows]

    def add_message(
        self,
        run_id: str,
        role: str,
        content: str,
        stage: str | None = None,
        related_run_id: str | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            message_id=f"msg_{uuid.uuid4().hex}",
            run_id=run_id,
            role=role,
            content=content,
            stage=stage,
            related_run_id=related_run_id,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO messages(message_id,run_id,created_at,message_json) VALUES(?,?,?,?)",
                (
                    message.message_id,
                    run_id,
                    message.created_at.isoformat(),
                    message.model_dump_json(),
                ),
            )
        return message

    def list_messages(self, run_id: str) -> list[ChatMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT message_json FROM messages WHERE run_id=? ORDER BY created_at,message_id",
                (run_id,),
            ).fetchall()
        return [ChatMessage.model_validate_json(row["message_json"]) for row in rows]

    def save_upload(
        self, name: str, role: str, content_type: str | None, content: bytes
    ) -> dict[str, Any]:
        if role not in {
            "historical_financials",
            "assumptions",
            "comparables",
            "evidence",
        }:
            raise ValueError("unknown file role")
        if not content:
            raise ValueError("empty files are not accepted")
        if len(content) > 50 * 1024 * 1024:
            raise ValueError("file exceeds the 50 MB limit")
        file_id = f"file_{uuid.uuid4().hex}"
        safe_suffix = Path(name).suffix.lower()
        if safe_suffix not in {
            ".pdf",
            ".docx",
            ".xlsx",
            ".xls",
            ".csv",
            ".json",
            ".txt",
            ".md",
        }:
            raise ValueError(
                "supported file types: PDF, DOCX, Excel, CSV, JSON, TXT, Markdown"
            )
        target = (self.upload_dir / f"{file_id}{safe_suffix}").resolve()
        if self.upload_dir not in target.parents:
            raise ValueError("invalid file name")
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        created_at = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO files(file_id,original_name,role,content_type,sha256,size_bytes,storage_path,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    file_id,
                    Path(name).name,
                    role,
                    content_type,
                    digest,
                    len(content),
                    str(target),
                    created_at.isoformat(),
                ),
            )
        return {
            "file_id": file_id,
            "original_name": Path(name).name,
            "role": role,
            "content_type": content_type,
            "sha256": digest,
            "size_bytes": len(content),
            "created_at": created_at.isoformat(),
        }

    def get_file(self, file_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM files WHERE file_id=?", (file_id,)
            ).fetchone()
        if not row:
            raise ValueError(f"未找到文件 {file_id}")
        return dict(row)

    def revisions(self, run_id: str) -> list[RunRecord]:
        root = self.get_run(run_id).root_run_id or run_id
        with self._connect() as db:
            ids = db.execute(
                "SELECT run_id FROM lineage WHERE root_id=? ORDER BY revision", (root,)
            ).fetchall()
        return [self.get_run(row[0]) for row in ids] or [self.get_run(run_id)]

    def checkpoint(self, run_id: str, key: str) -> dict | None:
        root = self.get_run(run_id).root_run_id or run_id
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE root_id=? AND cache_key=?", (root, key)
            ).fetchone()
        return dict(row) if row else None

    def save_checkpoint(
        self, run_id: str, key: str, tool: str, inputs: str, output: str
    ) -> None:
        root = self.get_run(run_id).root_run_id or run_id
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?)",
                (root, key, run_id, tool, inputs, output),
            )

    def artifacts(self, run_id: str) -> list[dict]:
        root = self.get_run(run_id).root_run_id or run_id
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM checkpoints WHERE root_id=?", (root,)
            ).fetchall()
        return [
            {
                "artifact_id": r["cache_key"],
                "run_id": r["run_id"],
                "tool": r["tool"],
                "inputs": json.loads(r["input_json"]),
                "output": json.loads(r["output_json"]),
            }
            for r in rows
        ]

    def acquire(self, run_id: str, owner: str) -> bool:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT expires FROM leases WHERE run_id=?", (run_id,)
            ).fetchone()
            if row and row[0] > time.time():
                return False
            db.execute(
                "INSERT OR REPLACE INTO leases VALUES(?,?,?)",
                (run_id, owner, time.time() + 30),
            )
        return True

    def active(self, run_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT expires FROM leases WHERE run_id=?", (run_id,)
            ).fetchone()
        return bool(row and row[0] > time.time())

    def heartbeat(self, run_id: str, owner: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE leases SET expires=? WHERE run_id=? AND owner=?",
                (time.time() + 30, run_id, owner),
            )

    def release(self, run_id: str, owner: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM leases WHERE run_id=? AND owner=?", (run_id, owner))

    def create_research(self, session):
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO research_sessions VALUES(?,?,?,?)", (
                session.session_id, session.revision, session.model_dump_json(),
                session.updated_at.isoformat()))
        return session

    def get_research(self, session_id):
        from valuationagent.schemas.research import ResearchSession
        with self._connect() as db:
            row = db.execute("SELECT session_json FROM research_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return ResearchSession.model_validate_json(row[0])

    def save_research(self, session):
        previous = session.revision
        updated = session.model_copy(update={"revision": previous + 1, "updated_at": _utc_now()})
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE research_sessions SET revision=?,session_json=?,updated_at=? WHERE session_id=? AND revision=?",
                (updated.revision, updated.model_dump_json(), updated.updated_at.isoformat(), session.session_id, previous))
            if cursor.rowcount != 1:
                raise ValueError("会话已更新，请刷新后重试。")
        session.revision, session.updated_at = updated.revision, updated.updated_at
        return session

    def list_research(self, limit=30):
        with self._connect() as db:
            rows = db.execute("SELECT session_id FROM research_sessions ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        return [self.get_research(row[0]) for row in rows]

    def save_research_blocks(self, session_id, file_id, blocks):
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO research_documents VALUES(?,?,?)", (session_id, file_id, _json(blocks)))

    def research_blocks(self, session_id, file_id):
        with self._connect() as db:
            row = db.execute("SELECT blocks_json FROM research_documents WHERE session_id=? AND file_id=?", (session_id, file_id)).fetchone()
        if row is None:
            raise ValueError("文件尚未加入当前会话，请先上传。")
        return json.loads(row[0])
