"""Screen-job fixtures and helpers (moved from tests/unit/test_screen_job.py)."""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import date
from pathlib import Path

import pytest
import yaml

from investment_agent.config import Settings
from investment_agent.factors.snapshot_model import SnapshotView
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules import screen_job
from investment_agent.rules.screen_job import (
    ScreenRunHandler,
    ScreenRunPayload,
    enqueue_screen_run,
)
from investment_agent.storage.database import get_engine, init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import UniverseMembership
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from tests.unit._factor_support import baostock_view

AS_OF = date(2026, 9, 30)
TICKERS = {"600519.SH": "600519", "601398.SH": "601398"}

TEMPLATE = {
    "id": "job_test",
    "name": "Job test",
    "applicable_groups": ["consumer", "banking"],
    "screen": {
        "all": [
            {"factor": "roe", "op": ">=", "value": "$roe_min"},
            {"factor": "gross_margin", "op": ">=", "value": 0.5},
        ]
    },
    "candidate_zones": {
        "buy_zone": {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": 0.3}]},
        "drop": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "holding_zones": {
        "add": {"all": [{"factor": "position_weight", "op": "<", "value": 0.1}]},
        "trim": {"any": [{"factor": "gain_vs_cost", "op": ">", "value": 0.5}]},
        "exit_review": {"any": [{"factor": "roe", "op": "<", "value": 0.05}]},
    },
    "params": {"roe_min": 0.1},
    "param_ranges": {"roe_min": {"min": 0.05, "max": 0.3}},
}


@pytest.fixture
def screen_db(tmp_path, monkeypatch):
    db_path = tmp_path / "screen.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    for key, value in {
        "API_KEY": "secret",
        "LLM_TIER3_API_KEY": "x",
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": url,
        "COLUMNS": "200",
    }.items():
        monkeypatch.setenv(key, value)

    def fake_view(session, ticker: str, as_of: date, **_: object) -> SnapshotView:
        return baostock_view(TICKERS[ticker], as_of)

    async def fake_load(session, ticker, as_of, **kwargs):  # noqa: ANN001
        return fake_view(session, ticker, as_of)

    monkeypatch.setattr(screen_job, "load_snapshot_view", fake_load)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "job_test.yaml").write_text(yaml.safe_dump(TEMPLATE), encoding="utf-8")
    monkeypatch.setattr(screen_job, "load_templates", _templates_from(templates))

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            await UniverseRepository(session).ensure_universe("CSI300")
            for ticker in TICKERS:
                listing = await ListingRepository(session).ensure_a_share_listing(ticker)
                session.add(
                    UniverseMembership(
                        universe_code="CSI300",
                        listing_id=listing.id,
                        valid_from=date(2020, 1, 1),
                        source="test",
                    )
                )

    asyncio.run(create())
    yield url

    async def dispose() -> None:
        # Close pooled aiosqlite connections deterministically: an engine left
        # to the garbage collector closes them from a dead event loop.
        await get_engine().dispose()

    asyncio.run(dispose())


def _templates_from(directory: Path):  # noqa: ANN202
    from investment_agent.rules.templates import load_templates

    def load(_: Path | None = None, **kwargs):  # noqa: ANN001, ANN202
        return load_templates(directory, **kwargs)

    return load


async def _run_job(payload: ScreenRunPayload) -> int:
    settings = Settings()  # type: ignore[call-arg]
    async with session_scope() as session:
        run, created = await enqueue_screen_run(session, payload, triggered_by="test")
        run_id = run.id
    assert created
    worker = ResearchWorker(
        settings=settings,
        worker_id=f"test:{socket.gethostname()}:{os.getpid()}",
        handlers=[ScreenRunHandler()],
    )
    await worker.run_once(run_id=run_id)
    return run_id
