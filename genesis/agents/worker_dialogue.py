"""
WorkerDialogue — a bounded, multi-turn conversation between a brain and a worker.

Instead of a single fire-and-forget worker call, the assigning brain and the
worker iterate: the worker implements, the brain evaluates and either approves or
asks for specific revisions, and the worker revises — up to a turn cap. Every
turn is posted to the step's worker_room, so the collaboration is watchable live.

The independent reviewer (a different agent) still gates the result afterward;
this dialogue only shapes the work into reviewable form.

The class depends on injected callables, not orchestrator internals, so it is
fully testable with fakes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from genesis.agents.worker import WorkerResult
    from genesis.schemas.plan import Step

logger = logging.getLogger(__name__)


@dataclass
class DialogueOutcome:
    result: "WorkerResult | None"
    turns: int
    approved: bool          # the directing brain approved within the dialogue
    last_step: "Step"       # the (possibly revised) step behind the final result


def _summary(
    result: "WorkerResult",
    turn: int,
    files: list[str] | None = None,
) -> str:
    files = list(files if files is not None else (getattr(result, "files_written", None) or []))
    head = (
        f"Turn {turn}: wrote {len(files)} file(s): {', '.join(files)}"
        if files
        else f"Turn {turn}: completed without file changes"
    )
    note = (getattr(result, "result_text", "") or "").strip().splitlines()
    if note:
        head += f"\n{note[0][:200]}"
    return head


class WorkerDialogue:
    def __init__(
        self,
        *,
        step: "Step",
        worker_name: str,
        brain_name: str,
        max_turns: int,
        run_worker: Callable[["Step"], "WorkerResult"],
        evaluate: Callable[["Step", "WorkerResult", int], tuple[bool, str]],
        make_revision: Callable[["Step", str], "Step"],
        post: Callable[..., None],
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.step = step
        self.worker_name = worker_name
        self.brain_name = brain_name
        self.max_turns = max(1, max_turns)
        self.run_worker = run_worker
        self.evaluate = evaluate
        self.make_revision = make_revision
        self.post = post
        self.on_status = on_status

    def _status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def run(self) -> DialogueOutcome:
        current = self.step
        result = None
        approved = False
        turns = 0
        cumulative_files: list[str] = []
        sid = self.step.step_id
        cap = self.max_turns

        for turn in range(1, self.max_turns + 1):
            turns = turn
            self._status(f"{self.worker_name} implementing {sid} (turn {turn}/{cap})...")
            self.post(
                self.worker_name,
                "worker",
                f"Actively working on {sid} (turn {turn}/{cap})...",
                "status",
            )
            result = self.run_worker(current)

            authoritative_files = list(
                getattr(result, "files_written", None) or []
            )
            evidence = getattr(result, "evidence", None) or {}
            turn_files = list(
                evidence.get("turn_reported_files", authoritative_files)
            )
            for path in authoritative_files:
                if path not in cumulative_files:
                    cumulative_files.append(path)
            # Each Codex turn reports only files changed during that turn. Keep
            # the cumulative manifest on the result handed to later brain and
            # independent-review phases so earlier implementation evidence is
            # never lost when the final turn touches just one file.
            try:
                result.files_written = list(cumulative_files)
            except Exception:
                pass
            result_text = (getattr(result, "result_text", "") or "").strip()
            made_progress = bool(turn_files or result_text)
            if made_progress:
                self.post(
                    self.worker_name,
                    "worker",
                    _summary(result, turn, turn_files),
                    "code",
                )
            elif getattr(result, "success", False):
                retrying = turn < self.max_turns
                status = (
                    f"No file changes returned on turn {turn}; retrying implementation."
                    if retrying
                    else f"No file changes returned on turn {turn}."
                )
                self._status(status)
                self.post(self.worker_name, "worker", status, "status")

            if not getattr(result, "success", False):
                self._status(f"{self.worker_name} failed on {sid} - handing to review")
                self.post(self.brain_name, "brain",
                          f"Worker failed: {getattr(result, 'error', '') or 'unknown error'}", "status")
                break

            # An empty successful CLI response is not reviewable. Retry it
            # directly without spending a brain call or posting a misleading
            # "wrote 0 files" implementation message.
            if not made_progress and turn < self.max_turns:
                current = self.make_revision(
                    current,
                    "No output or file changes were produced. Execute the step now "
                    "and make the concrete requested changes.",
                )
                continue

            guard_violations = list(evidence.get("guard_violations", []) or [])
            if guard_violations and turn < self.max_turns:
                feedback = "Deterministic evidence guard failed:\n- " + "\n- ".join(
                    str(item) for item in guard_violations
                )
                self._status(
                    f"Evidence guard rejected turn {turn} on {sid}; retrying repair"
                )
                self.post(self.brain_name, "brain", feedback, "status")
                current = self.make_revision(current, feedback)
                continue

            if turn >= self.max_turns:
                self._status(f"Turn budget reached on {sid} - handing to independent review")
                self.post(self.brain_name, "brain",
                          "Turn budget reached — handing to independent review.", "decision")
                break

            self._status(f"{self.brain_name} reviewing {sid} (turn {turn}/{cap})...")
            try:
                approve, feedback = self.evaluate(self.step, result, turn)
            except Exception as e:
                logger.warning("Brain evaluation failed on turn %d: %s", turn, e)
                approve, feedback = True, ""   # fail open — don't loop on evaluator errors

            if approve:
                self._status(f"{self.brain_name} approved {sid} - handing to independent review")
                self.post(self.brain_name, "brain",
                          "Looks complete — handing to independent review.", "decision")
                approved = True
                break

            self._status(f"{self.brain_name} requested changes on {sid} (turn {turn})")
            self.post(self.brain_name, "brain", f"Revise: {feedback}", "message")
            current = self.make_revision(self.step, feedback)

        return DialogueOutcome(result=result, turns=turns, approved=approved, last_step=current)
