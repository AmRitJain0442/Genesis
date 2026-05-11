"""Dependency and file-scope scheduling for isolated worker execution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from genesis.schemas.plan import Step


WILDCARD_SCOPE = "*"

_PATH_RE = re.compile(
    r"(?P<path>`[^`]+`|[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_./\\-]+|[A-Za-z0-9_.-]+[/\\][A-Za-z0-9_./\\-]+)"
)
_TRAILING_PUNCTUATION = ".,;:)]}'\""
_LEADING_PUNCTUATION = "([{'\""
_BROAD_WORDS = {
    "architecture",
    "config",
    "configuration",
    "dependencies",
    "dependency",
    "project",
    "repo",
    "repository",
    "runtime",
    "schema",
}
_BROAD_PATHS = {
    ".env",
    ".gitignore",
    "dockerfile",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "uv.lock",
    "yarn.lock",
}


@dataclass(frozen=True)
class StepScope:
    """A conservative approximation of the files a step may mutate."""

    step_id: str
    paths: tuple[str, ...]
    source: str = "inferred"

    @property
    def is_wildcard(self) -> bool:
        return WILDCARD_SCOPE in self.paths


@dataclass(frozen=True)
class ScheduledStep:
    step: Step
    scope: StepScope


class DependencyScheduler:
    """Pick ready steps whose dependency and file scopes do not overlap."""

    def __init__(self, steps: Iterable[Step]):
        self.steps = list(steps)
        self.scopes = {step.step_id: effective_step_scope(step) for step in self.steps}

    def scope_for(self, step_id: str) -> StepScope:
        return self.scopes[step_id]

    def select_ready(
        self,
        *,
        committed_ids: set[str],
        unavailable_ids: set[str],
        active_scopes: Iterable[StepScope],
        limit: int,
    ) -> list[ScheduledStep]:
        """Return a batch of ready, non-overlapping steps.

        ``unavailable_ids`` contains blocked, running, or otherwise held steps.
        The returned batch is also internally non-overlapping, so callers can
        start every returned step concurrently.
        """

        if limit <= 0:
            return []

        selected: list[ScheduledStep] = []
        selected_scopes: list[StepScope] = list(active_scopes)

        for step in self.steps:
            if len(selected) >= limit:
                break
            if step.step_id in committed_ids or step.step_id in unavailable_ids:
                continue
            if any(dep not in committed_ids for dep in step.depends_on):
                continue

            scope = self.scopes[step.step_id]
            if any(scopes_overlap(scope, existing) for existing in selected_scopes):
                continue

            selected.append(ScheduledStep(step=step, scope=scope))
            selected_scopes.append(scope)

        return selected


def infer_step_scope(step: Step) -> StepScope:
    """Infer a step's write scope from plan text.

    The planner is not yet required to emit machine-readable file ownership, so
    this parser deliberately prefers safety over parallelism. If it cannot find
    concrete paths, or if the text points at broad repo-level work, it returns a
    wildcard scope that serializes the step against all other active work.
    """

    text = " ".join(
        part
        for part in (step.title, step.description, step.context_hint, step.expected_output)
        if part
    )
    normalized_text = text.lower()

    if any(word in normalized_text.split() for word in _BROAD_WORDS):
        return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="inferred")

    paths: set[str] = set()
    for match in _PATH_RE.finditer(text):
        candidate = _normalize_path_candidate(match.group("path"))
        if not candidate:
            continue
        if candidate.lower() in _BROAD_PATHS:
            return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="inferred")
        paths.add(candidate)

    if not paths:
        return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="inferred")

    return StepScope(step_id=step.step_id, paths=tuple(sorted(paths)), source="inferred")


def declared_step_scope(step: Step) -> StepScope | None:
    """Return planner-declared file ownership, if the plan supplied it."""

    raw_scope = getattr(step, "file_scope", None) or []
    if not raw_scope:
        return None

    paths: set[str] = set()
    for raw in raw_scope:
        candidate = str(raw).strip()
        if not candidate:
            continue
        if candidate == WILDCARD_SCOPE:
            return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="declared")
        normalized = _normalize_path_candidate(candidate)
        if not normalized:
            return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="declared")
        if normalized.lower() in _BROAD_PATHS:
            return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="declared")
        paths.add(normalized)

    if not paths:
        return StepScope(step_id=step.step_id, paths=(WILDCARD_SCOPE,), source="declared")

    return StepScope(step_id=step.step_id, paths=tuple(sorted(paths)), source="declared")


def effective_step_scope(step: Step) -> StepScope:
    """Use explicit plan ownership first, then conservative text inference."""

    return declared_step_scope(step) or infer_step_scope(step)


def scopes_overlap(left: StepScope, right: StepScope) -> bool:
    if left.is_wildcard or right.is_wildcard:
        return True

    for left_path in left.paths:
        for right_path in right.paths:
            if _paths_overlap(left_path, right_path):
                return True
    return False


def _paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    left_prefix = f"{left.rstrip('/')}/"
    right_prefix = f"{right.rstrip('/')}/"
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def _normalize_path_candidate(candidate: str) -> str | None:
    value = candidate.strip().strip("`").strip()
    value = value.strip(_LEADING_PUNCTUATION).strip(_TRAILING_PUNCTUATION)
    value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value)
    if not value or value in {".", "/"}:
        return None
    if value.startswith("./"):
        value = value[2:]
    if value.startswith("/") or ".." in PurePosixPath(value).parts:
        return None
    return value.lower()
