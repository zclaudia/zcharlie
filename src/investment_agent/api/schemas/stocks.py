"""/stocks endpoint schemas (directory search behind the browser combobox)."""

from pydantic import BaseModel


class StockSearchItem(BaseModel):
    """One directory hit; the client fills ``ticker`` / ``name`` into a form."""

    ticker: str
    name: str
    status: str
    """``listed`` / ``suspended`` / ``delisted``."""
    in_universe: bool
    """Member of the current snapshot target pool today."""
    on_watchlist: bool
