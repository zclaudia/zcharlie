# 银行跨年度原件与修订复核验收

日期：2026-10-17。基线：`b75d5f2`。用户授权 GPT-6 Luna 子代理实施，主代理独立验收。实施拆分见 [R1–R6 计划](../product/plans/industry-expansion/bank-rollover-plan.md)。本轮没有提交，也没有修改、回退或纳入并行 P4 文件。

## 实现结果

三个 Luna 子任务分别完成官方原件获取、跨年度字段核验、年度核验与复核 CLI；主代理完成在线更新接入、可信复核状态、独立验收及时间边界修复。

| 子任务 | 交付与实际行为 |
| --- | --- |
| R1 原件获取 | 以保存的上市主体和巨潮官方目录 orgId 发现完整年度报告，支持沪深两市、有限分页、请求及字节上限。摘要、英文版、H股报告及仅说明更正的公告排除；多份完整修订版全部保留为歧义候选，不自动选择并验证字段。原始查询响应、请求与哈希保存在可备份的 research-runs 下；PDF 保存到现有原件、Blob 存储。 |
| R2 字段核验 | 独立 `bank-annual-original/2`，不替换既有 FY2025 v1 特例。核对实际证券、报告首页年度、当期列、集团范围、行标签、数值、倍率、脚注、原件哈希和时间；读侧重新校验 PDF。上一年度比较列不能充当该年度自己的报告。无法确定的布局保留候选与拒绝原因。 |
| R3 管理入口 | `ia bank-annual acquire/prepare/verify/reviews list/resolve`。prepare 生成完整字段模板；人工填写的 verified 仅是待重验声明。核验生成不可变快照；复核事件与新结果在同一事务入库，精确重复操作幂等。 |
| R4 更新闭环 | 在线任务将差异和旧/候选快照绑定，原子保存到数据库复核队列。同一已否决的供应商响应再次出现时，重新核验确认过的原件；新供应商版本、响应或数值不能借用该否决。新的未决更新优先于以前的复核结果。 |
| R5 修订复核 | reject_supplier 必须由原件确认旧值；accept_original 必须由另一份同年、正式更正/修订的完整报告确认候选值，且原件、内容哈希、公告身份独立，披露不早于原报告。读侧再次验证结果，不信任单独的 resolve 收据。旧快照与旧批次不回写。 |

实际联网下载以完成时刻写入 acquired_at/available_from；调用者给的历史时间只限制发现截止，不能把今天下载的原件回填成历史已获取。字段核验时间必须不早于原件实际获取时间，也不得晚于快照或查询 cutoff。当前年份尚未披露年度报告时显式 missing，不访问 HTTP。

## 独立原件实证

主代理通过官方来源做了三次有限 HTTP 请求：目录、工行 FY2024 公告查询、完整 PDF。正式获取服务的整链验收重放这些实际响应字节，并校验三次请求及缓存零新增请求；这不等同于生产在线整链已启用。

FY2024 来源：[工商银行2024年度报告](https://static.cninfo.com.cn/finalpage/2025-03-29/1222948914.PDF)，公告 1222948914，410 页，SHA-256 `27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c`。FY2025 使用既有独立完整原件，未用其比较列替代 FY2024 原件。主代理渲染查看 FY2024 PDF 第16页和第207页，复核指标列、集团/本行列和单位。

| 字段 | FY2024 | FY2025 | 原件位置与处理 |
| --- | --- | --- | --- |
| 年度加权 ROE | 9.88% | 9.45% | 各自第16页，保留年度加权定义，排除扣非行 |
| 净息差 | 1.42% | 1.28% | 各自第16页“净利息收益率”，不取净利息差行 |
| 不良贷款率 | 1.34% | 1.31% | 各自第16页及范围、脚注 |
| 拨备覆盖率 | 214.91% | 213.60% | 各自第16页，转换为 fraction 仅一次 |
| 归母净资产 | 3,969,841 百万元 | 4,244,259 百万元 | 第207/204页，“本集团”当期归母权益，转换为 CNY；不取本行或总权益 |

10 项均经 CLI verify 和实际数据库/PDF 读回通过；重复核验保持两个年度原始快照。FY2024 批次仍使用固定10家名单，其他9家缺数据，明确为部分覆盖，不输出完整行业统计。

修订整链使用明确标注的受控供应商差异（214.92%），不是发现了工行官方修订。真实 FY2024 原件确认214.91%，经 reject_supplier 后，新批次四项严格引用通过；历史批次数值不变。accept_original 的成功及负例由可读取的小型测试 PDF 和 SQLite 覆盖，不宣称已获取某银行真实官方修订版。

## 使用顺序

先导入银行在线名单，再对其保存的 listing_id 操作；数据库与 DATA_DIR 应按部署环境显式配置。

```sh
ia bank-annual acquire --cohort bank_run02_sample --listing-id <id> --period 2024-12-31 --data-dir <DATA_DIR>
ia bank-annual prepare --cohort bank_run02_sample --listing-id <id> --period 2024-12-31 --output <audit.json>
# 人工逐字段填写原件页码、当期列、范围、原值、倍率、摘录、哈希、脚注与实际核验时间。
ia bank-annual verify --cohort bank_run02_sample --listing-id <id> --period 2024-12-31 --audit <audit.json>
ia bank-annual reviews list --listing-id <id> --period 2024-12-31
ia bank-annual resolve <review-id> --audit <review-audit.json> --decision reject_supplier
# 若正式修订原件确认候选值，使用 accept_original，并填写 revision_basis 和 source_version。
ia peers collect --cohort-key bank_run02_sample --report-period 2024-12-31 --mode refresh --now
```

读取、核验、复核均不隐式联网；获取来源仅由显式 acquire 执行。新年度需要自己的原件和审计，既往年度 verified 不继承到新年度。

## 验证记录

| 检查 | 结果 |
| --- | --- |
| 五个新增单元模块 + 独立跨年度整链最终复验 | 49 passed，21.16s；包含实际两年度10字段、未来年度零HTTP、版本差异重开、复核、严格引用、历史读取、API与备份恢复；CLI不会以候选B作为重新核验A的旧基线 |
| 既有银行在线、原件协调、48/2导入、双行业和个股页面整链 | 19 passed，250.93s |
| 获取时间与首页年度进一步收紧 | 来源17项单测及真实跨年度整链均再次通过，包含于最终49项 |
| 备份恢复 | 调用现有 backup_research.py 与 verify_backup.py 后复制到独立目录，用新 SQLite engine 重开；两份PDF、所有verified字段版本、复核事件、新批次四项严格引用及旧批次历史值通过 |
| 完整 tests/unit 工作区快照 | 2610 passed、2 skipped、6 network deselected，384.67s；两个skip为忽略目录中的海外原件缺失，本轮真实银行验收已执行 |
| 本轮10个源文件类型与质量检查 | mypy零错误；17个修改/新增Python文件 Ruff 与 diff whitespace 检查通过 |
| 全仓 mypy 最终工作区快照 | 458个源文件中59条错误，分布于7个并行P4文件；未纳入本轮修复 |

可复现入口：`tests/integration/test_bank_rollover_acceptance.py`，以及 `tests/unit/test_bank_annual_sources.py`、`test_bank_annual_v2.py`、`test_bank_annual_review.py`、`test_bank_review_states.py`、`test_bank_rollover_cli.py`。真实原件整链依赖本地忽略目录中的精确输入；缺输入时明确 skip，不能将 clean checkout 的跳过视作重新通过实证。小型 PDF 的防伪收据、错年、前视时间、普通报告冒充修订、旧披露修订、候选值不符等测试不依赖这些下载件。

开发门禁日志保存在 `/tmp/zcharlie-bank-rollover-root/`：`delivery.log`、`compat.log`、`unit-final.log`、`final-mypy-owned.log`、`final-mypy-global.log` 和 `final-ruff.log`。完整单元采集测试集后的最后CLI基线修复另外由最终49项专项覆盖，不将两个测试集简单相加为独立用例总数。

全仓 mypy 早期453文件快照曾通过，随后并行P4持续变更，最终458文件快照为59条错误，涉及 `evidence/disclosure_probe.py`、`evidence/disclosure_profiles.py` 和 `supplemental_rules/disclosure_authorization.py`、`disclosure_campaign.py`、`disclosure_runner.py`、`disclosure_schedule.py`、`disclosure_updates.py`。本轮未改这些文件，也未放宽检查；因此银行本轮验收通过不等于整个并行工作区可以发布。

## 后续数据与运行条件

本轮实现可执行工作流并验证了工行两个年度，不表示10家银行所有历史年度已完成原件采集或字段核验。下一步按明确银行/年度矩阵批量执行 acquire、逐份审计、verify 与刷新；遇特殊布局补充有实际原件依据的规则，保留拒绝原因。旧版已有候选快照不会被自动改写为新复核事件，应通过本轮更新/核验生成受绑定的待复核项。生产来源用途条件、定时启用及真实 Agent 质量评阅沿用既有独立门禁。

本轮文件范围：5个新增源模块、5个既有源文件的小范围接入、7个测试/辅助文件，以及本验收、计划和索引文档。未改迁移、P4 HTTP 扩展、supplemental_rules、worker/scheduler、移动端及并行页面。
