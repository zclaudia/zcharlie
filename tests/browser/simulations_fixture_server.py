"""Disposable simulation UI server: real FastAPI/SQLite, no external network.

SIM_FIXTURE_DIR=/tmp/new-empty-dir SIM_FIXTURE_PORT=19886 .venv/bin/python tests/browser/simulations_fixture_server.py
The directory must be an existing system-temporary directory with no fixture DB.
"""

import asyncio
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import uvicorn  # noqa: E402
from fastapi import Depends  # noqa: E402
from sqlalchemy import select  # noqa: E402

from investment_agent.api.app import create_app  # noqa: E402
from investment_agent.api.auth import get_settings, verify_api_key_or_session  # noqa: E402
from investment_agent.api.routes import simulation as api  # noqa: E402
from investment_agent.api.schemas.simulation import CreateSimulation  # noqa: E402
from investment_agent.config import Settings  # noqa: E402
from investment_agent.scheduler.trading_calendar import load_trading_calendar  # noqa: E402
from investment_agent.simulation.contracts import CostRecord, Decision, OrderIntent  # noqa: E402
from investment_agent.simulation.data import ExecutionEvidenceInput  # noqa: E402
from investment_agent.simulation.service import SimulationService  # noqa: E402
from investment_agent.storage.database import init_engine, session_scope  # noqa: E402
from investment_agent.storage.models import Base  # noqa: E402
from investment_agent.storage.models.analysis import PipelineRun  # noqa: E402
from investment_agent.storage.models.pit_snapshot import (  # noqa: E402
    FinancialSnapshot,
    MarketSnapshot,
)
from investment_agent.storage.models.simulation import SimulationCostRecord  # noqa: E402
from investment_agent.storage.repositories.listing import ListingRepository  # noqa: E402

KEY = "simulations-synthetic-browser-key"
CATALOG = json.dumps(
    {
        "providers": [
            {
                "id": "offline",
                "name": "离线测试供应商",
                "kind": "openai",
                "base_url": "https://simulation-fixture.invalid/v1",
                "api_key": "synthetic-model-key-never-returned",
                "models": [
                    {"id": "model-a", "name": "合成模型 A", "model": "synthetic-a"},
                    {"id": "model-b", "name": "合成模型 B", "model": "synthetic-b"},
                ],
            }
        ]
    }
)


def block_external_network():
    original = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in {None, "localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("isolated simulation fixture forbids external network")
        return original(host, *args, **kwargs)

    socket.getaddrinfo = local_only
    connect = socket.socket.connect

    def connect_local(self, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise RuntimeError("isolated simulation fixture forbids external network")
        return connect(self, address)

    socket.socket.connect = connect_local


async def seed(session, target, calendar, settings, clock):
    first = await ListingRepository(session).ensure_a_share_listing("600519.SH")
    second = await ListingRepository(session).ensure_a_share_listing("600036.SH")
    from investment_agent.storage.models.reference import Company

    company = await session.get(Company, first.company_id)
    company.canonical_name = "合成样本 <img src=x onerror=alert(1)>"
    company = await session.get(Company, second.company_id)
    company.canonical_name = "合成银行"
    await session.flush()
    svc = SimulationService(session, data_dir=target, calendar=calendar, now=lambda: clock[0])
    values = dict(
        name="合成样本 · 已结算与观察中",
        universe=(first.id, second.id),
        start_date=date(2026, 11, 9),
        end_date=date(2026, 12, 9),
        initial_cash="1000000.000000000000000001",
        rebalance_frequency="daily",
        agents=[
            {"name": "模型 A", "provider_ref": "offline", "model_ref": "model-a"},
            {"name": "模型 B", "provider_ref": "offline", "model_ref": "model-b"},
        ],
    )
    config = api._friendly_config(
        CreateSimulation.model_validate(values), settings, calendar.calendar_id
    )
    demo = await svc.create(config, idempotency_key="fixture-demo")
    waiting = await svc.create(
        config.model_copy(update={"name": "合成样本 · 缺少逐日证据"}),
        idempotency_key="fixture-waiting",
    )
    demo = await svc.start(demo.id, expected_revision=0, idempotency_key="start-demo")
    waiting = await svc.start(waiting.id, expected_revision=0, idempotency_key="start-waiting")

    async def bar(listing_id, day, close="10"):
        session.add(
            MarketSnapshot(
                listing_id=listing_id,
                trade_date=day,
                open=Decimal("10"),
                high=max(Decimal(close), Decimal("10")),
                low=min(Decimal(close), Decimal("10")),
                close=Decimal(close),
                volume=Decimal("100000000"),
                amount=Decimal("1000000000"),
                trade_status=1,
                is_st=False,
                adjust="none",
                provider="offline-fixture",
                captured_at=datetime.combine(day, time(8, 30), UTC),
                version=1,
                raw_ref="fixture:archived-market",
                is_backfill=False,
            )
        )
        await session.flush()

    async def proof(experiment_id, listing_id, day):
        return await svc.ingest_execution_evidence(
            experiment_id,
            ExecutionEvidenceInput(
                listing_id=listing_id,
                trade_date=day,
                exchange="SSE",
                board="mainboard",
                security_type="ordinary_a",
                ipo_date=date(2000, 1, 1),
                ordinary_limit_session=True,
                reference_price="10",
                upper_limit="11",
                lower_limit="9",
                tick_size="0.01",
                lot_size=100,
                is_st=False,
                rule_version=config.rules.version,
                rule_source="fixture:reviewed-rule",
                rule_verified=True,
                corporate_actions_complete=True,
                published_at=datetime.combine(day, time(0), UTC),
                captured_at=datetime.combine(day, time(1), UTC),
            ),
            source_kind="user",
            source_ref="fixture:reviewed-session",
        )

    day = date(2026, 11, 9)
    clock[0] = datetime(2026, 11, 9, 9, tzinfo=UTC)
    for listing in (first, second):
        await bar(listing.id, day)
        session.add(
            FinancialSnapshot(
                listing_id=listing.id,
                report_period=date(2025, 12, 31),
                period_kind="annual",
                available_from=date(2026, 4, 1),
                availability="actual",
                fields={"roe": "0.12"},
                provider="offline-fixture",
                captured_at=datetime(2026, 11, 9, 8, 30, tzinfo=UTC),
                version=1,
                raw_ref="fixture:annual",
                is_backfill=False,
            )
        )
        await proof(demo.id, listing.id, day)
    await proof(waiting.id, first.id, day)
    args = dict(
        observation_date=day,
        cutoff=clock[0],
        decision_deadline=datetime(2026, 11, 10, 0, tzinfo=UTC),
    )
    round_ = await svc.prepare_round(demo.id, **args, idempotency_key="prepare-demo")
    await svc.prepare_round(waiting.id, **args, idempotency_key="prepare-waiting")
    participants = await svc.participants(demo.id)
    for participant in participants:
        if participant.config.kind != "agent":
            continue
        account = await svc.account(demo.id, participant.id)
        orders = (
            (OrderIntent(listing_id=first.id, side="buy", quantity="100", protection_price="10.5"),)
            if participant.config.name == "模型 A"
            else ()
        )
        decision = Decision(
            experiment_id=demo.id,
            participant_id=participant.id,
            account_id=account.account_id,
            id=f"fixture-decision-{participant.id}",
            round_id=round_.id,
            account_revision=account.revision,
            status="submitted",
            action="trade" if orders else "hold",
            orders=orders,
            rationale="合成判断：使用冻结资料观察经营改善；价格变化尚不能证伪经营假设。",
            counter_evidence=("盈利改善仍须后续报告验证。",),
            expected_holding_days=3,
            review_conditions=("下次年报核对现金流。",),
            submitted_at=clock[0],
            evidence_refs=(round_.manifest.evidence[0].id,),
            idempotency_key=f"fixture-decision-{participant.id}",
        )
        await svc.submit_decision(demo.id, round_.id, participant.id, decision)
    clock[0] = datetime(2026, 11, 10, 0, tzinfo=UTC)
    await svc.close_round(demo.id, round_.id)
    clock[0] = datetime(2026, 11, 10, 9, tzinfo=UTC)
    for listing in (first, second):
        await bar(listing.id, date(2026, 11, 10), "10.5")
        await proof(demo.id, listing.id, date(2026, 11, 10))
    await svc.settle_round(demo.id, round_.id)
    await svc.generate_reviews(demo.id)
    for index, participant in enumerate(p for p in participants if p.config.kind == "agent"):
        cost = CostRecord(
            experiment_id=demo.id,
            participant_id=participant.id,
            account_id=participant.account_id,
            id=f"fixture-cost-{index}",
            round_id=round_.id,
            call_key=f"fixture-call-{index}",
            model_ref=participant.config.model_ref,
            returned_model="synthetic-a" if index == 0 else None,
            status="settled" if index == 0 else "unknown",
            estimated=index == 0,
            amount="1",
            currency="USD",
            input_tokens=100 if index == 0 else 0,
            output_tokens=30 if index == 0 else 0,
            occurred_at=datetime(2026, 11, 9, 9, tzinfo=UTC),
        )
        session.add(
            SimulationCostRecord(
                id=cost.id,
                experiment_id=demo.id,
                participant_id=participant.id,
                account_id=participant.account_id,
                round_id=round_.id,
                call_key=cost.call_key,
                status=cost.status,
                payload=cost.model_dump(mode="json"),
            )
        )
    clock[0] = datetime(2026, 11, 11, 9, tzinfo=UTC)
    return {
        "demo_id": demo.id,
        "waiting_id": waiting.id,
        "listing_ids": [first.id, second.id],
        "round_id": round_.id,
    }


async def main():
    target = await asyncio.to_thread(Path(os.environ["SIM_FIXTURE_DIR"]).resolve)
    temp_root = await asyncio.to_thread(Path(tempfile.gettempdir()).resolve)
    if not target.is_relative_to(temp_root) or not target.is_dir():
        raise RuntimeError("fixture requires a pre-created system temporary directory")
    database = target / "simulations.db"
    if database.exists():
        raise RuntimeError("fixture refuses existing database")
    url = f"sqlite+aiosqlite:///{database}"
    os.environ.update(
        API_KEY=KEY,
        DATA_DIR=str(target),
        DATABASE_URL=url,
        LLM_CATALOG=CATALOG,
        MODEL_SLOTS="{}",
        APP_ENV="dev",
    )
    Settings.model_config["env_file"] = None
    settings = Settings(
        _env_file=None,
        api_key=KEY,
        data_dir=target,
        database_url=url,
        llm_catalog=CATALOG,
        model_slots="{}",
    )
    calendar = load_trading_calendar(Path("config/calendars/a_share.yaml"))
    clock = [datetime(2026, 11, 9, 8, tzinfo=UTC)]
    api._now = lambda: clock[0]
    api._calendar = lambda _: calendar
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with session_scope() as session:
            identities = await seed(session, target, calendar, settings, clock)
        app = create_app(settings=settings)
        app.dependency_overrides[get_settings] = lambda: settings

        @app.get("/fixture/state", dependencies=[Depends(verify_api_key_or_session)])
        async def state():
            async with session_scope() as session:
                jobs = (
                    await session.scalars(
                        select(PipelineRun.id).where(PipelineRun.kind.like("simulation%"))
                    )
                ).all()
            return {
                **identities,
                "queued_jobs": len(jobs),
                "external_calls": 0,
                "now": clock[0].isoformat(),
            }

        block_external_network()
        await uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=int(os.environ.get("SIM_FIXTURE_PORT", "19886")),
                lifespan="off",
                log_level="warning",
            )
        ).serve()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
