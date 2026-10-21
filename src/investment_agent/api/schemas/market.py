"""/market endpoint schemas (daily bars for the sparkline)."""

from datetime import date, datetime

from pydantic import BaseModel


class MarketBar(BaseModel):
    """One closing price; the client only plots it."""

    trade_date: date
    close: float


class MarketBarsOut(BaseModel):
    """Latest ``days`` closes known at ``as_of`` for one listing (oldest first)."""

    ticker: str
    provider: str
    as_of: datetime
    bars: list[MarketBar]


class MarketBarsBatchItem(BaseModel):
    """One requested ticker; ``found`` is false when no A-share listing exists."""

    ticker: str
    found: bool
    bars: list[MarketBar]


class MarketBarsBatchOut(BaseModel):
    """Latest ``days`` closes for several listings at one ``as_of`` (request order)."""

    provider: str
    as_of: datetime
    days: int
    items: list[MarketBarsBatchItem]
