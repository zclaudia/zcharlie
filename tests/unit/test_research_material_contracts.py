"""Frozen material contract: the eight validation rules, hash stability and
the ``fact_to_payload`` / ``fact_from_payload`` round trip (plan 02 T2.5)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from investment_agent.materials.contracts import (
    FACT_ENTRY_KEYS,
    FACT_METRICS,
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    ParseResult,
    PeriodBasis,
    PerShareBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    content_hash,
    fact_from_payload,
    fact_to_payload,
    payload_facts,
    validate_draft,
)

CAPTURED_AT = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)
FETCHED_AT = datetime(2026, 10, 12, 3, 29, 12, tzinfo=UTC)


# The plan's 12 frozen keys; FACT_ENTRY_KEYS adds the two FactRecord fields
# (conflict_note, source_version) that have no nested block to live in.
PLAN_ENTRY_KEYS = frozenset(
    {
        "metric",
        "period",
        "semantics",
        "value",
        "provenance",
        "verification",
        "conflict",
        "applicability",
        "source_label",
        "source_field",
        "original_locator",
        "missing_reason",
    }
)


def annual_period(*, days: int | None = None, start: date | None = None) -> FactPeriod:
    return FactPeriod(
        start=start if start is not None else date(2023, 10, 1),
        end=date(2024, 9, 29),
        basis=PeriodBasis.ANNUAL,
        label="FY2024",
        days=days,
    )


def make_value(**overrides: Any) -> FactValue:
    fields: dict[str, Any] = {
        "raw_value": "714724000000",
        "raw_unit": "CNY",
        "scale": 1,
        "unit": "CNY",
        "currency": "CNY",
        "sign_convention": "as_reported",
        "missing_reason": None,
    }
    fields.update(overrides)
    return FactValue(**fields)


def make_fact(**overrides: Any) -> FactRecord:
    metric = overrides.pop("metric", "revenue_operating")
    fields: dict[str, Any] = {
        "metric": metric,
        "period": annual_period(),
        "semantics": FactSemantics(
            metric=metric,
            source_label="营业额",
            source_field="OPERATE_INCOME",
            accounting_standard=AccountingStandard.HKFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.UNKNOWN,
            per_share_basis=PerShareBasis.NA,
        ),
        "value": make_value(),
        "provenance": FactProvenance(
            source="eastmoney_hk",
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=date(2025, 3, 19),
            source_published_precision=DatePrecision.DATE,
            accepted_at=None,
            source_available_at=None,
        ),
        "verification": Verification.CANDIDATE,
        "conflict": "none",
        "applicability": "applicable",
    }
    fields.update(overrides)
    return FactRecord(**fields)


def make_draft(**overrides: Any) -> DatasetDraft:
    facts = overrides.pop("facts", None)
    extra = overrides.pop("payload_extra", None)
    payload: dict[str, Any] = {
        "facts": [fact_to_payload(fact) for fact in (facts if facts is not None else [make_fact()])]
    }
    if extra:
        payload.update(extra)
    fields: dict[str, Any] = {
        "kind": DatasetKind.ANNUAL_FACTS,
        "subject_kind": SubjectKind.COMPANY,
        "subject_id": 7,
        "source": "eastmoney_hk",
        "schema_version": SCHEMA_VERSION,
        "adapter_version": "hk-native-1",
        "captured_at": CAPTURED_AT,
        "payload": payload,
        "period": annual_period(),
        "raw_bytes": b'{"result": {"data": []}}',
        "raw_media_type": "application/json",
        "raw_source_url": "https://example.invalid/report",
        "source_fetched_at": FETCHED_AT,
    }
    fields.update(overrides)
    return DatasetDraft(**fields)


def payload_of(*facts: FactRecord, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"facts": [fact_to_payload(fact) for fact in facts]}
    if extra:
        payload.update(extra)
    return payload


def reorder(value: Any) -> Any:
    """Recursively rebuild mappings with reversed key insertion order."""

    if isinstance(value, dict):
        return {key: reorder(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reorder(item) for item in value]
    return value


def problems_for_fact(**overrides: Any) -> tuple[str, ...]:
    return validate_draft(make_draft(facts=[make_fact(**overrides)]))


# --- frozen surfaces -------------------------------------------------------


def test_fact_metrics_are_frozen() -> None:
    assert (
        frozenset(
            {
                "revenue_operating",
                "revenue_gross",
                "revenue_net_credit",
                "credit_loss_expense",
                "cost_of_revenue",
                "net_profit",
                "profit_parent",
                "profit_ordinary_shareholders",
                "profit_other_equity_holders",
                "total_assets",
                "total_liabilities",
                "total_equity",
                "equity_parent",
                "equity_ordinary",
                "operating_cash_flow",
                "capex_ppe",
                "eps_basic",
                "eps_diluted",
                "shares_weighted_basic",
                "shares_outstanding_end",
            }
        )
        == FACT_METRICS
    )


def test_fact_entry_keys_cover_the_plan_keys_and_the_record_fields() -> None:
    assert PLAN_ENTRY_KEYS < FACT_ENTRY_KEYS
    assert {"conflict_note", "source_version"} == FACT_ENTRY_KEYS - PLAN_ENTRY_KEYS


def test_contract_dataclasses_are_frozen() -> None:
    fact = make_fact()
    with pytest.raises(FrozenInstanceError):
        fact.metric = "net_profit"  # type: ignore[misc]
    draft = make_draft()
    with pytest.raises(FrozenInstanceError):
        draft.subject_id = 8  # type: ignore[misc]
    assert SCHEMA_VERSION == 1


def test_adapter_carriers_default_and_hold_bytes() -> None:
    request = UpstreamRequest(name="income", url="https://example.invalid/income")
    assert request.params == () and request.media_type == "application/json"
    raw = RawPayload(
        name="income",
        content=b"{}",
        media_type="application/json",
        fetched_at=FETCHED_AT,
        source_url=request.url,
        http_status=200,
    )
    result = ParseResult(drafts=(make_draft(),), problems=("unmapped label: 其他收入",))
    assert raw.content == b"{}" and result.problems == ("unmapped label: 其他收入",)


# --- rule 1: captured_at is real and timezone-aware ------------------------


def test_valid_annual_draft_passes() -> None:
    assert validate_draft(make_draft()) == ()
    # No raw artifact and a period-less company-level filing index are legal.
    assert (
        validate_draft(make_draft(raw_bytes=None, raw_media_type=None, raw_source_url=None)) == ()
    )
    filing = make_draft(
        facts=[],
        kind=DatasetKind.FILING_INDEX,
        period=None,
        payload_extra={"latest_accession": "0000320193-24-000123", "unmapped": 0},
    )
    assert validate_draft(filing) == ()


def test_captured_at_must_be_timezone_aware() -> None:
    naive = datetime(2026, 10, 12, 3, 30)
    problems = validate_draft(make_draft(captured_at=naive))
    assert any("captured_at" in problem and "timezone-aware" in problem for problem in problems)
    problems = validate_draft(make_draft(captured_at=None))
    assert any("captured_at" in problem for problem in problems)
    problems = validate_draft(make_draft(source_fetched_at=naive))
    assert any("source_fetched_at" in problem for problem in problems)


# --- rule 2: metrics are from the frozen set ------------------------------


def test_metric_outside_fact_metrics_is_rejected() -> None:
    problems = problems_for_fact(metric="revenue")
    assert any("FACT_METRICS" in problem and "'revenue'" in problem for problem in problems)


# --- rule 3: units and currency -------------------------------------------


@pytest.mark.parametrize(
    ("value_overrides", "expected"),
    [
        ({"unit": "USD", "currency": None}, "requires currency 'USD'"),
        ({"unit": "HKD", "currency": "CNY"}, "requires currency 'HKD'"),
        ({"unit": "USD/share", "currency": None}, "requires a currency"),
        ({"unit": "USD/shares", "currency": "HKD"}, "requires currency 'USD'"),
        ({"unit": "ratio", "currency": "CNY"}, "must not carry a currency"),
        ({"unit": "shares", "currency": "HKD"}, "must not carry a currency"),
        ({"unit": "percent", "currency": "USD"}, "must not carry a currency"),
    ],
)
def test_unit_currency_violations(value_overrides: dict[str, Any], expected: str) -> None:
    problems = problems_for_fact(value=make_value(**value_overrides))
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    "value_overrides",
    [
        {"unit": "USD", "currency": "USD"},
        {"unit": "HKD/share", "currency": "HKD"},
        {"unit": "CNY/shares", "currency": "CNY"},
        {"unit": "ratio", "currency": None},
        {"unit": "shares", "currency": None},
        {"unit": "percent", "currency": None},
    ],
)
def test_unit_currency_accepted(value_overrides: dict[str, Any]) -> None:
    assert validate_draft(make_draft(facts=[make_fact(value=make_value(**value_overrides))])) == ()


# --- rule 4: missing is never zero ----------------------------------------


def test_missing_requires_reason_and_known_value_must_not_carry_one() -> None:
    missing = make_value(raw_value=None, missing_reason=None)
    problems = problems_for_fact(value=missing)
    assert any("missing_reason is required" in problem for problem in problems)

    assert problems_for_fact(value=make_value(raw_value=None, missing_reason="not_disclosed")) == ()

    problems = problems_for_fact(value=make_value(missing_reason="not_disclosed"))
    assert any("missing_reason must be null" in problem for problem in problems)


# --- rule 5: real period lengths ------------------------------------------


def test_instant_basis_requires_null_start() -> None:
    instant = FactPeriod(start=None, end=date(2024, 9, 30), basis=PeriodBasis.INSTANT, days=None)
    assert problems_for_fact(period=instant, metric="total_assets") == ()
    with_start = replace(instant, start=date(2024, 9, 30))
    problems = problems_for_fact(period=with_start, metric="total_assets")
    assert any("must be null for instant basis" in problem for problem in problems)


def test_annual_basis_needs_a_start() -> None:
    problems = problems_for_fact(period=replace(annual_period(), start=None))
    assert any("start is required for annual basis" in problem for problem in problems)


@pytest.mark.parametrize(
    ("span", "accepted"),
    [
        (371, True),  # 53-week fiscal year (AAPL FY2023 shape)
        (364, True),  # 52-week fiscal year (AAPL FY2022 shape)
        (365, True),
        (400, True),
        (330, True),
        (329, False),
        (300, False),
        (401, False),
    ],
)
def test_annual_span_accepts_52_53_week_years_and_rejects_others(span: int, accepted: bool) -> None:
    end = date(2023, 10, 8)
    period = FactPeriod(
        start=end - timedelta(days=span),
        end=end,
        basis=PeriodBasis.ANNUAL,
        label="FY2023",
        days=span,
    )
    problems = problems_for_fact(period=period)
    if accepted:
        assert problems == ()
    else:
        assert any("annual span must be 330..400 days" in problem for problem in problems)


def test_period_days_must_match_the_real_span() -> None:
    problems = problems_for_fact(period=replace(annual_period(), days=365))
    assert any("disagrees with the actual span 364" in problem for problem in problems)


# --- rule 6: publication time precision -----------------------------------


def test_unknown_precision_keeps_both_published_fields_null() -> None:
    provenance = FactProvenance(
        source="eastmoney_hk",
        accession=None,
        original_hash=None,
        source_published_at=datetime(2025, 3, 19, 8, 0, tzinfo=UTC),
        source_published_date=None,
        source_published_precision=DatePrecision.UNKNOWN,
        accepted_at=None,
        source_available_at=None,
    )
    problems = problems_for_fact(provenance=provenance)
    assert any("unknown date precision requires both published fields" in p for p in problems)

    empty = FactProvenance(
        source="eastmoney_hk",
        accession=None,
        original_hash=None,
        source_published_at=None,
        source_published_date=None,
        source_published_precision=DatePrecision.DATE,
        accepted_at=None,
        source_available_at=None,
    )
    problems = problems_for_fact(provenance=empty)
    assert any("requires source_published_at or source_published_date" in p for p in problems)

    timestamp = replace(empty, source_published_at=datetime(2025, 3, 19, 8, 0, tzinfo=UTC))
    assert (
        problems_for_fact(
            provenance=replace(timestamp, source_published_precision=DatePrecision.TIMESTAMP)
        )
        == ()
    )


def test_provenance_timestamps_must_be_timezone_aware() -> None:
    provenance = replace(
        make_fact().provenance,
        source_published_precision=DatePrecision.TIMESTAMP,
        source_published_at=datetime(2025, 3, 19, 8, 0),
        source_published_date=None,
    )
    problems = problems_for_fact(provenance=provenance)
    assert any("source_published_at must be timezone-aware" in problem for problem in problems)


# --- rule 7: conflicts stay visible ---------------------------------------


def test_unresolved_conflict_requires_a_note() -> None:
    problems = problems_for_fact(conflict="unresolved")
    assert any("conflict_note is required" in problem for problem in problems)
    assert problems_for_fact(conflict="unresolved", conflict_note="原值与修订值不同") == ()
    assert problems_for_fact(conflict="resolved", conflict_note="采用修订值") == ()


# --- rule 8: amounts are decimal strings ----------------------------------


@pytest.mark.parametrize(
    "raw_value",
    [1.5, 150, b"1.5", "", "   ", "abc", "NaN", "Infinity", "1_000", " 12.5"],
)
def test_raw_value_must_be_a_plain_decimal_string(raw_value: object) -> None:
    problems = problems_for_fact(value=make_value(raw_value=raw_value))
    assert any("raw_value" in problem for problem in problems), problems


@pytest.mark.parametrize("raw_value", ["0", "-12.5", "1234567.89", "+3", "0.000001"])
def test_plain_decimal_strings_are_accepted(raw_value: str) -> None:
    assert (
        validate_draft(make_draft(facts=[make_fact(value=make_value(raw_value=raw_value))])) == ()
    )


# --- content hash ---------------------------------------------------------


def test_content_hash_is_stable_and_ignores_acquisition_times() -> None:
    draft = make_draft()
    assert content_hash(draft) == content_hash(make_draft())
    assert content_hash(draft) == content_hash(
        make_draft(
            captured_at=CAPTURED_AT + timedelta(hours=5),
            source_fetched_at=None,
        )
    )


def test_content_hash_ignores_payload_key_order() -> None:
    payload = payload_of(make_fact(), extra={"unmapped": 2, "source_item_code": "001"})
    reordered = reorder(payload)
    assert list(reordered) != list(payload)
    assert content_hash(make_draft(payload=payload, facts=None)) == content_hash(
        make_draft(payload=reordered, facts=None)
    )


def test_content_hash_changes_with_content() -> None:
    base = make_draft()
    other_value = make_draft(facts=[make_fact(value=make_value(raw_value="714724000001"))])
    assert content_hash(base) != content_hash(other_value)
    assert content_hash(base) != content_hash(make_draft(raw_bytes=b'{"result": {"data": [1]}}'))
    assert content_hash(base) != content_hash(make_draft(raw_bytes=None))
    assert content_hash(base) != content_hash(
        make_draft(payload_extra={"source_item_code": "002"}, facts=None)
    )


# --- payload entry shape and round trip -----------------------------------


def test_payload_entries_key_set_is_exact() -> None:
    draft = make_draft()
    entry = dict(payload_facts(draft.payload)[0])
    entry["extra_key"] = 1
    problems = validate_draft(make_draft(payload=payload_of() | {"facts": [entry]}))
    assert any(
        "keys must equal FACT_ENTRY_KEYS" in problem and "extra_key" in problem
        for problem in problems
    )

    missing = dict(payload_facts(make_draft().payload)[0])
    del missing["source_version"]
    problems = validate_draft(make_draft(payload=payload_of() | {"facts": [missing]}))
    assert any("source_version" in problem and "missing=" in problem for problem in problems)


def test_mirror_fields_must_agree_with_the_nested_blocks() -> None:
    entry = dict(payload_facts(make_draft().payload)[0])
    entry["source_label"] = "另一标签"
    problems = validate_draft(make_draft(payload=payload_of() | {"facts": [entry]}))
    assert any("source_label disagrees" in problem for problem in problems)


def test_fact_round_trip_for_instant_missing_and_unknown_precision() -> None:
    cases = [
        make_fact(
            metric="total_assets",
            period=FactPeriod(
                start=None, end=date(2024, 9, 30), basis=PeriodBasis.INSTANT, days=None
            ),
        ),
        make_fact(
            metric="operating_cash_flow",
            value=make_value(
                raw_value=None, raw_unit="CNY", unit="CNY", missing_reason="not_disclosed"
            ),
        ),
        make_fact(
            metric="eps_basic",
            value=make_value(raw_value="1.23", raw_unit="HKD", unit="HKD/share", currency="HKD"),
            provenance=replace(
                make_fact().provenance,
                source_published_at=None,
                source_published_date=None,
                source_published_precision=DatePrecision.UNKNOWN,
            ),
        ),
    ]
    for fact in cases:
        entry = fact_to_payload(fact)
        assert json.loads(json.dumps(entry)) == entry  # stays JSON-clean
        assert fact_from_payload(entry) == fact
        assert fact_to_payload(fact_from_payload(entry)) == entry


def test_fact_payload_overrides_labels_and_locator() -> None:
    fact = make_fact()
    entry = fact_to_payload(
        fact, source_label="营业收入", source_field="OPERATE_INCOME", original_locator="p.12 表1"
    )
    assert entry["source_label"] == "营业收入"
    assert entry["source_field"] == "OPERATE_INCOME"
    assert entry["original_locator"] == "p.12 表1"
    restored = fact_from_payload(entry)
    assert restored.semantics.source_label == "营业收入"
    assert restored.original_locator == "p.12 表1"
    assert restored.value == fact.value
    assert fact_to_payload(restored) == entry


def test_conflict_note_and_source_version_round_trip() -> None:
    fact = make_fact(
        conflict="unresolved",
        conflict_note="原始 100 与修订 90 冲突",
        source_version="0000320193-24-000123",
    )
    entry = fact_to_payload(fact)
    assert entry["conflict_note"] == "原始 100 与修订 90 冲突"
    assert entry["source_version"] == "0000320193-24-000123"
    assert fact_from_payload(entry) == fact


def test_malformed_entries_are_reported_not_raised() -> None:
    payload = payload_of() | {"facts": ["not-an-object"]}
    problems = validate_draft(make_draft(payload=payload))
    assert any(
        "must be a list" in problem or "must be an object" in problem for problem in problems
    )

    bad_value = dict(payload_facts(make_draft().payload)[0])
    bad_value["value"] = {"raw_value": 1.5}
    problems = validate_draft(make_draft(payload=payload_of() | {"facts": [bad_value]}))
    assert problems


def test_payload_facts_raises_on_missing_or_wrong_shape() -> None:
    with pytest.raises(ValueError, match="'facts'"):
        payload_facts({"source_item_code": "001"})
    with pytest.raises(ValueError, match="'facts'"):
        payload_facts({"facts": {"metric": "revenue_operating"}})
    with pytest.raises(ValueError, match="must be an object"):
        payload_facts({"facts": [42]})
    with pytest.raises(ValueError, match="object"):
        payload_facts([("facts", [])])  # type: ignore[arg-type]

    assert payload_facts({"facts": [], "unmapped": 3}) == ()
    assert len(payload_facts(payload_of(make_fact()))) == 1


def test_payload_must_be_canonical_json() -> None:
    problems = validate_draft(make_draft(payload={"facts": [], "bad": object()}))
    assert any("canonical-JSON" in problem for problem in problems)
    problems = validate_draft(make_draft(payload={"facts": [], "nan": float("nan")}))
    assert any("canonical-JSON" in problem for problem in problems)
