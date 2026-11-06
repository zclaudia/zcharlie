"""Config-docs API (config-docs-plan §4.5): list, detail with baseline fallback,
validated writes with optimistic locking, history, explicit baseline import."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from investment_agent.config_docs import loader, registry

KEY = {"X-API-Key": "secret"}


def _document() -> dict[str, Any]:
    """A minimal valid field_sources document (``close`` market + ``roe`` financial)."""

    return {
        "version": "test-1",
        "default_tolerance": 0.02,
        "fields": {
            "close": {
                "table": "market",
                "primary": "baostock",
                "fallback": ["tencent"],
                "definition": "收盘价（元）",
                "tolerance": 0.001,
                "key": True,
                "calibration": {
                    "tencent": {"checked_on": "2026-10-09", "method": "qt.gtimg.cn live check"}
                },
            },
            "roe": {
                "table": "financial",
                "primary": "baostock",
                "definition": "净资产收益率（%）",
            },
        },
    }


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader cache is process-global: keep it (and its broken flag) clean."""

    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


@pytest.fixture
def baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Point the field_sources spec's baseline at a tmp file; return its document.

    The repo baseline holds unquoted YAML ``date`` values the JSON column (and
    the store's canonical drift JSON) cannot round-trip — tracked separately in
    the backend package; the API contract is identical with either file.
    """

    document = _document()
    path = tmp_path / "field_sources.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    specs = tuple(
        replace(spec, baseline=path) if spec.kind == "field_sources" else spec
        for spec in registry.KINDS
    )
    monkeypatch.setattr(registry, "KINDS", specs)
    monkeypatch.setattr(registry, "KINDS_BY_KIND", {spec.kind: spec for spec in specs})
    return document


def _patch(client, value: dict[str, Any], **extra: Any) -> Any:
    return client.patch(
        "/config-docs/field_sources",
        json={"value": value, "note": "改动", **extra},
        headers=KEY,
    )


def test_config_docs_require_auth(api_client) -> None:
    body = {"value": _document(), "note": "x"}
    assert api_client.get("/config-docs").status_code == 401
    assert api_client.get("/config-docs/field_sources").status_code == 401
    assert api_client.patch("/config-docs/field_sources", json=body).status_code == 401
    assert api_client.get("/config-docs/field_sources/history").status_code == 401
    assert (
        api_client.post(
            "/config-docs/field_sources/baseline-import", json={"note": "x"}
        ).status_code
        == 401
    )


def test_unknown_kind_is_404(api_client, baseline) -> None:
    assert api_client.get("/config-docs/nope", headers=KEY).status_code == 404
    assert (
        api_client.patch(
            "/config-docs/nope", json={"value": baseline, "note": "x"}, headers=KEY
        ).status_code
        == 404
    )
    assert api_client.get("/config-docs/nope/history", headers=KEY).status_code == 404
    assert (
        api_client.post(
            "/config-docs/nope/baseline-import", json={"note": "x"}, headers=KEY
        ).status_code
        == 404
    )


def test_list_and_detail_fall_back_to_the_baseline_before_import(api_client, baseline) -> None:
    listing = api_client.get("/config-docs", headers=KEY)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["kind"] for item in items] == [spec.kind for spec in registry.KINDS]
    assert [item for item in items if item["kind"] == "field_sources"] == [
        {
            "kind": "field_sources",
            "title": "字段来源优先级",
            "revision": 0,
            "updated_at": None,
            "updated_by": None,
            "imported": False,
            "drifted": False,
        }
    ]

    detail = api_client.get("/config-docs/field_sources", headers=KEY)
    assert detail.status_code == 200
    body = detail.json()
    assert body["kind"] == "field_sources" and body["title"] == "字段来源优先级"
    assert body["value"] == baseline
    assert body["baseline_value"] == baseline
    assert body["revision"] == 0 and body["drifted"] is False
    assert body["updated_at"] is None and body["updated_by"] is None
    assert body["imported_at"] is None
    assert api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"] == []


def test_patch_persists_and_returns_the_refreshed_detail(api_client, baseline) -> None:
    edited = {**baseline, "version": "test-2"}
    response = api_client.patch(
        "/config-docs/field_sources",
        json={"value": edited, "note": "  调整  ", "expected_revision": 0},
        headers=KEY,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["value"] == edited and body["baseline_value"] == baseline
    assert body["revision"] == 1 and body["updated_by"] == "api"
    assert body["drifted"] is True and body["imported_at"] is None
    assert datetime.fromisoformat(body["updated_at"])

    # One round-trip refresh: the plain GET returns exactly what the PATCH did.
    assert api_client.get("/config-docs/field_sources", headers=KEY).json() == body
    listed = api_client.get("/config-docs", headers=KEY).json()["items"]
    assert [item for item in listed if item["kind"] == "field_sources"] == [
        {
            "kind": "field_sources",
            "title": "字段来源优先级",
            "revision": 1,
            "updated_at": body["updated_at"],
            "updated_by": "api",
            "imported": True,
            "drifted": True,
        }
    ]
    # The store refreshed the process cache: the sync consumer sees the write.
    assert loader.field_sources_registry().version == "test-2"


def test_patch_invalid_document_is_422_with_the_parser_message(api_client, baseline) -> None:
    broken = {
        **baseline,
        "fields": {
            **baseline["fields"],
            "close": {**baseline["fields"]["close"], "primary": "ifind"},
        },
    }
    response = _patch(api_client, broken)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["message"] == "config document validation failed"
    [issue] = detail["issues"]
    assert issue["key"] == "field_sources"
    assert "iFinD only verifies" in issue["messages"][0]

    # Nothing was written.
    after = api_client.get("/config-docs/field_sources", headers=KEY).json()
    assert after["value"] == baseline and after["revision"] == 0
    assert api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"] == []


@pytest.mark.parametrize("note", [None, "", "   "])
def test_patch_requires_a_note(api_client, baseline, note: str | None) -> None:
    body: dict[str, Any] = {"value": baseline}
    if note is not None:
        body["note"] = note
    response = api_client.patch("/config-docs/field_sources", json=body, headers=KEY)
    assert response.status_code == 422
    [issue] = response.json()["detail"]["issues"]
    assert issue["key"] == "field_sources" and "note" in issue["messages"][0]
    assert api_client.get("/config-docs/field_sources", headers=KEY).json()["revision"] == 0


def test_patch_stale_expected_revision_is_409(api_client, baseline) -> None:
    assert _patch(api_client, {**baseline, "version": "test-2"}).status_code == 200
    stale = _patch(api_client, {**baseline, "version": "test-3"}, expected_revision=0)
    assert stale.status_code == 409
    assert "刷新" in stale.json()["detail"]
    after = api_client.get("/config-docs/field_sources", headers=KEY).json()
    assert after["value"]["version"] == "test-2" and after["revision"] == 1
    assert (
        len(api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"]) == 1
    )

    fresh = _patch(api_client, {**baseline, "version": "test-3"}, expected_revision=1)
    assert fresh.status_code == 200 and fresh.json()["revision"] == 2


def test_history_lists_changes_with_actor_and_note(api_client, baseline) -> None:
    first = _patch(api_client, {**baseline, "version": "test-2"})
    second = _patch(api_client, {**baseline, "version": "test-3"})
    assert (first.status_code, second.status_code) == (200, 200)

    items = api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"]
    assert [(item["actor"], item["note"]) for item in items] == [("api", "改动")] * 2
    assert items[0]["new_value"]["version"] == "test-3"
    assert items[0]["old_value"]["version"] == "test-2"
    assert items[1]["old_value"] is None and items[1]["new_value"]["version"] == "test-2"
    assert items[0]["id"] > items[1]["id"]
    assert datetime.fromisoformat(items[0]["changed_at"])
    assert (
        api_client.get("/config-docs/field_sources/history?limit=1", headers=KEY).json()["items"][
            0
        ]["id"]
        == items[0]["id"]
    )


@pytest.mark.parametrize("limit", [0, 201])
def test_history_limit_bounds(api_client, baseline, limit: int) -> None:
    assert (
        api_client.get(f"/config-docs/field_sources/history?limit={limit}", headers=KEY).status_code
        == 422
    )


def test_baseline_import_overwrites_drift_and_records_a_change(api_client, baseline) -> None:
    assert _patch(api_client, {**baseline, "version": "test-2"}).status_code == 200
    assert api_client.get("/config-docs/field_sources", headers=KEY).json()["drifted"]

    imported = api_client.post(
        "/config-docs/field_sources/baseline-import",
        json={"note": "回滚到基线"},
        headers=KEY,
    )
    assert imported.status_code == 200
    body = imported.json()
    assert body["value"] == baseline and body["drifted"] is False
    assert body["revision"] == 2 and body["imported_at"] is not None
    assert body["updated_by"] == "api"

    [newest, _write] = api_client.get("/config-docs/field_sources/history", headers=KEY).json()[
        "items"
    ]
    assert (newest["actor"], newest["note"]) == ("api", "回滚到基线")
    assert newest["old_value"]["version"] == "test-2"
    assert newest["new_value"]["version"] == "test-1"


def test_baseline_import_on_a_fresh_database_creates_the_row(api_client, baseline) -> None:
    imported = api_client.post(
        "/config-docs/field_sources/baseline-import",
        json={"note": "首次导入"},
        headers=KEY,
    )
    assert imported.status_code == 200
    body = imported.json()
    assert body["value"] == baseline and body["revision"] == 1
    assert body["imported_at"] is not None and body["drifted"] is False
    [row] = api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"]
    assert (row["actor"], row["note"]) == ("api", "首次导入")
    assert row["old_value"] is None and row["new_value"] == baseline


@pytest.mark.parametrize("note", [None, "", "   "])
def test_baseline_import_requires_a_note(api_client, baseline, note: str | None) -> None:
    body: dict[str, Any] = {} if note is None else {"note": note}
    response = api_client.post("/config-docs/field_sources/baseline-import", json=body, headers=KEY)
    assert response.status_code == 422
    assert api_client.get("/config-docs/field_sources", headers=KEY).json()["revision"] == 0


def test_cookie_write_requires_same_origin(api_client, baseline) -> None:
    api_client.post("/app/login", data={"api_key": "secret", "next": "/app"})
    body = {"value": {**baseline, "version": "test-2"}, "note": "web 改动"}
    cross = api_client.patch(
        "/config-docs/field_sources", json=body, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    ok = api_client.patch(
        "/config-docs/field_sources", json=body, headers={"X-Requested-With": "fetch"}
    )
    assert ok.status_code == 200
    [change] = api_client.get("/config-docs/field_sources/history", headers=KEY).json()["items"]
    assert change["actor"] == "web" and change["note"] == "web 改动"
