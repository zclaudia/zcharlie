"""Source trust tiers: display and audit labels, never arbitration.

The research policy orders evidence sources 一手披露 > 监管官方 > 供应商 > 用户/
派生, but that ordering is deliberately **not** a conflict-resolution rule:
conflicts are resolved only by publish time, scope and correction relations,
and unresolved conflicts keep blocking the fact (fail-closed). A tier is a
code-owned derivation from the evidence source identity whose only job is to
make the layering visible in reads, comparison reports and the UI. It never
gates eligibility and never picks a winner.

``official`` is reserved for regulator/statistics channels that do not exist
yet (A2+ candidate list in the research policy); it is never derived today.
"""

from __future__ import annotations

from typing import Literal

from investment_agent.evidence.contracts import EvidenceRecord

SourceTier = Literal["disclosure_original", "official", "snapshot_vendor", "user", "derived"]

SOURCE_KIND_TIERS: dict[str, SourceTier] = {
    # document records are disclosure originals (immutable, correction-chained).
    "document": "disclosure_original",
    "snapshot": "snapshot_vendor",
    "user_report": "user",
    "research_dataset": "derived",
}

SOURCE_TIER_LABELS: dict[SourceTier, str] = {
    "disclosure_original": "一手披露",
    "official": "监管官方",
    "snapshot_vendor": "供应商快照",
    "user": "用户材料",
    "derived": "派生数据集",
}


def source_tier(record: EvidenceRecord) -> SourceTier:
    """Derive one record's trust tier; unknown kinds read as vendor snapshots."""
    return SOURCE_KIND_TIERS.get(record.source_kind, "snapshot_vendor")
