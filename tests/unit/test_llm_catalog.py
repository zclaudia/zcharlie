"""Catalog validation, encrypted round trips, selection and native HTTP use."""

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel

from investment_agent.agent.model import AgentConfigurationError, AgentModelConfig
from investment_agent.agent.runtime import run_agent
from investment_agent.config import Settings
from investment_agent.domain.llm_catalog import CatalogError, merge_catalog, parse_catalog
from investment_agent.settings.crypto import EncryptionKeyError
from investment_agent.settings.loader import load_agent_model_config, load_settings
from investment_agent.settings.store import SettingsStore, SettingsValidationError
from investment_agent.storage.models.agent import AgentModelRun
from investment_agent.storage.models.app_settings import AppSetting
from tests.support.llm_catalog import catalog_values


def base_settings(**changes):
    return Settings(_env_file=None, api_key="fixture", agent_model="legacy-agent",
                    agent_base_url="https://legacy.invalid/v1", agent_api_key="legacy-secret",
                    llm_tier3_model="legacy-research", llm_tier3_api_key="legacy-research-key", **changes)


def test_legacy_and_multiple_models_resolve_independently():
    base = base_settings()
    assert AgentModelConfig.from_settings(base, 0).values["agent_model"] == "legacy-agent"
    assert base.llm_tier3.model == "legacy-research"
    selected = base_settings(llm_catalog=json.dumps(catalog_values()))
    config = AgentModelConfig.from_settings(selected, 4)
    assert config.values["agent_model"] == "reason-model"
    assert config.values["agent_thinking"] == "enabled"
    assert config.values["agent_timeout_seconds"] == 180
    assert config.credential.get_secret_value() == "sk-primary-fixture-1234"
    assert selected.llm_tier3.model == "deepseek-chat"
    assert selected.llm_tier3.thinking == "disabled"
    assert selected.llm_tier3.api_key == "sk-deepseek-fixture-5678"
    assert config.snapshot()["selection"] == {"provider_id": "primary", "model_id": "reason"}
    assert "sk-primary" not in json.dumps(config.snapshot())
    assert "primary.invalid" not in json.dumps(config.snapshot())


def test_missing_binding_uses_only_that_consumers_legacy_settings():
    value = catalog_values()
    del value["bindings"]["agent"]
    settings = base_settings(llm_catalog=json.dumps(value))
    assert AgentModelConfig.from_settings(settings, 0).values["agent_model"] == "legacy-agent"
    assert settings.llm_tier3.model == "deepseek-chat"


def test_omitted_keys_are_preserved_but_empty_keys_are_cleared():
    original = parse_catalog(json.dumps(catalog_values()))
    public = original.public()
    for provider in public["providers"]:
        provider.pop("configured")
        provider.pop("hint")
    public["providers"][0]["models"][0]["name"] = "Renamed"
    public["providers"][1]["api_key"] = ""
    updated = parse_catalog(merge_catalog(json.dumps(public), original.model_dump_json()))
    assert updated.providers[0].api_key == original.providers[0].api_key
    assert updated.providers[1].api_key == ""
    assert updated.providers[0].models[0].name == "Renamed"
    # Missing selected key never falls back to the legacy credential.
    settings = base_settings(llm_catalog=updated.model_dump_json())
    assert settings.llm_tier3.api_key == ""


@pytest.mark.parametrize("mutate", [
    lambda c: c["providers"].append(c["providers"][0]),
    lambda c: c["providers"][0]["models"].append(c["providers"][0]["models"][0]),
    lambda c: c["providers"].pop(0),
    lambda c: c["providers"][0]["models"].pop(1),
    lambda c: c["providers"][0].update(base_url="https://user:secret@example.com/v1"),
    lambda c: c["providers"][0].update(base_url="https://example.com/v1?api_key=secret"),
    lambda c: c["providers"][0].update(base_url="file:///tmp/model"),
    lambda c: c["providers"][0]["models"][0].update(timeout_seconds=0),
    lambda c: c["providers"][0]["models"][0].update(output_mode="native"),
])
def test_invalid_catalogs_are_rejected_without_echoing_secrets(mutate):
    value = catalog_values()
    mutate(value)
    with pytest.raises(CatalogError) as error:
        parse_catalog(json.dumps(value))
    assert "sk-primary" not in str(error.value)


async def test_catalog_storage_is_encrypted_and_versioned(db_session):
    key = Fernet.generate_key().decode()
    store = SettingsStore(db_session, encryption_key=key)
    await store.claim_import()
    await store.apply({"llm_catalog": json.dumps(catalog_values())}, actor="test")
    row = await db_session.get(AppSetting, "llm_catalog")
    assert row.value_json is None and row.ciphertext and row.secret_hint == ""
    assert "sk-primary" not in row.ciphertext
    described = next(item for item in await store.describe() if item["key"] == "llm_catalog")
    assert described["configured"] and "value" not in described
    changes = await store.history()
    assert all("sk-primary" not in str(change.new_value_json) for change in changes)
    base = base_settings(settings_encryption_key=key)
    first = await load_agent_model_config(db_session, base=base)
    effective = await load_settings(db_session, base=base)
    assert effective.agent_model == "reason-model"
    assert effective.llm_tier3.model == "deepseek-chat"
    value = json.loads((await store.load()).llm_catalog)
    value["bindings"]["agent"] = {"provider_id": "primary", "model_id": "fast"}
    await store.apply({"llm_catalog": json.dumps(value)}, actor="test")
    second = await load_agent_model_config(db_session, base=base)
    assert first.values["agent_model"] == "reason-model"
    assert second.values["agent_model"] == "fast-model"
    assert second.revision == first.revision + 1 and second.version != first.version
    broken_key = base.model_copy(update={"settings_encryption_key": Fernet.generate_key().decode()})
    with pytest.raises(CatalogError, match="cannot be decrypted"):
        await load_settings(db_session, base=broken_key)
    with pytest.raises(SettingsValidationError, match="cannot be decrypted"):
        await SettingsStore(db_session, encryption_key=broken_key.settings_encryption_key).apply(
            {"llm_catalog": json.dumps(value)}, actor="test")


async def test_catalog_write_requires_encryption_and_validation_is_atomic(db_session):
    with pytest.raises(EncryptionKeyError):
        await SettingsStore(db_session).apply({"llm_catalog": json.dumps(catalog_values())}, actor="test")
    assert await db_session.get(AppSetting, "llm_catalog") is None
    value = catalog_values()
    value["providers"] = []
    with pytest.raises(SettingsValidationError):
        await SettingsStore(db_session).apply({"llm_catalog": json.dumps(value), "agent_model": "changed"}, actor="test")
    assert await db_session.get(AppSetting, "agent_model") is None


async def test_worker_reusing_effective_settings_restores_original_fallback_keys(db_session):
    key = Fernet.generate_key().decode()
    store = SettingsStore(db_session, encryption_key=key)
    await store.claim_import()
    value = catalog_values()
    await store.apply({"llm_catalog": json.dumps(value), "agent_model": "legacy-agent",
                       "agent_base_url": "https://legacy.invalid/v1"}, actor="test")
    running = await load_settings(db_session, base=base_settings(settings_encryption_key=key))
    assert running.agent_api_key == "sk-primary-fixture-1234"
    assert running.llm_tier3_api_key == "sk-deepseek-fixture-5678"
    value["bindings"] = {}
    await store.apply({"llm_catalog": json.dumps(value)}, actor="test")
    reloaded = await load_settings(db_session, base=running)
    assert reloaded.agent_model == "legacy-agent"
    assert reloaded.agent_api_key == "legacy-secret"
    assert reloaded.llm_tier3_api_key == "legacy-research-key"


async def test_selected_provider_is_used_by_native_agent_http(db_session):
    class Answer(BaseModel):
        result: int

    settings = base_settings(llm_catalog=json.dumps(catalog_values()))
    config = AgentModelConfig.from_settings(settings, 3)

    def handle(request):
        assert str(request.url) == "https://primary.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-primary-fixture-1234"
        body = json.loads(request.content)
        assert body["model"] == "reason-model"
        assert body["thinking"] == {"type": "enabled"}
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "reason-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"result":9}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}})

    result = await run_agent(config, session=db_session, output_type=Answer, toolsets=[],
                             instructions="Return JSON", prompt="9", transport=httpx.MockTransport(handle))
    assert result.output.result == 9
    row = await db_session.get(AgentModelRun, str(result.call_id))
    assert row.config_snapshot["selection"]["provider_id"] == "primary"
    assert "sk-primary" not in json.dumps(row.config_snapshot)


def test_empty_selected_agent_key_fails_readiness_without_legacy_fallback():
    value = catalog_values()
    value["providers"][0]["api_key"] = ""
    config = AgentModelConfig.from_settings(base_settings(llm_catalog=json.dumps(value)), 0)
    with pytest.raises(AgentConfigurationError, match="unconfigured"):
        config.validate_ready()
