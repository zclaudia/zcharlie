# AF-P4-08 计划交叉复审

2026-10-17，Asia/Shanghai。状态：主代理整合及三名GPT‑6 Luna交叉复审通过（仅计划）；本轮只审计划，没有工程实施、真实采集或生产动作。目标文档为[08执行方案](stage-b-source-plan.md)，基线为[07最终验收](stage-b-acceptance.md)及其[源码快照](stage-b-source-snapshot.json)。

## 首轮发现与主代理裁决

| 接缝 | 真实证据与计划裁决 |
| --- | --- |
| 07工程不等于live可运行 | 三代理与主代理确认普通service protocol=None、唯一for_tests入口标fixture。08-P0补非测试raw probe/批准profile注册与受限runner，不能把live接缝写成已完成 |
| S0循环依赖 | 未签协议不能让普通poll产V3，S0也不能靠普通worker自证。受限raw probe只归档隔离raw+用量；先核使用条件，S0签核后才申请装载精确profile与样本授权 |
| 样本自动扩张 | 当前V1无manual_only、无campaign累计帽；正常maintenance会排active grant，换grant/跨日不能当一次性总预算。新严格版本与内部CAS总账、精确root runner/recovery-only是前置工程；旧V1/hash不重写 |
| 真实分类障碍 | 000858归档全文/摘要type复合串相同，不能映成full或按title猜；旧keyword结果不证明P4无keyword查询，旧count0/null不算空窗。没有可靠区分则S0/live保持blocked |
| 页与时间 | 单页totalpages=0和一次tie观察不证明稳定页协议；W0自然日半开表述转当前闭端wire最后1ms，原始时间单位不等于真实精度；不通过改query或时间语义凑通过 |
| 配额算术 | S0 8/40属于campaign32/192，样本子帽24/152；3×64不是保证可用。redirect/retry/unknown/条款/raw/元数据均归账，单写者及旧备份不自动恢复联网 |
| 独立门禁 | normal/repeat仅部分来源签核，更正/同号修订/反例/空窗/恢复缺样各留未验；独立人工评审不可由子代理代替；X/Q/T/N/P不随S0/S1通过 |

## 修订稿复核

三名GPT‑6 Luna均复核了具体修订稿，结论如下；没有未解决的计划阻断项。

| 复核者 | 结论及收尾修订 |
| --- | --- |
| `p4b_sources`：来源/样本 | 通过。确认seed仅本地线索、W0闭端转换、同type全文/摘要歧义、20条无keyword查询及条款缺口；主代理已修正§8省略路径与实际模块名 |
| `p4b_recovery`：授权/配额/恢复 | 通过。确认总帽/日帽、unknown、旧grant hash、exact runner与备份边界；主代理采纳第三poll服务端reserved动作清单及零受理/零HTTP反例，不能靠余量放行 |
| `p4b_gates`：信任/兼容/门禁 | 通过。确认08-P0未实施、S0分用途许可、raw不产V3、批准profile不自签、缺样不算08通过；S0-P的PDF固定到精确seed ID/locator，不同则停 |

主代理交叉核对：S0-T/P共用8 HTTP/40MiB，计入campaign32/192；样本子帽24/152不自动扩展。P1/P2为拟议normal+repeat，第三次须明确批准；独立门禁可同票分栏授权，已有明确许可不重复索取。分类、页序、末页或权利不能核实即blocked/partial，不保证首源最终可用。07工程验收保持原文，08-P0缺口不被写成07未通过。

本结论只验收可审查的执行计划。08-P0未实施、S0及真实样本未执行，X/Q/T/N/P均未因此通过。后续执行仍需实际时段、具名责任人、用途/保存条件及精确scope/配额；本轮没有签核真实来源能力或授予执行权限。

## 文档与工作区保全检查

主代理只写本次两份08方案/复审及五处文档入口（README、B计划、gates、P4总计划、产品设计）；未写src/tests/alembic、运行配置或07历史验收/冻结/日志/源码快照。起始1742项tracked/nonignored文件清单用于逐项比对；07自身43项源码及其历史证据保持原字节，移动端与P1既有探针也未改写。七份文档142个本地链接均存在，`git diff --check`通过；纯文档没有重跑业务测试。

起始HEAD为`7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`，1261个源码/测试/迁移文件与07全树摘要完全匹配。收尾抽查HEAD为`b75d5f28431222f3a56ab5c18d86ef2f9770dbef`（并行银行域提交`feat(industry): add bank annual online collection and refresh`）；另观察到`industry_peers/originals.py`及bank annual/rollover文件增量。这些均保留且不归属08，不能将本轮宣称为提交者，也不能把变动后的全树写成原07快照。该次抽查1270文件摘要为`89887a54135484fb468f767c399684219e2dea7ae4386906a9a92cb350f97097`，只是工作区观察，不替代冻结或验收。

本轮未联网、未执行真实采集、未连接生产库、未操作开关、未提交或推送。源码head054来自本地迁移图，不声称部署head或生产runtime已经核实。
