import time
import types
import unittest

import genesis.agents.orchestrator as orch_mod
from genesis.agents.availability import AccountRegistry, is_exhaustion_error
from genesis.agents.orchestrator import Orchestrator
from genesis.agents.worker import WorkerResult
from genesis.config import GenesisConfig


class ExhaustionDetectionTests(unittest.TestCase):
    def test_positive_signals(self) -> None:
        for text in [
            "Error: rate limit reached, try again later",
            "429 Too Many Requests",
            "Claude usage limit reached — resets at 5pm",
            "You exceeded your current quota",
            "insufficient_quota",
            "Your credit balance is too low",
        ]:
            self.assertTrue(is_exhaustion_error(text), text)

    def test_negative_signals(self) -> None:
        for text in [
            "",
            "Overloaded (529) — server busy",
            "503 temporarily unavailable",
            "SyntaxError: invalid syntax",
            "connection refused",
        ]:
            self.assertFalse(is_exhaustion_error(text), text)


class RegistryTests(unittest.TestCase):
    def test_mark_and_availability(self) -> None:
        reg = AccountRegistry(cooldown_seconds=900)
        self.assertTrue(reg.is_available("codex-main"))
        reg.mark_exhausted("codex-main")
        self.assertFalse(reg.is_available("codex-main"))
        self.assertIn("codex-main", reg.exhausted_names())

    def test_cooldown_expiry_restores_availability(self) -> None:
        reg = AccountRegistry(cooldown_seconds=900)
        reg.mark_exhausted("codex-main")
        # Simulate the cooldown elapsing.
        reg._until["codex-main"] = time.time() - 1
        self.assertTrue(reg.is_available("codex-main"))
        self.assertNotIn("codex-main", reg.exhausted_names())

    def test_clear(self) -> None:
        reg = AccountRegistry()
        reg.mark_exhausted("a")
        reg.mark_exhausted("b")
        reg.clear("a")
        self.assertTrue(reg.is_available("a"))
        self.assertFalse(reg.is_available("b"))
        reg.clear()
        self.assertTrue(reg.is_available("b"))


def _orch(worker_agents, **kw):
    agent = types.SimpleNamespace(name="claude-cli-orchestrator")
    mem = types.SimpleNamespace(get_summary=lambda n: "")
    return Orchestrator(agent, worker_agents, mem, git=None, config=GenesisConfig(),
                        work_dir=".", **kw)


def _step(step_id="s1", stype="code"):
    return types.SimpleNamespace(step_id=step_id, type=stype, title="t",
                                 description="d", expected_output="out",
                                 preferred_agent="any")


class WorkerFailoverTests(unittest.TestCase):
    def test_exhausted_worker_fails_over_to_another_account(self) -> None:
        A = types.SimpleNamespace(name="codex-main")
        B = types.SimpleNamespace(name="codex-pro2")

        def fake_make_worker(agent, mem, wd, output_callback=None):
            if agent is A:
                return types.SimpleNamespace(execute=lambda s: WorkerResult(
                    step_id=s.step_id, raw_response="", result_text="",
                    success=False, error="rate limit reached"))
            return types.SimpleNamespace(execute=lambda s: WorkerResult(
                step_id=s.step_id, raw_response="", result_text="ok",
                files_written=["x.py"], success=True))

        orig = orch_mod._make_worker
        orch_mod._make_worker = fake_make_worker
        try:
            orch = _orch({"codex-main": A, "codex-pro2": B})
            state = {"name": "codex-main", "agent": A}
            result = orch._worker_execute_with_failover(_step(), ".", None, state, "")
        finally:
            orch_mod._make_worker = orig

        self.assertTrue(result.success)
        self.assertEqual("codex-pro2", state["name"])          # took over
        self.assertIn("codex-main", orch.registry.exhausted_names())

    def test_no_alternate_surfaces_exhaustion(self) -> None:
        A = types.SimpleNamespace(name="codex-main")

        def fake_make_worker(agent, mem, wd, output_callback=None):
            return types.SimpleNamespace(execute=lambda s: WorkerResult(
                step_id=s.step_id, raw_response="", result_text="",
                success=False, error="usage limit reached"))

        orig = orch_mod._make_worker
        orch_mod._make_worker = fake_make_worker
        try:
            orch = _orch({"codex-main": A})
            state = {"name": "codex-main", "agent": A}
            result = orch._worker_execute_with_failover(_step(), ".", None, state, "")
        finally:
            orch_mod._make_worker = orig

        self.assertFalse(result.success)
        self.assertIn("codex-main", orch.registry.exhausted_names())


class BrainFailoverTests(unittest.TestCase):
    def test_invoke_fails_over_on_exhaustion(self) -> None:
        primary = types.SimpleNamespace(name="claude-cli-orchestrator")
        alt = types.SimpleNamespace(name="codex-orchestrator")
        orch = _orch({"w": object()}, co_brain=alt)

        def make_call(agent):
            if agent is primary:
                raise RuntimeError("Claude usage limit reached")
            return "ok-from-alt"

        out = orch._invoke([primary, alt], make_call)
        self.assertEqual("ok-from-alt", out)
        self.assertIn("claude-cli-orchestrator", orch.registry.exhausted_names())

    def test_invoke_skips_already_exhausted(self) -> None:
        primary = types.SimpleNamespace(name="p")
        alt = types.SimpleNamespace(name="a")
        orch = _orch({"w": object()})
        orch.registry.mark_exhausted("p")
        out = orch._invoke([primary, alt], lambda ag: ag.name)
        self.assertEqual("a", out)

    def test_invoke_raises_when_all_exhausted(self) -> None:
        p = types.SimpleNamespace(name="p")
        orch = _orch({"w": object()})

        def make_call(agent):
            raise RuntimeError("429 too many requests")

        with self.assertRaises(Exception):
            orch._invoke([p], make_call)


if __name__ == "__main__":
    unittest.main()
