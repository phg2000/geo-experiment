#!/usr/bin/env python3
"""
Experiment A — batch harness.

Run the SAME query through the web-search inspector N times, back-to-back, and
save each run's full result JSON. Feeds analyze_variability.py, which measures
how much the search queries and the resulting source sets differ between runs.

Reuses the inspection logic in ../ws-inspector-web/api/_core.py (single source
of truth for parsing). Uses OpenAI background mode + polling, so a long run
never hits any single-request timeout. Defaults are left untouched
(temperature, search_context_size=medium) so we measure the real product
behavior — the run-to-run variance IS the thing under study.

Run the batch in one tight window to minimize web-index drift, so the variance
you see is the model's stochasticity rather than the web changing underneath.

Usage:
    export OPENAI_API_KEY=sk-...
    python run_batch.py "what's the best supplement for building muscle?" -n 15
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "ws-inspector-web" / "api"
sys.path.insert(0, str(CORE_DIR))
import _core  # noqa: E402  (single source of truth for the API call + parsing)

POLL_INTERVAL_S = 3
MAX_WAIT_S = 9 * 60  # OpenAI retains background results ~10 min


def slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n] or "query"


def geocode_location(name: str):
    """Resolve a place name to a location dict via Open-Meteo's geocoding API."""
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": name, "count": 1, "language": "en", "format": "json"})
    with urllib.request.urlopen(url, timeout=15) as r:
        data = json.load(r)
    res = data.get("results") or []
    if not res:
        return None
    g = res[0]
    label = ", ".join(x for x in [g.get("name"), g.get("admin1"), g.get("country")] if x)
    return {
        "city": g.get("name"), "region": g.get("admin1") or "",
        "country": g.get("country_code") or "", "country_name": g.get("country") or "",
        "timezone": g.get("timezone") or "", "label": label,
    }


def run_one(query: str, mode: str = "instrumented", location=None, web_search: bool = True):
    """Start a background run and poll until it completes. Returns (id, result)."""
    started = _core.start_inspection(query, mode=mode, location=location, web_search=web_search)
    rid = started.get("id")
    if not rid:
        raise RuntimeError(f"No run id returned: {started}")
    t0 = time.time()
    while True:
        out = _core.poll_inspection(rid, query, mode=mode, web_search=web_search)
        status = out.get("status")
        if status == "completed":
            return rid, out["result"]
        if status not in ("queued", "in_progress"):
            raise RuntimeError(f"Run {rid} ended with status '{status}': {out.get('error')}")
        if time.time() - t0 > MAX_WAIT_S:
            raise TimeoutError(f"Run {rid} exceeded {MAX_WAIT_S}s")
        time.sleep(POLL_INTERVAL_S)


def main():
    ap = argparse.ArgumentParser(description="Run one query N times and save each result.")
    ap.add_argument("query")
    ap.add_argument("-n", "--runs", type=int, default=15, help="number of runs (default 15)")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="seconds to wait between runs (default 0; keep small to limit index drift)")
    ap.add_argument("--outdir", default=None, help="output dir (default experiments/runs/<ts>_<slug>)")
    ap.add_argument("--mode", choices=["instrumented", "chatgpt", "bare"], default="instrumented",
                    help="prompt mode: instrumented (+source dump), chatgpt (representative system "
                         "prompt), or bare (query only). Default instrumented.")
    ap.add_argument("--bare", action="store_true", help="alias for --mode bare")
    ap.add_argument("--geocode", default=None, metavar="CITY",
                    help="geo-target the web search to this place (resolved via Open-Meteo)")
    ap.add_argument("--no-web-search", action="store_true",
                    help="disable the web_search tool (answer from training data only)")
    args = ap.parse_args()
    mode = "bare" if args.bare else args.mode
    web_search = not args.no_web_search

    location = None
    if args.geocode:
        try:
            location = geocode_location(args.geocode)
            print(f"location: {location['label'] if location else '(no match)'}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] geocoding failed ({e}); proceeding with no location.", file=sys.stderr)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("ERROR: OPENAI_API_KEY is not set.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = Path(args.outdir) if args.outdir else (
        Path(__file__).resolve().parent / "runs" / f"{ts}_{slug(args.query)}")
    base.mkdir(parents=True, exist_ok=True)

    meta = {
        "query": args.query, "model": _core.MODEL, "requested_runs": args.runs,
        "mode": mode, "web_search": web_search, "location": location,
        "started_at": ts, "interval_s": args.interval, "runs": [],
    }
    loc_str = f", location={location['label']}" if location else ""
    ws_str = "" if web_search else ", web_search=OFF"
    print(f"Batch -> {base}   ({args.runs} runs, mode={mode}{ws_str}{loc_str}) of: {args.query!r}")

    for i in range(1, args.runs + 1):
        print(f"[{i}/{args.runs}] starting…", flush=True)
        rec = {"index": i}
        try:
            t0 = time.time()
            rid, result = run_one(args.query, mode=mode, location=location, web_search=web_search)
            elapsed = round(time.time() - t0, 1)
            (base / f"{i:02d}.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str))
            rec.update(
                id=rid, ok=True, elapsed_s=elapsed,
                n_queries=len(result.get("search_queries") or []),
                n_api_sources=len(result.get("api_sources") or []),
                n_citations=len(result.get("citations") or []),
            )
            print(f"     ok in {elapsed}s — {rec['n_queries']} queries, "
                  f"{rec['n_api_sources']} sources, {rec['n_citations']} citations")
        except Exception as e:
            rec.update(ok=False, error=f"{type(e).__name__}: {e}")
            print(f"     FAILED: {rec['error']}")
        meta["runs"].append(rec)
        # Write meta after every run so a crash still leaves a usable batch.
        (base / "batch_meta.json").write_text(json.dumps(meta, indent=2, default=str))
        if args.interval and i < args.runs:
            time.sleep(args.interval)

    ok = sum(1 for r in meta["runs"] if r.get("ok"))
    print(f"\nDone: {ok}/{args.runs} succeeded.")
    print(f"Analyze with:\n  python analyze_variability.py {base}")


if __name__ == "__main__":
    main()
