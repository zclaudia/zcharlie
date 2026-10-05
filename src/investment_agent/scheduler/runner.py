"""APScheduler-based daily Discovery and A-share watchlist runner.

Runs in its own process (`ia scheduler start` or Docker service).
Reads cron + timezone from config/app.yaml.scheduler.
"""

import asyncio
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from investment_agent.config import AppYamlConfig, Settings, load_app_yaml
from investment_agent.logging import configure_logging
from investment_agent.pipelines.discovery import DiscoveryPipeline
from investment_agent.research.watchlist_refresh import enqueue_watchlist_refresh
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)
from investment_agent.sources.yfinance_source import YFinanceSource
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.strategies.registry import load_all_strategies

log = structlog.get_logger(__name__)


def _parse_cron(cron_expr: str, *, timezone: str | None = None) -> CronTrigger:
    """Accept a 5-field crontab: 'minute hour day month dow'."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron must have 5 fields: '{cron_expr}'")
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=dow,
        timezone=timezone,
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


async def _enqueue_scheduled_watchlist_refresh(
    *,
    settings: Settings,
    calendar: TradingCalendar,
    trading_date: date,
) -> int | None:
    """Queue one idempotent daily batch, or skip a reviewed non-trading day."""

    if not calendar.is_trading_day(trading_date):
        log.info(
            "scheduler.watchlist.skipped",
            trading_date=trading_date.isoformat(),
            reason="non_trading_day",
        )
        return None
    if not settings.ifind_proxy_api_key:
        raise RuntimeError("IFIND_PROXY_API_KEY is not configured")
    async with session_scope() as session:
        outcomes = await enqueue_watchlist_refresh(
            session,
            as_of=trading_date,
            triggered_by="scheduler",
            idempotency_key=f"scheduled-watchlist:{trading_date.isoformat()}",
        )
    return sum(outcome.created for outcome in outcomes)


async def _run_watchlist_refresh_job(calendar_path: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    try:
        calendar = load_trading_calendar(Path(calendar_path))
        trading_date = datetime.now(ZoneInfo(calendar.timezone)).date()
        created = await _enqueue_scheduled_watchlist_refresh(
            settings=settings,
            calendar=calendar,
            trading_date=trading_date,
        )
        if created is not None:
            log.info(
                "scheduler.watchlist.queued",
                trading_date=trading_date.isoformat(),
                created_runs=created,
            )
    except UnknownTradingCalendarYearError:
        log.exception(
            "scheduler.watchlist.calendar_unknown",
            calendar_path=calendar_path,
        )
    except Exception:
        log.exception(
            "scheduler.watchlist.failed",
            calendar_path=calendar_path,
        )


def build_scheduler(
    *, settings: Settings, app_config: AppYamlConfig,
) -> AsyncIOScheduler:
    """Construct discovery jobs plus an optional A-share watchlist refresh job.

    The Settings instance is accepted for future expansion; scheduled jobs rebuild
    settings inside their own process-local execution context.
    """
    _ = settings  # reserved for future
    sched = AsyncIOScheduler(timezone=app_config.scheduler.discovery_timezone)
    trigger = _parse_cron(
        app_config.scheduler.discovery_cron,
        timezone=app_config.scheduler.discovery_timezone,
    )
    for ue in app_config.universes:
        sched.add_job(
            _run_discovery_job, trigger=trigger,
            args=[ue.code], id=f"discovery_{ue.code}", replace_existing=True,
        )
    watchlist_cron = app_config.scheduler.watchlist_refresh_cron
    if watchlist_cron is not None:
        sched.add_job(
            _run_watchlist_refresh_job,
            trigger=_parse_cron(
                watchlist_cron,
                timezone=app_config.scheduler.watchlist_refresh_timezone,
            ),
            args=[app_config.scheduler.a_share_calendar_path],
            id="watchlist_refresh",
            replace_existing=True,
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
        watchlist_cron=cfg.scheduler.watchlist_refresh_cron,
        watchlist_tz=cfg.scheduler.watchlist_refresh_timezone,
        universes=[u.code for u in cfg.universes],
    )
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("scheduler.stopping")
        sched.shutdown(wait=False)
