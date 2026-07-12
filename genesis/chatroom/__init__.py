"""
Chatroom substrate for Genesis.

A dependency-light, thread-safe message bus that agents post to and a localhost
web viewer streams from. The substrate knows nothing about agents or HTTP — it
is pure transport + storage — so it can be tested in isolation and reused by the
collaboration, worker, and review layers built on top of it.
"""
from genesis.chatroom.models import ChatMessage, Room, RoomKind
from genesis.chatroom.bus import ChatroomManager
from genesis.chatroom.server import ChatroomServer

__all__ = ["ChatMessage", "Room", "RoomKind", "ChatroomManager", "ChatroomServer"]
