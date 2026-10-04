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
        questions = [{"text": "How many orders are in Orders?", "sheet": 0, "columns": [0]},
            {"text": "Which cities have the most orders?", "sheet": 0, "columns": [0]},
            {"text": "How does amount vary by city?", "sheet": 0, "columns": [1, 0]}]
        with patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": questions})) as call:
            result = starter_questions(self.schema)
        self.assertEqual(result["questions"], [question["text"] for question in questions])
        self.assertEqual(result["source"], "gemini")
        self.assertEqual(json.loads(call.call_args.kwargs["prompt"]), self.schema)
        self.assertEqual(call.call_args.kwargs["timeout_seconds"], 25)
        self.assertIn("synonyms or another language", call.call_args.kwargs["system"])

    def test_synonym_or_translated_wording_is_accepted_when_schema_references_are_valid(self):
        questions = [{"text": "Wie viele Kundinnen und Kunden gibt es?", "sheet": 0, "columns": [0]},
            {"text": "Which cities contribute the most?", "sheet": 0, "columns": [0]},
            {"text": "Compare the amount between cities.", "sheet": 0, "columns": [1, 0]}]
        with patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": questions})):
            self.assertEqual(starter_questions(self.schema)["source"], "gemini")

    def test_outage_and_malformed_output_fail_closed_without_generic_questions(self):
        for failure in (LLMUnavailable("offline"), ValueError("bad json")):
            with patch("orchestrator.suggestions.llm.generate_text", side_effect=failure):
                with self.assertRaises(type(failure)):
                    starter_questions(self.schema)
        for raw in ('null', '{"questions":null}', '{"questions": [1,2,3]}',
                    json.dumps({"questions": [
                        {"text": "invalid sheet", "sheet": 5, "columns": [0]},
                        {"text": "wrong field", "sheet": 0, "columns": [8]},
                        {"text": "bad reference", "sheet": 1, "columns": [0]}]})):
            with patch("orchestrator.suggestions.llm.generate_text", return_value=raw):
                with self.assertRaises(ValueError):
                    starter_questions(self.schema)

    def test_duplicate_model_questions_are_rejected(self):
        duplicate = {"text": "How many rows are in Orders?", "sheet": 0, "columns": [0]}
        with patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": [duplicate] * 3})):
            with self.assertRaisesRegex(ValueError, "distinct"):
                starter_questions(self.schema)

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

    def test_http_caches_only_valid_gemini_prompts_and_rejects_values(self):
        from orchestrator import server
        with server._SUGGESTIONS_LOCK:
            server._SUGGESTIONS.clear()
        questions = [{"text": "How many orders are in Orders?", "sheet": 0, "columns": [0]},
            {"text": "Which cities have the most orders?", "sheet": 0, "columns": [0]},
            {"text": "How does amount vary by city?", "sheet": 0, "columns": [1, 0]}]
        with self.service() as url, patch("engine.auth.verified_identity", return_value=("suggestions-test", None)), \
                patch("orchestrator.suggestions.llm.generate_text", return_value=json.dumps({"questions": questions})) as generate:
            response = httpx.post(url, json=self.schema)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["source"], "gemini")
            self.assertEqual(httpx.post(url, json=self.schema).json(), response.json())
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(httpx.post(url, json=dict(self.schema, values=["private"])).status_code, 400)
            self.assertEqual(generate.call_count, 1)

    def test_http_does_not_cache_gemini_failures(self):
        from orchestrator import server
        with server._SUGGESTIONS_LOCK:
            server._SUGGESTIONS.clear()
        with self.service() as url, patch("engine.auth.verified_identity", return_value=("suggestions-outage", None)), \
                patch("orchestrator.suggestions.llm.generate_text", side_effect=LLMUnavailable("offline")) as generate:
            self.assertEqual(httpx.post(url, json=self.schema).status_code, 503)
            self.assertEqual(httpx.post(url, json=self.schema).status_code, 503)
            self.assertEqual(generate.call_count, 2)


if __name__ == "__main__":
    unittest.main()
