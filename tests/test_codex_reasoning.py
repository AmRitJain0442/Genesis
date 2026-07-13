import tempfile
import unittest
from pathlib import Path

import genesis.config as config_mod
from genesis.agents.base import AgentInfo
from genesis.agents.codex_cli import CodexCLIAgent


class ReasoningConfigTests(unittest.TestCase):
    def test_reasoning_parsed_from_config(self) -> None:
        original_file = config_mod.CONFIG_FILE
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """\
[[codex_cli.accounts]]
name = "codex-brain"
home = ""
model = "gpt-5.6-sol"
reasoning = "high"

[[codex_cli.accounts]]
name = "codex-plain"
model = "auto"
""",
                encoding="utf-8",
            )
            try:
                config_mod.CONFIG_FILE = path
                cfg = config_mod.load_config()
            finally:
                config_mod.CONFIG_FILE = original_file

        brain, plain = cfg.codex_cli.accounts
        self.assertEqual("high", brain.reasoning)
        self.assertEqual("gpt-5.6-sol", brain.model)
        self.assertEqual("", plain.reasoning)   # default when unset


class ReasoningCommandTests(unittest.TestCase):
    def _cmd(self, reasoning: str, model: str = "gpt-5.6-sol") -> list[str]:
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            raise RuntimeError("stop after capturing argv")

        agent = CodexCLIAgent(
            AgentInfo("codex-brain", "codex-cli", model, max_tokens=8096),
            reasoning=reasoning,
        )
        import genesis.agents.codex_cli as mod
        orig = mod.subprocess.run
        mod.subprocess.run = fake_run
        try:
            agent.chat("sys", [{"content": "hi"}])
        except RuntimeError:
            pass
        finally:
            mod.subprocess.run = orig
        return captured["cmd"]

    def test_high_reasoning_adds_config_flag(self) -> None:
        cmd = self._cmd("high")
        joined = " ".join(cmd)
        self.assertIn("model_reasoning_effort=high", joined)
        self.assertIn("--model", cmd)
        self.assertIn("gpt-5.6-sol", cmd)

    def test_no_reasoning_omits_flag(self) -> None:
        cmd = self._cmd("")
        self.assertNotIn("model_reasoning_effort", " ".join(cmd))


if __name__ == "__main__":
    unittest.main()
