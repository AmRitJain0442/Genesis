"""Command-line entry point for Genesis."""
from __future__ import annotations

import sys
from collections.abc import Sequence

from rich.console import Console, Group
from rich.text import Text

from genesis import __version__
from genesis.ui.console import console
from genesis.ui.theme import command_panel, command_table, markup


_ERROR_CONSOLE = Console(stderr=True)

_HELP_ROWS = (
    ("(no command)", "Start the interactive terminal"),
    ("run <task>", "Execute a task non-interactively"),
    ("plan <task>", "Generate and save a plan without executing it"),
    ("status", "Show agents, state, and recent repository activity"),
    ("usage [--refresh] [--json]", "Show aggregate account capacity, graphs, and cost"),
    ("runs", "Show recent durable runs"),
    ("inspect <run_id>", "Show a durable run trace"),
    ("resume <run_id>", "Resume a durable run"),
    ("retry <run_id> <step_id>", "Retry a blocked step"),
    ("cleanup <run_id>", "Remove stale worktrees for a run"),
    ("memory [command]", "Show, search, mine, clear, or append memory"),
    ("config [show|edit]", "Show or edit configuration"),
    ("init", "Create the default configuration file"),
    ("--version", "Show the installed version"),
    ("--help", "Show this command reference"),
)

_USAGE = {
    "run": "genesis run <task description>",
    "plan": "genesis plan <task description>",
    "resume": "genesis resume <run_id>",
    "retry": "genesis retry <run_id> <step_id>",
    "cleanup": "genesis cleanup <run_id>",
    "inspect": "genesis inspect <run_id>",
    "memory": "genesis memory [show | search <query> | mine | clear | append <text>]",
    "config": "genesis config [show | edit]",
    "usage": "genesis usage [--refresh] [--json]",
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    Passing ``argv`` makes command dispatch directly testable. When omitted, the
    arguments after the executable name are used, as expected by the console
    script and ``python -m genesis``.
    """
    args = list(sys.argv[1:] if argv is None else argv)

    if not args:
        _start_repl()
        return 0

    cmd = args[0].lower()
    operands = args[1:]

    if cmd in ("--version", "-v", "version"):
        if operands:
            return _usage_error(f"'{args[0]}' does not accept arguments")
        console.print(f"Genesis {__version__}")
        return 0

    if cmd in ("--help", "-h", "help"):
        if operands:
            return _usage_error(f"'{args[0]}' does not accept arguments")
        _print_help()
        return 0

    if cmd == "init":
        if operands:
            return _usage_error("'init' does not accept arguments", "genesis init")
        _init_config()
        return 0

    if cmd in ("run", "plan"):
        task = " ".join(operands).strip()
        if not task:
            return _usage_error(f"'{cmd}' requires a task description", _USAGE[cmd])
        if cmd == "run":
            _run_task(task)
        else:
            _with_repl("cmd_plan", task)
        return 0

    if cmd in ("status", "runs"):
        if operands:
            return _usage_error(f"'{cmd}' does not accept arguments", f"genesis {cmd}")
        _with_repl(f"cmd_{cmd}")
        return 0

    if cmd in ("usage", "limits", "quota"):
        normalized = [operand.lower() for operand in operands]
        allowed = {"--refresh", "--json"}
        if any(operand not in allowed for operand in normalized) or len(normalized) != len(set(normalized)):
            return _usage_error("unknown or repeated usage option", _USAGE["usage"])
        _with_repl("cmd_usage", normalized)
        return 0

    if cmd in ("resume", "cleanup", "inspect"):
        if len(operands) != 1 or not operands[0].strip():
            return _usage_error(f"'{cmd}' requires exactly one run ID", _USAGE[cmd])
        _with_repl(f"cmd_{cmd}", operands[0])
        return 0

    if cmd == "retry":
        if len(operands) != 2 or any(not value.strip() for value in operands):
            return _usage_error("'retry' requires a run ID and step ID", _USAGE[cmd])
        _with_repl("cmd_retry", operands)
        return 0

    if cmd == "memory":
        error = _validate_memory_args(operands)
        if error:
            return _usage_error(error, _USAGE[cmd])
        normalized = [operands[0].lower(), *operands[1:]] if operands else []
        _with_repl("cmd_memory", normalized)
        return 0

    if cmd == "config":
        if len(operands) > 1 or (operands and operands[0].lower() not in {"show", "edit"}):
            return _usage_error("unknown or malformed config command", _USAGE[cmd])
        normalized = [operands[0].lower()] if operands else []
        _with_repl("cmd_config", normalized)
        return 0

    shown = args[0] if args[0] else "<empty>"
    return _usage_error(f"unknown command '{shown}'")


def _validate_memory_args(args: list[str]) -> str | None:
    if not args:
        return None

    subcommand = args[0].lower()
    if subcommand in {"show", "mine", "clear"}:
        if len(args) == 1:
            return None
        return f"'memory {subcommand}' does not accept arguments"
    if subcommand in {"search", "append"}:
        if len(args) > 1 and " ".join(args[1:]).strip():
            return None
        return f"'memory {subcommand}' requires text"
    return f"unknown memory command '{args[0]}'"


def _print_help() -> None:
    intro = Text()
    intro.append("Genesis", style="bold cyan")
    intro.append(f"  v{__version__}\n", style="dim")
    intro.append("A reliable terminal harness for planning, running, and inspecting AI coding work.")
    intro.append("\nUsage: ", style="dim")
    intro.append("genesis [command] [arguments]", style="bold")

    table = command_table("COMMANDS", border_style="cyan", expand=True)
    table.add_column("Command", style="bold cyan", ratio=2, min_width=26, overflow="fold")
    table.add_column("Description", ratio=3, min_width=34, overflow="fold")
    for command, description in _HELP_ROWS:
        table.add_row(markup(command), markup(description))

    hint = Text.from_markup(
        "[dim]Arguments in <angle brackets> are required. "
        "Run [bold]genesis[/bold] with no command for the interactive terminal.[/dim]"
    )
    console.print(
        Group(
            command_panel(intro, "GENESIS CLI", border_style="blue", padding=(1, 2)),
            table,
            hint,
        )
    )


def _usage_error(message: str, usage: str | None = None) -> int:
    _ERROR_CONSOLE.print(f"[bold red]Error:[/bold red] {markup(message)}")
    if usage:
        _ERROR_CONSOLE.print(f"[dim]Usage:[/dim] {markup(usage)}")
    _ERROR_CONSOLE.print("[dim]Run 'genesis --help' for the command reference.[/dim]")
    return 2


def _start_repl() -> None:
    from genesis.repl import GenesisREPL

    GenesisREPL().run()


def _init_config() -> None:
    from genesis.config import init_config

    path = init_config()
    if path.exists():
        console.print(f"Config ready: {markup(path)}")
        console.print("Edit it to configure your agents, then run 'genesis' to start.")
    else:
        console.print(f"Created: {markup(path)}")


def _run_task(task: str) -> None:
    from genesis.repl import GenesisREPL

    repl = GenesisREPL()
    repl._print_banner()
    repl.cmd_run(task)


def _with_repl(method_name: str, *args: object) -> None:
    from genesis.repl import GenesisREPL

    repl = GenesisREPL()
    getattr(repl, method_name)(*args)


if __name__ == "__main__":
    raise SystemExit(main())
