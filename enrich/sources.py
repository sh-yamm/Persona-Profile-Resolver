"""High-precision candidate sources: links the persona itself publishes.

A personal/company website or a Bluesky bio that links to linkedin.com/in/<x> is the
strongest evidence available. Team pages can list many people, so the number of
distinct profiles found on a page is recorded and used to discount it.
"""

import re

from . import textutil as tu
from .net import log
from .search import LI_PROFILE_RE, canonical_profile

BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={}"


def _links_in(text: str) -> list[str]:
    out = []
    for m in LI_PROFILE_RE.finditer(text or ""):
        u = canonical_profile(m.group(0))
        if u and u not in out:
            out.append(u)
    return out


def persona_links(persona, fetcher) -> tuple[list[dict], dict]:
    """Returns (candidates, extras). candidates: {url, source, page_profiles};
    extras: {'bsky_bio': str, 'site_text': {domain: text}} for extra matching signal."""
    found: list[dict] = []
    extras = {"bsky_bio": "", "site_text": {}}

    for url in persona.urls[:3]:
        try:
            r = fetcher.get(url, bucket="web", cache_ns="http")
        except Exception as e:
            log(f"  [sources] {url}: {e}")
            continue
        if r.status >= 400:
            continue
        links = _links_in(r.text)
        extras["site_text"][tu.registrable_domain(url)] = re.sub(r"<[^>]+>", " ", r.text)[:20000]
        for u in links:
            found.append({"url": u, "source": "website", "page_profiles": len(links), "page": url})
        log(f"  [sources] {url} -> {len(links)} linkedin profile link(s)")

    if persona.bluesky:
        try:
            r = fetcher.get(BSKY_API.format(persona.bluesky), bucket="web", cache_ns="http")
            if r.status == 200:
                import json
                d = json.loads(r.text)
                bio = " ".join(filter(None, [d.get("displayName"), d.get("description")]))
                extras["bsky_bio"] = bio
                for u in _links_in(bio):
                    found.append({"url": u, "source": "bluesky", "page_profiles": 1, "page": "bsky"})
                log(f"  [sources] bluesky bio: {bio[:80]!r}")
        except Exception as e:
            log(f"  [sources] bluesky: {e}")

    if persona.linkedin_hint:
        u = canonical_profile(persona.linkedin_hint)
        if u:
            found.append({"url": u, "source": "persona", "page_profiles": 1, "page": "persona"})
    return found, extras
