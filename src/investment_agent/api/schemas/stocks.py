"""/stocks endpoint schemas (directory search behind the browser combobox)."""

from datetime import date

from pydantic import BaseModel


class StockSearchItem(BaseModel):
    """One directory hit; the client fills the stock's form fields."""

    ticker: str
    name: str
    industry: str | None = None
    """Current Shenwan level-1 industry, with CSRC as fallback."""
    report_year: int | None = None
    """Latest completed fiscal year confirmed by an actual annual disclosure."""
    disclosed_on: date | None = None
    status: str
    """``listed`` / ``suspended`` / ``delisted``."""
    in_universe: bool
    """Member of the current snapshot target pool today."""
    on_watchlist: bool


class StockIndustryItem(BaseModel):
    ticker: str
    industry: str | None = None


class StockReportYearItem(BaseModel):
    ticker: str
    report_year: int | None = None
    disclosed_on: date | None = None
