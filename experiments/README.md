# experiments

Research harnesses built on top of the web-search inspector. They reuse the
parsing/reconciliation logic in `../ws-inspector-web/api/_core.py` as the single
source of truth, and run as plain CLIs (no serverless time limits).

## A — run-to-run variability

**Question:** for the *same* query and model, how much do the search queries —
and therefore the consideration set / citations — differ from one run to the
next?

```bash
export OPENAI_API_KEY=sk-...
python run_batch.py "what's the best supplement for building muscle?" -n 15
python analyze_variability.py runs/<ts>_<slug>
```

`run_batch.py` runs the query N times (background mode + polling, so long runs
don't time out) and saves each run's full result JSON plus `batch_meta.json`.
`analyze_variability.py` then reports, for each channel (search queries / runtime
`api_sources` / citations):

- set size per run (min / mean / max)
- union vs intersection across all runs
- **core** (appears in every run) vs **tail** (appears in exactly one)
- **mean pairwise Jaccard** (1.0 = identical every run, 0 = disjoint)
- a histogram of how many items appeared in exactly k of N runs

It also writes `queries_frequency.csv`, `sources_frequency.csv`,
`citations_frequency.csv`, and `summary.json` for your own plotting.

URLs are normalized (`_core.normalize_url`, which strips `utm_*`); query strings
are lowercased and whitespace-collapsed — so trivial differences don't inflate
the apparent variability.

### Method notes / caveats

- **Defaults are untouched** (temperature 1, `search_context_size: medium`), so
  the variance measured is the real product behavior, not an artifact of our
  parameters. That run-to-run variance is the thing under study.
- **Index drift vs model stochasticity.** Repeated runs can differ because the
  model sampled differently *or* because the live web/Bing index changed between
  runs. Run a batch in one tight window (`--interval 0`) to minimize drift, so
  what you see is mostly the model. To *measure* drift instead, re-run the same
  batch hours/days later and compare the two batches' core/union.
- **Caching.** Responses are independent jobs; OpenAI prompt-caching affects
  token cost, not which searches run, so it doesn't bias the comparison.
- Each run is a billable `gpt-5.5` + web-search call. N=15 ≈ 15 runs of cost.

## B — Bing reproduction & retrieval cutoff (deferred)

**Question:** if you run the model's search queries on Bing, do you get the same
sources, and after how many results does the consideration set cut off?

Not built yet. Key constraint discovered: the **direct Bing Web Search API was
retired (HTTP 410) on 2025-08-11**, so this needs a third-party SERP provider
(e.g. SerpAPI's Bing engine) or an independent index (Brave), plus careful
handling of: OpenAI re-ranks on top of whatever backend it uses (so rank-match
is approximate), attribution is ambiguous across multiple queries (use min rank),
and the depth is likely governed by `search_context_size` (the cleanest cutoff
experiment sweeps low/medium/high). See the chat design notes when picking this
up.
