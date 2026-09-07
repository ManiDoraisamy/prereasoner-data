'use strict';

const http=require('http');
const fs=require('fs');
const path=require('path');

const root=path.resolve(__dirname,'../../public');
const conversation='c_0123456789abcdef0123456789abcdef';
let deleted=false;
let requestCount=0;

const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8',
  '.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.csv':'text/csv; charset=utf-8',
  '.txt':'text/plain; charset=utf-8'};

function send(res,status,body,type='application/json'){
  const data=Buffer.from(typeof body==='string'?body:JSON.stringify(body));
  res.writeHead(status,{'content-type':type,'content-length':data.length});res.end(data);
}
function readJson(req){return new Promise((resolve,reject)=>{let raw='';req.setEncoding('utf8');
  req.on('data',chunk=>raw+=chunk);req.on('end',()=>{try{resolve(JSON.parse(raw||'{}'));}catch(error){reject(error);}});req.on('error',reject);});}
function answer(question){
  const follow=/paris/i.test(question);
  const value=follow?120:180;
  const input=(column)=>({kind:'input',source:'upload',table:'orders',column});
  const ecb=(column)=>({kind:'reference',source:'European Central Bank',table:'exchange_rate',column,release_id:'ecb-2026-09-05'});
  const calc=(column,operation,inputs)=>({kind:'derived',source:'Prereasoner',column,operation,inputs});
  return {question,conversation_id:conversation,
    // Exercise both sides of the client contract: a non-empty claim paints the source-column badge,
    // while the follow-up's empty effective list must clear it.
    dataset_semantics:follow?[]:[{table:'orders',column:'amount',currency:'EUR',
      basis:{source:'conversation',text:'This is in euros.',attested:true},supplied_by:'conversation'}],
    sql:'SELECT SUM(converted) AS total FROM calculated',
    views:[
      {name:'calculated',op:'convert',label:'calculated',sql:'SELECT amount, rate_to_usd, amount * rate_to_usd AS converted FROM orders',
        columns:['amount','rate_to_usd','converted'],rows:[[100,1.2,120],[50,1.2,60]],
        column_provenance:[input('amount'),ecb('rate_to_usd'),calc('converted','multiply',['orders.amount','exchange_rate.rate_to_usd'])]},
      {name:'total',op:'group_agg',label:'total',sql:'SELECT SUM(converted) AS total FROM calculated',
        columns:['total'],rows:[[value]],column_provenance:[calc('total','sum',['calculated.converted'])]},
    ],result:{columns:['total'],rows:[[value]],column_provenance:[calc('total','sum',['calculated.converted'])]}};
}

const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:4173');
  if(url.pathname==='/__health')return send(res,200,'ok','text/plain');
  if(url.pathname==='/__state')return send(res,200,{deleted,requestCount});
  if(url.pathname==='/config')return send(res,200,{authMode:'test'});
  if(req.method==='GET'&&url.pathname==='/api/reason')return send(res,200,{ok:true});
  if(req.method==='POST'&&url.pathname==='/api/reason'){
    const body=await readJson(req);requestCount+=1;
    if(req.headers.authorization!=='Bearer local-dev')return send(res,401,{error:'sign in required'});
    return send(res,200,answer(body.question||''));
  }
  if(req.method==='GET'&&url.pathname==='/api/conversations')return send(res,200,{conversations:deleted?[]:[
    {id:conversation,question:'total amount',ts:'2026-09-05T12:00:00Z'}]});
  if(req.method==='POST'&&url.pathname==='/api/conversation/state')return send(res,200,{saved:conversation});
  if(req.method==='POST'&&url.pathname==='/api/conversation/delete'){
    const body=await readJson(req);deleted=body.id===conversation;return send(res,200,{deleted:body.id});
  }

  let relative=decodeURIComponent(url.pathname).replace(/^\/+/, '');
  if(!relative)relative='index.html';
  if(relative==='reason'||relative.startsWith('reason/'))relative='reason.html';
  const target=path.resolve(root,relative);
  if(!target.startsWith(root+path.sep)||!fs.existsSync(target)||!fs.statSync(target).isFile())return send(res,404,'not found','text/plain');
  const data=fs.readFileSync(target);res.writeHead(200,{'content-type':types[path.extname(target)]||'application/octet-stream','content-length':data.length});res.end(data);
});

server.listen(4173,'127.0.0.1');
