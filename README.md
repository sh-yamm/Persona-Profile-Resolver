# Persona-Profile-Resolver

Given a partial **persona**, this finds the matching public **LinkedIn profile** and says how sure it is. A persona can include:

- a name, possibly incomplete
- a photo
- a free-text intro
- a timezone
- company industry and size
- social links

For each persona it returns:

- the profile URL
- a calibrated probability, with a 90% confidence interval
- the number of persona fields the profile actually agrees with
- a per-field breakdown of the evidence

**No LinkedIn account is used, ever.** Every request is an anonymous guest request, sent slowly and with a realistic browser fingerprint. All results are cached, so an interrupted run resumes where it stopped.

## Quick start

```bash
python -m venv .venv && .venv/Scripts/activate        # Windows (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
python run.py personas.json -o out/                   # writes out/results.json + out/results.csv
pytest -q                                             # offline tests
```

Input is a JSON list of persona objects:

```json
[{"name": "Jane Doe", "image": "https://drive.google.com/file/d/<id>/view",
  "intro": "Head of Ops @ Acme (https://acme.io)", "timezone": "America/Chicago",
  "company_industry": null, "company_size": "11-50 Employees",
  "social_profile": ["https://twitter.com/janedoe"]}]
```

Options:
- `--limit N`
- `--no-face`
- `--delay-scale X`: multiplies every politeness delay
- `--http-only`: fetch LinkedIn with plain HTTP instead of the anonymous browser; `--headless`: hide the browser window

An optional `MISTRAL_API_KEY` in `.env` (the free tier is enough) lets an LLM help parse messy intros; the rule-based parser always runs.

## How it works

```
persona ─► normalise ─► candidates ─► snippet pre-rank ─► fetch top-3 guest profiles ─► features ─► calibrated score ─► decision
```

1. **Normalise:** clean up names, parse the intro, and turn the timezone into a country prior.
   - Names: `"Eric Doty (Superpath)"` → name + company hint; `"DamionW"` → `Damion W.`; `"Uriel S."` → first name + initial.
   - Intro: `@ Company`, `at Company`, `Founder of X`, `X (https://…)` patterns, plus domains, titles and "in UK"/"City, ST" locations. "Tools:" / "Looking for:" asides are ignored.
2. **Candidates** (all of them are used together):
   - **Links the persona publishes:** LinkedIn links on their own website, or in a Bluesky bio via its open public API.
   - **A search ladder:** start with `"Full Name" "Company" site:linkedin.com/in` and loosen step by step. It uses free search backends through `ddgs` (Bing, Yahoo, DDG, Brave, Mojeek), rotating between them, and stops early once a strong match appears.
   - **Rare intro words:** distinctive words from the intro (e.g. `"Kevin Burnett" spiffworkflow`) are searched, and the city when one is stated.
   - **Post URLs:** a result like `linkedin.com/posts/damionwaltermeyer_…` reveals its author's profile slug, and the post text becomes evidence.
   - **Slug guesses** (`linkedin.com/in/firstlast`) only when nothing else is found.
3. **Snippet pre-rank:** search snippets already carry the headline, company and location. Candidates are ranked on those first, so only about 3 profiles per persona are ever fetched.
4. **Guest profile parse:** the stable, SEO-facing data (JSON-LD `Person`, `og:` tags). This gives name, location and country, current company, the company website link, school, recent posts and the photo. Fields that LinkedIn masks for guests are ignored.
5. **Features:** each returns a level, or *unknown* when either side lacks the data. Missing data is never treated as a mismatch.

| field | how |
|---|---|
| name | Jaro-Winkler, nicknames (Chris↔Christopher), initials, conflicting last names |
| company | fuzzy company match against the profile's companies, headline and snippets |
| domain | a domain from the intro equals the profile's company-website link or company slug |
| title | fuzzy title match + seniority level |
| location | profile country vs stated country, or vs the timezone country/region |
| face | OpenCV **YuNet** detector + **SFace** embedding (open-source ONNX, CPU); cosine ≥ 0.363 means same identity |
| social | Twitter/GitHub handle on the profile, or the profile linked from the persona's own pages; a handle spelled from the candidate's name (`@rajavijayach` ≈ "Raja Vijaya Ch") |
| rare terms | a rare intro word (≥ 8 chars) that prefix-matches the candidate's company counts as a stated company ("spiffworkflow" ≈ "SpiffWorks") |
| keywords | overlap between intro interests and headline/about/posts |
| industry | Type-2 personas: industry synonyms in headline/company/snippets |
| source | persona-published link ≫ top result of a tight query ≫ loose search ≫ slug guess |

6. **Score (Fellegi–Sunter):**
   - `score = prior + Σ log(m/u) weights − namesake penalty`. The namesake penalty applies when several distinct profiles share the full name, because a common name is weaker evidence.
   - `p = σ(a·score + b)`, where `a` and `b` are fitted by `calibrate.py` (Platt scaling) on hand-verified labels.
   - The **90% interval** combines two sources of uncertainty: ±35% noise on every field weight, and 200 bootstrap refits of the calibration.
7. **Decide:**
   - `matched` when p ≥ 0.5 and the runner-up is clearly behind.
   - `ambiguous` when p ≥ 0.5 but the runner-up is close.
   - `low_confidence` otherwise, still with the best guess.
   - `not_found` when there are no candidates at all.

### Example output (abridged)

```json
{
  "linkedin_url": "https://www.linkedin.com/in/<slug>",
  "confidence": 0.99, "confidence_interval": [0.97, 1.0], "status": "matched",
  "fields_validated": {"count": 5, "available": 6, "agreed": ["name","company","domain","location","face"], "disagreed": []},
  "evidence": {"name": {"level": "full", "weight": 5.0}, "face": {"level": "strong", "weight": 6.0, "cosine": 0.80}, "...": "..."},
  "alternatives": [{"url": "...", "confidence": 0.02}]
}
```

## Calibrating

Put ground truth in `data/labels.json` (`{"<persona name>": "<linkedin url or null>"}`), then run:

```bash
python calibrate.py out/results.json --labels data/labels.json
```

This prints the top-1 accuracy and writes `models/calibration.json`, which every later run picks up automatically.

## Staying unflagged

| measure | detail |
|---|---|
| no login | anonymous guest only; no cookies or `session.json` from any account |
| real fingerprint | `curl_cffi` impersonates Chrome's TLS (JA3/JA4) and HTTP/2 fingerprint, with a single consistent identity per run |
| human-paced, jittered | LinkedIn 4–9 s, each search engine 2–5 s (rotation spreads load), other sites 1–3 s; short pauses every 25–40 requests. No published limits exist; 999 is driven by fingerprint + a per-IP guest view quota, not spacing |
| backoff | HTTP 999/429 or an authwall → 2 min cool-down, doubling up to 30 min, reset on success; escalation persists across restarts |
| fetch less | snippet pre-ranking means at most ~3 profile fetches per persona; everything is cached in SQLite |
| no wall bypass | only what LinkedIn shows logged-out visitors is read; walled profiles are scored from their search snippet |
| real browser for LinkedIn | anonymous Patchright on real Chrome (one window reused, headful, `AutomationControlled` off), modal close + small scroll; plain HTTP got fingerprinted to 999 far sooner. `--http-only` / `--headless` to change |

**What LinkedIn tolerates, in practice:** an anonymous guest gets only a handful of profile pages per IP before HTTP 999, after which roughly one request per 5–10 minutes gets through. The pipeline is built around that:

- **Snippet-first scoring:** search snippets already carry the name, headline, company and location, so most personas resolve without any LinkedIn request.
- **Non-blocking cool-downs:** a blocked fetch never stalls the run. The persona is scored on its snippets, and **retry rounds** at the end wait out each cool-down and fetch the most promising profiles first.
- **Decisive early stop:** once one fetched profile is decisively ahead, no further profiles are fetched for that persona.
- **Junk-URL filter:** slugs scraped from page markup (e.g. `carrie-chan-)43:t751`) are rejected, because fetching them triggered the first 999.
- **Session reset:** after a 999 the guest cookies are discarded along with the cool-down.

The first pass takes about 1 minute per persona. Retry rounds can take hours if the IP is throttled, and you can interrupt them safely. `python run.py … --offline` re-scores everything from the cache with no network at all, which is useful after changing weights or labels.

**Splice-safe snippets:** Bing occasionally glues neighbouring results into one title ("A – X …B – Y | LinkedIn"). Only the result's own part is used as evidence, and snippets whose name doesn't fit the profile slug are dropped.

## Layout

```
run.py            CLI
calibrate.py      fit calibration + report accuracy from labels
enrich/
  config.py       all delays/thresholds
  net.py          polite fetcher (curl_cffi), rate limiter, backoff
  cache.py        SQLite cache
  persona.py      name/intro normalisation (+ optional Mistral via llm.py)
  search.py       ddgs multi-backend search, snippet parser
  sources.py      persona websites / Bluesky direct links
  linkedin.py     guest profile parser; browser.py = Patchright fallback
  face.py         YuNet + SFace
  features.py     per-field comparators
  scorer.py       Fellegi–Sunter weights, calibration, intervals
  pipeline.py     orchestration
tests/            offline tests on synthetic data
```
