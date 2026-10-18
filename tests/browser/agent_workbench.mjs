// Real Chromium against an isolated FastAPI/SQLite fixture; no provider calls.
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const { chromium } = require('playwright');
const temp = await mkdtemp(join(tmpdir(), 'ag17-browser-'));
const port = 19847;
const base = `http://127.0.0.1:${port}`;
const server = spawn('.venv/bin/python', ['tests/browser/agent_fixture_server.py'], {
  env: {...process.env, AG17_FIXTURE_DIR:temp, AG17_FIXTURE_PORT:String(port)}, stdio:['ignore','pipe','pipe']
});
let logs=''; server.stdout.on('data', b => logs+=b); server.stderr.on('data', b => logs+=b);
let browser;
async function pane(page, name) {
  const tab = page.locator(`[data-agent-pane="${name}"]`);
  if (await tab.isVisible()) await tab.click();
}
async function clickNew(page) { await pane(page, 'list'); await page.locator('#agent-new').click(); }
async function clickConversation(page, selector) { await pane(page, 'list'); await page.locator(selector).click(); }
const headers = {'X-API-Key':'ag17-fixture-key'};
async function ready() {
  for (let i=0;i<100;i++) {
    if (server.exitCode !== null) throw new Error(logs);
    try { if ((await fetch(base+'/healthz')).ok) return; } catch {}
    await new Promise(r => setTimeout(r,100));
  }
  throw new Error('fixture startup timeout '+logs);
}
async function api(path, body) {
  const response = await fetch(base+path, {method:body ? 'POST':'GET',headers:{...headers,'Content-Type':'application/json'},body:body ? JSON.stringify(body):undefined});
  assert.ok(response.ok, `${path} ${response.status} ${await response.clone().text()}`);
  return response.json();
}
try {
  await ready(); browser = await chromium.launch({headless:true, executablePath:process.env.AG17_CHROMIUM_EXECUTABLE});
  for (const viewport of [{width:1440,height:1000},{width:390,height:844}]) {
    const context = await browser.newContext({viewport});
    // Mobile HTTP/WebViews may expose getRandomValues without randomUUID.
    // Exercise sending, cancellation and exact retries through that fallback.
    if (viewport.width === 390) await context.addInitScript(() => {
      Object.defineProperty(globalThis.crypto, 'randomUUID', {value: undefined, configurable: true});
    });
    const page = await context.newPage(); page.setDefaultTimeout(+(process.env.AG_TIMEOUT||30000)); const errors=[];
    page.on('pageerror', error => errors.push(String(error))); page.on('dialog', () => {throw new Error('XSS dialog');});
    await page.goto(base+'/app/agent'); assert.match(page.url(), /login/);
    await page.locator('[name=api_key]').fill('ag17-fixture-key');
    await page.getByRole('button',{name:'登录',exact:true}).click();
    await page.goto(base+'/app/agent?ticker=600002.SH');
    await page.getByRole('button', {name:'fixture 同名 <img src=x onerror=alert(1)> · 600002.SH',exact:true}).click();
    await clickNew(page);
    await page.waitForFunction(() => !document.querySelector('#agent-send').disabled);
    assert.match(await page.locator('#agent-binding').innerText(), /600002.SH/);
    await page.locator('#agent-input').fill('草稿一 <script>alert(1)</script>');
    await page.reload(); await page.waitForFunction(() => document.querySelector('#agent-input').value.startsWith('草稿一'));
    let lostBody;
    await page.route('**/agent/conversations/*/messages', async route => {
      if (route.request().method() !== 'POST' || lostBody) return route.continue();
      lostBody=route.request().postDataJSON(); await route.fetch(); await route.abort();
    });
    await page.locator('#agent-send').click();
    await page.waitForFunction(() => !document.querySelector('#agent-retry').hidden && !document.querySelector('#agent-retry').disabled);
    assert.equal(lostBody.context_refs[0].id,78); assert.equal(lostBody.research_kind,'auto');
    assert.match(lostBody.client_message_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    let retryBody;
    await page.unroute('**/agent/conversations/*/messages');
    await page.route('**/agent/conversations/*/messages', async route => {
      if (route.request().method()==='POST') retryBody=route.request().postDataJSON();
      await route.continue();
    });
    await page.locator('#agent-retry').click();
    await page.waitForFunction(() => document.querySelector('#agent-retry').hidden);
    assert.deepEqual(retryBody,lostBody);
    await page.unroute('**/agent/conversations/*/messages');
    const list = await api('/agent/conversations');
    const all = await Promise.all(list.map(c => api(`/agent/conversations/${c.id}/messages`)));
    const sent = all.flat().find(m => m.client_message_id===lostBody.client_message_id);
    assert.ok(sent); assert.equal(all.flat().filter(m=>m.client_message_id===lostBody.client_message_id).length,1);
    await api('/fixture/run',{});
    await page.waitForFunction(() => document.querySelector('#agent-messages').innerText.includes('确定性公开回答'), null,{timeout:15000});
    assert.equal(await page.locator('#agent-messages img, #agent-messages script').count(),0);
    await page.locator('#agent-input').fill('原会话草稿');
    await clickNew(page); await page.waitForFunction(() => !document.querySelector('#agent-new').disabled && document.querySelector('#agent-input').value==='');
    await page.locator('#agent-input').fill('另一会话草稿');
    await pane(page, 'list'); await page.locator('#agent-conversations button').last().click();
    // Explicit conversation route restores its own persisted draft, independent of list ordering.
    await page.goto(base+`/app/agent?conversation=${sent.conversation_ref.id}`);
    await page.waitForFunction(() => document.querySelector('#agent-input').value==='原会话草稿');
    await page.locator('#agent-input').fill('可取消请求'); await page.locator('#agent-send').click();
    await page.waitForFunction(() => document.querySelectorAll('#agent-messages button:not(:disabled)').length>0);
    const cancellationMessages=await api(`/agent/conversations/${sent.conversation_ref.id}/messages`);
    const cancellingTask=cancellationMessages.at(-1).task_ref.id;
    await api(`/fixture/claim/${cancellingTask}`,{});
    await page.waitForFunction(() => document.querySelector('#agent-messages').innerText.includes('进行中'));
    // A real committed artifact event updates the rail before any state change,
    // without reload or a terminal assistant message (the SSE browser path).
    await api(`/fixture/save-progress/${cancellingTask}`,{});
    await page.locator('.agent-conversation-progress').filter({hasText:'运行中保存的核查'}).waitFor({state:'attached'});
    if(!await page.locator('#agent-artifact-panel').isVisible()){
      if(await page.locator('#agent-drawer-toggle').isVisible())await page.locator('#agent-drawer-toggle').click();
      else await pane(page,'drawer');
    }
    await page.locator('.agent-conversation-progress').filter({hasText:'运行中保存的核查'}).waitFor();
    assert.equal((await api(`/agent/tasks/${cancellingTask}`)).status,'running');
    if(await page.locator('#agent-artifact-panel [data-agent-go="chat"]').isVisible())await page.locator('#agent-artifact-panel [data-agent-go="chat"]').click();
    else if(await page.locator('#agent-drawer-toggle').getAttribute('aria-expanded')==='true')await page.locator('#agent-drawer-toggle').click();
    await page.locator('#agent-messages button:not(:disabled)').click();
    await page.waitForFunction(() => document.querySelector('#agent-messages').innerText.includes('正在停止'));
    assert.equal((await api(`/agent/tasks/${cancellingTask}`)).status,'running');
    await api(`/fixture/ack/${cancellingTask}`,{});
    await page.waitForFunction(() => document.querySelector('#agent-messages').innerText.includes('已取消'));
    // Force HTTP410 then stream errors; browser must reload snapshot and fall back to real poll.
    await page.locator('#agent-input').fill('恢复连接请求'); await page.locator('#agent-send').click();
    await page.waitForFunction(() => document.querySelectorAll('#agent-messages button:not(:disabled)').length>0);
    let expired=false, failed=0, polls=0;
    await page.route('**/agent/tasks/*/events?*',async route=>{
      if (route.request().url().includes('transport=poll')) {polls++; return route.continue();}
      if (!expired) {expired=true; const taskId=route.request().url().match(/tasks\/([^/]+)\/events/)[1]; await api(`/fixture/expire/${taskId}`,{}); const response=await route.fetch(); assert.equal(response.status(),410); return route.fulfill({response});}
      failed++; return route.abort();
    });
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#agent-notice').innerText.includes('轮询'),null,{timeout:15000});
    await api('/fixture/run',{});
    await page.waitForFunction(() => document.querySelector('#agent-input').value==='');
    assert.ok(expired && failed>=3 && polls>=1);
    await page.unroute('**/agent/tasks/*/events?*');
    // Adding comparison objects keeps explicit canonical refs and deduplicates exact selections.
    await page.locator('#agent-add-object').click();
    await page.locator('#agent-search').fill('600001.SH');
    await page.locator('#agent-subject-form button[type=submit]').click();
    await page.getByRole('button',{name:'fixture 同名 <img src=x onerror=alert(1)> · 600001.SH',exact:true}).click();
    // Picking the same exact object again must not duplicate the reference.
    await page.locator('#agent-add-object').click();
    await page.locator('#agent-search').fill('600001.SH');
    await page.locator('#agent-subject-form button[type=submit]').click();
    await page.getByRole('button',{name:'fixture 同名 <img src=x onerror=alert(1)> · 600001.SH',exact:true}).click();
    // Two explicit objects switch the mode to comparison on their own; choosing it explicitly stays valid.
    assert.equal(await page.locator('#agent-kind').inputValue(),'comparison');
    await page.locator('#agent-kind').selectOption('comparison');
    await page.locator('#agent-input').fill('只比较两个明确对象');
    let compared;
    await page.route('**/agent/conversations/*/messages', async route => {
      if (route.request().method()==='POST') compared=route.request().postDataJSON();
      await route.continue();
    });
    await page.locator('#agent-send').click();
    await page.waitForFunction(() => document.querySelector('#agent-input').value==='');
    assert.deepEqual(compared.context_refs.map(r=>r.id).sort(),[77,78]);
    await page.unroute('**/agent/conversations/*/messages');
    const comparisonSaved=await api(`/agent/conversations/${sent.conversation_ref.id}/messages`);
    const waitTask=comparisonSaved.at(-1).task_ref.id;
    await api(`/fixture/claim/${waitTask}`,{}); await api(`/fixture/wait/${waitTask}`,{});
    await page.waitForFunction(() => document.querySelector('#agent-messages').innerText.includes('fixture 等待材料'));
    await page.locator(`#agent-messages [data-task-id="${waitTask}"] button`).click();
    await page.waitForFunction(id => document.querySelector(`#agent-messages [data-task-id="${id}"]`).innerText.includes('已取消'),waitTask);
    // Persist an unacknowledged body even if no server receipt exists; reload and retry unchanged.
    await page.locator('#agent-input').fill('重载后重取原请求'); let absentBody;
    await page.route('**/agent/conversations/*/messages',async route=>{
      if(route.request().method()==='POST') {absentBody=route.request().postDataJSON(); return route.abort();}
      return route.continue();
    });
    await page.locator('#agent-send').click();
    await page.waitForFunction(()=>!document.querySelector('#agent-retry').hidden && !document.querySelector('#agent-retry').disabled);
    await page.unroute('**/agent/conversations/*/messages'); await page.reload();
    await page.waitForFunction(()=>!document.querySelector('#agent-retry').hidden);
    let restoredBody;
    await page.route('**/agent/conversations/*/messages',async route=>{
      if(route.request().method()==='POST') restoredBody=route.request().postDataJSON();
      return route.continue();
    });
    await page.locator('#agent-retry').click();
    await page.waitForFunction(()=>document.querySelector('#agent-retry').hidden);
    assert.deepEqual(restoredBody,absentBody); await page.unroute('**/agent/conversations/*/messages');
    // Messages read before terminal snapshot: the latter must merge the persisted assistant answer.
    let raced=false;
    await page.route('**/agent/conversations/*/messages?*',async route=>{
      if(raced) return route.continue(); raced=true;
      const response=await route.fetch(); await api('/fixture/run',{}); return route.fulfill({response});
    });
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#agent-messages').innerText.includes('重载后重取原请求'));
    const racedMessages=await api(`/agent/conversations/${sent.conversation_ref.id}/messages`);
    const finalId=racedMessages.at(-1).id;
    await page.waitForFunction(id=>!!document.querySelector(`[data-message-id="${id}"]`),finalId);
    await page.unroute('**/agent/conversations/*/messages?*');
    assert.equal((await page.locator('body').innerText()).includes('private draft'),false);
    // Late results for an old conversation cannot enter the new conversation.
    await clickNew(page);
    await page.waitForFunction(()=>!document.querySelector('#agent-new').disabled);
    const newerId=new URL(page.url()).searchParams.get('conversation');
    let release; const gate=new Promise(r=>release=r); let requested=false;
    await page.route(`**/agent/conversations/${sent.conversation_ref.id}/messages?*`,async route=>{
      requested=true; const response=await route.fetch(); await gate; await route.fulfill({response});
    });
    await clickConversation(page, `[data-conversation-id="${sent.conversation_ref.id}"]`);
    for(let i=0;i<50&&!requested;i++) await new Promise(r=>setTimeout(r,10));
    assert.ok(requested);
    await clickConversation(page, `[data-conversation-id="${newerId}"]`); release();
    await page.waitForFunction(()=>document.querySelector('#agent-notice').innerText.includes('已恢复会话'));
    await new Promise(r=>setTimeout(r,100));
    assert.equal(await page.locator('#agent-messages article').count(),0);
    await page.unroute(`**/agent/conversations/${sent.conversation_ref.id}/messages?*`);
    const cookies=await context.cookies();
    const badCsrf=await fetch(base+'/agent/conversations',{method:'POST',headers:{Cookie:cookies.map(c=>`${c.name}=${c.value}`).join('; '),Origin:'https://evil.example','Content-Type':'application/json'},body:'{}'});
    assert.equal(badCsrf.status,403);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth>innerWidth),false);
    const csrf = await page.evaluate(async () => (await fetch('/agent/conversations',{method:'POST',headers:{'Content-Type':'application/json','Origin':'https://evil.example'},body:'{}'})).status);
    // Browsers forbid overriding Origin; same-origin mutation succeeds, explicit foreign Origin is tested by HTTP below.
    assert.equal(csrf,201);
    await page.screenshot({path:`/tmp/ag17-workbench-${viewport.width}.png`,fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(`AG17 real browser ${viewport.width}px: selection, drafts, lost ack/retry, terminal answer, switching, cancel, HTTP410/SSE/poll, XSS, layout passed`);
    await context.close();
  }
  const bad = await fetch(base+'/agent/conversations',{method:'POST',headers:{Origin:'https://evil.example','Content-Type':'application/json'},body:'{}'});
  assert.equal(bad.status,401);
} finally {
  if (browser) await browser.close(); server.kill('SIGTERM');
  await new Promise(r => server.exitCode!==null ? r() : server.once('exit',r));
  await rm(temp,{recursive:true,force:true});
}
