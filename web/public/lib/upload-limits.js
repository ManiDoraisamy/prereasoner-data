// Shared by the upload reader, its disposable worker and the Excel add-in's reader (office/excel/host.js);
// the Sheets add-on pins its copy to these in sheets-addon/tests. Text expansion matches
// engine.request_validation; display limits and execution policy are independent.
(function(root){
  root.UPLOAD_LIMITS=Object.freeze({workbookBytes:8*1024*1024,textBytes:2*1024*1024,
    tableChars:2000000,totalChars:6000000,sheets:8,rows:10000,columns:256,timeoutMs:15000});
})(globalThis);
