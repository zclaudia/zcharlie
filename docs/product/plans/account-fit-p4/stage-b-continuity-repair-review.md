# 同campaign连续性离线修复计划——交叉复审

2026-10-17，Asia/Shanghai。范围仅[08-RP-00—08修复计划](stage-b-continuity-repair-plan.md)，主代理总体设计/整合，三名GPT‑6 Luna只读审查。**结论：计划审查通过，未实施。** 这不是业务代码、S0、profile安装、真实采样或生产许可。未初始化任何库、安装registry或操作开关，没有真实HTTP、提交/推送。

当前16个P0源码与6个P0测试均匹配[历史最终快照](stage-b-p0-source-snapshot.json)。初始HEAD为`acb0856a6c32d319a5411404054e827a6276f05f`（并行银行域已提交），起始1871文件hash用于区分本轮写入。原P0冻结/验收/复审/日志、S0上轮复审/核验保留；移动端与历史探针原字节保全、并行银行域差异归属见[本轮核验](stage-b-continuity-repair-validation.json)。没有重跑业务测试，不把历史P0的银行域失败写成当前已修/全绿。

## 1. 主代理共享接缝裁决

| 风险/子代理意见 | 已纳入的最终计划 |
| --- | --- |
| 只允许已有reserved slot后绑定，仍无法接已有无slot S0-only | 显式严格CampaignV3提升特例：只纯S0、paused/quiescent/无sample grant/poll/history，0槽一次补标准3全reserved；完整3槽原样；不泛化append。新V3默认3预留/帽0 |
| 直接给V2加default或只追加extension，旧reader可能忽略 | 同UUID/feed/scope/source row，contract_version3；promote桥引用最后V2 authority，旧hash/载荷不改；严格reader/save按版本分派。旧缓存保存CAS/version拒，新版V2 raw拒新发送；legacyV2 sample不强升 |
| 旧进程或旧库可绕过新票 | 实施/提升前全停旧executor、清长事务/未决，不能证明则blocked；V3旧reader拒绝。明确未修复旧binary+旧备份无法认识新marker，禁止混跑或联网回退，不承诺JSON可约束旧程序 |
| 绑定就批准动作/授额度、grant需active形成循环 | profile-ready仍reserved/帽0；独立样本许可与一次D才approved；sample-lane激活先于V2 grant但不授网；grant/poll/runner仍各自核权。raw seal不可逆，P3新V3本轮不放行 |
| 一次D可被误读为补满剩额，忽略P1/P2和storage | D同时受phase硬帽、原全局剩额、已批P1/P2 action/poll帽之和限制；新增sample storage账。例S0 8/40/128后余24/152/384，而各P1/P2 8/64/128只授D≤16/128/256；一次分配无top-up，不给第三slot余权 |
| raw只改paused→active使旧executor复活 | persistent epoch/lane，独立resume命令和每op dispatch票；票在reserve与两账同事务消费，mark_sent再核当前epoch/票/持久撤销；pause/resume重放不能重新激活 |
| 改approval hash续旧progress，PDF index失真 | 首版仅同原ProbeApprovalV1完整hash/build/paths/有效时段；固定原包连续成功前驱；原PDF仍查同包query。新hash/跨包继承不支持；已reserve/canceled/failed/unknown/缺raw的当前步不重试 |
| `recover_hop`与resume混为一谈 | 旧机械结算不需要仍有采集许可，不排队/HTTP；raw resume只续从未reserve的下一op，不能回补预算或复活终态。新原包恢复和sample旧root恢复分开 |
| registry内存重启丢撤销，只保护sent不足 | campaign级持久批准admit/revoke与显式复核装载，默认空registry；同三窄表，无全局审批服务。原sent锚保留，新增独立control锚 |
| control锚只记pause/revoke，遗漏quota/票/授权回滚 | 每个V3 authority revision都推进control锚，head精确等projection.authority_ref；覆盖全部权限、grant/poll、dispatch、reserve/result/unknown/cancel/recover与phase帽/撤销，不许repo旁路 |
| 双锚/DB无法原子，提升/初始化失败可自动重建 | 外marker fsync在DB commit前；先sent后control的固定顺序，失败保守ahead/blocked，不删锚补库；新create/promote只合法非空control根，所有故障/旧库回滚纳入验收 |
| 增新表/迁移或放松import baseline | 新控制/批准/账仍落已有三窄表，054对任一行非空即拒降级，足够保护；无新表/空迁移。evidence保持纯接口，控制协调放supplemental_rules，不引入反向包依赖 |

## 2. 子代理审查与收敛

- `repair_binding`：核现action/grant/profile/runner接缝，指出无slot与reserved P1/P2两类阻断及active/grant循环。主代理选择V3纯S0提升兼容0/3槽，分离绑定/许可/激活；末轮指出D需再限P1/P2总帽，已修后终审通过。
- `repair_raw`：核原approval hash、operation key/quarantine/PDF index，建议首版同包suffix而非跨包继承；指出票必须在reserve消费、sent前epoch复核、旧V2 bypass、前驱/取消/unknown终态及控制状态回滚。计划与离线矩阵补齐，末轮终审通过。
- `repair_shared`：核strict版本/authority桥、054降级guard、两账/CAS、lane并发和读面；要求control锚覆盖权限/额度闭包及初始化失败、旧进程停机/version拒绝。主代理将control head统一为每个V3 authority ref，补新create双锚失败与每类save闭包断言，终审通过。

主代理没有采纳新增全局raw生产flag、通用授权/工作流或跨approval恢复图；V3默认无lane/票及每检查点epoch是此次窄发送栅栏，普通poll开关依旧不能当raw kill。没有把子代理review替代真实来源/许可的具名人工签核。

## 3. 任务顺序与工程退出裁决

RP-00严格合同→01显式提升→02持久权威/双锚→03/04 profile与样本链、05 raw恢复分别实施→06共享读/发布限制→07合成SQLite/HTTP/worker/故障/并发与兼容验证→08独立工程验收。修复版V2 raw拒发送、旧reader/CAS拒V3、控制锚完整闭包、原账/总帽/期限/sent锚不改、一次D、无隐式grant/queue/HTTP，是必须通过的退出条件。

后续实现须用临时file SQLite和controlled/loopback HTTP；旧V1/V2 wire golden、旧V2样本兼容、0/3槽纯S0提升、真实profile后生成hash形态、原包query→pause→resume→PDF、不支持终态恢复反例、双锚故障/同inode旧库回滚及每次V3 revision对应锚推进均列具体验收。shared auth/coverage/runner改动后做必要全量及五browser，最后补丁专项与全量时点分别记，不冒称同一快照全绿。

本轮只做静态代码/文件/提交、head与计划数字/链接/保全核验，未运行上述未来测试，没有创建假执行/验收manifest。计划通过后仍待用户另行决定工程实施；工程通过后再更新S0清单并逐阶段核具名人/许可/绝对时段/保存范围/真实profile/样本授权。X公司暴露、Q研究/付费、T交易规则、N外发、P生产继续独立，本轮不请求这些许可。

收尾只读观察：本轮之外新增/改变10项银行发布演练文件，含既有`cli/commands/bank_annual.py`与`peer_annual.py`及新测试/工具/文档；这些未由本轮写入，未恢复或混入任务。核验记录按观察时点列出差异，不宣称整个工作区源码静止；P0 22文件及141项关键历史/移动端证据仍原字节匹配。
