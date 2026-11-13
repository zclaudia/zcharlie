import { test } from 'node:test';
import assert from 'node:assert/strict';
import '../support/next_iterations_dom.mjs';
import { loadStockDetail, loadStockHistory, selectedStockRun } from '../../src/investment_agent/web/static/stock_page.js';

window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

const detail = (ticker = '600519.SH') => ({
  run_id: 796, status: 'success', result: { ticker, as_of: '2025-01-01', score: { items: [{ investment_score: 72, strategy_label: '历史策略' }], metadata: {} } },
});
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
function setup() {
  document.body.innerHTML = '<section id="stock-detail" data-ticker="600519.SH"></section><div id="stock-history"></div>';
  return document.querySelector('#stock-detail');
}

test('history links select the saved run on this stock page', async () => {
  setup();
  await loadStockHistory(document.querySelector('#stock-history'), [{ run_id: 796, as_of: '2025-01-01' }]);
  assert.equal(document.querySelector('#stock-history a').getAttribute('href'), '/app/stocks/600519.SH?run_id=796');
  history.replaceState({}, '', '?run_id=796');
  assert.equal(selectedStockRun(), 796);
  history.replaceState({}, '', '?run_id=invalid');
  assert.equal(selectedStockRun(), 0);
  history.replaceState({}, '', '/');
  assert.equal(selectedStockRun(), null);
});

test('selected historical run loads directly, including outside history page', async () => {
  const region = setup();
  const calls = [];
  globalThis.fetch = async (path) => { calls.push(path); return response(detail()); };
  await loadStockDetail(region, { run_id: 796 }, { historical: true });
  assert.deepEqual(calls, ['/research/runs/796', '/research/runs/796/changes']);
  assert.match(region.textContent, /历史研究 #796.*2025-01-01/);
  assert.match(region.textContent, /历史策略/);
  assert.equal(region.querySelector('#stock-historical-banner a').getAttribute('href'), '/app/stocks/600519.SH');
  assert.equal(globalThis.stockHistoricalRun.run_id, 796);
});

test('missing, wrong-stock, unscored and malformed selections do not show latest', async () => {
  for (const candidate of [detail('000001.SZ'), { run_id: 796, result: null }, null]) {
    const region = setup();
    globalThis.fetch = async () => candidate ? response(candidate) : response({ detail: 'not found' }, 404);
    await loadStockDetail(region, { run_id: 796 }, { historical: true });
    assert.equal(region.querySelector('#stock-verdict'), null);
    assert.match(region.textContent, /无法读取|不属于/);
    assert.match(region.textContent, /返回最新研究/);
    assert.equal(globalThis.stockHistoricalRun, null);
  }
  const region = setup();
  globalThis.fetch = async () => { throw new Error('unexpected fetch'); };
  await loadStockDetail(region, { run_id: 0 }, { historical: true });
  assert.match(region.textContent, /编号无效/);
});

test('peer panel uses selected historical binding and never asks for latest history', async () => {
  history.replaceState({}, '', '?run_id=796');
  document.body.innerHTML = '<section id="peer-context-section" data-ticker="600519.SH"><div id="peer-context-current"></div><div id="peer-context-bound"></div><aside id="peer-context-evidence"></aside></section>';
  delete globalThis.stockHistoricalRun;
  const calls = [];
  globalThis.fetch = async (path) => { calls.push(path); return response({ status: 'ready', metrics: [] }); };
  const { bootIndustryPeers } = await import('../../src/investment_agent/web/static/industry_peers.js');
  delete document.querySelector("#peer-context-section").dataset.peerMounted;
  const mounted = bootIndustryPeers();
  globalThis.stockHistoricalRun = { run_id: 796, industry_peer_context: { batch_run_id: 123 } };
  await mounted.loadResearchBinding();
  assert.ok(calls.some((path) => path.includes('batch_run_id=123')));
  assert.ok(!calls.some((path) => path.includes('/history')));
  assert.match(document.querySelector('#peer-context-bound').textContent, /运行 #796/);
  history.replaceState({}, '', '/');
});

test('peer binding handles selection before and after startup without latest fallback', async () => {
  history.replaceState({}, '', '?run_id=796');
  const { bootIndustryPeers } = await import('../../src/investment_agent/web/static/industry_peers.js');
  const calls = [];
  globalThis.fetch = async (path) => { calls.push(path); return response({ status: 'ready', metrics: [] }); };
  const makeSection = () => {
    document.body.innerHTML = '<section id="peer-context-section" data-ticker="600519.SH"><div id="peer-context-current"></div><div id="peer-context-bound"></div><aside id="peer-context-evidence"></aside></section>';
    return document.querySelector('#peer-context-bound');
  };
  // The detail completed before the peer module mounted.
  globalThis.stockHistoricalRun = { run_id: 796, industry_peer_context: null };
  let bound = makeSection();
  bootIndustryPeers();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.match(bound.textContent, /运行 #796.*未保存同行参照/);
  assert.ok(!calls.some((path) => path.includes('/history') || path.includes('batch_run_id=')));
  // The peer module started first and receives the validated detail later.
  delete globalThis.stockHistoricalRun;
  calls.length = 0;
  bound = makeSection();
  bootIndustryPeers();
  globalThis.stockHistoricalRun = { run_id: 796, industry_peer_context: { batch_run_id: 456 } };
  document.dispatchEvent(new CustomEvent('stock-historical-run', { detail: globalThis.stockHistoricalRun }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.match(bound.textContent, /运行 #796/);
  assert.ok(calls.some((path) => path.includes('batch_run_id=456')));
  assert.ok(!calls.some((path) => path.includes('/history')));
  history.replaceState({}, '', '/');
});
