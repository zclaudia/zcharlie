// WP2 标签 B（config-docs-plan §4.6）：真实 loopback HTTP + SQLite 的来源信任策略 UI 流。
// 覆盖：标签切换（惰性加载）、每来源一卡、许可审批（状态 / 公司 chip / 引用）、
// 验收报告行预检、必填备注、全文档 PATCH 保存 + 漂移横幅、共享历史 / 基线抽屉、
// 从基线导入回到基线、手机宽度。无外部数据源调用。
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = 19888, base = `http://127.0.0.1:${port}`;
const workdir = await mkdtemp(join(tmpdir(), 'ds-policy-ui-'));
const key = {'X-API-Key': 'ds-policy-fixture-key'};
const server = spawn(
  '.venv/bin/python', ['tests/browser/data_sources_policy_fixture_server.py', workdir, String(port)],
  {cwd: resolve('.'), stdio: ['ignore', 'pipe', 'pipe']});
let log = '', browser, succeeded = false;
server.stdout.on('data', x => { log += x; });
server.stderr.on('data', x => { log += x; });
const errors = [];

const HK = 'eastmoney.hk.native.v1';

async function openPolicyTab(width) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(`${base}/app/login`);
  await page.locator('[name="api_key"]').fill(key['X-API-Key']);
  await page.getByRole('button', {name: '登录', exact: true}).click();
  await page.goto(`${base}/app/data-sources`);
  await page.locator('#ds-form .ds-group-title').first().waitFor(); // 标签 A 先就绪
  await page.getByRole('tab', {name: '来源信任策略'}).click();
  await page.locator('#dsp-form .dsp-source-card').first().waitFor();
  return {context, page};
}

try {
  let ready = false;
  for (let i = 0; i < 180; i++) {
    if (server.exitCode !== null) throw new Error(log);
    try { if ((await fetch(`${base}/healthz`)).ok) { ready = true; break; } } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  assert.ok(ready, log);
  browser = await chromium.launch({headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE});
  const {context, page} = await openPolicyTab(1440);

  // 1) 两张来源卡（真实基线：sec.companyfacts.v1 + eastmoney.hk.native.v1，全部未审批）；
  //    头部展示受控记录标识（顶层 revision id）与修订号。
  const cards = await page.locator('#dsp-form .dsp-source-card').allInnerTexts();
  assert.equal(cards.length, 2, `unexpected cards: ${JSON.stringify(cards)}`);
  const hkCard = page.locator(`#dsp-form .dsp-source-card[data-source="${HK}"]`);
  const secCard = page.locator('#dsp-form .dsp-source-card[data-source="sec.companyfacts.v1"]');
  await hkCard.waitFor();
  await secCard.waitFor();
  assert.match(await page.locator('#dsp-meta').innerText(), /策略版本 hkus-source-policy-1 · 修订 0/);
  assert.equal(await page.locator('#dsp-dirty-count').innerText(), '没有未保存的修改');

  // 2) 审批 eastmoney 许可：状态 → approved、公司 chip、引用。
  await hkCard.getByLabel(`${HK} 许可状态`, {exact: true}).selectOption('approved');
  await hkCard.getByLabel(`${HK} 许可公司`, {exact: true}).fill('hsbc-holdings');
  await hkCard.getByRole('button', {name: `添加${HK} 许可公司`}).click();
  await hkCard.getByLabel(`${HK} 许可引用`, {exact: true}).fill('https://example.com/hk-license');
  assert.equal(await page.locator('#dsp-dirty-count').innerText(), '有未保存的修改');
  await page.locator(`#dsp-form .dsp-source-card[data-source="${HK}"].dirty`).waitFor();
  // 未动的来源卡没有脏标记。
  assert.ok(!(await secCard.getAttribute('class')).includes('dirty'), 'untouched card marked dirty');

  // 3) 空验收报告行被行内预检拦下；填好日期与引用后，缺备注仍被拦。
  await hkCard.getByRole('button', {name: `为 ${HK} 添加验收报告`}).click();
  await page.locator('#dsp-note').fill('审批 eastmoney');
  await page.locator('#dsp-save').click();
  await page.locator('#dsp-save-result').getByText('验收报告第 1 条缺少观测日期', {exact: false}).waitFor();
  await hkCard.getByLabel(`${HK} 报告 1 观测日期`, {exact: true}).fill('2026-10-12');
  await hkCard.getByLabel(`${HK} 报告 1 引用`, {exact: true}).fill('https://example.com/hk-report');
  await page.locator('#dsp-note').fill('');
  await page.locator('#dsp-save').click();
  await page.locator('#dsp-save-result').getByText('变更说明必填', {exact: false}).waitFor();

  // 4) 填备注保存：全文档 PATCH（expected_revision）→ 已保存、脏标记清零、漂移横幅出现。
  await page.locator('#dsp-note').fill('UI 流：审批 eastmoney 许可');
  await page.locator('#dsp-save').click();
  await page.waitForFunction(() => document.getElementById('dsp-dirty-count')?.innerText === '没有未保存的修改');
  await page.locator('#dsp-save-result').getByText('已保存。', {exact: false}).waitFor();
  assert.match(await page.locator('#dsp-meta').innerText(), /策略版本 hkus-source-policy-1 · 修订 1/);
  const detail = await (await page.request.get(`${base}/config-docs/source_policy`, {headers: key})).json();
  assert.equal(detail.revision, 1);
  assert.equal(detail.drifted, true);
  assert.deepEqual(detail.value.sources[HK].licensing, {
    status: 'approved', company_keys: ['hsbc-holdings'], citation: 'https://example.com/hk-license',
  });
  assert.deepEqual(detail.value.sources[HK].technical_acceptance.reports,
    [{observed_on: '2026-10-12', citation: 'https://example.com/hk-report'}]);
  // 未动的来源与顶层受控字段原样保留。
  assert.equal(detail.value.sources['sec.companyfacts.v1'].licensing.status, 'unknown');
  assert.equal(detail.value.revision, 'hkus-source-policy-1');
  assert.equal(detail.value.schema_version, 1);
  // chip 已渲染在卡上。
  await hkCard.locator('.dsp-key-chip').getByText('hsbc-holdings').waitFor();

  // 5) 漂移差异（共享基线抽屉）：基线 → 当前文档，逐项 old → new。
  await page.locator('#dsp-drift').getByText('存在漂移', {exact: false}).waitFor();
  await page.locator('#dsp-drift').getByRole('button', {name: '查看差异'}).click();
  const diff = page.locator('#ds-baseline-diff');
  await diff.locator('.ds-diff-item').first().waitFor();
  const diffText = await diff.innerText();
  assert.match(diffText, /许可 状态 unknown → approved/);
  assert.match(diffText, /许可 公司范围 （空） → hsbc-holdings/);
  assert.match(diffText, /技术验收 验收报告 0 条 → 1 条/);
  await page.keyboard.press('Escape');

  // 6) 共享历史抽屉：一条创建行（首次写入），备注可见。
  await page.locator('#dsp-history-open').click();
  const history = page.locator('#ds-history');
  await history.locator('.ds-history-item').first().waitFor();
  const historyText = await history.innerText();
  assert.match(historyText, /UI 流：审批 eastmoney 许可/);
  assert.match(historyText, /首次导入 \/ 创建文档/);
  await page.keyboard.press('Escape');

  // 7) 从基线导入（CD5）：导入说明必填；导入后回到基线、漂移消除。
  await page.locator('#dsp-baseline-open').click();
  await page.locator('#ds-baseline-diff .ds-diff-item').first().waitFor();
  await page.locator('#ds-baseline-import').click();
  await page.locator('#dsp-save-result').getByText('从基线导入需要填写导入说明', {exact: false}).waitFor();
  await page.locator('#ds-baseline-note').fill('UI 流：回到基线');
  await page.locator('#ds-baseline-import').click();
  await page.waitForFunction(() => document.getElementById('dsp-drift')?.hidden === true);
  const reverted = await (await page.request.get(`${base}/config-docs/source_policy`, {headers: key})).json();
  assert.equal(reverted.value.sources[HK].licensing.status, 'unknown');
  assert.deepEqual(reverted.value.sources[HK].licensing.company_keys, []);
  assert.equal(reverted.drifted, false);
  assert.equal(reverted.revision, 2);
  await page.locator('#dsp-save-result').getByText('已用基线文件覆盖当前文档', {exact: false}).waitFor();

  // 8) 标签切换保持各自状态；手机宽度无横向溢出。
  await page.getByRole('tab', {name: '字段来源优先级'}).click();
  await page.locator('#ds-form .ds-group-title').first().waitFor();
  assert.ok(await page.locator('#ds-panel-policy').isHidden(), 'policy panel should hide');
  await page.getByRole('tab', {name: '来源信任策略'}).click();
  await page.locator('#dsp-form .dsp-source-card').first().waitFor();
  assert.match(await page.locator('#dsp-meta').innerText(), /修订 2/);
  await page.setViewportSize({width: 390, height: 844});
  assert.ok(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), 'Mobile page overflows horizontally');
  await page.screenshot({path: '/tmp/zcharlie-data-sources-policy-mobile.png', fullPage: true});
  await context.close();

  assert.deepEqual(errors, []);
  console.log('WP2 Tab B source-policy UI flow passed (cards, approval, precheck, save, drift, history, baseline import)');
  succeeded = true;
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  await new Promise(r => { server.once('exit', r); setTimeout(r, 2000); });
  await rm(workdir, {recursive: true, force: true});
  if (!succeeded || (server.exitCode && server.exitCode !== 0)) console.error(log);
}
