/* global XLSX, importScripts, UPLOAD_LIMITS, WORKBOOK_IMPORT */
'use strict';

// SheetJS CE 0.20.3 is vendored at a fixed hash. Keep parsing off the UI thread; the conversion and
// its limits are WORKBOOK_IMPORT.convert (lib/workbook-import.js). A file upload sends {buffer}; a host
// reading live cells (the Excel task pane) sends {grids}.
importScripts('/vendor/xlsx-0.20.3.full.min.js');
importScripts('/lib/upload-limits.js');
importScripts('/lib/number-format.js');
importScripts('/lib/workbook-import.js');

self.onmessage=function(event){
  self.postMessage(WORKBOOK_IMPORT.convert(event.data,XLSX,UPLOAD_LIMITS));
};
