"""Resolve a consumer slot to a provider and model. No silent fallback."""

from __future__ import annotations

from collections.abc import Iterable
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


def _resolve_ref(
    settings: Settings,
    model_ref: str,
    *,
    required: Iterable[str],
    label: str,
) -> ResolvedModel:
    """Resolve one reference; unset, missing, disabled or weak models fail."""

    if not model_ref:
        raise ModelUnavailableError(f"{label}：未选择模型")
    try:
        provider, model = parse_catalog(settings.llm_catalog).resolve_ref(model_ref)
    except CatalogError:
        raise ModelUnavailableError(f"{label}：模型不可用") from None
    if not model.enabled:
        raise ModelUnavailableError(f"{label}：模型不可用")
    if not set(required) <= set(model.capabilities):
        raise ModelUnavailableError(f"{label}：模型不可用")
    return ResolvedModel(ref=model_ref, provider=provider, model=model)


def resolve_model_ref(
    settings: Settings,
    model_ref: str,
    *,
    required: Iterable[str],
    label: str,
) -> ResolvedModel:
    """Resolve an explicit ``provider/model`` reference, never substituting another.

    Used by variable-length model consumers such as review seats, which have no
    fixed slot in ``model_slots``. The rules and messages match
    :func:`resolve_slot`: missing -> "未选择模型", otherwise unavailable.
    """

    return _resolve_ref(settings, model_ref, required=required, label=label)


def resolve_slot(settings: Settings, consumer_id: str, slot_id: str) -> ResolvedModel:
    """Return the model bound to a consumer slot. Nothing else is substituted."""

    slot = slot_spec(consumer_id, slot_id)
    ref = parse_model_slots(settings.model_slots).get(consumer_id, {}).get(slot_id, "")
    label = f"{CONSUMERS_BY_ID[consumer_id].title} / {slot.label}"
    return _resolve_ref(
        settings, ref, required=slot.required_capabilities, label=label
    )


def tier_config(
    settings: Settings, consumer_id: str, slot_id: str, *, model: str | None = None,
) -> LLMTierConfig:
    """Chat-completion config for a slot. ``model`` overrides only the upstream name."""

    config = resolve_slot(settings, consumer_id, slot_id).tier()
    if model:
        return config.model_copy(update={"model": model})
    return config
