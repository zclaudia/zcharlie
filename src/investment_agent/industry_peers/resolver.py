"""Method-scoped validation for ``industry_peer_context`` calculations (WP12).

The generic :class:`~investment_agent.investor.publication_numbers.NumericResolver`
hands every input of a calculation to the *target company's* ``fact()``, so a
multi-company peer record fails there by design. This module is the method's
own entry: it validates a peer metric record against the task's trusted
``PeerContextBinding`` and answers the five fixed operations of the unified
method entry (technical design §多公司计算的专用校验):

* ``scope`` - the only identity this method may render is the bound target
  listing; peer facts are never scope.
* ``verify`` - the full backend checklist for one bound calculation reference.
* ``numeric_output`` - render one ``calculation.results.*`` ``NumericNode``
  (role ``output``) of a verified record through the WP13 registered display
  rules (the page and the agent format one quantity identically).
* ``review_material`` - backend verification plus the WP13 fixed formatter:
  the model receives the bounded metric digest (:func:`metric_digest`),
  never the expanded fact references.
* ``current_limits`` - the dependency recheck of a published opinion: the
  record must still verify *now*, with current correction states, the full
  input manifest, the bound method version and the newest roster revision
  (WP13 completed the re-check situations).

Registration is a single fixed mapping for this one method
(:func:`peer_method_entry`); every other method keeps the generic resolver and
the generic paths byte for byte. There is no plugin system.

Checklist implemented by :meth:`PeerContextResolver.verify` (design §WP12):

1. **Binding & mapping membership.** The reference must be the trusted
   binding's reference for its metric *and* be submitted by the bound batch's
   published mapping for the bound target - a fabricated
   ``calculation_ref``, an unbound batch, or a batch that does not belong to
   the target is rejected. A reused old record keeps its original
   ``details.batch_run_id``; that field is never required to equal the
   binding's batch and never restores authorization by itself.
2. **Method identity.** ``method`` / ``method_version`` / ``metric_key`` and
   the details schema must match this module's constants; target, report
   period and cohort must match the binding and the mapping.
3. **Real subjects.** Every cited fact is re-derived from its saved annual
   snapshot row (:func:`investment_agent.industry_peers.evidence.peer_field_evidence`)
   and its subject must be the roster listing of the company the record
   declares for it - rewriting a peer fact's ``subject_ref`` into the target
   cannot disguise it as a target fact.
4. **Roster membership, target exclusion, dedup.** Target and every included
   and excluded company must be roster members; the target never appears as a
   peer; no company appears twice.
5. **Period, semantics, units, currency.** Each cited field's period and
   basis follow the metric semantics (flows ``period_to_date/annual``,
   stocks ``point_in_time``, the disclosed ROE ``annual_ratio``); amount
   fields carry CNY, the saved ROE carries ``fraction``; result units follow
   the metric spec.
6. **Cutoff.** The record and every cited fact must be available no later
   than the binding cutoff - a record or evidence created later never
   validates for the older task.
7. **Original-document proof and conflicts.** The cited evidence must be the
   byte-identical trusted re-derivation of its snapshot field (identity,
   content hash and check status), and the field's current version state must
   be free of unresolved conflicts and undeclared corrections (WP4
   :func:`peer_field_states`); with ``require_consistent_sources=False`` the
   states are reported instead of rejected (the ``current_limits`` recheck).
8. **Exclusion bases.** Applicability exclusions (non-positive denominators,
   non-positive parent equity) must cite their field evidence with the
   non-positive value; a collection gap only proves "not obtained this time"
   and can never back a "not applicable" claim.
9. **Result keys, counts, statistics.** ``results`` carries only the five
   fixed keys; valid/candidate counts must match the inclusion/exclusion
   audit; median, ascending position and the metric status are recomputed
   from the audit; the record identity must be the deterministic derivation
   of its ``input_hash``. (The frozen-input hash itself is recomputed on the
   trusted save path - see
   :func:`investment_agent.industry_peers.calculations.verify_built_metric_record`
   - where the frozen batch is at hand; the read side proves the record's
   internal consistency and its evidence bindings.)
10. **Verification honesty.** ``inputs_verified`` is true only when every
    cited input is verified evidence in a consistent state and no input was
    declared snapshot-only; partial samples stay ``candidate`` and can never
    back critical claims as verified.

Verification results are cached per resolver instance - one request validates
a bound record once no matter how many ``NumericNode`` instances cite it. The
cache never crosses a request and never crosses a correction-state check: a
new resolver instance re-runs the WP4 state query, so a correction that
arrived after a previous validation is seen by the next request's resolver.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, NoReturn

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord, Period
from investment_agent.industry_peers.artifacts import (
    MappingArtifactError,
    PeerBatchMapping,
    mapping_artifact_path,
    read_batch_mapping,
)
from investment_agent.industry_peers.calculations import (
    _APPLICABILITY_REASONS,
    DETAILS_SCHEMA,
    METHOD,
    METHOD_VERSION,
    METRIC_KEYS,
    MIN_VALID_PEERS,
    POSITION_RULE,
    PeerExclusion,
    _metric_status,
    ascending_position,
    metric_spec,
)
from investment_agent.industry_peers.contracts import PeerContextBinding
from investment_agent.industry_peers.evidence import peer_field_evidence, peer_field_states
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.contracts import NumericNode
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import ResolvedNode, format_quantity
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot

RESULT_KEYS: frozenset[str] = frozenset(
    {"target", "peer_median", "ascending_position", "valid_peer_count", "candidate_peer_count"}
)
"""The only result keys a peer metric record may carry."""

CLEAN_VERSION_STATES = frozenset({"consistent", "correction"})
"""Version states a cited field may carry: consistent, or an explicit
correction of an older version. ``pending_recheck``, ``conflict`` and
``corrected`` mean the cited value awaits re-check or was superseded."""

_COUNT_UNIT = "count"
_POSITION_UNIT = "percent"

_FLOW_FIELDS = frozenset({"operating_revenue", "net_profit_parent", "operating_cash_flow"})
_STOCK_FIELDS = frozenset({"equity_parent", "contract_liabilities"})
_RATIO_FIELDS = frozenset({"roe_weighted_annual"})
"""Per-field period/basis semantics (the WP3 importer's field kinds; pinned
by a unit test against ``cli.commands.peer_annual.FIELD_SPECS``)."""

DIGEST_SCHEMA = "industry_peer_metric_digest/1"
"""Schema tag of the WP13 fixed review-material formatter output.

The backend runs the complete input checklist (:meth:`verify`); the model
receives only this bounded per-metric digest - definition, target, period,
results with units, sample counts, inclusion/exclusion rules as reason
categories with counts (never a per-company value table), check coverage,
limitations and hashes."""

MANIFEST_HASH_SCHEMA = "industry_peer_manifest_hash/1"
"""Schema tag of the full-manifest hash bound into the material set hash.

It covers the complete metric input list (the cited ``fact_evidence``
manifest) and the whole inclusion/exclusion audit - the material set hash of
a review therefore binds the exact inputs, while the model-visible digest
carries its own separate summary hash."""

RENEWAL_BLOCK_REASON = "peer_renewal_research_required"
"""Structured renewal-refusal prefix (WP13): an opinion with critical
peer-context dependencies is never "maintained unchanged" or extended. The
reason carries the dependency reference plus the dependency's current
``current_limits`` reasons; a new study bound to current results and a newly
published opinion is the only renewal path. The original opinion keeps its
established validity dates and its known limitation reasons."""

INCLUSION_RULE = (
    "bound roster members other than the target whose same-period metric inputs "
    "are usable; exclusions below carry their audited reason categories"
)
"""The fixed inclusion rule sentence of the digest (no per-company table)."""

DISPLAY_RULES: dict[tuple[str, str], tuple[str, int]] = {
    ("value", "fraction"): ("percent", 2),
    ("value", "ratio"): ("decimal", 4),
    ("position", "percent"): ("percent", 2),
    ("value", "count"): ("decimal", 0),
}
"""The WP13 registered publication display rules of this method.

The page (WP10 ``industry_peers.js`` ``formatMetricNumber``) and the agent
must never render the same quantity differently, so the method entry owns a
canonical ``(display, precision)`` per result kind and :meth:`numeric_output`
always formats through it - the model's ``display_format`` cannot produce a
diverging number: ROE and the contract-liabilities ratio are saved as
fraction and shown as percent with two decimals (``32.53%``), the cash-flow
ratio shows four decimals (``0.5792``), the ascending position shows percent
with two decimals (``94.44%``), counts stay whole numbers. No other method
is registered; every other caller keeps the two-decimal default of
:func:`format_quantity`."""


def registered_display(result_key: str, unit: str) -> tuple[str, int]:
    """The registered ``(display, precision)`` for one result kind."""

    kind = "position" if result_key == "ascending_position" else "value"
    rule = DISPLAY_RULES.get((kind, unit))
    if rule is None:
        raise InvestorError("invalid_reference", f"result kind {result_key} ({unit}) has no registered display rule")
    return rule


def _canonical_hash(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of ``payload`` (sorted keys, no float)."""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_period(field: str, report_period: date) -> Period:
    """The metric-semantics period of one canonical field for one report year."""

    if field in _STOCK_FIELDS:
        return Period(start=report_period, end=report_period, basis="point_in_time")
    if field in _RATIO_FIELDS:
        return Period(start=date(report_period.year, 1, 1), end=report_period, basis="annual_ratio")
    return Period(start=date(report_period.year, 1, 1), end=report_period, basis="period_to_date/annual")


def _expected_field_unit(field: str) -> tuple[str, str | None]:
    """The ``(unit, currency)`` a canonical field is saved in."""

    return ("fraction", None) if field in _RATIO_FIELDS else ("CNY", "CNY")


def peer_method_entry(method: str) -> type[PeerContextResolver] | None:
    """The registered entry of one method, or ``None``.

    This is the whole registration of the unified method entry: exactly one
    fixed method maps to :class:`PeerContextResolver`, whose public methods
    are the five fixed operations. Unknown methods return ``None`` and every
    caller keeps its original generic path - there is no plugin system and no
    runtime registration.
    """

    return _METHOD_ENTRIES.get(method)


async def peer_method_payload(
    session: AsyncSession, owner_id: OwnerId, ref: ObjectRef
) -> dict[str, Any] | None:
    """The saved payload of ``ref`` when it is this method's calculation.

    Call sites use this to route a reference before any generic validation
    runs; anything that is not this method's record returns ``None`` and the
    caller proceeds with its original (unchanged) path.
    """

    row = await session.get(SavedCalculation, (str(ref.id), ref.revision))
    if row is None or row.owner_id != owner_id:
        return None
    payload = row.payload
    if isinstance(payload, dict) and payload.get("method") == METHOD:
        return payload
    return None


@dataclass(frozen=True)
class PeerFactValidation:
    """One cited fact of a verified peer record, with its declared role."""

    ref: ObjectRef
    record: EvidenceRecord
    company_id: int
    field: str
    role: str


@dataclass(frozen=True)
class PeerMetricValidation:
    """The verified state of one bound peer metric record."""

    ref: ObjectRef
    metric_key: str
    record: CalculationRecord
    target_ref: ObjectRef
    """The bound target's listing reference - the only identity this
    method's nodes may render."""
    facts: tuple[PeerFactValidation, ...]
    inputs_verified: bool
    """True only when every cited input is verified evidence in a consistent
    state and no input was declared snapshot-only; partial samples stay
    candidate."""
    inconsistent_sources: tuple[tuple[str, str, str], ...]
    """``(ticker, field, version state)`` for cited fields whose current
    correction state is not clean (empty on the strict path)."""


@dataclass(frozen=True)
class PeerReviewMaterial:
    """One metric's semantic-review material (WP13 fixed formatter).

    The backend verified the complete input checklist; the model receives the
    bounded digest only. The two hashes keep the two bindings apart: the
    *material set hash* (computed by the caller over the delivered materials)
    binds the complete input manifest and exclusion audit through
    :attr:`material`'s ``manifest_hash``, while the model-summary hash
    :attr:`summary_hash` records exactly what the model was shown.
    """

    material: dict[str, Any]
    """The fixed metric digest (:data:`DIGEST_SCHEMA`) - no per-company value
    table, no raw fact payloads, no peer identities beyond the target."""
    summary_hash: str
    """sha256 over the canonical digest; recorded separately from the
    material set hash."""
    manifest_hash: str
    """sha256 over the complete input manifest and the exclusion audit."""
    verified: bool
    """``inputs_verified`` of the checklist - a digest never promotes a
    pending source to verified."""
    validation: PeerMetricValidation


class PeerContextResolver:
    """Per-request validator for one trusted peer-context binding.

    The binding comes from the saved :class:`ExecutionGrant` (or from the
    saved origin-task grant of a published opinion); it is the only
    authorization source. Every failure raises :class:`InvestorError` with
    code ``invalid_reference`` - callers translate that into their own error
    shapes.
    """

    method = METHOD
    operations = ("scope", "verify", "numeric_output", "review_material", "current_limits")

    def __init__(
        self,
        session: AsyncSession,
        *,
        binding: PeerContextBinding,
        data_dir: Path | None = None,
        owner_id: OwnerId = "local_owner",
    ) -> None:
        if binding.cutoff.tzinfo is None:
            raise ValueError("the peer-context binding cutoff must be timezone-aware")
        self.session, self.binding, self.owner_id = session, binding, owner_id
        self._data_dir = data_dir
        self._cache: dict[tuple[str, int], PeerMetricValidation] = {}
        self._field_states: dict[tuple[int, str], Any] = {}
        self._mapping: PeerBatchMapping | None = None
        self._target: tuple[int, int] | None = None
        """``(company_id, listing_id)`` of the bound target, resolved once."""
        self._roster: dict[int, dict[str, Any]] | None = None

    # --- shared lookups ------------------------------------------------------------

    def _reject(self, message: str) -> NoReturn:
        raise InvestorError("invalid_reference", message)

    def _artifacts_root(self) -> Path:
        if self._data_dir is not None:
            return Path(self._data_dir)
        from investment_agent.config import Settings

        return Settings().data_dir  # type: ignore[call-arg]

    async def _target_identity(self) -> tuple[int, int]:
        """The bound target's ``(company_id, listing_id)`` from the directory."""

        if self._target is None:
            from investment_agent.industry_peers.service import (
                PeerTargetLookupError,
                resolve_peer_target,
            )

            try:
                identity = await resolve_peer_target(self.session, ticker=self.binding.target)
            except PeerTargetLookupError as exc:
                self._reject(f"the bound peer target is unavailable: {exc}")
            except ValueError as exc:
                self._reject(f"the bound peer target is not a canonical ticker: {exc}")
            self._target = (identity.company_id, identity.listing_id)
        return self._target

    async def target_ref(self) -> ObjectRef:
        """The bound target's listing reference (the method's only identity)."""

        _company_id, listing_id = await self._target_identity()
        return ObjectRef(kind="listing", id=listing_id, revision=1)

    def _target_company_id(self) -> int:
        """The bound target's company id once :meth:`_target_identity` ran."""

        assert self._target is not None
        return self._target[0]

    def _batch_mapping(self) -> PeerBatchMapping:
        """The binding batch's published mapping (read once per request)."""

        if self._mapping is None:
            try:
                self._mapping = read_batch_mapping(
                    mapping_artifact_path(self._artifacts_root(), self.binding.batch_run_id)
                )
            except MappingArtifactError as exc:
                self._reject(f"the bound batch mapping is unavailable: {exc}")
            assert self._mapping is not None
            if self._mapping.cohort_ref != self.binding.cohort_ref:
                self._reject("the bound batch was built against a different roster version")
        return self._mapping

    async def _roster_entries(self) -> dict[int, dict[str, Any]]:
        """The bound roster's members by ``company_id`` (read once per request)."""

        if self._roster is None:
            row = await self.session.get(PeerCohortSnapshot, str(self.binding.cohort_ref.id))
            if row is None or row.revision != self.binding.cohort_ref.revision:
                self._reject("the bound cohort roster version is unavailable")
            entries = row.payload.get("entries") if isinstance(row.payload, dict) else None
            if not isinstance(entries, list):
                self._reject("the bound cohort roster has no member entries")
            roster: dict[int, dict[str, Any]] = {}
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("company_id"), int):
                    roster[int(entry["company_id"])] = entry
            self._roster = roster
        return self._roster

    async def _field_state(self, company_id: int, field: str) -> Any:
        """The WP4 conflict/correction report of one company field (cached)."""

        key = (company_id, field)
        if key not in self._field_states:
            self._field_states[key] = await peer_field_states(
                self.session,
                company_id=company_id,
                metric=field,
                report_period=self.binding.report_period,
            )
        return self._field_states[key]

    # --- the five fixed operations -------------------------------------------------

    async def scope(self, ref: ObjectRef) -> ObjectRef:
        """``scope``: the only identity in scope is the bound target listing.

        An exact target-listing reference is returned as itself; a bound
        calculation reference resolves to the target after full validation;
        anything else - peer listings first of all - is out of scope.
        """

        target = await self.target_ref()
        if ref.model_copy(update={"field_path": None}) == target:
            return target
        if ref.kind == "calculation":
            await self.verify(ref)
            return target
        self._reject(
            "entity nodes reference the bound target listing only; peer identities are not scope"
        )

    async def verify(
        self,
        ref: ObjectRef,
        *,
        require_verified_inputs: bool = False,
        require_consistent_sources: bool = True,
    ) -> PeerMetricValidation:
        """``verify``: the full checklist for one bound calculation reference.

        A record that cites only part of its inputs (the declared-inputs
        submission of an undated source) stays *citable*: whatever it cites
        must be genuine, and ``inputs_verified`` stays false so claims keep
        their pending state. ``require_verified_inputs`` (critical publication
        sources) additionally demands the full manifest - the metric's values,
        applicability and exclusion bases all cited as verified evidence.
        ``require_consistent_sources`` downgrades the correction-state check
        to a report (the ``current_limits`` recheck needs the reasons, not
        the refusal).
        """

        bare = ref.model_copy(update={"field_path": None})
        cached = self._cache.get((str(bare.id), bare.revision))
        if cached is not None:
            if require_consistent_sources and cached.inconsistent_sources:
                ticker, field, state = cached.inconsistent_sources[0]
                self._reject(
                    f"peer field {field} of {ticker} is in state {state}; unresolved conflicts "
                    "and undeclared corrections are excluded"
                )
        else:
            cached = await self._verify_uncached(
                bare, require_consistent_sources=require_consistent_sources,
                require_verified_inputs=require_verified_inputs,
            )
            self._cache[(str(bare.id), bare.revision)] = cached
        if require_verified_inputs and not cached.inputs_verified:
            self._reject(
                f"peer metric {cached.metric_key} inputs are not fully verified; "
                "candidate or snapshot-only inputs cannot back a critical source"
            )
        return cached

    async def numeric_output(self, node: NumericNode) -> ResolvedNode:
        """``numeric_output``: render one results node of a verified record.

        Peer medians and positions are derived statistics: they enter as
        ``NumericNode.role = output`` and resolve against the bound target's
        identity. The display comes from this method's WP13 registered rules
        (:func:`registered_display`), not from the model's ``display_format``:
        the page (WP10) and the agent format the same quantity identically -
        fraction metrics show percent with two decimals, the cash-flow ratio
        shows four decimals, positions show percent with two decimals.
        """

        if node.role is not NumericRole.OUTPUT:
            self._reject("peer-context statistics enter as output nodes only")
        ref = node.ref
        path = ref.field_path or ""
        if ref.kind != "calculation" or not path.startswith("results."):
            self._reject("a peer numeric node references calculation.results.* only")
        validation = await self.verify(ref)
        result_key = path[len("results.") :]
        quantity = validation.record.results.get(result_key)
        if quantity is None or quantity.value is None:
            self._reject("result key is absent from the verified peer calculation")
        display, precision = registered_display(result_key, quantity.unit)
        text, unit, precision, value = format_quantity(quantity, display, precision=precision)
        return ResolvedNode(
            ref=ref,
            role=node.role.value,
            text=text,
            subject_ref=validation.target_ref,
            period=None,
            value=value,
            unit=unit,
            precision=precision,
            quality=validation.record.quality,
        )

    async def review_material(
        self, ref: ObjectRef, *, require_verified_inputs: bool = False
    ) -> PeerReviewMaterial:
        """``review_material``: the WP13 fixed formatter over a verified record.

        The complete metric input checklist runs here (all fact references,
        exclusion audits, proof states) exactly as in :meth:`verify`; the model
        then receives the bounded digest built by :func:`metric_digest` -
        definition, target, period, results with units, sample counts,
        inclusion/exclusion rules as reason categories with counts (never a
        per-company value table), check coverage, limitations and the input
        manifest / digest hashes. The digest never promotes a pending source:
        :attr:`PeerReviewMaterial.verified` stays the checklist's honest
        ``inputs_verified`` answer.
        """

        validation = await self.verify(ref, require_verified_inputs=require_verified_inputs)
        digest, summary_hash, manifest_hash = metric_digest(validation)
        return PeerReviewMaterial(
            material=digest,
            summary_hash=summary_hash,
            manifest_hash=manifest_hash,
            verified=validation.inputs_verified,
            validation=validation,
        )

    async def current_limits(self, ref: ObjectRef) -> tuple[str, ...]:
        """``current_limits``: dependency recheck of a published opinion.

        The record must still verify against its saved binding *now*, with the
        current correction states and the full input manifest. WP13 completes
        the re-check situations of the technical design (§更新与历史可追溯性):

        * ``peer_method_version_changed:<version>`` - the cited record was
          built by another method version (reported, not refused: the reason
          is the point);
        * ``peer_field_recheck:<ticker>:<field>:<state>`` - a cited peer
          field's current version state awaits re-check, conflicts or was
          superseded by a declared correction (verification invalidated);
        * ``peer_inputs_unverified:<metric>`` - the record cites only part of
          its inputs (the declared-inputs mode), so the check coverage never
          reaches verified;
        * ``peer_cohort_revision:<revision>`` - a newer roster revision was
          imported after the bound one (a new cohort), so the published peer
          set may be outdated.

        Renewal blocking for critical peer dependencies belongs to the WP13
        guard in ``verify_unchanged_review`` (see :data:`RENEWAL_BLOCK_REASON`);
        this operation only answers the recheck.
        """

        row = await self.session.get(SavedCalculation, (str(ref.id), ref.revision))
        if row is None or row.owner_id != self.owner_id:
            self._reject("the peer-context calculation is unavailable for this owner")
        payload = row.payload
        if not isinstance(payload, dict) or payload.get("method") != METHOD:
            self._reject("the calculation is not this method's record")
        if payload.get("method_version") != METHOD_VERSION:
            return (f"peer_method_version_changed:{payload.get('method_version')}",)
        validation = await self.verify(ref, require_consistent_sources=False)
        limits: list[str] = []
        if validation.inconsistent_sources:
            limits.extend(
                f"peer_field_recheck:{ticker}:{field}:{state}"
                for ticker, field, state in validation.inconsistent_sources
            )
        if not validation.inputs_verified:
            limits.append(f"peer_inputs_unverified:{validation.metric_key}")
        newer_revision = await self._newer_cohort_revision()
        if newer_revision is not None:
            limits.append(f"peer_cohort_revision:{newer_revision}")
        return tuple(limits)

    async def bind_subject(self, ref: ObjectRef) -> ObjectRef:
        """The verified record's trusted target subject (the pre-check seam)."""

        return (await self.verify(ref)).target_ref

    # --- the checklist -------------------------------------------------------------

    async def _verify_uncached(
        self,
        ref: ObjectRef,
        *,
        require_consistent_sources: bool,
        require_verified_inputs: bool,
    ) -> PeerMetricValidation:
        from investment_agent.industry_peers.artifacts import batch_is_committed
        if not await batch_is_committed(self.session, self._batch_mapping(), owner=self.owner_id):
            self._reject("the bound batch has no committed submission receipt")
        record = await self._load_record(ref)
        details = self._details(record)
        metric_key = self._metric_key(details)
        await self._identity(details)
        self._binding_and_mapping(ref, metric_key)
        spec = metric_spec(metric_key)
        roster = await self._roster_entries()
        audit = self._audit(details, roster)
        facts, inconsistent, full_citation = await self._facts(
            record,
            details,
            roster,
            spec,
            audit,
            require_consistent_sources=require_consistent_sources,
            require_verified_inputs=require_verified_inputs,
        )
        self._exclusion_bases(details, spec, facts, audit)
        if full_citation:
            self._values_from_facts(details, spec, facts, audit)
        self._statistics(record, details, spec, audit)
        inputs_verified = (
            full_citation
            and bool(facts)
            and details.get("unmaterialized_inputs", 0) == 0
            and not inconsistent
            and all(
                fact.record.verification == "verified" and fact.record.conflict != "unresolved"
                for fact in facts
            )
        )
        return PeerMetricValidation(
            ref=ref,
            metric_key=metric_key,
            record=record,
            target_ref=await self.target_ref(),
            facts=tuple(facts),
            inputs_verified=inputs_verified,
            inconsistent_sources=tuple(inconsistent),
        )

    async def _load_record(self, ref: ObjectRef) -> CalculationRecord:
        if ref.kind != "calculation":
            self._reject("a peer-context reference is a calculation reference")
        row = await self.session.get(SavedCalculation, (str(ref.id), ref.revision))
        if row is None or row.owner_id != self.owner_id:
            self._reject("the peer-context calculation is unavailable for this owner")
        try:
            record = CalculationRecord.model_validate(row.payload)
        except ValueError as exc:
            self._reject(f"the peer-context calculation payload is invalid: {exc}")
        if record.method != METHOD or record.method_version != METHOD_VERSION:
            self._reject("the calculation is not this method's record")
        if record.details.get("schema") != DETAILS_SCHEMA:
            self._reject("the calculation details are not this method's schema")
        if not record.applicable or record.errors:
            self._reject("the peer metric record is not an applicable, error-free record")
        if record.as_of > self.binding.cutoff or record.created_at > self.binding.cutoff:
            self._reject(
                "the peer metric record was created after the binding cutoff; "
                "a later result never enters an older task"
            )
        if not record.input_hash:
            self._reject("the peer metric record carries no input hash")
        return record

    def _details(self, record: CalculationRecord) -> dict[str, Any]:
        details = record.details
        if not isinstance(details, dict):
            self._reject("the peer metric record has no method details")
        return details

    def _metric_key(self, details: dict[str, Any]) -> str:
        metric_key = details.get("metric_key")
        if metric_key not in METRIC_KEYS:
            self._reject("the record's metric key is not one of the three fixed metrics")
        return str(metric_key)

    async def _identity(self, details: dict[str, Any]) -> None:
        """Target, period and cohort must match binding and submitted batch."""

        company_id, _listing_id = await self._target_identity()
        target = details.get("target")
        if not isinstance(target, dict):
            self._reject("the record carries no target identity")
        if target.get("company_id") != company_id or target.get("listing_id") != _listing_id:
            self._reject(
                "the record's target identity differs from the trusted binding's target; "
                "a peer result can never be re-labelled onto another target"
            )
        if details.get("report_period") != self.binding.report_period.isoformat():
            self._reject("the record's report period differs from the binding's report period")
        cohort = details.get("cohort")
        if (
            not isinstance(cohort, dict)
            or ObjectRef.model_validate(cohort.get("ref")) != self.binding.cohort_ref
        ):
            self._reject("the record was built against a different roster version")
        if cohort.get("content_hash") != self._batch_mapping().cohort_content_hash:
            self._reject("the record's roster content hash differs from the submitted batch")
        if self._batch_mapping().target_for(company_id) is None:
            self._reject("the bound batch holds no mapping entry for the bound target")

    def _binding_and_mapping(self, ref: ObjectRef, metric_key: str) -> None:
        """The reference is the binding's reference and the mapping's submission."""

        bound = next(
            (
                entry
                for entry in self.binding.metric_calculation_refs
                if entry.metric_key == metric_key
            ),
            None,
        )
        if bound is None or bound.state == "gap" or bound.ref is None or bound.ref != ref:
            self._reject(
                f"calculation reference {ref.id} is not the trusted binding's "
                f"{metric_key} reference"
            )
        target_entry = self._batch_mapping().target_for(self._target_company_id())
        if target_entry is None:
            self._reject("the bound batch holds no mapping entry for the bound target")
        submitted = target_entry.ref_for(metric_key)
        if (
            submitted is None
            or submitted.state == "gap"
            or submitted.ref is None
            or submitted.ref != ref
        ):
            self._reject(
                "the bound batch mapping does not submit this calculation for the "
                f"bound target ({metric_key}); batch membership is proven by the "
                "submitted mapping only"
            )

    def _audit(
        self, details: dict[str, Any], roster: dict[int, dict[str, Any]]
    ) -> dict[str, Any]:
        """Parse and structurally check the inclusion/exclusion audit.

        Roster membership, target exclusion and company dedup are decided
        here; counts and statistics are recomputed against the audit in
        :meth:`_statistics`.
        """

        included_raw = details.get("included_companies")
        excluded_raw = details.get("excluded_companies")
        if not isinstance(included_raw, list) or not isinstance(excluded_raw, list):
            self._reject("the record's inclusion/exclusion audit is missing")
        target = details.get("target")
        if not isinstance(target, dict) or not isinstance(target.get("company_id"), int):
            self._reject("the record carries no target identity")
        target_id = int(target["company_id"])

        included: list[dict[str, Any]] = []
        seen: set[int] = set()
        for entry in included_raw:
            if not isinstance(entry, dict):
                self._reject("the inclusion audit carries a malformed entry")
            self._roster_member(entry, roster)
            if int(entry["company_id"]) == target_id:
                self._reject("the target company appears among the included peers")
            if int(entry["company_id"]) in seen:
                self._reject(f"company {entry.get('company_id')} appears twice in the audit")
            seen.add(int(entry["company_id"]))
            included.append(entry)
        excluded: list[dict[str, Any]] = []
        target_exclusion: dict[str, Any] | None = None
        for entry in excluded_raw:
            if not isinstance(entry, dict):
                self._reject("the exclusion audit carries a malformed entry")
            self._roster_member(entry, roster)
            if bool(entry.get("is_target")):
                if target_exclusion is not None:
                    self._reject("the target exclusion appears twice in the audit")
                target_exclusion = entry
                continue
            if int(entry["company_id"]) == target_id:
                self._reject("the target company appears among the excluded peers")
            if int(entry["company_id"]) in seen:
                self._reject(f"company {entry.get('company_id')} appears twice in the audit")
            seen.add(int(entry["company_id"]))
            excluded.append(entry)
        return {
            "included": included,
            "excluded": excluded,
            "target_exclusion": target_exclusion,
            "target_id": target_id,
            "counts": {"valid_peer_count": len(included), "candidate_peer_count": len(seen)},
        }

    def _roster_member(
        self, entry: dict[str, Any], roster: dict[int, dict[str, Any]]
    ) -> dict[str, Any]:
        company_id = entry.get("company_id")
        if not isinstance(company_id, int):
            self._reject("audit entries carry an integer company_id")
        member = roster.get(company_id)
        if member is None:
            self._reject(
                f"company {company_id} ({entry.get('ticker')}) is not a member of the bound "
                "roster; peers come from the roster only (cross-industry samples are rejected)"
            )
        if isinstance(entry.get("listing_id"), int) and int(member["listing_id"]) != int(
            entry["listing_id"]
        ):
            self._reject(f"company {company_id} audit listing differs from its roster listing")
        return member

    async def _newer_cohort_revision(self) -> int | None:
        """The roster's newest revision when it is newer than the bound one.

        A roster change is a new snapshot row with ``revision + 1`` under the
        same ``cohort_key``; it never rewrites the bound snapshot. A published
        opinion resting on the old roster must go back to review.
        """

        bound = await self.session.get(PeerCohortSnapshot, str(self.binding.cohort_ref.id))
        if bound is None or bound.revision != self.binding.cohort_ref.revision:
            return None  # the version check itself belongs to verify (rejects)
        latest = await self.session.scalar(
            select(func.max(PeerCohortSnapshot.revision)).where(
                PeerCohortSnapshot.cohort_key == bound.cohort_key
            )
        )
        if latest is None or latest <= self.binding.cohort_ref.revision:
            return None
        return int(latest)

    def _exclusion_bases(
        self,
        details: dict[str, Any],
        spec: Any,
        facts: list[PeerFactValidation],
        audit: dict[str, Any],
    ) -> None:
        """Applicability exclusions need their field evidence; gaps stay gaps."""

        by_key = {(fact.company_id, fact.field, fact.role): fact for fact in facts}
        applicability_field = spec.applicability_field
        for entry in (*audit["excluded"], audit["target_exclusion"]):
            if entry is None or entry.get("reason") not in _APPLICABILITY_REASONS:
                continue
            fact = None
            if applicability_field is not None:
                fact = by_key.get((int(entry["company_id"]), applicability_field, "applicability"))
            if fact is None:
                self._reject(
                    f"applicability exclusion of company {entry.get('company_id')} "
                    f"({entry.get('reason')}) has no {applicability_field} field evidence; "
                    "a collection gap never proves a metric does not apply"
                )
            value = fact.record.value.value if fact.record.value is not None else None
            if value is None or value > 0:
                self._reject(
                    f"applicability exclusion of company {entry.get('company_id')} is not backed "
                    "by a non-positive saved field value"
                )
        target_exclusion = audit["target_exclusion"]
        if details.get("metric_status") == "target_not_applicable" and (
                target_exclusion is None
                or target_exclusion.get("reason") not in _APPLICABILITY_REASONS
        ):
            self._reject(
                "target_not_applicable requires an evidence-backed applicability exclusion; "
                "a collection gap only proves the field was not obtained this time"
            )

    def _values_from_facts(self, details: dict[str, Any], spec: Any, facts: list[PeerFactValidation], audit: dict[str, Any]) -> None:
        """The audit cannot certify its own numeric values independently of facts."""
        values = {(f.company_id, f.field): f.record.value.value for f in facts if f.record.value is not None}
        entries = [(int(e["company_id"]), Decimal(e["value"])) for e in audit["included"]]
        if details.get("target_value") is not None:
            entries.append((audit["target_id"], Decimal(details["target_value"])))
        for company_id, claimed in entries:
            numerator = values.get((company_id, spec.numerator_field))
            if numerator is None:
                self._reject("the metric value has no numeric source fact")
            expected = numerator
            if spec.denominator_field is not None:
                denominator = values.get((company_id, spec.denominator_field))
                if denominator is None or denominator <= 0:
                    self._reject("the metric value has no applicable denominator fact")
                expected = numerator / denominator
            if spec.needs_applicability_ref:
                eligibility = values.get((company_id, spec.applicability_field))
                if eligibility is None or eligibility <= 0:
                    self._reject("the metric value has no applicable equity fact")
            if claimed != expected:
                self._reject("the metric audit value disagrees with its source facts")

    def _statistics(
        self, record: CalculationRecord, details: dict[str, Any], spec: Any, audit: dict[str, Any]
    ) -> None:
        """Recompute counts, median, position and status from the audit."""

        if set(record.results) - RESULT_KEYS:
            self._reject("the record carries result keys outside the method's fixed set")
        for key in ("valid_peer_count", "candidate_peer_count"):
            quantity = record.results.get(key)
            if quantity is None or quantity.unit != _COUNT_UNIT or quantity.value is None:
                self._reject(f"the record's {key} result is missing or miscounted")
            if int(quantity.value) != audit["counts"][key]:
                self._reject(f"the record's {key} disagrees with the inclusion/exclusion audit")
        values = [Decimal(entry["value"]) for entry in audit["included"]]
        target_value = details.get("target_value")
        target_quantity = record.results.get("target")
        if target_value is None:
            if target_quantity is not None:
                self._reject("the record reports a target value its audit excludes")
            if audit["target_exclusion"] is None:
                self._reject("the target has neither value nor exclusion in the audit")
        else:
            if (
                target_quantity is None
                or target_quantity.value is None
                or target_quantity.value != Decimal(target_value)
                or target_quantity.unit != spec.unit
            ):
                self._reject("the record's target result disagrees with its audit")
        median_quantity = record.results.get("peer_median")
        position_quantity = record.results.get("ascending_position")
        if len(values) >= MIN_VALID_PEERS:
            if (
                median_quantity is None
                or median_quantity.value is None
                or median_quantity.value != median(values)
                or median_quantity.unit != spec.unit
            ):
                self._reject("the peer median is absent or disagrees with the audit")
        elif median_quantity is not None:
            self._reject("a peer median exists without the required number of valid peers")
        if median_quantity is not None and target_value is not None:
            expected = ascending_position(values, Decimal(target_value))
            if (
                position_quantity is None
                or position_quantity.value is None
                or position_quantity.value != expected
                or position_quantity.unit != _POSITION_UNIT
            ):
                self._reject("the ascending position is absent or disagrees with the audit")
        elif position_quantity is not None:
            self._reject("an ascending position exists without a median and an applicable target")
        target_exclusion = audit["target_exclusion"]
        exclusion = (
            PeerExclusion(
                company_id=audit["target_id"],
                ticker=str(target_exclusion.get("ticker", "")),
                name=str(target_exclusion.get("name", "")),
                reason=str(target_exclusion.get("reason")),
                detail=str(target_exclusion.get("detail", "")),
            )
            if target_exclusion is not None
            else None
        )
        expected_status, _reason = _metric_status(
            target_included=target_value is not None,
            target_exclusion=exclusion,
            valid_peer_count=len(values),
        )
        if details.get("metric_status") != expected_status.value:
            self._reject("the record's metric status disagrees with its audit")

    async def _facts(
        self,
        record: CalculationRecord,
        details: dict[str, Any],
        roster: dict[int, dict[str, Any]],
        spec: Any,
        audit: dict[str, Any],
        *,
        require_consistent_sources: bool,
        require_verified_inputs: bool,
    ) -> tuple[list[PeerFactValidation], list[tuple[str, str, str]], bool]:
        """Validate every cited fact per its real subject (checklist items 3-7).

        Returns the validated facts, the inconsistent-source reports and
        whether the manifest cites *every* input the audit requires (the
        full-citation that ``require_verified_inputs`` builds on).
        """

        declared_raw = details.get("fact_evidence")
        if not isinstance(declared_raw, list):
            self._reject("the record's fact evidence manifest is missing")
        declared: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in declared_raw:
            if not isinstance(entry, dict) or not isinstance(entry.get("ref"), dict):
                self._reject("the fact evidence manifest carries a malformed entry")
            manifest_ref = ObjectRef.model_validate(entry["ref"])
            declared[(str(manifest_ref.id), manifest_ref.revision)] = entry
        cited = {(str(fact.id), fact.revision): fact for fact in record.fact_refs}
        if set(declared) != set(cited):
            self._reject("the record's fact references and its evidence manifest differ")

        declared_keys = {
            (int(item["company_id"]), str(item.get("field")), str(item.get("role")))
            for item in declared.values()
            if isinstance(item.get("company_id"), int)
        }
        missing_required = self._required_fact_keys(spec, audit) - declared_keys
        if require_verified_inputs and missing_required:
            key = sorted(missing_required)[0]
            self._reject(
                f"a critical peer source must cite its whole input manifest; company {key[0]} "
                f"field {key[1]} ({key[2]}) is missing from the record's evidence manifest"
            )

        facts: list[PeerFactValidation] = []
        inconsistent: list[tuple[str, str, str]] = []
        for fact_ref in record.fact_refs:
            entry = declared[(str(fact_ref.id), fact_ref.revision)]
            fact = await self._one_fact(fact_ref, entry, roster, spec)
            report = await self._field_state(fact.company_id, fact.field)
            version = next(
                (item for item in report.versions if item.source_ref == fact.record.source_ref),
                None,
            )
            if version is None:
                self._reject(
                    f"the cited {fact.field} version of company {fact.company_id} is not among "
                    "the saved field versions for the bound period"
                )
            if version.state not in CLEAN_VERSION_STATES:
                inconsistent.append((str(entry.get("ticker")), fact.field, version.state))
            facts.append(fact)
        if require_consistent_sources and inconsistent:
            ticker, field, state = inconsistent[0]
            self._reject(
                f"peer field {field} of {ticker} is in state {state}; unresolved conflicts "
                "and undeclared corrections are excluded"
            )
        return facts, inconsistent, not missing_required

    def _required_fact_keys(
        self, spec: Any, audit: dict[str, Any]
    ) -> set[tuple[int, str, str]]:
        """The ``(company_id, field, role)`` entries the audit must cite.

        Mirror of the save-side requirement: every included company's value
        inputs (plus the applicability field where the metric needs it), the
        included target's inputs, and the field evidence behind every
        applicability exclusion.
        """

        keys: set[tuple[int, str, str]] = set()

        def value_keys(company_id: int) -> None:
            keys.add((company_id, spec.numerator_field, "value"))
            if spec.denominator_field is not None:
                keys.add((company_id, spec.denominator_field, "value"))
            if spec.needs_applicability_ref and spec.applicability_field is not None:
                keys.add((company_id, spec.applicability_field, "applicability"))

        for entry in audit["included"]:
            value_keys(int(entry["company_id"]))
        if audit["target_exclusion"] is None:
            value_keys(audit["target_id"])
        for entry in (*audit["excluded"], audit["target_exclusion"]):
            if (
                entry is not None
                and entry.get("reason") in _APPLICABILITY_REASONS
                and spec.applicability_field is not None
            ):
                keys.add((int(entry["company_id"]), spec.applicability_field, "applicability"))
        return keys

    async def _one_fact(
        self,
        ref: ObjectRef,
        entry: dict[str, Any],
        roster: dict[int, dict[str, Any]],
        spec: Any,
    ) -> PeerFactValidation:
        """Checklist items 3-7 for one cited fact, per its real subject."""

        company_id = entry.get("company_id")
        field = entry.get("field")
        role = entry.get("role")
        if not isinstance(company_id, int) or not isinstance(field, str) or not isinstance(role, str):
            self._reject("the fact evidence manifest entries are malformed")
        member = roster.get(company_id)
        if member is None:
            self._reject(f"fact evidence cites company {company_id}, outside the bound roster")
        if role == "applicability":
            if spec.applicability_field is None or field != spec.applicability_field:
                self._reject(
                    f"fact evidence role applicability requires the {spec.applicability_field} field"
                )
        elif role == "value":
            if field not in {spec.numerator_field, spec.denominator_field}:
                self._reject(f"field {field} is not a value input of metric {spec.metric_key}")
        else:
            self._reject(f"unknown fact evidence role {role!r}")

        row = await self.session.get(SavedEvidence, (str(ref.id), ref.revision))
        if row is None or row.owner_id != self.owner_id:
            self._reject("a cited fact of the peer record is unavailable for this owner")
        try:
            fact = EvidenceRecord.model_validate(row.payload)
        except ValueError as exc:
            self._reject(f"a cited fact payload is invalid: {exc}")
        if fact.source_ref.kind != "peer_annual_snapshot":
            self._reject("peer metric facts cite peer annual snapshot evidence only")
        if fact.subject_ref.kind != "listing" or fact.subject_ref.revision != 1:
            self._reject("peer metric facts carry an exact listing subject")
        if int(fact.subject_ref.id) != int(member["listing_id"]):
            self._reject(
                f"fact for company {company_id} ({field}) cites listing {fact.subject_ref.id} "
                f"but the roster binds listing {member['listing_id']}; a subject_ref rewrite "
                "cannot disguise a peer fact as another company's fact"
            )
        if fact.source_ref.field_path != f"fields.{field}":
            self._reject(
                f"fact source locator {fact.source_ref.field_path} does not match field {field}"
            )
        snapshot = await self.session.get(PeerAnnualSnapshot, str(fact.source_ref.id))
        if (
            snapshot is None
            or snapshot.listing_id != int(member["listing_id"])
            or snapshot.report_period != self.binding.report_period
        ):
            self._reject("the cited fact's snapshot version does not match the bound period and roster")
        rederived = peer_field_evidence(snapshot, field)
        if rederived.record is not None and rederived.record.verification == "verified":
            from investment_agent.industry_peers.originals import validate_field_original
            if not await validate_field_original(self.session, snapshot, field, cutoff=self.binding.cutoff):
                self._reject("peer original proof does not match the saved original bytes")
        expected = rederived.record
        # The store's own trusted-branch comparison: the persisted fact must
        # re-derive from its snapshot field byte for byte (identity fields
        # aside - the store assigns id/created_at/actor at save time).
        if expected is None or rederived.reason is not None:
            self._reject(
                "the cited fact's snapshot field is not materializable; the fact stays "
                "in the snapshot and can never be cited evidence"
            )
        for name in (
            "subject_ref",
            "period",
            "source_ref",
            "locator",
            "value",
            "text",
            "published_at",
            "acquired_at",
            "available_from",
            "content_hash",
            "verification",
            "conflict",
            "date_precision",
            "document_provenance",
        ):
            if getattr(expected, name) != getattr(fact, name):
                self._reject(
                    "the cited fact is not the trusted re-derivation of its snapshot field; "
                    "original-document proof status is fabricated or stale"
                )
        if fact.period != _expected_period(field, self.binding.report_period):
            self._reject(f"fact period {fact.period} does not follow the field's metric semantics")
        expected_unit, expected_currency = _expected_field_unit(field)
        if (
            fact.value is None
            or fact.value.unit != expected_unit
            or fact.value.currency != expected_currency
        ):
            self._reject(f"fact {field} does not carry the field's canonical unit and currency")
        if (
            max(fact.published_at, fact.acquired_at, fact.available_from, fact.created_at)
            > self.binding.cutoff
        ):
            self._reject(
                "a cited fact was acquired or published after the binding cutoff; "
                "later knowledge never enters an older task"
            )
        return PeerFactValidation(ref=ref, record=fact, company_id=company_id, field=field, role=role)


def metric_digest(
    validation: PeerMetricValidation,
) -> tuple[dict[str, Any], str, str]:
    """The WP13 fixed model-facing digest of one verified metric record.

    Returns ``(digest, summary_hash, manifest_hash)``. The digest carries the
    metric definition, target, period, results with units, sample counts, the
    inclusion rule, exclusion reason categories with counts (never a
    per-company value table - the identity table stays page-only), check
    coverage, limitations and the three hashes. No strength ranking
    (stronger/weaker), no derived channel-inventory / sales / competitive
    advantage / expected-return field exists: the digest describes the fixed
    sample statistics and their audit only.

    * ``manifest_hash`` covers the complete input manifest and exclusion
      audit - the caller's material set hash binds it (delivered inside the
      digest), so a changed input list changes the material set hash even
      though the model-visible text did not grow.
    * ``summary_hash`` covers exactly the digest without its own hash - the
      model-summary hash recorded separately from the material set hash.
    """

    record = validation.record
    details = record.details if isinstance(record.details, dict) else {}
    spec = metric_spec(validation.metric_key)
    raw_target = details.get("target")
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    exclusion_counts: dict[str, int] = {}
    for entry in details.get("excluded_companies", []) if isinstance(details.get("excluded_companies"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("reason"), str):
            exclusion_counts[entry["reason"]] = exclusion_counts.get(entry["reason"], 0) + 1
    verification_counts: dict[str, int] = {}
    for fact in validation.facts:
        verification_counts[fact.record.verification] = (
            verification_counts.get(fact.record.verification, 0) + 1
        )
    unmaterialized = int(details.get("unmaterialized_inputs", 0) or 0)
    manifest_payload = {
        "schema": MANIFEST_HASH_SCHEMA,
        "input_hash": record.input_hash,
        "fact_evidence": details.get("fact_evidence", []),
        "included_companies": details.get("included_companies", []),
        "excluded_companies": details.get("excluded_companies", []),
        "unmaterialized_inputs": unmaterialized,
    }
    manifest_hash = _canonical_hash(manifest_payload)
    valid_quantity = record.results["valid_peer_count"]
    candidate_quantity = record.results["candidate_peer_count"]
    assert valid_quantity.value is not None and candidate_quantity.value is not None
    digest: dict[str, Any] = {
        "summary_schema": DIGEST_SCHEMA,
        "id": str(record.id),
        "revision": record.revision,
        "method": METHOD,
        "method_version": record.method_version,
        "metric_key": validation.metric_key,
        "definition": spec.formula,
        "unit": spec.unit,
        "position_rule": POSITION_RULE,
        "target_ticker": str(target.get("ticker", "")),
        "report_period": details.get("report_period"),
        "metric_status": details.get("metric_status"),
        "results": {
            key: {"value": None if quantity.value is None else str(quantity.value), "unit": quantity.unit}
            for key, quantity in record.results.items()
        },
        "sample": {
            "valid_peer_count": int(valid_quantity.value),
            "candidate_peer_count": int(candidate_quantity.value),
        },
        "inclusion_rule": INCLUSION_RULE,
        "exclusion_reasons": dict(sorted(exclusion_counts.items())),
        "verification_counts": dict(sorted(verification_counts.items())),
        "unmaterialized_inputs": unmaterialized,
        "limitations": tuple(record.quality),
        "input_hash": record.input_hash,
        "manifest_hash": manifest_hash,
    }
    summary_hash = _canonical_hash(digest)
    digest["summary_hash"] = summary_hash
    return digest, summary_hash, manifest_hash


_METHOD_ENTRIES: dict[str, type[PeerContextResolver]] = {METHOD: PeerContextResolver}
"""The whole unified-method-entry registration: exactly this one method.
No other method is registered, and there is no runtime registration API."""


__all__ = [
    "CLEAN_VERSION_STATES",
    "DIGEST_SCHEMA",
    "DISPLAY_RULES",
    "INCLUSION_RULE",
    "MANIFEST_HASH_SCHEMA",
    "RESULT_KEYS",
    "RENEWAL_BLOCK_REASON",
    "PeerContextResolver",
    "PeerFactValidation",
    "PeerMetricValidation",
    "PeerReviewMaterial",
    "metric_digest",
    "peer_method_entry",
    "peer_method_payload",
    "registered_display",
]
