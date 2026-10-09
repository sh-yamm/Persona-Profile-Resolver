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
# Multiplied by DELAY_SCALE (CLI --delay-scale). Keep LinkedIn slow.
# --------------------------------------------------------------------------- #
DELAY_SCALE = float(os.environ.get("PPR_DELAY_SCALE", "1.0"))
BUCKET_DELAYS = {
    "linkedin": (20.0, 45.0),
    "search": (6.0, 15.0),      # per search backend (bing, yahoo, ...)
    "web": (3.0, 7.0),          # persona websites, bluesky, google drive
    "llm": (1.5, 3.0),          # mistral free tier is ~1 req/s
}
# a longer "coffee break" after every N hits to the same bucket
BUCKET_BREAKS = {
    "linkedin": (15, (120.0, 300.0)),
    "search": (25, (60.0, 150.0)),
}
# on HTTP 999 / 429 / authwall: cool down, doubling each time
BACKOFF_BASE = 5 * 60
BACKOFF_MAX = 60 * 60

# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
SEARCH_BACKENDS = ["bing", "yahoo", "duckduckgo", "brave", "mojeek"]
SEARCH_RESULTS = 10
MAX_QUERIES_PER_PERSONA = 7
STRONG_SNIPPET_SCORE = 0.80     # stop issuing looser queries once a candidate scores this
TOP_K_FETCH = 3                 # full profile fetches per persona
BROWSER_FALLBACK = os.environ.get("PPR_BROWSER_FALLBACK", "0") == "1"

# decision thresholds on calibrated probability
TAU_ACCEPT = 0.50
TAU_AMBIGUOUS_MARGIN = 0.10

MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
