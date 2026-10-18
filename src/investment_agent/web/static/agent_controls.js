import { randomUUID } from "./uuid.js";
// AG19 authenticated, versioned controls. Credentials only use the encrypted settings page.
import {get, post, patch} from './api.js';
import {el} from './dom.js';
import {formatDateTime} from './format.js';

const ACTION_LABELS = {
  pause: '暂停后台', resume: '恢复后台', mute: '静音通知', unmute: '恢复通知',
  archive: '归档并撤下观点', reopen: '明确重开研究',
};
const DECISION_LABELS = {accept: '采纳', reject: '不采纳', defer: '暂缓', independent: '独立决定'};
const PENDING_KEY = 'agent-control-unconfirmed-v1';
const lines = node => node.value.split('\n').map(v => v.trim()).filter(Boolean);
const reference = (kind, value) => ({kind, id: value.id, revision: value.revision});

export function bootAgentControls() {
  const root = document.getElementById('agent-workbench');
  const host = document.getElementById('agent-controls-extension');
  if (!root || !host) return;
  let contextRefs = [], epoch = 0, contextVersion = 0, pending = null, contextSignature = null;
  const names = new Map();
  let lastBindingSignature = null;
  try {pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null');} catch {}
  const nav = el('div', {class: 'toolbar'});
  const body = el('section', {class: 'agent-control-body'});
  const status = el('p', {role: 'status', 'aria-live': 'polite'});
  host.append(nav, status, body);

  function button(text, fn) {
    // v2 emphasis: the one confirming button of a form is primary; refusing/revoking is a quiet warning action.
    const cls = /^(确认|明确保存|保存决定|批准)/.test(text) ? 'btn-primary' : /^(拒绝|撤销|放弃)/.test(text) ? 'btn-danger-ghost' : undefined;
    const node = el('button', {type: 'button', class: cls}, text);
    node.addEventListener('click', fn);
    return node;
  }
  function input(label, value = '', type = 'text', parent = body) {
    const wrap = el('label', {}, label);
    const node = el(type === 'textarea' ? 'textarea' : 'input', {
      type: type === 'textarea' ? undefined : type,
      autocomplete: 'off', spellcheck: 'false',
    });
    node.value = value;
    wrap.append(node);
    parent.append(wrap);
    return node;
  }
  function select(label, options, parent = body) {
    const wrap = el('label', {}, label), node = el('select');
    for (const [value, text] of options) node.append(el('option', {value}, text));
    wrap.append(node);
    parent.append(wrap);
    return node;
  }
  function audit(parent, value) {
    const details = el('details');
    details.append(el('summary', {}, '精确引用与版本审计'),
      el('pre', {class: 'agent-control-review'}, JSON.stringify(value, null, 2)));
    parent.append(details);
  }
  function error(exc) {
    let detail = exc.detail?.message || exc.message;
    if (Array.isArray(exc.detail)) detail = exc.detail.map(x => `${x.loc.join('.')}：${x.msg}`).join('；');
    status.textContent = `未确认成功：${detail}。${exc.status === 409 ? '版本或期限已变化，请刷新并重新审阅。' : ''}`;
  }
  function start(title) {
    ++epoch;
    body.replaceChildren(el('h3', {}, title));
    status.textContent = '';
    return epoch;
  }
  async function load(fn) {
    try {await fn();} catch (exc) {error(exc);}
  }
  function rememberPending() {
    if (pending) sessionStorage.setItem(PENDING_KEY, JSON.stringify(pending));
    else sessionStorage.removeItem(PENDING_KEY);
  }
  async function send(path, request, node, method = 'POST') {
    if (pending) {
      status.textContent = '先重取或明确放弃上一条未确认请求。';
      return;
    }
    delete retry.dataset.confirmed;
    delete retry.dataset.conflicted;
    retry.disabled = false;
    pending = {path, request, method};
    rememberPending();
    await transmit(node);
  }
  async function transmit(node) {
    if (!pending) return;
    const fixed = pending;
    node.disabled = true;
    try {
      await (fixed.method === 'PATCH' ? patch : post)(fixed.path, fixed.request, {idempotency: false});
      pending = null;
      rememberPending();
      node.dataset.confirmed = 'true';
      status.textContent = '服务器已确认保存。请刷新查看当前版本。';
    } catch (exc) {
      error(exc);
      if (exc.status > 0 && exc.status < 500) {
        pending = null;
        rememberPending();
        if (exc.status === 409) node.dataset.conflicted = 'true';
      }
    } finally {
      node.disabled = node.dataset.confirmed === 'true' || node.dataset.conflicted === 'true';
      retry.hidden = !pending;
      abandon.hidden = !pending;
    }
  }
  const retry = button('重取未确认操作', () => transmit(retry));
  const abandon = button('放弃重取并核对服务器记录', () => {
    pending = null;
    rememberPending();
    retry.hidden = true;
    abandon.hidden = true;
    status.textContent = '已放弃客户端重取；请刷新核对服务器是否已经保存。';
  });
  retry.hidden = !pending;
  abandon.hidden = !pending;
  host.append(retry, abandon);
  function execute(parameters, node, path = '/investor/actions') {
    return send(path, {parameters, idempotency_key: randomUUID()}, node);
  }
  async function listingName(ref) {
    if (!names.has(ref.id)) {
      const listing = await get(`/agent/listings/${ref.id}`);
      names.set(ref.id, `${listing.name} · ${listing.ticker}`);
    }
    return names.get(ref.id);
  }
  async function objectName(ref) {
    if (ref.kind === 'listing') return listingName(ref);
    if (ref.kind === 'case') {
      const detail = await get(`/investor/cases/${ref.id}`);
      return `${await listingName(detail.case.listing_ref)} · 档案 v${ref.revision}`;
    }
    if (ref.kind === 'recommendation') {
      const view = await get(`/investor/recommendations/${ref.id}?revision=${ref.revision}`);
      const rec = view.recommendation;
      const detail = await get(`/investor/cases/${rec.case_ids[0]}`);
      return `${await listingName(detail.case.listing_ref)} · 观点 v${ref.revision} · ${formatDateTime(rec.created_at)}`;
    }
    return `${ref.kind} · v${ref.revision}`;
  }
  // Name/code search is shared by interactive and background scope controls.
  async function scopePicker(label, initial, token) {
    const section = el('fieldset'), selected = new Map(initial.map(ref => [ref.id, ref]));
    section.append(el('legend', {}, label));
    body.append(section);
    const query = input('证券名称或完整代码', '', 'text', section);
    const results = el('div'), chosen = el('div');
    let searchVersion = 0;
    section.append(results, chosen);
    async function renderChosen() {
      const labels = await Promise.all([...selected.values()].map(objectName));
      if (token !== epoch) return;
      chosen.replaceChildren();
      [...selected.values()].forEach((ref, i) => {
        const row = el('p', {}, labels[i]);
        row.append(button('移除此证券', () => {
          selected.delete(ref.id);
          section.dispatchEvent(new Event('input', {bubbles: true}));
          load(renderChosen);
        }));
        chosen.append(row);
      });
      if (!selected.size) chosen.append(el('p', {}, '尚未选择证券'));
    }
    const search = button('查找并明确加入范围', () => load(async () => {
      const version = ++searchVersion;
      const rows = await get(`/agent/listings?q=${encodeURIComponent(query.value.trim())}`);
      if (token !== epoch || version !== searchVersion) return;
      results.replaceChildren();
      for (const row of rows) {
        names.set(row.ref.id, `${row.name} · ${row.ticker}`);
        results.append(button(`加入 ${row.name} · ${row.ticker}`, () => {
          selected.set(row.ref.id, row.ref);
          section.dispatchEvent(new Event('input', {bubbles: true}));
          load(renderChosen);
        }));
      }
      if (!rows.length) results.append(el('p', {}, '没有匹配证券，请核对完整代码或名称。'));
    }));
    section.insertBefore(search, results);
    await renderChosen();
    return {node: section, refs: () => [...selected.values()]};
  }
  function quantity(node) {
    return node.value.trim() ? {value: node.value.trim(), unit: 'USD', currency: 'USD', unknown_reason: null}
      : {value: null, unit: 'USD', currency: 'USD', unknown_reason: '用户尚未明确金额'};
  }
  function issueList(parent, title, values) {
    parent.append(el('h4', {}, title));
    const list = el('ul');
    for (const value of values) list.append(el('li', {}, value));
    if (!values.length) list.append(el('li', {}, '本地配置未发现缺项；实际执行仍须检查当时对象、价格、范围与剩余额度。'));
    parent.append(list);
  }
  async function setup() {
    const token = start('首次使用与授权');
    const [readiness, mandate] = await Promise.all([
      get('/investor/readiness'),
      get('/investor/mandate').catch(exc => {if (exc.status === 404) return null; throw exc;}),
    ]);
    if (token !== epoch) return;
    body.append(el('a', {href: '/app/settings#agent-settings'}, '独立 Agent 模型、加密密钥与运行开关设置'),
      el('p', {}, '首次只补本次相关信息，其他信息保留未知。后台关闭仍可明确交互；提问不会自动启用 Agent 或后台。'),
      el('p', {}, `Agent ${readiness.enabled ? '已启用' : '关闭'}；运行后台开关${readiness.runtime_background_enabled ? '已启用' : '关闭'}；约定后台授权${readiness.mandate_background_enabled ? '已启用' : '关闭'}。`),
      el('p', {}, `当前输出模式：${readiness.output_mode}；JSON Object ${readiness.supports_json_object ? '声明支持' : '未声明支持'}；JSON Schema ${readiness.supports_json_schema ? '声明支持' : '未声明支持'}。能力声明须与实际端点验证一致。`),
      el('p', {}, `当前有效探索上限：${readiness.effective_max_leads} 条线索 / ${readiness.effective_max_new_tasks} 个新任务；由运行设置与约定取较小值。`),
      el('p', {}, '配置保存与本地检查均不代表供应商网络验证。当前完整模型配置指纹（排除密钥）用于核验报价绑定：'));
    const fingerprint = input('当前模型配置指纹', readiness.model_config_fingerprint);
    fingerprint.readOnly = true;
    issueList(body, '交互配置与限制', readiness.interactive_issues);
    issueList(body, '后台复核配置与限制', readiness.review_issues);
    issueList(body, '后台探索配置与限制', readiness.exploration_issues);
    body.append(el('p', {}, '后台需分别启用运行开关与投资约定授权。USD 每日共享总额与单任务全生命周期上限分别生效；探索与复核分账不互借。每批至多20条线索/3个新任务，与运行设置取较小值。'));
    const goals = input('研究目标（每行一项）', (mandate?.goals || []).join('\n'), 'textarea');
    const unknowns = input('尚未知晓的信息（每行一项）',
      (mandate?.unknowns || ['资金用途、期限与组合信息尚未知晓']).join('\n'), 'textarea');
    const constraints = input('明确约束（每行一项）', (mandate?.hard_constraints || []).join('\n'), 'textarea');
    const scope = await scopePicker('明确交互证券范围', mandate?.scope_refs || contextRefs.filter(r => r.kind === 'listing'), token);
    if (token !== epoch) return;
    const auth = mandate?.authorization;
    const background = input('投资约定：明确允许后台研究', '', 'checkbox');
    background.checked = auth?.background_enabled || false;
    const backScope = await scopePicker('明确后台证券范围', auth?.exploration_scope_refs || [], token);
    if (token !== epoch) return;
    const daily = input('USD 每日共享总额（空白明确未知）', auth?.daily_budget.value ?? '');
    const task = input('USD 单任务全生命周期上限（空白明确未知）', auth?.per_task_budget.value ?? '');
    const review = input('USD 复核日金额（空白未配置）', auth?.review_daily_budget?.value ?? '');
    const explore = input('USD 探索日金额（空白关闭探索）', auth?.exploration_daily_budget?.value ?? '');
    const leads = input('每批线索上限（0–20）', auth?.max_leads_per_batch ?? 20, 'number');
    const tasks = input('每批新任务上限（0–3）', auth?.max_new_tasks_per_batch ?? 3, 'number');
    const preview = el('section');
    let editVersion = 0;
    const fields = [goals, unknowns, constraints, scope.node, background, backScope.node, daily, task, review, explore, leads, tasks];
    for (const node of fields) node.addEventListener('input', () => {++editVersion;});
    body.append(preview);
    const reviewButton = button('审阅投资约定变更', () => load(async () => {
      const reviewedVersion = editVersion;
      for (const node of [daily, task, review, explore]) {
        if (node.value.trim() && (!/^\d+(?:\.\d+)?$/.test(node.value.trim()) || !Number.isFinite(Number(node.value)))) {
          throw Error('USD 金额须为非负十进制数，未知请留空');
        }
      }
      const candidate = {
        ...(mandate || {id: randomUUID(), schema_version: 1,
          created_at: new Date().toISOString(), actor: 'user', origin_ref: null}),
        revision: mandate ? mandate.revision + 1 : 1,
        goals: lines(goals), unknowns: lines(unknowns), hard_constraints: lines(constraints),
        scope_refs: scope.refs(),
        authorization: {
          background_enabled: background.checked, exploration_scope_refs: backScope.refs(),
          daily_budget: quantity(daily), per_task_budget: quantity(task),
          review_daily_budget: review.value.trim() ? quantity(review) : null,
          exploration_daily_budget: explore.value.trim() ? quantity(explore) : null,
          max_leads_per_batch: Number(leads.value), max_new_tasks_per_batch: Number(tasks.value),
          source_refs: auth?.source_refs || [],
        },
      };
      if (!Number.isInteger(Number(leads.value)) || Number(leads.value) < 0 || Number(leads.value) > 20 ||
          !Number.isInteger(Number(tasks.value)) || Number(tasks.value) < 0 || Number(tasks.value) > 3) {
        throw Error('线索/新任务上限须为0–20/0–3内的整数');
      }
      preview.replaceChildren(el('h4', {}, '即将保存的明确授权'));
      await mandateSummary(preview, candidate);
      if (token !== epoch) return;
      if (reviewedVersion !== editVersion) {
        preview.replaceChildren(el('p', {}, '审阅期间输入已变化，请重新审阅当前内容。'));
        return;
      }
      audit(preview, candidate);
      const save = button('确认保存已审阅约定', () => mandate
        ? execute({operation: 'mandate_revision', candidate, expected_revision: mandate.revision}, save)
        : send('/investor/mandate', {candidate, idempotency_key: randomUUID()}, save));
      preview.append(save);
      reviewButton.disabled = true;
      for (const node of fields) {
        node.addEventListener('input', () => {save.disabled = true; reviewButton.disabled = false;}, {once: true});
      }
    }));
    body.append(reviewButton, button('刷新本地配置与当前约定', () => load(setup)));
  }
  async function mandateSummary(parent, candidate) {
    const auth = candidate.authorization;
    const scope = await Promise.all(candidate.scope_refs.map(objectName));
    const back = await Promise.all(auth.exploration_scope_refs.map(objectName));
    const amount = q => q?.value === null || !q ? '未知 / 未授权' : `${q.value} ${q.currency || q.unit}`;
    for (const text of [
      `投资约定版本：${candidate.revision}；研究目标：${candidate.goals.join('；') || '尚未明确'}`,
      `交互范围：${scope.join('；') || '尚未选择'}`,
      `明确约束：${candidate.hard_constraints.join('；') || '尚未明确'}；未知项：${candidate.unknowns.join('；') || '未记录'}`,
      `约定后台授权：${auth.background_enabled ? '允许' : '关闭'}；后台范围：${back.join('；') || '尚未选择'}`,
      `每日共享总额：${amount(auth.daily_budget)}；单任务全生命周期：${amount(auth.per_task_budget)}`,
      `复核日金额：${amount(auth.review_daily_budget)}；探索日金额：${amount(auth.exploration_daily_budget)}`,
      `每批至多 ${auth.max_leads_per_batch} 条线索 / ${auth.max_new_tasks_per_batch} 个新任务；仍与运行设置取较小值`,
    ]) parent.append(el('p', {}, text));
  }
  async function cases(after = null) {
    const token = start('档案控制'), rows = await get(`/investor/cases?limit=50${after ? '&after=' + encodeURIComponent(after) : ''}`);
    const labels = await Promise.all(rows.map(c => listingName(c.listing_ref)));
    if (token !== epoch) return;
    body.append(el('p', {}, '暂停只停止后台；静音只停止通知；归档撤下观点且不自动重开；显式重开创建新研究阶段。固定关注单独保留。'));
    rows.forEach((c, i) => {
      const section = el('article');
      section.append(el('h4', {}, labels[i]), el('p', {}, `档案 v${c.revision} · ${c.status} · 后台${c.background_paused ? '暂停' : '可用'} · 通知${c.muted ? '静音' : '可用'} · ${c.pinned ? '固定关注' : '未固定'}`));
      const reason = input('本次档案操作理由', '', 'text', section);
      for (const [action, label] of Object.entries(ACTION_LABELS)) {
        const node = button(label, () => {
          if (!reason.value.trim()) {status.textContent = '请补充本次操作理由。'; return;}
          execute({operation: 'case_action', case_ref: reference('case', c), action, reason: reason.value.trim()}, node);
        });
        section.append(node);
      }
      const pin = button(c.pinned ? '取消固定关注' : '固定关注', () => send(`/investor/cases/${c.id}/pinned`, {
        pinned: !c.pinned, mutation: {expected_revision: c.revision, idempotency_key: randomUUID()},
      }, pin, 'PATCH'));
      section.append(pin);
      audit(section, reference('case', c));
      body.append(section);
    });
    if (rows.length === 50) body.append(button('下一页档案', () => load(() => cases(rows.at(-1).id))));
    body.append(button('刷新档案状态', () => load(cases)));
  }
  function checkContext(version) {
    if (version !== contextVersion) {
      status.textContent = '当前对象已经变化，旧表单停止提交。请重新打开并审阅对象。';
      return false;
    }
    return true;
  }
  async function decision() {
    const token = start('录入用户决定'), version = contextVersion;
    const candidates = structuredClone(contextRefs.filter(r => ['case', 'recommendation'].includes(r.kind)));
    body.append(el('p', {}, '决定仅记录你的选择，不生成持仓、成交或现金预占。请明确选择本次决定对应的档案或观点。'));
    if (!candidates.length) {
      body.append(el('p', {}, '尚无明确档案或观点。请从公司档案或观点卡选择对象后再录入。'));
      return;
    }
    const labels = await Promise.all(candidates.map(objectName));
    if (token !== epoch) return;
    const target = select('本次决定的明确对象', [['', '请选择一个对象'], ...labels.map((name, i) => [`${candidates[i].kind}:${candidates[i].id}:${candidates[i].revision}`, name])]);
    const kind = select('决定', Object.entries(DECISION_LABELS));
    const text = input('你的决定原话', '', 'textarea');
    const reason = input('理由（可选）', '', 'textarea');
    const save = button('保存决定', () => {
      if (!checkContext(version)) return;
      if (!target.value) {status.textContent = '请选择本次决定对应的一个确切对象。'; return;}
      if (!text.value.trim()) {status.textContent = '请补充决定原话。'; return;}
      const chosen = candidates.find(r => `${r.kind}:${r.id}:${r.revision}` === target.value);
      execute({operation: 'decision', recommendation_ref: chosen.kind === 'recommendation' ? chosen : null,
        case_ref: chosen.kind === 'case' ? chosen : null, decision: kind.value,
        user_text: text.value.trim(), reason: reason.value.trim() || null}, save, '/investor/decisions');
    });
    body.append(save);
    audit(body, candidates);
  }
  async function memories(after = null) {
    const token = start('偏好与未核实观察'), version = contextVersion;
    const scope = structuredClone(contextRefs.filter(r => ['case', 'listing'].includes(r.kind)));
    body.append(el('p', {}, '显式偏好与未核实观察分别保存，不自动升格为事实或投资约定。撤销排除未来原文与派生上下文，保留审计。'));
    const scopeLabel = await Promise.all(scope.map(objectName));
    if (token !== epoch) return;
    body.append(el('p', {}, `本次记忆范围：${scopeLabel.join('；') || '全局明确偏好'}`));
    const kind = select('记忆类型', [['explicit_preference', '显式偏好'], ['unverified_observation', '未核实观察']]);
    const text = input('明确要保存的内容', '', 'textarea');
    const expiry = input('有效截止（观察必填，含时区 ISO 时间）');
    const save = button('明确保存记忆', () => {
      if (!checkContext(version)) return;
      if (!text.value.trim()) {status.textContent = '请补充记忆内容。'; return;}
      if (kind.value === 'unverified_observation' && !expiry.value.trim()) {
        status.textContent = '未核实观察需要明确有效截止时间。'; return;
      }
      execute({operation: 'memory_save', kind: kind.value, text: text.value.trim(), scope_refs: scope,
        source_refs: [], valid_until: expiry.value.trim() || null}, save, '/investor/memories');
    });
    body.append(save);
    audit(body, {scope_refs: scope, source_refs: []});
    const rows = await get(`/investor/memories?limit=50${after ? '&after=' + encodeURIComponent(after) : ''}`);
    if (token !== epoch) return;
    for (const memory of rows) {
      const row = el('article');
      row.append(el('p', {}, `${memory.kind === 'explicit_preference' ? '显式偏好' : '未核实观察'} · ${memory.status} · v${memory.revision} · ${memory.text}`));
      const why = input('撤销理由', '', 'text', row);
      if (memory.status === 'active') {
        const revoke = button('撤销此记忆', () => {
          if (!why.value.trim()) {status.textContent = '请填写撤销理由。'; return;}
          execute({operation: 'memory_revoke', memory_ref: reference('memory', memory), reason: why.value.trim()},
            revoke, `/investor/memories/${memory.id}/revoke`);
        });
        row.append(revoke);
      }
      audit(row, memory);
      body.append(row);
    }
    if (rows.length === 50) body.append(button('下一页记忆', () => load(() => memories(rows.at(-1).id))));
    body.append(button('刷新记忆状态', () => load(memories)));
  }
  async function proposalSummary(parent, parameters) {
    switch (parameters.operation) {
      case 'case_action':
        parent.append(el('p', {}, `${await objectName(parameters.case_ref)}：${ACTION_LABELS[parameters.action]}；理由：${parameters.reason}`));
        break;
      case 'mandate_revision':
        parent.append(el('p', {}, `修改投资约定当前版本 ${parameters.expected_revision}`));
        await mandateSummary(parent, parameters.candidate);
        break;
      case 'decision': {
        const target = parameters.recommendation_ref || parameters.case_ref;
        parent.append(el('p', {}, `${target ? await objectName(target) : '无明确对象'}：${DECISION_LABELS[parameters.decision]}；原话：${parameters.user_text}；理由：${parameters.reason || '未填写'}`));
        break;
      }
      case 'memory_save': {
        const scope = await Promise.all(parameters.scope_refs.map(objectName));
        parent.append(el('p', {}, `保存${parameters.kind === 'explicit_preference' ? '显式偏好' : '未核实观察'}：${parameters.text}`),
          el('p', {}, `范围：${scope.join('；') || '全局'}；有效截止：${parameters.valid_until || '未设期限'}`));
        break;
      }
      case 'memory_revoke': {
        const memory = await get(`/investor/memories/${parameters.memory_ref.id}?revision=${parameters.memory_ref.revision}`);
        const scope = await Promise.all(memory.scope_refs.map(objectName));
        parent.append(el('p', {}, `撤销${memory.kind === 'explicit_preference' ? '显式偏好' : '未核实观察'}：${memory.text}`),
          el('p', {}, `记忆版本 ${parameters.memory_ref.revision}；范围：${scope.join('；') || '全局'}；原截止：${memory.valid_until || '无期限'}；理由：${parameters.reason}。未来原文与派生上下文排除，审计保留。`));
        break;
      }
    }
  }
  async function proposals(after = null) {
    const token = start('操作提案审阅'), rows = await get(`/investor/proposals?status=pending&limit=50${after ? '&after=' + encodeURIComponent(after) : ''}`);
    if (token !== epoch) return;
    if (!rows.length) body.append(el('p', {}, '没有待审阅提案。'));
    for (const proposal of rows) {
      const row = el('article');
      row.append(el('h4', {}, `待审阅提案 · v${proposal.revision}`),
        el('p', {}, `来源：${proposal.source === 'trusted_task' ? '研究任务提出，尚未获授权' : '明确用户请求'}；批准有效截止：${proposal.expires_at}`));
      await proposalSummary(row, proposal.parameters);
      if (token !== epoch) return;
      audit(row, proposal);
      const why = input('提案决定理由', '', 'text', row);
      for (const approve of [true, false]) {
        const node = button(approve ? '批准上述确切变更' : '拒绝此提案', () => {
          if (!why.value.trim()) {status.textContent = '请填写本次决定理由。'; return;}
          send(`/investor/proposals/${proposal.id}/resolve`, {
            parameters: proposal.parameters, mutation: {expected_revision: proposal.revision, idempotency_key: randomUUID()},
            approve, reason: why.value.trim(),
          }, node);
        });
        row.append(node);
      }
      body.append(row);
    }
    if (rows.length === 50) body.append(button('下一页待处理提案', () => load(() => proposals(rows.at(-1).id))));
    body.append(button('刷新提案并重新审阅', () => load(proposals)));
  }
  function updateContext(refs) {
    const signature = JSON.stringify([root.dataset.controlConversation || null,
      (refs || []).map(r => `${r.kind}:${r.id}:${r.revision}:${r.field_path || ''}`).sort()]);
    if (signature === contextSignature) return;
    contextSignature = signature;
    contextRefs = refs || [];
    ++contextVersion;
    const form = body.querySelector('textarea');
    if (form) status.textContent = '当前对象已变化，已打开的决定/记忆表单须重新打开审阅后提交。';
  }
  nav.append(button('启用与投资约定', () => load(setup)), button('档案控制', () => load(cases)),
    button('录入决定', () => load(decision)), button('记忆管理', () => load(memories)),
    button('操作提案', () => load(proposals)));
  // The toolbar switches between views: keep the current one highlighted (aria-pressed styles it).
  for (const node of nav.children) node.setAttribute('aria-pressed', 'false');
  nav.addEventListener('click', ({target}) => {
    const clicked = target.closest('button');
    if (!clicked || !nav.contains(clicked)) return;
    for (const node of nav.children) node.setAttribute('aria-pressed', String(node === clicked));
  });
  // History messages do not choose an operation target. Only explicit current
  // bindings or the AG18 card/page chosen by the user may supply candidates.
  root.addEventListener('agent:binding', ({detail}) => {
    const signature = JSON.stringify([detail.conversationId || null,
      (detail.refs || []).map(r => `${r.kind}:${r.id}:${r.revision}:${r.field_path || ''}`).sort()]);
    // AG17 may repeat its unchanged binding while restoring names or messages.
    // Such a repeat must not override a subsequently selected AG18 opinion.
    if (signature === lastBindingSignature) return;
    lastBindingSignature = signature;
    updateContext(detail.refs);
  });
  root.addEventListener('agent:control-context', ({detail}) => updateContext(detail.refs));
  root.addEventListener('agent:messages', ({detail}) => {
    if (root.dataset.controlConversation !== detail.conversationId) {
      root.dataset.controlConversation = detail.conversationId;
      updateContext([]);
    }
  });
  body.append(el('p', {}, 'Agent 默认关闭。先查看启用与投资约定；已保存历史始终可读。'));
}
