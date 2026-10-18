import { randomUUID } from "./uuid.js";
// Only public AG09 contracts. Drafts and immutable unacknowledged bodies survive reload.
// AI page shell: conversation list / thread / research drawer, plus portfolio and controls tabs.
import { get, post, FETCH_MARKER_HEADERS } from "./api.js";
import { el } from "./dom.js";
import { renderAnswer } from "./agent_answer.js";
import { formatDate, formatDateTime } from "./format.js";
const TASK = {queued:"排队中",running:"进行中",waiting_data:"等待数据",waiting_user:"等你处理",succeeded:"已完成",partial:"部分完成",failed:"失败",cancelled:"已取消"};
const STOP = {answered:"已回答",evidence_blocked:"证据受阻",no_progress:"无进展",budget_exhausted:"预算用完",cancelled:"已取消",failed:"失败"};
const OPINION = {published:"有效",review_required:"待复核",expired:"已过期",superseded:"已被替代",withdrawn:"已撤回"};
const TERMINAL = new Set(["succeeded","partial","failed","cancelled"]);
const KEY = "zcharlie.agent.workbench.v1";
const TABS = ["chat","portfolio","controls"];
const PANES = ["list","chat","drawer"];
// 处理方式：界面文案；值与后端 research_kind 一一对应。
const MODES = [
  {value:"auto", label:"自动", title:"自动（推荐）", desc:"能用已有材料回答就直接答；需要新取证时在本次额度内执行"},
  {value:"explanation", label:"只解释已有材料", title:"只解释已有材料", desc:"不做新取证，只说明已保存的判断和依据"},
  {value:"local_check", label:"局部核查", title:"局部核查", desc:"只查一个具体问题，不改动整体公司判断"},
  {value:"company_research", label:"完整公司研究", title:"完整公司研究", desc:"围绕整家公司系统取证，受本次额度限制"},
  {value:"comparison", label:"比较", title:"比较明确对象", desc:"需要两个以上对象；只回答先研究哪家，不做总分排名"},
  {value:"review", label:"复盘", title:"复盘", desc:"回看当时的假设、复核是否按时触发、哪一步出了错"},
  {sep:true},
  {value:"portfolio_action", label:"组合建议", title:"组合建议", desc:"需要先绑定一个组合快照"},
];
// need：问题需要的证券数；core 为窄屏保留的 4 个（2×2）。
const STARTERS = [
  {group:"一家公司", text:"这家公司为什么值得关注？", need:1, core:true},
  {group:"一家公司", text:"这次下跌改变了什么？", need:1, core:true},
  {group:"几家之间", text:"比较这两家，先研究哪家？", need:2, core:true},
  {group:"复盘", text:"你上次的判断哪里错了？", need:0, core:true},
  {group:"一家公司", text:"你为什么不同意我？", need:1},
  {group:"一家公司", text:"现在价格隐含了什么预期？", need:1},
  {group:"复盘", text:"哪些判断快到复核日期？", need:0},
];
const SUBJECT_KINDS = new Set(["listing","case"]);
const PORTFOLIO_KINDS = new Set(["portfolio_snapshot","portfolio_plan"]);
export function bootAgent() {
  const root = document.querySelector("#agent-workbench");
  if (!root) return;
  const $ = (id) => root.querySelector(`#agent-${id}`);
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch { saved = {}; }
  const state = saved && typeof saved === "object" ? saved : {};
  state.drafts ||= {};
  state.labels ||= {};
  state.ui ||= {};
  let current = null, generation = 0, searchGeneration = 0, creating = false, searchTimer, autoCompare = false;
  let conversations = [], archivedConvs = [], messages = [], refs = [], stops = [], updateTimer;
  let showArchived = !!state.ui.archived;
  const locks = new Set(), tasks = new Map(), opinionViews = new Map();
  function noticeText(text) { $("notice").textContent = text; $("notice").hidden = !text; }
  const notice = noticeText;
  function persist() {
    try { sessionStorage.setItem(KEY, JSON.stringify(state)); return true; }
    catch { notice("浏览器无法保存草稿与待确认请求；请保留本页，恢复存储后再发送。"); return false; }
  }
  function draft(id = current) { return state.drafts[id || "new"] ||= {text:"",kind:"auto",refs:[],pending:null}; }
  function saveDraft() {
    const d = draft(); d.text = $("input").value; d.kind = $("kind").value; d.refs = refs;
    const ok = persist();
    $("draft-state").textContent = ok && d.text.trim() ? "草稿已保存" : "";
  }

  // ---- 页签、区域与档案抽屉 -------------------------------------------------
  function urlFor(id = current) {
    const p = new URLSearchParams();
    if (id) p.set("conversation", id);
    if (root.dataset.tab !== "chat") p.set("tab", root.dataset.tab);
    const text = p.toString();
    return `/app/agent${text ? `?${text}` : ""}`;
  }
  function setTab(tab, {focus = false, url = true} = {}) {
    if (!TABS.includes(tab)) tab = "chat";
    root.dataset.tab = tab;
    root.querySelectorAll("[data-agent-tab]").forEach(button => {
      const on = button.dataset.agentTab === tab;
      button.setAttribute("aria-selected", String(on)); button.tabIndex = on ? 0 : -1;
      if (on && focus) button.focus();
    });
    root.querySelectorAll("[data-panel]").forEach(panel => { panel.hidden = panel.dataset.panel !== tab; });
    if (url) history.replaceState(null, "", urlFor());
  }
  // 窄屏：会话是左侧抽屉、档案与「＋」是底部面板，共用一层遮罩。
  const narrow = () => window.matchMedia("(max-width: 56rem)").matches;
  // ---- 安卓内嵌：会话抽屉 / 档案抽屉 /「＋」面板各算一层，系统返回键先收层 ----
  // 复用 shell.js 的 overlay 历史契约：层打开时压一条历史记录（overlay:open），
  // 返回键（popstate → overlay:back）只收层，从界面关闭时消费这条记录
  // （overlay:close）。paneLevel 区分"BACK 收层"与"界面关闭"，避免双重导航。
  let paneLevel = false;
  const embeddedNarrow = () => document.body.dataset.embedded === "android" && narrow();
  function enterBackLevel() {
    if (paneLevel || !embeddedNarrow()) return;
    paneLevel = true;
    document.dispatchEvent(new CustomEvent("overlay:open"));
  }
  function exitBackLevel() {
    if (!paneLevel) return;
    paneLevel = false;
    document.dispatchEvent(new CustomEvent("overlay:close"));
  }
  // 当前是否还有未收起的层：「＋」面板优先于 pane（二者不会同时挂历史条目）。
  const agentLevelOpen = () => root.dataset.sheet === "plus" || root.dataset.pane !== "chat";
  document.addEventListener("overlay:back", () => {
    if (!paneLevel) return;
    paneLevel = false;
    if (root.dataset.sheet === "plus") openPlus(false);
    else setPane("chat");
  });
  function setPane(pane) {
    if (!PANES.includes(pane)) pane = "chat";
    if (pane !== "chat") openPlus(false);
    root.dataset.pane = pane;
    const scroller = pane === "list" ? root.querySelector(".ai-side") : pane === "drawer" ? $("artifact-panel") : null;
    if (scroller) scroller.scrollTop = 0;
    if (!embeddedNarrow()) return;
    if (pane !== "chat") enterBackLevel();
    else if (!root.dataset.sheet) exitBackLevel();
  }
  function setDrawer(open) {
    root.dataset.drawer = open ? "open" : "closed";
    const toggle = $("drawer-toggle");
    toggle.setAttribute("aria-expanded", String(open)); toggle.querySelector("span").textContent = open ? "收起档案" : "展开档案";
    state.ui.drawer = open ? "open" : "closed"; persist();
  }
  root.querySelectorAll("[data-agent-tab]").forEach(button => {
    button.addEventListener("click", () => setTab(button.dataset.agentTab));
    button.addEventListener("keydown", event => {
      const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (step) { event.preventDefault(); setTab(TABS[(TABS.indexOf(root.dataset.tab) + step + TABS.length) % TABS.length], {focus:true}); }
    });
  });
  root.querySelectorAll("[data-agent-pane]").forEach(button => button.addEventListener("click", () => setPane(button.dataset.agentPane)));
  root.querySelectorAll("[data-agent-go]").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.agentGo === "new") createConversation().catch(showError); else setPane(button.dataset.agentGo);
  }));
  root.querySelectorAll("[data-agent-tab-go]").forEach(button => button.addEventListener("click", () => { setPane("chat"); setTab(button.dataset.agentTabGo); }));
  root.querySelectorAll("[data-agent-tab-back]").forEach(button => button.addEventListener("click", () => setTab("chat")));
  root.querySelector("[data-agent-mainnav]")?.addEventListener("click", () => { setPane("chat"); document.querySelector(".nav-toggle")?.click(); });
  root.querySelector("[data-agent-plus-close]").addEventListener("click", () => openPlus(false));
  root.querySelector("[data-agent-plus-portfolio]").addEventListener("click", () => { openPlus(false); setTab("portfolio"); });
  $("scrim").addEventListener("click", () => { openPlus(false); setPane("chat"); });
  root.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !narrow() || (root.dataset.pane === "chat" && !root.dataset.sheet)) return;
    openPlus(false); setPane("chat");
  });
  $("conv-filter").addEventListener("input", () => renderList());
  $("drawer-toggle").addEventListener("click", () => setDrawer(root.dataset.drawer !== "open"));
  $("drawer-wide").addEventListener("click", () => {
    const panel = $("artifact-panel"), wide = panel.dataset.wide !== "true";
    panel.dataset.wide = String(wide); $("drawer-wide").setAttribute("aria-pressed", String(wide)); $("drawer-wide").textContent = wide ? "还原" : "加宽";
  });

  // ---- 对象、处理方式与草稿 ---------------------------------------------------
  const subjectCount = () => refs.filter(r => SUBJECT_KINDS.has(r.kind)).length;
  const hasPortfolio = () => refs.some(r => PORTFOLIO_KINDS.has(r.kind));
  function refLabel(r) {
    const hit = state.labels[`${r.kind}:${r.id}:${r.revision}`];
    return hit ? `${hit.name} · ${hit.ticker} · 版本 ${r.revision}` : `${r.kind === "listing" ? "证券名称待恢复" : r.kind === "case" ? "档案" : "候选"} #${r.id} · 版本 ${r.revision}`;
  }
  function remember(hit) { state.labels[`listing:${hit.ref.id}:${hit.ref.revision}`] = {name:hit.name,ticker:hit.ticker}; persist(); }
  function modeLabel() {
    const mode = MODES.find(m => m.value === $("kind").value);
    return `${mode ? mode.label : "自动"}${autoCompare ? "（按对象数识别）" : ""}`;
  }
  function syncMode() {
    const kind = $("kind"), count = subjectCount();
    if (kind.value === "auto" && count >= 2) { kind.value = "comparison"; autoCompare = true; }
    else if (kind.value === "comparison" && count < 2) { kind.value = "auto"; autoCompare = false; }
    $("mode-label").textContent = modeLabel();
    $("mode-hint").textContent = kind.value === "comparison" && autoCompare
      ? "已识别为比较：只回答先研究哪家，不做总分排名；口径不一致时会提示。"
      : "对象与版本在发送时固定，切换只影响下一条消息。普通提问不会自动启动完整公司研究。";
  }
  function renderModePop() {
    const pop = $("mode-pop"), count = subjectCount();
    pop.replaceChildren();
    for (const mode of MODES) {
      if (mode.sep) { pop.append(el("div", {class:"ai-opt-sep", role:"separator"})); continue; }
      let reason = "";
      if (mode.value === "comparison" && count < 2) reason = `需要两个以上对象 · 现在选了 ${count} 个`;
      if (mode.value === "portfolio_action" && !hasPortfolio()) reason = "需要先绑定一个组合快照";
      const option = el("button", {type:"button", class:"ai-opt", role:"option", "aria-selected":String($("kind").value === mode.value), "data-mode":mode.value});
      option.disabled = !!reason;
      option.append(el("i", {class:"ai-opt-r", "aria-hidden":"true"}), el("span", {}));
      option.lastChild.append(el("b", {}, mode.title), el("small", {}, reason || mode.desc));
      option.addEventListener("click", () => {
        $("kind").value = mode.value; autoCompare = false; saveDraft(); binding(); openMode(false); $("input").focus();
      });
      pop.append(option);
    }
  }
  function openPlus(open, focus) {
    if (open) renderModePop();
    if (open) root.dataset.sheet = "plus"; else delete root.dataset.sheet;
    $("picker").hidden = !open; $("mode-pop").hidden = !open;
    $("add-object").setAttribute("aria-expanded", String(open)); $("mode-btn").setAttribute("aria-expanded", String(open));
    if (open && focus === "mode") $("mode-pop").scrollIntoView({block:"nearest"});
    if (!embeddedNarrow()) return;
    if (open) enterBackLevel();
    else if (!agentLevelOpen()) exitBackLevel();
  }
  function openMode(open) {
    if (narrow()) { openPlus(open, "mode"); return; }
    if (open) { openPicker(false); renderModePop(); }
    $("mode-pop").hidden = !open; $("mode-btn").setAttribute("aria-expanded", String(open));
  }
  function openPicker(open) {
    if (narrow()) { openPlus(open, "search"); return; }
    if (open) openMode(false);
    $("picker").hidden = !open; $("add-object").setAttribute("aria-expanded", String(open));
    if (open) $("search").focus();
  }
  function renderChips() {
    const box = $("binding"); box.replaceChildren();
    if (!refs.length) { box.append(el("span", {class:"ai-chip ai-chip-empty"}, "未指定证券")); return; }
    for (const ref of refs) {
      const chip = el("span", {class:"ai-chip ai-chip-accent"}); chip.append(el("span", {class:"ai-chip-t"}, refLabel(ref)));
      const remove = el("button", {type:"button", class:"ai-chip-x", "aria-label":`移除 ${refLabel(ref)}`}, "✕");
      remove.addEventListener("click", () => {
        refs = refs.filter(r => !(r.kind === ref.kind && r.id === ref.id && r.revision === ref.revision));
        saveDraft(); binding();
      });
      chip.append(remove); box.append(chip);
    }
  }
  function binding() {
    syncMode();
    root.dispatchEvent(new CustomEvent("agent:binding", {detail:{conversationId:current,refs}}));
    renderChips();
    const pending = draft().pending;
    $("pending").hidden = !pending;
    if (pending) {
      let note = $("pending").querySelector(".ai-pending-refs");
      if (!note) { note = el("span", {class:"ai-pending-refs ai-faint"}); $("pending").insertBefore(note, $("retry")); }
      note.textContent = `原请求固定的对象：${pending.context_refs.map(refLabel).join("；") || "无证券绑定"}`;
    }
    $("retry").hidden = !pending;
    $("send").disabled = (!!current && locks.has(current)) || !!pending || creating || isArchived(current);
    $("retry").disabled = !current || locks.has(current);
  }
  function restoreDraft() {
    const d = draft(); refs = d.refs || []; $("input").value = d.text || ""; $("kind").value = d.kind || "auto";
    autoCompare = false; $("draft-state").textContent = d.text && d.text.trim() ? "草稿已保存" : ""; binding();
  }
  async function resolveLabels(values, id, token) {
    const listings = [...new Map(values.filter(r => r.kind === "listing").map(r => [r.id,r])).values()];
    for (const r of listings) {
      try { const hit = await get(`/agent/listings/${r.id}`); remember(hit); }
      catch { /* Keep explicit unavailable identity until the next restore. */ }
    }
    if (isCurrent(id,token)) { binding(); renderMessages(); }
  }
  root.addEventListener("agent:portfolio", event => {
    if (draft().pending || locks.has(current)) { notice("请先确认正在发送的原请求。"); return; }
    const ref = event.detail.ref;
    refs = [...refs.filter(r => !PORTFOLIO_KINDS.has(r.kind)), ref];
    state.labels[`${ref.kind}:${ref.id}:${ref.revision}`] = {name:event.detail.label,ticker:"组合快照"};
    $("kind").value = "portfolio_action"; autoCompare = false; saveDraft(); binding();
    setTab("chat"); setPane("chat"); $("input").focus();
  });

  function stopWatching() { stops.forEach(stop => stop()); stops = []; clearTimeout(updateTimer); tasks.clear(); }
  function isCurrent(id, token) { return current === id && generation === token; }

  // ---- 会话列表 ---------------------------------------------------------------
  function groupOf(value) {
    const day = formatDate(typeof value === "string" ? value : "");
    const today = formatDate(new Date().toISOString());
    const week = formatDate(new Date(Date.now() - 6 * 86400000).toISOString());
    if (day === today) return "今天";
    return day >= week ? "最近 7 天" : "更早";
  }
  function conversationStatus(c) {
    if (c.id !== current) return null;
    const live = [...taskStates.values()].filter(t => !TERMINAL.has(t.status));
    if (live.some(t => t.status === "waiting_user")) return {cls:"ai-dot-wait", text:"等你处理"};
    if (live.length) return {cls:"ai-dot-run", text:"进行中"};
    return null;
  }
  const taskStates = new Map();
  const isArchived = id => !!id && archivedConvs.some(c => c.id === id);
  const byNewest = (a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0);
  // 归档会话的清理倒计时（purge_at 由后端给出）。
  function purgeText(c) {
    const days = Math.ceil((Date.parse(c.purge_at) - Date.now()) / 86400000);
    return days > 1 ? `${days} 天后清理` : "即将清理";
  }
  // 一行 = 选择会话的主按钮 + 归档/恢复操作；data-conversation-id 与 .ai-conv 保持在主按钮上。
  function conversationRow(c, archived) {
    const row = el("div", {class:`ai-conv-row${archived ? " ai-conv-archived" : ""}`});
    const button = el("button", {type:"button", class:"ai-conv", "aria-current":c.id === current ? "true" : undefined, "data-conversation-id":c.id});
    const meta = el("span", {class:"ai-conv-meta"});
    const status = archived ? null : conversationStatus(c);
    if (status) { const dot = el("span", {}, ""); dot.className = `ai-dot ${status.cls}`; meta.append(dot, el("span", {}, status.text)); }
    // Today's items show the time, older ones the date (Beijing time via format.js).
    const stamp = formatDateTime(c.created_at);
    meta.append(el("span", {}, archived ? purgeText(c) : groupOf(c.created_at) === "今天" ? stamp.slice(11) : stamp.slice(5, 10)));
    button.append(el("span", {class:"ai-conv-title"}, c.title || "未命名对话"), meta);
    button.addEventListener("click", () => selectConversation(c.id).catch(showError));
    const act = el("button", {type:"button", class:"ai-conv-act", "aria-label":`${archived ? "恢复" : "归档"}会话：${c.title || "未命名对话"}`}, archived ? "恢复" : "归档");
    act.addEventListener("click", () => (archived ? restoreConversation(c) : archiveConversation(c)).catch(showArchiveError));
    row.append(button, act);
    return row;
  }
  function renderList() {
    const box = $("conversations");
    box.replaceChildren();
    let group = null;
    const query = $("conv-filter").value.trim().toLowerCase();
    const match = c => !query || (c.title || "").toLowerCase().includes(query);
    const shown = conversations.filter(match), shownArchived = archivedConvs.filter(match);
    if (query && !shown.length && !shownArchived.length) box.append(el("p", {class:"ai-faint ai-conv-none"}, "没有匹配的会话"));
    shown.forEach(c => {
      const name = groupOf(c.created_at);
      if (name !== group) { group = name; box.append(el("div", {class:"ai-group"}, name)); }
      box.append(conversationRow(c, false));
    });
    if (archivedConvs.length) {
      const toggle = el("button", {type:"button", class:"ai-archived-toggle", "aria-expanded":String(showArchived)}, `${showArchived ? "▾" : "▸"} 已归档 · ${archivedConvs.length}`);
      toggle.addEventListener("click", () => { showArchived = !showArchived; state.ui.archived = showArchived; persist(); renderList(); });
      box.append(toggle);
      if (showArchived) {
        box.append(el("p", {class:"ai-faint ai-archived-note"}, "归档的会话为只读，30 天后自动清理；清理只删除会话与消息，研究任务和产物保留。"));
        shownArchived.forEach(c => box.append(conversationRow(c, true)));
      }
    }
  }
  function showArchiveError(error) {
    const code = error.detail?.code;
    notice({conversation_active:"这个会话还有进行中的研究，完成或取消后才能归档。", not_found:"会话已不存在或不可访问。"}[code] || `操作失败：${error.message}`);
  }
  // 当前会话被归档后回到“新对话”空状态（首次发送时才会真正创建会话）。
  function leaveConversation() {
    saveDraft(); stopWatching(); taskStates.clear();
    current = null; state.current = null; generation++;
    delete root.dataset.conversation;
    history.replaceState(null, "", urlFor(null));
    messages = []; opinionViews.clear(); $("updates").replaceChildren();
    $("title").textContent = "新对话";
    restoreDraft(); renderMessages(); persist();
  }
  async function archiveConversation(c) {
    await post(`/agent/conversations/${c.id}/archive`, {});
    conversations = conversations.filter(x => x.id !== c.id);
    await listArchived();
    if (c.id === current) leaveConversation();
    renderList();
    notice("已归档，30 天后自动清理；可在列表底部的“已归档”中恢复。");
  }
  async function restoreConversation(c) {
    await post(`/agent/conversations/${c.id}/unarchive`, {});
    archivedConvs = archivedConvs.filter(x => x.id !== c.id);
    conversations = [...conversations, {id:c.id, title:c.title, created_at:c.created_at}].sort(byNewest);
    if (c.id === current) { binding(); notice("已恢复，可以继续提问。"); }
    renderList();
  }
  function showError(error) {
    const code = error.detail?.code;
    const explanation = {execution_denied:"当前执行未获授权，请核对启用状态与权限",idempotency_conflict:"原请求与已保存收据冲突，请恢复原请求内容",invalid_reference:"对象或版本不可用，请重新选择明确对象",task_conflict:"任务状态已变化，请恢复当前状态",conversation_archived:"会话已归档，请先在“已归档”中恢复再提问",not_found:"对象已不存在或不可访问"}[code];
    notice(`${explanation || error.message}。输入已保留；未确认的消息可用“确认是否已送达”重取原请求。`);
  }
  async function listConversations() {
    let all = [], after;
    do {
      const page = await get(`/agent/conversations?limit=100${after ? `&after=${encodeURIComponent(after)}` : ""}`);
      all.push(...page); after = page.length === 100 ? page.at(-1).id : null;
    } while (after);
    conversations = all.sort(byNewest);
    await listArchived(); renderList();
  }
  async function listArchived() {
    const items = []; let before;
    try {
      do {
        const page = await get(`/agent/conversation-summaries?archived=true&limit=100${before ? `&before=${encodeURIComponent(before)}` : ""}`);
        items.push(...page.items); before = page.next_cursor;
      } while (before);
    } catch { /* 归档列表不可用时不影响主列表 */ }
    archivedConvs = items;
  }
  async function readMessages(id) {
    const all = []; let after = 0;
    while (true) {
      const page = await get(`/agent/conversations/${id}/messages?after=${after}&limit=100`);
      all.push(...page);
      if (page.length < 100) return all;
      const next = page.at(-1).sequence;
      if (next <= after) throw new Error("消息游标未前进，请重新加载");
      after = next;
    }
  }

  // ---- 对话流 -----------------------------------------------------------------
  function emptyState() {
    const box = el("div", {class:"ai-empty"});
    const hello = el("div", {class:"ai-hello"});
    hello.append(el("span", {class:"ai-spark", "aria-hidden":"true"}, "✦"), el("h3", {}, "今天想研究什么？"),
      el("p", {class:"ai-faint"}, "直接提问，或从下面选一个起点。涉及某家公司时，用“＋”指定证券。"));
    const cards = el("div", {class:"ai-starters"});
    for (const item of STARTERS) {
      const button = el("button", {type:"button", class:`ai-sug${item.core ? "" : " ai-sug-extra"}`});
      button.append(el("small", {}, item.group), document.createTextNode(item.text));
      button.addEventListener("click", () => {
        $("input").value = item.text; saveDraft();
        if (subjectCount() < item.need) openPicker(true); else $("input").focus();
      });
      cards.append(button);
    }
    box.append(hello, cards, el("p", {class:"ai-faint ai-disc"}, "回答只基于已保存的材料与所列来源，包含假设与不确定性；交易决定由你作出。"));
    return box;
  }
  // Header subtitle (mobile app bar): how many research tasks this conversation started.
  function updateSubtitle() {
    const count = tasks.size || messages.filter(m => m.task_ref).length;
    $("subtitle").textContent = current && count ? `${count} 个研究任务` : "";
  }
  function renderMessages() {
    const thread = $("messages");
    const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
    // Task panels are long-lived; detach before the thread is rebuilt so listeners survive.
    tasks.forEach(panel => panel.remove());
    thread.replaceChildren();
    const placed = new Set();
    for (const m of messages) {
      const user = m.role === "user";
      const node = el("article", {class:`ai-msg ai-msg-${user ? "user" : "assistant"} agent-message agent-message-${m.role}`, "data-message-id":m.id});
      // Avatar + body column: user "我", assistant sparkle; the body carries bubble/answer, object chips and banners.
      const body = el("div", {class:"ai-body"});
      node.append(el("div", {class:`ai-av${user ? "" : " ai-av-bot"}`, "aria-hidden":"true"}, user ? "我" : "✦"), body);
      if (user) {
        body.append(el("div", {class:"ai-bubble"}, m.text));
        if (m.context_refs.length) {
          const ctx = el("div", {class:"ai-ctx"});
          m.context_refs.forEach(r => ctx.append(el("span", {class:"ai-chip ai-chip-obj"}, refLabel(r))));
          ctx.append(el("span", {class:"ai-ctx-note"}, "发送时对象已固定"));
          body.append(ctx);
        }
      } else {
        const who = el("div", {class:"ai-who"});
        who.append(el("b", {}, "研究助手"), el("span", {}, "基于已保存材料 · 不构成建议"));
        body.append(who);
        body.append(renderAnswer(m.text, m.content || []));
        if (m.context_refs.length) body.append(el("small", {class:"ai-faint"}, `回答时对象：${m.context_refs.map(refLabel).join("；")}`));
      }
      // AG18 consumes saved references through this real lifecycle event; no numeric-node evaluation here.
      [...m.artifact_refs,...m.context_refs].filter(r => r.kind === "recommendation").forEach(r => {
        const view = opinionViews.get(`${r.id}:${r.revision}`);
        if (view) body.append(el("div", {class:"ai-banner ai-banner-ai"}, `${view.replacement_ref ? "依据的观点已有更新 · " : ""}引用的观点当前${OPINION[view.status] || "状态无法识别"}`));
      });
      thread.append(node);
      const panel = m.task_ref && tasks.get(m.task_ref.id);
      if (panel) { thread.append(panel); placed.add(m.task_ref.id); }
    }
    tasks.forEach((panel, id) => { if (!placed.has(id)) thread.append(panel); });
    const empty = !messages.length && !tasks.size;
    if (empty) thread.append(emptyState());
    if (empty) thread.scrollTop = 0;
    else if (nearBottom) thread.scrollTop = thread.scrollHeight;
    updateSubtitle();
    root.dispatchEvent(new CustomEvent("agent:messages", {detail:{conversationId:current,messages}}));
  }
  async function reloadMessages(id, token) {
    const result = await readMessages(id);
    if (!isCurrent(id, token)) return;
    messages = result; renderMessages();
    resolveLabels([...refs,...result.flatMap(m => m.context_refs)],id,token);
    // A lost HTTP acknowledgement can be recovered from the persisted public message.
    const d = draft(id);
    if (d.pending && result.some(m => m.client_message_id === d.pending.client_message_id)) {
      if (d.text === d.pending.text) d.text = "";
      d.pending = null; persist(); restoreDraft();
    }
    for (const m of messages) if (m.task_ref && !tasks.has(m.task_ref.id)) watchTask(m.task_ref.id, id, token);
  }
  async function selectConversation(id) {
    saveDraft(); stopWatching(); taskStates.clear(); current = id; state.current = id; root.dataset.conversation = "1"; const token = ++generation;
    history.replaceState(null,"",urlFor(id));
    $("search-results").replaceChildren(); openPicker(false); openMode(false);
    messages = []; opinionViews.clear(); $("messages").replaceChildren(); $("updates").replaceChildren();
    $("title").textContent = [...conversations, ...archivedConvs].find(c => c.id === id)?.title || "对话";
    updateSubtitle();
    restoreDraft(); renderList(); persist(); notice("正在恢复消息与任务…");
    if (window.matchMedia("(max-width: 56rem)").matches) setPane("chat");
    await reloadMessages(id, token);
    if (isCurrent(id, token)) {
      const gone = archivedConvs.find(c => c.id === id);
      notice(gone ? `此会话已归档（${purgeText(gone)}），只读；恢复后才能继续提问。` : draft().pending ? "上次发送结果尚待确认，请重取原请求。" : "已恢复会话。");
      refreshUpdates(id, token); binding();
    }
  }
  function startTitle(seed, text) {
    const names = seed.refs.map(r => state.labels[`${r.kind}:${r.id}:${r.revision}`]?.name).filter(Boolean).join("、") || seed.refs.map(refLabel).join("、");
    const first = (text || "").trim().replace(/\s+/g, " ").slice(0, 40);
    const title = names && first ? `${names} · ${first}` : names || first || "新对话";
    return title.slice(0, 120);
  }
  async function createConversation({fromSend = false} = {}) {
    if (creating) return false;
    creating = true; $("new").disabled = true; binding();
    try {
      saveDraft(); const seed = current ? {text:"",kind:"auto",refs:structuredClone(refs),pending:null} : structuredClone(draft());
      const c = await post("/agent/conversations", {title:startTitle(seed, fromSend ? seed.text : ""),context_refs:seed.refs});
      state.drafts[c.id] = {...seed,pending:null}; state.drafts.new = {text:"",kind:"auto",refs:[],pending:null};
      conversations.unshift(c); await selectConversation(c.id);
      return true;
    } catch (error) { showError(error); return false; }
    finally { creating = false; $("new").disabled = false; binding(); }
  }
  async function send(retry = false) {
    const id = current;
    if (!id || locks.has(id)) return;
    saveDraft(); const d = draft(id);
    if (!retry && (d.pending || !d.text.trim())) return;
    if (!d.pending) d.pending = {client_message_id:randomUUID(),text:d.text,context_refs:structuredClone(d.refs),research_kind:d.kind};
    if (!persist()) return;
    const body = structuredClone(d.pending), token = generation;
    locks.add(id); binding();
    try {
      const receipt = await post(`/agent/conversations/${id}/messages`, body);
      d.pending = null; if (d.text === body.text) d.text = ""; persist();
      if (isCurrent(id, token)) { restoreDraft(); notice(""); await reloadMessages(id, token); watchTask(receipt.task.id, id, token); }
    } catch (error) {
      // Only explicit rejection proves no admission. Network/5xx acknowledgements remain uncertain.
      if ([401,403,404,422].includes(error.status) || error.detail?.code === "execution_denied") { d.pending = null; persist(); }
      if (isCurrent(id, token)) showError(error);
    } finally { locks.delete(id); if (isCurrent(id, token)) binding(); }
  }
  // First message from the empty state creates the conversation, titled from its subject and question.
  async function submit() {
    if (!current) {
      saveDraft();
      if (!draft().text.trim()) return;
      if (!(await createConversation({fromSend:true}))) return;
    }
    await send();
  }

  // ---- 任务卡（紧跟触发它的消息）----------------------------------------------
  // A task card sits directly under the message that started it; unknown owners go to the end.
  function placeTask(taskId) {
    const panel = tasks.get(taskId), owner = messages.find(m => m.task_ref && m.task_ref.id === taskId);
    const anchor = owner && $("messages").querySelector(`[data-message-id="${owner.id}"]`);
    $("messages").querySelector(".ai-empty")?.remove();
    if (anchor) anchor.after(panel); else $("messages").append(panel);
  }
  function watchTask(taskId, id, token) {
    if (tasks.has(taskId) || !isCurrent(id,token)) return;
    const panel = el("section", {class:"ai-task agent-task", "data-task-id":taskId, "data-stage":"queued"});
    const head = el("div", {class:"ai-task-head"});
    const badge = el("span", {class:"badge dot hollow ai-task-badge"}, "任务");
    const label = el("p", {class:"ai-task-label"}), progress = el("ul", {class:"ai-task-steps"}), cancel = el("button", {type:"button", class:"btn-sm btn-ghost ai-task-cancel"}, "取消任务");
    // Lifecycle bar: only the states the task API exposes (排队 / 执行 / 完成), driven by data-stage.
    const stages = el("ol", {class:"ai-stages", "aria-hidden":"true"});
    ["排队", "执行", "完成"].forEach(name => stages.append(el("li", {}, name)));
    const link = el("a", {class:"ai-task-link", href:"/app/runs"}, "在运行中心查看");
    head.append(label, badge, link, cancel); panel.append(head, stages, progress); tasks.set(taskId, panel); placeTask(taskId); updateSubtitle();
    let cursor = 0, failures = 0, stopped = false, timer, artifactTimer, controller, task, finishing;
    const seen = new Set();
    const alive = () => !stopped && isCurrent(id,token);
    const stop = () => { stopped = true; clearTimeout(timer); clearTimeout(artifactTimer); controller?.abort(); };
    stops.push(stop);
    const endpoint = `/agent/tasks/${taskId}`;
    function paint(value) {
      task = value; taskStates.set(taskId, value);
      const stopping = value.cancel_requested && !TERMINAL.has(value.status);
      const detail = `${value.stop_reason ? ` · ${STOP[value.stop_reason] || "停止原因无法识别"}` : ""}${value.waiting ? ` · ${value.waiting.required_input} · 截止 ${formatDateTime(value.waiting.deadline)}` : ""}`;
      label.textContent = `研究任务${detail}`;
      badge.textContent = stopping ? "正在停止" : TASK[value.status] || "任务";
      badge.className = `badge ai-task-badge ${stopping || value.status === "waiting_user" || value.status === "waiting_data" || value.status === "partial" || value.status === "failed" ? "warn dot" : value.status === "queued" ? "dot hollow" : value.status === "cancelled" ? "outline" : "accent dot"}`;
      panel.dataset.stage = {queued:"queued", running:"run", waiting_data:"wait", waiting_user:"wait", succeeded:"done", partial:"done", failed:"stop", cancelled:"stop"}[value.status] || "queued";
      panel.classList.toggle("ai-task-done", TERMINAL.has(value.status));
      panel.classList.toggle("ai-task-waiting", value.status === "waiting_user" || value.status === "waiting_data");
      cancel.disabled = TERMINAL.has(value.status) || value.cancel_requested;
      cancel.hidden = TERMINAL.has(value.status);
      renderList();
      root.dispatchEvent(new CustomEvent("agent:task", {detail:value}));
    }
    async function snapshot(refreshMessages = false) {
      const data = await get(`${endpoint}/snapshot`);
      if (!alive()) return;
      cursor = data.event_cursor; paint(data.task);
      const merged = new Map(messages.map(m => [m.id,m]));
      data.messages.forEach(m => merged.set(m.id,m));
      messages = [...merged.values()].sort((a,b) => a.sequence-b.sequence); renderMessages();
      // Always load all persisted messages, including terminal assistant responses.
      if (refreshMessages) await reloadMessages(id, token);
    }
    async function finish() {
      if (!alive()) return;
      if (finishing) return finishing;
      finishing = (async () => { await snapshot(true); stop(); })();
      try { await finishing; } finally { finishing = null; }
    }
    function event(value) {
      if (!alive() || seen.has(value.event_id) || value.event_id <= cursor) return;
      seen.add(value.event_id); cursor = value.event_id;
      const p = value.payload;
      // SSE carries artifact references separately from state transitions. Read
      // the authoritative task once per burst so the research rail can display
      // saved plans/materials while the model is still running.
      if (value.type === "artifact_saved" && !artifactTimer) {
        artifactTimer = setTimeout(async () => {
          artifactTimer = null;
          if (!alive()) return;
          try {
            const latest = await get(endpoint);
            if (!alive() || latest.revision < task.revision) return;
            paint(latest);
            if (TERMINAL.has(latest.status)) await finish();
          } catch (error) { if (alive()) notice(`研究档案更新暂不可用：${error.message}。已保存材料会在重连后恢复。`); }
        }, 200);
      }
      if (value.type === "task_state" && task) {
        paint({...task,status:p.status,cancel_requested:p.cancel_requested});
        // State events omit wait metadata and terminal reasons; read the authoritative task.
        get(endpoint).then(async latest => {
          if (!alive() || latest.revision < task.revision) return;
          paint(latest);
          if (TERMINAL.has(latest.status)) await finish();
        }).catch(error => { if (alive()) notice(`任务状态恢复失败：${error.message}。后台任务仍保留。`); });
      }
      const summary = value.type === "progress" || value.type === "step_started" ? p.summary : value.type === "input_required" ? p.question : value.type === "error" ? p.message : null;
      if (summary) { progress.append(el("li", {}, summary)); if (progress.children.length > 30) progress.firstChild.remove(); }
      root.dispatchEvent(new CustomEvent("agent:event", {detail:value}));
    }
    async function poll() {
      if (!alive()) return;
      root.dataset.transport = "poll";
      let wait = 2000;
      try {
        const data = await get(`${endpoint}/events?transport=poll&after_event_id=${cursor}`);
        if (!alive()) return;
        data.events.forEach(event); cursor = data.next_cursor; paint(data.task);
        if (TERMINAL.has(data.task.status)) { await finish(); return; }
      } catch (error) {
        if (!alive()) return;
        if (error.status === 410) {
          try { await snapshot(); } catch (snapshotError) { if (alive()) notice(`快照恢复失败：${snapshotError.message}。继续重试；后台任务仍保留。`); }
        }
        else if (error.status === 429) { wait = 10000; notice("请求过于频繁，已降低刷新频率，稍后自动恢复。后台任务仍保留。"); }
        else { notice(`任务连接暂不可用：${error.message}。后台任务仍保留。`); }
      }
      if (alive()) timer = setTimeout(poll, wait);
    }
    async function connect() {
      if (!alive()) return;
      // fetch SSE gives access to HTTP410; EventSource cannot inspect error status.
      controller = new AbortController();
      try {
        const response = await fetch(`${endpoint}/events?after_event_id=${cursor}`, {credentials:"same-origin",headers:{...FETCH_MARKER_HEADERS,"Last-Event-ID":String(cursor)},signal:controller.signal});
        if (!alive()) return;
        if (response.status === 410) { await snapshot(); if (alive()) timer = setTimeout(connect, 300); return; }
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
        const reader = response.body.getReader(); let buffer = "", decoder = new TextDecoder();
        while (alive()) {
          const part = await reader.read(); if (part.done) break;
          buffer += decoder.decode(part.value,{stream:true});
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0,boundary); buffer = buffer.slice(boundary+2);
            const line = frame.split("\n").find(l => l.startsWith("data: "));
            if (!line) continue;
            const value = JSON.parse(line.slice(6));
            if (frame.includes("event: cursor_expired")) { await snapshot(); controller.abort(); break; }
            event(value);
          }
        }
        if (!alive()) return;
        await snapshot(); if (TERMINAL.has(task.status)) { await finish(); return; }
      } catch (error) { if (!alive()) return; }
      failures++;
      if (alive()) { if (failures >= 3) { notice("实时连接未恢复，已切换为轮询刷新；后台任务继续，不会取消。"); poll(); } else timer = setTimeout(connect, 500 * failures); }
    }
    // Android 内嵌：IPC 事件流（Rust 侧读 SSE 经 Channel 逐事件推送），返回值
    // 决定后续——"expired" 重读快照；{rate} 限流退避后重连；"degrade" 落轮询兜底。
    // 终态不走这里：task_state 事件由 event() 既有逻辑处理并 finish。
    async function bridgeLive() {
      const internals = window.__TAURI_INTERNALS__;
      if (!internals?.invoke || !internals.transformCallback) return "degrade";
      root.dataset.transport = "stream";
      return new Promise((resolve) => {
        let settled = false;
        const done = (value) => { if (!settled) { settled = true; resolve(value); } };
        const callback = internals.transformCallback((raw) => {
          // Channel 回调拿到的是已反序列化的对象（Rust 侧 send 的是 JSON 文本）。
          let message;
          try { message = typeof raw === "string" ? JSON.parse(raw) : raw; } catch { return; }
          if (message.kind === "event") {
            try { event(JSON.parse(message.data)); } catch { /* 单条坏事件不中断流 */ }
          } else if (message.kind === "expired") done("expired");
          else if (message.kind === "rate_limited") done({ rate: Number(message.retry_after) || 10 });
          else done("degrade");
        });
        internals.invoke("stream_task_events", { taskId, cursor, onEvent: "__CHANNEL__:" + callback }).catch(() => done("degrade"));
      });
    }
    async function live() {
      while (alive()) {
        const how = await bridgeLive();
        if (!alive()) return;
        if (how === "expired") { await snapshot(); continue; }
        if (how && typeof how === "object") {
          notice(`请求过于频繁，${how.rate} 秒后自动恢复实时刷新。后台任务仍保留。`);
          await new Promise((resolve) => setTimeout(resolve, how.rate * 1000));
          continue;
        }
        notice("实时连接未恢复，已切换为轮询刷新；后台任务继续，不会取消。");
        root.dataset.transport = "poll";
        poll();
        return;
      }
    }
    cancel.addEventListener("click", async () => {
      if (cancel.disabled) return; cancel.disabled = true;
      try { const result = await post(`${endpoint}/cancel`, {idempotency_key:randomUUID()}); if (alive()) { paint(result); if (TERMINAL.has(result.status)) await finish(); } }
      catch (error) { if (alive()) { showError(error); cancel.disabled = false; } }
    });
    // 内嵌模式走 IPC 事件流（轮询兜底）；浏览器路径 SSE 照旧。
    const startLive = document.body.dataset.embedded ? live : connect;
    snapshot().then(() => { if (alive()) { if (TERMINAL.has(task.status)) stop(); else startLive(); } }).catch(error => { if (alive()) { showError(error); timer = setTimeout(poll,2000); } });
  }

  // ---- 观点更新条 ---------------------------------------------------------------
  async function refreshUpdates(id, token) {
    if (!isCurrent(id, token)) return;
    const recommendations = new Map();
    messages.forEach(m => [...m.artifact_refs,...m.context_refs].filter(r => r.kind === "recommendation").forEach(r => recommendations.set(`${r.id}:${r.revision}`,r)));
    const notices = [];
    for (const r of recommendations.values()) {
      try {
        const view = await get(`/investor/recommendations/${r.id}?revision=${r.revision}`);
        if (!isCurrent(id,token)) return;
        notices.push({ref:r,view});
      } catch (error) { notices.push({ref:r,error:error.message}); }
    }
    if (!isCurrent(id,token)) return;
    $("updates").replaceChildren();
    notices.forEach(({ref,view,error}) => {
      if (view) opinionViews.set(`${ref.id}:${ref.revision}`,view);
      const needsLook = error || view.replacement_ref || view.status !== "published";
      const text = error ? `观点状态暂未获取：${error}` : `引用的观点 v${ref.revision}：${OPINION[view.status] || "状态无法识别"}${view.replacement_ref ? "。依据的观点已有更新，此前的回答保持原样" : ""}`;
      $("updates").append(el("div", {class:`ai-banner ${needsLook ? "ai-banner-ai" : ""}`}, text));
    });
    renderMessages();
    root.dispatchEvent(new CustomEvent("agent:updates", {detail:notices}));
    updateTimer = setTimeout(() => refreshUpdates(id,token),15000);
  }

  // ---- 输入 -----------------------------------------------------------------------
  $("input").addEventListener("input", saveDraft); $("kind").addEventListener("change", () => { autoCompare = false; saveDraft(); binding(); });
  $("input").addEventListener("keydown", event => {
    // Enter sends; Shift+Enter breaks the line; never send while an IME candidate is open.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); if (!$("send").disabled) $("compose").requestSubmit();
    }
  });
  $("compose").addEventListener("submit", e => {e.preventDefault(); submit().catch(showError);});
  $("retry").addEventListener("click", () => send(true)); $("new").addEventListener("click", () => createConversation());
  $("add-object").addEventListener("click", () => openPicker($("picker").hidden));
  $("mode-btn").addEventListener("click", () => openMode($("mode-pop").hidden));
  document.addEventListener("click", event => {
    if (!root.querySelector(".ai-compose-wrap").contains(event.target)) { openPicker(false); openMode(false); }
  });
  root.addEventListener("keydown", event => { if (event.key === "Escape") { openPicker(false); openMode(false); } });

  async function search(q) {
    const attempt = ++searchGeneration, token = generation;
    const result = await get(`/agent/listings?q=${encodeURIComponent(q)}`);
    if (attempt !== searchGeneration || token !== generation) return;
    $("search-results").replaceChildren();
    if (!result.length) $("search-results").append(el("p", {}, "未找到已保存的证券身份；请核对完整代码或先导入证券资料。"));
    result.forEach(hit => {
      const button = el("button",{type:"button"}, `${hit.name} · ${hit.ticker}`);
      button.addEventListener("click", () => { if (attempt !== searchGeneration || token !== generation) return; remember(hit);
        if (refs.length >= 32) { notice("最多绑定32个对象，请先移除对象再重新选择。"); return; }
        if (!refs.some(r => r.kind === hit.ref.kind && r.id === hit.ref.id && r.revision === hit.ref.revision)) refs=[...refs,hit.ref];
        saveDraft(); binding(); $("search-results").replaceChildren(); $("search").value = ""; openPicker(false); $("input").focus(); });
      $("search-results").append(button);
    });
  }
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer); const q = $("search").value.trim();
    if (!q) { searchGeneration++; $("search-results").replaceChildren(); return; }
    searchTimer = setTimeout(() => search(q).catch(showError), 280);
  });
  $("subject-form").addEventListener("submit", e => {e.preventDefault(); clearTimeout(searchTimer); search($("search").value).catch(showError);});
  window.addEventListener("pagehide", () => {saveDraft(); stopWatching();});
  window.addEventListener("pageshow", e => { if (e.persisted && current) selectConversation(current).catch(showError); });

  async function init() {
    const params = new URLSearchParams(location.search);
    if (narrow()) $("input").placeholder = "问点什么…";
    // The research drawer starts open, except on mid-width desktops where it would cover the thread.
    setDrawer((state.ui.drawer || (window.matchMedia("(max-width: 68rem)").matches ? "closed" : "open")) === "open");
    setPane(params.get("pane") || "chat");
    setTab(params.get("tab") || "chat", {url:false});
    restoreDraft(); renderMessages(); await listConversations();
    const requested = params.get("conversation");
    if (requested || (!params.has("ticker") && !params.has("candidate") && state.current)) await selectConversation(requested || state.current);
    if (params.has("ticker")) { $("search").value=params.get("ticker"); openPicker(true); await search($("search").value); notice("请在查找结果中确认证券，再发送问题。" ); }
    if (params.has("candidate")) {
      const value = params.get("candidate");
      if (!/^[1-9]\d*$/.test(value)) throw new Error("候选编号无效");
      const context = await get(`/agent/candidate-context/${value}`); refs=context.context_refs; saveDraft(); binding(); await resolveLabels(refs,current,generation); notice("候选对象已明确，直接提问即可。" );
    }
    root.dispatchEvent(new CustomEvent("agent:ready", {detail:{artifactHost:$("artifact-extension"),controlsHost:$("controls-extension")}}));
  }
  init().catch(showError);
}
