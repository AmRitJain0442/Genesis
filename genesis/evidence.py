from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.schemas.plan import Step
    from genesis.worktree import WorktreePatch


_ARTIFACT_PARTS = {
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
}
_ARTIFACT_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp"}
_ARTIFACT_NAMES = {".coverage", "coverage.xml"}


@dataclass(frozen=True)
class EvidenceGuardResult:
    violations: list[str] = field(default_factory=list)
    artifact_files: list[str] = field(default_factory=list)
    out_of_scope_deletions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def evaluate_patch_evidence(step: "Step", patch: "WorktreePatch") -> EvidenceGuardResult:
    """Apply cheap deterministic guards before spending a model review call."""
    artifact_files = sorted(
        path for path in patch.changed_files if _is_transient_artifact(path)
    )
    deleted_files = _deleted_paths(patch.diff_status_lines)
    scopes = list(getattr(step, "file_scope", None) or [])
    out_of_scope_deletions = sorted(
        path for path in deleted_files if scopes and not _matches_scope(path, scopes)
    )

    violations: list[str] = []
    if artifact_files:
        violations.append(
            "Remove transient/generated artifacts from the patch: "
            + ", ".join(artifact_files)
        )
    if out_of_scope_deletions:
        violations.append(
            "Restore tracked files deleted outside the declared step scope: "
            + ", ".join(out_of_scope_deletions)
        )
    return EvidenceGuardResult(
        violations=violations,
        artifact_files=artifact_files,
        out_of_scope_deletions=out_of_scope_deletions,
    )


def _deleted_paths(status_lines: list[str]) -> list[str]:
    deleted: list[str] = []
    for line in status_lines:
        columns = line.split("\t")
        if columns and columns[0].startswith("D") and len(columns) >= 2:
            deleted.append(columns[-1].replace("\\", "/"))
    return deleted


def _matches_scope(path: str, scopes: list[str]) -> bool:
    normalized = _normalize(path)
    for raw_scope in scopes:
        scope = _normalize(str(raw_scope))
        if not scope:
            continue
        if fnmatch.fnmatch(normalized, scope):
            return True
        prefix = scope.rstrip("/") + "/"
        if normalized == scope.rstrip("/") or normalized.startswith(prefix):
            return True
    return False


def _is_transient_artifact(path: str) -> bool:
    normalized = _normalize(path)
    pure = PurePosixPath(normalized)
    if any(part in _ARTIFACT_PARTS for part in pure.parts):
        return True
    if pure.name in _ARTIFACT_NAMES:
        return True
    return pure.suffix.lower() in _ARTIFACT_SUFFIXES


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
