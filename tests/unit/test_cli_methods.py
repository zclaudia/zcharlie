"""``ia methods`` against the repo ``config/methods`` directory (offline, M3).

Complements ``test_methods_cli.py`` (card 09, single baseline file in a tmp
directory) with the repo-level scenarios: both shipped YAMLs validate, one
``sync`` imports the baseline and the expectations package as versions 1 and 2
of the same ``company-research`` key without touching any head, and
``activate-global`` can turn the strengthened ``@2`` into the global default
(revision-guarded, replayable, sync-stable). Assertions follow the implemented
rich-text CLI contract, not a JSON one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.investor import (
    MethodArtifact,
    MethodDefaultHead,
    MethodHead,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
METHODS_DIR = REPO_ROOT / "config" / "methods"


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

    return CliRunner().invoke(app, ["methods", *args])


def _packages(url: str) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        engine = init_engine(url)
        async with session_scope() as session:
            rows = (
                await session.scalars(
                    select(MethodArtifact).where(MethodArtifact.kind == "package")
                )
            ).all()
        await engine.dispose()
        return [row.payload for row in rows]

    return asyncio.run(load())


def _versions(url: str) -> list[dict[str, Any]]:
    async def load() -> list[dict[str, Any]]:
        engine = init_engine(url)
        async with session_scope() as session:
            rows = (
                await session.scalars(
                    select(MethodArtifact).where(MethodArtifact.kind == "version")
                )
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


def test_methods_help_lists_commands() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in ("validate", "sync", "list", "show", "status", "activate-global"):
        assert command in result.output


def test_validate_accepts_repo_packages_and_reports_their_shape() -> None:
    for name in ("company-research.v1.yaml", "company-research.v2.expectations.yaml"):
        result = _invoke("validate", str(METHODS_DIR / name))
        assert result.exit_code == 0, result.output
        assert "Valid package company-research" in result.output
        assert "hash=" in result.output


def test_validate_lists_every_error_of_a_broken_package(tmp_path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        "schema_version: 1\npackage_key: broken\napplicable_kinds: []\nsteps:\n"
        "  - id: bad-step\n    title: Bad\n    instruction_kind: no_such_kind\n    blocking: true\n",
        encoding="utf-8",
    )
    result = _invoke("validate", str(broken))
    assert result.exit_code == 1
    assert "instruction_kind" in result.output


def test_sync_imports_both_repo_packages_and_never_moves_the_head(cli_db) -> None:
    assert _head_state(cli_db) == ([], [])
    first = _invoke("sync", "--dir", str(METHODS_DIR))
    assert first.exit_code == 0, first.output
    assert "created" in first.output

    stored = _packages(cli_db)
    assert [payload["package_key"] for payload in stored] == ["company-research"] * 2
    assert sorted(payload["version"] for payload in stored) == [1, 2]
    assert {payload["source"] for payload in stored} == {"repo-sync"}
    assert _head_state(cli_db) == ([], [])  # sync never moves any head

    # Re-syncing identical content dedupes: no new artifacts, heads still untouched.
    again = _invoke("sync", "--dir", str(METHODS_DIR))
    assert again.exit_code == 0, again.output
    assert "unchanged" in again.output
    assert len(_packages(cli_db)) == 2
    assert _head_state(cli_db) == ([], [])


def test_list_and_status_after_sync(cli_db) -> None:
    assert _invoke("sync", "--dir", str(METHODS_DIR)).exit_code == 0

    listed = _invoke("list")
    assert listed.exit_code == 0, listed.output
    assert "company-research" in listed.output
    assert "repo-sync" in listed.output

    shown = _invoke("status")
    assert shown.exit_code == 0, shown.output
    assert "company-research" in shown.output
    assert "not set" in shown.output  # production default: no global head yet
    assert "v1" in shown.output  # ... so new tasks resolve to the legacy baseline


def test_activate_global_moves_head_once_and_rejects_stale_revisions(cli_db) -> None:
    assert _invoke("sync", "--dir", str(METHODS_DIR)).exit_code == 0
    stale = _invoke(
        "activate-global",
        "--package", "company-research@2",
        "--expected-revision", "1",
        "--reason", "Stale attempt",
    )
    assert stale.exit_code != 0
    assert "stale_revision" in stale.output
    assert _head_state(cli_db) == ([], [])

    ok = _invoke(
        "activate-global",
        "--package", "company-research@2",
        "--expected-revision", "0",
        "--reason", "Enable the expectations-gap package for new research",
    )
    assert ok.exit_code == 0, ok.output
    assert "Activated" in ok.output
    assert "company-research@2" in ok.output
    assert "head revision 1" in ok.output
    assert "method version" in ok.output

    status = _invoke("status")
    assert status.exit_code == 0, status.output
    assert "revision 1" in status.output
    assert "company-research@2" in status.output
    defaults, heads = _head_state(cli_db)
    assert len(defaults) == 1 and defaults[0][0] == 1
    assert defaults[0][1]["package_ref"]["revision"] == 2
    assert heads == []  # per-listing heads are never touched

    # The identical command replays its stored receipt and moves nothing.
    versions_before = len(_versions(cli_db))
    replay = _invoke(
        "activate-global",
        "--package", "company-research@2",
        "--expected-revision", "0",
        "--reason", "Enable the expectations-gap package for new research",
    )
    assert replay.exit_code == 0, replay.output
    assert "Replay" in replay.output
    assert len(_versions(cli_db)) == versions_before
    assert _head_state(cli_db) == (defaults, heads)

    # Sync after activation keeps the head exactly where the user left it.
    synced = _invoke("sync", "--dir", str(METHODS_DIR))
    assert synced.exit_code == 0, synced.output
    assert _head_state(cli_db) == (defaults, heads)


def test_activate_global_rejects_malformed_package_selector(cli_db) -> None:
    result = _invoke(
        "activate-global",
        "--package", "company-research",
        "--expected-revision", "0",
        "--reason", "x",
    )
    assert result.exit_code != 0  # bare key is ambiguous: <key@version> required
