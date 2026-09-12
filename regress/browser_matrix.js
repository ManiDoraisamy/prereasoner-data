// UNMOCKED Chrome release gate. Upload original files, ask each manifest question
// through the UI, retain returned programs/timings, and reuse the Python golds.
// No decomposition.json, mocked responses, or fabricated Firebase login.
const {chromium}=require('@playwright/test');
const fs=require('node:fs'),path=require('node:path'),cp=require('node:child_process');
const origin=process.env.EVAL_BASE_URL||'http://127.0.0.1:8091';
const destination=path.resolve(process.env.EVAL_BROWSER_REPORT||'regress/private/browser-matrix.json');
const modes=(process.env.EVAL_BROWSER_MODES||'py,sql,both').split(',');
if(!modes.length||modes.some(mode=>!['py','sql','both'].includes(mode)))throw new Error('EVAL_BROWSER_MODES must contain py,sql,both');
const selected=process.env.EVAL_DATASETS&&new Set(process.env.EVAL_DATASETS.split(','));
const minCaseMs=Number(process.env.EVAL_MIN_CASE_MS||0);
if(!Number.isFinite(minCaseMs)||minCaseMs<0||minCaseMs>60000)throw new Error('EVAL_MIN_CASE_MS must be 0..60000');
const manifest=JSON.parse(cp.execFileSync('python',['-m','regress.browser_gold','export'],{encoding:'utf8'}));
const report={origin,started:new Date().toISOString(),channel:'chrome',mocked:false,
  source:cp.execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim(),
  dirty:!!cp.execFileSync('git',['status','--porcelain'],{encoding:'utf8'}).trim(),
  selected:selected?[...selected]:null,modes,minCaseMs,
  plannedCases:manifest.filter(d=>!selected||selected.has(d.dataset)).reduce((n,d)=>n+d.cases.length*modes.length,0),records:[]};
const save=()=>fs.writeFileSync(destination,JSON.stringify(report,null,2));
async function main(){
  fs.mkdirSync(path.dirname(destination),{recursive:true});save();
  const health=await fetch(origin+'/api/reason',{signal:AbortSignal.timeout(10000)});
  if(!health.ok)throw new Error('Engine is not ready at '+origin+' (HTTP '+health.status+')');
  const browser=await chromium.launch({channel:'chrome',headless:true});
  let lastCaseStart=0;
  try{for(const dataset of manifest){
    if(selected&&!selected.has(dataset.dataset))continue;
    for(const mode of modes){
      const context=await browser.newContext(process.env.EVAL_STORAGE_STATE?{storageState:process.env.EVAL_STORAGE_STATE}:{});
      const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
      let conversation=null;
      try{
        await page.goto(origin+'/?use='+mode);
        await page.locator('#file').setInputFiles(dataset.files);
        await page.waitForFunction(()=>!UPLOAD_PENDING,{},{timeout:20000});
        const upload=await page.evaluate(()=>({error:UPLOAD_ERROR,tables:SHEETS.map(s=>({name:s.name,rows:parseCSV(s.data).rows.length,import:s.import}))}));
        if(upload.error)throw new Error(upload.error);
        for(let i=0;i<dataset.cases.length;i++){
          const pause=Math.max(0,lastCaseStart+minCaseMs-Date.now());
          if(pause)await new Promise(resolve=>setTimeout(resolve,pause));
          const test=dataset.cases[i];const start=Date.now();lastCaseStart=start;
          const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/chat'&&r.request().method()==='POST',{timeout:250000});
          // Attach the rejection handler immediately, including upload/navigation failures.
          pending.catch(()=>{});
          if(!i){await page.locator('#q').fill(test.question);await page.getByRole('button',{name:'Ask',exact:true}).click();}
          else{await page.locator('#chatq').fill(test.question);await page.locator('#chatsend').click();}
          const http=await pending;const body=await http.json();
          await page.waitForFunction(()=>SETTLED||FAILMSG,{},{timeout:250000});
          conversation=body.conversation_id||conversation;
          const trace=(body.traces||[]).filter(t=>t.engine).at(-1);
          const eng=trace&&trace.engine||{};
          const response={...eng,result:eng.answer||eng.result,clarify:eng.status==='clarify'||eng.clarify};
          const graded=cp.spawnSync('python',['-m','regress.browser_gold','grade'],{input:JSON.stringify({...test,response}),encoding:'utf8'});
          if(graded.status)throw new Error('gold comparator failed');
          const grade=JSON.parse(graded.stdout);
          const execution=eng.execution||{};
          if(test.expected!==null&&(execution.actual!==({both:'verify',py:'python',sql:'sql'}[mode])||(mode==='both'&&!execution.verified))){
            grade.passed=false;grade.reason=(grade.reason?grade.reason+'; ':'')+'requested execution mode was not verified';
          }
          const ui=await page.evaluate(()=>({failure:FAILMSG,views:VIEWS.length,sections:[...new Set(VIEWS.map(v=>v.section).filter(Boolean))],rows:BOOK.filter(s=>s.result&&!s.stale).map(s=>s.rows)}));
          if(ui.failure||errors.length){grade.passed=false;grade.reason=(grade.reason||'')+'; browser error';}
          const record={dataset:dataset.dataset,mode,...test,...grade,elapsedMs:Date.now()-start,execution,ui,upload,errors:[...errors],response:body};
          report.records.push(record);save();console.log(JSON.stringify({dataset:record.dataset,mode,question:test.question,passed:grade.passed,reason:grade.reason,elapsedMs:record.elapsedMs}));
          if(!grade.passed)await page.screenshot({path:path.join(path.dirname(destination),dataset.dataset+'-'+mode+'-'+i+'.png'),fullPage:true});
        }
      }catch(error){report.records.push({dataset:dataset.dataset,mode,passed:false,error:error.message});save();console.log(dataset.dataset,mode,'FAILED',error.message);}
      finally{
        // Delete only the conversation minted by THIS iteration, never a user's
        // existing history. Keep report evidence locally before cleanup.
        if(conversation)try{await page.evaluate(async id=>{const token=await window.ensureToken();await fetch('/api/conversation/delete',{method:'POST',headers:{'content-type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify({id})});},conversation);}catch(_){}
        await context.close();
      }
    }
  }}finally{await browser.close();report.finished=new Date().toISOString();save();}
  process.exitCode=report.records.some(r=>!r.passed)?1:0;
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
