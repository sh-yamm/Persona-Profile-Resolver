"""Fellegi–Sunter style scoring + calibration + confidence intervals.

score  = prior + Σ w(field, level)  + name-collision penalty        (log-odds, natural log)
prob   = σ(a · score + b)        (a, b) = Platt calibration fitted on labelled runs
CI     = 5th–95th percentile of prob under (i) ±35% multiplicative noise on every
         field weight and (ii) bootstrap resamples of the Platt fit when available.

The weights are log(m/u): how much more likely a level is for the true person than for
a random same-name candidate. They were set from first principles and can be
re-learned with `calibrate.py` once ground truth exists.
"""

import json
import math
import random

import numpy as np

from . import config

PRIOR = -5.0

WEIGHTS = {
    "name": {"full": 5.0, "partial": 2.5, "first_initial": 2.5, "first_only": 1.0,
             "last_only": -1.0, "mismatch": -15.0},   # coworkers share everything but the name
    "company": {"strong": 4.5, "weak": 1.8, "mismatch": -1.2},
    "domain": {"match": 5.0, "mismatch": -0.5},
    "title": {"strong": 2.0, "weak": 0.8, "mismatch": -0.6},
    "location": {"country_stated": 2.0, "country_tz": 1.4, "region_tz": 0.5,
                 "mismatch_stated": -2.5, "mismatch_tz": -1.5},
    "face": {"strong": 6.0, "likely": 3.0, "unclear": 0.0, "mismatch": -2.5},
    "social": {"match": 6.0, "handle_name": 3.5},
    "keywords": {"high": 1.5, "some": 0.6, "none": -0.3},
    "industry": {"match": 1.2, "none": -0.4},
    "source": {"persona_link": 3.0, "team_page": 1.0, "tight_top": 0.6, "search": 0.0,
               "slug_guess": -0.5},
}
NOISE = 0.35
CALIB_PATH = config.MODELS_DIR / "calibration.json"


def _load_calibration():
    if CALIB_PATH.exists():
        try:
            d = json.loads(CALIB_PATH.read_text())
            return d.get("a", 1.0), d.get("b", 0.0), d.get("bootstrap", [])
        except Exception:
            pass
    return 1.0, 0.0, []


A, B, BOOT = _load_calibration()


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(x, 40), -40)))


def contributions(levels: dict) -> dict:
    return {f: WEIGHTS[f][lv] for f, lv in levels.items()
            if lv is not None and f in WEIGHTS and lv in WEIGHTS[f]}


def collision_penalty(n_same_name: int) -> float:
    """Common names: several distinct profiles share the full name -> less name evidence."""
    return -0.9 * math.log2(1 + max(n_same_name, 0))


def raw_score(levels: dict, n_same_name: int = 0) -> float:
    return PRIOR + sum(contributions(levels).values()) + collision_penalty(n_same_name)


def probability(score: float) -> float:
    return sigmoid(A * score + B)


def interval(levels: dict, n_same_name: int, n: int = 400, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    contrib = contributions(levels)
    pen = collision_penalty(n_same_name)
    probs = []
    for i in range(n):
        s = PRIOR + pen + sum(w * math.exp(rng.gauss(0, NOISE)) for w in contrib.values())
        a, b = BOOT[i % len(BOOT)] if BOOT else (A, B)
        probs.append(sigmoid(a * s + b))
    lo, hi = np.percentile(probs, [5, 95])
    point = probability(PRIOR + pen + sum(contrib.values()))
    return round(min(float(lo), point), 3), round(max(float(hi), point), 3)


def fields_validated(levels: dict) -> dict:
    """How many comparable fields agree — the 'number of different fields validated'."""
    comparable = {f: lv for f, lv in levels.items() if lv is not None and f != "source"}
    agreed = [f for f, lv in comparable.items() if WEIGHTS.get(f, {}).get(lv, 0) > 0]
    disagreed = [f for f, lv in comparable.items() if WEIGHTS.get(f, {}).get(lv, 0) < 0]
    return {"count": len(agreed), "available": len(comparable),
            "agreed": agreed, "disagreed": disagreed}
