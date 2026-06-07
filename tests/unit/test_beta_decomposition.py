import pytest

from investment_agent.attribution.beta_decomposition import decompose


def test_decompose_perfectly_explains_market_only():
    """stock = 1.5 × market exactly → β_market = 1.5, R² = 1, idiosyncratic = 0."""
    market = [0.01, -0.02, 0.015, -0.01, 0.005, 0.02, -0.015]
    stock = [m * 1.5 for m in market]
    result = decompose(
        stock_returns=stock, market_returns=market, sector_returns=None,
    )
    assert abs(result.beta_market - 1.5) < 1e-6
    assert abs(result.r_squared - 1.0) < 1e-6
    assert abs(result.idiosyncratic_pct) < 1e-6
    assert result.beta_sector is None


def test_decompose_with_sector():
    """stock = 0.5 × market + 0.7 × sector → β values recovered."""
    market = [0.01, -0.02, 0.015, -0.005, 0.008, 0.02, -0.012]
    sector = [-0.005, 0.01, 0.012, 0.001, -0.003, 0.015, -0.004]
    stock = [0.5 * m + 0.7 * s for m, s in zip(market, sector)]
    result = decompose(stock_returns=stock, market_returns=market, sector_returns=sector)
    assert abs(result.beta_market - 0.5) < 1e-3
    assert result.beta_sector is not None
    assert abs(result.beta_sector - 0.7) < 1e-3
    assert result.r_squared > 0.99


def test_decompose_handles_short_series():
    with pytest.raises(ValueError, match="at least 3"):
        decompose(stock_returns=[0.01], market_returns=[0.01], sector_returns=None)


def test_idiosyncratic_is_residual():
    """Constant alpha on top of market beta=1 → idiosyncratic ≈ alpha at last point."""
    market = [0.01, -0.02, 0.015, -0.01, 0.005]
    stock = [m * 1.0 + 0.005 for m in market]
    result = decompose(stock_returns=stock, market_returns=market, sector_returns=None)
    assert abs(result.idiosyncratic_pct - 0.005) < 1e-3
