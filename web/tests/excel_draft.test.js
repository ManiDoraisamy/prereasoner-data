// Exercise the real task pane request lifecycle with host/network boundaries
// stubbed, including draft changes while reads and replies are pending.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/public/office/excel/taskpane.js','utf8')
  .replace(/^import .*\r?\n/gm,'').replace(/\ninit\(\)\.catch[^\n]+/,'');
const elements = new Map();
const element = id => {
  if(!elements.has(id))elements.set(id,{value:'',disabled:false,hidden:false,textContent:'',classList:{toggle(){}}});
  return elements.get(id);
};
const context = {console,crypto:require('node:crypto').webcrypto,TextEncoder,window:{PrereasonerTurnRenderer:{},PrereasonerSuggestions:{...require('../public/lib/sidebar-suggestions.js'),create:()=>({update(){},clear(){},setActive(){}})}},
  document:{getElementById:element},initializeApp:()=>({}),getAuth:()=>({currentUser:{uid:'fixture'}}),
  getDatabase:()=>({}),firebaseConfig:{}};
vm.createContext(context);
vm.runInContext(source+`
  restore=async()=>{}; renderTurns=()=>{}; mapReasoning=()=>[];
  awaitStream=()=>({promise:new Promise(()=>{}),cancel:()=>{}});
  globalThis.lifecycle={ask,setBusy,state};
`,context);
const deferred = () => {let resolve,reject;const promise=new Promise((ok,fail)=>{resolve=ok;reject=fail;});return {promise,resolve,reject};};
const deadline = setTimeout(()=>{console.error('Excel lifecycle did not complete');process.exitCode=1;},10000);
(async()=>{
  const read=deferred(), reply=deferred();
  context.readWorkbook=()=>read.promise; context.api=()=>reply.promise;
  context.persist=async()=>{throw new Error('storage full');};
  const pending=context.lifecycle.ask('total amount');
  assert.equal(element('question').disabled,false);
  assert.equal(element('send').disabled,true);
  assert.equal(element('newChat').disabled,true);
  element('question').value='and for France?';
  read.resolve({name:'Orders',activeSheet:'Orders',sheetNames:['Orders'],tables:[{name:'Orders',data:'Amount\n10',import:{warnings:['Repeated headers were kept.']}}]});
  reply.resolve({reply:'10',conversation_id:'fixture',traces:[]});
  await pending;
  assert.equal(element('question').value,'and for France?');
  assert.equal(context.lifecycle.state.turns.at(-1).reply,'10');
  assert.equal(context.lifecycle.state.history.at(-1).content,'10');
  // A failed history save and import warnings are not notices: the answer is on screen, and the
  // next answer saves the whole conversation again.
  assert.equal(element('notice').textContent,'');
  assert.equal(element('send').disabled,false);
  context.api=async()=>{throw new Error('Request rejected');};
  context.readWorkbook=async()=>({name:'Orders',activeSheet:'Orders',sheetNames:['Orders'],tables:[]});
  const failed=context.lifecycle.ask('second question');
  element('question').value='a newer draft'; await failed;
  assert.equal(element('question').value,'a newer draft');
  // The failed question stays in the thread even though a newer draft took the composer.
  assert.deepEqual({...context.lifecycle.state.turns.at(-1)},{question:'second question',reply:'Request rejected',error:true});
  // A typed question leaves the composer as it is sent (the submit handler empties it); a failure puts it back.
  element('question').value='';
  await context.lifecycle.ask('retry this');
  assert.equal(element('question').value,'retry this');
  // A starter question is asked without emptying the composer: a draft there stays (2026-10-08).
  element('question').value='my own draft';
  await context.lifecycle.ask('a starter question');
  assert.equal(element('question').value,'my own draft');
  // A chat deleted elsewhere: the workbook goes to a new chat first, and the question is asked there by its
  // hash (upload once). The retry used to send every cell with no chat at all.
  const posted=[];
  context.readWorkbook=async()=>({name:'Orders',activeSheet:'Orders',sheetNames:['Orders'],tables:[{name:'Orders',data:'Amount\n10'}]});
  context.api=async(path,body)=>{
    posted.push([path,body.id??body.conversation_id,body.source_hash??null,'tables' in body]);
    if(path==='/api/conversation/sync')return {conversation_id:body.id||'c_new',source_hash:(body.id?'a':'b').repeat(64)};
    if(body.conversation_id==='c_old')throw new Error('conversation not found');
    return {reply:'10',conversation_id:body.conversation_id,traces:[]};
  };
  Object.assign(context.lifecycle.state,{conversationId:'c_old',sourceHash:'',restored:true});
  await context.lifecycle.ask('total amount');
  assert.deepEqual(posted,[['/api/conversation/sync','c_old',null,true],['/chat','c_old','a'.repeat(64),false],
    ['/api/conversation/sync','',null,true],['/chat','c_new','b'.repeat(64),false]]);
  assert.equal(context.lifecycle.state.turns.at(-1).reply,'10');
  clearTimeout(deadline);
  console.log('Excel lifecycle: editable drafts, failure recovery and unsaved answers preserved');
})().catch(error=>{clearTimeout(deadline);console.error(error);process.exitCode=1;});
