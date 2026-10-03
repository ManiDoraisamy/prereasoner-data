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
const context = {console,crypto:require('node:crypto').webcrypto,window:{PrereasonerTurnRenderer:{}},
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
  read.resolve({name:'Orders',tables:[{name:'Orders',data:'Amount\n10',import:{warnings:['Repeated headers were kept.']}}]});
  reply.resolve({reply:'10',conversation_id:'fixture',traces:[]});
  await pending;
  assert.equal(element('question').value,'and for France?');
  assert.equal(context.lifecycle.state.turns.at(-1).reply,'10');
  assert.equal(context.lifecycle.state.history.at(-1).content,'10');
  assert.match(element('notice').textContent,/storage full/);
  assert.match(element('notice').textContent,/Repeated headers/);
  assert.equal(element('send').disabled,false);
  context.api=async()=>{throw new Error('Request rejected');};
  context.readWorkbook=async()=>({name:'Orders',tables:[]});
  const failed=context.lifecycle.ask('second question');
  element('question').value='a newer draft'; await failed;
  assert.equal(element('question').value,'a newer draft');
  await context.lifecycle.ask('retry this');
  assert.equal(element('question').value,'retry this');
  clearTimeout(deadline);
  console.log('Excel lifecycle: editable drafts, failure recovery and unsaved answers preserved');
})().catch(error=>{clearTimeout(deadline);console.error(error);process.exitCode=1;});
