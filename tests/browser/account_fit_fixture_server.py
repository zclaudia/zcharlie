"""Synthetic HTTP + file-SQLite account-fit fixture for the Playwright probe."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends
from sqlalchemy import select

sys.path.insert(0, str(Path.cwd()))
from tests.support.account_fit_browser_universe import seed_ranking_universe
from tests.support.account_fit_db import make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs

from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.deps import session_service
from investment_agent.api.routes import investment_assessments
from investment_agent.candidates.ranking_jobs import CandidateRankingHandler
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import PortfolioConstraints
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.contracts import Account
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.portfolio.suitability_jobs import AccountFitHandler
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import Base  # noqa: F401
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioSnapshot,
    PortfolioValuation,
)

FIXTURE_KEY = "account-fit-synthetic-test-key-2026"
FIRST_ACCOUNT = UUID("00000000-0000-0000-0000-000000000101")
SECOND_ACCOUNT = UUID("00000000-0000-0000-0000-000000000102")
FIT_LISTING = 600000
HOLD_LISTING = 600001


class SyntheticRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule:
        return make_trusted_rule(listing_ref)


def make_second_account(first):
    snapshot_id = uuid4()
    valuation_id = uuid4()
    account = Account(
        id=SECOND_ACCOUNT,
        name="Synthetic large account",
        currency="CNY",
        revision=1,
        current_snapshot_id=snapshot_id,
    )
    snapshot = first.snapshot.model_copy(
        update={
            "id": snapshot_id,
            "account_id": SECOND_ACCOUNT,
            "reference": ObjectRef(kind="portfolio_snapshot", id=snapshot_id, revision=1),
            "known_cash": Decimal("2000000"),
            "cash_subtotal": Decimal("2000000"),
            "holdings": tuple(
                holding
                for holding in make_inputs(
                    target_listing=FIT_LISTING,
                    extra_listings=(HOLD_LISTING,),
                    cash="2000000",
                    holdings=((HOLD_LISTING, "100", "1000"),),
                    prices={FIT_LISTING: "1500", HOLD_LISTING: "1000"},
                    constraints=PortfolioConstraints(
                        account_id=FIRST_ACCOUNT,
                        max_single_weight=Decimal("0.1"),
                    ),
                ).snapshot.holdings
            ),
            "known_subtotal": Decimal("2100000"),
            "total_assets": Decimal("2100000"),
        }
    )
    # Keep the existing immutable valuation tied to this account's own snapshot.
    source = make_inputs(
        target_listing=FIT_LISTING,
        extra_listings=(HOLD_LISTING,),
        cash="2000000",
        holdings=((HOLD_LISTING, "100", "1000"),),
        prices={FIT_LISTING: "1500", HOLD_LISTING: "1000"},
        constraints=PortfolioConstraints(
            account_id=FIRST_ACCOUNT,
            max_single_weight=Decimal("0.1"),
        ),
    )
    valuation = source.valuation.model_copy(
        update={
            "id": valuation_id,
            "account_id": SECOND_ACCOUNT,
            "snapshot_id": snapshot_id,
            "reference": ObjectRef(kind="portfolio_valuation", id=valuation_id, revision=1),
        }
    )
    account = account.model_copy(update={"current_snapshot_id": snapshot_id})
    return account, snapshot, valuation


async def main() -> None:
    workdir = await asyncio.to_thread(lambda: Path(os.environ["AF11_FIXTURE_DIR"]).resolve())
    if not str(workdir).startswith(("/private/var/folders/", "/tmp/")):
        raise RuntimeError("AF11 fixture must use an OS temporary directory")
    if await asyncio.to_thread(lambda: any(workdir.iterdir())):
        raise RuntimeError("AF11 fixture refuses a non-empty directory")
    db_path = workdir / "account-fit.sqlite"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    os.environ.update(
        API_KEY=FIXTURE_KEY,
        DATA_DIR=str(workdir),
        DATABASE_URL=database_url,
        INVESTOR_AGENT_ENABLED="true",
        PORTFOLIO_LEDGER_ENABLED="true",
        ACCOUNT_FIT_ENABLED="true",
        CANDIDATE_RANKING_ENABLED="true",
    )
    Settings.model_config["env_file"] = None
    settings = Settings(
        _env_file=None,
        api_key=FIXTURE_KEY,
        database_url=database_url,
        data_dir=workdir,
        portfolio_ledger_enabled=True,
        account_fit_enabled=True,
        candidate_ranking_enabled=True,
        investor_agent_enabled=True,
        agent_api_key="synthetic-disabled",
        agent_base_url="https://fixture.invalid/v1",
        agent_model="fixture",
        investor_agent_task_tokens=10000,
        investor_agent_daily_tokens=100000,
    )
    engine = init_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    constraints = PortfolioConstraints(account_id=FIRST_ACCOUNT, max_single_weight=Decimal("0.1"))
    first = make_inputs(
        target_listing=FIT_LISTING,
        extra_listings=(HOLD_LISTING,),
        cash="200000",
        prices={FIT_LISTING: "1500", HOLD_LISTING: "1000"},
        constraints=constraints,
    )
    async with session_scope() as session:
        await persist_inputs(session, first)
        await RuleStore(session).sync_templates(load_templates())
        await seed_ranking_universe(session, list(range(600010, 600115)))
        account, snapshot, valuation = make_second_account(first)
        session.add(
            PortfolioAccount(
                id=str(account.id),
                owner_id="local_owner",
                revision=account.revision,
                current_snapshot_id=str(snapshot.id),
                current_valuation_id=str(valuation.id),
                input_hash="c" * 64,
                payload=account.model_dump(mode="json"),
            )
        )
        await session.flush()
        session.add(
            PortfolioSnapshot(
                id=str(snapshot.id), account_id=str(account.id), revision=snapshot.revision,
                payload=snapshot.model_dump(mode="json"),
            )
        )
        session.add(
            PortfolioValuation(
                id=str(valuation.id), account_id=str(account.id), snapshot_id=str(snapshot.id),
                ledger_revision=snapshot.revision, valuation_date=valuation.valuation_date,
                algorithm_version=valuation.algorithm_version, input_digest=valuation.input_digest,
                payload=valuation.model_dump(mode="json"),
            )
        )

    app = create_app(settings=settings)

    app.dependency_overrides[investment_assessments.service] = session_service(
        lambda session: SuitabilityInputService(session, rules=SyntheticRules())
    )
    worker = ResearchWorker(
        settings=settings,
        worker_id="account-fit-browser",
        handlers=[
            AccountFitHandler(settings=settings),
            CandidateRankingHandler(settings=settings),
        ],
    )

    @app.post("/fixture/run", dependencies=[Depends(verify_api_key_or_session)])
    async def run_fixture_jobs():
        completed = 0
        for _ in range(10):
            if not await worker.run_once():
                break
            completed += 1
        return {"completed": completed}

    @app.post("/fixture/corrupt-run/{run_id}", dependencies=[Depends(verify_api_key_or_session)])
    async def corrupt_fixture_run(run_id: int):
        async with session_scope() as session:
            run = await session.get(PipelineRun, run_id)
            if run is None:
                return {"updated": False}
            payload = dict(run.request_json or {})
            payload["frozen_inputs"] = {}
            run.request_json = payload
            await session.flush()
            return {"updated": True}

    @app.get("/fixture/accounts", dependencies=[Depends(verify_api_key_or_session)])
    async def fixture_accounts():
        async with session_scope() as session:
            rows = list(await session.scalars(select(PortfolioAccount).order_by(PortfolioAccount.id)))
            return [Account.model_validate(row.payload).model_dump(mode="json") for row in rows]

    @asynccontextmanager
    async def isolated_lifespan(_app):
        yield

    app.router.lifespan_context = isolated_lifespan
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=int(os.environ["AF11_FIXTURE_PORT"]),
        lifespan="off",
        log_level="warning",
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
