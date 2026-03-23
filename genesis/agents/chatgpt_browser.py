"""
ChatGPTBrowserAgent — drives ChatGPT via Playwright browser automation.

Requires: pip install playwright && playwright install chromium

Uses your existing ChatGPT Pro session (reads cookies from your browser)
so no API key is needed. Set [chatgpt_browser] persist_session = true in
config to avoid logging in every time.
"""
from __future__ import annotations
import json
import logging
import time
from genesis.agents.base import BaseAgent, AgentInfo

logger = logging.getLogger(__name__)

_CHATGPT_URL = "https://chatgpt.com"
_INPUT_SELECTOR = "div#prompt-textarea"
_RESPONSE_SELECTOR = "article[data-testid^='conversation-turn']"

# How long to wait for a response before timing out (seconds)
_RESPONSE_TIMEOUT = 120


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class ChatGPTBrowserAgent(BaseAgent):
    """
    Automates ChatGPT via a real browser session using Playwright.

    Requires `playwright` to be installed and `playwright install chromium`
    to have been run. Your ChatGPT login session is stored in a persistent
    browser profile at ~/.genesis/chatgpt_profile/ so you only need to log
    in once.

    Usage in config.toml:
        [chatgpt_browser]
        enabled = true
        headless = true
        model = "gpt-4o"   # displayed model name in ChatGPT UI (informational only)
    """

    def __init__(
        self,
        info: AgentInfo,
        headless: bool = True,
        profile_dir: str = "",
    ):
        super().__init__(info)
        self.headless = headless
        self.profile_dir = profile_dir
        self._pw = None
        self._browser = None
        self._page = None

    # ── Public ──────────────────────────────────────────────────────────────

    def chat(self, system: str, messages: list[dict]) -> str:
        if not _playwright_available():
            raise RuntimeError(
                "Playwright is not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        self._ensure_browser()
        prompt = self._build_prompt(system, messages)
        return self._send_and_wait(prompt)

    def ping(self) -> bool:
        if not _playwright_available():
            return False
        try:
            self._ensure_browser()
            return self._page.url.startswith(_CHATGPT_URL)
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = self._browser = self._pw = None

    # ── Internals ────────────────────────────────────────────────────────────

    def _ensure_browser(self) -> None:
        if self._page and not self._page.is_closed():
            return

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        browser_type = self._pw.chromium

        launch_kwargs: dict = {"headless": self.headless}
        if self.profile_dir:
            self._browser = browser_type.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
            )
            self._page = self._browser.pages[0] if self._browser.pages else self._browser.new_page()
        else:
            self._browser = browser_type.launch(**launch_kwargs)
            ctx = self._browser.new_context()
            self._page = ctx.new_page()

        # Navigate to ChatGPT
        if not self._page.url.startswith(_CHATGPT_URL):
            self._page.goto(_CHATGPT_URL, wait_until="networkidle", timeout=30_000)

        # Warn if login is needed
        if "login" in self._page.url or "auth" in self._page.url:
            logger.warning(
                "ChatGPT login required. Open a browser, log in, then re-run. "
                "Set profile_dir in config to persist your session."
            )

    def _build_prompt(self, system: str, messages: list[dict]) -> str:
        parts = [system]
        for msg in messages:
            content = msg.get("content", "")
            if content:
                parts.append(content)
        return "\n\n---\n\n".join(parts)

    def _send_and_wait(self, prompt: str) -> str:
        page = self._page

        # Start a new conversation
        page.goto(_CHATGPT_URL, wait_until="networkidle", timeout=30_000)
        page.wait_for_selector(_INPUT_SELECTOR, timeout=15_000)

        # Type the prompt
        page.click(_INPUT_SELECTOR)
        page.keyboard.insert_text(prompt)
        page.keyboard.press("Enter")

        # Wait for the response to complete (streaming stops)
        deadline = time.time() + _RESPONSE_TIMEOUT
        last_text = ""
        stable_count = 0

        while time.time() < deadline:
            time.sleep(1.5)
            articles = page.query_selector_all(_RESPONSE_SELECTOR)
            if not articles:
                continue
            last_article = articles[-1]
            text = last_article.inner_text().strip()

            if text == last_text and text:
                stable_count += 1
                if stable_count >= 3:
                    return text
            else:
                stable_count = 0
            last_text = text

        return last_text or ""
