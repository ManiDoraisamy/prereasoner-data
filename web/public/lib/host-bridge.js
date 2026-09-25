// host-bridge.js — this page running INSIDE a spreadsheet add-in. The Google Sheets sidebar frames
// /embed/sheets; the add-in is only a host that supplies what the page cannot read itself: the
// signed-in user's Google token, the spreadsheet's identity and its cell grids. Everything else is
// the ordinary web workbook (workbook.js, live RTDB traces, conversations).
//
// Messages are accepted only from the direct parent frame and only from Apps Script's add-on
// content origins (localhost pages accept a localhost host, for the browser tests). The token only
// ever travels host -> page, addressed by the host to this page's origin; requests carry no data.
// Links open their own tabs from this frame (the user's click is here), never through the host.
(function(root){
  'use strict';
  const APPS_SCRIPT=/^https:\/\/n-[a-z0-9]+-[a-z0-9]+-script\.googleusercontent\.com$/;
  const LOCAL=/^http:\/\/(?:localhost|127\.0\.0\.1)(?::\d+)?$/;
  const local=location.hostname==='localhost'||location.hostname==='127.0.0.1';
  const embedded=/^\/embed\/sheets\/?$/.test(location.pathname)&&root.parent!==root;
  const trusted=origin=>APPS_SCRIPT.test(origin)||(local&&LOCAL.test(origin));
  const SESSION_KEYS=['pr_conversation_id','pr_orch_history','pr_conv_state',SS.TABLES,SS.Q,SS.CSV,SS.NAME,SS.SOURCE_INFO];
  const waiting=new Map();
  let sequence=0, hostOrigin=null, spreadsheetId=null;
  root.addEventListener('message',event=>{
    if(event.source!==root.parent||!trusted(event.origin))return;
    const message=event.data||{};
    if(!message.prereasoner||!waiting.has(message.id))return;
    hostOrigin=event.origin;
    const pending=waiting.get(message.id);waiting.delete(message.id);clearTimeout(pending.timer);
    if(message.type==='error')pending.reject(new Error((message.payload&&message.payload.message)||'The spreadsheet could not be read.'));
    else pending.resolve(message.payload);
  });
  // Ask the host for something; resolves with its reply.
  function request(type,timeoutMs){
    if(!embedded)return Promise.reject(new Error('Not running inside a spreadsheet add-in.'));
    return new Promise((resolve,reject)=>{
      const id=++sequence;
      const timer=setTimeout(()=>{waiting.delete(id);reject(new Error('The spreadsheet did not respond. Close and reopen Prereasoner.'));},timeoutMs||60000);
      waiting.set(id,{resolve,reject,timer});
      root.parent.postMessage({prereasoner:1,id,type},hostOrigin||'*');
    });
  }
  // The host's cell grids through the upload importer (lib/xlsx-worker.js): the same header,
  // date, duration, merge, total and error rule as an uploaded workbook of the same cells.
  function importGrids(grids){
    return new Promise((resolve,reject)=>{
      const worker=new Worker('/lib/xlsx-worker.js');
      worker.onmessage=event=>{
        worker.terminate();
        if(event.data&&event.data.ok)resolve(event.data.sheets.map(sheet=>({name:sheet.name,data:sheet.csv,
          import:sheet.import,source:{kind:'google-sheets-addon'}})));
        else reject(new Error((event.data&&event.data.error)||'The spreadsheet could not be read.'));
      };
      worker.onerror=event=>{worker.terminate();reject(new Error(event.message||'The spreadsheet could not be read.'));};
      worker.postMessage({grids});
    });
  }
  // A stable fingerprint of request tables, to tell whether the sheet changed.
  async function digest(tables){
    const text=JSON.stringify((tables||[]).map(table=>[table.name,table.data]));
    const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));
    return Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');
  }
  async function api(path,body){
    const token=await root.ensureToken();
    const response=await fetch(API_BASE+path,body?{method:'POST',headers:{'Content-Type':'application/json',
      Authorization:'Bearer '+token},body:JSON.stringify(body)}:{headers:{Authorization:'Bearer '+token}});
    let payload={};try{payload=await response.json();}catch(_){}
    if(!response.ok)throw new Error(payload.error||('Prereasoner request failed (HTTP '+response.status+').'));
    return payload;
  }
  function session(){try{return JSON.parse(sessionStorage.getItem(SS.EMBED)||'null');}catch(_){return null;}}
  // Load the spreadsheet's conversation (or, before its first question, its current tables) into this
  // frame's workbook session exactly as a /reason/<id> deep link does. A conversation whose source was
  // synced reopens with its answer marked stale (rememberConversationSource), as any reopened
  // conversation whose data changed. When this tab already holds that conversation, its own snapshot
  // and history are kept: the server copy is saved after a delay and can miss the latest turn.
  async function writeSession(cid,tables){
    const kept=cid&&sessionStorage.getItem('pr_conversation_id')===cid
      ?{state:sessionStorage.getItem('pr_conv_state'),history:sessionStorage.getItem('pr_orch_history')}:{};
    SESSION_KEYS.forEach(key=>sessionStorage.removeItem(key));
    if(cid){
      const conversation=await api('/api/conversation?id='+encodeURIComponent(cid));
      let state=conversation.state||null;
      try{if(kept.state)state=JSON.parse(kept.state);}catch(_){}
      sessionStorage.setItem('pr_conversation_id',conversation.conversation_id);
      sessionStorage.setItem(SS.TABLES,JSON.stringify(conversation.tables||[]));
      sessionStorage.setItem(SS.Q,conversation.question||'');
      if(state)sessionStorage.setItem('pr_conv_state',JSON.stringify(state));
      if(kept.history)sessionStorage.setItem('pr_orch_history',kept.history);
      root.rememberConversationSource({...conversation,state});
    }else{
      sessionStorage.setItem(SS.TABLES,JSON.stringify(tables));
    }
    sessionStorage.setItem(SS.EMBED,JSON.stringify({spreadsheetId,cid,tables:await digest(tables)}));
  }
  // After the workbook has run, a new session needs a fresh page: write it and reload once.
  async function rebuild(cid,tables){
    await writeSession(cid,tables);
    location.reload();
  }
  // Before the workbook runs: sign in with the host's Google account and make this frame's session the
  // spreadsheet's (workbook.js adoptSession reads it).
  async function boot(signIn){
    const context=await request('context');
    try{await signIn(context.token);}
    catch(error){throw new Error('Prereasoner could not sign in with this Google account ('
      +((error&&(error.code||error.message))||'unknown error')+'). Close and reopen Prereasoner.');}
    spreadsheetId=context.spreadsheetId;
    const tables=await importGrids(context.workbook.grids);
    const bound=await api('/api/spreadsheet/conversation/restore',{spreadsheet_id:spreadsheetId,host:'sheets',tables});
    const cid=bound.conversation_id||null;
    // The sheet changed since its conversation last saw it: bring the conversation's source up to date.
    if(cid&&bound.source_changed)await api('/api/conversation/sync',{id:cid,tables});
    const have=session();
    if(have&&have.spreadsheetId===spreadsheetId&&have.cid===cid&&have.tables===await digest(tables))return;
    await writeSession(cid,tables);
  }
  // The conversation a first answer created becomes this spreadsheet's conversation.
  async function bind(cid){
    await api('/api/spreadsheet/conversation/state',{spreadsheet_id:spreadsheetId,host:'sheets',conversation_id:cid,state:{}});
    const have=session();
    if(have)sessionStorage.setItem(SS.EMBED,JSON.stringify({...have,cid}));
  }
  // New chat: unbind the spreadsheet and rebuild the frame for its current cells.
  async function clear(){
    const workbook=await request('grids');
    const tables=await importGrids(workbook.grids);
    await api('/api/spreadsheet/conversation/clear',{spreadsheet_id:spreadsheetId,host:'sheets'});
    await rebuild(null,tables);
  }
  // Before a question: re-read the cells. Unchanged since this frame was built -> resolves true and the
  // question is asked now. Changed -> the conversation's source is synced and the frame rebuilt with
  // the question pending (workbook.js asks it after the reload); resolves false.
  async function ensureFresh(question,cid){
    const workbook=await request('grids');
    const tables=await importGrids(workbook.grids);
    const have=session();
    if(have&&have.tables===await digest(tables))return true;
    if(cid)await api('/api/conversation/sync',{id:cid,tables});
    sessionStorage.setItem(SS.EMBED_PENDING,question);
    await rebuild(cid,tables);
    return false;
  }
  root.HOST_BRIDGE={embedded,request,importGrids,digest,boot,bind,clear,ensureFresh,host:'sheets'};
})(window);
