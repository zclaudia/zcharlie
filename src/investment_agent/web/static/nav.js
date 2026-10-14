// 外壳基础行为（classic 脚本，放在 <head> 里，先于首帧执行）：
//   1. 主题：在渲染前把 localStorage 里的 light/dark 写到 <html data-theme>；
//      没有保存过就跟随系统（CSS 的 prefers-color-scheme）。
//   2. 侧栏折叠状态（<html data-sidebar="collapsed">），同样在渲染前恢复。
//   3. 覆盖层（底部弹层 / 右侧抽屉 / 搜索面板 + 遮罩）：window.zcOverlay.open(idOrEl) / close()。
//      声明式用法：任何元素 [data-overlay-open="目标id"] 点击即打开，[data-overlay-close] 点击即关闭。
//   4. 手机上输入框聚焦时隐藏底部 4 栏（<html class="kbd-open">）。
// 所有 localStorage 读写都包在 try/catch 里；脚本不依赖其他模块，app.js 出错时导航仍可用。
(function () {
  var root = document.documentElement;
  root.classList.add("nav-js");

  var THEME_KEY = "zc-theme";
  var SIDEBAR_KEY = "zc-sidebar";

  function readStore(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }
  function writeStore(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* 隐私模式 / 存储被禁用 */ }
  }

  var savedTheme = readStore(THEME_KEY);
  if (savedTheme === "light" || savedTheme === "dark") root.setAttribute("data-theme", savedTheme);
  if (readStore(SIDEBAR_KEY) === "collapsed") root.setAttribute("data-sidebar", "collapsed");

  function effectiveTheme() {
    var set = root.getAttribute("data-theme");
    if (set === "light" || set === "dark") return set;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  // ---- 覆盖层 --------------------------------------------------------------

  var current = null;
  var lastFocus = null;

  function getScrim() { return document.getElementById("scrim"); }

  function syncTriggers(target, open) {
    if (!target || !target.id) return;
    var triggers = document.querySelectorAll('[aria-controls="' + target.id + '"]');
    for (var i = 0; i < triggers.length; i += 1) triggers[i].setAttribute("aria-expanded", open ? "true" : "false");
  }

  function focusables(container) {
    return container.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
  }

  function openOverlay(target) {
    if (typeof target === "string") target = document.getElementById(target.replace(/^#/, ""));
    if (!target) return;
    if (current && current !== target) closeOverlay(true);
    if (current === target) return;
    lastFocus = document.activeElement;
    current = target;
    target.classList.add("is-open");
    var scrim = getScrim();
    if (scrim) scrim.classList.add("is-open");
    root.classList.add("overlay-open");
    syncTriggers(target, true);
    var first = target.querySelector("[data-autofocus]") || focusables(target)[0];
    if (first) first.focus({ preventScroll: true });
    target.dispatchEvent(new CustomEvent("overlay:open", { bubbles: true }));
  }

  function closeOverlay(keepFocus) {
    if (!current) return;
    var target = current;
    current = null;
    target.classList.remove("is-open");
    var scrim = getScrim();
    if (scrim) scrim.classList.remove("is-open");
    root.classList.remove("overlay-open");
    syncTriggers(target, false);
    if (keepFocus !== true && lastFocus && document.contains(lastFocus) && lastFocus.focus) {
      lastFocus.focus({ preventScroll: true });
    }
    target.dispatchEvent(new CustomEvent("overlay:close", { bubbles: true }));
  }

  window.zcOverlay = {
    open: openOverlay,
    close: function () { closeOverlay(false); },
    isOpen: function () { return current !== null; },
  };

  // ---- 事件 ----------------------------------------------------------------

  function setSidebar(collapsed) {
    if (collapsed) root.setAttribute("data-sidebar", "collapsed");
    else root.removeAttribute("data-sidebar");
    writeStore(SIDEBAR_KEY, collapsed ? "collapsed" : "expanded");
    var btn = document.getElementById("sb-collapse");
    if (btn) {
      btn.setAttribute("aria-pressed", collapsed ? "true" : "false");
      btn.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
    }
  }

  function setTheme(theme) {
    root.setAttribute("data-theme", theme);
    writeStore(THEME_KEY, theme);
    var toggles = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < toggles.length; i += 1) toggles[i].setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  }

  var TEXT_INPUT = /^(text|search|email|url|tel|password|number|date|datetime-local|month|time|week)?$/i;
  function isTextField(node) {
    if (!node || !node.tagName) return false;
    if (node.tagName === "TEXTAREA") return true;
    if (node.tagName === "INPUT") return TEXT_INPUT.test(node.getAttribute("type") || "");
    return node.isContentEditable === true;
  }

  function init() {
    var collapseBtn = document.getElementById("sb-collapse");
    if (collapseBtn) {
      var collapsed = root.getAttribute("data-sidebar") === "collapsed";
      collapseBtn.setAttribute("aria-pressed", collapsed ? "true" : "false");
      collapseBtn.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
      collapseBtn.addEventListener("click", function () {
        setSidebar(root.getAttribute("data-sidebar") !== "collapsed");
      });
    }

    var toggles = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < toggles.length; i += 1) {
      toggles[i].setAttribute("aria-pressed", effectiveTheme() === "dark" ? "true" : "false");
    }

    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var themeBtn = target.closest("[data-theme-toggle]");
      if (themeBtn) {
        setTheme(effectiveTheme() === "dark" ? "light" : "dark");
        return;
      }
      var opener = target.closest("[data-overlay-open]");
      if (opener) {
        event.preventDefault();
        openOverlay(opener.getAttribute("data-overlay-open"));
        return;
      }
      if (target.closest("[data-overlay-close]") || target.id === "scrim") {
        closeOverlay(false);
        return;
      }
      // 点了弹层里的链接：先收起弹层，页面随后跳转（bfcache 返回时也不会残留）
      if (current && target.closest("a[href]") && current.contains(target)) closeOverlay(true);
    });

    document.addEventListener("keydown", function (event) {
      if (!current) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeOverlay(false);
      } else if (event.key === "Tab") {
        var items = focusables(current);
        if (!items.length) return;
        var first = items[0];
        var last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });

    window.addEventListener("pageshow", function () { closeOverlay(true); });

    // 手机键盘弹起时让出底部 4 栏（CSS 里 .kbd-open 隐藏 .tabbar 并把 --tabbar-h 置 0）
    document.addEventListener("focusin", function (event) {
      if (isTextField(event.target)) root.classList.add("kbd-open");
    });
    document.addEventListener("focusout", function () {
      window.setTimeout(function () {
        if (!isTextField(document.activeElement)) root.classList.remove("kbd-open");
      }, 0);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
