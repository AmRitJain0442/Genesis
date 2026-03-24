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
from genesis.config import get_config, CONFIG_FILE, CodexAccount
from genesis.memory import MemoryManager
from genesis.git_ops import GitManager
from genesis.agents.base import AgentInfo, BaseAgent
from genesis.agents.claude_cli import ClaudeCodeCLIAgent, find_claude_binary
from genesis.agents.codex_cli import CodexCLIAgent, find_codex_binary
from genesis.agents.orchestrator import Orchestrator
from genesis.ui.console import console
from genesis.ui.dashboard import DashboardState, make_layout

logger = logging.getLogger(__name__)

_HELP = """\
[bold]GENESIS COMMANDS[/bold]

  [bold cyan]run[/bold cyan] [dim]<task>[/dim]                       Execute a task through the AI orchestrator
  [bold cyan]plan[/bold cyan] [dim]<task>[/dim]                      Generate and preview a plan (no execution)
  [bold cyan]status[/bold cyan]                            Show agent info and recent git log
  [bold cyan]memory show[/bold cyan]                       Display the shared memory file
  [bold cyan]memory clear[/bold cyan]                      Reset the memory file
  [bold cyan]memory append[/bold cyan] [dim]<text>[/dim]             Manually add a note to memory
  [bold cyan]config show[/bold cyan]                       Display current configuration
  [bold cyan]config edit[/bold cyan]                       Open config in your editor
  [bold cyan]git log[/bold cyan]                           Show recent Genesis commits
  [bold cyan]git commit[/bold cyan] [dim][message][/dim]             Manually commit current changes
  [bold cyan]agents[/bold cyan]                            List available agents
  [bold cyan]switch orchestrator[/bold cyan] [dim]<claude-cli|codex-cli>[/dim]   Hot-swap orchestrator
  [bold cyan]switch worker[/bold cyan] [dim]<claude-cli|codex-cli>[/dim]        Hot-swap worker
  [bold cyan]add-account[/bold cyan]                       Add a Codex account interactively
  [bold cyan]help[/bold cyan]                              Show this help
  [bold cyan]clear[/bold cyan]                             Clear the terminal
  [bold cyan]exit[/bold cyan]                              Exit Genesis
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
        """Detect available providers and create agent instances."""
        cfg = self.config
        cmd = find_claude_binary() or cfg.claude_cli.command

        # Claude Code CLI agents (no API key needed — uses `claude login` session)
        if find_claude_binary():
            self._agents["claude-cli-orchestrator"] = ClaudeCodeCLIAgent(
                AgentInfo(
                    "claude-cli-orchestrator",
                    "claude-cli",
                    cfg.orchestrator.model if self._orch_provider == "claude-cli" else "claude-opus-4-6",
                    max_tokens=8096,
                ),
                command=cmd,
                timeout=cfg.claude_cli.timeout,
            )
            self._agents["claude-cli-worker"] = ClaudeCodeCLIAgent(
                AgentInfo(
                    "claude-cli-worker",
                    "claude-cli",
                    cfg.worker.model if self._worker_provider == "claude-cli" else "claude-sonnet-4-6",
                    max_tokens=8096,
                ),
                command=cmd,
                timeout=cfg.claude_cli.timeout,
            )

        # Codex CLI agents (no API key — uses `codex login` / ChatGPT Pro OAuth)
        codex_cmd = find_codex_binary()
        if codex_cmd:
            accounts = cfg.codex_cli.accounts or [
                CodexAccount(name="codex-main", home="", model=cfg.codex_cli.model)
            ]
            for i, account in enumerate(accounts):
                model = account.model if account.model != "auto" else "auto"
                # Orchestrator slot: first account only (claude-cli preferred)
                if i == 0:
                    self._agents["codex-orchestrator"] = CodexCLIAgent(
                        AgentInfo("codex-orchestrator", "codex-cli", model, max_tokens=8096),
                        command=codex_cmd,
                        timeout=cfg.codex_cli.timeout,
                        work_dir=self.work_dir,
                        codex_home=account.home,
                    )
                # Every account registers as a worker
                worker_key = account.name if account.name else f"codex-worker-{i+1}"
                self._agents[worker_key] = CodexCLIAgent(
                    AgentInfo(worker_key, "codex-cli", model, max_tokens=8096),
                    command=codex_cmd,
                    timeout=cfg.codex_cli.timeout,
                    work_dir=self.work_dir,
                    codex_home=account.home,
                )

        # ChatGPT browser agent (optional — requires playwright)
        if cfg.chatgpt_browser.enabled:
            try:
                from genesis.agents.chatgpt_browser import ChatGPTBrowserAgent
                self._agents["chatgpt-orchestrator"] = ChatGPTBrowserAgent(
                    AgentInfo("chatgpt-orchestrator", "chatgpt-browser",
                              cfg.chatgpt_browser.model, max_tokens=4096),
                    headless=cfg.chatgpt_browser.headless,
                    profile_dir=cfg.chatgpt_browser.profile_dir,
                )
                self._agents["chatgpt-worker"] = ChatGPTBrowserAgent(
                    AgentInfo("chatgpt-worker", "chatgpt-browser",
                              cfg.chatgpt_browser.model, max_tokens=4096),
                    headless=cfg.chatgpt_browser.headless,
                    profile_dir=cfg.chatgpt_browser.profile_dir,
                )
            except Exception as e:
                logger.warning("ChatGPT browser agent failed to load: %s", e)

    def _get_orchestrator(self) -> BaseAgent | None:
        # Prefer configured provider, then Claude (best for JSON reasoning), then Codex
        for key in (
            f"{self._orch_provider}-orchestrator",
            "claude-cli-orchestrator",
            "codex-orchestrator",
            "chatgpt-orchestrator",
        ):
            if key in self._agents:
                return self._agents[key]
        return None

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
                "[red]No agents available.[/red]\n"
                "Make sure [bold]Claude Code[/bold] is installed and logged in:\n"
                "  [cyan]claude login[/cyan]\n"
                "Then run [cyan]genesis status[/cyan] to verify."
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
                refresh_per_second=8,
                screen=False,
            ) as live:
                def make_cb(fn):
                    def wrapped(*args, **kwargs):
                        fn(*args, **kwargs)
                        live.update(make_layout(state))
                    return wrapped

                callbacks = {k: make_cb(v) for k, v in raw_callbacks.items()}

                # on_output fires very frequently (every streaming line) — just
                # update state without forcing a redraw; Live's timer handles it.
                def on_output(line: str) -> None:
                    state.add_output(f"  {line}")

                callbacks["on_output"] = on_output

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

            claude_bin = find_claude_binary()
            codex_bin = find_codex_binary()
            rows = [
                ("orchestrator.provider", cfg.orchestrator.provider),
                ("orchestrator.model", cfg.orchestrator.model),
                ("worker.provider", cfg.worker.provider),
                ("worker.model", cfg.worker.model),
                ("claude_cli.command", claude_bin or f"[red]not found[/red]"),
                ("codex_cli.command", codex_bin or "[dim]not found[/dim]"),
                ("codex_cli.model", cfg.codex_cli.model),
                ("chatgpt_browser.enabled", "[green]yes[/green]" if cfg.chatgpt_browser.enabled else "no"),
                ("git.auto_commit", str(cfg.git.auto_commit)),
                ("git.auto_push", str(cfg.git.auto_push)),
                ("git.commit_prefix", cfg.git.commit_prefix),
                ("memory.file", cfg.memory.file),
                ("memory.max_context_chars", str(cfg.memory.max_context_chars)),
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

    def cmd_add_account(self, args: list[str]) -> None:
        """
        Interactively add a new Codex account to ~/.genesis/config.toml.

        Usage: add-account
        """
        import subprocess as _sp

        console.print("\n[bold]Add a Codex Account[/bold]")
        console.print("Each account needs its own CODEX_HOME directory.\n")

        console.print("[cyan]Account name[/cyan] (e.g. codex-pro2): ", end="")
        name = input().strip()
        if not name:
            console.print("[red]Name cannot be empty.[/red]")
            return

        default_home = str(Path.home() / f".codex-{name}")
        console.print(f"[cyan]CODEX_HOME directory[/cyan] (default: {default_home}): ", end="")
        home = input().strip() or default_home

        home_path = Path(home)
        if not home_path.exists():
            home_path.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]Created directory:[/green] {home}")

        console.print(f"\n[dim]Now logging in to Codex with CODEX_HOME={home}[/dim]")
        console.print("[dim]A browser window will open — log in with your second ChatGPT account.[/dim]\n")

        # Determine the codex binary
        codex_cmd = find_codex_binary() or "codex"
        env = os.environ.copy()
        env["CODEX_HOME"] = home

        login_cmd = [codex_cmd, "login"]
        if os.name == "nt" and codex_cmd.lower().endswith((".cmd", ".bat")):
            login_cmd = ["cmd", "/c"] + login_cmd

        try:
            _sp.run(login_cmd, env=env, check=False)
        except Exception as e:
            console.print(f"[red]Login failed: {e}[/red]")
            return

        # Verify login succeeded
        status_cmd = [codex_cmd, "login", "status"]
        if os.name == "nt" and codex_cmd.lower().endswith((".cmd", ".bat")):
            status_cmd = ["cmd", "/c"] + status_cmd

        check = _sp.run(status_cmd, env=env, capture_output=True, text=True)
        if check.returncode != 0:
            console.print("[red]Login did not complete. Account not added.[/red]")
            return

        # Append to config.toml — always use forward slashes (TOML requires it)
        home_toml = str(Path(home)).replace("\\", "/")
        entry = (
            f"\n[[codex_cli.accounts]]\n"
            f'name = "{name}"\n'
            f'home = "{home_toml}"\n'
            f'model = "auto"\n'
        )

        try:
            with open(CONFIG_FILE, "a", encoding="utf-8") as f:
                f.write(entry)
            console.print(f"\n[green]✓ Account '{name}' added to config.[/green]")
            console.print("[dim]Rebuilding agents…[/dim]")
            from genesis.config import reset_config_cache
            reset_config_cache()
            self.config = get_config()
            self._build_agents()
            self.cmd_status()
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            console.print(f"Add this manually to {CONFIG_FILE}:\n{entry}")

    def cmd_switch(self, args: list[str]) -> None:
        if len(args) < 2:
            console.print("Usage: switch [orchestrator|worker] [claude-cli|chatgpt]")
            return

        role, provider = args[0].lower(), args[1].lower()
        valid = ("claude-cli", "codex", "codex-cli", "chatgpt", "chatgpt-browser")
        if provider not in valid:
            console.print(f"[red]Provider must be one of: claude-cli, codex, chatgpt[/red]")
            return

        # Normalise aliases
        if provider == "chatgpt":
            provider = "chatgpt-browser"
        elif provider == "codex":
            provider = "codex-cli"

        if role == "orchestrator":
            self._orch_provider = provider
            console.print(f"[green]Orchestrator → {provider}[/green]")
        elif role == "worker":
            self._worker_provider = provider
            console.print(f"[green]Worker → {provider}[/green]")
        else:
            console.print("[red]Role must be 'orchestrator' or 'worker'[/red]")
            return

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
            elif cmd in ("add-account", "add_account", "addaccount"):
                self.cmd_add_account(args)
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
        if find_claude_binary():
            key_status.append("[green]Claude Code ✓[/green]")
        else:
            key_status.append("[red]Claude Code ✗[/red]")
        if find_codex_binary():
            key_status.append("[green]Codex ✓[/green]")
        else:
            key_status.append("[dim]Codex —[/dim]")
        if self.config.chatgpt_browser.enabled:
            key_status.append("[cyan]ChatGPT browser ✓[/cyan]")

        agents_line = "  ·  ".join(key_status)
        agent_names = ", ".join(self._agents.keys()) or "[red]none available[/red]"

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
