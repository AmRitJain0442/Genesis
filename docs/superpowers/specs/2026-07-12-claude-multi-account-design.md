# Claude multi-account support (parity with Codex)

**Date:** 2026-07-12
**Status:** Approved

## Goal

Add REPL commands to register additional Claude Code accounts in Genesis —
mirroring the existing Codex account commands — so the orchestrator/worker can
rotate through multiple Claude logins. Registering a new account must **never
disturb the user's existing default Claude login** (`~/.claude`).

## Background

Codex already supports multiple accounts. Each Codex account has its own
`CODEX_HOME` directory; `CodexCLIAgent` sets `CODEX_HOME` in the subprocess env
so accounts stay isolated. `_build_agents` rotates over `codex_cli.accounts`,
registering each as a worker (and the first as an orchestrator slot).

The Claude side has **no** multi-account support today:
- `ClaudeCLIConfig` only has `command` / `timeout`.
- `ClaudeCodeCLIAgent` never sets a config-dir env var.
- `_build_agents` creates exactly one `claude-cli-orchestrator` and one
  `claude-cli-worker` on the default login.

Claude Code's isolation mechanism is the `CLAUDE_CONFIG_DIR` env var (holds
settings, session history, credentials; defaults to `~/.claude`). It is the
direct analog of `CODEX_HOME`. Login is `claude auth login`; status is
`claude auth status`.

## Design

### 1. Config (`genesis/config.py`)

- New dataclass `ClaudeAccount`:
  - `name: str = "claude-main"`
  - `config_dir: str = ""`  — maps to `CLAUDE_CONFIG_DIR`; empty = default `~/.claude`
  - `model: str = "auto"`
- Extend `ClaudeCLIConfig`:
  - `accounts: list[ClaudeAccount] = field(default_factory=list)`
  - `accounts_explicit: bool = False`
  - keep `command`, `timeout`
- `load_config`: parse `[[claude_cli.accounts]]` mirroring the Codex block;
  set `accounts_explicit = "accounts" in c`.

### 2. Agent (`genesis/agents/claude_cli.py`)

- `ClaudeCodeCLIAgent.__init__` gains `config_dir: str = ""`, stored normalized
  (`str(Path(config_dir))` when set, else `""`).
- `_call` and `_call_streaming`: build `env = os.environ.copy()`, set
  `env["CLAUDE_CONFIG_DIR"] = self.config_dir` when set, pass `env=env` to the
  subprocess. Import `os`.
- **Isolation guarantee:** an account with its own `config_dir` never touches
  the default `~/.claude`.

### 3. Agent building (`_build_agents` in `genesis/repl.py`)

Rotate Claude accounts exactly like Codex:
- `accounts = cfg.claude_cli.accounts`; if empty and not `accounts_explicit`,
  synthesize `[ClaudeAccount(name="claude-main", config_dir="", model="")]`.
  This preserves today's single-default-login behavior.
- `i == 0` registers `claude-cli-orchestrator` (keeps orchestrator preference in
  `_get_orchestrator`).
- Every account registers as a worker keyed by `account.name` (falls back to
  `claude-worker-{i+1}`). Names contain `claude`, so `_assign_worker`'s
  substring check keeps treating them as Claude workers.
- Model resolution: when `account.model` is a real model (not `""`/`auto`/`default`),
  use it for both slots; otherwise fall back to `cfg.orchestrator.model` /
  `cfg.worker.model` when the role provider is `claude-cli`, else
  `claude-sonnet-4-6` — matching current behavior.

### 4. REPL commands (`genesis/repl.py`)

- `add-claude-account` (aliases `add_claude_account`, `addclaudeaccount`):
  1. Prompt for account name (default `claude-<n>`).
  2. Prompt for `CLAUDE_CONFIG_DIR` (default `~/.claude-<name>`), create if missing.
  3. Run `claude auth login` with `CLAUDE_CONFIG_DIR` set in env.
  4. Verify: `claude auth status` (env-scoped) returncode 0; fallback — a
     credentials file (`.credentials.json`) present in the config dir.
  5. Append the `[[claude_cli.accounts]]` block (forward-slash path), reload
     config, rebuild agents, show status.
- `remove-claude-account <name> [--delete-config-dir]` (aliases
  `remove_claude_account`, `removeclaudeaccount`): mirror `cmd_remove_account`.
- `remove-all-claude-accounts [--yes] [--delete-config-dir]` (aliases
  `remove_all_claude_accounts`, `remove-claude-accounts`, ...): mirror
  `cmd_remove_all_accounts`.
- `--delete-config-dir` refuses to delete the default `~/.claude` (and the home
  root), mirroring `_delete_codex_home_dirs`.
- Existing Codex commands (`add-account`, `remove-account`,
  `remove-all-accounts`) are untouched and stay Codex-only.

### 5. TOML rewrite helpers — small generalization

The `_..._codex_account...` helpers become section-parametrized
(`section: "codex_cli" | "claude_cli"`):
- Generalize `_is_*_account_header`, `_is_commented_*_account_header`,
  `_extract_*_account_name` (name regex is already section-agnostic),
  `_remove_empty_*_account_markers`, `_ensure_empty_*_accounts_marker`,
  `_rewrite_*_accounts_text` to take `section`.
- Keep `_rewrite_codex_accounts_text(...)` as a thin wrapper over the generalized
  `_rewrite_accounts_text(..., section="codex_cli")` — the existing test imports
  this symbol.
- Add `_rewrite_claude_accounts_text(...)` wrapper for `section="claude_cli"`.
- Empty-accounts marker comment text is parametrized ("Codex"/"Claude").

### 6. Help text + tests

- Add Claude rows to the Accounts help section
  (`add-claude-account`, `remove-claude-account <name>`,
  `remove-all-claude-accounts`, `... --delete-config-dir`).
- New `tests/test_claude_account_config.py` mirroring
  `test_codex_account_config.py`:
  - remove-one preserves remaining blocks + comments,
  - remove-all writes explicit `accounts = []`,
  - loader marks empty `accounts = []` as `accounts_explicit`.

## Non-goals

- No change to how the orchestrator chooses Codex-over-Claude for workers.
- No interactive TUI beyond the prompt-based login flow already used by Codex.
- No migration of existing config files (the default synthesized account keeps
  current behavior when no `accounts` key is present).

## Risks / notes

- `claude auth login` / `claude auth status` are recent subcommands. If a user's
  CLI predates them the status check may fail; the credentials-file fallback and
  a clear error message mitigate this.
- Worker-vs-orchestrator preference relies on the substring `claude` in the
  agent key, so account names should start with `claude-` (the command defaults
  them that way).
