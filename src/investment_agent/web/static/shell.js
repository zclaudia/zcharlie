// 外壳脚本（module）：全局搜索面板（⌘K / Ctrl+K）、侧栏/底部栏的数量与数据状态、顶栏滚动阴影。
// 覆盖层的开关与主题在 nav.js（classic，head 里先执行）；这里只在 DOM 里找已有节点，找不到就静默跳过。
// 页面脚本可以 import { openPalette } 或直接用 window.zcOverlay 打开自己的抽屉 / 弹层。

import { get } from "./api.js";
import { clear, el } from "./dom.js";
import { DEBOUNCE_MS, searchStocks } from "./combobox.js";

const STATUS_TAGS = { suspended: "停牌", delisted: "退市" };
const COUNTS_KEY = "zc-shell-counts";
const COUNTS_TTL_MS = 90 * 1000;

// ---- 全局搜索面板 ------------------------------------------------------------

const palette = document.getElementById("palette");
const input = document.getElementById("palette-input");
const stockGroup = document.getElementById("palette-stock-group");
const stockBox = document.getElementById("palette-stocks");
const note = document.getElementById("palette-note");

let activeIndex = -1;
let searchTimer = null;
let requestId = 0;

function allItems() {
  if (!palette) return [];
  return [...palette.querySelectorAll(".palette-item")].filter((node) => !node.hidden);
}

function setActive(index) {
  const items = allItems();
  if (!items.length) {
    activeIndex = -1;
    if (input) input.removeAttribute("aria-activedescendant");
    return;
  }
  activeIndex = ((index % items.length) + items.length) % items.length;
  items.forEach((node, position) => {
    const on = position === activeIndex;
    node.classList.toggle("is-active", on);
    node.setAttribute("aria-selected", on ? "true" : "false");
    if (on) {
      if (node.id) input?.setAttribute("aria-activedescendant", node.id);
      node.scrollIntoView({ block: "nearest" });
    }
  });
}

function filterPages(query) {
  const needle = query.trim().toLowerCase();
  let shown = 0;
  palette?.querySelectorAll("[data-palette-page]").forEach((node) => {
    const haystack = `${node.getAttribute("data-k") || ""} ${node.textContent}`.toLowerCase();
    node.hidden = needle !== "" && !haystack.includes(needle);
    if (!node.hidden) shown += 1;
  });
  const group = document.getElementById("palette-pages");
  if (group) group.hidden = shown === 0;
}

function showNote(text) {
  if (!note) return;
  note.textContent = text || "";
  note.hidden = !text;
}

function stockOption(item, index) {
  const link = el("a", {
    class: "palette-item",
    role: "option",
    id: `palette-stock-${index}`,
    href: `/app/stocks/${encodeURIComponent(item.ticker)}`,
    "aria-selected": "false",
  });
  link.appendChild(el("span", { class: "palette-name" }, item.name || item.ticker));
  link.appendChild(el("span", { class: "mono palette-code" }, item.ticker));
  const tags = el("span", { class: "palette-tags" });
  const status = STATUS_TAGS[item.status];
  if (status) tags.appendChild(el("span", { class: "badge warn" }, status));
  if (item.on_watchlist) tags.appendChild(el("span", { class: "badge accent" }, "观察池"));
  else if (item.in_universe) tags.appendChild(el("span", { class: "badge" }, "目标池"));
  link.appendChild(tags);
  return link;
}

function renderStocks(items) {
  if (!stockBox || !stockGroup) return;
  clear(stockBox);
  items.forEach((item, index) => stockBox.appendChild(stockOption(item, index)));
  stockGroup.hidden = items.length === 0;
}

function resetStocks() {
  if (stockBox) clear(stockBox);
  if (stockGroup) stockGroup.hidden = true;
  showNote("");
}

async function runSearch(query) {
  const mine = ++requestId;
  try {
    const items = await searchStocks(query);
    if (mine !== requestId) return;
    renderStocks(Array.isArray(items) ? items : []);
    showNote(Array.isArray(items) && items.length === 0 ? "没有匹配的股票" : "");
  } catch {
    if (mine !== requestId) return;
    renderStocks([]);
    showNote("股票搜索暂不可用");
  }
  setActive(0);
}

function onInput() {
  const query = input.value;
  filterPages(query);
  window.clearTimeout(searchTimer);
  if (query.trim() === "") {
    requestId += 1;
    resetStocks();
    setActive(-1);
    return;
  }
  setActive(0);
  searchTimer = window.setTimeout(() => runSearch(query.trim()), DEBOUNCE_MS);
}

function onKeydown(event) {
  if (event.isComposing) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    setActive(activeIndex + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    setActive(activeIndex - 1);
  } else if (event.key === "Enter") {
    const items = allItems();
    const target = items[activeIndex] || items[0];
    if (target) {
      event.preventDefault();
      window.location.assign(target.getAttribute("href"));
    }
  }
}

export function openPalette() {
  if (!palette || !window.zcOverlay) return;
  if (input) input.value = "";
  filterPages("");
  resetStocks();
  setActive(-1);
  window.zcOverlay.open(palette);
  input?.focus();
}

function wirePalette() {
  if (!palette || !input) return;
  input.addEventListener("input", onInput);
  input.addEventListener("keydown", onKeydown);
  palette.addEventListener("mousemove", (event) => {
    const item = event.target.closest?.(".palette-item");
    if (!item) return;
    const index = allItems().indexOf(item);
    if (index >= 0 && index !== activeIndex) setActive(index);
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest?.("[data-palette-open]")) {
      event.preventDefault();
      openPalette();
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (palette.classList.contains("is-open")) window.zcOverlay?.close();
      else openPalette();
    }
  });
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || "");
  document.querySelectorAll("[data-shortcut-hint]").forEach((node) => {
    node.textContent = mac ? "⌘K" : "Ctrl K";
  });
}

// ---- 数量与数据状态（沿用 /dashboard/today，失败静默） -----------------------------

function readCache() {
  try {
    const raw = window.sessionStorage.getItem(COUNTS_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (parsed && Date.now() - parsed.at < COUNTS_TTL_MS) return parsed.data;
  } catch {
    /* 存储不可用或内容损坏：当作没有缓存 */
  }
  return null;
}

function writeCache(data) {
  try {
    window.sessionStorage.setItem(COUNTS_KEY, JSON.stringify({ at: Date.now(), data }));
  } catch {
    /* 忽略 */
  }
}

function setText(selector, text) {
  document.querySelectorAll(selector).forEach((node) => {
    node.textContent = text;
  });
}

function toggle(selector, visible) {
  document.querySelectorAll(selector).forEach((node) => {
    node.hidden = !visible;
  });
}

function applyCounts(data) {
  if (!data || typeof data !== "object") return;
  const open = data.candidates && Number.isFinite(data.candidates.open) ? data.candidates.open : 0;
  setText('[data-nav-count="candidates"]', String(open));
  toggle('[data-nav-count="candidates"]', open > 0);
  setText('[data-tab-badge="funnel"]', String(open));
  toggle('[data-tab-badge="funnel"]', open > 0);

  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const errors = data.data_health
    ? data.data_health.errors
    : warnings.filter((item) => item.level === "error").length;
  const warns = data.data_health
    ? data.data_health.warnings
    : warnings.filter((item) => item.level === "warn").length;
  const total = errors + warns;
  const summary = `${errors} 错误 · ${warns} 警告`;
  document.querySelectorAll('[data-nav-dot="health"]').forEach((node) => {
    node.hidden = total === 0;
    node.setAttribute("title", summary);
  });
  const lead = warnings.find((item) => item.level === "error") || warnings[0];
  const showStatus = total > 0 && Boolean(lead);
  setText("[data-shell-status-title]", lead ? lead.message : "");
  setText("[data-shell-status-sub]", summary);
  document.querySelectorAll("[data-shell-status], [data-shell-status-row]").forEach((node) => {
    node.hidden = !showStatus;
    if (showStatus && lead && lead.link) node.setAttribute("href", lead.link);
  });
}

async function loadCounts() {
  const cached = readCache();
  if (cached) {
    applyCounts(cached);
    return;
  }
  try {
    const data = await get("/dashboard/today?window=10");
    writeCache(data);
    applyCounts(data);
  } catch {
    /* 没有数量也不影响导航 */
  }
}

// ---- 顶栏滚动阴影 --------------------------------------------------------------

function wireTopbar() {
  const bar = document.getElementById("topbar");
  if (!bar) return;
  const update = () => bar.classList.toggle("is-scrolled", window.scrollY > 4);
  window.addEventListener("scroll", update, { passive: true });
  update();
}

// ---- 安卓 App 内：系统返回键先关覆盖层 -------------------------------------------
// 覆盖层打开时压一条历史记录，返回键（popstate）就只关覆盖层；从界面关闭时再 history.back()
// 把这条记录消费掉。切换覆盖层（先关后开，同步发生）不消费；点弹层里的链接跳转也不消费。

function wireEmbeddedBack() {
  if (!document.body.dataset.embedded || !window.zcOverlay) return;
  let pushed = false;
  let leaving = false;
  document.addEventListener("overlay:open", () => {
    if (pushed) return;
    pushed = true;
    window.history.pushState({ zcOverlay: true }, "");
  });
  document.addEventListener("overlay:close", () => {
    queueMicrotask(() => {
      if (!pushed || leaving || window.zcOverlay.isOpen()) return;
      pushed = false;
      window.history.back();
    });
  });
  window.addEventListener("popstate", () => {
    if (!pushed) return;
    pushed = false;
    // 返回键的"收起最上层"是一个可扩展契约：zcOverlay 弹层是默认订阅者，
    // 页面自有层级（如 AI 页的会话/档案抽屉）可订阅 overlay:back 收自己。
    document.dispatchEvent(new CustomEvent("overlay:back"));
  });
  document.addEventListener("click", (event) => {
    if (window.zcOverlay.isOpen() && event.target.closest?.(".is-open a[href]")) leaving = true;
  }, true);
  window.addEventListener("pageshow", () => { leaving = false; });
}

// zcOverlay 是 overlay:back 的默认订阅者；已有页面的返回键行为不变。
document.addEventListener("overlay:back", () => {
  if (window.zcOverlay?.isOpen()) window.zcOverlay.close();
});

// ---- 安卓 App 内：系统返回键（AppPlugin 管线）---------------------------------
// MainActivity 有两条 BACK 管线：activity 层直接分流（注入按键）与 dispatcher 上的
// AppPlugin 回调（手势/导航条——有 JS 监听器时 trigger 给我们，消费掉）。这里注册
// back-button 监听承接后者：关弹层 → 研究台内逐级返回 → 根部请原生最小化
// （AppPlugin 已消费 BACK，JS 无法自行退栈）。与 wireEmbeddedBack 的历史条目语义一致。
function wireAndroidBack() {
  if (document.body.dataset.embedded !== "android") return;
  const internals = window.__TAURI_INTERNALS__;
  if (!internals?.invoke || !internals.transformCallback) return;
  const branch = () => {
    if (window.zcOverlay && window.zcOverlay.isOpen()) { window.zcOverlay.close(); return; }
    if (location.pathname !== "/app" && history.length > 1) { history.back(); return; }
    internals.invoke("move_task_to_back").catch(() => {});
  };
  try {
    const callback = internals.transformCallback(branch);
    internals.invoke("plugin:app|register_listener", {
      event: "back-button",
      handler: "__CHANNEL__:" + callback,
    }).catch(() => {});
  } catch { /* 桥不可用时 BACK 走 activity 层按键管线 */ }
}

wirePalette();
wireTopbar();
wireEmbeddedBack();
wireAndroidBack();
loadCounts();
