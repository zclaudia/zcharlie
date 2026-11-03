"""Pure conversion of legacy model settings into providers, models, and slots."""

import json

from investment_agent.domain.llm_catalog import parse_catalog
from investment_agent.settings.model_migration import plan_model_migration


def test_empty_settings_are_left_alone():
    assert plan_model_migration({}).changed is False


def test_legacy_agent_and_tier3_become_providers_and_slots():
    plan = plan_model_migration({
        "agent_provider": "deepseek",
        "agent_base_url": "https://agent.invalid/v1",
        "agent_model": "agent-model",
        "agent_api_key": "agent-secret-value",
        "agent_thinking": "enabled",
        "agent_profile": "deepseek",
        "llm_tier3_base_url": "https://research.invalid/v1",
        "llm_tier3_model": "explain-model",
        "llm_tier3_api_key": "research-secret-value",
        "llm_tier3_thinking": "disabled",
        "llm_rule_draft_thinking": "enabled",
    })
    assert set(plan.delete) >= {"agent_model", "agent_api_key", "llm_tier3_api_key", "llm_rule_draft_thinking"}
    catalog = parse_catalog(plan.catalog or "")
    assert {provider.base_url for provider in catalog.providers} == {
        "https://agent.invalid/v1", "https://research.invalid/v1",
    }
    agent = next(provider for provider in catalog.providers if provider.base_url.endswith("agent.invalid/v1"))
    assert agent.api_key == "agent-secret-value"
    assert agent.models[0].model == "agent-model"
    assert "tools" in agent.models[0].capabilities and "thinking" in agent.models[0].capabilities
    slots = json.loads(plan.slots or "")
    assert slots["investor"]["main"].startswith(agent.id + "/")
    assert slots["investor"]["thinking"] == slots["investor"]["main"]
    research = next(provider for provider in catalog.providers if "research" in provider.base_url)
    assert research.api_key == "research-secret-value"
    assert slots["research"]["explain"].startswith(research.id + "/")
    assert slots["rule_draft"]["main"] != slots["research"]["explain"]
    draft = research.models[-1]
    assert draft.thinking == "enabled" and "thinking" in draft.capabilities
    again = plan_model_migration({
        "llm_catalog": plan.catalog or "",
        "model_slots": plan.slots or "",
    })
    assert again.changed is False


def test_shared_endpoint_stays_one_provider():
    plan = plan_model_migration({
        "agent_base_url": "https://shared.invalid/v1",
        "agent_model": "agent-model",
        "agent_api_key": "shared-secret",
        "llm_tier3_base_url": "https://shared.invalid/v1",
        "llm_tier3_model": "explain-model",
        "llm_tier3_api_key": "shared-secret",
    })
    catalog = parse_catalog(plan.catalog or "")
    assert len(catalog.providers) == 1
    assert {model.model for model in catalog.providers[0].models} == {"agent-model", "explain-model"}


def test_catalog_bindings_move_out_of_the_catalog():
    raw = {
        "providers": [{
            "id": "primary", "name": "Primary", "kind": "openai",
            "base_url": "https://primary.invalid/v1", "api_key": "sk-primary",
            "models": [{"id": "reason", "name": "Reason", "model": "reason-model", "thinking": "enabled"}],
        }],
        "bindings": {"agent": {"provider_id": "primary", "model_id": "reason"},
                     "research": {"provider_id": "primary", "model_id": "reason"}},
    }
    plan = plan_model_migration({"llm_catalog": json.dumps(raw)})
    catalog = parse_catalog(plan.catalog or "")
    assert not hasattr(catalog, "bindings")
    assert "tools" in catalog.providers[0].models[0].capabilities
    slots = json.loads(plan.slots or "")
    assert slots["investor"]["main"] == "primary/reason"
    assert slots["investor"]["thinking"] == "primary/reason"
    assert slots["research"]["explain"] == "primary/reason"
    assert slots["rule_draft"]["main"] == "primary/reason"
