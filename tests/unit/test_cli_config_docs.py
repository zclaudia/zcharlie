"""``ia config-docs`` list / show / import / history (offline, config-docs-plan §4.7)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from investment_agent.config_docs import loader, registry
from investment_agent.storage.database import init_engine
from investment_agent.storage.models.base import Base


def _field_sources() -> dict[str, Any]:
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


def _source_policy_entry() -> dict[str, Any]:
    return {
        "licensing": {"status": "unknown", "company_keys": [], "citation": ""},
        "technical_acceptance": {
            "status": "not_accepted",
            "company_keys": [],
            "adapter_version": "",
            "reports": [],
        },
    }


def _source_policy() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": "unit-policy-1",
        "sources": {
            "eastmoney.hk.native.v1": _source_policy_entry(),
            "sec.companyfacts.v1": _source_policy_entry(),
        },
    }


def _industry_groups() -> dict[str, Any]:
    return {
        "version": "cov-1",
        "system": "csrc",
        "groups": {"other": "兜底", "farming": "农业", "mining": "采掘"},
        "codes": {"A01": "farming", "B06": "mining"},
        "legacy_name_rules": [],
        "shenwan": {"level1": {"110000": "farming", "220000": "mining"}, "overrides": {}},
    }


def _review_lenses() -> dict[str, Any]:
    return {
        "version": "unit-lenses-1",
        "lenses": [
            {
                "id": "value",
                "version": 1,
                "name": "价值视角",
                "focus": "估值与安全边际",
                "prompt": "从价值视角分析这家公司。",
                "required_dimensions": ["valuation", "cash_flow_quality"],
            }
        ],
    }


def _use_baselines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    documents: dict[str, dict[str, Any] | Path] | None = None,
) -> None:
    """Point every kind's baseline at a small tmp file (the ``_use_baseline`` pattern).

    A value may be a document (dumped to ``<kind>.yaml``) or a path — the
    latter lets a test point one kind at a missing or broken file.
    """

    documents = documents or {
        "field_sources": _field_sources(),
        "source_policy": _source_policy(),
        "industry_groups": _industry_groups(),
        "review_lenses": _review_lenses(),
    }
    specs = []
    for spec in registry.KINDS:
        if spec.kind not in documents:
            specs.append(spec)
            continue
        value = documents[spec.kind]
        path = value if isinstance(value, Path) else tmp_path / f"{spec.kind}.yaml"
        if not isinstance(value, Path):
            path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
        specs.append(replace(spec, baseline=path))
    patched = tuple(specs)
    monkeypatch.setattr(registry, "KINDS", patched)
    monkeypatch.setattr(registry, "KINDS_BY_KIND", {spec.kind: spec for spec in patched})


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader cache is process-global: keep it (and its broken flag) clean."""

    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.chdir(tmp_path)  # no stray .env / config/app.yaml
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _invoke(*args: str):
    from investment_agent.cli.main import app

    return CliRunner().invoke(app, ["config-docs", *args])


def _diverge(url: str, version: str = "test-2") -> None:
    """A DB-side edit the baseline file does not have (actor ``ui``)."""

    from investment_agent.config_docs.store import ConfigDocStore
    from investment_agent.storage.database import engine_lifespan, session_scope

    async def main() -> None:
        async with engine_lifespan(url), session_scope() as session:
            document = _field_sources()
            document["version"] = version
            await ConfigDocStore(session).put("field_sources", document, actor="ui", note="diverge")

    asyncio.run(main())


def test_config_docs_help_lists_commands() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in ("list", "show", "import", "history"):
        assert command in result.output


def test_unknown_kind_errors_like_settings(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)
    for args in (("show", "no_such_kind"), ("history", "no_such_kind")):
        result = _invoke(*args)
        assert result.exit_code == 2
        assert "Unknown config document no_such_kind" in result.output
        assert "ia config-docs list" in result.output


def test_list_and_show_before_import_fall_back_to_the_baseline(
    cli_db, tmp_path, monkeypatch
) -> None:
    _use_baselines(monkeypatch, tmp_path)

    listing = _invoke("list")
    assert listing.exit_code == 0, listing.output
    assert "field_sources" in listing.output and "字段来源优先级" in listing.output
    assert "test-1" in listing.output and "cov-1" in listing.output  # baseline refs

    for args in (("show", "field_sources"), ("show", "field_sources", "--baseline")):
        result = _invoke(*args)
        assert result.exit_code == 0, result.output
        assert result.output.startswith(
            "# field_sources revision=0 updated_at=— updated_by=— imported_at=— drifted=no"
        )
        # The ``#`` header is a YAML comment: stdout stays machine-parseable.
        assert yaml.safe_load(result.output) == _field_sources()


def test_import_once_then_show_and_history(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)

    result = _invoke("import")
    assert result.exit_code == 0, result.output
    assert (
        "Imported 4 document(s): "
        "field_sources, source_policy, industry_groups, review_lenses"
    ) in result.output
    assert "Already imported" in _invoke("import").output

    result = _invoke("show", "field_sources")
    assert result.exit_code == 0, result.output
    assert "# field_sources revision=1" in result.output
    assert "updated_by=import" in result.output and "drifted=no" in result.output
    assert yaml.safe_load(result.output) == _field_sources()

    listing = _invoke("list")
    assert listing.exit_code == 0, listing.output
    assert "yes" in listing.output  # the imported column

    history = _invoke("history", "field_sources")
    assert history.exit_code == 0, history.output
    assert "import" in history.output and "baseline" in history.output


def test_force_reimport_overwrites_a_drifted_document(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)
    assert _invoke("import").exit_code == 0
    _diverge(cli_db)

    result = _invoke("show", "field_sources")
    assert "drifted=yes" in result.output
    assert yaml.safe_load(result.output)["version"] == "test-2"
    baseline = _invoke("show", "field_sources", "--baseline")
    assert baseline.exit_code == 0, baseline.output
    assert yaml.safe_load(baseline.output) == _field_sources()

    # The drift banner the UI shows, headless: re-import is explicit and audited.
    result = _invoke("import", "--force", "--note", "ops sync")
    assert result.exit_code == 0, result.output
    assert "Re-imported 1 document(s): field_sources" in result.output
    result = _invoke("show", "field_sources")
    assert "revision=3" in result.output and "drifted=no" in result.output
    assert yaml.safe_load(result.output) == _field_sources()

    history = _invoke("history", "field_sources")
    assert history.exit_code == 0, history.output
    for cell in ("ops sync", "cli", "diverge", "ui", "baseline"):
        assert cell in history.output
    assert history.output.index("ops sync") < history.output.index("diverge")  # newest first
    assert history.output.index("diverge") < history.output.index("baseline")

    # Nothing drifted any more: the second --force writes nothing.
    assert "already matched" in _invoke("import", "--force").output


def test_force_reimport_default_note(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)
    assert _invoke("import").exit_code == 0
    _diverge(cli_db)
    assert _invoke("import", "--force").exit_code == 0
    history = _invoke("history", "field_sources")
    assert "cli baseline import" in history.output
    assert history.output.index("cli baseline import") < history.output.index("diverge")


def test_history_limit_keeps_the_newest_rows(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)
    assert _invoke("import").exit_code == 0
    _diverge(cli_db, "test-2")
    _diverge(cli_db, "test-3")

    history = _invoke("history", "field_sources", "--limit", "1")
    assert history.exit_code == 0, history.output
    assert "baseline" not in history.output  # the oldest row fell out of the window
    assert "diverge" in history.output

    assert len(_invoke("history", "field_sources", "--limit", "3").output) > 0


def test_show_baseline_of_an_unreadable_file_is_an_error(cli_db, tmp_path, monkeypatch) -> None:
    _use_baselines(monkeypatch, tmp_path)
    assert _invoke("import").exit_code == 0
    # After the import a row exists, so only the --baseline read hits the file.
    _use_baselines(
        monkeypatch,
        tmp_path,
        {"field_sources": tmp_path / "missing.yaml"},
    )
    result = _invoke("show", "field_sources", "--baseline")
    assert result.exit_code == 1
    assert "unreadable" in result.output
