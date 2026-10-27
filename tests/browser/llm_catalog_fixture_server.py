"""Disposable settings UI server. No startup collectors or live LLM calls.

PYTHONPATH=src .venv/bin/python tests/browser/llm_catalog_fixture_server.py /tmp/catalog-ui 19871
"""

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet

from investment_agent.api.app import create_app
from investment_agent.config import Settings
from investment_agent.storage.database import init_engine
from investment_agent.storage.models import Base


async def main():
    folder = Path(sys.argv[1])
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{folder / 'fixture.db'}"
    os.environ.update(DATABASE_URL=url, DATA_DIR=str(folder), API_KEY="fixture-app",
                      SETTINGS_ENCRYPTION_KEY=Fernet.generate_key().decode())
    Settings.model_config["env_file"] = None
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        await uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=int(sys.argv[2]),
                                            lifespan="off", log_level="warning")).serve()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
