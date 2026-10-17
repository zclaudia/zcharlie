"""Frozen local assessment orchestration. Callers own admission and transactions."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)
from investment_agent.storage.repositories.supplemental_rules import SupplementalRulesRepository
from investment_agent.supplemental_rules.canonical import (
    canonical_hash,
    rule_semantic_hash,
    stable_series_key,
)
from investment_agent.supplemental_rules.contracts import (
    ApplicationMode,
    Context,
    EvaluationRequest,
    FrozenManifest,
    FrozenPair,
    ResearchEvent,
    RuleApplication,
    RuleDefinition,
)
from investment_agent.supplemental_rules.evaluation import EVALUATOR_VERSION
from investment_agent.supplemental_rules.issues import project_issue_groups
from investment_agent.supplemental_rules.matching import MATCHER_VERSION
from investment_agent.supplemental_rules.read_snapshot import ensure_read_snapshot
from investment_agent.supplemental_rules.views import ApplicationView, CurrentApplicationsView

SCAN_LIMIT = 2000
ZERO_HASH = "0" * 64


def _current_page(apps: tuple[RuleApplication, ...], listing_ref: ObjectRef, context: Context,
                  scope: str, cursor: str | None, limit: int) -> tuple[int, str | None]:
    signature = canonical_hash({"apps": [(a.id, a.input_hash) for a in apps], "scope": scope})
    offset = 0
    if cursor:
        try:
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(value, dict) or set(value) != {"listing", "context", "hash", "offset"}:
                raise ValueError("cursor must be an object")
            if type(value["listing"]) is not int or not isinstance(value["context"], str) or not isinstance(value["hash"], str):
                raise ValueError("cursor identity shape")
            if value["listing"] != listing_ref.id or value["context"] != context or value["hash"] != signature:
                raise InvestorError("revision_conflict", "当前应用集合已变化，请刷新")
            offset = value["offset"]
            if type(offset) is not int or not 0 <= offset <= len(apps):
                raise ValueError("offset")
        except (ValueError, KeyError, TypeError) as exc:
            raise InvestorError("invalid_cursor", "分页标识无效") from exc
    next_offset = min(offset + limit, len(apps))
    next_cursor = base64.urlsafe_b64encode(json.dumps({"listing": listing_ref.id,
        "context": context, "hash": signature, "offset": next_offset}).encode()).decode() if next_offset < len(apps) else None
    return offset, next_cursor


def _factor_names(node: Any) -> set[str]:
    if hasattr(node, "factor_id"):
        return {str(node.factor_id)}
    return set().union(*(_factor_names(child) for child in getattr(node, "conditions", ())))


def _selector_sources(node: Any) -> tuple[ObjectRef, ...]:
    if getattr(node, "source_ref", None) is not None:
        return (node.source_ref,)
    if getattr(node, "expression", None) is not None:
        return _selector_sources(node.expression)
    return tuple(ref for term in getattr(node, "terms", ()) for ref in _selector_sources(term))


def manifest_digest(manifest: FrozenManifest) -> str:
    return canonical_hash(manifest.model_dump(mode="python", exclude={"input_hash", "manifest_hash", "evaluated_at"}))


def validate_manifest(manifest: FrozenManifest) -> FrozenManifest:
    if manifest.manifest_hash != manifest_digest(manifest) or manifest.input_hash != manifest.manifest_hash:
        raise InvestorError("historical_manifest_unavailable", "补充事项冻结清单摘要不匹配")
    identities = [(p.listing_ref, p.rule_ref.id, p.context) for p in manifest.pairs]
    if len(identities) != len(set(identities)):
        raise InvestorError("historical_manifest_unavailable", "冻结清单包含重复规则序列")
    for pair in manifest.pairs:
        expected = canonical_hash(pair.model_dump(mode="python", exclude={"input_hash"}))
        if pair.input_hash != expected or pair.listing_ref not in manifest.listing_refs or pair.context not in manifest.contexts:
            raise InvestorError("historical_manifest_unavailable", "冻结成员身份或摘要不匹配")
    if manifest.matcher_version != MATCHER_VERSION or manifest.evaluator_version != EVALUATOR_VERSION:
        raise InvestorError("historical_manifest_unavailable", "冻结清单的方法版本不可用")
    return manifest


def _subject_semantic(value: Any) -> Any:
    """Exclude observation-clock fields, retaining source acquisition identities."""
    if isinstance(value, dict):
        return {key: _subject_semantic(item) for key, item in value.items() if key not in {"as_of", "observed_at"}}
    if isinstance(value, (tuple, list)):
        return [_subject_semantic(item) for item in value]
    return value


def relevant_event(event: ResearchEvent, listing_ref: ObjectRef, company_id: int, ticker: str) -> bool:
    # An unspecified scope stays pending; it is not evidence of irrelevance.
    if not event.affected_subjects:
        return True
    identities = {str(listing_ref.id), str(company_id), ticker, f"listing:{listing_ref.id}", f"company:{company_id}"}
    return bool(identities.intersection(event.affected_subjects))


def scope_digest(manifest: FrozenManifest, listing_ref: ObjectRef, context: Context) -> str:
    pairs = [p for p in manifest.pairs if p.listing_ref == listing_ref and p.context == context
             and (p.match_status != "not_applicable" or p.prior_open_issue_refs)]
    subject = pairs[0].subject if pairs else None
    events = [e for e in manifest.unbound_events if subject is None or relevant_event(e, listing_ref, subject.company_id, subject.ticker)]
    return canonical_hash({
        "rules": [{"id": p.rule_ref.id, "semantic": rule_semantic_hash(p.rule), "state": p.rule_state,
                   "match": p.matching_manifest_hash, "event": p.selected_event,
                   "subject": _subject_semantic(p.subject.model_dump(mode="python"))} for p in pairs],
        "unbound_events": events, "scan_diagnostics": manifest.scan_diagnostics,
    })


class SupplementalRuleService:
    scope_hash = staticmethod(scope_digest)

    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        if owner != "local_owner":
            raise InvestorError("not_found", "补充事项不存在")
        self.session = session
        self.owner: OwnerId = "local_owner"
        self.store = SupplementalRulesRepository(session)
        self.assessments = InvestmentAssessmentRepository(session)

    async def latest_live(self, listing_ref: ObjectRef, context: Context, *, allow_partial: bool = False) -> tuple[RuleApplication, ...]:
        newer = aliased(InvestmentAssessment)
        row = InvestmentAssessment
        statement = select(row).where(
            row.owner_id == self.owner, row.kind == "rule_application", row.listing_id == listing_ref.id,
            row.application_mode == "live", row.payload["context"].as_string() == context,
            ~exists(select(newer.id).where(newer.owner_id == row.owner_id, newer.kind == "rule_application",
                 newer.listing_id == row.listing_id, newer.series_key == row.series_key,
                 newer.application_mode == "live", newer.application_sequence > row.application_sequence)),
        ).order_by(row.series_key).limit(SCAN_LIMIT + 1)
        rows = list((await self.session.scalars(statement)).all())
        if len(rows) > SCAN_LIMIT and not allow_partial:
            raise InvestorError("source_capacity_exceeded", "补充事项历史序列超过有界读取上限")
        rows = rows[:SCAN_LIMIT]
        values = []
        for saved in rows:
            value = await self.assessments.get_exact(self.owner, ObjectRef(kind="rule_application", id=UUID(saved.id), revision=1), expected_kind="rule_application")
            if not isinstance(value, RuleApplication):
                raise InvestorError("invalid_reference", "保存的补充事项序列不可读")
            values.append(value)
        return tuple(values)

    async def freeze(self, request: EvaluationRequest, *, as_of: datetime) -> FrozenManifest:
        try:
            return await self._freeze(request, as_of=as_of)
        except ValidationError as exc:
            if "manifest exceeds" not in str(exc) and not any(e["type"] in {"too_long", "string_too_long"} for e in exc.errors()):
                raise
            raise InvestorError("source_capacity_exceeded", "冻结清单超过容量上限") from exc

    async def _freeze(self, request: EvaluationRequest, *, as_of: datetime) -> FrozenManifest:
        from investment_agent.supplemental_rules.inputs import freeze_subject
        from investment_agent.supplemental_rules.matching import match_subject

        if request.mode != "live":
            raise InvestorError("historical_manifest_unavailable", "历史回放须经授权run接收路径")
        if len(set(request.listing_refs)) != len(request.listing_refs):
            raise InvestorError("invalid_reference", "评估证券不能重复")
        registry = await self.store.get_registry_head(self.owner)
        active, overflow = await self.store.list_active_rules(self.owner, limit=SCAN_LIMIT)
        if overflow:
            raise InvestorError("source_capacity_exceeded", "规则候选集合超过冻结上限")
        events, event_overflow = await self.store.list_events_visible_by(self.owner, cutoff=as_of, visible_at=as_of, limit=SCAN_LIMIT)
        event_by_id = {e.id: e for e in events}
        scan = ["events_scan_incomplete"] if event_overflow else []
        rules = {r.id: r for r in active}
        for ref in request.exact_rule_refs:
            rule = await self.store.get_rule_revision(self.owner, ref)
            if rule is None:
                raise InvestorError("invalid_reference", "指定规则不存在")
            if rule.id not in rules or rules[rule.id].revision != rule.revision:
                raise InvestorError("revision_conflict", "指定规则已非当前生效版本")
        priors_requested: dict[UUID, RuleApplication] = {}
        for ref in request.prior_application_refs:
            old = await self.assessments.get_exact(self.owner, ref, expected_kind="rule_application")
            if not isinstance(old, RuleApplication) or old.application_mode != "live" or old.listing_ref not in request.listing_refs or old.context not in request.contexts:
                raise InvestorError("invalid_reference", "原问题主体、用途或模式不匹配")
            if request.evaluation_purpose == "issue_review" and not old.open_issue_refs:
                raise InvestorError("invalid_reference", "问题复核必须引用已有未决事项")
            priors_requested[old.id] = old
        pairs: list[FrozenPair] = []
        unbound: dict[UUID, ResearchEvent] = {}
        for listing_ref in sorted(request.listing_refs, key=lambda ref: int(ref.id)):
            for context in sorted(request.contexts):
                previous = {a.rule_ref.id: a for a in await self.latest_live(listing_ref, context)}
                candidates = {rule.id: rule for rule in active if context in rule.contexts and rule.valid_from <= as_of and (rule.valid_until is None or as_of < rule.valid_until)}
                for earlier in previous.values():
                    if earlier.open_issue_refs or earlier.id in priors_requested:
                        candidates.setdefault(UUID(str(earlier.rule_ref.id)), earlier.frozen_pair.rule)
                factors = set()
                listing = await self.session.get(Listing, int(listing_ref.id))
                # Nonmatching exact identities must not contribute foreign
                # sources/factors to another listing's shared input snapshot.
                relevant_rules = [rule for rule in candidates.values()
                    if not (rule.selector.kind == "listing" and rule.selector.listing_ref != listing_ref)
                    and not (rule.selector.kind == "company" and listing is not None
                        and rule.selector.company_id != listing.company_id)]
                for rule in relevant_rules:
                    factors.update(_factor_names(getattr(rule.definition, "condition", None)))
                evidence_refs = tuple(dict.fromkeys(ref for r in relevant_rules for ref in (*r.source_refs, *_selector_sources(r.selector))))
                subject = await freeze_subject(self.session, listing_ref, as_of, owner_id=self.owner, factor_names=tuple(sorted(factors)), evidence_refs=evidence_refs, strict_source_subjects=False)
                scan.extend(f"listing:{listing_ref.id}:{diagnostic}" for diagnostic in subject.diagnostics
                    if "truncated" in diagnostic or "mutated_after_cutoff" in diagnostic)
                relevant = [e for e in events if relevant_event(e, listing_ref, subject.company_id, subject.ticker)]
                bound_ids = {r.event_ref.id for r in relevant_rules if r.event_ref}
                unbound.update({e.id: e for e in relevant if e.id not in bound_ids})
                for rule_id, rule in sorted(candidates.items(), key=lambda item: str(item[0])):
                    prior = previous.get(rule_id)
                    head = await self.store.get_rule_head(self.owner, rule_id)
                    state = head.active_state if head and head.active_state else "retired"
                    if head is not None and head.active_revision != rule.revision:
                        state = "retired"
                    if state == "active" and rule.valid_until is not None and as_of >= rule.valid_until:
                        state = "expired"
                    elif state == "active" and as_of >= rule.review_due_at:
                        state = "review_due"
                    match = match_subject(rule.selector, subject)
                    issue_keys = []
                    for issue_ref in prior.open_issue_refs if prior else ():
                        issue = await self.assessments.get_exact(self.owner, issue_ref, expected_kind="rule_application")
                        if not isinstance(issue, RuleApplication):
                            raise InvestorError("invalid_reference", "历史未决事项原件缺失")
                        issue_keys.append(issue.frozen_pair.rule.issue_key or str(issue.rule_ref.id))
                    data = dict(ordinal=len(pairs), listing_ref=listing_ref, context=context, rule_ref=rule.reference,
                        rule=rule, subject=subject, selected_event=event_by_id.get(UUID(str(rule.event_ref.id))) if rule.event_ref else None,
                        prior_live_application_ref=prior.reference if prior else None,
                        prior_open_issue_refs=prior.open_issue_refs if prior else (), prior_issue_keys=tuple(sorted(set(issue_keys))),
                        prior_input_hash=prior.input_hash if prior else None, rule_state=state,
                        match_status=match.status, matching_manifest_hash=match.manifest_hash, diagnostics=match.diagnostics,
                        input_hash=ZERO_HASH)
                    pair = FrozenPair.model_validate(data)
                    pairs.append(pair.model_copy(update={"input_hash": canonical_hash(pair.model_dump(mode="python", exclude={"input_hash"}))}))
                    if len(pairs) > SCAN_LIMIT:
                        raise InvestorError("source_capacity_exceeded", "补充事项声明成员超过上限")
        manifest = FrozenManifest(owner_id=self.owner, listing_refs=request.listing_refs, contexts=request.contexts,
            as_of=as_of, evaluated_at=as_of, rules_generation=registry.rules_generation,
            events_generation=registry.events_generation, matcher_version=MATCHER_VERSION, evaluator_version=EVALUATOR_VERSION,
            pairs=tuple(pairs), unbound_events=tuple(sorted(unbound.values(), key=lambda e: str(e.id))),
            scan_diagnostics=tuple(sorted(set(scan))), scope_semantic_hash=ZERO_HASH, input_hash=ZERO_HASH, manifest_hash=ZERO_HASH)
        manifest = manifest.model_copy(update={"scope_semantic_hash": canonical_hash([scope_digest(manifest, ref, context) for ref in manifest.listing_refs for context in sorted(manifest.contexts)])})
        value = manifest_digest(manifest)
        return validate_manifest(FrozenManifest.model_validate(manifest.model_copy(update={"input_hash": value, "manifest_hash": value}).model_dump(mode="python")))

    async def save_pair(self, manifest: FrozenManifest, pair: FrozenPair, *, operation_id: UUID, mode: ApplicationMode = "live", evaluation_purpose: Literal["coverage", "issue_review"] = "coverage", origin_ref: ObjectRef | None = None) -> RuleApplication:
        from investment_agent.supplemental_rules.evaluation import evaluate_pair

        validate_manifest(manifest)
        if pair not in manifest.pairs:
            raise InvestorError("invalid_reference", "成员不属于冻结批次")
        series = stable_series_key(self.owner, int(pair.listing_ref.id), UUID(str(pair.rule_ref.id)), pair.context)
        reference = ObjectRef(kind="rule_application", id=uuid5(operation_id, series), revision=1)
        fingerprint = canonical_hash({"pair": pair, "manifest_hash": manifest.manifest_hash, "mode": mode, "purpose": evaluation_purpose})
        saved = await self.assessments.get_exact(self.owner, reference, expected_kind="rule_application")
        if isinstance(saved, RuleApplication):
            if saved.input_hash != fingerprint or saved.operation_id != operation_id or saved.origin_ref != origin_ref:
                raise InvestorError("idempotency_conflict", "应用已绑定另一冻结输入")
            return saved
        current = next((a for a in await self.latest_live(pair.listing_ref, pair.context) if a.series_key == series), None)
        if mode == "live" and (current.reference if current else None) != pair.prior_live_application_ref:
            raise InvestorError("item_binding_conflict", "前序应用已推进，请重新接收评估")
        maximum = await self.session.scalar(select(func.max(InvestmentAssessment.application_sequence)).where(
            InvestmentAssessment.owner_id == self.owner, InvestmentAssessment.kind == "rule_application",
            InvestmentAssessment.listing_id == pair.listing_ref.id, InvestmentAssessment.series_key == series))
        result = evaluate_pair(pair, prior_open_issue_refs=pair.prior_open_issue_refs)
        open_refs = result.open_issue_refs
        issue_key = pair.rule.issue_key or str(pair.rule_ref.id)
        if result.assessment_status in {"unresolved", "inconclusive"} and issue_key not in pair.prior_issue_keys:
            open_refs = tuple(dict.fromkeys((*open_refs, reference)))
        result = result.model_copy(update={"open_issue_refs": open_refs})
        at = datetime.now(UTC)
        app = RuleApplication(id=UUID(str(reference.id)), operation_id=operation_id, owner_id=self.owner,
            listing_ref=pair.listing_ref, company_id=pair.subject.company_id, context=pair.context, rule_ref=pair.rule_ref,
            rules_generation=manifest.rules_generation, events_generation=manifest.events_generation,
            matching_manifest_hash=pair.matching_manifest_hash, event_ref=ObjectRef(kind="research_event", id=pair.selected_event.id, revision=pair.selected_event.revision) if pair.selected_event else None,
            as_of=manifest.as_of, evaluated_at=at, valid_until=pair.rule.valid_until, review_due_at=pair.rule.review_due_at,
            input_hash=fingerprint, matcher_version=manifest.matcher_version, evaluator_version=manifest.evaluator_version,
            dependencies=tuple(dict.fromkeys((pair.rule_ref, *pair.rule.source_refs))), series_key=series,
            application_sequence=(maximum or 0)+1, application_mode=mode, authorization="mechanical", origin_ref=origin_ref, created_at=at,
            actor="supplemental_mechanical_worker", previous_application_ref=pair.prior_live_application_ref,
            frozen_pair=pair, source_manifest_hash=manifest.manifest_hash, evaluation_purpose=evaluation_purpose,
            applicability=result.applicability, condition_result=result.condition_result, evidence_status=result.evidence_status,
            assessment_status=result.assessment_status, semantic_status=result.semantic_status, semantic_review_ref=None,
            effective_effects=tuple(e.model_dump(mode="json") for e in result.effective_effects), open_issue_refs=open_refs,
            closure_refs=(), result=result, payload={"scope_semantic_hash": scope_digest(manifest, pair.listing_ref, pair.context)})
        return await self.assessments.save_rule_application(self.owner, app)

    async def affected_listings(self, rule_or_event: RuleDefinition | ResearchEvent, limit: int = 100) -> tuple[tuple[ObjectRef, ...], tuple[str, ...]]:
        ids = set((await self.session.scalars(select(InvestmentAssessment.listing_id).where(
            InvestmentAssessment.owner_id == self.owner, InvestmentAssessment.kind == "rule_application",
            InvestmentAssessment.application_mode == "live").distinct().order_by(InvestmentAssessment.listing_id).limit(limit + 1))).all())
        if isinstance(rule_or_event, RuleDefinition):
            selector = rule_or_event.selector
            if selector.kind == "listing":
                ids.add(int(selector.listing_ref.id))
            elif selector.kind == "company":
                ids.update((await self.session.scalars(select(Listing.id).where(Listing.company_id == selector.company_id).order_by(Listing.id).limit(limit + 1))).all())
        else:
            tokens = {str(s) for s in rule_or_event.affected_subjects}
            explicit = [int(s.split(":")[-1]) for s in tokens if s.startswith("listing:") and s.split(":")[-1].isdigit()]
            ids.update(explicit)
        values = sorted(i for i in ids if i is not None)
        present = set((await self.session.scalars(select(Listing.id).where(Listing.id.in_(values)))).all()) if values else set()
        diagnostics = ("coverage_pending", "uncovered_count_unknown") if len(values) > limit or len(present) != len(values) or (isinstance(rule_or_event, RuleDefinition) and rule_or_event.selector.kind == "category") else ()
        return tuple(ObjectRef(kind="listing", id=i, revision=1) for i in values[:limit] if i in present), diagnostics

    async def freeze_reassessment(self, source: RuleDefinition | ResearchEvent, *, operation_id: UUID, as_of: datetime) -> FrozenManifest:
        """Bounded post-mutation work; lack of capacity cannot prevent retirement."""
        refs, diagnostics = await self.affected_listings(source)
        contexts: frozenset[Context] = source.contexts if isinstance(source, RuleDefinition) else frozenset({"company_research", "candidate", "account_fit", "holding_review"})
        while refs:
            try:
                request = EvaluationRequest(idempotency_key=operation_id, listing_refs=refs, contexts=contexts, mode="live")
                manifest = await self.freeze(request, as_of=as_of)
                manifest = manifest.model_copy(update={"scan_diagnostics": tuple(sorted(set((*manifest.scan_diagnostics, *diagnostics))))})
                value = manifest_digest(manifest)
                return validate_manifest(manifest.model_copy(update={"input_hash": value, "manifest_hash": value}))
            except InvestorError as exc:
                if exc.detail.code not in {"source_capacity_exceeded", "invalid_reference", "unsupported_subject"}:
                    raise
                refs = refs[:len(refs)//2]
                diagnostics = tuple(sorted(set((*diagnostics, "coverage_pending", "uncovered_count_unknown"))))
        registry = await self.store.get_registry_head(self.owner)
        manifest = FrozenManifest(owner_id=self.owner, listing_refs=(), contexts=contexts, as_of=as_of, evaluated_at=as_of,
            rules_generation=registry.rules_generation, events_generation=registry.events_generation,
            matcher_version=MATCHER_VERSION, evaluator_version=EVALUATOR_VERSION, pairs=(),
            unbound_events=(source,) if isinstance(source, ResearchEvent) else (), scan_diagnostics=diagnostics,
            scope_semantic_hash=canonical_hash({"zero_target": source.id}), input_hash=ZERO_HASH, manifest_hash=ZERO_HASH)
        value = manifest_digest(manifest)
        return validate_manifest(manifest.model_copy(update={"input_hash": value, "manifest_hash": value}))

    async def exact(self, reference: ObjectRef) -> ApplicationView:
        from investment_agent.supplemental_rules.current_limits import application_limits
        await ensure_read_snapshot(self.session)
        app = await self.assessments.get_exact(self.owner, reference, expected_kind="rule_application")
        if not isinstance(app, RuleApplication):
            raise InvestorError("not_found", "补充事项应用不存在")
        return ApplicationView(payload=app, current_limits=await application_limits(self, app))

    async def current(self, listing_ref: ObjectRef, context: Context, *, cursor: str | None = None, limit: int = 100) -> CurrentApplicationsView:
        from investment_agent.supplemental_rules.current_limits import application_limits
        await ensure_read_snapshot(self.session)
        if not 1 <= limit <= 100:
            raise InvestorError("invalid_cursor", "分页大小须为1至100")
        at = datetime.now(UTC)
        request = EvaluationRequest(idempotency_key=uuid4(), listing_refs=(listing_ref,), contexts=frozenset({context}), mode="live")
        try:
            manifest = await self.freeze(request, as_of=at)
        except InvestorError as exc:
            if exc.detail.code != "source_capacity_exceeded":
                raise
            from investment_agent.supplemental_rules.views import CurrentLimits
            apps = await self.latest_live(listing_ref, context, allow_partial=True)
            restricted = CurrentLimits(current_status="unknown", reasons=("coverage_pending",), refresh_required=True, coverage_pending=True)
            offset, next_cursor = _current_page(apps, listing_ref, context, "source_capacity_exceeded", cursor, limit)
            page = apps[offset:offset+limit]
            return CurrentApplicationsView(listing_ref=listing_ref, context=context,
                applications=tuple(ApplicationView(payload=app, current_limits=restricted) for app in page),
                issue_groups=project_issue_groups(apps, {app.reference for app in page}),
                cursor=next_cursor, coverage_complete=False, diagnostics=("coverage_pending", "source_capacity_exceeded"), observed_at=at)
        apps = await self.latest_live(listing_ref, context)
        scope = scope_digest(manifest, listing_ref, context)
        offset, next_cursor = _current_page(apps, listing_ref, context, scope, cursor, limit)
        page = apps[offset:offset+limit]
        latest_refs = frozenset(a.reference for a in apps)
        all_views = tuple([ApplicationView(payload=a, current_limits=await application_limits(self, a, manifest=manifest, current_apps=apps, now=at, scope_fingerprint=scope, latest_refs=latest_refs)) for a in apps])
        views = all_views[offset:offset+limit]
        expected = {(p.rule_ref.id, p.context) for p in manifest.pairs}
        present = {(a.rule_ref.id, a.context) for a in apps}
        complete = expected.issubset(present) and not manifest.scan_diagnostics and not manifest.unbound_events and all(not v.current_limits.refresh_required for v in all_views if v.current_limits)
        mixed = len({a.origin_ref for a in apps}) > 1 or len({a.as_of for a in apps}) > 1
        return CurrentApplicationsView(listing_ref=listing_ref, context=context, applications=views,
            issue_groups=project_issue_groups(apps, {app.reference for app in page}),
            unbound_events=manifest.unbound_events, coverage_complete=complete and not mixed,
            mixed=mixed, diagnostics=manifest.scan_diagnostics, cursor=next_cursor, observed_at=at)
