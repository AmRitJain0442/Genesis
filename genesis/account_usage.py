"""Aggregate provider capacity and locally recorded run cost.

Quota reads are deliberately best-effort: every configured account is queried
independently, failures are isolated, and a last-known-good snapshot is used
when a provider is temporarily unavailable.  No credential value is returned,
logged, cached, or rendered.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rich.console import Group
from rich.text import Text

from genesis.config import CONFIG_DIR, ClaudeAccount, CodexAccount, GenesisConfig
from genesis.ui.theme import command_panel, command_table, markup, progress_bar

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_TOKEN_RE = re.compile(r"Tokens:\s*in=(\d+)\s+out=(\d+)\s*\|\s*\$([0-9.]+)")
_CODEX_TOKEN_RE = re.compile(r"Tokens:\s*in=(\d+)\s+\(cached=(\d+)\)\s+out=(\d+)")
_RICH_TAG_RE = re.compile(r"\[/?[^\]]*\]")
_MONEY_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class TokenUsageEvent:
    provider: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0


def parse_token_usage_line(line: str) -> TokenUsageEvent | None:
    """Parse the stable telemetry line emitted by either supported CLI."""
    plain = _RICH_TAG_RE.sub("", str(line))
    if match := _CLAUDE_TOKEN_RE.search(plain):
        return TokenUsageEvent(
            provider="claude-cli",
            input_tokens=int(match.group(1)),
            output_tokens=int(match.group(2)),
            cost_usd=float(match.group(3)),
        )
    if match := _CODEX_TOKEN_RE.search(plain):
        return TokenUsageEvent(
            provider="codex-cli",
            input_tokens=int(match.group(1)),
            cached_tokens=int(match.group(2)),
            output_tokens=int(match.group(3)),
        )
    return None


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used_percent: float
    resets_at: str | int | None = None
    duration_minutes: int | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> UsageWindow | None:
        return cls(**value) if value else None


@dataclass(frozen=True)
class AccountUsage:
    name: str
    provider: str
    model: str = "auto"
    plan: str = "unknown"
    short_window: UsageWindow | None = None
    weekly_window: UsageWindow | None = None
    provider_cost: float | None = None
    currency: str = "USD"
    credit_balance: str | None = None
    status: str = "live"
    error: str = ""
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AccountUsage:
        data = dict(value)
        data["short_window"] = UsageWindow.from_dict(data.get("short_window"))
        data["weekly_window"] = UsageWindow.from_dict(data.get("weekly_window"))
        return cls(**data)


@dataclass(frozen=True)
class LedgerSummary:
    cost_24h: float = 0.0
    cost_7d: float = 0.0
    cost_total: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    by_account: dict[str, float] | None = None


@dataclass(frozen=True)
class UsageReport:
    accounts: tuple[AccountUsage, ...]
    ledger: LedgerSummary
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        short = _capacity_summary(self.accounts, "short_window")
        weekly = _capacity_summary(self.accounts, "weekly_window")
        return {
            "generated_at": self.generated_at,
            "accounts": [account.to_dict() for account in self.accounts],
            "capacity": {"short": short, "weekly": weekly},
            "recorded_run_cost_usd": asdict(self.ledger),
        }


class UsageLedger:
    """Small process-safe SQLite ledger for telemetry emitted by Genesis runs."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_DIR / "state" / "usage.db"
        self._lock = threading.RLock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        if not self._ready:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at REAL NOT NULL,
                    account TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS usage_events_time_idx "
                "ON usage_events(occurred_at)"
            )
            connection.commit()
            self._ready = True
        return connection

    def record(self, account: str, event: TokenUsageEvent) -> bool:
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO usage_events (
                        occurred_at, account, provider, input_tokens,
                        output_tokens, cached_tokens, cost_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        account or "unattributed",
                        event.provider,
                        event.input_tokens,
                        event.output_tokens,
                        event.cached_tokens,
                        event.cost_usd,
                    ),
                )
            return True
        except (OSError, sqlite3.Error) as exc:
            # Telemetry must never be able to break a coding run.
            logger.warning("Could not record usage telemetry: %s", exc)
            return False

    def summary(self, *, now: float | None = None) -> LedgerSummary:
        current = time.time() if now is None else now
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN occurred_at >= ? THEN cost_usd ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN occurred_at >= ? THEN cost_usd ELSE 0 END), 0),
                        COALESCE(SUM(cost_usd), 0),
                        COALESCE(SUM(input_tokens), 0),
                        COALESCE(SUM(output_tokens), 0),
                        COALESCE(SUM(cached_tokens), 0)
                    FROM usage_events
                    """,
                    (current - 86400, current - 604800),
                ).fetchone()
                account_rows = connection.execute(
                    "SELECT account, COALESCE(SUM(cost_usd), 0) "
                    "FROM usage_events GROUP BY account"
                ).fetchall()
            assert row is not None
            return LedgerSummary(
                cost_24h=float(row[0]),
                cost_7d=float(row[1]),
                cost_total=float(row[2]),
                input_tokens=int(row[3]),
                output_tokens=int(row[4]),
                cached_tokens=int(row[5]),
                by_account={str(name): float(cost) for name, cost in account_rows},
            )
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Could not read usage telemetry: %s", exc)
            return LedgerSummary(by_account={})


def collect_usage(
    config: GenesisConfig,
    *,
    refresh: bool = False,
    timeout: float = 12.0,
    cache_path: str | Path | None = None,
    ledger: UsageLedger | None = None,
) -> UsageReport:
    """Read every configured account concurrently and isolate provider faults."""
    cache_file = Path(cache_path) if cache_path else CONFIG_DIR / "state" / "usage-cache.json"
    specs = _account_specs(config)
    cached_at, cached = _read_cache(cache_file)
    expected_keys = {_account_key(provider, account.name) for provider, account, _ in specs}
    if (
        not refresh
        and cached_at is not None
        and time.time() - cached_at <= _CACHE_TTL_SECONDS
        and set(cached) == expected_keys
    ):
        accounts = tuple(cached[key] for key in sorted(cached, key=lambda item: item.casefold()))
        return UsageReport(
            accounts=accounts,
            ledger=(ledger or UsageLedger()).summary(),
            generated_at=_utc_now(),
        )

    results: dict[str, AccountUsage] = {}
    if specs:
        with ThreadPoolExecutor(max_workers=min(8, len(specs)), thread_name_prefix="usage") as pool:
            futures = {
                pool.submit(fetcher, account, config, timeout): (provider, account)
                for provider, account, fetcher in specs
            }
            for future in as_completed(futures):
                provider, account = futures[future]
                key = _account_key(provider, account.name)
                try:
                    result = future.result()
                except Exception as exc:  # an adapter bug still cannot sink the report
                    logger.debug("Usage adapter failed for %s: %s", account.name, exc)
                    result = _error_usage(account, provider, "provider query failed")
                old = cached.get(key)
                if result.status == "error" and old and old.status in {"live", "stale"}:
                    result = replace(
                        old,
                        status="stale",
                        error=result.error or "live refresh failed",
                    )
                results[key] = result

    successful = {
        key: value for key, value in results.items() if value.status in {"live", "stale"}
    }
    if successful:
        _write_cache(cache_file, successful)

    ordered = tuple(results[key] for key in sorted(results, key=lambda item: item.casefold()))
    return UsageReport(
        accounts=ordered,
        ledger=(ledger or UsageLedger()).summary(),
        generated_at=_utc_now(),
    )


def usage_report_renderable(report: UsageReport, *, width: int = 120) -> Group:
    """Render a compact capacity control room with terminal-safe bar graphs."""
    short = _capacity_summary(report.accounts, "short_window")
    weekly = _capacity_summary(report.accounts, "weekly_window")
    live = sum(account.status == "live" for account in report.accounts)
    stale = sum(account.status == "stale" for account in report.accounts)
    failed = len(report.accounts) - live - stale

    summary = Text()
    summary.append("LINKED  ", style="dim")
    summary.append(str(len(report.accounts)), style="bold cyan")
    summary.append("     LIVE  ", style="dim")
    summary.append(str(live), style="bold green")
    if stale:
        summary.append(f"     STALE  {stale}", style="bold yellow")
    if failed:
        summary.append(f"     OFFLINE  {failed}", style="bold red")
    summary.append("\nSHORT   ", style="dim")
    summary.append(_aggregate_label(short), style="bold cyan")
    summary.append("     WEEKLY  ", style="dim")
    summary.append(_aggregate_label(weekly), style="bold cyan")
    summary.append("\nRUN COST  ", style="dim")
    summary.append(f"${report.ledger.cost_total:,.4f}", style="bold green")
    summary.append(f"   24H ${report.ledger.cost_24h:,.4f}", style="dim")
    summary.append(f"   7D ${report.ledger.cost_7d:,.4f}", style="dim")

    table = command_table("Capacity by account", border_style="cyan", show_lines=True)
    by_account = report.ledger.by_account or {}
    wide = width >= 118
    compact = width >= 76
    if wide:
        table.add_column("Account", style="bold cyan", min_width=16, overflow="fold")
        table.add_column("Provider / Plan", min_width=14, overflow="fold")
        table.add_column("Short window", min_width=22, overflow="fold")
        table.add_column("Weekly", min_width=22, overflow="fold")
        table.add_column("Reset", min_width=15, overflow="fold")
        table.add_column("Cost", min_width=11, justify="right")
        table.add_column("State", min_width=9)
    elif compact:
        table.add_column("Account", style="bold cyan", ratio=2, min_width=12, overflow="fold")
        table.add_column("Provider / Cost", ratio=2, min_width=12, overflow="fold")
        table.add_column("Short", ratio=3, min_width=16, overflow="fold")
        table.add_column("Weekly", ratio=3, min_width=16, overflow="fold")
        table.add_column("State", ratio=2, min_width=10, overflow="fold")
    else:
        table.add_column("Account", style="bold cyan", ratio=2, min_width=12, overflow="fold")
        table.add_column("Capacity / Reset", ratio=3, min_width=22, overflow="fold")
        table.add_column("State", ratio=2, min_width=11, overflow="fold")

    for account in report.accounts:
        local_cost = by_account.get(account.name, 0.0)
        cost = f"${local_cost:,.4f}"
        if account.provider_cost is not None:
            cost += f"\n{account.currency} {account.provider_cost:,.2f} provider"
        state_style = {
            "live": "bold green",
            "stale": "bold yellow",
            "error": "bold red",
        }.get(account.status, "dim")
        state = Text(account.status.upper(), style=state_style)
        if account.error:
            state.append(f"\n{_short_error(account.error, 34)}", style="dim")
        balance = f"\ncredits {account.credit_balance}" if account.credit_balance else ""
        provider = f"{_provider_label(account.provider)} / {account.plan}{balance}"
        if wide:
            table.add_row(
                markup(account.name),
                markup(provider),
                _window_graph(account.short_window, width=12),
                _window_graph(account.weekly_window, width=12),
                markup(_reset_label(account.short_window, account.weekly_window)),
                markup(cost),
                state,
            )
        elif compact:
            table.add_row(
                markup(account.name),
                markup(f"{provider}\nrun {cost}"),
                _window_graph(account.short_window, width=8, show_reset=True),
                _window_graph(account.weekly_window, width=8, show_reset=True),
                state,
            )
        else:
            identity = f"{account.name}\n{provider}\nrun {cost}"
            capacity = Text()
            capacity.append("SHORT  ", style="dim")
            capacity.append_text(_window_graph(account.short_window, width=6, show_reset=True))
            capacity.append("\nWEEK   ", style="dim")
            capacity.append_text(_window_graph(account.weekly_window, width=6, show_reset=True))
            table.add_row(markup(identity), capacity, state)

    if not report.accounts:
        if wide:
            table.add_row("none", "-", "-", "-", "-", "$0.0000", "NO ACCOUNTS")
        elif compact:
            table.add_row("none", "-", "-", "-", "NO ACCOUNTS")
        else:
            table.add_row("none", "-", "NO ACCOUNTS")

    note = Text()
    note.append("CAPACITY MATH  ", style="bold yellow")
    note.append(
        "Percentages are summed as account-equivalents; providers do not expose a common token limit.\n",
        style="dim",
    )
    note.append("COST SOURCE    ", style="bold yellow")
    note.append(
        "CLI-reported Genesis run cost recorded from now on. Codex subscription dollar spend is not exposed.",
        style="dim",
    )
    return Group(
        command_panel(summary, "ACCOUNT CAPACITY", subtitle=report.generated_at, padding=(1, 2)),
        table,
        command_panel(note, "READOUT NOTES", border_style="yellow"),
    )


def _account_specs(config: GenesisConfig) -> list[tuple[str, Any, Any]]:
    claude_accounts = list(config.claude_cli.accounts)
    if not claude_accounts and not config.claude_cli.accounts_explicit:
        claude_accounts = [ClaudeAccount(name="claude-main")]
    codex_accounts = list(config.codex_cli.accounts)
    if not codex_accounts and not config.codex_cli.accounts_explicit:
        codex_accounts = [CodexAccount(name="codex-main", model=config.codex_cli.model)]
    return [
        *(("claude-cli", account, _fetch_claude_usage) for account in claude_accounts),
        *(("codex-cli", account, _fetch_codex_usage) for account in codex_accounts),
    ]


def _fetch_claude_usage(
    account: ClaudeAccount,
    config: GenesisConfig,
    timeout: float,
) -> AccountUsage:
    del config
    config_dir = _expanded_path(account.config_dir) if account.config_dir else Path.home() / ".claude"
    credentials_path = config_dir / ".credentials.json"
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        oauth = credentials.get("claudeAiOauth") or {}
        token = str(oauth.get("accessToken") or "")
        if not token:
            return _error_usage(account, "claude-cli", "not logged in")
        request = Request(
            _CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "Accept": "application/json",
                "User-Agent": "genesis/0.1.0",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except FileNotFoundError:
        return _error_usage(account, "claude-cli", "credentials not found")
    except HTTPError as exc:
        message = "login expired; re-authenticate this account" if exc.code in {401, 403} else f"HTTP {exc.code}"
        return _error_usage(account, "claude-cli", message)
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return _error_usage(account, "claude-cli", "usage service unavailable")

    weekly_key, weekly_value = _claude_weekly_window(payload, account.model)
    extra = payload.get("extra_usage") or {}
    provider_cost = _normalise_credit_cost(extra)
    return AccountUsage(
        name=account.name,
        provider="claude-cli",
        model=account.model or "auto",
        plan=str(oauth.get("subscriptionType") or "unknown"),
        short_window=_claude_window(payload.get("five_hour"), "5h"),
        weekly_window=_claude_window(weekly_value, _claude_weekly_label(weekly_key)),
        provider_cost=provider_cost,
        currency=str(extra.get("currency") or "USD").upper(),
        status="live",
        fetched_at=_utc_now(),
    )


def _fetch_codex_usage(
    account: CodexAccount,
    config: GenesisConfig,
    timeout: float,
) -> AccountUsage:
    command = shutil.which(config.codex_cli.command) or config.codex_cli.command
    env = os.environ.copy()
    if account.home:
        env["CODEX_HOME"] = str(_expanded_path(account.home))
    process: subprocess.Popen[str] | None = None
    messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
    try:
        process = subprocess.Popen(
            [command, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None

        def read_messages() -> None:
            assert process is not None and process.stdout is not None
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
            messages.put(None)

        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        _send_json(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "genesis", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        _wait_for_response(messages, 1, timeout)
        _send_json(process, {"method": "initialized"})
        _send_json(process, {"id": 2, "method": "account/rateLimits/read"})
        response = _wait_for_response(messages, 2, timeout)
        if response.get("error"):
            message = str((response.get("error") or {}).get("message") or "rate-limit query failed")
            if "401" in message or "invalidated" in message.lower():
                message = "login expired; re-authenticate this account"
            return _error_usage(account, "codex-cli", _short_error(message, 100))

        result = response.get("result") or {}
        snapshot = result.get("rateLimits") or {}
        if not snapshot:
            buckets = result.get("rateLimitsByLimitId") or {}
            snapshot = buckets.get("codex") or next(iter(buckets.values()), {})
        if not snapshot:
            return _error_usage(account, "codex-cli", "no quota data returned")
        individual = snapshot.get("individualLimit") or {}
        credits = snapshot.get("credits") or {}
        short_window, weekly_window = _codex_windows(snapshot)
        return AccountUsage(
            name=account.name,
            provider="codex-cli",
            model=account.model or "auto",
            plan=str(snapshot.get("planType") or "unknown"),
            short_window=short_window,
            weekly_window=weekly_window,
            provider_cost=_money_value(individual.get("used")),
            credit_balance=str(credits.get("balance")) if credits.get("balance") is not None else None,
            status="live",
            fetched_at=_utc_now(),
        )
    except (OSError, subprocess.SubprocessError):
        return _error_usage(account, "codex-cli", "Codex app-server unavailable")
    except TimeoutError:
        return _error_usage(account, "codex-cli", "quota query timed out")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def _send_json(process: subprocess.Popen[str], value: dict[str, Any]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _wait_for_response(
    messages: queue.Queue[dict[str, Any] | None],
    request_id: int,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if message is None:
            raise OSError("app-server closed")
        if message.get("id") == request_id:
            return message


def _claude_window(value: Any, label: str) -> UsageWindow | None:
    if not isinstance(value, dict) or value.get("utilization") is None:
        return None
    return UsageWindow(
        label=label,
        used_percent=float(value["utilization"]),
        resets_at=value.get("resets_at"),
        duration_minutes=300 if label == "5h" else 10080 if label.startswith("7d") else None,
    )


def _claude_weekly_window(payload: dict[str, Any], model: str) -> tuple[str, Any]:
    if payload.get("seven_day") is not None:
        return "seven_day", payload["seven_day"]
    model_key = "seven_day_opus" if "opus" in (model or "").lower() else "seven_day_sonnet"
    if payload.get(model_key) is not None:
        return model_key, payload[model_key]
    for key in ("seven_day_sonnet", "seven_day_opus"):
        if payload.get(key) is not None:
            return key, payload[key]
    return "seven_day", None


def _claude_weekly_label(key: str) -> str:
    suffix = key.removeprefix("seven_day_")
    return "7d" if suffix == key else f"7d {suffix}"


def _normalise_credit_cost(extra: dict[str, Any]) -> float | None:
    value = extra.get("used_credits")
    if value is None:
        return None
    try:
        places = int(extra.get("decimal_places") or 0)
        return float(value) / (10**places)
    except (TypeError, ValueError, OverflowError):
        return None


def _codex_window(value: Any) -> UsageWindow | None:
    if not isinstance(value, dict) or value.get("usedPercent") is None:
        return None
    duration = value.get("windowDurationMins")
    duration_int = int(duration) if duration is not None else None
    return UsageWindow(
        label=_duration_label(duration_int),
        used_percent=float(value["usedPercent"]),
        resets_at=value.get("resetsAt"),
        duration_minutes=duration_int,
    )


def _codex_windows(snapshot: dict[str, Any]) -> tuple[UsageWindow | None, UsageWindow | None]:
    """Classify by duration because some plans return a weekly primary only."""
    windows = [
        window
        for window in (
            _codex_window(snapshot.get("primary")),
            _codex_window(snapshot.get("secondary")),
        )
        if window is not None
    ]
    weekly = next(
        (window for window in windows if (window.duration_minutes or 0) >= 6 * 1440),
        None,
    )
    short = next((window for window in windows if window is not weekly), None)
    return short, weekly


def _duration_label(minutes: int | None) -> str:
    if minutes is None:
        return "short"
    if minutes % 10080 == 0:
        return f"{minutes // 10080}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _error_usage(account: Any, provider: str, message: str) -> AccountUsage:
    return AccountUsage(
        name=str(account.name),
        provider=provider,
        model=str(getattr(account, "model", "auto") or "auto"),
        status="error",
        error=message,
        fetched_at=_utc_now(),
    )


def _expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def _account_key(provider: str, name: str) -> str:
    return f"{provider}:{name}"


def _read_cache(path: Path) -> tuple[float | None, dict[str, AccountUsage]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != 1:
            return None, {}
        accounts = {
            str(key): AccountUsage.from_dict(value)
            for key, value in (payload.get("accounts") or {}).items()
        }
        return float(payload["written_at"]), accounts
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, {}


def _write_cache(path: Path, accounts: dict[str, AccountUsage]) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "written_at": time.time(),
            "accounts": {key: value.to_dict() for key, value in accounts.items()},
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="usage-cache-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, separators=(",", ":"))
            temporary = handle.name
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        logger.debug("Could not update usage cache: %s", exc)
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _capacity_summary(accounts: tuple[AccountUsage, ...], field: str) -> dict[str, float | int]:
    windows = [getattr(account, field) for account in accounts]
    known = [window for window in windows if window is not None]
    remaining = sum(window.remaining_percent for window in known) / 100.0
    return {
        "known_accounts": len(known),
        "remaining_account_equivalents": round(remaining, 3),
        "average_remaining_percent": round(remaining * 100 / len(known), 1) if known else 0.0,
    }


def _aggregate_label(value: dict[str, float | int]) -> str:
    known = int(value["known_accounts"])
    if not known:
        return "no provider data"
    return (
        f"{float(value['remaining_account_equivalents']):.2f}/{known} acct-eq  "
        f"({float(value['average_remaining_percent']):.0f}% avg)"
    )


def _window_graph(
    window: UsageWindow | None,
    *,
    width: int,
    show_reset: bool = False,
) -> Text:
    if window is None:
        return Text("not exposed", style="dim")
    remaining = int(round(window.remaining_percent))
    style = "green" if remaining >= 50 else "yellow" if remaining >= 20 else "red"
    graph = Text(progress_bar(remaining, 100, width=width), style=style)
    graph.append(f" {remaining:>3}%  ")
    graph.append(window.label, style="dim")
    if show_reset and window.resets_at is not None:
        graph.append(f"\n{_format_reset(window.resets_at)}", style="dim")
    return graph


def _reset_label(short: UsageWindow | None, weekly: UsageWindow | None) -> str:
    labels: list[str] = []
    for window in (short, weekly):
        if window and window.resets_at is not None:
            labels.append(f"{window.label} {_format_reset(window.resets_at)}")
    return "\n".join(labels) if labels else "-"


def _format_reset(value: str | int) -> str:
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, tz=timezone.utc).astimezone()
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
        now = datetime.now().astimezone()
        seconds = max(0, int((moment - now).total_seconds()))
        if seconds < 86400:
            hours, remainder = divmod(seconds, 3600)
            minutes = remainder // 60
            return f"in {hours}h {minutes:02d}m"
        return moment.strftime("%a %H:%M")
    except (OverflowError, TypeError, ValueError, OSError):
        return str(value)


def _money_value(value: Any) -> float | None:
    if value is None:
        return None
    match = _MONEY_RE.search(str(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _provider_label(provider: str) -> str:
    return "Claude" if provider == "claude-cli" else "Codex" if provider == "codex-cli" else provider


def _short_error(value: str, width: int) -> str:
    clean = " ".join(str(value).split())
    return clean if len(clean) <= width else clean[: max(1, width - 3)] + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
