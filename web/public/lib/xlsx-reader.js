// Bounded browser-side spreadsheet ingestion. Parsing runs in a disposable worker so
// a malformed workbook cannot freeze the application tab.
(function(root){
  'use strict';
  const MAX_XLSX_BYTES=root.UPLOAD_LIMITS.workbookBytes;
  const MAX_TEXT_BYTES=root.UPLOAD_LIMITS.textBytes;
  const MAX_OUTPUT_CHARS=root.UPLOAD_LIMITS.totalChars;
  const WORKER_TIMEOUT_MS=root.UPLOAD_LIMITS.timeoutMs;

  function readWorkbook(file){
    if(!file||typeof file.arrayBuffer!=='function')return Promise.reject(new Error('invalid spreadsheet file'));
    if(file.size>MAX_XLSX_BYTES)return Promise.reject(new Error('spreadsheet files must be 8 MB or smaller'));
    return file.arrayBuffer().then(buffer=>new Promise((resolve,reject)=>{
      const worker=new Worker('/lib/xlsx-worker.js');
      let settled=false;
      const finish=(fn,value)=>{if(settled)return;settled=true;clearTimeout(timer);worker.terminate();fn(value);};
      const timer=setTimeout(()=>finish(reject,new Error('spreadsheet parsing timed out')),WORKER_TIMEOUT_MS);
      worker.onmessage=e=>e.data&&e.data.ok
        ?finish(resolve,e.data.sheets||[])
        :finish(reject,new Error((e.data&&e.data.error)||'could not read the spreadsheet'));
      worker.onerror=()=>finish(reject,new Error('could not read the spreadsheet'));
      worker.postMessage({buffer},[buffer]);
    }));
  }

  async function readText(file){
    if(!file||typeof file.text!=='function')throw new Error('invalid text file');
    if(file.size>MAX_TEXT_BYTES)throw new Error('CSV and text files must be 2 MB or smaller');
    const text=await file.text();
    if(text.length>root.UPLOAD_LIMITS.tableChars)throw new Error('decoded table is too large');
    return text;
  }

  root.XLSX_READER={readWorkbook,readText,MAX_XLSX_BYTES,MAX_TEXT_BYTES,MAX_OUTPUT_CHARS};
})(window);
