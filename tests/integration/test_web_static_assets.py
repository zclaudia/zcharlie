"""Content-versioned static URLs (/app/static/v/<version>/…) and their caching."""

import os
import re

import pytest

from investment_agent.web import static_assets
from investment_agent.web.static_assets import (
    IMMUTABLE_CACHE_CONTROL,
    VersionedStaticFiles,
    static_url,
    static_version,
)


def _login(client):
    return client.post("/app/login", data={"api_key": "secret", "next": "/app"})


def test_current_version_is_immutable(api_client):
    response = api_client.get(static_url("app.js"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    assert response.content == api_client.get("/app/static/app.js").content


def test_relative_module_imports_stay_inside_the_version(api_client):
    # app.js imports "./api.js": the browser resolves it under the same prefix.
    base = static_url("app.js").rsplit("/", 1)[0]
    response = api_client.get(f"{base}/api.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE_CONTROL


def test_stale_version_serves_current_file_without_long_cache(api_client):
    # A page rendered before a deploy must not pin today's file under its old key.
    response = api_client.get("/app/static/v/000000000000/app.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_unversioned_static_urls_keep_revalidating(api_client):
    response = api_client.get("/app/static/app.js")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/app/static/v/",
        f"/app/static/v/{static_version()}",
        f"/app/static/v/{static_version()}/missing.js",
        f"/app/static/v/{static_version()}/../routes.py",
        f"/app/static/v/{static_version()}/%2e%2e/routes.py",
    ],
)
def test_bad_versioned_paths_are_not_found(api_client, path):
    response = api_client.get(path)
    assert response.status_code == 404
    assert response.headers.get("cache-control") != IMMUTABLE_CACHE_CONTROL


def test_rendered_pages_link_only_versioned_assets(api_client):
    _login(api_client)
    for path in ("/app", "/app/candidates", "/app/agent", "/app/settings", "/app/login"):
        html = api_client.get(path).text
        links = re.findall(r'(?:src|href)="(/app/static/[^"]+)"', html)
        assert links, path
        for link in links:
            assert link.startswith(f"/app/static/v/{static_version()}/"), (path, link)


def test_fingerprint_changes_when_a_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(static_assets, "_MEMO_SECONDS", 0.0)
    (tmp_path / "pages").mkdir()
    (tmp_path / "app.js").write_text("export const a = 1;\n")
    (tmp_path / "pages" / "x.css").write_text("body{}\n")
    first = static_version(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert static_version(tmp_path) == first

    (tmp_path / "app.js").write_text("export const a = 22;\n")
    assert static_version(tmp_path) != first

    second = static_version(tmp_path)
    (tmp_path / "new.js").write_text("")
    assert static_version(tmp_path) != second


def test_fingerprint_is_memoised_briefly(tmp_path, monkeypatch):
    monkeypatch.setattr(static_assets, "_MEMO_SECONDS", 60.0)
    (tmp_path / "app.js").write_text("1")
    first = static_version(tmp_path)
    (tmp_path / "app.js").write_text("22")
    os.utime(tmp_path / "app.js")
    # Within the memo window the cached value is returned unchanged.
    assert static_version(tmp_path) == first


async def test_versioned_files_serve_from_given_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(static_assets, "_MEMO_SECONDS", 0.0)
    (tmp_path / "a.js").write_text("export {};\n")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.mount("/v", VersionedStaticFiles(directory=tmp_path))
    client = TestClient(app)
    version = static_version(tmp_path)
    assert client.get(f"/v/{version}/a.js").headers["cache-control"] == IMMUTABLE_CACHE_CONTROL
    (tmp_path / "a.js").write_text("export const changed = true;\n")
    # The old URL now names a stale version: still served, no longer immutable.
    assert client.get(f"/v/{version}/a.js").headers["cache-control"] == "no-cache"
