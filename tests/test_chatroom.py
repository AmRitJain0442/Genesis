import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from genesis.chatroom import ChatroomManager, ChatroomServer, RoomKind


class ChatroomBusTests(unittest.TestCase):
    def test_post_assigns_increasing_seq_and_orders_history(self) -> None:
        mgr = ChatroomManager()
        room = mgr.create_room(RoomKind.brain_room, "Design", ["claude", "codex"])
        a = mgr.post(room.id, "claude", "brain", "let's start")
        b = mgr.post(room.id, "codex", "brain", "agreed")
        self.assertLess(a.seq, b.seq)
        history = mgr.history(room.id)
        self.assertEqual(["let's start", "agreed"], [m.content for m in history])

    def test_seq_is_monotonic_across_rooms(self) -> None:
        mgr = ChatroomManager()
        r1 = mgr.create_room(RoomKind.brain_room, "A")
        r2 = mgr.create_room(RoomKind.worker_room, "B")
        m1 = mgr.post(r1.id, "x", "brain", "1")
        m2 = mgr.post(r2.id, "y", "worker", "2")
        self.assertEqual([m1.seq, m2.seq], sorted([m1.seq, m2.seq]))
        merged = mgr.all_messages()
        self.assertEqual([m1.seq, m2.seq], [m.seq for m in merged])

    def test_post_to_unknown_room_raises(self) -> None:
        mgr = ChatroomManager()
        with self.assertRaises(KeyError):
            mgr.post("nope", "x", "brain", "hi")

    def test_subscriber_receives_posts_and_unsubscribe_stops_them(self) -> None:
        mgr = ChatroomManager()
        room = mgr.create_room(RoomKind.system, "run")
        q = mgr.subscribe()
        posted = mgr.post(room.id, "sys", "system", "started")
        received = q.get(timeout=1)
        self.assertEqual(posted.id, received.id)

        mgr.unsubscribe(q)
        mgr.post(room.id, "sys", "system", "again")
        self.assertTrue(q.empty())

    def test_persistence_writes_jsonl_per_room(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = ChatroomManager(persist_dir=tmp)
            room = mgr.create_room(RoomKind.brain_room, "Design")
            mgr.post(room.id, "claude", "brain", "hello")
            mgr.post(room.id, "codex", "brain", "world")
            path = Path(tmp) / f"{room.id}.jsonl"
            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(2, len(lines))
            self.assertEqual("hello", json.loads(lines[0])["content"])


class ChatroomServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = ChatroomManager()
        self.server = ChatroomServer(self.mgr, port=0)
        self.url = self.server.start()
        self.addCleanup(self.server.stop)

    def _get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=3) as resp:
            return resp.read()

    def test_start_returns_localhost_url(self) -> None:
        self.assertTrue(self.url.startswith("http://127.0.0.1:"))
        self.assertTrue(self.server.running)

    def test_index_page_served(self) -> None:
        body = self._get("/").decode("utf-8")
        self.assertIn("Genesis", body)
        self.assertIn("/events", body)

    def test_api_rooms_reflects_created_rooms(self) -> None:
        room = self.mgr.create_room(RoomKind.brain_room, "Design", ["claude"])
        data = json.loads(self._get("/api/rooms"))
        ids = [r["id"] for r in data]
        self.assertIn(room.id, ids)

    def test_events_stream_replays_snapshot_then_streams_message(self) -> None:
        room = self.mgr.create_room(RoomKind.brain_room, "Design")
        self.mgr.post(room.id, "claude", "brain", "first")

        req = urllib.request.Request(self.url + "/events")
        resp = urllib.request.urlopen(req, timeout=3)
        self.addCleanup(resp.close)

        # Read the snapshot event (contains the pre-existing message).
        snapshot = self._read_event(resp)
        self.assertEqual("snapshot", snapshot["event"])
        self.assertIn("first", [m["content"] for m in snapshot["data"]["messages"]])

        # A new post should arrive as a streamed message event.
        self.mgr.post(room.id, "codex", "brain", "second")
        msg = self._read_event(resp)
        self.assertEqual("message", msg["event"])
        self.assertEqual("second", msg["data"]["content"])

    @staticmethod
    def _read_event(resp) -> dict:
        """Read one SSE event (skipping keepalive comments) into {event, data}."""
        event = None
        while True:
            raw = resp.readline()
            if not raw:
                raise AssertionError("stream closed before an event arrived")
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith(":"):      # keepalive comment
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                return {"event": event, "data": data}


if __name__ == "__main__":
    unittest.main()
