# 行业与同行参照 首期验收日志（WP14，2026-10-12）

本记录保留当时的测试结果，不能作为当前验收结论。后续审查发现批次发布、原件核验及实际引用等六项遗漏，已修复；当前结论与升级说明见[2026-10-13 修复和复验记录](industry-peer-fix-acceptance-2026-10-13.md)。

- **范围**：WP14 开关统一 enforcement 与端到端离线验收（首期最后一个工作包）。基线为 WP1–WP13（`5acff79`）。
- **方法**：逐条核对[技术设计](../product/industry-peer-technical-design.md)"验证重点"清单（§验收与后续开发顺序，27 条）到具体测试；缺一条补一条。端到端场景全部离线：真实 alembic 迁移建库、run-02 固定证据导入、`FunctionModel` 假模型驱动真实 worker，不访问生产库、不调真实 LLM、不发任何在线请求。
- **门禁**：全量 pytest（`DATA_DIR`/`DATABASE_URL`/`API_KEY`/`LLM_TIER3_API_KEY` 覆盖）+ `mypy src`，结果见文末。

---

## 一、开关统一 enforcement 核查与补齐

`industry_peer_context_enabled`（默认关）在三个入口的核查结论与补齐动作：

| 入口 | 核查结论 | 动作 |
| --- | --- | --- |
| API（WP9） | 路由层已按开关返回 `not_supported` 业务状态（200），不触库 | 缺口文本提为共享契约 `industry_peers/contracts.py::FEATURE_DISABLED_GAP`，路由改引该常量 |
| Agent 工具（WP11） | 准入 gate（关→不进白名单、不绑定）与运行时 `prepare` gate（关→从模型工具集摘除）已实现且各自有测试；**缺口：settlement 适配器 `read_peer_context` 不查当前开关**——准入时开的任务在中途关掉开关后，动作重放/直接结算仍能读到绑定值，正是设计"数据用途与运行配置"一节禁止的"把入队时许可布尔值永久当作有效授权" | `agent/peer_tools.py::read_peer_context` 在授权检查之后增加读取时 gate：读当前生效配置（env bootstrap + 运行时 store），关→返回与 API 相同的 `not_supported` 覆盖答案（外层 `missing`、同一 `FEATURE_DISABLED_GAP`、无任何数值），不再读服务 |
| 页面（WP10） | `industry_peers.js` 对 `not_supported` 整区折叠为"尚未覆盖"一行，已有 node 驱动走查 | 无需修改；验收测试把标签映射与服务端返回体一起钉住（见下） |

三入口一致性测试：`tests/integration/test_peer_end_to_end.py::test_flag_off_answers_not_covered_at_api_agent_and_page`
（API 禁用体 == 工具禁用答案 == 同一 gap 文本；页面标签 `not_supported → 尚未覆盖`；准入无工具无绑定；中途关开关的已绑定任务读不到任何值）。
开关打开时仅暴露离线已导入结果：`test_golden_chain_migration_to_agent_tool_reads_one_batch`、`test_flag_on_exposes_only_the_imported_results`
（读取全程 `industry_peer_collect` 管道运行数为 0，无任何在线请求）。

## 二、端到端离线验收场景结论（tests/integration/test_peer_end_to_end.py）

| 场景 | 测试 | 结论 |
| --- | --- | --- |
| golden 全链 | `test_golden_chain_migration_to_agent_tool_reads_one_batch` | **通过**：alembic head（048）建库 → WP2 名单（19 家）→ WP3 年度事实（114 项核对）→ WP6 离线提交 → WP6 service 读 → WP9 API → WP11 工具（带 grant、假模型经真实 worker）。API、grant 绑定与工具返回引用同一批次、同一组逐指标 calculation refs（UUID+revision 逐一相等）、同一组 golden 数值 |
| 越界拒绝 | `test_unbound_peer_read_is_refused_in_the_full_task_flow` | **通过**：假模型读未绑定同行（名单内、自有已提交结果）→ 完整任务流失败，无任何数值到达模型，工具动作无一提交 |
| 伪造 calculation_ref | `test_forged_calculation_reference_is_refused_in_the_full_task_flow` | **通过**：幽灵 calculation 引用进入研究输出 → 端到端进度校验拒绝（可修复反馈、坚持即失败），不产生研究制品 |
| 伪证据拒绝（保存口） | `test_forged_verified_peer_evidence_is_refused_at_the_research_save` | **通过**：模型自供 `verified` 的 peer 来源记录经研究链路同一保存口被拒（`invalid_reference`）；全链 0 条 SavedEvidence |
| 伪证据拒绝（引用口） | `test_cited_forged_peer_evidence_fails_the_task_validation` | **通过**：伪造的同行主体证据行被引用进任务进度 → 端到端校验以 scope mismatch 终止任务（不可修复、无重试绕过） |
| 备份恢复 | `test_backup_restore_preserves_the_peer_chain` | **通过**：`tools/backup_research.py` 备份（含两张新表、57 份 SavedCalculation、`research-runs/` 批次映射制品）→ `tools/verify_backup.py` 校验 → 恢复到全新目录 → 名单/年度快照/SavedCalculation 的 UUID、revision、content/semantic hash、payload 与映射制品逐字节一致；WP6 service 恢复前后读取结果完全相等（同一冻结 cutoff）；run-02 证明的 `original_ref` 全为 None、定位 hash 全部保全（该链路无原件 ObjectRef，原件字节由 DisclosureBlob 随库备份，本链路为空集） |
| 开关（关） | `test_flag_off_answers_not_covered_at_api_agent_and_page` | **通过**：三入口一致"尚未覆盖" |
| 开关（开） | `test_flag_on_exposes_only_the_imported_results` | **通过**：未提交时 missing、提交后 partial，读取永不入队 |

## 三、验证重点对照表（设计 §验收 清单 27 条，全部有测试）

| # | 设计条目 | 覆盖测试（文件 :: 测试名） | 结论 |
| --- | --- | --- | --- |
| 1 | 收入口码误用 | unit/test_peer_calculations :: test_revenue_total_check_is_rejected_as_denominator；unit/test_peer_annual_import :: test_revenue_total_check_is_candidate_without_fabricated_proof | 通过 |
| 2 | 百分比重复转换 | unit/test_peer_calculations :: test_roe_percent_is_never_converted_twice；unit/test_peer_annual_import :: test_roe_conversion_is_exactly_one_division_for_all_companies；unit/test_eastmoney_annual :: test_roe_percent_divided_by_100_exactly_once_with_raw_kept | 通过 |
| 3 | 公司重复 | unit/test_peer_calculations :: test_duplicate_company_is_excluded_wherever_it_appears（WP12 审计去重由 save 路径与 resolver `_audit` 双重把守） | 通过 |
| 4 | 跨行业补样本 | unit/test_peer_calculations :: test_cross_industry_company_outside_the_roster_is_excluded（WP5）；**unit/test_peer_resolver :: test_audit_company_outside_the_roster_is_rejected（WP12 名单成员校验，本次补测）** | 通过（补 1 条） |
| 5 | 年度不一致 | unit/test_peer_calculations :: test_period_mismatch_is_never_mixed_in；unit/test_peer_resolver（记录期间 ≠ 绑定期间拒绝，`_identity` 分支由 test_records_after_the_cutoff_fail 等覆盖载荷校验） | 通过 |
| 6 | 母公司单体表 | unit/test_peer_annual_import :: test_all_114_checks_reproduce_and_trace_to_audit_records（逐项绑定 matched_row 的页码/表头/原值，审计记录同时保留被否决的季度表/其他页 candidates） | 通过 |
| 7 | 无法确认修订兼容 | unit/test_peer_evidence :: test_supplier_update_is_pending_recheck_never_a_correction、test_snapshot_versions_stay_separate；unit/test_peer_service :: test_known_recheck_marks_current_read_stale_history_stays_readable；unit/test_peer_jobs :: test_identical_refresh_reuses_batch_snapshots_and_records（旧快照复用逐项确认） | 通过 |
| 8 | 空值与零值 | unit/test_peer_calculations :: test_null_and_zero_values_stay_distinct；unit/test_peer_evidence :: test_zero_is_a_value_and_missing_stays_unknown；unit/test_peer_annual_import :: test_zero_kept_as_value_and_missing_explicit、test_missing_reason_is_required_for_null_values | 通过 |
| 9 | 亏损目标 | unit/test_peer_calculations :: test_loss_target_keeps_the_peer_median、test_loss_target_saves_median_with_applicability_evidence | 通过 |
| 10 | 负现金流 | unit/test_peer_calculations :: test_negative_cash_flow_is_a_value_not_an_error | 通过 |
| 11 | 少于 5 家 | unit/test_peer_calculations :: test_fewer_than_five_valid_peers_saves_only_coverage；unit/test_peer_service :: test_insufficient_peers_saves_no_statistics | 通过 |
| 12 | 相同值位置 | unit/test_peer_calculations :: test_tie_positions_use_the_midrank_rule | 通过 |
| 13 | HTTP 200 HTML | unit/test_eastmoney_annual :: test_html_at_200_is_a_schema_error_without_retry | 通过 |
| 14 | 分页截断 | unit/test_eastmoney_annual :: test_truncated_pagination_is_an_error_without_retry、test_missing_pagination_information_is_an_error | 通过 |
| 15 | 租约丢失与崩溃重试 | unit/test_peer_jobs :: test_lost_lease_blocks_fact_writes_and_checkpoint、test_crash_resumes_from_checkpoint_without_refetching | 通过 |
| 16 | 并发去重及旧批次晚提交 | unit/test_peer_jobs :: test_enqueue_reuses_active_scope_run_and_blocks_concurrent_duplicate、test_old_generation_submitted_late_never_covers_newer_selection | 通过 |
| 17 | 跨批次混用 | unit/test_peer_service :: test_history_and_generations_never_mix、test_unmapped_newer_records_are_invisible、test_late_old_generation_never_covers_the_newer_one；integration（WP9）:: test_batch_run_id_history_never_splices_the_newer_batch；unit/test_peer_jobs :: test_roster_change_after_enqueue_does_not_enter_the_batch | 通过 |
| 18 | 已知更正 | unit/test_peer_evidence :: test_explicit_correction_basis_connects_refs、test_supplier_update_is_pending_recheck_never_a_correction；unit/test_peer_service :: test_known_recheck_marks_current_read_stale_history_stays_readable；unit/test_peer_wp13 :: test_current_limits_answers_the_new_recheck_situations | 通过 |
| 19 | 任务 cutoff | unit/test_peer_service :: test_batch_submitted_after_cutoff_is_invisible、test_record_created_after_cutoff_is_excluded_per_metric；unit/test_peer_resolver :: test_records_after_the_cutoff_fail；unit/test_peer_agent_tool :: test_historical_cutoff_stays_unbound_and_a_new_message_rebinds、test_records_created_after_the_cutoff_never_enter_the_old_task | 通过 |
| 20 | 伪造 calculation_ref | unit/test_peer_resolver :: test_fabricated_or_unmapped_calculation_refs_are_rejected；integration/test_peer_end_to_end :: test_forged_calculation_reference_is_refused_in_the_full_task_flow（e2e） | 通过 |
| 21 | 读取未绑定同行 | unit/test_peer_agent_tool :: test_out_of_scope_target_fails_closed、test_adapter_rejects_ticker_outside_the_bound_target；integration/test_peer_end_to_end :: test_unbound_peer_read_is_refused_in_the_full_task_flow（e2e） | 通过 |
| 22 | 比较任务前置拒绝 | unit/test_peer_resolver :: test_progress_pre_check_refuses_mixed_fake_and_peer_facts、test_unbound_peer_calculation_keeps_the_pre_check_rejection、test_comparison_mixing_unbound_peer_facts_is_refused、test_comparison_without_bound_references_keeps_the_original_rule | 通过 |
| 23 | 原始反证要求 | unit/test_peer_resolver :: test_counter_evidence_cannot_be_peer_statistics | 通过 |
| 24 | 关键同行依赖的续期拒绝 | unit/test_peer_wp13 :: test_critical_peer_dependency_blocks_extension_with_structured_reasons、test_monitor_cycle_unchanged_review_is_refused_for_peer_dependencies、test_reading_the_old_summary_does_not_renew | 通过 |
| 25 | 来源摘要超过限额 | unit/test_peer_wp13 :: test_material_window_still_blocks_with_digests_counted、test_material_character_limit_blocks_with_a_bounded_digest_set（32 material / 64000 字符） | 通过 |
| 26 | 旧评分与旧授权兼容 | unit/test_peer_storage :: test_execution_grant_defaults_peer_context_to_null、test_research_snapshot_row_keeps_a_null_binding、test_industry_peer_context_defaults_to_off；unit/test_peer_agent_tool :: test_legacy_grant_payloads_stay_serialization_compatible；unit/test_peer_api :: test_snapshot_without_binding_stays_null、test_run_detail_returns_binding_null_and_verbatim、test_http_submit_cannot_set_the_binding；unit/test_peer_wp13 :: test_report_views_do_not_consume_peer_results；integration/test_peer_end_to_end :: test_flag_off_answers_not_covered_at_api_agent_and_page（WP14） | 通过 |
| 27 | 备份恢复后的原件读取 | integration/test_migration_048 :: test_pre_migration_backup_restores_and_reapplies（WP1 迁移/备份演练）；integration/test_peer_end_to_end :: test_backup_restore_preserves_the_peer_chain（WP14 e2e） | 通过 |

对照计划 §8 的映射无冲突；其中第 4 条 WP12 名单成员校验在本次验收前没有直接测试，已补（见上）。

## 四、阶段退出条件核对（设计第 11 节）

| 阶段 | 退出条件 | 核对 |
| --- | --- | --- |
| A 固定证据接入与计算（WP1–WP6） | 19 家名单及 114 项核对可追溯，三项统计复现，单位、排除规则、迁移和备份恢复通过 | unit/test_peer_cohort_import :: test_golden_import_matches_run02_sample / test_reimport_identical_content_appends_no_revision / test_unresolvable_codes_fail_the_whole_batch_without_writes；unit/test_peer_annual_import :: test_golden_import_matches_sample_exactly / test_all_114_checks_reproduce_and_trace_to_audit_records / test_moutai_golden_roe_fraction 及防篡改五连测；unit/test_peer_calculations :: test_golden_statistics_reproduce_for_maotai / test_every_golden_peer_row_reproduces；unit/test_peer_storage（唯一键并发去重、不可变约束、ObjectRef/枚举/Quantity 契约）；integration/test_migration_048（升降级、fresh 对照模型、迁移前备份回退）；integration/test_peer_end_to_end :: test_backup_restore_preserves_the_peer_chain —— **达成** |
| B 后台补采（WP7–WP8） | 假源和已保存响应验证部分失败、幂等、重试及修订；在线采集不在本拆分内验证 | unit/test_eastmoney_annual 全套（schema 错误不盲重试、429/Retry-After、响应复用、熔断、主机限定）；unit/test_peer_jobs（检查点崩溃恢复、租约、并发去重、generation、partial 逐公司原因、24h 冷却准入）—— **达成**（在线验证按 §10 留待启用条件） |
| C 页面接入（WP9–WP10） | 无评分也能读参照，历史不串新结果，未知及不足样本明确可见 | unit/test_peer_api :: test_reads_work_without_any_research_snapshot / test_batch_run_id_history_never_splices_the_newer_batch / test_unknown_target_is_404 / 状态枚举与边界；integration/test_web_stock_peer_section（四态走查：not_supported 折叠、旧快照不回填、golden 展示格式、仅两个只读端点）—— **达成** |
| D Agent 接入（WP11–WP14） | 离线假模型验证页面与工具引用一致、越界及伪证据被拒绝；真实启用另做用途检查与人工评阅 | unit/test_peer_agent_tool、unit/test_peer_resolver、unit/test_peer_wp13 全套 + 本文第二节的 8 个 e2e 场景 —— **达成**（真实启用按 §10 留待启用条件） |

## 五、golden 数值（本次 e2e 在迁移库上复现，三入口一致）

| 指标 | 茅台目标值 | 同行中位数 | 位置 | 有效/候选 |
| --- | --- | --- | --- | --- |
| ROE | 0.3253（32.53%） | 0.0739（7.39%） | 94.44% | 18/18 |
| 合同负债/营业收入 | 0.0474（4.74%） | 0.1630（16.30%） | 16.67% | 18/18 |
| 经营现金流/归母净利润 | 0.7474 | 0.5792 | 73.33% | 15/18 |

排除：现金流比剔除 000799.SZ / 000860.SZ / 600199.SH（非正分母）。整体状态 `partial`（run-02 证据无日期依据，不物化 EvidenceRecord，核对覆盖如实为空）。

## 六、遗留事项（不在此实现，引用原设计）

- **在线采集启用条件**：候选来源的自动获取、长期保存、备份与展示条件按 `research/industry-data-validation/evidence/run-02/rights-matrix.json` 落实，并通过真实年度请求的有限验收后才启用（技术设计 §数据用途与运行配置；计划 §10）。当前 `DEFAULT_PROVIDER/ADAPTER` 的适配器仅以假源与已保存响应验证。
- **真实 Agent 启用条件**：明确允许发送给外部 LLM 的数据范围，解释质量人工评阅通过后启用（技术设计 §验收与后续开发顺序、§数据用途与运行配置；计划 §10）。当前仅离线假模型（`FunctionModel`）契约验收。
- 2025 固定样本结论不外推其他行业/全市场/完整历史；名单变更、多市场、同行续期监控为后续设计项（计划 §10）。

## 七、WP14 修改清单

| 文件 | 修改 | 理由 |
| --- | --- | --- |
| `src/investment_agent/industry_peers/contracts.py` | 新增 `FEATURE_DISABLED_GAP` 共享 gap 常量 | 三入口禁用文案收敛为一个契约，防止漂移 |
| `src/investment_agent/api/routes/industry_peers.py` | `_NOT_ENABLED_GAP` 改引共享常量（文案不变） | 同上 |
| `src/investment_agent/agent/peer_tools.py` | `read_peer_context` 增加读取时开关 gate（关→`not_supported` 答案，无数值无服务读）+ `DISABLED_COVERAGE` 文案 | 补 WP11 缺口：settlement 不再信任准入时冻结的许可布尔值，按当前配置作答（设计"数据用途与运行配置"） |
| `tests/unit/test_peer_agent_tool.py` | `peer_env` 夹具钉住 env 开关；禁用准入测试显式传关开关的 settings | settlement 现在核查进程 env，测试环境与 `base(**FEATURE)` 对齐 |
| `tests/unit/test_peer_wp13.py` | `peer_env` 夹具同样钉住 env 开关 | 同上 |
| `tests/unit/test_peer_resolver.py` | 新增 `test_audit_company_outside_the_roster_is_rejected` | 验证重点第 4 条 WP12 名单成员校验此前无直接测试 |
| `tests/unit/test_peer_service.py` | `_after_everything()` 从固定 `2026-10-12T23:59Z` 改为时钟推导（now+6h） | 既有测试的定时炸弹：可信保存路径以真实时钟盖 `created_at`（`EvidenceService.save_calculation`），固定"晚于一切"截点在日历越过 2026-10-12 后变成过去时，`test_batch_submitted_after_cutoff_is_invisible` 于 2026-10-13T00:00Z 起确定性失败；改为 resolver/agent-tool 夹具既有的相对时钟约定 |
| `tests/integration/test_peer_end_to_end.py` | 新建，8 个端到端离线验收场景 | WP14 交付物 |

未修改 `web/` 下任何用户未提交文件；未触碰 `.dev/` 与真实数据目录（e2e 全部在临时 `DATA_DIR`）；未启用任何在线请求。

## 八、门禁结果

| 门禁 | 结果 |
| --- | --- |
| 全量 pytest（env 覆盖 `DATA_DIR` / `DATABASE_URL` / `API_KEY` / `LLM_TIER3_API_KEY`，2026-10-13 08:10 +08:00 运行） | **3248 passed, 3 skipped, 6 deselected, 0 failed**（7m01s；基线 3239 passed / 3 skipped + 本次净增 9 个用例 = 8 个 e2e + 1 个 WP12 名单成员校验） |
| `.venv/bin/mypy src` | **Success: no issues found in 329 source files** |

首次全量运行（2026-10-12 23:52 UTC 起）曾出现 1 个失败：`test_peer_service :: test_batch_submitted_after_cutoff_is_invisible`
—— 即第七节所列既有测试的固定截点定时炸弹在 UTC 越过 2026-10-12T23:59 后触发，修复后全量复跑全绿。
