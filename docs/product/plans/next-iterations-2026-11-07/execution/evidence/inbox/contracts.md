# IT-001 收件箱业务合同 v1

日期：2026-11-07。范围：闭合 Contribution、已有 EV 类型复用、独立 revision、共享 cursor head 的读取形状与本地 adapter 协议。不是持久化、生产者、投影、动作或 EV-12 的验收记录。

本卡只新增 `research_inbox/__init__.py`、`research_inbox/contracts.py`、`tests/inbox/conftest.py`、`tests/inbox/test_contracts.py` 与本文件。未修改 ObjectRef、正式方法白名单、事件三表、业务模型、迁移或任何共享注册。

## 共用身份与引用

Subject、ListingSubject、AccountSubject、ExactRef、PlannedObjectRef、EventType、StoredEvent、InboxRebuildState 直接引用 EV-01 定义，未复制另一套含义。证券 id 使用 strict 正整数，账户及事件 id 使用 UUID；质量实验引用不属于 ExactRef。所有 DTO 继承原 Contract，未知字段拒绝，时间及 Decimal 由原 Contract/DecimalValue 与 EV canonical 规则处理。

## InboxContribution.v1

| 字段 | 合同 |
|---|---|
| schema_version | 默认为 strict 整数 1；不替代 source_revision 或事件 sequence。 |
| owner_id / subject | 来自可信持久事件，不来自 HTTP 用户覆盖；账户与证券身份互斥。 |
| event_ref | event_id UUID 与已保存 sequence strict 正整数。 |
| exact_source_refs | 1–100 条共用 ExactRef；按 EV canonical 集合语义排序去重。 |
| materiality | attention / context；不是自动研究授权或投资信号。 |
| reason_code | 有界、闭合字符集的本地业务代码。 |
| evidence_status | current / source_unavailable / source_invalidated / superseded / historical；不将候选材料升级成 verified。 |
| display_text | 1–300 字纯文本摘要，拒绝 HTML、控制字符、换行及 URL/Markdown 链接。适配器使用服务端受控文案，不复制来源原文；DTO 无法判断语义上是否抄写了普通短句，该责任仍由具体 adapter 及审查承担。 |
| case_ref / episode | 必须成对出现，仅证券 subject 可有；case 为精确整行版本，episode strict 正整数。 |
| claim_ref | 精确 thesis/recommendation 父版本 + 稳定 field_path；不按相似文字寻找论点。实际子项存在、归属与稳定身份由精确 resolver 核验。 |
| topic_ref / monitor_condition_ref | 按 BASE-02 分别为精确 topic_revision / topic_condition；复用 EV PlannedObjectRef，未注册正式引用或发布能力。 |
| decision_ref | 精确、不可变 decision 引用，revision=1。 |
| reviewed_relation_ref | 精确地址，不是核验收据；仅有引用不能推断已评审支持/反对关系。Contribution 不接收 relation 字段。 |
| replaces_refs | 最多100条精确引用，canonical 集合语义。 |
| allowed_actions | 冻结 account fixture 的可选呈现提示，最多6个不重复的闭合值。账户仅 read/snooze/view_decision/view_check，拒绝 acknowledge/request_review。实际可执行动作仍由实时策略与原领域授权决定。 |

一条贡献只对应一个 subject；不允许任意 fan-out 或把账户拆成持仓证券卡。无 case 的证券贡献不自动创建 case。来源不存在、撤权、归属不符及未注册可选 resolver 不因 DTO 解析通过而获得发布/投影资格。

## 三种 revision 与持久 generation

`InboxRevisions` 分别保存 row `revision`、`content_revision`、`read_content_revision`，约束 `0 <= read <= content <= row`，前两者从1开始。unread 是 `read < content` 的派生属性，客户端不能提交来覆盖它。已读增加 row CAS、推进已读内容版本；新实质来源增加 content 与 row；纯重放不变更它们。具体事务更新由后续投影与操作任务实现。

`InboxRebuildState` 是同一个 EV 类，闭合 candidate_generation/domain_watermark/journal_watermark/last_replayed_sequence/state；不存在第四张收件箱 head 表。`InboxProjectionHead` 只描述 EV projection_cursor 的 research_inbox.v1 行：scanned_sequence、revision、active_generation、rebuild_state。重建须已有 active，candidate 不等 active；ready 须已重放到捕获的 journal 水位。切头、追赶、水位新鲜性、在途分片结清及 CAS 由后续 EV/IT 服务共同检查，DTO 不冒充这些并发保证。

## Adapter 协议

```python
class InboxAdapter(Protocol):
    specification: AdapterSpecification
    async def adapt(self, session: AsyncSession,
                    event: StoredEvent) -> InboxContribution | IgnoredContribution: ...
```

specification 绑定本地 name、projector_version、已知 event_types 及 schema_versions=(1,)。调用方先使用 EV 白名单 resolver 回读精确来源，再调用 adapter；session 及外层提交均归调用方，adapter 不自行 commit、不创建独立 session、不执行模型/网络或 HTTP 回调。输出单条贡献或明确 ignored(reason)，不把未交付适配器计为覆盖成功。

`validate_adapter_output` 将 owner、subject、event_id、sequence 与持久事件匹配，并要求 exact_source_refs 保留事件源整行引用。它不替代来源存在性/owner 授权校验。IT-005/021 仍须回读 reviewed_relation_ref 并核对精确来源 hash；没有已核验关系只能呈现 unlinked/context。字段解析与协议存在不注册任何具体适配器，也不扩大 publication/审批/AG22 的正式采信范围。

## 离线验证

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/inbox/test_contracts.py -m 'not network' -q
.venv/bin/ruff check src/investment_agent/research_inbox tests/inbox
.venv/bin/python -m mypy --follow-imports=silent src/investment_agent/research_inbox/contracts.py
```

测试覆盖冻结 listing/account 贡献、strict 身份与三 revision、非法/实验引用、账户动作约束、受控展示字段、默认与集合 hash、重建 head、水位边界、adapter 事件绑定及 socket/HTTP 外连诱饵。`tests/inbox/conftest.py` 只断言 BASE-03 共享 guard 已通过 `-p` 早加载，没有另造 guard。

结果：47 passed；ruff 通过；mypy 对新增合同源文件检查通过。合同源文件 SHA256 为 `f85e050241ad0528eb04baf39dca0f53d9915e115d6e79a2af6dbfd1c0411a9e`；测试文件 SHA256 为 `a679eeca62d3faef95fec57ac68fdd960323b7f3ef9bf49ac04844eab768785a`。集合字段通过 EV canonical_set_fields 显式声明，测试证明不同顺序/重复项与缺省/null 的同义输入产生相同 hash。

未验证：真实来源资格、持久投影/事务/CAS、完整生产者链、真实账户/材料、真实 E2E、生产运行与用户效果。上述属于后续任务或独立真实验证。
