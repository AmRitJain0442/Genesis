from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import threading
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
_SCANNER_CACHE: dict[tuple[str, ...], "AcceptanceCheck"] = {}
_SCANNER_CACHE_LOCK = threading.Lock()
_SCANNER_KEY_LOCKS: dict[tuple[str, ...], threading.Lock] = {}


@dataclass(frozen=True)
class EvidenceGuardResult:
    violations: list[str] = field(default_factory=list)
    artifact_files: list[str] = field(default_factory=list)
    out_of_scope_deletions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class AcceptanceGateReport:
    checks: list[AcceptanceCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def violations(self) -> list[str]:
        return [check.detail for check in self.checks if not check.passed]

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [
                {"name": item.name, "passed": item.passed, "detail": item.detail}
                for item in self.checks
            ],
            "violations": self.violations,
        }


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


def evaluate_acceptance_gates(
    step: "Step",
    patch: "WorktreePatch",
    work_dir: str | Path,
    *,
    run_external_scanners: bool = True,
) -> AcceptanceGateReport:
    """Evaluate objective acceptance clauses against the actual worktree.

    These gates are deliberately narrow. They activate only when the step asks
    for the corresponding artifact or security property, and they never infer
    success from a worker-authored narrative.
    """
    root = Path(work_dir).resolve()
    task = _task_text(step)
    lowered = task.lower()
    checks: list[AcceptanceCheck] = []

    basic = evaluate_patch_evidence(step, patch)
    checks.append(AcceptanceCheck(
        "patch-scope",
        basic.passed,
        "Patch scope and artifact guard passed."
        if basic.passed else " ".join(basic.violations),
    ))

    for artifact in _required_artifacts(lowered):
        exists = (root / artifact).is_file()
        checks.append(AcceptanceCheck(
            f"required-artifact:{artifact}",
            exists,
            f"Required artifact {artifact} exists."
            if exists else f"Create the required artifact {artifact}; it is missing from the worktree.",
        ))

    if "pin" in lowered and ("requirements" in lowered or "dependenc" in lowered):
        requirement_files = [
            root / name for name in ("requirements.txt", "requirements-dev.txt")
            if (root / name).is_file()
        ]
        unpinned = _unpinned_requirements(requirement_files)
        passed = bool(requirement_files) and not unpinned
        detail = (
            "Python dependency requirements are exactly pinned."
            if passed
            else "Pin every runtime requirement with ==; unpinned entries: "
            + (", ".join(unpinned) if unpinned else "requirements.txt is missing or empty")
        )
        checks.append(AcceptanceCheck("pinned-dependencies", passed, detail))

    security_task = any(
        token in lowered
        for token in ("secret", "credential", "service account", "service-account", "api key", "x-api-key")
    )
    if security_task and any(token in lowered for token in ("untrack", "git ls-files", "tracked key", "secret-free")):
        tracked = _git_lines(root, "ls-files")
        sensitive = [path for path in tracked if _sensitive_path(path)]
        checks.append(AcceptanceCheck(
            "tracked-credentials",
            not sensitive,
            "Git index contains no credential-shaped files."
            if not sensitive else "Remove credential files from the Git index: " + ", ".join(sensitive),
        ))

    env_names = sorted(set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", task)))
    env_names = [name for name in env_names if "_" in name]
    if env_names and any(token in lowered for token in ("environment", "env var", "from env", ".env")):
        corpus = _changed_text(root, patch.changed_files)
        example = root / ".env.example"
        if example.is_file():
            corpus += "\n" + example.read_text(encoding="utf-8", errors="replace")
        missing = [name for name in env_names if name not in corpus]
        checks.append(AcceptanceCheck(
            "environment-contract",
            not missing,
            "Requested environment variables are represented in the implementation/configuration."
            if not missing else "Represent these requested environment variables in code or .env.example: " + ", ".join(missing),
        ))

    if env_names and any(token in lowered for token in ("no fallback", "no in-code fallback", "required", "fail fast")):
        fallbacks = _literal_env_fallbacks(root, patch.changed_files, env_names)
        checks.append(AcceptanceCheck(
            "no-secret-fallback",
            not fallbacks,
            "No literal fallback is used for required environment configuration."
            if not fallbacks else "Remove literal fallback values for required environment variables: " + ", ".join(fallbacks),
        ))

    if security_task:
        hardcoded = _hardcoded_secret_lines(
            root,
            patch.changed_files,
            include_endpoints=(
                "hardcoded" in lowered
                and any(token in lowered for token in ("endpoint", "tunnel", "url"))
            ),
        )
        checks.append(AcceptanceCheck(
            "hardcoded-secrets",
            not hardcoded,
            "No likely hardcoded secrets were found in changed source files."
            if not hardcoded else "Remove likely hardcoded secrets from changed source: " + ", ".join(hardcoded),
        ))

    requested_scanners = [
        name for name in ("gitleaks", "trufflehog") if name in lowered
    ]
    if run_external_scanners and requested_scanners:
        snapshot_id = f"{patch.base_sha}:{patch.patch_sha}"
        with ThreadPoolExecutor(max_workers=min(2, len(requested_scanners))) as pool:
            futures = [
                pool.submit(_run_secret_scanner, scanner, root, snapshot_id)
                for scanner in requested_scanners
            ]
            checks.extend(future.result() for future in futures)
    else:
        for scanner in requested_scanners:
            checks.append(AcceptanceCheck(
                f"secret-scan:{scanner}:deferred",
                True,
                f"{scanner} scan deferred until the final patch gate.",
            ))

    return AcceptanceGateReport(checks=checks)


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


def _task_text(step: "Step") -> str:
    values = [
        getattr(step, "title", ""),
        getattr(step, "description", ""),
        getattr(step, "expected_output", ""),
        getattr(step, "context_hint", ""),
        " ".join(getattr(step, "file_scope", None) or []),
    ]
    return "\n".join(str(value or "") for value in values)


def _required_artifacts(lowered_task: str) -> list[str]:
    candidates = (".env.example", "requirements.txt", "requirements-dev.txt")
    verbs = ("add", "create", "real", "tracked", "provide", "include", "exist")
    required: list[str] = []
    for artifact in candidates:
        position = lowered_task.find(artifact)
        if position < 0:
            continue
        context = lowered_task[max(0, position - 90): position + len(artifact) + 40]
        if any(verb in context for verb in verbs):
            required.append(artifact)
    return required


def _unpinned_requirements(paths: list[Path]) -> list[str]:
    unpinned: list[str] = []
    found = False
    for path in paths:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "--")):
                continue
            found = True
            if "==" not in line or line.startswith(("-e ", "git+", "http://", "https://")):
                unpinned.append(f"{path.name}:{line}")
    if paths and not found:
        unpinned.append("requirements files contain no dependencies")
    return unpinned


def _git_lines(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _sensitive_path(path: str) -> bool:
    name = PurePosixPath(_normalize(path).lower()).name
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if PurePosixPath(name).suffix in {".pem", ".key", ".p12", ".pfx"}:
        return True
    return bool(re.search(
        r"(?:service[-_]?account|credentials|sa[-_]?key).*\.json$",
        name,
    ))


def _changed_text(root: Path, changed_files: list[str]) -> str:
    chunks: list[str] = []
    for relative in changed_files:
        path = root / relative
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def _source_files(root: Path, changed_files: list[str]):
    suffixes = {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".go", ".rs", ".java"}
    for relative in changed_files:
        normalized = _normalize(relative)
        path = root / normalized
        if (
            path.is_file()
            and path.suffix.lower() in suffixes
            and "tests" not in PurePosixPath(normalized).parts
            and path.stat().st_size <= 1_000_000
        ):
            yield normalized, path


def _literal_env_fallbacks(
    root: Path,
    changed_files: list[str],
    env_names: list[str],
) -> list[str]:
    findings: list[str] = []
    names = "|".join(re.escape(name) for name in env_names)
    patterns = [
        re.compile(rf"(?:getenv|environ\.get)\(\s*['\"](?:{names})['\"]\s*,\s*['\"][^'\"]+['\"]"),
        re.compile(rf"process\.env\.(?:{names})\s*(?:\|\||\?\?)\s*['\"][^'\"]+['\"]"),
    ]
    for relative, path in _source_files(root, changed_files):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if any(pattern.search(line) for pattern in patterns):
                findings.append(f"{relative}:{number}")
    return findings


def _hardcoded_secret_lines(
    root: Path,
    changed_files: list[str],
    *,
    include_endpoints: bool = False,
) -> list[str]:
    findings: list[str] = []
    assignment = re.compile(
        r"(?i)(?:api[_-]?key|key|secret|token|password|x-api-key)\s*['\"]?\s*[:=]\s*['\"]([^'\"]{16,})['\"]"
    )
    endpoint = re.compile(
        r"(?i)(?:endpoint|base[_-]?url|api[_-]?url|tunnel[_-]?url)\s*[:=]\s*['\"]https?://[^'\"]+['\"]"
    )
    safe_markers = ("example", "placeholder", "your_", "your-", "changeme", "${", "<")
    for relative, path in _source_files(root, changed_files):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            match = assignment.search(line)
            endpoint_match = include_endpoints and endpoint.search(line)
            if (
                (match and not any(marker in match.group(1).lower() for marker in safe_markers))
                or endpoint_match
            ):
                findings.append(f"{relative}:{number}")
    return findings


def _run_secret_scanner(
    scanner: str,
    root: Path,
    patch_sha: str = "",
) -> AcceptanceCheck:
    executable = _find_scanner(scanner)
    if not executable:
        return AcceptanceCheck(
            f"secret-scan:{scanner}",
            False,
            f"{scanner} was explicitly required but is unavailable; do not report the scan as clean.",
        )
    try:
        command = _scanner_command(scanner, executable)
        binary = Path(executable).resolve()
        stat = binary.stat()
    except (OSError, subprocess.SubprocessError) as exc:
        return AcceptanceCheck(
            f"secret-scan:{scanner}",
            False,
            f"{scanner} could not be inspected: {exc}",
        )
    cache_key = (
        "scanner-cache-v2",
        scanner,
        str(root.resolve()),
        patch_sha or "unversioned",
        str(binary),
        _file_digest(binary),
        "\0".join(command),
        _scanner_config_digest(scanner, root),
    )
    with _SCANNER_CACHE_LOCK:
        cached = _SCANNER_CACHE.get(cache_key)
        key_lock = _SCANNER_KEY_LOCKS.setdefault(cache_key, threading.Lock())
    if cached is not None:
        return cached

    with key_lock:
        with _SCANNER_CACHE_LOCK:
            cached = _SCANNER_CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AcceptanceCheck(
                f"secret-scan:{scanner}",
                False,
                f"{scanner} could not complete: {exc}",
            )
        output = (result.stdout + "\n" + result.stderr).strip().replace("\n", " ")[-600:]
        trufflehog_findings = (
            scanner == "trufflehog"
            and "--fail" not in command
            and bool(result.stdout.strip())
        )
        passed = result.returncode == 0 and not trufflehog_findings
        check = AcceptanceCheck(
            f"secret-scan:{scanner}",
            passed,
            f"{scanner} working-tree scan completed cleanly ({Path(executable).name})."
            if passed
            else (
                "trufflehog detected one or more findings in the working tree."
                if trufflehog_findings
                else f"{scanner} scan failed with exit {result.returncode}: {output}"
            ),
        )
        tool_error = any(marker in output.lower() for marker in (
            "unknown command",
            "unknown flag",
            "flag provided but not defined",
            "not recognized as the name",
        ))
        if not tool_error:
            with _SCANNER_CACHE_LOCK:
                _SCANNER_CACHE[cache_key] = check
        return check


def _find_scanner(scanner: str) -> str | None:
    executable_name = scanner + (".exe" if os.name == "nt" else "")
    candidates: list[Path] = []
    found = shutil.which(scanner)
    if found:
        candidates.append(Path(found))
    candidates.extend([
        Path.home() / "go" / "bin" / executable_name,
        Path.home() / ".genesis" / "tools" / executable_name,
    ])
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _scanner_command(scanner: str, executable: str) -> list[str]:
    help_result = subprocess.run(
        [executable, "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    help_text = (help_result.stdout + "\n" + help_result.stderr).lower()
    if scanner == "gitleaks":
        # Gitleaks v8.24+ uses `dir`; older supported releases use `detect`.
        if re.search(r"(?m)^\s*dir\s+", help_text):
            return [executable, "dir", ".", "--no-banner", "--redact"]
        return [
            executable,
            "detect",
            "--source", ".",
            "--no-git",
            "--no-banner",
            "--redact",
        ]

    command = [executable, "filesystem", "--no-verification", "--json"]
    filesystem_help = subprocess.run(
        [executable, "filesystem", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    fs_help = (filesystem_help.stdout + "\n" + filesystem_help.stderr).lower()
    if "--fail" in fs_help:
        command.append("--fail")
    command.append(".")
    return command


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scanner_config_digest(scanner: str, root: Path) -> str:
    names = (
        (".gitleaks.toml", ".gitleaksignore")
        if scanner == "gitleaks"
        else (".trufflehog-exclude-paths.txt",)
    )
    digest = hashlib.sha256()
    for name in names:
        path = root / name
        digest.update(name.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()
