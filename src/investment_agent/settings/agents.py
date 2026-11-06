"""Agent types and other model consumers.

Each entry declares its own settings topics and model slots. A slot stores a
``model_ref`` (``provider_id/model_id``) and the capabilities that model must
have. The model catalog does not learn about these consumers.

Add a later agent type by appending a :class:`ConsumerSpec`. The settings page
renders one sidebar entry per ``kind="agent"`` from this registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from investment_agent.domain.llm_catalog import CatalogError, split_model_ref


class SlotError(ValueError):
    """A model-slot document is not valid. The message names no credentials."""


@dataclass(frozen=True)
class ModelSlotSpec:
    id: str
    label: str
    help: str
    required_capabilities: tuple[str, ...] = ()
    required: bool = True


@dataclass(frozen=True)
class TopicBlock:
    title: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class TopicSpec:
    id: str
    title: str
    note: str = ""
    keys: tuple[str, ...] = ()
    blocks: tuple[TopicBlock, ...] = ()
    kind: Literal["fields", "slots"] = "fields"


@dataclass(frozen=True)
class ConsumerSpec:
    id: str
    kind: Literal["agent", "consumer"]
    title: str
    blurb: str
    nav_group: str
    section_id: str
    slots: tuple[ModelSlotSpec, ...]
    topics: tuple[TopicSpec, ...]
    topic_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _blocks(*pairs: tuple[str, tuple[str, ...]]) -> tuple[TopicBlock, ...]:
    return tuple(TopicBlock(title, keys) for title, keys in pairs)


INVESTOR = ConsumerSpec(
    id="investor",
    kind="agent",
    title="投资 Agent",
    blurb="开关、额度和队列。模型按槽位选择；端点和密钥在「模型」里管理。",
    nav_group="Agent",
    section_id="agent",
    slots=(
        ModelSlotSpec(
            "main", "主模型", "对话、工具和研究使用的模型。只列出具备工具能力且已启用的模型。",
            ("tools",), True,
        ),
        ModelSlotSpec(
            "thinking", "思考模型",
            "需要推理能力的步骤。可以不选；选了之后模型缺失、停用或不再具备推理能力时会报错，不会改用其他模型。",
            ("thinking",), False,
        ),
    ),
    topic_groups=(
        ("模型", ("models",)),
        ("运行", ("switches", "quota")),
        ("不常改", ("advanced",)),
    ),
    topics=(
        TopicSpec(
            "models", "模型",
            "每个槽位只保存模型引用。这里不填写接口地址或密钥。",
            keys=("model_slots",), kind="slots",
        ),
        TopicSpec(
            "switches", "开关",
            "后台研究和投资约定仍要各自明确打开；补充事项的本地更新与公告轮询默认关闭。",
            keys=(
                "investor_agent_enabled",
                "investor_agent_background_enabled",
                "disclosure_originals_enabled",
                "portfolio_ledger_enabled",
                "account_fit_enabled",
                "candidate_ranking_enabled",
                "supplemental_rules_enabled",
                "supplemental_updates_enabled",
                "supplemental_disclosure_polling_enabled",
                "portfolio_valuation_max_staleness_days",
            ),
        ),
        TopicSpec(
            "quota", "额度",
            "仍与投资约定取较小值。每批上限不是产出目标。",
            blocks=_blocks(
                ("单任务", (
                    "investor_agent_task_requests", "investor_agent_task_tokens",
                    "investor_agent_task_seconds", "investor_agent_task_tool_calls",
                )),
                ("每日", (
                    "investor_agent_daily_requests", "investor_agent_daily_tokens",
                    "investor_agent_daily_tool_calls",
                )),
                ("每批", (
                    "investor_agent_max_leads_per_batch", "investor_agent_max_new_tasks_per_batch",
                )),
            ),
        ),
        TopicSpec(
            "advanced", "队列与方法影子",
            "队列满时拒绝新任务，不重建已有任务。",
            blocks=_blocks(
                ("队列", (
                    "agent_queue_max_running", "agent_queue_interactive_capacity",
                    "agent_queue_review_capacity", "agent_queue_exploration_capacity",
                )),
                ("方法影子运营", (
                    "method_candidate_shadow_min_positives",
                    "method_candidate_shadow_max_per_proposal",
                )),
            ),
        ),
    ),
)

RULE_DRAFT = ConsumerSpec(
    id="rule_draft",
    kind="consumer",
    title="规则草拟",
    blurb="规则草拟单独选择模型。推理方式用该模型自己的能力标签和调用参数，不再另设开关。",
    nav_group="研究",
    section_id="rule_draft",
    slots=(
        ModelSlotSpec("main", "草拟模型", "规则草拟使用的模型。未选择或模型不可用时，草拟会直接失败。", (), True),
    ),
    topics=(
        TopicSpec(
            "models", "模型",
            "只保存模型引用。研究解释使用另一个槽位。",
            keys=("model_slots",), kind="slots",
        ),
    ),
)

RESEARCH = ConsumerSpec(
    id="research",
    kind="consumer",
    title="研究解释",
    blurb="研究解释单独选择模型，与投资 Agent 和规则草拟互不影响。",
    nav_group="研究",
    section_id="research",
    slots=(
        ModelSlotSpec("explain", "解释模型", "研究解释使用的模型。未选择或模型不可用时，解释会直接失败。", (), True),
    ),
    topics=(
        TopicSpec(
            "models", "模型",
            "只保存模型引用。",
            keys=("model_slots",), kind="slots",
        ),
    ),
)

REVIEW_PANEL = ConsumerSpec(
    id="review_panel",
    kind="agent",
    title="多视角评审",
    blurb="同一时点让多个视角和模型各自分析，再算出一致与分歧。席位在「评审组」里配置，不使用固定模型槽位。",
    nav_group="Agent",
    section_id="review_panel",
    slots=(),
    topic_groups=(
        ("评审组", ("panels",)),
        ("运行", ("switches", "quota")),
    ),
    topics=(
        TopicSpec(
            "panels", "评审组",
            "每个席位是一个视角加一个模型引用。评审组的席位可增删，模型不可用时不会改用其他模型。",
            keys=("review_panels",), kind="fields",
        ),
        TopicSpec(
            "switches", "开关",
            "功能总开关默认关闭；自动触发和定期批量需要分别打开。",
            keys=(
                "review_panel_enabled",
                "review_panel_auto_enabled",
                "review_panel_schedule_enabled",
            ),
        ),
        TopicSpec(
            "quota", "额度",
            "创建轮次前整轮预留，不足时不创建；单席运行超限时保留已有输出并标记为部分完成。",
            blocks=_blocks(
                ("单席", (
                    "review_panel_seat_requests", "review_panel_seat_tool_calls",
                    "review_panel_seat_tokens",
                )),
                ("每日", (
                    "review_panel_daily_rounds", "review_panel_daily_tokens",
                )),
                ("批量与冷却", (
                    "review_panel_batch_max_rounds",
                    "review_panel_auto_cooldown_trading_days",
                    "review_panel_max_running_seats",
                )),
            ),
        ),
    ),
)

CONSUMERS: tuple[ConsumerSpec, ...] = (INVESTOR, RULE_DRAFT, RESEARCH, REVIEW_PANEL)
CONSUMERS_BY_ID: dict[str, ConsumerSpec] = {spec.id: spec for spec in CONSUMERS}
AGENT_TYPES: tuple[ConsumerSpec, ...] = tuple(spec for spec in CONSUMERS if spec.kind == "agent")


def slot_spec(consumer_id: str, slot_id: str) -> ModelSlotSpec:
    consumer = CONSUMERS_BY_ID.get(consumer_id)
    if consumer is None:
        raise SlotError(f"unknown consumer {consumer_id}")
    for slot in consumer.slots:
        if slot.id == slot_id:
            return slot
    raise SlotError(f"unknown slot {consumer_id}.{slot_id}")


def parse_model_slots(value: str) -> dict[str, dict[str, str]]:
    if not value:
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SlotError("模型槽位不是合法 JSON") from exc
    if not isinstance(raw, dict):
        raise SlotError("模型槽位必须是对象")
    parsed: dict[str, dict[str, str]] = {}
    for consumer_id, slots in raw.items():
        if not isinstance(consumer_id, str) or consumer_id not in CONSUMERS_BY_ID:
            raise SlotError("模型槽位包含未知使用者")
        if not isinstance(slots, dict):
            raise SlotError("模型槽位必须是对象")
        known = {slot.id for slot in CONSUMERS_BY_ID[consumer_id].slots}
        parsed[consumer_id] = {}
        for slot_id, ref in slots.items():
            if slot_id not in known:
                raise SlotError("模型槽位包含未知槽位")
            if ref in ("", None):
                parsed[consumer_id][slot_id] = ""
                continue
            if not isinstance(ref, str):
                raise SlotError("模型引用必须是字符串")
            try:
                split_model_ref(ref)
            except CatalogError as exc:
                raise SlotError("模型引用格式应为 provider/model") from exc
            parsed[consumer_id][slot_id] = ref
    return parsed


def validate_model_slots(value: str) -> str:
    parsed = parse_model_slots(value)
    if not parsed:
        return ""
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def consumer_public() -> list[dict[str, Any]]:
    """JSON the settings page uses to render one entry per consumer."""

    published: list[dict[str, Any]] = []
    for spec in CONSUMERS:
        published.append({
            "id": spec.id,
            "kind": spec.kind,
            "title": spec.title,
            "blurb": spec.blurb,
            "nav_group": spec.nav_group,
            "section_id": spec.section_id,
            "slots": [
                {
                    "id": slot.id,
                    "label": slot.label,
                    "help": slot.help,
                    "required_capabilities": list(slot.required_capabilities),
                    "required": slot.required,
                }
                for slot in spec.slots
            ],
            "topic_groups": [
                {"label": label, "topicIds": list(ids)} for label, ids in spec.topic_groups
            ],
            "topics": [
                {
                    "id": topic.id,
                    "title": topic.title,
                    "note": topic.note,
                    "kind": topic.kind,
                    "keys": list(topic.keys or tuple(key for block in topic.blocks for key in block.keys)),
                    "blocks": [{"title": block.title, "keys": list(block.keys)} for block in topic.blocks],
                }
                for topic in spec.topics
            ],
        })
    return published

