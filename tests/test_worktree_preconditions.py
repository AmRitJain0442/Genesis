import subprocess
import tempfile
import unittest
from pathlib import Path

from genesis.worktree import WorktreeManager


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


class EnsureCleanMainTests(unittest.TestCase):
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

    def test_no_commits_reports_missing_base(self):
        # Fresh `git init`, no commit — has_head is False.
        wt = self._wt()
        self.assertFalse(wt.has_head())
        with self.assertRaises(RuntimeError) as ctx:
            wt.ensure_clean_main()
        self.assertIn("no commits yet", str(ctx.exception))

    def test_untracked_files_reported_as_untracked(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        # Now add an untracked file — tree is dirty via ??.
        (self.repo / "new.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self._wt().ensure_clean_main()
        msg = str(ctx.exception)
        self.assertIn("untracked files", msg)
        self.assertIn("new.py", msg)

    def test_clean_repo_passes(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        self.assertTrue(self._wt().has_head())
        self._wt().ensure_clean_main()  # must not raise

    def test_ignored_paths_do_not_count_as_dirty(self):
        (self.repo / "seed.py").write_text("print('hi')\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "GENESIS_MEMORY.md").write_text("notes\n", encoding="utf-8")
        # Untracked GENESIS_MEMORY.md is ignored → still clean.
        self._wt().ensure_clean_main(ignore_paths=["GENESIS_MEMORY.md", ".genesis/"])


if __name__ == "__main__":
    unittest.main()
