"""
Vercel Python serverless function: GET /api/serp

Fetches a ranked Bing SERP for one query via SerpAPI (engine=bing), so experiment
B can compare the model's consideration set / citations against Bing's ranking.
The SerpAPI key stays server-side.

  GET /api/serp                      -> health check (key present? auth?)
  GET /api/serp?q=...&cc=us&count=30 -> {query, results:[{rank,url,title}], provider}

Env vars (Vercel project settings):
  SERPAPI_KEY   (required for results) -> https://serpapi.com  (free ~100/mo)
  APP_PASSWORD  (optional)             -> if set, requests must pass &password=
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SERPAPI_URL = "https://serpapi.com/search.json"
DEFAULT_COUNT = 30


def _request(params: dict) -> dict:
    """One SerpAPI call. Returns {"results"|"error", ...}; raises nothing."""
    url = SERPAPI_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ws-inspector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # SerpAPI puts a useful message in the JSON body (invalid key, bad
        # location, …); urllib raises before we can read it, so surface it here.
        msg = f"HTTP {e.code}"
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
            if isinstance(body, dict) and body.get("error"):
                msg = str(body["error"])
        except Exception:
            pass
        if e.code == 401:
            msg = f"SerpAPI rejected the key (401): {msg}"
        return {"error": msg}


def fetch_bing(query: str, cc: str, location: str, count: int, api_key: str) -> dict:
    base = {"engine": "bing", "q": query, "api_key": api_key, "count": count}
    if cc:
        base["cc"] = cc.lower()

    # Bing's `location` only accepts canonical names from SerpAPI's Locations API,
    # so a free-form "City, Region, Country" can be rejected. Try with it, and on
    # a location-specific error fall back to a country-only (cc) search.
    note = None
    data = _request({**base, "location": location} if location else base)
    if location and isinstance(data, dict) and data.get("error") \
            and "location" in str(data["error"]).lower():
        note = f"Bing ignored the location ({location!r}): {data['error']}. Showing country-level results."
        data = _request(base)

    if isinstance(data, dict) and data.get("error"):
        return {"error": str(data["error"]), "results": []}

    results = []
    for i, item in enumerate(data.get("organic_results") or [], start=1):
        link = item.get("link")
        if not link:
            continue
        results.append({"rank": i, "url": link, "title": item.get("title")})
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

        # No query -> health check (no SerpAPI cost).
        if not q:
            return self._send(200, {
                "ok": True,
                "provider": "serpapi-bing",
                "has_serpapi_key": bool(os.environ.get("SERPAPI_KEY")),
                "auth_required": bool(os.environ.get("APP_PASSWORD")),
            })

        # Cost gate: SerpAPI spends credits, so honor APP_PASSWORD here too.
        required = os.environ.get("APP_PASSWORD")
        if required and (params.get("password") or [""])[0] != required:
            return self._send(401, {"error": "Unauthorized: wrong or missing password."})

        api_key = (os.environ.get("SERPAPI_KEY") or "").strip()
        if not api_key:
            return self._send(500, {"error": "Server is missing SERPAPI_KEY env var."})

        cc = (params.get("cc") or [""])[0]
        location = (params.get("location") or [""])[0]
        try:
            count = int((params.get("count") or [str(DEFAULT_COUNT)])[0])
        except ValueError:
            count = DEFAULT_COUNT
        count = max(1, min(50, count))

        try:
            out = fetch_bing(q, cc, location, count, api_key)
        except Exception as e:  # network / SerpAPI error
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})

        if out.get("error"):
            return self._send(502, {"error": out["error"], "query": q, "results": []})
        return self._send(200, {
            "query": q, "cc": cc, "count": count, "note": out.get("note"),
            "provider": "serpapi-bing", "results": out["results"],
        })
