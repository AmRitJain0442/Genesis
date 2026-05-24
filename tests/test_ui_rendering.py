from __future__ import annotations

import unittest

from rich.console import Console

from genesis.repl import _help_renderable
from genesis.schemas.plan import Plan
from genesis.ui.dashboard import DashboardState, make_layout
from genesis.ui.theme import command_table, progress_bar, status_label, trim


class UIRenderingTests(unittest.TestCase):
    def test_theme_helpers_are_ascii_safe_and_compact(self) -> None:
        self.assertEqual("[###.......]", progress_bar(3, 10, width=10))
        self.assertEqual("abcdef...", trim("abcdefghijklmnopqrstuvwxyz", 9))
        self.assertIn("RUN", status_label("running"))

    def test_dashboard_layout_renders_with_team_metadata(self) -> None:
        state = DashboardState(agent_names=["claude-cli-orchestrator", "codex-main"])
        state.task_name = "build a command center dashboard"
        state.run_phase = "running"
        state.plan = Plan(
            task_id="run-ui",
            task_summary="render ui",
            estimated_steps=1,
            steps=[
                {
                    "step_id": "step-1",
                    "title": "Render dashboard",
                    "description": "Update the live dashboard.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "file_scope": ["genesis/ui/dashboard.py"],
                    "expected_output": "Dashboard renders.",
                    "context_hint": "genesis/ui/dashboard.py",
                }
            ],
        )
        state.total = 1
        state.current_step = "step-1"
        state.current_worker = "codex-main"
        state.step_statuses["step-1"] = "running"
        state.step_workers["step-1"] = "codex-main"
        state.step_scopes["step-1"] = "genesis/ui/dashboard.py"
        state.add_event("lease", "codex-main -> step-1", "green")
        state.add_output("verify $ pytest -q")

        console = Console(record=True, width=140, height=40)
        console.print(make_layout(state))
        rendered = console.export_text()

        self.assertIn("GENESIS COMMAND CENTER", rendered)
        self.assertIn("Render dashboard", rendered)
        self.assertIn("codex-main", rendered)

    def test_dashboard_escapes_raw_agent_output(self) -> None:
        state = DashboardState()
        state.add_output("shell printed [literal] brackets")

        console = Console(record=True, width=100, height=20)
        console.print(make_layout(state))

        self.assertIn("[literal]", console.export_text())

    def test_command_table_renders(self) -> None:
        tbl = command_table("Recent Runs")
        tbl.add_column("Run")
        tbl.add_column("Status")
        tbl.add_row("run-a", status_label("completed"))

        console = Console(record=True, width=80)
        console.print(tbl)
        self.assertIn("Recent Runs", console.export_text())

    def test_help_renders_rich_tables_without_literal_markup(self) -> None:
        console = Console(record=True, width=120)
        console.print(_help_renderable())
        rendered = console.export_text()

        self.assertIn("run <task>", rendered)
        self.assertIn("remove-all-accounts", rendered)
        self.assertNotIn("[bold", rendered)
        self.assertNotIn("[/bold", rendered)


if __name__ == "__main__":
    unittest.main()
