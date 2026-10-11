"""AG17 canonical subject selection, session shell, and exact read-only helpers."""
import pytest

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing, Market

KEY = {"X-API-Key": "secret"}


@pytest.mark.asyncio
async def test_workbench_canonical_listing_and_auth(api_client):
    async with session_scope() as session:
        session.add(Market(code="SSE", name="fixture", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="同名 fixture <img src=x onerror=alert(1)>")
        session.add(company)
        await session.flush()
        session.add_all([Listing(id=77, company_id=company.id, market_code="SSE", ticker="600001"),
                         Listing(id=78, company_id=company.id, market_code="SSE", ticker="600002")])
    assert api_client.get("/agent/listings?q=同名").status_code == 401
    matches = api_client.get("/agent/listings?q=同名", headers=KEY).json()
    assert [item["ref"]["id"] for item in matches] == [77, 78]
    exact = api_client.get("/agent/listings?q=600002.SH", headers=KEY).json()
    assert len(exact) == 1 and exact[0]["ref"] == {"kind":"listing", "id":78, "revision":1, "field_path":None}
    assert api_client.get("/agent/listings/78", headers=KEY).json() == exact[0]
    assert api_client.get("/agent/listings/999", headers=KEY).status_code == 404
    assert api_client.get("/agent/listings/0", headers=KEY).status_code == 422
    assert api_client.get("/agent/listings/78").status_code == 401
    assert api_client.get("/agent/listings?q=%25", headers=KEY).json() == []
    assert api_client.get("/agent/listings?q=600002.SZ", headers=KEY).json() == []
    assert api_client.get("/agent/candidate-context/999", headers=KEY).status_code == 404
    assert api_client.get("/app/agent", follow_redirects=False).status_code == 302
    api_client.post("/app/login", data={"api_key":"secret", "next":"/app/agent"})
    page = api_client.get("/app/agent")
    assert page.status_code == 200
    assert "agent-workbench" in page.text and "unsafe-inline" not in page.headers["content-security-policy"]
    assert "<style" not in page.text and "onclick=" not in page.text
