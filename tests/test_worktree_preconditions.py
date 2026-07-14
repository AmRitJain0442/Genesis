import subprocess
import tempfile
import unittest
from pathlib import Path

from genesis.worktree import WorktreeManager


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class PrepareMainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init")
        _git(self.repo, "config", "user.email", "t@t.t")
        _git(self.repo, "config", "user.name", "t")

    def tearDown(self):
        self._tmp.cleanup()

    def _wt(self):
        return WorktreeManager(self.repo)

    def _git_output(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_no_commits_creates_initial_checkpoint(self):
        wt = self._wt()
        self.assertFalse(wt.has_head())

        sha = wt.prepare_main()

        self.assertTrue(sha)
        self.assertTrue(wt.has_head())
        self.assertIn("initial project state", self._git_output("log", "-1", "--pretty=%s"))

    def test_untracked_files_are_checkpointed(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "new.py").write_text("x = 1\n", encoding="utf-8")

        sha = self._wt().prepare_main()

        self.assertTrue(sha)
        self.assertEqual("", self._git_output("status", "--porcelain"))
        self.assertEqual("x = 1\n", self._git_output("show", "HEAD:new.py"))

    def test_clean_repo_does_not_create_checkpoint(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        self.assertTrue(self._wt().has_head())
        self.assertIsNone(self._wt().prepare_main())

    def test_ignored_paths_are_not_checkpointed(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "GENESIS_MEMORY.md").write_text("notes\n", encoding="utf-8")

        sha = self._wt().prepare_main(
            ignore_paths=["GENESIS_MEMORY.md", ".genesis/"]
        )

        self.assertIsNone(sha)
        self.assertIn("GENESIS_MEMORY.md", self._git_output("status", "--porcelain"))

    def test_staged_ignored_path_is_not_folded_into_checkpoint(self):
        (self.repo / "seed.py").write_text("old\n", encoding="utf-8")
        (self.repo / "GENESIS_MEMORY.md").write_text("old notes\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "seed.py").write_text("new\n", encoding="utf-8")
        (self.repo / "GENESIS_MEMORY.md").write_text("new notes\n", encoding="utf-8")
        _git(self.repo, "add", "GENESIS_MEMORY.md")

        sha = self._wt().prepare_main(ignore_paths=["GENESIS_MEMORY.md"])

        self.assertTrue(sha)
        self.assertEqual("new\n", self._git_output("show", "HEAD:seed.py"))
        self.assertEqual(
            "old notes\n", self._git_output("show", "HEAD:GENESIS_MEMORY.md")
        )
        self.assertEqual(
            ["GENESIS_MEMORY.md"],
            self._git_output("diff", "--cached", "--name-only").splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
