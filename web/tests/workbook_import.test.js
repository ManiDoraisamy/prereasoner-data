const assert=require('node:assert/strict');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const XLSX=require('../public/vendor/xlsx-0.20.3.full.min.js');
require('../public/lib/number-format.js');
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
// An elapsed duration ([h]:mm) keeps Excel's stored day count, as the Excel add-in keeps it; it
// used to become a timestamp near 1900. Dates and [Red] amounts read as before, in both systems.
const os=require('node:os'),fs=require('node:fs');
for(const date1904 of [false,true]){
  const ws=XLSX.utils.aoa_to_sheet([['worked','day','amount']]);
  const put=(r,c,v,z)=>{ws[XLSX.utils.encode_cell({r,c})]={t:'n',v,z};};
  put(1,0,55/48,'[h]:mm');put(1,1,45292,'yyyy-mm-dd');put(1,2,-1234.5,'#,##0.00;[Red]-#,##0.00');
  put(2,0,0.75,'[mm]:ss');put(2,1,45293,'yyyy-mm-dd');put(2,2,20,'#,##0.00;[Red]-#,##0.00');
  ws['!ref']='A1:C3';
  const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,ws,'Hours');
  if(date1904)book.Workbook={WBProps:{date1904:true}};
  const file=path.join(os.tmpdir(),`elapsed-${date1904?1904:1900}-${process.pid}.xlsx`);
  fs.writeFileSync(file,Buffer.from(XLSX.write(book,{type:'array',bookType:'xlsx'})));
  try{
    const [worked,second]=readWorkbook(file)[0].csv.trim().split('\n').slice(1).map(line=>line.split(','));
    assert.ok(Math.abs(Number(worked[0])-55/48)<1e-9,`elapsed hours stay a day count: ${worked[0]}`);
    assert.ok(Math.abs(Number(second[0])-0.75)<1e-9,`elapsed minutes stay a day count: ${second[0]}`);
    assert.equal(worked[1],date1904?'2028-01-02':'2024-01-01');
    assert.equal(worked[2],'-1234.5');
  }finally{fs.unlinkSync(file);}
}
// Live hosts (the Excel task pane, the Sheets sidebar) post cell grids to the same worker. A grid
// must import exactly like an upload of the same cells, and fail the same way.
const {normalizeGrids}=require('../../tests/workbook_fixture.js');
const cells=[['worked','day','amount','paid'],[55/48,45292,-1234.5,true],[0.75,45293,20,false]];
const cellFormats=[[],['[h]:mm','yyyy-mm-dd','#,##0.00;[Red]-#,##0.00','General'],
  ['[mm]:ss','yyyy-mm-dd','#,##0.00;[Red]-#,##0.00','General']];
for(const date1904 of [false,true]){
  const ws=XLSX.utils.aoa_to_sheet(cells);
  cellFormats.forEach((row,r)=>row.forEach((z,c)=>{if(z&&z!=='General')ws[XLSX.utils.encode_cell({r,c})].z=z;}));
  const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,ws,'Hours');
  if(date1904)book.Workbook={WBProps:{date1904:true}};
  const file=path.join(os.tmpdir(),`grid-${date1904?1904:1900}-${process.pid}.xlsx`);
  fs.writeFileSync(file,Buffer.from(XLSX.write(book,{type:'array',bookType:'xlsx'})));
  try{
    const uploaded=readWorkbook(file)[0];
    const [grid]=normalizeGrids([{name:'Hours',rows:cells,formats:cellFormats,date1904}]);
    assert.equal(grid.csv,uploaded.csv,'a host grid imports exactly like an upload of the same cells');
    assert.equal(JSON.stringify(grid.import),JSON.stringify(uploaded.import));   // results from separate VM realms
  }finally{fs.unlinkSync(file);}
}
// The Sheets add-on screenshot: an index column A was inserted but the header row was not moved, so
// "amount" labels the currency codes and the amounts in H have no header. The add-ins used to name
// that column "column_8" and answer from shifted labels. Every answer would read the wrong column, so
// this layout is refused, with the worksheet, the evidence and the fix.
const shifted=[['order ID','customer','city','tier','ordered','currency','amount'],
  [1,101,'Sherlock Holmes','London','Gold','Magnifying Glass','GBP',118],
  [2,102,'Sherlock Holmes','London','Gold','Calabash Pipe','GBP',95]];
assert.throws(()=>normalizeGrids([{name:'notes',rows:[['note'],['ok']]},{name:'sales',rows:shifted}]),
  /^Error: Sheet "sales": Column H has values but no header, and the headers look one column to the left of their data \(G1 "amount" is above "GBP"\)\. Put each header above its data\.$/);
// The owner's fix moved the headers right and left A1 empty over the row numbers. A column with values
// but no header is not a field: it is left out, and the import says which; nothing is named for it.
const [realigned]=normalizeGrids([{name:'sales',rows:[[null,...shifted[0]],...shifted.slice(1)]}]);
assert.strictEqual(realigned.csv,'order ID,customer,city,tier,ordered,currency,amount\n'
  +'101,Sherlock Holmes,London,Gold,Magnifying Glass,GBP,118\n102,Sherlock Holmes,London,Gold,Calabash Pipe,GBP,95');
assert.strictEqual(JSON.stringify([realigned.import.leftOutColumns,realigned.import.columns,realigned.import.headerRow]),'[["A"],7,1]');
const [helper]=normalizeGrids([{name:'orders',rows:[['id','amount',null],[1,10,'check'],[2,20,'ok']]}]);
assert.strictEqual(helper.csv,'id,amount\n1,10\n2,20');
assert.strictEqual(JSON.stringify(helper.import.leftOutColumns),'["C"]');
// A layout with no header-like row at all keeps the general message.
assert.throws(()=>normalizeGrids([{name:'numbers',rows:[[1,2],[3,4]]}]),/No unambiguous header found in the first 64 rows/);
assert.throws(()=>normalizeGrids([{name:'dupes',rows:[['id','customer','customer'],[1,'A','B']]}]),/Duplicate column headers/);
assert.throws(()=>normalizeGrids([{name:'errors',rows:[['id','ratio'],[1,'#DIV/0!']],errors:[[false,false],[false,true]]}]),
  /formula error/);
const [groupedGrid]=normalizeGrids([{name:'grouped',rows:[['Order','Amounts',null],['ID','Net','Tax'],[1,10,2]],
  merges:[{s:{r:0,c:1},e:{r:0,c:2}}]}]);
assert.match(groupedGrid.csv,/"?ID"?,Amounts Net,Amounts Tax/);
console.log('workbook layout: 32 checks passed (including 3 downloaded originals, 3 timezones, 2 date systems and host grids)');
