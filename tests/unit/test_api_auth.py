from fastapi.testclient import TestClient


def test_healthz_does_not_require_auth(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

    from investment_agent.api.app import create_app
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
