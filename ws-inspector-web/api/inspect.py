"""
Vercel Python serverless function: POST /api/inspect

Body: {"query": "...", "password": "..."}  (password only needed if APP_PASSWORD set)
Returns: the structured inspection result as JSON (same shape as the CLI's .json).

Env vars (set in Vercel project settings):
  OPENAI_API_KEY  (required) — billable OpenAI key; never hardcode.
  APP_PASSWORD    (optional) — if set, requests must supply a matching password.
                  Leave unset only if you intend the endpoint to be public
                  (anyone hitting it spends your OpenAI credits).
"""

import json
import os
from http.server import BaseHTTPRequestHandler

from _core import run_inspection


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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "Invalid JSON body."})

        query = (data.get("query") or "").strip()
        if not query:
            return self._send(400, {"error": "Missing 'query'."})

        # Optional password gate to protect your OpenAI credits.
        required = os.environ.get("APP_PASSWORD")
        if required and data.get("password") != required:
            return self._send(401, {"error": "Unauthorized: wrong or missing password."})

        if not os.environ.get("OPENAI_API_KEY"):
            return self._send(500, {"error": "Server is missing OPENAI_API_KEY env var."})

        try:
            result = run_inspection(query)
        except Exception as e:  # surface a clean message to the browser
            return self._send(502, {"error": f"{type(e).__name__}: {e}"})

        # Tell the frontend whether a password is required (drives the UI).
        result["_auth_required"] = bool(required)
        return self._send(200, result)
