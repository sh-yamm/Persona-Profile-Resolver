"""Central knobs. Everything politeness-related lives here so it can be tuned in one place."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DB = ROOT / "cache" / "cache.sqlite"
MODELS_DIR = ROOT / "models"


def _load_dotenv():
    """Tiny .env reader (KEY=VALUE lines) so MISTRAL_API_KEY can live in ./.env."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# --------------------------------------------------------------------------- #
# Network identity — one consistent Chrome fingerprint (TLS + HTTP/2 + headers)
# --------------------------------------------------------------------------- #
IMPERSONATE = "chrome"
ACCEPT_LANGUAGE = "en-US,en;q=0.9"
REQUEST_TIMEOUT = 25

# --------------------------------------------------------------------------- #
# Rate limiting: (min, max) seconds between two requests to the same bucket.
# Multiplied by DELAY_SCALE (CLI --delay-scale).
#
# Neither LinkedIn nor Bing publish limits. LinkedIn's HTTP 999 is driven mainly by
# client fingerprint + IP reputation, not a fixed per-minute rate: in our runs a plain
# HTTP client got 999 even after 6 idle hours while a real (anonymous) Chrome loaded the
# same page first try. So gaps are human-browsing-sized, and the real protection is
# (a) a genuine browser for LinkedIn and (b) backing off only when a block actually happens.
# --------------------------------------------------------------------------- #
DELAY_SCALE = float(os.environ.get("PPR_DELAY_SCALE", "1.0"))
BUCKET_DELAYS = {
    "linkedin": (4.0, 9.0),     # a person clicking through profiles
    "search": (2.0, 5.0),       # per search backend (bing, yahoo, ...) - rotation spreads load
    "web": (1.0, 3.0),          # persona websites, bluesky, google drive, licdn image CDN
    "llm": (1.2, 2.0),          # mistral free tier is ~1 req/s
}
# a short pause after every N hits to the same bucket, so traffic isn't perfectly regular
BUCKET_BREAKS = {
    "linkedin": (25, (30.0, 90.0)),
    "search": (40, (20.0, 60.0)),
}
# on HTTP 999 / 429 / authwall: cool down, doubling each time, reset after a success
BACKOFF_BASE = 2 * 60
BACKOFF_MAX = 30 * 60

# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
SEARCH_BACKENDS = ["bing", "yahoo", "duckduckgo", "brave", "mojeek"]
SEARCH_RESULTS = 10
MAX_QUERIES_PER_PERSONA = 7
STRONG_SNIPPET_SCORE = 0.80     # stop issuing looser queries once a candidate scores this
TOP_K_FETCH = 3                 # candidates that get profile/posts/company evidence
FACE_SWEEP_MAX = 6              # plausible candidates that get a face comparison
# LinkedIn profile pages: "browser" = anonymous real Chrome via Patchright (default; plain
# HTTP clients get fingerprinted to 999 quickly), "http" = curl_cffi only.
LINKEDIN_FETCHER = os.environ.get("PPR_LINKEDIN_FETCHER", "browser")
BROWSER_HEADLESS = os.environ.get("PPR_HEADLESS", "0") == "1"

# decision thresholds on calibrated probability
TAU_ACCEPT = 0.50
TAU_AMBIGUOUS_MARGIN = 0.10

# --offline: answer only from the cache (re-score / calibrate without any network)
OFFLINE = os.environ.get("PPR_OFFLINE", "0") == "1"

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
