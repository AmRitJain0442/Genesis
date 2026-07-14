import subprocess
import tempfile
import types
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

    def test_sensitive_untracked_credential_is_excluded_from_checkpoint(self):
        (self.repo / "seed.py").write_text("old\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "seed.py").write_text("new\n", encoding="utf-8")
        credential = self.repo / "my-product-sa-key.json"
        credential.write_text('{"private_key": "live"}\n', encoding="utf-8")

        sha = self._wt().prepare_main()

        self.assertTrue(sha)
        self.assertEqual("new\n", self._git_output("show", "HEAD:seed.py"))
        self.assertNotIn(
            "my-product-sa-key.json",
            self._git_output("ls-tree", "--name-only", "HEAD").splitlines(),
        )
        self.assertTrue(credential.exists())

    def test_workspace_snapshot_reports_ignored_source_and_sensitive_paths(self):
        (self.repo / ".gitignore").write_text("legacy.py\n", encoding="utf-8")
        (self.repo / "seed.py").write_text("seed\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "legacy.py").write_text("value = 1\n", encoding="utf-8")
        (self.repo / "service-account.json").write_text(
            '{"private_key": "live"}\n',
            encoding="utf-8",
        )

        snapshot = self._wt().workspace_snapshot()

        self.assertIn("legacy.py", snapshot["ignored_source"])
        self.assertIn("service-account.json", snapshot["sensitive_paths"])

    def test_capture_patch_includes_worker_commits_on_takeover_branch(self):
        (self.repo / "seed.py").write_text("old\n", encoding="utf-8")
        _git(self.repo, "add", "seed.py")
        _git(self.repo, "commit", "-m", "init")
        manager = self._wt()
        worktree = manager.create("run", "step")
        try:
            _git(worktree, "switch", "-c", "takeover/test")
            (worktree / "seed.py").write_text("new\n", encoding="utf-8")
            _git(worktree, "add", "seed.py")
            _git(worktree, "commit", "-m", "worker committed implementation")
            (worktree / "added.py").write_text("value = 1\n", encoding="utf-8")
            _git(worktree, "add", "added.py")
            _git(worktree, "commit", "-m", "worker committed final documentation")

            patch = manager.capture_patch(worktree)

            self.assertEqual(["added.py", "seed.py"], patch.changed_files)
            self.assertTrue(patch.has_changes)
            self.assertTrue(patch.base_sha)
            self.assertTrue(patch.head_sha)
            self.assertEqual(16, len(patch.patch_sha))
            self.assertEqual([], patch.status_lines)
            self.assertTrue(any(
                "added.py" in line for line in patch.diff_status_lines
            ))
            self.assertIn("added.py", patch.patch_text)
            manager.apply_check(patch.patch_text)
            manager.apply_patch(patch.patch_text)
            self.assertEqual("new\n", (self.repo / "seed.py").read_text(encoding="utf-8"))
            self.assertEqual(
                "value = 1\n",
                (self.repo / "added.py").read_text(encoding="utf-8"),
            )
        finally:
            manager.remove(worktree)

    def test_referenced_ignored_source_is_overlaid_and_patched_safely(self):
        (self.repo / ".gitignore").write_text("tribe.py\n.env\n", encoding="utf-8")
        (self.repo / "seed.py").write_text("seed\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        (self.repo / "tribe.py").write_text("KEY = 'old'\n", encoding="utf-8")
        (self.repo / ".env").write_text("SECRET=live\n", encoding="utf-8")
        manager = self._wt()
        worktree = manager.create("overlay", "step")
        step = types.SimpleNamespace(
            title="Harden tribe.py",
            description="Move secrets out of tribe.py and never copy .env.",
            context_hint="",
            expected_output="tribe.py reads environment variables",
            file_scope=["tribe.py", ".env"],
        )
        try:
            overlaid = manager.materialize_referenced_ignored(worktree, step)

            self.assertEqual(["tribe.py"], overlaid)
            self.assertTrue((worktree / "tribe.py").exists())
            self.assertFalse((worktree / ".env").exists())
            (worktree / "tribe.py").write_text(
                "import os\nKEY = os.environ['TRIBE_API_KEY']\n",
                encoding="utf-8",
            )

            patch = manager.capture_patch(worktree)

            self.assertIn("tribe.py", patch.changed_files)
            self.assertIn("M\ttribe.py", patch.diff_status_lines)
            manager.apply_check(patch.patch_text)
            manager.apply_patch(patch.patch_text)
            self.assertIn(
                "os.environ",
                (self.repo / "tribe.py").read_text(encoding="utf-8"),
            )
        finally:
            manager.remove(worktree)

    def test_explicit_safe_ignored_artifact_is_force_staged(self):
        (self.repo / ".gitignore").write_text(".env*\n", encoding="utf-8")
        (self.repo / "seed.py").write_text("seed\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        manager = self._wt()
        worktree = manager.create("ignored-artifact", "step")
        step = types.SimpleNamespace(
            title="Add .env.example",
            description="Create a tracked .env.example template.",
            context_hint="",
            expected_output=".env.example exists",
            file_scope=[".env.example"],
        )
        try:
            (worktree / ".env.example").write_text(
                "API_KEY=your-api-key\n",
                encoding="utf-8",
            )

            patch = manager.capture_patch(worktree, step)

            self.assertIn(".env.example", patch.changed_files)
            self.assertIn(".env.example", patch.patch_text)
        finally:
            manager.remove(worktree)


if __name__ == "__main__":
    unittest.main()
