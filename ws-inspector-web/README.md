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
    inspect.py       # Vercel Python serverless function  -> POST /api/inspect
    _core.py         # shared parsing/reconciliation logic (underscore = not a route)
  requirements.txt   # openai
  vercel.json        # maxDuration = 60s
```

The browser posts `{query, password}` to `/api/inspect`; the function calls
OpenAI's Responses API with the hosted `web_search` tool, parses the response,
and returns the structured result as JSON. Nothing is persisted server-side
(serverless has no writable disk) — downloads are generated client-side.

## Deploy (fastest path)

1. Push this repo to GitHub (already done if you're reading this on the branch).
2. In Vercel: **Add New → Project → Import** your repo.
3. Set **Root Directory** to `ws-inspector-web`.
4. Add environment variables (Project → Settings → Environment Variables):
   - `OPENAI_API_KEY` — **required**. Your billable OpenAI key. Never commit it.
   - `APP_PASSWORD` — **strongly recommended** (see below).
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
- **Timeouts:** `maxDuration` is 60s (the Vercel Hobby max). A slow web-search
  run can exceed that and return a 504 — just retry, or move to a plan with a
  higher limit. There's no streaming here; it's a single request/response.
