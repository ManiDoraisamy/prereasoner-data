// Dependency-free regression checks for reference-data state in the production classic script.
// Run: node web/tests/workbook_reference.test.js
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const referenceSource = fs.readFileSync(
  path.join(__dirname, '..', 'public', 'lib', 'workbook-reference.js'), 'utf8');
const conversationSource = fs.readFileSync(
  path.join(__dirname, '..', 'public', 'lib', 'workbook-conversations.js'), 'utf8');
const workbookSource = fs.readFileSync(
  path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
const turnRendererSource = fs.readFileSync(
  path.join(__dirname, '..', 'public', 'lib', 'turn-renderer.js'), 'utf8');
const firebaseSource = fs.readFileSync(
  path.join(__dirname, '..', 'public', 'lib', 'firebase-init.js'), 'utf8');
const source = fs.readFileSync(path.join(__dirname,'..','public','lib','result-wire.js'),'utf8')
  + '\n' + turnRendererSource + '\n' + referenceSource + '\n' + conversationSource + '\n' + workbookSource;
assert(!/world_join:\s*'wikipedia lookup'/.test(source),
  'shared-data joins must not label non-Wikidata sources such as ECB as Wikipedia');
assert(/world_join:\s*'reference lookup'/.test(source),
  'shared-data joins must use source-neutral provenance language');
assert(firebaseSource.includes("at('dataset_semantics')") && firebaseSource.includes('onDatasetSemantics'),
  'live engine traces must carry dataset-semantics set and clear state');
assert(firebaseSource.includes("at('analysis')") && firebaseSource.includes('onAnalysis'),
  'live engine traces must carry the server-owned analysis identity');
assert(firebaseSource.includes("at('execution')") && firebaseSource.includes('onExecution'),
  'live engine traces must carry the backend that produced each call');
let finish;
const done = new Promise((resolve, reject) => { finish = error => error ? reject(error) : resolve(); });
const storage = new Map();
const context = {
  console,
  esc: value => String(value),
  setTimeout,
  clearTimeout,
  TextEncoder,
  URL,
  crypto: {randomUUID: () => 'job'},
  location: {search: '', pathname: '/reason'},
  history: {replaceState() {}},
  document: {getElementById: () => null, querySelector: () => null, querySelectorAll: () => []},
  sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  localStorage: {getItem: key => storage.get('local:' + key) || null, setItem: (key, value) => storage.set('local:' + key, value)},
  SS: {TABLES: 'tables', Q: 'question'},
  API_BASE: '',
  slug: (name, index) => name || ('t' + index),
  parseCSV: () => ({cols: [], rows: []}),
  confirm: () => true,
  __finish: finish,
};
context.window = context;
vm.createContext(context);

const checks = `
(async function () {
  try {
    if (!hasCellValue(0)) throw new Error('numeric zero must be a real reference value');
    // A share of a whole reads as a percentage in the rail; any other one-number answer as before.
    J = {result: {columns: ['share'], rows: [['0.62318840579710144928']]}, unit: 'percent'};
    if (resultSummary().v !== '62.32%') throw new Error('a share was not shown as a percentage: ' + resultSummary().v);
    J = {result: {columns: ['share'], rows: [['0.30000000000000000000']]}, unit: 'percent'};
    if (resultSummary().v !== '30%') throw new Error('a share was not shown as a percentage: ' + resultSummary().v);
    J = {result: {columns: ['avg'], rows: [['0.30000000000000000000']]}};
    if (resultSummary().v !== '0.3') throw new Error('a plain fraction was shown as a percentage: ' + resultSummary().v);
    J = null;
    if (referenceKey('products', ['sku', 'category']) !== 'sku') throw new Error('reference identity ignored its join key');
    const compact = referenceRows({rows: [[0, 'zero'], ['', ''], [null, null]]});
    if (compact.length !== 1 || compact[0][0] !== 0) throw new Error('referenceRows discarded or changed zero');

    BOOK = [{id:'m1', cls:'master', name:'sku', cols:['sku','category'], rows:[['A','Apparel']],
             saved:true, dirty:true, cellAI:new Set(['0,1'])}];
    CHAT = [{q:'prior', reply:'answer'}]; SETTLED=false;
    DS_META = [{table:'orders', column:'amount', currency:'EUR'}];
    const snapshot = convSnapshot();
    if (snapshot.v !== 3) throw new Error('per-sheet execution provenance requires snapshot v3');
    const saved = snapshot.sheets[0];
    if (!saved.dirty) throw new Error('dirty state was serialized as clean');
    if (!saved.cellAI || saved.cellAI[0] !== '0,1') throw new Error('cell provenance was not serialized');
    if (!snapshot.datasetSemantics || snapshot.datasetSemantics[0].currency !== 'EUR')
      throw new Error('dataset semantics were omitted from the restorable snapshot');

    // A turn can contain several engine calls whose auto policy chose different backends.
    // A late terminal event must annotate only that call's sheets, not relabel the whole turn.
    paint = () => {}; VIEWS = []; BOOK = []; RUN = 4; AUTO = true; J = null; EXEC = null; EXEC_BY_KEY = new Map();
    noteExecution({actual:'python', verified:false}, 'call-python');
    appendView({name:'from_python', op:'select', label:'from python', columns:['v'], rows:[[1]],
      sql:'SELECT 1', python:'python one'}, null, 'call-python');
    appendView({name:'from_sql', op:'select', label:'from sql', columns:['v'], rows:[[2]],
      sql:'SELECT 2', python:'python two'}, null, 'call-sql');
    noteExecution({actual:'sql', verified:false}, 'call-sql');
    const pySheet = BOOK.find(s => s.viewName === 'from_python');
    const sqlSheet = BOOK.find(s => s.viewName === 'from_sql');
    if (sheetSource(pySheet).primary !== 'py') throw new Error('a later SQL call relabelled a Python sheet');
    if (sheetSource(sqlSheet).primary !== 'sql') throw new Error('a SQL sheet did not retain its own backend');

    // An exact decimal from the engine displays at three decimals; an uploaded cell shows as typed
    // (formesign-contracts showed "19040.46312152585994148" on its Result sheet, 2026-10-01).
    if (fmt('19040.46312152585994148', true) !== '19040.463') throw new Error('a computed decimal was not rounded for display');
    if (fmt('3.14159') !== '3.14159') throw new Error('an uploaded cell was reformatted');
    if (fmt('365.631', true) !== '365.631' || fmt('ACME 1.2345', true) !== 'ACME 1.2345')
      throw new Error('a short decimal or a text cell was changed');

    // A world lookup the finished derivation never joined leaves the workbook when the turn settles
    // (bank-marketing leads, 2026-10-01); one a world_join used stays.
    const lookupBook = BOOK, lookupViews = VIEWS, lookupRun = RUN, lookupPaint = paint;
    paint = () => {}; RUN = 9;
    for (const [op, kept] of [['filter', 0], ['world_join', 2]]) {
      BOOK = [{id:'u', cls:'input', name:'leads'}, {id:'r9_1', cls:'ref', name:'city'},
              {id:'r9_2', cls:'ref', name:'city'}, {id:'v9_1', cls:'deriv', name:'count', result:true}];
      VIEWS = [{op, name:'step'}, {op:'group_agg', name:'count'}];
      dropUnusedLookups();
      if (BOOK.filter(s => s.cls === 'ref').length !== kept)
        throw new Error(op + ' kept ' + BOOK.filter(s => s.cls === 'ref').length + ' lookup sheets');
    }
    BOOK = lookupBook; VIEWS = lookupViews; RUN = lookupRun; paint = lookupPaint;

    // The flat tab strip names each sheet once: a branch name tells shared step names apart, and
    // a name still shared is numbered (complex-promotions and the two-lookup FX trail, 2026-10-01).
    const tabBook = BOOK;
    BOOK = [
      {id:'u', cls:'input', name:'orders'},
      {id:'a', cls:'deriv', name:'combined', sectionLabel:'top customers'},
      {id:'b', cls:'deriv', name:'total', sectionLabel:'top customers'},
      {id:'c', cls:'deriv', name:'combined', sectionLabel:'top products'},
      {id:'d', cls:'deriv', name:'candidate pairs', sectionLabel:'gaps'},
      {id:'e', cls:'deriv', name:'reference lookup'},
      {id:'f', cls:'deriv', name:'reference lookup'},
      {id:'r', cls:'deriv', name:'not yet matched', result:true},
    ];
    const flat = flatNames();
    const named = ['a','b','c','d','e','f'].map(id => flat.get(id)).join(' | ');
    if (named !== 'combined · top customers | total | combined · top products | candidate pairs | enriched 1 | enriched 2')
      throw new Error('tab names were not made unique: ' + named);
    if (flatName(BOOK[0]) !== 'orders') throw new Error('an uploaded sheet lost its own name');
    BOOK = tabBook;

    CHAT = [{q:'prior', reply:'answer'}]; SETTLED=false; REFCANDS=[];
    const perSheet = convSnapshot();
    if (perSheet.sheets[0].execution.actual !== 'python' || perSheet.sheets[1].execution.actual !== 'sql')
      throw new Error('snapshot omitted per-sheet execution provenance');
    if (restoredSheetExecution({v:3,execution:{actual:'sql'}},{}) !== null)
      throw new Error("v3 restore relabelled an unknown sheet with the turn's final backend");
    if (restoredSheetExecution({v:2,execution:{actual:'python'}},{}).actual !== 'python')
      throw new Error('legacy snapshots lost their turn-level execution fallback');

    const hugeRows = Array.from({length:500}, (_, index) => [index, 'x'.repeat(5000)]);
    BOOK = [
      {id:'large', cls:'deriv', name:'combined', cols:['id','payload'], rows:hugeRows,
       sql:'SELECT * FROM input', python:'combined = input.for_each(...)', execution:{actual:'python',verified:false}},
      {id:'answer', cls:'deriv', name:'result', cols:['total'], rows:[[500]], result:true,
       sql:'SELECT COUNT(*) FROM combined', python:'result = combined.reduce(...)', execution:{actual:'python',verified:false}},
    ];
    const large = convSnapshot(), compacted = compactConvSnapshot(large);
    if (!compacted || conversationStateBytes(compacted) > MAX_CONVERSATION_STATE_BYTES)
      throw new Error('large snapshots were discarded instead of compacted below the server limit');
    if (!compacted.compacted || compacted.sheets[0].rows.length >= large.sheets[0].rows.length)
      throw new Error('large derived rows were not trimmed');
    if (!compacted.sheets[0].python || !compacted.sheets[1].sql || compacted.sheets[1].rows[0][0] !== 500)
      throw new Error('snapshot compaction discarded source or the scalar result');

    const safeAnalysis = {analysis_id:'a_'+'1'.repeat(32), slug:'total_sales', revision:2};
    if (!analysisHeading(safeAnalysis).includes('loadAnalysis'))
      throw new Error('a valid analysis revision did not produce a workbook link');
    const unsafeHeading = analysisHeading({analysis_id:"x');alert(1)//", slug:'total_sales', revision:2});
    if (unsafeHeading.includes('loadAnalysis') || unsafeHeading.includes('alert(1)'))
      throw new Error('an invalid stored analysis id reached an inline workbook handler');
    const linkedReply = conv2html('See [source](https://example.com/data) and **verify**.');
    if (!linkedReply.includes('<a href="https://example.com/data"') || !linkedReply.includes('<strong>verify</strong>'))
      throw new Error('shared turn renderer flattened answer links or emphasis');
    if (conv2html('[bad](javascript:alert(1))').includes('<a '))
      throw new Error('shared turn renderer accepted an unsafe link');

    paint = () => {}; saveConvState = () => {};
    let posted = null;
    window.ensureToken = async () => 'token';
    fetch = async (url, options) => { posted = JSON.parse(options.body); return {ok:true,status:200,json:async()=>({name:'sku'})}; };
    BOOK = [{id:'m2', cls:'master', name:'sku', cols:['sku','score'], rows:[[0,0]], saved:false, dirty:true}];
    await autosaveRefs();
    if (!posted || posted.rows.length !== 1 || posted.rows[0][0] !== 0 || posted.rows[0][1] !== 0)
      throw new Error('autosave did not preserve numeric zero');
    if (!BOOK[0].saved || BOOK[0].dirty) throw new Error('successful autosave did not settle state');

    posted = null;
    BOOK = [{id:'m-clear', cls:'master', name:'sku', cols:['sku'], rows:[], saved:true, dirty:true}];
    await autosaveRefs();
    if (!posted || posted.columns.length !== 1 || posted.rows.length !== 0)
      throw new Error('clearing a saved reference left its stale server copy in use');

    fetch = async () => ({ok:false,status:400,json:async()=>({error:'duplicate sku key: A'})});
    BOOK = [{id:'m3', cls:'master', name:'sku', cols:['sku','category'], rows:[['A','x']], saved:false, dirty:true}];
    let rejected = false;
    try { await autosaveRefs(); } catch (error) { rejected = /duplicate sku key/.test(String(error.message)); }
    if (!rejected) throw new Error('autosave swallowed a server validation failure');
    if (BOOK[0].saved || !BOOK[0].dirty) throw new Error('failed autosave falsely marked reference clean');

    // Turn transition: a follow-up data turn must retire the PRIOR turn's stale steps, never show a stale/fresh
    // mix (regression: an Apparel total was grafted onto the previous "in France" turn's filtered/total steps).
    VIEWS = []; RUN = 7; AUTO = true;
    BOOK = [{id:'d1', cls:'deriv', name:'combined', stale:true},
            {id:'d2', cls:'deriv', name:'filtered', stale:true, sql:"SELECT * FROM x WHERE country='France'"},
            {id:'d3', cls:'deriv', name:'total', stale:true, result:true, rows:[[970]]}];
    ACTIVE = 'd3';
    appendView({op:'group_agg', columns:['sum'], rows:[[892]], sql:'SELECT SUM("amount") FROM "filtered"'});
    if (BOOK.some(s => s.stale)) throw new Error('a fresh derivation must retire the prior turn stale steps');
    if (BOOK.filter(s => s.cls === 'deriv').length !== 1) throw new Error('stale prior-turn steps were left showing');

    // A viewless data result path: dropStale (what onResult now calls when no fresh derivation exists) retires stale.
    BOOK = [{id:'s1', cls:'deriv', name:'filtered', stale:true, sql:"WHERE country='France'"},
            {id:'s2', cls:'deriv', name:'total', stale:true, result:true, rows:[[970]]}];
    ACTIVE = 's2';
    dropStale();
    if (BOOK.some(s => s.stale)) throw new Error('a viewless data result must retire the prior turn stale steps');

    // Completeness: a typed-AST own-data answer (sql + answer, no views stack) is surfaced as ONE reasoning step,
    // so the SQL and result are visible (regression: reference-join answers showed no steps/views/SQL at all).
    VIEWS = []; RUN = 9; AUTO = true; J = null;
    BOOK = [{id:'in', cls:'input', name:'orders', cols:[], rows:[]}];
    renderTurnFromHTTP({traces:[{question:'total amount for apparel', engine:{
      sql:'SELECT SUM("orders"."amount") FROM "orders" JOIN "ordered" ON "orders"."ordered"="ordered"."ordered" WHERE "ordered"."category"=\\'Apparel\\'',
      answer:{columns:['sum'], rows:[[892]]}}}]});
    const step = BOOK.find(s => s.cls === 'deriv');
    if (!step) throw new Error('a typed-AST answer (sql+answer, no views) must surface a reasoning step');
    if (!/SUM/.test(step.sql) || step.rows[0][0] !== 892) throw new Error('the surfaced step lost the SQL or the result');

    const currencyClarify = clarifyFallbackText({
      reason:'the selected computation does not convert mixed source currencies to EUR',
      proposed:'total order amount in US dollars'
    });
    if (!currencyClarify.includes('does not convert') || !currencyClarify.includes('Try'))
      throw new Error('typed currency reason or answerable proposal was hidden by the browser fallback');
    const ordinaryClarify = clarifyFallbackText({proposed:'total revenue by country'});
    if (!ordinaryClarify.includes('Did you mean'))
      throw new Error('ordinary clarification lost its existing proposal fallback');
    // Snapshot validation is atomic; malformed legacy rows must not erase the
    // current workbook or masquerade as a valid empty answer.
    const beforeRestore=JSON.stringify(BOOK);
    if(restoreConvState({v:3,turns:[{q:'notices',reply:'three rows'}],sheets:[
      {id:'bad',cls:'deriv',cols:['notice'],rows:{1:[7]}}
    ]}))throw new Error('malformed legacy snapshot was treated as valid');
    if(JSON.stringify(BOOK)!==beforeRestore)throw new Error('failed restore partially mutated the workbook');

    // A restored snapshot names each derived sheet as the rail does now: a projection saved before it had a
    // name showed "purch result" (complex-promotions-xlsx, 2026-10-01); other saved descriptions stand.
    const keepBook = BOOK, keepPaint = paint;
    BOOK = []; paint = () => {};
    if(!restoreConvState({v:3,turns:[{q:'pairs',reply:'two gaps'}],sheets:[
      {id:'p',cls:'deriv',op:'select',name:'purch result',desc:'purch result',cols:['customer_name'],rows:[['Cara']]},
      {id:'f',cls:'deriv',op:'filter',name:'filtered',desc:'Kept only the rows where city is Paris.',cols:['city'],rows:[['Paris']]},
      {id:'i',cls:'input',name:'orders',cols:['city'],rows:[['Paris']]},
    ]}))throw new Error('a valid snapshot was not restored');
    const restoredNames = BOOK.map(sheet => [sheet.name, sheet.desc]);
    if (JSON.stringify(restoredNames) !== JSON.stringify([
      ['selected columns', 'Kept the columns the answer needs.'],
      ['filtered', 'Kept only the rows where city is Paris.'],
      ['orders', ''],
    ])) throw new Error('restored sheet names: ' + JSON.stringify(restoredNames));
    BOOK = keepBook; paint = keepPaint;

    // The /chat body's reply replaces a streamed prefix, and a settled turn is saved again with it
    // (a first reply was saved as "Your rest", 2026-10-01). An empty body reply changes nothing.
    const keepSave = saveConvState, keepRender = renderRail;
    let resaved = 0; saveConvState = () => { resaved++; }; renderRail = () => {};
    REPLY = 'Your rest'; CONV = 'Your rest'; SETTLED = true; FAILMSG = null;
    takeBodyReply('Your restaurant total comes to 9,600.');
    if (REPLY !== 'Your restaurant total comes to 9,600.' || CONV !== REPLY || resaved !== 1)
      throw new Error('the body reply did not replace a streamed prefix');
    takeBodyReply(''); takeBodyReply(null); takeBodyReply('Your restaurant total comes to 9,600.');
    if (CONV !== 'Your restaurant total comes to 9,600.' || resaved !== 1)
      throw new Error('an empty or unchanged body reply saved the turn again');
    SETTLED = false; REPLY = 'Your rest'; CONV = null;
    takeBodyReply('Your restaurant total comes to 9,600.');
    if (REPLY !== 'Your restaurant total comes to 9,600.' || CONV !== null || resaved !== 1)
      throw new Error('an unsettled turn was saved before it finished');
    saveConvState = keepSave; renderRail = keepRender;
    __finish();
  } catch (error) { __finish(error); }
}());`;

// Execute as three distinct classic scripts, matching the browser's real loading model.
vm.runInContext(fs.readFileSync(path.join(__dirname,'..','public','lib','result-wire.js'),'utf8'),context);
vm.runInContext(turnRendererSource, context, {filename: 'turn-renderer.js'});

// The rail's step presentation is one shared component (turn-renderer.js): the web workspace and the
// Google Sheets add-on's sidebar render the same sentence, lineage and backend for an engine view.
{
  const R = context.PrereasonerTurnRenderer;
  const views = [
    {name: 'france_orders', op: 'filter', label: "where country = 'France'", inputs: ['c_0123456789abcdef0123456789abcdef'],
      execution: {actual: 'python', verified: false}},
    {name: 'france_total', op: 'group_agg', label: 'SUM(amount)', sql: 'SELECT SUM(amount) FROM france_orders',
      inputs: ['france_orders'], execution: {actual: 'verify', verified: true}, is_output: true}
  ];
  const steps = R.stepsFromViews(views, {sourceName: 'orders'});
  assert.deepStrictEqual(JSON.parse(JSON.stringify(steps.map(step => [step.name, step.description, step.lineage, step.ran]))), [
    ['filtered', 'Kept only the rows where country is France.', 'orders', 'py'],
    ['total', 'Added up the values to get the total.', 'filtered', 'both']
  ]);
  assert.strictEqual(R.stepStatus(views[0]), 'Filtering the rows…');
  // A filter's sentence reads every comparison in its label. The engine's month comparison names the
  // month: "total transfers signed in August" read "where signed = 8" (Chrome gate, 2026-10-02).
  const filtered = label => R.stepDescription({op: 'filter', label: label});
  assert.strictEqual(filtered('where month(signed) = 8'), 'Kept only the rows where signed is in August.');
  assert.strictEqual(filtered('where month(signed) >= 7 and month(signed) <= 9'),
    'Kept only the rows where signed is in or after July and signed is in or before September.');
  assert.strictEqual(filtered("where country = 'France' and quarter = 'Q1'"),
    'Kept only the rows where country is France and quarter is Q1.');
  assert.strictEqual(filtered('where amount >= 100'), 'Kept only the rows where amount is at least 100.');
  assert.strictEqual(filtered("where city <> 'Paris'"), 'Kept only the rows where city is not Paris.');
  assert.strictEqual(filtered("where contestant_name LIKE '%al%'"), 'Kept only the rows where contestant_name contains al.');
  const link = R.renderStepLink(steps[1], 1, {href: 'https://chat.prereasoner.com/reason/c_1'});
  assert(link.startsWith('<a class="steplink" href="https://chat.prereasoner.com/reason/c_1"'), link);
  assert(link.includes('<span class=idx>2</span>') && link.includes(' · from filtered') && link.includes('PY = SQL'), link);
  const legacyLink = R.renderStepLink({title: 'Legacy step', operation: 'filter'}, 0);
  assert(legacyLink.includes('Legacy step'), legacyLink);
  assert.strictEqual(R.renderAsks(['total amount in France']), '<div class=cotask>read as &ldquo;total amount in France&rdquo;</div>');
  // A decomposition sends the same question twice (probe, then proposal): it is read once.
  assert.strictEqual(R.renderAsks(['top customers by spend', 'top customers by spend']),
    '<div class=cotask>read as &ldquo;top customers by spend&rdquo;</div>');
  // The user's own words are not a reading: "read as" the typed question said nothing (2026-10-04).
  assert.strictEqual(R.renderAsks(['how many orders in Paris'], 'How many orders in  Paris?'), '');
  assert.strictEqual(R.renderAsks(['total amount in Belgium in US dollars'], 'how about Belgium?'),
    '<div class=cotask>read as &ldquo;total amount in Belgium in US dollars&rdquo;</div>');
  // Every per-row calculation is a `convert` step. Only one whose columns the server traced to the
  // exchange-rate reference is described as a currency conversion (payment-commissions, 2026-10-01).
  const commission = {op: 'convert', label: 'calculated', columns: ['payments__amount', 'aggregate_operand_1'],
    column_provenance: [{kind: 'input', source: 'upload'}, {kind: 'derived', source: 'Prereasoner'}]};
  const fx = {op: 'convert', label: 'calculated', columns: ['orders__amount', 'exchange_rate__rate_to_usd'],
    column_provenance: [{kind: 'input', source: 'upload'}, {kind: 'reference', source: 'European Central Bank'}]};
  assert.strictEqual(R.stepDescription(commission), 'Calculated a new value for each row from the columns before it.');
  assert.strictEqual(R.stepDescription({op: 'join', label: 'combined'}), 'Combined your tables into one table.');
  assert.strictEqual(R.stepDescription({op: 'join', label: 'join orders + tier'}), 'Combined orders and tier into one table.');
  // One extreme is named as such; only a step computing both is "extremes".
  assert.deepStrictEqual(['SELECT MAX(cost) FROM a', 'SELECT MIN(cost) FROM a', 'SELECT MIN(cost), MAX(cost) FROM a']
    .map(sql => R.stepLabel({op: 'group_agg', sql: sql, label: 'total'})), ['highest', 'lowest', 'extremes']);
  assert(/ECB reference rate/.test(R.stepDescription(fx)), R.stepDescription(fx));
  // A projection is named for what it does, never by the engine's label, which carries the
  // decomposition leaf's id ("purch result", complex-promotions-xlsx, 2026-10-01).
  const projection = {op: 'select', label: 'purch result'};
  assert.strictEqual(R.stepLabel(projection), 'selected columns');
  assert.strictEqual(R.stepDescription(projection), 'Kept the columns the answer needs.');
}
vm.runInContext(referenceSource, context, {filename: 'workbook-reference.js'});
vm.runInContext(conversationSource, context, {filename: 'workbook-conversations.js'});
vm.runInContext(workbookSource + checks, context, {filename: 'workbook.js'});

done.then(() => console.log('workbook reference state: passed'))
  .catch(error => { console.error(error.stack || error); process.exitCode = 1; });
