import unittest

from genesis.agents.collaboration import BrainCollaboration, parse_agreement
from genesis.chatroom import ChatroomManager, RoomKind


class FakeBrain:
    """A brain whose replies are scripted; last script repeats if exhausted."""
    def __init__(self, name: str, replies: list[str]) -> None:
        self.name = name
        self._replies = list(replies)
        self.calls = 0

    def chat(self, system, messages, output_callback=None) -> str:
        self.calls += 1
        idx = min(self.calls - 1, len(self._replies) - 1)
        return self._replies[idx]


class ParseAgreementTests(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertTrue(parse_agreement("looks good\nAGREE: yes"))
        self.assertTrue(parse_agreement("AGREE: YES"))
        self.assertFalse(parse_agreement("needs work\nAGREE: no"))
        self.assertFalse(parse_agreement("no marker at all"))
        # The final marker wins.
        self.assertTrue(parse_agreement("AGREE: no\n...reconsidered...\nAGREE: yes"))


class DebateTests(unittest.TestCase):
    def test_converges_when_both_agree(self) -> None:
        claude = FakeBrain("claude", ["plan A\nAGREE: no", "refined\nAGREE: yes"])
        codex = FakeBrain("codex", ["counter\nAGREE: no", "ok\nAGREE: yes"])
        result = BrainCollaboration(claude, codex, max_rounds=4).discuss("build X")
        self.assertTrue(result.converged)
        self.assertEqual(2, result.rounds)
        self.assertEqual(4, len(result.turns))
        self.assertIn("refined", result.transcript)

    def test_immediate_agreement_in_first_round(self) -> None:
        claude = FakeBrain("claude", ["great plan\nAGREE: yes"])
        codex = FakeBrain("codex", ["agreed\nAGREE: yes"])
        result = BrainCollaboration(claude, codex, max_rounds=4).discuss("build X")
        self.assertTrue(result.converged)
        self.assertEqual(1, result.rounds)
        self.assertEqual(2, len(result.turns))

    def test_hits_round_cap_without_agreement(self) -> None:
        claude = FakeBrain("claude", ["nope\nAGREE: no"])
        codex = FakeBrain("codex", ["also nope\nAGREE: no"])
        result = BrainCollaboration(claude, codex, max_rounds=2).discuss("build X")
        self.assertFalse(result.converged)
        self.assertEqual(2, result.rounds)
        self.assertEqual(4, len(result.turns))   # 2 brains x 2 rounds

    def test_one_sided_agreement_does_not_converge(self) -> None:
        claude = FakeBrain("claude", ["AGREE: yes"])
        codex = FakeBrain("codex", ["AGREE: no"])
        result = BrainCollaboration(claude, codex, max_rounds=3).discuss("build X")
        self.assertFalse(result.converged)
        self.assertEqual(3, result.rounds)

    def test_debate_is_posted_to_chatroom(self) -> None:
        mgr = ChatroomManager()
        claude = FakeBrain("claude", ["AGREE: yes"])
        codex = FakeBrain("codex", ["AGREE: yes"])
        result = BrainCollaboration(claude, codex, chatroom=mgr, max_rounds=3).discuss("build X")

        rooms = mgr.rooms()
        self.assertEqual(1, len(rooms))
        self.assertEqual(RoomKind.brain_room, rooms[0].kind)
        msgs = mgr.history(result.room_id)
        # two brain turns + one moderator verdict
        self.assertEqual(3, len(msgs))
        self.assertEqual({"claude", "codex", "moderator"}, {m.sender for m in msgs})
        self.assertTrue(any(m.role == "system" for m in msgs))


if __name__ == "__main__":
    unittest.main()
