"""
Vercel Python serverless function: GET /api/serp

Fetches a ranked SERP for one query so experiment B can compare the model's
consideration set / citations against a search engine's ranking. Two providers,
selected with &provider= ; keys stay server-side.

  GET /api/serp                                  -> health check (which keys present?)
  GET /api/serp?q=...&provider=bing&cc=us&count=30
  GET /api/serp?q=...&provider=exa&count=30
  -> {query, provider, results:[{rank,url,title}], note?}

Providers:
  bing  -> SerpAPI Bing engine (scraped consumer SERP; navigational/homepage-y)
  exa   -> Exa neural search (relevance-ranked; deep pages, closer to how an LLM
           retriever behaves) -> https://exa.ai

Env vars (Vercel project settings):
  SERPAPI_KEY   -> https://serpapi.com  (free ~100/mo)   [provider=bing]
  EXA_API_KEY   -> https://exa.ai       (free credits)   [provider=exa]
  APP_PASSWORD  (optional)  -> if set, result requests must pass &password=
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SERPAPI_URL = "https://serpapi.com/search.json"
EXA_URL = "https://api.exa.ai/search"
DEFAULT_COUNT = 30


def _http(url: str, *, data: bytes = None, headers: dict = None) -> dict:
    """One HTTP call returning parsed JSON, or {"error": msg}; raises nothing."""
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "ws-inspector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Providers put a useful message in the JSON body (invalid key, bad
        # location, …); urllib raises before we can read it, so surface it here.
        msg = f"HTTP {e.code}"
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            if isinstance(body, dict):
                msg = str(body.get("error") or body.get("message") or msg)
        except Exception:
            pass
        if e.code in (401, 403):
            msg = f"provider rejected the key ({e.code}): {msg}"
        return {"error": msg}


def fetch_bing(query: str, cc: str, location: str, count: int, api_key: str) -> dict:
    base = {"engine": "bing", "q": query, "api_key": api_key, "count": count}
    # Force a market/language, or Bing returns international junk (baidu, zhihu,
    # chiebukuro) for English queries. cc is a 2-letter ISO code.
    base["mkt"] = f"en-{cc.upper()}" if cc else "en-US"
    if cc:
        base["cc"] = cc.lower()

    def call(p):
        return _http(SERPAPI_URL + "?" + urllib.parse.urlencode(p))

    # Bing's `location` only accepts canonical names from SerpAPI's Locations API,
    # so a free-form "City, Region, Country" can be rejected. Try with it, and on
    # a location-specific error fall back to a country-only (cc) search.
    note = None
    data = call({**base, "location": location} if location else base)
    if location and isinstance(data, dict) and data.get("error") \
            and "location" in str(data["error"]).lower():
        note = f"Bing ignored the location ({location!r}): {data['error']}. Showing country-level results."
        data = call(base)

    if isinstance(data, dict) and data.get("error"):
        return {"error": str(data["error"]), "results": []}

    results = []
    for i, item in enumerate(data.get("organic_results") or [], start=1):
        link = item.get("link")
        if not link:
            continue
        results.append({"rank": i, "url": link, "title": item.get("title")})
    return {"results": results, "note": note}


def fetch_exa(query: str, count: int, api_key: str) -> dict:
    payload = json.dumps({"query": query, "numResults": count, "type": "auto"}).encode()
    data = _http(EXA_URL, data=payload, headers={
        "Content-Type": "application/json", "x-api-key": api_key,
        "User-Agent": "ws-inspector/1.0",
    })
    if isinstance(data, dict) and data.get("error"):
        return {"error": str(data["error"]), "results": []}

    results = []
    for i, item in enumerate(data.get("results") or [], start=1):
        link = item.get("url")
        if not link:
            continue
        results.append({"rank": i, "url": link, "title": item.get("title")})
    # Exa is keyword/neural relevance, with no geo targeting.
    note = "Exa is relevance-ranked and not geo-targeted (location ignored)."
    return {"results": results, "note": note}


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        q = (params.get("q") or [""])[0].strip()
        provider = (params.get("provider") or ["bing"])[0].lower()

        # No query -> health check (no API cost).
        if not q:
            return self._send(200, {
                "ok": True,
                "providers": {
                    "bing": bool(os.environ.get("SERPAPI_KEY")),
                    "exa": bool(os.environ.get("EXA_API_KEY")),
                },
                "auth_required": bool(os.environ.get("APP_PASSWORD")),
            })

        # Cost gate: providers spend credits, so honor APP_PASSWORD here too.
        required = os.environ.get("APP_PASSWORD")
        if required and (params.get("password") or [""])[0] != required:
            return self._send(401, {"error": "Unauthorized: wrong or missing password."})

        try:
            count = int((params.get("count") or [str(DEFAULT_COUNT)])[0])
        except ValueError:
            count = DEFAULT_COUNT
        count = max(1, min(50, count))

        try:
            if provider == "exa":
                api_key = (os.environ.get("EXA_API_KEY") or "").strip()
                if not api_key:
                    return self._send(500, {"error": "Server is missing EXA_API_KEY env var."})
                out = fetch_exa(q, count, api_key)
            elif provider == "bing":
                api_key = (os.environ.get("SERPAPI_KEY") or "").strip()
                if not api_key:
                    return self._send(500, {"error": "Server is missing SERPAPI_KEY env var."})
                cc = (params.get("cc") or [""])[0]
                location = (params.get("location") or [""])[0]
                out = fetch_bing(q, cc, location, count, api_key)
            else:
                return self._send(400, {"error": f"Unknown provider {provider!r} (use bing or exa)."})
        except Exception as e:  # network / provider error
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})

        if out.get("error"):
            return self._send(502, {"error": out["error"], "query": q, "results": []})
        return self._send(200, {
            "query": q, "count": count, "note": out.get("note"),
            "provider": provider, "results": out["results"],
        })
