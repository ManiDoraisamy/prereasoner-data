"""Hermetic tests of the request-contract helpers the engine keeps (engine/query_contract.py: unreadable cells and
operands, the Coverage certificate) and of the own-data adapters that carry the planner's records into a reply."""
from engine.sql_schema import SchemaGraph
from tests.test_sql_ast import _hermetic_planner, _model_query, _request


def test_model_numeric_prediction_does_not_erase_notes_or_formula_errors():
    import numpy as np
    from engine.tables import TableQuery
    planner = TableQuery()
    planner.dims = [{"name": "is_num", "family": "struct", "dim_id": 0}]
    planner._encode = lambda names: np.ones((len(names), 1))
    planner._layers = lambda units, encoded: [encoded]
    schema, _, _ = planner.schema([{"name": "orders", "columns": ["amount", "units"],
                                   "rows": [[10, 2], ["#DIV/0!", 3]]}], [])
    assert schema[0]['affinity'] == 'TEXT'
    assert schema[0]['values'] == [10, '#DIV/0!']
    assert schema[1]['affinity'] == 'INTEGER'


def test_final_results_keep_more_than_fifty_rows():
    table = {'name': 'orders', 'columns': ['Amount'], 'rows': [[i] for i in range(1000)]}
    planner = _hermetic_planner()
    planner.pool = [_model_query(planner, 'SELECT "Amount" FROM "orders"', [table])]
    result = planner.serve([table], 'show Amount')
    assert result['valid'] and result['result']['rows'] == table['rows']


def test_world_coverage_keeps_unicode_constraints():
    from unittest.mock import patch
    from engine.knowledge_query import KnowledgeQuery
    owner = object.__new__(KnowledgeQuery)
    schema = [{'table': 'orders', 'name': 'Amount', 'affinity': 'REAL', 'values': [100]}]
    with patch.object(owner, '_phrase_qids', return_value={}), patch.object(owner, '_word_qid', return_value=None), \
            patch.object(owner, '_best_world_entity', return_value=None):
        assert '東京' in owner._uncovered('total Amount for 東京', schema, 'SELECT SUM(Amount) FROM orders')
        assert '東京' not in owner._uncovered('total Amount for 東京', schema, "SELECT SUM(Amount) FROM orders WHERE city='東京'")


def test_rate_magnitude_cannot_invent_its_unit():
    from engine.calculations.specifications import _rate_scale
    table = {'name': 'orders', 'columns': ['rate', 'rate_pct', 'rate_fraction'], 'rows': [[0.8, 0.8, 0.8]]}
    graph = SchemaGraph.from_tables([table], [])
    refs = {c.ref.name: c.ref for c in graph.columns}
    assert _rate_scale(graph, refs['rate']) is None
    assert _rate_scale(graph, refs['rate_pct']) == (100.0, 'percent')
    assert _rate_scale(graph, refs['rate_fraction']) == (1.0, 'fraction')


def test_long_headers_that_differ_at_the_end_stay_two_fields():
    """A Forms grid's items exceed PostgreSQL's 63 bytes and differ only at the end. Cut to their
    start, they became one repeated field, and every question about either was asked "Which repeated
    field should be used" (review, 2026-10-04). Headers that differ only at the end stay two fields."""
    from engine.column_names import canonical_columns
    grid = 'How satisfied are you with the following aspects of our service? '
    names = canonical_columns(['Name', grid + '[Speed]', grid + '[Support]', grid + '[Support]'])
    assert all(len(name.encode('utf-8')) <= 63 for name in names)
    assert '[Speed]' in names[1] and '[Support]' in names[2], names


def test_own_data_adapter_preserves_calendar_proof_and_selection():
    """KnowledgeTableQuery's own-data branch carries the planner's coverage certificate and selection record into the
    reply as the planner made them, and cannot manufacture a certificate the planner did not give."""
    from types import SimpleNamespace
    from engine.knowledge_tables import KnowledgeTableQuery
    from engine.query_contract import Coverage
    tables=[{'name':'responses','columns':['submitted','budget'],
             'rows':[['2026-08-11',18000],['2026-08-15',4000]]}]
    planner=_hermetic_planner()
    _,fks,sch,_=_request(planner,tables)
    for question,sql in (
        ('How many leads were submitted after August 10, 2026?',
         "SELECT COUNT(submitted) FROM responses WHERE submitted >= '2026-08-11'"),
        ('What is the total budget from August 11 to August 15, 2026?',
         "SELECT SUM(budget) FROM responses WHERE submitted >= '2026-08-11' AND submitted < '2026-08-16'"),
    ):
        proof=Coverage(True).record()
        assert proof['complete']
        for carried in (proof,{'complete':False,'violations':['missing date']}):
            response={'sql':sql,'coverage':carried,'selection':{'selected':0}}
            adapter=KnowledgeTableQuery.__new__(KnowledgeTableQuery)
            adapter.q11=SimpleNamespace(ingest=lambda t:(t,[]),schema=lambda n,f:(sch,{},{}),serve=lambda *a:response)
            adapter.route=lambda t:{};adapter.column_dims=lambda s,n:{};adapter.read_op_all=lambda q,s:None
            adapter._currency_conversion_binding=lambda *a:None;adapter._world_rate_binding=lambda *a:None
            adapter._own_value_matches=lambda *a:[];adapter.meaning_filter=lambda *a:None
            adapter.world_target=lambda *a:None;adapter._debug_input=lambda *a:{}
            actual=adapter.serve(tables,question)
            assert actual['coverage']==carried and actual['selection']==response['selection']
    adapter.q11.serve=lambda *a:{'sql':sql}
    assert 'coverage' not in adapter.serve(tables,question), 'an absent proof cannot be manufactured'


def test_the_coverage_gate_passes_an_error_through():
    """Production, 2026-10-05: the planner served nothing for "keyword volume for home inspection checklist", and
    the coverage gate read its error against no SQL. Every word was dropped, one named a world entity, and the reply
    became "Which interpretation should I use?" with nothing to choose. An error has no query to check."""
    from unittest.mock import patch
    from engine.entities import EntityQuery
    from engine.knowledge_query import KnowledgeQuery
    question = 'keyword volume for home inspection checklist'
    error = {'question': question, 'sql': None, 'result': None, 'error': 'planner: no executable AST candidate'}
    tables = [{'name': 'Checklist', 'columns': ['Keyword', 'Avg. monthly searches'],
               'rows': [['home inspection checklist', 5000]]}]
    owner = object.__new__(KnowledgeQuery)
    with patch.object(owner, 'ingest', return_value=(tables, [])),             patch.object(owner, 'schema', return_value=([{'table': 'Checklist', 'name': 'Keyword'}], {}, {})),             patch.object(owner, 'read_op_all', return_value=('SUM', 'Checklist', 'Avg. monthly searches')),             patch.object(owner, '_nongeo_plan', return_value=None),             patch.object(EntityQuery, 'serve', return_value=dict(error)),             patch.object(owner, '_uncovered', return_value=['volume']),             patch.object(owner, '_clarify', return_value=None),             patch.object(owner, '_word_qid', return_value='Q39297'):
        served = owner.serve(tables, question, schema='conversation')
    assert served == error, served


def test_the_own_data_adapter_carries_a_clarification():
    """The planner's clarification when the readers are not confident of their reading (engine/tables.py
    unsure_clarification) reaches the reply through the own-data branch of KnowledgeTableQuery.serve, which copies
    the planner's keys it knows, the reading's read-as line among them."""
    from types import SimpleNamespace
    from engine.knowledge_tables import KnowledgeTableQuery
    asked = {'sql': None, 'result': None, 'error': None, 'clarify': True, 'dropped': [],
             'read_as': 'the total Avg. monthly searches of the Keyword home inspection checklist',
             'reason': ("I read this as “the total Avg. monthly searches of the Keyword home inspection checklist”, "
                        "but I'm not confident that is what you asked. Ask again with the column names as your "
                        "sheet spells them.")}
    adapter = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    adapter.q11 = SimpleNamespace(ingest=lambda t: (t, []), schema=lambda n, f: ([], {}, {}), serve=lambda *a: asked)
    adapter.route = lambda t: {}; adapter.column_dims = lambda s, n: {}; adapter.read_op_all = lambda q, s: None
    adapter._currency_conversion_binding = lambda *a: None; adapter._world_rate_binding = lambda *a: None
    adapter._own_value_matches = lambda *a: []; adapter.meaning_filter = lambda *a: None
    adapter.world_target = lambda *a: None; adapter._debug_input = lambda *a: {}
    served = adapter.serve([{'name': 'Checklist', 'columns': ['Keyword'], 'rows': []}],
                           'keyword volume for home inspection checklist')
    assert {key: served.get(key) for key in ('clarify', 'reason', 'read_as', 'error')} == {
        key: asked[key] for key in ('clarify', 'reason', 'read_as', 'error')}, served


def test_a_named_amount_with_a_cell_that_is_no_number_is_refused_naming_the_cell():
    """Planted-text test (2026-10-08): with one amount "118 (accounting says 11800)" the amount column is text. Every
    cell that is not a number counts, however many there are (the planted-text review, 2026-10-08: a threshold on their
    share once let a mostly malformed amount be replaced by another column), and the refusal names them
    (query_contract.unreadable_cells)."""
    from engine.query_contract import unreadable_cells
    assert unreadable_cells(["n/a", "pending", "12", ""]) == ["n/a", "pending"]
    assert unreadable_cells(["118", "95", None]) is None
    # Negative: a text column the question groups by is no operand (sql_rank.aggregate_operand).
    from engine.sql_rank import aggregate_operand
    assert aggregate_operand("total amount by status", ["status"], "SUM") is None
    assert aggregate_operand("the total order amount by status", ["amount", "status"], "SUM") == "amount"
    # A phrase ends with its sentence (Spider DEV 945, 2026-10-08): the next sentence's "first names" was read as the
    # average's operand, and the question was refused for the names not being numbers.
    asked = ("Which professionals have operated a treatment that costs less than the average? "
             "Give me their first names and last names.")
    assert aggregate_operand(asked, ["first_name", "last_name"], "AVG") is None


def test_a_named_amount_that_is_no_number_is_never_replaced_by_a_saved_reference_column():
    """The planted-text test in the browser (2026-10-09): beside the owner's saved reference `ordered`, whose
    `estimated amount` is numeric, a malformed amount made "which city has the highest total amount" rank the cities by
    the reference's estimated amounts. The amount the question names cannot be totaled (query_contract.unreadable_operand);
    nothing stands in for it."""
    from engine.query_contract import unreadable_operand
    from engine.tables import csv_table, table_from_rows

    def orders(amount_of_101):
        return csv_table("id,city,ordered,currency,amount\n"
                         f"101,London,Pipe,GBP,{amount_of_101}\n102,London,Cap,GBP,95\n103,Paris,Hat,EUR,180\n"
                         "104,Cleveland,Van,USD,3400\n105,Cleveland,Snacks,USD,35", "orders")

    ordered = table_from_rows("ordered", ["ordered", "category", "estimated amount"],
                              [["Pipe", "Accessory", 18.5], ["Cap", "Apparel", 29.99], ["Hat", "Apparel", 32],
                               ["Van", "Vehicle", 15000], ["Snacks", "Snack", 5.99]])
    bad = orders("118 (accounting says 11800)")
    graph = SchemaGraph.from_tables([bad, ordered], ())
    assert unreadable_operand("total amount by city", graph) == ("amount", ["118 (accounting says 11800)"], "SUM")
    assert unreadable_operand("which city has the highest total amount?", graph)[0] == "amount"
    assert unreadable_operand("the average amount", graph)[2] == "AVG"
    # Contrast: a clean amount, or a question naming the reference's column more fully, asks for no refusal.
    assert unreadable_operand("total amount by city", SchemaGraph.from_tables([orders("118"), ordered], ())) is None
    assert unreadable_operand("total estimated amount by city", graph) is None
    assert unreadable_operand("how many orders", graph) is None
    # A column with no number at all is no measure: the release's dataset gate (2026-10-09) refused "total transfers
    # signed in August" for the dates of `signed`, read as the operand after the participle; a Yes/No `paid` alike.
    transfers = csv_table("hospital,signed,paid,transfers\nMayo Clinic,2026-08-04,Yes,14\n"
                          "Toronto General,2026-08-06,No,8\nMount Sinai,2026-07-30,Yes,9", "transfers")
    sheet = SchemaGraph.from_tables([transfers], ())
    assert unreadable_operand("total transfers signed in August", sheet) is None
    assert unreadable_operand("total transfers paid", sheet) is None
    assert unreadable_operand("total amount", SchemaGraph.from_tables([orders("n/a")], ()))[1] == ["n/a"]


def test_a_negated_comparative_is_a_bound_and_not_an_exclusion():
    """Spider DEV 153 and 1028, 2026-10-10: "weighing no less than 3000 and no more than 4000" read "no" as an
    exclusion. A negated comparative is a bound, never an exclusion cue (closed_class.EXCLUSION_CUES); a negation
    that is no bound is still an exclusion."""
    from engine.closed_class import EXCLUSION_CUES
    assert not EXCLUSION_CUES.search("cars weighing no less than 3000 and no more than 4000")
    # Negative: a negation that is no bound is still an exclusion.
    for asked in ("people who do not play poker", "semesters with no students enrolled", "has no more pets"):
        assert EXCLUSION_CUES.search(asked), asked
    # Review of 2026-10-10: the exclusion cue kept its own list of comparatives, so "not older than 35" asked for an
    # exclusion while "not higher than 35" did not. Every comparative the parser reads is a bound.
    for word in ("higher", "greater", "older", "younger", "taller", "cheaper", "more", "fewer"):
        assert not EXCLUSION_CUES.search(f"people not {word} than 35"), word


TESTS =[value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]

if __name__ == '__main__':
    for test in TESTS:
        test()
        print('PASS', test.__name__)
