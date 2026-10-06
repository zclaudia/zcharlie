"""Business-calendar clock helpers.

The server process may run in any timezone (UTC inside Docker), but A-share
business dates follow Asia/Shanghai. Use these helpers instead of
``date.today()`` whenever a value represents a business/trading date.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# Legacy NASDAQ Discovery/Attribution business dates follow the US exchange.
US_LEGACY_TZ = ZoneInfo("America/New_York")


def today_in(timezone: str | ZoneInfo) -> date:
    """Return the current calendar date in ``timezone``."""

    zone = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(timezone)
    return datetime.now(zone).date()


def shanghai_today() -> date:
    """Return the current A-share business date (Asia/Shanghai)."""

    return datetime.now(SHANGHAI_TZ).date()
