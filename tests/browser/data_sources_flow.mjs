// WP1 标签 A（config-docs-plan §4.6）：真实 loopback HTTP + SQLite 的字段来源优先级 UI 流。
// 覆盖：分组渲染、只读证据面、primary / fallback / tolerance / key 编辑、必填备注、
// 保存后漂移横幅 + 基线差异、变更历史、高级编辑 YAML 保存。无外部数据源调用。
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = 19887, base = `http://127.0.0.1:${port}`;
const workdir = await mkdtemp(join(tmpdir(), 'ds-ui-'));
const key = {'X-API-Key': 'ds-ui-fixture-key'};
const server = spawn('.venv/bin/python', ['tests/browser/data_sources_fixture_server.py', workdir, String(port)],
  {cwd: resolve('.'), stdio: ['ignore', 'pipe', 'pipe']});
let log = '', browser, succeeded = false;
server.stdout.on('data', x => { log += x; });
server.stderr.on('data', x => { log += x; });
const errors = [];

async function openPage(width) {
  const context = await browser.newContext({viewport: {width, height: 1000}});
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  await page.goto(`${base}/app/login`);
  await page.locator('[name="api_key"]').fill(key['X-API-Key']);
  await page.getByRole('button', {name: '登录', exact: true}).click();
  await page.goto(`${base}/app/data-sources`);
  await page.locator('#ds-form .ds-group-title').first().waitFor();
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
  const {context, page} = await openPage(1440);

  // 1) 分组与只读证据面（真实基线 41 个字段；六个表都有中文组头）。
  const titles = await page.locator('#ds-form .ds-group-title').allInnerTexts();
  for (const label of ['财务', '派生', '行情', '披露逾期', '分红', '业绩事件']) {
    assert.ok(titles.some(t => t.startsWith(label + ' · ')), `missing group ${label}`);
  }
  const roeRow = page.locator('tr[data-field="roe"]');
  await roeRow.locator('.ds-chip-summary').getByText(/^校准/).waitFor();
  assert.match(await roeRow.innerText(), /净资产收益率|ROE|roeAvg|fraction/i);
  assert.ok(await page.locator('tr[data-field="eps_basic_fy"] .badge.mono').count() >= 1, 'formula chip missing');
  assert.match(await page.locator('#ds-meta').innerText(), /版本 .+ · 修订 \d+/);

  // 2) close（行情，baostock / tencent 都有校准记录）：primary 换 tencent。
  //    必填备注缺省 → 保存被拦；填了备注 → primary 与 fallback 重复被 422 拒（CD3 逐条提示）。
  const closeRow = page.locator('tr[data-field="close"]');
  await closeRow.getByLabel('close 主来源', {exact: true}).selectOption('tencent');
  assert.equal(await page.locator('#ds-dirty-count').innerText(), '有未保存的修改');
  await page.locator('#ds-save').click();
  await page.locator('#ds-save-result').getByText('变更说明必填', {exact: false}).waitFor();
  await page.locator('#ds-note').fill('UI 流：close 换 tencent 优先');
  await page.locator('#ds-save').click();
  await page.locator('#ds-save-result').getByText('a source is listed twice', {exact: false}).waitFor();

  // 3) 完成一次来源交换（表单可表达的完整路径）：fallback 加 baostock、移除与 primary
  //    重复的 tencent、容差清空（继承默认）、key 勾选 → 保存成功并按响应刷新。
  await closeRow.locator('.ds-fallback-add').selectOption('baostock');
  await closeRow.getByRole('button', {name: '移除 tencent'}).click();
  await closeRow.getByLabel('close 容差', {exact: true}).fill('');
  await closeRow.getByLabel('close 关键字段', {exact: true}).check();
  await page.locator('#ds-save').click();
  await page.waitForFunction(() => document.getElementById('ds-dirty-count')?.innerText === '没有未保存的修改');
  await page.locator('#ds-save-result').getByText('已保存。', {exact: false}).waitFor();
  assert.equal(await page.locator('#ds-dirty-count').innerText(), '没有未保存的修改');
  assert.match(await page.locator('#ds-meta').innerText(), /修订 1/);

  // 4) 保存后 API 与页面一致；漂移横幅出现，差异视图给出逐字段 old→new。
  const detail = await (await page.request.get(`${base}/config-docs/field_sources`, {headers: key})).json();
  assert.equal(detail.value.fields.close.primary, 'tencent');
  assert.deepEqual(detail.value.fields.close.fallback, ['baostock']);
  assert.ok(!('tolerance' in detail.value.fields.close), 'cleared tolerance should be absent');
  assert.equal(detail.value.fields.close.key, true);
  assert.equal(detail.drifted, true);
  await page.locator('#ds-drift').getByText('存在漂移', {exact: false}).waitFor();
  await page.locator('#ds-drift').getByRole('button', {name: '查看差异'}).click();
  const diff = page.locator('#ds-baseline-diff');
  await diff.locator('.ds-diff-item').first().waitFor();
  const diffText = await diff.innerText();
  assert.match(diffText, /close（行情）：主来源 baostock → tencent；备用顺序 tencent → baostock；容差 0\.001 → （未设置）；关键字段 （未设置） → 是/);
  await page.keyboard.press('Escape');

  // 5) 变更历史：首次写入是创建行（old 为空），备注与来源可见。
  await page.locator('#ds-history-open').click();
  const history = page.locator('#ds-history');
  await history.locator('.ds-history-item').first().waitFor();
  let historyText = await history.innerText();
  assert.match(historyText, /UI 流：close 换 tencent 优先/);
  assert.match(historyText, /首次导入 \/ 创建文档/);
  await page.keyboard.press('Escape');

  // 6) 高级编辑：编辑器承载全文档 YAML（含第 3 步的表单改动）；改 version 保存后生效。
  //    details 的 toggle 事件是异步任务，等内容真的填好再断言。
  await page.locator('#ds-advanced summary').click();
  const editor = page.locator('#ds-yaml');
  await page.waitForFunction(() => (document.getElementById('ds-yaml')?.value || '').length > 0);
  assert.match(await editor.inputValue(), /fields:\n  roe:/);
  assert.match(await editor.inputValue(), /primary: tencent/);
  const before = await editor.inputValue();
  await editor.fill(before.replace('version: 2026-10-30.1', 'version: 2026-11-01.1'));
  assert.equal(await page.locator('#ds-dirty-count').innerText(), '有未保存的修改');
  await page.locator('#ds-note').fill('UI 流：版本号推进');
  await page.locator('#ds-save').click();
  await page.waitForFunction(() => document.getElementById('ds-dirty-count')?.innerText === '没有未保存的修改');
  await page.locator('#ds-save-result').getByText('已保存。', {exact: false}).waitFor();
  const after = await (await page.request.get(`${base}/config-docs/field_sources`, {headers: key})).json();
  assert.equal(after.value.version, '2026-11-01.1');
  assert.equal(after.value.fields.close.primary, 'tencent', 'advanced edit dropped form edits');
  assert.deepEqual(after.value.fields.close.fallback, ['baostock']);

  // 6a) 第二条历史：old → new 摘要（1 处变更：版本）。
  await page.locator('#ds-history-open').click();
  await history.locator('.ds-history-item').nth(1).waitFor();
  historyText = await history.innerText();
  assert.match(historyText, /UI 流：版本号推进/);
  assert.match(historyText, /1 处变更/);
  await history.locator('.ds-history-details summary').first().click();
  await history.getByText('版本：2026-10-30.1 → 2026-11-01.1').waitFor();
  await page.keyboard.press('Escape');


  // 6b) 从基线导入（CD5）：差异先行、导入说明必填；导入后回到基线、漂移消除。
  await page.locator('#ds-baseline-open').click();
  await page.locator('#ds-baseline-diff .ds-diff-item').first().waitFor();
  await page.locator('#ds-baseline-import').click();
  await page.locator('#ds-save-result').getByText('从基线导入需要填写导入说明', {exact: false}).waitFor();
  await page.locator('#ds-baseline-note').fill('UI 流：回到基线');
  await page.locator('#ds-baseline-import').click();
  await page.waitForFunction(() => document.getElementById('ds-drift')?.hidden === true);
  const reverted = await (await page.request.get(`${base}/config-docs/field_sources`, {headers: key})).json();
  assert.equal(reverted.value.version, '2026-10-30.1');
  assert.equal(reverted.value.fields.close.primary, 'baostock');
  assert.equal(reverted.drifted, false);
  await page.locator('#ds-save-result').getByText('已用基线文件覆盖当前文档', {exact: false}).waitFor();

  // 7) 手机宽度无横向溢出；放弃修改回到已保存版本。
  await page.locator('#ds-advanced summary').click(); // 关闭高级编辑回表单
  await page.locator('tr[data-field="roe"]').getByLabel('roe 容差', {exact: true}).fill('0.5');
  await page.locator('#ds-discard').click();
  assert.equal(await page.locator('#ds-dirty-count').innerText(), '没有未保存的修改');
  await page.setViewportSize({width: 390, height: 844});
  assert.ok(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), 'Mobile page overflows horizontally');
  // 堆叠卡片不落在 46rem 的横向滚动容器里（stack-table 生效）。
  assert.ok(await page.evaluate(() => document.querySelector('.ds-groups table').getBoundingClientRect().width <= innerWidth),
    'stacked table wider than viewport');
  await page.screenshot({path: '/tmp/zcharlie-data-sources-mobile.png', fullPage: true});
  await context.close();

  assert.deepEqual(errors, []);
  console.log('WP1 Tab A data-sources UI flow passed (groups, evidence, edit, save, drift, history, advanced)');
  succeeded = true;
} finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  await new Promise(r => { server.once('exit', r); setTimeout(r, 2000); });
  await rm(workdir, {recursive: true, force: true});
  if (!succeeded || (server.exitCode && server.exitCode !== 0)) console.error(log);
}
