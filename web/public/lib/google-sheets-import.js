// Read only the user-selected Google Sheet. Export preserves the layout and typed
// dates that values:batchGet discards, then use the EXACT Excel upload importer.
// OAuth stays in memory; never save it with the normalized tables.
(function(root){
  'use strict';
  const MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
  async function read(token,id,title){
    if(!token||typeof id!=='string'||!/^[-\w]+$/.test(id))throw new Error('Invalid Google Sheet selection.');
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),30000);
    try{
      const response=await fetch('https://www.googleapis.com/drive/v3/files/'+encodeURIComponent(id)
        +'/export?mimeType='+encodeURIComponent(MIME),{
          headers:{Authorization:'Bearer '+token},signal:controller.signal,
        });
      if(response.status===429)throw new Error('Google rate-limited this import. Wait a minute and try again.');
      if(!response.ok)throw new Error('Google export failed (HTTP '+response.status+'). Check that downloading this Sheet is allowed.');
      const limit=root.UPLOAD_LIMITS.workbookBytes;
      if(Number(response.headers.get('content-length'))>limit)throw new Error('Spreadsheet exports must be 8 MB or smaller.');
      // Enforce the limit while downloading, including chunked responses with no
      // Content-Length. Do not read an unbounded response into browser memory.
      const reader=response.body.getReader(), chunks=[];
      let size=0;
      try{for(;;){
        const {done,value}=await reader.read();if(done)break;
        size+=value.byteLength;
        if(size>limit){await reader.cancel();throw new Error('Spreadsheet exports must be 8 MB or smaller.');}
        chunks.push(value);
      }}finally{reader.releaseLock();}
      const sheets=await root.XLSX_READER.readWorkbook(new Blob(chunks,{type:MIME}));
      if(!sheets.length)throw new Error('This Sheet has no tab with a header and a data row.');
      return sheets.map(s=>({name:sheets.length>1?s.name:title||s.name,data:s.csv,
        import:{...s.import,source:'google-sheets',formulaValues:'google-export-snapshot'}}));
    }catch(error){
      if(error.name==='AbortError')throw new Error('Google Sheet import timed out. Try again or export a smaller workbook.');
      throw error;
    }finally{clearTimeout(timer);}
  }
  root.GOOGLE_SHEETS_IMPORT={read};
})(window);
