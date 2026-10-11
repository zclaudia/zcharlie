# AG27 受控保存材料搜索与跨公司取证

日期：2026-10-11。工程交付不等于真实研究质量通过；默认 Agent / 原件获取配置仍关闭，AG20 真实质量门仍 pending。本卡只搜索已有属主材料，不下载、不爬网、不建向量库、不调用真实模型，不新增表，migration head 保持 037。

## 可用入口与授权

认证 `POST /investor/disclosures/search` 读取已保存证据库。Agent 认证消息选中 listing 后，由服务器将 `search_saved_sources` / `compare_saved_sources` 加入已有不可变查询 grant；真实 queue → ResearchPolicy → FunctionModel 路径可调用，调用仍经过当前 lease、取消、feature、scope、as_of 和预算边界。搜索权限不授予 discover/fetch/parse 权限；原件取得和解析仍须 AG25/26 的显式 disclosure_access。关闭功能时保留认证历史搜索读回。

工作台“搜索保存材料”只搜索当前消息选中的证券；显示证券名称/完整代码、公告标题、核验状态、披露/取得/解析可用时间、定位、精确证据引用、冲突与更正限制，以及相邻原文和校验 hash。全部文本使用安全 DOM；来源文字和摘要只作线索。

## 范围、时间和资源边界

直接使用 immutable `SavedEvidence` 中的 AG26 `parsed_fragments` manifest 和已有研究证据，搜索不重跑 PDF/HTML 解析，不扫描原始 blob。manifest 内容 hash、原件 hash、片段 hash 逐项校验；数值事实相邻文本只来自同 original、listing、parser version、fragment locator/hash 且在 cutoff 可见的唯一保存 manifest，多版本歧义不挑选赢家。

owner、请求证券集合、field、period_end、文字匹配，以及 published/acquired/available/created 全部先在 SQL 过滤，再读取候选记录。时间使用 UTC 六位小数精确比较，避免 SQLite 日期浮点毫秒舍入使新解析提前可见。搜索是“保存证据库在 cutoff 已存在”的可见性，较 AG05 对已有历史 provider facts 的即时包装更严格；没有修改既有 get_evidence 的语义。显式 historical_reconstruction 仅标识历史只读意图，搜索仍不开放后来取得或解析的材料。

关键词为字面子串：中文及其他 Unicode 按字面匹配，仅 ASCII 字母忽略大小写，不作 Unicode casefold、分词、FTS 表达式、模糊或语义推断。引号、反斜线、百分号、下划线和搜索操作符均为普通文字，SQL 使用绑定参数。query 1–120 字符、最多 10 个不同证券、limit 1–20、offset 0–200；只允许证券正整数。

每次最多读取 100 条匹配的保存记录，按请求证券公平分配；最多保留 220 个片段命中，也按证券公平分配。同相关性结果按证券交错，不能让一家公司占满候选窗口或第一页。记录/片段上限、耗尽证券和 coverage 明确返回，未搜索材料不是“没有证据”。结果页只展示窗口中命中；达到窗口时缩小证券/field/period/关键词范围，不声称全库覆盖。每条片段和每条邻文展示最多 2,000 字，并保留实际偏移、完整片段 hash 与截短标识；相邻内容最多前后各一段。同原文审计最多显示 10 条，保留总条数和截短标识，全部核验状态、限制和更正引用合并可见。

## 去重与比较

同 listing、原件 hash、locator 合并展示，同时保留各 source/evidence/version 的审计。不同证券仍独立展示；跨来源共用原件 hash 的 provenance group，转载不增加独立支持。`distinct_original_count` 是原件数量，不代表已核验或可以支持判断；已有用户报告保持 user_report。排序只按字面出现次数，不表示真伪、认证或投资排名。候选 manifest 和已核验数值的状态分别保留，不因排序抹去已知差异、更正或冲突。

`compare_saved_sources` 只读取 exact saved evidence refs，检查主体、有效状态、verified 数值、完整报告期（含 basis）、单位、币种、指标和显式 reporting_scope。任一缺失、已更正、待核差异或不一致时返回 not_comparable_reasons，并列事实、ranking=null，不硬排。快照币种须有显式一致元数据；搜索结果不会绕过原有发布、NumericResolver 或 AG15 复核门。

## 验证与限制

专项覆盖生产 PDF/HTML parser 保存 manifest、晚于消息的 parse cutoff、微秒时间、属主/证券集过滤先于窗口、Unicode/标点字面查询、同原文去重、记录及片段公平限额、同期间比较与期间不一致、当前更正传播，以及认证 queue → native FunctionModel 与范围外拒绝。浏览器用真实 HTTP / 文件 SQLite 在 1440 / 390 检查分页、来源读回、中文身份/时间、安全 DOM 与无横向溢出。

本卡不支持外部网络搜索、全市场索引、通用分词、OCR、复杂年报表格或自动事实认证。AG26 的一处明确 header / 年度范围 / 合并口径 / 单位规则、标题仅更正线索和格式限制保持原样。AG20 真实调用仍为 6 HTTP / 75,669 tokens / 0 有效研究，FCFE 0/4，trading rules unavailable；未追加任何收费调用。

## AG28 交接

AG28 可复用当前工具及覆盖限制做后台取证，但不能扩大来源、既有 grant 或旧任务 cutoff，也不能把搜索命中数量当支持强度或产品质量。长期运营、外部通知和独立后台预算仍属下一卡；本卡完成后停止，不自动启动 AG28。


## 最终验证结果

集中 focused 44 passed / 19.43s；Ruff、mypy（267 files）、JavaScript syntax、diff 与 1440/390 真实 HTTP/file SQLite 浏览器检查通过。单次冻结代码的固定离线全量：2127 passed、3 skipped、6 deselected，282.69 秒，exit 0。三项 skip 仅外部 ZCLAUDIA_GATEWAY_DIR 未配置。早期 focused 失败和修复原因保留在计划卡片，不计作通过；全量没有提前重启。真实数据库保持 021 / 360513536 字节 / mtime 1791489715，迁移 head 037。测试与浏览器子进程均已退出，AG27 完成后停止。
