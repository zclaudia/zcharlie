"""Point-in-time snapshot collection jobs (P1-A3, P1-A7).

Four queue job kinds feed the PIT snapshot store:

* ``universe_sync`` - baostock index constituents -> ``universe_membership``;
* ``industry_sync`` - baostock CSRC industries -> ``industry_classification``;
* ``market_snapshot`` - daily bars (raw + backward-adjusted) -> ``market_snapshot``:
  baostock for the target pool (with PE/PB/PS TTM, turnover, suspension, ST),
  iFinD for the watchlist (with total shares);
* ``financial_snapshot`` - one report period -> ``financial_snapshot``:
  baostock quarterly tables for the target pool (``available_from`` = pubDate),
  iFinD statements / indicators for the watchlist.

Routing follows decision D6 (``provider`` + ``scope`` on the payload).
"""
