"""Route input validation: bounded query limits and watchlist ticker/note checks."""

import pytest

H = {"X-API-Key": "secret"}


@pytest.mark.parametrize("path", ["/admin/llm-calls?limit={}"])
def test_query_limits_are_bounded(api_client, path):
    for bad in (0, -1, 100_000):
        assert api_client.get(path.format(bad), headers=H).status_code == 422, (path, bad)


def test_watchlist_add_validates_ticker_and_note(api_client):
    for bad in ("A$PL", "<script>", "X" * 20):
        r = api_client.post("/watchlist", json={"ticker": bad}, headers=H)
        assert r.status_code == 422, bad
    r = api_client.post("/watchlist", json={"ticker": "AAPL", "note": "n" * 501}, headers=H)
    assert r.status_code == 422
    # Valid shape passes validation (unknown listing -> 404).
    r = api_client.post("/watchlist", json={"ticker": "brk.b", "note": "ok"}, headers=H)
    assert r.status_code == 404
