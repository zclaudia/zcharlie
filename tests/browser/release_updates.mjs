// Chromium: backend releases arrive on an unchanged APK without losing work.
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire(process.env.AG17_NODE_MODULES + '/package.json');
const { chromium } = require('playwright');
const temp = await mkdtemp(join(tmpdir(), 'release-browser-'));
const port = 19857, base = `http://127.0.0.1:${port}`;
const server = spawn('.venv/bin/python', ['tests/browser/agent_fixture_server.py'], {
  env: { ...process.env, AG17_FIXTURE_DIR: temp, AG17_FIXTURE_PORT: String(port) },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let logs = '', browser;
server.stdout.on('data', b => logs += b); server.stderr.on('data', b => logs += b);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(test, label) {
  for (let i = 0; i < 300; i++) { if (await test()) return; await delay(50); }
  throw new Error(label + '\n' + logs);
}
const A = 'a'.repeat(24), B = 'b'.repeat(24), C = 'c'.repeat(24);
const errors = [];
try {
  await until(async () => {
    if (server.exitCode !== null) throw new Error(logs);
    try { return (await fetch(base + '/healthz')).ok; } catch { return false; }
  }, 'fixture startup');
  browser = await chromium.launch({ headless: true, executablePath: process.env.AG17_CHROMIUM_EXECUTABLE });

  async function fixture({ path = '/app', native = 'legacy', viewport = { width: 390, height: 844 } } = {}) {
    const context = await browser.newContext({ viewport, extraHTTPHeaders: { 'X-API-Key': 'ag17-fixture-key' } });
    await context.addInitScript(mode => {
      globalThis.__TAURI_INTERNALS__ = { invoke: async command => {
        if (command === 'bridge_capabilities' && mode !== 'legacy') return {
          bridge_version: mode === 'future' ? 2 : 1, capabilities: [],
        };
        throw `Command ${command} not found`;
      } };
    }, native);
    const page = await context.newPage(); page.on('pageerror', error => errors.push(String(error)));
    const state = { served: A, published: A, requests: 0, loads: 0, status: 200, versions: [0, 1] };
    await page.route('**/app/release?*', async route => {
      state.requests++;
      await route.fulfill({ status: state.status, contentType: 'application/json', body: JSON.stringify({
        schema_version: 1, release_id: state.published, supported_bridge_versions: state.versions,
      }) });
    });
    await page.route(/\/app(?:\?[^/]*)?$|\/app\/agent(?:\?.*)?$/, async route => {
      const response = await route.fetch(); state.loads++;
      await route.fulfill({ response, body: (await response.text()).replace(
        /name="zcharlie-release" content="[^"]+"/, `name="zcharlie-release" content="${state.served}"`,
      ) });
    });
    await page.clock.install(); await page.goto(base + path);
    await until(() => state.requests > 0, 'initial release probe');
    await delay(100);
    const advance = async ms => { await page.clock.runFor(ms); await delay(100); };
    const publish = async () => {
      state.published = B; state.served = B;
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await until(async () => await page.locator('.release-notice').isVisible(), 'new release notice');
      await advance(5_100);
    };
    return { page, state, context, advance, publish };
  }

  // Both APK generations: detect a release, load fresh HTML, then stay stable.
  for (const native of ['legacy', 'modern']) {
    const f = await fixture({ native });
    await f.publish(); await f.advance(10_000);
    await until(() => f.state.loads === 2, `${native} automatic refresh`);
    await f.advance(70_000); assert.equal(f.state.loads, 2, 'no reload loop');
    await f.context.close();
  }

  // Dirty forms and IME compositions survive publication; clearing allows refresh.
  {
    const f = await fixture();
    await f.page.evaluate(() => {
      const form = document.createElement('form'); form.innerHTML = '<textarea id="release-draft"></textarea>';
      document.body.append(form);
    });
    await f.page.locator('#release-draft').fill('尚未提交的内容');
    await f.page.locator('#release-draft').blur();
    await f.publish(); await f.advance(20_000);
    assert.equal(f.state.loads, 1); assert.equal(await f.page.locator('#release-draft').inputValue(), '尚未提交的内容');
    assert.match(await f.page.locator('.release-notice').innerText(), /已编辑的表单/);
    await f.page.locator('#release-draft').fill(''); await f.page.locator('#release-draft').blur();
    await f.page.evaluate(() => document.dispatchEvent(new CompositionEvent('compositionstart')));
    await f.advance(20_000); assert.equal(f.state.loads, 1);
    await f.page.evaluate(() => document.dispatchEvent(new CompositionEvent('compositionend')));
    await f.advance(10_000); await until(() => f.state.loads === 2, 'cleared form refresh');
    await f.context.close();
  }

  // Failed probes retain current content; reconnecting resumes a confirmed release.
  {
    const f = await fixture();
    await f.publish(); f.state.status = 503;
    await f.advance(20_000); assert.equal(f.state.loads, 1);
    assert.match(await f.page.locator('.release-notice').innerText(), /连接恢复/);
    f.state.status = 200;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await until(() => f.state.loads === 2, 'reconnect refresh');
    await f.context.close();
  }

  // A release disappearing during confirmation must never reload the document.
  {
    const f = await fixture();
    f.state.published = B;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await until(async () => await f.page.locator('.release-notice').isVisible(), 'candidate release');
    f.state.published = A;
    await f.advance(20_000); assert.equal(f.state.loads, 1);
    assert.equal(await f.page.locator('.release-notice').isVisible(), false);
    // Rollback is also a release change, not a monotonically increasing number.
    f.state.published = C; f.state.served = C;
    await f.page.evaluate(() => window.dispatchEvent(new Event('online')));
    await f.advance(5_100); await f.advance(10_000);
    await until(() => f.state.loads === 2, 'rollback reload');
    await f.context.close();
  }

  // Capability policy rejects an incompatible native generation gracefully.
  {
    const f = await fixture({ native: 'future' });
    await f.publish(); await f.advance(20_000); assert.equal(f.state.loads, 1);
    assert.match(await f.page.locator('.release-notice').innerText(), /更新应用/);
    await f.context.close();
  }

  // API work and page-specific guards prevent automatic refresh until released.
  {
    const f = await fixture({ viewport: { width: 1440, height: 1000 } });
    await f.page.evaluate(async () => {
      const base = document.querySelector('script[src$="/shell.js"]').src;
      const { holdRefresh, registerRefreshGuard } = await import(new URL('./refresh_state.js', base));
      window.releaseRequest = holdRefresh('正在提交操作');
      window.releaseTask = registerRefreshGuard('test-task', () => '有进行中的研究任务');
    });
    await f.publish(); await f.advance(20_000); assert.equal(f.state.loads, 1);
    await f.page.evaluate(() => window.releaseRequest());
    await f.advance(10_000); assert.equal(f.state.loads, 1);
    await f.page.evaluate(() => window.releaseTask());
    await f.advance(2_000); await until(() => f.state.loads === 2, 'guards released');
    await f.context.close();
  }

  // Restored AI drafts also block updates (no input event was fired on restore).
  {
    const f = await fixture({ path: '/app/agent' });
    await until(async () => !(await f.page.locator('#agent-new').isDisabled()), 'agent initialization');
    await f.page.locator('#agent-input').fill('恢复后的草稿');
    await f.page.reload(); await delay(100);
    assert.equal(await f.page.locator('#agent-input').inputValue(), '恢复后的草稿');
    const initial = f.state.loads;
    await f.publish(); await f.advance(20_000);
    assert.equal(f.state.loads, initial);
    assert.match(await f.page.locator('.release-notice').innerText(), /草稿/);
    await f.context.close();
  }
  // Deleting a populated value and unmounting its control are still edits.
  {
    const f = await fixture();
    await f.page.evaluate(() => {
      const input = document.createElement('input'); input.id = 'existing-value'; input.value = '原值';
      document.body.append(input);
    });
    await f.page.locator('#existing-value').fill(''); await f.page.locator('#existing-value').blur();
    await f.publish(); await f.advance(20_000); assert.equal(f.state.loads, 1);
    await f.page.evaluate(() => document.querySelector('#existing-value').remove());
    await f.advance(20_000); assert.equal(f.state.loads, 1);
    await f.context.close();
  }
  // Hidden pages stay put; foreground resumes probing and the idle grace period.
  {
    const f = await fixture();
    await f.page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    f.state.served = B; f.state.published = B;
    const probes = f.state.requests;
    await f.advance(70_000); assert.equal(f.state.loads, 1); assert.equal(f.state.requests, probes);
    await f.page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await until(async () => await f.page.locator('.release-notice').isVisible(), 'foreground probe');
    await f.advance(5_100); await f.advance(10_000);
    await until(() => f.state.loads === 2, 'foreground refresh');
    await f.context.close();
  }
  assert.deepEqual(errors, []);
  console.log('release updates: 10 Chromium scenarios passed (old/new APK, forms, IME, offline, rollback, compatibility, guards, restored drafts, removed controls, foreground)');
} finally {
  await browser?.close(); server.kill('SIGTERM');
  await new Promise(resolve => server.once('exit', resolve));
  await rm(temp, { recursive: true, force: true });
}
