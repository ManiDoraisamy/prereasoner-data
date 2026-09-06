/* global XLSX, importScripts */
'use strict';

// SheetJS CE 0.20.3 is vendored at a fixed hash. Keep parsing off the UI thread
// and cap both the compressed input (in xlsx-reader.js) and expanded output here.
importScripts('/vendor/xlsx-0.20.3.full.min.js');

const MAX_SHEETS=8;
const MAX_ROWS=5000;
const MAX_COLUMNS=256;
const MAX_SHEET_CHARS=2*1024*1024;
const MAX_OUTPUT_CHARS=6*1024*1024;

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
      cellFormula:false,cellHTML:false,cellNF:false,cellStyles:false,bookVBA:false,
    });
    if(workbook.SheetNames.length>MAX_SHEETS)throw new Error('workbooks may contain at most 8 worksheets');
    let total=0;
    const sheets=[];
    for(const name of workbook.SheetNames){
      const sheet=workbook.Sheets[name];
      const size=dimensions(sheet);
      if(size.rows>MAX_ROWS+1)throw new Error('each worksheet may contain at most 5,000 data rows');
      if(size.columns>MAX_COLUMNS)throw new Error('each worksheet may contain at most 256 columns');
      const csv=XLSX.utils.sheet_to_csv(sheet,{blankrows:false});
      if(csv.length>MAX_SHEET_CHARS)throw new Error('an expanded worksheet is too large');
      total+=csv.length;
      if(total>MAX_OUTPUT_CHARS)throw new Error('the expanded workbook is too large');
      if(csv.trim().split(/\r?\n/).filter(Boolean).length>=2)sheets.push({name,csv});
    }
    self.postMessage({ok:true,sheets});
  }catch(error){
    self.postMessage({ok:false,error:(error&&error.message)||String(error)});
  }
};
