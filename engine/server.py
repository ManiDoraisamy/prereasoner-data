"""The ONE Prereasoner server. Serves all three inference paths from a single process:

  POST /api/reason    — the composition reasoner (view-stacking) on the live world path. Firebase auth
                        derives the verified user; the working Postgres schema is the CONVERSATION (owned
                        by that user, see engine.conversations) + live reasoning-trace streaming to RTDB
                        (/runs/{uid}/{jobId}) when RTDB_URL is configured.
  POST /api/knowledge     — the world path (unified-encoder world joins / hybrid semantic SQL). Same auth +
                        conversation + trace contract; both routes share ONE KnowledgeReasoner instance.
  POST /api/dimension — the stateless per-column/per-cell taxonomy readout (no Postgres, auth required).
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
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import urlparse, parse_qs

from engine import config
from engine.config import HOST, PORT, external_llm_enabled
from engine.auth import _verify_principal, _bearer
from engine.tables import csv_table, table_name
from engine.trace import emitter, stream_final, set_ctx
from engine import dataset_semantics
from engine import dataset_attestation
from engine.dataset_semantics import DatasetOpError
from engine.conversations import (resolve_conversation, conversation_page, get_conversation,
                                  load_dataset_ops, append_dataset_ops,
                                   delete_conversation, delete_all_conversations, save_state, NotOwned,
                                   QuotaExceeded, DatasetOpsLimitError)
from engine import master
from engine import admin
from engine import request_timing
from engine.pg import _pg
from engine.request_budget import BudgetPolicy, PostgresRequestBudget
from engine.request_limits import (
    JSONBodyError, RequestGate, RequestLease, SlidingWindowLimiter, allowed_origin, read_json_object,
)
from engine.request_validation import RequestValidationError, validate_reason_request
from engine.provenance import ProvenanceContext


MODEL = None                       # the ONE KnowledgeReasoner, shared by /api/reason and /api/knowledge
DIM_MODEL = None                   # the ONE DimensionModel for /api/dimension
ENRICHMENT = None                  # request-local enrichment; registry activation remains authoritative
WORLD_LOCK = threading.Lock()      # one request at a time through the shared world model (set_ctx is per-request)
DIM_LOCK = threading.Lock()        # one request at a time through the dimension model
MAX_BODY = 10 * 1024 * 1024
MAX_SHEETS = 8
MAX_ROWS = 5000
MAX_TABLE_CHARS = 2 * 1024 * 1024
MAX_TABLE_TOTAL_CHARS = 6 * 1024 * 1024
MAX_CONVERSE_CHARS = 256 * 1024
MAX_GENERATE_ROWS = 250
MAX_GENERATE_COLS = 32
MAX_GENERATE_CHARS = 256 * 1024
WORLD_RATE = SlidingWindowLimiter(limit=30, window_seconds=60)
DIM_RATE = SlidingWindowLimiter(limit=60, window_seconds=60)
PAID_LOCAL_GATES = {
    "converse": RequestGate(requests=6, window_seconds=60, in_flight=4),
    "master_generate": RequestGate(requests=3, window_seconds=60, in_flight=2),
}
PAID_BUDGET = PostgresRequestBudget(_pg, {
    "converse": BudgetPolicy(6, 120, 2, 16, user_requests_per_day=100,
                              global_requests_per_day=2_000),
    "master_generate": BudgetPolicy(3, 30, 1, 6, user_requests_per_day=30,
                                     global_requests_per_day=300),
})

WORLD_ROUTES = ("/api/reason", "/api/knowledge")
DIM_ROUTE = "/api/dimension"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        origin = allowed_origin(self.headers.get("Origin"), config.CORS_ORIGINS)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send(self, code, body, ctype="application/json", retry_after=None):
        self._status = code                              # every exit path, so the timing line reports the outcome
        b = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self._cors()
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def _read_json(self, max_bytes=MAX_BODY):
        """Read the shared bounded request shape and send client errors consistently."""
        try:
            return read_json_object(self.rfile, self.headers.get("Content-Length"), max_bytes)
        except JSONBodyError as exc:
            self._send(exc.status_code, json.dumps({"error": str(exc)}))
            return None

    def _acquire_paid_budget(self, subject, operation):
        local, retry_after, reason = PAID_LOCAL_GATES[operation].acquire(subject)
        if local is None:
            self._send(429, json.dumps({"error": "request budget exceeded", "reason": reason}),
                       retry_after=retry_after)
            return None
        try:
            distributed, retry_after, reason = PAID_BUDGET.acquire(subject, operation)
        except Exception as exc:                              # accounting must fail closed before paid work
            local.release()
            print(f"paid request budget unavailable: {type(exc).__name__}", flush=True)
            self._send(503, json.dumps({"error": "request budget unavailable"}))
            return None
        if distributed is None:
            local.release()
            self._send(429, json.dumps({"error": "request budget exceeded", "reason": reason}),
                       retry_after=retry_after)
            return None

        def release():
            distributed.release()
            local.release()

        return RequestLease(release)

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
                try:
                    limit = int((qs.get("limit") or ["50"])[0])
                except ValueError:
                    self._send(400, json.dumps({"error": "limit is invalid"})); return
                before = (qs.get("before") or [None])[0]
                try:
                    page = conversation_page(sub, limit, before)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)})); return
                self._send(200, json.dumps(page)); return
            cid = (qs.get("id") or [""])[0]
            try:
                self._send(200, json.dumps(get_conversation(sub, cid)))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))   # not yours OR absent
        except Exception as e:                               # noqa: BLE001
            print(f"conversation lookup failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

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
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)}))
        except Exception as e:                               # noqa: BLE001
            print(f"master lookup failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    def _post_master(self, path):
        """POST /api/master {name, columns, rows} → create-or-replace a master table.
        POST /api/master/delete {name} → drop it. Both uid-scoped to the verified subject."""
        try:
            req = self._read_json()
            if req is None:
                return
            sub, uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if path == "/api/master/delete":
                try:
                    self._send(200, json.dumps(master.delete_master(sub, req.get("name", ""))))
                except ValueError as e:
                    self._send(400, json.dumps({"error": str(e)}))
                return
            try:
                out = master.save_master(sub, req.get("name", ""), req.get("columns") or [], req.get("rows") or [])
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)})); return
            self._send(200, json.dumps(out))
        except Exception as e:                               # noqa: BLE001
            print(f"master write failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    def _post_conv_state(self):
        """POST /api/conversation/state {id, state} -> persist the client's renderable snapshot so a reload
        restores the conversation instead of re-running. uid-scoped; `state` is opaque display JSON."""
        try:
            req = self._read_json()
            if req is None:
                return
            sub, uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            try:
                self._send(200, json.dumps(save_state(sub, req.get("id", ""), req.get("state"))))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))
            except QuotaExceeded as exc:
                self._send(413, json.dumps({"error": str(exc)}))
        except Exception as e:                               # noqa: BLE001
            print(f"conversation state failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    def _post_conv_delete(self, path):
        """POST /api/conversation/delete {id} -> drop one conversation; /delete-all -> drop them all. uid-scoped."""
        try:
            req = self._read_json()
            if req is None:
                return
            sub, uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if path == "/api/conversation/delete-all":
                self._send(200, json.dumps(delete_all_conversations(sub, rtdb_uid=uid))); return
            try:
                self._send(200, json.dumps(
                    delete_conversation(sub, req.get("id", ""), rtdb_uid=uid)
                ))
            except NotOwned:
                self._send(404, json.dumps({"error": "conversation not found"}))
        except Exception as e:                               # noqa: BLE001
            print(f"conversation delete failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

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
            print(f"admin read failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    def _post_admin_delete(self):
        try:
            req = self._read_json()
            if req is None:
                return
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
            print(f"admin delete failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    # ---------------- /api/converse (Sonnet conversational fallback for the /reason rail) ----------------
    def _post_converse(self):
        """Answer a clarify / non-data question conversationally (Sonnet), so the rail replies in-chat
        instead of redirecting. One Anthropic call is protected by local concurrency and PostgreSQL-backed
        cross-instance quotas; the deterministic path is unchanged. Firebase-auth'd like the reasoning routes;
        a missing key degrades to a clear 503."""
        try:
            req = self._read_json(MAX_CONVERSE_CHARS)
            if req is None:
                return
            sub, _uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if not external_llm_enabled():
                self._send(503, json.dumps({
                    "error": "assistant processing is unavailable for this request"
                })); return
            if len(json.dumps(req, ensure_ascii=False)) > MAX_CONVERSE_CHARS:
                self._send(413, json.dumps({"error": "conversation context is too large"})); return
            lease = self._acquire_paid_budget(sub, "converse")
            if lease is None:
                return
            from engine import converse
            try:
                with lease:
                    text = converse.reply(req.get("question", ""), clarify=req.get("clarify"),
                                          error=req.get("error"), tables=req.get("tables"),
                                          answer=req.get("answer"), sql=req.get("sql"))
            except Exception as e:                           # noqa: BLE001 — no key / SDK / upstream: let the client fall back
                print(f"/api/converse degraded (503): {type(e).__name__}", flush=True)
                self._send(503, json.dumps({"error": "converse unavailable"})); return
            self._send(200, json.dumps({"reply": text}))
        except Exception as e:                               # noqa: BLE001
            print(f"/api/converse failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

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
            req = self._read_json(MAX_GENERATE_CHARS)
            if req is None:
                return
            sub, uid = _verify_principal(_bearer(self.headers, req))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            if not external_llm_enabled():
                self._send(503, json.dumps({
                    "error": "assistant processing is unavailable for this request"
                })); return
            columns, rows = req.get("columns") or [], req.get("rows") or []
            if not isinstance(columns, list) or not isinstance(rows, list):
                self._send(400, json.dumps({"error": "columns and rows must be arrays"})); return
            if (
                not columns
                or any(not isinstance(column, str) or not column.strip() for column in columns)
                or any(not isinstance(row, list) or len(row) > len(columns) for row in rows)
            ):
                self._send(400, json.dumps({"error": "generation table shape is invalid"})); return
            if len(columns) > MAX_GENERATE_COLS or len(rows) > MAX_GENERATE_ROWS:
                self._send(413, json.dumps({"error": "generation table is too large"})); return
            generation_chars = len(json.dumps({
                "name": req.get("name"), "columns": columns, "rows": rows,
                "instruction": req.get("instruction"),
            }, ensure_ascii=False))
            if generation_chars > MAX_GENERATE_CHARS:
                self._send(413, json.dumps({"error": "generation context is too large"})); return
            lease = self._acquire_paid_budget(sub, "master_generate")
            if lease is None:
                return
            emit = emitter(uid, req.get("jobId"))            # no-op if RTDB/jobId absent; else streams past the 60s proxy
            emit("status", "running")
            from engine import converse
            try:
                with lease:
                    out = converse.generate_master(req.get("name", "reference"), columns, rows,
                                                   instruction=req.get("instruction"), emit=emit)
            except Exception as e:                           # noqa: BLE001 — no key / SDK / bad JSON: let the client re-enable
                print(f"/api/master/generate degraded (503): {type(e).__name__}", flush=True)
                emit("error", "generate unavailable"); emit("status", "error")
                self._send(503, json.dumps({"error": "generate unavailable"})); return
            emit("result", out); emit("status", "done")      # terminal state -> RTDB (decoupled from this response)
            self._send(200, json.dumps(out))
        except Exception as e:                               # noqa: BLE001
            if emit is not None:
                try: emit("error", "internal server error"); emit("status", "error")
                except Exception: pass                       # noqa: BLE001 — streaming is best-effort
            print(f"/api/master/generate failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))

    # ---------------- /api/reason + /api/knowledge (Firebase auth + RTDB trace stream) ----------------
    def _post_world(self):
        emit = None                                          # so the except can stream a terminal error to RTDB
        # ONE timing scope per request, closed in the finally so a 4xx/5xx is measured like a 200.
        # The id comes from the caller when the orchestrator supplies one, so a chat turn's engine
        # calls can be correlated with the turn that issued them across the two services.
        timing_token = request_timing.begin(self.headers.get("X-Request-Id") or uuid.uuid4().hex[:12])
        try:
            req = self._read_json()
            if req is None:
                return
            try:
                req = validate_reason_request(req)
            except RequestValidationError as exc:
                self._send(exc.status_code, json.dumps({"error": str(exc)})); return
            if not self.headers.get("X-Request-Id"):
                request_timing.set_request_id(req.get("jobId"))   # jobId already ties a call to its chat turn
            sub, uid = _verify_principal(_bearer(self.headers, req))   # sub = VERIFIED user id (auth); uid = RTDB /runs key
            if not sub:
                self._send(401, json.dumps({"error": "sign in required (no valid Google token)"}))
                return
            allowed, retry_after = WORLD_RATE.allow(sub or self.client_address[0])
            if not allowed:
                self._send(429, json.dumps({"error": "request rate limit exceeded"}), retry_after=retry_after)
                return
            sheets = req["tables"]
            if sheets:
                tabs = [csv_table(s["data"], s["name"])
                        for i, s in enumerate(sheets[:MAX_SHEETS]) if isinstance(s, dict) and (s.get("data") or "").strip()]
            else:
                data = req["data"]
                if not data.strip():
                    self._send(200, json.dumps({"error": "no CSV (need {tables:[…], question})"})); return
                tabs = [csv_table(data, req["table"])]
            if not tabs:
                self._send(200, json.dumps({"error": "no CSV rows"})); return
            truncated = []
            for t in tabs:
                if len(t["rows"]) > MAX_ROWS:
                    truncated.append(f"{t['name']}: only the first {MAX_ROWS} rows were used ({len(t['rows'])} uploaded)")
                    t["rows"] = t["rows"][:MAX_ROWS]
            uploaded_count = len(tabs)
            references = master.relevant_tables(sub, tabs, MAX_SHEETS - len(tabs), MAX_ROWS)
            tabs.extend(references["tables"])
            reference_count = len(references["tables"])
            enrichment = None
            if ENRICHMENT is not None:
                from engine.enrichment.runtime import table_versions
                enrichment = ENRICHMENT.prepare(
                    tabs, req.get("question", ""), as_of=req.get("as_of"),
                    private_reference_versions=table_versions(references["tables"]),
                    table_budget=max(0, MAX_SHEETS - len(tabs)), row_budget=MAX_ROWS,
                )
                if enrichment.used:
                    tabs = list(enrichment.tables)
            # The WORKING Postgres schema is the CONVERSATION, not the user. A client-supplied
            # conversation id is honored ONLY after the ownership check (chat.user_conversation);
            # otherwise a new conversation is minted for the verified user. No conversation id
            # ever reaches the schema without passing through this authorization (no IDOR).
            try:
                conv = resolve_conversation(sub, req.get("conversation_id"), req.get("question", ""), sheets)
            except NotOwned:
                # 404 (not 403) to match GET /api/conversation — "not yours" and "absent" look identical (no enumeration).
                self._send(404, json.dumps({"error": "conversation not found"})); return
            except QuotaExceeded as exc:
                self._send(429, json.dumps({"error": str(exc)}), retry_after=60); return
            # DATASET SEMANTICS (engine/dataset_semantics.py): validate any incoming ops against the
            # UPLOADED tables, append them to the conversation's log, replay the full log into
            # effective metadata, and apply it (a currency claim synthesizes the code column the FX
            # machinery already consumes). Runs AFTER conversation authorization — ops persist only
            # on a conversation the caller owns — and BEFORE provenance/serve so the synthesized
            # column flows through the one existing conversion path. A bad op is a CLARIFY, never a
            # silent application and never a 500.
            semantics = []
            try:
                raw_ops = req.get("dataset_ops")
                attested = (not raw_ops or dataset_attestation.verify(
                    sub, raw_ops, self.headers.get(dataset_attestation.HEADER)))
                incoming = dataset_semantics.validate_ops(
                    raw_ops, tabs[:uploaded_count], attested=attested)
                if incoming:
                    ops_log = append_dataset_ops(
                        conv, incoming,
                        validate=lambda candidate: dataset_semantics.validate_replay(
                            candidate, tabs[:uploaded_count]),
                    )
                else:
                    ops_log = load_dataset_ops(conv) if req.get("conversation_id") else []
                    # A claim can survive while its sheet is detached. Re-check it when that sheet
                    # returns so a replacement upload cannot be silently shadowed by old metadata.
                    dataset_semantics.validate_replay(ops_log, tabs[:uploaded_count])
                # Apply only to the uploaded prefix. Saved/published reference tables are separate
                # trust domains even if a name collision somehow reaches this point; the mutated
                # upload dictionaries remain the same objects used by the full working table list.
                semantics = dataset_semantics.apply(tabs[:uploaded_count], ops_log)
            except (DatasetOpError, DatasetOpsLimitError) as exc:
                res = {"question": req.get("question", ""), "clarify": True, "reason": str(exc),
                       "conversation_id": conv,
                       "model": "engine - dataset semantics (op rejected)"}
                emit = emitter(uid, req.get("jobId"))
                stream_final(emit, res)
                self._send(200, json.dumps(res)); return
            provenance_context = ProvenanceContext(
                tabs, uploaded_count=uploaded_count, reference_count=reference_count,
                enrichment=enrichment, dataset_semantics=semantics,
            )
            emit = provenance_context.wrap_emitter(emitter(uid, req.get("jobId")))
            emit("conversation_id", conv)                    # stream it EARLY so the browser gets it even if the HTTP body is lost to a proxy timeout
            emit("status", "running")
            # lock_wait is measured SEPARATELY from serve: the global lock serializes the engine, so
            # queueing behind another request and doing the work are different problems with different
            # fixes, and one line has to tell them apart.
            with request_timing.span("lock_wait"):
                WORLD_LOCK.acquire()
            try:
                set_ctx(emit)                                # so the DEEP bridge build streams the cell→qid lookup live
                try:
                    serve_kwargs = {"emit": emit}
                    if enrichment is not None and enrichment.used:
                        serve_kwargs["explicit_fks"] = enrichment.explicit_fks
                    with request_timing.span("serve"):
                        res = MODEL.serve(
                            tabs, req.get("question", ""), conv, req.get("as_of"),
                            dataset_semantics=semantics, **serve_kwargs
                        )
                finally:
                    set_ctx(None)
            finally:
                WORLD_LOCK.release()
            res = provenance_context.decorate_response(res)
            if isinstance(res, dict):
                res["conversation_id"] = conv                # so the browser persists it for follow-up turns
            # Include an empty effective list after a clear: [] is the authoritative state that tells
            # the browser to remove a previously rendered conversation-metadata badge.
            if isinstance(res, dict) and (incoming or ops_log):
                res["dataset_semantics"] = semantics         # the UI badge + audit surface
                emit("dataset_semantics", semantics)
            if truncated and isinstance(res, dict):
                res.setdefault("warnings", []).extend(truncated)
            if references["warnings"] and isinstance(res, dict):
                res.setdefault("warnings", []).extend(references["warnings"])
            if enrichment is not None and isinstance(res, dict):
                if enrichment.used:
                    provenance = res.get("provenance")
                    if not isinstance(provenance, dict):
                        provenance = {"world": provenance} if provenance is not None else {}
                    provenance["enrichment"] = enrichment.provenance()
                    res["provenance"] = provenance
                if enrichment.warnings:
                    res.setdefault("warnings", []).extend(enrichment.warnings)
            stream_final(emit, res)                          # terminal state -> RTDB (decoupled from this response)
            self._send(200, json.dumps(res))
        except Exception as e:                           # noqa: BLE001
            if emit is not None:                         # don't leave the client stuck on 'running' — stream the error
                try:
                    emit("error", "internal server error"); emit("status", "error")
                except Exception:                        # noqa: BLE001
                    pass
            print(f"world request failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))
        finally:
            request_timing.emit("reason", status=getattr(self, "_status", None))
            request_timing.end(timing_token)

    # ---------------- /api/dimension (stateless, authenticated) ----------------
    def _post_dimension(self):
        try:
            req = self._read_json()
            if req is None:
                return
            sub, _uid = _verify_principal(_bearer(self.headers, None))
            if not sub:
                self._send(401, json.dumps({"error": "sign in required"})); return
            allowed, retry_after = DIM_RATE.allow(sub or self.client_address[0])
            if not allowed:
                self._send(429, json.dumps({"error": "request rate limit exceeded"}), retry_after=retry_after)
                return
            data = req.get("data", "")
            if not isinstance(data, str):
                self._send(400, json.dumps({"error": "CSV data must be text"})); return
            if not data.strip():
                self._send(400, json.dumps({"error": "no CSV (need {data, mode:'analyze'})"})); return
            tbl = csv_table(data, table_name(req.get("table", "data"), 0))
            if len(tbl["rows"]) > MAX_ROWS:
                tbl["rows"] = tbl["rows"][:MAX_ROWS]
            with DIM_LOCK:
                res = DIM_MODEL.analyze(tbl)
            self._send(200, json.dumps(res))
        except Exception as e:                               # noqa: BLE001
            print(f"dimension request failed: {type(e).__name__}", flush=True)
            self._send(500, json.dumps({"error": "internal server error"}))


def main():
    global MODEL, DIM_MODEL, ENRICHMENT
    from engine.knowledge import KnowledgeReasoner
    from engine.dimension import DimensionModel
    print("loading world reasoner (composition engine + unified Qwen + bge resolver + spaCy; LIVE Postgres)...",
          flush=True)
    MODEL = KnowledgeReasoner()
    from engine.enrichment import EnrichmentRuntime, SnapshotStore, deployment_dataset_allowlist
    from engine.pg import _pg
    ENRICHMENT = EnrichmentRuntime(
        SnapshotStore(_pg),
        enabled_datasets=deployment_dataset_allowlist(os.environ.get("ENRICHMENT_ACTIVE_DATASETS")),
    )
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
