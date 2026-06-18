#!/usr/bin/env python3
"""
Experiment A — analysis.

Given a batch directory from run_batch.py, measure how much the search queries
and the resulting source sets differ across the repeated runs.

Per channel (search queries / runtime consideration set / citations) it reports:
  - set sizes per run (min / mean / max)
  - union vs intersection across all runs
  - a "core" set (appears in EVERY run) vs a "tail" (appears in exactly one)
  - mean pairwise Jaccard similarity (1.0 = identical every run, 0 = disjoint)
  - a frequency histogram: how many items appeared in exactly k of N runs

URLs are normalized (via _core.normalize_url, which also strips utm_*), and
query strings are lowercased/whitespace-collapsed, so trivial differences don't
inflate the variability.

Outputs a printed report, three CSVs (one per channel), and summary.json.

Usage:
    python analyze_variability.py experiments/runs/<ts>_<slug>
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "ws-inspector-web" / "api"
sys.path.insert(0, str(CORE_DIR))
import _core  # noqa: E402  (normalize_url only; no OpenAI import at module load)


def norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def load_runs(d: Path):
    runs = []
    for p in sorted(d.glob("[0-9]*.json")):
        try:
            runs.append((p.name, json.loads(p.read_text())))
        except Exception as e:  # noqa: BLE001
            print(f"  skip {p.name}: {e}", file=sys.stderr)
    return runs


def per_run_url_sets(runs, key):
    """Return (list-of-normalized-url-sets, {normalized: example_original})."""
    sets, disp = [], {}
    for _, r in runs:
        s = set()
        for item in (r.get(key) or []):
            u = item.get("url") if isinstance(item, dict) else item
            n = _core.normalize_url(u)
            if n:
                s.add(n)
                disp.setdefault(n, u)
        sets.append(s)
    return sets, disp


def per_run_query_sets(runs):
    sets, disp = [], {}
    for _, r in runs:
        s = set()
        for q in (r.get("search_queries") or []):
            nq = norm_query(q)
            if nq:
                s.add(nq)
                disp.setdefault(nq, q)
        sets.append(s)
    return sets, disp


def mean_pairwise_jaccard(sets):
    if len(sets) < 2:
        return None
    vals = []
    for a, b in combinations(sets, 2):
        u = a | b
        vals.append(1.0 if not u else len(a & b) / len(u))
    return sum(vals) / len(vals)


def summarize(per_run_sets):
    n = len(per_run_sets)
    sizes = [len(s) for s in per_run_sets]
    union = set().union(*per_run_sets) if per_run_sets else set()
    inter = set(per_run_sets[0]) if per_run_sets else set()
    for s in per_run_sets[1:]:
        inter &= s
    freq = Counter()
    for s in per_run_sets:
        freq.update(s)
    return {
        "n_runs": n,
        "mean_set_size": round(sum(sizes) / n, 2) if n else 0,
        "min_set_size": min(sizes) if sizes else 0,
        "max_set_size": max(sizes) if sizes else 0,
        "union_size": len(union),
        "intersection_size": len(inter),
        "core_count": sum(1 for c in freq.values() if c == n),
        "tail_count": sum(1 for c in freq.values() if c == 1),
        "mean_pairwise_jaccard": mean_pairwise_jaccard(per_run_sets),
        "freq": dict(freq),
    }


def appearance_histogram(freq: dict, n: int):
    """k -> how many distinct items appeared in exactly k of N runs."""
    buckets = Counter(freq.values())
    return {k: buckets.get(k, 0) for k in range(1, n + 1)}


def jacc_str(v):
    return "n/a" if v is None else f"{v:.3f}"


def print_channel(title, s, n):
    print(f"\n## {title}")
    print(f"  set size per run: min {s['min_set_size']} · mean {s['mean_set_size']} · max {s['max_set_size']}")
    print(f"  union across runs:        {s['union_size']}")
    print(f"  intersection (all runs):  {s['intersection_size']}")
    print(f"  core (in every run):      {s['core_count']}")
    print(f"  tail (in exactly 1 run):  {s['tail_count']}")
    print(f"  mean pairwise Jaccard:    {jacc_str(s['mean_pairwise_jaccard'])}")
    hist = appearance_histogram(s["freq"], n)
    print(f"  appeared in k/{n} runs:")
    maxbar = max(hist.values()) if hist and max(hist.values()) else 1
    for k in range(n, 0, -1):
        cnt = hist[k]
        if cnt:
            bar = "█" * max(1, round(20 * cnt / maxbar))
            print(f"    {k:>2}/{n}: {cnt:>3}  {bar}")


def write_freq_csv(path, freq, disp, n):
    rows = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["item", "runs_present", "fraction"])
        for key, cnt in rows:
            w.writerow([disp.get(key, key), cnt, round(cnt / n, 3)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch_dir")
    args = ap.parse_args()
    d = Path(args.batch_dir)
    if not d.is_dir():
        sys.exit(f"Not a directory: {d}")

    runs = load_runs(d)
    n = len(runs)
    if n < 2:
        sys.exit(f"Need >=2 runs to compare; found {n}.")

    meta = {}
    mp = d / "batch_meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text())

    q_sets, q_disp = per_run_query_sets(runs)
    a_sets, a_disp = per_run_url_sets(runs, "api_sources")
    c_sets, c_disp = per_run_url_sets(runs, "citations")

    q_sum, a_sum, c_sum = summarize(q_sets), summarize(a_sets), summarize(c_sets)

    print("=" * 70)
    print("EXPERIMENT A — run-to-run variability")
    print(f"query: {meta.get('query', '(unknown)')}")
    print(f"model: {meta.get('model', '(unknown)')}   runs analyzed: {n}")
    print("=" * 70)
    print_channel("Search queries the model ran", q_sum, n)
    print_channel("Runtime consideration set (api_sources)", a_sum, n)
    print_channel("Citations", c_sum, n)

    # CSVs + summary.json
    write_freq_csv(d / "queries_frequency.csv", q_sum["freq"], q_disp, n)
    write_freq_csv(d / "sources_frequency.csv", a_sum["freq"], a_disp, n)
    write_freq_csv(d / "citations_frequency.csv", c_sum["freq"], c_disp, n)

    def strip_freq(s):
        return {k: v for k, v in s.items() if k != "freq"}

    summary = {
        "query": meta.get("query"),
        "model": meta.get("model"),
        "runs_analyzed": n,
        "search_queries": strip_freq(q_sum),
        "api_sources": strip_freq(a_sum),
        "citations": strip_freq(c_sum),
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print("\nWrote: queries_frequency.csv, sources_frequency.csv, "
          "citations_frequency.csv, summary.json")
    print(f"  in {d}")


if __name__ == "__main__":
    main()
