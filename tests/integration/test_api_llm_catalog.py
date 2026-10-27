"""Authenticated provider catalog edits and connection probes, entirely offline."""

import json

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from tests.support.llm_catalog import catalog_values

AUTH = {"X-API-Key": "secret"}


@pytest.fixture
def client(api_client, monkeypatch):
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    api_client.get("/settings", headers=AUTH)
    return api_client


def save(client, value):
    return client.patch("/settings", headers=AUTH, json={"values": {"llm_catalog": json.dumps(value)}})


def editable(public):
    for provider in public["providers"]:
        provider.pop("configured")
        provider.pop("hint")
    return public


def test_catalog_read_update_and_delete_preserve_secrets(client):
    result = save(client, catalog_values())
    assert result.status_code == 200, result.text
    assert "sk-primary-fixture-1234" not in result.text
    assert "sk-deepseek-fixture-5678" not in result.text
    public = result.json()["settings"]["llm_catalog"]
    assert len(public["providers"]) == 2
    assert len(public["providers"][0]["models"]) == 2
    assert public["providers"][0]["configured"] is True
    assert public["providers"][0]["hint"] == "1234"
    assert public["providers"][0]["api_key"] is None
    assert "sk-primary" not in client.get("/settings/history", headers=AUTH).text
    value = editable(public)
    value["providers"][0]["name"] = "Renamed Provider"
    renamed = save(client, value)
    assert renamed.status_code == 200
    assert renamed.json()["settings"]["llm_catalog"]["providers"][0]["configured"] is True
    # Referenced provider/model removals must not silently change the Agent.
    value["providers"].pop(0)
    rejected = save(client, value)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["issues"][0]["key"] == "llm_catalog"
    assert len(client.get("/settings", headers=AUTH).json()["llm_catalog"]["providers"]) == 2
    del value["bindings"]["agent"]
    assert save(client, value).status_code == 200


def test_binding_changes_reach_readiness_with_safe_selection_snapshot(client):
    assert save(client, catalog_values()).status_code == 200
    response = client.get("/investor/readiness", headers=AUTH)
    assert response.status_code == 200, response.text
    assert not any("未配置" in issue for issue in response.json()["interactive_issues"] if "模型" in issue or "端点" in issue or "API Key" in issue)
    assert "sk-primary" not in response.text
    assert "primary.invalid" not in response.text


@respx.mock
def test_probe_uses_specific_provider_and_model_not_research_default(client):
    assert save(client, catalog_values()).status_code == 200

    def respond(request):
        assert request.headers["authorization"] == "Bearer sk-primary-fixture-1234"
        data = json.loads(request.content)
        assert data["model"] == "reason-model" and data["thinking"] == {"type": "enabled"}
        return httpx.Response(200, json={"id": "probe", "object": "chat.completion", "created": 1,
            "model": "reason-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    route = respx.post("https://primary.invalid/v1/chat/completions").mock(side_effect=respond)
    probe = client.post("/settings/test/llm", headers=AUTH, json={"provider_id": "primary", "model_id": "reason"})
    assert probe.status_code == 200 and probe.json()["ok"] and route.called
    assert "sk-primary" not in probe.text
    # Clearing the candidate key never substitutes the saved key or another provider's key.
    probe = client.post("/settings/test/llm", headers=AUTH, json={"provider_id": "primary", "model_id": "reason", "api_key": ""})
    assert not probe.json()["ok"] and route.call_count == 1


@respx.mock
def test_unsaved_provider_probe_never_persists_or_borrows_credentials(client):
    route = respx.post("https://candidate.invalid/v1/chat/completions").respond(401, json={"error": {"message": "sk-candidate-key rejected"}})
    body = {"provider_id": "new", "base_url": "https://candidate.invalid/v1", "model": "candidate", "api_key": "sk-candidate-key"}
    probe = client.post("/settings/test/llm", headers=AUTH, json=body)
    assert probe.status_code == 200 and not probe.json()["ok"] and route.called
    assert "sk-candidate-key" not in probe.text
    assert client.get("/settings", headers=AUTH).json()["llm_catalog"]["providers"] == []
    body.pop("api_key")
    probe = client.post("/settings/test/llm", headers=AUTH, json=body)
    assert not probe.json()["ok"] and route.call_count == 1
    assert client.post("/settings/test/llm", headers=AUTH, json={"provider_id": "missing"}).status_code == 422


def test_catalog_validation_errors_do_not_echo_keys(client):
    value = catalog_values()
    value["providers"][0]["models"][0]["thinking"] = "invalid"
    result = save(client, value)
    assert result.status_code == 422
    assert "sk-primary" not in result.text
    assert "sk-deepseek" not in result.text
    assert client.get("/settings", headers=AUTH).json()["llm_catalog"]["providers"] == []


def test_first_save_of_an_env_fallback_catalog_preserves_its_redacted_keys(client, monkeypatch):
    monkeypatch.setenv("LLM_CATALOG", json.dumps(catalog_values()))
    value = editable(client.get("/settings", headers=AUTH).json()["llm_catalog"])
    assert all(provider["api_key"] is None for provider in value["providers"])
    value["providers"][0]["name"] = "Imported Provider"
    result = save(client, value)
    assert result.status_code == 200
    assert result.json()["settings"]["llm_catalog"]["providers"][0]["configured"] is True


def test_catalog_cookie_updates_require_same_origin(client):
    client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    result = client.patch("/settings", headers={"Origin": "https://other.invalid"},
                          json={"values": {"llm_catalog": json.dumps(catalog_values())}})
    assert result.status_code == 403


def test_stale_editor_cannot_overwrite_provider_changes(client):
    initial = client.get("/settings", headers=AUTH).json()["revision"]
    assert save(client, catalog_values()).status_code == 200
    result = client.patch("/settings", headers=AUTH, json={
        "values": {"llm_catalog": ""}, "expected_revision": initial,
    })
    assert result.status_code == 409
    assert len(client.get("/settings", headers=AUTH).json()["llm_catalog"]["providers"]) == 2


@respx.mock
def test_legacy_probe_does_not_send_selected_providers_key_to_legacy_endpoint(client):
    client.patch("/settings", headers=AUTH, json={"values": {
        "llm_tier3_base_url": "https://legacy.invalid/v1", "llm_tier3_model": "legacy",
        "llm_tier3_api_key": "legacy-fixture-key",
    }})
    assert save(client, catalog_values()).status_code == 200

    def respond(request):
        assert request.headers["authorization"] == "Bearer legacy-fixture-key"
        assert json.loads(request.content)["model"] == "legacy"
        return httpx.Response(401, json={"error": {"message": "inert test endpoint"}})

    route = respx.post("https://legacy.invalid/v1/chat/completions").mock(side_effect=respond)
    probe = client.post("/settings/test/llm", headers=AUTH, json={"use_legacy": True})
    assert probe.status_code == 200 and route.called
