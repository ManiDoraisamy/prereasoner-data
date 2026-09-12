// Conservative layout normalization, shared by the browser worker and release
// evaluator. Formatting is not business meaning: never invent values, fill down
// data cells, execute formulas/macros, or silently include an ambiguous subtotal.
(function(root){
  'use strict';
  const filled=v=>v!==null&&v!==undefined&&v!=='';
  const text=v=>typeof v==='string'&&v.trim()!=='';
  function normalize(sheet,XLSX){
    if(!sheet['!ref'])return null;
    const bounds=XLSX.utils.decode_range(sheet['!ref']);
    const raw=XLSX.utils.sheet_to_json(sheet,{header:1,raw:true,defval:null,blankrows:true});
    let width=0;
    raw.forEach(r=>r.forEach((v,c)=>{if(filled(v))width=Math.max(width,c+1);}));
    if(!width)return null;
    const rows=raw.map(r=>Array.from({length:width},(_,c)=>r[c]??null));
    const count=r=>r.filter(filled).length;
    // A header must name every populated column. Prefix rows must be narrower
    // metadata, not an earlier rectangular data table that we would discard.
    let start=rows.findIndex((r,i)=>i<64&&r.every(text));
    if(start<0)throw new Error('No unambiguous header found in the first 64 rows. Use one named column per field.');
    if(rows.slice(0,start).some(r=>count(r)===width))
      throw new Error('Multiple or ambiguous header rows. Select a single table before uploading.');
    let columns=rows[start].map(v=>v.trim());
    const merges=sheet['!merges']||[];
    if(start>0&&count(rows[start-1])>=2){
      const parent=rows[start-1], absolute=start-1+bounds.s.r;
      columns=columns.map((name,c)=>{
        const merge=merges.find(m=>m.s.r===absolute&&m.e.r===absolute&&m.s.c<=c+bounds.s.c&&m.e.c>=c+bounds.s.c);
        const group=merge&&parent[merge.s.c-bounds.s.c];
        return text(group)&&group.trim()!==name?group.trim()+' '+name:name;
      });
    }
    if(new Set(columns.map(c=>c.toLowerCase())).size!==columns.length)
      throw new Error('Duplicate column headers. Give each field a unique name.');
    let end=rows.length;
    while(end>start+1&&!count(rows[end-1]))end--;
    // A separated, sparse final annotation block is metadata, not table rows.
    // Require an explicit note label: density alone would discard valid records.
    for(let i=start+1;i<end;i++){
      const values=rows[i].filter(filled);
      if(i>start+1&&!count(rows[i-1])&&values.length===1&&typeof values[0]==='string'&&
         /^(?:notes?|source|last reviewed|last updated)\s*:/i.test(values[0])&&
         rows.slice(i).every(r=>count(r)<=1)) {end=i;break;}
    }
    const data=[]; const skipped=[];
    for(let i=start+1;i<end;i++){
      if(!count(rows[i])){skipped.push(i+bounds.s.r+1);continue;}
      const row=rows[i];
      if(row.some(v=>typeof v==='string'&&/^(grand total|sub[ -]?total|total)\s*:?$/i.test(v.trim())))
        throw new Error('A total/subtotal row is mixed with records. Select the detail table to avoid double-counting.');
      data.push(row.map(v=>v instanceof Date?v.toISOString().slice(0,10):v));
    }
    if(!data.length)return null;
    // The input file's cached values are authoritative; no formula is executed.
    for(let r=start+1;r<end;r++)for(let c=0;c<width;c++){
      const cell=sheet['!data']?sheet['!data'][r+bounds.s.r]?.[c+bounds.s.c]
        :sheet[XLSX.utils.encode_cell({r:r+bounds.s.r,c:c+bounds.s.c})];
      if(cell&&cell.f&&!filled(cell.v))throw new Error('A formula has no cached value. Recalculate and save the workbook in Excel first.');
      if(cell&&cell.t==='e')throw new Error('The table contains an Excel formula error. Correct it before uploading.');
    }
    const normalized=XLSX.utils.aoa_to_sheet([columns,...data]);
    return {csv:XLSX.utils.sheet_to_csv(normalized,{blankrows:false}),import:{
      version:1,method:'deterministic-layout',headerRow:start+bounds.s.r+1,
      dataRows:data.length,columns:width,skippedBlankRows:skipped,
      prefixRows:start,suffixRows:rows.length-end,formulaValues:'cached-only',
    }};
  }
  root.WORKBOOK_IMPORT={normalize};
})(typeof window==='undefined'?globalThis:window);
