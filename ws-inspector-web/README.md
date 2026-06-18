# ws-inspector-web

A one-page web UI for the [ws-inspector](../ws-inspector) tool, deployable to
**Vercel** with zero servers to manage. Type a query → see the raw anatomy of a
single ChatGPT web-search run: search queries, final answer, citations (ground
truth), the runtime consideration set (`action.sources`), the model's
self-report (unverified), the 3-way reconciliation, and token usage. Download
the `.json` / `.md` from the browser.

Same logic and trust model as the CLI — see [../ws-inspector/README.md](../ws-inspector/README.md)
for the full explanation of the three tiers and limitations.

## Architecture

```
ws-inspector-web/
  index.html         # static frontend (vanilla JS, no build step)
  api/
    inspect.py       # Vercel Python serverless function (start + poll)
    _core.py         # shared parsing/reconciliation logic (underscore = not a route)
  requirements.txt   # openai
  vercel.json        # maxDuration = 60s
```

The run uses **OpenAI background mode**, so no single request waits on the long
web-search job and Vercel's function time limit is never hit:

1. `POST /api/inspect {query, password}` → calls `responses.create(..., background=True)`
   and returns a job `id` immediately.
2. The browser polls `GET /api/inspect?id=…&query=…` every ~3s → the function
   calls `responses.retrieve(id)` and returns the status.
3. When the run is `completed`, the same parse → 3-way reconcile runs and the
   full structured result comes back; the browser renders it and offers downloads.

It's the **same** API call (model, `web_search` tool, `include=action.sources`,
default params) — `background=True` only defers collection, so the output is
identical to a synchronous run. Nothing is persisted server-side (serverless has
no writable disk); OpenAI retains the background result for ~10 minutes, and the
`.json` / `.md` downloads are generated client-side.

### Prompt modes

The **Prompt mode** selector controls the system prompt sent with the query, so
you can trade instrumentation for fidelity to the chat interface:

- **Instrumented** (default) — appends our "list every source" instruction.
  Gives the self-reported tier, but perturbs the model's search/answer behavior.
- **ChatGPT-like** — sends a representative persona prompt (current date, helpful
  assistant, markdown, willingness to search). **Directional, NOT faithful:** the
  real consumer system prompt is proprietary and changes often; this only induces
  a similar register/formatting/tool-use. No source dump.
- **Bare** — only the user query, no system message; the most neutral baseline.

In ChatGPT-like and Bare modes the self-report tier is intentionally empty, but
citations and the runtime consideration set (`action.sources`, a non-perturbing
`include` flag) are unaffected. The final answer is rendered as **markdown**
(headers, bold, tables, links) via `marked` + `DOMPurify`, so it reads like the
interface; if those CDN libs don't load it falls back to plain text.

Note: even ChatGPT-like mode is not the chat interface — the product also has
memory, personalization, account/location context, and possibly a different
model build / tool config that the API doesn't expose. This isolates the effect
of the *system prompt*, not the entire gap.

### Web search toggle

The **Web search** checkbox enables/disables the `web_search` tool. With it off,
the model answers from its training data only — no searches, sources, or
citations — and the `include`/`user_location` are dropped (they reference the
search call). Useful as a parametric-only baseline. Batch CLI: `--no-web-search`.

### Location

The **Location** field geo-targets the run. The selected city is sent two ways,
mirroring how the interface uses location:

- as the web_search tool's `user_location` (`{type: approximate, city, region,
  country, timezone}`) — this actually steers search results (regional
  availability, local recs), in **every** prompt mode; and
- named in the **ChatGPT-like** system prompt's location paragraph.

Cities are resolved with the **Open-Meteo geocoding API** (free, no key) as you
type — the call runs client-side in your browser. If geocoding is unreachable,
free-text still works (it fills the prompt + a city-only `user_location`). The
`user_location` the run actually used is echoed back in each result. The batch
CLI has the same feature via `--geocode "Palo Alto"`.

### Raw API request echo

Each run's output starts with a collapsed **"Raw API request sent"** section
showing exactly what went out — the system prompt (verbatim), the `input`
messages, and the model/tools/`user_location`/`include`/`background` config.
It's the actual payload sent to `responses.create` (echoed from the server, not
reconstructed), so you can verify the prompt and location per run. It's included
in the downloaded JSON too (`_request`).

### Bing comparison matrix (experiment B)

Each completed run has a **"Compare to Bing"** button. It fetches Bing's ranked
results (via SerpAPI's Bing engine, `GET /api/serp`, key server-side) for each of
the run's search queries — geo-matched to the run's `user_location` — and draws a
matrix:

- **columns** = the run's search queries (vertical headers),
- **rows** = Bing rank 1…30,
- **cell (rank, query)** colored by the URL Bing ranked there: **green** if the
  model cited it, **red** if it was in the consideration set (consulted, not
  cited), blank if Bing showed it but the model didn't surface it.

How far down the green/red cells reach is the retrieval **depth/cutoff**, visible
per query. URLs are normalized for matching. Consulted sources that Bing didn't
rank in the top-30 (re-ranking / tenant mismatch / query variants) have no cell
and are counted + listed separately. Results are cached per query in the browser
session; the SerpAPI call honors `APP_PASSWORD` (cost gate). Caveat: OpenAI
re-ranks on top of its backend and a SerpAPI scrape ≠ OpenAI's exact Bing view,
so rank-matching is approximate — clustering near the top is suggestive, not
proof of a hard cutoff.

### Batch / variability mode

Set **Runs > 1** to repeat the same query N times. The browser starts N
background jobs in parallel (staggered slightly to avoid rate-limit bursts) and
polls them all, so N runs take about as long as one. Every run's full result is
shown (collapsible), and once N > 1 a **cross-run analysis** appears at the top:
per-run set sizes, union vs intersection, core (every run) vs tail (one run),
and mean pairwise Jaccard for the search queries, the runtime consideration set,
and the citations — the in-browser equivalent of `experiments/analyze_variability.py`.
"Download all runs (.json)" saves the batch. (For large N or persistent storage,
use the `experiments/` CLI instead.)

## Deploy (fastest path)

1. Push this repo to GitHub (already done if you're reading this on the branch).
2. In Vercel: **Add New → Project → Import** your repo.
3. Set **Root Directory** to `ws-inspector-web`.
4. Add environment variables (Project → Settings → Environment Variables):
   - `OPENAI_API_KEY` — **required**. Your billable OpenAI key. Never commit it.
   - `APP_PASSWORD` — **strongly recommended** (see below).
   - `SERPAPI_KEY` — optional; enables the "Compare to Bing" matrix (experiment B).
     Get one at https://serpapi.com (free ~100 searches/mo).
5. **Deploy.** Done — you get a `*.vercel.app` URL.

No framework preset is needed; Vercel auto-detects the static `index.html` and
the Python function in `api/`.

### Or via CLI

```bash
npm i -g vercel
cd ws-inspector-web
vercel            # follow prompts; set Root Directory to "." when run from here
vercel env add OPENAI_API_KEY
vercel env add APP_PASSWORD
vercel --prod
```

## ⚠️ Protect your credits

A public endpoint that calls *your* `OPENAI_API_KEY` means **anyone who finds
the URL spends your money** (each run is a billable `gpt-5.5` + web-search call).

- Set **`APP_PASSWORD`** to a shared secret. The function then rejects any request
  without it, and the UI shows a password field. This is a light gate, not real
  auth — fine for "just me and a few people."
- For stronger protection, put the deployment behind
  [Vercel Authentication / password protection](https://vercel.com/docs/security/deployment-protection)
  (Pro feature), or keep the project's Preview Protection on.

Leave `APP_PASSWORD` unset only if you genuinely want it open.

## Model / API

- Model: `gpt-5.5`; tool `{"type": "web_search"}`; `include=["web_search_call.action.sources"]`.
- Verified against OpenAI docs **2026-06-18**. To change the model, edit `MODEL`
  in `api/_core.py`.

## Limitations (same as the CLI)

- The runtime exposes the **consulted-URL set**, not the full upstream SERP /
  candidate pool, and not the passage text (that only comes from the model's
  unverified self-report).
- Citations are a lower bound on influence; self-report may omit/paraphrase/
  confabulate — which the 3-way reconciliation is designed to surface.
- **Timeouts:** a slow web-search run can exceed Vercel's function limit and
  return a **504 / FUNCTION_INVOCATION_TIMEOUT**. See "Tuning for the timeout"
  below. There's no streaming here; it's a single request/response.

## The timeout — solved via background mode

Vercel's function duration cap is **60s on Hobby** / up to **800s on Pro**, and
a thorough web-search run can exceed 60s. This app sidesteps that entirely with
OpenAI background mode (see Architecture above): every serverless call returns
in well under a second, so even multi-minute runs work on Hobby. The browser
polls until the job finishes.

We deliberately **do not** lower reasoning effort or `search_context_size` to
fit a time budget — those change the model's search/answer behavior, so the run
would no longer faithfully model what the ChatGPT interface does. The call uses
plain API defaults; background mode makes that affordable.

Notes and limits:

- **Keep the tab open** while it polls. If you close it, the job still completes
  on OpenAI's side, but this UI won't capture the result (it's client-driven).
  The job `id` is shown during the run.
- **~10-minute retention.** OpenAI holds a background result for about 10 min,
  which is the polling window. Download the `.json` / `.md` to keep it.
- For very heavy/repeated runs you can also use the CLI (`../ws-inspector`),
  which has no serverless limits at all.
