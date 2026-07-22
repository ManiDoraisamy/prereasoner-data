"""tests/stub_engine.py — a fake PreReasoner engine for testing the layers ABOVE it (MCP server,
orchestrator, chat UI) WITHOUT a seeded world Postgres, model weights, or Docker.

It returns responses in the engine's EXACT documented shapes (mcp-now.md §0.3): an answer with a `views`
stack (the France=270 demo), a `clarify`, and an `error` — plus /api/dimension and /healthz. This is NOT
the real engine; it is a contract-shaped fixture. The real engine has its own tests (tests/test_*.py) and
is exercised end-to-end only against the deployed Cloud Run service.

Run: python -m tests.stub_engine   (binds 127.0.0.1:$STUB_PORT, default 8080)
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("STUB_PORT", "8080"))

_FRANCE = {
    "question": "total amount in France",
    "sql": ("SELECT SUM(orders.amount) AS total FROM customers "
            "JOIN orders ON orders.customer_id = customers.customer_id "
            "JOIN \"customers connected to wikipedia\" b ON b.column='city' AND lower(b.value)=lower(customers.city) "
            "JOIN knowledgebase.\"city\" ON knowledgebase.\"city\".qid = b.world_key "
            "WHERE knowledgebase.\"city\".country = 'Q142'"),
    "result": {"columns": ["total"], "rows": [[270]]},
    "views": [
        {"name": "base", "op": "world_join",
         "label": "join customers.city → knowledgebase.\"city\"",
         "sql": "JOIN knowledgebase.\"city\" ON qid = b.world_key",
         "columns": ["city", "country"],
         "rows": [["Paris", "Q142"], ["Lyon", "Q142"], ["Berlin", "Q183"]]},
        {"name": "filter", "op": "filter",
         "label": "filter country = France (Q142)",
         "sql": "WHERE knowledgebase.\"city\".country = 'Q142'",
         "columns": ["city", "amount"], "rows": [["Paris", 120], ["Lyon", 150]]},
        {"name": "agg", "op": "group_agg",
         "label": "SUM(amount)",
         "sql": "SELECT SUM(amount) AS total",
         "columns": ["total"], "rows": [[270]]},
    ],
    "meaning_join": "customers.city → knowledgebase.\"city\".country = France (Q142)",
    "model": "engine - composed view stack (STUB)",
    "error": None,
}

_CLARIFY = {
    "question": "",
    "clarify": True,
    "proposed": "total amount by country (there is no 'region' column in your data)",
    "dropped": ["region"],
    "bindings": [],
    "original_sql": "SELECT SUM(amount) FROM orders GROUP BY region  -- 'region' never resolved",
    "model": "engine - clarify (STUB)",
}


def _answer(question: str) -> dict:
    q = (question or "").lower()
    if "region" in q:  # an intentionally ambiguous term -> the clarify gate
        return dict(_CLARIFY, question=question)
    if "france" in q:
        return dict(_FRANCE, question=question)
    # generic answer for anything else
    return {
        "question": question,
        "sql": "SELECT SUM(amount) AS total FROM orders",
        "result": {"columns": ["total"], "rows": [[480]]},
        "views": [{"name": "agg", "op": "group_agg", "label": "SUM(amount)",
                   "sql": "SELECT SUM(amount) AS total FROM orders",
                   "columns": ["total"], "rows": [[480]]}],
        "model": "engine - composed view stack (STUB)",
        "error": None,
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", "/api/healthz"):
            self._send(200, {"ok": True, "reason": True, "world": True, "dimension": True, "stub": True})
        else:
            self._send(200, {"stub": "POST /api/reason | /api/knowledge | /api/dimension"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(200, {"error": "bad json"}); return
        path = self.path.rstrip("/")
        if path in ("/api/reason", "/api/knowledge"):
            self._send(200, _answer(req.get("question", "")))
        elif path == "/api/dimension":
            self._send(200, {
                "columns": [
                    {"name": "city", "evolution": [{"populated_place": 0.71, "city": 0.93}]},
                    {"name": "amount", "evolution": [{"is_num": 0.96}]},
                ],
                "rows": [], "cols": ["city", "amount"], "n_layers": 1,
                "model": "engine - dimension readout (STUB)",
            })
        else:
            self._send(404, {"error": "POST /api/reason | /api/knowledge | /api/dimension"})


def main():
    print(f"STUB engine on http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
