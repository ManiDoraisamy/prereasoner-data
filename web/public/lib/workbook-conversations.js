// workbook-conversations.js — conversation URL, history drawer, and persisted workbook snapshots.
// Classic script loaded before workbook.js; functions resolve workbook state from the shared global environment.

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
