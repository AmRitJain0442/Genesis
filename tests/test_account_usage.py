from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from genesis.account_usage import (
    AccountUsage,
    LedgerSummary,
    TokenUsageEvent,
    UsageLedger,
    UsageReport,
    UsageWindow,
    _codex_windows,
    collect_usage,
    parse_token_usage_line,
    usage_report_renderable,
)
from genesis.config import ClaudeAccount, ClaudeCLIConfig, CodexCLIConfig, GenesisConfig


class AccountUsageTests(unittest.TestCase):
    def test_token_lines_are_parsed_without_guessing_codex_cost(self) -> None:
        claude = parse_token_usage_line("Tokens: in=1200 out=34 | $0.0567")
        codex = parse_token_usage_line("Tokens: in=900 (cached=400) out=21")

        self.assertEqual(
            TokenUsageEvent("claude-cli", 1200, 34, cost_usd=0.0567),
            claude,
        )
        self.assertEqual(TokenUsageEvent("codex-cli", 900, 21, 400, 0.0), codex)
        self.assertIsNone(parse_token_usage_line("ordinary worker output"))

    def test_ledger_persists_cost_tokens_and_account_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "usage.db"
            ledger = UsageLedger(path)
            self.assertTrue(
                ledger.record(
                    "claude-one",
                    TokenUsageEvent("claude-cli", 100, 20, cost_usd=0.125),
                )
            )
            self.assertTrue(
                ledger.record(
                    "codex-one",
                    TokenUsageEvent("codex-cli", 300, 40, cached_tokens=80),
                )
            )

            summary = UsageLedger(path).summary()
            self.assertAlmostEqual(0.125, summary.cost_total)
            self.assertAlmostEqual(0.125, summary.cost_24h)
            self.assertEqual(400, summary.input_tokens)
            self.assertEqual(60, summary.output_tokens)
            self.assertEqual(80, summary.cached_tokens)
            self.assertEqual({"claude-one": 0.125, "codex-one": 0.0}, summary.by_account)

    def test_weekly_only_codex_limit_is_not_mislabeled_as_short(self) -> None:
        short, weekly = _codex_windows(
            {
                "primary": {
                    "usedPercent": 62,
                    "resetsAt": 1785141900,
                    "windowDurationMins": 10080,
                },
                "secondary": None,
            }
        )

        self.assertIsNone(short)
        self.assertIsNotNone(weekly)
        assert weekly is not None
        self.assertEqual("1w", weekly.label)
        self.assertEqual(38.0, weekly.remaining_percent)

    def test_live_failure_uses_last_good_snapshot_and_marks_it_stale(self) -> None:
        config = GenesisConfig(
            claude_cli=ClaudeCLIConfig(
                accounts=[ClaudeAccount(name="claude-one")],
                accounts_explicit=True,
            ),
            codex_cli=CodexCLIConfig(accounts=[], accounts_explicit=True),
        )
        success = AccountUsage(
            name="claude-one",
            provider="claude-cli",
            plan="pro",
            short_window=UsageWindow("5h", 25.0),
            status="live",
            fetched_at="2026-07-21T00:00:00Z",
        )
        failure = AccountUsage(
            name="claude-one",
            provider="claude-cli",
            status="error",
            error="temporary outage",
        )

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "usage-cache.json"
            ledger = UsageLedger(Path(temporary) / "usage.db")
            with patch("genesis.account_usage._fetch_claude_usage", return_value=success):
                first = collect_usage(config, refresh=True, cache_path=cache, ledger=ledger)
            with patch("genesis.account_usage._fetch_claude_usage") as fetch:
                cached = collect_usage(config, cache_path=cache, ledger=ledger)
                fetch.assert_not_called()
            with patch("genesis.account_usage._fetch_claude_usage", return_value=failure):
                second = collect_usage(config, refresh=True, cache_path=cache, ledger=ledger)

        self.assertEqual("live", first.accounts[0].status)
        self.assertEqual("live", cached.accounts[0].status)
        self.assertEqual("stale", second.accounts[0].status)
        self.assertEqual("temporary outage", second.accounts[0].error)
        self.assertEqual(75.0, second.accounts[0].short_window.remaining_percent)

    def test_capacity_renderer_contains_graphs_cost_and_honest_units(self) -> None:
        report = UsageReport(
            accounts=(
                AccountUsage(
                    name="account-[one]",
                    provider="claude-cli",
                    plan="pro",
                    short_window=UsageWindow("5h", 25.0),
                    weekly_window=UsageWindow("7d", 50.0),
                ),
            ),
            ledger=LedgerSummary(
                cost_24h=0.5,
                cost_7d=1.25,
                cost_total=2.0,
                by_account={"account-[one]": 2.0},
            ),
            generated_at="2026-07-21T00:00:00Z",
        )
        stream = io.StringIO()
        console = Console(file=stream, width=150, color_system=None, force_terminal=False)
        console.print(usage_report_renderable(report))
        rendered = stream.getvalue()

        self.assertIn("ACCOUNT CAPACITY", rendered)
        self.assertIn("[#########...]", rendered)
        self.assertIn("[######......]", rendered)
        self.assertIn("0.75/1 acct-eq", rendered)
        self.assertIn("$2.0000", rendered)
        self.assertIn("account-[one]", rendered)
        self.assertIn("Codex subscription dollar spend is not exposed", rendered)


if __name__ == "__main__":
    unittest.main()
