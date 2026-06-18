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
- **Verified against OpenAI docs on:** **2026-06-18**

Both were checked against OpenAI's web-search guide and models pages at build
time. If OpenAI rotates the flagship model, update the `MODEL` constant — the
tool type string is independent of the model.

## What each section means

| Section | Source | Trust level |
| --- | --- | --- |
| Search queries | `web_search_call` items, `action.query` | factual (what the model issued) |
| Final answer | the `message` item's `output_text`, before the sources marker | factual |
| **Citations** | the answer text's `url_citation` annotations | **GROUND TRUTH** |
| **Self-reported sources** | a `=== SOURCES IN CONTEXT ===` block the model is asked to append | **MODEL-REPORTED, UNVERIFIED** |
| Reconciliation | normalized-URL set comparison of the two above | derived |
| Usage | `response.usage` | factual |

The reconciliation makes the gap explicit:

- **in_both** — the model's self-report is corroborated by a real citation.
- **citation_only** — actually cited, but the model omitted it from its own dump.
- **self_report_only** — the model **claims** it saw a source it didn't cite.
  This could be a genuine retrieved-but-not-cited source, **or** a
  confabulation. **This tool does not resolve which — it only surfaces it.**

## Limitations (read this)

State plainly what you are and are NOT seeing:

- **The API does not expose the retrieved SERP / candidate pool.** You see only
  what reached the model's context — not the full set of pages web search
  considered, ranked, and filtered upstream.
- **The self-reported sources are bounded by the re-ranker.** They cannot reveal
  anything filtered out before it reached the model. They are also
  *model-reported*, so they may omit, paraphrase, or confabulate sources, and
  must always be cross-checked against the structured citations.
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
