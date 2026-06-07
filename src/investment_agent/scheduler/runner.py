"""APScheduler-based daily Discovery runner.

Runs in its own process (`ia scheduler start` or Docker service).
Reads cron + timezone from config/app.yaml.scheduler.
"""

import asyncio
from datetime import date
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from investment_agent.config import AppYamlConfig, Settings, load_app_yaml
from investment_agent.logging import configure_logging
from investment_agent.pipelines.discovery import DiscoveryPipeline
from investment_agent.sources.yfinance_source import YFinanceSource
from investment_agent.storage.database import init_engine
from investment_agent.strategies.registry import load_all_strategies

log = structlog.get_logger(__name__)


def _parse_cron(cron_expr: str) -> CronTrigger:
    """Accept a 5-field crontab: 'minute hour day month dow'."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron must have 5 fields: '{cron_expr}'")
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute, hour=hour, day=day, month=month, day_of_week=dow,
    )


async def _run_discovery_job(universe_code: str) -> None:
    """One scheduled tick: run discovery for the given universe."""
    log.info("scheduler.tick.start", universe=universe_code)
    settings = Settings()  # type: ignore[call-arg]
    strategies = load_all_strategies(
        tier2_config=settings.llm_tier2, tier3_config=settings.llm_tier3,
    )
    pipeline = DiscoveryPipeline(
        data_source=YFinanceSource(
            max_concurrent=settings.yfinance_max_concurrent,
        ),  # type: ignore[arg-type]
        strategies=strategies, settings=settings,
    )
    try:
        run_id = await pipeline.run(universe_code, date.today())
        log.info("scheduler.tick.complete", universe=universe_code, run_id=run_id)
    except Exception:
        log.exception("scheduler.tick.failed", universe=universe_code)


def build_scheduler(
    *, settings: Settings, app_config: AppYamlConfig,
) -> AsyncIOScheduler:
    """Construct AsyncIOScheduler with one cron job per configured universe.

    The job is `_run_discovery_job(universe_code)` with the cron from app_config.
    The Settings instance is accepted for future expansion (concurrency-limits etc.)
    even though _run_discovery_job currently rebuilds its own.
    """
    _ = settings  # reserved for future
    sched = AsyncIOScheduler(timezone=app_config.scheduler.discovery_timezone)
    trigger = _parse_cron(app_config.scheduler.discovery_cron)
    for ue in app_config.universes:
        sched.add_job(
            _run_discovery_job, trigger=trigger,
            args=[ue.code], id=f"discovery_{ue.code}", replace_existing=True,
        )
    return sched


async def run_forever(app_config_path: str = "config/app.yaml") -> None:
    """Entry point: configure logging, init engine, start scheduler, idle until cancelled."""
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    init_engine(settings.database_url)
    cfg = load_app_yaml(Path(app_config_path))
    sched = build_scheduler(settings=settings, app_config=cfg)
    sched.start()
    log.info(
        "scheduler.started",
        cron=cfg.scheduler.discovery_cron,
        tz=cfg.scheduler.discovery_timezone,
        universes=[u.code for u in cfg.universes],
    )
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("scheduler.stopping")
        sched.shutdown(wait=False)
