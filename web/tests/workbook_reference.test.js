// Dependency-free regression checks for reference-data state in the production classic script.
// Run: node web/tests/workbook_reference.test.js
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, '..', 'public', 'lib', 'workbook.js'), 'utf8');
let finish;
const done = new Promise((resolve, reject) => { finish = error => error ? reject(error) : resolve(); });
const storage = new Map();
const context = {
  console,
  setTimeout,
  clearTimeout,
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
    if (referenceKey('products', ['sku', 'category']) !== 'sku') throw new Error('reference identity ignored its join key');
    const compact = referenceRows({rows: [[0, 'zero'], ['', ''], [null, null]]});
    if (compact.length !== 1 || compact[0][0] !== 0) throw new Error('referenceRows discarded or changed zero');

    BOOK = [{id:'m1', cls:'master', name:'sku', cols:['sku','category'], rows:[['A','Apparel']],
             saved:true, dirty:true, cellAI:new Set(['0,1'])}];
    CHAT = [{q:'prior', reply:'answer'}]; SETTLED=false;
    const snapshot = convSnapshot();
    const saved = snapshot.sheets[0];
    if (!saved.dirty) throw new Error('dirty state was serialized as clean');
    if (!saved.cellAI || saved.cellAI[0] !== '0,1') throw new Error('cell provenance was not serialized');

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
    __finish();
  } catch (error) { __finish(error); }
}());`;

vm.runInContext(source + checks, context, {filename: 'workbook.js'});

done.then(() => console.log('workbook reference state: 9 passed, 0 failed'))
  .catch(error => { console.error(error.stack || error); process.exitCode = 1; });
