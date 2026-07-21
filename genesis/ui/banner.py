"""Responsive Genesis startup banner.

The wide treatment is a single-color industrial wordmark.  Smaller terminals
get a compact control-plane plate rather than a wrapped block logo.
"""
from __future__ import annotations

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from genesis.ui.theme import SIGNAL_AMBER, SIGNAL_CYAN, SIGNAL_GREEN, SIGNAL_RED, STEEL, trim


_FONT: dict[str, list[str]] = {
    "G": ["█████", "█    ", "█  ██", "█   █", "█████"],
    "E": ["█████", "█    ", "███  ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "S": ["█████", "█    ", "█████", "    █", "█████"],
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
}


def _wordmark(word: str = "GENESIS") -> Text:
    out = Text(justify="center")
    for row in range(5):
        line = Text()
        for index, character in enumerate(word):
            line.append(_FONT[character][row], style=f"bold {SIGNAL_CYAN}")
            if index != len(word) - 1:
                line.append(" ", style=STEEL)
        out.append_text(line)
        if row != 4:
            out.append("\n")
    return out


def _status_strip(systems: list[tuple[str, bool]], *, compact: bool = False) -> Text:
    """Build a text-first status strip that remains useful without color."""
    strip = Text(justify="center", no_wrap=compact, overflow="ellipsis")
    for index, (name, online) in enumerate(systems):
        marker = "UP" if online else "--"
        color = SIGNAL_GREEN if online else SIGNAL_RED
        strip.append(f"[{marker}]", style=f"bold {color}")
        strip.append(f" {name}", style="default" if online else f"dim {STEEL}")
        if index != len(systems) - 1:
            strip.append("  /  ", style=f"dim {STEEL}")
    return strip


def _info_line(version: str, info: list[tuple[str, str]], *, width: int) -> Text:
    values = [("ver", f"v{version}"), *info]
    line = Text(justify="center", overflow="ellipsis")
    for index, (key, value) in enumerate(values):
        line.append(f"{key.upper()} ", style=f"dim {STEEL}")
        line.append(trim(value, 24), style=f"bold {SIGNAL_CYAN}")
        if index != len(values) - 1:
            line.append("  |  ", style=f"dim {STEEL}")
    line.truncate(max(8, width), overflow="ellipsis")
    return line


def _compact_banner(
    *,
    width: int,
    version: str,
    systems: list[tuple[str, bool]],
    info: list[tuple[str, str]],
    commands: str,
) -> Panel:
    heading = Text(justify="left")
    heading.append("GENESIS", style=f"bold {SIGNAL_CYAN}")
    heading.append(" // CONTROL PLANE", style=f"bold {SIGNAL_AMBER}")

    usable = max(18, width - 6)
    command_line = Text(trim(commands, usable), style=f"dim {STEEL}", no_wrap=True, overflow="ellipsis")
    body = Group(
        heading,
        _status_strip(systems, compact=True),
        _info_line(version, info, width=usable),
        command_line,
    )
    return Panel(
        body,
        title=f"[bold {SIGNAL_AMBER}] LOCAL MISSION CONTROL [/]",
        title_align="right",
        border_style=STEEL,
        box=box.SQUARE,
        padding=(0, 1),
    )


def _wide_banner(
    *,
    width: int,
    version: str,
    systems: list[tuple[str, bool]],
    info: list[tuple[str, str]],
    commands: str,
) -> Panel:
    identity = Table.grid(expand=True)
    identity.add_column(ratio=1)
    identity.add_column(justify="right", no_wrap=True)
    identity.add_row(
        f"[bold {SIGNAL_CYAN}]GENESIS[/] [bold {SIGNAL_AMBER}]// CONTROL PLANE / LOCAL[/]",
        f"[dim {STEEL}]OPERATOR CONSOLE[/]",
    )

    command_line = Text(trim(commands, max(24, width - 12)), style=f"dim {STEEL}", justify="center")
    body = Group(
        identity,
        Text(""),
        _wordmark(),
        Text(""),
        Text("AUTONOMOUS SOFTWARE OPERATIONS", style=f"bold {SIGNAL_AMBER}", justify="center"),
        _status_strip(systems),
        Text(""),
        _info_line(version, info, width=max(24, width - 12)),
        command_line,
    )
    return Panel(
        Align.center(body),
        box=box.SQUARE,
        border_style=SIGNAL_CYAN,
        padding=(0, 2),
    )


def render_banner(
    console: Console,
    *,
    version: str,
    systems: list[tuple[str, bool]],
    info: list[tuple[str, str]],
    commands: str,
) -> None:
    width = max(40, console.size.width)
    if width < 96:
        panel = _compact_banner(
            width=width,
            version=version,
            systems=systems,
            info=info,
            commands=commands,
        )
    else:
        panel = _wide_banner(
            width=width,
            version=version,
            systems=systems,
            info=info,
            commands=commands,
        )
    console.print(panel)
