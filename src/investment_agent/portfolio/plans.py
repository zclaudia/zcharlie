"""AG23 trusted publication using AG12's prepared-action/claim transaction.

Plans own a separate artifact table because a cash account need not have a Case.
The model supplies business alternatives; saved arithmetic and current authority
supply quantities, checks and rendering. No acceptance locks funds or trades.
"""
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import check_boundary
from investment_agent.agent.tasks import ActionResult, TaskClaim, TaskService, fingerprint
from investment_agent.config import Settings
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, Quantity
from investment_agent.investor.contracts import EntityNode, NumericNode, PublicationCheck, TextNode
from investment_agent.investor.enums import NumericRole, RecommendationStatus
from investment_agent.investor.foundations import InvestorError, InvestorService, storage_conflicts
from investment_agent.investor.publication import PublicationPolicy, PublicationService
from investment_agent.investor.publication_numbers import NUMBER, NumericResolver, format_quantity
from investment_agent.portfolio.calculation_contracts import PortfolioCalculationRequest
from investment_agent.portfolio.calculations import PortfolioCalculationService, TradingRuleResolver
from investment_agent.portfolio.contracts import Snapshot
from investment_agent.portfolio.plan_contracts import (
    PlanInput,
    PlanOption,
    PlanSemanticReview,
    PlanView,
    PortfolioAction,
    PortfolioPlan,
)
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import ValuationService
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPlanArtifact,
    PortfolioPlanEvent,
    PortfolioPlanHead,
    PortfolioPlanOutbox,
)


class PlanPublicationResult(Contract):
    published: bool
    plan_ref: ObjectRef | None
    draft_ref: ObjectRef | None
    check_ref: ObjectRef
    check: PublicationCheck


class PortfolioNumericResolver:
    """Narrow exact portfolio outputs, preserving company resolver restrictions."""
    def __init__(self, record: CalculationRecord, account_id: UUID) -> None:
        self.record, self.account_id = record, account_id

    def quantity(self, ref: ObjectRef, *, scenario: int, listing: ObjectRef | None = None, metric: str | None = None) -> Quantity:
        if ref.kind != "calculation" or ref.id != self.record.id or ref.revision != self.record.revision:
            raise InvestorError("invalid_reference", "Exact plan calculation required")
        matches = [p for p in self.record.details["result_paths"] if p["field_path"] == ref.field_path and p["scenario_index"] == scenario]
        if len(matches) != 1 or (listing is not None and matches[0].get("listing_ref") != listing.model_dump(mode="json")) or (metric is not None and matches[0]["metric"] != metric):
            raise InvestorError("invalid_reference", "Output subject, scenario or metric differs")
        result = self.record.results.get((ref.field_path or "")[8:])
        if result is None or result.value is None:
            raise InvestorError("invalid_reference", "Unknown output cannot become a target")
        return result

    async def render(self, action: PortfolioAction, scenario: int, session: AsyncSession) -> tuple[str, ...]:
        rendered = []
        for node in action.content:
            if isinstance(node, TextNode):
                if NUMBER.search(node.text):
                    raise InvestorError("invalid_reference", "Quantitative prose requires exact numeric nodes")
                rendered.append(node.text)
            elif isinstance(node, EntityNode):
                if node.ref != action.listing_ref:
                    raise InvestorError("invalid_reference", "Action identity differs")
                rendered.append((await NumericResolver(EvidenceService(session), action.listing_ref, self.record.as_of).resolve(node)).text)
            elif isinstance(node, NumericNode):
                if node.role != NumericRole.OUTPUT:
                    raise InvestorError("invalid_reference", "Plan prose supports saved arithmetic outputs only")
                quantity = self.quantity(node.ref, scenario=scenario, listing=action.listing_ref)
                rendered.append(format_quantity(quantity, node.display_format)[0])
            else:
                raise InvestorError("invalid_reference", "Unsupported portfolio content node")
        return tuple(rendered)


def constraint_measure(scenario: dict[str, Any], constraint: str, listing: ObjectRef | None = None, sector: str | None = None) -> Decimal | None:
    if constraint == "minimum_cash":
        return Decimal(scenario["cash"]) if scenario["cash"] is not None else None
    if constraint == "minimum_cash_weight":
        cash, nav = scenario["cash"], scenario["nav"]
        from investment_agent.portfolio.calculations import ratio
        return ratio(Decimal(cash), Decimal(nav)) if cash is not None and nav is not None else None
    if constraint == "max_single_weight":
        values = [h["weight"] for h in scenario["holdings"] if listing is None or h["listing_ref"] == listing.model_dump(mode="json")]
    else:
        if sector is not None:
            value = scenario["sector_weights"].get(sector)
            # An exited sector is zero only with complete, known coverage.
            if value is None and sector not in scenario["sector_weights"] and scenario["sector_coverage"] == "complete":
                return Decimal(0)
            values = [value]
        else:
            if scenario["sector_coverage"] != "complete":
                return None
            values = list(scenario["sector_weights"].values())
    if any(v is None for v in values):
        return None
    return max((Decimal(v) for v in values), default=Decimal(0))


def price_limits(record: CalculationRecord, at: datetime, max_days: int, scenario_indices: set[int] | None = None) -> tuple[str, ...]:
    snapshot = Snapshot.model_validate(record.details["snapshot"])
    required = {h.listing_ref for h in snapshot.holdings if h.confirmed_quantity != 0}
    required.update(ObjectRef.model_validate(a["listing_ref"]) for index, scenario in enumerate(record.details["scenarios"]) if scenario_indices is None or index in scenario_indices for a in scenario["actions"])
    limits = []
    for source in record.details["sources"]:
        ref = ObjectRef.model_validate(source["listing_ref"])
        if ref in required and source["price_date"] is not None:
            from datetime import date
            if (at.date() - date.fromisoformat(source["price_date"])).days > max_days:
                limits.append(f"price_basis_stale:{ref.id}")
    return tuple(limits)


class PortfolioPlanService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner", *, rules: TradingRuleResolver | None = None) -> None:
        self.session, self.owner = session, owner
        self.ledger = PortfolioService(session, owner)
        self.foundation = InvestorService(session)
        self.calculations = PortfolioCalculationService(session, owner, rules)

    async def artifact(self, ref: ObjectRef) -> PortfolioPlanArtifact:
        row = await self.session.get(PortfolioPlanArtifact, str(ref.id), populate_existing=True)
        if row is None or row.owner_id != self.owner or row.kind != ref.kind or ref.revision != 1 or ref.field_path is not None:
            raise InvestorError("not_found", "Portfolio artifact unavailable")
        await self.ledger._account(UUID(row.account_id))
        return row

    def _save(self, account_id: UUID, kind: str, value: dict[str, Any], identifier: UUID | None = None) -> ObjectRef:
        identifier = identifier or uuid4()
        self.session.add(PortfolioPlanArtifact(id=str(identifier), owner_id=self.owner, account_id=str(account_id), kind=kind, payload=value))
        return ObjectRef(kind=kind, id=identifier, revision=1)  # type: ignore[arg-type]

    async def current(self, account_id: UUID) -> PlanView | None:
        await self.ledger._account(account_id)
        head = await self.session.get(PortfolioPlanHead, str(account_id), populate_existing=True)
        return await self.get(ObjectRef(kind="portfolio_plan", id=UUID(head.plan_id), revision=1)) if head and head.plan_id else None

    async def verified_calculation(self, candidate: PlanInput) -> CalculationRecord:
        record = await self.calculations.get(candidate.account_id, UUID(str(candidate.calculation_ref.id)))
        if candidate.calculation_ref.revision != record.revision or record.method_version not in {"ag22-v1", "ag22-v2"}:
            raise InvestorError("invalid_reference", "Calculation version differs")
        request = PortfolioCalculationRequest.model_validate(record.details["request"])
        if request.snapshot_ref != candidate.snapshot_ref or request.mandate_ref != candidate.mandate_ref:
            raise InvestorError("invalid_reference", "All options require one exact snapshot and mandate")
        # Valuation binding (plan 2.5): a v2 calculation must be planned with
        # exactly the valuation it was priced on; a v1 one has none. A newer
        # valuation existing is a limitation marker on reads, never a silent
        # substitution here.
        if request.valuation_ref != candidate.valuation_ref:
            raise InvestorError("invalid_reference", "Plan valuation differs from the calculation's")
        if record.method_version == "ag22-v2" and request.valuation_ref is None:
            raise InvestorError("invalid_reference", "A v2 calculation requires its exact valuation")
        if record.method_version == "ag22-v1" and request.valuation_ref is not None:
            raise InvestorError("invalid_reference", "A valuation-bound calculation needs the v2 contract")
        rebuilt = await self.calculations.rebuild(request)
        if rebuilt.input_hash != record.input_hash or rebuilt.results != record.results or rebuilt.details != record.details:
            raise InvestorError("invalid_reference", "Saved calculation does not match server rebuilt inputs and outputs")
        return record

    async def _company(self, action: PortfolioAction) -> None:
        if not action.company_view_refs:
            raise InvestorError("invalid_reference", "Company basis requires exact current valid company view")
        for ref in action.company_view_refs:
            view = await PublicationService(self.session).get_recommendation(ref)
            case = await self.foundation.get_case("local_owner", view.recommendation.case_ids[0])
            if case.status.value == "archived" or case.listing_ref != action.listing_ref or case.current_recommendation_ref != ref or view.status != RecommendationStatus.PUBLISHED or view.limitation_reasons:
                raise InvestorError("invalid_reference", "Company view is expired, restricted, replaced or for another listing")

    async def check(self, candidate: PlanInput, semantic: PlanSemanticReview, *, as_of: datetime, task_id: UUID) -> tuple[PublicationCheck, PortfolioPlan | None]:
        errors: dict[str, str] = {}
        now = datetime.now(UTC)
        try:
            record = await self.verified_calculation(candidate)
        except (InvestorError, ValueError, KeyError) as exc:
            return PublicationCheck(rule_version="ag23-v1", passed=(), failures=("calculation_ref",), field_errors={"calculation_ref": str(exc)}, semantic_review_ref=None), None
        snapshot = Snapshot.model_validate(record.details["snapshot"])
        current_snapshot = await self.ledger.snapshot(candidate.account_id)
        mandate = await self.foundation.get_mandate("local_owner")
        if current_snapshot.reference != candidate.snapshot_ref:
            errors["snapshot_ref"] = "Actual facts changed; recalculate the whole plan"
        # The price basis of the calculation: the bound valuation's knowledge
        # cutoff for v2, the snapshot's own time otherwise.
        basis_at = snapshot.valuation_at
        if candidate.valuation_ref is not None:
            try:
                valuation = await ValuationService(self.session, self.owner).get(
                    candidate.account_id, UUID(str(candidate.valuation_ref.id))
                )
            except InvestorError as exc:
                errors["valuation_ref"] = str(exc)
            else:
                if valuation.snapshot_id != UUID(str(candidate.snapshot_ref.id)) or valuation.ledger_revision != candidate.snapshot_ref.revision:
                    errors["valuation_ref"] = "Valuation belongs to another ledger version"
                else:
                    basis_at = valuation.knowledge_cutoff
        if candidate.mandate_ref != ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision):
            errors["mandate_ref"] = "User mandate changed"
        if mandate.created_at > as_of:
            errors["mandate_ref.time"] = "Mandate was unavailable at research cutoff"
        if as_of > now or as_of < basis_at:
            errors["as_of"] = "Publication cutoff precedes observed snapshot or is future"
        if NUMBER.search(candidate.question_scope):
            errors["question_scope"] = "Numeric facts require saved output nodes"
        from investment_agent.portfolio.purposes import PurposeService
        purposes = await PurposeService(self.session, self.owner).current(candidate.account_id)
        purpose_ref = PortfolioCalculationRequest.model_validate(record.details["request"]).purpose_ref
        if purposes is not None and purposes.version.confirmed_at > as_of:
            errors["purpose_ref.time"] = "用途确认发生在研究知识时点之后，须主动发起新的研究"
        if purposes is not None and purpose_ref != purposes.version.reference:
            errors["purpose_ref"] = "用途已确认或修订，须绑定当前用途重新比较整案"
        actions = [a for o in candidate.options for a in o.actions]
        expected_sources = {candidate.snapshot_ref, candidate.mandate_ref, candidate.calculation_ref, *(r for a in actions for r in a.company_view_refs)}
        if purpose_ref is not None:
            expected_sources.add(purpose_ref)
        if semantic.candidate_hash != fingerprint(candidate.model_dump(mode="json")) or semantic.conclusion != "pass" or set(semantic.assessments) != {str(a.id) for a in actions} or not expected_sources <= set(semantic.checked_sources) or any(not all(t.strip() for t in assessment) for assessment in semantic.assessments.values()):
            errors["semantic_review"] = "Review must bind every exact action and saved premise/countercase/materiality"
        if semantic.unresolved_questions and any(a.stance in {"buy", "add", "reduce", "exit", "hold"} for a in actions):
            errors["semantic_review.unresolved_questions"] = "Directional action has unresolved semantic questions"
        resolver = PortfolioNumericResolver(record, candidate.account_id)
        scenarios = record.details["scenarios"]
        baseline = scenarios[0]
        formal = mandate.portfolio_constraints
        options = []
        for option in candidate.options:
            prefix = f"options.{option.id}"
            if option.scenario_index >= len(scenarios):
                errors[prefix] = "Scenario is not in saved calculation"
                continue
            scenario = scenarios[option.scenario_index]
            scenario_request = PortfolioCalculationRequest.model_validate(record.details["request"]).scenarios[option.scenario_index - 1]
            movements = {a.listing_ref: a for a in scenario_request.actions}
            if len(movements) != len(scenario_request.actions):
                errors[prefix] = "One listing movement per option avoids ambiguous action targets"
            if len({a.listing_ref for a in option.actions}) != len(option.actions):
                errors[prefix] = "Duplicate listing actions are ambiguous"
            covered: set[ObjectRef] = set()
            improvements: list[str] = []
            rendered: dict[str, tuple[str, ...]] = {}
            constraint_only = bool(option.actions) and all(a.basis == "portfolio_constraint" for a in option.actions)
            for action in option.actions:
                path = prefix + f".actions.{action.id}"
                try:
                    if action.research_gap and NUMBER.search(action.research_gap):
                        raise InvestorError("invalid_reference", "Quantitative research gaps require referenced content nodes")
                    holding = next((h for h in snapshot.holdings if h.listing_ref == action.listing_ref), None)
                    before = holding.confirmed_quantity if holding else Decimal(0) if snapshot.coverage == "complete" else None
                    if action.stance in {"hold", "add", "reduce", "exit"} and (before is None or before <= 0):
                        raise InvestorError("invalid_reference", "This stance requires confirmed positive actual holding")
                    if action.stance == "buy" and before != 0:
                        raise InvestorError("invalid_reference", "Buy requires confirmed absence; unknown absence is not zero")
                    movement = movements.get(action.listing_ref)
                    if action.stance in {"buy", "add", "reduce", "exit"}:
                        if movement is None or movement.direction != ("buy" if action.stance in {"buy", "add"} else "sell"):
                            raise InvestorError("invalid_reference", "Stance differs from calculated movement")
                        covered.add(action.listing_ref)
                        if action.target_ref is None:
                            raise InvestorError("invalid_reference", "Directional target requires saved calculation quantity")
                        quantity = resolver.quantity(action.target_ref, scenario=option.scenario_index, listing=action.listing_ref, metric="quantity")
                        matching_path = next(p for p in record.details["result_paths"] if p["field_path"] == action.target_ref.field_path)
                        if "action_index" not in matching_path or quantity.value is None or quantity.value <= 0:
                            raise InvestorError("invalid_reference", "Directional target is a positive action quantity")
                        if action.stance == "exit" and quantity.value != before:
                            raise InvestorError("invalid_reference", "Exit must sell the entire confirmed actual holding")
                        if action.stance == "reduce" and before is not None and quantity.value >= before:
                            raise InvestorError("invalid_reference", "Reduce must leave a positive holding; full sale is exit")
                    else:
                        if movement is not None:
                            raise InvestorError("invalid_reference", "Hold/wait/further research cannot conceal a calculated trade")
                        if action.target_ref:
                            target = resolver.quantity(action.target_ref, scenario=option.scenario_index, listing=action.listing_ref, metric="quantity")
                            if action.stance == "hold" and target.value != before:
                                raise InvestorError("invalid_reference", "Hold target differs from actual holding")
                    if action.basis == "company_thesis":
                        await self._company(action)
                        if action.constraint is not None:
                            raise InvestorError("invalid_reference", "Company basis cannot assert constraint authority")
                    else:
                        if action.stance not in {"reduce", "exit", "wait"} or before is None or before <= 0 or not action.research_gap or action.company_view_refs:
                            raise InvestorError("invalid_reference", "Constraint exception requires holding, reduce/exit/wait and explicit research gap")
                        constraint_name = action.constraint
                        if constraint_name is None or formal is None or formal.account_id != candidate.account_id or getattr(formal, constraint_name) is None or baseline["mandate_checks"].get(constraint_name) != "fail":
                            raise InvestorError("invalid_reference", "Known violated exact user typed hard constraint required")
                        sector = next((s["industry"]["code"] for s in record.details["sources"] if s["listing_ref"] == action.listing_ref.model_dump(mode="json") and s["industry"]), None)
                        old = constraint_measure(baseline, constraint_name, action.listing_ref, sector)
                        new = constraint_measure(scenario, constraint_name, action.listing_ref, sector)
                        threshold = getattr(formal, constraint_name)
                        violated = old is not None and (old > threshold if constraint_name.startswith("max_") else old < threshold)
                        improved = old is not None and new is not None and (new < old if constraint_name.startswith("max_") else new > old)
                        waiting = action.stance == "wait" and not scenario_request.actions and action.target_ref is None
                        if not violated or not (improved or waiting):
                            raise InvestorError("invalid_reference", "Exact calculation must show relevant holding/sector constraint improvement")
                        if improved:
                            improvements.append(constraint_name)
                    rendered[str(action.id)] = await resolver.render(action, option.scenario_index, self.session)
                except (InvestorError, ValueError, KeyError, StopIteration) as exc:
                    errors[path] = str(exc)
            if set(movements) != covered:
                errors[prefix + ".movements"] = "Each calculated movement needs exactly one declared directional action"
            # A violation in no_action is not an automatic rejection of risk reduction.
            # Residual typed mandate violations may remain if every known metric
            # is no worse; unknown execution/data/rules still forbids sizing.
            remaining = set(scenario["violations"])
            safe_exception = constraint_only and bool(improvements) and not scenario["unknowns"]
            if safe_exception:
                for constraint in ("max_single_weight", "max_sector_weight", "minimum_cash", "minimum_cash_weight"):
                    if formal is None or getattr(formal, constraint) is None:
                        continue
                    old, new = constraint_measure(baseline, constraint), constraint_measure(scenario, constraint)
                    if old is None or new is None or (new > old if constraint.startswith("max_") else new < old):
                        safe_exception = False
                safe_exception = safe_exception and remaining <= {v for v in baseline["violations"] if v.startswith("mandate_constraint_violation:")}
            directional = any(a.stance in {"buy", "add", "reduce", "exit"} for a in option.actions)
            request = PortfolioCalculationRequest.model_validate(record.details["request"])
            stale = price_limits(record, as_of, request.max_price_age_days, {o.scenario_index for o in candidate.options})
            if directional and stale:
                errors[prefix + ".price_basis"] = "Frozen price basis is stale at publication cutoff"
            if directional and not (scenario["publication_ready"] or safe_exception):
                errors[prefix + ".sizing"] = "Execution requires known cash, fees, coverage, trusted rules and safe constraints"
            saved_option = self._option(record, option.id, option.scenario_index, option.actions, bool(scenario["publication_ready"] or safe_exception) and directional, tuple(improvements), rendered)
            if stale:
                saved_option = saved_option.model_copy(update={"unknowns": tuple(dict.fromkeys((*saved_option.unknowns, *stale)))})
            options.append(saved_option)
        check = PublicationCheck(rule_version="ag23-v1", passed=() if errors else ("identity", "time", "numeric", "semantic", "action_semantics", "whole_account_constraints"), failures=tuple(errors), field_errors=errors, semantic_review_ref=None)
        next_review, valid_until = PublicationPolicy().dates(as_of)
        plan = PortfolioPlan(id=uuid4(), revision=1, created_at=now, actor="research_service", origin_ref=ObjectRef(kind="task", id=task_id, revision=1), account_id=candidate.account_id, snapshot_ref=candidate.snapshot_ref, valuation_ref=candidate.valuation_ref, mandate_ref=candidate.mandate_ref, calculation_ref=candidate.calculation_ref, previous_ref=candidate.previous_ref, question_scope=candidate.question_scope, as_of=as_of, price_basis_at=basis_at, valid_until=valid_until, next_review_at=next_review, no_action=self._option(record, uuid4(), 0, (), False), options=tuple(options))
        return check, plan

    def _option(self, record: CalculationRecord, identifier: UUID, index: int, actions: tuple[PortfolioAction, ...], executable: bool, improvements: tuple[str, ...] = (), rendered: dict[str, tuple[str, ...]] | None = None) -> PlanOption:
        scenario = record.details["scenarios"][index]
        paths = [p["field_path"] for p in record.details["result_paths"] if p["scenario_index"] == index]
        return PlanOption(id=identifier, scenario_index=index, name=scenario["name"], actions=actions, totals={path[8:]: record.results[path[8:]] for path in paths}, result_refs=tuple(ObjectRef(kind="calculation", id=record.id, revision=record.revision, field_path=p) for p in paths), violations=tuple(scenario["violations"]), unknowns=tuple(scenario["unknowns"]), executable=executable, constraint_improvements=improvements, rendered_actions=rendered or {})

    @storage_conflicts
    async def publish(self, claim: TaskClaim, operation_id: UUID, candidate: PlanInput, semantic: PlanSemanticReview, *, base: Settings) -> PlanPublicationResult:
        # Physical transaction before TaskService savepoint preserves outer rollback.
        await self.ledger._reserve()
        account = await self.ledger._account(candidate.account_id)
        await self.session.execute(update(type(account)).where(type(account).id == account.id).values(revision=type(account).revision))
        tasks = TaskService(self.session)
        action = await tasks._action(claim.task_id, operation_id)
        if action.parameters != {"candidate": candidate.model_dump(mode="json"), "semantic": semantic.model_dump(mode="json")}:
            raise InvestorError("idempotency_conflict", "Prepared whole plan publication parameters differ")
        async def write(session: AsyncSession, operation: UUID) -> ActionResult:
            from investment_agent.agent.observation import effective_cutoff
            required = (candidate.snapshot_ref, *(a.listing_ref for o in candidate.options for a in o.actions))
            grant, settings = await check_boundary(session, claim, base, tool="publish_portfolio_plan", required_refs=required)
            if not settings.portfolio_ledger_enabled or grant.research_kind != "portfolio_action":
                raise InvestorError("invalid_reference", "Portfolio publication is disabled or outside exact grant")
            if grant.portfolio_account_id != candidate.account_id or grant.portfolio_snapshot_ref != candidate.snapshot_ref:
                raise InvestorError("invalid_reference", "Grant account/snapshot differs")
            head = await session.get(PortfolioPlanHead, str(candidate.account_id), populate_existing=True)
            previous = ObjectRef(kind="portfolio_plan", id=UUID(head.plan_id), revision=1) if head and head.plan_id else None
            if candidate.previous_ref != previous:
                raise InvestorError("revision_conflict", "Whole plan changed; reload explicit prior plan")
            expected = head.revision if head else 0
            if head is None:
                head = PortfolioPlanHead(account_id=str(candidate.account_id), revision=0, plan_id=None)
                session.add(head)
                await session.flush()
            cutoff = await effective_cutoff(session, claim.task_id)
            from investment_agent.portfolio.purposes import PurposeService
            purpose_view = await PurposeService(session, self.owner).current(candidate.account_id)
            task = await TaskService(session).get_task(claim.task_id)
            if purpose_view is not None and purpose_view.version.confirmed_at > task.binding.as_of:
                raise InvestorError("invalid_reference", "用途在任务知识时点之后确认，请主动发起新的研究")
            check, plan = await self.check(candidate, semantic, as_of=cutoff, task_id=claim.task_id)
            review_ref = self._save(candidate.account_id, "review", semantic.model_dump(mode="json"))
            check = check.model_copy(update={"semantic_review_ref": review_ref})
            check_ref = self._save(candidate.account_id, "publication_check", check.model_dump(mode="json"))
            if check.failures or plan is None:
                draft_ref = self._save(candidate.account_id, "draft", {"candidate": candidate.model_dump(mode="json"), "check": check.model_dump(mode="json")})
                result = PlanPublicationResult(published=False, plan_ref=None, draft_ref=draft_ref, check_ref=check_ref, check=check)
            else:
                plan_ref = self._save(candidate.account_id, "portfolio_plan", plan.model_dump(mode="json"), plan.id)
                advanced = await session.execute(update(PortfolioPlanHead).where(PortfolioPlanHead.account_id == str(candidate.account_id), PortfolioPlanHead.revision == expected, PortfolioPlanHead.plan_id == (str(previous.id) if previous else None)).values(revision=expected + 1, plan_id=str(plan.id)))
                if advanced.rowcount != 1:  # type: ignore[attr-defined]
                    raise InvestorError("revision_conflict", "Whole plan changed concurrently")
                if previous:
                    await self._event(previous, "superseded", ("whole_plan_replaced",), replacement=plan_ref)
                await self._event(plan_ref, "published", ())
                result = PlanPublicationResult(published=True, plan_ref=plan_ref, draft_ref=None, check_ref=check_ref, check=check)
            await check_boundary(session, claim, base, tool="publish_portfolio_plan", required_refs=required)
            return ActionResult(result.model_dump(mode="json"), tuple(r for r in (result.plan_ref, result.draft_ref, result.check_ref) if r))
        return PlanPublicationResult.model_validate(await tasks.settle_action(claim, operation_id, write))

    async def _event(self, ref: ObjectRef, state: str, reasons: tuple[str, ...], *, replacement: ObjectRef | None = None, checked_refs: tuple[ObjectRef, ...] = ()) -> None:
        key = fingerprint({"state": state, "reasons": reasons, "replacement": replacement.model_dump(mode="json") if replacement else None, "checked_refs": [r.model_dump(mode="json") for r in checked_refs]})
        exists = await self.session.scalar(select(PortfolioPlanEvent).where(PortfolioPlanEvent.plan_id == str(ref.id), PortfolioPlanEvent.event_key == key))
        if exists:
            return
        payload = {"plan_ref": ref.model_dump(mode="json"), "state": state, "reasons": reasons, "at": datetime.now(UTC).isoformat(), "replacement_ref": replacement.model_dump(mode="json") if replacement else None, "checked_refs": [r.model_dump(mode="json") for r in checked_refs]}
        self.session.add(PortfolioPlanEvent(plan_id=str(ref.id), event_key=key, payload=payload))
        self.session.add(PortfolioPlanOutbox(key=f"{ref.id}:{key}", plan_id=str(ref.id), state="pending", payload=payload))
        await self.session.flush()

    async def get(self, ref: ObjectRef) -> PlanView:
        # Safe current projection is read-only. Durable state/outbox integration is
        # maintained separately, so GET does not require a write lock or side effect.
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN")
        row = await self.artifact(ref)
        plan = PortfolioPlan.model_validate(row.payload)
        snapshot = await self.ledger.snapshot(plan.account_id)
        mandate = await self.foundation.get_mandate("local_owner")
        mandate_ref = ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision)
        head = await self.session.get(PortfolioPlanHead, str(plan.account_id), populate_existing=True)
        current_ref = ObjectRef(kind="portfolio_plan", id=UUID(head.plan_id), revision=1) if head and head.plan_id else None
        reasons = []
        if snapshot.reference != plan.snapshot_ref:
            reasons.append("actual_snapshot_changed")
        if plan.valuation_ref is not None:
            # Price basis discipline (plan 2.5): a newer valuation exists as a
            # limitation marker - the plan keeps its own frozen reference and
            # is never silently re-priced.
            current_valuation = await ValuationService(self.session, self.owner).current(plan.account_id)
            if current_valuation is None:
                reasons.append("valuation_unavailable")
            elif current_valuation.reference != plan.valuation_ref:
                reasons.append("valuation_updated")
        original_mandate = await self.foundation.get_mandate("local_owner", plan.mandate_ref.revision)
        if original_mandate.id != mandate.id or any(getattr(original_mandate, field) != getattr(mandate, field) for field in ("portfolio_constraints", "hard_constraints", "scope_refs", "goals")):
            reasons.append("mandate_changed")
        for option in plan.options:
            for action in option.actions:
                if action.basis == "company_thesis":
                    try:
                        await self._company(action)
                    except (InvestorError, ValueError):
                        reasons.append(f"company_view_unavailable:{action.listing_ref.id}")
        now = datetime.now(UTC)
        calculation = await self.calculations.get(plan.account_id, UUID(str(plan.calculation_ref.id)))
        request = PortfolioCalculationRequest.model_validate(calculation.details["request"])
        from investment_agent.portfolio.purposes import PurposeService
        purposes = await PurposeService(self.session, self.owner).current(plan.account_id)
        if purposes is not None:
            original = calculation.details.get("purposes")
            if original is None or original["semantic_hash"] != purposes.version.semantic_hash:
                reasons.append("purpose_constraints_changed")
            if purposes.status == "overallocated" or purposes.known_cash is None:
                reasons.append("purpose_cash_requires_review")
        if any(o.executable for o in plan.options):
            reasons.extend(price_limits(calculation, now, request.max_price_age_days, {o.scenario_index for o in plan.options}))
        status = "published"
        replacement = None
        if current_ref != ref:
            status, replacement = "superseded", current_ref
            reasons.append("not_current")
        elif now >= plan.valid_until:
            status = "expired"
            reasons.append("validity_expired")
        elif reasons or now >= plan.next_review_at:
            status = "review_required"
            if now >= plan.next_review_at:
                reasons.append("calendar_due")
        return PlanView(plan=plan, status=status, limitation_reasons=tuple(dict.fromkeys(reasons)), replacement_ref=replacement, current_refs=tuple(r for r in (snapshot.reference, mandate_ref, current_ref, purposes.version.reference if purposes else None) if r))  # type: ignore[arg-type]

    async def reconcile(self, *, limit: int = 50) -> int:
        """Bounded AG15 adapter: record one durable invalidation, no paid retry."""
        await self.ledger._reserve()
        heads = (await self.session.scalars(select(PortfolioPlanHead).where(PortfolioPlanHead.plan_id.is_not(None)).order_by(PortfolioPlanHead.checked_at.asc().nulls_first(), PortfolioPlanHead.account_id).limit(limit))).all()
        changed = 0
        for head in heads:
            assert head.plan_id is not None
            ref = ObjectRef(kind="portfolio_plan", id=UUID(head.plan_id), revision=1)
            view = await self.get(ref)
            head.checked_at = datetime.now(UTC).isoformat()
            if view.status != "published":
                await self._event(ref, view.status, view.limitation_reasons, replacement=view.replacement_ref, checked_refs=view.current_refs)
                changed += 1
        return changed


async def maintain_portfolio_plans(session: AsyncSession, base: Settings, *, limit: int = 50) -> None:
    """AG15 bounded, durable review dispatch; one Task for an invalidated plan.

    No paid retry/budget reset: once admitted the original Task owns all attempts.
    Admission failures remain cheap authorization rechecks, without model work.
    """
    from investment_agent.agent.contracts import ContextBinding
    from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
    from investment_agent.investor.enums import TaskStatus

    service = PortfolioPlanService(session)
    await service.reconcile(limit=limit)
    rows = (await session.scalars(select(PortfolioPlanOutbox).where(PortfolioPlanOutbox.state == "pending").order_by(PortfolioPlanOutbox.payload["dispatch_checked_at"].as_string().asc().nulls_first(), PortfolioPlanOutbox.key).limit(limit))).all()
    for row in rows:
        row.payload = {**row.payload, "dispatch_checked_at": datetime.now(UTC).isoformat()}
        if row.payload["state"] == "published" or row.payload["state"] == "superseded":
            row.state = "consumed"
            continue
        # Several dependency events may refer to the same plan, but only its
        # earliest outbox row owns dispatch. Later rows are audit notifications.
        dispatched = await session.scalar(select(PortfolioPlanOutbox).where(PortfolioPlanOutbox.plan_id == row.plan_id, PortfolioPlanOutbox.payload["task_id"].as_string().is_not(None)))
        if dispatched is not None:
            if dispatched.key != row.key:
                row.state = "consumed"
                continue
            task = await TaskService(session).get_task(UUID(row.payload["task_id"]))
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                row.state = "consumed"
            continue
        ref = ObjectRef(kind="portfolio_plan", id=UUID(row.plan_id), revision=1)
        view = await service.get(ref)
        if view.status == "superseded":
            row.state = "consumed"
            continue
        snapshot = await service.ledger.snapshot(view.plan.account_id)
        mandate = await service.foundation.get_mandate("local_owner")
        valuation = await ValuationService(session, service.owner).current(view.plan.account_id)
        valuation_ref = valuation.reference if valuation is not None else None
        scope = tuple(dict.fromkeys((snapshot.reference, *((valuation_ref,) if valuation_ref else ()), *(h.listing_ref for h in snapshot.holdings), *(a.listing_ref for o in view.plan.options for a in o.actions))))
        mandate_ref = ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision)
        grant = ExecutionGrant(mode="background", source="authorized_background", prompt="Recheck the whole current portfolio after recorded actual facts or relevant dependencies changed. Original options are stale and never automatically executed. Obtain current company views or explicitly limited typed constraint analysis, calculate the whole account, and review mutually exclusive options. Stop on unavailable data or budget.", tool_names=("save_research_progress", "get_portfolio_context", "calculate_portfolio_options", "prepare_portfolio_plan", "publish_portfolio_plan"), scope_refs=scope, mandate_id=mandate.id, current_research=True, research_kind="portfolio_action", purpose="review", portfolio_account_id=view.plan.account_id, portfolio_snapshot_ref=snapshot.reference, portfolio_valuation_ref=valuation_ref)
        try:
            task = await admit(session, base=base, input_key=f"portfolio-review:{row.plan_id}", binding=ContextBinding(context_refs=(snapshot.reference,), base_refs=(*scope, mandate_ref), as_of=datetime.now(UTC)), grant=grant, kind="agent_review")
        except ExecutionDeniedError as exc:
            row.payload = {**row.payload, "blocked": str(exc)}
        else:
            row.payload = {**row.payload, "task_id": str(task.id), "attempted": True, "blocked": None}
    await session.flush()
