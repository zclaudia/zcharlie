# S0-T / S0-P 实际清单交叉复审

2026-10-17，Asia/Shanghai。范围：08-P0后实际执行方案的文档细化；没有S0/真实HTTP、数据库初始化、registry装载、开关、生产、提交或推送。主代理总体设计/整合，三名GPT‑6 Luna只读审查；最终清单见[stage-b-s0-execution-plan.md](stage-b-s0-execution-plan.md)，只读核验与保全见[JSON记录](stage-b-s0-plan-validation.json)。

**结论：计划文档经交叉审查通过；不授任何实际请求。** 姓名、实际绝对时段、许可原件/用途/留存范围及执行模式未确定，实际执行仍blocked。用户仅要求解释职责，主代理解释后没有视为用户接受分工或指定姓名。

## 1. 实际基线

开始HEAD为`b5da5a8eb11757801fa4ed0f5c64f14bee3823af`；起始1868项tracked/nonignored文件摘要用于保全。P0的16源码、6测试全部匹配[最终快照](stage-b-p0-source-snapshot.json)；源码迁移054单head，只读源码图不代表部署状态。本轮未重跑业务测试，没有将P0旧全量或并行银行域问题记成当前通过。P0工程177专项及此前972/717等证据仍按[原验收](stage-b-p0-acceptance.md)的时点和限制引用，不累计，不重写。

银行域当前已存在新的单测/支持文件/验收文档等并行改动；移动端Cargo.lock和P1截图等原字节按本轮起始库存保留。以起始hash区分本轮写入，不用当前HEAD diff推断归属；没有修银行测试、改baseline或夹带代码。

## 2. 交叉裁决与修订

| 接缝/发现 | 最终裁决 |
| --- | --- |
| S0需要先approved profile才写query造成循环 | raw probe独立于source profile。将`sortName=time`作为待批准探测字面量，注明只来自本地fixture而非已核官方语义；固定192字节page1/page2与SHA256；不能据返回排序一致签profile |
| 把8项全序写成可跳page2流程 | approval内严格previous=index-1不可跳；分为T、D、A1、条件A2、互斥B-one/B-two，B完整复核query与PDF在同包，PDF index只指本包query。跨包依赖由人审，代码不保证；要求自动强制者先补工程 |
| P初建reserved或T-only后再补P | raw actions不能amend新增/激活。八slot初始票必须明确条件性P的全部scope/分支/额度/保存用途；T未过不装P registry。只批T不能创建approved P，且不能期待后来追加P。source子代理指出该前置，主代理补粗体启动条件 |
| 单页/双页用量误算 | 主代理独立复算：含2 terms，一页6HTTP/30MiB响应/36MiB storage；两页8/34/42。P-only双页6/32/38。40/128是共享上限，不是保证或可任意改作其他请求的额度 |
| 把普通开关当raw停止开关 | raw捕获/账本不读polling flag；停止用probe revoke、campaign pause、停止唯一显式执行器，在途安全计量。没有承诺关闭普通开关即可挡raw |
| 把429和sink失败都按unknown恢复 | 已收确定HTTP错误是known actual result；sink失败通常在已结算之后，只记raw_pending/failed。仅sent无result/canceled才机械recover unknown，已有unknown不重复计量；不借恢复重发 |
| 把raw恢复套进native sample lease/resume | raw无native worker lease；显式操作者停止旧执行器，终态CAS防冲突。paused raw只能recover_hop结账，不能resume采集；sent raw action终态，换批准hash也不能重发 |
| 真实profile后绑定未被fixture证明 | initial sample P1/P2未知hash不能approved，reserved又不能amend激活，省略不能追加；create也不能prefill历史usage。连续S0→S1目前blocked。推荐先独立补离线接缝；若明确选择S0-only则样本cap0，留原账，不换campaign清零 |
| 只写非空ref即当人工许可 | 清单列具名授权/许可/执行/独立来源评审/暂停恢复职责和绝对时段/留存范围，全部待填；代码不认证签名/人名/许可内容。技术操作只回传已允许metadata，正文默认人工本地查看，模型阅读须另获用途许可 |
| 日额度误当每source一份、重启复活撤销批准 | 说明owner-day为当前P4全owner共享日桶，raw reserve/sent同上海日；registry内存重启为空，人工核持久批准/撤销记录后才能显式装载，不仅凭旧JSON复活 |
| URL/seed被写作实时事实或许可 | 仅代码/本地归档候选，无联网确认；两首页可能不足/JS壳，任何3xx本批停止，redirect预算0。目录/issuer/索引/PDF与type歧义逐项核，raw从不入正式证据/覆盖/V3 |

## 3. 子代理审查归属

- `s0_urls`：URL allowlist、page_url_body/固定query validator、本地000858全文/摘要seed、PDF同包索引与one-shot action；再次复核分包及初始条件性P要求，最终请求字节/hash和预算核对一致。
- `s0_recovery`：campaign/raw/native授权同账、全owner日桶、storage、10s间隔、cross-day、sent锚、恢复接口及实际暂停限制；发现raw flag与样本后绑定缺口，并纠正known HTTP错误/settled sink失败的结算表述。
- `s0_gates`：人工许可/签字与模型hash的信任边界、角色解释/未填blocked、用途矩阵、独立来源评审与S0-R/profile/样本/X/Q/T/N/P分门；终审未发现授权扩大，建议明确B包只落实最初预签互斥条件，已纳入。

主代理逐个读实际代码，未照搬子代理“必须先live profile才能填sortName”“一页7hop”等不成立的推论；用纯本地序列化和独立加总交叉核对，保留来源语义未验。子代理只辅助工程与计划审查，不替代后续具名人工来源/许可评审。

## 4. 核验范围与退出

本轮仅：读取git状态/提交、文件hash与054源码图；本地POST原字节/hash和固定query校验；清单数字/本地链接/git diff --check；P0源码测试/历史验收及并行文件保全。没有调用OfficialHTTPS、capture、任何registry install、campaign创建/迁移/worker、真实浏览器来源或生产runtime overlay；没有新业务实现或全量测试“通过”声明。

计划退出条件已满足：精确请求/有限额度、人工责任与待填项、固定分包与人工接缝、已知能力限制、各类结果的停止/机械恢复、独立S0门禁可审查。本轮只留下经审查的方案；执行模式/具名人/时段/保留许可或后续离线工程，均需后续明确选择，不由本轮自动推进。
