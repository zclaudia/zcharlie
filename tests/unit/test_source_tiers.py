"""Source trust tiers: code-owned display labels, never arbitration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.evidence.source_tiers import (
    SOURCE_KIND_TIERS,
    SOURCE_TIER_LABELS,
    SourceTier,
    source_tier,
)
from investment_agent.investor.common import ObjectRef, Quantity


def _record(source_kind: str) -> EvidenceRecord:
    now = datetime.now(UTC)
    return EvidenceRecord(id=uuid4(), revision=1, actor="fixture", origin_ref=None, created_at=now,
        source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1), source_kind=source_kind,
        locator="fixture", content_hash="fixture", subject_ref=ObjectRef(kind="listing", id=1, revision=1),
        period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual"),
        published_at=now - timedelta(days=1), acquired_at=now - timedelta(days=1),
        available_from=now - timedelta(days=1),
        # user_report sources keep user_report verification until separately verified.
        verification="user_report" if source_kind == "user_report" else "verified",
        conflict="none", value=Quantity(value="1", unit="CNY"))


def test_every_evidence_source_kind_maps_to_a_labelled_tier() -> None:
    assert set(SOURCE_KIND_TIERS) == {"document", "snapshot", "user_report", "research_dataset"}
    assert source_tier(_record("document")) == "disclosure_original"
    assert source_tier(_record("snapshot")) == "snapshot_vendor"
    assert source_tier(_record("user_report")) == "user"
    assert source_tier(_record("research_dataset")) == "derived"
    # The contract itself refuses unknown source kinds; nothing ever maps to
    # the reserved regulator tier.
    with pytest.raises(ValidationError):
        _record("future_kind")
    assert "official" not in SOURCE_KIND_TIERS.values()


def test_tier_labels_are_display_only_and_complete() -> None:
    tiers: tuple[SourceTier, ...] = ("disclosure_original", "official", "snapshot_vendor", "user", "derived")
    assert set(SOURCE_TIER_LABELS) == set(tiers)
    assert all(SOURCE_TIER_LABELS[tier].strip() for tier in tiers)
