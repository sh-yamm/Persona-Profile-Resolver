"""Polite HTTP layer.

- curl_cffi with a real Chrome TLS/HTTP2 fingerprint (no headless-browser tells).
- One persistent anonymous session (guest cookies only — never a LinkedIn login).
- Per-bucket jittered delays, periodic long breaks, exponential cool-down on blocks.
"""

import random
import time
from dataclasses import dataclass

from curl_cffi import requests as cffi

from . import config

LINKEDIN_WALL_MARKERS = ("/authwall", "/login", "/uas/login", "/checkpoint", "/signup")


class Blocked(Exception):
    """The host is throttling/walling us; caller should back off or degrade."""


@dataclass
class Response:
    status: int
    url: str
    content: bytes
    headers: dict

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class RateLimiter:
    def __init__(self, cache=None):
        self.cache = cache          # cool-downs survive restarts
        self._last: dict[str, float] = {}
        self._count: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    def _family(self, bucket: str) -> str:
        return bucket.split(":", 1)[0]

    def wait(self, bucket: str) -> None:
        fam = self._family(bucket)
        until = self._blocked_until.get(bucket, 0)
        if self.cache is not None:
            until = max(until, float(self.cache.get_json("cooldown", bucket) or 0))
        if until > time.time():
            secs = until - time.time()
            log(f"  [rate] {bucket} cooling down {secs/60:.1f} min")
            time.sleep(secs)
        lo, hi = config.BUCKET_DELAYS.get(fam, (1.0, 3.0))
        gap = random.uniform(lo, hi) * config.DELAY_SCALE
        n = self._count.get(bucket, 0)
        brk = config.BUCKET_BREAKS.get(fam)
        if brk and n and n % brk[0] == 0:
            extra = random.uniform(*brk[1]) * config.DELAY_SCALE
            log(f"  [rate] {bucket}: break after {n} requests, {extra:.0f}s")
            gap = max(gap, extra)
        last = self._last.get(bucket)
        if last is not None:
            remaining = gap - (time.time() - last)
            if remaining > 0:
                time.sleep(remaining)

    def done(self, bucket: str) -> None:
        self._last[bucket] = time.time()
        self._count[bucket] = self._count.get(bucket, 0) + 1

    def strike(self, bucket: str) -> float:
        s = self._strikes.get(bucket, 0)
        cool = min(config.BACKOFF_BASE * (2 ** s), config.BACKOFF_MAX) * max(config.DELAY_SCALE, 0.05)
        self._strikes[bucket] = s + 1
        self._blocked_until[bucket] = time.time() + cool
        if self.cache is not None:
            self.cache.set_json("cooldown", bucket, self._blocked_until[bucket])
        return cool

    def clear_strikes(self, bucket: str) -> None:
        self._strikes.pop(bucket, None)


class Fetcher:
    def __init__(self, cache):
        self.cache = cache
        self.limiter = RateLimiter(cache)
        self._session = cffi.Session(impersonate=config.IMPERSONATE)
        self._session.headers.update({"Accept-Language": config.ACCEPT_LANGUAGE})

    def get(self, url: str, bucket: str = "web", cache_ns: str | None = "http",
            linkedin: bool = False, **kw) -> Response:
        """GET with caching + rate limiting. For LinkedIn, raises Blocked on 999/429/authwall."""
        if cache_ns:
            hit = self.cache.get_json(cache_ns, url)
            if hit is not None:
                return Response(hit["status"], hit["url"], hit["content"].encode("latin-1"), hit["headers"])

        attempts = (config.LINKEDIN_MAX_BLOCK_RETRIES + 1) if linkedin else 2
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self.limiter.wait(bucket)
            try:
                r = self._session.get(url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True, **kw)
            except Exception as e:  # network error: short retry
                self.limiter.done(bucket)
                last_exc = e
                log(f"  [net] {type(e).__name__} on {url[:80]}")
                continue
            self.limiter.done(bucket)
            resp = Response(r.status_code, str(r.url), r.content, dict(r.headers))

            if linkedin and self._is_blocked(resp):
                cool = self.limiter.strike(bucket)
                log(f"  [net] LinkedIn block (HTTP {resp.status}) on {url[:70]} -> cool-down {cool/60:.1f} min")
                last_exc = Blocked(f"HTTP {resp.status} {resp.url}")
                continue
            if resp.status == 429:
                cool = self.limiter.strike(bucket)
                last_exc = Blocked(f"429 on {url}")
                continue

            self.limiter.clear_strikes(bucket)
            if cache_ns and resp.status < 500:
                self.cache.set_json(cache_ns, url, {
                    "status": resp.status, "url": resp.url,
                    "content": resp.content.decode("latin-1"),
                    "headers": {k: v for k, v in resp.headers.items() if k.lower() == "content-type"},
                })
            return resp
        raise last_exc if isinstance(last_exc, Blocked) else Blocked(str(last_exc))

    @staticmethod
    def _is_blocked(resp: Response) -> bool:
        if resp.status in (999, 429, 403):
            return True
        u = resp.url.lower()
        if any(m in u for m in LINKEDIN_WALL_MARKERS):
            return True
        # JS-redirect authwall stub (tiny page that sends the browser to /authwall)
        return len(resp.content) < 5000 and b"/authwall" in resp.content
