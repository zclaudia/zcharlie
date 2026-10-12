"""Daily valuation jobs (watch-holding-plan 2.10 / 6.2 WP6, WP10).

Reads never compute: the API enqueues the deterministic job and reports
"更新中"; the job freezes (account, ledger version, date) + knowledge cutoff so
a retry reuses the exact input identity, and it is partial (audit only, pointer
untouched) when facts advanced during the computation.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.portfolio.contracts import CreateAccount
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import ValuationService, trading_days_after
from investment_agent.portfolio.valuation_jobs import (
    PortfolioValuationHandler,
    _max_staleness_days,
    enqueue_due_valuations,
    enqueue_valuation,
    required_valuation_date,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import ensure_imported, load_settings, store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.portfolio import PortfolioValuation
from tests.test_portfolio_ledger import append, balance
from tests.test_portfolio_ledger import ledger as ledger_fixture

ledger = ledger_fixture
D = Decimal
KEY = {"X-API-Key": "secret"}


def _settings() -> Settings:
    return Settings(_env_file=None, api_key="ci-inert-api-key", portfolio_ledger_enabled=True)


def _worker() -> ResearchWorker:
    settings = _settings()
    return ResearchWorker(
        settings=settings,
        worker_id="valuation-test",
        handlers=[PortfolioValuationHandler(settings=settings)],
        retry_base_delay_seconds=0.0,
    )


async def _run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        return run


async def _valuation_runs() -> list[PipelineRun]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.kind == "portfolio_valuation")
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_handler_stores_one_valuation_per_ledger_version_and_date(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    async with session_scope() as session:
        run, created = await enqueue_valuation(
            session,
            account_id=account,
            valuation_date=date(2025, 1, 2),
            knowledge_cutoff=datetime(2025, 1, 2, 8, tzinfo=UTC),
            triggered_by="test",
        )
        assert created
        again, created_again = await enqueue_valuation(
            session,
            account_id=account,
            valuation_date=date(2025, 1, 2),
            knowledge_cutoff=datetime(2025, 1, 2, 8, tzinfo=UTC),
            triggered_by="test",
        )
        assert not created_again and again.id == run.id  # retry reuses the identity

    assert await _worker().run_once(run_id=run.id) == run.id
    stored = await _run(run.id)
    assert stored.status == "success", stored.error_summary
    assert stored.metadata_json["valuation_status"] == "stored"
    async with session_scope() as session:
        current = await ValuationService(session).current(account)
    assert current is not None and current.ledger_revision == 1
    assert current.valuation_date == date(2025, 1, 2)
    assert len(await _valuation_runs()) == 1


@pytest.mark.asyncio
async def test_facts_advanced_during_computation_is_partial_and_audit_only(ledger, monkeypatch):
    account, ref = ledger
    await append(account, balance(ref), 0)

    class RacingAccount:
        """Simulates a concurrent fact commit after the snapshot was read."""

        revision = 99

        def __init__(self, row) -> None:  # noqa: ANN001
            self.id = row.id
            self.current_valuation_id = row.current_valuation_id

    original = ValuationService._account

    async def racing(self, account_id):  # noqa: ANN001, ANN202
        return RacingAccount(await original(self, account_id))

    monkeypatch.setattr(ValuationService, "_account", racing)
    async with session_scope() as session:
        run, _ = await enqueue_valuation(
            session,
            account_id=account,
            valuation_date=date(2025, 1, 2),
            knowledge_cutoff=datetime(2025, 1, 2, 8, tzinfo=UTC),
            triggered_by="test",
        )
    assert await _worker().run_once(run_id=run.id) == run.id
    monkeypatch.undo()
    stored = await _run(run.id)
    assert stored.status == "partial"
    assert "ledger_advanced" in (stored.error_summary or "")
    async with session_scope() as session:
        assert await ValuationService(session).current(account) is None
        rows = await session.scalar(select(func.count()).select_from(PortfolioValuation))
    assert rows == 1  # the audit row stays, the current pointer does not move


@pytest.mark.asyncio
async def test_unreviewed_calendar_year_blocks_full_nav(ledger):
    account, ref = ledger
    await append(account, balance(ref), 0)
    day = date(2025, 1, 2)

    def predicate(value: date) -> bool | None:
        return None if value.year == 2025 else value.weekday() < 5

    assert trading_days_after(date(2025, 1, 1), day, predicate) is None
    async with session_scope() as session:
        value = await ValuationService(session).ensure(
            account,
            valuation_date=day,
            now=datetime(2025, 1, 2, 8, tzinfo=UTC),
            is_trading_day=predicate,
        )
    assert value is not None
    assert any(u.startswith("price_staleness_unreviewed:") for u in value.unknowns)
    assert value.total_assets is None and value.weight_available is False


@pytest.mark.asyncio
async def test_same_day_cron_and_ingestion_enqueue_reuse_one_run(ledger):
    """The 00:05 cron and a later snapshot hook share one (account, revision,
    date) identity: the payload's cutoff is the deterministic day-end, so the
    second enqueue reuses the run instead of raising a payload mismatch."""
    account, ref = ledger
    await append(account, balance(ref), 0)
    async with session_scope() as session:
        created = await enqueue_due_valuations(session, triggered_by="scheduler")
    assert len(created) == 1
    async with session_scope() as session:
        again = await enqueue_due_valuations(session, triggered_by="snapshot_close")
    assert again == []  # same key, same payload: reused, never a mismatch
    async with session_scope() as session:
        runs = await _valuation_runs()
    assert len(runs) == 1
    assert runs[0].triggered_by == "scheduler"  # the cron's run is the one kept


@pytest.mark.asyncio
async def test_new_close_price_mints_a_new_identity_and_recomputes(ledger):
    """Once the close price is ingested, the next hook's enqueue creates a new
    run for the same (account, revision, date) via the price watermark (WP6)."""
    account, ref = ledger
    await append(account, balance(ref), 0)
    day = date(2025, 1, 2)
    async with session_scope() as session:
        first, created = await enqueue_valuation(
            session, account_id=account, valuation_date=day, triggered_by="scheduler"
        )
        assert created
    # The day's close lands: new relevant price knowledge for this holding.
    async with session_scope() as session:
        session.add(MarketSnapshot(listing_id=int(ref.id), trade_date=day, open=D(11), high=D(11), low=D(11), close=D(11), volume=D(1), adjust="none", provider="fixture", captured_at=datetime(2025, 1, 2, 7, 15, tzinfo=UTC), version=1))
    async with session_scope() as session:
        second, created = await enqueue_valuation(
            session, account_id=account, valuation_date=day, triggered_by="snapshot_close"
        )
        assert created and second.id != first.id
    assert await _worker().run_once(run_id=second.id) == second.id
    async with session_scope() as session:
        current = await ValuationService(session).current(account)
    assert current is not None and current.valuation_date == day
    assert current.holdings[0].price == D(11)  # the day's close, not the morning price


@pytest.mark.asyncio
async def test_zone_backstop_survives_a_failed_valuation_enqueue(ledger, monkeypatch):
    """The runner's zone enqueue has its own error boundary (P1: no guilty chain)."""
    account, ref = ledger
    await append(account, balance(ref), 0)
    import investment_agent.portfolio.valuation_jobs as valuation_jobs
    import investment_agent.rules.zone_jobs as zone_jobs

    async def broken(*args, **kwargs):
        raise RuntimeError("valuation enqueue unavailable")

    monkeypatch.setattr(valuation_jobs, "enqueue_due_valuations", broken)
    seen: dict[str, object] = {}

    async def zone(session, **kwargs):
        seen["as_of"] = kwargs.get("as_of")
        return []

    monkeypatch.setattr(zone_jobs, "enqueue_due_zone_detections", zone)
    from investment_agent.scheduler.runner import _enqueue_portfolio_valuations_after_ingestion

    await _enqueue_portfolio_valuations_after_ingestion("close_quote", today=date(2025, 1, 2))
    assert seen["as_of"] == date(2025, 1, 2)


@pytest.mark.asyncio
async def test_fanout_isolates_one_account_failure(ledger, monkeypatch):
    """One account's enqueue failure logs and continues; the rest still run."""
    account, ref = ledger
    await append(account, balance(ref), 0)
    async with session_scope() as session:
        broken = await PortfolioService(session).create_account(
            CreateAccount(idempotency_key=uuid4(), name="Broken enqueue")
        )
    import investment_agent.portfolio.valuation_jobs as valuation_jobs

    real = valuation_jobs.enqueue_valuation

    async def flaky(session, *, account_id, **kwargs):
        if str(account_id) == str(broken.id):
            raise RuntimeError("enqueue blew up for this account")
        return await real(session, account_id=account_id, **kwargs)

    monkeypatch.setattr(valuation_jobs, "enqueue_valuation", flaky)
    async with session_scope() as session:
        created = await enqueue_due_valuations(session, triggered_by="test")
    assert len(created) == 1  # the healthy account's run was still created


def test_required_valuation_date_uses_the_calendar():
    def calendar(day: date) -> bool:
        return day.weekday() < 5  # 2025-01-04/05 are a Saturday and a Sunday

    # Saturday 01-04 / Sunday 01-05 fall back to Friday 01-03.
    assert required_valuation_date(date(2025, 1, 4), calendar) == date(2025, 1, 3)
    assert required_valuation_date(date(2025, 1, 5), calendar) == date(2025, 1, 3)
    assert required_valuation_date(date(2025, 1, 3), calendar) == date(2025, 1, 3)
    assert required_valuation_date(date(2026, 6, 1), lambda day: None) is None
    assert required_valuation_date(date(2025, 1, 4), None) is None


@pytest.mark.asyncio
async def test_staleness_threshold_is_runtime_configurable(ledger):
    settings = _settings()
    async with session_scope() as session:
        await ensure_imported(session, settings, Path("config/app.yaml"))
        await store_for(session, settings).apply(
            {"portfolio_valuation_max_staleness_days": 1}, actor="test"
        )
    async with session_scope() as session:
        assert await _max_staleness_days(session, settings) == 1
        selected = await load_settings(session, base=settings)
    assert selected.portfolio_valuation_max_staleness_days == 1


@pytest.mark.asyncio
async def test_read_path_reports_updating_and_enqueues_instead_of_computing(api_client, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_LEDGER_ENABLED", "true")
    account = api_client.post(
        "/portfolio/accounts",
        headers=KEY,
        json={"idempotency_key": str(uuid4()), "name": "Fixture"},
    ).json()["id"]
    fact = {
        "occurred_at": "2025-01-01T00:00:00+08:00",
        "source": "user statement",
        "user_confirmed": True,
        "kind": "opening_balance",
        "coverage": "complete",
        "asset_scope": "explicit account",
        "holdings": [],
        "cash": "1000",
        "prior_history": "included_in_confirmed_balance",
    }
    assert (
        api_client.post(
            "/portfolio/events",
            headers=KEY,
            json={
                "account_id": account,
                "expected_revision": 0,
                "idempotency_key": str(uuid4()),
                "fact": fact,
            },
        ).status_code
        == 200
    )
    response = api_client.get(f"/portfolio/valuations/current?account_id={account}", headers=KEY)
    assert response.status_code == 409, response.text
    assert "更新中" in response.json()["detail"]

    runs = [row for row in await _valuation_runs() if (row.request_json or {}).get("account_id") == account]
    assert len(runs) == 1 and runs[0].status == "queued"
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(PortfolioValuation)) == 0

    # The job then stores it; the read returns the stored immutable result.
    assert await _worker().run_once(run_id=runs[0].id) == runs[0].id
    response = api_client.get(f"/portfolio/valuations/current?account_id={account}", headers=KEY)
    assert response.status_code == 200, response.text
    assert response.json()["ledger_revision"] == 1
    listed = await _valuation_runs()
    assert len(listed) == 1  # a second read does not enqueue a new run
