"""Tests for the Markdown renderer."""

import pytest

from investment_agent.renderers.markdown_renderer import MarkdownRenderer


@pytest.mark.asyncio
async def test_empty_picks_render_no_picks_messages() -> None:
    renderer = MarkdownRenderer()
    out = await renderer.render_discovery(
        run_id=1,
        consensus_picks=[],
        divergent_picks=[],
        overlooked_picks=[],
        scores_by_listing={},
    )
    assert out.format == "markdown"
    assert "_No consensus picks today._" in out.body
    assert "_No divergent picks today._" in out.body
    assert "_No overlooked picks today._" in out.body
    assert out.metadata == {"run_id": 1}


@pytest.mark.asyncio
async def test_consensus_picks_render_ticker_avg_and_supporting() -> None:
    renderer = MarkdownRenderer()
    out = await renderer.render_discovery(
        run_id=42,
        consensus_picks=[
            {
                "listing_id": 7,
                "ticker": "AAPL",
                "avg_score": 78.5,
                "supporting": ["value_v1", "growth_v1"],
            }
        ],
        divergent_picks=[],
        overlooked_picks=[],
        scores_by_listing={},
    )
    assert "| AAPL | 78.5 | value_v1, growth_v1 |" in out.body
    assert "# Discovery Report — Run 42" in out.body


@pytest.mark.asyncio
async def test_divergent_picks_render_spread_bull_bear() -> None:
    renderer = MarkdownRenderer()
    out = await renderer.render_discovery(
        run_id=2,
        consensus_picks=[],
        divergent_picks=[
            {
                "listing_id": 3,
                "ticker": "TSLA",
                "spread": 55,
                "bull": ["growth_v1"],
                "bear": ["value_v1"],
            }
        ],
        overlooked_picks=[],
        scores_by_listing={},
    )
    assert "| TSLA | 55 | growth_v1 | value_v1 |" in out.body


@pytest.mark.asyncio
async def test_scores_section_truncates_theses_to_200_chars() -> None:
    long_bull = "x" * 300
    long_bear = "y" * 300
    renderer = MarkdownRenderer()
    out = await renderer.render_discovery(
        run_id=3,
        consensus_picks=[],
        divergent_picks=[],
        overlooked_picks=[],
        scores_by_listing={
            7: [
                {
                    "strategy_code": "value_v1",
                    "score": 85,
                    "confidence": 0.75,
                    "bull_thesis": long_bull,
                    "bear_thesis": long_bear,
                    "ticker": "AAPL",
                }
            ]
        },
    )
    # Header uses ticker
    assert "### AAPL" in out.body
    # Bull line is truncated to 200 chars (preceded by "Bull: ")
    assert f"Bull: {'x' * 200}\n" in out.body
    assert f"Bear: {'y' * 200}\n" in out.body
    assert "x" * 201 not in out.body
    # Confidence is formatted to 2 decimals
    assert "(conf 0.75)" in out.body
