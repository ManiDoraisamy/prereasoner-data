// workbook.js — the editable WORKBOOK + CHAT shared by /reason and /knowledge. CLASSIC script: load
// AFTER lib/shared.js and after an inline <script> that sets window.WB_CONFIG. The page's module
// block (firebase-init.js) calls run() once signed in, or fail(msg) when sign-in fails.
//
// WB_CONFIG: { endpoint:   engine route ('/api/reason' | '/api/knowledge'),
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
  demoTables: [{name:'customers',data:'customer_id,name,city\n1,Ada,Paris\n2,Lin,Lyon\n3,Bo,Paris\n4,Sam,Berlin\n5,Mai,Lyon'},
               {name:'orders',data:'order_id,customer_id,amount,status\n101,1,120,shipped\n102,1,60,shipped\n103,2,80,shipped\n104,2,160,shipped\n105,3,90,shipped\n106,4,40,pending\n107,5,200,shipped\n108,5,30,shipped'}],
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
// ORCH (Sonnet front-door) is the DEFAULT. Turn it OFF for a session with the URL query ?chat=0 (works on
// any origin, no console; persists to localStorage 'pr_chat'), or set WB.chat=false on a page. ?chat=1
// forces it on. This is the path that reads each message in context and rewrites it into a precise query.
const ORCH = (()=>{ try{
  const p=new URLSearchParams(location.search).get('chat');
  if(p==='1'||p==='0'){ try{localStorage.setItem('pr_chat',p);}catch(_){} return p==='1'; }
  const o=localStorage.getItem('pr_chat'); if(o==='1')return true; if(o==='0')return false;
}catch(_){} return WB.chat!==false; })();
const CHAT_ENDPOINT = API_BASE + (WB.chatEndpoint || '/chat');
// ---- external LLM: /chat, /api/converse and /api/master/generate send the user's message AND sheet
// data to Anthropic's Claude API. EXTERNAL_LLM_ENABLED is the authoritative operator switch. The
// durable disclosure is /privacy; do not add notices to the answer rail. ?chat=0 is a developer
// routing control that runs the deterministic /api/reason path without the orchestrator.
let HISTORY=[];                                  // lean cross-turn transcript for the orchestrator [{role,content}]
let CALLS=[],SEEN_CALL=new Set(),REPLY=null,callSubs=[];   // this turn's announced engine calls + their trace subs
let HTTPHIST=false;                              // did the /chat body land (authoritative history)? else reconstruct client-side
let EDITED=false,LASTQ=null;                     // the user edited an input cell (-> offer Recalculate); the last question run
let SEL=null,ANCH=null,INEDIT=false;              // spreadsheet: the ACTIVE cell {sid,r,c}, the selection ANCHor {sid,r,c}, edit mode
let UNDO=[],REDO=[],DRAG=false;                   // per-sheet undo/redo snapshots; mouse drag-select in progress
const UNDOCAP=120;

function sheetById(id){return BOOK.find(s=>s.id===id);}
function addSheet(m){ BOOK.push(m); const passive=(m.cls==='ref'||m.cls==='master'); if(!passive&&AUTO) ACTIVE=m.id; if(passive&&!ACTIVE) ACTIVE=m.id; paint(); }

/* ---------------- rendering: the sheet ---------------- */
function isNum(v){return v!==''&&v!=null&&/^-?\$?[\d,]*\.?\d+%?$/.test(String(v).trim());}
function fmt(v){ if(typeof v==='number'&&!Number.isInteger(v)) return (Math.round(v*1000)/1000).toString(); return v==null?'':String(v); }
function renderGrid(m){
  const cols=m.cols||[],rows=(m.rows||[]);
  const numeric=cols.map((_,ci)=>rows.length>0&&rows.every(r=>r[ci]===''||r[ci]==null||isNum(r[ci])));
  const shown=rows.slice(0,MAX_RENDER_ROWS);
  const edit=(m.cls==='input'||m.cls==='master');            // input + master are EDITABLE; derived/reference are read-only but STILL navigable
  // B1: per-column provenance on derived/reference sheets — SRC = carried from your data, AI = added by lookup/derivation.
  const inputCols=new Set(BOOK.filter(s=>s.cls==='input').flatMap(s=>(s.cols||[]).map(c=>String(c).toLowerCase())));
  const showProv=(m.cls==='deriv'||m.cls==='ref');
  const provOf=c=>inputCols.has(String(c).toLowerCase())?'src':'ai';
  let h='<div class=sheetscroll><table class="wb'+(m.result?' result':'')+(edit?' editable':' readonly')+'"><thead><tr><th class=rn></th>';
  for(let ci=0;ci<cols.length;ci++){ const pv=showProv?provOf(cols[ci]):'';
    if(m.cls==='master'&&m._editCol===ci){                    // inline column-name editor — spreadsheet-style, no prompt() dialog
      h+='<th class=colnamewrap><input class=colnameedit data-orig="'+escAttr(cols[ci])+'" value="'+escAttr(cols[ci])+'" placeholder="column name" spellcheck=false autocomplete=off '
        +'onkeydown="event.stopPropagation(); if(event.key===\'Enter\'){this.blur();} else if(event.key===\'Escape\'){this.value=this.dataset.orig; this.blur();}" '
        +'onblur="commitColName(\''+m.id+'\','+ci+',this.value,this.dataset.orig)"></th>';
      continue; }
    h+='<th class="'+((numeric[ci]?'n ':'')+(pv?'prov prov-'+pv:'')).trim()+'"'
      +(m.cls==='master'?' ondblclick="editMasterCol(\''+m.id+'\','+ci+')" title="Double-click to rename"'
                        :(pv?' title="'+(pv==='src'?'From your data':'Added by a public-source lookup or deterministic derivation — worth a sanity-check')+'"':''))
      +'>'+esc(cols[ci])+(pv?'<span class="provtag '+pv+'">'+(pv==='src'?'SRC':'AI')+'</span>':'')+'</th>'; }
  if(m.cls==='master') h+='<th class=newcol onclick="addMasterCol(\''+m.id+'\')" title="Add a column">+ new column</th>';   // ghost "add column" — mirrors the "+ new row" ghost row
  h+='</tr></thead><tbody>';
  const nrows=edit?Math.min(shown.length+1,MAX_RENDER_ROWS):shown.length;   // editable: one trailing blank "new record" row
  const rc=(SEL&&SEL.sid===m.id)?selRect():null;                           // the selection rectangle if the selection is on THIS sheet (any sheet)
  for(let ri=0;ri<Math.max(nrows,edit?1:0);ri++){ const row=shown[ri]||cols.map(()=>'');
    const isNew=edit&&ri===shown.length;                                    // the Access-style "new record" row (editable only)
    h+='<tr'+(isNew?' class=newrow':'')+'><td class=rn>'+(isNew?'<span class=newstar title="New row — type here to add">&lowast;</span>':((ri+1)+(edit?'<button class=rowdel title="Delete row" onclick="delRow(\''+m.id+'\','+ri+')">&times;</button>':'')))+'</td>';
    for(let ci=0;ci<cols.length;ci++){ const val=fmt(row[ci]);
      let mark=''; if(rc){ if(ri===SEL.r&&ci===SEL.c)mark=' sel'; else if(ri>=rc.r0&&ri<=rc.r1&&ci>=rc.c0&&ci<=rc.c1)mark=' insel'; }
      const aic=(m.cls==='master'&&m.cellAI&&m.cellAI.has(ri+','+ci))?' aicell':'';   // B3: autofilled cell (edit to confirm)
      h+='<td class="'+(numeric[ci]?'n ':'')+'wbc'+mark+aic+'"'   // EVERY cell is a navigable wbc; only editable sheets accept edits/paste/new-row
        +' data-sid="'+m.id+'" data-r="'+ri+'" data-c="'+ci+'" tabindex="-1"'+(isNew&&ci===0?' data-ph="+ new row"':'')+' title="'+escAttr(val+(aic?'  ·  AI-generated — edit to confirm':''))+'">'+esc(val)+'</td>'; }
    if(m.cls==='master') h+='<td class=newcol onclick="addMasterCol(\''+m.id+'\')"></td>';   // ghost cells under the "+ new column" header
    h+='</tr>'; }
  if(!rows.length&&!edit) h+='<tr><td class=rn>1</td><td colspan='+Math.max(1,cols.length)+' style="color:#9a93b5">no rows</td></tr>';
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
const KINDLBL={input:'Your data',deriv:'Derived',ref:'Public source',master:'Reference'};
function dispName(s){ let n=s&&s.name||''; if(s&&s.cls==='deriv'&&/(wikipedia|reference) lookup/i.test(n)) n='enriched'; return n; }
function renderSheet(){
  const m=sheetById(ACTIVE);
  if(!m){ $('sheetcard').innerHTML='<div class=sheetmsg id=sheetmsg>'+(FAILMSG?'&#9888; '+esc(FAILMSG):'<span class=spin></span> '+esc(STATUS))+'</div>'; return; }
  let h='<div class="bandbar band-'+m.cls+'"></div><div class=sheetband>'
    +'<span class="dot '+m.cls+'"></span><span class=snm title="'+esc(m.result?'Result':m.name)+'">'+esc(m.result?'Result':dispName(m))+'</span>'
    +'<span class="skind '+m.cls+'">'+(m.result?esc(m.name):KINDLBL[m.cls])+'</span>'
    +(m.sql?'<span class=spacer></span><button class=sqlbtn title="View the SQL for this sheet" aria-label="View SQL" onclick=toggleSql()>SQL</button>':'')
    +(m.cls==='master'?'<span class=spacer></span>'
        +((m.saved&&!m.dirty)                                 // saved -> a ⋮ menu (Autofill / Upload / Delete); otherwise the Save button
           ?'<button class="mbtn mdots" aria-label="More actions" onclick="masterMenu(\''+m.id+'\',this,event)">⋮</button>'
           :'<button class="mbtn msave dirty" title="Save as reference — reused across your conversations" onclick="saveMaster(\''+m.id+'\')">Save</button>'):'')
    +'</div>';
  if(EDITED) h+='<div class="masterhint recalchint"><span>&#9998; You changed your data — recompute to update the answer.</span>'
    +'<span class=mactions><button class=mlink onclick="recalc()">Recalculate</button></span></div>';
  if(m.cls==='master'){
    // D3: teach once, then get out of the way — empty tables get guidance, populated ones a quiet caption; the long pitch moves to a "?".
    const hasAttr=(m.cols||[]).length>1;
    const help='Reference data is reused across every conversation. Add attribute columns (category, price, region…), then Autofill or Upload to fill them.';
    h+='<div class="masterhint'+(hasAttr?' quiet':'')+'">';
    h+= hasAttr
      ? '<span>Reference data for <b>'+esc(m.name)+'</b> · reused across your conversations <button class=mhelp title="'+escAttr(help)+'" aria-label="About reference data">?</button></span>'
      : '<span>Add a column, then <b>Autofill</b> or <b>Upload</b> to enrich <b>'+esc(m.name)+'</b>. <button class=mhelp title="'+escAttr(help)+'" aria-label="About reference data">?</button></span>';
    if(!(m.saved&&!m.dirty))                                  // unsaved -> inline actions; once saved they move into the ⋮ menu
      h+='<span class=mactions>'
        +'<button class=mlink'+(m._genBusy?' disabled':'')+' onclick="generateMaster(\''+m.id+'\')">'+esc(m._gen||'Autofill')+'</button>'
        +'<button class=mlink onclick="masterUpload(\''+m.id+'\')">Upload</button>'
        +(m.saved?'':'<button class="mlink mclose" aria-label="Remove — move to + Reference" title="Remove from tabs — add it back any time from “+ Reference”" onclick="removeMasterSheet(\''+m.id+'\')">✕</button>')   // D2
        +'</span>';
    h+='</div>';
  }
  if(m.sql) h+='<div class=sqlrow id=sqlrow><div class=vsql>'+sqlTokens(m.sql).map(tk=>'<span class="vtok '+tokCls(tk)+'">'+esc(tk)+'</span>').join('')+'</div></div>';
  h+=renderGrid(m);
  if(m.result){                                              // E1/B2: ground the answer — link it to the rows it aggregated
    const feeders=BOOK.filter(s=>s.cls==='deriv'&&!s.result);
    const src=feeders[feeders.length-1];
    if(src){ const n=(src.rows||[]).length;
      h+='<div class=resultcap>= aggregated from <b>'+n+'</b> row'+(n===1?'':'s')+' · <button class=mlink onclick="pick(\''+src.id+'\')">view the “'+esc(dispName(src))+'” rows</button></div>'; }
  }
  $('sheetcard').innerHTML=h;
}
function toggleSql(){const r=$('sqlrow'); if(r) r.classList.toggle('open');}
function tabTxt(s){ const t=s.result?'Result':dispName(s); return t.length>26?t.slice(0,24)+'…':t; }
function renderTabs(){
  // A5: group the strip by pipeline role — Sources · Reference · Steps · Result — so inputs and the answer are never
  // lost among the machine scratch sheets. The zone labels double as a non-color cue for sheet kind (E3).
  const sources=BOOK.filter(s=>s.cls==='input');
  const reference=BOOK.filter(s=>s.cls==='master');
  const result=BOOK.filter(s=>s.result);
  const steps=BOOK.filter(s=>(s.cls==='deriv'||s.cls==='ref')&&!s.result);
  const tab=s=>'<button class="wtab'+(s.id===ACTIVE?' active':'')+'" onclick="pick(\''+s.id+'\')" title="'+esc((s.result?'Result':s.name)+(s.cls==='master'&&!s.saved?' — unsaved reference':''))+'"><span class="dot '+s.cls+'"></span>'+esc(tabTxt(s))+(s.cls==='master'&&!s.saved?'<span class=unsaveddot title="unsaved" aria-label="unsaved"> •</span>':'')+'</button>';
  const zone=(label,arr,extra)=> (arr.length||extra) ? '<span class=tabzone>'+arr.map(tab).join('')+(extra||'')+'</span>' : '';   // grouped (subtle dividers) but no space-wasting labels
  const suggest = REFCANDS.length ? '<button class="wtab refsuggest" title="'+REFCANDS.length+' reference '+(REFCANDS.length===1?'table':'tables')+' not shown — click to add as a sheet" onclick="refSuggestMenu(this,event)">+ Reference</button>' : '';
  $('tabstrip').innerHTML = zone('Sources',sources) + zone('Reference',reference,suggest) + zone('Steps',steps) + zone('Result',result);
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
function conv2html(t){
  // Render the assistant reply's inline markdown. esc() runs FIRST so any HTML in the (LLM-generated) text is
  // neutralized to entities; the markdown tags below are then added on that safe string, so this stays XSS-safe.
  let s = esc(String(t||''));
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');            // `inline code`
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');  // **bold**  (consumed before single-* italic)
  s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');              // *italic*
  return s.replace(/\n/g, '<br>');
}
// Only THIS turn's derivation is "Reasoning steps". Stale sheets (kept from the previous turn so a conversational
// follow-up's workbook isn't empty) must NOT render here — else an empty/no-data turn ("chennai?" with no Chennai
// rows) would show the PRIOR turn's steps (e.g. France's world_join/world_filter) under the new question's header.
function lineage(s){ if(!s||!s.sql)return ''; const t=[]; const re=/(?:from|join)\s+"([^"]+)"/gi; let m2;   // C1: which sheets feed this step (from its SQL)
  while((m2=re.exec(s.sql))){ const nm=m2[1]; if(!/world|meaning/i.test(nm)&&!t.includes(nm)) t.push(nm); } return t.slice(0,4).join(', '); }
function derivLinks(){ const d=BOOK.filter(s=>s.cls==='deriv'&&!s.stale); return d.length?('<div class=steps>'+d.map((s,i)=>{ const lin=lineage(s);
  return '<button class="steplink'+(s.id===ACTIVE?' on':'')+'" title="Open the “'+escAttr(dispName(s))+'” sheet'+(lin?' — built from: '+escAttr(lin):'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>'+(i+1)+'</span><span class=stx>'+esc(s.desc||dispName(s))+(lin?'<span class=steplin> · from '+esc(lin)+'</span>':'')+'</span></button>'; }).join('')+'</div>'):''; }
function asksLine(){ return CALLS.length?('<div class=cotask>read as '+CALLS.map(c=>'&ldquo;'+esc(c.question)+'&rdquo;').join(', ')+'</div>'):''; }
// The chain of thought — collapsed under a "Reasoning steps" toggle. COTOPEN persists the open state so a
// re-render (e.g. clicking a step to open its sheet) doesn't snap it shut.
let COTOPEN=false;
function cotHtml(){
  if(!ORCH) return '';
  const body=asksLine()+derivLinks();
  if(!body) return '';
  return '<div class="cot'+(COTOPEN?' open':'')+'"><button class=cotbtn onclick="toggleCot()"><span class=cotchev>&#8250;</span>Reasoning steps</button><div class=cotbody'+(COTOPEN?'':' hidden')+'>'+body+'</div></div>';
}
function toggleCot(){ COTOPEN=!COTOPEN; renderRail(); }
function turnHtml(){                                          // the CURRENT (live) turn's assistant block
  if(FAILMSG) return '<div class=failbox>'+esc(FAILMSG)+'<br><button class=retry onclick=location.reload()>Retry</button></div>';
  return turnHtmlBody();
}
function turnHtmlBody(){
  if(CONV){ let h='';
    if(ORCH) h+=cotHtml();                                  // "Reasoning steps" ABOVE the answer (part of THIS turn, not floating near the next prompt)
    h+='<div class=convmsg>'+conv2html(CONV)+'</div>';       // the plain answer (or a clarify/meta reply), for the end user
    if(CONVPROP) h+='<div class=convrun><button onclick="runProposed()">Run &ldquo;'+esc(CONVPROP)+'&rdquo;</button></div>';
    if(!ORCH&&PRESENT) h+=derivLinks();                      // present mode keeps the derivation reachable from the rail
    return h; }
  if(CONVPENDING) return '<div class=statusline><span class=spin></span> '+esc(STATUS)+'</div>';
  let h='<div class=statusline>'+(SETTLED?'&#10003; ':'<span class=spin></span> ')+esc(STATUS)+'</div>';
  if(!ORCH){                                                  // orchestrated runs show a clean status while composing; the full,
    const refs=BOOK.filter(s=>s.cls==='ref'), derivs=BOOK.filter(s=>s.cls==='deriv');   // plain-English steps live in the "Reasoning steps" panel once the answer lands
    if(refs.length)
      h+='<div class=steps>'+refs.map(s=>'<button class="steplink refl'+(s.id===ACTIVE?' on':'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>&#9707;</span><span class=stx>looked up '+esc(s.name)+'</span></button>').join('')+'</div>';
    if(derivs.length)
      h+='<div class=steps>'+derivs.map((s,i)=>'<button class="steplink'+(s.id===ACTIVE?' on':'')+'" onclick="pick(\''+s.id+'\')"><span class=idx>'+(i+1)+'</span><span class=stx>'+esc(s.desc||s.name)+'</span></button>').join('')+'</div>';
  }
  if(SETTLED){ const rs=resultSummary();
    if(rs) h+='<div class=resultline><div class=rk>'+esc(rs.k)+'</div><div class="rv'+(rs.big?'':' small')+'">'+esc(rs.v)+'</div></div>';
  }
  ((J&&J.warnings)||[]).forEach(w=>{ h+='<div class=warn>&#9888; '+esc(String(w))+'</div>'; });
  return h;
}
function turnReply(){                                         // the current turn's plain answer text (for the persisted snapshot)
  if(CONV) return CONV;
  const rs=resultSummary(); return rs?(rs.k==='result'?rs.v:rs.k+': '+rs.v):'';
}
function archiveTurn(){                                       // freeze the finished turn to a MINIMAL line (no dead links)
  let h; const reply=turnReply();
  if(FAILMSG) h='<div class=statusline>&#9888; '+esc(FAILMSG)+'</div>';
  else if(CONV) h=(ORCH?('<div class=cot>'+asksLine()+'</div>'):'')+'<div class=convmsg>'+conv2html(CONV)+'</div>';   // frozen: the "read as" ABOVE the reply (steps are gone)
  else{ const rs=resultSummary(); const n=BOOK.filter(s=>s.cls==='deriv').length;
    h='<div class=statusline>&#10003; '+esc(rs?(rs.k==='result'?rs.v:rs.k+': '+rs.v):('answered in '+n+' step'+(n===1?'':'s')))+'</div>'; }
  CHAT.push({q:question, html:h, reply:reply});
}
function renderRail(){
  let h='';
  for(const t of CHAT) h+='<div class="turn user"><div class=msg>'+esc(t.q)+'</div></div><div class="turn ai">'+t.html+'</div>';
  h+='<div class="turn user"><div class=msg>'+esc(question)+'</div></div><div class="turn ai">'+turnHtml()+'</div>';
  const sc=$('rail'); sc.innerHTML=h; sc.scrollTop=sc.scrollHeight;
  // A follow-up needs the conversation_id (arrives with the response), so a NEW conversation keeps send
  // disabled until it lands — otherwise the follow-up would POST conversation_id:null and orphan into a fresh
  // server conversation (splitting the thread + never updating the /reason/<id> URL). ORCH is NOT exempt:
  // its history is client-side, but server-side grouping + the shareable URL still need the id threaded.
  const btn=$('chatsend'); if(btn) btn.disabled=!((SETTLED&&convId())||FAILMSG);
}
function paint(){ renderTabs(); renderSheet(); renderRail(); }
function fail(m){ FAILMSG=String(m||'something went wrong'); STATUS='failed'; paint(); }

/* ---------------- the run (streaming + fallbacks) ---------------- */
const ENDPOINT=API_BASE+WB.endpoint;
function seedInputs(){
  SHEETS.forEach((s,i)=>{ const p=parseCSV(s.data);
    addSheet({id:'in'+i, cls:'input', si:i, name:s.name, cols:p.cols, rows:p.rows}); });
  stampBaseline();                                            // pristine baseline -> undo back to it clears the Recalculate nag
  ACTIVE=BOOK.length?BOOK[0].id:null; paint();
}
/* ---- editable sheets: a proper TWO-MODE spreadsheet (Excel / Google Sheets model). A cell is either
   SELECTED (navigate mode — arrows move; Shift+arrows/drag/Shift+click extend a multi-cell range;
   a typed char overwrites; Enter/F2/double-click edits; Delete clears the range; Ctrl+C/X/V copy/cut/
   paste ranges; Ctrl+D/R fill down/right; Ctrl+A select all; Ctrl+Z/Y undo/redo) or in EDIT mode
   (caret in the cell — Enter/Tab commit + move, Escape cancels). Input sheets ("what-if" -> Recalculate)
   + master data are editable; the rest read-only. */
function cellEl(sid,r,c){ return document.querySelector('#sheetcard td.wbc[data-sid="'+sid+'"][data-r="'+r+'"][data-c="'+c+'"]'); }
function sheetEditable(sid){ const sh=BOOK.find(s=>s.id===sid); return !!(sh&&(sh.cls==='input'||sh.cls==='master')); }   // read-only sheets navigate + copy, but never edit
function markDirtySheet(sh){ if(!sh)return; if(sh.cls==='input'){ if(!EDITED){ EDITED=true; showRecalc(true); } } else if(sh.cls==='master'){
  sh.dirty=true; const b=document.querySelector('.msave'); if(b)b.classList.add('dirty'); saveConvState(); } }
function lastDataRow(sh){ return Math.max(0,(sh.rows||[]).length-1); }   // ranges/fills stop here; the trailing "new record" row is never multi-selected
function cellVal(sh,r,c){ return (sh.rows[r]&&sh.rows[r][c]!=null)?sh.rows[r][c]:''; }
// The Recalculate bar reflects whether input data differs from what was last computed. markDirtySheet sets it on a
// forward edit; refreshDirty recomputes it after undo/redo so returning to the pristine baseline clears the nag.
function inputSig(sh){ return JSON.stringify([sh.cols||[], sh.rows||[]]); }   // boundary-unambiguous signature for the dirty check
function stampBaseline(){ BOOK.forEach(s=>{ if(s.cls==='input') s._base=inputSig(s); }); }
function refreshDirty(){ const inputs=BOOK.filter(s=>s.cls==='input'); if(inputs.length&&inputs.every(s=>s._base!=null)){ const ch=inputs.some(s=>inputSig(s)!==s._base); EDITED=ch; showRecalc(ch); } }
function selRect(){ if(!SEL)return null; const a=(ANCH&&ANCH.sid===SEL.sid)?ANCH:SEL;   // the normalized selection rectangle on SEL's sheet
  return {sid:SEL.sid, r0:Math.min(SEL.r,a.r), r1:Math.max(SEL.r,a.r), c0:Math.min(SEL.c,a.c), c1:Math.max(SEL.c,a.c)}; }
function applySelHi(){                                        // repaint the highlight in place (no full re-render) — for fast arrow/drag select
  document.querySelectorAll('#sheetcard td.wbc.sel,#sheetcard td.wbc.insel').forEach(x=>x.classList.remove('sel','insel'));
  if(!SEL)return; const rc=selRect();
  for(let r=rc.r0;r<=rc.r1;r++)for(let c=rc.c0;c<=rc.c1;c++){ const el=cellEl(rc.sid,r,c); if(el) el.classList.add((r===SEL.r&&c===SEL.c)?'sel':'insel'); }
}
function focusActive(){ if(!SEL)return; const el=cellEl(SEL.sid,SEL.r,SEL.c); if(el){ el.focus({preventScroll:true}); el.scrollIntoView({block:'nearest',inline:'nearest'}); } }
function selCell(sid,r,c,extend){                            // set the ACTIVE cell (navigate mode); extend=true keeps the anchor -> a range
  commitEdit();
  const sh=BOOK.find(s=>s.id===sid); if(!sh)return; const nc=(sh.cols||[]).length; if(nc===0)return;
  const editable=(sh.cls==='input'||sh.cls==='master');
  if(c<0)c=0; if(c>=nc)c=nc-1; if(r<0)r=0;
  if(!editable){ const maxr=lastDataRow(sh); if(r>maxr)r=maxr; }   // read-only: never move past (or grow) the existing data
  if(extend&&SEL&&SEL.sid===sid){ const maxr=lastDataRow(sh); if(r>maxr)r=maxr; SEL={sid:sid,r:r,c:c}; if(!ANCH||ANCH.sid!==sid)ANCH={sid:sid,r:r,c:c}; }   // a RANGE never spans the new-record row
  else { SEL={sid:sid,r:r,c:c}; ANCH={sid:sid,r:r,c:c}; }
  let el=cellEl(sid,SEL.r,SEL.c);
  if(!el&&editable){ while(sh.rows.length<=SEL.r) sh.rows.push(sh.cols.map(()=>'')); paint(); el=cellEl(sid,SEL.r,SEL.c); }   // grow into a new row (editable only)
  else applySelHi();
  if(el){ el.focus({preventScroll:true}); el.scrollIntoView({block:'nearest',inline:'nearest'}); }
}
function beginEdit(ch){                                       // enter edit mode on the active cell (ch = typed char overwrites)
  if(!SEL||!sheetEditable(SEL.sid))return; ANCH={sid:SEL.sid,r:SEL.r,c:SEL.c};   // read-only sheets are never editable
  document.querySelectorAll('#sheetcard td.wbc.insel').forEach(x=>x.classList.remove('insel'));
  const el=cellEl(SEL.sid,SEL.r,SEL.c); if(!el)return;
  el.dataset.orig=el.textContent; el.contentEditable='true'; el.classList.add('editing'); INEDIT=true;
  if(ch!=null) el.textContent=ch;
  el.focus();
  try{ const rg=document.createRange(); rg.selectNodeContents(el); rg.collapse(false); const s=getSelection(); s.removeAllRanges(); s.addRange(rg); }catch(_){}
}
function commitEdit(){                                        // write the edit to the model + leave edit mode
  if(!INEDIT||!SEL)return; INEDIT=false; const el=cellEl(SEL.sid,SEL.r,SEL.c), sh=BOOK.find(s=>s.id===SEL.sid);
  if(!el)return; el.contentEditable='false'; el.classList.remove('editing');
  if(sh){ const v=(el.textContent||'').replace(/\n/g,' ');
    const grew=SEL.r>=sh.rows.length;                          // typing into the trailing "new record" row appends a real row
    const old=(sh.rows[SEL.r]&&sh.rows[SEL.r][SEL.c]!=null)?sh.rows[SEL.r][SEL.c]:'';
    if(old!==v){ pushUndo(SEL.sid); while(sh.rows.length<=SEL.r) sh.rows.push(sh.cols.map(()=>'')); sh.rows[SEL.r][SEL.c]=v;
      if(sh.cellAI) sh.cellAI.delete(SEL.r+','+SEL.c);         // B3: editing an autofilled cell promotes it to user-entered
      markDirtySheet(sh);
      if(grew) paint(); }                                      // re-render so a fresh new-record row appears below (Access datasheet behavior)
    el.title=v; }
}
function cancelEdit(){                                        // discard the edit, restore the original
  if(!INEDIT||!SEL)return; INEDIT=false; const el=cellEl(SEL.sid,SEL.r,SEL.c);
  if(el){ el.textContent=el.dataset.orig!=null?el.dataset.orig:''; el.contentEditable='false'; el.classList.remove('editing'); el.focus(); }
}
/* ---- undo / redo: a bounded stack of per-sheet row snapshots (these tables are small). Each mutating op
   calls pushUndo(sid) with the PRE-state; undo swaps in that state and banks the current one for redo. */
function snap(sid){ const sh=BOOK.find(s=>s.id===sid); if(!sh)return null; return {sid:sid, rows:sh.rows.map(r=>r.slice()), sel:SEL&&{sid:SEL.sid,r:SEL.r,c:SEL.c}, anch:ANCH&&{sid:ANCH.sid,r:ANCH.r,c:ANCH.c}}; }
function pushUndo(sid){ const s=snap(sid); if(s){ UNDO.push(s); if(UNDO.length>UNDOCAP)UNDO.shift(); REDO.length=0; } }
function applyState(s){ const sh=BOOK.find(x=>x.id===s.sid); if(!sh)return; sh.rows=s.rows.map(r=>r.slice()); ACTIVE=s.sid; AUTO=false;
  SEL=s.sel?{sid:s.sel.sid,r:s.sel.r,c:s.sel.c}:null; ANCH=s.anch?{sid:s.anch.sid,r:s.anch.r,c:s.anch.c}:(SEL&&{sid:SEL.sid,r:SEL.r,c:SEL.c});
  if(sh.cls==='master') markDirtySheet(sh); else refreshDirty(); }   // recompute the recalc bar from baseline so undo-to-pristine clears it
function undo(){ if(!UNDO.length)return; const s=UNDO.pop(); REDO.push(snap(s.sid)); applyState(s); paint(); focusActive(); }
function redo(){ if(!REDO.length)return; const s=REDO.pop(); UNDO.push(snap(s.sid)); applyState(s); paint(); focusActive(); }
function clearRange(){                                        // Delete/Backspace -> blank every cell in the selection
  if(!SEL||!sheetEditable(SEL.sid))return; const rc=selRect(); const sh=BOOK.find(s=>s.id===rc.sid); if(!sh)return;
  let changed=false;                                          // scan first (read-only) so undo snapshots the true pre-state
  for(let r=rc.r0;r<=rc.r1&&!changed;r++){ if(!sh.rows[r])continue; for(let c=rc.c0;c<=rc.c1;c++){ if(sh.rows[r][c]!==''&&sh.rows[r][c]!=null){ changed=true; break; } } }
  if(!changed)return; pushUndo(rc.sid);
  for(let r=rc.r0;r<=rc.r1;r++){ if(!sh.rows[r])continue; for(let c=rc.c0;c<=rc.c1;c++) sh.rows[r][c]=''; }
  markDirtySheet(sh); paint(); focusActive();
}
function fillDown(){ const rc=selRect(); if(!rc||rc.r1===rc.r0||!sheetEditable(rc.sid))return; const sh=BOOK.find(s=>s.id===rc.sid); if(!sh)return;
  const rmax=Math.min(rc.r1,lastDataRow(sh)); if(rmax<=rc.r0)return;        // never grow past real data (no phantom row)
  let changed=false; for(let c=rc.c0;c<=rc.c1&&!changed;c++){ const src=cellVal(sh,rc.r0,c); for(let r=rc.r0+1;r<=rmax;r++)if(cellVal(sh,r,c)!==src){changed=true;break;} }
  if(!changed)return; pushUndo(rc.sid);                                     // no-op fills must not push undo / wipe redo
  for(let c=rc.c0;c<=rc.c1;c++){ const src=cellVal(sh,rc.r0,c); for(let r=rc.r0+1;r<=rmax;r++)sh.rows[r][c]=src; }
  markDirtySheet(sh); paint(); focusActive(); }
function fillRight(){ const rc=selRect(); if(!rc||rc.c1===rc.c0||!sheetEditable(rc.sid))return; const sh=BOOK.find(s=>s.id===rc.sid); if(!sh)return;
  const rmax=Math.min(rc.r1,lastDataRow(sh));
  let changed=false; for(let r=rc.r0;r<=rmax&&!changed;r++){ const src=cellVal(sh,r,rc.c0); for(let c=rc.c0+1;c<=rc.c1;c++)if(c<sh.cols.length&&cellVal(sh,r,c)!==src){changed=true;break;} }
  if(!changed)return; pushUndo(rc.sid);
  for(let r=rc.r0;r<=rmax;r++){ if(!sh.rows[r])continue; const src=cellVal(sh,r,rc.c0); for(let c=rc.c0+1;c<=rc.c1;c++)if(c<sh.cols.length)sh.rows[r][c]=src; }
  markDirtySheet(sh); paint(); focusActive(); }
function selectAll(){ if(!SEL)return; const sh=BOOK.find(s=>s.id===SEL.sid); if(!sh)return; ANCH={sid:SEL.sid,r:0,c:0}; SEL={sid:SEL.sid,r:lastDataRow(sh),c:(sh.cols||[]).length-1}; applySelHi(); focusActive(); }
function selectRow(){ if(!SEL)return; const sh=BOOK.find(s=>s.id===SEL.sid); if(!sh)return; ANCH={sid:SEL.sid,r:SEL.r,c:0}; SEL={sid:SEL.sid,r:SEL.r,c:(sh.cols||[]).length-1}; applySelHi(); focusActive(); }
function selectCol(){ if(!SEL)return; const sh=BOOK.find(s=>s.id===SEL.sid); if(!sh)return; ANCH={sid:SEL.sid,r:0,c:SEL.c}; SEL={sid:SEL.sid,r:lastDataRow(sh),c:SEL.c}; applySelHi(); focusActive(); }
function sheetKey(ev){                                        // the mode controller (fires when a grid cell has focus)
  if(!SEL)return; const a=document.activeElement; if(!a||!a.classList||!a.classList.contains('wbc'))return;
  const sh=BOOK.find(s=>s.id===SEL.sid); if(!sh)return; const k=ev.key;
  const altgr=!!(ev.getModifierState&&ev.getModifierState('AltGraph'));      // AltGr (Ctrl+Alt on Windows) makes @ { } [ ] € etc. — NOT a shortcut
  const ctrl=(ev.ctrlKey||ev.metaKey)&&!ev.altKey&&!altgr;
  if(INEDIT){
    if(k==='Enter'&&!ev.shiftKey){ ev.preventDefault(); commitEdit(); selCell(SEL.sid,SEL.r+1,SEL.c); }
    else if(k==='Tab'){ ev.preventDefault(); commitEdit(); selCell(SEL.sid,SEL.r,SEL.c+(ev.shiftKey?-1:1)); }
    else if(k==='Escape'){ ev.preventDefault(); cancelEdit(); }
    return;                                                  // everything else edits the text (caret keys, etc.)
  }
  if(ctrl){                                                  // command shortcuts (Ctrl+C/X/V ride their own clipboard events)
    const lk=k.toLowerCase();
    if(lk==='z'&&!ev.shiftKey){ ev.preventDefault(); undo(); return; }
    if(lk==='y'||(lk==='z'&&ev.shiftKey)){ ev.preventDefault(); redo(); return; }
    if(lk==='a'){ ev.preventDefault(); selectAll(); return; }
    if(lk==='d'){ ev.preventDefault(); fillDown(); return; }
    if(lk==='r'){ ev.preventDefault(); fillRight(); return; }
    if(k===' '){ ev.preventDefault(); selectCol(); return; }
    return;                                                  // leave Ctrl+C/X/V for the copy/cut/paste handlers
  }
  if(k==='ArrowUp'){ ev.preventDefault(); selCell(SEL.sid,SEL.r-1,SEL.c,ev.shiftKey); }
  else if(k==='ArrowDown'){ ev.preventDefault(); selCell(SEL.sid,SEL.r+1,SEL.c,ev.shiftKey); }
  else if(k==='ArrowLeft'){ ev.preventDefault(); selCell(SEL.sid,SEL.r,SEL.c-1,ev.shiftKey); }
  else if(k==='ArrowRight'){ ev.preventDefault(); selCell(SEL.sid,SEL.r,SEL.c+1,ev.shiftKey); }
  else if(k===' '&&ev.shiftKey){ ev.preventDefault(); selectRow(); }
  else if(k==='Tab'){ ev.preventDefault(); selCell(SEL.sid,SEL.r,SEL.c+(ev.shiftKey?-1:1)); }
  else if(k==='Enter'||k==='F2'){ ev.preventDefault(); beginEdit(); }
  else if(k==='Delete'||k==='Backspace'){ ev.preventDefault(); clearRange(); }
  else if(k==='Escape'){ SEL=null; ANCH=null; document.querySelectorAll('#sheetcard td.wbc.sel,#sheetcard td.wbc.insel').forEach(x=>x.classList.remove('sel','insel')); }
  else if(k.length===1&&!(ev.altKey&&!ev.ctrlKey)){ ev.preventDefault(); beginEdit(k); }   // type to overwrite (AltGr chars pass; plain Alt+key doesn't)
}
function serializeRange(){ const rc=selRect(); const sh=BOOK.find(s=>s.id===rc.sid); if(!sh)return '';   // selection -> TSV (Excel/Sheets paste-in format)
  const lines=[]; for(let r=rc.r0;r<=rc.r1;r++){ const cells=[]; for(let c=rc.c0;c<=rc.c1;c++)cells.push((sh.rows[r]&&sh.rows[r][c]!=null)?String(sh.rows[r][c]):''); lines.push(cells.join('\t')); } return lines.join('\n'); }
function cellCopy(ev){ if(!SEL||INEDIT)return; if(ev.clipboardData){ ev.clipboardData.setData('text/plain', serializeRange()); ev.preventDefault(); } }
function cellCut(ev){ if(!SEL||INEDIT||!sheetEditable(SEL.sid))return; if(ev.clipboardData){ ev.clipboardData.setData('text/plain', serializeRange()); ev.preventDefault(); clearRange(); } }
// Excel, Google Sheets and this grid's own copy all put TSV on the clipboard (TAB = column, NEWLINE = row); a cell
// containing a tab/newline/quote is wrapped in double quotes, with "" for a literal quote. Parse in ONE quote-aware
// pass so an in-cell line break isn't torn into two rows, and a literal comma stays literal (never a delimiter).
function parseClipboard(txt){
  txt=txt.replace(/\r\n/g,'\n').replace(/\r/g,'\n');
  const rows=[]; let row=[],cur='',q=false,any=false;
  for(let i=0;i<txt.length;i++){ const ch=txt[i];
    if(q){ if(ch==='"'){ if(txt[i+1]==='"'){cur+='"';i++;} else q=false; } else cur+=ch; }
    else if(ch==='"'){ q=true; any=true; }
    else if(ch==='\t'){ row.push(cur); cur=''; any=true; }
    else if(ch==='\n'){ row.push(cur); rows.push(row); row=[]; cur=''; any=false; }
    else { cur+=ch; any=true; }
  }
  if(any||cur!==''||row.length){ row.push(cur); rows.push(row); }
  return rows;
}
function cellPasteEvent(ev){                                  // paste a TSV block from Excel/Sheets -> REPLACES the selected cells
  if(!SEL||!sheetEditable(SEL.sid))return; const cd=ev.clipboardData||window.clipboardData; const txt=cd&&cd.getData('text'); if(txt==null||txt==='')return;
  if(INEDIT&&!/[\t\n]/.test(txt))return;                     // single value while editing -> let the browser paste into the caret
  ev.preventDefault(); if(INEDIT)cancelEdit(); INEDIT=false;
  const rc=selRect(); const sh=BOOK.find(s=>s.id===rc.sid); if(!sh)return;
  const grid=parseClipboard(txt); if(!grid.length)return;
  const single=grid.length===1&&grid[0].length===1;
  const blockW=single?(rc.c1-rc.c0+1):grid.reduce((m,g)=>Math.max(m,g.length),0);
  const needCols=single?(rc.c1+1):(rc.c0+blockW);            // grow columns to fit rather than silently dropping the overflow
  let changed=false;                                         // no-op guard: a paste that changes nothing must not push undo / wipe redo / nag Recalculate
  if(single){ const v=String(grid[0][0]); for(let r=rc.r0;r<=rc.r1&&!changed;r++)for(let c=rc.c0;c<=rc.c1;c++){ if(r>=sh.rows.length||cellVal(sh,r,c)!==v){changed=true;break;} } }
  else { for(let ri=0;ri<grid.length&&!changed;ri++)for(let ci=0;ci<grid[ri].length;ci++){ const r=rc.r0+ri,c=rc.c0+ci; if(r>=sh.rows.length||c>=sh.cols.length||cellVal(sh,r,c)!==String(grid[ri][ci])){changed=true;break;} } }
  if(!changed)return;
  pushUndo(rc.sid);
  while(sh.cols.length<needCols){ sh.cols.push('column '+(sh.cols.length+1)); sh.rows.forEach(rw=>{ while(rw.length<sh.cols.length)rw.push(''); }); }
  if(single){                                                // one value pasted over a range -> fill the whole selection with it
    const v=String(grid[0][0]);
    for(let r=rc.r0;r<=rc.r1;r++){ while(sh.rows.length<=r)sh.rows.push(sh.cols.map(()=>'')); for(let c=rc.c0;c<=rc.c1;c++)sh.rows[r][c]=v; }
  } else {                                                   // a block -> anchor at the top-left of the selection, replacing outward
    grid.forEach((cells,ri)=>{ const r=rc.r0+ri; while(sh.rows.length<=r)sh.rows.push(sh.cols.map(()=>''));
      cells.forEach((v,ci)=>{ const c=rc.c0+ci; if(c<sh.cols.length)sh.rows[r][c]=String(v); }); });
    ANCH={sid:rc.sid,r:rc.r0,c:rc.c0}; SEL={sid:rc.sid,r:rc.r0+grid.length-1,c:Math.min(sh.cols.length-1,rc.c0+blockW-1)};   // select the pasted block
  }
  markDirtySheet(sh); paint(); focusActive();
}
function delRow(sid,r){
  const sh=BOOK.find(s=>s.id===sid); if(!sh||!sh.rows[r]||!sheetEditable(sid))return;
  pushUndo(sid); sh.rows.splice(r,1); markDirtySheet(sh);
  if(SEL&&SEL.sid===sid){                                     // keep the selection rectangle, shifted up for the removed row (Excel behavior)
    if(SEL.r>r)SEL.r--; SEL.r=Math.min(SEL.r,Math.max(0,sh.rows.length-1));
    if(ANCH&&ANCH.sid===sid){ if(ANCH.r>r)ANCH.r--; ANCH.r=Math.min(ANCH.r,Math.max(0,sh.rows.length-1)); }
    else ANCH={sid:sid,r:SEL.r,c:SEL.c};
  }                                                           // SEL on another sheet -> leave it (and ANCH) untouched
  paint(); focusActive();                                     // restore grid focus so keyboard nav / copy-paste keep working
}
function wireGrid(){                                          // one delegated set of listeners on the (stable) sheet container
  const sc=$('sheetcard'); if(!sc||sc._wired)return; sc._wired=true;
  sc.addEventListener('mousedown', ev=>{ const td=ev.target.closest('td.wbc'); if(!td)return;
    if(INEDIT&&SEL&&td===cellEl(SEL.sid,SEL.r,SEL.c))return;  // click inside the editing cell -> place caret
    const ext=ev.shiftKey&&SEL&&SEL.sid===td.dataset.sid;
    selCell(td.dataset.sid,+td.dataset.r,+td.dataset.c,ext);
    if(!INEDIT)DRAG=true;                                     // begin a drag-select
  });
  sc.addEventListener('mouseover', ev=>{ if(!DRAG||INEDIT)return; const td=ev.target.closest('td.wbc'); if(!td||td.dataset.sid!==SEL.sid)return;
    selCell(td.dataset.sid,+td.dataset.r,+td.dataset.c,true); });
  document.addEventListener('mouseup', ()=>{ DRAG=false; });
  sc.addEventListener('dblclick', ev=>{ const td=ev.target.closest('td.wbc'); if(td){ selCell(td.dataset.sid,+td.dataset.r,+td.dataset.c); beginEdit(); } });
  sc.addEventListener('keydown', sheetKey);
  sc.addEventListener('copy', cellCopy);
  sc.addEventListener('cut', cellCut);
  sc.addEventListener('paste', cellPasteEvent);
  sc.addEventListener('focusout', ev=>{ if(INEDIT){ const to=ev.relatedTarget; if(!to||!to.classList||!to.classList.contains('wbc')) commitEdit(); } });
}
function csvCell(v){ v=v==null?'':String(v); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }
function syncInputsToSheets(){                                // serialize edited input sheets back to SHEETS (+ persist) before a run
  BOOK.filter(s=>s.cls==='input').forEach(s=>{ if(s.si!=null&&SHEETS[s.si])
    SHEETS[s.si].data=[s.cols.map(csvCell).join(',')].concat((s.rows||[]).map(r=>r.map(csvCell).join(','))).join('\n'); });
  try{ sessionStorage.setItem(SS.TABLES, JSON.stringify(SHEETS)); }catch(_){}
}
function showRecalc(on){ renderSheet(); }   // the Recalculate bar is rendered by renderSheet (a masterhint-style top bar) when EDITED
function recalc(){                                            // re-run the last question on the edited data (auto-composed; no retyping)
  if(!SETTLED&&!FAILMSG) return;                             // one run at a time
  syncInputsToSheets();                                      // serialize the edits INTO SHEETS before the run (recalc clears EDITED, so startTurn can't)
  showRecalc(false); EDITED=false; stampBaseline();          // the data we're about to compute becomes the new pristine baseline
  const q=LASTQ||question; if(!q) return;
  archiveTurn(); question=q; try{ sessionStorage.setItem(SS.Q,q); }catch(_){}
  resetRun(); paint(); startRun();
}

/* ---- MASTER DATA: the user's own reference tables for private entities (product, rep, region...) that the
   world model doesn't know. Stored per-user server-side (/api/master), so they're shared across every
   conversation. Unresolved text columns surface here as empty sheets to fill and reuse. ---- */
let MSEEN=new Set();                                          // master/unresolved names already surfaced (no dupes)
const referenceKey=(name,cols)=>String((cols&&cols[0])||name||'').trim().toLowerCase();
function addMasterSheet(name,cols,rows,saved,dirty){
  const id='m'+(BOOK.filter(s=>s.cls==='master').length)+'_'+Math.random().toString(36).slice(2,6);
  addSheet({id, cls:'master', name, cols:cols&&cols.length?cols:[name], rows:rows||[], saved:!!saved, dirty:!!dirty});
  MSEEN.add(referenceKey(name,cols));
  return id;
}
let MDATA={};                                                // cache aliases by table name and first-column join key
let MASTER_READY=null;                                       // resolves once loadMaster has cached the server's saved references (guards the blank-shadow autosave race)
function cacheMaster(sh){
  Object.keys(MDATA).forEach(key=>{ if(MDATA[key]&&MDATA[key].name===sh.name) delete MDATA[key]; });
  const data={name:sh.name,cols:(sh.cols||[]).slice(),rows:(sh.rows||[]).map(row=>row.slice())};
  MDATA[String(sh.name||'').toLowerCase()]=data; MDATA[referenceKey(sh.name,sh.cols)]=data;
}
function inputColumns(){                                      // {lc colname -> original} across the user's input sheets
  const m={}; BOOK.filter(s=>s.cls==='input').forEach(sh=>(sh.cols||[]).forEach(c=>{ const k=String(c||'').trim().toLowerCase(); if(k)m[k]=c; })); return m;
}
async function loadMaster(){                                  // cache the user's master tables; SHOW only the ones relevant to THIS data
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/master',{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok)return; const j=await r.json();
    for(const t of (j.tables||[])){
      const full=await fetch(API_BASE+'/api/master?name='+encodeURIComponent(t.name),{headers:{Authorization:'Bearer '+tk}}).then(x=>x.ok?x.json():null).catch(()=>null);
      if(full&&full.columns){ const d={name:full.name,cols:full.columns,rows:full.rows};
        MDATA[String(full.name).toLowerCase()]=d; MDATA[referenceKey(full.name,full.columns)]=d; }
    }
    const cols=inputColumns();                                // a saved master shows only if its name matches a column in this conversation's data (skip unrelated like "product")
    const colVals=k=>{                                        // distinct lower-cased values of THIS data's column named k
      for(const sh of BOOK.filter(s=>s.cls==='input')){ const ci=(sh.cols||[]).findIndex(c=>String(c).toLowerCase()===k);
        if(ci>=0) return new Set((sh.rows||[]).map(r=>String(r[ci]==null?'':r[ci]).trim().toLowerCase()).filter(Boolean)); }
      return new Set(); };
    const unique=[...new Map(Object.values(MDATA).map(d=>[String(d.name).toLowerCase(),d])).values()];
    unique.forEach(d=>{ const k=referenceKey(d.name,d.cols); if(!cols[k])return;
      // RELEVANCE: a saved reference belongs to THIS conversation only if its entities (col 0) actually appear in
      // the data's column of that name — otherwise a generic column like "name" surfaces an UNRELATED saved table
      // (e.g. a customers "name"/segment table shown over detective names — a pure name collision).
      const ents=new Set((d.rows||[]).map(r=>String((r&&r[0])==null?'':r[0]).trim().toLowerCase()).filter(Boolean));
      if(ents.size){ const vals=colVals(k); if(![...ents].some(v=>vals.has(v))) return; }   // no overlapping entity -> not for this data
      const existing=BOOK.find(s=>s.cls==='master'&&referenceKey(s.name,s.cols)===k);
      if(existing){                                           // surfaceUnresolved won the race and showed a BLANK shadow -> upgrade it in place with the saved data
        if(!existing.saved&&!existing.dirty){ if(d.cols&&d.cols.length)existing.cols=d.cols; existing.rows=(d.rows||[]).map(r=>r.slice()); existing.saved=true; }
      } else if(!MSEEN.has(k)){ addMasterSheet(d.name,d.cols,d.rows,true); }   // (if the user already edited the shadow, leave their edits — a Save will overwrite the server intentionally)
    });
    paint();
  }catch(_){}
}
// After a query, the engine marks each resolved column as connected (wtable) or NOT (unconnected:true).
// The unconnected ones are the private entities the world model can't enrich — surface each as a master
// sheet (its saved data if any, else its distinct values) for the user to enrich.
let REFCANDS=[];                                              // A1: reference-data CANDIDATES, offered as one collapsed suggestion (not N tabs)
function surfaceUnresolved(){
  try{
    const seen=[];                                            // normalized value-sets already surfaced or offered (dedup)
    (RESOLVES||[]).forEach(r=>{
      if(!r||!r.unconnected)return;                          // only columns the engine could NOT connect to the world
      const nm=String(r.column||'').trim(), key=nm.toLowerCase();
      if(!nm||MSEEN.has(key)||REFCANDS.some(c=>c.key===key))return;   // already shown, surfaced, or already a candidate
      let vals=[];                                           // its distinct values, from whichever input sheet has the column
      for(const sh of BOOK.filter(s=>s.cls==='input')){
        const ci=(sh.cols||[]).findIndex(c=>String(c).toLowerCase()===key);
        if(ci>=0){ vals=[...new Set((sh.rows||[]).map(rw=>rw[ci]).filter(v=>v!==''&&v!=null).map(String))]; break; }
      }
      const norm=new Set(vals.map(v=>String(v).trim().toLowerCase()));
      const saved=MDATA[key];
      if(saved){                                             // A2: only surface a SAVED reference whose entities OVERLAP this data
        const ents=new Set((saved.rows||[]).map(rw=>String((rw&&rw[0])==null?'':rw[0]).trim().toLowerCase()).filter(Boolean));
        if(ents.size && ![...ents].some(v=>norm.has(v))) return;   // zero entity overlap -> unrelated reference, don't surface
        addMasterSheet(saved.name,saved.cols,saved.rows,true); seen.push(norm); return;
      }
      // A1: skip a DUPLICATE column (orders.customer == customers.name) and FREE-TEXT / line-item columns (e.g. 'ordered').
      if(norm.size && seen.some(prev=>[...norm].every(v=>prev.has(v)))) return;
      const avgLen=vals.length?vals.reduce((a,v)=>a+String(v).length,0)/vals.length:0;
      if(vals.length>=6 && (avgLen>22 || vals.length>80)) return;   // long descriptive values -> not a reference entity
      REFCANDS.push({name:nm,key,vals:vals.slice(0,500)}); seen.push(norm);   // collect as ONE suggestion — no auto-opened tab
    });
  }catch(_){}
}
function acceptRefCand(i){                                    // A1: user accepts a candidate -> SHOW that column as a reference sheet (leaves AVAILABLE)
  const c=REFCANDS[i]; if(!c)return; REFCANDS.splice(i,1);
  let cols=null, rows=null, saved=!!c.saved, dirty=!!c.dirty;
  const md=MDATA[c.key];
  if(saved&&!dirty&&md&&md.cols&&md.cols.length){ cols=md.cols.slice(); rows=(md.rows||[]).map(r=>r.slice()); }
  if(!cols&&(c.cols&&c.cols.length)){ cols=c.cols; rows=c.rows; }                // a removed local draft carries its full data back
  if(!cols&&md&&md.cols&&md.cols.length){ cols=md.cols.slice(); rows=(md.rows||[]).map(r=>r.slice()); saved=true; }
  if(!cols){ cols=[c.name]; rows=(c.vals||[]).map(v=>[v]); saved=false; }        // ...else a bare entity column to enrich
  const id=addMasterSheet(c.name, cols, rows, saved, dirty); const sh=BOOK.find(s=>s.id===id);
  if(sh&&c.cellAI) sh.cellAI=new Set(c.cellAI); pick(id); saveConvState();
}
function refSuggestMenu(btn,ev){                             // the "+ Reference" popover — reference data available to show as a sheet
  if(ev) ev.stopPropagation(); closeMasterMenu();
  const el=document.createElement('div'); el.id='mmenu'; el.className='mmenu';
  el.innerHTML='<div class=mmenu-hd>Add reference data as a sheet</div>'
    + REFCANDS.map((c,i)=>'<button onclick="closeMasterMenu();acceptRefCand('+i+')">'+esc(c.name)+' <span class=mmenu-sub>'+((c.cols&&c.cols.length>1)?(c.cols.length-1)+' column'+(c.cols.length===2?'':'s'):(c.vals.length+' value'+(c.vals.length===1?'':'s')))+'</span></button>').join('');
  document.body.appendChild(el);
  const r=btn.getBoundingClientRect();
  el.style.left=Math.max(8,r.left)+'px'; el.style.top=Math.max(8,r.top-el.offsetHeight-6)+'px';   // open ABOVE the bottom tab bar
  _mmenuDoc=e=>{ if(!el.contains(e.target)) closeMasterMenu(); };
  setTimeout(()=>document.addEventListener('mousedown',_mmenuDoc),0);
}
const hasCellValue=v=>v!=null&&String(v).trim()!=='';
const referenceRows=s=>(s.rows||[]).filter(r=>r.some(hasCellValue));
const masterSig=s=>JSON.stringify({cols:s.cols, rows:referenceRows(s)});
async function persistMaster(sh, tk){                        // POST one reference sheet; rejects with the server's actionable error (or a timeout)
  const rows=referenceRows(sh);                              // drop blank trailing rows, preserving numeric zero
  const ctl=(typeof AbortController!=='undefined')?new AbortController():null;
  const timer=ctl?setTimeout(()=>ctl.abort(),45000):null;    // a save gates the turn — never wedge on a stalled POST; fail cleanly so it's retryable
  try{
    const r=await fetch(API_BASE+'/api/master',{method:'POST',
      headers:{'content-type':'application/json','Authorization':'Bearer '+tk},
      body:JSON.stringify({name:sh.name, columns:sh.cols, rows}), signal:ctl?ctl.signal:undefined});
    let body={}; try{ body=await r.json(); }catch(_){}
    if(!r.ok||body.error) throw new Error(body.error||('HTTP '+r.status));
    return body;
  }catch(e){ if(ctl&&e&&e.name==='AbortError') throw new Error('saving reference data timed out — please try again'); throw e; }
  finally{ if(timer)clearTimeout(timer); }
}
async function saveMaster(id){
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  const btn=document.querySelector('.msave'); if(btn){ btn.textContent='Saving…'; btn.disabled=true; }
  try{ const tk=await window.ensureToken();
    const sentSig=masterSig(sh);                             // exactly what this POST persists (guards against an edit landing mid-flight)
    await persistMaster(sh, tk); sh.saved=true;
    try{ if(!localStorage.getItem('pr_ref_explained')){ localStorage.setItem('pr_ref_explained','1');   // D4: one-time explainer
      toast('Saved as reference — “'+sh.name+'” will now appear in your other conversations too.', 'Got it', null, 9000); } }catch(_){}
    if(masterSig(sh)===sentSig){ sh.dirty=false; cacheMaster(sh); }   // an edit during the POST remains dirty and out of the server cache
  }catch(e){ toast('Could not save “'+sh.name+'”: '+(e&&e.message||e), null, null, 9000); }
  paint();                                                   // refresh the sheet (Save->Saved) AND the tab strip (drop the unsaved dot)
  saveConvState();                                           // fold the saved master into the conversation snapshot
}
async function autosaveRefs(){                               // auto-save-on-use: persist shown enriched references before a turn
  let pending=BOOK.filter(s=>s.cls==='master' &&
    ((s.saved&&s.dirty) || (!s.saved&&(s.cols||[]).length>1&&(s.rows||[]).some(r=>r.some(hasCellValue)))));
  if(!pending.length) return false;
  let ready=false;                                            // did loadMaster's cache settle in time? (bounded so a stalled loadMaster never hangs the turn)
  try{ ready=await Promise.race([Promise.resolve(MASTER_READY).then(()=>true,()=>true), new Promise(res=>setTimeout(()=>res(false),8000))]); }catch(_){ ready=false; }
  const entities=t=>new Set((t.rows||[]).map(r=>String((r&&r[0])==null?'':r[0]).trim().toLowerCase()).filter(Boolean));   // col-0 join keys
  pending=pending.filter(sh=>{
    if(sh.saved) return true;                                 // an intentionally-edited saved reference is a full sheet -> safe to persist
    if(!ready) return false;                                  // server state unknown -> don't risk overwriting a saved copy; this reference just joins on the next turn
    const prior=MDATA[referenceKey(sh.name,sh.cols)]||MDATA[String(sh.name||'').toLowerCase()];
    if(!prior) return true;                                   // no server copy of this name -> a genuinely new reference, persist it
    const pe=entities(prior), sameRef=[...entities(sh)].some(v=>pe.has(v));   // shares entities -> the SAME reference (a partial shadow), not a mere name collision
    const fuller=(prior.cols||[]).length>(sh.cols||[]).length
      || (prior.rows||[]).filter(r=>r.some(hasCellValue)).length>referenceRows(sh).length;
    return !(sameRef&&fuller);                                // suppress ONLY a partial shadow of the same, fuller server reference (an explicit Save can still replace it)
  });
  if(!pending.length) return false;
  const tk=await window.ensureToken();
  let changed=false;
  try{ for(const sh of pending){
    const sig=masterSig(sh); await persistMaster(sh, tk); sh.saved=true;
    if(masterSig(sh)===sig){ sh.dirty=false; cacheMaster(sh); }
    else throw new Error('“'+sh.name+'” changed while it was being saved; run the query again.');
    changed=true;
  } } finally { if(changed){ paint(); saveConvState(); } }
  return changed;
}
// The DEFAULT generation prompt, aware of context: first run -> "add columns + fill" (referencing the uploaded
// data so the columns are useful alongside it); already-generated with empty cells (the input CSV grew, so new
// entities were surfaced) -> "fill only the missing cells, keep the rest".
function genPromptDefault(sh){
  const inputCols=Object.values(inputColumns()).filter(c=>String(c).toLowerCase()!==String(sh.name||'').toLowerCase());
  const attr=(sh.cols||[]).slice(1);                          // the non-entity (attribute) columns
  const real=(sh.rows||[]).filter(r=>hasCellValue((r||[])[0]));
  const filled=attr.length&&real.some(r=>attr.some((c,i)=>hasCellValue(r[i+1])));
  const empty =attr.length&&real.some(r=>attr.some((c,i)=>!hasCellValue(r[i+1])));
  if(filled&&empty)                                           // partially filled -> the CSV added rows -> fill the gaps
    return 'Fill in only the empty cells for "'+sh.name+'", keeping every existing value exactly as it is.';
  return 'Add useful reference columns about each "'+sh.name+'" and fill in a value for every row'
       +(inputCols.length?', so this reference data works alongside my uploaded spreadsheet (columns: '+inputCols.join(', ')+')':'')+'.';
}
function generateMaster(id){ const sh=BOOK.find(s=>s.id===id); if(sh) openGenModal(sh); }   // open the editable-prompt popup
function closeGenModal(){ const ov=document.getElementById('genmodal'); if(ov) ov.remove(); }
function openGenModal(sh){
  closeGenModal();
  const ov=document.createElement('div'); ov.id='genmodal'; ov.className='genbackdrop';
  ov.onclick=e=>{ if(e.target===ov) closeGenModal(); };
  ov.innerHTML='<div class=gencard role=dialog aria-modal=true>'
    +'<div class=genhd>Autofill reference data for <b>'+esc(sh.name)+'</b></div>'
    +'<div class=gensub>Edit the prompt if you like, then Generate. Columns are filled with AI; any values you already entered are kept.</div>'
    +'<textarea class=genta id=genta spellcheck=false>'+esc(genPromptDefault(sh))+'</textarea>'
    +'<div class=genmsg id=genmsg></div>'
    +'<div class=genbtns><button type=button class=gencancel onclick="closeGenModal()">Cancel</button>'
    +'<button type=button class=genrun id=genrun onclick="runGenerate(\''+sh.id+'\')">Autofill</button></div></div>';
  document.body.appendChild(ov);
  const ta=document.getElementById('genta'); if(ta){ ta.focus(); ta.setSelectionRange(ta.value.length,ta.value.length); }
}
// Fire the fill job and render the master sheet LIVE as Sonnet streams: the header arrives (columns appear),
// then each row fills in as it completes. RTDB is the primary channel (decoupled from the 60s proxy timeout);
// the HTTP body is the warm/fast fallback. Progress rides on the master sheet's own Generate button (sh._gen),
// so it survives every per-row re-render.
async function runGenerate(id){
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  const ta=document.getElementById('genta'); const instruction=ta?ta.value:'';
  closeGenModal();                                            // reveal the sheet so the user watches it fill
  const nReal=(sh.rows||[]).filter(r=>hasCellValue((r||[])[0])).length;
  const preFilled=new Set();                                   // B3: cells the user already had before this autofill stay "user", not AI
  (sh.rows||[]).forEach((r,ri)=>(r||[]).forEach((v,ci)=>{ if(ci>=1&&hasCellValue(v)) preFilled.add(ri+','+ci); }));
  const setGen=(lbl,busy)=>{ sh._gen=lbl; sh._genBusy=!!busy; renderSheet(); };   // reflected by the masterhint render
  setGen('Filling…', true);
  try{
    const tk=await window.ensureToken(), uid=window.__uid;
    const jobId=(crypto&&crypto.randomUUID)?crypto.randomUUID():(Date.now()+'-'+Math.random().toString(36).slice(2));
    const rows=(sh.rows||[]).filter(r=>hasCellValue((r||[])[0]));   // real entities only, IN ORDER (index == mrows key)
    let done=false, unsub=null, timer=null, got=0;
    const finish=()=>{ done=true; if(unsub){try{unsub();}catch(_){}} if(timer)clearTimeout(timer); };
    const padRows=()=>{ const w=(sh.cols||[]).length; (sh.rows||[]).forEach(r=>{ while(r.length<w)r.push(''); }); };
    const applyCols=cols=>{ if(!cols||!cols.length||done)return; sh.cols=cols.slice(); padRows(); sh.dirty=true; setGen('Filling…',true); };
    const applyRow=(idx,cells)=>{ if(!Array.isArray(cells)||done)return;
      while(sh.rows.length<=idx) sh.rows.push((sh.cols||[]).map(()=>''));
      sh.cellAI=sh.cellAI||new Set();
      cells.forEach((v,ci)=>{ if(ci>=1&&hasCellValue(v)&&!preFilled.has(idx+','+ci)) sh.cellAI.add(idx+','+ci); });   // B3: mark autofilled cells
      sh.rows[idx]=cells.slice(); sh.dirty=true; got++; setGen('Filling… ('+Math.min(got,nReal||got)+(nReal?'/'+nReal:'')+')',true); };
    const complete=out=>{ if(done)return; finish();
      if(out&&out.columns&&out.columns.length){ sh.cols=out.columns.slice(); sh.rows=(out.rows||[]).map(x=>x.slice()); sh.dirty=true;
        sh.cellAI=new Set(); sh.rows.forEach((r,ri)=>(r||[]).forEach((v,ci)=>{ if(ci>=1&&hasCellValue(v)&&!preFilled.has(ri+','+ci)) sh.cellAI.add(ri+','+ci); })); }
      setGen(null,false); saveConvState(); };                 // -> button back to "Generate"
    if(uid&&window.subscribeRun){                             // (1) LIVE stream: header, then each row as it fills
      unsub=window.subscribeRun(uid,jobId,{
        onMasterCols:cols=>applyCols(cols),
        onMasterRow:(k,cells)=>applyRow(parseInt(k,10)||0, cells),
        onResult:v=>{ if(v&&v.columns) complete(v); },
        onStatus:st=>{ if(done)return; if(st==='done'){ finish(); setGen(null,false); saveConvState(); }   // streamed rows already applied; reset the button even if the result node was missed
                       else if(st==='error'){ finish(); setGen('Autofill failed — retry',false); saveConvState(); } } });
    }
    timer=setTimeout(()=>{ if(!done){ finish(); setGen('Autofill failed — retry',false); saveConvState(); } }, 180000);
    fetch(API_BASE+'/api/master/generate',{method:'POST',                         // (2) warm/fast fallback: the body returns the whole table
      headers:{'content-type':'application/json','Authorization':'Bearer '+tk},
      body:JSON.stringify({name:sh.name, columns:sh.cols, rows, instruction, jobId})})
      .then(async r=>{ if(!r.ok){ if(!done&&!uid) setGen('Autofill failed — retry',false); return; }
        const j=await r.json(); if(!j||!j.columns||done) return;
        if(!uid){ complete(j); return; }                    // no RTDB at all -> the HTTP body IS the result
        await new Promise(res=>setTimeout(res,3000));        // RTDB present: PREFER the live stream (onMasterRow renders row-by-row)
        if(!done && got===0) complete(j); })                 // ...only fall back to the body if the stream showed NO rows (RTDB silent)
      .catch(()=>{});                                          // proxy 60s timeout on a cold engine is EXPECTED; RTDB delivers
  }catch(_){ setGen('Autofill failed — retry',false); }
}
function removeMasterSheet(id){                               // the ONE remove: take a reference sheet out of the tabs and back under "+ Reference". Reversible, non-destructive.
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  const key=referenceKey(sh.name,sh.cols);
  const vals=[...new Set((sh.rows||[]).map(r=>String((r&&r[0])==null?'':r[0]).trim()).filter(Boolean))];
  const cand={name:sh.name, key, vals, cols:(sh.cols||[]).slice(), rows:(sh.rows||[]).map(r=>r.slice()), saved:!!sh.saved,
    dirty:!!sh.dirty, cellAI:sh.cellAI?[...sh.cellAI]:undefined};   // carry full state so re-add/reload is lossless
  const idx=BOOK.indexOf(sh);
  BOOK=BOOK.filter(s=>s.id!==id);
  if(!REFCANDS.some(c=>c.key===key)) REFCANDS.push(cand);      // now AVAILABLE under "+ Reference" (MSEEN keeps loadMaster from auto-showing it again)
  if(ACTIVE===id) ACTIVE=(BOOK.find(s=>s.cls==='input')||BOOK[0]||{}).id||null;
  paint(); saveConvState();
  toast('Moved “'+sh.name+'” to “+ Reference”.', 'Undo', ()=>{  // reversible — re-show it exactly as it was
    REFCANDS=REFCANDS.filter(c=>c.key!==key);
    BOOK.splice(Math.min(idx,BOOK.length),0,{id:sh.id, cls:'master', name:sh.name, cols:cand.cols, rows:cand.rows,
      saved:cand.saved, dirty:!!sh.dirty, cellAI:cand.cellAI?new Set(cand.cellAI):undefined});
    MSEEN.add(key); ACTIVE=sh.id; paint(); saveConvState();
  }, 8000);
}
function confirmRemoveMasterSheet(id){                        // remove from this workbook; the saved cross-conversation copy remains
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  if(!confirm('Remove “'+sh.name+'” from this conversation?\nIt stays saved — add it back any time from “+ Reference”.')) return;
  removeMasterSheet(id);
}
async function deleteMasterData(id){                          // permanently delete the authenticated user's saved copy
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  if(!confirm('Permanently delete the saved reference “'+sh.name+'”?\nThis removes it from every conversation and cannot be undone.')) return;
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/master/delete',{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+tk},body:JSON.stringify({name:sh.name})});
    let body={}; try{ body=await r.json(); }catch(_){}
    if(!r.ok||body.error) throw new Error(body.error||('HTTP '+r.status));
    const key=referenceKey(sh.name,sh.cols); Object.keys(MDATA).forEach(k=>{ if(MDATA[k]&&MDATA[k].name===sh.name) delete MDATA[k]; }); MSEEN.delete(key);
    REFCANDS=REFCANDS.filter(c=>c.key!==key); BOOK=BOOK.filter(s=>s.id!==id);
    if(ACTIVE===id) ACTIVE=(BOOK.find(s=>s.cls==='input')||BOOK[0]||{}).id||null;
    paint(); saveConvState(); toast('Deleted saved reference “'+sh.name+'”.', null, null, 7000);
  }catch(e){ toast('Could not delete “'+sh.name+'”: '+(e&&e.message||e), null, null, 9000); }
}
let _mmenuDoc=null;
function masterMenu(id, btn, ev){                             // the ⋮ menu on a saved reference sheet
  if(ev) ev.stopPropagation(); closeMasterMenu();
  const el=document.createElement('div'); el.id='mmenu'; el.className='mmenu';
  el.innerHTML='<button onclick="closeMasterMenu();generateMaster(\''+id+'\')">Autofill</button>'
    +'<button onclick="closeMasterMenu();masterUpload(\''+id+'\')">Upload</button>'
    +'<button onclick="closeMasterMenu();confirmRemoveMasterSheet(\''+id+'\')">Remove from workbook</button>'
    +'<button class=danger onclick="closeMasterMenu();deleteMasterData(\''+id+'\')"><span class=dgico aria-hidden=true>🗑</span> Delete saved reference</button>';
  document.body.appendChild(el);
  const r=btn.getBoundingClientRect();                        // anchor the dropdown to the ⋮ button, right-aligned + on-screen
  el.style.left=Math.max(8, r.right-el.offsetWidth)+'px'; el.style.top=(r.bottom+4)+'px';
  _mmenuDoc=e=>{ if(!el.contains(e.target)) closeMasterMenu(); };   // click OUTSIDE closes; clicks on a menu item run first
  setTimeout(()=>document.addEventListener('mousedown', _mmenuDoc), 0);
}
function closeMasterMenu(){
  if(_mmenuDoc){ document.removeEventListener('mousedown', _mmenuDoc); _mmenuDoc=null; }
  const m=document.getElementById('mmenu'); if(m) m.remove();
}
let _toastTimer=null;
function toast(msg, actionLabel, actionFn, ms){              // a transient bottom toast with an optional action (e.g. Undo)
  const old=document.getElementById('wbtoast'); if(old)old.remove(); if(_toastTimer)clearTimeout(_toastTimer);
  const el=document.createElement('div'); el.id='wbtoast'; el.className='wbtoast';
  el.innerHTML='<span>'+esc(msg)+'</span>'+(actionLabel?'<button type=button class=wbtoast-act>'+esc(actionLabel)+'</button>':'');
  document.body.appendChild(el);
  const close=()=>{ if(_toastTimer){clearTimeout(_toastTimer);_toastTimer=null;} const e=document.getElementById('wbtoast'); if(e)e.remove(); };
  if(actionLabel&&actionFn){ const b=el.querySelector('.wbtoast-act'); if(b) b.onclick=()=>{ close(); try{actionFn();}catch(_){} }; }
  _toastTimer=setTimeout(close, ms||6000);
}
function addMasterCol(id){                                    // add a column and edit its NAME inline (spreadsheet-style, no prompt() dialog)
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  for(let i=(sh.cols||[]).length-1;i>=1;i--){ if(String(sh.cols[i]||'').trim()===''){ sh.cols.splice(i,1); sh.rows.forEach(r=>r.splice(i,1)); } }   // drop abandoned unnamed columns first (no accumulation)
  sh._editCol=null; sh._editWasDirty=!!sh.dirty;
  sh.cols.push(''); sh.rows.forEach(r=>r.push('')); sh._editCol=sh.cols.length-1; sh.dirty=true;
  renderSheet();
  setTimeout(()=>{ const inp=document.querySelector('#sheetcard .colnameedit'); if(inp){ inp.focus(); inp.select&&inp.select(); } }, 0);
}
function editMasterCol(id, ci){ const sh=BOOK.find(s=>s.id===id); if(!sh)return;   // double-click a header to rename it inline
  sh._editWasDirty=!!sh.dirty; sh._editCol=ci; renderSheet();
  setTimeout(()=>{ const inp=document.querySelector('#sheetcard .colnameedit'); if(inp){ inp.focus(); inp.select&&inp.select(); } }, 0);
}
function commitColName(id, ci, val, original){
  const sh=BOOK.find(s=>s.id===id); if(!sh||sh._editCol==null)return; sh._editCol=null;
  const wasDirty=!!sh._editWasDirty; delete sh._editWasDirty; let changed=false;
  const nm=String(val||'').trim(), orig=String(original||'').trim();
  const dup=(sh.cols||[]).some((c,i)=>i!==ci&&String(c).trim().toLowerCase()===nm.toLowerCase());
  if(!nm||dup){
    if(orig){ sh.cols[ci]=orig; toast(dup?'Column names must be unique.':'A column name cannot be empty.', null, null, 6000); }
    else { sh.cols.splice(ci,1); sh.rows.forEach(r=>r.splice(ci,1)); sh.dirty=wasDirty; if(sh.cellAI) sh.cellAI=new Set([...sh.cellAI].flatMap(k=>{
      const p=k.split(','), c=+p[1]; return c===ci?[]:[p[0]+','+(c>ci?c-1:c)]; })); changed=true; }
  } else if(nm!==orig){ sh.cols[ci]=nm; sh.dirty=true; changed=true; }
  if(changed) saveConvState();
  renderSheet();
}
function masterUpload(id){
  const sh=BOOK.find(s=>s.id===id); if(!sh)return;
  const inp=document.createElement('input'); inp.type='file'; inp.accept='.csv,.tsv,.txt,text/csv';
  inp.onchange=()=>{ const f=inp.files&&inp.files[0]; if(!f)return; const rd=new FileReader();
    rd.onload=()=>{ const p=parseCSV(String(rd.result||'')); if(p.cols.length){ sh.cols=p.cols; sh.rows=p.rows; sh.dirty=true; renderSheet(); saveConvState(); } };
    rd.readAsText(f); };
  inp.click();
}
// This run just produced its OWN first sheet -> retire the previous turn's derivation/reference sheets that
// resetRun kept around (so a conversational follow-up could still show them). A data query replaces them.
function dropStale(){
  if(!BOOK.some(s=>s.stale))return;
  BOOK=BOOK.filter(s=>!s.stale);
  if(!BOOK.some(s=>s.id===ACTIVE)) ACTIVE=BOOK.length?BOOK[0].id:null;
}
// Short, logical step names (by op) — readable at a glance ("combined", "reference lookup", "filtered",
// "total") instead of the engine's verbose "join orders + customers" / "where country = 'France'".
const SHORTLBL={join:'combined',world_join:'reference lookup',world_filter:'filtered',filter:'filtered',
  time_filter:'date filter',having:'filtered',group_agg:'total',yoy:'year-over-year',running:'running total',
  divide:'ratio',share:'share',topn:'top results',sort:'sorted'};
function stepLabel(v){
  if(v&&v.op==='group_agg'){ const s=String((v.sql||'')+' '+(v.label||'')).toLowerCase();
    if(/\bcount\b/.test(s))return 'count'; if(/\bavg\b|average/.test(s))return 'average'; if(/\bmin\b|\bmax\b/.test(s))return 'extremes'; return 'total'; }
  return (v&&SHORTLBL[v.op])||(v&&v.label)||oplabel(v&&v.op);
}
// A plain-English sentence for what a step does — shown in the Reasoning steps (the short name labels the tab).
function humanCond(c){ return String(c||'').replace(/\s*<>\s*/,' is not ').replace(/\s*=\s*/,' is ').replace(/'/g,'').trim(); }
function stepDesc(v){
  const lbl=(v&&v.label)||'', op=v&&v.op;
  if(op==='join'){ const t=lbl.replace(/^join\s+/i,'').replace(/\s*\+\s*/g,' and '); return 'Combined '+(t||'your tables')+' into one table.'; }
  if(op==='world_join'){ const m=lbl.match(/on\s+(.+)$/i); return 'Looked up shared facts for each '+(m?m[1].trim():'entity')+' from the source named in the answer provenance.'; }
  if(op==='world_filter'||op==='filter'||op==='having'){ const m=lbl.match(/where\s+(.+)$/i); return m?('Kept only the rows where '+humanCond(m[1])+'.'):'Filtered to the matching rows.'; }
  if(op==='time_filter'){ return 'Kept only the rows in that time period.'; }
  if(op==='convert'){ return 'Converted each amount at its ECB reference rate — the rate and its publication date are columns on this sheet, so the Result is just the converted column summed.'; }
  if(op==='group_agg'){ return ({count:'Counted the rows.',average:'Averaged the values.',extremes:'Found the highest and lowest values.'})[stepLabel(v)]||'Added up the values to get the total.'; }
  if(op==='topn'){ return 'Kept just the top-ranked results.'; }
  if(op==='sort'){ return 'Sorted the results in order.'; }
  if(op==='yoy'){ return 'Computed the year-over-year change.'; }
  if(op==='running'){ return 'Computed a running (cumulative) total.'; }
  if(op==='share'){ return 'Computed each row’s share of the total.'; }
  if(op==='divide'){ return 'Computed the ratio between the two measures.'; }
  return lbl||stepLabel(v);
}
function stepStatus(v){                                       // a friendly, plain-English "working…" line (no internal step names)
  switch(v&&v.op){
    case 'join': return 'Combining your tables…';
    case 'world_join': return 'Looking up world facts…';
    case 'world_filter': case 'filter': case 'having': return 'Filtering the rows…';
    case 'group_agg': return 'Crunching the numbers…';
    case 'order': case 'sort': return 'Sorting the results…';
    case 'divide': return 'Working out the ratio…';
    default: return 'Working it out…';
  }
}
function appendView(v){
  dropStale();
  VIEWS.push(v); J=J||{}; J.views=VIEWS; if(v.sql&&!J.sql)J.sql=v.sql;
  const label=stepLabel(v);                                  // short logical name (never v1/step_1/b2)
  STATUS=stepStatus(v);
  addSheet({id:'v'+RUN+'_'+VIEWS.length, cls:'deriv', name:label, desc:stepDesc(v), cols:v.columns||[], rows:v.rows||[], sql:v.sql||''});
}
function appendResolve(r){
  dropStale();
  RESOLVES.push(r);
  STATUS='Looking up '+(r.column||'the world')+'…';
  if(!r.unconnected&&r.columns)
    addSheet({id:'r'+RUN+'_'+RESOLVES.length, cls:'ref', name:(r.wtable||'world'), cols:r.columns||[], rows:r.rows||[]});   // just "city", not "city (wikipedia)"
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
  surfaceUnresolved();                                        // offer master-data sheets for text columns not in the world model
  paint();
  if(PRESENT) tryPresent();                                   // real answer + human phrasing -> Sonnet presents it (derivation stays in the panel)
  else saveConvState();                                       // (present path re-saves once Sonnet's reply lands) persist a restorable snapshot
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
    BOOK=BOOK.filter(s=>s.cls==='input'||s.cls==='master'||s.stale);   // keep the user's tables + master data (drop the derivation, stale)
    if(!BOOK.some(s=>s.id===ACTIVE)){ const last=BOOK.filter(s=>s.stale).pop(); ACTIVE=(last&&last.id)||(BOOK.length?BOOK[0].id:null); }
  }
  CONV=null; CONVPROP=(c&&c.proposed)||null; CONVPENDING=true; STATUS=present?'Putting it in context…':'Thinking…'; renderRail();
  let reply=null;
  // A background presentation nicety must never be the thing that prompts for AI consent — without a
  // stored "Yes" it is skipped entirely (no data leaves) and the local fallback text below is used.
  try{
    const token=await window.ensureToken();
    const body={question:c.question,
      clarify:c.clarify?{proposed:c.proposed||null,original_sql:c.original_sql||null,bindings:c.bindings||null,
        reason:c.reason||null,unmet:c.unmet||null,calculations:c.calculations||null,
        currency:c.currency||null}:null,
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
  saveConvState();                                            // persist the presented/clarified answer so a reload restores it
}
function clarifyFallbackText(c){
  const p=(c&&c.proposed)||'';
  let t = (c&&c.reason) ? (String(c.reason)+'. ') :
    (p ? ('Did you mean “'+p+'”? ') : 'I couldn’t map that to a query over your sheets. ');
  if(p&&c&&c.reason)t+='Try “'+p+'”. ';
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
  LASTQ=question; if(EDITED){ syncInputsToSheets(); EDITED=false; showRecalc(false); } stampBaseline();   // pick up any input-cell edits; re-baseline what we're about to compute
  let token;
  try{ token=await window.ensureToken(); }
  catch(e){ if(RUN===myRun) fail('sign-in required to run on your data: '+(e&&e.message||e)); return; }
  const uid=window.__uid;
  const streaming=!!(uid&&window.subscribeTurn);
  const turnId=(crypto&&crypto.randomUUID)?crypto.randomUUID():(Date.now()+'-'+Math.random().toString(36).slice(2));
  const parseBody=async r=>{ try{ if(!r)return null; const t=await r.text(); return (r.ok&&t.trim().charAt(0)==='{')?JSON.parse(t):null; }catch(_){ return null; } };
  const httpPromise=fetch(CHAT_ENDPOINT,{method:'POST',
    headers:{'content-type':'application/json','Authorization':'Bearer '+token},
    body:JSON.stringify({message:question, tables:SHEETS, history:HISTORY, turnId:turnId,
      conversation_id:convId()})}).then(parseBody).catch(()=>null);
  // (1) LIVE: subscribe to the turn node -> render each announced engine call's trace as it streams. This is
  // the PRIMARY completion path: the Firebase Hosting proxy times out at ~60s but the engine cold start +
  // Sonnet loop can exceed that, so the answer often lands on RTDB after the HTTP call has already given up.
  if(streaming){
    UNSUB=window.subscribeTurn(uid,turnId,{
      onStatus:st=>{ if(!live())return; if(st==='done') markTurnDone(); if(st==='error') fail('the assistant hit an error'); },
      onCall:(k,c)=>{ if(!live()||!c||!c.jobId||SEEN_CALL.has(c.jobId))return; SEEN_CALL.add(c.jobId); addCall(uid,c); },
      onReply:t=>{ if(RUN!==myRun)return; if(t){REPLY=t;} if(!SETTLED)renderRail(); },
      onConversation:cid=>{ if(RUN===myRun) setConversation(cid); },   // stable conversation id -> persist + reflect in the URL
      onError:e=>{ if(!live())return; fail(e||'the assistant hit an error'); },
    });
  }
  // (2) HTTP body (blocking; {reply, traces, history}): the authoritative HISTORY + the completion path when
  // RTDB is off. On a null body (proxy 60s timeout on a cold engine) DO NOT fail while streaming — the RTDB
  // 'done' will finish the turn; only fail here if there's no stream to fall back on.
  httpPromise.then(j=>{ if(RUN!==myRun)return;
    if(j&&j.conversation_id) setConversation(j.conversation_id);
    if(j&&Array.isArray(j.history)){ HISTORY=j.history; HTTPHIST=true; }
    if(!j){ if(!streaming&&!SETTLED) fail('the assistant did not respond — please try again'); return; }
    if(j.error&&!VIEWS.length&&!REPLY){ REPLY='⚠ '+j.error; }
    if(!VIEWS.length&&Array.isArray(j.traces)){ renderTurnFromHTTP(j);   // no live stream -> render from the body
      if(SETTLED){ const n=BOOK.filter(s=>s.cls==='deriv').length; if(n){ STATUS='Answered in '+n+' step'+(n===1?'':'s'); renderRail(); } saveConvState(); } }   // body landed AFTER 'done' settled: refresh the settled status + re-persist so a reload restores the real derivation
    if(!REPLY&&j.reply) REPLY=j.reply;
    if(!SETTLED) markTurnDone();
  });
  // (3) SAFETY NET: fail only if NOTHING arrived through either channel (covers a truly dead run / cold start).
  setTimeout(()=>{ if(RUN!==myRun||SETTLED)return; if(!VIEWS.length&&!REPLY&&!CALLS.length) fail('the assistant is taking too long — please try again in a moment'); }, 180000);
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
    onResult:r=>{ if(!r||!Array.isArray(r.rows))return;
      if(!BOOK.some(s=>s.cls==='deriv'&&!s.stale)) dropStale();   // a data result with no fresh derivation of its own -> retire the prior turn's stale steps; NEVER graft this answer onto them
      const last=BOOK.filter(s=>s.cls==='deriv'&&!s.stale).pop();
      if(last){ if(r.columns&&r.columns.length)last.cols=r.columns; last.rows=r.rows; last.result=true; if(last.id===ACTIVE)paint(); } else paint(); },
    onStatus:()=>{}, onClarify:()=>{}, onLowConfidence:()=>{}, onPresent:()=>{}, onError:()=>{},
  });
  callSubs.push(sub);
}
function renderTurnFromHTTP(j){                               // fallback: no RTDB -> build the derivation from the /chat body's traces
  let rendered=false;
  (j.traces||[]).forEach(t=>{ const eng=t.engine||{};
    if(Array.isArray(eng.views)&&eng.views.length){ eng.views.forEach(v=>{ appendView(v); rendered=true; }); }   // composed query: the full view stack
    else if(eng.sql&&eng.answer&&Array.isArray(eng.answer.rows)){                                                  // typed-AST own-data path returns one SQL + answer, no view stack -> surface it as a single step so the SQL + result are visible
      const agg=/\b(sum|count|avg|min|max)\s*\(/i.test(eng.sql);
      appendView({op:agg?'group_agg':'select', label:'result', columns:eng.answer.columns||[], rows:eng.answer.rows, sql:eng.sql}); rendered=true; }
  });
  if(!rendered && (j.traces||[]).some(t=>Array.isArray(((t.engine||{}).result||{}).rows))) dropStale();   // a data answer with no derivation at all -> don't leave the prior turn's stale steps showing as this answer's
}
function markTurnDone(){                                      // the turn finished: settle, show Sonnet's reply in the rail
  if(SETTLED)return;
  callSubs.forEach(u=>{try{u();}catch(_){}}); callSubs=[];
  settle();
  CONV = REPLY || 'Done.';
  // If the /chat body was lost to the proxy timeout, the answer came via RTDB — reconstruct this turn's
  // transcript client-side so the NEXT follow-up still has context.
  if(!HTTPHIST) HISTORY=HISTORY.concat([{role:'user',content:question},{role:'assistant',content:REPLY||''}]);
  try{ sessionStorage.setItem('pr_orch_history', JSON.stringify(HISTORY)); }catch(_){}   // survive a reload of THIS conversation
  const n=BOOK.filter(s=>s.cls==='deriv').length;
  STATUS = n?('Answered in '+n+' step'+(n===1?'':'s')):'Done';
  surfaceUnresolved();                                        // offer master-data sheets for unresolved text columns
  renderRail();
  saveConvState();                                            // persist a renderable snapshot so a reload restores this turn
}

async function startRun(){
  try{ await autosaveRefs(); }                                // reference state is part of this turn: never reason against a stale saved copy
  catch(e){ fail('Could not save reference data, so the query was not run: '+(e&&e.message||e)); return; }
  if(ORCH) return startTurn();                                // orchestrated front door; ?chat=0 falls through to the direct path below, which uses no external LLM
  const myRun=++RUN;                                          // supersede guard: an old run's async callbacks must not paint
  const live=()=>RUN===myRun&&!SETTLED;
  LASTQ=question; if(EDITED){ syncInputsToSheets(); EDITED=false; showRecalc(false); } stampBaseline();   // pick up any input-cell edits; re-baseline what we're about to compute
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
  httpPromise.then(j=>{ if(RUN===myRun&&j&&j.conversation_id){ setConversation(j.conversation_id); renderRail(); } });   // setConversation (not a bare sessionStorage write) so the URL becomes /reason/<id> + a snapshot can save
  // The HTTP body is ATOMIC (result+present+sql together) — the race-free source for present. Stash it and
  // (re)attempt present; tryPresent no-ops until the derivation has settled, so this can't pre-empt streaming.
  httpPromise.then(j=>{ if(RUN!==myRun||!j)return; HTTPJ=j; if(j.present) PRESENT=true; tryPresent(); });
  // (2) live trace -> sheets appear as the engine works.
  if(uid&&window.subscribeRun){
    UNSUB=window.subscribeRun(uid,jobId,{
      onConversation:c=>{ if(RUN!==myRun||!c)return; setConversation(c); renderRail(); },   // arrives early via the stream — persist + reflect in the URL (mirrors the orchestrated path), reliable even if the HTTP body is lost
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
  BOOK.forEach(s=>{ if(s.cls!=='input'&&s.cls!=='master') s.stale=true; });   // master data persists like the user's own tables
  J=null; VIEWS=[]; RESOLVES=[]; SETTLED=false; DONE=false; FAILMSG=null;
  CONV=null; CONVPENDING=false; CONVPROP=null; PRESENT=false; HTTPJ=null;
  CALLS=[]; SEEN_CALL=new Set(); REPLY=null; HTTPHIST=false;  // orchestrated turn state (HISTORY persists across turns)
  callSubs.forEach(u=>{try{u();}catch(_){}}); callSubs=[];
  SEEN=new Set(); SEEN_R=new Set(); AUTO=true;
  STATUS='Analyzing input…'; if(!BOOK.some(s=>s.id===ACTIVE)) ACTIVE=BOOK.length?BOOK[0].id:null;
}
function sendChat(){
  const box=$('chatq'); const q=(box&&box.value||'').trim();
  if(!q||!((SETTLED&&convId())||FAILMSG))return;              // one run at a time; a follow-up needs the conversation_id (else it orphans) — mirrors the send-button gate
  archiveTurn();
  box.value='';
  question=q; try{ sessionStorage.setItem(SS.Q,q); }catch(_){}
  resetRun(); paint();
  if(box) box.focus();                                        // keep the cursor in the chat box for rapid follow-ups
  startRun();
}
function wireChat(){
  const box=$('chatq'), btn=$('chatsend');
  if(btn) btn.onclick=sendChat;
  if(box) box.addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendChat(); } });
  const t=$('tabstrip'); if(t) t.addEventListener('scroll',updateTabArrows);
  window.addEventListener('resize',updateTabArrows);
  document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDrawer(); });
  const cl=$('convlist'); if(cl) cl.addEventListener('click',e=>{
    const del=e.target.closest('.convdel'); if(del){ e.stopPropagation(); deleteConv(del.dataset.del); return; }
    const it=e.target.closest('.convitem'); if(it&&it.dataset.cid) openConversation(it.dataset.cid); });
}

/* ---- header title = the conversation's opening question (truncates with … via CSS) ---- */
function setHeaderTitle(q){ const el=$('htitle'); if(el){ el.textContent=q; el.title=q; } document.title='Prereasoner · '+(q.length>40?q.slice(0,40)+'…':q); }

/* ---- conversations drawer (backed by the engine's chat schema; ownership-scoped) ---- */
function convId(){ try{ return sessionStorage.getItem('pr_conversation_id')||null; }catch(_){ return null; } }
function urlConvId(){ const m=(location.pathname||'').match(/\/reason\/(c_[0-9a-f]{32})/i); return m?m[1]:null; }
// Give the live conversation a stable, shareable URL: /reason/<conversationId>. Persists the id + rewrites the
// address bar in place (no reload) so refresh, back/forward, and copy-link all land on THIS conversation.
function setConversation(cid){
  if(!cid||typeof cid!=='string') return;
  try{ sessionStorage.setItem('pr_conversation_id', cid); }catch(_){}
  if(urlConvId()!==cid){ try{ history.replaceState({}, '', '/reason/'+cid); }catch(_){} }
  const b=$('chatsend'); if(b) b.disabled=!((SETTLED&&convId())||FAILMSG);   // now that the id landed, a follow-up can safely attach to this conversation
}
function prettyTs(iso){ if(!iso)return ''; try{ return new Date(iso).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }catch(_){ return ''; } }
async function listConversations(){
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversations',{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok) return []; const j=await r.json(); return j.conversations||[];
  }catch(_){ return []; }
}
async function openConversation(id){                          // re-hydrate a past conversation (its stored tables + prompt) at its own URL
  const it=document.querySelector('.convitem[data-cid="'+id+'"]'); if(it) it.classList.add('loading');
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversation?id='+encodeURIComponent(id),{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok){ if(it){ it.classList.remove('loading'); it.classList.add('err'); } return; }
    const j=await r.json();
    sessionStorage.removeItem('pr_orch_history');            // a different conversation -> fresh context
    sessionStorage.setItem('pr_conversation_id', j.conversation_id);
    sessionStorage.setItem(SS.TABLES, JSON.stringify(j.tables||[]));
    sessionStorage.setItem(SS.Q, j.question||'');
    try{ if(j.state) sessionStorage.setItem('pr_conv_state', JSON.stringify(j.state)); else sessionStorage.removeItem('pr_conv_state'); }catch(_){}   // restore the snapshot (else run() re-runs)
    location.href='/reason/'+j.conversation_id;              // deep-linkable per-conversation URL
  }catch(_){ if(it){ it.classList.remove('loading'); it.classList.add('err'); } }
}
function newConversation(){ try{ ['pr_conversation_id','pr_orch_history','pr_conv_state',SS.TABLES,SS.Q,SS.CSV,SS.NAME].forEach(k=>k&&sessionStorage.removeItem(k)); }catch(_){}; location.href='/'; }
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
    const b=document.createElement('div'); b.className='convitem'+(c.id===cur?' on':''); b.dataset.cid=c.id;
    const q=document.createElement('div'); q.className='cq'; q.textContent=c.question||'(untitled)'; b.appendChild(q);
    if(c.ts){ const t=document.createElement('div'); t.className='ct'; t.textContent=prettyTs(c.ts); b.appendChild(t); }
    const x=document.createElement('button'); x.className='convdel'; x.dataset.del=c.id; x.title='Delete conversation'; x.textContent='×'; b.appendChild(x);
    list.appendChild(b);
  }
  const clr=document.createElement('button'); clr.className='convclear'; clr.textContent='Clear all conversations'; clr.onclick=clearAllConvs;
  list.appendChild(clr);
}
async function deleteConv(id){
  const it=document.querySelector('.convitem[data-cid="'+id+'"]'); if(it) it.style.opacity='.4';
  try{ const tk=await window.ensureToken();
    await fetch(API_BASE+'/api/conversation/delete',{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+tk},body:JSON.stringify({id})});
  }catch(_){}
  if(id===convId()) newConversation(); else renderDrawer();   // deleting the open one -> start fresh
}
async function clearAllConvs(){
  if(!confirm('Delete ALL your conversations? This cannot be undone.'))return;
  try{ const tk=await window.ensureToken();
    await fetch(API_BASE+'/api/conversation/delete-all',{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+tk},body:'{}'});
  }catch(_){}
  newConversation();
}

/* ---- conversation snapshot: persist a RENDERABLE view of the conversation (turns + derived sheets + result +
   history) so a reload RESTORES what the user saw instead of re-running the model from scratch. Input sheets come
   from the stored `tables`; master (per-user) reloads via loadMaster; only the derivation + rail are snapshotted. */
function convSnapshot(){
  const turns=CHAT.map(t=>({q:t.q, reply:t.reply||''}));
  if(SETTLED && turnReply()) turns.push({q:question, reply:turnReply()});   // the live (settled) turn isn't archived yet
  if(!turns.length) return null;
  const sheets=BOOK.filter(s=>s.cls==='deriv'||s.cls==='ref'||(s.cls==='master'&&(!s.saved||s.dirty))).map(s=>({
    id:s.id, cls:s.cls, name:s.name, cols:s.cols||[],
    rows:s.cls==='master'?(s.rows||[]).map(r=>r.slice()):(s.rows||[]).slice(0,MAX_RENDER_ROWS),
    sql:s.sql||'', desc:s.desc||'', result:!!s.result, saved:!!s.saved, dirty:s.cls==='master'&&!!s.dirty,
    cellAI:s.cls==='master'&&s.cellAI?[...s.cellAI]:undefined }));
  const refcands=REFCANDS.map(c=>({name:c.name, key:c.key, vals:(c.vals||[]).slice(0,500),   // the AVAILABLE list must survive reload so "+ Reference" persists
    cols:(c.cols&&c.cols.length>1)?c.cols:undefined,
    rows:(c.cols&&c.cols.length>1&&c.rows)?((!c.saved||c.dirty)?c.rows.map(r=>r.slice()):c.rows.slice(0,MAX_RENDER_ROWS)):undefined,
    saved:!!c.saved, dirty:!!c.dirty, cellAI:c.cellAI}));
  return {v:1, cid:convId(), turns, sheets, active:ACTIVE, history:HISTORY, refcands};
}
let _saveStateT=null;
function saveConvState(){                                     // persist the snapshot after a turn settles
  const cid=convId(); if(!cid) return;
  const st=convSnapshot(); if(!st||st.cid!==cid) return;
  const body=JSON.stringify({id:cid, state:st});
  if(body.length>3000000) return;                            // don't persist an oversized snapshot (server also caps)
  try{ sessionStorage.setItem('pr_conv_state', body.length?JSON.stringify(st):''); }catch(_){}   // IMMEDIATE: a same-tab refresh restores the latest
  clearTimeout(_saveStateT);
  _saveStateT=setTimeout(async ()=>{                         // DEBOUNCED: durable server persist (survives a fresh session / other device)
    try{ const tk=await window.ensureToken();
      await fetch(API_BASE+'/api/conversation/state',{method:'POST',
        headers:{'content-type':'application/json','Authorization':'Bearer '+tk}, body});
    }catch(_){}
  }, 700);
}
function restoreConvState(st){                               // render a stored snapshot; returns true if it took over (no re-run)
  if(!st||st.v!==1||!Array.isArray(st.turns)||!st.turns.length) return false;
  if(st.cid && convId() && st.cid!==convId()) return false;  // stale snapshot from another conversation
  (st.sheets||[]).forEach(s=>{ BOOK.push({id:s.id||('r'+BOOK.length), cls:s.cls, name:s.name, cols:s.cols||[],
      rows:s.rows||[], sql:s.sql||'', desc:s.desc||'', result:!!s.result, saved:!!s.saved, dirty:!!s.dirty,
      cellAI:Array.isArray(s.cellAI)?new Set(s.cellAI):undefined});
    if(s.cls==='master'&&s.name) MSEEN.add(referenceKey(s.name,s.cols)); });   // don't let loadMaster duplicate it
  if(Array.isArray(st.refcands)){                            // AVAILABLE candidates (removed or never-shown) -> "+ Reference" persists across reload
    const shown=new Set(BOOK.filter(s=>s.cls==='master').map(s=>referenceKey(s.name,s.cols)));
    REFCANDS=st.refcands.filter(c=>c&&c.key&&!shown.has(c.key)).map(c=>({name:c.name, key:c.key, vals:c.vals||[], cols:c.cols,
      rows:c.rows, saved:!!c.saved, dirty:!!c.dirty, cellAI:c.cellAI}));
    REFCANDS.forEach(c=>MSEEN.add(c.key));                   // keep loadMaster from auto-promoting a removed reference back to a sheet
  }
  const turns=st.turns.slice(), last=turns.pop();
  CHAT=turns.map(t=>({q:t.q, reply:t.reply||'', html:'<div class=convmsg>'+conv2html(t.reply||'')+'</div>'}));
  if(last){ question=last.q; try{ sessionStorage.setItem(SS.Q, last.q); }catch(_){}; CONV=last.reply||''; }
  if(Array.isArray(st.history)) HISTORY=st.history;
  SETTLED=true; DONE=true; STATUS='';
  ACTIVE=(st.active && BOOK.some(s=>s.id===st.active)) ? st.active
        : ((BOOK.filter(s=>s.cls==='deriv').pop()||BOOK.find(s=>s.cls==='input')||BOOK[0]||{}).id||null);
  AUTO=false;
  setHeaderTitle(CHAT.length?CHAT[0].q:question);
  paint();
  const b=$('chatsend'); if(b) b.disabled=!((SETTLED&&convId())||FAILMSG);   // follow-ups allowed (conversation exists)
  return true;
}
async function run(){
  // Deep link: landing on /reason/<id> in a session that isn't that conversation -> load it, then reload so
  // the module-level SHEETS/question pick it up. (A normal home->reason flow has no id in the URL.)
  const ucid=urlConvId();
  if(ucid&&ucid!==convId()){
    try{ const tk=await window.ensureToken();
      const r=await fetch(API_BASE+'/api/conversation?id='+encodeURIComponent(ucid),{headers:{Authorization:'Bearer '+tk}});
      if(r.ok){ const j=await r.json();
        sessionStorage.setItem('pr_conversation_id', j.conversation_id);
        sessionStorage.setItem(SS.TABLES, JSON.stringify(j.tables||[]));
        sessionStorage.setItem(SS.Q, j.question||'');
        try{ if(j.state) sessionStorage.setItem('pr_conv_state', JSON.stringify(j.state)); else sessionStorage.removeItem('pr_conv_state'); }catch(_){}
        location.reload(); return;
      }
    }catch(_){}
  }
  try{ const h=sessionStorage.getItem('pr_orch_history'); if(h){ const a=JSON.parse(h); if(Array.isArray(a)) HISTORY=a; } }catch(_){}   // restore ORCH context on reload
  wireChat(); wireGrid(); setHeaderTitle(question); seedInputs(); MASTER_READY=loadMaster();
  window.addEventListener('beforeunload', e=>{ if(BOOK.some(s=>s.cls==='master'&&s.dirty)){ e.preventDefault(); e.returnValue=''; } });  // guard unsaved master edits
  // RESTORE the saved snapshot (turns + derived sheets + result) instead of re-running the model. Only when it
  // belongs to THIS conversation; otherwise fall through to a fresh run (a brand-new conversation, or no snapshot yet).
  let restored=false;
  try{ const s=sessionStorage.getItem('pr_conv_state'); if(s){ const st=JSON.parse(s); if(st&&st.cid&&st.cid===convId()) restored=restoreConvState(st); } }catch(_){}
  if(!restored) startRun();
}
try{ fetch(ENDPOINT,{method:'GET',cache:'no-store'}).catch(()=>{}); }catch(_){}   // pre-warm the scale-to-zero backend
