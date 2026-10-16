"""Runtime settings consumers (settings-plan §4.4): API, worker, LLM key, health."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from structlog.testing import capture_logs

from investment_agent.config import LLMTierConfig, Settings
from investment_agent.settings.loader import (
    ensure_imported,
    import_preview,
    load_app_config,
    load_runtime,
)
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.app_settings import AppSettingChange, AppSettingMeta
from investment_agent.storage.models.base import Base

NO_YAML = Path("does-not-exist.yaml")


def _settings(tmp_path: Path, **values: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, api_key="test", llm_tier3_api_key="k", data_dir=tmp_path, **values
    )


async def _import(settings: Settings, app_yaml: Path = NO_YAML) -> None:
    async with session_scope() as session:
        await ensure_imported(session, settings, app_yaml)


async def _apply(settings: Settings, values: dict[str, object]) -> None:
    async with session_scope() as session:
        await SettingsStore(session).apply(values, actor="ui")


# --- API ---------------------------------------------------------------------


def test_api_settings_follow_the_db_without_restart(api_client) -> None:
    headers = {"X-API-Key": "secret"}
    def budget_cap() -> int:
        response = api_client.get("/health/data", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["verification"]["budget"]["cap"]

    assert budget_cap() == 150

    settings = Settings()  # type: ignore[call-arg]
    asyncio.run(_import(settings))
    asyncio.run(_apply(settings, {"ifind_budget_per_5h": 77}))

    assert budget_cap() == 77
    # Auth keeps working off the env bootstrap layer.
    assert api_client.get("/health/data").status_code == 401


def _import_state(url: str) -> tuple[object, int, int, int]:
    """``(imported_at, revision, history rows, ifind_budget_per_5h)``."""

    async def read() -> tuple[object, int, int, int]:
        engine = init_engine(url)
        async with session_scope() as session:
            meta = await session.get(AppSettingMeta, 1)
            assert meta is not None
            changes = await session.scalar(select(func.count()).select_from(AppSettingChange))
            budget = (await SettingsStore(session).load()).ifind_budget_per_5h
            result = (meta.imported_at, meta.revision, int(changes or 0), budget)
        await engine.dispose()
        return result

    return asyncio.run(read())


def test_app_startup_import_is_idempotent(tmp_path, monkeypatch) -> None:
    from investment_agent.api.app import create_app

    async def offline_directory(**kwargs):
        # This verifies settings import, not the provider bootstrap. A real
        # directory thread can outlive TestClient and hang an offline suite.
        return None

    monkeypatch.setattr("investment_agent.api.app.initialize_directory", offline_directory)

    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("IFIND_BUDGET_PER_5H", "120")

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    state = _import_state(url)
    assert state[0] is not None
    assert state[3] == 120

    monkeypatch.setenv("IFIND_BUDGET_PER_5H", "90")  # ignored: the DB is the source of truth
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    assert _import_state(url) == state


def test_app_starts_on_an_unmigrated_database(monkeypatch, caplog) -> None:
    from investment_agent.api.app import create_app

    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    # The lifespan reconfigures structlog (stdlib-backed), so read stdlib records.
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
    assert "settings.import_failed" in caplog.text


# --- worker ------------------------------------------------------------------


async def test_worker_rebuilds_handlers_only_when_the_revision_moves(db_session, tmp_path) -> None:
    from investment_agent.research.worker import ResearchWorker

    base = _settings(tmp_path)
    await ensure_imported(db_session, base, NO_YAML)
    await db_session.commit()
    worker = ResearchWorker(settings=base, worker_id="w", kinds=["screen_run", "market_snapshot"])
    original = dict(worker.handlers)

    with capture_logs() as logs:
        assert await worker.run_once() is None  # records the revision
        assert await worker.run_once() is None
    assert worker.handlers == original
    assert all(a is b for a, b in zip(worker.handlers.values(), original.values(), strict=True))
    assert not [entry for entry in logs if entry["event"] == "worker.settings_reloaded"]

    await _apply(
        base, {"candidate_expiry_days": 9, "ifind_collect_rate_per_minute": 12.0}
    )
    async with session_scope() as session:
        revision = await SettingsStore(session).revision()
    with capture_logs() as logs:
        assert await worker.run_once() is None
    (reloaded,) = [entry for entry in logs if entry["event"] == "worker.settings_reloaded"]
    assert reloaded["revision"] == revision
    assert set(worker.kinds) == {"market_snapshot", "screen_run"}
    assert worker.settings.candidate_expiry_days == 9
    assert worker.handlers["screen_run"] is not original["screen_run"]
    assert worker.handlers["screen_run"].candidate_policy.expiry_days == 9  # type: ignore[attr-defined]
    assert worker.handlers["market_snapshot"].runtime.limiter.rate_per_minute == 12.0  # type: ignore[attr-defined]

    with capture_logs() as logs:
        await worker.run_once()
    assert not [entry for entry in logs if entry["event"] == "worker.settings_reloaded"]


async def test_worker_with_injected_handlers_never_reloads(db_session, tmp_path) -> None:
    from investment_agent.candidates.lifecycle import CandidatePolicy
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.rules.screen_job import ScreenRunHandler

    base = _settings(tmp_path)
    await ensure_imported(db_session, base, NO_YAML)
    await db_session.commit()
    handler = ScreenRunHandler(candidate_policy=CandidatePolicy(expiry_days=3))
    worker = ResearchWorker(settings=base, worker_id="w", handlers=[handler])
    await worker.run_once()
    await _apply(base, {"candidate_expiry_days": 9})
    await worker.run_once()
    assert worker.handlers == {"screen_run": handler}
    assert worker.settings is base


# --- LLM key -----------------------------------------------------------------


def test_llm_key_may_be_absent_from_env_but_fails_clearly_at_use(monkeypatch) -> None:
    from investment_agent.llm.openai_compatible import (
        LLMNotConfiguredError,
        OpenAICompatibleClient,
    )

    monkeypatch.delenv("LLM_TIER3_API_KEY", raising=False)
    settings = Settings(_env_file=None, api_key="test")  # type: ignore[call-arg]
    assert settings.llm_tier3_api_key == ""
    with pytest.raises(LLMNotConfiguredError, match="llm_tier3_api_key"):
        OpenAICompatibleClient(tier_name="tier3", config=settings.llm_tier3, session=None)  # type: ignore[arg-type]
    config = LLMTierConfig(base_url="http://llm", api_key="k", model="m")
    OpenAICompatibleClient(tier_name="tier3", config=config, session=None)  # type: ignore[arg-type]


# --- schedule readers ----------------------------------------------------------


async def test_runtime_schedule_before_and_after_import(db_session, tmp_path) -> None:
    yaml_path = tmp_path / "app.yaml"
    yaml_path.write_text(
        "scheduler:\n  watchlist_refresh_cron: '30 19 * * 1-5'\n"
        "  a_share_calendar_path: cal.yaml\n"
        "snapshot:\n  universes: [SSE50]\n",
        encoding="utf-8",
    )
    base = _settings(tmp_path)

    # Before the import: what the import would store (app.yaml + env).
    runtime = await load_runtime(db_session, base, yaml_path)
    assert runtime == import_preview(base, yaml_path)
    assert runtime.schedule.snapshot.universes == ["SSE50"]
    assert runtime.schedule.screen.cron is None  # section absent from app.yaml

    await ensure_imported(db_session, base, yaml_path)
    await SettingsStore(db_session).apply(
        {"schedule.snapshot.universes": ["CSI300"], "schedule.screen.cron": "0 6 * * mon"},
        actor="ui",
    )
    config, revision = await load_app_config(db_session, base, yaml_path)
    assert revision == await SettingsStore(db_session).revision() > 0
    assert config.scheduler.a_share_calendar_path == "cal.yaml"  # stays in app.yaml
    assert config.scheduler.watchlist_refresh_cron == "30 19 * * 1-5"
    assert config.snapshot is not None and config.snapshot.universes == ["CSI300"]
    assert config.screen is not None and config.screen.cron == "0 6 * * mon"


async def test_data_health_universes_come_from_the_store(db_session, tmp_path) -> None:
    from investment_agent.api.routes import data_health

    base = _settings(tmp_path)
    await ensure_imported(db_session, base, NO_YAML)
    await SettingsStore(db_session).apply(
        {"schedule.snapshot.universes": ["SSE50", "CSI300"]}, actor="ui"
    )
    assert await data_health._snapshot_universes(db_session, base) == ("SSE50", "CSI300")
