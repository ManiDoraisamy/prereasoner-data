const {test,expect}=require('@playwright/test');
const XLSX=require('../../public/vendor/xlsx-0.20.3.full.min.js');

const firebaseApp='export function initializeApp(){return {}}';
const firebaseAuth=`
  const currentUser=null;
  export function getAuth(){return {currentUser,authStateReady:async()=>{}}}
  export class GoogleAuthProvider { addScope(){} }
  export async function signInWithRedirect(){}
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

test('sign in, upload, answer, inspect trace, follow up, and delete',async({page,request})=>{
  await mockAuth(page);

  await page.goto('/');
  await page.getByRole('button',{name:'Login'}).click();
  // Signing in replaces the badge with the conversation rail: the rail IS the signed-in
  // state, so a "Signed in" button would be redundant chrome.
  await expect(page.locator('#homerail')).toBeVisible();
  await expect(page.getByRole('button',{name:'New conversation'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Login'})).toBeHidden();

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
  await expect(page.locator('.sheetband .snm')).toHaveText('Result');
  await expect(page.locator('.sheetband .skind')).toHaveText('total');
  await expect(page.locator('.wb.result tbody')).toContainText('180');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for total sales');
  await expect(page.locator('.cotbar').last()).toContainText('Created');
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  const amountHeader=page.locator('th').filter({hasText:'amount'}).first();
  await expect(amountHeader).toContainText('EUR');
  await expect(amountHeader).toHaveAttribute('title',/Denominated in EUR.*This is in euros/);

  await page.reload();
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).toContainText('EUR');
  await expect.poll(async()=>((await request.get('/__state')).json()).then(v=>v.requestCount)).toBe(1);

  await page.locator('.wtab').filter({hasText:'calculated'}).click();
  await expect(page.locator('.provtag')).toHaveText(['SRC','ECB','CALC']);
  await expect(page.locator('th').filter({hasText:'rate_to_usd'})).toHaveAttribute('title',/European Central Bank.*ecb-2026-09-05/);
  // No `use` on the URL, so the deployment's auto policy runs Python for this small input
  // and the sheet's derivation badge names Python rather than SQL.
  await page.getByRole('button',{name:'View Python'}).click();
  await expect(page.locator('#sqlrow')).toHaveClass(/open/);
  await expect(page.locator('#sqlrow')).toContainText('for_each(');

  await page.locator('#chatq').fill('only Paris');
  await page.getByRole('button',{name:'Send'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('120');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for total sales');
  await expect(page.locator('.cotbar').last()).toContainText('Updated');
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).not.toContainText('EUR');
  await page.reload();
  await page.locator('.wtab').filter({hasText:'orders'}).click();
  await expect(page.locator('th').filter({hasText:'amount'}).first()).not.toContainText('EUR');

  await page.locator('#chatq').fill('top selling products');
  await page.getByRole('button',{name:'Send'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('Coat');
  await expect(page.locator('.cotbar').last()).toContainText('Reasoning steps for top selling products');
  await expect(page.locator('.cotbar').last()).toContainText('Created');

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

  await page.getByRole('button',{name:'Conversations'}).click();
  const deletion=page.waitForRequest(req=>req.url().endsWith('/api/conversation/delete')&&req.method()==='POST');
  await page.getByTitle('Delete conversation').click();
  await deletion;
  await expect(page).toHaveURL('http://127.0.0.1:4173/');
  await expect.poll(async()=>((await request.get('/__state')).json()).then(v=>v.deleted)).toBe(true);
});

function conversationPattern(){return 'c_[0-9a-f]{32}';}

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

  const badge=page.getByRole('button',{name:'View Python'});
  await expect(badge).toBeVisible();
  await expect(page.getByRole('button',{name:'View SQL'})).toHaveCount(0);
  await badge.click();
  await expect(page.locator('#sqlrow .vpy')).toContainText('calculated.reduce(');
  await expect(page.locator('#sqlrow .vpy')).toContainText('SUM(result.total, row.converted)');
});
}

test('verify mode offers a Python/SQL picker over the same proven stages',async({page})=>{
  await mockAuth(page,'0');
  await page.goto('/?load=orders-tiers&use=both');
  await page.locator('#q').fill('total amount');
  await page.getByRole('button',{name:'Ask'}).click();
  await expect(page.locator('.wb.result tbody')).toContainText('180');

  const picker=page.locator('select.srcsel');
  await expect(picker).toBeVisible();
  await expect(picker).toHaveValue('py');
  await expect(page.locator('#sqlrow .vpy')).toContainText('calculated.reduce(');

  const requestsBefore=(await (await page.request.get('/__state')).json()).requestCount;
  await picker.selectOption('sql');
  await expect(page.locator('#sqlrow .vsql')).toContainText('SUM');
  await expect(page.locator('#sqlrow .vpy')).toHaveCount(0);
  const requestsAfter=(await (await page.request.get('/__state')).json()).requestCount;
  expect(requestsAfter).toBe(requestsBefore);          // switching language must not re-execute
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
  await derivationTabs.nth(0).click();
  await expect(page.getByRole('button',{name:'View Python'})).toBeVisible();
  await expect(page.getByRole('button',{name:'View SQL'})).toHaveCount(0);
  await derivationTabs.nth(1).click();
  await expect(page.getByRole('button',{name:'View SQL'})).toBeVisible();
  await expect(page.getByRole('button',{name:'View Python'})).toHaveCount(0);
});

test('signed-in conversations remain reachable on mobile home',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await mockAuth(page);
  await page.goto('/');
  await page.getByRole('button',{name:'Login'}).click();
  const menu=page.getByRole('button',{name:'Conversations'});
  await expect(menu).toBeVisible();
  await expect(menu).toHaveAttribute('aria-expanded','false');
  await expect(page.locator('#homerail')).toHaveAttribute('aria-hidden','true');
  expect(await page.locator('#homerail').evaluate(element=>element.inert)).toBe(true);
  await menu.click();
  await expect(menu).toHaveAttribute('aria-expanded','true');
  await expect(page.locator('#homerail')).toHaveAttribute('aria-hidden','false');
  await expect(page.locator('body')).toHaveClass(/homeopen/);
  await expect(page.getByRole('button',{name:'New conversation'})).toBeVisible();
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
