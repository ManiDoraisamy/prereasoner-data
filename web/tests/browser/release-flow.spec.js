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

test('sign in, upload, answer, inspect trace, follow up, and delete',async({page,request})=>{
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-app.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseApp}));
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-auth.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseAuth}));
  await page.route('https://www.gstatic.com/firebasejs/**/firebase-database.js',route=>route.fulfill({contentType:'text/javascript',body:firebaseDatabase}));
  await page.addInitScript(()=>{
    sessionStorage.setItem('pr_test_auth','1');
    localStorage.setItem('pr_chat','1');
  });

  await page.goto('/');
  await page.getByRole('button',{name:'Login'}).click();
  await expect(page.getByRole('button',{name:'Signed in'})).toBeDisabled();

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
  await page.getByRole('button',{name:'View SQL'}).click();
  await expect(page.locator('#sqlrow')).toHaveClass(/open/);
  await expect(page.locator('#sqlrow')).toContainText('SELECT');

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
