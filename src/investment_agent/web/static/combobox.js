// 股票搜索下拉（mrd-intake D4）：输入 → 防抖 200ms → GET /stocks/search →
// role="listbox" 的 <ul>；↑↓ 移动、回车选中、Esc 关闭；选中把 ticker 回填并回调 onSelect(item)。
// 只读本地字典（代码前缀 / 名称子串 / 拼音首字母），不解释任何数据；DOM 全部经 dom.js 构造。

import { get } from "./api.js";
import { clear, el } from "./dom.js";

export const DEBOUNCE_MS = 200;
export const SEARCH_LIMIT = 10;
const STATUS_TAGS = { suspended: "停牌", delisted: "退市" };
let sequence = 0;

export function searchStocks(query, limit = SEARCH_LIMIT) {
  return get(`/stocks/search?q=${encodeURIComponent(query)}&limit=${limit}`);
}

// 把 input 包进 .combo 容器并挂上下拉；返回 { close, destroy }。
// fetch(query) 须返回 Promise<[{ticker, name, status, in_universe, on_watchlist}]>。
export function attachCombobox(input, { fetch = searchStocks, onSelect, minChars = 1 } = {}) {
  const host = el("div", { class: "combo" });
  input.parentNode.insertBefore(host, input);
  host.appendChild(input);
  sequence += 1;
  const listId = `${input.name || "combo"}-${sequence}-listbox`;
  const list = el("ul", { class: "combo-list", role: "listbox", id: listId });
  list.hidden = true;
  host.appendChild(list);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-controls", listId);
  input.setAttribute("autocomplete", "off");

  let items = [];
  let active = -1;
  let timer = null;
  let requestId = 0;

  function close() {
    items = [];
    active = -1;
    clear(list);
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function open() {
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function highlight(index) {
    active = index;
    [...list.children].forEach((node, position) => {
      const on = position === active;
      if (on) node.classList.add("is-active");
      else node.classList.remove("is-active");
      if (node.getAttribute("role") === "option") node.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (active >= 0) input.setAttribute("aria-activedescendant", `${listId}-${active}`);
    else input.removeAttribute("aria-activedescendant");
  }

  function option(item, index) {
    const node = el("li", {
      role: "option",
      id: `${listId}-${index}`,
      "data-index": index,
      "aria-selected": "false",
    });
    node.appendChild(el("span", { class: "combo-ticker" }, item.ticker));
    node.appendChild(el("span", { class: "combo-name" }, item.name || ""));
    const status = STATUS_TAGS[item.status];
    if (status) node.appendChild(el("span", { class: "combo-tag combo-tag-warn" }, status));
    if (item.in_universe) node.appendChild(el("span", { class: "combo-tag" }, "目标池"));
    if (item.on_watchlist) node.appendChild(el("span", { class: "combo-tag" }, "自选"));
    return node;
  }

  function render(results) {
    items = Array.isArray(results) ? results : [];
    clear(list);
    if (!items.length) {
      list.appendChild(el("li", { class: "combo-empty", role: "presentation" }, "无匹配；可直接输入代码。"));
    } else {
      items.forEach((item, index) => list.appendChild(option(item, index)));
    }
    open();
    highlight(-1);
  }

  function renderNotice(text) {
    items = [];
    clear(list);
    list.appendChild(el("li", { class: "combo-empty", role: "presentation" }, text));
    open();
    highlight(-1);
  }

  function select(index) {
    const item = items[index];
    if (!item) return;
    input.value = item.ticker;
    close();
    if (typeof onSelect === "function") onSelect(item);
  }

  async function search(query) {
    requestId += 1;
    const id = requestId;
    let results;
    try {
      results = await fetch(query);
    } catch (error) {
      if (id === requestId) renderNotice(`搜索失败：${error && error.message ? error.message : "请稍后重试"}`);
      return;
    }
    if (id !== requestId || input.value.trim() !== query) return;
    render(results);
  }

  function schedule() {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < minChars) {
      requestId += 1;
      close();
      return;
    }
    timer = setTimeout(() => search(query), DEBOUNCE_MS);
  }

  function onKeydown(event) {
    if (list.hidden) {
      if (event.key === "ArrowDown" && input.value.trim().length >= minChars) {
        event.preventDefault();
        clearTimeout(timer);
        search(input.value.trim());
      }
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (items.length) highlight((active + 1) % items.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (items.length) highlight((active - 1 + items.length) % items.length);
    } else if (event.key === "Enter") {
      if (items.length) {
        event.preventDefault();
        select(active >= 0 ? active : 0);
      }
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  }

  function onListMousedown(event) {
    event.preventDefault(); // 保持输入框焦点，blur 不会先于 click 关闭下拉
  }

  function onListClick(event) {
    const target = event.target && event.target.closest ? event.target.closest("[data-index]") : null;
    if (target) select(Number(target.getAttribute("data-index")));
  }

  function onBlur() {
    setTimeout(close, 120);
  }

  input.addEventListener("input", schedule);
  input.addEventListener("keydown", onKeydown);
  input.addEventListener("blur", onBlur);
  list.addEventListener("mousedown", onListMousedown);
  list.addEventListener("click", onListClick);
  const form = input.form || null;
  if (form) form.addEventListener("reset", close);

  return {
    close,
    destroy() {
      clearTimeout(timer);
      close();
      input.removeEventListener("input", schedule);
      input.removeEventListener("keydown", onKeydown);
      input.removeEventListener("blur", onBlur);
      list.removeEventListener("mousedown", onListMousedown);
      list.removeEventListener("click", onListClick);
      if (form) form.removeEventListener("reset", close);
    },
  };
}
