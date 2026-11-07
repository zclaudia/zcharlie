# 银行财报专题工作台：净息差下降时净利息收入为何仍增长

基线 `1f62fa4`；日期 2026-11-07；状态：设计主审完成，待实施与验收。本文只定义待实施方案。
遵守[共享工程及事件约定](README.md)；首期为招商银行 FY2025 对比 FY2024，集团、人民币、中国会计准则年度口径。
产品问题固定为“净息差下降时净利息收入为何仍增长，支撑能否持续”，不扩展为银行投资评级或估值推荐。

## 1. 交付边界与可见结果

用户从股票页进入一个 TopicCase，依次核对事实、官方量价分解、竞争解释与反证，再保存下一报告的检查条件。
首页应直接回答：增长是已披露的历史事实；规模贡献抵消利率拖累是披露口径下的解释；能否持续仍有待下一可比报告验证。
不新增银行同行表。现有年度矩阵、原件、scope bridge、字段复核与版本是输入；专题只保存问题结构及精确引用。

| 阶段 | 范围 | 退出条件 |
|---|---|---|
| MVP-A，静态专题 | 固定招行年度案例、事实原页、披露分解、人工整理解释与反证、条件草稿、历史版本 | 不调用模型也能完整核对本期变化；错误与缺口可见 |
| MVP-B，条件检查 | 有限 DSL、到期/新资料检查、只读本地材料、不可变检查结果、IN-01 事件适配 | 重复、迟到、更正、取消、崩溃恢复均可追溯 |
| 后续 | 第二家银行、半年度独立模板、可选 Docling 候选定位、经预算授权的解释草稿 | 各自有原件金样本和可比规则后单独启用 |

MVP-A 不依赖 IN-01；MVP-B 必须等待 IN-01 持久事件账本交付并通过恢复测试。
不自动下载新年报、不付费研究、不运行任意脚本、不触发交易；“补充研究”沿用现有显式授权入口。
衡量目标：核对 5 个关键事实能回原页，分解残差明确，用户能说出一项尚未验证的持续性条件；这些尚未实测。

## 2. 当前代码地图与复用限制

下列均为基线已有路径，行号供实施定位；新增路径在任务卡中另列。

| 已有位置 | 现状与增量接缝 |
|---|---|
| `src/investment_agent/industry_peers/contracts.py:98` | PeerAnnualField 已保留 raw_value/raw_unit、期间、Decimal、取得时间、核验状态；专题不得另造 float 财务字段 |
| `src/investment_agent/storage/models/peer.py:96` | PeerAnnualSnapshot 依内容/语义身份追加版本；引用固定快照 UUID、revision=1，不能把“最新”存为证据 |
| `src/investment_agent/industry_peers/bank_annual_matrix.py:80` | 已有审核年度包导入；复用年度事实，不重新建设导入/同行矩阵 |
| `src/investment_agent/industry_peers/bank_annual_v2.py:136` | 当前 v2 校验行列、表头、口径、单位；新 NII/分解行需要专题规则，不假定 v2 已支持全部新指标 |
| `src/investment_agent/industry_peers/bank_annual_v2.py:266` | 回读 owner/listing、原件、blob/hash；专题定位必须有同等级回读 |
| `src/investment_agent/industry_peers/originals.py:351` | 原件验证按规则版本分派；既有字段继续使用原规则 |
| `src/investment_agent/industry_peers/bank_scope_bridges.py:283` | 已有宁波 NIM bridge；规则银行/期间特定，不能套到招行或任意新年度 |
| `src/investment_agent/industry_peers/evidence.py:87` | source ref 固定快照字段路径；复用精确事实引用，避免复制后丢失更正关系 |
| `src/investment_agent/industry_peers/evidence.py:354` | 字段版本冲突/待复核状态应传入专题，不把 candidate 提升为 verified |
| `src/investment_agent/industry_peers/bank_annual_review.py:379` | 官方修订接受需新版原件与正式修订依据；专题更正沿用此要求 |
| `src/investment_agent/storage/models/disclosure.py:25` | owner-scoped 原件按 hash 不可变；复用 bytes 与下载，不存第二套 PDF |
| `src/investment_agent/evidence/contracts.py:27` | Period、DocumentProvenance 已区分期间、原件 hash、集团口径；保留 schema 语义 |
| `src/investment_agent/investor/common.py:23` | DecimalValue JSON 为字符串；ObjectRef 在 :47 是封闭 Literal，当前没有 topic 类型 |
| `src/investment_agent/api/auth.py:171` | 复用 API Key/会话校验及会话写来源检查；领域服务仍检查 owner/引用 |
| `src/investment_agent/jobs/base.py:25` | ClaimedJob、lease、partial 合约；新增 handler 不另建队列 |
| `src/investment_agent/storage/repositories/pipeline_run.py:27` | LeaseLostError/RunLease fencing；结果提交必须仍持有效 lease |
| `src/investment_agent/research/worker.py:256` | default_job_handlers 与 :295 分工；新检查为本地 collection handler，维护派发归单例 |
| `src/investment_agent/web/templates/stock.html:309` | 股票分区及 :338 同行区；加“财报专题”链接，同行区内容继续独立 |
| `src/investment_agent/web/static/industry_peers.js:535` | 可借证据 chip 交互，数据仍由服务端算；抽取公共只读证据组件时避免改其口径 |
| `src/investment_agent/api/app.py:272`、`web/routes.py:647` | 按现有路由注册/模板渲染方式加入口 |
| `src/investment_agent/storage/database.py:151` | WAL/busy_timeout 已配置；未启用 SQLite FK，引用完整性靠服务校验与必要唯一/检查约束 |

新增 ObjectRef kinds 限 `topic_case`、`topic_revision`、`topic_condition`、`topic_check`，使用 UUID；专题子字段以精确父 revision+稳定字段 ID 定位。
共享IN-01的EventSourceRef白名单仅增本事件所需的topic_check，source_ref为精确UUID/revision=1并与source_revision一致；不把topic任意子对象开放成事件生产者。
在新 `bank_topics/references.py` 做 owner、kind、revision、field_path、listing 校验与回读；不把任意 JSON 当作 ObjectRef。
专题 resolver 仅供专题和收件箱白名单调用；正式 publication 的现有证据适配/可采信白名单保持封闭。
要在正式研究中引用专题，必须解析到底层 evidence/disclosure/calculation 并经过既有发布检查；解释文字与检查通过不自动获得事实资格。

## 3. 样本事实与原件边界

官方依据为[招行 2025 年度报告](https://s3gw.cmbimg.com/lb5001-cmbweb-prd-1255000097/cmbir/20260327/e1efc81f-7ad9-41b7-9b02-5db202026930.pdf)，印刷 21/PDF 第22页与印刷22/PDF第23页。PDF 页码采用从1计数，另存印刷页标签。
本轮复用了已取得原件 `research/industry-data-validation/evidence/run-02/raw/official/600036.SH_2025.pdf`；实施时须审核 manifest/hash 后正式导入，不能把研究目录路径当运行时引用。
FY2024 比较值优先固定 FY2025 报告的同表比较列；如另引 FY2024 年报，必须标明另一 original ref 与是否重述，禁止无声混用。

| 事实 ID | FY2025 / FY2024，原文单位 | 标准化及定位 |
|---|---|---|
| `nii` | 215593 / 211277，人民币百万元 | CNY 215593000000 / 211277000000；集团年度，印刷21 |
| `nim` | 1.87 / 1.98，% | fraction 0.0187 / 0.0198；年度净利息收益率，印刷21 |
| `nii_volume_effect` | 16798，人民币百万元 | CNY 16798000000；FY2025 对 FY2024 变动，印刷22 |
| `nii_rate_effect` | -12482，人民币百万元 | CNY -12482000000；同上 |
| `nii_change_disclosed` | 4316，人民币百万元 | CNY 4316000000；同上 |

以上数值与规模/利率共同变化计入规模的规则均来自同一官方表，不能解释成不含交互项的纯数量效应。[原报告](https://s3gw.cmbimg.com/lb5001-cmbweb-prd-1255000097/cmbir/20260327/e1efc81f-7ad9-41b7-9b02-5db202026930.pdf)
新事实必须保存 issuer/listing、CAS、consolidated、CNY、期间 start/end/basis、比较期间、原文行列标题与脚注；“本行”值不得混入集团计算。
定位最小集合为 original_ref、pdf_sha256、pdf_page、printed_page、表标题/行列路径、excerpt/hash、规则版本、checked_at、review_actor。
bbox 为可空辅助字段，包含坐标系、页尺寸及旋转；有 bbox 不代表值已核验，无 bbox 仍可按原页与唯一行列定位核对。
解析输出先是 candidate；原页、列、单位、期间、口径全部通过可信规则或人工审核才为 verified；截图只能帮助核验，不能替代 bytes/hash。
保留 original published_at、公开日期精度、acquired_at、checked_at；未知时刻保留 date precision，不伪造凌晨精确时间。
可用性策略为公开时间上界及系统取得时间均不晚于检查 as_of；只有日期时，公开上界取该日 Asia/Shanghai 结束，未到上界不用于自动检查。
公开时间用于资料先后，取得时间用于“系统当时知道什么”，期间用于财务比较，三者不能互换。
正式更正保存新原件/新事实/新 TopicRevision，显式 supersedes 与更正依据；旧页面和旧检查继续读旧引用，head 显示“有更正”。
抓取失败、URL 失效、官方撤回和正式更正分别建状态；无可信撤回记录只标 source_unavailable，不能自行宣告撤回。

## 4. 页面及完整用户流程

1. 股票页“财报专题”列出固定问题、集团口径、比较年度、事实核验和检查状态；无案例时显示“尚未建立专题”和创建入口。
2. 创建向导只允许首期招行/CAS/集团/FY2025，展示已存材料及缺口；确认创建只做本地引用绑定，缺材料显示导入/复核待办。
3. 详情顶部显示问题、结论边界、版本、材料截至时间、与最新版差异；用户可切换历史，历史页禁用编辑。
4. 事实区两期数值并排，明确百万元/亿元切换只是显示；每格点开原页、行列、单位、集团表头及脚注。
5. 分解区展示 NII 变动瀑布及等值数据表；“披露分解”徽标常驻，鼠标/键盘可访问每一项来源。
6. 解释区分“原报告陈述”“恒等关系”“分析假设”“待验证”，支持与反证分别列出，缺反证显示未完成。
7. 条件区先预览依赖、下一可比报告、到期日期和未知时处理，再明确保存/启用；保存草稿不启用检查。
8. 结果区显示本次观察值、阈值、材料时间、条件版本和逐项 outcome；有更正时并排查看旧、新结果。
9. 用户可暂停/取消条件或新建版本；收件箱确认仅改变阅读状态，不改变专题事实、解释审核或条件结果。

| 页面状态 | 呈现与允许动作 |
|---|---|
| loading / 空 | 骨架屏；无事实时保留问题与材料清单，不显示零/绿色分解 |
| candidate / pending_review | 原值与待复核标识；可看来源、保存草稿，不生成已核验结论或自动条件 |
| partial | 已核验部分可读，缺项逐条 reason；瀑布缺项留空，不能把剩余项补成“其他” |
| failed / source_unavailable | 明确失败阶段、重试条件和原有版本；重试复用 mutation ID，新增材料后另建版本 |
| scope_conflict / stale | 并列冲突及精确来源，冻结依赖条件的确定性判断，提供复核入口 |
| revision_conflict | 保留本地草稿，载入新 head 差异，用户刷新后重新提交 |
| unauthorized / archived | 按现有访问处理；归档停止新动作，历史按当前权限可读 |

客户端不得把缺值格式化为0；Decimal 展示采用字符串安全格式；窄屏以事实卡+数据表承载，不依赖图表传达信息。
原文及解释都做文本转义，禁止来源 HTML 注入；来源下载沿用授权路由，不在页面暴露文件系统路径。

## 5. 数据模型、索引与不可变引用

不复制年度矩阵。下表为新增领域表；payload 均有 schema_version 与 canonical hash，精确引用可回读。

| 表 | 字段与约束 | 索引/写策略 |
|---|---|---|
| `topic_case` | id UUID PK、owner_id、listing_id、template_key、base_period日期、head_revision≥1、control_revision≥1、state、created_at | UNIQUE(owner_id,listing_id,template_key,base_period)；(owner_id,state)；CAS 更新 head/control |
| `topic_revision` | id UUID PK、case_id、revision、schema_version、question、period/baseline、accounting/scope/currency、as_of、fact_bindings、decomposition、explanations、condition_refs、hash、supersedes_ref、created_at/actor | UNIQUE(case_id,revision)；(case_id,created_at)；只追加 |
| `topic_fact_revision` | id UUID PK、owner_id、case_id、stable_fact_id、revision、metric、value字符串/null、unit、raw_value/unit、period/comparison、scope、source_refs、proof、verification、reason、published/acquired/checked时间 | UNIQUE(case_id,stable_fact_id,revision)；(case_id,metric,revision)；只追加 |
| `topic_condition_revision` | condition_id UUID、revision、owner_id、case_id、topic_revision_ref、dsl_version、predicate、dependency_manifest、target_selector、review_due_at、expires_at、timezone、activation_basis、hash | PK(condition_id,revision)；只追加；依赖绑定创建时冻结 |
| `topic_condition_head` | condition_id、owner_id、case_id、head_revision、control_revision、state、next_due_at、active_run_id、last_check_ref | (owner_id,state,next_due_at)；head/control CAS；运行状态不覆盖条件内容 |
| `topic_check_revision` | id UUID PK、owner_id、condition_id、condition_revision、condition_ref、evaluator_version、execution_manifest JSON、execution_manifest_hash、semantic_input_hash、as_of、trigger、result/unknown_reasons、prior_check_ref、supersedes_ref、created_at | UNIQUE(owner_id,condition_id,condition_revision,semantic_input_hash,evaluator_version)；只追加 |
| `topic_mutation_receipt` | owner_id、mutation_id、route/object identity、request_hash、response/status、created_at | UNIQUE(owner_id,mutation_id)；与领域结果同事务写；不可变 |

事实表只补充专题新增 NII/分解等字段；现有 peer 字段用 fact_bindings 精确引用，不为已有矩阵每格创建副本。
对外事实引用统一使用 topic_revision 的 `field_path="facts.<stable_fact_id>"`，该版本绑定不可变事实行；fact 行不新增通用 ref kind。
topic_revision 用独立 UUID、revision=1 表示一个不可变版本；其业务 revision 单独记载。topic_case ref 则携带真实 head_revision 并解析相应历史版本。
condition_ref 的 id 为稳定 condition_id、revision 为内容版本；topic_check 每次结果独立 UUID、revision=1，避免一次结果多个 payload 身份冲突。
UPDATE/DELETE 保护覆盖 revision/fact/check/receipt 表，迁移与 create_all 测试均设置；head/control 表允许受控更新。
新建及每次写入检查 owner、listing、父版本存在、引用字段存在、原件 bytes/hash、期间语义；批量引用要有数量上限，拒绝循环引用。
删除案例采用归档；不级联删除证据。当前单用户 OwnerId 固定 local_owner，新增表仍保留 owner，不能只按 UUID 查询。

## 6. API、JSON、幂等与状态

新增 `/bank-topics` router；示例 UUID 为协议示例，实施测试使用真实已存引用。所有 JSON Decimal 都是字符串。

| 方法/路径 | 语义 |
|---|---|
| GET `/stocks/{ticker}/bank-topics` | owner-scoped 列表，空列表200；按 template/period 筛选 |
| POST `/bank-topics` | 创建固定模板并返回 head、缺口；201，重放原201 |
| GET `/bank-topics/{id}?revision=2` | 固定业务版本与当前 head/control 信息；历史不随 head 漂移 |
| POST `/bank-topics/{id}/revisions` | expected_revision CAS，追加事实/解释绑定；201 |
| POST `/bank-topics/{id}/conditions` | 保存草稿或显式启用条件，CAS case head；201 |
| POST `/bank-topics/{id}/conditions/{condition_id}/checks` | 本地确定性检查，冻结输入后入 pipeline_run；202，返回 run ref |
| POST `/bank-topics/{id}/conditions/{condition_id}/control` | pause/resume/cancel，expected_control_revision；200 |
| GET `/bank-topics/{id}/checks/{check_id}` | 固定结果、输入 manifest、前次差异及未知原因 |

创建请求（listing 用 ticker 由服务端解析，客户端不能伪造 listing_id）：
```json
{"schema_version":1,"idempotency_key":"dc58d9a7-918c-4e86-8a06-544b255b817e","template_key":"bank_nii_sustainability_v1","ticker":"600036.SH","period":{"start":"2025-01-01","end":"2025-12-31","basis":"period_to_date/annual"},"baseline_period":{"start":"2024-01-01","end":"2024-12-31","basis":"period_to_date/annual"},"reporting_scope":"consolidated","accounting_standard":"CAS","currency":"CNY"}
```
创建响应为薄收据，详情另取，缺材料也不伪造引用：
```json
{"schema_version":1,"case_ref":{"kind":"topic_case","id":"3d09f0e8-b175-486d-aa86-5c0b3be4ddf8","revision":1},"head_ref":{"kind":"topic_revision","id":"c29723fd-50cd-4437-82a5-bd6200880a6b","revision":1},"state":"draft","coverage":"partial","gaps":[{"code":"needs_reviewed_nii_decomposition","field_path":"decomposition"}],"control_revision":1}
```
启用条件请求；predicate 有限枚举，threshold 单位与 metric 注册表一致：
```json
{"schema_version":1,"idempotency_key":"0bb9aa09-31ca-45b2-8877-be90bad9ea84","expected_revision":1,"topic_revision_ref":{"kind":"topic_revision","id":"c29723fd-50cd-4437-82a5-bd6200880a6b","revision":1},"activate":true,"dsl_version":"bank-condition/1","predicate":{"kind":"all","conditions":[{"metric":"nii_yoy","operator":"gt","threshold":"0","unit":"fraction"},{"metric":"nii_scale_covers_rate","operator":"eq","threshold":true,"unit":"boolean"}]},"target_selector":"next_comparable_annual","review_due_at":"2027-04-30T15:59:59Z","expires_at":"2027-06-30T15:59:59Z","timezone":"Asia/Shanghai"}
```
该请求只有当绑定材料已核验且依赖完整时成功；不能因为创建收据是 draft/partial 就省略领域校验。日期为用户检查计划，不宣称法定披露期限。
启用门槛中的完整仅指起始FY2025/24材料、口径和指标resolver已具备；目标FY2026尚未出现是正常等待，不阻塞armed。目标报告公开时刻不可知时保存unknown，不伪造截止日期或财年知识；目标财年必须由已核验财年日历确定，不能确定则待人工补充选择规则。
检查结果示例：
```json
{"schema_version":1,"check_ref":{"kind":"topic_check","id":"f4cc63f8-2aa2-4b8e-a79f-cfc33278a7d2","revision":1},"condition_ref":{"kind":"topic_condition","id":"97281ba8-5c2c-453a-82de-51025225b05a","revision":1},"outcome":"insufficient_evidence","coverage":"partial","values":{"nii_yoy":null,"nii_scale_covers_rate":null},"unknown_reasons":[{"metric":"nii_yoy","code":"no_new_comparable_report"}],"as_of":"2027-04-30T16:00:00Z","execution_manifest_hash":"2e5b0e1635c80e728c5405989b62cdcf15b9c60d07ca89cff2eed41ebcc16f67a","semantic_input_hash":"8e6a2f7b49b539938f9c3f0cf17b4b591fe927ab09f26cce39b063bd48a9300e"}
```
错误沿用 BusinessError：401/403 身份/来源，404 对象不存在或不可访问，409 revision_conflict / idempotency_mismatch / source_conflict，422 unsupported_scope / invalid_dsl / unit_mismatch / incomplete_dependencies，503 feature_disabled / event_delivery_unavailable。
复用 `api/deps.py:54` 的 INVESTOR_ERROR_STATUS 与 :86 转换器，新增code显式登记HTTP状态，否则现实现默认409；响应固定为 `{"detail":{"code":"invalid_dsl","message":"条件不受支持","field_errors":[],"retryable":false,"current_refs":[]}}`，不另造error信封。
409 返回 current_refs 与 retryable=false；临时数据库竞争503可重试，但同 mutation ID 不换参数；错误不泄露他人引用或凭证。
mutation_id 使用现有命名 idempotency_key；规范化 hash 包括 API/schema/object/完整 body，去掉该 key 自身，Decimal 先按合约规范化。
先验身份及当前访问权再读重放收据；同 ID 同参返回原收据，同 ID 异参409。首次请求 CAS 与领域写入及收据同事务，失败不留假成功收据。
CAS 采用 `UPDATE ... WHERE head_revision=:expected` 并查 rowcount；两个新修订竞争仅一项成功，失败方刷新后以新 mutation ID 重新决定。
case 状态 draft→active→archived；修订完整度另用 ready/partial/needs_review，不混作生命周期。
condition 内容版本不可变；运行控制 draft→armed↔paused，armed→completed/expired/cancelled；新内容版本不能继承旧 armed 授权而悄悄启用。
启用/改写条件同事务追加包含精确condition_refs的TopicRevision并CAS更新case head；冻结topic_revision_ref仍指启用前审阅的材料版本，不能环形引用自己。
专题运行视图 queued→running→completed/partial/failed/cancelled；cancelled仅为领域展示，不新增pipeline_run状态，落已有failed并存reason=cancelled、retryable=false。outcome只为met/not_met/insufficient_evidence/not_comparable/source_invalidated，不把运行失败记为条件不满足。

## 7. Decimal 算法、容差与解释层级

以 CNY/fraction 标准化，计算使用 Decimal localcontext precision=40；拒绝 float/bool 金额、NaN/Infinity；显示舍入不回写。
`delta_nii = nii_2025 - nii_2024 = 4316000000`，`nii_yoy = delta_nii / nii_2024`，按展示精度为2.04%。
`delta_nim_bp = (nim_2025 - nim_2024) * 10000 = -11`；percentage points 与 bp 不能混用。
`disclosed_total = volume_effect + rate_effect`，`residual = delta_nii - disclosed_total = 0`；主图直接用披露数，不用已四舍五入 NIM 反推规模贡献。
若补充资产/负债行：净规模=资产规模−负债规模，净利率=资产利率−负债利率；利息支出贡献反号后展示，原表负号另保留。
对单项 `I=B*r`，交互归规模时 `volume=(B1-B0)*r1`、`rate=B0*(r1-r0)`；此为规则说明，缺未舍入利率不输出伪精确重建分解。
印刷精度为金额百万元整数，每个金额对应±500000 CNY 的舍入区间；k项恒等式残差允许界为各项半单位之和。
金样本主等式要求残差精确0；其他样本显示 residual、tolerance、rounding_consistent，不把“容差内”说成原文一致或自动verified。
比例印刷0.01%时半单位为0.00005 fraction；禁止用比例展示精度的误差补救集团/母行或不同期间混用。
缺 NII 比较值：不算增长率；前期值0：增长率null/zero_denominator，仍显示绝对变动；负基数则显示绝对变动并标增长率不适用。
缺量价表：只展示 NII/NIM 事实，decomposition=unavailable；缺一个贡献：保留另一已披露数但整条归因partial；不使用平衡数填空。
原件冲突、单位不明、期间不符：相关派生量null并给结构化原因，独立已核验事实仍可读。

解释必须分四层：L1原报告事实与原文陈述；L2确定性恒等/披露分解；L3竞争机制假设；L4前瞻检查条件。
每条解释存稳定 ID、level、statement、support_refs、counter_refs、missing_evidence、review_state、reviewer/time；L3不能只存一个置信分数。
候选解释A为规模扩张抵消利率压力；反证是下一期规模贡献不足/转负，不由本期净增就证明长期可持续。
候选解释B为负债成本下降缓冲资产收益率下降；须分开引用资产收益与负债成本，反证是负债成本下行空间减弱或资产端继续压降。
候选解释C为资产/负债结构迁移；本期总表不足以识别结构因果时标待验证，不能把与NII同步变化的指标写成原因。
“披露贡献”是会计分解，“管理层解释”是有出处的陈述，“分析假设”需要竞争解释和反证；模型草稿永不覆盖这三个标签。
持续性页面只能说满足/未满足预定可观察条件，不输出“已证明持续”或投资买卖结论。

## 8. 有限条件 DSL、到期与材料选择

DSL v1 仅支持一个 all/any 根、1–5个叶子、深度≤2；无代码、表达式字符串、SQL、URL、递归嵌套或任意字段访问。
metric 白名单：nii_yoy、nim_delta_bp、nii_volume_effect、nii_rate_effect、nii_scale_covers_rate；数值操作符 gt/gte/lt/lte，布尔仅eq。
依赖注册表：nii_yoy需同口径本/上年度NII；nim_delta_bp需两期NIM；两项effect需目标报告明确量价表；covers需两effect。rate≥0时叶状态not_applicable、值null/unknown_reason=no_rate_drag，不能说“已覆盖拖累”；rate<0时计算 `volume >= abs(rate)`，volume≤0明确false；缺数仍unknown。

not_applicable只用于叶解释，参与all/any三值求值时映射为unknown；根结果不得因省略此叶而变为met。若其他叶已足以确定all为false或any为true，则按既定三值逻辑保留该结果。
叶值threshold与unit严格匹配，布尔只用JSON true/false；根采用三值逻辑：all有false则not_met，否则有unknown则insufficient；any有true则met，否则有unknown则insufficient。
即使根可判定，缺叶仍报告coverage=partial及unknown；页面不能给“全部已核对”的绿色标记。
本期 FY2025 为冻结基准，selector 仅 next_comparable_annual：同发行人、集团、CAS、CNY、连续12个月且期末晚于基准的最早财年；本例目标FY2026。
不把系统已有2026Q1/H1/前三季累计年化或偷换为下一报告；半年模板须后续独立契约。
目标FY确定后不自动跨到FY2027；目标报告未公开/未取得为 awaiting_source，公开但无所需披露为 insufficient_evidence。
同目标FY多个原件：同 hash去重；仅对正式更正链选择 as_of 前最新合格版本；不明重发不自动胜出，报 source_conflict。
execution_manifest固定目标/比较原件、事实值、topic/condition/evaluator版本与as_of，execution_manifest_hash覆盖全部；入队后新材料不加入本次。semantic_input_hash只覆盖精确refs/values、condition/evaluator版本、目标、可用性状态与到期阶段pre_due/due/expired，不含仅扫描时钟/as_of；唯一键及作业去重均用后者。跨due/expiry各可成一次新语义，阶段内无新资料的每日tick不产生新检查/事件。
review_due_at 与 expires_at 输入必须带时区并统一UTC保存，界面Asia/Shanghai；due用于提醒核对，expires结束自动等待窗口，均由用户计划定义。
到期无新资料：due阶段追加一次 insufficient_evidence 检查并保留 awaiting_source；到expiry阶段可再追加一次不足结果并终止等待，阶段内daily tick无新semantic_input_hash不新增重复结果/通知。
截止前补发材料：新增检查，prior_check_ref链接旧结果；截止后迟到材料只建“迟到可检查”上下文，不重开/改写已过期条件，需显式新版本/手动检查。
补发的更正与原件失效可在过期后追加 superseding check 以更正历史事实，并标 late_correction；不等于重新启用持续监控。
条件被暂停/取消后不派新检查；已运行在提交前重查control_revision，撤销则机械结账cancelled，不发布旧授权结果。
awaiting_source为armed条件的等待原因，不是额外生命周期；目标报告得到可判定met/not_met后completed，缺披露则继续armed等待补充至expires，届时expired并保留不足结果。

## 9. 队列、IN-01 事件与权限边界

新增 bank_topic_check handler，通过既有 pipeline_run 存任务；去重键为owner+condition版本+semantic_input_hash+evaluator_version，活跃作业只复用同一冻结输入。命中已完成语义返回原check及其原execution_manifest，不把新扫描as_of冒充原执行时间。
scheduler 每轮扫描有界到期条件，使用数据库唯一键协调多进程；时间扫描游标不得代替共享事件sequence。
handler不网络请求、不LLM；先校验owner/当前控制和lease，再计算，短事务中存check、更新运行head、写事件、结账作业。
提交丢lease整笔回滚；提交后进程退出由相同唯一键回读check并完成恢复，不能产生第二份结果或只在finalize写事件。取消/撤权/停用导致的机械结账为pipeline_run.failed+reason=cancelled且不重试，不存伪检查outcome或观察事件。
可恢复锁竞争按现有退避重试；缺数据为partial结果不无限重试；毒payload/非法引用failed并进健康视图。
IN-01开启后，领域结果与 `topic.observation_changed` 同事务提交，source_ref=`topic_check` UUID/revision=1，source_revision=1。
信封字段、source唯一性、payload_hash、sequence与消费收据遵守README；一次check仅一事件，变化理由列表放同一payload。
payload限 subject={kind:"listing",listing_id:严格正整数}、topic_ref、condition_ref、check_ref、prior_check_ref、target_period、baseline_period、scope、scope_changed、outcome、changed_reasons、exact_source_refs；subject必须与case及源check一致，不包含任意原文/凭证/模型秘密。
InboxContribution.v1 输出 owner_id、subject={kind:listing,listing_id:严格正整数}、event_ref、exact_source_refs、materiality、reason_code、evidence_status、display_text，另带topic_ref与monitor_condition_ref；与02的受限subject union完全一致。
条件引用包含版本；事实/解释子项按parent ref+stable field_path；没有审核关系时 relation固定unlinked/context，不因met自动标支持论点。
首次 met/not_met、outcome变化、正式更正/口径冲突 materiality=attention；due无材料context一次，expiry结束等待可context一次；同semantic_input_hash重跑无新事件，首个静态专题不假装有观察变化。
收件箱关闭仍写持久事件/机械收据；适配器未交付显示未接入，不能算专题提醒已成功。归档/静音实时检查当前控制并保留事件历史。
当前资料授权只涵盖本地确定性读取；事件、到期、点击已读都不授权抓取、付费研究、外部通知或交易。

## 10. 迁移、开关与回退

Alembic revision由集成人员在实际head之后分配；新增表/索引/immutable触发器，不修改旧peer字段/分解语义，不硬编码下一序号。
配置 bank_topics.enabled=false、bank_topics.condition_checks_enabled=false；IN-01未就绪不允许开启后者；设置注册与运行时快照沿用现有体系。
旧数据只用明确招行FY2025材料manifest一次性导入，校验引用后建草稿，不自动启用条件、不重发历史通知。
关闭专题开关阻止创建/编辑/启用/新检查；已入队取消，运行提交前检查开关，机械结账及有权限历史读取继续。
关闭条件开关保留静态页面；已提交终态check/outbox仍可机械消费，未提交计算取消。回退只切开关，不删证据表或重写旧版本。
降级旧程序前先停新handler并结账活跃run，备份新增表；旧程序不得消费未知kind。破坏性down migration仅离线确认后执行。

## 11. 任务卡与退出条件

| 任务 | 具体文件范围（新增除注明外） | 可验收退出 |
|---|---|---|
| BT-01 合约与引用 | `bank_topics/contracts.py`、`references.py`；修改`investor/common.py` | 所有ref回读owner/type/版本/字段；publication白名单未被扩大 |
| BT-02 持久化/CAS | `storage/models/bank_topics.py`、`storage/repositories/bank_topics.py`、实际head后迁移、模型注册 | immutable直写保护、CAS竞争、幂等异参、重放撤权均验证 |
| BT-03 样本定位与计算 | `bank_topics/facts.py`、`decomposition.py`、`templates.py`、`tests/fixtures/bank_topics/` | 审核PDF22/23页hash与行列，5事实和残差0；缺值/错口径正确降级 |
| BT-04 静态服务与API | `bank_topics/service.py`、`api/routes/bank_topics.py`、修改`api/app.py` | 创建/修订/历史/权限/错误契约完整；无需IN-01即交MVP-A |
| BT-05 页面 | `web/templates/bank_topic.html`、`web/static/bank_topics.js/css`，修改`stock.html`/`web/routes.py` | 完成流程及所有空/错/partial态；键盘、窄屏、原页回读可用 |
| BT-06 DSL与选择器 | `bank_topics/conditions.py`、`reports.py`、`checks.py` | 有限枚举、三值逻辑、年度选择、时区到期/更正/迟到均验证 |
| BT-07 队列与事件 | `bank_topics/jobs.py`、`events.py`，修改`research/worker.py`；依赖IN-01适配注册 | lease崩溃恢复、同事务事件、唯一消费、无付费动作验证 |
| BT-08 启用与验收 | 修改`settings/registry.py`及配置合约；`tests/integration/test_bank_topics_acceptance.py`、操作文档 | 开关回退/有界导入/健康状态通过，金样本人工签核后才启用 |

共享Literal、事件注册、路由/设置注册及Alembic由集成人员串行合并；上述范围为实施分工，不表示本轮修改业务代码。

## 12. 验收矩阵、投入与风险

| 场景 | 预期结果 |
|---|---|
| 1. 招行金样本 | NII两期、NIM两期、净规模/利率贡献回原页；增长2.04%、NIM-11bp、残差0 |
| 2. 母行值混入集团 | 422/scope_conflict，不生成可比结果；原始事实仍可追溯 |
| 3. 百万元误当亿元/百分比二次除100 | 单位校验阻止保存；标准化只执行一次 |
| 4. 对比列被错选本期/季度 | 期间/表头审核失败，不能用最近数字补齐 |
| 5. 缺量价表或缺一个贡献 | 显示partial/unavailable，不反推/补零/误标完整 |
| 6. 原件0值/前期0值 | 0保留，增长率null并明确zero_denominator |
| 7. 伪造owner/不存在ref/错field_path | 服务拒绝；错误不泄露他人源信息 |
| 8. 同mutation重复、异参、双写竞争 | 原收据一次、异参409、CAS仅一个新head |
| 9. H1/Q3材料已存在 | 年度selector不偷用；FY2026未取得仍awaiting_source |
| 10. due/expiry临界与日期精度 | UTC比较且上海展示正确；未到公开时间上界不视为当时可得 |
| 11. 到期无资料、每日重复tick | due/expiry阶段各至多一次不足结果/context；阶段内as_of变化不堆重复检查或提醒 |
| 12. 同年正式更正/不明重发 | 前者新check+supersedes，后者source_conflict；旧输入不漂移 |
| 13. 过期后补发/正式更正 | 迟到资料需显式检查；正式更正可更正历史且不重新armed |
| 14. false+unknown / true+unknown | all/any按三值规则判定；coverage仍partial且列出未知项 |
| 15. lease丢失/提交后退出/消费两次 | 无半份结果；恢复同check；同事件消费一次且不重复通知 |
| 16. 暂停/撤权/关闭功能与在途任务 | 不再派发；提交前重查控制，机械结账仍完成且历史可读 |
| 17. 新check已确认后又有更正 | 新事件身份独立；旧已读不吞新变化，不把已读变成事实审核 |
| 18. 未审核解释/相关指标同向 | UI保留假设标签；发布和收件箱不自动标已证实支持关系 |
| 19. 利率无拖累/规模非正 | rate≥0返回not_applicable/no_rate_drag；rate<0且volume≤0返回false，无误判覆盖 |

估算净投入：MVP-A 3–4人周（领域/材料1.5–2，页面/API1–1.5，人工金样本0.5）；MVP-B另2–3人周（选择器/队列1–1.5，事件/恢复/验收1–1.5），不含IN-01共享投入。
主要风险是披露口径变化、表格定位可靠性、next annual缺量价数据、SQLite短事务竞争，以及把历史分解过度解释为未来因果；各以原件金样本、显式unknown与有限DSL约束。
实现前需确认新NII/量价字段的校验规则和manifest，以及IN-01 subject/引用注册最终接口；不能把本文示例UUID作为真实已导入状态。
MVP-A优先使用已有文本定位与人工审核；Docling只可在后续页级试验输出候选定位，记录引擎/模型版本和bbox坐标系，必须与原字节核对。
[Docling官方文档](https://docling-project.github.io/docling/reference/docling_document/)提供page_no/bbox/charspan与表格结构；这不保证招行表格抽取准确，需金样本评估后决定是否引入依赖。
[Arelle官方文档](https://arelle.org/arelle/documentation/)提供XBRL处理和规则验证能力；本案来源是PDF，只借鉴单位、上下文、恒等校验思想，不宣称PDF通过XBRL认证。
[OpenBB官方Quick Start](https://docs.openbb.co/odp/v4/python/quickstart)展示results/provider/warnings等返回字段；这里只参考来源与警告显式分离，不引入其SDK/默认provider选择或新付费数据链。
工程测试、材料人工核验、真实解释质量与生产启用分别报告；通过静态样本不代表第二家银行或下个年度已覆盖。
