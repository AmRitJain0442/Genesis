"""
genesis — Terminal AI Orchestration System
Usage:
  genesis              Start interactive REPL (default)
  genesis run <task>   Execute a task non-interactively
  genesis init         Create ~/.genesis/config.toml
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
        print("Edit it to add your API keys, then run 'genesis' to start.")
    else:
        print(f"Created: {path}")


def _run_task(task: str) -> None:
    from genesis.repl import GenesisREPL
    repl = GenesisREPL()
    repl._print_banner()
    repl.cmd_run(task)


if __name__ == "__main__":
    main()
