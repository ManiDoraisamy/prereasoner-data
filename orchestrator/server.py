"""orchestrator/server.py — the chat backend + a single local origin for the chat UI.

Routes:
  POST /chat        {message, tables, history} + optional Bearer  -> {reply, traces, history}
  GET  /healthz     liveness
  ANY  /api/**      proxied to the engine (ENGINE_BASE_URL) — pre-warm pings, direct engine calls
  GET  /...         static files from web/public (the chat UI); / -> chat.html

In production Firebase Hosting serves the static UI and rewrites /chat + /api to the respective Cloud Run
services (docs/MCP.md); this single-origin server is the local-dev equivalent so the whole
browser -> orchestrator -> MCP -> engine loop runs from one URL.

Run: python -m orchestrator.server
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from engine import config
from orchestrator.orchestrator import run_chat

WEB_ROOT = Path(config.__file__).resolve().parent.parent / "web" / "public"
MAX_BODY = 12 * 1024 * 1024

# One asyncio loop in a background thread — the MCP stdio subprocess is spawned on it consistently
# (avoids per-request loop churn and Windows non-main-thread signal issues).
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, name="orch-loop", daemon=True).start()

_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
         ".png": "image/png", ".ico": "image/x-icon"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self._cors()
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bearer(self):
        h = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        return h[7:].strip() if h.lower().startswith("bearer ") else None

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.send_header("Content-Length", "0"); self.end_headers()

    # ---------- GET: health, /api proxy, static ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/healthz":
            self._send(200, json.dumps({"ok": True, "service": "orchestrator"}))
        elif path.rstrip("/") == "/config":
            # The chat UI reads this to decide sign-in: "test" => local AUTH_TEST_SUB bypass,
            # "firebase" => real Google sign-in (talking to the deployed engine).
            self._send(200, json.dumps({"authMode": config.ORCH_AUTH_MODE,
                                        "engine": config.ENGINE_BASE_URL}))
        elif path.startswith("/api/"):
            self._proxy_api("GET")
        else:
            self._static(path)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") == "/chat":
            self._chat()
        elif path.startswith("/api/"):
            self._proxy_api("POST")
        else:
            self._send(404, json.dumps({"error": "POST /chat"}))

    # ---------- /chat ----------
    def _chat(self):
        emit = None
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}")
            message = (req.get("message") or "").strip()
            if not message:
                self._send(200, json.dumps({"error": "no message"})); return
            tables = req.get("tables") or []
            history = req.get("history") or []
            turn_id = (req.get("turnId") or "").strip() or None
            conversation_id = (req.get("conversation_id") or "").strip() or None
            token = self._bearer()
            # AUTH GATE (required): run_chat drives PAID Sonnet inference on the owner's key, so demand a verified
            # identity BEFORE any work — otherwise an anonymous caller is denial-of-wallet. In local dev the engine's
            # AUTH_TEST_SUB makes _verify_principal accept without a token, so this is a no-op there; in prod it
            # requires a real Firebase token. The browser always sends Authorization: Bearer <token> to /chat.
            try:
                from engine.auth import _verify_principal
                sub, uid = _verify_principal(token)
            except Exception as e:                           # noqa: BLE001
                print("orchestrator auth verify failed:", e, flush=True)
                sub, uid = None, None
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            # LIVE TRACE: stream this turn under /runs/{uid}/{turnId} — the SAME verified uid the browser subscribes
            # to (never client-supplied). Each engine call is announced there. Best-effort; a no-op if RTDB is absent.
            if turn_id and uid:
                try:
                    from engine.trace import emitter
                    emit = emitter(uid, turn_id)
                    emit("status", "running")
                except Exception as e:                       # noqa: BLE001 — streaming must never block the answer
                    print("orchestrator stream setup skipped:", e, flush=True)
            fut = asyncio.run_coroutine_threadsafe(
                run_chat(message, tables, history,
                         engine_base_url=config.ENGINE_BASE_URL, bearer_token=token,
                         api_key=config.anthropic_api_key(), model=config.ANTHROPIC_MODEL,
                         turn_id=turn_id, emit=emit, conversation_id=conversation_id),
                _LOOP,
            )
            res = fut.result(timeout=300)
            self._send(200, json.dumps(res))
        except Exception as e:  # noqa: BLE001
            if emit:
                try:
                    emit("error", str(e)); emit("status", "error")
                except Exception:                            # noqa: BLE001
                    pass
            self._send(500, json.dumps({"error": str(e)}))

    # ---------- /api proxy ----------
    def _proxy_api(self, method):
        try:
            url = config.ENGINE_BASE_URL.rstrip("/") + self.path
            headers = {}
            if self.headers.get("Authorization"):
                headers["Authorization"] = self.headers["Authorization"]
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n) if n else None
                headers["content-type"] = self.headers.get("content-type", "application/json")
            r = httpx.request(method, url, content=body, headers=headers, timeout=180)
            self._send(r.status_code, r.content,
                       r.headers.get("content-type", "application/json"))
        except Exception as e:  # noqa: BLE001
            self._send(502, json.dumps({"error": f"engine proxy failed: {e}"}))

    # ---------- static ----------
    def _static(self, path):
        # "/" is the real pitch home (web/public/index.html) — the chat lives at /chat.html.
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB_ROOT / rel).resolve()
        if not f.suffix and not f.is_file():
            f = (WEB_ROOT / (rel + ".html")).resolve()   # Firebase Hosting cleanUrls parity: /reason -> reason.html
        try:
            f.relative_to(WEB_ROOT.resolve())  # no path traversal
        except ValueError:
            self._send(403, json.dumps({"error": "forbidden"})); return
        if not f.is_file():
            self._send(404, json.dumps({"error": "not found"})); return
        ctype = _MIME.get(f.suffix.lower(), "application/octet-stream")
        self._send(200, f.read_bytes(), ctype)


def main():
    # engine.config autoloads the repo .env at import, so ANTHROPIC_API_KEY / ENGINE_BASE_URL are already
    # in place here. Nothing else to bootstrap.
    print(f"orchestrator ready: http://{config.ORCH_HOST}:{config.ORCH_PORT}  "
          f"(POST /chat; auth={config.ORCH_AUTH_MODE}; engine at {config.ENGINE_BASE_URL})", flush=True)
    ThreadingHTTPServer((config.ORCH_HOST, config.ORCH_PORT), H).serve_forever()


if __name__ == "__main__":
    main()
