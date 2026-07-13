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


def _summary(result: "WorkerResult", turn: int) -> str:
    files = getattr(result, "files_written", None) or []
    head = f"Turn {turn}: wrote {len(files)} file(s)"
    if files:
        head += ": " + ", ".join(files)
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
        sid = self.step.step_id
        cap = self.max_turns

        for turn in range(1, self.max_turns + 1):
            turns = turn
            self._status(f"{self.worker_name} implementing {sid} (turn {turn}/{cap})...")
            result = self.run_worker(current)
            self.post(self.worker_name, "worker", _summary(result, turn), "code")

            if not getattr(result, "success", False):
                self._status(f"{self.worker_name} failed on {sid} - handing to review")
                self.post(self.brain_name, "brain",
                          f"Worker failed: {getattr(result, 'error', '') or 'unknown error'}", "status")
                break

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
