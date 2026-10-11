// Real HTTP/native FunctionModel/SQLite. Aborted acknowledgements fetch real API first.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, mkdir, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const browser = await chromium.launch({headless:true, executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
const output = 'docs/product/probes/ag30-ui';
await mkdir(output,{recursive:true});
try {
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const temp = await mkdtemp(join(tmpdir(),'ag30-browser-')), base = 'http://127.0.0.1:19861';
    const server = spawn('.venv/bin/python',['tests/browser/method_fixture_server.py'],{env:{...process.env,AG30_FIXTURE_DIR:temp,AG30_FIXTURE_PORT:'19861'},stdio:['ignore','pipe','pipe']});
    let logs='';server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
    const context = await browser.newContext({viewport}), page = await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(String(e)));
    async function api(path,body) {const r=await fetch(base+path,{method:body?'POST':'GET',headers:{'X-API-Key':'fixture-app','Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});assert.ok(r.ok,(await r.clone().text())+logs);return r.json();}
    const ui=page.locator('#method-workbench');
    const click=name=>ui.getByRole('button',{name,exact:true}).click();
    try {
      for(let i=0;i<180;i++){if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
      await page.goto(base+'/app/methods');await page.locator('[name=api_key]').fill('fixture-app');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/methods');
      await ui.getByText('尚无方法提案。需要真实已完成任务复盘。').waitFor();
      await ui.getByLabel('改进摘要').fill('加强反向解释 <img src=x onerror=alert(1)>');await ui.getByLabel('实际错误模式与证据说明').fill('实际不完整复盘只留下一个解释，检查失败与反例。');await ui.getByLabel('要求两个不同的反向解释').check();await click('保存提案（不会启用）');
      await ui.getByRole('heading',{name:'加强反向解释 <img src=x onerror=alert(1)>'}).waitFor();
      let data=await api('/investor/methods');assert.equal(data.heads.length,0);assert.equal(data.proposals.length,1);
      await click('评估真实失败与反例');await ui.getByText(/等待影子检查与批准/).waitFor();
      await click('运行冻结样本的离线影子检查');await ui.getByRole('status').filter({hasText:'已保存'}).waitFor();assert.equal(await ui.getByRole('button',{name:'明确批准并用于该证券的新任务',exact:true}).count(),0);
      await api('/fixture/prospective',{});await click('刷新记录');await ui.getByLabel('评估后新建的成功研究任务').locator('option').waitFor({state:'attached'});await click('记录新任务的前瞻步骤影子');await ui.getByRole('button',{name:'明确批准并用于该证券的新任务',exact:true}).waitFor();
      await ui.locator('details').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));await ui.locator('#method-proposals').screenshot({path:join(output,`methods-${viewport.width}-evidence.png`)});
      // Commit approval then drop acknowledgement. Reload retries exactly the saved body.
      await page.route('**/investor/methods/decisions',async route=>{await route.fetch();await route.abort('failed');});
      await click('明确批准并用于该证券的新任务');await ui.getByRole('status').filter({hasText:'原操作内容已保留'}).waitFor();
      const pending=await page.evaluate(()=>JSON.parse(sessionStorage.getItem('investor.method.pending.v1')));assert.equal(pending.body.decision,'approve');
      data=await api('/investor/methods');assert.equal(data.heads[0].revision,1);assert.equal(data.proposals[0].proposal.status,'active');
      await page.unroute('**/investor/methods/decisions');await page.reload();await click('重试待确认的原操作');await ui.getByRole('status').filter({hasText:'已保存'}).waitFor();
      data=await api('/investor/methods');assert.equal(data.heads[0].revision,1);assert.equal(await page.evaluate(()=>sessionStorage.getItem('investor.method.pending.v1')),null);
      await page.screenshot({path:join(output,`methods-${viewport.width}-active.png`),fullPage:true});
      // A real competing rollback makes the old UI request stale; no automatic resubmission.
      const before=data.heads[0];await ui.getByLabel('回退原因').fill('审阅后回到基线');
      await api('/investor/methods/rollback',{mutation_id:randomUUID(),listing_ref:before.binding.listing_ref,expected_head_revision:before.revision,target_method:data.baseline_method,reason:'Concurrent authenticated user rollback'});
      await click('明确回退并追加版本');await ui.getByRole('status').filter({hasText:'版本或证据已改变'}).waitFor();
      data=await api('/investor/methods');assert.equal(data.heads[0].revision,2);assert.equal(data.proposals[0].proposal.status,'retired');
      await ui.getByLabel('回退原因').fill('再次明确回退并保留历史');await click('明确回退并追加版本');await ui.getByRole('status').filter({hasText:'已保存'}).waitFor();
      data=await api('/investor/methods');assert.equal(data.heads[0].revision,3);assert.equal(data.versions.length,3);assert.ok((await api('/investor/methods/proposals/' + data.proposals[0].proposal.id)).shadows[0].cases.length===3);
      await page.screenshot({path:join(output,`methods-${viewport.width}-rollback.png`),fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);assert.equal(await ui.locator('img').count(),0);assert.deepEqual(errors,[]);
      await context.clearCookies();await click('刷新记录');await ui.getByRole('status').filter({hasText:'登录已过期'}).waitFor();
      console.log(`AG30 ${viewport.width}: native frozen failure/inside/outside evaluation, shadow, user approval, lost ack/reload, real CAS409 rollback history, auth401 and safe DOM passed`);
    } finally {await context.close();server.kill('SIGTERM');await new Promise(r=>server.exitCode!==null?r():server.once('exit',r));await rm(temp,{recursive:true,force:true});}
  }
} finally {await browser.close();}
