from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from genesis.ui.theme import command_panel, markup, progress_bar, role_label, status_label, trim

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan


_SPINNER = ["|", "/", "-", "\\"]
_TOKEN_RE_CLAUDE = re.compile(r"Tokens:\s*in=(\d+)\s+out=(\d+)\s*\|\s*\$([0-9.]+)")
_TOKEN_RE_CODEX = re.compile(r"Tokens:\s*in=(\d+)\s+\(cached=(\d+)\)\s+out=(\d+)")
_RICH_TAG_RE = re.compile(r"\[/?[^\]]*\]")


class UsageStats:
    __slots__ = ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.cost_usd = 0.0

    def absorb_line(self, line: str) -> None:
        plain = _RICH_TAG_RE.sub("", line)
        if m := _TOKEN_RE_CLAUDE.search(plain):
            self.input_tokens += int(m.group(1))
            self.output_tokens += int(m.group(2))
            self.cost_usd += float(m.group(3))
            return
        if m := _TOKEN_RE_CODEX.search(plain):
            self.input_tokens += int(m.group(1))
            self.cached_tokens += int(m.group(2))
            self.output_tokens += int(m.group(3))


class DashboardState:
    """Mutable state shared between orchestration callbacks and the Live display."""

    def __init__(self, agent_names: list[str] | None = None) -> None:
        self.task_name: str = ""
        self.run_phase: str = "idle"
        self.plan: Plan | None = None
        self.step_statuses: dict[str, str] = {}
        self.step_workers: dict[str, str] = {}
        self.step_scopes: dict[str, str] = {}
        self.step_repairs: dict[str, int] = {}
        self.step_reviewers: dict[str, str] = {}
        self.step_verification: dict[str, str] = {}
        self.current_step: str = ""
        self.current_worker: str = ""
        self.current_reviewer: str = ""
        self.blocked_reason: str = ""
        self.output_lines: list[str] = []
        self.recent_events: list[tuple[str, str, str]] = []
        self.completed: int = 0
        self.total: int = 0
        self.git_sha: str = "-"
        self.start_time: datetime = datetime.now()
        self.step_start: datetime | None = None
        self.step_elapsed: dict[str, float] = {}
        self.agent_names: list[str] = agent_names or []
        self.usage: dict[str, UsageStats] = {}

    def add_output(self, line: str, *, trusted_markup: bool = False) -> None:
        text = str(line)
        self.output_lines.append(text if trusted_markup else escape(text))
        if len(self.output_lines) > 240:
            self.output_lines = self.output_lines[-240:]

    def add_event(self, label: str, detail: str = "", style: str = "cyan") -> None:
        self.recent_events.append((label.upper()[:10], detail, style))
        if len(self.recent_events) > 80:
            self.recent_events = self.recent_events[-80:]

    def record_token_line(self, raw_line: str) -> None:
        key = self.current_worker or "__orch__"
        if key not in self.usage:
            self.usage[key] = UsageStats()
        self.usage[key].absorb_line(raw_line)

    def total_input(self) -> int:
        return sum(u.input_tokens for u in self.usage.values())

    def total_output(self) -> int:
        return sum(u.output_tokens for u in self.usage.values())

    def total_cached(self) -> int:
        return sum(u.cached_tokens for u in self.usage.values())

    def total_cost(self) -> float:
        return sum(u.cost_usd for u in self.usage.values())


def _spinner_frame() -> str:
    idx = int(datetime.now().timestamp() * 8) % len(_SPINNER)
    return _SPINNER[idx]


def _elapsed(start: datetime) -> str:
    return _fmt_duration((datetime.now() - start).total_seconds())


def _fmt_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,}"


def _step_scope(step, state: DashboardState) -> str:
    if step.step_id in state.step_scopes:
        return state.step_scopes[step.step_id]
    scope = getattr(step, "file_scope", []) or []
    return ", ".join(scope) if scope else getattr(step, "context_hint", "") or "*"


def _status_style(status: str) -> str:
    return {
        "running": "bold cyan",
        "approved": "green",
        "committed": "bold green",
        "completed": "bold green",
        "needs_revision": "yellow",
        "reviewing": "magenta",
        "verifying": "blue",
        "blocked": "red",
        "rejected": "red",
    }.get(status, "dim")


def _header(state: DashboardState) -> Panel:
    spin = _spinner_frame() if state.current_step and state.run_phase != "completed" else "-"
    task = trim(state.task_name or "No active task", 86)
    active = state.current_worker or state.current_reviewer or "-"
    step = state.current_step or "-"
    elapsed = _elapsed(state.start_time)
    bar = progress_bar(state.completed, state.total, width=30)

    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(justify="right", ratio=2)
    grid.add_row(
        f"[bold cyan]{spin} GENESIS COMMAND CENTER[/bold cyan] {status_label(state.run_phase, width=8)} [bold]{markup(task)}[/bold]",
        f"[dim]elapsed[/dim] [bold]{elapsed}[/bold]  [dim]git[/dim] [cyan]{markup(state.git_sha)}[/cyan]",
    )
    grid.add_row(
        f"[cyan]{bar}[/cyan] [bold]{state.completed}/{state.total}[/bold] [dim]steps[/dim]",
        f"[dim]active[/dim] [bold cyan]{markup(active, 28)}[/bold cyan]  [dim]step[/dim] [yellow]{markup(step, 18)}[/yellow]",
    )
    return Panel(grid, border_style="cyan", box=box.HORIZONTALS, padding=(0, 1))


def _plan_panel(state: DashboardState) -> Panel:
    if state.plan is None:
        return command_panel(Text("Waiting for planner output...", style="dim"), "EXECUTION PLAN")

    tbl = Table(box=None, show_header=True, header_style="bold cyan", expand=True, pad_edge=False)
    tbl.add_column("State", width=9, no_wrap=True)
    tbl.add_column("Step", width=7, no_wrap=True)
    tbl.add_column("Fx", width=3, justify="right")
    tbl.add_column("Title / Scope", overflow="fold")

    for step in state.plan.steps:
        status = state.step_statuses.get(step.step_id, "pending")
        repairs = state.step_repairs.get(step.step_id, 0)
        title = Text(trim(step.title, 40), style=_status_style(status))
        title.append("\n")
        title.append(trim(_step_scope(step, state), 40), style="dim")
        tbl.add_row(
            status_label(status),
            markup(step.step_id, 7),
            str(repairs) if repairs else "",
            title,
        )

    subtitle = f"{state.completed}/{state.total} done"
    return command_panel(tbl, "EXECUTION PLAN", border_style="blue", subtitle=subtitle)


def _style_output_line(line: str) -> str:
    plain = _RICH_TAG_RE.sub("", line).strip()
    if plain.startswith("$") or plain.startswith("verify $"):
        return f"[bold yellow]{line}[/bold yellow]"
    if plain.startswith("+") or " file_change" in plain:
        return f"[green]{line}[/green]"
    if "review:" in plain or "approved" in plain:
        return f"[cyan]{line}[/cyan]"
    if "Retrying" in plain or "Repairing" in plain or "needs_revision" in plain:
        return f"[yellow]{line}[/yellow]"
    if "exit " in plain or "failed" in plain.lower() or plain.startswith("x "):
        return f"[red]{line}[/red]"
    return line


def _output_panel(state: DashboardState) -> Panel:
    lines = state.output_lines[-34:] if state.output_lines else ["[dim]No agent output yet.[/dim]"]
    content = "\n".join(_style_output_line(line) for line in lines)
    title = "AGENT OUTPUT"
    subtitle = state.current_worker or state.current_reviewer
    return command_panel(content, title, border_style="green", subtitle=subtitle)


def _team_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=True, header_style="bold magenta", expand=True, pad_edge=False)
    tbl.add_column("Role", width=7, no_wrap=True)
    tbl.add_column("Agent", overflow="ellipsis")
    tbl.add_column("State", width=9, no_wrap=True)

    for name in state.agent_names:
        role = "orchestrator" if "orchestrator" in name else "worker"
        active = name == state.current_worker or name == state.current_reviewer
        state_text = "[bold green]ACTIVE[/bold green]" if active else "[dim]READY[/dim]"
        tbl.add_row(role_label(role), markup(name, 28), state_text)

    if not state.agent_names:
        tbl.add_row(role_label("worker"), "[dim]none[/dim]", status_label("blocked"))

    return command_panel(tbl, "TEAM", border_style="magenta")


def _events_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=False, expand=True, pad_edge=False)
    tbl.add_column("Kind", width=10, no_wrap=True)
    tbl.add_column("Detail", overflow="ellipsis")
    events = state.recent_events[-8:] if state.recent_events else [("WAIT", "No runtime events yet", "dim")]
    for label, detail, style in events:
        tbl.add_row(f"[{style}]{markup(label, 10)}[/]", markup(detail, 42))
    return command_panel(tbl, "EVENT TRACE", border_style="cyan")


def _metrics_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=False, padding=(0, 0), expand=True, pad_edge=False)
    tbl.add_column("Metric", style="dim", width=9)
    tbl.add_column("Value", justify="right")

    tbl.add_row("input", f"[cyan]{_fmt_tokens(state.total_input())}[/cyan]")
    tbl.add_row("output", f"[green]{_fmt_tokens(state.total_output())}[/green]")
    tbl.add_row("cached", f"[dim cyan]{_fmt_tokens(state.total_cached())}[/dim cyan]")
    cost = state.total_cost()
    tbl.add_row("cost", f"[bold yellow]${cost:.4f}[/bold yellow]" if cost else "[dim]$0.0000[/dim]")
    tbl.add_row("", "")

    for worker, usage in state.usage.items():
        label = "orch" if worker == "__orch__" else trim(worker, 9)
        style = "bold cyan" if worker == state.current_worker else "dim"
        tbl.add_row(
            f"[{style}]{markup(label, 9)}[/]",
            f"[{style}]{_fmt_tokens(usage.input_tokens)}/{_fmt_tokens(usage.output_tokens)}[/]",
        )

    return command_panel(tbl, "TELEMETRY", border_style="yellow")


def _verification_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=True, header_style="bold blue", expand=True, pad_edge=False)
    tbl.add_column("Step", width=8, no_wrap=True)
    tbl.add_column("Review", width=9, no_wrap=True)
    tbl.add_column("Verify", width=9, no_wrap=True)
    tbl.add_column("Reviewer", overflow="ellipsis")

    if state.plan:
        for step in state.plan.steps[-6:]:
            review = state.step_statuses.get(step.step_id, "pending")
            verify = state.step_verification.get(step.step_id, "")
            reviewer = state.step_reviewers.get(step.step_id, "")
            tbl.add_row(markup(step.step_id, 8), status_label(review), markup(verify, 9), markup(reviewer, 22))
    else:
        tbl.add_row("-", status_label("pending"), "-", "-")

    return command_panel(tbl, "QUALITY GATES", border_style="blue")


def _footer(state: DashboardState) -> Table:
    tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    tbl.add_column("progress", ratio=3)
    tbl.add_column("phase", ratio=1, justify="center")
    tbl.add_column("blocked", ratio=2)
    tbl.add_column("usage", ratio=1, justify="right")

    blocked = trim(state.blocked_reason, 64, placeholder="")
    usage = f"{_fmt_tokens(state.total_input())}/{_fmt_tokens(state.total_output())} tok"
    tbl.add_row(
        f"[cyan]{progress_bar(state.completed, state.total, width=34)}[/cyan]",
        status_label(state.run_phase, width=8),
        f"[red]{markup(blocked)}[/red]" if blocked else "[dim]no blockers[/dim]",
        f"[yellow]{usage}[/yellow]",
    )
    return tbl


def make_layout(state: DashboardState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )

    layout["header"].update(_header(state))
    layout["body"].split_row(
        Layout(name="plan", ratio=34),
        Layout(name="output", ratio=42),
        Layout(name="right", ratio=24),
    )
    layout["right"].split_column(
        Layout(name="team", ratio=26),
        Layout(name="quality", ratio=27),
        Layout(name="events", ratio=24),
        Layout(name="metrics", ratio=23),
    )

    layout["plan"].update(_plan_panel(state))
    layout["output"].update(_output_panel(state))
    layout["right"]["team"].update(_team_panel(state))
    layout["right"]["quality"].update(_verification_panel(state))
    layout["right"]["events"].update(_events_panel(state))
    layout["right"]["metrics"].update(_metrics_panel(state))
    layout["footer"].update(_footer(state))
    return layout
