from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from genesis.account_usage import TokenUsageEvent, parse_token_usage_line
from genesis.ui.theme import (
    SIGNAL_AMBER,
    SIGNAL_CYAN,
    SIGNAL_GREEN,
    SIGNAL_RED,
    STEEL,
    command_panel,
    markup,
    palette_style,
    progress_bar,
    role_label,
    status_label,
    trim,
)

if TYPE_CHECKING:
    from genesis.schemas.plan import Plan


_SPINNER = ["|", "/", "-", "\\"]
_RICH_TAG_RE = re.compile(r"\[/?[^\]]*\]")


class UsageStats:
    __slots__ = ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.cost_usd = 0.0

    def absorb_line(self, line: str) -> TokenUsageEvent | None:
        event = parse_token_usage_line(line)
        if event is None:
            return None
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        self.cached_tokens += event.cached_tokens
        self.cost_usd += event.cost_usd
        return event

    def copy(self) -> UsageStats:
        clone = UsageStats()
        clone.input_tokens = self.input_tokens
        clone.output_tokens = self.output_tokens
        clone.cached_tokens = self.cached_tokens
        clone.cost_usd = self.cost_usd
        return clone


class _LockedDict(dict):
    """Small lock-aware dict used by callback-mutated dashboard collections."""

    def __init__(self, lock: RLock, values=None) -> None:
        self._lock = lock
        dict.__init__(self, values or {})

    def __setitem__(self, key, value) -> None:
        with self._lock:
            dict.__setitem__(self, key, value)

    def __delitem__(self, key) -> None:
        with self._lock:
            dict.__delitem__(self, key)

    def clear(self) -> None:
        with self._lock:
            dict.clear(self)

    def pop(self, key, *default):
        with self._lock:
            return dict.pop(self, key, *default)

    def popitem(self):
        with self._lock:
            return dict.popitem(self)

    def setdefault(self, key, default=None):
        with self._lock:
            return dict.setdefault(self, key, default)

    def update(self, *args, **kwargs) -> None:
        with self._lock:
            dict.update(self, *args, **kwargs)

    def __ior__(self, other):
        self.update(other)
        return self

    def snapshot(self) -> dict:
        with self._lock:
            return {key: dict.__getitem__(self, key) for key in dict.keys(self)}


class _LockedList(list):
    """List counterpart used for the rolling output and event buffers."""

    def __init__(self, lock: RLock, values=None) -> None:
        self._lock = lock
        list.__init__(self, values or [])

    def append(self, value) -> None:
        with self._lock:
            list.append(self, value)

    def extend(self, values) -> None:
        with self._lock:
            list.extend(self, values)

    def insert(self, index, value) -> None:
        with self._lock:
            list.insert(self, index, value)

    def clear(self) -> None:
        with self._lock:
            list.clear(self)

    def pop(self, index=-1):
        with self._lock:
            return list.pop(self, index)

    def remove(self, value) -> None:
        with self._lock:
            list.remove(self, value)

    def __setitem__(self, index, value) -> None:
        with self._lock:
            list.__setitem__(self, index, value)

    def __delitem__(self, index) -> None:
        with self._lock:
            list.__delitem__(self, index)

    def __iadd__(self, values):
        self.extend(values)
        return self

    def snapshot(self) -> list:
        with self._lock:
            return list(self)


@dataclass(frozen=True)
class DashboardSnapshot:
    task_name: str
    run_phase: str
    plan: Plan | None
    step_statuses: dict[str, str]
    step_workers: dict[str, str]
    step_scopes: dict[str, str]
    step_repairs: dict[str, int]
    step_reviewers: dict[str, str]
    step_verification: dict[str, str]
    active_steps: dict[str, str]
    current_step: str
    current_worker: str
    current_reviewer: str
    blocked_reason: str
    output_lines: tuple[str, ...]
    recent_events: tuple[tuple[str, str, str], ...]
    completed: int
    total: int
    git_sha: str
    start_time: datetime
    step_start: datetime | None
    step_elapsed: dict[str, float]
    agent_names: tuple[str, ...]
    usage: dict[str, UsageStats]
    chat_url: str

    def total_input(self) -> int:
        return sum(item.input_tokens for item in self.usage.values())

    def total_output(self) -> int:
        return sum(item.output_tokens for item in self.usage.values())

    def total_cached(self) -> int:
        return sum(item.cached_tokens for item in self.usage.values())

    def total_cost(self) -> float:
        return sum(item.cost_usd for item in self.usage.values())


class DashboardState:
    """Mutable callback state with atomic snapshots for Rich's render thread.

    Existing callers may keep assigning scalar attributes and mutating the
    public dictionaries directly.  Those containers are transparently wrapped
    so :meth:`snapshot` never iterates a concurrently-changing collection.
    """

    _DICT_FIELDS = {
        "step_statuses",
        "step_workers",
        "step_scopes",
        "step_repairs",
        "step_reviewers",
        "step_verification",
        "active_steps",
        "step_elapsed",
        "usage",
    }
    _LIST_FIELDS = {"output_lines", "recent_events", "agent_names"}

    def __init__(self, agent_names: list[str] | None = None) -> None:
        object.__setattr__(self, "_lock", RLock())
        self.task_name: str = ""
        self.run_phase: str = "idle"
        self.plan: Plan | None = None
        self.step_statuses: dict[str, str] = {}
        self.step_workers: dict[str, str] = {}
        self.step_scopes: dict[str, str] = {}
        self.step_repairs: dict[str, int] = {}
        self.step_reviewers: dict[str, str] = {}
        self.step_verification: dict[str, str] = {}
        self.active_steps: dict[str, str] = {}
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
        self.chat_url: str = ""

    def __setattr__(self, name: str, value) -> None:
        if name == "_lock":
            object.__setattr__(self, name, value)
            return
        lock = object.__getattribute__(self, "_lock")
        with lock:
            if name in self._DICT_FIELDS and not isinstance(value, _LockedDict):
                value = _LockedDict(lock, value)
            elif name in self._LIST_FIELDS and not isinstance(value, _LockedList):
                value = _LockedList(lock, value)
            object.__setattr__(self, name, value)

    def add_output(self, line: str, *, trusted_markup: bool = False) -> None:
        text = str(line)
        with self._lock:
            self.output_lines.append(text if trusted_markup else escape(text))
            if len(self.output_lines) > 240:
                del self.output_lines[:-240]

    def add_event(self, label: str, detail: str = "", style: str = SIGNAL_CYAN) -> None:
        with self._lock:
            self.recent_events.append((label.upper()[:10], detail, palette_style(style)))
            if len(self.recent_events) > 80:
                del self.recent_events[:-80]

    def record_token_line(
        self,
        raw_line: str,
        *,
        worker_name: str | None = None,
    ) -> TokenUsageEvent | None:
        with self._lock:
            key = worker_name or self.current_worker or "__orch__"
            if key not in self.usage:
                self.usage[key] = UsageStats()
            return self.usage[key].absorb_line(raw_line)

    def total_input(self) -> int:
        return self.snapshot().total_input()

    def total_output(self) -> int:
        return self.snapshot().total_output()

    def total_cached(self) -> int:
        return self.snapshot().total_cached()

    def total_cost(self) -> float:
        return self.snapshot().total_cost()

    def snapshot(self) -> DashboardSnapshot:
        with self._lock:
            def copy_dict(name: str) -> dict:
                value = object.__getattribute__(self, name)
                return value.snapshot() if isinstance(value, _LockedDict) else dict(value)

            def copy_list(name: str) -> list:
                value = object.__getattribute__(self, name)
                return value.snapshot() if isinstance(value, _LockedList) else list(value)

            usage = {
                name: stats.copy()
                for name, stats in copy_dict("usage").items()
            }
            return DashboardSnapshot(
                task_name=self.task_name,
                run_phase=self.run_phase,
                plan=self.plan,
                step_statuses=copy_dict("step_statuses"),
                step_workers=copy_dict("step_workers"),
                step_scopes=copy_dict("step_scopes"),
                step_repairs=copy_dict("step_repairs"),
                step_reviewers=copy_dict("step_reviewers"),
                step_verification=copy_dict("step_verification"),
                active_steps=copy_dict("active_steps"),
                current_step=self.current_step,
                current_worker=self.current_worker,
                current_reviewer=self.current_reviewer,
                blocked_reason=self.blocked_reason,
                output_lines=tuple(copy_list("output_lines")),
                recent_events=tuple(copy_list("recent_events")),
                completed=self.completed,
                total=self.total,
                git_sha=self.git_sha,
                start_time=self.start_time,
                step_start=self.step_start,
                step_elapsed=copy_dict("step_elapsed"),
                agent_names=tuple(copy_list("agent_names")),
                usage=usage,
                chat_url=self.chat_url,
            )


class DashboardView:
    """Dynamic Rich renderable for ``Live``.

    Rich refreshes this object on its own thread.  Each render takes one atomic
    snapshot and rebuilds the layout for the current render dimensions, so
    streaming output appears without a per-line ``live.update`` call.
    """

    def __init__(self, state: DashboardState) -> None:
        self.state = state

    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        width = max(40, options.max_width)
        height = options.height or options.max_height or console.size.height
        yield make_layout(self.state, width=width, height=max(8, height))


def _spinner_frame() -> str:
    index = int(datetime.now().timestamp() * 8) % len(_SPINNER)
    return _SPINNER[index]


def _elapsed(start: datetime) -> str:
    return _fmt_duration((datetime.now() - start).total_seconds())


def _fmt_duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


def _chat_endpoint(url: str) -> str:
    if not url:
        return ""
    candidate = url if "://" in url else f"//{url}"
    parsed = urlsplit(candidate)
    return parsed.netloc or trim(parsed.path, 32, placeholder="")


def _step_scope(step, state: DashboardSnapshot) -> str:
    if step.step_id in state.step_scopes:
        return state.step_scopes[step.step_id]
    scope = getattr(step, "file_scope", []) or []
    return ", ".join(scope) if scope else getattr(step, "context_hint", "") or "*"


def _status_style(status: str) -> str:
    return {
        "running": f"bold {SIGNAL_CYAN}",
        "approved": SIGNAL_GREEN,
        "committed": f"bold {SIGNAL_GREEN}",
        "completed": f"bold {SIGNAL_GREEN}",
        "needs_revision": SIGNAL_AMBER,
        "reviewing": SIGNAL_AMBER,
        "verifying": SIGNAL_CYAN,
        "repairing": SIGNAL_AMBER,
        "blocked": SIGNAL_RED,
        "rejected": SIGNAL_RED,
        "failed": SIGNAL_RED,
    }.get(status, f"dim {STEEL}")


def _header(state: DashboardSnapshot, *, width: int, compact: bool = False) -> Panel:
    assignments = state.active_steps or (
        {state.current_step: state.current_worker}
        if state.current_step and state.current_worker
        else {}
    )
    if len(assignments) > 1:
        active = f"{len(set(assignments.values()))} workers"
        step = "+".join(assignments)
    else:
        active = next(iter(assignments.values()), state.current_worker or state.current_reviewer or "standby")
        step = next(iter(assignments), state.current_step or "-")
    spin = _spinner_frame() if state.current_step and state.run_phase != "completed" else "-"
    elapsed = _elapsed(state.start_time)
    endpoint = _chat_endpoint(state.chat_url)

    if compact:
        top = Table.grid(expand=True)
        top.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        top.add_column(justify="right", no_wrap=True)
        top.add_row(
            f"[bold {SIGNAL_CYAN}]{spin} GENESIS // OPS[/] {status_label(state.run_phase, width=6)}",
            f"[dim {STEEL}]{elapsed}[/]",
        )
        detail_width = max(20, width - 13)
        detail = (
            f"[bold]{markup(state.task_name or 'No active task', detail_width)}[/]  "
            f"[dim {STEEL}]{state.completed}/{state.total}[/]  "
            f"[{SIGNAL_AMBER}]{markup(step, 10)}[/]@[{SIGNAL_CYAN}]{markup(active, 18)}[/]"
        )
        top.add_row(detail, "")
        return Panel(top, border_style=STEEL, box=box.HORIZONTALS, padding=(0, 1))

    grid = Table.grid(expand=True)
    grid.add_column(ratio=3, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", ratio=2, no_wrap=True, overflow="ellipsis")
    task_width = max(18, int(width * 0.6) - 35)
    progress_width = 14 if width < 132 else 22
    grid.add_row(
        f"[bold {SIGNAL_CYAN}]{spin} GENESIS // MISSION CONTROL[/] "
        f"{status_label(state.run_phase, width=8)} [bold]{markup(state.task_name or 'No active task', task_width)}[/]",
        f"[dim {STEEL}]ELAPSED[/] [bold]{elapsed}[/]  "
        f"[dim {STEEL}]GIT[/] [{SIGNAL_CYAN}]{markup(state.git_sha, 10)}[/]",
    )
    watch = f"  [dim {STEEL}]WATCH[/] [{SIGNAL_GREEN}]{markup(endpoint, 28)}[/]" if endpoint else ""
    grid.add_row(
        f"[{SIGNAL_CYAN}]{escape(progress_bar(state.completed, state.total, width=progress_width))}[/] "
        f"[bold]{state.completed}/{state.total}[/] [dim {STEEL}]LOCKED[/]",
        f"[dim {STEEL}]ACTIVE[/] [bold {SIGNAL_CYAN}]{markup(active, 24)}[/]  "
        f"[dim {STEEL}]STEP[/] [{SIGNAL_AMBER}]{markup(step, 14)}[/]{watch}",
    )
    return Panel(grid, border_style=SIGNAL_CYAN, box=box.HORIZONTALS, padding=(0, 1))


def _visible_steps(state: DashboardSnapshot, limit: int | None) -> list:
    if state.plan is None:
        return []
    steps = list(state.plan.steps)
    if not limit or len(steps) <= limit:
        return steps
    current_index = next(
        (index for index, step in enumerate(steps) if step.step_id == state.current_step),
        min(state.completed, len(steps) - 1),
    )
    start = max(0, min(current_index - 1, len(steps) - limit))
    return steps[start : start + limit]


def _plan_panel(
    state: DashboardSnapshot,
    *,
    detailed: bool,
    max_steps: int | None,
    work_width: int,
    compact_title: bool = False,
) -> Panel:
    title = "RUN QUEUE" if compact_title else "EXECUTION PLAN"
    if state.plan is None:
        waiting = Text("AWAITING PLANNER SIGNAL", style=f"dim {STEEL}")
        return command_panel(waiting, title, border_style=STEEL)

    table = Table(box=None, show_header=True, header_style=f"bold {SIGNAL_AMBER}", expand=True, pad_edge=False)
    table.add_column("State", width=8, no_wrap=True)
    table.add_column("Step", width=7, no_wrap=True)
    if detailed:
        table.add_column("R", width=2, justify="right", no_wrap=True)
    table.add_column("Work item", no_wrap=True, overflow="ellipsis")

    visible = _visible_steps(state, max_steps)
    for step in visible:
        status = state.step_statuses.get(step.step_id, "pending")
        repairs = state.step_repairs.get(step.step_id, 0)
        work = Text(trim(step.title, work_width), style=_status_style(status), no_wrap=True, overflow="ellipsis")
        if detailed:
            work.append("\n")
            work.append(trim(_step_scope(step, state), work_width), style=f"dim {STEEL}")
        row = [status_label(status, width=6), markup(step.step_id, 7)]
        if detailed:
            row.append(str(repairs) if repairs else "")
        row.append(work)
        table.add_row(*row)

    hidden = max(0, len(state.plan.steps) - len(visible))
    subtitle = f"{state.completed}/{state.total} locked"
    if hidden:
        subtitle += f" / {hidden} hidden"
    return command_panel(table, title, border_style=STEEL, subtitle=subtitle)


def _style_output_line(line: str) -> str:
    plain = _RICH_TAG_RE.sub("", line).strip()
    if plain.startswith("$") or plain.startswith("verify $"):
        return f"[bold {SIGNAL_AMBER}]{line}[/]"
    if plain.startswith("+") or " file_change" in plain:
        return f"[{SIGNAL_GREEN}]{line}[/]"
    if "review:" in plain or "approved" in plain:
        return f"[{SIGNAL_CYAN}]{line}[/]"
    if "retrying" in plain.lower() or "repairing" in plain.lower() or "needs_revision" in plain:
        return f"[{SIGNAL_AMBER}]{line}[/]"
    if "exit " in plain or "failed" in plain.lower() or plain.startswith("x "):
        return f"[{SIGNAL_RED}]{line}[/]"
    return line


def _output_panel(state: DashboardSnapshot, *, max_lines: int) -> Panel:
    lines = state.output_lines[-max(1, max_lines) :] if state.output_lines else (f"[dim {STEEL}]No agent output yet.[/]",)
    content = "\n".join(_style_output_line(line) for line in lines)
    actor = state.current_worker or state.current_reviewer or "standby"
    subtitle = trim(f"{actor} / {state.current_step or '-'}", 36)
    return command_panel(content, "LIVE FEED", border_style=SIGNAL_GREEN, subtitle=subtitle)


def _signal_panel(state: DashboardSnapshot, *, body_height: int, rail_width: int) -> Panel:
    content_rows = max(7, body_height - 2)
    generous = content_rows >= 14
    team_limit = 3 if generous else 2
    quality_limit = 3 if generous else 2
    event_limit = 3 if generous else 1

    grid = Table.grid(expand=True, padding=(0, 0))
    grid.add_column(overflow="ellipsis", no_wrap=True)
    grid.add_row(f"[bold {SIGNAL_AMBER}]TEAM / ASSIGNMENTS[/]")
    active_workers = set(state.active_steps.values())
    if state.current_worker:
        active_workers.add(state.current_worker)
    if state.current_reviewer:
        active_workers.add(state.current_reviewer)
    names = list(state.agent_names)
    names.sort(key=lambda name: name not in active_workers)
    for name in names[:team_limit]:
        active = name in active_workers
        marker = ">" if active else "."
        marker_style = SIGNAL_GREEN if active else STEEL
        role = "orchestrator" if "orchestrator" in name else "worker"
        grid.add_row(
            f"[{marker_style}]{marker}[/] {role_label(role)} "
            f"[{'bold ' + SIGNAL_CYAN if active else 'dim ' + STEEL}]{markup(name, max(10, rail_width - 13))}[/]"
        )
    if not names:
        grid.add_row(f"[{SIGNAL_RED}]! NO AGENTS ONLINE[/]")

    grid.add_row(f"[bold {SIGNAL_AMBER}]QUALITY LOCKS[/]")
    steps = list(state.plan.steps) if state.plan else []
    for step in steps[-quality_limit:]:
        status = state.step_statuses.get(step.step_id, "pending")
        verification = state.step_verification.get(step.step_id, "-")
        grid.add_row(
            f"[{SIGNAL_CYAN}]{markup(step.step_id, 7)}[/] "
            f"{status_label(status, width=6)} [dim {STEEL}]{markup(verification, 8)}[/]"
        )
    if not steps:
        grid.add_row(f"[dim {STEEL}]No gates reported[/]")

    grid.add_row(f"[bold {SIGNAL_AMBER}]RECENT SIGNAL[/]")
    events = state.recent_events[-event_limit:]
    if events:
        for label, detail, style in events:
            grid.add_row(
                f"[{style}]{markup(label, 8)}[/] "
                f"[dim {STEEL}]{markup(detail, max(12, rail_width - 12))}[/]"
            )
    else:
        grid.add_row(f"[dim {STEEL}]WAIT / no events[/]")

    grid.add_row(f"[bold {SIGNAL_AMBER}]TELEMETRY[/]")
    grid.add_row(
        f"[{SIGNAL_CYAN}]IN {_fmt_tokens(state.total_input())}[/]  "
        f"[{SIGNAL_GREEN}]OUT {_fmt_tokens(state.total_output())}[/]  "
        f"[dim {STEEL}]CACHE {_fmt_tokens(state.total_cached())}[/]"
    )
    if state.total_cost():
        grid.add_row(f"[{SIGNAL_AMBER}]COST ${state.total_cost():.4f}[/]")

    return command_panel(grid, "SYSTEM SIGNAL", border_style=SIGNAL_AMBER)


def _footer(state: DashboardSnapshot, *, width: int) -> Table:
    endpoint = _chat_endpoint(state.chat_url)
    if width < 96:
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(overflow="ellipsis", no_wrap=True)
        if state.blocked_reason:
            grid.add_row(f"[{SIGNAL_RED}]BLOCKER[/] [bold]{markup(state.blocked_reason, width - 12)}[/]")
        else:
            grid.add_row(
                f"[{SIGNAL_GREEN}]NOMINAL[/]  {state.completed}/{state.total}  "
                f"[{SIGNAL_CYAN}]IN {_fmt_tokens(state.total_input())}[/]  "
                f"[{SIGNAL_GREEN}]OUT {_fmt_tokens(state.total_output())}[/]"
            )
        if endpoint:
            grid.add_row(f"[dim {STEEL}]WATCH[/] [{SIGNAL_GREEN}]{markup(endpoint, width - 9)}[/]")
        return grid

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=3, overflow="ellipsis", no_wrap=True)
    grid.add_column(justify="right", ratio=2, overflow="ellipsis", no_wrap=True)
    if state.blocked_reason:
        left = f"[{SIGNAL_RED}]BLOCKER[/] [bold]{markup(state.blocked_reason, max(18, width // 2))}[/]"
    else:
        left = f"[{SIGNAL_GREEN}]NOMINAL[/] [dim {STEEL}]no blockers[/]"
    token_label = "" if width < 132 else f"[dim {STEEL}]TOKENS[/] "
    usage = f"{token_label}[{SIGNAL_CYAN}]IN {_fmt_tokens(state.total_input())}[/] [{SIGNAL_GREEN}]OUT {_fmt_tokens(state.total_output())}[/]"
    if endpoint:
        usage += f"  [dim {STEEL}]WATCH[/] [{SIGNAL_GREEN}]{markup(endpoint, 24)}[/]"
    grid.add_row(left, usage)
    return grid


def _terminal_dimensions(width: int | None, height: int | None) -> tuple[int, int]:
    fallback = shutil.get_terminal_size(fallback=(120, 30))
    return max(40, width or fallback.columns), max(8, height or fallback.lines)


def _layout_profile(width: int, height: int) -> str:
    if height < 18:
        return "short"
    if width >= 132 and height >= 22:
        return "wide"
    if width >= 96:
        return "standard"
    return "compact"


def make_layout(
    state: DashboardState | DashboardSnapshot,
    width: int | None = None,
    height: int | None = None,
) -> Layout:
    """Build a responsive dashboard for the supplied render dimensions."""
    snapshot = state.snapshot() if isinstance(state, DashboardState) else state
    width, height = _terminal_dimensions(width, height)
    profile = _layout_profile(width, height)

    layout = Layout(name="dashboard")
    header_size = 4
    footer_size = 2
    layout.split_column(
        Layout(name="header", size=header_size),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=footer_size),
    )
    layout["header"].update(_header(snapshot, width=width, compact=profile in {"compact", "short"}))
    layout["footer"].update(_footer(snapshot, width=width))

    body_height = max(2, height - header_size - footer_size)
    if profile == "short":
        layout["body"].update(_output_panel(snapshot, max_lines=max(2, body_height - 2)))
        return layout

    if profile == "compact":
        queue_size = min(8, max(5, body_height // 3 + 1))
        layout["body"].split_column(
            Layout(name="queue", size=queue_size),
            Layout(name="output", ratio=1),
        )
        layout["body"]["queue"].update(
            _plan_panel(
                snapshot,
                detailed=False,
                max_steps=max(1, queue_size - 3),
                work_width=max(20, width - 25),
                compact_title=True,
            )
        )
        layout["body"]["output"].update(
            _output_panel(snapshot, max_lines=max(2, body_height - queue_size - 2))
        )
        return layout

    if profile == "standard":
        layout["body"].split_row(
            Layout(name="plan", ratio=42),
            Layout(name="output", ratio=58),
        )
        layout["body"]["plan"].update(
            _plan_panel(
                snapshot,
                detailed=False,
                max_steps=max(3, body_height - 3),
                work_width=max(16, int(width * 0.42) - 23),
            )
        )
        layout["body"]["output"].update(
            _output_panel(snapshot, max_lines=max(3, body_height - 2))
        )
        return layout

    layout["body"].split_row(
        Layout(name="plan", ratio=32),
        Layout(name="output", ratio=44),
        Layout(name="signal", ratio=24),
    )
    layout["body"]["plan"].update(
        _plan_panel(
            snapshot,
            detailed=True,
            max_steps=max(3, (body_height - 3) // 2),
            work_width=max(18, int(width * 0.32) - 24),
        )
    )
    layout["body"]["output"].update(
        _output_panel(snapshot, max_lines=max(3, body_height - 2))
    )
    layout["body"]["signal"].update(
        _signal_panel(snapshot, body_height=body_height, rail_width=max(22, int(width * 0.24) - 2))
    )
    return layout
