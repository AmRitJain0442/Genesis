from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from genesis.palace import PalaceStore
from genesis.runtime import RuntimeStore


class PalaceRobustnessTests(unittest.TestCase):
    def test_noisy_natural_language_query_retrieves_relevant_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = PalaceStore(Path(td) / "state.db")
            expected = store.add_drawer(
                wing="repo",
                room="run-a",
                closet="auth",
                kind="decision",
                title="Authentication middleware",
                content="Use signed session cookies for the authentication middleware.",
            )

            hits = store.search(
                "Please fix the authentication bug in the new login route",
                wing="repo",
            )

            self.assertIn(expected, [hit.id for hit in hits])

    def test_identical_content_from_different_runs_keeps_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = PalaceStore(Path(td) / "state.db")
            ids = {
                store.add_drawer(
                    wing="repo",
                    room=run_id,
                    closet="outcomes",
                    kind="step-result",
                    title="Implemented cache",
                    content="Added a bounded local cache.",
                    run_id=run_id,
                    step_id="step-1",
                )
                for run_id in ("run-a", "run-b")
            }

            hits = store.search("bounded local cache", wing="repo")

            self.assertEqual(2, len(ids))
            self.assertEqual({"run-a", "run-b"}, {hit.room for hit in hits})

    def test_context_respects_small_and_zero_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = PalaceStore(Path(td) / "state.db")
            store.add_drawer(
                wing="repo",
                room="run",
                closet="notes",
                kind="note",
                title="A useful note",
                content="useful " * 200,
            )

            self.assertEqual("", store.wakeup_context("useful", max_chars=0))
            self.assertLessEqual(len(store.wakeup_context("useful", max_chars=73)), 73)

    def test_search_index_can_be_rebuilt_from_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            store = PalaceStore(path)
            expected = store.add_drawer(
                wing="repo",
                room="run",
                closet="notes",
                kind="note",
                title="Recovery note",
                content="The durable canary memory survives index loss.",
            )
            with closing(sqlite3.connect(path)) as con:
                con.execute("DROP TABLE palace_fts")
                con.commit()

            self.assertTrue(store.rebuild_search_index())
            self.assertEqual(expected, store.search("durable canary")[0].id)


class RuntimeRobustnessTests(unittest.TestCase):
    def test_duplicate_run_id_is_rejected_without_mixing_old_children(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStore(Path(td) / "state.db")
            store.start_run("original task", run_id="run-a")
            store.checkpoint("run-a", "plan_created", payload={"original": True})
            store.upsert_step("run-a", "step-1", title="Original", status="pending")
            before_events = store.events("run-a")

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.start_run("different task", run_id="run-a")

            self.assertEqual("original task", store.get_run("run-a").task)
            self.assertEqual({"original": True}, store.get_checkpoint("run-a", "plan_created"))
            self.assertEqual("Original", store.get_step("run-a", "step-1").title)
            self.assertEqual(before_events, store.events("run-a"))

    def test_state_and_event_roll_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStore(Path(td) / "state.db")
            store.start_run("task", run_id="run-a", metadata={"stable": True})
            before_events = store.events("run-a")

            with mock.patch.object(
                RuntimeStore,
                "_insert_event",
                side_effect=RuntimeError("simulated event failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated event failure"):
                    store.update_run_status(
                        "run-a",
                        "completed",
                        metadata={"should_not_persist": True},
                    )

            run = store.get_run("run-a")
            self.assertEqual("running", run.status)
            self.assertNotIn("should_not_persist", run.metadata)
            self.assertEqual(before_events, store.events("run-a"))

    def test_parallel_metadata_updates_do_not_get_lost(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.db"
            RuntimeStore(path).start_run("task", run_id="run-a")
            barrier = threading.Barrier(8)

            def update(index: int) -> None:
                local = RuntimeStore(path)
                barrier.wait()
                local.update_run_status(
                    "run-a",
                    "running",
                    metadata={f"worker_{index}": index},
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(update, range(8)))

            run = RuntimeStore(path).get_run("run-a")
            self.assertEqual(
                {f"worker_{index}": index for index in range(8)},
                {key: value for key, value in run.metadata.items() if key.startswith("worker_")},
            )

            with closing(sqlite3.connect(path)) as con:
                mode = con.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual("wal", mode.lower())

    def test_limited_event_trace_returns_the_newest_events_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RuntimeStore(Path(td) / "state.db")
            store.start_run("task", run_id="run-a")
            for index in range(10):
                store.record_event("run-a", "tick", payload={"index": index})

            events = store.events("run-a", limit=3)

            self.assertEqual([7, 8, 9], [event.payload["index"] for event in events])
            self.assertEqual(sorted(event.id for event in events), [event.id for event in events])


if __name__ == "__main__":
    unittest.main()
