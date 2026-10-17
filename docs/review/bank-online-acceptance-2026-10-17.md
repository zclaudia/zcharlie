# 银行在线年度采集与更新验收

日期：2026-10-17（Asia/Shanghai）。按用户要求，由三个 GPT-6 Luna 子代理实现适配器、任务与命令、原件重验；主代理负责来源探针、读侧绑定、整链测试、审查与验收。[执行计划](../product/plans/industry-expansion/bank-online-plan.md)保留 O1–O6 分工。

## 实现范围

银行固定10家研究名单可按一个完整年度显式采集、刷新和恢复任务。新增 `bank-annual-v1` 来源版本；四项比率映射为 `ROEJQ`、`NET_INTEREST_MARGIN`、`NONPERLOAN`、`BLDKBBL`，percent 只除100一次。每家公司仅请求一次 `RPT_F10_FINANCE_MAINFINADATA`，精确限定证券代码和12月31日期别，核验银行类型、年度报告类型、币种、页数、披露日期和字段结构。供应商响应版本、原始值/单位、披露/修订日期、请求参数及响应字节哈希均保留。

归母净资产没有可靠在线映射，明确保留 `unverified_mapping_reconcile_annual_report`。同年已有可信原件时，重新读取存储的 PDF、校验主体/归属/年度/范围/字段后使用原件值；不把总权益、普通股权益或资本指标猜作归母权益。新年度缺少原件时，比率保持候选、归母权益保持缺失，不能继承上一年度 verified。

供应商比率与同年原件相符时也要重新核验原件字节，并独立保存 `supplier_comparison`。数值变化或缺失保留 candidate，记录 `original_comparison` 和字段级待复核原因；不改写旧快照，不把新数值直接声明为集团口径。平安两项已知单体指标只有在当前供应商值仍与真实年报排除行一致时才继续排除，否则转待复核。

在线读取需要新的不可变名单 revision 显式绑定 `read_scope=eastmoney/bank-annual-v1`。旧名单保持原离线来源；自动准入返回 `offline_only`，给出升级命令且不入队。切换后的当前查询不会悄悄回退旧来源。显式历史批次及研究 cutoff 继续固定旧名单、旧快照和原件。不同来源版本之间不比较 generation 大小。

原始响应缓存、限流重试、任务范围去重、generation、租约事务、公司检查点和冻结批次沿用现有机制。相同来源字节与相同证明不会因采集/核验时间更新而追加快照；来源版本、原始字节或证明变化可以形成新版本。GET 和 Agent 工具保持只读。

## 有限真实来源验证

2026-10-17 09:46（上海时间）对固定10家 FY2025 各执行一次真实请求，正常 TLS 校验，15秒超时、无重试、至少1秒请求间隔：**10次请求、10个HTTP 200、10家解析成功、0失败**。40个比率数值与此前年报审计清单一致（按 Decimal 比较）；这仅说明供应商数值匹配，48项 verified 仍须来自独立 PDF 重验。平安两项仍为单体排除。

原始请求、返回字节、抓取时间和 SHA-256 位于本地忽略目录 `research/industry-data-validation/evidence/bank-online-2026-10-17/root/live/`，汇总为 `probe-results.json`。最终解析器对这10份原始字节离线重放，确认保留根对象 `version`，没有增加网络请求。

此次有限开发探针不代表已取得长期自动化采集、持久缓存、展示及外部模型使用的全部上游许可。来源用途条件仍需单独确认，参见[东方财富服务协议](https://about.eastmoney.com/home/protocol)。本轮没有启用生产调度、修改真实数据库或调用真实 LLM。

## 使用方式

先在与研究相同的数据库中完成证券目录、名单及所需年度原件导入。以下命令只演示银行在线切换与显式采集；`--now` 会访问真实供应商，应在准备好的环境中执行。

```sh
ia peers import-roster research/industry-data-validation/evidence/run-02/sample.json --industry bank --bank-online
ia peers collect --cohort-key bank_run02_sample --report-period 2025-12-31 --now
ia peers collect --cohort-key bank_run02_sample --report-period 2025-12-31 --mode refresh --now
ia peers admit 002142.SZ
```

`collect` 不带 `--now` 只入队；自动准入依赖本地披露记录确定年度并遵守冷却期。显式采集可以用于旧名单的开发验证，但不会替换旧名单默认读取来源；要让当前页面和研究绑定读在线批次，需上述 `--bank-online` 名单版本。功能默认关闭配置保持原状。

## 验收结果

主代理整链测试在隔离 SQLite 中导入实际10份银行年报及宁波补充原件，经过实际任务入口、快照、冻结批次、API、绑定与严格引用；测试供应商 transport 使用标明为受控数据的响应，真实网络验证单独记录，不混称为生产整链。

| 检查 | 实测结果 |
| --- | --- |
| 银行在线采集、白酒兼容、任务、读侧、证据、API、Agent与引用专项 | 211 passed，155.06s |
| 最终年度身份收紧：拒绝FY2025响应中矛盾的“2024年年报”标签 | 适配器26 passed，1.46s；其中25项与上行重叠，新增1个反例 |
| 主代理实际原件→在线任务→快照→API→修订→恢复→历史读取 | 1 passed，100.55s |
| 原件协调专项：48/2及坏字节、错owner、错主体、未来快照 | 1 passed，88.27s；另有3项helper单元测试 |
| 白酒整链、双行业整链、既有银行原件导入、个股同行页面集成 | 25 passed，268.20s |
| 本轮修改的9个源文件 mypy | 零错误；修改范围 Ruff 与 `git diff --check` 通过 |
| 官方备份工具与manifest校验 | 40个文件的尺寸和SHA-256全部匹配；SQLite包含11份PDF |
| 恢复后的独立读回 | 11份PDF哈希一致；11条在线快照、52个verified字段版本全部原件重验；3个已提交批次、30份原始响应字节哈希一致 |
| 完整 `tests/unit` 工作区快照 | 2542 passed、2 failed、2 skipped、6 network deselected，507.35s |
| 全仓 mypy 工作区快照 | 447个源文件中5条错误，均在下述并行修改范围 |

主代理整链确认首次在线结果48 verified、平安2项范围排除；重复刷新维持10条在线快照。仅修订宁波拨备覆盖率后增加1条快照，该字段保持candidate并有待复核原因，其余4项（含原件归母权益）可重验。任务在最终提交前受控中断，重启后不再发起HTTP请求；旧批次数值与原引用保持不变。

历史可读与当前发布校验分别验收：旧cutoff可还原当时的数值和引用，但已知修订仍会使旧引用在当前严格校验中进入待复核。这沿用已有方法契约，不把历史读取当作继续发布已核验结论的依据。

全仓门禁尚未全绿，不能据本轮专项通过宣称整个并行工作区可发布。两项全量单元失败分别为 `test_import_graph.py::test_no_new_package_edges_beyond_baseline`（并行 `evidence.disclosure_pages` 引入 `evidence -> supplemental_rules` 依赖）和 `test_material_release_audit.py::test_valid_package_passes_m1_and_keeps_missing_values_explicit`（并行迁移头053/054不一致）。全仓mypy错误位于 `evidence/disclosure_pages.py`、`supplemental_rules/disclosure_updates.py`、`agent/research_publication.py`。这些文件属于正在进行的P4工作，本轮未修改、回退或放宽其门禁。跳过项为两个依赖本地忽略原件的海外样本；本轮银行真实原件用例均实际执行。

恢复后另核验修订前、修订后两套绑定：不受影响的各3项严格引用通过；历史拨备覆盖率原值3.7316仍可读取并保留原件，而新旧两套该指标的当前严格引用均因已知修订被拒。没有为恢复测试放宽发布校验。

**本轮银行在线年度采集与显式更新功能验收通过；全仓发布门禁仍需并行P4工作消除上列失败后再确认。** 原件证据自动补全到新年度、生产持续调度和来源用途条件不在本次通过范围内。

主代理复现日志、隔离数据库、备份与恢复脚本保存在 `/tmp/zcharlie-bank-online-root/`。
