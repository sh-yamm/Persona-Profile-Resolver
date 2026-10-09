#!/usr/bin/env python3
"""Fit the confidence calibration from hand-verified labels, and report accuracy.

labels.json:  {"<persona name>": "https://www.linkedin.com/in/<slug>" | null, ...}
              (null = the person has no findable public profile)

    python calibrate.py out/results.json --labels data/labels.json

For every resolved persona, each scored candidate becomes one example
(1 = the labelled profile, 0 = any other candidate). A Platt scaling σ(a·score + b)
is fitted on the Fellegi–Sunter scores, plus 200 bootstrap refits that feed the
per-prediction confidence intervals. Written to models/calibration.json.
"""

import argparse
import json
import random
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

from enrich import scorer
from enrich.search import canonical_profile


def load(results_paths, labels_path):
    labels = {k: (canonical_profile(v) if v else None)
              for k, v in json.loads(Path(labels_path).read_text(encoding="utf-8")).items()}
    rows, per_persona = [], []
    for rp in results_paths:
        for r in json.loads(Path(rp).read_text(encoding="utf-8")):
            name = r["input"].get("name")
            if name not in labels:
                continue
            truth = labels[name]
            per_persona.append((name, truth, r.get("linkedin_url"), r.get("confidence", 0.0)))
            for c in r.get("candidates", []):
                s = scorer.raw_score(c["levels"], c.get("same_name", 0))
                rows.append((s, int(truth is not None and c["url"] == truth)))
    return rows, per_persona


def fit(rows):
    X = np.array([[s] for s, _ in rows])
    y = np.array([t for _, t in rows])
    if len(set(y)) < 2:
        raise SystemExit("need both positive and negative candidates to calibrate")
    # strong L2: with a handful of labelled personas the classes are often separable,
    # and an unregularised fit would become absurdly overconfident
    m = LogisticRegression(C=0.3).fit(X, y)
    return float(m.coef_[0][0]), float(m.intercept_[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--labels", default="data/labels.json")
    ap.add_argument("--dry-run", action="store_true", help="report only, do not write calibration")
    args = ap.parse_args()

    rows, per = load(args.results, args.labels)
    n = len(per)
    correct = sum(1 for _, t, p, _ in per if (t or None) == (p if t else None) or (t is None and p is None))
    found = [x for x in per if x[1] is not None]
    top1 = sum(1 for _, t, p, _ in found if p == t)
    print(f"personas labelled: {n} | with a true profile: {len(found)}")
    print(f"top-1 accuracy (labelled-with-profile): {top1}/{len(found)} = {top1 / max(len(found), 1):.0%}")
    for name, t, p, conf in per:
        mark = "OK " if p == t else "-- "
        print(f"  {mark} {conf:5.2f}  {name:28} pred={p}  truth={t}")

    if not rows:
        return
    a, b = fit(rows)
    rng = random.Random(0)
    boot = []
    for _ in range(200):
        sample = [rows[rng.randrange(len(rows))] for _ in rows]
        try:
            boot.append(list(fit(sample)))
        except SystemExit:
            continue
    # Brier score before / after
    y = np.array([t for _, t in rows])
    s = np.array([x for x, _ in rows])
    before = np.mean((1 / (1 + np.exp(-s)) - y) ** 2)
    after = np.mean((1 / (1 + np.exp(-(a * s + b))) - y) ** 2)
    print(f"\nPlatt a={a:.3f} b={b:.3f} on {len(rows)} candidates | Brier {before:.3f} -> {after:.3f}")
    if not args.dry_run:
        scorer.CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
        scorer.CALIB_PATH.write_text(json.dumps({"a": a, "b": b, "bootstrap": boot,
                                                 "n_candidates": len(rows), "n_personas": n}, indent=1))
        print(f"wrote {scorer.CALIB_PATH}")


if __name__ == "__main__":
    main()
