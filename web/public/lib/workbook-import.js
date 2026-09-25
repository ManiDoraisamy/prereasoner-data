// Conservative layout normalization, shared by the browser worker and release
// evaluator. Formatting is not business meaning: never invent values, fill down
// data cells, execute formulas/macros, or silently include an ambiguous subtotal.
(function(root){
  'use strict';
  const filled=v=>v!==null&&v!==undefined&&v!=='';
  const text=v=>typeof v==='string'&&v.trim()!=='';
  const DAY=86400000;
  // SheetJS reads an elapsed-time cell ([h]:mm) as a Date near its epoch. Give back the day count
  // Excel stores, so a duration stays the number the Excel add-in (office/excel/host.js) also
  // keeps; root.NUMBER_FORMAT (lib/number-format.js) decides which formats are elapsed. In the
  // 1900 system SheetJS counts Excel's fictitious 1900-02-29, so counts below 61 days read one high.
  function elapsedDays(date,date1904){
    const ms=date.getTime()-(date1904?Date.UTC(1904,0,1):Date.UTC(1899,11,30));
    return (!date1904&&ms<61*DAY?ms-DAY:ms)/DAY;
  }
  function normalize(sheet,XLSX,options){
    if(!sheet['!ref'])return null;
    const date1904=Boolean(options&&options.date1904);
    const bounds=XLSX.utils.decode_range(sheet['!ref']);
    const cellAt=(r,c)=>sheet['!data']?sheet['!data'][r+bounds.s.r]?.[c+bounds.s.c]
      :sheet[XLSX.utils.encode_cell({r:r+bounds.s.r,c:c+bounds.s.c})];
    // SheetJS otherwise localizes date objects during extraction. Excel/Sheets
    // cells are timezone-free: keep their wall-clock value across browser zones.
    const raw=XLSX.utils.sheet_to_json(sheet,{header:1,raw:true,defval:null,blankrows:true,UTC:true});
    raw.forEach((r,i)=>r.forEach((v,c)=>{
      const cell=v instanceof Date&&cellAt(i,c);
      if(cell&&root.NUMBER_FORMAT.isElapsed(cell.z))r[c]=elapsedDays(v,date1904);
    }));
    let width=0;
    raw.forEach(r=>r.forEach((v,c)=>{if(filled(v))width=Math.max(width,c+1);}));
    if(!width)return null;
    const rows=raw.map(r=>Array.from({length:width},(_,c)=>r[c]??null));
    const count=r=>r.filter(filled).length;
    const letter=c=>XLSX.utils.encode_col(c+bounds.s.c);
    const merges=sheet['!merges']||[];
    // The header row names the fields: the first row that names every populated column or, failing
    // that, the first all-text row that names at least two thirds of them. A column with values but
    // no header is not a field (row numbers, a helper column): it is left out, and the import says so;
    // nothing is named for it. Prefix rows must be narrower metadata, not an earlier rectangular data
    // table that we would discard.
    const noHeader='No unambiguous header found in the first 64 rows. Use one named column per field.';
    let start=rows.findIndex((r,i)=>i<64&&r.every(text));
    if(start<0)start=rows.findIndex((r,i)=>i<64&&count(r)>=2&&count(r)*3>=width*2&&r.every(v=>!filled(v)||text(v)));
    if(start<0)throw new Error(noHeader);
    const unnamed=rows[start].map((v,c)=>c).filter(c=>!filled(rows[start][c]));
    // A cell under a merged header belongs to that header's group: one name for two columns is ambiguous.
    if(unnamed.some(c=>merges.some(m=>m.s.r<=start+bounds.s.r&&m.e.r>=start+bounds.s.r&&m.s.c<=c+bounds.s.c&&m.e.c>=c+bounds.s.c)))
      throw new Error(noHeader);
    const below=c=>rows.slice(start+1).map(r=>r[c]).filter(filled);
    const leftOut=unnamed.filter(c=>below(c).length);
    // A header row one column to the left of its data (a column inserted without moving the headers)
    // leaves only the last column unnamed, holding numbers, while the last header sits over text:
    // every answer would read the wrong column, so the sheet is refused with the fix.
    const last=width-1, share=(c,test)=>below(c).filter(test).length/Math.max(1,below(c).length);
    if(leftOut.length===1&&leftOut[0]===last&&unnamed.length===1&&share(last,v=>typeof v==='number')>=0.8
       &&share(last-1,v=>typeof v==='string'&&!/^-?[\d.,]+$/.test(v.trim()))>=0.8){
      throw new Error('Column '+letter(last)+' has values but no header, and the headers look one column to the left of their data ('
        +letter(last-1)+(start+bounds.s.r+1)+' "'+rows[start][last-1].trim()+'" is above "'+below(last-1)[0]+'"). Put each header above its data.');
    }
    if(rows.slice(0,start).some(r=>count(r)===width))
      throw new Error('Multiple or ambiguous header rows. Select a single table before uploading.');
    const keep=rows[start].map((v,c)=>c).filter(c=>!unnamed.includes(c));
    let columns=keep.map(c=>rows[start][c].trim());
    if(start>0&&count(rows[start-1])>=2){
      const parent=rows[start-1], absolute=start-1+bounds.s.r;
      columns=columns.map((name,i)=>{
        const c=keep[i];
        const merge=merges.find(m=>m.s.r===absolute&&m.e.r===absolute&&m.s.c<=c+bounds.s.c&&m.e.c>=c+bounds.s.c);
        const group=merge&&parent[merge.s.c-bounds.s.c];
        return text(group)&&group.trim()!==name?group.trim()+' '+name:name;
      });
    }
    if(new Set(columns.map(c=>c.toLowerCase())).size!==columns.length)
      throw new Error('Duplicate column headers. Give each field a unique name.');
    let end=rows.length, validationEnd=rows.length;
    while(end>start+1&&!count(rows[end-1]))end--;
    // A separated, sparse final annotation block is metadata, not table rows.
    // Require an explicit note label: density alone would discard valid records.
    for(let i=start+1;i<end;i++){
      const values=rows[i].filter(filled);
      if(i>start+1&&!count(rows[i-1])&&values.length===1&&typeof values[0]==='string'&&
         /^(?:notes?|source|last reviewed|last updated)\s*:/i.test(values[0])&&
         rows.slice(i).every(r=>count(r)<=1)) {end=i;validationEnd=i;break;}
    }
    const data=[]; const skipped=[];
    if(merges.some(m=>m.e.r>=start+1+bounds.s.r&&m.s.r<end+bounds.s.r&&
        m.s.c<width+bounds.s.c&&m.e.c>=bounds.s.c))
      throw new Error('Merged data cells are ambiguous. Unmerge the detail table and supply each record explicitly.');
    for(let i=start+1;i<end;i++){
      if(!count(rows[i])){skipped.push(i+bounds.s.r+1);continue;}
      const row=rows[i];
      if(row.some(v=>typeof v==='string'&&/^(grand total|sub[ -]?total|total)\s*:?$/i.test(v.trim())))
        throw new Error('A total/subtotal row is mixed with records. Select the detail table to avoid double-counting.');
      data.push(keep.map(c=>row[c]).map(v=>{
        if(!(v instanceof Date))return v;
        const iso=v.toISOString();
        // Excel dates carry no timezone. Preserve non-midnight time components
        // instead of silently truncating a timestamp to a calendar date.
        return iso.endsWith('T00:00:00.000Z')?iso.slice(0,10):iso.slice(0,-1);
      }));
    }
    // The input file's cached values are authoritative; no formula is executed.
    for(let r=start+1;r<validationEnd;r++)for(const c of keep){
      const cell=cellAt(r,c);
      if(cell&&cell.f&&cell.v==null)throw new Error('A formula has no cached value. Recalculate and save the workbook in Excel first.');
      if(cell&&cell.t==='e')throw new Error('The table contains an Excel formula error. Correct it before uploading.');
    }
    if(!data.length)return null;
    const normalized=XLSX.utils.aoa_to_sheet([columns,...data]);
    return {csv:XLSX.utils.sheet_to_csv(normalized,{blankrows:false}),import:{
      version:1,method:'deterministic-layout',headerRow:start+bounds.s.r+1,
      dataRows:data.length,columns:keep.length,leftOutColumns:leftOut.map(letter),skippedBlankRows:skipped,
      prefixRows:start,suffixRows:rows.length-end,formulaValues:'cached-only',
    }};
  }
  // A host that reads live cells (the Excel task pane, the Sheets sidebar) passes grids instead of
  // file bytes: [{name, rows, formats, errors, merges, date1904}]. They are written as the .xlsx an
  // export of those cells would be, and the worker reads it like any upload, so ONE reader and ONE
  // normalize() decide headers, dates, durations, merged cells, totals and errors for every source.
  const ERROR_CODES={'#NULL!':0x00,'#DIV/0!':0x07,'#VALUE!':0x0F,'#REF!':0x17,'#NAME?':0x1D,'#NUM!':0x24,'#N/A':0x2A};
  function gridWorkbook(grids,XLSX){
    const book=XLSX.utils.book_new();
    let date1904=false;
    grids.forEach((grid,index)=>{
      const rows=grid.rows||[],formats=grid.formats||[],errors=grid.errors||[];
      const sheet=XLSX.utils.aoa_to_sheet(rows.map(r=>r.map(v=>v===''?null:v)));
      rows.forEach((row,r)=>row.forEach((value,c)=>{
        const address=XLSX.utils.encode_cell({r,c});
        if(errors[r]&&errors[r][c]){
          sheet[address]={t:'e',v:ERROR_CODES[String(value)]??0x0F,w:String(value)};return;
        }
        const format=formats[r]&&formats[r][c];
        if(typeof value==='number'&&sheet[address]&&format&&format!=='General')sheet[address].z=format;
      }));
      if(Array.isArray(grid.merges)&&grid.merges.length)sheet['!merges']=grid.merges;
      date1904=date1904||Boolean(grid.date1904);
      XLSX.utils.book_append_sheet(book,sheet,String(grid.name||'Sheet'+(index+1)).slice(0,31));
    });
    if(date1904)book.Workbook={WBProps:{date1904:true}};
    return XLSX.write(book,{type:'array',bookType:'xlsx'});
  }
  // The whole conversion: file bytes ({buffer}) or live cells ({grids}) -> {ok, sheets} or {ok:false,
  // error}, within the upload limits. The upload worker (lib/xlsx-worker.js) runs it off the page; the
  // Google Sheets add-on's sidebar runs it on its page.
  function convert(data,XLSX,limits){
    try{
      const bytes=Array.isArray(data.grids)?new Uint8Array(gridWorkbook(data.grids,XLSX)):new Uint8Array(data.buffer);
      const workbook=XLSX.read(bytes,{
        type:'array',dense:true,sheetRows:limits.rows+2,
        cellFormula:true,cellDates:true,cellHTML:false,cellNF:true,cellStyles:false,bookVBA:false,
      });
      // Number formats are kept (cellNF) so an elapsed duration can be told from a date.
      const date1904=Boolean(workbook.Workbook&&workbook.Workbook.WBProps&&workbook.Workbook.WBProps.date1904);
      if(workbook.SheetNames.length>limits.sheets)throw new Error('workbooks may contain at most 8 worksheets');
      let total=0;
      const sheets=[];
      for(const name of workbook.SheetNames){
        const sheet=workbook.Sheets[name];
        // A worksheet's own problem names it: a workbook (or a whole spreadsheet) can have several.
        let normalized;
        try{
          const ref=sheet['!fullref']||sheet['!ref'], range=ref?XLSX.utils.decode_range(ref):null;
          if(range&&range.e.r-range.s.r+1>limits.rows+1)throw new Error('each worksheet may contain at most 10,000 data rows');
          if(range&&range.e.c-range.s.c+1>limits.columns)throw new Error('each worksheet may contain at most 256 columns');
          normalized=normalize(sheet,XLSX,{date1904});
          if(normalized&&normalized.csv.length>limits.tableChars)throw new Error('an expanded worksheet is too large');
        }catch(error){throw new Error('Sheet "'+name+'": '+((error&&error.message)||String(error)));}
        if(!normalized)continue;
        total+=normalized.csv.length;
        if(total>limits.totalChars)throw new Error('the expanded workbook is too large');
        sheets.push({name,...normalized});
      }
      return {ok:true,sheets};
    }catch(error){
      return {ok:false,error:(error&&error.message)||String(error)};
    }
  }
  root.WORKBOOK_IMPORT={normalize,gridWorkbook,convert};
})(typeof window==='undefined'?globalThis:window);
