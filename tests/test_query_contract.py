"""Contrast tests for request completeness, independently of model accuracy."""
from engine.query_contract import coverage, constraint_violations
from engine.question_rewrite import _preserves_explicit_constraints
from engine.sql_schema import SchemaGraph
from tests.test_sql_ast import _hermetic_planner, _gemini_planner, _model_query, _request, _select

TABLE = {"name": "orders", "columns": ["Amount", "city", "signed", "created"], "rows": [
    [120, "Paris", "2026-07-01", "2026-08-12"], [40, "東京", "2026-08-12", "2026-07-01"]]}


def test_repeated_headers_need_clarification_only_when_a_field_is_selected():
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType, Star
    graph = SchemaGraph.from_tables([{"name": "orders", "columns": ["Amount [column B]", "Amount [column C]", "Units"],
                                     "rows": [[10, 20, 99]]}], [])
    amount = ColumnRef("orders", "Amount [column B]", SQLType.INTEGER)
    query = SelectQuery((SelectItem(Aggregate("SUM", amount)),), "orders")
    assert any('Which repeated field' in v for v in constraint_violations('total Amount', query, graph))
    assert not constraint_violations('total Amount in column B', query, graph)
    count = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "orders")
    assert not constraint_violations('How many rows?', count, graph)
    unrelated = SelectQuery((SelectItem(Aggregate("COUNT", Star())),), "orders")
    assert any('Which repeated field' in v for v in constraint_violations('total Amount', unrelated, graph))
    units = SelectQuery((SelectItem(Aggregate('SUM', ColumnRef('orders','Units',SQLType.INTEGER))),), 'orders')
    assert constraint_violations('total Amount in column B', units, graph)


def test_non_numeric_formula_error_column_cannot_produce_a_sum():
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType
    table = {"name": "orders", "columns": ["Amount", "Units"],
             "rows": [[10, 2], ["#DIV/0!", 3]]}
    graph = SchemaGraph.from_tables([table], [])
    bad = SelectQuery((SelectItem(Aggregate("SUM", ColumnRef("orders", "Amount", SQLType.TEXT))),), "orders")
    units = SelectQuery((SelectItem(Aggregate("SUM", ColumnRef("orders", "Units", SQLType.INTEGER))),), "orders")
    assert any("Amount" in value and "nonnumeric or ambiguous" in value
               for value in constraint_violations("total Amount", bad, graph))
    assert not constraint_violations("total Units", units, graph)


def test_gemini_rewrite_cannot_pick_a_duplicate_field_or_substitute_another_measure():
    from engine.tables import csv_table
    from tests.test_sql_ast import _gemini_planner

    duplicate = csv_table("id,Amount,Amount,Units\n1,10,20,2\n2,30,40,3", "orders")
    planner, _ = _gemini_planner(question="total Amount in column C")
    result = planner.serve([duplicate], "total Amount")
    assert result["valid"] is False and "Which repeated field" in result["error"]
    assert result.get("result") is None

    broken = csv_table("id,Amount,Units\n1,10,2\n2,#DIV/0!,3", "orders")
    planner, _ = _gemini_planner(question="total Amount")
    result = planner.serve([broken], "total Amount")
    assert result["valid"] is False and "Amount" in result["error"]
    assert result.get("result") is None
    assert planner.serve([broken], "How many orders?")["result"]["rows"] == [[2]]
    assert planner.serve([broken], "total Units")["result"]["rows"] == [[5]]


def test_a_known_named_field_cannot_be_replaced_by_another_measure():
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType
    graph = SchemaGraph.from_tables([{"name": "orders", "columns": ["Amount", "Units"], "rows": [[10, 99]]}], [])
    query = SelectQuery((SelectItem(Aggregate("SUM", ColumnRef('orders','Units',SQLType.INTEGER))),), 'orders')
    assert any("total for 'Amount'" in v for v in constraint_violations('total Amount', query, graph))
    assert not constraint_violations('total Units', query, graph)
    count = SelectQuery((SelectItem(Aggregate('COUNT', ColumnRef('orders','Amount',SQLType.INTEGER))),), 'orders')
    assert any('requested total needs a sum' in v for v in constraint_violations('total Amount', count, graph))


def test_the_total_of_a_field_answers_a_spelled_total_field_name():
    """A customer's Stripe workbook, 2026-10-04: "What is the total Amount broken down by Plan and Currency?"
    spells the report tabs' Total Amount, and the contract refused SUM(Amount) by Plan and Currency from an
    export as leaving that field unused, so the question had no answer. "total Amount" also asks the total of
    Amount: the aggregate its first word asks for, over the field the rest names, uses what was asked."""
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType, Star
    graph = SchemaGraph.from_tables([
        {"name": "NT", "columns": ["Plan", "Currency", "Amount"], "rows": [["price_a", "usd", 10]]},
        {"name": "NT Report", "columns": ["Currency", "Total Amount"], "rows": [["usd", 10]]}], [])
    plan, currency = ColumnRef("NT", "Plan", SQLType.TEXT), ColumnRef("NT", "Currency", SQLType.TEXT)
    amount = ColumnRef("NT", "Amount", SQLType.INTEGER)
    question = "What is the total Amount broken down by Plan and Currency?"

    def grouped(aggregate):
        return SelectQuery((SelectItem(plan), SelectItem(currency), SelectItem(aggregate)), "NT",
                           group_by=(plan, currency))

    assert not constraint_violations(question, grouped(Aggregate("SUM", amount)), graph)
    # Contrast: another aggregate of Amount is not its total, and a count reads neither field.
    assert constraint_violations(question, grouped(Aggregate("AVG", amount)), graph)
    assert constraint_violations(question, grouped(Aggregate("COUNT", Star())), graph)


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


def test_all_rows_preserve_the_complete_projection():
    assert not check('show all rows', 'SELECT city FROM orders').complete
    assert check('show all rows', 'SELECT * FROM orders').complete
    assert not check('show all rows', 'SELECT * FROM orders LIMIT 50').complete


def test_complete_records_do_not_shrink_to_the_ordering_key():
    from tests.test_scale import fixture
    from tests.test_sql_ast import execute
    table = fixture(100)
    planner = _hermetic_planner()
    for question in ('show all rows ordered by id', 'show all columns ordered by id'):
        selected = _select(planner, question, [table]).candidate
        assert selected is not None
        rows = execute([table], selected.sql)
        assert len(rows) == 100 and all(len(row) == len(table['columns']) for row in rows)
        assert [row[0] for row in rows] == [row[0] for row in table['rows']]


def test_recipient_class_does_not_invent_a_grouping():
    table = {'name': 'payments', 'columns': ['Amount', 'Supplier'],
             'rows': [[10, 'Acme'], [20, 'Other']]}
    planner = _hermetic_planner()
    _, fks, sch, _ = _request(planner, [table])
    graph = SchemaGraph.from_planner(sch, fks)
    total = _model_query(planner, 'SELECT SUM(Amount) FROM payments', [table])
    assert coverage('What is the total amount paid to suppliers?', total, graph).complete
    assert not coverage('total Amount paid to German suppliers', total, graph).complete
    assert not coverage('total Amount paid to Acme', total, graph).complete
    assert not coverage('total Amount by Supplier', total, graph).complete
    selected = _select(planner, 'What is the total amount paid to suppliers?', [table]).candidate
    assert selected is not None and 'GROUP BY' not in selected.sql
    from tests.test_sql_ast import execute
    assert execute([table], selected.sql) == [(30,)]


def test_distinct_is_proven_by_the_selected_plan():
    assert check('show distinct city', 'SELECT DISTINCT city FROM orders').complete
    assert not check('show distinct city', 'SELECT city FROM orders').complete
    assert check('count different city', 'SELECT COUNT(DISTINCT city) FROM orders').complete
    assert not check('count different city', 'SELECT COUNT(city) FROM orders').complete
    assert check('show unique city', 'SELECT city FROM orders GROUP BY city').complete


def test_explicit_ordering_requires_direction_and_field():
    question = 'show city and Amount in descending order of Amount'
    assert check(question, 'SELECT city, Amount FROM orders ORDER BY Amount DESC').complete
    assert not check(question, 'SELECT city, Amount FROM orders ORDER BY Amount ASC').complete
    assert not check(question, 'SELECT city, Amount FROM orders ORDER BY city DESC').complete
    assert not check(question, 'SELECT city, Amount FROM orders').complete
    assert check('show Amount in ascending order of Amount',
                 'SELECT Amount FROM orders ORDER BY Amount ASC').complete


def test_operator_words_in_source_fields_remain_data():
    from engine.sql_ast import ColumnRef, SelectItem, SelectQuery, SQLType, render_query
    from engine.sql_candidate import ScoredQuery
    table = {'name': 'labels', 'columns': ['Unique', 'Descending'], 'rows': [['A', 'B']]}
    graph = SchemaGraph.from_tables([table], [])
    query = SelectQuery(tuple(SelectItem(ColumnRef('labels', name, SQLType.TEXT))
                              for name in table['columns']), 'labels')
    assert coverage('show Unique and Descending', ScoredQuery(query, render_query(query), 0.0, ()), graph).complete


def test_a_complete_lower_ranked_reading_survives():
    from unittest.mock import patch
    planner = _hermetic_planner()
    question = 'total Amount for 東京'
    pool = [_model_query(planner, 'SELECT SUM(Amount) FROM orders', [TABLE]),
            _model_query(planner, "SELECT SUM(Amount) FROM orders WHERE city='東京'", [TABLE])]
    with patch.object(planner, 'search_pool', return_value=pool):
        selection = _select(planner, question, [TABLE])
    assert selection.selected == 1 and "東京" in selection.candidate.sql


def test_named_table_scope_is_proven_by_the_ast_without_business_synonyms():
    tables=[{'name':'Checklist','columns':['Keyword','Avg. monthly searches'],
             'rows':[['home inspection checklist',5000]]},
            {'name':'Other','columns':['Keyword','Avg. monthly searches'],'rows':[['home inspection checklist',99]]}]
    planner=_hermetic_planner(); _,fks,sch,_=_request(planner,tables)
    graph=SchemaGraph.from_planner(sch,fks)
    question='What is the Avg. monthly searches for the Keyword home inspection checklist in the Checklist table?'
    for table, expected in [('Checklist',True),('Other',False)]:
        candidate=_model_query(planner,f'''SELECT AVG("Avg. monthly searches") FROM "{table}" WHERE Keyword='home inspection checklist' ''',tables)
        assert coverage(question,candidate,graph).complete is expected
    candidate=_model_query(planner,'''SELECT AVG("Avg. monthly searches") FROM "Checklist" WHERE Keyword='home inspection checklist' ''',tables)
    assert not coverage('average monthly searches for home inspection checklist in an unknown table',candidate,graph).complete


def test_long_headers_that_differ_at_the_end_stay_two_fields():
    """A Forms grid's items exceed PostgreSQL's 63 bytes and differ only at the end. Cut to their
    start, they became one repeated field, and every question about either was asked "Which repeated
    field should be used" (review, 2026-10-04). Identical long headers are still one repeated field."""
    from engine.column_names import canonical_columns
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType
    grid = 'How satisfied are you with the following aspects of our service? '
    names = canonical_columns(['Name', grid + '[Speed]', grid + '[Support]', grid + '[Support]'])
    assert all(len(name.encode('utf-8')) <= 63 for name in names)
    assert '[Speed]' in names[1] and '[Support]' in names[2], names
    graph = SchemaGraph.from_tables([{'name': 'responses', 'columns': names, 'rows': [['Ana', 4, 5, 3]]}], [])
    speed = ColumnRef('responses', names[1], SQLType.INTEGER)
    question = 'What is the average ' + grid + '[Speed]?'
    query = SelectQuery((SelectItem(Aggregate('AVG', speed)),), 'responses')
    assert not any('Which repeated field' in v for v in constraint_violations(question, query, graph))
    # Contrast: the two identical [Support] headers are one repeated field, and choosing one asks which.
    support = ColumnRef('responses', names[2], SQLType.INTEGER)
    chosen = SelectQuery((SelectItem(Aggregate('AVG', support)),), 'responses')
    assert any('Which repeated field' in v for v in constraint_violations(
        'What is the average ' + grid + '[Support]?', chosen, graph))


def _graph(planner, tables):
    _, fks, sch, _ = _request(planner, tables)
    return SchemaGraph.from_planner(sch, fks)


def test_an_aggregate_word_spelling_a_field_name_does_not_hide_the_requested_total():
    # The owner's keyword sheet: "Avg." opens the header. Deleting it from the question made
    # the filter column Keyword the requested total, and every candidate was refused.
    from engine.tables import csv_table
    tables = [{'name': 'Checklist', 'columns': ['Keyword', 'Avg. monthly searches'],
               'rows': [['home inspection checklist', 5000], ['forklift inspection checklist', 5000],
                        ['roof inspection', 700]]}]
    planner = _hermetic_planner()
    graph = _graph(planner, tables)
    total = 'What is the total Avg. monthly searches for the Keyword forklift inspection checklist?'
    summed = _model_query(planner, '''SELECT SUM("Avg. monthly searches") FROM Checklist WHERE Keyword='forklift inspection checklist' ''', tables)
    assert coverage(total, summed, graph).complete
    named = "What is the Avg. monthly searches for the Keyword 'home inspection checklist'?"
    for sql in ('''SELECT "Avg. monthly searches" FROM Checklist WHERE Keyword='home inspection checklist' ''',
                '''SELECT AVG("Avg. monthly searches") FROM Checklist WHERE Keyword='home inspection checklist' '''):
        assert coverage(named, _model_query(planner, sql, tables), graph).complete
    # Contrast: a word outside the name still asks for its calculation.
    listed = _model_query(planner, 'SELECT "Avg. monthly searches" FROM Checklist', tables)
    assert not coverage('What is the average Avg. monthly searches?', listed, graph).complete
    sheet = csv_table('Keyword,Avg. monthly searches\nhome inspection checklist,5000\n'
                      'forklift inspection checklist,5000\nroof inspection,700', 'Checklist')
    assert planner.serve([sheet], total)['result']['rows'] == [[5000]]


def test_a_value_the_query_compares_is_data_not_an_exclusion_cue():
    from engine.tables import csv_table
    planner = _hermetic_planner()
    forms = [{'name': 'responses', 'columns': ['Name', 'Newsletter'],
              'rows': [['Ana', 'Yes'], ['Bo', 'No'], ['Cy', 'No'], ['Di', 'No']]}]
    graph = _graph(planner, forms)
    said_no = _model_query(planner, "SELECT COUNT(*) FROM responses WHERE Newsletter='No'", forms)
    assert coverage('How many responses have Newsletter No?', said_no, graph).complete
    # Contrast: "not" outside the compared value still needs its exclusion.
    said_yes = _model_query(planner, "SELECT COUNT(*) FROM responses WHERE Newsletter='Yes'", forms)
    assert 'requested exclusion is missing' in coverage(
        'How many responses do not have Newsletter Yes?', said_yes, graph).violations
    tasks = csv_table('Task,Status\na,Not Started\nb,Not Started\nc,Done', 'tasks')
    assert planner.serve([tasks], 'How many tasks are Not Started?')['result']['rows'] == [[2]]
    assert planner.serve([tasks], 'How many tasks are not Done?')['result']['rows'] == [[2]]
    answers = csv_table('Name,Newsletter\nAna,Yes\nBo,No\nCy,No\nDi,No', 'responses')
    assert planner.serve([answers], 'How many responses have Newsletter No?')['result']['rows'] == [[3]]


def test_the_requested_grain_ends_with_its_clause_and_a_shared_name_counts_once():
    # "sorted by average Price" orders the groups; reading it as a second grain refused the
    # correct plan and served GROUP BY Category, Price.
    from engine.tables import csv_table
    planner = _hermetic_planner()
    tables = [{'name': 'items', 'columns': ['Item', 'Category', 'Price'],
               'rows': [['a', 'Tools', 30], ['b', 'Tools', 10], ['c', 'Toys', 5]]}]
    graph = _graph(planner, tables)
    question = 'What is the average Price by Category, sorted by average Price?'
    grouped = _model_query(planner, 'SELECT Category, AVG(Price) FROM items GROUP BY Category ORDER BY AVG(Price) DESC', tables)
    assert coverage(question, grouped, graph).complete
    # Contrast: a named grain the plan leaves out is still missing.
    overall = _model_query(planner, 'SELECT AVG(Price) FROM items', tables)
    assert 'requested output grain is missing' in coverage(
        'What is the average Price by Category?', overall, graph).violations
    items = csv_table('Item,Category,Price\na,Tools,30\nb,Tools,10\nc,Toys,5', 'items')
    assert planner.serve([items], question)['result']['rows'] == [['Tools', 20], ['Toys', 5]]
    # A name two tables share (a join key) is one requested grain, met by grouping either column.
    joined = [{'name': 'stadium', 'columns': ['Stadium_ID', 'Name'], 'rows': [[1, 'Arena'], [2, 'Bowl']]},
              {'name': 'concert', 'columns': ['concert_ID', 'Stadium_ID'], 'rows': [[10, 1], [11, 1], [12, 2]]}]
    graph = _graph(planner, joined)
    per_key = _model_query(planner, 'SELECT Stadium_ID, COUNT(*) FROM concert GROUP BY Stadium_ID', joined)
    assert coverage('How many concerts are there for each stadium id?', per_key, graph).complete


def test_own_data_adapter_preserves_calendar_proof_and_selection():
    from types import SimpleNamespace
    from engine.knowledge_tables import KnowledgeTableQuery
    tables=[{'name':'responses','columns':['submitted','budget'],
             'rows':[['2026-08-11',18000],['2026-08-15',4000]]}]
    planner=_hermetic_planner()
    _,fks,sch,_=_request(planner,tables)
    graph=SchemaGraph.from_planner(sch,fks)
    for question,sql in (
        ('How many leads were submitted after August 10, 2026?',
         "SELECT COUNT(submitted) FROM responses WHERE submitted >= '2026-08-11'"),
        ('What is the total budget from August 11 to August 15, 2026?',
         "SELECT SUM(budget) FROM responses WHERE submitted >= '2026-08-11' AND submitted < '2026-08-16'"),
    ):
        proof=coverage(question,_model_query(planner,sql,tables),graph).record()
        assert proof['complete']
        for carried in (proof,{'complete':False,'violations':['missing date']}):
            response={'sql':sql,'coverage':carried,'selection':{'served_by':'search','selected':0}}
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


TESTS = [value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]

if __name__ == '__main__':
    for test in TESTS:
        test()
        print('PASS', test.__name__)
