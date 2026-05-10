"""
genesis — Terminal AI Orchestration System
Usage:
  genesis              Start interactive REPL (default)
  genesis run <task>   Execute a task non-interactively
  genesis init         Create ~/.genesis/config.toml
  genesis status       Show agents, state DB, recent commits/runs
  genesis runs         Show recent durable runs
  genesis inspect <id> Show a durable run trace
  genesis resume <id>  Resume a durable run
  genesis retry <id> <step> Retry a blocked step
  genesis cleanup <id> Remove stale worktrees for a run
  genesis --version    Show version
  genesis --help       Show help
"""
from __future__ import annotations
import sys

from genesis import __version__


def main() -> None:
    argv = sys.argv[1:]

    if not argv:
        _start_repl()
        return

    cmd = argv[0].lower()

    if cmd in ("--version", "-v", "version"):
        print(f"Genesis {__version__}")

    elif cmd in ("--help", "-h", "help"):
        print(__doc__)

    elif cmd == "init":
        _init_config()

    elif cmd == "run":
        if len(argv) < 2:
            print("Usage: genesis run <task description>")
            sys.exit(1)
        task = " ".join(argv[1:])
        _run_task(task)

    elif cmd == "status":
        _with_repl("cmd_status")

    elif cmd == "runs":
        _with_repl("cmd_runs")

    elif cmd == "resume":
        if len(argv) < 2:
            print("Usage: genesis resume <run_id>")
            sys.exit(1)
        _with_repl("cmd_resume", argv[1])

    elif cmd == "retry":
        if len(argv) < 3:
            print("Usage: genesis retry <run_id> <step_id>")
            sys.exit(1)
        _with_repl("cmd_retry", argv[1:3])

    elif cmd == "cleanup":
        if len(argv) < 2:
            print("Usage: genesis cleanup <run_id>")
            sys.exit(1)
        _with_repl("cmd_cleanup", argv[1])

    elif cmd == "inspect":
        if len(argv) < 2:
            print("Usage: genesis inspect <run_id>")
            sys.exit(1)
        _with_repl("cmd_inspect", argv[1])

    elif cmd == "memory":
        _with_repl("cmd_memory", argv[1:])

    elif cmd == "config":
        _with_repl("cmd_config", argv[1:])

    else:
        # Unknown subcommand — drop into the REPL and let it handle it
        _start_repl()


def _start_repl() -> None:
    from genesis.repl import GenesisREPL
    GenesisREPL().run()


def _init_config() -> None:
    from genesis.config import init_config
    path = init_config()
    if path.exists():
        print(f"Config ready: {path}")
        print("Edit it to configure your agents, then run 'genesis' to start.")
    else:
        print(f"Created: {path}")


def _run_task(task: str) -> None:
    from genesis.repl import GenesisREPL
    repl = GenesisREPL()
    repl._print_banner()
    repl.cmd_run(task)


def _with_repl(method_name: str, *args) -> None:
    from genesis.repl import GenesisREPL
    repl = GenesisREPL()
    getattr(repl, method_name)(*args)


if __name__ == "__main__":
    main()
