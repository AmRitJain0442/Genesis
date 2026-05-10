from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from genesis.config import GenesisConfig
from genesis.policy import ExecutionPolicy


@dataclass
class CommandResult:
    command: str
    returncode: int
    output: str


@dataclass
class VerificationResult:
    passed: bool
    skipped: bool = False
    reason: str = ""
    commands: list[CommandResult] = field(default_factory=list)


class Verifier:
    def __init__(
        self,
        config: GenesisConfig,
        policy: ExecutionPolicy,
        work_dir: str | Path,
        output_callback: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.policy = policy
        self.work_dir = Path(work_dir)
        self.output_callback = output_callback

    def verify(self, *, changed_files: list[str] | None = None) -> VerificationResult:
        policy_check = self.policy.check_paths(changed_files or [])
        if not policy_check.allowed:
            return VerificationResult(False, reason=policy_check.reason)

        commands = list(self.config.verification.commands)
        if not commands:
            return VerificationResult(True, skipped=True, reason="no verification commands configured")

        results: list[CommandResult] = []
        for command in commands:
            command_check = self.policy.check_command(command)
            if not command_check.allowed:
                return VerificationResult(
                    False,
                    reason=command_check.reason,
                    commands=results,
                )

            if self.output_callback:
                self.output_callback(f"verify $ {command}")

            try:
                proc = subprocess.run(
                    command,
                    cwd=self.work_dir,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.config.verification.timeout,
                )
                output = (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                return VerificationResult(
                    False,
                    reason=f"verification timed out after {self.config.verification.timeout}s: {command}",
                    commands=results,
                )

            output = output.strip()
            if len(output) > 4000:
                output = output[:4000].rstrip() + "\n..."
            results.append(CommandResult(command, proc.returncode, output))
            if proc.returncode != 0:
                return VerificationResult(
                    False,
                    reason=f"verification failed: {command}",
                    commands=results,
                )

        return VerificationResult(True, commands=results)
