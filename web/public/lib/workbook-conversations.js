// workbook-conversations.js — conversation URL, history drawer, and persisted workbook snapshots.
// Classic script loaded before workbook.js; functions resolve workbook state from the shared global environment.

function convId(){ try{ return sessionStorage.getItem('pr_conversation_id')||null; }catch(_){ return null; } }
function urlConvId(){ const m=(location.pathname||'').match(/\/reason\/(c_[0-9a-f]{32})/i); return m?m[1]:null; }
// Give the live conversation a stable, shareable URL: /reason/<conversationId>. Persists the id + rewrites the
// address bar in place (no reload) so refresh, back/forward, and copy-link all land on THIS conversation.
function setConversation(cid){
  if(!cid||typeof cid!=='string') return;
  try{ sessionStorage.setItem('pr_conversation_id', cid); }catch(_){}
  if(urlConvId()!==cid){ try{ history.replaceState({}, '', '/reason/'+cid+executionQuery()); }catch(_){} }
  const b=$('chatsend'); if(b) b.disabled=!((SETTLED&&convId())||FAILMSG);   // now that the id landed, a follow-up can safely attach to this conversation
}
function prettyTs(iso){ if(!iso)return ''; try{ return new Date(iso).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }catch(_){ return ''; } }
async function listConversations(before){
  try{ const tk=await window.ensureToken();
    const url=API_BASE+'/api/conversations?limit=50'+(before?'&before='+encodeURIComponent(before):'');
    const r=await fetch(url,{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok) return {conversations:[],next_cursor:null}; const j=await r.json();
    return {conversations:j.conversations||[],next_cursor:j.next_cursor||null};
  }catch(_){ return {conversations:[],next_cursor:null}; }
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
    location.href='/reason/'+j.conversation_id+executionQuery(); // deep-linkable per-conversation URL
  }catch(_){ if(it){ it.classList.remove('loading'); it.classList.add('err'); } }
}
function newConversation(){ try{ ['pr_conversation_id','pr_orch_history','pr_conv_state',SS.TABLES,SS.Q,SS.CSV,SS.NAME].forEach(k=>k&&sessionStorage.removeItem(k)); }catch(_){}; location.href='/'+executionQuery(); }
function openDrawer(){ $('drawer').classList.add('open'); $('drawerback').classList.add('open'); renderDrawer(); }
function closeDrawer(){ $('drawer').classList.remove('open'); $('drawerback').classList.remove('open'); }
async function renderDrawer(){
  const list=$('convlist'); if(!list)return;
  list.innerHTML='<div class=convempty>Loading…</div>';
  const page=await listConversations(), convs=page.conversations;
  list.innerHTML='';
  if(!convs.length){ list.innerHTML='<div class=convempty>Your past conversations will appear here.</div>'; return; }
  const cur=convId();
  // Build with the DOM API (dataset + textContent), never string-concatenated HTML — the conversation
  // id/question come from the server and must not be interpolated into markup or an inline handler.
  const appendItems=(items,before=null)=>items.forEach(c=>{
    const b=document.createElement('div'); b.className='convitem'+(c.id===cur?' on':''); b.dataset.cid=c.id;
    const q=document.createElement('div'); q.className='cq'; q.textContent=c.question||'(untitled)'; b.appendChild(q);
    if(c.ts){ const t=document.createElement('div'); t.className='ct'; t.textContent=prettyTs(c.ts); b.appendChild(t); }
    const x=document.createElement('button'); x.className='convdel'; x.dataset.del=c.id; x.title='Delete conversation'; x.textContent='×'; b.appendChild(x);
    list.insertBefore(b,before);
  });
  appendItems(convs);
  if(page.next_cursor){ let cursor=page.next_cursor; const more=document.createElement('button'); more.className='convclear'; more.textContent='Load older conversations';
    more.onclick=async()=>{more.disabled=true;const next=await listConversations(cursor);appendItems(next.conversations,more);
      cursor=next.next_cursor;if(cursor)more.disabled=false;else more.remove();}; list.appendChild(more); }
  const clr=document.createElement('button'); clr.className='convclear'; clr.textContent='Clear all conversations'; clr.onclick=clearAllConvs;
  list.appendChild(clr);
}
// One binding for the conversation list, used by BOTH surfaces that render it: the /reason
// drawer and the signed-in home rail. Delegated, so it survives renderDrawer() rebuilds.
function bindConversationList(){
  const cl=$('convlist'); if(!cl||cl._convBound) return; cl._convBound=true;
  cl.addEventListener('click',e=>{
    const del=e.target.closest('.convdel'); if(del){ e.stopPropagation(); deleteConv(del.dataset.del); return; }
    const it=e.target.closest('.convitem'); if(it&&it.dataset.cid) openConversation(it.dataset.cid); });
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
  const turns=CHAT.map(t=>({q:t.q, reply:t.reply||'', analysis:t.analysis||null}));
  if(SETTLED && turnReply()) turns.push({q:question, reply:turnReply(), analysis:TURN_ANALYSIS||null});   // the live (settled) turn isn't archived yet
  if(!turns.length) return null;
  const sheets=BOOK.filter(s=>s.cls==='deriv'||s.cls==='ref'||(s.cls==='master'&&(!s.saved||s.dirty))).map(s=>({
    id:s.id, cls:s.cls, name:s.name, cols:s.cols||[],
    rows:s.cls==='master'?(s.rows||[]).map(r=>r.slice()):(s.rows||[]).slice(0,MAX_RENDER_ROWS),
    sql:s.sql||'', python:s.python||'', desc:s.desc||'', result:!!s.result, columnProvenance:s.columnProvenance||[], saved:!!s.saved, dirty:s.cls==='master'&&!!s.dirty,
    execution:normalizedExecution(s.execution),
    cellAI:s.cls==='master'&&s.cellAI?[...s.cellAI]:undefined }));
  const refcands=REFCANDS.map(c=>({name:c.name, key:c.key, vals:(c.vals||[]).slice(0,500),   // the AVAILABLE list must survive reload so "+ Reference" persists
    cols:(c.cols&&c.cols.length>1)?c.cols:undefined,
    rows:(c.cols&&c.cols.length>1&&c.rows)?((!c.saved||c.dirty)?c.rows.map(r=>r.slice()):c.rows.slice(0,MAX_RENDER_ROWS)):undefined,
    saved:!!c.saved, dirty:!!c.dirty, cellAI:c.cellAI}));
  return {v:3, cid:convId(), turns, sheets, active:ACTIVE, history:HISTORY, refcands,
    datasetSemantics:DS_META, viewedAnalysis:VIEWED_ANALYSIS||null, execution:EXEC||null};
}
const MAX_CONVERSATION_STATE_BYTES=1024*1024;                 // must match engine.conversations.MAX_STATE_BYTES
function conversationStateBytes(st){ return new TextEncoder().encode(JSON.stringify(st)).byteLength; }
// Keep the durable snapshot under the server limit without throwing away the whole conversation.
// Derived/reference rows are reproducible display material; unsaved or dirty master data is not, so
// compaction never truncates those user-authored rows. Exact SQL/Python source and the final result row
// survive every normal compaction tier so a restored workbook remains interpretable.
function compactConvSnapshot(snapshot,maxBytes=MAX_CONVERSATION_STATE_BYTES){
  if(!snapshot||conversationStateBytes(snapshot)<=maxBytes) return snapshot;
  const st=JSON.parse(JSON.stringify(snapshot)); st.compacted=true;
  const fits=()=>conversationStateBytes(st)<=maxBytes;
  // Saved, clean candidates can be reloaded from the user's reference store. Their identity is enough
  // to keep "+ Reference" present while avoiding hundreds of duplicate rows in conversation state.
  (st.refcands||[]).forEach(c=>{ if(c&&c.saved&&!c.dirty){ c.vals=(c.vals||[]).slice(0,50); if(c.rows)c.rows=[]; } });
  if(fits()) return st;
  for(const cap of [100,25,5,1,0]){
    (st.sheets||[]).forEach(s=>{
      if(s.cls==='deriv'||s.cls==='ref'){
        const keep=s.result?Math.max(1,cap):cap;
        s.rows=(s.rows||[]).slice(0,keep);
      }
    });
    (st.refcands||[]).forEach(c=>{ if(c&&!c.dirty){
      c.vals=(c.vals||[]).slice(0,cap); if(c.rows)c.rows=c.rows.slice(0,cap);
    } });
    if(fits()) return st;
  }
  // A pathological final cell can itself exceed the cap. Preserve the source, columns, turn text,
  // and workbook structure; the human-readable answer in `turns` still restores in the chat rail.
  (st.sheets||[]).forEach(s=>{ if(s.cls==='deriv'||s.cls==='ref')s.rows=[]; });
  if(fits()) return st;
  return null;                                                // only irreducible user-authored state is too large
}
let _saveStateT=null;
function saveConvState(){                                     // persist the snapshot after a turn settles
  const cid=convId(); if(!cid) return;
  const full=convSnapshot(); if(!full||full.cid!==cid) return;
  const st=compactConvSnapshot(full);
  // Session storage is allowed to retain the fuller same-device copy. If its browser quota is
  // smaller, retry with the server-safe compact copy rather than silently losing restore state.
  try{ sessionStorage.setItem('pr_conv_state',JSON.stringify(full)); }
  catch(error){
    if(st){ try{ sessionStorage.setItem('pr_conv_state',JSON.stringify(st)); }catch(inner){ console.warn('conversation snapshot could not be cached',inner&&inner.name||'Error'); } }
    else console.warn('conversation snapshot exceeds both persistence limits',error&&error.name||'Error');
  }
  if(!st){ console.warn('conversation snapshot exceeds the server limit after safe compaction'); return; }
  const body=JSON.stringify({id:cid, state:st});
  clearTimeout(_saveStateT);
  _saveStateT=setTimeout(async ()=>{                         // DEBOUNCED: durable server persist (survives a fresh session / other device)
    try{ const tk=await window.ensureToken();
      const response=await fetch(API_BASE+'/api/conversation/state',{method:'POST',
        headers:{'content-type':'application/json','Authorization':'Bearer '+tk}, body});
      if(!response.ok) console.warn('conversation snapshot was not persisted: HTTP '+response.status);
    }catch(error){ console.warn('conversation snapshot was not persisted',error&&error.name||'Error'); }
  }, 700);
}
function restoredSheetExecution(st,s){
  return normalizedExecution((s&&s.execution)||((st&&st.v<3)&&st.execution));
}
function restoreConvState(st){                               // render a stored snapshot; returns true if it took over (no re-run)
  if(!st||![1,2,3].includes(st.v)||!Array.isArray(st.turns)||!st.turns.length) return false;
  if(st.cid && convId() && st.cid!==convId()) return false;  // stale snapshot from another conversation
  noteExecution(st.execution);                               // v1/v2 fallback; v3 stores provenance per sheet
  (st.sheets||[]).forEach(s=>{ BOOK.push({id:s.id||('r'+BOOK.length), cls:s.cls, name:s.name, cols:s.cols||[],
      rows:s.rows||[], sql:s.sql||'', python:s.python||'', desc:s.desc||'', result:!!s.result, columnProvenance:s.columnProvenance||[], saved:!!s.saved, dirty:!!s.dirty,
      execution:restoredSheetExecution(st,s),cellAI:Array.isArray(s.cellAI)?new Set(s.cellAI):undefined});
    if(s.cls==='master'&&s.name) MSEEN.add(referenceKey(s.name,s.cols)); });   // don't let loadMaster duplicate it
  if(Array.isArray(st.refcands)){                            // AVAILABLE candidates (removed or never-shown) -> "+ Reference" persists across reload
    const shown=new Set(BOOK.filter(s=>s.cls==='master').map(s=>referenceKey(s.name,s.cols)));
    REFCANDS=st.refcands.filter(c=>c&&c.key&&!shown.has(c.key)).map(c=>({name:c.name, key:c.key, vals:c.vals||[], cols:c.cols,
      rows:c.rows, saved:!!c.saved, dirty:!!c.dirty, cellAI:c.cellAI}));
    REFCANDS.forEach(c=>MSEEN.add(c.key));                   // keep loadMaster from auto-promoting a removed reference back to a sheet
  }
  const turns=st.turns.slice(), last=turns.pop();
  VIEWED_ANALYSIS=st.viewedAnalysis||((last&&last.analysis)||null); ANALYSIS_ERROR=null;
  CHAT=turns.map(t=>({q:t.q, reply:t.reply||'', analysis:t.analysis||null,
    html:(t.analysis?'<div class="cot archived"><div class=cotbar>'+analysisHeading(t.analysis)+'</div></div>':'')
      +'<div class=convmsg>'+conv2html(t.reply||'')+'</div>'}));
  if(last){ question=last.q; TURN_ANALYSIS=last.analysis||null;
    try{ sessionStorage.setItem(SS.Q, last.q); }catch(_){}; CONV=last.reply||''; }
  if(Array.isArray(st.history)) HISTORY=st.history;
  if(Array.isArray(st.datasetSemantics)) DS_META=st.datasetSemantics;
  SETTLED=true; DONE=true; STATUS='';
  ACTIVE=(st.active && BOOK.some(s=>s.id===st.active)) ? st.active
        : ((BOOK.filter(s=>s.cls==='deriv').pop()||BOOK.find(s=>s.cls==='input')||BOOK[0]||{}).id||null);
  AUTO=false;
  setHeaderTitle(CHAT.length?CHAT[0].q:question);
  paint();
  const b=$('chatsend'); if(b) b.disabled=!((SETTLED&&convId())||FAILMSG);   // follow-ups allowed (conversation exists)
  return true;
}
