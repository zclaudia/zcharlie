# P4 阶段 B 计划交叉审查

2026-10-17，Asia/Shanghai。范围仅AF-P4-07—08计划。主代理核对A最终代码/验收，三名GPT‑6 Luna子代理只读审查及复核；没有实现、迁移、live采集、生产操作、开关写入、commit或push。具体方案见[stage-b-plan.md](stage-b-plan.md)。

## 基线核对

HEAD `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`；A源码快照1229文件树摘要 `b8949477fee7b1f208fa17e34a96a579bdc9f360249d8e27e9444b7332d4a23a` 与60个变更文件摘要全部匹配；`.venv/bin/alembic heads`输出053单head（只读迁移脚本图，不连接部署数据库）。A是当前未提交工作区；保留mobile Cargo.lock、其他并行及原有探针。

## 首轮发现与主代理裁决

| 审查域/发现 | 最终计划裁决 |
| --- | --- |
| 来源：当前discover固定第一页、无类型allowlist，SZSE身份不等于交易所独立来源 | 首源准确命名CNInfo上的指定SZSE发行人；先限年度报告全文/明确关联更正补充；category/全文类型无真实映射就unknown，不猜类别、不扩政策源 |
| 来源：item身份含URL/时间；fetch跨公告同hash会返回旧Original | 以source/issuer/native ID稳定业务UUID，单文档CAS映射；blob共享、每native独立provenance。旧hash再现隔离，不按抓取顺序反转事实 |
| 来源：更正标题lead不是关系，官方PDF不是verified | 真实关系原件精确refs，首版candidate；S-correction与同号revision分别待验，不制造样本或直接提权 |
| 恢复：task DisclosurePermission不能给cron，研究Mandate不授采集 | 新typed后台grant，owner-source/day跨scope与授权revision聚合；明确建议值及硬cap，无授权0请求/新增费用0 |
| 恢复：execute持久error，固定operation重放不会重试 | logical request与实际attempt分离；成功页重用，失败新attempt受原poll/day/重试额度；reserved/sent/结果可恢复，不重复清额度 |
| 恢复：底层redirect可能隐藏多次HTTP，关闭与网络不可原子 | 每hop独立许可/配额；sent commit为线性化点，关闭前零发送，在途只审计且不落可见业务。unknown按上限计用，不虚称外部exactly-once |
| 覆盖：A V2只查evidence_revision、非空精确refs、资料可见窗口 | B新增typed V3 poll proof，空结果有真实页证明，publication窗口与acquisition分开，V1/V2原hash不变；不冒充A的complete |
| 覆盖：新poll不能让旧task追新，健康续查也不应重复收费 | cutoff内选历史proof，now检查健康；semantic_generation/history_epoch与有界连续水位证明分离，健康续查不改事实代次，新事实仍立即限制旧观点 |
| 事务：早期文字假定原件在文件库/全页原子 | 实际DisclosureBlob在SQLite；索引页与逐文档原子接收分开，最后确认完整；没有新增文件库/原件表/预算平台 |
| 门禁：fixture、原件下载、工程通过不能互相代替 | B-E、S0/S样本子门禁、X/Q/T/N/P分开；真实更正/同号修订无样本明确待验，live与生产分别另获授权 |

## 新稿复核与文档验收

三名子代理均完成新稿复核及修订后的再次只读确认，结论均为无剩余计划级阻塞。主代理接受以下修订后通过计划：

- 来源域：排序/tie-break/末页语义无法实证则两遍相等也只partial；首版PDF-only；内部candidate写入采用封闭持久化口；真实空窗须人工核真实目录和合法schema。
- 授权/恢复域：首请求及每个redirect hop各自reserve→sent commit→一次调用→result；阶段收据独立身份、同attempt关联；跨日预留迁移、grant UUID稳定、unknown不退款；recent/backfill交替和1hop/20local的quantum形成可测公平界限。
- 覆盖/门禁域：来源state单调保存epoch/generation，R2发现pending即收紧、R3语义变化同事务CAS；final仅有界直接页/叶refs，前驱连续性在写时冻结anchor，读不遍全史；V1/V2保真、未知V3 fail closed。
- 费用澄清：B source grant不授研究费用，A已有独立Mandate可沿原逐次检查产生合法新task和费用；不复活旧终态。08.1使用无付费/外发权限的隔离环境，采集许可不充当研究授权。

主代理最终补明：poll根身份与有界PipelineRun step分离，不让parent占租约等child，每hop让出且generic retry不能透明重发HTTP；V3后继downgrade guard为必要迁移，即使现有索引足够也不能省略或修改053来补。新表为空但旧表V3/B payload仍须拒降；具体索引只在实施07.0以EXPLAIN裁决。门禁子代理对这两项另行窄复核，确认无新阻塞；guard须用明确schema/tag避免误拦普通历史收据。另区分poll启动间隔与HTTP逐hop最小间隔，共享blob读取始终先核owner receipt。

**计划审查结论：AF-P4-07—08具体计划通过。B工程未实现；真实来源、暴露、研究质量、交易规则、外发和生产门禁均未因此通过。** S0真实条件、类型映射、真实样本及独立人工评审仍按计划是后续前置门禁，不用猜测或fixture替代。

文档验收：对本轮开始时1687个已跟踪/非忽略未跟踪文件逐项SHA256保全核对，只有主设计、P4总计划、P4 README/gates四份文档变化，新增本B计划/复核两份文件，无删除；其他内容全部保全。A的1229文件树摘要及60项变更hash完全匹配原snapshot，A的验收/冻结/probes、mobile Cargo.lock和并行内容未变。修改/新增文档的101条本地Markdown引用全部存在，`git diff --check`通过，新增文档无行尾空白；迁移图仍053单head、HEAD未变。只跑这些文档/保全检查，不重复业务测试；没有新增迁移文件、live或生产操作、开关写入、提交/推送。
