"""Schema-only, natural-language sheet suggestions; no cell values or formulas cross the boundary."""
import json
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import threading
import httpx

from orchestrator.suggestions import validate_schema, starter_questions
from engine.llm import LLMUnavailable


class SuggestionsTests(unittest.TestCase):
    def setUp(self):
        self.schema = validate_schema({"sheets": [{"name": "Orders", "columns": ["city", "amount"]},
            {"name": "Notes", "columns": []}], "active_sheet": "Orders", "scope": ["Orders"]})

    def test_only_metadata_is_accepted(self):
        for extra in ("values", "data", "history", "formula", "message"):
            with self.assertRaises(ValueError):
                validate_schema(dict(self.schema, **{extra: "private"}))
            body = json.loads(json.dumps(self.schema)); body["sheets"][0][extra] = "private"
            with self.assertRaises(ValueError):
                validate_schema(body)

    def test_exactly_three_natural_language_questions_and_only_schema_is_sent(self):
        questions = ["How many orders are in Orders?", "Which cities have the most orders in Orders?",
            "How does amount vary by city in Orders?"]
        with patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": questions})) as call:
            result = starter_questions(self.schema)
        self.assertEqual(result["questions"], questions)
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(json.loads(call.call_args.kwargs["prompt"]), self.schema)
        self.assertEqual(call.call_args.kwargs["timeout_seconds"], 12)
        self.assertTrue(all(isinstance(question, str) for question in result["questions"]))

    def test_outage_and_malformed_output_have_three_safe_defaults(self):
        for failure in (LLMUnavailable("offline"), ValueError("bad json")):
            with patch("orchestrator.suggestions.llm.generate_text", side_effect=failure):
                result = starter_questions(self.schema)
            self.assertEqual(len(result["questions"]), 3)
            self.assertEqual(result["source"], "schema")
        for raw in ('null', '{"questions":null}', '{"questions": [1,2,3]}',
                    '{"questions": ["off-topic question", "another question", "third question"]}'):
            with patch("orchestrator.suggestions.llm.generate_text", return_value=raw):
                self.assertEqual(starter_questions(self.schema)["source"], "schema")

    def test_duplicate_or_unanchored_model_questions_use_schema_fallback(self):
        for questions in (["How many rows are in Orders?", "How many rows are in Orders?", "How many rows are in Orders?"],
                          ["How many orders are in Orders?", "Which cities are common in Orders?", "What is the weather today?"]):
            with patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": questions})):
                result = starter_questions(self.schema)
            self.assertEqual(result["source"], "schema")
            self.assertEqual(len(set(result["questions"])), 3)

    @contextmanager
    def service(self):
        from orchestrator.server import H
        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/chat/suggestions"
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_authentication_happens_before_generation(self):
        with self.service() as url, patch("engine.auth.verified_identity", return_value=(None, None)), \
                patch("orchestrator.suggestions.llm.generate_text") as generate:
            response = httpx.post(url, json=self.schema)
            self.assertEqual(response.status_code, 401)
            generate.assert_not_called()

    def test_http_cache_and_invalid_values(self):
        from orchestrator import server
        with server._SUGGESTIONS_LOCK:
            server._SUGGESTIONS.clear()
        with self.service() as url, patch("engine.auth.verified_identity", return_value=("suggestions-test", None)), \
                patch("orchestrator.suggestions.llm.generate_text", side_effect=LLMUnavailable("offline")) as generate:
            response = httpx.post(url, json=self.schema)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["questions"]), 3)
            self.assertEqual(httpx.post(url, json=self.schema).json(), response.json())
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(httpx.post(url, json=dict(self.schema, values=["private"])).status_code, 400)
            self.assertEqual(generate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
