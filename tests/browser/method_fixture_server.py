"""AG30 isolated real HTTP service, seeded by actual native research runs."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
from pydantic_ai.models.function import FunctionModel

# Explicitly register every referenced model before metadata creation.
from tests import conftest  # noqa: F401
from tests.support.conversations import message, new_conversation, submit
from tests.support.methods import fresh_native_tasks, samples
from tests.support.research import progress, response, run

from investment_agent.agent.research import ResearchOutput
from investment_agent.api.app import create_app
from investment_agent.config import Settings
from investment_agent.investor.methods import MethodService
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base


async def main() -> None:
    folder = Path(os.environ['AG30_FIXTURE_DIR'])
    url = f"sqlite+aiosqlite:///{folder / 'fixture.db'}"
    os.environ.update(DATABASE_URL=url, DATA_DIR=str(folder), API_KEY='fixture-app', INVESTOR_AGENT_ENABLED='true', AGENT_API_KEY='inert', AGENT_BASE_URL='https://fixture.invalid/v1', AGENT_MODEL='fixture', AGENT_MAX_TOKENS='100', INVESTOR_AGENT_TASK_TOKENS='100000', INVESTOR_AGENT_DAILY_TOKENS='500000', INVESTOR_AGENT_TASK_SECONDS='600', APP_CONFIG_PATH=str(folder / 'absent.yaml'))
    Settings.model_config['env_file'] = None
    async with engine_lifespan(url) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        subject, _, _, failure, counter, weak, strong = await samples()
        outside = await submit(await new_conversation(), message(text='解释研究方法', research_kind='explanation'))
        actual = await run(outside, FunctionModel(lambda m, i: response(ResearchOutput(progress=progress()))))
        async with session_scope() as session:
            await MethodService(session).process_review(actual.id)
    app = create_app()
    from fastapi import Depends

    from investment_agent.api.auth import verify_api_key_or_session
    @app.post('/fixture/prospective', dependencies=[Depends(verify_api_key_or_session)])
    async def explicit_new_tasks() -> dict[str, list[str]]:
        failed, success = await fresh_native_tasks(subject, weak, strong)
        return {"task_ids": [str(failed.id), str(success.id)]}
    await uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['AG30_FIXTURE_PORT']), log_level='warning')).serve()


if __name__ == '__main__':
    asyncio.run(main())
