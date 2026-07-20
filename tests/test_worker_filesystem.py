from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from genesis.agents.worker import Worker
from genesis.schemas.plan import Step


class _StaticAgent:
    def __init__(self, response: str):
        self.response = response

    def chat(self, _system, _messages, output_callback=None) -> str:
        return self.response


def _step() -> Step:
    return Step(
        step_id="step-1",
        title="Write files",
        description="Create the requested files.",
        type="code",
        expected_output="Working files",
    )


def _response(*blocks: tuple[str, str]) -> str:
    code = "".join(
        f'<code lang="text" file="{filename}">\n{content}</code>'
        for filename, content in blocks
    )
    return f"<result>{code}</result>"


class WorkerFilesystemTests(unittest.TestCase):
    def test_parent_traversal_is_rejected_before_any_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            work_dir.mkdir()
            worker = Worker(
                _StaticAgent(
                    _response(
                        ("safe.txt", "safe\n"),
                        ("../escaped.txt", "escaped\n"),
                    )
                ),
                "",
                str(work_dir),
            )

            result = worker.execute(_step())

            self.assertFalse(result.success)
            self.assertIn("may not contain '..'", result.error)
            self.assertFalse((work_dir / "safe.txt").exists())
            self.assertFalse((root / "escaped.txt").exists())

    def test_absolute_and_drive_relative_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            work_dir.mkdir()
            paths = [
                (root / "absolute.txt").resolve().as_posix(),
                r"C:\drive-relative.txt",
                r"\\server\share\escaped.txt",
            ]

            for filename in paths:
                with self.subTest(filename=filename):
                    worker = Worker(
                        _StaticAgent(_response((filename, "escaped\n"))),
                        "",
                        str(work_dir),
                    )
                    result = worker.execute(_step())
                    self.assertFalse(result.success)
                    self.assertIn("absolute worker output path", result.error)

            self.assertFalse((root / "absolute.txt").exists())

    def test_ambiguous_or_special_windows_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            paths = ["safe.txt:alternate-stream", "line\nbreak.txt"]
            if os.name == "nt":
                paths.extend(["NUL.txt", "trailing-dot.", "trailing-space "])

            for filename in paths:
                with self.subTest(filename=filename):
                    result = Worker(
                        _StaticAgent(_response((filename, "unsafe\n"))),
                        "",
                        str(work_dir),
                    ).execute(_step())
                    self.assertFalse(result.success)

    def test_symlink_to_outside_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            outside_dir = root / "outside"
            work_dir.mkdir()
            outside_dir.mkdir()
            link = work_dir / "linked"
            try:
                link.symlink_to(outside_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            worker = Worker(
                _StaticAgent(_response(("linked/escaped.txt", "escaped\n"))),
                "",
                str(work_dir),
            )
            result = worker.execute(_step())

            self.assertFalse(result.success)
            self.assertIn("escapes the work directory", result.error)
            self.assertFalse((outside_dir / "escaped.txt").exists())

    def test_duplicate_canonical_destinations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            worker = Worker(
                _StaticAgent(
                    _response(
                        ("nested/file.txt", "first\n"),
                        ("nested/./file.txt", "second\n"),
                    )
                ),
                "",
                str(work_dir),
            )

            result = worker.execute(_step())

            self.assertFalse(result.success)
            self.assertIn("duplicate worker output destination", result.error)
            self.assertFalse((work_dir / "nested" / "file.txt").exists())

    def test_atomic_replace_preserves_existing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            destination = work_dir / "script.sh"
            destination.write_text("old\n", encoding="utf-8")
            destination.chmod(0o744)
            original_mode = stat.S_IMODE(destination.stat().st_mode)
            worker = Worker(
                _StaticAgent(_response(("script.sh", "new\n"))),
                "",
                str(work_dir),
            )

            result = worker.execute(_step())

            self.assertTrue(result.success, result.error)
            self.assertEqual("new\n", destination.read_text(encoding="utf-8"))
            self.assertEqual(original_mode, stat.S_IMODE(destination.stat().st_mode))
            self.assertEqual(["script.sh"], result.files_written)
            self.assertEqual([], list(work_dir.glob(".script.sh.genesis-*.tmp")))

    def test_failed_replace_leaves_original_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            destination = work_dir / "existing.txt"
            destination.write_text("old\n", encoding="utf-8")
            worker = Worker(
                _StaticAgent(_response(("existing.txt", "new\n"))),
                "",
                str(work_dir),
            )

            with patch("genesis.agents.worker.os.replace", side_effect=OSError("busy")):
                result = worker.execute(_step())

            self.assertFalse(result.success)
            self.assertEqual("old\n", destination.read_text(encoding="utf-8"))
            self.assertEqual([], list(work_dir.glob(".existing.txt.genesis-*.tmp")))


if __name__ == "__main__":
    unittest.main()
