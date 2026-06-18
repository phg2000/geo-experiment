"""
Vercel Python serverless function (background mode).

  GET  /api/inspect            -> health check (does the function load? key set?)
  GET  /api/inspect?id=&query= -> poll a background run; returns status or result
  POST /api/inspect            -> start a background run; body {"query","password"}
                                  returns {"id","status"} immediately

The run executes in OpenAI's background mode, so every call here returns
near-instantly and never hits Vercel's function time limit. The browser polls
the GET endpoint until the run completes.

Env vars (set in Vercel project settings):
  OPENAI_API_KEY  (required) — billable OpenAI key; never hardcode.
  APP_PASSWORD    (optional) — if set, starting a run requires a matching password.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Make sibling modules (_core.py) importable regardless of Vercel's working dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IMPORT_ERROR = None
try:
    from _core import start_inspection, poll_inspection, normalize_mode, MODE_BARE, MODE_INSTRUMENTED, MODEL
except Exception as e:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    start_inspection = poll_inspection = None
    normalize_mode = lambda m: m  # noqa: E731
    MODE_BARE, MODE_INSTRUMENTED = "bare", "instrumented"
    MODEL = "unknown"


class handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        run_id = (params.get("id") or [""])[0].strip()

        # No id -> health check (no OpenAI cost).
        if not run_id:
            return self._send(200, {
                "ok": _IMPORT_ERROR is None,
                "model": MODEL,
                "mode": "background",
                "has_openai_key": bool(os.environ.get("OPENAI_API_KEY")),
                "auth_required": bool(os.environ.get("APP_PASSWORD")),
                "import_error": _IMPORT_ERROR,
            })

        # Poll an existing background run.
        if _IMPORT_ERROR is not None:
            return self._send(500, {"error": "Function failed to load.", "detail": _IMPORT_ERROR})
        query = (params.get("query") or [""])[0]
        mode = normalize_mode((params.get("mode") or [MODE_INSTRUMENTED])[0])
        try:
            return self._send(200, poll_inspection(run_id, query, mode=mode))
        except Exception as e:  # invalid id, expired, network, etc.
            return self._send(502, {"error": f"{type(e).__name__}: {e}", "detail": traceback.format_exc()})

    def do_POST(self):
        if _IMPORT_ERROR is not None:
            return self._send(500, {"error": "Function failed to load.", "detail": _IMPORT_ERROR})

        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "Invalid JSON body."})

        query = (data.get("query") or "").strip()
        if not query:
            return self._send(400, {"error": "Missing 'query'."})

        required = os.environ.get("APP_PASSWORD")
        if required and data.get("password") != required:
            return self._send(401, {"error": "Unauthorized: wrong or missing password."})

        if not os.environ.get("OPENAI_API_KEY"):
            return self._send(500, {"error": "Server is missing OPENAI_API_KEY env var."})

        # Accept `mode`; fall back to legacy `instrument` boolean if present.
        if "mode" in data:
            mode = normalize_mode(data.get("mode"))
        else:
            mode = MODE_INSTRUMENTED if data.get("instrument", True) else MODE_BARE
        try:
            started = start_inspection(query, mode=mode)  # {id, status}
        except Exception as e:
            return self._send(502, {"error": f"{type(e).__name__}: {e}", "detail": traceback.format_exc()})

        started["_auth_required"] = bool(required)
        started["_mode"] = mode
        return self._send(200, started)
