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
assert.equal(normalize([['id','id'],[1,2]]).csv,'id [column A],id [column B]\n1,2');
const summaries=normalize([['id','amount'],[1,2],['Total',2]]);
assert.equal(summaries.csv,'id,amount\n1,2');
assert.equal(summaries.summaryCsv,'id,amount\nTotal,2');
const missing=XLSX.utils.aoa_to_sheet([['id','amount'],[1,2]]);
missing.B2={t:'n',f:'1+1'};
assert.match(WORKBOOK_IMPORT.normalize(missing,XLSX).csv,/#UNAVAILABLE!/);
const blankFormula=XLSX.utils.aoa_to_sheet([['amount'],[1]]);blankFormula.A2={t:'n',f:'1+1'};
assert.match(WORKBOOK_IMPORT.normalize(blankFormula,XLSX).csv,/#UNAVAILABLE!/);
const timestamp=new Date('2026-06-01T13:45:00.000Z');
assert.match(normalize([['id','created'],[1,timestamp]]).csv,/2026-06-01T13:45:00/);
const merged=XLSX.utils.aoa_to_sheet([['id','amount'],[1,2],[null,3]]);
merged['!merges']=[{s:{r:1,c:0},e:{r:2,c:0}}];
assert.equal(WORKBOOK_IMPORT.normalize(merged,XLSX).csv,'id,amount\n1,2\n,3');
const grouped=XLSX.utils.aoa_to_sheet([['Order','Amounts',null],['ID','Net','Tax'],[1,10,2]]);
grouped['!merges']=[{s:{r:0,c:1},e:{r:0,c:2}}];
assert.match(WORKBOOK_IMPORT.normalize(grouped,XLSX).csv,/"?ID"?,Amounts Net,Amounts Tax/);
const yearly=XLSX.utils.aoa_to_sheet([['State',2024,null,2023,null],['Name','Mines','Production','Mines','Production'],['Alabama',18,14382,20,12043]]);
yearly['!merges']=[{s:{r:0,c:1},e:{r:0,c:2}},{s:{r:0,c:3},e:{r:0,c:4}}];
assert.equal(WORKBOOK_IMPORT.normalize(yearly,XLSX).csv.split('\n')[0],'Name,2024 Mines,2024 Production,2023 Mines,2023 Production');
const yearsHead=normalize([['Doctorates by field'],[],['Field',1978,1983,1988,1993],['Education',7194,7147,6362,6689],['Humanities',4231,3500,3558,4482]]);
assert.equal(yearsHead.import.headerRow,3);
assert.equal(yearsHead.csv.split('\n')[0],'Field,1978,1983,1988,1993');
assert.equal(yearsHead.csv.split('\n').length,3);
assert.equal(normalize([['Alabama',2020,1999,2005],['Alaska',1,2,3]]).import.headerRow,null);
assert.equal(normalize([['Region',5,6,7],['East',1,2,3]]).import.headerRow,null);
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
// Messy headers preserve every value and its source position. Suspected shifts
// use neutral names rather than guessing a binding that could yield wrong money.
const shifted=[['order ID','customer','city','tier','ordered','currency','amount'],
  [1,101,'Sherlock Holmes','London','Gold','Magnifying Glass','GBP',118],
  [2,102,'Sherlock Holmes','London','Gold','Calabash Pipe','GBP',95]];
const [shiftedSheet]=normalizeGrids([{name:'sales',rows:shifted}]);
assert.match(shiftedSheet.csv,/Column A,Column B,Column C,Column D,Column E,Column F,Column G,Column H/);
assert.match(shiftedSheet.csv,/1,101,Sherlock Holmes,London,Gold,Magnifying Glass,GBP,118/);
assert.match(shiftedSheet.import.warnings.join(' '),/shifted/);
const [realigned]=normalizeGrids([{name:'sales',rows:[[null,...shifted[0]],...shifted.slice(1)]}]);
assert.match(realigned.csv,/Column A,order ID,customer,city,tier,ordered,currency,amount/);
assert.deepEqual(Array.from(realigned.import.leftOutColumns),[]);
const [helper]=normalizeGrids([{name:'orders',rows:[['id','amount',null],[1,10,'check'],[2,20,'ok']]}]);
assert.equal(helper.csv,'id,amount,Column C\n1,10,check\n2,20,ok');
const [numbers]=normalizeGrids([{name:'numbers',rows:[[1,2],[3,4]]}]);
assert.equal(numbers.csv,'Column A,Column B\n1,2\n3,4');
assert.equal(numbers.import.headerRow,null);
const [dupes]=normalizeGrids([{name:'dupes',rows:[['id','customer','customer'],[1,'A','B']]}]);
assert.equal(dupes.csv,'id,customer [column B],customer [column C]\n1,A,B');
const [errors]=normalizeGrids([{name:'errors',rows:[['id','ratio'],[1,'#DIV/0!']],errors:[[false,false],[false,true]]}]);
assert.match(errors.csv,/#DIV\/0!/);
assert.match(errors.import.warnings.join(' '),/not numbers/);
const sections=normalizeGrids([{name:'sales',rows:[['item','amount'],['A',2],['B',3],['Total',5]]}]);
assert.equal(sections.length,2);
assert.equal(sections[0].csv,'item,amount\nA,2\nB,3');
assert.equal(sections[1].csv,'item,amount\nTotal,5');
assert.equal(sections[1].name,'sales summaries');
// A row led by a total label is a summary even when it carries attributes: a regional subtotal or a
// total that names its currency was counted twice as data (2026-10-04).
const regional=normalize([['item','region','amount'],['A','FR',2],['Total','FR',3]]);
assert.equal(regional.csv,'item,region,amount\nA,FR,2');
assert.equal(regional.summaryCsv,'item,region,amount\nTotal,FR,3');
// Exact source rows survive skipped blanks and separated totals. Compact runs
// identify physical observations, including duplicate-looking source rows.
const [lineageData,lineageTotals]=normalizeGrids([{name:'Ledger',rows:[['item','amount'],['A',2],[],['A',2],['Total',4]]}]);
assert.deepEqual(JSON.parse(JSON.stringify(lineageData.import.sourceRows)),
  {encoding:'ranges',count:2,ranges:[[0,2,1],[1,4,1]]});
assert.deepEqual(JSON.parse(JSON.stringify(lineageTotals.import.sourceRows)),
  {encoding:'ranges',count:1,ranges:[[0,5,1]]});
assert.equal(lineageData.import.sourceSheet,'Ledger');
assert.equal(lineageTotals.import.sourceSheet,'Ledger');
assert.equal(lineageTotals.import.dataRows,1);
const [contiguous]=normalizeGrids([{name:'Run',rows:[['id','amount'],[1,2],[2,3],[3,4]]}]);
assert.deepEqual(JSON.parse(JSON.stringify(contiguous.import.sourceRows)),
  {encoding:'ranges',count:3,ranges:[[0,2,3]]});
// Embedded newlines in a summary cell are not extra observations.
const [notesData,notesTotal]=normalizeGrids([{name:'Notes',rows:[['item','note','amount'],['A','ok',2],['Total','first\nsecond',2]]}]);
assert.equal(notesData.import.dataRows,1);
assert.equal(notesTotal.import.dataRows,1);
assert.deepEqual(JSON.parse(JSON.stringify(notesTotal.import.sourceRows)),
  {encoding:'ranges',count:1,ranges:[[0,3,1]]});
assert.equal(normalize([['region','amount','currency'],['FR',100,'USD'],['DE',50,'USD'],['Total',150,'USD']]).csv,
  'region,amount,currency\nFR,100,USD\nDE,50,USD');
// Summaries give way before a workbook is refused: five tabs with a total row each were ten tables,
// over the limit of eight, with no picker in the Sheets sidebar (2026-10-04).
require('../public/lib/upload-limits.js');
const totalled=name=>({name,rows:[['item','amount'],['A',2],['B',3],['Total',5]]});
const five=WORKBOOK_IMPORT.convert({grids:['N','S','E','W','C'].map(totalled)},XLSX,UPLOAD_LIMITS);
assert.equal(five.ok,true,five.error);
assert.deepEqual(five.sheets.map(sheet=>sheet.name),['N','N summaries','S','S summaries','E','E summaries','W','C']);
assert.match(five.sheets[6].import.warnings.join(' '),/left out to avoid counting them twice/);
assert.match(five.sheets[0].import.warnings.join(' '),/separated into a summaries table/);
const eight=WORKBOOK_IMPORT.convert({grids:'ABCDEFGH'.split('').map(totalled)},XLSX,UPLOAD_LIMITS);
assert.equal(eight.ok,true,eight.error);
assert.deepEqual(eight.sheets.map(sheet=>sheet.name),'ABCDEFGH'.split(''));
const two=WORKBOOK_IMPORT.convert({grids:['N','S'].map(totalled)},XLSX,UPLOAD_LIMITS);
assert.deepEqual(two.sheets.map(sheet=>sheet.name),['N','N summaries','S','S summaries']);
const [groupedGrid]=normalizeGrids([{name:'grouped',rows:[['Order','Amounts',null],['ID','Net','Tax'],[1,10,2]],
  merges:[{s:{r:0,c:1},e:{r:0,c:2}}]}]);
assert.match(groupedGrid.csv,/"?ID"?,Amounts Net,Amounts Tax/);
// The server CSV parser and the browser importer use the same positional
// naming contract, including UTF-8 PostgreSQL's 63-byte identifier bound.
const headers=['amount','amount','', '東京'.repeat(40), 'x'.repeat(52)+' [column C]', 'x'.repeat(100),
  // A Forms grid's items differ only at the end, past 63 bytes (2026-10-04).
  'How satisfied are you with the following aspects of our service? [Speed]',
  'How satisfied are you with the following aspects of our service? [Support]'];
const imported=normalize([headers,[1,2,3,4,5,6]]);
const expected=JSON.parse(execFileSync('python',['-c',
  'import json,sys; from engine.column_names import canonical_columns; print(json.dumps(canonical_columns(json.loads(sys.stdin.buffer.read().decode("utf-8")))))'],
  {input:JSON.stringify(headers),encoding:'utf8'}));
assert.deepEqual(imported.import.columnBindings.map(binding=>binding.name),expected);
assert(expected.every(name=>Buffer.byteLength(name,'utf8')<=63));
// A workbook whose bytes are CSV text keeps each number's text, for the engine's one parser to read:
// SheetJS dropped every comma, so "1,50" came through as 150 and "1.234,56" as 1.23456 (release
// review, 2026-10-07). A number a real workbook formats with grouping still arrives as its value (above).
const csvText=WORKBOOK_IMPORT.convert({buffer:Buffer.from('id,amount\n1,"1,50"\n2,"1.234,56"\n3,"1,234.56"\n4,"12,34,567"\n5,-5\n')},
  XLSX,UPLOAD_LIMITS);
assert.equal(csvText.ok,true,csvText.error);
assert.equal(csvText.sheets[0].csv,'id,amount\n1,"1,50"\n2,"1.234,56"\n3,"1,234.56"\n4,"12,34,567"\n5,-5');
// A percentage cell is the percent the sheet shows (0.2 under 0% is 20%), through a file and a host grid
// alike: the stored fraction went in, and a "tax percent" column applied 20% as 0.2% (release review,
// revision 2, 2026-10-07). The same fraction without a percentage format stays the fraction.
const rates=[['item','tax_percent','discount'],['A',0.2,0.2],['B',0.075,-0.05],['C',-0.05,0.333]];
const rateFormats=[[],['General','0%','General'],['General','0.0%','General'],['General','0%','General']];
const rateSheet=XLSX.utils.aoa_to_sheet(rates);
rateFormats.forEach((row,r)=>row.forEach((z,c)=>{if(z!=='General')rateSheet[XLSX.utils.encode_cell({r,c})].z=z;}));
const rateBook=XLSX.utils.book_new();XLSX.utils.book_append_sheet(rateBook,rateSheet,'Rates');
const rateFile=WORKBOOK_IMPORT.convert({buffer:Buffer.from(XLSX.write(rateBook,{type:'array',bookType:'xlsx'}))},XLSX,UPLOAD_LIMITS);
assert.equal(rateFile.ok,true,rateFile.error);
assert.equal(rateFile.sheets[0].csv,'item,tax_percent,discount\nA,20%,0.2\nB,7.5%,-0.05\nC,-5%,0.333');
const [rateGrid]=normalizeGrids([{name:'Rates',rows:rates,formats:rateFormats}]);
assert.equal(rateGrid.csv,rateFile.sheets[0].csv,'a host grid reads percentages like a file');
// One tab past the limits refuses the workbook, unless the caller has no picker to choose again from: the Sheets sidebar
// names it and goes on with the others.
{
  const small={name:'Small',rows:[['a','b'],[1,2]]},big={name:'Big',rows:[['note'],['x'.repeat(200)]]};
  const limits={sheets:8,rows:50000,columns:256,tableChars:100,totalChars:150};
  const refused=WORKBOOK_IMPORT.convert({grids:[small,big]},XLSX,limits);
  assert.equal(refused.ok,false);
  assert.equal(refused.error,'Sheet "Big": an expanded worksheet is too large');
  const kept=WORKBOOK_IMPORT.convert({grids:[small,big],leaveOut:true},XLSX,limits);
  assert.deepEqual(kept.sheets.map(sheet=>sheet.name),['Small']);
  assert.deepEqual(kept.leftOut,[{name:'Big',reason:'an expanded worksheet is too large'}]);
  const whole={name:'Whole',rows:[['note'],['y'.repeat(80)]]},more={name:'More',rows:[['note'],['z'.repeat(80)]]};
  assert.equal(WORKBOOK_IMPORT.convert({grids:[whole,more]},XLSX,limits).error,'the expanded workbook is too large');
  const spared=WORKBOOK_IMPORT.convert({grids:[whole,more],leaveOut:true},XLSX,limits);
  assert.deepEqual(spared.sheets.map(sheet=>sheet.name),['Whole']);
  assert.deepEqual(spared.leftOut,[{name:'More',reason:'the expanded workbook is too large'}]);
}
console.log('workbook layout: checks passed (including source-row lineage, 3 downloaded originals, 3 timezones, 2 date systems and host grids)');
