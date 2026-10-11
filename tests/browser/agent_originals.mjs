// Real isolated API projections rendered in desktop/mobile Chromium.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const temp=await mkdtemp(join(tmpdir(),'ag26-browser-')),port=19859,base=`http://127.0.0.1:${port}`;
const server=spawn('.venv/bin/python',['tests/browser/agent_fixture_server.py'],{env:{...process.env,AG17_FIXTURE_DIR:temp,AG17_FIXTURE_PORT:String(port)},stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
const headers={'X-API-Key':'ag17-fixture-key','Content-Type':'application/json'};
async function api(path,body) {const r=await fetch(base+path,{method:body?'POST':'GET',headers,body:body?JSON.stringify(body):undefined});if(!r.ok)await new Promise(done=>setTimeout(done,200));assert.ok(r.ok,(await r.clone().text())+logs);return r.json();}
try {
  for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  const fixture=await api('/fixture/ag26',{});
  browser=await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
    const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.stack || String(e)));page.on('dialog',()=>{throw Error('XSS dialog');});
    await page.goto(base+'/app/agent?pane=drawer');await page.locator('[name=api_key]').fill('ag17-fixture-key');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/agent?pane=drawer');
    await page.getByRole('button',{name:'公司档案',exact:true}).waitFor();
    await page.evaluate(task=>document.querySelector('#agent-workbench').dispatchEvent(new CustomEvent('agent:task',{detail:task})),fixture.task);
    await page.getByRole('button',{name:new RegExp('研究过程 '+fixture.task.id)}).click();
    await page.locator(`[data-ref-id="${fixture.fact.id}"]`).first().click();
    await page.getByRole('button',{name:'查看原文片段',exact:true}).click();
    const excerpt=page.locator('[aria-label="原文片段"]');await excerpt.getByRole('button',{name:'下一页原文片段',exact:true}).waitFor();
    assert.match(await excerpt.innerText(),/证券代码：000001/);assert.match(await excerpt.innerText(),/table:1\/row:2/);assert.match(await excerpt.innerText(),/12,345\.6789/);
    assert.match(await excerpt.innerText(),/原文候选事实（待核验）/);assert.match(await excerpt.innerText(),/net_profit_total/);assert.match(await excerpt.innerText(),/片段校验：[a-f0-9]{64}/);
    assert.match(await excerpt.innerText(),/<img src=x onerror=alert\(1\)>/);assert.equal(await excerpt.locator('img,script,iframe').count(),0);
    await excerpt.getByRole('button',{name:'下一页原文片段',exact:true}).click();await excerpt.getByRole('button',{name:'上一页原文片段',exact:true}).waitFor();assert.doesNotMatch(await excerpt.innerText(),/证券代码：000001/);
    await excerpt.getByRole('button',{name:'上一页原文片段',exact:true}).click();await excerpt.getByText('证券代码：000001',{exact:true}).waitFor();
    const link=excerpt.getByRole('link',{name:'下载原件附件',exact:true});assert.match(await link.getAttribute('href'),new RegExp(fixture.original_id));
    const download=await page.request.get(base+(await link.getAttribute('href')));assert.equal(download.status(),200);assert.match(download.headers()['content-disposition'],/^attachment;/);assert.equal(download.headers()['content-security-policy'],"sandbox; default-src 'none'");
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);assert.doesNotMatch(await page.locator('body').innerText(),/HTTP 500/);
    await page.screenshot({path:`/tmp/ag26-original-${viewport.width}.png`,fullPage:true});await context.close();
  }
  console.log('AG26 desktop/mobile actual saved source, candidate provenance, safe excerpts, pagination and authenticated sandbox attachment passed');
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));await rm(temp,{recursive:true,force:true});}
