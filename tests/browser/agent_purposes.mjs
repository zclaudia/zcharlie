// Isolated real HTTP + SQLite + native FunctionModel/worker. No mocked API success.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm, mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const browser = await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
const headers = {'X-API-Key':'ag24-fixture-key','Content-Type':'application/json'};
const output = process.env.AG24_SCREENSHOTS || 'docs/product/probes/ag29-ui';
await mkdir(output,{recursive:true});
try {
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const temp = await mkdtemp(join(tmpdir(),'ag29-browser-')), base = 'http://127.0.0.1:19859';
    const server = spawn('.venv/bin/python',['tests/browser/portfolio_fixture_server.py'],{env:{...process.env,AG24_FIXTURE_DIR:temp,AG24_FIXTURE_PORT:'19859'},stdio:['ignore','pipe','pipe']});
    let logs = ''; server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
    const context = await browser.newContext({viewport}), page = await context.newPage().then(p=>(p.setDefaultTimeout(+(process.env.AG_TIMEOUT||30000)),p)), errors = [];
    page.on('pageerror',e=>errors.push(String(e)));
    async function api(path,body) {
      const r = await fetch(base+path,{method:body ? 'POST':'GET',headers,body:body ? JSON.stringify(body):undefined});
      assert.ok(r.ok,(await r.clone().text())+logs); return r.json();
    }
    const ui = page.locator('#agent-portfolio'), tab = name => page.locator(`[data-agent-tab="${name}"]`).click(), open = async name => {await tab('portfolio'); await ui.getByRole('button',{name,exact:true}).click();};
    const goChat = async () => {await tab('chat'); const list = page.locator('[data-agent-pane="list"]'); if (await list.isVisible()) await list.click();};
    const saved = () => ui.getByRole('status').filter({hasText:'服务器已确认保存'}).waitFor();
    const fillFacts = async time => {
      await ui.getByLabel('事实日期与时间').fill(time.slice(0,19));await ui.getByLabel('事实时间的时区').selectOption('+00:00');
      await ui.getByLabel('事实来源（对账单/成交回报等）').fill('用户确认的模拟回报 <script>alert(1)</script>');
      await ui.getByLabel('我确认这是已经发生的实际事实').check();
    };
    try {
      for(let i=0;i<100;i++) {if(server.exitCode !== null) throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{} await new Promise(r=>setTimeout(r,100));}
      await page.goto(base+'/app/agent?tab=portfolio'); await page.locator('[name=api_key]').fill('ag24-fixture-key');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/agent?tab=portfolio');
      await ui.getByText('尚无账户。先记录真实账户和起点；未知现金留空。').waitFor();
      assert.equal((await api('/fixture/state')).calls,0);
      await open('新建账户');await ui.getByLabel('账户名称').fill('我的明确账户 <img src=x onerror=alert(1)>');await open('确认新建账户');await saved();
      const account = (await api('/portfolio/accounts'))[0], state = await api('/fixture/state');
      await open('新建账户');await ui.getByLabel('账户名称').fill('第二个明确账户');await open('确认新建账户');await saved();const second = (await api('/portfolio/accounts')).find(x=>x.name === '第二个明确账户');assert.equal(await ui.getByLabel('组合账户').inputValue(),second.id);await ui.getByLabel('组合账户').selectOption(account.id);await ui.getByText('尚无起点，请录入确认的余额。').waitFor();
      await open('记录起点');await ui.getByLabel('资产范围（例如本账户全部A股和现金）').fill('本账户全部A股和现金');await ui.getByLabel('覆盖范围').selectOption('complete');await ui.getByLabel('现金金额（未知留空，确认零请输入0）').fill('1000');await open('添加确认持仓');await ui.getByLabel('查找证券名称或完整代码').fill('600001.SH');await open('查找证券');await ui.getByRole('button',{name:/组合证券.*600001.SH/}).click();await ui.getByLabel('确认持仓数量（可为0）').fill('100');await open('确认保存余额事实');await ui.getByRole('status').filter({hasText:'请明确确认实际事实'}).waitFor();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,0);await fillFacts(state.fact_time);await open('确认保存余额事实');await saved();
      let snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.known_cash,'1000');assert.equal(snap.holdings[0].confirmed_quantity,'100');
      await api(`/fixture/portfolio/${account.id}`,{});
      await open('资金用途规划');
      await ui.getByLabel('用途名称').fill('近期生活 <script>alert(1)</script>');
      await ui.getByLabel('分配金额（元）').fill('600');
      await ui.getByLabel('可用于方案的预算（含费用，元）').fill('100');
      await ui.getByLabel('必须保留现金（元）').fill('500');
      await ui.getByLabel('资金需求日期').fill('2026-11-01');
      await ui.getByLabel('来源版本').fill('用户核对第一版');
      await open('添加资金用途');
      await ui.getByLabel('用途名称').nth(1).fill('长期研究');await ui.getByLabel('分配金额（元）').nth(1).fill('400');await ui.getByLabel('可用于方案的预算（含费用，元）').nth(1).fill('400');await ui.getByLabel('必须保留现金（元）').nth(1).fill('0');await ui.getByLabel('期限').nth(1).selectOption('long_term');await ui.getByLabel('资金需求日期').nth(1).fill('2028-01-01');await ui.getByLabel('来源版本').nth(1).fill('用户核对第一版');
      await ui.locator(':scope > section:last-child').screenshot({path:join(output,`purposes-${viewport.width}-form.png`)});
      await page.route('**/portfolio/purposes',async route=>{await route.fetch();await route.abort('failed');});
      await open('确认保存用途规划');await ui.getByRole('status').filter({hasText:'网络请求失败'}).waitFor();
      const pending = await page.evaluate(()=>JSON.parse(sessionStorage.getItem('zcharlie.portfolio.pending.v1')));
      assert.equal(pending.request.purposes.length,2);assert.equal(await ui.getByLabel('组合账户').isDisabled(),true);
      await page.unroute('**/portfolio/purposes');await api('/fixture/writes/false',{});await page.reload();await open('重取原待确认请求');await saved();
      let pv = await api(`/portfolio/purposes/current?account_id=${account.id}`);assert.equal(pv.version.reference.id,pending.request.idempotency_key);assert.equal(pv.allocated,'1000');assert.equal(pv.required_cash,'500');
      assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,1);
      await api('/fixture/writes/true',{});await open('刷新账户与快照');await ui.getByText(/必须保留现金：500 元/).waitFor();
      await api(`/fixture/portfolio/${account.id}`,{});
      await goChat();await page.locator('#agent-new').click();await page.waitForFunction(()=>!document.querySelector('#agent-send').disabled);await open('绑定此快照到下一条消息');await page.locator('#agent-input').fill('Compare the same account with confirmed short term cash purposes');await page.locator('#agent-send').click();await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('Compare the same account with confirmed short term cash purposes'));await api('/fixture/run',{});await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('已完成'));
      await open('当前方案/复核');await ui.getByText('不行动对照',{exact:true}).waitFor();await ui.getByText(/方案后现金 1099 元；必留现金 500 元/).waitFor();
      const plan = await api(`/portfolio/plans/current?account_id=${account.id}`);assert.equal(plan.status,'published');
      const none = ui.getByRole('group',{name:'不行动对照',exact:true});await none.getByLabel('我的原话（未来愿望不会记入账本）').fill('接受不行动但没有真实交易');await none.getByRole('button',{name:'确认记录一个选项的决定'}).click();await saved();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,1);
      await open('当前方案/复核');await ui.getByText('不行动对照',{exact:true}).waitFor();await page.screenshot({path:join(output,`purposes-${viewport.width}-comparison.png`),fullPage:true});await ui.locator('fieldset').first().evaluate(n=>n.scrollIntoView({block:'start'}));await page.screenshot({path:join(output,`purposes-${viewport.width}-comparison-viewport.png`)});
      await open('资金用途规划');await ui.getByLabel('用途名称').first().fill('换个用途名称');await open('确认保存用途规划');await saved();assert.equal((await api(`/portfolio/plans/current?account_id=${account.id}`)).status,'published');
      await open('资金用途规划');await ui.getByLabel('可用于方案的预算（含费用，元）').first().fill('0');await ui.getByLabel('必须保留现金（元）').first().fill('600');await open('确认保存用途规划');await saved();assert.equal((await api(`/portfolio/plans/current?account_id=${account.id}`)).status,'review_required');
      await open('资金用途规划');await ui.getByLabel('用途名称').first().fill('并发原表单');
      pv = await api(`/portfolio/purposes/current?account_id=${account.id}`);await api('/portfolio/purposes',{...pending.request,idempotency_key:randomUUID(),expected_revision:pv.version.reference.revision,purposes:pv.version.purposes});await open('确认保存用途规划');await ui.getByRole('status').filter({hasText:'版本已变化'}).waitFor();assert.equal(await page.evaluate(()=>sessionStorage.getItem('zcharlie.portfolio.pending.v1')),null);
      await api('/portfolio/events',{account_id:account.id,expected_revision:snap.revision,idempotency_key:randomUUID(),fact:{kind:'cash_transfer',amount:'-500',occurred_at:new Date().toISOString(),source:'实际转出',user_confirmed:true}});await open('刷新账户与快照');await ui.getByText(/用途状态：实际现金已不足/).waitFor();assert.equal((await api(`/portfolio/purposes/current?account_id=${account.id}`)).status,'overallocated');
      await page.screenshot({path:join(output,`purposes-${viewport.width}-cash-drop.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await page.locator('#agent-portfolio img,#agent-portfolio script').count(),0);assert.deepEqual(errors,[]);
      console.log(`AG29 ${viewport.width}: purpose replay while off, native comparison, label/semantic change, conflict and actual cash drop passed`);
    } finally {await context.close();server.kill('SIGTERM');await new Promise(resolve=>server.exitCode !== null ? resolve() : server.once('exit',resolve));await rm(temp,{recursive:true,force:true});}
  }
} finally {await browser.close();}
