// One boundary for HTTP, RTDB and saved tables. Never turn malformed data into
// a successful empty answer. RTDB envelopes preserve empty and all-null arrays.
(function(root){
  'use strict';
  const MAX_JSON_CHARS=16*1024*1024, MAX_CELLS=1000000;
  function decode(value,depth=0){
    if(depth>40)throw new Error('result nesting exceeds the limit');
    if(!value||typeof value!=='object')return value;
    if(Array.isArray(value))return value.map(v=>decode(v,depth+1));
    if(Object.prototype.hasOwnProperty.call(value,'__pr_wire__')){
      if(value.__pr_wire__!=='array/v1'||Object.keys(value).length!==2||
         typeof value.json!=='string'||value.json.length>MAX_JSON_CHARS)
        throw new Error('unsupported result encoding');
      const rows=JSON.parse(value.json);
      if(!Array.isArray(rows))throw new Error('invalid array encoding');
      return rows;
    }
    return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,decode(v,depth+1)]));
  }
  function row(value,width){
    if(value==null)return Array(width).fill(null);
    if(Array.isArray(value)){
      if(value.length>width)throw new Error('result row exceeds its columns');
      return Array.from({length:width},(_,i)=>value[i]===undefined?null:value[i]);
    }
    // Legacy RTDB cells have a known width, so gaps can be restored losslessly.
    if(typeof value!=='object')throw new Error('invalid result row');
    const out=Array(width).fill(null);
    for(const [k,v] of Object.entries(value)){
      if(!/^(0|[1-9]\d*)$/.test(k)||Number(k)>=width)throw new Error('invalid result cell index');
      out[Number(k)]=v;
    }
    return out;
  }
  function table(value){
    const data=decode(value);
    if(!data||typeof data!=='object'||Array.isArray(data))throw new Error('invalid result table');
    const columns=data.columns||data.cols||[];
    if(!Array.isArray(columns))throw new Error('invalid result columns');
    const rows=data.rows==null?[]:data.rows;
    // A legacy sparse OUTER object has no trustworthy trailing-row count.
    // Refetch authoritative HTTP data instead of guessing or discarding rows.
    if(!Array.isArray(rows))throw new Error('incomplete legacy result; reload the authoritative analysis');
    if(columns.length>10000||rows.length>100000||rows.length*Math.max(1,columns.length)>MAX_CELLS)
      throw new Error('result exceeds display limits');
    return {...data,rows:Array.from(rows,r=>row(r,columns.length))};
  }
  root.RESULT_WIRE={decode,table};
})(typeof window==='undefined'?globalThis:window);
