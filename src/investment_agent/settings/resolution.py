"""Resolve a consumer slot to a provider and model. No silent fallback."""

from __future__ import annotations

from dataclasses import dataclass

from investment_agent.config import LLMTierConfig, Settings
from investment_agent.domain.llm_catalog import (
    CatalogError,
    LLMModel,
    LLMProvider,
    ModelRef,
    parse_catalog,
)
from investment_agent.settings.agents import CONSUMERS_BY_ID, parse_model_slots, slot_spec


class ModelUnavailableError(ValueError):
    """The slot is unset, or its model is missing, disabled, or missing a capability."""


@dataclass(frozen=True)
class ResolvedModel:
    ref: str
    provider: LLMProvider
    model: LLMModel

    @property
    def selection(self) -> ModelRef:
        provider_id, _, model_id = self.ref.partition("/")
        return ModelRef(provider_id=provider_id, model_id=model_id)

    def tier(self) -> LLMTierConfig:
        return LLMTierConfig(
            base_url=self.provider.base_url,
            api_key=self.provider.api_key or "",
            model=self.model.model,
            timeout_seconds=self.model.timeout_seconds,
            thinking=self.model.thinking,
        )

    def agent_values(self) -> dict[str, object]:
        model = self.model
        return {
            "agent_provider": self.provider.kind,
            "agent_base_url": self.provider.base_url,
            "agent_model": model.model,
            "agent_profile": model.profile,
            "agent_profile_model": model.profile_model,
            "agent_output_mode": model.output_mode,
            "agent_supports_json_schema": model.supports_json_schema,
            "agent_supports_json_object": model.supports_json_object,
            "agent_tool_choice_required": model.tool_choice_required,
            "agent_thinking": model.thinking,
            "agent_timeout_seconds": model.timeout_seconds,
            "agent_temperature": model.temperature,
            "agent_max_tokens": model.max_tokens,
            "agent_retries": model.retries,
        }


def _fail(consumer_id: str, slot_id: str, reason: str) -> ModelUnavailableError:
    consumer = CONSUMERS_BY_ID[consumer_id]
    slot = slot_spec(consumer_id, slot_id)
    return ModelUnavailableError(f"{consumer.title} / {slot.label}：{reason}")


def resolve_slot(settings: Settings, consumer_id: str, slot_id: str) -> ResolvedModel:
    """Return the model bound to a consumer slot. Nothing else is substituted."""

    slot_spec(consumer_id, slot_id)
    ref = parse_model_slots(settings.model_slots).get(consumer_id, {}).get(slot_id, "")
    if not ref:
        raise _fail(consumer_id, slot_id, "未选择模型")
    try:
        provider, model = parse_catalog(settings.llm_catalog).resolve_ref(ref)
    except CatalogError:
        raise _fail(consumer_id, slot_id, "模型不可用") from None
    if not model.enabled:
        raise _fail(consumer_id, slot_id, "模型不可用")
    required = set(slot_spec(consumer_id, slot_id).required_capabilities)
    if not required <= set(model.capabilities):
        raise _fail(consumer_id, slot_id, "模型不可用")
    return ResolvedModel(ref=ref, provider=provider, model=model)


def tier_config(
    settings: Settings, consumer_id: str, slot_id: str, *, model: str | None = None,
) -> LLMTierConfig:
    """Chat-completion config for a slot. ``model`` overrides only the upstream name."""

    config = resolve_slot(settings, consumer_id, slot_id).tier()
    if model:
        return config.model_copy(update={"model": model})
    return config
