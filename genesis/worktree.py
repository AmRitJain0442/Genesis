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

    def ensure_clean_main(self, ignore_paths: list[str] | None = None) -> None:
        # A brand-new `git init` (no commit yet) has no HEAD to create worktrees
        # from. That's a different problem from a dirty tree, so report it plainly
        # instead of the misleading "commit or stash current changes".
        if not self.has_head():
            raise RuntimeError(
                "This repository has no commits yet, so Genesis has no base "
                "revision to build from. Make an initial commit first:\n"
                '    git add -A && git commit -m "initial commit"'
            )

        ignored = [p.replace("\\", "/").lstrip("./") for p in (ignore_paths or [])]
        dirty = []
        for line in self._git("status", "--porcelain").stdout.splitlines():
            path = line[3:].replace("\\", "/").lstrip("./")
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if any(path == item or path.startswith(item.rstrip("/") + "/") for item in ignored):
                continue
            dirty.append(line)
        if dirty:
            preview = "\n".join("    " + line for line in dirty[:10])
            more = f"\n    ... and {len(dirty) - 10} more" if len(dirty) > 10 else ""
            only_untracked = all(line.startswith("??") for line in dirty)
            hint = (
                "These are untracked files. Commit them so Genesis uses them as "
                "the base (git add -A && git commit), or add them to .gitignore."
                if only_untracked else
                "Commit or stash these changes before running "
                "(e.g. git add -A && git commit)."
            )
            raise RuntimeError(
                "Genesis isolated execution requires a clean git worktree, but "
                f"found {len(dirty)} uncommitted change(s):\n{preview}{more}\n{hint}"
            )

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
        # the binary patch. The main repository index is not touched.
        self._git_in(path, "add", "-A")
        patch = self._git_in(path, "diff", "--cached", "--binary").stdout
        changed = self._git_in(path, "diff", "--cached", "--name-only").stdout.splitlines()
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
