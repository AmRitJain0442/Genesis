# Genesis

```
   ____                          _
  / ___| ___ _ __   ___  ___  __(_)___
 | |  _ / _ \ '_ \ / _ \/ __|/ _` / __|
 | |_| |  __/ | | |  __/\__ \ (_| \__ \
  \____|\___|_| |_|\___||___/\__,_|___/

        local AI orchestration for software work.
```

Genesis is a terminal-only AI orchestration system for Windows. It coordinates Claude Code as the planner and reviewer, plus Codex CLI workers as autonomous code executors, so a single prompt can be broken into scoped, reviewed, verified, and committed development steps.

No API keys are required. Genesis uses your existing Claude Code Pro and ChatGPT Pro sessions through their official CLI tools.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Interface](https://img.shields.io/badge/interface-terminal-green)

<p align="center">
  <img src="docs/images/genesis-command-center.png" alt="Genesis command center with connected AI coding workers" width="100%">
</p>

## Why Genesis

Most AI coding tools work as one assistant in one working tree. Genesis is built around a small local team model:

- Claude Code plans the task and reviews completed work.
- Codex workers execute implementation steps in isolated git worktrees.
- Verification commands run before approved changes reach the main repository.
- Durable state is stored locally so runs can be inspected, resumed, or retried.
- Git commits are created only after review and verification pass.

The result is a local command center for multi-step software work, with a visible audit trail and conservative repository handling.

## How It Works

When you type `run <task>`, Genesis:

1. Sends the task to the orchestrator, which returns a structured JSON execution plan.
2. Snapshots the real repository state and checkpoints recoverable local work while excluding credentials.
3. Assigns ready steps based on dependencies and file scope, then runs each worker in an isolated git worktree.
4. Captures every turn as a base-relative Git patch with an immutable patch SHA and filters transient artifacts or unrelated deletions.
5. Runs objective acceptance gates before spending a reviewer call, then binds the independent review verdict to that exact patch SHA.
6. Runs configured verification commands in isolation.
7. Applies only the reviewed changed-file manifest to the main repository and commits it.
8. Stores the authoritative patch artifact and audit trail in SQLite, then records only committed outcomes (or blocked failures) in project memory.

During execution you see a live command-center dashboard with plan state, active workers, reviewer handoffs, streaming output, verification status, git activity, recent events, and usage metrics.

<p align="center">
  <img src="docs/images/genesis-workflow.png" alt="Genesis workflow from planning through workers, review, verification, and commit" width="100%">
</p>

## Features

- Local terminal workflow with no hosted control plane.
- Claude Code and Codex CLI integration through existing OAuth sessions.
- Multi-account Codex worker support for parallel execution.
- Isolated git worktrees for worker changes.
- Inactivity-based worker watchdogs that stay alive while Codex is producing events.
- Git-grounded, versioned evidence on every worker turn.
- Deterministic acceptance gates before model review.
- Reviewer verdicts bound to immutable patch versions.
- Bounded self-repair for worker errors, empty patches, deterministic gates,
  actionable reviews, and verification failures.
- Durable runs with `resume`, `retry`, `inspect`, and `cleanup`.
- Configurable verification gates before commit.
- Memory file plus searchable SQLite runtime memory.
- Account management from inside the Genesis REPL.

## Requirements

- Windows
- Python 3.10 or later
- Git
- Claude Code CLI installed and logged in
- Codex CLI installed and logged in
- Node.js 18 or later if installing Codex through npm

## Quick Start

Clone and install Genesis:

```powershell
git clone https://github.com/AmRitJain0442/Genesis.git
cd Genesis
pip install -e .
```

Install and authenticate the required agent CLIs:

```powershell
claude login
codex login
```

Create the Genesis config:

```powershell
genesis init
```

Start Genesis inside any git repository:

```powershell
cd C:\Projects\my-app
genesis
```

Run a task:

```text
genesis> run build a REST API with user auth, a PostgreSQL backend, and pytest tests
```

## Setup Guide

### 1. Install Python

Download Python 3.10 or later from:

```text
https://python.org/downloads
```

During installation, enable `Add Python to PATH`.

Verify the installation:

```powershell
python --version
```

### 2. Install Claude Code CLI

Download Claude Code from:

```text
https://claude.ai/download
```

Log in:

```powershell
claude login
```

Verify it works:

```powershell
claude --version
```

### 3. Install Codex CLI

Install Codex through npm:

```powershell
npm install -g @openai/codex
```

Log in:

```powershell
codex login
```

Verify it works:

```powershell
codex --version
```

### 4. Configure Genesis

Create the config file:

```powershell
genesis init
```

This creates:

```text
~/.genesis/config.toml
```

Recommended orchestrator settings:

```toml
[orchestrator]
provider = "claude-cli"
model = "claude-sonnet-4-6"
```

Recommended worker settings:

```toml
[worker]
provider = "codex-cli"
model = "auto"
```

Check the system:

```powershell
genesis status
```

Expected agent roster:

```text
Agents:   Claude Code . Codex
Active:   claude-cli-orchestrator, claude-cli-worker, codex-main
```

If either CLI is missing, run `claude login` or `codex login` again in the same terminal.

## Commands

| Command | Description |
| --- | --- |
| `run <task>` | Execute a task through the AI orchestrator. |
| `plan <task>` | Generate, save, and preview a plan without executing it. |
| `resume <run_id>` | Resume a durable run from stored state. |
| `retry <run_id> <step_id>` | Retry a blocked step, then continue the run. |
| `runs` | Show recent durable runs. |
| `inspect <run_id>` | Show run state and event trace. |
| `cleanup <run_id>` | Remove stale isolated worktrees for a run. |
| `status` | Show agents, config, and recent git log. |
| `usage [--refresh] [--json]` | Aggregate quota windows, reset times, capacity graphs, and recorded run cost across linked accounts. |
| `agents` | List registered agents and their status. |
| `config show` | Print the active configuration. |
| `config edit` | Open the config file in your editor. |
| `git log` | Show recent Genesis commits. |
| `git commit [message]` | Manually commit current changes. |
| `memory show` | Print the shared memory file. |
| `memory search <query>` | Search SQLite memory. |
| `memory mine` | Import `GENESIS_MEMORY.md` into SQLite memory. |
| `memory clear` | Reset memory for a new project. |
| `memory append <text>` | Add a manual note to memory. |
| `switch orchestrator <name>` | Hot-swap the orchestrator agent. |
| `switch worker <name>` | Hot-swap the default worker agent. |
| `add-account` | Add a Codex account interactively. |
| `remove-account <name>` | Remove one Codex account from Genesis. |
| `remove-all-accounts` | Remove every Codex account from Genesis. |
| `help` | Show all commands. |
| `exit` | Quit Genesis. |

## Codex Account Management

Genesis can register multiple Codex accounts as separate workers. Each account uses a separate `CODEX_HOME` directory and its own login session.

Set `reserve = true` on a last-resort account to keep it out of normal worker
assignment and the Codex brain slot. Genesis unlocks that account only after
every non-reserve Codex worker has reported a rate, usage, or quota limit;
workers that are merely busy do not unlock it. All Codex worker accounts use
the same write-enabled execution mode inside their isolated git worktrees.

For example, keep `CODEX-200` last while three Terra workers are available:

```toml
[[codex_cli.accounts]]
name = "Codex-harshita"
home = "C:/Users/you/.codex-harshita"

[[codex_cli.accounts]]
name = "Codex-post-1"
home = "C:/Users/you/.codex-post-1"

[[codex_cli.accounts]]
name = "Codex-post-2"
home = "C:/Users/you/.codex-post-2"

[[codex_cli.accounts]]
name = "CODEX-200"
home = "C:/Users/you/.codex-200"
reserve = true
```

Add an account:

```text
genesis> add-account
```

Remove one account:

```text
genesis> remove-account codex-2
```

Remove all registered Codex accounts:

```text
genesis> remove-all-accounts
```

Remove accounts and delete non-default `CODEX_HOME` folders:

```text
genesis> remove-all-accounts --delete-home
```

Genesis never deletes the default `~/.codex` directory through `--delete-home`. Use `codex logout` if you want to clear the global Codex login.

## Command-Center UI

The live dashboard is designed for repeated software work rather than a one-off chat session.

It automatically changes its information density for wide, standard, and narrow
terminals. Wide windows show the full plan, output, team, quality gates, event
trace, and telemetry together; smaller windows keep the active work and rolling
output readable instead of compressing every panel into unusable columns.

| Area | What It Shows |
| --- | --- |
| Header | Task, phase, elapsed time, current step, active worker, active reviewer, latest commit, and progress. |
| Execution Plan | Step status, effective scope, repair count, title, and completion state. |
| Agent Output | Streaming worker commands, file changes, review results, repair attempts, verification output, commits, and errors. |
| Team | Configured agents with role and active state. |
| Quality Gates | Review and verification status per step. |
| Event Trace | Recent leases, reviews, repairs, verification, commits, and release summaries. |
| Telemetry | Input tokens, output tokens, cached tokens, cost, and per-agent usage. |

Static REPL views use the same command-center styling. `status` shows the agent roster and runtime controls, `runs` lists recent runs, `inspect <run_id>` opens the diagnostic view, and `plan <task>` previews dependencies and file scopes before execution.

### Account capacity and cost

Run `genesis usage` (or type `usage` in the interactive terminal) for one
capacity readout across every configured Claude and Codex login:

```powershell
genesis usage
genesis usage --refresh
genesis usage --json
```

The terminal view graphs each provider's short and weekly windows, shows reset
times, and sums the remaining percentages as account-equivalent capacity. Reads
run concurrently, successful results are cached for 60 seconds, and a failed or
expired login is isolated to its own row. A live failure falls back to the last
good snapshot and is marked `STALE`.

Genesis also records the token and dollar telemetry emitted by completed CLI
calls in `~/.genesis/state/usage.db`. The readout shows 24-hour, seven-day, and
all-time recorded run cost. This history starts when this feature is installed.
Codex subscription accounts do not expose a dollar cost, so Genesis reports
their exact capacity without inventing a price estimate.

### Live transcript viewer

Every run can expose a dependency-free, read-only observer at a localhost URL.
The viewer is an operations log for the agent team: rooms are ordered by recent
activity, decisions and code are visually distinct, transcripts are searchable,
and the connection indicator reports live/reconnecting/offline state. It follows
new activity only while you are already at the bottom, so reading earlier work is
not interrupted.

From the interactive terminal, start the viewer or open it in your browser:

```text
chat
chat open
```

The viewer remains self-contained and works offline; no messages or assets are
sent to a third-party frontend service.

Plans are retained in SQLite as soon as planning completes. Running the same
task text reuses its saved unfinished plan; interrupted runs continue from their
stored step state, and repeating a blocked task retries its blocked portion
without paying for another planning pass.

## Memory

Genesis writes a `GENESIS_MEMORY.md` file to the root of your repository. This records task plans, committed step outcomes, blocked attempts, and completion timestamps. Writes are synchronized and durable, and prompt wakeups read only the bounded tail rather than scanning an ever-growing file.

Workers and reviewers receive the retained task, complete plan, dependency
state, prior step results, and project memory. Review prompts include a bounded
sample of large patches plus a bounded changed-file manifest, preventing a
generated or binary file from overflowing the reviewer's context window.

Genesis also keeps a local SQLite state database:

```text
~/.genesis/state/genesis.db
```

SQLite stores durable run events, checkpoints, verification results, patch artifacts, and searchable memory entries. State transitions and their audit events commit together under a WAL-backed concurrency policy. Markdown memory remains the human-readable project log, while full patches are retained once as runtime artifacts instead of being duplicated into the search index.

Search memory:

```text
genesis> memory search authentication middleware
```

Import an existing memory file:

```text
genesis> memory mine
```

Clear memory for a conceptually new project:

```text
genesis> memory clear
```

## Git Integration

With `auto_commit = true`, Genesis commits only after review and verification pass:

```text
[genesis] step-2: Implement authentication middleware
[genesis] task-complete: Build REST API with auth and tests
```

Enable auto-push in `~/.genesis/config.toml`:

```toml
[git]
auto_push = true
remote = "origin"
branch = "main"
```

Genesis runs workers in isolated git worktrees under:

```text
~/.genesis/worktrees/
```

A task can start with tracked, staged, or untracked local changes. Genesis first
preserves those changes in a visible `[genesis] checkpoint` commit, then creates
isolated worktrees from that exact project state. In a brand-new `git init`
repository, this checkpoint also becomes the initial commit. Genesis runtime
state and the configured memory file are excluded from the checkpoint.

A worker patch is captured, stored in SQLite, reviewed, verified in isolation, checked with `git apply --check`, then applied to the main repository and committed. Failed or rejected steps leave the main repository unchanged and can be inspected or retried.

### Evidence and review integrity

Worker summaries are advisory. Genesis builds its own evidence from Git after
every turn: base and head revisions, changed-file manifest, status, deletion
status, patch content, and patch SHA. Brain feedback and independent review see
that evidence rather than trusting a claim such as “all files were fixed.”

Before review, deterministic gates reject cache/build noise, deletions outside
the declared scope, explicitly requested artifacts that do not exist, tracked
credential-shaped files, unpinned requirements when pinning was requested,
literal secret fallbacks, and likely hardcoded secrets or endpoints. If a task
explicitly requires `gitleaks` or `trufflehog`, an unavailable scanner is
reported as unavailable and never mislabeled as a clean scan.

Recoverable failures do not immediately discard the step. Genesis retains the
isolated worktree, gives the worker the exact observed evidence, captures a new
patch version, and runs the complete gate -> independent review -> verification
pipeline again. One shared `runtime.retry_budget` bounds all automatic repairs
for the step, persists across crash/resume, and resets only for an explicit
operator retry. Automatic and explicit retries continue in the retained
worktree, so useful draft changes are not discarded. An unchanged repair is
never re-reviewed as if it were new.
If current main no longer accepts an approved patch, Genesis keeps the reviewed
draft, opens a fresh isolated worktree from current committed main, and spends
the same bounded budget on a reconciliation turn. A transient commit failure
is rolled back path-for-path, then the immutable candidate is revalidated and
retried; an unsafe or unclean rollback remains a hard block.
Tooling or policy failures that code changes cannot fix remain hard blocks.

External secret scanners run at the final patch gate, not after every worker
turn. Genesis detects the installed Gitleaks command set (`detect` on older
versions and `dir` on newer versions), runs independent scanners concurrently,
and caches results by repository, patch, scanner binary, command, and scanner
configuration. Install scanners on `PATH`, in `~/go/bin`, or in
`~/.genesis/tools`; Genesis does not execute binaries discovered in temporary
download folders.

Each verdict and verification result records the exact patch SHA and version it
observed. A repair creates a new patch version, clears superseded review and
verification state, and requires both stages again. Crash recovery and the main
repository refuse any result whose verified, approved, and current SHAs do not
all match.

Ignored source is not copied wholesale. Genesis may overlay only a source file
explicitly named by the task, excludes credential-shaped files, and preserves
its modification as a reviewable patch. Explicit safe templates such as
`.env.example` can still be captured even when a broad `.env*` rule ignores
them.

Genesis uses up to three workers by default when independent steps have
non-overlapping file scopes. A failed branch blocks its dependents but does not
stop unrelated branches. Accepted patches are still applied one at a time to
the main repository.

## Configuration Reference

Full `~/.genesis/config.toml` example:

```toml
[orchestrator]
provider = "claude-cli"
model = "claude-sonnet-4-6"

[worker]
provider = "codex-cli"
model = "auto"

[claude_cli]
command = "claude"
timeout = 300

[codex_cli]
command = "codex"
timeout = 600 # inactivity timeout; active worker events reset it

[[codex_cli.accounts]]
name = "codex-main"
home = ""
model = "auto"
# reserve = true # last-resort account; unlocks after normal accounts exhaust

[collaboration]
enabled = true
max_rounds = 2

[dialogue]
enabled = true
max_turns = 2
fast_path = true # deterministic preflight passes go directly to independent review

[failover]
enabled = true
cooldown_seconds = 900

[git]
auto_commit = true
auto_push = false
remote = "origin"
branch = "main"
commit_prefix = "[genesis]"

[memory]
file = "GENESIS_MEMORY.md"
max_context_chars = 6000
auto_append_plan = true
palace_enabled = true

[runtime]
state_db = ""
retry_budget = 2 # shared automatic repair attempts per step
max_parallel_workers = 3
checkpoint_mode = "always"

[verification]
commands = []
timeout = 300
require_for_commit = true

[policy]
file = "genesis.policy.toml"
protected_paths = [".git/", ".genesis/state/"]
blocked_commands = ["git reset --hard", "git checkout --", "Remove-Item -Recurse -Force", "rm -rf /"]
allowed_commands = []
```

## Architecture

```text
genesis/
  agents/
    orchestrator.py    Planning, review, scheduling, and run control
    claude_cli.py      Claude Code subprocess adapter
    codex_cli.py       Codex CLI subprocess adapter
    worker.py          Claude worker implementation
    codex_worker.py    Codex worker implementation
  schemas/
    plan.py            Plan and Step models
    review.py          Review model
  ui/
    dashboard.py       Live dashboard state and layout
    console.py         Shared Rich Console singleton
  config.py            TOML config loader and dataclasses
  palace.py            SQLite memory store with FTS search
  runtime.py           Durable run events and artifacts
  scheduler.py         Dependency and file-scope worker leasing
  worktree.py          Isolated git worktrees and patch application
  evidence.py          Patch guards and deterministic acceptance gates
  policy.py            Protected path and command policy checks
  verifier.py          Configurable verification gates
  memory.py            GENESIS_MEMORY.md reader and writer
  git_ops.py           GitPython wrapper
  repl.py              REPL loop and command dispatch
  main.py              CLI entry point
```

## Development

Install in editable mode:

```powershell
pip install -e .
```

Run tests:

```powershell
python -m pytest -q
```

Run the REPL from the repository:

```powershell
python -m genesis.main
```

## Troubleshooting

### Claude says you have hit your limit

Claude Pro rate limit reached. Wait for the reset time shown in the message. To reduce how often this happens, use `claude-sonnet-4-6` instead of `claude-opus-4-6` for the orchestrator.

### Codex exits with code 1

The Codex account is not logged in, or the session expired. Run:

```powershell
codex login
```

### Codex worker reaches 600 seconds while still active

`codex_cli.timeout` is an inactivity watchdog, not an absolute task deadline.
Each streamed Codex JSONL event resets it. If a worker is terminated after this
interval, it produced no observable event for the full window; any partial file
changes are preserved for the next turn. A no-progress stall fails over to the
next normal account without falsely marking the stalled account quota-exhausted.

For a secondary account:

```powershell
set CODEX_HOME=C:/Users/yourname/.codex-2
codex login
```

### No worker agents are available

Open a new terminal, then confirm both CLIs are visible:

```powershell
claude --version
codex --version
```

Run `genesis status` from the same terminal.

### TOML parse error after editing config.toml

Use forward slashes in Windows paths inside TOML strings.

Correct:

```toml
home = "C:/Users/yourname/.codex-2"
```

Incorrect:

```toml
home = "C:\Users\yourname\.codex-2"
```

### Dashboard shows boxes or garbled characters

Set UTF-8 mode before running Genesis:

```powershell
set PYTHONUTF8=1
genesis
```

To make this permanent, add `PYTHONUTF8=1` to your user environment variables in Windows.

## Contributing

Issues and pull requests are welcome. Good contributions for this project include:

- Better CLI adapters and status checks.
- Safer verification defaults.
- More tests around worktree and patch behavior.
- Documentation improvements.
- UI refinements that keep the terminal workflow fast and readable.

Please run the test suite before opening a pull request:

```powershell
python -m pytest -q
```
