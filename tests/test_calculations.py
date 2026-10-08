"""Hermetic adversarial tests for the registered typed-calculation engine."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from engine.currency_intent import (
    CurrencyIntentKind, currency_conversion_target, currency_intent, currency_rate_target,
)
from engine.knowledge import KnowledgeReasoner
from engine.knowledge_tables import KnowledgeTableQuery
from engine.numeric import coerce_numeric, parse_decimal, wire_decimal
from engine.dataset_semantics import synthetic_currency_column
from engine.pg import _PGTYPE, _numeric_to_py
from engine.calculations import (
    ComputationEvidence,
    assess_calculations,
    calculation_clarify,
    describe_computation,
    detect_calculations,
    select_calculation_candidate,
)
from engine.sql_ast import (
    Aggregate, BinaryExpr, BooleanExpr, ColumnRef, Comparison, Join, Literal, SQLType,
    SelectItem, SelectQuery, SetQuery, render_query,
)
from engine.sql_schema import SchemaGraph
from engine.sql_rank import SemanticSignals
from engine.sql_search import SQLSearcher
from engine.tables import TableQuery, table_from_rows
from engine.trace import stream_final
from engine.artifact_provenance import (
    semantic_encoder_fingerprint, sha256_file, sha256_tree,
)
from engine.model_revisions import QWEN_MODEL_ID, QWEN_REVISION
from mcp_server.engine_client import shape_reason_response
from training.props.calculation_contrastive import build_rows, write_rows
from training.props.eval_intent import read_op_mirror, thresholds_from_score_rows
from training.props.promote import promote


P = 0
F = 0


def ok(condition, message):
    global P, F
    if condition:
        P += 1
        print(f"  PASS  {message}")
    else:
        F += 1
        print(f"  FAIL  {message}")


ORDERS = {
    "name": "orders",
    "columns": ["currency", "amount"],
    "rows": [["EUR", 310], ["GBP", 100], ["USD", 95]],
}
USD_RATES = {
    "name": "fx",
    "columns": ["currency_code", "rate_to_usd"],
    "rows": [["EUR", 1.5], ["GBP", 2.0], ["USD", 1.0]],
}
EDGE = {
    "from_table": "orders", "from_col": "currency",
    "to_table": "fx", "to_col": "currency_code",
}


def _assessment(question, tables=(ORDERS, USD_RATES), fks=(EDGE,)):
    graph = SchemaGraph.from_tables(tables, fks)
    candidate, assessments, _ = select_calculation_candidate(
        question, tables, graph, SQLSearcher(graph).search(question),
    )
    assessment = next(row for row in assessments if row["specification"] == "currency")
    return candidate, assessment


def _currency_assessment(question, tables, graph, computation):
    return next(
        row for row in assess_calculations(question, tables, graph, computation)
        if row["specification"] == "currency"
    )


def test_intent_is_not_a_bare_currency_phrase():
    count = currency_intent("how many orders in USD")
    output = currency_intent("total order amount in euros")
    european_output = currency_intent("total amount in Europe in British pounds")
    explicit = currency_intent("convert USD to EUR")
    ok(count is not None and count.kind == CurrencyIntentKind.FILTER,
       "COUNT + in USD is a row filter")
    ok(output is not None and output.kind == CurrencyIntentKind.OUTPUT,
       "aggregate + in euros is an output-unit request")
    ok(european_output is not None and european_output.kind == CurrencyIntentKind.OUTPUT
       and european_output.target == "GBP",
       "geographic Europe plus GBP on a total keeps GBP as output units, not a row filter")
    ok(explicit is not None and explicit.kind == CurrencyIntentKind.OUTPUT and explicit.explicit,
       "explicit convert-to phrase is an output conversion")
    ok(currency_conversion_target("how many orders in USD") is None,
       "filter intent cannot activate FX enrichment or conversion ranking")
    ok(currency_conversion_target("total order amount in JPY") == "JPY",
       "pinned ISO/CLDR codes are recognized beyond three hand-written aliases")
    ok(currency_intent("show orders in top category") is None,
       "ordinary lowercase words that collide with ISO codes do not become currency intent")
    ok(currency_rate_target("rate_to_abc") is None,
       "malformed rate columns cannot enter typed FX availability")

    # Contrastive intent matrix: geography does not turn an output unit into a source-currency
    # filter, while COUNT and explicit negation remain row-selection requests.
    matrix = (
        ("how many orders are in GBP", CurrencyIntentKind.FILTER, "GBP"),
        ("total amount in Europe in GBP", CurrencyIntentKind.OUTPUT, "GBP"),
        ("total amount in Europe in British pounds", CurrencyIntentKind.OUTPUT, "GBP"),
        ("convert total amount from EUR to GBP", CurrencyIntentKind.OUTPUT, "GBP"),
        ("orders not in USD", CurrencyIntentKind.FILTER, "USD"),
    )
    for phrase, expected_kind, expected_target in matrix:
        intent = currency_intent(phrase)
        ok(intent is not None and intent.kind == expected_kind and intent.target == expected_target,
           f"currency intent keeps scope and output/filter semantics: {phrase!r}")


def test_filter_conversion_and_annotation_matrix():
    count, count_result = _assessment("how many orders in USD")
    ok(count_result["status"] == "satisfied"
       and count_result["realization"] == "currency_filter",
       "typed WHERE currency=USD satisfies the count filter")
    ok("WHERE" in count.sql and "'USD'" in count.sql,
       "filter evidence agrees with rendered SQL")

    excluded, excluded_result = _assessment("how many orders not in USD")
    ok(excluded_result["status"] == "satisfied"
       and excluded_result["realization"] == "currency_exclusion",
       "negated currency filters preserve their polarity")
    ok(excluded.sql.startswith("SELECT COUNT") and "!=" in excluded.sql and "'USD'" in excluded.sql,
       "post-ranking admissibility skips the invalid EXCEPT candidate for the typed exclusion")

    _, ambiguous = _assessment("average amount in GBP")
    ok(ambiguous["status"] == "ambiguous"
       and ambiguous["proposal"] == "average amount where currency is GBP",
       "a scalable aggregate resolved as a filter asks instead of silently choosing a reading")

    _, missing = _assessment("total order amount in euros")
    ok(missing["status"] == "unmet" and missing["available_targets"] == ["USD"],
       "mixed-currency raw SUM cannot masquerade as an EUR total")
    ok(missing["proposal"] == "total order amount in US dollars",
       "proposal names a target genuinely joinable from the measure table")

    converted, satisfied = _assessment("total order amount in US dollars")
    ok(satisfied["status"] == "satisfied" and satisfied["realization"] == "converted",
       "typed SUM(amount*rate_to_usd) satisfies conversion")
    ok("rate_to_usd" in converted.sql and " * " in converted.sql,
       "conversion evidence agrees with rendered arithmetic")

    products = {"name": "products", "columns": ["product", "revenue"],
                "rows": [["A", 100], ["B", 250]]}
    _, annotation = _assessment("total revenue in euros", (products,), ())
    ok(annotation["status"] == "satisfied" and annotation["realization"] == "unit_annotation",
       "a measure with no currency dimension accepts the user's stated unit")

    eur_orders = {**ORDERS, "rows": [["EUR", 10], ["EUR", 20]]}
    _, identity = _assessment("total order amount in euros", (eur_orders,), ())
    ok(identity["status"] == "satisfied" and identity["realization"] == "identity",
       "an all-EUR measure needs no rate multiplication")

    incomplete_orders = {**ORDERS, "rows": [["EUR", 10], ["", 20], ["unknown", 30]]}
    _, incomplete = _assessment("total order amount in euros", (incomplete_orders,), ())
    ok(incomplete["status"] == "unmet"
       and incomplete["source_currency"]["state"] == "incomplete",
       "blank or non-ISO source values cannot falsely certify an identity conversion")

    jpy_rates = {"name": "jpy_fx", "columns": ["currency_code", "rate_to_jpy"],
                 "rows": [["EUR", 160.0], ["GBP", 190.0], ["USD", 150.0]]}
    jpy_edge = {**EDGE, "to_table": "jpy_fx"}
    _, jpy = _assessment("total order amount in JPY", (ORDERS, jpy_rates), (jpy_edge,))
    ok(jpy["status"] == "satisfied" and jpy["realization"] == "converted",
       "typed direct-rate conversion works for any pinned ISO currency code")


def test_an_output_unit_request_is_not_satisfied_by_a_query_that_drops_its_aggregate():
    """The live suite caught this after the SQL proposer shipped: for "total order amount in KWD"
    the arbiter chose a proposer beam that dropped the SUM and filtered currency = 'KWD'. A query
    without a value output was treated as a filter reading, so the unit request looked satisfied
    and the answer was an empty table instead of a decline. Only the parser's row-selection
    intents are filters; an output-unit request needs a monetary aggregate."""
    graph = SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,))
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    currency = ColumnRef("orders", "currency", SQLType.TEXT)

    def filtered(select, code):
        return SelectQuery(select, "orders", where=Comparison(currency, "=", Literal(code, SQLType.TEXT)))

    dropped = filtered((SelectItem(amount),), "KWD")
    output = _currency_assessment("total order amount in KWD", (ORDERS, USD_RATES), graph,
                                  describe_computation(dropped))
    ok(output["status"] != "satisfied",
       "an output-unit request is not satisfied by a filter that dropped the aggregate")

    # Contrast: a COUNT is a row selection, so the same filter still satisfies it.
    counted = filtered((SelectItem(Aggregate("COUNT", amount)),), "EUR")
    count = _currency_assessment("how many orders in EUR", (ORDERS, USD_RATES), graph,
                                 describe_computation(counted))
    ok(count["status"] == "satisfied" and count["realization"] == "currency_filter",
       "a count filtered to EUR still satisfies its row-filter intent")

    # Negative: an aggregate filtered to the unit stays the explicit ambiguity it always was.
    summed = filtered((SelectItem(Aggregate("SUM", amount)),), "EUR")
    ambiguous = _currency_assessment("total order amount in EUR", (ORDERS, USD_RATES), graph,
                                     describe_computation(summed))
    ok(ambiguous["status"] == "ambiguous" and ambiguous["realization"] == "currency_filter",
       "a summed measure filtered to the unit remains an explicit convert-or-filter question")


def test_an_output_unit_is_not_also_a_filter_on_the_same_currency():
    """Production 2026-09-29 (customer-orders): "total amount in Europe in GBP" answered £810 —
    the plan converted to GBP AND kept only the rows whose currency was GBP (the five London
    orders, each times 1), dropping every EUR order in Brussels and Paris. The phrase "in GBP"
    names the output unit; it was realized twice and the verdict said satisfied."""
    graph = SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,))
    amount, currency, rate, code = (graph.column_map[key].ref for key in (
        ("orders", "amount"), ("orders", "currency"), ("fx", "rate_to_usd"), ("fx", "currency_code")))
    join = Join("fx", currency, code)
    converted_sum = (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate))),)
    convert_only = SelectQuery(converted_sum, "orders", joins=(join,))
    convert_and_keep = SelectQuery(converted_sum, "orders", joins=(join,),
                                   where=Comparison(currency, "=", Literal("USD", SQLType.TEXT)))
    question = "total order amount in USD"

    doubled = _currency_assessment(question, (ORDERS, USD_RATES), graph,
                                   describe_computation(convert_and_keep))
    ok(doubled["status"] != "satisfied" and "only the rows already in USD" in doubled["reason"],
       f"converting while keeping only USD rows does not satisfy 'in USD' (got {doubled['status']})")
    whole = _currency_assessment(question, (ORDERS, USD_RATES), graph,
                                 describe_computation(convert_only))
    ok(whole["status"] == "satisfied" and whole["realization"] == "converted",
       "the same conversion over every row satisfies the output unit")

    # Selection takes the best-ranked SATISFIED query, so the doubled reading ranked first loses.
    class Ranked:
        def __init__(self, query):
            self.query = query
    chosen, _, index = select_calculation_candidate(
        question, (ORDERS, USD_RATES), graph, [Ranked(convert_and_keep), Ranked(convert_only)])
    ok(index == 1 and chosen.query is convert_only,
       "selection skips the filtered conversion for the one that converts every row")

    # Contrast: a SOURCE currency named outside the target phrase is a real filter.
    source = SelectQuery(converted_sum, "orders", joins=(join,),
                         where=Comparison(currency, "=", Literal("GBP", SQLType.TEXT)))
    gbp_in_usd = _currency_assessment("total amount of GBP orders in USD", (ORDERS, USD_RATES), graph,
                                      describe_computation(source))
    ok(gbp_in_usd["status"] == "satisfied" and gbp_in_usd["realization"] == "converted",
       "GBP orders converted to USD keep their GBP filter")

    # Negative: a COUNT "in USD" is a row selection and keeps its filter.
    counted = SelectQuery((SelectItem(Aggregate("COUNT", amount)),), "orders",
                          where=Comparison(currency, "=", Literal("USD", SQLType.TEXT)))
    count = _currency_assessment("how many orders in USD", (ORDERS, USD_RATES), graph,
                                 describe_computation(counted))
    ok(count["status"] == "satisfied" and count["realization"] == "currency_filter",
       "how many orders in USD is still a USD row filter")


def test_a_world_word_that_names_the_scope_is_not_the_answer():
    """Production 2026-09-29: the assistant's accepted offer "total amount for all European countries
    in GBP" projected DISTINCT countries instead of the total, and the engine clarified. A world
    column word lists, groups or counts only under a cue that puts it in the answer."""
    from engine.knowledge_tables import _world_word_is_output
    total, count = ("SUM", "orders", "amount"), ("COUNT", "orders", None)
    for question, agg, expected in (
        ("total amount for all European countries in GBP", total, False),
        ("average amount in Asian countries", ("AVG", "orders", "amount"), False),
        ("how many orders from European countries", count, False),
        ("total amount by country", total, True),
        ("total amount for each continent", total, True),
        ("which country has the highest total amount", total, True),
        ("how many countries are the customers in", count, True),
        ("number of distinct countries", count, True),
        ("which countries are the customers in", count, True),
        ("What is the total currency?", total, True),     # the attribute itself, listed per value
    ):
        word = next(w for w in ("continent", "countries", "currency", "country") if w in question)
        ok(_world_word_is_output(question, word, agg) is expected,
           f"world word role: {question!r} -> {'answer' if expected else 'scope'}")


def test_the_coverage_gate_reads_a_place_as_one_name():
    """The coverage gate declined three correct plans in the customer-orders sweep (2026-09-30), each by
    resolving one word of a place on its own: 'united' of 'the United Kingdom' surfaced another country,
    'north' of 'North American' a town called North, 'European' Germany. A span that names a qid the SQL
    filters on covers its words, and a demonym is its place plus '-n'/'-an'."""
    from unittest.mock import patch

    import numpy as np
    from engine import knowledge_query

    words = {"unitedkingdom": ("country", "Q145"), "northamerica": ("continent", "Q49"),
             "europe": ("continent", "Q46"), "america": ("country", "Q30")}
    fuzzy = {"united": ("united", "Q30", "country", 0.80), "north": ("north", "Q14692921", "city", 0.91),
             "european": ("european", "Q183", "country", 0.75), "american": ("american", "Q30", "country", 0.8),
             "leads": ("leads", "Q584982", "city", 0.85), "german": ("german", "Q183", "country", 0.75)}
    gate = object.__new__(knowledge_query.KnowledgeQuery)
    gate._kb_rows = lambda _sql, params: [(norm, *words[norm]) for norm in params[0] if norm in words]
    gate._word_qid = lambda _word: None
    gate._best_world_entity = lambda tokens: fuzzy.get(tokens[0])
    gate._encode = lambda texts: np.zeros((len(texts), 2), dtype=np.float32)
    schema = [{"table": "orders", "name": "amount", "affinity": "INTEGER", "qvec": [0.0, 0.0]},
              {"table": "orders", "name": "currency", "affinity": "TEXT", "qvec": [0.0, 0.0]}]

    def dropped(question, filtered_qid, aggregate='SUM("amount" * "rate_to_gbp")'):
        sql = f'SELECT {aggregate} FROM t WHERE "country__continent" = \'{filtered_qid}\''
        with patch.object(knowledge_query, "closed_class_words",
                          lambda _text: frozenset({"the", "for", "all", "in", "from", "how", "many"})):
            return gate._uncovered(question, schema, sql)

    ok(dropped("total amount in the United Kingdom in GBP", "Q145") == [],
       "'the United Kingdom' is one place the query filters on")
    ok(dropped("total amount for all European countries in GBP", "Q46") == [],
       "'European' names Europe (Q46)")
    ok(dropped("total amount for all North American countries in GBP", "Q49") == [],
       "'North American' names North America (Q49), not a town called North")
    # Negative: a place the query did NOT filter on is still dropped.
    ok(dropped("total amount for all European countries in GBP", "Q30") == ["european"],
       "a Europe question over a United States filter still declines")
    # The noun a count cue governs is what COUNT counts ('leads' sat 0.85 from the city of Leeds), but only
    # the head: 'German' in 'how many German leads' is still a place the query must filter on.
    ok(dropped("how many leads from Europe", "Q46", "COUNT(*)") == [],
       "the counted noun is covered by COUNT, however close it sits to a town")
    ok(dropped("count leads from Europe", "Q46", "COUNT(*)") == [],
       "a bare 'count' governs its noun like 'how many' (the one COUNT_CUE the world-word role reads)")
    ok(dropped("how many German leads", "Q46", "COUNT(*)") == ["german"],
       "a place before the counted noun is still checked")


def test_the_coverage_gate_reads_a_measure_named_in_other_words():
    """A customer's Keyword Stats sheet (2026-10-02): "What is the total search volume for forklift
    inspection checklist" built SUM("Avg. monthly searches") WHERE Keyword = 'forklift inspection checklist'
    and was declined as having dropped 'search' and 'volume', which sat near places. 'search' is the column's
    'searches' (one plural rule, sql_schema.canon), and a quantity noun after a word of the tables' names
    ('search volume', 'sales volume') is that measure's amount. After any other word it is still checked."""
    from unittest.mock import patch

    import numpy as np
    from engine import knowledge_query

    near = {"search": ("search", "Q1001", "city", 0.66), "volume": ("volume", "Q1002", "city", 0.62),
            "shipping": ("shipping", "Q1003", "city", 0.7)}
    gate = object.__new__(knowledge_query.KnowledgeQuery)
    gate._kb_rows = lambda _sql, params: []
    gate._word_qid = lambda _word: None
    gate._best_world_entity = lambda tokens: near.get(tokens[0])
    gate._encode = lambda texts: np.zeros((len(texts), 2), dtype=np.float32)

    def dropped(question, measure, table="Forklift"):
        schema = [{"table": table, "name": "Keyword", "affinity": "TEXT", "qvec": [0.0, 0.0]},
                  {"table": table, "name": measure, "affinity": "INTEGER", "qvec": [0.0, 0.0]}]
        sql = (f'SELECT SUM("{table}"."{measure}") FROM "{table}" '
               f'WHERE "{table}"."Keyword" = \'forklift inspection checklist\'')
        with patch.object(knowledge_query, "closed_class_words",
                          lambda _text: frozenset({"what", "is", "the", "for"})):
            return gate._uncovered(question, schema, sql)

    ok(dropped("What is the total search volume for forklift inspection checklist", "Avg. monthly searches") == [],
       "'search volume' names the searches column's amount")
    ok(dropped("What is the total sales volume for forklift inspection checklist", "sales") == [],
       "'sales volume' names the sales column's amount")
    # Negative: a quantity noun after a word no table names is still checked, and so is that word.
    ok(dropped("What is the total shipping volume for forklift inspection checklist", "Avg. monthly searches")
       == ["shipping", "volume"], "'shipping volume' names nothing in the sheet")


def test_a_continent_demonym_resolves_to_its_continent():
    """'how many orders from North American countries' filtered the United States (Q30): 'North American'
    has no exact entry, and the embedding fallback put it nearer the country than the continent. A
    continent's demonym is its name plus '-n'/'-an', read exactly, and only for continents."""
    from unittest.mock import patch

    from engine import entities

    words = {"northamerica": ("continent", "Q49"), "europe": ("continent", "Q46"), "pl": ("country", "Q36")}
    resolver = object.__new__(entities.EntityQuery)
    resolver._kb_rows = lambda _sql, params: [(norm, *words[norm]) for norm in params[0] if norm in words]
    resolver._nn = lambda _vector, _type: (None, -1.0)
    embedder = type("Embedder", (), {"encode": staticmethod(lambda texts: [[0.0] for _ in texts])})

    def resolve(candidates, type_):
        resolver._candidates = lambda _question: list(candidates)
        with patch.object(entities.Embedder, "get", lambda: embedder):
            return resolver._resolve("question", type_)

    ok(resolve(["North American countries", "North American", "countries"], "continent") == ("Q49", 1.0, "North American"),
       "'North American' is North America")
    ok(resolve(["European countries", "European", "countries"], "continent") == ("Q46", 1.0, "European"),
       "'European' is Europe")
    ok(resolve(["North American"], "country") is None, "the stem is read only as a continent")
    ok(resolve(["plan", "cost"], "country") is None and resolve(["plan"], "continent") is None,
       "'plan' never reads as Poland's 'pl'")


def test_an_exact_world_name_beats_a_nearer_fuzzy_guess():
    """With 'North American' read exactly, the meaning walk still filtered the United States: it takes the
    nearest hop first, and there the embedding guess 'American' -> United States (0.85, via city.country)
    was accepted before the exact continent one hop further. Exact names win across hops too."""
    words = {"city": {"key": "qid", "links": [{"col": "country", "to_table": "country", "to_col": "qid"}]},
             "country": {"key": "qid", "links": []}}
    seen = []

    def find_value(_low_q, w, exact_only=False):
        table = next(name for name, value in words.items() if value is w)
        seen.append((table, exact_only))
        if table == "country":
            return "continent", "Q49"                       # exact: 'North American' -> North America
        return None if exact_only else ("country", "Q30")  # fuzzy: 'American' -> United States

    walker = object.__new__(KnowledgeTableQuery)
    walker.words = words
    walker._find_value = find_value
    hit = walker.meaning_filter("how many orders from North American countries", {("orders", "city"): "city"})
    ok(hit is not None and (hit["attr"], hit["value"]) == ("continent", "Q49")
       and [join["right_table"] for join in hit["joins"]] == ["city", "country"],
       f"the exact continent two hops away wins over the nearer fuzzy country (got {hit})")
    ok(seen[:2] == [("city", True), ("country", True)], f"exact pass walks the whole graph first ({seen})")
    # Contrast: with no exact name anywhere, the nearest fuzzy hit still resolves.
    words["country"]["links"] = []
    walker._find_value = lambda _q, w, exact_only=False: None if exact_only or w is words["country"] else ("country", "Q30")
    hit = walker.meaning_filter("orders from American customers", {("orders", "city"): "city"})
    ok(hit is not None and hit["value"] == "Q30", "a fuzzy reading still resolves when nothing is exact")


def test_a_question_mark_is_never_a_world_value():
    """Chrome exploration, neartail-orders (2026-10-01): "what share of the total amount comes from Paris?"
    filtered continent = '?' AND city = 'Paris', matched no row, and the reply said there were no orders for
    Paris. word_country.json lists '?' among the continents, and the matcher read the question's own question
    mark as that value. A value with no letter or digit names nothing a question can say."""
    country = {"key": "qid", "filter_attrs": ["continent"],
               "filter_values": {"continent": ["?", "Africa", "Europe", "North America"]}}
    walker = object.__new__(KnowledgeTableQuery)
    ok(walker._find_value(" what share of the total   comes from  ? ", country) is None,
       "the question mark is punctuation, not the continent '?'")
    ok(walker._find_value(" total amount in europe? ", country) == ("continent", "Europe"),
       "a continent the question names is still found before its question mark")
    ok(walker._find_value(" orders from north america ", country) == ("continent", "North America"),
       "a multi-word value is still read whole")


def test_a_measure_named_in_two_words_is_summed_not_counted():
    """The sweep of the shipped sheets found "total weight kg for deliveries in Germany" answered COUNT(*)
    = 1: the measure matcher knew one-word column names only, so the table noun "deliveries" read as
    "count the deliveries". A column named by all of its words, or by a first word no other numeric column
    starts with, is the measure; the one-word rule and the table-noun count are unchanged."""
    import numpy as np
    from engine.encoder_overlay import EncoderQuery

    reader = object.__new__(EncoderQuery)
    reader.ingest = lambda tables, *args, **kwargs: (tables, [])
    reader._encode = lambda texts: np.ones((len(texts), 2), dtype=np.float32)
    schema = [{"table": "deliveries", "name": name, "affinity": affinity}
              for name, affinity in (("delivery ID", "INTEGER"), ("customer", "TEXT"), ("city", "TEXT"),
                                     ("weight kg", "INTEGER"), ("fee", "INTEGER"))]

    def read(question, op="SUM"):
        reader.read_op_model = lambda *_args: (op, {})
        return reader.read_op_all(question, schema)

    for question in ("total weight kg for deliveries in Germany", "total weight for deliveries in Germany",
                     "total weight in kg for deliveries in Germany"):
        ok(read(question) == ("SUM", "deliveries", "weight kg"), f"{question!r} sums weight kg (got {read(question)})")
    ok(read("total fee for deliveries in Germany") == ("SUM", "deliveries", "fee"), "a one-word measure is unchanged")
    ok(read("total deliveries in Germany") == ("COUNT", "deliveries", None),
       "a table noun with no measure still counts the rows")
    ok(read("how many deliveries in Germany", "COUNT") == ("COUNT", "deliveries", None), "a COUNT is unchanged")


def test_an_average_converts_every_row_before_averaging():
    """Chrome exploration, 2026-10-02: "average amount in US dollars" was declined: the currency check
    knew only a converted SUM, so AVG(amount * rate_to_usd) read as "does not convert". A rate is a row
    factor; it is realized whether the converted rows are then totalled or averaged."""
    graph = SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,))
    amount, currency, rate, code = (graph.column_map[key].ref for key in (
        ("orders", "amount"), ("orders", "currency"), ("fx", "rate_to_usd"), ("fx", "currency_code")))
    join = Join("fx", currency, code)
    question = "average order amount in US dollars"
    averaged = SelectQuery((SelectItem(Aggregate("AVG", BinaryExpr(amount, "*", rate))),), "orders",
                           joins=(join,))
    converted = _currency_assessment(question, (ORDERS, USD_RATES), graph, describe_computation(averaged))
    ok(converted["status"] == "satisfied" and converted["realization"] == "converted",
       f"AVG(amount * rate_to_usd) converts every row before averaging (got {converted['status']}: "
       f"{converted.get('reason')})")
    # Contrast: the raw average of mixed currencies converts nothing.
    raw = SelectQuery((SelectItem(Aggregate("AVG", amount)),), "orders")
    unconverted = _currency_assessment(question, (ORDERS, USD_RATES), graph, describe_computation(raw))
    ok(unconverted["status"] != "satisfied", "a raw average of mixed currencies is not in US dollars")
    # Negative: a converted maximum is not a registered realization, so it is not certified either.
    largest = SelectQuery((SelectItem(Aggregate("MAX", BinaryExpr(amount, "*", rate))),), "orders",
                          joins=(join,))
    maximum = _currency_assessment("largest order amount in US dollars", (ORDERS, USD_RATES), graph,
                                   describe_computation(largest))
    ok(maximum["status"] != "satisfied", "only a SUM or an AVG of converted rows is certified")
    # The own-data selection keeps the question's aggregate around the registered row factor: it served
    # SUM(amount * rate_to_usd) AS total_usd for the average.
    chosen, selected = _assessment(question)
    ok(chosen.sql.startswith('SELECT AVG(("orders"."amount" * "fx"."rate_to_usd")) AS "average_usd"')
       and selected["status"] == "satisfied" and selected["realization"] == "converted",
       f"the average of converted rows is selected for the average (got {chosen.sql})")
    total, _ = _assessment("total order amount in US dollars")
    ok(total.sql.startswith('SELECT SUM(("orders"."amount" * "fx"."rate_to_usd")) AS "total_usd"'),
       f"a total keeps the registered SUM (got {total.sql})")


def test_set_query_requires_every_numeric_branch_to_convert():
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    rate = ColumnRef("fx", "rate_to_usd", SQLType.REAL)
    join = Join("fx", ColumnRef("orders", "currency", SQLType.TEXT),
                ColumnRef("fx", "currency_code", SQLType.TEXT))
    converted = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate))),),
        "orders", joins=(join,),
    )
    raw = SelectQuery((SelectItem(Aggregate("SUM", amount)),), "orders")
    computation = describe_computation(SetQuery(converted, "UNION", raw))
    result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, USD_RATES),
        SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,)), computation,
    )
    ok(len(computation.branches) == 2
       and sum("rate_to_usd" in repr(output.expression)
               for branch in computation.branches for output in branch.outputs) == 1,
       "set-operation evidence counts both operands")
    ok(result["status"] == "unmet",
       "one converted branch cannot bless an unconverted set operand")

    eur_rate = ColumnRef("fx", "rate_to_eur", SQLType.REAL)
    wrong_target = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", eur_rate))),),
        "orders", joins=(join,),
    )
    dual_rates = {**USD_RATES, "columns": ["currency_code", "rate_to_usd", "rate_to_eur"],
                  "rows": [["EUR", 1.5, 1.0], ["GBP", 2.0, 1.2], ["USD", 1.0, 0.8]]}
    mixed_targets = describe_computation(SetQuery(converted, "UNION", wrong_target))
    mixed_result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, dual_rates),
        SchemaGraph.from_tables((ORDERS, dual_rates), (EDGE,)), mixed_targets,
    )
    rate_columns = {
        column.name
        for branch in mixed_targets.branches for output in branch.outputs for column in output.columns
        if column.name.startswith("rate_to_")
    }
    ok(rate_columns == {"rate_to_eur", "rate_to_usd"},
       "set-operation evidence retains every converted target")
    ok(mixed_result["status"] == "unmet",
       "fully converted branches cannot certify a request when one uses the wrong target")

    projection = SelectQuery((SelectItem(amount),), "orders")
    mixed_shape = describe_computation(SetQuery(converted, "UNION", projection))
    mixed_shape_result = _currency_assessment(
        "total order amount in US dollars", (ORDERS, USD_RATES),
        SchemaGraph.from_tables((ORDERS, USD_RATES), (EDGE,)), mixed_shape,
    )
    numeric_aggregate_branches = sum(any(output.aggregate_functions for output in branch.outputs)
                                     for branch in mixed_shape.branches)
    ok(numeric_aggregate_branches == 1 and len(mixed_shape.branches) == 2
       and mixed_shape_result["status"] == "unmet",
       "a converted aggregate cannot bless a projection-only set operand")


def test_filter_evidence_is_guaranteed_on_every_path():
    currency = ColumnRef("orders", "currency", SQLType.TEXT)
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    usd = Comparison(currency, "=", Literal("USD", SQLType.TEXT))
    positive = Comparison(amount, ">", Literal(0, SQLType.INTEGER))
    filtered = SelectQuery((SelectItem(amount),), "orders", where=usd)
    unfiltered = SelectQuery((SelectItem(amount),), "orders")
    graph = SchemaGraph.from_tables((ORDERS,), ())

    set_result = _currency_assessment(
        "show orders in USD", (ORDERS,), graph,
        describe_computation(SetQuery(filtered, "UNION", unfiltered)),
    )
    ok(set_result["status"] == "unmet",
       "a currency filter on only one set operand does not certify the whole query")

    disjoined = SelectQuery(
        (SelectItem(amount),), "orders", where=BooleanExpr("OR", (usd, positive)),
    )
    or_result = _currency_assessment(
        "show orders in USD", (ORDERS,), graph,
        describe_computation(disjoined),
    )
    ok(or_result["status"] == "unmet",
       "a currency comparison on only one OR arm is not treated as a guaranteed filter")


def test_unjoinable_rate_is_not_advertised():
    archive = {"name": "legacy_archive", "columns": ["code", "rate_to_eur"],
               "rows": [["EUR", 1.0], ["USD", 0.9]]}
    _, result = _assessment("total order amount in British pounds",
                            (ORDERS, USD_RATES, archive), (EDGE,))
    ok(result["available_targets"] == ["USD"],
       "availability uses the same typed edge as planner binding")
    ok("euros" not in result["proposal"].lower(),
       "an unjoinable rate column cannot produce a dead-end proposal")

    malformed = {"name": "bad_fx", "columns": ["currency_code", "rate_to_abc"],
                 "rows": [["EUR", 1.0], ["USD", 1.1]]}
    malformed_edge = {**EDGE, "to_table": "bad_fx"}
    _, malformed_result = _assessment(
        "total order amount in British pounds",
        (ORDERS, USD_RATES, malformed), (EDGE, malformed_edge),
    )
    ok(malformed_result["available_targets"] == ["USD"],
       "an invalid rate_to suffix is ignored rather than crashing availability")


def test_asserted_currency_join_is_scoped_to_its_measure():
    budget_ccy = synthetic_currency_column("budget")
    cost_ccy = synthetic_currency_column("cost")
    sales = {
        "name": "sales",
        "columns": ["budget", "cost", budget_ccy, cost_ccy],
        "rows": [[10, 20, "EUR", "GBP"]],
    }
    edges = (
        {"from_table": "sales", "from_col": budget_ccy,
         "to_table": "fx", "to_col": "currency_code"},
        {"from_table": "sales", "from_col": cost_ccy,
         "to_table": "fx", "to_col": "currency_code"},
    )
    graph = SchemaGraph.from_tables((sales, USD_RATES), edges)
    candidate, assessments, _ = select_calculation_candidate(
        "total cost in US dollars", (sales, USD_RATES), graph,
        SQLSearcher(graph).search("total cost in US dollars"),
    )
    assessment = next(row for row in assessments if row["specification"] == "currency")
    ok(candidate is not None and cost_ccy in candidate.sql and budget_ccy not in candidate.sql,
       "the selected cost conversion joins through cost's asserted currency only")
    ok(assessment["status"] == "satisfied" and assessment["realization"] == "converted",
       "measure-scoped asserted currency still produces a verified conversion")


def test_only_monetary_measures_can_convert():
    inventory = {"name": "inventory", "columns": ["currency", "quantity"],
                 "rows": [["EUR", 10], ["USD", 20]]}
    edge = {**EDGE, "from_table": "inventory"}
    graph = SchemaGraph.from_tables((inventory, USD_RATES), (edge,))
    candidates = SQLSearcher(graph).search("total quantity in USD")
    ok(candidates and all("rate_to_usd" not in candidate.sql for candidate in candidates),
       "FX generation cannot multiply a physical quantity by an exchange rate")
    assessment = _currency_assessment(
        "total quantity in USD", (inventory, USD_RATES), graph,
        describe_computation(candidates[0].query),
    )
    ok(assessment["status"] in {"ambiguous", "unmet"},
       "a non-monetary aggregate cannot be certified as converted or currency-denominated")


def test_missing_typed_evidence_fails_closed():
    eur_orders = {**ORDERS, "rows": [["EUR", 10], ["EUR", 20]]}
    reasoner = object.__new__(KnowledgeReasoner)
    response = reasoner._verify_calculations(
        {"question": "total order amount in euros", "sql": "SELECT 30",
         "result": {"columns": ["total"], "rows": [[30]]}},
        (eur_orders,), "total order amount in euros", (),
    )
    ok(response["clarify"] is True and response["result"] is None
       and response["currency"]["computation"]["verified"] is False,
       "a route without typed computation evidence cannot release a numeric answer")


def test_decline_contract_reaches_stream_and_mcp():
    _, assessment = _assessment("total order amount in euros")
    response = calculation_clarify(
        "total order amount in euros",
        {"sql": 'SELECT SUM("orders"."amount") FROM "orders"'},
        (assessment,),
    )
    ok(response["clarify"] and response["result"] is None and response["reason"],
       "wrong number is removed from the clarify envelope")

    emitted, events = {}, []
    response["execution"] = {"actual": "python", "verified": False}
    def emit(key, value):
        events.append(key)
        emitted[key] = value
    stream_final(emit, response)
    ok(emitted["clarify"]["reason"] == response["reason"]
       and emitted["clarify"]["unmet"],
       "RTDB terminal event preserves reason and unmet evidence")
    ok(emitted["execution"] == response["execution"],
       "RTDB terminal events preserve the backend that produced the sheets")
    ok(events.index("execution") < events.index("status"),
       "execution reaches RTDB before the terminal status can close the subscription")

    shaped = shape_reason_response(response, "job-currency")
    ok(shaped["status"] == "clarify"
       and shaped["clarify"]["reason"] == response["reason"],
       "MCP output preserves the same reason")


def _run_calculation(question, tables, fks):
    graph = SchemaGraph.from_tables(tables, fks)
    candidates = SQLSearcher(graph).search(question)
    candidate, assessments, index = select_calculation_candidate(
        question, tables, graph, candidates,
    )
    return graph, candidate, assessments, index


def test_ratio_uses_composite_keys_and_derives_units():
    economy = {"name": "economy", "columns": ["country", "year", "gdp"],
               "rows": [["FR", 2024, 3000], ["DE", 2024, 4000]]}
    population = {"name": "population", "columns": ["country", "year", "population"],
                  "rows": [["FR", 2024, 60], ["DE", 2024, 80]]}
    edge = {"from_table": "economy", "from_cols": ["country", "year"],
            "to_table": "population", "to_cols": ["country", "year"]}
    _, candidate, assessments, index = _run_calculation(
        "GDP per capita", (economy, population), (edge,),
    )
    ratio = next(row for row in assessments if row["specification"] == "ratio")
    ok(index == 0 and ratio["status"] == "satisfied" and ratio["output_unit"] == "currency/person",
       "ratio verification derives a named output unit from bound operands")
    ok("SUM(\"economy\".\"gdp\")" in candidate.sql
       and "SUM(\"population\".\"population\")" in candidate.sql
       and " AND " in candidate.sql,
       "per-capita search emits ratio-of-sums over the complete composite join")
    paraphrase = detect_calculations("national output per inhabitant")
    ok(paraphrase and paraphrase[0].specification == "ratio"
       and paraphrase[0].target == "per_capita",
       "per-capita intent recognizes heldout person-denominator paraphrases")
    ok(not detect_calculations("number of concerts for each person"),
       "ordinary for-each grouping is not a division request")


def test_learned_operand_signal_orders_only_typed_eligible_plans():
    economy = {
        "name": "economy",
        "columns": ["gdp", "revenue", "population", "country_id"],
        "rows": [[3000, 2000, 60, 1]],
    }
    graph = SchemaGraph.from_tables((economy,), ())
    signals = SemanticSignals(
        {},
        {},
        calculation_operands={
            "ratio:numerator": {
                ("economy", "gdp"): 0.95,
                ("economy", "revenue"): 0.10,
                ("economy", "country_id"): 1.0,
            },
            "ratio:denominator": {("economy", "population"): 0.95},
        },
    )
    candidates = SQLSearcher(graph).search("economic output per capita", semantic_signals=signals)
    candidate, assessments, _ = select_calculation_candidate(
        "economic output per capita", (economy,), graph, candidates,
    )
    ok(assessments[0]["status"] == "satisfied" and '"economy"."gdp"' in candidate.sql,
       "learned operand similarity orders typed ratio bindings")
    ok('"economy"."country_id"' not in candidate.sql,
       "learned similarity cannot make an identifier an eligible operand")
    adversarial = SemanticSignals(
        {},
        {},
        calculation_operands={
            "ratio:numerator": {
                ("economy", "gdp"): -1.0,
                ("economy", "revenue"): 1.0,
            },
            "ratio:denominator": {("economy", "population"): 1.0},
        },
    )
    explicit = SQLSearcher(graph).search("GDP per capita", semantic_signals=adversarial)[0]
    ok('"economy"."gdp"' in explicit.sql and '"economy"."revenue"' not in explicit.sql,
       "learned similarity cannot override an exact lexical operand binding")


def test_rate_application_supports_percent_and_fraction_units():
    payments = {"name": "payments", "columns": ["instrument", "amount"],
                "rows": [["card", 100], ["bank", 200]]}
    commissions = {"name": "commission_rates", "columns": ["instrument", "commission_fraction"],
                   "rows": [["card", 0.03], ["bank", 0.01]]}
    edge = {"from_table": "payments", "from_col": "instrument",
            "to_table": "commission_rates", "to_col": "instrument"}
    _, commission_query, commission_rows, _ = _run_calculation(
        "total commission amount", (payments, commissions), (edge,),
    )
    commission = commission_rows[0]
    ok(commission["status"] == "satisfied" and commission["rule"] == "commission_fraction"
       and "/ 100" not in commission_query.sql,
       "fractional commission rates multiply directly")

    transactions = {"name": "transactions", "columns": ["country", "amount"],
                    "rows": [["FR", 100], ["DE", 200]]}
    taxes = {"name": "tax_rates", "columns": ["country", "tax_percent"],
             "rows": [["FR", 20], ["DE", 19]]}
    tax_edge = {"from_table": "transactions", "from_col": "country",
                "to_table": "tax_rates", "to_col": "country"}
    _, tax_query, tax_rows, _ = _run_calculation(
        "total tax amount", (transactions, taxes), (tax_edge,),
    )
    tax = tax_rows[0]
    ok(tax["status"] == "satisfied" and tax["rule"] == "tax_percent"
       and "/ 100.0" in tax_query.sql,
       "percent tax rates are normalized before monetary multiplication")

    loans = {"name": "loans", "columns": ["loan_id", "principal_amount", "interest_percent"],
             "rows": [[1, 1000, 5], [2, 2000, 4]]}
    _, interest_query, interest_rows, _ = _run_calculation(
        "total annual simple interest amount", (loans,), (),
    )
    interest = interest_rows[0]
    ok(interest["status"] == "satisfied" and interest["rule"] == "interest_percent"
       and "principal_amount" in interest_query.sql,
       "an explicit annual one-year simple-interest policy uses the same typed rate primitive")

    fees = {
        "name": "merchant_fees",
        "columns": ["payment_amount", "merchant_fee_percent"],
        "rows": [[100, 2.5], [200, 1.5]],
    }
    _, fee_query, fee_rows, _ = _run_calculation(
        "compute merchant fee amount", (fees,), (),
    )
    ok(fee_rows[0]["status"] == "satisfied" and "merchant_fee_percent" in fee_query.sql,
       "commission intent and bindings recognize explicit merchant-fee terminology")

    financing = {
        "name": "financing",
        "columns": ["principal_amount", "financing_percent"],
        "rows": [[1000, 5]],
    }
    _, financing_query, financing_rows, _ = _run_calculation(
        "compute yearly simple financing charge", (financing,), (),
    )
    ok(financing_rows[0]["status"] == "satisfied" and "financing_percent" in financing_query.sql,
       "explicit yearly simple-financing terminology maps to the interest rule")


def test_a_percentage_cell_applies_the_percent_it_shows():
    # The upload importer writes a percentage-formatted cell as the percent the sheet shows, "20%", where it
    # wrote the stored fraction 0.2, and "total price including tax" came to 150.24 for 173.75 (release
    # review, revision 2, 2026-10-07; web/tests/workbook_import.test.js checks the importer's side).
    from engine.tables import csv_table

    sales = csv_table("item,price,tax_percent\nA,100,20%\nB,50,7.5%\n", "sales")
    ok([row[2] for row in sales["rows"]] == [20, Decimal("7.5")], "a percentage cell reads as its percent")
    graph, candidate, assessments, _ = _run_calculation("total price including tax", (sales,), ())
    ok(assessments[0]["status"] == "satisfied" and assessments[0]["rule"] == "tax_add_percent",
       "a percent-named rate column is applied as a percent")
    schema = [{
        "table": column.ref.table,
        "name": column.ref.name,
        "affinity": ("INTEGER" if column.ref.type == SQLType.INTEGER else
                     "REAL" if column.ref.type == SQLType.REAL else "TEXT"),
    } for column in graph.columns]
    _columns, rows = TableQuery().execute({"sales": sales}, schema, candidate.sql, query=candidate.query)
    ok(rows == [(173.75,)], f"20% and 7.5% are applied as shown, not as 0.2% and 0.075%: {rows}")


def test_joined_discount_and_currency_compose_as_one_typed_calculation():
    orders = {
        "name": "orders",
        "columns": ["order_id", "tier", "currency", "amount"],
        "rows": [[1, "Bronze", "EUR", 100], [2, "Silver", "GBP", 200],
                 [3, "Gold", "USD", 300]],
    }
    tiers = {
        "name": "tier",
        "columns": ["tier", "discount_percent"],
        "rows": [["Bronze", 5], ["Silver", 10], ["Gold", 15]],
    }
    rates = {
        "name": "exchange_rate",
        "columns": ["currency_code", "rate_to_usd"],
        "rows": [["EUR", 1.1], ["GBP", 1.25], ["USD", 1]],
    }
    fks = (
        {"from_table": "orders", "from_col": "tier",
         "to_table": "tier", "to_col": "tier"},
        {"from_table": "orders", "from_col": "currency",
         "to_table": "exchange_rate", "to_col": "currency_code"},
    )
    question = "total amount in US dollars after customer tier discount"
    graph, candidate, assessments, index = _run_calculation(
        question, (orders, tiers, rates), fks,
    )
    ok(index == 0 and len(assessments) == 2
       and all(row["status"] == "satisfied" for row in assessments),
       "currency conversion and a joined tier discount are certified together")
    ok("rate_to_usd" in candidate.sql and "discount_percent" in candidate.sql
       and "1 -" in candidate.sql and "GROUP BY" not in candidate.sql,
       "the planner emits one scalar SUM with both row factors and no incidental tier grouping")

    schema = [{
        "table": column.ref.table,
        "name": column.ref.name,
        "affinity": ("INTEGER" if column.ref.type == SQLType.INTEGER else
                     "REAL" if column.ref.type == SQLType.REAL else "TEXT"),
    } for column in graph.columns]
    columns, rows = TableQuery().execute(
        {table["name"]: table for table in (orders, tiers, rates)},
        schema,
        candidate.sql,
        query=candidate.query,
    )
    ok(columns == ["net_amount_usd"] and rows == [(584.5,)],
       "the composed calculation executes with exact typed arithmetic")

    discount_only = "reduce the discount from total amount based on customer's tier"
    _, net_candidate, net_assessments, _ = _run_calculation(
        discount_only, (orders, tiers), fks[:1],
    )
    ok(net_assessments[0]["realization"] == "rate_subtraction"
       and "GROUP BY" not in net_candidate.sql,
       "a tier names the rate lookup dimension; it does not force grouped output")

    planner_schema = [
        {
            "table": column.ref.table,
            "name": column.ref.name,
            "affinity": ("INTEGER" if column.ref.type == SQLType.INTEGER else
                         "REAL" if column.ref.type == SQLType.REAL else "TEXT"),
            "values": list(column.values),
        }
        for column in graph.columns
        if column.ref.table != "exchange_rate"
    ]
    world_plan, _, world_fk = KnowledgeTableQuery._row_calculation_context(
        question,
        ("SUM", "orders", "amount"),
        planner_schema,
        fks[:1],
        {
            "fact": "orders", "ccy_col": "currency", "date_col": None,
            "rate_col": "rate_to_usd", "target": "USD",
        },
    )
    ok(world_plan is not None and world_fk is not None
       and world_plan.expression == candidate.query.select[0].expression,
       "the world bridge consumes the same composed calculation plan as own-data AST search")

    from engine.knowledge_query import _calculation_coverage_words
    claimed = _calculation_coverage_words(assessments)
    ok({"after", "reduce", "applying", "based"} <= claimed,
       "a verified subtraction claims its structural direction and binding words in coverage checking")
    rewritten = detect_calculations(
        "total amount in France in US dollars after applying the discount based on customer's tier"
    )
    rewritten_words = _calculation_coverage_words(({
           "status": "satisfied", "operation": "subtract_rate",
       },))
    ok({intent.operation for intent in rewritten} == {"convert", "subtract_rate"}
       and {"applying", "based"} <= rewritten_words,
       "the orchestrator's complete follow-up rewrite remains a covered subtraction")
    ok("after" not in _calculation_coverage_words(({
        "status": "satisfied", "operation": "apply_rate",
    },)), "ordinary rate application cannot swallow a temporal after-filter")


def test_temporal_rate_requires_and_accepts_composite_alignment():
    sales = {"name": "sales", "columns": ["country", "effective_date", "amount"],
             "rows": [["FR", "2024-01-01", 100], ["FR", "2025-01-01", 200]]}
    rates = {"name": "tax_rates", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20], ["FR", "2025-01-01", 21]]}
    edge = {"from_table": "sales", "from_cols": ["country", "effective_date"],
            "to_table": "tax_rates", "to_cols": ["country", "effective_date"]}
    _, candidate, assessments, _ = _run_calculation(
        "total tax amount", (sales, rates), (edge,),
    )
    ok(assessments[0]["status"] == "satisfied"
       and '"sales"."effective_date" = "tax_rates"."effective_date"' in candidate.sql,
       "dated rates are admissible only when the complete temporal key is present in the join")


def test_a_calculation_at_the_wrong_grain_is_not_satisfied():
    # Regression: the verifier checked the ratio EXPRESSION but never the requested GRAIN, so
    # "gdp per capita by country" was certified satisfied while the query computed one global
    # SUM(gdp)/SUM(population) = 43.54 instead of France 43.28 / Germany 49.40 / Italy 35.59.
    # A figure at the wrong grain is a different quantity, not a coarser version of the right one.
    countries = {"name": "countries", "columns": ["country", "gdp", "population"],
                 "rows": [["France", 2900.0, 67.0], ["Germany", 4100.0, 83.0],
                          ["Italy", 2100.0, 59.0]]}
    graph = SchemaGraph.from_tables((countries,), ())

    def column(name):
        return graph.column_map[("countries", name)].ref

    ratio = BinaryExpr(Aggregate("SUM", column("gdp")), "/", Aggregate("SUM", column("population")))
    ungrouped = SelectQuery((SelectItem(ratio, alias="per_capita"),), "countries")
    grouped = SelectQuery(
        (SelectItem(column("country")), SelectItem(ratio, alias="per_capita")),
        "countries",
        group_by=(column("country"),),
    )

    question = "gdp per capita by country"
    rows = assess_calculations(question, (countries,), graph, describe_computation(ungrouped))
    ok(bool(rows) and rows[0]["status"] != "satisfied",
       "a ratio computed over every row does not answer a per-country question")
    ok(bool(rows) and "countries.country" in (rows[0].get("reason") or ""),
       "the decline names the grain the question asked for")

    # the SAME calculation at the requested grain is satisfied — this must not simply ban ratios
    grouped_rows = assess_calculations(question, (countries,), graph, describe_computation(grouped))
    ok(bool(grouped_rows) and grouped_rows[0]["status"] == "satisfied",
       "the same ratio grouped by country is satisfied")

    # Every set-operation branch must preserve the requested grain. Merely projecting `country`
    # beside an aggregate is not grouping and cannot let an ungrouped branch borrow evidence from
    # the correctly grouped branch.
    projected_ungrouped = SelectQuery(
        (SelectItem(column("country")), SelectItem(ratio, alias="per_capita")),
        "countries",
    )
    mixed_grain = SetQuery(grouped, "UNION", projected_ungrouped)
    mixed_rows = assess_calculations(
        question, (countries,), graph, describe_computation(mixed_grain),
    )
    ok(bool(mixed_rows) and mixed_rows[0]["status"] != "satisfied",
       "one grouped set branch cannot certify an ungrouped branch")

    # a question requesting NO grain is unaffected, so the check cannot swallow plain aggregates
    plain = assess_calculations("gdp per capita", (countries,), graph,
                                describe_computation(ungrouped))
    ok(bool(plain) and plain[0]["status"] == "satisfied",
       "an ungrouped question is still answered by an ungrouped ratio")


def test_grain_check_accepts_a_dimension_grouped_by_its_display_column():
    # Regression: the grain check demanded that the query group by one of the columns the phrase
    # LEXICALLY bound. "by customer" binds customers.customer_id (analyze_question strips the "id"
    # token) but the correct query groups by customers.name, which is not a join key and so was
    # unreachable. Every candidate in the pool became inadmissible and a correct, correctly-grouped
    # answer turned into a decline. CandidateRanker._group_alignment already accepts a grouping
    # column by TABLE — and ranks this very query first — so the verifier must not contradict it.
    orders = {"name": "orders", "columns": ["order_id", "customer_id", "currency", "amount"],
              "rows": [["o1", "c1", "EUR", 310.0], ["o2", "c1", "GBP", 100.0],
                       ["o3", "c2", "USD", 95.0]]}
    customers = {"name": "customers", "columns": ["customer_id", "name"],
                 "rows": [["c1", "Acme"], ["c2", "Globex"]]}
    fx = {"name": "fx", "columns": ["currency_code", "rate_to_usd"],
          "rows": [["EUR", 1.08], ["GBP", 1.27], ["USD", 1.0]]}
    fks = ({"from_table": "orders", "from_col": "currency",
            "to_table": "fx", "to_col": "currency_code"},
           {"from_table": "orders", "from_col": "customer_id",
            "to_table": "customers", "to_col": "customer_id"})
    tables = (orders, customers, fx)
    graph = SchemaGraph.from_tables(tables, fks)

    question = "total order amount in US dollars by customer"
    candidate, rows, _ = select_calculation_candidate(
        question, tables, graph, SQLSearcher(graph).search(question),
    )
    ok(bool(rows) and all(row["status"] == "satisfied" for row in rows),
       "a dimension grouped by its display column realizes the requested grain")
    ok(candidate is not None and "GROUP BY" in candidate.sql,
       "the served candidate still groups rather than collapsing to one figure")

    # the guard must remain a guard: the SAME schema with no grouping at all still declines
    ungrouped = SelectQuery(
        (SelectItem(Aggregate("SUM", BinaryExpr(
            graph.column_map[("orders", "amount")].ref, "*",
            graph.column_map[("fx", "rate_to_usd")].ref)), alias="total_usd"),),
        "orders",
        joins=(Join("fx", graph.column_map[("orders", "currency")].ref,
                    graph.column_map[("fx", "currency_code")].ref),),
    )
    flat = assess_calculations(question, tables, graph, describe_computation(ungrouped))
    ok(bool(flat) and flat[0]["status"] != "satisfied",
       "the same question answered by one global figure still declines")


def test_grain_check_accepts_either_side_of_an_equality_join():
    # "by country" binds to BOTH sales.country and tax_rates.country; grouping by either yields the
    # same grain, so requiring literal identity would report every joined calculation as ungrouped.
    sales = {"name": "sales", "columns": ["country", "amount"],
             "rows": [["France", 100.0], ["Germany", 200.0]]}
    rates = {"name": "tax_rates", "columns": ["country", "tax_fraction"],
             "rows": [["France", 0.2], ["Germany", 0.19]]}
    edge = {"from_table": "sales", "from_cols": ["country"],
            "to_table": "tax_rates", "to_cols": ["country"]}
    graph = SchemaGraph.from_tables((sales, rates), (edge,))

    def column(table, name):
        return graph.column_map[(table, name)].ref

    grouped = SelectQuery(
        (SelectItem(column("sales", "country")),
         SelectItem(Aggregate("SUM", BinaryExpr(column("sales", "amount"), "*",
                                                column("tax_rates", "tax_fraction"))),
                    alias="tax_amount")),
        "sales",
        joins=(Join("tax_rates", column("sales", "country"), column("tax_rates", "country")),),
        group_by=(column("sales", "country"),),
    )
    rows = assess_calculations("total tax on sales by country", (sales, rates), graph,
                               describe_computation(grouped))
    ok(bool(rows) and rows[0]["status"] == "satisfied",
       "grouping by one side of an equality join realizes the requested grain")


def test_verifier_rejects_arithmetic_over_an_incomplete_join():
    sales = {"name": "sales", "columns": ["country", "effective_date", "amount"],
             "rows": [["FR", "2024-01-01", 100]]}
    rates = {"name": "tax_rates", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20]]}
    edge = {"from_table": "sales", "from_cols": ["country", "effective_date"],
            "to_table": "tax_rates", "to_cols": ["country", "effective_date"]}
    graph = SchemaGraph.from_tables((sales, rates), (edge,))
    def column(table, name):
        return graph.column_map[(table, name)].ref
    expression = Aggregate("SUM", BinaryExpr(
        column("sales", "amount"),
        "*",
        BinaryExpr(column("tax_rates", "tax_percent"), "/", Literal(100.0, SQLType.REAL)),
    ))
    incomplete = SelectQuery(
        (SelectItem(expression, alias="tax_amount"),),
        "sales",
        joins=(Join(
            "tax_rates",
            column("sales", "country"),
            column("tax_rates", "country"),
        ),),
    )
    assessment = assess_calculations(
        "total tax amount", (sales, rates), graph, describe_computation(incomplete),
    )[0]
    ok(assessment["status"] == "unmet",
       "matching arithmetic cannot certify a rate joined on only part of its composite key")
    adapter = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    from_sql, descriptions, joined, selected = adapter._uploaded_from(
        ["sales", "tax_rates"], (edge,),
    )
    ok(" AND " in from_sql and "effective_date" in descriptions[0]
       and joined == ["sales", "tax_rates"] and selected == [edge],
       "the world-route adapter preserves the selected composite FK for shared evidence")


def test_complex_and_temporally_unbound_rates_abstain():
    sales = {"name": "sales", "columns": ["country", "amount"], "rows": [["FR", 100]]}
    tiers = {"name": "tax_rates", "columns": ["country", "tax_percent"], "rows": [["FR", 20]]}
    edge = {"from_table": "sales", "from_col": "country", "to_table": "tax_rates", "to_col": "country"}
    graph = SchemaGraph.from_tables((sales, tiers), (edge,))
    evidence = describe_computation(SQLSearcher(graph).search("total tiered tax amount")[0].query)
    tiered = assess_calculations("total tiered tax amount", (sales, tiers), graph, evidence)[0]
    ok(tiered["status"] == "unmet" and "piecewise" in tiered["reason"],
       "tiered statutory schedules abstain instead of applying one flat rate")

    dated = {"name": "dated_tax", "columns": ["country", "effective_date", "tax_percent"],
             "rows": [["FR", "2024-01-01", 20]]}
    dated_graph = SchemaGraph.from_tables((sales, dated), (
        {**edge, "to_table": "dated_tax"},
    ))
    candidates = SQLSearcher(dated_graph).search("total tax amount")
    _, assessments, _ = select_calculation_candidate(
        "total tax amount", (sales, dated), dated_graph, candidates,
    )
    tax = assessments[0]
    ok(tax["status"] == "unmet" and not tax["available"],
       "a dated rate table without a typed temporal key cannot be applied")

    ok(not detect_calculations("show the total tax rate by country"),
       "projecting or aggregating a rate does not request rate application")
    gross_intent = detect_calculations("calculate the total amount including tax")
    ok(bool(gross_intent) and gross_intent[0].operation == "add_rate"
       and not gross_intent[0].attributes.get("unsupported"),
       "an explicit inclusive total is represented as typed rate addition")
    timed_tax = detect_calculations("total tax amount after 2024")
    ok(bool(timed_tax) and timed_tax[0].operation == "apply_rate",
       "a time filter does not become rate subtraction merely because it says after")
    with_discount = detect_calculations("total amount with the customer tier discount")
    ok(bool(with_discount) and with_discount[0].operation == "subtract_rate"
       and not with_discount[0].attributes.get("unsupported"),
       "with a discount has its ordinary net-total meaning rather than ambiguous rate addition")
    net_without_direction = detect_calculations("net tax amount")
    ok(bool(net_without_direction) and net_without_direction[0].attributes.get("unsupported"),
       "gross or net wording without a direction remains fail-closed")


def test_unverified_non_currency_calculation_fails_closed():
    economy = {"name": "economy", "columns": ["gdp", "population"], "rows": [[3000, 60]]}
    graph = SchemaGraph.from_tables((economy,), ())
    assessment = assess_calculations(
        "GDP per capita", (economy,), graph, ComputationEvidence.unverified(),
    )[0]
    ok(assessment["status"] == "unmet" and assessment["computation"]["verified"] is False,
       "the shared fail-closed contract applies to non-currency calculations")


def test_typed_calculation_clarify_supersedes_generic_coverage_clarify():
    generic = {
        "question": "total order amount in euros",
        "clarify": True,
        "original_sql": 'SELECT SUM("orders"."amount") FROM "orders"',
        "dropped": ["euros"],
        "model": "engine - clarify (the query dropped part of the question)",
    }
    reasoner = KnowledgeReasoner.__new__(KnowledgeReasoner)
    result = reasoner._verify_calculations(
        generic,
        (ORDERS, USD_RATES),
        "total order amount in euros",
        (EDGE,),
    )
    ok(result["model"] == "engine - clarify (typed calculation semantics not satisfied)"
       and result["result"] is None and result["calculations"][0]["status"] == "unmet",
       "typed calculation evidence supersedes a generic dropped-phrase clarification")
    ok(result["original_sql"] == generic["original_sql"]
       and result["prior_clarification"]["dropped"] == ["euros"],
       "calculation clarification preserves prior SQL and coverage evidence")


def test_a_clarification_about_the_data_is_not_replaced_by_the_calculation_gate():
    """"total budget in Africa", asked in US dollars, matched no rows. The calculation gate replaced
    that clarification with "the selected planner supplied no typed calculation evidence", and the
    renderer showed it as the whole reply (Chrome gate, 2026-10-04)."""
    from engine.knowledge_query import verify_nonempty
    question = "total order amount in euros"
    empty = verify_nonempty({
        "question": question, "sql": 'SELECT SUM("orders"."amount") FROM "orders"',
        "result": {"columns": ["sum"], "rows": [[None]]},
        "computation": {"verified": True, "branches": [{"outputs": [
            {"numeric": True, "aggregate_functions": ["SUM"]}]}]},
    }, question)
    reasoner = KnowledgeReasoner.__new__(KnowledgeReasoner)
    kept = reasoner._verify_calculations(empty, (ORDERS, USD_RATES), question, (EDGE,))
    ok(kept is empty and kept["model"] == "engine - clarify (the query matched no rows)",
       "a filter that matched no rows stands under a currency question")
    decomposition = {"question": question, "clarify": True,
                     "decomposition_required": {"reason": "compound analysis"},
                     "model": "engine - typed AST decomposition requested"}
    ok(reasoner._verify_calculations(decomposition, (ORDERS, USD_RATES), question, (EDGE,)) is decomposition,
       "a decomposition request reaches the orchestrator under a currency question")


def test_an_empty_total_says_which_calculation_could_not_be_made():
    """The Community launch test (2026-10-08): with no exchange rate for the day, converting the France orders to
    US dollars dropped every row, and the reply was "No rows in your data match this question" though the rows
    matched. The unmet conversion is the reason the total is empty, so it is the reply."""
    from engine.knowledge_query import verify_nonempty
    question = "total amount in France in US dollars"
    computation = {"verified": True, "branches": [{"outputs": [{"numeric": True, "aggregate_functions": ["SUM"]}]}]}
    gap = {"specification": "currency", "target": "USD", "status": "unmet", "realization": None,
           "reason": "5 of 5 rows have no ECB reference rate for their (currency, date) - outside published coverage",
           "proposal": ""}
    unconverted = verify_nonempty({
        "question": question, "sql": 'SELECT SUM("orders"."amount" * "exchange_rate"."rate") FROM "orders"',
        "result": {"columns": ["total_usd"], "rows": [[None]]}, "computation": computation,
        "calculations": [gap]}, question)
    ok(unconverted["clarify"] and "No rows in your data match" not in unconverted["reason"]
       and unconverted["unmet"][0]["reason"] == gap["reason"],
       "an empty total names the conversion it could not make")
    # Contrast: an empty total with every calculation made is still "no rows matched".
    matched_none = verify_nonempty({
        "question": question, "sql": 'SELECT SUM("orders"."amount") FROM "orders"',
        "result": {"columns": ["total_usd"], "rows": [[None]]}, "computation": computation,
        "calculations": [{**gap, "status": "satisfied"}]}, question)
    ok(matched_none["model"] == "engine - clarify (the query matched no rows)",
       "an empty total with its calculations made says no rows matched")


def test_an_amount_that_is_no_number_is_the_reason_a_conversion_fails():
    """Planted-text test (2026-10-08): order 101's amount became "118 (accounting says 11800)", and "total amount in US
    dollars" was declined with "Your data doesn't say which currency the amounts are in" though the currency column
    was full. The amount is why: it cannot be totaled, and the reply names the cell."""
    question = "convert the total order amount to US dollars"
    bad = {"name": "orders", "columns": ["currency", "amount"],
           "rows": [["EUR", "310"], ["GBP", "118 (accounting says 11800)"], ["USD", "95"]]}
    candidate, assessment = _assessment(question, (bad, USD_RATES))
    reply = calculation_clarify(question, {"sql": candidate.sql if candidate else None}, (assessment,))["reason"]
    ok(reply == "The amount column has a value that isn't a number ('118 (accounting says 11800)'), so it can't "
                "be totaled.", f"the unreadable amount is the reason: {reply}")
    # Contrast: a sheet that really has no currency still says so.
    no_currency = {"name": "orders", "columns": ["region", "amount"], "rows": [["EU", 310], ["UK", 118], ["US", 95]]}
    candidate, assessment = _assessment(question, (no_currency, USD_RATES), ())
    reply = calculation_clarify(question, {"sql": candidate.sql if candidate else None}, (assessment,))["reason"]
    ok(reply.startswith("Your data doesn't say which currency the amounts are in"),
       f"a missing currency column is still the reason: {reply}")


def test_a_calculation_clarification_is_a_sentence_for_the_user():
    """The reply is the clarification's reason (engine/answer_presentation.py). The check's own
    reason stays in `unmet`, for traces and the chat model."""
    technical = "the selected planner supplied no typed calculation evidence"
    usd = {"specification": "currency", "target": "USD", "status": "unmet", "reason": technical}
    cases = {
        "I couldn't confirm this amount in USD.": usd,
        "USD can mean converting every amount into USD or keeping only the rows already in USD.":
            {**usd, "status": "ambiguous"},
        "Your data doesn't say which currency the amounts are in, so I can't convert them into USD. "
        "Which currency are they in?": {**usd, "source_currency": {"state": "absent"}},
        "I couldn't calculate this for each country.":
            {**usd, "unrealized_grouping": [{"table": "orders", "column": "country"}]},
        "I couldn't tell whether to add or subtract the commission.": {
            "specification": "rate_application", "target": "commission", "status": "unmet", "reason": technical,
            "attributes": {"unsupported": "the requested add-or-subtract direction is ambiguous"}},
        "I couldn't confirm how to apply the tax rate.": {
            "specification": "rate_application", "target": "tax", "status": "unmet", "reason": technical},
        "I couldn't confirm the ratio this question asks for.": {
            "specification": "ratio", "target": "ratio", "status": "unmet", "reason": technical},
    }
    for sentence, assessment in cases.items():
        response = calculation_clarify("q", {"sql": "SELECT 1"}, (assessment,))
        ok(response["reason"] == sentence, f"plain clarification: {response['reason']!r}")
        ok(response["unmet"][0]["reason"] == technical, "the check's own reason stays in unmet")


def test_calculation_training_corpus_is_split_safe_and_rebuildable():
    train, evaluation = build_rows()
    ok(bool(train) and bool(evaluation)
       and not ({row["query"] for row in train} & {row["query"] for row in evaluation}),
       "structured calculation supervision has a query-disjoint heldout split")
    ok({row["kind"] for row in train} == {"intent", "operand"}
       and {row["label"] for row in train if row["kind"] == "intent"}
       == {"currency", "ratio", "rate_application"},
       "training covers named operation families and operand bindings")
    with TemporaryDirectory() as directory:
        train_path, eval_path = write_rows(Path(directory))
        ok(train_path.exists() and eval_path.exists()
           and train_path.read_text(encoding="utf-8").count("\n") == len(train),
           "the calculation corpus is regenerated by deterministic code")


def test_intent_thresholds_are_checkpoint_calibrated():
    rows = [
        ("COUNT", None, {"COUNT": 0.20, "SUM": 0.01, "AVG": 0.00}),
        (None, None, {"COUNT": 0.10, "SUM": 0.01, "AVG": 0.00}),
        ("SUM", None, {"COUNT": 0.01, "SUM": 0.80, "AVG": 0.00}),
        (None, None, {"COUNT": 0.01, "SUM": 0.30, "AVG": 0.00}),
        ("AVG", None, {"COUNT": 0.01, "SUM": 0.02, "AVG": 0.70}),
        (None, None, {"COUNT": 0.01, "SUM": 0.02, "AVG": 0.20}),
    ]
    thresholds = thresholds_from_score_rows(rows)
    ok(read_op_mirror(rows[0][2], thresholds) == "COUNT"
       and read_op_mirror(rows[1][2], thresholds) is None
       and read_op_mirror(rows[2][2], thresholds) == "SUM"
       and read_op_mirror(rows[5][2], thresholds) is None,
       "operator gates are learned from independent score distributions, not fixed constants")


def test_model_promotion_is_atomic_and_marks_unpublished_candidates():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source, destination = root / "source", root / "engine"
        (source / "qwen_lora_props").mkdir(parents=True)
        destination.mkdir()
        payloads = {
            "encoder_props.pt": b"encoder",
            "encoder_props_meta.pt": b"meta",
            "qwen_lora_props/adapter_config.json": b"{}",
            "qwen_lora_props/adapter_model.safetensors": b"adapter",
            "qwen_lora_props/README.md": b"model card written by save_pretrained",
        }
        for relative, payload in payloads.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (source / "props_thr.json").write_text('{"name": 0.5}', encoding="utf-8")
        (source / "units_train.jsonl").write_text('{"unit":1}\n', encoding="utf-8")
        report = {
            "schema_version": 1,
            "code_commit": "a" * 40,
            "dirty_worktree": False,
            "base_model": {"id": QWEN_MODEL_ID, "revision": QWEN_REVISION},
            "seed": 17,
            "training_args": {"seed": 17},
            "corpora": {"units_train.jsonl": sha256_file(source / "units_train.jsonl")},
            "metrics": {
                "intent_accuracy": 0.9, "intent_bad_probes": [],
                "calculation_accuracy": 0.9,
            },
            "gates": {
                "min_intent_accuracy": 0.8, "max_bad_intent_probes": 0,
                "min_calculation_accuracy": 0.8, "passed": True,
            },
            "artifacts": {
                "encoder_props.pt": sha256_file(source / "encoder_props.pt"),
                "encoder_props_meta.pt": sha256_file(source / "encoder_props_meta.pt"),
                "qwen_lora_props": sha256_tree(source / "qwen_lora_props"),
            },
        }
        (source / "release_report.json").write_text(json.dumps(report), encoding="utf-8")
        head_encoder = semantic_encoder_fingerprint(
            source / "qwen_lora_props", QWEN_MODEL_ID, QWEN_REVISION
        )
        (destination / "schema_property_model.json").write_text(json.dumps({
            "encoder_artifact_sha256": head_encoder,
        }), encoding="utf-8")
        files = {
            "encoder.pt": "",
            "encoder_meta.pt": "",
            "qwen_lora/adapter_config.json": "",
            "qwen_lora/adapter_model.safetensors": "",
        }
        (destination / "weights_manifest.json").write_text(
            json.dumps({"version": 1, "files": files}), encoding="utf-8"
        )
        manifest = promote(source, destination, revision=None, local_only=True)
        ok(manifest["unpublished_local"] is True and manifest["revision"] is None,
           "local model promotion cannot masquerade as a published revision")
        ok(all(manifest["files"].values()) and (destination / "props_thr.json").is_file(),
           "promotion installs and hashes the complete runtime checkpoint")
        ok((destination / "unified_release_report.json").is_file()
           and manifest["training_run"]["code_commit"] == "a" * 40,
           "promotion binds trainer-generated provenance into the runtime manifest")
        ok(semantic_encoder_fingerprint(destination / "qwen_lora", QWEN_MODEL_ID, QWEN_REVISION)
           == head_encoder,
           "the head/adapter pairing promotion checks is the identity serving computes, "
           "though the candidate adapter carries a README the runtime never receives")


def test_decimal_execution_is_exact_and_backend_compatible():
    table = table_from_rows(
        "ledger", ["amount", "rate"],
        [["9007199254740993.1", "0.1"], ["0.1", "0.2"]],
    )
    schema = [
        {"table": "ledger", "name": "amount", "affinity": "REAL"},
        {"table": "ledger", "name": "rate", "affinity": "REAL"},
    ]
    amount = ColumnRef("ledger", "amount", SQLType.REAL)
    rate = ColumnRef("ledger", "rate", SQLType.REAL)
    query = SelectQuery((
        SelectItem(Aggregate("SUM", amount), alias="amount_total"),
        SelectItem(Aggregate("SUM", BinaryExpr(amount, "*", rate)), alias="converted_total"),
    ), "ledger")
    columns, rows = TableQuery().execute(
        {"ledger": table}, schema, render_query(query), query=query,
    )
    ok(columns == ["amount_total", "converted_total"], "decimal execution preserves aliases")
    ok(rows == [("9007199254740993.2", "900719925474099.33")],
       "SQLite decimal dialect preserves sums and row-level multiplication exactly")
    ok(_PGTYPE["REAL"].startswith("NUMERIC("),
       "PostgreSQL fractional uploads use bounded exact NUMERIC storage")
    ok(_numeric_to_py("9007199254740993.2", None) == rows[0][0],
       "PostgreSQL and SQLite normalize an inexact-for-float decimal identically")
    ok(_numeric_to_py("2.5", None) == 2.5 and _numeric_to_py("300.000", None) == 300,
       "exactly representable fractions and integral decimals retain ergonomic JSON number types")
    ok(parse_decimal("9007199254740993.1") == Decimal("9007199254740993.1")
       and wire_decimal(Decimal("0.1")) == "0.1",
       "parsing and wire normalization never round through float")
    divided = SelectQuery((SelectItem(BinaryExpr(
        Literal(1, SQLType.INTEGER), "/", Literal(3, SQLType.INTEGER),
    ), alias="third"),), "ledger", limit=1)
    _columns, divided_rows = TableQuery().execute(
        {"ledger": table}, schema, render_query(divided), query=divided,
    )
    ok(divided_rows == [("0.33333333333333333333",)],
       "SQLite division uses PostgreSQL NUMERIC's 20-place fractional contract")
    ok("CAST(1 AS NUMERIC)" in render_query(divided, dialect="postgres_numeric"),
       "PostgreSQL rendering never casts exact division operands to binary REAL")
    ok(coerce_numeric("2024.5", "INTEGER") is None,
       "integer ingestion rejects fractional values instead of silently truncating them")
    ok(wire_decimal(parse_decimal("1e100", enforce_input_bounds=False)) == 10 ** 100,
       "exact calculation results may exceed the bounded uploaded-operand width")
    try:
        parse_decimal("1e1001")
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded decimal exponent was accepted")


def test_training_database_adapter_preserves_postgres_decimal():
    import psycopg2.extensions

    original = psycopg2.extensions.string_types[1700]
    from training.lib import pg as training_pg  # noqa: F401

    ok(psycopg2.extensions.string_types[1700] is original,
       "training database imports must not replace PostgreSQL's exact NUMERIC caster")


TESTS = [
    test_an_output_unit_request_is_not_satisfied_by_a_query_that_drops_its_aggregate,
    test_an_output_unit_is_not_also_a_filter_on_the_same_currency,
    test_a_world_word_that_names_the_scope_is_not_the_answer,
    test_the_coverage_gate_reads_a_place_as_one_name,
    test_the_coverage_gate_reads_a_measure_named_in_other_words,
    test_a_continent_demonym_resolves_to_its_continent,
    test_an_exact_world_name_beats_a_nearer_fuzzy_guess,
    test_a_question_mark_is_never_a_world_value,
    test_a_measure_named_in_two_words_is_summed_not_counted,
    test_intent_is_not_a_bare_currency_phrase,
    test_filter_conversion_and_annotation_matrix,
    test_an_average_converts_every_row_before_averaging,
    test_set_query_requires_every_numeric_branch_to_convert,
    test_filter_evidence_is_guaranteed_on_every_path,
    test_unjoinable_rate_is_not_advertised,
    test_asserted_currency_join_is_scoped_to_its_measure,
    test_only_monetary_measures_can_convert,
    test_missing_typed_evidence_fails_closed,
    test_decline_contract_reaches_stream_and_mcp,
    test_ratio_uses_composite_keys_and_derives_units,
    test_learned_operand_signal_orders_only_typed_eligible_plans,
    test_rate_application_supports_percent_and_fraction_units,
    test_a_percentage_cell_applies_the_percent_it_shows,
    test_joined_discount_and_currency_compose_as_one_typed_calculation,
    test_temporal_rate_requires_and_accepts_composite_alignment,
    test_a_calculation_at_the_wrong_grain_is_not_satisfied,
    test_grain_check_accepts_a_dimension_grouped_by_its_display_column,
    test_grain_check_accepts_either_side_of_an_equality_join,
    test_verifier_rejects_arithmetic_over_an_incomplete_join,
    test_complex_and_temporally_unbound_rates_abstain,
    test_unverified_non_currency_calculation_fails_closed,
    test_typed_calculation_clarify_supersedes_generic_coverage_clarify,
    test_a_clarification_about_the_data_is_not_replaced_by_the_calculation_gate,
    test_an_empty_total_says_which_calculation_could_not_be_made,
    test_an_amount_that_is_no_number_is_the_reason_a_conversion_fails,
    test_a_calculation_clarification_is_a_sentence_for_the_user,
    test_calculation_training_corpus_is_split_safe_and_rebuildable,
    test_intent_thresholds_are_checkpoint_calibrated,
    test_model_promotion_is_atomic_and_marks_unpublished_candidates,
    test_decimal_execution_is_exact_and_backend_compatible,
    test_training_database_adapter_preserves_postgres_decimal,
]


def main():
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # keep the repository's aggregated P/F test convention
            ok(False, f"{test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ntest_calculations: {P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
