"""A backend release must reach both cookie browsers and unchanged APKs."""

import re

from investment_agent.web import release, static_assets


def test_release_requires_authentication(api_client):
    response = api_client.get("/app/release", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    assert api_client.get("/app/release", headers={"X-API-Key": "wrong"}).status_code == 401


def test_embedded_and_cookie_clients_get_same_uncached_release(api_client):
    embedded = api_client.get("/app/release", headers={"X-API-Key": "secret"})
    assert embedded.status_code == 200
    manifest = embedded.json()
    assert manifest["schema_version"] == 1
    assert manifest["supported_bridge_versions"] == [0, 1]
    assert re.fullmatch(r"[a-f0-9]{24}", manifest["release_id"])
    assert embedded.headers["cache-control"] == "no-store"
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    assert api_client.get("/app/release").json() == manifest
    page = api_client.get("/app")
    assert page.headers["cache-control"] == "no-store"
    assert f'name="zcharlie-release" content="{manifest["release_id"]}"' in page.text


def test_release_changes_for_deploy_rollback_assets_and_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(static_assets, "_MEMO_SECONDS", 0)
    templates = tmp_path / "templates"
    templates.mkdir()
    template = templates / "base.html"
    template.write_text("old")
    monkeypatch.setattr(release, "TEMPLATES_DIR", templates)
    monkeypatch.setenv("APP_VERSION", "deploy-a")
    first = release.web_release()
    monkeypatch.setenv("APP_VERSION", "deploy-b")
    assert release.web_release() != first
    monkeypatch.setenv("APP_VERSION", "deploy-a")
    assert release.web_release() == first
    template.write_text("new template")
    assert release.web_release() != first
    after_template = release.web_release()
    # Replace just the asset revision while keeping the template fingerprint.
    original = release.static_version
    monkeypatch.setattr(release, "static_version", lambda *args: original(*args) if args else "new-assets")
    assert release.web_release() != after_template
