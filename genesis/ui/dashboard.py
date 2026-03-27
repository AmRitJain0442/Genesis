from __future__ import annotations
import re
from datetime import datetime
from typing import TYPE_CHECKING

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan

_STEP_ICONS = {
    "pending":        ("[dim][ ][/dim]",        "dim"),
    "running":        ("[bold cyan][>][/bold cyan]", "bold cyan"),
    "approved":       ("[green][+][/green]",     "green"),
    "needs_revision": ("[yellow][~][~][/yellow]","yellow"),
    "rejected":       ("[red][x][/red]",         "red"),
}

_SPINNER = ["|", "/", "-", "\\"]

_TOKEN_RE_CLAUDE = re.compile(
    r"Tokens:\s*in=(\d+)\s+out=(\d+)\s*\|\s*\$([0-9.]+)"
)
_TOKEN_RE_CODEX = re.compile(
    r"Tokens:\s*in=(\d+)\s+\(cached=(\d+)\)\s+out=(\d+)"
)


class UsageStats:
    __slots__ = ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.cost_usd = 0.0

    def absorb_line(self, line: str) -> None:
        """Parse a streaming token line and add to accumulators."""
        plain = re.sub(r"\[/?[^\]]*\]", "", line)  # strip Rich markup
        m = _TOKEN_RE_CLAUDE.search(plain)
        if m:
            self.input_tokens  += int(m.group(1))
            self.output_tokens += int(m.group(2))
            self.cost_usd      += float(m.group(3))
            return
        m = _TOKEN_RE_CODEX.search(plain)
        if m:
            self.input_tokens  += int(m.group(1))
            self.cached_tokens += int(m.group(2))
            self.output_tokens += int(m.group(3))


class DashboardState:
    """Mutable state shared between orchestration callbacks and the Live display."""

    def __init__(self, agent_names: list[str] | None = None) -> None:
        self.task_name: str = ""
        self.plan: Plan | None = None
        self.step_statuses: dict[str, str] = {}
        self.current_step: str = ""
        self.current_worker: str = ""
        self.output_lines: list[str] = []
        self.completed: int = 0
        self.total: int = 0
        self.git_sha: str = "-"
        self.start_time: datetime = datetime.now()
        self.step_start: datetime | None = None
        self.step_elapsed: dict[str, float] = {}   # step_id -> seconds
        self.agent_names: list[str] = agent_names or []
        # per-agent usage (keyed by worker name)
        self.usage: dict[str, UsageStats] = {}

    def add_output(self, line: str) -> None:
        self.output_lines.append(line)
        if len(self.output_lines) > 200:
            self.output_lines = self.output_lines[-200:]

    def record_token_line(self, raw_line: str) -> None:
        key = self.current_worker or "__orch__"
        if key not in self.usage:
            self.usage[key] = UsageStats()
        self.usage[key].absorb_line(raw_line)

    # ── Aggregate helpers ───────────────────────────────────────────────

    def total_input(self) -> int:
        return sum(u.input_tokens for u in self.usage.values())

    def total_output(self) -> int:
        return sum(u.output_tokens for u in self.usage.values())

    def total_cached(self) -> int:
        return sum(u.cached_tokens for u in self.usage.values())

    def total_cost(self) -> float:
        return sum(u.cost_usd for u in self.usage.values())


# ── Rendering helpers ───────────────────────────────────────────────────────

def _spinner_frame() -> str:
    idx = int(datetime.now().timestamp() * 6) % len(_SPINNER)
    return _SPINNER[idx]


def _elapsed(start: datetime) -> str:
    s = int((datetime.now() - start).total_seconds())
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _fmt_tokens(n: int) -> str:
    return f"{n:,}" if n < 1_000_000 else f"{n/1_000_000:.1f}M"


# ── Header ──────────────────────────────────────────────────────────────────

def _header(state: DashboardState) -> Panel:
    spin = _spinner_frame() if state.current_step else "-"
    step_info = ""
    if state.current_step and state.total:
        step_info = f"  step {state.completed + 1}/{state.total}"
        if state.step_start:
            step_info += f" [{_elapsed(state.step_start)}]"

    t = Text()
    t.append(f" {spin} ", style="bold cyan")
    t.append("GENESIS", style="bold white on dark_magenta")
    t.append("  ", style="")
    task_display = state.task_name or "No active task"
    t.append(task_display[:80], style="bold")
    if state.current_worker:
        t.append(f"  [worker: {state.current_worker}]", style="dim cyan")
    t.append(step_info, style="bold yellow")
    elapsed = _elapsed(state.start_time)
    t.append(f"  {elapsed}", style="dim")

    return Panel(t, style="on grey7", padding=(0, 1), box=box.HORIZONTALS)


# ── Plan panel ──────────────────────────────────────────────────────────────

def _plan_panel(state: DashboardState) -> Panel:
    if state.plan is None:
        inner = Text("Waiting for plan...", style="dim")
        return Panel(inner, title="[bold]PLAN[/bold]", border_style="blue",
                     box=box.ROUNDED)

    tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True,
                show_edge=False)
    tbl.add_column("ic", width=4, no_wrap=True)
    tbl.add_column("id", width=7, no_wrap=True)
    tbl.add_column("title")
    tbl.add_column("t", width=6, no_wrap=True, justify="right")

    for step in state.plan.steps:
        status = state.step_statuses.get(step.step_id, "pending")
        icon_markup, row_style = _STEP_ICONS.get(status, ("[dim][ ][/dim]", "dim"))
        elapsed_str = ""
        if step.step_id in state.step_elapsed:
            sec = state.step_elapsed[step.step_id]
            elapsed_str = f"{int(sec)}s"
        elif status == "running" and state.step_start:
            sec = (datetime.now() - state.step_start).total_seconds()
            elapsed_str = f"[cyan]{int(sec)}s[/cyan]"

        tbl.add_row(
            icon_markup,
            Text(step.step_id, style="dim"),
            Text(step.title[:34], style=row_style),
            elapsed_str,
        )

    done = state.completed
    total = state.total
    subtitle = f"[dim]{done}/{total} done[/dim]"
    return Panel(tbl, title=f"[bold]PLAN[/bold]  {subtitle}",
                 border_style="blue", box=box.ROUNDED)


# ── Output panel ─────────────────────────────────────────────────────────────

def _output_panel(state: DashboardState) -> Panel:
    lines = state.output_lines[-30:] if state.output_lines else ["[dim]Waiting...[/dim]"]
    content = "\n".join(lines)
    title = "[bold]AGENT OUTPUT[/bold]"
    if state.current_worker:
        title += f"  [dim cyan]{state.current_worker}[/dim cyan]"
    return Panel(content, title=title, border_style="green", box=box.ROUNDED)


# ── Agents panel ─────────────────────────────────────────────────────────────

def _agents_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True,
                show_edge=False)
    tbl.add_column("dot", width=3, no_wrap=True)
    tbl.add_column("name")

    for name in state.agent_names:
        is_active = name == state.current_worker
        if is_active:
            dot = "[bold green]>[/bold green]"
            style = "bold green"
        else:
            dot = "[dim]o[/dim]"
            style = "dim"
        tbl.add_row(dot, Text(name[:20], style=style))

    if not state.agent_names:
        tbl.add_row("[dim]o[/dim]", Text("none", style="dim"))

    return Panel(tbl, title="[bold]AGENTS[/bold]", border_style="magenta",
                 box=box.ROUNDED)


# ── Metrics panel ────────────────────────────────────────────────────────────

def _metrics_panel(state: DashboardState) -> Panel:
    tbl = Table(box=None, show_header=False, padding=(0, 0), expand=True,
                show_edge=False)
    tbl.add_column("k", style="dim", width=8)
    tbl.add_column("v", justify="right")

    tot_in  = state.total_input()
    tot_out = state.total_output()
    tot_cac = state.total_cached()
    cost    = state.total_cost()

    tbl.add_row("in",     Text(_fmt_tokens(tot_in),  style="cyan"))
    tbl.add_row("out",    Text(_fmt_tokens(tot_out), style="green"))
    if tot_cac:
        tbl.add_row("cached", Text(_fmt_tokens(tot_cac), style="dim cyan"))
    tbl.add_row("cost",   Text(f"${cost:.4f}", style="bold yellow" if cost > 0 else "dim"))
    tbl.add_row("", "")

    # Per-worker breakdown
    for worker, u in state.usage.items():
        if worker == "__orch__":
            label = "orch"
        else:
            label = worker[:7]
        is_active = worker == state.current_worker
        style = "bold cyan" if is_active else "dim"
        tbl.add_row(
            Text(label, style=style),
            Text(f"{_fmt_tokens(u.input_tokens)}/{_fmt_tokens(u.output_tokens)}",
                 style=style),
        )

    return Panel(tbl, title="[bold]USAGE[/bold]", border_style="yellow",
                 box=box.ROUNDED)


# ── Footer ───────────────────────────────────────────────────────────────────

def _footer(state: DashboardState) -> Table:
    total = max(state.total, 1)
    filled = int((state.completed / total) * 28)
    bar = "#" * filled + "." * (28 - filled)

    cost = state.total_cost()
    cost_str = f"${cost:.4f}" if cost > 0 else ""

    tbl = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    tbl.add_column("bar", ratio=3)
    tbl.add_column("steps", ratio=1, justify="center")
    tbl.add_column("git", ratio=1, justify="center")
    tbl.add_column("cost", ratio=1, justify="right")
    tbl.add_row(
        Text(f"[{bar}]", style="cyan"),
        Text(f"{state.completed}/{state.total} steps", style="bold"),
        Text(f"git:{state.git_sha}", style="dim"),
        Text(cost_str, style="yellow"),
    )
    return tbl


# ── Top-level layout builder ─────────────────────────────────────────────────

def make_layout(state: DashboardState) -> Layout:
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )

    layout["header"].update(_header(state))

    layout["body"].split_row(
        Layout(name="plan",   ratio=30),
        Layout(name="output", ratio=47),
        Layout(name="right",  ratio=23),
    )

    layout["plan"].update(_plan_panel(state))
    layout["output"].update(_output_panel(state))

    layout["right"].split_column(
        Layout(name="agents",  ratio=45),
        Layout(name="metrics", ratio=55),
    )
    layout["right"]["agents"].update(_agents_panel(state))
    layout["right"]["metrics"].update(_metrics_panel(state))

    layout["footer"].update(_footer(state))

    return layout
