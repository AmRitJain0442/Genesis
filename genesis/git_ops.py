from __future__ import annotations
import hashlib
import logging
import os
import subprocess
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
        *,
        patch_sha: str = "",
        run_id: str = "",
    ) -> str | None:
        if not self._available:
            return None
        try:
            if paths:
                # Commit exactly the independently reviewed patch manifest.
                # -f is required for a task-referenced source file that was
                # intentionally ignored before Genesis safely overlaid it.
                stageable_paths = [
                    path
                    for path in paths
                    if os.path.lexists(
                        Path(self.repo.working_tree_dir) / path
                    )
                    or self._path_in_index(path)
                ]
                if stageable_paths:
                    self.repo.git.add("-A", "-f", "--", *stageable_paths)
            else:
                self.repo.git.add("-A", "--", ".", ":(exclude).genesis")
            staged_names = (
                self.repo.git.diff(
                    "--cached", "--name-only", "--", *paths
                )
                if paths
                else self.repo.git.diff("--cached", "--name-only")
            )
            if not staged_names.strip():
                return None
            message = f"{self.config.commit_prefix} {step_id}: {title}"
            if patch_sha:
                message += (
                    "\n\n"
                    f"Genesis-Patch-SHA: {patch_sha}\n"
                    f"Genesis-Run-ID: {run_id}\n"
                    f"Genesis-Step-ID: {step_id}"
                )
            if paths:
                if patch_sha and not self._working_patch_matches(
                    patch_sha,
                    paths,
                ):
                    logger.warning(
                        "Refusing commit for %s: main-tree bytes do not match patch %s",
                        step_id,
                        patch_sha,
                    )
                    return None
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.repo.working_tree_dir),
                        "-c",
                        "commit.gpgSign=false",
                        "commit",
                        "--no-verify",
                        "--only",
                        "-m",
                        message,
                        "--",
                        *paths,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                if result.returncode != 0:
                    logger.warning(
                        "Commit failed for %s: %s",
                        step_id,
                        (result.stderr or result.stdout).strip(),
                    )
                    return None
                sha = self.repo.git.rev_parse("--short", "HEAD").strip()
                if patch_sha and not self._commit_patch_matches(
                    sha,
                    patch_sha,
                    paths,
                ):
                    logger.error(
                        "Created commit %s does not match reviewed patch %s",
                        sha,
                        patch_sha,
                    )
                    return None
            else:
                self.repo.index.commit(message)
                sha = self.repo.head.commit.hexsha[:7]
            logger.info("Committed %s: %s", sha, message)
            return sha
        except Exception as e:
            logger.warning("Commit failed: %s", e, exc_info=True)
            return None

    def find_step_commit(
        self,
        run_id: str,
        step_id: str,
        patch_sha: str,
        *,
        max_count: int = 100,
    ) -> str | None:
        """Find a prior idempotent Genesis integration commit."""
        if not self._available or not patch_sha:
            return None
        expected = {
            "Genesis-Patch-SHA": patch_sha,
            "Genesis-Run-ID": run_id,
            "Genesis-Step-ID": step_id,
        }
        try:
            for commit in self.repo.iter_commits(max_count=max_count):
                trailers: dict[str, str] = {}
                for raw in commit.message.splitlines():
                    key, separator, value = raw.partition(":")
                    if separator and key in expected:
                        trailers[key] = value.strip()
                if (
                    all(trailers.get(key) == value for key, value in expected.items())
                    and self._commit_patch_matches(
                        commit.hexsha,
                        patch_sha,
                        [],
                    )
                    and self._commit_effect_matches_head(commit.hexsha, [])
                ):
                    return commit.hexsha[:7]
        except Exception as exc:
            logger.warning("Could not search Genesis commit identities: %s", exc)
        return None

    def find_commit_by_patch(
        self,
        patch_sha: str,
        paths: list[str],
        *,
        max_count: int = 100,
    ) -> str | None:
        """Compatibility recovery for a checkpoint containing the exact patch."""
        if not self._available or not patch_sha or not paths:
            return None
        try:
            for commit in self.repo.iter_commits(max_count=max_count):
                if self._commit_patch_matches(
                    commit.hexsha,
                    patch_sha,
                    paths,
                ) and self._commit_effect_matches_head(
                    commit.hexsha,
                    paths,
                ):
                    return commit.hexsha[:7]
        except Exception as exc:
            logger.warning("Could not reconcile commit patch identity: %s", exc)
        return None

    def paths_match_head(self, paths: list[str]) -> bool:
        """Return true when reviewed paths have no staged/unstaged HEAD delta."""
        if not self._available or not paths:
            return False
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo.working_tree_dir),
                "diff",
                "--quiet",
                "--no-ext-diff",
                "HEAD",
                "--",
                *paths,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode not in {0, 1}:
            logger.warning(
                "Could not compare reviewed paths with HEAD: %s",
                (result.stderr or result.stdout).strip(),
            )
        if result.returncode != 0:
            return False
        root = Path(self.repo.working_tree_dir)
        return not any(
            os.path.lexists(root / path) and not self._path_in_index(path)
            for path in paths
        )

    def _working_patch_matches(self, patch_sha: str, paths: list[str]) -> bool:
        diff_text = self._git_output("diff", "HEAD", "--binary", "--", *paths)
        manifest = self._status_paths(self._git_output(
            "diff", "HEAD", "--name-status", "-z", "--", *paths
        ))
        return self._patch_identity_matches(
            diff_text,
            manifest,
            patch_sha,
            paths,
        )

    def _commit_patch_matches(
        self,
        commit_sha: str,
        patch_sha: str,
        paths: list[str],
    ) -> bool:
        try:
            parent = self._git_output("rev-parse", f"{commit_sha}^").strip()
        except subprocess.CalledProcessError:
            return False
        manifest = self._status_paths(self._git_output(
            "diff",
            parent,
            commit_sha,
            "--name-status",
            "-z",
        ))
        effective_paths = list(paths or manifest)
        if not effective_paths:
            return False
        diff_text = self._git_output(
            "diff",
            parent,
            commit_sha,
            "--binary",
            "--",
            *effective_paths,
        )
        return self._patch_identity_matches(
            diff_text,
            manifest,
            patch_sha,
            effective_paths,
        )

    def _commit_effect_matches_head(
        self,
        commit_sha: str,
        paths: list[str],
    ) -> bool:
        """Return true only while a historical patch's result remains in HEAD.

        A valid Genesis commit can later be reverted. Recovery must not treat
        that ancestor as the current integration merely because an identical
        patch happens to exist in the dirty working tree.
        """
        effective_paths = list(paths)
        if not effective_paths:
            try:
                parent = self._git_output("rev-parse", f"{commit_sha}^").strip()
                effective_paths = self._status_paths(self._git_output(
                    "diff",
                    parent,
                    commit_sha,
                    "--name-status",
                    "-z",
                ))
            except subprocess.CalledProcessError:
                return False
        if not effective_paths:
            return False
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo.working_tree_dir),
                "diff",
                "--quiet",
                "--no-ext-diff",
                commit_sha,
                "HEAD",
                "--",
                *effective_paths,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode not in {0, 1}:
            logger.warning(
                "Could not compare commit %s with HEAD: %s",
                commit_sha,
                (result.stderr or result.stdout).strip(),
            )
        return result.returncode == 0

    @staticmethod
    def _patch_identity_matches(
        diff_text: str,
        manifest: list[str],
        patch_sha: str,
        paths: list[str],
    ) -> bool:
        normalized_manifest = sorted(
            _normalize_git_path(item) for item in manifest if item
        )
        normalized_paths = sorted(
            _normalize_git_path(item) for item in paths if item
        )
        digest = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()[:16]
        return (
            bool(normalized_manifest)
            and normalized_manifest == normalized_paths
            and digest == patch_sha
        )

    @staticmethod
    def _status_paths(raw: str) -> list[str]:
        if "\0" in raw:
            fields = [item for item in raw.split("\0") if item]
            paths: list[str] = []
            index = 0
            while index < len(fields):
                status = fields[index]
                index += 1
                path_count = 2 if status.startswith(("R", "C")) else 1
                if index + path_count > len(fields):
                    raise RuntimeError(
                        "malformed NUL-delimited Git name-status output"
                    )
                paths.extend(fields[index:index + path_count])
                index += path_count
            return paths

        # Compatibility for callers that provide traditional line output.
        paths: list[str] = []
        for line in raw.splitlines():
            columns = line.split("\t")
            if not columns:
                continue
            if columns[0].startswith(("R", "C")) and len(columns) >= 3:
                paths.extend(columns[1:3])
            elif len(columns) >= 2:
                paths.append(columns[-1])
        return paths

    def _git_output(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo.working_tree_dir), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        return result.stdout

    def _path_in_index(self, path: str) -> bool:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo.working_tree_dir),
                "ls-files",
                "--error-unmatch",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode == 0

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


def _normalize_git_path(value: str) -> str:
    return value.replace("\\", "/") if os.name == "nt" else value
