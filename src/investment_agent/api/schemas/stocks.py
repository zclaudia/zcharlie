"""/stocks endpoint schemas (directory search behind the browser combobox)."""

from datetime import date

from pydantic import BaseModel

from investment_agent.directory_search import (
    StockSearchItem as StockSearchItem,  # moved read model, re-exported
)


class StockIndustryItem(BaseModel):
    ticker: str
    industry: str | None = None


class StockReportYearItem(BaseModel):
    ticker: str
    report_year: int | None = None
    disclosed_on: date | None = None
