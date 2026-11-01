import { randomUUID } from "./uuid.js";
/* Exact saved body + UUID survives lost acknowledgements and reloads. */
import {ApiError, get, post} from './api.js';
import {formatDateTime} from './format.js';
(() => {
  'use strict';
  const root = document.getElementById('method-workbench');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const states = {evaluating: '评估中', shadow: '等待影子检查与批准', active: '已启用', rejected: '已拒绝', retired: '已退役'};
  let data;
  let busy = false;
  let offset = 0;
  let selected = null;
  const storageKey = 'investor.method.pending.v1';
  const status = text => { $('method-status').textContent = text; };
  const element = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const scopeName = ref => data.scope_names[String(ref.id)] || `证券 ${ref.id}`;
  const refText = ref => `${ref.kind} ${ref.id} · 版本 ${ref.revision}`;
  const listing = review => review.inputs.task.binding.base_refs.filter(r => r.kind === 'listing');
  const option = (select, value, text) => { const node = element('option', text); node.value = value; select.append(node); };
  const button = (host, text, handler, kind) => { const node = element('button', text); node.type = 'button'; node.className = kind ? `btn ${kind}` : 'btn'; node.onclick = handler; host.append(node); return node; };
  // 包提案（卡 21）：概要带 package_key（路由层附加），详情带 package_ref（服务端冻结）。
  const isPackageProposal = p => p.request.package_ref != null || p.request.package_key != null;
  const shortId = value => String(value ?? '').slice(0, 8);
  // §9 三态重放结论的固定文案；config-only 检查结果（result/errors）不经过这里。
  const replayStateText = state => ({pass: '通过', block: '拦截', not_replayable: '不可重放'})[state] ?? '—';
  const disciplineLabels = discipline => {
    const labels = [];
    if (!discipline) return labels;
    if (discipline.require_two_counter_hypotheses) labels.push('两个不同反向解释');
    if (discipline.require_verified_counter_evidence) labels.push('已验证反证');
    if (discipline.require_explicit_stop_decision) labels.push('明确停止或继续判断');
    return labels;
  };
  // 步骤条：当前步骤高亮，之前的步骤标为已完成（只改 class，不碰数据）。
  const setStep = current => { for (const index of [1, 2, 3]) { for (const id of [`method-step-${index}`, `method-panel-${index}`]) { const node = $(id); if (!node) continue; if (index === current) node.classList.add('on'); else node.classList.remove('on'); if (index < current) node.classList.add('done'); else node.classList.remove('done'); } } };
  const stateKind = {evaluating: 'ai', shadow: 'ai', active: 'accent', rejected: 'warn', retired: ''};
  // 请求统一走共享客户端 api.js（超时中断、CSRF/幂等头、401/403/409/422 分类、
  // 非 JSON 响应兜底都由它负责）；本页只保留 /investor/methods 前缀。
  const BASE = '/investor/methods';
  // api.js 的文案与本页旧实现不同（401、对象 detail、超时/断网）；在调用点翻译回
  // 用户一直看到的措辞：中断（超时）由 ApiError.aborted 区分，status 分类保持不变。
  function legacyMessage(error) {
    if (error.status === 0) return error.aborted ? '请求超时' : '网络请求失败或超时';
    if (error.status === 401) return '登录已过期';
    const detail = error.detail;
    if (detail && typeof detail === 'object' && detail.message) return String(detail.message);
    if (typeof detail === 'string') return detail;
    return `请求失败 ${error.status}`;
  }
  async function api(path, body) {
    try {
      return body === undefined ? await get(BASE + path) : await post(BASE + path, body);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      throw Object.assign(new Error(legacyMessage(error)), {status: error.status});
    }
  }
  async function mutate(path, body) {
    if (busy) return;
    busy = true;
    try {
      const previous = sessionStorage.getItem(storageKey);
      const pending = previous ? JSON.parse(previous) : {path, body};
      if (previous && (pending.path !== path || JSON.stringify({...pending.body, mutation_id: null}) !== JSON.stringify({...body, mutation_id: null}))) {
        status('还有待确认的操作，请先重试原操作或刷新核对记录。'); return;
      }
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
      status('正在保存明确的操作内容…');
      const result = await api(pending.path, pending.body);
      sessionStorage.removeItem(storageKey);
      // 卡 21：候选影子的两种新回执补专门提示；既有路径文案逐字保留。
      const note = result.status === 'running' ? '离线运行已有领取记录，请查看影子结果。中断后需要发起新的试验。'
        : result.status === 'queued' ? '候选影子任务已发起：真实研究任务，占用研究预算；无发布、通知或账本副作用。任务完成后请回此处登记结果。'
        : result.candidate_shadow ? '候选影子结果已登记，可刷新核对覆盖情况。'
        : '已保存。提案创建不会自动启用方法。';
      status(note);
      selected = pending.body.proposal_id || result.proposal?.id || (result.request ? result.id : selected);
      offset = 0; await load();
    } catch (error) {
      status(error.status === 409 ? '版本或证据已改变，已刷新记录。请核对变化后重新操作。' : `${error.message}。原操作内容已保留，可重试。`);
      if (error.status === 409 || error.status === 422) { sessionStorage.removeItem(storageKey); await load(); }
    } finally { busy = false; }
  }
  function evidence(host, item) {
    const request = item.proposal.request;
    host.append(element('p', `适用证券：${scopeName(request.listing_ref)}；原方法版本 ${request.old_method.revision}`));
    const audit = element('details'); audit.append(element('summary', '审核精确引用'), element('p', `适用范围：${refText(request.listing_ref)}；原方法：${refText(request.old_method)}；冻结复盘：${request.review_id}`)); host.append(audit);
    host.append(element('p', `错误模式：${request.error_pattern}`));

    const labels = [];
    if (request.config.require_two_counter_hypotheses) labels.push('两个不同反向解释');
    if (request.config.require_verified_counter_evidence) labels.push('已验证反证');
    if (request.config.require_explicit_stop_decision) labels.push('明确停止或继续判断');
    host.append(element('p', `步骤变化：${labels.join('；')}`));
    for (const evaluation of item.evaluations) {
      const box = element('details'); box.append(element('summary', `评估 ${evaluation.id} · ${evaluation.passed ? '通过工程检查' : '未通过'} · 完整：${evaluation.complete ? '是' : '否'}`));
      for (const sample of evaluation.cases) {
        // 包提案的 proposed 是三态重放结果（state/replayed，无 result/errors）；
        // config-only 两条路径都有 result/errors，输出逐字不变。
        const proposed = sample.proposed.result ?? replayStateText(sample.proposed.state);
        const check = (sample.proposed.errors || []).join('；') || (sample.proposed.state ? `${replayStateText(sample.proposed.state)}${(sample.proposed.blocked_requirements || []).length ? `（${sample.proposed.blocked_requirements.join('；')}）` : ''}` : '通过');
        box.append(element('p', `${sample.role === 'failure' ? '失败样本' : '反例'} ${sample.review_id} · 适用：${sample.applicable ? '是' : '否（原方法）'} · 预期 ${sample.expected} · 基线 ${sample.baseline.result} · 提案 ${proposed}`));
        box.append(element('p', `实际检查：${check}；冻结材料 hash：${sample.frozen_review.input_hash}`));
        box.append(element('p', `实际问题：${sample.frozen_review.progress.question}；停止说明：${sample.frozen_review.progress.stopped_because || '未保存'}`));
      }
      host.append(box);
    }
    for (const shadow of item.shadows) {
      const box = element('details'); box.append(element('summary', `离线影子 ${shadow.id} · ${shadow.passed ? '通过工程检查' : '未通过'} · 评估 ${shadow.evaluation_id}`));
      for (const sample of shadow.cases || []) {
        // 包提案离线影子的结论在 candidate_conclusion/errors（无 checked）；config-only 仍读 checked。
        const conclusion = sample.checked ? sample.checked.result : `${sample.candidate_conclusion}${sample.matched ? '' : '（与预期不符）'}`;
        const detail = sample.checked ? sample.checked.errors.join('；') : (sample.errors || []).join('；');
        box.append(element('p', `${sample.role === 'failure' ? '失败样本' : '反例'} ${sample.review_id} · 预期 ${sample.expected} · 实际 ${conclusion} · ${detail}`));
        const progress = sample.output.output;
        if (progress) box.append(element('p', `影子实际输出：${progress.question}；反向解释 ${(progress.hypotheses || []).map(h => h.alternative).join('；')}；停止说明 ${progress.stopped_because || '未保存'}`));
      }
      host.append(box);
    }
    for (const shadow of item.prospective || []) {
      const box = element('details'); box.append(element('summary', `前瞻步骤影子 ${shadow.id} · ${shadow.passed ? '通过工程检查' : '未通过'} · 不代表研究语义质量`));
      for (const sample of shadow.cases) {
        box.append(element('p', `评估后新任务 ${sample.task_id} · 修订 ${sample.task_revision} · 预期 ${sample.expected} · 基线 ${sample.baseline.result} · 拟启用步骤 ${sample.proposed.result}`));
        box.append(element('p', `真实输出：${sample.frozen_review.progress.question}；反向解释 ${(sample.frozen_review.progress.hypotheses || []).map(h => h.alternative).join('；')}；检查 ${sample.proposed.errors.join('；') || '通过'}；冻结 hash ${sample.frozen_review.input_hash}`));
      }
      host.append(box);
    }
  }
  // §9 结构对比分组：id 前缀到中文标签（与后端 _STRUCTURE_DIFF_GROUPS 对应）。
  const diffGroups = [
    ['new_blocking_steps', '新增拦截步骤'],
    ['outputs', '输出收紧'],
    ['dimension_minimums', '维度下限提高'],
    ['depends_on', '新增依赖'],
    ['discipline', '纪律开关'],
    ['applicable_kinds', '适用研究类型'],
    ['gap_tightenings', '缺口容忍收紧'],
  ];
  // §9 包提案工作台四块：candidate / structure_diff / replay / candidate_shadows。
  // 只读展示 detail 附带的投影；缺失的块跳过，不影响 config-only 详情。
  function packageEvidence(card, item) {
    const candidate = item.candidate || {};
    card.append(element('p', `候选包：${candidate.package_key ?? '—'} @ 版本 ${candidate.version ?? '—'} · 内容 hash ${candidate.content_hash ?? '—'}`));
    const labels = disciplineLabels(candidate.discipline);
    if (labels.length) card.append(element('p', `包内纪律开关：${labels.join('；')}`));
    const structure = item.structure_diff;
    if (structure) {
      const box = element('details');
      box.append(element('summary', `结构对比 · 旧方法形态：${structure.old_package_kind === 'package' ? '方法包' : '旧基线（legacy）'}`));
      let shown = 0;
      for (const [key, label] of diffGroups) {
        const group = structure[key];
        if (!group || !group.items.length) continue;
        shown += 1;
        box.append(element('p', `${label}（${group.total}）：${group.items.join('；')}${group.truncated ? `（仅展示前 ${group.items.length} 条）` : ''}`));
      }
      const enhancements = structure.enhancement_ids;
      if (enhancements && enhancements.items.length) box.append(element('p', `全部增强项（${enhancements.total}）：${enhancements.items.join('；')}${enhancements.truncated ? `（仅展示前 ${enhancements.items.length} 条）` : ''}`));
      if (!shown && !(enhancements && enhancements.items.length)) box.append(element('p', '无结构差异条目。'));
      card.append(box);
    }
    const replay = item.replay;
    if (replay) {
      const box = element('details');
      const required = replay.requires_candidate_shadow || [];
      box.append(element('summary', `失败样本与反例重放 · 待候选影子覆盖 ${required.length} 项`));
      if (required.length) box.append(element('p', `待候选影子覆盖项：${required.join('；')}`));
      for (const sample of replay.samples || []) {
        const applicable = sample.applicable === true ? '是' : sample.applicable === false ? '否' : '—';
        box.append(element('p', `${sample.role === 'failure' ? '失败样本' : '反例'} ${sample.review_id} · 适用：${applicable} · 重放：${replayStateText(sample.state)}${(sample.not_replayable || []).length ? ` · 不可重放项：${sample.not_replayable.join('；')}` : ''}`));
      }
      card.append(box);
    }
    const shadows = item.candidate_shadows;
    if (shadows) {
      const box = element('details');
      box.append(element('summary', `候选影子结果 ${shadows.total} 条`));
      for (const shadow of shadows.items || []) {
        // item 无 matched 字段：按 expected/outcome 推导（与登记侧一致）。
        const matched = (shadow.expected === 'pass' && shadow.outcome === 'passed') || (shadow.expected === 'block' && shadow.outcome === 'blocked');
        const covered = shadow.requirement_ids || [];
        box.append(element('p', `影子任务 ${shortId(shadow.task_id)} · 预期 ${shadow.expected} · 实际 ${shadow.outcome} · ${matched ? '与预期一致' : '与预期不符'} · 覆盖 ${covered.length} 项${covered.length ? `：${covered.join('；')}` : ''}`));
      }
      if (!(shadows.items || []).length) box.append(element('p', '尚无已登记的候选影子结果。'));
      if (shadows.truncated) box.append(element('p', `仅展示最近 ${shadows.items.length} 条，共 ${shadows.total} 条。`));
      card.append(box);
    }
  }
  // §7.3/卡 16 批准门槛的展示镜像：服务端仍为权威，409 由 mutate() 处理。
  // 返回缺口说明；空数组表示候选影子证据满足。
  function packageApprovalGaps(item) {
    const shadows = item.candidate_shadows?.items || [];
    const required = item.replay?.requires_candidate_shadow || [];
    const positive = [...shadows].reverse().find(s => s.expected === 'pass' && s.outcome === 'passed');
    const missing = positive ? required.filter(id => !(positive.requirement_ids || []).includes(id)) : required;
    const gaps = [];
    if (!positive) gaps.push('匹配成功的正例候选影子');
    if (missing.length) gaps.push(`待覆盖：${missing.join('、')}`);
    if (!shadows.some(s => s.expected === 'block' && s.outcome === 'blocked')) gaps.push('被拒绝的反例影子');
    return gaps;
  }
  // §2 候选影子操作（包提案 status=shadow 且评估通过）：单发、批量与结果登记。
  function candidateShadowOps(card, item) {
    const p = item.proposal;
    const box = element('div'); box.className = 'method-shadow-ops';
    box.append(element('p', '候选影子：真实 worker 研究任务，占用研究预算；无发布、通知或账本副作用。离线影子（上方按钮）不能替代候选影子。'));
    const prompt = element('input'); prompt.maxLength = 2000; prompt.setAttribute('aria-label', '候选影子任务的研究问题');
    prompt.placeholder = '候选影子任务的研究问题（prompt）';
    const singleExpected = element('select'); singleExpected.setAttribute('aria-label', '单发候选影子预期');
    option(singleExpected, 'pass', '预期通过（pass）'); option(singleExpected, 'block', '预期拦截（block）');
    const singleRow = element('div'); singleRow.className = 'method-shadow-row';
    singleRow.append(prompt, singleExpected);
    button(singleRow, '发起候选影子任务', () => { const text = prompt.value.trim(); if (!text) return status('请先填写候选影子任务的研究问题。'); return mutate(`/proposals/${p.id}/candidate-shadow`, {mutation_id: randomUUID(), prompt: text, expected: singleExpected.value}); }, 'btn-sm');
    box.append(singleRow);
    const batchText = element('textarea'); batchText.setAttribute('aria-label', '批量候选影子研究问题（每行一条，最多 4 条）');
    batchText.placeholder = '批量发起：每行一个研究问题，最多 4 行（空行忽略），prompt 不得重复';
    const batchExpected = element('select'); batchExpected.setAttribute('aria-label', '批量候选影子预期');
    option(batchExpected, 'pass', '预期通过（pass）'); option(batchExpected, 'block', '预期拦截（block）');
    const batchRow = element('div'); batchRow.className = 'method-shadow-row';
    batchRow.append(batchText, batchExpected);
    button(batchRow, '批量发起候选影子', () => {
      const items = batchText.value.split('\n').map(line => line.trim()).filter(Boolean);
      if (!items.length) return status('请先填写批量候选影子的研究问题，每行一条。');
      if (items.length > 4) return status(`批量候选影子每次最多 4 条，当前 ${items.length} 条，请删减后重试。`);
      if (new Set(items).size !== items.length) return status('同一批候选影子的研究问题不得重复。');
      return mutate(`/proposals/${p.id}/candidate-shadow-batch`, {mutation_id: randomUUID(), items: items.map(text => ({prompt: text, expected: batchExpected.value}))});
    }, 'btn-sm');
    box.append(batchRow);
    const pending = (item.candidate_shadow_claims?.items || []).filter(claim => !claim.registered);
    if (pending.length) {
      box.append(element('p', '待登记结果的候选影子（任务完成后登记，以服务端校验为准）：'));
      for (const claim of pending) {
        const line = element('p'); line.className = 'method-claim-line';
        line.append(element('span', `任务 ${shortId(claim.task_id)} · ${claim.expected === 'pass' ? '预期通过' : '预期拦截'} · ${claim.prompt.length > 60 ? claim.prompt.slice(0, 60) + '…' : claim.prompt}`));
        button(line, '登记影子结果', () => mutate('/candidate-shadow-results', {mutation_id: randomUUID(), proposal_id: p.id, task_id: claim.task_id, expected_revision: p.revision}), 'btn-sm');
        box.append(line);
      }
    }
    card.append(box);
  }
  function render() {
    for (const id of ['method-task', 'method-failure', 'method-proposals', 'method-heads']) $(id).replaceChildren();
    for (const task of data.completed_tasks) option($('method-task'), task.id, `${task.status} · ${task.id}`);
    for (const review of data.reviews.filter(r => ['partial', 'failed'].includes(r.status) && listing(r).length === 1)) option($('method-failure'), review.id, `${review.status} · ${review.progress.question} · ${review.id}`);
    for (const item of data.proposals) {
      const p = item.proposal;
      const card = element('article'); card.className = 'method-card';
      const top = element('div'); top.className = 'method-card-head'; const badge = element('span', states[p.status]); badge.className = `badge ${stateKind[p.status] || ''}`.trim(); top.append(element('h3', p.request.summary), badge);
      card.append(top, element('p', `提案 ${p.id} · 修订 ${p.revision}`));
      if (!item.detail_loaded) {
        card.append(element('p', `步骤改进：${p.request.summary}`));
        button(card, '查看变更与冻结证据', async () => { selected = p.id; try { const detail = await api('/proposals/' + p.id); data.proposals = data.proposals.map(v => v.proposal.id === p.id ? detail : v); render(); } catch (error) { status(`${error.message}。${error.status === 401 ? '请重新登录后重试。' : '详情未加载，可重试。'}`); } }, 'btn-sm');
        $('method-proposals').append(card); continue;
      }
      evidence(card, item);
      if (isPackageProposal(p)) packageEvidence(card, item);
      const history = element('details'); history.append(element('summary', '方法完整历史与原始证据'));
      history.append(element('p', `已保存 ${item.history_counts.evaluation_ids} 次评估、${item.history_counts.shadow_ids} 次离线重放、${item.history_counts.prospective_ids} 次前瞻步骤影子。当前展示最近证据；所有旧记录保留。`));
      const historyRows = element('div'); history.append(historyRows);
      let historyOffset = 0;
      button(history, '读取下一页历史引用', async () => { if (historyOffset === null) return; try { const page = await api('/proposals/' + p.id + '/history?offset=' + historyOffset); for (const record of page.items) { const link = element('a', `${record.kind} · ${record.id} · ${formatDateTime(record.created_at)}`); link.href = '/investor/methods/artifacts/' + record.kind + '/' + record.id; link.target = '_blank'; link.rel = 'noopener'; const line = element('p'); line.append(link, element('span', ` · hash ${record.hash}`)); historyRows.append(line); } historyOffset = page.next_offset; if (historyOffset === null) historyRows.append(element('p', '已显示全部历史引用。')); } catch (error) { status(`${error.message}。历史未翻页，可重试。`); } }, 'btn-ghost btn-sm');
      card.append(history);
      if (p.status === 'evaluating') {
        const select = element('select'); select.setAttribute('aria-label', '成功反例复盘');
        for (const review of data.reviews.filter(r => r.status === 'succeeded' && listing(r).some(ref => ref.id === p.request.listing_ref.id))) option(select, review.id, `${review.progress.question} · ${review.id}`);
        const outside = element('select'); outside.setAttribute('aria-label', '适用范围外成功反例复盘');
        for (const review of data.reviews.filter(r => r.status === 'succeeded' && !listing(r).some(ref => ref.id === p.request.listing_ref.id))) option(outside, review.id, `${review.progress.question} · ${review.id}`);
        card.append(element('p', '原失败样本必须被加强步骤拦截；同范围成功反例不能被误伤；范围外反例保留原方法。'), select, outside);
        button(card, '评估真实失败与反例', () => { if (!select.value || !outside.value) return status('请先冻结同证券与范围外的成功任务作为反例。'); return mutate('/evaluations', {mutation_id: randomUUID(), proposal_id: p.id, expected_revision: p.revision, cases: [{review_id: p.request.review_id, role: 'failure', expected: 'block'}, {review_id: select.value, role: 'counterexample', expected: 'pass'}, {review_id: outside.value, role: 'counterexample', expected: 'pass'}]}); }, 'btn-sm');
      }
      if (p.status === 'shadow') {
        // evaluations/evaluation_ids 可能为空：判空后再启用按钮，避免 render 阶段 TypeError。
        const latestEvaluationId = p.evaluation_ids.at(-1) || null;
        const evaluation = item.evaluations.at(-1) || null;
        const noEvaluation = () => status('尚无已保存评估，请先完成真实失败与反例评估。');
        const shadow = button(card, '运行冻结样本的离线影子检查', () => latestEvaluationId ? mutate('/offline-shadow', {mutation_id: randomUUID(), proposal_id: p.id, expected_revision: p.revision, evaluation_id: latestEvaluationId}) : noEvaluation(), 'btn-sm');
        if (!latestEvaluationId) shadow.disabled = true;
        // §2 候选影子操作入口：包提案且评估通过（与发起侧门槛一致）。
        if (isPackageProposal(p) && item.evaluations.at(-1)?.passed) candidateShadowOps(card, item);
        const newTasks = evaluation ? data.completed_tasks.filter(t => Date.parse(t.created_at) > Date.parse(evaluation.created_at) && t.base_refs.some(r => r.kind === 'listing' && r.id === p.request.listing_ref.id)) : [];
        const failure = element('select'), success = element('select');
        failure.setAttribute('aria-label', '评估后新建的不完整研究任务'); success.setAttribute('aria-label', '评估后新建的成功研究任务');
        for (const task of newTasks.filter(t => ['failed', 'partial'].includes(t.status))) option(failure, task.id, `${task.status} · ${task.id}`);
        for (const task of newTasks.filter(t => t.status === 'succeeded')) option(success, task.id, `${task.status} · ${task.id}`);
        card.append(element('p', '前瞻步骤影子：请先在对话中明确发起新的同证券研究，完成后刷新选择。仅检查真实新输出中的步骤，不额外调用模型、不改正式观点或发提醒。离线重放不能替代此项。'), failure, success);
        const prospective = button(card, '记录新任务的前瞻步骤影子', () => { if (!latestEvaluationId) return noEvaluation(); if (!failure.value || !success.value) return status('需要评估后新建的同证券成功和不完整研究任务。'); return mutate('/prospective-shadow', {mutation_id: randomUUID(), proposal_id: p.id, expected_revision: p.revision, evaluation_id: latestEvaluationId, task_ids: [failure.value, success.value]}); }, 'btn-sm');
        if (!latestEvaluationId) prospective.disabled = true;

      }
      if (['evaluating', 'shadow'].includes(p.status)) {
        const decision = action => mutate('/decisions', {mutation_id: randomUUID(), proposal_id: p.id, expected_revision: p.revision, expected_head_revision: item.head_revision, listing_ref: p.request.listing_ref, evidence_hash: item.evidence_hash, summary: item.approval_summary, decision: action});
        if (isPackageProposal(p)) {
          // §3 包提案批准入口：镜像候选影子覆盖门槛；按钮始终显示，未满足时禁用并说明缺口。
          const evaluationPassed = Boolean(item.evaluations.at(-1)?.passed);
          const gaps = evaluationPassed ? packageApprovalGaps(item) : ['通过的评估'];
          const approve = button(card, '明确批准并用于该证券的新任务', () => decision('approve'), 'btn-primary');
          if (!evaluationPassed || gaps.length) { approve.disabled = true; card.append(element('p', `批准前还缺：${gaps.join('；')}`)); }
        } else if (item.evaluations.at(-1)?.passed && item.prospective?.at(-1)?.passed) {
          button(card, '明确批准并用于该证券的新任务', () => decision('approve'), 'btn-primary');
        }
        button(card, '拒绝此提案', () => decision('reject'), 'btn-danger-ghost');
      }
      $('method-proposals').append(card);
    }
    if (!data.proposals.length) { const note = element('p', '尚无方法提案。需要真实已完成任务复盘。'); note.className = 'muted method-none'; $('method-proposals').append(note); }
    setStep(data.proposals.length ? 3 : $('method-failure').children.length ? 2 : 1);
    // §4/§5 全局默认方法 head：未设置时新单证券公司研究/复核任务用 legacy v1 基线。
    const defaultHead = data.default_head;
    if (defaultHead && defaultHead.binding) {
      const binding = defaultHead.binding;
      let line = `全局默认方法（company-research）· 修订 ${defaultHead.revision} · 方法 ${shortId(binding.method_ref?.id)}`;
      if (binding.package_ref) line += ` · 包 ${binding.package_ref.revision} 版`;
      $('method-default-head').textContent = line;
    } else {
      $('method-default-head').textContent = '全局默认方法未设置：新的单证券公司研究/复核任务使用 legacy v1 基线。';
    }
    $('method-heads-empty').hidden = data.heads.length > 0;
    for (const head of data.heads) {
      const card = element('article'); card.className = 'method-card';
      card.append(element('p', `${scopeName(head.binding.listing_ref)} · 范围修订 ${head.revision} · 当前 ${refText(head.binding.method_ref)}`));
      const select = element('select'); select.setAttribute('aria-label', '回退目标版本');
      // Baseline reference supplied by the server, never an invented model ref.
      option(select, JSON.stringify(data.baseline_method), '固定基线方法');
      for (const version of data.versions.filter(v => v.binding.listing_ref?.id === head.binding.listing_ref.id && v.binding.method_ref.id !== head.binding.method_ref.id)) option(select, JSON.stringify(version.binding.method_ref), refText(version.binding.method_ref));
      const reason = element('input'); reason.placeholder = '说明回退原因'; reason.maxLength = 1000; reason.setAttribute('aria-label', '回退原因');
      card.append(select, reason);
      button(card, '明确回退并追加版本', () => { if (!reason.value.trim()) return status('请说明回退原因。'); return mutate('/rollback', {mutation_id: randomUUID(), listing_ref: head.binding.listing_ref, expected_head_revision: head.revision, target_method: JSON.parse(select.value), reason: reason.value.trim()}); }, 'btn-sm');
      $('method-heads').append(card);
    }
  }
  async function load() { try { data = await api('?offset=' + offset); const target = data.proposals.find(v => v.proposal.id === selected) || data.proposals[0]; if (target) { selected = target.proposal.id; const detail = await api('/proposals/' + selected); data.proposals = data.proposals.map(v => v.proposal.id === selected ? detail : v); } $('method-page-note').textContent = data.page.truncated ? '当前仅显示一页摘要，较早记录可翻页或按精确引用查看。' : '当前页已显示全部可用摘要。'; $('method-next').disabled = data.page.next_offset === null; $('method-prev').disabled = offset === 0; render(); } catch (error) { status(error.message); } }
  $('method-refresh').onclick = () => { offset = 0; return load(); };
  $('method-next').onclick = () => { offset = data.page.next_offset; return load(); };
  $('method-prev').onclick = () => { offset = Math.max(0, offset - 20); return load(); };
  $('method-review').onclick = () => { const task = $('method-task').value; if (task) mutate('/reviews/' + task, {}); };
  // §5 候选包 YAML（可选）：填写后以包提案提交（不发 config/package，服务端按包内
  // discipline 填充），三个 config-only 开关隐藏；留空走既有提交，逐字不变。
  const togglePackageChecks = () => { $('method-checks').hidden = $('method-package-yaml').value.trim().length > 0; };
  $('method-package-yaml').addEventListener('input', togglePackageChecks);
  $('method-package-yaml').addEventListener('change', togglePackageChecks);
  togglePackageChecks();
  $('method-create').onsubmit = event => {
    event.preventDefault(); const review = data.reviews.find(r => r.id === $('method-failure').value); if (!review) return status('请先保存一个真实失败或不完整复盘。');
    const refs = listing(review); if (refs.length !== 1) return status('只支持一个明确证券。');
    const packageYaml = $('method-package-yaml').value.trim();
    if (packageYaml) return mutate('/proposals', {mutation_id: randomUUID(), review_id: review.id, listing_ref: refs[0], old_method: review.old_method.method_ref, error_pattern: $('method-pattern').value, summary: $('method-summary').value, package_yaml: packageYaml});
    mutate('/proposals', {mutation_id: randomUUID(), review_id: review.id, listing_ref: refs[0], old_method: review.old_method.method_ref, error_pattern: $('method-pattern').value, summary: $('method-summary').value, config: {schema_version: 1, require_two_counter_hypotheses: $('method-two').checked, require_verified_counter_evidence: $('method-verified').checked, require_explicit_stop_decision: $('method-stop').checked}});
  };
  if (sessionStorage.getItem(storageKey)) {
    const pending = JSON.parse(sessionStorage.getItem(storageKey));
    button($('method-status').parentNode, '重试待确认的原操作', () => mutate(pending.path, pending.body));
    status('检测到待确认的操作。内容和操作编号已保留，可重试核对。');
  }
  load();
})();
