// Verification status: NOT RUN — browser launch permission was declined. Syntax/HTTP/Node tests only.
// Real FastAPI/SQLite simulation workbench. All data and provider configuration are synthetic.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import assert from 'node:assert/strict';

const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const folder=await mkdtemp(join(tmpdir(),'simulations-browser-'));
const port=Number(process.env.SIM_FIXTURE_PORT||19886),base=`http://127.0.0.1:${port}`;
const key='simulations-synthetic-browser-key';
const server=spawn('.venv/bin/python',['tests/browser/simulations_fixture_server.py'],{
  cwd:resolve('.'),env:{...process.env,SIM_FIXTURE_DIR:folder,SIM_FIXTURE_PORT:String(port)},stdio:['ignore','pipe','pipe'],
});
let serverLog='';server.stdout.on('data',value=>serverLog+=value);server.stderr.on('data',value=>serverLog+=value);
let browser;
async function ready(){for(let i=0;i<200;i++){if(server.exitCode!==null)throw Error(`fixture exited: ${serverLog}`);try{if((await fetch(base+'/healthz')).ok)return;}catch{}await new Promise(done=>setTimeout(done,100));}throw Error(`fixture startup timed out: ${serverLog}`);}
async function api(path,body){const response=await fetch(base+path,{method:body===undefined?'GET':'POST',headers:{'X-API-Key':key,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});assert.ok(response.ok,`${path}: ${response.status} ${await response.clone().text()}`);return response.json();}
try{
  await ready();
  const fixture=await api('/fixture/state');
  browser=await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  const context=await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  const page=await context.newPage(),errors=[],csp=[];
  await page.clock.setFixedTime(new Date('2026-11-11T09:00:00Z'));
  page.on('pageerror',error=>errors.push(error.message));
  page.on('console',event=>{if(event.type()==='error'&&/Content Security Policy|Refused to/.test(event.text()))csp.push(event.text());});
  await page.goto(base+'/app/login');await page.locator('[name=api_key]').fill(key);await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.goto(base+'/app/simulations');
  await page.getByRole('heading',{name:'合成样本 · 已结算与观察中',exact:true}).waitFor();
  await page.getByRole('heading',{name:'合成样本 · 缺少逐日证据',exact:true}).waitFor();
  await page.goto(`${base}/app/simulations/${fixture.demo_id}`);
  await page.locator('#sim-title').getByText('合成样本 · 已结算与观察中').waitFor();
  await page.locator('#sim-leaderboard table tbody tr').first().waitFor();
  assert.equal(await page.locator('#sim-leaderboard tbody tr').count(),4);
  await page.locator('#sim-nav-chart svg path').first().waitFor();
  await page.getByRole('tab',{name:'账户与持仓'}).click();
  assert.match(await page.locator('#sim-accounts').innerText(),/1000000|1,000,000/);
  assert.match(await page.locator('#sim-accounts').innerText(),/合成样本 <img src=x onerror=alert\(1\)>/);
  assert.equal(await page.locator('#simulations-workbench img').count(),0,'provider/company text is never HTML');
  await page.getByRole('tab',{name:'决策与证据'}).click();
  const modelA = page.locator('#sim-decisions details').filter({hasText:'模型 A'});
  await modelA.locator('summary').click();
  await modelA.getByText('合成判断：使用冻结资料观察经营改善；价格变化尚不能证伪经营假设。',{exact:true}).waitFor();
  await page.getByRole('tab',{name:'订单与成交'}).click();
  assert.equal(await page.locator('#sim-fills tbody tr').count(),3);
  assert.match(await page.locator('#sim-fills').innerText(),/系统确认时间/);
  await page.getByRole('tab',{name:'复盘与成本'}).click();
  await page.getByText('仍在观察',{exact:true}).first().waitFor();
  assert.match(await page.locator('#sim-costs').innerText(),/按保守规则估算/);
  assert.match(await page.locator('#sim-costs').innerText(),/响应不确定/);
  await page.getByRole('tab',{name:'资料与运行'}).click();
  await page.locator('#sim-availability').getByText('该日资料已就绪',{exact:false}).waitFor();
  await page.locator('#sim-rounds summary').first().click();
  assert.match(await page.locator('#sim-rounds').innerText(),/manifest SHA-256/);
  await page.screenshot({path:join(folder,'settled-desktop.png'),fullPage:true});
  const downloadEvent=page.waitForEvent('download');await page.locator('#sim-export').click();const exported=await downloadEvent;
  const exportedText=await readFile(await exported.path(),'utf8');
  assert.doesNotMatch(exportedText,/synthetic-model-key|simulation-fixture\.invalid|model_snapshot|artifact_path/);
  assert.match(exportedText,/simulation_export_v1/);

  await page.goto(`${base}/app/simulations/${fixture.waiting_id}`);
  await page.getByRole('tab',{name:'资料与运行'}).click();
  await page.locator('#sim-availability').getByText(/缺少已核对的当日交易规则证据/).first().waitFor();
  await page.locator('#sim-rounds summary').first().click();
  assert.match(await page.locator('#sim-rounds').innerText(),/等待公共资料/);
  assert.match(await page.locator('#sim-rounds').innerText(),/reservation/);
  await page.screenshot({path:join(folder,'missing-evidence-desktop.png'),fullPage:true});

  // Create through the actual friendly API. A committed response is deliberately lost.
  await page.goto(base+'/app/simulations');await page.locator('#sim-create-open').click();
  const form=page.locator('#sim-create-form');await form.locator('[name=name]').fill('浏览器创建 · 原请求恢复');
  await form.locator('[name=start_date]').fill('2026-11-12');await form.locator('[name=end_date]').fill('2026-12-09');
  await form.locator('[name=initial_cash]').fill('1000000.000000000000000001');
  await page.locator('#sim-stock-query').fill('600519');await page.locator('#sim-stock-search').click();
  await page.locator('#sim-stock-results .sim-search-result').first().click();
  await page.locator('#sim-agent-rows .sim-agent-row').first().locator('[name=agent_name]').fill('浏览器模型 A');
  await page.locator('#sim-agent-add').click();
  await page.locator('#sim-agent-rows .sim-agent-row').last().locator('[name=agent_name]').fill('浏览器模型 B');
  assert.equal(await page.locator('#sim-agent-rows .sim-agent-row').count(),2);
  const bodies=[];let dropped=false;
  await page.route('**/api/simulations',async route=>{if(route.request().method()!=='POST')return route.continue();bodies.push(route.request().postDataJSON());if(!dropped){dropped=true;await route.fetch();await route.abort('failed');}else await route.continue();});
  await page.locator('#sim-create-submit').click();await page.locator('#sim-pending').waitFor({state:'visible'});
  await page.getByText(/网络请求失败或超时/).waitFor();
  await page.locator('#sim-retry-write').click();await page.waitForURL(/\/app\/simulations\/experiment-/);
  await page.locator('#sim-title').getByText('浏览器创建 · 原请求恢复').waitFor();
  assert.equal(bodies.length,2);assert.deepEqual(bodies[0],bodies[1]);
  assert.equal(typeof bodies[0].initial_cash,'string');assert.equal(bodies[0].initial_cash,'1000000.000000000000000001');
  assert.equal(bodies[0].agents[0].budget.round_cost_limit,'3');
  const createdID=new URL(page.url()).pathname.split('/').at(-1),createdPath=`/api/simulations/${createdID}`;
  assert.equal((await api('/api/simulations')).filter(item=>item.config.name==='浏览器创建 · 原请求恢复').length,1);
  assert.equal((await api(createdPath)).participants.length,4);
  await page.locator('[data-sim-action=start]').click();await page.locator('#sim-state').getByText('运行中',{exact:true}).waitFor();
  await page.locator('[data-sim-action=pause]').click();await page.locator('#sim-state').getByText('已暂停',{exact:true}).waitFor();
  await page.locator('[data-sim-action=start]').click();await page.locator('#sim-state').getByText('运行中',{exact:true}).waitFor();
  await page.locator('[data-sim-action=run-now]').click();await page.locator('#sim-message').getByText('推进运行已由服务器确认。').waitFor();
  await page.locator('[data-sim-action=data-refresh]').click();await page.locator('#sim-message').getByText('采集实验数据已由服务器确认。').waitFor();

  // The independent write advances revision behind the displayed page.
  const current=(await api(createdPath)).experiment;
  await api(`${createdPath}/pause`,{expected_revision:current.revision,idempotency_key:'outside-pause'});
  await page.locator('[data-sim-action=run-now]').click();
  await page.locator('#sim-message').getByText(/revision_conflict/).waitFor();
  await page.locator('#sim-state').getByText('已暂停',{exact:true}).waitFor();

  // Evidence import is a real, explicit user write and does not claim to supply absent bars.
  await page.getByRole('tab',{name:'资料与运行'}).click();
  await page.getByText('导入已核对的交易规则证据',{exact:true}).click();
  const evidence=page.locator('#sim-evidence-form');
  await evidence.locator('[name=listing_id]').selectOption(String(fixture.listing_ids[0]));
  for(const [name,value] of Object.entries({trade_date:'2026-11-11',ipo_date:'2000-01-01',reference_price:'10',upper_limit:'11',lower_limit:'9',rule_source:'fixture:browser-rule',source_ref:'fixture:browser-session',published_at:'2026-11-11T08:00',captured_at:'2026-11-11T16:00'}))await evidence.locator(`[name=${name}]`).fill(value);
  for(const name of ['ordinary_limit_session','rule_verified','corporate_actions_complete'])await evidence.locator(`[name=${name}]`).check();
  await evidence.getByRole('button',{name:'导入交易证据',exact:true}).click();await page.locator('#sim-message').getByText('导入交易证据已由服务器确认。').waitFor();
  const readiness=await api(`${createdPath}/availability?trade_date=2026-11-11`);
  assert.ok(readiness.securities[0].evidence_id);assert.ok(readiness.securities[0].missing.includes('raw_market_missing'));

  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/app/simulations/${fixture.demo_id}`);await page.locator('#sim-leaderboard table').waitFor();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'mobile shell must not overflow horizontally');
  await page.getByRole('tab',{name:'账户与持仓'}).click();await page.screenshot({path:join(folder,'accounts-mobile.png'),fullPage:true});
  assert.equal((await api('/fixture/state')).external_calls,0);
  assert.deepEqual(errors,[]);assert.deepEqual(csp,[]);
  console.log(JSON.stringify({result:'passed',desktop_and_mobile:true,exact_request_replay:true,real_http_sqlite:true,external_calls:0}));
}catch(error){console.error(serverLog);throw error;}
finally{if(browser)await browser.close();if(server.exitCode===null){server.kill('SIGTERM');await new Promise(done=>server.once('exit',done));}await rm(folder,{recursive:true,force:true});}
