// Real isolated API projections rendered in desktop/mobile Chromium.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const temp=await mkdtemp(join(tmpdir(),'ag18-browser-')),port=19847,base=`http://127.0.0.1:${port}`;
const server=spawn('.venv/bin/python',['tests/browser/agent_fixture_server.py'],{env:{...process.env,AG17_FIXTURE_DIR:temp,AG17_FIXTURE_PORT:String(port)},stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
const headers={'X-API-Key':'ag17-fixture-key','Content-Type':'application/json'};
async function api(path,body) {const r=await fetch(base+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});if(!r.ok)await new Promise(done=>setTimeout(done,200));assert.ok(r.ok,(await r.clone().text())+logs);return r.json();}
try {
  for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const fixture=await api('/fixture/ag18',{});
  browser=await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
    const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.stack || String(e)));page.on('dialog',()=>{throw Error('XSS dialog');});
    await page.goto(base+'/app/agent?pane=drawer');await page.locator('[name=api_key]').fill('ag17-fixture-key');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/agent?pane=drawer&conversation='+fixture.conversation_id);
    if(!await page.getByRole('button',{name:'公司档案',exact:true}).count()){await page.waitForTimeout(1000);assert.ok(await page.getByRole('button',{name:'公司档案',exact:true}).count(),JSON.stringify(errors)+' '+await page.locator('body').innerText());}
    if(!await page.getByRole('button',{name:'对话产物',exact:true}).isVisible()){if(await page.locator('#agent-drawer-toggle').isVisible())await page.locator('#agent-drawer-toggle').click();else await page.getByRole('button',{name:'研究档案',exact:true}).click();}
    await page.getByRole('button',{name:'对话产物',exact:true}).click();
    await page.locator('.agent-conversation-progress').first().waitFor();
    await page.waitForFunction(()=>document.querySelector('.agent-conversation-progress')?.innerText.includes('读取保存的现金证据'));
    assert.match(await page.locator('.agent-conversation-progress').first().innerText(),/仍需现金流附注/);
    assert.ok(await page.locator('.agent-conversation-progress [data-ref-id]').count());
    assert.match(await page.locator('.agent-collected-materials summary').first().innerText(),/已保存材料 1 项（含候选或缺失记录）/);
    await page.getByRole('button',{name:'公司档案',exact:true}).click();await page.locator(`[data-case-id="${fixture.case_id}"]`).click();
    await page.getByRole('heading',{name:'用户决定时间线'}).waitFor();assert.match(await page.locator('.agent-review-body').innerText(),/现金来源是否可靠/);assert.match(await page.locator('.agent-review-body').innerText(),/旧观点已被替代/);assert.match(await page.locator('.agent-review-body').innerText(),/1234500000.00 CNY/);assert.match(await page.locator('.agent-review-body').innerText(),/未知不等于未采纳/);
    await page.locator(`[data-ref-id="${fixture.recommendations[1].id}"]`).first().click();
    await page.getByRole('heading',{name:'正式公司观点',exact:true}).waitFor();assert.match(await page.locator('.agent-review-body').innerText(),/1234500000.00 CNY/);assert.match(await page.locator('.agent-review-body').innerText(),/历史正文截止/);assert.match(await page.locator('.agent-review-body').innerText(),/待复核/);
    await page.getByRole('button',{name:'依据',exact:true}).click();await page.getByRole('heading',{name:new RegExp('证据 '+fixture.fact.id)}).waitFor();await page.waitForFunction(()=>document.querySelector('.agent-source-drawer').innerText.includes('12.345'));assert.match(await page.locator('.agent-source-drawer').innerText(),/12.345/);assert.equal(await page.locator('.agent-review-body img,.agent-source-drawer img').count(),0);
    await page.getByRole('button',{name:'公司档案',exact:true}).click();await page.locator(`[data-case-id="${fixture.case_id}"]`).click();
    await page.locator(`[data-ref-id="${fixture.calculation.id}"]`).click();await page.getByRole('heading',{name:new RegExp('计算 '+fixture.calculation.id)}).waitFor();await page.waitForFunction(()=>document.querySelector('.agent-source-drawer').innerText.includes('缺少输入'));await page.locator('.agent-source-drawer summary').click();assert.match(await page.locator('.agent-source-drawer').innerText(),/缺少输入/);assert.match(await page.locator('.agent-source-drawer').innerText(),/未知/);assert.match(await page.locator('.agent-source-drawer').innerText(),/0\/4/);
    // Source requests have their own generation: a slow A cannot overwrite B or reopen a closed drawer.
    await page.getByRole('button',{name:'关闭来源',exact:true}).click();
    let sourceRelease;const sourceDelay=new Promise(resolve=>sourceRelease=resolve),sourceRequested=new Promise(resolve=>{
      page.route('**/investor/evidence/*',async route=>{resolve();await sourceDelay;await route.continue();});
    });
    await page.locator(`[data-ref-id="${fixture.fact.id}"]`).first().click();await sourceRequested;
    await page.locator(`[data-ref-id="${fixture.calculation.id}"]`).click();await page.waitForFunction(()=>document.querySelector('.agent-source-drawer').innerText.includes('缺少输入'));sourceRelease();await page.waitForTimeout(200);
    assert.match(await page.locator('.agent-source-drawer h3').innerText(),new RegExp(fixture.calculation.id));await page.unroute('**/investor/evidence/*');
    let closeRelease;const closeDelay=new Promise(resolve=>closeRelease=resolve),closeRequested=new Promise(resolve=>{
      page.route('**/investor/evidence/*',async route=>{resolve();await closeDelay;await route.continue();});
    });
    await page.locator(`[data-ref-id="${fixture.fact.id}"]`).first().click();await closeRequested;await page.getByRole('button',{name:'关闭来源',exact:true}).click();closeRelease();await page.waitForTimeout(200);assert.equal(await page.locator('.agent-source-drawer').isVisible(),false);await page.unroute('**/investor/evidence/*');
    await page.getByRole('button',{name:'站内提醒',exact:true}).click();await page.getByRole('button',{name:/标为/}).waitFor();assert.match(await page.locator('.agent-notification-content').innerText(),/材料变化，需要复核/);
await page.getByRole('button',{name:/标为/}).click();await page.waitForFunction(()=>document.querySelector('.agent-review-body').innerText.includes('服务器已确认'));assert.equal((await api('/investor/notifications')).length,1);assert.match(await page.locator('.agent-notification-content').innerText(),/材料变化，需要复核/);assert.match(await page.locator('.agent-notification-read').innerText(),/服务器已确认/);
    await page.getByRole('button',{name:'线索与探索',exact:true}).click();await page.getByRole('button',{name:/探索批次/}).click();await page.getByRole('heading',{name:'有限探索处理过程'}).waitFor();assert.match(await page.locator('.agent-review-body').innerText(),/非研究成功/);assert.match(await page.locator('.agent-review-body').innerText(),/后台研究已暂停/);assert.match(await page.locator('.agent-review-body').innerText(),/恢复后台并获得核验材料/);
    await page.getByRole('button',{name:'公司档案',exact:true}).click();await page.locator(`[data-case-id="${fixture.case_id}"]`).click();const compare=page.getByRole('button',{name:'并列比较保存的观点版本'});await compare.waitFor();assert.equal(await compare.count(),1);{await compare.click();await page.getByRole('heading',{name:'观点版本并列比较'}).waitFor();assert.equal(await page.locator('.agent-opinion').count(),2);}
    // Actual HTTP failures and a delayed old-object response must not replace a new view.
    await page.route('**/investor/recommendations/*/body?revision=1',route=>route.fulfill({status:503,json:{detail:'fixture 暂不可读'}}));
    await page.getByRole('button',{name:'刷新当前有效性'}).last().click();await page.getByRole('button',{name:'重试',exact:true}).waitFor();
    await page.unroute('**/investor/recommendations/*/body?revision=1');await page.getByRole('button',{name:'重试',exact:true}).click();await page.getByRole('heading',{name:'正式公司观点',exact:true}).waitFor();
    let release;const delayed=new Promise(resolve=>release=resolve),requested=new Promise(resolve=>{
      page.route('**/investor/recommendations/*/body?revision=1',async route=>{resolve();await delayed;await route.continue();});
    });
    await page.getByRole('button',{name:'刷新当前有效性'}).click();await requested;
    await page.getByRole('button',{name:'站内提醒',exact:true}).click();await page.getByRole('heading',{name:'站内提醒',exact:true}).waitFor();release();await page.waitForTimeout(200);assert.equal(await page.locator('.agent-opinion').count(),0);await page.unroute('**/investor/recommendations/*/body?revision=1');
    await page.getByRole('button',{name:'公司档案',exact:true}).click();await page.locator(`[data-case-id="${fixture.case_id}"]`).click();await page.getByRole('button',{name:'并列比较保存的观点版本'}).click();await page.getByRole('heading',{name:'观点版本并列比较'}).waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);await page.screenshot({path:`/tmp/ag18-views-${viewport.width}.png`,fullPage:true});await context.close();
  }
  console.log('AG18 desktop/mobile source, historical opinion, failed calculation, notification CAS, discovery and timeline passed');
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));await rm(temp,{recursive:true,force:true});}
