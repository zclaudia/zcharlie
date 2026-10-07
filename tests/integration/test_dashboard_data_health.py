"""``/dashboard/today`` merges data health warnings (P1-E2)."""

from __future__ import annotations

from freezegun import freeze_time

from tests.integration.test_api_data_health import FROZEN, KEY, _seed_basic


async def test_dashboard_merges_data_health_warnings(api_client) -> None:
    await _seed_basic()
    with freeze_time(FROZEN, tick=True):
        body = api_client.get("/dashboard/today", headers=KEY).json()
    merged = [w for w in body["warnings"] if w["source"] == "data_health"]
    codes = {w["code"] for w in merged}
    assert {"collection_failed", "report_overdue"} <= codes
    assert all(w["level"] in ("warn", "error") for w in merged), "info stays on the health page"
    assert all(w["link"].startswith("/app/health#health-") for w in merged)
    assert body["data_health"]["errors"] >= 1
    assert body["data_health"]["streak_weeks"] == 2
    assert body["data_health"]["target_weeks"] == 8
    assert body["data_health"]["link"] == "/app/health"


def test_dashboard_keeps_calendar_warnings_alongside_data_health(api_client) -> None:
    with freeze_time("2026-11-15T02:00:00Z", tick=True):
        body = api_client.get("/dashboard/today", headers=KEY).json()
    assert [w["code"] for w in body["warnings"]] == ["calendar"]
    assert body["warnings"][0]["source"] == "calendar"
    assert body["data_health"]["errors"] == 0


async def test_dashboard_survives_a_failing_health_check(api_client, monkeypatch) -> None:
    from investment_agent.api.routes import data_health

    async def boom(*args, **kwargs):
        raise RuntimeError("broken aggregate")

    monkeypatch.setattr(data_health, "compute_data_health", boom)
    body = api_client.get("/dashboard/today", headers=KEY).json()
    assert any(w["code"] == "data_health_unavailable" for w in body["warnings"])
    assert body["data_health"] is None
