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
model = "claude-opus-4-6"        # alias "opus" also works

[worker]
# The executor agents: write code, docs, tests, configs.
provider = "claude-cli"          # "claude-cli" | "chatgpt-browser"
model = "claude-sonnet-4-6"      # alias "sonnet" also works

[claude_cli]
# Settings for the Claude Code CLI agent.
# Requires: `claude` is installed and you are logged in (`claude login`).
command = "claude"               # path to the claude binary (auto-detected if in PATH)
timeout = 300                    # seconds to wait for a response

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

[memory]
file = "GENESIS_MEMORY.md"       # created in the current working directory
max_context_chars = 6000         # max memory chars injected into prompts
auto_append_plan = true          # write the task plan to memory at start
"""


@dataclass
class OrchestratorConfig:
    provider: str = "claude-cli"
    model: str = "claude-opus-4-6"


@dataclass
class WorkerConfig:
    provider: str = "claude-cli"
    model: str = "claude-sonnet-4-6"


@dataclass
class ClaudeCLIConfig:
    command: str = "claude"
    timeout: int = 300


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
class MemoryConfig:
    file: str = "GENESIS_MEMORY.md"
    max_context_chars: int = 6000
    auto_append_plan: bool = True


@dataclass
class GenesisConfig:
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    claude_cli: ClaudeCLIConfig = field(default_factory=ClaudeCLIConfig)
    codex_cli: CodexCLIConfig = field(default_factory=CodexCLIConfig)
    chatgpt_browser: ChatGPTBrowserConfig = field(default_factory=ChatGPTBrowserConfig)
    git: GitConfig = field(default_factory=GitConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


def load_config() -> GenesisConfig:
    if not CONFIG_FILE.exists() or tomllib is None:
        return GenesisConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    cfg = GenesisConfig()

    if o := data.get("orchestrator"):
        cfg.orchestrator = OrchestratorConfig(
            provider=o.get("provider", "claude-cli"),
            model=o.get("model", "claude-opus-4-6"),
        )

    if w := data.get("worker"):
        cfg.worker = WorkerConfig(
            provider=w.get("provider", "claude-cli"),
            model=w.get("model", "claude-sonnet-4-6"),
        )

    if c := data.get("claude_cli"):
        cfg.claude_cli = ClaudeCLIConfig(
            command=c.get("command", "claude"),
            timeout=c.get("timeout", 300),
        )

    if cx := data.get("codex_cli"):
        accounts = [
            CodexAccount(
                name=a.get("name", f"codex-{i+1}"),
                home=a.get("home", ""),
                model=a.get("model", "auto"),
            )
            for i, a in enumerate(cx.get("accounts", []))
        ]
        cfg.codex_cli = CodexCLIConfig(
            command=cx.get("command", "codex"),
            timeout=cx.get("timeout", 600),
            model=cx.get("model", "auto"),
            accounts=accounts,
        )

    if b := data.get("chatgpt_browser"):
        cfg.chatgpt_browser = ChatGPTBrowserConfig(
            enabled=b.get("enabled", False),
            headless=b.get("headless", True),
            profile_dir=b.get("profile_dir", ""),
            model=b.get("model", "gpt-4o"),
        )

    if g := data.get("git"):
        cfg.git = GitConfig(
            auto_commit=g.get("auto_commit", True),
            auto_push=g.get("auto_push", False),
            remote=g.get("remote", "origin"),
            branch=g.get("branch", "main"),
            commit_prefix=g.get("commit_prefix", "[genesis]"),
        )

    if m := data.get("memory"):
        cfg.memory = MemoryConfig(
            file=m.get("file", "GENESIS_MEMORY.md"),
            max_context_chars=m.get("max_context_chars", 6000),
            auto_append_plan=m.get("auto_append_plan", True),
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
