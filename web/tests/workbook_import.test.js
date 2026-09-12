const assert=require('node:assert/strict');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const XLSX=require('../public/vendor/xlsx-0.20.3.full.min.js');
require('../public/lib/workbook-import.js');
const {readWorkbook}=require('../../tests/workbook_fixture.js');
const normalize=rows=>WORKBOOK_IMPORT.normalize(XLSX.utils.aoa_to_sheet(rows,{UTC:true}),XLSX);
assert.equal(normalize([['Report'],[],['id','amount'],[1,0],[2,10]]).import.headerRow,3);
assert.match(normalize([['id','amount'],[1,0]]).csv,/1,0/);
assert.throws(()=>normalize([['id','id'],[1,2]]),/Duplicate/);
assert.throws(()=>normalize([['id','amount'],[1,2],['Total',2]]),/double-counting/);
const missing=XLSX.utils.aoa_to_sheet([['id','amount'],[1,2]]);
missing.B2={t:'n',f:'1+1'};
assert.throws(()=>WORKBOOK_IMPORT.normalize(missing,XLSX),/no cached value/);
const blankFormula=XLSX.utils.aoa_to_sheet([['amount'],[1]]);blankFormula.A2={t:'n',f:'1+1'};
assert.throws(()=>WORKBOOK_IMPORT.normalize(blankFormula,XLSX),/no cached value/);
const timestamp=new Date('2026-06-01T13:45:00.000Z');
assert.match(normalize([['id','created'],[1,timestamp]]).csv,/2026-06-01T13:45:00/);
const merged=XLSX.utils.aoa_to_sheet([['id','amount'],[1,2],[null,3]]);
merged['!merges']=[{s:{r:1,c:0},e:{r:2,c:0}}];
assert.throws(()=>WORKBOOK_IMPORT.normalize(merged,XLSX),/Merged data/);
const grouped=XLSX.utils.aoa_to_sheet([['Order','Amounts',null],['ID','Net','Tax'],[1,10,2]]);
grouped['!merges']=[{s:{r:0,c:1},e:{r:0,c:2}}];
assert.match(WORKBOOK_IMPORT.normalize(grouped,XLSX).csv,/"?ID"?,Amounts Net,Amounts Tax/);
const fixtures=[['eval-formesign-assets-xls/assets.xls',3,4],['eval-neartail-supplier-report-xlsx/payments.xlsx',30,11],
  ['eval-formesign-procurement-xlsx/procurement.xlsx',1007,2]];
for(const [file,rows,header] of fixtures){
  const sheets=readWorkbook(path.join(__dirname,'../public/dataset',file));
  assert.equal(sheets[0].import.dataRows,rows);
  assert.equal(sheets[0].import.headerRow,header);
  assert(!sheets[0].csv.includes('Last reviewed:'));
}
assert(readWorkbook(path.join(__dirname,'../public/dataset/eval-formesign-assets-xls/assets.xls'))[0].csv.includes('2012-02-06'));
const fixture=path.resolve(__dirname,'../public/dataset/eval-formesign-assets-xls/assets.xls');
const worker=path.resolve(__dirname,'../../tests/workbook_fixture.js');
const original=readWorkbook(fixture)[0].csv;
for(const zone of ['UTC','America/Los_Angeles','Pacific/Auckland']){
  const csv=execFileSync(process.execPath,['-e',`process.stdout.write(require(${JSON.stringify(worker)}).readWorkbook(${JSON.stringify(fixture)})[0].csv)`],
    {encoding:'utf8',env:{...process.env,TZ:zone}});
  assert.equal(csv,original,'worksheet dates changed in '+zone);
}
console.log('workbook layout: 16 checks passed (including 3 downloaded originals and 3 timezones)');
