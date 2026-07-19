"""Shared SQLite connection policy for Genesis' durable local stores."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
from typing import Iterator


logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 30_000


@contextmanager
def sqlite_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a configured connection and always commit/rollback and close it.

    Runtime and Palace share a database and are called from parallel worker
    threads.  A consistent busy timeout plus WAL-friendly synchronous settings
    keeps those short transactions from needlessly blocking one another.  The
    explicit close is important on Windows, where a lingering handle prevents
    state databases and temporary test directories from being moved or removed.
    """

    con = sqlite3.connect(
        str(db_path),
        timeout=_BUSY_TIMEOUT_MS / 1000,
        check_same_thread=True,
    )
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA synchronous = NORMAL")
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def enable_wal(con: sqlite3.Connection) -> str:
    """Enable WAL when the filesystem supports it, retaining safe fallback."""

    try:
        row = con.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.DatabaseError as exc:
        # Read-only/network filesystems may reject WAL. SQLite's existing
        # journal mode remains usable, so startup should not fail solely for it.
        logger.warning("SQLite WAL mode is unavailable; using existing journal mode: %s", exc)
        return ""
    return str(row[0]).lower() if row else ""
