# P3契约与资源冻结（AF-P3-01）

日期：2026-10-17。范围：AF-P3-01（G0）。输入为分层设计第5.5/10.1节、P3计划第2–3.5节、P2冻结（`account-fit-p2/p2-freeze.md`）与本轮代码现实（HEAD=`de77ec5`，迁移head=052）。本文件固定 P3 foundations 三项（01/02/03）交付的第一版边界；不是生产启用授权，也不预支 04–10 的服务语义。配套机读样本：`tests/fixtures/research_closure_v1/expected.json`（独立期望，不 import 任何 service）；DTO 构造器在 `tests/support/supplemental_rules.py`（`synthetic_binding`/`synthetic_coverage_manifest`/`synthetic_application`）。

## 引用与对象冻结

- `investor/common.py::ObjectRef` kind Literal 追加 `rule_coverage`、`research_rule_binding`，二者均为 UUID 身份（不在 legacy 整数集合内）。company_id 仍为整数；不新增 company ObjectRef。`MutationReceipt.result_kind` 不扩展：受理绑定、prepared/published coverage 三条新写入均不经 InvestorMutation，幂等分别由 task 唯一与 ToolAction 参数全等承担（计划3.1）。
- 导入方向红线：investor 层禁止 import supplemental_rules（会成环）。逐规则评审项 `RuleReviewItem` 因此定义在 `investor/publication.py`（与 `SemanticAssessment` 并列）；supplemental 层的 `PreparedClosureProposal` 是独立同构 DTO，由后续任务（AF-P3-05/06）做 supplemental→investor 方向的映射。本轮新增包对包 import 边：零条（全部走既有边 agent→investor、supplemental_rules→investor、storage→supplemental_rules），import graph 基线无需放宽。

## DTO 形状冻结（supplemental_rules/contracts.py）

- `BindingRuleEntry{rule_ref(ObjectRef, supplemental_rule, exact), semantic_hash, selector_hash}`。
- `ResearchRuleBinding`：schema_version=1、id、revision=1、owner_id、task_ref(task)、listing_ref(listing)、research_kind∈{company_research,review}、method_ref(method)、admission_as_of、rules_generation/events_generation≥0、`rule_set: tuple[BindingRuleEntry, ...]`（可空，每规则至多一条）、rule_set_hash、matching_manifest_hash|None、事件摘要（`events_visible_until` 可见性上界、`events_visible_unknown` 截断标志）、input_hash、content_hash、created_at。校验：身份 kind 精确、`rule_set_hash=H(rule_set)`、`content_hash=H(全DTO−content_hash)`、`events_visible_until ≤ admission_as_of`、`admission_as_of ≤ created_at`。空规则集是合法绑定（"当时无生效规则"），与 legacy_unbound（根本无绑定行）机器可区分。
- `CoverageRuleEntry{rule_ref, semantic_hash, status, application_ref|None, basis|None, unknown_reason|None}`，status∈{application, not_applicable, unknown, unbound_new}，字段与状态一一对应：application 必须有精确 rule_application ref 且不得带 basis/unknown_reason；not_applicable 必须有 basis；unknown 必须有 unknown_reason；unbound_new 三者皆不得携带。
- `PreparedClosureProposal{target_application_refs(非空, 精确 rule_application), evidence_refs(精确revision), claim_key|None, rationale, proposal_hash}`；`proposal_hash=H({target_application_refs, evidence_refs, claim_key, rationale})`。
- `RuleCoverageManifest`：schema_version=1、id、revision=1、stage∈{prepared, published}、owner_id、task_ref(task)、**task_id**（=task_ref.id；承载 052 部分表达式索引 `json_extract(payload,'$.task_id')`，这是对计划 DTO 字段清单的一个显式补充）、binding_ref(research_rule_binding)、listing_ref(listing)、case_ref(case)|None、admission_as_of、effective_cutoff、rule_entries(≤2000)、open_issue_refs、closure_proposals、scan 摘要（scan_capped/scan_known_total≥0|None/scan_uncovered_note|None，未 capped 不得带 note）、material_set_hash|None、input_hash、manifest_hash、created_at。published 独有：prepared_ref(rule_coverage)、prepared_manifest_hash、semantic_review_ref(review)、closure_application_refs、thesis_ref|None、recommendation_ref|None、limits_recheck、committed_at。stage 校验：prepared 禁止任何 published 字段取值；published 四项（prepared_ref、prepared_manifest_hash、semantic_review_ref、committed_at）必须齐备，prepared_manifest_hash 与被引用 prepared 的 manifest_hash 的一致性由服务层（06）核对，DTO 只校验格式与存在性。时间序：`admission_as_of ≤ effective_cutoff ≤ created_at ≤ committed_at`（committed_at 仅归一化）。`reference` property 返回 kind="rule_coverage"；binding 返回 kind="research_rule_binding"。
- `RuleApplication.authorization: Literal["mechanical","formal_review"]="mechanical"`。mechanical 逐字保留 P2 禁令（reviewed 结果拒绝、supported/mitigated/resolved 拒绝、closure_refs 拒绝，错误文案不变）；formal_review 要求 semantic_status=reviewed 且精确 semantic_review_ref、closure_refs 非空、application_mode=live、evaluation_purpose=issue_review。缺省值保证 P2 旧行 JSON 回读不变；input_hash 域不含 authorization。replay+issue_review 的既有 P2 硬禁令优先于 formal 分支（formal 的 live 校验是冗余防线）。
- investor 侧：`RuleReviewItem{application_ref(精确 rule_application), disposition∈{no_action_required, resolved_with_closure, limitation_acknowledged}, basis_refs(精确revision)=(), proposal_hash|None}`，proposal_hash 与 resolved_with_closure 严格配对（必须带/不得带）。`SemanticReview` 增 `rule_reviews=()`、`coverage_manifest_ref|None`、`coverage_manifest_hash|None`（ref 与 hash 必须同时出现/同时缺席，ref.kind=rule_coverage）；`agent/research_publication.py` 的 `SemanticInput` 增同样三字段（复用 publication 的 RuleReviewItem），`CompanyViewInput` 增 `closure_proposals=()` 与文件内定义的 `ClosureProposalInput`（形状与 PreparedClosureProposal 的内容四元组一致，无 hash——hash 由服务端冻结时计算，模型不能自报）。全部带缺省值，旧工件 JSON 回读逐字段不变。

## digest 规范

指纹统一沿 P2 `supplemental_rules/canonical.py::canonical_json+sha256`（Decimal/float/timezone-aware datetime 显式 tagging）。每个 `*_hash` 不覆盖自身字段：`rule_set_hash=H(rule_set)`；`content_hash=H(binding 全DTO−content_hash)`；`manifest_hash=H(coverage 全DTO−manifest_hash)`；`proposal_hash=H({targets, evidence_refs, claim_key, rationale})`。`input_hash` 为调用方供给的 64-hex 摘要，DTO 只固定形式与不可变性：binding 的 input_hash 内容域由受理绑定任务（AF-P3-04）冻结，coverage 的由 prepare 任务（AF-P3-05）冻结；application 的 input_hash 域维持 P2（frozen pair/manifest hash/mode/purpose）不变。

## 评审窗口预算与逐规则覆盖判定

规则定义摘要、事件版本、逐规则结果与 closure 提案经固定服务端适配器进入既有评审窗口，与证据材料共享 32 份/64k 预算；超限 `review_blocked` 且摘要清空，不存在"部分通过"。coverage 语义摘要独立为 `coverage_manifest_hash`，不混入 evidence_set_hash。`coverage_manifest_ref/hash` 与逐规则评审项由服务端从 prepare 回执填入，模型不能自报。check（06 实现，此处冻结判定）要求：manifest 存在且 stage=prepared、hash 一致、binding_ref 等于任务绑定；**凡 manifest 中带 open issue、closure 提案目标、critical 效果或 unbound_new 的条目，必须有对应 `rule_reviews` 项**（以 application_ref 为键）；resolved_with_closure 的 proposal_hash 必须等于 manifest 中匹配提案的 proposal_hash；directional 时 coverage 不得含未评审 critical 事项。缺项/不符 → check fail 落 draft，未决问题原状。

## 容量与存储形状

单 manifest 规则 pair 沿用 2000/4MiB 双上限（entry 数上限在 DTO，字节上限在构造时 canonical_json 实测），溢出显式受限：admission/prepare 前拒绝，或 `scan_capped+note` 如实记录，不得当作全覆盖或"无变化"。`research_rule_binding` 行：PK(id,revision)、owner_id、task_id String36 UNIQUE、listing_id、research_kind、admission_as_of、rules/events_generation、rule_set_hash、input_hash、content_hash、created_at、payload JSON（json_valid/object CHECK）；不可变 UPDATE/DELETE/同 id 重复 INSERT 触发器，task_id 唯一由 UNIQUE 约束承担。`investment_assessment` kind=rule_coverage 形状：account NULL、listing NOT NULL、entry_count NOT NULL≥0、manifest_hash NOT NULL、universe NULL、series 三列 NULL；既有三类形状、uq、全部触发器一字未改；新增部分表达式索引 (owner_id, json_extract(payload,'$.task_id')) WHERE kind='rule_coverage'。

## 052 迁移

沿 051 手式重建机制（drop 触发器→entries 暂存→建 _new→拷贝→drop 旧→rename→索引→触发器）；新建 research_rule_binding；downgrade 前置 `_p3_business_data_exists()`：任一 rule_coverage 行、任一 binding 行、或 investor_publication_dependency 中 `json_extract(payload,'$.ref.kind')='rule_coverage'` 行存在即 RuntimeError。无 P3 数据时 051↔052 往返 P1/P2 payload/hash/entries 逐字节不变（`tests/integration/test_migration_052.py` 固定）。051 文件未改动。

## 开关矩阵

三开关（supplemental_rules_enabled、account_fit_enabled、candidate_ranking_enabled）保持默认 false。任务受理时刻的 supplemental 开关状态决定有无绑定，之后不回溯；开关关闭期间新任务走 legacy 路径且行为与当前逐字节一致；已发布结论的限制检查读取不受开关影响；fit/排序组合沿用各自开关取交集。不新增开关。

## 样本与边界

fixture 覆盖计划要求的六类场景（空规则集绑定/legacy、prepared 多版本、发布成败、新规则/新事件限制、reduce/exit 不受阻断）并附 migration/容量/预算的机读期望；全部合成数据，不产生真实公司或政策的结论。本轮明确不做：admit 钩子、prepare/publish 链改动、依赖解析分支、fit/排序（04–10 的责任）；`SemanticInput` 新字段在本轮只落 DTO，不接线。G0 结论：形状、权限边界、digest 域、预算与超限行为、评审覆盖判定、容量上限、开关矩阵均无悬空决策；与计划的偏离仅三处且已记录——RuleCoverageManifest 增 task_id 字段（承载规定索引路径）、RuleReviewItem 的 proposal_hash 严格配对（计划只规定单侧必须）、共享测试夹具 `tests/support/conversations.py::base` 的 `investor_agent_task_tokens` 由 100000 提至 200000（P3 契约面使每次请求序列化增大约 2KB，原值令 full-research 夹具落在 finish_only 储备翻转线约 1.2KB 之内、末轮工具被隐藏；生产预算与断言语义不变，仅恢复夹具本意）。
