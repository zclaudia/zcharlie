"""API defaults use the Asia/Shanghai business date, not the server's local date."""

from freezegun import freeze_time


def test_single_stock_research_defaults_as_of_to_shanghai_date(
    api_client, monkeypatch
):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    with freeze_time("2026-10-08T20:00:00Z", tick=True):
        response = api_client.post(
            "/research/single-stock",
            headers={"X-API-Key": "secret"},
            json={"ticker": "600406.SH", "industry": "电力设备", "report_year": 2025},
        )
    assert response.status_code == 202
    detail = api_client.get(
        response.json()["result_url"], headers={"X-API-Key": "secret"}
    )
    assert detail.json()["request"]["as_of"] == "2026-10-09"


def test_watchlist_refresh_defaults_as_of_to_shanghai_date(api_client, monkeypatch):
    monkeypatch.setenv("IFIND_PROXY_API_KEY", "ifind-test-key")
    with freeze_time("2026-10-08T20:00:00Z", tick=True):
        response = api_client.post(
            "/watchlist/refresh",
            json={},
            headers={"X-API-Key": "secret"},
        )
    assert response.status_code == 202
    assert response.json()["as_of"] == "2026-10-09"
