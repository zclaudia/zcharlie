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
const output = process.env.AG24_SCREENSHOTS || '/tmp/ag24-screenshots';
await mkdir(output,{recursive:true});
try {
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const temp = await mkdtemp(join(tmpdir(),'ag24-browser-')), base = 'http://127.0.0.1:19853';
    const server = spawn('.venv/bin/python',['tests/browser/portfolio_fixture_server.py'],{env:{...process.env,AG24_FIXTURE_DIR:temp,AG24_FIXTURE_PORT:'19853'},stdio:['ignore','pipe','pipe']});
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
      // Account numeric limits preserve all other fields, including authorization/free text.
      const before = await api('/investor/mandate');await open('账户数值约束');await ui.getByLabel('最低现金 CNY').fill('0');await ui.getByLabel('我确认这些是本账户的正式硬约束').check();await open('确认保存约束修订');await saved();
      const after = await api('/investor/mandate');assert.deepEqual({...after.authorization,source_refs:before.authorization.source_refs},before.authorization);assert.ok(after.authorization.source_refs.length>=before.authorization.source_refs.length);assert.deepEqual(after.hard_constraints,before.hard_constraints);assert.equal(after.portfolio_constraints.minimum_cash,'0');
      // Rebuild fixture candidate against the latest confirmed mandate without replacing it.
      await api(`/fixture/portfolio/${account.id}`,{});
      await goChat();await page.locator('#agent-new').click();await page.waitForFunction(()=>!document.querySelector('#agent-send').disabled);
      await open('绑定此快照到下一条消息');assert.equal(await page.locator('#agent-kind').inputValue(),'portfolio_action');await page.locator('#agent-input').fill('Review the confirmed portfolio risk');await page.locator('#agent-send').click();await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('Review the confirmed portfolio risk'));
      const run = await api('/fixture/run',{});assert.ok(run.calls>=5);
      await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('已完成'),{timeout:20000});
      await open('当前方案/复核');await ui.getByText('不行动对照',{exact:true}).waitFor();assert.match(await ui.innerText(),/方案互斥/);assert.match(await ui.innerText(),/当前有效/);
      const plan = await api(`/portfolio/plans/current?account_id=${account.id}`);assert.equal(plan.status,'published');assert.equal(plan.plan.options.length,2);assert.equal(plan.plan.options[0].executable,true);
      const calc = await api(`/portfolio/calculations/${plan.plan.calculation_ref.id}?account_id=${account.id}`);assert.equal(calc.details.scenarios[1].cash,'1099');assert.match(calc.details.scenarios[1].nav,/^1999(?:\.0+)?$/);assert.equal(calc.details.scenarios[1].holdings[0].quantity,'90');assert.match(await ui.innerText(),/本次交易数量：10/);assert.match(await ui.innerText(),/方案后持仓数量 90/);assert.match(await ui.innerText(),/固定费用 1 CNY/);
      await page.screenshot({path:join(output,`portfolio-${viewport.width}-plan.png`),fullPage:true});await ui.screenshot({path:join(output,`portfolio-${viewport.width}-card.png`)});await ui.evaluate(node=>node.scrollIntoView({block:'start'}));await page.screenshot({path:join(output,`portfolio-${viewport.width}-card-viewport.png`)});await ui.locator('fieldset').first().evaluate(node=>node.scrollIntoView({block:'start'}));await page.screenshot({path:join(output,`portfolio-${viewport.width}-options-viewport.png`)});await ui.locator('fieldset').nth(1).evaluate(node=>node.scrollIntoView({block:'start'}));await page.screenshot({path:join(output,`portfolio-${viewport.width}-directional-viewport.png`)});
      // Accept no-action separately; it never writes to ledger or reserves cash.
      let count = (await api(`/portfolio/events?account_id=${account.id}`)).items.length;
      const none = ui.getByRole('group',{name:'不行动对照',exact:true});await none.getByLabel('我的原话（未来愿望不会记入账本）').fill('接受不行动');await none.getByRole('button',{name:'确认记录一个选项的决定'}).click();await saved();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,count);
      await open('当前方案/复核');const options = ui.locator('fieldset');await options.nth(1).getByLabel('本人的决定').selectOption('defer');await options.nth(1).getByLabel('我的原话（未来愿望不会记入账本）').fill('以后再考虑');await options.nth(1).getByRole('button',{name:'确认记录一个选项的决定'}).click();await saved();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,count);
      await open('当前方案/复核');await ui.locator('fieldset').nth(2).getByLabel('本人的决定').selectOption('independent');await ui.locator('fieldset').nth(2).getByLabel('我的原话（未来愿望不会记入账本）').fill('自行决定，不预留');await ui.locator('fieldset').nth(2).getByRole('button',{name:'确认记录一个选项的决定'}).click();await saved();
      // Actual trade never pre-fills hypothetical quantity/price/fee. Lost ACK then reload retries exact body.
      await open('当前方案/复核');await ui.locator('fieldset').nth(1).getByLabel('我的原话（未来愿望不会记入账本）').fill('接受本项测算但不代表已成交');await ui.locator('fieldset').nth(1).getByRole('button',{name:'确认记录一个选项的决定'}).click();await saved();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,count);assert.equal((await api(`/portfolio/snapshot?account_id=${account.id}`)).known_cash,'1000');
      await open('已经成交');await ui.locator(':scope > section:last-child').screenshot({path:join(output,`portfolio-${viewport.width}-trade-form.png`)});await ui.locator(':scope > section:last-child').evaluate(node=>node.scrollIntoView({block:'start'}));await page.screenshot({path:join(output,`portfolio-${viewport.width}-trade-viewport.png`)});assert.equal(await ui.getByLabel('本次成交数量（不是最终持仓）').inputValue(),'');assert.equal(await ui.getByLabel('实际成交价格').inputValue(),'');assert.equal(await ui.getByLabel('实际费用（未知留空，明确免费填0）').inputValue(),'');
      await ui.getByLabel('查找证券名称或完整代码').fill('600001.SH');await open('查找证券');await ui.getByRole('button',{name:/组合证券.*600001.SH/}).click();await ui.getByLabel('实际方向').selectOption('sell');await ui.getByLabel('本次成交数量（不是最终持仓）').fill('10');await ui.getByLabel('实际成交价格').fill('10');await fillFacts(new Date().toISOString());
      await page.route('**/portfolio/events',async route=>{if(route.request().method() === 'POST') {await route.fetch();await route.abort('failed');}else await route.continue();});
      await open('确认保存实际成交');await ui.getByRole('status').filter({hasText:'网络请求失败'}).waitFor();const pending = await page.evaluate(()=>JSON.parse(sessionStorage.getItem('zcharlie.portfolio.pending.v1')));assert.equal(pending.request.fact.fee,null);await page.unroute('**/portfolio/events');
      await api('/fixture/writes/false',{});await open('重取原待确认请求');await ui.getByRole('status').filter({hasText:'账本写入暂时关闭'}).waitFor();assert.deepEqual(await page.evaluate(()=>JSON.parse(sessionStorage.getItem('zcharlie.portfolio.pending.v1'))),pending);
      await api('/fixture/writes/true',{});await page.reload();await open('重取原待确认请求');await saved();assert.equal((await api(`/portfolio/events?account_id=${account.id}`)).items.length,count+1);
      snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.holdings[0].confirmed_quantity,'90');assert.equal(snap.known_cash,null);
      await open('当前方案/复核');await ui.getByText(/当前状态：需要复核/).waitFor();assert.equal((await api(`/portfolio/plans/current?account_id=${account.id}`)).plan.options[0].executable,plan.plan.options[0].executable);
      await page.screenshot({path:join(output,`portfolio-${viewport.width}-limited.png`),fullPage:true});
      // Explicit new research against changed facts uses the real native worker and produces public failure fields.
      await api(`/fixture/portfolio/${account.id}`,{});await open('刷新账户与快照');await open('绑定此快照到下一条消息');await page.locator('#agent-input').fill('Recheck after the actual unknown-fee trade');await page.locator('#agent-send').click();await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('Recheck after the actual unknown-fee trade'));await api('/fixture/run',{});await page.waitForFunction(()=>document.querySelectorAll('.agent-task').length === 2 && [...document.querySelectorAll('.agent-task')].every(x=>/已完成|失败|已取消|部分完成/.test(x.textContent)));await tab('portfolio');await ui.getByText('组合发布检查/受限原因',{exact:true}).first().waitFor();assert.equal((await api(`/portfolio/plans/current?account_id=${account.id}`)).status,'review_required');const conversationId = new URL(page.url()).searchParams.get('conversation');const messages = await api(`/agent/conversations/${conversationId}/messages`);const limitedTask = await api(`/agent/tasks/${messages.filter(x=>x.role === 'user').at(-1).task_ref.id}`);assert.equal(limitedTask.status,'partial');assert.equal(limitedTask.stop_reason,'evidence_blocked');
      // Original traceable reversal, then partial balance with unknown cash; immutable old snapshot remains.
      await open('事实历史/冲销');await ui.getByRole('button',{name:'冲销这条原始事实'}).nth(1).click();await ui.getByLabel('冲销原因').fill('测试回报录入错误');await fillFacts(new Date().toISOString());await open('确认冲销原事实');await saved();snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.known_cash,'1000');assert.equal(snap.holdings[0].confirmed_quantity,'100');
      await api(`/fixture/portfolio/${account.id}`,{});await goChat();await page.locator('#agent-new').click();await page.waitForFunction(()=>!document.querySelector('#agent-send').disabled);await open('绑定此快照到下一条消息');await page.locator('#agent-input').fill('Recheck after the correction');await page.locator('#agent-send').click();await page.waitForFunction(()=>document.querySelector('#agent-messages').textContent.includes('Recheck after the correction'));await api('/fixture/run',{});await page.waitForFunction(()=>document.querySelectorAll('.agent-task').length === 1 && [...document.querySelectorAll('.agent-task')].every(x=>/已完成|失败|已取消|部分完成/.test(x.textContent)));await open('当前方案/复核');await ui.getByText('不行动对照',{exact:true}).waitFor();const replacement = await api(`/portfolio/plans/current?account_id=${account.id}`);assert.equal(replacement.status,'published');assert.equal(replacement.plan.previous_ref.id,plan.plan.id);await open('回看上一份不可变整案');await ui.getByText(/当前状态：已被替代/).waitFor();
      await open('对账修正');await ui.getByLabel('资产范围（例如本账户全部A股和现金）').fill('仅已核对证券，其他资产未知');await ui.getByLabel('差异说明（无差异也明确写无）').fill('现金尚未核对');await fillFacts(new Date().toISOString());await open('确认保存余额事实');await saved();snap = await api(`/portfolio/snapshot?account_id=${account.id}`);assert.equal(snap.coverage,'partial');assert.equal(snap.known_cash,null);assert.equal(snap.total_assets,null);
      // Real concurrent cash event creates stale revision; UI rejects and asks explicit review.
      await open('资金转入/转出');await ui.getByLabel('实际金额（转出为负数）').fill('1');await fillFacts(new Date().toISOString());
      await api('/portfolio/events',{account_id:account.id,expected_revision:snap.revision,idempotency_key:randomUUID(),fact:{kind:'cash_transfer',amount:'1',occurred_at:new Date().toISOString(),source:'concurrent actual fact',user_confirmed:true}});
      await open('确认保存资金事实');await ui.getByRole('status').filter({hasText:'版本已变化'}).waitFor();assert.equal(await page.evaluate(()=>sessionStorage.getItem('zcharlie.portfolio.pending.v1')),null);
      assert.equal(await page.locator('#agent-portfolio img,#agent-portfolio script').count(),0);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      // Actual read-only/default-off boundary and no automatic model call on refresh/account changes.
      const calls = (await api('/fixture/state')).calls;await api('/fixture/writes/false',{});await open('刷新账户与快照');await ui.getByText('账本写入关闭：请在设置页明确启用真实组合账本。读取历史不会启动模型。').waitFor();assert.equal(await ui.getByRole('button',{name:'新建账户',exact:true}).isDisabled(),true);assert.equal((await api('/fixture/state')).calls,calls);await page.screenshot({path:join(output,`portfolio-${viewport.width}-disabled.png`),fullPage:true});
      assert.deepEqual(errors,[]);
      console.log(`AG24 ${viewport.width}: real HTTP/SQLite/native-worker flow passed, calls=${run.calls}`);
    } finally {await context.close();server.kill('SIGTERM');await new Promise(resolve=>server.exitCode !== null ? resolve() : server.once('exit',resolve));await rm(temp,{recursive:true,force:true});}
  }
} finally {await browser.close();}
