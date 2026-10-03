"""Contrast tests for request completeness, independently of model accuracy."""
from engine.query_contract import coverage
from engine.question_rewrite import _preserves_explicit_constraints
from engine.sql_schema import SchemaGraph
from tests.test_sql_ast import _hermetic_planner, _gemini_planner, _model_query, _request, _select

TABLE = {"name": "orders", "columns": ["Amount", "city", "signed", "created"], "rows": [
    [120, "Paris", "2026-07-01", "2026-08-12"], [40, "東京", "2026-08-12", "2026-07-01"]]}


def check(question, sql):
    planner = _hermetic_planner()
    _, fks, sch, _ = _request(planner, [TABLE])
    graph = SchemaGraph.from_planner(sch, fks)
    return coverage(question, _model_query(planner, sql, [TABLE]), graph)


def test_threshold_polarity_and_boolean_scope_are_mandatory():
    q = 'total Amount greater than 100'
    assert check(q, 'SELECT SUM(Amount) FROM orders WHERE Amount > 100').complete
    for sql in ('SELECT SUM(Amount) FROM orders WHERE Amount < 100',
                'SELECT SUM(Amount) FROM orders LIMIT 100',
                "SELECT SUM(Amount) FROM orders WHERE Amount > 100 OR city = '東京'"):
        assert not check(q, sql).complete


def test_unicode_values_are_not_dropped():
    assert not check('total Amount for 東京', 'SELECT SUM(Amount) FROM orders').complete
    assert check('total Amount for 東京', "SELECT SUM(Amount) FROM orders WHERE city = '東京'").complete
    planner = _hermetic_planner()
    selection = _select(planner, 'total Amount for 東京', [TABLE])
    assert selection.candidate is not None and "東京" in selection.candidate.sql


def test_calendar_requires_the_requested_column():
    q = 'total Amount signed in August'
    assert check(q, "SELECT SUM(Amount) FROM orders WHERE signed >= '2026-08-01' AND signed < '2026-09-01'").complete is False  # a yearless month needs every year's August
    assert not check(q, 'SELECT SUM(Amount) FROM orders WHERE created > \'2026-08-01\'').complete
    assert not check('total Amount signed after August 10, 2026',
                     "SELECT SUM(Amount) FROM orders WHERE created >= '2026-08-11'").complete


def test_group_and_aggregate_are_not_optional():
    assert not check('total Amount by city', 'SELECT SUM(Amount) FROM orders').complete
    assert check('total Amount by city', 'SELECT city, SUM(Amount) FROM orders GROUP BY city').complete
    assert not check('maximum Amount', 'SELECT MIN(Amount) FROM orders').complete


def test_rewrites_preserve_known_semantics_and_unicode_literals():
    graph = SchemaGraph.from_tables([TABLE], [])
    for original, changed in (('total Amount not in Paris', 'total Amount in Paris'),
                              ('average Amount', 'total Amount'),
                              ('total Amount for 東京', 'total Amount'),
                              ('top 5 city by Amount', 'top 3 city by Amount'),
                              ('total Amount greater than 100', 'total Amount less than 100')):
        assert not _preserves_explicit_constraints(original, changed, graph)


def test_empty_baseline_does_not_excuse_incomplete_rewrite():
    planner, _ = _gemini_planner(question='total Amount for nonexistentmeasure')
    selection = _select(planner, 'unreadable phrase', [TABLE], searched=[])
    assert selection.candidate is None and selection.fallback.kind == 'none'


def test_final_results_keep_more_than_fifty_rows():
    table = {'name': 'orders', 'columns': ['Amount'], 'rows': [[i] for i in range(1000)]}
    result = _hermetic_planner().serve([table], 'show Amount')
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


def test_row_count_noun_does_not_depend_on_the_upload_filename():
    from tests.test_datasets import _tables, DATASET_DIR
    from tests.test_sql_ast import execute
    tables = _tables(DATASET_DIR / 'formfacade-leads')
    planner = _hermetic_planner()
    for question, gold in [('How many leads were submitted after August 10, 2026?', 5),
                           ('How many leads were submitted between August 4 and August 9, 2026?', 4)]:
        selected = _select(planner, question, tables).candidate
        assert selected is not None
        assert execute(tables, selected.sql) == [(gold,)]
    _, fks, schema, _ = _request(planner, tables)
    graph = SchemaGraph.from_planner(schema, fks)
    candidate = _model_query(planner, 'SELECT COUNT(*) FROM responses', tables)
    assert not coverage('How many German leads?', candidate, graph).complete


TESTS = [value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]

if __name__ == '__main__':
    for test in TESTS:
        test()
        print('PASS', test.__name__)
