/* global XLSX, importScripts */
'use strict';

// SheetJS CE 0.20.3 is vendored at a fixed hash. Keep parsing off the UI thread
// and cap both the compressed input (in xlsx-reader.js) and expanded output here.
importScripts('/vendor/xlsx-0.20.3.full.min.js');
importScripts('/lib/upload-limits.js');
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

self.onmessage=function(event){
  try{
    const bytes=new Uint8Array(event.data.buffer);
    const workbook=XLSX.read(bytes,{
      type:'array',dense:true,sheetRows:MAX_ROWS+2,
      cellFormula:true,cellDates:true,cellHTML:false,cellNF:false,cellStyles:false,bookVBA:false,
    });
    if(workbook.SheetNames.length>MAX_SHEETS)throw new Error('workbooks may contain at most 8 worksheets');
    let total=0;
    const sheets=[];
    for(const name of workbook.SheetNames){
      const sheet=workbook.Sheets[name];
      const size=dimensions(sheet);
      if(size.rows>MAX_ROWS+1)throw new Error('each worksheet may contain at most 10,000 data rows');
      if(size.columns>MAX_COLUMNS)throw new Error('each worksheet may contain at most 256 columns');
      const normalized=WORKBOOK_IMPORT.normalize(sheet,XLSX);
      if(!normalized)continue;
      const csv=normalized.csv;
      if(csv.length>MAX_SHEET_CHARS)throw new Error('an expanded worksheet is too large');
      total+=csv.length;
      if(total>MAX_OUTPUT_CHARS)throw new Error('the expanded workbook is too large');
      sheets.push({name,...normalized});
    }
    self.postMessage({ok:true,sheets});
  }catch(error){
    self.postMessage({ok:false,error:(error&&error.message)||String(error)});
  }
};
