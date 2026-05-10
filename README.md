# Genesis

A terminal-only AI orchestration system for Windows. Genesis runs as a local CLI that coordinates multiple AI agents — Claude Code as the orchestrator and ChatGPT Codex as autonomous workers — to complete software development tasks from a single prompt.

No API keys required. Authentication is handled through your existing Claude Code Pro and ChatGPT Pro subscriptions via their respective CLI tools.

---

## How it works

When you type `run <task>`, Genesis:

1. Sends the task to Claude (orchestrator), which produces a JSON execution plan breaking the work into concrete steps
2. Assigns each step to a Codex worker agent, which writes files and runs shell commands inside an isolated git worktree
3. After each step, Claude reviews the result and either approves it, requests a revision, or rejects it
4. Approved steps are committed to git automatically
5. Progress is written to `GENESIS_MEMORY.md` in the repository so the context accumulates across steps

While workers execute you see a live terminal dashboard: the plan with step statuses, streaming agent output (shell commands, file writes, token counts), agent roster, and cumulative usage metrics.

---

## Setup

Follow these steps in order. Do not skip any step.

### Step 1 — Install Python

Download and install Python 3.10 or later from https://python.org/downloads

During installation, check the box that says **"Add Python to PATH"**.

Verify it works:

```
python --version
```

You should see `Python 3.10.x` or higher.

---

### Step 2 — Install Claude Code CLI

Claude Code is the CLI for your Claude Pro subscription.

Download and install it from https://claude.ai/download

After installation, log in with your Claude account:

```
claude login
```

A browser window will open. Sign in with the same account that has your Claude Pro subscription.

Verify it works:

```
claude --version
```

---

### Step 3 — Install Codex CLI

Codex is the CLI for your ChatGPT Pro subscription.

Install it via npm (requires Node.js 18+):

```
npm install -g @openai/codex
```

After installation, log in with your ChatGPT account:

```
codex login
```

A browser window will open. Sign in with the account that has your ChatGPT Pro subscription.

Verify it works:

```
codex --version
```

---

### Step 4 — Clone and install Genesis

```
git clone https://github.com/yourname/genesis
cd genesis
pip install -e .
```

This installs a global `genesis` command. Verify it installed:

```
genesis --help
```

---

### Step 5 — Create the config file

```
genesis init
```

This creates `~/.genesis/config.toml` with default settings. Open it in any text editor.

Find the `[orchestrator]` section and make sure the model is set to `claude-sonnet-4-6`:

```toml
[orchestrator]
provider = "claude-cli"
model    = "claude-sonnet-4-6"
```

Sonnet is recommended over Opus for the orchestrator because it has higher rate limits on Claude Pro.

The `[worker]` section should be set to Codex:

```toml
[worker]
provider = "codex-cli"
model    = "auto"
```

Save and close the file.

---

### Step 6 — Verify everything is connected

```
genesis status
```

You should see output like:

```
Agents:   Claude Code  ·  Codex
Active:   claude-cli-orchestrator, claude-cli-worker, codex-main
```

If Claude Code or Codex shows as missing, re-run `claude login` or `codex login` in the same terminal and try again.

---

### Step 7 (optional) — Add more Codex accounts

If you have multiple ChatGPT Pro accounts, you can register each one as a separate worker. Each account runs in parallel on different steps, which speeds up execution.

Inside the Genesis REPL:

```
genesis> add-account
```

You will be asked for:
- A name for the account (e.g. `codex-2`)
- A directory path where this account's login will be stored (e.g. `C:/Users/yourname/.codex-2`)

Genesis will open a browser window for you to log in with the second account. After login, the account is saved to `~/.genesis/config.toml` and available immediately as a worker.

Repeat this for each additional account.

---

### Step 8 — Fix character rendering (if needed)

If the terminal shows garbled symbols or boxes instead of the dashboard, set the UTF-8 environment variable before running Genesis:

```
set PYTHONUTF8=1
genesis
```

To make this permanent, add `PYTHONUTF8=1` to your system environment variables in Windows Settings > System > Advanced system settings > Environment Variables.

---

## Running Genesis

Navigate to any git repository and start the REPL:

```
cd C:\Projects\my-app
genesis
```

If the folder is not yet a git repository, initialise it first:

```
cd C:\Projects\my-app
git init
git add .
git commit -m "initial commit"
genesis
```

Then give it a task:

```
genesis> run build a REST API with user auth, a PostgreSQL backend, and pytest tests
```

Genesis will plan the work, assign steps to Codex workers, and commit each approved step to git automatically.

---

## Commands

```
run <task>                 Execute a task through the AI orchestrator
resume <run_id>            Resume a durable run from stored step state
retry <run_id> <step_id>   Retry a blocked step, then continue the run
plan <task>                Generate and preview a plan without executing it
status                     Show agents, config, and recent git log
agents                     List all registered agents and their status
memory show                Print the shared memory file
memory search <query>      Search the SQLite memory palace
memory mine                Import GENESIS_MEMORY.md into palace memory
memory clear               Reset memory for a new project
memory append <text>       Add a manual note to memory
config show                Print the active configuration
config edit                Open the config file in your editor
git log                    Show recent Genesis commits
git commit [message]       Manually commit current changes
runs                       Show recent durable runs
inspect <run_id>           Show run state and event trace
cleanup <run_id>           Remove stale isolated worktrees for a run
switch orchestrator <name> Hot-swap the orchestrator agent
switch worker <name>       Hot-swap the default worker agent
add-account                Add a Codex account interactively
help                       Show all commands
exit                       Quit Genesis
```

---

## Dashboard

The terminal dashboard refreshes 8 times per second and shows:

- **Header** — animated spinner, task name, active worker, current step progress, elapsed time
- **PLAN** (left) — all steps with status icons and per-step elapsed time
- **AGENT OUTPUT** (center) — live streaming from the active worker: shell commands and their output, files written, thinking previews, token counts per turn
- **AGENTS** (top right) — full roster of registered agents with the active worker highlighted
- **USAGE** (bottom right) — cumulative input/output/cached tokens and cost broken down per worker
- **Footer** — progress bar, step count, latest git SHA, session cost

---

## Memory

Genesis writes a `GENESIS_MEMORY.md` file to the root of your repository. This file records the plan for each task, the outcome of each step, review verdicts, and completion timestamps.

This memory is injected into every planning and review prompt so the orchestrator understands what already exists before planning new work. It persists across sessions, so running `genesis` in the same repository later will pick up context from previous tasks.

Genesis also keeps a local SQLite state database at `~/.genesis/state/genesis.db` by default. This database stores durable run events, checkpoints, verification results, and searchable verbatim "memory palace" drawers. Markdown memory remains the human-readable project log; SQLite is the operational memory and trace store.

Search memory:

```
genesis> memory search authentication middleware
```

Import an existing Markdown memory file into the palace store:

```
genesis> memory mine
```

Clear memory when starting a conceptually new project:

```
genesis> memory clear
```

---

## Git integration

With `auto_commit = true` (default), Genesis commits only after a step is approved by review and verification gates pass:

```
[genesis] step-2: Implement authentication middleware
[genesis] task-complete: Build REST API with auth and tests
```

Auto-push is disabled by default. To enable it, edit `~/.genesis/config.toml`:

```toml
[git]
auto_push = true
remote    = "origin"
branch    = "main"
```

Genesis runs workers in isolated git worktrees under `~/.genesis/worktrees/`. A worker patch is captured and stored in SQLite, reviewed, verified in the isolated worktree, checked with `git apply --check`, then applied to the main repository and committed. Rejected or failed steps leave the main repository unchanged and can be inspected or retried with `inspect`, `resume`, and `retry`.

Because each accepted step becomes the base for the next worktree, isolated execution requires `git.auto_commit = true` and a clean main worktree apart from Genesis-managed memory/state files.

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
palace_enabled    = true

[runtime]
state_db = ""                    # empty = ~/.genesis/state/genesis.db
retry_budget = 1
max_parallel_workers = 1
checkpoint_mode = "always"

[verification]
commands = []                    # e.g. ["python -m compileall genesis"]
timeout = 300
require_for_commit = true

[policy]
file = "genesis.policy.toml"
protected_paths = [".git/", ".genesis/state/"]
blocked_commands = ["git reset --hard", "git checkout --", "Remove-Item -Recurse -Force", "rm -rf /"]
allowed_commands = []
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
    codex_worker.py    CodexWorker — Codex worker, detects content-hash file changes
  schemas/
    plan.py            Plan and Step Pydantic models
    review.py          Review Pydantic model
  ui/
    dashboard.py       Rich Live layout — DashboardState, make_layout()
    console.py         Shared Rich Console singleton
  config.py            TOML config loader, dataclasses
  palace.py            SQLite verbatim memory palace + FTS search
  runtime.py           Durable run events, checkpoints, artifacts
  worktree.py          Isolated git worktrees and patch application
  policy.py            Protected path and command policy checks
  verifier.py          Configurable verification gates before commit
  memory.py            MemoryManager — reads/writes GENESIS_MEMORY.md
  git_ops.py           GitManager — wraps GitPython
  repl.py              GenesisREPL — main REPL loop, command dispatch
  main.py              Entry point
```

Claude makes two calls per task: one `plan()` call to get a validated JSON execution plan, and one `review()` call per step to get a structured verdict. Codex workers execute in isolated git worktrees, not the main repository. Genesis captures a patch, records durable checkpoints in SQLite, verifies the patch in isolation, and commits only approved, verified steps.

---

## Troubleshooting

**"You've hit your limit"**
Claude Pro rate limit reached. Wait for the reset time shown in the message (usually less than an hour). To reduce how often this happens, make sure the orchestrator model is `claude-sonnet-4-6` and not `claude-opus-4-6`. Sonnet has significantly higher rate limits.

**Codex exits with code 1**
The Codex account is not logged in, or the session expired. Run `codex login` and try again. For a secondary account: `set CODEX_HOME=C:/Users/yourname/.codex-2` then `codex login`.

**"No worker agents available"**
Neither Claude Code nor Codex was detected in PATH. Open a new terminal, confirm `claude --version` and `codex --version` both print a version number, then run `genesis` from that same terminal.

**TOML parse error after editing config.toml**
Windows path backslashes in TOML strings are treated as escape sequences. Use forward slashes in all paths inside the config file. Correct: `C:/Users/yourname/.codex-2`. Incorrect: `C:\Users\yourname\.codex-2`. Genesis writes forward slashes automatically when you use `add-account`, so this only affects manual edits.

**Dashboard shows boxes or garbled characters**
Set `PYTHONUTF8=1` before running Genesis. See Step 8 in the setup section above.
