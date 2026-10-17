"""Typed persistence for immutable account-fit and ranking artifacts."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investment_agent.candidates.ranking_contracts import RankingEntry, RankingSnapshot
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.suitability_contracts import AccountFitAssessment
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry as CandidateRankingEntryRow,
)
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    stable_series_key,
)
from investment_agent.supplemental_rules.contracts import (
    EvaluationResult,
    FrozenPair,
    ResearchRuleBinding,
    RuleApplication,
    RuleCoverageManifest,
)

Assessment = AccountFitAssessment | RankingSnapshot | RuleApplication | RuleCoverageManifest


class RuleBindingState(NamedTuple):
    """Consumer-side binding honesty marker for one subject.

    ``bound`` carries the exact published manifest (and its binding when
    readable); ``unknown`` always carries a precise reason and never treats
    a missing binding as "no rules apply".
    """

    state: Literal["bound", "unknown"]
    reasons: tuple[str, ...]
    manifest: RuleCoverageManifest | None
    binding: ResearchRuleBinding | None


class StoredRanking(NamedTuple):
    """A persisted header and its complete, ordinal-ordered entry tuple."""

    snapshot: RankingSnapshot
    entries: tuple[RankingEntry, ...]


class InvestmentAssessmentRepository:
    """Save and read validated P1 artifacts without mutating stored payloads.

    The methods flush inside a savepoint but leave commit ownership with the
    caller. A ranking header and all entries therefore participate in the
    caller's one transaction while a failed bundle cannot leave a partial
    flush in that transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_assessment(
        self, owner_id: OwnerId, assessment: AccountFitAssessment
    ) -> AccountFitAssessment:
        """Persist one account-fit result or return an exact input-hash replay."""

        _check_owner(owner_id)
        assessment = AccountFitAssessment.model_validate(assessment.model_dump(mode="json"))
        if assessment.reference.kind != "account_fit":
            raise ValueError("account-fit assessment reference required")
        row = await self.session.get(
            InvestmentAssessment,
            (str(assessment.id), assessment.reference.revision),
        )
        if row is not None:
            if row.owner_id != owner_id or row.kind != "account_fit":
                raise ValueError("assessment UUID is already used by another owner or kind")
            saved = _parse_assessment(row)
            _check_header_columns(row, saved)
            if not isinstance(saved, AccountFitAssessment) or _json_payload(saved) != _json_payload(assessment):
                raise ValueError("account-fit UUID conflicts with immutable input or payload")
            return saved

        new_row = InvestmentAssessment(
            id=str(assessment.id),
            revision=assessment.reference.revision,
            owner_id=owner_id,
            kind="account_fit",
            account_id=str(assessment.account_id),
            listing_id=_listing_id(assessment.listing_ref),
            as_of=assessment.assessment_as_of,
            input_hash=assessment.input_hash,
            entry_count=None,
            manifest_hash=None,
            universe_hash=None,
            payload=_json_payload(assessment),
        )
        async with self.session.begin_nested():
            self.session.add(new_row)
            await self.session.flush()
        return assessment

    async def save_rule_application(
        self, owner_id: OwnerId, application: RuleApplication
    ) -> RuleApplication:
        """Persist a strict P2 application in the shared immutable assessment table."""

        _check_owner(owner_id)
        application = RuleApplication.model_validate(application.model_dump(mode="json"))
        if application.owner_id != owner_id or application.application_sequence < 1:
            raise ValueError("rule application owner/sequence is invalid")
        if application.series_key != stable_series_key(
            owner_id, _listing_id(application.listing_ref), str(application.rule_ref.id), application.context
        ):
            raise ValueError("rule application series_key does not match its stable identity")
        if application.previous_application_ref is not None:
            previous_row = await self.session.get(
                InvestmentAssessment,
                (str(application.previous_application_ref.id), application.previous_application_ref.revision),
            )
            if previous_row is None or previous_row.owner_id != owner_id or previous_row.kind != "rule_application":
                raise ValueError("previous application must resolve to an owned rule application")
            previous = RuleApplication.model_validate(previous_row.payload)
            if previous.series_key != application.series_key or previous.application_mode != "live":
                raise ValueError("previous application must be a live predecessor from the same series")
        existing = await self.session.get(
            InvestmentAssessment, (str(application.id), application.revision)
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.kind != "rule_application":
                raise ValueError("application UUID is already used by another owner or kind")
            saved = _parse_assessment(existing)
            _check_header_columns(existing, saved)
            if not isinstance(saved, RuleApplication) or _json_payload(saved) != _json_payload(application):
                raise ValueError("application UUID conflicts with immutable input or payload")
            return saved

        new_row = InvestmentAssessment(
            id=str(application.id),
            revision=application.revision,
            owner_id=owner_id,
            kind="rule_application",
            account_id=None,
            listing_id=_listing_id(application.listing_ref),
            as_of=application.as_of,
            input_hash=application.input_hash,
            entry_count=None,
            manifest_hash=None,
            universe_hash=None,
            series_key=application.series_key,
            application_sequence=application.application_sequence,
            application_mode=application.application_mode,
            payload=_json_payload(application),
        )
        async with self.session.begin_nested():
            self.session.add(new_row)
            await self.session.flush()
        return application

    async def save_rule_coverage(
        self, owner_id: OwnerId, manifest: RuleCoverageManifest
    ) -> RuleCoverageManifest:
        """Persist a strict P3 coverage manifest; the caller owns the commit."""

        _check_owner(owner_id)
        manifest = RuleCoverageManifest.model_validate(manifest.model_dump(mode="json"))
        if manifest.owner_id != owner_id or manifest.reference.revision != 1:
            raise ValueError("rule coverage owner/revision is invalid")
        if manifest.reference.kind != "rule_coverage":
            raise ValueError("rule coverage reference required")
        existing = await self.session.get(
            InvestmentAssessment, (str(manifest.id), manifest.reference.revision)
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.kind != "rule_coverage":
                raise ValueError("coverage UUID is already used by another owner or kind")
            saved = _parse_assessment(existing)
            _check_header_columns(existing, saved)
            if (
                not isinstance(saved, RuleCoverageManifest)
                or _json_payload(saved) != _json_payload(manifest)
            ):
                raise ValueError("coverage UUID conflicts with immutable input or payload")
            return saved

        new_row = InvestmentAssessment(
            id=str(manifest.id),
            revision=manifest.reference.revision,
            owner_id=owner_id,
            kind="rule_coverage",
            account_id=None,
            listing_id=_listing_id(manifest.listing_ref),
            as_of=manifest.effective_cutoff,
            input_hash=manifest.input_hash,
            entry_count=len(manifest.rule_entries),
            manifest_hash=manifest.manifest_hash,
            universe_hash=None,
            series_key=None,
            application_sequence=None,
            application_mode=None,
            payload=_json_payload(manifest),
        )
        async with self.session.begin_nested():
            self.session.add(new_row)
            await self.session.flush()
        return manifest

    async def list_latest_live_rule_application_payloads(
        self,
        owner_id: OwnerId,
        listing_id: int,
        context: str,
        *,
        limit: int = 2000,
    ) -> tuple[tuple[dict[str, Any], ...], bool]:
        """Bounded latest-live application payloads of one listing and context.

        Publication-side rechecks consume raw payloads so the investor layer
        can read rule state without importing the supplemental package. The
        boolean reports a truncated scan: a known lower bound, never absence.
        """

        _check_owner(owner_id)
        if not 1 <= limit <= 5000:
            raise ValueError("rule application scan limit must be in 1..5000")
        row = InvestmentAssessment
        # A distinct alias is required: unaliased self-reference collapses the
        # correlation and lets superseded series members read as latest.
        newer = aliased(InvestmentAssessment)
        superseded = select(newer.id).where(
            newer.owner_id == row.owner_id,
            newer.kind == row.kind,
            newer.listing_id == row.listing_id,
            newer.series_key == row.series_key,
            newer.application_mode == "live",
            newer.application_sequence > row.application_sequence,
        ).exists()
        statement = (
            select(row)
            .where(
                row.owner_id == owner_id,
                row.kind == "rule_application",
                row.listing_id == listing_id,
                row.application_mode == "live",
                row.payload["context"].as_string() == context,
                ~superseded,
            )
            .order_by(row.series_key)
            .limit(limit + 1)
        )
        rows = list((await self.session.scalars(statement)).all())
        truncated = len(rows) > limit
        return tuple(row.payload for row in rows[:limit]), truncated

    async def save_publication_closure(
        self,
        owner_id: OwnerId,
        *,
        target: RuleApplication,
        semantic_review_ref: ObjectRef,
        operation_id: UUID,
        as_of: datetime,
        source_manifest_hash: str,
    ) -> RuleApplication:
        """Build and persist one formal_review closure for one open issue.

        The prepared coverage manifest is the frozen source; the target must
        still be its series' latest live head or the closure is rejected. The
        caller owns the transaction and the commit.
        """

        _check_owner(owner_id)
        if target.owner_id != owner_id or not target.open_issue_refs:
            raise InvestorError("rule_coverage_closure_conflict", "闭合目标不是本主体的未决事项")
        head = await self.session.execute(
            select(InvestmentAssessment)
            .where(
                InvestmentAssessment.owner_id == owner_id,
                InvestmentAssessment.kind == "rule_application",
                InvestmentAssessment.series_key == target.series_key,
                InvestmentAssessment.application_mode == "live",
            )
            .order_by(InvestmentAssessment.application_sequence.desc())
            .limit(1)
        )
        head_row = head.scalars().first()
        if head_row is None or head_row.id != str(target.id):
            raise InvestorError("rule_coverage_closure_conflict", "目标规则序列已推进，闭合被拒绝")
        maximum = await self.session.scalar(
            select(func.max(InvestmentAssessment.application_sequence)).where(
                InvestmentAssessment.owner_id == owner_id,
                InvestmentAssessment.kind == "rule_application",
                InvestmentAssessment.listing_id == target.listing_ref.id,
                InvestmentAssessment.series_key == target.series_key,
            )
        )
        pair = _closure_pair(target, as_of=as_of)
        fingerprint = canonical_hash({
            "pair": pair,
            "manifest_hash": source_manifest_hash,
            "mode": "live",
            "purpose": "issue_review",
        })
        result = EvaluationResult.model_validate({
            **target.result.model_dump(mode="python"),
            "assessment_status": "resolved",
            "semantic_status": "reviewed",
            "semantic_review_ref": semantic_review_ref.model_dump(mode="python"),
            "closure_refs": (target.reference.model_dump(mode="python"),),
            "open_issue_refs": (),
            "diagnostics": (*target.result.diagnostics, "resolved_by_publication_review"),
        })
        now = datetime.now(UTC)
        application = RuleApplication(
            id=uuid5(operation_id, f"publication-closure:{target.series_key}:{target.id}"),
            operation_id=operation_id,
            owner_id=owner_id,
            listing_ref=target.listing_ref,
            company_id=target.company_id,
            context=target.context,
            rule_ref=target.rule_ref,
            rules_generation=target.rules_generation,
            events_generation=target.events_generation,
            matching_manifest_hash=target.matching_manifest_hash,
            event_ref=target.event_ref,
            as_of=as_of,
            evaluated_at=now,
            valid_until=target.valid_until,
            review_due_at=target.review_due_at,
            input_hash=fingerprint,
            matcher_version=target.matcher_version,
            evaluator_version=target.evaluator_version,
            dependencies=target.dependencies,
            series_key=target.series_key,
            application_sequence=(maximum or 0) + 1,
            application_mode="live",
            authorization="formal_review",
            origin_ref=None,
            created_at=now,
            actor="publication_formal_review",
            previous_application_ref=target.reference,
            frozen_pair=pair,
            source_manifest_hash=source_manifest_hash,
            evaluation_purpose="issue_review",
            applicability=target.applicability,
            condition_result=target.condition_result,
            evidence_status=target.evidence_status,
            assessment_status="resolved",
            semantic_status="reviewed",
            semantic_review_ref=semantic_review_ref,
            effective_effects=tuple(e.model_dump(mode="json") for e in result.effective_effects),
            open_issue_refs=(),
            closure_refs=(target.reference,),
            result=result,
            payload={
                "scope_semantic_hash": target.payload.get("scope_semantic_hash"),
                "prepared_manifest_hash": source_manifest_hash,
            },
        )
        return await self.save_rule_application(owner_id, application)

    async def save_published_rule_coverage(
        self,
        owner_id: OwnerId,
        *,
        prepared: RuleCoverageManifest,
        manifest_id: UUID,
        semantic_review_ref: ObjectRef,
        closure_application_refs: tuple[ObjectRef, ...],
        thesis_ref: ObjectRef,
        recommendation_ref: ObjectRef,
        limits_recheck: tuple[str, ...],
        committed_at: datetime,
    ) -> RuleCoverageManifest:
        """Derive and persist the published manifest from its prepared source.

        The prepared content is carried verbatim; only the publication-stage
        identity fields are added. The caller owns the transaction.
        """

        _check_owner(owner_id)
        if prepared.stage != "prepared":
            raise ValueError("published coverage must derive from a prepared manifest")
        if committed_at < prepared.created_at:
            raise ValueError("published coverage cannot precede its prepared source")
        data = prepared.model_dump(mode="python")
        data.update({
            "id": manifest_id,
            "stage": "published",
            "prepared_ref": prepared.reference.model_dump(mode="python"),
            "prepared_manifest_hash": prepared.manifest_hash,
            "semantic_review_ref": semantic_review_ref.model_dump(mode="python"),
            "closure_application_refs": tuple(ref.model_dump(mode="python") for ref in closure_application_refs),
            "thesis_ref": thesis_ref.model_dump(mode="python"),
            "recommendation_ref": recommendation_ref.model_dump(mode="python"),
            "limits_recheck": tuple(limits_recheck),
            "committed_at": committed_at,
            "created_at": committed_at,
            "manifest_hash": "pending",
        })
        data["manifest_hash"] = canonical_hash(
            {key: value for key, value in data.items() if key != "manifest_hash"}
        )
        published = RuleCoverageManifest.model_validate(data)
        return await self.save_rule_coverage(owner_id, published)

    async def get_rule_application(
        self, owner_id: OwnerId, reference: ObjectRef
    ) -> RuleApplication | None:
        """Exact rule application or None; narrows the shared assessment union."""

        value = await self.get_exact(owner_id, reference, expected_kind="rule_application")
        return value if isinstance(value, RuleApplication) else None

    async def get_rule_coverage(
        self, owner_id: OwnerId, reference: ObjectRef
    ) -> RuleCoverageManifest | None:
        """Exact coverage manifest or None; narrows the shared assessment union."""

        value = await self.get_exact(owner_id, reference, expected_kind="rule_coverage")
        return value if isinstance(value, RuleCoverageManifest) else None

    async def published_rule_coverage_limits(
        self,
        owner_id: OwnerId,
        manifest: RuleCoverageManifest,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Bounded current-limitation recheck of one published coverage manifest.

        The shared supplemental predicate runs here so publication and
        consumer reads never import the supplemental package; storage is the
        single allowed edge. Queries stay inside the caller's transaction.
        """

        _check_owner(owner_id)
        from investment_agent.supplemental_rules.publication_limits import (
            published_manifest_limits,
        )

        return await published_manifest_limits(self.session, owner=owner_id, manifest=manifest, at=now)

    async def latest_published_rule_coverage(
        self, owner_id: OwnerId, listing_id: int
    ) -> RuleCoverageManifest | None:
        """Newest published coverage manifest of one listing, or None."""

        _check_owner(owner_id)
        row = await self.session.scalar(
            select(InvestmentAssessment)
            .where(
                InvestmentAssessment.owner_id == owner_id,
                InvestmentAssessment.kind == "rule_coverage",
                InvestmentAssessment.listing_id == listing_id,
                InvestmentAssessment.payload["stage"].as_string() == "published",
            )
            .order_by(InvestmentAssessment.as_of.desc(), InvestmentAssessment.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        value = RuleCoverageManifest.model_validate(row.payload)
        _check_header_columns(row, value)
        return value

    async def rule_binding_state(
        self, owner_id: OwnerId, listing_ref: ObjectRef
    ) -> RuleBindingState:
        """Binding/coverage honesty marker of one subject for consumers.

        A published manifest makes the subject ``bound``; a binding without a
        published manifest is ``rule_coverage_unpublished``; no rows at all is
        ``rule_binding_missing`` (legacy conclusions are never retroactively
        bound). Pure reads; no backfill of any historical payload.
        """

        _check_owner(owner_id)
        from investment_agent.storage.repositories.supplemental_rules import (
            SupplementalRulesRepository,
        )

        manifest = await self.latest_published_rule_coverage(owner_id, int(listing_ref.id))
        if manifest is not None:
            binding = await SupplementalRulesRepository(self.session).get_research_rule_binding(
                owner_id, manifest.binding_ref
            )
            return RuleBindingState("bound", (), manifest, binding)
        binding = await SupplementalRulesRepository(
            self.session
        ).latest_research_rule_binding_for_listing(owner_id, int(listing_ref.id))
        if binding is not None:
            return RuleBindingState("unknown", ("rule_coverage_unpublished",), None, binding)
        return RuleBindingState("unknown", ("rule_binding_missing",), None, None)


    async def get_exact(
        self,
        owner_id: OwnerId,
        reference: ObjectRef,
        *,
        expected_kind: Literal[
            "account_fit", "candidate_ranking", "rule_application", "rule_coverage"
        ] | None = None,
    ) -> Assessment | None:
        """Read an exact UUID/revision only after checking owner and kind."""

        _check_owner(owner_id)
        kind = _kind_for_ref(reference)
        if expected_kind is not None and kind != expected_kind:
            raise ValueError(f"reference kind {kind!r} does not match {expected_kind!r}")
        if not isinstance(reference.id, UUID) or reference.revision != 1 or reference.field_path:
            raise ValueError("exact revision-1 investment assessment reference required")
        row = await self.session.get(InvestmentAssessment, (str(reference.id), reference.revision))
        if row is None or row.owner_id != owner_id or row.kind != kind:
            return None
        result = _parse_assessment(row)
        _check_header_columns(row, result)
        return result

    async def save_ranking(
        self,
        owner_id: OwnerId,
        snapshot: RankingSnapshot,
        entries: Sequence[RankingEntry],
    ) -> StoredRanking:
        """Atomically append a complete ranking header and its full entry set.

        Exact input-hash retries return the original snapshot and entries. A
        changed payload under the same hash is rejected instead of overwriting
        the earlier immutable artifact.
        """

        _check_owner(owner_id)
        _validate_ranking_bundle(snapshot, entries)
        existing = await self.session.get(
            InvestmentAssessment,
            (str(snapshot.id), snapshot.reference.revision),
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.kind != "candidate_ranking":
                raise ValueError("ranking UUID is already used by another owner or kind")
            stored = await self._read_ranking_row(existing)
            if _json_payload(stored.snapshot) != _json_payload(snapshot) or tuple(
                _json_payload(entry) for entry in stored.entries
            ) != tuple(_json_payload(entry) for entry in entries):
                raise ValueError("ranking UUID conflicts with immutable input or entries")
            return stored

        header = InvestmentAssessment(
            id=str(snapshot.id),
            revision=snapshot.reference.revision,
            owner_id=owner_id,
            kind="candidate_ranking",
            account_id=str(snapshot.account_id),
            listing_id=None,
            as_of=snapshot.as_of,
            input_hash=snapshot.input_hash,
            entry_count=snapshot.entry_count,
            manifest_hash=snapshot.manifest_hash,
            universe_hash=snapshot.universe_hash,
            payload=_json_payload(snapshot),
        )
        child_rows = [
            CandidateRankingEntryRow(
                ranking_id=str(snapshot.id),
                revision=snapshot.reference.revision,
                owner_id=owner_id,
                account_id=str(snapshot.account_id),
                listing_id=_listing_id(entry.listing_ref),
                ordinal=entry.ordinal,
                payload=_json_payload(entry),
            )
            for entry in entries
        ]
        async with self.session.begin_nested():
            self.session.add(header)
            await self.session.flush()
            self.session.add_all(child_rows)
            await self.session.flush()
        return StoredRanking(snapshot, tuple(entries))

    async def read_ranking(
        self, owner_id: OwnerId, reference: ObjectRef
    ) -> StoredRanking | None:
        """Read and revalidate a complete snapshot before exposing any page."""

        artifact = await self.get_exact(
            owner_id, reference, expected_kind="candidate_ranking"
        )
        if artifact is None:
            return None
        if not isinstance(artifact, RankingSnapshot):
            raise ValueError("candidate-ranking header has invalid payload type")
        row = await self.session.get(InvestmentAssessment, (str(reference.id), reference.revision))
        assert row is not None
        return await self._read_ranking_row(row)

    async def list_ranking_entries(
        self,
        owner_id: OwnerId,
        reference: ObjectRef,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[RankingEntry, ...] | None:
        """Return a validated page; validation always covers the full snapshot."""

        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("ranking page requires offset >= 0 and limit in 1..100")
        ranking = await self.read_ranking(owner_id, reference)
        if ranking is None:
            return None
        stmt = (
            select(CandidateRankingEntryRow)
            .where(
                CandidateRankingEntryRow.ranking_id == str(reference.id),
                CandidateRankingEntryRow.revision == reference.revision,
            )
            .order_by(CandidateRankingEntryRow.ordinal)
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return tuple(_parse_entry(row) for row in rows)

    async def _read_ranking_row(self, row: InvestmentAssessment) -> StoredRanking:
        if row.kind != "candidate_ranking":
            raise ValueError("ranking reference resolves to a non-ranking artifact")
        snapshot = _parse_assessment(row)
        if not isinstance(snapshot, RankingSnapshot):
            raise ValueError("candidate-ranking header has invalid payload type")
        _check_header_columns(row, snapshot)
        stmt = (
            select(CandidateRankingEntryRow)
            .where(
                CandidateRankingEntryRow.ranking_id == row.id,
                CandidateRankingEntryRow.revision == row.revision,
            )
            .order_by(CandidateRankingEntryRow.ordinal)
        )
        entry_rows = list((await self.session.execute(stmt)).scalars())
        entries = tuple(_parse_entry(entry_row) for entry_row in entry_rows)
        _validate_ranking_bundle(snapshot, entries)
        if any(
            entry_row.owner_id != row.owner_id or entry_row.account_id != row.account_id
            for entry_row in entry_rows
        ):
            raise ValueError("ranking entry owner/account does not match its header")
        return StoredRanking(snapshot, entries)


def _closure_pair(target: RuleApplication, *, as_of: datetime) -> FrozenPair:
    """The closure application's frozen pair: the target's series advanced by one.

    The subject is re-frozen at the coverage cutoff, and the target becomes the
    exact live predecessor; every other identity field stays with the rule.
    """

    data = target.frozen_pair.model_dump(mode="python")
    data["subject"]["as_of"] = as_of
    data["prior_live_application_ref"] = target.reference.model_dump(mode="python")
    data["prior_open_issue_refs"] = tuple(
        ref.model_dump(mode="python") for ref in target.open_issue_refs
    )
    issue_key = target.frozen_pair.rule.issue_key or str(target.rule_ref.id)
    data["prior_issue_keys"] = (issue_key,) if target.open_issue_refs else ()
    data["prior_input_hash"] = target.input_hash
    data["input_hash"] = "0" * 64
    pair = FrozenPair.model_validate(data)
    return pair.model_copy(update={
        "input_hash": canonical_hash(pair.model_dump(mode="python", exclude={"input_hash"})),
    })


def _check_owner(owner_id: OwnerId) -> None:
    if owner_id != "local_owner":
        raise ValueError("unsupported owner")


def _kind_for_ref(reference: ObjectRef) -> (
    Literal["account_fit", "candidate_ranking", "rule_application", "rule_coverage"]
):
    if reference.kind not in {"account_fit", "candidate_ranking", "rule_application", "rule_coverage"}:
        raise ValueError("unsupported investment assessment reference kind")
    return reference.kind


def _listing_id(reference: ObjectRef) -> int:
    if reference.kind != "listing" or not isinstance(reference.id, int) or reference.field_path:
        raise ValueError("exact integer listing reference required")
    return reference.id


def _json_payload(
    value: AccountFitAssessment | RankingSnapshot | RankingEntry | RuleApplication | RuleCoverageManifest,
) -> dict[str, object]:
    return value.model_dump(mode="json")


def _parse_assessment(row: InvestmentAssessment) -> Assessment:
    if row.kind == "account_fit":
        return AccountFitAssessment.model_validate(row.payload)
    if row.kind == "candidate_ranking":
        return RankingSnapshot.model_validate(row.payload)
    if row.kind == "rule_application":
        return RuleApplication.model_validate(row.payload)
    if row.kind == "rule_coverage":
        return RuleCoverageManifest.model_validate(row.payload)
    raise ValueError(f"unsupported investment assessment kind {row.kind!r}")


def _parse_entry(row: CandidateRankingEntryRow) -> RankingEntry:
    entry = RankingEntry.model_validate(row.payload)
    if _listing_id(entry.listing_ref) != row.listing_id or entry.ordinal != row.ordinal:
        raise ValueError("ranking entry columns do not match its payload")
    if entry.ranking_ref.id != UUID(row.ranking_id) or entry.ranking_ref.revision != row.revision:
        raise ValueError("ranking entry reference does not match its parent row")
    return entry


def _check_header_columns(row: InvestmentAssessment, artifact: Assessment) -> None:
    if row.id != str(artifact.id) or row.revision != artifact.reference.revision:
        raise ValueError("assessment identity columns do not match payload")
    if row.input_hash != artifact.input_hash:
        raise ValueError("assessment input hash column does not match payload")
    if row.kind == "rule_application":
        if not isinstance(artifact, RuleApplication):
            raise ValueError("assessment kind and payload disagree")
        if (
            row.account_id is not None
            or row.listing_id != _listing_id(artifact.listing_ref)
            or not _same_timestamp(row.as_of, artifact.as_of)
            or row.series_key != artifact.series_key
            or row.application_sequence != artifact.application_sequence
            or row.application_mode != artifact.application_mode
            or any(value is not None for value in (row.entry_count, row.manifest_hash, row.universe_hash))
        ):
            raise ValueError("rule-application header columns do not match payload")
        return
    if row.kind == "rule_coverage":
        if not isinstance(artifact, RuleCoverageManifest):
            raise ValueError("assessment kind and payload disagree")
        if (
            row.account_id is not None
            or row.listing_id != _listing_id(artifact.listing_ref)
            or not _same_timestamp(row.as_of, artifact.effective_cutoff)
            or row.entry_count != len(artifact.rule_entries)
            or row.manifest_hash != artifact.manifest_hash
            or row.universe_hash is not None
            or any(value is not None for value in (row.series_key, row.application_sequence, row.application_mode))
        ):
            raise ValueError("rule-coverage header columns do not match payload")
        return
    if row.kind == "account_fit":
        if not isinstance(artifact, AccountFitAssessment):
            raise ValueError("assessment kind and payload disagree")
        if row.account_id != str(artifact.account_id):
            raise ValueError("assessment account column does not match payload")
        if row.listing_id != _listing_id(artifact.listing_ref):
            raise ValueError("assessment listing column does not match payload")
        if not _same_timestamp(row.as_of, artifact.assessment_as_of) or any(
            value is not None for value in (row.entry_count, row.manifest_hash, row.universe_hash)
        ):
            raise ValueError("account-fit header columns do not match payload")
    else:
        if not isinstance(artifact, RankingSnapshot):
            raise ValueError("assessment kind and payload disagree")
        if row.account_id != str(artifact.account_id):
            raise ValueError("assessment account column does not match payload")
        if (
            row.listing_id is not None
            or not _same_timestamp(row.as_of, artifact.as_of)
            or row.entry_count != artifact.entry_count
            or row.manifest_hash != artifact.manifest_hash
            or row.universe_hash != artifact.universe_hash
        ):
            raise ValueError("ranking header columns do not match payload")


def _same_timestamp(left: datetime, right: datetime) -> bool:
    """SQLite drops timezone metadata when returning DateTime values."""

    if left.tzinfo is None or left.utcoffset() is None:
        left = left.replace(tzinfo=UTC)
    else:
        left = left.astimezone(UTC)
    if right.tzinfo is None or right.utcoffset() is None:
        right = right.replace(tzinfo=UTC)
    else:
        right = right.astimezone(UTC)
    return left == right


def _validate_ranking_bundle(
    snapshot: RankingSnapshot, entries: Sequence[RankingEntry]
) -> None:
    # Run Pydantic validation again at the persistence boundary, even if a
    # caller bypassed the standard constructor with model_construct().
    snapshot = RankingSnapshot.model_validate(snapshot.model_dump(mode="json"))
    checked_entries = tuple(RankingEntry.model_validate(entry.model_dump(mode="json")) for entry in entries)
    if snapshot.reference.kind != "candidate_ranking" or snapshot.reference.id != snapshot.id:
        raise ValueError("exact candidate-ranking reference required")
    if snapshot.entry_count != len(checked_entries):
        raise ValueError("ranking entry count does not match the complete member set")
    if tuple(entry.ordinal for entry in checked_entries) != tuple(range(len(checked_entries))):
        raise ValueError("ranking ordinals must be unique and continuous from zero")
    if any(entry.ranking_ref != snapshot.reference for entry in checked_entries):
        raise ValueError("ranking entry references must match the exact snapshot")
    entry_ids = tuple(_listing_id(entry.listing_ref) for entry in checked_entries)
    members_by_listing = {
        _listing_id(member.listing_ref): member for member in snapshot.source_manifest.members
    }
    member_ids = tuple(members_by_listing)
    if len(set(entry_ids)) != len(entry_ids) or set(entry_ids) != set(member_ids):
        raise ValueError("ranking entries must exactly cover the frozen source members")
    for entry in checked_entries:
        expected_sources = {
            source.source_ref for source in members_by_listing[_listing_id(entry.listing_ref)].sources
        }
        if set(entry.source_refs) != expected_sources:
            raise ValueError("ranking entry sources must match its frozen manifest member")
    if snapshot.manifest_hash != snapshot.source_manifest.manifest_hash:
        raise ValueError("ranking manifest hash does not match the frozen source manifest")
    if snapshot.entry_count != snapshot.source_manifest.processed_count:
        raise ValueError("ranking header count does not match the processed source manifest")
    if snapshot.coverage != snapshot.source_manifest.coverage:
        raise ValueError("ranking coverage does not match its frozen source manifest")
