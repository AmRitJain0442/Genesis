import json
import types
import unittest

from genesis.agents.worker_dialogue import WorkerDialogue
from genesis.agents.orchestrator import Orchestrator
from genesis.config import GenesisConfig


def _result(files=None, success=True, text="did the thing", error=""):
    return types.SimpleNamespace(
        success=success, files_written=list(files or []), result_text=text, error=error,
    )


def _step(step_id="s1"):
    return types.SimpleNamespace(step_id=step_id, title="t", type="code",
                                 description="d", expected_output="out")


class _Recorder:
    def __init__(self):
        self.posts = []
        self.kinds = []

    def __call__(self, sender, role, content, kind="message"):
        self.posts.append((role, content))
        self.kinds.append(kind)


class WorkerDialogueTests(unittest.TestCase):
    def _dialogue(self, *, evaluations, max_turns=3, worker_results=None, fast_path=False):
        step = _step()
        results = iter(worker_results or [_result(["a.py"]), _result(["a.py", "test_a.py"]),
                                          _result(["a.py"])])
        evals = iter(evaluations)
        rec = _Recorder()

        def run_worker(s):
            return next(results)

        def evaluate(s, r, turn):
            return next(evals)

        def make_revision(s, feedback):
            return _step(step_id=s.step_id)  # feedback carried implicitly

        dlg = WorkerDialogue(
            step=step, worker_name="codex-main", brain_name="claude", max_turns=max_turns,
            run_worker=run_worker, evaluate=evaluate, make_revision=make_revision, post=rec,
            fast_path=fast_path,
        )
        return dlg.run(), rec

    def test_approves_after_one_revision(self) -> None:
        outcome, rec = self._dialogue(evaluations=[(False, "add tests"), (True, "")])
        self.assertTrue(outcome.approved)
        self.assertEqual(2, outcome.turns)
        roles = [
            role
            for (role, _), kind in zip(rec.posts, rec.kinds)
            if kind != "status"
        ]
        # worker, brain(revise), worker, brain(approve)
        self.assertEqual(["worker", "brain", "worker", "brain"], roles)
        self.assertIn("Revise: add tests", [c for _, c in rec.posts])

    def test_empty_turn_is_status_only_and_retries_without_brain_noise(self) -> None:
        outcome, rec = self._dialogue(
            evaluations=[(True, "")],
            worker_results=[_result(files=[], text=""), _result(["a.py"])],
        )

        self.assertTrue(outcome.approved)
        self.assertEqual(2, outcome.turns)
        self.assertFalse(any("wrote 0" in content for _, content in rec.posts))
        self.assertTrue(any(
            kind == "status" and "Actively working" in content
            for (_, content), kind in zip(rec.posts, rec.kinds)
        ))
        self.assertFalse(any("Revise:" in content for _, content in rec.posts))

    def test_fast_path_skips_redundant_director_review(self) -> None:
        outcome, rec = self._dialogue(
            evaluations=[],
            worker_results=[_result(["a.py"])],
            fast_path=True,
        )

        self.assertTrue(outcome.approved)
        self.assertEqual(1, outcome.turns)
        self.assertTrue(any(
            "handing directly to independent review" in content
            for _, content in rec.posts
        ))

    def test_result_retains_files_from_all_dialogue_turns(self) -> None:
        outcome, _ = self._dialogue(
            evaluations=[(False, "add docs"), (True, "")],
            worker_results=[
                _result(["app.py", "tests/test_app.py"]),
                _result(["SECURITY.md"]),
            ],
        )

        self.assertEqual(
            ["app.py", "tests/test_app.py", "SECURITY.md"],
            outcome.result.files_written,
        )

    def test_evidence_guard_retries_before_model_evaluation(self) -> None:
        bad = _result(["brain.html"])
        bad.evidence = {
            "guard_violations": [
                "Restore tracked files deleted outside the declared step scope: brain.html"
            ]
        }
        good = _result(["config.py"])
        good.evidence = {"guard_violations": []}

        outcome, rec = self._dialogue(
            evaluations=[(True, "")],
            worker_results=[bad, good],
        )

        self.assertTrue(outcome.approved)
        self.assertEqual(2, outcome.turns)
        self.assertTrue(any(
            "Deterministic evidence guard failed" in content
            for _, content in rec.posts
        ))

    def test_final_turn_guard_failure_never_reports_preflight_passed(self) -> None:
        blocked = _result(["docs/PRD.md"])
        blocked.evidence = {
            "guard_violations": ["Required artifact is still missing."]
        }

        outcome, rec = self._dialogue(
            evaluations=[],
            max_turns=1,
            worker_results=[blocked],
            fast_path=True,
        )

        self.assertFalse(outcome.approved)
        messages = [content for _, content in rec.posts]
        self.assertFalse(any("preflight passed" in item.lower() for item in messages))
        self.assertTrue(any("draft is not accepted" in item.lower() for item in messages))
        self.assertTrue(any("Required artifact is still missing" in item for item in messages))

    def test_hits_turn_budget_without_approval(self) -> None:
        outcome, rec = self._dialogue(evaluations=[(False, "more"), (False, "more")], max_turns=2)
        self.assertFalse(outcome.approved)
        self.assertEqual(2, outcome.turns)
        self.assertTrue(any("Turn budget reached" in c for _, c in rec.posts))

    def test_worker_failure_ends_dialogue(self) -> None:
        outcome, rec = self._dialogue(
            evaluations=[(True, "")],
            worker_results=[_result(success=False, error="boom")],
        )
        self.assertFalse(outcome.approved)
        self.assertEqual(1, outcome.turns)
        self.assertTrue(any("Worker failed: boom" in c for _, c in rec.posts))

    def test_evaluator_exception_fails_open(self) -> None:
        step = _step()
        rec = _Recorder()

        def boom(s, r, turn):
            raise RuntimeError("judge crashed")

        dlg = WorkerDialogue(
            step=step, worker_name="w", brain_name="b", max_turns=3,
            run_worker=lambda s: _result(["a.py"]),
            evaluate=boom, make_revision=lambda s, f: s, post=rec,
        )
        outcome = dlg.run()
        self.assertTrue(outcome.approved)     # failed open, no infinite loop
        self.assertEqual(1, outcome.turns)


class BrainEvaluateTests(unittest.TestCase):
    def _orch(self, reply):
        agent = types.SimpleNamespace(
            name="claude", chat=lambda system, messages, output_callback=None: reply)
        mem = types.SimpleNamespace(get_summary=lambda n: "")
        return Orchestrator(agent, {"w": object()}, mem, git=None, config=GenesisConfig(),
                            work_dir=".")

    def test_parses_revise(self) -> None:
        orch = self._orch(json.dumps({"action": "revise", "feedback": "add a test"}))
        approve, feedback = orch._brain_evaluate(_step(), _result(["a.py"]))
        self.assertFalse(approve)
        self.assertEqual("add a test", feedback)

    def test_parses_approve(self) -> None:
        orch = self._orch(json.dumps({"action": "approve", "feedback": ""}))
        approve, _ = orch._brain_evaluate(_step(), _result(["a.py"]))
        self.assertTrue(approve)

    def test_unparseable_reply_fails_open(self) -> None:
        orch = self._orch("not json at all")
        approve, _ = orch._brain_evaluate(_step(), _result(["a.py"]))
        self.assertTrue(approve)

    def test_brain_receives_versioned_patch_evidence(self) -> None:
        captured = {}

        def chat(system, messages, output_callback=None):
            captured["prompt"] = messages[0]["content"]
            return json.dumps({"action": "approve", "feedback": ""})

        agent = types.SimpleNamespace(name="claude", chat=chat)
        mem = types.SimpleNamespace(get_summary=lambda n: "")
        orch = Orchestrator(
            agent,
            {"w": object()},
            mem,
            git=None,
            config=GenesisConfig(),
            work_dir=".",
        )
        result = _result(["app.py"])
        result.evidence = {
            "version": 2,
            "patch_sha": "abc123",
            "base_sha": "base",
            "head_sha": "head",
            "status_lines": ["M  app.py"],
            "patch_text": "diff --git a/app.py b/app.py\n+value = 1\n",
        }

        approve, _ = orch._brain_evaluate(_step(), result)

        self.assertTrue(approve)
        self.assertIn("Version: 2", captured["prompt"])
        self.assertIn("Patch ID: abc123", captured["prompt"])
        self.assertIn("ACTUAL PATCH:", captured["prompt"])
        self.assertIn("+value = 1", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
