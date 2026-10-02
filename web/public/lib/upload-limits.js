// Shared by the upload reader, its disposable worker and the Excel add-in's reader (office/excel/host.js);
// the Sheets add-on pins its copy to these in sheets-addon/tests. Text expansion matches
// engine.request_validation; display limits and execution policy are independent. The worker's
// parse timeout grew with the row limit (10,000 to 50,000 rows a tab, 2026-10-02).
(function(root){
  root.UPLOAD_LIMITS=Object.freeze({workbookBytes:16*1024*1024,textBytes:8*1024*1024,
    tableChars:8000000,totalChars:20000000,sheets:8,rows:50000,columns:256,cells:500000,timeoutMs:45000});
})(globalThis);
