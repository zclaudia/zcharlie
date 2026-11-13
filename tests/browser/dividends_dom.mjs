import { test } from 'node:test';
import assert from 'node:assert/strict';
import '../support/next_iterations_dom.mjs';
import { renderDividends } from '../../src/investment_agent/web/static/dividends.js';

test('cash, platform and pending remain distinct, including zero and high payout', () => {
  const region = document.createElement('div');
  renderDividends(region, {
    as_of: '2026-11-13', cash: { value: 0, cash_per_share: 0, window_start: '2025-11-13', window_end: '2026-11-13', price: 10, price_date: '2026-11-13', provider: 'wmshare', quality: 'ok', flags: ['stale_price'] },
    events: [{ ex_date: '2026-06-01', dps: 1, pay_date: null, provider: 'wmshare' }],
    platform: { yield_ttm: 0.02, payout_ttm: 1.5, quote_date: '2026-11-13', captured_at: '2026-11-13T10:00:00Z' },
    pending: [{ dps: 0.5, announce: '2026-10-01', sas: '<script>' }], difference: { value: .02, points: 2 },
  });
  assert.match(region.textContent, /现金股息率：0\.00%/);
  assert.match(region.textContent, /派息率：150\.00%/);
  assert.match(region.textContent, /口径差异/);
  assert.match(region.textContent, /质量：正常/);
  assert.match(region.textContent, /质量标记：价格过期/);
  assert.match(region.textContent, /2\.00 个百分点/);
  assert.match(region.textContent, /抓取时间：2026-11-13 18:00/);
  assert.match(region.textContent, /2026 年（1 项）/);
  assert.match(region.textContent, /公告日 2026-10-01/);
  assert.equal(region.querySelector('script'), null);
});

test('missing values and no valid records are explicit', () => {
  const region = document.createElement('div');
  renderDividends(region, { cash: {}, events: [], pending: [], platform: null });
  assert.match(region.textContent, /现金股息率：未采到/);
  assert.match(region.textContent, /暂无有效分红记录/);
  assert.match(region.textContent, /尚未采到 wmshare 平台快照/);
  assert.equal(region.querySelector('.badge.warn'), null);
});
