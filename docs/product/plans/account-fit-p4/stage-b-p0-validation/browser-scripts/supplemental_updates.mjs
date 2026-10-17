// P4 real API/worker view at desktop and mobile widths, no paid execution.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = 19885, base = `http://127.0.0.1:${port}`;
const workdir = await mkdtemp(join(tmpdir(), 'p4-updates-'));
const probeDir = resolve('docs/product/probes/account-fit-p4-p0/account-fit-p4');
await mkdir(probeDir, {recursive: true});
const server = spawn('.venv/bin/python', ['tests/browser/supplemental_updates_fixture_server.py'], {
  cwd: resolve('.'), env: {...process.env, P4_FIXTURE_DIR: workdir, P4_FIXTURE_PORT: String(port)},
  stdio: ['ignore', 'pipe', 'pipe']});
let log = '';
server.stdout.on('data', c => {log += c;}); server.stderr.on('data', c => {log += c;});
let browser;
const key = {'X-API-Key': 'p4-local-browser-inert-key-2026'};
async function api(path, method='GET') {
  const r = await fetch(base+path, {method, headers: key});
  assert.ok(r.ok, `${path} HTTP ${r.status} ${await r.clone().text()}`); return r.json();
}
try {
  let ready = false;
  for (let i=0; i<180; i++) {
    if (server.exitCode !== null) throw new Error(log);
    try { if ((await fetch(base+'/healthz')).ok) {ready=true; break;} } catch {}
    await new Promise(r => setTimeout(r,100));
  }
  assert.ok(ready, log);
  const drained = await api('/fixture/drain','POST');
  assert.ok(drained.completed >= 1, 'real worker must claim the durable intake');
  browser = await chromium.launch({headless:true,executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  const errors=[];
  for (const width of [1440,390]) {
    const context = await browser.newContext({viewport:{width,height:1100}});
    const page = await context.newPage(); page.on('pageerror', e=>errors.push(String(e)));
    await page.goto(base+'/app/login');
    await page.locator('[name=api_key]').fill('p4-local-browser-inert-key-2026');
    await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.goto(base+'/app/stocks/600901.SH');
    const panel = page.locator('#supplemental-update-content');
    await panel.getByText('持续更新与复核').waitFor();
    await panel.getByText('来源 controlled_local_v1').waitFor();
    assert.match(await panel.innerText(),/仅本次声明的已保存资料/);
    assert.match(await panel.innerText(),/没有采集到不表示没有风险/);
    assert.match(await panel.innerText(),/待处理成员：0/);
    assert.doesNotMatch(await panel.innerText(),/建议买入/);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),`overflow ${width}`);
    await page.screenshot({path:join(probeDir,`local-${width}.png`),fullPage:true});
    await page.goto(base+'/app/stocks/600902.SH');
    await panel.getByText(/来源覆盖：尚未声明/).waitFor();
    assert.doesNotMatch(await panel.innerText(),/controlled_local_v1/,'subject switch must not retain source');
    await page.screenshot({path:join(probeDir,`uncovered-${width}.png`),fullPage:true});
    await context.close();
  }
  await api('/fixture/fail-source','POST');
  for (const width of [1440,390]) {
    const context = await browser.newContext({viewport:{width,height:1100}});
    const page = await context.newPage();
    await page.goto(base+'/app/login'); await page.locator('[name=api_key]').fill('p4-local-browser-inert-key-2026');
    await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.goto(base+'/app/stocks/600901.SH');
    const panel=page.locator('#supplemental-update-content');
    await panel.getByText(/来源失败原因：已保存来源不完整或存在冲突/).waitFor();
    assert.match(await panel.innerText(),/最后成功/);
    assert.match(await panel.innerText(),/没有采集到不表示没有风险/);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.screenshot({path:join(probeDir,`source-failed-${width}.png`),fullPage:true});
    await context.close();
  }
  await api('/fixture/close','POST');
  const context=await browser.newContext({viewport:{width:390,height:1100}});
  const page=await context.newPage();
  await page.goto(base+'/app/login'); await page.locator('[name=api_key]').fill('p4-local-browser-inert-key-2026');
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.goto(base+'/app/stocks/600901.SH');
  await page.locator('#supplemental-update-content').getByText(/持续更新当前关闭/).waitFor();
  await page.locator('#supplemental-update-content').getByText('来源 controlled_local_v1').waitFor();
  await page.screenshot({path:join(probeDir,'closed-390.png'),fullPage:true});
  await context.close();
  assert.deepEqual(errors,[]);
  console.log('P4 real intake/worker/browser 1440/390 passed');
} finally {
  if (browser) await browser.close(); server.kill('SIGTERM');
  await new Promise(r=>{server.once('exit',r);setTimeout(r,2000);});
  await rm(workdir,{recursive:true,force:true});
  if (server.exitCode && server.exitCode!==0) console.error(log);
}
