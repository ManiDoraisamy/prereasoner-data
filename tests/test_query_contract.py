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
    the currency check use (query_contract.unreadable_measure_reason)."""
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


PEOPLE_TABLE = {"name": "people", "columns": ["Person_ID", "Name", "Country", "Age"],
                "rows": [[1, "Alice", "France", 30], [2, "Bob", "France", 20], [3, "Cara", "Spain", 40]]}

TESTS =[value for name, value in globals().copy().items() if name.startswith('test_') and callable(value)]

if __name__ == '__main__':
    for test in TESTS:
        test()
        print('PASS', test.__name__)
