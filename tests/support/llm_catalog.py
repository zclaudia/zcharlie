"""Synthetic provider catalog and slot bindings shared by offline settings tests."""

import json


def catalog_values():
    return {
        "providers": [
            {"id": "primary", "name": "Primary", "kind": "openai",
             "base_url": "https://primary.invalid/v1", "api_key": "sk-primary-fixture-1234",
             "models": [
                 {"id": "fast", "name": "Fast", "model": "fast-model", "enabled": True,
                  "capabilities": ["tools", "json_object"]},
                 {"id": "reason", "name": "Reason", "model": "reason-model", "enabled": True,
                  "thinking": "enabled", "timeout_seconds": 180, "context_length": 128000,
                  "capabilities": ["thinking", "tools", "json_object"]},
             ]},
            {"id": "deepseek", "name": "DeepSeek", "kind": "deepseek",
             "base_url": "https://deepseek.invalid/v1", "api_key": "sk-deepseek-fixture-5678",
             "models": [{"id": "chat", "name": "Chat", "model": "deepseek-chat", "enabled": True,
                         "profile": "deepseek", "thinking": "disabled",
                         "capabilities": ["json_object"]}]},
        ],
    }


def slot_values(**overrides):
    slots = {
        "investor": {"main": "primary/reason", "thinking": "primary/reason"},
        "rule_draft": {"main": "deepseek/chat"},
        "research": {"explain": "deepseek/chat"},
    }
    for consumer, chosen in overrides.items():
        slots.setdefault(consumer, {}).update(chosen)
    return slots


def bound_settings(**model):
    """Settings fields that replace the removed agent_* model keys in fixtures."""

    upstream = model.get("model", "fixture-model")
    provider_kind = model.get("provider", "openai")
    profile = model.get("profile", provider_kind if provider_kind in {"openai", "deepseek"} else "openai")
    capabilities = list(model.get("capabilities", ["tools", "json_object", "thinking"]))
    catalog = {
        "providers": [{
            "id": "fixture",
            "name": "Fixture",
            "kind": provider_kind,
            "base_url": model.get("base_url", "https://fixture.invalid/v1"),
            "api_key": model.get("api_key", "fixture-secret"),
            "models": [{
                "id": "main",
                "name": upstream,
                "model": upstream,
                "enabled": model.get("enabled", True),
                "capabilities": capabilities,
                "context_length": model.get("context_length", 32000),
                "thinking": model.get("thinking", "auto"),
                "timeout_seconds": model.get("timeout_seconds", 120),
                "profile": profile,
                "profile_model": model.get("profile_model", ""),
                "output_mode": model.get("output_mode", "prompted"),
                "supports_json_schema": model.get("supports_json_schema", "json_schema" in capabilities),
                "supports_json_object": model.get("supports_json_object", "json_object" in capabilities or model.get("output_mode", "prompted") == "prompted"),
                "tool_choice_required": model.get("tool_choice_required", False),
                "temperature": model.get("temperature"),
                "max_tokens": model.get("max_tokens", 100),
                "retries": model.get("retries", 1),
            }],
        }],
    }
    ref = "fixture/main"
    return {
        "llm_catalog": json.dumps(catalog),
        "model_slots": json.dumps({
            "investor": {"main": ref, "thinking": ref if "thinking" in capabilities else ""},
            "rule_draft": {"main": ref},
            "research": {"explain": ref},
        }),
    }


_AGENT_FIELDS = {
    "agent_provider": "provider",
    "agent_base_url": "base_url",
    "agent_model": "model",
    "agent_api_key": "api_key",
    "agent_profile": "profile",
    "agent_profile_model": "profile_model",
    "agent_output_mode": "output_mode",
    "agent_supports_json_schema": "supports_json_schema",
    "agent_supports_json_object": "supports_json_object",
    "agent_tool_choice_required": "tool_choice_required",
    "agent_thinking": "thinking",
    "agent_timeout_seconds": "timeout_seconds",
    "agent_temperature": "temperature",
    "agent_max_tokens": "max_tokens",
    "agent_retries": "retries",
}


def settings_from_legacy(**overrides):
    """Build catalog/slot fields from the old agent_* keyword names tests still pass."""

    mapped = {}
    for old, new in _AGENT_FIELDS.items():
        if old in overrides:
            mapped[new] = overrides.pop(old)
    overrides.pop("agent_credential_ref", None)
    tier_url = overrides.pop("llm_tier3_base_url", None)
    tier_key = overrides.pop("llm_tier3_api_key", None)
    tier_model = overrides.pop("llm_tier3_model", None)
    overrides.pop("llm_tier3_thinking", None)
    overrides.pop("llm_rule_draft_thinking", None)
    overrides.pop("llm_request_timeout_seconds", None)
    if "llm_catalog" not in overrides and "model_slots" not in overrides:
        if tier_url:
            mapped.setdefault("base_url", tier_url)
        if tier_key:
            mapped.setdefault("api_key", tier_key)
        if tier_model:
            mapped.setdefault("model", tier_model)
        overrides.update(bound_settings(**mapped))
    return overrides
