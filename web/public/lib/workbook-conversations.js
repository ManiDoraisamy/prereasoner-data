// workbook-conversations.js — conversation URL, history drawer, and persisted workbook snapshots.
// Classic script loaded before workbook.js; functions resolve workbook state from the shared global environment.

function convId(){ try{ return sessionStorage.getItem('pr_conversation_id')||null; }catch(_){ return null; } }
function urlConvId(){ const m=(location.pathname||'').match(/\/reason\/(c_[0-9a-f]{32})/i); return m?m[1]:null; }
function urlAnalysis(){
  try{
    const params=new URLSearchParams(location.search),id=params.get('analysis_id')||'',raw=params.get('revision')||'';
    const revision=Number(raw);
    return /^a_[0-9a-f]{32}$/.test(id)&&/^\d+$/.test(raw)&&Number.isInteger(revision)
      &&revision>=1&&revision<=1000000 ? {analysis_id:id,revision} : null;
  }catch(_){return null;}
}
function sourceKind(tables){
  const kinds=(tables||[]).map(t=>(t&&t.source&&t.source.kind)||(t&&t.import&&t.import.source)||'');
  if(kinds.some(k=>/^google-sheets/.test(k)))return 'google-sheets';
  if(kinds.some(k=>k==='example'))return 'example';
  if(kinds.some(k=>k==='excel'||k==='xlsx'))return 'excel';
  return kinds.some(Boolean)?kinds.find(Boolean):'upload';
}
function rememberConversationSource(j){
  if(!j)return;const info={kind:sourceKind(j.tables),sourceHash:j.source_hash||'',answerHash:(j.state&&j.state.sourceHash)||'',datasetVersion:j.dataset_version||0};
  info.stale=!!(info.sourceHash&&info.answerHash&&info.sourceHash!==info.answerHash);
  try{sessionStorage.setItem(SS.SOURCE_INFO,JSON.stringify(info));}catch(_){}
  // The reopened sheets are the stored ones: the page fingerprints them when it loads (settleUploadRecord),
  // so the next question names them instead of uploading them again.
  try{if(j.conversation_id&&j.source_hash)sessionStorage.setItem(SS.UPLOADED,JSON.stringify({cid:j.conversation_id,sourceHash:j.source_hash,fingerprint:''}));}catch(_){}
}

/* ---- upload once (2026-10-02): the sheets go to the conversation when they change; a question names
   them by conversation and source hash instead of carrying every row ---- */
async function sheetsFingerprint(){
  const bytes=new TextEncoder().encode(JSON.stringify(SHEETS.map(s=>[s.name,s.data])));
  const hash=await crypto.subtle.digest('SHA-256',bytes);
  return Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('');
}
function uploadRecord(){try{return JSON.parse(sessionStorage.getItem(SS.UPLOADED)||'null');}catch(_){return null;}}
// A reopened conversation's sheets, as the page loaded them, are its stored snapshot.
async function settleUploadRecord(){
  const last=uploadRecord();
  if(!last||last.fingerprint||last.cid!==convId())return;
  last.fingerprint=await sheetsFingerprint();
  try{sessionStorage.setItem(SS.UPLOADED,JSON.stringify(last));}catch(_){}
}
async function uploadedSource(token,question,force){
  const fingerprint=await sheetsFingerprint(),cid=convId(),last=uploadRecord();
  if(!force&&last&&last.cid===cid&&last.sourceHash&&last.fingerprint===fingerprint)
    return {conversation_id:cid,source_hash:last.sourceHash};
  const r=await fetch(API_BASE+'/api/conversation/sync',{method:'POST',
    headers:{'content-type':'application/json','Authorization':'Bearer '+token},
    body:JSON.stringify({id:cid||'',question:question||'',tables:SHEETS})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok||!j.conversation_id||!j.source_hash)throw new Error(j.error||('the sheets could not be uploaded (HTTP '+r.status+')'));
  setConversation(j.conversation_id);
  try{sessionStorage.setItem(SS.UPLOADED,JSON.stringify({cid:j.conversation_id,sourceHash:j.source_hash,fingerprint}));}catch(_){}
  return {conversation_id:j.conversation_id,source_hash:j.source_hash};
}
function currentSourceInfo(){try{return JSON.parse(sessionStorage.getItem(SS.SOURCE_INFO)||'{}')||{};}catch(_){return {};}}
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
    if(!r.ok) throw new Error('Could not load chats (HTTP '+r.status+'). Please retry.'); const j=await r.json();
    return {conversations:j.conversations||[],next_cursor:j.next_cursor||null};
  }catch(error){ return {conversations:[],next_cursor:null,error:error.message||'Could not load chats. Please retry.'}; }
}
async function openConversation(id){                          // re-hydrate a past conversation (its stored tables + prompt) at its own URL
  const it=document.querySelector('.convitem[data-cid="'+id+'"]'); if(it) it.classList.add('loading');
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversation?id='+encodeURIComponent(id),{headers:{Authorization:'Bearer '+tk}});
    if(!r.ok){ if(it){ it.classList.remove('loading'); it.classList.add('err'); } return; }
    const j=await r.json();
    sessionStorage.removeItem('pr_orch_history');            // a different conversation -> fresh context
    sessionStorage.setItem('pr_conversation_id', j.conversation_id);
    await SHEET_HANDOFF.put(SS.TABLES, j.tables||[]);
    sessionStorage.setItem(SS.Q, j.question||'');
    rememberConversationSource(j);
    try{ if(j.state) sessionStorage.setItem('pr_conv_state', JSON.stringify(j.state)); else sessionStorage.removeItem('pr_conv_state'); }catch(_){}   // restore the snapshot (else run() re-runs)
    location.href='/reason/'+j.conversation_id+executionQuery(); // deep-linkable per-conversation URL
  }catch(_){ if(it){ it.classList.remove('loading'); it.classList.add('err'); } }
}
function newConversation(){
  let route='/';try{const saved=sessionStorage.getItem(SS.ENTRY_ROUTE)||'/';if(/^\/(sheets|excel|csv)?\/?$/.test(saved))route=saved.replace(/\/$/,'')||'/';
    ['pr_conversation_id','pr_orch_history','pr_conv_state',SS.Q,SS.SOURCE_INFO,SS.UPLOADED].forEach(k=>k&&sessionStorage.removeItem(k));
  }catch(_){}
  SHEET_HANDOFF.clear(SS.TABLES).catch(()=>{}).finally(()=>{location.href=route+executionQuery();});
}
function setDrawer(open){
  open=!!open;const drawer=$('drawer'),back=$('drawerback');if(!drawer)return;
  const returnFocus=!open&&drawer.contains(document.activeElement);
  drawer.classList.toggle('open',open);document.body.classList.toggle('chat-nav-open',open);
  drawer.inert=!open;drawer.setAttribute('aria-hidden',open?'false':'true');
  if(back){back.classList.toggle('open',open);back.setAttribute('aria-hidden',open?'false':'true');}const menu=$('menubtn');if(menu)menu.setAttribute('aria-expanded',open?'true':'false');
  if(open){renderDrawer();requestAnimationFrame(()=>{const close=drawer.querySelector('.drawerx');if(close)close.focus();});}
  else if(returnFocus&&menu)menu.focus();
}
function openDrawer(){setDrawer(true);}
function closeDrawer(){setDrawer(false);}
let conversationListGeneration=0;
async function renderDrawer(){
  const list=$('convlist'); if(!list)return;
  const generation=++conversationListGeneration;
  if(!list.querySelector('.convitem'))list.innerHTML='<div class=convempty>Loading…</div>';
  const page=await listConversations(), convs=page.conversations;
  if(generation!==conversationListGeneration)return;
  if(page.error){conversationListError(page.error);return;}
  list.innerHTML='';
  showConversationQuota(list);
  if(!convs.length){ list.innerHTML='<div class=convempty>Your previous chats will appear here.</div>'; return; }
  const cur=convId();
  // Build with the DOM API (dataset + textContent), never string-concatenated HTML — the conversation
  // id/question come from the server and must not be interpolated into markup or an inline handler.
  const appendItems=(items,before=null)=>items.forEach(c=>{
    const b=document.createElement('div'); b.className='convitem'+(c.id===cur?' on':''); b.dataset.cid=c.id;
    const q=document.createElement('div'); q.className='cq'; q.textContent=c.question||'(untitled)'; b.appendChild(q);
    if(c.ts){ const t=document.createElement('div'); t.className='ct'; t.textContent=prettyTs(c.ts); b.appendChild(t); }
    const x=document.createElement('button'); x.className='convdel'; x.type='button'; x.dataset.del=c.id; x.title='Delete chat'; x.setAttribute('aria-label','Delete chat');
    const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');
    icon.setAttribute('viewBox','0 0 24 24'); icon.setAttribute('aria-hidden','true'); icon.setAttribute('focusable','false');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d','M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3');
    path.setAttribute('fill','none'); path.setAttribute('stroke','currentColor'); path.setAttribute('stroke-width','1.8');
    path.setAttribute('stroke-linecap','round'); path.setAttribute('stroke-linejoin','round');
    icon.appendChild(path); x.appendChild(icon); b.appendChild(x);
    list.insertBefore(b,before);
  });
  appendItems(convs);
  if(page.next_cursor){ let cursor=page.next_cursor; const more=document.createElement('button'); more.className='convclear'; more.textContent='Load older conversations';
    more.onclick=async()=>{more.disabled=true;const next=await listConversations(cursor);if(next.error){conversationListError(next.error);more.disabled=false;return;}appendItems(next.conversations,more);
      cursor=next.next_cursor;if(cursor)more.disabled=false;else more.remove();}; list.appendChild(more); }
  const clr=document.createElement('button'); clr.className='convclear'; clr.textContent='Clear all conversations'; clr.onclick=clearAllConvs;
  list.appendChild(clr);
}
async function showConversationQuota(list){
  try{const tk=await window.ensureToken();const r=await fetch(API_BASE+'/api/conversation/quota',{headers:{Authorization:'Bearer '+tk}});if(!r.ok)return;
    const quota=await r.json();if(!Number.isFinite(quota.used)||!Number.isFinite(quota.limit))return;
    let item=list.querySelector('.convquota');if(!item){item=document.createElement('div');item.className='convempty convquota';list.prepend(item);}
    item.textContent=quota.used+' of '+quota.limit+' saved chats'+(quota.used>=quota.limit?'. Delete a saved chat to start another, or continue an existing chat.':'.');
  }catch(_){} // quota metadata is optional; a failure must not replace working history
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
  const it=document.querySelector('.convitem[data-cid="'+id+'"]');
  if(it&&it.dataset.pending)return;
  if(it){it.dataset.pending='1';it.style.opacity='.4';it.querySelectorAll('button').forEach(b=>b.disabled=true);}
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversation/delete',{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+tk},body:JSON.stringify({id})});
    if(!r.ok)throw new Error('Chat was not deleted (HTTP '+r.status+'). Please retry.');
  }catch(error){conversationListError(error.message||'Chat was not deleted. Check your connection and retry.');return;}
  finally{if(it){delete it.dataset.pending;it.style.opacity='';it.querySelectorAll('button').forEach(b=>b.disabled=false);}}
  if(id===convId()) newConversation(); else renderDrawer();   // deleting the open one -> start fresh
}
async function clearAllConvs(){
  if(!confirm('Delete ALL your conversations? This cannot be undone.'))return;
  try{ const tk=await window.ensureToken();
    const r=await fetch(API_BASE+'/api/conversation/delete-all',{method:'POST',headers:{'content-type':'application/json','Authorization':'Bearer '+tk},body:'{}'});
    if(!r.ok)throw new Error('Chats were not deleted (HTTP '+r.status+'). Please retry.');
  }catch(error){conversationListError(error.message||'Chats were not deleted. Check your connection and retry.');return;}
  newConversation();
}

function conversationListError(message){
  const list=$('convlist');if(!list)return;
  const loading=list.querySelector('.convempty');if(loading&&loading.textContent==='Loading…')loading.remove();
  let box=list.querySelector('.converror');if(!box){box=document.createElement('div');box.className='convempty converror';box.setAttribute('role','alert');list.prepend(box);}
  box.textContent=message+' ';const retry=document.createElement('button');retry.type='button';retry.textContent='Retry';retry.onclick=renderDrawer;box.appendChild(retry);
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
    viewName:s.viewName||'',op:s.op||'',inputs:s.inputs||[],section:s.section||null,sectionLabel:s.sectionLabel||'',
    sectionQuestion:s.sectionQuestion||'',sectionInputs:s.sectionInputs||[],isOutput:!!s.isOutput,
    execution:normalizedExecution(s.execution),
    cellAI:s.cls==='master'&&s.cellAI?[...s.cellAI]:undefined }));
  const refcands=REFCANDS.map(c=>({name:c.name, key:c.key, vals:(c.vals||[]).slice(0,500),   // the AVAILABLE list must survive reload so "+ Reference" persists
    cols:(c.cols&&c.cols.length>1)?c.cols:undefined,
    rows:(c.cols&&c.cols.length>1&&c.rows)?((!c.saved||c.dirty)?c.rows.map(r=>r.slice()):c.rows.slice(0,MAX_RENDER_ROWS)):undefined,
    saved:!!c.saved, dirty:!!c.dirty, cellAI:c.cellAI}));
  const source=currentSourceInfo();
  return {v:3, cid:convId(), turns, sheets, active:ACTIVE, history:HISTORY, refcands,
    datasetSemantics:DS_META, viewedAnalysis:VIEWED_ANALYSIS||null, execution:EXEC||null,
    sourceHash:source.answerHash||(!source.stale?source.sourceHash:'')||undefined};
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
  if(!st){conversationSaveError('This workbook is too large to save. Your answer is still available; download it before leaving.');return;}
  const body=JSON.stringify({id:cid, state:st});
  clearTimeout(_saveStateT);
  _saveStateT=setTimeout(async ()=>{                         // DEBOUNCED: durable server persist (survives a fresh session / other device)
    try{ const tk=await window.ensureToken();
      const response=await fetch(API_BASE+'/api/conversation/state',{method:'POST',
        headers:{'content-type':'application/json','Authorization':'Bearer '+tk}, body});
      if(!response.ok) conversationSaveError('This chat view was not saved (HTTP '+response.status+'). Your answer is still available. Download it or retry saving.');
      // The saved answer keeps the hash of the source it read (answer_hash); it is stale when another tab
      // has uploaded since. Marking it current with the newest hash hid that (release review, 2026-10-07).
      else{const saved=await response.json();if(saved.source_hash){const info=currentSourceInfo();info.sourceHash=saved.source_hash;info.answerHash=saved.answer_hash||saved.source_hash;info.stale=info.answerHash!==info.sourceHash;
        try{sessionStorage.setItem(SS.SOURCE_INFO,JSON.stringify(info));full.sourceHash=info.answerHash;sessionStorage.setItem('pr_conv_state',JSON.stringify(full));}catch(_){}
        if(typeof renderSourceStatus==='function')renderSourceStatus();}}
    }catch(error){conversationSaveError('This chat view was not saved. Check your connection and retry saving.');}
  }, 700);
}
function conversationSaveError(message){
  let box=document.getElementById('conversation-save-error');
  if(!box){box=document.createElement('div');box.id='conversation-save-error';box.setAttribute('role','alert');box.style.cssText='padding:10px;background:#fff4df;color:#6c4200;';const host=$('chat')||$('chatrail')||document.body;host.appendChild(box);}
  box.textContent=message+' ';const retry=document.createElement('button');retry.type='button';retry.textContent='Retry save';retry.onclick=()=>{box.remove();saveConvState();};box.appendChild(retry);
}
function restoredSheetExecution(st,s){
  return normalizedExecution((s&&s.execution)||((st&&st.v<3)&&st.execution));
}
// A derived sheet takes its step's current name when a snapshot is restored: one saved before a projection had a
// name showed the engine's label, which carries the decomposition leaf's id ("purch result", 2026-10-01). A
// description that only repeated that label is written again; any other saved description stands.
function restoredStep(s){
  if(s.cls!=='deriv'||!s.op) return {name:s.name, desc:s.desc||''};
  const view={op:s.op, label:s.name, sql:s.sql||'', column_provenance:s.columnProvenance||[]};
  return {name:stepLabel(view), desc:(!s.desc||s.desc===s.name)?stepDesc(view):s.desc};
}
function restoreConvState(st){                               // render a stored snapshot; returns true if it took over (no re-run)
  if(!st||![1,2,3].includes(st.v)||!Array.isArray(st.turns)||!st.turns.length) return false;
  if(st.compacted)return false; // recover the complete immutable analysis, never display a partial snapshot as complete
  if(st.cid && convId() && st.cid!==convId()) return false;  // stale snapshot from another conversation
  try{
    // Validate atomically: do not restore half a workbook before discovering an
    // incompatible legacy result. The caller can load its authoritative analysis.
    st={...st,sheets:(st.sheets||[]).map(s=>RESULT_WIRE.table(s))};
  }catch(error){
    console.warn('Saved result requires authoritative recovery',error.message);
    return false;
  }
  noteExecution(st.execution);                               // v1/v2 fallback; v3 stores provenance per sheet
  (st.sheets||[]).forEach(s=>{ const step=restoredStep(s); BOOK.push({id:s.id||('r'+BOOK.length), cls:s.cls, name:step.name, cols:s.cols||[],
      rows:s.rows||[], sql:s.sql||'', python:s.python||'', desc:step.desc, result:!!s.result, columnProvenance:s.columnProvenance||[], saved:!!s.saved, dirty:!!s.dirty,
      viewName:s.viewName||'',op:s.op||'',inputs:s.inputs||[],section:s.section||null,sectionLabel:s.sectionLabel||'',
      sectionQuestion:s.sectionQuestion||'',sectionInputs:s.sectionInputs||[],isOutput:!!s.isOutput,
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
