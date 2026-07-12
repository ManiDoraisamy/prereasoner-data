"""The ONE PreReasoner server. Serves all three inference paths from a single process:

  POST /api/reason    — the composition reasoner (view-stacking) on the live world path. Firebase auth
                        derives the verified user; the working Postgres schema is the CONVERSATION (owned
                        by that user, see engine.conversations) + live reasoning-trace streaming to RTDB
                        (/runs/{uid}/{jobId}) when RTDB_URL is configured.
  POST /api/world     — the world path (unified-encoder world joins / hybrid semantic SQL). Same auth +
                        conversation + trace contract; both routes share ONE WorldReasoner instance.
  POST /api/dimension — the stateless per-column/per-cell taxonomy readout (no Postgres, no auth).
  GET  /api/conversations       — the signed-in user's conversations (drawer list; ownership-scoped).
  GET  /api/conversation?id=…   — one conversation's opening prompt + stored tables (re-open).
  GET  /healthz — liveness (+ model load state); /api/healthz = same (GFE reserves /healthz on run.app).

Request shape for reason/world: {tables:[{name,data}], question, as_of?, jobId?, conversation_id?} +
header Authorization: Bearer <Firebase ID token>. The response echoes conversation_id. For dimension:
{data, mode:'analyze'}. Non-prod bypass: AUTH_TEST_SUB -> fixed user, skips token verification (test-only).

Run: python -m engine.server
"""
from __future__ import annotations
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import urlparse, parse_qs

from engine.config import HOST, PORT
from engine.auth import _verify_principal, _bearer, _slug
from engine.tables import csv_table
from engine.trace import emitter, stream_final, set_ctx
from engine.conversations import resolve_conversation, list_conversations, get_conversation, NotOwned

MODEL = None                       # the ONE WorldReasoner, shared by /api/reason and /api/world
DIM_MODEL = None                   # the ONE DimensionModel for /api/dimension
WORLD_LOCK = threading.Lock()      # one request at a time through the shared world model (set_ctx is per-request)
DIM_LOCK = threading.Lock()        # one request at a time through the dimension model
MAX_BODY = 10 * 1024 * 1024
MAX_SHEETS = 8
MAX_ROWS = 5000

WORLD_ROUTES = ("/api/reason", "/api/world")
DIM_ROUTE = "/api/dimension"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self._cors(); self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        # /api/healthz is an alias: Google's front end reserves /healthz on *.run.app
        # domains (answers 404 itself), so external monitors must use the /api/ path.
        if path in ("/healthz", "/api/healthz"):
            self._send(200, json.dumps({"ok": MODEL is not None and DIM_MODEL is not None,
                                        "reason": MODEL is not None, "world": MODEL is not None,
                                        "dimension": DIM_MODEL is not None}))
        elif path in ("/api/conversations", "/api/conversation"):
            self._get_conversations(path, parse_qs(u.query))
        else:
            self._send(200, "prereasoner engine - POST /api/reason | /api/world {tables, question} + Bearer "
                            "Firebase token; POST /api/dimension {data, mode:'analyze'}",
                       "text/plain; charset=utf-8")

    # ---------------- conversation list / re-open (auth required; ownership-scoped) ----------------
    def _get_conversations(self, path, qs):
        try:
            sub, _uid = _verify_principal(_bearer(self.headers, None))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if path == "/api/conversations":
                self._send(200, json.dumps({"conversations": list_conversations(sub)})); return
            cid = (qs.get("id") or [""])[0]
            try:
                self._send(200, json.dumps(get_conversation(sub, cid)))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))   # not yours OR absent
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        path = self.path.rstrip("/")
        if path in WORLD_ROUTES:
            self._post_world()
        elif path == DIM_ROUTE:
            self._post_dimension()
        else:
            self._send(404, json.dumps({"error": "POST /api/reason | /api/world | /api/dimension"}))

    # ---------------- /api/reason + /api/world (Firebase auth + RTDB trace stream) ----------------
    def _post_world(self):
        emit = None                                          # so the except can stream a terminal error to RTDB
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}")
            sub, uid = _verify_principal(_bearer(self.headers, req))   # sub = VERIFIED user id (auth); uid = RTDB /runs key
            if not sub:
                self._send(401, json.dumps({"error": "sign in required (no valid Google token)"}))
                return
            sheets = req.get("tables")
            if isinstance(sheets, dict):
                sheets = [sheets]
            if sheets:
                tabs = [csv_table(s["data"], _slug(s.get("name"), i))
                        for i, s in enumerate(sheets[:MAX_SHEETS]) if isinstance(s, dict) and (s.get("data") or "").strip()]
            else:
                data = req.get("data", "")
                if not data.strip():
                    self._send(200, json.dumps({"error": "no CSV (need {tables:[…], question})"})); return
                tabs = [csv_table(data, req.get("table", "data"))]
            if not tabs:
                self._send(200, json.dumps({"error": "no CSV rows"})); return
            truncated = []
            for t in tabs:
                if len(t["rows"]) > MAX_ROWS:
                    truncated.append(f"{t['name']}: only the first {MAX_ROWS} rows were used ({len(t['rows'])} uploaded)")
                    t["rows"] = t["rows"][:MAX_ROWS]
            # The WORKING Postgres schema is the CONVERSATION, not the user. A client-supplied
            # conversation id is honored ONLY after the ownership check (chat.user_conversation);
            # otherwise a new conversation is minted for the verified user. No conversation id
            # ever reaches the schema without passing through this authorization (no IDOR).
            try:
                conv = resolve_conversation(sub, req.get("conversation_id"), req.get("question", ""), sheets)
            except NotOwned:
                # 404 (not 403) to match GET /api/conversation — "not yours" and "absent" look identical (no enumeration).
                self._send(404, json.dumps({"error": "conversation not found"})); return
            emit = emitter(uid, req.get("jobId"))            # RTDB key = the Firebase uid (== browser auth.uid); no-op if no jobId
            emit("conversation_id", conv)                    # stream it EARLY so the browser gets it even if the HTTP body is lost to a proxy timeout
            emit("status", "running")
            with WORLD_LOCK:
                set_ctx(emit)                                # so the DEEP bridge build streams the cell→qid lookup live
                try:
                    res = MODEL.serve(tabs, req.get("question", ""), conv, req.get("as_of"), emit=emit)
                finally:
                    set_ctx(None)
            if isinstance(res, dict):
                res["conversation_id"] = conv                # so the browser persists it for follow-up turns
            if truncated and isinstance(res, dict):
                res.setdefault("warnings", []).extend(truncated)
            stream_final(emit, res)                          # terminal state -> RTDB (decoupled from this response)
            self._send(200, json.dumps(res))
        except Exception as e:                           # noqa: BLE001
            if emit is not None:                         # don't leave the client stuck on 'running' — stream the error
                try:
                    emit("error", str(e)); emit("status", "error")
                except Exception:                        # noqa: BLE001
                    pass
            self._send(500, json.dumps({"error": str(e)}))

    # ---------------- /api/dimension (stateless, no auth) ----------------
    def _post_dimension(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}")
            data = req.get("data", "")
            if not data.strip():
                self._send(200, json.dumps({"error": "no CSV (need {data, mode:'analyze'})"})); return
            tbl = csv_table(data, req.get("table", "data"))
            if len(tbl["rows"]) > MAX_ROWS:
                tbl["rows"] = tbl["rows"][:MAX_ROWS]
            with DIM_LOCK:
                res = DIM_MODEL.analyze(tbl)
            self._send(200, json.dumps(res))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))


def main():
    global MODEL, DIM_MODEL
    from engine.world import WorldReasoner
    from engine.dimension import DimensionModel
    print("loading world reasoner (composition engine + unified Qwen + bge resolver + spaCy; LIVE Postgres)...",
          flush=True)
    MODEL = WorldReasoner()
    try:
        from engine.embeddings import Embedder
        Embedder.get().encode(["warmup"])               # load bge weights at startup, not on first request
        MODEL.qw._spacy()                               # load spaCy model at startup too
    except Exception as e:                              # noqa: BLE001
        print("warmup note:", e, flush=True)
    print("loading dimension model (taxonomy unified Qwen + LoRA + relational readout)...", flush=True)
    DIM_MODEL = DimensionModel()
    print(f"engine ready: http://{HOST}:{PORT}  (POST /api/reason /api/world /api/dimension)", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
