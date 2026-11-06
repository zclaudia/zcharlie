"""ConfigDocStore / loader: parse-as-validation, import, revision linkage, cache."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from structlog.testing import capture_logs

from investment_agent.config_docs import loader, registry
from investment_agent.config_docs.store import (
    ConfigDocConflictError,
    ConfigDocStore,
    ConfigDocValidationError,
)
from investment_agent.factors.field_sources import parse_field_sources
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.models.app_settings import AppSettingMeta
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.config_docs import (
    ConfigDocument,
    ConfigDocumentChange,
)


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


def _use_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: dict[str, Any]
) -> Path:
    """Point the field_sources spec's baseline at a tmp file (drift / import tests)."""

    path = tmp_path / "field_sources.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    specs = tuple(
        replace(spec, baseline=path) if spec.kind == "field_sources" else spec
        for spec in registry.KINDS
    )
    monkeypatch.setattr(registry, "KINDS", specs)
    monkeypatch.setattr(registry, "KINDS_BY_KIND", {spec.kind: spec for spec in specs})
    return path


def _expected_fingerprint(document: dict[str, Any]) -> dict[str, str]:
    """Independent §4.8 oracle: canonical-JSON SHA-256 (short) plus the version."""

    canonical = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    version = document.get("version")
    return {
        "version": version if isinstance(version, str) else "",
        "hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12],
    }


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader cache is process-global: keep it (and its broken flag) clean."""

    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


# --- store: parse-as-validation -------------------------------------------------


async def test_put_rejects_an_invalid_document_and_writes_nothing(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    document = _document()
    document["fields"]["close"]["primary"] = "ifind"  # iFinD only verifies
    with pytest.raises(ConfigDocValidationError, match="iFinD only verifies") as excinfo:
        await store.put("field_sources", document, actor="ui", note="bad")
    assert not isinstance(excinfo.value, ConfigDocConflictError)
    assert (await db_session.execute(select(ConfigDocument))).scalars().all() == []
    assert (await db_session.execute(select(ConfigDocumentChange))).scalars().all() == []
    assert await SettingsStore(db_session).revision() == 0


async def test_put_requires_a_non_empty_note(db_session, tmp_path, monkeypatch) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    for note in ("", "   "):
        with pytest.raises(ConfigDocValidationError, match="note"):
            await store.put("field_sources", _document(), actor="ui", note=note)
    assert (await db_session.execute(select(ConfigDocument))).scalars().all() == []


def test_conflict_and_validation_errors_are_distinguishable() -> None:
    assert not issubclass(ConfigDocConflictError, ConfigDocValidationError)
    assert not issubclass(ConfigDocValidationError, ConfigDocConflictError)


# --- store: get / baseline status -------------------------------------------------


async def test_get_falls_back_to_the_baseline_file_before_import(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    value, row = await store.get("field_sources")
    assert row is None
    assert value == _document()
    assert await store.revision("field_sources") == 0
    status = await store.baseline_status()
    assert status["field_sources"] == {
        "kind": "field_sources",
        "title": "字段来源优先级",
        "imported": False,
        "baseline": "test-1",
        "drifted": False,
    }


# --- store: first import, drift, force re-import ----------------------------------


async def test_ensure_imported_once_then_drift_warning(db_session, tmp_path, monkeypatch) -> None:
    path = _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    with capture_logs() as logs:
        assert await store.ensure_imported() == [spec.kind for spec in registry.KINDS]
    assert not [entry for entry in logs if entry["event"] == "config_docs.baseline_drift"]

    value, row = await store.get("field_sources")
    assert row is not None
    assert row.imported_at is not None and row.updated_by == "import"
    assert value == _document()
    assert await store.revision("field_sources") == 1
    assert await SettingsStore(db_session).revision() == len(registry.KINDS)  # the CD4 bump
    (change,) = await store.history("field_sources")
    assert (change.actor, change.note, change.old_value_json) == ("import", f"baseline {path}", None)

    # Second start: nothing imported, no duplicate history.
    assert await store.ensure_imported() == []
    assert await store.revision("field_sources") == 1
    assert len(await store.history("field_sources")) == 1

    # A DB edit the file does not have: one warning naming the kind; DB wins.
    edited = _document()
    edited["version"] = "test-2"
    await store.put("field_sources", edited, actor="ui", note="diverge")
    with capture_logs() as logs:
        assert await store.ensure_imported() == []
    (drift,) = [entry for entry in logs if entry["event"] == "config_docs.baseline_drift"]
    assert drift["kinds"] == ["field_sources"]
    status = await store.baseline_status()
    assert status["field_sources"]["drifted"] is True
    assert status["field_sources"]["baseline"] == "test-1"


async def test_ensure_imported_skips_a_baseline_that_does_not_parse(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, {"version": "broken", "fields": {}})
    store = ConfigDocStore(db_session)
    with capture_logs() as logs:
        imported = await store.ensure_imported()
    assert "field_sources" not in imported
    assert any(entry["event"] == "config_docs.import_skipped" for entry in logs)
    stored = {
        row.kind
        for row in (await db_session.execute(select(ConfigDocument))).scalars().all()
    }
    assert "field_sources" not in stored


async def test_import_baseline_force_overwrites_and_stamps_imported(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    await store.ensure_imported()
    edited = _document()
    edited["version"] = "test-2"
    await store.put("field_sources", edited, actor="ui", note="diverge")
    assert (await store.baseline_status())["field_sources"]["drifted"] is True

    # Without force the stored document wins (the re-import must be explicit).
    assert await store.import_baseline("field_sources", actor="ui", note="re-import") is False
    value, _ = await store.get("field_sources")
    assert value["version"] == "test-2"

    # With force the baseline replaces it, as a normal audited write.
    assert await store.import_baseline(
        "field_sources", actor="ui", note="re-import", force=True
    ) is True
    value, row = await store.get("field_sources")
    assert value["version"] == "test-1"
    assert row is not None and row.imported_at is not None
    (change, *_) = await store.history("field_sources")
    assert (change.actor, change.note) == ("ui", "re-import")
    assert (change.old_value_json["version"], change.new_value_json["version"]) == (
        "test-2",
        "test-1",
    )
    assert (await store.baseline_status())["field_sources"]["drifted"] is False


async def test_import_baseline_requires_a_note(db_session, tmp_path, monkeypatch) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    with pytest.raises(ConfigDocValidationError, match="note"):
        await store.import_baseline("field_sources", actor="ui", note=" ")


# --- store: put / revision linkage / optimistic lock ------------------------------


async def test_put_bumps_settings_revision_and_records_history(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    settings_store = SettingsStore(db_session)
    document = _document()
    assert await store.put("field_sources", document, actor="ui", note="initial") == 1
    assert await settings_store.revision() == 1
    meta = await db_session.get(AppSettingMeta, 1)
    assert meta is not None and meta.revision == 1

    document["version"] = "test-2"
    assert await store.put("field_sources", document, actor="ui", note="new version") == 2
    assert await settings_store.revision() == 2
    history = await store.history("field_sources")
    assert [
        (row.actor, row.note, (row.old_value_json or {}).get("version"), row.new_value_json["version"])
        for row in history
    ] == [
        ("ui", "new version", "test-1", "test-2"),
        ("ui", "initial", None, "test-1"),
    ]

    # Saving the identical document writes nothing: no history, no bump.
    assert await store.put("field_sources", document, actor="ui", note="no change") == 2
    assert await settings_store.revision() == 2
    assert len(await store.history("field_sources")) == 2


async def test_expected_revision_conflict_writes_nothing(db_session, tmp_path, monkeypatch) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    store = ConfigDocStore(db_session)
    await store.put("field_sources", _document(), actor="ui", note="initial")
    edited = _document()
    edited["version"] = "test-2"
    with pytest.raises(ConfigDocConflictError) as excinfo:
        await store.put(
            "field_sources", edited, actor="ui", note="stale", expected_revision=0
        )
    assert not isinstance(excinfo.value, ConfigDocValidationError)
    value, _ = await store.get("field_sources")
    assert value["version"] == "test-1"
    assert await store.revision("field_sources") == 1
    assert len(await store.history("field_sources")) == 1

    assert (
        await store.put(
            "field_sources", edited, actor="ui", note="fresh", expected_revision=1
        )
        == 2
    )
    value, _ = await store.get("field_sources")
    assert value["version"] == "test-2"


# --- loader: cache prime / put refresh / sync fallback -----------------------------


async def test_loader_primes_and_a_put_refreshes_the_cache(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    assert await loader.ensure_imported(db_session) == [spec.kind for spec in registry.KINDS]
    assert loader.field_sources_registry().version == "test-1"

    edited = _document()
    edited["version"] = "test-2"
    await ConfigDocStore(db_session).put("field_sources", edited, actor="ui", note="bump")
    assert loader.field_sources_registry().version == "test-2"


async def test_sync_getter_falls_back_to_the_baseline_file(tmp_path, monkeypatch) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    loader.invalidate()
    cached = loader.field_sources_registry()
    assert cached.version == "test-1"
    assert cached.fields.keys() == parse_field_sources(_document()).fields.keys()
    # The public factor entry point goes through the same loader.
    from investment_agent.factors.field_sources import default_field_sources

    assert default_field_sources().version == "test-1"


async def test_refresh_if_changed_reloads_external_writes(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    await loader.ensure_imported(db_session)
    edited = _document()
    edited["version"] = "test-2"
    await ConfigDocStore(db_session).put("field_sources", edited, actor="ui", note="bump")
    loader.invalidate()  # as if another process had written the change
    assert await loader.refresh_if_changed(db_session) is True
    assert loader.field_sources_registry().version == "test-2"
    assert await loader.refresh_if_changed(db_session) is False


async def test_refresh_opens_its_own_session_when_none_given(tmp_path, monkeypatch) -> None:
    from investment_agent.storage.database import engine_lifespan, session_scope

    _use_baseline(monkeypatch, tmp_path, _document())
    url = f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"
    async with engine_lifespan(url) as engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            await loader.ensure_imported(session)
        loader.invalidate()
        assert await loader.refresh_if_changed() is True  # own short session
        assert loader.field_sources_registry().version == "test-1"
        assert await loader.refresh_if_changed() is False


async def test_bars_providers_follow_the_saved_document(db_session, tmp_path, monkeypatch) -> None:
    from investment_agent.api.views import bars_provider, bars_providers

    _use_baseline(monkeypatch, tmp_path, _document())
    loader.invalidate()
    assert bars_providers() == ("baostock", "tencent")
    assert bars_provider() == "baostock,tencent"

    edited = _document()
    edited["fields"]["close"]["primary"] = "akshare"
    edited["fields"]["close"]["fallback"] = ["baostock"]
    del edited["fields"]["close"]["calibration"]  # tencent is no longer a source
    await ConfigDocStore(db_session).put("field_sources", edited, actor="ui", note="flip")
    assert bars_providers() == ("akshare", "baostock")


def test_repo_baseline_parses_through_the_registry() -> None:
    """The shipped baseline stays a valid document (the first import must succeed)."""

    spec = registry.spec_for("field_sources")
    assert registry.parse_baseline(spec).version
    document = registry.baseline_document(spec)
    assert isinstance(document["version"], str)


def test_repo_field_sources_header_documents_the_baseline_role() -> None:
    text = registry.spec_for("field_sources").baseline.read_text(encoding="utf-8")
    assert "FIRST-IMPORT BASELINE" in text.splitlines()[0]
    assert "/app/data-sources" in text


# --- loader: document fingerprint (§4.8) -------------------------------------------


async def test_document_fingerprint_is_stable_for_the_same_document(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    assert await loader.ensure_imported(db_session) == [spec.kind for spec in registry.KINDS]

    # Primed from the DB and, after a cold restart, from the baseline file:
    # one document, one fingerprint.
    primed = loader.document_fingerprint("field_sources")
    assert primed == _expected_fingerprint(_document())
    loader.invalidate()
    assert loader.document_fingerprint("field_sources") == primed

    # A document without its own version string fingerprints with version "".
    versionless = {"fields": _document()["fields"]}
    assert loader.fingerprint_document(versionless) == {
        "version": "",
        "hash": _expected_fingerprint(versionless)["hash"],
    }


async def test_document_fingerprint_changes_when_the_document_changes(
    db_session, tmp_path, monkeypatch
) -> None:
    _use_baseline(monkeypatch, tmp_path, _document())
    await loader.ensure_imported(db_session)
    before = loader.document_fingerprint("field_sources")

    # A field's primary flips: the version string and the hashed document move.
    edited = _document()
    edited["version"] = "test-2"
    edited["fields"]["close"]["primary"] = "akshare"
    edited["fields"]["close"]["fallback"] = ["baostock"]
    del edited["fields"]["close"]["calibration"]  # tencent is no longer a source
    await ConfigDocStore(db_session).put("field_sources", edited, actor="ui", note="flip")

    # The worker's propagation path (refresh, not the same-process write): the
    # fingerprint names the newly effective document.
    loader.invalidate()
    assert await loader.refresh_if_changed(db_session) is True
    after = loader.document_fingerprint("field_sources")
    assert after == _expected_fingerprint(edited)
    assert after != before


# --- real baseline: YAML dates must not escape the registry boundary ---------------


def _date_objects(value: Any) -> list[date]:
    """Every ``datetime`` / ``date`` object nested in ``value`` (keys included)."""

    found: list[date] = []

    def walk(node: Any) -> None:
        if isinstance(node, datetime | date):
            found.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                walk(key)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


def test_real_baseline_document_is_json_serializable() -> None:
    """The shipped file's unquoted YAML dates arrive as ISO strings.

    ``yaml.safe_load`` yields ``datetime.date`` objects, which neither the JSON
    columns nor the canonical-JSON drift comparison can serialize — the WP1
    defect that broke the first import and ``baseline_status`` in production.
    ``baseline_document`` normalizes at the boundary, so nothing downstream
    ever sees a date object.
    """

    spec = registry.spec_for("field_sources")
    raw = yaml.safe_load(spec.baseline.read_text(encoding="utf-8"))
    assert _date_objects(raw), "the real baseline must still carry unquoted dates"

    document = registry.baseline_document(spec)
    assert _date_objects(document) == []
    json.dumps(document, sort_keys=True)  # must not raise
    for found in _date_objects(raw):
        assert found.isoformat() in json.dumps(document, default=str)


async def test_store_ensure_imported_against_the_real_baseline(db_session) -> None:
    """The production first-start path against the shipped file, no tmp fixtures."""

    store = ConfigDocStore(db_session)
    assert await store.ensure_imported() == [spec.kind for spec in registry.KINDS]
    value, row = await store.get("field_sources")
    assert row is not None and row.imported_at is not None
    json.dumps(value, sort_keys=True)  # the stored document is JSON-shaped
    registry.spec_for("field_sources").parse(value)  # and parses through its loader

    status = await store.baseline_status()  # this 500'd before the fix
    assert status["field_sources"] == {
        "kind": "field_sources",
        "title": "字段来源优先级",
        "imported": True,
        "baseline": value["version"],
        "drifted": False,
    }

    # The resilient start-up path succeeds too — the defect had it silently
    # swallow the failure, so the import never happened in production.
    loader.invalidate()
    assert await loader.ensure_imported(db_session) == []  # idempotent second start
    assert loader.field_sources_registry().version == value["version"]


async def test_put_normalizes_date_objects_in_incoming_documents(
    db_session, tmp_path, monkeypatch
) -> None:
    """A programmatic caller may hand over date objects; the store must not crash.

    A UI / API PATCH delivers strings (JSON), but ``put`` accepts any dict:
    normalize before parse / storage so the parser sees the ISO strings a JSON
    transport would deliver and the stored row and history serialize.
    """

    _use_baseline(monkeypatch, tmp_path, _document())
    document = _document()
    document["fields"]["close"]["calibration"]["tencent"]["checked_on"] = date(2026, 10, 9)
    store = ConfigDocStore(db_session)
    assert await store.put("field_sources", document, actor="ui", note="date objects") == 1

    value, _ = await store.get("field_sources")
    assert value["fields"]["close"]["calibration"]["tencent"]["checked_on"] == "2026-10-09"
    json.dumps(value, sort_keys=True)
    (change,) = await store.history("field_sources")
    assert (
        change.new_value_json["fields"]["close"]["calibration"]["tencent"]["checked_on"]
        == "2026-10-09"
    )
    # The parsed cache entry carries the parser's own date type again.
    close = loader.field_sources_registry().require("close")
    assert close.calibration["tencent"].checked_on == date(2026, 10, 9)
