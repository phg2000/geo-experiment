"""
Vercel Python serverless function.

  GET  /api/inspect  -> tiny health check (does the function load? is the key set?)
  POST /api/inspect  -> run an inspection; body {"query": "...", "password": "..."}

Env vars (set in Vercel project settings):
  OPENAI_API_KEY  (required) — billable OpenAI key; never hardcode.
  APP_PASSWORD    (optional) — if set, requests must supply a matching password.
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

# Make sibling modules (_core.py) importable regardless of how Vercel sets the
# working directory / sys.path. Without this, `from _core import ...` can fail
# at load time, and Vercel then serves an opaque "A server error has occurred"
# HTML page instead of our JSON.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import defensively: if anything goes wrong, capture it and report it as JSON
# from the handler rather than crashing the whole function on load.
_IMPORT_ERROR = None
try:
    from _core import run_inspection, MODEL
except Exception as e:  # noqa: BLE001
    _IMPORT_ERROR = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    run_inspection = None
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
        # Health check: open this route in a browser to confirm the function
        # loaded and the env is wired up, without spending any OpenAI credits.
        self._send(200, {
            "ok": _IMPORT_ERROR is None,
            "model": MODEL,
            "has_openai_key": bool(os.environ.get("OPENAI_API_KEY")),
            "auth_required": bool(os.environ.get("APP_PASSWORD")),
            "import_error": _IMPORT_ERROR,
        })

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

        try:
            result = run_inspection(query)
        except Exception as e:  # surface a clean, readable message to the browser
            return self._send(502, {
                "error": f"{type(e).__name__}: {e}",
                "detail": traceback.format_exc(),
            })

        result["_auth_required"] = bool(required)
        return self._send(200, result)
