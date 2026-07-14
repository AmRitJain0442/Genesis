import unittest
from pathlib import Path

from genesis import config as config_mod


class DefaultConfigTests(unittest.TestCase):
    def test_embedded_default_is_valid_and_exposes_harness_controls(self) -> None:
        self.assertIsNotNone(config_mod.tomllib)
        data = config_mod.tomllib.loads(config_mod._DEFAULT_CONFIG)

        self.assertTrue(data["collaboration"]["enabled"])
        self.assertTrue(data["dialogue"]["enabled"])
        self.assertTrue(data["failover"]["enabled"])
        self.assertEqual(600, data["codex_cli"]["timeout"])

    def test_tracked_example_config_is_valid(self) -> None:
        example = Path(__file__).parents[1] / "config.example.toml"
        with example.open("rb") as handle:
            data = config_mod.tomllib.load(handle)

        self.assertTrue(data["dialogue"]["enabled"])
        self.assertTrue(data["failover"]["enabled"])
        accounts = data["codex_cli"]["accounts"]
        self.assertTrue(accounts)
        self.assertFalse(bool(accounts[0].get("reserve", False)))


if __name__ == "__main__":
    unittest.main()
