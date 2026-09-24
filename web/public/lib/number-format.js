// Excel number-format classification, shared by the Excel add-in (office/excel/host.js) and the
// workbook upload (lib/workbook-import.js), so one workbook reads the same through either door.
(function(root){
  'use strict';
  const ELAPSED=/\[(?:h+|m+|s+)\]/;
  // The date and time codes of a format. Quoted text, escaped characters, padding (`_x`, `*x`)
  // and bracket sections are not codes: a colour ([Red]), a currency or locale ([$USD], [$-409])
  // or a condition ([>=100]) would otherwise read as date letters, and `#,##0.00;[Red]-#,##0.00`
  // turned ordinary amounts into dates. Elapsed-time sections ([h], [mm], [ss]) are kept.
  function codes(format){
    return String(format||'').replace(/"[^"]*"|\\.|[_*]./g,'')
      .replace(/\[(?![hms]+\])[^\]]*\]/gi,'').toLowerCase();
  }
  // [h]:mm, [mm]:ss: accumulated time. Excel stores it as a day count like a date, but it is a
  // duration, so it stays that number instead of becoming a timestamp near the 1899 epoch.
  function isElapsed(format){return ELAPSED.test(codes(format));}
  function isDate(format){
    const c=codes(format);
    return !ELAPSED.test(c)&&/[ydhms]/.test(c)&&/[ymd]/.test(c);
  }
  root.NUMBER_FORMAT={codes,isElapsed,isDate};
})(typeof window==='undefined'?globalThis:window);
