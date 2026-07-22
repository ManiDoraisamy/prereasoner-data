// table-render.js — the shared "table in a bubble" renderer (the .bubble/.rtab markup used by
// reason.html and knowledge.html for inputs, resolution slides, streamed views and the result).
// CLASSIC script; requires lib/shared.js (esc) to be loaded first.

// tableBubble(cols, rows, label, opts)
//   cols   : array of column names
//   rows   : array of row arrays
//   label  : optional small-caps label above the table ('' / null = none)
//   opts   : { hlcol   : column NAME to highlight (resolution slides),
//              thExtra : fn(colName) -> extra HTML appended inside the <th>
//                        (knowledge.html uses it for the hover dimension-tag popup),
//              maxRows : row cap (default 14) }
// Numeric columns are right-aligned; non-integer numbers render to <=3 decimals.
function tableBubble(cols,rows,label,opts){
  opts=opts||{};
  const max=opts.maxRows||14;
  const nums=cols.map((_,i)=>rows.length>0 && rows.every(r=>r[i]===''||r[i]==null||!isNaN((''+r[i]).replace(/,/g,''))));
  const hl=i=>opts.hlcol&&cols[i]===opts.hlcol;
  const cell=v=>v==null?'':(typeof v==='number'&&!Number.isInteger(v)?(+v).toFixed(3).replace(/\.?0+$/,''):v);
  let h='<div class=bubble>'+(label?'<div class=lbl>'+esc(label)+'</div>':'')+'<div class=rtwrap><table class=rtab><tr>';
  h+=cols.map((c,i)=>'<th class="'+(nums[i]?'n ':'')+(hl(i)?'hl':'')+'">'+esc(c)+(opts.thExtra?(opts.thExtra(c)||''):'')+'</th>').join('')+'</tr>';
  h+=rows.slice(0,max).map(r=>'<tr>'+cols.map((c,i)=>'<td class="val'+(nums[i]?' n':'')+(hl(i)?' hl':'')+'">'+esc(cell(r[i]))+'</td>').join('')+'</tr>').join('');
  return h+'</table></div></div>';
}
