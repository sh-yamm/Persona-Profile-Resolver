#!/usr/bin/env python3
"""Resolve personas to public LinkedIn profiles with a calibrated confidence.

    python run.py dataset1.json dataset2.json -o out/
    python run.py personas.json --limit 3 --no-face

No LinkedIn login is used. Requests are slow on purpose (see enrich/config.py);
everything is cached in ./cache, so an interrupted run resumes where it stopped.
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="JSON files, each a list of persona objects")
    ap.add_argument("-o", "--out", default="out", help="output directory")
    ap.add_argument("--limit", type=int, default=0, help="only the first N personas")
    ap.add_argument("--delay-scale", type=float, default=None,
                    help="multiply all politeness delays (default 1.0; keep >= 1 for real runs)")
    ap.add_argument("--no-face", action="store_true", help="skip face matching")
    ap.add_argument("--no-retry-pass", action="store_true",
                    help="don't wait out a LinkedIn cool-down to re-fetch profiles that were blocked")
    ap.add_argument("--retry-rounds", type=int, default=3,
                    help="max passes over personas whose profiles could not be fetched (default 3)")
    ap.add_argument("--offline", action="store_true",
                    help="no network at all: re-score purely from the cache")
    ap.add_argument("--browser-fallback", action="store_true",
                    help="retry blocked profile fetches in an anonymous Patchright browser")
    args = ap.parse_args()

    if args.delay_scale is not None:
        os.environ["PPR_DELAY_SCALE"] = str(args.delay_scale)
    if args.browser_fallback:
        os.environ["PPR_BROWSER_FALLBACK"] = "1"
    if args.offline:
        os.environ["PPR_OFFLINE"] = "1"
        args.no_retry_pass = True

    from enrich import config
    from enrich.cache import Cache
    from enrich.linkedin import LinkedInClient
    from enrich.llm import MistralParser
    from enrich.net import Fetcher, log
    from enrich.pipeline import Resolver
    from enrich.search import Searcher

    personas = []
    for f in args.inputs:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        personas += data if isinstance(data, list) else [data]
    if args.limit:
        personas = personas[:args.limit]

    cache = Cache(config.CACHE_DB)
    fetcher = Fetcher(cache)
    face = None
    if not args.no_face:
        from enrich.face import FaceMatcher
        face = FaceMatcher(fetcher, cache)
    llm = MistralParser.maybe(cache, fetcher.limiter)
    log(f"{len(personas)} personas | delay x{config.DELAY_SCALE} | face={'on' if face else 'off'} "
        f"| mistral={'on' if llm else 'off'} | browser-fallback={'on' if config.BROWSER_FALLBACK else 'off'}")
    li = LinkedInClient(fetcher, cache)
    resolver = Resolver(fetcher, cache, li, Searcher(cache, fetcher.limiter), face, llm)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results, t0 = [], time.time()
    try:
        for i, raw in enumerate(personas, 1):
            log(f"[{i}/{len(personas)}]")
            try:
                results.append(resolver.resolve(raw))
            except KeyboardInterrupt:
                raise
            except Exception as e:  # one bad persona never kills the run
                log(f"  !! {type(e).__name__}: {e}")
                results.append({"input": raw, "linkedin_url": None, "confidence": 0.0,
                                "status": "error", "error": f"{type(e).__name__}: {e}"})
            _write(out, results)

        # second pass: personas whose candidates could not be fetched because LinkedIn
        # was cooling down. Search results are cached, so this only costs profile fetches.
        # LinkedIn's guest budget can be ~1 request per cool-down window, so wait the
        # cool-down out before *each* persona. Spend it where a fetch can change the
        # decision: most uncertain first (confidence nearest 0.5), already-certain last.
        for rnd in range(0 if args.no_retry_pass else args.retry_rounds):
            pending = sorted((i for i, r in enumerate(results) if _needs_refetch(r)),
                             key=lambda i: abs(results[i].get("confidence", 0) - 0.5))
            if not pending:
                break
            log(f"retry round {rnd + 1}: {len(pending)} persona(s) still need a profile fetch")
            for i in pending:
                secs = fetcher.limiter.cooling("linkedin")
                if secs:
                    log(f"  waiting {secs / 60:.1f} min for LinkedIn cool-down")
                    time.sleep(secs)
                log(f"[retry {results[i]['input'].get('name')}]")
                try:
                    results[i] = resolver.resolve(personas[i])
                except Exception as e:
                    log(f"  !! {type(e).__name__}: {e}")
                _write(out, results)
    finally:
        li.close()
        _write(out, results)

    by = {}
    for r in results:
        by[r["status"]] = by.get(r["status"], 0) + 1
    log(f"done in {(time.time() - t0) / 60:.1f} min: {by}  -> {out / 'results.json'}")


def _needs_refetch(r: dict) -> bool:
    prof = r.get("profile") or {}
    return bool(r.get("linkedin_url")) and not prof.get("fetched") and r.get("status") != "error"


def _write(out: Path, results: list) -> None:
    (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "linkedin_url", "confidence", "ci_low", "ci_high", "status",
                    "fields_validated", "fields_available"])
        for r in results:
            ci = r.get("confidence_interval") or [None, None]
            fv = r.get("fields_validated") or {}
            w.writerow([r["input"].get("name"), r.get("linkedin_url"), r.get("confidence"),
                        ci[0], ci[1], r.get("status"), fv.get("count"), fv.get("available")])


if __name__ == "__main__":
    sys.exit(main())
