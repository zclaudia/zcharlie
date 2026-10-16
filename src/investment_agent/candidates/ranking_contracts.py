"""Frozen source and result DTOs for account-specific candidate ranking."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, ValidationInfo, model_validator

from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.portfolio.calculation_contracts import Fraction, Nonnegative
from investment_agent.portfolio.suitability_contracts import (
    MAX_FIT_CHECKS,
    AccountAssessmentRequest,
    FitCheck,
    TradeAssumptions,
)

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RANKING_POLICY_VERSION: Literal["account_round_robin_v1"] = "account_round_robin_v1"


class CandidateContextValue(Contract):
    key: Annotated[str, Field(min_length=1, max_length=100)]
    value: JsonValue


class ScreenCandidateSource(Contract):
    source_kind: Literal["screen_hit", "screen_truncated"]
    source_ref: ObjectRef
    listing_ref: ObjectRef
    run_ref: ObjectRef
    source_row_id: Annotated[int, Field(strict=True, ge=1)]
    run_status: Literal["success"]
    run_finished_at: datetime
    run_hash: Hash
    source_snapshot_hash: Hash
    row_snapshot: dict[str, JsonValue] = Field(default_factory=dict)
    outcome: Literal["hit", "truncated"]
    source_as_of: datetime
    template_id: Text
    template_version: Text
    rank: Annotated[int, Field(strict=True, ge=1)]
    strength: str | None = None
    reasons: Annotated[tuple[JsonValue, ...], Field(max_length=100)] = ()
    context: Annotated[tuple[CandidateContextValue, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def screen_identity(self) -> ScreenCandidateSource:
        if (
            self.run_ref.kind != "pipeline_run"
            or not isinstance(self.run_ref.id, int)
            or self.run_ref.revision != 1
            or self.run_ref.field_path is not None
        ):
            raise ValueError("screen source requires an exact pipeline run")
        if (
            self.listing_ref.kind != "listing"
            or self.source_ref.kind != "pipeline_run"
            or self.source_ref.id != self.run_ref.id
            or self.source_ref.revision != self.run_ref.revision
            or self.source_ref.field_path != f"screen_results.{self.source_row_id}"
        ):
            raise ValueError("screen source requires an exact run and result-row reference")
        if (self.source_kind == "screen_hit") != (self.outcome == "hit"):
            raise ValueError("screen source kind and outcome disagree")
        if len({item.key for item in self.context}) != len(self.context):
            raise ValueError("duplicate screen context key")
        if not self.row_snapshot:
            raise ValueError("screen source requires its complete frozen result row")
        canonical_row = json.dumps(
            self.row_snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if hashlib.sha256(canonical_row).hexdigest() != self.source_snapshot_hash:
            raise ValueError("screen row snapshot hash mismatch")
        return self


class OpenCandidateSource(Contract):
    source_kind: Literal["open_candidate"]
    source_ref: ObjectRef
    listing_ref: ObjectRef
    candidate_id: Annotated[int, Field(strict=True, ge=1)]
    status: Literal["open"]
    opened_at: datetime
    expires_at: datetime
    closed_at: None = None
    source_snapshot_hash: Hash
    candidate_sources: Annotated[tuple[JsonValue, ...], Field(max_length=100)]
    manual_nomination: bool

    @model_validator(mode="after")
    def open_identity(self) -> OpenCandidateSource:
        if (
            self.source_ref.kind != "candidate"
            or not isinstance(self.source_ref.id, int)
            or self.source_ref.id != self.candidate_id
            or self.source_ref.revision != 1
            or self.source_ref.field_path is not None
        ):
            raise ValueError("candidate source requires its existing integer candidate reference")
        if self.listing_ref.kind != "listing":
            raise ValueError("candidate source requires a listing reference")
        return self


class CandidateLifecycleSnapshot(Contract):
    source_kind: Literal["candidate_lifecycle"]
    source_ref: ObjectRef
    listing_ref: ObjectRef
    candidate_id: Annotated[int, Field(strict=True, ge=1)]
    status: Literal["open", "promoted", "dismissed", "expired"]
    opened_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    source_snapshot_hash: Hash
    candidate_sources: Annotated[tuple[JsonValue, ...], Field(max_length=100)]
    manual_nomination: bool

    @model_validator(mode="after")
    def lifecycle_identity(self) -> CandidateLifecycleSnapshot:
        if (
            self.source_ref.kind != "candidate"
            or not isinstance(self.source_ref.id, int)
            or self.source_ref.id != self.candidate_id
            or self.source_ref.revision != 1
            or self.source_ref.field_path is not None
        ):
            raise ValueError("lifecycle snapshot requires its existing candidate reference")
        if self.listing_ref.kind != "listing":
            raise ValueError("lifecycle snapshot requires a listing reference")
        if (self.status == "open") != (self.closed_at is None):
            raise ValueError("open candidate has no closed_at; closed candidate requires it")
        return self


class WatchlistCandidateSource(Contract):
    source_kind: Literal["watchlist"]
    source_ref: ObjectRef
    listing_ref: ObjectRef
    item_id: Annotated[int, Field(strict=True, ge=1)]
    status: Literal["active", "paused", "removed"]
    changed_at: datetime
    source_snapshot_hash: Hash
    provider: Text
    report_year: int | None = None
    note: Text | None = None
    industry: Text | None = None
    last_refreshed_at: datetime | None = None
    last_refresh_status: Text

    @model_validator(mode="after")
    def watchlist_identity(self) -> WatchlistCandidateSource:
        if (
            self.source_ref.kind != "watchlist_item"
            or not isinstance(self.source_ref.id, int)
            or self.source_ref.id != self.item_id
            or self.source_ref.revision != 1
            or self.source_ref.field_path is not None
        ):
            raise ValueError("watchlist source requires its existing integer item reference")
        if self.listing_ref.kind != "listing":
            raise ValueError("watchlist source requires a listing reference")
        return self


CandidateSource = ScreenCandidateSource | OpenCandidateSource | WatchlistCandidateSource


class SourceMember(Contract):
    listing_ref: ObjectRef
    sources: Annotated[tuple[CandidateSource, ...], Field(min_length=1, max_length=101)]

    @model_validator(mode="after")
    def source_listing_identity(self) -> SourceMember:
        if self.listing_ref.kind != "listing" or self.listing_ref.revision != 1:
            raise ValueError("source member requires an exact listing reference")
        if any(source.listing_ref != self.listing_ref for source in self.sources):
            raise ValueError("every frozen source must match its member listing")
        identities = [
            (
                source.source_ref,
                getattr(source, "template_id", None),
                getattr(source, "template_version", None),
            )
            for source in self.sources
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source identity for listing")
        return self


class SourceExclusion(Contract):
    kind: Literal["business", "source_cap"]
    listing_ref: ObjectRef | None
    reason_code: Text
    source_snapshots: Annotated[
        tuple[CandidateSource | CandidateLifecycleSnapshot, ...], Field(max_length=102)
    ] = ()
    unprocessed_range_start: Annotated[int, Field(strict=True, ge=0)] | None = None
    unprocessed_range_end: Annotated[int, Field(strict=True, ge=0)] | None = None

    @model_validator(mode="after")
    def exclusion_shape(self) -> SourceExclusion:
        if self.kind == "business":
            if self.listing_ref is None or not self.source_snapshots:
                raise ValueError("business exclusion requires its frozen source rows and hashes")
            if any(snapshot.listing_ref != self.listing_ref for snapshot in self.source_snapshots):
                raise ValueError("all excluded source rows must match the listing identity")
            if self.unprocessed_range_start is not None or self.unprocessed_range_end is not None:
                raise ValueError("business exclusion cannot carry a source cap range")
        elif self.source_snapshots:
            raise ValueError("source cap exclusion cannot claim unprocessed frozen source rows")
        elif (self.unprocessed_range_start is None) != (self.unprocessed_range_end is None):
            raise ValueError("source cap range requires both inclusive bounds")
        elif self.unprocessed_range_start is None:
            raise ValueError("source cap exclusion requires its unprocessed range")
        elif (
            self.unprocessed_range_end is not None
            and self.unprocessed_range_end < self.unprocessed_range_start
        ):
            raise ValueError("source cap range end must not precede its start")
        return self


class SourceIssue(Contract):
    template_id: Text
    kind: Literal["missing_run", "no_complete_run", "run_metadata_missing"]
    reason_code: Text
    observed_run_refs: tuple[ObjectRef, ...] = ()

    @model_validator(mode="after")
    def exact_runs(self) -> SourceIssue:
        if any(
            ref.kind != "pipeline_run"
            or not isinstance(ref.id, int)
            or ref.revision != 1
            or ref.field_path
            for ref in self.observed_run_refs
        ):
            raise ValueError("source issue run references must be exact pipeline runs")
        return self


class SourceRunSnapshot(Contract):
    run_ref: ObjectRef
    status: Literal["success"]
    finished_at: datetime
    run_hash: Hash
    as_of: datetime
    template_id: Text
    template_version: Text
    row_count: Annotated[int, Field(strict=True, ge=0)]

    @model_validator(mode="after")
    def run_identity(self) -> SourceRunSnapshot:
        if (
            self.run_ref.kind != "pipeline_run"
            or not isinstance(self.run_ref.id, int)
            or self.run_ref.revision != 1
            or self.run_ref.field_path is not None
        ):
            raise ValueError("source run must be an exact pipeline run")
        return self


class RankingSourcePolicy(Contract):
    template_ids: Annotated[tuple[Text, ...], Field(min_length=1, max_length=100)]
    include_manual_open: Literal[True] = True
    include_watchlist_exclusions: Literal[True] = True
    as_of_policy: Literal["server_latest_success_per_template"] = (
        "server_latest_success_per_template"
    )

    @model_validator(mode="after")
    def unique_templates(self) -> RankingSourcePolicy:
        if len(set(self.template_ids)) != len(self.template_ids):
            raise ValueError("duplicate ranking source template")
        return self


class CandidatePolicySnapshot(Contract):
    expiry_days: Annotated[int, Field(strict=True, ge=0, le=365)]
    dismiss_cooldown_days: Annotated[int, Field(strict=True, ge=0, le=365)]


class SourceManifest(Contract):
    source_run_refs: tuple[ObjectRef, ...]
    source_runs: Annotated[tuple[SourceRunSnapshot, ...], Field(max_length=100)]
    candidate_policy: CandidatePolicySnapshot
    as_of: datetime
    members: Annotated[tuple[SourceMember, ...], Field(max_length=2000)]
    exclusions: Annotated[tuple[SourceExclusion, ...], Field(max_length=10000)] = ()
    source_issues: Annotated[tuple[SourceIssue, ...], Field(max_length=100)] = ()
    coverage: Literal["complete", "partial"]
    total_unique_count: Annotated[int, Field(strict=True, ge=0)]
    processed_count: Annotated[int, Field(strict=True, ge=0)]
    unprocessed_count: Annotated[int, Field(strict=True, ge=0)]
    source_hashes: Annotated[tuple[Hash, ...], Field(max_length=210000)]
    manifest_hash: Hash
    canonical_bytes: Annotated[int, Field(strict=True, ge=0, le=4 * 1024 * 1024)]

    @model_validator(mode="after")
    def universe_shape(self, info: ValidationInfo) -> SourceManifest:
        listing_refs = [member.listing_ref for member in self.members]
        if len(set(listing_refs)) != len(listing_refs):
            raise ValueError("source manifest has duplicate listing members")
        if self.total_unique_count != self.processed_count + self.unprocessed_count:
            raise ValueError("manifest counts do not reconcile")
        if self.processed_count != len(self.members):
            raise ValueError("processed count must equal frozen members")
        if self.coverage == "complete" and (self.unprocessed_count != 0 or self.source_issues):
            raise ValueError(
                "complete manifest cannot have unprocessed candidates or source issues"
            )
        if self.coverage == "partial" and self.unprocessed_count == 0 and not self.source_issues:
            raise ValueError("partial manifest requires an unprocessed range or source issue")
        if any(
            ref.kind != "pipeline_run"
            or not isinstance(ref.id, int)
            or ref.revision != 1
            or ref.field_path is not None
            for ref in self.source_run_refs
        ):
            raise ValueError("source runs must be exact pipeline run references")
        if self.source_run_refs != tuple(run.run_ref for run in self.source_runs):
            raise ValueError("source run references must match frozen run snapshots")
        if any(run.as_of > self.as_of or run.finished_at > self.as_of for run in self.source_runs):
            raise ValueError("source run knowledge cannot be after manifest as_of")
        frozen_hashes = tuple(
            sorted(
                [
                    source.source_snapshot_hash
                    for member in self.members
                    for source in member.sources
                ]
                + [
                    snapshot.source_snapshot_hash
                    for exclusion in self.exclusions
                    for snapshot in exclusion.source_snapshots
                ]
            )
        )
        if self.source_hashes != frozen_hashes:
            raise ValueError("source hashes must match frozen member source rows")
        frozen_sources = [source for member in self.members for source in member.sources] + [
            source for exclusion in self.exclusions for source in exclusion.source_snapshots
        ]
        for source in frozen_sources:
            if isinstance(source, ScreenCandidateSource):
                run = next(
                    (
                        candidate
                        for candidate in self.source_runs
                        if candidate.run_ref == source.run_ref
                        and candidate.template_id == source.template_id
                        and candidate.template_version == source.template_version
                    ),
                    None,
                )
                if run is None:
                    raise ValueError("screen source run is absent from manifest run snapshots")
                if (
                    source.run_hash != run.run_hash
                    or source.source_as_of != run.as_of
                    or source.template_id != run.template_id
                    or source.template_version != run.template_version
                    or source.run_finished_at != run.finished_at
                ):
                    raise ValueError("screen source row does not match its frozen run metadata")
                if any(
                    value > self.as_of for value in (source.source_as_of, source.run_finished_at)
                ):
                    raise ValueError("screen source knowledge cannot be after manifest as_of")
            elif isinstance(source, OpenCandidateSource | CandidateLifecycleSnapshot):
                if source.opened_at > self.as_of:
                    raise ValueError("candidate cannot be opened after manifest as_of")
                if source.closed_at is not None and source.closed_at > self.as_of:
                    raise ValueError("candidate cannot be closed after manifest as_of")
            elif isinstance(source, WatchlistCandidateSource):
                if source.changed_at > self.as_of or (
                    source.last_refreshed_at is not None and source.last_refreshed_at > self.as_of
                ):
                    raise ValueError("watchlist source knowledge cannot be after manifest as_of")
        if not (info.context and info.context.get("skip_manifest_hash")):
            canonical = canonical_source_manifest_bytes(self)
            if len(canonical) != self.canonical_bytes:
                raise ValueError("canonical manifest byte count mismatch")
            if hashlib.sha256(canonical).hexdigest() != self.manifest_hash:
                raise ValueError("canonical manifest hash mismatch")
        return self


class PerListingTradeAssumptions(Contract):
    kind: Literal["per_listing"] = "per_listing"
    items: Annotated[tuple[ListingTradeAssumption, ...], Field(max_length=2000)]

    @model_validator(mode="after")
    def unique_listings(self) -> PerListingTradeAssumptions:
        if len({item.listing_ref for item in self.items}) != len(self.items):
            raise ValueError("duplicate per-listing trade assumption")
        return self


class ListingTradeAssumption(Contract):
    listing_ref: ObjectRef
    assumptions: TradeAssumptions


class BatchTradeAssumptions(Contract):
    kind: Literal["batch"] = "batch"
    fixed_fee: Nonnegative | None
    fee_rate: Fraction | None
    assumption_source: Text
    unknown_fields: tuple[Literal["fixed_fee", "fee_rate"], ...] = ()
    price_policy: Literal["valuation_price_as_scenario"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def explicit_unknowns(self) -> BatchTradeAssumptions:
        if (self.fixed_fee is None) != ("fixed_fee" in self.unknown_fields):
            raise ValueError("null fixed fee must be explicitly unknown")
        if (self.fee_rate is None) != ("fee_rate" in self.unknown_fields):
            raise ValueError("null fee rate must be explicitly unknown")
        if len(set(self.unknown_fields)) != len(self.unknown_fields):
            raise ValueError("duplicate unknown fee field")
        return self


TradeAssumptionPolicy = Annotated[
    PerListingTradeAssumptions | BatchTradeAssumptions,
    Field(discriminator="kind"),
]


class RankingRequest(AccountAssessmentRequest):
    view: Literal["account"] = "account"
    action_policy: Literal["consider_buy"] = "consider_buy"
    source_policy: RankingSourcePolicy
    trade_assumptions: TradeAssumptionPolicy


class RankingEntry(Contract):
    ranking_ref: ObjectRef
    listing_ref: ObjectRef
    listing_ticker: Text | None = None
    listing_name: Text | None = None
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    ranking_bucket: Literal["incomplete", "blocked"]
    financial_status: Literal["pass", "warning", "blocked", "unknown"]
    resolved_action: Literal["open", "add", "hold"] | None
    action_unknown_reason: Text | None = None
    fit_ref: ObjectRef | None = None
    source_refs: Annotated[tuple[ObjectRef, ...], Field(min_length=1, max_length=101)]
    checks: Annotated[tuple[FitCheck, ...], Field(max_length=MAX_FIT_CHECKS)] = ()
    execution_error: Text | None = None

    @model_validator(mode="after")
    def entry_identity(self) -> RankingEntry:
        if (
            self.ranking_ref.kind != "candidate_ranking"
            or not isinstance(self.ranking_ref.id, UUID)
            or self.ranking_ref.revision != 1
            or self.ranking_ref.field_path is not None
        ):
            raise ValueError("exact ranking snapshot reference required")
        if (
            self.listing_ref.kind != "listing"
            or not isinstance(self.listing_ref.id, int)
            or self.listing_ref.revision != 1
            or self.listing_ref.field_path is not None
        ):
            raise ValueError("exact listing reference required")
        if (self.resolved_action is None) != (self.action_unknown_reason is not None):
            raise ValueError("unknown action requires a reason")
        if self.resolved_action is None and (
            self.financial_status != "unknown"
            or self.fit_ref is not None
            or self.execution_error is not None
        ):
            raise ValueError("unknown holding presence requires a finite unknown without a fit or failure")
        if self.fit_ref is not None and (
            self.fit_ref.kind != "account_fit"
            or not isinstance(self.fit_ref.id, UUID)
            or self.fit_ref.revision != 1
            or self.fit_ref.field_path is not None
        ):
            raise ValueError("fit reference must identify an account-fit result")
        if self.ranking_bucket == "blocked" and self.financial_status != "blocked":
            raise ValueError("blocked bucket requires a proven financial block")
        if self.financial_status == "blocked" and self.ranking_bucket != "blocked":
            raise ValueError("blocked financial result must be in the blocked bucket")
        return self


class RankingSnapshot(Contract):
    id: UUID
    reference: ObjectRef
    input_hash: Hash
    manifest_hash: Hash
    universe_hash: Hash
    account_id: UUID
    created_at: datetime
    as_of: datetime
    source_manifest: SourceManifest
    ranking_policy_version: Literal["account_round_robin_v1"] = RANKING_POLICY_VERSION
    entry_count: Annotated[int, Field(strict=True, ge=0, le=2000)]
    coverage: Literal["complete", "partial"]
    current_status: Literal["valid", "stale"]
    previous_ref: ObjectRef | None = None
    change_reasons: Annotated[tuple[Text, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def snapshot_identity(self) -> RankingSnapshot:
        if (
            self.reference.kind != "candidate_ranking"
            or not isinstance(self.reference.id, UUID)
            or self.reference.id != self.id
            or self.reference.revision != 1
            or self.reference.field_path is not None
        ):
            raise ValueError("ranking snapshot requires its exact UUID reference at revision 1")
        if self.manifest_hash != self.source_manifest.manifest_hash:
            raise ValueError("snapshot manifest hash mismatch")
        if self.entry_count != self.source_manifest.processed_count:
            raise ValueError("entry count must match processed manifest members")
        if self.coverage != self.source_manifest.coverage:
            raise ValueError("snapshot coverage must match frozen manifest")
        if canonical_universe_hash(self.source_manifest.members) != self.universe_hash:
            raise ValueError("ranking universe hash mismatch")
        if self.previous_ref is not None and self.previous_ref.kind != "candidate_ranking":
            raise ValueError("previous ref must be a ranking snapshot")
        if self.previous_ref is not None and (
            not isinstance(self.previous_ref.id, UUID)
            or self.previous_ref.revision != 1
            or self.previous_ref.field_path is not None
        ):
            raise ValueError("previous ref must identify an exact ranking snapshot")
        return self


class RankingCursorClaims(Contract):
    ranking_ref: ObjectRef
    ordinal: Annotated[int, Field(strict=True, ge=0)]
    bucket_filter: Literal["incomplete", "blocked"] | None = None
    financial_status_filter: Literal["pass", "warning", "blocked", "unknown"] | None = None
    filter_hash: Hash

    @model_validator(mode="after")
    def exact_ranking(self) -> RankingCursorClaims:
        if (
            self.ranking_ref.kind != "candidate_ranking"
            or not isinstance(self.ranking_ref.id, UUID)
            or self.ranking_ref.revision != 1
            or self.ranking_ref.field_path is not None
        ):
            raise ValueError("cursor requires an exact ranking snapshot reference")
        return self


def canonical_source_manifest_bytes(manifest: SourceManifest) -> bytes:
    """Canonical UTF-8 payload hashed by ``SourceManifest.manifest_hash``."""
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash", "canonical_bytes"})
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def build_source_manifest(payload: Mapping[str, object]) -> SourceManifest:
    """Construct a manifest while computing its canonical SHA-256 and byte count."""
    values = dict(payload)
    values.pop("manifest_hash", None)
    values.pop("canonical_bytes", None)
    preliminary = SourceManifest.model_validate(
        {**values, "manifest_hash": "0" * 64, "canonical_bytes": 0},
        context={"skip_manifest_hash": True},
    )
    canonical = canonical_source_manifest_bytes(preliminary)
    values["manifest_hash"] = hashlib.sha256(canonical).hexdigest()
    values["canonical_bytes"] = len(canonical)
    return SourceManifest.model_validate(values)


def canonical_universe_hash(members: tuple[SourceMember, ...]) -> str:
    """SHA-256 over sorted exact listing refs, independent of source order."""
    listings = sorted(
        (member.listing_ref.model_dump(mode="json") for member in members),
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
    )
    payload = json.dumps(
        listings, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_source_manifest_hash(payload: Mapping[str, object]) -> tuple[str, int]:
    """Return the canonical manifest SHA-256 and byte count before construction."""
    values = dict(payload)
    values.pop("manifest_hash", None)
    values.pop("canonical_bytes", None)
    preliminary = SourceManifest.model_validate(
        {**values, "manifest_hash": "0" * 64, "canonical_bytes": 0},
        context={"skip_manifest_hash": True},
    )
    canonical = canonical_source_manifest_bytes(preliminary)
    return hashlib.sha256(canonical).hexdigest(), len(canonical)


__all__ = [
    "BatchTradeAssumptions",
    "CandidateContextValue",
    "CandidateSource",
    "CandidatePolicySnapshot",
    "CandidateLifecycleSnapshot",
    "ListingTradeAssumption",
    "OpenCandidateSource",
    "PerListingTradeAssumptions",
    "RankingCursorClaims",
    "RankingEntry",
    "RankingRequest",
    "RankingSourcePolicy",
    "RankingSnapshot",
    "RANKING_POLICY_VERSION",
    "ScreenCandidateSource",
    "SourceExclusion",
    "SourceManifest",
    "SourceMember",
    "SourceRunSnapshot",
    "SourceIssue",
    "TradeAssumptionPolicy",
    "WatchlistCandidateSource",
    "build_source_manifest",
    "canonical_source_manifest_bytes",
    "canonical_source_manifest_hash",
    "canonical_universe_hash",
]
