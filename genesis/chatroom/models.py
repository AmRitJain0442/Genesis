"""Value types for the chatroom substrate. No behavior beyond serialization."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class RoomKind(str, Enum):
    """What a room is for. `str` mixin so it serializes as a plain string."""
    brain_room = "brain_room"      # brains discussing / ideating
    worker_room = "worker_room"    # a brain and its assigned worker(s)
    review_room = "review_room"    # reviewers evaluating work
    system = "system"              # run-level status / lifecycle events


@dataclass(frozen=True)
class ChatMessage:
    """A single utterance in a room.

    `seq` is a per-manager monotonic counter so viewers can order and de-dupe
    messages across rooms without relying on wall-clock timestamps.
    """
    room_id: str
    seq: int
    sender: str
    role: str            # brain | worker | reviewer | system
    content: str
    kind: str = "message"   # message | code | decision | tool | status
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "seq": self.seq,
            "sender": self.sender,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChatMessage":
        return cls(
            room_id=data["room_id"],
            seq=data["seq"],
            sender=data["sender"],
            role=data["role"],
            content=data["content"],
            kind=data.get("kind", "message"),
            ts=data.get("ts", time.time()),
            id=data.get("id", uuid.uuid4().hex),
        )


@dataclass
class Room:
    kind: RoomKind
    title: str
    participants: list[str] = field(default_factory=list)
    created_ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "participants": list(self.participants),
            "created_ts": self.created_ts,
        }
