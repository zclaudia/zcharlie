# AG28 有界运营与可选外部通知

日期：2026-10-11。本卡完成工程接口、持久调度和离线投递验证；A2 产品开放仍 pending。默认 Agent/background/外部通知保持关闭，不修改真实设置；本次只向 fixture transport 投递。AG20 真实记录仍 6 HTTP / 75,669 tokens / 0 有效研究，FCFE 0/4，production trading rules unavailable。

## 既有队列上的容量与公平性

继续使用 `PipelineRun`、`TaskService`、已有 worker heartbeat/retry 和单一 native Agent；没有 Redis、工作流引擎或第二套研究任务队列。038 的 `agent_operations_cursor` 只保存调度游标，不保存模型历史或新预算。

默认持久 active 容量为 interactive 128、review 96、exploration 32，分别计入 queued/running。设置页高级执行限额可将这些上限调低，不能突破安全上限；`agent_queue_max_running` 为 1–4，默认 4。默认 shared native worker registry 的全部执行最多 4 个，各 class 最多 interactive 4、review 2、exploration 1。全部非 interactive（含 legacy）最多 3，留一槽给交互；调为并发 1 时没有额外保留槽，但加权顺序继续生效。减小上限不终止在途执行，下一 claim 等待实际数量下降。

普通 native enqueue/admit、恢复等待后的 enqueue 和 explicit native run_id claim 均受限；同 key 的既有 Task 仍返回原 identity，不因容量满重新建任务。queue_class 只由可信调用方写入，claim 优先读取不可变授权，模型不能选择预算用途或授权来源。独立 SQLite writer 在短 reservation 中检查容量/候选并 CAS claim，事务结束后才执行网络/模型。研究执行的 lease、设置 revision、取消、scope、unknown usage 和 lifetime/day/purpose budget 边界沿用 AG08。

跨 class 使用持久 `interactive, interactive, interactive, review, exploration, legacy` 槽，空槽借出。后台不会被持续交互无限挤出，交互也不会排在完整探索 backlog 后。每个 class 优先选择最近最少服务的 account/case，然后 immutable enqueue id；等待 300 秒的任务使用老任务顺序，避免持续新增 scope 挤出既有 scope。候选读取先筛 due/重试余量，native 窗口最多 256，legacy 窗口 128，计数及游标跨重启保留。AG23 组合 head 的 least-checked 扫描未替换。

单 worker 仍依次执行，不抢占已运行研究；交互下一 claim 的优先级不等于立即响应。已有模型/tool timeout、Task lifetime 和后台扫描 bounds 仍约束在途工作；一轮可选通知最多增加一次 DNS 5 秒 + HTTPS 10 秒。legacy-only CLI/自定义 worker 的历史单类 claim，以及不传 kinds/run_id 的内部 FIFO 调用，保留原行为；不能声称任意这些消费者受 managed native worker 的容量/保留槽保证。它们可占用全局 running 数并延迟 managed interactive；部署应使用默认包含 native kinds 的 worker registry，4 是 managed claim 上限，不是跨任意内部消费者的系统总保证。

任务重试、deferred resume、过午夜都保留同一 business Task 与累计预算。failed/partial/cancelled 不产生自动新付费任务。已取消任务不会被本卡重排；恢复已知无变化继续静默，暂停恢复由 AG15/23 合并当前 unresolved changes，历史提醒不逐条重放。

## 正向用户授权与外部内容

复用已认证、CSRF 保护的 `/settings` PATCH 和加密 store，界面提供独立“可选外部通知”组。`notification_external_enabled` 必须为用户实际 API/web 写入的 true，endpoint 必须同样来自 API/web；四个 notification 设置不从环境 bootstrap 导入。仅有 env 值或 import actor 不授予外部发送能力。HTTPS 地址和可选 Bearer token 均 Fernet 加密、读回/历史仅掩码，未保存到浏览器存储。invalid authority/private IP/http/query/fragment 被拒；有效配置不代表网络连通性已验证。

AG15 `_upsert` 发生真实内容变化时才冻结 delivery；首次启用不扫描、回放旧站内提醒。stable id 由站内 key、revision、channel fingerprint 得到；fingerprint 包含 destination/token/receiver idempotency 和本次正向 consent 时间。目的地/凭证/能力变化或 disable→enable 使旧 payload 被抑制，即使期间没有 worker tick，也不转发到新渠道。

只导出 schema/id/case/revision、受控 category/stage 和最多 20 个意见/研究公开 refs。没有 private prompts、原始 HTML、用户问题/理由、模型说明或任何凭证。最多 256 pending/retry/sending delivery，超限记录 capacity_blocked，站内提醒继续保留，不在容量恢复后偷偷重放。可选摘要未实现；本卡不产生额外摘要模型费用。

尚未发送的同 key revisions 合并为当前 revision，旧 pending/retry superseded；每次 actual send 都复查 current station revision、当前 case revision、mute/pause/archive 与 consent/channel。控制 revision 变化保守抑制旧 pending，即使 mute→unmute 或 pause→resume 发生在两轮 polling 之间；后续真实站内变化可产生新的 eligible payload。已开始发送无法撤回，控制变更只阻止后续动作。

## delivery-only 重试与 acknowledgement

038 的 `investor_notification_delivery` 是 AG15 站内 outbox 的可选发送投影，包含冻结 public body，不是研究队列。默认 worker 每轮先处理至多一项 due delivery；短事务提交 `sending` 后才建立连接，随后以 id/attempt CAS 结算。未来 backoff 不占据 due/stale batch 窗口。HTTP 成功只表示收到 2xx acknowledgement，不证明物理恰好一次。

真实 adapter 使用用户指定 HTTPS 公网 host、全部 DNS 地址验证及数字地址 pin；TLS SNI/证书仍验证原 host。无代理、ambient cookies/credentials、重定向跟随或响应解压。单次 DNS ≤5 秒、连接/headers/body 总 deadline ≤10 秒、request ≤16 KiB、response body ≤4 KiB；不记录异常原文或 response body。每次 POST 带 stable `Idempotency-Key`。

未知 ack（包括 sending 窗口进程中断）保守记录 unknown。只有用户明确勾选接收端确实保证 Idempotency-Key 去重，才按 30/60 秒 backoff 重试同一 bytes/id，最多 3 次；否则停止，不自动重试、不生成观点、不调用模型、不重建 Task。失效/改变的 channel 或 station revision 不重试旧发送；老 sending 在新 revision 后恢复记录 prior acknowledgement unknown 并 superseded，防止新提醒后重发旧内容。4xx 拒绝/redirect 为 failed；不提供未经用户授权的自动 reanalysis 或 manual-reconciliation 操作。

## 可读运行指标

认证 `GET /investor/operations` 的配置上限适用于 managed native worker / explicit native claims，queued/running 计数读取持久实际记录，不证明任意 legacy/custom 消费者遵守这些上限。它返回每个 purpose queued/ready/running、active capacity、最大并发和 oldest_wait_seconds、持久槽策略、budget_blocked review cycle 数、unknown_usage receipt 数、delivery_states 与 external_notifications_ready。ready 是时间已到的候选数量，不表示成本/授权/并发允许执行；只有真实 execution boundary 能确定预算与授权。readiness 只确认实际正向 consent 与可解密合法 channel，不输出地址/token，也不发送连接测试请求。

## 验证、修复与产品界限

真实临时 file SQLite 的独立 writers 验证 claim contention、class/global/reserved capacity、weighted priority、scope fairness、aging、runtime 下调、重启游标、跨日原 Task/retry/取消。delivery fixtures 验证 unknown ack/restart、只有 delivery 重试且 bytes/id 一致、无模型调用、并发 sender、mute/pause/archive/channel/consent lifecycle、quiet/coalescing、future backoff 不挡 ready、capacity_blocked。production adapter 经 fixture DNS/socket 验证 public DNS 拒绝、POST header、大小界限及 redirect 拒绝，没有真实目的地请求。

S13/A2 集成使用 AG25 fixture acquisition、AG26 实际子进程 HTML parser、冲突与显式原文更正、AG27 保存材料搜索，再执行 native maintenance；没有正式观点时不产生虚假通知或模型任务。既有带正式观点的更正/复核和跨午夜预算回归一起检查。真实浏览器 1440/390 验证默认关闭、invalid endpoint、加密写入/掩码、reload 和 disable，无 fake success API。

初次旧 regression 98 passed / 1 failed 为 exact secret registry 断言未列两个新 encrypted 字段；修正该断言。新测试初轮 36 passed / 5 failed 为测试 helper 的 cancel/case 参数、required reason 和误比较既有旧索引；修复测试调用并将 038 schema 比较限定本卡表。随后 38 passed / 3 failed 仍为漏 reason；补齐。集中 182 passed / 1 failed 为新 settings group 破坏 SPECS 分组顺序；将四个通知 spec 放到连续的 notifications group。后续 70、72 和 274 focused 全通过。以上修前失败不计作成功，也没有更改旧索引。最终冻结前专项 39 passed / 9.36s。首轮完整离线 **2164 passed / 2 failed / 3 skipped / 6 deselected，287.52s**：固定033元数据比较的 `investor_notification` substring误包含 later038 delivery 表；AG16 budget stress fixture 假设能 simultaneous claim 两个探索，与本卡 managed exploration=1 相冲突。只排除固定033测试中的 later delivery 表，并将 ledger stress helper 标明改用既有内部 unfiltered FIFO seam（真实 leases/CAS 仍保留），没有全局放宽生产容量或删预算断言。固定后的相关集中专项 **62 passed，16.63s**，Ruff、mypy270、JS syntax、diff/head038 通过。最终固定 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **2166 passed、3 skipped、6 deselected，278.00s，exit 0**。三 skip 仍仅外部 `ZCLAUDIA_GATEWAY_DIR` 未配置，不计作通过。mypy270、Ruff、JS syntax、diff、head038 与 1440/390 实际浏览器检查通过；测试和浏览器子进程已结束。

迁移只对临时 fresh/037→038→037→038，保留 disclosure bytes；历史固定 037 目标不变，当前 head assertions 随 038。两个新 model 类放在已被 Alembic/OS/browser fixture 导入的 monitoring 模块，不新增遗漏 metadata import。真实 `.dev/data/app.db` 仍 revision 021 / 360513536 bytes / mtime 1791489715，未迁移、未写真实设置或模型/付费/采集/通知。没有发布产品质量、现场渠道可达性或来源覆盖通过的声明。

## AG29 交接

下一卡由父任务依据既有授权按顺序启动；本子 agent 不启动下一卡。多资金用途比较继续使用 AG24 精确账户/持仓 snapshot、约束及未成交方案语义。可使用本卡 queue/purpose caps/operations metrics，但不能换 Task 重置预算、扩大 source/grant 或把 delivery ack 当投资有效性。保持 AG20 质量 pending、FCFE 0/4、trading rules unavailable；本 agent 完成 AG28 后停止，不启动 AG29。
