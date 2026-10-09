"""Normalise a raw persona dict into structured, searchable facts."""

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import textutil as tu

URL_RE = re.compile(r"https?://[^\s)\],;]+", re.I)
BARE_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9][a-z0-9-]*\.)+(?:com|ai|io|co|uk|life|net|org|dev|app|xyz|tech|so|me|"
    r"community|club|studio|agency|digital|cloud|world|us|in|de|fr|eu))\b", re.I)
SOCIAL_HOSTS = ("twitter.com", "x.com", "bsky.app", "github.com", "linkedin.com",
                "instagram.com", "facebook.com", "youtube.com", "medium.com", "substack.com")


@dataclass
class Persona:
    raw: dict
    pid: str
    display_name: str
    first: str = ""
    last: str = ""
    last_initial: str = ""
    name_complete: bool = False
    image_url: str | None = None
    intro: str = ""
    companies: list[str] = field(default_factory=list)       # strong: @X, at X, URL, domain, name hint
    weak_companies: list[str] = field(default_factory=list)  # capitalised fragments, past employers
    titles: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    rare_terms: list[str] = field(default_factory=list)  # distinctive intro words, set by pipeline
    li_companies: list[str] = field(default_factory=list)  # linkedin.com/company pages the persona's site links
    country: str | None = None          # strong: stated in intro
    tz_country: str | None = None       # prior from timezone
    tz_region: str | None = None
    tz_weak: bool = True
    city_hint: str | None = None
    industry: str | None = None
    size: tuple[int, int] | None = None
    socials: list[str] = field(default_factory=list)
    twitter: str | None = None
    bluesky: str | None = None
    github: str | None = None
    linkedin_hint: str | None = None    # persona already contains a linkedin url

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p)

    @property
    def kind(self) -> str:
        return "type1" if self.name_complete else "type2"

    def summary(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k not in ("raw",) and v not in (None, [], "")}


def _pid(raw: dict) -> str:
    return hashlib.sha1(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:10]


def parse_name(name: str) -> dict:
    out = {"first": "", "last": "", "last_initial": "", "complete": False, "hint": None}
    name = (name or "").strip()
    m = re.search(r"\(([^)]*)\)", name)
    if m:
        out["hint"] = m.group(1).strip()
        name = (name[:m.start()] + name[m.end():]).strip()
    name = re.sub(r"\s*[|•·,-]\s*.*$", "", name) if re.search(r"\s[|•·]\s", name) else name
    name = re.sub(r"[^\w\s.'-]", " ", name, flags=re.U).strip()
    parts = [p for p in name.split() if p]
    if len(parts) == 1:
        p = parts[0]
        m = re.match(r"^([A-Z][a-z]+)([A-Z])\.?$", p)          # DamionW
        m2 = re.match(r"^([A-Z][a-z]+)([A-Z][a-z]{2,})$", p)    # JohnSmith
        if m:
            out["first"], out["last_initial"] = m.group(1), m.group(2)
        elif m2:
            out["first"], out["last"], out["complete"] = m2.group(1), m2.group(2), True
        else:
            out["first"] = p.capitalize() if p.islower() else p
        return out
    if not parts:
        return out
    out["first"] = parts[0]
    last = parts[-1]
    if re.fullmatch(r"[A-Za-z]\.?", last):
        out["last_initial"] = last[0].upper()
        if len(parts) > 2:
            out["first"] = " ".join(parts[:-1])
    else:
        out["last"] = " ".join(parts[1:]) if len(parts) <= 3 else parts[-1]
        out["complete"] = True
    return out


def _split_segments(intro: str) -> list[str]:
    text = URL_RE.sub(" ", intro)
    text = re.sub(r"\(\s*\)", " ", text)
    segs = re.split(r"\s*(?:[|/+;•·]|\.\s|,\s(?=[A-Z0-9])| - | – )\s*", text)
    return [s.strip(" .,-()") for s in segs if s and s.strip(" .,-()")]


def _is_title(seg: str) -> bool:
    toks = set(tu.norm(seg).replace("co founder", "co-founder").split())
    return bool(toks & tu.TITLE_WORDS)


def parse_intro(intro: str) -> dict:
    res = {"companies": [], "weak_companies": [], "titles": [], "urls": [], "domains": [], "country": None,
           "city": None, "keywords": []}
    if not intro:
        return res

    urls = [u.rstrip(".,)") for u in URL_RE.findall(intro)]
    bare = [d for d in BARE_DOMAIN_RE.findall(URL_RE.sub(" ", intro))]
    for u in urls + bare:
        host = tu.registrable_domain(u)
        if any(s in host for s in SOCIAL_HOSTS):
            continue
        res["urls"].append(u if u.startswith("http") else "https://" + u)
        if host not in res["domains"]:
            res["domains"].append(host)

    # drop list-style asides that are not about employment
    intro = re.sub(r"(?i)\b(?:tools|looking for|interests|skills|hobbies)\s*:.*?(?:\.\s|$)", " ", intro)
    companies, weak, titles = [], [], []
    # "X @ Company", "X at Company", "Founder of Company", "Company (https://..)"
    for m in re.finditer(r"@\s*([A-Za-z0-9][\w.&'-]*(?:\s+[A-Z0-9][\w.&'-]*){0,3})", intro):
        companies.append(m.group(1))
    for m in re.finditer(r"\bat\s+([A-Z0-9][\w.&'-]*(?:\s+[A-Z0-9][\w.&'-]*){0,3})", intro):
        companies.append(m.group(1))
    for m in re.finditer(r"\b(?:founder|ceo|owner|cto|head|lead)\s+(?:of|@)\s+([A-Za-z0-9][\w.&'-]*(?:\s+[A-Z0-9][\w.&'-]*){0,3})",
                         intro, re.I):
        companies.append(m.group(1))
    for m in re.finditer(r"([A-Z][\w&' -]{1,40}?)\s*\(\s*https?://", intro):
        companies.append(m.group(1))

    city_m = re.search(r"([A-Z][a-z]+(?: [A-Z][a-z]+)?),\s*([A-Z]{2})\b", intro)
    if city_m and city_m.group(2).lower() in tu.US_STATES:
        res["city"], res["country"] = city_m.group(1), "US"

    for seg in _split_segments(intro):
        if re.search(r"\b(in|from|based in)\s+(US|USA|UK|[A-Z][a-z]+)$", seg):
            loc = re.search(r"\b(?:in|from|based in)\s+(.+)$", seg).group(1)
            res["country"] = res["country"] or tu.country_from_text(loc)
            seg = re.sub(r"\s+\b(?:in|from|based in)\s+.+$", "", seg)
        if _is_title(seg):
            t = re.split(r"\s+(?:@|at)\s+", seg)[0].strip()
            if t and len(t) < 80:
                titles.append(t)
        elif (len(seg.split()) <= 4 and seg[:1].isupper() and len(seg) > 2
              and not tu.country_from_text(seg) and seg != res["city"]
              and not re.search(r"(?i)\b(years?|experience|looking)\b", seg)):
            weak.append(seg)

    m = re.search(r"(?i)\b(?:based in|located in|living in)\s+([A-Z][\w ,]+)", intro)
    if m and not res["country"]:
        res["country"] = tu.country_from_text(m.group(1))

    for d in res["domains"]:
        label = d.split(".")[0]
        companies.append(label)

    seen = set()

    def dedupe(items):
        out = []
        for c in items:
            c = re.sub(r"\s+(?:and|with|for|in)$", "", c.strip(" .,-"))
            k = tu.norm_company(c)
            if c and k and k not in seen and not _is_title(c) and len(k) > 1:
                seen.add(k)
                out.append(c)
        return out

    res["companies"] = dedupe(companies)
    res["weak_companies"] = dedupe(weak)
    res["titles"] = list(dict.fromkeys(titles))
    res["keywords"] = list(dict.fromkeys(tu.tokens(URL_RE.sub(" ", intro))))[:25]
    return res


def clean_social_bio(text: str, own_handles: list[str]) -> str:
    """Search-snippet bios carry page boilerplate ('Eric Doty (@DotyContent) / X', 'on X:',
    'Posts / X'). Strip it, drop the persona's own handle, and turn company-style handles
    ('@dock_us', '@butter_hq') into plain names so they parse as companies."""
    if not text:
        return ""
    # search engines that couldn't crawl the page show a placeholder instead of the bio
    text = re.sub(r"(?i)(?:x\.com\s*)?We would like to show you a description here but the site won.t allow us\.?",
                  " ", text)
    t = re.sub(r"[^()]{0,60}\(@\w+\)\s*/\s*(?:X|Twitter)\b", " ", text)
    t = re.sub(r"(?i)\b(?:on (?:X|Twitter)|Posts? / X|X \(formerly Twitter\)|/ X\b|\| X\b)", " ", t)
    t = re.sub(r"[️‍]", "", t)
    for h in own_handles:
        if h:
            t = re.sub(rf"@?{re.escape(h)}\b", " ", t, flags=re.I)

    def handle_to_name(m):
        h = re.sub(r"_(?:us|hq|inc|app|io|co|team|official)$", "", m.group(1), flags=re.I)
        return " @ " + h.replace("_", " ").title() + ", "
    t = re.sub(r"@(\w{3,30})", handle_to_name, t)
    return " ".join(t.split())


def enrich(p: Persona, extras: dict) -> list[str]:
    """Fold everything learned from the persona's own websites and social bios back into
    the persona, so search queries and features use it like any intro field.
    Returns a list of human-readable notes for the log/output."""
    notes = []
    known = {tu.norm_company(c) for c in p.companies + p.weak_companies}

    def add_company(c, strong):
        k = tu.norm_company(c)
        if c and k and len(k) > 1 and k not in known and not _is_title(c):
            known.add(k)
            (p.companies if strong else p.weak_companies).append(c)
            notes.append(f"{'company' if strong else 'weak company'}: {c}")

    p.li_companies = list(dict.fromkeys(extras.get("li_companies", [])))
    # the site's own name is the organisation the persona linked to -> a stated company
    for name in extras.get("site_names", []):
        add_company(name, strong=True)
    # text about the person (site sentences, social bios) -> titles / companies / location / keywords
    own = [p.twitter, p.bluesky.split(".")[0] if p.bluesky else None, p.github]
    bios = [clean_social_bio(extras.get(k, ""), own) for k in ("twitter_bio", "bsky_bio", "github_bio")]
    for src in extras.get("mentions", []) + bios:
        if not src:
            continue
        info = parse_intro(src)
        for c in info["companies"]:
            add_company(c, strong=False)
        for t in info["titles"]:
            # "Jane Doe is Head of Community" -> "Head of Community"
            for nm in (p.full_name, p.first, p.last):
                if nm and len(nm) > 1:
                    t = re.sub(rf"(?i)\b{re.escape(nm)}\b", " ", t)
            t = re.sub(r"(?i)^\W*(?:is|was|as|our|the|a|an|now)\b\s*(?:(?:a|an|the)\b\s*)?", "", t.strip()).strip(" ,.-")
            if t and t not in p.titles and len(t) < 60 and len(t.split()) <= 5:
                p.titles.append(t)
                notes.append(f"title: {t}")
        if info["country"] and not p.country:
            p.country = info["country"]
            notes.append(f"country: {info['country']}")
        p.keywords = list(dict.fromkeys(p.keywords + info["keywords"]))[:60]
        for d in info["domains"]:
            if d not in p.domains:
                p.domains.append(d)
    return notes


def build(raw: dict, llm=None) -> Persona:
    n = parse_name(raw.get("name") or "")
    intro = (raw.get("intro") or "").strip()
    info = parse_intro(intro)
    if llm is not None and intro:
        extra = llm.parse_intro(intro)
        if extra:
            for key in ("companies", "titles"):
                for v in extra.get(key) or []:
                    if v and tu.norm_company(v) not in {tu.norm_company(x) for x in info[key]}:
                        info[key].append(v)
            info["country"] = info["country"] or (extra.get("country") or None)

    p = Persona(raw=raw, pid=_pid(raw), display_name=raw.get("name") or "",
                first=n["first"], last=n["last"], last_initial=n["last_initial"],
                name_complete=n["complete"], image_url=raw.get("image"), intro=intro)
    p.companies = info["companies"]
    if n["hint"] and tu.norm_company(n["hint"]) not in {tu.norm_company(c) for c in p.companies}:
        p.companies.append(n["hint"])
    p.weak_companies = info["weak_companies"]
    p.titles, p.urls, p.domains = info["titles"], info["urls"], info["domains"]
    p.keywords, p.country, p.city_hint = info["keywords"], info["country"], info["city"]
    p.tz_country, p.tz_region, p.tz_weak = tu.tz_prior(raw.get("timezone"))
    p.industry = raw.get("company_industry")
    p.size = tu.parse_size(raw.get("company_size"))

    for s in raw.get("social_profile") or []:
        p.socials.append(s)
        low = s.lower()
        if "twitter.com/" in low or "x.com/" in low:
            p.twitter = s.rstrip("/").split("/")[-1].lstrip("@")
        elif "bsky.app/profile/" in low:
            p.bluesky = s.rstrip("/").split("/profile/")[-1]
        elif "github.com/" in low:
            p.github = s.rstrip("/").split("/")[-1]
        elif "linkedin.com/in/" in low:
            p.linkedin_hint = s
        elif not any(h in low for h in SOCIAL_HOSTS):
            p.urls.append(s)
            d = tu.registrable_domain(s)
            if d not in p.domains:
                p.domains.append(d)
    return p
