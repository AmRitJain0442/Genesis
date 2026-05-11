from __future__ import annotations

from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


STATUS_STYLES = {
    "pending": ("PEND", "dim"),
    "running": ("RUN", "bold cyan"),
    "reviewing": ("REV", "bold magenta"),
    "approved": ("OK", "bold green"),
    "needs_revision": ("FIX", "bold yellow"),
    "verifying": ("VERIFY", "bold blue"),
    "committed": ("COMMIT", "bold green"),
    "completed": ("DONE", "bold green"),
    "blocked": ("BLOCK", "bold red"),
    "rejected": ("REJECT", "bold red"),
    "failed": ("FAIL", "bold red"),
    "repairing": ("REPAIR", "bold yellow"),
    "planning": ("PLAN", "bold cyan"),
    "online": ("ONLINE", "bold green"),
    "missing": ("MISS", "bold red"),
    "idle": ("IDLE", "dim"),
}


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
    if "orchestrator" in role:
        return "[bold magenta]ORCH[/bold magenta]"
    if "review" in role:
        return "[bold blue]REVIEW[/bold blue]"
    return "[cyan]WORK[/cyan]"


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
    border_style: str = "cyan",
    show_lines: bool = False,
    expand: bool = True,
) -> Table:
    return Table(
        title=f"[bold]{escape(title)}[/bold]",
        box=box.ROUNDED,
        border_style=border_style,
        header_style="bold cyan",
        show_lines=show_lines,
        expand=expand,
        pad_edge=False,
    )


def command_panel(
    renderable,
    title: str,
    *,
    border_style: str = "cyan",
    subtitle: str = "",
    padding: tuple[int, int] = (0, 1),
) -> Panel:
    title_text = f"[bold]{escape(title)}[/bold]"
    if subtitle:
        title_text += f" [dim]{escape(subtitle)}[/dim]"
    return Panel(
        renderable,
        title=title_text,
        border_style=border_style,
        box=box.ROUNDED,
        padding=padding,
    )


def kv_table(rows: list[tuple[str, object]], *, title: str = "", border_style: str = "cyan") -> Table:
    tbl = command_table(title or "Details", border_style=border_style)
    tbl.add_column("Key", style="dim", no_wrap=True)
    tbl.add_column("Value", overflow="fold")
    for key, value in rows:
        tbl.add_row(markup(key), markup(value))
    return tbl


def muted(text: object) -> Text:
    return Text(str(text), style="dim")
