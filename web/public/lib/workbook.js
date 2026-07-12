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
function turnHtml(){                                          // the CURRENT (live) turn's assistant block
  if(FAILMSG) return '<div class=failbox>'+esc(FAILMSG)+'<br><button class=retry onclick=location.reload()>Retry</button></div>';
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
  else{ const rs=resultSummary(); const n=BOOK.filter(s=>s.cls==='deriv').length;
    h='<div class=statusline>&#10003; '+esc(rs?(rs.k==='result'?rs.v:rs.k+': '+rs.v):('answered in '+n+' step'+(n===1?'':'s')))+'</div>'; }
  CHAT.push({q:question, html:h});
}
function renderRail(){
  let h='';
  for(const t of CHAT) h+='<div class="turn user"><div class=msg>'+esc(t.q)+'</div></div><div class="turn ai">'+t.html+'</div>';
  h+='<div class="turn user"><div class=msg>'+esc(question)+'</div></div><div class="turn ai">'+turnHtml()+'</div>';
  const sc=$('rail'); sc.innerHTML=h; sc.scrollTop=sc.scrollHeight;
  const btn=$('chatsend'); if(btn) btn.disabled=!SETTLED&&!FAILMSG;
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
function appendView(v){
  VIEWS.push(v); J=J||{}; J.views=VIEWS; if(v.sql&&!J.sql)J.sql=v.sql;
  const label=v.label||oplabel(v.op);                        // HUMAN name ("join orders + customers"), never v1/step_1
  STATUS=label+'…';
  addSheet({id:'v'+RUN+'_'+VIEWS.length, cls:'deriv', name:label, cols:v.columns||[], rows:v.rows||[], sql:v.sql||''});
}
function appendResolve(r){
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
  } else if(J&&J.result&&J.result.rows){                      // the last view's table is the authoritative final answer
    const lv=VIEWS[VIEWS.length-1], lm=BOOK.filter(s=>s.cls==='deriv').pop();
    lv.columns=J.result.columns||lv.columns; lv.rows=J.result.rows||lv.rows;
    if(lm){ lm.cols=lv.columns; lm.rows=lv.rows; }
  }
  const last=BOOK.filter(s=>s.cls==='deriv').pop();
  if(last){ last.result=true; if(AUTO) ACTIVE=last.id; }
  const n=BOOK.filter(s=>s.cls==='deriv').length;
  STATUS='Answered in '+n+' step'+(n===1?'':'s');
  paint();
}
function renderFromJSON(j){
  if(SETTLED)return;
  if(j.clarify){ goClarify(Object.assign({question:question},j)); return; }
  if(j.error){ fail(j.error); settle(); return; }
  J=j; (j.views||[]).forEach(v=>appendView(v));
  DONE=true; finalize();
}
function settle(){ SETTLED=true; clearTimeout(doneTimer); if(UNSUB){try{UNSUB();}catch(_){}UNSUB=null;} renderRail(); }
function goClarify(c){ settle(); try{ sessionStorage.setItem(SS.CLARIFY,JSON.stringify(c)); }catch(_){}; location.href='clarify'; }

async function startRun(){
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
                                    body:JSON.stringify({tables:SHEETS,question:question,jobId:jobId})}).then(parseBody).catch(()=>null);
  // (2) live trace -> sheets appear as the engine works.
  if(uid&&window.subscribeRun){
    UNSUB=window.subscribeRun(uid,jobId,{
      onStatus:st=>{ if(!live())return;
        if(st==='resolving'&&!VIEWS.length&&!RESOLVES.length){ STATUS='Resolving to the world…'; renderRail(); }
        else if(st==='running'&&!VIEWS.length&&!RESOLVES.length){ STATUS=WB.runningMsg; renderRail(); }
        else if(st==='done') markDone(); },
      onResolve:(k,r)=>{ if(!live()||!r||typeof r!=='object'||!r.column||SEEN_R.has(k))return; SEEN_R.add(k); appendResolve(r); if(DONE)markDone(); },
      onView:(k,v)=>{ if(!live()||!v||SEEN.has(k))return; SEEN.add(k); appendView(v); if(DONE)markDone(); },
      onResult:r=>{ if(!live())return; J=J||{}; J.result=r; if(DONE)markDone(); },
      onClarify:c=>{ if(!live())return; goClarify(Object.assign({question:question},c)); },
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
        j=a===0?await httpPromise:await fetch(ENDPOINT,{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+token},body:JSON.stringify({tables:SHEETS,question:question,jobId:jobId})}).then(parseBody);
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
  BOOK=BOOK.filter(s=>s.cls==='input');                       // the user's sheets stay; derived/reference sheets are the run's
  J=null; VIEWS=[]; RESOLVES=[]; SETTLED=false; DONE=false; FAILMSG=null;
  SEEN=new Set(); SEEN_R=new Set(); AUTO=true;
  STATUS='Analyzing input…'; ACTIVE=BOOK.length?BOOK[0].id:null;
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
}

/* ---- header title = the conversation's opening question (truncates with … via CSS) ---- */
function setHeaderTitle(q){ const el=$('htitle'); if(el){ el.textContent=q; el.title=q; } document.title='Prereasoner · '+(q.length>40?q.slice(0,40)+'…':q); }

/* ---- conversations drawer ----
   listConversations() is the SEAM to the conversation store. The store (a "chat" Postgres
   schema: user_profile / conversation / user_conversation, per the conversation-schema design)
   is a backend change owned with the MCP/orchestrator layer; until it lands this returns [] and
   the drawer shows an empty state. When wired, return [{id, question, ts}] newest-first and
   selecting one should load that conversation (its own schema). */
async function listConversations(){ try{ if(window.listConversations) return await window.listConversations(); }catch(_){} return []; }
function openDrawer(){ $('drawer').classList.add('open'); $('drawerback').classList.add('open'); renderDrawer(); }
function closeDrawer(){ $('drawer').classList.remove('open'); $('drawerback').classList.remove('open'); }
async function renderDrawer(){
  const list=$('convlist'); if(!list)return;
  list.innerHTML='<div class=convempty>Loading…</div>';
  const convs=await listConversations();
  if(!convs.length){ list.innerHTML='<div class=convempty>Your past conversations will appear here.</div>'; return; }
  list.innerHTML=convs.map(c=>'<button class=convitem onclick="openConversation(\''+esc(c.id)+'\')"><div class=cq>'+esc(c.question||'(untitled)')+'</div>'+(c.ts?'<div class=ct>'+esc(c.ts)+'</div>':'')+'</button>').join('');
}
function openConversation(id){ if(window.openConversation){ window.openConversation(id); return; } closeDrawer(); }

function run(){ wireChat(); setHeaderTitle(question); seedInputs(); startRun(); }
try{ fetch(ENDPOINT,{method:'GET',cache:'no-store'}).catch(()=>{}); }catch(_){}   // pre-warm the scale-to-zero backend
