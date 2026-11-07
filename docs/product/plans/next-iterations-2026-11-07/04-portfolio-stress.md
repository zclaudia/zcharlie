# 方向四：组合压力试验与决定事实核对

- 状态：设计主审完成；本文是设计提案，待实施与验收。
- 基线：`1f62fa4`；审阅日期：2026-11-07；范围：单用户、A股、CNY。
- 交付顺序：S1确定性压力试验 → S2到期事实核对；两阶段分别验收、分别开关。
- 本轮只写设计；不修改业务代码、不请求付费模型、不连接券商、不部署。
- 共同约定与依赖见[本目录总览](README.md)；发生冲突时先按该约定联审，不私建共享基础。

## 1. 用户问题与交付边界

用户希望知道：如果行业再跌30%，当前持仓和已经讨论过的方案分别损失多少，哪些约束先失守？
决定作出后，希望把当时的条件、后来确认的真实事件和到期事实放在一起，避免靠记忆复盘。
S1回答静态冲击的金额、权重与阈值检查；S2回答预先登记的问题是否有足够事实支持。
首期排除完整收益率、资金加权/时间加权收益、反事实收益、收益归因、最优组合与自动交易。
不把压力冲击称为预测或概率，不把到期涨跌称为决定质量，不根据后见数据重写原决定。
不新增账本导入、account-fit、组合建议或研究过程评分，不复制现有AG22买卖情景计算。

## 2. 已核对的复用能力

以下路径与接口是基线已存在能力；后文标“新增”的接口、对象、文件均为提案。

| 基线对象/入口 | 已有事实 | 本方向复用方式 |
|---|---|---|
| `storage/models/evidence.py:19` | `SavedCalculation`主键`(id,revision)`，owner与JSON payload，不可变ORM钩子 | 保存压力与核对的`CalculationRecord`，不新建结果仓库 |
| `evidence/contracts.py:108` | 方法版本、fact_refs、assumptions、input_hash、质量、details | 结果逐项引用；partial在details表达，不新增全局CalculationStatus |
| `storage/models/portfolio.py:11` | account CAS head、append-only event、immutable snapshot/valuation | 只读精确事实与价格版本；从不推进账户revision |
| `portfolio/calculation_contracts.py:15` | 单股/行业/现金约束、20情景、费用、交易规则、精确估值 | 复用已保存情景的数量与现金，增加价格冲击层 |
| `portfolio/calculations.py` | 保存request/snapshot/mandate/sources/valuation及逐情景诊断 | 从冻结details适配，禁止用当前价格重新解释旧计算 |
| `portfolio/plan_contracts.py` | whole-account plan/no_action/options及计算引用 | 选项以精确plan+option+calculation关联，不生成新建议 |
| `investor/contracts.py:262` | `UserDecision`可绑定plan/option/action或公司意见 | 新观察关联原决定；接受建议不等于发生交易 |
| `portfolio/contracts.py` | `EventRecord`含occurred_at/recorded_at；更正通过reversal+replacement | 显式关联事件并核对更正链，不追加交易事实 |
| `investor/methods.py:444` | 已有真实任务的过程复盘 | 保持原职责；本方向核对用户决定后的结构化事实 |

已有HTTP入口：`POST /portfolio/calculations`、`GET /portfolio/calculations/{id}?account_id=...`。
已有读取：`GET /portfolio/snapshot`、`/portfolio/events`、`/portfolio/events/{id}`，均需account_id。
已有估值：`GET /portfolio/valuations/current|history|{id}`；已有方案：`GET /portfolio/plans/current|{id}`。
已有决定：`POST /investor/decisions`使用`ExecuteRequest`，`GET /investor/decisions`分页；无假定的现成单决定GET。
实施时在`UserControlService`补owner校验的精确决定读取方法；不依赖分页恰好找到决定。
`ObjectRef.kind`是闭合枚举，新增`portfolio_stress`与`decision_observation`须显式扩展与测试解析器。
`IndustryClassification`不是不可变版本表，且没有ObjectRef命名空间；保存字段副本+hash，不能伪造revision。
SavedCalculation新增method不扩大正式采信范围：publication、AG22、方案与方法审批消费者保持原方法/版本白名单。
`portfolio_stress`和`decision_fact_check`只能供本页、观察与收件箱读取，不能充当正式估值、执行证据或审批通过证明。
新增ObjectRef仅提供寻址；每个消费者仍须验证kind、method/version、用途和原有门禁，通用resolver不得自动提权。

## 3. 用户流程、页面与状态反馈

S1入口在现有持仓/组合方案页，新增“压力试验”；独立页建议`/app/portfolio/stress`。
第一步选择账户与精确snapshot/valuation；显示账本revision、事实时间、价格时间和覆盖。
第二步选择真实基线，最多附加3个现有计算情景或方案选项；总共最多4条比较路径。
无历史方案时只比较真实持仓，不自动要求Agent生成方案；不在此页输入新的买卖指令。
第三步选择最多20个命名冲击；默认模板为全局-20%、指定行业-30%，都需用户确认。
逐股覆盖行业、行业覆盖全局；无全局且未命中的持仓明确采用0冲击并展示，不偷偷补默认值。
可选“最多承受损失X元/比例Y”是本次研究阈值，界面不称正式授权、保证或止损单。
第四步提交后显示队列、版本与取消按钮；结果显示前后资产、损失、损失来源和约束变化。
比较表每个数字可展开原数量、价格引用、行业副本、冲击来源、公式和未知原因。
保存自动发生于成功提交；“按新价格重做”创建新revision，旧结果标注历史基准并永久保留。
用户可将结果关联现有决定；若没有决定，导航到已有决定流程，返回时带回引用。
S2入口为“登记到期核对”；仅允许有限结构化问题，不把自由文本交给模型执行。
登记前展示时间、指标、阈值、容差、数据口径及自动本地核对授权；到期后进入核对详情页。
详情排列“当时决定 → 预登记问题 → 显式关联实际事件 → 观察结果/缺口”，不默认标记已执行。

| 页面条件 | 呈现与行动 |
|---|---|
| 无账户/无snapshot | 空态引导到已有账本；不虚构初始资金 |
| 无valuation或仍更新 | 展示已有更新入口/等待；不静默改用旧snapshot价格 |
| partial或缺现金/缺价 | 显示已知部分与受影响行；禁止展示伪完整总资产/损失率 |
| 旧方案/旧估值 | 可读、可计算历史压力，醒目标识“历史研究”；不冒充当前建议 |
| 输入冲突/账户已变 | 保留表单，展示旧/新revision；由用户重选，不自动重绑定 |
| 排队/失败/取消 | 展示安全错误码、原run与显式重试；不得重复创建研究对象 |
| 无关联实际事件 | 显示“执行未知”；允许去账本确认后显式关联 |
| 到期缺证据 | 显示“无法判断”及缺失项；允许补录、迟到重算与历史报告切换 |

## 4. S1输入冻结、计算与精度

提交事务先验证owner、账户、对象版本以及引用之间的关联，再冻结全部可计算输入并入队。
真实基线必须引用同一账户的snapshot与valuation，valuation.snapshot_id必须与snapshot一致。
选项必须来自同一snapshot/valuation口径；跨基准比较返回422，提示分别建立研究。
旧AG22-v1没有valuation引用：S1不适配为同页路径，可单独显示“需以明确估值重建计算”。
适配器只读`portfolio_scenarios/ag22-v2`已保存details；未知method_version返回unsupported_basis。
方案仅补plan/option出处；正式计算输入始终来自其calculation对应scenario，不能信任客户端提交数量。
数量、现金和费用继承原计算；费用已计入现金，不再扣一次，不修改交易规则或可执行标记。
行情只用valuation与原计算sources精确冻结的未复权`market_snapshot`；验证id/version/股号/价格正值。
`trade_date <= valuation_date`且`captured_at <= knowledge_cutoff`；cutoff以服务端冻结引用为准，不接受未来cutoff。
沿用估值的可用/过期判定；不得把已被原估值判为未知的价格从底层表重新捞回。
原计算新增持仓的价格也须在共同cutoff内且满足同一价格年龄策略；否则该路径partial。
S1行业体系在请求、manifest、resolver查询统一使用存储值`csrc`，UI显示CSRC；区间按上海基准日，半开`[valid_from,valid_to)`。
基线AG22请求枚举存在大写CSRC，不据此发大写存储查询；适配其已存来源副本，缺失保持未知，不补造来源。
行业捕获必须不晚于知识截止；来源多解/缺码均为未知，不任选一条，不以今天分类解释旧持仓。
保存行业row id、system/code/source、valid区间、captured_at及规范化hash；重放直接使用副本。
若重读发现行业源行改变，只报告source_changed，不能改变已有结果；重建研究需新revision。
主审决定：S1可先交全局/逐股；行业缺失不阻止整份研究，逐结果标unknown。行业冲击影响行未知，不退回全局值。
已知分类行继续计算，全局/逐股独立冲击仍可complete；仅受行业冲击或行业约束影响的结果partial。
冲击值`s`是Decimal比例，允许`-1 <= s <= 10`；上界为产品计算范围，不是行情涨幅限制。
每个冲击内同粒度重复目标拒绝422；覆盖只取最细一个，不做加法或乘法合成。
模板与自填均记录`provenance=user_scenario`，客户端不得声明为历史事实或模型预测。
输入Decimal接受字符串/整数、拒绝float/bool/NaN/Infinity；沿用40位、最多18位小数的输入边界。
输入数量/价格/现金还需有限位数上界；禁止`1e1000000`等超大指数先触发高精度分配。
金额用动态localcontext：至少128位，按输入最大整数/小数跨度预留乘加和持仓累积位数。
内核全程Decimal，禁止先float后转回；比例precision=40；显示金额四舍六入五成双到0.01元。
保存未做显示量化的数值字符串；展示四舍五入差额单列，不能逐行舍入后再当作精确合计。

```text
v_i = q_i * p_i;          v_i,s = q_i * p_i * (1 + s_i)
V = C + sum(v_i);         V_s = C + sum(v_i,s)
loss_i = v_i - v_i,s;     loss = V - V_s
loss_ratio = loss / V     (仅V > 0且全量已知)
w_i,s = v_i,s / V_s       (仅V_s > 0且全量已知)
```

负loss表示假设盈利，不截断为0；净资产为0时金额可存在，比例与权重为unknown。
全仓-100%且现金为0得到V_s=0，损失率可为100%，冲击后权重unknown，不抛除零错误。
正式mandate约束只读取原冻结版本并以`pass/fail/unknown/not_configured`重检，不改其head。
未知现金时仅输出已知证券损失小计，不能称总损失或总资产；数量/价格缺失行不按0处理。
q=0时无需价格即可产生零市值/零损失；partial资产范围不产生完整权重或承受阈值通过结论。
停牌行保留日期、trade_status与标识；可计算假设估值但明确“未评估流动性”，不能标可卖出。
涨跌停、T+1、滑点、连续停牌、冲击相关性及价格冲击均不模拟；不输出成交能力或概率。
SavedCalculation.status=ok表示算术执行完；details.quality_state=complete|partial，未知Quantity保留原因。
完全无可用计算项时status=missing_inputs、results={}；不可把非ok结果塞入results绕过现有校验。

## 5. 新增数据结构与不可变版本

所有时间入库UTC，JSON按ISO8601输出；金额JSON为Decimal字符串；owner只取认证上下文。
所有新领域表由service维护owner及关系完整性，不能依赖当前未启用的SQLite外键强制检查。

| 表/字段 | 约束、索引与职责 |
|---|---|
| `portfolio_stress_head` | id UUID PK, owner_id, account_id, revision, control_revision, enabled, permission_epoch, created_at |
| head索引 | `(owner_id,account_id,created_at,id)`；revision为配置head，control_revision为取消/授权CAS |
| `portfolio_stress_revision` | `(stress_id,revision)` PK, owner_id, account_id, input_hash, result_id UUID, requested_at, payload JSON |
| revision约束 | `UNIQUE(owner_id,result_id)`；payload不可变，包含frozen_inputs/version_refs/参数/边界/算法版本 |
| `decision_observation_head` | id UUID PK, owner_id, account_id, revision, control_revision, enabled, permission_epoch, due_at, next_check_at |
| observation索引 | `(owner_id,enabled,next_check_at,id)`；仅active head可自动排队 |
| `decision_observation_revision` | `(observation_id,revision)` PK, owner_id, decision_id, registered_at, input_hash, payload JSON |
| observation约束 | payload不可变；保存精确决定副本、predicate、截止日规则、授权、calendar副本/hash |
| `decision_event_binding` | id UUID PK, owner_id, observation_id, observation_revision, operation_id, binding_kind, payload, recorded_at |
| binding约束 | `UNIQUE(owner_id,operation_id)`；仅追加，payload含事件/fact_hash/分配数量及撤销绑定引用 |
| `decision_binding_head` | `(owner_id,account_id)` PK, revision；跨观察分配共用的CAS计数，首次创建为0 |
| 完整声明 | 作为binding_kind=declare_complete追加，保存ledger_revision、binding_head_revision、observation_revision及集合hash |
| `portfolio_study_operation` | `(owner_id,operation_id)` PK, action, request_hash, response_payload, created_at；请求幂等台账 |
| 既有`investor_calculation` | S1 method=`portfolio_stress`；S2 method=`decision_fact_check`；revision=1，每次新run新id |

不再建立本方向outbox或独立通知队列；共享`research_domain_event`由02方向IN-01提供。
pipeline_run沿用kind最长32约束：新增`portfolio_stress`、`decision_fact_check`两个kind。
run.request_json只放owner/object/revision/permission_epoch/result_id/input_hash，不存凭证或客户端任意grant。
运行态从pipeline_run投影，结果通过预分配result_id读取；不把运行状态写回不可变revision payload。
S1唯一输入hash含snapshot/valuation/原计算/方案选项/mandate/价格/行业副本/冲击/数值规则版本。
version_refs包含精确ObjectRef及各payload hash；price引用id+version；行业是独立manifest项而非伪ObjectRef。
S2保存decision/observation/binding集合hash、ledger observed_revision、event id+revision+fact_hash及观察证据manifest。
actor、schema_version、method_version、created_at按CalculationRecord现有约定；逻辑版本与DB迁移号分开。

## 6. 新增API与JSON示例

新增路由使用现有`verify_api_key_or_session`，Cookie写请求继续受同源检查；不接受owner或actor覆盖。
下列UUID为示例；`*_ref`均为完整引用，示例`shocks`是一个冲击内的分层覆盖。

```json
{
  "operation_id":"11111111-1111-4111-8111-111111111111",
  "account_id":"22222222-2222-4222-8222-222222222222",
  "expected_account_revision":12,
  "snapshot_ref":{"kind":"portfolio_snapshot","id":"33333333-3333-4333-8333-333333333333","revision":12},
  "valuation_ref":{"kind":"portfolio_valuation","id":"44444444-4444-4444-8444-444444444444","revision":1},
  "alternatives":[],
  "shocks":[{"name":"市场与银行压力","global":"-0.20","industries":[{"system":"csrc","code":"J66","shock":"-0.30"}],"listings":[]}],
  "thresholds":{"maximum_loss_cny":"100000","maximum_loss_ratio":"0.25"}
}
```

`POST /portfolio/stress-studies`：事务冻结+入队，201返回study_ref/run_id/result_id/input_hash。
`POST /portfolio/stress-studies/{id}/revisions`：含expected_revision，选择新基准后建新revision，201。
alternatives元素为`{calculation_ref,scenario_index,plan_ref?,option_id?}`；至多3个且全部同基准。
`GET /portfolio/stress-studies?account_id=...&after=...&limit=...`：keyset分页，limit<=100。
`GET /portfolio/stress-studies/{id}?revision=N`：200返回快照/运行态/结果，不在GET中重算。
`POST /portfolio/stress-studies/{id}/cancel`：operation_id+expected_control_revision，200且重复请求原样返回。
`POST /investor/decision-observations`：引用原决定与可选stress_ref，登记predicate/deadline/授权，201。
`POST /investor/decision-observations/{id}/revisions`：配置CAS；截止后禁止改原predicate，只能新增观察。
`POST .../{id}/bindings`：显式绑定/撤销关联；`POST .../{id}/checks`：到期后人工触发本地核对，202。
`POST .../{id}/cancel`：停止后续自动核对；`GET .../{id}`与`GET .../{id}/checks`：保留历史结果。

```json
{
  "state":"partial","study_ref":{"kind":"portfolio_stress","id":"55555555-5555-4555-8555-555555555555","revision":1},
  "run_id":81,"calculation_ref":{"kind":"calculation","id":"66666666-6666-4666-8666-666666666666","revision":1},
  "results":{"known_securities_loss":{"value":"24000","unit":"CNY","currency":"CNY"},"total_loss":{"value":null,"unit":"CNY","currency":"CNY","unknown_reason":"cash_missing"}},
  "quality":["cash_missing"],"version_refs":[],"current_basis_changed":false
}
```

以上为领域read-model响应，不是CalculationRecord完整序列化；正式响应version_refs必须非空且可解析。
401未认证；404对象不存在/跨owner；409 CAS/幂等冲突/数据基准变动；422引用不一致/参数越界/版本不支持。
错误体沿用`api/deps.py`与BusinessError的HTTP detail包裹：`{"detail":{"code":"...","message":"...","field_errors":[],"retryable":false,"current_refs":[]}}`。版本差异在field_errors的`{field,message}`元素或current_refs表达，刷新使用当前页面已知对象地址；不得回显敏感原始payload。
新增code必须显式登记INVESTOR_ERROR_STATUS及状态测试，不能依赖所有未登记错误默认409。
开关关闭的新增写接口返回503 feature_disabled；已有对象读取仍可用，未知引用仍404。
operation_id作用域为owner+operation_id，action进入request_hash；同key异action也冲突，同参返回原响应。
先认证及校验对象访问范围，再在当前CAS之前查台账：成功请求重试时即使head已变化，也必须重放原结果。
新请求CAS比较配置head/control head及expected_account_revision；校验、输入冻结、入队在同一事务。
当前账户已变化时显式历史重放需`basis_mode=historical`并明确旧snapshot；默认current模式409。
hash统一Decimal规范化、UTC时间、排序键、UTF-8 compact JSON；不同有效数值表达不得改变语义hash。

S2登记完整示例：以下日期/引用来自验收fixture，交易日与not_before均须由实际冻结日历推导。
`predicates`为按`type`区分的异构联合，id在观察内唯一；首次expected_revision/control_revision=0表示create-only。

```json
{
  "operation_id":"77777777-7777-4777-8777-777777777777",
  "observation_id":"88888888-8888-4888-8888-888888888888",
  "account_id":"22222222-2222-4222-8222-222222222222",
  "expected_revision":0,"expected_control_revision":0,"expected_account_revision":12,"expected_binding_head_revision":0,
  "decision_ref":{"kind":"decision","id":"99999999-9999-4999-8999-999999999999","revision":1},
  "stress_ref":{"kind":"portfolio_stress","id":"55555555-5555-4555-8555-555555555555","revision":1},
  "predicates":[
    {"id":"sold","type":"linked_trade_quantity","listing_ref":{"kind":"listing","id":1,"revision":1},"direction":"sell","operator":"ge","threshold":"100","unit":"shares"},
    {"id":"profit","type":"document_metric_compare","listing_ref":{"kind":"listing","id":1,"revision":1},"metric":"net_profit_parent","report_period":"2026-09-30","unit":"CNY","currency":"CNY","operator":"gt","threshold":"1000000","timing_mode":"subsequent_confirmation"}
  ],
  "deadline":{"type":"fixed_date","date":"2026-11-12","timezone":"Asia/Shanghai","non_trading_day":"next_trading_day"},
  "authorization":{"local_auto_check":true,"expires_at":"2026-11-20T15:59:59.999999Z","scope":"registered_predicates_local_only"},
  "note":"核对减持事实与指定报告期利润；不计算收益率"
}
```

201响应中的input_hash由服务端完整规范化请求与冻结引用计算；下例hash仅是fixture占位。

```json
{
  "observation_ref":{"kind":"decision_observation","id":"88888888-8888-4888-8888-888888888888","revision":1},
  "control_revision":1,"permission_epoch":1,"binding_head_revision":0,"state":"scheduled",
  "registered_at":"2026-11-07T04:00:00Z","registered_after_decision":true,
  "due_at":"2026-11-12T15:59:59.999999Z","not_before":"2026-11-13T12:00:00Z",
  "calendar":{"id":"fixture-a-share-2026","content_hash":"fixture-calendar-sha256"},
  "authorization":{"local_auto_check":true,"expires_at":"2026-11-20T15:59:59.999999Z"},
  "input_hash":"fixture-input-sha256","bindings_completeness":"unconfirmed","predicate_ids":["sold","profit"]
}
```

binding动作也是按action区分的联合；创建与撤销均须三重CAS：观察control、账户ledger、账户binding head。

```json
{
  "operation_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","action":"allocate",
  "account_id":"22222222-2222-4222-8222-222222222222",
  "expected_control_revision":1,"expected_account_revision":12,"expected_binding_head_revision":0,
  "event_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","event_revision":12,"fact_hash":"fixture-fact-sha256",
  "allocation_quantity":"100","reason":"这笔已确认卖出对应原决定"
}
```

allocate必须恰有allocation_quantity或allocation_amount，量取正值；金额币种固定CNY，现金方向取源事件符号。
200返回binding_id、control_revision=2、binding_head_revision=1、bindings_completeness=unconfirmed及原事件引用。
revoke用同样CAS字段加`binding_id,reason`，无分配量；declare_complete加`predicate_ids,confirmed_complete:true,scope_end_at=due_at`。
declare_complete只能到期后提交，冻结当前集合与两个账户revision；后续新增/撤销或账本变化使其自动失效，见下节。

## 7. S2观察谓词、事件绑定与迟到事实

S2首期只支持下列type联合；自由文本只作备注，不作为可执行表达式、SQL、Python或模型指令。

| predicate | 参数与输出 |
|---|---|
| `linked_trade_quantity` | 证券+buy/sell+比较符+份额；只汇总用户绑定且有效的真实trade，不含建议数量 |
| `linked_cash_transfer_amount` | 明确in/out方向+比较符+CNY；仅cash_transfer，不混入dividend/校正 |
| `confirmed_holding_quantity` | 用户明确确认覆盖到due_at的历史snapshot份额，覆盖不足为unknown |
| `document_metric_compare` | 明确证券/指标白名单/报告期/单位/比较符/阈值/容差，读取既有已核验证据 |

比较符枚举`lt/le/eq/ge/gt`；eq使用绝对容差，其他比较不暗加容差；缺单位/币种/期次拒绝登记。
`eq`必须带absolute_tolerance>=0；其他operator禁止该字段。现金类型需direction/threshold/CNY；持仓类型需snapshot口径与shares。
document_metric首期仅选现有证据中可精确读取的revenue_total/net_profit_parent；不承诺自动联网补齐。
保存source-field resolver版本与允许的evidence verification；非verified、冲突或重述待核均unknown。
不支持价格涨幅、行业指数收益、全账户收益或“是否跑赢”；这些需要公司行动/资金流独立设计。
登记必须发生在其due_at前；允许晚于原决定，但显示registered_after_decision，不冒充决定前预登记。
deadline支持上海本地固定日期或“登记日后N个A股交易日”，N在1..250；不按原决定日偷偷倒推。
deadline联合另一分支为`{type:trading_days_after_registration,count:N,timezone:Asia/Shanghai}`；不接受客户端computed_due_at。
固定日期遇非交易日顺延到下一已核验交易日；截止为该日上海23:59:59.999999，存UTC。
冻结calendar id/version/hash及推导日期；日历覆盖未知时422 calendar_unavailable，不能按工作日猜。
到期自动核对not_before为下一已核验交易日20:00上海时间，允许正常晚间采集；不改变due_at。
真实事件窗口是`(registered_at,due_at]`；更早事件只作背景。财务指标按指定经济报告期，不要求其期初晚于登记。
财务report_period必须<=上海due日期；首次报告固定observed_at/knowledge_cutoff，获取时间不得越过该cutoff。
document_metric必须显式选timing_mode；`as_known_at_deadline`要求published_at/available_from/acquired_at都<=due_at。
该模式下due后公开或系统迟采的资料不能用于首次延迟核对，也不能通过补充核对把原unknown改成当时已知。
`subsequent_confirmation`核对经济期次<=due的事实，允许首次延迟核对使用due后公开且cutoff前取得的资料。
后一模式一律显示“事后事实确认”；published_at或acquired_at>due则late_data=true，并保存公开/获取延迟。
同一资料可在不同模式得到不同覆盖；所有结果保存timing_mode，迟到新报告不回写旧报告或原决定依据。
bound trade/cash使用事件occurred_at筛窗口、recorded_at筛知识截止；两种时间都展示。
不能因没有绑定就判“未执行”；用户需显式确认窗口内事件绑定集合完整，否则数量/现金谓词unknown。
完整声明亦是user_report；零笔事件的0来自用户声明，页面不能称券商验证。持仓核对不得拿当前snapshot冒充历史截止。
绑定请求包含account_id、event_id、event_revision、fact_hash、allocation_quantity或allocation_amount及用户说明。
事件必须属于同账户、类型匹配；分配不超过该事件有效数量/金额，跨决定分配总量也不得超配。
绑定service使用专用短事务，第一条业务SQL为`BEGIN IMMEDIATE`；获得SQLite writer锁后再读台账/CAS/源事件和有效分配。
在锁内比较ledger revision及binding head revision，汇总该事件跨所有决定/观察的未撤销分配，再验证剩余量。
插入绑定/撤销记录并推进账户binding head、观察control revision、幂等收据同事务提交；任何失败整笔回滚。
两个观察同时消耗同一剩余额度时，第二请求等锁后看见新revision并409；不能各自先SELECT余量后独立INSERT。
索引新增`(owner_id,account_id,event_id)`，event_id/account_id是binding实体列；撤销链查询保留目标binding_id索引。
declare_complete也持同锁，保存ledger revision+本操作推进后的binding head revision+观察revision+有效allocate集合hash及predicate scope。
核对时重算该指纹；新增绑定/撤销/任何账本追加更正都会失配，完整声明立即失效，不等后台通知修正。
账户级指纹对其他观察的绑定变动也保守失效；UI展示重新确认，旧报告只标basis_changed并保留原声明副本。
绑定更正只能追加撤销记录+新分配；不原地编辑。并发账本更正受同一SQLite写锁排序，再由ledger CAS校验。
账本reversal不删除旧绑定；核对时解析原事件的有效性，已撤销标reversed并停止计入。
replacement不自动继承决定关联；用户明确绑定后才计入；报告同时显示原事件、更正链与新关联。
已有报告不覆盖；新绑定、账本更正或迟到证据只能创建新check calculation，带previous_check_ref。
自动首次核对后最多在5个交易日内重试缺数据3次；稳定input_digest不创建重复报告或重复通知。
窗口后仍缺数据保持unknown并结束自动重试；用户可手动创建“补充核对”，明确late_data标签与新cutoff。
没有自动模型调用；事实核对结果pass/fail/unknown独立于研究方法、建议当前有效性与盈亏。

## 8. 队列状态、授权、取消和共享事件

S1领域态：queued → running → complete/partial/failed；queued/running可进入cancel_requested → cancelled。
S2领域态：scheduled → queued → running → checked/partial；无数据重试回waiting_data，最终可closed_unknown。
配置修改旧revision不再自动调度；已有报告只读；cancelled不等于删除或撤销原决定。
pipeline_run仍只使用queued/running/success/partial/failed，不给全系统新增cancelled终态。
领域取消由handler将run标failed并写metadata.reason=cancelled，页面映射cancelled且禁止自动重试。
提交、claim和结果提交前检查feature、owner账户、enabled、permission_epoch与引用仍存在；不复用Agent grant。
S1用户提交授权仅覆盖该冻结本地计算；S2自动核对需显式local_auto_check授权，保存scope/期限/epoch。
S2 scope仅限决定、账户、登记指标和本地已存在证据；授权不隐含采集、付费模型、外部通知或交易。
取消CAS推进permission_epoch；结果提交条件更新head/control epoch，旧执行器失配不得发布结果或事件。
所有handler使用run_with_heartbeat与lease_scope；恢复重用result_id/input_hash，不能重新选当前行情。
commit前同事务写SavedCalculation与run终态；接入IN-01时共享事件也必须同事务，禁止finally后补通知。
崩溃后重试先查结果与hash，幂等补齐而非重复计算；S1未接事件时仍能独立交付本地结果。
DB瞬时错误沿用有限重试；输入缺失是partial非异常；授权撤销/取消/非法引用是终止，不消耗外部额度。
S1每请求最多50只证券×4路径×20冲击，预算本地5秒；S2单run最多10谓词/200关联事件，本地10秒。
超界请求422；超时failed local_budget_exceeded，可人工拆分重试；不扩大现有Agent预算或iFinD预算。
due dispatcher每分钟批量取50条、全局待执行上限200；只入队；开关关闭停止新调度，已有读取不受影响。

事件依赖方向02的IN-01：`research_domain_event`、consumer_receipt/projection_cursor；可先于收件箱UI交付。
共享信封为`event_id,event_type,owner_id,source_ref,source_revision,occurred_at,recorded_at,schema_version,payload_hash,payload`。
source_ref若含revision必须与source_revision一致；唯一源键`(owner,type,source identity,revision)`；内部sequence排序。
payload_hash只对规范化payload计算；recorded_at与事件id不进入payload_hash，生产者不能伪造消费者游标。
S1发`portfolio_stress.completed|partial`，source_ref为结果calculation、revision=1，occurred_at为结果完成时间。
S2发`decision_observation.due`，source_ref为观察revision，occurred_at=due_at；取消后禁止新due事件。
S2发`decision_fact_check.completed|partial`，source_ref为各次核对calculation，payload含observation/decision/previous_check。
更正发`decision_fact_check.superseded`，source_ref为新核对calculation，payload引用旧结果，不修改旧事件。
每个信封payload包含subject={kind:"account",account_id:UUID}、相关version_refs、quality、safe_summary、detail_url；subject必须与源结果的owner及账户一致，无敏感账本全文或用户备注。
收件箱适配器输出方向02定义的InboxContribution.v1：owner_id、该account subject、event_ref、exact_source_refs、materiality、reason_code、evidence_status、display_text；压力结果默认context，决定到期及待查看结果按对应attention规则处理。
相同结果重试共享唯一源键重放；事件重复投递由consumer_receipt去重，顺序依sequence不依occurred_at。
未交付IN-01时不启用S2；S1可正式交付本地压力试验，事件适配器关闭，不能宣称已接通收件箱。
后来接入IN-01仅对新结果发事件；历史补入须按README有界manifest执行，不能用内存重放队列代替持久基础。

## 9. 迁移、开关、回退与实施卡

迁移编号在实施时从真实Alembic head分配，基线head为060；不得提前占号或修改历史迁移。
S1新增表、约束和索引及ObjectRef受控扩展；S2可独立追加，旧SavedCalculation payload不回填不改写。
默认`portfolio_stress_enabled=false`、`decision_observation_enabled=false`，后者还要求前者与ledger启用。
设置同时登记于config/registry/runtime模型，沿用revision重载；关停时推进本功能epoch并停止入队。
发布顺序：迁移 → 关闭开关的兼容代码 → S1离线验收 → 人工启用S1 → S2验收后人工启用。
回退先关闭功能、等待/取消在途、保留数据，再回滚兼容代码；不对生产执行drop表型downgrade。
未识别新kind的旧worker应不认领；部署kind列表需显式更新，不能无意交给Agent worker。

| 卡片 | 阶段/建议文件 | 退出条件 |
|---|---|---|
| PS-01 | S1 `portfolio/stress_contracts.py`、common.py、设计样例 | 契约/精度/partial规则冻结，既有ObjectRef兼容测试通过 |
| PS-02 | S1 `storage/models/portfolio_stress.py`、新Alembic、`portfolio/stress_service.py` | CAS/请求幂等/输入冻结单事务；不可变与owner边界验证 |
| PS-03 | S1 `portfolio/stress.py`、`stress_inputs.py`、calculations.py只读适配 | 全局/行业/逐股覆盖、极端值、AG22-v2同基准适配通过 |
| PS-04 | S1 `portfolio/stress_jobs.py`、worker注册、API `portfolio_stress.py` | 租约/取消/恢复/预算通过；事件为可选adapter，接入时验证原子性 |
| PS-05 | S1 web模板与static模块、API schema | 390/1440宽流程完整，partial/空错/历史标签无歧义 |
| PS-06 | S2 `investor/decision_observations.py`、controls精确读取、观察模型/迁移 | 决定引用、谓词白名单、事件分配/更正/CAS/登记时区通过 |
| PS-07 | S2 `portfolio/fact_check.py`、jobs、scheduler入口、IN-01 adapter | 到期/迟到/无数据重试/授权撤销/事件去重与版本报告通过 |
| PS-08 | 分阶段tests与operations文档、开关配置 | 下列矩阵通过；无原业务回归；完成启停/恢复演练并提交实施验收 |

## 10. 验收矩阵、投入与风险

| 编号 | 场景 | 退出证据 |
|---|---|---|
| A01 | 同请求重试、同key异body | 同run/result；异body409，零重复记录 |
| A02 | 修改head后旧请求重试 | 成功台账先重放；新operation旧CAS409 |
| A03 | 账户/估值/计算/plan错配或跨owner | 404/422且无入队、无源数据泄露 |
| A04 | 未来/晚捕获价、陈旧价、AG22-v1 | 拒绝或partial；无静默当前价替换 |
| A05 | 行业有效边界/多解/源行后改 | 正确半开区间；旧结果hash不变 |
| A06 | 分层冲击、重复目标、未命中 | 最细覆盖；重复422；未命中0明确呈现 |
| A07 | -100%、上界、超界、NaN/float/超指数 | 边界可算；非法422且无巨量精度分配 |
| A08 | 缺现金/价格/数量、零持仓、partial账户 | 已知小计准确；未知总额/比例不冒充0 |
| A09 | 高精度拆股数量、费用、舍入、V_s=0 | 费用不重扣；Decimal可复算；无除零 |
| A10 | 停牌、涨跌停、历史旧方案 | 假设估值标记，不出现可成交承诺 |
| A11 | 取消与结果提交并发、租约失效 | 最多一方提交；旧epoch/attempt不能发布 |
| A12 | commit前后崩溃、事件重复投递 | 结果与事件同事务；共享源键/receipt去重 |
| A13 | 假日、未知日历、上海/UTC边界 | 正确截止/入队；未知日历不猜测 |
| A14 | 无绑定/部分绑定/跨账户/超配事件 | 执行未知；超配与错配拒绝，不推断成交 |
| A15 | reversal+replacement、撤销绑定 | 旧报告保留；原事件不再有效，新事件需明确关联 |
| A16 | 迟到/重述/非verified证据、同hash重试 | 新报告带新cutoff；冲突unknown；不刷重复通知 |
| A17 | 自动授权到期/关开关/排队容量满 | 停止新工作、旧报告可读、明确待处理原因 |
| A18 | 无资金流完整链或用户要反事实收益 | 界面与API无收益率路径，不产生隐含成绩评分 |
| A19 | 两观察同时分配最后100股；complete后绑定/更正 | 仅一笔成功；第二409；完整声明随指纹变化失效 |
| A20 | due后公开的同一报告按两种timing_mode核对 | 当时可知模式unknown；事后确认可用且late_data显式 |
| A21 | 将stress/fact_check引用送正式publication/审批/AG22 | 原方法白名单拒绝，不因kind已注册或status=ok采信 |

目标：50只×4路径×20冲击本地P95<2秒，队列等待单列；这是待实测目标，不是已有性能。
至少5次真实用户走查能复述最大损失来源、覆盖缺口和“决定≠执行”；不得只验截图或API200。
投入估计：S1约3.5—4.5人周，S2约2.5—3.5人周，共6—8人周；IN-01基础能力不重复计费。
风险一：已有AG22 details不是永久公共接口；PS-03建立版本化适配器与golden样例，未知版本拒绝。
风险二：行业源可变与历史知识边界；用冻结副本而非后读当前行，必要时仅支持全局/逐股冲击。
风险三：决定与真实交易缺天然外键；坚持显式分配/完整确认，缺数据宁可unknown。
风险四：简单静态压力易被当概率和流动性保证；结果页持续显示模型边界，不以“安全/最优”命名。

## 11. 官方调研取舍与实施验证

已先搜索并打开官方文档/仓库，再作取舍；以下内容是设计依据，不代表已安装或试跑这些系统。
- [Ghostfolio](https://github.com/ghostfolio/ghostfolio)：采用静态风险卡与构成展开思路；不复制其交易管理或Angular/NestJS实现；AGPLv3代码不直接搬入。
- [Portfolio Performance](https://help.portfolio-performance.info/en/concepts/performance/time-weighted/)：资金流与业绩需分离；因此本期排除收益率，不把资产变化当回报，不移植桌面/导入系统。
- [skfolio](https://skfolio.org/)：提供风险度量、约束、压力试验和按时间验证，BSD-3-Clause；留作后续可选adapter，本期确定性算术不引入其优化/随机模拟依赖。
- [Riskfolio-Lib](https://riskfolio-lib.readthedocs.io/en/latest/)：借鉴约束表达范围；现阶段不引入广泛风险模型和求解器，避免数据稀疏时输出虚假精度。

主审已决定：csrc源不完备时S1先交全局/逐股；行业未知逐结果降级。PS-03仍需真实AG22-v2样例验证适配覆盖。
实施验证：按IN-01共享事件API与schema联调源键和事务边界；本方向不自建第二套outbox兜底。
实施验证：S2财务证据resolver覆盖与两种timing_mode样例；绑定SQLite互斥、完整声明失效需PS-06并发小样验证。
