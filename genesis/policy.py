from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from genesis.config import GenesisConfig

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class PolicyCheck:
    allowed: bool
    reason: str = ""


@dataclass
class ExecutionPolicy:
    protected_paths: list[str] = field(default_factory=lambda: [".git/", ".genesis/state/"])
    blocked_commands: list[str] = field(
        default_factory=lambda: [
            "git reset --hard",
            "git checkout --",
            "Remove-Item -Recurse -Force",
            "rm -rf /",
        ]
    )
    allowed_commands: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, repo_path: str | Path, config: GenesisConfig) -> "ExecutionPolicy":
        policy = cls(
            protected_paths=list(config.policy.protected_paths),
            blocked_commands=list(config.policy.blocked_commands),
            allowed_commands=list(config.policy.allowed_commands),
        )
        policy_file = Path(repo_path) / config.policy.file
        if policy_file.exists() and tomllib is not None:
            with open(policy_file, "rb") as f:
                data = tomllib.load(f)
            section = data.get("policy", data)
            policy.protected_paths = section.get("protected_paths", policy.protected_paths)
            policy.blocked_commands = section.get("blocked_commands", policy.blocked_commands)
            policy.allowed_commands = section.get("allowed_commands", policy.allowed_commands)
        return policy

    def check_paths(self, paths: list[str]) -> PolicyCheck:
        normalized = [p.replace("\\", "/").lstrip("./") for p in paths]
        for path in normalized:
            if path.startswith("../") or path == "..":
                return PolicyCheck(False, f"path escapes repository: {path}")
            for protected in self.protected_paths:
                protected_norm = protected.replace("\\", "/").lstrip("./")
                if path == protected_norm.rstrip("/") or path.startswith(protected_norm):
                    return PolicyCheck(False, f"protected path changed: {path}")
        return PolicyCheck(True)

    def check_command(self, command: str) -> PolicyCheck:
        compact = " ".join(command.strip().split())
        lowered = compact.lower()
        for blocked in self.blocked_commands:
            if blocked.lower() in lowered:
                return PolicyCheck(False, f"blocked command: {blocked}")
        if self.allowed_commands:
            if not any(lowered.startswith(prefix.lower()) for prefix in self.allowed_commands):
                return PolicyCheck(False, f"command not in allow-list: {compact}")
        return PolicyCheck(True)
