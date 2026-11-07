import {get, post, download} from './api.js';
import {el} from './dom.js';
import {randomUUID} from './uuid.js';

const API = '/api/simulations';
const PENDING_KEY = 'zcharlie.simulations.pending.v1';
const LABELS = {draft:'待启动',running:'运行中',paused:'已暂停',completed:'已结束',blocked:'资料阻塞',cancelled:'已取消',waiting_data:'等待公共资料',collecting_decisions:'收集决策',locked:'已锁定',awaiting_execution_data:'待行情确认',settled:'模拟成交已结算',skipped:'本轮跳过',submitted:'已提交',invalid:'校验失败',timeout:'已超时',failed:'执行失败',unknown:'调用结果未知',filled:'已成交',partially_filled:'部分成交',rejected:'已拒绝',expired:'本次未成交',complete:'完整',stale:'估值陈旧',incomplete:'资料不完整',observing:'仍在观察',matured:'观察期已成熟',reserved:'预算预留',cash:'现金基准',equal_weight:'等权基准',agent:'模型参赛者',buy:'买入',sell:'卖出',hold:'主动持有',trade:'提交交易',no_submission:'未提交新订单'};
const GAPS = {execution_evidence_missing:'缺少已核对的当日交易规则证据',raw_market_missing:'缺少未复权日线行情',raw_market_invalid:'行情数值或证券状态无效',raw_market_not_final:'行情取得时间早于收盘，尚不能确认终值',financial_missing:'缺少时点内可用财报',calendar_unreviewed:'交易日历尚未审定',not_trading_day:'该日不是交易日',session_not_closed:'该交易时段尚未结束',rule_unverified:'交易规则尚未核对',corporate_action_coverage_missing:'公司行动覆盖不完整',corporate_action_unsupported:'存在未支持的公司行动',special_session_unsupported:'该特殊交易日尚不支持',security_status_conflict:'证券状态与规则证据冲突',market_missing:'缺少观察日行情',market_status_missing:'证券交易状态缺失',unsettled_order:'经济发生的订单尚待结算',missing_price:'持仓估值价格缺失',unapplied_corporate_actions:'公司行动尚未入账',deadline_passed:'未在截止前提交',budget_exhausted:'调用预算已用尽',model_timeout:'模型调用超时',unknown_call:'模型调用结果不明确',insufficient_cash:'保护价格与费用超过可用现金',insufficient_sellable:'可卖持仓不足',capacity_exceeded:'超过预先声明的成交额容量',price_protection:'开盘价格超过保护条件'};
const label = value => LABELS[value] || String(value ?? '未知');
const explain = value => {
  const text = String(value), parts = text.split(':');
  const code = parts.find(part => GAPS[part]);
  return code ? `${GAPS[code]}${parts.length > 1 ? `（${text}）` : ''}` : `待核对：${text}`;
};
const stamp = value => {
  if (!value) return '未知';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : `${new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(parsed)} 北京时间`;
};

// Presentation shifts digits only. Source money/ratios never pass through Number.
export function decimalText(value, shift = 0, group = true) {
  if (value === null || value === undefined) return '未知';
  const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(String(value));
  if (!match) return '数值格式无法展示';
  const exponent = BigInt(match[4] || '0') + BigInt(shift);
  if (exponent > 200n || exponent < -200n) return '数值超出展示范围';
  const digits = match[2] + (match[3] || '');
  const point = match[2].length + Number(exponent);
  let whole, fraction;
  if (point <= 0) {whole = '0'; fraction = '0'.repeat(-point) + digits;}
  else if (point >= digits.length) {whole = digits + '0'.repeat(point-digits.length); fraction = '';}
  else {whole = digits.slice(0, point); fraction = digits.slice(point);}
  whole = whole.replace(/^0+(?=\d)/,''); fraction = fraction.replace(/0+$/,'');
  const sign = match[1] === '-' && /[1-9]/.test(whole + fraction) ? '-' : '';
  return sign + (group ? whole.replace(/\B(?=(\d{3})+(?!\d))/g,',') : whole) + (fraction ? `.${fraction}` : '');
}
export const percentText = value => value == null ? '不可计算' : `${decimalText(value,2,false)}%`;
const money = value => el('span', {class:'sim-number',title:value == null ? '未知，不能按零处理' : `精确原值：${value}`}, decimalText(value));
const badge = status => el('span',{class:'sim-status','data-tone':['running','settled','complete','matured','filled'].includes(status) ? 'good' : ['blocked','waiting_data','awaiting_execution_data','incomplete','failed','unknown','timeout'].includes(status) ? 'warn' : '',title:status},label(status));
const empty = text => el('p',{class:'sim-empty'},text);
const button = (text,handler,cls='btn btn-sm') => {const node=el('button',{type:'button',class:cls},text);node.addEventListener('click',handler);return node;};
const cell = (value,cls='') => {const node=el('td',{class:cls});node.append(value instanceof Node ? value : document.createTextNode(value == null ? '未知' : String(value)));return node;};
export function table(headers,rows) {
  if (!rows.length) return empty('暂无记录');
  const wrap=el('div',{class:'sim-table-wrap'}), node=el('table',{class:'sim-table'}), head=el('tr');
  headers.forEach(text=>head.append(el('th',{scope:'col'},text)));const thead=el('thead');thead.append(head);node.append(thead);
  const body=el('tbody');rows.forEach(values=>{const row=el('tr');values.forEach(value=>row.append(value instanceof HTMLTableCellElement ? value : cell(value)));body.append(row);});node.append(body);wrap.append(node);return wrap;
}
function gaps(values) {const list=el('ul',{class:'sim-gap-list'});values.forEach(value=>list.append(el('li',{},explain(value))));return list;}
function facts(pairs) {const node=el('dl',{class:'sim-facts'});pairs.forEach(([key,value])=>{const group=el('div');group.append(el('dt',{},key),el('dd',{},value == null ? '未知' : String(value)));node.append(group);});return node;}
function jsonAudit(value) {const node=el('pre',{class:'sim-json'});node.textContent=JSON.stringify(value,null,2);return node;}
function decimal(form,name,optional=false) {const value=form.elements.namedItem(name).value.trim();if (!value && optional) return null;if (!/^\d+(?:\.\d+)?$/.test(value)) throw Error(`${form.elements.namedItem(name).closest('label')?.textContent.trim() || name} 请填写非负十进制数。`);return value;}
function today() {const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());return ['year','month','day'].map(key=>parts.find(p=>p.type===key).value).join('-');}

// A stored recovery request is never authority to write another page's scope.
export function pendingDestination(value) {
  if (!value || typeof value !== 'object' || !value.body || Array.isArray(value.body)
      || typeof value.body !== 'object' || typeof value.body.idempotency_key !== 'string'
      || !/^[A-Za-z0-9._:-]{1,160}$/.test(value.body.idempotency_key)
      || typeof value.path !== 'string') return null;
  if (value.path === API) return {experimentId:null,href:'/app/simulations#sim-create'};
  const match=/^\/api\/simulations\/([^/]+)\/(start|pause|end|run-now|data-refresh|execution-evidence)$/.exec(value.path);
  if (!match) return null;
  let identity;try {identity=decodeURIComponent(match[1]);} catch {return null;}
  if (identity==='.' || identity==='..' || !/^[A-Za-z0-9._:-]{1,160}$/.test(identity)
      || value.path !== `${API}/${encodeURIComponent(identity)}/${match[2]}`) return null;
  return {experimentId:identity,href:`/app/simulations/${encodeURIComponent(identity)}`};
}
export function pendingMatchesView(value,view,experimentId) {
  const target=pendingDestination(value);
  return !!target && (view==='list' ? target.experimentId===null : target.experimentId===experimentId);
}
export function availableActions(experiment,orders=[],day=today()) {
  const status=experiment.status,unfinished=orders.some(order=>order.status==='locked');
  return {start:['draft','paused'].includes(status) && experiment.config.end_date>=day,
    pause:['running','blocked'].includes(status),end:['draft','running','paused','blocked'].includes(status),
    'run-now':status==='running',
    'data-refresh':['draft','running','paused'].includes(status)||(status==='completed'&&unfinished)};
}

export function bootSimulations(root = document.querySelector('#simulations-workbench')) {
  if (!root) return;
  const message=document.querySelector('#sim-message'), pendingHost=document.querySelector('#sim-pending');
  let busy=false,pending=null,refresh=null;
  try {pending=JSON.parse(sessionStorage.getItem(PENDING_KEY)||'null');} catch { /* unavailable storage is handled before writes */ }
  if (pending && !pendingDestination(pending)) pending=null;
  const pendingHere=()=>pendingMatchesView(pending,root.dataset.view,root.dataset.experimentId);
  function clearStoredPending() {try {sessionStorage.removeItem(PENDING_KEY);} catch {notify('服务器已确认结果，但本地请求标记未清除。刷新后仍可按原幂等键核对。',true);}}
  function notify(text,error=false) {message.textContent=text;message.dataset.error=String(error);}
  function report(error) {
    const detail=error.detail;
    const text=Array.isArray(detail) ? detail.map(item=>`${(item.loc||[]).filter(x=>x!=='body').join(' / ')}：${item.msg}`).join('；') : detail?.message || error.message || '操作未完成';
    notify(`${text}${detail?.code ? `（${detail.code}）` : ''}${error.status===401 ? ' 请重新登录。' : ''}`,true);
  }
  const run=task=>Promise.resolve().then(task).catch(report);
  function syncPending() {
    pendingHost.hidden=!pending;
    document.querySelector('#sim-retry-write').disabled=busy || !pendingHere();
    root.querySelectorAll('[data-sim-action],#sim-create-submit,#sim-evidence-form button[type=submit],#sim-corporate-form button[type=submit]').forEach(node=>node.disabled=busy || !!pending || node.dataset.allowed==='false');
    if (pending) {
      const text=document.querySelector('#sim-pending-text');text.replaceChildren();
      text.append(document.createTextNode(`${pending.description || '原请求'}的服务器结果尚未确认。`));
      if (pendingHere()) text.append(document.createTextNode('重试将发送同一正文与幂等键；不会另建一份请求。'));
      else {text.append(document.createTextNode('此请求属于另一个页面，请返回原页面核对，当前页面不会重发。 '));
        text.append(el('a',{href:pendingDestination(pending).href},'返回原请求页面'));}
    }
  }
  async function transmit() {
    if (!pending || busy || !pendingHere()) return;
    const fixed=pending;busy=true;syncPending();notify(`正在${fixed.description}…`);
    try {
      const result=await post(fixed.path,fixed.body,{idempotency:false});
      pending=null;clearStoredPending();notify(`${fixed.description}已由服务器确认。`);
      if (fixed.path===API && result?.id) {location.assign(`/app/simulations/${encodeURIComponent(result.id)}`);return;}
      if (refresh) await refresh();
    } catch(error) {
      // A rejected revision/body is a confirmed failure; network/5xx remains replayable.
      if ([400,403,404,409,422].includes(error.status)) {pending=null;clearStoredPending();}
      report(error);
      if (error.status===409 && refresh) {await refresh();report(error);}
    } finally {busy=false;syncPending();}
  }
  async function write(path,body,description) {
    if (pending || busy) throw Error('请先重试尚未确认的原请求。');
    pending={path,body:{...structuredClone(body),idempotency_key:randomUUID()},description};
    try {sessionStorage.setItem(PENDING_KEY,JSON.stringify(pending));} catch {pending=null;throw Error('浏览器存储不可用，尚未发送操作。');}
    await transmit();
  }
  document.querySelector('#sim-retry-write').addEventListener('click',()=>run(transmit));
  document.querySelector('#sim-refresh').addEventListener('click',()=>run(()=>refresh()));
  syncPending();

  if (root.dataset.view==='list') {
    let options=null,searchEpoch=0,agentIndex=0;
    const selected=new Map(),rows=document.querySelector('#sim-agent-rows'),form=document.querySelector('#sim-create-form');
    const start=form.elements.namedItem('start_date'),end=form.elements.namedItem('end_date');start.value=today();const endDate=new Date(`${start.value}T12:00:00+08:00`);endDate.setUTCDate(endDate.getUTCDate()+84);end.value=endDate.toISOString().slice(0,10);
    const createButton=document.querySelector('#sim-create-submit');createButton.dataset.allowed='false';
    document.querySelector('#sim-create-open').addEventListener('click',()=>{document.querySelector('#sim-create').open=true;});
    function selection() {
      const host=document.querySelector('#sim-selected-stocks');host.replaceChildren();
      selected.forEach(stock=>{const chip=el('span',{class:'sim-chip'});chip.append(document.createTextNode(`${stock.name} · ${stock.ticker}`),button('×',()=>{selected.delete(stock.listing_id);selection();},''));chip.lastChild.setAttribute('aria-label',`移除 ${stock.name}`);host.append(chip);});
      document.querySelector('#sim-stock-count').textContent=selected.size ? `已选择 ${selected.size} 只股票` : '尚未选择股票';
    }
    async function search() {
      const epoch=++searchEpoch,query=document.querySelector('#sim-stock-query').value.trim(),host=document.querySelector('#sim-stock-results');host.replaceChildren(el('p',{class:'small muted'},'正在搜索本地证券…'));
      try {const stocks=await get(`${API}/options/stocks?q=${encodeURIComponent(query)}`);if (epoch!==searchEpoch) return;host.replaceChildren();if (!stocks.length) host.append(empty('未找到本地证券。请核对代码，或先在研究台保存证券。'));
        stocks.forEach(stock=>{const choice=button('',()=>{if (selected.size>=100 && !selected.has(stock.listing_id)) {notify('每个实验最多选择 100 只股票。',true);return;}selected.set(stock.listing_id,stock);selection();},'sim-search-result');choice.append(el('span',{},stock.name),el('small',{},`${stock.ticker} · 选择`));host.append(choice);});
      } catch(error) {if (epoch===searchEpoch) {host.replaceChildren(empty('股票列表读取失败，请重新搜索。'));report(error);}}
    }
    document.querySelector('#sim-stock-search').addEventListener('click',()=>run(search));
    document.querySelector('#sim-stock-query').addEventListener('keydown',event=>{if (event.key==='Enter') {event.preventDefault();run(search);}});
    function agentRow() {
      if (rows.children.length>=8) {notify('最多配置 8 名模型参赛者。',true);return;}
      const index=++agentIndex,row=el('section',{class:'sim-agent-row'}),head=el('div',{class:'sim-agent-row-head'});
      head.append(el('h3',{},`参赛者 ${index}`),button('移除',()=>{if(rows.children.length===1) {notify('请至少保留一名模型参赛者。',true);return;}row.remove();}));row.append(head);
      const fields=el('div',{class:'sim-fields'});row.append(fields);
      function input(labelText,name,value,type='text',parent=fields) {const wrap=el('label',{},labelText),node=el('input',{name,value,type,required:'required',autocomplete:'off'});if(type==='text')node.inputMode='decimal';wrap.append(node);parent.append(wrap);return node;}
      input('参赛名称','agent_name',`参赛者 ${index}`).inputMode='text';
      const modelWrap=el('label',{},'模型'),models=el('select',{name:'model_index',required:'required'});models.append(el('option',{value:''},'请选择已配置模型'));
      (options?.models||[]).forEach((model,i)=>models.append(el('option',{value:String(i),disabled:model.available?null:'disabled'},`${model.name}${model.available?'':' · 未配置'}`)));
      const available=(options?.models||[]).map((model,i)=>model.available?String(i):null).filter(value=>value!==null);if (available.length) models.value=available[(index-1)%available.length];modelWrap.append(models);fields.append(modelWrap);
      input('每轮金额上限（USD）','round_cost_limit','3');input('赛季金额上限（USD）','season_cost_limit','100');
      const advanced=el('details'),summary=el('summary',{},'调用次数、时限与价格'),more=el('div',{class:'sim-fields'});advanced.append(summary,more);row.append(advanced);
      input('单次保守预留（USD）','conservative_call_reservation','1','text',more);const calls=input('每轮最多调用次数','max_calls_per_round','8','number',more);calls.min='1';calls.max='100';
      const timeout=input('单次时限（秒）','timeout_seconds','300','number',more);timeout.min='1';timeout.max='3600';
      const repairs=input('格式修复次数','format_repair_attempts','1','number',more);repairs.min='0';repairs.max='5';
      ['input','output'].forEach(kind=>{const node=input(`${kind==='input'?'输入':'输出'}每百万 token 价格（USD，可空）`,`${kind}_price_per_million`,'','text',more);node.required=false;});rows.append(row);
    }
    document.querySelector('#sim-agent-add').addEventListener('click',agentRow);
    refresh=async()=>{const values=await get(API),host=document.querySelector('#sim-list');host.replaceChildren();if (!values.length) {const card=el('section',{class:'card sim-empty'});card.append(el('h2',{},'从一个共同起点开始'),el('p',{},'选择股票和参赛模型，现金与等权基准会自动加入。'));host.append(card);}
      values.forEach(value=>{const card=el('article',{class:'card sim-experiment'});card.append(el('h2',{},value.config.name),badge(value.status),el('p',{class:'small muted'},`${value.config.start_date} — ${value.config.end_date} · ${value.config.rebalance_frequency==='daily'?'每日':'每周'}调仓`),el('p',{},`${value.config.universe.length} 只股票 · ${value.config.participants.filter(p=>p.kind==='agent').length} 名模型 · 每账户 ${decimalText(value.config.initial_cash)} 元`));const foot=el('div',{class:'sim-experiment-foot'});foot.append(el('span',{class:'small muted'},`配置修订 ${value.revision}`),el('a',{class:'btn btn-sm',href:`/app/simulations/${encodeURIComponent(value.id)}`},'查看实验'));card.append(foot);host.append(card);});};
    form.addEventListener('submit',event=>{event.preventDefault();run(async()=>{
      if(!selected.size) throw Error('请至少搜索并选择一只股票。');
      if(!rows.children.length) throw Error('请至少配置一名模型参赛者。');
      const agents=[...rows.children].map(row=>{const values=Object.fromEntries([...row.querySelectorAll('input,select')].map(input=>[input.name,input.value.trim()])),model=options.models[Number(values.model_index)];if(!model?.available || values.model_index==='')throw Error('请为每名参赛者选择已配置模型。');const budget={currency:'USD',round_cost_limit:values.round_cost_limit,season_cost_limit:values.season_cost_limit,conservative_call_reservation:values.conservative_call_reservation,max_calls_per_round:Number(values.max_calls_per_round),timeout_seconds:Number(values.timeout_seconds),format_repair_attempts:Number(values.format_repair_attempts)};if(values.input_price_per_million || values.output_price_per_million){if(!values.input_price_per_million || !values.output_price_per_million)throw Error('输入和输出价格须成对填写。');budget.input_price_per_million=values.input_price_per_million;budget.output_price_per_million=values.output_price_per_million;}return {name:values.agent_name,provider_ref:model.provider_ref,model_ref:model.model_ref,budget};});
      await write(API,{name:form.elements.namedItem('name').value.trim(),universe:[...selected.keys()],start_date:start.value,end_date:end.value,initial_cash:decimal(form,'initial_cash'),rebalance_frequency:form.elements.namedItem('rebalance_frequency').value,mode:form.elements.namedItem('mode').value,agents,fees:Object.fromEntries(['commission_rate','minimum_commission','sell_tax_rate','transfer_rate'].map(key=>[key,decimal(form,key)])),...Object.fromEntries(['slippage_rate','max_position_weight','max_historical_turnover_ratio'].map(key=>[key,decimal(form,key)]))},'创建实验');
    });});
    const loadOptions=async()=>{options=await get(`${API}/options`);const status=document.querySelector('#sim-options-status'),available=options.models.filter(model=>model.available);const maxDate=options.calendar.max_observation_date; if(maxDate){start.max=maxDate;end.max=maxDate;if(end.value>maxDate)end.value=maxDate;} status.textContent=`交易日历：${options.calendar.available ? options.calendar.id : '不可用'}（已核验年份 ${(options.calendar.reviewed_years||[]).join('、')||'无'}；最晚观察日 ${maxDate||'未知'}）；${available.length} 个模型可用。${available.length?'启动后将按设置预算自动运行。':'请先在设置中配置模型供应商与凭证。'}`;createButton.dataset.allowed=String(options.calendar.available && available.length>0 && !!maxDate && today()<=maxDate);if(!maxDate || today()>maxDate)status.textContent+=' 当前日期已超出安全观察范围，请先到数据健康页更新已核验日历。';if(!rows.children.length)agentRow();syncPending();if(location.hash==='#sim-create')document.querySelector('#sim-create').open=true;};const refreshList=refresh;refresh=async()=>{await refreshList();if(!options)await loadOptions();};run(refresh);
    return;
  }

  const experimentID=root.dataset.experimentId,base=`${API}/${encodeURIComponent(experimentID)}`;
  let state=null,epoch=0,availabilityEpoch=0,stockNames=new Map(),activeTab='overview';
  const participantFilter=document.querySelector('#sim-participant-filter'),roundFilter=document.querySelector('#sim-round-filter');
  const participantName=id=>state?.detail.participants.find(p=>p.id===id)?.config.name || id;
  const stockName=id=>stockNames.get(id) || `证券 #${id}`;
  const filtered=values=>(values||[]).filter(value=>(!participantFilter.value || value.participant_id===participantFilter.value) && (!roundFilter.value || !value.round_id || value.round_id===roundFilter.value));
  const roundDate=id=>state?.detail.rounds.find(round=>round.id===id)?.observation_date || '未知轮次';
  const holder=(id,records,render)=>{const host=document.querySelector(id);host.replaceChildren();if(state?.failures[records])host.append(empty('此部分读取失败，请刷新重试。'));else host.append(render());};
  function tab(name,focus=false) {activeTab=name;document.querySelectorAll('[data-sim-tab]').forEach(node=>{const on=node.dataset.simTab===name;node.setAttribute('aria-selected',String(on));node.tabIndex=on?0:-1;if(on && focus)node.focus();});document.querySelectorAll('.sim-panel').forEach(node=>node.hidden=node.id!==`sim-panel-${name}`);}
  document.querySelectorAll('[data-sim-tab]').forEach(node=>{node.addEventListener('click',()=>tab(node.dataset.simTab));node.addEventListener('keydown',event=>{const tabs=[...document.querySelectorAll('[data-sim-tab]')],index=tabs.indexOf(node);let next;if(event.key==='ArrowRight')next=(index+1)%tabs.length;else if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=tabs.length-1;if(next!==undefined){event.preventDefault();tab(tabs[next].dataset.simTab,true);}});});
  function viewDecision(identity) {const decision=state.decisions.find(d=>d.id===identity);if(!decision){notify('原始决策尚未读取，请刷新。',true);return;}participantFilter.value=decision.participant_id;roundFilter.value=decision.round_id;render();tab('decisions');const node=document.getElementById(`sim-decision-${identity}`);if(node){node.open=true;node.scrollIntoView({block:'center',behavior:'smooth'});}}
  function viewEvidence(identity) {tab('data');const round=state.detail.rounds.find(r=>r.manifest.evidence.some(ref=>ref.id===identity));if(round){roundFilter.value=round.id;renderRounds();const node=document.getElementById(`sim-round-${round.id}`);node.open=true;node.scrollIntoView({block:'start',behavior:'smooth'});}else notify(`证据引用：${identity}。当前轮次资料中未找到该引用。`,true);}
  function fillSelect(node,options,blank) {const value=node.value;node.replaceChildren(el('option',{value:''},blank));options.forEach(([id,name])=>node.append(el('option',{value:id},name)));node.value=options.some(([id])=>id===value)?value:'';}
  function latestNavs() {const latest=new Map();filtered(state.nav).forEach(nav=>{const key=`${nav.participant_id}:${nav.valuation_date}`,prior=latest.get(key);if(!prior || nav.version>prior.version)latest.set(key,nav);});return [...latest.values()].sort((a,b)=>a.valuation_date.localeCompare(b.valuation_date)||a.participant_id.localeCompare(b.participant_id));}
  function controls() {
    if(!state)return;const experiment=state.detail.experiment,status=experiment.status;document.querySelector('#sim-title').textContent=experiment.config.name;
    document.querySelector('#sim-subtitle').textContent=`${experiment.config.start_date} — ${experiment.config.end_date} · ${experiment.config.universe.length} 只股票 · ${experiment.config.rebalance_frequency==='daily'?'每日':'每周'}调仓 · 配置修订 ${experiment.revision}`;
    document.querySelector('#sim-state').replaceChildren(badge(status));
    const allowed=availableActions(experiment,state.orders);
    root.querySelectorAll('[data-sim-action]').forEach(node=>{node.dataset.allowed=String(allowed[node.dataset.simAction]);if(node.dataset.simAction==='start')node.textContent=status==='paused'?'恢复运行':'启动实验';});
    document.querySelector('#sim-runtime-hint').textContent=status==='paused'?'新决策已停止。暂停前已经发生的交易时段仍需结算；已有持仓继续估值。':status==='completed'?'实验已结束；保留原持仓和历史记录，经济发生过的订单仍须确认结算。':'启动后在冻结预算内自动运行。每轮先确认公共资料，再决策；日线成交在资料到齐后结算。';syncPending();
  }
  root.querySelectorAll('[data-sim-action]').forEach(node=>node.addEventListener('click',()=>run(async()=>{if(!state)throw Error('请先读取实验状态。');const action=node.dataset.simAction;await write(`${base}/${action}`,{expected_revision:state.detail.experiment.revision},({'start':'启动或恢复实验','pause':'暂停实验','end':'结束实验','run-now':'推进运行','data-refresh':'采集实验数据'})[action]);})));
  document.querySelector('#sim-export').addEventListener('click',()=>run(async()=>{await download(`${base}/export`);notify('已导出冻结资料与模拟账本。');}));
  participantFilter.addEventListener('change',()=>render());roundFilter.addEventListener('change',()=>render());

  function chart(navs) {
    const host=document.querySelector('#sim-nav-chart'),legend=document.querySelector('#sim-chart-legend');host.replaceChildren();legend.replaceChildren();
    const participants=state.detail.participants.filter(p=>!participantFilter.value || p.id===participantFilter.value),dates=[...new Set(navs.map(n=>n.valuation_date))].sort();
    const known=navs.filter(n=>n.quality==='complete' && n.equity!=null),numbers=known.map(n=>Number(n.equity)).filter(Number.isFinite);
    if(!numbers.length || !dates.length){host.append(empty('尚无完整净值，等待账户与行情确认。'));return;}
    const NS='http://www.w3.org/2000/svg',svg=document.createElementNS(NS,'svg');svg.setAttribute('viewBox','0 0 760 250');svg.setAttribute('role','img');svg.setAttribute('aria-label','各独立账户的净值曲线；精确值见每日净值表，资料不完整处不连线');
    const low=Math.min(...numbers),high=Math.max(...numbers),span=high-low || Math.max(Math.abs(high)*0.02,1),min=low-span*0.1,max=high+span*0.1;
    function lineNode(tag,attrs,text){const node=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,String(value)));if(text!==undefined)node.textContent=text;svg.append(node);return node;}
    const x=day=>70+(dates.length===1?0.5:dates.indexOf(day)/(dates.length-1))*660,y=value=>210-(value-min)/(max-min)*180;
    [0,0.5,1].forEach(fraction=>{const py=30+fraction*180;lineNode('line',{x1:70,x2:730,y1:py,y2:py,class:'sim-chart-grid'});lineNode('text',{x:62,y:py+4,'text-anchor':'end',class:'sim-chart-label'},new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(max-fraction*(max-min)));});
    lineNode('text',{x:70,y:238,class:'sim-chart-label'},dates[0]);if(dates.length>1)lineNode('text',{x:730,y:238,'text-anchor':'end',class:'sim-chart-label'},dates.at(-1));
    participants.forEach(p=>{const color=state.detail.participants.indexOf(p)%10;let segment=[],priorIndex=-2;const segments=[];navs.filter(n=>n.participant_id===p.id).forEach(nav=>{const index=dates.indexOf(nav.valuation_date);if(nav.quality!=='complete' || nav.equity==null || !Number.isFinite(Number(nav.equity)) || index!==priorIndex+1){if(segment.length)segments.push(segment);segment=[];}if(nav.quality==='complete' && nav.equity!=null && Number.isFinite(Number(nav.equity)))segment.push([x(nav.valuation_date),y(Number(nav.equity))]);priorIndex=index;});if(segment.length)segments.push(segment);segments.forEach(points=>{lineNode('path',{d:points.map(([px,py],i)=>`${i?'L':'M'}${px.toFixed(2)},${py.toFixed(2)}`).join(' '),class:`sim-chart-line sim-color-${color}`});points.forEach(([cx,cy])=>lineNode('circle',{cx,cy,r:2,class:`sim-chart-line sim-color-${color}`}));});legend.append(el('span',{class:`sim-color-${color}`},p.config.name));});host.append(svg);
  }
  function renderOverview() {
    const board=state.detail.leaderboard,kpis=document.querySelector('#sim-kpis'),latestRound=state.detail.rounds.at(-1);kpis.replaceChildren();
    [['起始资金 / 每账户',`${decimalText(state.detail.experiment.config.initial_cash)} 元`,'各账户独立记账'],['共同有效观察日',`${board.common_observations ?? 0} 日`,board.valuation_date?`同窗截至 ${board.valuation_date}`:'尚无共同估值日'],['最近轮次',latestRound?label(latestRound.status):'尚未准备',latestRound?`资料截止 ${stamp(latestRound.cutoff)}`:'公共资料齐备后开始']].forEach(([name,value,note])=>{const card=el('div',{class:'sim-kpi'});card.append(el('small',{},name),el('b',{},value),el('span',{class:'small muted'},note));kpis.append(card);});
    document.querySelector('#sim-ranking-window').textContent=`${board.valuation_date?`共同观察窗口截至 ${board.valuation_date}`:'尚无共同观察窗口'}；正式名次仅采用完整资料与足够共同样本。收益由服务端账户净值计算。`;
    document.querySelector('#sim-leaderboard').replaceChildren(table(['名次','参赛者','累计净收益','最大回撤','数据状态'],(board.rows||[]).filter(row=>!participantFilter.value||row.participant_id===participantFilter.value).map(row=>[row.official&&row.rank!=null?String(row.rank):'未进入正式排名',row.name,cell(percentText(row.cumulative_return),'sim-number'),cell(percentText(row.max_drawdown),'sim-number'),badge(row.quality)])));
    const navs=latestNavs();if(state.failures.nav)document.querySelector('#sim-nav-chart').replaceChildren(empty('净值读取失败，请刷新。'));else chart(navs);
    holder('#sim-nav-table','nav',()=>table(['日期','账户','现金','持仓价值','应收 / 应付','权益','质量','版本'],navs.map(n=>[n.valuation_date,participantName(n.participant_id),cell(money(n.cash),'sim-number'),cell(money(n.position_value),'sim-number'),`${decimalText(n.receivables)} / ${decimalText(n.payables)}`,cell(money(n.equity),'sim-number'),n.quality_reasons?.length?`${label(n.quality)} · ${n.quality_reasons.map(explain).join('；')}`:badge(n.quality),String(n.version)])));
  }
  function renderAccounts() {
    const host=document.querySelector('#sim-accounts');host.replaceChildren();
    state.detail.participants.filter(p=>!participantFilter.value||p.id===participantFilter.value).forEach(p=>{const account=state.detail.accounts.find(a=>a.participant_id===p.id),card=el('article',{class:'card'}),head=el('div',{class:'sim-account-head'});head.append(el('h2',{},p.config.name),badge(p.config.kind));card.append(head);if(!account){card.append(empty('账户状态不可用'));host.append(card);return;}
      card.append(facts([['现金（元）',decimalText(account.state.cash)],['冻结现金（元）',decimalText(account.state.reserved_cash)],['应收权益（元）',decimalText(account.state.receivables)],['应付（元）',decimalText(account.state.payables)],['账户修订',account.revision],['参赛版本',p.version]]));
      if(p.config.kind==='agent')card.append(el('p',{class:'small muted'},`${p.config.provider_ref} / ${p.config.model_ref} · 每轮 ${decimalText(p.config.budget.round_cost_limit)} USD · 赛季 ${decimalText(p.config.budget.season_cost_limit)} USD`));
      card.append(el('h3',{},'持仓批次'),table(['证券','股数','单位成本','取得日','可卖日'],account.state.lots.map(lot=>[stockName(lot.listing_id),cell(money(lot.quantity),'sim-number'),cell(money(lot.unit_cost),'sim-number'),lot.acquired_on,lot.available_on])));host.append(card);
    });
  }
  function renderDecisions() {
    holder('#sim-decisions','decisions',()=>{const host=el('div',{class:'sim-record-list'}),values=filtered(state.decisions).sort((a,b)=>roundDate(b.round_id).localeCompare(roundDate(a.round_id))||b.revision-a.revision);if(!values.length)return empty('尚无决策。未及时提交的轮次会保留明确失败状态。');values.forEach(d=>{const card=el('details',{class:'sim-record',id:`sim-decision-${d.id}`}),summary=el('summary');summary.append(el('span',{},participantName(d.participant_id)),badge(d.status),el('span',{},label(d.action)),el('span',{class:'muted'},`${roundDate(d.round_id)} · 修订 ${d.revision}`));card.append(summary,el('h3',{},'当时理由'),el('p',{},d.rationale));if(d.failure_code)card.append(gaps([d.failure_code]));if(d.orders.length)card.append(table(['证券','方向','固定股数','保护价'],d.orders.map(order=>[stockName(order.listing_id),label(order.side),cell(money(order.quantity),'sim-number'),cell(money(order.protection_price),'sim-number')])));
      card.append(el('h3',{},'反对证据'),el('p',{},d.counter_evidence.length?d.counter_evidence.join('\n'):'未列出反对证据'),el('p',{class:'small muted'},`预期持有期限：${d.expected_holding_days??'未声明'} 天；复核条件：${d.review_conditions.join('；')||'未声明'}`),el('p',{class:'small muted'},`提交时间：${stamp(d.submitted_at)}；绑定账户修订 ${d.account_revision}`));if(d.evidence_refs.length){const refs=el('div',{class:'sim-chips'});d.evidence_refs.forEach(ref=>refs.append(button(ref,()=>viewEvidence(ref),'link-button sim-reference')));card.append(el('h3',{},'公共资料引用'),refs);}host.append(card);});return host;});
  }
  function renderExecution() {
    holder('#sim-orders','orders',()=>table(['参赛者 / 轮次','证券 / 方向','固定股数','保护价','冻结现金 / 股数','执行日','状态'],filtered(state.orders).map(o=>[`${participantName(o.participant_id)} / ${roundDate(o.round_id)}`,`${stockName(o.listing_id)} · ${label(o.side)}`,cell(money(o.quantity),'sim-number'),cell(money(o.protection_price),'sim-number'),`${decimalText(o.reserved_cash)} / ${decimalText(o.reserved_quantity)}`,o.execution_date,o.rejection_reason?`${label(o.status)} · ${explain(o.rejection_reason)}`:badge(o.status)])));
    holder('#sim-fills','fills',()=>table(['参赛者','证券 / 方向','数量','成交价','费用','经济发生时间','系统确认时间','行情引用'],filtered(state.fills).map(f=>[participantName(f.participant_id),`${stockName(f.listing_id)} · ${label(f.side)}`,cell(money(f.quantity),'sim-number'),cell(money(f.price),'sim-number'),cell(money(f.fee),'sim-number'),stamp(f.economic_at),stamp(f.confirmed_at),cell(f.market_data_ref,'sim-reference')])));
  }
  function renderReviews() {
    holder('#sim-reviews','reviews',()=>{const host=el('div',{class:'sim-record-list'}),reviews=filtered(state.reviews).filter(r=>!roundFilter.value||state.decisions.find(d=>d.id===r.decision_id)?.round_id===roundFilter.value);if(!reviews.length)return empty('尚无复盘；观察窗口成熟、结果齐备后生成。');reviews.forEach(r=>{const card=el('article',{class:'sim-record'});card.append(el('h3',{},`${participantName(r.participant_id)} · 版本 ${r.version}`),badge(r.status),el('p',{class:'small muted'},`${r.observation_start} — ${r.observation_end}；可知时间 ${stamp(r.available_at)}`),el('p',{},r.findings),el('p',{class:'small muted'},r.memory_eligible?'已满足独立复盘记忆条件':'本记录尚未进入学习记忆'));if(r.improvement_proposals.length)card.append(el('h3',{},'待验证改进提案'),el('p',{},r.improvement_proposals.join('\n')));card.append(button('查看原始决策',()=>viewDecision(r.decision_id)));if(r.nav_refs.length)card.append(el('p',{class:'sim-reference'},`净值引用：${r.nav_refs.join('、')}`));host.append(card);});return host;});
    holder('#sim-costs','costs',()=>table(['参赛者','调用状态','金额','计费依据','模型','调用时间'],filtered(state.costs?.records).map(c=>[participantName(c.participant_id),badge(c.status),cell(`${decimalText(c.amount)} ${c.currency}`,'sim-number'),c.status==='unknown'?'响应不确定，存在成本风险':c.status==='reserved'?'保守预留，尚未确认为费用':c.status==='failed'?'调用失败；按记录保留成本风险':c.estimated?'按保守规则估算，非供应商账单':'按已配置单价计算',c.returned_model||c.model_ref,stamp(c.occurred_at)])));
  }
  function renderRounds() {
    const host=document.querySelector('#sim-rounds');host.replaceChildren();const rounds=state.detail.rounds.filter(r=>!roundFilter.value||r.id===roundFilter.value);if(!rounds.length)host.append(empty('尚无正式轮次。启动并推进运行后，系统会先检查共同资料。'));
    rounds.forEach(r=>{const card=el('details',{class:'sim-record',id:`sim-round-${r.id}`}),summary=el('summary');summary.append(el('span',{},`${r.observation_date} → ${r.execution_date}`),badge(r.status),el('span',{class:'muted'},r.manifest.complete?'公共资料完整':'公共资料缺失'));card.append(summary,facts([['共同截止',stamp(r.cutoff)],['决策截止',stamp(r.decision_deadline)],['经济执行时点',stamp(r.execution_at)],['资料版本',r.manifest.version]]));const missing=[...new Set([...(r.manifest.missing_reasons||[]),...(state.detail.data_gaps?.[r.id]||[]),...(state.availability?.rounds?.find(v=>v.round_id===r.id)?.missing||[])])];if(missing.length)card.append(gaps(missing));card.append(el('p',{class:'sim-reference'},`manifest SHA-256：${r.manifest.content_hash}`));const evidence=el('ol',{class:'sim-evidence-list'});r.manifest.evidence.forEach(ref=>{const item=el('li');item.append(el('b',{class:'sim-reference'},ref.id),el('div',{class:'small muted'},`${ref.source} · 公开 ${stamp(ref.published_at)} · 取得 ${stamp(ref.acquired_at)} · 可用 ${stamp(ref.available_at)}`),el('div',{class:'sim-reference'},`SHA-256：${ref.content_hash}`));evidence.append(item);});card.append(evidence);host.append(card);});
  }
  function renderAvailability() {
    const host=document.querySelector('#sim-availability');host.replaceChildren();const value=state.availability;if(!value){host.append(empty('尚未读取当日资料状态，请检查资料。'));return;}
    host.append(el('p',{},`${value.trade_date} · ${value.ready?'该日资料已就绪':'该日资料仍有缺口'}`));if(value.missing?.length)host.append(gaps(value.missing));host.append(table(['证券','当日数据门槛','缺失原因'],(value.securities||[]).map(s=>[stockName(s.listing_id),s.ready?'已就绪':'尚未就绪',s.missing.length?s.missing.map(explain).join('；'):'行情、财报和规则证据均已具备'])));
    if(value.rounds?.some(r=>r.missing.length)){host.append(el('h3',{},'轮次冻结或结算缺口'));value.rounds.filter(r=>r.missing.length).forEach(r=>{host.append(el('p',{class:'small'},`${roundDate(r.round_id)} · ${label(r.status)}`),gaps(r.missing));});}
  }
  async function availability(day) {const version=++availabilityEpoch;try{const value=await get(`${base}/availability?trade_date=${encodeURIComponent(day)}`);if(version!==availabilityEpoch||!state)return;state.availability=value;renderAvailability();renderRounds();}catch(error){if(version===availabilityEpoch){document.querySelector('#sim-availability').replaceChildren(empty('资料诊断读取失败，请重试。'));report(error);}}}
  document.querySelector('#sim-availability-form').addEventListener('submit',event=>{event.preventDefault();const day=event.currentTarget.elements.namedItem('trade_date').value;run(()=>availability(day));});
  function render() {if(!state)return;controls();renderOverview();renderAccounts();renderDecisions();renderExecution();renderReviews();renderRounds();renderAvailability();document.querySelector('#sim-config').replaceChildren(jsonAudit(state.detail.experiment.config));tab(activeTab);}

  refresh=async()=>{
    const version=++epoch;const detail=await get(base);if(version!==epoch)return;
    const names=['decisions','orders','fills','nav','reviews','costs'],results=await Promise.allSettled(names.map(name=>get(`${base}/${name}`)));if(version!==epoch)return;
    const next={detail,failures:{},availability:state?.availability||null};results.forEach((result,i)=>{next[names[i]]=result.status==='fulfilled'?result.value:names[i]==='costs'?{records:[]}:[];if(result.status==='rejected')next.failures[names[i]]=true;});state=next;
    fillSelect(participantFilter,detail.participants.map(p=>[p.id,`${p.config.name} · ${label(p.config.kind)}`]),'全部参赛者与基准');fillSelect(roundFilter,detail.rounds.map(r=>[r.id,`${r.observation_date} · ${label(r.status)}`]),'全部轮次');
    const evidence=document.querySelector('#sim-evidence-form');fillSelect(evidence.elements.namedItem('listing_id'),detail.experiment.config.universe.map(id=>[String(id),stockName(id)]),'请选择证券');evidence.elements.namedItem('rule_version').value=detail.experiment.config.rules.version;
    const diagnostic=document.querySelector('#sim-availability-form').elements.namedItem('trade_date');if(!diagnostic.value)diagnostic.value=detail.rounds.at(-1)?.execution_date||today();if(!evidence.elements.namedItem('trade_date').value)evidence.elements.namedItem('trade_date').value=diagnostic.value;
    render();if(Object.keys(next.failures).length)notify('部分记录读取失败，相关区域已标明。请刷新重试。',true);await availability(diagnostic.value);
  };
  const evidenceForm=document.querySelector('#sim-evidence-form');
  evidenceForm.addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=name=>evidenceForm.elements.namedItem(name).value.trim(),checked=name=>evidenceForm.elements.namedItem(name).checked;const evidence={listing_id:Number(value('listing_id')),trade_date:value('trade_date'),exchange:value('exchange'),board:'mainboard',security_type:'ordinary_a',ipo_date:value('ipo_date'),ordinary_limit_session:checked('ordinary_limit_session'),reference_price:decimal(evidenceForm,'reference_price'),upper_limit:decimal(evidenceForm,'upper_limit'),lower_limit:decimal(evidenceForm,'lower_limit'),tick_size:decimal(evidenceForm,'tick_size'),lot_size:Number(value('lot_size')),is_st:checked('is_st'),rule_version:value('rule_version'),rule_source:value('rule_source'),rule_verified:checked('rule_verified'),corporate_actions_complete:checked('corporate_actions_complete'),corporate_action_refs:value('corporate_action_refs').split(/\r?\n/).map(v=>v.trim()).filter(Boolean),unsupported_corporate_actions:[],published_at:`${value('published_at')}:00+08:00`,captured_at:`${value('captured_at')}:00+08:00`};await write(`${base}/execution-evidence`,{evidence,source_ref:value('source_ref')},'导入交易证据');});});
  run(async()=>{try{const stocks=await get(`${API}/options/stocks?limit=100`);stockNames=new Map(stocks.map(stock=>[stock.listing_id,`${stock.name} · ${stock.ticker}`]));}catch{/* IDs remain visible when the local naming query is unavailable. */}await refresh();});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && !busy && !pending)run(()=>refresh());});
}
if (typeof document!=='undefined') bootSimulations();
