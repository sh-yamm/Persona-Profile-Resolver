"""Keyless web search via `ddgs`, with backend rotation, caching and rate limiting."""

import re
import urllib.parse as up

from ddgs import DDGS

from . import config
from .net import log

LI_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([^/?#\s]+)", re.I)
LI_COMPANY_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/([^/?#\s]+)", re.I)


def canonical_profile(url: str) -> str | None:
    m = LI_PROFILE_RE.search(url or "")
    if not m:
        return None
    slug = up.unquote(m.group(1)).strip().lower()
    # real vanity slugs: letters (any script), digits, '-' and '_' only, 3..100 chars.
    # Scraped HTML often yields junk like 'carrie-chan-)43:t751,hello'; fetching those
    # wastes a LinkedIn request and tends to trigger HTTP 999.
    if not slug or slug in {"me", "edit"} or not re.fullmatch(r"[\w-]{3,100}", slug):
        return None
    return f"https://www.linkedin.com/in/{up.quote(slug, safe='-_.~')}"


LI_POST_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/posts/([\w%-]+?)_", re.I)


def profile_from_post(url: str) -> str | None:
    """/posts/damionwaltermeyer_hey-everyone-... -> /in/damionwaltermeyer (the author)."""
    m = LI_POST_RE.search(url or "")
    return canonical_profile(f"https://www.linkedin.com/in/{m.group(1)}") if m else None


def canonical_company(url: str) -> str | None:
    m = LI_COMPANY_RE.search(url or "")
    return f"https://www.linkedin.com/company/{m.group(1).lower()}" if m else None


def slug_of(url: str) -> str:
    return up.unquote(url.rstrip("/").split("/")[-1])


class Searcher:
    def __init__(self, cache, limiter):
        self.cache = cache
        self.limiter = limiter
        self._rot = 0
        self._dead: dict[str, int] = {}   # backend -> consecutive failures

    def _backends(self):
        order = config.SEARCH_BACKENDS[self._rot:] + config.SEARCH_BACKENDS[:self._rot]
        self._rot = (self._rot + 1) % 2          # alternate between the two best backends
        return sorted(order, key=lambda b: self._dead.get(b, 0) >= 3)

    def search(self, query: str, max_results: int = config.SEARCH_RESULTS) -> list[dict]:
        hit = self.cache.get_json("search", query)
        if hit is not None:
            return hit
        if config.OFFLINE:
            return []
        results: list[dict] = []
        for backend in self._backends():
            if self._dead.get(backend, 0) >= 6:
                continue
            bucket = f"search:{backend}"
            self.limiter.wait(bucket)
            try:
                raw = DDGS(timeout=12).text(query, max_results=max_results, backend=backend) or []
            except Exception as e:  # DDGSException("No results found"), ratelimit, timeout
                raw = []
                if "No results" not in str(e):
                    log(f"  [search] {backend}: {type(e).__name__}: {str(e)[:80]}")
            finally:
                self.limiter.done(bucket)
            if raw:
                self._dead[backend] = 0
                results = [{"href": r.get("href", ""), "title": r.get("title", ""),
                            "body": r.get("body", ""), "backend": backend} for r in raw]
                break
            self._dead[backend] = self._dead.get(backend, 0) + 1
        log(f"  [search] {len(results):2d} results  <- {query}")
        self.cache.set_json("search", query, results)
        return results


# --------------------------------------------------------------------------- #
# Snippet parsing: LinkedIn search results carry a lot of the profile already.
#   title: "Gaurav Nemade - Co-Founder @ Inventive.ai | LinkedIn"
#          "Jane Doe – Head of Sales – Acme | LinkedIn"
#   body:  "Experience: Acme · Education: MIT · Location: Boston · 500+ connections ..."
# --------------------------------------------------------------------------- #

def parse_snippet(title: str, body: str) -> dict:
    t = re.sub(r"\s*[|\-–]\s*LinkedIn.*$", "", title or "", flags=re.I).strip()
    t = t.split("...")[0].split("…")[0].strip()     # bing sometimes splices the next result after "..."
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    # bing splices neighbouring results into one title/body ("A - X ...B - Y | LinkedIn ...");
    # keep only this result's own part as evidence
    b = body or ""
    m = re.search(r"profile on LinkedIn[^.]*?\.\.\.", b)
    if m:
        b = b[:m.end()]
    out = {"name": parts[0] if parts else "", "headline": "", "company": "",
           "location": "", "education": "", "text": f"{t} {b}"}
    if len(parts) >= 3:
        out["headline"], out["company"] = parts[1], parts[-1]
    elif len(parts) == 2:
        out["headline"] = parts[1]
    for key, label in (("company", "Experience"), ("education", "Education"), ("location", "Location")):
        m = re.search(rf"{label}:\s*([^·•|]+?)(?:\s*[·•|]|\s+\d+\+? connections|$)", b)
        if m:
            out[key] = out[key] or m.group(1).strip()
    if not out["company"]:
        m = re.search(r"(?:@|\bat)\s+([A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*){0,3})", out["headline"])
        if m:
            out["company"] = m.group(1)
    # newer layout: "<Name> <Headline> Brooklyn, New York, United States 625 followers
    #               500+ connections See your mutual connections <Current company>"
    if not out["location"]:
        m = re.search(r"([A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*(?:, [A-Z][\w .'-]+){1,2})\s+[\d,.]+K?\+?\s+followers", b)
        if m:
            out["location"] = m.group(1).strip()
    if not out["company"]:
        m = re.search(r"mutual connections\s+([^·•|]{2,60}?)(?:\s+(?:Education|Experience|Location|\d)|[·•|]|\.\.\.|$)", b)
        if m:
            out["company"] = m.group(1).strip()
    if not out["location"]:
        m = re.search(r"(?:^|[·•]\s*)([A-Z][\w .'-]+,\s*[A-Z][\w .'-]+(?:,\s*[A-Z][\w .'-]+)?)\s*[·•]", b)
        if m:
            out["location"] = m.group(1).strip()
    return out
