from __future__ import annotations
import os
import shlex
import logging
from pathlib import Path

from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.markdown import Markdown
from rich import box

from genesis import __version__
from genesis.config import get_config, CONFIG_FILE
from genesis.memory import MemoryManager
from genesis.git_ops import GitManager
from genesis.agents.base import AgentInfo, BaseAgent
from genesis.agents.claude_agent import ClaudeAgent
from genesis.agents.gpt_agent import GPTAgent
from genesis.agents.orchestrator import Orchestrator
from genesis.ui.console import console
from genesis.ui.dashboard import DashboardState, make_layout

logger = logging.getLogger(__name__)

_HELP = """\
[bold]GENESIS COMMANDS[/bold]

  [bold cyan]run[/bold cyan] [dim]<task>[/dim]                    Execute a task through the AI orchestrator
  [bold cyan]plan[/bold cyan] [dim]<task>[/dim]                   Generate and preview a plan (no execution)
  [bold cyan]status[/bold cyan]                         Show agent info and recent git log
  [bold cyan]memory show[/bold cyan]                    Display the shared memory file
  [bold cyan]memory clear[/bold cyan]                   Reset the memory file (prompts for confirmation)
  [bold cyan]memory append[/bold cyan] [dim]<text>[/dim]          Manually add a note to memory
  [bold cyan]config show[/bold cyan]                    Display current configuration
  [bold cyan]config edit[/bold cyan]                    Open config in your editor
  [bold cyan]git log[/bold cyan]                        Show recent Genesis commits
  [bold cyan]git commit[/bold cyan] [dim][message][/dim]          Manually commit current changes
  [bold cyan]agents[/bold cyan]                         List configured agents
  [bold cyan]switch orchestrator[/bold cyan] [dim]<claude|gpt>[/dim]  Hot-swap orchestrator
  [bold cyan]switch worker[/bold cyan] [dim]<claude|gpt>[/dim]      Hot-swap worker
  [bold cyan]help[/bold cyan]                           Show this help
  [bold cyan]clear[/bold cyan]                          Clear the terminal
  [bold cyan]exit[/bold cyan]                           Exit Genesis
"""


class GenesisREPL:
    def __init__(self) -> None:
        self.config = get_config()
        self.work_dir = str(Path.cwd())
        self.memory = MemoryManager(
            str(Path(self.work_dir) / self.config.memory.file)
        )
        self.git = GitManager(self.work_dir, self.config.git)
        self._orch_provider = self.config.orchestrator.provider
        self._worker_provider = self.config.worker.provider
        self._agents: dict[str, BaseAgent] = {}
        self._build_agents()

    # ── Agent construction ─────────────────────────────────────────────────

    def _build_agents(self) -> None:
        ak = self.config.get_anthropic_key()
        ok = self.config.get_openai_key()

        if ak:
            self._agents["claude-orchestrator"] = ClaudeAgent(
                AgentInfo(
                    "claude-orchestrator", "claude",
                    self.config.orchestrator.model
                    if self._orch_provider == "claude"
                    else "claude-opus-4-6",
                    self.config.orchestrator.max_tokens,
                ),
                ak,
            )
            self._agents["claude-worker"] = ClaudeAgent(
                AgentInfo(
                    "claude-worker", "claude",
                    self.config.worker.model
                    if self._worker_provider == "claude"
                    else "claude-sonnet-4-6",
                    self.config.worker.max_tokens,
                ),
                ak,
            )

        if ok:
            self._agents["gpt-orchestrator"] = GPTAgent(
                AgentInfo(
                    "gpt-orchestrator", "gpt",
                    self.config.orchestrator.model
                    if self._orch_provider == "gpt"
                    else "gpt-4o",
                    self.config.orchestrator.max_tokens,
                ),
                ok,
            )
            self._agents["gpt-worker"] = GPTAgent(
                AgentInfo(
                    "gpt-worker", "gpt",
                    self.config.worker.model
                    if self._worker_provider == "gpt"
                    else "gpt-4o",
                    self.config.worker.max_tokens,
                ),
                ok,
            )

    def _get_orchestrator(self) -> BaseAgent | None:
        return self._agents.get(f"{self._orch_provider}-orchestrator")

    def _get_workers(self) -> dict[str, BaseAgent]:
        return {k: v for k, v in self._agents.items() if "worker" in k}

    def _make_orchestrator(self) -> Orchestrator | None:
        orch = self._get_orchestrator()
        workers = self._get_workers()
        if not orch or not workers:
            return None
        return Orchestrator(orch, workers, self.memory, self.git, self.config, self.work_dir)

    # ── Commands ───────────────────────────────────────────────────────────

    def cmd_run(self, task: str) -> None:
        if not task:
            console.print("[red]Usage: run <task description>[/red]")
            return

        orchestrator = self._make_orchestrator()
        if not orchestrator:
            console.print(
                "[red]No agents available.[/red] "
                "Set [bold]ANTHROPIC_API_KEY[/bold] and/or [bold]OPENAI_API_KEY[/bold], "
                "then run [cyan]config show[/cyan] to verify."
            )
            return

        state = DashboardState()
        state.task_name = task[:70] + ("…" if len(task) > 70 else "")

        # ── Callback closures ────────────────────────────────────────────
        def on_plan(plan):
            state.plan = plan
            state.total = len(plan.steps)
            for s in plan.steps:
                state.step_statuses[s.step_id] = "pending"

        def on_step_start(step, idx, total):
            state.current_step = step.step_id
            state.step_statuses[step.step_id] = "running"
            state.add_output(f"[cyan]→[/cyan] [bold]{step.step_id}[/bold]: {step.title}")

        def on_worker_assigned(step, worker_name):
            state.add_output(f"  [dim]assigned → {worker_name}[/dim]")

        def on_step_result(step, result, worker_name):
            if result.files_written:
                flist = ", ".join(result.files_written)
                state.add_output(f"  [green]wrote:[/green] {flist}")

        def on_review(step, review):
            color = "green" if review.verdict == "approved" else "yellow" if review.verdict == "needs_revision" else "red"
            icon = "✓" if review.verdict == "approved" else "⚠" if review.verdict == "needs_revision" else "✗"
            state.add_output(
                f"  [{color}]{icon}[/{color}] {review.verdict} "
                f"[dim]({review.quality_score}/10)[/dim]"
            )
            state.step_statuses[step.step_id] = review.verdict

        def on_commit(step, sha):
            if sha:
                state.git_sha = sha
                state.add_output(f"  [dim]committed {sha}[/dim]")

        def on_step_complete(step, review, completed, total):
            state.completed = completed

        def on_status(msg):
            state.add_output(f"[dim]{msg}[/dim]")

        def on_error(step, error):
            state.add_output(f"  [red]✗ {error}[/red]")
            state.step_statuses[step.step_id] = "rejected"

        def on_task_complete(plan):
            state.add_output(f"\n[bold green]✓ Task complete — {state.completed}/{state.total} steps[/bold green]")

        # ── Wire callbacks to also refresh the Live display ──────────────
        raw_callbacks = {
            "on_plan": on_plan,
            "on_step_start": on_step_start,
            "on_worker_assigned": on_worker_assigned,
            "on_step_result": on_step_result,
            "on_review": on_review,
            "on_commit": on_commit,
            "on_step_complete": on_step_complete,
            "on_status": on_status,
            "on_error": on_error,
            "on_task_complete": on_task_complete,
        }

        try:
            with Live(
                make_layout(state),
                console=console,
                refresh_per_second=4,
                screen=False,
            ) as live:
                def make_cb(fn):
                    def wrapped(*args, **kwargs):
                        fn(*args, **kwargs)
                        live.update(make_layout(state))
                    return wrapped

                callbacks = {k: make_cb(v) for k, v in raw_callbacks.items()}
                orchestrator.run_task(task, callbacks)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted. Progress has been saved to memory.[/yellow]")
        except Exception as e:
            console.print(f"\n[red]Task failed: {e}[/red]")
            logger.exception("run_task error")

    def cmd_plan(self, task: str) -> None:
        if not task:
            console.print("[red]Usage: plan <task description>[/red]")
            return

        orchestrator = self._make_orchestrator()
        if not orchestrator:
            console.print("[red]No agents available.[/red]")
            return

        console.print(f"[dim]Planning:[/dim] {task}\n")
        try:
            plan = orchestrator.plan(task)
        except Exception as e:
            console.print(f"[red]Planning failed: {e}[/red]")
            return

        tbl = Table(
            title=f"[bold]{plan.task_summary}[/bold]  [dim](id: {plan.task_id})[/dim]",
            box=box.ROUNDED,
            show_lines=True,
        )
        tbl.add_column("#", style="dim", width=8)
        tbl.add_column("Title", width=32)
        tbl.add_column("Type", width=10)
        tbl.add_column("Agent", width=16)
        tbl.add_column("Depends On", width=14)

        for s in plan.steps:
            deps = ", ".join(s.depends_on) if s.depends_on else "—"
            tbl.add_row(s.step_id, s.title, s.type, s.preferred_agent, deps)

        console.print(tbl)

    def cmd_status(self) -> None:
        tbl = Table(title="Configured Agents", box=box.ROUNDED)
        tbl.add_column("Name", style="cyan")
        tbl.add_column("Provider")
        tbl.add_column("Model")
        tbl.add_column("Role")
        tbl.add_column("API Key")

        for name, agent in self._agents.items():
            has_key = "[green]✓[/green]"
            role = "orchestrator" if "orchestrator" in name else "worker"
            tbl.add_row(name, agent.provider, agent.model, role, has_key)

        if not self._agents:
            tbl.add_row("[dim]none[/dim]", "—", "—", "—", "[red]✗[/red]")

        console.print(tbl)

        log = self.git.get_log(5)
        if log:
            console.print("\n[bold]Recent commits:[/bold]")
            for entry in log:
                console.print(f"  [dim]{entry}[/dim]")

    def cmd_memory(self, args: list[str]) -> None:
        sub = args[0] if args else "show"

        if sub == "show":
            console.print(Markdown(self.memory.read()))
        elif sub == "clear":
            console.print("[yellow]Reset GENESIS_MEMORY.md? This cannot be undone. (y/N): [/yellow]", end="")
            if input().strip().lower() == "y":
                self.memory.clear()
                console.print("[green]Memory cleared.[/green]")
        elif sub == "append" and len(args) > 1:
            note = " ".join(args[1:])
            self.memory.append_note(note)
            console.print("[green]Note added.[/green]")
        else:
            console.print("Usage: memory [show | clear | append <text>]")

    def cmd_config(self, args: list[str]) -> None:
        sub = args[0] if args else "show"

        if sub == "show":
            cfg = self.config
            tbl = Table(title="Genesis Configuration", box=box.ROUNDED)
            tbl.add_column("Setting", style="cyan")
            tbl.add_column("Value")

            rows = [
                ("orchestrator.provider", cfg.orchestrator.provider),
                ("orchestrator.model", cfg.orchestrator.model),
                ("orchestrator.max_tokens", str(cfg.orchestrator.max_tokens)),
                ("worker.provider", cfg.worker.provider),
                ("worker.model", cfg.worker.model),
                ("worker.max_tokens", str(cfg.worker.max_tokens)),
                ("git.auto_commit", str(cfg.git.auto_commit)),
                ("git.auto_push", str(cfg.git.auto_push)),
                ("git.commit_prefix", cfg.git.commit_prefix),
                ("memory.file", cfg.memory.file),
                ("memory.max_context_chars", str(cfg.memory.max_context_chars)),
                ("anthropic_key", "[green]✓ set[/green]" if cfg.get_anthropic_key() else "[red]✗ not set[/red]"),
                ("openai_key", "[green]✓ set[/green]" if cfg.get_openai_key() else "[red]✗ not set[/red]"),
            ]
            for k, v in rows:
                tbl.add_row(k, v)

            console.print(tbl)
            console.print(f"\n[dim]Config file: {CONFIG_FILE}[/dim]")

        elif sub == "edit":
            editor = os.environ.get("EDITOR", "notepad")
            os.system(f'{editor} "{CONFIG_FILE}"')

    def cmd_git(self, args: list[str]) -> None:
        sub = args[0] if args else "log"

        if sub == "log":
            log = self.git.get_log(10)
            if log:
                for entry in log:
                    console.print(f"  {entry}")
            else:
                console.print("[dim]No commits found.[/dim]")

        elif sub == "commit":
            msg = " ".join(args[1:]) or "manual commit"
            sha = self.git.commit_step("manual", msg)
            if sha:
                console.print(f"[green]Committed: {sha}[/green]")
            else:
                console.print("[dim]Nothing to commit.[/dim]")

    def cmd_agents(self) -> None:
        self.cmd_status()

    def cmd_switch(self, args: list[str]) -> None:
        if len(args) < 2:
            console.print("Usage: switch [orchestrator|worker] [claude|gpt]")
            return

        role, provider = args[0].lower(), args[1].lower()
        if provider not in ("claude", "gpt"):
            console.print("[red]Provider must be 'claude' or 'gpt'[/red]")
            return

        if role == "orchestrator":
            self._orch_provider = provider
            console.print(f"[green]Orchestrator → {provider}[/green]")
        elif role == "worker":
            self._worker_provider = provider
            console.print(f"[green]Worker → {provider}[/green]")
        else:
            console.print("[red]Role must be 'orchestrator' or 'worker'[/red]")

        # Rebuild agents with new provider settings
        self._build_agents()

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self._print_banner()

        while True:
            try:
                console.print("[bold cyan]genesis[/bold cyan][dim]>[/dim] ", end="")
                line = input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye.[/dim]")
                break

            if not line:
                continue

            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()

            if not parts:
                continue

            cmd, args = parts[0].lower(), parts[1:]

            if cmd in ("exit", "quit", "q"):
                console.print("[dim]Goodbye.[/dim]")
                break
            elif cmd == "run":
                self.cmd_run(" ".join(args))
            elif cmd == "plan":
                self.cmd_plan(" ".join(args))
            elif cmd == "status":
                self.cmd_status()
            elif cmd == "memory":
                self.cmd_memory(args)
            elif cmd == "config":
                self.cmd_config(args)
            elif cmd == "git":
                self.cmd_git(args)
            elif cmd in ("agents", "agent"):
                self.cmd_agents()
            elif cmd == "switch":
                self.cmd_switch(args)
            elif cmd == "clear":
                console.clear()
            elif cmd == "help":
                console.print(_HELP)
            else:
                console.print(
                    f"[yellow]Unknown command '{cmd}'.[/yellow] "
                    f"Type [cyan]help[/cyan] for available commands."
                )

    def _print_banner(self) -> None:
        key_status = []
        if self.config.get_anthropic_key():
            key_status.append("[green]Claude ✓[/green]")
        else:
            key_status.append("[red]Claude ✗[/red]")
        if self.config.get_openai_key():
            key_status.append("[green]GPT ✓[/green]")
        else:
            key_status.append("[red]GPT ✗[/red]")

        agents_line = "  ·  ".join(key_status)
        agent_names = ", ".join(self._agents.keys()) or "[red]none — set API keys[/red]"

        console.print()
        console.print(
            Panel(
                f"[bold white]GENESIS[/bold white] [dim]v{__version__}[/dim]\n"
                f"[dim]AI Software Development Firm · Terminal Edition[/dim]\n\n"
                f"[cyan]Work dir:[/cyan]  {self.work_dir}\n"
                f"[cyan]Memory:[/cyan]    {self.config.memory.file}\n"
                f"[cyan]Agents:[/cyan]    {agents_line}\n"
                f"[cyan]Active:[/cyan]    {agent_names}\n\n"
                f"[dim]Type [bold]help[/bold] · [bold]run <task>[/bold] to start · [bold]exit[/bold] to quit[/dim]",
                title="[bold magenta]◈ GENESIS[/bold magenta]",
                border_style="magenta",
                padding=(1, 3),
            )
        )
        console.print()
