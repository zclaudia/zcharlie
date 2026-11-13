"""Stock-page dividends read stored data and queue only enabled watchlist stocks."""
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from tests.unit._snapshot_support import seed_watchlist

H = {"X-API-Key": "secret"}
URL = "/stocks/600519.SH/dividends"


def test_auth_and_a_share_validation(api_client):
    assert api_client.get(URL).status_code == 401
    assert api_client.post(URL + "/refresh").status_code == 401
    assert api_client.get("/stocks/AAPL/dividends", headers=H).status_code == 422


async def test_read_stored_context_and_queue(api_client, monkeypatch):
    await seed_watchlist(["600519.SH", "000001.SZ"], disabled=("000001.SZ",))
    monkeypatch.setenv("WMSHARE_DIVIDENDS_ENABLED", "true")
    response = api_client.get(URL, headers=H)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ticker"] == "600519.SH"
    assert data["refresh_enabled"] is True
    assert data["cash"]["value"] is None
    assert data["platform"] is None
    assert api_client.post("/stocks/000001.SZ/dividends/refresh", headers=H).status_code == 403
    response = api_client.post(URL + "/refresh", headers=H)
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["status"] == "queued"
    async with session_scope() as session:
        run = await session.scalar(select(PipelineRun).where(PipelineRun.id == queued["run_id"]))
        assert run.kind == "dividend_snapshot"
        assert run.request_json["provider"] == "wmshare"
        assert run.request_json["scope"] == "watchlist"
        assert run.request_json["tickers"] == ["600519.SH"]
    again = api_client.post(URL + "/refresh", headers=H).json()
    assert again["run_id"] == queued["run_id"] and again["created"] is False


async def test_disabled_refresh(api_client):
    await seed_watchlist(["600519.SH"])
    data = api_client.get(URL, headers=H).json()
    assert data["refresh_enabled"] is False
    assert api_client.post(URL + "/refresh", headers=H).status_code == 409
