# 方向1执行拆分：可信研究入口与质量对照

日期：2026-11-07。状态：**执行计划主审完成；本表全部任务尚未执行**。依据：[最终设计](../01-research-quality.md)、[共同约定](../README.md)、[主审记录](../review.md)。本文件只安排工作；没有实现、运行迁移、安装依赖或取得任何验收结果。

交付包含 G0、Q01–Q08，以及共享事件可用后的来源失效提示适配。G0 使用既有普通研究入口；实验限定同一已授权投资者模型、baseline 与 C1 兼容候选、冻结材料、最多两次重复。继续采用设计中已核查的 Pydantic Evals、MLflow、Qlib 取舍，不增加评测平台、自动 judge 签署或收益回测。

## 分派、依赖与验收边界

- 共同前置由主代理交付：`BASE-01` 为基线、已有改动及命令清单；`BASE-02` 为共享错误、引用、事件和时间契约冻结；`BASE-03` 为离线测试隔离及 fixtures 约定。它们不计入本表人日。
- 共享PK-01/PK-02负责资产package-data与离线wheel检查器；G0及最终页面验收检查实际本方向模板/JS/CSS，不依靠源码目录存在证明发行可用。
- 共享SV-01至SV-06实现可信文档事实更正的实际source.invalidated生产者；QT-046依赖SV-06。当前没有通用原件撤回入口，有可信撤销记录则拒读，否则不可读只标source_unavailable并拒绝相关动作，不靠fixture宣称已实现原件撤回。
- 共享事件任务为 `EV-01` 至 `EV-12`，其中 `EV-12` 为事件基础交付验收。本表只有 QT-046 接入依赖 EV-12；G0 和实验核心的准入、源有效性检查及运行不依赖事件系统。QT-046 属于本次完整交付，须在最终验收前完成；事件未交付不阻塞 G0 或实验核心的分阶段交付。
- 每个任务交给一个 sub-agent／worker。任务正文标 `owner=集成` 的工作只由同一集成人员串行写入。领域 worker 只能写该行允许范围；新增测试文件随所属任务拥有。未列出的文件只读，超范围修改须先由主代理重新分配，不以“顺手修复”扩散授权。
- 文件冲突按依赖串行：QT-013 → QT-007 → QT-040 是共享注册写入链；QT-019 → QT-020 → QT-024 → QT-025 → QT-026 → QT-027 → QT-028 → QT-029 → QT-030 是方法准入和执行接线合并链。QT-026 后 QT-032 负责正式查询隔离；QT-030、QT-032 完成后才统一验证完整运行。其余任务只有允许范围互不相交时才并行。
- `src/investment_agent/storage/models/__init__.py`、`api/deps.py`、API/Web router 注册、`settings/registry.py`、Alembic 链和全局导航归集成人员。本表的相应路径均补全项目相对前缀。其他方向对这些文件的修改也经主代理同一串行队列合并，不能各建分支后盲目覆盖。
- 设计基线为 `1f62fa4`，当前工作区已有用户并行 simulation 改动，包括 `061_simulation.py` 和 ORM 注册。实施必须以 BASE-01 的实际结果复核 head；不占用固定迁移号，不编辑 simulation、`tests/simulation/` 或用户既有改动。
- 允许：纯函数、真实临时 SQLite 的服务与竞争测试、进程内 ASGI HTTP 合约、FunctionModel／mock-provider、前端组件／DOM fixture、响应式样式约束、构建和类型检查、局部故障注入。所有 provider、行情和下载入口使用 BASE-03 的网络阻断及 allowlist 测试入口；使用惰性密钥、独立临时数据目录与数据库。现有 conftest 的 AkShare 阻断和 `-m 'not network'` 本身不证明没有外连。
- **不包含真实 E2E 测试**：不启动真实浏览器全栈旅程，不连接真实账户、模型、provider、行情或付费链路。原设计 Q08 中的 browser／真实 HTTP 页面要求，在本执行范围内以进程内 ASGI、固定 API fixture 和组件／DOM 测试替代，不运行现有 `tests/browser/run.sh` 作为本轮完成条件。
- 实现任务含必要的局部测试，不用测试重写实现；独立“测试”任务覆盖跨模块合约和故障边界。Review 由非该批主要作者承担，回修由原 owner 在既定范围执行；不得以审查者自行改代码替代复核记录。

实施细化已由主审冻结并纳入 BASE-02：增加只读 `GET /investor/methods/quality-mutations/{mutation_id}`，owner 鉴权后返回原 mutation 收据及原 result refs；两个功能开关关闭仍允许有权限读取。404 仅表示尚无可见的已提交收据，不证明没有在途请求；客户端继续使用原 key、原 payload 重放或轮询，不能生成新 key 重开。失败事务回滚不留下伪成功收据。该路径补足原设计“丢 ack 先查询收据”在草稿创建应答丢失、尚不知道 experiment_id 时的可执行协议，不扩大执行权限。

## 原子任务表

估算单位为人日，每行一个有界任务；0.25–1 人日为单次分派范围。所有新路径均明确标“新”；`tests/fixtures/research_quality_v1/` 为本方向专用新 fixture 目录。表中同名文件的多次写入必须按所列依赖串行，Review／验收不得修改实现。

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---:|
| QT-001 | 实现 | 冻结实验 DTO、QualityArtifactRef、生命周期、mutation 收据查询响应与引用解析白名单，包含内部 blind_map 的单独类型边界 | 新 `src/investment_agent/research_quality/__init__.py`、`src/investment_agent/research_quality/contracts.py`、`src/investment_agent/research_quality/refs.py`；新 `tests/unit/test_quality_contracts.py` | BASE-01, BASE-02 | Contract extra=forbid；UUID/revision/Decimal/UTC 校验通过；正式 ObjectRef 与实验 ref 不隐式互转；blind_map 不可作公开 ref；收据查询返回原result refs，不暴露凭证/私有prompt | 0.75 |
| QT-002 | 实现 | 建立十二情境索引及 C1 兼容候选包/fixture；固定三公司、每公司正常/旧失败/不足/反例，4 开发与 8 留出、预期及 hash | 新 `config/methods/company-research.c1-counter-analysis.yaml`；新 `tests/fixtures/research_quality_v1/dataset/`、`tests/fixtures/research_quality_v1/methods/`；新 `tests/support/quality_dataset.py`；新 `tests/unit/test_quality_dataset.py` | QT-001, BASE-03 | 索引有精确 refs/模式/split/预期/次数；C1 保留原三步骤，仅加强反证，不声明动态采集/计算能力；包可经原提案导入且不默认启用/移动head；无编造成真实成功的样例 | 0.75 |
| QT-003 | 测试 | 用既有导入、提案、结构评估、原 admit 与方法校验链验证 v1+C1 可执行组合，形成兼容性门槛 | 新 `tests/integration/test_quality_method_compatibility.py`；新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-compatibility.md` | QT-001, QT-002, BASE-03 | 临时 SQLite+FunctionModel 实际保存计划、读取 verified 反证、结算方法步骤；不 mock admit；不兼容 old_method/capture 包真实拒绝，不移动 head，不降 validator | 1 |
| QT-004 | 测试 | 对已保存结果做 Pydantic Evals 薄适配兼容性 spike，记录沿用或暂缓结论 | 新 `tests/unit/test_quality_evals_adapter.py`；新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-evals-spike.md` | QT-001, BASE-03 | 固定 pydantic-ai==2.31.1；仅用当前可用依赖和保存 fixture；若 API 不可用明确 defer，核心仍用本地纯函数，不安装/升级依赖，不让外部 repeat/retry 包住 Task | 0.25 |
| QT-005 | 实现 | 定义五类实验索引 ORM 与唯一键、不可变 task binding，复用 MethodArtifact 存原件 | 新 `src/investment_agent/storage/models/research_quality.py`；新 `tests/unit/test_quality_models.py` | QT-001 | 五表字段、owner 索引、trial/task 唯一关系及连续 assessment revision 约束明确；不新增费用事实账本；模型模块可独立导入 | 0.5 |
| QT-006 | 实现 | 实现 owner/引用完整性、CAS、追加制品及 mutation receipt 仓储 | 新 `src/investment_agent/storage/repositories/research_quality.py`；新 `tests/unit/test_quality_repository.py` | QT-005 | SQLite 不开 FK 时仍拒绝孤儿/跨 owner；同键异参冲突；追加报告/评分不覆盖旧版；短写事务不包模型或解析 I/O | 0.75 |
| QT-007 | 集成 | owner=集成：注册 ORM、两个默认关闭开关、实验制品 kind 支持及本方向迁移；按实际 head 串行分配版本 | `src/investment_agent/storage/models/__init__.py`、`src/investment_agent/config.py`、`src/investment_agent/settings/registry.py`；仅必要时修改 `src/investment_agent/storage/models/investor.py` 的 kind 白名单；新 `alembic/versions/<实际分配号>_research_quality.py`；新 `tests/unit/test_quality_settings.py` | QT-005, QT-006, QT-013 | 开关关闭兼容旧行为；不改既有 migration；五表/索引与 ORM 一致；保留 simulation 注册；授权历史读与机械结账不被新写开关阻断 | 0.75 |
| QT-008 | 测试 | 验证仓储竞争、制品原子保存与迁移升级回环 | 新 `tests/integration/test_quality_storage.py`、`tests/integration/test_quality_migrations.py` | QT-007, BASE-03 | 临时空库/实施时 head 升级/downgrade-upgrade 通过；同 mutation 并发只留一份结果；hash 不符或失败无半制品；含数据破坏性 downgrade 拒绝或受导出保护 | 0.75 |
| QT-009 | Review | 第一批审查：契约、兼容组合、持久化和迁移安全，给出逐项问题与复现方式 | 新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-review-1.md` | QT-003, QT-004, QT-008 | 非作者核对引用隔离、真实校验链、事务、现有 simulation 兼容；每项标严重度、owner 和退出证据 | 0.5 |
| QT-010 | 回修 | 关闭第一批问题并复核；没有问题也留下复核记录 | 仅 QT-001–QT-008 原 owner 的已授权文件；更新 `execution/evidence/quality-review-1.md` | QT-009 | 阻塞项全部关闭；非阻塞项明确处理且不影响本范围；重复相关局部检查，记录兼容组合能否进入 Q04 | 0.5 |
| QT-011 | 实现 | 实现 G0 只读 coverage/preflight 与问题模板契约，返回已有材料/缺口及当前方法模型摘要 | 新 `src/investment_agent/research_quality/entry.py`；新 `src/investment_agent/api/schemas/research_entry.py`、`src/investment_agent/api/routes/research_entry.py`；新 `tests/unit/test_quality_entry.py` | BASE-01, BASE-02, BASE-03 | 只接 listing_ref/template_key/question；读已提交精确材料；没有 candidate 也可用；不写 Task/预算/grant，不采集；预检时间及非承诺性明确；遵守现有 Agent 开关 | 0.75 |
| QT-012 | 实现 | 制作 G0 独立组件：选模板/编辑问题、coverage/缺口、明确选择 company_research、原任务结果解释 | 新 `src/investment_agent/web/static/research_entry.js`；新 `src/investment_agent/web/templates/_research_entry.html`；新 `src/investment_agent/web/static/pages/research_entry.css` | QT-011 | 使用既有 listing/conversation/message/research API 和 MessageInput 幂等键；只看缺口不提交；发送需明确动作；有观点/不足/未完成及真实停止原因分开呈现 | 0.75 |
| QT-013 | 集成 | owner=集成：注册 G0 preflight、挂载研究组件及其现有 Agent 页入口，复用认证和原消息路径 | `src/investment_agent/api/app.py`、`src/investment_agent/api/deps.py`、`src/investment_agent/web/templates/agent.html`、`src/investment_agent/web/static/agent.js`；新 `tests/unit/test_quality_entry_wiring.py` | QT-011, QT-012, BASE-02 | 完整路径为 POST /investor/research-entry/preflight；会话写有同源检查；不触碰 admit 权限；入口不要求实验开关/candidate；注册错误码有显式映射且不改既有语义 | 0.5 |
| QT-014 | 实现 | 实现 FrozenReadEnvelope canonical 构建与同输入重现，绑定精确来源及所有读取算法版本 | 新 `src/investment_agent/research_quality/envelope.py`；新 `tests/unit/test_quality_envelope.py` | QT-010 | 身份、问题、三类来源时间、单位、refs/hash、缺口、排序/分页、renderer/schema/tool profile 完整；默认 observed，reconstructed 分列；大原件引用不可变 blob | 0.75 |
| QT-015 | 实现 | 实现 case/draft/seal 生命周期与原子封存 mutation，固定次数、随机种子、方法/模型快照及每 trial 份额 | 新 `src/investment_agent/research_quality/lifecycle.py`；新 `tests/unit/test_quality_lifecycle.py` | QT-014, QT-006, QT-003 | seal 不创建 Task/费用；同事务绑定 spec/envelope/artifact hash/receipt；次数 1–2；整除两臂份额、余数不用；不兼容候选阻塞，封存后不可漂移 | 0.75 |
| QT-016 | 实现 | 实现精确源版本授权、撤销/更正实时检查服务，覆盖 seal 与之后每次读取 | 新 `src/investment_agent/research_quality/source_validity.py`；新 `tests/unit/test_quality_source_validity.py` | QT-014 | acquired/published/available 均不晚于 cutoff；撤销拒读、核验失败停；更正标 superseded_source 并要求新 spec；无需事件系统也成立，不通过复制文本规避遗忘 | 0.75 |
| QT-017 | 实现 | 实现冻结材料查询/搜索 adapter，按 membership/hash 读取固定快照、证据、事件、原件片段及预存计算 | 新 `src/investment_agent/research_quality/read_adapter.py`；新 `tests/unit/test_quality_read_adapter.py` | QT-014, QT-016 | 包内同 ref 固定响应/分页；包外 outside_envelope；无当前库回落、动态估值/同行扩样/搜索/下载；不伪造 capture 或 committed action | 0.75 |
| QT-018 | 实现 | 实现内部 ResearchExperimentAdmission 解析与 proposal/scope/model/能力 fences | 新 `src/investment_agent/research_quality/admission.py`；新 `tests/unit/test_quality_admission.py` | QT-010, QT-015, QT-016 | 只认服务端封存 spec，baseline=old_method、candidate 最新通过且 shadow；包/tool family 展开检查；不接客户端配置/Model/validator；精确 scope 校验不放宽原授权 | 0.75 |
| QT-019 | 集成 | owner=集成：接入 MethodService 精确两臂 receipt 冻结，保留原 shadow 语义 | `src/investment_agent/investor/methods.py`；新 `tests/unit/test_quality_method_binding.py` | QT-018 | 复用 _freeze_package_receipt；baseline/candidate hash 可追溯；实验不计 candidate_shadow_result 批准覆盖；旧 method_override 行为保持；不移动 head | 0.75 |
| QT-020 | 集成 | owner=集成：在原 admit 接入 typed context 与两臂原子 start，写 trial/Task/grant/receipt/binding | `src/investment_agent/agent/execution.py`；新 `src/investment_agent/research_quality/start.py`；新 `tests/unit/test_quality_start.py` | QT-019 | context 与 method_override 互斥；API/聊天/发现不能注入；唯一键去重；两臂任一队列/结构准入失败全回滚；旧 grant 序列化兼容，无一臂半入场 | 1 |
| QT-021 | 测试 | 对真实临时 SQLite 的原准入链做并发、丢响应、拒绝和 CAS 合约验证 | 新 `tests/integration/test_quality_admission.py` | QT-020, QT-017, BASE-03 | 重复 start 只留一组 Task；异参409、head/proposal 变更拒绝；capability 不可用零模型发送；owner/授权先于 receipt，原 Task/grant/receipt 可查询 | 1 |
| QT-022 | Review | 第二批审查：冻结读取、源有效性、方法绑定及原子准入 | 新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-review-2.md` | QT-021 | 非作者检查 sources、scope、时间、hash、两臂公平与事务边界；检查测试真实走 admit，指出所有隐式当前头读取 | 0.5 |
| QT-023 | 回修 | 关闭第二批准入与 envelope 缺陷并复核 | 仅 QT-014–QT-021 原 owner 的已授权文件；更新 `execution/evidence/quality-review-2.md` | QT-022 | 阻塞项清零；回归第一批兼容性和准入故障用例；无法合法执行的组合保持 blocked，不降低校验 | 0.5 |
| QT-024 | 集成 | owner=集成：接入实验专用 assemble 与最终 payload 来源清单检查 | `src/investment_agent/agent/research.py`；新 `src/investment_agent/research_quality/assembly.py`；新 `tests/unit/test_quality_assembly.py` | QT-023 | 先原 boundary 再实验装配；无当前观点/案头/持仓/mandate 正文/记忆/旧会话/cache；只恢复本 Task checkpoint；指令/schema/method/tools/首输入分别 hash | 1 |
| QT-025 | 集成 | owner=集成：将 TrustedTools 后置 membership/adapter 校验接入运行，收紧实验工具 profile | `src/investment_agent/agent/tools.py`、`src/investment_agent/agent/builtin_tools.py`；新 `tests/unit/test_quality_tool_boundary.py` | QT-024, QT-017 | 顺序为原 scope/cutoff/check_boundary → membership/hash → adapter；禁止写业务/刷新/外网工具；允许内部过程提交仍走动作账本；未知 ref 零源网络请求 | 0.75 |
| QT-026 | 集成 | owner=集成：接入实验 RunSpecification、阶段/finish_only 指令及独立完成器，持久化 quality_result | `src/investment_agent/agent/research.py`；新 `src/investment_agent/research_quality/policy.py`；新 `tests/unit/test_quality_policy.py` | QT-025 | 保留计划/引用/方法步骤/scope 校验；不要求 prepare_company_view，不调用 PublicationService；最终研究与 semantic_review/hash/完整性不可变；缺口=partial，不写正式 note/evidence/memory/outbox | 1 |
| QT-027 | 集成 | owner=集成：在原 BudgetService 短事务追加 trial/实验硬上限，保留 reserve/sent/settle 事实账本 | `src/investment_agent/agent/budget.py`；新 `src/investment_agent/research_quality/budget.py`；新 `tests/unit/test_quality_budget.py` | QT-026, QT-015 | 统计所有 trial/attempt 的 used+reserved+unknown；禁止借额/重建Task/跨日重置；结论预留/输出上限/超时同份额；原日/task 限额仍更严；unknown 保留 null 及占用 | 1 |
| QT-028 | 集成 | owner=集成：接入每次发送、工具、提交的模型/提案/源/开关 fence，保留原取消与记忆撤销边界 | `src/investment_agent/agent/execution.py`、`src/investment_agent/agent/executor.py`；新 `tests/unit/test_quality_runtime_fences.py` | QT-027, QT-016 | 同模型 hash 变化/不可用拒发、不替代；提案撤回/材料撤销/关执行开关停后续动作；原 memory revision fence 生效；迟到结果 fenced，已发生 usage 仍结算 | 1 |
| QT-029 | 实现 | 实现试次状态投影、交错调度、取消与恢复的实验编排，调用原 TaskService 而不直改 Task | 新 `src/investment_agent/research_quality/orchestration.py`；新 `tests/unit/test_quality_orchestration.py` | QT-028 | 封存 seed 决定顺序、相同 repeat 优先、最多两任务；真实失败仍 awaiting_review/partial；cancelling 等结账后 cancelled；外部日额度争用单列，不创建新Task重试 sent/unknown | 0.75 |
| QT-030 | 集成 | owner=集成：接入现有 agent job/worker 的实验 profile 选择和恢复入口 | `src/investment_agent/agent/job.py`、`src/investment_agent/research/worker.py`；新 `tests/unit/test_quality_worker_wiring.py` | QT-029 | 复用原队列/claim/lease/heartbeat，不建付费调度器；普通任务走原 ResearchPolicy；实验绑定缺失/未知 profile 拒绝；恢复沿原 Task/checkpoint，关功能仍机械结账 | 0.75 |
| QT-031 | 测试 | 对运行预算及 lease 做临时 SQLite 双 worker 与 sent 中断等局部故障注入 | 新 `tests/integration/test_quality_runtime_faults.py`；新 `tests/support/quality_runtime.py` | QT-030, BASE-03 | 最后额度竞争不超额；一臂耗尽不借额；sent 中断留 unknown 不重发；取消/关开关后零后续发送；迟到 usage 结算、迟到结果不可评分；外部争用保留 planned 分母 | 1 |
| QT-032 | 集成 | owner=集成：为正式搜索、上下文复用、监测/评审触发和发布入口加入实验来源污染栅栏 | `src/investment_agent/evidence/search.py`、`src/investment_agent/agent/context.py`、`src/investment_agent/agent/company_context.py`、`src/investment_agent/investor/monitoring.py`、`src/investment_agent/review_panel/triggering.py`、`src/investment_agent/investor/publication.py`；新 `src/investment_agent/research_quality/isolation.py` | QT-026 | 各入口调用同一窄检查、拒实验 Task/产物来源；没有直接读取实验 namespace 的绕路；无实验绑定的正式任务原行为不变；不修改正式 ObjectRef 以采信实验 | 1 |
| QT-033 | 测试 | 捕获实际模型 prompt/工具响应并验证隔离及旧 Agent/review_panel 回归 | 新 `tests/integration/test_quality_isolation.py`；新 `tests/fixtures/research_quality_v1/isolation/` | QT-031, QT-032 | 修改持仓/观点/记忆/cache 后两臂材料不漂移；包外数据未到模型；撤销仍停；正式搜索/监测/上下文/评审/发布无实验内容，无新增正式证据/通知/批准覆盖 | 1 |
| QT-034 | Review | 第三批审查：执行边界、预算、worker 恢复与污染隔离 | 新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-review-3.md` | QT-033 | 非作者检查实际 prompt/工具返回、所有发送入口、实际正式查询、unknown/lease 证据及旧行为回归；单独记录执行开关开放门槛 | 0.75 |
| QT-035 | 回修 | 关闭第三批执行/隔离缺陷并复核，形成实验核心可独立交付证据 | 仅 QT-024–QT-033 原 owner 的已授权文件；更新 `execution/evidence/quality-review-3.md` | QT-034 | 任何越权/超额/污染/重发阻塞项清零；相关故障注入重过；源边界直接校验，无 EV-12 依赖；真实研究质量保持未验证 | 0.75 |
| QT-036 | 实现 | 实现固定 A/B 盲映射、人工五维 assessment 追加/CAS 与揭盲后修订标记 | 新 `src/investment_agent/research_quality/assessment.py`；新 `tests/unit/test_quality_assessment.py` | QT-023, QT-001 | reviewer 取认证上下文；仅固定完成 output_hash 可评；下一revision连续；事实/反证门槛与 N/A 理由校验；泄盲可标 compromised；blind_map 不进入公开字段 | 0.75 |
| QT-037 | 实现 | 实现不可变逐例与数据集报告，固定评分集/hash/算法版本及七个分母 | 新 `src/investment_agent/research_quality/reporting.py`；新 `tests/unit/test_quality_reporting.py` | QT-036, QT-029 | planned/admitted/sent/completed/usable_output/paired/scored 正确；qualified/scored 及 qualified/planned 并列；partial/unknown/取消不删样本；公司聚合、重建模式/留出使用记录分列，不给投资胜率 | 0.75 |
| QT-038 | 测试 | 验证评分、盲映射泄漏面、报告重现和失败分母的离线服务合约 | 新 `tests/integration/test_quality_assessment_reporting.py` | QT-037, QT-035 | 评分409/错hash422不覆盖；揭盲后改分分组正确；旧评分集重放旧报告；不足/反例可按预期合格；方向观点无反证不可用 N/A 掩盖；过滤显示隐藏数量 | 0.75 |
| QT-039 | 实现 | 实现质量 case/experiment/seal/start/cancel/blind/assessment/report 及 mutation 收据查询的薄 API 与响应 schema | 新 `src/investment_agent/api/routes/research_quality.py`、`src/investment_agent/api/schemas/research_quality.py`；新 `tests/unit/test_quality_api_schemas.py` | QT-037, QT-035 | 设计完整路径及 GET /investor/methods/quality-mutations/{mutation_id} 对齐；请求无 actor/内部context/任意方法dict；mutation/CAS齐全；owner鉴权后查询原receipt/result refs，关闭仍可读；GET不写不调模型，404无在途承诺；无blind_map公开后门 | 1 |
| QT-040 | 集成 | owner=集成：注册质量 API/Web 页面、显式错误状态、开关依赖与 methods 入口/全局导航 | `src/investment_agent/api/app.py`、`src/investment_agent/api/deps.py`、`src/investment_agent/web/routes.py`、`src/investment_agent/web/templates/base.html`、`src/investment_agent/web/static/nav.js`、`src/investment_agent/web/templates/methods.html`、`src/investment_agent/web/static/methods.js`；新 `tests/unit/test_quality_registration.py` | QT-007, QT-013, QT-039, QT-042, QT-043, BASE-02, PK-02 | /investor/methods 前缀正确；新增403/404/409/422/503显式映射，feature_disabled=503，writes_disabled仍403；关闭新写不隐藏授权历史/receipt；菜单缺条件有解释；用PK helper检查发行包含新增模板/脚本/嵌套CSS，不重复改package-data或升级依赖 | 0.75 |
| QT-041 | 验收 | 独立验收 G0：分别验证进程内ASGI原认证消息合约及组件状态，不串接全栈旅程 | 新 `tests/integration/test_quality_entry_acceptance.py`；新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-g0-acceptance.md` | QT-013, BASE-03, PK-02 | 无candidate走原company_research admit/任务结果fixture；预检与只看缺口零Task/预算/采集；明确提交才创建原任务；无实验binding/扩大grant；停止原因可见；实际G0资产通过wheel检查，不依赖实验核心/事件交付 | 0.75 |
| QT-042 | 实现 | 制作质量准备/执行页面组件：选 case/提案、差异/缺口/额度预检、封存、明确 start、状态/取消 | 新 `src/investment_agent/web/templates/research_quality.html`；新 `src/investment_agent/web/static/research_quality.js`、`src/investment_agent/web/static/pages/research_quality.css` | QT-015, QT-039 | 不手填内部ID；seal不收费、start独立动作；丢ack先查quality-mutations，即使草稿ID未知也恢复原refs；404仍保留原key/payload轮询或重放，不换key重开；empty/blocked/partial/409保留输入、分母及原因 | 1 |
| QT-043 | 实现 | 制作盲评/报告组件：同材料引用、五维评分、泄盲标记、提交后揭盲、版本对照与分母 | 新 `src/investment_agent/web/static/quality_assessment.js`、`src/investment_agent/web/static/quality_report.js`；新 `src/investment_agent/web/templates/_quality_assessment.html`、`src/investment_agent/web/templates/_quality_report.html` | QT-038, QT-039 | 盲评 DOM/数据属性不含 arm/方法/模型/预算暗示；失败数可见；提交评分前不可揭盲；已揭盲后改分提示明确；unknown 不显示0，过滤保留总体分母 | 1 |
| QT-044 | 测试 | 使用固定 API fixture 做 G0 与质量页组件/DOM、390/1440 响应式样式约束和可访问性状态验证 | 新 `tests/components/research_quality/`；新 `tests/fixtures/research_quality_v1/ui/` | QT-012, QT-042, QT-043, BASE-03 | BASE-03本地组件工具运行；不启动浏览器/后端全栈；两宽度DOM/CSS规则、键盘/label/状态有断言；403/409/422/503、empty/partial/blocked、盲泄漏均覆盖；草稿/start丢ack含404均保持原key/payload；不声称真实布局/设备通过 | 0.75 |
| QT-045 | 测试 | 运行进程内 ASGI 全部质量路由合约，验证身份/CSRF/收据查询重放/开关/只读及 blind_map 白名单 | 新 `tests/integration/test_api_quality.py` | QT-040, QT-038, BASE-03 | 临时库、真实领域服务/原admit；GET/seal零费用；丢ack查询返回原refs，重放不增Task/sent；收据404不表示无在途，失败回滚无伪成功；同键异参/spec_hash冲突409；他人404/detail规范；关闭仍可读历史/receipt；公开URL无blind_map | 1 |
| QT-046 | 集成 | owner=集成：接入source.invalidated白名单事件作为失效提示，保持直接源检查为准 | 新 `src/investment_agent/research_quality/events.py`；新 `tests/integration/test_quality_source_events.py`；共享事件注册位置只允许修改EV-12交付清单指定注册表 | QT-035, EV-12, SV-06 | 消费实际可信更正样本，精确revision/owner匹配且幂等；无可信撤回记录不伪造withdrawn；事件不授权/启动付费任务，不把实验结果投进收件箱；关闭事件适配仍通过直接边界用例 | 0.5 |
| QT-047 | 实现 | 写用户/运维与数据集说明，记录开关、回退、unknown对账及尚未验证项 | 新 `docs/product/research-quality-workflow.md`；新 `docs/operations/research-quality.md`；新 `tests/fixtures/research_quality_v1/README.md` | QT-035, QT-038, QT-041, QT-045, QT-046 | 文档与实际路径/配置一致；先停派发再处理在途/unknown；优先代码回退，历史不删；留出变更/使用计数与污染规则清楚；真实材料/研究质量/生产启用显式 pending | 0.5 |
| QT-048 | Review | 第四批总审：API、页面、评分报告、事件适配、文档及前批回修回归 | 新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-review-4.md` | QT-038, QT-041, QT-044, QT-045, QT-046, QT-047 | 非作者按下方覆盖矩阵逐项核对；确认所有公开泄漏面、错误映射与关闭模式；没有将 DOM/ASGI 结果写成真实 E2E 或研究质量通过 | 0.75 |
| QT-049 | 回修 | 关闭第四批残余缺陷并交叉复核，冻结待验收提交及命令 | 仅 QT-036–QT-047 原 owner 的已授权文件；如涉及前批只回到对应 owner；更新 `execution/evidence/quality-review-4.md` | QT-048 | 全部阻塞项关闭；变更涉及的合同/回归重过；形成可复核 commit、fixture hash、命令和结果清单，保留用户并行改动边界 | 0.75 |
| QT-050 | 验收 | 由独立验收 worker 对 G0、实验核心和事件适配做离线交付验收并签署证据清单 | 新 `docs/product/plans/next-iterations-2026-11-07/execution/evidence/quality-final-acceptance.md`；新 `tests/fixtures/research_quality_v1/acceptance_manifest.json` | QT-049, QT-041, QT-010, QT-023, QT-035 | 固定提交重跑必要纯函数/临时SQLite/ASGI/mock-provider/组件检查及 ruff、mypy、not-network 相关回归；矩阵全有证据；核对 migration/默认关闭/旧流程；真实E2E、真实质量与生产均列未验证 | 1 |

回修容量为计划预留，不是允许带阻塞缺陷结束；超出该行预算由主代理追加有范围的任务，不吞并到验收或让 Review 成为无界实现任务。QT-032 是同一污染栅栏接入任务，只做六个既有查询/入口的窄接线；若实际需要重构各子系统，须拆补卡，不能扩大到通用搜索或发布重写。

## 原任务卡覆盖矩阵

| 原设计任务 | 对应执行任务 | 完成证据归属 |
|---|---|---|
| G0 首次可信研究 | QT-011–QT-013、QT-041、QT-044 | G0 独立验收；原消息准入及结果读取，候选和事件均非前置 |
| Q01 契约与可执行组合 | QT-001–QT-004、QT-009–QT-010 | 十二情境/留出 hash、QualityArtifactRef、v1+C1 原校验链、Evals spike 结论 |
| Q02 存储与迁移 | QT-005–QT-010 | 五表、唯一键、owner/CAS/追加、临时库迁移回环及审查 |
| Q03 envelope 封存 | QT-014–QT-017、QT-022–QT-023 | 精确材料/算法版本/hash、有效性、无当前头回落 |
| Q04 可信两臂准入 | QT-018–QT-023 | 原子 start、typed context、精确 receipt、真实原 admit/并发证据 |
| Q05 执行与预算 | QT-024–QT-035 | prompt/工具隔离、独立完成器、原账本附加上限、worker/fence/unknown/污染栅栏 |
| Q06 报告与盲评 | QT-036–QT-038、QT-043 | 五维量表、盲映射/CAS、七分母、不可变报告及揭盲后修订 |
| Q07 API与页面 | QT-039–QT-045 | 薄API、共享注册/错误/开关、组件状态和进程内ASGI，不含真实浏览器全栈验收 |
| Q08 联合验收 | QT-009–QT-010、QT-022–QT-023、QT-031、QT-033–QT-035、QT-038、QT-041、QT-044–QT-050 | 四批Review和回修、文档、源事件适配、独立离线签署；真实研究质量保持 pending |

## 原验收场景到新任务的覆盖矩阵

场景编号仅为本表行号，不是外部任务依赖。每项验收均使用离线 fixture；涉及“模型实际收到”指可观察的 FunctionModel/mock-provider 输入。

| 原验收场景 | 实现/集成任务 | 主要测试或验收任务 | 必须保存的证据 |
|---|---|---|---|
| 正常两臂研究 | QT-015、QT-019–QT-020、QT-024–QT-030 | QT-021、QT-033、QT-050 | 相同 envelope hash、不同精确 receipt、真实两Task refs、不可变输出与无正式发布 |
| 无 candidate 首次研究 | QT-011–QT-013 | QT-041、QT-044 | 原 company_research grant/admit、材料缺口、无实验授权扩大 |
| 草稿/封存/GET | QT-015、QT-039–QT-040 | QT-045 | 读与封存前后 Task/BudgetEntry计数一致，无源网络调用 |
| 同mutation丢ack重放 | QT-006、QT-020、QT-039、QT-042 | QT-021、QT-044、QT-045 | quality-mutations查询返回同一receipt/原refs，含未知草稿ID；404仅无可见已提交收据；Task/sent不增，页面不换key重启 |
| 同mutation异参 | QT-006、QT-020、QT-039 | QT-008、QT-021、QT-045 | 409，原spec与费用不变 |
| 并发start/最后额度 | QT-020、QT-027 | QT-021、QT-031 | 一组trial、无半准入、reserve原子上限不超 |
| 先执行臂耗尽/unknown | QT-015、QT-027、QT-029、QT-037 | QT-031、QT-038 | 固定份额不可借、unknown占用不释放、external_budget_contention单列partial |
| baseline head变化 | QT-018–QT-020 | QT-021 | 新入场拒绝，原Task receipt不随head变化 |
| 提案修订/拒绝 | QT-018–QT-020、QT-028 | QT-021、QT-031、QT-033 | 停新准入、迟到fenced、不增加AG30覆盖 |
| 包要求capture/外网 | QT-003、QT-018、QT-025 | QT-003、QT-021 | capability错误、零HTTP、没有假capture/action |
| 取得时间晚于cutoff | QT-014、QT-016 | QT-016局部测试、QT-033 | 公开/取得/available三类时间逐一反例，报告期不豁免 |
| 包外ref/搜索新材料 | QT-017、QT-025 | QT-017局部测试、QT-033 | outside_envelope、固定搜索分页、无当前库/网络回落 |
| 当前观点/记忆/恢复缓存变化 | QT-024、QT-028 | QT-033 | 捕获首输入/恢复输入；无非清单投影、无跨Task checkpoint；原撤销栅栏生效 |
| 持仓/案头/mandate正文变化 | QT-024、QT-028 | QT-033 | 最终assembled payload来源清单和排除断言；实时预算/撤销继续检查 |
| 材料更正/撤销 | QT-016、QT-028、QT-046 | QT-033、QT-046适配测试 | 无事件时直接拒读；有事件时幂等提示；旧制品不改写、更正需新版本 |
| 模型配置/服务不可用 | QT-018、QT-028 | QT-028局部测试、QT-031 | 同模型hash固定、拒后续发送、不换provider或模型、不返回空成功 |
| sent后进程终止 | QT-027–QT-030 | QT-031 | durable sent/unknown、原Task/checkpoint恢复、无重发、可信对账记录 |
| 发出后取消/关开关 | QT-028–QT-030、QT-040 | QT-031、QT-045 | 后续发送零、迟到usage结算、输出不进有效评分；历史仍可读 |
| 一臂失败/预算耗尽 | QT-029、QT-037、QT-042–QT-043 | QT-031、QT-038、QT-044 | planned分母/错误/paired/partial一致，不丢失败卡 |
| 评分冲突/错output hash | QT-036、QT-039、QT-043 | QT-038、QT-045 | 409/422、旧版不覆盖、重新打开不泄漏arm |
| 揭盲后改分 | QT-036–QT-037、QT-043 | QT-038、QT-044 | unblinded新revision、旧盲评汇总可重现 |
| 证据不足/反例 | QT-002、QT-036–QT-037 | QT-038、QT-050 | 拒答按预期判断；方向观点反证门槛不得N/A规避；小样本无胜率结论 |
| 旧Agent/review_panel | QT-019、QT-024–QT-030、QT-032 | QT-033、QT-050 | 无实验binding的指令/工具/receipt/收费与既有fixture一致 |
| 实验输出被正式搜索/监测读取 | QT-026、QT-032 | QT-033 | 实际查询/上下文/触发器的排除证据；无正式SavedEvidence/观点/长期memory/通知/monitor/批准覆盖 |
| QualityArtifactRef与blind_map | QT-001、QT-006、QT-036、QT-039 | QT-001局部测试、QT-038、QT-045 | 正式EvidenceService/NumericResolver拒绝实验ref；公开下载/HTML/data属性无blind_map |

## 分批 Review 与交付门槛

| 批次/切片 | 入口与退出 | 可并行分工 | 交付判断 |
|---|---|---|---|
| G0 独立切片 | QT-011 → QT-012 → QT-013 → QT-041；与 QT-001–QT-006 可并行 | entry 服务与契约冻结后 UI 可开发；共享挂载由集成串行 | 仅原路径离线工程验收；不等 C1、实验开关或 EV-12，不宣称真实模型质量 |
| Review 1 | QT-009 → QT-010 | 契约/数据集、ORM仓储与 G0 分离；兼容验证使用锁定fixture | 兼容组合通过才继续 Q04；不兼容时实验组合真实blocked，G0仍可完成 |
| Review 2 | QT-022 → QT-023 | envelope/source/read adapter 分开；方法准入接缝串行 | 首次允许完整实验执行接线，仍不开放真实模型调用 |
| Review 3 | QT-034 → QT-035 | 运行链顺序合并；评分服务可在冻结契约后并行，查询隔离独立写入 | 实验核心不依赖EV-12，直接源检查、预算与隔离必须全部通过 |
| Review 4与最终验收 | QT-048 → QT-049 → QT-050 | 页面组件与ASGI合约独立作者；QT-046待EV-12后接入 | 最终完整交付含事件提示、文档与独立离线证据，不把真实研究试用设阻塞项 |

## 工程量与保留判断

本表 **50 个任务，合计 38.75 人日**：实现 13.75、集成 10.75、独立测试 7.5、Review 2.5、回修 2.5、验收 1.75。完整工程量以完成本表全部范围为准；较概念设计估算增加了独立 G0、四批审查/回修及故障验证拆分。共享 BASE/EV 基础只在主计划计算一次；主代理跨方向总验收另计。人日含开发、局部测试、四批独立 Review、明确回修容量和最终离线验收，不能当并行后的日历承诺。

以下判断在本轮完成后仍为 **未验证**，必须出现在最终验收记录中，但不阻塞本表离线工程交付：真实模型的研究质量和预训练先验影响、真实提供商缓存/更新、生产材料原件的全面人工核验、真实用户试用、真实账户/浏览器全栈旅程、真实付费成本及生产运行恢复。十二情境的离线 fixture 只证明契约和工程行为，不是十二次真实研究质量评测。若将来启用真实试用，另定人工评分、具体授权和封存预算，不让本计划的验收触发付费调用。

实施前保留两点显式核对：当前实际 old_method 是否确与 v1+C1 情境相符；Pydantic Evals 在固定版本中是否有必要且可用。前者不通过只阻塞该组合，不改 head 凑数；后者不通过保留纯本地报告实现，不升级依赖抢通。当前并行 simulation 导致的迁移和共享文件差异由 BASE-01 与集成人员复查，不能据本文路径列表覆盖已有改动。
