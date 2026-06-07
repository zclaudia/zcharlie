"""Tests for scheduler.runner (no actual pipeline execution)."""

import pytest

from investment_agent.config import (
    AppYamlConfig,
    AuxiliaryTrackingConfig,
    PipelineConfig,
    SchedulerConfig,
    Settings,
    UniverseEntry,
)
from investment_agent.scheduler.runner import _parse_cron, build_scheduler


def test_parse_cron_valid() -> None:
    t = _parse_cron("30 16 * * 1-5")
    assert t is not None


def test_parse_cron_invalid_raises() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        _parse_cron("30 16 *")


def test_build_scheduler_registers_job_per_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_KEY", "x")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "x")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    cfg = AppYamlConfig(
        universes=[UniverseEntry(code="NASDAQ_100"), UniverseEntry(code="TEST_U")],
        pipeline=PipelineConfig(),
        scheduler=SchedulerConfig(
            discovery_cron="30 16 * * 1-5", discovery_timezone="UTC",
        ),
        auxiliary_tracking=AuxiliaryTrackingConfig(),
    )
    sched = build_scheduler(settings=settings, app_config=cfg)
    job_ids = {j.id for j in sched.get_jobs()}
    assert "discovery_NASDAQ_100" in job_ids
    assert "discovery_TEST_U" in job_ids
    # Scheduler was never started, so no shutdown call is needed.
