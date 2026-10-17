// AF-P4-07: real loopback HTTP, SQL and worker; no official source/provider.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
const require=createRequire(process.env.AG17_NODE_MODULES+'/package.json');
const {chromium}=require('playwright');
const port=19886, base=`http://127.0.0.1:${port}`;
const workdir=await mkdtemp(join(tmpdir(),'p4-b-browser-'));
const probes=resolve('docs/product/probes/account-fit-p4-p0/account-fit-p4-b');
await mkdir(probes,{recursive:true});
const key={'X-API-Key':'p4-b-local-browser-fixture-only-key'};
const server=spawn('.venv/bin/python',['tests/browser/disclosure_updates_fixture_server.py'],{
  cwd:resolve('.'),env:{...process.env,P4B_FIXTURE_DIR:workdir,P4B_FIXTURE_PORT:String(port)},
  stdio:['ignore','pipe','pipe']});
let log='',browser,succeeded=false;
server.stdout.on('data',x=>{log+=x;}); server.stderr.on('data',x=>{log+=x;});
async function api(path,method='GET',body){
  const r=await fetch(base+path,{method,headers:{...key,...(body?{'Content-Type':'application/json'}:{})},
    ...(body?{body:JSON.stringify(body)}:{})});
  assert.ok(r.ok,`${path}: ${r.status} ${await r.clone().text()}`);return r.json();
}
const errors=[];
async function openStock(width){
  const context=await browser.newContext({viewport:{width,height:1100}});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/app/login');await page.locator('[name=api_key]').fill(key['X-API-Key']);
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.goto(base+'/app/stocks/300901.SZ');
  const panel=page.locator('#supplemental-update-content');
  await panel.getByText('来源 cninfo_szse_v1').waitFor();
  assert.match(await panel.innerText(),/没有采集到不表示没有风险/);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),`overflow ${width}`);
  return {context,page,panel};
}
try{
  let ready=false;
  for(let i=0;i<180;i++){
    if(server.exitCode!==null)throw new Error(log);
    try{if((await fetch(base+'/healthz')).ok){ready=true;break;}}catch{}
    await new Promise(r=>setTimeout(r,100));
  }
  assert.ok(ready,log);
  const info=await api('/fixture/info');
  const {authorization:grant}=await api('/supplemental-updates/sources/cninfo_szse_v1/scopes','POST',info.grant_command);
  const poll=await api('/supplemental-updates/polls','POST',{
    operation_id:randomUUID(),authorization_id:grant.authorization_id,
    authorization_revision:grant.revision,authorization_hash:grant.authorization_hash,
    listing_ref:info.listing_ref,window_start:info.window_start,window_end:info.window_end,
    expected_source_revision:0});
  let status;
  for(let step=0;step<20;step++){
    await api('/fixture/drain','POST');status=await api(`/supplemental-updates/scopes/${info.listing_id}`);
    if(status.sources[0]?.failure_code)break;
  }
  assert.match(status.sources[0]?.failure_code||'',/429|rate_limit/);
  browser=await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  for(const width of [1440,390]){
    const {context,page,panel}=await openStock(width);assert.match(await panel.innerText(),/429|限流/);
    await page.screenshot({path:join(probes,`rate-limited-${width}.png`),fullPage:true});await context.close();
  }
  await api('/fixture/normal','POST');
  console.log('B local worker drain',JSON.stringify(await api('/fixture/drain?wait=true','POST')));
  status=await api(`/supplemental-updates/scopes/${info.listing_id}`);
  assert.ok(['completed','complete'].includes(status.sources[0]?.poll_state),JSON.stringify(status));
  assert.ok(status.sources[0].index_watermark);assert.ok(status.sources[0].original_watermark);
  assert.equal(status.sources[0].bridge_pending,0);assert.equal(status.sources[0].protocol_profile,'fixture');
  assert.equal(status.sources[0].current_status,'unknown');
  const stats=await api('/fixture/stats');assert.equal(stats.agent_tasks,0);assert.equal(stats.events,1);
  assert.ok(stats.http_calls>=5);console.log('B fixture stats',JSON.stringify(stats),'poll',poll.poll_id);
  for(const width of [1440,390]){
    const {context,page,panel}=await openStock(width);assert.match(await panel.innerText(),/索引|目录/);
    assert.match(await panel.innerText(),/原件/);
    assert.match(await panel.innerText(),/本轮进度：本轮完成/);
    await page.screenshot({path:join(probes,`completed-fixture-${width}.png`),fullPage:true});
    await page.goto(base+'/app/stocks/300902.SZ');await panel.getByText(/来源覆盖：尚未声明/).waitFor();
    assert.doesNotMatch(await panel.innerText(),/来源 cninfo_szse_v1/);await context.close();
  }
  await api('/fixture/close','POST');const {context,page,panel}=await openStock(390);
  assert.match(await panel.innerText(),/持续更新当前关闭/);
  await page.screenshot({path:join(probes,'closed-390.png'),fullPage:true});await context.close();
  assert.deepEqual(errors,[]);
  console.log('AF-P4-07 real loopback HTTP/worker browser 1440/390 passed; AF-P4-08 not executed');
  succeeded=true;
}finally{
  if(browser)await browser.close();server.kill('SIGTERM');
  await new Promise(r=>{server.once('exit',r);setTimeout(r,2000);});await rm(workdir,{recursive:true,force:true});
  if(!succeeded||(server.exitCode&&server.exitCode!==0))console.error(log);
}
