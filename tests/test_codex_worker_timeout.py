import tempfile
import types
import unittest
from pathlib import Path

from genesis.agents.codex_worker import CodexWorker


def _step():
    return types.SimpleNamespace(
        step_id="step-1",
        title="Implement feature",
        type="code",
        description="Make the requested change.",
        file_scope=[],
        expected_output="Feature implemented.",
        context_hint="",
    )


class CodexWorkerTimeoutTests(unittest.TestCase):
    def test_timeout_preserves_partial_file_changes_for_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Agent:
                def execute_task(self, prompt, output_callback=None):
                    self.callback = output_callback
                    (root / "partial.py").write_text("value = 1\n", encoding="utf-8")
                    raise RuntimeError(
                        "Codex produced no activity for 600s and was stopped"
                    )

            agent = Agent()
            result = CodexWorker(agent, "", str(root)).execute(_step())

            self.assertTrue(result.success)
            self.assertEqual(["partial.py"], result.files_written)
            self.assertIn("preserved", result.result_text)
            self.assertIsNotNone(agent.callback)

    def test_timeout_without_changes_remains_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Agent:
                def execute_task(self, prompt, output_callback=None):
                    raise RuntimeError(
                        "Codex produced no activity for 600s and was stopped"
                    )

            result = CodexWorker(Agent(), "", tmp).execute(_step())

            self.assertFalse(result.success)
            self.assertIn("no activity for", result.error)


if __name__ == "__main__":
    unittest.main()
