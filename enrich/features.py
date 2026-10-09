"""Compare a persona against one candidate profile, field by field.

Every comparator returns a discrete *level* (e.g. "strong", "weak", "mismatch") or
None when either side lacks the data — missing data is never counted as a mismatch.
The scorer turns levels into Fellegi–Sunter log-likelihood weights.
"""

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from . import textutil as tu
from .linkedin import Profile


@dataclass
class Candidate:
    url: str
    sources: set = field(default_factory=set)        # website / bluesky / search:tight / search:loose / slug
    snippets: list = field(default_factory=list)     # parsed snippet dicts
    post_urls: list = field(default_factory=list)    # linkedin.com/posts/<slug>_... by this person
    company_info: list | None = None                 # guest company-page facts of current employer
    best_rank: int = 99
    page_profiles: int = 99                           # profiles on the linking web page
    profile: Profile | None = None
    face_sim: float | None = None
    levels: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    score: float = 0.0
    prob: float = 0.0

    def view(self) -> dict:
        """Merged facts from the fetched profile (if any) and every search snippet."""
        p = self.profile if (self.profile and self.profile.ok) else None
        snips = self.snippets
        name = (p.name if p else "") or next((s["name"] for s in snips if s.get("name")), "") \
            or _name_from_slug(self.url)
        headlines = [s["headline"] for s in snips if s.get("headline")]
        if p and p.headline:
            headlines.insert(0, p.headline)
        companies = list(p.companies) if p else []
        companies += [s["company"] for s in snips if s.get("company")]
        location = (p.location if p else "") or next((s["location"] for s in snips if s.get("location")), "")
        country = (p.country if p else None) or tu.country_from_text(location)
        snippet_text = " ".join(s["text"] for s in snips)
        facts = self.company_info or []
        websites = (p.websites if p else []) + [f["website"] for f in facts if f.get("website")]
        company_slugs = [u.rstrip("/").split("/")[-1] for u in (p.company_urls if p else [])]
        company_slugs += [f["url"].rstrip("/").split("/")[-1] for f in facts if f.get("url")]
        return {
            "name": name, "headlines": headlines, "companies": companies,
            "company_slugs": company_slugs, "company_facts": facts,
            "websites": websites, "location": location, "country": country,
            "education": (p.education if p else []) + [s["education"] for s in snips if s.get("education")],
            "about": p.about if p else "", "posts": p.posts if p else [],
            "snippet_text": snippet_text, "image_url": p.image_url if p else "",
            "fetched": bool(p),
        }


def _name_from_slug(url: str) -> str:
    """'/in/morgan-rice-4b2a17' -> 'morgan rice'. Only for multi-part slugs; a fused slug
    like 'zhawtof' says nothing reliable about the name and is left unknown."""
    slug = url.rstrip("/").split("/")[-1].lower()
    parts = [x for x in re.split(r"[-_]", slug) if x and not re.search(r"\d", x)]
    return " ".join(parts) if len(parts) >= 2 else ""


# --------------------------------------------------------------------------- #
# name
# --------------------------------------------------------------------------- #

def _first_match(a: str, b: str) -> float:
    a, b = tu.norm(a), tu.norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if b in tu.NICKNAMES.get(a, set()) or a in tu.NICKNAMES.get(b, set()):
        return 0.95
    if len(a) >= 3 and (b.startswith(a) or a.startswith(b)):
        return 0.9
    return JaroWinkler.similarity(a, b)


def name_level(persona, cand_name: str) -> tuple[str | None, dict]:
    toks = [t for t in tu.norm(re.sub(r"\(.*?\)", " ", cand_name)).split()
            if t not in {"dr", "mr", "ms", "mrs", "phd", "mba", "md", "jr", "sr", "ii", "iii"}]
    if not toks or not persona.first:
        return None, {}
    pf = tu.norm(persona.first).split()
    first_sim = max(_first_match(pf[0], t) for t in toks[:2])
    ev = {"first_sim": round(first_sim, 3)}
    rest = toks[1:] if _first_match(pf[0], toks[0]) >= 0.85 else toks
    if persona.last:
        pl = tu.norm(persona.last).split()
        last_sim = max((JaroWinkler.similarity(pl[-1], t) for t in rest), default=0.0)
        joined = "".join(toks)
        if tu.norm(persona.last).replace(" ", "") in joined:
            last_sim = max(last_sim, 0.97)
        ev["last_sim"] = round(last_sim, 3)
        # candidate shows only an initial ("Ihor D.") that agrees with the persona's surname
        if first_sim >= 0.9 and rest and len(rest[-1]) == 1 and pl[-1].startswith(rest[-1]):
            return "partial", ev
        if first_sim >= 0.9 and last_sim >= 0.93:
            return "full", ev
        if first_sim >= 0.85 and last_sim >= 0.85:
            return "partial", ev
        if last_sim >= 0.93 and first_sim < 0.85:
            return "last_only", ev
        return "mismatch", ev
    if first_sim < 0.85:
        return "mismatch", ev
    if persona.last_initial:
        ok = any(t.startswith(persona.last_initial.lower()) for t in rest)
        return ("first_initial" if ok else "mismatch"), ev
    return "first_only", ev


# --------------------------------------------------------------------------- #
# company / domain / title / location / social / keywords / industry
# --------------------------------------------------------------------------- #

def _prefix_match(a: str, b: str) -> bool:
    """'spiffworkflow' ~ 'spiffworks': long shared prefix of two long tokens."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 6 and n >= 0.7 * min(len(a), len(b))


def _company_sim(a: str, b: str) -> float:
    a, b = tu.norm_company(a), tu.norm_company(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if any(_prefix_match(x, y) for x in a.split() for y in b.split()):
        return 90.0
    if len(a) < 3 or len(b) < 3:
        return 0.0
    short, long_ = sorted((a, b), key=len)
    if re.search(rf"\b{re.escape(short)}\b", long_):
        return 95.0
    return fuzz.token_set_ratio(a, b)


def _text_contains_company(company: str, text: str) -> bool:
    c = tu.norm_company(company)
    if len(c) < 4:
        return False
    return re.search(rf"\b{re.escape(c)}\b", tu.norm(text)) is not None or \
        c.replace(" ", "") in tu.norm(text).replace(" ", "")


def company_level(persona, v: dict) -> tuple[str | None, dict]:
    # rare intro words (>= 8 chars, e.g. 'spiffworkflow') act like a stated company
    rare = [k for k in getattr(persona, "rare_terms", []) if len(k) >= 8]
    strong, weak = persona.companies + rare, persona.weak_companies
    li_slugs = {u.rstrip("/").split("/")[-1] for u in extras_li_companies(persona)}
    if li_slugs and li_slugs & set(v["company_slugs"]):
        return "strong", {"matched": sorted(li_slugs & set(v["company_slugs"]))[0], "where": "company page"}
    if not strong and not weak:
        return None, {}
    fields_ = v["companies"] + v["headlines"] + v["company_slugs"]
    best, best_c = 0.0, ""
    for c in strong:
        for f in fields_:
            s = _company_sim(c, f)
            if s > best:
                best, best_c = s, c
    if best >= 88:
        return "strong", {"matched": best_c, "sim": best}
    for c in strong:
        if _text_contains_company(c, v["snippet_text"]):
            return "strong", {"matched": c, "where": "snippet"}
    for c in strong + weak:
        if _text_contains_company(c, v["about"] + " " + " ".join(v["posts"][:5])):
            return "weak", {"matched": c, "where": "about/posts"}
    for c in weak:
        for f in fields_:
            if _company_sim(c, f) >= 88:
                return "weak", {"matched": c}
    if not v["companies"] and not v["headlines"]:
        return None, {}
    return ("mismatch" if strong else None), {"best_sim": best}


def extras_li_companies(persona) -> list[str]:
    return getattr(persona, "li_companies", []) or []


def size_level(persona, v: dict) -> tuple[str | None, dict]:
    """Persona company_size vs the candidate's current company size bucket."""
    if not persona.size:
        return None, {}
    for f in v["company_facts"]:
        rng = tu.parse_size(f.get("size"))
        if rng:
            lo, hi = persona.size
            ok = not (rng[1] < lo or rng[0] > hi)
            return ("match" if ok else "mismatch"), {"persona": persona.size, "company": f.get("size")}
    return None, {}


def domain_level(persona, v: dict) -> tuple[str | None, dict]:
    if not persona.domains:
        return None, {}
    sites = {tu.registrable_domain(w) for w in v["websites"]}
    for d in persona.domains:
        if d in sites:
            return "match", {"domain": d}
        if d in v["snippet_text"].lower() or d in (v["about"] or "").lower():
            return "match", {"domain": d, "where": "text"}
        label = d.split(".")[0]
        if len(label) >= 4 and any(label.replace("-", "") in s.replace("-", "") for s in v["company_slugs"]):
            return "match", {"domain": d, "where": "company_slug"}
    return (None if not sites else "mismatch"), {}


def title_level(persona, v: dict) -> tuple[str | None, dict]:
    if not persona.titles:
        return None, {}
    if not v["headlines"]:
        # no headline (e.g. evidence came from a post): an exact multi-word title phrase
        # in the person's own writing still counts, e.g. "...need a Principal Security Engineer"
        text = tu.norm(" ".join(v["posts"][:5]) + " " + v["about"])
        for t in persona.titles:
            nt = tu.norm(t)
            if len(nt.split()) >= 2 and f" {nt} " in f" {text} ":
                return "weak", {"phrase_in_text": t}
        return None, {}
    head = tu.expand_title(" ".join(v["headlines"]))
    best = max(fuzz.token_set_ratio(tu.expand_title(t), head) for t in persona.titles)
    ps = max((tu.seniority(t) or -1) for t in persona.titles)
    cs = tu.seniority(head)
    if best >= 85:
        return "strong", {"sim": best}
    if ps >= 0 and cs is not None and ps == cs:
        return "weak", {"sim": best, "seniority": ps}
    if best >= 60:
        return "weak", {"sim": best}
    return "mismatch", {"sim": best}


def location_level(persona, v: dict) -> tuple[str | None, dict]:
    cc = v["country"]
    if not cc:
        return None, {}
    if persona.country:
        if cc == persona.country:
            return "country_stated", {"country": cc}
        return "mismatch_stated", {"cand": cc, "persona": persona.country}
    if persona.city_hint and persona.city_hint.lower() in (v["location"] or "").lower():
        return "country_stated", {"city": persona.city_hint}
    if persona.tz_country and not persona.tz_weak:
        if cc == persona.tz_country:
            return "country_tz", {"country": cc}
        if persona.tz_region and tu.COUNTRY_REGION.get(cc) == persona.tz_region:
            return "region_tz", {"country": cc}
        return "mismatch_tz", {"cand": cc, "tz": persona.tz_country}
    if persona.tz_region:
        return ("region_tz" if tu.COUNTRY_REGION.get(cc) == persona.tz_region else None), {}
    return None, {}


def social_level(persona, v: dict, extras: dict, url: str) -> tuple[str | None, dict]:
    handles = [h.lower() for h in (persona.twitter, persona.github) if h]
    blob = (" ".join(v["websites"]) + " " + v["about"] + " " + v["snippet_text"]).lower()
    for h in handles:
        if len(h) >= 4 and re.search(rf"(twitter\.com|x\.com|github\.com)/{re.escape(h)}\b|@{re.escape(h)}\b", blob):
            return "match", {"handle": h}
    # handle spelled from the candidate's own name tokens: @rajavijayach ~ 'Raja Vijaya Ch'
    name_glued = tu.norm(v["name"]).replace(" ", "")
    all_handles = handles + ([persona.bluesky.split(".")[0].lower()] if persona.bluesky else [])
    for h in all_handles:
        if len(h) >= 6 and name_glued and (h == name_glued or (len(name_glued) >= 6 and h.startswith(name_glued)
                                                                and len(h) - len(name_glued) <= 2)):
            return "handle_name", {"handle": h, "name": v["name"]}
    slug = url.rstrip("/").split("/")[-1].lower()
    for t in [extras.get("bsky_bio", "")] + list(extras.get("site_text", {}).values()):
        if slug and f"linkedin.com/in/{slug}" in t.lower():
            return "match", {"linked_from": "persona page"}
    return None, {}


def keyword_level(persona, v: dict) -> tuple[str | None, dict]:
    pk = set(persona.keywords) - set(tu.tokens(persona.full_name))
    if len(pk) < 3:
        return None, {}
    ct = set(tu.tokens(" ".join(v["headlines"]) + " " + v["about"] + " " + v["snippet_text"]
                       + " " + " ".join(v["posts"][:5])))
    if not ct:
        return None, {}
    overlap = pk & ct
    overlap |= {k for k in pk - overlap if len(k) >= 6 and any(_prefix_match(k, t) for t in ct)}
    frac = len(overlap) / len(pk)
    lvl = "high" if frac >= 0.3 else "some" if frac >= 0.12 else "none"
    return lvl, {"overlap": sorted(overlap)[:10], "frac": round(frac, 2)}


INDUSTRY_SYNONYMS = {
    "human resources software": ["hr", "human resources", "hr tech", "hrtech", "people", "talent", "recruit", "payroll", "workforce"],
    "construction software": ["construction", "contech", "builder", "building", "aec", "jobsite"],
    "internet": ["internet", "online", "web", "digital", "platform", "saas", "marketplace"],
    "ai": ["ai", "artificial intelligence", "machine learning", "ml", "llm", "genai"],
    "tech": ["tech", "technology", "software", "saas", "digital"],
}


def industry_level(persona, v: dict) -> tuple[str | None, dict]:
    if not persona.industry:
        return None, {}
    key = tu.norm(persona.industry)
    words = INDUSTRY_SYNONYMS.get(key) or [w for w in key.split() if w not in {"software", "services"}] or key.split()
    for f in v["company_facts"]:
        ind = tu.norm(f.get("industry"))
        if ind:
            desc = tu.norm(f.get("description"))
            hit = (fuzz.token_set_ratio(key, ind) >= 80
                   or any(f" {tu.norm(w)} " in f" {ind} " for w in words)
                   or sum(f" {tu.norm(w)} " in f" {desc} " for w in words) >= 2)
            return ("company_match" if hit else "company_mismatch"), {"company_industry": f.get("industry")}
    blob = " " + tu.norm(" ".join(v["headlines"] + v["companies"]) + " " + v["about"] + " " + v["snippet_text"]) + " "
    hits = [w for w in words if f" {tu.norm(w)} " in blob]
    if not blob.strip():
        return None, {}
    return ("match" if hits else "none"), {"hits": hits}


def face_level(sim: float | None) -> str | None:
    if sim is None:
        return None
    if sim >= 0.50:
        return "strong"
    if sim >= 0.363:
        return "likely"
    if sim >= 0.25:
        return "unclear"
    return "mismatch"


def source_level(c: Candidate) -> str:
    if "website" in c.sources or "bluesky" in c.sources or "persona" in c.sources:
        return "persona_link" if c.page_profiles <= 3 else "team_page"
    if "search:tight" in c.sources and c.best_rank <= 2:
        return "tight_top"
    if any(s.startswith("search") for s in c.sources):
        return "search"
    return "slug_guess"


def extract(persona, c: Candidate, extras: dict) -> None:
    v = c.view()
    comps = {
        "name": name_level(persona, v["name"]),
        "company": company_level(persona, v),
        "domain": domain_level(persona, v),
        "title": title_level(persona, v),
        "location": location_level(persona, v),
        "social": social_level(persona, v, extras, c.url),
        "keywords": keyword_level(persona, v),
        "industry": industry_level(persona, v),
        "size": size_level(persona, v),
    }
    c.levels = {k: lv for k, (lv, _) in comps.items()}
    c.evidence = {k: ev for k, (_, ev) in comps.items() if ev}
    c.levels["face"] = face_level(c.face_sim)
    if c.face_sim is not None:
        c.evidence["face"] = {"cosine": round(c.face_sim, 3)}
    c.levels["source"] = source_level(c)
