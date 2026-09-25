/* global XLSX, importScripts */
'use strict';

// SheetJS CE 0.20.3 is vendored at a fixed hash. Keep parsing off the UI thread
// and cap both the compressed input (in xlsx-reader.js) and expanded output here.
importScripts('/vendor/xlsx-0.20.3.full.min.js');
importScripts('/lib/upload-limits.js');
importScripts('/lib/number-format.js');
importScripts('/lib/workbook-import.js');

const MAX_SHEETS=UPLOAD_LIMITS.sheets;
const MAX_ROWS=UPLOAD_LIMITS.rows;
const MAX_COLUMNS=UPLOAD_LIMITS.columns;
const MAX_SHEET_CHARS=UPLOAD_LIMITS.tableChars;
const MAX_OUTPUT_CHARS=UPLOAD_LIMITS.totalChars;

function dimensions(sheet){
  const ref=sheet['!fullref']||sheet['!ref'];
  if(!ref)return {rows:0,columns:0};
  const range=XLSX.utils.decode_range(ref);
  return {rows:range.e.r-range.s.r+1,columns:range.e.c-range.s.c+1};
}

// A file upload sends {buffer}; a host reading live cells (Excel, Sheets) sends {grids}, written as
// the .xlsx those cells would export to. Both then take the same read, limits and normalize rule.
function worksheets(data){
  const bytes=Array.isArray(data.grids)?new Uint8Array(WORKBOOK_IMPORT.gridWorkbook(data.grids,XLSX))
    :new Uint8Array(data.buffer);
  const workbook=XLSX.read(bytes,{
    type:'array',dense:true,sheetRows:MAX_ROWS+2,
    cellFormula:true,cellDates:true,cellHTML:false,cellNF:true,cellStyles:false,bookVBA:false,
  });
  // Number formats are kept (cellNF) so an elapsed duration can be told from a date.
  const date1904=Boolean(workbook.Workbook&&workbook.Workbook.WBProps&&workbook.Workbook.WBProps.date1904);
  return workbook.SheetNames.map(name=>({name,sheet:workbook.Sheets[name],date1904}));
}

self.onmessage=function(event){
  try{
    const inputs=worksheets(event.data);
    if(inputs.length>MAX_SHEETS)throw new Error('workbooks may contain at most 8 worksheets');
    let total=0;
    const sheets=[];
    for(const {name,sheet,date1904} of inputs){
      // A worksheet's own problem names it: a workbook (or a whole spreadsheet) can have several.
      let normalized;
      try{
        const size=dimensions(sheet);
        if(size.rows>MAX_ROWS+1)throw new Error('each worksheet may contain at most 10,000 data rows');
        if(size.columns>MAX_COLUMNS)throw new Error('each worksheet may contain at most 256 columns');
        normalized=WORKBOOK_IMPORT.normalize(sheet,XLSX,{date1904});
        if(normalized&&normalized.csv.length>MAX_SHEET_CHARS)throw new Error('an expanded worksheet is too large');
      }catch(error){throw new Error('Sheet "'+name+'": '+((error&&error.message)||String(error)));}
      if(!normalized)continue;
      total+=normalized.csv.length;
      if(total>MAX_OUTPUT_CHARS)throw new Error('the expanded workbook is too large');
      sheets.push({name,...normalized});
    }
    self.postMessage({ok:true,sheets});
  }catch(error){
    self.postMessage({ok:false,error:(error&&error.message)||String(error)});
  }
};
