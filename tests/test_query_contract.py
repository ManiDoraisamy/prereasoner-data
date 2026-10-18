"""Contrast tests for request completeness, independently of model accuracy."""
from engine.query_contract import coverage, constraint_violations
from engine.question_rewrite import _preserves_explicit_constraints
from engine.sql_schema import SchemaGraph
from regress.sql_import import import_sql
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
    # Amount holds numbers: the total is missing, not a field with cells to repair (review F4, 2026-10-11).
    assert "requested aggregate is missing: SUM of Amount" in constraint_violations('total Amount', query, graph)
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


def test_a_counted_noun_names_the_rows_when_its_field_cannot_be_reached():
    """"How many subscriptions are there by Status?" over subscription exports and report tabs (2026-10-05): the
    reports' Subscriptions column joins none of the exports, so counting an export's rows by Status answers the
    question. Contrast: a Subscriptions field a key joins to the counted table must be read."""
    from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType, Star
    question = "How many subscriptions are there by Status?"
    status = ColumnRef("NT", "Status", SQLType.TEXT)
    counted = SelectQuery((SelectItem(status), SelectItem(Aggregate("COUNT", Star()))), "NT", group_by=(status,))
    apart = SchemaGraph.from_tables([
        {"name": "NT", "columns": ["Plan", "Status"], "rows": [["a", "active"], ["b", "canceled"]]},
        {"name": "NT Report", "columns": ["Product", "Subscriptions"], "rows": [["x", 2]]}], [])
    assert not constraint_violations(question, counted, apart)
    joined = SchemaGraph.from_tables([
        {"name": "NT", "columns": ["Plan", "Status", "Product"], "rows": [["a", "active", "x"], ["b", "canceled", "x"]]},
        {"name": "Products", "columns": ["Product", "Subscriptions"], "rows": [["x", 2]]}],
        [("NT", "Product", "Products", "Product")])
    assert any("Subscriptions" in violation for violation in constraint_violations(question, counted, joined))


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
    """The planner's clarification (engine/tables.py unread_clarification) reaches the reply through the own-data
    branch of KnowledgeTableQuery.serve, which copies the planner's keys it knows."""
    from types import SimpleNamespace
    from engine.knowledge_tables import KnowledgeTableQuery
    asked = {'sql': None, 'result': None, 'error': None, 'clarify': True, 'dropped': ['volume'],
             'reason': "I couldn't tell which column “volume” means. Did you mean Avg. monthly searches?"}
    adapter = KnowledgeTableQuery.__new__(KnowledgeTableQuery)
    adapter.q11 = SimpleNamespace(ingest=lambda t: (t, []), schema=lambda n, f: ([], {}, {}), serve=lambda *a: asked)
    adapter.route = lambda t: {}; adapter.column_dims = lambda s, n: {}; adapter.read_op_all = lambda q, s: None
    adapter._currency_conversion_binding = lambda *a: None; adapter._world_rate_binding = lambda *a: None
    adapter._own_value_matches = lambda *a: []; adapter.meaning_filter = lambda *a: None
    adapter.world_target = lambda *a: None; adapter._debug_input = lambda *a: {}
    served = adapter.serve([{'name': 'Checklist', 'columns': ['Keyword'], 'rows': []}],
                           'keyword volume for home inspection checklist')
    assert {key: served.get(key) for key in ('clarify', 'reason', 'dropped', 'error')} == {
        key: asked[key] for key in ('clarify', 'reason', 'dropped', 'error')}, served


def _held_inside(graph):
    """SUM("Avg. monthly searches") over the keywords holding 'inspection checklist', as the search builds it."""
    from engine.sql_ast import Aggregate, Comparison, Literal, Lower, SelectItem, SelectQuery, render_query
    from engine.sql_candidate import ScoredQuery
    searches, keyword = (graph.column_map[('Checklist', name)].ref for name in ('Avg. monthly searches', 'Keyword'))
    query = SelectQuery((SelectItem(Aggregate('SUM', searches)),), 'Checklist',
                        where=Comparison(Lower(keyword), 'LIKE', Literal('%inspection checklist%', keyword.type)))
    return ScoredQuery(query, render_query(query), 0.0, ())


def test_a_keyword_volume_total_is_not_a_total_of_the_keyword_column():
    # The owner's keyword sheet (2026-10-06): "total keyword volume for all inspection checklist" took Keyword
    # for what "total" totals, so the sum Gemini's rewording found was refused with "The requested total for
    # 'Keyword' could not be computed from that field", and so was every other reading.
    from tests.test_sql_ast import KEYWORDS
    planner = _hermetic_planner()
    graph = _graph(planner, [KEYWORDS])
    summed = _held_inside(graph)
    assert not constraint_violations('total keyword volume for all inspection checklist', summed.query, graph)
    # Contrast: a total the question takes of the Keyword column itself still asks for that column.
    assert any('Keyword' in violation for violation in constraint_violations(
        'total Keyword for all inspection checklist', summed.query, graph))


def test_values_asked_to_hold_a_text_are_compared_with_like():
    # The same sheet: "all inspection checklist" and "containing 'inspection checklist'" ask for every keyword
    # holding the phrase. A reading, or a rewording's reading, that compares the one keyword 'inspection
    # checklist' answers for one of four; the words that asked are read once the query holds the LIKE.
    from tests.test_sql_ast import KEYWORDS
    planner = _hermetic_planner()
    graph = _graph(planner, [KEYWORDS])
    held = _held_inside(graph)
    one = _model_query(planner, '''SELECT SUM("Avg. monthly searches") FROM Checklist
                                   WHERE Keyword = 'inspection checklist' ''', [KEYWORDS])
    for question in ('total Avg. monthly searches for all inspection checklist',
                     "total Avg. monthly searches for every Keyword containing 'inspection checklist'",
                     'total Avg. monthly searches for keywords containing inspection checklist'):
        assert coverage(question, held, graph).complete, (question, coverage(question, held, graph).record())
        assert "the values holding 'inspection checklist' are not the ones compared" in \
            constraint_violations(question, one.query, graph), question
    # Contrast: without "all" or "containing" the phrase may be the one keyword.
    assert coverage('total Avg. monthly searches for inspection checklist', one, graph).complete
    # Negative: "all" over a value many rows hold asks for no LIKE.
    orders = {'name': 'orders', 'columns': ['City', 'Amount'],
              'rows': [['Paris', 10], ['Paris', 20], ['Paris Nord', 7]]}
    paris = _model_query(planner, "SELECT SUM(Amount) FROM orders WHERE City = 'Paris'", [orders])
    assert coverage('total Amount for all Paris orders', paris, _graph(planner, [orders])).complete
    # The column named before "containing" is the one the text is in. Spider DEV 970 served a reading that
    # searched the street for "a city containing the substring 'West'", once the cue was read (2026-10-06).
    from engine.sql_ast import Comparison, Literal, Lower, SelectItem, SelectQuery, render_query
    from engine.sql_candidate import ScoredQuery
    staff = {'name': 'professionals', 'columns': ['role', 'street', 'city'],
             'rows': [['vet', 'West Road', 'Paris'], ['nurse', 'Main Street', 'West Haven']]}
    graph = _graph(planner, [staff])
    role, street, city = (graph.column_map[('professionals', name)].ref for name in ('role', 'street', 'city'))

    def searched_in(column):
        query = SelectQuery((SelectItem(role), SelectItem(street), SelectItem(city)), 'professionals',
                            where=Comparison(Lower(column), 'LIKE', Literal('%west%', column.type)))
        return ScoredQuery(query, render_query(query), 0.0, ())

    question = "Which professionals live in a city containing the substring 'West'? List their role, street and city."
    assert coverage(question, searched_in(city), graph).complete
    assert "the values holding 'west' are not the ones compared" in constraint_violations(
        question, searched_in(street).query, graph)
    # Contrast: on a street containing it, the street is searched.
    on_street = "Which professionals live on a street containing the substring 'West'? List their role, street and city."
    assert coverage(on_street, searched_in(street), graph).complete
    assert not coverage(on_street, searched_in(city), graph).complete


def test_an_exclusion_is_the_one_the_question_makes():
    # A release review (2026-10-07): "orders not Done in France" was served as status != 'Done' AND country !=
    # 'France', the orders outside France, and the check passed it, as it passed any exclusion at all (status =
    # 'Done' AND country != 'France' too). A negation excludes the value after it and the values listed with it.
    planner = _hermetic_planner()
    orders = [{'name': 'orders', 'columns': ['status', 'country', 'Amount'],
               'rows': [['Done', 'France', 10], ['Open', 'France', 15], ['Done', 'Spain', 30],
                        ['Cancelled', 'France', 7], ['Not Started', 'Spain', 9]]}]
    graph = _graph(planner, orders)
    question = 'orders not Done in France'
    right = _model_query(planner, "SELECT * FROM orders WHERE status != 'Done' AND country = 'France'", orders)
    assert not constraint_violations(question, right.query, graph)
    for sql in ("SELECT * FROM orders WHERE status != 'Done' AND country != 'France'",
                "SELECT * FROM orders WHERE status = 'Done' AND country != 'France'"):
        assert constraint_violations(question, _model_query(planner, sql, orders).query, graph), sql
    # Contrast: a value listed with the excluded one is excluded too.
    both = _model_query(planner, "SELECT * FROM orders WHERE status != 'Done' AND status != 'Cancelled'", orders)
    assert not constraint_violations('orders not Done or Cancelled', both.query, graph)
    # Negative: a negation word inside a value is the value's.
    started = _model_query(planner, "SELECT * FROM orders WHERE status = 'Not Started' AND country = 'Spain'", orders)
    assert not constraint_violations('orders Not Started in Spain', started.query, graph)


def test_the_excluded_value_is_excluded_whatever_wraps_its_column():
    # A release review, revision 2 (2026-10-07): the polarity check read only a bare column, so LOWER(status) =
    # 'done' kept the excluded value and passed, and any unrelated exclusion (Amount != 0) stood for the one asked.
    planner = _hermetic_planner()
    orders = [{'name': 'orders', 'columns': ['status', 'country', 'Amount'],
               'rows': [['Done', 'France', 10], ['Open', 'France', 15], ['Done', 'Spain', 30],
                        ['Cancelled', 'France', 7], ['Not Started', 'Spain', 9]]}]
    graph = _graph(planner, orders)

    from dataclasses import replace

    from engine.sql_ast import BooleanExpr, ColumnRef, Lower

    def lowered(predicate):          # status = 'done' read as LOWER(status) = 'done'
        if isinstance(predicate, BooleanExpr):
            return replace(predicate, terms=tuple(lowered(term) for term in predicate.terms))
        if isinstance(predicate.left, ColumnRef) and predicate.left.name == 'status':
            return replace(predicate, left=Lower(predicate.left))
        return predicate

    def refused(question, sql, lower=False):
        query = _model_query(planner, sql, orders).query
        if lower:
            query = replace(query, where=lowered(query.where))
        return constraint_violations(question, query, graph)

    question = 'orders not Done in France'
    assert refused(question, "SELECT * FROM orders WHERE status = 'done' AND country = 'France' AND Amount != 0",
                   lower=True)
    for sql in ("SELECT * FROM orders WHERE country = 'France' AND Amount != 0",
                "SELECT * FROM orders WHERE 'Done' = status AND country = 'France'"):
        assert refused(question, sql), sql
    assert not refused(question, "SELECT * FROM orders WHERE status != 'done' AND country = 'France'", lower=True)
    for sql in ("SELECT * FROM orders WHERE 'Done' != status AND country = 'France'",
                "SELECT * FROM orders WHERE status NOT IN ('Done') AND country = 'France'"):
        assert not refused(question, sql), sql
    # Both listed values are excluded, or the query is refused.
    assert not refused('orders not Done or Cancelled', "SELECT * FROM orders WHERE status NOT IN ('Done', 'Cancelled')")
    assert refused('orders not Done or Cancelled', "SELECT * FROM orders WHERE status NOT IN ('Done')")
    # Contrast: an exclusion made inside a negated subquery is the question's.
    assert not refused('orders not Done in France',
                       "SELECT * FROM orders WHERE country = 'France' AND status NOT IN "
                       "(SELECT status FROM orders WHERE status = 'Done')")


def _reading(tables, question, sql):
    """The production completeness reading of ``question`` by the imported ``sql``: (complete, unread)."""
    from engine.query_contract import read_question
    planner = _hermetic_planner()
    graph = _graph(planner, tables)
    candidate = _model_query(planner, sql, tables)
    return coverage(question, candidate, graph).complete, read_question(question, candidate, graph).unread


def test_a_schema_word_is_read_from_the_tables_the_query_reads():
    """Spider DEV, 2026-10-08. Every column's words once counted as read, whatever table the query read: an
    answer over the students' ages was complete for "the average weight of pets", because the pets table has a
    weight column. And a name was read as one word, so "the life expectancy of Angola" was unread over the
    LifeExpectancy column the query read: the search splits names at their capitals, and the check now does."""
    pets = [{"name": "students", "columns": ["StuID", "LName", "Age"], "rows": [[1, "Smith", 20]]},
            {"name": "pets", "columns": ["PetID", "PetType", "weight"], "rows": [[1, "cat", 12.0]]}]
    assert _reading(pets, "Find the average weight of pets.", "SELECT AVG(Age) FROM students") == (
        False, ("weight", "pets"))
    assert _reading(pets, "Find the average weight of pets.", "SELECT AVG(weight) FROM pets") == (True, ())
    world = [{"name": "country", "columns": ["Code", "Name", "LifeExpectancy"],
              "rows": [["AGO", "Angola", 38.3], ["ABW", "Aruba", 78.4]]},
             {"name": "city", "columns": ["ID", "Name", "CountryCode"], "rows": [[1, "Luanda", "AGO"]]}]
    question = "What is the life expectancy of Angola?"
    assert _reading(world, question, "SELECT LifeExpectancy FROM country WHERE Name = 'Angola'") == (True, ())
    # Negative: a word no table the query reads names stays unread.
    assert _reading(world, "What is the literacy rate of Angola?",
                    "SELECT LifeExpectancy FROM country WHERE Name = 'Angola'") == (False, ("literacy", "rate"))
    # Spider DEV 738: a word that names only an unused column of a table the query reads, once the name is split
    # ("IsOfficial"), is a qualifier the query left out.
    languages = [{"name": "country", "columns": ["Code", "Name"], "rows": [["AFG", "Afghanistan"]]},
                 {"name": "countrylanguage", "columns": ["CountryCode", "Language", "IsOfficial"],
                  "rows": [["AFG", "Pashto", "T"], ["AFG", "Uzbek", "F"]]}]
    question = "How many official languages does Afghanistan have?"
    join = ('FROM countrylanguage JOIN country ON countrylanguage.CountryCode = country.Code '
            "WHERE country.Name = 'Afghanistan'")
    assert _reading(languages, question, "SELECT COUNT(Language) " + join) == (False, ("official",))
    assert _reading(languages, question, "SELECT COUNT(Language) " + join + " AND IsOfficial = 'T'") == (True, ())


def test_a_name_the_question_spells_in_two_words_is_read():
    """Spider network_1: "high schoolers" names the Highschooler table the query reads (2026-10-08)."""
    school = [{"name": "Highschooler", "columns": ["ID", "name", "grade"], "rows": [[1, "Jordan", 9], [2, "Ana", 10]]},
              {"name": "Likes", "columns": ["student_id", "liked_id"], "rows": [[1, 2]]}]
    sql = "SELECT name FROM Highschooler WHERE grade = 9"
    assert _reading(school, "List the names of high schoolers in grade 9.", sql) == (True, ())
    # Contrast: two words that join into no name the query reads stay unread.
    assert _reading(school, "List the names of high achievers in grade 9.", sql) == (False, ("high", "achievers"))


def test_a_spelled_number_is_read_when_the_query_keeps_that_many():
    """Spider DEV, 2026-10-08: "the two" was unread over a query keeping two rows. Its review: the number must be read
    in its role, not wherever the SQL holds it. A cutoff in a question that ranks is the query's LIMIT, so a 2 in a
    predicate does not read "the two" over a query keeping three rows; after "more than" it is a comparison."""
    question = "List the names of the two people with the largest Age."
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people ORDER BY Age DESC LIMIT 2") == (True, ())
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people ORDER BY Age DESC LIMIT 3") == (False, ("two",))
    excluding = "List the names of the two people with the largest Age, excluding Person_ID 2."
    assert _reading([PEOPLE_TABLE], excluding,
                    "SELECT Name FROM people WHERE Person_ID != 2 ORDER BY Age DESC LIMIT 3") == (False, ("two",))
    assert _reading([PEOPLE_TABLE], excluding,
                    "SELECT Name FROM people WHERE Person_ID != 2 ORDER BY Age DESC LIMIT 2") == (True, ())
    threshold = "List the names of people with an Age of more than two."
    assert _reading([PEOPLE_TABLE], threshold, "SELECT Name FROM people WHERE Age > 2") == (True, ())
    assert _reading([PEOPLE_TABLE], threshold, "SELECT Name FROM people WHERE Age > 3 LIMIT 2") == (False, ("two",))


def test_order_words_are_read_by_a_query_that_orders_its_rows():
    """Spider DEV, 2026-10-08: "in alphabetical order" was unread over a query ordering by the name. Its review: any
    ordering read it, by age or Z to A. "Alphabetical" is read by an order on a text field in the direction asked,
    and the search builds that order for "in alphabetical order" as for "alphabetically"."""
    question = "List the names in alphabetical order."
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people ORDER BY Name") == (True, ())
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people") == (False, ("alphabetical", "order"))
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people ORDER BY Age") == (False, ("alphabetical",))
    assert _reading([PEOPLE_TABLE], question, "SELECT Name FROM people ORDER BY Name DESC") == (False, ("alphabetical",))
    reverse = "List the names in reverse alphabetical order."
    assert _reading([PEOPLE_TABLE], reverse, "SELECT Name FROM people ORDER BY Name DESC") == (True, ())
    assert _reading([PEOPLE_TABLE], reverse, "SELECT Name FROM people ORDER BY Name")[0] is False
    # Served: both forms order the names A to Z, and the reversed one Z to A.
    for asked, expected in (("List the names in alphabetical order.", ["Alice", "Bob", "Cara"]),
                            ("List the names ordered alphabetically.", ["Alice", "Bob", "Cara"]),
                            ("List the names in reverse alphabetical order.", ["Cara", "Bob", "Alice"])):
        served = _hermetic_planner().serve([PEOPLE_TABLE], asked)
        assert [row[0] for row in served["result"]["rows"]] == expected, (asked, served.get("sql"))


def test_a_participle_relating_rows_to_a_compared_value_is_read():
    """Spider flight_2, 2026-10-08: "flights departing from APG" was unread over a query comparing the source
    airport with 'APG'. A participle the data holds as a value is a filter: "orders returned by Alice" over a
    status holding 'Returned' still asks for the returned ones."""
    flights = [{"name": "flights", "columns": ["Airline", "FlightNo", "SourceAirport", "DestAirport"],
                "rows": [[1, 28, "APG", "ASY"], [1, 29, "CVO", "ASY"]]}]
    question = "List the flight numbers of flights departing from APG."
    assert _reading(flights, question, "SELECT FlightNo FROM flights WHERE SourceAirport = 'APG'") == (True, ())
    # Contrast: no compared value, nothing read.
    assert _reading(flights, question, "SELECT FlightNo FROM flights") == (False, ("departing", "apg"))
    # Its review: with APG a departure and an arrival, the value no longer says which column the relationship is,
    # and DestAirport = 'APG' (arrivals) was read as departures. The ranker's travel reading says which
    # (sql_rank.travel_direction): "departing" is the source, so only SourceAirport reads it.
    both_ways = [{**flights[0], "rows": [[1, 28, "APG", "ASY"], [1, 29, "ASY", "APG"]]}]
    assert _reading(both_ways, question, "SELECT FlightNo FROM flights WHERE SourceAirport = 'APG'") == (True, ())
    assert _reading(both_ways, question, "SELECT FlightNo FROM flights WHERE DestAirport = 'APG'") == (
        False, ("departing",))
    # The same through a second key between the tables: departing from a city is the join on the source.
    airports = {"name": "airports", "columns": ["AirportCode", "City"], "rows": [["APG", "Aberdeen"], ["ASY", "Ashley"]]}
    routed = [{**flights[0], "rows": [[1, 28, "APG", "ASY"]]}, airports]
    keys = [{"from_table": "flights", "from_col": "SourceAirport", "to_table": "airports", "to_col": "AirportCode"},
            {"from_table": "flights", "from_col": "DestAirport", "to_table": "airports", "to_col": "AirportCode"}]
    planner = _hermetic_planner()
    graph = SchemaGraph.from_tables(routed, keys)
    from engine.query_contract import read_question
    asked = "List the flight numbers of flights departing from Aberdeen."
    for column, unread in (("DestAirport", True), ("SourceAirport", False)):
        candidate = _model_query(planner, f"SELECT FlightNo FROM flights JOIN airports ON flights.{column} = "
                                          "airports.AirportCode WHERE airports.City = 'Aberdeen'", routed)
        assert ("departing" in read_question(asked, candidate, graph).unread) is unread, column
    # A participle with no such reading cannot choose between two columns that select different rows.
    shipped = [{"name": "orders", "columns": ["id", "billing_city", "shipping_city"],
                "rows": [[1, "Paris", "Lyon"], [2, "Lyon", "Paris"]]}]
    assert _reading(shipped, "List the ids of orders shipped to Paris.",
                    "SELECT id FROM orders WHERE billing_city = 'Paris'") == (False, ("shipped",))
    # One value in two columns of the same row is one relationship: a country's Name and LocalName.
    languages = [{"name": "country", "columns": ["Code", "Name", "LocalName"], "rows": [["ABW", "Aruba", "Aruba"]]},
                 {"name": "countrylanguage", "columns": ["CountryCode", "Language"], "rows": [["ABW", "Dutch"]]}]
    assert _reading(languages, "What languages are spoken in Aruba?",
                    "SELECT Language FROM countrylanguage JOIN country ON countrylanguage.CountryCode = country.Code "
                    "WHERE country.Name = 'Aruba'") == (True, ())
    orders = [{"name": "orders", "columns": ["customer", "status", "amount"],
               "rows": [["Alice", "Returned", 10], ["Alice", "Shipped", 20]]}]
    assert _reading(orders, "List the amounts of orders returned by Alice.",
                    "SELECT amount FROM orders WHERE customer = 'Alice'") == (False, ("returned",))
    # Spider DEV 416: the participle's object is its own noun phrase, not the clause attached to it. "the museums"
    # is no compared value, so "working" stays unread over a query averaging the opening year.
    museums = [{"name": "museum", "columns": ["Museum_ID", "Name", "Num_of_Staff", "Open_Year"],
                "rows": [[1, "Plaza", 62, 2000], [2, "Capital", 25, 2012]]}]
    _complete, unread = _reading(museums,
                                 "Find the average number of staff working for the museums that were opened before 2009.",
                                 "SELECT AVG(Open_Year) FROM museum WHERE Open_Year < 2009")
    assert "working" in unread


def test_a_counted_noun_reads_as_the_rows_counted_only_when_it_names_a_field():
    """Spider DEV 728 (2026-10-08): "How many people live in Gelderland district?" over cities and countries was
    served as a count of the district's cities. Across several tables a counted noun names the rows counted only
    when it is a field's name ("how many subscriptions by Status", test_sql_ast); "people" names none."""
    world = [{"name": "city", "columns": ["ID", "Name", "District", "Population"],
              "rows": [[1, "Arnhem", "Gelderland", 138020], [2, "Nijmegen", "Gelderland", 152463]]},
             {"name": "country", "columns": ["Code", "Name"], "rows": [["NLD", "Netherlands"]]}]
    question = "How many people live in Gelderland district?"
    assert _reading(world, question, "SELECT COUNT(*) FROM city WHERE District = 'Gelderland'")[1] == ("people",)
    assert _reading(world, "How many names are in Gelderland district?",
                    "SELECT COUNT(*) FROM city WHERE District = 'Gelderland'") == (True, ())


def test_a_named_amount_with_a_cell_that_is_no_number_is_refused_naming_the_cell():
    """Planted-text test (2026-10-08): with one amount "118 (accounting says 11800)" the amount column is text. The
    own-data planner already refused to total another field; the refusal now names the cell, in the words compose and
    the currency check use (answer_presentation.unreadable_measure_reason)."""
    from engine.tables import csv_table
    orders = csv_table("id,currency,amount\n101,GBP,118 (accounting says 11800)\n102,GBP,95\n103,GBP,72", "orders")
    served = _hermetic_planner().serve([orders], "total amount of GBP orders")
    assert served["valid"] is False and served.get("result") is None, served
    assert served["error"] == ("The amount column has a value that isn't a number ('118 (accounting says 11800)'), "
                               "so it can't be totaled."), served["error"]
    # Contrast: a clean amount column is totaled.
    clean = csv_table("id,currency,amount\n101,GBP,118\n102,GBP,95\n103,GBP,72", "orders")
    assert _hermetic_planner().serve([clean], "total amount of GBP orders")["result"]["rows"] == [[285]]
    # Every cell that is not a number counts, however many there are (the planted-text review, 2026-10-08: a
    # threshold on their share once let a mostly malformed amount be replaced by another column).
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
    the reference's estimated amounts. The amount the question names cannot be totaled; nothing stands in for it."""
    from engine.tables import csv_table, table_from_rows

    def orders(amount_of_101):
        return csv_table("id,city,ordered,currency,amount\n"
                         f"101,London,Pipe,GBP,{amount_of_101}\n102,London,Cap,GBP,95\n103,Paris,Hat,EUR,180\n"
                         "104,Cleveland,Van,USD,3400\n105,Cleveland,Snacks,USD,35", "orders")

    ordered = table_from_rows("ordered", ["ordered", "category", "estimated amount"],
                              [["Pipe", "Accessory", 18.5], ["Cap", "Apparel", 29.99], ["Hat", "Apparel", 32],
                               ["Van", "Vehicle", 15000], ["Snacks", "Snack", 5.99]])
    bad = orders("118 (accounting says 11800)")
    reason = "The amount column has a value that isn't a number ('118 (accounting says 11800)'), so it can't be totaled."
    for question in ("which city has the highest total amount?", "total amount by city", "total amount"):
        served = _hermetic_planner().serve([dict(bad), dict(ordered)], question)
        assert served["valid"] is False and served.get("result") is None, (question, served.get("sql"))
        assert served["error"] == reason, (question, served["error"])
    # The reading the live search served: the amount kept as a grouping column beside a total of the estimated
    # amounts, which the requested-total check passed as a ranked grouping. Selection may not serve it.
    planner, tables = _hermetic_planner(), [dict(bad), dict(ordered)]
    swapped = _model_query(planner, 'SELECT "orders"."city", "orders"."amount", SUM("ordered"."estimated amount") '
                                    'FROM "orders" JOIN "ordered" ON "ordered"."ordered" = "orders"."ordered" '
                                    'GROUP BY "orders"."city", "orders"."amount" '
                                    'ORDER BY SUM("ordered"."estimated amount") DESC LIMIT 1', tables)
    _, fks, sch, _ = _request(planner, tables)
    question = "which city has the highest total amount?"
    assert reason in constraint_violations(question, swapped.query, SchemaGraph.from_planner(sch, fks))
    assert _select(planner, question, tables, searched=[swapped]).candidate is None
    # Contrast: a clean amount is totaled beside the same reference.
    clean = _hermetic_planner().serve([orders("118"), dict(ordered)], "which city has the highest total amount?")
    assert clean["result"]["rows"][0][0] == "Cleveland", clean["result"]
    # Negative: a count asks no amount, and a question naming the reference's column more fully asks for that column.
    assert _hermetic_planner().serve([dict(bad), dict(ordered)], "how many orders")["result"]["rows"] == [[5]]
    estimated = _hermetic_planner().serve([dict(bad), dict(ordered)], "which city has the highest total estimated amount?")
    assert estimated["result"]["rows"][0][0] == "Cleveland" and "estimated amount" in estimated["sql"], estimated
    from engine.query_contract import unreadable_operand
    graph = SchemaGraph.from_tables([bad, ordered], ())
    assert unreadable_operand("total amount by city", graph) == ("amount", ["118 (accounting says 11800)"], "SUM")
    assert unreadable_operand("the average amount", graph)[2] == "AVG"
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


def _violations(tables, question, sql):
    planner = _hermetic_planner()
    return constraint_violations(question, _model_query(planner, sql, tables).query, _graph(planner, tables))


def test_a_set_query_meets_the_questions_constraints_as_a_whole():
    """Spider DEV, 2026-10-10: each branch of a set query answered for the whole question, so an INTERSECT of "before
    1945" and "after 1955" missed a comparison in each branch, and an EXCEPT of the poker players missed the
    exclusion of "people who do not play poker": every set-query answer to such a question was refused."""
    tables = [PEOPLE_TABLE]
    both = "Show countries where a person above age 35 and a person below age 25 are from."
    assert _violations(tables, both, "SELECT Country FROM people WHERE Age > 35 "
                                     "INTERSECT SELECT Country FROM people WHERE Age < 25") == ()
    # Contrast: one branch alone, or the second comparison reversed, still misses it.
    for sql in ("SELECT Country FROM people WHERE Age > 35",
                "SELECT Country FROM people WHERE Age > 35 INTERSECT SELECT Country FROM people WHERE Age > 25"):
        assert _violations(tables, both, sql) == ("requested comparison direction, value or operand is missing",), sql
    # An EXCEPT excludes what its subtracted branch keeps.
    excluded = "List the names of people who are not from France."
    assert _violations(tables, excluded, "SELECT Name FROM people EXCEPT SELECT Name FROM people "
                                         "WHERE Country = 'France'") == ()
    # Negative: subtracting another value does not exclude France, and keeping France excludes nothing.
    assert _violations(tables, excluded, "SELECT Name FROM people EXCEPT SELECT Name FROM people "
                                         "WHERE Country = 'Spain'") == (
        "the query does not exclude 'France', which the question excludes",)
    assert "requested exclusion is missing" in _violations(
        tables, excluded, "SELECT Name FROM people WHERE Country = 'France'")


def test_a_comparison_counts_only_where_it_filters_the_rows_returned():
    """Review of 2026-10-10: the whole-query check found each comparison anywhere in the query, so "above 35 and below
    50" was met by a UNION (everybody) and by an EXCEPT (the person aged 60) of the two comparisons, and "people above
    35" by a scalar subquery comparing their ids, which filters no person by age. "Below 50" also compared nothing,
    six words after "age"."""
    from engine.sql_expansion import ExpansionSupport, tokens
    planner = _hermetic_planner()
    tables = [{"name": "people", "columns": ["ID", "Name", "Country", "Age"],
               "rows": [[1, "Ann", "France", 20], [2, "Bo", "Spain", 40], [3, "Cy", "France", 60]]}]
    graph = _graph(planner, tables)

    def complete(question, sql):
        return coverage(question, _model_query(planner, sql, tables), graph).complete

    both = "List names of people whose age is above 35 and below 50."
    assert [(c.operator, c.right.value) for c in ExpansionSupport(graph).numeric_comparisons(tokens(both))] == [
        (">", 35), ("<", 50)]
    # Contrast: a number a noun follows compares that noun, not the field before it (DEV 911).
    friends = "List names of people whose age is over 5 and who have 2 or more friends."
    assert [(c.operator, c.right.value) for c in ExpansionSupport(graph).numeric_comparisons(tokens(friends))] == [
        (">", 5)]
    for sql in ("SELECT Name FROM people WHERE Age > 35 AND Age < 50",
                "SELECT Name FROM people WHERE Age > 35 INTERSECT SELECT Name FROM people WHERE Age < 50"):
        assert complete(both, sql), sql
    for operator in ("UNION", "EXCEPT"):
        assert not complete(both, f"SELECT Name FROM people WHERE Age > 35 {operator} "
                                  "SELECT Name FROM people WHERE Age < 50"), operator
    assert not complete(both, "SELECT Name FROM people WHERE Age > 35")
    # Contrast: alternatives the question offers are a UNION's branches.
    assert complete("List names of people whose age is above 50 or below 25.",
                    "SELECT Name FROM people WHERE Age > 50 UNION SELECT Name FROM people WHERE Age < 25")
    # A comparison in a scalar subquery filters no returned row; one in an IN subquery filters them by membership.
    above = "List names of people whose age is above 35."
    assert not complete(above, "SELECT Name FROM people WHERE ID > (SELECT MIN(ID) FROM people WHERE Age > 35)")
    assert complete(above, "SELECT Name FROM people WHERE ID IN (SELECT ID FROM people WHERE Age > 35)")
    # A subtracted comparison is its complement: everyone EXCEPT those over 35 are those not older than 35.
    assert complete("List names of people who are not older than 35.",
                    "SELECT Name FROM people EXCEPT SELECT Name FROM people WHERE Age > 35")
    # Contrast: one country with a person above 35 and another below 25 is an INTERSECT of the countries, not one
    # person meeting both.
    assert _violations(tables, "Show countries where a person above age 35 and a person below age 25 are from.",
                       "SELECT Country FROM people WHERE Age > 35 "
                       "INTERSECT SELECT Country FROM people WHERE Age < 25") == ()


def test_an_exclusion_removes_the_rows_the_question_names():
    """Spider DEV 316, 2026-10-10: "templates that are not used in any documents" subtracted the templates of documents
    that have paragraphs, a table the question never names, so a template whose documents have none stayed. A table
    joined inside an exclusion only to filter, which the question does not name, narrows what it removes."""
    templates = [{"name": "Templates", "columns": ["Template_ID", "Template_Type_Code"], "rows": [[1, "PP"], [2, "BK"]]},
                 {"name": "Documents", "columns": ["Document_ID", "Template_ID"], "rows": [[10, 1]]},
                 {"name": "Paragraphs", "columns": ["Paragraph_ID", "Document_ID"], "rows": []}]
    asked = "Show ids for all templates not used by any document."
    used = " FROM Documents JOIN Templates ON Documents.Template_ID = Templates.Template_ID"
    assert _violations(templates, asked, "SELECT Template_ID FROM Templates EXCEPT SELECT Templates.Template_ID"
                                         + used) == ()
    assert _violations(templates, asked, "SELECT Template_ID FROM Templates EXCEPT SELECT Templates.Template_ID"
                                         + used + " JOIN Paragraphs ON Paragraphs.Document_ID = Documents.Document_ID") == (
        "the exclusion is narrowed by Paragraphs, which the question does not name",)
    # Contrast: a table the question names may narrow it ("documents with paragraphs").
    assert _violations(templates, "Show ids for all templates not used by any document with paragraphs.",
                       "SELECT Template_ID FROM Templates EXCEPT SELECT Templates.Template_ID" + used
                       + " JOIN Paragraphs ON Paragraphs.Document_ID = Documents.Document_ID") == ()


def test_alternatives_counted_thresholds_and_subqueries_realize_comparisons():
    """Spider DEV, 2026-10-10: "opened after 2013 or before 2008" was unrealized by an OR of the two comparisons,
    "more than 2 car makers" by a HAVING COUNT(*) > 2, and "did not visit any museum opened after 2010" by the
    comparison inside its NOT IN; each read only the conjunction at the top of the query."""
    tables = [PEOPLE_TABLE]
    alternatives = "SELECT COUNT(*) FROM people WHERE Age > 35 OR Age < 25"
    assert _violations(tables, "How many people are above age 35 or below age 25?", alternatives) == ()
    # Contrast: a conjunction the question asks for is not realized by an OR.
    assert _violations(tables, "How many people are above age 35 and below age 25?", alternatives) == (
        "requested comparison direction, value or operand is missing",)
    cars = [{"name": "countries", "columns": ["CountryId", "CountryName"], "rows": [[1, "usa"], [2, "japan"]]},
            {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
             "rows": [[1, "ford", "Ford", 1], [2, "gm", "GM", 1], [3, "toyota", "Toyota", 2]]}]
    asked = "Which countries have more than 1 car maker?"
    assert _violations(cars, asked, "SELECT Country FROM car_makers GROUP BY Country HAVING COUNT(*) > 1") == ()
    # Negative: another threshold is not the one asked.
    assert _violations(cars, asked, "SELECT Country FROM car_makers GROUP BY Country HAVING COUNT(*) > 2") == (
        "requested comparison direction, value or operand is missing",)
    visits = [{"name": "museum", "columns": ["Museum_ID", "Name", "Open_Year"],
               "rows": [[1, "Louvre", 2009], [2, "Tate", 2012]]},
              {"name": "visitor", "columns": ["ID", "Name"], "rows": [[1, "Ann"], [2, "Bo"]]},
              {"name": "visit", "columns": ["Museum_ID", "visitor_ID"], "rows": [[2, 1]]}]
    asked = "How many visitors did not visit any museum opened after 2010?"
    assert _violations(visits, asked, "SELECT COUNT(*) FROM visitor WHERE ID NOT IN (SELECT visit.visitor_ID FROM "
                       "visit JOIN museum ON visit.Museum_ID = museum.Museum_ID WHERE museum.Open_Year > 2010)") == ()


def test_a_count_of_qualifying_groups_is_one_number_and_a_stated_total_is_compared_as_one():
    """The review of 2026-10-11 (F2): "how many countries have more than 1 car maker" was met by a COUNT(*) grouped by
    country, the car makers of each; "more than 1 car maker" by a car maker's Country key above 1; and a total stated
    over a number ("customers with a total amount over 10") by each order's amount over 10."""
    cars = [{"name": "countries", "columns": ["CountryId", "CountryName"], "rows": [[1, "usa"], [2, "japan"]]},
            {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
             "rows": [[1, "ford", "Ford", 1], [2, "gm", "GM", 1], [3, "toyota", "Toyota", 2]]}]
    asked = "How many countries have more than 1 car maker?"
    per_group = "SELECT COUNT(*) FROM car_makers GROUP BY Country HAVING COUNT(*) > 1"
    from engine.sql_ast import Aggregate, ColumnRef, Comparison, Literal, SelectItem, SelectQuery, SQLType, Star
    from engine.sql_ast import SubquerySource
    # It also counts the car makers referring to the countries counted (sql_expansion.counted_references).
    assert _violations(cars, asked, per_group) == ("the count counts the car_makers rows referring to what the question "
                                                   "counts", "the question counts the groups that qualify, not the rows "
                                                   "in each")
    country = ColumnRef("car_makers", "Country", SQLType.INTEGER)
    count = Aggregate("COUNT", Star())
    qualifying = SelectQuery((SelectItem(country),), "car_makers", group_by=(country,),
                             having=Comparison(count, ">", Literal(1, SQLType.INTEGER)))
    counted = SelectQuery((SelectItem(count),), SubquerySource(qualifying, "qualifying"))
    assert constraint_violations(asked, counted, _graph(_hermetic_planner(), cars)) == ()
    # Contrast: a question that asks for groups counts in each.
    assert _violations(cars, "How many car makers does each country have, among countries with more than 1 car maker?",
                       "SELECT Country, COUNT(*) FROM car_makers GROUP BY Country HAVING COUNT(*) > 1") == ()
    assert _violations(cars, asked, "SELECT COUNT(DISTINCT Country) FROM car_makers WHERE Country > 1") == (
        "requested comparison direction, value or operand is missing",)
    # The review of 2026-10-11 (R1): a count that admits none is met by the countries NOT IN those failing it, or by
    # the car makers grouped alone, not by an inner join that leaves out the countries without one; and "exactly 0
    # car makers" not by a Country key equal to 0.
    joined = " FROM car_makers JOIN countries ON car_makers.Country = countries.CountryId"
    fewer = "Which countries have fewer than 2 car makers?"
    missing = ("requested comparison direction, value or operand is missing",)
    assert _violations(cars, fewer, "SELECT countries.CountryName" + joined
                       + " GROUP BY countries.CountryName, countries.CountryId HAVING COUNT(*) < 2") == missing
    assert _violations(cars, fewer, "SELECT CountryName FROM countries WHERE CountryId NOT IN (SELECT "
                       "countries.CountryId" + joined + " GROUP BY countries.CountryId HAVING COUNT(*) >= 2)") == ()
    # Contrast: a NOT IN of the groups with at most 1 is not "more than 1": it keeps the countries without one.
    assert _violations(cars, "Which countries have more than 1 car maker?", "SELECT CountryName FROM countries WHERE "
                       "CountryId NOT IN (SELECT countries.CountryId" + joined + " GROUP BY countries.CountryId "
                       "HAVING COUNT(*) <= 1)") == ("requested comparison direction, value or operand is missing",)
    assert _violations(cars, "How many countries have exactly 0 car makers?",
                       "SELECT COUNT(DISTINCT Country) FROM car_makers WHERE Country = 0") == (
        "requested comparison direction, value or operand is missing",)
    # A count over a join to a table the question does not name counts that table's rows too: the appointments
    # joined to their nurses (held-out Spider train 47, newly answered once counts were checked).
    clinic = [{"name": "Patient", "columns": ["SSN", "Name"], "rows": [[1, "Ann"], [2, "Bo"]]},
              {"name": "Nurse", "columns": ["EmployeeID", "Name"], "rows": [[7, "Cy"], [8, "Di"]]},
              {"name": "Appointment", "columns": ["AppointmentID", "Patient", "PrepNurse"],
               "rows": [[1, 1, 7], [2, 1, 8], [3, 2, 7]]}]
    asked = "Which patients made more than one appointment?"
    via = " FROM Appointment JOIN Patient ON Appointment.Patient = Patient.SSN"
    assert _violations(clinic, asked, "SELECT Patient.Name" + via + " JOIN Nurse ON Appointment.PrepNurse = "
                       "Nurse.EmployeeID GROUP BY Patient.Name, Patient.SSN HAVING COUNT(*) > 1") == missing
    assert _violations(clinic, asked, "SELECT Patient.Name" + via
                       + " GROUP BY Patient.Name, Patient.SSN HAVING COUNT(*) > 1") == ()
    # Groups that include the counted rows' own key are about one row each (held-out Spider train 296), and the rows
    # counted may be a table whose name extends the noun: jobs held are job_history rows (held-out train 220).
    world = [{"name": "country", "columns": ["country_id", "country"], "rows": [[1, "France"], [2, "Peru"]]},
             {"name": "city", "columns": ["city_id", "city", "country_id"],
              "rows": [[1, "Paris", 1], [2, "Lyon", 1], [3, "Nice", 1], [4, "Lima", 2]]}]
    asked = "Which countries have at least 3 cities?"
    pair = " FROM city JOIN country ON city.country_id = country.country_id"
    assert _violations(world, asked, "SELECT country.country" + pair + " GROUP BY country.country, city.city_id "
                                     "HAVING COUNT(*) >= 3") == missing
    assert _violations(world, asked, "SELECT country.country" + pair + " GROUP BY country.country HAVING COUNT(*) >= 3") \
        == ()
    staffing = [{"name": "employees", "columns": ["EMPLOYEE_ID", "NAME"], "rows": [[1, "Ann"], [2, "Bo"]]},
                {"name": "jobs", "columns": ["JOB_ID", "TITLE"], "rows": [["A", "Clerk"], ["B", "Lead"]]},
                {"name": "job_history", "columns": ["EMPLOYEE_ID", "JOB_ID"], "rows": [[1, "A"], [1, "B"], [2, "A"]]}]
    asked = "What are the employee ids for those who had two or more jobs?"
    assert _violations(staffing, asked, "SELECT EMPLOYEE_ID FROM job_history GROUP BY EMPLOYEE_ID "
                                        "HAVING COUNT(*) >= 2") == ()
    orders = [{"name": "orders", "columns": ["Customer", "Amount"],
               "rows": [["Ann", 5], ["Ann", 6], ["Ann", 7], ["Bo", 8], ["Bo", 9], ["Cy", 1]]}]
    asked = "Which customers have a total amount over 10?"
    assert _violations(orders, asked, "SELECT Customer, SUM(Amount) FROM orders GROUP BY Customer "
                                      "HAVING SUM(Amount) > 10") == ()
    assert _violations(orders, asked, "SELECT Customer, SUM(Amount) FROM orders WHERE Amount > 10 "
                                      "GROUP BY Customer") == (
        "requested comparison direction, value or operand is missing",)
    # Contrast: the total amount of the orders over 7 compares each order.
    assert _violations(orders, "What is the total amount of orders over 7?",
                       "SELECT SUM(Amount) FROM orders WHERE Amount > 7") == ()
    # Spider DEV 125: a number said after a field whose cells are not all numbers compares that field, and the
    # cylinders over 150 answer another question once the per-group count was refused.
    cars = [{"name": "cars_data", "columns": ["Id", "Cylinders", "Horsepower"],
             "rows": [[1, 8, "160"], [2, 4, "null"], [3, 6, "95"]]}]
    asked = "What is the number of the cars with horsepower more than 150?"
    assert _violations(cars, asked, "SELECT COUNT(Horsepower) FROM cars_data WHERE Cylinders > 150") == (
        "requested comparison direction, value or operand is missing",)
    assert _violations(cars, asked, "SELECT COUNT(*) FROM cars_data WHERE Horsepower > 150") == ()


def test_a_condition_inside_a_membership_test_filters_only_the_rows_it_binds():
    """The review of 2026-10-11 (F3): "people whose age is above 35" was met by an uncorrelated EXISTS (every person
    while someone is over 35) and by person IDs IN a list of ages. A comparison inside IN filters the returned rows
    only when the IN compares values of one kind, and inside EXISTS only when it refers to the outer row."""
    from engine.sql_ast import (
        ColumnRef, Comparison, ExistsPredicate, InPredicate, Literal, SelectItem, SelectQuery, SQLType, and_predicates,
    )
    people = [{"name": "people", "columns": ["ID", "Name", "Age"], "rows": [[1, "Ann", 20], [2, "Bo", 40], [3, "Cy", 60]]}]
    asked = "List names of people whose age is above 35."
    assert _violations(people, asked, "SELECT Name FROM people WHERE ID IN (SELECT ID FROM people WHERE Age > 35)") == ()
    missing = ("requested comparison direction, value or operand is missing",)
    assert _violations(people, asked, "SELECT Name FROM people WHERE ID IN (SELECT Age FROM people WHERE Age > 35)") \
        == missing
    assert _violations(people, asked, "SELECT Name FROM people WHERE EXISTS (SELECT ID FROM people WHERE Age > 35)") \
        == missing
    # A comparison in a scalar subquery filters no row either.
    assert _violations(people, asked, "SELECT Name FROM people WHERE Age >= (SELECT MIN(Age) FROM people "
                                      "WHERE Age > 35)") == missing
    # The review of 2026-10-11 (R2): a shared word is no binding (a manager's ID is not the person's), nor a computed
    # value, and a set of queries binds only when each branch does.
    staff = [{"name": "people", "columns": ["Person_ID", "Name", "Age", "Manager_ID"],
              "rows": [[1, "Ann", 20, 3], [2, "Bo", 40, 1], [3, "Cy", 60, 1]]}]
    assert _violations(staff, asked, "SELECT Name FROM people WHERE Person_ID IN (SELECT Manager_ID FROM people "
                                     "WHERE Age > 35)") == missing
    assert _violations(staff, asked, "SELECT Name FROM people WHERE Person_ID IN (SELECT Age - 39 FROM people "
                                     "WHERE Age > 35)") == missing
    assert _violations(staff, asked, "SELECT Name FROM people WHERE Person_ID IN (SELECT Age FROM people WHERE "
                                     "Age > 35 INTERSECT SELECT Age FROM people WHERE Age > 30)") == missing
    assert _violations(staff, asked, "SELECT Name FROM people WHERE Person_ID IN (SELECT Person_ID FROM people "
                                     "WHERE Age > 35 INTERSECT SELECT Person_ID FROM people WHERE Age > 30)") == ()
    # The review of 6263502 (S4): the largest id of the people above 35 keeps one of them; it binds the rows it keeps
    # when the question asks for an extremum, or each group is one person.
    largest = "SELECT Name FROM people WHERE Person_ID IN (SELECT MAX(Person_ID) FROM people WHERE Age > 35{})"
    assert _violations(staff, asked, largest.format("")) == missing
    assert _violations(staff, asked, largest.format(" GROUP BY Person_ID")) == ()
    assert "requested comparison direction, value or operand is missing" not in _violations(
        staff, "List the name of the person with the highest Person_ID among people whose age is above 35.",
        largest.format(""))
    # An EXISTS that refers to the outer row binds it; one that does not keeps every row or none.
    tables = [{"name": "owners", "columns": ["ID", "Name"], "rows": [[1, "Ann"], [2, "Bo"]]},
              {"name": "pets", "columns": ["PetID", "OwnerID", "PetAge"], "rows": [[10, 1, 2], [11, 2, 5]]}]
    graph = SchemaGraph.from_tables(tables, [{"from_table": "pets", "from_col": "OwnerID",
                                              "to_table": "owners", "to_col": "ID"}])
    integer = SQLType.INTEGER
    owner, pet_owner = ColumnRef("owners", "ID", integer), ColumnRef("pets", "OwnerID", integer)
    older = Comparison(ColumnRef("pets", "PetAge", integer), ">", Literal(3, integer))
    names = (SelectItem(ColumnRef("owners", "Name", SQLType.TEXT)),)
    asked = "List the names of owners who have a pet with PetAge above 3."
    correlated = SelectQuery(names, "owners", where=ExistsPredicate(SelectQuery(
        (SelectItem(ColumnRef("pets", "PetID", integer)),), "pets",
        where=and_predicates((Comparison(pet_owner, "=", owner), older)))))
    assert constraint_violations(asked, correlated, graph) == ()
    uncorrelated = SelectQuery(names, "owners", where=ExistsPredicate(SelectQuery(
        (SelectItem(ColumnRef("pets", "PetID", integer)),), "pets", where=older)))
    assert constraint_violations(asked, uncorrelated, graph) == missing
    # A foreign key pairs the outer key with the inner one: the owners IN the pets' owners over 3 are bound.
    member = SelectQuery(names, "owners", where=InPredicate(owner, SelectQuery((SelectItem(pet_owner),), "pets",
                                                                             where=older)))
    assert constraint_violations(asked, member, graph) == ()
    unrelated = SelectQuery(names, "owners", where=InPredicate(owner, SelectQuery(
        (SelectItem(ColumnRef("pets", "PetAge", integer)),), "pets", where=older)))
    assert constraint_violations(asked, unrelated, graph) == missing


def test_a_ranking_keeps_its_said_number_of_rows_and_shows_them():
    """The review of 2026-10-11 (F5): the contract read a cutoff only after "top" or "bottom"; "the 2 people with the
    largest Age" kept one row, and "top 2 products by price" listed two prices."""
    people = [{"name": "people", "columns": ["Person_ID", "Name", "Age"],
               "rows": [[1, "Ann", 30], [2, "Bo", 20], [3, "Cy", 40]]}]
    asked = "List the names of the 2 people with the largest Age."
    assert _violations(people, asked, "SELECT Name FROM people ORDER BY Age DESC LIMIT 2") == ()
    assert "requested ranking cutoff is missing" in _violations(
        people, asked, "SELECT Name FROM people ORDER BY Age DESC LIMIT 1")
    products = [{"name": "products", "columns": ["product_id", "product_name", "price"],
                 "rows": [[1, "Alpha", 10], [2, "Beta", 30], [3, "Gamma", 20]]}]
    asked = "top 2 products by price"
    assert _violations(products, asked, "SELECT price FROM products ORDER BY price DESC LIMIT 2") == (
        "the ranked products are not shown, only the field that ranks them",)
    assert _violations(products, asked, "SELECT product_name, price FROM products ORDER BY price DESC LIMIT 2") == ()
    # Contrast: the field asked for before the cutoff is the answer.
    assert _violations(products, "What are the prices of the top 2 products?",
                       "SELECT price FROM products ORDER BY price DESC LIMIT 2") == ()
    # A field named after its table's name is that table's: "the highest savings balances" are not the checking ones
    # (held-out Spider train, 2026-10-11, newly answered once the cutoff was built).
    bank = [{"name": "ACCOUNTS", "columns": ["custid", "name"], "rows": [[1, "Ann"], [2, "Bo"]]},
            {"name": "SAVINGS", "columns": ["custid", "balance"], "rows": [[1, 50], [2, 90]]},
            {"name": "CHECKING", "columns": ["custid", "balance"], "rows": [[1, 70], [2, 10]]}]
    asked = "What are the names and savings balances of the two accounts with the highest savings balances?"
    joined = (" FROM ACCOUNTS JOIN SAVINGS ON SAVINGS.custid = ACCOUNTS.custid"
              " JOIN CHECKING ON CHECKING.custid = ACCOUNTS.custid")
    assert _violations(bank, asked, "SELECT ACCOUNTS.name, SAVINGS.balance" + joined
                       + " ORDER BY CHECKING.balance DESC LIMIT 2") == ("the balance asked is SAVINGS's, not CHECKING's",)
    assert _violations(bank, asked, "SELECT ACCOUNTS.name, SAVINGS.balance FROM ACCOUNTS JOIN SAVINGS ON "
                       "SAVINGS.custid = ACCOUNTS.custid ORDER BY SAVINGS.balance DESC LIMIT 2") == ()


def test_each_count_the_question_states_is_met_by_a_count_of_its_own_rows():
    """The review of 6263502: a HAVING was read only as the comparisons of its conjunction, each COUNT taken for the rows
    asked. "More than 1 car maker" was met by a count of distinct countries (S2); "more than 1 car maker and more than
    1 airport" was one threshold, met by the car makers' count alone (S3); the subtraction of "between 0 and 2"
    (fewer than 0 or more than 2) was refused while the car makers grouped alone, which list only the countries that
    have one, were taken for every country (S1); and each count up to the number said was tried (S5)."""
    import engine.sql_expansion as expansion
    cars = [{"name": "countries", "columns": ["CountryId", "CountryName"],
             "rows": [[1, "usa"], [2, "japan"], [3, "chile"], [4, "peru"]]},
            {"name": "car_makers", "columns": ["Id", "Maker", "Country"],
             "rows": [[1, "ford", 1], [2, "gm", 1], [3, "tesla", 1], [4, "toyota", 2], [5, "cmc", 3], [6, "zz", 3]]},
            {"name": "airports", "columns": ["AirportId", "Code", "Country"],
             "rows": [[1, "JFK", 1], [2, "HND", 2], [3, "NRT", 2], [4, "SCL", 3], [5, "PMC", 3]]}]
    missing = ("requested comparison direction, value or operand is missing",)
    joined = " FROM car_makers JOIN countries ON car_makers.Country = countries.CountryId"
    grouped = " GROUP BY countries.CountryId, countries.CountryName HAVING "
    more = "Which countries have more than 1 car maker?"
    assert _violations(cars[:2], more, "SELECT countries.CountryName" + joined + grouped
                       + "COUNT(DISTINCT countries.CountryId) > 1") == missing
    assert _violations(cars[:2], more, "SELECT countries.CountryName" + joined + grouped + "COUNT(*) > 1") == ()
    assert _violations(cars[:2], more, "SELECT countries.CountryName" + joined + grouped
                       + "COUNT(DISTINCT car_makers.Id) > 1") == ()
    both = "Which countries have more than 1 car maker and more than 1 airport?"
    airports = joined + " JOIN airports ON airports.Country = countries.CountryId"
    assert _violations(cars, both, "SELECT countries.CountryName" + airports + grouped
                       + "COUNT(DISTINCT car_makers.Id) > 1") == missing
    assert _violations(cars, both, "SELECT countries.CountryName" + airports + grouped + "COUNT(*) > 1") == missing
    assert _violations(cars, both, "SELECT countries.CountryName" + airports + grouped
                       + "COUNT(DISTINCT car_makers.Id) > 1 AND COUNT(DISTINCT airports.AirportId) > 1") == ()
    ranged = "Which countries have between 0 and 2 car makers?"
    assert _violations(cars[:2], ranged, "SELECT Country FROM car_makers GROUP BY Country HAVING COUNT(*) >= 0 "
                       "AND COUNT(*) <= 2") == missing
    assert _violations(cars[:2], ranged, "SELECT CountryName FROM countries WHERE CountryId NOT IN (SELECT "
                       "car_makers.Country FROM car_makers GROUP BY car_makers.Country HAVING COUNT(*) < 0 OR "
                       "COUNT(*) > 2)") == ()
    # Negative: an OR of the count with anything else says nothing of the count, and one sheet's groups are all its
    # entities.
    assert _violations(cars[:2], ranged, "SELECT CountryName FROM countries WHERE CountryId NOT IN (SELECT "
                       "car_makers.Country FROM car_makers GROUP BY car_makers.Country HAVING COUNT(*) > 2 OR "
                       "MAX(car_makers.Id) > 4)") == missing
    orders = [{"name": "orders", "columns": ["Order ID", "Customer", "Amount"],
               "rows": [[10, "Ann", 5], [11, "Ann", 6], [12, "Bo", 7]]}]
    assert _violations(orders, "Which customers have fewer than 2 orders?",
                       "SELECT Customer FROM orders GROUP BY Customer HAVING COUNT(*) < 2") == ()
    # A table whose name extends the counted noun stands for it only where the query does not read it: beside the
    # templates, the template types are what they are grouped by. The subtraction was refused as grouping templates by
    # their own key once the templates grouped alone no longer stood for every type (Spider DEV 333 and 334).
    templates = [{"name": "Ref_Template_Types", "columns": ["Template_Type_Code", "Template_Type_Description"],
                  "rows": [["PPT", "Presentation"], ["CV", "Resume"], ["AD", "Advertisement"]]},
                 {"name": "Templates", "columns": ["Template_ID", "Template_Type_Code"],
                  "rows": [[1, "PPT"], [2, "PPT"], [3, "PPT"], [4, "CV"], [5, "CV"]]}]
    asked = "Show all template type codes with less than three templates."
    assert _violations(templates, asked, "SELECT Template_Type_Code FROM Ref_Template_Types WHERE Template_Type_Code "
                       "NOT IN (SELECT Ref_Template_Types.Template_Type_Code FROM Templates JOIN Ref_Template_Types ON "
                       "Templates.Template_Type_Code = Ref_Template_Types.Template_Type_Code GROUP BY "
                       "Ref_Template_Types.Template_Type_Code HAVING COUNT(*) >= 3)") == ()
    served = _hermetic_planner().serve(templates, asked, [
        {"from_table": "Templates", "from_col": "Template_Type_Code", "to_table": "Ref_Template_Types",
         "to_col": "Template_Type_Code"}])
    assert served.get("valid") and sorted(list(row) for row in served["result"]["rows"]) == [["AD"], ["CV"]], served
    # The work is the bounds', not the number's: a count is tried at the numbers where a comparison can change.
    calls = []
    admits = expansion.count_admits
    expansion.count_admits = lambda *args: calls.append(args) or admits(*args)
    try:
        assert _violations(cars[:2], "Which countries have more than 1000000000000 car makers?",
                           "SELECT countries.CountryName" + joined + grouped + "COUNT(*) > 1000000000000") == ()
    finally:
        expansion.count_admits = admits
    assert 0 < len(calls) < 100, len(calls)


def test_count_requirements_preserve_their_boolean_connection():
    """Separate count witnesses cannot turn the requested union into an intersection, or vice versa."""
    tables = [{"name": "countries", "columns": ["CountryId", "CountryName"],
               "rows": [[1, "usa"], [2, "japan"], [3, "chile"], [4, "peru"]]},
              {"name": "car_makers", "columns": ["Id", "Maker", "Country"],
               "rows": [[1, "ford", 1], [2, "gm", 1], [3, "tesla", 1], [4, "toyota", 2],
                        [5, "cmc", 3], [6, "zz", 3]]},
              {"name": "airports", "columns": ["AirportId", "Code", "Country"],
               "rows": [[1, "JFK", 1], [2, "HND", 2], [3, "NRT", 2], [4, "SCL", 3], [5, "PMC", 3]]}]
    left = "CountryId IN (SELECT Country FROM car_makers GROUP BY Country HAVING COUNT(*) > 1)"
    right = "CountryId IN (SELECT Country FROM airports GROUP BY Country HAVING COUNT(*) > 1)"
    for connective in ("AND", "OR"):
        question = f"Which countries have more than 1 car maker {connective.lower()} more than 1 airport?"
        sql = "SELECT CountryName FROM countries WHERE "
        assert _reading(tables, question, sql + left + f" {connective} " + right)[0]
        opposite = "OR" if connective == "AND" else "AND"
        assert not _reading(tables, question, sql + left + f" {opposite} " + right)[0]


def test_a_count_grouped_by_the_key_of_its_one_table_counts_the_rows_that_refer_to_it():
    """Held-out Spider train 370 (2026-10-11): "the transaction type code that occurs the most frequently" was met by the
    transaction types grouped by their own code, one row in each, so every type tied at 1; the transactions referring
    to the types hold the occurrences."""
    types = {"name": "Ref_Transaction_Types", "columns": ["transaction_type_code", "transaction_type_description"],
             "rows": [["SALE", "Sale"], ["PUR", "Purchase"]]}
    trades = {"name": "Transactions", "columns": ["transaction_id", "transaction_type_code", "amount"],
              "rows": [[1, "SALE", 10], [2, "PUR", 20], [3, "PUR", 30]]}
    asked = "Show the transaction type code that occurs the most frequently."
    ranked = " GROUP BY transaction_type_code ORDER BY COUNT(*) DESC LIMIT 1"
    assert _violations([types, trades], asked, "SELECT transaction_type_code FROM Ref_Transaction_Types" + ranked) == (
        "each group counts one Ref_Transaction_Types row, not the rows its key joins",)
    # Contrast: the transactions grouped by their code, alone or joined to the types, count the occurrences.
    assert _violations([types, trades], asked, "SELECT transaction_type_code FROM Transactions" + ranked) == ()
    assert _violations([types, trades], asked, "SELECT Ref_Transaction_Types.transaction_type_code FROM Transactions "
                       "JOIN Ref_Transaction_Types ON Transactions.transaction_type_code = "
                       "Ref_Transaction_Types.transaction_type_code GROUP BY Ref_Transaction_Types.transaction_type_code "
                       "ORDER BY COUNT(*) DESC LIMIT 1") == ()
    # Negative: a sheet no other sheet refers to counts its own rows, each value once.
    assert _violations([types], asked, "SELECT transaction_type_code FROM Ref_Transaction_Types" + ranked) == ()
    served = _hermetic_planner().serve([types, trades], asked, [
        {"from_table": "Transactions", "from_col": "transaction_type_code", "to_table": "Ref_Transaction_Types",
         "to_col": "transaction_type_code"}])
    assert served.get("valid") and [list(row) for row in served["result"]["rows"]] == [["PUR"]], served


def test_a_negated_comparative_is_a_bound_and_the_nearest_cue_sets_a_comparison():
    """Spider DEV 153 and 1028, 2026-10-10: "weighing no less than 3000 and no more than 4000" read "no" as an
    exclusion and "no less than 3000" as less, and in "before 1945 and after 1955" 1955 was before: the first cue
    in a fixed order set every number's comparison."""
    from engine.closed_class import EXCLUSION_CUES
    from engine.sql_expansion import nearby_operator, tokens
    bounded = tokens("cars weighing no less than 3000 and no more than 4000")
    assert [nearby_operator(bounded, bounded.index(n)) for n in ("3000", "4000")] == [">=", "<="]
    assert not EXCLUSION_CUES.search("cars weighing no less than 3000 and no more than 4000")
    ranged = tokens("singers with birth year before 1945 and after 1955")
    assert [nearby_operator(ranged, ranged.index(n)) for n in ("1945", "1955")] == ["<", ">"]
    # Negative: a negation that is no bound is still an exclusion.
    for asked in ("people who do not play poker", "semesters with no students enrolled", "has no more pets"):
        assert EXCLUSION_CUES.search(asked), asked
    # DEV 413: any comparative is negated, and "not higher than 4" is at most 4, read only by a query keeping that.
    level = tokens("visitors whose membership level is not higher than 4")
    assert nearby_operator(level, level.index("4")) == "<="
    asked = "What is the average age of people whose age is not higher than 30?"
    assert _reading([PEOPLE_TABLE], asked, "SELECT AVG(Age) FROM people WHERE Age <= 30") == (True, ())
    assert _reading([PEOPLE_TABLE], asked, "SELECT AVG(Age) FROM people WHERE Age > 30") == (False, ("higher",))
    # Review of 2026-10-10: the exclusion cue kept its own list of comparatives, so "not older than 35" asked for an
    # exclusion while "not higher than 35" did not. Every comparative the parser reads is a bound.
    for word, operator in (("higher", "<="), ("greater", "<="), ("older", "<="), ("younger", ">="), ("taller", "<="),
                           ("cheaper", ">="), ("more", "<="), ("fewer", ">=")):
        assert not EXCLUSION_CUES.search(f"people not {word} than 35"), word
        said = tokens(f"people whose age is not {word} than 35")
        assert nearby_operator(said, said.index("35")) == operator, word
    for word, operator in (("older", "<="), ("younger", ">=")):
        asked = f"What is the average age of people whose age is not {word} than 30?"
        assert _reading([PEOPLE_TABLE], asked, f"SELECT AVG(Age) FROM people WHERE Age {operator} 30") == (True, ())


def test_a_field_named_inside_a_longer_name_is_the_longer_one():
    """Spider DEV 6, 12 and 108, 2026-10-10: "the name and the release year of the song" asked for a Name and a Year
    over Song_Name and Song_release_year; "the most car makers" asked for a Maker field of the car_makers table it
    counts; and a stadium's "Average" column made "singers above the average age" a request to total it."""
    singer = [{"name": "singer", "columns": ["Singer_ID", "Name", "Song_Name", "Song_release_year", "Age"],
               "rows": [[1, "Joe", "Hey", 2001, 30], [2, "Ann", "Yo", 2010, 20]]},
              {"name": "stadium", "columns": ["Stadium_ID", "Name", "Average", "Capacity"],
               "rows": [[1, "Park", 1000, 5000]]}]
    songs = "SELECT Song_Name, Song_release_year FROM singer"
    assert _violations(singer, "Show the name and the release year of the song by each singer.", songs) == ()
    # Contrast: without "song", the name asked is the singer's.
    assert _violations(singer, "Show the name of each singer and the release year.", songs) == (
        "The requested field is not used: Name",)
    above = "List all song names by singers above the average age."
    assert _violations(singer, above, "SELECT Song_Name FROM singer WHERE Age > "
                                      "(SELECT AVG(Age) FROM singer)") == ()
    # Negative: an average still has to be taken.
    assert _violations(singer, above, "SELECT Song_Name FROM singer WHERE Age > 25")
    cars = [{"name": "countries", "columns": ["CountryId", "CountryName"], "rows": [[1, "usa"], [2, "japan"]]},
            {"name": "car_makers", "columns": ["Id", "Maker", "FullName", "Country"],
             "rows": [[1, "ford", "Ford", 1], [2, "gm", "GM", 1], [3, "toyota", "Toyota", 2]]}]
    most = ("SELECT countries.CountryName FROM car_makers JOIN countries ON car_makers.Country = countries.CountryId "
            "GROUP BY countries.CountryName ORDER BY COUNT(*) DESC LIMIT 1")
    assert _violations(cars, "What is the name of the country with the most car makers?", most) == ()
    # Contrast: the name of the stadiums is the stadiums' Name, though "concerts" is said later (DEV 41).
    shows = [{"name": "stadium", "columns": ["Stadium_ID", "Name", "Location"], "rows": [[1, "Park", "Leeds"]]},
             {"name": "concert", "columns": ["concert_ID", "concert_Name", "Stadium_ID", "Year"],
              "rows": [[1, "Gala", 1, 2014]]}]
    asked = "Find the name and location of the stadiums which some concerts happened in 2014."
    joined = " FROM concert JOIN stadium ON concert.Stadium_ID = stadium.Stadium_ID WHERE concert.Year = 2014"
    assert _violations(shows, asked, "SELECT stadium.Name, stadium.Location" + joined) == ()
    assert _violations(shows, asked, "SELECT concert.concert_Name, stadium.Location" + joined) == (
        "The requested field is not used: Name",)
    # Contrast: "makers" alone is not the table's name, and names the Maker field.
    assert _violations(cars, "What is the name of the country with the most makers?", most) == (
        "The requested field is not used: Maker",)


def test_a_graded_word_is_read_in_its_direction_on_a_field_of_its_kind():
    """Spider DEV, 2026-10-10: "youngest", "older" and "most common" were unread over queries realizing them. A rule
    reading graded words without their direction served 27 wrong answers for 19 right (2026-10-08), so a grade is read
    only in its direction, on a field it can grade."""
    tables = [PEOPLE_TABLE]
    youngest = "What is the name of the youngest person?"
    assert _reading(tables, youngest, "SELECT Name FROM people ORDER BY Age LIMIT 1") == (True, ())
    # Contrast: the other direction, or another field, leaves it unread.
    for sql in ("SELECT Name FROM people ORDER BY Age DESC LIMIT 1", "SELECT Name FROM people ORDER BY Name LIMIT 1"):
        assert _reading(tables, youngest, sql) == (False, ("youngest",)), sql
    assert _reading(tables, "What is the age of the youngest person?", "SELECT MIN(Age) FROM people") == (True, ())
    assert _reading(tables, "What is the age of the youngest person?", "SELECT MAX(Age) FROM people")[0] is False
    ranged = "List the names ordered from the oldest to the youngest."
    assert _reading(tables, ranged, "SELECT Name FROM people ORDER BY Age DESC") == (True, ())
    assert _reading(tables, ranged, "SELECT Name FROM people ORDER BY Age") == (False, ("oldest", "youngest"))
    older = "List the names of people older than 25."
    assert _reading(tables, older, "SELECT Name FROM people WHERE Age > 25") == (True, ())
    for sql in ("SELECT Name FROM people WHERE Age < 25", "SELECT Name FROM people WHERE Person_ID > 25"):
        assert _reading(tables, older, sql) == (False, ("older",)), sql
    common = "What is the most common country?"
    assert _reading(tables, common, "SELECT Country FROM people GROUP BY Country ORDER BY COUNT(*) DESC LIMIT 1") == (
        True, ())
    assert _reading(tables, common, "SELECT Country FROM people GROUP BY Country ORDER BY COUNT(*) LIMIT 1") == (
        False, ("common",))
    # Served: the search orders an age from the oldest (sql_extrema.superlative_direction), where "oldest" was
    # ascending on every field.
    served = _hermetic_planner().serve(tables, "List the names ordered by age from the oldest to the youngest.")
    assert [row[0] for row in served["result"]["rows"]] == ["Cara", "Alice", "Bob"], served.get("sql")


def test_an_adjective_after_most_is_read_in_its_direction():
    """Held-out Spider train 110, 355, 956, 2026-10-17: "the most expensive phone" ordered by Price was refused for
    "expensive", and the reading added for it read "the youngest person" over a descending age as well. The adjective
    after "most" or "least" is read by a field of its dimension ordered in that direction; a superlative of its own
    keeps its own reading."""
    products = [{"name": "product", "columns": ["product_id", "product_name", "price"],
                 "rows": [[1, "Desk", 300], [2, "Lamp", 40], [3, "Chair", 120]]}]
    cheapest = "List the names of the 2 least expensive products."
    assert _reading(products, cheapest, "SELECT product_name FROM product ORDER BY price LIMIT 2") == (True, ())
    assert _reading(products, cheapest, "SELECT product_name FROM product ORDER BY price DESC LIMIT 2") == (
        False, ("expensive",))
    dearest = "What is the name of the most expensive product?"
    assert _reading(products, dearest, "SELECT product_name FROM product ORDER BY price DESC LIMIT 1") == (True, ())
    assert _reading([PEOPLE_TABLE], "What is the name of the youngest person?",
                    "SELECT Name FROM people ORDER BY Age DESC LIMIT 1") == (False, ("youngest",))


def test_a_verb_relating_a_table_to_a_field_it_reads_is_read():
    """Spider DEV 603, 971, 973, 2026-10-17: "the professionals living in a city that contains the substring 'West'"
    was refused for "living", read only between two tables. A verb relating a table named before it to a field the query
    reads named just after it is read. Contrast: with no field after it, it stays unread."""
    professionals = [{"name": "Professionals", "columns": ["professional_id", "role_code", "street", "city", "state"],
                      "rows": [[1, "Employee", "6915 Oberbrunner", "West Heidi", "Indiana"],
                               [2, "Veterenarian", "88665 Terence", "North Odellfurt", "Connecticut"]]}]
    living = "Find the role, street, city and state of the professionals living in a city that contains the substring 'West'."
    assert _reading(professionals, living, "SELECT role_code, street, city, state FROM Professionals "
                    "WHERE city LIKE '%West%'") == (True, ())
    assert _reading(professionals, "Which professionals are living?", "SELECT role_code FROM Professionals")[1] == (
        "living",)


def test_of_origin_after_a_field_is_that_field():
    """Spider DEV 40, 640, 2026-10-17: "the pixel aspect ratio and country of origin for all TV channels" was refused
    for "origin". "Of origin" after a field the query reads says that field. Contrast: "origin" alone asks for one."""
    channels = [{"name": "TV_Channel", "columns": ["id", "Country", "Language", "Pixel_aspect_ratio_PAR"],
                 "rows": [["700", "Italy", "Italian", "16:9"], ["701", "UK", "English", "4:3"]]}]
    asked = "What is the pixel aspect ratio and country of origin for all TV channels that do not use English?"
    sql = "SELECT Pixel_aspect_ratio_PAR, Country FROM TV_Channel WHERE Language != 'English'"
    assert _reading(channels, asked, sql) == (True, ())
    assert "origin" in _reading(channels, "What is the origin of each TV channel?", "SELECT Country FROM TV_Channel")[1]


def test_first_and_last_grade_a_date():
    """Spider DEV 565, 691, 2026-10-17: "What is last date created of votes from the state 'CA'?" was refused for
    "last" over the latest created date. "First" and "last" before no name or number grade a date as "earliest" and
    "latest" do, in their direction. Contrast: the other direction leaves it unread, and "the last name" is a name."""
    votes = [{"name": "VOTES", "columns": ["vote_id", "phone_number", "state", "created"],
              "rows": [[1, 7182887233, "NY", "2018-03-09 19:03:21"], [2, 7148407040, "CA", "2018-03-09 19:03:36"],
                       [3, 6209389222, "CA", "2018-03-09 19:03:39"]]}]
    asked = "What is last date created of votes from the state 'CA'?"
    assert _reading(votes, asked, "SELECT created FROM VOTES WHERE state = 'CA' ORDER BY created DESC LIMIT 1") == (
        True, ())
    assert "last" in _reading(votes, asked, "SELECT created FROM VOTES WHERE state = 'CA' ORDER BY created LIMIT 1")[1]
    assert _reading(votes, "When was the first vote from the state 'CA' created?",
                    "SELECT MIN(created) FROM VOTES WHERE state = 'CA'") == (True, ())


def test_a_participle_saying_the_rows_are_there_and_single_are_read():
    """Spider DEV 173, 519, 520, 732, 750, 759, 2026-10-17: "How many different degrees are offered?", "How many unique
    languages are spoken in the world?" and "countries where there is not a single car maker" were refused for the
    participle, "world" and "single" over the queries answering them. A passive participle after a copula whose subject
    the query reads, with nothing after it but "in the world", says only that the rows are there; "single" is one, by
    an exclusion after "not a" or a count of one after "a". Contrast: a participle with an object stays unread."""
    degrees = [{"name": "Degree_Programs", "columns": ["degree_program_id", "degree_summary_name", "department_id"],
                "rows": [[1, "Master", 13], [2, "Bachelor", 2], [3, "Master", 13]]}]
    assert _reading(degrees, "How many different degrees are offered?",
                    "SELECT COUNT(DISTINCT degree_summary_name) FROM Degree_Programs") == (True, ())
    assert "offered" in _reading(degrees, "How many different degrees are offered by department 13?",
                                 "SELECT COUNT(DISTINCT degree_summary_name) FROM Degree_Programs")[1]
    languages = [{"name": "countrylanguage", "columns": ["CountryCode", "Language", "IsOfficial"],
                  "rows": [["ABW", "Dutch", "T"], ["ABW", "English", "F"], ["AFG", "Pashto", "T"]]}]
    assert _reading(languages, "How many unique languages are spoken in the world?",
                    "SELECT COUNT(DISTINCT Language) FROM countrylanguage") == (True, ())
    cars = [{"name": "countries", "columns": ["CountryId", "CountryName"], "rows": [[1, "usa"], [2, "japan"], [3, "chile"]]},
            {"name": "car_makers", "columns": ["Id", "Maker", "Country"], "rows": [[1, "ford", 1], [2, "toyota", 2]]}]
    assert _reading(cars, "What are the name of the countries where there is not a single car maker?",
                    "SELECT CountryName FROM countries EXCEPT SELECT countries.CountryName FROM car_makers JOIN countries "
                    "ON car_makers.Country = countries.CountryId") == (True, ())
    assert "single" in _reading(cars, "What are the name of the countries where there is not a single car maker?",
                                "SELECT CountryName FROM countries")[1]


def test_a_participle_naming_a_table_is_read_only_by_it():
    """Held-out Spider train 601, 2026-10-17: "How many regions are affected?" was served as COUNT(*) FROM region, the
    participle read as saying only that the regions are there, where it names affected_region, whose rows are the
    regions a storm affected. A participle a table or field of the data spells is that name. Contrast: the count of
    affected_region's regions reads it."""
    storms = [{"name": "region", "columns": ["Region_id", "Region_name"],
               "rows": [[1, "Afghanistan"], [2, "Albania"], [3, "Algeria"]]},
              {"name": "affected_region", "columns": ["Region_id", "Storm_ID", "Number_city_affected"],
               "rows": [[1, 1, 10], [2, 1, 15], [1, 2, 30]]}]
    assert "affected" in _reading(storms, "How many regions are affected?", "SELECT COUNT(*) FROM region")[1]
    assert _reading(storms, "How many regions are affected?",
                    "SELECT COUNT(DISTINCT Region_id) FROM affected_region") == (True, ())


def test_a_member_after_a_tables_name_is_its_row():
    """Held-out Spider train 197, 551, 627, 651, 785, 809, 918 and fresh holdout 15, 31, 76, 86, 314, 343, 544, 567,
    730, 807, 888, 2026-10-17: every question saying "staff member" or "faculty member" was refused for "member" over
    the query reading Staff or Faculty. "Member" after the name of a table the query reads names its rows, as "record"
    does. Contrast: said alone it names no table the query reads."""
    faculty = [{"name": "Faculty", "columns": ["FacID", "Lname", "Fname", "Rank", "Sex"],
                "rows": [[1082, "Giuliano", "Mark", "Instructor", "M"], [1121, "Goodrich", "Michael", "Professor", "M"]]}]
    assert _reading(faculty, "What are the last names of the faculty members?", "SELECT Lname FROM Faculty") == (True, ())
    assert "members" in _reading(faculty, "What are the last names of the members?", "SELECT Lname FROM Faculty")[1]


def test_a_list_of_fields_each_its_own_shows_each():
    """Held-out Spider train 920 and fresh holdout 709, 2026-10-17: "Give me the claim date, settlement date for all the
    claims whose claimed amount is larger than the average." showed the settlement date alone. A list whose items each
    name fields, more of them than the answer shows, asks for each. Contrast: values a list joins name no field, and a
    count of one field asks for one output."""
    claims = [{"name": "Claims", "columns": ["Claim_ID", "Date_Claim_Made", "Date_Claim_Settled", "Amount_Claimed"],
               "rows": [[1, "2017-01-01", "2017-02-01", 100], [2, "2017-03-01", "2017-04-01", 300]]}]
    question = ("Give me the claim date, settlement date for all the claims whose claimed amount is larger than the "
                "average.")
    average = "(SELECT AVG(Amount_Claimed) FROM Claims)"
    assert "the answer shows 1 of the 2 outputs the question lists" in _violations(
        claims, question, f"SELECT Date_Claim_Settled FROM Claims WHERE Amount_Claimed > {average}")
    assert not any("outputs the question lists" in violation for violation in _violations(
        claims, question, f"SELECT Date_Claim_Made, Date_Claim_Settled FROM Claims WHERE Amount_Claimed > {average}"))
    countries = [{"name": "country", "columns": ["Code", "Name", "Continent", "SurfaceArea"],
                  "rows": [["A", "x", "Asia", 1.0], ["B", "y", "Europe", 2.0], ["C", "z", "Africa", 4.0]]}]
    assert not any("outputs the question lists" in violation for violation in _violations(
        countries, "What is the total surface area of the continents Asia and Europe?",
        "SELECT SUM(SurfaceArea) FROM country WHERE Continent = 'Asia' OR Continent = 'Europe'"))


def test_a_noun_after_a_numeric_fields_name_is_its_measure():
    """Held-out Spider train 649, 717, 788, 864, 2026-10-17: "What is the minimum snatch score?" was refused for "score"
    over MIN(Snatch). A noun right after the one-word name of a numeric field the query reads is what it measures.
    Contrast: after a text field's name it stays unread."""
    builders = [{"name": "body_builder", "columns": ["Body_Builder_ID", "Snatch", "Clean_Jerk", "Nickname"],
                 "rows": [[1, 142.5, 175.0, "Ace"], [2, 137.5, 177.5, "Bo"]]}]
    assert _reading(builders, "What is the minimum snatch score?", "SELECT MIN(Snatch) FROM body_builder") == (True, ())
    assert "score" in _reading(builders, "What is the nickname score?", "SELECT Nickname FROM body_builder")[1]


PEOPLE_TABLE = {"name": "people", "columns": ["Person_ID", "Name", "Country", "Age"],
                "rows": [[1, "Alice", "France", 30], [2, "Bob", "France", 20], [3, "Cara", "Spain", 40]]}

def _keyed_violations(tables, keys, question, sql):
    """The constraint violations of ``sql`` over ``tables`` joined by the foreign ``keys`` stated."""
    from regress.sql_import import import_sql
    graph = SchemaGraph.from_tables(tables, keys)
    return constraint_violations(question, import_sql(sql, graph), graph)


def test_an_extreme_a_compared_subquery_finds_is_realized():
    """Spider DEV 44, 159, 2026-10-17: "What are the number of concerts that occurred in the stadium with the largest
    capacity?" was refused for a missing ordering when the stadium was the one a subquery ordered by capacity returns.
    The row holding the extreme may be the one a scalar subquery the rows are compared with finds, over the rows the
    query keeps of its tables. Contrast: an ascending subquery finds the smallest, and a subquery ignoring the outer
    query's filter on its own table finds an extreme of other rows."""
    stadium = {"name": "stadium", "columns": ["Stadium_ID", "Name", "Capacity"],
               "rows": [[1, "Raith", 4000], [2, "Ayr", 10000], [3, "Hampden", 52000]]}
    concert = {"name": "concert", "columns": ["concert_ID", "concert_Name", "Stadium_ID"],
               "rows": [[1, "Auditions", 1], [2, "Super bootcamp", 2], [3, "Home Visits", 2]]}
    keys = [("concert", "Stadium_ID", "stadium", "Stadium_ID")]
    question = "What are the number of concerts that occurred in the stadium with the largest capacity?"
    found = ("SELECT COUNT(*) FROM concert WHERE concert.Stadium_ID = "
             "(SELECT stadium.Stadium_ID FROM stadium ORDER BY stadium.Capacity {} LIMIT 1)")
    missing = "requested extremum or ordering is missing"
    assert missing not in _keyed_violations([stadium, concert], keys, question, found.format("DESC"))
    assert missing in _keyed_violations([stadium, concert], keys, question, found.format("ASC"))
    filtered = "What is the name of the stadium with the largest capacity among the stadiums named Raith or Ayr?"
    assert missing in _keyed_violations(
        [stadium, concert], keys, filtered, "SELECT stadium.Name FROM stadium WHERE stadium.Name IN ('Raith', 'Ayr') "
        "AND stadium.Capacity = (SELECT MAX(stadium.Capacity) FROM stadium)")
    # Spider DEV 789: a join keeping some stadiums is no more the subquery's every stadium than a filter is.
    assert missing in _keyed_violations(
        [stadium, concert], keys, "What is the name of the stadium with the largest capacity?",
        "SELECT stadium.Name FROM stadium JOIN concert ON concert.Stadium_ID = stadium.Stadium_ID WHERE "
        "concert.concert_Name = 'Auditions' AND stadium.Capacity = (SELECT MAX(stadium.Capacity) FROM stadium)")


def test_a_name_inside_a_longer_one_after_each_is_part_of_it():
    """Spider DEV 902, 903 and held-out Spider train 243, 655, 2026-10-17: "Count the number of likes for each student
    id." was refused for an output grain missing, grouped by the student_id it asks for, since "id" alone named
    Highschooler's ID; "For each country and airline name, how many routes are there?" for the routes' airline code.
    A name inside a longer one the clause says is part of it, and a field's name after its table's names that
    field. Contrast: a count of all likes, and the airlines grouped by the routes' airline code, miss the grain."""
    highschooler = {"name": "Highschooler", "columns": ["ID", "name", "grade"],
                    "rows": [[1510, "Jordan", 9], [1689, "Gabriel", 9], [1381, "Tiffany", 9]]}
    likes = {"name": "Likes", "columns": ["student_id", "liked_id"], "rows": [[1689, 1709], [1689, 1510], [1381, 1247]]}
    keys = [("Likes", "student_id", "Highschooler", "ID")]
    grain = "requested output grain is missing"
    question = "Count the number of likes for each student id."
    assert grain not in _keyed_violations([highschooler, likes], keys, question,
                                          "SELECT student_id, COUNT(*) FROM Likes GROUP BY student_id")
    assert grain in _keyed_violations([highschooler, likes], keys, question, "SELECT COUNT(*) FROM Likes")
    airlines = {"name": "airlines", "columns": ["alid", "name", "country"],
                "rows": [[1, "Aerocondor", "Portugal"], [2, "Air Hong Kong", "Hong Kong"]]}
    routes = {"name": "routes", "columns": ["rid", "alid", "airline"],
              "rows": [[37, 1, "AC"], [38, 1, "AC"], [39, 2, "AH"]]}
    keys = [("routes", "alid", "airlines", "alid")]
    question = "For each country and airline name, how many routes are there?"
    joined = ("SELECT airlines.country, {0}, COUNT(*) FROM routes JOIN airlines ON routes.alid = airlines.alid "
              "GROUP BY airlines.country, {0}")
    assert grain not in _keyed_violations([airlines, routes], keys, question, joined.format("airlines.name"))
    assert grain in _keyed_violations([airlines, routes], keys, question, joined.format("routes.airline"))


def test_a_row_count_counts_the_rows_its_joins_keep():
    """Spider DEV 740 and held-out Spider train 573, 2026-10-17: "What is name of the country that speaks the largest
    number of languages?" was refused for the Language field it counts, ordered by a count of the country's
    countrylanguage rows; "Which counties have two or more delegates?" for the Delegate field, a HAVING count of their
    election rows. A populated field of the table whose rows a row count anywhere in the query counts is counted by it:
    the one table no foreign-key join refers to. Contrast: counted over the countries, the languages are not."""
    country = {"name": "country", "columns": ["Code", "Name"], "rows": [["ABW", "Aruba"], ["AFG", "Afghanistan"]]}
    language = {"name": "countrylanguage", "columns": ["CountryCode", "Language", "Percentage"],
                "rows": [["ABW", "Dutch", 5.3], ["ABW", "English", 9.5], ["AFG", "Pashtu", 52.4]]}
    keys = [("countrylanguage", "CountryCode", "country", "Code")]
    question = "What is name of the country that speaks the largest number of languages?"
    unused = "The requested field is not used: Language"
    assert unused not in _keyed_violations(
        [country, language], keys, question, "SELECT country.Name FROM country JOIN countrylanguage ON "
        "countrylanguage.CountryCode = country.Code GROUP BY country.Name ORDER BY COUNT(*) DESC LIMIT 1")
    assert unused in _keyed_violations([country, language], keys, "How many languages are there?",
                                       "SELECT COUNT(*) FROM country")
    # Held-out Spider train 912: a threshold on a field the question names is no count of rows.
    city = {"name": "city", "columns": ["City_ID", "City", "Regional_Population"],
            "rows": [[1, "Shanghai", 23019148], [2, "Nanjing", 8004680], [3, "Hangzhou", 21102000]]}
    assert "The requested field is not used: Regional_Population" in _keyed_violations(
        [city], [], "Please give me a list of cities whose regional population is over 10000000.",
        "SELECT City FROM city GROUP BY City HAVING COUNT(*) > 10000000")
    # Fresh holdout 9, 51: nor is a number before a numeric field's name after it, "more than 30000 in salary".
    employee = {"name": "employee", "columns": ["Ssn", "Fname", "Lname", "Salary"],
                "rows": [[1, "John", "Smith", 30000], [2, "Franklin", "Wong", 40000], [3, "Alicia", "Zelaya", 25000]]}
    assert "The requested field is not used: Salary" in _keyed_violations(
        [employee], [], "Return the first names and last names of employees who earn more than 30000 in salary.",
        "SELECT Fname, Lname FROM employee GROUP BY Fname, Lname HAVING COUNT(*) > 30000")


def test_a_one_word_name_said_as_two_words_is_that_name():
    """Held-out Spider train 612, 2026-10-17: "What is the partition id of the user named "Iron Man"." was refused
    for the tweets' id field it does not ask, answered with partitionid. A one-word name the question says as two
    words is that name. Contrast: "the id" alone is not partitionid."""
    users = {"name": "user_profiles", "columns": ["uid", "name", "partitionid"],
             "rows": [[1, "Iron Man", 1], [2, "Black Widow", 23]]}
    tweets = {"name": "tweets", "columns": ["id", "uid", "text"], "rows": [[1, 1, "Hello"], [2, 2, "Hi"]]}
    keys = [("tweets", "uid", "user_profiles", "uid")]
    shown = "SELECT partitionid FROM user_profiles WHERE name = 'Iron Man'"
    assert _keyed_violations([users, tweets], keys, 'What is the partition id of the user named "Iron Man".',
                             shown) == ()
    assert _keyed_violations([users, tweets], keys, 'What is the id of the user named "Iron Man".', shown) == (
        "The requested field is not used: id",)


def test_an_abbreviated_aggregate_word_said_in_full_is_the_name():
    """Held-out Spider train 666, 2026-10-17: "What are the products with the maximum page size A4 that also have a
    pages per minute color smaller than 5?" was refused for a missing extreme, max_page_size being the field it
    names. Like "average" before the rest of an avg_ name, "maximum" and "minimum" before the rest of a max_ or min_
    name say that name. Contrast: "the maximum pages per minute color" asks for an extreme."""
    product = {"name": "product", "columns": ["product_id", "product", "max_page_size", "pages_per_minute_color"],
               "rows": [[1, "Fujitsu fi-6130", "A4", 30], [2, "Plustek MobileOffice", "A4", 3],
                        [3, "Canon imageFORMULA", "A3", 4]]}
    missing = "requested extremum or ordering is missing"
    assert missing not in _keyed_violations(
        [product], [], "What are the products with the maximum page size A4 that also have a pages per minute color "
        "smaller than 5?", "SELECT product FROM product WHERE max_page_size = 'A4' AND pages_per_minute_color < 5")
    assert missing in _keyed_violations(
        [product], [], "What is the product with the maximum pages per minute color?", "SELECT product FROM product")
    # Held-out Spider train 625: of a field the query does not read, "the maximum page size" is still asked.
    assert "requested aggregate is missing: MAX" in _keyed_violations(
        [product], [], "What is the maximum page size for everything that has more than 3 products listed?",
        "SELECT product FROM product GROUP BY product HAVING COUNT(*) > 3")


def test_a_key_whose_references_lead_to_the_asked_table_shows_it():
    """Held-out Spider train 506, 2026-10-17: "List the id of students who attended some courses?" was refused for
    showing attendance fields, answered by the attendance rows' student_id, which refers to the registrations'
    student_id and that to Students'. A key whose references lead to an asked table holds that table's keys.
    Contrast: a field of the attendance rows referring to nothing asked is no student's."""
    students = {"name": "Students", "columns": ["student_id", "student_details"],
                "rows": [[111, "Marry"], [121, "Martha"]]}
    registrations = {"name": "Student_Course_Registrations", "columns": ["student_id", "course_id"],
                     "rows": [[111, 301], [121, 301]]}
    attendance = {"name": "Student_Course_Attendance", "columns": ["student_id", "course_id", "date_of_attendance"],
                  "rows": [[111, 301, "2008-11-04"], [121, 301, "2012-04-09"]]}
    keys = [("Student_Course_Attendance", "student_id", "Student_Course_Registrations", "student_id"),
            ("Student_Course_Registrations", "student_id", "Students", "student_id")]
    question = "List the id of students who attended some courses?"
    tables = [students, registrations, attendance]
    shows = "the answer shows Student_Course_Attendance's fields, not the Students asked for"
    assert shows not in _keyed_violations(tables, keys, question, "SELECT student_id FROM Student_Course_Attendance")
    assert shows in _keyed_violations(tables, keys, question,
                                      "SELECT date_of_attendance FROM Student_Course_Attendance")


def _keyed_reading(tables, keys, question, sql):
    """The completeness reading of ``sql`` over ``tables`` joined by the foreign ``keys`` stated: (complete, unread)."""
    from engine.query_contract import read_question
    from engine.sql_ast import render_query
    from engine.sql_rank import ScoredQuery
    from regress.sql_import import import_sql
    graph = SchemaGraph.from_tables(tables, keys)
    query = import_sql(sql, graph)
    candidate = ScoredQuery(query, render_query(query), 0.0, ())
    return coverage(question, candidate, graph).complete, read_question(question, candidate, graph).unread


def test_people_are_persons():
    """Fresh holdout of Spider train 743, 973, 2026-10-18: "How many different cities do people originate from?" was
    refused for "people" over a Person table, and "the person with the greatest height" for "person" over a people
    table. People are persons (sql_schema.canon). Contrast: a noun no table or field names is still unread."""
    person = {"name": "Person", "columns": ["name", "age", "city", "job"],
              "rows": [["Alice", 25, "New York City", "student"], ["Bob", 35, "Salt Lake City", "engineer"]]}
    sql = "SELECT COUNT(DISTINCT city) FROM Person"
    assert _keyed_reading([person], [], "How many different cities do people originate from?", sql) == (True, ())
    assert "pupils" in _keyed_reading([person], [], "How many different cities do pupils originate from?", sql)[1]


def test_a_table_a_read_key_refers_to_names_its_rows():
    """Fresh holdout of Spider train 364, 2026-10-18: "How many students are affected by cat allergies?" was refused for
    "students", answered by a count of the Has_Allergy rows, whose StuID refers to Student. A table a key of a table the
    query reads refers to, said by its whole name, names the rows that key refers to. Contrast: a table no read key
    refers to is not read so."""
    student = {"name": "Student", "columns": ["StuID", "LName", "Age"], "rows": [[1001, "Smith", 18], [1002, "Kim", 19]]}
    allergy = {"name": "Has_Allergy", "columns": ["StuID", "Allergy"], "rows": [[1001, "Cat"], [1002, "Cat"]]}
    teacher = {"name": "Teacher", "columns": ["TeacherID", "LName"], "rows": [[1, "Lee"]]}
    keys = [("Has_Allergy", "StuID", "Student", "StuID")]
    sql = "SELECT COUNT(Allergy) FROM Has_Allergy WHERE Allergy = 'Cat'"
    assert _keyed_reading([student, allergy, teacher], keys, "How many students are affected by cat allergies?",
                          sql) == (True, ())
    assert "teachers" in _keyed_reading([student, allergy, teacher], keys,
                                       "How many teachers are affected by cat allergies?", sql)[1]


def test_located_is_read_by_a_place_field():
    """Fresh holdout of Spider train 518, 609, 2026-10-18: "In how many different cities are banks located?" was refused
    for "located", answered by the banks' distinct cities. "Located" is read by a place field the query reads
    (closed_class.PLACE_WORDS). Contrast: the banks' distinct names read no place."""
    bank = {"name": "bank", "columns": ["branch_ID", "bname", "no_of_customers", "city"],
            "rows": [[1, "morningside", 203, "New York City"], [2, "downtown", 123, "Salt Lake City"]]}
    question = "In how many different cities are banks located?"
    assert _keyed_reading([bank], [], question, "SELECT COUNT(DISTINCT city) FROM bank") == (True, ())
    assert "located" in _keyed_reading([bank], [], "In how many different branches are banks located?",
                                       "SELECT COUNT(DISTINCT bname) FROM bank")[1]


def test_greater_than_no_number_is_read_by_its_comparison():
    """Fresh holdout of Spider train 655, 2026-10-18: "Find the name of people whose age is greater than any engineer
    sorted by their age." listed the engineers, "greater" read wherever said. "Greater than" something that is no
    number is read by the comparison it makes. Contrast: compared with an engineer's age it is read."""
    person = {"name": "Person", "columns": ["name", "age", "job"],
              "rows": [["Alice", 25, "student"], ["Bob", 35, "engineer"], ["Zach", 45, "doctor"]]}
    question = "Find the names of persons whose age is greater than any engineer sorted by their age."
    listed = "SELECT name FROM Person WHERE job = 'engineer' ORDER BY age ASC"
    compared = ("SELECT name FROM Person WHERE age > (SELECT MIN(age) FROM Person WHERE job = 'engineer') "
                "ORDER BY age ASC")
    assert "greater" in _keyed_reading([person], [], question, listed)[1]
    assert _keyed_reading([person], [], question, compared) == (True, ())



def test_living_is_read_by_a_place_field():
    """Fresh holdout of Spider train 70, 429, 2026-10-18: "How many employees are living in Canada?" was refused for
    "living", answered by the employees' country, a track's name holding the word. A verb saying where something lives
    or is is read by a place field the query reads (closed_class.PLACE_VERBS), one compared with a value being of a
    table named before the verb; a cell of a table the query does not read says nothing of it. Contrast: the country of
    the customers the employees support does not say where they live, a count of the employees reads no place, a key
    naming an address is no place (Spider DEV 578: the first version answered "the different addresses that have
    students living there" with the students' first names), and a status of the employees holding the word asks for
    it."""
    employees = {"name": "employees", "columns": ["id", "last_name", "first_name", "country"],
                 "rows": [[1, "Adams", "Andrew", "Canada"], [2, "Gill", "Jane", "USA"]]}
    customers = {"name": "customers", "columns": ["id", "first_name", "country", "support_rep_id"],
                 "rows": [[1, "Luis", "Canada", 2], [2, "Leonie", "Germany", 1]]}
    tracks = {"name": "tracks", "columns": ["id", "name"], "rows": [[1, "Living on a Prayer"], [2, "Hey Jude"]]}
    keys = [("customers", "support_rep_id", "employees", "id")]
    tables = [employees, customers, tracks]
    question = "How many employees are living in Canada?"
    assert _keyed_reading(tables, keys, question, "SELECT COUNT(*) FROM employees WHERE country = 'Canada'") == (
        True, ())
    supported = ("SELECT COUNT(DISTINCT employees.id) FROM employees JOIN customers ON customers.support_rep_id = "
                 "employees.id WHERE customers.country = 'Canada'")
    assert "living" in _keyed_reading(tables, keys, question, supported)[1]
    assert "living" in _keyed_reading(tables, keys, "How many employees are living?",
                                      "SELECT COUNT(*) FROM employees")[1]
    addresses = {"name": "Addresses", "columns": ["address_id", "city"], "rows": [[1, "Lake Ryan"], [2, "Port Kimberly"]]}
    students = {"name": "Students", "columns": ["student_id", "first_name", "current_address_id"],
                "rows": [[1, "Timmothy", 1], [2, "Hobart", 2]]}
    assert "living" in _keyed_reading(
        [addresses, students], [("Students", "current_address_id", "Addresses", "address_id")],
        "What are the different addresses that have students living there?",
        "SELECT DISTINCT Students.first_name FROM Addresses JOIN Students ON Students.current_address_id = "
        "Addresses.address_id")[1]
    staff = {"name": "employees", "columns": ["id", "last_name", "country", "status"],
             "rows": [[1, "Adams", "Canada", "Living"], [2, "Gill", "USA", "Retired"]]}
    assert "living" in _keyed_reading([staff], [], question,
                                      "SELECT COUNT(*) FROM employees WHERE country = 'Canada'")[1]


def test_corresponding_before_a_read_table_names_its_rows():
    """Fresh holdout of Spider train 111, 223, 434, 2026-10-18: "What categories have two or more corresponding books
    that were made after 1989?" was refused for "corresponding", answered by the categories of two or more book_club
    rows. Before the name of a table the query reads, "corresponding" says its rows related to those asked for.
    Contrast: before a noun naming no table the query reads it is unread, and so before a field's name ("its
    corresponding problem id")."""
    books = {"name": "book_club", "columns": ["book_club_id", "Year", "Author", "Book_Title", "Category"],
             "rows": [[1, 1989, "Michael Nava", "Goldenboy", "Gay M/SF"], [2, 1990, "Lev Raphael", "Somebody", "Gay M/SF"],
                      [3, 1991, "Nikki Baker", "In the Game", "Lesb. M/SF"]]}
    sql = "SELECT Category FROM book_club WHERE Year > 1989 GROUP BY Category HAVING COUNT(*) >= 2"
    asked = "What categories have two or more corresponding books that were made after 1989?"
    assert _keyed_reading([books], [], asked, sql) == (True, ())
    assert "corresponding" in _keyed_reading(
        [books], [], "What categories have two or more corresponding films that were made after 1989?", sql)[1]
    problems = {"name": "Problems", "columns": ["problem_id", "product_id", "date_reported"],
                "rows": [[1, 4, "2010-01-01"], [2, 5, "2011-02-02"]]}
    assert "corresponding" in _keyed_reading(
        [problems], [], "What is the oldest problem and its corresponding problem product?",
        "SELECT problem_id FROM Problems ORDER BY date_reported ASC LIMIT 1")[1]


def test_a_table_named_in_another_form_does_not_narrow_an_exclusion():
    """Fresh holdout of Spider train 635, 855, 2026-10-18: "What are the ids of courses without prerequisites?" refused
    the courses NOT IN those a prereq row names, as narrowed by prereq, "which the question does not name", and answered
    with prereq rows less themselves (none). A table the question names in another form (sql_expansion.
    says_in_another_form: "prerequisites" spells out prereq) is named. Contrast: a table named in no form still
    narrows the exclusion."""
    course = {"name": "course", "columns": ["course_id", "title", "credits"],
              "rows": [["BIO-101", "Intro. to Biology", 4], ["BIO-301", "Genetics", 4], ["CS-101", "Intro. to CS", 4]]}
    prereq = {"name": "prereq", "columns": ["course_id", "prereq_id"], "rows": [["BIO-301", "BIO-101"]]}
    sql = ("SELECT course_id FROM course WHERE course_id NOT IN "
           "(SELECT course.course_id FROM course JOIN prereq ON prereq.course_id = course.course_id)")
    assert _violations([course, prereq], "What are the ids of courses without prerequisites?", sql) == ()
    assert _violations([course, prereq], "What are the ids of courses without requirements?", sql) == (
        "the exclusion is narrowed by prereq, which the question does not name",)



def test_a_difference_keeps_the_rows_named_before_its_denial():
    """Held out 368, 2026-10-18: "Return the names of gymnasts who did not grow up in Santo Domingo." was answered,
    once the denial reached Santo Domingo, by every person's name less the gymnasts from there: the gymnasts were read
    only by the subtracted branch. A table the question names before a difference's denial (sql_extrema.
    denial_position) is read by the part the difference keeps, an EXCEPT's first branch or the query a NOT IN keeps.
    Contrast: the rows named after the denial ("the people who are not gymnasts") may be read by the subtracted part
    alone, and the gymnasts' own rows kept answer it."""
    people = {"name": "people", "columns": ["People_ID", "Name", "Hometown"],
              "rows": [[1, "Paul Hamm", "Santo Domingo"], [2, "Lorraine Sutton", "Bonao"], [3, "Ruby Sanchez", "Bonao"]]}
    gymnast = {"name": "gymnast", "columns": ["Gymnast_ID", "Floor_Exercise_Points"], "rows": [[1, 9.725], [2, 9.7]]}
    from engine.sql_ast import render_query
    from regress.sql_import import import_sql
    graph = SchemaGraph.from_tables([people, gymnast], [("gymnast", "Gymnast_ID", "people", "People_ID")])

    def violations(question, sql):
        return constraint_violations(question, import_sql(sql, graph), graph)
    question = "Return the names of gymnasts who did not grow up in Santo Domingo."
    unkept = "the difference keeps rows that are not the gymnast asked for"
    subtracted = ("SELECT people.Name FROM people EXCEPT SELECT people.Name FROM gymnast JOIN people ON "
                  "gymnast.Gymnast_ID = people.People_ID WHERE people.Hometown = 'Santo Domingo'")
    assert unkept in violations(question, subtracted)
    kept_out = ("SELECT Name FROM people WHERE People_ID NOT IN (SELECT gymnast.Gymnast_ID FROM gymnast JOIN people "
                "ON gymnast.Gymnast_ID = people.People_ID WHERE people.Hometown = 'Santo Domingo')")
    assert unkept in violations(question, kept_out)
    kept = ("SELECT people.Name FROM gymnast JOIN people ON gymnast.Gymnast_ID = people.People_ID "
            "WHERE people.Hometown != 'Santo Domingo'")
    assert violations(question, kept) == (), render_query(import_sql(kept, graph))
    assert unkept not in violations("Return the names of the people who are not gymnasts.",
                                    "SELECT Name FROM people EXCEPT SELECT people.Name FROM gymnast JOIN people ON "
                                    "gymnast.Gymnast_ID = people.People_ID")
    # A key the kept part shows names the rows it refers to (Spider DEV 778, 779: the first version refused "the
    # country codes for countries that do not speak English" over countrylanguage.CountryCode), and a name after
    # "whose" names what the rows have (held out 597: "reviewers whose ratings do not have a date").
    country = {"name": "country", "columns": ["Code", "Name"], "rows": [["ABW", "Aruba"], ["AFG", "Afghanistan"]]}
    language = {"name": "countrylanguage", "columns": ["CountryCode", "Language"],
                "rows": [["ABW", "Dutch"], ["ABW", "English"], ["AFG", "Pashto"]]}
    spoken = SchemaGraph.from_tables([country, language], [("countrylanguage", "CountryCode", "country", "Code")])
    codes = ("SELECT countrylanguage.CountryCode FROM countrylanguage EXCEPT SELECT countrylanguage.CountryCode FROM "
             "country JOIN countrylanguage ON countrylanguage.CountryCode = country.Code WHERE "
             "countrylanguage.Language = 'English'")
    assert "the difference keeps rows that are not the country asked for" not in constraint_violations(
        "What are the country codes for countries that do not speak English?", import_sql(codes, spoken), spoken)
    reviewer = {"name": "Reviewer", "columns": ["rID", "name"], "rows": [[201, "Sarah Martinez"], [202, "Daniel Lewis"]]}
    rating = {"name": "Rating", "columns": ["rID", "mID", "stars"], "rows": [[201, 101, 2], [201, 106, 4]]}
    rated = SchemaGraph.from_tables([reviewer, rating], [("Rating", "rID", "Reviewer", "rID")])
    unrated = ("SELECT DISTINCT Reviewer.name FROM Reviewer WHERE Reviewer.rID NOT IN (SELECT Reviewer.rID FROM Rating "
               "JOIN Reviewer ON Rating.rID = Reviewer.rID)")
    assert "the difference keeps rows that are not the Rating asked for" not in constraint_violations(
        "What are the different names of all reviewers whose ratings do not have a date field?",
        import_sql(unrated, rated), rated)


def test_a_difference_subtracting_every_row_it_keeps_answers_nothing():
    """Fresh holdout of Spider train 635, 855, 2026-10-18: "What are the ids of courses without prerequisites?" was
    answered with the prereq rows' course ids less those of the prereq rows joined to their courses, which is no row.
    An EXCEPT whose second branch reads its first branch's table unfiltered, joined only to tables that table's keys
    refer to, subtracts every row it keeps, and is refused. Contrast: a filter in the second branch subtracts only the
    rows it keeps."""
    course = {"name": "course", "columns": ["course_id", "title"],
              "rows": [["BIO-101", "Intro. to Biology"], ["BIO-301", "Genetics"], ["CS-101", "Intro. to CS"]]}
    prereq = {"name": "prereq", "columns": ["course_id", "prereq_id"], "rows": [["BIO-301", "BIO-101"]]}
    graph = SchemaGraph.from_tables([course, prereq], [("prereq", "course_id", "course", "course_id"),
                                                       ("prereq", "prereq_id", "course", "course_id")])
    question = "What are the ids of courses without prerequisites?"
    empty = ("SELECT prereq.course_id FROM prereq EXCEPT SELECT prereq.course_id FROM course JOIN prereq ON "
             "prereq.course_id = course.course_id")
    assert "the difference subtracts every row it keeps" in constraint_violations(
        question, import_sql(empty, graph), graph)
    filtered = empty + " WHERE course.title = 'Genetics'"
    assert "the difference subtracts every row it keeps" not in constraint_violations(
        "What are the ids of courses with prerequisites other than Genetics?", import_sql(filtered, graph), graph)


def test_a_hyphenated_first_ranks_nothing():
    """Held out 485, 923, 2026-10-18: "What are the first and last names of the first-grade students who are NOT taught
    by teacher OTHA MOYER?" was answered by the lowest grade's rows: "first" was read as a rank, the query's
    FirstName spelling the word. "First" or "last" joined to the next word by a hyphen names a kind of row, and where
    the question ranks by nothing else a query keeping its top rows is refused. Contrast: "the first student to
    register" ranks."""
    students = {"name": "list", "columns": ["LastName", "FirstName", "Grade", "Classroom"],
                "rows": [["CAR", "MAUDE", 1, 101], ["KRISTENSEN", "STORMY", 6, 112], ["VANDERWOUDE", "SHERWOOD", 3, 107]]}
    question = "What are the first and last names of the first-grade students?"
    ranked = "SELECT FirstName, LastName FROM list ORDER BY Grade ASC LIMIT 1"
    assert _violations([students], question, ranked) == (
        "the query keeps only its top rows, which the question does not rank",)
    assert _violations([students], question, "SELECT FirstName, LastName FROM list WHERE Grade = 1") == ()
    assert "the query keeps only its top rows, which the question does not rank" not in _violations(
        [students], "What are the first and last names of the first student in the lowest grade?", ranked)



def test_the_names_of_a_table_holding_a_name_are_its_own():
    """Fresh holdout of Spider train 600, 910, 474, 2026-10-18: "What are the names of the albums that have more than 10
    tracks?" was answered with the tracks' names, albums holding a title, and "the names of the 3 departments with the
    most courses" with the instructors'. The names of a table that holds a name of its own are that name, not a joined
    table's, and the album's own title answers, asking for no field another table calls a name. Contrast: a table
    holding no name is named by a table it is joined to ("the names of poker players" are people's)."""
    albums = {"name": "albums", "columns": ["id", "title", "artist_id"],
              "rows": [[1, "For Those About To Rock", 1], [2, "Balls to the Wall", 2]]}
    tracks = {"name": "tracks", "columns": ["id", "name", "album_id"],
              "rows": [[1, "For Those About To Rock (We Salute You)", 1], [2, "Put The Finger On You", 1],
                       [3, "Balls to the Wall", 2]]}
    graph = SchemaGraph.from_tables([albums, tracks], [("tracks", "album_id", "albums", "id")])
    question = "What are the names of the albums that have more than 10 tracks?"
    joined = ("SELECT tracks.name FROM albums JOIN tracks ON tracks.album_id = albums.id GROUP BY tracks.name "
              "HAVING COUNT(*) > 10")
    assert "the answer shows the names of tracks, not of the albums asked for" in constraint_violations(
        question, import_sql(joined, graph), graph)
    own = ("SELECT albums.title FROM albums JOIN tracks ON tracks.album_id = albums.id GROUP BY albums.title "
           "HAVING COUNT(*) > 10")
    assert constraint_violations(question, import_sql(own, graph), graph) == ()
    # Contrast: a self-join's shown field is its table's ("the title of the course with Differential Geometry as a
    # prerequisite" shows the related course's title; fresh holdout 539, 790, a loss the first version made).
    course = {"name": "course", "columns": ["course_id", "title"],
              "rows": [[1, "Differential Geometry"], [2, "Topology"], [3, "Number Theory"]]}
    prereq = {"name": "prereq", "columns": ["course_id", "prereq_id"], "rows": [[2, 1], [3, 2]]}
    courses = SchemaGraph.from_tables([course, prereq], [("prereq", "course_id", "course", "course_id"),
                                                        ("prereq", "prereq_id", "course", "course_id")])
    related = ('SELECT related.title FROM prereq AS relation JOIN course AS owner ON relation.prereq_id = '
               'owner.course_id JOIN course AS related ON relation.course_id = related.course_id WHERE owner.title = '
               "'Differential Geometry'")
    assert not [violation for violation in constraint_violations(
        "What is the title of the course with Differential Geometry as a prerequisite?",
        import_sql(related, courses), courses) if "the names of" in violation or "shows no" in violation]
    people = {"name": "people", "columns": ["People_ID", "Name"],
              "rows": [[1, "Aleksey Ostapenko"], [2, "Teodor Salparov"]]}
    players = {"name": "poker_player", "columns": ["Poker_Player_ID", "People_ID", "Earnings"],
               "rows": [[1, 1, 476090.0], [2, 2, 189233.0]]}
    poker = SchemaGraph.from_tables([people, players], [("poker_player", "People_ID", "people", "People_ID")])
    names = "SELECT people.Name FROM people JOIN poker_player ON poker_player.People_ID = people.People_ID"
    assert not [violation for violation in constraint_violations(
        "What are the names of poker players?", import_sql(names, poker), poker) if "the names of" in violation]




def test_the_names_a_list_asks_for_each_table_are_its_own():
    """Fresh holdout of Spider train 582, 2026-10-18: "What is the name and country of origin for each artist who has
    released a song with a resolution higher than 900?" showed the songs' names: the list's owner follows "for each".
    Contrast: an artist's own name, a song's field naming the artist ("artist names" of songs), the songs a possessive
    names ("the artist's songs"), and a list of two tables' names."""
    artist = {"name": "artist", "columns": ["artist_name", "country", "gender"],
              "rows": [["Shrikanta", "India", "Male"], ["Prity", "Bangladesh", "Female"]]}
    song = {"name": "song", "columns": ["song_name", "artist_name", "resolution"],
            "rows": [["Tumi robe nirobe", "Shrikanta", 1080], ["Shukno patar nupur pae", "Prity", 512]]}
    music = SchemaGraph.from_tables([artist, song], [("song", "artist_name", "artist", "artist_name")])
    question = ("What is the name and country of origin for each artist who has released a song with a resolution "
                "higher than 900?")
    joined = " FROM artist JOIN song ON song.artist_name = artist.artist_name WHERE song.resolution > 900"
    message = "the answer shows the names of song, not of the artist asked for"
    assert message in constraint_violations(question, import_sql("SELECT song.song_name, artist.country" + joined,
                                                                 music), music)
    assert message not in constraint_violations(
        question, import_sql("SELECT artist.artist_name, artist.country" + joined, music), music)
    assert not [violation for violation in constraint_violations(
        "List the artist names of songs with a resolution above 900.",
        import_sql("SELECT song.artist_name FROM song WHERE song.resolution > 900", music), music)
        if "name" in violation]
    assert not [violation for violation in constraint_violations(
        "What are the names of the artist's songs?", import_sql("SELECT song.song_name FROM song", music), music)
        if "name" in violation]
    # So are the names a list asks "for" a table: "What are the names and average salaries for departments with average
    # salary higher than 42000?" showed the instructors' names (fresh holdout 848, 2026-10-18).
    department = {"name": "department", "columns": ["dept_name", "building", "budget"],
                  "rows": [["Physics", "Watson", 70000], ["Finance", "Painter", 120000]]}
    instructor = {"name": "instructor", "columns": ["ID", "name", "dept_name", "salary"],
                  "rows": [["10101", "Srinivasan", "Physics", 65000], ["12121", "Wu", "Finance", 90000]]}
    school = SchemaGraph.from_tables([department, instructor], [("instructor", "dept_name", "department", "dept_name")])
    question = "What are the names and average salaries for departments with average salary higher than 42000?"
    grouped = (" FROM instructor JOIN department ON instructor.dept_name = department.dept_name GROUP BY {0} "
               "HAVING AVG(instructor.salary) > 42000")
    assert "the answer shows the names of instructor, not of the department asked for" in constraint_violations(
        question, import_sql("SELECT instructor.name, AVG(instructor.salary)" + grouped.format("instructor.name"),
                             school), school)
    assert not [violation for violation in constraint_violations(question, import_sql(
        "SELECT department.dept_name, AVG(instructor.salary)" + grouped.format("department.dept_name"), school),
        school) if "the names of" in violation]
    # Each name a list asks for is its own table's ("titles of songs and names of singers"), and names said after a
    # possessive ("their instructors' names for courses") or of what no table names are not read.
    singer = {"name": "singer", "columns": ["Singer_ID", "Name"], "rows": [[1, "Liliane Bettencourt"], [2, "Tabea"]]}
    track = {"name": "song", "columns": ["Song_ID", "Title", "Singer_ID"], "rows": [[1, "Do They Know", 1]]}
    songs = SchemaGraph.from_tables([singer, track], [("song", "Singer_ID", "singer", "Singer_ID")])
    both = "SELECT song.Title, singer.Name FROM singer JOIN song ON song.Singer_ID = singer.Singer_ID"
    assert not [violation for violation in constraint_violations(
        "Show titles of songs and names of singers.", import_sql(both, songs), songs) if "name" in violation]
    assert not [violation for violation in constraint_violations(
        "Show the songs' titles for each singer's names.", import_sql(both, songs), songs) if "name" in violation]



def test_each_listed_output_is_shown_by_a_field_it_names():
    """Spider DEV 800, 2026-10-18: "What are the names and areas of countries with the top 5 largest area?" was answered
    by the cities' and the countries' names ranked by SurfaceArea, two outputs for the two listed but no area among
    them. Each output a list names is shown by a field it names, or by a field whose name says its words. Contrast: the
    countries' names and areas, and "the name and the release year of the song" by Song_Name."""
    country = {"name": "country", "columns": ["Code", "Name", "SurfaceArea"],
               "rows": [["RUS", "Russian Federation", 17075400.0], ["CHN", "China", 9572900.0], ["ABW", "Aruba", 193.0]]}
    city = {"name": "city", "columns": ["ID", "Name", "CountryCode"],
            "rows": [[1, "Moscow", "RUS"], [2, "Shanghai", "CHN"], [3, "Oranjestad", "ABW"]]}
    graph = SchemaGraph.from_tables([country, city], [("city", "CountryCode", "country", "Code")])
    question = "What are the names and areas of countries with the top 2 largest area?"
    names = ("SELECT city.Name, country.Name FROM city JOIN country ON city.CountryCode = country.Code "
             "ORDER BY country.SurfaceArea DESC LIMIT 2")
    assert "the answer shows no area the question lists" in constraint_violations(
        question, import_sql(names, graph), graph)
    areas = "SELECT country.Name, country.SurfaceArea FROM country ORDER BY country.SurfaceArea DESC LIMIT 2"
    assert not [violation for violation in constraint_violations(question, import_sql(areas, graph), graph)
                if "the question lists" in violation]



def test_never_and_what_rows_do_not_have_are_exclusions():
    """Held-out Spider train 48, 72, 2026-10-18: "the artists that have produced a song in English but have never
    received a rating higher than 8" was answered by the songs rated over 8, "never" asking no exclusion, and "the
    students who are living in the dorms that do not have amenity TV Lounge" by an inequality on the amenity's name,
    which keeps every dorm with another amenity. "Never" excludes as "not" does (closed_class.EXCLUSION_CUES), and what
    the rows do not have is subtracted from the rows they relate to. Contrast: the set difference, and an inequality on
    the rows shown ("the dorms whose name is not Smith Hall")."""
    artist = {"name": "artist", "columns": ["artist_name", "country"], "rows": [["Enrique", "USA"], ["Prity", "India"]]}
    song = {"name": "song", "columns": ["song_name", "artist_name", "languages", "rating"],
            "rows": [["Tumi", "Prity", "english", 9], ["Hola", "Enrique", "english", 7]]}
    music = SchemaGraph.from_tables([artist, song], [("song", "artist_name", "artist", "artist_name")])
    question = ("What are the names of the different artists that have produced a song in English but have never "
                "received a rating higher than 8?")
    rated = ("SELECT DISTINCT artist.artist_name FROM artist JOIN song ON song.artist_name = artist.artist_name "
             "WHERE song.languages = 'english' AND song.rating > 8")
    assert "requested exclusion is missing" in constraint_violations(question, import_sql(rated, music), music)
    difference = ("SELECT DISTINCT artist_name FROM song WHERE languages = 'english' EXCEPT "
                  "SELECT DISTINCT artist_name FROM song WHERE rating > 8")
    assert "requested exclusion is missing" not in constraint_violations(
        question, import_sql(difference, music), music)
    # A difference reads "never" as it reads "not" (Spider DEV 282: "the employees who never received any evaluation",
    # a loss the first version made, "never" left unread).
    employees = [{"name": "employee", "columns": ["Employee_ID", "Name"], "rows": [[1, "George"], [2, "Lee"]]},
                 {"name": "evaluation", "columns": ["Employee_ID", "Year_awarded", "Bonus"],
                  "rows": [[1, "2011", 3000]]}]
    assert _reading(employees, "What are the names of the employees who never received any evaluation?",
                    "SELECT employee.Name FROM employee EXCEPT SELECT employee.Name FROM employee JOIN evaluation ON "
                    "evaluation.Employee_ID = employee.Employee_ID") == (True, ())
    tables = [{"name": "Student", "columns": ["StuID", "Fname", "Age"],
               "rows": [[1001, "Linda", 18], [1002, "Tracy", 19]]},
              {"name": "Dorm", "columns": ["dormid", "dorm_name"],
               "rows": [[100, "Smith Hall"], [109, "Fawlty Towers"]]},
              {"name": "Dorm_amenity", "columns": ["amenid", "amenity_name"],
               "rows": [[900, "TV Lounge"], [901, "Gym"]]},
              {"name": "Has_amenity", "columns": ["dormid", "amenid"], "rows": [[100, 900], [100, 901], [109, 901]]},
              {"name": "Lives_in", "columns": ["stuid", "dormid", "room_number"],
               "rows": [[1001, 100, 112], [1002, 109, 105]]}]
    dorms = SchemaGraph.from_tables(tables, [("Has_amenity", "dormid", "Dorm", "dormid"),
                                             ("Has_amenity", "amenid", "Dorm_amenity", "amenid"),
                                             ("Lives_in", "stuid", "Student", "StuID"),
                                             ("Lives_in", "dormid", "Dorm", "dormid")])
    question = "Find the first name and age of students who are living in the dorms that do not have amenity TV Lounge."
    unequal = ("SELECT Student.Fname, Student.Age FROM Dorm JOIN Has_amenity ON Has_amenity.dormid = Dorm.dormid "
               "JOIN Dorm_amenity ON Has_amenity.amenid = Dorm_amenity.amenid JOIN Lives_in ON Lives_in.dormid = "
               "Dorm.dormid JOIN Student ON Lives_in.stuid = Student.StuID "
               "WHERE Dorm_amenity.amenity_name != 'TV Lounge'")
    message = "the exclusion keeps rows having some Dorm_amenity other than the one excluded"
    assert message in constraint_violations(question, import_sql(unequal, dorms), dorms)
    subtracted = ("SELECT Student.Fname, Student.Age FROM Student JOIN Lives_in ON Lives_in.stuid = Student.StuID "
                  "WHERE Lives_in.dormid NOT IN (SELECT Has_amenity.dormid FROM Has_amenity JOIN Dorm_amenity ON "
                  "Has_amenity.amenid = Dorm_amenity.amenid WHERE Dorm_amenity.amenity_name = 'TV Lounge')")
    assert message not in constraint_violations(question, import_sql(subtracted, dorms), dorms)
    assert not [violation for violation in constraint_violations(
        "Find the names of the dorms that do not have the name Smith Hall.",
        import_sql("SELECT Dorm.dorm_name FROM Dorm WHERE Dorm.dorm_name != 'Smith Hall'", dorms), dorms)
        if "keeps rows having" in violation]


def test_a_table_named_only_inside_a_longer_name_is_not_joined():
    """Fresh holdout of Spider train 917, 2026-10-18: "Show all document type codes, document type names, document type
    descriptions." joined the documents, showing only the types some document has: "document" is said only inside the
    names of the type's fields. Contrast: a question naming the documents alone joins them, and a table the other one
    refers to (an apartment booking's apartment) narrows and repeats no row."""
    types = {"name": "Ref_Document_Types",
             "columns": ["Document_Type_Code", "Document_Type_Name", "Document_Type_Description"],
             "rows": [["CD", "CD", "Compact disc"], ["BK", "Book", "A book"], ["PP", "Paper", "Paper document"]]}
    documents = {"name": "Documents", "columns": ["Document_ID", "Document_Type_Code", "Document_Name"],
                 "rows": [[1, "CD", "Robin CV"], [2, "BK", "Marry CV"]]}
    graph = SchemaGraph.from_tables([types, documents], [("Documents", "Document_Type_Code", "Ref_Document_Types",
                                                          "Document_Type_Code")])
    joined = ('SELECT DISTINCT "Ref_Document_Types"."Document_Type_Code", "Ref_Document_Types"."Document_Type_Name", '
              '"Ref_Document_Types"."Document_Type_Description" FROM "Documents" JOIN "Ref_Document_Types" ON '
              '"Documents"."Document_Type_Code" = "Ref_Document_Types"."Document_Type_Code"')
    message = "the query joins Documents, which the question names only inside a longer name"
    question = "Show all document type codes, document type names, document type descriptions."
    assert message in constraint_violations(question, import_sql(joined, graph), graph)
    own = ('SELECT "Ref_Document_Types"."Document_Type_Code", "Ref_Document_Types"."Document_Type_Name", '
           '"Ref_Document_Types"."Document_Type_Description" FROM "Ref_Document_Types"')
    assert message not in constraint_violations(question, import_sql(own, graph), graph)
    assert message not in constraint_violations(
        "Show the document type codes of all documents.", import_sql(joined, graph), graph)
    apartments = {"name": "Apartments", "columns": ["apt_id", "apt_number"],
                  "rows": [[1, "Suite 645"], [2, "Apt. 585"]]}
    bookings = {"name": "Apartment_Bookings", "columns": ["apt_booking_id", "apt_id", "booking_start_date"],
                "rows": [[258, 1, "2016-09-26"], [279, 2, "2016-04-01"]]}
    booked = SchemaGraph.from_tables([apartments, bookings], [("Apartment_Bookings", "apt_id", "Apartments", "apt_id")])
    starts = ('SELECT "Apartment_Bookings"."booking_start_date" FROM "Apartment_Bookings" JOIN "Apartments" ON '
              '"Apartment_Bookings"."apt_id" = "Apartments"."apt_id"')
    assert not [violation for violation in constraint_violations(
        "Show the start dates of all the apartment bookings.", import_sql(starts, booked), booked)
        if "inside a longer name" in violation]


TESTS =[value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]

if __name__ == '__main__':
    for test in TESTS:
        test()
        print('PASS', test.__name__)
