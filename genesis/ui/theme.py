from __future__ import annotations

from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# Genesis uses a restrained, industrial palette: cyan is live signal, green is
# accepted work, amber means attention, and steel carries structure.  Keeping
# the colors here prevents individual screens from drifting into unrelated
# accents and makes the UI legible on both dark and light terminal themes.
SIGNAL_CYAN = "#2DD4D7"
SIGNAL_GREEN = "#68D391"
SIGNAL_AMBER = "#F2B84B"
SIGNAL_RED = "#F06A6A"
STEEL = "#78909C"
STEEL_DARK = "#3E5961"


STATUS_STYLES = {
    "pending": ("PEND", f"dim {STEEL}"),
    "running": ("RUN", f"bold {SIGNAL_CYAN}"),
    "reviewing": ("REV", f"bold {SIGNAL_AMBER}"),
    "approved": ("OK", f"bold {SIGNAL_GREEN}"),
    "needs_revision": ("FIX", f"bold {SIGNAL_AMBER}"),
    "verifying": ("VERIFY", f"bold {SIGNAL_CYAN}"),
    "committed": ("COMMIT", f"bold {SIGNAL_GREEN}"),
    "completed": ("DONE", f"bold {SIGNAL_GREEN}"),
    "blocked": ("BLOCK", f"bold {SIGNAL_RED}"),
    "rejected": ("REJECT", f"bold {SIGNAL_RED}"),
    "failed": ("FAIL", f"bold {SIGNAL_RED}"),
    "repairing": ("REPAIR", f"bold {SIGNAL_AMBER}"),
    "planning": ("PLAN", f"bold {SIGNAL_CYAN}"),
    "committing": ("COMMIT", f"bold {SIGNAL_AMBER}"),
    "online": ("ONLINE", f"bold {SIGNAL_GREEN}"),
    "missing": ("MISS", f"bold {SIGNAL_RED}"),
    "idle": ("IDLE", f"dim {STEEL}"),
}


_BORDER_ALIASES = {
    "cyan": SIGNAL_CYAN,
    "blue": STEEL,
    "magenta": SIGNAL_CYAN,
    "green": SIGNAL_GREEN,
    "yellow": SIGNAL_AMBER,
    "red": SIGNAL_RED,
}


def palette_style(style: str) -> str:
    """Map legacy Rich color names onto the Genesis signal palette."""
    return _BORDER_ALIASES.get(style, style)


def trim(value: object, width: int, *, placeholder: str = "-") -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return placeholder
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def markup(value: object, width: int | None = None, *, placeholder: str = "-") -> str:
    text = trim(value, width, placeholder=placeholder) if width else str(value if value is not None else "")
    return escape(text if text else placeholder)


def status_label(status: str, *, width: int = 7) -> str:
    key = (status or "idle").lower()
    label, style = STATUS_STYLES.get(key, (trim(status, width).upper(), "white"))
    return f"[{style}][{label:^{width}}][/]"


def role_label(role: str) -> str:
    role = (role or "worker").lower()
    if "orchestrator" in role or role == "brain":
        return f"[bold {SIGNAL_AMBER}]CTRL[/]"
    if "review" in role:
        return f"[bold {SIGNAL_CYAN}]QA[/]"
    return f"[{SIGNAL_GREEN}]EXEC[/]"


def progress_bar(done: int, total: int, *, width: int = 24) -> str:
    total = max(total, 1)
    done = max(0, min(done, total))
    filled = int((done / total) * width)
    if done and filled == 0:
        filled = 1
    return "[" + "#" * filled + "." * (width - filled) + "]"


def command_table(
    title: str,
    *,
    border_style: str = SIGNAL_CYAN,
    show_lines: bool = False,
    expand: bool = True,
) -> Table:
    return Table(
        title=f"[bold {SIGNAL_CYAN}]{escape(title.upper())}[/]",
        box=box.SQUARE,
        border_style=palette_style(border_style),
        header_style=f"bold {SIGNAL_AMBER}",
        show_lines=show_lines,
        expand=expand,
        pad_edge=False,
    )


def command_panel(
    renderable,
    title: str,
    *,
    border_style: str = SIGNAL_CYAN,
    subtitle: str = "",
    padding: tuple[int, int] = (0, 1),
) -> Panel:
    title_text = f"[bold {SIGNAL_CYAN}]{escape(title.upper())}[/]"
    if subtitle:
        title_text += f" [dim {STEEL}]{escape(subtitle)}[/]"
    return Panel(
        renderable,
        title=title_text,
        border_style=palette_style(border_style),
        box=box.SQUARE,
        padding=padding,
    )


def kv_table(rows: list[tuple[str, object]], *, title: str = "", border_style: str = SIGNAL_CYAN) -> Table:
    tbl = command_table(title or "Details", border_style=border_style)
    tbl.add_column("Key", style=f"dim {STEEL}", no_wrap=True)
    tbl.add_column("Value", overflow="fold")
    for key, value in rows:
        tbl.add_row(markup(key), markup(value))
    return tbl


def muted(text: object) -> Text:
    return Text(str(text), style=f"dim {STEEL}")
