from __future__ import annotations

import io
import threading
import unittest

from rich.console import Console

from genesis.repl import _help_renderable, _prompt_renderable
from genesis.schemas.plan import Plan
from genesis.ui.banner import render_banner
from genesis.ui.dashboard import DashboardState, DashboardView, make_layout
from genesis.ui.theme import command_table, progress_bar, status_label, trim


def _render(renderable, *, width: int, height: int = 26) -> str:
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=width,
        height=height,
        color_system=None,
        force_terminal=False,
    )
    console.print(renderable, height=height)
    return stream.getvalue()


def _dashboard_state() -> DashboardState:
    state = DashboardState(
        agent_names=["claude-cli-orchestrator", "codex-main", "codex-reserve"]
    )
    state.task_name = "Render responsive mission dashboard"
    state.run_phase = "running"
    state.plan = Plan(
        task_id="run-ui",
        task_summary="render ui",
        estimated_steps=4,
        steps=[
            {
                "step_id": f"step-{number}",
                "title": title,
                "description": "Update the live dashboard.",
                "type": "code",
                "preferred_agent": "codex-main",
                "depends_on": [f"step-{number - 1}"] if number > 1 else [],
                "file_scope": [f"genesis/ui/{scope}"],
                "expected_output": "Dashboard renders.",
                "context_hint": f"genesis/ui/{scope}",
            }
            for number, title, scope in [
                (1, "Define the signal palette", "theme.py"),
                (2, "Render dashboard", "dashboard.py"),
                (3, "Add responsive preview", "_preview.py"),
                (4, "Lock rendering tests", "test_ui.py"),
            ]
        ],
    )
    state.total = 4
    state.completed = 1
    state.current_step = "step-2"
    state.current_worker = "codex-main"
    state.active_steps["step-2"] = "codex-main"
    state.step_statuses.update(
        {"step-1": "committed", "step-2": "running", "step-3": "pending", "step-4": "pending"}
    )
    state.step_workers["step-2"] = "codex-main"
    state.step_scopes["step-2"] = "genesis/ui/dashboard.py"
    state.step_verification["step-1"] = "passed"
    state.git_sha = "abc1234"
    state.chat_url = "http://127.0.0.1:8765/"
    state.add_event("lease", "codex-main -> step-2", "green")
    state.add_output("verify $ pytest -q")
    state.record_token_line("Tokens: in=12000 out=640 | $0.0200")
    return state


class UIRenderingTests(unittest.TestCase):
    def test_theme_helpers_are_ascii_safe_and_compact(self) -> None:
        self.assertEqual("[###.......]", progress_bar(3, 10, width=10))
        self.assertEqual("abcdef...", trim("abcdefghijklmnopqrstuvwxyz", 9))
        self.assertIn("RUN", status_label("running"))
        self.assertNotIn("magenta", status_label("reviewing"))

    def test_dashboard_uses_readable_width_profiles(self) -> None:
        expectations = {
            72: ("RUN QUEUE", False),
            88: ("RUN QUEUE", False),
            110: ("EXECUTION PLAN", False),
            140: ("SYSTEM SIGNAL", True),
            160: ("SYSTEM SIGNAL", True),
        }
        for width, (profile_marker, has_signal_rail) in expectations.items():
            with self.subTest(width=width):
                rendered = _render(
                    make_layout(_dashboard_state(), width=width, height=26),
                    width=width,
                    height=26,
                )
                self.assertIn("GENESIS", rendered)
                self.assertIn(profile_marker, rendered)
                self.assertIn("LIVE FEED", rendered)
                self.assertIn("Render dashboard", rendered)
                self.assertIn("verify $ pytest -q", rendered)
                self.assertEqual(has_signal_rail, "TEAM / ASSIGNMENTS" in rendered)
                self.assertLessEqual(max(map(len, rendered.splitlines())), width)

    def test_short_dashboard_preserves_live_state_and_output(self) -> None:
        rendered = _render(
            make_layout(_dashboard_state(), width=140, height=14),
            width=140,
            height=14,
        )
        self.assertIn("GENESIS", rendered)
        self.assertIn("step-2@codex-main", rendered)
        self.assertIn("LIVE FEED", rendered)
        self.assertIn("verify $ pytest -q", rendered)
        self.assertNotIn("EXECUTION PLAN", rendered)

    def test_wide_dashboard_surfaces_parallel_workers_and_chat_endpoint(self) -> None:
        state = _dashboard_state()
        state.active_steps["step-3"] = "codex-reserve"
        state.step_statuses["step-3"] = "running"
        rendered = _render(DashboardView(state), width=160, height=30)

        self.assertIn("2 workers", rendered)
        self.assertIn("codex-main", rendered)
        self.assertIn("codex-reserve", rendered)
        self.assertIn("127.0.0.1:8765", rendered)

    def test_dashboard_view_rebuilds_after_streaming_state_changes(self) -> None:
        state = _dashboard_state()
        view = DashboardView(state)
        before = _render(view, width=110, height=24)
        self.assertNotIn("streamed marker", before)

        state.add_output("streamed marker")
        after = _render(view, width=110, height=24)
        self.assertIn("streamed marker", after)

    def test_dashboard_snapshot_is_safe_during_callback_mutation(self) -> None:
        state = _dashboard_state()
        started = threading.Event()
        failures: list[BaseException] = []

        def mutate() -> None:
            try:
                started.set()
                for index in range(500):
                    step_id = f"parallel-{index % 4}"
                    state.step_statuses[step_id] = "running"
                    state.active_steps[step_id] = f"worker-{index % 3}"
                    state.add_output(f"line {index}")
                    if index % 3 == 0:
                        state.active_steps.pop(step_id, None)
            except BaseException as exc:  # pragma: no cover - assertion captures thread failures
                failures.append(exc)

        writer = threading.Thread(target=mutate)
        writer.start()
        started.wait(timeout=1)
        for _ in range(30):
            snapshot = state.snapshot()
            _render(make_layout(snapshot, width=88, height=20), width=88, height=20)
        writer.join(timeout=2)

        self.assertFalse(writer.is_alive())
        self.assertEqual([], failures)
        self.assertLessEqual(len(state.snapshot().output_lines), 240)

    def test_dashboard_escapes_raw_agent_output(self) -> None:
        state = DashboardState()
        state.add_output("shell printed [literal] brackets")
        rendered = _render(make_layout(state, width=100, height=20), width=100, height=20)
        self.assertIn("[literal]", rendered)

    def test_banner_switches_to_compact_plate_before_it_wraps(self) -> None:
        for width in (72, 88, 110):
            with self.subTest(width=width):
                stream = io.StringIO()
                console = Console(file=stream, width=width, color_system=None)
                render_banner(
                    console,
                    version="0.1.0",
                    systems=[("Claude Code", True), ("Codex", False)],
                    info=[("cwd", "genesis"), ("agents", "3"), ("parallel", "2")],
                    commands="run <task> / runs / inspect <id> / status / help / exit",
                )
                rendered = stream.getvalue()
                self.assertIn("GENESIS", rendered)
                self.assertLessEqual(max(map(len, rendered.splitlines())), width)
                if width < 96:
                    self.assertIn("CONTROL PLANE", rendered)
                    self.assertLessEqual(len(rendered.splitlines()), 8)
                else:
                    self.assertIn("AUTONOMOUS SOFTWARE OPERATIONS", rendered)

    def test_command_table_and_contextual_prompt_render(self) -> None:
        table = command_table("Recent Runs")
        table.add_column("Run")
        table.add_column("Status")
        table.add_row("run-a", status_label("completed"))

        rendered_table = _render(table, width=80, height=10)
        rendered_prompt = _render(_prompt_renderable("genesis", "agent/ui"), width=80, height=4)
        self.assertIn("RECENT RUNS", rendered_table)
        self.assertIn("genesis", rendered_prompt)
        self.assertIn("agent/ui", rendered_prompt)

    def test_help_renders_rich_tables_without_literal_markup(self) -> None:
        rendered = _render(_help_renderable(), width=120, height=80)
        self.assertIn("run <task>", rendered)
        self.assertIn("remove-all-accounts", rendered)
        self.assertNotIn("[bold", rendered)
        self.assertNotIn("[/bold", rendered)


if __name__ == "__main__":
    unittest.main()
