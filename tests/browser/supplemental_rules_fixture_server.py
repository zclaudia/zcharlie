"""Local synthetic HTTP fixture for the P2 management/stock browser flow."""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends

sys.path.insert(0, str(Path.cwd()))

from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.supplemental_rules.jobs import SupplementalRuleHandler

FIXTURE_KEY = "supplemental-rules-synthetic-test-key-2026"
FIXTURE_LISTING = 600090


async def main() -> None:
    workdir = await asyncio.to_thread(Path(os.environ["SUPPLEMENTAL_RULES_FIXTURE_DIR"]).resolve)
    if not str(workdir).startswith(("/private/var/folders/", "/tmp/")):
        raise RuntimeError("supplemental P2 fixture must use an OS temporary directory")
    if await asyncio.to_thread(lambda: any(workdir.iterdir())):
        raise RuntimeError("supplemental P2 fixture refuses a non-empty directory")
    db_path = workdir / "supplemental-rules.sqlite"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    os.environ.update(
        API_KEY=FIXTURE_KEY,
        DATA_DIR=str(workdir),
        DATABASE_URL=database_url,
        APP_ENV="dev",
        SUPPLEMENTAL_RULES_ENABLED="true",
    )
    Settings.model_config["env_file"] = None
    settings = Settings(
        _env_file=None,
        api_key=FIXTURE_KEY,
        database_url=database_url,
        data_dir=workdir,
        supplemental_rules_enabled=True,
    )
    engine = init_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        session.add(Company(id=700090, canonical_name="合成测试公司"))
        session.add(Listing(id=FIXTURE_LISTING, company_id=700090, market_code="SSE", ticker="600090", is_primary=True))
        await session.flush()

    app = create_app(settings=settings)
    worker = ResearchWorker(
        settings=settings, worker_id="supplemental-rules-browser", handlers=[SupplementalRuleHandler(settings=settings)],
    )

    @app.get("/fixture/listing", dependencies=[Depends(verify_api_key_or_session)])
    async def listing() -> dict[str, int]:
        return {"listing_id": FIXTURE_LISTING, "company_id": 700090}

    @app.post("/fixture/run", dependencies=[Depends(verify_api_key_or_session)])
    async def run_jobs() -> dict[str, int]:
        completed = 0
        for _ in range(10):
            if not await worker.run_once():
                break
            completed += 1
        return {"completed": completed}

    @asynccontextmanager
    async def isolated_lifespan(_app):
        yield

    app.router.lifespan_context = isolated_lifespan
    config = uvicorn.Config(app, host="127.0.0.1", port=int(os.environ["SUPPLEMENTAL_RULES_FIXTURE_PORT"]), lifespan="off", log_level="warning")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
