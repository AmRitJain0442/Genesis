import types
import unittest

from genesis.agents.orchestrator import Orchestrator
from genesis.chatroom import ChatroomManager, RoomKind
from genesis.config import GenesisConfig


def _make_orch(co_brain=None, chatroom=None):
    agent = types.SimpleNamespace(name="claude-cli-orchestrator")
    memory = types.SimpleNamespace(get_summary=lambda n: "MEM")
    return Orchestrator(
        agent, {"codex-main": object()}, memory, git=None, config=GenesisConfig(),
        work_dir=".", runtime=None, palace=None, policy=None,
        co_brain=co_brain, chatroom=chatroom,
    )


def _step(step_id="s1", title="t", stype="test", desc="do it", expected="out"):
    return types.SimpleNamespace(
        step_id=step_id, title=title, type=stype,
        description=desc, expected_output=expected, file_scope=[],
    )


class SpecialtyTests(unittest.TestCase):
    def test_specialty_mapping(self) -> None:
        orch = _make_orch()
        self.assertEqual("testing & test coverage", orch._specialty_for(_step(stype="test")))
        self.assertEqual("documentation", orch._specialty_for(_step(stype="docs")))
        self.assertEqual("implementation", orch._specialty_for(_step(stype="unknown")))

    def test_step_memory_includes_specialty_directive(self) -> None:
        orch = _make_orch()
        mem = orch._step_memory(_step(stype="test"))
        self.assertIn("YOUR SPECIALTY FOR THIS STEP: testing & test coverage", mem)
        self.assertIn("MEM", mem)


class ReviewerSelectionTests(unittest.TestCase):
    def test_prefers_peer_brain_as_reviewer(self) -> None:
        co = types.SimpleNamespace(name="codex-orchestrator")
        orch = _make_orch(co_brain=co)
        self.assertIs(co, orch._review_agent())
        self.assertEqual("codex-orchestrator", orch._reviewer_name())

    def test_falls_back_to_primary_when_alone(self) -> None:
        orch = _make_orch(co_brain=None)
        self.assertEqual("claude-cli-orchestrator", orch._reviewer_name())


class StepRoomTests(unittest.TestCase):
    def test_open_step_room_posts_brief(self) -> None:
        mgr = ChatroomManager()
        orch = _make_orch(chatroom=mgr)
        room_id = orch._open_step_room(_step(stype="test"), "codex-main", "testing & test coverage")
        self.assertTrue(room_id)
        rooms = mgr.rooms()
        self.assertEqual(RoomKind.worker_room, rooms[0].kind)
        msgs = mgr.history(room_id)
        self.assertEqual(1, len(msgs))
        self.assertEqual("brain", msgs[0].role)
        self.assertIn("specialty: testing & test coverage", msgs[0].content)

    def test_step_room_helpers_are_noops_without_chatroom(self) -> None:
        orch = _make_orch(chatroom=None)
        self.assertEqual("", orch._open_step_room(_step(), "w", "impl"))
        orch._post_step("", "w", "worker", "hi")  # must not raise

    def test_post_step_appends_messages(self) -> None:
        mgr = ChatroomManager()
        orch = _make_orch(chatroom=mgr)
        room_id = orch._open_step_room(_step(), "codex-main", "impl")
        orch._post_step(room_id, "codex-main", "worker", "wrote 2 files", "code")
        orch._post_step(room_id, "reviewer", "reviewer", "approved (9/10)", "decision")
        roles = [m.role for m in mgr.history(room_id)]
        self.assertEqual(["brain", "worker", "reviewer"], roles)


if __name__ == "__main__":
    unittest.main()
