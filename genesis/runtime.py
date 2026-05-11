from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis.config import GenesisConfig
from genesis.palace import resolve_state_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(data: dict[str, Any] | None) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task: str
    status: str
    created_at: str
    updated_at: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RuntimeEvent:
    id: int
    run_id: str
    step_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class StepRecord:
    run_id: str
    step_id: str
    title: str
    status: str
    worker: str
    worktree_path: str
    patch_artifact_id: str
    review_json: dict[str, Any]
    verification_json: dict[str, Any]
    commit_sha: str
    updated_at: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    step_id: str
    kind: str
    path: str
    content: str
    metadata: dict[str, Any]
    created_at: str


class RuntimeStore:
    """Durable run state, checkpoints, and events."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @classmethod
    def from_config(cls, config: GenesisConfig) -> "RuntimeStore":
        return cls(resolve_state_db(config))

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL DEFAULT '',
                    checkpoint_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, step_id, checkpoint_name)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS run_steps (
                    run_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    worker TEXT NOT NULL DEFAULT '',
                    worktree_path TEXT NOT NULL DEFAULT '',
                    patch_artifact_id TEXT NOT NULL DEFAULT '',
                    review_json TEXT NOT NULL DEFAULT '{}',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    commit_sha TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (run_id, step_id)
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_run ON runtime_events(run_id, id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, step_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_steps_run ON run_steps(run_id, status)"
            )

    def start_run(
        self,
        task: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = run_id or uuid.uuid4().hex[:12]
        now = _utc_now()
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO runs(run_id, task, status, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, task, "running", now, now, _json(metadata)),
            )
        self.record_event(run_id, "run_started", payload={"task": task, **(metadata or {})})
        return run_id

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self._connect() as con:
            row = con.execute(
                "SELECT metadata_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            current: dict[str, Any] = {}
            if row:
                try:
                    current = json.loads(row["metadata_json"] or "{}")
                except json.JSONDecodeError:
                    current = {}
            current.update(metadata or {})
            con.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, metadata_json = ?
                WHERE run_id = ?
                """,
                (status, now, _json(current), run_id),
            )
        self.record_event(run_id, f"run_{status}", payload=metadata or {})

    def record_event(
        self,
        run_id: str,
        event_type: str,
        *,
        step_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO runtime_events(run_id, step_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, step_id, event_type, _json(payload), _utc_now()),
            )

    def checkpoint(
        self,
        run_id: str,
        checkpoint_name: str,
        *,
        step_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO checkpoints(
                    run_id, step_id, checkpoint_name, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, step_id, checkpoint_name, _json(payload), _utc_now()),
            )
        self.record_event(
            run_id,
            "checkpoint",
            step_id=step_id,
            payload={"checkpoint": checkpoint_name, **(payload or {})},
        )

    def get_checkpoint(
        self,
        run_id: str,
        checkpoint_name: str,
        *,
        step_id: str = "",
    ) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT payload_json
                FROM checkpoints
                WHERE run_id = ? AND step_id = ? AND checkpoint_name = ?
                """,
                (run_id, step_id, checkpoint_name),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def add_artifact(
        self,
        run_id: str,
        *,
        step_id: str = "",
        kind: str,
        path: str = "",
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        artifact_id = "art_" + uuid.uuid4().hex[:16]
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, run_id, step_id, kind, path, content,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    step_id,
                    kind,
                    path,
                    content,
                    _json(metadata),
                    _utc_now(),
                ),
            )
        return artifact_id

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return self._artifact(row) if row else None

    def artifacts(self, run_id: str, *, step_id: str = "") -> list[ArtifactRecord]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if step_id:
            clauses.append("step_id = ?")
            params.append(step_id)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT *
                FROM artifacts
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()
        return [self._artifact(row) for row in rows]

    def upsert_step(
        self,
        run_id: str,
        step_id: str,
        *,
        title: str = "",
        status: str | None = None,
        worker: str | None = None,
        worktree_path: str | None = None,
        patch_artifact_id: str | None = None,
        review: dict[str, Any] | None = None,
        verification: dict[str, Any] | None = None,
        commit_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        current = self.get_step(run_id, step_id)
        merged_metadata = dict(current.metadata) if current else {}
        merged_metadata.update(metadata or {})
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO run_steps(
                    run_id, step_id, title, status, worker, worktree_path,
                    patch_artifact_id, review_json, verification_json,
                    commit_sha, updated_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, step_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    worker = excluded.worker,
                    worktree_path = excluded.worktree_path,
                    patch_artifact_id = excluded.patch_artifact_id,
                    review_json = excluded.review_json,
                    verification_json = excluded.verification_json,
                    commit_sha = excluded.commit_sha,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    run_id,
                    step_id,
                    title or (current.title if current else ""),
                    status if status is not None else (current.status if current else "pending"),
                    worker if worker is not None else (current.worker if current else ""),
                    worktree_path if worktree_path is not None else (current.worktree_path if current else ""),
                    patch_artifact_id if patch_artifact_id is not None else (current.patch_artifact_id if current else ""),
                    _json(review if review is not None else (current.review_json if current else {})),
                    _json(verification if verification is not None else (current.verification_json if current else {})),
                    commit_sha if commit_sha is not None else (current.commit_sha if current else ""),
                    now,
                    _json(merged_metadata),
                ),
            )
        self.record_event(
            run_id,
            "step_status",
            step_id=step_id,
            payload={
                "status": status if status is not None else (current.status if current else "pending"),
                "title": title or (current.title if current else ""),
            },
        )

    def get_step(self, run_id: str, step_id: str) -> StepRecord | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM run_steps WHERE run_id = ? AND step_id = ?",
                (run_id, step_id),
            ).fetchone()
        return self._step(row) if row else None

    def steps(self, run_id: str) -> list[StepRecord]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM run_steps
                WHERE run_id = ?
                ORDER BY step_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self._step(row) for row in rows]

    def reset_step_for_retry(self, run_id: str, step_id: str) -> None:
        current = self.get_step(run_id, step_id)
        if not current:
            return
        self.upsert_step(
            run_id,
            step_id,
            title=current.title,
            status="pending",
            worker="",
            worktree_path="",
            patch_artifact_id="",
            review={},
            verification={},
            commit_sha="",
            metadata={"retried": True},
        )
        self.update_run_status(
            run_id,
            "running",
            metadata={"retry_step": step_id, "blocked_step": "", "reason": ""},
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def latest_runs(self, limit: int = 10) -> list[RunRecord]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM runs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._run(row) for row in rows]

    def events(self, run_id: str, limit: int = 100) -> list[RuntimeEvent]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM runtime_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def _run(self, row: sqlite3.Row) -> RunRecord:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return RunRecord(
            run_id=row["run_id"],
            task=row["task"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=metadata,
        )

    def _event(self, row: sqlite3.Row) -> RuntimeEvent:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return RuntimeEvent(
            id=int(row["id"]),
            run_id=row["run_id"],
            step_id=row["step_id"],
            event_type=row["event_type"],
            payload=payload,
            created_at=row["created_at"],
        )

    def _step(self, row: sqlite3.Row) -> StepRecord:
        return StepRecord(
            run_id=row["run_id"],
            step_id=row["step_id"],
            title=row["title"],
            status=row["status"],
            worker=row["worker"],
            worktree_path=row["worktree_path"],
            patch_artifact_id=row["patch_artifact_id"],
            review_json=_loads(row["review_json"]),
            verification_json=_loads(row["verification_json"]),
            commit_sha=row["commit_sha"],
            updated_at=row["updated_at"],
            metadata=_loads(row["metadata_json"]),
        )

    def _artifact(self, row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            kind=row["kind"],
            path=row["path"],
            content=row["content"],
            metadata=_loads(row["metadata_json"]),
            created_at=row["created_at"],
        )


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
