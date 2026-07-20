from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from genesis.config import GenesisConfig
from genesis.agents.base import terminate_process_tree
from genesis.policy import ExecutionPolicy


logger = logging.getLogger(__name__)


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
                try:
                    self.output_callback(f"verify $ {command}")
                except Exception:
                    logger.warning("Verification output callback failed", exc_info=True)

            with tempfile.TemporaryFile(mode="w+b") as captured:
                try:
                    proc = subprocess.Popen(
                        command,
                        cwd=self.work_dir,
                        shell=True,
                        stdout=captured,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                except OSError as exc:
                    return VerificationResult(
                        False,
                        reason=f"could not start verification command {command}: {exc}",
                        commands=results,
                    )
                try:
                    proc.wait(timeout=self.config.verification.timeout)
                except subprocess.TimeoutExpired:
                    terminate_process_tree(proc)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return VerificationResult(
                        False,
                        reason=f"verification timed out after {self.config.verification.timeout}s: {command}",
                        commands=results,
                    )
                output = _read_bounded_output(captured)

            output = output.strip()
            results.append(CommandResult(command, proc.returncode, output))
            if proc.returncode != 0:
                return VerificationResult(
                    False,
                    reason=f"verification failed: {command}",
                    commands=results,
                )

        return VerificationResult(True, commands=results)


def _read_bounded_output(stream, limit: int = 4000) -> str:
    """Read useful head/tail diagnostics without buffering unlimited output."""

    stream.flush()
    size = stream.seek(0, 2)
    if size <= limit:
        stream.seek(0)
        data = stream.read()
    else:
        marker = b"\n... (verification output truncated) ...\n"
        payload_limit = max(0, limit - len(marker))
        head_size = int(payload_limit * 0.7)
        tail_size = payload_limit - head_size
        stream.seek(0)
        head = stream.read(head_size)
        stream.seek(-tail_size, 2)
        tail = stream.read(tail_size)
        data = head + marker + tail
    return data.decode("utf-8", errors="replace")
