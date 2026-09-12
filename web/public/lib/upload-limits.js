// Shared by the upload reader and its disposable worker. Text expansion matches
// engine.request_validation; display limits and execution policy are independent.
(function(root){
  root.UPLOAD_LIMITS=Object.freeze({workbookBytes:8*1024*1024,textBytes:2*1024*1024,
    tableChars:2000000,totalChars:6000000,sheets:8,rows:10000,columns:256,timeoutMs:15000});
})(typeof window==='undefined'?self:window);
