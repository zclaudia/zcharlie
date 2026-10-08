"""Field source registry (P1-A12): loading, validation, coverage and precedence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
import yaml

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.field_sources import (
    DEFAULT_FIELD_SOURCES_PATH,
    FREE_PROVIDERS,
    UNIT_WORDS,
    Calibration,
    FieldSourceError,
    default_field_sources,
    parse_field_sources,
    relative_difference,
)
from investment_agent.factors.view import (
    OBSERVATION,
    TARGET_POOL,
    FinancialPeriod,
    ProviderFields,
    SnapshotView,
    load_snapshot_view,
    resolve_period_fields,
)
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository

FY2024 = date(2024, 12, 31)


def _document() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_FIELD_SOURCES_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Registry file
# --------------------------------------------------------------------------- #


def test_every_factor_input_is_registered_and_no_primary_is_ifind():
    registry = default_field_sources()
    inputs = {item.field for factor in DEFAULT_REGISTRY for item in factor.inputs}
    missing = sorted(name for name in inputs if name not in registry)
    assert not missing, f"factor inputs without a field source entry: {missing}"
    for source in registry:
        assert source.primary != "ifind", source.name
        assert "ifind" not in source.fallback, source.name
        assert source.definition
        assert 0 < source.tolerance <= 1


def test_registry_content_matches_the_coverage_table():
    registry = default_field_sources()
    assert registry.version
    debt = registry.require("debt_ratio")
    assert debt.order == ("baostock", "akshare")
    assert debt.key and debt.sanity["baostock"].minimum == 0.02
    assert debt.sanity_problem("baostock", 0.0019) is not None
    assert debt.sanity_problem("akshare", 0.0019) is None
    for name in ("net_profit_parent", "revenue_total", "capital_expenditure", "free_cash_flow"):
        assert registry.require(name).primary == "akshare", name
    ocf = registry.require("operating_cash_flow")
    assert ocf.order == ("akshare", "baostock") and ocf.is_approx("baostock")
    for name in ("roe", "gross_margin", "net_margin", "eps_ttm", "total_shares"):
        assert registry.require(name).primary == "baostock", name
    # different definitions are never each other's fallback (B1 split)
    assert registry.require("net_profit_parent").fallback == ()
    assert registry.require("revenue_total").fallback == ()
    assert registry.require("eps_basic_fy").formula is not None
    assert {s.table for s in registry} == {
        "financial", "derived", "market", "report_overdue", "dividend"
    }


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"primary": "ifind"}, "iFinD only verifies"),
        ({"fallback": ["ifind"]}, "iFinD only verifies"),
        ({"primary": "wind"}, "unknown source"),
        ({"fallback": ["baostock"]}, "listed twice"),
        ({"tolerance": 0}, "tolerance"),
        ({"sanity": {"akshare": {"min": 1}}}, "not a source"),
        ({"definition": ""}, "definition"),
        ({"table": "nope"}, "table"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_validation_rejects_bad_entries(patch, message):
    document = _document()
    document["fields"]["roe"] = {**document["fields"]["roe"], **patch}
    with pytest.raises(FieldSourceError, match=message):
        parse_field_sources(document)


def test_validation_rejects_bad_formulas():
    document = _document()
    document["fields"]["eps_basic_fy"]["formula"] = "net_profit_parent / close"
    with pytest.raises(FieldSourceError, match="not a registered financial field"):
        parse_field_sources(document)
    document = _document()
    document["fields"]["eps_basic_fy"]["formula"] = "a ^ b"
    with pytest.raises(FieldSourceError, match="formula"):
        parse_field_sources(document)
    document = _document()
    document["fields"]["roe"]["formula"] = "roe / roe"
    with pytest.raises(FieldSourceError, match="only allowed"):
        parse_field_sources(document)


def test_free_providers():
    assert {"baostock", "akshare"} == FREE_PROVIDERS
    assert relative_difference(100.0, 98.0) == pytest.approx(0.02)
    assert relative_difference(0.0, 0.0) == 0.0


# --------------------------------------------------------------------------- #
# Calibration records and unit check (mrd-intake A3)
# --------------------------------------------------------------------------- #


def test_registry_definitions_name_units_and_key_fields_are_calibrated():
    registry = default_field_sources()
    assert registry.version == "2026-10-09.1"
    for source in registry:
        assert any(word in source.definition for word in UNIT_WORDS), source.name
    checked = date(2026, 10, 6)
    for source in registry:
        if not source.key:
            continue
        record = source.calibration.get(source.primary)
        assert record is not None, f"{source.name}: primary {source.primary} has no calibration"
        assert record.checked_on == checked and record.method
    debt = registry.require("debt_ratio")
    assert set(debt.calibration) == {"baostock", "akshare"}
    assert "percent" in debt.calibration["akshare"].method
    assert debt.calibration["baostock"].note == "see sanity.baostock"
    assert registry.require("close").calibration["baostock"] == Calibration(
        checked_on=checked,
        method="query_history_k_data_plus live: 600519 close 1258.62 on 2026-09-30 equals iFinD",
    )
    assert registry.require("gross_margin").calibration == {}


def test_validation_requires_a_unit_word_in_the_definition():
    document = _document()
    document["fields"]["roe"]["definition"] = "return on equity, period-to-date"
    with pytest.raises(FieldSourceError, match="must name the unit"):
        parse_field_sources(document)
    document = _document()
    document["fields"]["roe"]["definition"] = "return on equity, %"
    assert parse_field_sources(document).require("roe").definition == "return on equity, %"


def test_new_provider_entries_need_a_calibration_record():
    document = _document()
    close = document["fields"]["close"]
    close["fallback"] = ["tencent"]
    with pytest.raises(FieldSourceError, match="calibration"):
        parse_field_sources(document)
    close["calibration"] = {
        "tencent": {
            "checked_on": "2026-10-09",
            "method": "qt.gtimg.cn live: field 3 is the close in CNY after 15:00",
            "note": "amount is 万元",
        }
    }
    source = parse_field_sources(document).require("close")
    assert source.order == ("baostock", "tencent")
    assert source.calibration["tencent"].checked_on == date(2026, 10, 9)
    assert source.calibration["tencent"].note == "amount is 万元"
    # a record alone does not legitimise iFinD or a malformed name
    close["fallback"] = ["ifind"]
    close["calibration"] = {"ifind": {"checked_on": "2026-10-09", "method": "x"}}
    with pytest.raises(FieldSourceError, match="iFinD only verifies"):
        parse_field_sources(document)
    close["fallback"] = ["Wind-EDB"]
    close["calibration"] = {"Wind-EDB": {"checked_on": "2026-10-09", "method": "x"}}
    with pytest.raises(FieldSourceError, match="unknown source"):
        parse_field_sources(document)


@pytest.mark.parametrize(
    ("calibration", "message"),
    [
        ({"akshare": {"checked_on": "2026-10-06", "method": "x"}}, "not a source"),
        ({"baostock": {"method": "x"}}, "checked_on is required"),
        ({"baostock": {"checked_on": "yesterday", "method": "x"}}, "ISO date"),
        ({"baostock": {"checked_on": "2026-10-06"}}, "method is required"),
        ({"baostock": {"checked_on": "2026-10-06", "method": "x", "by": "me"}}, "expected checked_on"),
        ("2026-10-06", "expected a mapping"),
    ],
)
def test_validation_rejects_bad_calibration_records(calibration, message):
    document = _document()
    document["fields"]["roe"]["calibration"] = calibration
    with pytest.raises(FieldSourceError, match=message):
        parse_field_sources(document)


# --------------------------------------------------------------------------- #
# Precedence (pure resolver)
# --------------------------------------------------------------------------- #


def _resolve(*candidates: ProviderFields):
    return resolve_period_fields(candidates, anchor="baostock")


def test_suspect_baostock_debt_ratio_falls_back_to_eastmoney():
    fields, sources = _resolve(
        ProviderFields("baostock", {"debt_ratio": 0.0019, "roe": 0.3}, "b:1"),
        ProviderFields("akshare", {"debt_ratio": 0.1904}, "a:1"),
    )
    assert fields["debt_ratio"] == 0.1904
    resolution = sources["debt_ratio"]
    assert resolution.provider == "akshare" and resolution.verification == "unverified"
    assert resolution.rejected[0].provider == "baostock"
    assert resolution.rejected[0].value == 0.0019
    assert "plausible minimum" in resolution.rejected[0].reason
    assert fields["roe"] == 0.3 and sources["roe"].provider == "baostock"


def test_sane_baostock_debt_ratio_is_primary_and_agreement_is_unverified():
    fields, sources = _resolve(
        ProviderFields("baostock", {"debt_ratio": 0.19}, "b:1"),
        ProviderFields("akshare", {"debt_ratio": 0.1904}, "a:1"),
    )
    assert fields["debt_ratio"] == 0.19
    assert sources["debt_ratio"].provider == "baostock"
    assert sources["debt_ratio"].verification == "unverified"
    assert sources["debt_ratio"].conflicts == ()


def test_free_sources_beyond_tolerance_are_a_conflict():
    fields, sources = _resolve(
        ProviderFields("baostock", {"debt_ratio": 0.19}, "b:1"),
        ProviderFields("akshare", {"debt_ratio": 0.25}, "a:1"),
    )
    assert fields["debt_ratio"] == 0.19
    resolution = sources["debt_ratio"]
    assert resolution.verification == "conflict"
    assert resolution.conflicts[0]["provider"] == "akshare"
    assert resolution.conflicts[0]["relative_difference"] == pytest.approx(0.24)


def test_suspect_without_fallback_is_missing_with_the_rejection_kept():
    fields, sources = _resolve(ProviderFields("baostock", {"debt_ratio": 0.0019}, "b:1"))
    assert fields["debt_ratio"] is None
    assert sources["debt_ratio"].provider is None
    assert sources["debt_ratio"].rejected[0].value == 0.0019


def test_verified_ifind_value_wins_and_unverified_ifind_is_ignored():
    baostock = ProviderFields("baostock", {"debt_ratio": 0.19, "roe": 0.30}, "b:1")
    ifind = ProviderFields(
        "ifind",
        {"debt_ratio": 0.2, "roe": 0.5},
        "i:1",
        verification={"debt_ratio": "verified"},
    )
    fields, sources = _resolve(baostock, ifind)
    assert fields["debt_ratio"] == 0.2
    assert sources["debt_ratio"].provider == "ifind"
    assert sources["debt_ratio"].verification == "verified"
    assert fields["roe"] == 0.30 and sources["roe"].provider == "baostock"


def test_ifind_conflict_wins_and_recorded_conflicts_mark_the_free_value():
    fields, sources = _resolve(
        ProviderFields("baostock", {"roe": 0.30}, "b:1"),
        ProviderFields("ifind", {"roe": 0.5}, "i:1", verification={"roe": "conflict"}),
        ProviderFields(
            "akshare",
            {"operating_cash_flow": 10.0},
            "a:1",
            recorded_conflicts={"operating_cash_flow": {"baostock": 8.0, "akshare": 10.0}},
        ),
    )
    # D6 rev 2: a verified conflict means iFinD wins; the free value stays on record.
    assert fields["roe"] == 0.5
    assert sources["roe"].provider == "ifind" and sources["roe"].verification == "conflict"
    assert [c["provider"] for c in sources["roe"].conflicts] == ["baostock"]
    assert sources["roe"].conflicts[0]["value"] == 0.30
    assert sources["operating_cash_flow"].verification == "conflict"


def test_baostock_operating_cash_flow_is_an_approx_fallback():
    fields, sources = _resolve(
        ProviderFields("baostock", {"cfo_to_np": 1.5, "net_profit_total": 100.0}, "b:1")
    )
    assert fields["operating_cash_flow"] == pytest.approx(150.0)
    assert sources["operating_cash_flow"].provider == "baostock"
    assert sources["operating_cash_flow"].approx is True
    # Eastmoney wins when present; the approx baostock value never raises a conflict.
    fields, sources = _resolve(
        ProviderFields("baostock", {"cfo_to_np": 1.5, "net_profit_total": 100.0}, "b:1"),
        ProviderFields("akshare", {"operating_cash_flow": 120.0}, "a:1"),
    )
    assert fields["operating_cash_flow"] == 120.0
    assert sources["operating_cash_flow"].verification == "unverified"


def test_derived_per_share_fields():
    fields, sources = _resolve(
        ProviderFields("baostock", {"total_shares": 10.0}, "b:1"),
        ProviderFields("akshare", {"net_profit_parent": 50.0, "revenue_total": 200.0}, "a:1"),
    )
    assert fields["eps_basic_fy"] == 5.0 and fields["revenue_ps_fy"] == 20.0
    assert "bvps_fy" not in fields  # total_equity missing
    assert sources["eps_basic_fy"].provider == "derived" and sources["eps_basic_fy"].approx


# --------------------------------------------------------------------------- #
# Factor library on resolved views
# --------------------------------------------------------------------------- #


def _period(fields, sources=None, **extra) -> FinancialPeriod:
    return FinancialPeriod(
        report_period=FY2024,
        period_kind="annual",
        available_from=date(2025, 4, 3),
        availability="actual",
        fields=fields,
        ref="fixture",
        sources=sources or {},
        **extra,
    )


def test_debt_ratio_factor_uses_the_fallback_and_carries_provenance():
    fields, sources = _resolve(
        ProviderFields("baostock", {"debt_ratio": 0.0019}, "b:1"),
        ProviderFields("akshare", {"debt_ratio": 0.1904}, "a:1"),
    )
    view = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2025, 6, 30),
        policy=TARGET_POOL,
        industry_code="C15",
        industry_name="酒、饮料和精制茶制造业",
        periods=[_period(fields, sources)],
    )
    value = DEFAULT_REGISTRY.get("debt_ratio").compute(view)
    assert value.value == 0.1904 and value.quality == "ok"
    assert "provider_fallback" in value.flags
    assert value.inputs_ref["provider_value"] == 0.0019
    source = value.inputs_ref["financial"][0]["sources"]["debt_ratio"]
    assert source["provider"] == "akshare" and source["verification"] == "unverified"
    assert source["rejected"][0]["provider"] == "baostock"


def test_pure_target_pool_periods_get_the_registry_sanity_rule():
    view = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2025, 6, 30),
        policy=TARGET_POOL,
        periods=[_period({"debt_ratio": 0.0019, "roe": 0.3})],
    )
    value = DEFAULT_REGISTRY.get("debt_ratio").compute(view)
    assert value.value is None and "suspect_provider_value" in value.flags
    roe = DEFAULT_REGISTRY.get("roe").compute(view)
    assert roe.inputs_ref["financial"][0]["sources"]["roe"]["provider"] == "baostock"


def test_observation_periods_are_not_resolved():
    view = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2025, 6, 30),
        policy=OBSERVATION,
        periods=[_period({"debt_ratio": 0.0019})],
    )
    assert view.latest_annual().sources == {}
    assert DEFAULT_REGISTRY.get("debt_ratio").compute(view).value == 0.0019


def test_free_cash_flow_on_the_target_pool():
    fields, sources = _resolve(
        ProviderFields("baostock", {"roe": 0.3}, "b:1"),
        ProviderFields(
            "akshare",
            {"operating_cash_flow": 100.0, "capital_expenditure": 30.0, "free_cash_flow": 70.0},
            "a:1",
        ),
    )
    view = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2025, 6, 30),
        policy=TARGET_POOL,
        periods=[_period(fields, sources)],
    )
    value = DEFAULT_REGISTRY.get("free_cash_flow").compute(view)
    assert value.value == 70.0 and value.quality == "ok"
    refs = value.inputs_ref["financial"][0]["sources"]
    assert refs["capital_expenditure"]["provider"] == "akshare"


# --------------------------------------------------------------------------- #
# Database view
# --------------------------------------------------------------------------- #


async def test_load_snapshot_view_resolves_across_providers(db_session):
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "600519.SH", company_name="贵州茅台"
    )
    repo = FinancialSnapshotRepository(db_session)
    captured = datetime(2026, 5, 1, tzinfo=UTC)

    async def insert(period, provider, fields, available_from, **kwargs):
        await repo.insert(
            listing_id=listing.id,
            report_period=period,
            period_kind="annual",
            fields=fields,
            provider=provider,
            captured_at=kwargs.pop("captured_at", captured),
            availability=kwargs.pop("availability", "actual"),
            available_from=available_from,
            is_backfill=kwargs.pop("is_backfill", True),
        )

    fy2023 = date(2023, 12, 31)
    await insert(fy2023, "baostock", {"debt_ratio": 0.2, "roe": 0.3}, date(2024, 4, 3))
    await insert(FY2024, "baostock", {"debt_ratio": 0.0019, "roe": 0.38}, date(2025, 4, 3))
    await insert(
        FY2024,
        "akshare",
        {"part": "summary", "debt_ratio": 0.1904, "net_profit_parent": 86.2,
         "operating_cash_flow": 92.0},
        date(2025, 4, 3),
    )
    await insert(
        FY2024,
        "akshare",
        {"part": "cash_flow", "operating_cash_flow": 92.4, "capital_expenditure": 4.7,
         "free_cash_flow": 87.7, "restated": True},
        date(2025, 4, 3),
        captured_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    # An older summary version of FY2024 (superseded) and an Eastmoney-only period.
    await insert(fy2023, "akshare", {"part": "summary", "debt_ratio": 0.21}, date(2024, 4, 3))
    await insert(
        date(2025, 12, 31), "akshare", {"part": "summary", "debt_ratio": 0.17},
        date(2026, 4, 17), availability="approx",
    )
    # A verified iFinD ROE and an unverified iFinD debt ratio.
    await insert(
        FY2024,
        "ifind",
        {"roe": 0.385, "debt_ratio": 0.5, "verification": {"roe": "verified"}},
        date(2025, 4, 3),
        availability="approx",
    )
    await db_session.flush()

    view = await load_snapshot_view(db_session, "600519.SH", date(2026, 5, 6))
    assert [p.report_period for p in view.periods] == [fy2023, FY2024]  # baostock anchors
    period = view.latest_annual()
    assert period.fields["debt_ratio"] == 0.1904
    assert period.source("debt_ratio").provider == "akshare"
    assert period.fields["roe"] == 0.385 and period.source("roe").verification == "verified"
    # both Eastmoney parts merge; the later cash-flow capture wins operating cash flow
    assert period.fields["operating_cash_flow"] == 92.4
    assert period.fields["capital_expenditure"] == 4.7
    assert period.fields["net_profit_parent"] == 86.2
    fcf = DEFAULT_REGISTRY.get("free_cash_flow").compute(view)
    assert fcf.value == pytest.approx(92.4 - 4.7)
    debt = DEFAULT_REGISTRY.get("debt_ratio").compute(view)
    assert debt.value == 0.1904 and debt.inputs_ref["provider_value"] == 0.0019
    # The Eastmoney row is not public before its own available_from.
    early = await load_snapshot_view(db_session, "600519.SH", date(2025, 4, 2))
    assert early.latest_annual().report_period == fy2023
    fy2023_period = early.latest_annual()
    assert fy2023_period.fields["debt_ratio"] == 0.2  # sane baostock stays primary
    assert fy2023_period.source("debt_ratio").verification == "conflict"  # 0.21 is > 2% off
