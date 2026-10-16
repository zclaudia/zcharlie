// Synthetic P1 account-fit browser flow: real HTTP, temporary SQLite and the
// production ResearchWorker handlers. No live data providers or production DB.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = Number(process.env.AF11_FIXTURE_PORT || 19866);
const base = `http://127.0.0.1:${port}`;
const fixtureDir = await mkdtemp(join(tmpdir(), 'account-fit-p1-synthetic-'));
const server = spawn('.venv/bin/python', ['tests/browser/account_fit_fixture_server.py'], {
  cwd: resolve('.'),
  env: {...process.env, AF11_FIXTURE_DIR: fixtureDir, AF11_FIXTURE_PORT: String(port)},
  stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', (chunk) => { serverLog += chunk; });
server.stderr.on('data', (chunk) => { serverLog += chunk; });
const browser = await chromium.launch({headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE});
const apiHeaders = {'X-API-Key': 'account-fit-synthetic-test-key-2026', 'Content-Type': 'application/json'};

async function api(path, body) {
  const response = await fetch(base + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: apiHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.clone().text();
  assert.ok(response.ok, `${path}: HTTP ${response.status} ${text}`);
  return text ? JSON.parse(text) : null;
}

async function setMandateAccount(accountId) {
  const current = await api('/investor/mandate');
  const candidate = {
    ...current,
    revision: current.revision + 1,
    portfolio_constraints: {
      ...(current.portfolio_constraints || {}),
      account_id: accountId,
      currency: 'CNY',
      max_single_weight: '0.1',
    },
  };
  return api('/investor/mandate/revisions', {
    candidate,
    mutation: {expected_revision: current.revision, idempotency_key: crypto.randomUUID()},
  });
}

async function seedPurposeChoices(accountId) {
  const snapshot = await api(`/portfolio/snapshot?account_id=${accountId}`);
  const requiredOn = new Date().toISOString().slice(0, 10);
  const purpose = (name) => ({
    id: crypto.randomUUID(), name, amount: '1000', available_budget: '1000',
    horizon: 'long_term', required_cash: '0', required_on: requiredOn,
    source: 'synthetic browser fixture', source_version: 'v1', unknowns: [],
  });
  await api('/portfolio/purposes', {
    account_id: accountId, idempotency_key: crypto.randomUUID(), expected_revision: 0,
    snapshot_ref: snapshot.reference, purposes: [purpose('合成用途甲'), purpose('合成用途乙')],
    confirmation: 'user_confirmed',
  });
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
  await page.locator('[name=api_key]').fill('account-fit-synthetic-test-key-2026');
  await page.getByRole('button', {name: '登录', exact: true}).click();
}

function fitPost(page) {
  return page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/portfolio/fit-assessments'));
}

async function submitRanking(page, accountId) {
  await page.locator('#candidate-ranking-content [name=account_id]').selectOption(accountId);
  await page.waitForFunction(() => {
    const submit = document.querySelector('#candidate-ranking-content button[type=submit]');
    return submit && !submit.disabled;
  });
  await page.locator('#candidate-ranking-content [name=price_confirmation]').check();
  const requestPromise = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith('/candidates/rankings'));
  const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/candidates/rankings'));
  await page.getByRole('button', {name: '生成账户候选排序'}).click();
  const request = await requestPromise;
  const response = await responsePromise;
  assert.equal(response.status(), 202, await response.text());
  const body = request.postDataJSON();
  const pending = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`)), accountId);
  assert.deepEqual(pending.body, body, 'complete ranking request must be durable before send');
  assert.equal(request.headers()['idempotency-key'], undefined);
  return {body, receipt: await response.json()};
}

async function submitFit(page, {accountId, price, fee, rate, action = 'open'}, {doubleClick = false} = {}) {
  const account = page.locator('#account-fit-root select[name="account_id"]');
  await account.selectOption(accountId);
  await page.waitForFunction(() => {
    const submit = document.querySelector('#account-fit-content button[type=submit]');
    return submit && !submit.disabled;
  });
  const actionSelect = page.locator('#account-fit-content select[name=action]');
  await actionSelect.selectOption(action);
  for (const [name, value] of [['execution_price', price], ['fixed_fee', fee], ['fee_rate', rate]]) {
    const input = page.locator(`#account-fit-content [name=${name}]`);
    if (await input.isDisabled()) continue;
    if (value === null) await input.fill(''); else await input.fill(value);
  }
  const requestPromise = fitPost(page);
  const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/portfolio/fit-assessments'));
  const submitButton = page.getByRole('button', {name: '生成账户适配检查'});
  if (doubleClick) await submitButton.dblclick({delay: 120});
  else await submitButton.click();
  let request;
  try {
    request = await Promise.race([requestPromise, new Promise((_, reject) => setTimeout(() => reject(new Error('no fit POST within 5s')), 5000))]);
  } catch (error) {
    const state = await page.evaluate((id) => ({
      pending: sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`),
      disabled: document.querySelector('#account-fit-content button[type=submit]')?.disabled,
      text: document.querySelector('#account-fit-content')?.innerText,
    }), accountId);
    throw new Error(`${error.message}; fit UI state=${JSON.stringify(state)}`);
  }
  const response = await responsePromise;
  assert.equal(response.status(), 202, `${await response.text()} request=${JSON.stringify(request.postDataJSON())}`);
  const body = request.postDataJSON();
  const pending = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`)), accountId);
  assert.deepEqual(pending.body, body, 'complete request must be durable before it reaches fetch');
  assert.equal(pending.operation_id, body.operation_id);
  assert.equal(request.headers()['idempotency-key'], undefined, 'api.js auto UUID header is disabled');
  return {request, response, receipt: await response.json(), body};
}

async function submitFitWithLostReceipt(page, {accountId, price, fee, rate}) {
  const account = page.locator('#account-fit-root select[name="account_id"]');
  await account.selectOption(accountId);
  await page.waitForFunction(() => {
    const submit = document.querySelector('#account-fit-content button[type=submit]');
    return submit && !submit.disabled;
  });
  await page.locator('#account-fit-content select[name=action]').selectOption('open');
  for (const [name, value] of [['execution_price', price], ['fixed_fee', fee], ['fee_rate', rate]]) {
    await page.locator(`#account-fit-content [name=${name}]`).fill(value);
  }
  let firstBody = null;
  let firstReceipt = null;
  let fetchCount = 0;
  page.__expectedDroppedReceipt = true;
  await page.route('**/portfolio/fit-assessments', async (route) => {
    fetchCount += 1;
    if (fetchCount === 1) {
      firstBody = route.request().postDataJSON();
      const response = await route.fetch();
      assert.equal(response.status(), 202, 'the real API accepts the first request before its response is dropped');
      firstReceipt = await response.json();
      await route.abort('failed');
      return;
    }
    await route.continue();
  });
  const firstRequest = fitPost(page);
  await page.getByRole('button', {name: '生成账户适配检查'}).click();
  await firstRequest;
  await page.getByRole('button', {name: '用相同操作 ID 重试确认'}).waitFor();
  const uncertain = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`)), accountId);
  assert.equal(uncertain.phase, 'uncertain');
  assert.deepEqual(uncertain.body, firstBody, 'the uncertain request must retain its exact body');

  const retryRequest = fitPost(page);
  const retryResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/portfolio/fit-assessments'));
  await page.getByRole('button', {name: '用相同操作 ID 重试确认'}).click();
  const request = await retryRequest;
  const response = await retryResponse;
  assert.equal(response.status(), 202, await response.text());
  assert.deepEqual(request.postDataJSON(), firstBody, 'retry must resend the exact same complete body and UUID');
  assert.equal((await response.json()).run_ref.id, firstReceipt.run_ref.id, 'same operation ID must recover the original accepted run');
  page.__expectedDroppedReceipt = false;
  await page.unroute('**/portfolio/fit-assessments');
  return {body: firstBody};
}

async function finishRun(page, body, {visibilityResume = false} = {}) {
  if (visibilityResume) {
    await page.locator('#account-fit-content form.fit-form').waitFor();
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable: true, value: true}); document.dispatchEvent(new Event('visibilitychange')); });
  }
  const response = await api('/fixture/run', {});
  assert.ok(response.completed >= 1, 'real worker must claim the admitted assessment');
  if (visibilityResume) {
    assert.ok(await page.locator('#account-fit-content form.fit-form').count(), 'polling must retain the editable form while hidden');
    await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable: true, value: false}); document.dispatchEvent(new Event('visibilitychange')); });
  }
  await page.waitForFunction((operationId) => {
    const key = `zc-investment-assessment-pending-fit-v1:${document.querySelector('#account-fit-root select[name=account_id]')?.value}`;
    const record = JSON.parse(sessionStorage.getItem(key) || 'null');
    const saved = JSON.parse(sessionStorage.getItem(key.replace('pending-fit', 'last-fit')) || 'null');
    return record?.operation_id === operationId && record.phase === 'accepted' || saved?.operation_id === operationId;
  }, body.operation_id);
  await page.getByText(/财务约束检查：/).waitFor({timeout: 15000});
  assert.ok(await page.locator('#account-fit-content form.fit-form').count(), 'polling results must not replace the form');
  return page.locator('#account-fit-content').innerText();
}

try {
  await ready();
  const accounts = await api('/portfolio/accounts');
  assert.equal(accounts.length, 2);
  const [small, large] = accounts;
  const probeDir = resolve('docs/product/probes/account-fit-p1');
  await mkdir(probeDir, {recursive: true});

  for (const viewport of [{width: 1440, height: 1150}, {width: 390, height: 980}]) {
    const context = await browser.newContext({viewport});
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(String(error.stack || error)));
    page.on('console', (message) => {
      const location = message.location();
      if (message.type() === 'error' && !location.url.includes('/stocks/search?') && !(page.__expectedDroppedReceipt && location.url.endsWith('/portfolio/fit-assessments')) && !(page.__expectedTransientFitRead && location.url.includes('/portfolio/fit-assessments/')) && !(page.__expectedTransientRunRead && location.url.includes('/investment-assessment-runs/'))) errors.push(`console: ${message.text()} ${JSON.stringify(location)}`);
    });
    let fitPosts = 0;
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/portfolio/fit-assessments')) fitPosts += 1;
    });
    await login(page);
    await page.goto(`${base}/app/candidates`);
    await page.waitForFunction(() => document.querySelector('#candidate-ranking-content select[name=purpose_state]'));
    assert.equal(await page.locator('#candidate-ranking-content select[name=purpose_state] option').count(), 5);
    assert.equal(await page.locator('#candidate-ranking-content [name=price_confirmation]').isChecked(), false);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `ranking page must not overflow horizontally at ${viewport.width}px`);
    let rankingPosts = 0;
    page.on('request', (request) => { if (request.method() === 'POST' && request.url().endsWith('/candidates/rankings')) rankingPosts += 1; });
    const ranking = await submitRanking(page, small.id);
    assert.equal(ranking.receipt.run_ref.kind, 'pipeline_run');
    await page.reload();
    await page.waitForFunction((id) => {
      const record = JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`) || 'null');
      return record?.phase === 'accepted' && record.accepted?.run_ref?.id;
    }, small.id);
    assert.equal(rankingPosts, 1, 'reload after 202 resumes the accepted ranking without a second POST');
    const rankingWorker = await api('/fixture/run', {});
    assert.ok(rankingWorker.completed >= 1);
    await page.getByText(/生成时证据：/).waitFor({timeout: 15000});
    await page.getByText(/当前证据：/).waitFor({timeout: 15000});
    assert.match(await page.locator('#candidate-ranking-content').innerText(), /当前证据：/);
    assert.ok(await page.locator('#candidate-ranking-content form.ranking-form').count(), 'ranking results must preserve the form');
    await page.reload();
    const savedRankingDetails = page.locator('#candidate-ranking-content details.ranking-saved').first();
    await savedRankingDetails.waitFor();
    assert.equal(rankingPosts, 1, 'reload reads the saved ranking without resubmitting');
    const savedRanking = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`)), small.id);
    const firstPage = await api(`/candidates/rankings/${savedRanking.ranking_ref.id}?account_id=${small.id}&revision=1&limit=50`);
    assert.ok(firstPage.snapshot.entry_count >= 100, 'real ranking must contain enough rows to exercise pagination');
    await savedRankingDetails.locator('summary').first().click();
    await page.waitForFunction(() => document.querySelectorAll('#candidate-ranking-content .ranking-entry').length >= 50);
    assert.match(await page.locator('#candidate-ranking-content .ranking-entries').innerText(), /\.SH · /, 'entry rows use frozen ticker and name metadata');
    await page.locator('#candidate-ranking-content .ranking-view').scrollIntoViewIfNeeded();
    const rankingScreenshot = viewport.width === 390 ? 'synthetic-ranking-mobile-390.png' : 'synthetic-ranking-desktop-1440.png';
    await page.screenshot({path: join(probeDir, rankingScreenshot), fullPage: false});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `rendered ranking page must not overflow horizontally at ${viewport.width}px`);
    let releaseDelayedPage;
    let delayedPageReady;
    const delayedPagePromise = new Promise((resolve) => { delayedPageReady = resolve; });
    await page.route('**/candidates/rankings/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.has('cursor') && !url.searchParams.has('financial_status')) {
        const response = await route.fetch();
        delayedPageReady({route, response});
        await new Promise((resolve) => { releaseDelayedPage = resolve; });
        await route.fulfill({response});
      } else await route.continue();
    });
    await page.locator('#candidate-ranking-content .ranking-pager button').click();
    const delayedPage = await delayedPagePromise;
    const financialFilter = page.locator('#candidate-ranking-content .ranking-view select[aria-label="财务状态筛选"]');
    await financialFilter.selectOption('blocked');
    await page.waitForResponse((response) => response.request().method() === 'GET' && new URL(response.url()).pathname.endsWith(`/candidates/rankings/${savedRanking.ranking_ref.id}`) && new URL(response.url()).searchParams.get('financial_status') === 'blocked');
    const filteredText = await page.locator('#candidate-ranking-content .ranking-entries').innerText();
    releaseDelayedPage();
    await page.waitForTimeout(200);
    assert.equal(await financialFilter.inputValue(), 'blocked');
    assert.equal(await page.locator('#candidate-ranking-content .ranking-entries').innerText(), filteredText, 'late page-2 response cannot replace the current filtered result');
    await page.unroute('**/candidates/rankings/**');
    await page.goto(`${base}/app/stocks/600000.SH`);
    await page.locator('#account-fit-root').waitFor();
    try { await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'), null, {timeout: 10000}); }
    catch (error) {
      const moduleChecks = await page.evaluate(async () => {
        const out = [];
        for (const module of ['account_fit.js', 'candidate_ranking.js', 'app.js', 'industry_peers.js', 'api.js', 'dom.js']) {
          const response = await fetch(`/app/static/${module}`);
          const source = await response.text();
          try { await import(`/app/static/${module}?probe=${Date.now()}`); out.push(`${module}:ok ${response.status} ${response.headers.get('content-type')} ${source.length}`); }
          catch (issue) {
            let classic = '';
            try { new Function(source.replace(/^import .*;\s*$/gm, '').replace(/^export\s+/gm, '')); } catch (parse) { classic = `${parse.stack || parse.message}`; }
            out.push(`${module}:${issue.stack || issue.message} line=${issue.lineNumber} col=${issue.columnNumber} classic=${classic} HTTP ${response.status} ${response.headers.get('content-type')} ${source.length}`);
          }
        }
        return out;
      });
      console.error('Initial stock UI:', page.url(), await page.locator('body').innerText(), errors, moduleChecks);
      throw error;
    }

    // F01 at CNY 200,000 is blocked by a 10% single-listing cap. The admitted
    // immutable assessment is completed by the actual worker and rendered.
    const blocked = await submitFit(page, {accountId: small.id, price: '1500', fee: '10', rate: '0'});
    await page.reload();
    const pendingAfterReload = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`)), small.id);
    assert.equal(pendingAfterReload.phase, 'accepted');
    assert.equal(pendingAfterReload.accepted.run_ref.id, blocked.receipt.run_ref.id, 'reload follows the exact accepted run before worker completion');
    assert.equal(fitPosts, 1, 'reload after 202 does not send another fit POST');
    const blockedText = await finishRun(page, blocked.body);
    assert.match(blockedText, /财务约束检查：blocked/);
    assert.match(blockedText, /incomplete|blocked/);
    const runRef = blocked.receipt.run_ref;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#account-fit-content details.fit-saved-result'));
    assert.equal(fitPosts, 1, 'reload reads the last saved result without resubmitting');
    assert.equal((await api(`/investment-assessment-runs/${runRef.id}`)).run_ref.id, runRef.id);

    // Mandates are owner-global. A user-confirmed revision switches the
    // explicit account constraint before testing the second account.
    await setMandateAccount(large.id);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    if (viewport.width === 1440) {
      const history = page.locator('#account-fit-content details.fit-saved-result');
      await history.waitFor();
      await history.locator('summary').first().click();
      await page.getByText(/财务约束检查：blocked/).waitFor();
      assert.match(await page.locator('#account-fit-content').innerText(), /基于旧输入/);
      await page.locator('#account-fit-root').scrollIntoViewIfNeeded();
      await page.screenshot({path: join(probeDir, 'synthetic-desktop-1440.png'), fullPage: false});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'fit page must not overflow horizontally at desktop width');
    }
    const postsBeforeDoubleClick = fitPosts;
    const pass = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'}, {doubleClick: true});
    await page.waitForTimeout(350);
    assert.equal(fitPosts - postsBeforeDoubleClick, 1, 'double-click creates exactly one admitted operation');
    const passText = await finishRun(page, pass.body, {visibilityResume: true});
    assert.match(passText, /财务约束检查：pass/);
    assert.match(passText, /整体状态：incomplete/);
    await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
    assert.equal(await page.evaluate((id) => sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`), large.id), null, 'terminal fit must release its pending operation before a new scenario');
    const repeat = await submitFit(page, {accountId: large.id, price: '1500', fee: '11', rate: '0'});
    assert.notEqual(repeat.body.operation_id, pass.body.operation_id);
    await finishRun(page, repeat.body);
    if (viewport.width === 390) {
      await page.locator('#account-fit-root').scrollIntoViewIfNeeded();
      await page.screenshot({path: join(probeDir, 'synthetic-mobile-390.png'), fullPage: false});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'fit page must not overflow horizontally at mobile width');
    }

    // An explicit hold scenario carries no TradeAssumptions and remains usable
    // without a trusted buy rule.
    await page.goto(`${base}/app/stocks/600001.SH`);
    await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    const hold = await submitFit(page, {accountId: large.id, price: '999', fee: null, rate: null, action: 'hold'});
    assert.equal(hold.body.items[0].action, 'hold');
    assert.equal('trade_assumptions' in hold.body.items[0], false);
    const holdText = await finishRun(page, hold.body);
    assert.match(holdText, /财务约束检查：/);

    // Blank price and fee fields are preserved as explicitly unknown values.
    await page.goto(`${base}/app/stocks/600000.SH`);
    await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    await setMandateAccount(small.id);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    const unknown = await submitFit(page, {accountId: small.id, price: null, fee: null, rate: null});
    assert.deepEqual(new Set(unknown.body.items[0].trade_assumptions.unknown_fields), new Set(['execution_price', 'fixed_fee', 'fee_rate']));
    const unknownText = await finishRun(page, unknown.body);
    assert.match(unknownText, /financial|unknown|incomplete|blocked/i);

    if (viewport.width === 1440) {
      const dropped = await submitFitWithLostReceipt(page, {accountId: small.id, price: '1500', fee: '10', rate: '0'});
      await finishRun(page, dropped.body);

      const doomed = await submitFit(page, {accountId: small.id, price: '1500', fee: '10', rate: '0'});
      const corrupted = await api(`/fixture/corrupt-run/${doomed.receipt.run_ref.id}`, {});
      assert.equal(corrupted.updated, true);
      await api('/fixture/run', {});
      await page.getByText(/评估已结束（failed）/).waitFor({timeout: 15000});
      const failedRecord = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}:terminal`)), small.id);
      assert.equal(failedRecord.operation_id, doomed.body.operation_id);
      assert.equal(failedRecord.terminal_status, 'failed');
      await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
      const recovered = await submitFit(page, {accountId: small.id, price: '1500', fee: '12', rate: '0'});
      assert.notEqual(recovered.body.operation_id, doomed.body.operation_id);
      await finishRun(page, recovered.body);
    }

    // Browser storage failure is caught before any request is sent.
    await page.goto(`${base}/app/stocks/600000.SH`);
    await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    await page.locator('#account-fit-content select[name=account_id]').selectOption(small.id);
    await page.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith('zc-investment-assessment-pending-fit-v1:')) throw new Error('synthetic storage denied');
        return original.call(this, key, value);
      };
    });
    const beforeStorageFailure = fitPosts;
    await page.getByRole('button', {name: '生成账户适配检查'}).click();
    await page.getByText(/无法保存稳定操作 ID 和完整请求/).waitFor();
    await page.waitForTimeout(100);
    assert.equal(fitPosts, beforeStorageFailure, 'storage failure must produce zero assessment POSTs');

    // Late account-context responses cannot replace the newly selected account.
    const holdPage = await context.newPage();
    await login(holdPage);
    await holdPage.goto(`${base}/app/stocks/600001.SH`);
    await holdPage.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
    let releaseLate;
    let delayed;
    const lateArrived = new Promise((resolve) => { delayed = resolve; });
    const lateHold = new Promise((resolve) => { releaseLate = resolve; });
    await holdPage.route('**/portfolio/fit-context?**', async (route) => {
      const accountId = new URL(route.request().url()).searchParams.get('account_id');
      if (accountId === large.id) { delayed(); await lateHold; }
      await route.continue();
    });
    const accountSelect = holdPage.locator('#account-fit-content select[name=account_id]');
    await accountSelect.selectOption(large.id);
    await lateArrived;
    await accountSelect.selectOption(small.id);
    await holdPage.waitForFunction(() => document.querySelector('#account-fit-content select[name=action]')?.value === 'open');
    releaseLate();
    await holdPage.waitForTimeout(250);
    assert.equal(await holdPage.locator('#account-fit-content select[name=action]').inputValue(), 'open');
    await holdPage.unroute('**/portfolio/fit-context?**');

    // Editing fee inputs while a real account-context response is delayed must
    // not invalidate that context load or leave submit permanently disabled.
    await holdPage.goto(`${base}/app/candidates`);
    await holdPage.waitForFunction(() => document.querySelector('#candidate-ranking-content [name=account_id]'));
    let releaseRankingContext;
    let heldRankingContext;
    const rankingContextHeld = new Promise((resolve) => { heldRankingContext = resolve; });
    const rankingContextGate = new Promise((resolve) => { releaseRankingContext = resolve; });
    await holdPage.route('**/portfolio/fit-context?**', async (route) => {
      if (new URL(route.request().url()).searchParams.get('account_id') === large.id) {
        const response = await route.fetch();
        heldRankingContext();
        await rankingContextGate;
        await route.fulfill({response});
      } else await route.continue();
    });
    await holdPage.locator('#candidate-ranking-content [name=account_id]').selectOption(large.id);
    await rankingContextHeld;
    await holdPage.locator('#candidate-ranking-content [name=fixed_fee]').fill('2');
    releaseRankingContext();
    await holdPage.waitForFunction(() => !document.querySelector('#candidate-ranking-content button[type=submit]')?.disabled);
    assert.equal(await holdPage.locator('#candidate-ranking-content [name=fixed_fee]').inputValue(), '2');
    await holdPage.unroute('**/portfolio/fit-context?**');

    await holdPage.goto(`${base}/app/stocks/600001.SH`);
    await holdPage.waitForFunction(() => document.querySelector('#account-fit-content [name=account_id]'));
    let releaseFitContext;
    let heldFitContext;
    const fitContextHeld = new Promise((resolve) => { heldFitContext = resolve; });
    const fitContextGate = new Promise((resolve) => { releaseFitContext = resolve; });
    await holdPage.route('**/portfolio/fit-context?**', async (route) => {
      if (new URL(route.request().url()).searchParams.get('account_id') === large.id) {
        const response = await route.fetch();
        heldFitContext();
        await fitContextGate;
        await route.fulfill({response});
      } else await route.continue();
    });
    await holdPage.locator('#account-fit-content [name=account_id]').selectOption(large.id);
    await fitContextHeld;
    await holdPage.locator('#account-fit-content [name=fixed_fee]').fill('2');
    releaseFitContext();
    await holdPage.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
    assert.equal(await holdPage.locator('#account-fit-content [name=fixed_fee]').inputValue(), '2');
    await holdPage.unroute('**/portfolio/fit-context?**');

    if (viewport.width === 390) {
      // Editing the fit form must not disable an already accepted poll. The
      // native select dispatches input and then change, the sequence that used
      // to invalidate the input listener's newly scheduled poll token.
      await page.goto(`${base}/app/candidates`);
      const savedDetail = page.locator('#candidate-ranking-content details.ranking-saved').first();
      await savedDetail.waitFor();
      await savedDetail.locator('summary').first().click();
      await page.waitForFunction(() => document.querySelectorAll('#candidate-ranking-content .ranking-entry').length >= 50);
      await page.locator('#candidate-ranking-content [name=fixed_fee]').fill('1');
      await page.locator('#candidate-ranking-content .ranking-pager button').click();
      await page.waitForFunction(() => Number(document.querySelector('#candidate-ranking-content .ranking-entry strong')?.textContent.match(/^(\d+)/)?.[1]) > 50);
      assert.ok(await page.locator('#candidate-ranking-content .ranking-stale-scenario').count(), 'edited ranking form must label the visible snapshot as the prior scenario');

      await seedPurposeChoices(large.id);
      const confirmedPurposes = await api(`/portfolio/purposes/current?account_id=${large.id}`);
      assert.equal(confirmedPurposes.version.purposes.length, 2, 'the synthetic purpose version is written through the real API');
      await page.goto(`${base}/app/stocks/600000.SH`);
      await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
      await page.locator('#account-fit-content select[name=account_id]').selectOption(large.id);
      try { await page.waitForFunction(() => document.querySelectorAll('#account-fit-content select[name=purpose] option').length === 3, null, {timeout: 8000}); }
      catch (error) {
        const state = await page.evaluate(() => ({account: document.querySelector('#account-fit-content [name=account_id]')?.value, purposes: [...document.querySelectorAll('#account-fit-content [name=purpose] option')].map((option) => ({value: option.value, text: option.textContent})), note: document.querySelector('#account-fit-content .fit-context-note')?.textContent}));
        throw new Error(`${error.message}; purpose context UI=${JSON.stringify(state)}`);
      }
      const purposePollBefore = fitPosts;
      const purposeRun = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'});
      await page.locator('#account-fit-content select[name=purpose]').selectOption({index: 1});
      await api('/fixture/run', {});
      await page.waitForFunction(({operationId, id}) => {
        const pending = JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null');
        const saved = JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null');
        return saved?.operation_id === operationId && !pending;
      }, {operationId: purposeRun.body.operation_id, id: large.id}, {timeout: 10000});
      assert.equal(fitPosts - purposePollBefore, 1, 'purpose input/change sequence follows the accepted run without another POST');
      assert.match(await page.locator('#account-fit-content .fit-operation-result').innerText(), /以下状态来自上次已提交情景/);
      const changedResultStatus = await page.locator('#account-fit-content .fit-financial-status').innerText();
      assert.match(changedResultStatus, /上次情景：/, 'the previous pass/block heading must be marked as a prior scenario after an edit');

      // A terminal run and exact fit_ref stay durable when the result GET has
      // one transient failure. Reload must retry that GET without another POST.
      await page.goto(`${base}/app/stocks/600000.SH`);
      await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
      await page.locator('#account-fit-content select[name=account_id]').selectOption(large.id);
      await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
      const previousFitId = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.fit_ref?.id, large.id);
      let failedResultRead = false;
      page.__expectedTransientFitRead = true;
      await page.route('**/portfolio/fit-assessments/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (!failedResultRead && path.startsWith('/portfolio/fit-assessments/') && path.split('/').at(-1) !== previousFitId) {
          failedResultRead = true;
          await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic temporary result read failure'})});
        } else await route.continue();
      });
      const readFailureBefore = fitPosts;
      const readFailureRun = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'});
      await api('/fixture/run', {});
      await page.getByText(/原操作和结果引用仍已保存/).waitFor({timeout: 10000});
      const terminalPending = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null'), large.id);
      assert.equal(terminalPending.phase, 'terminal');
      assert.ok(terminalPending.fit_ref?.id);
      await page.reload();
      page.__expectedTransientFitRead = false;
      await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
      await page.locator('#account-fit-content select[name=account_id]').selectOption(large.id);
      await page.waitForFunction(({operationId, id}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {operationId: readFailureRun.body.operation_id, id: large.id}, {timeout: 10000});
      assert.equal(fitPosts - readFailureBefore, 1, 'terminal result recovery reads the exact fit without resending the accepted POST');
      await page.unroute('**/portfolio/fit-assessments/**');

      // Failure of the second storage write leaves a durable prepared record.
      // Reload exposes an explicit safe continuation using the same UUID/body.
      await page.locator('#account-fit-content select[name=account_id]').selectOption(large.id);
      await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
      await page.evaluate((id) => {
        const original = Storage.prototype.setItem;
        let writes = 0;
        Storage.prototype.setItem = function (key, value) {
          if (key === `zc-investment-assessment-pending-fit-v1:${id}` && ++writes === 2) throw new Error('synthetic second storage write failure');
          return original.call(this, key, value);
        };
      }, large.id);
      const preparedBefore = fitPosts;
      await page.getByRole('button', {name: '生成账户适配检查'}).click();
      await page.getByRole('button', {name: '继续发送已准备操作'}).waitFor();
      const prepared = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`)), large.id);
      assert.equal(prepared.phase, 'prepared');
      assert.equal(fitPosts, preparedBefore, 'second storage write failure must occur before POST');
      await page.reload();
      await page.waitForFunction(() => document.querySelector('#account-fit-content select[name=account_id]'));
      await page.locator('#account-fit-content select[name=account_id]').selectOption(large.id);
      await page.getByRole('button', {name: '继续发送已准备操作'}).waitFor();
      const retryRequest = fitPost(page);
      const retryResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/portfolio/fit-assessments'));
      await page.getByRole('button', {name: '继续发送已准备操作'}).click();
      const retriedRequest = await retryRequest;
      const retriedResponse = await retryResponse;
      assert.equal(retriedResponse.status(), 202);
      assert.deepEqual(retriedRequest.postDataJSON(), prepared.body);
      assert.equal(fitPosts - preparedBefore, 1);
      const preparedReceipt = await retriedResponse.json();
      await api('/fixture/run', {});
      await page.waitForFunction(({operationId, id}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {operationId: prepared.body.operation_id, id: large.id}, {timeout: 10000});
      assert.equal((await api(`/investment-assessment-runs/${preparedReceipt.run_ref.id}`)).run_ref.id, preparedReceipt.run_ref.id);

      // Delay a real accepted response until after an edit. The late receipt is
      // still tracked for this account and its old assumptions remain labeled.
      await page.locator('#account-fit-content [name=execution_price]').fill('1500');
      await page.locator('#account-fit-content [name=fixed_fee]').fill('10');
      await page.locator('#account-fit-content [name=fee_rate]').fill('0');
      let releaseReceipt;
      let heldReceipt;
      const acceptedResponseReady = new Promise((resolve) => { heldReceipt = resolve; });
      const responseGate = new Promise((resolve) => { releaseReceipt = resolve; });
      await page.route('**/portfolio/fit-assessments', async (route) => {
        if (route.request().method() !== 'POST') return route.continue();
        const response = await route.fetch();
        heldReceipt({route, response});
        await responseGate;
        await route.fulfill({response});
      });
      const delayedPostBefore = fitPosts;
      const delayedRequest = fitPost(page);
      await page.getByRole('button', {name: '生成账户适配检查'}).click();
      const held = await acceptedResponseReady;
      const delayedBody = (await delayedRequest).postDataJSON();
      await page.locator('#account-fit-content [name=fixed_fee]').fill('11');
      const delayedResponsePromise = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/portfolio/fit-assessments'));
      releaseReceipt();
      const delayedResponse = await delayedResponsePromise;
      assert.equal(delayedResponse.status(), 202);
      assert.equal((await delayedResponse.json()).run_ref.id > 0, true);
      await api('/fixture/run', {});
      await page.waitForFunction(({operationId, id}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {operationId: delayedBody.operation_id, id: large.id}, {timeout: 10000});
      assert.equal(fitPosts - delayedPostBefore, 1);
      assert.ok(await page.locator('#account-fit-content .fit-stale-scenario').count(), 'late receipt remains visible with the old submitted assumptions labeled');
      await page.unroute('**/portfolio/fit-assessments');
      assert.equal(typeof held.response.status(), 'number');

      // An accepted ranking for A remains in A's slot while B submits and
      // finishes its own run. A delayed A poll response must not interfere
      // with B's timer or visible result; switching back restores A's run.
      await page.goto(`${base}/app/candidates`);
      await page.waitForFunction(() => document.querySelector('#candidate-ranking-content [name=account_id]'));
      await page.locator('#candidate-ranking-content [name=account_id]').selectOption(large.id);
      await page.waitForFunction(() => !document.querySelector('#candidate-ranking-content button[type=submit]')?.disabled);
      const rankingA = await submitRanking(page, large.id);
      const rankingRunA = String(rankingA.receipt.run_ref.id);
      let releaseRankingA;
      let heldRankingA;
      const rankingARequest = new Promise((resolve) => { heldRankingA = resolve; });
      const rankingAGate = new Promise((resolve) => { releaseRankingA = resolve; });
      await page.route(`**/investment-assessment-runs/${rankingRunA}`, async (route) => {
        const queued = await route.fetch();
        assert.equal((await queued.json()).status, 'queued');
        heldRankingA();
        await rankingAGate;
        await route.fulfill({response: queued});
      }, {times: 1});
      await rankingARequest;
      await page.locator('#candidate-ranking-content [name=account_id]').selectOption(small.id);
      await page.waitForFunction(() => !document.querySelector('#candidate-ranking-content button[type=submit]')?.disabled);
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`) || 'null')?.operation_id ?? null, small.id), null, 'switching to B must not copy A into B pending slot');
      const rankingB = await submitRanking(page, small.id);
      assert.notEqual(rankingB.body.operation_id, rankingA.body.operation_id);
      const lateRankingPoll = page.waitForResponse((response) => response.url().endsWith(`/investment-assessment-runs/${rankingRunA}`));
      releaseRankingA();
      await lateRankingPoll;
      await page.waitForTimeout(150);
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`) || 'null')?.operation_id === operationId, {id: small.id, operationId: rankingB.body.operation_id}, {timeout: 15000});
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`) || 'null')?.operation_id, large.id), rankingA.body.operation_id, 'late A response must leave A resumable and not clear it');
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`) || 'null')?.operation_id, small.id), rankingB.body.operation_id, 'late A response must not overwrite B result');
      await page.locator('#candidate-ranking-content [name=account_id]').selectOption(large.id);
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: rankingA.body.operation_id}, {timeout: 15000});

      // A one-time run-status GET failure must keep the same accepted
      // operation live and retry without requiring a page reload.
      let releaseRankingFailure;
      const rankingStatusFailed = new Promise((resolve) => { releaseRankingFailure = resolve; });
      let failRankingStatusOnce = true;
      page.__expectedTransientRunRead = true;
      const retryRanking = await submitRanking(page, large.id);
      await page.route(`**/investment-assessment-runs/${retryRanking.receipt.run_ref.id}`, async (route) => {
        if (failRankingStatusOnce) {
          failRankingStatusOnce = false;
          await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic temporary run read failure'})});
          releaseRankingFailure();
        } else await route.continue();
      });
      await rankingStatusFailed;
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: retryRanking.body.operation_id}, {timeout: 15000});
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`) || 'null'), large.id), null, 'ranking status retry must finish the original operation without a reload');
      await page.unroute(`**/investment-assessment-runs/${retryRanking.receipt.run_ref.id}`);
      page.__expectedTransientRunRead = false;

      let permanentStatusReads = 0;
      page.__expectedTransientRunRead = true;
      const permanentRead = await submitRanking(page, large.id);
      await page.route(`**/investment-assessment-runs/${permanentRead.receipt.run_ref.id}`, async (route) => {
        permanentStatusReads += 1;
        await route.fulfill({status: 404, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic permanent run read failure'})});
      });
      await page.getByText(/任务状态读取被拒绝/).waitFor({timeout: 5000});
      await page.waitForTimeout(2600);
      assert.equal(permanentStatusReads, 1, 'a permanent run GET error must not create an automatic request loop');
      const postsBeforeStatusReload = rankingPosts;
      await page.unroute(`**/investment-assessment-runs/${permanentRead.receipt.run_ref.id}`);
      await page.reload();
      await page.waitForFunction(() => document.querySelector('#candidate-ranking-content [name=account_id]'));
      await page.locator('#candidate-ranking-content [name=account_id]').selectOption(large.id);
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-ranking-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: permanentRead.body.operation_id});
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-ranking-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: permanentRead.body.operation_id}, {timeout: 15000});
      assert.equal(rankingPosts, postsBeforeStatusReload, 'reloading after a permanent status error resumes the accepted run without another POST');
      page.__expectedTransientRunRead = false;

      // Repeat the same account isolation for fit runs and their fit_ref GET.
      await page.goto(`${base}/app/stocks/600000.SH`);
      await page.waitForFunction(() => document.querySelector('#account-fit-content [name=account_id]'));
      const fitA = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'});
      let releaseFitA;
      let heldFitA;
      const fitARequest = new Promise((resolve) => { heldFitA = resolve; });
      const fitAGate = new Promise((resolve) => { releaseFitA = resolve; });
      await page.route(`**/investment-assessment-runs/${fitA.receipt.run_ref.id}`, async (route) => {
        const queued = await route.fetch();
        assert.equal((await queued.json()).status, 'queued');
        heldFitA();
        await fitAGate;
        await route.fulfill({response: queued});
      }, {times: 1});
      await fitARequest;
      await page.locator('#account-fit-content [name=account_id]').selectOption(small.id);
      await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null')?.operation_id ?? null, small.id), null, 'fit A must remain exclusively in A slot');
      const fitB = await submitFit(page, {accountId: small.id, price: '1500', fee: '10', rate: '0'});
      assert.notEqual(fitB.body.operation_id, fitA.body.operation_id);
      const lateFitPoll = page.waitForResponse((response) => response.url().endsWith(`/investment-assessment-runs/${fitA.receipt.run_ref.id}`));
      releaseFitA();
      await lateFitPoll;
      await page.waitForTimeout(150);
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {id: small.id, operationId: fitB.body.operation_id}, {timeout: 15000});
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null')?.operation_id, large.id), fitA.body.operation_id);
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id, small.id), fitB.body.operation_id);
      await page.locator('#account-fit-content [name=account_id]').selectOption(large.id);
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: fitA.body.operation_id}, {timeout: 15000});

      // Hold A's old terminal fit_ref GET, switch A→B→A so the saved terminal
      // record is recovered, submit a fresh A operation, then deliver a late
      // failure for the old GET. It must not replace the new operation UI.
      const oldSavedFitId = await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.fit_ref?.id, large.id);
      let releaseOldFitRead;
      let heldOldFitRead;
      let finishOldFitRead;
      const oldFitReadHeld = new Promise((resolve) => { heldOldFitRead = resolve; });
      const oldFitReadGate = new Promise((resolve) => { releaseOldFitRead = resolve; });
      const oldFitReadDelivered = new Promise((resolve) => { finishOldFitRead = resolve; });
      let heldOldFitOnce = false;
      page.__expectedTransientFitRead = true;
      await page.route('**/portfolio/fit-assessments/**', async (route) => {
        const url = new URL(route.request().url());
        const id = url.pathname.split('/').at(-1);
        if (route.request().method() === 'GET' && id !== oldSavedFitId && !heldOldFitOnce) {
          heldOldFitOnce = true;
          await route.fetch(); // The synthetic failure follows a real exact GET.
          heldOldFitRead();
          await oldFitReadGate;
          await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'late old-operation fit read failure'})});
          finishOldFitRead();
        } else await route.continue();
      });
      const oldTerminalOperation = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'});
      await api('/fixture/run', {});
      await oldFitReadHeld;
      await page.locator('#account-fit-content [name=fixed_fee]').fill('12');
      await page.locator('#account-fit-content [name=account_id]').selectOption(small.id);
      await page.waitForFunction(() => !document.querySelector('#account-fit-content button[type=submit]')?.disabled);
      await page.locator('#account-fit-content [name=account_id]').selectOption(large.id);
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId && !JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null'), {id: large.id, operationId: oldTerminalOperation.body.operation_id}, {timeout: 15000});
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.stale_scenario, large.id), true, 'editing during the terminal fit-ref GET must remain labeled after recovery');
      const newFitOperation = await submitFit(page, {accountId: large.id, price: '1500', fee: '11', rate: '0'});
      releaseOldFitRead();
      await oldFitReadDelivered;
      await page.waitForTimeout(150);
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null')?.operation_id, large.id), newFitOperation.body.operation_id, 'late old fit-ref response must preserve the newer same-account operation');
      assert.equal(await page.getByRole('button', {name: '重试读取已完成记录'}).count(), 0, 'late old fit-ref failure must not replace the new operation with a stale retry control');
      assert.doesNotMatch(await page.locator('#account-fit-content').innerText(), /late old-operation fit read failure/);
      await page.unroute('**/portfolio/fit-assessments/**');
      page.__expectedTransientFitRead = false;
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: newFitOperation.body.operation_id}, {timeout: 15000});

      let releaseFitFailure;
      const fitStatusFailed = new Promise((resolve) => { releaseFitFailure = resolve; });
      let failFitStatusOnce = true;
      page.__expectedTransientRunRead = true;
      const retryFit = await submitFit(page, {accountId: large.id, price: '1500', fee: '10', rate: '0'});
      await page.route(`**/investment-assessment-runs/${retryFit.receipt.run_ref.id}`, async (route) => {
        if (failFitStatusOnce) {
          failFitStatusOnce = false;
          await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic temporary run read failure'})});
          releaseFitFailure();
        } else await route.continue();
      });
      await fitStatusFailed;
      await api('/fixture/run', {});
      await page.waitForFunction(({id, operationId}) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-last-fit-v1:${id}`) || 'null')?.operation_id === operationId, {id: large.id, operationId: retryFit.body.operation_id}, {timeout: 15000});
      assert.equal(await page.evaluate((id) => JSON.parse(sessionStorage.getItem(`zc-investment-assessment-pending-fit-v1:${id}`) || 'null'), large.id), null, 'fit status retry must finish the original operation without a reload');
      await page.unroute(`**/investment-assessment-runs/${retryFit.receipt.run_ref.id}`);
      page.__expectedTransientRunRead = false;
    }

    assert.equal(errors.length, 0, errors.join('\n'));
    await holdPage.close();
    await context.close();
    console.log(`synthetic ${viewport.width}px: 105-member ranking page/filter race, F01 blocked/pass, hold, explicit unknown, accepted reload, lost-receipt retry, double-click, storage failure and late-context epoch passed`);
  }
  await mkdir('docs/product/probes/account-fit-p1', {recursive: true});
  console.log('All browser evidence uses synthetic prices, accounts, constraints and exchange rules in temporary SQLite.');
} catch (error) {
  console.error(error);
  console.error(serverLog);
  process.exitCode = 1;
} finally {
  await browser.close();
  server.kill('SIGTERM');
  await new Promise((resolve) => server.once('exit', resolve));
  await rm(fixtureDir, {recursive: true, force: true});
}
