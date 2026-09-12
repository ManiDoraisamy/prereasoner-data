// Read binary fixtures through the EXACT bounded browser worker, not a second
// XML parser that compacts missing cells or bypasses layout normalization.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../web/public');
function readWorkbook(file){
  const bytes=fs.readFileSync(file);
  let result;
  const context={Uint8Array,Date,console,postMessage:value=>{result=value;}};
  context.self=context;
  context.globalThis=context;
  context.importScripts=(...files)=>files.forEach(f=>vm.runInContext(fs.readFileSync(path.join(root,f),'utf8'),context));
  vm.createContext(context);
  context.importScripts('/lib/xlsx-worker.js');
  if(bytes.length>context.self.UPLOAD_LIMITS.workbookBytes)throw new Error('workbook exceeds upload limit');
  context.self.onmessage({data:{buffer:bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)}});
  if(!result||!result.ok)throw new Error(file+': '+(result&&result.error||'worker failed'));
  return result.sheets;
}
module.exports={readWorkbook};
if(require.main===module){
  try{process.stdout.write(JSON.stringify(process.argv.slice(2).map(file=>({file,sheets:readWorkbook(file)}))));}
  catch(error){console.error(error.message);process.exitCode=1;}
}
