"""Persona enrichment: squeeze every field the persona gives us *before* matching.

- Websites in the intro / social links: homepage + about/team pages (parent domain when a
  subdomain is dead). Extract direct linkedin.com/in links (strongest evidence), the
  site's own name (= company), linkedin.com/company links, and the sentences that mention
  the person (-> title / role / keywords / location).
- Bluesky: open public API -> display name + bio.
- Twitter/X handles: X can't be read without login, but search engines index the profile
  page with its bio, so the handle is searched and the bio snippet mined the same way.

Team pages can list many people, so the number of distinct profiles found on a page is
recorded and used to discount it.
"""

import json
import re
import urllib.parse as up

from lxml import html as lh

from . import textutil as tu
from .net import log
from .search import LI_PROFILE_RE, canonical_company, canonical_profile

BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={}"
SUBPAGE_HINTS = ("about", "team", "founder", "people", "who-we-are", "company", "story", "contact")
LI_COMPANY_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[\w%-]+", re.I)


def _links_in(text: str) -> list[str]:
    out = []
    for m in LI_PROFILE_RE.finditer(text or ""):
        u = canonical_profile(m.group(0))
        if u and u not in out:
            out.append(u)
    return out


def _visible_text(tree) -> str:
    for bad in tree.xpath("//script|//style|//noscript|//svg"):
        bad.getparent().remove(bad)
    return " ".join(tree.text_content().split())


def _site_name(tree, url: str) -> str:
    candidates = tree.xpath("//meta[@property='og:site_name']/@content") + \
        tree.xpath("//meta[@name='application-name']/@content") + tree.xpath("//title/text()")
    for raw in candidates:
        # "Acme-Labs | AI marketing", "Acme Life - A global ..." -> the name part only
        part = re.split(r"\s+[|\-–—:·]\s+", raw.strip())[0].strip() if raw.strip() else ""
        if 2 < len(part) <= 40:
            return part
    return tu.registrable_domain(url).split(".")[0]


def _mentions(text: str, persona) -> list[str]:
    """Sentences / windows that mention the person by full name or by last name."""
    keys = [persona.full_name] if persona.full_name.strip() else []
    if persona.last and len(persona.last) >= 4:
        keys.append(persona.last)
    out = []
    for k in keys:
        for m in re.finditer(re.escape(k), text, re.I):
            w = text[max(0, m.start() - 160): m.end() + 220]
            if w not in out:
                out.append(w)
        if out:
            break
    return out[:6]


def analyse_site(url: str, persona, fetcher) -> dict | None:
    """Homepage + up to 2 about/team pages. Falls back to the parent domain."""
    tried = [url]
    root = "https://" + tu.registrable_domain(url)
    if root.rstrip("/") != url.rstrip("/").split("?")[0]:
        tried.append(root)
    page = None
    for u in tried:
        try:
            r = fetcher.get(u, bucket="web", cache_ns="http")
            if r.status < 400 and r.content:
                page = (u, r)
                break
        except Exception as e:
            log(f"  [sources] {u}: {type(e).__name__}")
    if not page:
        return None
    base, r = page
    info = {"url": base, "site_name": "", "profiles": [], "companies": [], "mentions": [],
            "text": "", "pages": [base]}
    try:
        tree = lh.fromstring(r.content)
    except Exception:
        return None
    info["site_name"] = _site_name(tree, base)
    hrefs = [up.urljoin(base, h) for h in tree.xpath("//a/@href")]
    subpages = []
    for h in hrefs:
        if (tu.registrable_domain(h) == tu.registrable_domain(base) and h not in subpages
                and any(k in h.lower().split("?")[0].rsplit("/", 2)[-1] + "/" + h.lower() for k in SUBPAGE_HINTS)
                and not h.lower().endswith((".pdf", ".png", ".jpg"))):
            subpages.append(h.split("#")[0])
    texts, raw = [_visible_text(tree)], [r.text]
    for sp in list(dict.fromkeys(subpages))[:2]:
        try:
            rs = fetcher.get(sp, bucket="web", cache_ns="http")
            if rs.status < 400:
                raw.append(rs.text)
                texts.append(_visible_text(lh.fromstring(rs.content)))
                info["pages"].append(sp)
        except Exception:
            pass
    blob_raw = " ".join(raw)
    info["profiles"] = _links_in(blob_raw)
    info["companies"] = list(dict.fromkeys(c for c in (canonical_company(m.group(0))
                                                           for m in LI_COMPANY_RE.finditer(blob_raw)) if c))
    info["text"] = " ".join(texts)[:30000]
    info["mentions"] = _mentions(info["text"], persona)
    return info


def handle_bio(handle: str, searcher) -> str:
    """X/Twitter bio via search snippets of the indexed profile page."""
    bios = []
    for q in (f'"{handle}" twitter OR x.com', f"x.com/{handle}"):
        for r in searcher.search(q, max_results=8):
            h = r["href"].lower()
            if re.search(rf"(twitter|x)\.com/{re.escape(handle.lower())}(?:$|[/?])", h):
                bios.append(f"{r['title']} {r['body']}")
        if bios:
            break
    return " ".join(bios)[:2000]


def persona_links(persona, fetcher, searcher=None) -> tuple[list[dict], dict]:
    """Returns (candidates, extras). candidates: {url, source, page_profiles, page}.
    extras: bsky_bio, twitter_bio, site_text {domain: text}, site_names, li_companies,
    mentions (sentences about the person) - all used as extra matching evidence."""
    found: list[dict] = []
    extras = {"bsky_bio": "", "twitter_bio": "", "site_text": {}, "site_names": [],
              "li_companies": [], "mentions": []}

    seen_domains = set()
    for url in persona.urls[:3]:
        dom = tu.registrable_domain(url)
        if dom in seen_domains:
            continue
        seen_domains.add(dom)
        info = analyse_site(url, persona, fetcher)
        if not info:
            log(f"  [sources] {url}: unreachable (also tried parent domain)")
            continue
        extras["site_text"][dom] = info["text"]
        extras["site_names"].append(info["site_name"])
        extras["li_companies"] += info["companies"]
        extras["mentions"] += info["mentions"]
        for u in info["profiles"]:
            found.append({"url": u, "source": "website", "page_profiles": len(info["profiles"]),
                          "page": info["url"]})
        log(f"  [sources] {info['url']} ({len(info['pages'])} page(s)): site={info['site_name']!r} "
            f"profiles={len(info['profiles'])} company_pages={len(info['companies'])} "
            f"mentions={len(info['mentions'])}")

    if persona.bluesky:
        try:
            r = fetcher.get(BSKY_API.format(persona.bluesky), bucket="web", cache_ns="http")
            if r.status == 200:
                d = json.loads(r.text)
                bio = " ".join(filter(None, [d.get("displayName"), d.get("description")]))
                extras["bsky_bio"] = bio
                for u in _links_in(bio):
                    found.append({"url": u, "source": "bluesky", "page_profiles": 1, "page": "bsky"})
                log(f"  [sources] bluesky bio: {bio[:80]!r}")
        except Exception as e:
            log(f"  [sources] bluesky: {e}")

    if persona.twitter and searcher is not None:
        bio = handle_bio(persona.twitter, searcher)
        extras["twitter_bio"] = bio
        for u in _links_in(bio):
            found.append({"url": u, "source": "persona", "page_profiles": 1, "page": "twitter"})
        if bio:
            log(f"  [sources] x.com/{persona.twitter} bio: {bio[:90]!r}")

    if persona.github:
        gh = github_profile(persona.github, fetcher)
        extras["github_bio"] = gh
        for u in _links_in(gh):
            found.append({"url": u, "source": "persona", "page_profiles": 1, "page": "github"})
        if gh:
            log(f"  [sources] github/{persona.github}: {gh[:90]!r}")

    if persona.linkedin_hint:
        u = canonical_profile(persona.linkedin_hint)
        if u:
            found.append({"url": u, "source": "persona", "page_profiles": 1, "page": "persona"})
    return found, extras


GITHUB_API = "https://api.github.com/users/{}"


def github_profile(handle: str, fetcher) -> str:
    """Public GitHub API (no auth, 60 req/h): name, company, blog, location, bio, twitter."""
    try:
        r = fetcher.get(GITHUB_API.format(handle), bucket="web", cache_ns="http",
                        headers={"Accept": "application/vnd.github+json"})
        if r.status != 200:
            return ""
        d = json.loads(r.text)
    except Exception:
        return ""
    parts = [d.get("name"), d.get("company"), d.get("bio"), d.get("location"), d.get("blog"),
             f"twitter.com/{d['twitter_username']}" if d.get("twitter_username") else None]
    return " | ".join(p for p in parts if p)
