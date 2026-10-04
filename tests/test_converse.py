"""Shared deterministic answer presentation and explicit reference generation contracts."""
from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import patch

from engine import reference_generation as converse, llm


def _gen(chunks, columns, rows, instruction=None, emit=None):
    """Call generate_master with a FAKE Gemini stream that yields `chunks` (a str is sent as one chunk; a
    list is streamed piece by piece, letting a test split JSONL mid-line). Returns (out, captured) where
    captured has the user/system prompt the stream was asked for."""
    chunks = [chunks] if isinstance(chunks, str) else list(chunks)
    cap = {}

    def stream_text(*, system, prompt, max_output_tokens, timeout_seconds=60.0):
        cap["user"] = prompt; cap["system"] = system; cap["max_output_tokens"] = max_output_tokens
        return iter(chunks)

    with patch.object(llm, "stream_text", stream_text):
        return converse.generate_master("series", columns, rows, instruction=instruction, emit=emit), cap


def test_reply_presents_the_computed_answer_without_external_processing():
    from engine.answer_presentation import terminal_reply
    with patch.object(llm, 'generate_text', side_effect=AssertionError('Results must not reach Gemini')):
        assert terminal_reply({'status': 'answered', 'answer': {'columns': ['net_amount'], 'rows': [['876.50']]}}) == '876.50'
        assert terminal_reply({'status': 'answered', 'answer': {'rows': [[-120]]}}) == '-120'
        assert terminal_reply({'status': 'clarify', 'clarify': {'reason': 'Choose a measure'}}) == 'Choose a measure'
        reason = 'GBP can mean converting every order into GBP or keeping only the orders recorded in GBP'
        # One sentence ends before the question: "…recorded in GBP Which interpretation…" ran on (2026-10-04).
        assert terminal_reply({'status': 'clarify', 'clarify': {'reason': reason}}) == reason + '. Which interpretation should I use?'
        assert terminal_reply({'status': 'clarify', 'clarify': {'reason': reason + '.'}}) == reason + '. Which interpretation should I use?'
        assert terminal_reply({'status': 'clarify', 'clarify': {'reason': 'Which Amount column should I use?'}}) == 'Which Amount column should I use?'
        rows = [['Ava', 'Travel'], ['Cleo', None]]
        assert terminal_reply({'status': 'answered', 'answer': {'columns': ['customer', 'category'], 'rows': rows}}) == '- customer: Ava; category: Travel\n- customer: Cleo; category: Not recorded'
        # A long answer lists its first rows and how many there are: the Sheets sidebar has no workbook, and
        # "shown in the workbook" left a 160-row total with no number (2026-10-05). Wide rows list fewer.
        large = {'status': 'answered', 'answer': {'columns': ['customer', 'category'], 'rows': rows * 6}}
        listed = terminal_reply(large)
        assert listed.startswith('- customer: Ava; category: Travel\n- customer: Cleo; category: Not recorded\n')
        assert listed.count('- customer: ') == 10 and listed.endswith('\n\nThe first 10 of 12 rows.'), listed
        wide = {'status': 'answered', 'answer': {'columns': ['note'], 'rows': [['x' * 900]] * 3}}
        assert terminal_reply(wide).endswith('\n\nThe first 2 of 3 rows.')
        huge = {'status': 'answered', 'answer': {'columns': ['note'], 'rows': [['x' * 2100]] * 3}}
        assert terminal_reply(huge) == 'The answer has 3 rows, each too long to show here.'
        assert terminal_reply({'status': 'error', 'error': 'Invalid date'}) == 'Invalid date'
        assert 'again' in terminal_reply({'status': 'error', 'error': 'Engine is busy; retry shortly'})


def test_an_engine_failure_is_one_sentence_the_user_can_act_on():
    """A 500 reached chat users as the words "internal server error", a lost connection as the engine's
    internal address, and a clarification's proposal as "Try: total commission_percent Which
    interpretation should I use?" (2026-10-04)."""
    from engine import answer_presentation as presentation
    from engine.answer_presentation import terminal_reply
    failures = {
        'internal server error': {'status': 'error', 'error': 'internal server error', 'http_status': 500},
        'unreachable': {'status': 'error', 'unreachable': True,
                        'error': 'could not reach the Prereasoner engine at https://engine.internal: refused'},
        'gateway page': {'status': 'error', 'http_status': 502, 'error': 'engine returned non-JSON (HTTP 502): <html>'},
        'exception text': {'status': 'error', 'error': "KeyError: 'amount'"},
        'payload echo': {'status': 'error', 'error': 'no CSV (need {tables:[…], question})'},
    }
    for name, shaped in failures.items():
        reply = terminal_reply(shaped)
        assert reply == presentation.UNAVAILABLE_REPLY, (name, reply)
    assert terminal_reply({'status': 'error', 'error': 'sign in required', 'http_status': 401}) == presentation.SIGN_IN_REPLY
    assert terminal_reply({'status': 'error', 'error': 'request rate limit exceeded', 'http_status': 429}) == presentation.BUSY_REPLY
    assert terminal_reply({'status': 'error', 'error': 'conversation limit reached', 'http_status': 429}) \
        == presentation.CONVERSATION_LIMIT_REPLY
    assert terminal_reply({'status': 'error', 'error': 'conversation has too many analyses'}) \
        == presentation.FULL_CONVERSATION_REPLY
    # Text the engine wrote for the user still reaches them.
    assert terminal_reply({'status': 'error', 'error': 'The interpretation could not be verified. Please retry.'}) \
        == 'The interpretation could not be verified. Please retry.'
    # A proposed question is quoted as one the user can send, with no interpretation prompt after it.
    assert terminal_reply({'status': 'clarify', 'clarify': {
        'reason': 'I need one more detail before I can answer that.', 'proposed': 'total commission_percent'}}) \
        == 'I need one more detail before I can answer that. Try asking: “total commission_percent”'
    assert terminal_reply({'status': 'clarify', 'clarify': {'reason': 'Which amount', 'proposed': 'total amount'}}) \
        == 'Which amount. Try asking: “total amount”'


def test_a_blank_answer_or_a_link_in_a_cell_is_shown_as_data():
    """Serving writes a missing value as "", which answered a blank cell with an empty reply, and a
    cell holding "[x](https://…)" rendered as a live link in the chat (2026-10-04)."""
    from engine.answer_presentation import terminal_reply
    for blank in ('', '   ', None):
        assert terminal_reply({'status': 'answered', 'answer': {'columns': ['note'], 'rows': [[blank]]}}) \
            == 'No value was recorded for the matching rows.'
    rows = [['Ava', ''], ['Cleo', 'Travel']]
    assert terminal_reply({'status': 'answered', 'answer': {'columns': ['customer', 'category'], 'rows': rows}}) \
        == '- customer: Ava; category: Not recorded\n- customer: Cleo; category: Travel'
    link = '[Reset your password](https://example.com/login)'
    single = terminal_reply({'status': 'answered', 'answer': {'columns': ['note'], 'rows': [[link]]}})
    listed = terminal_reply({'status': 'answered', 'answer': {'columns': ['customer', 'note'],
                                                              'rows': [['Ava', link], ['Bo', 'ok']]}})
    for reply in (single, listed):
        assert '](' not in reply and 'Reset your password' in reply and 'https://example.com/login' in reply


def test_a_listed_answer_reads_as_a_one_number_answer_does():
    """"which country has the most deposits?" was answered "- country: Switzerland; sum: 1550" one
    line under Switzerland's total as "1,550", and the top US-dollar city as "total_usd: 3495"
    (Chrome gate, 2026-10-04). A computed column is written as a one-number answer is; a value taken
    from the data, a year or an ID, as it is."""
    from engine.answer_presentation import terminal_reply
    country = {'kind': 'reference', 'source': 'Wikidata', 'column': 'country'}
    total = {'kind': 'derived', 'source': 'Prereasoner', 'operation': 'SUM', 'measure': True}
    top = {'status': 'answered', 'answer': {'columns': ['country', 'sum'], 'rows': [['Switzerland', 1550]],
                                            'column_provenance': [country, total]}}
    assert terminal_reply(top) == 'country: Switzerland; sum: 1,550'
    usd = {'specification': 'currency', 'status': 'satisfied', 'realization': 'converted', 'target': 'USD'}
    city = {'status': 'answered', 'calculations': [usd],
            'answer': {'columns': ['city', 'total_usd'], 'rows': [['Cleveland', 3495]],
                       'column_provenance': [{'kind': 'input', 'source': 'upload'}, total]}}
    assert terminal_reply(city) == 'city: Cleveland; total USD: 3,495.00'
    year = {'kind': 'derived', 'source': 'Prereasoner', 'operation': 'DatePart'}
    count = {'kind': 'derived', 'source': 'Prereasoner', 'operation': 'COUNT', 'measure': True}
    average = {'kind': 'derived', 'source': 'Prereasoner', 'operation': 'AVG', 'measure': True}
    yearly = {'status': 'answered', 'answer': {
        'columns': ['order_year', 'count', 'avg'], 'rows': [[2026, 1200, '4.66666666666666666667'], [2025, 3, 2]],
        'column_provenance': [year, count, average]}}
    assert terminal_reply(yearly) == ('- order year: 2026; count: 1,200; avg: 4.67\n'
                                      '- order year: 2025; count: 3; avg: 2')
    products = {'status': 'answered', 'answer': {'columns': ['product_name'], 'rows': [['Delta'], ['Omega']],
                                                 'column_provenance': [{'kind': 'input', 'source': 'upload'}]}}
    assert terminal_reply(products) == '- Delta\n- Omega'
    # Without a record for every column, nothing is taken for a computed number.
    bare = {'status': 'answered', 'answer': {'columns': ['order_year', 'count'], 'rows': [[2026, 1200]]}}
    assert terminal_reply(bare) == 'order year: 2026; count: 1200'


def test_an_answer_from_one_of_several_tables_says_which():
    """A customer's Stripe workbook, 2026-10-04: three subscription tabs with one layout, and the total by Plan
    and Currency came from the first with nothing to say so: it read as the workbook's total. The reply names the
    table it read and the others that could answer."""
    from engine.answer_presentation import terminal_reply
    answer = {'columns': ['Plan', 'sum'], 'rows': [['price_a', 20]]}
    copies = {'read': ['NT'], 'others': ['SI', 'FF']}
    assert terminal_reply({'status': 'answered', 'answer': answer, 'layout_copies': copies}) == (
        'Plan: price_a; sum: 20\n\nFrom NT. SI and FF could answer this too; name one in your question to '
        'read that one instead.')
    one = {'status': 'answered', 'answer': answer, 'layout_copies': {'read': ['NT'], 'others': ['SI']}}
    assert terminal_reply(one).endswith('From NT. SI could answer this too; name it in your question to read '
                                        'it instead.')
    assert terminal_reply({'status': 'answered', 'answer': answer}) == 'Plan: price_a; sum: 20'


def test_a_malformed_converse_body_is_a_client_error_not_a_500():
    """`clarify: true`, `answer: "42"` and a row of 5 raised inside the renderer, and /api/converse
    answered 500 (review, 2026-10-04)."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from engine import server

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(body):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/converse", data=json.dumps(body).encode(),
            method="POST", headers={"Authorization": "Bearer token", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        with patch.object(server, "_verify_principal", lambda _token: ("sub-1", "uid-1")):
            for body in ({"clarify": True}, {"answer": "42"}, {"answer": {"rows": [5]}},
                         {"answer": {"columns": "n", "rows": [[5]]}}, {"error": ["boom"]},
                         {"answer": {"rows": [[5]]}, "calculations": "currency"}, {"answer": None, "unit": 3}):
                status, reply = post(body)
                assert status == 400 and "wrong shape" in reply["error"], (body, status, reply)
            assert post({"answer": {"columns": ["n"], "rows": [[5000]]}}) == (200, {"reply": "5,000"})
            assert post({"clarify": {"reason": "Which Amount column should I use?"}}) \
                == (200, {"reply": "Which Amount column should I use?"})
    finally:
        httpd.shutdown()


def test_unavailable_gemini_reaches_reference_generation_as_llm_unavailable():
    with patch.dict('os.environ', {'EXTERNAL_LLM_ENABLED': 'false'}):
        try:
            converse.generate_master('series', ['series'], [['Doyle']])
        except llm.LLMUnavailable:
            pass
        else:
            raise AssertionError('Expected unavailable generation')


def _jsonl(cols, rows):
    return json.dumps({"columns": cols}) + "\n" + "".join(json.dumps({"row": r}) + "\n" for r in rows)


def test_preserves_existing_and_fills_empty():
    # Doyle.category is already "Detective Fiction"; the model tries to OVERWRITE it -> it must be PRESERVED.
    jl = _jsonl(["series", "category", "author"],
                [["Doyle", "OVERWRITE", "Arthur Conan Doyle"], ["Christie", "Mystery", "Agatha Christie"]])
    out, _ = _gen(jl, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""], ["Christie", "", ""]])
    assert out["rows"][0] == ["Doyle", "Detective Fiction", "Arthur Conan Doyle"], out["rows"]
    assert out["rows"][1] == ["Christie", "Mystery", "Agatha Christie"], out["rows"]


def test_preserves_by_column_name_even_if_model_reorders():
    jl = _jsonl(["series", "author", "category"], [["Doyle", "someone else", "WRONG"]])   # reordered header
    out, _ = _gen(jl, ["series", "category", "author"], [["Doyle", "Detective Fiction", ""]])
    row = dict(zip(out["columns"], out["rows"][0]))
    assert row["category"] == "Detective Fiction", out          # preserved despite reorder + overwrite attempt
    assert row["author"] == "someone else", out                 # empty cell filled


def test_instruction_and_current_rows_reach_the_prompt():
    _, cap = _gen(_jsonl(["series"], [["Doyle"]]), ["series"], [["Doyle"]],
                  instruction="Fill only the empty cells.")
    assert "Fill only the empty cells." in cap["user"], cap["user"]
    assert "Current rows" in cap["user"], cap["user"]


def test_entity_only_adds_columns():
    out, _ = _gen(_jsonl(["series", "genre", "origin"], [["Doyle", "Detective", "UK"]]), ["series"], [["Doyle"]])
    assert out["columns"] == ["series", "genre", "origin"], out["columns"]
    assert out["rows"][0] == ["Doyle", "Detective", "UK"], out["rows"]


def test_ragged_row_normalized_to_width():
    jl = json.dumps({"columns": ["series", "a", "b"]}) + "\n" + json.dumps({"row": ["Doyle", "x"]}) + "\n"
    out, _ = _gen(jl, ["series"], [["Doyle"]])
    assert out["rows"][0] == ["Doyle", "x", ""], out["rows"]


def test_streaming_emits_header_then_rows_in_order():
    jl = _jsonl(["series", "genre"], [["Doyle", "Detective"], ["Christie", "Mystery"]])
    ev = []
    _gen(jl, ["series"], [["Doyle"], ["Christie"]], emit=lambda *a: ev.append(a))
    nodes = [n for n, *_ in ev]
    assert nodes[0] == "mcols", ev
    assert nodes[1] == "mrows/0000" and nodes[2] == "mrows/0001", ev      # zero-padded keys keep RTDB child order
    assert dict((n, v) for n, v in ev)["mrows/0000"] == ["Doyle", "Detective"], ev


def test_incremental_parse_survives_chunk_splits_midline():
    jl = _jsonl(["series", "genre"], [["Doyle", "Detective"], ["Christie", "Mystery"]])
    chunks = [jl[i:i + 5] for i in range(0, len(jl), 5)]        # arbitrary 5-char chunks, split across + inside lines
    out, _ = _gen(chunks, ["series"], [["Doyle"], ["Christie"]])
    assert out["columns"] == ["series", "genre"], out
    assert out["rows"] == [["Doyle", "Detective"], ["Christie", "Mystery"]], out


def test_fallback_when_model_returns_one_nested_json_blob():
    blob = json.dumps({"columns": ["series", "genre"], "rows": [["Doyle", "Detective"]]})   # ignored JSONL -> one blob
    out, _ = _gen(blob, ["series"], [["Doyle"]])
    assert out["rows"] == [["Doyle", "Detective"]], out


def test_fallback_handles_a_code_fence():
    blob = "```json\n" + json.dumps({"columns": ["series", "g"], "rows": [["Doyle", "x"]]}) + "\n```"
    out, _ = _gen(blob, ["series"], [["Doyle"]])
    assert out["rows"] == [["Doyle", "x"]], out


def test_no_entities_returns_empty_without_calling_the_model():
    out, cap = _gen("{}", ["series"], [["", ""]])              # blank entity -> nothing to do, no model call
    assert out["rows"] == [], out
    assert "user" not in cap, "the model was called for an empty entity list"


def test_external_llm_requires_deployment_authorization():
    import engine.config as cfg

    previous = os.environ.get("EXTERNAL_LLM_ENABLED")
    try:
        os.environ.pop("EXTERNAL_LLM_ENABLED", None)
        assert not cfg.external_llm_enabled()
        os.environ["EXTERNAL_LLM_ENABLED"] = "true"
        assert cfg.external_llm_enabled()
    finally:
        if previous is None:
            os.environ.pop("EXTERNAL_LLM_ENABLED", None)
        else:
            os.environ["EXTERNAL_LLM_ENABLED"] = previous


def test_trace_deletion_filters_by_conversation_and_supports_delete_all():
    import engine.trace as trace

    records = {
        "job-a": {"conversation_id": "c_" + "a" * 32},
        "job-b": {"conversation_id": "c_" + "b" * 32},
    }

    class _Ref:
        def __init__(self, job=None):
            self.job = job
            self.match = None
        def order_by_child(self, key):
            assert key == "conversation_id"
            return self
        def equal_to(self, value):
            self.match = value
            return self
        def get(self, shallow=False):
            if self.match is not None:
                return {key: value for key, value in records.items()
                        if value.get("conversation_id") == self.match}
            return {key: True for key in records} if shallow else dict(records)
        def child(self, job):
            return _Ref(job)
        def delete(self):
            if self.job is None:
                records.clear()
            else:
                records.pop(self.job, None)

    fake_admin = types.ModuleType("firebase_admin")
    fake_admin.db = types.SimpleNamespace(reference=lambda path: _Ref())
    previous_module = sys.modules.get("firebase_admin")
    previous_url = trace.RTDB_URL
    previous_ensure = trace.ensure_app
    try:
        sys.modules["firebase_admin"] = fake_admin
        trace.RTDB_URL = "https://example.invalid"
        trace.ensure_app = lambda: None
        assert trace.delete_traces("uid", "c_" + "a" * 32) == 1
        assert set(records) == {"job-b"}
        assert trace.delete_traces("uid") == 1
        assert not records
    finally:
        trace.RTDB_URL = previous_url
        trace.ensure_app = previous_ensure
        if previous_module is None:
            sys.modules.pop("firebase_admin", None)
        else:
            sys.modules["firebase_admin"] = previous_module


TESTS = [
    test_reply_presents_the_computed_answer_without_external_processing,
    test_an_engine_failure_is_one_sentence_the_user_can_act_on,
    test_a_blank_answer_or_a_link_in_a_cell_is_shown_as_data,
    test_a_listed_answer_reads_as_a_one_number_answer_does,
    test_an_answer_from_one_of_several_tables_says_which,
    test_a_malformed_converse_body_is_a_client_error_not_a_500,
    test_unavailable_gemini_reaches_reference_generation_as_llm_unavailable,
    test_preserves_existing_and_fills_empty,
    test_preserves_by_column_name_even_if_model_reorders,
    test_instruction_and_current_rows_reach_the_prompt,
    test_entity_only_adds_columns,
    test_ragged_row_normalized_to_width,
    test_streaming_emits_header_then_rows_in_order,
    test_incremental_parse_survives_chunk_splits_midline,
    test_fallback_when_model_returns_one_nested_json_blob,
    test_fallback_handles_a_code_fence,
    test_no_entities_returns_empty_without_calling_the_model,
    test_external_llm_requires_deployment_authorization,
    test_trace_deletion_filters_by_conversation_and_supports_delete_all,
]


def main():
    failed = []
    for t in TESTS:
        try:
            t(); print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed.append(t.__name__); print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\nConverse: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
