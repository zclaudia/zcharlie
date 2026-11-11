"""Sell / trim discipline: no floating-profit trim, absurd PE, two exit tiers, streak."""

from __future__ import annotations

from datetime import date, timedelta

from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import ConditionGroup
from investment_agent.rules.screen import evaluate_zone_outcomes
from investment_agent.rules.streak import StreakMemory, StreakSession
from investment_agent.rules.templates import load_templates
from investment_agent.rules.validation import effective_params, factors_in
from investment_agent.rules.zone_detection import zone_disposition

DAY = date(2026, 5, 12)
ABSURD_TEMPLATES = {
    "quality_growth",
    "steady_quality_growth",
    "cycle_reversal",
    "high_growth_venture",
}


def _value(name: str, value: float | None, quality: str = "ok") -> FactorValue:
    return FactorValue(name, "test", value, quality)  # type: ignore[arg-type]


def _session(
    params: dict[str, float], as_of: date, prior: dict[str, StreakMemory] | None = None
) -> StreakSession:
    return StreakSession(prior or {}, as_of, params)


def test_templates_do_not_trim_on_floating_profit() -> None:
    loaded = {template.id: template for _, template in load_templates()}
    assert set(loaded) == ABSURD_TEMPLATES | {"dividend_blue_chip"}
    for template in loaded.values():
        assert "gain_vs_cost" not in factors_in(template.holding_zones["trim"]), template.id
        assert "gain_vs_cost" not in factors_in(template.screen), template.id
        help_text = (template.param_meta or {})["streak_periods"].help or ""
        assert "未校准" in help_text
        assert template.params["streak_periods"] == 3
        assert "exit_sell" in template.holding_zones
        assert "exit_review" in template.holding_zones
    for name in ABSURD_TEMPLATES:
        template = loaded[name]
        assert "pe_ttm" in factors_in(template.holding_zones["trim"])
        assert "trim_pe_absurd" in template.params
        assert "未校准" in ((template.param_meta or {})["trim_pe_absurd"].help or "")
    assert "pe_ttm" not in factors_in(loaded["dividend_blue_chip"].holding_zones["trim"])


def test_absurd_pe_trims_and_a_large_gain_does_not() -> None:
    template = next(item for _, item in load_templates() if item.id == "quality_growth")
    params = effective_params(template)
    params["streak_periods"] = 1
    zones = {"trim": template.holding_zones["trim"]}
    gained = {
        "pe_ttm": _value("pe_ttm", 20),
        "pe_percentile_5y": _value("pe_percentile_5y", 0.40),
        "position_weight": _value("position_weight", 0.02),
        "gain_vs_cost": _value("gain_vs_cost", 2.0),
    }
    quiet = evaluate_zone_outcomes(
        zones=zones, factors=gained, params=params, streak=_session(params, DAY)
    )
    assert quiet["trim"].holds is False

    absurd = {**gained, "pe_ttm": _value("pe_ttm", 80)}
    fired = evaluate_zone_outcomes(
        zones=zones, factors=absurd, params=params, streak=_session(params, DAY)
    )
    assert fired["trim"].holds is True
    assert [reason["factor"] for reason in fired["trim"].met] == ["pe_ttm"]


def test_exit_tiers_have_different_runtime_dispositions() -> None:
    assert zone_disposition("exit_sell") == "sell"
    assert zone_disposition("exit_review") == "rereview"
    assert zone_disposition("trim") is None

    template = next(item for _, item in load_templates() if item.id == "quality_growth")
    params = effective_params(template)
    params["streak_periods"] = 1
    zones = {
        "exit_sell": template.holding_zones["exit_sell"],
        "exit_review": template.holding_zones["exit_review"],
    }
    falsified = {
        "gross_margin": _value("gross_margin", 0.10),
        "roe": _value("roe", 0.20),
        "net_profit_growth_3y": _value("net_profit_growth_3y", 0.10),
        "revenue_growth_3y": _value("revenue_growth_3y", 0.20),
    }
    sold = evaluate_zone_outcomes(
        zones=zones, factors=falsified, params=params, streak=_session(params, DAY)
    )
    assert sold["exit_sell"].holds is True
    assert sold["exit_review"].holds is False
    assert [reason["factor"] for reason in sold["exit_sell"].met] == ["gross_margin"]

    review = {
        **falsified,
        "gross_margin": _value("gross_margin", 0.60),
        "revenue_growth_3y": _value("revenue_growth_3y", 0.0),
    }
    queued = evaluate_zone_outcomes(
        zones=zones, factors=review, params=params, streak=_session(params, DAY)
    )
    assert queued["exit_sell"].holds is False
    assert queued["exit_review"].holds is True
    assert [reason["factor"] for reason in queued["exit_review"].met] == ["revenue_growth_3y"]


def test_consecutive_periods_filter_a_single_noisy_period() -> None:
    zone = ConditionGroup.model_validate(
        {
            "any": [
                {
                    "factor": "pe_ttm",
                    "op": ">=",
                    "value": "$trim_pe_absurd",
                    "min_periods": "$streak_periods",
                }
            ]
        }
    )
    params = {"trim_pe_absurd": 60.0, "streak_periods": 3.0}
    zones = {"trim": zone}

    def step(
        as_of: date, pe: float, prior: dict[str, StreakMemory]
    ) -> tuple[bool | None, StreakSession]:
        session = _session(params, as_of, prior)
        outcome = evaluate_zone_outcomes(
            zones=zones,
            factors={"pe_ttm": _value("pe_ttm", pe)},
            params=params,
            streak=session,
        )
        return outcome["trim"].holds, session

    leaf = "trim.any[0]"

    def remembered(session: StreakSession) -> dict[str, StreakMemory]:
        parsed = StreakMemory.from_json(session.snapshots()["trim"])
        assert parsed is not None
        return {"trim": parsed}

    holds, first = step(DAY, 80, {})
    assert holds is False
    assert first.snapshots()["trim"]["after"][leaf] == 1

    holds, second = step(DAY + timedelta(days=1), 80, remembered(first))
    assert holds is False
    assert second.snapshots()["trim"]["after"][leaf] == 2

    holds, third = step(DAY + timedelta(days=2), 80, remembered(second))
    assert holds is True
    assert third.snapshots()["trim"]["after"][leaf] == 3

    # A single quiet period resets the count, so the next loud period is noise again.
    holds, reset = step(DAY + timedelta(days=1), 20, remembered(first))
    assert holds is False
    assert reset.snapshots()["trim"]["after"][leaf] == 0
    holds, again = step(DAY + timedelta(days=2), 80, remembered(reset))
    assert holds is False
    assert again.snapshots()["trim"]["after"][leaf] == 1

    # Re-running the same date does not add a period.
    replay = _session(params, DAY, remembered(first))
    replayed = evaluate_zone_outcomes(
        zones=zones,
        factors={"pe_ttm": _value("pe_ttm", 80)},
        params=params,
        streak=replay,
    )
    assert replayed["trim"].holds is False
    assert replay.snapshots()["trim"]["after"][leaf] == 1
