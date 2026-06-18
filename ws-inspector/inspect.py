#!/usr/bin/env python3
"""
ChatGPT web-search "raw run" inspector.

Give it one query. It calls OpenAI's Responses API with the hosted web_search
tool, then shows ALL the raw data from that single run:

  - the search queries the model ran
  - the final answer text
  - the structured citations (GROUND TRUTH, from annotations)
  - the model's self-reported consideration set (UNVERIFIED, parsed from a
    section the model appends to its answer)
  - token usage
  - the full raw response object

Output is printed to the terminal AND saved as a timestamped .json + .md in runs/.

Usage:
    python inspect.py "what's the best supplement for building muscle?"
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# -- Model + tool config -------------------------------------------------------
# VERIFIED against OpenAI docs on 2026-06-18:
#   - hosted web-search tool type string: "web_search"  (newer/recommended;
#     "web_search_preview" is the legacy variant)
#   - current flagship general-purpose model with web_search support: "gpt-5.5"
# See README.md for the verification note.
MODEL = "gpt-5.5"
WEB_SEARCH_TOOL = {"type": "web_search"}

# `include` fields requested from the Responses API. action.sources returns the
# actual list of source URLs the web_search tool consulted (often more than the
# citations). Verified against OpenAI docs on 2026-06-18.
INCLUDE_FIELDS = ["web_search_call.action.sources"]

# Marker the model is instructed to emit before its self-reported source dump.
SOURCES_MARKER = "=== SOURCES IN CONTEXT ==="

# -- Retry config --------------------------------------------------------------
MAX_RETRIES = 5
BASE_BACKOFF = 2.0  # seconds; doubles each retry: 2, 4, 8, 16, 32

RUNS_DIR = Path(__file__).resolve().parent / "runs"


# =============================================================================
# Prompt construction
# =============================================================================

# Kept clearly separated from the user's query. This asks the model to append a
# structured dump of every source it had in context, cited or not.
SOURCES_INSTRUCTION = f"""\
You have access to a web_search tool. Use it to research the user's question, \
then answer it normally and conversationally.

After answering, output a section titled exactly:

{SOURCES_MARKER}

In that section, list EVERY source passage and URL that was provided to you in \
context for this query, INCLUDING ones you did NOT cite in your answer. For each \
source use this exact, repeating block format so it can be parsed:

- URL: <the full url>
  TITLE: <the page title if available, else "(unknown)">
  EXCERPT: <the passage text you were given for this source, quoted VERBATIM. \
Copy it exactly as it appeared in your context — do not summarize, paraphrase, \
or shorten it. If it is long, include as much as you can.>

Rules for this section:
- Do NOT invent or guess URLs. Only list sources actually present in your context.
- If you cannot recover a URL exactly, write "URL: (could not recover exactly)" \
for that entry rather than approximating.
- List sources you saw but did not cite, too — that is the whole point.
- If you genuinely had no sources in context, write "(none)" under the title."""


def build_input(user_query: str):
    """Return the Responses API `input` list, query and instructions separated."""
    return [
        {
            "role": "system",
            "content": SOURCES_INSTRUCTION,
        },
        {
            "role": "user",
            "content": user_query,
        },
    ]


# =============================================================================
# API call with retry/backoff
# =============================================================================


def call_openai(client, user_query: str):
    """Call the Responses API with web_search, retrying transient failures."""
    # Import here so a missing dependency produces a clear message at runtime.
    from openai import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    transient = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            return client.responses.create(
                model=MODEL,
                tools=[WEB_SEARCH_TOOL],
                input=build_input(user_query),
                # Ask the runtime to return the actual URLs the web_search tool
                # surfaced to the model (the "consideration set"). This is
                # runtime-reported, NOT model-narrated — the trustworthy middle
                # tier between cited annotations and the model's self-report.
                include=INCLUDE_FIELDS,
            )
        except transient as err:  # rate limit / transient server / network
            last_err = err
            if attempt == MAX_RETRIES - 1:
                break
            backoff = BASE_BACKOFF * (2 ** attempt)
            print(
                f"[retry] {type(err).__name__} on attempt {attempt + 1}/{MAX_RETRIES}; "
                f"sleeping {backoff:.0f}s...",
                file=sys.stderr,
            )
            time.sleep(backoff)
    raise SystemExit(f"OpenAI request failed after {MAX_RETRIES} attempts: {last_err}")


# =============================================================================
# Parsing the response
# =============================================================================


def response_to_dict(response) -> dict:
    """Get a plain-dict view of the response object, robustly across SDK versions."""
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                pass
    # Last resort: round-trip through JSON.
    if hasattr(response, "model_dump_json"):
        return json.loads(response.model_dump_json())
    raise TypeError("Could not convert response object to dict.")


def extract_search_queries(output: list) -> list:
    """Pull search query strings from every web_search_call item, in order.

    An item's action may carry a singular `query`, a plural `queries` list, or
    BOTH — so collect from both rather than treating them as exclusive. Within a
    single item, de-dupe (a singular `query` often just repeats `queries[0]`);
    across items, duplicates are preserved (a repeated search is informative).
    """
    queries = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        if not isinstance(action, dict):
            continue
        local = []
        q = action.get("query")
        if isinstance(q, str) and q.strip():
            local.append(q)
        if isinstance(action.get("queries"), list):
            for x in action["queries"]:
                if isinstance(x, str) and x.strip():
                    local.append(x)
        seen = set()
        for x in local:
            if x not in seen:
                seen.add(x)
                queries.append(x)
    return queries


def extract_api_sources(output: list) -> list:
    """Runtime-reported consideration set: action.sources[] from every
    web_search_call item, in order, de-duplicated by URL (first wins).

    These come from the tool runtime (requested via include=
    ["web_search_call.action.sources"]) — NOT narrated by the model — so they
    are a reliable list of what web search actually surfaced. Note: this gives
    URLs/titles, not the passage text the model read.
    """
    sources = []
    seen = set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        if not isinstance(action, dict):
            continue
        for src in action.get("sources", []) or []:
            if not isinstance(src, dict):
                # Some shapes may surface a bare URL string.
                if isinstance(src, str) and src not in seen:
                    seen.add(src)
                    sources.append({"url": src, "title": None, "type": "url"})
                continue
            url = src.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "url": url,
                    "title": src.get("title"),
                    "type": src.get("type", "url"),
                }
            )
    return sources


def find_message_item(output: list):
    """Return the first assistant message item, or None.

    IMPORTANT: search calls come first in output; never assume output[0].
    """
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            # Only assistant messages carry the answer + annotations.
            if item.get("role", "assistant") == "assistant":
                return item
    return None


def extract_message_text_and_annotations(message: dict):
    """From a message item, return (full_text, annotations list).

    Concatenates all output_text parts; collects their annotations.
    """
    if not message:
        return "", []
    full_text_parts = []
    annotations = []
    for part in message.get("content", []) or []:
        if not isinstance(part, dict):
            continue
        # output_text parts carry the text + citation annotations.
        if part.get("type") in ("output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                full_text_parts.append(text)
            for ann in part.get("annotations", []) or []:
                if isinstance(ann, dict):
                    annotations.append(ann)
    return "".join(full_text_parts), annotations


def extract_citations(annotations: list) -> list:
    """Ground-truth citations: every url_citation annotation."""
    citations = []
    for ann in annotations:
        if ann.get("type") != "url_citation":
            continue
        citations.append(
            {
                "url": ann.get("url"),
                "title": ann.get("title"),
                "start_index": ann.get("start_index"),
                "end_index": ann.get("end_index"),
            }
        )
    return citations


def split_answer_and_sources(full_text: str):
    """Split the message text on the SOURCES marker.

    Returns (answer_text_before_marker, sources_block_or_None).
    """
    if not full_text:
        return "", None
    idx = full_text.find(SOURCES_MARKER)
    if idx == -1:
        return full_text.strip(), None
    answer = full_text[:idx].strip()
    sources_block = full_text[idx + len(SOURCES_MARKER):].strip()
    return answer, sources_block


# Regex for any http(s) URL.
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def parse_self_reported_sources(sources_block):
    """Best-effort parse of the model's appended sources section.

    The exact formatting is model-determined, so this is tolerant:
      1. Primary path: parse the requested "- URL: / TITLE: / EXCERPT:" blocks.
      2. Fallback: if that yields nothing, scrape any URLs found in the block.

    Returns (sources_list, flags_dict). Never raises.
    """
    flags = {
        "sources_section_present": sources_block is not None,
        "parse_method": None,
        "partial_or_malformed": False,
        "notes": [],
    }

    if sources_block is None:
        flags["partial_or_malformed"] = True
        flags["notes"].append("SOURCES section marker not found in model output.")
        return [], flags

    stripped = sources_block.strip()
    if not stripped or stripped.lower().startswith("(none)"):
        flags["parse_method"] = "explicit-none"
        flags["notes"].append("Model reported no sources in context.")
        return [], flags

    # --- Primary: labelled blocks --------------------------------------------
    sources = _parse_labelled_blocks(sources_block)
    if sources:
        flags["parse_method"] = "labelled-blocks"
        # Sanity check: did we miss URLs the regex can see?
        all_urls = set(_URL_RE.findall(sources_block))
        parsed_urls = {s["url"] for s in sources if s["url"] and s["url"].startswith("http")}
        missed = all_urls - parsed_urls
        if missed:
            flags["partial_or_malformed"] = True
            flags["notes"].append(
                f"{len(missed)} URL(s) present in the section were not captured "
                f"by block parsing; output may be malformed."
            )
        return sources, flags

    # --- Fallback: scrape URLs -----------------------------------------------
    urls = _URL_RE.findall(sources_block)
    if urls:
        flags["parse_method"] = "url-scrape-fallback"
        flags["partial_or_malformed"] = True
        flags["notes"].append(
            "Could not parse the requested block format; fell back to scraping "
            "raw URLs. Titles/excerpts unavailable."
        )
        # De-dupe preserving order.
        seen = set()
        out = []
        for u in urls:
            u = u.rstrip(".,);")
            if u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "title": None, "excerpt": None})
        return out, flags

    # --- Nothing usable ------------------------------------------------------
    flags["parse_method"] = "none"
    flags["partial_or_malformed"] = True
    flags["notes"].append(
        "SOURCES section present but no URLs or parseable entries were found."
    )
    return [], flags


def _parse_labelled_blocks(block: str) -> list:
    """Parse repeating 'URL: / TITLE: / EXCERPT:' entries from the section."""
    sources = []
    current = None

    def flush():
        nonlocal current
        if current and (current.get("url") or current.get("title") or current.get("excerpt")):
            sources.append(current)
        current = None

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip a leading list marker like "- ", "* ", "1. ", "• ".
        line = re.sub(r"^[-*•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)

        m = re.match(r"(?i)^url\s*[:\-]\s*(.*)$", line)
        if m:
            # New entry starts at each URL line.
            flush()
            val = m.group(1).strip()
            current = {"url": val or None, "title": None, "excerpt": None}
            continue

        m = re.match(r"(?i)^title\s*[:\-]\s*(.*)$", line)
        if m and current is not None:
            current["title"] = m.group(1).strip() or None
            continue

        m = re.match(r"(?i)^excerpt\s*[:\-]\s*(.*)$", line)
        if m and current is not None:
            current["excerpt"] = m.group(1).strip() or None
            continue

        # Continuation line for an excerpt (wrapped text).
        if current is not None and current.get("excerpt"):
            current["excerpt"] = (current["excerpt"] + " " + line).strip()

    flush()

    # Normalise the "could not recover" sentinel into a None url + note.
    for s in sources:
        if s["url"] and not s["url"].lower().startswith("http"):
            # Keep the model's text (e.g. "(could not recover exactly)") visible.
            s["url_unrecoverable_note"] = s["url"]
            s["url"] = None
    return sources


# =============================================================================
# Reconciliation
# =============================================================================


def normalize_url(url):
    """Normalise a URL for set comparison: lowercase host/scheme, drop fragment,
    drop trailing slash, drop common tracking params. Best-effort, never raises."""
    if not url or not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower() or "https"
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/")
        # Drop utm_* and a few common tracking params.
        q = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
            and k.lower() not in {"ref", "fbclid", "gclid"}
        ]
        query = urlencode(sorted(q))
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url.strip().lower().rstrip("/")


def _url_index(items: list) -> dict:
    """Map normalized-URL -> original URL (first occurrence wins)."""
    idx = {}
    for it in items:
        n = normalize_url(it.get("url"))
        if n:
            idx.setdefault(n, it.get("url"))
    return idx


def reconcile(citations: list, api_sources: list, self_reported: list) -> dict:
    """Three-way URL set comparison across the three trust tiers:

      C = citations          (GROUND TRUTH — cited in the answer)
      A = api_sources         (RUNTIME-REPORTED — consulted, cited or not)
      S = self_reported       (MODEL-REPORTED — UNVERIFIED)

    The interesting buckets:
      - consulted_not_cited (A - C): genuine retrieved-but-not-cited sources.
      - cited_not_in_api    (C - A): citation missing from runtime list (flag).
      - self_report_corroborated   (S ∩ A): model's claim backed by the runtime.
      - self_report_uncorroborated (S - A): model claims a source the runtime
        never reported — likely confabulated (or paraphrased past recognition).
    """
    C = _url_index(citations)
    A = _url_index(api_sources)
    S = _url_index(self_reported)
    ck, ak, sk = set(C), set(A), set(S)

    def urls(index, keys):
        return [index[k] for k in sorted(keys)]

    return {
        # Pairwise: citations vs runtime consideration set.
        "cited_and_in_api": urls(C, ck & ak),
        "consulted_not_cited": urls(A, ak - ck),       # the real "considered, uncited"
        "cited_not_in_api": urls(C, ck - ak),          # unexpected; worth flagging
        # Pairwise: model self-report vs runtime consideration set.
        "self_report_corroborated": urls(S, sk & ak),
        "self_report_uncorroborated": urls(S, sk - ak),  # likely confabulated
        # Pairwise: model self-report vs citations (kept for continuity).
        "self_report_matches_citation": urls(S, sk & ck),
        "counts": {
            "citations": len(ck),
            "api_sources": len(ak),
            "self_reported": len(sk),
            "cited_and_in_api": len(ck & ak),
            "consulted_not_cited": len(ak - ck),
            "cited_not_in_api": len(ck - ak),
            "self_report_corroborated": len(sk & ak),
            "self_report_uncorroborated": len(sk - ak),
        },
    }


# =============================================================================
# Usage
# =============================================================================


def extract_usage(raw: dict) -> dict:
    usage = raw.get("usage") or {}
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "raw": usage,
    }


# =============================================================================
# Output: terminal + files
# =============================================================================

LABEL_CITATIONS = "CITATIONS — GROUND TRUTH (structured annotations)"
LABEL_API = "CONSIDERATION SET — RUNTIME-REPORTED (web_search_call.action.sources)"
LABEL_SELF = "SELF-REPORTED SOURCES — MODEL-REPORTED, UNVERIFIED"


def _hr(char="=", n=78):
    return char * n


def print_terminal(result: dict):
    out = []
    out.append(_hr())
    out.append("WEB-SEARCH RAW RUN INSPECTOR")
    out.append(f"model: {result['model']}   |   timestamp: {result['timestamp']}")
    out.append(_hr())

    out.append("\n### QUERY")
    out.append(result["query"])

    out.append("\n### SEARCH QUERIES THE MODEL RAN")
    if result["search_queries"]:
        for i, q in enumerate(result["search_queries"], 1):
            out.append(f"  {i}. {q}")
    else:
        out.append("  (none captured)")

    out.append("\n### FINAL ANSWER TEXT")
    out.append(result["final_text"] or "  (empty)")

    out.append(f"\n### {LABEL_CITATIONS}")
    if result["citations"]:
        for i, c in enumerate(result["citations"], 1):
            out.append(f"  {i}. {c.get('url')}")
            out.append(f"     title: {c.get('title')}")
    else:
        out.append("  (none)")

    out.append(f"\n### {LABEL_API}")
    out.append("  >> URLs the web_search tool actually surfaced (cited or not). "
               "From the runtime, not the model.")
    if result["api_sources"]:
        for i, s in enumerate(result["api_sources"], 1):
            out.append(f"  {i}. {s.get('url')}")
            if s.get("title"):
                out.append(f"     title: {s.get('title')}")
    else:
        out.append("  (none returned — model may not have searched, or the "
                   "include field was unsupported)")

    out.append(f"\n### {LABEL_SELF}")
    out.append("  >> These are the model's CLAIMS about what it saw. Not verified. "
               "Value here is the verbatim excerpt text, which the API does not return.")
    if result["self_reported_sources"]:
        for i, s in enumerate(result["self_reported_sources"], 1):
            url = s.get("url") or s.get("url_unrecoverable_note") or "(no url)"
            out.append(f"  {i}. {url}")
            out.append(f"     title:   {s.get('title')}")
            if s.get("excerpt"):
                out.append(f"     excerpt: {s.get('excerpt')}")
    else:
        out.append("  (none parsed)")
    flags = result["self_reported_flags"]
    if flags.get("partial_or_malformed"):
        out.append(f"  [!] FLAG: {' '.join(flags.get('notes', [])) or 'partial/malformed.'}")

    out.append("\n### RECONCILIATION (3-way, normalized-URL set comparison)")
    rec = result["reconciliation"]
    c = rec["counts"]
    out.append(
        f"  citations={c['citations']}  api_sources={c['api_sources']}  "
        f"self_reported={c['self_reported']}"
    )
    out.append("  -- citations vs runtime consideration set --")
    out.append("  - consulted_not_cited (runtime surfaced it, answer did NOT cite it):")
    for u in rec["consulted_not_cited"] or ["    (none)"]:
        out.append(f"      • {u}" if not u.startswith("    ") else u)
    out.append("  - cited_not_in_api (cited but absent from runtime list — flag):")
    for u in rec["cited_not_in_api"] or ["    (none)"]:
        out.append(f"      • {u}" if not u.startswith("    ") else u)
    out.append("  -- model self-report vs runtime consideration set --")
    out.append("  - self_report_corroborated (runtime confirms the model saw it):")
    for u in rec["self_report_corroborated"] or ["    (none)"]:
        out.append(f"      • {u}" if not u.startswith("    ") else u)
    out.append("  - self_report_UNCORROBORATED (runtime never reported it — LIKELY CONFABULATED):")
    for u in rec["self_report_uncorroborated"] or ["    (none)"]:
        out.append(f"      • {u}" if not u.startswith("    ") else u)

    out.append("\n### TOKEN USAGE")
    u = result["usage"]
    out.append(
        f"  input={u.get('input_tokens')}  output={u.get('output_tokens')}  "
        f"total={u.get('total_tokens')}"
    )

    out.append("\n" + _hr())
    out.append(f"Saved: {result['_json_path']}")
    out.append(f"Saved: {result['_md_path']}")
    out.append(_hr())

    print("\n".join(out))


def build_markdown(result: dict) -> str:
    lines = []
    lines.append(f"# Web-Search Raw Run — {result['timestamp']}")
    lines.append("")
    lines.append(f"- **model:** `{result['model']}`")
    lines.append(f"- **tool:** `web_search` (hosted)")
    lines.append("")
    lines.append("## Query")
    lines.append("")
    lines.append("> " + result["query"].replace("\n", "\n> "))
    lines.append("")

    lines.append("## Search queries the model ran")
    lines.append("")
    if result["search_queries"]:
        for i, q in enumerate(result["search_queries"], 1):
            lines.append(f"{i}. {q}")
    else:
        lines.append("_(none captured)_")
    lines.append("")

    lines.append("## Final answer text")
    lines.append("")
    lines.append(result["final_text"] or "_(empty)_")
    lines.append("")

    lines.append(f"## Citations — GROUND TRUTH (structured annotations)")
    lines.append("")
    lines.append("_These come from the response's `url_citation` annotations. "
                 "They are the verifiable record of what the answer actually cited._")
    lines.append("")
    if result["citations"]:
        for i, c in enumerate(result["citations"], 1):
            lines.append(f"{i}. [{c.get('title') or c.get('url')}]({c.get('url')})")
            lines.append(f"   - url: `{c.get('url')}`")
            lines.append(f"   - char span: {c.get('start_index')}–{c.get('end_index')}")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("## Consideration set — RUNTIME-REPORTED (`web_search_call.action.sources`)")
    lines.append("")
    lines.append("_The actual list of source URLs the web_search tool surfaced to the model "
                 "(cited or not), returned by the runtime via "
                 "`include=[\"web_search_call.action.sources\"]`. Not narrated by the model, "
                 "so it cannot be confabulated — but it gives URLs/titles, **not** the "
                 "passage text the model read._")
    lines.append("")
    if result["api_sources"]:
        for i, s in enumerate(result["api_sources"], 1):
            lines.append(f"{i}. [{s.get('title') or s.get('url')}]({s.get('url')})")
            lines.append(f"   - url: `{s.get('url')}`")
    else:
        lines.append("_(none returned — the model may not have searched, or the "
                     "include field was unsupported)_")
    lines.append("")

    lines.append("## Self-reported sources — MODEL-REPORTED, UNVERIFIED")
    lines.append("")
    lines.append("> ⚠️ **UNVERIFIED.** This section is parsed from text the *model* "
                 "appended to its own answer. It is the model's claim about what was in "
                 "its context — it may omit, paraphrase, or confabulate sources. Its unique "
                 "value is the **verbatim excerpt text** (which the API does not return); "
                 "cross-check its URLs against the runtime consideration set above.")
    lines.append("")
    if result["self_reported_sources"]:
        for i, s in enumerate(result["self_reported_sources"], 1):
            url = s.get("url") or s.get("url_unrecoverable_note") or "(no url)"
            lines.append(f"{i}. **{s.get('title') or '(unknown title)'}**")
            lines.append(f"   - url: `{url}`")
            if s.get("excerpt"):
                lines.append(f"   - excerpt: {s.get('excerpt')}")
    else:
        lines.append("_(none parsed)_")
    lines.append("")
    flags = result["self_reported_flags"]
    if flags.get("partial_or_malformed") or flags.get("notes"):
        lines.append(f"**Parse flags:** method=`{flags.get('parse_method')}`, "
                     f"partial_or_malformed=`{flags.get('partial_or_malformed')}`")
        for n in flags.get("notes", []):
            lines.append(f"- {n}")
        lines.append("")

    lines.append("## Reconciliation (3-way, normalized-URL set comparison)")
    lines.append("")
    lines.append("Three tiers, descending trust: **C** = citations (ground truth), "
                 "**A** = runtime consideration set, **S** = model self-report.")
    lines.append("")
    rec = result["reconciliation"]
    c = rec["counts"]
    lines.append("| metric | count |")
    lines.append("| --- | --- |")
    lines.append(f"| citations (C) | {c['citations']} |")
    lines.append(f"| api/runtime sources (A) | {c['api_sources']} |")
    lines.append(f"| self-reported (S) | {c['self_reported']} |")
    lines.append(f"| consulted_not_cited (A − C) | {c['consulted_not_cited']} |")
    lines.append(f"| cited_not_in_api (C − A) | {c['cited_not_in_api']} |")
    lines.append(f"| self_report_corroborated (S ∩ A) | {c['self_report_corroborated']} |")
    lines.append(f"| self_report_uncorroborated (S − A) | {c['self_report_uncorroborated']} |")
    lines.append("")
    lines.append("**consulted_not_cited** — runtime surfaced it, but the answer did NOT cite it "
                 "(genuine retrieved-but-not-cited sources):")
    for u in rec["consulted_not_cited"] or ["_(none)_"]:
        lines.append(f"- {u}")
    lines.append("")
    lines.append("**cited_not_in_api** — cited in the answer but absent from the runtime source "
                 "list (unexpected; worth flagging):")
    for u in rec["cited_not_in_api"] or ["_(none)_"]:
        lines.append(f"- {u}")
    lines.append("")
    lines.append("**self_report_corroborated** — the model listed it AND the runtime confirms it "
                 "was consulted (its excerpt text is now plausible):")
    for u in rec["self_report_corroborated"] or ["_(none)_"]:
        lines.append(f"- {u}")
    lines.append("")
    lines.append("**self_report_uncorroborated** — the model listed it but the runtime never "
                 "reported it:")
    lines.append("")
    lines.append("> ⚠️ The runtime did not report consulting this URL, yet the model claims it as "
                 "a source. **Likely confabulated** (or paraphrased past URL recognition). This is "
                 "the bucket the runtime channel lets you catch that pure self-report could not.")
    lines.append("")
    for u in rec["self_report_uncorroborated"] or ["_(none)_"]:
        lines.append(f"- {u}")
    lines.append("")

    lines.append("## Token usage")
    lines.append("")
    u = result["usage"]
    lines.append(f"- input: {u.get('input_tokens')}")
    lines.append(f"- output: {u.get('output_tokens')}")
    lines.append(f"- total: {u.get('total_tokens')}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Raw response object is stored in the companion `.json` file under "
                 "`raw_response`._")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Main
# =============================================================================


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('Usage: python inspect.py "your query here"', file=sys.stderr)
        raise SystemExit(2)
    user_query = sys.argv[1]

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print(
            "ERROR: OPENAI_API_KEY is not set. Export it first:\n"
            '    export OPENAI_API_KEY="sk-..."',
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "ERROR: the `openai` package is not installed.\n"
            "    pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client = OpenAI()  # reads OPENAI_API_KEY from env

    print(f"[info] calling Responses API (model={MODEL}, tool=web_search)...",
          file=sys.stderr)
    response = call_openai(client, user_query)

    # --- Parse ----------------------------------------------------------------
    raw = response_to_dict(response)
    output = raw.get("output") or []

    search_queries = extract_search_queries(output)
    api_sources = extract_api_sources(output)
    message = find_message_item(output)
    full_text, annotations = extract_message_text_and_annotations(message)
    citations = extract_citations(annotations)
    final_text, sources_block = split_answer_and_sources(full_text)
    self_reported, sr_flags = parse_self_reported_sources(sources_block)
    reconciliation = reconcile(citations, api_sources, self_reported)
    usage = extract_usage(raw)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RUNS_DIR / f"{timestamp}.json"
    md_path = RUNS_DIR / f"{timestamp}.md"

    result = {
        "timestamp": timestamp,
        "model": MODEL,
        "query": user_query,
        "search_queries": search_queries,
        "final_text": final_text,
        "citations": citations,
        "citations_label": "ground truth (structured annotations)",
        "api_sources": api_sources,
        "api_sources_label": "runtime-reported consideration set (web_search_call.action.sources)",
        "self_reported_sources": self_reported,
        "self_reported_label": "model-reported, UNVERIFIED",
        "self_reported_flags": sr_flags,
        "reconciliation": reconciliation,
        "usage": usage,
        "raw_response": raw,
        "_json_path": str(json_path),
        "_md_path": str(md_path),
    }

    # --- Write files ----------------------------------------------------------
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    with md_path.open("w", encoding="utf-8") as f:
        f.write(build_markdown(result))

    # --- Terminal -------------------------------------------------------------
    print_terminal(result)


if __name__ == "__main__":
    main()
