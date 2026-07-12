from __future__ import annotations
import sys
import shutil
from pathlib import Path
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

CONFIG_DIR = Path.home() / ".genesis"
CONFIG_FILE = CONFIG_DIR / "config.toml"

_DEFAULT_CONFIG = """\
# Genesis Configuration
# Run 'genesis init' to create this file, then edit as needed.

[orchestrator]
# The director agent: plans tasks, assigns workers, reviews results.
provider = "claude-cli"          # "claude-cli" | "chatgpt-browser"
model = "claude-sonnet-4-6"      # sonnet has higher Pro rate limits than opus

[worker]
# The executor agents: write code, docs, tests, configs.
provider = "codex-cli"           # "codex-cli" | "claude-cli" | "chatgpt-browser"
model = "auto"

[claude_cli]
# Settings for the Claude Code CLI agent.
# Requires: `claude` is installed and you are logged in (`claude login`).
command = "claude"               # path to the claude binary (auto-detected if in PATH)
timeout = 300                    # seconds to wait for a response

[codex_cli]
command = "codex"
timeout = 600
model = "auto"

[[codex_cli.accounts]]
name = "codex-main"
home = ""
model = "auto"

[chatgpt_browser]
# Settings for the optional ChatGPT browser agent.
# Requires: pip install playwright && playwright install chromium
enabled = false
headless = true
profile_dir = ""                 # set to ~/.genesis/chatgpt_profile to persist login

[git]
auto_commit = true               # commit after each approved step
auto_push = false                # push to remote after each commit
remote = "origin"
branch = "main"
commit_prefix = "[genesis]"

[runtime]
state_db = ""                    # empty = ~/.genesis/state/genesis.db
retry_budget = 1                 # bounded self-repair attempts per failed review/verification
max_parallel_workers = 1         # safe parallel worker leases; increase when multiple workers exist
checkpoint_mode = "always"

[memory]
file = "GENESIS_MEMORY.md"       # created in the current working directory
max_context_chars = 6000         # max memory chars injected into prompts
auto_append_plan = true          # write the task plan to memory at start
palace_enabled = true            # persist searchable verbatim memory in SQLite

[verification]
commands = []                    # e.g. ["python -m compileall genesis"]
timeout = 300
require_for_commit = true

[policy]
file = "genesis.policy.toml"
protected_paths = [".git/", ".genesis/state/"]
blocked_commands = ["git reset --hard", "git checkout --", "Remove-Item -Recurse -Force", "rm -rf /"]
allowed_commands = []            # empty = allow all except blocked
"""


@dataclass
class OrchestratorConfig:
    provider: str = "claude-cli"
    model: str = "claude-sonnet-4-6"


@dataclass
class WorkerConfig:
    provider: str = "codex-cli"
    model: str = "auto"


@dataclass
class ClaudeAccount:
    name: str = "claude-main"
    config_dir: str = ""    # CLAUDE_CONFIG_DIR — empty = system default (~/.claude)
    model: str = "auto"


@dataclass
class ClaudeCLIConfig:
    command: str = "claude"
    timeout: int = 300
    accounts: list[ClaudeAccount] = field(default_factory=list)
    accounts_explicit: bool = False


@dataclass
class CodexAccount:
    name: str = "codex-main"
    home: str = ""          # CODEX_HOME — empty = system default (~/.codex)
    model: str = "auto"


@dataclass
class CodexCLIConfig:
    command: str = "codex"
    timeout: int = 600
    model: str = "auto"     # fallback model if no accounts are defined
    accounts: list[CodexAccount] = field(default_factory=list)
    accounts_explicit: bool = False


@dataclass
class ChatroomConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 0           # 0 = ephemeral (OS picks a free port)
    persist: bool = True     # write per-room JSONL under .genesis/state/chatrooms
    open_browser: bool = False


@dataclass
class ChatGPTBrowserConfig:
    enabled: bool = False
    headless: bool = True
    profile_dir: str = ""
    model: str = "gpt-4o"


@dataclass
class GitConfig:
    auto_commit: bool = True
    auto_push: bool = False
    remote: str = "origin"
    branch: str = "main"
    commit_prefix: str = "[genesis]"


@dataclass
class RuntimeConfig:
    state_db: str = ""
    retry_budget: int = 1
    max_parallel_workers: int = 1
    checkpoint_mode: str = "always"


@dataclass
class MemoryConfig:
    file: str = "GENESIS_MEMORY.md"
    max_context_chars: int = 6000
    auto_append_plan: bool = True
    palace_enabled: bool = True


@dataclass
class VerificationConfig:
    commands: list[str] = field(default_factory=list)
    timeout: int = 300
    require_for_commit: bool = True


@dataclass
class PolicyConfig:
    file: str = "genesis.policy.toml"
    protected_paths: list[str] = field(default_factory=lambda: [".git/", ".genesis/state/"])
    blocked_commands: list[str] = field(
        default_factory=lambda: [
            "git reset --hard",
            "git checkout --",
            "Remove-Item -Recurse -Force",
            "rm -rf /",
        ]
    )
    allowed_commands: list[str] = field(default_factory=list)


@dataclass
class GenesisConfig:
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    claude_cli: ClaudeCLIConfig = field(default_factory=ClaudeCLIConfig)
    codex_cli: CodexCLIConfig = field(default_factory=CodexCLIConfig)
    chatgpt_browser: ChatGPTBrowserConfig = field(default_factory=ChatGPTBrowserConfig)
    chatroom: ChatroomConfig = field(default_factory=ChatroomConfig)
    git: GitConfig = field(default_factory=GitConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)


def load_config() -> GenesisConfig:
    if not CONFIG_FILE.exists() or tomllib is None:
        return GenesisConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    cfg = GenesisConfig()

    if o := data.get("orchestrator"):
        cfg.orchestrator = OrchestratorConfig(
            provider=o.get("provider", "claude-cli"),
            model=o.get("model", "claude-sonnet-4-6"),
        )

    if w := data.get("worker"):
        # Fall back to the dataclass defaults (codex-cli / auto) so a partial
        # [worker] table can't silently flip the worker over to Claude.
        defaults = WorkerConfig()
        cfg.worker = WorkerConfig(
            provider=w.get("provider", defaults.provider),
            model=w.get("model", defaults.model),
        )

    if c := data.get("claude_cli"):
        claude_accounts_data = c.get("accounts", [])
        claude_accounts = [
            ClaudeAccount(
                name=a.get("name", f"claude-{i+1}"),
                config_dir=a.get("config_dir", ""),
                model=a.get("model", "auto"),
            )
            for i, a in enumerate(claude_accounts_data)
            if isinstance(a, dict)
        ]
        cfg.claude_cli = ClaudeCLIConfig(
            command=c.get("command", "claude"),
            timeout=c.get("timeout", 300),
            accounts=claude_accounts,
            accounts_explicit="accounts" in c,
        )

    if cx := data.get("codex_cli"):
        accounts_data = cx.get("accounts", [])
        accounts = [
            CodexAccount(
                name=a.get("name", f"codex-{i+1}"),
                home=a.get("home", ""),
                model=a.get("model", "auto"),
            )
            for i, a in enumerate(accounts_data)
            if isinstance(a, dict)
        ]
        cfg.codex_cli = CodexCLIConfig(
            command=cx.get("command", "codex"),
            timeout=cx.get("timeout", 600),
            model=cx.get("model", "auto"),
            accounts=accounts,
            accounts_explicit="accounts" in cx,
        )

    if b := data.get("chatgpt_browser"):
        cfg.chatgpt_browser = ChatGPTBrowserConfig(
            enabled=b.get("enabled", False),
            headless=b.get("headless", True),
            profile_dir=b.get("profile_dir", ""),
            model=b.get("model", "gpt-4o"),
        )

    if cr := data.get("chatroom"):
        cfg.chatroom = ChatroomConfig(
            enabled=cr.get("enabled", True),
            host=cr.get("host", "127.0.0.1"),
            port=cr.get("port", 0),
            persist=cr.get("persist", True),
            open_browser=cr.get("open_browser", False),
        )

    if g := data.get("git"):
        cfg.git = GitConfig(
            auto_commit=g.get("auto_commit", True),
            auto_push=g.get("auto_push", False),
            remote=g.get("remote", "origin"),
            branch=g.get("branch", "main"),
            commit_prefix=g.get("commit_prefix", "[genesis]"),
        )

    if r := data.get("runtime"):
        cfg.runtime = RuntimeConfig(
            state_db=r.get("state_db", ""),
            retry_budget=r.get("retry_budget", 1),
            max_parallel_workers=r.get("max_parallel_workers", 1),
            checkpoint_mode=r.get("checkpoint_mode", "always"),
        )

    if m := data.get("memory"):
        cfg.memory = MemoryConfig(
            file=m.get("file", "GENESIS_MEMORY.md"),
            max_context_chars=m.get("max_context_chars", 6000),
            auto_append_plan=m.get("auto_append_plan", True),
            palace_enabled=m.get("palace_enabled", True),
        )

    if v := data.get("verification"):
        cfg.verification = VerificationConfig(
            commands=list(v.get("commands", [])),
            timeout=v.get("timeout", 300),
            require_for_commit=v.get("require_for_commit", True),
        )

    if p := data.get("policy"):
        cfg.policy = PolicyConfig(
            file=p.get("file", "genesis.policy.toml"),
            protected_paths=list(p.get("protected_paths", [".git/", ".genesis/state/"])),
            blocked_commands=list(
                p.get(
                    "blocked_commands",
                    [
                        "git reset --hard",
                        "git checkout --",
                        "Remove-Item -Recurse -Force",
                        "rm -rf /",
                    ],
                )
            ),
            allowed_commands=list(p.get("allowed_commands", [])),
        )

    return cfg


def init_config(force: bool = False) -> Path:
    """Create ~/.genesis/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists() and not force:
        return CONFIG_FILE

    # Try to copy from the package's example file
    example = Path(__file__).parent.parent / "config.example.toml"
    if example.exists():
        shutil.copy(example, CONFIG_FILE)
    else:
        CONFIG_FILE.write_text(_DEFAULT_CONFIG, encoding="utf-8")

    return CONFIG_FILE


_config_cache: GenesisConfig | None = None


def get_config() -> GenesisConfig:
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reset_config_cache() -> None:
    global _config_cache
    _config_cache = None
