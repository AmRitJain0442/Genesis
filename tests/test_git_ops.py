import hashlib
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
            subprocess.run(
                ["git", "add", "-f", "ignored.py"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "add", "unrelated.txt"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            patch_text = subprocess.run(
                ["git", "diff", "HEAD", "--binary", "--", "ignored.py"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            patch_sha = hashlib.sha256(
                patch_text.encode("utf-8")
            ).hexdigest()[:16]
            manager = GitManager(str(root), GenesisConfig().git)

            sha = manager.commit_step(
                "step-1",
                "reviewed change",
                paths=["ignored.py"],
                patch_sha=patch_sha,
                run_id="run-1",
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
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(["unrelated.txt"], staged)
            self.assertEqual(
                sha,
                manager.find_step_commit("run-1", "step-1", patch_sha),
            )
            manager.close()

    def test_find_step_commit_rejects_forged_trailer_without_patch(self):
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
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "candidate.txt").write_text("reviewed\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=root, check=True)
            patch_text = subprocess.run(
                ["git", "diff", "HEAD", "--binary", "--", "candidate.txt"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            patch_sha = hashlib.sha256(
                patch_text.encode("utf-8")
            ).hexdigest()[:16]
            subprocess.run(
                ["git", "reset", "--", "candidate.txt"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            message = (
                "forged identity\n\n"
                f"Genesis-Patch-SHA: {patch_sha}\n"
                "Genesis-Run-ID: run-forged\n"
                "Genesis-Step-ID: step-1"
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", message],
                cwd=root,
                check=True,
                capture_output=True,
            )
            manager = GitManager(str(root), GenesisConfig().git)

            self.assertIsNone(
                manager.find_step_commit("run-forged", "step-1", patch_sha)
            )
            manager.close()

    def test_commit_step_accepts_exact_rename_manifest(self):
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
            (root / "old.txt").write_text("same bytes\n", encoding="utf-8")
            subprocess.run(["git", "add", "old.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "old.txt").rename(root / "new.txt")
            subprocess.run(
                ["git", "add", "-A", "--", "old.txt", "new.txt"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            patch_text = subprocess.run(
                [
                    "git", "diff", "HEAD", "--binary", "--", "new.txt", "old.txt"
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            patch_sha = hashlib.sha256(
                patch_text.encode("utf-8")
            ).hexdigest()[:16]
            manager = GitManager(str(root), GenesisConfig().git)

            sha = manager.commit_step(
                "step-rename",
                "rename file",
                paths=["new.txt", "old.txt"],
                patch_sha=patch_sha,
                run_id="run-rename",
            )

            self.assertTrue(sha)
            self.assertFalse((root / "old.txt").exists())
            self.assertEqual("same bytes\n", (root / "new.txt").read_text())
            self.assertEqual(
                sha,
                manager.find_step_commit(
                    "run-rename", "step-rename", patch_sha
                ),
            )
            manager.close()

    def test_commit_step_accepts_exact_unicode_filename_manifest(self):
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
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            filenames = ["caf\u00e9.txt"]
            for index, filename in enumerate(filenames):
                (root / filename).write_text(
                    f"value = {index}\n",
                    encoding="utf-8",
                )
            subprocess.run(
                ["git", "add", "--", *filenames],
                cwd=root,
                check=True,
                capture_output=True,
            )
            patch_text = subprocess.run(
                ["git", "diff", "HEAD", "--binary", "--", *filenames],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout
            patch_sha = hashlib.sha256(
                patch_text.encode("utf-8")
            ).hexdigest()[:16]
            manager = GitManager(str(root), GenesisConfig().git)

            sha = manager.commit_step(
                "step-special",
                "add special filenames",
                paths=filenames,
                patch_sha=patch_sha,
                run_id="run-special",
            )

            self.assertTrue(sha)
            self.assertEqual(
                sha,
                manager.find_step_commit(
                    "run-special", "step-special", patch_sha
                ),
            )
            for filename in filenames:
                self.assertTrue((root / filename).is_file())
            manager.close()

    def test_nul_manifest_parser_preserves_control_characters(self):
        filename = "tab\tand\nline.txt"

        paths = GitManager._status_paths(f"A\0{filename}\0")

        self.assertEqual([filename], paths)


if __name__ == "__main__":
    unittest.main()
