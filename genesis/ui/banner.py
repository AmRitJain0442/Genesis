"""
Genesis banner — a block-letter wordmark with a cyan→violet gradient plus a
compact status strip. Rendered at REPL startup.
"""
from __future__ import annotations

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich import box


# 5-row block font for the wordmark. Each glyph is a list of 5 equal-width rows.
_FONT: dict[str, list[str]] = {
    "G": ["█████", "█    ", "█  ██", "█   █", "█████"],
    "E": ["█████", "█    ", "███  ", "█    ", "█████"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
    "S": ["█████", "█    ", "█████", "    █", "█████"],
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
}

# Per-letter gradient stops: cyan → violet across "GENESIS".
_GRADIENT = ["#22D3EE", "#3DC7F0", "#62B2F3", "#8A93F6", "#A87DF9", "#B66FFB", "#C084FC"]


def _wordmark(word: str = "GENESIS") -> Text:
    out = Text(justify="center")
    for r in range(5):
        line = Text()
        for i, ch in enumerate(word):
            glyph = _FONT[ch][r]
            color = _GRADIENT[i % len(_GRADIENT)]
            line.append(glyph, style=f"bold {color}")
            if i != len(word) - 1:
                line.append(" ")
        out.append_text(line)
        if r != 4:
            out.append("\n")
    return out


def _status_strip(systems: list[tuple[str, bool]]) -> Text:
    """systems: list of (name, online)."""
    strip = Text(justify="center")
    for i, (name, online) in enumerate(systems):
        marker = "●" if online else "○"
        color = "#4ADE80" if online else "#F87171"
        strip.append(f"{marker} ", style=f"bold {color}")
        strip.append(name, style="#E5E7EB" if online else "#6B7280")
        if i != len(systems) - 1:
            strip.append("    ", style="#334155")
    return strip


def render_banner(
    console: Console,
    *,
    version: str,
    systems: list[tuple[str, bool]],
    info: list[tuple[str, str]],
    commands: str,
) -> None:
    wordmark = _wordmark()
    tagline = Text("terminal AI orchestration · command center", style="italic #6B7280", justify="center")
    strip = _status_strip(systems)

    info = [("genesis", f"v{version}"), *info]
    info_line = Text(justify="center")
    for i, (k, v) in enumerate(info):
        info_line.append(f"{k} ", style="#6B7280")
        info_line.append(v, style="bold #22D3EE")
        if i != len(info) - 1:
            info_line.append("   •   ", style="#334155")

    cmd = Text(commands, style="#6B7280", justify="center")

    body = Group(
        Text(""),
        wordmark,
        Text(""),
        tagline,
        Text(""),
        strip,
        Text(""),
        info_line,
        Text(""),
        cmd,
        Text(""),
    )

    panel = Panel(
        Align.center(body),
        box=box.DOUBLE,
        border_style="#22D3EE",
        padding=(0, 4),
    )
    console.print(panel)
