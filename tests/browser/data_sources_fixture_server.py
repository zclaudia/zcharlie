"""Disposable data-sources UI server (config-docs-plan §4.6, Tab A).

Runs against a throwaway SQLite in ``argv[1]``; the field_sources baseline is
the repo's ``config/field_sources.yaml`` (first import happens on demand).

PYTHONPATH=src .venv/bin/python tests/browser/data_sources_fixture_server.py /tmp/ds-ui 19887
"""

import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from investment_agent.api.app import create_app
from investment_agent.config import Settings
from investment_agent.storage.database import init_engine
from investment_agent.storage.models import Base


async def main() -> None:
    folder = Path(sys.argv[1])
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{folder / 'fixture.db'}"
    os.environ.update(DATABASE_URL=url, DATA_DIR=str(folder), API_KEY="ds-ui-fixture-key")
    Settings.model_config["env_file"] = None
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        await uvicorn.Server(
            uvicorn.Config(create_app(), host="127.0.0.1", port=int(sys.argv[2]), lifespan="off", log_level="warning")
        ).serve()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
