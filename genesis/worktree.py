from __future__ import annotations

import shutil
import subprocess
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from genesis.config import CONFIG_DIR


@dataclass(frozen=True)
class WorktreePatch:
    worktree_path: str
    patch_text: str
    changed_files: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.patch_text.strip() or self.changed_files)


class WorktreeManager:
    """Create isolated git worktrees and move approved patches back safely."""

    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path).resolve()
        self.repo_root = self._repo_root()
        repo_key = hashlib.sha256(str(self.repo_root).encode("utf-8")).hexdigest()[:16]
        self.worktrees_root = CONFIG_DIR / "worktrees" / repo_key

    def has_head(self) -> bool:
        """True if the repo has at least one commit (a HEAD to branch from)."""
        return self._git("rev-parse", "--verify", "--quiet", "HEAD",
                          check=False).returncode == 0

    def prepare_main(self, ignore_paths: list[str] | None = None) -> str | None:
        """Create a recoverable base revision for isolated execution.

        Git worktrees require a commit to branch from. Rather than rejecting a
        repository with local edits (or an unborn repository), preserve the
        current project state in a dedicated checkpoint commit. Explicitly
        ignored runtime files are neither staged nor included in that commit.

        Returns the abbreviated checkpoint SHA, or ``None`` when the existing
        HEAD was already a usable base.
        """
        ignored = self._normalize_paths(ignore_paths or [])
        pathspec = [".", *[f":(top,exclude){path}" for path in ignored]]
        had_head = self.has_head()

        # Supplying pathspecs to both add and commit is important: it keeps an
        # already-staged memory/runtime file staged but out of the checkpoint.
        self._git("add", "-A", "--", *pathspec)
        staged = self._git(
            "diff", "--cached", "--quiet", "--", *pathspec, check=False
        ).returncode == 1
        if had_head and not staged:
            return None

        message = (
            "[genesis] checkpoint: preserve worktree before isolated run"
            if had_head
            else "[genesis] checkpoint: initial project state"
        )
        # An entirely empty unborn repository has no path that can match `.`.
        # Omit the pathspec only in that case so --allow-empty can create HEAD.
        commit_pathspec = (
            ["--", *pathspec]
            if self._git("ls-files", "--cached").stdout.strip()
            else []
        )
        self._git(
            "-c", "user.name=Genesis",
            "-c", "user.email=genesis@localhost",
            "-c", "commit.gpgSign=false",
            "commit", "--no-verify", "--allow-empty", "-m", message,
            *commit_pathspec,
        )
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()

    def ensure_clean_main(self, ignore_paths: list[str] | None = None) -> None:
        """Backward-compatible alias that now prepares instead of rejecting."""
        self.prepare_main(ignore_paths=ignore_paths)

    def _normalize_paths(self, paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in paths:
            path = Path(value)
            if path.is_absolute():
                try:
                    path = path.resolve().relative_to(self.repo_root)
                except ValueError:
                    # A configured file outside the repository cannot affect
                    # this worktree and needs no exclusion pathspec.
                    continue
            item = path.as_posix()
            if item.startswith("./"):
                item = item[2:]
            item = item.rstrip("/")
            if item and item != "." and item not in normalized:
                normalized.append(item)
        return normalized

    def create(self, run_id: str, step_id: str) -> Path:
        safe_run = _safe_name(run_id)
        safe_step = _safe_name(step_id)
        path = (self.worktrees_root / safe_run / safe_step).resolve()
        self._assert_under(path, self.worktrees_root.resolve())
        if path.exists():
            self.remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--force", "--detach", str(path), "HEAD")
        return path

    def capture_patch(self, worktree_path: str | Path) -> WorktreePatch:
        path = Path(worktree_path).resolve()
        self._assert_under(path, self.worktrees_root.resolve())
        # Stage only inside the isolated worktree so untracked files appear in
        # the binary patch. The main repository index is not touched. Compare
        # the staged worktree against its shared base with main, rather than
        # only against the worktree's HEAD: autonomous workers may create a
        # takeover branch and commit their work before Genesis captures it.
        self._git_in(path, "add", "-A")
        main_head = self._git("rev-parse", "HEAD").stdout.strip()
        worktree_head = self._git_in(path, "rev-parse", "HEAD").stdout.strip()
        merge_base = self._git_in(
            path,
            "merge-base",
            worktree_head,
            main_head,
        ).stdout.strip()
        if not merge_base:
            raise RuntimeError(
                "worker history no longer shares a base with the main repository"
            )
        patch = self._git_in(
            path, "diff", "--cached", "--binary", merge_base
        ).stdout
        changed = self._git_in(
            path, "diff", "--cached", "--name-only", merge_base
        ).stdout.splitlines()
        return WorktreePatch(
            worktree_path=str(path),
            patch_text=patch,
            changed_files=sorted(p for p in changed if p),
        )

    def apply_check(self, patch_text: str) -> None:
        if not patch_text.strip():
            return
        self._git_stdin(patch_text, "apply", "--check", "--binary", "-")

    def apply_patch(self, patch_text: str) -> None:
        if not patch_text.strip():
            return
        self._git_stdin(patch_text, "apply", "--binary", "-")

    def remove(self, worktree_path: str | Path) -> None:
        path = Path(worktree_path).resolve()
        self._assert_under(path, self.worktrees_root.resolve())
        try:
            self._git("worktree", "remove", "--force", str(path), check=False)
        finally:
            if path.exists():
                shutil.rmtree(path)

    def cleanup_run(self, run_id: str) -> int:
        run_root = (self.worktrees_root / _safe_name(run_id)).resolve()
        self._assert_under(run_root, self.worktrees_root.resolve())
        if not run_root.exists():
            return 0
        count = 0
        for child in sorted(run_root.iterdir()):
            if child.is_dir():
                self.remove(child)
                count += 1
        if run_root.exists():
            shutil.rmtree(run_root)
        return count

    def _repo_root(self) -> Path:
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return Path(result.stdout.strip()).resolve()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def _git_in(self, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    def _git_stdin(self, patch_text: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            input=patch_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )

    @staticmethod
    def _assert_under(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError:
            raise RuntimeError(f"refusing to operate outside worktree root: {path}")


def _safe_name(value: str) -> str:
    keep = [ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in value]
    name = "".join(keep).strip(".-")
    return name or "item"
