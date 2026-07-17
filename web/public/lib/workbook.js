// workbook.js — the read-only WORKBOOK + CHAT shared by /reason and /world. CLASSIC script: load
// AFTER lib/shared.js and after an inline <script> that sets window.WB_CONFIG. The page's module
// block (firebase-init.js) calls run() once signed in, or fail(msg) when sign-in fails.
//
// WB_CONFIG: { endpoint:   engine route ('/api/reason' | '/api/world'),
//              runningMsg: status line while decomposing,
//              warmupMsg:  status line on the cold-start retry path,
//              demoQ, demoTables: what to show when arriving with an empty session }
//
// Layout: the spreadsheet left (sheet card + Excel-style bottom tabs), the chat rail right.
// Sheet order = reading order: green input sheets (the user's tables — the AI never writes
// here), grey reference sheets (lookups the resolution used; collapsed), then blue derivation
// sheets (one per reasoning step, human-named). The rail is a chat: each question is a turn;
// a follow-up re-runs the workbook on the same attached tables.
const WB = Object.assign({
  endpoint: '/api/reason',
  runningMsg: 'Decomposing into steps…',
  warmupMsg: 'Warming up the model (cold start)…',
  demoQ: 'top 3 cities by total amount',
  demoTables: [{name:'customers.csv',data:'customer_id,name,city\n1,Ada,Paris\n2,Lin,Lyon\n3,Bo,Paris\n4,Sam,Berlin\n5,Mai,Lyon'},
               {name:'orders.csv',data:'order_id,customer_id,amount,status\n101,1,120,shipped\n102,1,60,shipped\n103,2,80,shipped\n104,2,160,shipped\n105,3,90,shipped\n106,4,40,pending\n107,5,200,shipped\n108,5,30,shipped'}],
}, window.WB_CONFIG || {});

const $=id=>document.getElementById(id);
function getSheets(){
  try{const t=sessionStorage.getItem(SS.TABLES);if(t){const a=JSON.parse(t);if(a&&a.length)return a;}}catch(_){}
  return WB.demoTables;
}
function getQ(){try{const q=sessionStorage.getItem(SS.Q);if(q)return q;}catch(_){}return WB.demoQ;}
const SHEETS=getSheets();
const TABNAMES=SHEETS.map((s,i)=>slug(s.name,i));
const MAX_RENDER_ROWS=500;

/* ---------------- state ----------------
   BOOK = sheet metas {id, cls:'input'|'deriv'|'ref', name(label, human), cols, rows, sql, result}.
   CHAT = finished turns [{q, html}] (minimal: the answer line); the CURRENT turn renders live.
   RUN guards every async callback so a superseded run (follow-up question) can't paint. */
let BOOK=[],ACTIVE=null,AUTO=true;
let CHAT=[],question=getQ();
let J=null,VIEWS=[],RESOLVES=[],SETTLED=false,DONE=false,UNSUB=null,doneTimer=null,STATUS='Analyzing input…',FAILMSG=null,RUN=0;
let SEEN=new Set(),SEEN_R=new Set();
let CONV=null,CONVPENDING=false,CONVPROP=null;   // conversational fallback: a clarify / non-data question answered IN the rail (no redirect)
let PRESENT=false;                               // present mode: a REAL answer, phrased humanly -> Sonnet presents it in words, derivation stays in the panel
let HTTPJ=null;                                  // the atomic HTTP body (result+present+sql) — the race-free answer source for present
// ---- orchestrated (Sonnet front-door) mode: WB.chat routes each turn through /chat (Sonnet + engine-MCP),
// which resolves context ("How about germany?" -> "total amount in Germany") and can make several engine
// calls per turn. Off by default -> the direct /api/reason path above is byte-identical. ----
// ORCH default = WB.chat, overridable per-browser with localStorage 'pr_chat' ('1' on / '0' off) so the
// orchestrated path can be exercised on the live site before it becomes the default. No redeploy needed.
const ORCH = (()=>{ try{ const o=localStorage.getItem('pr_chat'); if(o==='1')return true; if(o==='0')return false; }catch(_){} return !!WB.chat; })();
const CHAT_ENDPOINT = API_BASE + (WB.chatEndpoint || '/chat');
let HISTORY=[];                                  // lean cross-turn transcript for the orchestrator [{role,content}]
let CALLS=[],SEEN_CALL=new Set(),REPLY=null,callSubs=[];   // this turn's announced engine calls + their trace subs

function sheetById(id){return BOOK.find(s=>s.id===id);}
function addSheet(m){ BOOK.push(m); if(m.cls!=='ref'&&AUTO) ACTIVE=m.id; if(m.cls==='ref'&&!ACTIVE) ACTIVE=m.id; paint(); }

/* ---------------- rendering: the sheet ---------------- */
function isNum(v){return v!==''&&v!=null&&/^-?\$?[\d,]*\.?\d+%?$/.test(String(v).trim());}
function fmt(v){ if(typeof v==='number'&&!Number.isInteger(v)) return (Math.round(v*1000)/1000).toString(); return v==null?'':String(v); }
function renderGrid(m){
  const cols=m.cols||[],rows=(m.rows||[]);
  const numeric=cols.map((_,ci)=>rows.length>0&&rows.every(r=>r[ci]===''||r[ci]==null||isNum(r[ci])));
  const shown=rows.slice(0,MAX_RENDER_ROWS);
  let h='<div class=sheetscroll><table class="wb'+(m.result?' result':'')+'"><thead><tr><th class=rn></th>';
  for(let ci=0;ci<cols.length;ci++) h+='<th'+(numeric[ci]?' class=n':'')+'>'+esc(cols[ci])+'</th>';
  h+='</tr></thead><tbody>';
  for(let ri=0;ri<shown.length;ri++){ h+='<tr><td class=rn>'+(ri+1)+'</td>';
    for(let ci=0;ci<cols.length;ci++) h+='<td'+(numeric[ci]?' class=n':'')+'>'+esc(fmt(shown[ri][ci]))+'</td>';
    h+='</tr>'; }
  if(!rows.length) h+='<tr><td class=rn>1</td><td colspan='+Math.max(1,cols.length)+' style="color:#9a93b5">no rows</td></tr>';
  h+='</tbody></table></div>';
  if(rows.length>MAX_RENDER_ROWS) h+='<div class=capnote>showing the first '+MAX_RENDER_ROWS+' of '+rows.length+' rows</div>';
  return h;
}
function tokCls(tk){const u=tk.toUpperCase();
  if(['SELECT','SUM','COUNT','AVG','MIN','MAX','FROM','JOIN','LEFT','ON','WHERE','AND','OR','GROUP','BY','ORDER','LIMIT','DESC','ASC','AS','IS','NULL','NULLIF'].includes(u))return 'kw';
  if(/^'/.test(tk))return 'lit';
  const m=tk.match(/^"([^"]+)"$/); if(m&&TABNAMES.includes(m[1].toLowerCase()))return 'tbl';
  if(/world|meaning/i.test(tk))return 'world';
  return '';}
const KINDLBL={input:'Your data',deriv:'AI derived',ref:'Reference'};
function renderSheet(){
  const m=sheetById(ACTIVE);
  if(!m){ $('sheetcard').innerHTML='<div class=sheetmsg id=sheetmsg>'+(FAILMSG?'&#9888; '+esc(FAILMSG):'<span class=spin></span> '+esc(STATUS))+'</div>'; return; }
  let h='<div class="bandbar band-'+m.cls+'"></div><div class=sheetband>'
    +'<span class="dot '+m.cls+'"></span><span class=snm>'+esc(m.result?'Result':m.name)+'</span>'
    +'<span class="skind '+m.cls+'">'+(m.result?esc(m.name):KINDLBL[m.cls])+'</span>'
    +(m.sql?'<span class=spacer></span><button class=sqlbtn onclick=toggleSql()>SQL</button>':'')
    +'</div>';
  if(m.sql) h+='<div class=sqlrow id=sqlrow><div class=vsql>'+sqlTokens(m.sql).map(tk=>'<span class="vtok '+tokCls(tk)+'">'+esc(tk)+'</span>').join('')+'</div></div>';
  h+=renderGrid(m);
  $('sheetcard').innerHTML=h;
}
function toggleSql(){const r=$('sqlrow'); if(r) r.classList.toggle('open');}
function tabTxt(s){ const t=s.result?'Result':s.name; return t.length>26?t.slice(0,24)+'…':t; }
function renderTabs(){
  // Reading order: your data -> the reference lookups it used -> the derivation steps.
  const inputs=BOOK.filter(s=>s.cls==='input'), refs=BOOK.filter(s=>s.cls==='ref'), derivs=BOOK.filter(s=>s.cls==='deriv');
  const tab=s=>'<button class="wtab'+(s.id===ACTIVE?' active':'')+'" onclick="pick(\''+s.id+'\')"><span class="dot '+s.cls+'"></span>'+esc(tabTxt(s))+'</button>';
  $('tabstrip').innerHTML=inputs.map(tab).join('')+refs.map(tab).join('')+derivs.map(tab).join('');
  const a=document.querySelector('.wtab.active'); if(a&&a.scrollIntoView) a.scrollIntoView({inline:'nearest',block:'nearest'});
  updateTabArrows();
}
function pick(id){ AUTO=false; ACTIVE=id; paint(); }
// Google-Sheets-style paging for the tab strip (its native scrollbar is hidden).
function scrollTabs(d){ const t=$('tabstrip'); if(t) t.scrollBy({left:d*220,behavior:'smooth'}); }
function updateTabArrows(){
  const t=$('tabstrip'), l=$('tabL'), r=$('tabR'); if(!t||!l||!r)return;
  const over=t.scrollWidth>t.clientWidth+2;
  l.disabled=!over||t.scrollLeft<=2;
  r.disabled=!over||t.scrollLeft+t.clientWidth>=t.scrollWidth-2;
}

/* ---------------- rendering: the chat rail ---------------- */
function resultSummary(){
  const r=(J&&J.result)||null; if(!r||!r.rows||!r.rows.length) return null;
  if(r.rows.length===1&&r.columns&&r.columns.length===1) return {k:r.columns[0],v:fmt(r.rows[0][0]),big:true};
  if(r.rows.length===1) return {k:'result',v:r.columns.map((c,i)=>c+': '+fmt(r.rows[0][i])).join('  ·  '),big:false};
  return {k:'result',v:r.rows.length+' rows — see the Result sheet',big:false};
}
function conv2html(t){ return esc(String(t||'')).replace(/\n/g,'<br>'); }
function turnHtml(){                                          // the CURRENT (live) turn's assistant block
  if(FAILMSG) return '<div class=failbox>'+esc(FAILMSG)+'<br><button class=retry onclick=location.reload()>Retry</button></div>';
  if(CONV){ let h='<div class=convmsg>'+conv2html(CONV)+'</div>';   // a clarify / non-data question, answered right here
    if(CONVPROP) h+='<div class=convrun><button onclick="runProposed()">Run &ldquo;'+esc(CONVPROP)+'&rdquo;</button></div>';
    if(PRESENT||ORCH){ const derivs=BOOK.filter(s=>s.cls==='deriv');   // keep the derivation reachable from the rail (it lives in the panel)
      if(derivs.length) h+='<div class=steps>'+derivs.map((s,i)=>'<button class="steplink'+(s.id===ACTIVE?' on':'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>'+(i+1)+'</span><span class=stx>'+esc(s.name)+'</span></button>').join('')+'</div>'; }
    return h; }
  if(CONVPENDING) return '<div class=statusline><span class=spin></span> '+esc(STATUS)+'</div>';
  let h='<div class=statusline>'+(SETTLED?'&#10003; ':'<span class=spin></span> ')+esc(STATUS)+'</div>';
  const refs=BOOK.filter(s=>s.cls==='ref'), derivs=BOOK.filter(s=>s.cls==='deriv');
  if(refs.length)
    h+='<div class=steps>'+refs.map(s=>'<button class="steplink refl'+(s.id===ACTIVE?' on':'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>&#9707;</span><span class=stx>looked up '+esc(s.name)+'</span></button>').join('')+'</div>';
  if(derivs.length)
    h+='<div class=steps>'+derivs.map((s,i)=>'<button class="steplink'+(s.id===ACTIVE?' on':'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>'+(i+1)+'</span><span class=stx>'+esc(s.name)+'</span></button>').join('')+'</div>';
  if(SETTLED){ const rs=resultSummary();
    if(rs) h+='<div class=resultline><div class=rk>'+esc(rs.k)+'</div><div class="rv'+(rs.big?'':' small')+'">'+esc(rs.v)+'</div></div>';
  }
  ((J&&J.warnings)||[]).forEach(w=>{ h+='<div class=warn>&#9888; '+esc(String(w))+'</div>'; });
  return h;
}
function archiveTurn(){                                       // freeze the finished turn to a MINIMAL line (no dead links)
  let h;
  if(FAILMSG) h='<div class=statusline>&#9888; '+esc(FAILMSG)+'</div>';
  else if(CONV) h='<div class=convmsg>'+conv2html(CONV)+'</div>';   // freeze the conversational reply (drop the run button)
  else{ const rs=resultSummary(); const n=BOOK.filter(s=>s.cls==='deriv').length;
    h='<div class=statusline>&#10003; '+esc(rs?(rs.k==='result'?rs.v:rs.k+': '+rs.v):('answered in '+n+' step'+(n===1?'':'s')))+'</div>'; }
  CHAT.push({q:question, html:h});
}
function renderRail(){
  let h='';
  for(const t of CHAT) h+='<div class="turn user"><div class=msg>'+esc(t.q)+'</div></div><div class="turn ai">'+t.html+'</div>';
  h+='<div class="turn user"><div class=msg>'+esc(question)+'</div></div><div class="turn ai">'+turnHtml()+'</div>';
  const sc=$('rail'); sc.innerHTML=h; sc.scrollTop=sc.scrollHeight;
  // A follow-up needs the conversation_id (arrives with the response), so a NEW conversation keeps
  // send disabled until it lands — otherwise the follow-up would orphan into a fresh conversation.
  const btn=$('chatsend'); if(btn) btn.disabled=!((SETTLED&&(convId()||ORCH))||FAILMSG);   // ORCH keeps history client-side (no server conversation_id gate)
}
function paint(){ renderTabs(); renderSheet(); renderRail(); }
function fail(m){ FAILMSG=String(m||'something went wrong'); STATUS='failed'; paint(); }

/* ---------------- the run (streaming + fallbacks) ---------------- */
const ENDPOINT=API_BASE+WB.endpoint;
function seedInputs(){
  SHEETS.forEach((s,i)=>{ const p=parseCSV(s.data);
    addSheet({id:'in'+i, cls:'input', name:s.name, cols:p.cols, rows:p.rows}); });
  ACTIVE=BOOK.length?BOOK[0].id:null; paint();
}
// This run just produced its OWN first sheet -> retire the previous turn's derivation/reference sheets that
// resetRun kept around (so a conversational follow-up could still show them). A data query replaces them.
function dropStale(){
  if(!BOOK.some(s=>s.stale))return;
  BOOK=BOOK.filter(s=>!s.stale);
  if(!BOOK.some(s=>s.id===ACTIVE)) ACTIVE=BOOK.length?BOOK[0].id:null;
}
function appendView(v){
  dropStale();
  VIEWS.push(v); J=J||{}; J.views=VIEWS; if(v.sql&&!J.sql)J.sql=v.sql;
  const label=v.label||oplabel(v.op);                        // HUMAN name ("join orders + customers"), never v1/step_1
  STATUS=label+'…';
  addSheet({id:'v'+RUN+'_'+VIEWS.length, cls:'deriv', name:label, cols:v.columns||[], rows:v.rows||[], sql:v.sql||''});
}
function appendResolve(r){
  dropStale();
  RESOLVES.push(r);
  STATUS='Resolving '+(r.column||'')+'…';
  if(!r.unconnected&&r.columns)
    addSheet({id:'r'+RUN+'_'+RESOLVES.length, cls:'ref', name:(r.wtable||'world')+' (wikipedia)', cols:r.columns||[], rows:r.rows||[]});
  else paint();
}
function markDone(){ if(SETTLED)return; DONE=true; clearTimeout(doneTimer); doneTimer=setTimeout(finalize,400); }
function finalize(){
  if(SETTLED)return; settle();
  if(!VIEWS.length){                                          // delegated (no composition) — synthesize the single result sheet
    const r=(J&&J.result)||{};
    appendView({name:'result',op:'group_agg',label:'result',sql:(J&&J.sql)||'',columns:r.columns||[],rows:r.rows||[]});
  } else if(J&&J.result&&Array.isArray(J.result.rows)){       // the last view's table is the authoritative final answer
    const lv=VIEWS[VIEWS.length-1], lm=BOOK.filter(s=>s.cls==='deriv').pop();
    lv.columns=J.result.columns||lv.columns; lv.rows=J.result.rows;   // an empty result ([]) legitimately shows "no rows"
    if(lm){ lm.cols=lv.columns; lm.rows=lv.rows; }
  }
  const last=BOOK.filter(s=>s.cls==='deriv').pop();
  if(last){ last.result=true; if(AUTO) ACTIVE=last.id; }
  const n=BOOK.filter(s=>s.cls==='deriv').length;
  STATUS='Answered in '+n+' step'+(n===1?'':'s');
  paint();
  if(PRESENT) tryPresent();                                   // real answer + human phrasing -> Sonnet presents it (derivation stays in the panel)
}
// Route a REAL answer through Sonnet to present it in words. RTDB delivers result/present/status on separate
// nodes with NO cross-node ordering guarantee, so this is called from several triggers (onPresent, finalize,
// onResult, the HTTP body) and only fires once BOTH the derivation has settled in the panel AND a concrete
// answer is in hand — otherwise it no-ops and a later trigger retries. Never sends a null answer to Sonnet.
function tryPresent(){
  if(!PRESENT||CONV||CONVPENDING||!SETTLED)return;            // not flagged / already presenting / derivation not final yet
  const ans=(J&&J.result)||(HTTPJ&&HTTPJ.result)||null;      // prefer the streamed result, fall back to the atomic HTTP body
  if(!ans||!ans.columns)return;                              // no answer in hand yet -> a later trigger will retry
  conversationalReply({question:question, present:true, answer:ans, sql:(J&&J.sql)||(HTTPJ&&HTTPJ.sql)||null});
}
function renderFromJSON(j){
  if(SETTLED)return;
  if(j.clarify||j.low_confidence){ conversationalReply(Object.assign({question:question},j)); return; }
  if(j.error){ fail(j.error); settle(); return; }
  J=j; (j.views||[]).forEach(v=>appendView(v));
  if(j.present) PRESENT=true;                                 // flag BEFORE finalize so it triggers the present reply
  DONE=true; finalize();
}
function settle(){ SETTLED=true; clearTimeout(doneTimer); if(UNSUB){try{UNSUB();}catch(_){}UNSUB=null;} renderRail(); }
// Answer a clarify / non-data question IN THE RAIL (no page redirect). Try the Sonnet fallback
// (POST /api/converse); if it isn't deployed yet or errors, degrade to a payload-based "did you mean".
async function conversationalReply(c){
  const present=!!(c&&c.present);
  settle();                                                  // stop the reasoning stream for this turn
  if(present){ PRESENT=true; }                               // present: KEEP the derivation sheets in the panel
  else{                                                      // fallback: drop THIS turn's abandoned sheets, but KEEP the previous turn's
    BOOK=BOOK.filter(s=>s.cls==='input'||s.stale);           // derivation (stale) — a meta/general question is usually ABOUT it
    if(!BOOK.some(s=>s.id===ACTIVE)){ const last=BOOK.filter(s=>s.stale).pop(); ACTIVE=(last&&last.id)||(BOOK.length?BOOK[0].id:null); }
  }
  CONV=null; CONVPROP=(c&&c.proposed)||null; CONVPENDING=true; STATUS=present?'Putting it in context…':'Thinking…'; renderRail();
  let reply=null;
  try{
    const token=await window.ensureToken();
    const body={question:c.question,
      clarify:c.clarify?{proposed:c.proposed||null,original_sql:c.original_sql||null,bindings:c.bindings||null}:null,
      error:c.error||null, tables:SHEETS, conversation_id:convId()};
    if(present){ body.answer=c.answer||((J&&J.result)||null); body.sql=c.sql||((J&&J.sql)||null); }
    const res=await fetch(API_BASE+'/api/converse',{method:'POST',
      headers:{'content-type':'application/json','Authorization':'Bearer '+token},
      body:JSON.stringify(body)});
    if(res.ok){ const j=await res.json().catch(()=>null); reply=j&&j.reply; }
  }catch(_){}
  CONVPENDING=false;
  if(present&&!reply){                                        // present + Sonnet unavailable -> degrade to the raw result with a clean header
    CONV=null; const n=BOOK.filter(s=>s.cls==='deriv').length; STATUS='Answered in '+n+' step'+(n===1?'':'s');
  } else {
    CONV=reply||clarifyFallbackText(c);
  }
  renderRail();
}
function clarifyFallbackText(c){
  const p=(c&&c.proposed)||'';
  let t = p ? ('Did you mean “'+p+'”? ') : 'I couldn’t map that to a query over your sheets. ';
  t += 'Rephrase it as a question about your data — a total, count, average, or filter — or tap a step in the trace panel to see how a value was derived.';
  return t;
}
function runProposed(){ if(!CONVPROP)return; const p=CONVPROP; archiveTurn(); question=p;
  try{ sessionStorage.setItem(SS.Q,p); }catch(_){}; resetRun(); paint(); startRun(); }

/* ---------------- orchestrated run (Sonnet front-door via /chat) ---------------- */
// One turn = one POST /chat. Sonnet (with HISTORY) resolves context + decides the engine calls; each call is
// announced on the turn's RTDB node with its REWRITTEN question, and the engine streams that call's trace
// under its own jobId (rendered by the same appendView/appendResolve as the direct path). The turn's answer
// is Sonnet's REPLY (shown in the rail); the derivation (every call's steps) stays in the panel.
async function startTurn(){
  const myRun=++RUN;
  const live=()=>RUN===myRun&&!SETTLED;
  let token;
  try{ token=await window.ensureToken(); }
  catch(e){ if(RUN===myRun) fail('sign-in required to run on your data: '+(e&&e.message||e)); return; }
  const uid=window.__uid;
  const turnId=(crypto&&crypto.randomUUID)?crypto.randomUUID():(Date.now()+'-'+Math.random().toString(36).slice(2));
  const parseBody=async r=>{ try{ if(!r)return null; const t=await r.text(); return (r.ok&&t.trim().charAt(0)==='{')?JSON.parse(t):null; }catch(_){ return null; } };
  const httpPromise=fetch(CHAT_ENDPOINT,{method:'POST',
    headers:{'content-type':'application/json','Authorization':'Bearer '+token},
    body:JSON.stringify({message:question, tables:SHEETS, history:HISTORY, turnId:turnId})}).then(parseBody).catch(()=>null);
  // (1) LIVE: subscribe to the turn node -> render each announced engine call's trace as it streams.
  if(uid&&window.subscribeTurn){
    UNSUB=window.subscribeTurn(uid,turnId,{
      onStatus:st=>{ if(!live())return; if(st==='done') markTurnDone(); },
      onCall:(k,c)=>{ if(!live()||!c||!c.jobId||SEEN_CALL.has(c.jobId))return; SEEN_CALL.add(c.jobId); addCall(uid,c); },
      onReply:t=>{ if(RUN!==myRun)return; if(t){REPLY=t;} if(!SETTLED)renderRail(); },
      onError:e=>{ if(!live())return; fail(e||'the assistant hit an error'); settle(); },
    });
  }
  // (2) HTTP body is AUTHORITATIVE (blocking; returns {reply, traces, history}): update history, and if the
  // stream produced nothing (RTDB off), render the traces from the body. Then finalize.
  httpPromise.then(j=>{ if(RUN!==myRun)return;
    if(j&&Array.isArray(j.history)) HISTORY=j.history;
    if(!j){ if(!SETTLED&&!VIEWS.length) fail('the assistant did not respond — please try again'); return; }
    if(j.error&&!VIEWS.length&&!REPLY){ REPLY='⚠ '+j.error; }
    if(!VIEWS.length&&Array.isArray(j.traces)) renderTurnFromHTTP(j);   // no live stream -> render from the body
    if(!REPLY&&j.reply) REPLY=j.reply;
    if(!SETTLED) markTurnDone();
  });
  // (3) SAFETY NET: never hang forever.
  setTimeout(()=>{ if(RUN!==myRun||SETTLED)return; if(!VIEWS.length&&!REPLY) fail('the assistant is taking too long — please try again in a moment'); }, 150000);
}
function addCall(uid,c){                                      // an engine call Sonnet made this turn — stream its trace into the panel
  CALLS.push(c);
  STATUS='Reading as: “'+c.question+'”…'; renderRail();
  if(!uid||!window.subscribeRun)return;
  const sub=window.subscribeRun(uid,c.jobId,{
    onView:(k,v)=>{ if(!v)return; const id=c.jobId+'/'+k; if(SEEN.has(id))return; SEEN.add(id); appendView(v); },
    onResolve:(k,r)=>{ if(!r||typeof r!=='object'||!r.column)return; const id=c.jobId+'/'+k; if(SEEN_R.has(id))return; SEEN_R.add(id); appendResolve(r); },
    // reconcile this call's last view with its authoritative result rows (calls stream sequentially, so the
    // most recent deriv sheet is this call's last step). The answer + any clarify are synthesized into REPLY.
    onResult:r=>{ if(!r||!Array.isArray(r.rows))return; const last=BOOK.filter(s=>s.cls==='deriv').pop();
      if(last){ if(r.columns&&r.columns.length)last.cols=r.columns; last.rows=r.rows; last.result=true; if(last.id===ACTIVE)paint(); } },
    onStatus:()=>{}, onClarify:()=>{}, onLowConfidence:()=>{}, onPresent:()=>{}, onError:()=>{},
  });
  callSubs.push(sub);
}
function renderTurnFromHTTP(j){                               // fallback: no RTDB -> build the derivation from the /chat body's traces
  (j.traces||[]).forEach(t=>{ const eng=t.engine||{}; (eng.views||[]).forEach(v=>appendView(v)); });
}
function markTurnDone(){                                      // the turn finished: settle, show Sonnet's reply in the rail
  if(SETTLED)return;
  callSubs.forEach(u=>{try{u();}catch(_){}}); callSubs=[];
  settle();
  CONV = REPLY || 'Done.';
  const n=BOOK.filter(s=>s.cls==='deriv').length;
  STATUS = n?('Answered in '+n+' step'+(n===1?'':'s')):'Done';
  renderRail();
}

async function startRun(){
  if(ORCH) return startTurn();                                // orchestrated front-door (flag; direct path below is unchanged)
  const myRun=++RUN;                                          // supersede guard: an old run's async callbacks must not paint
  const live=()=>RUN===myRun&&!SETTLED;
  let token;
  try{ token=await window.ensureToken(); }
  catch(e){ if(RUN===myRun) fail('sign-in required to run on your data: '+(e&&e.message||e)); return; }
  const uid=window.__uid;
  const jobId=(crypto&&crypto.randomUUID)?crypto.randomUUID():(Date.now()+'-'+Math.random().toString(36).slice(2));
  // (1) kick off the job FIRE-AND-FORGET: on the streaming path the answer arrives via RTDB, not this
  // response. Keep the parsed-body promise so both fallbacks can await it (body reads exactly once).
  const parseBody=async r=>{ try{ if(!r)return null; const txt=await r.text(); return (r.ok&&txt.trim().charAt(0)==='{')?JSON.parse(txt):null; }catch(_){ return null; } };
  const httpPromise=fetch(ENDPOINT,{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+token},
                                    body:JSON.stringify({tables:SHEETS,question:question,jobId:jobId,conversation_id:convId()})}).then(parseBody).catch(()=>null);
  // Persist the server-authoritative conversation_id — GUARDED to this turn (RUN===myRun) so a slow
  // earlier turn can't clobber a later one — and re-render so the follow-up send button re-enables.
  httpPromise.then(j=>{ if(RUN===myRun&&j&&j.conversation_id){ try{ sessionStorage.setItem('pr_conversation_id', j.conversation_id); }catch(_){} renderRail(); } });
  // The HTTP body is ATOMIC (result+present+sql together) — the race-free source for present. Stash it and
  // (re)attempt present; tryPresent no-ops until the derivation has settled, so this can't pre-empt streaming.
  httpPromise.then(j=>{ if(RUN!==myRun||!j)return; HTTPJ=j; if(j.present) PRESENT=true; tryPresent(); });
  // (2) live trace -> sheets appear as the engine works.
  if(uid&&window.subscribeRun){
    UNSUB=window.subscribeRun(uid,jobId,{
      onConversation:c=>{ if(RUN!==myRun||!c)return; try{ sessionStorage.setItem('pr_conversation_id', c); }catch(_){} renderRail(); },   // arrives early via the stream — reliable even if the HTTP body is lost
      onStatus:st=>{ if(!live())return;
        if(st==='resolving'&&!VIEWS.length&&!RESOLVES.length){ STATUS='Resolving to the world…'; renderRail(); }
        else if(st==='running'&&!VIEWS.length&&!RESOLVES.length){ STATUS=WB.runningMsg; renderRail(); }
        else if(st==='done') markDone(); },
      onResolve:(k,r)=>{ if(!live()||!r||typeof r!=='object'||!r.column||SEEN_R.has(k))return; SEEN_R.add(k); appendResolve(r); if(DONE)markDone(); },
      onView:(k,v)=>{ if(!live()||!v||SEEN.has(k))return; SEEN.add(k); appendView(v); if(DONE)markDone(); },
      onResult:r=>{ if(RUN!==myRun)return; J=J||{}; J.result=r; if(live()){ if(DONE)markDone(); } else if(PRESENT){ tryPresent(); } },   // keep late results for present (result node may arrive after settle)
      onClarify:c=>{ if(!live())return; conversationalReply(Object.assign({question:question,clarify:true},c)); },
      onLowConfidence:()=>{ if(!live())return; conversationalReply({question:question}); },
      onPresent:()=>{ if(RUN!==myRun)return; PRESENT=true; tryPresent(); },   // real answer, human phrasing -> present it (no-ops until settled + answer in hand)
      onError:e=>{ if(!live())return; fail(e||'the model reported an error'); settle(); },
    });
  }
  // (2b) EARLY FALLBACK: the POST body only arrives when the job is COMPLETE — if it lands and the
  // stream has shown no life shortly after, streaming is unavailable; render the JSON now.
  httpPromise.then(async j=>{
    if(!j||!live()||DONE)return;
    await new Promise(res=>setTimeout(res,3000));             // grace: a merely-lagging stream still wins
    if(!live()||DONE||VIEWS.length||RESOLVES.length)return;
    renderFromJSON(j);
  });
  // (3) SAFETY NET: if RTDB never reports within ~90s, await/re-drive the POST (cold-start retries).
  setTimeout(async()=>{
    if(!live()||DONE||VIEWS.length)return;
    let j=null;
    for(let a=0;a<5&&!j&&live()&&!DONE;a++){
      try{
        j=a===0?await httpPromise:await fetch(ENDPOINT,{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({tables:SHEETS,question:question,jobId:jobId,conversation_id:convId()})}).then(parseBody);
        if(j)break;
      }catch(_){}
      if(a<4&&live()){ STATUS=WB.warmupMsg; renderRail(); await new Promise(res=>setTimeout(res,4000)); }
    }
    if(!live()||DONE||VIEWS.length)return;
    if(!j){ fail('the model is taking too long to start — please try again in a moment'); return; }
    renderFromJSON(j);
  },90000);
}

/* ---------------- chat: follow-up questions re-run the workbook ---------------- */
function resetRun(){
  if(UNSUB){try{UNSUB();}catch(_){}UNSUB=null;} clearTimeout(doneTimer);
  // Keep the previous turn's derivation/reference sheets, marked "stale", rather than dropping them now: a
  // data query retires them when it makes its own first sheet (dropStale); a conversational/meta follow-up
  // (answered by Sonnet, no sheets of its own) leaves the last derivation on screen — it's usually the subject.
  BOOK.forEach(s=>{ if(s.cls!=='input') s.stale=true; });
  J=null; VIEWS=[]; RESOLVES=[]; SETTLED=false; DONE=false; FAILMSG=null;
  CONV=null; CONVPENDING=false; CONVPROP=null; PRESENT=false; HTTPJ=null;
  CALLS=[]; SEEN_CALL=new Set(); REPLY=null;                  // orchestrated turn state (HISTORY persists across turns)
  callSubs.forEach(u=>{try{u();}catch(_){}}); callSubs=[];
  SEEN=new Set(); SEEN_R=new Set(); AUTO=true;
  STATUS='Analyzing input…'; if(!BOOK.some(s=>s.id===ACTIVE)) ACTIVE=BOOK.length?BOOK[0].id:null;
}
function sendChat(){
  const box=$('chatq'); const q=(box&&box.value||'').trim();
  if(!q||(!SETTLED&&!FAILMSG))return;                         // one run at a time
  archiveTurn();
  box.value='';
  question=q; try{ sessionStorage.setItem(SS.Q,q); }catch(_){}
  resetRun(); paint();
  startRun();
}
function wireChat(){
  const box=$('chatq'), btn=$('chatsend');
  if(btn) btn.onclick=sendChat;
  if(box) box.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendChat(); } });
  const t=$('tabstrip'); if(t) t.addEventListener('scroll',updateTabArrows);
  window.addEventListener('resize',updateTabArrows);
  document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDrawer(); });
  const cl=$('convlist'); if(cl) cl.addEventListener('click',e=>{ const it=e.target.closest('.convitem'); if(it&&it.dataset.cid) openConversation(it.dataset.cid); });
}

/* ---- header title = the conversation's opening question (truncates with … via CSS) ---- */
function setHeaderTitle(q){ const el=$('htitle'); if(el){ el.textContent=q; el.title=q; } document.title='Prereasoner · '+(q.length>40?q.slice(0,40)+'…':q); }

/* ---- conversations drawer (backed by the engine's chat schema; ownership-scoped) ---- */
function convId(){ try{ return sessionStorage.getItem('pr_conversation_id')||null; }catch(_){ return null; } }
function prettyTs(iso){ if(!iso)return ''; try{ return new Date(iso).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }catch(_){ return ''; } }
async function listConversations(){
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversations',{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok) return []; const j=await r.json(); return j.conversations||[];
  }catch(_){ return []; }
}
async function openConversation(id){                          // re-hydrate a past conversation (its stored tables + prompt), then reload
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversation?id='+encodeURIComponent(id),{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok){ closeDrawer(); return; }
    const j=await r.json();
    sessionStorage.setItem('pr_conversation_id', j.conversation_id);
    sessionStorage.setItem(SS.TABLES, JSON.stringify(j.tables||[]));
    sessionStorage.setItem(SS.Q, j.question||'');
    location.reload();
  }catch(_){ closeDrawer(); }
}
function newConversation(){ try{ ['pr_conversation_id',SS.TABLES,SS.Q,SS.CSV,SS.NAME].forEach(k=>k&&sessionStorage.removeItem(k)); }catch(_){}; location.href='/'; }
function openDrawer(){ $('drawer').classList.add('open'); $('drawerback').classList.add('open'); renderDrawer(); }
function closeDrawer(){ $('drawer').classList.remove('open'); $('drawerback').classList.remove('open'); }
async function renderDrawer(){
  const list=$('convlist'); if(!list)return;
  list.innerHTML='<div class=convempty>Loading…</div>';
  const convs=await listConversations();
  list.innerHTML='';
  if(!convs.length){ list.innerHTML='<div class=convempty>Your past conversations will appear here.</div>'; return; }
  const cur=convId();
  // Build with the DOM API (dataset + textContent), never string-concatenated HTML — the conversation
  // id/question come from the server and must not be interpolated into markup or an inline handler.
  for(const c of convs){
    const b=document.createElement('button'); b.className='convitem'+(c.id===cur?' on':''); b.dataset.cid=c.id;
    const q=document.createElement('div'); q.className='cq'; q.textContent=c.question||'(untitled)'; b.appendChild(q);
    if(c.ts){ const t=document.createElement('div'); t.className='ct'; t.textContent=prettyTs(c.ts); b.appendChild(t); }
    list.appendChild(b);
  }
}

function run(){ wireChat(); setHeaderTitle(question); seedInputs(); startRun(); }
try{ fetch(ENDPOINT,{method:'GET',cache:'no-store'}).catch(()=>{}); }catch(_){}   // pre-warm the scale-to-zero backend
