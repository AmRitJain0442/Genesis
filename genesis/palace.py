from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis.config import CONFIG_DIR, GenesisConfig


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(data: dict[str, Any] | None) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _terms(query: str) -> str:
    parts = re.findall(r"[\w./:-]+", query)
    return " ".join(parts) if parts else query


@dataclass(frozen=True)
class MemoryHit:
    id: str
    wing: str
    room: str
    closet: str
    kind: str
    title: str
    content: str
    source: str
    created_at: str
    score: float
    metadata: dict[str, Any]


class PalaceStore:
    """Local-first, verbatim memory store inspired by memory-palace systems.

    The canonical record is the full drawer content. Summaries, tags, and FTS
    rows are only retrieval indexes; they can be rebuilt without losing memory.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @classmethod
    def from_config(cls, config: GenesisConfig) -> "PalaceStore":
        return cls(resolve_state_db(config))

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30.0)
        con.row_factory = sqlite3.Row
        return con

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS palace_drawers (
                    id TEXT PRIMARY KEY,
                    wing TEXT NOT NULL,
                    room TEXT NOT NULL,
                    closet TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    step_id TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS palace_fts USING fts5(
                    drawer_id UNINDEXED,
                    wing,
                    room,
                    closet,
                    title,
                    content,
                    tokenize='unicode61'
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_run ON palace_drawers(run_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_step ON palace_drawers(step_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_scope ON palace_drawers(wing, room, closet)"
            )

    def add_drawer(
        self,
        *,
        wing: str,
        room: str,
        closet: str,
        kind: str,
        title: str,
        content: str,
        source: str = "",
        run_id: str = "",
        step_id: str = "",
        file_path: str = "",
        status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        content = content or ""
        created_at = _utc_now()
        digest = hashlib.sha256(
            "\n".join([wing, room, closet, kind, title, source, content]).encode("utf-8")
        ).hexdigest()
        drawer_id = "mem_" + digest[:20]
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO palace_drawers (
                    id, wing, room, closet, kind, title, content, source,
                    run_id, step_id, file_path, status, content_hash,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    drawer_id,
                    wing,
                    room,
                    closet,
                    kind,
                    title,
                    content,
                    source,
                    run_id,
                    step_id,
                    file_path,
                    status,
                    digest,
                    _json(metadata),
                    created_at,
                ),
            )
            con.execute("DELETE FROM palace_fts WHERE drawer_id = ?", (drawer_id,))
            con.execute(
                """
                INSERT INTO palace_fts(drawer_id, wing, room, closet, title, content)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (drawer_id, wing, room, closet, title, content),
            )
        return drawer_id

    def search(
        self,
        query: str,
        *,
        wing: str | None = None,
        room: str | None = None,
        closet: str | None = None,
        limit: int = 10,
    ) -> list[MemoryHit]:
        query = query.strip()
        if not query:
            return self.recent(limit=limit, wing=wing, room=room, closet=closet)

        try:
            return self._search_fts(query, wing=wing, room=room, closet=closet, limit=limit)
        except sqlite3.Error:
            return self._search_like(query, wing=wing, room=room, closet=closet, limit=limit)

    def _search_fts(
        self,
        query: str,
        *,
        wing: str | None,
        room: str | None,
        closet: str | None,
        limit: int,
    ) -> list[MemoryHit]:
        clauses = ["palace_fts MATCH ?"]
        params: list[Any] = [_terms(query)]
        if wing:
            clauses.append("d.wing = ?")
            params.append(wing)
        if room:
            clauses.append("d.room = ?")
            params.append(room)
        if closet:
            clauses.append("d.closet = ?")
            params.append(closet)
        params.append(limit)

        sql = f"""
            SELECT d.*, bm25(palace_fts) AS score
            FROM palace_fts
            JOIN palace_drawers d ON d.id = palace_fts.drawer_id
            WHERE {' AND '.join(clauses)}
            ORDER BY score
            LIMIT ?
        """
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._hit(row) for row in rows]

    def _search_like(
        self,
        query: str,
        *,
        wing: str | None,
        room: str | None,
        closet: str | None,
        limit: int,
    ) -> list[MemoryHit]:
        clauses = ["(title LIKE ? OR content LIKE ?)"]
        like = f"%{query}%"
        params: list[Any] = [like, like]
        if wing:
            clauses.append("wing = ?")
            params.append(wing)
        if room:
            clauses.append("room = ?")
            params.append(room)
        if closet:
            clauses.append("closet = ?")
            params.append(closet)
        params.append(limit)
        sql = f"""
            SELECT *, 0.0 AS score
            FROM palace_drawers
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
        """
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [self._hit(row) for row in rows]

    def recent(
        self,
        *,
        limit: int = 10,
        wing: str | None = None,
        room: str | None = None,
        closet: str | None = None,
    ) -> list[MemoryHit]:
        clauses: list[str] = []
        params: list[Any] = []
        if wing:
            clauses.append("wing = ?")
            params.append(wing)
        if room:
            clauses.append("room = ?")
            params.append(room)
        if closet:
            clauses.append("closet = ?")
            params.append(closet)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT *, 0.0 AS score
                FROM palace_drawers
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._hit(row) for row in rows]

    def wakeup_context(self, query: str, *, max_chars: int = 6000, wing: str = "") -> str:
        hits = self.search(query, wing=wing or None, limit=8)
        if not hits:
            return ""
        lines: list[str] = ["# Retrieved Genesis Memory"]
        used = len(lines[0])
        for hit in hits:
            snippet = hit.content.strip()
            if len(snippet) > 900:
                snippet = snippet[:900].rstrip() + "\n..."
            block = (
                f"\n## {hit.title}\n"
                f"- scope: {hit.wing}/{hit.room}/{hit.closet}\n"
                f"- kind: {hit.kind} source: {hit.source}\n\n"
                f"{snippet}\n"
            )
            if used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)

    def import_markdown(
        self,
        path: str | Path,
        *,
        wing: str,
        room: str = "legacy",
        closet: str = "markdown-memory",
    ) -> int:
        source_path = Path(path)
        if not source_path.exists():
            return 0
        content = source_path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return 0
        self.add_drawer(
            wing=wing,
            room=room,
            closet=closet,
            kind="legacy-memory",
            title=source_path.name,
            content=content,
            source=str(source_path),
            metadata={"imported_from": "GENESIS_MEMORY.md"},
        )
        return 1

    def _hit(self, row: sqlite3.Row) -> MemoryHit:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        return MemoryHit(
            id=row["id"],
            wing=row["wing"],
            room=row["room"],
            closet=row["closet"],
            kind=row["kind"],
            title=row["title"],
            content=row["content"],
            source=row["source"],
            created_at=row["created_at"],
            score=float(row["score"] or 0.0),
            metadata=metadata,
        )


def resolve_state_db(config: GenesisConfig) -> Path:
    configured = getattr(config.runtime, "state_db", "")
    if configured:
        return Path(configured).expanduser()
    return CONFIG_DIR / "state" / "genesis.db"
