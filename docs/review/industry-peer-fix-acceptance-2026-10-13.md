# 行业与同行参照修复和复验记录

日期：2026-10-13。结论：六项审查问题已修复，首期**离线功能验收通过**。持续在线采集、来源用途确认和真实外部 Agent 的研究质量评阅仍为独立启用条件。依据为[首期范围](../product/industry-peer-scope-draft.md)和[技术设计](../product/industry-peer-technical-design.md)。

## 六项修复

| 问题 | 修复 | 对应验证 |
|---|---|---|
| 回滚的映射文件成为当前结果 | 复用 InvestorMutation 保存 owner、批次 ID 和映射 hash 的不可变提交收据；查询、引用和续跑均要求收据。证据保存点之前建立真实 SQLite 外层事务，回滚不残留发布资格 | 完整且 hash 正确的文件保留在磁盘，回滚后当前读取仍为原批次，显式读取该文件返回 missing |
| 旧任务被重新编号，覆盖较新批次 | handler 使用入队冻结的 generation。终结时按检查点的具体快照 ID 构建输入，失败复用入队时的旧快照，不重新查最新版本 | 真实 handler 的 generation 1 在已发布 generation 2 后结束，仍为 1，当前选择保持 2 |
| 无原件仍标记 verified | JSON 对照本身只产生 candidate；读官方元数据与实际 PDF，核对 hash、发行人、年度、字段、当期值、单位及报表范围，再保存原件和证明。保存及引用重新校验真实字节 | 替换表头、片段或同步篡改 JSON 数值均被拒；缺原件保持 candidate；114 个核心字段在真实原件上通过 |
| 固定样本现金流比无法引用 | 从已有官方公告元数据补真实披露日期，登记 19 份原件，物化 114 条字段证据；亏损排除仍必须引用归母利润事实 | 茅台三项指标全部输入 verified；三家亏损同行保留排除依据；酒鬼酒目标不适用但同行中位数可引用；真实 worker 的假模型输出三项 NumericNode，现金流比渲染为 0.7474 |
| 移出名单后退回旧版本 | 先选每个名单的最新已知版本，再判断目标成员资格；历史查询按已提交批次的固定名单读取 | 移除后当前 not_supported，旧批次仍可查看，较早 cutoff 不吸收新名单 |
| 普通研究提交不保存绑定 | 开关开启时服务端在入队入口读取并冻结绑定；客户端不能自供绑定。幂等重试复用原 payload、cutoff 和批次，写入前取得事务预约 | 已有参照的普通提交携带绑定，刷新后的相同幂等请求沿用原绑定；既有 worker、快照与 run detail 测试覆盖保存传递 |

复验还补上两个关联边界：已核对的统计值必须与实际字段事实重算一致；普通个股原始年度事实引用也检查原件失效、已知修订和冲突。原始反证要求、派生统计的 output/inference 要求、任务授权、语义材料限额与关键同行依赖续期拒绝均保留。

主要回归位于[test_peer_review_regressions.py](../../tests/unit/test_peer_review_regressions.py)，端到端证据位于[test_peer_end_to_end.py](../../tests/integration/test_peer_end_to_end.py)。备份恢复测试读取恢复库里的 19 份真实 PDF，逐一比对字节 hash，并重新检查核心字段证明；不再以空原件集合代替原件读取验收。

## 门禁和复审

- 全量 pytest：**3259 passed，3 skipped，6 deselected，0 failed**，隔离 DATA_DIR、DATABASE_URL 和 API/LLM 测试凭据，耗时 9 分 20 秒。
- 跳过的 3 项需要外部 zclaudia-gateway 构建环境；6 项联网测试按仓库默认规则排除。该结果不代表在线数据接口或真实 LLM 已验收。
- 引用、发布、续期、真实 worker 与备份重点复验：63 项通过；原件与导入重点复验覆盖了旧断言更新后重新执行的全量门禁。
- mypy：330 个源文件，0 错误；修改范围的 Ruff 和 diff whitespace 检查通过。
- 最后复审核对了事务/文件一致性、失租约时回滚、generation、名单当前与历史分离、原件与字段绑定、统计与事实一致性、原始事实引用、普通研究绑定及重试。没有发现阻止上述离线验收的遗留问题。

没有执行生产迁移、修改真实数据目录、访问在线数据源或调用真实 LLM。工作区已有移动端、导航与页面样式修改保持原样。

## 已有数据升级

无需新增数据库表或迁移。本次复用原件库、不可变证据/计算和操作收据。

旧快照不会被原地升级为 verified，旧映射文件也不会因文件存在而补发提交收据。需要在选定环境中重新导入固定样本，再提交新离线批次：

```sh
ia peer-annual import --originals-root research/industry-data-validation/evidence/run-02
ia peers submit-offline-batch --report-period 2025-12-31
```

命令沿用该环境的 DATABASE_URL 和 DATA_DIR；已有白酒名单可直接复用。独立部署需要显式提供 sample、audit 和 originals-root 的路径。目录必须包含原件 PDF 和公告元数据；缺原件时仅保存 candidate，不能进入关键主张。

修复门禁阶段没有执行上述数据升级。后续已按用户要求在本地开发环境完成固定样本导入和新批次提交，结果见下节。历史研究没有保存绑定时仍显示未保存，不回填最新数据；更新后新研究冻结新批次。开启在线获取或真实外部 Agent 前，仍需完成对应来源用途与人工质量验收。

## 本地固定样本升级复验

2026-10-13 23:23 起，在 `.dev/data/app.db`（schema 048）执行固定样本升级，通过现有本地 API `http://127.0.0.1:8002` 验证页面。8001 的 Docker 服务仍为旧版本，本次没有部署或升级该环境。本地库原先没有同行名单和年度快照，因此先补齐首次导入条件：按现有 `stock_directory` 的精确代码为 11 家公司建立 listing 身份，再导入 19 家白酒固定样本名单。

导入前已生成[备份清单](../../.dev/backups/peer-before-import-2026-10-13/manifest.json)，175 个文件的大小和 SHA-256 全部通过校验。数据库、研究制品和通知均在备份内。导入沿用本地已有 schema，无数据库迁移。

| 项目 | 实际结果 |
| --- | --- |
| 年度数据 | FY2025，19 条年度快照；114 个核心字段通过实际 PDF 原件验证 |
| 原件读取 | 19 份 PDF 的数据库字节 hash 正确；经本地鉴权下载接口读取，全部 HTTP 200，附件字节与 hash 一致 |
| 已提交批次 | `peer-offline-20261013T152301Z-ed35f362`，generation 1，success |
| 批次内容 | 19 个目标，57 条计算记录，0 个指标缺口；映射与数据库提交收据可读 |
| 页面验证 | 1440px 桌面、390px 手机；三项固定数值一致，19 家明细、计算记录和 verified 证据详情可读；无 JavaScript 报错、无横向溢出 |
| 不适用目标 | 酒鬼酒的经营现金流 / 归母净利润显示目标不适用，保留 16/18 个有效同行 |
| 关键引用校验 | 在本地实际数据库中对茅台、酒鬼酒的六项计算执行 `require_verified_inputs=True`，全部通过 |
| 假模型 Agent | 在升级后的数据库副本中运行 FunctionModel 和真实 worker，任务 succeeded；三项引用与页面 API 完全一致，保存回答包含 `0.7474 ratio`、`4.74%`、`32.53%` |

茅台实际页面数值：ROE 为 **32.53% / 7.39% / 94.44%**，合同负债收入比为 **4.74% / 16.30% / 16.67%**，现金流利润比为 **0.7474 / 0.5792 / 73.33%**；每组三项依次为目标值、同行中位数、升序位置。有效同行数依次为 18/18、18/18、15/18。

本地只开启 `industry_peer_context_enabled`，用于暴露已提交的离线结果；`investor_agent_enabled` 保持 false。实际库的 Agent 任务数与导入前同为 2，同行在线采集任务前后同为 0。假模型任务仅存在于 `.dev/peer-upgrade-2026-10-13/agent-sandbox/app.db`，仅执行该任务的 run ID，IPv4/IPv6 socket 连接全部被阻断；副本单独采用假凭据、有限输出预算和 explanation 任务类型，保留原件核验、任务授权及引用校验。

浏览器复验发现并修复一个既有股票页错误：`app.js` 调用未导入的 `loadBars`。现已导出并复用 `watchlist.js` 的本地日线读取函数；桌面和手机浏览器重新通过，相关页面测试 **48 passed**，两项 JS 语法检查和 `git diff --check` 通过。

本地可复查材料：[年度导入](../../.dev/peer-upgrade-2026-10-13/annual-import.json)、[批次提交](../../.dev/peer-upgrade-2026-10-13/batch-submit.json)、[页面复验](../../.dev/peer-upgrade-2026-10-13/page-check.json)、[原件下载复验](../../.dev/peer-upgrade-2026-10-13/original-download-check.json)、[Agent 引用复验](../../.dev/peer-upgrade-2026-10-13/agent-check.json)。这些本地运行材料和备份保存在 `.dev`，不加入 Git。
