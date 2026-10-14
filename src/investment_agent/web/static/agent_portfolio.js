// Actual user facts are never inferred from decisions. Decimal values stay strings.
import {get, post} from './api.js';
import {el} from './dom.js';
import {formatDateTime} from './format.js';
const KEY = 'zcharlie.portfolio.pending.v1';
const labels = {published:'当前有效',review_required:'需要复核',expired:'已过期',superseded:'已被替代',buy:'买入',sell:'卖出',add:'加仓',hold:'继续持有',reduce:'减仓',exit:'退出',wait:'等待',further_research:'继续研究',company_thesis:'公司研究依据',portfolio_constraint:'账户硬约束依据',user_scenario:'用户情景假设',agent_scenario:'研究情景假设'};
const reason = value => {
  const raw = String(value), parts = raw.split(':');
  if (/constraint_(violation|unknown)/.test(parts[0])) return `${parts[0].includes('violation') ? '超过或未满足' : '尚不能检查'}${({max_single_weight:'单股占比上限',max_sector_weight:'行业占比上限',minimum_cash:'最低现金',minimum_cash_weight:'最低现金占比'})[parts[1]] || '账户约束'}${parts[0].startsWith('scenario') ? '（情景假设）' : '（用户正式约束）'}`;
  const code = parts[0].replace(/^action[._]\d+\./,'');
  return ({purpose_constraints_changed:'用户用途金额、期限或必留现金已变化，请按最新用途复核整案',purpose_cash_requires_review:'用途现金基础未知或已不足，请核对实际账户现金',purpose_required_cash_shortfall:'方案侵占必须保留的用途现金',purpose_overallocated:'实际现金已不足以覆盖原用途分配',purpose_association_required:'交易未明确关联现有资金用途',purpose_budget_exceeded:'交易和费用超过该用途预算',purpose_outlay_unknown:'用途支出或费用未知',purpose_reserve_unknown:'方案后保留现金未知',purpose_cash_unknown:'实际现金未知，不能确认用途资金覆盖',known_cash_missing:'确认现金缺失，不能确定可用资金',partial_asset_scope:'资产覆盖不完整，不能计算完整总资产和占比',complete_nav_missing:'总资产资料不完整，不能计算占比',trading_rule_missing:'可信交易规则缺失，不能给出正式交易数量',trading_rule_is_scenario_assumption:'交易规则仅为情景假设，不能授权正式交易数量',execution_price_missing:'成交价格假设未明确',fee_missing:'交易费用未知，不能按免费计算',confirmed_holding_missing:'持仓起点尚未确认',valuation_price_missing:'估值价格缺失，不能计算市值',sector_coverage_missing:'行业归属缺失，不能计算完整行业占比',formal_mandate_constraints_not_structured:'自由文字约束尚未确认成正式数值约束',actual_snapshot_changed:'实际组合事实已变化，请重新研究',snapshot_changed:'组合事实已变化，请重新研究',snapshot_superseded:'组合事实已变化，请重新研究',mandate_changed:'用户约束已修订，请重新研究',price_basis_stale:'价格基础已过期',cash_unknown:'现金未知，不能确定可用资金',fee_unknown:'费用未知，不能按免费计算',fees_unknown:'费用未知，不能按免费计算',partial_coverage:'资产覆盖不完整，不能计算完整总资产和占比',max_single_weight:'单股占比超过用户约定',max_sector_weight:'行业占比超过用户约定',minimum_cash:'现金低于用户约定',minimum_cash_weight:'现金占比低于用户约定',trading_rules_unavailable:'可信交易规则不可用，不能提供正式交易数量',quantity_unknown:'持仓数量未知',price_unknown:'价格未知'})[code] || `需要核对资料或约束：${value}`;
};
// Decimal-string presentation only: shifting digits, never computing cash or NAV.
export const percent = value => {
  if (value == null) return '未知';
  const parts = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
  if (!parts) return '数值格式无法展示（请查看精确来源）';
  let shift = BigInt(parts[4] || '0') + 2n, integer = parts[2], fraction = parts[3] || '';
  if (shift < -1000n || shift > 1000n) return '数值超出展示范围（请查看精确来源）';
  while (shift > 0n) {integer += fraction[0] || '0'; fraction = fraction.slice(1); shift--;}
  while (shift < 0n) {fraction = (integer.at(-1) || '0') + fraction; integer = integer.slice(0,-1) || '0'; shift++;}
  integer = integer.replace(/^0+(?=\d)/,''); fraction = fraction.replace(/0+$/,'');
  const sign = parts[1] === '-' && /[1-9]/.test(integer + fraction) ? '-' : '';
  const shown = fraction.length > 18 ? `${fraction.slice(0,4)}…` : fraction;
  return `${sign}${integer}${shown ? `.${shown}` : ''}%`;
};
const fieldLabel = field => String(field).replace(/options\.[^.]+\.actions\.[^.]+/g,'选项中的证券行动').replace(/options\.[^.]+/g,'方案选项');
const explain = message => ({'Known violated exact user typed hard constraint required':'目前无法确认该账户违反了用户已确认的数值约束；请补齐现金、持仓和费用后重新研究。','Portfolio ledger writes are disabled':'账本写入暂时关闭；原请求仍保留，恢复权限后重取原收据。'})[message] || message;
const unknown = value => value == null ? '未知（不能按零计算）' : String(value).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/,'');
const decimal = (node, optional = false) => {
  const value = node.value.trim();
  if (!value && optional) return null;
  if (!/^-?\d+(?:\.\d+)?$/.test(value)) throw Error('金额/数量须为完整十进制数；未知字段请留空。');
  return value;
};
export function bootAgentPortfolio() {
  const root = document.querySelector('#agent-workbench'), host = document.querySelector('#agent-portfolio');
  if (!root || !host) return;
  let purposes = null;
  let accounts = [], account = null, snapshot = null, epoch = 0, busy = false, pending = null, enabled = false, selectedId = null;
  try {pending = JSON.parse(sessionStorage.getItem(KEY) || 'null'); selectedId = sessionStorage.getItem(`${KEY}.account`);} catch {}
  const artifactReads = new Set();
  const status = el('p', {role:'status','aria-live':'polite'}), selector = el('select', {'aria-label':'组合账户'});
  const nav = el('div', {class:'toolbar'}), body = el('section'), summary = el('section');
  const retry = button('重取原待确认请求', () => transmit());
  host.append(el('h2', {}, '真实组合'), el('p', {class:'notice notice-warn'}, '工程演示默认关闭。真实研究质量验收尚未通过；生产默认可信交易规则不可用；隔离演示不能验证实盘规则。接受或以后买不会记为成交，也不预留现金。'), status, selector, nav, retry, summary, body);
  function error(exc) {
    const values = exc.detail?.field_errors || {};
    const fields = Array.isArray(exc.detail) ? exc.detail.map(x => `${x.loc.join(' · ')}：${x.msg}`).join('；') : Array.isArray(values) ? values.map(x => `${x.field}：${x.message}`).join('；') : Object.entries(values).map(([field,message])=>`${fieldLabel(field)}：${explain(message)}`).join('；');
    status.textContent = `${fields || explain(exc.detail?.message || exc.message)}${exc.status === 409 ? '。版本已变化，请刷新并重新审阅，原事实没有自动重写。' : ''}`;
  }
  function run(fn) {return Promise.resolve().then(fn).catch(error);}
  // v2 emphasis: the one confirming button of a form is primary; removing a row is a quiet warning action.
  function button(text, fn) {const b = el('button', {type:'button', class:/^确认/.test(text) ? 'btn-primary' : /^移除/.test(text) ? 'btn-danger-ghost' : undefined}, text); b.addEventListener('click', () => run(fn)); return b;}
  function field(label, value = '', type = 'text', parent = body) {
    const wrap = el('label', {}, label), node = el('input', {type,autocomplete:'off'}); node.value = value; wrap.append(node); parent.append(wrap); return node;
  }
  function choice(label, options, parent = body) {
    const wrap = el('label', {}, label), node = el('select'); options.forEach(([value,text]) => node.append(el('option',{value},text))); wrap.append(node); parent.append(wrap); return node;
  }
  function start(title) {body.dataset.mode = 'form'; status.textContent = ''; ++epoch; body.replaceChildren(el('h3',{},title)); return epoch;}
  function audit(parent, value) {const d = el('details'); d.append(el('summary',{},'原始事实、来源与版本'),el('pre',{class:'agent-control-review'},JSON.stringify(value,null,2))); parent.append(d);}
  function locks() {
    retry.hidden = !pending; retry.disabled = busy;
    selector.disabled = busy || !!pending;
    nav.querySelectorAll('button').forEach(x => x.disabled = busy || !!pending || (x.dataset.write === 'true' && !enabled));
    body.querySelectorAll('input,select,button').forEach(x => x.disabled = busy || !!pending);
  }
  async function write(path, request) {
    if (!enabled) throw Error('真实账本写入尚未启用；请在设置中明确配置后刷新。');
    if (busy || pending) throw Error('请先重取上次待确认请求。');
    status.textContent = '正在确认保存…';
    pending = {path, method:'POST', request:structuredClone(request)};
    // Storage must succeed before admitting the write.
    try {sessionStorage.setItem(KEY,JSON.stringify(pending));} catch {pending = null; throw Error('浏览器存储不可用，尚未发送。');}
    await transmit();
  }
  async function transmit() {
    if (!pending || busy) return;
    busy = true; locks(); const fixed = pending;
    try {
      const response = await post(fixed.path, fixed.request, {idempotency:false});
      if (fixed.path === '/portfolio/accounts') account = response;
      pending = null; sessionStorage.removeItem(KEY); start('事实已保存'); status.textContent = '服务器已确认保存。组合方案将按最新事实重新检查。';
      await refresh();
    } catch (exc) {
      if ([409,422].includes(exc.status)) {pending = null; sessionStorage.removeItem(KEY);}
      error(exc);
      if (exc.status === 409) {start('请刷新后重新录入与审阅'); audit(body,fixed.request); await refresh(); error(exc);}
    } finally {busy = false; locks();}
  }
  async function name(ref) {
    try {const hit = await get(`/agent/listings/${ref.id}`); return `${hit.name} · ${hit.ticker}`;} catch {return '证券身份暂不可读取（请核对来源）';}
  }
  async function picker(parent, token) {
    const query = field('查找证券名称或完整代码','','text',parent), results = el('div'), selected = el('p'); parent.append(results,selected);
    let ref = null, seq = 0;
    parent.append(button('查找证券', async () => {
      const version = ++seq, hits = await get(`/agent/listings?q=${encodeURIComponent(query.value.trim())}`);
      if (token !== epoch || version !== seq) return;
      results.replaceChildren(); if (!hits.length) results.append(el('p',{},'没有找到已保存证券。请核对完整代码。'));
      hits.forEach(hit => results.append(button(`${hit.name} · ${hit.ticker}`, () => {if (token !== epoch || version !== seq) return; ref = hit.ref; selected.textContent = `${hit.name} · ${hit.ticker}`; results.replaceChildren();})));
    }));
    return () => {if (!ref) throw Error('请查找并明确选择证券。'); return ref;};
  }
  async function refresh() {
    if (body.dataset.mode === 'plan') {start('组合记录已刷新，请重新查看当前方案。');}
    const token = ++epoch, id = account?.id || pending?.request?.account_id || selectedId;
    const [values, readiness] = await Promise.all([get('/portfolio/accounts'), get('/investor/readiness')]); if (token !== epoch) return;
    enabled = readiness.portfolio_ledger_enabled; locks();
    accounts = values; account = accounts.find(x => x.id === id) || accounts[0] || null;
    selectedId = account?.id || null; try {sessionStorage.setItem(`${KEY}.account`,selectedId || '');} catch {}
    selector.replaceChildren(el('option',{value:''},'请选择明确账户'));
    accounts.forEach(x => selector.append(el('option',{value:x.id},`${x.name} · 修订 ${x.revision}`))); selector.value = account?.id || '';
    snapshot = null; purposes = null; artifactReads.clear(); summary.replaceChildren();
    if (!enabled) summary.append(el('p',{},'账本写入关闭：请在设置页明确启用真实组合账本。读取历史不会启动模型。'));
    if (!account) {summary.append(el('p',{},'尚无账户。先记录真实账户和起点；未知现金留空。')); return;}
    try {snapshot = await get(`/portfolio/snapshot?account_id=${account.id}`);} catch (exc) {if (exc.status !== 404) throw exc;}
    if (token !== epoch) return;
    if (!snapshot) {summary.append(el('p',{},'尚无起点，请录入确认的余额。')); return;}
    purposes = await get(`/portfolio/purposes/current?account_id=${account.id}`); if (token !== epoch) return;
    if (purposes) {
      summary.append(el('h3',{},'已确认资金用途'),el('p',{},`用途状态：${({active:'当前有效',needs_review:'实际现金或资料变化，需复核',overallocated:'实际现金已不足，用途超额，需复核'})[purposes.status]}；未分配现金：${unknown(purposes.unallocated)} 元；必须保留现金：${unknown(purposes.required_cash)} 元`));
      for (const p of purposes.version.purposes) summary.append(el('p',{},`${p.name}：分配 ${unknown(p.amount)} 元，可用于方案 ${unknown(p.available_budget)} 元；${p.horizon === 'short_term' ? '短期' : '长期'}，${p.required_on} 必留 ${unknown(p.required_cash)} 元。来源：${p.source}（${p.source_version}）${p.unknowns.length ? '；待核对：' + p.unknowns.join('、') : ''}`));
    }
    summary.append(el('p',{class:'muted'},'新的事实不会替换已绑定或待确认消息中的旧组合版本；重新研究前请明确绑定最新记录。'));
    summary.append(el('p',{},`${account.name} · ${snapshot.coverage === 'complete' ? '完整' : '部分'}覆盖：${snapshot.asset_scope}`),el('p',{},`可确认现金：${unknown(snapshot.known_cash)} CNY；总资产：${unknown(snapshot.total_assets)} CNY`),el('p',{},`事实截至 ${formatDateTime(snapshot.fact_as_of)}；估值知识时点 ${formatDateTime(snapshot.valuation_at)}；快照版本 ${snapshot.revision}`));
    for (const holding of snapshot.holdings) {
      const label = await name(holding.listing_ref); if (token !== epoch) return;
      summary.append(el('p',{},`${label} · 确认数量 ${unknown(holding.confirmed_quantity)}；市值 ${unknown(holding.market_value)} CNY；单股占比（比例）${percent(holding.weight)}；价格 ${unknown(holding.price)} / ${holding.price_date || '未知日期'}`));
    }
    [...snapshot.unknowns,...snapshot.conflicts].forEach(x => summary.append(el('p',{class:'notice'},reason(x))));
    audit(summary,snapshot);
  }
  function requireAccount() {if (!account) throw Error('请先选择真实账户。'); return account;}
  function facts(token) {
    const date = field('事实日期与时间','','datetime-local');
    const zone = choice('事实时间的时区',[['+08:00','北京时间 UTC+08:00'],['+00:00','UTC+00:00']]);
    const preview = el('p',{class:'muted'},'请主动填写事实发生的日期和时间。'); body.append(preview);
    const timestamp = () => `${date.value}${date.value.length === 16 ? ':00' : ''}${zone.value}`;
    [date,zone].forEach(node=>node.addEventListener('change',()=>{preview.textContent = date.value ? `将保存事实时间：${date.value.replace('T',' ')}（${zone.selectedOptions[0].textContent}）` : '请主动填写事实发生的日期和时间。';}));
    const source = field('事实来源（对账单/成交回报等）');
    const confirmed = field('我确认这是已经发生的实际事实','','checkbox');
    return () => {
      if (token !== epoch) throw Error('账户或页面已变化，请重新录入。');
      if (!confirmed.checked) throw Error('请明确确认实际事实；接受、愿望和未来计划请用决定记录。');
      if (!date.value || !Number.isFinite(Date.parse(timestamp()))) throw Error('请选择实际发生日期、时间，并核对时区。');
      if (!source.value.trim()) throw Error('请填写事实来源。');
      return {occurred_at:timestamp(),source:source.value.trim(),currency:'CNY',user_confirmed:true};
    };
  }
  function saveFact(fact) {const exact = requireAccount(); return write('/portfolio/events',{account_id:exact.id,expected_revision:exact.revision,idempotency_key:crypto.randomUUID(),fact});}
  function purposeForm() {
    requireAccount(); if (!snapshot) throw Error('请先确认实际现金起点。');
    const token = start('确认资金用途'), rows = [], area = el('section');
    body.append(el('p',{},'用途只是规划分配，不是现金转账、券商预留或成交。分配合计由同一账户已确认现金覆盖；短期必留现金不能由公司长期观点覆盖。'),area);
    const add = value => {
      if (rows.length >= 12) throw Error('最多十二项用途。');
      const row = el('fieldset'); area.append(row);
      const entry = {id:value?.id || crypto.randomUUID(), row,
        name:field('用途名称',value?.name || '','text',row),
        amount:field('分配金额（元）',value?.amount || '','text',row),
        budget:field('可用于方案的预算（含费用，元）',value?.available_budget || '','text',row),
        reserve:field('必须保留现金（元）',value?.required_cash || '','text',row),
        horizon:choice('期限',[['short_term','短期'],['long_term','长期']],row),
        date:field('资金需求日期',value?.required_on || '','date',row),
        source:field('确认来源',value?.source || '用户确认','text',row),
        version:field('来源版本',value?.source_version || '','text',row),
        unknowns:field('尚待核对（可留空）',value?.unknowns.join('；') || '','text',row)};
      if (value) entry.horizon.value = value.horizon; rows.push(entry);
      row.append(button('移除用途',()=>{rows.splice(rows.indexOf(entry),1);row.remove();}));
    };
    if (purposes) purposes.version.purposes.forEach(add); else add();
    body.append(button('添加资金用途',()=>add()),button('确认保存用途规划',()=>{
      if (token !== epoch) throw Error('页面已变化，请刷新。');
      const values = rows.map(r=>({id:r.id,name:r.name.value.trim(),amount:decimal(r.amount),available_budget:decimal(r.budget),required_cash:decimal(r.reserve),horizon:r.horizon.value,required_on:r.date.value,source:r.source.value.trim(),source_version:r.version.value.trim(),unknowns:r.unknowns.value.trim() ? [r.unknowns.value.trim()] : []}));
      return write('/portfolio/purposes',{account_id:account.id,idempotency_key:crypto.randomUUID(),expected_revision:purposes?.version.reference.revision || 0,snapshot_ref:structuredClone(snapshot.reference),purposes:values,confirmation:'user_confirmed'});
    }));
  }
  function create() {
    const token = start('新建真实账户'), input = field('账户名称');
    body.append(button('确认新建账户', () => {if (token !== epoch) throw Error('页面已变化。'); if (!input.value.trim()) throw Error('请输入账户名称。'); return write('/portfolio/accounts',{name:input.value.trim(),currency:'CNY',idempotency_key:crypto.randomUUID()});}));
  }
  async function balance(kind) {
    requireAccount(); const token = start(kind === 'opening_balance' ? '记录组合起点' : '确认对账余额');
    const scope = field('资产范围（例如本账户全部A股和现金）'), coverage = choice('覆盖范围',[['partial','部分：仍有未知资产'],['complete','完整：已确认全部范围']]);
    const cash = field('现金金额（未知留空，确认零请输入0）'), holdings = [], area = el('section'); body.append(area);
    body.append(button('添加确认持仓', async () => {
      const row = el('fieldset'); area.append(row); const listing = await picker(row,token), quantity = field('确认持仓数量（可为0）','','text',row);
      const entry = {listing,quantity}; holdings.push(entry); row.append(button('移除此行',()=>{holdings.splice(holdings.indexOf(entry),1); row.remove();}));
    }));
    const explanation = kind === 'reconciliation' ? field('差异说明（无差异也明确写无）') : null, base = facts(token);
    body.append(button('确认保存余额事实',()=>{if (!scope.value.trim()) throw Error('请明确资产范围。'); return saveFact({...base(),kind,coverage:coverage.value,asset_scope:scope.value.trim(),cash:decimal(cash,true),holdings:holdings.map(x=>({listing_ref:x.listing(),quantity:decimal(x.quantity)})),prior_history:'included_in_confirmed_balance',unexplained_difference:explanation?.value.trim() || null});}));
  }
  async function trade() {
    requireAccount(); const token = start('已经发生的成交'), listing = await picker(body,token);
    const direction = choice('实际方向',[['buy','买入'],['sell','卖出']]), quantity = field('本次成交数量（不是最终持仓）'), price = field('实际成交价格'), fee = field('实际费用（未知留空，明确免费填0）'), base = facts(token);
    body.append(button('确认保存实际成交',()=>saveFact({...base(),kind:'trade',holding:{listing_ref:listing(),quantity:decimal(quantity)},direction:direction.value,actual_price:decimal(price),fee:decimal(fee,true)})));
  }
  async function cashFact(kind) {
    requireAccount(); const token = start(kind === 'dividend' ? '实际分红' : '实际资金转入/转出');
    const listing = kind === 'dividend' ? await picker(body,token) : null, amount = field('实际金额（转出为负数）'), base = facts(token);
    body.append(button('确认保存资金事实',()=>saveFact({...base(),kind,amount:decimal(amount),listing_ref:listing?.() || null})));
  }
  async function history() {
    const exact = requireAccount(), token = start('不可变事实历史与冲销修正');
    async function pageAfter(after = 0) {
    const page = await get(`/portfolio/events?account_id=${exact.id}&limit=100&after_revision=${after}`);
    if (token !== epoch) return;
    for (const event of page.items) {
      const row = el('section'); row.append(el('p',{},`修订 ${event.revision} · ${event.request.fact.kind} · ${event.request.fact.occurred_at} · 来源 ${event.request.fact.source}`)); audit(row,event);
      if (event.request.fact.kind !== 'reversal') row.append(button('冲销这条原始事实',()=>{
        const t = start('冲销已发生事实'), reason = field('冲销原因'), base = facts(t); audit(body,event);
        body.append(button('确认冲销原事实',()=>saveFact({...base(),kind:'reversal',original_event_id:event.id,reason:reason.value.trim()})));
      })); body.append(row);
    }
    if (page.next_after_revision) {const more = button('加载后续事实',async()=>{more.disabled = true; try {await pageAfter(page.next_after_revision); more.remove();} catch(exc) {more.disabled = false; throw exc;}}); body.append(more);}
    }
    await pageAfter();
    if (token === epoch && snapshot?.reconciliations.length) audit(body,snapshot.reconciliations);
  }
  async function mandate() {
    const exact = requireAccount(), token = start('用户确认的账户数值约束'), old = await get('/investor/mandate'); if (token !== epoch) return;
    body.append(el('p',{},'只修改此明确账户的数值约束。比例使用0到1；空白为未约定。其他约定字段保留。请先在操作面板创建研究约定。'));
    if (old.portfolio_constraints && old.portfolio_constraints.account_id !== exact.id) body.append(el('p',{class:'notice'},'现有硬约束属于另一账户。保存会将唯一数值约束明确改绑到本账户，请核对后确认。'));
    const previous = old.portfolio_constraints?.account_id === exact.id ? old.portfolio_constraints : {}, fields = {};
    for (const [key,label] of [['max_single_weight','单股占比上限（比例）'],['max_sector_weight','行业占比上限（比例）'],['minimum_cash','最低现金 CNY'],['minimum_cash_weight','最低现金占比（比例）']]) fields[key] = field(label,previous[key] ?? '');
    const confirm = field('我确认这些是本账户的正式硬约束','','checkbox');
    body.append(button('确认保存约束修订',()=>{
      if (token !== epoch || !confirm.checked) throw Error('请核对账户并明确确认约束。');
      const limits = Object.fromEntries(Object.entries(fields).map(([key,node])=>[key,decimal(node,true)]));
      const candidate = {...structuredClone(old),revision:old.revision+1,portfolio_constraints:{account_id:exact.id,currency:'CNY',...limits}};
      return write('/investor/actions',{idempotency_key:crypto.randomUUID(),parameters:{operation:'mandate_revision',candidate,expected_revision:old.revision}});
    }));
  }
  async function plans(ref = null) {
    const exact = requireAccount(), token = start('同一现金与价格时点下的互斥方案'), view = await get(ref ? `/portfolio/plans/${ref.id}?account_id=${exact.id}` : `/portfolio/plans/current?account_id=${exact.id}`); if (token !== epoch) return;
    if (!view) {body.append(el('p',{},'暂无已发布组合方案。选择快照绑定到下一条消息，并明确选择组合建议。读取页面不会自动研究。')); return;}
    const plan = view.plan; body.dataset.mode = 'plan';
    body.append(el('p',{},`当前状态：${labels[view.status] || view.status}；共同价格基础 ${formatDateTime(plan.price_basis_at)}；组合记录版本 ${plan.snapshot_ref.revision}；有效至 ${formatDateTime(plan.valid_until)}`),el('p',{},'方案互斥，只能选择一个。数量是本次交易量。接受不预留资金；实际成交须单独确认录入。'));
    view.limitation_reasons.forEach(x=>body.append(el('p',{class:'notice'},reason(x))));
    const calculation = await get(`/portfolio/calculations/${plan.calculation_ref.id}?account_id=${exact.id}`); if (token !== epoch) return;
    body.append(el('h4',{},'费用与成交价格假设（不是实际成交事实）'));
    const trusted = calculation.details?.trusted_rules || {};
    Object.values(trusted).forEach(rule=>body.append(el('p',{},`登记的交易规则来源：${rule.trusted_source}；版本：${rule.source_version}。来源与适用时间可在审计中核对。`)));
    audit(body,{trusted_trading_rule_sources:trusted});
    const scenarios = calculation.details?.request?.scenarios || [];
    for (const scenario of scenarios) {
      const group = el('section'); group.append(el('h4',{},scenario.name));
      if (!scenario.actions.length) group.append(el('p',{},'没有交易：没有本次交易费用和成交价格假设。'));
      for (const action of scenario.actions) {
        const label = await name(action.listing_ref); if (token !== epoch) return;
        group.append(el('p',{},`${label} · ${labels[action.direction] || action.direction}；本次交易数量 ${unknown(action.quantity)}${action.budget != null ? `；支出上限 ${unknown(action.budget)} CNY` : ''}`),el('p',{},`假设成交价格 ${unknown(action.execution_price)} CNY；固定费用 ${unknown(action.fixed_fee)} CNY；费率 ${percent(action.fee_rate)}；性质 ${labels[action.nature] || action.nature}；来源 ${action.assumption_source}`));
      } body.append(group);
    }
    body.append(el('p',{},`计算版本 ${calculation.revision}；计算规则 ${calculation.method_version || '见来源审计'}。未知费用与成交价格不能视为免费或确定成交。`)); audit(body,calculation);
    for (const [index,option] of [plan.no_action,...plan.options].entries()) {
      const row = el('fieldset'); row.append(el('legend',{},index === 0 ? '不行动对照' : option.name),el('p',{},`原方案：${option.executable ? '含交易数量测算' : '等待或不行动，不涉及交易数量'}；当前方案状态：${labels[view.status] || view.status}`));
      const result = calculation.details?.scenarios?.[option.scenario_index];
      if (result?.purpose_checks) {
        const check = result.purpose_checks;
        row.append(el('p',{},`方案后现金 ${unknown(check.remaining_cash)} 元；必留现金 ${unknown(check.required_cash)} 元；保留缺口 ${unknown(check.reserve_shortfall)} 元。此处机会成本是可用现金与保留缺口，不假定投资收益。`));
        check.purposes.forEach(p=>row.append(el('p',{},`${p.name}：本方案使用 ${unknown(p.spent)} 元（含费用），用途剩余预算 ${unknown(p.remaining_budget)} 元`)));
      }
      if (result) {
        row.append(el('p',{},`方案后现金 ${unknown(result.cash)} CNY；方案后总资产 ${unknown(result.nav)} CNY；本次交易费用 ${unknown(result.fees)} CNY`));
        for (const holding of result.holdings || []) {const label = await name(holding.listing_ref); if (token !== epoch) return; row.append(el('p',{},`${label} · 方案后持仓数量 ${unknown(holding.quantity)}；持仓市值 ${unknown(holding.market_value)} CNY；单股占比 ${percent(holding.weight)}`));}
        Object.entries(result.sector_weights || {}).forEach(([sector,value])=>row.append(el('p',{},`行业 ${sector} · 行业占比 ${percent(value)}`)));
      }
      [...option.violations,...option.unknowns].forEach(x=>row.append(el('p',{class:'notice'},reason(x))));
      for (const action of option.actions) {const label = await name(action.listing_ref); if (token !== epoch) return; row.append(el('p',{},`${label} · ${labels[action.stance] || action.stance} · ${labels[action.basis] || action.basis}`)); if (action.target_ref) {const quantity = calculation.results?.[action.target_ref.field_path.replace(/^results\./,'')]; row.append(el('p',{},`本次交易数量：${unknown(quantity?.value)} ${quantity?.unit === 'shares' ? '股' : quantity?.unit || ''}（不是最终持仓）`));}
      (option.rendered_actions[action.id] || []).forEach(x=>row.append(el('p',{},x)));}
      const decision = choice('本人的决定',[['accept','接受此选项'],['defer','以后再考虑'],['independent','独立决定']],row), text = field('我的原话（未来愿望不会记入账本）','','text',row);
      if (view.status !== 'published' || (!option.executable && option.actions.some(a=>!['wait','hold','further_research'].includes(a.stance)))) {decision.querySelector('[value=accept]').disabled = true; decision.value = 'defer';}
      const submit = button('确认记录一个选项的决定',()=>{
        if (token !== epoch) throw Error('页面已变化，请重新查看当前方案。');
        if (decision.value === 'accept' && (view.status !== 'published' || (!option.executable && option.actions.some(a=>!['wait','hold','further_research'].includes(a.stance))))) throw Error('当前方案受限，不能接受；请刷新复核。');
        if (!text.value.trim()) throw Error('请填写本人原话。');
        return write('/investor/decisions',{idempotency_key:crypto.randomUUID(),parameters:{operation:'decision',portfolio_plan_ref:{kind:'portfolio_plan',id:plan.id,revision:plan.revision},option_id:option.id,decision:decision.value,user_text:text.value.trim()}});
      }); row.append(submit); audit(row,option); body.append(row);
    }
    audit(body,view);
    if (plan.previous_ref) body.append(button('回看上一份不可变整案',()=>plans(plan.previous_ref)));
    if (view.replacement_ref) body.append(button('查看替代方案',()=>plans(view.replacement_ref)));
  }
  selector.addEventListener('change',()=>run(async()=>{account = accounts.find(x=>x.id === selector.value) || null; snapshot = null; start('账户已切换，请明确绑定下一条消息'); await refresh();}));
  nav.append(button('刷新账户与快照',refresh),button('新建账户',create),button('记录起点',()=>balance('opening_balance')),button('已经成交',trade),button('资金转入/转出',()=>cashFact('cash_transfer')),button('实际分红',()=>cashFact('dividend')),button('对账修正',()=>balance('reconciliation')),button('事实历史/冲销',history),button('账户数值约束',mandate),button('资金用途规划',purposeForm),button('当前方案/复核',()=>plans()),button('绑定此快照到下一条消息',()=>{
    requireAccount(); if (!snapshot) throw Error('请先确认组合起点。');
    root.dispatchEvent(new CustomEvent('agent:portfolio',{detail:{ref:structuredClone(snapshot.reference),label:account.name}}));
    status.textContent = '下一条消息已固定当前快照；请选择组合建议并主动发送。持仓以外证券需在查找结果中明确选择。';
  }));
  root.addEventListener('agent:messages',event=>{
    if (!account || busy || pending) return;
    const refs = event.detail.messages.flatMap(m=>m.artifact_refs || []).filter(r=>['draft','publication_check','review'].includes(r.kind));
    if (refs.length) run(async()=>{
      const id = account.id, token = epoch;
      for (const ref of refs) {
        const key = `${id}:${ref.kind}:${ref.id}:${ref.revision}`; if (artifactReads.has(key)) continue;
        try {const value = await get(`/portfolio/artifacts/${ref.kind}/${ref.id}?account_id=${id}`);
          if (account?.id !== id || token !== epoch) return; artifactReads.add(key);
          const row = el('details'); row.append(el('summary',{},'组合发布检查/受限原因'));
          const check = value.check || value;
          Object.entries(check.field_errors || {}).forEach(([field,message])=>row.append(el('p',{},`${fieldLabel(field)}：${explain(message)}`)));
          (check.failures || []).forEach(message=>row.append(el('p',{},message)));
          audit(row,value); summary.append(row);
        } catch (exc) {if (exc.status !== 404) error(exc);}
      }
    });
  });
  [...nav.children].filter(x=>['新建账户','记录起点','已经成交','资金转入/转出','实际分红','对账修正','账户数值约束','资金用途规划'].includes(x.textContent)).forEach(x=>x.dataset.write = 'true');
  locks(); run(refresh);
}
