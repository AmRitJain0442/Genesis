# Genesis

A terminal-only AI orchestration system for Windows. Genesis runs as a local CLI that coordinates multiple AI agents — Claude Code as the orchestrator and ChatGPT Codex as autonomous workers — to complete software development tasks from a single prompt.

No API keys required. Authentication is handled through your existing Claude Code Pro and ChatGPT Pro subscriptions via their respective CLI tools.

---

## How it works

When you type `run <task>`, Genesis:

1. Sends the task to Claude (orchestrator), which produces a JSON execution plan breaking the work into concrete steps
2. Assigns each step to a Codex worker agent, which writes files and runs shell commands autonomously inside your repository
3. After each step, Claude reviews the result and either approves it, requests a revision, or rejects it
4. Approved steps are committed to git automatically
5. Progress is written to `GENESIS_MEMORY.md` in the repository so the context accumulates across steps

While workers execute you see a live four-panel terminal dashboard: the plan with step statuses, streaming agent output (shell commands, file writes, token counts), agent roster, and cumulative usage metrics.

---

## Requirements

- Windows 10 or 11
- Python 3.10 or later
- [Claude Code CLI](https://claude.ai/download) installed and logged in (`claude login`)
- [Codex CLI](https://github.com/openai/codex) installed and logged in (`codex login`)
- A git repository to work in (Genesis operates on the current directory)

---

## Installation

```
git clone https://github.com/yourname/genesis
cd genesis
pip install -e .
```

This installs a global `genesis` command available from any terminal.

---

## Setup

Initialize the config file:

```
genesis init
```

This creates `~/.genesis/config.toml`. Open it and verify the orchestrator model:

```toml
[orchestrator]
provider = "claude-cli"
model    = "claude-sonnet-4-6"

[worker]
provider = "codex-cli"
model    = "auto"
```

`claude-sonnet-4-6` is recommended for the orchestrator. It has higher rate limits on Claude Pro than Opus and is fast enough for planning and reviewing.

---

## Multiple Codex accounts

Each ChatGPT Pro account can be registered as a separate worker. Genesis isolates accounts using the `CODEX_HOME` environment variable, which points each Codex process at its own directory containing a separate `auth.json`.

To add an account interactively:

```
genesis> add-account
```

You will be prompted for a name and a home directory path. Genesis will run `codex login` scoped to that directory. After login, the account is appended to `~/.genesis/config.toml` and immediately available as a worker.

Manual config entry:

```toml
[[codex_cli.accounts]]
name = "codex-work"
home = "C:/Users/yourname/.codex-work"
model = "auto"
```

All registered accounts appear in the AGENTS panel and are used as workers in round-robin order. The first account listed is also registered as the `codex-orchestrator` fallback.

---

## Usage

Navigate to any git repository and start Genesis:

```
cd C:\Projects\my-app
genesis
```

If the folder is not yet a git repository, initialise it first:

```
git init
git add .
git commit -m "initial"
genesis
```

### Commands

```
run <task>                 Execute a task through the AI orchestrator
plan <task>                Generate and preview a plan without executing it
status                     Show agents, config, and recent git log
agents                     List all registered agents and their status
memory show                Print the shared memory file
memory clear               Reset memory for a new project
memory append <text>       Add a manual note to memory
config show                Print the active configuration
config edit                Open the config file in your editor
git log                    Show recent Genesis commits
git commit [message]       Manually commit current changes
switch orchestrator <name> Hot-swap the orchestrator agent
switch worker <name>       Hot-swap the default worker agent
add-account                Add a Codex account interactively
help                       Show all commands
exit                       Quit Genesis
```

### Example session

```
genesis> run build a REST API with user auth, a PostgreSQL backend, and pytest tests

genesis> plan add rate limiting to the existing API endpoints

genesis> memory show

genesis> status
```

---

## Dashboard

The terminal dashboard refreshes 8 times per second and shows:

- **Header** — animated spinner, task name, active worker, current step progress, elapsed time
- **PLAN** (left) — all steps with status icons, per-step elapsed time
- **AGENT OUTPUT** (center) — live streaming from the active worker: shell commands and their output, files written, thinking previews, token counts per turn
- **AGENTS** (top right) — full roster of registered agents, with the active worker highlighted
- **USAGE** (bottom right) — cumulative input/output/cached tokens and cost, broken down per worker
- **Footer** — progress bar, step count, latest git SHA, session cost

---

## Memory

Genesis writes a `GENESIS_MEMORY.md` file to the root of your repository. This file records:

- The plan for each task (step IDs, titles, types, assigned agents)
- The outcome of each step (what was built, review verdict, quality score)
- Task completion timestamps

This memory is injected into every planning and review prompt so the orchestrator understands what already exists before planning new work. It persists across sessions, so running `genesis` in the same repository later will pick up the context from previous tasks.

Clear memory when starting a conceptually new project:

```
genesis> memory clear
```

---

## Git integration

With `auto_commit = true` (default), Genesis commits after every approved step using the message format:

```
[genesis] step-2: Implement authentication middleware
```

A final commit is added when the task completes:

```
[genesis] task-complete: Build REST API with auth and tests
```

Auto-push is disabled by default. To enable it:

```toml
[git]
auto_push = true
remote    = "origin"
branch    = "main"
```

---

## Configuration reference

Full `~/.genesis/config.toml` with all options:

```toml
[orchestrator]
provider = "claude-cli"          # claude-cli | codex-cli
model    = "claude-sonnet-4-6"

[worker]
provider = "codex-cli"           # codex-cli | claude-cli
model    = "auto"

[claude_cli]
command = "claude"               # path to binary, auto-detected from PATH
timeout = 300                    # seconds per call

[codex_cli]
command = "codex"
timeout = 600                    # codex tasks run longer (it executes code)

[[codex_cli.accounts]]
name  = "codex-main"
home  = ""                       # empty = default ~/.codex
model = "auto"

[git]
auto_commit   = true
auto_push     = false
remote        = "origin"
branch        = "main"
commit_prefix = "[genesis]"

[memory]
file              = "GENESIS_MEMORY.md"
max_context_chars = 6000         # chars of memory injected per prompt
auto_append_plan  = true
```

---

## Architecture

```
genesis/
  agents/
    orchestrator.py    Orchestrator class — plan(), review(), run_task()
    claude_cli.py      ClaudeCodeCLIAgent — drives `claude --print`
    codex_cli.py       CodexCLIAgent — drives `codex exec`
    worker.py          Worker — Claude worker, parses XML code blocks
    codex_worker.py    CodexWorker — Codex worker, detects file changes via mtime diff
  schemas/
    plan.py            Plan and Step Pydantic models
    review.py          Review Pydantic model
  ui/
    dashboard.py       Rich Live layout — DashboardState, make_layout()
    console.py         Shared Rich Console singleton
  config.py            TOML config loader, dataclasses
  memory.py            MemoryManager — reads/writes GENESIS_MEMORY.md
  git_ops.py           GitManager — wraps GitPython
  repl.py              GenesisREPL — main REPL loop, command dispatch
  main.py              Entry point
```

The orchestrator makes two types of Claude calls per task:

- One `plan()` call using `--json-schema` to get a validated JSON execution plan
- One `review()` call per step using `--json-schema` to get a structured verdict

All code execution goes to Codex workers, which run inside the repository with `--sandbox workspace-write`. Codex writes files directly; Genesis detects changes using an mtime snapshot diff before and after execution.

Streaming output from both CLIs is parsed as JSONL and forwarded to the dashboard in real time:

- Claude: `--output-format stream-json --verbose` — emits `assistant` events with `thinking` and `text` blocks, and a `result` event with usage
- Codex: `--json` — emits `item.completed` events for agent messages, command executions, and file changes, plus `turn.completed` with token counts

---

## Troubleshooting

**"You've hit your limit"** — Claude Pro rate limit reached. Genesis will automatically retry after the reset time shown. To reduce usage, ensure the orchestrator model is `claude-sonnet-4-6` (higher limits than Opus).

**Codex exits with code 1** — Usually means the Codex account is not logged in. Run `codex login` (or for a secondary account: `CODEX_HOME=C:/path/to/account codex login`).

**TOML parse error after add-account** — Path backslashes in TOML strings are interpreted as escape sequences. Genesis writes forward slashes automatically. If you edit `config.toml` manually, use forward slashes in all paths: `C:/Users/name/.codex-work`.

**Dashboard renders garbage characters** — Run with `PYTHONUTF8=1` prefix or set the environment variable permanently in Windows system settings.

**"No worker agents available"** — Neither Claude Code nor Codex was detected in PATH. Confirm `claude --version` and `codex --version` work in the same terminal before running `genesis`.
