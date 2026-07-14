from __future__ import annotations
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.config import GitConfig

logger = logging.getLogger(__name__)


class GitManager:
    def __init__(self, repo_path: str, config: GitConfig):
        self.config = config
        self._available = False
        self.repo = None

        try:
            import git
            self.repo = git.Repo(repo_path, search_parent_directories=True)
            self._available = True
        except Exception as e:
            logger.warning("Git not available: %s", e)

    def has_changes(self) -> bool:
        if not self._available:
            return False
        try:
            return self.repo.is_dirty(untracked_files=True)
        except Exception:
            return False

    def changed_files(self) -> list[str]:
        """Return tracked and untracked files currently changed in the repo."""
        if not self._available:
            return []
        try:
            changed = set(self.repo.git.diff("--name-only").splitlines())
            changed.update(self.repo.git.diff("--name-only", "--cached").splitlines())
            changed.update(self.repo.untracked_files)
            return sorted(p for p in changed if p)
        except Exception as e:
            logger.warning("Could not read changed files: %s", e)
            return []

    def diff_text(self, paths: list[str] | None = None, max_chars: int = 20000) -> str:
        """Return a bounded git diff for review/memory context."""
        if not self._available:
            return ""
        try:
            args = ["--"]
            if paths:
                args.extend(paths)
            diff = self.repo.git.diff(*args)
            if paths:
                # Include new untracked files as small synthetic sections because
                # plain git diff omits them until they are staged.
                for path in paths:
                    p = Path(self.repo.working_tree_dir or ".") / path
                    if p.exists() and path in self.repo.untracked_files:
                        try:
                            content = p.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            content = "<unreadable>"
                        diff += f"\n--- untracked: {path} ---\n{content}\n"
            if len(diff) > max_chars:
                return diff[:max_chars].rstrip() + "\n... (diff truncated)"
            return diff
        except Exception as e:
            logger.warning("Could not read diff: %s", e)
            return ""

    def commit_step(
        self,
        step_id: str,
        title: str,
        paths: list[str] | None = None,
    ) -> str | None:
        if not self._available:
            return None
        try:
            if paths:
                # Commit exactly the independently reviewed patch manifest.
                # -f is required for a task-referenced source file that was
                # intentionally ignored before Genesis safely overlaid it.
                self.repo.git.add("-A", "-f", "--", *paths)
            else:
                self.repo.git.add("-A", "--", ".", ":(exclude).genesis")
            if not self.repo.git.diff("--cached", "--name-only").strip():
                return None
            message = f"{self.config.commit_prefix} {step_id}: {title}"
            self.repo.index.commit(message)
            sha = self.repo.head.commit.hexsha[:7]
            logger.info("Committed %s: %s", sha, message)
            return sha
        except Exception as e:
            logger.warning("Commit failed: %s", e, exc_info=True)
            return None

    def push(self) -> bool:
        if not self._available or self.repo is None:
            return False
        try:
            if self.config.remote not in [r.name for r in self.repo.remotes]:
                logger.warning("Remote '%s' not found — skipping push", self.config.remote)
                return False
            remote = self.repo.remote(self.config.remote)
            remote.push(self.config.branch)
            return True
        except Exception as e:
            logger.warning("Push failed: %s", e)
            return False

    def get_log(self, n: int = 5) -> list[str]:
        if not self._available:
            return []
        try:
            commits = list(self.repo.iter_commits(max_count=n))
            return [f"{c.hexsha[:7]}  {c.message.strip()[:72]}" for c in commits]
        except Exception:
            return []

    def close(self) -> None:
        if self.repo is not None and hasattr(self.repo, "close"):
            try:
                self.repo.close()
            except Exception:
                pass
