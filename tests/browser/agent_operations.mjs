// Real isolated settings/API writes; no external request or fake success routes.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const temp=await mkdtemp(join(tmpdir(),'ag28-browser-')),port=19871,base=`http://127.0.0.1:${port}`;
const server=spawn('.venv/bin/python',['tests/browser/agent_fixture_server.py'],{env:{...process.env,AG19_FIXTURE:'true',AG17_FIXTURE_DIR:temp,AG17_FIXTURE_PORT:String(port)},stdio:['ignore','pipe','pipe']});
let logs='',browser;server.stdout.on('data',b=>logs+=b);server.stderr.on('data',b=>logs+=b);
const headers={'X-API-Key':'ag17-fixture-key','Content-Type':'application/json'};
async function api(path){const response=await fetch(base+path,{headers});assert.ok(response.ok,(await response.clone().text())+logs);return response.json();}
try {
  for(let i=0;i<100;i++){if(server.exitCode!==null)throw Error(logs);try{if((await fetch(base+'/healthz')).ok)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  browser=await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
    // Return to default off through actual authenticated PATCH before this viewport.
    const disable=await fetch(base+'/settings',{method:'PATCH',headers,body:JSON.stringify({values:{notification_external_enabled:false}})});
    assert.ok(disable.ok);
    const context=await browser.newContext({viewport}),page=await context.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(base+'/app/settings');await page.locator('[name=api_key]').fill('ag17-fixture-key');await page.getByRole('button',{name:'登录',exact:true}).click();await page.goto(base+'/app/settings#notify');
    const enabled=page.locator('#setting-notification_external_enabled');await enabled.waitFor();assert.equal(await enabled.isChecked(),false);
    assert.match(await page.locator('body').innerText(),/默认只在站内提醒/);
    assert.equal((await api('/investor/operations')).external_notifications_ready,false);
    await page.locator('#setting-notification_webhook_url').fill('http://127.0.0.1/unsafe');
    await page.locator('#settings-save').click();
    await page.waitForFunction(()=>document.querySelector('#settings-save-result').textContent.includes('失败')||document.querySelector('#settings-save-result').textContent.includes('校验'));
    assert.equal((await api('/investor/operations')).external_notifications_ready,false);
    await page.locator('#setting-notification_webhook_url').fill('https://fixture.example/hook');
    await page.locator('#setting-notification_webhook_token').fill('ag28-fixture-secret');
    await enabled.check();
    await page.locator('#settings-save').click();await page.waitForFunction(()=>document.querySelector('#settings-save-result').textContent.includes('已保存'));
    assert.equal((await api('/investor/operations')).external_notifications_ready,true);
    const snapshot=JSON.stringify(await api('/settings'))+JSON.stringify(await api('/settings/history'));
    assert.ok(!snapshot.includes('ag28-fixture-secret')&&!snapshot.includes('https://fixture.example/hook'));
    assert.ok(!(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}))).includes('ag28-fixture-secret'));
    assert.equal(await page.locator('#setting-notification_webhook_token').inputValue(),'');
    await page.reload();await enabled.waitFor();assert.equal(await enabled.isChecked(),true);
    await enabled.uncheck();await page.locator('#settings-save').click();await page.waitForFunction(()=>document.querySelector('#settings-save-result').textContent.includes('已保存'));
    assert.equal((await api('/investor/operations')).external_notifications_ready,false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);await context.close();
  }
  console.log('AG28 actual settings consent/invalid URL/encrypted save/reload/disable 1440 + 390 passed');
} finally {
  if(browser)await browser.close();server.kill('SIGTERM');await new Promise(resolve=>server.once('exit',resolve));await rm(temp,{recursive:true,force:true});
}
