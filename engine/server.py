"""The ONE PreReasoner server. Serves all three inference paths from a single process:

  POST /api/reason    — the composition reasoner (view-stacking) on the live world path. Firebase auth
                        derives the verified user; the working Postgres schema is the CONVERSATION (owned
                        by that user, see engine.conversations) + live reasoning-trace streaming to RTDB
                        (/runs/{uid}/{jobId}) when RTDB_URL is configured.
  POST /api/knowledge     — the world path (unified-encoder world joins / hybrid semantic SQL). Same auth +
                        conversation + trace contract; both routes share ONE KnowledgeReasoner instance.
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
from engine.conversations import (resolve_conversation, list_conversations, get_conversation,
                                   delete_conversation, delete_all_conversations, save_state, NotOwned)
from engine import master
from engine import admin

MODEL = None                       # the ONE KnowledgeReasoner, shared by /api/reason and /api/knowledge
DIM_MODEL = None                   # the ONE DimensionModel for /api/dimension
WORLD_LOCK = threading.Lock()      # one request at a time through the shared world model (set_ctx is per-request)
DIM_LOCK = threading.Lock()        # one request at a time through the dimension model
MAX_BODY = 10 * 1024 * 1024
MAX_SHEETS = 8
MAX_ROWS = 5000

WORLD_ROUTES = ("/api/reason", "/api/knowledge")
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
        elif path == "/api/master":
            self._get_master(parse_qs(u.query))
        elif path.startswith("/api/admin/"):
            self._get_admin(path, parse_qs(u.query))
        else:
            self._send(200, "prereasoner engine - POST /api/reason | /api/knowledge {tables, question} + Bearer "
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

    # ---------------- master data (per-user reference tables; auth required, uid-scoped) ----------------
    def _get_master(self, qs):
        """GET /api/master → the user's master tables (list). GET /api/master?name=X → one table's rows."""
        try:
            sub, _uid = _verify_principal(_bearer(self.headers, None))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            name = (qs.get("name") or [""])[0]
            if name:
                m = master.get_master(sub, name)
                self._send(200 if m else 404, json.dumps(m or {"error": "not found"})); return
            self._send(200, json.dumps({"tables": master.list_master(sub)}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def _post_master(self, path):
        """POST /api/master {name, columns, rows} → create-or-replace a master table.
        POST /api/master/delete {name} → drop it. Both uid-scoped to the verified subject."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sub, _uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if path == "/api/master/delete":
                self._send(200, json.dumps(master.delete_master(sub, req.get("name", "")))); return
            try:
                out = master.save_master(sub, req.get("name", ""), req.get("columns") or [], req.get("rows") or [])
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)})); return
            self._send(200, json.dumps(out))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def _post_conv_state(self):
        """POST /api/conversation/state {id, state} -> persist the client's renderable snapshot so a reload
        restores the conversation instead of re-running. uid-scoped; `state` is opaque display JSON."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sub, _uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            try:
                self._send(200, json.dumps(save_state(sub, req.get("id", ""), req.get("state"))))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def _post_conv_delete(self, path):
        """POST /api/conversation/delete {id} -> drop one conversation; /delete-all -> drop them all. uid-scoped."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sub, _uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if path == "/api/conversation/delete-all":
                self._send(200, json.dumps(delete_all_conversations(sub))); return
            try:
                self._send(200, json.dumps(delete_conversation(sub, req.get("id", ""))))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        path = self.path.rstrip("/")
        if path in WORLD_ROUTES:
            self._post_world()
        elif path == DIM_ROUTE:
            self._post_dimension()
        elif path == "/api/converse":
            self._post_converse()
        elif path == "/api/master/generate":
            self._post_master_generate()
        elif path in ("/api/master", "/api/master/delete"):
            self._post_master(path)
        elif path in ("/api/conversation/delete", "/api/conversation/delete-all"):
            self._post_conv_delete(path)
        elif path == "/api/conversation/state":
            self._post_conv_state()
        elif path == "/api/admin/delete":
            self._post_admin_delete()
        else:
            self._send(404, json.dumps({"error": "POST /api/reason | /api/knowledge | /api/dimension"}))

    # ---------------- admin dashboard (email-allowlisted; reads via GET, deletes via POST) ----------------
    def _require_admin(self, body=None):
        who = admin.verify_admin(_bearer(self.headers, body))
        if not who:
            self._send(403, json.dumps({"error": "admin only"}))
        return who

    def _get_admin(self, path, qs):
        try:
            if not self._require_admin():
                return
            if path.rstrip("/") == "/api/admin/users":
                self._send(200, json.dumps({"users": admin.list_users()}))
            elif path.rstrip("/") == "/api/admin/conversations":
                self._send(200, json.dumps({"conversations": admin.list_conversations((qs.get("user") or [None])[0])}))
            elif path.rstrip("/") == "/api/admin/orphans":
                self._send(200, json.dumps({"orphans": admin.list_orphans()}))
            else:
                self._send(404, json.dumps({"error": "GET /api/admin/users | conversations[?user=] | orphans"}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    def _post_admin_delete(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not self._require_admin(req):
                return
            target = req.get("target")
            if target == "conversation":
                out = admin.delete_conversation(req.get("id", ""))
            elif target == "user":
                out = admin.delete_user(req.get("id", ""), also_auth=bool(req.get("also_auth")))
            elif target == "orphans":
                out = admin.delete_orphans()
            else:
                self._send(400, json.dumps({"error": "target must be conversation | user | orphans"})); return
            self._send(200, json.dumps({"ok": True, **out}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    # ---------------- /api/converse (Sonnet conversational fallback for the /reason rail) ----------------
    def _post_converse(self):
        """Answer a clarify / non-data question conversationally (Sonnet), so the rail replies in-chat
        instead of redirecting. No model/Postgres — a single Anthropic call; the deterministic path is
        unchanged. Firebase-auth'd like the reasoning routes; a missing key degrades to a clear 503."""
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sub, _uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            from engine import converse
            try:
                text = converse.reply(req.get("question", ""), clarify=req.get("clarify"),
                                      error=req.get("error"), tables=req.get("tables"),
                                      answer=req.get("answer"), sql=req.get("sql"))
            except Exception as e:                           # noqa: BLE001 — no key / SDK / upstream: let the client fall back
                print(f"/api/converse degraded (503): {type(e).__name__}: {e}", flush=True)  # missing key vs down vs rate-limit
                self._send(503, json.dumps({"error": f"converse unavailable: {type(e).__name__}: {e}"})); return
            self._send(200, json.dumps({"reply": text}))
        except Exception as e:                               # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))

    # ---------------- /api/master/generate (Sonnet fills a reference table) ----------------
    def _post_master_generate(self):
        """POST /api/master/generate {name, columns, rows, instruction?, jobId?} → Sonnet fills the reference
        table's attribute columns for each entity (col 0), preserving already-filled cells, and returns
        {columns, rows}. The Sonnet fill can exceed the ~60s Firebase-proxy timeout (cold start + generation),
        so — exactly like the reasoning routes — the result is ALSO streamed to RTDB (/runs/{uid}/{jobId}):
        the browser reads it there even when the POST response is lost to the proxy. A missing/failed Anthropic
        key degrades to a clear 503 (+ an RTDB error) so the popup re-enables."""
        emit = None
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                self._send(200, json.dumps({"error": "payload too large"})); return
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            sub, uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            emit = emitter(uid, req.get("jobId"))            # no-op if RTDB/jobId absent; else streams past the 60s proxy
            emit("status", "running")
            from engine import converse
            try:
                out = converse.generate_master(req.get("name", "reference"), req.get("columns") or [],
                                               req.get("rows") or [], instruction=req.get("instruction"), emit=emit)
            except Exception as e:                           # noqa: BLE001 — no key / SDK / bad JSON: let the client re-enable
                print(f"/api/master/generate degraded (503): {type(e).__name__}: {e}", flush=True)
                emit("error", f"generate unavailable: {type(e).__name__}"); emit("status", "error")
                self._send(503, json.dumps({"error": f"generate unavailable: {type(e).__name__}: {e}"})); return
            emit("result", out); emit("status", "done")      # terminal state -> RTDB (decoupled from this response)
            self._send(200, json.dumps(out))
        except Exception as e:                               # noqa: BLE001
            if emit is not None:
                try: emit("error", str(e)); emit("status", "error")
                except Exception: pass                       # noqa: BLE001 — streaming is best-effort
            self._send(500, json.dumps({"error": str(e)}))

    # ---------------- /api/reason + /api/knowledge (Firebase auth + RTDB trace stream) ----------------
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
    from engine.knowledge import KnowledgeReasoner
    from engine.dimension import DimensionModel
    print("loading world reasoner (composition engine + unified Qwen + bge resolver + spaCy; LIVE Postgres)...",
          flush=True)
    MODEL = KnowledgeReasoner()
    try:
        from engine.embeddings import Embedder
        Embedder.get().encode(["warmup"])               # load bge weights at startup, not on first request
        MODEL.qw._spacy()                               # load spaCy model at startup too
    except Exception as e:                              # noqa: BLE001
        print("warmup note:", e, flush=True)
    print("loading dimension model (taxonomy unified Qwen + LoRA + relational readout)...", flush=True)
    DIM_MODEL = DimensionModel()
    print(f"engine ready: http://{HOST}:{PORT}  (POST /api/reason /api/knowledge /api/dimension)", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
