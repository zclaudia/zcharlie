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
                    "collect-close-quote", "collect-earnings-events", "status"):
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
        ["snapshot", "collect-close-quote", "--universe", "CSI300", "--date", "2026-09-30"],
        ["snapshot", "collect-close-quote", "--universe", "CSI300", "--date", "2026-09-30",
         "--retry"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        assert "Queued" in result.output
    again = runner.invoke(app, commands[2])
    assert again.exit_code == 0 and "Existing" in again.output
    close_quotes = [run for run in _runs(cli_db) if run.kind == "close_quote"]
    assert all(run.request_json["provider"] == "tencent" for run in close_quotes)
    assert [run.request_json["slot"] for run in close_quotes] == ["close", "retry"]

    runs = _runs(cli_db)
    assert [(run.kind, run.idempotency_key, run.status) for run in runs] == [
        ("universe_sync", "universe_sync:CSI300:2026-10-05", "queued"),
        ("industry_sync", "industry_sync:CSI300:2026-10-01", "queued"),
        ("market_snapshot", "market_snapshot:baostock:CSI300:2026-09-30", "queued"),
        (
            "financial_snapshot",
            "financial_snapshot:baostock:CSI300:2026-06-30:2026-10-06",
            "queued",
        ),
        ("close_quote", "close_quote:tencent:CSI300:2026-09-30", "queued"),
        ("close_quote", "close_quote:tencent:CSI300:2026-09-30:retry", "queued"),
    ]
    assert runs[2].request_json == {
        "provider": "baostock",
        "scope": "universe",
        "universe_code": "CSI300",
        "trade_date": "2026-09-30",
    }
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


def test_cli_routes_provider_and_scope(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    ok = [
        ["snapshot", "collect-market", "--provider", "ifind", "--date", "2026-09-30"],
        ["snapshot", "collect-market", "--scope", "watchlist", "--date", "2026-09-30"],
        ["snapshot", "collect-financial", "--provider", "ifind", "--scope", "watchlist",
         "--period", "2026-06-30", "--as-of", "2026-10-06"],
    ]
    for command in ok:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
    rejected = runner.invoke(
        app, ["snapshot", "collect-market", "--provider", "ifind", "--scope", "universe"]
    )
    assert rejected.exit_code != 0
    assert runner.invoke(app, ["snapshot", "collect-market", "--provider", "wind"]).exit_code != 0
    assert runner.invoke(app, ["snapshot", "collect-market", "--scope", "all"]).exit_code != 0

    runs = _runs(cli_db)
    assert [run.idempotency_key for run in runs] == [
        "market_snapshot:ifind:watchlist:2026-09-30",
        "market_snapshot:baostock:watchlist:2026-09-30",
        "financial_snapshot:ifind:watchlist:2026-06-30:2026-10-06",
    ]
    assert all(run.universe_code is None for run in runs)
    assert runs[0].request_json["scope"] == "watchlist"


def test_sync_industry_defaults_to_the_configured_target_pool(cli_db, tmp_path):
    from investment_agent.cli.main import app

    config = tmp_path / "app.yaml"
    config.write_text(
        "scheduler: {}\nsnapshot:\n  universes: [SSE50, CSI300]\n", encoding="utf-8"
    )
    runner = CliRunner()
    default = runner.invoke(
        app, ["snapshot", "sync-industry", "--as-of", "2026-10-01", "--config", str(config)]
    )
    assert default.exit_code == 0, default.output
    whole = runner.invoke(
        app, ["snapshot", "sync-industry", "--as-of", "2026-10-02", "--all-market"]
    )
    assert whole.exit_code == 0, whole.output
    shipped = runner.invoke(app, ["snapshot", "sync-industry", "--as-of", "2026-10-03"])
    assert shipped.exit_code == 0, shipped.output
    both = runner.invoke(
        app, ["snapshot", "sync-industry", "--all-market", "--universe", "CSI300"]
    )
    assert both.exit_code != 0

    runs = _runs(cli_db)
    assert [run.request_json["universe_codes"] for run in runs] == [
        ["CSI300", "SSE50"],
        [],
        ["CSI300"],  # config/app.yaml ships snapshot.universes: [CSI300]
    ]
    assert [run.idempotency_key for run in runs] == [
        "industry_sync:CSI300+SSE50:2026-10-01",
        "industry_sync:ALL:2026-10-02",
        "industry_sync:CSI300:2026-10-03",
    ]
    assert all(run.request_json["systems"] == ["csrc", "sw2021"] for run in runs)


def test_sync_industry_system_option(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    only_sw = runner.invoke(
        app,
        ["snapshot", "sync-industry", "--universe", "CSI300", "--as-of", "2026-10-01",
         "--system", "sw2021"],
    )
    assert only_sw.exit_code == 0, only_sw.output
    both = runner.invoke(
        app,
        ["snapshot", "sync-industry", "--universe", "CSI300", "--as-of", "2026-10-01",
         "--system", "SW2021", "--system", "csrc"],
    )
    assert both.exit_code == 0, both.output
    bad = runner.invoke(
        app, ["snapshot", "sync-industry", "--universe", "CSI300", "--system", "gics"]
    )
    assert bad.exit_code != 0

    runs = _runs(cli_db)
    assert [(run.idempotency_key, run.request_json["systems"]) for run in runs] == [
        ("industry_sync:CSI300:2026-10-01:sw2021", ["sw2021"]),
        ("industry_sync:CSI300:2026-10-01", ["csrc", "sw2021"]),
    ]


def test_backfill_cli_plans_membership_then_reports_status(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    args = ["snapshot", "backfill", "--universe", "csi300", "--as-of", "2026-10-06",
            "--prices", "5y", "--annual", "6", "--quarters", "4"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "membership: queued" in first.output
    again = runner.invoke(app, args)
    assert again.exit_code == 0 and "membership: pending" in again.output
    runs = _runs(cli_db)
    assert len(runs) == 1
    assert runs[0].kind == "snapshot_backfill"
    assert runs[0].idempotency_key == "snapshot_backfill:CSI300:2026-10-06:membership:p5a6q4"
    assert runs[0].request_json["part"] == "membership"

    status = runner.invoke(app, ["snapshot", "backfill", "--universe", "CSI300", "--status"])
    assert status.exit_code == 0, status.output
    assert "2026-10-06" in status.output and "queued 1" in status.output


def test_backfill_cli_rejects_bad_depth_and_future_dates(cli_db):
    from investment_agent.cli.main import app

    runner = CliRunner()
    bad = runner.invoke(app, ["snapshot", "backfill", "--prices", "five"])
    assert bad.exit_code != 0
    future = runner.invoke(app, ["snapshot", "backfill", "--as-of", "2099-01-01"])
    assert future.exit_code != 0
    assert _runs(cli_db) == []


def test_backfill_cli_dry_run_prints_estimates(cli_db, monkeypatch):
    from datetime import UTC, date, datetime

    from investment_agent.cli.main import app
    from investment_agent.providers import baostock_market
    from tests.unit._baostock_history_fake import FakeBaostockHistory

    backend = FakeBaostockHistory(
        constituent_changes=[
            (date(2020, 1, 6), {"600519.SH", "000001.SZ"}),
            (date(2024, 6, 17), {"600519.SH", "601398.SH"}),
        ]
    )
    real = baostock_market.BaostockMarketClient

    def client(*_args, **_kwargs):
        return real(backend, clock=lambda: datetime(2026, 10, 6, 9, 0, tzinfo=UTC))  # type: ignore[arg-type]

    monkeypatch.setattr(baostock_market, "BaostockMarketClient", client)
    result = CliRunner().invoke(
        app, ["snapshot", "backfill", "--as-of", "2026-10-06", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Symbols: 3 (current 2 + removed 1)" in result.output
    assert "prices" in result.output and "financials" in result.output
    assert {method for method, _ in backend.calls} <= {"login", "logout", "query_hs300_stocks"}
    assert _runs(cli_db) == []
