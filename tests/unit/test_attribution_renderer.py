import pytest

from investment_agent.renderers.markdown_renderer import MarkdownRenderer


@pytest.mark.asyncio
async def test_render_attribution_minimal():
    payload = {
        "ticker": "AAPL", "company_name": "Apple", "as_of": "2025-06-03",
        "beta": {
            "stock_return_pct": 0.025, "market_return_pct": 0.005,
            "sector_return_pct": None, "beta_market": 1.2, "beta_sector": None,
            "explained_by_market_pct": 0.006, "explained_by_sector_pct": None,
            "idiosyncratic_pct": 0.019, "r_squared": 0.82, "window_days": 30,
        },
        "result": {
            "summary": "Strong rally on services beat.",
            "triggering_events": [
                {"event_type": "earnings",
                 "headline": "Q3 services revenue surprise",
                 "impact_estimate": "+1.5%", "source": "press_release"},
            ],
            "confidence": 0.75,
        },
    }
    out = await MarkdownRenderer().render_attribution(42, payload)
    assert out.format == "markdown"
    assert "AAPL" in out.body
    assert "Idiosyncratic" in out.body
    assert "1.90%" in out.body
    assert "Q3 services revenue surprise" in out.body


@pytest.mark.asyncio
async def test_render_attribution_no_events():
    payload = {
        "ticker": "MSFT", "company_name": "Microsoft", "as_of": "2025-06-03",
        "beta": {
            "stock_return_pct": 0.001, "market_return_pct": 0.002,
            "sector_return_pct": None, "beta_market": 1.0, "beta_sector": None,
            "explained_by_market_pct": 0.002, "explained_by_sector_pct": None,
            "idiosyncratic_pct": -0.001, "r_squared": 0.90, "window_days": 30,
        },
        "result": {
            "summary": "Move is in-line with market; no specific drivers.",
            "triggering_events": [], "confidence": 0.85,
        },
    }
    out = await MarkdownRenderer().render_attribution(43, payload)
    assert "No specific triggering events" in out.body


@pytest.mark.asyncio
async def test_render_attribution_with_sector_and_extras():
    payload = {
        "ticker": "NVDA", "company_name": "Nvidia", "as_of": "2025-06-03",
        "beta": {
            "stock_return_pct": 0.05, "market_return_pct": 0.01,
            "sector_return_pct": 0.02, "beta_market": 1.4, "beta_sector": 1.1,
            "explained_by_market_pct": 0.014,
            "explained_by_sector_pct": 0.022,
            "idiosyncratic_pct": 0.014, "r_squared": 0.78, "window_days": 30,
        },
        "result": {
            "summary": "Driven by AI demand commentary + supplier news.",
            "triggering_events": [
                {"event_type": "news", "headline": "AI demand commentary",
                 "url": "https://x"},
                {"event_type": "related_company",
                 "headline": "TSMC capex up", "related_ticker": "2330"},
            ],
            "valuation_delta": "PE expanded from 35→40",
            "cross_market_context": "TSMC capex hike confirms upstream demand.",
            "confidence": 0.7,
        },
    }
    out = await MarkdownRenderer().render_attribution(44, payload)
    assert "β_sector = 1.1" in out.body
    assert "Sector return" in out.body
    assert "Explained by sector" in out.body
    assert "Valuation Delta" in out.body
    assert "Cross-Market Context" in out.body
    assert "https://x" in out.body
