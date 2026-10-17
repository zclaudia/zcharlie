from datetime import date

from investment_agent.config import Settings
from investment_agent.domain.security import ControlledIdentity, SecurityType, SourceBinding
from investment_agent.materials.source_policy import evaluate_source_policy
from tests.support.material_source_policy import synthetic_approved_source_policy

SOURCE = "eastmoney.hk.native.v1"


def _identity() -> ControlledIdentity:
    return ControlledIdentity(
        market_code="HKEX",
        ticker="00005",
        company_key="hsbc-holdings",
        company_name="HSBC Holdings plc",
        company_name_zh="汇丰控股",
        security_type=SecurityType.ORDINARY,
        quote_currency="HKD",
        report_currency="USD",
        fiscal_year_end_note="December",
        sources=(SourceBinding(source=SOURCE, symbol="00005.HK"),),
        valid_from=date(2020, 1, 1),
    )


def test_default_policy_fails_closed_despite_enabled_ops_setting() -> None:
    settings = Settings(api_key="test", materials_hk_refresh_enabled=True)

    decision = evaluate_source_policy(
        SOURCE,
        _identity(),
        settings=settings,
        adapter_version="1",
        global_enabled=True,
    )

    assert decision.enabled is False
    assert decision.bound is True
    assert decision.licensing == "unknown"
    assert decision.technical_acceptance == "not_accepted"
    assert "licensing_unknown" in decision.reason_codes
    assert "technical_acceptance_missing_or_mismatched" in decision.reason_codes


def test_injected_synthetic_approval_requires_ops_and_global_switches() -> None:
    identity = _identity()
    approvals = synthetic_approved_source_policy(SOURCE, identity, "adapter-2")
    settings = Settings(api_key="test", materials_hk_refresh_enabled=True)

    allowed = evaluate_source_policy(
        SOURCE,
        identity,
        settings=settings,
        adapter_version="adapter-2",
        global_enabled=True,
        approvals=approvals,
    )
    globally_disabled = evaluate_source_policy(
        SOURCE,
        identity,
        settings=settings,
        adapter_version="adapter-2",
        global_enabled=False,
        approvals=approvals,
    )
    wrong_adapter = evaluate_source_policy(
        SOURCE,
        identity,
        settings=settings,
        adapter_version="adapter-3",
        global_enabled=True,
        approvals=approvals,
    )

    assert allowed.enabled is True
    assert allowed.as_dict()["reason_codes"] == []
    assert "global_refresh_disabled" in globally_disabled.reason_codes
    assert "technical_acceptance_missing_or_mismatched" in wrong_adapter.reason_codes


def test_license_scope_and_unknown_source_are_rejected() -> None:
    identity = _identity()
    approvals = synthetic_approved_source_policy(SOURCE, identity)
    record = approvals[SOURCE]
    assert isinstance(record, dict)
    licensing = record["licensing"]
    assert isinstance(licensing, dict)
    licensing["company_keys"] = ["other-company"]
    settings = Settings(api_key="test", materials_hk_refresh_enabled=True)

    mismatch = evaluate_source_policy(
        SOURCE,
        identity,
        settings=settings,
        adapter_version="synthetic-1",
        global_enabled=True,
        approvals=approvals,
    )
    unknown = evaluate_source_policy(
        "unregistered.source.v1",
        identity,
        settings=settings,
        adapter_version="synthetic-1",
        global_enabled=True,
        approvals=approvals,
    )

    assert "licensing_scope_mismatch" in mismatch.reason_codes
    assert "unknown_source" in unknown.reason_codes
    assert "identity_not_bound" in unknown.reason_codes


def test_approval_citations_are_required_and_reason_is_a_single_code() -> None:
    identity = _identity()
    approvals = synthetic_approved_source_policy(SOURCE, identity)
    record = approvals[SOURCE]
    assert isinstance(record, dict)
    licensing = record["licensing"]
    assert isinstance(licensing, dict)
    licensing["citation"] = ""

    decision = evaluate_source_policy(
        SOURCE,
        identity,
        settings=Settings(api_key="test", materials_hk_refresh_enabled=True),
        adapter_version="synthetic-1",
        global_enabled=False,
        approvals=approvals,
    )

    assert decision.enabled is False
    assert decision.licensing == "not_approved"
    assert decision.reason == "licensing_citation_missing_or_invalid"
    assert len(decision.reason_codes) > 1


def test_invalid_and_future_report_dates_fail_technical_acceptance() -> None:
    identity = _identity()
    approvals = synthetic_approved_source_policy(SOURCE, identity)
    record = approvals[SOURCE]
    assert isinstance(record, dict)
    technical = record["technical_acceptance"]
    assert isinstance(technical, dict)
    reports = technical["reports"]
    assert isinstance(reports, list)
    reports[0]["observed_on"] = "2026-02-30"
    reports[1]["observed_on"] = "2999-01-01"

    decision = evaluate_source_policy(
        SOURCE,
        identity,
        settings=Settings(api_key="test", materials_hk_refresh_enabled=True),
        adapter_version="synthetic-1",
        global_enabled=True,
        approvals=approvals,
    )

    assert decision.enabled is False
    assert decision.technical_acceptance == "not_accepted"
    assert "technical_report_dates_invalid_or_insufficient" in decision.reason_codes


def test_technical_acceptance_is_limited_to_its_company_scope() -> None:
    identity = _identity()
    approvals = synthetic_approved_source_policy(SOURCE, identity)
    record = approvals[SOURCE]
    assert isinstance(record, dict)
    technical = record["technical_acceptance"]
    assert isinstance(technical, dict)
    technical["company_keys"] = ["another-company"]

    decision = evaluate_source_policy(
        SOURCE,
        identity,
        settings=Settings(api_key="test", materials_hk_refresh_enabled=True),
        adapter_version="synthetic-1",
        global_enabled=True,
        approvals=approvals,
    )

    assert decision.enabled is False
    assert decision.technical_acceptance == "scope_mismatch"
    assert "technical_scope_mismatch" in decision.reason_codes


def test_unknown_source_cannot_be_approved_by_injection() -> None:
    identity = _identity()
    source = "unknown.synthetic.v1"
    approvals = synthetic_approved_source_policy(source, identity)

    decision = evaluate_source_policy(
        source,
        identity,
        settings=Settings(api_key="test", materials_hk_refresh_enabled=True),
        adapter_version="synthetic-1",
        global_enabled=True,
        approvals=approvals,
    )

    assert decision.enabled is False
    assert decision.reason == "unknown_source"
    assert "unknown_source" in decision.reason_codes


def test_invalid_policy_metadata_fails_closed(monkeypatch) -> None:
    from investment_agent.materials import source_policy

    monkeypatch.setattr(
        source_policy,
        "load_source_policy",
        lambda: {"schema_version": 2, "revision": "INVALID REVISION", "sources": {SOURCE: {}}},
    )

    decision = evaluate_source_policy(
        SOURCE,
        _identity(),
        settings=Settings(api_key="test", materials_hk_refresh_enabled=True),
        adapter_version="anything",
        global_enabled=True,
    )

    assert decision.enabled is False
    assert decision.reason == "invalid_policy_schema"
    assert decision.policy_revision.startswith("INVALID REVISION@")
