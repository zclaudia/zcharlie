"""P1-C2: the five v1 templates validate against the real registry and groups."""

from __future__ import annotations

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.rules.schema import (
    CANDIDATE_ZONES,
    HOLDING_ZONES,
    Condition,
    canonical_hash,
)
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import DEFAULT_TEMPLATES_DIR, load_templates
from investment_agent.rules.validation import PENDING_FACTORS, factors_in

EXPECTED = {
    "cycle_reversal",
    "dividend_blue_chip",
    "high_growth_venture",
    "quality_growth",
    "steady_quality_growth",
}


def test_all_five_templates_validate() -> None:
    loaded = load_templates(DEFAULT_TEMPLATES_DIR)
    assert {template.id for _, template in loaded} == EXPECTED
    registry = set(DEFAULT_REGISTRY.names())
    for path, template in loaded:
        assert path.stem == template.id
        assert template.calibration_status == "uncalibrated"
        assert set(template.candidate_zones) == set(CANDIDATE_ZONES)
        assert set(template.holding_zones) == set(HOLDING_ZONES)
        # Screen and candidate zones only use computable factors ...
        assert factors_in(template.screen) <= registry, template.id
        for zone in template.candidate_zones.values():
            assert factors_in(zone) <= registry, template.id
        # ... holding zones may already use the P2 position factors.
        for zone in template.holding_zones.values():
            assert factors_in(zone) <= registry | PENDING_FACTORS, template.id
        # Every screen threshold is a parameter, so instances can tune it in range.
        for condition in template.screen.children:
            assert isinstance(condition, Condition)
            values = condition.value if isinstance(condition.value, list) else [condition.value]
            assert all(isinstance(v, str) and v.startswith("$") for v in values), (
                template.id,
                condition,
            )


async def test_templates_sync_idempotently(db_session) -> None:
    loaded = load_templates(DEFAULT_TEMPLATES_DIR)
    store = RuleStore(db_session)
    first = await store.sync_templates(loaded)
    assert {(o.template_id, o.version, o.action) for o in first} == {
        (template_id, 1, "created") for template_id in EXPECTED
    }
    second = await store.sync_templates(load_templates(DEFAULT_TEMPLATES_DIR))
    assert {o.action for o in second} == {"unchanged"}
    stored = {t.id: t for t in await store.latest_templates()}
    for _, template in loaded:
        assert canonical_hash(stored[template.id]) == canonical_hash(template)
