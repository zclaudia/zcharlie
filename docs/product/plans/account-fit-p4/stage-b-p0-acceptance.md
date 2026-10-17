# AF-P4-08 / 08-P0 离线工程最终验收

2026-10-17，Asia/Shanghai。主代理负责整合、共享接缝审查及最终验收，三名GPT‑6 Luna分别承担campaign、runner、source。用户仅授权08-P0离线工程，临时SQLite与合成/loopback HTTP；未执行S0、真实来源采集、实际profile装载、生产操作、开关启用、提交或推送。

**结论：08-P0专属离线工程验收通过；共享工作区全量存在并行银行域失败，不签全库全绿。** 实际契约见[冻结](stage-b-p0-freeze.md)，审查裁决见[交叉复审](stage-b-p0-review.md)，实施过程见[执行记录](stage-b-p0-execution.md)，源码归属及验证原文见[快照](stage-b-p0-source-snapshot.json)和[验证清单](stage-b-p0-validation/manifest.json)。07历史验收没有重写；工程准备不能替代S0或真实来源签核。

## 交付与验收矩阵

| 项目 | 最终行为与验证依据 |
| --- | --- |
| 严格V2及旧wire | 独立body/command/result/snapshot/hash域；V1原字节不变，旧reader拒绝V2。V2不能换wire/campaign/env逃累计，公开V1 API不创建V2 |
| campaign精确许可 | 单主体、绝对时段、真实file SQLite规范路径/身份；action唯一poll，第三slot初始reserved/disabled；独立批准修订才能解锁，scope/deadline/caps与未决不能重置 |
| 全程累计账 | campaign总32HTTP/192MiB/512MiB，S0子8/40/128，sample子24/152；action/poll帽、owner-source/day、grant/day、storage/cooldown同事务，跨日/换grant/revision不清总账 |
| 发送与故障 | reserve→sent commit→一次HTTP→result/unknown；重复sent拒绝。unknown按响应/存储预留满额扣；未sent取消释放预留但旧permit不能再发。native追加故障注入验证双账无局部提交 |
| 批准profile与raw probe | 两类registry默认空，独立protocol/contract/build/approval hash、用途及绝对期；同hash重装不复活撤销。raw仅精确operation隔离捕获，无正常SourceCoverage/Original/Evidence/Event/V3/fanout |
| 当前许可检查 | 每hop发送前/收包后核当前批准及campaign；关闭/撤权后的在途仅安全hash/用量，正文withheld；读取/prepare/publish/renew同步受限，不靠timer发现失效。API/metrics使用source.config_hash只读，不回写历史payload |
| 精确运行 | 普通非测试handler+native worker/lease/heartbeat，内部一次性capability绑定exact run/poll/action/env；generic worker、scheduler、maintenance不授manual任务HTTP；P1完成不自动运行P2 |
| 机械恢复 | raw的file-backed ledger精确恢复permit；sample独立recovery票仅结算旧hop，恢复后manual_recovery_required且不排HTTP。暂停/撤权不阻止安全计量，重启后沿原账，旧permit不可重发 |
| 重新采集 | 另一个明确resume命令，绑定原grant revision/hash、profile、poll/action/env；paused→active CAS，仅排一步，继承原窗口/截止/预算/逻辑attempt，仍核Retry-After和当前profile/授权/余额；generic V2 API resume拒绝 |
| 备份与兼容 | 独立0600/fsync sent锚阻止同inode旧库回滚账；sent事务回滚留下超前锚时保守blocked、离线对账，不删锚解锁。旧V1、P2/P3/A及coverage版本不改，050—054原字节，源码054单head，无新表/迁移 |
| 边界继承 | 不变更评分/Quality/DSL/账户硬约束、知识窗可信回执、冻结清单、评审绑定和原子闭环；暂停/到期/关闭/采集失败不解除旧问题；没有研究、交易、通知或生产权限增量 |

各层的approval_ref是受信服务端操作者提交的独立批准引用，代码校验精确绑定/CAS/预算/重放并保存不可变收据，不独立核验引用背后的人工签字或许可文本。没有公开API/env/model或daemon造票入口；未来实际许可须由具名责任人与独立评审提供。

## 验证结果

最终命令、退出码、原始日志/hash与源码时点由验证清单记录；unit/integration/roots/P4专项存在重叠，绝不相加为独立通过数量。pytest临时guard拒绝非loopback socket连接，6个network标记仍默认排除。五个浏览器脚本使用临时DB与loopback fixture，1440/390通过；仅截图目的地改到新P0目录。它们验证旧P2/P3/A/07兼容，不是08真实来源证据。

| 验证 | 结果与边界 |
| --- | --- |
| 最终P4专项及架构守卫 | **177 passed / 56.49s**，全部最终恢复补丁之后；覆盖P4旧V1与新V2、真实临时SQLite、受控HTTP、worker、配额/并发/故障注入、当前批准、读面零DML与恢复 |
| unit全量 | **2670 passed / 1 failed / 2 skipped / 6 network deselected**，331.76s；唯一失败为下述并行银行PDF用例 |
| integration全量 | **972 passed**，357 warnings，908.90s；含历史迁移、P2/P3/A/B与共享代码回归 |
| roots全量 | **717 passed**，222.68s |
| 浏览器 | 五脚本1440/390全部通过，25张新归档截图；只验证临时/fixture兼容，移动失败来源与桌面完成态人工抽查 |
| Ruff | P0自身16源码+6测试通过；全库1处并行银行矩阵测试import排序错误，退出1，未替其修改 |
| mypy | **466 source files，无错误**，最终补丁后 |
| 其他 | JS两文件syntax、git diff --check通过；源码迁移054单head，历史迁移/07记录/旧截图保全通过；七项bootstrap开关false |

全量分区在最后窄V2 resume/raw恢复补丁前启动，不能声称每份全量日志都对应最终同一源码快照。最后补丁没有扩展普通worker/旧V1行为，最终177专项重验全部P4/V1兼容、真实429→停止→明确恢复→原checkpoint完成、raw孤儿及import graph，Ruff/mypy重新执行；全量972/2670等作为此前共享路径回归证据分别保留，不和177累加。

首轮unit因新增包依赖触发架构守卫，移动probe编排到supplemental_rules、exact worker入口到research后重跑，不增加P0允许边；并行materials→settings边由并行工作保留。首轮integration为布局修订主动中止；原日志均保留，不计通过。

共享工作区unit另有银行域失败：`test_v2_rollover_rechecks_pdf_bytes_and_persists_complete_snapshot`。并行`bank_annual_v2._load_original`新增调用`bank_annual_identity.annual_front_matter`，pypdf解析既有测试截断合成PDF时报EOF。测试与review文件和起始hash相同，bank v2已变、identity新加；失败堆栈及银行导入无P0接缝，独立复现仍失败。原首轮unit没有此银行失败。保留银行域改动，不为P0改写其校验或测试。收尾全库Ruff还在并行新增的`tests/integration/test_bank_two_year_matrix.py`发现I001导入排序错误；P0范围Ruff通过，两次全库观察均保留。不能宣布共享工作区全量全绿。

## 源码归属与保全

起始HEAD b75d5f28431222f3a56ab5c18d86ef2f9770dbef，起始1757项tracked/nonignored摘要。并行提交推进至b5da5a8eb11757801fa4ed0f5c64f14bee3823af；银行、materials、mobile及原P1探针改动保留，本轮未执行git提交/推送。不能以当前HEAD diff替代本轮归属；快照按起始hash列出P0自身16个源码文件、6个测试文件与并行差异。050—054、旧V1 contracts、07冻结/验收/复审/源码快照及旧截图分别校验。

bootstrap七个相关开关只读核对均false，仅用测试API key满足Settings必填字段，未读部署runtime overlay；源码迁移图054不代表生产DB head。测试中的设置写入都在临时DB，没有对生产发起操作。

## 独立门禁与后续

本次仅交付08-P0工程。S0-T/P许可/字段/分类/时间/稳定tie/末页合同未核；首选000858.SZ/FY2025/W0仍是本地seed，不是已核实来源事实。真实profile安装、normal/repeat、更正、同号修订、反例、空窗与真实恢复证据均未执行/未验，AF-P4-08整体未完成。

后续须先明确具名执行者、独立来源评审、暂停负责人、确切URL/主体/窗口、用途及保存范围、绝对时段和有限额度，再另行批准S0。S0签核不自动授样本许可；sample仍需精确P1/P2批准，第三poll保留禁用。X公司暴露、Q研究质量/付费、T交易规则、N外发及P生产运行各自守门。本次不申请或执行这些步骤。
