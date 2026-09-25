// Read binary fixtures through the EXACT bounded browser worker, not a second
// XML parser that compacts missing cells or bypasses layout normalization.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../web/public');
function runWorker(data){
  let result;
  const context={Uint8Array,ArrayBuffer,Date,console,postMessage:value=>{result=value;}};
  context.self=context;
  context.globalThis=context;
  context.importScripts=(...files)=>files.forEach(f=>vm.runInContext(fs.readFileSync(path.join(root,f),'utf8'),context));
  vm.createContext(context);
  context.importScripts('/lib/xlsx-worker.js');
  if(data.buffer&&data.buffer.byteLength>context.self.UPLOAD_LIMITS.workbookBytes)throw new Error('workbook exceeds upload limit');
  context.self.onmessage({data});
  return result;
}
function readWorkbook(file){
  const bytes=fs.readFileSync(file);
  const result=runWorker({buffer:bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)});
  if(!result||!result.ok)throw new Error(file+': '+(result&&result.error||'worker failed'));
  return result.sheets;
}
// The grids a live host (Excel task pane, Sheets sidebar) posts to the same worker.
function normalizeGrids(grids){
  const result=runWorker({grids});
  if(!result||!result.ok)throw new Error(result&&result.error||'worker failed');
  return result.sheets;
}
module.exports={readWorkbook,normalizeGrids};
if(require.main===module){
  try{process.stdout.write(JSON.stringify(process.argv.slice(2).map(file=>({file,sheets:readWorkbook(file)}))));}
  catch(error){console.error(error.message);process.exitCode=1;}
}
