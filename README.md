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
- `--browser-fallback`: retries a blocked fetch in an anonymous Patchright browser

An optional `MISTRAL_API_KEY` in `.env` (the free tier is enough) lets an LLM help parse messy intros; the rule-based parser always runs.

## How it works

```
persona ─► normalise ─► candidates ─► snippet pre-rank ─► fetch top-3 guest profiles ─► features ─► calibrated score ─► decision
```

1. **Normalise:** clean up names, parse the intro, and turn the timezone into a country prior.
   - Names: `"Eric Doty (Superpath)"` → name + company hint; `"DamionW"` → `Damion W.`; `"Uriel S."` → first name + initial.
   - Intro: `@ Company`, `at Company`, `Founder of X`, `X (https://…)` patterns, plus domains, titles and "in UK"/"City, ST" locations. "Tools:" / "Looking for:" asides are ignored.
2. **Candidates,** in order of precision:
   - **Links the persona publishes:** LinkedIn links on their own website, or in a Bluesky bio via its open public API.
   - **A search ladder:** start with `"Full Name" "Company" site:linkedin.com/in` and loosen step by step. It uses free search backends through `ddgs` (Bing, Yahoo, DDG, Brave, Mojeek), rotating between them, and stops early once a strong match appears.
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
| social | Twitter/GitHub handle on the profile, or the profile linked from the persona's own pages |
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
| slow, jittered | LinkedIn 20–45 s between requests + a 2–5 min break every 15; each search engine 6–15 s, with a break every 25 |
| backoff | HTTP 999/429 or an authwall → 15 min cool-down, doubling up to 1 h |
| fetch less | snippet pre-ranking means at most ~3 profile fetches per persona; everything is cached in SQLite |
| no wall bypass | only what LinkedIn shows logged-out visitors is read; walled profiles are scored from their search snippet |
| browser fallback | Patchright (CDP leaks patched) on real Chrome, headful, `AutomationControlled` disabled, plus a dismissible sign-in modal close and a small scroll |

Expect about 1–2 minutes per persona at default delays.

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
