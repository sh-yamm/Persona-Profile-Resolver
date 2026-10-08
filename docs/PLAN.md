# Persona → LinkedIn Profile Resolver: Plan

## 1. Problem recap

- **Stage 1:** given a persona (name, image, intro, timezone, company industry/size, social profiles — any field may be missing), find that person's LinkedIn profile.
- **Stage 2:** verification pipeline that outputs a confidence (e.g. "80% this persona is this profile"), driven by *how many* fields and *how deeply* they match.
- **Evaluation:** accuracy on a hidden test set 50% · innovativeness 20% · verification pipeline quality 30%. Python, runnable as a script.

### Dataset analysis

| | Type 1 (`dataset1.json`, 12 rows) | Type 2 (`dataset2.json`, 5 rows) |
|---|---|---|
| Name | Full name, sometimes noisy: `"Eric Doty (Superpath)"`, handle `"DamionW"` | First name only (`"Peter"`), or first + initial (`"Uriel S."`) |
| Image | Always (Google Drive link) | 2/5 |
| Intro | 10/12 — company, title, URLs (`tightknit.ai`, `m1-project.com`, `deallite.uk`) | Generic: `"Founder in US"`, `"Managing Consultant in UK"` |
| Timezone | Always (→ region/country prior) | Never |
| Industry / size | Mostly null | Always (`"Construction Software"`, `"201–500 Employees"`) |
| Social | 3/12 (Twitter, Bluesky) | None |

Type 1 is a search-and-verify problem. Type 2 is a needle in a haystack: the name alone is useless, so the signal has to come from **industry + size + role + country + face**. Expect lower recall on Type 2. The confidence score needs to reflect that honestly and abstain rather than guess.

## 2. What the probes showed (run 2026-10-09, from this machine, no login)

| Probe | Result |
|---|---|
| `ddgs` backend `bing` / `yahoo` | ✅ `Gaurav Nemade Inventive.ai linkedin` → `linkedin.com/in/gauravnemade` at the top, with a rich snippet (headline, company) |
| `ddgs` backend `duckduckgo` / `brave` / `google` / `mojeek` | ❌ empty this run (likely throttled by the earlier rapid probe). Keep as rotating fallbacks |
| `curl_cffi` (`impersonate="chrome"`) GET `linkedin.com/in/<slug>` | ✅ HTTP 200, no authwall, full guest HTML with JSON-LD `Person`: `name`, `worksFor[]` (current company + company URL), `address` (city, country), `image` (profile photo URL), `awards`, plus recent posts and `og:description` ("Experience · Education · Location"). Past employers and `jobTitle` are **masked** (`****`) for guests |
| Google Drive `uc?export=download&id=…` | ✅ returns `image/jpeg` directly |

**Key takeaway:** the hot path needs **no browser at all**. Plain HTTP with a real Chrome TLS/HTTP2 fingerprint gets everything LinkedIn serves to guests. That is lighter and harder to fingerprint than Playwright. A browser is only a fallback.

## 3. Account safety

- **No LinkedIn account, cookie, or `session.json` is ever used.** Only anonymous guest requests are made. The worst case is a temporary IP throttle (LinkedIn answers HTTP `999`), never an account ban.
- **We only read what LinkedIn serves to logged-out visitors.** We don't bypass a hard authwall (no Referer spoofing, Translate-proxy tricks, etc.). If a profile is walled, we fall back to the search-engine snippet for that candidate and score it with less evidence.

## 4. Anti-detection & rate limiting

Taken from `scrape_linkedin.py` and `linkedin-substack-agent/tools/linkedin_scraper.py`, plus research:

| Technique | Where it comes from | How it's used |
|---|---|---|
| TLS/JA3 + HTTP/2 Chrome fingerprint | research: `curl_cffi` scored on par with stealth browsers in a 2026 31-target benchmark | primary fetcher for LinkedIn and search |
| Consistent identity per session | — | one UA / `Accept-Language` / `sec-ch-ua` set matching the impersonated Chrome version (no random UA mismatch) |
| Sequential, jittered delays | `scrape_linkedin.py` (8–22 s) | per-host token bucket: search ~1 req / 6–15 s; LinkedIn ~1 req / 20–45 s; a long "coffee break" pause (2–5 min) every ~15 LinkedIn hits |
| Backoff on block | — | `999` / `429` / authwall redirect → stop hitting that host for 15–30 min, exponential; the run continues on other stages |
| Resumable runs | `scrape_linkedin.py` skip-if-done | SQLite cache of every search result, profile HTML, image and score; reruns never refetch |
| Browser fallback | ref repo: `--disable-blink-features=AutomationControlled`, `navigator.webdriver` override, scroll, Windows ProactorEventLoop thread fix | swap in **Patchright** (patches those CDP leaks properly), `channel="chrome"`, headful, fresh guest profile; plus the overlay dismissal from `scrape_linkedin.py` (content is already in the DOM) |
| Minimise LinkedIn hits | — | rank candidates on search snippets first; fetch at most the top 3 profiles per persona |

Expected throughput: about 1–2 min per persona. A 100-persona test set is roughly 2–3 h unattended, and resumable.

## 5. Architecture

```
persona.json ─► 1 Normalize ─► 2 Candidate generation ─► 3 Snippet pre-rank ─► 4 Profile fetch (top-K)
                                                                                      │
             results.json ◄─ 7 Decide / abstain ◄─ 6 Calibrated scorer ◄─ 5 Feature extraction
```

**1 · Normalize** (`persona.py`)
- Clean the name: strip `(Superpath)`-style suffixes into a company hint, split handles (`DamionW` → `Damion` + initial `W`), handle initials (`Uriel S.`), and a nickname table (Chris↔Christopher, Jeff↔Jeffrey).
- Parse the intro with regex and heuristics: `@ Company`, `at Company`, `X of Y`, URLs → domains, title keywords, `in UK/US` → country. An LLM parse can be added as an option.
- Map timezone to a country/region prior (`Asia/Kolkata` → IN, `America/*` → US/CA, `Africa/Monrovia` is often just UTC+0, so give it a weak prior).

**2 · Candidate generation** (`search.py`, `sources.py`), in order of precision:
1. **Direct links:** fetch the persona's own websites (`tightknit.ai`, `m1-project.com`, `deallite.uk`, …) and social bios (Bluesky has an open public API; Twitter only via search), then pull any `linkedin.com/in/…` links. These are near-certain hits.
2. **Search ladder:** start tight (`"Full Name" "Company" site:linkedin.com/in`), then loosen (`Full Name Company linkedin`, `Full Name Title Country linkedin`, `Name "domain.com"`). Use Bing first, rotating through Yahoo, DDG and Brave, and stop once strong candidates appear.
3. **Type 2:** search for the company first (`site:linkedin.com/company <industry> <size>`), then `"<FirstName>" <role> <company>`. The guest company page exposes industry and employee count.
4. **Slug guess:** try `linkedin.com/in/firstname-lastname` and `firstnamelastname` as candidates.

**3 · Snippet pre-rank:** score candidates cheaply on the search title/snippet (name + company + title fuzzy match) and keep the top K (3).

**4 · Profile fetch** (`linkedin.py`): parse JSON-LD `Person`, `og:*`, and the visible About/Experience/Education HTML into a `Profile` dataclass. Download the profile photo.

**5 · Feature extraction** (`features.py`), one feature vector per (persona, candidate). Each feature returns *match / mismatch / unknown*, so missing data isn't counted as a mismatch:

| Feature | Method |
|---|---|
| name | Jaro-Winkler on first/last, nickname-aware, initial-compatible, penalise conflicting last names |
| company | fuzzy (`token_set_ratio`) on intro company vs `worksFor` / headline; exact domain match from intro URL vs company website |
| title / role | normalised title tokens, seniority level (Founder/CEO/Head/Lead) |
| location | profile country/city vs timezone / intro country |
| industry & size (Type 2) | company page industry string vs persona industry (fuzzy + small synonym map); employee bucket overlap |
| **face** | OpenCV **YuNet** detector + **SFace** embedder (ONNX, Apache-2.0, already runs with installed `opencv` + `onnxruntime`, no extra install). Cosine similarity of Drive image vs LinkedIn photo. Persona photos may not be real, so the feature is "unknown" when no face is detected |
| social cross-link | persona Twitter/Bluesky/website appears on the profile, or the profile URL appears on those pages |
| semantic overlap | intro interests vs headline/about/posts (TF-IDF cosine; small sentence-embedding model optional) |
| source prior | which channel found it (direct link ≫ tight search ≫ loose search ≫ slug guess), and its rank |

**6 · Scorer** (`scorer.py`), the Stage 2 deliverable:
- **v0, Fellegi–Sunter:** per-feature log-likelihood weights `log(m/u)` from sensible priors, summed and passed through a sigmoid. Works with zero labels and is directly interpretable ("name +3.1, company +4.0, face +2.5, location −0.8").
- **v1, learned:** hand-label the 17 sample personas plus the validation set (the correct profile, plus the top-K wrong candidates as negatives). Fit logistic regression on the same features, calibrate with Platt/isotonic, and validate with leave-one-out.
- **Confidence interval:** bootstrap the scorer over the labelled set to get a percentile interval per prediction. Also report `fields_validated: k / n_available`, as the task asks.

**7 · Decide:**
- Take the best candidate if `p ≥ τ_accept`.
- If the top two are close, say so (`ambiguous`).
- If `p < τ_abstain`, output `NOT_FOUND` rather than a wrong guess.
- Tune τ on labels.

## 6. Output

```json
{
  "input": { ...persona... },
  "linkedin_url": "https://www.linkedin.com/in/gauravnemade",
  "confidence": 0.94,
  "confidence_interval": [0.88, 0.97],
  "status": "matched | ambiguous | not_found",
  "fields_validated": {"count": 5, "available": 6},
  "evidence": {"name": 0.98, "company": 0.95, "face": 0.71, "location": "match", "industry": "unknown"},
  "alternatives": [{"url": "...", "confidence": 0.12}]
}
```

There is also a `results.csv` summary and a `run_log.jsonl`.

## 7. Layout

```
persona-profile-resolver/
  run.py                    # CLI: python run.py dataset1.json dataset2.json -o out/
  enrich/
    config.py               # delays, K, thresholds, backends
    http.py                 # curl_cffi session, per-host rate limiter, backoff, 999 detection
    cache.py                # SQLite cache (search, html, images, scores)
    persona.py              # normalisation + intro parsing
    search.py               # ddgs multi-backend query ladder
    sources.py              # persona websites / Bluesky / social cross-links
    linkedin.py             # guest profile + company page parsing; Patchright fallback
    face.py                 # YuNet + SFace (models auto-downloaded once)
    features.py
    scorer.py               # Fellegi–Sunter v0, logistic + calibration v1, bootstrap CI
    pipeline.py
  data/labels.json          # hand-verified ground truth for the samples
  tests/                    # parser/feature unit tests on saved HTML fixtures (offline)
  docs/PLAN.md
```

## 8. Build order

1. `http.py` + `cache.py` + rate limiter (everything else depends on politeness and resumability).
2. `persona.py` + `search.py` → end-to-end Type 1 with snippet-only scoring (first working baseline).
3. `linkedin.py` guest profile parser + saved fixtures + tests.
4. `features.py` + Fellegi–Sunter scorer → `run.py` producing `results.json`.
5. `face.py`.
6. `sources.py` (website/Bluesky direct links).
7. Type 2 path: company-first search + company page industry/size.
8. Label the 17 samples, fit the calibrated scorer + bootstrap CI, tune thresholds, report accuracy.
9. Patchright fallback, README.

## 9. Risks

- **Search engine throttling:** this is the most likely bottleneck, not LinkedIn. Mitigations: backend rotation, caching, the query ladder stopping early. Optionally, a paid SERP API key (Brave/Serper) if one becomes available.
- **LinkedIn markup changes:** parsing relies on JSON-LD + `og:` tags (stable, SEO-facing), not CSS classes.
- **Type 2 ambiguity:** some personas aren't uniquely identifiable from public data. The abstain path keeps precision up.
- **Non-real persona photos:** the face feature is weighted by detection quality and never vetoes alone.
