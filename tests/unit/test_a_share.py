"""A-share symbol mapping tests."""

from investment_agent.domain.a_share import (
    format_listing_ticker,
    parse_a_share_ticker,
)


def test_parse_a_share_ticker_maps_supported_exchanges():
    shanghai = parse_a_share_ticker("600406.SH")
    shenzhen = parse_a_share_ticker("000001.sz")
    beijing = parse_a_share_ticker("920001.BJ")
    assert shanghai is not None
    assert shenzhen is not None
    assert beijing is not None
    assert shanghai[0] == "600406"
    assert shenzhen[1].market_code == "SZSE"
    assert beijing[1].market_code == "BSE"


def test_parse_a_share_ticker_rejects_noncanonical_symbols():
    assert parse_a_share_ticker("AAPL") is None
    assert parse_a_share_ticker("600406") is None
    assert parse_a_share_ticker("60040.SH") is None


def test_format_listing_ticker_only_adds_known_a_share_suffix():
    assert format_listing_ticker(ticker="600406", market_code="SSE") == "600406.SH"
    assert format_listing_ticker(ticker="AAPL", market_code="NASDAQ") == "AAPL"
