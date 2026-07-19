from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager

from genesis._sqlite import enable_wal, sqlite_connection
from genesis.config import CONFIG_DIR, GenesisConfig


logger = logging.getLogger(__name__)

_QUERY_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
    "from", "in", "into", "is", "it", "of", "on", "or", "please", "that",
    "the", "this", "to", "with",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(data: dict[str, Any] | None) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)


def _terms(query: str) -> str:
    """Build a safe, recall-oriented FTS query from natural language.

    Full task descriptions contain plenty of words that will not occur in a
    useful memory. Requiring every word (FTS' implicit AND) therefore hides the
    relevant record. Quoted OR terms are safe from FTS operators and let bm25
    rank records matching several terms ahead of one-word matches.
    """

    parts = _query_terms(query)
    if not parts:
        return ""
    quoted = [f'"{part}"' for part in parts]
    if len(quoted) == 1:
        return quoted[0]
    phrase = '"' + " ".join(parts) + '"'
    return " OR ".join([phrase, *quoted])


def _query_terms(query: str) -> list[str]:
    all_parts = list(dict.fromkeys(re.findall(r"\w+", query, flags=re.UNICODE)))
    meaningful = [part for part in all_parts if part.casefold() not in _QUERY_STOP_WORDS]
    return (meaningful or all_parts)[:16]


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
        self._fts_enabled = False
        self._ensure_schema()

    @classmethod
    def from_config(cls, config: GenesisConfig) -> "PalaceStore":
        return cls(resolve_state_db(config))

    def _connect(self) -> ContextManager[sqlite3.Connection]:
        return sqlite_connection(self.db_path)

    def _ensure_schema(self) -> None:
        with self._connect() as con:
            enable_wal(con)
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
                "CREATE INDEX IF NOT EXISTS idx_palace_run ON palace_drawers(run_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_step ON palace_drawers(step_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_scope ON palace_drawers(wing, room, closet)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_palace_recent ON palace_drawers(created_at DESC)"
            )
            try:
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
                # Recover canonical rows written while FTS was unavailable.
                con.execute(
                    """
                    INSERT INTO palace_fts(drawer_id, wing, room, closet, title, content)
                    SELECT d.id, d.wing, d.room, d.closet, d.title, d.content
                    FROM palace_drawers AS d
                    WHERE NOT EXISTS (
                        SELECT 1 FROM palace_fts AS f WHERE f.drawer_id = d.id
                    )
                    """
                )
                self._fts_enabled = True
            except sqlite3.DatabaseError as exc:
                # FTS5 is optional in some Python/SQLite builds. Canonical
                # verbatim memory remains available through the LIKE fallback.
                logger.warning("Palace full-text index unavailable; using fallback search: %s", exc)
                self._fts_enabled = False

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
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        # Provenance is part of record identity. The same words produced by two
        # runs must remain independently attributable instead of one run
        # replacing the other's canonical memory.
        identity_hash = hashlib.sha256(
            "\n".join(
                [
                    wing,
                    room,
                    closet,
                    kind,
                    title,
                    source,
                    run_id,
                    step_id,
                    file_path,
                    status,
                    content_hash,
                ]
            ).encode("utf-8")
        ).hexdigest()
        drawer_id = "mem_" + identity_hash[:20]
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO palace_drawers (
                    id, wing, room, closet, kind, title, content, source,
                    run_id, step_id, file_path, status, content_hash,
                    metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    wing = excluded.wing,
                    room = excluded.room,
                    closet = excluded.closet,
                    kind = excluded.kind,
                    title = excluded.title,
                    content = excluded.content,
                    source = excluded.source,
                    run_id = excluded.run_id,
                    step_id = excluded.step_id,
                    file_path = excluded.file_path,
                    status = excluded.status,
                    content_hash = excluded.content_hash,
                    metadata_json = excluded.metadata_json
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
                    content_hash,
                    _json(metadata),
                    created_at,
                ),
            )
            if self._fts_enabled:
                try:
                    con.execute("DELETE FROM palace_fts WHERE drawer_id = ?", (drawer_id,))
                    con.execute(
                        """
                        INSERT INTO palace_fts(drawer_id, wing, room, closet, title, content)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (drawer_id, wing, room, closet, title, content),
                    )
                except sqlite3.DatabaseError as exc:
                    # Index damage/unavailability must never discard the
                    # canonical verbatim record in palace_drawers.
                    logger.warning("Palace full-text update failed; memory kept canonically: %s", exc)
                    self._fts_enabled = False
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
        limit = max(0, int(limit))
        if limit == 0:
            return []
        if not query:
            return self.recent(limit=limit, wing=wing, room=room, closet=closet)

        if self._fts_enabled and _terms(query):
            try:
                hits = self._search_fts(
                    query, wing=wing, room=room, closet=closet, limit=limit
                )
                if hits:
                    return hits
            except sqlite3.Error as exc:
                logger.warning("Palace full-text search failed; using fallback: %s", exc)
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
        raw_terms = _query_terms(query)
        raw_terms = raw_terms or [query]
        term_clauses: list[str] = []
        params: list[Any] = []
        for term in raw_terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            term_clauses.append(
                "(title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like])
        clauses = ["(" + " OR ".join(term_clauses) + ")"]
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
        limit = max(0, int(limit))
        if limit == 0:
            return []
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
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._hit(row) for row in rows]

    def wakeup_context(self, query: str, *, max_chars: int = 6000, wing: str = "") -> str:
        max_chars = max(0, int(max_chars))
        if max_chars == 0:
            return ""
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
                remaining = max_chars - used
                if remaining > 1:
                    lines.append(block[:remaining].rstrip())
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)[:max_chars]

    def rebuild_search_index(self) -> bool:
        """Rebuild the disposable FTS index from canonical drawer records."""

        try:
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
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
                con.execute("DELETE FROM palace_fts")
                con.execute(
                    """
                    INSERT INTO palace_fts(drawer_id, wing, room, closet, title, content)
                    SELECT id, wing, room, closet, title, content
                    FROM palace_drawers
                    """
                )
            self._fts_enabled = True
            return True
        except sqlite3.DatabaseError as exc:
            logger.warning("Could not rebuild Palace full-text index: %s", exc)
            self._fts_enabled = False
            return False

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
            metadata=_loads(row["metadata_json"]),
        )


def resolve_state_db(config: GenesisConfig) -> Path:
    configured = getattr(config.runtime, "state_db", "")
    if configured:
        return Path(configured).expanduser()
    return CONFIG_DIR / "state" / "genesis.db"


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}
