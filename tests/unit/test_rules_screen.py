"""P1-C3: pure screen engine - semantics, determinism, pre-filter, cap/ranking."""

from __future__ import annotations

import random
from datetime import date
from typing import Any

import pytest

from investment_agent.factors.registry import FactorValue
from investment_agent.rules.schema import ConditionGroup, RuleTemplate
from investment_agent.rules.screen import (
    ScreenConfig,
    ScreenDefinitionError,
    ScreenMember,
    TemplateResult,
    evaluate_zone_conditions,
    required_factors,
    run_screen,
    screen_json,
)
from investment_agent.rules.validation import parse_template
from tests.unit._rules_support import CONTEXT, template_data

AS_OF = date(2026, 9, 30)
KNOWN = CONTEXT.factor_names


def fv(name: str, value: float | None, quality: str = "ok", *flags: str) -> FactorValue:
    return FactorValue(
        name,
        "1",
        value,
        quality,  # type: ignore[arg-type]
        tuple(flags),
        {"provider": "baostock", "as_of": "2026-09-30", "field": name, "ref": f"snap:{name}"},
    )


GOOD: dict[str, Any] = {
    "roe": 0.20,
    "pe_percentile_5y": 0.20,
    "debt_ratio": 0.40,
    "gross_margin": 0.50,
    "is_st": 0.0,
    "report_overdue": 0.0,
}


def member(
    ticker: str, group: str = "consumer", listing_id: int = 1, **overrides: Any
) -> ScreenMember:
    factors: dict[str, FactorValue] = {}
    for name, value in {**GOOD, **overrides}.items():
        if isinstance(value, FactorValue):
            factors[name] = value
        elif value is not None:
            factors[name] = fv(name, value)
    return ScreenMember(
        listing_id=listing_id,
        ticker=ticker,
        industry_group=group,
        factors=factors,
        context={"provider": "baostock", "industry_group": group},
    )


def template(**overrides: Any) -> RuleTemplate:
    return parse_template(template_data(**overrides), context=CONTEXT)


def screen(members: list[ScreenMember], templates: list[RuleTemplate] | None = None,
           config: ScreenConfig | None = None) -> TemplateResult:
    run = run_screen(
        as_of=AS_OF,
        members=members,
        templates=templates or [template()],
        config=config,
        known_factors=KNOWN,
    )
    return run.templates[0]


def outcome(result: TemplateResult, ticker: str) -> str:
    return next(m.outcome for m in result.members if m.ticker == ticker)


def test_evaluate_zone_conditions_returns_screen_reason_shape() -> None:
    zones: dict[str, Any] = {
        "buy_zone": ConditionGroup.model_validate(
            {"all": [{"factor": "pe_percentile_5y", "op": "<=", "value": "$pe_pct_max"}]}
        )
    }
    checks = evaluate_zone_conditions(
        zones=zones, factors={"pe_percentile_5y": fv("pe_percentile_5y", 0.2)}, params={"pe_pct_max": 0.2}
    )
    [reason] = checks["buy_zone"]
    assert reason["factor"] == "pe_percentile_5y"
    assert reason["outcome"] == "met"
    assert reason["actual"] == 0.2
    assert reason["threshold"] == 0.2
    assert reason["param"] == "pe_pct_max"
    assert isinstance(reason["snapshot_ref"], dict)


def test_evaluate_zone_conditions_records_missing_factor() -> None:
    zones: dict[str, Any] = {
        "drop": ConditionGroup.model_validate({"any": [{"factor": "roe", "op": "<", "value": 0.08}]})
    }
    checks = evaluate_zone_conditions(zones=zones, factors={}, params={})
    [reason] = checks["drop"]
    assert reason["outcome"] == "missing"
    assert reason["actual"] is None
    assert reason["quality"] == "missing"


def test_hit_reasons_carry_threshold_param_actual_and_snapshot_ref() -> None:
    result = screen([member("600519.SH")])
    hit = result.by_outcome("hit")[0]
    assert hit.rank == 1
    by_path = {reason["rule_path"]: reason for reason in hit.reasons}
    roe = by_path["screen.all[0]"]
    assert roe == {
        "rule_path": "screen.all[0]",
        "factor": "roe",
        "op": ">=",
        "threshold": 0.15,
        "param": "roe_min",
        "actual": 0.20,
        "quality": "ok",
        "flags": [],
        "outcome": "met",
        "snapshot_ref": {"field": "roe", "ref": "snap:roe"},
    }
    # any-group: both alternatives met -> both are reasons; literal threshold has no param.
    assert by_path["screen.all[2].any[0]"]["param"] is None
    assert by_path["screen.all[2].any[1]"]["threshold"] == [0.3, 0.9]


def test_miss_aggregation_by_failing_condition() -> None:
    members = [
        member("000001.SZ", roe=0.10),
        member("000002.SZ", roe=0.10, pe_percentile_5y=0.9),
        member("000003.SZ", debt_ratio=0.9, gross_margin=0.1),
    ]
    result = screen(members)
    assert result.counts()["miss"] == 3
    misses = result.misses_by_condition
    assert misses["screen.all[0]"]["not_met"] == 2
    assert misses["screen.all[1]"]["not_met"] == 1
    assert misses["screen.all[2].any[0]"]["not_met"] == 1
    assert misses["screen.all[2].any[1]"]["not_met"] == 1
    miss = next(m for m in result.members if m.ticker == "000002.SZ")
    assert {r["rule_path"] for r in miss.reasons} == {"screen.all[0]", "screen.all[1]"}


def test_missing_factor_fails_condition_and_is_recorded() -> None:
    result = screen(
        [
            member("000001.SZ", roe=fv("roe", None, "missing", "no_annual_report")),
            member("000002.SZ", roe=None),  # factor absent altogether
        ]
    )
    assert result.counts()["miss"] == 2
    assert result.missing_by_factor == {"roe": 2}
    assert result.misses_by_condition["screen.all[0]"]["missing"] == 2
    first = next(m for m in result.members if m.ticker == "000001.SZ")
    assert first.reasons[0]["outcome"] == "missing"
    assert first.reasons[0]["flags"] == ["no_annual_report"]
    absent = next(m for m in result.members if m.ticker == "000002.SZ")
    assert absent.reasons[0]["flags"] == ["factor_absent"]


def test_missing_inside_any_group_degrades_gracefully() -> None:
    result = screen([member("000001.SZ", debt_ratio=fv("debt_ratio", None, "missing"))])
    hit = result.by_outcome("hit")[0]
    assert [r["factor"] for r in hit.reasons if r["outcome"] == "met"].count("gross_margin") == 1


def test_not_applicable_condition_is_skipped() -> None:
    na = {"debt_ratio": fv("debt_ratio", None, "not_applicable"),
          "gross_margin": fv("gross_margin", None, "not_applicable")}
    result = screen([member("601398.SH", **na)])
    # The whole any-group is skipped; the remaining conditions decide.
    hit = result.by_outcome("hit")[0]
    skipped = [r for r in hit.reasons if r["outcome"] == "not_applicable"]
    assert {r["factor"] for r in skipped} == {"debt_ratio", "gross_margin"}


def test_all_conditions_not_applicable_is_skipped_not_hit() -> None:
    tpl = template(
        screen={"all": [{"factor": "debt_ratio", "op": "<", "value": 0.6}]},
    )
    result = screen([member("601398.SH", debt_ratio=fv("debt_ratio", None, "not_applicable"))],
                    [tpl])
    assert outcome(result, "601398.SH") == "skipped"
    assert result.members[0].detail == "all_conditions_not_applicable"


def test_approx_is_allowed_but_flagged() -> None:
    result = screen(
        [member("000001.SZ", roe=fv("roe", 0.2, "approx", "short_history"))]
    )
    hit = result.by_outcome("hit")[0]
    roe = next(r for r in hit.reasons if r["factor"] == "roe")
    assert (roe["quality"], roe["flags"]) == ("approx", ["short_history"])


def test_industry_group_not_applicable_is_skipped() -> None:
    result = screen([member("601398.SH", group="banking")])
    assert outcome(result, "601398.SH") == "skipped"
    assert result.members[0].detail == "industry_group_not_applicable"


def test_prefilter_excludes_st_and_overdue_and_is_configurable() -> None:
    members = [
        member("000001.SZ", is_st=1.0),
        member("000002.SZ", report_overdue=1.0),
        member("000003.SZ"),
    ]
    run = run_screen(as_of=AS_OF, members=members, templates=[template()], known_factors=KNOWN)
    assert dict(run.excluded) == {"000001.SZ": ("is_st",), "000002.SZ": ("report_overdue",)}
    result = run.templates[0]
    assert outcome(result, "000001.SZ") == "excluded"
    assert outcome(result, "000002.SZ") == "excluded"
    assert outcome(result, "000003.SZ") == "hit"
    relaxed = screen(members, config=ScreenConfig(exclude_factors=("report_overdue",)))
    assert outcome(relaxed, "000001.SZ") == "hit"
    assert outcome(relaxed, "000002.SZ") == "excluded"


def test_hit_cap_and_strength_ranking() -> None:
    members = [
        member(f"0000{i:02d}.SZ", listing_id=i, roe=0.15 + 0.01 * i, pe_percentile_5y=0.3)
        for i in range(1, 8)
    ]
    result = screen(members, config=ScreenConfig(hit_cap=3))
    counts = result.counts()
    assert (counts["hit"], counts["truncated"]) == (3, 4)
    ranked = [m for m in result.members if m.hit]
    assert [m.rank for m in ranked] == list(range(1, 8))
    # Higher ROE -> further inside the threshold -> stronger.
    assert [m.ticker for m in ranked[:3]] == ["000007.SZ", "000006.SZ", "000005.SZ"]
    strengths = [m.strength for m in ranked]
    assert strengths == sorted(strengths, reverse=True)  # type: ignore[type-var]
    assert result.to_dict()["truncated"] == 4


def test_strength_ties_break_by_ticker() -> None:
    result = screen([member("000002.SZ", listing_id=2), member("000001.SZ", listing_id=1)])
    assert [m.ticker for m in result.members] == ["000001.SZ", "000002.SZ"]


def test_determinism_is_byte_identical_regardless_of_input_order() -> None:
    members = [
        member(f"0000{i:02d}.SZ", listing_id=i, roe=0.1 + 0.02 * i,
               pe_percentile_5y=0.05 * i, group=("consumer" if i % 3 else "banking"))
        for i in range(1, 12)
    ]
    other = template(id="other_test", applicable_groups=["consumer", "banking"])
    first = run_screen(as_of=AS_OF, members=members, templates=[template(), other],
                       known_factors=KNOWN)
    shuffled = list(members)
    random.Random(7).shuffle(shuffled)
    second = run_screen(as_of=AS_OF, members=shuffled, templates=[other, template()],
                        known_factors=KNOWN)
    assert screen_json(first) == screen_json(second)
    assert [t.template_id for t in first.templates] == ["other_test", "quality_test"]


def test_pending_or_unknown_factor_in_screen_is_rejected() -> None:
    tpl = template().model_copy(
        update={
            "screen": template(
                holding_zones=template_data()["holding_zones"]
            ).holding_zones["add"]
        }
    )
    with pytest.raises(ScreenDefinitionError, match="position_weight"):
        run_screen(as_of=AS_OF, members=[], templates=[tpl], known_factors=KNOWN)


def test_duplicate_members_rejected_and_required_factors() -> None:
    with pytest.raises(ValueError):
        run_screen(as_of=AS_OF, members=[member("000001.SZ"), member("000001.SZ")],
                   templates=[template()], known_factors=KNOWN)
    assert required_factors([template()], ScreenConfig()) == {
        "roe", "pe_percentile_5y", "debt_ratio", "gross_margin", "is_st", "report_overdue"
    }
    with pytest.raises(ValueError):
        ScreenConfig(hit_cap=0)


def test_equality_and_in_operators() -> None:
    tpl = template(
        screen={
            "all": [
                {"factor": "is_st", "op": "==", "value": False},
                {"factor": "roe", "op": "in", "value": [0.2, 0.3]},
                {"factor": "roe", "op": "!=", "value": 0.3},
            ]
        }
    )
    result = screen([member("000001.SZ"), member("000002.SZ", roe=0.3)], [tpl],
                    ScreenConfig(exclude_factors=()))
    assert outcome(result, "000001.SZ") == "hit"
    assert outcome(result, "000002.SZ") == "miss"
    assert result.by_outcome("hit")[0].strength == 0.0  # no comparison margins
