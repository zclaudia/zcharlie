// Real-browser P3 closure flow: binding provenance, per-rule coverage status,
// REVIEW_REQUIRED reasons, fit composition fields, legacy markers and subject
// switching at 1440px/390px. Runs against the real publication chain served by
// research_closure_fixture_server.py.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = Number(process.env.RESEARCH_CLOSURE_FIXTURE_PORT || 19881);
const base = `http://127.0.0.1:${port}`;
const fixtureDir = await mkdtemp(join(tmpdir(), 'research-closure-p3-'));
const probeDir = resolve('docs/product/probes/account-fit-p3');
await mkdir(probeDir, {recursive: true});
const server = spawn('.venv/bin/python', ['tests/browser/research_closure_fixture_server.py'], {
  cwd: resolve('.'),
  env: {...process.env, RESEARCH_CLOSURE_FIXTURE_DIR: fixtureDir, RESEARCH_CLOSURE_FIXTURE_PORT: String(port)},
  stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', (chunk) => { serverLog += chunk; });
server.stderr.on('data', (chunk) => { serverLog += chunk; });
const browser = await chromium.launch({headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE});
const headers = {'X-API-Key': 'research-closure-synthetic-test-key-2026', 'Content-Type': 'application/json'};
const errors = [];

async function api(path, body) {
  const response = await fetch(base + path, {method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body)});
  const raw = await response.text();
  assert.ok(response.ok, `${path}: HTTP ${response.status} ${raw}`);
  return raw ? JSON.parse(raw) : null;
}

async function ready() {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (server.exitCode !== null) throw new Error(`fixture exited: ${serverLog}`);
    try { if ((await fetch(base + '/healthz')).ok) return; } catch { /* wait for bind */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`fixture server did not become healthy: ${serverLog}`);
}

async function login(page) {
  await page.goto(base + '/app/login');
  await page.locator('[name=api_key]').fill('research-closure-synthetic-test-key-2026');
  await page.getByRole('button', {name: '登录', exact: true}).click();
}

async function submitFit(page, {accountId, price, fee, rate, action = 'open'} = {}) {
  const account = page.locator('#account-fit-root select[name="account_id"]');
  await account.selectOption(accountId);
  await page.waitForFunction(() => {
    const submit = document.querySelector('#account-fit-content button[type=submit]');
    const note = document.querySelector('#account-fit-content .fit-context-note')?.textContent || '';
    return submit && !submit.disabled && (note.includes('标的参考来源：') || note.includes('账户快照、约定与标的信息已读取。'));
  }, null, {timeout: 30000});
  await page.locator('#account-fit-content select[name=action]').selectOption(action);
  for (const [name, value] of [['execution_price', price], ['fixed_fee', fee], ['fee_rate', rate]]) {
    const input = page.locator(`#account-fit-content [name=${name}]`);
    if (await input.isDisabled()) continue;
    if (value === null) await input.fill(''); else await input.fill(value);
  }
  const response = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith('/portfolio/fit-assessments'));
  await page.getByRole('button', {name: '生成账户适配检查'}).click();
  return (await response).json();
}

async function finishRun(page, body) {
  const drained = await api('/fixture/run', {});
  assert.ok(drained.completed >= 1, 'real worker must claim the admitted assessment');
  await page.waitForFunction((operationId) => {
    const key = `zc-investment-assessment-pending-fit-v1:${document.querySelector('#account-fit-root select[name=account_id]')?.value}`;
    const record = JSON.parse(sessionStorage.getItem(key) || 'null');
    const saved = JSON.parse(sessionStorage.getItem(key.replace('pending-fit', 'last-fit')) || 'null');
    return record?.operation_id === operationId || saved?.operation_id === operationId;
  }, body.operation_id, {timeout: 30000});
  await page.getByText(/财务约束检查：/).waitFor({timeout: 30000});
  return page.locator('#account-fit-content').innerText();
}

function assertNoBuyAdvice(text, where) {
  assert.doesNotMatch(text, /建议买入/, `${where} must never read as a buy recommendation`);
}

try {
  await ready();
  const chain = await api('/fixture/closure', {});
  assert.ok(chain.published_coverage_id && chain.manifest_hash && chain.task_id, `closure chain incomplete: ${JSON.stringify(chain)}`);

  const context = await browser.newContext({viewport: {width: 1440, height: 1150}});
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(String(error.stack || error)));
  page.on('console', (message) => {
    const location = message.location();
    if (message.type() === 'error' && !location.url.includes('/stocks/search?')) {
      errors.push(`console: ${message.text()} ${JSON.stringify(location)}`);
    }
  });
  await login(page);

  // --- Bound subject: provenance, per-rule coverage, REVIEW_REQUIRED reason ---
  await page.goto(`${base}/app/stocks/${chain.bound_listing}.SH`);
  const coverage = page.locator('#supplemental-coverage-content');
  await coverage.getByText('研究规则绑定与覆盖（工程展示）').waitFor({timeout: 30000});
  await coverage.getByText(/绑定：bound/).waitFor();
  assert.match(await coverage.innerText(), /规则世代 R\d+ · 事件世代 E\d+/);
  assert.match(await coverage.innerText(), /绑定规则 2 条/);
  assert.match(await coverage.innerText(), /覆盖清单 · 已发布/);
  assert.match(await coverage.innerText(), /逐规则：supplemental_rule:[0-9a-f]/);
  assert.match(await coverage.innerText(), /当前限制（REVIEW_REQUIRED 依据）：存在新增重要规则未覆盖此结论/);
  const coverageText = await coverage.innerText();
  assertNoBuyAdvice(coverageText, 'coverage panel');
  const provenance = await coverage.locator('details', {hasText: '绑定溯源哈希'}).textContent();
  assert.ok(provenance.includes(chain.rule_set_hash), 'provenance hashes must be shown');
  assert.ok(provenance.includes(chain.binding_id), 'binding identity must appear in the provenance details');
  await page.locator('#supplemental-items-content').getByText(/评估视角：company_research/).waitFor({timeout: 30000});
  assert.ok(await page.locator('#supplemental-items-content article').count() >= 1, 'P2 applications panel still renders beside the coverage panel');

  // Real fit composition: bound binding, REVIEW_REQUIRED research status.
  const accounts = await api('/portfolio/accounts');
  assert.equal(accounts.length, 1);
  const fitText = await finishRun(page, await submitFit(page, {accountId: accounts[0].id, price: '1500', fee: '10', rate: '0'}));
  assert.match(fitText, /财务约束检查：pass/);
  assert.match(fitText, /规则绑定：bound（已绑定研究规则覆盖）/);
  assert.match(fitText, /研究结论状态：REVIEW_REQUIRED（需要人工复核）/);
  assert.match(fitText, /整体状态：incomplete/);
  assert.match(fitText, /ready 只表示满足本次已检查条件，不构成买卖建议/);
  assertNoBuyAdvice(fitText, 'fit panel (bound subject)');

  await page.locator('#stock-page').scrollIntoViewIfNeeded();
  await page.screenshot({path: join(probeDir, 'synthetic-closure-bound-1440.png'), fullPage: false});

  // Synthetic projections: prepared/published distinction and disabled switch.
  const preparedProjection = await page.evaluate(async () => {
    const items = await import('/app/static/supplemental_items.js');
    const target = document.createElement('div');
    items.renderRuleCoverage(target, {
      listing_ref: {kind: 'listing', id: 600801, revision: 1},
      enabled: false,
      state: 'bound',
      reasons: [],
      manifest: {
        id: '90000000-0000-4000-8000-000000000001', stage: 'prepared', effective_cutoff: '2026-10-17T00:00:00Z',
        rule_entries: [
          {rule_ref: {kind: 'supplemental_rule', id: '20000000-0000-4000-8000-000000000009', revision: 1}, status: 'unknown', unknown_reason: 'evaluation_missing'},
          {rule_ref: {kind: 'supplemental_rule', id: '20000000-0000-4000-8000-00000000000a', revision: 1}, status: 'unbound_new'},
        ],
      },
      binding: {rules_generation: 3, events_generation: 5, rule_set: [{}, {}], rule_set_hash: 'c'.repeat(64), admission_as_of: '2026-10-17T00:00:00Z'},
      current_limits: null,
    });
    return target.innerText;
  });
  assert.match(preparedProjection, /覆盖清单 · 准备中（未发布）/);
  assert.match(preparedProjection, /补充规则链功能当前关闭/);
  assert.match(preparedProjection, /· 未知 · 原因：evaluation_missing/);
  assert.match(preparedProjection, /绑定后新增规则/);
  assert.doesNotMatch(preparedProjection, /已发布/);
  const legacyProjection = await page.evaluate(async () => {
    const items = await import('/app/static/supplemental_items.js');
    const target = document.createElement('div');
    items.renderRuleCoverage(target, {
      listing_ref: {kind: 'listing', id: 600802, revision: 1},
      enabled: true, state: 'unknown', reasons: ['rule_binding_missing'],
      manifest: null, binding: null, current_limits: null,
    });
    return target.innerText;
  });
  assert.match(legacyProjection, /legacy 标记：未覆盖新增规则/);
  assert.match(legacyProjection, /不回填、不追认/);

  // Synthetic fit rows for the buckets this environment cannot stage live.
  const fitProjections = await page.evaluate(async () => {
    const fit = await import('/app/static/account_fit.js');
    const region = document.createElement('div');
    fit.renderFitView(region, {payload: {
      financial_status: 'pass', ranking_bucket: 'limited', supplemental_binding: 'bound',
      research_status: 'current', company_alignment: 'restrictive',
      coverage_ref: {kind: 'rule_coverage', id: '90000000-0000-4000-8000-000000000002', revision: 1},
      rules_generation: 2, events_generation: 4,
    }}, 'basis');
    const limited = region.innerText;
    const readyRegion = document.createElement('div');
    fit.renderFitView(readyRegion, {payload: {
      financial_status: 'pass', ranking_bucket: 'ready', supplemental_binding: 'bound',
      research_status: 'current', company_alignment: 'supportive',
    }}, 'basis');
    return {limited, ready: readyRegion.innerText};
  });
  assert.match(fitProjections.limited, /整体状态：limited（存在非阻断限制）/);
  assert.match(fitProjections.limited, /公司论据对齐：restrictive（限制性）/);
  assertNoBuyAdvice(fitProjections.limited, 'synthetic limited fit');
  assert.match(fitProjections.ready, /整体状态：ready（满足本次已检查条件；不构成买卖建议）/);
  assertNoBuyAdvice(fitProjections.ready, 'synthetic ready fit');

  // --- Subject switch: legacy subject shows the marker, no bound residue ---
  await page.goto(`${base}/app/stocks/${chain.legacy_listing}.SH`);
  const legacyCoverage = page.locator('#supplemental-coverage-content');
  await legacyCoverage.getByText(/legacy 标记：未覆盖新增规则/).waitFor({timeout: 30000});
  const legacyText = await page.locator('body').innerText();
  assert.doesNotMatch(legacyText, /绑定：bound/, 'legacy subject must not inherit the bound marker');
  assert.ok(!legacyText.includes(chain.manifest_hash), 'legacy page must not show the other subject manifest hash');
  assertNoBuyAdvice(legacyText, 'legacy stock page');

  const legacyFit = await finishRun(page, await submitFit(page, {accountId: accounts[0].id, price: null, fee: null, rate: null}));
  assert.match(legacyFit, /规则绑定：unknown（绑定状态未知）/);
  assert.match(legacyFit, /未知原因：无研究规则绑定（结论早于规则体系）/);
  assertNoBuyAdvice(legacyFit, 'fit panel (legacy subject)');

  // --- Mobile pass: both subjects, no horizontal overflow ---
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 980});
    await page.goto(`${base}/app/stocks/${chain.bound_listing}.SH`);
    await page.locator('#supplemental-coverage-content').getByText(/绑定：bound/).waitFor({timeout: 30000});
    const historySummary = page.locator('#account-fit-content details.fit-saved-result > summary');
    if (await historySummary.count()) await historySummary.first().click();
    await page.getByText(/财务约束检查：/).waitFor({timeout: 30000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, `bound stock page must not overflow at ${width}px`);
    await page.locator('#account-fit-root').scrollIntoViewIfNeeded();
    await page.screenshot({path: join(probeDir, `synthetic-closure-revisit-bound-${width}.png`), fullPage: false});
    await page.goto(`${base}/app/stocks/${chain.legacy_listing}.SH`);
    await page.locator('#supplemental-coverage-content').getByText(/legacy 标记：未覆盖新增规则/).waitFor({timeout: 30000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, `legacy stock page must not overflow at ${width}px`);
    await page.screenshot({path: join(probeDir, `synthetic-closure-legacy-${width}.png`), fullPage: false});
  }
  assert.equal(errors.length, 0, `unexpected page errors: ${errors.join('\n')}`);
  await context.close();
  console.log('P3 research-closure stock flow passed (1440px/390px).');
} catch (error) {
  console.error(error);
  try {
    const current = browser.contexts()[0]?.pages()[0];
    if (current) {
      console.error('URL:', current.url());
      for (const selector of ['#supplemental-coverage-content', '#account-fit-content', '#supplemental-items-content']) {
        console.error(selector, await current.locator(selector).innerText().catch((e) => String(e)));
      }
    }
  } catch { /* diagnostics only */ }
  console.error('page errors:', errors);
  console.error(serverLog);
  process.exitCode = 1;
} finally {
  await browser.close().catch(() => {});
  server.kill('SIGTERM');
  await new Promise((resolve) => server.once('exit', resolve));
  await rm(fixtureDir, {recursive: true, force: true});
}
