from __future__ import annotations
import os
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

[orchestrator]
provider = "claude"
model = "claude-opus-4-6"
max_tokens = 4096

[worker]
provider = "claude"
model = "claude-sonnet-4-6"
max_tokens = 8192

[api_keys]
anthropic = ""
openai = ""

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
"""


@dataclass
class OrchestratorConfig:
    provider: str = "claude"
    model: str = "claude-opus-4-6"
    max_tokens: int = 4096


@dataclass
class WorkerConfig:
    provider: str = "claude"
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192


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
class ApiKeys:
    anthropic: str = ""
    openai: str = ""


@dataclass
class GenesisConfig:
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    git: GitConfig = field(default_factory=GitConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    api_keys: ApiKeys = field(default_factory=ApiKeys)

    def get_anthropic_key(self) -> str:
        return self.api_keys.anthropic or os.environ.get("ANTHROPIC_API_KEY", "")

    def get_openai_key(self) -> str:
        return self.api_keys.openai or os.environ.get("OPENAI_API_KEY", "")


def load_config() -> GenesisConfig:
    if not CONFIG_FILE.exists() or tomllib is None:
        return GenesisConfig()

    with open(CONFIG_FILE, "rb") as f:
        data = tomllib.load(f)

    cfg = GenesisConfig()

    if o := data.get("orchestrator"):
        cfg.orchestrator = OrchestratorConfig(
            provider=o.get("provider", "claude"),
            model=o.get("model", "claude-opus-4-6"),
            max_tokens=o.get("max_tokens", 4096),
        )

    if w := data.get("worker"):
        cfg.worker = WorkerConfig(
            provider=w.get("provider", "claude"),
            model=w.get("model", "claude-sonnet-4-6"),
            max_tokens=w.get("max_tokens", 8192),
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

    if k := data.get("api_keys"):
        cfg.api_keys = ApiKeys(
            anthropic=k.get("anthropic", ""),
            openai=k.get("openai", ""),
        )

    return cfg


def init_config(force: bool = False) -> Path:
    """Create ~/.genesis/config.toml from the example template."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists() and not force:
        return CONFIG_FILE

    # Try to copy from the package's example file first
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
