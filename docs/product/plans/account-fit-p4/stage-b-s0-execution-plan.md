# AF-P4-08：S0-T / S0-P 实际执行清单

2026-10-17，Asia/Shanghai。**状态：待执行方案，不是执行许可；本轮仅修订文档。** 08-P0专属工程已验收，16个源码及6个测试与其[最终快照](stage-b-p0-source-snapshot.json)一致。本方案由主代理整合、三名GPT‑6 Luna交叉审查，见[本轮复审](stage-b-s0-plan-review.md)。未执行真实HTTP、S0、临时库初始化、registry装载、生产或开关操作，未提交/推送。银行域和移动端并行改动保留。

本清单细化并优先约束[08总方案](stage-b-source-plan.md)中的S0操作；不重写[08-P0验收](stage-b-p0-acceptance.md)、[冻结](stage-b-p0-freeze.md)与历史测试。P0的177专项/972集成/717 roots/五浏览器为工程证据；全量unit银行PDF失败、全库Ruff银行测试I001是当时观察，不在本轮宣称已修或重新全绿。它们均不证明来源、许可、研究或生产通过。

后续的[同campaign连续性离线修复计划](stage-b-continuity-repair-plan.md)已细化严格CampaignV3提升、profile后绑定、raw同包受控恢复和双锚；[复审](stage-b-continuity-repair-review.md)单列。**该修复尚未实现，以下当前代码限制仍成立，不能据新计划执行S0。** 修复后需以工程验收更新本运行清单，不提前采用拟议API或认为raw已可恢复。

## 1. 执行模式与现存阻断

**推荐顺序：先独立补审离线接缝，再批准连续S0→profile→样本执行。** 本轮不实施补丁。如用户只希望先得到来源可用性/不适用的观察结论，可另明确批准下述**S0-only**，但须知当前没有从其已用账本继续到真实样本的完整接口；不能默认选这种模式或将其签成08整体完成。

| 代码事实 | 本清单裁决 |
| --- | --- |
| raw capture不读取普通polling/updates feature flag；靠默认空`SERVER_PROBE_APPROVAL_REGISTRY`、精确campaign/action、时限、revoke和file DB约束 | S0不启普通开关；开关false不是raw kill switch。实际停止用撤销probe批准、暂停campaign和停止唯一执行器；在途结果继续安全结算 |
| `amend_disclosure_campaign`只接受active，actions不可增删/重排，只能启用第三sample slot；sample resume要求已存在sample poll/grant/profile | raw campaign一旦paused，当前无raw采集resume。`recover_hop`只能机械结账，不重新采集。不要套用sample resume或换新campaign清账 |
| 真实profile/contract在S0后才可能确定；P1/P2若初始reserved不能再激活，若初始省略不能追加；approved又须精确profile/contract | 当前不能完成同campaign的真实S0→S1后绑定。fixture预知hash不证明此接缝。S1前须独立离线工程支持原campaign精确后绑定并继承原UUID、绝对截止、全部用量/未决/journal/锚；未经实现与审查则S1 blocked |
| 同一ProbeApproval内必须固定、连续、不可跳的operation序列；PDF必须引用同批准包内更早query | 用第5节固定分包、人审选一页/两页复核分支；不建设动态自动workflow，不将跨包人工检查描述为代码保证 |

S0-only的未来campaign只列第4节8个raw动作、`sample_http_cap=0`、`sample_response_bytes_cap=0`，没有sample_poll actions、sample grant或后台排队权限。本次授权最多8/40MiB/128MiB；lifetime外框≤32HTTP/192MiB/512MiB仅保留后续累计上限，不授剩余样本额度。后续也不能靠新campaign或复制库重新取得完整额度。若必须连续完成真实样本，先解上述工程阻断，不先发S0请求。

## 2. 责任、时段与签票字段（当前全待填写）

角色描述是职责，不要求另组团队。可以一人兼任授权、许可核对和暂停决策；**采集执行者与最终独立来源评审人必须不同**。可讨论“用户负责授权/独立来源复核，Codex作技术操作”的简化配置，但目前没有采用、没有代填姓名，也没有任命AI/子代理为独立人工来源评审人。

| 必填项 | 填写与责任 | 当前状态 |
| --- | --- | --- |
| 授权负责人、批准依据 | 真实姓名/身份、可批准范围、批准原件/记录ref与SHA256；决定阶段、精确请求、额度、用途和期限 | 待填，blocked |
| 许可核对责任人 | 真实姓名；逐用途核对第一手条款/合同/许可及适用范围，记录依据原文位置与结论 | 待填，blocked |
| 采集操作责任人 | 姓名/操作身份；唯一执行器、逐hop核票/记账/留证，不扩大地址、页数或主体 | 待填，blocked |
| 独立来源评审人 | 真实姓名，与执行者不同；核issuer/类型/时间/页序/原件和证据充分性，不以AI建议代签 | 待填，blocked |
| 暂停、恢复批准及账务操作责任人 | 具名联系人及可兼任关系；异常立即停，先结旧账，新的网络动作必须仍有明确许可 | 待填，blocked |
| 实际执行时段 | campaign及T/P各自绝对`valid_from/expires_at`，ISO8601含时区，T/P合计≤24h，campaign≤7日且包含它；不得自动按“现在+24h”滚动 | 待填，blocked |
| 证据保留/删除及备份期 | raw、PDF、许可文件、审计收据、备份分别填起止/可查看人/允许摘录与导出/删除责任；未决账、引用及锚不得随意删除，冲突先停并决定处理 | 待填，blocked |
| 隔离执行环境 | 一次性campaign UUID、sample_env_id，真实绝对SQLite/DATA_DIR/quarantine路径及DB device/inode，0700目录/0600证据与锚、单写者；不使用项目或生产DATA_DIR | 待填，blocked |
| 版本与准确scope | 源/feed、实际隔离listing ref、orgId、native ID、W0、每action/body hash、各批准包完整顺序、probe build/批准hash、环境/许可证据hash | 模板如下，实例化和签核待填 |

批准原件才是人工授权依据；`approval_ref`/姓名/evidence_hashes只是受信操作者写入模型和收据的绑定材料。代码核结构/hash/时间/CAS/预算，不认证人名、签字或许可文本真实性。只有获签后操作者才能装载**probe** registry；不装来源profile registry，不调用V2 sample grant创建、普通daemon或任何生产启用入口。本轮不创建带假人名/假ref/假日期的“已批准”JSON。技术执行交回本会话/模型的内容仅限已明确允许的脱敏metadata/hash；许可和PDF正文默认由具名人工本地查阅，若需要本会话或其他模型阅读正文，须先另获该处理用途许可，不能因Codex负责技术操作就默认授予。

可在一份人审票中分别勾选T和条件性P，不必重复申请已明确授予的动作；阶段、包/分支或必填字段未明确即不授权。保留期限与执行时段不同：前者管保存多久，后者管什么时候能发请求。

## 3. 主体、数据窗口与未核seed

| 项目 | 固定提案与核对规则 |
| --- | --- |
| source/feed | `cninfo.v1` / `cninfo_szse_v1`，仅SZSE；不扩SSE/BSE/另一公司/监管政策/通用新闻 |
| 主体/年度 | 五粮液`000858.SZ`、FY2025；名称候选“宜宾五粮液股份有限公司”。实际listing ref只在获准后的全新隔离库生成并锁定，不查生产ID、不填fixture ID |
| 目录seed | `code="000858"`唯一行的orgId候选`gssz0000858`。缺行、多行、代码/发行人不符即停；目录是来源身份观察，不是公司暴露证明 |
| W0 | 上海自然日半开`[2026-04-27T00:00:00+08:00, 2026-05-04T00:00:00+08:00)`。本次query固定`seDate=2026-04-27~2026-05-03`；对应毫秒闭端点UTC `2026-04-26T16:00:00Z`至`2026-05-03T15:59:59.999Z`。源时间语义仍须核，不用取得时间替代发布时间 |
| 唯一PDF seed | native `1225273091`；`https://static.cninfo.com.cn/finalpage/2026-04-30/1225273091.PDF`。先由本次query确认精确索引locator/主体/年度，再允许GET；不是按历史URL直接下载 |
| 同主体摘要反例 | native `1225273090`，只允许在本次获批query中自然观察或用已有本地材料离线核对，不授其PDF请求；不加入另一主体000568 |

本地[公告归档](../../../../research/industry-data-validation/evidence/run-02/raw/announcements/000858.SZ.json)两者同时间`1777544425000`、同type `01010503||010112||010301`；目前精确type映射不能可靠区分全文与摘要，是profile强阻断。旧PDF 3,085,658 bytes、SHA256 `09133e1f44b3bb4b2cebe211529ad68f5b04b6be10b43870f2a78c947d5910a4`仅作差异线索，不是未来必有此bytes/hash的官网真值。不同hash先隔离，不覆盖旧版、不声称修订关系已核实。

## 4. 精确HTTP动作与配额

以下URL是代码/本地归档候选，本轮未联网确认当前状态或许可。每行一个唯一campaign action，`http_cap=1`；kind为T的`terms`或其余`raw_probe`，URL allowlist只有该行URL，GET body/hash为None、请求体帽0，POST请求体帽192 bytes及精确body SHA256。动作均须有独立批准的operation才能发送；unused不是自由额度。8个slot在campaign初建清单固定；已选T及P动作的实际status=`approved`，条件只存在于外部签票与阶段装载人审，代码没有`conditional_approved`状态。未选择的T slot应为disabled且不得执行。

**八slot模板的启动前提：初始签票已明确条件性预授P-D/A1/A2/B1/B2/O1全部精确scope、互斥分支、额度、保存用途和截止，且只有T逐用途通过后才允许装P的ProbeApproval/调用。** P签核不是T成功自动生成的许可；后续包只落实已签条件。若用户只批准T、没有签明conditional P，不能创建含approved P的八slot campaign；单独T-only清单可只含T动作，但当前不能再把P追加到同一campaign。若要从T-only接P，须先修订方案/独立补工程，不能另起campaign规避总账。不能初建reserved raw再声称现有amend能激活。

| action ID | 阶段/请求 | 精确URL | 响应帽 / 逻辑存储帽 |
| --- | --- | --- | --- |
| T1 | S0-T / GET terms | `https://www.cninfo.com.cn/` | 1 / 2 MiB |
| T2 | S0-T / GET terms | `https://webapi.cninfo.com.cn/` | 1 / 2 MiB |
| D1 | S0-P / GET directory | `https://www.cninfo.com.cn/new/data/szse_stock.json` | 8 / 9 MiB |
| A1 | S0-P / POST query，首遍page1 | `https://www.cninfo.com.cn/new/hisAnnouncement/query` | 2 / 3 MiB |
| A2 | S0-P / POST query，仅首遍确需page2 | 同上精确URL | 2 / 3 MiB |
| B1 | S0-P / POST query，复核page1 | 同上精确URL | 2 / 3 MiB |
| B2 | S0-P / POST query，仅两页分支page2 | 同上精确URL | 2 / 3 MiB |
| O1 | S0-P / GET original，仅同包索引已定位 | `https://static.cninfo.com.cn/finalpage/2026-04-30/1225273091.PDF` | 16 / 17 MiB |

**本批redirect额度=0、自动retry=0、网络并发=1。** 任意3xx都只记录该已发生hop并停止，不自动follow Location/页面链接/JS/Cookie/浏览器/代理。不得挪未用A2/B2去重试或跟跳转；需要新目标先停，另审精确修订及代码能力。本代码虽有redirect校验，purpose跳数、固定全序及原件index使本批不能当成通用跟跳转能力。

T1/T2是最多两个可选择URL：只有确需且已批才请求；人工已提供带出处许可文件可令T HTTP=0。历史webapi仅JS壳、旧disclaimer 404，不保证两首页足以核许可，不重试disclaimer或搜索新地址。

### 固定POST原字节

Content-Type `application/x-www-form-urlencoded`，ASCII、无换行/尾空格；按当前`page_url_body`参数插入顺序。`sortName=time`只是本地fixture现值形成的**待批准探测字面量**，不是已核官方排序字段，也不要求先装live profile才准raw探测。实际返回不能证明其官方排序语义；若要换值，先修改方案/精确body hash及相应许可，不能现场猜。

page1（A1/B1共用body，各自不同action/permit并分别计费）：

```text
pageNum=1&pageSize=20&column=szse&tabName=fulltext&stock=000858%2Cgssz0000858&seDate=2026-04-27~2026-05-03&sortName=time&sortType=desc&plate=&searchkey=&secid=&category=&trade=&isHLtitle=false
```

192 bytes；SHA256 `7f858aab9e2a30d3c0c286e3a590a565367234d8cec4369b3ded156c7aa94915`。

page2（A2/B2共用body，不能换pageSize/keyword/主体/日期）：

```text
pageNum=2&pageSize=20&column=szse&tabName=fulltext&stock=000858%2Cgssz0000858&seDate=2026-04-27~2026-05-03&sortName=time&sortType=desc&plate=&searchkey=&secid=&category=&trade=&isHLtitle=false
```

192 bytes；SHA256 `e8c9d988960883e1fdff8db35664f10c2207027a30f34552134f3d3e4d1728b5`。

### 总账与逐hop预留

1MiB=1,048,576 bytes。两页分支全8hop名义响应34MiB、存储最低预留合计42MiB；一页分支含两条款共6hop、30MiB响应/36MiB存储，**不消费A2/B2**。T为0则各减2hop/2MiB响应/4MiB存储；T只选1则各减1/1/2。P本身最多6hop/32MiB响应/38MiB存储。8/40/128是T+P共同上限，不是各8次，也不授补满差额。

同campaign总账与S0子账、全owner共享上海日桶24HTTP/128MiB、累计storage/cooldown同时约束，取批准/代码/剩额中更严者。每hop间隔至少10秒，来源要求更慢则从严；不是网站许可承诺。每hop预留整行响应帽+至少1MiB元数据，known结算实际body+1MiB，sent unknown消耗整行响应及存储帽，不退款。磁盘至少1GiB并通过代码剩余空间检查；raw/SQLite/WAL/备份物理容量另核，不能将128MiB逻辑帽当实际磁盘占用证明。

raw reserve与sent必须同一上海自然日；跨午夜的未sent不能自动迁移后发送，须机械取消并重新核票，旧permit终态。跨日只换日桶，不清campaign。研究、模型、外发、交易预算均0；没有case/Mandate/provider key/外发endpoint，不借来源请求触发下游任务。

## 5. 固定分包与人工检查点

所有包必须是`DisclosureProbeApprovalV1`，真实具名执行/不同来源评审人、原票ref/evidence_hashes、同campaign/env/DATA_DIR/quarantine、当前probe build、绝对期与精确operations都已锁定。每包index从0连续，`previous_operation_index=index-1`；包内每步等前步200/无failure/raw安全存储才考虑下一步。每次仅显式调用一次`DisclosureProbeExecutor.capture`，不写while循环自动耗尽operations。

| 包 | 固定operations及通过后的人审动作 |
| --- | --- |
| T | 选择的T1、T2顺序列表；人工许可文件可不建此网络包。每响应留hash/字节/取得时刻与可用文本；JS壳或模糊说明记不足，不自动加载脚本。由许可负责人逐用途签结论 |
| P-D | `[D1]`。T允许此次目录/query/raw临时留存、P已签的条件性预授权仍有效且本包操作票已落实才装此包。唯一匹配000858→预列orgId；不符停止，不改stock追随 |
| P-A1 | `[A1]`。取得未按标题/年度过滤的整个固定W0 raw JSON，核字段类型/实际行/页端线索；不是normal parser调用，也不授verified |
| P-A2（条件包） | `[A2]`，仅A1合法且明确需要第二页、人审已放行。若需第三页、页数/hasMore/total冲突或null announcements，停止，不能发page3或换查询缩窗 |
| P-B-one | 仅首遍一页且目标O1存在、前置人审通过时选择`[B1,O1]`；O1.indexed_by_operation_index=0。本包index 0/1，previous=None/0 |
| P-B-two | 仅首遍两页完整且目标已定位时选择`[B1,B2,O1]`；O1.indexed_by_operation_index指本包实际含目标的query，值0或1，必须在装载前锁定。index 0/1/2，previous=None/0/1 |

P-B-one与P-B-two互斥，只装一个；不得并行安装两分支，B1/B2/O1的action one-shot终态也阻止已发送后换批准hash重发。B包签核在首遍已知页数/locator之后进行，且仍在原P条件授权和绝对时段内；若原票没有明确预授该分支，先等补充明确批准，不以观察结果自动授权。单页情况下A2/B2保持未用；不是在一个全8项Approval中跳过它们。

**跨包依赖当前只由操作者与评审人核对，代码不验证。** 后包evidence_hashes与人审清单必须引用前包raw/audit/hash、同DB/env、当前许可、两账余额与签核结论；不得将A包捕获搬成B包内的query、伪造approval hash或索引。若要求这些依赖也由代码强制执行，须先另行离线补工程，本轮不能靠文档承诺自动保障。

O1固定绑定`indexed_native_id="1225273091"`、`indexed_native_id_field="announcementId"`、`indexed_locator_field="adjunctUrl"`、`indexed_locator_value="finalpage/2026-04-30/1225273091.PDF"`；当前probe会核同包先前query中恰好一条匹配及规范化URL。人工还须核该行secCode/orgId/年度、其他页无冲突、两遍ID/时间/顺序/locator一致和PDF获取许可。若locator有前导斜杠/绝对URL或native类型不同，虽归一URL可能相同也先停修订精确票，不现场改字段绕核。

若任一页出现全文/摘要同type、时间/排序/终页语义未知：先停在已捕获页作离线评审，不自动把后页/PDF发完。只有人审明确另准“继续有限raw观察，不用于profile认可”且原票已列的后续动作/额度/保存用途全部覆盖，才可在active原campaign中显式继续；否则以partial/blocked结束。两遍一致最多证明这两次观察一致，一页成功不证明多页边界或全站覆盖。

## 6. S0-T与S0-P独立退出门禁

S0-T逐用途记录`allow/deny/unknown`与精确第一手依据（URL/文件hash/原文位置/版本/取得时间/适用主体）。用途至少分：条款页有限取得、目录/query探测、PDF取得、临时留存/备份、私人查看、摘录/再分发、外部模型处理。公开下载、200、robots或免责声明缺席都不等于许可；缺项只阻对应动作，不由程序做法律判断。不明时获取具名许可核对人的进一步第一手依据，**本轮不自行联系网站或他人**。再分发/外部模型本提案不请求，预算0；条款不足支持raw保存则P全部blocked。

S0-P入场：T已允许对应用途；P明确签票；责任/时段/保留范围齐全；隔离环境身份及唯一执行器锁定；普通poll/agent/background/external等关闭、无provider/下游授权；source profile registry空；只装明确probe包；DB/锚一致、无旧未决、容量/日/S0/总账余额足。T与P批准、捕获、复核状态分别留栏，不把T HTTP成功记作许可通过。

S0-P出场只形成raw pack与S0-R评审：issuer身份、字段/类型、时间精度/可用时点、固定query条件、页链/稳定tie/末页、PDF主体/年度/hash、用途分别为`pass/fail/partial/unknown/not_observed/not_executed`，依据ref/hash与人工姓名/签字/时间完整。全文/摘要不能可靠区分、稳定排序/末页缺可信依据时**不注册live profile、不运行P1**；S0可以得出“不适用”或部分结论。raw不入SourceCoverage/Original/Evidence/Event/V3/fanout，不追认旧fixture，不推进旧任务effective_cutoff。

即使S0-R各项均通过，仍须解决第1节同campaign工程接缝并另获精确profile装载及样本P1/P2许可；S0 pass不是这些许可。真实更正/同号修订/反例/空窗/真实恢复，以及X公司暴露、Q质量/付费、T交易规则、N外发、P生产均未通过。

## 7. 停止、机械恢复与备份

| 触发 | 立即动作 | 当前代码允许的恢复与禁止项 |
| --- | --- | --- |
| 正常200且raw已安全存储 | 两账按实际结算，核hash/前置/余额/间隔，再人工决定下一hop | 只继续原已批action/分支；不因余额有余扩URL/主体/窗口 |
| JS壳、401/403、坏MIME/JSON、身份/分类/时间/页序/locator歧义，或任何3xx | 停后续、保留安全raw/原因/计量；权限不足撤销probe批准并暂停campaign | 已sent action终态，不重试、不换Cookie/浏览器/代理；语义问题可离线判partial，不假报无风险 |
| 已收429/5xx等确定HTTP错误 | 按实际body+元数据known结算，存安全raw/terminal_error，保留Retry-After，停并撤销/暂停 | 已有result，不改为unknown、不退款或重发；Retry-After只作停止/审计依据，不授本批自动等待后重试 |
| timeout/断连/崩溃，发送结果不明 | 停、撤销probe批准/暂停campaign，确认唯一旧执行器已停止；检查是否已有unknown/result marker | capture可能已自动满额settle unknown；仅sent且无result/canceled才用`recover_hop`精确补unknown。不重复计量，不再发该action；无raw采集resume |
| 已结算后quarantine/sink失败 | 保留known/unknown原账，记raw_pending/failed并停；不伪造正文、审计成功或覆盖 | `recover_hop`至多返回既有result，不能补回raw或重发；另审允许的离线文件恢复并核hash，仍不能捏造未落盘原响应 |
| reserved且能证明没有sent | 先禁止旧执行器继续，再精确机械取消，释放原预留 | paused也能取消；旧permit不可再发。只有campaign仍active、原许可仍有效、action非terminal且新精确批准明确时才可能新operation；一旦paused则不能借此重启采集 |
| 授权到期/撤销、预算/磁盘上限、出现第二采集者或未批网络/任务 | 停并撤销/暂停，记录拒绝点、未发送与未决；在途只安全hash/用量，正文withheld | 记账不需仍有采集权限；不延长绝对期，不换UUID/跨日/新grant清预算；需新动作先独立补审 |
| DB路径/device/inode改变、锚缺失/损坏、DB比锚旧、sent事务回滚后锚超前 | 不发网络，封存当前DB/WAL/独立锚和已知最高receipt/audit，离线对账 | 不复制旧库续跑、不删/重建锚解锁，不用旧备份回补余额；无法可靠重建则unknown/blocked，原风险不解除 |

机械入口：`CampaignProbeLedger.recover_hop(*, permit_id: UUID, campaign_id: UUID, sample_env_id: str, operation_id: UUID, recovery_approval_ref: str, now: datetime|None=None)`。受信操作者在明确恢复批准下调用；file SQLite自身事务中，unsent→cancel，sent无result→unknown满额，已有result/canceled只读既有状态；同operation相同scope幂等，异ref/scope冲突。它没有HTTP/registry/sink依赖，不会重发、排任务或签新许可。恢复引用背后的真实签字仍由人审负责。

raw没有native worker lease，不把sample lease步骤套到raw：本次唯一显式执行器需由操作者停止/隔离；终态CAS防旧permit再次sent或冲突结算，但已经sent在途请求不可撤回，只能安全计量/withheld。sample的`resume_disclosure_campaign_poll`仅供后续已存在sample原root使用，不是S0恢复工具。

备份用SQLite一致快照，不单拷活跃.db；独立sent锚保留在备份回滚之外，并保存最高receipt与raw audit哈希。可以制作只读副本离线核hash，不运行其联网执行器。physical副本/证据保存也须在批准期限与空间内。重启后两registry都为空，不自动加载批准。若未来允许active原campaign继续，操作者先核持久票及撤销记录、原账/锚/已用action，再显式装载仍有效且未撤销的包；registry本身不持久化跨进程撤销史，不能仅凭旧approval JSON重装复活。正常阶段结束先核无reserved/未决；S0-only campaign先保留active与空probe registry的账本等待具名收尾决定，或明确一次性pause/terminate并承认不可raw恢复；不得自动用pause充当可续跑的阶段间等待。

## 8. 未来执行包与本轮交付

未来每hop留：stage/包/index/action、campaign/env、许可及evidence hash、request原字节/hash、purpose/method/URL/帽、permit/reserved/sent/result/unknown/cancel精确refs、当前批准/DB/锚校验、取得/提交时间、安全headers/body hash/bytes/quarantine状态、S0/总账/全owner日/storage/cooldown前后值、人工继续或停止决定。不要把`captured_at`（调用前记录）冒充网络完成时间；另以实际收包观察、raw落盘与receipt提交时点留证，来源available time不由本机timer伪造。

未来收尾包留：责任/许可原件refs、用途矩阵、绝对执行与保留期限、互斥分支选择与签核、每子门禁结论/缺口、余额与未决、独立sent锚/备份seal/hash、无残留执行器、批准撤销/收尾状态。原件/许可正文不自动进Git、外发或送外部模型；在repo只归档已获准的脱敏metadata及hash。未发生的真实故障标not_observed，不向官网注入故障。

本轮交付仅本清单、[复审](stage-b-s0-plan-review.md)及[只读核验记录](stage-b-s0-plan-validation.json)。人名、执行时段、许可/保留范围、S0-only还是先补连续链工程尚待明确；这是具体方案的待定项，不是本轮请求执行许可。
