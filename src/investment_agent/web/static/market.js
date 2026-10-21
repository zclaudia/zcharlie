// 日线读取（迷你图与价格图的数据源，接口见 api/routes/market.py）。
// 列表页（候选、观察池）一次拉整页的迷你图：内嵌安卓端每个请求都要经网关往返一次，
// 逐行请求会把一页放大成几十个请求。
import { get } from "./api.js";

// 与后端 MAX_BATCH_TICKERS 一致；超过就分批，批次并发。
export const MAX_BATCH_TICKERS = 100;

export function loadBars(ticker, days) {
  return get(`/market/${encodeURIComponent(ticker)}/bars?days=${days}`);
}

// tickers → Map（大写代码 → { ticker, found, bars }）；未知标的 found=false、bars 为空。
export async function loadBarsMany(tickers, days) {
  const unique = [...new Set(tickers.filter(Boolean).map((ticker) => ticker.toUpperCase()))];
  const chunks = [];
  for (let i = 0; i < unique.length; i += MAX_BATCH_TICKERS) chunks.push(unique.slice(i, i + MAX_BATCH_TICKERS));
  const pages = await Promise.all(
    chunks.map((chunk) => get(`/market/bars?tickers=${encodeURIComponent(chunk.join(","))}&days=${days}`))
  );
  const byTicker = new Map();
  for (const page of pages) {
    for (const item of (page && page.items) || []) byTicker.set(item.ticker, item);
  }
  return byTicker;
}

// 页面里所有 data-spark 占位的日线，一次（分批）取回；失败时返回空 Map，迷你图留空。
export async function loadSparkBars(root, days) {
  const tickers = [...root.querySelectorAll("[data-spark]")].map((node) => node.getAttribute("data-spark"));
  if (!tickers.length) return new Map();
  try {
    return await loadBarsMany(tickers, days);
  } catch {
    return new Map();
  }
}
