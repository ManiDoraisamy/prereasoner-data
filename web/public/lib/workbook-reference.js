// workbook-reference.js — saved-reference discovery, editing, generation, and persistence.
// Classic script loaded before workbook.js; functions resolve workbook state from the shared global environment.

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
