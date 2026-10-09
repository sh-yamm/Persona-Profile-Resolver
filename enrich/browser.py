"""Anonymous real-Chrome fetcher for LinkedIn guest pages (Patchright = Playwright with
the CDP/automation leaks patched).

Why a browser: LinkedIn answers non-browser clients with HTTP 999 based on fingerprint
(TLS/HTTP2/JS signals) and IP reputation. In our runs curl_cffi stayed blocked for hours
while this browser loaded the same profile on the first try.

- No login, ever: a fresh, throw-away guest profile; no cookies from any account.
- One browser + one tab reused for the whole run (like a person browsing), owned by a
  dedicated worker thread because Playwright objects are bound to their thread
  (also sidesteps the Windows asyncio subprocess issue).
- Real Chrome channel when installed, headful by default, AutomationControlled disabled.
- Human-ish behaviour: short settle time, Escape / close the dismissible sign-in modal,
  a small scroll. Content behind that modal is already in the DOM.
- Hard authwall / 999 is reported as a block - never bypassed.
"""

import asyncio
import queue
import random
import sys
import threading
from concurrent.futures import Future

from .net import log

DISMISS_SELECTORS = [
    "button[data-tracking-control-name*='modal_dismiss']",
    "button.contextual-sign-in-modal__modal-dismiss",
    "button.modal__dismiss",
    "button[aria-label='Dismiss']",
]
WALL_MARKERS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


class BrowserBlocked(Exception):
    pass


class GuestBrowser:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._started = threading.Event()
        self._start_error: Exception | None = None
        self._thread.start()
        self._started.wait(90)
        if self._start_error:
            raise self._start_error

    # ------------------------------------------------------------------ worker
    def _worker(self):
        if sys.platform == "win32":
            asyncio.set_event_loop(asyncio.ProactorEventLoop())
        try:
            from patchright.sync_api import sync_playwright
            pw = sync_playwright().start()
            args = ["--disable-blink-features=AutomationControlled"]
            try:
                browser = pw.chromium.launch(channel="chrome", headless=self.headless, args=args)
            except Exception:
                browser = pw.chromium.launch(headless=self.headless, args=args)
            ctx = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
        except Exception as e:
            self._start_error = e
            self._started.set()
            return
        self._started.set()

        while True:
            job = self._jobs.get()
            if job is None:
                break
            url, fut = job
            try:
                fut.set_result(self._load(page, url))
            except Exception as e:
                fut.set_exception(e)
        try:
            browser.close()
            pw.stop()
        except Exception:
            pass

    @staticmethod
    def _load(page, url: str) -> tuple[int, str, str]:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        status = resp.status if resp else 0
        page.wait_for_timeout(random.randint(1200, 2500))
        final = page.url
        if status in (999, 429) or any(m in final.lower() for m in WALL_MARKERS):
            raise BrowserBlocked(f"HTTP {status} {final}")
        try:
            page.keyboard.press("Escape")
            for sel in DISMISS_SELECTORS:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500)
                    break
            page.mouse.wheel(0, random.randint(250, 600))
            page.wait_for_timeout(random.randint(400, 900))
        except Exception:
            pass
        return status, final, page.content()

    # -------------------------------------------------------------------- API
    def fetch(self, url: str, timeout: float = 90) -> tuple[int, str, str]:
        """Returns (status, final_url, html). Raises BrowserBlocked on 999/authwall."""
        fut: Future = Future()
        self._jobs.put((url, fut))
        return fut.result(timeout=timeout)

    def close(self):
        self._jobs.put(None)
        self._thread.join(timeout=20)


def try_start(headless: bool):
    try:
        return GuestBrowser(headless=headless)
    except Exception as e:
        log(f"  [browser] unavailable ({type(e).__name__}: {e}); falling back to plain HTTP")
        return None
