"""
ChatroomManager — the thread-safe message bus.

Agents post messages to rooms; the web viewer subscribes for a live feed. The
manager owns room state, an ordered per-room history, a monotonic sequence
counter, optional JSONL persistence, and fan-out to subscriber queues.

It deliberately has no knowledge of agents or HTTP so it stays trivially
testable and reusable by every layer above it.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path

from genesis.chatroom.models import ChatMessage, Room, RoomKind

logger = logging.getLogger(__name__)


class ChatroomManager:
    def __init__(self, persist_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._rooms: dict[str, Room] = {}
        self._history: dict[str, list[ChatMessage]] = {}
        self._subscribers: set[queue.Queue] = set()
        self._seq = 0
        self._persist_dir = Path(persist_dir) if persist_dir else None
        if self._persist_dir:
            self._persist_dir.mkdir(parents=True, exist_ok=True)

    # ── Rooms ───────────────────────────────────────────────────────────────

    def create_room(
        self,
        kind: RoomKind,
        title: str,
        participants: list[str] | None = None,
    ) -> Room:
        room = Room(kind=kind, title=title, participants=list(participants or []))
        with self._lock:
            self._rooms[room.id] = room
            self._history[room.id] = []
        return room

    def rooms(self) -> list[Room]:
        with self._lock:
            return list(self._rooms.values())

    def get_room(self, room_id: str) -> Room | None:
        with self._lock:
            return self._rooms.get(room_id)

    # ── Messages ────────────────────────────────────────────────────────────

    def post(
        self,
        room_id: str,
        sender: str,
        role: str,
        content: str,
        kind: str = "message",
    ) -> ChatMessage:
        with self._lock:
            if room_id not in self._rooms:
                raise KeyError(f"Unknown room: {room_id}")
            self._seq += 1
            msg = ChatMessage(
                room_id=room_id,
                seq=self._seq,
                sender=sender,
                role=role,
                content=content,
                kind=kind,
            )
            self._history[room_id].append(msg)
            subscribers = list(self._subscribers)

        self._persist(msg)
        # Fan out outside the lock so a slow/full subscriber can't block posters.
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                logger.debug("Dropping message for a full subscriber queue")
        return msg

    def history(self, room_id: str) -> list[ChatMessage]:
        with self._lock:
            return list(self._history.get(room_id, []))

    def all_messages(self) -> list[ChatMessage]:
        """Every message across all rooms, ordered by seq. Used for viewer replay."""
        with self._lock:
            merged: list[ChatMessage] = []
            for msgs in self._history.values():
                merged.extend(msgs)
        merged.sort(key=lambda m: m.seq)
        return merged

    # ── Subscriptions (for the SSE server) ──────────────────────────────────

    def subscribe(self, maxsize: int = 1000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    # ── Persistence ─────────────────────────────────────────────────────────

    def _persist(self, msg: ChatMessage) -> None:
        if not self._persist_dir:
            return
        path = self._persist_dir / f"{msg.room_id}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg.to_dict()) + "\n")
        except OSError as e:
            logger.debug("Chatroom persistence failed: %s", e)
