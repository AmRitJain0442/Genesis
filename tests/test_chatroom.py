import json
import tempfile
import types
import unittest
import urllib.request
from pathlib import Path

from genesis.chatroom import ChatroomManager, ChatroomServer, RoomKind
from genesis.config import GenesisConfig
from genesis.repl import GenesisREPL


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


class ChatroomBridgeTests(unittest.TestCase):
    def test_failed_step_is_a_draft_followed_by_a_distinct_blocked_message(self) -> None:
        repl = GenesisREPL.__new__(GenesisREPL)
        repl.config = GenesisConfig()
        repl.chatroom = ChatroomManager()
        repl._agents = {}
        repl._get_orchestrator = lambda: types.SimpleNamespace(name="brain")
        callbacks_seen: list[str] = []
        bridged = repl._bridge_callbacks_to_chatroom(
            {
                "on_step_result": lambda *args: callbacks_seen.append("result"),
                "on_error": lambda *args: callbacks_seen.append("error"),
            },
            "Build docs",
        )
        step = types.SimpleNamespace(step_id="step-1")
        result = types.SimpleNamespace(
            files_written=["docs/ASSUMPTIONS.md", "docs/PRD.md", "docs/TRACEABILITY.md"]
        )

        bridged["on_step_result"](step, result, "worker")
        bridged["on_error"](step, "Deterministic acceptance gates failed")

        messages = repl.chatroom.all_messages()
        self.assertEqual(["result", "error"], callbacks_seen)
        self.assertEqual(2, len(messages))
        self.assertEqual("tool", messages[0].kind)
        self.assertIn("DRAFT PATCH | step-1 | 3 files | not applied", messages[0].content)
        self.assertIn("\n- docs/PRD.md\n", messages[0].content)
        self.assertNotIn("wrote", messages[0].content.lower())
        self.assertEqual("status", messages[1].kind)
        self.assertEqual(
            "BLOCKED | step-1\nDeterministic acceptance gates failed",
            messages[1].content,
        )

    def test_repair_callback_is_phase_labelled_in_run_room(self) -> None:
        repl = GenesisREPL.__new__(GenesisREPL)
        repl.config = GenesisConfig()
        repl.chatroom = ChatroomManager()
        repl._agents = {}
        repl._get_orchestrator = lambda: types.SimpleNamespace(name="brain")
        bridged = repl._bridge_callbacks_to_chatroom(
            {"on_repair": lambda *args: None},
            "Repair docs",
        )
        step = types.SimpleNamespace(step_id="step-1")

        bridged["on_repair"](step, {
            "attempts_used": 1,
            "budget_total": 2,
            "stage": "acceptance",
            "reason": "Required artifact requirements.txt is missing.",
        })

        message = repl.chatroom.all_messages()[0]
        self.assertEqual("status", message.kind)
        self.assertEqual("brain", message.sender)
        self.assertEqual(
            "REPAIR 1/2 | step-1 | ACCEPTANCE\n"
            "Required artifact requirements.txt is missing.",
            message.content,
        )


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

    def test_index_exposes_accessible_responsive_viewer_controls(self) -> None:
        body = self._get("/").decode("utf-8")

        self.assertIn("GENESIS // MISSION LOG", body)
        self.assertIn('id="room-search"', body)
        self.assertIn('id="room-filter"', body)
        self.assertIn('id="message-search"', body)
        self.assertIn('id="message-filter"', body)
        self.assertIn('id="connection-status" role="status" aria-live="polite"', body)
        self.assertIn('role="log" aria-live="polite"', body)
        self.assertIn('id="follow-live"', body)
        self.assertIn('id="jump-latest"', body)
        self.assertIn('aria-controls="sidebar"', body)
        self.assertIn("@media (max-width:860px)", body)

    def test_index_includes_deduplication_and_gap_recovery_hooks(self) -> None:
        body = self._get("/").decode("utf-8")

        self.assertIn("seenMessageIds", body)
        self.assertIn("seenMessageSeqs", body)
        self.assertIn("scheduleResync", body)
        self.assertIn('fetchJSON("/api/snapshot")', body)
        self.assertIn("appendVisibleMessage", body)
        self.assertIn("MAX_RENDERED_EVENTS = 500", body)
        self.assertIn("trimRenderedWindow", body)

    def test_responses_disable_caching_and_set_browser_safety_headers(self) -> None:
        with urllib.request.urlopen(self.url + "/", timeout=3) as response:
            self.assertEqual("no-store", response.headers["Cache-Control"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
            self.assertEqual("DENY", response.headers["X-Frame-Options"])
            self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("connect-src 'self'", csp)

    def test_api_rooms_reflects_created_rooms(self) -> None:
        room = self.mgr.create_room(RoomKind.brain_room, "Design", ["claude"])
        data = json.loads(self._get("/api/rooms"))
        ids = [r["id"] for r in data]
        self.assertIn(room.id, ids)

    def test_snapshot_api_returns_rooms_messages_and_sequence_cursor(self) -> None:
        room = self.mgr.create_room(RoomKind.worker_room, "Implement viewer", ["brain", "worker"])
        first = self.mgr.post(room.id, "brain", "brain", "Start the implementation")
        second = self.mgr.post(room.id, "worker", "worker", "Viewer updated", "code")

        snapshot = json.loads(self._get("/api/snapshot"))

        self.assertEqual(second.seq, snapshot["last_seq"])
        self.assertEqual([room.id], [item["id"] for item in snapshot["rooms"]])
        self.assertEqual([first.id, second.id], [item["id"] for item in snapshot["messages"]])

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
        self.assertEqual(str(snapshot["data"]["last_seq"]), snapshot["id"])
        self.assertEqual("2000", snapshot["retry"])

        # A new post should arrive as a streamed message event.
        posted = self.mgr.post(room.id, "codex", "brain", "second")
        msg = self._read_event(resp)
        self.assertEqual("message", msg["event"])
        self.assertEqual("second", msg["data"]["content"])
        self.assertEqual(str(posted.seq), msg["id"])

    def test_events_resume_after_last_event_id_without_replaying_older_messages(self) -> None:
        room = self.mgr.create_room(RoomKind.brain_room, "Design")
        first = self.mgr.post(room.id, "claude", "brain", "first")
        second = self.mgr.post(room.id, "codex", "brain", "second")
        request = urllib.request.Request(
            self.url + "/events",
            headers={"Last-Event-ID": str(first.seq)},
        )
        response = urllib.request.urlopen(request, timeout=3)
        self.addCleanup(response.close)

        snapshot = self._read_event(response)

        self.assertEqual("snapshot", snapshot["event"])
        self.assertEqual(["second"], [item["content"] for item in snapshot["data"]["messages"]])
        self.assertEqual(second.seq, snapshot["data"]["last_seq"])
        self.assertEqual(str(second.seq), snapshot["id"])

    def test_stale_event_cursor_triggers_full_replay_and_client_reset(self) -> None:
        room = self.mgr.create_room(RoomKind.system, "Restarted run")
        posted = self.mgr.post(room.id, "system", "system", "server restarted", "status")
        request = urllib.request.Request(
            self.url + "/events",
            headers={"Last-Event-ID": "9999"},
        )
        response = urllib.request.urlopen(request, timeout=3)
        self.addCleanup(response.close)

        snapshot = self._read_event(response)

        self.assertTrue(snapshot["data"]["reset"])
        self.assertEqual([posted.id], [item["id"] for item in snapshot["data"]["messages"]])
        self.assertEqual(posted.seq, snapshot["data"]["last_seq"])
        self.assertEqual(str(posted.seq), snapshot["id"])

    @staticmethod
    def _read_event(resp) -> dict:
        """Read one SSE event (skipping keepalive comments) into {event, data}."""
        event = None
        event_id = None
        retry = None
        while True:
            raw = resp.readline()
            if not raw:
                raise AssertionError("stream closed before an event arrived")
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith(":"):      # keepalive comment
                continue
            if line.startswith("id:"):
                event_id = line.split(":", 1)[1].strip()
            elif line.startswith("retry:"):
                retry = line.split(":", 1)[1].strip()
            elif line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                return {"event": event, "data": data, "id": event_id, "retry": retry}


if __name__ == "__main__":
    unittest.main()
