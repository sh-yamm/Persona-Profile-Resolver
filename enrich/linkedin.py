"""LinkedIn guest-page fetching and parsing (no login, ever).

Primary path: plain HTTP via the polite Fetcher (Chrome TLS fingerprint).
Optional fallback: a Patchright (stealth Playwright) browser as an anonymous guest,
reusing the overlay-dismissal tricks of the original post scraper.
Only what LinkedIn serves to logged-out visitors is read; hard authwalls are not bypassed.
"""

import html as htmllib
import json
import re
import urllib.parse as up
from dataclasses import asdict, dataclass, field

from lxml import html as lh

from . import config
from . import textutil as tu
from .net import Blocked, log

MASK_RE = re.compile(r"^[*\s·•.,-]*$")


@dataclass
class Profile:
    url: str
    ok: bool = False
    source: str = ""                 # guest_http | browser | snippet
    name: str = ""
    headline: str = ""
    location: str = ""
    country: str | None = None
    companies: list[str] = field(default_factory=list)       # current first
    company_urls: list[str] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)        # external links on the profile
    education: list[str] = field(default_factory=list)
    about: str = ""
    posts: list[str] = field(default_factory=list)
    awards: list[str] = field(default_factory=list)
    image_url: str = ""
    connections: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def text_blob(self) -> str:
        return " ".join([self.name, self.headline, self.location, " ".join(self.companies),
                         " ".join(self.education), self.about, " ".join(self.posts[:5]),
                         " ".join(self.awards), " ".join(self.websites)])


def _clean(s) -> str:
    s = " ".join(htmllib.unescape(str(s or "")).split())
    return "" if MASK_RE.match(s) else s


def _unredirect(href: str) -> str:
    """linkedin.com/redir/redirect?url=https%3A%2F%2Finventive%2Eai -> https://inventive.ai"""
    if "/redir/redirect" in href:
        q = up.parse_qs(up.urlparse(href).query).get("url", [""])[0]
        return up.unquote(q.replace("%2E", "."))
    return href


def parse_profile_html(src: str, url: str) -> Profile:
    p = Profile(url=url, source="guest_http")
    try:
        tree = lh.fromstring(src)
    except Exception as e:
        p.error = f"parse: {e}"
        return p

    # ---- JSON-LD (most stable, SEO-facing) --------------------------------
    for block in tree.xpath("//script[@type='application/ld+json']/text()"):
        try:
            data = json.loads(block)
        except Exception:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in nodes:
            t = n.get("@type")
            if t == "Person" and not p.name:
                p.name = _clean(n.get("name"))
                addr = n.get("address") or {}
                if isinstance(addr, dict):
                    p.location = _clean(addr.get("addressLocality"))
                    p.country = (addr.get("addressCountry") or "").upper() or None
                for org in n.get("worksFor") or []:
                    nm = _clean(org.get("name"))
                    if nm and nm not in p.companies:
                        p.companies.append(nm)
                        if org.get("url"):
                            p.company_urls.append(org["url"])
                for org in n.get("alumniOf") or []:
                    nm = _clean(org.get("name"))
                    if nm and nm not in p.education and org.get("@type") in ("EducationalOrganization", "CollegeOrUniversity"):
                        p.education.append(nm)
                for jt in n.get("jobTitle") or []:
                    jt = _clean(jt)
                    if jt and not p.headline:
                        p.headline = jt
                p.about = _clean(n.get("description"))
                p.awards = [_clean(a) for a in (n.get("awards") or []) if _clean(a)]
                img = n.get("image")
                if isinstance(img, dict):
                    p.image_url = img.get("contentUrl") or img.get("url") or ""
                elif isinstance(img, str):
                    p.image_url = img
            elif t in ("DiscussionForumPosting", "Article", "SocialMediaPosting"):
                txt = _clean(n.get("text") or n.get("articleBody") or n.get("headline"))
                author = (n.get("author") or {}).get("url", "") if isinstance(n.get("author"), dict) else ""
                if txt and (not author or url.rstrip("/").split("/")[-1] in author):
                    p.posts.append(txt[:600])

    # ---- meta tags ---------------------------------------------------------
    def meta(attr, key):
        v = tree.xpath(f"//meta[@{attr}='{key}']/@content")
        return _clean(v[0]) if v else ""

    og_title = meta("property", "og:title")
    desc = meta("name", "description") or meta("property", "og:description")
    if not p.name and og_title:
        p.name = re.split(r"\s+[-–|]\s+", og_title)[0]
    if og_title:
        parts = [x for x in re.split(r"\s+[-–]\s+", re.sub(r"\s*\|\s*LinkedIn$", "", og_title)) if x]
        if len(parts) >= 2 and not p.companies:
            p.companies.append(parts[-1])
    if desc:
        for label, attr in (("Experience", "companies"), ("Education", "education"), ("Location", "location")):
            m = re.search(rf"{label}:\s*([^·•]+?)\s*(?:[·•]|$)", desc)
            if m:
                val = m.group(1).strip()
                if attr == "location":
                    p.location = p.location or val
                elif val not in getattr(p, attr):
                    getattr(p, attr).append(val)
        m = re.search(r"(\d+\+?)\s+connections", desc)
        if m:
            p.connections = m.group(1)
        # when a guest can see the About section, the description starts with it
        if not desc.startswith("Experience:") and len(desc) > 60 and not p.about:
            p.about = re.sub(r"\s*Experience:.*$", "", desc)
    if not p.image_url:
        p.image_url = meta("property", "og:image")

    # ---- top-card HTML -----------------------------------------------------
    h1 = tree.xpath("//h1")
    if h1 and not p.name:
        p.name = _clean(h1[0].text_content())
    for a in tree.xpath("//*[@data-section='websites']//a/@href"):
        w = _unredirect(a)
        if w and "linkedin.com" not in w and w not in p.websites:
            p.websites.append(w)
    for a in tree.xpath("//*[@data-section='currentPositionsDetails']//a/@href"):
        c = a.split("?")[0]
        if "/company/" in c and c not in p.company_urls:
            p.company_urls.append(c)
    sub = tree.xpath("//*[contains(@class,'top-card-layout__headline')]")
    if sub and not p.headline:
        p.headline = _clean(sub[0].text_content())

    if p.location and not p.country:
        p.country = tu.country_from_text(p.location)
    if p.image_url and ("ghost" in p.image_url or "static.licdn.com" in p.image_url):
        p.image_url = ""      # default silhouette, not a real photo
    p.ok = bool(p.name)
    return p


class LinkedInClient:
    def __init__(self, fetcher, cache):
        self.fetcher = fetcher
        self.cache = cache
        self._browser = None

    def profile(self, url: str) -> Profile:
        hit = self.cache.get_json("profile", url)
        if hit is not None:
            return Profile.from_dict(hit)
        prof = None
        try:
            r = self.fetcher.get(url, bucket="linkedin", cache_ns=None, linkedin=True)
            if r.status == 404:
                prof = Profile(url=url, error="404")
            else:
                prof = parse_profile_html(r.text, url)
                if not prof.ok:
                    prof.error = f"unparsed HTTP {r.status}"
        except Blocked as e:
            log(f"  [linkedin] blocked on {url}: {e}")
            if config.BROWSER_FALLBACK:
                prof = self._browser_profile(url)
            if prof is None:
                # not cached: a later run may succeed once the cool-down passes
                return Profile(url=url, error=f"blocked: {e}")
        if prof.ok or prof.error == "404":
            self.cache.set_json("profile", url, prof.to_dict())
        return prof

    # ---------------------------------------------------------------- browser
    def _browser_profile(self, url: str) -> Profile | None:
        try:
            from .browser import GuestBrowser
        except Exception as e:  # patchright not installed
            log(f"  [linkedin] browser fallback unavailable: {e}")
            return None
        if self._browser is None:
            self._browser = GuestBrowser()
        src = self._browser.get_html(url, self.fetcher.limiter)
        if not src:
            return None
        prof = parse_profile_html(src, url)
        prof.source = "browser"
        return prof if prof.ok else None

    def close(self):
        if self._browser is not None:
            self._browser.close()
