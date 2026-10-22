import { refreshBlockers } from "./refresh_state.js";
import { nativeCapabilities } from "./native_bridge.js";

const POLL_MS = 60_000;
const CONFIRM_MS = 5_000;
const IDLE_MS = 8_000;
const RELOAD_KEY = "zcharlie.release.reload.v1";

function controlValue(node) {
  if (node.type === "checkbox" || node.type === "radio") return String(node.checked);
  if (node.type === "file") return [...node.files].map(f => `${f.name}:${f.size}:${f.lastModified}`).join("|");
  if (node.tagName === "SELECT") return JSON.stringify([...node.selectedOptions].map(o => o.value));
  return node.isContentEditable ? node.textContent : node.value;
}

// Conservative for arbitrary forms: changing a field blocks background reload
// until reverted or the page explicitly acknowledges a save. Unmounting a
// control does not prove that its edit was saved (settings topics, for example).
// Do not infer a save from an unrelated successful HTTP request.
function trackEdits(doc) {
  const originals = new WeakMap(), dirty = new Set();
  const control = target => target?.closest?.("input,textarea,select,[contenteditable='true']");
  const remember = event => {
    const node = control(event.target);
    if (node && !originals.has(node)) originals.set(node, controlValue(node));
  };
  doc.addEventListener("focusin", remember, true);
  doc.addEventListener("pointerdown", remember, true);
  const changed = event => {
    const node = control(event.target);
    if (!node || node.type === "search" || node.id === "palette-input") return;
    dirty.add(node);
  };
  doc.addEventListener("input", changed, true);
  doc.addEventListener("change", changed, true);
  doc.addEventListener("release:form-saved", event => {
    for (const node of dirty) {
      if (event.target === node || event.target.contains?.(node)) {
        originals.set(node, controlValue(node)); dirty.delete(node);
      }
    }
  });
  return () => {
    for (const node of dirty) {
      const value = controlValue(node);
      // Emptying a previously populated field is also an unsaved edit.
      if (value === originals.get(node)) dirty.delete(node);
    }
    return dirty.size ? "有已编辑的表单，请先保存并切换页面，或还原输入" : "";
  };
}

function validManifest(value) {
  return value?.schema_version === 1 && typeof value.release_id === "string" &&
    /^[a-f0-9]{24}$/.test(value.release_id) && Array.isArray(value.supported_bridge_versions) &&
    value.supported_bridge_versions.every(Number.isInteger);
}

export function startReleaseMonitor() {
  const current = document.querySelector('meta[name="zcharlie-release"]')?.content;
  // A document predating this feature acquires it on its next navigation.
  if (!current) return;
  const edits = trackEdits(document);
  let lastInput = Date.now(), composing = false, pending = null, confirmed = false;
  let observedAt = 0;
  let inFlight = false, reachable = false, stopped = false, reloading = false;
  let pollTimer, checkTimer, uiTimer, controller, compatible = true;
  let banner, message, button;

  function show(text) {
    if (!banner) {
      banner = document.createElement("aside"); banner.className = "release-notice";
      banner.setAttribute("aria-label", "版本更新");
      message = document.createElement("span"); message.setAttribute("role", "status");
      button = document.createElement("button"); button.type = "button"; button.textContent = "安全刷新";
      // Re-check the server and all guards even on an explicit click.
      button.addEventListener("click", () => { void check(true); });
      banner.append(message, button); document.body.append(banner);
    }
    if (message.textContent !== text) message.textContent = text;
    banner.hidden = false;
  }

  function blockers(manual = false) {
    const reasons = refreshBlockers();
    const edited = edits(); if (edited) reasons.push(edited);
    if (composing) reasons.push("正在输入");
    if (document.querySelector('[aria-busy="true"], [data-refresh-blocked="true"]')) reasons.push("当前操作尚未完成");
    if (!manual && (window.zcOverlay?.isOpen() || document.activeElement?.matches("input,textarea,select,[contenteditable='true']"))) reasons.push("正在操作页面");
    if (!manual && Date.now() - lastInput < IDLE_MS) reasons.push("等待页面空闲");
    return reasons;
  }

  function recentlyReloaded() {
    try {
      const previous = JSON.parse(sessionStorage.getItem(RELOAD_KEY) || "null");
      // Also bounds alternating releases during a rolling deploy/rollback.
      return previous && Date.now() - previous.at < POLL_MS;
    } catch { return false; }
  }

  function maybeReload(manual = false) {
    if (!pending || reloading || stopped || document.hidden) return;
    if (!compatible) {
      show("当前安装版本暂不支持此更新，请更新应用后继续使用。"); button.disabled = true; return;
    }
    const reasons = blockers(manual);
    button && (button.disabled = !reachable || !confirmed || reasons.length > 0);
    if (!reachable) { show("新版已就绪，连接恢复后再刷新；当前内容已保留。"); button.disabled = true; return; }
    if (!confirmed) { show("检测到新版，正在确认发布状态…"); button.disabled = true; return; }
    if (reasons.length) {
      show(`新版已就绪，${reasons[0]}。`);
      button.disabled = blockers(true).length > 0;
      return;
    }
    if (recentlyReloaded()) { show("发布正在切换，稍后会自动刷新。"); button.disabled = true; return; }
    show("新版已就绪，正在刷新…"); button.disabled = true;
    try { sessionStorage.setItem(RELOAD_KEY, JSON.stringify({ at: Date.now(), target: pending.release_id })); }
    catch { /* Storage is optional; server-rendered release id remains the baseline. */ }
    reloading = true;
    window.location.reload();
  }

  async function check(manual = false) {
    if (stopped || inFlight || document.hidden || reloading) return;
    inFlight = true;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8_000);
    try {
      // Goes through the existing same-origin scheme transport on every APK;
      // never depends on a newly added native command or healthz caching.
      const response = await fetch(`/app/release?_=${Date.now()}`, {
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("release_unavailable");
      const manifest = await response.json();
      if (!validManifest(manifest)) throw new Error("release_invalid");
      if (stopped) return;
      reachable = true;
      if (manifest.release_id === current) {
        pending = null; confirmed = false; if (banner) banner.hidden = true;
        return;
      }
      const capabilities = document.body.dataset.embedded === "android" ? await nativeCapabilities() : null;
      if (stopped) return;
      compatible = !capabilities || manifest.supported_bridge_versions.includes(capabilities.bridge_version);
      if (pending?.release_id === manifest.release_id) confirmed = Date.now() - observedAt >= CONFIRM_MS;
      else { pending = manifest; confirmed = false; observedAt = Date.now(); }
      maybeReload(manual);
      if (!confirmed) { clearTimeout(checkTimer); checkTimer = setTimeout(() => { void check(); }, CONFIRM_MS); }
    } catch {
      reachable = false;
      // Offline, throttled, login redirect, old server, invalid JSON: keep the
      // current document and retry on the next tick/foreground transition.
      if (pending && !stopped) maybeReload();
    } finally {
      clearTimeout(timeout); inFlight = false;
    }
  }

  const activity = () => { lastInput = Date.now(); };
  for (const name of ["pointerdown", "keydown", "input", "change", "scroll"]) document.addEventListener(name, activity, { capture: true, passive: true });
  document.addEventListener("compositionstart", () => { composing = true; activity(); });
  document.addEventListener("compositionend", () => { composing = false; activity(); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { activity(); void check(); }
  });
  window.addEventListener("online", () => { void check(); });
  window.addEventListener("pagehide", () => {
    stopped = true; controller?.abort();
    clearInterval(pollTimer); clearInterval(uiTimer); clearTimeout(checkTimer);
  });
  function resume() {
    stopped = false;
    clearInterval(pollTimer); clearInterval(uiTimer);
    pollTimer = setInterval(() => { void check(); }, POLL_MS);
    uiTimer = setInterval(() => {
      // Revalidate availability immediately before reloading; a previous
      // successful probe may be stale after a long edit/task or a disconnect.
      if (pending && !document.hidden && !inFlight && !reloading) {
        if (reachable && confirmed && !blockers().length && !recentlyReloaded()) void check();
        else maybeReload();
      }
    }, 1_000);
    void check();
  }
  window.addEventListener("pageshow", event => { if (event.persisted) resume(); });
  resume();
}
