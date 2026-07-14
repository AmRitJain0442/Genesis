import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from genesis.evidence import evaluate_acceptance_gates
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
        with patch("genesis.evidence.shutil.which", return_value=None):
            report = evaluate_acceptance_gates(
                _step("Run gitleaks clean on the working tree."),
                _patch(["seed.txt"]),
                self.root,
            )

        self.assertFalse(report.passed)
        self.assertTrue(any("unavailable" in item for item in report.violations))

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
