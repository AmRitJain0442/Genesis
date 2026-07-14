import subprocess
import tempfile
import unittest
from pathlib import Path

from genesis.config import GenesisConfig
from genesis.git_ops import GitManager


class GitManagerCommitTests(unittest.TestCase):
    def test_commit_step_stages_only_reviewed_paths_including_ignored_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
            (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            (root / "seed.py").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "ignored.py").write_text("value = 1\n", encoding="utf-8")
            (root / "unrelated.txt").write_text("do not commit\n", encoding="utf-8")
            manager = GitManager(str(root), GenesisConfig().git)

            sha = manager.commit_step(
                "step-1",
                "reviewed change",
                paths=["ignored.py"],
            )

            self.assertTrue(sha)
            tree = subprocess.run(
                ["git", "ls-tree", "--name-only", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn("ignored.py", tree)
            self.assertNotIn("unrelated.txt", tree)
            self.assertTrue((root / "unrelated.txt").exists())
            manager.close()


if __name__ == "__main__":
    unittest.main()
