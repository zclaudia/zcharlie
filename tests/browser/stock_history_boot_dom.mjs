import { test } from 'node:test';
import assert from 'node:assert/strict';
import '../support/next_iterations_dom.mjs';

window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const response = (body) => new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });

test('stock page boot without a selection shows latest saved research', async () => {
  history.replaceState({}, '', '/app/stocks/600519.SH');
  document.body.innerHTML = '<section id="stock-detail" data-ticker="600519.SH"></section><div class="data-region" id="stock-history" data-endpoint="/research/stocks/600519.SH/history"></div>';
  const calls = [];
  globalThis.fetch = async (path) => {
    calls.push(path);
    if (path.includes('/history')) return response([{ run_id: 900, as_of: '2026-01-01' }, { run_id: 796, as_of: '2025-01-01' }]);
    if (path === '/research/runs/900') return response({ run_id: 900, status: 'success', result: { ticker: '600519.SH', as_of: '2026-01-01', score: { items: [{ investment_score: 88, strategy_label: '最新策略' }], metadata: {} } } });
    if (path.endsWith('/changes')) return response({ baseline: true, changes: [] });
    throw new Error(`unexpected ${path}`);
  };
  await import('../../src/investment_agent/web/static/app.js');
  document.dispatchEvent(new Event('DOMContentLoaded'));
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.match(document.querySelector('#stock-detail').textContent, /最新策略/);
  assert.equal(document.querySelector('#stock-historical-banner'), null);
  assert.ok(calls.includes('/research/runs/900'));
  assert.ok(!calls.includes('/research/runs/796'));
});
