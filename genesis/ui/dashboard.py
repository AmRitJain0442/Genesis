from __future__ import annotations
from datetime import datetime
from typing import TYPE_CHECKING

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan, Step
    from genesis.schemas.review import Review
    from genesis.agents.worker import WorkerResult

_STEP_ICONS = {
    "pending":        "[dim][ ][/dim]",
    "running":        "[bold cyan][→][/bold cyan]",
    "approved":       "[green][✓][/green]",
    "needs_revision": "[yellow][⚠][/yellow]",
    "rejected":       "[red][✗][/red]",
}


class DashboardState:
    """Mutable state shared between the orchestration callbacks and the Live display."""

    def __init__(self) -> None:
        self.task_name: str = ""
        self.plan: Plan | None = None
        self.step_statuses: dict[str, str] = {}   # step_id → status string
        self.current_step: str = ""
        self.output_lines: list[str] = []
        self.completed: int = 0
        self.total: int = 0
        self.git_sha: str = "—"
        self.start_time: datetime = datetime.now()

    def add_output(self, line: str) -> None:
        self.output_lines.append(line)
        if len(self.output_lines) > 40:
            self.output_lines = self.output_lines[-40:]


def _header(state: DashboardState) -> Text:
    elapsed = int((datetime.now() - state.start_time).total_seconds())
    t = Text()
    t.append("  GENESIS  ", style="bold white on dark_magenta")
    t.append(f"  {state.task_name or 'No active task'}", style="bold cyan")
    t.append(f"  [{elapsed}s]", style="dim")
    return t


def _plan_panel(state: DashboardState) -> Panel:
    if state.plan is None:
        return Panel(
            "[dim]Waiting for plan…[/dim]",
            title="[bold]PLAN[/bold]",
            border_style="blue",
        )

    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=True)
    tbl.add_column("St", width=5)
    tbl.add_column("Title")

    for step in state.plan.steps:
        status = state.step_statuses.get(step.step_id, "pending")
        icon = _STEP_ICONS.get(status, "[ ]")
        is_current = step.step_id == state.current_step
        style = (
            "green" if status == "approved"
            else "bold cyan" if is_current
            else "red" if status == "rejected"
            else "yellow" if status == "needs_revision"
            else "dim"
        )
        tbl.add_row(icon, Text(f"{step.step_id}: {step.title}", style=style))

    subtitle = f"{state.completed}/{state.total} complete"
    return Panel(tbl, title=f"[bold]PLAN[/bold]  [dim]{subtitle}[/dim]", border_style="blue")


def _output_panel(state: DashboardState) -> Panel:
    lines = state.output_lines[-22:] if state.output_lines else ["[dim]Waiting…[/dim]"]
    content = "\n".join(lines)
    return Panel(content, title="[bold]AGENT OUTPUT[/bold]", border_style="green")


def _footer(state: DashboardState) -> Table:
    total = max(state.total, 1)
    filled = int((state.completed / total) * 24)
    bar = "█" * filled + "░" * (24 - filled)

    tbl = Table(box=None, show_header=False, padding=(0, 1))
    tbl.add_column()
    tbl.add_column()
    tbl.add_column(justify="right")
    tbl.add_row(
        Text(f"[{bar}]", style="cyan"),
        Text(f"{state.completed}/{state.total} steps", style="bold"),
        Text(f"git: {state.git_sha}", style="dim"),
    )
    return tbl


def make_layout(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(_header(state), name="header", size=1),
        Layout(name="body", ratio=1),
        Layout(_footer(state), name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(_plan_panel(state), name="plan", ratio=2),
        Layout(_output_panel(state), name="output", ratio=3),
    )
    return layout
