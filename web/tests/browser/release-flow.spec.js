const {test,expect}=require('@playwright/test');
const XLSX=require('../../public/vendor/xlsx-0.20.3.full.min.js');
const path=require('node:path');
const fs=require('node:fs');

for(const timezoneId of ['America/Los_Angeles','Pacific/Auckland'])test('spreadsheet dates retain their day in '+timezoneId,async({browser})=>{
  const context=await browser.newContext({timezoneId});const page=await context.newPage();
  try{
    await mockAuth(page);await page.goto('/');
    await page.locator('#file').setInputFiles(path.resolve(__dirname,'../../public/dataset/eval-formesign-assets-xls/assets.xls'));
    await expect(page.locator('#chips .nm')).toHaveText(['assets']);
    const data=await page.evaluate(()=>SHEETS[0].data);
    expect(data).toContain(',2012-02-06,');
    expect(data).toContain(',2016-04-09,');
    expect(data).not.toContain('T23:00');
  }finally{await context.close();}
});

for(const failure of [null,429,'layout'])test('Google Sheets uses the shared workbook importer: '+(failure||'success'),async({page})=>{
  await mockAuth(page);
  await page.addInitScript(()=>{
    sessionStorage.setItem('pr_return_to','/sheets?use=both');
    sessionStorage.setItem('pr_pending_q','What is the total amount paid to suppliers?');
  });
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-auth.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseAuth
    .replace('class GoogleAuthProvider {','class GoogleAuthProvider { static credentialFromResult(){return {accessToken:"picker-fixture-token"}}')
    .replace('getRedirectResult(){return null}', 'getRedirectResult(){return {}}')}));
  await page.route('https://apis.google.com/js/api.js*',route=>route.fulfill({contentType:'text/javascript',body:`
    window.gapi={load:(_name,cb)=>cb()};
    window.google={picker:{Action:{CANCELLED:'cancelled',PICKED:'picked'},ViewId:{SPREADSHEETS:1},
      View:class{},PickerBuilder:class{
        addView(){return this}setOAuthToken(){return this}setDeveloperKey(){return this}
        setAppId(){return this}setOrigin(){return this}setCallback(cb){this.cb=cb;return this}
        build(){return {setVisible:()=>this.cb({action:'picked',docs:[{id:'selected-sheet',name:'Supplier report'}]})}}
      }}};window.__gapiOnLoad();
  `}));
  let downloads=0;
  await page.route('https://www.googleapis.com/drive/v3/files/selected-sheet/export?*',async route=>{
    const headers={'access-control-allow-origin':'*','access-control-allow-headers':'authorization'};
    if(route.request().method()==='OPTIONS')return route.fulfill({status:204,headers});
    downloads++;
    expect(route.request().headers().authorization).toBe('Bearer picker-fixture-token');
    if(failure===429)return route.fulfill({status:429,headers});
    let body=fs.readFileSync(path.resolve(__dirname,'../../public/dataset/eval-neartail-supplier-report-xlsx/payments.xlsx'));
    if(failure==='layout'){
      const book=XLSX.utils.book_new();XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['amount'],[10],['Total']]),'Data');
      body=Buffer.from(XLSX.write(book,{type:'buffer',bookType:'xlsx'}));
    }
    return route.fulfill({headers,contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',body});
  });
  await page.goto('/picker?use=both');
  if(failure){
    await expect(page.locator('#msg')).toContainText(failure===429?'rate-limited':'double-counting');
    expect(await page.evaluate(()=>sessionStorage.getItem(SS.PENDING_SHEETS))).toBeNull();
  }else{
    await expect(page).toHaveURL(/\/sheets\?use=both$/);
    await expect(page.locator('#chips .nm')).toHaveText(['Supplier report']);
    await page.locator('#chips .chip').click();
    await expect(page.locator('#ph')).toContainText('30 rows');
    await expect(page.locator('#ph')).toContainText('header row 11 (Google Sheets snapshot)');
    expect(await page.evaluate(()=>SHEETS[0].data)).toBe(require('../../../tests/workbook_fixture.js').readWorkbook(
      path.resolve(__dirname,'../../public/dataset/eval-neartail-supplier-report-xlsx/payments.xlsx'))[0].csv);
  }
  expect(downloads).toBe(1);
  expect(await page.evaluate(()=>JSON.stringify({...sessionStorage}))).not.toContain('picker-fixture-token');
});

test('a rejected upload cannot submit the previous demo; a retry recovers',async({page})=>{
  await mockAuth(page);await page.goto('/');
  await expect(page.locator('#chips .nm')).toHaveText(['orders']);
  await page.locator('#file').setInputFiles({name:'too-big.csv',mimeType:'text/csv',buffer:Buffer.alloc(2*1024*1024+1)});
  await expect(page.locator('#err')).toContainText('Upload failed');
  await expect(page.getByRole('button',{name:'Ask',exact:true})).toBeDisabled();
  await page.locator('#q').press('Enter');
  await expect(page).toHaveURL(/\/$/);
  await page.locator('#file').setInputFiles({name:'replacement.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n1,7')});
  await expect(page.locator('#chips .nm')).toHaveText(['replacement']);
  await expect(page.getByRole('button',{name:'Ask',exact:true})).toBeEnabled();
});

for(const [file,names,count,header] of [
  ['eval-formesign-assets-xls/assets.xls',['assets'],3,4],
  ['eval-neartail-supplier-report-xlsx/payments.xlsx',['payments'],30,11],
  ['eval-formesign-procurement-xlsx/procurement.xlsx',['Contracts Register','Purchase orders over £5000'],1007,2],
])test('formatted original upload: '+file,async({page})=>{
  await mockAuth(page);await page.goto('/');
  await page.locator('#file').setInputFiles(path.resolve(__dirname,'../../public/dataset',file));
  await expect(page.locator('#chips .nm')).toHaveText(names);
  await page.locator('#chips .chip').first().click();
  await expect(page.locator('#ph')).toContainText(count+' rows');
  await expect(page.locator('#ph')).toContainText('header row '+header);
  await expect(page.locator('#err')).toBeEmpty();
});

test('10,000-row workbooks are accepted, but 10,001 rows are rejected atomically',async({page})=>{
  await mockAuth(page);await page.goto('/');
  const book=XLSX.utils.book_new();
  const rows=[['id','amount'],...Array.from({length:10000},(_,i)=>[i+1,1])];
  XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet(rows),'orders');
  const upload=()=>page.locator('#file').setInputFiles({name:'boundary.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer:Buffer.from(XLSX.write(book,{type:'buffer',bookType:'xlsx'}))});
  await upload();await expect(page.locator('#chips .nm')).toHaveText('boundary');
  rows.push([10001,1]);book.Sheets.orders=XLSX.utils.aoa_to_sheet(rows);
  await upload();await expect(page.locator('#err')).toContainText('10,000 data rows');
  await expect(page.locator('#chips .nm')).toHaveText('boundary');
  await expect(page.getByRole('button',{name:'Ask',exact:true})).toBeDisabled();
});

for(const malformed of [false,true])test('partial streams reconcile without duplicate stages (malformed='+malformed+')',async({page})=>{
  await mockAuth(page);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  const encode=json=>({__pr_wire__:'array/v1',json:JSON.stringify(json)});
  const first={name:'sparse_input',label:'input',op:'select',columns:['notice'],rows:[[null],[7],[null]],sql:'SELECT notice FROM contracts'};
  const final={name:'sparse_result',label:'result',op:'select',columns:['notice'],rows:[[null],[7],[null]],sql:'SELECT * FROM sparse_input',is_output:true};
  const streamed={...first,columns:encode(first.columns),rows:malformed?{1:[7]}:encode(first.rows)};
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-database.js',route=>route.fulfill({contentType:'text/javascript',body:`
    export function getDatabase(){return {}}
    export function ref(_db,path){return {path}}
    export function onValue(){return ()=>{}}
    export function onChildAdded(r,cb){
      const value=r.path.endsWith('/calls')?{jobId:'sparse-call',question:'show notice'}:
        r.path.endsWith('/views')?${JSON.stringify(streamed)}:null;
      const t=value&&setTimeout(()=>cb({key:'0',val:()=>value}),30);
      return ()=>clearTimeout(t);
    }
    export function off(){}
  `}));
  await page.route('**/chat',async route=>{
    await new Promise(resolve=>setTimeout(resolve,500));
    await route.fulfill({json:{reply:'Three notice rows.',conversation_id:'c_0123456789abcdef0123456789abcdef',history:[],
      traces:[{jobId:'sparse-call',engine:{status:'answered',views:[first,final],answer:{columns:final.columns,rows:final.rows},
        execution:{actual:'python',verified:false}}}]}});
  });
  await page.goto('/');await expect(page.locator('#chips .nm')).toHaveText(['orders']);
  await page.getByRole('button',{name:'Ask',exact:true}).click();
  await expect(page.locator('.convmsg').last()).toContainText('Three notice rows');
  const state=await page.evaluate(()=>({views:VIEWS.length,sheets:BOOK.filter(s=>s.cls==='deriv').map(s=>s.rows)}));
  expect(state).toEqual({views:2,sheets:[[[null],[7],[null]],[[null],[7],[null]]]});
  expect(errors).toEqual([]);
});

const firebaseApp='export function initializeApp(){return {}}';
const firebaseAuth=`
  const currentUser={displayName:'Test User',email:'test@example.com'};
  export function getAuth(){return {get currentUser(){return window.__uid?currentUser:null},authStateReady:async()=>{}}}
  export class GoogleAuthProvider { addScope(){} static credential(idToken,accessToken){return {idToken,accessToken}} }
  export class OAuthProvider { setCustomParameters(){} }
  export async function signOut(){}
  export async function signInWithRedirect(){}
  export async function signInAnonymously(){window.__uid='anonymous-test-user';return {user:currentUser}}
  export async function signInWithCredential(_auth,credential){window.__hostToken=credential.accessToken;window.__uid='sheets-user';return {user:{uid:'sheets-user'}}}
  export async function getRedirectResult(){return null}
  export async function getIdToken(){return 'browser-token'}
`;
const firebaseDatabase=`
  export function getDatabase(){return {}}
  export function ref(_db,path){return {path}}
  export function onValue(){return ()=>{}}
  export function onChildAdded(){return ()=>{}}
  export function off(){}
`;

async function mockAuth(page,chat='1'){
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-app.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseApp}));
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-auth.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseAuth}));
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-database.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseDatabase}));
  await page.addInitScript(chat=>{
    sessionStorage.setItem('pr_test_auth','1');
    localStorage.setItem('pr_chat',chat);
  },chat);
}

test('a workbook demo retains every worksheet and strips file extensions from the picker',async({page})=>{
  await mockAuth(page);
  const book=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['id'],[1]]),'orders');
  XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['customer'],['Ada']]),'customers');
  const body=XLSX.write(book,{type:'buffer',bookType:'xlsx'});
  await page.route('**/dataset/neartail-orders-xlsx/orders.xlsx',route=>route.fulfill({
    contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',body:Buffer.from(body),
  }));
  await page.goto('/?load=neartail-orders-xlsx');
  await expect(page.locator('#chips .nm')).toHaveText(['orders','customers']);
  await page.getByRole('button',{name:'More examples'}).click();
  await expect(page.locator('[data-load="neartail-orders-xlsx"] .xd')).toHaveText('orders');
});

test('a workbook demo with no data fails visibly instead of creating an empty table',async({page})=>{
  await mockAuth(page);
  const book=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(book,XLSX.utils.aoa_to_sheet([['id']]),'empty');
  const body=XLSX.write(book,{type:'buffer',bookType:'xlsx'});
  await page.route('**/dataset/neartail-orders-xlsx/orders.xlsx',route=>route.fulfill({
    contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',body:Buffer.from(body),
  }));
  await page.goto('/?load=neartail-orders-xlsx');
  await expect(page.locator('#err')).toContainText('no sheet has a header and a data row');
  await expect(page.locator('#chips .nm')).toHaveCount(0);
});

test('home menus and previews close cleanly with Escape and restore focus',async({page})=>{
  await mockAuth(page);
  await page.goto('/');
  await expect(page.locator('#chips .nm')).toHaveText('orders');
  const add=page.locator('#addbtn');
  await add.click();
  await expect(add).toHaveAttribute('aria-expanded','true');
  await page.keyboard.press('Escape');
  await expect(add).toHaveAttribute('aria-expanded','false');

  const chip=page.locator('.chip').first();
  await chip.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#preview')).toHaveAttribute('aria-hidden','false');
  await expect(page.locator('#preview .px')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#preview')).toHaveAttribute('aria-hidden','true');
  await expect(chip).toBeFocused();

  const examples=page.locator('#morex');
  await examples.click();
  await expect(page.locator('#examples')).toHaveAttribute('aria-hidden','false');
  await page.keyboard.press('Escape');
  await expect(page.locator('#examples')).toHaveAttribute('aria-hidden','true');
  await expect(examples).toBeFocused();
});

test('sign in, upload, answer, inspect trace, follow up, and delete',async({page,request})=>{
  await mockAuth(page);
  await page.addInitScript(()=>localStorage.setItem('pr_chat_nav_open','1'));

  await page.goto('/');
  await expect(page.locator('#chips .chip')).toHaveText('orders×');
  await expect(page.locator('#chips')).not.toContainText(/Example data|rows/);
  await page.getByRole('button',{name:'Login'}).click();
  // Signing in exposes history through a closed-by-default drawer. An obsolete persisted
  // preference must not shift the whole page or reopen it on arrival.
  await expect(page.locator('#homerail')).toBeVisible();
  await expect(page.getByRole('button',{name:'Login'})).toBeHidden();
  await expect(page.locator('body')).not.toHaveClass(/home-nav-open/);
  await expect(page.locator('.page')).toHaveCSS('margin-left','0px');
  await expect(page.locator('.brand')).toBeHidden();
  const homeMenu=page.getByRole('button',{name:'Conversations',exact:true});
  await expect(homeMenu).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#homerail')).toHaveAttribute('aria-hidden','true');
  expect(await page.locator('#homerail').evaluate(element=>element.inert)).toBe(true);
  await homeMenu.click();
  await expect(page.getByRole('button',{name:'New chat'})).toBeVisible();
  await expect(page.locator('#railuser')).toContainText('Test User');
  expect(await page.evaluate(()=>Math.abs(document.querySelector('.drawerhd').getBoundingClientRect().bottom-document.querySelector('.hdr').getBoundingClientRect().bottom))).toBeLessThanOrEqual(0.5);
  await page.getByRole('button',{name:'Close chats'}).click();
  await expect(page.locator('.page')).toHaveCSS('margin-left','0px');

  const workbook=XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook,XLSX.utils.aoa_to_sheet([
    ['order_id','city','amount','currency'],[1,'Paris',100,'EUR'],[2,'Lyon',50,'EUR'],
  ]),'orders');
  await page.locator('#file').setInputFiles({
    name:'orders.xlsx',mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer:Buffer.from(XLSX.write(workbook,{type:'buffer',bookType:'xlsx'})),
  });
  await expect(page.locator('.chip .nm')).toHaveCount(1);
  await expect(page.locator('.chip .nm').first()).toHaveText('orders');
  await page.locator('#q').fill('total amount in US dollars');
  await page.getByRole('button',{name:'Ask'}).click();

  await expect(page).toHaveURL(new RegExp(`/reason/${conversationPattern()}`));
  await expect(page.locator('body')).not.toHaveClass(/chat-nav-open/);
  const workbookMenu=page.getByRole('button',{name:'Conversations',exact:true});
  await expect(workbookMenu).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#drawer')).toHaveAttribute('aria-hidden','true');
  expect(await page.locator('#drawer').evaluate(element=>element.inert)).toBe(true);
  await expect(page.locator('.drawerlinks')).toHaveCount(0);
  await workbookMenu.click();
  await expect(page.locator('#draweruser')).toContainText('Test User');
  expect(await page.evaluate(()=>Math.abs(document.querySelector('.drawerhd').getBoundingClientRect().bottom-document.querySelector('.hdr').getBoundingClientRect().bottom))).toBeLessThanOrEqual(0.5);
  await page.getByRole('button',{name:'Close chats'}).click();
  await expect(page.locator('.sheetband .snm')).toHaveText('Result');
  await expect(page.locator('.sheetband .skind')).toHaveText('total');
  await expect(page.locator('.wb.result tbody')).toContainText('180');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for total sales');
  await expect(page.locator('.cotbar').last()).not.toContainText(/Created|Updated/);
  await expect(page.locator('.steplink').first()).not.toContainText(/c_[0-9a-f]{32}|Combined combined/i);
  // Older stored analyses may only expose their source through parsed SQL lineage.
  // That fallback must be cleaned just like the explicit `inputs` array.
  await page.evaluate(()=>{const step=BOOK.find(sheet=>sheet.desc);step.inputs=[];step.sql='SELECT * FROM "c_0123456789abcdef0123456789abcdef"';renderRail();});
  await expect(page.locator('.steplink').first()).toContainText('orders');
  await expect(page.locator('.steplink').first()).not.toContainText(/c_[0-9a-f]{32}/i);
  const aligned=await page.evaluate(()=>{const band=document.querySelector('.sheetband').getBoundingClientRect(),head=document.querySelector('.railhead').getBoundingClientRect(),tabs=document.querySelector('.tabsbar').getBoundingClientRect(),chat=document.querySelector('.chatbar').getBoundingClientRect();return {top:Math.abs(band.bottom-head.bottom),bottom:Math.abs(tabs.top-chat.top)};});
  expect(aligned.top).toBeLessThanOrEqual(0.5);
  expect(aligned.bottom).toBeLessThanOrEqual(0.5);
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  const amountHeader=page.locator('th').filter({hasText:'amount'}).first();
  await expect(amountHeader).toContainText('EUR');
  await expect(amountHeader).toHaveAttribute('title',/Denominated in EUR.*This is in euros/);

  await page.reload();
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).toContainText('EUR');
  await expect.poll(async()=>((await request.get('/__state')).json()).then(v=>v.requestCount)).toBe(1);

  await page.locator('.wtab').filter({hasText:'calculated'}).click();
  // The header carries the owning table as a chip and the provenance kind as a glyph, so the
  // <table>__<column> wire alias never reaches the user. aria-label keeps the kind readable.
  await expect(page.locator('.provemoji')).toHaveText(['\u{1F4C4}','\u{1F4B1}','\u{1F9EE}']);
  await expect(page.locator('.provemoji').first()).toHaveAttribute('aria-label',/uploaded data/);
  // The glyph leads the table inside one chip: "<currency>exchange_rate", not the reverse.
  await expect(page.locator('th').filter({hasText:'rate_to_usd'}).locator('.tabtag')).toHaveText('\u{1F4B1}exchange_rate');
  await expect(page.locator('th').filter({hasText:'rate_to_usd'})).not.toContainText('exchange_rate__rate_to_usd');
  await expect(page.locator('th').filter({hasText:'rate_to_usd'})).toHaveAttribute('title',/European Central Bank.*ecb-2026-09-05/);
  // No `use` on the URL, so the deployment's auto policy runs Python for this small input.
  // Both sources exist, so both are offered; only Python is marked as the one that ran.
  await expect(page.locator('.srcmain')).toHaveText('Python');
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).toContainText('· ran');
  await expect(page.locator('#popmenu button').filter({hasText:'SQL'})).not.toContainText('ran');
  await page.locator('#popmenu button').filter({hasText:'Python'}).click();
  await expect(page.locator('#sqlrow')).toContainText('for_each(');

  await page.locator('#chatq').fill('only Paris');
  await page.getByRole('button',{name:'Send'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('120');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for total sales');
  await expect(page.locator('.cotbar').last()).not.toContainText(/Created|Updated/);
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).not.toContainText('EUR');
  await page.reload();
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).not.toContainText('EUR');

  await page.locator('#chatq').fill('top selling products');
  await page.getByRole('button',{name:'Send'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('Coat');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for top selling products');
  await expect(page.locator('.cotbar').last()).not.toContainText(/Created|Updated/);

  const totalSalesLinks=page.locator('.analysislink',{hasText:'total sales'});
  await expect(totalSalesLinks).toHaveCount(2);
  await totalSalesLinks.first().click();
  await expect(page.locator('.wb.result tbody')).toContainText('180');
  await expect(page.locator('.wtab').filter({hasText:'orders'})).toHaveCount(1);
  await totalSalesLinks.nth(1).click();
  await expect(page.locator('.wb.result tbody')).toContainText('120');
  await page.reload();
  await expect(page.locator('.wb.result tbody')).toContainText('120');
  await expect(page.locator('.wtab').filter({hasText:'orders'})).toHaveCount(1);

  // A Sheets reasoning link carries one immutable analysis revision. Opening it must select that
  // workbook and turn, not whichever analysis happened to be active in this browser session. It
  // must also work in a fresh tab with no local render snapshot and must not re-run the question.
  await page.evaluate(()=>sessionStorage.removeItem('pr_conv_state'));
  const requestsBeforeLinkedOpen=(await (await request.get('/__state')).json()).requestCount;
  await page.goto('/reason/c_0123456789abcdef0123456789abcdef?analysis_id=a_11111111111111111111111111111111&revision=1');
  await expect(page.locator('.wb.result tbody')).toContainText('180');
  await expect(page.locator('.analysislink.on')).toHaveText('total sales');
  await expect(page.locator('.turn.user')).toHaveCount(1);
  await expect(page.locator('.turn.user')).toContainText('total amount');
  await expect(page.locator('.turn-answer')).toHaveText('Your total is 180.');
  await expect(page.locator('.turn-reasoning')).toHaveAttribute('open','');
  const linkedParts=page.locator('.turn.ai').locator(':scope > .turn-content').first().locator(':scope > *');
  await expect(linkedParts.nth(0)).toHaveClass(/turn-reasoning/);
  await expect(linkedParts.nth(1)).toHaveClass(/turn-answer/);
  expect((await (await request.get('/__state')).json()).requestCount).toBe(requestsBeforeLinkedOpen);

  const deleteChat=page.getByTitle('Delete chat');
  if(!await deleteChat.isVisible())await page.getByRole('button',{name:'Conversations',exact:true}).click();
  const deletion=page.waitForRequest(req=>req.url().endsWith('/api/conversation/delete')&&req.method()==='POST');
  await deleteChat.click();
  await deletion;
  await expect(page).toHaveURL('http://127.0.0.1:4173/');
  await expect.poll(async()=>((await request.get('/__state')).json()).then(v=>v.deleted)).toBe(true);
});

test('Google source freshness uses one quiet status control and recalculates in place',async({page})=>{
  await mockAuth(page,'0');
  await page.addInitScript(()=>{
    sessionStorage.setItem('pr_world_tables',JSON.stringify([
      {name:'orders',data:'order_id,city,amount\n1,Paris,120\n2,Lyon,60',source:{kind:'google-sheets'}},
    ]));
    sessionStorage.setItem('pr_world_q','total amount');
  });
  await page.goto('/reason?chat=0');
  await expect(page.locator('#inputcount')).toHaveText('1 sheet');
  await expect(page.locator('#syncstate')).toHaveClass(/current/);
  const turns=await page.locator('.turn.user').count();
  await page.evaluate(()=>{const info=sourceStatusInfo();info.stale=true;writeSourceInfo(info);renderSourceStatus();});
  await expect(page.locator('#syncstate')).toHaveClass(/stale/);
  await expect(page.locator('#syncstate')).toHaveAttribute('aria-label','Answer is stale. Recalculate');
  await expect(page.locator('.recalchint')).toHaveCount(0);
  await expect(page.locator('.analysisstale')).toHaveCount(0);
  await page.locator('#syncstate').click();
  await expect(page.locator('#syncstate')).toHaveClass(/current/);
  await expect(page.locator('.turn.user')).toHaveCount(turns);
});

function conversationPattern(){return 'c_[0-9a-f]{32}';}

// The split badge: the caret opens the language menu, an item pins the language and
// opens the panel. Clicking an item is also what dismisses the menu here, so the next
// action never fights a lingering fixed-position popup.
async function pickLang(page,label){
  await page.locator('.srccaret').click();
  await page.locator('#popmenu button').filter({hasText:label}).click();
}

// The derivation a sheet shows must name the backend that actually produced it. Python mode
// shows the generated loops; `both` (verify) ran BOTH and proved every stage equal, so it
// offers a picker that re-renders the pair already in hand — it never re-executes.
// Both transports, because they carry `execution` in DIFFERENT places: the direct engine
// reply has it at the top level, the orchestrated /chat reply nests it under
// traces[].engine. Testing only the direct path let a SQL-labelled Python answer ship.
for(const chat of ['0','1']){
test(`the sheet badge names the backend that produced it (${chat==='1'?'chat':'direct'})`,async({page})=>{
  await mockAuth(page,chat);
  await page.goto('/?load=orders-tiers&use=py');
  await page.locator('#q').fill('total amount');
  await page.getByRole('button',{name:'Ask'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('180');

  await expect(page.locator('.srcmain')).toHaveText('Python');
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).toContainText('· ran');
  await page.locator('#popmenu button').filter({hasText:'Python'}).click();
  await expect(page.locator('#sqlrow .vpy')).toContainText('calculated.reduce(');
  await expect(page.locator('#sqlrow .vpy')).toContainText('SUM(result.total, row.converted)');
  // Reading the unexecuted counterpart is free and must not re-run anything.
  const before=(await (await page.request.get('/__state')).json()).requestCount;
  await pickLang(page,'SQL');
  await expect(page.locator('.srcmain')).toHaveText('SQL');
  await expect(page.locator('#sqlrow .srcnote')).toContainText('was not run');
  await expect(page.locator('#sqlrow .vsql')).toContainText('SUM');
  expect((await (await page.request.get('/__state')).json()).requestCount).toBe(before);
});
}

test('verify mode offers a Python/SQL picker over the same proven stages',async({page})=>{
  await mockAuth(page,'0');
  await page.goto('/?load=orders-tiers&use=both');
  await page.locator('#q').fill('total amount');
  await page.getByRole('button',{name:'Ask'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('180');

  await expect(page.locator('.srcmain')).toBeVisible();
  await expect(page.locator('.srcmain')).toHaveText('Python');
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).toContainText('· ran');
  await expect(page.locator('#popmenu button').filter({hasText:'SQL'})).toContainText('· ran');
  await expect(page.locator('#popmenu button').filter({hasText:'Both'})).toContainText('· ran');
  await page.locator('#popmenu button').filter({hasText:'Python'}).click();
  await expect(page.locator('#sqlrow .srcnote')).toContainText('every stage matched');
  await expect(page.locator('#sqlrow .vpy')).toContainText('calculated.reduce(');

  const requestsBefore=(await (await page.request.get('/__state')).json()).requestCount;
  await pickLang(page,'SQL');
  await expect(page.locator('#sqlrow .vsql')).toContainText('SUM');
  await expect(page.locator('#sqlrow .vpy')).toHaveCount(0);
  // The main segment is a true toggle: it collapses the panel it opened.
  await expect(page.locator('#sqlrow')).toBeVisible();
  await page.locator('.srcmain').click();
  await expect(page.locator('#sqlrow')).not.toBeVisible();
  await page.locator('.srcmain').click();
  await expect(page.locator('#sqlrow')).toBeVisible();
  // 'Both' renders the stage-aligned pair at once.
  await pickLang(page,'Both');
  await expect(page.locator('.srcmain')).toHaveText('Both');
  await expect(page.locator('#sqlrow .vpy')).toHaveCount(1);
  await expect(page.locator('#sqlrow .vsql')).toHaveCount(1);
  const requestsAfter=(await (await page.request.get('/__state')).json()).requestCount;
  expect(requestsAfter).toBe(requestsBefore);          // switching language must not re-execute
});

test('a complex eval renders one nested dependency tree with stage-aligned Python and SQL',async({page})=>{
  await mockAuth(page,'1');
  await page.goto('/?load=complex-promotions&use=both');
  await expect(page.locator('.chip .nm')).toHaveCount(4);
  await page.locator('#q').fill('Find the top 3 products by units sold and top 2 customers by spend, then list the top-customer/product pairs those customers are not buying.');
  await page.getByRole('button',{name:'Ask'}).click();

  await expect(page.locator('.wb.result tbody')).toContainText('Cara');
  await expect(page.locator('.wb.result tbody')).toContainText('Beta');
  await page.locator('.cotbtn').last().click();
  const tree=page.locator('.reasontree');
  await expect(tree).toBeVisible();
  await expect(tree.locator('.branchhead b').first()).toBeInViewport();
  await expect(tree.locator('.branchhead b')).toHaveText([
    'Promotion gaps','Candidate pairs','Top selling products','Top buying customers','Existing purchases',
  ]);
  await expect(tree.locator('.steplink')).toHaveCount(10);
  await expect(tree.locator('.stepbackend')).toHaveCount(10);
  await expect(tree.locator('.stepbackend')).toHaveText(Array(10).fill('PY = SQL'));
  const root=tree.locator('.reasonnode').first();
  const rootChildren=root.locator(':scope > .reasonchildren > .reasonnode');
  await expect(rootChildren).toHaveCount(2);
  await expect(rootChildren.first().locator('.branchhead b').first()).toHaveText('Candidate pairs');
  await expect(rootChildren.nth(1).locator('.branchhead b').first()).toHaveText('Existing purchases');

  await root.locator(':scope > .branchsteps > .steplink').click();
  await expect(page.locator('#sqlrow')).toBeVisible();
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).toContainText('· ran');
  await expect(page.locator('#popmenu button').filter({hasText:'SQL'})).toContainText('· ran');
  await page.locator('#popmenu button').filter({hasText:'Python'}).click();
  await expect(page.locator('#sqlrow .srccontext')).toContainText('Promotion gaps');
  await expect(page.locator('#sqlrow .vpy')).toContainText('anti_join');
  await pickLang(page,'SQL');
  await expect(page.locator('#sqlrow .vsql')).toContainText('NOTEXISTS');
});

test('an orchestrated turn keeps backend provenance per engine call',async({page})=>{
  await mockAuth(page,'1');
  await page.route('**/chat',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    const mk=(name,value,actual)=>({jobId:'job-'+name,question:name,engine:{
      execution:{requested:'auto',actual,verified:false,implementation:'shared_plan',fallback_reason:null},
      sql:`SELECT ${value} AS value`,
      views:[{name,logical_name:name,op:'select',label:name,columns:['value'],rows:[[value]],
        sql:`SELECT ${value} AS value`,python:`${name} = source.for_each(...)`}],
      answer:{columns:['value'],rows:[[value]]},
    }});
    await route.fulfill({contentType:'application/json',body:JSON.stringify({
      reply:'Both calculations completed.',conversation_id:'c_0123456789abcdef0123456789abcdef',
      traces:[mk('python_stage',1,'python'),mk('sql_stage',2,'sql')],history:[],
    })});
  });
  await page.goto('/?load=orders-tiers');
  await page.locator('#q').fill('compare backends');
  await page.getByRole('button',{name:'Ask'}).click();
  const derivationTabs=page.locator('.wtab').filter({has:page.locator('.dot.deriv')});
  await expect(derivationTabs).toHaveCount(2);
  await derivationTabs.nth(1).click();
  await expect(page.locator('.srcmain')).toHaveText('SQL');      // unpinned: each sheet defaults to the backend that produced it
  await derivationTabs.nth(0).click();
  await expect(page.locator('.srcmain')).toHaveText('Python');
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).toContainText('· ran');
  await expect(page.locator('#popmenu button').filter({hasText:'SQL'})).not.toContainText('ran');
  await page.locator('#popmenu button').filter({hasText:'Python'}).click();
  await derivationTabs.nth(1).click();
  await page.locator('.srccaret').click();
  await expect(page.locator('#popmenu button').filter({hasText:'SQL'})).toContainText('· ran');   // provenance stays per engine call
  await expect(page.locator('#popmenu button').filter({hasText:'Python'})).not.toContainText('ran');
  await page.locator('#popmenu button').filter({hasText:'SQL'}).click();
});

test('signed-in conversations remain reachable on mobile home',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await mockAuth(page);
  await page.goto('/');
  await page.getByRole('button',{name:'Login'}).click();
  const menu=page.getByRole('button',{name:'Conversations',exact:true});
  await expect(menu).toBeVisible();
  await expect(menu).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#homerail')).toHaveAttribute('aria-hidden','true');
  expect(await page.locator('#homerail').evaluate(element=>element.inert)).toBe(true);
  await menu.click();
  await expect(menu).toHaveAttribute('aria-expanded','true');
  await expect(page.locator('#homerail')).toHaveAttribute('aria-hidden','false');
  await expect(page.locator('body')).toHaveClass(/homeopen/);
  await expect(page.getByRole('button',{name:'New chat'})).toBeVisible();
  await expect(page.locator('#homeback')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(menu).toHaveAttribute('aria-expanded','false');
});

for(const use of ['sql','py','both']){
  for(const chat of ['0','1']){
    test(`execution mode ${use} survives ${chat==='1'?'chat':'direct'} navigation and follow-up`,async({page})=>{
      await mockAuth(page,chat);
      const endpoint=chat==='1'?'/chat':'/api/reason';
      await page.goto('/?load=orders-tiers&use='+use);
      await page.locator('#q').fill('total amount');
      const first=page.waitForRequest(req=>new URL(req.url()).pathname===endpoint&&req.method()==='POST');
      await page.getByRole('button',{name:'Ask'}).click();
      expect((await first).postDataJSON().use).toBe(use);
      await expect(page).toHaveURL(new RegExp(`/reason/${conversationPattern()}\\?use=${use}$`));
      await expect(page.locator('.wb.result tbody')).toContainText('180');
      const followup=page.waitForRequest(req=>new URL(req.url()).pathname===endpoint&&req.method()==='POST');
      await page.locator('#chatq').fill('only Paris');
      await page.getByRole('button',{name:'Send'}).click();
      expect((await followup).postDataJSON().use).toBe(use);
      await expect(page.locator('.wb.result tbody')).toContainText('120');
    });
  }
}

// The Google Sheets add-in frames this web workbook at /embed/sheets (lib/host-bridge.js); the
// test host page stands in for sheets-addon/Sidebar.html and answers with window.__SHEETS_HOST's
// cells, as the add-in's Apps Script server would.
const sheetsConversation='c_0123456789abcdef0123456789abcdef';
async function mockSheetsHost(page,rows){
  await mockAuth(page);
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-auth.js',route=>route.fulfill({contentType:'text/javascript',
    body:firebaseAuth.replace("getIdToken(){return 'browser-token'}","getIdToken(){return 'local-dev'}")}));
  await page.addInitScript(rows=>{
    if(location.pathname!=='/__sheets-host')return;
    const grid=cells=>({name:'orders',rows:cells,formats:cells.map(row=>row.map(()=>'General')),
      errors:cells.map(row=>row.map(()=>false)),merges:[],date1904:false});
    window.__SHEETS_HOST={rows,grids(){return [grid(this.rows)];}};
  },rows);
}
const sheetRows=[['order_id','city','amount'],[1,'Paris',120],[2,'Lyon',60]];
const sheetState=async request=>(await request.get('/__sheets')).json();
const requestCount=async request=>(await (await request.get('/__state')).json()).requestCount;

test('the Sheets add-in shows the web workbook: first question, reopen, changed sheet, new chat',async({page,browser,request})=>{
  await request.get('/__sheets?reset=1');
  await mockSheetsHost(page,sheetRows);
  await page.goto('/__sheets-host');
  const frame=page.frameLocator('#prereasoner');
  await expect(frame.locator('.embedempty')).toContainText('Ask a question about this spreadsheet.');
  await expect(frame.locator('.embedempty')).toContainText('visible, non-empty tabs');
  await expect(frame.locator('.embednotice')).toContainText('not used to train generalized AI models');
  await expect(frame.locator('.embednotice a')).toHaveAttribute('href','/privacy');
  await expect(frame.locator('.embedprivacy')).toHaveAttribute('href','/privacy');
  await expect(frame.locator('.embedprivacy')).toBeVisible();
  await expect(frame.locator('#chatq')).toHaveAttribute('placeholder','Ask a question…');
  expect(await page.frame({url:/\/embed\/sheets$/}).evaluate(()=>window.__hostToken)).toBe('host-google-token');
  const [restore]=(await sheetState(request)).calls;
  expect(restore).toMatchObject({path:'/api/spreadsheet/conversation/restore',body:{spreadsheet_id:'sheet-1',host:'sheets'}});
  expect(restore.body.tables.map(table=>[table.name,table.data,table.source.kind]))
    .toEqual([['orders','order_id,city,amount\n1,Paris,120\n2,Lyon,60','google-sheets-addon']]);

  // The first question runs through the web workbook and binds its conversation to the spreadsheet.
  const before=await requestCount(request);
  await frame.locator('#chatq').fill('total amount');
  await frame.locator('#chatq').press('Enter');
  await expect(frame.locator('.turn-answer').last()).toHaveText('Your total is 180.');
  await expect(frame.locator('#syncstate')).toHaveClass(/current/);           // a settled answer is not still "checking"
  await expect(frame.locator('#chatq')).toHaveAttribute('placeholder','Ask a follow-up…');
  await expect.poll(async()=>(await sheetState(request)).bound).toBe(sheetsConversation);
  expect(await requestCount(request)).toBe(before+1);
  await expect.poll(async()=>(await sheetState(request)).saved).toBe(true);   // the snapshot reached the server

  // Reopening the sidebar restores the conversation without asking again: in the same browser tab
  // from its session, and in another browser from the server.
  await page.reload();
  await expect(frame.locator('.turn-answer').last()).toHaveText('Your total is 180.');
  const other=await browser.newContext();
  try{
    const second=await other.newPage();
    await mockSheetsHost(second,sheetRows);
    await second.goto('/__sheets-host');
    await expect(second.frameLocator('#prereasoner').locator('.turn-answer').last()).toHaveText('Your total is 180.');
  }finally{await other.close();}
  expect(await requestCount(request)).toBe(before+1);

  // A changed sheet is re-read before the next question: the conversation's source is synced and
  // the question waits for the frame to reload with the new cells.
  await page.evaluate(()=>window.__SHEETS_HOST.rows.push([3,'Paris',30]));
  await frame.locator('#chatq').fill('total amount in Paris');
  await frame.locator('#chatq').press('Enter');
  await expect(frame.locator('.turn-answer').last()).toHaveText('The Paris total is 120.');
  await expect(frame.locator('.turn.user')).toHaveText(['total amount','total amount in Paris']);
  const sync=(await sheetState(request)).calls.find(call=>call.path==='/api/conversation/sync');
  expect(sync.body).toMatchObject({id:sheetsConversation});
  expect(sync.body.tables[0].data).toBe('order_id,city,amount\n1,Paris,120\n2,Lyon,60\n3,Paris,30');
  expect(await page.frame({url:/\/embed\/sheets$/}).evaluate(()=>SHEETS[0].data)).toContain('3,Paris,30');
  expect(await requestCount(request)).toBe(before+2);

  // Previous conversations open in their own tab; New chat unbinds the spreadsheet.
  await frame.getByRole('button',{name:'Conversations',exact:true}).click();
  await frame.getByRole('button',{name:'New chat'}).click();
  await expect(frame.locator('.embedempty')).toContainText('Ask a question about this spreadsheet.');
  expect((await sheetState(request)).calls.some(call=>call.path==='/api/spreadsheet/conversation/clear')).toBe(true);
  expect((await sheetState(request)).bound).toBeNull();

  // The add-on's "Previous conversations" opens the same sidebar with its list open; a past
  // conversation opens in its own tab, and the sidebar stays on this spreadsheet.
  await page.goto('/__sheets-host?view=conversations');
  await expect(frame.locator('#drawer')).toHaveAttribute('aria-hidden','false');
  const [opened]=await Promise.all([page.context().waitForEvent('page'),frame.locator('.convitem').first().click()]);
  await expect(opened).toHaveURL(new RegExp('/reason/'+sheetsConversation));
  expect(page.frame({url:/\/embed\/sheets/}).url()).toMatch(/\/embed\/sheets\?view=conversations$/);
  await opened.close();
});

test('a sheet changed right after an answer keeps that answer on screen',async({page,request})=>{
  // The server copy of the snapshot is saved after a delay; until it lands, the tab's own copy is the
  // only record of the answer, and the rebuild for the changed sheet must keep it.
  await request.get('/__sheets?reset=1&dropState=1');
  await mockSheetsHost(page,sheetRows);
  await page.goto('/__sheets-host');
  const frame=page.frameLocator('#prereasoner');
  await frame.locator('#chatq').fill('total amount');
  await frame.locator('#chatq').press('Enter');
  await expect(frame.locator('.turn-answer').last()).toHaveText('Your total is 180.');
  await page.evaluate(()=>window.__SHEETS_HOST.rows.push([3,'Paris',30]));
  await frame.locator('#chatq').fill('total amount in Paris');
  await frame.locator('#chatq').press('Enter');
  await expect(frame.locator('.turn-answer').last()).toHaveText('The Paris total is 120.');
  await expect(frame.locator('.turn.user')).toHaveText(['total amount','total amount in Paris']);
  expect((await sheetState(request)).saved).toBe(false);
});

test('the Sheets add-in shows the upload importer message for a sheet it cannot read, and asking again re-reads it',async({page,request})=>{
  await request.get('/__sheets?reset=1');
  // Column C has values but no header: the one import rule refuses it instead of naming it.
  await mockSheetsHost(page,[['order_id','amount'],[1,'Paris',120],[2,'Lyon',60]]);
  const before=await requestCount(request);
  await page.goto('/__sheets-host');
  const frame=page.frameLocator('#prereasoner');
  await expect(frame.locator('.embedempty.failed')).toContainText('Sheet "orders": No unambiguous header found');
  await expect(frame.locator('.embedempty.failed')).toContainText('Fix the spreadsheet, then ask again.');
  expect((await sheetState(request)).calls).toEqual([]);
  await page.evaluate(()=>{window.__SHEETS_HOST.rows[0]=['order_id','city','amount'];});
  await frame.locator('#chatq').fill('total amount');
  await expect(frame.locator('#chatsend')).toBeEnabled();
  await frame.locator('#chatq').press('Enter');
  await expect(frame.locator('.turn-answer').last()).toHaveText('Your total is 180.');
  await expect(frame.locator('.embedempty')).toHaveCount(0);
  await expect(frame.locator('.turn.user')).toHaveText(['total amount']);
  expect(await requestCount(request)).toBe(before+1);                        // only the user's question ran
});
