"""
Account availability tracking for failover.

When an account's CLI reports a rate/usage/quota limit, Genesis marks that
account exhausted for a cooldown window and routes work to another account.
Exhaustion is inferred from the CLI's error text (the only signal available for
OAuth-based `claude`/`codex` sessions), so the matcher is deliberately broad but
avoids transient-overload signals (e.g. 529 "overloaded"), which are retries,
not failovers.
"""
from __future__ import annotations

import threading
import time

# Substrings (lower-cased) that indicate the account is out of capacity for now.
_EXHAUSTION_MARKERS = (
    "rate limit",
    "rate-limit",
    "rate_limit_exceeded",
    "too many requests",
    "429",
    "usage limit",
    "reached your limit",
    "reached your usage",
    "you've reached",
    "you have reached",
    "quota",
    "insufficient_quota",
    "exceeded your current quota",
    "out of credits",
    "credit balance is too low",
    "upgrade to a higher plan",
    "limit reached",
)

# Signals that are transient overload, NOT exhaustion — must not trigger failover.
_TRANSIENT_MARKERS = (
    "overloaded",
    "529",
    "503",
    "temporarily unavailable",
)


def is_exhaustion_error(text: str | None) -> bool:
    """True if the error text indicates the account is rate/usage/quota limited."""
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in _TRANSIENT_MARKERS) and not any(
        m in low for m in ("rate limit", "usage limit", "quota", "429")
    ):
        return False
    return any(m in low for m in _EXHAUSTION_MARKERS)


class AccountRegistry:
    """Thread-safe record of which account names are currently exhausted.

    An account marked exhausted becomes available again once its cooldown
    elapses, so a temporarily rate-limited account is reused rather than lost.
    """

    def __init__(self, cooldown_seconds: int = 900) -> None:
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}     # name -> epoch when it's available again
        self.cooldown_seconds = max(1, cooldown_seconds)

    def mark_exhausted(self, name: str, cooldown_seconds: int | None = None) -> None:
        if not name:
            return
        cooldown = self.cooldown_seconds if cooldown_seconds is None else max(1, cooldown_seconds)
        with self._lock:
            self._until[name] = time.time() + cooldown

    def is_available(self, name: str) -> bool:
        with self._lock:
            until = self._until.get(name)
            if until is None:
                return True
            if time.time() >= until:
                del self._until[name]      # cooldown elapsed — clear it
                return True
            return False

    def exhausted_names(self) -> set[str]:
        now = time.time()
        with self._lock:
            return {n for n, until in self._until.items() if now < until}

    def clear(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._until.clear()
            else:
                self._until.pop(name, None)
