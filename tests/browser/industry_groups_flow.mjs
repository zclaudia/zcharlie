// WP3（config-docs-plan §4.6）：真实 loopback HTTP + SQLite 的行业分组 UI 流。
// 覆盖：三段编辑区渲染（真实基线）、映射编辑 + 必填备注 + 保存自动 bump version、
// 漂移横幅 + 基线差异、覆盖率检查（完整 → 摘除映射后未映射 + 快速指派）、分组移除守卫。
// 无外部数据源调用。
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const {chromium} = require('playwright');
const port = 19889, base = `http://127.0.0.1:${port}`;
const workdir = await mkdtemp(join(tmpdir(), 'ig-ui-'));
const key = {'X-API-Key': 'ig-ui-fixture-key'};
const server = spawn('.venv/bin/python', ['tests/browser/industry_groups_fixture_server.py', workdir, String(port)],
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
  await page.goto(`${base}/app/industry-groups`);
  await page.locator('#ig-form .ig-section-title').first().waitFor();
  return {context, page};
}

function shanghaiToday() {
  return new Date().toLocaleDateString('sv-SE', {timeZone: 'Asia/Shanghai'});
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

  // 1) 三段编辑区 + 导航 + 真实基线数据。
  const titles = await page.locator('#ig-form .ig-section-title').allInnerTexts();
  assert.deepEqual(titles, ['分组定义', '映射表（CSRC 大类码 → 分组）', '申万 2021（先于 CSRC 表）']);
  assert.ok(await page.locator('nav a[href="/app/industry-groups"]').count() > 0);
  assert.ok((await page.locator('#ig-form tr[data-group]').count()) >= 10, 'baseline groups render');
  assert.ok((await page.locator('#ig-form tr[data-code]').count()) >= 40, 'baseline codes render');
  assert.ok((await page.locator('#ig-form tr[data-sw]').count()) >= 30, 'baseline shenwan rows render');
  assert.match(await page.locator('#ig-meta').innerText(), /修订 0 · 未导入/);
  assert.equal(await page.locator('#ig-save').isDisabled(), true);

  // 2) 映射编辑：J66 → other；未填备注保存被拦截；填备注后保存成功且 version 自动推进。
  const j66 = page.locator('tr[data-code="J66"]');
  await j66.locator('select').selectOption('other');
  assert.equal(await page.locator('#ig-save').isDisabled(), false);
  await page.locator('#ig-save').click();
  await page.locator('#ig-save-result').getByText('变更说明必填').waitFor();
  assert.equal(await page.locator('#ig-save-result .issue-list').count(), 0, 'no PATCH without a note');
  await page.locator('#ig-note').fill('J66 归入 other（UI 流测试）');
  await page.locator('#ig-save').click();
  await page.locator('#ig-save-result').getByText('已保存').waitFor();
  const today = shanghaiToday();
  assert.match(await page.locator('#ig-meta').innerText(), new RegExp(`版本 ${today}\\.1 · 修订 1`));
  assert.equal(await page.locator('#ig-save').isDisabled(), true, 'saved: clean again');

  // 3) 漂移横幅 + 基线差异（方向：基线 → 当前）。
  await page.locator('#ig-drift').waitFor({state: 'visible'});
  await page.locator('#ig-drift button').click();
  await page.locator('#ig-baseline-drawer .ig-diff-item').first().waitFor();
  const diff = await page.locator('#ig-baseline-drawer .ig-diff-item').allInnerTexts();
  assert.ok(diff.some(t => t.includes('映射 J66：banking → other')), diff.join(' | '));
  assert.ok(diff.some(t => t.startsWith('版本：')), diff.join(' | '));
  await page.locator('#ig-baseline-drawer [data-overlay-close]').click();

  // 4) 覆盖率检查：摘掉一个无覆写的一级默认（目录级检查不依赖成分数据）→
  //    检查发现缺口 → 快速“设默认”指回 → 恢复完整。
  await page.locator('#ig-coverage-run').click();
  await page.locator('#ig-coverage').getByText('覆盖完整').waitFor();
  const level1Rows = page.locator('#ig-form tr[data-sw]').filter({hasText: '一级'});
  let target = null, code = '';
  for (let i = 0; i < await level1Rows.count(); i++) {
    const row = level1Rows.nth(i);
    if (!(await row.locator('button').first().isDisabled())) { target = row; break; }
  }
  assert.ok(target, 'a child-free level-1 default exists');
  code = (await target.locator('.ig-code-cell').innerText()).trim();
  await target.locator('button', {hasText: '移除'}).click();
  await page.locator('#ig-coverage-run').click();
  const gap = page.locator('#ig-coverage .ig-gap', {hasText: code});
  await gap.locator('select').selectOption('consumer');
  await gap.locator('button', {hasText: '设默认'}).click();
  await page.locator('#ig-coverage').getByText('覆盖完整').waitFor();
  assert.equal(await page.locator(`#ig-form tr[data-sw="${code}"] select`).first().inputValue(), 'consumer',
    'quick-assign wrote the level-1 default back');

  // 5) 分组移除守卫（客户端面）：banking 仍被映射引用 → 按钮禁用并说明原因。
  const bankingRemove = page.locator('tr[data-group="banking"] button', {hasText: '移除'});
  assert.equal(await bankingRemove.isDisabled(), true);
  assert.match(await bankingRemove.getAttribute('title'), /引用/);

  // 6) 变更历史：一条 PATCH 记录（actor web + 备注）。
  await page.locator('#ig-history-open').click();
  await page.locator('#ig-history-drawer .ig-history-item').first().waitFor();
  const history = await page.locator('#ig-history-drawer .ig-history-item').allInnerTexts();
  assert.ok(history.some(t => t.includes('J66 归入 other')), history.join(' | '));

  await context.close();
  succeeded = true;
} finally {
  if (browser) await browser.close().catch(() => {});
  server.kill('SIGTERM');
  await rm(workdir, {recursive: true, force: true});
}
if (errors.length) throw new Error('page errors: ' + errors.join('\n'));
if (!succeeded) throw new Error(log);
console.log('industry_groups_flow: ok');
