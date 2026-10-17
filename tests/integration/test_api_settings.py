"""Settings API (settings-plan S4): read model, validated writes, history, connection tests."""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
import pytest_asyncio
import respx
from cryptography.fernet import Fernet
from sqlalchemy import text

from investment_agent.settings.registry import GROUPS, SPECS
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import session_scope

KEY = {"X-API-Key": "secret"}
SECRET = "sk-test-abcdefgh-9876"


def _items(body: dict) -> dict[str, dict]:
    return {item["key"]: item for group in body["groups"] for item in group["items"]}


@pytest_asyncio.fixture
async def client(api_client):
    """The API with the one-time import already done (nothing imported)."""

    async with session_scope() as session:
        assert await SettingsStore(session).claim_import()
    return api_client


@pytest.fixture
def encryption_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    return key


async def test_first_read_runs_the_one_time_import(api_client) -> None:
    body = api_client.get("/settings", headers=KEY).json()
    # config/app.yaml enables the watchlist refresh the registry leaves disabled.
    assert _items(body)["schedule.watchlist.cron"]["value"] == "30 19 * * 1-5"
    assert body["revision"] == 1
    assert {item["actor"] for item in api_client.get("/settings/history", headers=KEY).json()["items"]} == {"import"}
    assert api_client.get("/settings", headers=KEY).json()["revision"] == 1


def test_settings_require_auth(client) -> None:
    assert client.get("/settings").status_code == 401
    assert client.patch("/settings", json={"values": {"candidate_expiry_days": 3}}).status_code == 401
    assert client.get("/settings/history").status_code == 401
    assert client.post("/settings/test/llm", json={}).status_code == 401


def test_cookie_write_requires_same_origin(client) -> None:
    client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    body = {"values": {"candidate_expiry_days": 21}}
    cross = client.patch("/settings", json=body, headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403
    ok = client.patch("/settings", json=body, headers={"X-Requested-With": "fetch"})
    assert ok.status_code == 200
    [change] = client.get("/settings/history", headers=KEY).json()["items"]
    assert change["actor"] == "web"


def test_get_settings_shape(client) -> None:
    response = client.get("/settings", headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert [group["key"] for group in body["groups"]] == list(GROUPS)
    assert [group["title"] for group in body["groups"]] == list(GROUPS.values())
    items = _items(body)
    assert list(items) == [spec.key for spec in SPECS]
    assert body["revision"] == 0
    assert body["encryption_key_configured"] is False

    expiry = items["candidate_expiry_days"]
    assert expiry["value"] == 14 and expiry["default"] == 14 and expiry["is_default"] is True
    assert expiry["apply"] == "live" and expiry["label"] and expiry["help"]
    assert expiry["input"] == {
        "type": "int", "nullable": False, "min": 1, "min_exclusive": False,
        "max": 365, "max_exclusive": False,
    }
    assert items["llm_tier3_thinking"]["input"]["options"] == ["auto", "enabled", "disabled"]
    assert items["schedule.screen.universes"]["input"]["type"] == "multi"
    assert "CSI300" in items["schedule.screen.universes"]["input"]["options"]
    assert items["llm_request_timeout_seconds"]["input"]["min_exclusive"] is True

    # These switches only control operations. They do not imply licensing or
    # technical acceptance, which are independently evaluated per source.
    for key in ("materials_sec_refresh_enabled", "materials_hk_refresh_enabled"):
        assert items[key]["value"] is False and items[key]["default"] is False
        assert "许可" in items[key]["help"] and "技术验收" in items[key]["help"]

    secret = items["llm_tier3_api_key"]
    assert "value" not in secret and "default" not in secret
    assert secret["configured"] is False
    # The test env sets LLM_TIER3_API_KEY (not importable without the master key).
    assert secret["env_fallback"] is True

    cron = items["schedule.screen.cron"]
    assert cron["timezone"] == "Asia/Shanghai"
    fire = datetime.fromisoformat(cron["next_run"])
    assert fire.utcoffset().total_seconds() == 8 * 3600
    assert (fire.weekday(), fire.hour, fire.minute) == (6, 20, 0)  # sun 20:00

    bootstrap = {item["key"]: item for item in body["bootstrap"]}
    assert set(bootstrap) >= {
        "APP_ENV", "LOG_LEVEL", "DATA_DIR", "DATABASE_URL", "API_KEY", "WEB_SESSION_SECRET",
        "WEB_SESSION_TTL_MINUTES", "WEB_COOKIE_SECURE", "WEB_PUBLIC_ORIGIN",
        "SETTINGS_ENCRYPTION_KEY",
    }
    assert bootstrap["API_KEY"]["configured"] is True and "value" not in bootstrap["API_KEY"]
    assert bootstrap["SETTINGS_ENCRYPTION_KEY"]["configured"] is False
    assert all(item.get("value") != "secret" for item in body["bootstrap"])


def test_bootstrap_redacts_database_credentials() -> None:
    from investment_agent.api.routes.settings import redact_database_url

    assert redact_database_url("postgresql+asyncpg://user:pw@db:5432/app") == (
        "postgresql+asyncpg://***@db:5432/app"
    )
    assert redact_database_url("sqlite+aiosqlite:///./data/app.db") == (
        "sqlite+aiosqlite:///./data/app.db"
    )


def test_disabled_cron_has_no_next_run(client) -> None:
    patched = client.patch(
        "/settings", json={"values": {"schedule.screen.cron": None}}, headers=KEY
    )
    assert patched.status_code == 200
    cron = _items(patched.json()["settings"])["schedule.screen.cron"]
    assert cron["value"] is None and "next_run" not in cron


def test_patch_applies_bumps_revision_and_records_history(client) -> None:
    response = client.patch(
        "/settings",
        json={
            "values": {"candidate_expiry_days": 21, "schedule.screen.cron": "0 21 * * mon"},
            "note": "  试用  ",
        },
        headers=KEY,
    )
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["changed"]) == ["candidate_expiry_days", "schedule.screen.cron"]
    assert body["revision"] == 1 == body["settings"]["revision"]
    items = _items(body["settings"])
    assert items["candidate_expiry_days"]["value"] == 21
    assert items["candidate_expiry_days"]["is_default"] is False
    fire = datetime.fromisoformat(items["schedule.screen.cron"]["next_run"])
    assert (fire.weekday(), fire.hour) == (0, 21)

    again = client.patch(
        "/settings", json={"values": {"candidate_expiry_days": 21}}, headers=KEY
    ).json()
    assert again["changed"] == [] and again["revision"] == 1

    history = client.get("/settings/history?limit=10", headers=KEY).json()["items"]
    assert {item["key"] for item in history} == {"candidate_expiry_days", "schedule.screen.cron"}
    expiry = next(item for item in history if item["key"] == "candidate_expiry_days")
    assert (expiry["old_value"], expiry["new_value"]) == (14, 21)
    assert expiry["actor"] == "api" and expiry["note"] == "试用"
    assert expiry["label"] == "候选过期天数"
    assert client.get("/settings/history?limit=1", headers=KEY).json()["items"][0]["id"] == max(
        item["id"] for item in history
    )


@pytest.mark.parametrize("limit", [0, 201])
def test_history_limit_bounds(client, limit: int) -> None:
    assert client.get(f"/settings/history?limit={limit}", headers=KEY).status_code == 422


def test_patch_validation_issues_per_key(client) -> None:
    response = client.patch(
        "/settings",
        json={"values": {"candidate_expiry_days": 0, "schedule.screen.cron": "0 20 * * 9"}},
        headers=KEY,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "settings validation failed"
    keys = {issue["key"] for issue in detail["issues"]}
    assert keys == {"candidate_expiry_days", "schedule.screen.cron"}
    assert all(issue["messages"] for issue in detail["issues"])
    assert client.get("/settings", headers=KEY).json()["revision"] == 0


def test_patch_unknown_key_and_empty_body(client) -> None:
    unknown = client.patch("/settings", json={"values": {"nope": 1}}, headers=KEY)
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["issues"] == [{"key": "nope", "messages": ["unknown setting"]}]
    assert client.patch("/settings", json={"values": {}}, headers=KEY).status_code == 422


def test_secret_without_master_key_is_409(client) -> None:
    response = client.patch(
        "/settings", json={"values": {"ifind_proxy_api_key": SECRET}}, headers=KEY
    )
    assert response.status_code == 409
    assert "SETTINGS_ENCRYPTION_KEY" in response.json()["detail"]
    assert SECRET not in response.text


async def test_secret_is_never_returned(client, encryption_key) -> None:
    response = client.patch(
        "/settings", json={"values": {"llm_tier3_api_key": SECRET}}, headers=KEY
    )
    assert response.status_code == 200
    assert SECRET not in response.text
    item = _items(response.json()["settings"])["llm_tier3_api_key"]
    assert (item["configured"], item["hint"]) == (True, "9876")
    assert "value" not in item and "env_fallback" not in item
    assert response.json()["settings"]["encryption_key_configured"] is True

    for path in ("/settings", "/settings/history"):
        assert SECRET not in client.get(path, headers=KEY).text
    async with session_scope() as session:
        rows = (await session.execute(text("SELECT * FROM app_setting"))).all()
        changes = (await session.execute(text("SELECT * FROM app_setting_change"))).all()
    assert SECRET not in repr(rows) + repr(changes)

    cleared = client.patch("/settings", json={"values": {"llm_tier3_api_key": ""}}, headers=KEY)
    assert _items(cleared.json()["settings"])["llm_tier3_api_key"]["configured"] is False


# ---- connection tests --------------------------------------------------------


@respx.mock
def test_llm_test_uses_candidates_and_never_echoes_the_key(client) -> None:
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "x", "object": "chat.completion", "created": 0, "model": "m-1",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
            },
        )
    )
    response = client.post(
        "/settings/test/llm",
        json={"base_url": "https://llm.example/v1", "model": "m-1", "api_key": SECRET},
        headers=KEY,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and isinstance(body["latency_ms"], int) and "m-1" in body["message"]
    assert SECRET not in response.text
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert client.get("/settings", headers=KEY).json()["revision"] == 0


@respx.mock
def test_llm_test_falls_back_to_the_effective_key_and_reports_errors(client) -> None:
    route = respx.post("https://llm.example/v1/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key x"}})
    )
    response = client.post(
        "/settings/test/llm", json={"base_url": "https://llm.example/v1", "api_key": ""},
        headers=KEY,
    )
    body = response.json()
    assert body["ok"] is False and "401" in body["message"]
    # LLM_TIER3_API_KEY=x from the test env (the effective value).
    assert route.calls.last.request.headers["authorization"] == "Bearer x"


def test_llm_test_without_any_key(client, monkeypatch) -> None:
    monkeypatch.setenv("LLM_TIER3_API_KEY", "")
    body = client.post("/settings/test/llm", json={}, headers=KEY).json()
    assert body["ok"] is False and "API Key" in body["message"]


@respx.mock
def test_ifind_test_calls_health_only(client) -> None:
    route = respx.get("http://proxy.example:7100/health").mock(
        return_value=httpx.Response(200, json={"status": "ok", "version": "1.2"})
    )
    body = client.post(
        "/settings/test/ifind",
        json={"url": "http://proxy.example:7100/", "api_key": SECRET},
        headers=KEY,
    ).json()
    assert body["ok"] is True and "1.2" in body["message"]
    assert route.calls.last.request.headers["x-api-key"] == SECRET
    assert SECRET not in str(body)


@respx.mock
def test_ifind_test_reports_rejected_key_and_unreachable(client) -> None:
    respx.get("http://proxy.example:7100/health").mock(return_value=httpx.Response(401))
    rejected = client.post(
        "/settings/test/ifind",
        json={"url": "http://proxy.example:7100", "api_key": SECRET},
        headers=KEY,
    ).json()
    assert rejected["ok"] is False and "401" in rejected["message"]

    respx.get("http://down.example/health").mock(side_effect=httpx.ConnectError("refused"))
    down = client.post(
        "/settings/test/ifind", json={"url": "http://down.example", "api_key": SECRET},
        headers=KEY,
    ).json()
    assert down["ok"] is False and "无法连接" in down["message"]


def test_ifind_test_without_key(client) -> None:
    body = client.post(
        "/settings/test/ifind", json={"url": "http://proxy.example"}, headers=KEY
    ).json()
    assert body["ok"] is False and "API Key" in body["message"]


def test_agent_settings_api_isolated_encrypted_and_validated(client, encryption_key):
    before = _items(client.get("/settings", headers=KEY).json())
    response = client.patch("/settings", headers=KEY, json={"values": {
        "agent_provider": "openai", "agent_model": "deepseek-v4-pro",
        "agent_base_url": "https://fixture.invalid/v1", "agent_api_key": SECRET,
        "agent_output_mode": "prompted", "agent_tool_choice_required": False,
        "agent_profile": "deepseek", "agent_profile_model": "deepseek-v4-pro",
    }})
    assert response.status_code == 200
    items = _items(response.json()["settings"])
    assert items["agent_model"]["value"] == "deepseek-v4-pro"
    assert items["agent_api_key"]["configured"] and "value" not in items["agent_api_key"]
    assert items["agent_tool_choice_required"]["input"]["type"] == "text"
    assert items["llm_tier3_model"] == before["llm_tier3_model"]
    assert SECRET not in response.text
    assert SECRET not in client.get("/settings/history", headers=KEY).text
    for invalid in [{"agent_provider": "silent-fallback"}, {"agent_retries": -1}, {"agent_credential_ref": "llm_tier3_api_key"}]:
        assert client.patch("/settings", headers=KEY, json={"values": invalid}).status_code == 422
