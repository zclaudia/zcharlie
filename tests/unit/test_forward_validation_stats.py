"""Pure statistics and tradability rules of the forward-validation tool.

No database and no clock: these tests pin the formulas that the report is
built on (rank correlation with ties, bucket splitting, monotonicity), the
A-share price-limit ratios, the T+1 entry / fixed-session exit window, the
suspension and limit deferral, and the cost model.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from investment_agent.research.forward_validation import stats
from investment_agent.research.forward_validation.contracts import (
    HORIZON_SESSIONS,
    CostModel,
    PriceSeries,
)
from investment_agent.research.forward_validation.prices import (
    compute_outcome,
    entry_tradability,
    exit_tradability,
    net_return,
    price_limit_ratio,
    resolve_entry,
)

START = date(2024, 1, 2)


def sessions(count: int, *, start: date = START, step_days: int = 1) -> list[date]:
    return [start + timedelta(days=step_days * index) for index in range(count)]


def linear_series(
    days: list[date],
    *,
    ticker: str = "600000.SH",
    start_price: float = 10.0,
    daily: float = 0.01,
    suspended: set[date] | None = None,
) -> PriceSeries:
    """Prices growing ``daily`` per session; suspended days repeat the previous close."""

    suspended = suspended or set()
    raw_open: dict[date, float] = {}
    raw_close: dict[date, float] = {}
    adjusted_open: dict[date, float] = {}
    adjusted_close: dict[date, float] = {}
    volume: dict[date, float] = {}
    trade_status: dict[date, int | None] = {}
    is_st: dict[date, bool | None] = {}
    previous = start_price
    for index, day in enumerate(days):
        if day in suspended:
            raw_open[day] = previous * 1.001
            raw_close[day] = previous
            volume[day] = 0.0
            trade_status[day] = 0
        else:
            previous = start_price * (1.0 + daily) ** index
            raw_open[day] = previous
            raw_close[day] = previous
            volume[day] = 1_000_000.0
            trade_status[day] = 1
        adjusted_open[day] = raw_open[day] * 2.0
        adjusted_close[day] = raw_close[day] * 2.0
        is_st[day] = False
    return PriceSeries(
        ticker=ticker,
        raw_open=raw_open,
        raw_close=raw_close,
        adjusted_open=adjusted_open,
        adjusted_close=adjusted_close,
        volume=volume,
        trade_status=trade_status,
        is_st=is_st,
    )


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #


def test_spearman_matches_pearson_on_ranks_and_handles_ties():
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # Average ranks: ties must not be broken by input order.
    assert stats.spearman([1, 1, 2, 2], [5, 4, 3, 2]) is not None
    assert stats.spearman([1], [2]) is None
    assert stats.spearman([1, 1, 1], [1, 2, 3]) is None


def test_average_ranks_shares_tied_positions():
    assert stats.average_ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_bucket_count_prefers_deciles_then_coarser_groups():
    assert stats.bucket_count(group_size=5, total=300) == 10
    assert stats.bucket_count(group_size=5, total=40) == 5
    assert stats.bucket_count(group_size=5, total=15) == 3
    assert stats.bucket_count(group_size=5, total=12) == 0


def test_score_buckets_are_sorted_by_score_and_keep_the_top_rank_first():
    pairs = [(float(score), float(score) / 100.0, float(score) / 100.0) for score in range(10)]
    buckets, used = stats.score_buckets(pairs, group_size=1, buckets=5)
    assert used == 10
    assert [bucket.label for bucket in buckets] == ["b1", "b2", "b3", "b4", "b5"]
    assert buckets[0].mean_net_excess == pytest.approx(0.085)  # scores 9,8
    assert buckets[-1].mean_net_excess == pytest.approx(0.005)  # scores 1,0
    assert buckets[0].count == 2


def test_score_buckets_never_invent_groups_when_small():
    pairs = [(float(score), 0.01, 0.01) for score in range(4)]
    buckets, used = stats.score_buckets(pairs, group_size=5)
    assert buckets == []
    assert used == 4


def test_monotonicity_reports_strict_and_insufficient():
    increasing = [
        stats.Bucket(
            label=f"b{index + 1}",
            lower_rank=index,
            upper_rank=index,
            count=10,
            # Bucket b1 is the top score; its mean is the highest here.
            mean_net_excess=float(5 - index) / 100.0,
            median_net_excess=None,
            hit_rate=None,
            mean_net_return=None,
            below_min_group_size=False,
        )
        for index in range(5)
    ]
    verdict = stats.monotonicity(increasing)
    assert verdict["status"] == "ok" and verdict["strict"] is True
    assert verdict["spearman"] == pytest.approx(1.0)

    overlapping = [
        stats.Bucket(
            label=f"b{index}",
            lower_rank=index,
            upper_rank=index,
            count=2,
            mean_net_excess=0.01,
            median_net_excess=None,
            hit_rate=None,
            mean_net_return=None,
            below_min_group_size=True,
        )
        for index in range(3)
    ]
    assert stats.monotonicity(overlapping)["status"] == "insufficient"


# --------------------------------------------------------------------------- #
# price limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("ticker", "is_st", "expected"),
    [
        ("600000.SH", False, 0.10),
        ("000001.SZ", False, 0.10),
        ("300750.SZ", False, 0.20),
        ("688111.SH", False, 0.20),
        ("430047.BJ", False, 0.30),
        ("600000.SH", True, 0.05),
        ("300750.SZ", True, 0.20),
    ],
)
def test_price_limit_ratio_by_board_and_st(ticker: str, is_st: bool, expected: float):
    assert price_limit_ratio(ticker, is_st=is_st) == expected


# --------------------------------------------------------------------------- #
# tradable entry / exit
# --------------------------------------------------------------------------- #


def test_suspension_blocks_and_defers_the_entry():
    days = sessions(40)
    suspended = set(days[1:4])
    series = linear_series(days, suspended=suspended)
    entry, deferral, state = resolve_entry(series, days, days[0], max_deferral=5)
    assert entry == days[4]
    assert deferral == 3
    assert state.tradable is True


def test_entry_that_stays_untradable_beyond_max_deferral_is_reported():
    days = sessions(20)
    series = linear_series(days, suspended=set(days[1:10]))
    entry, deferral, state = resolve_entry(series, days, days[0], max_deferral=2)
    assert entry is None
    assert state.reason == "suspended"
    assert deferral == 2


def test_limit_up_open_blocks_entry():
    days = sessions(10)
    series = linear_series(days)
    # 10 % limit on a main-board ticker: make the next open exactly +10 %.
    target = days[1]
    previous = series.raw_close[days[0]]
    series.raw_open[target] = round(previous * 1.10, 2)
    series.raw_close[target] = series.raw_open[target]
    blocked = entry_tradability(series, days, target)
    assert blocked.tradable is False and blocked.reason == "limit_up"
    entry, deferral, state = resolve_entry(series, days, days[0], max_deferral=0)
    assert entry is None and state.reason == "limit_up" and deferral == 0


def test_limit_down_close_blocks_exit():
    days = sessions(10)
    series = linear_series(days)
    target = days[2]
    previous = series.raw_close[days[1]]
    series.raw_close[target] = round(previous * 0.90, 2)
    blocked = exit_tradability(series, days, target)
    assert blocked.tradable is False and blocked.reason == "limit_down"


def _drop_bars(series: PriceSeries, days: list[date]) -> PriceSeries:
    """The same stock with no bar at all on ``days`` (a long suspension)."""

    def without(mapping):
        return {key: value for key, value in mapping.items() if key not in set(days)}

    return PriceSeries(
        ticker=series.ticker,
        raw_open=without(series.raw_open),
        raw_close=without(series.raw_close),
        adjusted_open=without(series.adjusted_open),
        adjusted_close=without(series.adjusted_close),
        volume=without(series.volume),
        trade_status=without(series.trade_status),
        is_st=without(series.is_st),
    )


def test_resumption_at_limit_up_is_not_buyable():
    # A long suspension leaves no bar on the previous market session; the limit
    # must still be computed from the stock's last pre-suspension close.
    days = sessions(60)
    base = linear_series(days, daily=0.0)
    last_traded = days[10]
    resumption = days[40]
    reference = base.raw_close[last_traded]
    gap = days[11:40]
    series = _drop_bars(base, gap)
    # Resumption opens exactly at +10 % (main board) of the last actual close.
    series.raw_open[resumption] = round(reference * 1.10, 2)
    series.raw_close[resumption] = series.raw_open[resumption]
    series.adjusted_open[resumption] = series.raw_open[resumption]
    series.adjusted_close[resumption] = series.raw_close[resumption]
    blocked = entry_tradability(series, days, resumption)
    assert blocked.tradable is False and blocked.reason == "limit_up"
    assert "limit_unknown" not in blocked.flags


def test_resumption_at_limit_down_is_not_sellable():
    days = sessions(60)
    base = linear_series(days, daily=0.0)
    last_traded = days[10]
    resumption = days[40]
    reference = base.raw_close[last_traded]
    series = _drop_bars(base, days[11:40])
    series.raw_open[resumption] = round(reference * 0.95, 2)
    series.raw_close[resumption] = round(reference * 0.90, 2)  # closes limit-down
    series.adjusted_open[resumption] = series.raw_open[resumption]
    series.adjusted_close[resumption] = series.raw_close[resumption]
    blocked = exit_tradability(series, days, resumption)
    assert blocked.tradable is False and blocked.reason == "limit_down"


def test_zero_volume_is_untradable_even_when_status_says_trading():
    days = sessions(10)
    series = linear_series(days)
    day = days[3]
    series.volume[day] = 0.0
    series.trade_status[day] = 1  # inconsistent provider row: still no trade
    blocked = entry_tradability(series, days, day)
    assert blocked.tradable is False and blocked.reason == "suspended"
    assert exit_tradability(series, days, day).reason == "suspended"


def test_first_listing_has_no_limit_reference_and_is_flagged():
    days = sessions(10)
    series = linear_series(days)
    first = days[0]
    result = entry_tradability(series, days, first)
    assert result.tradable is True
    assert "limit_unknown" in result.flags


def test_horizon_exit_uses_fixed_session_count_and_next_session_open_entry():
    days = sessions(200)
    series = linear_series(days, daily=0.001)
    cost = CostModel()
    outcome = compute_outcome(
        series,
        days,
        days[0],
        1,
        cost,
        max_entry_deferral=5,
        max_exit_deferral=5,
    )
    assert outcome.status == "ok"
    assert outcome.entry_date == days[1]  # T+1
    assert outcome.exit_date == days[1 + HORIZON_SESSIONS[1]]
    entry_price = series.adjusted_open[days[1]]
    exit_price = series.adjusted_close[outcome.exit_date]
    gross = exit_price / entry_price - 1.0
    assert outcome.gross_return == pytest.approx(gross)
    assert outcome.net_return == pytest.approx(net_return(gross, cost))
    assert outcome.net_return < outcome.gross_return  # costs always bite
    assert outcome.signal_return is not None


def test_pending_horizon_is_not_silently_zero():
    days = sessions(30)
    series = linear_series(days)
    outcome = compute_outcome(
        series, days, days[0], 6, CostModel(), max_entry_deferral=0, max_exit_deferral=0
    )
    assert outcome.status == "pending_horizon"
    assert outcome.net_return is None


def test_suspended_exit_day_defers_the_sale():
    days = sessions(60)
    target = days[1 + HORIZON_SESSIONS[1]]
    series = linear_series(days, suspended={target})
    outcome = compute_outcome(
        series, days, days[0], 1, CostModel(), max_entry_deferral=0, max_exit_deferral=3
    )
    assert outcome.status == "ok"
    assert outcome.exit_date == days[1 + HORIZON_SESSIONS[1] + 1]
    assert outcome.exit_deferral_sessions == 1


def test_exit_deferral_exhausted_by_suspension_is_exit_untradable():
    days = sessions(60)
    target_index = 1 + HORIZON_SESSIONS[1]
    series = linear_series(
        days, suspended=set(days[target_index : target_index + 4])
    )
    outcome = compute_outcome(
        series, days, days[0], 1, CostModel(), max_entry_deferral=0, max_exit_deferral=3
    )
    assert outcome.status == "exit_untradable"
    assert outcome.reason == "suspended"
    assert outcome.exit_date is None
    assert outcome.exit_deferral_sessions == 3


def test_exit_deferral_exhausted_by_missing_bar_is_exit_untradable():
    days = sessions(60)
    target_index = 1 + HORIZON_SESSIONS[1]
    series = _drop_bars(
        linear_series(days), days[target_index : target_index + 4]
    )
    outcome = compute_outcome(
        series, days, days[0], 1, CostModel(), max_entry_deferral=0, max_exit_deferral=3
    )
    assert outcome.status == "exit_untradable"
    assert outcome.reason == "missing_bar"
    assert outcome.exit_date is None
    assert outcome.exit_deferral_sessions == 3


def test_exit_deferral_exhausted_by_limit_down_is_exit_untradable():
    days = sessions(60)
    target_index = 1 + HORIZON_SESSIONS[1]
    series = linear_series(days)
    previous = series.raw_close[days[target_index - 1]]
    for day in days[target_index : target_index + 4]:
        previous = round(previous * 0.90, 2)
        series.raw_close[day] = previous
    outcome = compute_outcome(
        series, days, days[0], 1, CostModel(), max_entry_deferral=0, max_exit_deferral=3
    )
    assert outcome.status == "exit_untradable"
    assert outcome.reason == "limit_down"
    assert outcome.exit_date is None
    assert outcome.exit_deferral_sessions == 3


def test_cost_model_is_explicit_and_applied_to_both_sides():
    cost = CostModel(commission_bps=3.0, stamp_duty_bps=5.0, slippage_bps=10.0)
    assert cost.round_trip_bps == pytest.approx(2 * 13.0 + 5.0)
    assert net_return(0.0, cost) == pytest.approx(
        cost.sell_factor / cost.buy_factor - 1.0
    )
    assert net_return(0.0, cost) < 0.0
    assert net_return(0.0, CostModel(commission_bps=0.0, stamp_duty_bps=0.0, slippage_bps=0.0)) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        CostModel(commission_bps=-1.0)
