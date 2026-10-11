# AI 页后端实现方案

日期：2026-10-11。状态：AIB1、AIB2 已实现并过门禁（完成记录见[实现任务清单](investor-agent-implementation-plan.md)"AI 页后端完成记录"）；AIB3、AIB4 与前端 AIF 未实现。

本文把 [AI 页 mockup](agent-conversation-mockup.html) 中前端暂时做不了的部分落到后端，并列出前端的配套接入。范围只限 `/app/agent` 这一页用到的会话摘要、重命名、任务与进度读取；研究策略、观点发布、组合和估值不在本文范围，仍以[研究与运营设计](investor-agent-research-policy.md)、[领域契约](investor-agent-contracts.md)为准。任务编号用 `AIB`（后端）与 `AIF`（前端），与 [AG01–AG30](investor-agent-implementation-plan.md) 的编号互不占用。

## 结论摘要

1. **后端缺口比 mockup 看上去的小。** 任务用量（请求数、工具调用、tokens、成本）、工具步骤和 `artifact_saved` 事件已经在发出，前端没有完整接入；主要缺口是**会话摘要读取**（现有列表按 UUID 排序且没有任何任务状态）与**任务进度投影**（计划、步骤与限额，目前要从原始工具结果里自己拆），另有重命名写接口。不要重复补发已有事件。
2. **“确认后才开始”的计划卡与既有契约冲突，建议默认不做。** [领域契约](investor-agent-contracts.md)明确“深入研究它或比较两家公司：给出简短问题与计划，在额度内执行；普通研究不逐步确认”。mockup 里的“开始研究 / 先不做”按钮超出了契约。建议计划只**展示**、不拦截，用户随时可取消；确认闸作为可选项（AIB3），默认关闭，先做技术验证再决定。
3. 推荐先交付 AIF2 已有事件接入，再做 AIB1 → AIB2 → AIF1/AIF2 剩余部分；AIF3 可独立交付。AIB3 只在你明确要“先确认再执行”时才排期。

## 现状盘点（代码事实已核实，真实库版本引用交付记录）

| 事实 | 位置 | 对本方案的含义 |
|---|---|---|
| 会话列表 `ORDER BY id`（UUIDv4，实际是随机序），游标 `after` 也是 id；`Conversation` 含公共 `Record` 字段及 `title / context_refs`；`ConversationRow` 没有独立 `created_at` 列 | `agent/conversations.py` 的 `list`，`storage/models/agent.py` | 活动时间需要新列；创建时间须从 `payload.created_at` 读取，不能查询不存在的 SQL 列 |
| 会话到任务的关系只存在于消息 payload 的 `task_ref` | `MessageRow.payload`，`snapshot` 路由已有同样的 JSON 路径查询 | “需要你处理 / 进行中”可在读取时由非终态任务反查，无需维护冗余状态 |
| `Task.usage`（`tool_calls / requests / input_tokens / output_tokens / cost`）每次预算结算都会写回任务并发 `usage` 事件 | `agent/budget.py` 的 `settle` 与工具计数处 | **用量已是实时的**，前端未消费该事件。成本未知时是带 `unknown_reason` 的 `Quantity`，不是 0 |
| 工具生命周期事件 `tool_state`（`tool_name / state`）已发出；`step_started` 只有固定文案 “Executing authorized agent phase” | `agent/tools.py` 的 `call`，`agent/executor.py` | 步骤文案可由前端按 `tool_name` 映射，不需要后端改事件 |
| 模型保存的 `ResearchProgress` 已含 `question / evidence_steps / completion_criteria / stop_conditions / completed_steps` | `agent/research.py` | **计划数据已存在**，只是在运行中由模型第一次 `save_research_progress` 时产生，且只能经 `GET /agent/tasks/{id}/research` 读到原始结果 |
| `TaskService.settle_action` 在 `ActionResult.artifact_refs` 保存时逐个发出 `artifact_saved`；`save_progress` 返回研究进度引用，最终结论也走同一保存路径 | `agent/tasks.py`、`agent/research.py`；已有 `tests/test_agent_tasks.py` 验证 | 进度与事件在同一事务提交；只需前端消费，不能在工具层再次发出 |
| 限额来自运行时设置：`investor_agent_task_requests=20`、`task_tool_calls=50`、`task_tokens=100000`、`task_seconds=600`；成本上限来自授权与投资约定，仅支持 USD | `config.py`，`agent/execution.py` 的 `cost_limits` | 前端要显示“x/y”必须有限额读取；成本只能显示美元或“未结算”，不能显示人民币 |
| `waiting_user` 与 `input_required` 已有契约，通用 `checkpoint(wait_status=...)` 支持该状态，但当前研究执行路径未调用；`resume` 的应用接入只由依赖完成触发 | `agent/tasks.py` 的 `checkpoint / resume` | mockup 的就地回答表单缺少实际生产者及输入入口，本方案不实现，待有研究需求时再接 |
| `Contract` 基类是 `extra="forbid", frozen=True`；旧任务 payload 以 JSON 保存 | `investor/common.py` | 默认字段只能保证新代码读旧数据，不能保证旧客户端接受新增字段；本方案保持 `Task / Conversation` 形状，使用新的摘要、进度模型 |
| 当前源码迁移 head 为 040；交付记录载明真实库为 021，本文未重新检查真实库 | [实现任务清单](investor-agent-implementation-plan.md)“当前交付状态” | 新迁移 041 依赖真实库先升级到 040；上线前重新核对，见风险一节 |

## 设计决策

| 编号 | 决策 | 推荐 | 备选与代价 |
|---|---|---|---|
| D1 | 完整研究的计划如何呈现 | **展示不拦截**：模型保存计划后立即在任务卡显示，随时可“取消任务”；运行中取消先显示“正在取消”，以终态为准 | 确认闸（AIB3）：需要持有态、过期语义、迁移与测试；且与契约“普通研究不逐步确认”冲突 |
| D2 | 用量与成本口径 | 沿用 `Usage`；成本未知显示“未结算”，已知显示美元；不换算人民币 | 另做汇率换算：契约要求已验证汇率，当前没有，不做 |
| D3 | 会话状态如何得出 | 读取时由非终态任务反查（`waiting_user → 需要你处理`，`queued / running / waiting_data → 进行中`） | 在会话上冗余存状态：写路径更热、易与任务不一致 |
| D4 | 排序与分页 | 新增摘要接口，按 `last_activity_at DESC, id DESC` 键集分页；分组时在服务端先按 `attention` 筛选，再分别分页；旧列表保持不变 | 只对最新一页分组会漏掉更早的待处理会话；跨请求的活动与状态会变化，不能承诺静态快照分页 |
| D5 | 步骤文案谁来写 | 前端按 `tool_name` 映射为中文短语；计划步骤文本来自模型的 `evidence_steps` | 后端发带文案的事件：文案变更要动后端，且事件已保留期内不可改写 |

## 接口变更

新增摘要和进度 GET 均为只读，Agent 关闭时历史仍可读；重命名 PATCH 是认证写操作，沿用现有认证与 CSRF 约束。既有 `Task / Conversation` 响应形状保持不变；`snapshot` 采用显式选择进度投影，避免让使用 `extra="forbid"` 的旧客户端收到未知字段。

### 会话摘要

`GET /agent/conversation-summaries?limit=50&before=<cursor>&attention=needs_you|running|none`（`attention` 可省略；路径刻意不用 `/agent/conversations/summaries`，避免与 `{conversation_id}` 路由歧义）

```json
{
  "items": [{
    "id": "uuid", "title": "公司甲 · 这次下跌改变了什么？",
    "created_at": "...", "last_activity_at": "...", "title_revision": 1,
    "subjects": [{"ref": {"kind": "listing", "id": 78, "revision": 1}, "status": "available", "name": "公司甲", "ticker": "600001.SH"}],
    "message_count": 4,
    "attention": "running",
    "latest_task": {"id": "uuid", "status": "running", "cancel_requested": false, "stop_reason": null}
  }],
  "next_cursor": null
}
```

- `subjects` 优先取最后一条用户消息关联任务的已保存 `binding.base_refs` 中的去重 `listing`；没有消息时取创建会话的上下文。消息可以更换对象，不能只读创建时的 `Conversation.context_refs`；`case / candidate` 等间接引用在创建时解析为 listing，并在内部保存对象投影。按主键批量关联 `Listing / Company`，复用 `authorized_identities` 的身份校验语义，避免逐条调用导致 N+1。身份缺失返回 `status="unavailable"`、`name/ticker=null`，不猜测、不因当前档案归档而使历史列表报错。名称是当前元数据，不声称历史名称已核验。
- `attention` 由该会话**全部关联任务**聚合，优先级 `needs_you > running > none`，按 task ID 去重、限制 task owner；最新任务结束不能掩盖更早仍在运行的任务。`latest_task` 按用户消息 `sequence` 最大值取任务（可为终态），助手旧任务的晚到回复不会替换它；无任务为 `null`。筛选发生在分页之前。
- `last_activity_at` 定义为会话创建、实际新增用户/助手消息时间的最大值；任务事件、取消、用量结算与重命名不改变列表顺序，避免研究过程持续刷屏。
- `limit` 为 1–100；游标包含版本、UTC 六位小数时间、UUID 和筛选值，用有长度限制的不透明编码；解码后校验全部字段，筛选不一致或非法游标返回 422 `invalid_cursor`。降序下一页条件为 `time < cursor.time OR (time = cursor.time AND id < cursor.id)`，取 `limit+1` 判断是否还有下一页。
- 每个请求内使用一致读快照。跨请求有新消息或状态变化时，尚未加载会话可能移动到第一页；前端按 ID 去重并提供刷新，刷新清空各组游标。仅对没有活动/状态变更的数据保证不重复不遗漏，不宣称跨请求冻结快照。

`PATCH /agent/conversations/{id}`：请求体 `{title, expected_title_revision}`，返回 `{id, title, title_revision}`。`title` 先 NFC 与首尾空白规整，再校验 1–256 字符；`null` 清除自定义标题。新增内部 `title_revision` 计数器（初值 1），在会话写锁内检查并更新，冲突返回 409 `conversation_title_conflict`，跨 owner 返回 404；无变化不递增。它只版本化可编辑标题，不递增 `Conversation.revision`，避免破坏现有消息中固定版本的 `conversation_ref`；保持 `created_at / context_refs / next_sequence` 与既有消息不变。响应丢失后重新读取摘要核对，不能把冲突当作保存成功。

### 任务进度

`GET /agent/tasks/{task_id}/progress`；`GET /agent/tasks/{task_id}/snapshot?include_progress=true` 才以 `"progress"` 键并入快照。默认快照保持原形状；投影须与 task、messages、event_cursor 在同一真实读事务内生成，不得另起会话或先读游标再单独读取进度。现有但没有研究授权的任务独立进度接口返回 404；带进度快照仍返回该任务，`progress=null`。

```json
{
  "task_id": "uuid", "task_revision": 5, "research_kind": "company_research", "as_of": "...",
  "plan": {
    "ref": {"kind": "research_progress", "id": "uuid", "revision": 1},
    "question": "…", "saved_at": "...", "saved_at_unknown_reason": null,
    "steps": [{"text": "读取同口径历史现金流", "state": "next"}],
    "extra_completed": ["模型自行补充且不在计划里的已完成项"],
    "completion_criteria": ["…"], "stop_conditions": ["…"]
  },
  "limits": {"basis": "current_policy", "model_requests": 20, "tool_calls": 50, "tokens": 100000, "seconds": 600,
             "cost_usd": "1.00", "cost_limit_reason": null, "finish_only": false},
  "usage": {"tool_calls": 7, "requests": 4, "input_tokens": null, "output_tokens": null,
            "cost": {"value": null, "unit": "currency", "currency": "USD", "unknown_reason": "price_or_usage_unknown"}},
  "elapsed_seconds": 123, "elapsed_unknown_reason": null, "deadline": "..."
}
```

- `plan` 在首次保存进展前或 `evidence_steps` 为空时为 `null`；不按研究种类强制隐藏实际存在的计划。
- 最新进展按 `Task.artifact_refs` 的追加顺序逆向选取最后一个已提交且属于该任务的 `research_progress`，再校验 `ResearchProgress`；现有 `research_artifacts` 查询没有排序，不能直接取返回结果的末项，也不能按随机 UUID 排序。`saved_at` 取该引用对应的已保存 `artifact_saved.occurred_at`；事件已清理时返回 `null` 与 `event_history_pruned`，不假造保存时间。
- `completed_steps` 与 `evidence_steps` 经 NFC 与首尾空白规整后精确匹配视为 `done`；首个未完成项仅在 `running` 且未请求取消时为 `next`，含义是“下一待完成项”，不声称它就是正在执行的步骤。排队、等待与终态的未完成项均为 `pending`。实时工具活动独立按 `tool_call_id` 渲染，不能以工具名称猜测计划项已经完成。未匹配的完成项放入 `extra_completed`，不丢信息。
- `limits` 明确是 **当前策略参考值**，不是历史执行时的额度快照。设置会实时变化，历史任务没有完整策略快照；终态卡标注“当前限额参考”，不暗示旧任务使用过这些上限。使用只读策略投影：从 `load_settings` 与已保存 grant 取数据，按现有 `cost_limits` 的最小值与 USD 校验规则计算；不能直接调用会断言当前 mandate 存在或抛授权错误的执行函数。mandate 被清除、替换或预算无有效美元口径时返回 `cost_usd=null` 和稳定 `cost_limit_reason`；无设置上限之外的成本约束时原因是 `unbounded`，不是费用为零。
- `finish_only` 复用 `research_headroom` 的纯计算规则（当前仅 `company_research / review` 适用），同时计入任务生命周期、上海当日所有任务的 `reserved / sent / settled / unknown` 预算记录，不只看 `Task.usage`。执行层和投影共用无副作用计算函数；读取不得构造 `TaskClaim`、调用 `fence / check_boundary / live_policy` 或写预算记录。仅活动任务显示收尾提示，终态为 `false`；unknown usage 仍须标明待核对，该提示不表示允许继续付费。实际收尾会禁用所有工具（包括保存进展），最终输出由控制器保存。
- `usage` 直接取 `Task.usage`，保持“未知不是零”。
- 时间口径与执行边界一致：从 `Task.created_at` 起包含排队、等待、重试时间；`deadline = created_at + 当前 task_seconds`，授权到期是另外的执行边界。活动任务 `elapsed_seconds=max(0, as_of-created_at)`；终态从保留的终态 `task_state.occurred_at` 得到固定时长，事件已清理则返回 `null` 与 `event_history_pruned`，不能随着查看时间继续增长或用最近 usage 结算时间当完成时间。前端将其称为“自提交起用时”，未知时显示“时长未知”；本期不新增历史策略或时间持久化。
- 暴露面与现有 `/agent/tasks/{id}/research` 相同：只含已提交的公开进展，不含模型私有历史、提示词或密钥。

### 事件

复用已经发出的 `artifact_saved`，payload 形状为 `{"ref": ObjectRef}`；仅 `ref.kind="research_progress"` 触发计划刷新，不在 `agent/tools.py` 重复追加。其余沿用 `usage`、`tool_state`、`task_state`。SSE 允许重放，前端按 `event_id` 去重，不承诺网络 exactly-once。

新前端首次进入、重连和 `cursor_expired` 均读取带进度快照，再从返回的 `event_cursor` 继续事件流；计划与用量刷新合并限频，用响应 `task_revision` 防止较旧请求覆盖新任务状态，同 revision 时再比较 `as_of`，会话切换用请求 generation 作废旧回调，并保留同一任务最新事件用量。终态再读一次快照，获取最终计划与结算；进度失败不阻断任务/消息读取。会话列表在新消息、取消结果和任务状态变化后刷新，回到页签时刷新，历史读取不启用 Agent。

## 任务拆分

### AIB1 会话活动时间与摘要读取

- **目标与范围：** 给会话加活动时间，提供摘要读取与重命名，解决“列表无可靠顺序、无任务状态”。
- **交付与位置：**
  - 迁移 `041`（实施时再次确认 head，若已占用则顺延）：`agent_conversation` 增加可空 `last_activity_at`、非空 `title_revision`（server default 1）、内部创建对象投影 `subject_refs`（JSON，默认空数组），索引 `(owner_id, last_activity_at, id)`；同步 `ConversationRow` ORM。从会话及消息 payload 解析带时区时间，统一转 UTC 后比较，写成与 ORM 相同的六位小数格式；SQLite 的 `DateTime(timezone=True)` 本身不保存时区，读取须按 UTC 恢复。不要对混有 `Z / +00:00 / 时区偏移` 的 JSON 文本直接取字符串 MAX，也不要用降低精度的日期函数。缺消息时取会话创建时间；非法创建时间使迁移明确失败并回滚，不能用当前时间伪造活动。成功升级后所有已有行均有活动值，新写入也保证非空；列可空仅为 DDL/兼容安排，查询直接用已回填列以命中索引。创建对象投影只从已有精确引用/领域主键关系回填，无法解析时保留空投影并标明对象不可用。
  - `agent/conversations.py`：`create` 保存活动时间、标题修订和已解析的创建对象；`submit`、`save_assistant` 只在实际新增消息时于既有会话写锁内更新 `last_activity_at=max(旧值, 新消息时间)`，幂等重放不更新时间。新增 `summaries(before, limit, attention)`：在 owner 范围内以相关 EXISTS 聚合状态并筛选后分页；再以固定次数批量查询取本页消息计数、最新用户消息对应的任务（包括终态）、去重任务状态和证券身份。不可通过仅查询非终态任务推断 `latest_task`；计数包括用户和助手消息。重命名使用同一写锁与独立标题 CAS，不覆盖并发消息写入。
  - `agent/contracts.py`：`ConversationSummary`、`ConversationPage`、`ConversationTitleUpdate`。
  - `api/routes/agent.py`：`GET /agent/conversation-summaries` 与 `PATCH /agent/conversations/{id}`；GET 沿用 `snapshot` 的短事务只读快照写法（先 `BEGIN`），PATCH 沿用认证写事务，不共用 GET 的只读入口。新旧接口都限定 owner。
- **独立验收：** 创建、新增用户消息、助手回答更新排序，重复提交/助手重放、任务事件、取消和重命名不更新活动时间；取消仅更新摘要状态，运行中保持 `cancel_requested=true`，直到实际终态。并发发送/助手完成/重命名不丢消息或覆盖标题，活动时间不倒退。多任务混合状态按优先级聚合，终态最新任务不遮蔽旧活动任务，旧助手回复不改变 `latest_task`；更换消息对象后 subjects 更新，间接对象与缺失身份正确处理；owner 隔离。分组筛选在 limit 前执行，静态数据同时间分页不重复不遗漏，活动变更后刷新可找回移动项，非法/错筛选游标 422；旧列表形状及 `after` 语义不变。PATCH 修订冲突 409、空白/超长标题 422、跨 owner 404、null 清除标题，旧消息引用不变。迁移上下行及不同偏移/微秒时间回填正确，可在临时库 021→040→041 重放；查询次数不随页内会话/证券数量线性增长。
- **边界：** 不改旧列表接口；不在会话上存任务状态；不增加会话归档、未读或删除。

### AIB2 任务进度投影

- **目标与范围：** 把计划、步骤状态、限额与用量整理成稳定的读取接口，并让前端能被通知刷新。
- **交付与位置：**
  - 新增 `agent/progress.py`：`task_progress(session, task_id, base)`，先读 owner 所属 Task，再读 `ExecutionGrantRow`；按任务 artifact 引用追加顺序读取已提交研究结果，不依赖 `research_artifacts` 的 SQL 返回顺序；读取保留事件得到可用时间。只读当前设置与 grant/mandate/预算记录，缺失当前授权不能影响已保存计划和用量的读取。纯计算函数复用预算 headroom 与美元上限校验规则；执行路径继续原样做 claim fencing、实时授权与预算守卫。
  - `agent/contracts.py`：`TaskProgress`、`PlanView`、`PlanStep`、`TaskLimits`。
  - `api/routes/agent.py`：新增独立 progress GET 与 snapshot 的 `include_progress` 参数，投影共用调用方事务；旧默认 snapshot 保持原形状。
  - `agent/budget.py` / `agent/execution.py`：必要时抽取纯计算函数供执行与展示共用，不改变执行错误、预算状态或终止判定；`agent/tasks.py` / `agent/tools.py` 不重复补发 artifact 事件。
- **独立验收：** 无进展或无 evidence_steps 时 `plan=null`；运行中首个未完成项为 `next`，其余 `pending`，匹配完成项为 `done`，措辞不同项进入 `extra_completed`；等待/取消中/终态无 next。乱序 SQL/随机 UUID/幂等重放不影响最新计划选取。tokens 未知保持 null，成本与上限未知保留原因，收尾计入跨日 lifetime 与当前日其他任务的预留资源；terminal finish_only=false。关闭 Agent、取消、授权过期、mandate 被删除/替换时历史计划与用量仍可读，GET 不写数据库；缺当前美元上限降级而非 500。当前策略修改不被表述为历史限额；终态时长固定、事件清理后时间未知，排队/等待包含在计时内。复用已有 artifact 事件，回滚时无事件、幂等重放不额外发事件、SSE 重放可去重。默认 snapshot 不增字段，带进度快照的 task/progress/cursor 一致；独立 progress 对不存在/跨 owner/无 grant 任务 404。清除命令仍由 submit 先建立 grant，不能仅按“清除命令”判 404，应按实际是否有研究授权投影（可 plan=null）。响应不含私有历史、提示词与原始授权。
- **边界：** 不改研究提示词与 `ResearchProgress` 校验；不要求模型的 `completed_steps` 必须逐字等于计划；不改变预算与终止判定。

### AIB3（可选，默认不排期）完整研究的计划确认闸

仅当你明确希望“看完计划再开始”，且接受与契约“普通研究不逐步确认”不同的行为时才做。先做 2 天内的技术验证，再决定是否实现。

- **目标与范围：** 对 `company_research / comparison / review`，在入队前生成一份由服务端确定性生成的研究简报（问题、对象、将核查的维度、完成标准、停止条件、本次上限），用户确认后才开始执行。默认关闭，由运行时设置 `investor_agent_confirm_full_research` 控制，不改变 `MessageInput`；先同步更新领域契约以允许本次确认行为。
- **要验证的点：**
  1. `TaskService.create` 总会同时 `enqueue` 一条运行；需要“已创建但不入队”的持有态，并保证容量统计 `admission` 不把它算进队列。
  2. 复用 `waiting_user` 与 `resume` 时，`resume` 依赖既有 `run_id` 的运行行，持有态需要有可被重新入队的运行行或改造 `resume`。
  3. `expire_due` 对过期等待任务会置为 `partial / failed` 且停止原因为 `evidence_blocked`，未确认的简报应当是 `cancelled`，需要分支。
  4. 确认接口 `POST /agent/tasks/{id}/start` 的幂等键、与 `cancel` 的竞态、以及确认时必须重新校验授权与预算边界。
  5. 现有时间预算从 `Task.created_at` 开始，确认等待会吃掉 600 秒；必须明确独立确认期限和执行预算起点，并对重试/恢复不重置 lifetime 预算。不能只加按钮后假定确认会重新开始计时。
  6. 设置开启后的 pending 任务在设置关闭、服务重启、授权撤销时如何收口；对比研究当前不强制完整计划，确定性简报必须按实际种类生成，不能声称已取证。
- **若实现的交付：** 独立简报读取模型与内部持有元数据（保持既有 Task 响应兼容，新增公开字段需版本/显式 opt-in）、`agent/brief.py`、持有态与确认接口、过期取消语义、对应测试与浏览器用例；前端在任务卡显示简报与“开始 / 取消”。迁移编号按实施时 head 分配，不占用 AIB1 的 041。
- **边界：** 不用模型生成简报（不增加调用、不引入未验证内容）；不对解释与局部核查设闸；设置关闭时行为与现在完全一致。

### AIB4（可选）服务端识别比较

`classify` 现在只按关键词判断；前端目前按 `listing / case` 芯片数量把 auto 改成 comparison，这可能把同一证券的不同引用算成两个对象。若要统一，应先对解析后的 listing 主键去重，且仅在 `auto` 的关键词分类仍为 `local_check`、没有 portfolio 上下文或明确“不研究”约束时考虑比较；明确解释、完整研究、复核和组合意图保留现有优先级。不能无条件将所有两个对象的请求升级为 comparison，尤其不能按组合快照展开的持仓数分类。客户端改为保留 auto 并展示服务端结果，测试相同证券的 case+listing、多证券解释/完整研究/拒绝研究及组合请求；此项独立评审，不与摘要/进度绑定。

## 前端配套（agent.js）

| 编号 | 内容 | 依赖 |
|---|---|---|
| AIF1 | 各组分别请求带 attention 筛选的摘要，独立“更多”与游标；最近再按上海日期分为今天、此前六天及更早，组间互斥。展示对象缺失、取消中，按 ID 合并；刷新重置游标；标题保存带 expected_title_revision，冲突重新读后保留编辑内容 | AIB1 |
| AIF2 | 接入带进度快照：✓ 已完成、○ 待完成，next 标“下一待完成项”，工具活动单独显示；完成/停止标准折叠。用量 null 显示“未知”，USD 成本未知显示“未结算”，保留未知原因；时间标“自提交起用时”。终态限额标“当前限额参考”；finish_only 活动任务显示“正在收尾”。消费 artifact_saved/usage/tool_state/task_state，刷新限频与防旧响应覆盖，断流回退 polling、游标过期重新加载、终态重读。**无后端依赖的部分可先做**：消费现有 usage/tool_state，初始与终态用量取 snapshot.task.usage | AIB2（计划与限额），其余无 |
| AIF3 | “约定与记忆”角标用 `GET /investor/proposals?status=pending`，沿已有 after/limit 分页。有限加载时显示“有待审阅”或“已加载 N 项”，只有取完全部页才显示精确总数；批准/拒绝后和回到页签时刷新。将“操作提案”置顶 | 无 |
| AIF4 | 若做 AIB3：任务卡的简报与“开始 / 取消” | AIB3 |

## 顺序、依赖与估算

| 顺序 | 任务 | 规模 | 前置 |
|---|---|---|---|
| 1 | AIF2 中不依赖后端的部分（用量与工具步骤） | 小 | 无 |
| 2 | AIB1 + 迁移 041 | 中 | 真实库已升级到 040（仅上线时需要，开发与测试不受影响） |
| 3 | AIB2 | 中 | 无 |
| 4 | AIF1、AIF2 其余部分、AIF3 | 中 | AIB1、AIB2 |
| 可选 | AIB3 技术验证 → 决定是否实现；AIB4 | 验证小 / 实现大 | 你的取舍 |

AIB1 与 AIB2 业务上互不依赖，但共用契约与路由需串行集成；摘要含迁移及重命名写入，AIB2 含预算纯函数抽取，不能称为只新增读取。前端先保留旧接口回退，再分别上线。应用回退应保留新增数据库列；真正降级 041 前先停止新代码写入并验证旧版本运行，标题修订/创建对象投影会随列删除丢失，数据库降级须按备份流程执行。

## 验证

- 后端：新增 `tests/integration/test_api_agent_summaries.py`、`tests/test_agent_progress.py`、`tests/integration/test_migration_041.py`；复用既有 fixture 与原生 FunctionModel，不触发真实模型调用。门禁沿用 `ruff check .`、`mypy src`、`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 pytest -q -m "not network"`。
- 浏览器：扩展 `tests/browser/agent_workbench.mjs` 与 fixture 服务，覆盖第一页之外的待处理会话、多任务状态、修改对象、重命名冲突、事件重放/断线/游标过期/乱序刷新、终态最终进展、关闭后历史读取、tokens/成本/时间未知、提案跨页角标，以及桌面与 390px 手机。waiting_user 使用有效契约 fixture，不声称生产已具备输入流程。运行方式见[实现任务清单](investor-agent-implementation-plan.md)里 AG17 的复用命令。
- 记录：完成后按上表在[实现任务清单](investor-agent-implementation-plan.md)追加简短完成记录，写明命令、结果与未验证项。

## 风险

| 风险 | 缓解 |
|---|---|
| 实现清单记录真实库还在 021，041 上线前必须先过 022–040 | 上线前以实际库版本为准，按运维文档备份并在副本验证升级/恢复；本文 review 不升级真实库。041 还含回填与标题修订，须验证失败恢复，不能把可空列当作整条迁移链可无损回滚的证明 |
| 摘要读取在 SQLite 上与写入争锁、JSON task_ref 关联随历史增长变慢 | 短真实读事务，分页前状态 EXISTS 与本页批量聚合，任务查询均限定 owner；用历史量级 fixture 验证查询计划与次数，必要时增加 task_ref JSON 表达式索引并同步迁移，不能假设只查非终态就天然便宜 |
| 实时策略与保留期不足以重建历史限额/保存时间/终态时长 | 明示当前限额参考，时间不可恢复时 null+原因；以后确需精确历史再设计独立持久化收据，不用当前设置或随机 UUID 伪造历史 |
| 模型把 `completed_steps` 写成与 `evidence_steps` 不同的措辞，步骤进度看起来停滞 | `extra_completed` 兜底；不强制逐字相等，避免改动研究提示词与校验；上线后若不匹配率高，再评估在研究提示词里要求复用计划原文 |
| 成本显示被误读为人民币或零 | 接口只给 USD 与“未知”，前端文案固定为“美元 / 未结算”，不做换算 |
| 摘要里的消息预览与对象名暴露范围 | 仅 owner 可读，且只含已保存的用户消息与证券身份，不含模型私有内容；本文摘要未包含消息预览，需要时单独评审 |

## 不做的事

- 不改研究策略、预算与终止判定；纯函数抽取须保持执行守卫语义。不改 `Contract` 基类、既有 `Task / Conversation` 形状或默认 snapshot 字段。
- 不为 `waiting_user` 做回答接口：当前研究路径没有接入该状态的实际生产者，等有真实需求（例如研究因缺投资期限而等待）时作为独立任务设计，前端已按契约文案渲染该状态。
- 不在会话上存储任务状态，不做会话归档、未读标记和删除。
