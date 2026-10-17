"""P4 local browser fixture: real HTTP intake and lease-fenced local workers."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))

import uvicorn
from fastapi import Depends
from tests.support.account_fit_db import make_inputs
from tests.support.account_fit_flow import persist_inputs
from tests.support.research_closure import seed_company_view

from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler
from investment_agent.supplemental_rules.update_intake import (
    ImportRequest,
    import_saved_sources,
    resolve_saved_sources,
)
from investment_agent.supplemental_rules.update_jobs import SupplementalUpdateHandler
from investment_agent.supplemental_rules.updates import SupplementalUpdatesService

KEY = "p4-local-browser-inert-key-2026"
LISTING, OTHER = 600901, 600902


async def main():
    root = await asyncio.to_thread(Path(os.environ["P4_FIXTURE_DIR"]).resolve)
    if not str(root).startswith(("/private/var/folders/", "/tmp/")) or any(root.iterdir()):
        raise RuntimeError("P4 fixture requires empty OS temporary directory")
    database_url = f"sqlite+aiosqlite:///{root / 'p4.sqlite'}"
    os.environ.update(API_KEY=KEY, DATA_DIR=str(root), DATABASE_URL=database_url, APP_ENV="dev")
    Settings.model_config["env_file"] = None
    settings = Settings(_env_file=None, api_key=KEY, data_dir=root, database_url=database_url,
                        supplemental_rules_enabled=True, supplemental_updates_enabled=True)
    engine = init_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    inputs = make_inputs(target_listing=LISTING, extra_listings=(OTHER,))
    async with session_scope() as session:
        await persist_inputs(session, inputs)
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply({"supplemental_rules_enabled": True, "supplemental_updates_enabled": True}, actor="api")
        listing_ref = ObjectRef(kind="listing", id=LISTING, revision=1)
        _, evidence_ref = await seed_company_view(session, listing_ref)
        now = datetime.now(UTC)
        sources = await resolve_saved_sources(session, listing_ref, (evidence_ref,), settings=settings, now=now)
        digest = canonical_hash({"listing_ref": listing_ref, "context": "company_research", "sources": sources})
        await import_saved_sources(session, ImportRequest(operation_id=uuid4(), listing_ref=listing_ref,
                                  source_refs=(evidence_ref,), manifest_hash=digest), settings)
    app = create_app(settings=settings)
    worker = ResearchWorker(settings=settings, worker_id="p4-browser", handlers=[
        SupplementalUpdateHandler(settings=settings), SupplementalRuleHandler(settings=settings)])

    @app.post("/fixture/drain", dependencies=[Depends(verify_api_key_or_session)])
    async def drain():
        count = 0
        for _ in range(20):
            async with session_scope() as session:
                await SupplementalUpdatesService(session, settings).sweep()
            if await worker.run_once() is None:
                break
            count += 1
        async with session_scope() as session:
            # Force the final leased settlement wakeup without waiting for a
            # wall-clock minute bucket in a browser test.
            settle, _ = await PipelineRunRepository(session).enqueue(
                kind="supplemental_update_process", idempotency_key="p4:browser:final-settle",
                triggered_by="browser-fixture", request_json={"schema_version": 1,
                    "owner_id": "local_owner", "action": "maintenance"})
            settle_id = settle.id
        if await worker.run_once(run_id=settle_id) is not None:
            count += 1
        return {"completed": count}

    @app.post("/fixture/close", dependencies=[Depends(verify_api_key_or_session)])
    async def close():
        async with session_scope() as session:
            await store_for(session, settings).apply({"supplemental_updates_enabled": False}, actor="api")
        return {"closed": True}

    @app.post("/fixture/fail-source", dependencies=[Depends(verify_api_key_or_session)])
    async def fail_source():
        async with session_scope() as session:
            repo = SupplementalUpdatesRepository(session)
            await repo.reserve("local_owner")
            state = await repo.get_source("local_owner", "controlled_local_v1", f"listing:{LISTING}:company_research")
            assert state is not None
            await repo.save_source(state.model_copy(update={
                "revision": state.revision + 1, "coverage_state": "failed",
                "failure_code": "saved_source_incomplete", "last_attempt_at": datetime.now(UTC),
            }), expected_revision=state.revision)
        return {"failed": True}

    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(os.environ["P4_FIXTURE_PORT"]), lifespan="off", log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())
