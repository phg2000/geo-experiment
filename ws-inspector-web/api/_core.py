"""
Core web-search inspection logic, shared by the Vercel serverless function.

This is a trimmed, file-IO-free port of ws-inspector/inspect.py: same parsing,
same 3-tier trust model (citations / runtime sources / self-report), same 3-way
reconciliation. It returns a plain dict instead of printing or writing files.
"""

import json
import re
import time

# -- Model + tool config (verified against OpenAI docs 2026-06-18) -------------
# Defaults are intentionally left at the API defaults so this run mirrors the
# ChatGPT product as closely as possible. Do NOT add reasoning-effort or
# search_context_size overrides here: they change the model's search/answer
# behavior and would stop this from faithfully modeling what the interface does.
MODEL = "gpt-5.5"
WEB_SEARCH_TOOL = {"type": "web_search"}
INCLUDE_FIELDS = ["web_search_call.action.sources"]
SOURCES_MARKER = "=== SOURCES IN CONTEXT ==="

# Shorter, web-friendly retry budget (long sleeps would eat the function timeout).
MAX_RETRIES = 3
BASE_BACKOFF = 1.5


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
    return [
        {"role": "system", "content": SOURCES_INSTRUCTION},
        {"role": "user", "content": user_query},
    ]


# =============================================================================
# API call
# =============================================================================


def _call(client, user_query: str):
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
                include=INCLUDE_FIELDS,
            )
        except transient as err:
            last_err = err
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(BASE_BACKOFF * (2 ** attempt))
    raise RuntimeError(f"OpenAI request failed after {MAX_RETRIES} attempts: {last_err}")


# =============================================================================
# Parsing
# =============================================================================


def response_to_dict(response) -> dict:
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                pass
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
    """Runtime-reported consideration set from action.sources[] (deduped, ordered)."""
    sources, seen = [], set()
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        if not isinstance(action, dict):
            continue
        for src in action.get("sources", []) or []:
            if isinstance(src, str):
                if src not in seen:
                    seen.add(src)
                    sources.append({"url": src, "title": None, "type": "url"})
                continue
            if not isinstance(src, dict):
                continue
            url = src.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({"url": url, "title": src.get("title"), "type": src.get("type", "url")})
    return sources


def find_message_item(output: list):
    for item in output:
        if isinstance(item, dict) and item.get("type") == "message":
            if item.get("role", "assistant") == "assistant":
                return item
    return None


def extract_message_text_and_annotations(message: dict):
    if not message:
        return "", []
    text_parts, annotations = [], []
    for part in message.get("content", []) or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("output_text", "text"):
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            for ann in part.get("annotations", []) or []:
                if isinstance(ann, dict):
                    annotations.append(ann)
    return "".join(text_parts), annotations


def extract_citations(annotations: list) -> list:
    out = []
    for ann in annotations:
        if ann.get("type") != "url_citation":
            continue
        out.append({
            "url": ann.get("url"), "title": ann.get("title"),
            "start_index": ann.get("start_index"), "end_index": ann.get("end_index"),
        })
    return out


def split_answer_and_sources(full_text: str):
    if not full_text:
        return "", None
    idx = full_text.find(SOURCES_MARKER)
    if idx == -1:
        return full_text.strip(), None
    return full_text[:idx].strip(), full_text[idx + len(SOURCES_MARKER):].strip()


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def parse_self_reported_sources(sources_block):
    flags = {"sources_section_present": sources_block is not None,
             "parse_method": None, "partial_or_malformed": False, "notes": []}
    if sources_block is None:
        flags["partial_or_malformed"] = True
        flags["notes"].append("SOURCES section marker not found in model output.")
        return [], flags
    stripped = sources_block.strip()
    if not stripped or stripped.lower().startswith("(none)"):
        flags["parse_method"] = "explicit-none"
        flags["notes"].append("Model reported no sources in context.")
        return [], flags

    sources = _parse_labelled_blocks(sources_block)
    if sources:
        flags["parse_method"] = "labelled-blocks"
        all_urls = set(_URL_RE.findall(sources_block))
        parsed_urls = {s["url"] for s in sources if s["url"] and s["url"].startswith("http")}
        missed = all_urls - parsed_urls
        if missed:
            flags["partial_or_malformed"] = True
            flags["notes"].append(f"{len(missed)} URL(s) in the section were not captured by block parsing.")
        return sources, flags

    urls = _URL_RE.findall(sources_block)
    if urls:
        flags["parse_method"] = "url-scrape-fallback"
        flags["partial_or_malformed"] = True
        flags["notes"].append("Could not parse block format; scraped raw URLs. Titles/excerpts unavailable.")
        seen, out = set(), []
        for u in urls:
            u = u.rstrip(".,);")
            if u not in seen:
                seen.add(u)
                out.append({"url": u, "title": None, "excerpt": None})
        return out, flags

    flags["parse_method"] = "none"
    flags["partial_or_malformed"] = True
    flags["notes"].append("SOURCES section present but no URLs/entries found.")
    return [], flags


def _parse_labelled_blocks(block: str) -> list:
    sources, current = [], None

    def flush():
        nonlocal current
        if current and (current.get("url") or current.get("title") or current.get("excerpt")):
            sources.append(current)
        current = None

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•]\s+", "", line)
        line = re.sub(r"^\d+[.)]\s+", "", line)
        m = re.match(r"(?i)^url\s*[:\-]\s*(.*)$", line)
        if m:
            flush()
            current = {"url": m.group(1).strip() or None, "title": None, "excerpt": None}
            continue
        m = re.match(r"(?i)^title\s*[:\-]\s*(.*)$", line)
        if m and current is not None:
            current["title"] = m.group(1).strip() or None
            continue
        m = re.match(r"(?i)^excerpt\s*[:\-]\s*(.*)$", line)
        if m and current is not None:
            current["excerpt"] = m.group(1).strip() or None
            continue
        if current is not None and current.get("excerpt"):
            current["excerpt"] = (current["excerpt"] + " " + line).strip()
    flush()

    for s in sources:
        if s["url"] and not s["url"].lower().startswith("http"):
            s["url_unrecoverable_note"] = s["url"]
            s["url"] = None
    return sources


# =============================================================================
# Reconciliation
# =============================================================================


def normalize_url(url):
    if not url or not isinstance(url, str):
        return None
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower() or "https"
        netloc = parts.netloc.lower()
        path = parts.path.rstrip("/")
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"ref", "fbclid", "gclid"}]
        return urlunsplit((scheme, netloc, path, urlencode(sorted(q)), ""))
    except Exception:
        return url.strip().lower().rstrip("/")


def _url_index(items: list) -> dict:
    idx = {}
    for it in items:
        n = normalize_url(it.get("url"))
        if n:
            idx.setdefault(n, it.get("url"))
    return idx


def reconcile(citations: list, api_sources: list, self_reported: list) -> dict:
    C, A, S = _url_index(citations), _url_index(api_sources), _url_index(self_reported)
    ck, ak, sk = set(C), set(A), set(S)

    def urls(index, keys):
        return [index[k] for k in sorted(keys)]

    return {
        "cited_and_in_api": urls(C, ck & ak),
        "consulted_not_cited": urls(A, ak - ck),
        "cited_not_in_api": urls(C, ck - ak),
        "self_report_corroborated": urls(S, sk & ak),
        "self_report_uncorroborated": urls(S, sk - ak),
        "self_report_matches_citation": urls(S, sk & ck),
        "counts": {
            "citations": len(ck), "api_sources": len(ak), "self_reported": len(sk),
            "cited_and_in_api": len(ck & ak), "consulted_not_cited": len(ak - ck),
            "cited_not_in_api": len(ck - ak), "self_report_corroborated": len(sk & ak),
            "self_report_uncorroborated": len(sk - ak),
        },
    }


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
# Orchestration
# =============================================================================


def run_inspection(query: str, include_raw: bool = True) -> dict:
    """Run one web-search inspection and return the structured result dict.

    Assumes OPENAI_API_KEY is present in the environment (OpenAI() reads it).
    Raises on hard failures; the caller maps that to an HTTP error.
    """
    from openai import OpenAI

    client = OpenAI()
    response = _call(client, query)
    raw = response_to_dict(response)
    output = raw.get("output") or []

    message = find_message_item(output)
    full_text, annotations = extract_message_text_and_annotations(message)
    final_text, sources_block = split_answer_and_sources(full_text)
    citations = extract_citations(annotations)
    api_sources = extract_api_sources(output)
    self_reported, sr_flags = parse_self_reported_sources(sources_block)

    result = {
        "model": MODEL,
        "query": query,
        "search_queries": extract_search_queries(output),
        "final_text": final_text,
        "citations": citations,
        "citations_label": "ground truth (structured annotations)",
        "api_sources": api_sources,
        "api_sources_label": "runtime-reported consideration set (web_search_call.action.sources)",
        "self_reported_sources": self_reported,
        "self_reported_label": "model-reported, UNVERIFIED",
        "self_reported_flags": sr_flags,
        "reconciliation": reconcile(citations, api_sources, self_reported),
        "usage": extract_usage(raw),
    }
    if include_raw:
        result["raw_response"] = raw
    return result
