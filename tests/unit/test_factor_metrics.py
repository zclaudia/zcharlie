"""Shared metric functions: one definition used by iFinD canonicalization and factors."""

from datetime import date

import pytest

from investment_agent.factors import metrics
from investment_agent.research import ifind_chain


def test_ifind_chain_uses_the_factor_library_definitions():
    assert ifind_chain._annualized_growth is metrics.annualized_growth
    assert ifind_chain._roe_stability is metrics.roe_stability
    assert ifind_chain._positive_multiple is metrics.positive_multiple
    assert ifind_chain.ONE_YEAR_WINDOW_DAYS == metrics.ONE_YEAR_WINDOW_DAYS == 365
    assert ifind_chain.MIN_ONE_YEAR_SESSIONS == metrics.MIN_ONE_YEAR_SESSIONS == 200
    assert ifind_chain.return_stats is metrics.return_stats
    assert ifind_chain.trim_to_window is metrics.trim_to_window
    assert ifind_chain._number is metrics.parse_number
    assert ifind_chain._fraction is metrics.percent_to_fraction


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.5", 1234.5),
        (" 2 ", 2.0),
        ("--", None),
        ("", None),
        ("nan", None),
        ("x", None),
        (None, None),
        ("inf", None),
        (3, 3.0),
    ],
)
def test_parse_number(raw, expected):
    assert metrics.parse_number(raw) == expected


def test_percent_to_fraction():
    assert metrics.percent_to_fraction("34.46") == 34.46 / 100.0
    assert metrics.percent_to_fraction("--") is None


@pytest.mark.parametrize(
    ("latest", "base", "expected"),
    [
        (133.1, 100.0, 0.1),
        (None, 100.0, None),
        (100.0, None, None),
        (-1.0, 100.0, None),
        (100.0, 0.0, None),
        (100.0, -5.0, None),
    ],
)
def test_annualized_growth(latest, base, expected):
    assert metrics.annualized_growth(latest, base, 3) == expected


def test_annualized_growth_rejects_non_positive_span():
    assert metrics.annualized_growth(2.0, 1.0, 0) is None


def test_roe_stability_rules():
    assert metrics.roe_stability([]) is None
    assert metrics.roe_stability([0.1, None, 0.1, 0.1]) is None
    assert metrics.roe_stability([-0.1, -0.2, 0.05, 0.0]) is None
    assert metrics.roe_stability([0.2, 0.2, 0.2, 0.2]) == 1.0
    assert metrics.roe_stability([0.01, 0.01, 0.01, 1.0]) == 0.0


def test_positive_multiple():
    assert metrics.positive_multiple(10.0, 2.0) == 5.0
    assert metrics.positive_multiple(10.0, 0.0) is None
    assert metrics.positive_multiple(10.0, -1.0) is None
    assert metrics.positive_multiple(None, 1.0) is None


def test_trim_to_window_is_left_open_right_closed():
    items = [(date(2025, 1, 1), "a"), (date(2025, 1, 2), "b"), (date(2026, 1, 1), "c")]
    assert metrics.trim_to_window(items, date(2026, 1, 1)) == ["b", "c"]
    assert metrics.trim_to_window(items, None) == ["b", "c"]
    assert metrics.trim_to_window(items, date(2025, 12, 31)) == ["a", "b"]
    assert metrics.trim_to_window(items, date(2026, 1, 1), days=1) == ["c"]
    assert metrics.trim_to_window([], None) == []


def test_return_stats_ignores_non_positive_and_sorts():
    series = [("3", 110.0), ("1", 100.0), ("2", None), ("2b", 0.0), ("4", 99.0)]
    total, vol, drawdown = metrics.return_stats(series)
    assert total == -0.01
    assert drawdown == 0.1
    assert vol is not None and vol > 0
    assert metrics.return_stats([("1", 1.0)]) == (None, None, None)
