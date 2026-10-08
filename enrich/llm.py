"""Optional Mistral (free tier) helper for messy free-text intros.

Enabled only when MISTRAL_API_KEY is set. The rule-based parser always runs; the LLM
only adds companies/titles/country it finds. Results are cached per intro.
"""

import json

from curl_cffi import requests as cffi

from . import config
from .net import log

PROMPT = """Extract structured facts from this short professional bio/intro.
Return JSON with keys:
  "companies": list of organisation names the person currently works at or founded (no tools/products they merely use),
  "titles": list of job titles/roles,
  "country": ISO-3166 alpha-2 country code if the location is stated, else null.
Do not guess. Intro: {intro}"""


class MistralParser:
    URL = "https://api.mistral.ai/v1/chat/completions"

    def __init__(self, cache, limiter):
        self.cache = cache
        self.limiter = limiter

    @classmethod
    def maybe(cls, cache, limiter):
        return cls(cache, limiter) if config.MISTRAL_API_KEY else None

    def parse_intro(self, intro: str) -> dict | None:
        hit = self.cache.get_json("llm", intro)
        if hit is not None:
            return hit
        self.limiter.wait("llm")
        try:
            r = cffi.post(self.URL, timeout=30, headers={
                "Authorization": f"Bearer {config.MISTRAL_API_KEY}",
                "Content-Type": "application/json"},
                json={"model": config.MISTRAL_MODEL, "temperature": 0,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": PROMPT.format(intro=intro)}]})
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
        except Exception as e:
            log(f"  [llm] mistral failed: {e}")
            return None
        finally:
            self.limiter.done("llm")
        self.cache.set_json("llm", intro, data)
        return data
