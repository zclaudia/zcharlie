"""``ia snapshot`` enqueue / status commands (offline)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _runs(url: str) -> list[PipelineRun]:
    async def load() -> list[PipelineRun]:
        engine = init_engine(url)
        async with session_scope() as session:
            rows = list(
                (await session.execute(select(PipelineRun).order_by(PipelineRun.id))).scalars()
            )
        await engine.dispose()
        return rows

    return asyncio.run(load())


def test_snapshot_help_lists_commands():
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["snapshot", "--help"])
    assert result.exit_code == 0
    for command in ("sync-universe", "sync-industry", "collect-market", "collect-financial",
                    "status"):
        assert command in result.output


def test_cli_enqueues_each_kind_idempotently(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    commands = [
        ["snapshot", "sync-universe", "--universe", "CSI300", "--as-of", "2026-10-05"],
        ["snapshot", "sync-industry", "--universe", "CSI300", "--as-of", "2026-10-01"],
        ["snapshot", "collect-market", "--universe", "CSI300", "--date", "2026-09-30"],
        ["snapshot", "collect-financial", "--universe", "CSI300", "--period", "2026-06-30",
         "--as-of", "2026-10-06"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert "Queued" in result.output
    again = runner.invoke(app, commands[2])
    assert again.exit_code == 0 and "Existing" in again.output

    runs = _runs(cli_db)
    assert [(run.kind, run.idempotency_key, run.status) for run in runs] == [
        ("universe_sync", "universe_sync:CSI300:2026-10-05", "queued"),
        ("industry_sync", "industry_sync:CSI300:2026-10-01", "queued"),
        ("market_snapshot", "market_snapshot:CSI300:2026-09-30", "queued"),
        (
            "financial_snapshot",
            "financial_snapshot:CSI300:2026-06-30:2026-10-06",
            "queued",
        ),
    ]
    assert runs[2].request_json == {"universe_code": "CSI300", "trade_date": "2026-09-30"}
    assert all(run.triggered_by == "cli" for run in runs)

    status = runner.invoke(app, ["snapshot", "status"], env={"COLUMNS": "200"})
    assert status.exit_code == 0, status.output
    assert "market_snapshot" in status.output


def test_cli_rejects_bad_period_and_universe(cli_db):
    del cli_db
    from investment_agent.cli.main import app

    runner = CliRunner()
    bad_period = runner.invoke(
        app, ["snapshot", "collect-financial", "--period", "2026-05-31"]
    )
    assert bad_period.exit_code != 0
    bad_universe = runner.invoke(app, ["snapshot", "collect-market", "--universe", "SPX"])
    assert bad_universe.exit_code != 0


def test_worker_cli_accepts_kinds():
    from investment_agent.cli.commands.worker import parse_kinds
    from investment_agent.cli.main import app

    result = CliRunner().invoke(app, ["worker", "start", "--help"])
    assert result.exit_code == 0 and "--kinds" in result.output
    assert parse_kinds(" market_snapshot, ifind_research ,") == [
        "market_snapshot",
        "ifind_research",
    ]
    assert parse_kinds("") is None
