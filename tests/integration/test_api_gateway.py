"""GET /gateway/status: the mobile gateway link as seen from the API."""

KEY = {"X-API-Key": "secret"}


def test_status_requires_auth(api_client) -> None:
    assert api_client.get("/gateway/status").status_code == 401


def test_status_is_disabled_until_configured(api_client) -> None:
    body = api_client.get("/gateway/status", headers=KEY).json()
    assert body["state"] == "disabled"
    assert body["backend_id"] is None


def test_gateway_settings_are_editable_and_the_credential_is_write_only(api_client) -> None:
    groups = {g["key"]: g for g in api_client.get("/settings", headers=KEY).json()["groups"]}
    items = {item["key"]: item for item in groups["gateway"]["items"]}
    assert set(items) == {
        "gateway_enabled",
        "gateway_url",
        "gateway_credential",
        "gateway_namespace",
        "gateway_instance_id",
    }
    assert items["gateway_credential"]["input"]["type"] == "secret"
    assert "value" not in items["gateway_credential"]

    bad = api_client.patch(
        "/settings", json={"values": {"gateway_url": "gateway.example.com"}}, headers=KEY
    )
    assert bad.status_code == 422
    assert [issue["key"] for issue in bad.json()["detail"]["issues"]] == ["gateway_url"]
