// shared.js — helpers shared by every page. CLASSIC script (not a module) so the pages'
// inline <script> blocks can call these directly: load it with <script src="lib/shared.js">
// BEFORE any inline script that uses it.

// Where the pages talk to the engine. '' = same origin, i.e. the /api/** rewrite in
// firebase.json (Firebase Hosting -> the "prereasoner-api" Cloud Run service).
// LOCAL DEV: the Firebase Hosting emulator proxies /api/** to the DEPLOYED Cloud Run
// service, not to localhost — so to test against a locally running engine, run once
// in the browser console:  localStorage.setItem('pr_api_base','http://localhost:8080')
// (the engine sends permissive CORS). Remove the key to return to same-origin.
// SECURITY: the override is honored ONLY on localhost. In production the destination is always
// same-origin, so a signed-in user's Firebase ID token can never be redirected to an off-origin
// host by a stray localStorage write (mirrors the dev-host gate in firebase-init.js / config.js).
const API_BASE = ((location.hostname === 'localhost' || location.hostname === '127.0.0.1')
  && localStorage.getItem('pr_api_base')) || '';

// sessionStorage keys — the only state handed between pages (tables, question, clarify payload).
const SS = {
  TABLES: 'pr_world_tables',          // JSON [{name,data}] — every attached sheet, CSV text
  CSV: 'pr_world_csv',                // legacy single-table fallback (first sheet's CSV)
  NAME: 'pr_world_name',              // legacy single-table fallback (first sheet's name)
  Q: 'pr_world_q',                    // the question being asked
  PENDING_SHEETS: 'pr_pending_sheets',// JSON [{name,data}] — Google Sheets import -> home
  PENDING_Q: 'pr_pending_q',          // the typed prompt preserved across the Sheets picker round-trip
  RETURN_TO: 'pr_return_to'           // home route (/, /sheets, /excel, /csv) the picker returns to
};

// HTML-escape for TEXT NODES (& < >). NOT safe inside an attribute value — use escAttr there.
function esc(s){return (s==null?'':s+'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
// HTML-escape for a double-quoted ATTRIBUTE value: also neutralize " (and ') so a crafted cell/name
// can't break out of e.g. title="…" and inject an event handler (attribute-injection XSS).
function escAttr(s){return esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

// Quote-aware CSV parse: "Dominguez, Mcmillan and Donovan" is ONE cell.
function parseCSV(t){
  const rows=[];let row=[],cur='',q=false;t=(t||'').replace(/\r/g,'');
  for(let i=0;i<t.length;i++){const ch=t[i];
    if(q){ if(ch==='"'){ if(t[i+1]==='"'){cur+='"';i++;} else q=false; } else cur+=ch; }
    else if(ch==='"')q=true;
    else if(ch===','){row.push(cur);cur='';}
    else if(ch==='\n'){row.push(cur);if(row.some(x=>x.trim()!==''))rows.push(row);row=[];cur='';}
    else cur+=ch;}
  row.push(cur);if(row.some(x=>x.trim()!==''))rows.push(row);
  return {cols:(rows[0]||[]).map(s=>s.trim()),rows:rows.slice(1)};}

// The SQL table name the server derives from a sheet's file name (for token colouring).
function slug(n,i){return ((n||'').replace(/\.csv$/i,'').replace(/[^0-9A-Za-z_]+/g,'_').replace(/^_+|_+$/g,'').toLowerCase())||('t'+i);}

// Split SQL on whitespace but keep "quoted identifiers" / 'literals' (which may contain spaces).
function sqlTokens(sql){
  const toks=[];let cur='',q=null;
  for(const ch of (sql||'')){ if(q){cur+=ch;if(ch===q)q=null;} else if(ch==='"'||ch==="'"){q=ch;cur+=ch;} else if(/\s/.test(ch)){if(cur){toks.push(cur);cur='';}} else cur+=ch; }
  if(cur)toks.push(cur);return toks;
}

// Human labels for the engine's view ops (the decomposition breadcrumb).
const OPLBL={filter:'filter',time_filter:'time filter',having:'having',join:'join',world_join:'world',world_filter:'world filter',group_agg:'aggregate',yoy:'YoY growth',running:'running total',divide:'ratio',share:'share',topn:'top-N',sort:'sort'};
function oplabel(op){return OPLBL[op]||op;}

// UI constants: the transport play/pause icons and the small "reasoning" spinner.
const PLAY='<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="6,4 6,20 20,12"/></svg>';
const PAUSE='<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14"/><rect x="14" y="5" width="4" height="14"/></svg>';
const SPINNER='<span class=spin></span>';
