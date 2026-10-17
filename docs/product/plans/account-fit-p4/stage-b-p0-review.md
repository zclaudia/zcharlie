# AF-P4-08 / 08-P0 工程交叉复审

2026-10-17，Asia/Shanghai。主代理设计、整合与最终验收；三名GPT‑6 Luna子代理分别实施并交叉检查。状态：08-P0专属工程整合/交叉审查通过；共享全量unit与Ruff的银行域问题单列，不能声称全库全绿；S0或真实来源未签核。范围与接缝见[冻结](stage-b-p0-freeze.md)，基线为[08计划](stage-b-source-plan.md)和[07最终验收](stage-b-acceptance.md)。

## 共享接缝裁决

| 发现 | 最终修订及验证边界 |
| --- | --- |
| S0先要求已批准来源协议形成循环 | 独立ProbeApproval控制精确raw operation；不产正常覆盖/Original/Evidence/Event/V3。source profile由后续独立S0人工签核装载，两种registry默认都空 |
| V1 sidecar可能被旧reader忽略 | 独立严格V2 grant/body/command/result，旧V1原字节/hash域不变；V2不允许切换旧wire/campaign/env；旧reader拒绝V2 |
| 换UUID重复poll或第三slot自行放行 | 精确action→唯一poll；第三slot初始reserved/disabled，须独立批准修订补全更正ID/窗口等。原预算与未决状态保留 |
| 只排除scheduler仍可被普通worker领取 | 普通非测试handler要求exact runner内部一次性dispatch capability；generic claim也零HTTP。scope/root/step/lease/env/profile重复核验，恢复不排新HTTP |
| raw与sample分账可绕日帽/并发 | campaign总帽+S0/sample子帽+action帽与native owner/day/storage/cooldown同事务。首次S0 policy声明用精确campaign批准形成权威收据，固定24/128MiB/10s，不授样本/研究；sample grant沿同policy revision |
| sent幂等成功可能被用作再次发送许可 | sent仅一次CAS；已sent/终态再mark失败。unknown扣满响应预留，V2 raw/sample的campaign及共享owner storage也保守扣满；旧V1 metadata-only兼容 |
| raw多动作永久占根、未sent取消无法恢复 | 已sent结算终结one-shot action并释放raw根；取消只终结permit/释放根，新明确批准操作可用原未消费预算，旧permit不可再发送。两个raw动作后可接sample |
| 撤权、关闭和恢复互相混淆 | 发送前重复核当前批准；收包后权限变动只结算安全hash/用量，正文withheld。独立exact recovery票允许旧权限失效后机械结算指定orphan，不授HTTP、不handoff；原attempt计数不清 |
| 旧SQLite备份保留inode仍可回滚用量 | 独立0600/fsync sent锚在SQLite备份之外保留最高不可变sent receipt；同inode回滚也blocked。sent事务回滚导致锚超前时保守blocked，必须离线对账；锚缺失不得自动重建 |
| profile重装复活、用途不核、代码变更沿旧hash | 同hash重装保留revocation；每hop核purpose；build绑定profile/pages/http代码，raw另绑定自身/http。contract与protocol、批准hash独立，当前registry同步影响读/prepare/publish/renew |
| payload没有contract_hash造成误显示未批准 | API scope和operating metrics用source.config_hash做只读输入，不回写历史payload；真实repo行测试验证两读面及零DML |
| memory/URI执行无持久边界 | 实际probe ledger和manual admission/runner拒绝memory/SQLite URI，只file DB。纯契约账本memory单元用例不代表live执行能力 |
| recovery只能停止、不能重新受控采集 | 增加独立绑定原grant/hash/profile/poll/action/env的resume票与paused→active CAS，仅排一步，继承原时段/quota/attempt；generic V2 resume仍拒绝。机械恢复不隐式续跑 |
| raw孤儿仅有底层helper、无ledger接线 | CampaignProbeLedger.recover_hop在file SQLite事务内接精确机械恢复，重启/暂停/撤销下可达，旧permit不可再sent，无网络/正常证据产物 |
| 新包依赖违反架构ratchet | probe编排从evidence移至supplemental_rules，worker入口移至research；不放宽P0依赖基线。并行materials新增边单列保留 |

## 子代理交叉审查

- `p0_source`：来源profile/probe实现及runner/auth只读审查；核默认空registry、V1 live拒绝、V2 contract/protocol双绑、用途、发送/回包当前批准、quarantine失败/回放和file DB门禁。专项通过，未安装真实profile。
- `p0_runner`：runner/调度及source/probe、root quota/anchor与API读面只读审查；generic worker零HTTP、exact capability、ticket机械恢复、完整V3正向→撤销即时失效均有受控验证。正向V3测试最初将cutoff设在合成clock收据之前，测试改为收据后1秒保持时间一致，未放宽生产loader/知识窗。
- `p0_campaign`：新契约/campaign账本及root auth/raw quota/sent锚交叉审查；指出raw根占用和unknown共享storage差异，已修并补双账/原件前后权限与并发测试。

子代理辅助工程审查，不替代独立人工来源/许可/真实样本签核。当前所有工程修订只在临时SQLite和合成/loopback HTTP验证；S0、原件可信性、公司暴露、真实研究质量/收费、交易规则、外发和生产仍未验/未授权。

## 回归及保全裁决

首轮unit架构守卫暴露两条新edge；改模块层次后重启unit。首轮integration为布局修订主动中止，日志保留，不计通过；最终完整分区和专项分别记录，不累加重叠用例。五browser只将截图目标改至独立P0目录，脚本其余内容不变，历史截图保持原字节。

并行bank/materials/mobile改动和提交保留，不恢复、不归属本轮。源码054单head，未连接部署库；050—054和07验收/冻结/源码快照保持历史。最终源码归属、并行差异、完整命令/退出码、日志摘要和状态以[验收](stage-b-p0-acceptance.md)为准。

恢复引用的信任边界：这些是经审查服务端操作者的窄命令，引用与operation不可变审计；代码不声称验证引用背后人工许可文本。未新增公开API、env或模型工具，不由普通maintenance造票。未来真实执行仍需具名、范围、绝对时段与独立评审批准。

最终P4专项177通过、integration972通过、roots717通过、五browser通过、P0 Ruff和全源码mypy466通过。unit为2670通过/1银行失败/2缺原始材料跳过/6network排除，全库Ruff另有并行银行矩阵测试I001；未修改这些外部文件。全量在最后窄恢复补丁前启动，最终177专项与静态检查覆盖最后字节，原全量不冒称最终同一快照单次全绿。campaign子代理再次只读确认active/ref阻断关闭；首次票据+转场+enqueue同事务，同operation早期重放只返回原run，再次暂停不复活。
