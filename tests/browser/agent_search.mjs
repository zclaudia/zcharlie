// Real isolated API projections rendered in desktop/mobile Chromium.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const temp=await mkdtemp(join(tmpdir(),'ag27-browser-')),port=19861,base=`http://127.0.0.1:${port}`;
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
    const evidence=await api(`/investor/evidence/${fixture.fact.id}`);
    await page.evaluate(ref=>document.querySelector('#agent-workbench').dispatchEvent(new CustomEvent('agent:binding',{detail:{refs:[ref]}})),evidence.data.subject_ref);
    await page.getByRole('button',{name:'搜索保存材料',exact:true}).click();
    const input=page.getByRole('searchbox',{name:'已保存材料关键词'});
    await input.fill('原文段落');await page.getByRole('button',{name:'搜索保存材料',exact:true}).last().click();
    await page.getByRole('button',{name:'下一页搜索结果',exact:true}).waitFor();
    assert.match(await page.locator('.agent-review-body').innerText(),/相关性不代表真实性/);
    assert.match(await page.locator('.agent-review-body').innerText(),/原文段落/);
    assert.match(await page.locator('.agent-review-body').innerText(),/披露.*取得.*解析可用/);
    await page.getByRole('button',{name:'下一页搜索结果',exact:true}).click();await page.getByRole('button',{name:'上一页搜索结果',exact:true}).waitFor();
    await input.fill('<img');await page.getByRole('button',{name:'搜索保存材料',exact:true}).last().click();
    await page.getByText('<img src=x onerror=alert(1)>',{exact:true}).first().waitFor();
    assert.equal(await page.locator('.agent-review-body img,.agent-review-body script,.agent-review-body iframe').count(),0);
    await page.getByText('查看相邻原文与校验信息',{exact:true}).click();
    assert.match(await page.locator('.agent-review-body').innerText(),/片段校验 [a-f0-9]{64}/);
    await page.locator(`[data-ref-id="${fixture.fact.id}"]`).count();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);assert.doesNotMatch(await page.locator('body').innerText(),/HTTP 500/);
    await page.screenshot({path:`/tmp/ag27-search-${viewport.width}.png`,fullPage:true});await context.close();
  }
  console.log('AG27 desktop/mobile real saved manifest HTTP search, pagination, exact adjacent source, safe DOM passed');
}finally{await browser?.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));await rm(temp,{recursive:true,force:true});}
