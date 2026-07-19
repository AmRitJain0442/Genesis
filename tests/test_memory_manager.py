from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import genesis.memory as memory_module
from genesis.memory import MemoryManager


def _expected_summary(content: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(content) <= max_chars:
        return content
    tail = content[-max_chars:]
    idx = tail.find("\n\n")
    if idx > 0:
        tail = tail[idx + 2 :]
    else:
        idx = tail.find("\n")
        if idx > 0:
            tail = tail[idx + 1 :]
    return "[...earlier context truncated...]\n\n" + tail


class _TrackedBinaryFile:
    def __init__(self, stream, reads: list[int]):
        self._stream = stream
        self._reads = reads

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        return self._stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def read(self, size: int = -1):
        self._reads.append(size)
        return self._stream.read(size)


class MemoryManagerTests(unittest.TestCase):
    def test_initialization_creates_parents_and_never_truncates_existing_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "deep" / "state" / "MEMORY.md"

            manager = MemoryManager(str(path))

            self.assertEqual(memory_module._HEADER, manager.read())
            path.write_text("existing durable memory\n", encoding="utf-8")
            MemoryManager(str(path))
            self.assertEqual("existing durable memory\n", path.read_text(encoding="utf-8"))

    def test_concurrent_instances_share_a_lock_and_append_complete_records(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MEMORY.md"
            first = MemoryManager(str(path))
            second = MemoryManager(str(path))

            self.assertIs(first._lock, second._lock)
            managers = [first, second]
            tokens = [f"unique-note-{index}" for index in range(24)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(managers[index % 2].append_note, token)
                    for index, token in enumerate(tokens)
                ]
                for future in futures:
                    future.result()

            content = first.read()
            for token in tokens:
                self.assertEqual(1, content.count(f": {token}\n"))

    def test_summary_reads_a_bounded_unicode_safe_tail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MEMORY.md"
            manager = MemoryManager(str(path))
            limit = 41
            base = "😀αβ" * 300 + "\n\nrecent paragraph\n\nfinal café 😀 result"

            # Choose padding that makes the byte seek land inside a code point.
            content = base
            for padding in range(4):
                candidate = "x" * padding + base
                encoded = candidate.encode("utf-8")
                start = len(encoded) - (limit * 4 + 4)
                if start > 0 and encoded[start] & 0xC0 == 0x80:
                    content = candidate
                    break
            else:
                self.fail("test fixture did not create a split UTF-8 boundary")
            path.write_bytes(content.encode("utf-8"))

            original_open = Path.open
            read_sizes: list[int] = []

            def tracked_open(path_self, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                stream = original_open(path_self, *args, **kwargs)
                if path_self == path and mode == "rb":
                    return _TrackedBinaryFile(stream, read_sizes)
                return stream

            with patch.object(Path, "open", new=tracked_open):
                summary = manager.get_summary(limit)

            self.assertEqual(_expected_summary(content, limit), summary)
            self.assertNotIn("�", summary)
            self.assertEqual([limit * 4 + 4], read_sizes)

    def test_summary_nonpositive_limit_does_no_io(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = MemoryManager(str(Path(td) / "MEMORY.md"))
            with patch.object(manager, "_read_tail", side_effect=AssertionError("unexpected read")):
                self.assertEqual("", manager.get_summary(0))
                self.assertEqual("", manager.get_summary(-10))

    def test_clear_is_atomic_and_preserves_old_file_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MEMORY.md"
            manager = MemoryManager(str(path))
            manager.append_note("must survive a failed clear")
            before = path.read_bytes()

            with patch.object(memory_module.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    manager.clear()

            self.assertEqual(before, path.read_bytes())
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

            real_replace = os.replace
            with patch.object(memory_module.os, "replace", side_effect=real_replace) as replace:
                manager.clear()
            replace.assert_called_once()
            self.assertEqual(memory_module._HEADER, manager.read())

    def test_append_syncs_and_surfaces_persistence_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MEMORY.md"
            manager = MemoryManager(str(path))

            with patch.object(memory_module.os, "fsync") as fsync:
                manager.append_note("synced note")
            fsync.assert_called_once()
            self.assertIn("synced note", manager.read())

            before = path.read_bytes()
            with patch.object(Path, "open", side_effect=PermissionError("read only")):
                with self.assertRaisesRegex(PermissionError, "read only"):
                    manager.append_note("cannot persist")
            self.assertEqual(before, path.read_bytes())

    def test_append_recreates_a_deleted_memory_file_with_its_header(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "MEMORY.md"
            manager = MemoryManager(str(path))
            path.unlink()

            manager.append_note("after recreation")

            content = manager.read()
            self.assertTrue(content.startswith(memory_module._HEADER))
            self.assertIn("after recreation", content)


if __name__ == "__main__":
    unittest.main()
