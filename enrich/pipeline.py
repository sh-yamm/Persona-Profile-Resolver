"""End-to-end resolution of one persona."""

import re

from . import config, features, scorer, search
from . import persona as persona_mod
from . import textutil as tu
from .features import Candidate
from .linkedin import Profile
from .net import log
from .sources import persona_links

ISO_NAME = {}
for _name, _iso in sorted(tu.COUNTRY_NAMES.items(), key=lambda kv: len(kv[0])):
    if len(_name) > 3 and _name not in {"america", "london", "dubai", "england"}:
        ISO_NAME[_iso] = _name.title()
ISO_NAME.update({"US": "United States", "GB": "United Kingdom"})


def _q_company(c: str) -> str:
    c = re.sub(r"\(.*?\)", "", c)
    c = re.sub(r"\.(com|ai|io|co|uk|life|net|org|app|dev)\b", "", c, flags=re.I)
    return c.replace("-", " ").strip()


def _distinctive(p) -> list[str]:
    """Intro words specific enough to search on ('spiffworkflow', 'neovim'): not generic,
    not a country/name/company already used."""
    used = set(tu.tokens(p.full_name)) | {w for c in p.companies + p.weak_companies for w in tu.tokens(c)}
    out = []
    for k in p.keywords:
        if (len(k) >= 5 and k not in tu.COMMON_WORDS and k not in used
                and k not in tu.TITLE_WORDS and not tu.country_from_text(k)):
            out.append(k)
    return sorted(out, key=len, reverse=True)


def _q_title(t: str) -> str:
    t = re.split(r"\s+(?:of|@|at)\s+", t)[0]
    return re.sub(r"\b\d+x\s+", "", t).strip()


def build_queries(p) -> list[tuple[str, str]]:
    """(query, tier) from most to least specific."""
    site = "site:linkedin.com/in"
    q: list[tuple[str, str]] = []
    country = ISO_NAME.get(p.country or (None if p.tz_weak else p.tz_country) or "", "")
    if p.name_complete:
        full = p.full_name
        for c in p.companies[:2]:
            q.append((f'"{full}" "{_q_company(c)}" {site}', "tight"))
        if p.titles:
            best_title = max(p.titles, key=lambda t: ((tu.seniority(t) or 0), -len(t)))
            q.append((f'"{full}" {_q_title(best_title)} {site}', "tight"))
        if p.city_hint:
            q.append((f'"{full}" {p.city_hint} {site}', "tight"))
        rare = _distinctive(p)
        if rare and not p.companies:
            q.append((f'"{full}" {" ".join(rare[:2])}', "tight"))
        q.append((f'"{full}" {site}', "tight" if not p.companies else "loose"))
        if p.companies:
            q.append((f"{full} {_q_company(p.companies[0])} linkedin", "loose"))
        if p.twitter:
            q.append((f"{full} {p.twitter} linkedin", "loose"))
        if p.domains:
            q.append((f'"{full}" {p.domains[0]} linkedin', "loose"))
        q.append((f"{full} linkedin {country}".strip(), "loose"))
        for c in p.weak_companies[:1]:
            q.append((f'"{full}" {_q_company(c)} linkedin', "loose"))
    else:
        first, title = p.first, (_q_title(p.titles[0]) if p.titles else "")
        ind = p.industry or ""
        if p.companies:
            q.append((f'"{first}" "{_q_company(p.companies[0])}" {site}', "tight"))
        q.append((" ".join(x for x in (f'"{first}"', title, ind, country, site) if x), "tight"))
        q.append((" ".join(x for x in (f'"{first}"', title, f'"{ind}"' if ind else "", "linkedin") if x), "loose"))
        if title and country:
            q.append((f'"{first}" {title} {country} {site}', "loose"))
        if ind and country:
            q.append((f'"{first}" {ind} {country} {site}', "loose"))
    seen, out = set(), []
    for qq, tier in q:
        qq = " ".join(qq.split())
        if qq not in seen:
            seen.add(qq)
            out.append((qq, tier))
    return out[:config.MAX_QUERIES_PER_PERSONA]


class Resolver:
    def __init__(self, fetcher, cache, linkedin, searcher, face=None, llm=None):
        self.fetcher, self.cache, self.li = fetcher, cache, linkedin
        self.searcher, self.face, self.llm = searcher, face, llm

    # ------------------------------------------------------------------ helpers
    def _score_pool(self, p, pool: dict, extras: dict) -> list[Candidate]:
        for c in pool.values():
            features.extract(p, c, extras)
        full_names = [c for c in pool.values() if c.levels.get("name") in ("full", "first_initial")]
        for c in pool.values():
            same = len(full_names) - (1 if c in full_names else 0)
            if not p.name_complete:
                # first name only: always assume many namesakes; a last initial
                # ("Damion W.") already rules most of them out
                same = max(same, 1 if p.last_initial else 3)
            c.score = scorer.raw_score(c.levels, same)
            c.prob = scorer.probability(c.score)
            c.evidence["_same_name_candidates"] = same
        return sorted(pool.values(), key=lambda c: c.score, reverse=True)

    @staticmethod
    def _decided(pool: dict) -> bool:
        ranked = sorted(pool.values(), key=lambda c: c.prob, reverse=True)
        if not ranked or not (ranked[0].profile and ranked[0].profile.ok):
            return False
        runner_up = ranked[1].prob if len(ranked) > 1 else 0.0
        return ranked[0].prob >= 0.95 and ranked[0].prob - runner_up >= 0.5

    def _add(self, pool, url, source, snippet=None, rank=99, page_profiles=1):
        c = pool.get(url) or Candidate(url=url)
        c.sources.add(source)
        if snippet and _snippet_fits_url(snippet, url):
            c.snippets.append(snippet)
        c.best_rank = min(c.best_rank, rank)
        if source in ("website", "bluesky", "persona"):
            c.page_profiles = min(c.page_profiles, page_profiles)
        pool[url] = c

    # --------------------------------------------------------------------- main
    def resolve(self, raw: dict) -> dict:
        p = persona_mod.build(raw, self.llm)
        p.rare_terms = _distinctive(p)
        log(f"=== {p.display_name!r} ({p.kind}) companies={p.companies} titles={p.titles[:2]}")
        pool: dict[str, Candidate] = {}

        # 1) links the persona publishes itself
        direct, extras = persona_links(p, self.fetcher)
        if extras.get("bsky_bio"):
            more = persona_mod.parse_intro(extras["bsky_bio"])
            p.weak_companies += [c for c in more["companies"] if c not in p.companies]
            p.keywords = list(dict.fromkeys(p.keywords + more["keywords"]))
        for d in direct:
            self._add(pool, d["url"], d["source"], page_profiles=d["page_profiles"])

        # 2) search ladder, stop early once a strong candidate appears
        for query, tier in build_queries(p):
            results = self.searcher.search(query)
            for rank, r in enumerate(results, 1):
                url = search.canonical_profile(r["href"])
                if url:
                    snip = search.parse_snippet(r["title"], r["body"])
                    self._add(pool, url, f"search:{tier}", snip, rank)
                    continue
                # a post by the person reveals their profile slug; its text is evidence too
                url = search.profile_from_post(r["href"])
                if url:
                    snip = {"name": _name_for_fused_slug(p, url), "headline": "", "company": "",
                            "location": "", "education": "",
                            "text": f"{r['title']} {r['body']}"}
                    self._add(pool, url, f"search:{tier}", snip, rank + 2)
                    if r["href"] not in pool[url].post_urls:
                        pool[url].post_urls.append(r["href"])
            if pool:
                ranked = self._score_pool(p, pool, extras)
                if ranked[0].prob >= config.STRONG_SNIPPET_SCORE and ranked[0].levels.get("name") == "full":
                    break

        # 3) slug guesses when search found nobody with the right name
        if p.name_complete:
            ranked = self._score_pool(p, pool, extras) if pool else []
            if not any(c.levels.get("name") == "full" for c in ranked):
                base = [tu.norm(p.first).replace(" ", ""), tu.norm(p.last).replace(" ", "")]
                for slug in (f"{base[0]}{base[1]}", f"{base[0]}-{base[1]}"):
                    self._add(pool, f"https://www.linkedin.com/in/{slug}", "slug")

        if not pool:
            return self._result(p, [])

        # 4) fetch the top-K guest profiles (persona-linked ones always)
        ranked = self._score_pool(p, pool, extras)
        ranked = [c for c in ranked if c.levels.get("name") != "mismatch"] or ranked
        to_fetch = [c for c in ranked if c.levels.get("source") == "persona_link"]
        to_fetch += [c for c in ranked if c not in to_fetch][:max(0, config.TOP_K_FETCH - len(to_fetch))]
        persona_face = self.face.embedding(_drive(p.image_url)) if (self.face and p.image_url) else None
        # every LinkedIn request is precious: fetch one at a time, best first, and stop as
        # soon as a fetched profile is decisively ahead of everything else in the pool
        for c in to_fetch:
            if self._decided(pool):
                break
            log(f"  [fetch] {c.url}  (snippet p={c.prob:.2f})")
            c.profile = self.li.profile(c.url)
            if c.profile.error == "404":
                pool.pop(c.url, None)
                continue
            if persona_face and c.profile.ok and c.profile.image_url:
                c.face_sim = self.face.similarity(persona_face, self.face.embedding(c.profile.image_url))
            self._score_pool(p, pool, extras)

        # profile page walled? posts are still served to guests and carry the author's
        # name, photo and writing -> enough for name, face and keyword evidence
        for c in to_fetch:
            if c.url not in pool or (c.profile and c.profile.ok) or self._decided(pool):
                continue
            prof = self.li.author_from_posts(c.url, c.post_urls or self._find_posts(c.url))
            if prof:
                log(f"  [posts] {c.url} <- author {prof.name!r}, photo={'yes' if prof.image_url else 'no'}")
                c.profile = prof
                if persona_face and prof.image_url:
                    c.face_sim = self.face.similarity(persona_face, self.face.embedding(prof.image_url))
                self._score_pool(p, pool, extras)

        ranked = self._score_pool(p, pool, extras)
        return self._result(p, ranked, persona_face is not None)

    def _find_posts(self, profile_url: str) -> list[str]:
        slug = search.slug_of(profile_url)
        out = []
        for r in self.searcher.search(f"linkedin.com/posts/{slug}", max_results=8):
            if search.profile_from_post(r["href"]) == profile_url and r["href"] not in out:
                out.append(r["href"])
        return out

    def _result(self, p, ranked: list[Candidate], persona_face: bool = False) -> dict:
        best = ranked[0] if ranked else None
        status = "not_found"
        if best:
            second = ranked[1].prob if len(ranked) > 1 else 0.0
            if best.prob >= config.TAU_ACCEPT:
                status = "matched" if best.prob - second >= config.TAU_AMBIGUOUS_MARGIN else "ambiguous"
            else:
                status = "low_confidence"
        out = {
            "input": p.raw,
            "linkedin_url": best.url if best else None,
            "confidence": round(best.prob, 3) if best else 0.0,
            "confidence_interval": list(scorer.interval(best.levels, best.evidence.get("_same_name_candidates", 0))) if best else [0.0, 0.0],
            "status": status,
            "persona_type": p.kind,
            "fields_validated": scorer.fields_validated(best.levels) if best else {"count": 0, "available": 0},
            "evidence": _explain(best) if best else {},
            "profile": _profile_summary(best) if best else None,
            "alternatives": [{"url": c.url, "confidence": round(c.prob, 3),
                              "name": c.view()["name"]} for c in ranked[1:4]],
            "persona_face_detected": persona_face,
            # per-candidate feature levels: input for calibrate.py
            "candidates": [{"url": c.url, "score": round(c.score, 3), "levels": c.levels,
                            "same_name": c.evidence.get("_same_name_candidates", 0)}
                           for c in ranked[:8]],
            "parsed_persona": {k: v for k, v in p.summary().items() if k in (
                "first", "last", "last_initial", "companies", "weak_companies", "titles",
                "domains", "country", "tz_country", "industry", "size")},
        }
        log(f"  => {out['status']} {out['linkedin_url']} p={out['confidence']} CI={out['confidence_interval']}")
        return out


def _name_for_fused_slug(p, url: str) -> str:
    """'damionwaltermeyer' + persona first 'Damion' -> 'damion waltermeyer'."""
    slug = re.sub(r"[-_]?\d.*$", "", search.slug_of(url).lower())
    if "-" in slug:
        return slug.replace("-", " ")
    first = tu.norm(p.first).replace(" ", "")
    if first and slug.startswith(first) and len(slug) > len(first) + 1:
        return f"{first} {slug[len(first):]}"
    return ""


def _snippet_fits_url(snippet: dict, url: str) -> bool:
    """Search engines occasionally splice two results together; reject a snippet whose
    name shares nothing with the profile slug (e.g. 'Jane Doe' on /in/jsmith-12)."""
    toks = [t for t in tu.norm(snippet.get("name")).split() if len(t) >= 3]
    slug = tu.norm(search.slug_of(url)).replace(" ", "")
    if not toks or not re.search(r"[a-z]{4,}", slug):
        return True                       # opaque slug (e.g. ACoAAB...): cannot judge
    if any(t in slug or t[:4] in slug for t in toks):
        return True
    # transliterations / nicknames: 'Ihor' on /in/igor1313
    from rapidfuzz.distance import JaroWinkler
    return any(JaroWinkler.similarity(t, slug[i:i + len(t)]) >= 0.8
               for t in toks for i in range(0, max(1, len(slug) - len(t) + 1)))


def _drive(url):
    from .face import drive_direct
    return drive_direct(url)


def _explain(c: Candidate) -> dict:
    contrib = scorer.contributions(c.levels)
    return {f: {"level": lv, "weight": contrib.get(f, 0.0), **c.evidence.get(f, {})}
            for f, lv in c.levels.items() if lv is not None}


def _profile_summary(c: Candidate) -> dict:
    v = c.view()
    return {"name": v["name"], "headline": (v["headlines"] or [""])[0],
            "companies": list(dict.fromkeys(v["companies"]))[:4], "location": v["location"],
            "fetched": v["fetched"], "via": (c.profile.source if c.profile and c.profile.ok else "snippet"),
            "sources": sorted(c.sources)}
