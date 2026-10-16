"""One-off PC12 benchmark using a fresh file-backed temporary SQLite database."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import AccountFitRequest, FitRequestItem
from investment_agent.portfolio.suitability_jobs import AccountFitAdmission, AccountFitHandler
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from tests.support.account_fit import assumptions
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs

SOURCE_TABLES = (
    "portfolio_account",
    "portfolio_snapshot",
    "portfolio_valuation",
    "investor_mandate_revision",
    "market_snapshot",
    "industry_classification",
)


def source_selects(statements: list[str]) -> int:
    return sum(
        statement.lstrip().upper().startswith("SELECT")
        and any(table in statement.lower() for table in SOURCE_TABLES)
        for statement in statements
    )


async def measure() -> dict[str, int | float | str]:
    with tempfile.TemporaryDirectory(prefix="account_fit_pc12_") as directory:
        root = Path(directory)
        database_url = f"sqlite+aiosqlite:///{root / 'pc12.sqlite'}"
        os.environ["DATABASE_URL"] = database_url
        os.environ["DATA_DIR"] = directory
        engine = init_engine(database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        listing_ids = tuple(range(600001, 600101))
        inputs = make_inputs(extra_listings=listing_ids, rules=(600000,))
        async with session_scope() as session:
            await persist_inputs(session, inputs)

        statements: list[str] = []
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _parameters, _context, _many: statements.append(
                statement
            ),
        )
        settings = Settings(
            _env_file=None,
            api_key="test",
            llm_tier3_api_key="test",
            data_dir=root,
            portfolio_ledger_enabled=True,
            account_fit_enabled=True,
            candidate_ranking_enabled=False,
        )
        request = AccountFitRequest(
            operation_id=uuid4(),
            account_id=inputs.account_id,
            snapshot_ref=inputs.snapshot.reference,
            mandate_ref=ObjectRef(
                kind="mandate", id=inputs.mandate.id, revision=inputs.mandate.revision
            ),
            valuation_binding=inputs.valuation_binding,
            purpose_binding=inputs.purpose_binding,
            items=tuple(
                FitRequestItem(
                    listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
                    action="open",
                    trade_assumptions=assumptions(),
                )
                for listing_id in listing_ids
            ),
        )

        async with session_scope() as session:
            receipt = await AccountFitAdmission(session, settings=settings).accept(request)
        admission_statements = statements[:]
        statements.clear()

        started = time.perf_counter()
        worker = ResearchWorker(
            settings=settings,
            worker_id="pc12-measurement",
            handlers=[AccountFitHandler(settings=settings)],
        )
        await worker.run_once(run_id=int(receipt.run_ref.id))
        worker_elapsed = time.perf_counter() - started
        async with session_scope() as session:
            from investment_agent.storage.models.analysis import PipelineRun

            run = await session.get(PipelineRun, int(receipt.run_ref.id))
            status = run.status if run else "missing"

        result: dict[str, int | float | str] = {
            "items": len(listing_ids),
            "admission_sql_statements": len(admission_statements),
            "admission_source_selects": source_selects(admission_statements),
            "worker_sql_statements": len(statements),
            "worker_source_selects": source_selects(statements),
            "worker_elapsed_seconds": round(worker_elapsed, 4),
            "terminal_status": status,
        }
        await dispose_engine()
        return result


def main() -> None:
    print(json.dumps(asyncio.run(measure()), sort_keys=True))


if __name__ == "__main__":
    main()
