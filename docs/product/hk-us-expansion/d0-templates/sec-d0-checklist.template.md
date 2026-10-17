# SEC D0 前置清单（填写模板）

用法：复制本模板到 `<evidence-root>/reviews/sec-d0-checklist.md`，逐项填写 `[待填]`
处并附证据引用（相对 evidence-root 路径 + SHA-256）。全部六项完成后按
[38 号手册](../plans/hk-us-expansion/38-hk2-06-d0-readiness-runbook.md) §4 执行 D0 检查。
填写人：[待填]　填写日期：[待填]　审批人：[待填]

## 1 责任主体

- 访问 SEC 的项目/组织身份：[待填]
- 审批人：[待填]　批准方式与日期：[待填]
- 留档文件：[待填（相对路径）]

## 2 项目用途确认（对照 34 号使用范围矩阵）

- 本地保存/缓存（原件归档、期限）：[待确认结论]
- 私有应用内展示：[待确认结论]
- 是否商业用途：[待确认结论]
- 确认依据与结论留档：[待填（相对路径）]

## 3 真实 User-Agent 与可监控邮箱

- 拟用 UA 字符串（格式 `产品或组织名/产品名 (联系邮箱)`）：[待填]
- 邮箱可监控证据（如接收测试记录）：[待填（相对路径）]
- 注意：probe 仅校验格式；example/占位邮箱与默认 UA（无联系邮箱）不得进入真实流程。

## 4 聚合限流证明

证明方式（二选一）：[ ] 单实例独占　[ ] 跨进程共享限流器

### 4a 全部 SEC 消费者清单（代码侧事实已预填，部署侧待补）

| # | 消费者 | 代码侧事实（2026-10-17 盘点，基线 ad034fb） | 主机 | 速率 | 门禁现状 |
| --- | --- | --- | --- | --- | --- |
| 1 | API 服务 + ResearchWorker（`MaterialCollectionHandler` → `HttpxMaterialFetcher`） | 在线刷新路径；三重门禁：`ONLINE_REFRESH_ENABLED` 模块常量（False，普通设置不可改）+ 来源运维开关（默认 false）+ source policy（当前 unknown/not_accepted → 拒绝） | [待填] | [待填] | 当前 0 请求 |
| 2 | `tools/hk206_source_probe.py --allow-network` | 一次性隔离 probe；需人工显式 `--allow-network` + policy 全源公司 approved | [待填] | 手动单次 | 当前会拒绝 |
| 3 | `tools/collect_hk_us_annual_evidence.py` | HK2-04 一次性取证 CLI，直接 httpx 拉取 CompanyFacts/Submissions/分片；**不受 source policy 门禁** | [待填] | 手动单次 | 已完成取证；**观测窗口期间不得运行** |
| 4 | `tools/hk206_drills.py` | 演练 CLI；socket 审计钩子拦一切真实网络 | 任意 | 0 | 非真实消费者 |

- 其它主机/进程（含个人电脑、其它项目、浏览器插件等凡会请求 data.sec.gov 的）：[待填，无则写"无"]
- 所选证明方式的证据：[待填（独占约束说明/锁机制/运行日志核对，或共享限流器配置+生效证据）]

## 5 archive 空间与留存策略

- evidence-root 所在卷与余量：[待填]
- 留存期限与到期处置：[待填]（须被第 2 项确认覆盖）

## 6 公司范围批准（六发行人）

| company_key | CIK | 批准人 | 日期 | 依据 |
| --- | --- | --- | --- | --- |
| hsbc-holdings | 1089113 | [待填] | [待填] | [待填] |
| apple | 320193 | [待填] | [待填] | [待填] |
| costco | 909832 | [待填] | [待填] | [待填] |
| jpmorgan-chase | 19617 | [待填] | [待填] | [待填] |
| exxon-mobil | 34088 | [待填] | [待填] | [待填] |
| tsmc | 1046179 | [待填] | [待填] | [待填] |

批准落点：受控 source policy 审批流程（由审批人执行），本清单仅是准备材料。
