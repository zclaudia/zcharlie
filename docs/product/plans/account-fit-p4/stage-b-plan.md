# P4 阶段 B：有限披露来源与真实样本实施计划

2026-10-17，Asia/Shanghai。范围仅 AF-P4-07—08。**具体计划经交叉审查通过；本次是计划修订，不是实现、联网采集、付费研究、生产操作或启用授权。** 主代理整合，三名 GPT‑6 Luna 子代理分别审查来源/样本、授权/恢复、覆盖兼容/独立门禁；复核裁决另见 [stage-b-review.md](stage-b-review.md)。本文件对 B 的具体裁决优先于[总计划](../account-fit-p4-plan.md)中 B 的早期拟议；不改写 A 的历史验收。

后续状态（同日）：用户已另行授权 AF-P4-07 工程实施，交付与最终签收见[工程验收](stage-b-acceptance.md)、[执行](stage-b-execution.md)及[冻结](stage-b-freeze.md)。以下“本轮仅计划/未实现”描述为原计划审查历史，不覆盖后续实施事实；AF-P4-08、真实付费、生产启用继续未授权。

2026-10-17后续计划接续：依据07最终代码与验收，已交叉复审[AF-P4-08 S0与首批真实样本执行方案](stage-b-source-plan.md)及[复审记录](stage-b-source-plan-review.md)。08具体主体/窗口、campaign总帽、manual-only、raw probe与profile前置、停止恢复及缺样门禁以该方案为准；本轮没有实施08-P0或执行真实采集。

## 1. 实际基线与增量

已核对 [A 验收](acceptance.md)、[冻结](p4-freeze.md)、[复审](review.md)和[源码快照](source-snapshot.json)。HEAD 仍为 `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`，A 是该提交之后的未提交工作区；1229 个 `src/tests/alembic` 文件的树摘要仍为 `b8949477fee7b1f208fa17e34a96a579bdc9f360249d8e27e9444b7332d4a23a`，60 个 A 变更文件逐项匹配。源码迁移图为 053 单 head；未连接部署库。移动端及其他并行改动保留。

| A 最终已有能力 | B 必要增量及代码依据 |
| --- | --- |
| 三窄表、不可变 journal、CAS、P2 受理/检查点、PipelineRun 租约 fencing、due/补偿、精确 pending 恢复 | 不重做这些机制；注册一个来源 poll handler，新增来源专用 typed 协议与原子接收接缝。`update_jobs.py` 中 reserved source kind 不等于已有联网 handler |
| ReviewCycle/dirty 逐成员交接、Mandate/预算/价格/每次付费前检查、unknown 停止；正式 outbox | 不新增研究预算或通知平台；来源grant不授研究/外发权限。本地候选变化仍沿A交接，没有独立研究授权时blocked；已有独立A授权可能产生新研究费用 |
| `DisclosureService`：仅 SZSE 六位 ticker、directory 中唯一 orgId、逐项 issuer 校验、窄 HTTPS 传输、blob/hash、candidate evidence | `DiscoveryRequest` 无 page，`discover()` 固定 `pageNum=1`，category 为空，只回 `truncated`；`execute()` 缓存成功及 error。补有界分页、类型判定、每次 HTTP attempt 身份、429 安全分类与持续覆盖合同 |
| `DisclosureItem.identity_hash` 含公告号、URL、发布时间；`DisclosureOriginal` 保存原件及 prior refs | B 自然键须独立于可变元数据；现有 `fetch()` 会按同 listing/content_hash 复用其他公告的 Original，不能直接作为新公告的来源证明。B 复用 blob 字节，但每个 native 文档保留自己的 provenance/Original |
| `DisclosureBlob.body` 是 SQLite LargeBinary，Original/Evidence 与之同事务 | 首源原件、目录及索引原响应均复用 blob；本阶段不引入文件归档/rename 链或新的原件库。材料文件备份边界沿 A |
| 事件公共仓储只允许 user_report；P3 立即检查新事件 UUID/revision，A 保留同事务原 run 与 journal | 新的仅服务端 candidate ingest 命令及受限 authorizer；不能把公告写成 user_report，也不能放宽公共入口的 verification 写权限 |
| A V2 精确保存来源引用、cutoff 内历史 proof、now 健康检查；同步读/发布/续期守卫 | V2 builder 只查 `evidence_revision`，要求非空 exact refs，并从资料 available/commit 重建窗口；不能表示联网发现窗口、成功空结果或两种水位。B 新增显式 V3 poll proof；V1/V2 DTO/hash 原样保留 |

A 验收的 unit 2491、integration 853、根目录 717 通过及四个浏览器脚本是工程历史证据，不是 B 测试或真实来源证据；本轮不重跑业务测试，也不把这些数字累加到 B。

## 2. 首源裁决与覆盖合同

**推荐首源：CNInfo 上指定 SZSE A 股发行人的年度报告全文，以及与该年度报告有明确原公告号/原件引用关系的更正或补充。** 不是深圳交易所独立适配器，也不自动支持 SSE、BSE、监管政策或全网新闻。B1 只实现这一 source family；先验一个发行人，代码 allowlist 硬上限 3 个显式 listing，不能用行业/账户池自动扩展。

| 合同项 | 具体约束 |
| --- | --- |
| 标识 | 底层原件 `source=cninfo.v1` 保留；P4 feed `cninfo_szse_v1`、contract_version=`1`。合同 hash 绑定 adapter/schema、允许端点/文档类型/时间语义、覆盖和修订策略；授权 hash 单独保存，不能混为来源内容 hash |
| 端点 | 沿代码已有 DIRECTORY、QUERY 与 `static.cninfo.com.cn/finalpage/…` 的窄 URL/HTTPS/public-IP 校验；不扩域、代理、cookie、浏览器采集或任意 URL。端点现时可用性、使用条件和适用限速尚未实证，由 S0 单独验收，不能从代码常量推断已获长期采集许可 |
| 文档类型 | 首批只接受 PDF 年度报告全文及明确关联的更正/补充 PDF；摘要、英文版、季度/半年报、新闻、任意公告及 HTML 不纳入首批正文覆盖。目录索引可保存窗口内全部返回项以审计过滤。类型映射须由真实源字段/官方分类及原件核对冻结；标题关键词仅作 lead。无法可靠区分全文/摘要或关联更正时标 `classification_unknown`，不宣称该窗口正文完整；不得猜 category 常量 |
| 主体 | 本地精确 listing_ref revision=1 → market_code=SZSE + 六位 ticker → 唯一 directory orgId；再逐条比 secCode/orgId。目录歧义、证券改名/重组映射冲突隔离，不能只按公司名、代码前缀或标题归因 |
| 初始窗口 | 建议真实样本选一份年度报告公布日前后共 7 个上海自然日，具体绝对起止经后续授权写入；持续模式从显式 history_start 起，首次历史最多 366 日，以最长 31 日子窗依次补齐，拥挤子窗再拆至 1 日。不能自动抓完一年或全历史 |
| 持续窗口 | 每次固定 poll_end（不得超过授权 end/当前时间），查 `[max(history_start,已确认水位−72h), poll_end]`；UTC 存储并保留原时间精度，查询日期按上海日界外扩后逐项精确筛选。另每 7 日用独立低优先级批次重扫最近 31 日，均计原授权配额。超 31 日迟到/下线数据不承诺自动发现，显式扩大历史必须另获授权 |
| 覆盖含义 | `bounded_window_checked` 只表示该合同、主体、查询窗口内按协议查过，不等于官方站点无遗漏。无远端稳定快照时采用第 5 节两遍封口；无法封口就是 partial。即使封口成功，也必须显示覆盖局限、历史缺口和重叠/深扫范围 |
| 更正/撤回 | 同公告号内容新 hash 是新原件及 candidate revision；不同公告号是另一文档/事件，明确关系存精确 refs+关系来源。标题“更正”、404、索引缺席、原件消失均不能证明撤回，更不能清除旧风险。撤回状态只有可核官方声明才可追加；首版无法验证则保留 conflict/待人工 |

若 S0 无法确认可使用的索引协议、稳定类型或原件取得条件，B 工程仍以封存真实形状响应/本地 HTTP 验证，来源门禁保持未过；不得改成搜索引擎、其他新闻源或扩大来源以凑验收。

## 3. 独立后台授权与配额

新增严格 `BackgroundDisclosureAuthorizationV1`，仅既有认证 API/web 明确提交。登录、环境开关、模型指令、导入、task DisclosurePermission、Mandate、通知设置均不能授予后台来源权限。未授权时无 poll、无外部请求；建议值不自动创建授权。

授权字段至少包含：`authorization_id/revision/owner/operation_id/expected_revision`，服务端 actor/time 与 request_hash；固定 source/feed/contract hash、精确 listing_refs、doc type allowlist、history_start/end、允许 overlap/deep-sweep、最小间隔/有效期、scope 最大失联时限；per-poll query/fetch/HTTP/byte caps，owner-source 自然日及 grant 自然日 request/byte caps、总存储上限；status/撤销时间/reason。整数 strict 且非负、时间有时区；启用授权的必要额度须大于 0，缺省为未授权，不默默补默认许可。模型无批准/改配额工具。

| 项目 | 建议首次受控验收申请值（须用户后续明确授予） | 首版工程硬边界 |
| --- | --- | --- |
| 主体/期限 | 1 个 SZSE listing，授权 7 日；只读指定样本窗口 | listing ≤3，单次授权有效期 ≤31 日，不自动续期 |
| 频率/并发 | 12 小时一轮；同 source 全局网络并发 1 | 最小间隔 6 小时；同 scope 1 active poll，复用 native=4 总上限，不增加后台并发 |
| 每 poll | query HTTP ≤4（含封口第二遍/重试）、新原件 fetch ≤3、全部 HTTP ≤8、响应预算 64 MiB | query HTTP ≤10、发现项去重后 ≤200、fetch ≤20、全部 HTTP ≤32、响应预算 ≤64 MiB；到限 partial 留续点 |
| 每上海自然日 | owner-source 总 HTTP ≤24、响应预算 ≤128 MiB；单 grant 不得大于总额度 | 总 HTTP ≤64、响应预算 ≤256 MiB；提额至硬边界内仍需新授权 revision，超硬边界须先改计划/复审 |
| 响应/重试 | directory ≤8 MiB、query ≤2 MiB、PDF ≤16 MiB；每逻辑请求最多 3 次实际 HTTP | 响应上限不超过现有16 MiB；重试仍吃 poll/day 配额和最小源间隔；封口不足不假成功 |
| 健康/存储 | 36 小时未完整成功即 stale；owner-source blob/索引留存预算 512 MiB，磁盘可用下限 1 GiB | health 不由 timer 续命；命中存储/磁盘阈值停止新请求，保留审计及旧限制，不删除已引用数据 |
| 研究/通知/交易 | 本次来源授权授予的模型/付费研究预算 **0**，外部发送额度 **0**，交易 **0** | 不新增或扩大上述授权；已有独立A研究授权的后续派发按下文处理，不能承诺真实运行总费用必为0。工程验收无已授权case/Mandate/provider时零调用 |

配额是 HTTP 资源授权，不是已存在 BudgetEntry 的货币预算。已有其他任务的披露工具仍沿其 task permission；本授权计入的范围是所有 B 后台 poll（手动/定时/补偿/重试），不能用新 operation、scope 或授权修订绕过。若后续要求后台与 task 共用网站总限额，应另审公共 limiter，B 不暗改旧任务计费/权限语义。

建议额度针对小样本窗口，不保证任意窗口都能在一轮内完成；两遍各两页即用尽4次query，没有重试余量时明确partial/待续。采集本身不创建研究task/正式通知；candidate提交后进入A本地评估/交接，**若另有仍有效的A Mandate/背景研究授权，A可经每次dispatch/admit/reserve检查建立合法的新review cycle/task，可能收费**。无独立授权则blocked；失败/canceled/unknown旧task不能因此重生。B来源grant不批准这条费用，不重设其预算。工程分别验证无授权零调用、独立授权新事实可派发、poll/健康重放不新增task；真实样本环境默认没有付费授权/provider。

轮询间隔12小时（硬下限6小时）约束**新poll启动**，不要求每一页等6小时。HTTP另设owner-source全局`min_request_interval_seconds`，建议≥5秒，S0如要求更慢则取更严格值；每hop、重试、手动poll、补偿均遵守，持久`next_request_at`，等待时yield不占token/租约。该建议也不是对官网限速许可的断言；未实证S0不发送真实请求。

### 3.1 存储与原子记账

继续三张 A 表，不新增授权表/配额账本/租约平台。`supplemental_source_state` 的内部 feed 分别为 `cninfo_szse_auth_v1`（scope=`grant:<uuid>`）、`cninfo_szse_quota_v1`（scope=`owner-day:<YYYY-MM-DD>`、`grant-day:<uuid>:<date>`）、`cninfo_szse_document_v1`（scope=`doc:<H(source,issuer,native_id)>`）。这些内部行 listing_id/context 均空，精确 listing 只在 typed payload 内；不进入 `list_sources()` 的声明覆盖及总覆盖统计。真正来源行仍为 feed=`cninfo_szse_v1`、scope=`listing:<id>:company_research`，账号不进入公司来源 scope。

授权、owner-source cap 的修改、撤销用 source_authorization 根收据绑定完整 command/path/body+CAS；权限扩大创建新 revision，**修订沿用同一个grant UUID**，旧收据不改。创建全新grant也受owner-source/day总限额，不回补旧unknown。配额状态 JSON 固定 `day/timezone, request_reserved/consumed, bytes_reserved/consumed, cap_revision, revision`，单位为整数次/byte；事实账是不可变 source_attempt 分阶段收据。投影可从事实账重建，缺序号/状态矛盾/对账不一致则fail closed，不能自动减消耗额度。

reservation绑定reserve时的上海日；sent事务若已跨日，必须原子取消可证未发送的旧日预留、重核新日额度并迁移预留后才可sent。最终用量属于**sent许可点**上海日，有sent的旧日未决用量永久留在旧日。重新授权、拆scope、新poll不清零owner-source或grant当日累计；未决reservation在恢复前扣除可用额度。终态poll的承接新代次继承原未完成集合、attempt计数/已用量；改变window/scope不是resume，须当前grant内的新受理且仍不逃日总限额。

attempt事实账固定关联 `(logical_request_key, attempt_id, attempt_ordinal, hop_ordinal, hop_id)`；每个hop的reserved/sent/result/canceled阶段各有独立receipt UUID/change_key，例如 `H(attempt_id,hop,stage)`，共同引用attempt_id。仅用户命令根operation_id非空；阶段收据operation_id为空，不能撞A的partial unique(owner,operation_id)。阶段状态推进与quota投影CAS同事务。新worker只能审计/结算旧attempt，不能把撤权/失租时未提交的旧响应升级为可见业务成功。

存储512MiB采用保守逻辑归属：所有B归档响应bytes（即使相同hash再次引用也计入本次留存预算）加对应Item/Original/Evidence/Event/映射/页及阶段收据的canonical payload字节；共享blob物理上可去重，配额不因此返还。每次reserve另预留max_response_bytes+最多1MiB元数据/收据开销；超此元数据上限则提前分批或capacity_blocked，不发请求后无处落审计。quota自身有限计数元数据纳入保留开销，known canceled可返未用预留，已接收归档不自动回收。网络每日字节指所有hop响应body，含重抓/错误，单独逐次累计；request body也有协议长度上限，但不混算为PDF大小。磁盘下限取实际可用空间；逻辑留存配额和磁盘任一不足均停新HTTP，恢复仅经有审计的空间处理，不删已引用数据。

### 3.2 每个实际请求的检查点

1. 短 writer 事务内核 PipelineRun lease/worker/attempt/running、当前设置（supplemental∧updates∧polling∧originals）、当前 grant revision/有效期/scope/合同、频率/源 cooldown/磁盘与空间阈值；同时 CAS owner-source/day、grant/day 和 poll 额度，预留 1 request+本次 max_response_bytes，并写 `reserved`。没有空间容纳最坏响应就不发送，不赌响应小。commit 后才可能联网。
2. 真正调用前再次进入短事务复核同一授权/开关和 lease，写唯一 `sent` 收据后 commit。**sent commit 是许可线性化点**：其后的关闭不能撤回在途 HTTP；关闭在此点之前获锁则零发送。sent 后实际调用前崩溃也按未知已用，不自动退额度。网络期间不持 DB 锁。
3. **首请求及每个redirect hop都完整执行reserve commit →该hop独立sent commit→一次网络调用→result**，各自重核当前setting/grant/lease/URL/public DNS、扣owner-source/day、grant/day、poll的HTTP和字节额度；同一sent不得发送第二hop，禁止库自动follow/retry。hop之间关闭/撤权/失租则下一hop不发，逻辑页partial/unknown，已发hop照计。沿旧传输最多2次redirect，所有query用途hop（含retry）计入query cap；每逻辑请求最多3次attempt，attempt内每hop只调用一次，最终还受poll/day总HTTP cap。HTTP 200/429/5xx、断连、未知响应均计次数；已知实际body字节结算后才释放未用byte reservation，未知按上限消耗。无sent的reservation仅在旧worker attempt已fenced且调用入口严格晚于sent commit可证明时释放，写cancellation审计；有sent无result（包括sent后socket调用前kill）按1request+max_response_bytes消耗，不能靠超时退款。
4. 响应持久化前再次核授权/开关/lease/配置 revision。撤权或关闭发生在途时只存安全 attempt 结果与用量，不创建可见原件/evidence/event、不推进页/水位、不 fanout；响应正文丢弃。结果因失租不能被旧 worker 落业务，恢复者依据已存 sent/结果确认 unknown。已提交成功的原件和事件不撤回。
5. 网络 429/可重试 5xx/超时按持久 retry_at 恢复，不借 PipelineRun 默认 max_attempts=3 快速重打。安全保存经限长/限范围验证的 Retry-After；下一次 ≥ max(源最小间隔、Retry-After、指数退避+jitter)。base 60 秒，上限24小时；超过授权 expiry 则 blocked_expired。schema/issuer/host/hash 身份错误 quarantine；403/401 blocked_source_access；404 原件保留 original_missing。无效 Retry-After 用本地退避；不得越过远端长等待截短重试。

## 4. 接口、身份和生命周期

| 接口/对象（拟议，现有认证/owner 隔离） | 必须冻结的行为 |
| --- | --- |
| `POST /supplemental-updates/sources/cninfo_szse_v1/scopes` | typed grant/create/amend/revoke + operation UUID/CAS；服务端生成授权 hash、范围 hash；重放先验完整原请求再返原结果。不能接任意 source/URL/verified/费用字段 |
| `POST /supplemental-updates/polls` | 精确 source、grant_ref/revision、绝对窗口、operation UUID；只持久入队 PipelineRun，不在 HTTP route 抓取。范围不符403、CAS/请求冲突409、非法/超cap422；queue满保留已有待排根收据，返回 capacity_blocked，不能谎称已开始 |
| `POST /supplemental-updates/polls/{id}/resume` | 原 poll proof/版本/游标+CAS，仅承接未完成 HTTP/桥接；不重取已完成页/成功文档，不重开旧付费 task。poll 已终止、grant/config 改变或 retry 预算耗尽时拒绝，需新的明确操作承接旧未完成集合 |
| GET sources/scopes/receipt/batch/operations | 复用 A 零 DML/read snapshot；增加 grant 到期/剩余额度、index/original/bridge 三进度、窗口/历史缺口、latest complete/attempt/failure/retry、stale、积压及恢复原因。列表有 keyset+revision cursor；内部 feed 不算真实来源 |
| 定时维护 | 沿已有worker/scheduler唤醒持久poll，不新增APScheduler网络cron；missed slots合并为一个固定窗口的补偿poll，不为每个漏跑时刻复制一批；公平算法见下文 |

身份分层：

- `document_key=H(source,issuer_id,native_announcement_id)`，owner 由隔离列绑定；映射行保存首次 Item/所有 metadata revision refs、当前 Original/event_ref/content hash/status/semantic digest。URL、标题、时间修正不得改变业务 UUID；未知 issuer 不建正常映射。事件 UUID 固定 namespace UUIDv5(owner+document_key)。不同公告号即使原件 hash 相同仍有不同 provenance 和事件身份，blob 可共享。
- `poll_key=H(owner,source,grant revision/hash,contract/config/scope hash,fixed window,explicit operation/generation)`，一个 scope 一个 active poll，确定性 PipelineRun key≤128。无无限增长的 JSON 文档目录。
- `logical_request_key` 绑定 poll、请求种类（directory/query/fetch/seal）、窗口、pass、page ordinal、native doc key/locator版本；成功响应的 receipt 重用原身份。每次真实 `attempt_id` 为独立 UUID，绑定 logical key+持久 attempt ordinal+请求 hash；目录、query、原件、redirect 分别记账。失败 operation 不能重用旧 `DisclosureReceipt` 以为已重试。
- 页 identity 不是业务事件 identity。`source_page` 收据含前页 hash、原 bytes blob hash、HTTP状态/安全headers/final URL、issuer目录 proof、acquired/available、页序/count/hasMore、当前/后继 token、文档键集合与去重/过滤/隔离数量；bounded payload≤256 KiB，原 bytes 另存既有 blob。没有源 cursor 就明确 pageNum+固定 window/pass，不伪造远端 snapshot token。

blob可按hash物理共享，但读取原目录/页响应必须先核所属owner的精确page/attempt receipt、listing/合同及hash，只从受限审计接口返回；不新增按裸hash读取任意blob的通用接口，不借共享blob越owner访问。

生命周期：`pending_admission → queued → running → retry_wait / capacity_blocked / paused / blocked_authorization / quarantined → completed`；failed/canceled/expired 保留终态。index、original、bridge 另有完整/未完成成员计数，poll completed 不能覆盖 partial source 状态。恢复者先验证 frozen request、receipt/hash/CAS/lease，再从未确认位置继续；不复活终态 run，必要时建有 audit 的新 poll generation，并继承未决项、用量与重试上限而非清零。

公平算法冻结为source专用dispatcher的有界步骤：内部source_state保存`next_lane`（recent/backfill）和各lane `last_served_scope`，同事务CAS认领；两lane都可运行时严格交替，每lane按scope稳定keyset循环，冷却/撤权/无额度scope跳过并留原因。单次调度quantum最多**1个实际HTTP hop或20条纯本地桥接**，完成后持久checkpoint、释放网络token并让出；同scope两个lane共享active poll，不并发修改窗口。既有PipelineRun租约继续使用，有界子步run用poll+step身份串接，不能sleep持有单网络token；总队列满保留原step/年龄。已就绪scope在本lane一圈内必被服务，若≤3个scope且两lane都就绪，正常无失败时至多6个quantum获一次服务；新任务不重置旧scope排序年龄。cooldown不计可运行，最长等待/跳过原因显示operations。公平测试用真实worker断言服务序列及kill后续序，不仅测最终都完成。

poll是根receipt/冻结请求身份，生命周期保存在source_state/各step证明；每个有界step用现有PipelineRun承租约，不让一个parent run长时间running等待child。`active_poll_id`始终指同一根，`active_step_run_id`可轮换；step完成只表示该quantum交接完成，不是poll/source完整。redirect响应先持久化安全状态和next-hop输入，下一step重新认领token/许可再发目标hop。handler重试先查原sent/result/step checkpoint；未决HTTP走unknown消耗/有限新attempt，不能让PipelineRun通用retry透明重发网络。

## 5. 页协议、修订与恢复边界

首版新增 `DisclosurePageRequestV1`/`DisclosureBackgroundFetchV1`，不要向旧 DiscoveryRequest/FetchRequest 加默认字段重算已存 operation hash。旧 task 工具、旧收据及首屏返回形状保持。B 调用受限的后台服务入口；可抽取纯校验/原件保存 helper，但旧调用行为与防护不得松动。实际 HTTP 传输增加受限 attempt permit 与安全状态/Retry-After 分类，沿已有 pinned HTTPS/URL/响应 cap；不得把测试 transport 暴露为生产可注入参数。

### 5.1 无稳定快照时的封口

固定window、issuer、query排序`sortName=time/sortType=desc`及pageSize≤20；同时间戳tie-break、total/hasMore/末页字段语义由S0实证冻结，不能从现有bool转换推断其可靠性。保存逐页原响应及按native key+metadata/content locator计算的集合摘要。第一遍逐页持久化，见hasMore必须续页，空页+hasMore/重复非前进页/页序断裂/total不一致为partial。第二遍从第一页独立查询至末页，核两遍集合及计数/边界一致；不一致保留drift receipt，不覆盖原页，放弃完整标记并在配额内重开有限pass。每轮逻辑请求最多3次attempt、最多2次封口代次，仍受总query cap；不足就留partial/retry。**无法确认排序/tie-break和末页/总数语义时，即使两遍相等也只可partial，不能生成positive proof。**

两遍相等只能证明**有限观察下的窗口检查**，不能证明远端无遗漏；合同及 UI 必须如此表述。没有可靠末页/完整性字段时不产生 positive poll proof。31日窗超cap可拆为不重不漏的上海日子窗，拆分清单/区间及父窗口 hash 持久化，只有全部子窗完成才推进父窗口水位；1日仍超cap则 capacity_blocked/待人工，不静默截断。迟到项由72h overlap和31日深扫补偿；更老未知范围显示 uncovered。

### 5.2 原件与候选事件

目录/index页接收与正文接收分事务，不能为了等全部PDF而持长事务。每份 PDF 先完整验证 issuer/locator、MIME/PDF签名/bytes/hash，短 writer 事务内原子保存 blob→该 native 文档的 Original/provenance→candidate Evidence→candidate ResearchEvent revision/previous_ref→events_generation→原 P2 mutation/run（若 A/P2 当时允许受理）及 P4 journal→document CAS/bridge checkpoint。队列满或开关关闭但已有合法已提交成果时沿 A 待处理 journal，不丢事件；在途授权撤销则沿3.2拒绝业务接收。

新内部命令建议 `research_event.ingest_disclosure_candidate.v1`，冻结授权/attempt/page/raw blob/原件/issuer/native key/请求hash；只有受限服务端authorizer可独立重建，走封闭私有candidate持久化入口，不调用拒绝candidate的公共create_event，不给公共仓储增加任意verification写参数。公共create/revise_user_report与模型工具保持原权限。事件只描述“该发行人披露了此文档”，verification=candidate，policy_stage=unknown，effective_from/to未知留空，affected_products为空；affected_subjects只含明确发行人/listing，不推广到同业/供应链。B在类型过滤/原件接收层只认PDF，通用validator允许HTML不代表B可接收HTML；HTML/未知类型保留索引诊断且不形成完整正文proof。内容/核实状态/撤回冲突的语义变化才加revision；同hash健康重取不加revision、不触发新研究/通知。元数据更正单独记来源修订；若影响事件可见时间/身份或已有事实陈述，则冲突待核或追加审计明确的candidate revision，不按抓取先后认定事实新旧。

源毫秒字段不自动保证真实timestamp精度；发布时间原值/precision保存在typed文档侧录，date-only采用上海该日结束后的保守可见边界，unknown不伪造午夜已知。ResearchEvent没有独立precision字段，首版通过该侧录及保守available_from继承精度，不向旧事件DTO加默认字段改变hash；available_from至少为server acquired，历史重构不倒填今日采集的知识可见性。

同 native 文档读到曾见旧 hash（A→B→A）不能自动把头改回 A：标 source_revision_conflict、暂停完整 proof/自动研究，待专门核对；源提供可信版本序时才按该序受理。不同公告号更正关系仅在原件明确指向旧文档后核实；对“解除/撤回影响”的研究结论仍须 P3 正式闭环，不随 source 状态更新关闭 issue。

### 5.3 原子边界与失败恢复

| 边界 | 同事务内容与恢复断言 |
| --- | --- |
| R0 poll admission | grant/配额低成本检查、根 receipt、唯一 run/待排状态、scope active CAS；响应丢失重放原根；不联网 |
| R1 reserve/sent | 见3.2；每阶段不可变收据+配额投影+lease fencing；commit后kill按已存状态恢复，不重新清配额 |
| R2 index page | raw response blob+source_page+文档发现/分类清单+cursor CAS；校验整页再写。commit前故障零半页；commit后丢响应复用成功页，原件未取完状态独立 |
| R3 one original | 见5.2，全链和 bridge checkpoint 一起提交；原件已取但桥接失败则回滚该文档的可见新增。已完成的其他文档不重做；旧 worker 不得绕过 lease 写 blob/事件/P2 run |
| R4 window final | 在完整页链/封口、所有 eligible 文档原件/事件交接完成、无unknown分类/冲突、授权仍有效后，最终 source_page proof+双水位+source CAS+有界 pending 交接同事务；失败不推进完整水位 |
| R5 本地评估→研究→通知 | 沿 A 既有 P2 run/精确 outcome/ReviewCycle/Mandate/Budget/正式 outbox；poll 本身不调用模型或发正式结论通知。采集/容量/失败仅本地 operations；无 case/授权清楚待处理 |

`index_complete_watermark`与`original_complete_watermark`分开，另列bridge未完成数；来源行通用complete_watermark取全部所需环节已完成的连续边界，不跳缺口。总coverage判定：无声明内历史证明/授权失效为unknown；HTTP/身份安全失败为failed；任一index封口/eligible原件/bridge/分类/连续区间未完成为partial；全部完成且无冲突才complete。partial/failed/unknown均限制该声明scope的方向性publish，不能用index成功抵消原件缺口。新poll尚未观察变化的in_progress单独显示，可沿仍有效旧proof/原健康期限；一旦发现新eligible待下载/bridge、未知分类或失败，立即将总状态收紧，不等final/timer。失败保留最近成功时间及水位；timer停跑时仍按now判断stale。多子窗/回看不把水位倒退或跳过旧缺口。首次history未补齐不显示整体complete；近期窗口可单列已查而历史仍pending。

HTTP 超时可在原授权中重取只读资源，但不保证远端请求 exactly-once；每次重取收费/请求资源如实计数，业务事件只一份。数据库内不漏已接收变化，站点漏项/下线/未授权历史仍显式 unknown。关闭、expiry、租约丢失、重启、磁盘满、队列128容量阻塞都不能清旧问题/已用量；`resume` 不恢复失败付费研究或未知通知。

## 6. V3 覆盖证明与共享接缝

裁决采用新的 `RuleCoverageManifestV3` 和显式 tagged 来源 entry；**不改变 V1/V2 任何 DTO、默认字段或 hash 域**。没有 B source 时继续原V1/V2；有 B source 的新准备清单使用V3，混合 controlled_local 来源通过 `proof_type=saved_refs_v2` 显式封装原 entry，B 仅允许 `proof_type=poll_window_v1`。任意未知 tag/schema fail closed；不猜测降级为V1/V2。

poll proof 冻结：owner/listing/context、source/contract/config/scope hash、grant ref/hash、poll/final receipt UUID+sequence/hash、publication query window与历史起点、index/original连续水位、page链根/页数/文档数/过滤数、bounded文档交接清单hash、observation/acquired/available/committed、history_epoch、semantic_generation、last_success/health。窗口内文档≤200，叶子清单分≤20条/receipt并有hash/count/精确Original/Evidence/Event refs；final proof 只含有界子收据refs。无新通用ObjectRef kind：内部journal引用使用专用 UUID/sequence/hash 结构，资料引用仍用既有kind。空结果合法地有0文档和真实成功页，不伪造 source_refs。

历史连续区间由每次 final proof 的 previous_final_ref/hash、history_epoch、已确认连续水位和本次区间原子增量证明；补洞/回看不会把新的时间当旧知识。写入时核前驱及本次完整链；当前读只读绑定final、当前final/状态和有界叶子，不遍历全史。按owner/listing/context/合同/scope/epoch/cutoff索引定位历史 proof；需要时增专用partial index，不能把所有source_attempt拉到应用内过滤。审计/重建可离线分页复核全链，损坏/断链即unknown，不能以投影覆盖损坏事实账。

精确持久化：真实来源行payload内固定`history_epoch`（UUID）、`epoch_ordinal`（从1单调增）、`semantic_generation`（从0单调增）、`coverage_anchor`、index/original/bridge进度、bounded active poll。每次事件语义变化在R3同事务同时CAS document映射和来源行、加generation并写不可变语义收据；R2发现pending/unknown立即收紧状态。换epoch同事务写source_authorization/合同变更收据和ordinal+1，generation不归零；合同/文档类型/issuer映射/历史覆盖起点改变或无法复用旧游标的adapter升级必须换epoch。只改更低配额/expiry续修、同合同健康重查不换epoch，不因新grant UUID重置代次。可分页重放epoch/语义/final收据重建；损坏或不单调立即unknown。

有界读取算法：先按专用partial index取cutoff内最新匹配final，核其UUID/sequence/hash/owner/合同/epoch；每个final最多20个页/叶清单ref（按每页≤20条、每窗≤200条上限冻结），叶清单不递归，只含直接精确refs及hash。核当前/绑定final的有界链、计数/区间、raw blob哈希及确切资料可见性；历史连续性由final写入时验证前驱后冻结的coverage_anchor承担，读不递归前驱。当前final必须与同一受保护来源行的epoch/generation/连续起点一致并不收窄，才可维持旧binding；任何缺ref/哈希错/不一致均fail closed。索引/旧schema不可用时不全史扫描兜底，显示unknown/需升级；单scope每种source最多16，超限明确拒绝，不静默截断。

`history_epoch` 在合同/覆盖范围不可兼容变化时换代；`semantic_generation` 只在新native文档、正文hash或事件事实/核实状态/冲突发生变化时递增，普通健康重取、无新增封口、用量/授权续修不递增。后来的健康proof要维持旧结论，须同合同/scope/history_epoch/semantic_generation、覆盖不收窄、无缺口/冲突/失败、当前授权与能力状态符合策略。新事实立即使旧清单受限，即使 P4 worker 没跑；健康续查不反复触发研究。

prepare只选 `committed/acquired/available <= effective_cutoff` 的历史proof及可见原件，now只用于当前运行健康。当前时间、定时slot、poll成功均不扩 `observation.py` collector白名单、不改变旧task effective_cutoff或prepared/hash；无cutoff内proof就unbound/unknown，晚到资料只能等待已有授权内的新任务。

publication/read/renew 共用来源守卫，通过既有 storage 仓储接缝传入 investor，不能增加 investor→supplemental_rules import。关闭或撤销不能把已声明source行移出检查以解除限制；保留声明/审计并显示paused/unknown，后续解除该依赖须新受理/新评审且不关闭旧 issue。V3进入assessment/proposal/review hash并与prepared/published同版绑定；正式closure与发布仍原子。

**迁移裁决：**复用053三表和DisclosureBlob，不新增表；但V3和B专用payload需要独立后继迁移的降级guard（若实施时仍053 head，候选054），不能改已冻结050—053。07.0先以EXPLAIN裁决是否还需poll proof partial index；即使无需新增索引，后继guard仍必要，否则旧053的schema=2检查不能覆盖仅残留在旧表的V3。若需新表必须先证明三表/唯一键和有界payload不能满足并重新审查。053旧二进制不认识V3，不能当兼容回退；有V3数据后只能回退至认识V3的版本或停写维护。后继downgrade guard须检查三表及旧assessment/run/task/grant、DisclosureReceipt的B/V3/source proof载荷，即使新表为空也拒绝返回不认识它们的版本；不在guard中清理/改写数据。实现和启用包必须验证未知schema读/发布fail closed，不能只依赖人工记得不运行旧版。

## 7. 任务顺序、文件与退出条件

相对路径均在 `src/investment_agent/`；“拟”表示尚未实现。每步先冻结expected fixture，三代理分域实现，主代理复核共享DTO/事务/授权接缝后才推进下一依赖。不能将下表子步骤扩展成通用平台。

| 子任务/依赖 | 拟涉及文件 | 逐项验收/退出条件 |
| --- | --- | --- |
| 07.0 契约/接缝冻结；依赖A | 本文、拟 `stage-b-freeze.md`；现有 contracts/update_contracts、仓储/model、disclosures/disclosure_http 的设计 | 类型判定未实证处明确unknown；typed grant/attempt/page/document/V3 hash域、cap、状态机、旧DTO literal、索引/迁移及降级裁决经交叉审查；默认关闭、无授权零请求 |
| 07.1 来源授权和额度；依赖07.0 | 拟 `supplemental_rules/disclosure_authorization.py`；`storage/repositories/supplemental_updates.py`、`api/routes/supplemental_updates.py` | CAS+根请求重放/跨owner、owner-source+grant日聚合、重授/拆scope/跨日不清已用；reserved/sent/unknown恢复与逐hop许可真实SQLite并发验证 |
| 07.2 有界page/HTTP；依赖07.1 | `evidence/disclosures.py`/`disclosure_http.py`或同目录窄协议模块；旧接口不扩hash | 老task入口/receipt兼容；真实本地HTTP分页/原bytes封存/429/漂移/大小/签名/issuer/redirect；封口或partial、双水位、失败新attempt、成功页不重取 |
| 07.3 candidate桥接；依赖07.2 | 拟 `supplemental_rules/disclosure_updates.py`；`commands/admission/jobs`受限映射、supplemental_rules仓储/事件代次/P4 journal | 自然键映射、跨文档同hash独立provenance、同文档revision/previous正确、公共user_report边界不变；原件/事件/generation/run/journal/checkpoint故障原子；无事实/暴露升级 |
| 07.4 durable poll/补偿；依赖07.1—03 | `update_jobs.py`、worker注册/现有maintenance；source_state/operations | 关闭/撤权/失租各边界重核；重启补偿、历史/新窗口公平、source cooldown、满盘/队列不ACK、不借新poll重置配额/失败重试；仅持久poll，不新增网络cron |
| 07.5 V3/read/publish与读面；依赖07.3—04 | `contracts/coverage/publication_limits/views.py`、assessment仓储、`agent/operations.py`、现有API/JS/stock/settings；后继降级守卫迁移/按需索引 | V1/V2原payload/hash字节不变；V3空结果/混合来源/历史cutoff/代次与健康续查；同步读/publish/renew和closure原子、旧事件/critical限制及reduce/exit；1440/390诚实显示三进度/缺口/额度 |
| 07.6 B工程整合；依赖07.1—05 | 新专项及既有disclosure/P2/P3/A测试，拟 `stage-b-{execution,acceptance}.md` | 第9节真实SQLite/HTTP/worker/故障/浏览器及必要全量通过，源码快照/日志/缺口归档；没有live权限则仅工程通过，S门禁仍未验 |
| 08.1 真实来源样本；依赖07.6+独立live许可 | 拟 `stage-b-source-acceptance.md`、样本manifest/probes；原件只存授权临时DATA_DIR/DB | 按第8节记录真实来源/同号重复/元数据或正文修订/更正关系/空窗；不能用fixture或skip补足。无许可则明确未执行，不越权采集 |
| 08.2 适配核验/独立签核；依赖08.1 | 样本审查表、gates；必要专用核验adapter需另有冻结定义 | 首版只签原件身份/时间/关系和“候选披露事实”；verification仍candidate。若要verified，先明确字段级真值、核验权及反例再单独审查，禁止以人工签表直接改verified。来源/暴露/质量/生产结论分别记 |

AF-P4-07 工程退出可独立于真实源取得。AF-P4-08 必须对正常原件、重复、修订/更正、类型/主体不匹配各子门禁分别写通过/未过/未执行；真实更正样本找不到不能宣布完整08通过。来源门禁未过时最多离线/受控临时环境，不开放持续生产；A工程结论不受影响。AF-P4-09—10 的真实收费研究、暴露/解除质量、交易规则和生产启用包仍在阶段C，不能混进本次B实施计划当已获授权。

## 8. 真实样本与独立门禁

### 8.1 样本manifest与评审

本轮**未选定/取得任何真实样本，也未授权联网**。后续建议先选1个SZSE发行人、一份可核年度报告全文和其明确更正/补充；不在计划里编造公告号/原件URL/hash或将仓库FixtureHTTP称真实材料。样本身份由具体ticker/issuer/native公告号确定，不能只报公司名。08.1必须使用没有付费Mandate/provider/外发目的地的隔离临时环境；若改用已有A研究授权的环境，先给出隔离dispatch方案并另获该环境操作授权，或明确另获新研究费用授权，不能把来源采集许可当研究许可。

每份样本manifest记录：case_id、采集授权ref/hash/次数/字节账、source合同/使用条件核对日期、精确listing/issuer目录ref+hash、查询参数/UTC与上海窗口/页链原bytes hash、native公告号/源类型字段/标题、source/final URL、原件PDF bytes hash/长度/MIME、Item/Original/Evidence/Event精确refs及previous、published值和precision、server acquired/available/committed、correction_lead与已核关系分栏、coverage/漏项/不确定原因、重复/恢复结果。原件存临时授权库及可恢复备份；公开报告只存manifest/经批准可再分发的摘录，不把含权限信息的整包直接放Git。

| 真实样本组 | 最小观察/核对 | 未满足时 |
| --- | --- | --- |
| S1 正常原件 | 1份年度报告全文；directory→公告号→PDF内发行人/报告年度对读；SHA256/时间/身份经独立人工复核 | 来源身份门禁未过，不能用candidate标签遮掩issuer不明 |
| S2 同号重复 | 同一公告真实重取一次，同hash；两个attempt/用量，单文档事件revision不增、无重复评估/收费/通知 | 真实重复门禁未过；工程fixture不代替 |
| S3 修订/更正 | 至少1份官方明确指向S1的真实更正/补充，保留新公告号和精确关系；若能取得同号不同正文版本另验，不能要求源提供不存在的历史bytes | 没有更正关系样本则S-correction待验；没有同号历史版本则S-inplace-revision待验。不可制造或把标题词当关系证据；相应自动能力隔离/待人工 |
| S4 类型/主体反例 | 至少1份同发行人的摘要/非目标类型、1份其他发行人或映射歧义的真实候选；应被明确排除或unknown，而非按标题/代码猜测适用 | 分类/主体子门禁未过，窗口不能宣称正文完整 |
| S5 窗口与恢复 | 对明确小窗口人工核索引与eligibility清单、一次真实无目标公告的成功空窗及一次限额内续接；空窗必须人工核真实目录/分类，非空HTTP body中有合法schema/页终止/0 eligible，不能用空body冒充；真实服务错误只在自然发生时记录，不对官网主动施压/造故障 | 站点未发生429/断连时标“真实未观察”；故障工程用本地HTTP测，不能故意触发官网限流充样本 |

样本准备可后续请求受控人工导入真实原件+采集manifest；它可验bytes/身份/类型/映射，**不能代替live分页、限速/使用条件或持续采集可靠性签核**。采集执行者与最终来源评审人应不同；没有独立人工评审人时自动审查只算工程辅助，S仍待签。

### 8.2 门禁及其允许动作

| 门禁 | B 交付要求/当前状态 | 通过后仍不授予的能力 |
| --- | --- | --- |
| B-E 工程 | 07工程已验收，见[最终记录](stage-b-acceptance.md)；08受限执行前置仍未实现，见[08方案](stage-b-source-plan.md) | 不授live/生产/付费 |
| S0 来源可用与使用条件 | 当前未实证；后续有限live只读核对服务条件、端点/字段/分类、速率、原件留存/再分发范围，记录适用日期与约束；不明则blocked | 公开可访问不自动授连续采集或生产 |
| S1—5 来源真实性/覆盖 | 按样本表逐项签核；normal/repeat/correction/inplace/type/issuer/window分栏，候选仍candidate | 不授verified政策事实、公司暴露或质量结论 |
| X 公司暴露 | B只确认文档发行主体，不判断产品/地区/订单/同业受影响；仍待阶段C受影响/不适用/未知真实证据+人工签核 | 不授自动影响/解除结论 |
| Q 研究质量/费用 | P3/A工程不代替真实评审；B来源授权不授付费研究，A独立Mandate仍可能使新研究收费，不承诺运行总费用0；未实测真实token/finish_only/未知费用对账 | 本计划不授背景收费、新case/Mandate或自动续期 |
| T 账户/交易规则 | 仍UnavailableTradingRules，B不改变 | 不授账户ready、自动交易或更宽buy/add |
| N 正式外发 | 仅复用A已有outbox工程，B不授权endpoint；运行告警仅认证operations | 不授外部运行告警/正式结论群发 |
| P 生产运行 | 当前未授权；需后续独立批准代码/DB版本、备份恢复、源/主体/历史/频率/配额/费用/时段/观察期/负责人/回退版本 | 文档、实现、工程或来源签核均不能自动开开关 |

门禁失败的暂停对象按能力区分：S身份/hash/越权/分类漂移→暂停该源新HTTP及桥接；额度/磁盘/积压→capacity_blocked留续点；X/Q失败→停止相关研究交接/后续收费，保留旧结论和current-limits；N unknown沿原delivery不换ID；P未授权→不部署/迁移/启用。恢复只在当前授权/版本/配额/审计核对后有界继续，绝不删除声明source行或把failed改空列表来清风险。

## 9. 验证矩阵与实施验收

所有工程测试使用临时file-backed SQLite/WAL、多连接、本地可控真实HTTP服务、惰性key、无provider/外部目的地；真实HTTPS传输测试用明确test seam验证host/DNS/redirect/timeout，测试注入不开放给生产。必须在真实worker/handler/SQL事务中注故障，不能只改fixture状态断言标签。

| 编号 | 必须覆盖的正反例 |
| --- | --- |
| B-F01 授权/额度 | API→CAS→journal；无授权/撤销/expiry/超scope零请求；并发两个worker、拆scope/新授权revision/上海日界/跨日sent、owner总额度+grant额度；reserve后kill、sent后kill、超时unknown、redirect多hop、重试不得返还已用 |
| B-F02 分页/封口 | 本地HTTP乱序/重复/空末页/空页hasMore/页漂移/total不符/变更插队/迟到回填/窗口拆分与补洞；成功页重用、error新attempt；配额不足停partial；索引成功不掩盖原件/bridge缺口 |
| B-F03 安全/失败 | issuer/host/redirect/DNS混合IP/过期grant、429合法/非法Retry-After、5xx/404/403/断连/slowbody/超字节/PDF伪装HTML/坏schema/hash损坏；严格响应cap且安全错误不泄凭据，逐hop许可，旧task HTTP行为兼容 |
| B-F04 原子/身份 | 原件→evidence→candidate→generation→P2 run/journal→checkpoint每个commit前后kill；同native元数据变化、同hash重复、不同native同hash、A→B→A旧版重现、不同公告号更正；旧worker写全部拒绝；公共user_report不可写candidate/verified |
| B-F05 关闭/恢复 | queued/reserved/sent/response/bridge/final/publish前关闭各开关/撤授权/失租；sent线性化前零发、之后只审计且不落可见新内容；重启/lease接管/补偿、队列128满/满盘/原件缺失、backlog与新窗公平、终态不复活/配额不重置 |
| B-F06 V3/同步守卫 | V1/V2归档literal payload/hash完全一致；V3正常/空结果/混合sources/未知tag/hash坏/缺页/缺原件/缺bridge/新合同epoch；cutoff内外proof、newfacts语义代次立即限制旧观点、同事实健康续查不重研究、now stale且timer不跑；publish/renew竞态仍原子，关闭/撤权/退役不解旧issue |
| B-F07 存储/兼容 | fresh/053→后继降级守卫迁移、ORM/DDL/index EXPLAIN；无B数据往返、有B/V3拒降（含三表空但旧表有载荷；按明确schema/tag识别B，不误拦普通历史DisclosureReceipt）；旧Discovery/Fetch请求与error重放hash、旧cursor明确拒绝、新grant/内部feed不污染A覆盖；DB+blob备份恢复/readback/hash，裸hash不可跨owner读 |
| B-F08 读面/浏览器 | GET零DML/owner/同snapshot/列表cursor；1440/390真实API+worker：未授权、正常/空结果、partial/failed/429/stale、原件/bridge待处理、额度耗尽、关闭/恢复、切listing无残留、历史缺口不可伪0/无风险；probes只归档B |
| B-F09 域回归 | P2/P3/A新critical/新UUID/修订/CAS/精确refs/冻结/reviewhash/closure；财务基础分/Quality/DSL/账户硬约束与reduce/exit不变；effective_cutoff collector白名单无新增；正式通知不被poll成功/重复/运维错误触发 |

拟新增 `tests/unit/test_p4_disclosure_contracts.py`、`tests/integration/test_p4_disclosure_{authorization,polling,recovery,coverage}.py`、真实样本manifest校验及B browser scenarios，复用现有disclosure/P4 fixture server；expected手工冻结，不能由被测service生成。按实际落点记最终文件，不要求为每张表再造镜像测试。

阶段B工程结束必须完整跑unit/integration/根目录三个分区、Ruff `src tests alembic`、mypy全源、JS语法、迁移单head、旧P2/P3/A专项共享接缝及四个既有browser脚本串行+新B场景。沿A记录惰性API_KEY、邻接网关目录、network默认排除及材料缺失skip；真实样本结果另计，不能用network skip声称S通过。不覆盖P1/P2/P3/A历史probes；测试可能覆盖旧探针时先备份恢复，只归档B截图。最终提供唯一测试数量/日志/源码快照/共享接缝复审，专项与全量不相加。

## 10. 后续需用户决定的输入

本计划按最窄推荐值可完成工程设计，以下未决输入不阻塞本轮文档，但在各自动作前必须明确：

1. 是否采用上述CNInfo/SZSE年度报告范围；具体ticker/listing/issuer、报告年度、绝对历史窗口、真实更正样本。未定则不采集，也不换源。
2. 有限live验收是否获准，以及明确grant的有效期/频率/请求/字节/存储配额、使用条件核对责任人、独立来源评审人。表中建议数值不算授权。
3. 是否要求首版通过同号正文替换/撤回关系子门禁；没有真实样本时接受该能力quarantine/待人工，不能把“没找到”写通过。
4. 阶段C真实研究样本/评审人/费用、交易规则供应、外发目的地与生产批准继续未授权；B无需为了接通原件而先决定这些。

本轮结束仅交付经交叉审查的07—08计划；B工程实施、有限live验收和任何生产动作分别等待后续授权。
