// AG18 read projections. All material uses textContent; numbers are resolved by the server.
import {get, patch, post} from './api.js';
import {el} from './dom.js';
// 时间一律按北京时间（format.js 统一实现），不再原样渲染 UTC ISO 串。
import {formatDateTime as dateLabel} from './format.js';
const LABEL = {critical_document_conflict:'已核验原文事实存在冲突',critical_document_discrepancy_pending_verification:'候选原文与保存事实存在待核差异',source_corrected:'来源事实已更正',favorable:'经营有利',mixed:'经营因素交织',unfavorable:'经营不利',undetermined:'暂不判断',attractive:'价格有吸引力',balanced:'价格与预期均衡',demanding:'价格要求较高',insufficient:'证据不足',bounded:'仅支持有限范围',sufficient_for_scope:'足以支持本次问题',published:'当前有效',review_required:'待复核',expired:'已过期',superseded:'已被替代',withdrawn:'已撤回',evidenced:'已取证',qualitative:'仅定性',missing:'尚缺材料',candidate:'候选待核验',verified:'已核验',user_report:'用户提供未核验',unresolved:'冲突未解决',none:'无已知冲突',ok:'计算完成',not_applicable:'方法不适用',missing_inputs:'缺少输入',missing_assumptions:'缺少假设',stale_inputs:'输入过期',outside_search_range:'超出搜索范围',calculation_error:'计算失败',queued:'排队中',running:'研究中',waiting_data:'等待材料',waiting_user:'等待用户',succeeded:'任务已完成',partial:'部分完成',failed:'失败',cancelled:'取消',selection_completed:'选择已完成（非研究成功）',research_pending:'等待现有研究',pending_research:'等待现有研究',not_current:'非当前观点',validity_expired:'有效期已到',exact_question_and_sources_already_answered:'相同问题与来源已回答',uncovered_selected_question_followup:'已选问题尚待跟进',live_new_task_limit:'当前新增任务上限',predecessor_incomplete_requires_explicit_retry:'前序研究未完成，需要明确重试',awaiting_existing_research_with_saved_question_and_sources:'保存问题与来源，等待现有研究',selected_answerable_question:'已选择可回答的问题',scope_archived:'档案已归档',background_paused:'后台研究已暂停',major_change:'重大研究变化',reviewed:'已完成复核',review_required:'待复核',pending:'待处理',covered:'已有研究覆盖',stopped:'已停止',blocked:'受阻',selected:'已选择',not_selected:'未选择',accept:'采纳',reject:'不采纳',defer:'暂缓',independent:'独立决定',lead:'线索',researching:'研究中',tracking:'跟踪',parked:'搁置',archived:'归档',business_demand:'业务与需求',competition_durability:'竞争与持续性',financial_cash:'财务与现金',balance_sheet_allocation:'资产负债与资本配置',price_expectations:'价格与预期',downside_counter_case:'下行与反证',portfolio_alternatives:'替代选择（本次范围）'};
const FIELDS = {question:'研究问题',reason:'理由',summary:'摘要',state:'处理状态',category:'提醒类别',stage:'提醒阶段',recommendation_ref:'观点引用',updated_at:'更新时间',pending_reason:'等待原因',selected:'是否选入研究',listing_ref:'证券对象',research_task_id:'研究任务标识',merged_task_id:'现有任务标识',current_task_states:'当前任务状态',selection_task_id:'选择任务标识',sequence:'记录序号',revision:'记录版本',case_id:'档案标识',task_id:'任务标识',stop_reason:'停止原因',kind:'研究范围',dimensions:'七项取证状态',dimension:'研究维度',refs:'依据引用',status:'状态',created_at:'记录时间',checked_at:'核查时间',completed_at:'完成时间',as_of:'材料截止',next_review_at:'原始下次复核',effective_next_review_at:'当前下次复核',valid_until:'原始有效期',method:'计算方法',method_version:'方法版本',input_hash:'输入校验标识',applicable:'方法适用',quality:'质量限制',errors:'错误原因',results:'保存结果',details:'保存输入与诊断',assumptions:'保存假设',value:'保存值',unit:'单位',currency:'币种',name:'名称',provenance:'假设来源',rationale:'假设理由',source_ref:'原始来源',fact_refs:'事实依据',assumption_refs:'假设引用',period:'报告期间',basis:'口径',start:'开始',end:'结束',locator:'来源定位',content_hash:'内容校验标识',published_at:'披露时间',acquired_at:'取得时间',available_from:'可得时间',verification:'核验状态',conflict:'来源冲突',text:'保存材料',correction_refs:'更正关系',subject_ref:'主体',decision:'决定',user_text:'用户原话',decided_at:'决定时间',reevaluate_when:'再评估条件',processing_reason:'处理理由',processing:'处理结果',question_scope:'本次问题',change_reason:'改变原因',outcome:'复核结果',limitations:'限制',gaps:'缺失材料',answered:'问题已回答',stopped_because:'停止原因',completed_steps:'完成步骤',evidence_steps:'取证步骤',completion_criteria:'完成条件',stop_conditions:'停止条件',inferences:'分析推断',counter_refs:'反对证据',hypotheses:'待证假设',support_condition:'支持条件',falsification_condition:'证伪条件',check_by:'核查期限',source:'来源类型',scope:'适用范围',read:'已读',type:'提醒类型',title:'提醒标题',message:'提醒内容',result_refs:'结果引用',dedup_key:'提醒去重标识',version:'来源版本',sources:'线索来源',leads:'线索清单',max_leads:'线索上限',max_new_tasks:'新研究上限',window:'覆盖窗口',changed_fields:'变化字段'};
const label = v => LABEL[v] || String(v ?? '未知');
const originalLimitation = v => {
  const reason=String(v);
  if(reason.includes('ocr_') || reason.includes('image_text_layer'))return '扫描或图像文字尚未配置可信 OCR，需补充可读原文。';
  if(reason.includes('encrypted'))return '加密 PDF 暂不支持解析。';
  if(reason.includes('encoding'))return '原文文字编码暂不支持。';
  if(reason.includes('identity'))return '原文证券身份缺失或存在冲突。';
  if(reason.includes('period') || reason.includes('scope') || reason.includes('unit'))return '报告期间、合并口径或币种单位未能明确核验。';
  if(reason.includes('table') || reason.includes('numeric_row'))return '表格结构复杂或定位不明确，需要补充材料。';
  if(reason.includes('limit') || reason.includes('resource') || reason.includes('timeout'))return '原文超过当前解析的大小、页数或资源限制。';
  return '当前格式无法可靠解析，需要补充可读材料。';
};

const limitation=v=>v.startsWith('dependency_unavailable:')?'关键依据已更正或当前不可用，需要复核':v.startsWith('monitor_')?'复核条件或监测结果存在限制':label(v);
const refText = r => `${({recommendation:'观点',thesis:'论证',evidence:'证据',calculation:'计算',listing:'证券',task:'任务',case:'档案'}[r.kind] || r.kind)} ${r.id} · v${r.revision}${r.field_path ? ' · '+r.field_path : ''}`;
function button(text, action, cls) {const b=el('button',{type:'button',class:cls},text); b.addEventListener('click',action); return b;}
// Artifact card: a whole-width button row in the research rail (text stays a single node).
const art=(text,action)=>button(text,action,'art');
export function bootAgentViews() {
  const root=document.querySelector('#agent-workbench'); if (!root) return;
  const host=root.querySelector('#agent-artifact-extension');
  const nav=el('nav',{'aria-label':'研究浏览',class:'agent-review-nav'}), body=el('section',{'aria-live':'polite',class:'agent-review-body'}), drawer=el('section',{class:'agent-source-drawer','aria-label':'来源详情',hidden:true});
  host.append(nav,body,drawer);
  let sourceGeneration=0;
  let bindingSignature=null, searchListings=[];
  let epoch=0, conversation=null, selected='conversation', recommendations=[], taskRefs=new Map();
  const alive = token => token===epoch;
  const listingNames=new Map();async function identities(values){await Promise.all([...new Set(values.filter(Boolean).map(r=>r.id))].map(async id=>{if(listingNames.has(id))return;try{const item=await get(`/agent/listings/${id}`);listingNames.set(id,`${item.name} · ${item.ticker}`);}catch{listingNames.set(id,'证券身份暂不可读');}}));}
  const notificationCases=new Map();async function notificationIdentities(rows){await Promise.all([...new Set(rows.map(r=>r.case_id))].map(async id=>{try{const detail=await get(`/investor/cases/${id}`);notificationCases.set(id,detail.case.listing_ref);}catch{notificationCases.delete(id);}}));await identities([...notificationCases.values()]);}
  const name = r=>listingNames.get(r?.id) || '证券身份待恢复';
  function loading() {body.replaceChildren(el('p',{},'正在读取保存的研究记录…'));}
  function error(node, exc, retry) {node.replaceChildren(el('p',{class:'notice notice-error'},exc.message || '读取失败'),button('重试',retry));}
  function refs(node, values, cutoff) {for(const r of [...new Map((values || []).map(r=>[refText(r),r])).values()]){const b=button(`查看${({recommendation:'观点',thesis:'论证',evidence:'证据',calculation:'计算',listing:'证券',task:'任务',case:'档案',monitor:'复核条件'}[r.kind] || '来源')} · 版本 ${r.revision}${r.field_path?' · '+r.field_path:''}`,()=>source(r,cutoff));b.title=refText(r);b.dataset.refId=r.id;node.append(b);}}
  function audit(node,value,cutoff) {const detail=el('details');detail.append(el('summary',{},'来源版本与保存记录详情'));facts(detail,value,0,cutoff);node.append(detail);}
  function lines(node,title,values){node.append(el('h4',{},title));const list=el('ul');for(const value of values || [])list.append(el('li',{},value));if(!list.children.length)list.append(el('li',{},'尚未记录，无法据此判定完成'));node.append(list);}
  function progressCard(node,item){const p=item.progress || item;node.append(el('h3',{},p.question || '研究问题未记录'),el('p',{},item.text || '尚无保存的研究正文'));lines(node,'取证计划',p.evidence_steps);lines(node,'已完成核查',p.completed_steps);lines(node,'缺失材料与限制',[...(p.gaps || []),...(p.limitations || [])]);if(p.stopped_because)node.append(el('p',{},'停止原因：'+p.stopped_because));refs(node,item.refs || p.fact_refs);audit(node,p);}
  function facts(node, value, depth=0, cutoff) {
    if(value === null || value === undefined) {node.append(el('span',{},'未知')); return;}
    if(Array.isArray(value)) {if(!value.length) node.append(el('p',{class:'muted'},'未记录')); for(const item of value){const p=el('div',{class:'agent-detail-item'}); facts(p,item,depth+1,cutoff);node.append(p);} return;}
    if(typeof value === 'object') {
      if(value.kind && value.id && value.revision) {refs(node,[value],cutoff); return;}
      if(value.type==='numeric_claim' || value.type==='entity_ref') {node.append(el('p',{},'数值需查看对应来源与原始版本'));refs(node,[value.ref],cutoff);return;}
      const dl=el('dl'); for(const [key,item] of Object.entries(value)) {if(['id','schema_version','actor','origin_ref'].includes(key))continue;dl.append(el('dt',{},FIELDS[key] || key));const dd=el('dd');facts(dd,item,depth+1,cutoff);dl.append(dd);} node.append(dl);return;
    }
    node.append(el('span',{},typeof value==='boolean' ? (value?'是':'否') : label(value)));
  }
  async function source(r, cutoff) {
    const token=epoch,sourceToken=++sourceGeneration;const sourceAlive=()=>alive(token)&&sourceToken===sourceGeneration; drawer.hidden=false;drawer.replaceChildren(button('关闭来源',()=>{sourceGeneration++;drawer.hidden=true;}),el('h3',{},refText(r)),el('p',{},'正在读取…'));
    const run=async()=>{try {
      let endpoint;
      if(r.kind==='evidence') endpoint=`/investor/evidence/${r.id}?revision=${r.revision}${cutoff ? '&as_of='+encodeURIComponent(cutoff) : ''}`;
      else if(r.kind==='calculation') endpoint=`/investor/calculations/${r.id}?revision=${r.revision}`;
      else if(r.kind==='review') endpoint=`/investor/reviews/${r.id}?revision=${r.revision}`;
      else if(r.kind==='monitor') endpoint=`/investor/monitors/${r.id}?revision=${r.revision}`;
      else if(r.kind==='thesis') endpoint=`/investor/theses/${r.id}?revision=${r.revision}`;
      else if(r.kind==='listing') endpoint=`/agent/listings/${r.id}`;
      else if(r.kind==='case') {await casePage(r.id);return;}
      else if(r.kind==='recommendation') {await opinionPage([r]);return;}
      else if(r.kind==='task') {await taskPage(r.id);return;}
      else {drawer.replaceChildren(button('关闭来源',()=>{sourceGeneration++;drawer.hidden=true;}),el('p',{},'该来源没有独立公开详情；保留精确引用供复核。'),el('p',{},refText(r)));return;}
      const result=await get(endpoint);if(!sourceAlive())return;
      drawer.replaceChildren(button('关闭来源',()=>{sourceGeneration++;drawer.hidden=true;}),el('h3',{},refText(r)),el('p',{class:'notice'},cutoff ? `历史材料截止 ${cutoff}；当前有效性另见观点卡。` : '保存的原始记录；失败或未知结果不代表零。'));
      const record=result.data || result;if(r.kind==='calculation'){drawer.append(el('p',{class:'notice'},`${label(record.status)} · ${record.applicable?'方法适用':'方法未获适用'} · ${record.method} ${record.method_version}`));lines(drawer,'限制与错误',[...(record.quality || []),...(record.errors || [])]);refs(drawer,record.fact_refs,cutoff);}else if(r.kind==='evidence'){const discrepancy=result.warnings?.includes('critical_document_discrepancy_pending_verification'),effectiveConflict=result.status==='conflict'?(discrepancy?'存在待核差异':'存在来源冲突或更正'):label(record.conflict);drawer.append(el('p',{},`${label(record.verification)} · ${effectiveConflict} · ${record.locator}`));if(result.status==='conflict')drawer.append(el('p',{class:'notice'},'当前已知来源存在更正、冲突或待核差异；保存的旧原值不代表仍可用于判断。'));if(result.warnings?.length)lines(drawer,'当前核查限制',result.warnings.map(w=>w.startsWith('verification=')?label(w.slice(13)):label(w)));facts(drawer,{value:record.value,period:record.period,text:record.text},0,cutoff);if(record.source_kind==='document' && record.source_ref?.kind==='disclosure_original'){
        const originalButton=button('查看原文片段',async()=>{
          const excerpt=el('section',{'aria-label':'原文片段'}),asOf=cutoff || new Date().toISOString();let fragmentGeneration=0;
          const load=async offset=>{const generation=++fragmentGeneration;try{
            const original=await get(`/investor/disclosures/originals/${record.source_ref.id}/fragments?listing_id=${record.subject_ref.id}&as_of=${encodeURIComponent(asOf)}&offset=${offset}&limit=10`);
            if(!sourceAlive() || generation!==fragmentGeneration)return;
            excerpt.replaceChildren(el('p',{class:'notice'},'原文为待核材料；扫描与复杂表格可能无法解析。'));
            for(const fragment of original.fragments || [])excerpt.append(el('p',{},fragment.locator),el('pre',{},fragment.text));
            const candidates=original.candidate_facts || [];
            excerpt.append(el('h4',{},'原文候选事实（待核验）'),el('p',{},`共 ${original.candidate_fact_count || 0} 项；候选与片段按相同偏移分别分页。`));
            for(const candidate of candidates)excerpt.append(el('p',{},`${candidate.metric} · ${candidate.locator} · ${candidate.period?.start} 至 ${candidate.period?.end} · ${candidate.period?.basis}`),el('pre',{},`${candidate.value?.value ?? '未知'} ${candidate.value?.unit || ''} ${candidate.value?.currency || ''}`),el('p',{},`片段校验：${candidate.fragment_hash}`));
            lines(excerpt,'原文解析限制',[...(original.warnings || []),...(original.reason?[original.reason]:[])].map(originalLimitation));
            const pagination=el('nav',{'aria-label':'原文片段分页'});
            if(offset>0)pagination.append(button('上一页原文片段',()=>load(Math.max(0,offset-10))));
            if(offset+10<original.fragment_count)pagination.append(button('下一页原文片段',()=>load(offset+10)));
            pagination.append(el('a',{href:`/investor/disclosures/originals/${record.source_ref.id}/download?as_of=${encodeURIComponent(asOf)}`,download:'',rel:'noopener'},'下载原件附件'));
            excerpt.append(pagination);if(!excerpt.isConnected)drawer.append(excerpt);originalButton.disabled=true;
          }catch(exc){if(sourceAlive() && generation===fragmentGeneration)error(excerpt.isConnected?excerpt:drawer,exc,()=>load(offset));}};
          await load(0);
        });drawer.append(originalButton);
      }}audit(drawer,record,cutoff);drawer.scrollIntoView({block:'nearest'});
    }catch(exc){if(sourceAlive()){error(drawer,exc,run);drawer.prepend(button('关闭来源',()=>{sourceGeneration++;drawer.hidden=true;}));}}};await run();
  }
  function content(node,nodes,prefix,data) {
    const p=el('p'); (nodes||[]).forEach((n,index)=>{const resolved=data.resolved_nodes[`${prefix}.${index}`];p.append(el('span',{},n.type==='text'?n.text:resolved?.text || '［原始引用暂无法解析］'));if(n.ref)p.append(button('依据',()=>source(n.ref,data.body_as_of)));});node.append(p);
  }
  function opinion(data) {
    const v=data.view,r=v.recommendation,card=el('article',{class:'agent-opinion','data-recommendation-id':r.id});
    card.append(el('h3',{},`${data.subject_label || "公司观点"} · 保存于 ${dateLabel(r.created_at)}`),el('p',{class:'notice agent-current-validity'},`当前有效性：${label(v.status)}。历史正文截止 ${dateLabel(data.body_as_of)}，不随新材料改写。`),el('p',{},r.question_scope));
    const summary=el('div',{class:'agent-opinion-summary'});for(const [title,value] of [['经营判断',r.business_view],['价格判断',r.price_view],['研究充分度',r.sufficiency]])summary.append(el('p',{},`${title}：${label(value)}`));card.append(summary,el('p',{},'充分度是本次问题的支撑程度，不是盈利概率。'));
    card.append(el('p',{},`原有效期 ${dateLabel(r.valid_until)}；当前有效期 ${dateLabel(v.effective_valid_until)}`),el('p',{},`原复核时间 ${dateLabel(r.next_review_at)}；当前复核时间 ${dateLabel(v.effective_next_review_at)}`));
    if(v.limitation_reasons.length)card.append(el('p',{class:'notice notice-warn'},'有效性限制：'+v.limitation_reasons.map(limitation).join('；')));
    if(v.replacement_ref) {card.append(el('p',{},'已有替代观点，请分别查看原始版本与新观点。'));refs(card,[v.replacement_ref]);}
    card.append(el('h4',{},'七项取证状态'));for(const d of r.assessed_dimensions){const p=el('div');p.append(el('p',{},`${label(d.dimension)}：${label(d.status)}${d.applicable?'':' · 不适用'}${d.reason?' · '+d.reason:''}`));refs(p,d.refs,data.body_as_of);card.append(p);}
    card.append(el('h4',{},'事实、假设与推断'));r.claims.forEach((c,i)=>{const part=el('section');part.append(el('strong',{},`${({fact:'事实',assumption:'假设',inference:'分析推断'}[c.kind])} · ${label(c.verification)}`));content(part,c.text,`recommendation.claims.${i}.text`,data);part.append(el('p',{},'若判断错误：'+c.impact_if_wrong),el('small',{},'支持依据'));refs(part,c.support_refs,data.body_as_of);part.append(el('small',{},'反对依据'));refs(part,c.opposition_refs,data.body_as_of);card.append(part);});
    card.append(el('h4',{},'反证'));content(card,r.counter_case,'recommendation.counter_case',data);
    card.append(el('h4',{},'假设与缺口'));for(const h of r.assumptions){card.append(el('p',{},`${h.question} · ${label(h.status)}`),el('p',{},'支持条件：'+h.support_condition),el('p',{},'证伪条件：'+h.falsification_condition),el('p',{},'核查期限：'+dateLabel(h.check_by)));if(h.last_check_ref)refs(card,[h.last_check_ref],data.body_as_of);}if(!r.assumptions.length)card.append(el('p',{},'未保存独立假设'));lines(card,'材料缺口',r.gaps);
    card.append(el('h4',{},'下一步与复核条件'));for(const action of r.conditional_actions){card.append(el('p',{},data.action_labels[action.action] || '行动标签暂不可用'));card.append(el('small',{},'成立条件与理由'));refs(card,[...action.condition_refs,...action.rationale_refs],data.body_as_of);}if(!r.conditional_actions.length)card.append(el('p',{},'没有保存行动建议，等待核验材料。'));refs(card,r.conditions,data.body_as_of);refs(card,r.monitor_refs,data.body_as_of);
    card.append(el('h4',{},'估值与来源'));refs(card,r.dependency_refs.map(d=>d.ref),data.body_as_of);refs(card,r.provenance,data.body_as_of);
    if(Object.keys(data.render_errors).length)card.append(el('p',{class:'notice notice-error'},'部分历史引用无法解析，请查看原始来源；未补零或重算。'));
    audit(card,{limitation_reasons:v.limitation_reasons,id:r.id,revision:r.revision,as_of:r.as_of,created_at:r.created_at,method_revision_id:r.method_revision_id,mandate_revision_id:r.mandate_revision_id});
    card.append(button('刷新当前有效性',()=>opinionPage([{kind:'recommendation',id:r.id,revision:r.revision}])));
    card.append(button('档案、决定与过程复盘',()=>casePage(r.case_ids[0])));return card;
  }
  async function opinionPage(values) {
    selected="opinion";
    const token=++epoch;drawer.hidden=true;loading();
    try {const data=await Promise.all(values.map(r=>get(`/investor/recommendations/${r.id}/body?revision=${r.revision}`)));if(!alive(token))return;
      root.dispatchEvent(new CustomEvent('agent:control-context',{detail:{refs:values}}));
      body.replaceChildren(el('h2',{},values.length>1?'观点版本并列比较':'正式公司观点'));const grid=el('div',{class:'agent-version-grid'});data.forEach(d=>grid.append(opinion(d)));body.append(grid);
      if(values.length>1)body.append(el('p',{},'逐版本保留当时来源、期间、方法与限制；此页面不生成总分或新排名。'));
    }catch(exc){if(alive(token))error(body,exc,()=>opinionPage(values));}
  }
  async function taskPage(id) {
    selected="task";
    const token=++epoch;loading();try {const [task,progress]=await Promise.all([get(`/agent/tasks/${id}`),get(`/agent/tasks/${id}/research`)]);if(!alive(token))return;
      body.replaceChildren(el('h2',{},'研究过程与比较产物'),el('p',{},`任务 ${id} · ${label(task.status)} · ${task.stop_reason || '尚未停止'}`));refs(body,task.binding?.context_refs || []);
      if(!progress.length)body.append(el('p',{},'尚无已保存研究过程。任务完成不自动等于正式公司观点。'));
      for(const item of progress){const p=item.progress || item; const section=el('section');section.append(el('h3',{},p.kind==='comparison'?'比较研究（保存的依据）':p.kind==='review'?'过程复盘':'本次问题研究'));progressCard(section,item);body.append(section);}refs(body,task.artifact_refs.filter(r=>r.kind==='recommendation'));
    }catch(exc){if(alive(token))error(body,exc,()=>taskPage(id));}
  }
  async function casePage(id) {
    selected="case";
    const token=++epoch;drawer.hidden=true;loading();try {const [detail,reviews,decisions,history,research,events]=await Promise.all([get(`/investor/cases/${id}`),get(`/investor/cases/${id}/reviews?limit=50`),get(`/investor/decisions?case_id=${id}&limit=50`),get(`/investor/cases/${id}/recommendations?limit=50`),get(`/investor/cases/${id}/research?limit=50`),get(`/investor/cases/${id}/publication-events?limit=50`)]);await identities([detail.case.listing_ref]);if(!alive(token))return;
      root.dispatchEvent(new CustomEvent('agent:control-context',{detail:{refs:[{kind:'case',id:detail.case.id,revision:detail.case.revision},detail.case.listing_ref,detail.case.current_recommendation_ref].filter(Boolean)}}));
      body.replaceChildren(el('h2',{},'公司档案与过程复盘'),el('p',{},`${name(detail.case.listing_ref)} · ${label(detail.case.status)}`));refs(body,[detail.case.listing_ref]);refs(body,detail.source_refs);
      body.append(el('p',{},'复盘关注决定依据、观点变化、缺失材料与核查过程；没有真实成交口径时不展示收益或胜率。'));
      if(detail.case.current_recommendation_ref)refs(body,[detail.case.current_recommendation_ref]);
      const recs=history.map(x=>x.ref);for(const episode of detail.episodes)for(const r of episode.artifact_refs || [])if(r.kind==='recommendation'&&!recs.some(x=>x.id===r.id&&x.revision===r.revision))recs.push(r);
      for(const r of recs)refs(body,[r]);
      if(recs.length>1)body.append(button('并列比较保存的观点版本',()=>opinionPage(recs)));
      body.append(el('h3',{},'当时计划与缺失材料'));if(!research.length && !detail.plans.length)body.append(el('p',{},'尚无已保存的计划或研究进展，完成度未知。'));for(const item of research){const section=el('section');progressCard(section,item);section.append(button('查看对应研究任务',()=>taskPage(item.task_id)));body.append(section);}if(detail.plans.length)audit(body,detail.plans);
      body.append(el('h3',{},'用户决定时间线'));if(!decisions.length)body.append(el('p',{},'尚无用户决定；未知不等于未采纳。'));else for(const decision of decisions){body.append(el('p',{},`${dateLabel(decision.decided_at)} · ${label(decision.decision)} · ${decision.user_text}${decision.reason?' · '+decision.reason:''}`));audit(body,decision);}
      body.append(el('h3',{},'观点变化时间线'));for(const entry of [...history].sort((a,b)=>a.created_at.localeCompare(b.created_at))){const row=el('article');row.append(el('p',{},`发布于 ${dateLabel(entry.created_at)} · 材料截止 ${dateLabel(entry.as_of)}`));refs(row,[entry.ref]);body.append(row);}
      for(const event of events){const row=el('article',{class:'agent-publication-event'});row.append(el('p',{},`${dateLabel(event.checked_at)} · ${{published:'正式观点已发布',superseded:'旧观点已被替代',extended:'原观点通过复核并延长有效期',review_required:'观点需要复核',expired:'观点有效期已到',withdrawn:'观点已撤回'}[event.type] || '观点状态变化'}`));refs(row,[event.recommendation_ref,event.replacement_ref,event.review_ref].filter(Boolean));audit(row,event);body.append(row);}
      body.append(el('h3',{},'研究与复核时间线'));for(const episode of detail.episodes)body.append(el('p',{},`研究阶段 ${episode.number}：${episode.question} · ${dateLabel(episode.created_at)}`));audit(body,detail.episodes);if(!reviews.length)body.append(el('p',{},'尚无复核周期。普通检查不代表重大提醒。'));for(const cycle of reviews){body.append(el('p',{},`${cycle.checked_at || cycle.started_at ? dateLabel(cycle.checked_at || cycle.started_at) : '核查时间未知'} · ${label(cycle.state)} · ${label(cycle.pending_reason || cycle.outcome || '暂无已确认结果')}`));refs(body,cycle.result_refs);audit(body,cycle);if(cycle.task_id)body.append(button('查看复核研究过程',()=>taskPage(cycle.task_id)));}
      for(const [title,rows,path,key] of [['更多用户决定',decisions,`/investor/decisions?case_id=${id}&limit=50`,'id'],['更多复核周期',reviews,`/investor/cases/${id}/reviews?limit=50`,'id'],['更多已保存研究',research,`/investor/cases/${id}/research?limit=50`,'id'],['更多观点历史',history,`/investor/cases/${id}/recommendations?limit=50`,'ref'],['更多观点变化事件',events,`/investor/cases/${id}/publication-events?limit=50`,'sequence']]){if(rows.length!==50)continue;let cursor=key==='ref'?rows.at(-1).ref.id:rows.at(-1)[key];const more=button(title,async()=>{more.disabled=true;try{const page=await get(path+'&after='+encodeURIComponent(cursor));if(!alive(token))return;for(const item of page){const section=el('section');if(item.progress)progressCard(section,item);else if(item.ref){section.append(el('p',{},`发布于 ${dateLabel(item.created_at)}`));refs(section,[item.ref]);recs.push(item.ref);}else if(item.decision){section.append(el('p',{},`${dateLabel(item.decided_at)} · ${label(item.decision)} · ${item.user_text}`));audit(section,item);}else{section.append(el('p',{},`${label(item.type || item.state)} · ${label(item.pending_reason || item.outcome || '结果未知')}`));audit(section,item);}body.insertBefore(section,more);}if(page.length<50)more.remove();else cursor=key==='ref'?page.at(-1).ref.id:page.at(-1)[key];}catch(exc){if(alive(token))body.append(el('p',{class:'notice notice-error'},exc.message));}finally{more.disabled=false;}});body.append(more);}
      if(reviews.length===50 || decisions.length===50 || history.length===50 || research.length===50)body.append(el('p',{},'本页各显示最多50条，未展示全部记录；分母未知。'));
    }catch(exc){if(alive(token))error(body,exc,()=>casePage(id));}
  }
  async function listPage(kind,after) {
    selected=kind;mark(kind);const token=++epoch;drawer.hidden=true;loading();const path=kind==='cases'?`/investor/cases?limit=50${after?'&after='+encodeURIComponent(after):''}`:kind==='notifications'?`/investor/notifications?limit=50&after=${after || 0}`:`/investor/discovery/batches?limit=50&after=${after || 0}`;
    try {const rows=await get(path);if(kind==='cases')await identities(rows.map(r=>r.listing_ref));if(kind==='notifications')await notificationIdentities(rows);if(!alive(token))return;body.replaceChildren(el('h2',{},kind==='cases'?'公司档案':kind==='notifications'?'站内提醒':'有限探索与线索'));
      if(!rows.length)body.append(el('p',{},'本页没有已保存记录。未产生记录不代表研究通过或没有风险。'));
      body.append(el('p',{class:'muted'},`本页 ${rows.length} 条；全体记录数量未知。`));
      for(const row of rows){const section=el('article',{class:'agent-browse-record'});
        if(kind==='cases'){const open=button(`${name(row.listing_ref)} · ${label(row.status)}`,()=>casePage(row.id));open.dataset.caseId=row.id;section.append(open);refs(section,[row.listing_ref]);}
        else if(kind==='notifications'){const readStatus=el('p',{class:'agent-notification-read'},`提醒 #${row.sequence} · v${row.revision} · ${row.read?'已读':'未读'}`);section.append(el('h3',{},`${name(notificationCases.get(row.case_id))} · ${label(row.category)}`),el('p',{class:'agent-notification-content'},({published:'新公司观点已发布',review_required:'材料变化，需要复核',reviewed:'复核已完成',needs_input:'复核需要补充材料'}[row.stage] || '研究状态待确认')),readStatus);lines(section,'变化或待处理原因',(row.reasons || []).map(reason=>typeof reason==='string'?limitation(reason):reason.reason || reason.summary || '查看保存的复核依据'));refs(section,[row.recommendation_ref,row.original_recommendation_ref,...(row.result_refs || [])].filter(Boolean));audit(section,row);section.append(button('查看相关档案',()=>casePage(row.case_id)));
          const b=button(row.read?'标为未读':'标为已读',async()=>{b.disabled=true;try{const result=await patch(`/investor/notifications/${row.sequence}/read`,{read:!row.read,mutation:{expected_revision:row.revision,idempotency_key:crypto.randomUUID()}});if(alive(token)){row.read=result.read;row.revision=result.revision;b.textContent=row.read?'标为未读':'标为已读';readStatus.textContent=`提醒 #${row.sequence} · 服务器已确认：${row.read?'已读':'未读'} · v${row.revision}`;}}catch(exc){if(alive(token))section.append(el('p',{class:'notice notice-error'},exc.message+'；请刷新后核对当前状态。'));}finally{b.disabled=false;}});section.append(b);}
        else section.append(button(`探索批次 #${row.sequence} · ${label(row.state)}`,()=>batchPage(row.id)));
        body.append(section);
      }
      if(rows.length===50)body.append(button('下一页',()=>listPage(kind,kind==='cases'?rows.at(-1).id:rows.at(-1).sequence)));
      body.append(button('刷新首页',()=>listPage(kind)));
    }catch(exc){if(alive(token))error(body,exc,()=>listPage(kind,after));}
  }
  async function batchPage(id) {selected="batch";const token=++epoch;loading();try{const batch=await get(`/investor/discovery/batches/${id}`);await identities((batch.leads || []).map(lead=>lead.listing_ref));if(!alive(token))return;body.replaceChildren(el('h2',{},'有限探索处理过程'),el('p',{},`批次 ${id} · ${label(batch.state)}。选择完成仅代表短名单处理完成。`));body.append(el('p',{},batch.summary || '摘要尚未保存'));for(const lead of batch.leads || []){const section=el('article');section.append(el('h3',{},name(lead.listing_ref)),el('p',{},lead.question || '待核查线索'),el('p',{},`${label(lead.status)} · ${label(lead.processing_reason || '处理原因未知')}`),el('p',{},'选择理由：'+(lead.reason || '未知')),el('p',{},'再评估条件：'+(lead.reevaluate_when || '未知')));refs(section,[lead.listing_ref].filter(Boolean));audit(section,lead);body.append(section);}audit(body,batch);for(const lead of batch.leads || []){const tid=lead.merged_task_id || lead.research_task_id;if(tid)body.append(button(`${lead.merged_task_id?'合并到现有研究':'查看研究任务'} ${tid}`,()=>taskPage(tid)));}}catch(exc){if(alive(token))error(body,exc,()=>batchPage(id));}}
  function conversationPage() {selected='conversation';mark('conversation');++epoch;drawer.hidden=true;body.replaceChildren(el('h2',{},'当前对话研究产物'));if(!recommendations.length)body.append(el('p',{},'当前对话没有正式公司观点；局部回答不表示完整公司判断。'));for(const r of recommendations)body.append(art(refText(r),()=>opinionPage([r])));if(recommendations.length>1)body.append(art('并列查看引用的观点',()=>opinionPage(recommendations)));for(const t of taskRefs.values())body.append(art(`研究过程 ${t.id} · ${label(t.status)}`,()=>taskPage(t.id)));}
  function searchPage() {
    selected='search';mark('search');const token=++epoch;let searchGeneration=0;drawer.hidden=true;
    const input=el('input',{type:'search',maxlength:'120',placeholder:'例如：净利润、竞争、风险','aria-label':'已保存材料关键词'}),results=el('section',{'aria-live':'polite'});
    const run=async offset=>{const generation=++searchGeneration;if(!searchListings.length){results.replaceChildren(el('p',{},'请先在当前对话选择证券，再搜索已保存材料。'));return;}
      results.replaceChildren(el('p',{},'正在搜索已保存材料…'));
      try{const cutoff=new Date().toISOString(),data=await post('/investor/disclosures/search',{listing_ids:searchListings.map(r=>r.id),query:input.value,as_of:cutoff,offset,limit:10});await identities(searchListings);if(!alive(token)||generation!==searchGeneration)return;
        results.replaceChildren(el('p',{},`已检查 ${data.scanned_records} 条保存记录，窗口内 ${data.matched_count_in_window} 处命中。${data.window_exhausted?'覆盖窗口已达上限，可按证券缩小范围。':''}`),el('p',{class:'notice'},'相关性不代表真实性；搜索仅提供线索，相同原文转载不增加独立支持。'));
        if(!data.matches.length)results.append(el('p',{},'没有可见命中；这不表示公司没有披露相关内容。'));
        for(const hit of data.matches){const section=el('article');section.append(el('h3',{},`${name(hit.subject_ref)} · ${hit.title}`),el('p',{},`${hit.locator} · ${hit.verification_states.map(label).join(' / ')} · ${hit.effective_status==='conflict'?'存在冲突、更正或待核差异':hit.effective_status==='ok'?'保存材料可读':'材料不可用'}`),el('p',{},`披露 ${dateLabel(hit.published_at)}；取得 ${dateLabel(hit.acquired_at)}；解析可用 ${dateLabel(hit.available_from)}`),el('pre',{},hit.text));if(hit.source_audit_truncated)section.append(el('p',{},`同原文来源共 ${hit.source_copy_count} 条，此处审计仅展示前 10 条；核验状态与限制已合并保留。`));if(hit.text_truncated)section.append(el('p',{},'此处仅显示命中附近最多 2000 字；请按精确来源定位读回。'));if(hit.warnings.length)lines(section,'冲突、更正及核查限制',hit.warnings.map(w=>w.startsWith('verification=')?label(w.slice(13)):label(w)));refs(section,[hit.evidence_ref],cutoff);const adjacent=el('details');adjacent.append(el('summary',{},'查看相邻原文与校验信息'));for(const f of hit.adjacent_context)adjacent.append(el('p',{},f.locator),el('pre',{},f.text),el('p',{},`片段校验 ${f.fragment_hash}${f.text_truncated?' · 展示已截短':''}`));section.append(adjacent);audit(section,{original_hash:hit.original_hash,fragment_hash:hit.fragment_hash,source_audit:hit.source_audit},cutoff);results.append(section);}
        if(offset>0)results.append(button('上一页搜索结果',()=>run(Math.max(0,offset-10))));if(offset+10<data.matched_count_in_window)results.append(button('下一页搜索结果',()=>run(offset+10)));
      }catch(exc){if(alive(token)&&generation===searchGeneration)error(results,exc,()=>run(offset));}};
    const form=el('form');form.append(input,el('button',{type:'submit'},'搜索保存材料'));form.addEventListener('submit',event=>{event.preventDefault();run(0);});body.replaceChildren(el('h2',{},'已保存材料搜索'),el('p',{},'仅搜索当前所选证券的既有研究来源和已保存解析片段；不会下载新材料。'),form,results);
  }
  nav.append(button('对话产物',conversationPage),button('搜索保存材料',searchPage),button('公司档案',()=>listPage('cases')),button('站内提醒',()=>listPage('notifications')),button('线索与探索',()=>listPage('discovery')));
  // The rail's segmented switch marks the active projection; drill-down pages keep the parent's mark.
  ['conversation','search','cases','notifications','discovery'].forEach((key,index)=>{nav.children[index].dataset.view=key;});
  function mark(key){for(const b of nav.children)b.setAttribute('aria-pressed',String(b.dataset.view===key));}
  root.addEventListener('agent:messages',({detail})=>{if(conversation!==detail.conversationId){conversation=detail.conversationId;taskRefs.clear();selected='conversation';++epoch;drawer.hidden=true;}recommendations=[...new Map(detail.messages.flatMap(m=>[...m.context_refs,...m.artifact_refs]).filter(r=>r.kind==='recommendation').map(r=>[`${r.id}:${r.revision}`,r])).values()];if(selected==='conversation')conversationPage();});
  root.addEventListener('agent:task',({detail})=>{taskRefs.set(detail.id,detail);if(selected==='conversation')conversationPage();});
  root.addEventListener('agent:binding',({detail})=>{searchListings=detail.refs.filter(r=>r.kind==='listing').slice(0,10);const signature=JSON.stringify(detail.refs);if(bindingSignature!==null && signature!==bindingSignature){selected='conversation';conversationPage();}bindingSignature=signature;});
  root.addEventListener('agent:ready',()=>{if(selected==='conversation')conversationPage();});
  conversationPage();
}
