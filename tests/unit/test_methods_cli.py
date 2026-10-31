"""``ia methods``: validate / sync / list / show / status / activate-global (CLI).

Covers card 09's acceptance points: offline ``validate`` aggregates every
problem, ``sync`` imports idempotently without ever moving a head (both
``MethodDefaultHead`` and ``MethodHead`` asserted unchanged) and refuses to
store anything when one file is invalid, ``show`` prints the canonical
definition, and ``activate-global`` is revision-guarded, moves only the
global default head and shows up in ``status`` (per-listing overrides listed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.method_packages import canonical_hash, load_package_file
from investment_agent.investor.methods import MethodBinding
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investor import (
    MethodArtifact,
    MethodDefaultHead,
    MethodHead,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.unit.test_method_packages import BASELINE_PACKAGE_FILE


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "methods-cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("COLUMNS", "200")

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _copy_baseline(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "company-research.v1.yaml"
    target.write_text(BASELINE_PACKAGE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def _broken_file(path: Path) -> Path:
    """The baseline with a duplicate kind, a duplicate step id and an unknown dependency."""

    data: dict[str, Any] = yaml.safe_load(BASELINE_PACKAGE_FILE.read_text(encoding="utf-8"))
    data["applicable_kinds"] = ["company_research", "company_research"]
    data["steps"][0]["id"] = data["steps"][2]["id"]
    data["steps"][1]["depends_on"] = ["missing_step"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _artifact_payloads(url: str, kind: str) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        engine = init_engine(url)
        async with session_scope() as session:
            rows = (
                await session.scalars(select(MethodArtifact).where(MethodArtifact.kind == kind))
            ).all()
        await engine.dispose()
        return [row.payload for row in rows]

    return asyncio.run(load())


def _head_state(url: str) -> tuple[list[tuple[int, dict[str, Any]]], list[tuple[int, dict[str, Any]]]]:
    """(MethodDefaultHead rows, MethodHead rows) as (revision, payload) pairs."""

    async def load() -> tuple[list[tuple[int, dict[str, Any]]], list[tuple[int, dict[str, Any]]]]:
        engine = init_engine(url)
        async with session_scope() as session:
            defaults = [
                (row.revision, row.payload)
                for row in (await session.scalars(select(MethodDefaultHead))).all()
            ]
            heads = [
                (row.revision, row.payload)
                for row in (await session.scalars(select(MethodHead))).all()
            ]
        await engine.dispose()
        return defaults, heads

    return asyncio.run(load())


def _seed_listing_override(url: str) -> int:
    """One real listing with a v1 per-listing method head; returns the listing id."""

    async def seed() -> int:
        engine = init_engine(url)
        async with session_scope() as session:
            session.add(Market(code="FX", name="Fixture market", timezone="UTC", currency="CNY"))
            company = Company(canonical_name="Methods CLI fixture")
            session.add(company)
            await session.flush()
            listing = Listing(
                company_id=company.id, market_code="FX", ticker="600001.FX", is_primary=True
            )
            session.add(listing)
            await session.flush()
            session.add(
                MethodHead(
                    owner_id="local_owner",
                    listing_id=listing.id,
                    revision=1,
                    payload=MethodBinding(
                        listing_ref=ObjectRef(kind="listing", id=listing.id, revision=1)
                    ).model_dump(mode="json"),
                )
            )
        await engine.dispose()
        return int(listing.id)

    return asyncio.run(seed())


def _seed_default_head(url: str) -> None:
    async def seed() -> None:
        engine = init_engine(url)
        async with session_scope() as session:
            session.add(
                MethodDefaultHead(
                    owner_id="local_owner",
                    channel="company-research",
                    revision=3,
                    payload={"default_head": "sentinel"},
                )
            )
        await engine.dispose()

    asyncio.run(seed())


def test_methods_help_lists_commands() -> None:
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["methods", "--help"])
    assert result.exit_code == 0
    for command in ("validate", "sync", "list", "show", "status", "activate-global"):
        assert command in result.output


def test_validate_package_file(tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    good = _copy_baseline(tmp_path)
    result = CliRunner().invoke(app, ["methods", "validate", str(good)])
    assert result.exit_code == 0, result.output
    assert "Valid package company-research" in result.output
    assert canonical_hash(load_package_file(good)) in result.output

    # Every problem of the file is reported together, then exit 1.
    broken = _broken_file(tmp_path / "broken.yaml")
    result = CliRunner().invoke(app, ["methods", "validate", str(broken)])
    assert result.exit_code == 1
    assert "3 issue(s)" in result.output
    assert "applicable_kinds: duplicate research kinds are not allowed" in result.output
    assert "duplicate step id 'dimension_assessment'" in result.output
    assert "unknown step id 'missing_step'" in result.output


def test_sync_is_idempotent_and_never_moves_heads(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    _seed_listing_override(cli_db)
    _seed_default_head(cli_db)
    before = _head_state(cli_db)

    directory = tmp_path / "methods"
    _copy_baseline(directory)
    runner = CliRunner()
    first = runner.invoke(app, ["methods", "sync", "--dir", str(directory)])
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    second = runner.invoke(app, ["methods", "sync", "--dir", str(directory)])
    assert second.exit_code == 0, second.output
    assert "unchanged" in second.output

    packages = _artifact_payloads(cli_db, "package")
    assert len(packages) == 1
    assert packages[0]["package_key"] == "company-research"
    assert packages[0]["version"] == 1
    assert packages[0]["source"] == "repo-sync"

    # Both head tables kept their exact row count, revision and payload.
    assert _head_state(cli_db) == before
    assert len(before[0]) == 1 and before[0][0][0] == 3
    assert len(before[1]) == 1 and before[1][0][0] == 1


def test_sync_rejects_bad_files_without_storing(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    directory = tmp_path / "methods"
    _copy_baseline(directory)
    _broken_file(directory / "aaa-broken.yaml")  # sorts first, still fails the whole sync

    result = CliRunner().invoke(app, ["methods", "sync", "--dir", str(directory)])
    assert result.exit_code == 1
    assert "issue(s)" in result.output
    assert "aaa-broken.yaml" in result.output
    assert _artifact_payloads(cli_db, "package") == []
    assert _head_state(cli_db) == ([], [])


def test_list_and_show_stored_packages(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    directory = tmp_path / "methods"
    _copy_baseline(directory)
    runner = CliRunner()
    synced = runner.invoke(app, ["methods", "sync", "--dir", str(directory)])
    assert synced.exit_code == 0, synced.output

    content_hash = _artifact_payloads(cli_db, "package")[0]["content_hash"]
    listed = runner.invoke(app, ["methods", "list"])
    assert listed.exit_code == 0, listed.output
    assert "company-research" in listed.output
    assert "repo-sync" in listed.output
    assert content_hash[:8] in listed.output

    shown = runner.invoke(app, ["methods", "show", "company-research@1"])
    assert shown.exit_code == 0, shown.output
    assert f"# package company-research@1 hash={content_hash[:12]}" in shown.output
    assert "falsifiable_plan" in shown.output
    assert "business_demand" in shown.output  # canonical form: dimensions expanded
    assert "content_hash" not in shown.output  # lifecycle fields are not part of it

    latest = runner.invoke(app, ["methods", "show", "company-research"])
    assert latest.exit_code == 0 and latest.output == shown.output

    assert "Not found" in runner.invoke(app, ["methods", "show", "company-research@9"]).output
    assert runner.invoke(app, ["methods", "show", "company-research@9"]).exit_code == 1
    assert runner.invoke(app, ["methods", "show", "nope"]).exit_code == 1
    assert runner.invoke(app, ["methods", "show", "company-research@x"]).exit_code != 0


def test_activate_global_updates_status_and_keeps_listing_heads(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    directory = tmp_path / "methods"
    _copy_baseline(directory)
    runner = CliRunner()
    synced = runner.invoke(app, ["methods", "sync", "--dir", str(directory)])
    assert synced.exit_code == 0, synced.output

    _seed_listing_override(cli_db)
    overrides_before = _head_state(cli_db)[1]

    # The default head is still unset: a stale expected revision is rejected, non-zero.
    stale = runner.invoke(
        app,
        [
            "methods", "activate-global",
            "--package", "company-research@1",
            "--expected-revision", "1",
            "--reason", "stale expectation",
        ],
    )
    assert stale.exit_code != 0
    assert "stale_revision" in stale.output
    assert _head_state(cli_db)[0] == []

    shown = runner.invoke(app, ["methods", "status"])
    assert shown.exit_code == 0, shown.output
    assert "not set" in shown.output
    assert "600001.FX" in shown.output  # per-listing overrides are listed

    ok = runner.invoke(
        app,
        [
            "methods", "activate-global",
            "--package", "company-research@1",
            "--expected-revision", "0",
            "--reason", "启用已评审基线包",
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert "Activated" in ok.output
    assert "head revision 1" in ok.output
    assert "method version" in ok.output

    shown = runner.invoke(app, ["methods", "status"])
    assert shown.exit_code == 0, shown.output
    assert "revision 1" in shown.output
    assert "company-research@1" in shown.output

    defaults, overrides = _head_state(cli_db)
    assert len(defaults) == 1 and defaults[0][0] == 1
    assert defaults[0][1]["schema_version"] == 2
    assert overrides == overrides_before  # the per-listing head never moved

    # The identical command replays its stored receipt and moves nothing.
    versions_before = len(_artifact_payloads(cli_db, "version"))
    replay = runner.invoke(
        app,
        [
            "methods", "activate-global",
            "--package", "company-research@1",
            "--expected-revision", "0",
            "--reason", "启用已评审基线包",
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert "Replay" in replay.output
    assert len(_artifact_payloads(cli_db, "version")) == versions_before
    assert _head_state(cli_db) == (defaults, overrides)

    # A new explicit activation with the current revision moves the head forward.
    again = runner.invoke(
        app,
        [
            "methods", "activate-global",
            "--package", "company-research@1",
            "--expected-revision", "1",
            "--reason", "重新确认基线包",
        ],
    )
    assert again.exit_code == 0, again.output
    assert "head revision 2" in again.output
    assert _head_state(cli_db)[0][0][0] == 2
    assert _head_state(cli_db)[1] == overrides_before
