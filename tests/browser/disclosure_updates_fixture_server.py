"""B browser fixture with a real loopback source and a real durable worker."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path.cwd()))

import uvicorn
from fastapi import Depends, Request, Response
from sqlalchemy import func, select
from tests.support.p4_disclosures import (
    LoopbackDisclosureHTTP,
    fixture_protocol,
    fixture_source_response,
)

from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.evidence.disclosure_pages import DisclosurePageService
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_rules import ResearchEventRevision
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_updates import (
    DisclosurePollingHandler,
    maintain_disclosure_polls,
)
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler
from investment_agent.supplemental_rules.update_jobs import SupplementalUpdateHandler

KEY = "p4-b-local-browser-fixture-only-key"
FIXTURE_CONTRACT_HASH = canonical_hash({
    "fixture_contract": "p4b-browser-v1", "protocol_hash": fixture_protocol().protocol_hash,
})


async def main() -> None:
    root = await asyncio.to_thread(Path(os.environ["P4B_FIXTURE_DIR"]).resolve)
    if not str(root).startswith(("/private/var/folders/", "/tmp/")) or any(root.iterdir()):
        raise RuntimeError("B browser requires an empty OS temporary directory")
    port = int(os.environ["P4B_FIXTURE_PORT"])
    url = f"sqlite+aiosqlite:///{root / 'p4b.sqlite'}"
    os.environ.update(API_KEY=KEY, DATA_DIR=str(root), DATABASE_URL=url, APP_ENV="dev")
    Settings.model_config["env_file"] = None
    settings = Settings(_env_file=None, api_key=KEY, data_dir=root, database_url=url,
                        supplemental_rules_enabled=True, supplemental_updates_enabled=True,
                        supplemental_disclosure_polling_enabled=True, disclosure_originals_enabled=True)
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Market(code="SZSE", name="Fixture Shenzhen", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="B browser fixture issuer, no real company facts")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        other = Listing(company_id=company.id, market_code="SZSE", ticker="300902")
        session.add_all([listing, other])
        await session.flush()
        listing_id = listing.id
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply({"supplemental_rules_enabled": True, "supplemental_updates_enabled": True,
                           "supplemental_disclosure_polling_enabled": True,
                           "disclosure_originals_enabled": True}, actor="api")
    app = create_app(settings=settings)
    published = datetime.now(UTC) - timedelta(hours=6)
    mode = {"value": "rate_limited"}
    http = LoopbackDisclosureHTTP(f"http://127.0.0.1:{port}", headers={"X-API-Key": KEY})
    page_service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    worker = ResearchWorker(settings=settings, worker_id="p4b-browser", handlers=[
        DisclosurePollingHandler.for_tests(settings=settings, page_service=page_service),
        SupplementalUpdateHandler(settings=settings), SupplementalRuleHandler(settings=settings),
    ])

    @app.post("/fixture/source", dependencies=[Depends(verify_api_key_or_session)])
    async def source(request: Request) -> Response:
        status, headers, body = fixture_source_response(
            request.headers.get("X-Fixture-Purpose", ""), await request.body(),
            published_at=published, mode=mode["value"],
        )
        return Response(body, status_code=status, headers=headers)

    @app.get("/fixture/info", dependencies=[Depends(verify_api_key_or_session)])
    async def info() -> dict:
        now = datetime.now(UTC)
        listing_ref = {"kind": "listing", "id": listing_id, "revision": 1}
        window_start, window_end = now - timedelta(days=1), now
        command = {"action": "create", "operation_id": str(uuid4()), "authorization_id": str(uuid4()),
                   "expected_revision": 0, "expected_owner_policy_revision": 0,
                   "owner_daily_http": 24, "owner_daily_response_bytes": 128 * 1024 * 1024,
                   "owner_min_request_interval_seconds": 5,
                   "body": {"contract_hash": FIXTURE_CONTRACT_HASH, "listing_refs": [listing_ref],
                            "doc_type_allowlist": ["annual_report_full"],
                            "history_start": window_start.isoformat(), "history_end": window_end.isoformat(),
                            "min_poll_interval_seconds": 43_200, "min_http_interval_seconds": 5,
                            "expires_at": (now + timedelta(days=7)).isoformat(),
                            "max_staleness_seconds": 129_600,
                            "per_poll_query_http": 4, "per_poll_fetch": 3, "per_poll_http": 8,
                            "per_poll_response_bytes": 64 * 1024 * 1024,
                            "grant_daily_http": 24, "grant_daily_response_bytes": 128 * 1024 * 1024,
                            "storage_bytes_cap": 512 * 1024 * 1024}}
        return {"listing_id": listing_id, "listing_ref": listing_ref, "grant_command": command,
                "window_start": window_start, "window_end": window_end}

    @app.post("/fixture/normal", dependencies=[Depends(verify_api_key_or_session)])
    async def normal() -> dict:
        mode["value"] = "normal"
        return {"mode": "normal"}

    @app.post("/fixture/drain", dependencies=[Depends(verify_api_key_or_session)])
    async def drain(wait: bool = False) -> dict:
        completed, deadline = 0, time.monotonic() + (100 if wait else 10)
        while time.monotonic() < deadline:
            async with session_scope() as session:
                await maintain_disclosure_polls(session, settings=settings)
            run_id = await worker.run_once()
            if run_id is not None:
                completed += 1
                if not wait:
                    break
            elif not wait:
                break
            else:
                async with session_scope() as session:
                    current = await SupplementalUpdatesRepository(session).get_source(
                        "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research")
                    if current and current.payload.get("poll_state") in {"completed", "complete"}:
                        break
                # No inline collection or mock status mutation: let the actual
                # retry_at and the source's five-second cadence become due.
                await asyncio.sleep(0.2)
        return {"completed": completed, "http_calls": len(http.calls)}

    @app.get("/fixture/stats", dependencies=[Depends(verify_api_key_or_session)])
    async def stats() -> dict:
        async with session_scope() as session:
            return {"agent_tasks": await session.scalar(select(func.count()).select_from(AgentTask)),
                    "events": await session.scalar(select(func.count()).select_from(ResearchEventRevision)),
                    "http_calls": len(http.calls)}

    @app.post("/fixture/close", dependencies=[Depends(verify_api_key_or_session)])
    async def close() -> dict:
        async with session_scope() as session:
            await store_for(session, settings).apply({"supplemental_disclosure_polling_enabled": False,
                                                     "supplemental_updates_enabled": False}, actor="api")
        return {"closed": True}

    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off",
                                        log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())
