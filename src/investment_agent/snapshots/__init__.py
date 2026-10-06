"""Point-in-time snapshot collection jobs (P1-A3).

Four queue job kinds feed the PIT snapshot store:

* ``universe_sync`` - baostock index constituents -> ``universe_membership``;
* ``industry_sync`` - baostock CSRC industries -> ``industry_classification``;
* ``market_snapshot`` - iFinD daily bars (raw + backward-adjusted) and total
  shares for every universe member -> ``market_snapshot``;
* ``financial_snapshot`` - iFinD statements / indicators for one report
  period -> ``financial_snapshot``.
"""
