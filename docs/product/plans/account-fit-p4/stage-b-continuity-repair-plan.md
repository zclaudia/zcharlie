# AF-P4-08：同campaign后绑定与raw受控恢复——离线修复计划

2026-10-17，Asia/Shanghai。**计划待实施；本轮仅文档，无工程实施或运行授权。** 用户要求先细化两项接缝；主代理设计/整合，三名GPT‑6 Luna交叉审查。依据[08-P0最终验收](stage-b-p0-acceptance.md)、[冻结](stage-b-p0-freeze.md)、[源码快照](stage-b-p0-source-snapshot.json)和[S0实际清单](stage-b-s0-execution-plan.md)。本轮起始HEAD `acb0856a6c32d319a5411404054e827a6276f05f`，P0的16源码/6测试全部匹配；并行银行域提交/移动端改动保留。历史P0测试与当时银行域失败不重新解释为当前全库通过。

任务代号`08-RP-00—08`仅用于本次窄修复，不替代08-P1真实profile装载、S0-T/P或真实样本编号。源码迁移仍054；本轮不初始化数据库、不装载批准、不发真实HTTP、不操作生产/开关、不提交/推送。计划复审及只读保全见[复审](stage-b-continuity-repair-review.md)和[核验](stage-b-continuity-repair-validation.json)。

## 1. 缺口与明确范围

| 当前代码 | 本修复的增量 | 不纳入 |
| --- | --- | --- |
| V2 actions固定；P1/P2需要未知profile hash却不能reserved→approved；无slot时不能追加 | 同UUID显式提升严格V3；只补固定3个sample占位；独立profile后绑定、样本额度授权、lane激活，然后沿既有V2 grant/精确poll | 任意追加/改写action、替换已有批准/已消费profile、自动注册真实profile或启动样本 |
| raw仅recover_hop结账；暂停后无raw resume；改approval hash断开前驱与PDF index | 同原ProbeApprovalV1 hash、同build/路径的未消费suffix，持久epoch/单次dispatch票+明确resume命令；已有同包前驱不重取 | 失败重试平台、跨approval引用图、重新批准已撤销/过期包、复活sent/canceled/unknown步骤 |
| registry仅内存；sent锚不保护最后一次sent之后的pause/revoke/授权回滚 | 同三窄表的campaign级批准/撤销与控制收据；新增独立control锚保护V3控制及额度状态；原sent锚保留 | 通用审批平台、全局registry服务、新新闻源、新表、生产启用 |
| raw不读普通poll开关，active状态不能隔离旧执行器 | V3默认paused/no lane/无dispatch；所有V3新发送检查lane+epoch+权威票；修复版V2 raw不授新发送 | 新全局生产开关；仍不得声称普通polling=false能kill raw |

首版恢复能力刻意有限：支持在reserve之前或成功步骤之间暂停后续行；下一步骤必须从未reserve。已reserved后cancel、已知HTTP失败、unknown、raw缺失的当前步骤均不重试，也不能跳过去继续后序。后续若要新approval继承或未sent重试，需另审精确引用机制，本计划不以“恢复”扩大许可。

## 2. 冻结不变量

1. 同一owner/campaign UUID、feed/scope/source row、sample_env、DB规范路径/device/inode、原`valid_from/expires_at`不变。绝对期过后blocked，不延长、不另建campaign/复制库逃账。
2. 原raw action ID/顺序/scope/body/URL/帽、immutable journal、authority refs、原件/审计hash不改；原V1/V2 DTO、canonical hash域和历史payload逐字节保留。新控制与绑定不能追认旧raw/fixture为verified、coverage或V3 final。
3. 原campaign总HTTP/响应/storage帽只减不增，≤32/192MiB/512MiB；S0子帽及历史用量不变，sample上界≤24/152MiB。所有consumed、reserved、poll/action用量、全owner日桶/storage/cooldown/grant账继续原值，unknown不退款。取消只释放可证明未sent的原预留，不清历史。
4. 原`.p4-08-<campaign>.sent-anchor.json`格式/路径/高水位保留，仅新sent推进，提升/绑定/恢复不初始化、删除或重置它。新增control锚不替代sent锚；任一缺失/超前/损坏/与库不符均停止并离线对账。
5. 后绑定、人工样本许可、lane激活、grant创建、poll准入、单次执行分别核权。绑定不是许可，激活不是grant，grant不是自动排队，准入不是发HTTP。无模型/API/env批准入口、无自动loop/maintenance/重试。
6. 旧事件/修订/激活规则继续限制旧结论；读取/prepare/publish/renew即时核当前限制，timer不推进知识窗。评分、Quality、DSL、账户硬约束、reduce/exit、P2/P3冻结清单/评审/发布闭环和正式通知/付费/交易权限不扩展。

## 3. 严格V3与兼容提升

新增独立`DisclosureCampaignV3`/严格内部command/receipt类型，沿同feed/scope/row，`source.contract_version=3`及payload.schema_version=3。新V3仅指campaign wire，不改既有SourceCoverageBindingV3、ProbeApprovalV1或grant V2 wire。不得给V2加可忽略defaults、重算V2旧hash，或只改外层version伪装提升。V3增加：`control_epoch`、`active_lane=none|raw|sample`、不可逆`raw_stage_sealed`、profile/phase许可ref、一次分配的sample帽及sample storage consumed/reserved、campaign级批准状态heads。每个新V3 authority快照仍完整覆盖账与scope；控制锚头取其精确authority ref，避免自引用hash。

| 输入/动作 | 决定 |
| --- | --- |
| 新V3创建 | paused、lane none、epoch起点、sample帽0，标准P1/P2/P3全reserved且profile空；raw仍需独立精确批准和单次票 |
| V2纯S0无sample槽 | 明确promote命令一次补固定P1/P2/P3全reserved；其余raw原序/值保持。此为受审查提升特例，不是任意append |
| V2纯S0已完整3槽 | 仅全reserved、无profile/既有sample批准、grant绑定、poll准入或sample用量者可原样提升；不重排/改ID |
| partial/重复slot、已有approved/consumed/sample grant或poll历史 | 拒promote，不覆盖成真实profile；保持legacy只读/结算及原样本路径。若确需提升这类数据另审，不能借清空字段通过 |
| V2 active或有未决、identity/锚/authority链不符 | 不提升。先停旧执行器、显式pause、精确机械结账；已sent无result按unknown满扣；证据不足则blocked |
| V2 raw在修复版的capture/reserve/mark_sent | 一律拒新发送，即使active；只读和精确旧账结算保留。旧V2 sample沿P0行为，不将本修复自动套入旧授权 |
| 旧二进制看到V3 row | 严格contract_version拒绝；旧缓存V2对象尝试save必须CAS/version失败，不能回写V2。真实升级前仍须停掉旧进程，不能靠新reader约束未更新的执行器 |

promote前置：仅file SQLite、纯S0（原已审S0 raw清单≤8个唯一动作，不接任意16动作清单）、paused、active_poll空；campaign/action/poll及owner/raw/storage预留均0、cooldown active_hop空、无raw_pending/未完成quarantine；旧执行器已隔离且无旧长写事务；两账/旧authority链/原sent锚精确一致。不能证明旧进程已停则不promote/resume。来源/批准过期不妨碍只读历史；但不能由提升赋新网络权力。严格版本/CAS保护的是现存V3 row；未修复的旧二进制无法认识control锚，不能声称它在同时退回旧库后仍会拒绝。禁止旧二进制+旧库联网、混跑或自动回退；修复版读取V2/旧库也须检查残留control锚。此单写者/版本隔离门禁不可用放宽DTO代替。

提升事务：`BEGIN IMMEDIATE`重读版本2/expected_revision/完整V2链→追加promote决策（原authority ref/hash/revision、raw manifest digest、补slot清单、新V3 digest）与V3 authority→同一次CAS切source row为3→初始化control锚指向非空V3 authority并fsync→提交。V3链通过唯一promote桥引用最后V2 authority；桥以前严格V2，桥以后严格V3；截断、未来版本、重复不一致提升拒绝。原V2收据不改写，sent锚不动。

## 4. 控制权威、双锚与事务

新增窄`disclosure_campaign_control.py`、`disclosure_campaign_control_anchor.py`（拟定），复用既有source-state/journal及CAS；raw批准/dispatch辅助可放窄`disclosure_probe_control.py`，不能反向把领域控制放evidence底层。没有新表/队列/worker平台。source.campaign V3的每次变动都生成V3 authority新修订并推进control锚，覆盖控制与可用额度回滚，而不是仅记录pause。

control锚建议`.p4-08-<campaign>.control-anchor.json`：strict version、campaign UUID、规范DB路径/device/inode、epoch、最高authority receipt的sequence/id/payload_hash/record_type；其ref必须与当前V3 projection.authority_ref完全一致，且权威链完整。它是本地故障/旧SQLite备份回滚防护，不能防DB与独立锚一起恶意回滚；备份/恢复须独立保全两锚。

保护闭包必须包括：create/promote、pause/resume/terminate/lane切换、raw seal、profile binding、sample范围许可/一次帽分配、probe与campaign级profile authority admit/revoke、V3 grant创建/bind/revoke、poll准入、dispatch issue/consume、每个reserve/sent/result/unknown/cancel/recovery。任何可能恢复发送权、复用旧票或回补余额的写入都须同事务触V3 authority/control锚，不许旁路repo直接写投影。纯读取/重放返回既有收据不产生新revision或锚写。顶层operation_id在owner内唯一，一条命令只有一条原生operation收据；子收据使用命名空间change_key，不与grant/poll/dispatch复用operation列。再次pause后同命令重放只返旧收据，不重新激活或签新票。

每次写入顺序统一：SQLite短写锁与当前两锚/authority核对→验证scope/CAS/idempotency→追加决策/账/新authority→一次source CAS→外部锚0600临时文件、fsync、原子rename、目录fsync→DB commit→才允许对应HTTP（仅sent路径）。native与campaign quota、原reservation/dispatch消费处于同事务。无HTTP跨长事务；读取零DML，不自动补头/修锚。

仅sent时两个锚都可能推进：固定先原sent锚、后control锚，再DB commit；任一写失败回滚DB，已前进的锚不回退，下一次发送blocked。其他控制操作只动control锚。两锚初始化仅新create/promote合法一次；已有锚缺失/损坏不能initialize空文件“修复”。若marker落盘后SQL回滚，ahead是预期保守停止，必须人工离线对账，不能自动重试、删锚或补造收据。promote回滚时修复版V2 loader也要探测遗留control锚并拒新发送；旧DB本身应仍paused。

持久批准是本campaign的窄执行权威：保存原ProbeApprovalV1/来源approval的精确原字节/hash、具名记录ref、evidence refs与admit/revoke收据；ref并不认证签字/许可真实性。新V3安装与撤销经受信内部服务，持久journal/control锚提交先于runtime registry改变；V3发包同时核持久状态及当前registry，不把内存当唯一authority。重启registry默认空，只有显式核完原记录、全部撤销史、build/path/env/期限及双锚后才可装未撤销原对象；不能只拿旧JSON复活。来源registry保持evidence层纯接口，不引入evidence→supplemental_rules新依赖；持久协调在领域层。无法证明撤销史则不发送。

## 5. 真实profile后绑定与样本受理

前提是后续真实S0/具名独立来源评审已通过需要的用途/字段/分类/时间/页序/终页门禁；本工程只用合成依据验证，绝不生成真实签核。操作者先关闭raw阶段并显式`seal_raw_stage`，要求paused/quiescent、留S0结论及许可/证据refs；未消费raw动作仍保留但永久不能执行，不退款、不删除。

| 顺序/拟内部command | 原子行为 | 必须没有的副作用 |
| --- | --- | --- |
| `bind_campaign_profile_v1` | paused/none且raw已sealed；只填P1/P2尚空的profile-ready reserved槽，锁定精确listing/W0/URL/方法/帽、protocol_hash、contract_hash、approval_hash、adapter build、purpose及S0-R/许可refs；独立BindingReceipt+V3 CAS/control锚 | 不改status为approved、不分配额度、不active、不grant、不queue、无HTTP/coverage |
| `authorize_sample_phase_v1` | 独立人工样本scope/额度批准；再次核profile当前有效，批准P1/P2精确槽并一次从0分配D；保存独立authority type/人审ref/时窗，P3仍reserved | 不混同来源approval，不自动创建grant或排poll；不得以后top-up重授 |
| `activate_sample_lane_v1` | paused/none+binding及phase许可有效、quiescent；epoch递增，status active/lane sample，raw永久closed | 不要求先已有grant（否则形成循环）；本身不授网络、不建grant/queue |
| 既有严格V2 grant命令 | 独立scope/≤72h/原campaign截止内；新增V3 branch核sample lane、binding、phase许可/当前profile及额度，再原子grant bind并触control锚 | 不改V2 wire/hash；grant换UUID/revision不清累计；API/env/model不能批准 |
| 既有poll准入→exact runner | P1/P2分别显式操作，当前profile+binding+phase许可+grant+lane/epoch完整，准入与执行分开；每hop重核并双账扣算 | P1完成不自动跑P2，不授P3/历史补扫/研究/通知/交易 |

BindingReceipt精确引用原V2/V3 authority、campaign/env/DB身份、slot ID、manifest digest、listing/window、profile三hash/build/purpose、外部来源许可/具名独立评审/evidence refs、绝对有效期及operation命令hash。任何已有profile/approved/admission/usage的P1/P2不可覆盖或追认；同operation同payload仅返回旧收据，异payload冲突。P3本修复保持reserved；V2原第三slot能力不改，新V3若将来要更正slot须另审精确后绑定/授权，不借generic amend放行。

额度定义为一次授予的累计phase cap，不是每次恢复的新余额。纯S0提升后sample消费为0：`D_http ≤ min(24, 原总HTTP帽−全campaign consumed−reserved, 已批P1/P2各min(action HTTP帽, poll 8)之和)`；`D_response ≤ min(152MiB, 原总响应帽−全campaign consumed−reserved, 已批P1/P2各min(action响应帽, poll 64MiB)之和)`；`D_storage ≤ min(原总storage帽−全campaign storage consumed−reserved, 已批P1/P2的action storage帽之和)`。D均明确批准且>0，phase新字段保存D及历史ceiling，只分配一次；总帽和S0帽不变。各样本hop另受action/poll、共享日桶/owner storage/grant/cooldown及磁盘余量限制，不把D当保证完成量。sample新增storage consumed/reserved每次和两账同事务更新，不只在绑定时检查。

例：S0已耗8HTTP/40MiB响应/128MiB逻辑留存且无预留，原32/192/512帽下全局余量至多24/152/384；若P1/P2各批8HTTP/64MiB响应/128MiB storage，则本次D至多16/128/256，不能按全局余量授给尚未批准的P3，也不能把storage再授512。未用余额不是追加许可。若原总帽更严、剩余不足或原截止来不及，blocked；不得提总帽/延期。绑定、样本许可、激活分事务可各自停留paused，不能用一个“继续”调用自动串到HTTP。

## 6. raw暂停、受控恢复与单次派发

首版只接受原ProbeApprovalV1**同hash、同完整operations、同build、同DATA_DIR/quarantine、有效且未撤销**。不改原operation_key/UUID5 permit身份或quarantine路径，不复制旧包为新hash；PDF仍以原包先前query index验证，不能把外部capture或人工声明当原索引。

新V3 raw初次激活和resume同样要求精确授权，不仅为resume加票。raw lane与sample lane互斥，raw已sealed、campaign unknown/terminated/过期一律拒。普通操作pause只持久fence，不自动撤销原批准；安全撤销是另一不可逆命令，被撤销者不能resume。不得为了恢复撤销前的许可而重新install同hash或延长日期。

| 操作 | 契约/线性化点 |
| --- | --- |
| `pause_campaign_v1` | 任意当前lane可pause：status paused、lane none、epoch+1，同事务控制收据/锚；所有旧activation/dispatch票与旧epoch未sent permit失效。在途仍可安全结算 |
| 先结旧账 | 沿recover_hop取消可证未sent或unknown满扣sent；不要求仍有capture权限，不HTTP。当前待续op曾reserved/canceled/result/unknown/failed则本次恢复不支持，不绕过它 |
| `resume_raw_campaign_v1` | expected paused revision/epoch、无active poll/任何预留/cooldown active_hop/raw_pending、旧执行器隔离、两锚齐；原包的已消费prefix必须全部200/无failure且raw/audit stored/hash正确。精确锁下一从未reserve的index/key/action及原前驱refs；独立resume ref/用途/人审时窗≤原批准与campaign期限 |
| resume提交 | epoch再+1、active/raw、锁原approval；消费一次resume命令并签第一张exact dispatch票，CAS/control锚同事务；不HTTP、不排worker。票只覆盖一个op，不批量执行suffix |
| 每步`admit_raw_dispatch_v1` / `run_probe_once` | 后续仍显式admit及单次调用，不自动loop；逐次验证前驱连续成功、原scope/批准/剩额，签一op票。后包/另一approval不得因当前包结束自动激活 |
| reserve | 原子消费一次dispatch票，并在同SQLite写事务创建固定permit、campaign与全owner/day/storage/cooldown预留、epoch companion receipt与V3 authority/control锚。票绑定campaign/env/DB/epoch/approval/index/key/action、请求hash/帽及前驱refs |
| mark_sent→HTTP | 重核票已消费且绑定此reservation、当前lane/epoch、持久admit/revoke+runtime批准、期限/余额/磁盘/双锚；native+campaign sent/锚同事务提交后才一次HTTP。无私有cap/票的V3 capture、直接ledger/旧mark入口均0 HTTP |
| 回包/结算 | 旧epoch或撤权只结安全hash/用量，正文withheld；unknown按原整帽，既有result/cancel幂等，不因控制失效拒绝安全计量或退款。结果与quarantine非原子时保留raw_pending，不伪造成功 |

票在reserve时消费而非等sent；并发同票只有一个预留者。同operation同请求重放只返既有receipt/permit状态，绝不再次调用capture或重新激活；异scope/ref/reason冲突。若reserve之前纯校验拒绝且未写票/账/锚，可在许可与票仍有效时重新作同次显式调用；出现reserve或任何锚推进后不自动重调。进程/回执未知先查原收据与精确结账，已取消旧permit不复用。

恢复前逐一验证`approval_hash + operation_index/key + action_id + result receipt ref + audit SHA256 + body SHA256`；原PDF index query必须仍在同包、原JSON schema/唯一native/locator精确匹配，人工票不能替代既有代码校验。多页/分包间仍是S0清单中的人工门禁，本修复不建跨approval依赖图。raw暂停在一次成功query之后、PDF尚未reserve时可从原包PDF继续；query unknown、失败或缺raw则不能继续PDF。

pause/revoke与sent争同SQLite写锁：控制先commit则旧epoch不得新sent；sent先commit视为已授权在途，实际socket可能发生在pause之后，不能声称pause可撤回它。sent后/pre-HTTP及回包前再次核epoch，能挡则unknown保守计量，不能挡则仅安全结算/withheld。pause再resume也不能让旧执行器因status恢复active而通过，必须exact epoch/票匹配。

当前读面复用source views/metrics：展示实际版本/lane、profile-ready与sample许可分别状态、停止原因、未决/已用/余量及需人工恢复的步骤。锚/权威损坏应显示blocked/unknown及安全原因，不显示ready/无风险；不得通过读路径补锚、重授票、清用量或推进effective_cutoff。

## 7. 涉及文件、任务顺序与逐项验收

下列全为未来实施任务，本轮不动业务文件。路径均以`src/investment_agent/`为根。

| ID/依赖 | 文件与最小工作 | 验收/退出 |
| --- | --- | --- |
| RP-00 | `supplemental_rules/disclosure_campaign_contracts.py`及新control模块，冻结V3/commands/record tags、版本矩阵、lane/epoch、一次额度及兼容golden | V1/V2 hash原字节不变；未知version/多余字段拒；newV3默认无权；先签工程冻结再实现 |
| RP-01 ←00 | `disclosure_campaign.py` reader/save分派、显式promote；source repo必要窄CAS；control-anchor新模块 | 真file SQLite纯S0已耗账0/3槽提升；旧链逐字节、UUID/路径/用量/sent锚不变；拒sample历史/active/未决/错链/identity；旧reader/CAS缓存对象拒V3 |
| RP-02 ←01 | `disclosure_probe_control.py`或control模块，持久批准admit/revoke、双锚、所有权限/额度写闭包，evidence registry只纯接口 | 重启默认空；撤销不能靠旧JSON复活；回滚掉pause/revoke/admit/票/额度而sent不变也blocked；无新evidence反向依赖 |
| RP-03 ←02 | `disclosure_campaign.py`/control、`evidence/disclosure_profiles.py`窄校验，raw seal与profile-ready绑定 | paused/quiescent+独立S0-R refs；当前profile三hash/build/purpose有效；P1/P2保reserved/帽0；无grant/queue/HTTP/coverage |
| RP-04 ←03 | 独立sample phase许可/一次D、sample-lane激活；`disclosure_authorization.py`、`disclosure_schedule.py`、`disclosure_updates.py`、`research/disclosure_campaign_runner.py`的V3 guards | 例8/40/128后全局余量≤24/152/384且P1/P2各8/64/128时D≤16/128/256，0→D仅一次；raw sealed，lane sample先于grant可激活但无grant0HTTP；新V2 grant/精确P1/P2独立；P3不自动准入 |
| RP-05 ←02，可与03/04并行 | `disclosure_probe.py`/quota/control，pause/原包resume、单次dispatch消费与每检查点epoch | 同原包query成功→pause→resume→PDF成功且query不重取；直接/无票/旧epoch/V2 raw均0HTTP；失败/取消/unknown/过期/撤销/不同hash/build均拒 |
| RP-06 ←04,05 | `coverage.py`、`publication_limits.py`、`disclosure_views.py`、API scope/metrics的只读V3当前限制；native helper所有save路径审查 | 读/prepare/publish/renew同步核binding+phase许可+当前profile/grant/lane/期限；不改历史proof/cutoff；零DML；source.config_hash仍只读 |
| RP-07 ←06 | 新unit/integration及扩现有campaign/probe/runner/authorization/recovery/coverage/migration测试；不改050—054 | 通过第8节矩阵，源码054单head；054三窄表有V3行downgrade拒；不新增空迁移、不松架构baseline |
| RP-08 ←07 | 独立freeze/execution/review/acceptance/source snapshot/log manifest，更新S0运行清单 | 主代理交叉审查+必要全量回归完成，精确记录最后源码与覆盖；只签离线工程，真实S0/profile安装/样本/生产仍待各自许可 |

不新建表/列/索引：全部新JSON/authority在054 guard已覆盖的P4三窄表；新receipt必须仍走其边界。若未来实现把载荷移到未保护旧表，须先另审有具体理由的新guard/迁移，不能沿本计划豁免。旧binary回退遇V3数据须失败，不能靠删除新状态/锚回退；只可用无新写入的隔离一致快照离线核验，生产回退另获授权。

## 8. 离线验证与分阶段退出

仅临时file SQLite、controlled/loopback HTTP及合成批准/评审材料。pytest guard禁非loopback；不得拿当前网站作测试、不造真实故障。案例须验证返回值、DB收据/两账/旧文件hash、外部锚及HTTP调用次数，不只镜像实现。

- **兼容/升级**：V2纯S0 0/3槽、有known与unknown已结算账，提升前后所有旧payload/hash/总账及sent ref相同；sample历史/错slot/partial/预填usage/未来版本拒；新V3默认无发送能力；修复版V2 direct capture/reserve/mark_sent 0HTTP，legacyV2 sample fixture回归；旧P0严格reader和缓存V2保存V3都拒。
- **后绑定**：真实样本形态合成profile在S0后才构造hash，完整走同UUID promote→seal→profile-ready→独立phase许可→sample lane→V2 grant→显式P1/P2；期间profile未装/撤销/过期/build变化、错protocol/contract、错listing/window/URL/用途、未授权/额度不足、重复绑定/填P3、raw未关均无副作用/0HTTP。
- **恢复**：原包完整前驱续PDF的正向；换approval hash/索引指别包、缺/坏raw/audit、非200/unknown/null、current已reserve/cancel/terminal、pending/quarantine失败、原批准过期/撤销、错epoch/DB/path均拒。pause-between-ops支持；pause-after-reserve只能结旧账而不重试，明确能力退出界限。
- **并发**：两会话/独立连接同CAS bind/phase授权/raw与sample activate只一胜；同票双reserve只一预留；pause/revoke在reserve前/后、mark前/后、HTTP前/后、raw落盘前各时点fence；旧executor+新resume不能二次发送；控制失效不漏unknown安全计量。
- **故障注入**：journal追加、source CAS、control锚temp/fsync/rename/目录fsync、sent锚先成功control锚失败、DB commit前后/返回回执丢失、settle后sink失败、revoke DB提交后内存未更新崩溃；无局部退款/隐形生效，ahead必须blocked而非自动修复。
- **备份**：同inode旧SQLite回写保留sent高水位，但回滚掉pause/revoke/票/phase帽，control锚要识别；marker缺失/损坏/DBidentity改变、提升失败残留control锚、新旧二进制共存均拒恢复；控制事件不动原sent marker，sent事件单调推进两锚。保持两锚独立备份边界。新V3 create首次两锚任何一个失败/事务回滚也须测试，残留空sent锚不得被当作可自动修复成功的证据。
- **写闭包断言**：所有V3 source revision变化均有对应authority与control marker推进，至少枚举result、unknown、cancel、recover、grant revoke、sample phase撤销和额度变更；不得只测pause/resume典型路径。sample许可撤销先同事务CAS/控锚再更新runtime状态，既有用量/D不清零。
- **回归**：新契约/真实SQLite/HTTP/worker/只读/API专项；shared auth/coverage/runner受影响须unit、integration、roots、P2/P3/A/07五browser（1440/390、新截图目录）、Ruff/mypy/JS/架构守卫/迁移全组。最终源码修改后复验受影响专项，准确注明全量与最后补丁时点，不累加重叠数字；并行银行/mobile失败独立归属，不擅改或冒称全绿。

阶段退出：RP-00合同通过→RP-01/02版本/双锚安全底座通过→03/04与05两条路径分别通过→06共享读面/权限接缝通过→07/08最终工程验收。任何旧路径可绕票、任一锚可回滚、旧账改变、resume隐含重试或profile绑定隐含授权，均阻断工程退出。

离线修复通过后仍需更新S0-T/P的具名责任、精确URL/主体/W0/用途/绝对时段/留存、scope和额度，独立真实S0/来源评审、profile装载与样本批准才可执行。计划或工程验收不能替这些步骤签字；X/Q/T/N/P继续独立。当前无需先决定人名/实际日期来完成工程方案，也不要求用户本轮批准实现或采集。
