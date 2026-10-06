"""APScheduler-based runner: A-share watchlist refresh + PIT snapshot collection.

Runs in its own process (`ia scheduler start` or Docker service).
Reads cron + timezone from config/app.yaml (``scheduler``, ``snapshot`` and
``screen``).
The scheduler only enqueues; the worker executes the jobs.
"""

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from investment_agent.config import (
    AppYamlConfig,
    ScreenScheduleConfig,
    Settings,
    SnapshotScheduleConfig,
    load_app_yaml,
)
from investment_agent.logging import configure_logging
from investment_agent.research.watchlist_refresh import enqueue_watchlist_refresh
from investment_agent.scheduler.snapshot_jobs import (
    CALENDAR_FREE_JOBS,
    SNAPSHOT_SCHEDULE_JOBS,
    SnapshotScheduleJob,
    enqueue_snapshot_payloads,
    snapshot_payloads,
)
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


# Standard crontab numbers weekdays 0/7 = Sunday, 1 = Monday ... 6 = Saturday,
# but APScheduler 3.x CronTrigger numbers them 0 = Monday ... 6 = Sunday (even
# via from_crontab), so "1-5" would fire Tuesday-Saturday. Numeric weekday
# fields are translated to names, which both read the same way.
_CRONTAB_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _crontab_weekday_number(token: str) -> int:
    if not token.isdigit() or int(token) > 7:
        raise ValueError(f"Invalid crontab weekday: '{token}'")
    return int(token) % 7  # 7 is an alias of 0 (Sunday)


def _crontab_dow(dow: str) -> str:
    """Translate a crontab day-of-week field into APScheduler weekday names.

    Numeric items (``1-5``, ``0,6``, ``1-5/2``) are expanded to explicit names
    (``mon,tue,wed,thu,fri``); ``*``, ``*/n`` and named items pass through.
    """

    items: list[str] = []
    for item in dow.split(","):
        base, _, step = item.partition("/")
        if base == "*" or not any(ch.isdigit() for ch in base):
            items.append(item)
            continue
        if "-" in base:
            first, last = base.split("-", 1)
            start, stop = _crontab_weekday_number(first), int(last)
            if not last.isdigit() or stop > 7 or stop < start:
                raise ValueError(f"Invalid crontab weekday range: '{item}'")
            numbers = range(start, stop + 1, int(step) if step else 1)
        else:
            if step:
                raise ValueError(f"Step without range in crontab weekday: '{item}'")
            numbers = range(_crontab_weekday_number(base), _crontab_weekday_number(base) + 1)
        items.extend(_CRONTAB_WEEKDAYS[number % 7] for number in numbers)
    return ",".join(dict.fromkeys(items))


def _parse_cron(cron_expr: str, *, timezone: str | None = None) -> CronTrigger:
    """Accept a standard 5-field crontab: 'minute hour day month dow'."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron must have 5 fields: '{cron_expr}'")
    minute, hour, day, month, dow = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=_crontab_dow(dow),
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

    today = fired_today(now=now, trigger=trigger)
    if today is None:
        return None
    try:
        return today if calendar.is_trading_day(today) else None
    except UnknownTradingCalendarYearError:
        return None


def fired_today(*, now: datetime, trigger: CronTrigger) -> date | None:
    """Today's local date if ``trigger`` already had a fire time today before ``now``."""

    zone = trigger.timezone
    local_now = now.astimezone(zone)
    today = local_now.date()
    day_start = datetime.combine(today, time.min, tzinfo=zone)
    first_fire = trigger.get_next_fire_time(None, day_start)
    if first_fire is None or first_fire.date() != today or local_now < first_fire:
        return None
    return today


async def run_snapshot_schedule_job(
    job: SnapshotScheduleJob,
    *,
    calendar_path: str,
    snapshot_cfg: SnapshotScheduleConfig,
    today: date | None = None,
) -> int | None:
    """One fire of a snapshot job: apply its gate and enqueue idempotently.

    Returns the number of newly created runs, or ``None`` when the job could
    not decide (unreviewed calendar year, enqueue error).
    """

    local_today = today or datetime.now(ZoneInfo(snapshot_cfg.timezone)).date()
    calendar: TradingCalendar | None = None
    try:
        if job not in CALENDAR_FREE_JOBS:
            calendar = load_trading_calendar(Path(calendar_path))
            log_calendar_coverage(calendar, local_today)
        payloads = snapshot_payloads(
            job, today=local_today, calendar=calendar, universes=snapshot_cfg.universes
        )
    except UnknownTradingCalendarYearError:
        log.exception(
            "scheduler.snapshot.calendar_unknown", job=job, calendar_path=calendar_path
        )
        return None
    except Exception:
        log.exception("scheduler.snapshot.failed", job=job)
        return None
    if not payloads:
        log.info("scheduler.snapshot.skipped", job=job, today=local_today.isoformat())
        return 0
    outcomes = await enqueue_snapshot_payloads(payloads)
    created = sum(outcome.created for outcome in outcomes)
    log.info(
        "scheduler.snapshot.queued",
        job=job,
        today=local_today.isoformat(),
        created_runs=created,
        keys=[outcome.key for outcome in outcomes],
        errors=[outcome.error for outcome in outcomes if outcome.error],
    )
    return created


async def run_screen_schedule_job(
    screen_cfg: ScreenScheduleConfig, *, today: date | None = None
) -> int | None:
    """One fire of the weekly screen: enqueue one ``screen_run`` per universe.

    ``as_of`` is the local fire date. Idempotent per (universe, as_of).
    Returns the number of newly created runs, ``None`` on an enqueue error.
    """

    from investment_agent.rules.screen_job import ScreenRunPayload, enqueue_screen_run

    local_today = today or datetime.now(ZoneInfo(screen_cfg.timezone)).date()
    created = 0
    try:
        for code in dict.fromkeys(screen_cfg.universes):
            payload = ScreenRunPayload.model_validate(
                {"universe_code": code, "as_of": local_today, "hit_cap": screen_cfg.hit_cap}
            )
            async with session_scope() as session:
                run, was_created = await enqueue_screen_run(
                    session, payload, triggered_by="scheduler"
                )
                created += was_created
                log.info(
                    "scheduler.screen.queued",
                    run_id=run.id,
                    key=run.idempotency_key,
                    created=was_created,
                )
    except Exception:
        log.exception("scheduler.screen.failed", today=local_today.isoformat())
        return None
    return created


def _screen_trigger(cfg: ScreenScheduleConfig | None) -> CronTrigger | None:
    if cfg is None or cfg.cron is None:
        return None
    return _parse_cron(cfg.cron, timezone=cfg.timezone)


def _snapshot_triggers(cfg: SnapshotScheduleConfig) -> dict[SnapshotScheduleJob, CronTrigger]:
    crons = cfg.crons()
    return {
        job: _parse_cron(cron, timezone=cfg.timezone)
        for job in SNAPSHOT_SCHEDULE_JOBS
        if (cron := crons.get(job)) is not None
    }


async def _startup_snapshot_check(app_config: AppYamlConfig, now: datetime) -> list[str]:
    """Catch up snapshot jobs whose fire time already passed today (idempotent)."""

    snapshot_cfg = app_config.snapshot
    if snapshot_cfg is None:
        return []
    caught_up: list[str] = []
    for job, trigger in _snapshot_triggers(snapshot_cfg).items():
        today = fired_today(now=now, trigger=trigger)
        if today is None:
            continue
        log.info("scheduler.snapshot.catch_up", job=job, today=today.isoformat())
        await run_snapshot_schedule_job(
            job,
            calendar_path=app_config.scheduler.a_share_calendar_path,
            snapshot_cfg=snapshot_cfg,
            today=today,
        )
        caught_up.append(job)
    return caught_up


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
    """Construct the watchlist refresh job and the snapshot collection jobs.

    Each job is only registered when its cron is configured.

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
    snapshot_cfg = app_config.snapshot
    if snapshot_cfg is not None:
        for job, trigger in _snapshot_triggers(snapshot_cfg).items():
            sched.add_job(
                run_snapshot_schedule_job,
                trigger=trigger,
                args=[job],
                kwargs={
                    "calendar_path": scheduler_cfg.a_share_calendar_path,
                    "snapshot_cfg": snapshot_cfg,
                },
                id=f"snapshot_{job}",
                replace_existing=True,
                **_JOB_DEFAULTS,
            )
    screen_trigger = _screen_trigger(app_config.screen)
    if screen_trigger is not None:
        sched.add_job(
            run_screen_schedule_job,
            trigger=screen_trigger,
            args=[app_config.screen],
            id="screen_run",
            replace_existing=True,
            **_JOB_DEFAULTS,
        )
    return sched


async def _startup_screen_check(app_config: AppYamlConfig, now: datetime) -> bool:
    """Catch up the weekly screen if its fire time already passed today (idempotent)."""

    trigger = _screen_trigger(app_config.screen)
    if trigger is None or app_config.screen is None:
        return False
    today = fired_today(now=now, trigger=trigger)
    if today is None:
        return False
    log.info("scheduler.screen.catch_up", today=today.isoformat())
    await run_screen_schedule_job(app_config.screen, today=today)
    return True


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
        snapshot_jobs=[job.id for job in sched.get_jobs() if job.id.startswith("snapshot_")],
    )
    started_at = datetime.now(ZoneInfo("UTC"))
    await _startup_watchlist_check(cfg, started_at)
    await _startup_snapshot_check(cfg, started_at)
    await _startup_screen_check(cfg, started_at)
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("scheduler.stopping")
        sched.shutdown(wait=False)
