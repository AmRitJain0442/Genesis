import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from genesis.evidence import (
    _SCANNER_CACHE,
    _run_secret_scanner,
    _scanner_command,
    evaluate_acceptance_gates,
)
from genesis.worktree import WorktreePatch


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _step(description: str):
    return types.SimpleNamespace(
        title="Harden configuration",
        description=description,
        expected_output="Security acceptance criteria pass.",
        context_hint="",
        file_scope=[],
    )


def _patch(files: list[str]) -> WorktreePatch:
    return WorktreePatch(
        worktree_path=".",
        patch_text="patch",
        changed_files=files,
        diff_status_lines=[f"M\t{path}" for path in files],
    )


class AcceptanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        _SCANNER_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, "init")
        _git(self.root, "config", "user.email", "test@example.com")
        _git(self.root, "config", "user.name", "Test")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(self.root, "add", "seed.txt")
        _git(self.root, "commit", "-m", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_required_artifacts_are_concrete_failures(self) -> None:
        report = evaluate_acceptance_gates(
            _step("Create a real .env.example and add requirements.txt with pinned dependencies."),
            _patch(["SECURITY.md"]),
            self.root,
        )

        self.assertFalse(report.passed)
        self.assertTrue(any(".env.example" in item for item in report.violations))
        self.assertTrue(any("requirements.txt" in item for item in report.violations))
        self.assertIn(
            "pinned-dependencies",
            [check.name for check in report.checks],
        )

    def test_requirement_mapping_does_not_activate_dependency_pinning(self) -> None:
        report = evaluate_acceptance_gates(
            _step(
                "Create docs/TRACEABILITY.md mapping every requirement to a stable "
                "REQ-### id. Reconcile the requirements against the WBS and deliver "
                "docs only — no source."
            ),
            _patch([
                "docs/ASSUMPTIONS.md",
                "docs/PRD.md",
                "docs/TRACEABILITY.md",
            ]),
            self.root,
        )

        self.assertTrue(report.passed, report.violations)
        self.assertNotIn(
            "pinned-dependencies",
            [check.name for check in report.checks],
        )

    def test_explicit_dependency_pinning_phrases_activate_gate(self) -> None:
        phrases = (
            "Pin every runtime requirement with ==.",
            "Use pinned dependencies for reproducible installs.",
            "Finish pinning dependency versions.",
        )

        for phrase in phrases:
            with self.subTest(phrase=phrase):
                report = evaluate_acceptance_gates(
                    _step(phrase),
                    _patch(["seed.txt"]),
                    self.root,
                )

                self.assertFalse(report.passed)
                self.assertIn(
                    "pinned-dependencies",
                    [check.name for check in report.checks],
                )

    def test_security_contract_passes_on_actual_files_and_index(self) -> None:
        (self.root / ".env.example").write_text(
            "TRIBE_API_KEY=your-api-key\nTRIBE_ENDPOINT=https://example.invalid\n",
            encoding="utf-8",
        )
        (self.root / "requirements.txt").write_text(
            "python-dotenv==1.0.1\nrequests==2.32.3\n",
            encoding="utf-8",
        )
        (self.root / "app.py").write_text(
            "import os\nKEY = os.environ['TRIBE_API_KEY']\n"
            "ENDPOINT = os.environ['TRIBE_ENDPOINT']\n",
            encoding="utf-8",
        )
        report = evaluate_acceptance_gates(
            _step(
                "Create a real .env.example and requirements.txt with pinned dependencies. "
                "Load TRIBE_API_KEY and TRIBE_ENDPOINT from environment variables; both are "
                "required with no fallback. Ensure git ls-files is secret-free."
            ),
            _patch([".env.example", "requirements.txt", "app.py"]),
            self.root,
        )

        self.assertTrue(report.passed, report.violations)

    def test_literal_api_key_fallback_is_rejected(self) -> None:
        (self.root / "app.py").write_text(
            "import os\nKEY = os.getenv('TRIBE_API_KEY', 'e8732332e08eb0b5da3581fc6ba1284d')\n",
            encoding="utf-8",
        )

        report = evaluate_acceptance_gates(
            _step("Read required TRIBE_API_KEY from env with no fallback; remove the hardcoded API key."),
            _patch(["app.py"]),
            self.root,
        )

        self.assertFalse(report.passed)
        self.assertTrue(any("fallback" in item.lower() for item in report.violations))

    def test_explicit_scanner_is_not_reported_clean_when_unavailable(self) -> None:
        with patch("genesis.evidence._find_scanner", return_value=None):
            report = evaluate_acceptance_gates(
                _step("Run gitleaks clean on the working tree."),
                _patch(["seed.txt"]),
                self.root,
            )

        self.assertFalse(report.passed)
        self.assertFalse(report.repairable)
        self.assertTrue(any("unavailable" in item for item in report.violations))

    def test_external_scanners_are_deferred_during_worker_dialogue(self) -> None:
        with patch("genesis.evidence._run_secret_scanner") as scanner:
            report = evaluate_acceptance_gates(
                _step("Run gitleaks and trufflehog clean on the working tree."),
                _patch(["seed.txt"]),
                self.root,
                run_external_scanners=False,
            )

        self.assertTrue(report.passed)
        scanner.assert_not_called()
        names = [check.name for check in report.checks]
        self.assertIn("secret-scan:gitleaks:deferred", names)
        self.assertIn("secret-scan:trufflehog:deferred", names)

    def test_legacy_gitleaks_uses_detect_instead_of_unsupported_dir(self) -> None:
        help_result = types.SimpleNamespace(
            stdout="Available Commands:\n  detect  detect secrets\n",
            stderr="",
            returncode=0,
        )
        with patch("genesis.evidence.subprocess.run", return_value=help_result):
            command = _scanner_command("gitleaks", "gitleaks.exe")

        self.assertEqual("detect", command[1])
        self.assertIn("--no-git", command)

    def test_modern_gitleaks_uses_dir_when_capability_is_advertised(self) -> None:
        help_result = types.SimpleNamespace(
            stdout="Available Commands:\n  dir       scan a directory\n",
            stderr="",
            returncode=0,
        )
        with patch("genesis.evidence.subprocess.run", return_value=help_result):
            command = _scanner_command("gitleaks", "gitleaks.exe")

        self.assertEqual("dir", command[1])

    def test_scanner_result_is_cached_by_patch_sha(self) -> None:
        completed = types.SimpleNamespace(stdout="", stderr="", returncode=0)
        executable = self.root / "gitleaks.exe"
        executable.write_bytes(b"scanner")
        with (
            patch("genesis.evidence._find_scanner", return_value=str(executable)),
            patch("genesis.evidence._scanner_command", return_value=[str(executable), "detect"]),
            patch("genesis.evidence.subprocess.run", return_value=completed) as run,
        ):
            first = _run_secret_scanner("gitleaks", self.root, "patch-1")
            second = _run_secret_scanner("gitleaks", self.root, "patch-1")

        self.assertTrue(first.passed)
        self.assertEqual(first, second)
        run.assert_called_once()

    def test_trufflehog_findings_fail_even_without_fail_flag(self) -> None:
        completed = types.SimpleNamespace(
            stdout='{"DetectorName":"Test secret"}\n',
            stderr="",
            returncode=0,
        )
        executable = self.root / "trufflehog.exe"
        executable.write_bytes(b"scanner")
        with (
            patch("genesis.evidence._find_scanner", return_value=str(executable)),
            patch(
                "genesis.evidence._scanner_command",
                return_value=[str(executable), "filesystem", "--json", "."],
            ),
            patch("genesis.evidence.subprocess.run", return_value=completed),
        ):
            check = _run_secret_scanner(
                "trufflehog", self.root, "patch-with-secret"
            )

        self.assertFalse(check.passed)
        self.assertIn("detected", check.detail)
        self.assertNotIn("DetectorName", check.detail)

    def test_hardcoded_endpoint_is_rejected_when_env_migration_is_requested(self) -> None:
        (self.root / "app.py").write_text(
            "ENDPOINT = 'https://rotating.trycloudflare.com'\n",
            encoding="utf-8",
        )

        report = evaluate_acceptance_gates(
            _step(
                "Remove the hardcoded tunnel URL and API key; move endpoint configuration "
                "to the TRIBE_ENDPOINT environment variable."
            ),
            _patch(["app.py"]),
            self.root,
        )

        self.assertFalse(report.passed)
        self.assertTrue(any("hardcoded" in item.lower() for item in report.violations))


if __name__ == "__main__":
    unittest.main()
