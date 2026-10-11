"""The correction service is reachable through the authenticated fact API."""

from uuid import uuid4

from investment_agent.api.routes.portfolio import trading_calendar_predicate
from investment_agent.config import Settings

KEY = {"X-API-Key": "secret"}


def test_correction_endpoint_receipt_and_write_gate(api_client, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    account = api_client.post(
        "/portfolio/accounts",
        headers=KEY,
        json={"idempotency_key": str(uuid4()), "name": "Fixture"},
    ).json()["id"]
    base = {
        "occurred_at": "2025-01-01T00:00:00+08:00",
        "source": "user statement",
        "user_confirmed": True,
    }
    opening = {
        **base,
        "kind": "opening_balance",
        "coverage": "complete",
        "asset_scope": "explicit account",
        "holdings": [],
        "cash": "1000",
        "prior_history": "included_in_confirmed_balance",
    }
    assert (
        api_client.post(
            "/portfolio/events",
            headers=KEY,
            json={
                "account_id": account,
                "expected_revision": 0,
                "idempotency_key": str(uuid4()),
                "fact": opening,
            },
        ).status_code
        == 200
    )
    transfer = {
        **base,
        "occurred_at": "2025-01-02T00:00:00+08:00",
        "kind": "cash_transfer",
        "amount": "200",
    }
    event = api_client.post(
        "/portfolio/events",
        headers=KEY,
        json={
            "account_id": account,
            "expected_revision": 1,
            "idempotency_key": str(uuid4()),
            "fact": transfer,
        },
    ).json()["event"]
    request = {
        "account_id": account,
        "expected_revision": 2,
        "operation_id": str(uuid4()),
        "reason": "wrong amount",
        "reversals": [event["id"]],
        "replacements": [{**transfer, "amount": "100"}],
    }
    assert api_client.post("/portfolio/corrections", json=request).status_code == 401
    result = api_client.post("/portfolio/corrections", headers=KEY, json=request)
    assert result.status_code == 200, result.text
    assert result.json()["snapshot"]["known_cash"] == "1100"
    assert (
        api_client.post("/portfolio/corrections", headers=KEY, json=request).json() == result.json()
    )
    assert (
        api_client.post(
            "/portfolio/corrections", headers=KEY, json={**request, "reason": "different"}
        ).status_code
        == 409
    )
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "false")
    assert api_client.post("/portfolio/corrections", headers=KEY, json=request).status_code == 403


def test_api_valuation_uses_reviewed_calendar():
    from datetime import date

    predicate = trading_calendar_predicate(Settings(api_key="fixture", _env_file=None))
    assert predicate(date(2026, 10, 1)) is False  # A weekday exchange holiday.
    assert predicate(date(2026, 10, 11)) is False
