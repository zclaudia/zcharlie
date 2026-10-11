// Only public AG09 contracts. Drafts and immutable unacknowledged bodies survive reload.
// AI page shell: conversation list / thread / research drawer, plus portfolio and controls tabs.
import { get, post, FETCH_MARKER_HEADERS } from "./api.js";
import { el } from "./dom.js";
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
  {value:"portfolio_action", label:"组合建议", title:"组合建议", desc:"需要先在“组合”页签选择账户快照"},
];
const STARTERS = [
  {title:"关于一家公司", items:["这家公司为什么值得关注？","这次下跌改变了什么？","你为什么不同意我？","现在价格隐含了什么预期？"]},
  {title:"在几家之间选", items:["比较这两家，先研究哪家？"]},
  {title:"回头检查自己", items:["你上次的判断哪里错了？","哪些判断快到复核日期？"]},
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
  let conversations = [], messages = [], refs = [], stops = [], updateTimer;
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
  function setPane(pane) {
    if (!PANES.includes(pane)) pane = "chat";
    root.dataset.pane = pane;
    root.querySelectorAll("[data-agent-pane]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.agentPane === pane)));
  }
  function setDrawer(open) {
    root.dataset.drawer = open ? "open" : "closed";
    const toggle = $("drawer-toggle");
    toggle.setAttribute("aria-expanded", String(open)); toggle.textContent = open ? "档案 ▸ 收起" : "档案 ◂ 展开";
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
      if (mode.value === "portfolio_action" && !hasPortfolio()) reason = "需要先在“组合”页签选择账户快照";
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
  function openMode(open) {
    if (open) { openPicker(false); renderModePop(); }
    $("mode-pop").hidden = !open; $("mode-btn").setAttribute("aria-expanded", String(open));
  }
  function openPicker(open) {
    if (open) openMode(false);
    $("picker").hidden = !open; $("add-object").setAttribute("aria-expanded", String(open));
    if (open) $("search").focus();
  }
  function renderChips() {
    const box = $("binding"); box.replaceChildren();
    if (!refs.length) { box.append(el("span", {class:"ai-chip"}, "未指定证券")); return; }
    for (const ref of refs) {
      const chip = el("span", {class:"ai-chip ai-chip-accent"}, refLabel(ref));
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
    $("send").disabled = (!!current && locks.has(current)) || !!pending || creating;
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
  function renderList() {
    const box = $("conversations");
    box.replaceChildren();
    let group = null;
    conversations.forEach(c => {
      const name = groupOf(c.created_at);
      if (name !== group) { group = name; box.append(el("div", {class:"ai-group"}, name)); }
      const button = el("button", {type:"button", class:"ai-conv", "aria-current":c.id === current ? "true" : undefined, "data-conversation-id":c.id});
      const meta = el("span", {class:"ai-conv-meta"});
      const status = conversationStatus(c);
      if (status) meta.append(el("span", {}, ""), el("span", {}, status.text));
      if (status) meta.firstChild.className = `ai-dot ${status.cls}`;
      meta.append(el("span", {}, formatDateTime(c.created_at).slice(5)));
      button.append(el("span", {class:"ai-conv-title"}, c.title || "未命名对话"), meta);
      button.addEventListener("click", () => selectConversation(c.id).catch(showError));
      box.append(button);
    });
  }
  function showError(error) {
    const code = error.detail?.code;
    const explanation = {execution_denied:"当前执行未获授权，请核对启用状态与权限",idempotency_conflict:"原请求与已保存收据冲突，请恢复原请求内容",invalid_reference:"对象或版本不可用，请重新选择明确对象",task_conflict:"任务状态已变化，请恢复当前状态",not_found:"对象已不存在或不可访问"}[code];
    notice(`${explanation || error.message}。输入已保留；未确认的消息可用“确认是否已送达”重取原请求。`);
  }
  async function listConversations() {
    let all = [], after;
    do {
      const page = await get(`/agent/conversations?limit=100${after ? `&after=${encodeURIComponent(after)}` : ""}`);
      all.push(...page); after = page.length === 100 ? page.at(-1).id : null;
    } while (after);
    conversations = all; renderList();
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
    box.append(el("h3", {}, "今天想研究什么？"), el("p", {class:"ai-faint"}, "从一个具体的问题开始。涉及某家公司时，点输入框里的“＋ 对象”指定证券；没有填写完整画像也能开始。"));
    const cards = el("div", {class:"ai-starters"});
    for (const group of STARTERS) {
      const card = el("div", {class:"ai-starter-card"}, undefined);
      card.append(el("h4", {}, group.title));
      for (const text of group.items) {
        const button = el("button", {type:"button"}, text);
        button.addEventListener("click", () => { $("input").value = text; saveDraft(); $("input").focus(); });
        card.append(button);
      }
      cards.append(card);
    }
    box.append(cards, el("p", {class:"ai-faint"}, "回答只基于已保存的材料与所列来源，包含假设与不确定性；交易决定由你作出。"));
    return box;
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
      if (user) {
        node.append(el("div", {class:"ai-bubble"}, m.text));
        if (m.context_refs.length) {
          const ctx = el("div", {class:"ai-ctx"});
          m.context_refs.forEach(r => ctx.append(el("span", {class:"ai-chip"}, refLabel(r))));
          ctx.append(el("span", {}, "发送时对象已固定"));
          node.append(ctx);
        }
      } else {
        const who = el("div", {class:"ai-who"});
        who.append(el("span", {class:"ai-chip ai-chip-ai"}, "AI"));
        node.append(who);
        m.text.split(/\n{2,}/).filter(Boolean).forEach(part => node.append(el("p", {class:"ai-answer"}, part)));
        if (m.context_refs.length) node.append(el("small", {class:"ai-faint"}, `回答时对象：${m.context_refs.map(refLabel).join("；")}`));
      }
      // AG18 consumes saved references through this real lifecycle event; no numeric-node evaluation here.
      [...m.artifact_refs,...m.context_refs].filter(r => r.kind === "recommendation").forEach(r => {
        const view = opinionViews.get(`${r.id}:${r.revision}`);
        if (view) node.append(el("div", {class:"ai-banner ai-banner-ai"}, `${view.replacement_ref ? "依据的观点已有更新 · " : ""}引用的观点当前${OPINION[view.status] || "状态无法识别"}`));
      });
      thread.append(node);
      const panel = m.task_ref && tasks.get(m.task_ref.id);
      if (panel) { thread.append(panel); placed.add(m.task_ref.id); }
    }
    tasks.forEach((panel, id) => { if (!placed.has(id)) thread.append(panel); });
    if (!messages.length && !tasks.size) thread.append(emptyState());
    if (nearBottom) thread.scrollTop = thread.scrollHeight;
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
    saveDraft(); stopWatching(); taskStates.clear(); current = id; state.current = id; const token = ++generation;
    history.replaceState(null,"",urlFor(id));
    $("search-results").replaceChildren(); openPicker(false); openMode(false);
    messages = []; opinionViews.clear(); $("messages").replaceChildren(); $("updates").replaceChildren();
    $("title").textContent = conversations.find(c => c.id === id)?.title || "对话";
    restoreDraft(); renderList(); persist(); notice("正在恢复消息与任务…");
    if (window.matchMedia("(max-width: 56rem)").matches) setPane("chat");
    await reloadMessages(id, token);
    if (isCurrent(id, token)) { notice(draft().pending ? "上次发送结果尚待确认，请重取原请求。" : "已恢复会话。"); refreshUpdates(id, token); }
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
    if (!d.pending) d.pending = {client_message_id:crypto.randomUUID(),text:d.text,context_refs:structuredClone(d.refs),research_kind:d.kind};
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
    const panel = el("section", {class:"ai-task agent-task", "data-task-id":taskId});
    const head = el("div", {class:"ai-task-head"});
    const badge = el("span", {class:"ai-chip ai-chip-ai"}, "任务");
    const label = el("p", {class:"ai-task-label"}), progress = el("ul", {class:"ai-task-steps"}), cancel = el("button", {type:"button"}, "取消任务");
    head.append(badge, label, cancel); panel.append(head, progress); tasks.set(taskId, panel); placeTask(taskId);
    let cursor = 0, failures = 0, stopped = false, timer, controller, task, finishing;
    const seen = new Set();
    const alive = () => !stopped && isCurrent(id,token);
    const stop = () => { stopped = true; clearTimeout(timer); controller?.abort(); };
    stops.push(stop);
    const endpoint = `/agent/tasks/${taskId}`;
    function paint(value) {
      task = value; taskStates.set(taskId, value);
      const stopping = value.cancel_requested && !TERMINAL.has(value.status);
      const detail = `${value.stop_reason ? ` · ${STOP[value.stop_reason] || "停止原因无法识别"}` : ""}${value.waiting ? ` · ${value.waiting.required_input} · 截止 ${value.waiting.deadline}` : ""}`;
      label.textContent = `研究任务${detail}`;
      badge.textContent = stopping ? "正在停止" : TASK[value.status] || "任务";
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
        else { notice(`任务连接暂不可用：${error.message}。后台任务仍保留。`); }
      }
      if (alive()) timer = setTimeout(poll, 2000);
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
    cancel.addEventListener("click", async () => {
      if (cancel.disabled) return; cancel.disabled = true;
      try { const result = await post(`${endpoint}/cancel`, {idempotency_key:crypto.randomUUID()}); if (alive()) { paint(result); if (TERMINAL.has(result.status)) await finish(); } }
      catch (error) { if (alive()) { showError(error); cancel.disabled = false; } }
    });
    snapshot().then(() => { if (alive()) { if (TERMINAL.has(task.status)) stop(); else connect(); } }).catch(error => { if (alive()) { showError(error); timer = setTimeout(poll,2000); } });
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
    setDrawer((state.ui.drawer || "open") === "open");
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
