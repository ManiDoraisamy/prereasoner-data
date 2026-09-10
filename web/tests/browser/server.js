'use strict';

const http=require('http');
const fs=require('fs');
const path=require('path');

const root=path.resolve(__dirname,'../../public');
const conversation='c_0123456789abcdef0123456789abcdef';
let deleted=false;
let requestCount=0;
const totalSalesId='a_11111111111111111111111111111111';
const topProductsId='a_22222222222222222222222222222222';
const revisions=new Map();

const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8',
  '.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.csv':'text/csv; charset=utf-8',
  '.txt':'text/plain; charset=utf-8'};

function send(res,status,body,type='application/json'){
  const data=Buffer.from(typeof body==='string'?body:JSON.stringify(body));
  res.writeHead(status,{'content-type':type,'content-length':data.length});res.end(data);
}
function readJson(req){return new Promise((resolve,reject)=>{let raw='';req.setEncoding('utf8');
  req.on('data',chunk=>raw+=chunk);req.on('end',()=>{try{resolve(JSON.parse(raw||'{}'));}catch(error){reject(error);}});req.on('error',reject);});}
function answer(question,analysis){
  const follow=/paris/i.test(question);
  const top=/top selling products/i.test(question);
  const value=follow?120:180;
  const input=(column)=>({kind:'input',source:'upload',table:'orders',column});
  const ecb=(column)=>({kind:'reference',source:'European Central Bank',table:'exchange_rate',column,release_id:'ecb-2026-09-05'});
  const calc=(column,operation,inputs)=>({kind:'derived',source:'Prereasoner',column,operation,inputs});
  if(top)return {question,conversation_id:conversation,analysis,
    sql:'SELECT product, SUM(amount) AS total FROM orders GROUP BY product ORDER BY total DESC',
    views:[{name:analysis.slug+'_top_results',logical_name:'top_results',op:'topn',label:'top results',
      sql:'SELECT product, SUM(amount) AS total FROM orders GROUP BY product ORDER BY total DESC',
      columns:['product','total'],rows:[['Coat',100]],column_provenance:[input('product'),calc('total','sum',['orders.amount'])]}],
    result:{columns:['product','total'],rows:[['Coat',100]],column_provenance:[input('product'),calc('total','sum',['orders.amount'])]}};
  return {question,conversation_id:conversation,analysis,
    // Exercise both sides of the client contract: a non-empty claim paints the source-column badge,
    // while the follow-up's empty effective list must clear it.
    dataset_semantics:follow?[]:[{table:'orders',column:'amount',currency:'EUR',
      basis:{source:'conversation',text:'This is in euros.',attested:true},supplied_by:'conversation'}],
    sql:'SELECT SUM(converted) AS total FROM calculated',
    views:[
      {name:'calculated',op:'convert',label:'calculated',sql:'SELECT amount, rate_to_usd, amount * rate_to_usd AS converted FROM orders',
        python:`        # View: calculated
        calculated = combined.for_each(
            name='calculated',
            emit=lambda row: Calculated(
                converted=MULTIPLY(row.orders.amount, row.exchange_rate.rate_to_usd),
            ),
        )`,
        columns:['amount','rate_to_usd','converted'],rows:[[100,1.2,120],[50,1.2,60]],
        column_provenance:[input('amount'),ecb('rate_to_usd'),calc('converted','multiply',['orders.amount','exchange_rate.rate_to_usd'])]},
      {name:'total',op:'group_agg',label:'total',sql:'SELECT SUM(converted) AS total FROM calculated',
        python:`        # View: total
        total = calculated.reduce(
            name='total', initial=Total(total=None),
            step=lambda result, row: Total(total=SUM(result.total, row.converted)),
        )`,
        columns:['total'],rows:[[value]],column_provenance:[calc('total','sum',['calculated.converted'])]},
    ],result:{columns:['total'],rows:[[value]],column_provenance:[calc('total','sum',['calculated.converted'])]}};
}
function analysisFor(question){
  if(/top selling products/i.test(question))return {analysis_id:topProductsId,slug:'top_selling_products',revision:1,action:'create',stale:false,display_name:'top selling products'};
  if(/paris/i.test(question))return {analysis_id:totalSalesId,slug:'total_sales',revision:2,action:'modify',stale:false,display_name:'total sales'};
  return {analysis_id:totalSalesId,slug:'total_sales',revision:1,action:'create',stale:false,display_name:'total sales'};
}
// Mirrors enforce_execution_response: `use` selects the backend, and omitted `use` lets the
// auto policy pick Python for a small input.
function executionFor(use){
  const actual=use==='sql'?'sql':(use==='both'||use==='verify')?'verify':'python';
  return {requested:use||'default',actual,verified:actual==='verify',implementation:'shared_plan',fallback_reason:null};
}
function shaped(raw){return {status:'answered',model:'test',answer:raw.result,sql:raw.sql,views:raw.views,
  execution:raw.execution,
  analysis:raw.analysis,dataset_semantics:raw.dataset_semantics,conversation_id:conversation,trace:{jobId:'test'}};}

const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1:4173');
  if(url.pathname==='/__health')return send(res,200,'ok','text/plain');
  if(url.pathname==='/__state')return send(res,200,{deleted,requestCount});
  if(url.pathname==='/config')return send(res,200,{authMode:'test'});
  if(req.method==='GET'&&url.pathname==='/api/reason')return send(res,200,{ok:true});
  if(req.method==='POST'&&url.pathname==='/api/reason'){
    const body=await readJson(req);requestCount+=1;
    if(req.headers.authorization!=='Bearer local-dev')return send(res,401,{error:'sign in required'});
    const analysis=analysisFor(body.question||''); const raw=answer(body.question||'',analysis);
    raw.execution=executionFor(body.use);
    revisions.set(analysis.analysis_id+':'+analysis.revision,raw);
    return send(res,200,raw);
  }
  if(req.method==='POST'&&url.pathname==='/chat'){
    const body=await readJson(req);requestCount+=1;
    if(req.headers.authorization!=='Bearer local-dev')return send(res,401,{error:'sign in required'});
    const analysis=analysisFor(body.message||''); const raw=answer(body.message||'',analysis);
    raw.execution=executionFor(body.use);
    revisions.set(analysis.analysis_id+':'+analysis.revision,raw);
    return send(res,200,{reply:/top selling/i.test(body.message||'')?'Coat is the top-selling product.':
      (/paris/i.test(body.message||'')?'The Paris total is 120.':'Your total is 180.'),
      traces:[{jobId:'test',question:body.message,engine:shaped(raw)}],
      history:(body.history||[]).concat([{role:'user',content:body.message},{role:'assistant',content:'Answered.'}]),
      conversation_id:conversation});
  }
  if(req.method==='GET'&&url.pathname==='/api/analysis'){
    const key=url.searchParams.get('analysis_id')+':'+url.searchParams.get('revision');
    const raw=revisions.get(key);
    return raw?send(res,200,{analysis:raw.analysis,question:raw.question,response:raw}):send(res,404,{error:'analysis not found'});
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
