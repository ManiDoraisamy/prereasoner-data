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
from concurrent.futures import TimeoutError as FutureTimeoutError
import importlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from engine import config
from engine.request_limits import SlidingWindowLimiter, allowed_origin
from orchestrator.orchestrator import run_chat
from orchestrator.validation import validate_chat_request

WEB_ROOT = Path(config.__file__).resolve().parent.parent / "web" / "public"
MAX_BODY = 8 * 1024 * 1024
CHAT_TIMEOUT_SECONDS = 240
CHAT_RATE = SlidingWindowLimiter(limit=10, window_seconds=60)
CHAT_IN_FLIGHT = threading.BoundedSemaphore(8)

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
        origin = allowed_origin(self.headers.get("Origin"), config.CORS_ORIGINS)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code, body, ctype="application/json", retry_after=None):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self._cors()
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
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
        elif path.rstrip("/") == "/readyz":
            ready = bool(os.environ.get("ANTHROPIC_API_KEY"))
            try:
                module = importlib.import_module("mcp_server.server")
                ready = ready and (hasattr(module, "mcp") or hasattr(module, "main"))
            except Exception:  # noqa: BLE001 - readiness must not expose import details
                ready = False
            self._send(200 if ready else 503, json.dumps({"ok": ready, "service": "orchestrator"}))
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
        fut = None
        acquired = False
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > MAX_BODY:
                self._send(413, json.dumps({"error": "payload too large"})); return
            raw = self.rfile.read(n) if n else b"{}"
            try:
                req = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, json.dumps({"error": "invalid JSON payload"})); return
            try:
                message, tables, history, turn_id, conversation_id = validate_chat_request(req)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)})); return
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
            if not config.external_llm_enabled() or req.get("external_llm_consent") is not True:
                self._send(503, json.dumps({
                    "error": "external LLM processing is disabled or this request lacks explicit consent"
                })); return
            key = uid or sub or self.client_address[0]
            allowed, retry_after = CHAT_RATE.allow(key)
            if not allowed:
                self._send(429, json.dumps({"error": "chat rate limit exceeded"}), retry_after=retry_after)
                return
            if not CHAT_IN_FLIGHT.acquire(blocking=False):
                self._send(429, json.dumps({"error": "chat capacity temporarily exhausted"}), retry_after=5)
                return
            acquired = True
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
            res = fut.result(timeout=CHAT_TIMEOUT_SECONDS)
            self._send(200, json.dumps(res))
        except FutureTimeoutError:
            if fut is not None:
                fut.cancel()
            if emit:
                emit("error", "request timed out"); emit("status", "error")
            self._send(504, json.dumps({"error": "chat request timed out"}))
        except Exception as e:  # noqa: BLE001
            if emit:
                try:
                    emit("error", "internal server error"); emit("status", "error")
                except Exception:                            # noqa: BLE001
                    pass
            print(f"orchestrator chat failed: {type(e).__name__} turn={getattr(self, 'path', '-')}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))
        finally:
            if acquired:
                CHAT_IN_FLIGHT.release()

    # ---------- /api proxy ----------
    def _proxy_api(self, method):
        try:
            url = config.ENGINE_BASE_URL.rstrip("/") + self.path
            headers = {}
            if self.headers.get("Authorization"):
                headers["Authorization"] = self.headers["Authorization"]
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n > MAX_BODY:
                    self._send(413, json.dumps({"error": "payload too large"})); return
                body = self.rfile.read(n) if n else None
                headers["content-type"] = self.headers.get("content-type", "application/json")
            r = httpx.request(method, url, content=body, headers=headers, timeout=180)
            self._send(r.status_code, r.content,
                       r.headers.get("content-type", "application/json"))
        except Exception as e:  # noqa: BLE001
            print(f"orchestrator engine proxy failed: {type(e).__name__}", flush=True)
            self._send(502, json.dumps({"error": "engine proxy unavailable"}))

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
