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

    def commit_step(self, step_id: str, title: str) -> str | None:
        if not self._available or not self.has_changes():
            return None
        try:
            self.repo.git.add("-A")
            message = f"{self.config.commit_prefix} {step_id}: {title}"
            self.repo.index.commit(message)
            sha = self.repo.head.commit.hexsha[:7]
            logger.info("Committed %s: %s", sha, message)
            return sha
        except Exception as e:
            logger.warning("Commit failed: %s", e)
            return None

    def push(self) -> bool:
        if not self._available:
            return False
        try:
            remote = self.repo.remotes[self.config.remote]
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
