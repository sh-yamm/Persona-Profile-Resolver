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
    """linkedin.com/redir/redirect?url=https%3A%2F%2Facme%2Eai -> https://acme.ai"""
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


def parse_post_html(src: str, url: str) -> dict | None:
    """Guest post page -> author facts. Post pages stay readable for guests even when
    profile pages are walled, and their JSON-LD names the author with profile URL,
    profile photo and follower count."""
    try:
        tree = lh.fromstring(src)
    except Exception:
        return None
    for block in tree.xpath("//script[@type='application/ld+json']/text()"):
        try:
            d = json.loads(block)
        except Exception:
            continue
        if d.get("@type") not in ("SocialMediaPosting", "DiscussionForumPosting", "Article"):
            continue
        a = d.get("author") or {}
        img = a.get("image")
        img = (img.get("url") or img.get("contentUrl")) if isinstance(img, dict) else (img or "")
        if not img:   # JSON-LD sometimes omits it; the author avatar <img> still has it
            for el in tree.xpath("//img[starts-with(@alt, 'View profile for')]"):
                u = el.get("data-delayed-url") or el.get("src") or ""
                if "profile-displayphoto" in u:
                    img = u
                    break
        stats = a.get("interactionStatistic") or {}
        return {"post_url": url, "author_name": _clean(a.get("name")),
                "author_url": a.get("url", ""), "author_image": img or "",
                "followers": stats.get("userInteractionCount") if isinstance(stats, dict) else None,
                "text": _clean(d.get("articleBody") or d.get("headline"))[:1500],
                "date": d.get("datePublished", "")}
    return None


def parse_company_html(src: str, url: str) -> dict | None:
    """Guest company page -> industry, size bucket, website, HQ, description."""
    try:
        tree = lh.fromstring(src)
    except Exception:
        return None
    out = {"url": url, "name": "", "industry": "", "size": "", "website": "", "hq": "",
           "description": "", "employees_on_li": None}
    for block in tree.xpath("//script[@type='application/ld+json']/text()"):
        try:
            d = json.loads(block)
        except Exception:
            continue
        for n in d.get("@graph", [d]) if isinstance(d, dict) else d:
            if n.get("@type") == "Organization":
                out["name"] = _clean(n.get("name"))
                out["description"] = _clean(n.get("description"))[:1500]
                out["website"] = n.get("sameAs") or ""
                emp = n.get("numberOfEmployees") or {}
                out["employees_on_li"] = emp.get("value") if isinstance(emp, dict) else None
    labels = {"industry": "industry", "size": "size", "website": "website", "headquarters": "hq"}
    for el in tree.xpath("//*[starts-with(@data-test-id, 'about-us__')]"):
        key = labels.get(el.get("data-test-id").split("__", 1)[1].lower())
        if not key:
            continue
        dd = el.xpath(".//dd")
        val = _clean(dd[0].text_content()) if dd else ""
        if key == "website":
            val = (el.xpath(".//a/@href") or [val])[0]
            val = _unredirect(val)
        if val:
            out[key] = out[key] or val
    if not out["name"]:
        out["name"] = _clean((tree.xpath("//h1") or [None])[0].text_content()) if tree.xpath("//h1") else ""
    return out if (out["name"] or out["industry"] or out["size"]) else None


class LinkedInClient:
    def __init__(self, fetcher, cache):
        self.fetcher = fetcher
        self.cache = cache
        self._browser = None
        self._browser_failed = False

    def _get_browser(self):
        if self._browser is None and not self._browser_failed and config.LINKEDIN_FETCHER == "browser":
            from .browser import try_start
            self._browser = try_start(config.BROWSER_HEADLESS)
            self._browser_failed = self._browser is None
        return self._browser

    def profile(self, url: str) -> Profile:
        hit = self.cache.get_json("profile", url)
        if hit is not None:
            return Profile.from_dict(hit)
        if config.OFFLINE:
            return Profile(url=url, error="offline")
        browser = self._get_browser()
        try:
            prof = self._via_browser(browser, url) if browser else self._via_http(url)
        except Blocked as e:
            log(f"  [linkedin] blocked on {url}: {e}")
            # not cached: a later round may succeed once the cool-down passes
            return Profile(url=url, error=f"blocked: {e}")
        if prof.ok or prof.error == "404":
            self.cache.set_json("profile", url, prof.to_dict())
        return prof

    def _via_http(self, url: str) -> Profile:
        r = self.fetcher.get(url, bucket="linkedin", cache_ns=None, linkedin=True)
        if r.status == 404:
            return Profile(url=url, error="404")
        prof = parse_profile_html(r.text, url)
        if not prof.ok:
            prof.error = f"unparsed HTTP {r.status}"
        return prof

    def _via_browser(self, browser, url: str) -> Profile:
        from .browser import BrowserBlocked
        lim = self.fetcher.limiter
        lim.wait("linkedin", sleep_through_cooldown=False)   # raises Blocked while cooling
        try:
            status, final, src = browser.fetch(url)
        except BrowserBlocked as e:
            cool = lim.strike("linkedin")
            log(f"  [net] LinkedIn block in browser ({e}) -> cool-down {cool/60:.1f} min")
            raise Blocked(str(e))
        except Exception as e:                                # timeout / navigation error
            raise Blocked(f"{type(e).__name__}: {e}")
        finally:
            lim.done("linkedin")
        lim.clear_strikes("linkedin")
        if status == 404:
            return Profile(url=url, error="404")
        prof = parse_profile_html(src, url)
        prof.source = "browser"
        if not prof.ok:
            prof.error = f"unparsed HTTP {status}"
        return prof

    def author_from_posts(self, profile_url: str, post_urls: list[str], max_posts: int = 2) -> Profile | None:
        """Build a (partial) Profile for profile_url from the author block of its posts."""
        from .search import canonical_profile
        prof = None
        for pu in post_urls[:max_posts]:
            data = self.cache.get_json("post", pu)
            if data is None:
                if config.OFFLINE:
                    continue
                try:
                    r = self.fetcher.get(pu, bucket="linkedin:posts", cache_ns=None, linkedin=True)
                except Blocked as e:
                    log(f"  [posts] blocked on {pu[:70]}: {e}")
                    break
                data = parse_post_html(r.text, pu) or {}
                self.cache.set_json("post", pu, data)
            if not data or canonical_profile(data.get("author_url", "")) != profile_url:
                continue                      # a repost / someone else's post
            if prof is None:
                prof = Profile(url=profile_url, ok=True, source="post", name=data["author_name"],
                               image_url=data["author_image"])
            if data.get("text"):
                prof.posts.append(data["text"])
            if not prof.image_url and data.get("author_image"):
                prof.image_url = data["author_image"]
        return prof

    def company(self, company_url: str) -> dict | None:
        """Company facts (cached). Plain HTTP: company pages are served to guests."""
        hit = self.cache.get_json("company", company_url)
        if hit is not None:
            return hit or None
        if config.OFFLINE:
            return None
        try:
            r = self.fetcher.get(company_url, bucket="linkedin:company", cache_ns=None, linkedin=True)
        except Blocked as e:
            log(f"  [company] blocked on {company_url}: {e}")
            return None
        info = parse_company_html(r.text, company_url) if r.status == 200 else None
        self.cache.set_json("company", company_url, info or {})
        return info

    def close(self):
        if self._browser is not None:
            self._browser.close()
