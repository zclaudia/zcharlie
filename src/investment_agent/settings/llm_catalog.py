"""Adapt shared catalog bindings to effective runtime settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from investment_agent.domain.llm_catalog import catalog_settings_overrides, parse_catalog

if TYPE_CHECKING:
    from investment_agent.config import Settings


def resolve_catalog_settings(settings: Settings) -> Settings:
    update = catalog_settings_overrides(parse_catalog(settings.llm_catalog))
    if not update:
        return settings
    selected = settings.model_copy(update=update)
    selected._llm_legacy_credentials = settings._llm_legacy_credentials or {
        key: getattr(settings, key) for key in ("agent_api_key", "llm_tier3_api_key")
    }
    return selected
