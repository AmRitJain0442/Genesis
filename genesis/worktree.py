from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from genesis.config import CONFIG_DIR


_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
    ".scss", ".md", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".sh",
    ".ps1", ".bat", ".sql", ".rs", ".go", ".java", ".kt", ".swift",
}
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.@+\\/-]+\.[A-Za-z0-9]{1,12}")


@dataclass(frozen=True)
class WorktreePatch:
    worktree_path: str
    patch_text: str
    changed_files: list[str] = field(default_factory=list)
    base_sha: str = ""
    head_sha: str = ""
    patch_sha: str = ""
    status_lines: list[str] = field(default_factory=list)
    diff_status_lines: list[str] = field(default_factory=list)

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
        for sensitive in self._sensitive_workspace_paths():
            if sensitive not in ignored:
                ignored.append(sensitive)
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

    def workspace_snapshot(self) -> dict:
        """Return bounded, non-secret repository facts for planning and workers."""
        status = self._git(
            "status",
            "--short",
            "--untracked-files=all",
        ).stdout.splitlines()
        untracked = [line[3:] for line in status if line.startswith("?? ")]
        dirty = [line for line in status if not line.startswith("?? ")]
        ignored_source = [
            path
            for path in self._ignored_files()
            if (self.repo_root / path).suffix.lower() in _SOURCE_SUFFIXES
            and not self._sensitive_path(path)
        ]
        tracked_count = len(
            self._git("ls-files").stdout.splitlines()
        )
        return {
            "head": self._git("rev-parse", "--short", "HEAD", check=False).stdout.strip(),
            "tracked_count": tracked_count,
            "dirty": dirty[:200],
            "untracked": untracked[:200],
            "ignored_source": sorted(ignored_source)[:200],
            "sensitive_paths": self._sensitive_workspace_paths()[:200],
        }

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

    def materialize_referenced_ignored(self, worktree_path: str | Path, step) -> list[str]:
        """Copy task-referenced ignored source into the isolated worktree safely."""
        path = Path(worktree_path).resolve()
        self._assert_under(path, self.worktrees_root.resolve())
        ignored = set(self._ignored_files())
        referenced = self._referenced_paths(step)
        selected: list[str] = []
        for candidate in sorted(ignored):
            source = self.repo_root / candidate
            if not source.is_file() or source.stat().st_size > 1_000_000:
                continue
            if source.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            if self._sensitive_path(candidate):
                continue
            if candidate not in referenced and source.name not in referenced:
                continue
            destination = path / candidate
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            selected.append(candidate)

        if selected:
            records = [
                {
                    "path": relative,
                    "sha256": self._file_sha(self.repo_root / relative),
                }
                for relative in selected
            ]
            self._overlay_metadata_path(path).write_text(
                json.dumps(records, indent=2),
                encoding="utf-8",
            )
        return selected

    def capture_patch(self, worktree_path: str | Path) -> WorktreePatch:
        path = Path(worktree_path).resolve()
        self._assert_under(path, self.worktrees_root.resolve())
        # Stage only inside the isolated worktree so untracked files appear in
        # the binary patch. The main repository index is not touched. Compare
        # the staged worktree against its shared base with main, rather than
        # only against the worktree's HEAD: autonomous workers may create a
        # takeover branch and commit their work before Genesis captures it.
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
        overlays = self._overlay_records(path)
        overlay_paths = [str(record["path"]) for record in overlays]
        self._git_in(path, "add", "-A", "--", ".")
        # A worker may force-add or commit an overlaid ignored source. Reset its
        # index entry to the base; a safe modification patch is appended below.
        for relative in overlay_paths:
            self._git_in(
                path,
                "reset",
                merge_base,
                "--",
                relative,
                check=False,
            )
        patch = self._git_in(
            path, "diff", "--cached", "--binary", merge_base
        ).stdout
        changed = self._git_in(
            path, "diff", "--cached", "--name-only", merge_base
        ).stdout.splitlines()
        status_lines = self._git_in(
            path, "status", "--short", "--untracked-files=all"
        ).stdout.splitlines()
        diff_status_lines = self._git_in(
            path, "diff", "--cached", "--name-status", merge_base
        ).stdout.splitlines()
        overlay_patch, overlay_changed, overlay_status = self._overlay_patch(
            path,
            overlays,
        )
        if overlay_patch:
            patch = patch.rstrip() + "\n" + overlay_patch
        changed = sorted(set(changed) | set(overlay_changed))
        diff_status_lines.extend(overlay_status)
        return WorktreePatch(
            worktree_path=str(path),
            patch_text=patch,
            changed_files=sorted(p for p in changed if p),
            base_sha=merge_base,
            head_sha=worktree_head,
            patch_sha=hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16],
            status_lines=status_lines,
            diff_status_lines=diff_status_lines,
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
            self._overlay_metadata_path(path).unlink(missing_ok=True)

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

    def _git_in(
        self,
        path: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
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

    def _ignored_files(self) -> list[str]:
        output = self._git(
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
        ).stdout
        return [item for item in output.split("\0") if item]

    def _sensitive_workspace_paths(self) -> list[str]:
        output = self._git(
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ).stdout
        return sorted(
            item for item in output.split("\0")
            if item and self._sensitive_path(item)
        )

    @staticmethod
    def _referenced_paths(step) -> set[str]:
        values = list(getattr(step, "file_scope", None) or [])
        values.extend([
            str(getattr(step, "title", "") or ""),
            str(getattr(step, "description", "") or ""),
            str(getattr(step, "context_hint", "") or ""),
            str(getattr(step, "expected_output", "") or ""),
        ])
        referenced: set[str] = set()
        for value in values:
            normalized = str(value).replace("\\", "/")
            for match in _PATH_TOKEN.finditer(normalized):
                item = match.group(0)
                while item.startswith("./"):
                    item = item[2:]
                referenced.add(item)
            if "." in normalized and " " not in normalized:
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                referenced.add(normalized)
        referenced.update(Path(item).name for item in list(referenced))
        return referenced

    @staticmethod
    def _sensitive_path(path: str) -> bool:
        name = Path(path.replace("\\", "/").lower()).name
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
        if Path(name).suffix in {".pem", ".key", ".p12", ".pfx"}:
            return True
        return bool(re.search(
            r"(?:service[-_]?account|credentials|sa[-_]?key).*\.json$",
            name,
        ))

    def _overlay_metadata_path(self, worktree_path: Path) -> Path:
        return worktree_path.parent / f".{worktree_path.name}.overlay.json"

    def _overlay_records(self, worktree_path: Path) -> list[dict]:
        metadata = self._overlay_metadata_path(worktree_path)
        if not metadata.exists():
            return []
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in value if isinstance(item, dict) and item.get("path")]

    def _overlay_patch(
        self,
        worktree_path: Path,
        records: list[dict],
    ) -> tuple[str, list[str], list[str]]:
        sections: list[str] = []
        changed: list[str] = []
        statuses: list[str] = []
        for record in records:
            relative = str(record["path"]).replace("\\", "/")
            original = self.repo_root / relative
            worker_file = worktree_path / relative
            if not original.exists():
                raise RuntimeError(f"overlaid source disappeared from main: {relative}")
            if self._file_sha(original) != record.get("sha256"):
                raise RuntimeError(
                    f"overlaid source changed in main during worker execution: {relative}"
                )
            original_text = original.read_text(
                encoding="utf-8",
                errors="surrogateescape",
            )
            worker_text = (
                worker_file.read_text(encoding="utf-8", errors="surrogateescape")
                if worker_file.exists()
                else ""
            )
            if original_text == worker_text:
                continue
            diff = "".join(difflib.unified_diff(
                original_text.splitlines(keepends=True),
                worker_text.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            ))
            sections.append(f"diff --git a/{relative} b/{relative}\n{diff}")
            changed.append(relative)
            statuses.append(("M" if worker_file.exists() else "D") + f"\t{relative}")
        return "\n".join(sections), changed, statuses

    @staticmethod
    def _file_sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_name(value: str) -> str:
    keep = [ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in value]
    name = "".join(keep).strip(".-")
    return name or "item"
