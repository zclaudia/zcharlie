"""Daily zone detection (watch-holding-plan 6.2 WP8, funnel 5.4/6/7.3/8/9).

The rule store records what the user approved; this module is what makes an
approved rule *fire*. For every listing with an active rule it evaluates the
rule's fixed zones against the day's inputs and records a
:class:`~investment_agent.storage.models.zones.ZoneEventRecord`:

* **candidate / observation scope** - ``buy_zone`` / ``drop`` against the
  point-in-time factors of the day (the same factors the screen and the
  drafting agent read);
* **holding scope** - ``add`` / ``trim`` / ``exit_review`` against those same
  factors plus the position factors (``position_weight``, ``gain_vs_cost``,
  ``holding_days``) derived from the accounts' current immutable valuations.
  The valuation's ``ledger_revision`` / ``knowledge_cutoff`` / price refs are
  frozen into the event (plan 2.10 / 5.4).

Determinism and identity
------------------------

:func:`evaluate_rule_zones` is pure: same zones + same factors + same params ->
same per-zone outcome. The event identity is
``(listing, scope, zone, rule instance, as_of, transition, input digest)``,
where the digest covers the referenced factor values *and their provenance*
plus the valuation references. Re-running the same task on the same facts /
prices and the same rule version therefore inserts nothing, while revised
facts or prices produce a new identity instead of a rewrite (plan 6.2 WP8).

Rules that cannot be evaluated are reported, never invented: a zone whose
factors are all unavailable is *undecided* (no event, interval unchanged), and
every event names the rule version it came from.

Enter / leave and notification (funnel 9)
-----------------------------------------

``zone_interval_state`` remembers whether a ``(listing, scope, zone)`` is
currently inside. A zone that starts holding raises one notice (``info`` for
``buy_zone`` / ``add`` / ``trim``, ``action_required`` for ``drop`` /
``exit_review``); staying inside, including on revised inputs, notifies
nothing; leaving records a ``left`` event and no notice; re-entering raises a
notice again. The event row is the in-app notice, and when the listing has an
investor case the same entry is additionally written into the agent's
notification outbox (:mod:`investment_agent.investor.notifications`), so the
existing external channel carries it without a second delivery path.

A candidate-scope ``drop`` also archives the open candidate (funnel 7.3) with a
system ``drop`` decision that carries the event's rule reference and the
conditions that fired. The event is a prompt and a record: no rule event ever
writes to the user's portfolio ledger or submits anything.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorValue
from investment_agent.factors.view import load_snapshot_view
from investment_agent.portfolio.service import PortfolioService
from investment_agent.portfolio.valuation import Valuation, ValuationHolding, ValuationService
from investment_agent.rules.schema import ConditionGroup
from investment_agent.rules.screen import evaluate_zone_outcomes, jsonable
from investment_agent.rules.store import RuleStore, instance_from_record
from investment_agent.rules.validation import effective_params, factors_in
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.models.zones import ZoneEventRecord, ZoneIntervalStateRecord

log = structlog.get_logger(__name__)

ZoneScope = Literal["candidate", "holding"]
CANDIDATE_SCOPE: ZoneScope = "candidate"
HOLDING_SCOPE: ZoneScope = "holding"
OWNER = "local_owner"

POSITION_FACTORS: tuple[str, ...] = ("position_weight", "gain_vs_cost", "holding_days")
"""Holding factors assembled here from the valuation (not in the factor registry)."""

ZONE_LEVELS: dict[str, str] = {
    "buy_zone": "info",
    "add": "info",
    "trim": "info",
    "drop": "action_required",
    "exit_review": "action_required",
}
"""funnel 9: ``exit_review`` / ``drop`` are action_required, the rest info."""


def zone_level(zone: str) -> str:
    return ZONE_LEVELS.get(zone, "info")


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZoneRule:
    """One active rule instance with its zones and effective parameters."""

    listing_id: int
    ticker: str
    scope: str
    rule_instance_id: int
    rule_version: int
    template_id: str
    template_version: int
    calibration_status: str
    zones: Mapping[str, ConditionGroup]
    params: Mapping[str, float]

    @property
    def template_ref(self) -> str:
        return f"{self.template_id}@{self.template_version}"

    @property
    def rule_ref(self) -> str:
        return f"{self.ticker}/{self.template_ref}/v{self.rule_version}"


def _record_zones(
    record: RuleInstanceRecord, template: Any, scope: str
) -> dict[str, ConditionGroup]:
    """The rule's zones: the instance's own definitions, else the template's.

    Instance-defined zones go through the same zone-name whitelist as authored
    templates (``template.zones_for``): a leftover or hand-edited row cannot
    invent a zone the template never declared - unknown names are skipped with
    a warning instead of generating events.
    """

    raw = (record.definition or {}).get("zones")
    if isinstance(raw, dict) and raw:
        allowed = set(template.zones_for(scope))
        zones: dict[str, ConditionGroup] = {}
        for name, node in raw.items():
            if name not in allowed:
                log.warning(
                    "zone_detection.zone_unknown",
                    rule_instance_id=record.id,
                    zone=name,
                    scope=scope,
                )
                continue
            try:
                zones[name] = ConditionGroup.model_validate(node)
            except ValueError:
                log.warning(
                    "zone_detection.zone_invalid", rule_instance_id=record.id, zone=name
                )
        return zones
    return dict(template.zones_for(scope))


async def active_zone_rules(session: AsyncSession, scope: str) -> list[ZoneRule]:
    """Every active rule of ``scope`` that declares at least one zone."""

    store = RuleStore(session)
    rules: list[ZoneRule] = []
    for record in sorted(
        (await store.instances(status="active")), key=lambda row: (row.listing_id, row.version)
    ):
        if record.scope != scope:
            continue
        template = await store.get_template(record.template_id, record.template_version)
        if template is None:
            log.warning(
                "zone_detection.template_missing",
                rule_instance_id=record.id,
                template_ref=f"{record.template_id}@{record.template_version}",
            )
            continue
        zones = _record_zones(record, template, scope)
        if not zones:
            continue
        rules.append(
            ZoneRule(
                listing_id=record.listing_id,
                ticker=record.symbol,
                scope=scope,
                rule_instance_id=record.id,
                rule_version=record.version,
                template_id=record.template_id,
                template_version=record.template_version,
                # The template's calibration status is the honest label: an
                # instance never carries its own calibration (funnel 10).
                calibration_status=template.calibration_status,
                zones=zones,
                params=effective_params(template, instance_from_record(record)),
            )
        )
    return rules


def referenced_factors(zones: Mapping[str, ConditionGroup]) -> tuple[set[str], set[str]]:
    """``(registry factors, position factors)`` a rule's zones reference."""

    names: set[str] = set()
    for group in zones.values():
        names |= factors_in(group)
    return (
        {name for name in names if name in DEFAULT_REGISTRY},
        {name for name in names if name in POSITION_FACTORS},
    )


async def candidate_factor_values(
    session: AsyncSession, ticker: str, as_of: date, names: Iterable[str]
) -> Mapping[str, FactorValue]:
    """The point-in-time registry factor values of ``ticker`` on ``as_of``."""

    selected = [name for name in sorted(set(names)) if name in DEFAULT_REGISTRY]
    if not selected:
        return {}
    view = await load_snapshot_view(session, ticker, as_of)
    return DEFAULT_REGISTRY.compute_all(view, selected)


# --------------------------------------------------------------------------- #
# Position factors (plan 2.4 / 2.7 / 2.10)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HoldingPosition:
    """One account's valuation of one held listing (the position factor inputs)."""

    account_id: str
    valuation_id: str
    ledger_revision: int
    valuation_date: date
    knowledge_cutoff: datetime
    snapshot_id: str
    algorithm_version: str
    quantity: Decimal
    market_value: Decimal | None
    remaining_cost: Decimal | None
    total_assets: Decimal | None
    holding_days: int | None
    weight_available: bool
    price_ref: Mapping[str, Any] | None
    price_date: date | None
    cost_unknown_reason: str | None
    price_unknown_reason: str | None

    def valuation_ref(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "valuation_id": self.valuation_id,
            "ledger_revision": self.ledger_revision,
            "valuation_date": self.valuation_date.isoformat(),
            "knowledge_cutoff": self.knowledge_cutoff.isoformat(),
            "snapshot_id": self.snapshot_id,
            "algorithm_version": self.algorithm_version,
            "quantity": str(self.quantity),
            "price_ref": dict(self.price_ref) if self.price_ref is not None else None,
            "price_date": self.price_date.isoformat() if self.price_date is not None else None,
        }


def position_of(
    valuation: Valuation, holding: ValuationHolding, *, account_id: str
) -> HoldingPosition | None:
    """The valuation holding as a position; ``None`` for an empty line."""

    if holding.quantity is None or holding.quantity == 0:
        return None
    return HoldingPosition(
        account_id=account_id,
        valuation_id=str(valuation.id),
        ledger_revision=valuation.ledger_revision,
        valuation_date=valuation.valuation_date,
        knowledge_cutoff=valuation.knowledge_cutoff,
        snapshot_id=str(valuation.snapshot_id),
        algorithm_version=valuation.algorithm_version,
        quantity=holding.quantity,
        market_value=holding.market_value,
        remaining_cost=holding.remaining_cost,
        total_assets=valuation.total_assets,
        holding_days=holding.holding_days,
        weight_available=valuation.weight_available,
        price_ref=(
            holding.price_ref.model_dump(mode="json") if holding.price_ref is not None else None
        ),
        price_date=holding.price_date,
        cost_unknown_reason=holding.cost_unknown_reason,
        price_unknown_reason=holding.price_unknown_reason,
    )


def _position_factor(
    name: str,
    value: Decimal | int | float | None,
    *,
    positions: Sequence[HoldingPosition],
    reason: str | None = None,
) -> FactorValue:
    ref = {"positions": [item.valuation_ref() for item in positions]}
    if value is None:
        # Unavailable is reported as such (plan 2.9), never as zero: the zone
        # condition then does not hold and the event records the gap in flags.
        return FactorValue(
            name=name,
            version="position_v1",
            value=None,
            quality="missing",
            flags=(reason or "unavailable",),
            inputs_ref=ref,
        )
    return FactorValue(
        name=name, version="position_v1", value=float(value), quality="ok", inputs_ref=ref
    )


def holding_factor_values(positions: Sequence[HoldingPosition]) -> dict[str, FactorValue]:
    """``position_weight`` / ``gain_vs_cost`` / ``holding_days`` across accounts.

    One stock is one holding for the rule's purposes: quantities, market values
    and costs add up and the account weights are re-derived from the summed
    market value over the summed NAV; holding days take the longest open cycle.
    A datum unknown in any account keeps the factor unavailable instead of
    silently understating it (plan 2.9).
    """

    if not positions:
        return {}

    def _sum(attr: str) -> Decimal | None:
        values: list[Decimal | None] = [getattr(item, attr) for item in positions]
        if any(value is None for value in values):
            return None
        return sum((value for value in values if value is not None), Decimal(0))

    market_value = _sum("market_value")
    cost = _sum("remaining_cost")
    nav = _sum("total_assets")
    weight: Decimal | None = None
    weight_reason: str | None = None
    if not all(item.weight_available for item in positions):
        weight_reason = "weight_unavailable"
    elif market_value is None or nav is None or nav <= 0:
        weight_reason = "nav_or_market_value_unknown"
    else:
        weight = market_value / nav
    gain: Decimal | None = None
    gain_reason: str | None = None
    if cost is None or cost == 0:
        gain_reason = "cost_unknown" if cost is None else "zero_cost_ratio_undefined"
    elif market_value is None:
        gain_reason = next(
            (item.price_unknown_reason for item in positions if item.price_unknown_reason), None
        ) or "market_value_unknown"
    else:
        gain = (market_value - cost) / cost
    days = [item.holding_days for item in positions]
    known_days = [value for value in days if value is not None]
    holding_days = max(known_days) if len(known_days) == len(days) else None
    return {
        "position_weight": _position_factor(
            "position_weight", weight, positions=positions, reason=weight_reason
        ),
        "gain_vs_cost": _position_factor(
            "gain_vs_cost", gain, positions=positions, reason=gain_reason
        ),
        "holding_days": _position_factor(
            "holding_days",
            holding_days,
            positions=positions,
            reason="holding_days_unknown",
        ),
    }


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZoneSignal:
    zone: str
    holds: bool
    level: str
    conditions_met: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ListingEvaluation:
    """One listing's zone outcome under one rule version and input pair."""

    rule: ZoneRule
    as_of: date
    input_digest: str
    inputs: Mapping[str, Any]
    signals: Mapping[str, ZoneSignal]
    undecided: tuple[str, ...] = ()
    positions: tuple[HoldingPosition, ...] = ()

    @property
    def listing_id(self) -> int:
        return self.rule.listing_id

    @property
    def ticker(self) -> str:
        return self.rule.ticker

    @property
    def scope(self) -> str:
        return self.rule.scope

    @property
    def valuation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(position.valuation_id for position in self.positions))

    @property
    def account_id(self) -> str | None:
        """The single holding account, or ``None`` when several were aggregated."""

        accounts = {position.account_id for position in self.positions}
        return next(iter(accounts)) if len(accounts) == 1 else None

    @property
    def ledger_revision(self) -> int | None:
        revisions = {position.ledger_revision for position in self.positions}
        return next(iter(revisions)) if len(revisions) == 1 else None

    @property
    def knowledge_cutoff(self) -> datetime | None:
        cutoffs = {position.knowledge_cutoff for position in self.positions}
        return next(iter(cutoffs)) if len(cutoffs) == 1 else None


def _factor_payload(factors: Mapping[str, FactorValue]) -> dict[str, Any]:
    return {
        name: {
            "value": factors[name].value,
            "quality": factors[name].quality,
            "flags": list(factors[name].flags),
            "ref": jsonable(dict(factors[name].inputs_ref)),
        }
        for name in sorted(factors)
    }


def input_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        jsonable(dict(payload)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def valuation_input(positions: Sequence[HoldingPosition]) -> dict[str, Any] | None:
    """The frozen valuation provenance block of a holding-scope evaluation."""

    if not positions:
        return None
    return {"positions": [item.valuation_ref() for item in positions]}


def evaluate_rule_zones(
    rule: ZoneRule,
    *,
    as_of: date,
    factors: Mapping[str, FactorValue],
    positions: Sequence[HoldingPosition] = (),
) -> ListingEvaluation:
    """Evaluate one rule over one stock's factors (pure) and freeze its inputs."""

    outcomes = evaluate_zone_outcomes(zones=rule.zones, factors=factors, params=rule.params)
    signals: dict[str, ZoneSignal] = {}
    undecided: list[str] = []
    for name, outcome in outcomes.items():
        if outcome.holds is None:
            undecided.append(name)
            continue
        signals[name] = ZoneSignal(
            zone=name,
            holds=bool(outcome.holds),
            level=zone_level(name),
            conditions_met=outcome.met,
        )
    referenced = {
        name for group in rule.zones.values() for name in factors_in(group) if name in factors
    }
    inputs: dict[str, Any] = {
        "rule_ref": rule.rule_ref,
        "rule_instance_id": rule.rule_instance_id,
        "rule_version": rule.rule_version,
        "template_ref": rule.template_ref,
        "factors": _factor_payload({name: factors[name] for name in referenced}),
        "valuation": valuation_input(positions),
    }
    return ListingEvaluation(
        rule=rule,
        as_of=as_of,
        input_digest=input_digest({"as_of": as_of, **inputs}),
        inputs=inputs,
        signals=signals,
        undecided=tuple(undecided),
        positions=tuple(positions),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DetectionResult:
    created: tuple[int, ...] = ()
    notified: tuple[int, ...] = ()
    archived_candidates: tuple[int, ...] = ()


async def _locked_state(
    session: AsyncSession, *, listing_id: int, scope: str, zone: str
) -> ZoneIntervalStateRecord:
    # Reserve the writer before reading (SQLite ignores SELECT FOR UPDATE).
    # The no-op self-assignment keeps ``updated_at`` in place - the lock is
    # not a change and must not refresh the audit timestamp.
    await session.execute(
        update(ZoneIntervalStateRecord)
        .where(
            ZoneIntervalStateRecord.listing_id == listing_id,
            ZoneIntervalStateRecord.scope == scope,
            ZoneIntervalStateRecord.zone == zone,
        )
        .values(
            inside=ZoneIntervalStateRecord.inside,
            updated_at=ZoneIntervalStateRecord.updated_at,
        )
    )
    stmt = (
        select(ZoneIntervalStateRecord)
        .where(
            ZoneIntervalStateRecord.listing_id == listing_id,
            ZoneIntervalStateRecord.scope == scope,
            ZoneIntervalStateRecord.zone == zone,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = (await session.execute(stmt)).scalar_one_or_none()
    if state is None:
        state = ZoneIntervalStateRecord(
            listing_id=listing_id, scope=scope, zone=zone, inside=False
        )
        session.add(state)
        await session.flush()
    return state


async def _existing_event(
    session: AsyncSession, event: ZoneEventRecord
) -> ZoneEventRecord | None:
    stmt = select(ZoneEventRecord).where(
        ZoneEventRecord.listing_id == event.listing_id,
        ZoneEventRecord.scope == event.scope,
        ZoneEventRecord.zone == event.zone,
        ZoneEventRecord.rule_instance_id == event.rule_instance_id,
        ZoneEventRecord.as_of == event.as_of,
        ZoneEventRecord.transition == event.transition,
        ZoneEventRecord.input_digest == event.input_digest,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _event_of(
    evaluation: ListingEvaluation, zone: str, transition: str, *, level: str
) -> ZoneEventRecord:
    rule = evaluation.rule
    return ZoneEventRecord(
        as_of=evaluation.as_of,
        listing_id=evaluation.listing_id,
        ticker=evaluation.ticker,
        scope=evaluation.scope,
        zone=zone,
        transition=transition,
        level=level,
        rule_instance_id=rule.rule_instance_id,
        rule_version=rule.rule_version,
        template_id=rule.template_id,
        template_version=rule.template_version,
        calibration_status=rule.calibration_status,
        conditions_met=[],
        inputs=dict(evaluation.inputs),
        input_digest=evaluation.input_digest,
        account_id=evaluation.account_id,
        valuation_ids=list(evaluation.valuation_ids),
        ledger_revision=evaluation.ledger_revision,
        knowledge_cutoff=evaluation.knowledge_cutoff,
    )


async def record_evaluations(
    session: AsyncSession,
    evaluations: Sequence[ListingEvaluation],
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> DetectionResult:
    """Record the zones that hold and the exits, and notify entries once.

    One event per ``(as_of, input pair)`` while a zone holds - the daily series
    the review pages read - and one ``left`` event when it stops holding. The
    *notice* is driven by the interval state only: entering raises it once,
    staying inside (including on revised inputs) raises nothing, leaving and
    re-entering raises it again (funnel 9).
    """

    at = now or datetime.now(UTC)
    created: list[int] = []
    notified: list[int] = []
    dropped: list[ZoneEventRecord] = []
    for evaluation in evaluations:
        for zone in sorted(evaluation.signals):
            signal = evaluation.signals[zone]
            state = await _locked_state(
                session,
                listing_id=evaluation.listing_id,
                scope=evaluation.scope,
                zone=zone,
            )
            if state.rule_instance_id != evaluation.rule.rule_instance_id:
                # A new rule version restarts the interval: the user has just
                # approved a (possibly different) commitment, so its first
                # firing notices again instead of staying silent.
                state.rule_instance_id = evaluation.rule.rule_instance_id
                state.inside = False
            was_inside = bool(state.inside)
            state.last_evaluated_as_of = evaluation.as_of
            state.last_evaluated_at = at
            if not signal.holds and not was_inside:
                # Outside and staying outside: no event, nothing to report.
                await session.flush()
                continue
            transition = "entered" if signal.holds else "left"
            event = _event_of(evaluation, zone, transition, level=signal.level)
            event.conditions_met = (
                [jsonable(dict(reason)) for reason in signal.conditions_met] if signal.holds else []
            )
            stored = await _existing_event(session, event)
            if stored is None:
                session.add(event)
                await session.flush()
                stored = event
                created.append(stored.id)
            state.last_event_id = stored.id
            if signal.holds:
                state.inside = True
                if not was_inside:
                    state.entered_as_of = evaluation.as_of
                    state.entered_at = at
                    # The entry notice: raised once per entry, even when
                    # revised inputs later re-state the same interval.
                    await notify_zone_entry(session, stored, at=at, settings=settings)
                    stored.notified_at = at
                    state.last_notified_at = at
                    notified.append(stored.id)
                    if zone == "drop" and evaluation.scope == CANDIDATE_SCOPE:
                        dropped.append(stored)
            else:
                state.inside = False
                state.entered_as_of = None
                state.entered_at = None
            await session.flush()
    archived: tuple[int, ...] = ()
    if dropped:
        archived = await _archive_dropped_candidates(session, dropped, now=at)
    log.info(
        "zone_detection.recorded",
        created=created,
        notified=notified,
        archived_candidates=list(archived),
    )
    return DetectionResult(
        created=tuple(created), notified=tuple(notified), archived_candidates=archived
    )


async def _close_intervals(
    session: AsyncSession, rule: ZoneRule, *, as_of: date, at: datetime
) -> None:
    """Record ``left`` for zones a rule still shows as inside when its listing
    no longer has a position to evaluate (plan 6.2 WP8).

    Without an evaluation there are no factors or valuations to freeze, so the
    event's inputs say so explicitly; the identity stays reproducible (same
    rule, date and empty inputs -> same digest), and leaving notifies nothing,
    like every other ``left``.
    """

    inputs: dict[str, Any] = {
        "rule_ref": rule.rule_ref,
        "rule_instance_id": rule.rule_instance_id,
        "rule_version": rule.rule_version,
        "template_ref": rule.template_ref,
        "factors": {},
        "valuation": None,
        "position_closed": True,
    }
    digest = input_digest({"as_of": as_of, **inputs})
    states = list(
        (
            await session.scalars(
                select(ZoneIntervalStateRecord).where(
                    ZoneIntervalStateRecord.listing_id == rule.listing_id,
                    ZoneIntervalStateRecord.scope == rule.scope,
                    ZoneIntervalStateRecord.rule_instance_id == rule.rule_instance_id,
                    ZoneIntervalStateRecord.zone.in_(sorted(rule.zones)),
                    ZoneIntervalStateRecord.inside.is_(True),
                )
            )
        ).all()
    )
    for state in states:
        state.last_evaluated_as_of = as_of
        state.last_evaluated_at = at
        event = ZoneEventRecord(
            as_of=as_of,
            listing_id=rule.listing_id,
            ticker=rule.ticker,
            scope=rule.scope,
            zone=state.zone,
            transition="left",
            level=zone_level(state.zone),
            rule_instance_id=rule.rule_instance_id,
            rule_version=rule.rule_version,
            template_id=rule.template_id,
            template_version=rule.template_version,
            calibration_status=rule.calibration_status,
            conditions_met=[],
            inputs=dict(inputs),
            input_digest=digest,
        )
        stored = await _existing_event(session, event)
        if stored is None:
            session.add(event)
            await session.flush()
            stored = event
        state.last_event_id = stored.id
        state.inside = False
        state.entered_as_of = None
        state.entered_at = None
        await session.flush()
    if states:
        log.info(
            "zone_detection.position_closed_left",
            ticker=rule.ticker,
            zones=[state.zone for state in states],
            as_of=as_of.isoformat(),
        )


async def notify_zone_entry(
    session: AsyncSession, event: ZoneEventRecord, *, at: datetime, settings: Settings | None
) -> None:
    """Walk the agent notification outbox for an entry notice (best effort).

    The event row is the in-app notice; when the listing has an investor case
    the same entry also lands in ``investor_notification``, so the configured
    external channel carries it without a second delivery path. A missing case
    or an unconfigured channel is not an error - the event stays visible on the
    pages either way, and an entry still notifies exactly once.
    """

    if settings is None:
        return
    # The notification is best effort: it runs in its own savepoint so a failed
    # write (or delivery hiccup) rolls back alone instead of poisoning the
    # detection run's outer transaction.
    nested = await session.begin_nested()
    try:
        from investment_agent.investor.controls import UserControlService
        from investment_agent.investor.notifications import freeze
        from investment_agent.storage.models.investor import InvestorCase
        from investment_agent.storage.models.monitoring import InvestorNotification

        case = await session.scalar(
            select(InvestorCase).where(
                InvestorCase.owner_id == OWNER, InvestorCase.listing_id == event.listing_id
            )
        )
        if case is None:
            return
        if not await UserControlService(session).notifications_allowed(UUID(str(case.id))):
            return
        payload = {
            "category": "major_change" if event.level == "action_required" else "change",
            "stage": "changed",
            "level": event.level,
            "zone": event.zone,
            "scope": event.scope,
            "as_of": event.as_of.isoformat(),
            "rule_ref": {
                "rule_instance_id": event.rule_instance_id,
                "rule_version": event.rule_version,
                "template_ref": f"{event.template_id}@{event.template_version}",
            },
            "conditions_met": list(event.conditions_met),
            "calibration_status": event.calibration_status,
            "updated_at": at.isoformat(),
        }
        key = f"zone:{event.listing_id}:{event.scope}:{event.zone}"
        row = await session.scalar(
            select(InvestorNotification).where(InvestorNotification.key == key)
        )
        if row is None:
            session.add(
                InvestorNotification(
                    key=key,
                    owner_id=OWNER,
                    case_id=str(case.id),
                    revision=1,
                    payload={**payload, "read": False},
                )
            )
        else:
            row.revision += 1
            row.payload = {**row.payload, **payload, "read": False}
        await session.flush()
        current = await session.scalar(
            select(InvestorNotification).where(InvestorNotification.key == key)
        )
        if current is not None:
            await freeze(session, settings, current)
        await nested.commit()
    except Exception:
        await nested.rollback()
        # A delivery hiccup never loses the event or blocks the detection run.
        log.exception("zone_detection.notify_failed", event_id=event.id, zone=event.zone)


async def _archive_dropped_candidates(
    session: AsyncSession, events: Sequence[ZoneEventRecord], *, now: datetime
) -> tuple[int, ...]:
    """funnel 7.3: a candidate ``drop`` archives the open candidate and records it."""

    from investment_agent.candidates.lifecycle import archive_candidate_on_drop

    archived: list[int] = []
    for event in events:
        candidate = await session.scalar(
            select(CandidateRecord).where(
                CandidateRecord.listing_id == event.listing_id,
                CandidateRecord.status == "open",
            )
        )
        if candidate is None:
            continue
        if await archive_candidate_on_drop(session, candidate, event=event, now=now) is not None:
            archived.append(candidate.id)
    return tuple(archived)


# --------------------------------------------------------------------------- #
# The daily scan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ZoneScanOutcome:
    as_of: date
    scope: str
    rules: int
    evaluated: tuple[int, ...]
    skipped: tuple[str, ...]
    undecided: tuple[str, ...]
    result: DetectionResult = field(default_factory=DetectionResult)

    @property
    def created(self) -> tuple[int, ...]:
        return self.result.created

    def metadata(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "scope": self.scope,
            "rules": self.rules,
            "evaluated_listings": list(self.evaluated),
            "created_events": list(self.result.created),
            "notified_events": list(self.result.notified),
            "archived_candidates": list(self.result.archived_candidates),
            "skipped": list(self.skipped),
            "undecided": list(self.undecided),
        }


async def held_positions(
    session: AsyncSession, *, as_of: date, owner: str = OWNER
) -> dict[int, list[HoldingPosition]]:
    """``listing_id -> positions`` from each account's current valuation.

    A valuation dated after ``as_of`` is ignored (it cannot be an input of an
    earlier day), and the valuation's own date becomes the event's ``as_of`` -
    a run that fires before today's valuation exists cannot claim today.
    """

    accounts = await PortfolioService(session, owner).accounts()
    service = ValuationService(session, owner)
    priced: dict[int, list[HoldingPosition]] = {}
    for account in accounts:
        valuation = await service.current(UUID(str(account.id)))
        if valuation is None or valuation.valuation_date > as_of:
            continue
        for holding in valuation.holdings:
            position = position_of(valuation, holding, account_id=str(valuation.account_id))
            if position is None:
                continue
            priced.setdefault(int(holding.listing_ref.id), []).append(position)
    return priced


async def scan_scope(
    session: AsyncSession,
    *,
    as_of: date,
    scope: str,
    now: datetime | None = None,
    settings: Settings | None = None,
    rules: Sequence[ZoneRule] | None = None,
) -> ZoneScanOutcome:
    """Evaluate every active rule of one scope and record the outcome."""

    at = now or datetime.now(UTC)
    selected = list(rules) if rules is not None else await active_zone_rules(session, scope)
    positions = await held_positions(session, as_of=as_of) if scope == HOLDING_SCOPE else {}
    evaluations: list[ListingEvaluation] = []
    evaluated: list[int] = []
    skipped: list[str] = []
    undecided: list[str] = []
    for rule in selected:
        registry_names, position_names = referenced_factors(rule.zones)
        rule_positions = positions.get(rule.listing_id, [])
        if scope == HOLDING_SCOPE and not rule_positions:
            # A holding rule for a listing the user no longer holds has
            # nothing to evaluate; the rule itself stays approved. Zones it
            # still shows as inside get their ``left`` event, so a closed
            # position cannot leave the interval state stuck.
            skipped.append(f"{rule.ticker}:not_held")
            await _close_intervals(session, rule, as_of=as_of, at=at)
            continue
        # The event date (the valuation's own date for a holding rule) is also
        # the knowledge point of the factors: what the rule sees must be what
        # was knowable on that date, not on the later run date.
        event_date = (
            max(position.valuation_date for position in rule_positions)
            if rule_positions
            else as_of
        )
        try:
            factors: dict[str, FactorValue] = dict(
                await candidate_factor_values(session, rule.ticker, event_date, registry_names)
            )
        except Exception:
            log.warning(
                "zone_detection.factors_failed", ticker=rule.ticker, scope=scope, exc_info=True
            )
            skipped.append(f"{rule.ticker}:factors_unavailable")
            continue
        if position_names:
            factors.update(
                {
                    name: value
                    for name, value in holding_factor_values(rule_positions).items()
                    if name in position_names
                }
            )
        evaluation = evaluate_rule_zones(
            rule, as_of=event_date, factors=factors, positions=rule_positions
        )
        evaluations.append(evaluation)
        evaluated.append(rule.listing_id)
        undecided.extend(f"{rule.ticker}:{zone}" for zone in evaluation.undecided)
    result = await record_evaluations(session, evaluations, now=at, settings=settings)
    outcome = ZoneScanOutcome(
        as_of=as_of,
        scope=scope,
        rules=len(selected),
        evaluated=tuple(evaluated),
        skipped=tuple(skipped),
        undecided=tuple(undecided),
        result=result,
    )
    log.info("zone_detection.scanned", **outcome.metadata())
    return outcome


async def run_zone_detection(
    session: AsyncSession,
    *,
    as_of: date,
    scope: str,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> ZoneScanOutcome:
    """One deterministic detection run for one scope and date."""

    if scope not in {CANDIDATE_SCOPE, HOLDING_SCOPE}:
        raise ValueError(f"unknown zone scope {scope!r}")
    return await scan_scope(session, as_of=as_of, scope=scope, now=now, settings=settings)


# --------------------------------------------------------------------------- #
# Reads (page/API summaries)
# --------------------------------------------------------------------------- #


def event_block(event: ZoneEventRecord) -> dict[str, Any]:
    """One event as the API/pages see it (the rule version is always present)."""

    return {
        "id": event.id,
        "as_of": event.as_of,
        "listing_id": event.listing_id,
        "ticker": event.ticker,
        "scope": event.scope,
        "zone": event.zone,
        "transition": event.transition,
        "level": event.level,
        "rule_instance_id": event.rule_instance_id,
        "rule_version": event.rule_version,
        "template_id": event.template_id,
        "template_version": event.template_version,
        "template_ref": f"{event.template_id}@{event.template_version}",
        "rule_ref": f"{event.ticker}/{event.template_id}@{event.template_version}/v{event.rule_version}",
        "calibration_status": event.calibration_status,
        "conditions_met": list(event.conditions_met or []),
        "inputs": dict(event.inputs or {}),
        "input_digest": event.input_digest,
        "valuation_ids": list(event.valuation_ids or []),
        "ledger_revision": event.ledger_revision,
        "notified_at": event.notified_at,
        "created_at": event.created_at,
    }


async def zone_events(
    session: AsyncSession,
    *,
    listing_id: int | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> list[ZoneEventRecord]:
    """The newest events first (the review/history read)."""

    stmt = select(ZoneEventRecord).order_by(
        ZoneEventRecord.as_of.desc(), ZoneEventRecord.id.desc()
    )
    if listing_id is not None:
        stmt = stmt.where(ZoneEventRecord.listing_id == listing_id)
    if scope is not None:
        stmt = stmt.where(ZoneEventRecord.scope == scope)
    return list((await session.scalars(stmt.limit(limit))).all())


async def zone_summaries(
    session: AsyncSession, *, listing_ids: Sequence[int], scope: str
) -> dict[int, dict[str, Any]]:
    """``listing_id -> {interval, zones, latest}`` for the list pages (batched).

    ``interval`` is the headline state (the zone currently holding, else the
    most recently evaluated one); ``zones`` keeps every zone's state so a page
    can pin ``exit_review`` / ``drop`` without a second query.
    """

    ids = sorted({int(item) for item in listing_ids})
    if not ids:
        return {}
    states = list(
        (
            await session.scalars(
                select(ZoneIntervalStateRecord).where(
                    ZoneIntervalStateRecord.listing_id.in_(ids),
                    ZoneIntervalStateRecord.scope == scope,
                )
            )
        ).all()
    )
    by_listing: dict[int, list[ZoneIntervalStateRecord]] = {}
    for row in states:
        by_listing.setdefault(row.listing_id, []).append(row)
    latest: dict[int, ZoneEventRecord] = {}
    # One latest ``entered`` event per listing, ranked in the database: the
    # list pages call this per item, so pulling every historical entered event
    # only to keep the first per listing would grow without bound.
    ranked = (
        select(
            ZoneEventRecord.id,
            func.row_number()
            .over(
                partition_by=ZoneEventRecord.listing_id,
                order_by=(ZoneEventRecord.as_of.desc(), ZoneEventRecord.id.desc()),
            )
            .label("rn"),
        )
        .where(
            ZoneEventRecord.listing_id.in_(ids),
            ZoneEventRecord.scope == scope,
            ZoneEventRecord.transition == "entered",
        )
        .subquery()
    )
    recent = await session.scalars(
        select(ZoneEventRecord)
        .join(ranked, ZoneEventRecord.id == ranked.c.id)
        .where(ranked.c.rn == 1)
        .order_by(ZoneEventRecord.as_of.desc(), ZoneEventRecord.id.desc())
    )
    for event in recent.all():
        latest[event.listing_id] = event
    summaries: dict[int, dict[str, Any]] = {}
    for listing_id in ids:
        rows = by_listing.get(listing_id, [])
        newest: ZoneEventRecord | None = latest.get(listing_id)
        if not rows and newest is None:
            continue
        headline = _headline_state(rows, newest)
        summaries[listing_id] = {
            "interval": _state_block(headline),
            "zones": {
                row.zone: _state_block(row)
                for row in sorted(rows, key=lambda item: (item.zone,))
            },
            "latest": event_block(newest) if newest is not None else None,
        }
    return summaries


def _headline_state(
    rows: Sequence[ZoneIntervalStateRecord], latest: ZoneEventRecord | None
) -> ZoneIntervalStateRecord | None:
    inside = [row for row in rows if row.inside]
    if inside:
        return max(inside, key=lambda row: (row.entered_at or row.created_at, row.zone))
    if not rows:
        return None
    evaluated = [row for row in rows if row.last_evaluated_as_of is not None]
    candidates = evaluated or rows
    return max(
        candidates,
        key=lambda row: (
            row.last_evaluated_as_of or row.created_at.date(),
            row.last_event_id or 0,
            row.zone,
        ),
    )


def _state_block(row: ZoneIntervalStateRecord | None) -> dict[str, Any]:
    if row is None:
        return {
            "inside": False,
            "zone": None,
            "entered_as_of": None,
            "rule_instance_id": None,
            "notified_at": None,
        }
    return {
        "inside": bool(row.inside),
        "zone": row.zone,
        "entered_as_of": row.entered_as_of,
        "rule_instance_id": row.rule_instance_id,
        "notified_at": row.last_notified_at,
    }


__all__ = [
    "CANDIDATE_SCOPE",
    "HOLDING_SCOPE",
    "POSITION_FACTORS",
    "ZONE_LEVELS",
    "DetectionResult",
    "HoldingPosition",
    "ListingEvaluation",
    "ZoneRule",
    "ZoneScanOutcome",
    "ZoneScope",
    "ZoneSignal",
    "active_zone_rules",
    "candidate_factor_values",
    "event_block",
    "evaluate_rule_zones",
    "held_positions",
    "holding_factor_values",
    "input_digest",
    "notify_zone_entry",
    "position_of",
    "record_evaluations",
    "referenced_factors",
    "run_zone_detection",
    "scan_scope",
    "zone_events",
    "zone_level",
    "zone_summaries",
]
