from __future__ import annotations
import os
import logging
import re
from pathlib import Path

from rich.live import Live
from rich.console import Group
from rich.markdown import Markdown

from genesis import __version__
from genesis.config import get_config, CONFIG_FILE, CodexAccount, ClaudeAccount
from genesis.memory import MemoryManager
from genesis.git_ops import GitManager
from genesis.palace import PalaceStore, resolve_state_db
from genesis.policy import ExecutionPolicy
from genesis.runtime import RuntimeStore
from genesis.worktree import WorktreeManager
from genesis.agents.base import AgentInfo, BaseAgent
from genesis.agents.claude_cli import ClaudeCodeCLIAgent, find_claude_binary
from genesis.agents.codex_cli import CodexCLIAgent, find_codex_binary
from genesis.agents.orchestrator import Orchestrator
from genesis.agents.availability import AccountRegistry
from genesis.chatroom import ChatroomManager, ChatroomServer, RoomKind
from genesis.ui.console import console
from genesis.ui.dashboard import DashboardState, make_layout
from genesis.ui.theme import command_panel, command_table, kv_table, markup, progress_bar, status_label, trim

logger = logging.getLogger(__name__)

_HELP_SECTIONS = [
    (
        "Run Control",
        [
            ("run <task>", "Execute a task through the AI orchestrator"),
            ("plan <task>", "Generate and preview a plan without execution"),
            ("resume <run_id>", "Resume a durable run"),
            ("retry <run_id> <step_id>", "Retry a blocked step, then resume"),
            ("runs", "Show recent durable runs"),
            ("inspect <run_id>", "Show run state and event trace"),
            ("cleanup <run_id>", "Remove stale worktrees for a run"),
        ],
    ),
    (
        "Memory",
        [
            ("memory show", "Display the shared memory file"),
            ("memory search <query>", "Search SQLite palace memory"),
            ("memory mine", "Import GENESIS_MEMORY.md into palace memory"),
            ("memory clear", "Reset the memory file"),
            ("memory append <text>", "Manually add a note to memory"),
        ],
    ),
    (
        "Configuration",
        [
            ("status", "Show agent info and recent git log"),
            ("agents", "List available agents"),
            ("config show", "Display current configuration"),
            ("config edit", "Open config in your editor"),
            ("switch orchestrator <claude-cli|codex-cli>", "Hot-swap the orchestrator"),
            ("switch worker <claude-cli|codex-cli>", "Hot-swap the default worker"),
        ],
    ),
    (
        "Accounts",
        [
            ("add-account", "Add a Codex account interactively"),
            ("remove-account <name>", "Remove one Codex account from Genesis"),
            ("remove-all-accounts", "Remove every Codex account from Genesis"),
            ("remove-all-accounts --delete-home", "Also delete non-default CODEX_HOME folders"),
            ("add-claude-account", "Add a Claude account interactively"),
            ("remove-claude-account <name>", "Remove one Claude account from Genesis"),
            ("remove-all-claude-accounts", "Remove every Claude account from Genesis"),
            ("remove-all-claude-accounts --delete-config-dir", "Also delete non-default CLAUDE_CONFIG_DIR folders"),
        ],
    ),
    (
        "Utility",
        [
            ("git log", "Show recent Genesis commits"),
            ("git commit [message]", "Manually commit current changes"),
            ("help", "Show this command index"),
            ("clear", "Clear the terminal"),
            ("exit", "Quit Genesis"),
        ],
    ),
]


def _help_renderable() -> Group:
    parts: list[object] = [
        "[dim]Type a command exactly as shown. Values in angle brackets are required; values in square brackets are optional.[/dim]"
    ]

    for title, rows in _HELP_SECTIONS:
        tbl = command_table(title, border_style="cyan", expand=True)
        tbl.add_column("Command", ratio=2, min_width=24, overflow="fold")
        tbl.add_column("Action", ratio=3, min_width=32, overflow="fold")
        for command, description in rows:
            tbl.add_row(f"[bold cyan]{markup(command)}[/bold cyan]", markup(description))
        parts.append(tbl)

    return Group(*parts)


_ACCOUNT_NAME_RE = re.compile(
    r"""^\s*name\s*=\s*(?P<quote>["'])(?P<name>.*?)(?P=quote)\s*(?:#.*)?$"""
)
_EMPTY_ACCOUNTS_RE = re.compile(r"^\s*accounts\s*=\s*\[\s*\]\s*(?:#.*)?$")


def _is_active_toml_header(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("[") and not stripped.startswith("#")


def _is_account_header(line: str, section: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(f"[[{section}.accounts]]") and not stripped.startswith("#")


def _is_commented_account_header(line: str, section: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(f"# [[{section}.accounts]]") or stripped.startswith(f"#[[{section}.accounts]]")


def _is_cli_section(line: str, section: str) -> bool:
    return bool(re.match(rf"^\s*\[{re.escape(section)}\]\s*(?:#.*)?$", line))


def _extract_account_name(block: list[str]) -> str | None:
    for line in block:
        match = _ACCOUNT_NAME_RE.match(line)
        if match:
            return match.group("name")
    return None


def _remove_empty_account_markers(lines: list[str], section: str) -> list[str]:
    cleaned: list[str] = []
    in_section = False
    for line in lines:
        if _is_active_toml_header(line):
            in_section = _is_cli_section(line, section)
        if in_section and _EMPTY_ACCOUNTS_RE.match(line):
            continue
        cleaned.append(line)
    return cleaned


def _line_ending(lines: list[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
    return "\n"


def _ensure_empty_accounts_marker(lines: list[str], section: str, label: str) -> list[str]:
    lines = _remove_empty_account_markers(lines, section)
    ending = _line_ending(lines)
    marker = f"accounts = []                # no {label} accounts registered in Genesis{ending}"

    for index, line in enumerate(lines):
        if _is_cli_section(line, section):
            lines.insert(index + 1, marker)
            return lines

    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = f"{lines[-1]}{ending}"
    if lines and lines[-1].strip():
        lines.append(ending)
    lines.extend((f"[{section}]{ending}", marker))
    return lines


def _rewrite_accounts_text(
    config_text: str,
    section: str,
    label: str,
    *,
    remove_names: set[str] | None = None,
    remove_all: bool = False,
) -> tuple[str, list[str], list[str], list[str]]:
    """Remove active [[<section>.accounts]] blocks while preserving other TOML text."""
    names = remove_names or set()
    lines = config_text.splitlines(keepends=True)
    rewritten: list[str] = []
    seen: list[str] = []
    removed: list[str] = []
    remaining: list[str] = []

    index = 0
    while index < len(lines):
        if not _is_account_header(lines[index], section):
            rewritten.append(lines[index])
            index += 1
            continue

        block_start = index
        index += 1
        while (
            index < len(lines)
            and not _is_active_toml_header(lines[index])
            and not _is_commented_account_header(lines[index], section)
        ):
            index += 1

        block = lines[block_start:index]
        account_name = _extract_account_name(block)
        if account_name:
            seen.append(account_name)

        should_remove = remove_all or (account_name in names if account_name else False)
        if should_remove:
            removed.append(account_name or "<unnamed>")
            continue

        if account_name:
            remaining.append(account_name)
        rewritten.extend(block)

    rewritten = _remove_empty_account_markers(rewritten, section)
    if not remaining:
        rewritten = _ensure_empty_accounts_marker(rewritten, section, label)

    return "".join(rewritten), removed, seen, remaining


def _rewrite_codex_accounts_text(
    config_text: str,
    *,
    remove_names: set[str] | None = None,
    remove_all: bool = False,
) -> tuple[str, list[str], list[str], list[str]]:
    return _rewrite_accounts_text(
        config_text, "codex_cli", "Codex",
        remove_names=remove_names, remove_all=remove_all,
    )


def _rewrite_claude_accounts_text(
    config_text: str,
    *,
    remove_names: set[str] | None = None,
    remove_all: bool = False,
) -> tuple[str, list[str], list[str], list[str]]:
    return _rewrite_accounts_text(
        config_text, "claude_cli", "Claude",
        remove_names=remove_names, remove_all=remove_all,
    )


class GenesisREPL:
    def __init__(self) -> None:
        self.config = get_config()
        self.work_dir = str(Path.cwd())
        self.memory = MemoryManager(
            str(Path(self.work_dir) / self.config.memory.file)
        )
        self.git = GitManager(self.work_dir, self.config.git)
        self.runtime = RuntimeStore.from_config(self.config)
        self.palace = PalaceStore.from_config(self.config) if self.config.memory.palace_enabled else None
        self.policy = ExecutionPolicy.load(self.work_dir, self.config)
        self._orch_provider = self.config.orchestrator.provider
        self._worker_provider = self.config.worker.provider
        self._agents: dict[str, BaseAgent] = {}
        self._build_agents()

        # Chatroom substrate — a shared, observable message bus. The web viewer
        # is created lazily on the first run so idle sessions open no ports.
        persist_dir = None
        if self.config.chatroom.persist:
            persist_dir = str(Path(resolve_state_db(self.config)).parent / "chatrooms")
        self.chatroom = ChatroomManager(persist_dir=persist_dir)
        self.chatroom_server: ChatroomServer | None = None

        # Shared across runs so a rate/usage-limited account stays skipped for its
        # cooldown while another account takes over.
        self.registry = AccountRegistry(self.config.failover.cooldown_seconds)

    # ── Agent construction ─────────────────────────────────────────────────

    def _build_agents(self) -> None:
        """Detect available providers and create agent instances."""
        self._agents = {}
        cfg = self.config
        cmd = find_claude_binary() or cfg.claude_cli.command

        # Claude Code CLI agents (no API key needed — uses `claude auth login`
        # session). Each account gets its own CLAUDE_CONFIG_DIR so multiple
        # logins stay isolated from the default ~/.claude session.
        if find_claude_binary():
            claude_accounts = cfg.claude_cli.accounts
            if not claude_accounts and not cfg.claude_cli.accounts_explicit:
                claude_accounts = [ClaudeAccount(name="claude-main", config_dir="", model="")]
            for i, account in enumerate(claude_accounts):
                real_model = account.model if account.model not in ("", "auto", "default") else ""
                orch_model = real_model or (
                    cfg.orchestrator.model if self._orch_provider == "claude-cli" else "claude-sonnet-4-6"
                )
                worker_model = real_model or (
                    cfg.worker.model if self._worker_provider == "claude-cli" else "claude-sonnet-4-6"
                )
                # Orchestrator slot: first account only
                if i == 0:
                    self._agents["claude-cli-orchestrator"] = ClaudeCodeCLIAgent(
                        AgentInfo("claude-cli-orchestrator", "claude-cli", orch_model, max_tokens=8096),
                        command=cmd,
                        timeout=cfg.claude_cli.timeout,
                        config_dir=account.config_dir,
                    )
                # Every account registers as a worker
                worker_key = account.name if account.name else f"claude-worker-{i+1}"
                self._agents[worker_key] = ClaudeCodeCLIAgent(
                    AgentInfo(worker_key, "claude-cli", worker_model, max_tokens=8096),
                    command=cmd,
                    timeout=cfg.claude_cli.timeout,
                    config_dir=account.config_dir,
                )

        # Codex CLI agents (no API key — uses `codex login` / ChatGPT Pro OAuth)
        codex_cmd = find_codex_binary()
        if codex_cmd:
            accounts = cfg.codex_cli.accounts
            if not accounts and not cfg.codex_cli.accounts_explicit:
                accounts = [CodexAccount(name="codex-main", home="", model=cfg.codex_cli.model)]
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
        return {k: v for k, v in self._agents.items() if "orchestrator" not in k}

    def _get_co_brain(self, primary: BaseAgent) -> BaseAgent | None:
        """The second brain (a different orchestrator agent) for collaborative
        planning — the one that isn't the primary. None if only one brain."""
        for key in ("claude-cli-orchestrator", "codex-orchestrator", "chatgpt-orchestrator"):
            agent = self._agents.get(key)
            if agent is not None and agent is not primary:
                return agent
        return None

    def _make_orchestrator(self) -> Orchestrator | None:
        orch = self._get_orchestrator()
        workers = self._get_workers()
        if not orch or not workers:
            return None
        co_brain = self._get_co_brain(orch) if self.config.collaboration.enabled else None
        return Orchestrator(
            orch,
            workers,
            self.memory,
            self.git,
            self.config,
            self.work_dir,
            runtime=self.runtime,
            palace=self.palace,
            policy=self.policy,
            co_brain=co_brain,
            chatroom=self.chatroom,
            registry=self.registry,
        )

    # ── Commands ───────────────────────────────────────────────────────────

    # ── Chatroom viewer ─────────────────────────────────────────────────────

    def _ensure_chatroom_server(self) -> str | None:
        """Start the localhost viewer on first use; return its URL (or None)."""
        if not self.config.chatroom.enabled:
            return None
        if self.chatroom_server is None:
            self.chatroom_server = ChatroomServer(
                self.chatroom,
                host=self.config.chatroom.host,
                port=self.config.chatroom.port,
            )
        try:
            url = self.chatroom_server.start()
        except OSError as e:
            logger.warning("Chatroom viewer failed to start: %s", e)
            self.chatroom_server = None
            return None
        if self.config.chatroom.open_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        return url

    def _stop_chatroom_server(self) -> None:
        if self.chatroom_server is not None:
            self.chatroom_server.stop()
            self.chatroom_server = None

    def _bridge_callbacks_to_chatroom(self, raw: dict, task: str) -> dict:
        """Wrap orchestrator callbacks so every coordination event is also posted
        to a chatroom — making the flow observable in the web viewer. Posting
        must never break a run, so all failures are swallowed."""
        if not self.config.chatroom.enabled:
            return raw

        orch = self._get_orchestrator()
        brain = getattr(orch, "name", "brain") or "brain"
        room = self.chatroom.create_room(
            RoomKind.system, task[:60] or "run", participants=list(self._agents.keys())
        )

        def say(sender: str, role: str, content: str, kind: str = "message") -> None:
            try:
                self.chatroom.post(room.id, sender, role, content, kind)
            except Exception:
                pass

        def _files(result) -> str:
            written = getattr(result, "files_written", None) or []
            head = f"wrote {len(written)} file(s)"
            return head + (": " + ", ".join(written) if written else "")

        posters = {
            "on_plan": lambda plan, *a: say(
                brain, "brain", f"Planned {len(plan.steps)} steps — {plan.task_summary}", "decision"),
            "on_step_start": lambda step, idx, total, *a: say(
                brain, "brain", f"> Step {step.step_id}: {step.title}", "status"),
            "on_worker_assigned": lambda step, worker, *a: say(
                brain, "brain", f"Assigned {worker} -> {step.step_id}", "status"),
            "on_step_result": lambda step, result, worker, *a: say(
                worker, "worker", f"{step.step_id}: {_files(result)}", "code"),
            "on_review": lambda step, review, *a: say(
                "reviewer", "reviewer",
                f"{step.step_id}: {review.verdict} ({review.quality_score}/10) — {review.feedback}",
                "decision"),
            "on_commit": lambda step, sha, *a: say(
                brain, "brain", f"Committed {step.step_id}" + (f" @ {sha}" if sha else ""), "decision"),
            "on_status": lambda msg, *a: say(brain, "system", str(msg), "status"),
            "on_error": lambda step, error, *a: say(
                brain, "system", f"{getattr(step, 'step_id', '?')}: ERROR {error}", "status"),
            "on_task_complete": lambda plan, *a: say(brain, "brain", "Task complete", "decision"),
        }

        def compose(name, original):
            poster = posters.get(name)
            if poster is None:
                return original

            def wrapped(*args, **kwargs):
                try:
                    poster(*args, **kwargs)
                except Exception:
                    pass
                return original(*args, **kwargs)

            return wrapped

        return {k: compose(k, v) for k, v in raw.items()}

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

        from datetime import datetime as _dt
        all_agent_names = list(self._agents.keys())
        state = DashboardState(agent_names=all_agent_names)
        state.task_name = task[:80] + ("..." if len(task) > 80 else "")

        # ── Callback closures ────────────────────────────────────────────
        def on_plan(plan):
            state.run_phase = "planning"
            state.plan = plan
            state.total = len(plan.steps)
            for s in plan.steps:
                state.step_statuses[s.step_id] = "pending"
                scope = ", ".join(getattr(s, "file_scope", []) or []) or s.context_hint or "*"
                state.step_scopes[s.step_id] = scope
            state.add_event("plan", f"{len(plan.steps)} steps", "cyan")

        def on_step_start(step, idx, total):
            state.run_phase = "running"
            state.current_step = step.step_id
            state.step_start = _dt.now()
            state.step_statuses[step.step_id] = "running"
            state.add_event("start", f"{step.step_id} {step.title}", "cyan")
            state.add_output(
                f"[cyan]STEP[/cyan] [bold]{markup(step.step_id)}[/bold] {markup(step.title)}",
                trusted_markup=True,
            )

        def on_worker_assigned(step, worker_name):
            state.current_worker = worker_name
            state.step_workers[step.step_id] = worker_name
            state.add_event("lease", f"{worker_name} -> {step.step_id}", "green")
            state.add_output(
                f"  [dim]worker[/dim] [cyan]{markup(worker_name)}[/cyan]",
                trusted_markup=True,
            )

        def on_step_result(step, result, worker_name):
            if result.files_written:
                state.add_event("files", f"{step.step_id}: {len(result.files_written)} changed", "green")
                for f in result.files_written:
                    state.add_output(f"  [green]+[/green] {markup(f)}", trusted_markup=True)

        def on_review(step, review):
            state.run_phase = "reviewing"
            state.current_reviewer = "independent-reviewer"
            state.step_reviewers[step.step_id] = "independent-reviewer"
            color = "green" if review.verdict == "approved" else "yellow" if review.verdict == "needs_revision" else "red"
            icon  = "+" if review.verdict == "approved" else "~" if review.verdict == "needs_revision" else "x"
            state.add_output(
                f"  [{color}]{icon} {review.verdict}[/{color}] "
                f"[dim]score:{review.quality_score}/10[/dim]",
                trusted_markup=True,
            )
            state.step_statuses[step.step_id] = review.verdict
            state.add_event("review", f"{step.step_id}: {review.verdict} {review.quality_score}/10", color)

        def on_commit(step, sha):
            state.run_phase = "committing"
            if sha:
                state.git_sha = sha
                state.add_output(f"  [dim]git:{markup(sha)}[/dim]", trusted_markup=True)
                state.add_event("commit", f"{step.step_id}: {sha}", "green")
            state.step_statuses[step.step_id] = "committed"

        def on_step_complete(step, review, completed, total):
            state.completed = completed
            state.step_statuses[step.step_id] = "committed"
            if state.step_start:
                elapsed = (_dt.now() - state.step_start).total_seconds()
                state.step_elapsed[step.step_id] = elapsed
            state.step_start = None
            state.current_worker = ""
            state.current_reviewer = ""
            state.step_verification[step.step_id] = "passed"
            state.add_event("done", f"{step.step_id} complete", "green")

        def on_status(msg):
            lowered = str(msg).lower()
            if "planning" in lowered:
                state.run_phase = "planning"
            elif "executing" in lowered:
                state.run_phase = "running"
            elif "repair" in lowered or "retry" in lowered:
                state.run_phase = "repairing"
                if state.current_step:
                    state.step_repairs[state.current_step] = state.step_repairs.get(state.current_step, 0) + 1
            elif "blocked" in lowered or "no runnable" in lowered:
                state.run_phase = "blocked"
                state.blocked_reason = msg
            state.add_event("status", trim(msg, 80), "dim")
            state.add_output(f"[dim]{markup(msg)}[/dim]", trusted_markup=True)

        def on_error(step, error):
            state.run_phase = "blocked"
            state.blocked_reason = error
            state.add_output(f"  [red]x {markup(error)}[/red]", trusted_markup=True)
            state.step_statuses[step.step_id] = "blocked"
            state.current_worker = ""
            state.add_event("error", f"{step.step_id}: {error}", "red")

        def on_task_complete(plan):
            state.run_phase = "completed"
            state.add_output(
                f"\n[bold green]+ Task complete  {state.completed}/{state.total} steps[/bold green]",
                trusted_markup=True,
            )
            state.add_event("release", "run complete", "green")

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

        # Observability: start the localhost viewer and mirror every coordination
        # event into a chatroom before wiring the Live display on top.
        chat_url = self._ensure_chatroom_server()
        raw_callbacks = self._bridge_callbacks_to_chatroom(raw_callbacks, task)
        if chat_url:
            console.print(f"[dim]Watch the agents live:[/dim] [cyan]{chat_url}[/cyan]")

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

                # on_output fires on every streaming line — update state only;
                # Live's timer handles redraw. Also extract token counts.
                def on_output(line: str) -> None:
                    lowered = str(line).lower()
                    if "verify $" in lowered:
                        state.run_phase = "verifying"
                        if state.current_step:
                            state.step_verification[state.current_step] = "running"
                        state.add_event("verify", trim(line, 80), "blue")
                    elif "retrying" in lowered or "repairing" in lowered:
                        state.run_phase = "repairing"
                        if state.current_step:
                            state.step_repairs[state.current_step] = state.step_repairs.get(state.current_step, 0) + 1
                        state.add_event("repair", trim(line, 80), "yellow")
                    state.add_output(f"  {line}")
                    if "Tokens:" in line:
                        state.record_token_line(line)

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

        console.print(command_panel(f"[bold]{markup(task, 120)}[/bold]", "PLAN PREVIEW", border_style="cyan"))
        try:
            plan = orchestrator.plan(task)
        except Exception as e:
            console.print(f"[red]Planning failed: {e}[/red]")
            return

        tbl = command_table(f"Plan: {plan.task_summary}", border_style="blue", show_lines=True)
        tbl.add_column("#", style="dim", width=8, no_wrap=True)
        tbl.add_column("Title", width=32)
        tbl.add_column("Type", width=10)
        tbl.add_column("Agent", width=16)
        tbl.add_column("Depends On", width=14)
        tbl.add_column("Scope", width=24)

        for s in plan.steps:
            deps = ", ".join(s.depends_on) if s.depends_on else "-"
            scope = ", ".join(s.file_scope) if getattr(s, "file_scope", []) else s.context_hint or "*"
            tbl.add_row(
                markup(s.step_id),
                markup(s.title),
                markup(s.type),
                markup(s.preferred_agent),
                markup(deps),
                markup(scope, 24),
            )

        console.print(tbl)

    def cmd_status(self) -> None:
        agent_tbl = command_table("Agent Roster", border_style="magenta")
        agent_tbl.add_column("Role", width=12)
        agent_tbl.add_column("Name", style="cyan")
        agent_tbl.add_column("Provider", width=14)
        agent_tbl.add_column("Model", width=22)
        agent_tbl.add_column("State", width=10)

        for name, agent in self._agents.items():
            role = "orchestrator" if "orchestrator" in name else "worker"
            agent_tbl.add_row(
                "ORCH" if role == "orchestrator" else "WORK",
                markup(name),
                markup(agent.provider),
                markup(agent.model),
                status_label("idle"),
            )

        if not self._agents:
            agent_tbl.add_row("-", "[dim]none[/dim]", "-", "-", status_label("blocked"))

        cfg = self.config
        ops_tbl = kv_table(
            [
                ("work_dir", self.work_dir),
                ("state_db", resolve_state_db(cfg)),
                ("max_parallel", cfg.runtime.max_parallel_workers),
                ("retry_budget", cfg.runtime.retry_budget),
                ("verification", ", ".join(cfg.verification.commands) or "not configured"),
                ("auto_commit", cfg.git.auto_commit),
                ("auto_push", cfg.git.auto_push),
            ],
            title="Runtime Controls",
            border_style="cyan",
        )

        console.print(Group(agent_tbl, ops_tbl))

        log = self.git.get_log(5)
        if log:
            commit_tbl = command_table("Recent Commits", border_style="green")
            commit_tbl.add_column("Commit")
            for entry in log:
                commit_tbl.add_row(markup(entry))
            console.print(commit_tbl)

        runs = self.runtime.latest_runs(5)
        if runs:
            run_tbl = command_table("Recent Runs", border_style="blue")
            run_tbl.add_column("Run", style="cyan", width=14)
            run_tbl.add_column("State", width=11)
            run_tbl.add_column("Updated", style="dim", width=20)
            run_tbl.add_column("Task")
            for run in runs:
                run_tbl.add_row(
                    markup(run.run_id),
                    status_label(run.status),
                    markup(run.updated_at[:19]),
                    markup(run.task, 84),
                )
            console.print(run_tbl)

    def cmd_memory(self, args: list[str]) -> None:
        sub = args[0] if args else "show"

        if sub == "show":
            console.print(Markdown(self.memory.read()))
        elif sub == "search" and len(args) > 1:
            if not self.palace:
                console.print("[yellow]Palace memory is disabled.[/yellow]")
                return
            query = " ".join(args[1:])
            hits = self.palace.search(query, wing=str(Path(self.work_dir).resolve()), limit=8)
            if not hits:
                console.print("[dim]No memory hits.[/dim]")
                return
            tbl = command_table(f"Memory Search: {query}", border_style="cyan")
            tbl.add_column("When", style="dim", width=20)
            tbl.add_column("Scope", style="cyan", width=24)
            tbl.add_column("Title")
            tbl.add_column("Kind", width=16)
            for hit in hits:
                tbl.add_row(
                    markup(hit.created_at[:19]),
                    markup(f"{hit.room}/{hit.closet}", 24),
                    markup(hit.title, 70),
                    markup(hit.kind),
                )
            console.print(tbl)
        elif sub == "mine":
            if not self.palace:
                console.print("[yellow]Palace memory is disabled.[/yellow]")
                return
            count = self.palace.import_markdown(
                Path(self.work_dir) / self.config.memory.file,
                wing=str(Path(self.work_dir).resolve()),
            )
            console.print(f"[green]Imported {count} memory document(s).[/green]")
        elif sub == "clear":
            console.print("[yellow]Reset GENESIS_MEMORY.md? This cannot be undone. (y/N): [/yellow]", end="")
            if input().strip().lower() == "y":
                self.memory.clear()
                console.print("[green]Memory cleared.[/green]")
        elif sub == "append" and len(args) > 1:
            note = " ".join(args[1:])
            self.memory.append_note(note)
            if self.palace:
                self.palace.add_drawer(
                    wing=str(Path(self.work_dir).resolve()),
                    room="manual",
                    closet="notes",
                    kind="manual-note",
                    title="Manual note",
                    content=note,
                    source="genesis-repl",
                )
            console.print("[green]Note added.[/green]")
        else:
            console.print("Usage: memory [show | search <query> | mine | clear | append <text>]")

    def cmd_config(self, args: list[str]) -> None:
        sub = args[0] if args else "show"

        if sub == "show":
            cfg = self.config
            tbl = command_table("Genesis Configuration", border_style="cyan")
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
                ("runtime.state_db", str(resolve_state_db(cfg))),
                ("runtime.retry_budget", str(cfg.runtime.retry_budget)),
                ("runtime.max_parallel_workers", str(cfg.runtime.max_parallel_workers)),
                ("memory.file", cfg.memory.file),
                ("memory.max_context_chars", str(cfg.memory.max_context_chars)),
                ("memory.palace_enabled", str(cfg.memory.palace_enabled)),
                ("verification.commands", ", ".join(cfg.verification.commands) or "[dim]none[/dim]"),
                ("policy.file", cfg.policy.file),
            ]
            for k, v in rows:
                tbl.add_row(markup(k), str(v))

            console.print(tbl)
            console.print(command_panel(markup(CONFIG_FILE), "CONFIG FILE", border_style="blue"))

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

    def cmd_runs(self) -> None:
        runs = self.runtime.latest_runs(20)
        if not runs:
            console.print("[dim]No durable runs recorded yet.[/dim]")
            return
        tbl = command_table("Mission Control: Recent Runs", border_style="blue")
        tbl.add_column("Run ID", style="cyan", width=14)
        tbl.add_column("Status", width=12)
        tbl.add_column("Progress", width=12)
        tbl.add_column("Updated", style="dim", width=20)
        tbl.add_column("Blocker", width=26)
        tbl.add_column("Task")
        for run in runs:
            completed = run.metadata.get("completed_steps", 0)
            total = run.metadata.get("total_steps", run.metadata.get("estimated_steps", 0))
            progress = f"{completed}/{total}" if total else "-"
            blocker = run.metadata.get("reason") or run.metadata.get("blocked_step") or ""
            tbl.add_row(
                markup(run.run_id),
                status_label(run.status),
                markup(progress),
                markup(run.updated_at[:19]),
                markup(blocker, 26),
                markup(run.task, 80),
            )
        console.print(tbl)

    def cmd_resume(self, run_id: str) -> None:
        if not run_id:
            console.print("[red]Usage: resume <run_id>[/red]")
            return
        orchestrator = self._make_orchestrator()
        if not orchestrator:
            console.print("[red]No agents available.[/red]")
            return
        callbacks = self._simple_callbacks()
        try:
            orchestrator.resume_task(run_id, callbacks)
        except Exception as e:
            console.print(f"[red]Resume failed:[/red] {e}")

    def cmd_retry(self, args: list[str]) -> None:
        if len(args) < 2:
            console.print("[red]Usage: retry <run_id> <step_id>[/red]")
            return
        run_id, step_id = args[0], args[1]
        orchestrator = self._make_orchestrator()
        if not orchestrator:
            console.print("[red]No agents available.[/red]")
            return
        callbacks = self._simple_callbacks()
        try:
            orchestrator.resume_task(run_id, callbacks, retry_step_id=step_id)
        except Exception as e:
            console.print(f"[red]Retry failed:[/red] {e}")

    def cmd_cleanup(self, run_id: str) -> None:
        if not run_id:
            console.print("[red]Usage: cleanup <run_id>[/red]")
            return
        try:
            removed = WorktreeManager(self.work_dir).cleanup_run(run_id)
        except Exception as e:
            console.print(f"[red]Cleanup failed:[/red] {e}")
            return
        console.print(f"[green]Removed {removed} worktree(s).[/green]")

    def cmd_inspect(self, run_id: str) -> None:
        if not run_id:
            console.print("[red]Usage: inspect <run_id>[/red]")
            return
        run = self.runtime.get_run(run_id)
        if not run:
            console.print(f"[red]Run not found:[/red] {run_id}")
            return
        progress = f"{run.metadata.get('completed_steps', 0)}/{run.metadata.get('total_steps', run.metadata.get('estimated_steps', 0))}"
        console.print(
            command_panel(
                Group(
                    kv_table(
                        [
                            ("run", run.run_id),
                            ("status", run.status),
                            ("progress", progress),
                            ("created", run.created_at),
                            ("updated", run.updated_at),
                            ("blocked", run.metadata.get("reason", "")),
                        ],
                        title="Run Metadata",
                        border_style="blue",
                    ),
                    command_panel(f"[bold]{markup(run.task, 140)}[/bold]", "TASK", border_style="cyan"),
                ),
                "RUN INSPECTOR",
                border_style="blue",
            )
        )
        steps = self.runtime.steps(run_id)
        if steps:
            step_tbl = command_table("Step Handoff Matrix", border_style="cyan")
            step_tbl.add_column("Step", style="cyan", width=12)
            step_tbl.add_column("Status", width=12)
            step_tbl.add_column("Worker", width=18)
            step_tbl.add_column("Reviewer", width=18)
            step_tbl.add_column("Lease", width=10)
            step_tbl.add_column("Repairs", width=7)
            step_tbl.add_column("Scope", width=24)
            step_tbl.add_column("Commit", width=10)
            step_tbl.add_column("Patch", width=18)
            step_tbl.add_column("Blocker", width=24)
            step_tbl.add_column("Title")
            for step in steps:
                scope = step.metadata.get("effective_scope", step.metadata.get("scope", []))
                if isinstance(scope, list):
                    scope_text = ", ".join(scope)
                else:
                    scope_text = str(scope)
                step_tbl.add_row(
                    markup(step.step_id),
                    status_label(step.status),
                    markup(step.worker, 18),
                    markup(step.metadata.get("reviewer", ""), 18),
                    markup(step.metadata.get("lease", ""), 10),
                    markup(step.metadata.get("repair_attempts", ""), 7),
                    markup(scope_text, 24),
                    markup(step.commit_sha, 10),
                    markup(step.patch_artifact_id, 18),
                    markup(step.metadata.get("blocked_reason", ""), 24),
                    markup(step.title, 60),
                )
            console.print(step_tbl)
        events = self.runtime.events(run_id, limit=80)
        tbl = command_table("Runtime Event Trace", border_style="magenta")
        tbl.add_column("#", style="dim", width=5)
        tbl.add_column("Time", style="dim", width=20)
        tbl.add_column("Step", style="cyan", width=12)
        tbl.add_column("Type", width=20)
        tbl.add_column("Payload")
        for event in events:
            payload = str(event.payload)
            tbl.add_row(
                markup(event.id),
                markup(event.created_at[:19]),
                markup(event.step_id),
                markup(event.event_type),
                markup(payload, 110),
            )
        console.print(tbl)

    def _simple_callbacks(self) -> dict:
        def on_plan(plan):
            console.print(f"[bold]Plan:[/bold] {plan.task_summary} ({len(plan.steps)} steps)")

        def on_step_start(step, idx, total):
            console.print(f"[cyan]>[/cyan] {step.step_id}: {step.title}")

        def on_worker_assigned(step, worker_name):
            console.print(f"  [dim]worker: {worker_name}[/dim]")

        def on_step_result(step, result, worker_name):
            if result.files_written:
                console.print(f"  [green]+[/green] {', '.join(result.files_written)}")

        def on_review(step, review):
            console.print(f"  review: {review.verdict} score:{review.quality_score}/10")

        def on_commit(step, sha):
            if sha:
                console.print(f"  commit: {sha}")

        def on_status(msg):
            console.print(f"[dim]{msg}[/dim]")

        def on_error(step, error):
            console.print(f"  [red]x {error}[/red]")

        def on_task_complete(plan):
            console.print("[green]Run complete.[/green]")

        return {
            "on_plan": on_plan,
            "on_step_start": on_step_start,
            "on_worker_assigned": on_worker_assigned,
            "on_step_result": on_step_result,
            "on_review": on_review,
            "on_commit": on_commit,
            "on_status": on_status,
            "on_error": on_error,
            "on_task_complete": on_task_complete,
        }

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
            existing_text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
            existing_lines = existing_text.splitlines(keepends=True)
            cleaned_text = "".join(_remove_empty_account_markers(existing_lines, "codex_cli"))
            if cleaned_text and not cleaned_text.endswith(("\n", "\r")):
                cleaned_text += "\n"
            CONFIG_FILE.write_text(f"{cleaned_text}{entry}", encoding="utf-8")
            console.print(f"\n[green]✓ Account '{name}' added to config.[/green]")
            console.print("[dim]Rebuilding agents…[/dim]")
            self._reload_config_and_agents()
            self.cmd_status()
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            console.print(f"Add this manually to {CONFIG_FILE}:\n{entry}")

    def _reload_config_and_agents(self) -> None:
        from genesis.config import reset_config_cache

        reset_config_cache()
        self.config = get_config()
        self._build_agents()

    def _account_map(self) -> dict[str, CodexAccount]:
        return {account.name: account for account in self.config.codex_cli.accounts}

    def _delete_codex_home_dirs(self, accounts: list[CodexAccount]) -> None:
        import shutil

        home_root = Path.home().resolve()
        for account in accounts:
            if not account.home:
                console.print(
                    f"[yellow]Skipped deleting the default Codex home for '{account.name}'. "
                    "Run `codex logout` if you want to clear that global login.[/yellow]"
                )
                continue

            path = Path(account.home).expanduser()
            try:
                resolved = path.resolve()
            except OSError as e:
                console.print(f"[yellow]Skipped {path}: {e}[/yellow]")
                continue

            if not resolved.exists():
                console.print(f"[dim]CODEX_HOME already absent: {resolved}[/dim]")
                continue
            if resolved == home_root:
                console.print(f"[yellow]Skipped deleting home directory: {resolved}[/yellow]")
                continue

            shutil.rmtree(resolved)
            console.print(f"[green]Deleted CODEX_HOME:[/green] {resolved}")

    def cmd_remove_account(self, args: list[str]) -> None:
        """
        Remove one Codex account registration from ~/.genesis/config.toml.

        Usage: remove-account <name> [--delete-home]
        """
        flags = {arg for arg in args if arg.startswith("-")}
        names = [arg for arg in args if not arg.startswith("-")]

        if "--help" in flags or "-h" in flags:
            console.print("Usage: remove-account <name> [--delete-home]")
            return

        if not CONFIG_FILE.exists():
            console.print(f"[red]Config file not found:[/red] {CONFIG_FILE}")
            return

        accounts = self._account_map()
        if not names:
            if not accounts:
                console.print("[dim]No Codex accounts are registered in Genesis.[/dim]")
                return
            tbl = command_table("Codex Accounts", border_style="cyan")
            tbl.add_column("Name")
            tbl.add_column("CODEX_HOME")
            for account in accounts.values():
                tbl.add_row(markup(account.name), account.home or "[dim]default ~/.codex[/dim]")
            console.print(tbl)
            console.print("[cyan]Account name to remove[/cyan]: ", end="")
            account_name = input().strip()
        else:
            account_name = names[0]

        if not account_name:
            console.print("[red]Name cannot be empty.[/red]")
            return

        if account_name.lower() == "all":
            self.cmd_remove_all_accounts(list(flags))
            return

        try:
            original_text = CONFIG_FILE.read_text(encoding="utf-8")
            updated_text, removed, seen, remaining = _rewrite_codex_accounts_text(
                original_text,
                remove_names={account_name},
            )
        except Exception as e:
            console.print(f"[red]Failed to read config: {e}[/red]")
            return

        if not removed:
            available = ", ".join(seen) if seen else "none"
            console.print(f"[yellow]No Genesis Codex account named '{account_name}'. Available: {available}[/yellow]")
            return

        try:
            CONFIG_FILE.write_text(updated_text, encoding="utf-8")
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            return

        removed_accounts = [accounts[name] for name in removed if name in accounts]
        if "--delete-home" in flags and removed_accounts:
            self._delete_codex_home_dirs(removed_accounts)

        self._reload_config_and_agents()
        console.print(f"[green]Removed Codex account from Genesis:[/green] {', '.join(removed)}")
        if not remaining:
            console.print("[dim]No Codex accounts remain registered in Genesis.[/dim]")
        self.cmd_status()

    def cmd_remove_all_accounts(self, args: list[str]) -> None:
        """
        Remove every Codex account registration from ~/.genesis/config.toml.

        Usage: remove-all-accounts [--yes] [--delete-home]
        """
        flags = {arg for arg in args if arg.startswith("-")}
        if "--help" in flags or "-h" in flags:
            console.print("Usage: remove-all-accounts [--yes] [--delete-home]")
            return

        if not CONFIG_FILE.exists():
            console.print(f"[red]Config file not found:[/red] {CONFIG_FILE}")
            return

        accounts = list(self.config.codex_cli.accounts)
        if "--yes" not in flags:
            console.print("[yellow]Remove all Codex accounts from Genesis config? (y/N): [/yellow]", end="")
            if input().strip().lower() != "y":
                console.print("[dim]No accounts removed.[/dim]")
                return

        try:
            original_text = CONFIG_FILE.read_text(encoding="utf-8")
            updated_text, removed, _seen, _remaining = _rewrite_codex_accounts_text(
                original_text,
                remove_all=True,
            )
            CONFIG_FILE.write_text(updated_text, encoding="utf-8")
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            return

        if "--delete-home" in flags and accounts:
            self._delete_codex_home_dirs(accounts)

        self._reload_config_and_agents()
        if removed:
            console.print(f"[green]Removed Codex accounts from Genesis:[/green] {', '.join(removed)}")
        else:
            console.print("[green]Disabled Genesis Codex account fallback.[/green]")
        self.cmd_status()

    # ── Claude accounts ────────────────────────────────────────────────────

    def _claude_account_map(self) -> dict[str, ClaudeAccount]:
        return {account.name: account for account in self.config.claude_cli.accounts}

    def _claude_login_ok(self, claude_cmd: str, env: dict, config_dir: str) -> bool:
        """Confirm the isolated Claude login completed.

        Primary signal is `claude auth status`; fall back to a credentials file
        appearing in the config dir for CLI versions without that subcommand."""
        import subprocess as _sp

        status_cmd = [claude_cmd, "auth", "status"]
        if os.name == "nt" and claude_cmd.lower().endswith((".cmd", ".bat")):
            status_cmd = ["cmd", "/c"] + status_cmd
        try:
            check = _sp.run(status_cmd, env=env, capture_output=True, text=True)
            if check.returncode == 0:
                return True
        except Exception:
            pass

        cfg_path = Path(config_dir)
        return (cfg_path / ".credentials.json").exists() or any(cfg_path.glob("*credentials*"))

    def _delete_claude_config_dirs(self, accounts: list[ClaudeAccount]) -> None:
        import shutil

        home_root = Path.home().resolve()
        default_claude = (Path.home() / ".claude").resolve()
        for account in accounts:
            if not account.config_dir:
                console.print(
                    f"[yellow]Skipped deleting the default Claude config for '{account.name}'. "
                    "Run `claude auth logout` if you want to clear that global login.[/yellow]"
                )
                continue

            path = Path(account.config_dir).expanduser()
            try:
                resolved = path.resolve()
            except OSError as e:
                console.print(f"[yellow]Skipped {path}: {e}[/yellow]")
                continue

            if not resolved.exists():
                console.print(f"[dim]CLAUDE_CONFIG_DIR already absent: {resolved}[/dim]")
                continue
            if resolved in (home_root, default_claude):
                console.print(f"[yellow]Skipped deleting protected directory: {resolved}[/yellow]")
                continue

            shutil.rmtree(resolved)
            console.print(f"[green]Deleted CLAUDE_CONFIG_DIR:[/green] {resolved}")

    def cmd_add_claude_account(self, args: list[str]) -> None:
        """
        Interactively add a new Claude account to ~/.genesis/config.toml.

        Usage: add-claude-account
        """
        import subprocess as _sp

        console.print("\n[bold]Add a Claude Account[/bold]")
        console.print("Each account needs its own CLAUDE_CONFIG_DIR directory.\n")

        console.print("[cyan]Account name[/cyan] (e.g. claude-pro2): ", end="")
        name = input().strip()
        if not name:
            console.print("[red]Name cannot be empty.[/red]")
            return
        if "claude" not in name.lower():
            console.print(
                "[yellow]Tip: start the name with 'claude-' so Genesis treats it as a Claude worker.[/yellow]"
            )

        default_dir = str(Path.home() / f".claude-{name}")
        console.print(f"[cyan]CLAUDE_CONFIG_DIR directory[/cyan] (default: {default_dir}): ", end="")
        config_dir = input().strip() or default_dir

        dir_path = Path(config_dir)
        if not dir_path.exists():
            dir_path.mkdir(parents=True, exist_ok=True)
            console.print(f"[green]Created directory:[/green] {config_dir}")

        console.print(f"\n[dim]Now logging in to Claude with CLAUDE_CONFIG_DIR={config_dir}[/dim]")
        console.print(
            "[dim]A browser window will open — log in with your other Claude account. "
            "Your default ~/.claude login is not affected.[/dim]\n"
        )

        claude_cmd = find_claude_binary() or "claude"
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir))

        login_cmd = [claude_cmd, "auth", "login"]
        if os.name == "nt" and claude_cmd.lower().endswith((".cmd", ".bat")):
            login_cmd = ["cmd", "/c"] + login_cmd

        try:
            _sp.run(login_cmd, env=env, check=False)
        except Exception as e:
            console.print(f"[red]Login failed: {e}[/red]")
            return

        if not self._claude_login_ok(claude_cmd, env, config_dir):
            console.print("[red]Login did not complete. Account not added.[/red]")
            return

        # Append to config.toml — always use forward slashes (TOML requires it)
        config_toml = str(Path(config_dir)).replace("\\", "/")
        entry = (
            f"\n[[claude_cli.accounts]]\n"
            f'name = "{name}"\n'
            f'config_dir = "{config_toml}"\n'
            f'model = "auto"\n'
        )

        try:
            existing_text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
            existing_lines = existing_text.splitlines(keepends=True)
            cleaned_text = "".join(_remove_empty_account_markers(existing_lines, "claude_cli"))
            if cleaned_text and not cleaned_text.endswith(("\n", "\r")):
                cleaned_text += "\n"
            CONFIG_FILE.write_text(f"{cleaned_text}{entry}", encoding="utf-8")
            console.print(f"\n[green]✓ Claude account '{name}' added to config.[/green]")
            console.print("[dim]Rebuilding agents…[/dim]")
            self._reload_config_and_agents()
            self.cmd_status()
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            console.print(f"Add this manually to {CONFIG_FILE}:\n{entry}")

    def cmd_remove_claude_account(self, args: list[str]) -> None:
        """
        Remove one Claude account registration from ~/.genesis/config.toml.

        Usage: remove-claude-account <name> [--delete-config-dir]
        """
        flags = {arg for arg in args if arg.startswith("-")}
        names = [arg for arg in args if not arg.startswith("-")]

        if "--help" in flags or "-h" in flags:
            console.print("Usage: remove-claude-account <name> [--delete-config-dir]")
            return

        if not CONFIG_FILE.exists():
            console.print(f"[red]Config file not found:[/red] {CONFIG_FILE}")
            return

        accounts = self._claude_account_map()
        if not names:
            if not accounts:
                console.print("[dim]No Claude accounts are registered in Genesis.[/dim]")
                return
            tbl = command_table("Claude Accounts", border_style="cyan")
            tbl.add_column("Name")
            tbl.add_column("CLAUDE_CONFIG_DIR")
            for account in accounts.values():
                tbl.add_row(markup(account.name), account.config_dir or "[dim]default ~/.claude[/dim]")
            console.print(tbl)
            console.print("[cyan]Account name to remove[/cyan]: ", end="")
            account_name = input().strip()
        else:
            account_name = names[0]

        if not account_name:
            console.print("[red]Name cannot be empty.[/red]")
            return

        if account_name.lower() == "all":
            self.cmd_remove_all_claude_accounts(list(flags))
            return

        try:
            original_text = CONFIG_FILE.read_text(encoding="utf-8")
            updated_text, removed, seen, remaining = _rewrite_claude_accounts_text(
                original_text,
                remove_names={account_name},
            )
        except Exception as e:
            console.print(f"[red]Failed to read config: {e}[/red]")
            return

        if not removed:
            available = ", ".join(seen) if seen else "none"
            console.print(f"[yellow]No Genesis Claude account named '{account_name}'. Available: {available}[/yellow]")
            return

        try:
            CONFIG_FILE.write_text(updated_text, encoding="utf-8")
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            return

        removed_accounts = [accounts[name] for name in removed if name in accounts]
        if "--delete-config-dir" in flags and removed_accounts:
            self._delete_claude_config_dirs(removed_accounts)

        self._reload_config_and_agents()
        console.print(f"[green]Removed Claude account from Genesis:[/green] {', '.join(removed)}")
        if not remaining:
            console.print("[dim]No Claude accounts remain registered in Genesis.[/dim]")
        self.cmd_status()

    def cmd_remove_all_claude_accounts(self, args: list[str]) -> None:
        """
        Remove every Claude account registration from ~/.genesis/config.toml.

        Usage: remove-all-claude-accounts [--yes] [--delete-config-dir]
        """
        flags = {arg for arg in args if arg.startswith("-")}
        if "--help" in flags or "-h" in flags:
            console.print("Usage: remove-all-claude-accounts [--yes] [--delete-config-dir]")
            return

        if not CONFIG_FILE.exists():
            console.print(f"[red]Config file not found:[/red] {CONFIG_FILE}")
            return

        accounts = list(self.config.claude_cli.accounts)
        if "--yes" not in flags:
            console.print("[yellow]Remove all Claude accounts from Genesis config? (y/N): [/yellow]", end="")
            if input().strip().lower() != "y":
                console.print("[dim]No accounts removed.[/dim]")
                return

        try:
            original_text = CONFIG_FILE.read_text(encoding="utf-8")
            updated_text, removed, _seen, _remaining = _rewrite_claude_accounts_text(
                original_text,
                remove_all=True,
            )
            CONFIG_FILE.write_text(updated_text, encoding="utf-8")
        except Exception as e:
            console.print(f"[red]Failed to update config: {e}[/red]")
            return

        if "--delete-config-dir" in flags and accounts:
            self._delete_claude_config_dirs(accounts)

        self._reload_config_and_agents()
        if removed:
            console.print(f"[green]Removed Claude accounts from Genesis:[/green] {', '.join(removed)}")
        else:
            console.print("[green]Disabled Genesis Claude account fallback.[/green]")
        self.cmd_status()

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

    def _cleanup(self) -> None:
        """Close any browser agents and shut down the chatroom viewer."""
        for agent in self._agents.values():
            if hasattr(agent, "close"):
                try:
                    agent.close()
                except Exception:
                    pass
        self._stop_chatroom_server()

    def run(self) -> None:
        self._print_banner()

        try:
            self._run_loop()
        finally:
            self._cleanup()

    def _run_loop(self) -> None:
        while True:
            try:
                console.print("[bold cyan]genesis[/bold cyan][dim]>[/dim] ", end="")
                line = input().strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye.[/dim]")
                break

            if not line:
                continue

            # Split only on the first word — everything after is raw text.
            # This lets users paste prompts with quotes, apostrophes, etc.
            first_space = line.find(" ")
            if first_space == -1:
                cmd = line.lower()
                rest = ""
            else:
                cmd = line[:first_space].lower()
                rest = line[first_space + 1:].strip()

            # For sub-commands that need a second word (memory, config, git, switch)
            # split just that second word off the rest.
            def _split_sub(text: str) -> tuple[str, str]:
                i = text.find(" ")
                if i == -1:
                    return text.lower(), ""
                return text[:i].lower(), text[i + 1:].strip()

            if cmd in ("exit", "quit", "q"):
                console.print("[dim]Goodbye.[/dim]")
                break
            elif cmd == "run":
                self.cmd_run(rest)
            elif cmd == "resume":
                self.cmd_resume(rest)
            elif cmd == "retry":
                sub, sub_rest = _split_sub(rest)
                self.cmd_retry([sub] + ([sub_rest] if sub_rest else []))
            elif cmd == "plan":
                self.cmd_plan(rest)
            elif cmd == "status":
                self.cmd_status()
            elif cmd == "memory":
                sub, sub_rest = _split_sub(rest)
                self.cmd_memory([sub] + ([sub_rest] if sub_rest else []))
            elif cmd == "config":
                sub, _ = _split_sub(rest)
                self.cmd_config([sub] if sub else [])
            elif cmd == "git":
                sub, sub_rest = _split_sub(rest)
                self.cmd_git([sub] + ([sub_rest] if sub_rest else []))
            elif cmd in ("agents", "agent"):
                self.cmd_agents()
            elif cmd == "runs":
                self.cmd_runs()
            elif cmd == "inspect":
                self.cmd_inspect(rest)
            elif cmd == "cleanup":
                self.cmd_cleanup(rest)
            elif cmd in ("add-account", "add_account", "addaccount"):
                self.cmd_add_account([])
            elif cmd in ("remove-account", "remove_account", "removeaccount"):
                self.cmd_remove_account(rest.split())
            elif cmd in (
                "remove-all-accounts",
                "remove_all_accounts",
                "removeallaccounts",
                "remove-accounts",
                "remove_accounts",
                "remove-all",
                "remove_all",
            ):
                self.cmd_remove_all_accounts(rest.split())
            elif cmd in ("add-claude-account", "add_claude_account", "addclaudeaccount"):
                self.cmd_add_claude_account([])
            elif cmd in ("remove-claude-account", "remove_claude_account", "removeclaudeaccount"):
                self.cmd_remove_claude_account(rest.split())
            elif cmd in (
                "remove-all-claude-accounts",
                "remove_all_claude_accounts",
                "removeallclaudeaccounts",
                "remove-claude-accounts",
                "remove_claude_accounts",
            ):
                self.cmd_remove_all_claude_accounts(rest.split())
            elif cmd == "switch":
                sub, sub_rest = _split_sub(rest)
                self.cmd_switch([sub] + ([sub_rest] if sub_rest else []))
            elif cmd == "clear":
                console.clear()
            elif cmd == "help":
                console.print(command_panel(_help_renderable(), "COMMAND INDEX", border_style="cyan", padding=(1, 2)))
            else:
                console.print(
                    f"[yellow]Unknown command '{cmd}'.[/yellow] "
                    f"Type [cyan]help[/cyan] for available commands."
                )

    def _print_banner(self) -> None:
        from genesis.ui.banner import render_banner

        systems: list[tuple[str, bool]] = [
            ("Claude Code", bool(find_claude_binary())),
            ("Codex", bool(find_codex_binary())),
        ]
        if self.config.chatgpt_browser.enabled:
            systems.append(("ChatGPT browser", True))

        info = [
            ("cwd", Path(self.work_dir).name or self.work_dir),
            ("agents", str(len(self._agents)) if self._agents else "none"),
            ("parallel", str(self.config.runtime.max_parallel_workers)),
            ("memory", self.config.memory.file),
        ]

        console.print()
        render_banner(
            console,
            version=__version__,
            systems=systems,
            info=info,
            commands="run <task>   ·   runs   ·   inspect <id>   ·   status   ·   help   ·   exit",
        )
        console.print()
