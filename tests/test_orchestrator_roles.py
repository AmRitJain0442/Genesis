import json
import tempfile
import types
import unittest
from pathlib import Path

from genesis.agents.orchestrator import Orchestrator
from genesis.agents.worker import WorkerResult
from genesis.chatroom import ChatroomManager, RoomKind
from genesis.config import GenesisConfig
from genesis.schemas.plan import Plan, Step


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


class ReviewerContextTests(unittest.TestCase):
    def test_large_diff_is_bounded_and_samples_both_ends(self) -> None:
        diff = (
            "diff --git a/first.py b/first.py\n" + "a" * 40000
            + "\ndiff --git a/last.py b/last.py\n" + "z" * 40000
        )

        bounded = Orchestrator._bounded_diff(diff, max_chars=8000)

        self.assertLessEqual(len(bounded), 8000)
        self.assertIn("first.py", bounded)
        self.assertIn("last.py", bounded)
        self.assertIn("sampled", bounded)

    def test_reviewer_receives_full_step_and_retained_plan_context(self) -> None:
        class CapturingReviewer:
            name = "reviewer"

            def __init__(self):
                self.prompt = ""

            def chat_review(self, system, messages):
                self.prompt = messages[0]["content"]
                return json.dumps({
                    "step_id": "step-1",
                    "verdict": "approved",
                    "quality_score": 9,
                    "feedback": "",
                    "memory_note": "Implemented the requested behavior.",
                    "should_retry": False,
                    "suggested_revision": "",
                })

        reviewer = CapturingReviewer()
        memory = types.SimpleNamespace(get_summary=lambda n: "MEM")
        orch = Orchestrator(
            reviewer,
            {"worker": object()},
            memory,
            git=None,
            config=GenesisConfig(),
            work_dir=".",
        )
        step = Step(
            step_id="step-1",
            title="Implement feature",
            description="Preserve the detailed implementation requirement.",
            type="code",
            file_scope=["src/expected.py"],
            expected_output="The feature works end to end.",
            context_hint="Also inspect src/support.py.",
        )
        plan = Plan(
            task_id="run-context",
            task_summary="Build the complete feature",
            estimated_steps=1,
            steps=[step],
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "actual.py").write_text("value = 1\n", encoding="utf-8")
            result = WorkerResult(
                step_id="step-1",
                raw_response="done",
                result_text="Changed a supporting file because it was required.",
                files_written=["actual.py"],
                evidence={"version": 3, "patch_sha": "abc123def456"},
            )
            orch.review(
                step,
                result,
                plan=plan,
                work_dir=root,
                diff_text="diff --git a/actual.py b/actual.py\n+value = 1\n",
            )

        self.assertIn("Build the complete feature", reviewer.prompt)
        self.assertIn("Preserve the detailed implementation requirement", reviewer.prompt)
        self.assertIn("scheduling hint", reviewer.prompt)
        self.assertIn("Changed a supporting file", reviewer.prompt)
        self.assertIn("SHARED PROJECT MEMORY:\nMEM", reviewer.prompt)
        self.assertIn("Version: 3", reviewer.prompt)
        self.assertIn("Patch SHA: abc123def456", reviewer.prompt)
        self.assertIn("verdict applies only to this exact patch SHA", reviewer.prompt)


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
