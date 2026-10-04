"""test_orchestrator.py — the Gemini orchestrator's routing discipline (docs/MCP.md), against the
in-process STUB engine so the orchestrator->MCP->engine loop is exercised without a seeded world
Postgres. Every model call goes to live Vertex AI Gemini through engine/llm.py.

GATED on GOOGLE_CLOUD_PROJECT plus Application Default Credentials (`gcloud auth application-default
login`), the way the engine tests gate on KB_PG_PASSWORD: either absent => the suite self-skips with exit 0
rather than failing, so CI without Google Cloud access stays green. engine.config loads the repo .env.

For a hosted-release gate, set REQUIRE_ORCHESTRATOR_TESTS=1 so missing Gemini access fails instead of
skipping.
Run: python -m tests.test_orchestrator
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

P = 0
F = 0


def ok(cond, msg):
    global P, F
    if cond:
        P += 1
        print(f"  PASS  {msg}")
    else:
        F += 1
        print(f"  FAIL  {msg}")


def _gemini_missing():
    """Why this suite cannot reach Gemini, or "" when it can."""
    from engine import config
    if not config.GOOGLE_CLOUD_PROJECT:
        return "GOOGLE_CLOUD_PROJECT not set"
    try:
        import google.auth
        google.auth.default()
    except Exception as exc:  # noqa: BLE001 — any credential lookup failure means no Gemini access
        return f"no Application Default Credentials ({type(exc).__name__})"
    return ""


TABLES = [
    {"name": "customers", "data": "customer_id,city\n1,Paris\n2,Lyon\n3,Berlin\n"},
    {"name": "orders", "data": "order_id,customer_id,amount\n10,1,120\n11,2,150\n12,3,90\n"},
]


def _start_stub(port):
    from tests.stub_engine import H
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    missing = _gemini_missing()
    if missing:
        if os.environ.get("REQUIRE_ORCHESTRATOR_TESTS") == "1":
            print(f"test_orchestrator: FAIL ({missing}; Gemini access is required for this release gate)")
            sys.exit(1)
        print(f"test_orchestrator: SKIP ({missing})")
        sys.exit(0)

    from engine import config
    from orchestrator.orchestrator import TOOL_EXHAUSTED_REPLY, run_chat

    port = 8812
    base = f"http://127.0.0.1:{port}"
    srv = _start_stub(port)

    async def chat(msg, history=None, tables=None):
        return await run_chat(msg, tables or TABLES, history or [], engine_base_url=base,
                              bearer_token=None, model=config.llm_model(),
                              project=config.GOOGLE_CLOUD_PROJECT, location=config.GEMINI_LOCATION)

    try:
        # Rule 1 — a truth-bearing number buried in a strategic question must come from the tool.
        print("[1] truth-bearing number routes to the tool")
        r1 = asyncio.run(chat("Does our French revenue justify hiring in Europe? Give me the French total."))
        answered = [t for t in r1["traces"] if (t["engine"] or {}).get("status") == "answered"]
        ok(len(answered) >= 1, "at least one prereasoner_query call was made")
        ok("270" in r1["reply"],
           f"the French total (270) from the tool appears in the reply (got {r1['reply']!r})")

        # Rule 1b — a standalone question reaches the engine in the user's words (prompt rule 3).
        # A rewrite that dropped "in US dollars" shipped an unconverted total on 2026-09-06. The
        # stub echoes the received question into the trace, so this asserts at the exact boundary.
        print("[1b] standalone question passes through with its conversion intact")
        standalone = "total amount in France in US dollars"
        r1b = asyncio.run(chat(standalone))
        sent = [t.get("question", "") for t in r1b["traces"]]
        ok(any(q == standalone for q in sent),
           f"the engine receives the standalone question verbatim (got {sent})")

        # Rule 1b also covers qualifier families that a currency-specific code guard could not.
        print("[1b] standalone grouping, limit, and time qualifiers pass through verbatim")
        qualified = "top 3 customers by total amount per month since January 2025"
        rq = asyncio.run(chat(qualified))
        sent_q = [t.get("question", "") for t in rq["traces"]]
        ok(any(q == qualified for q in sent_q),
           f"the engine receives all standalone qualifiers verbatim (got {sent_q})")

        # Rule 1c — a follow-up rewrite carries the conversation's qualifiers (prompt rule 4):
        # after a France-in-USD turn, "how about Germany?" must become a Germany question that
        # still converts to USD. This is the case no per-dimension code guard could cover.
        print("[1c] follow-up rewrite carries the USD qualifier")
        r1c = asyncio.run(chat("how about Germany?", history=[
            {"role": "user", "content": "total amount in France in US dollars"},
            {"role": "assistant", "content": "Your total for France comes to about 1,127 in US dollars."}]))
        sent_c = [t.get("question", "") for t in r1c["traces"]]
        ok(any("german" in q.lower() and "us dollar" in q.lower() and "france" not in q.lower()
               for q in sent_c),
           f"the rewritten follow-up changes the country and keeps the USD qualifier (got {sent_c})")

        # A repeated short value is still a data question. This exact shape regressed in the live
        # customer-orders demo: Sonnet treated "how about Belgium?" as conversational confirmation
        # after already answering Belgium, so no new numeric engine result was produced.
        print("[1c] repeated short follow-up still calls the engine")
        r1e = asyncio.run(chat("how about Belgium?", history=[
            {"role": "user", "content": "total amount in Belgium in US dollars"},
            {"role": "assistant", "content": "Your total for Belgium comes to about $374 in US dollars."},
        ]))
        sent_e = [t.get("question", "") for t in r1e["traces"]]
        ok(len(sent_e) == 1 and "belgium" in sent_e[0].lower() and "us dollar" in sent_e[0].lower(),
           f"the repeated short follow-up reaches the engine with its inherited query (got {sent_e})")
        ok("did you mean a different country" not in r1e["reply"].lower(),
           "a repeated short follow-up cannot be downgraded to a meta clarification")

        # The same inheritance rule applies when the follow-up restates the metric in natural
        # language. The payment-commissions demo regressed by asking whether "how much commission"
        # meant a total or a rate, even though the preceding turn had already selected total amount.
        print("[1c] metric follow-up inherits the prior measure")
        r1f = asyncio.run(chat("how much commission came from cards?", history=[
            {"role": "user", "content": "total commission amount for card payments"},
            {"role": "assistant", "content": "The commission on card payments adds up to 9.28."},
        ]))
        sent_f = [t.get("question", "") for t in r1f["traces"]]
        ok(len(sent_f) == 1 and "commission" in sent_f[0].lower() and "card" in sent_f[0].lower(),
           f"the metric follow-up reaches the engine with its prior subject (got {sent_f})")
        ok("total or a rate" not in r1f["reply"].lower(),
           "an established metric cannot be reopened as a metric-choice clarification")

        # Customer-orders Chrome report: "in GBP for the whole of Europe?" must change the
        # geographic scope and output unit, not select only source rows already denominated in GBP.
        print("[1c] European geography and GBP output units remain separate")
        europe_history = [
            {"role": "user", "content": "total amount in France in US dollars"},
            {"role": "assistant", "content": "Your total amount for France comes to about $1,103.67 USD."},
        ]
        europe_turn = asyncio.run(chat("in GBP for the whole of Europe?", history=europe_history))
        europe_sent = [t.get("question", "") for t in europe_turn["traces"]]
        ok(len(europe_sent) == 1 and "europe" in europe_sent[0].lower()
           and "gbp" in europe_sent[0].lower()
           and any(term in europe_sent[0].lower() for term in ("total", "sum", "amount"))
           and "france" not in europe_sent[0].lower()
           and "where currency is gbp" not in europe_sent[0].lower(),
           f"the follow-up requests an all-Europe amount in GBP, not a source-currency filter (got {europe_sent})")

        # Exact failure conversation from the reported bug: after the assistant offers to include
        # every European country and convert the result, "yes" executes that offered calculation.
        print("[1c] yes accepts the specific Europe-to-GBP offer")
        offer_history = [
            *europe_history,
            {"role": "user", "content": "in GBP for the whole of Europe?"},
            {"role": "assistant", "content": "For all of Europe, your total comes to about £810 GBP."},
            {"role": "user", "content": "why is France and other European countries not included?"},
            {"role": "assistant", "content": (
                "Would you like me to instead pull the total for all European countries, regardless "
                "of original currency, converted into GBP?"
            )},
        ]
        accepted = asyncio.run(chat("yes", history=offer_history))
        accepted_sent = [t.get("question", "") for t in accepted["traces"]]
        ok(len(accepted_sent) == 1 and "europe" in accepted_sent[0].lower()
           and "gbp" in accepted_sent[0].lower()
           and any(term in accepted_sent[0].lower() for term in ("total", "sum", "amount"))
           and "where currency is gbp" not in accepted_sent[0].lower(),
           f"acceptance executes the offered geographic conversion (got {accepted_sent})")
        ok("not available" not in accepted["reply"].lower()
           and "don't have a reliable way" not in accepted["reply"].lower(),
           "the assistant does not invent a missing-rate failure before the engine responds")

        # The engine's own clarification offers a question ("Try asking: “…”"), and the user says
        # "yes". The model sees no earlier replies, so it had nothing to accept (2026-10-04).
        print("[1c] yes accepts the question an engine clarification offered")
        from engine.answer_presentation import clarify_reply
        offered = clarify_reply({"reason": "I need one more detail before I can answer that.",
                                 "proposed": "total amount in Germany"})
        clarified = asyncio.run(chat("yes", history=[{"role": "user", "content": "germany amount please"},
                                                     {"role": "assistant", "content": offered}]))
        clarified_sent = [t.get("question", "") for t in clarified["traces"]]
        ok(clarified_sent == ["total amount in Germany"],
           f"yes sends the offered question as written (got {clarified_sent})")

        # An acknowledgment was forced into an engine query and came back as a clarification
        # (2026-10-04). Unforced, the model answers it in words.
        print("[1c] an acknowledgment is answered without an engine query")
        thanked = asyncio.run(chat("thank you so much!", history=[
            {"role": "user", "content": "total amount in France"},
            {"role": "assistant", "content": "270"}]))
        ok(not thanked["traces"] and thanked["reply"].strip()
           and thanked["reply"] != TOOL_EXHAUSTED_REPLY,
           f"no engine call and a reply in words (calls {[t.get('question') for t in thanked['traces']]}, "
           f"reply {thanked['reply'][:80]!r})")

        # Chrome pass, 2026-09-30 (formfacade-leads, fresh conversation): the complete question "total
        # budget in Africa" between the USD request and the shorthand made the rewrite drop USD, so
        # Europe came back as 62,000 in euros. The standalone question itself stays verbatim ([1e]).
        print("[1c] an output currency survives a complete question in between")
        usd_history = [
            {"role": "user", "content": "total budget in Europe"},
            {"role": "assistant", "content": "Your total budget for Europe comes to 62,000."},
            {"role": "user", "content": "total budget in Germany"},
            {"role": "assistant", "content": "Your total budget for Germany comes to 33,000."},
            {"role": "user", "content": "This is in euros. Whats in USD"},
            {"role": "assistant", "content": "Converted to US dollars, Germany's budget comes to $37,471.50."},
            {"role": "user", "content": "total budget in Africa"},
            {"role": "assistant", "content": "There's no budget data recorded for Africa in your sheet."},
        ]
        carried = asyncio.run(chat("How about all of Europe?", history=usd_history))
        carried_sent = [t.get("question", "").lower() for t in carried["traces"]]
        ok(len(carried_sent) == 1 and "europe" in carried_sent[0] and "budget" in carried_sent[0]
           and ("usd" in carried_sent[0] or "us dollar" in carried_sent[0]),
           f"the shorthand keeps the USD the user asked for (got {carried_sent})")
        renamed = asyncio.run(chat("and in euros for all of Europe?", history=usd_history))
        renamed_sent = [t.get("question", "").lower() for t in renamed["traces"]]
        ok(len(renamed_sent) == 1 and "europe" in renamed_sent[0]
           and ("eur" in renamed_sent[0]) and "usd" not in renamed_sent[0]
           and "us dollar" not in renamed_sent[0],
           f"naming another currency replaces the one in force (got {renamed_sent})")

        print("[1c] follow-up rewrite carries grouping and limit qualifiers")
        r1d = asyncio.run(chat("what about 2024?", history=[
            {"role": "user", "content": "top 3 customers by total amount per month in 2025"},
            {"role": "assistant", "content": "I found the top three customers for each month in 2025."}]))
        sent_d = [t.get("question", "") for t in r1d["traces"]]
        ok(any(all(term in q.lower() for term in ("top 3", "customer", "total", "month", "2024"))
               and "2025" not in q for q in sent_d),
           f"the rewritten follow-up changes the year and keeps grouping/limit qualifiers (got {sent_d})")

        # Chrome pass (2026-09-24): a complete question asked after related turns reached the engine
        # with those turns' context added ("... for PHOENIX SOFTWARE LTD", "... to suppliers",
        # "... in US dollars"), and the engine read the extra words literally.
        print("[1e] a complete question stays verbatim after related turns")
        for message, history in (
            ("How many payments are listed?", [
                {"role": "user", "content": "What is the total amount paid to PHOENIX SOFTWARE LTD?"},
                {"role": "assistant", "content": "You've paid a total of 445,494.54 to Phoenix Software Ltd."},
            ]),
            ("What is the highest amount paid?", [
                {"role": "user", "content": "What is the total amount paid to suppliers?"},
                {"role": "assistant", "content": "You've paid 2,128,324.96 to suppliers."},
            ]),
            ("total budget in Germany", [
                {"role": "user", "content": "total budget in Europe"},
                {"role": "assistant", "content": "62000"},
                {"role": "user", "content": "This is in euros. Whats in USD"},
                {"role": "assistant", "content": "About 70,748.20 US dollars."},
            ]),
        ):
            sent_s = [t.get("question", "") for t in asyncio.run(chat(message, history=history))["traces"]]
            ok(sent_s == [message], f"{message!r} reaches the engine as typed (got {sent_s})")

        print("[1f] a cutoff follow-up changes only the cutoff it names")
        gaps = ("Find the top 3 categories by revenue and the top 3 customers by total spend, then list "
                "each customer-category pair where that customer has never bought from that category. "
                "Order customers by spend descending and categories by revenue descending.")
        r1g = asyncio.run(chat("only use the top 2 customers", history=[
            {"role": "user", "content": gaps},
            {"role": "assistant", "content": "Cleo has never bought Home; Ava has never bought Travel "
                                             "or Home; Ben has never bought Office or Travel."}]))
        sent_g = [t.get("question", "") for t in r1g["traces"]]
        ok(bool(sent_g) and "top 2 customer" in sent_g[0].lower() and "top 3 categor" in sent_g[0].lower(),
           f"the rewrite keeps the top 3 categories and changes customers to 2 (got {sent_g})")

        # A re-asked question restates a catalog analysis, so it is a recalculation and reaches the
        # engine. Chrome pass (2026-09-24): in a reopened conversation the morning's Belgium total
        # came back at the morning's exchange rate, with no engine call.
        print("[1g] a re-asked question recalculates instead of repeating the earlier reply")
        belgium = {"analysis_id": "a_" + "5" * 32, "slug": "belgium_total_usd",
                   "latest_question": "total amount in Belgium in US dollars",
                   "revision": 1, "stale": False}

        async def belgium_catalog(*_args, **_kwargs):
            return [belgium]

        with patch("orchestrator.orchestrator.engine_client.call_analysis_catalog", belgium_catalog):
            r1g = asyncio.run(run_chat(
                belgium["latest_question"], TABLES,
                [{"role": "user", "content": belgium["latest_question"]},
                 {"role": "assistant", "content": "367.4342"}],
                engine_base_url=base, bearer_token=None, model=config.llm_model(),
                project=config.GOOGLE_CLOUD_PROJECT, location=config.GEMINI_LOCATION,
                conversation_id="c_" + "5" * 32,
            ))
        sent_r = [t.get("question", "") for t in r1g["traces"]]
        ok(sent_r == [belgium["latest_question"]],
           f"the re-asked question reaches the engine once (got {sent_r})")

        # A short re-asked question the catalog does not hold ("Still 5 days" in the 2026-09-24
        # re-check) is caught as a repeat of the user's own words.
        print("[1h] a short re-asked question recalculates too")
        r1h = asyncio.run(chat("minimum notice_days", history=[
            {"role": "user", "content": "minimum notice_days"},
            {"role": "assistant", "content": "The shortest notice period in your data is 5 days."}]))
        sent_h = [t.get("question", "") for t in r1h["traces"]]
        ok(len(sent_h) == 1 and "notice" in sent_h[0].lower(),
           f"the short re-asked question reaches the engine once (got {sent_h})")

        # Chrome gate (2026-09-30, payment-commissions, existing conversation): "how much commission
        # came from cards?" reached the engine as typed and its clarification became the reply,
        # although an earlier turn had asked for the total commission amount. A transcript that
        # already held that clarification made it 5 of 8 turns. Whether the model writes the
        # follow-up from the earlier turn or answers the engine's clarification from it, the answer
        # is the amount. With nothing earlier to settle it, the clarification is asked.
        print("[1i] an engine clarification an earlier turn settles is answered from it")
        from orchestrator.orchestrator import _question_words

        def clarifying_engine(typed, clarify, sent):
            async def query(question, *_args, **_kwargs):
                sent.append(question)
                if _question_words(question) == _question_words(typed):
                    return {"status": "clarify", "clarify": clarify}
                return {"status": "answered",
                        "answer": {"columns": ["commission_amount"], "rows": [["9.28"]]}}
            return query

        payments = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "payment-commissions"
        payment_tables = [{"name": name, "data": (payments / f"{name}.csv").read_text(encoding="utf-8")}
                          for name in ("payments", "commission_rates")]
        follow_up = "how much commission came from cards?"
        amount = "total commission amount for card payments"
        settled_history = [
            {"role": "user", "content": amount},
            {"role": "assistant", "content": "The commission from card payments comes to 9.28."},
            {"role": "user", "content": "total amount after subtracting commission for digital wallet payments"},
            {"role": "assistant", "content": "For your digital wallet payments, the total after "
                                             "subtracting commission comes to 226.09."},
            {"role": "user", "content": follow_up},
            {"role": "assistant", "content": "I want to make sure I get this right: are you asking for "
                                             "the total commission earned from card payments, or about "
                                             "a commission percentage rate?"},
            {"role": "user", "content": amount},
            {"role": "assistant", "content": "The commission from card payments comes to 9.28."},
        ]
        sent_i = []
        with patch("orchestrator.orchestrator.engine_client.call_query", clarifying_engine(
                follow_up, {"proposed": "total commission_percent", "dropped": ["came", "cards"]}, sent_i)):
            settled = asyncio.run(chat(follow_up, history=settled_history, tables=payment_tables))
        ok(bool(sent_i) and "commission" in sent_i[-1].lower() and "card" in sent_i[-1].lower()
           and _question_words(sent_i[-1]) != _question_words(follow_up) and "9.28" in settled["reply"],
           f"the follow-up is answered with the amount an earlier turn asked for "
           f"(sent {sent_i}, reply {settled['reply']!r})")
        typed = "total amount in GBP"
        sent_j = []
        with patch("orchestrator.orchestrator.engine_client.call_query", clarifying_engine(
                typed, {"reason": "GBP can mean converting every order into GBP or keeping only the "
                                  "orders recorded in GBP"}, sent_j)):
            asked = asyncio.run(chat(typed, history=[
                {"role": "user", "content": "how many orders are there?"},
                {"role": "assistant", "content": "There are 25 orders."}]))
        ok(sent_j == [typed] and "?" in asked["reply"] and "9.28" not in asked["reply"],
           f"a clarification nothing earlier settles is asked of the user "
           f"(sent {sent_j}, reply {asked['reply']!r})")

        # Production regression (2026-09-08): this follow-up was decomposed into five progressively
        # weaker queries, ended on a grouped COUNT, and discarded all useful terminal results with
        # "step budget". The shipped workbook now includes the exact tier schedule as a fixture.
        print("[1d] joined discount follow-up is one complete terminal query")
        dataset = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "orders-tiers"
        discount_tables = [
            {"name": name, "data": (dataset / f"{name}.csv").read_text(encoding="utf-8")}
            for name in ("orders", "tier")
        ]
        discount = asyncio.run(chat(
            "reduce the discount from total amount based on customer's tier",
            history=[
                {"role": "user", "content": "total amount in France in US dollars"},
                {"role": "assistant", "content":
                 "Your total sales in France comes to about $1,126.56 US dollars."},
            ],
            tables=discount_tables,
        ))
        sent_discount = [trace.get("question", "") for trace in discount["traces"]]
        ok(len(sent_discount) == 1,
           f"the completed engine result terminates the tool loop (got {sent_discount})")
        ok(bool(sent_discount) and all(term in sent_discount[0].lower() for term in (
            "discount", "tier", "france", "us dollar",
        )), f"the one query carries calculation and prior qualifiers (got {sent_discount})")
        ok("step budget" not in discount["reply"].lower(),
           "the workflow cannot replace a terminal result with a step-budget error")

        # Rule 4 — a clarify must pass through, never be smoothed into a fabricated answer.
        print("[4] clarify passes through, not smoothed over")
        r2 = asyncio.run(chat("What is our total revenue by region?"))
        clarified = [t for t in r2["traces"] if (t["engine"] or {}).get("status") == "clarify"]
        ok(len(clarified) >= 1, "the ambiguous query produced a clarify outcome")
        ok("region" in r2["reply"].lower(), "the reply relays the clarification (mentions 'region')")

        # Real Gemini authors the proposal. The fake engine asserts orchestration,
        # not SQL/Python correctness (the unmocked Chrome gate owns that evidence).
        print('[5] complex ordering follows output priority, not mention order')
        question = ('Find the top 3 categories by revenue and top 3 customers by spend, '
                    'then customer-category pairs never purchased. '
                    'Order customers by spend descending and categories by revenue descending.')
        calls = []
        async def query(q, tables, job_id, conversation, **kwargs):
            calls.append((q, kwargs))
            if kwargs.get('decomposition') is None:
                return {'status': 'decompose', 'decomposition_required': {'reason': 'compound query'}}
            return {'status': 'answered', 'answer': {'columns': ['customer', 'category'], 'rows': [['A', 'B']]}}
        with patch('orchestrator.orchestrator.engine_client.call_query', query):
            asyncio.run(chat(question))
        proposals = [options['decomposition'] for _, options in calls if options.get('decomposition')]
        ok(len(proposals) == 1, 'Gemini makes one engine-triggered decomposition proposal')
        if proposals:
            proposal = proposals[0]
            leaves = {node['id']: node['question'].lower() for node in proposal['subquestions']}
            cross = next((node for node in proposal['merges'] if node['op'] == 'cross'), None)
            ok(cross is not None and 'customer' in leaves.get(cross['inputs'][0], '')
               and 'categor' in leaves.get(cross['inputs'][1], ''),
               'customer sort is primary even though categories were mentioned first')

        # Chrome pass, 2026-09-30: replies that the numeric grading passed still said things the
        # result did not. Each case replays the engine result of the reported turn.
        def presented(message, shaped, history=None):
            seen = {}

            async def answer(_question, _tables, _job_id, _conversation, **kwargs):
                seen["slug"] = str((kwargs.get("analysis") or {}).get("slug") or "")
                return shaped

            with patch("orchestrator.orchestrator.engine_client.call_query", answer):
                return asyncio.run(chat(message, history=history))["reply"], seen.get("slug", "")

        usd = [{"specification": "currency", "status": "satisfied", "realization": "converted",
                "target": "USD"}]

        print("[6] a figure with no currency is presented as a bare number")
        price, _ = presented("average price for VIP customers", {
            "status": "answered",
            "sql": "SELECT AVG(price) AS avg_price FROM orders WHERE customer_segment = 'VIP'",
            "answer": {"columns": ["avg_price"], "rows": [["250.77935327248009322198"]]},
        })
        ok("250.78" in price and not re.search(r"[$£€]|\b(?:dollars?|usd|pounds?|euros?)\b", price, re.I),
           f"the average of a plain price column names no currency (got {price!r})")
        duration, _ = presented("average duration", {
            "status": "answered", "sql": "SELECT AVG(duration) AS avg_duration FROM leads",
            "answer": {"columns": ["avg_duration"], "rows": [["263.96129174961291749613"]]},
        })
        ok(re.search(r"26[34]", duration) and not re.search(r"second|minute|hour", duration, re.I),
           f"the average of a plain duration column guesses no unit (got {duration!r})")

        # The one row of the category-gaps demo holds the SECOND of each top 2 (Cleo spent 340 and
        # Office earned 300). This is a prompt rule, so the check is a rate: the prompt before it
        # promoted Ava or Travel in 6 of 8 replies and this one in 0 of 24. "At most 1 of 4" fails
        # about 95% of the time at the old rate and about 1% of the time at a 5% rate.
        print("[6] a row from a top N is not promoted to the top")
        gaps_prompt = (Path(__file__).resolve().parents[1] / "web" / "public" / "dataset"
                       / "complex-category-gaps" / "prompt.txt").read_text(encoding="utf-8").strip()

        async def gap_replies():
            async def answer(*_args, **_kwargs):
                return {"status": "answered",
                        "answer": {"columns": ["customer_name", "sum", "category", "top_categories_sum"],
                                   "rows": [["Ava", 200, "Travel", 240]]}}

            with patch("orchestrator.orchestrator.engine_client.call_query", answer):
                return [turn["reply"] for turn in
                        await asyncio.gather(*(chat(gaps_prompt) for _ in range(4)))]

        gaps = asyncio.run(gap_replies())
        promoted = [reply for reply in gaps if re.search(
            r"\b(?:your|the)\s+(?:top|highest|biggest|largest|best|leading|number[- ]one|#\d)"
            r"(?:[-\s](?:spending|earning|selling|grossing|performing|revenue))?\s+"
            r"(?:spender|customer|category|earner|buyer)\b", reply, re.I)]
        ok(all("Ava" in reply and "Travel" in reply for reply in gaps) and len(promoted) <= 1,
           f"Ava and Travel are two of a top 2, not the top of it (promoted in {promoted!r} of {len(gaps)})")

        print("[6] a reply leaves out the figure of an earlier turn")
        discounted, _ = presented(
            "total amount in France in US dollars after customer tier discount",
            {"status": "answered", "calculations": usd,
             "answer": {"columns": ["net_amount_usd"], "rows": [["995.26575"]]}},
            history=[
                {"role": "user", "content": "total amount in France in US dollars"},
                {"role": "assistant", "content": "Your total for France comes to $1,101.44 in US dollars."},
            ])
        ok("995.27" in discounted and "1,101" not in discounted and "1101" not in discounted
           and re.search(r"\$|\busd\b|us dollars?", discounted, re.I),
           f"the discounted total stands alone, in its verified currency (got {discounted!r})")

        # The gate's comparator binds a converted value to the currency written against it. After the
        # sign rule, replies read "In US dollars, ... comes to 70,401." and failed it with the right
        # value; the chat now writes the verified currency beside the amount itself.
        print("[6] a converted total is written beside its currency")
        europe, _ = presented(
            "How about all of Europe?",
            {"status": "answered", "calculations": usd,
             "answer": {"columns": ["total_usd"], "rows": [[70401]]}},
            history=[
                {"role": "user", "content": "total budget in Germany"},
                {"role": "assistant", "content": "Your total budget for German entries comes to 33,000."},
                {"role": "user", "content": "This is in euros. Whats in USD"},
                {"role": "assistant", "content":
                 "In US dollars, your total budget for the German entries comes to 37,471.50."},
            ])
        ok(re.search(r"(?:\$|\bUSD)\s*70,401\b|\b70,401(?:\.00)?\s*(?:USD|US dollars)\b", europe),
           f"the converted total carries its currency (got {europe!r})")

        # Three conversations asked at once: the engine serves one question at a time, and two of the
        # first six turns came back busy. The replies were "Let me try that again." with nothing
        # retried, since a terminal engine outcome ends the turn.
        print("[6] a failed turn promises no retry")
        busy, _ = presented("total budget in Germany",
                            {"status": "error", "error": "Engine is busy; retry shortly"})
        ok(re.search(r"again", busy, re.I) and not re.search(
            r"\b(?:let me|i'll|i will|i'm going to|i am going to)\s+(?:try|retry|re-?run|run|give)",
            busy, re.I),
           f"a busy engine is reported and the user is asked to send the question again (got {busy!r})")

        print("[7] an analysis is named for its measure, not its filter")
        _, slug = presented("total amount in France in US dollars", {
            "status": "answered", "calculations": usd,
            "answer": {"columns": ["total_usd"], "rows": [["1101.435"]]},
        })
        ok(bool(slug) and not re.search(r"franc|french|usd|dollar", slug, re.I) and len(slug) <= 40,
           f"the new workbook's name survives a change of place or currency (got {slug!r})")

        # server wiring — POST /chat returns a well-formed envelope (cheap, no tool needed).
        print("[S] POST /chat server envelope")
        _test_server(base)
    finally:
        srv.shutdown()

    print(f"\ntest_orchestrator: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


def _test_server(engine_base):
    """Start the orchestrator HTTP server and post a trivial message (no tool call needed)."""
    os.environ["ENGINE_BASE_URL"] = engine_base
    os.environ["ORCH_PORT"] = "8813"
    previous_app_env = os.environ.get("APP_ENV")
    previous_test_sub = os.environ.get("AUTH_TEST_SUB")
    # The production guard intentionally ignores AUTH_TEST_SUB. Declare this harness as a test
    # environment before reloading config so the envelope test exercises chat, not real Firebase.
    os.environ["APP_ENV"] = "test"
    os.environ["AUTH_TEST_SUB"] = "localdev"
    # Relaying the message to Gemini is egress, so /chat requires the operator's deployment
    # switch. Set it explicitly so the test does not depend on the developer's environment.
    previous_egress = os.environ.get("EXTERNAL_LLM_ENABLED")
    os.environ["EXTERNAL_LLM_ENABLED"] = "1"
    # reimport config so ENGINE_BASE_URL/ORCH_PORT take effect
    import importlib
    from engine import config as _cfg
    importlib.reload(_cfg)
    from orchestrator import server as _srv
    importlib.reload(_srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 8813), _srv.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(payload, timeout=120):
        request = urllib.request.Request(
            "http://127.0.0.1:8813/chat", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        message = {"message": "Say hello in one short sentence. Do not call any tool.",
                   "tables": TABLES, "history": []}
        _, granted = post(message)
        ok("reply" in granted and isinstance(granted.get("history"), list),
           "POST /chat returns {reply, history}")

        os.environ["EXTERNAL_LLM_ENABLED"] = "0"
        status, disabled = post(message, timeout=30)
        ok(status == 503 and "reply" not in disabled,
           "the deployment-level egress switch disables chat")
    finally:
        if previous_egress is None:
            os.environ.pop("EXTERNAL_LLM_ENABLED", None)
        else:
            os.environ["EXTERNAL_LLM_ENABLED"] = previous_egress
        httpd.shutdown()
        if previous_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = previous_app_env
        if previous_test_sub is None:
            os.environ.pop("AUTH_TEST_SUB", None)
        else:
            os.environ["AUTH_TEST_SUB"] = previous_test_sub
        importlib.reload(_cfg)


if __name__ == "__main__":
    main()
