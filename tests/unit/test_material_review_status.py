"""Unit checks for the reviewed-material receipt gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime

from investment_agent.materials.context import (
    _current_versions,
    _fact_view,
    _iso_date,
    _valid_review_envelope,
)
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    DatasetKind,
    FactPeriod,
    PeriodBasis,
    SubjectKind,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.evidence import dataset_facts
from investment_agent.storage.models.research_material import ResearchDataset
from tests.integration.test_api_research_materials import fixture_facts


def _reviewed_dataset(
    *,
    checked_on: str = "2026-10-15",
    candidate: bool = False,
    years: tuple[str, ...] = ("FY2024",),
    include_opening: bool = False,
) -> ResearchDataset:
    facts = tuple(fact for year in years for fact in fixture_facts("apple", year))
    if include_opening:
        equity = next(fact for fact in facts if fact.metric in {"total_equity", "equity_parent"})
        facts = (
            *facts,
            replace(
                equity,
                period=FactPeriod(
                    start=None,
                    end=date(2020, 9, 26),
                    basis=PeriodBasis.INSTANT,
                    label="FY2020 opening",
                ),
            ),
        )
    verification = Verification.CANDIDATE if candidate else Verification.VERIFIED
    facts = tuple(replace(fact, verification=verification) for fact in facts)
    payload_facts = [fact_to_payload(fact) for fact in facts]
    latest_end = max(fact.period.end for fact in facts)
    latest_start = next(
        fact.period.start for fact in facts
        if fact.period.end == latest_end and fact.period.start is not None
    )
    digests = [
        hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for item in payload_facts
    ]
    dataset = ResearchDataset(
        kind=DatasetKind.ANNUAL_FACTS.value,
        subject_kind=SubjectKind.COMPANY.value,
        subject_id=11,
        source="issuer.reviewed_annual.v1",
        schema_version=SCHEMA_VERSION,
        adapter_version="reviewed-annual-1",
        period_end=latest_end,
        period_basis="annual",
        revision=1,
        previous_id=None,
        content_hash="a" * 64,
        captured_at=datetime(2026, 10, 15, tzinfo=UTC),
        source_fetched_at=None,
        raw_ref=None,
        payload={
            "facts": payload_facts,
            "review": {
                "schema_version": 1,
                "company_key": "apple",
                "manifest_sha256": "b" * 64,
                "checked_on": checked_on,
                "status": "complete",
                "latest_period_end": latest_end.isoformat(),
                "latest_period_start": latest_start.isoformat(),
                "latest_published_on": None,
                "issues": [],
                "documents": [{
                    "id": "annual-2024",
                    "raw_ref": "review/raw/annual-2024.pdf",
                    "sha256": "c" * 64,
                }],
                "fact_evidence": [
                    {
                        "fact_sha256": digest,
                        "references": [
                            {
                                "document_id": "annual-2024",
                                "raw_ref": "review/raw/annual-2024.pdf",
                                "locator": "page 1",
                                "excerpt": "reviewed evidence",
                                "source_label": "Annual report",
                            }
                        ],
                    }
                    for digest in digests
                ],
                "superseded_facts": [],
                "equity_reconciliations": [],
                "equity_scope_reviews": [],
                "conflict_resolutions": [],
            },
        },
    )
    return dataset


def _provider_dataset(year: str, *, dataset_id: int, end: date | None = None) -> ResearchDataset:
    facts = fixture_facts("apple", year)
    if end is not None:
        facts = tuple(
            replace(
                fact,
                period=FactPeriod(
                    start=date(2024, 9, 29),
                    end=end,
                    basis=PeriodBasis.ANNUAL,
                    label="FY2025",
                ),
            )
            for fact in facts
        )
    period_end = max(fact.period.end for fact in facts)
    return ResearchDataset(
        id=dataset_id,
        kind=DatasetKind.ANNUAL_FACTS.value,
        subject_kind=SubjectKind.COMPANY.value,
        subject_id=11,
        source="sec.companyfacts.v1",
        schema_version=SCHEMA_VERSION,
        adapter_version="fixture",
        period_end=period_end,
        period_basis="annual",
        revision=1,
        previous_id=None,
        content_hash=f"{dataset_id:064x}",
        captured_at=datetime(2026, 10, 15, tzinfo=UTC),
        source_fetched_at=None,
        raw_ref=None,
        payload={"facts": [fact_to_payload(fact) for fact in facts]},
    )


def test_review_receipt_requires_verified_facts_and_exact_date() -> None:
    accepted = _valid_review_envelope(_reviewed_dataset(), expected_company_key="apple")
    assert accepted is not None
    assert accepted.checked_on == date(2026, 10, 15)

    assert _valid_review_envelope(
        _reviewed_dataset(candidate=True), expected_company_key="apple"
    ) is None
    assert _valid_review_envelope(
        _reviewed_dataset(), expected_company_key="microsoft"
    ) is None
    assert _valid_review_envelope(
        _reviewed_dataset(checked_on="2026-10-16"), expected_company_key="apple"
    ) is None


def test_unknown_or_noncanonical_review_dates_stay_unknown() -> None:
    assert _iso_date(None) is None
    assert _iso_date("2026-10-15") == date(2026, 10, 15)
    assert _iso_date("2026-1-5") is None
    assert _iso_date("not-a-date") is None


def test_reconciliation_fact_is_labeled_as_derived_evidence() -> None:
    dataset = _reviewed_dataset()
    fact = dataset_facts(dataset)[0]
    derived = replace(
        fact,
        provenance=replace(
            fact.provenance,
            source="material.equity_reconciliation",
        ),
    )

    view = _fact_view(dataset, derived)

    assert view.evidence_kind == "derived_calculation"
    assert view.source == "复核计算"
    assert view.locator == derived.original_locator


def test_reviewed_multi_year_window_replaces_legacy_rows_but_keeps_newer_period() -> None:
    old_review = _reviewed_dataset(years=("FY2023",), checked_on="2025-10-15")
    old_review.id = 19
    reviewed = _reviewed_dataset(
        years=("FY2021", "FY2022", "FY2023", "FY2024")
        , include_opening=True
    )
    reviewed.id = 20
    legacy = [
        _provider_dataset(year, dataset_id=index)
        for index, year in enumerate(("FY2021", "FY2022", "FY2023", "FY2024"), start=1)
    ]
    legacy[0].period_end = date(2020, 9, 26)
    newer = _provider_dataset("FY2024", dataset_id=10, end=date(2025, 9, 27))

    selected = _current_versions(
        [*legacy, newer, old_review, reviewed], expected_company_key="apple"
    )

    assert [dataset.id for dataset in selected] == [20, 10]
    assert selected[0].source == "issuer.reviewed_annual.v1"
    assert selected[1].period_end == date(2025, 9, 27)
