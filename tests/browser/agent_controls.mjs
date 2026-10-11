// AG19 uses a fresh file SQLite server per viewport; never starts against a user database.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const browser = await chromium.launch({headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE});
const headers = {'X-API-Key': 'ag17-fixture-key', 'Content-Type': 'application/json'};
try {
  for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}]) {
    const temp = await mkdtemp(join(tmpdir(), 'ag19-browser-'));
    const port = 19849, base = `http://127.0.0.1:${port}`;
    const server = spawn('.venv/bin/python', ['tests/browser/agent_fixture_server.py'], {
      env: {...process.env, AG17_FIXTURE_DIR: temp, AG17_FIXTURE_PORT: String(port), AG19_FIXTURE: 'true'},
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let logs = '';
    server.stdout.on('data', b => logs += b);
    server.stderr.on('data', b => logs += b);
    const context = await browser.newContext({viewport}), page = await context.newPage().then(p=>(p.setDefaultTimeout(+(process.env.AG_TIMEOUT||30000)),p)), errors = [];
    page.on('pageerror', e => errors.push(e.stack || String(e)));
    async function api(path, body, method) {
      const result = await fetch(base + path, {method: method || (body ? 'POST' : 'GET'), headers,
        body: body ? JSON.stringify(body) : undefined});
      assert.ok(result.ok, (await result.clone().text()) + logs);
      return result.json();
    }
    const control = page.locator('.agent-control-body');
    // Views live in the chat tab's research drawer; controls have their own tab.
    const open = async name => {
      const button = page.getByRole('button', {name, exact: true});
      await page.locator('[data-agent-tab="chat"]').click();
      const drawer = page.locator('[data-agent-pane="drawer"]'); if (await drawer.isVisible()) await drawer.click();
      if (!(await button.first().isVisible())) await page.locator('[data-agent-tab="controls"]').click();
      await button.click();
    };
    const confirmed = () => page.waitForFunction(() => document.querySelector('#agent-controls-extension [role=status]').textContent.includes('服务器已确认'));
    try {
      for (let i = 0; i < 100; i++) {
        if (server.exitCode !== null) throw Error(logs);
        try {if ((await fetch(base + '/healthz')).ok) break;} catch {}
        await new Promise(r => setTimeout(r, 100));
      }
      await page.goto(base + '/app/agent?tab=controls');
      await page.locator('[name=api_key]').fill('ag17-fixture-key');
      await page.getByRole('button', {name: '登录', exact: true}).click();
      await page.goto(base + '/app/agent?tab=controls');
      await open('启用与投资约定');
      await control.getByText('Agent 关闭；运行后台开关关闭；约定后台授权关闭。').waitFor();
      assert.match(await control.innerText(), /价格未知/);
      assert.equal((await api('/investor/readiness')).network_verified, false);
      assert.match(await control.getByLabel('尚未知晓的信息（每行一项）').inputValue(), /尚未知晓/);
      // Encrypted settings: changed fields only, legacy model unchanged, no Agent network-test imitation.
      await page.goto(base + '/app/settings');
      await page.locator('#setting-investor_agent_enabled').waitFor();
      const before = await api('/settings');
      const legacyBefore = before.groups.flatMap(g => g.items).find(x => x.key === 'llm_tier3_model').value;
      await page.locator('#setting-investor_agent_enabled').check();
      await page.locator('#setting-agent_api_key').fill('ag19-secret-do-not-store');
      let patchBody;
      page.on('request', r => {if (r.method() === 'PATCH' && r.url().endsWith('/settings')) patchBody = r.postDataJSON();});
      await page.locator('#settings-save').click();
      await page.waitForFunction(() => document.querySelector('#settings-save-result').textContent.includes('已保存'));
      assert.deepEqual(Object.keys(patchBody.values).sort(), ['agent_api_key', 'investor_agent_enabled']);
      assert.equal('expected_revision' in patchBody, false);
      const after = await api('/settings');
      assert.equal(after.groups.flatMap(g => g.items).find(x => x.key === 'llm_tier3_model').value, legacyBefore);
      assert.ok(!JSON.stringify(after).includes('ag19-secret-do-not-store'));
      assert.ok(!JSON.stringify(await api('/settings/history')).includes('ag19-secret-do-not-store'));
      assert.equal(await page.locator('#agent-settings button').filter({hasText: '测试连接'}).count(), 0);
      assert.ok(!(await page.evaluate(() => JSON.stringify({...sessionStorage, ...localStorage}))).includes('ag19-secret-do-not-store'));
      const fixture = await api('/fixture/ag18', {});
      await page.goto(base + '/app/agent?tab=controls');
      await open('启用与投资约定');
      await control.getByLabel('USD 每日共享总额（空白明确未知）').fill('2');
      await control.getByLabel('USD 单任务全生命周期上限（空白明确未知）').fill('1');
      await control.getByLabel('USD 复核日金额（空白未配置）').fill('0.8');
      await control.getByLabel('每批线索上限（0–20）').fill('20');
      await control.getByLabel('每批新任务上限（0–3）').fill('3');
      const scope = control.getByRole('group', {name: '明确交互证券范围'});
      await scope.getByLabel('证券名称或完整代码').fill('600002.SH');
      await scope.getByRole('button', {name: '查找并明确加入范围'}).click();
      await scope.getByRole('button', {name: /加入.*600002.SH/}).click();
      await open('审阅投资约定变更');
      await open('确认保存已审阅约定');
      await confirmed();
      let mandate = await api('/investor/mandate');
      assert.equal(mandate.authorization.background_enabled, false);
      assert.equal(mandate.authorization.exploration_daily_budget, null);
      assert.ok(mandate.scope_refs.some(r => r.id === 78));
      // A question is accepted while BOTH background switches are off.
      await page.locator('[data-agent-tab="chat"]').click();
      {const list = page.locator('[data-agent-pane="list"]'); if (await list.isVisible()) await list.click();}
      await page.locator('#agent-new').click();
      await page.waitForFunction(() => !document.querySelector('#agent-send').disabled);
      await page.locator('#agent-add-object').click();
      await page.locator('#agent-search').fill('600002.SH');
      await page.locator('#agent-subject-form button[type=submit]').click();
      await page.locator('#agent-search-results button').first().click();
      await page.locator('#agent-input').fill('明确交互，后台保持关闭');
      await page.locator('#agent-send').click();
      await page.waitForFunction(() => document.querySelector('#agent-messages').textContent.includes('明确交互，后台保持关闭'));
      assert.equal((await api('/investor/readiness')).runtime_background_enabled, false);
      // Select actual AG18 case and multiple actual historical opinions, then choose ONE explicit target.
      await open('公司档案');
      await page.locator(`[data-case-id="${fixture.case_id}"]`).click();
      await page.getByRole('heading', {name: '公司档案与过程复盘'}).waitFor();
      await open('并列比较保存的观点版本');
      await page.getByRole('heading', {name: '观点版本并列比较'}).waitFor();
      await open('录入决定');
      const target = control.getByLabel('本次决定的明确对象');
      await target.waitFor();
      assert.equal(await target.inputValue(), '');
      assert.equal(await target.locator('option').count(), 3);
      await control.getByLabel('你的决定原话').fill('采纳意见但没有成交 <img src=x onerror=alert(1)>');
      await open('保存决定');
      assert.match(await page.locator('#agent-controls-extension').innerText(), /请选择本次决定/);
      await target.selectOption(`recommendation:${fixture.recommendations[1].id}:1`);
      await open('保存决定');
      await confirmed();
      const decisions = await api('/investor/decisions');
      assert.equal(decisions.length, 1);
      assert.equal(decisions[0].recommendation_ref.id, fixture.recommendations[1].id);
      // Same context broadcasts preserve a form; actual changed scope invalidates its submit.
      await open('录入决定');
      await control.getByLabel('本次决定的明确对象').selectOption(`recommendation:${fixture.recommendations[0].id}:1`);
      await control.getByLabel('你的决定原话').fill('旧表单不得跟随另一家公司');
      await page.evaluate(() => document.querySelector('#agent-workbench').dispatchEvent(new CustomEvent('agent:binding', {
        detail: {conversationId: new URL(location.href).searchParams.get('conversation'), refs: [{kind: 'listing', id: 78, revision: 1}]},
      })));
      assert.ok(!(await page.locator('#agent-controls-extension [role=status]').innerText()).includes('对象已变化'));
      await page.evaluate(refs => document.querySelector('#agent-workbench').dispatchEvent(new CustomEvent('agent:control-context', {detail: {refs}})), fixture.recommendations);
      await page.locator('[data-agent-tab="chat"]').click();
      {const chatPane = page.locator('[data-agent-pane="chat"]'); if (await chatPane.isVisible()) await chatPane.click();}
      await page.locator('#agent-add-object').click();
      await page.locator('#agent-search').fill('600001.SH');
      await page.locator('#agent-subject-form button[type=submit]').click();
      await page.locator('#agent-search-results button').first().click();
      await open('保存决定');
      assert.match(await page.locator('#agent-controls-extension').innerText(), /旧表单停止提交/);
      assert.equal((await api('/investor/decisions')).length, 1);
      // Case controls are distinct and retain the pinned state.
      await open('档案控制');
      const article = control.locator('article').first();
      await article.getByLabel('本次档案操作理由').fill('仅暂停后台');
      await article.getByRole('button', {name: '暂停后台', exact: true}).click();
      await confirmed();
      let detail = await api(`/investor/cases/${fixture.case_id}`);
      assert.equal(detail.case.background_paused, true);
      assert.equal(detail.case.muted, false);
      await open('刷新档案状态');
      await control.locator('article').first().getByLabel('本次档案操作理由').fill('只关闭通知');
      await control.locator('article').first().getByRole('button', {name: '静音通知'}).click();
      await confirmed();
      detail = await api(`/investor/cases/${fixture.case_id}`);
      assert.equal(detail.case.muted, true);
      assert.equal(detail.case.background_paused, true);
      // Memory lost acknowledgement is recovered twice with the SAME immutable body, including across reload.
      for (let i = 0; i < 2; i++) {
        await open('记忆管理');
        await control.getByLabel('明确要保存的内容').fill(`显式偏好 ${i} <img src=x onerror=alert(1)>`);
        let request, dropped = false;
        await page.route('**/investor/memories', async route => {
          if (route.request().method() !== 'POST' || dropped) return route.continue();
          request = route.request().postDataJSON(); dropped = true;
          await route.fetch(); await route.abort();
        });
        await open('明确保存记忆');
        await page.getByRole('button', {name: '重取未确认操作'}).waitFor();
        await page.unroute('**/investor/memories');
        if (i === 0) await page.reload();
        const retry = page.getByRole('button', {name: '重取未确认操作'});
        assert.equal(await retry.isEnabled(), true);
        let replay;
        await page.route('**/investor/memories', route => {if (route.request().method() === 'POST') replay = route.request().postDataJSON(); return route.continue();});
        await retry.click(); await confirmed(); await page.unroute('**/investor/memories');
        assert.deepEqual(replay, request);
      }
      let memories = await api('/investor/memories'); assert.equal(memories.length, 2);
      await open('记忆管理');
      await control.locator('article').first().getByLabel('撤销理由').fill('未来不要使用');
      await control.locator('article').first().getByRole('button', {name: '撤销此记忆'}).click();
      await confirmed(); memories = await api('/investor/memories');
      assert.equal(memories.filter(m => m.status === 'revoked').length, 1);
      // Pending exact proposal, server expiry and explicit rejection are real AG14 operations.
      const parameters = {operation: 'memory_save', kind: 'explicit_preference', text: '提案偏好', scope_refs: [], source_refs: []};
      const expired = await api('/investor/proposals', {parameters, idempotency_key: randomUUID(), expires_at: new Date(Date.now() + 1500).toISOString()});
      await open('操作提案'); await control.getByText('保存显式偏好：提案偏好').waitFor();
      await new Promise(r => setTimeout(r, 1700));
      await control.getByLabel('提案决定理由').fill('批准已过期应拒绝');
      await open('批准上述确切变更');
      await page.waitForFunction(() => document.querySelector('#agent-controls-extension [role=status]').textContent.includes('版本或期限已变化'));
      assert.equal((await api(`/investor/proposals/${expired.id}`)).status, 'pending');
      await api('/investor/proposals', {parameters: {...parameters, text: '明确拒绝的偏好'}, idempotency_key: randomUUID(), expires_at: new Date(Date.now() + 60000).toISOString()});
      await open('刷新提案并重新审阅');
      const rejected = control.locator('article').filter({hasText: '明确拒绝的偏好'});
      await rejected.getByLabel('提案决定理由').fill('不同意');
      await rejected.getByRole('button', {name: '拒绝此提案'}).click(); await confirmed();
      assert.equal((await api('/investor/proposals?status=declined')).length, 1);
      // A real CAS change while the human reviews never silently adopts the new version.
      await open('启用与投资约定');
      await control.getByLabel('研究目标（每行一项）').fill('客户端审阅内容');
      await open('审阅投资约定变更');
      await control.getByRole('button', {name: '确认保存已审阅约定'}).waitFor();
      mandate = await api('/investor/mandate');
      const concurrent = {...mandate, revision: mandate.revision + 1, goals: ['并发真实修改']};
      await api('/investor/actions', {parameters: {operation: 'mandate_revision', candidate: concurrent,
        expected_revision: mandate.revision}, idempotency_key: randomUUID()});
      await open('确认保存已审阅约定');
      await page.waitForFunction(() => document.querySelector('#agent-controls-extension [role=status]').textContent.includes('版本或期限已变化'));
      assert.deepEqual((await api('/investor/mandate')).goals, ['并发真实修改']);
      assert.equal(await control.getByRole('button', {name: '确认保存已审阅约定'}).isEnabled(), false);
      await open('刷新本地配置与当前约定');
      await control.getByLabel('研究目标（每行一项）').waitFor();
      assert.equal(await control.getByLabel('研究目标（每行一项）').inputValue(), '并发真实修改');
      await page.screenshot({path: `/tmp/ag19-setup-${viewport.width}.png`, fullPage: true});
      // Failed API does not display success and does not destroy existing server records.
      await page.route('**/investor/cases?*', route => route.fulfill({status: 503, json: {detail: 'fixture 读取失败'}}));
      await open('档案控制'); await page.waitForFunction(() => document.querySelector('#agent-controls-extension [role=status]').textContent.includes('fixture 读取失败'));
      await page.unroute('**/investor/cases?*');
      assert.equal(await page.locator('.agent-control-body img').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await page.screenshot({path: `/tmp/ag19-controls-${viewport.width}.png`, fullPage: true});
      console.log(`AG19 ${viewport.width}px isolated controls passed`);
    } finally {
      await context.close(); server.kill('SIGTERM');
      if (server.exitCode === null) await new Promise(r => server.once('exit', r));
      await rm(temp, {recursive: true, force: true});
    }
  }
} finally {await browser.close();}
