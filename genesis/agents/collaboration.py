"""
BrainCollaboration — two senior "brains" debate a task until they agree.

The Claude brain (e.g. Opus 4.8) and the Codex brain (e.g. GPT-5.6-sol) take
turns in a chatroom, critiquing and refining until BOTH end a message with
`AGREE: yes`, or a hard round cap is reached (the backstop). The resulting
discussion transcript is handed to the planner, which synthesizes the final
structured plan — so even a capped, unresolved debate still yields a plan.

The class depends only on the BaseAgent `.chat` interface and an optional
ChatroomManager, so it is fully testable with scripted fake agents.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.agents.base import BaseAgent
    from genesis.chatroom import ChatroomManager

logger = logging.getLogger(__name__)

_AGREE_RE = re.compile(r"AGREE:\s*(yes|no)", re.IGNORECASE)

_DEBATE_SYSTEM = """You are {name}, one of two senior engineering "brains" collaborating to plan a software task.
You are talking directly to your peer brain. Discuss the approach, architecture, risks, and — critically —
the testing strategy needed for fully tested code. Be concise and concrete; critique your peer's points and
refine toward a single shared plan.

End EVERY message with a final line in exactly this form:
AGREE: yes   — only if you fully agree with the current shared plan and have nothing material to add
AGREE: no    — if you still want changes (say briefly what)

Do not write AGREE: yes just to end the conversation; agree only when the plan is genuinely sound."""


@dataclass
class DiscussionResult:
    transcript: str
    converged: bool
    rounds: int
    room_id: str = ""
    turns: list[tuple[str, str]] = field(default_factory=list)  # (sender, content)


def parse_agreement(text: str) -> bool:
    """True iff the message's final AGREE marker says yes."""
    matches = _AGREE_RE.findall(text or "")
    return bool(matches) and matches[-1].lower() == "yes"


class BrainCollaboration:
    def __init__(
        self,
        claude_brain: "BaseAgent",
        codex_brain: "BaseAgent",
        chatroom: "ChatroomManager | None" = None,
        max_rounds: int = 4,
    ) -> None:
        # Claude speaks first each round and later arbitrates, so it is listed first.
        self.brains = [claude_brain, codex_brain]
        self.chatroom = chatroom
        self.max_rounds = max(1, max_rounds)

    def discuss(self, task: str, context: str = "") -> DiscussionResult:
        room_id = ""
        if self.chatroom is not None:
            room = self.chatroom.create_room(
                _room_kind(),
                f"Brain debate — {task[:50]}",
                participants=[_name(b) for b in self.brains],
            )
            room_id = room.id

        turns: list[tuple[str, str]] = []
        agreed: dict[str, bool] = {_name(b): False for b in self.brains}
        converged = False
        rounds = 0

        for rounds in range(1, self.max_rounds + 1):
            for brain in self.brains:
                name = _name(brain)
                reply = self._speak(brain, task, context, turns)
                turns.append((name, reply))
                agreed[name] = parse_agreement(reply)
                self._post(room_id, name, reply)

                # Converged the moment both brains' most recent messages agree.
                if all(agreed.values()):
                    converged = True
                    break
            if converged:
                break

        transcript = "\n\n".join(f"{sender}:\n{content}" for sender, content in turns)
        if self.chatroom is not None and room_id:
            verdict = "Brains converged." if converged else "Round cap reached — Claude will arbitrate."
            self._post(room_id, "moderator", verdict, role="system", kind="decision")

        logger.debug("Brain debate finished: converged=%s rounds=%s", converged, rounds)
        return DiscussionResult(
            transcript=transcript,
            converged=converged,
            rounds=rounds,
            room_id=room_id,
            turns=turns,
        )

    # ── internals ────────────────────────────────────────────────────────────

    def _speak(self, brain: "BaseAgent", task: str, context: str,
               turns: list[tuple[str, str]]) -> str:
        name = _name(brain)
        system = _DEBATE_SYSTEM.format(name=name)
        if turns:
            transcript = "\n\n".join(f"{s}:\n{c}" for s, c in turns)
            discussion = f"DISCUSSION SO FAR:\n{transcript}\n\n"
        else:
            discussion = "You are opening the discussion.\n\n"
        user = (
            (f"{context}\n\n---\n\n" if context else "")
            + f"TASK TO PLAN:\n{task}\n\n{discussion}"
            f"Your turn, {name}. Respond, then end with your AGREE line."
        )
        try:
            reply = brain.chat(system, [{"role": "user", "content": user}])
        except Exception as e:
            logger.warning("Brain %s failed to respond: %s", name, e)
            reply = f"(no response — {e})\nAGREE: no"
        return (reply or "").strip() or "AGREE: no"

    def _post(self, room_id: str, sender: str, content: str,
              role: str = "brain", kind: str = "message") -> None:
        if self.chatroom is None or not room_id:
            return
        try:
            self.chatroom.post(room_id, sender, role, content, kind)
        except Exception:
            pass


def _name(agent: "BaseAgent") -> str:
    return getattr(agent, "name", None) or "brain"


def _room_kind():
    # Imported lazily so the module has no hard dependency on the chatroom package.
    from genesis.chatroom import RoomKind
    return RoomKind.brain_room
