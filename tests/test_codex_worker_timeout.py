import tempfile
import types
import unittest
import subprocess
from pathlib import Path
from unittest import mock

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
    @staticmethod
    def _init_repo(root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=root,
            check=True,
        )

    def test_git_snapshot_ignores_cache_noise_and_detects_tracked_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            (root / ".gitignore").write_text(
                ".pytest_cache/\ntrain_data/\n",
                encoding="utf-8",
            )
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            class Agent:
                def execute_task(self, prompt, output_callback=None):
                    (root / "app.py").unlink()
                    cache = root / ".pytest_cache" / "v" / "cache"
                    cache.mkdir(parents=True)
                    (cache / "nodeids").write_text("[]", encoding="utf-8")
                    generated = root / "train_data"
                    generated.mkdir()
                    (generated / "atlas.npz").write_bytes(b"generated")
                    return "done"

            result = CodexWorker(Agent(), "", str(root)).execute(_step())

            self.assertEqual(["app.py"], result.files_written)

    def test_clean_unchanged_tracked_files_are_not_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            app = root / "app.py"
            untouched = root / "large.bin"
            app.write_text("value = 1\n", encoding="utf-8")
            untouched.write_bytes(b"x" * 100_000)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            class Agent:
                def execute_task(self, prompt, output_callback=None):
                    app.write_text("value = 2\n", encoding="utf-8")
                    return "done"

            paths_read: list[Path] = []
            original_read_bytes = Path.read_bytes

            def tracked_read_bytes(path: Path) -> bytes:
                paths_read.append(path)
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", tracked_read_bytes):
                result = CodexWorker(Agent(), "", str(root)).execute(_step())

            self.assertEqual(["app.py"], result.files_written)
            self.assertNotIn(untouched, paths_read)

    def test_timeout_detects_changes_committed_by_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)
            app = root / "app.py"
            app.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )

            class Agent:
                def execute_task(self, prompt, output_callback=None):
                    app.write_text("value = 2\n", encoding="utf-8")
                    subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "worker commit"],
                        cwd=root,
                        check=True,
                        capture_output=True,
                    )
                    raise RuntimeError("Codex produced no activity for 600s and was stopped")

            result = CodexWorker(Agent(), "", str(root)).execute(_step())

            self.assertTrue(result.success)
            self.assertEqual(["app.py"], result.files_written)
            self.assertIn("preserved", result.result_text)

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
