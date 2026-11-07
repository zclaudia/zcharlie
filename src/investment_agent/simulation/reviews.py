"""Deterministic reviews quote stored decisions and calculated NAV, never model P&L."""

from datetime import date, datetime, time, timedelta

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.simulation.contracts import Decision, NavSnapshot, SimulationReview
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.risk import stable_id


def review_decision(
    decision: Decision,
    navs: tuple[NavSnapshot, ...],
    *,
    observation_start: date,
    now: datetime,
    version: int = 1,
) -> SimulationReview:
    end = observation_start + timedelta(days=decision.expected_holding_days or 1)
    mature_at = datetime.combine(end, time(15), SHANGHAI_TZ)
    relevant = []
    for nav in navs:
        if any(
            getattr(nav, k) != getattr(decision, k)
            for k in ("experiment_id", "participant_id", "account_id")
        ):
            raise SimulationError("scope_mismatch", "review NAV belongs to another participant")
        if observation_start <= nav.valuation_date and nav.created_at <= now:
            relevant.append(nav)
    candidates = sorted(
        n.valuation_date for n in relevant if n.quality == "complete" and n.valuation_date >= end
    )
    if candidates:
        end = candidates[0]
        mature_at = datetime.combine(end, time(15), SHANGHAI_TZ)
    relevant = [n for n in relevant if n.valuation_date <= end]
    # A closed observation window also requires a complete actual valuation at its end.
    complete = [n for n in relevant if n.quality == "complete"]
    matured = now >= mature_at and any(n.valuation_date >= end for n in complete)
    available = max(now, datetime.combine(end, time(0), SHANGHAI_TZ))
    text = (
        "观察期已结束，已附上确定性计算的账户结果。"
        "投资假设仍需后续事实验证，价格变化本身不能自动证伪投资逻辑。"
        if matured
        else "仍在观察：持有窗口尚未结束，或缺少期末完整估值。"
    )
    return SimulationReview(
        **{
            key: getattr(decision, key) for key in ("experiment_id", "participant_id", "account_id")
        },
        id=stable_id("review", decision.id, version),
        decision_id=decision.id,
        observation_start=observation_start,
        observation_end=end,
        available_at=available,
        status="matured" if matured else "observing",
        evidence_refs=decision.evidence_refs,
        nav_refs=tuple(n.id for n in complete),
        findings=text,
        memory_eligible=False,
        version=version,
    )
