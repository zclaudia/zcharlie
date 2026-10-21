// Real-browser P2 management/recovery and read-only stock-panel flow.
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
import {randomUUID} from 'node:crypto';

const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = Number(process.env.SUPPLEMENTAL_RULES_FIXTURE_PORT || 19879);
const base = `http://127.0.0.1:${port}`;
const fixtureDir = await mkdtemp(join(tmpdir(), 'supplemental-rules-p2-'));
const probeDir = resolve('docs/product/probes/account-fit-p2');
await mkdir(probeDir, {recursive: true});
const server = spawn('.venv/bin/python', ['tests/browser/supplemental_rules_fixture_server.py'], {
  cwd: resolve('.'), env: {...process.env, SUPPLEMENTAL_RULES_FIXTURE_DIR: fixtureDir, SUPPLEMENTAL_RULES_FIXTURE_PORT: String(port)},
  stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', (chunk) => { serverLog += chunk; });
server.stderr.on('data', (chunk) => { serverLog += chunk; });
const browser = await chromium.launch({headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE});
const headers = {'X-API-Key': 'supplemental-rules-synthetic-test-key-2026', 'Content-Type': 'application/json'};

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

function localInput(date) {
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

try {
  await ready();
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
  const page = await context.newPage();
  await page.goto(base + '/app/login');
  await page.locator('[name=api_key]').fill('supplemental-rules-synthetic-test-key-2026');
  await page.getByRole('button', {name: '登录', exact: true}).click();
  await page.goto(base + '/app/supplemental-rules');
  await page.locator('#supplemental-feature-state').getByText('已开启').waitFor();

  // Register a user report; the UI must display the verification boundary.
  await page.locator('.supplemental-events-list > summary').click();
  const eventForm = page.locator('#supplemental-event-form');
  await eventForm.locator('[name=event_key]').fill('synthetic-event-600090');
  await eventForm.locator('[name=title]').fill('合成用户报告：待核对事项');
  await eventForm.locator('[name=event_type]').fill('regulatory_update');
  await eventForm.locator('[name=jurisdiction]').fill('CN');
  await eventForm.locator('[name=policy_stage]').selectOption('unknown');
  await eventForm.locator('[name=affected_subjects]').fill('listing:600090');
  const eventRequest = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/research-events'));
  await eventForm.getByRole('button', {name: '登记用户线索'}).click();
  const createdEvent = await eventRequest;
  assert.ok([200, 201].includes(createdEvent.status()), await createdEvent.text());
  await page.getByText('用户报告 · 尚未核验').waitFor();
  assert.equal(await page.locator('#supplemental-event-list').getByText(/已核验/).count(), 0);

  // Create, validate, and explicitly approve a structured research requirement.
  const form = page.locator('#supplemental-rule-form');
  await form.locator('[name=name]').fill('合成研究核对要求');
  await form.locator('[name=rationale]').fill('浏览器流程合成验证，不代表真实政策或公司事实。');
  await form.locator('[name=materiality]').fill('仅验证规则生命周期和界面交互。');
  await form.locator('[name=selector_kind]').selectOption('listing');
  await form.locator('[name=listing_id]').selectOption('600090');
  await form.locator('[name=rule_type]').selectOption('research_requirement');
  await form.locator('[name=question]').fill('是否有可读的合成证据？');
  await form.locator('[name=required_evidence]').fill('synthetic_evidence');
  await form.locator('[name=completion_criteria]').fill('存在合成证据记录。');
  await form.locator('[name=falsification_criteria]').fill('证据缺失或无法读回。');
  const now = new Date();
  const start = new Date(now.getTime() - 60_000);
  const checkBy = new Date(now.getTime() + 24 * 60 * 60_000);
  const reviewBy = new Date(now.getTime() + 3 * 24 * 60 * 60_000);
  await form.locator('[name=valid_from]').fill(localInput(start));
  await form.locator('[name=check_by]').fill(localInput(checkBy));
  await form.locator('[name=review_due_at]').fill(localInput(reviewBy));
  await form.locator('[name=release_requirements]').fill('明确记录核对结论。');
  const createResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/supplemental-rules'));
  await page.locator('#supplemental-save-rule').click();
  const createdRule = await createResponse;
  assert.ok([200, 201].includes(createdRule.status()), `HTTP ${createdRule.status()}`);
  const createdRulePayload = await createdRule.json();
  const firstRuleId = createdRulePayload.rule_ref.id;
  await page.locator('#supplemental-rule-detail').getByRole('button', {name: '校验此草稿版本'}).click();
  await page.getByText('最近校验：pass').waitFor({timeout: 10000});
  const approval = page.locator('#supplemental-rule-detail').getByRole('button', {name: /明确批准/});
  await page.locator('#supplemental-rule-detail .decision-reason').first().fill('已核对精确合成规则版本及校验结果。');
  const approvalResponse = page.waitForResponse((response) => response.request().method() === 'POST' && /\/supplemental-rules\/[^/]+\/decisions$/.test(response.url()));
  await approval.click();
  const approved = await approvalResponse;
  assert.ok([200, 201].includes(approved.status()), await approved.text());
  await page.locator('#supplemental-rule-detail').getByText(/当前状态：已启用/).waitFor();

  // Produce a second visible rule, then exercise a true A→B→A detail-read
  // race. The first A response is held and made visibly stale before release.
  const firstDetail = await api(`/supplemental-rules/${firstRuleId}`);
  const firstDefinition = firstDetail.active_rule || firstDetail.active || firstDetail.draft_rule || firstDetail.draft;
  const secondContent = {
    schema_version: 1, name: 'ABA 第二条合成规则', rationale: firstDefinition.rationale,
    source_refs: firstDefinition.source_refs, selector: firstDefinition.selector,
    contexts: firstDefinition.contexts, rule_type: firstDefinition.rule_type,
    definition: firstDefinition.definition, dimensions: firstDefinition.dimensions,
    effects: firstDefinition.effects, severity: firstDefinition.severity,
    materiality: firstDefinition.materiality, event_ref: firstDefinition.event_ref,
    issue_key: firstDefinition.issue_key, valid_from: firstDefinition.valid_from,
    valid_until: firstDefinition.valid_until, review_due_at: firstDefinition.review_due_at,
    release_requirements: firstDefinition.release_requirements,
  };
  const secondCreated = await api('/supplemental-rules', {
    idempotency_key: randomUUID(), expected_head_revision: 0, definition: secondContent,
  });
  const secondRuleId = secondCreated.rule_ref.id;
  await page.reload();
  await page.locator(`.supplemental-rule-choice[data-rule-id="${firstRuleId}"]`).waitFor();
  await page.locator(`.supplemental-rule-choice[data-rule-id="${secondRuleId}"]`).waitFor();
  let releaseFirstA;
  let signalFirstA;
  const firstAFetched = new Promise((resolve) => { signalFirstA = resolve; });
  const releasePromise = new Promise((resolve) => { releaseFirstA = resolve; });
  let delayedAUsed = false;
  let firstASettled;
  const firstASettledPromise = new Promise((resolve) => { firstASettled = resolve; });
  await page.route(`**/supplemental-rules/${firstRuleId}`, async (route) => {
    if (route.request().method() !== 'GET' || new URL(route.request().url()).pathname !== `/supplemental-rules/${firstRuleId}` || delayedAUsed) return route.continue();
    delayedAUsed = true;
    const response = await route.fetch();
    const stale = await response.json();
    signalFirstA();
    await releasePromise;
    stale.active_state = 'retired';
    await route.fulfill({response, body: JSON.stringify(stale)});
    firstASettled();
  });
  await page.locator(`.supplemental-rule-choice[data-rule-id="${firstRuleId}"]`).click();
  await firstAFetched;
  await page.locator(`.supplemental-rule-choice[data-rule-id="${secondRuleId}"]`).click();
  await page.locator('#supplemental-rule-detail').getByText(`规则 ${secondRuleId}`).waitFor();
  await page.locator(`.supplemental-rule-choice[data-rule-id="${firstRuleId}"]`).click();
  await page.locator('#supplemental-rule-detail').getByText(`规则 ${firstRuleId}`).waitFor();
  await page.locator('#supplemental-rule-detail').getByText(/当前状态：已启用/).waitFor();
  releaseFirstA();
  await firstASettledPromise;
  await page.waitForTimeout(100);
  assert.doesNotMatch(await page.locator('#supplemental-rule-detail').innerText(), /当前状态：已退役/, 'late A response must not overwrite the newer A selection after A→B→A');
  await page.unroute(`**/supplemental-rules/${firstRuleId}`);
  await page.locator('.supplemental-events-list > summary').click();

  // Lost response recovery must replay the exact event request and stable key.
  await eventForm.locator('[name=event_key]').fill('synthetic-event-lost-response');
  await eventForm.locator('[name=title]').fill('合成丢响应恢复线索');
  let count = 0;
  let firstBody;
  let firstResponse;
  await page.route('**/research-events', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    count += 1;
    if (count === 1) {
      firstBody = route.request().postDataJSON();
      firstResponse = await route.fetch();
      assert.ok([200, 201].includes(firstResponse.status()));
      await route.abort('failed');
      return;
    }
    assert.deepEqual(route.request().postDataJSON(), firstBody, 'uncertain recovery must replay the exact same body');
    await route.continue();
  });
  await eventForm.getByRole('button', {name: '登记用户线索'}).click();
  await page.getByRole('button', {name: '用相同操作 ID 重试确认'}).waitFor();
  const retryResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/research-events'));
  await page.getByRole('button', {name: '用相同操作 ID 重试确认'}).click();
  const recovered = await retryResponse;
  assert.ok([200, 201].includes(recovered.status()), await recovered.text());
  const recoveredBody = await recovered.json();
  assert.equal(recoveredBody.event_ref.id, JSON.parse(await firstResponse.text()).event_ref.id);
  await page.unroute('**/research-events');

  // A known accepted response must survive a sessionStorage cleanup failure,
  // stay out of the lost-response retry path, and keep its run tracked while
  // the user edits another field.
  await page.evaluate(() => {
    const original = Storage.prototype.removeItem;
    window.__restoreSupplementalRemove = () => { Storage.prototype.removeItem = original; };
    Storage.prototype.removeItem = function (key) {
      if (key === 'zc-supplemental-rule-pending-v1') throw new Error('synthetic cleanup failure');
      return original.call(this, key);
    };
  });
  await eventForm.locator('[name=event_key]').fill('synthetic-event-accepted-cleanup-failure');
  await eventForm.locator('[name=title]').fill('已接收但本机清理失败的合成线索');
  const acceptedRunResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/research-events'));
  await eventForm.getByRole('button', {name: '登记用户线索'}).click();
  const acceptedResponse = await acceptedRunResponse;
  assert.ok([200, 201].includes(acceptedResponse.status()), await acceptedResponse.text());
  const acceptedMutation = await acceptedResponse.json();
  assert.ok(acceptedMutation.run_ref, 'successful event mutation must expose its accepted reassessment run');
  const acceptedRunId = String(acceptedMutation.run_ref.id);
  await page.getByText(/本机无法清理回执标记/).waitFor();
  assert.equal(await page.locator('[data-retry-pending]').count(), 0, 'known accepted run must not be offered as a resend');
  await eventForm.locator('[name=event_key]').fill('editing-does-not-stop-run-tracking');
  let runReadCount = 0;
  const countRunReads = (request) => {
    if (request.method() === 'GET' && request.url().includes('/supplemental-rule-evaluations/runs/' + acceptedRunId)) runReadCount += 1;
  };
  page.on('request', countRunReads);
  const runRead = page.waitForResponse((response) =>
    response.request().method() === 'GET'
      && response.url().includes('/supplemental-rule-evaluations/runs/' + acceptedRunId)
  );
  await runRead;
  const runCard = page.locator('.supplemental-run-item[data-run-id="' + acceptedRunId + '"]');
  await runCard.waitFor();
  assert.match(await runCard.innerText(), /处理中|终态/);
  await api('/fixture/run', {});
  await runCard.getByText(/终态 success|终态 partial|终态 failed/).waitFor({timeout: 15000});
  assert.match(await runCard.innerText(), /覆盖完整|覆盖尚未完整/);
  const terminalReadCount = runReadCount;
  await page.waitForTimeout(1500);
  assert.equal(runReadCount, terminalReadCount, 'polling must stop after a terminal run response');
  page.off('request', countRunReads);
  await page.evaluate(() => window.__restoreSupplementalRemove());
  await page.addInitScript(() => {
    if (sessionStorage.getItem('__supplemental_cleanup_failure_once') !== '1') return;
    sessionStorage.removeItem('__supplemental_cleanup_failure_once');
    const original = Storage.prototype.removeItem;
    window.__restoreReloadCleanup = () => { Storage.prototype.removeItem = original; };
    Storage.prototype.removeItem = function (key) {
      if (key === 'zc-supplemental-rule-pending-v1') throw new Error('synthetic reload cleanup failure');
      return original.call(this, key);
    };
  });
  await page.evaluate(() => sessionStorage.setItem('__supplemental_cleanup_failure_once', '1'));
  await page.reload();
  await page.getByText(/本机无法清理回执标记/).waitFor();
  assert.equal(await page.getByText(/上一项操作已收到服务器回执；正在恢复运行跟踪/).count(), 0, 'reload must preserve the cleanup warning instead of overwriting it with success');
  await page.evaluate(() => { window.__restoreReloadCleanup(); sessionStorage.removeItem('zc-supplemental-rule-pending-v1'); });
  await page.locator('.supplemental-events-list > summary').click();

  // Force both receipt persistence and cleanup to fail after the server accepts
  // the command. The independently durable run tracker must bind the pending
  // record by operation ID, exact command path, operation kind, and exact body
  // after a reload, without offering a second POST.
  await page.evaluate(() => {
    const originalSet = Storage.prototype.setItem;
    const originalRemove = Storage.prototype.removeItem;
    let pendingWrites = 0;
    window.__restoreDualRecoveryStorage = () => {
      Storage.prototype.setItem = originalSet;
      Storage.prototype.removeItem = originalRemove;
    };
    Storage.prototype.setItem = function (key, value) {
      if (key === 'zc-supplemental-rule-pending-v1' && ++pendingWrites > 1) throw new Error('synthetic receipt persistence failure');
      return originalSet.call(this, key, value);
    };
    Storage.prototype.removeItem = function (key) {
      if (key === 'zc-supplemental-rule-pending-v1') throw new Error('synthetic cleanup failure');
      return originalRemove.call(this, key);
    };
  });
  await eventForm.locator('[name=event_key]').fill('synthetic-event-dual-storage-failure');
  await eventForm.locator('[name=title]').fill('双重存储故障后安全恢复');
  await eventForm.locator('[name=event_type]').fill('regulatory_update');
  await eventForm.locator('[name=jurisdiction]').fill('CN');
  await eventForm.locator('[name=policy_stage]').selectOption('unknown');
  await eventForm.locator('[name=affected_subjects]').fill('listing:600090');
  let dualFailurePosts = 0;
  const countDualFailurePosts = (request) => {
    if (request.method() === 'POST' && request.url().endsWith('/research-events')) dualFailurePosts += 1;
  };
  page.on('request', countDualFailurePosts);
  const dualFailureResponse = page.waitForResponse((response) => response.request().method() === 'POST' && response.url().endsWith('/research-events'));
  await eventForm.getByRole('button', {name: '登记用户线索'}).click();
  const dualResponse = await dualFailureResponse;
  assert.ok([200, 201].includes(dualResponse.status()), await dualResponse.text());
  const dualMutation = await dualResponse.json();
  const dualPending = await page.evaluate(() => JSON.parse(sessionStorage.getItem('zc-supplemental-rule-pending-v1')));
  const durableTracker = await page.evaluate(() => JSON.parse(sessionStorage.getItem('zc-supplemental-rule-runs-v1') || '[]'));
  assert.equal(dualPending.phase, 'prepared', 'failed accepted-copy write leaves the original prepared request durable');
  assert.equal(dualFailurePosts, 1);
  const durableMatch = durableTracker.find((item) => item.run_ref?.id === dualMutation.run_ref.id);
  assert.ok(durableMatch, 'accepted run is durable before the failed cleanup');
  assert.equal(durableMatch.operation_id, dualPending.body.idempotency_key);
  assert.equal(durableMatch.operation, dualPending.operation);
  assert.equal(durableMatch.path, dualPending.path);
  assert.deepEqual(durableMatch.body, dualPending.body);
  await page.reload();
  await page.getByText(/已从本机保存的运行记录恢复此前已接受的操作/).waitFor();
  assert.equal(dualFailurePosts, 1, 'reload recovery must track the accepted run without resending the command');
  assert.equal(await page.evaluate(() => sessionStorage.getItem('zc-supplemental-rule-pending-v1')), null, 'reload can now clear the stale prepared marker');
  await page.locator('.supplemental-run-item[data-run-id="' + dualMutation.run_ref.id + '"]').waitFor();
  page.off('request', countDualFailurePosts);

  // Corrupt local tracking must not erase a real accepted receipt just because
  // another local record happens to have the same numeric run ID.
  const trackedBeforeCollision = await page.evaluate(() => sessionStorage.getItem('zc-supplemental-rule-runs-v1'));
  await page.evaluate(({record, result}) => {
    sessionStorage.setItem('zc-supplemental-rule-runs-v1', JSON.stringify([
      {...record, operation_id: 'different-local-operation'},
    ]));
    sessionStorage.setItem('zc-supplemental-rule-pending-v1', JSON.stringify({
      phase: 'accepted', operation: record.operation, path: record.path,
      body: record.body, result,
    }));
  }, {record: durableMatch, result: dualMutation});
  await page.reload();
  await page.getByText(/原回执继续保留|原回执保留/).first().waitFor();
  assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem('zc-supplemental-rule-pending-v1'))?.phase), 'accepted');
  await page.evaluate((records) => {
    sessionStorage.removeItem('zc-supplemental-rule-pending-v1');
    sessionStorage.setItem('zc-supplemental-rule-runs-v1', records);
  }, trackedBeforeCollision);
  await page.reload();
  await page.locator('#supplemental-feature-state').getByText('已开启').waitFor();

  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 950});
    await page.locator('#supplemental-rules-root').scrollIntoViewIfNeeded();
    await page.screenshot({path: join(probeDir, `synthetic-management-${width}.png`)});
  }

  await page.goto(base + '/app/stocks/600090.SH');
  await page.locator('#supplemental-items-root').waitFor();
  await page.getByText(/覆盖尚未完整|用户报告，尚未核验|没有可展示的逐规则应用/).first().waitFor({timeout: 10000});
  await page.getByText(/已读取范围：证券 600090；评估视角：company_research/).waitFor();
  assert.equal(await page.locator('link[href^="/app/static/v/"][href$="/pages/supplemental_rules.css"]').count(), 1);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 950});
    await page.locator('#supplemental-items-root').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('.supplemental-issue-group').first().evaluate((element) => getComputedStyle(element).overflowWrap), 'anywhere');
    await page.screenshot({path: join(probeDir, `synthetic-stock-${width}.png`)});
  }
  const groupedProjection = await page.evaluate(async () => {
    const module = await import('/app/static/supplemental_items.js');
    const target = document.createElement('div');
    const appA = {
      id: '10000000-0000-4000-8000-000000000001', revision: 1,
      rule_ref: {kind: 'supplemental_rule', id: '20000000-0000-4000-8000-000000000001', revision: 1},
      applicability: 'applicable', condition_result: 'triggered', evidence_status: 'pending',
      assessment_status: 'unresolved', semantic_status: 'pending', as_of: '2026-10-16T00:00:00Z',
      evaluated_at: '2026-10-16T00:00:00Z', application_mode: 'live', open_issue_refs: [],
      frozen_pair: {rule: {name: '合成规则甲', revision: 1, source_refs: []}, subject: {evidence: [
        {id: '30000000-0000-4000-8000-000000000001', revision: 2},
      ]}},
      result: {event_fact: {status: 'pending', evidence_refs: []}, company_exposure: {status: 'unknown', evidence_refs: []},
        impact_assessment: {status: 'pending', summary: '甲项需核对', evidence_refs: []}, diagnostics: []},
    };
    const appBRef = {kind: 'rule_application', id: '10000000-0000-4000-8000-000000000002', revision: 1};
    const view = {
      listing_ref: {kind: 'listing', id: 600090, revision: 1}, context: 'company_research',
      applications: [{payload: appA, current_limits: {current_status: 'valid'}}],
      coverage_complete: false, mixed: false, diagnostics: [], unbound_events: [], cursor: 'next-cursor',
      issue_groups: [{
        display_key: 'synthetic-group', dimension: 'downside_counter_case', issue_key: 'synthetic-issue',
        application_refs: [{kind: 'rule_application', id: appA.id, revision: 1}, appBRef],
        rule_refs: [appA.rule_ref, {kind: 'supplemental_rule', id: '20000000-0000-4000-8000-000000000002', revision: 3}],
        event_refs: [{kind: 'research_event', id: '40000000-0000-4000-8000-000000000001', revision: 1}],
        bases: [
          {application_ref: {kind: 'rule_application', id: appA.id, revision: 1}, rule_ref: appA.rule_ref,
            severity: 'high', materiality: 'material', condition_result: 'triggered', evidence_status: 'pending',
            assessment_status: 'unresolved', impact_assessment: {status: 'pending', summary: '甲项需核对', evidence_refs: []},
            effects: [], release_requirements: [], diagnostics: []},
          {application_ref: appBRef, rule_ref: {kind: 'supplemental_rule', id: '20000000-0000-4000-8000-000000000002', revision: 3},
            event_ref: {kind: 'research_event', id: '40000000-0000-4000-8000-000000000001', revision: 2},
            severity: 'critical', materiality: 'material', condition_result: 'unknown', evidence_status: 'conflicted',
            assessment_status: 'inconclusive', impact_assessment: {status: 'pending', summary: '乙项存在冲突', evidence_refs: []},
            effects: [], release_requirements: [], diagnostics: ['synthetic-conflict']},
        ],
      }],
    };
    let next = 0;
    module.renderCurrentApplications(target, view, {pageIndex: 1, onPrevious: () => {}, onNext: () => { next += 1; }});
    const text = target.innerText;
    const frozenEvidence = target.querySelector('summary');
    const evidenceReference = [...target.querySelectorAll('li')].some((item) => item.textContent.includes('evidence:30000000-0000-4000-8000-000000000001@2'));
    target.querySelector('button[aria-label="下一页"], .supplemental-page-controls button:last-child')?.click();
    return {
      text, evidenceReference, next,
      groupCount: target.querySelectorAll('.supplemental-issue-group').length,
      basisCount: target.querySelectorAll('.supplemental-issue-basis').length,
      previousButton: [...target.querySelectorAll('button')].some((button) => button.textContent === '上一页'),
      nextButton: [...target.querySelectorAll('button')].some((button) => button.textContent === '下一页'),
      frozenEvidenceSummary: frozenEvidence?.textContent || '',
    };
  });
  assert.match(groupedProjection.text, /证券 600090；评估视角：company_research/);
  assert.match(groupedProjection.text, /第 2 页，本页 1 条。仅显示本页/);
  assert.equal(groupedProjection.groupCount, 1);
  assert.equal(groupedProjection.basisCount, 2, 'cross-page grouping must preserve each full-scope impact basis');
  assert.equal(groupedProjection.evidenceReference, true, 'saved evidence records must be rendered as exact evidence refs');
  assert.equal(groupedProjection.previousButton, true);
  assert.equal(groupedProjection.nextButton, true);
  assert.equal(groupedProjection.next, 1);
  const syntheticRunProjection = await page.evaluate(async () => {
    const module = await import('/app/static/supplemental_rules.js');
    const target = document.createElement('article');
    module.renderRunSummary(target, {operation: '合成批准', run_ref: {id: 77}}, {
      status: 'partial', completed_count: 2, declared_count: 6, coverage_complete: false, failure_code: 'item_failures',
      members: [
        {ordinal: 0, state: 'pending'}, {ordinal: 1, state: 'pending'}, {ordinal: 2, state: 'saved'},
        {ordinal: 3, state: 'item_failed'}, {ordinal: 4, state: 'pending'}, {ordinal: 5, state: 'saved'},
      ],
      scan_diagnostics: ['event_scan_truncated:at_least_limit'], unbound_event_refs: [],
    });
    return target.innerText;
  });
  assert.match(syntheticRunProjection, /待处理成员：0–1、4。/);
  assert.match(syntheticRunProjection, /失败成员：3。/);
  assert.match(syntheticRunProjection, /event_scan_truncated:at_least_limit/);
  assert.match(syntheticRunProjection, /失败代码：item_failures/);
  assert.match(syntheticRunProjection, /覆盖尚未完整/);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height: 950});
    await page.waitForTimeout(150);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert.equal(overflow, false, `supplemental panels must not overflow at ${width}px`);
  }
  await page.goto(base + '/app/supplemental-rules');
  await page.setViewportSize({width: 390, height: 950});
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth), false, 'management page must not overflow at 390px');
  await context.close();
  console.log('P2 supplemental management and read-only stock flow passed (1440px/390px).');
} catch (error) {
  console.error(error);
  console.error(serverLog);
  process.exitCode = 1;
} finally {
  await browser.close().catch(() => {});
  server.kill('SIGTERM');
  await new Promise((resolve) => server.once('exit', resolve));
  await rm(fixtureDir, {recursive: true, force: true});
}
