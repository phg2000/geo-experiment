# ws-inspector — ChatGPT web-search "raw run" inspector

A dead-simple Python CLI. You give it one query. It calls OpenAI's **Responses
API** with the hosted **`web_search`** tool, then dumps ALL the raw data from
that single run:

- the search queries the model ran
- the final answer text
- the structured **citations** (ground truth, from annotations)
- the model's self-reported **consideration set** (every source it claims it had
  in context, cited or not — **unverified**)
- token usage
- the full raw response object

It prints everything to the terminal **and** saves a timestamped `.json` + `.md`
into `runs/`.

## Setup

```bash
cd ws-inspector
pip install -r requirements.txt
export OPENAI_API_KEY="sk-..."     # required; never hardcoded
```

## Run

```bash
python inspect.py "what's the best supplement for building muscle?"
```

One query → one run. No loops, no batches, no analysis layer. Outputs land in
`runs/<timestamp>.json` and `runs/<timestamp>.md`.

## Verified model string

- **Model used:** `gpt-5.5` (constant `MODEL` at the top of `inspect.py`)
- **Web-search tool type:** `{"type": "web_search"}` — the current/recommended
  hosted tool. The older `web_search_preview` is the legacy variant and is not
  used here.
- **Include field:** `include=["web_search_call.action.sources"]` — returns the
  runtime's list of consulted source URLs (the consideration set).
- **Verified against OpenAI docs on:** **2026-06-18**

All three were checked against OpenAI's web-search guide and models pages at
build time. If OpenAI rotates the flagship model, update the `MODEL` constant —
the tool type and include strings are independent of the model.

## What each section means

There are **three tiers of source data**, in descending order of trust:

| Section | Source | Trust level |
| --- | --- | --- |
| Search queries | `web_search_call` items, `action.query` | factual (what the model issued) |
| Final answer | the `message` item's `output_text`, before the sources marker | factual |
| **Citations** | the answer text's `url_citation` annotations | **GROUND TRUTH** (cited) |
| **Consideration set** | `web_search_call.action.sources`, via `include=[...]` | **RUNTIME-REPORTED** — actual URLs the tool surfaced, cited or not; cannot be confabulated by the model |
| **Self-reported sources** | a `=== SOURCES IN CONTEXT ===` block the model is asked to append | **MODEL-REPORTED, UNVERIFIED** — its unique value is the verbatim excerpt text the API doesn't return |
| Reconciliation | 3-way normalized-URL set comparison of the above | derived |
| Usage | `response.usage` | factual |

**Why three tiers?** Citations tell you what the answer *cited*. The runtime
consideration set (`action.sources`) tells you what web search *actually
surfaced* to the model — independent of the model, so it can't be faked. The
self-report is the only channel that carries the **passage text** the model
read, but it's unverifiable on its own. Cross-checking the three is the point.

The 3-way reconciliation makes the gaps explicit:

- **consulted_not_cited** (`A − C`) — the runtime surfaced it but the answer
  didn't cite it. These are the genuine *retrieved-but-not-cited* sources.
- **cited_not_in_api** (`C − A`) — cited but missing from the runtime list.
  Unexpected; flagged.
- **self_report_corroborated** (`S ∩ A`) — the model listed it *and* the runtime
  confirms it was consulted. Its excerpt text is now plausible.
- **self_report_uncorroborated** (`S − A`) — the model claims a source the
  runtime never reported. **Likely confabulated** (or paraphrased past URL
  recognition). The runtime channel lets you catch this — pure self-report
  could not.

## Limitations (read this)

State plainly what you are and are NOT seeing:

- **The API exposes the consulted-URL set, but NOT the full SERP / candidate
  pool.** `web_search_call.action.sources` gives you the URLs web search
  surfaced *to the model* — a real consideration set, often larger than the
  citations. It does **not** give you the pages that were retrieved, ranked, and
  filtered out *upstream* of the model. So "consideration set" here means "what
  reached the model," not "everything web search looked at."
- **`action.sources` gives URLs/titles, not passage text.** To see the actual
  text the model read per source you still depend on the model's self-report,
  which is unverifiable — hence the third tier.
- **The self-reported sources are bounded by the re-ranker.** They cannot reveal
  anything filtered out before it reached the model. They are also
  *model-reported*, so they may omit, paraphrase, or confabulate sources, and
  must always be cross-checked against the runtime consideration set and the
  structured citations.
- **Citations are a lower bound on influence.** A source can shape the answer
  without earning an inline `url_citation`. So "not cited" ≠ "not used."
- **Parsing of the self-reported section is best-effort.** Its format is
  model-determined; the tool tries the requested block format, falls back to
  scraping raw URLs, and flags anything partial/malformed rather than crashing.
- For a verified, complete retrieval pool you would need your **own retrieval
  setup** (own search + own re-ranking, with full logging). That is out of scope
  here — this tool inspects the *hosted* web-search path only.

## Files

```
ws-inspector/
  inspect.py          # the CLI
  runs/               # output: <timestamp>.json + <timestamp>.md per run
  requirements.txt
  README.md
```
