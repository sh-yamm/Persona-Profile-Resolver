"""Optional anonymous-guest browser fallback (Patchright = Playwright with CDP leaks patched).

Evasion carried over from the earlier scrapers:
- no login, fresh guest profile; real Chrome channel when installed, headful by default
- `--disable-blink-features=AutomationControlled` (patchright also hides navigator.webdriver)
- human-ish pauses + a small scroll before reading
- close/remove the dismissible guest sign-in modal (content is already in the DOM)
- Windows: run Playwright in its own thread with a ProactorEventLoop
"""

import asyncio
import random
import sys
import threading

from . import config
from .net import log

DISMISS_SELECTORS = [
    "button[data-tracking-control-name*='modal_dismiss']",
    "button.contextual-sign-in-modal__modal-dismiss",
    "button.modal__dismiss",
    "button[aria-label='Dismiss']",
]
WALL_MARKERS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


class GuestBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless

    def get_html(self, url: str, limiter) -> str | None:
        out: dict = {}

        def run():
            if sys.platform == "win32":
                asyncio.set_event_loop(asyncio.ProactorEventLoop())
            try:
                out["html"] = self._fetch(url, limiter)
            except Exception as e:
                log(f"  [browser] {type(e).__name__}: {e}")

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join()
        return out.get("html")

    def _fetch(self, url: str, limiter) -> str | None:
        from patchright.sync_api import sync_playwright

        limiter.wait("linkedin")
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=self.headless,
                                            args=["--disable-blink-features=AutomationControlled"])
            except Exception:
                browser = p.chromium.launch(headless=self.headless,
                                            args=["--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(random.randint(2500, 4500))
                if any(m in page.url.lower() for m in WALL_MARKERS):
                    log(f"  [browser] hard authwall on {url}; not bypassing")
                    return None
                page.keyboard.press("Escape")
                for sel in DISMISS_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=1500)
                        break
                page.mouse.wheel(0, random.randint(250, 500))
                page.wait_for_timeout(random.randint(800, 1600))
                return page.content()
            finally:
                limiter.done("linkedin")
                browser.close()

    def close(self):
        pass
