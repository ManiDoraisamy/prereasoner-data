// The tables a client sends the engine for a workbook: its worksheets through the browser's importer
// (web/public/lib/workbook-import.js), run by tests/workbook_fixture.js exactly as the upload worker runs it. A web
// upload gives the file's bytes ("file"); the Sheets sidebar and the Excel task pane give the live cells they read
// ("grids": a JSON file of [{name, rows, columnFormats, errorCells, merges, date1904}]). Prints {ok, sheets} or
// {ok: false, error}: the importer's own refusal (more than eight worksheets, a sheet too large) is a result here,
// since it is what the person uploading the workbook meets.
//   node regress/upload_sheets.js file WORKBOOK.xlsx
//   node regress/upload_sheets.js grids GRIDS.json
const fs=require('node:fs');
const {readWorkbook,normalizeGrids}=require('../tests/workbook_fixture.js');
const [mode,source]=process.argv.slice(2);
let out;
try{
  if(mode!=='file'&&mode!=='grids')throw new Error('usage: upload_sheets.js file|grids SOURCE');
  const sheets=mode==='file'?readWorkbook(source):normalizeGrids(JSON.parse(fs.readFileSync(source,'utf8')));
  out={ok:true,sheets:sheets.map(sheet=>({name:sheet.name,csv:sheet.csv,section:sheet.import&&sheet.import.section||null,
    headerRow:sheet.import&&sheet.import.headerRow||null,warnings:sheet.import&&sheet.import.warnings||[]}))};
}catch(error){
  out={ok:false,error:String(error&&error.message||error)};
}
process.stdout.write(JSON.stringify(out));
