"""APScheduler-based A-share watchlist refresh runner.

Runs in its own process (`ia scheduler start` or Docker service).
Reads cron + timezone from config/app.yaml.scheduler.
"""

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from investment_agent.config import AppYamlConfig, Settings, load_app_yaml
from investment_agent.logging import configure_logging
from investment_agent.research.watchlist_refresh import enqueue_watchlist_refresh
from investment_agent.scheduler.trading_calendar import (
    CalendarCoverage,
    TradingCalendar,
    UnknownTradingCalendarYearError,
    calendar_coverage,
    load_trading_calendar,
)
from investment_agent.storage.database import init_engine, session_scope

log = structlog.get_logger(__name__)

# A job that misses its fire time (process busy, host asleep, short restart)
# still runs if the scheduler notices within this window; queued misfires of
# the same job are coalesced into a single run.
MISFIRE_GRACE_SECONDS = 3600
_JOB_DEFAULTS: dict[str, object] = {
    "misfire_grace_time": MISFIRE_GRACE_SECONDS,
    "coalesce": True,
}


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


def log_calendar_coverage(calendar: TradingCalendar, today: date) -> CalendarCoverage:
    """Log calendar coverage; WARNING when it ends within the warning window."""

    coverage = calendar_coverage(calendar, today)
    fields = {
        "calendar_id": coverage.calendar_id,
        "today": today.isoformat(),
        "coverage_end": (
            coverage.coverage_end.isoformat() if coverage.coverage_end else None
        ),
        "days_remaining": coverage.days_remaining,
    }
    if coverage.expired:
        log.warning("scheduler.calendar.expired", **fields)
    elif coverage.expiring_soon:
        log.warning("scheduler.calendar.expiring_soon", **fields)
    else:
        log.info("scheduler.calendar.coverage", **fields)
    return coverage


def watchlist_catch_up_date(
    *,
    now: datetime,
    trigger: CronTrigger,
    calendar: TradingCalendar,
) -> date | None:
    """Return today's trading date if its scheduled refresh already passed.

    Used at scheduler startup: APScheduler does not replay fire times that
    elapsed while the process was down, so a restart after 15:30 on a trading
    day would otherwise skip that day's refresh. Enqueueing is idempotent per
    date (``scheduled-watchlist:<date>``), so a duplicate catch-up is harmless.
    """

    zone = trigger.timezone
    local_now = now.astimezone(zone)
    today = local_now.date()
    day_start = datetime.combine(today, time.min, tzinfo=zone)
    first_fire = trigger.get_next_fire_time(None, day_start)
    if first_fire is None or first_fire.date() != today or local_now < first_fire:
        return None
    try:
        return today if calendar.is_trading_day(today) else None
    except UnknownTradingCalendarYearError:
        return None


async def _startup_watchlist_check(app_config: AppYamlConfig, now: datetime) -> None:
    """At startup: warn about calendar expiry and catch up a missed refresh."""

    scheduler_cfg = app_config.scheduler
    calendar_path = scheduler_cfg.a_share_calendar_path
    try:
        calendar = load_trading_calendar(Path(calendar_path))
        log_calendar_coverage(
            calendar, now.astimezone(ZoneInfo(calendar.timezone)).date()
        )
        if scheduler_cfg.watchlist_refresh_cron is None:
            return
        trigger = _parse_cron(
            scheduler_cfg.watchlist_refresh_cron,
            timezone=scheduler_cfg.watchlist_refresh_timezone,
        )
        catch_up = watchlist_catch_up_date(now=now, trigger=trigger, calendar=calendar)
    except Exception:
        log.exception("scheduler.watchlist.startup_check_failed", calendar_path=calendar_path)
        return
    if catch_up is None:
        return
    log.info("scheduler.watchlist.catch_up", trading_date=catch_up.isoformat())
    await _run_watchlist_refresh_job(calendar_path)


async def _run_watchlist_refresh_job(calendar_path: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    try:
        calendar = load_trading_calendar(Path(calendar_path))
        trading_date = datetime.now(ZoneInfo(calendar.timezone)).date()
        log_calendar_coverage(calendar, trading_date)
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
    """Construct the A-share watchlist refresh job (when a cron is configured).

    The Settings instance is accepted for future expansion; scheduled jobs rebuild
    settings inside their own process-local execution context.
    """
    _ = settings  # reserved for future
    scheduler_cfg = app_config.scheduler
    sched = AsyncIOScheduler(timezone=scheduler_cfg.watchlist_refresh_timezone)
    watchlist_cron = scheduler_cfg.watchlist_refresh_cron
    if watchlist_cron is not None:
        sched.add_job(
            _run_watchlist_refresh_job,
            trigger=_parse_cron(
                watchlist_cron,
                timezone=scheduler_cfg.watchlist_refresh_timezone,
            ),
            args=[scheduler_cfg.a_share_calendar_path],
            id="watchlist_refresh",
            replace_existing=True,
            **_JOB_DEFAULTS,
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
        watchlist_cron=cfg.scheduler.watchlist_refresh_cron,
        watchlist_tz=cfg.scheduler.watchlist_refresh_timezone,
    )
    await _startup_watchlist_check(cfg, datetime.now(ZoneInfo("UTC")))
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("scheduler.stopping")
        sched.shutdown(wait=False)
