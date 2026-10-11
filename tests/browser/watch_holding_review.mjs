// Real HTTP/SQLite regression checks; isolated fixture, no live model calls.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const browser = await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
const temp = await mkdtemp(join(tmpdir(),'watch-holding-review-'));
const base = 'http://127.0.0.1:19863';
const server = spawn('.venv/bin/python',['tests/browser/portfolio_fixture_server.py'],{env:{...process.env,AG24_FIXTURE_DIR:temp,AG24_FIXTURE_PORT:'19863'},stdio:['ignore','pipe','pipe']});
let logs = ''; server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
const headers = {'X-API-Key':'ag24-fixture-key','Content-Type':'application/json'};
async function api(path,body) {
  const response = await fetch(base+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});
  assert.ok(response.ok,await response.clone().text());return response.json();
}
try {
  for(let i=0;i<100;i++) {if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const time = (await api('/fixture/state')).fact_time;
  const listing = (await api('/portfolio/listing?ticker=600002.SH&name=Second')).listing_ref;
  const create = name => api('/portfolio/accounts',{name,idempotency_key:randomUUID()});
  const account = await create('Two holdings'), second = await create('Cash only');
  const balance = {kind:'opening_balance',occurred_at:time,source:'fixture statement',user_confirmed:true,coverage:'complete',asset_scope:'all assets of this account',cash:'1000',prior_history:'included_in_confirmed_balance',holdings:[{listing_ref:{kind:'listing',id:77,revision:1},quantity:'100',average_cost:'10'},{listing_ref:listing,quantity:'200',average_cost:'20'}]};
  const append = (id,revision,fact) => api('/portfolio/events',{account_id:id,expected_revision:revision,idempotency_key:randomUUID(),fact});
  await append(account.id,0,balance);await append(second.id,0,{...balance,cash:'2222',holdings:[]});
  await api('/fixture/valuate',{});  // reads never compute: the day's valuation must be stored first
  for(const width of [1440,390]) {
    const context = await browser.newContext({viewport:{width,height:1000}}), page = await context.newPage(), errors = [];
    page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(base+'/app/portfolio');await page.locator('[name=api_key]').fill('ag24-fixture-key');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/portfolio');
    const select = page.locator('#portfolio-account'), form = page.locator('#portfolio-fact-form');
    await select.selectOption(account.id);await page.waitForFunction(()=>document.querySelector('#portfolio-holdings tbody')?.textContent.includes('600002.SH'));
    const fill = async () => {await form.locator('[name=occurred_at]').fill(time.slice(0,19));await form.locator('[name=timezone]').selectOption('+00:00');await form.locator('[name=source]').fill('review statement');await form.locator('[name=user_confirmed]').check();};
    const submit = () => form.getByRole('button',{name:'提交',exact:true}).click();
    const saved = async () => {try {await page.locator('#portfolio-fact-result').getByText(/服务器已确认保存/).waitFor({timeout:10000});}catch(error){console.log(await page.locator('#portfolio-fact-result').innerText());console.log(await form.evaluate(f=>[...f.elements].map(n=>({name:n.name,value:n.value,disabled:n.disabled,valid:n.validity?.valid,checked:n.checked}))));throw error;}};
    await form.locator('[name=kind]').selectOption('reconciliation');await page.getByRole('button',{name:'载入当前全部持仓',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-balance-row]').length===2);
    assert.deepEqual(await form.locator('[name=balance_quantity]').evaluateAll(nodes=>nodes.map(n=>n.value)),['100','200']);
    await form.locator('[name=unexplained_difference]').fill('none');await form.locator('[name=balance_confirmed]').check();await fill();await submit();await saved();
    let snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.holdings.length,2);assert.equal(snap.holdings[0].remaining_cost,'1000');assert.equal(snap.holdings[1].remaining_cost,'4000');
    await form.locator('[name=kind]').selectOption('cash_transfer');await form.locator('[name=amount]').fill('100.000000000000000001');await fill();
    await page.route('**/portfolio/events',async route=>{if(route.request().method()==='POST'){await route.fetch();await route.abort('failed');}else await route.continue();});
    await submit();await page.locator('#portfolio-fact-result').getByText(/原请求已保留/).waitFor();
    const pending = await page.evaluate(()=>JSON.parse(sessionStorage.getItem('zcharlie.portfolio.pending.v1')));
    assert.equal(pending.request.fact.amount,'100.000000000000000001');assert.equal(pending.request.fact.occurred_at,time.slice(0,19)+'+00:00');
    await api('/fixture/valuate',{});
  const revision = (await api(`/portfolio/snapshot?account_id=${account.id}`)).revision;
    await page.unroute('**/portfolio/events');await page.reload();await page.getByRole('button',{name:'重取待确认请求',exact:true}).click();await saved();
    snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.revision,revision);
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('zcharlie.portfolio.pending.v1')),null);
    // An old account's late valuation response cannot replace the selected account.
    let release, received, finished;const started = new Promise(r=>received=r), hold = new Promise(r=>release=r), done = new Promise(r=>finished=r);
    await page.route('**/portfolio/valuations/current?**',async route=>{if(new URL(route.request().url()).searchParams.get('account_id')===account.id){received();await hold;await route.continue();finished();}else await route.continue();});
    await page.getByRole('button',{name:'刷新估值',exact:true}).click();await started;await select.selectOption(second.id);
    await page.waitForFunction(()=>document.querySelector('#portfolio-valuation-meta').textContent.replaceAll(',','').includes('2222'));release();await done;await page.unroute('**/portfolio/valuations/current?**');
    await page.waitForTimeout(200);assert.match(await page.locator('#portfolio-valuation-meta').innerText(),/2,?222/);
    // No storage receipt means no fact POST is admitted to the network.
    await page.evaluate(()=>{const original=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key==='zcharlie.portfolio.pending.v1')throw Error('fixture storage unavailable');return original.call(this,key,value);};});
    await form.locator('[name=kind]').selectOption('cash_transfer');await form.locator('[name=amount]').fill('1');await fill();await submit();await page.locator('#portfolio-fact-result').getByText(/未发送事实/).waitFor();
    assert.equal((await api(`/portfolio/snapshot?account_id=${second.id}`)).revision,1);
    assert.equal(await page.locator('#portfolio-holdings img,#portfolio-holdings script').count(),0);
    // Holding rule review (WP8): the row's 规则 cell opens the panel, a draft
    // is requested with a template, approved, and then shows as the active
    // version - all without a model call (the fixture stubs the draft client).
    await select.selectOption(account.id);
    await page.waitForFunction(()=>document.querySelector('#portfolio-holdings tbody')?.textContent.includes('600002.SH'));
    const ruleCell = page.locator(`[data-rule-listing="77"]`);
    await ruleCell.getByRole('button',{name:'审阅',exact:true}).click();
    const panel = page.locator('tr.rule-detail-row');
    await panel.getByRole('heading',{name:'持仓规则（add / trim / exit_review）'}).waitFor();
    if (await panel.locator('.rule-chooser select').count() === 0) {
      // A later viewport reuses the fixture database: the rule approved by the
      // first pass must still be visible and reviewable.
      assert.match(await ruleCell.innerText(),/规则已生效 v1/);
      await panel.getByRole('heading',{name:/当前生效规则 v1/}).waitFor();
      assert.ok(errors.length===0, errors);
      await context.close();
      console.log(`watch/holding ${width}: multirow, exact decimals, lost receipt, account race, storage failure, holding rule visible passed`);
      continue;
    }
    const chooser = panel.locator('.rule-chooser select');
    await chooser.waitFor();
    await page.waitForFunction(()=>document.querySelector('.rule-chooser select')?.options.length>1);
    const templateId = await chooser.evaluate(node=>[...node.options].map(o=>o.value).find(value=>value));
    assert.ok(templateId,'fixture must expose at least one rule template');
    await chooser.selectOption(templateId);
    await panel.getByRole('button',{name:'起草',exact:true}).click();
    // The draft POST settled and the row re-read the slot (the table is rebuilt).
    await page.waitForFunction(()=>document.querySelector('[data-rule-listing="77"]').textContent.includes('起草中'));
    await api('/fixture/run',{});  // drain the queued rule_draft run (stub client)
    await ruleCell.getByRole('button',{name:'审阅',exact:true}).click();  // re-reads the current slot
    await page.locator('tr.rule-detail-row').getByRole('heading',{name:/规则草稿 v1/}).waitFor();
    assert.match(await ruleCell.innerText(),/规则待批准/);
    await page.locator('tr.rule-detail-row').getByRole('button',{name:'批准该版本',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('[data-rule-listing="77"]').textContent.includes('规则已生效 v1'));
    assert.equal(await page.locator('tr.rule-detail-row').count(),0,'the panel closes after approving');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.deepEqual(errors,[]);await context.close();console.log(`watch/holding ${width}: multirow, exact decimals, lost receipt, account race, storage failure, holding rule review passed`);
  }
  // The watchlist page renders the same zone block (WP8): an entry with a
  // draft in flight must show its rule state without script errors.
  const added = await api('/watchlist',{ticker:'600001.SH',name:'Fixture',industry:'Fixture',template_id:'quality_growth'});
  assert.equal(added.rule.state,'queued');
  const wlContext = await browser.newContext({viewport:{width:1440,height:1000}}), wlPage = await wlContext.newPage(), wlErrors = [];
  wlPage.on('pageerror',e=>wlErrors.push(String(e)));
  await wlPage.goto(base+'/app/watchlist');
  await wlPage.locator('[name=api_key]').fill('ag24-fixture-key');
  await wlPage.getByRole('button',{name:'登录',exact:true}).click();
  await wlPage.goto(base+'/app/watchlist');
  await wlPage.waitForFunction(()=>document.querySelector('#watchlist-list tbody')?.textContent.includes('600001.SH'));
  await wlPage.waitForFunction(()=>document.querySelector('.rule-cell')?.textContent.includes('起草中'));
  await api('/fixture/run',{});  // the fixture's stub draft client stores the draft
  await wlPage.reload();
  await wlPage.waitForFunction(()=>document.querySelector('.rule-cell')?.textContent.includes('规则待批准'));
  await wlPage.locator('.rule-cell').getByRole('button',{name:'审阅',exact:true}).click();
  await wlPage.locator('tr.rule-detail-row').getByRole('heading',{name:/规则草稿 v1/}).waitFor();
  assert.deepEqual(wlErrors,[]);
  await wlContext.close();
  console.log('watchlist: rule cell + review panel render without script errors passed');
  assert.equal((await api('/fixture/state')).calls,0);
} finally {
  await browser.close();server.kill('SIGTERM');await new Promise(resolve=>server.exitCode!==null?resolve():server.once('exit',resolve));await rm(temp,{recursive:true,force:true});
}
