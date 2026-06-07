"""β decomposition: regress stock returns on (market, sector) returns; report contributions."""

from collections.abc import Sequence

import numpy as np

from investment_agent.domain.strategy_types import BetaDecomposition


def decompose(
    *,
    stock_returns: Sequence[float],
    market_returns: Sequence[float],
    sector_returns: Sequence[float] | None,
) -> BetaDecomposition:
    """Returns BetaDecomposition.

    Convention:
    - sequences aligned by date (most-recent last)
    - returns are decimals (0.01 = +1%)
    - stock_return_pct / market_return_pct / sector_return_pct = most-recent point
    - β_market / β_sector come from OLS on the full window
    - explained_by_X = β_X × X_return (most recent) — point-in-time attribution
    - idiosyncratic = stock_return - explained_market - (explained_sector or 0)
    """
    s = np.asarray(stock_returns, dtype=float)
    m = np.asarray(market_returns, dtype=float)
    if s.shape != m.shape:
        raise ValueError("stock_returns and market_returns must align")
    if s.size < 3:
        raise ValueError("Need at least 3 observations for regression")

    if sector_returns is not None:
        sec: np.ndarray | None = np.asarray(sector_returns, dtype=float)
        assert sec is not None
        if sec.shape != s.shape:
            raise ValueError("sector_returns must align with stock_returns")
        X = np.column_stack([np.ones_like(s), m, sec])  # noqa: N806 (design matrix)
    else:
        sec = None
        X = np.column_stack([np.ones_like(s), m])  # noqa: N806 (design matrix)

    coeffs, _residuals, _rank, _ = np.linalg.lstsq(X, s, rcond=None)
    beta_market = float(coeffs[1])
    beta_sector = float(coeffs[2]) if sector_returns is not None else None

    fitted = X @ coeffs
    ss_res = float(np.sum((s - fitted) ** 2))
    ss_tot = float(np.sum((s - s.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0

    last_stock = float(s[-1])
    last_market = float(m[-1])
    last_sector = float(sec[-1]) if sec is not None else None

    explained_market = beta_market * last_market
    explained_sector = (
        beta_sector * last_sector
        if beta_sector is not None and last_sector is not None
        else None
    )
    idio = last_stock - explained_market - (explained_sector or 0.0)

    return BetaDecomposition(
        stock_return_pct=last_stock,
        market_return_pct=last_market,
        sector_return_pct=last_sector,
        beta_market=beta_market,
        beta_sector=beta_sector,
        explained_by_market_pct=explained_market,
        explained_by_sector_pct=explained_sector,
        idiosyncratic_pct=idio,
        r_squared=r_squared,
        window_days=int(s.size),
    )
