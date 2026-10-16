# P2 提交归档记录

日期：2026-10-17（Asia/Shanghai）。用户在P2实现及复审验收后明确授权整理并提交，仅包含本任务改动；没有授权推送、推进P3/P4或启用生产开关。本记录与P2实现、修复及验收文档进入同一Git提交，提交号通过Git历史查询，避免文档自引用提交哈希。

## 提交范围

- P2十五项实现：`supplemental_rules`契约、来源、匹配、机械评估、生命周期、命令受理、worker、恢复、读取投影；051迁移、五张规则/事件表及共享assessment扩展；API、管理页、A股单股只读面板与默认关闭的配置接缝。
- 后续十四组问题/边界修复及对应回归测试，详见[复审记录](followup-review-2026-10-16.md)。P0/P1共享接缝只包含本次必要兼容改动，原有评分、排序及正式发布语义保持原阶段边界。
- P2计划、冻结、执行、验收、复审、验证摘要与独立合成浏览器截图；主设计和P0/P1索引只补充本次交付状态及链接。
- 旧测试的必要适配：051当前head、worker/settings/import graph注册、历史来源时间夹具、通用research接口隔离、非A股页面边界，以及新增面板后的P1浏览器等待与点击同步。`tests/browser/account_fit.mjs`是本任务兼容验证修复，虽含390宽度用例，并非并行移动端业务改动。

明确排除`mobile/`全部未提交改动、`src/investment_agent/web/static/shell.js`的Android返回桥改动、`.playwright-mcp/`、`.zcode/`及生产数据库/运行时设置。已有并行提交保留在原分支历史，不纳入本次提交差异；本任务不回退或重写这些提交。

## 快照与证据核验

最终复审源码清单覆盖`src/investment_agent`、`tests`、`alembic`共1187个文件，清单SHA256为`c4bbadb970eaa40d3725dc455f020ee7bcc02368a3760d27fefb1088be69c589`。归档前逐文件核对，P2源码与该快照一致，工作区仅有排除的并行`shell.js`差异。核对期间移动端独立提交了该差异，本次保留其提交且不将其计入P2变更。暂存区检查另发现11个新增Python文件末尾多余空行，已仅清理这些空行并重新补验；最终index相对复审快照仅有这11处空白清理及上述并行文件差异。

11处空白清理为`supplemental_rules/`下的canonical、evaluation、event_sources、issues、matching、metric_adapter、metric_evaluation，以及`tests/integration/test_supplemental_source_boundaries.py`和`tests/unit/`下的test_supplemental_evaluation、test_supplemental_inputs、test_supplemental_matching。它们没有语义修改。最终暂存文件白名单共95项，不含移动端或shell.js。

已核对[最终验证摘要](review-validation-summary.json)中unit、integration、root、focused的JUnit、日志与收集清单，以及浏览器日志，实际文件SHA256均与记录一致。全量验收仍以该摘要为准：unit 2435 passed / 2 skipped / 6 network默认排除，integration 732 passed，root 717 passed；专项与完整分区重叠，不累加。原始日志位于摘要所列本机`/tmp`路径，并非仓库内永久归档；仓库保存的是结果、原因、命令、哈希与测试源码，临时日志后续可能被清理。

首次验收摘要、执行及复审文档中的“未提交”保留为当时状态，不应解释为本次提交授权仍未发生。此次仅整理文档、清理末尾空行及归档，没有再次修改业务逻辑。

## 提交前补验

- P2全部单元、集成、十四组复审回归，加051、API/Web、导入图，以及所有本次改动的旧Python测试：**214 passed，12 warnings，49.76秒**（末尾空行清理后重跑）。警告均为既有SQLite表达式索引反射警告；实际DDL及EXPLAIN验证已包含在相关回归中，没有隐藏警告或放宽检查。
- 命令：`.venv/bin/python -m pytest -q tests/unit/test_p2_review*.py tests/integration/test_p2_review*.py tests/unit/test_supplemental*.py tests/integration/test_supplemental*.py tests/integration/test_migration_051.py tests/integration/test_api_research.py tests/integration/test_web_supplemental_rules.py tests/unit/test_import_graph.py tests/unit/test_settings_registry.py tests/unit/test_settings_store.py tests/unit/test_job_dispatch.py tests/unit/test_candidate_ranking_sources.py tests/unit/test_material_release_audit.py tests/integration/test_migration_050.py tests/integration/test_web_stock_financials.py --junitxml=/tmp/zcharlie-p2-commit-verification.xml`；日志`/tmp/zcharlie-p2-commit-verification.log`。
- Ruff `src tests alembic`通过，`alembic heads`为051单head，Git差异空白检查通过。
- 本次未重复运行完整分区及浏览器；其验收记录对应上述复审快照，归档差异已单独说明，提交前补验不替代或重复累计原全量结果。

生产数据库未迁移，`account_fit_enabled`、`candidate_ranking_enabled`、`supplemental_rules_enabled`默认保持false；未写入生产设置，未推进P3/P4，未推送。
