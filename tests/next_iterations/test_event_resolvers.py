"""EV-05 exact persisted source identities, owner gates and closed registrations."""

import copy
import json
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.next_iterations.test_event_append import FIXTURES, NOW, request, seed_round
from tests.next_iterations.test_event_append import db as db

from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Case,
    CompanyRecommendation,
    RecommendationEvent,
    ThesisRevision,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events.contracts import (
    SOURCE_REF_ADAPTER,
    ReviewCyclePayload,
    StoredEvent,
)
from investment_agent.research_events.registry import DEFAULT_CONSUMERS, EventRegistry
from investment_agent.research_events.repository import append_event
from investment_agent.research_events.resolvers import (
    ResolvedSource,
    install_builtin_sources,
    read_calculation_ref,
    read_case_ref,
    read_evidence_ref,
    read_review_comparison,
    resolve_review_cycle,
)
from investment_agent.storage.models.analysis import (
    PipelineRun,
    ResearchChangeSet,
    ResearchSnapshot,
)
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.investor import (
    CaseRevision,
    InvestorCase,
    UserObject,
    UserObjectRevision,
)
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.publication import PublicationArtifact, PublicationEvent
from investment_agent.storage.models.research_events import ResearchDomainEvent
from investment_agent.storage.models.review_panel import ReviewRoundRecord


def uid(number: int) -> str:
    return str(UUID(int=number))
FIXTURE_EVENTS = {item["event_type"]: item for item in json.loads((FIXTURES / "events.valid.json").read_text())["events"]}


def event_request(name: str) -> dict[str, Any]:
    value = copy.deepcopy(FIXTURE_EVENTS[name])
    return {key: value[key] for key in ("owner_id", "event_type", "source_ref", "source_revision", "payload")} | {"occurred_at": NOW}


async def seed_case(session: AsyncSession, *, owner: str = "local_owner") -> Case:
    case = Case.model_validate({
        "id": uid(102), "schema_version": 1, "revision": 4, "created_at": NOW - timedelta(days=10),
        "actor": "user", "origin_ref": {"kind": "instruction", "id": uid(900), "revision": 1},
        "user_id": "local_owner", "listing_ref": {"kind": "listing", "id": 18, "revision": 1},
        "status": "researching", "episode": 1, "pinned": False, "background_paused": False,
        "user_archived": False, "muted": True, "current_thesis_ref": None, "current_recommendation_ref": None,
    })
    case_payload = case.model_dump(mode="json")
    session.add(InvestorCase(id=uid(102), owner_id=owner, listing_id=18, revision=4,
                             payload=case_payload, source_refs=[]))
    session.add(CaseRevision(case_id=uid(102), revision=4, payload=case_payload))
    # Version 3 remains readable after head advances, with its own saved fields.
    prior = {**case_payload, "revision": 3, "muted": False}
    session.add(CaseRevision(case_id=uid(102), revision=3, payload=prior))
    instruction = {"id": uid(900), "revision": 1, "created_at": NOW.isoformat(),
                   "parameters": {"operation": "case_action", "case_ref": {"kind": "case", "id": uid(102), "revision": 3}, "action": "mute", "reason": "fixture"}}
    session.add(UserObject(id=uid(900), owner_id=owner, kind="instruction", revision=1, payload=instruction))
    session.add(UserObjectRevision(id=uid(900), revision=1, payload=instruction))
    await session.flush()
    return case


async def seed_change(session: AsyncSession, *, listing_id: int | None = 18) -> None:
    session.add(PipelineRun(id=80, kind="ifind_research", listing_id=listing_id, triggered_by="fixture",
                            started_at=NOW, status="running", stage="snapshot", attempt_count=0,
                            max_attempts=1, request_json={}))
    for identity, run_id in ((54, 79), (55, 80)):
        session.add(ResearchSnapshot(id=identity, pipeline_run_id=run_id, symbol="600036", market="SH",
            company_name="fixture", industry="fixture", as_of=NOW.date(), report_year=2025,
            canonical_schema_version="fixture", scoring_version="fixture", score_schema_version="fixture",
            score_json={}, explanation_json=None, artifact_manifest={}, created_at=NOW))
    session.add(ResearchChangeSet(id=42, current_snapshot_id=55, previous_snapshot_id=54,
        schema_version="fixture", baseline=False, has_meaningful_changes=True, change_count=1,
        meaningful_change_count=1, diff_json={"source_changed": False}, created_at=NOW))
    await session.flush()


async def test_case_source_reads_instruction_action_and_time(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        case = await seed_case(session)
        result = await append_event(session, **event_request("case.control_changed"))
        assert result.occurred_at == NOW and result.occurred_at != case.created_at
        historical = await read_case_ref(session, "local_owner", ObjectRef(kind="case", id=UUID(int=102), revision=3))
        assert historical.revision == 3 and historical.muted is False


@pytest.mark.parametrize("field,value", [("action", "archive"), ("case_episode", 2)])
async def test_case_control_cannot_forge_action_or_episode(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], field: str, value: object) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_case(session)
        body = event_request("case.control_changed")
        body["payload"][field] = value
        with pytest.raises(InvestorError):
            await append_event(session, **body)
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def test_case_owner_comes_from_persistent_head_not_case_payload(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_case(session, owner="other_owner")
        with pytest.raises(InvestorError) as failure:
            await append_event(session, **event_request("case.control_changed"))
        assert failure.value.detail.code == "not_found"


@pytest.mark.parametrize("has_listing_fk", [True, False])
async def test_research_change_exact_snapshot_run_listing_mapping(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], has_listing_fk: bool) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        await seed_change(session, listing_id=18 if has_listing_fk else None)
        result = await append_event(session, **event_request("research_change.recorded"))
        assert result.source_ref.kind == "research_change_set" and result.payload.subject.listing_id == 18  # type: ignore[union-attr]


@pytest.mark.parametrize("mutation", ["snapshot_market", "run_listing", "previous_listing", "source_flags", "run_ref"])
async def test_research_change_rejects_mismatched_persistent_identity(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], mutation: str) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_change(session)
        body = event_request("research_change.recorded")
        if mutation == "snapshot_market":
            row = await session.get(ResearchSnapshot, 55)
            assert row is not None
            row.market = "SZ"
        elif mutation == "run_listing":
            run = await session.get(PipelineRun, 80)
            assert run is not None
            run.listing_id = 19
        elif mutation == "previous_listing":
            row = await session.get(ResearchSnapshot, 54)
            assert row is not None
            row.symbol = "600000"
        elif mutation == "source_flags":
            body["payload"]["baseline"] = True
        else:
            body["payload"]["run_ref"]["id"] = 81
        with pytest.raises(InvestorError):
            await append_event(session, **body)
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def test_round_comparison_requires_independent_id_parent_and_algorithm(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        assert (await read_review_comparison(session, "local_owner", 452, round_id=381, algorithm_version="v1")).id == 452
        for comparison_id, round_id, version in ((381, 381, "v1"), (452, 382, "v1"), (452, 381, "v2")):
            with pytest.raises(InvestorError):
                await read_review_comparison(session, "local_owner", comparison_id, round_id=round_id, algorithm_version=version)


@pytest.mark.parametrize("mutation", ["missing", "incomplete", "subject", "owner", "reasons"])
async def test_round_missing_subject_owner_or_result_cannot_publish(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], mutation: str) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        body = request()
        if mutation == "missing":
            body["source_ref"]["id"] = 999
        elif mutation == "incomplete":
            row = await session.get(ReviewRoundRecord, 381)
            assert row is not None
            row.status = "running"
        elif mutation == "subject":
            body["payload"]["subject"]["listing_id"] = 19
        elif mutation == "owner":
            body["owner_id"] = "other_owner"
        else:
            body["payload"]["notify_reasons"] = ["stance_flip"]
        with pytest.raises(InvestorError):
            await append_event(session, **body)
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def seed_publication_event(session: AsyncSession, *, owner: str = "local_owner") -> None:
    payload = event_request("recommendation.validity_changed")["payload"]
    event = RecommendationEvent.model_validate({
        "id": uid(103), "revision": 1, "created_at": NOW, "actor": "research_service", "origin_ref": None,
        "recommendation_ref": payload["recommendation_ref"], "type": payload["validity"],
        "review_ref": None, "checked_at": NOW, "checked_dependency_refs": [], "next_review_at": None,
        "effective_valid_until": None, "replacement_ref": None,
    })
    dimensions: list[dict[str, Any]] = [{"dimension": dimension, "status": "missing", "applicable": True,
                   "reason": "fixture gap", "refs": []} for dimension in DIMENSIONS]
    metadata = {"id": uid(907), "revision": 1, "created_at": NOW, "actor": "research_service", "origin_ref": None}
    method_ref = {"kind": "method", "id": uid(908), "revision": 1}
    thesis = ThesisRevision.model_validate({**metadata, "case_ref": payload["case_ref"],
        "previous_ref": None, "change_reason": "fixture", "question_scope": "fixture",
        "assessed_dimensions": dimensions, "claims": [], "assumptions": [], "counter_case": [],
        "evidence_set_hash": "fixture", "method_ref": method_ref, "as_of": NOW})
    published = CompanyRecommendation.model_validate({**metadata, "id": uid(104),
        "scope": "company_view", "case_ids": [uid(102)],
        "thesis_revision_refs": [{"kind": "thesis", "id": uid(907), "revision": 1}],
        "as_of": NOW, "question_scope": "fixture", "assessed_dimensions": dimensions,
        "sufficiency": "bounded", "claims": [], "counter_case": [], "assumptions": [],
        "gaps": ["fixture"], "conditions": [], "valid_until": NOW + timedelta(days=7),
        "next_review_at": NOW + timedelta(days=1), "monitor_refs": [], "dependency_refs": [],
        "method_revision_id": method_ref, "provenance": [], "business_view": "undetermined",
        "price_view": "undetermined", "conditional_actions": []})
    session.add(PublicationArtifact(id=uid(907), revision=1, kind="thesis", owner_id=owner,
                                    case_id=uid(102), payload=thesis.model_dump(mode="json")))
    session.add(PublicationArtifact(id=uid(104), revision=1, kind="recommendation", owner_id=owner,
                                    case_id=uid(102), payload=published.model_dump(mode="json")))
    session.add(PublicationEvent(sequence=753, recommendation_id=uid(104), owner_id=owner,
                                payload=event.model_dump(mode="json")))
    await session.flush()


async def test_recommendation_event_uses_payload_uuid_not_physical_sequence(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        await seed_case(session)
        await seed_publication_event(session)
        result = await append_event(session, **event_request("recommendation.validity_changed"))
        assert result.source_ref.id == UUID(int=103)
        assert result.source_ref.id != UUID(int=753)
        raw = event_request("recommendation.validity_changed")
        raw["source_ref"]["id"] = uid(753)
        with pytest.raises(InvestorError):
            await append_event(session, **raw)


@pytest.mark.parametrize("mutation", ["owner", "duplicate_uuid", "wrong_validity", "artifact_owner"])
async def test_recommendation_event_enforces_owner_uniqueness_and_payload(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], mutation: str) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_case(session)
        await seed_publication_event(session, owner="other_owner" if mutation == "owner" else "local_owner")
        body = event_request("recommendation.validity_changed")
        if mutation == "duplicate_uuid":
            existing = await session.get(PublicationEvent, 753)
            assert existing is not None
            session.add(PublicationEvent(sequence=754, recommendation_id=existing.recommendation_id,
                                         owner_id=existing.owner_id, payload=existing.payload))
        elif mutation == "wrong_validity":
            body["payload"]["validity"] = "superseded"
        elif mutation == "artifact_owner":
            # Seed a foreign artifact from the beginning; immutable artifacts
            # cannot be modified via ORM after persistence.
            from sqlalchemy import text
            await session.execute(text("UPDATE investor_publication_artifact SET owner_id='other_owner'"))
        with pytest.raises(InvestorError):
            await append_event(session, **body)
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def test_evidence_and_calculation_helpers_enforce_row_owner(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        evidence = EvidenceRecord.model_validate({"id": uid(105), "revision": 1, "created_at": NOW,
            "actor": "fixture", "origin_ref": None, "source_ref": {"kind": "disclosure_original", "id": uid(906), "revision": 1},
            "source_kind": "document", "locator": "fixture", "content_hash": "a" * 64,
            "subject_ref": {"kind": "listing", "id": 18, "revision": 1}, "period": None,
            "published_at": NOW, "acquired_at": NOW, "available_from": NOW, "verification": "candidate",
            "conflict": "none", "text": "candidate fixture"})
        calculation = CalculationRecord.model_validate({"id": uid(204), "revision": 1, "created_at": NOW,
            "actor": "fixture", "origin_ref": None, "method": "portfolio_stress", "method_version": "fixture",
            "fact_refs": [], "assumption_refs": [], "input_hash": "a" * 64, "as_of": NOW,
            "applicable": False, "status": "missing_inputs", "results": {}})
        session.add_all([SavedEvidence(id=uid(105), revision=1, owner_id="local_owner", payload=evidence.model_dump(mode="json")),
                         SavedCalculation(id=uid(204), revision=1, owner_id="other_owner", payload=calculation.model_dump(mode="json"))])
        await session.flush()
        actual = await read_evidence_ref(session, "local_owner", ObjectRef(kind="evidence", id=UUID(int=105), revision=1))
        assert actual.verification == "candidate"
        with pytest.raises(InvestorError) as failed:
            await read_calculation_ref(session, "local_owner", ObjectRef(kind="calculation", id=UUID(int=204), revision=1))
        assert failed.value.detail.code == "not_found"
        for name in ("source.invalidated", "portfolio_stress.partial"):
            with pytest.raises(InvestorError, match="not installed"):
                await append_event(session, **event_request(name))


async def test_cycle_does_not_guess_revision_from_mutable_payload(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]]) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_case(session)
        session.add(ReviewCycle(id=uid(101), case_id=uid(102), task_id=None, state="pending", payload={"revision": 2}))
        await session.flush()
        value = event_request("review_cycle.changed")
        with pytest.raises(InvestorError) as failure:
            await resolve_review_cycle(session, "local_owner", SOURCE_REF_ADAPTER.validate_python(value["source_ref"]),
                                       2, ReviewCyclePayload.model_validate(value["payload"]))
        assert failure.value.detail.code == "dependency_unavailable"
        with pytest.raises(InvestorError):
            await append_event(session, **value)


@pytest.mark.parametrize("name", ["source.invalidated", "topic.observation_changed", "decision_observation.due", "decision_fact_check.completed", "portfolio_stress.completed"])
async def test_uninstalled_source_adapters_never_publish(db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], name: str) -> None:
    _, factory = db
    async with factory() as session:
        with pytest.raises(InvestorError) as failure:
            await append_event(session, **event_request(name))
        assert failure.value.detail.code == "invalid_reference"
        assert await session.scalar(select(ResearchDomainEvent)) is None


async def never_resolve(session: AsyncSession, owner: str, ref: object, revision: int, payload: object) -> ResolvedSource:
    raise AssertionError("not invoked by registration tests")


async def consume(session: AsyncSession, event: StoredEvent) -> object:
    return None


def test_registry_limits_source_events_and_consumer_names() -> None:
    registry = EventRegistry()
    for name in DEFAULT_CONSUMERS:
        registry.register_consumer(name, consume, event_types=("research_change.recorded",))
    assert set(registry.consumer_names) == DEFAULT_CONSUMERS
    builtins = EventRegistry()
    install_builtin_sources(builtins)
    assert set(builtins.source_kinds) == {"review_round", "research_change_set", "case", "recommendation_event"}
    with pytest.raises(ValueError):
        registry.register_source("unknown", never_resolve, allowed_event_types=("research_change.recorded",))
    with pytest.raises(ValueError):
        registry.register_source("case", never_resolve, allowed_event_types=("review_panel.finalized",))
    with pytest.raises(ValueError):
        registry.register_consumer("arbitrary_webhook.v1", consume, event_types=("research_change.recorded",))
    with pytest.raises(ValueError):
        registry.register_consumer("research_inbox.v1", consume, event_types=("research_change.recorded",))
    with pytest.raises(ValueError):
        EventRegistry(approved_consumers=frozenset({"unstable-or-url"}))
    # A new explicitly reviewed consumer scope can be installed without making
    # the storage/API hard-code only the inbox consumer.
    scoped = EventRegistry(approved_consumers=DEFAULT_CONSUMERS | {"quality_source_validity.v1"})
    scoped.register_consumer("quality_source_validity.v1", consume, event_types=("source.invalidated",))
    assert scoped.consumer("quality_source_validity.v1").name == "quality_source_validity.v1"


async def test_replay_still_checks_persistent_access_before_original_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from sqlalchemy import text

    _, factory = db
    async with factory() as session, session.begin():
        await seed_round(session)
        await seed_case(session)
        await append_event(session, **event_request("case.control_changed"))
    async with factory() as session, session.begin():
        await session.execute(text("UPDATE investor_case SET owner_id='other_owner'"))
    async with factory() as session:
        with pytest.raises(InvestorError) as failure:
            await append_event(session, **event_request("case.control_changed"))
        assert failure.value.detail.code == "not_found"
        assert len((await session.scalars(select(ResearchDomainEvent))).all()) == 1


@pytest.mark.parametrize("new_episode", [1, 2])
async def test_recommendation_case_ref_cannot_select_another_historical_revision(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]], new_episode: int,
) -> None:
    _, factory = db
    async with factory() as session:
        await seed_round(session)
        case = await seed_case(session)
        await seed_publication_event(session)
        head = await session.get(InvestorCase, uid(102))
        assert head is not None
        alternate = case.model_copy(update={"revision": 5, "episode": new_episode})
        head.revision, head.payload = 5, alternate.model_dump(mode="json")
        session.add(CaseRevision(case_id=uid(102), revision=5, payload=alternate.model_dump(mode="json")))
        request_body = event_request("recommendation.validity_changed")
        request_body["payload"]["case_ref"]["revision"] = 5
        with pytest.raises(InvestorError, match="published case version"):
            await append_event(session, **request_body)
        assert await session.scalar(select(ResearchDomainEvent)) is None
        accepted = await append_event(session, **event_request("recommendation.validity_changed"))
        assert accepted.payload.case_ref.revision == 3  # type: ignore[union-attr]


async def test_recommendation_checked_references_are_a_set_on_both_sides(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    from sqlalchemy import text

    _, factory = db
    async with factory() as session:
        await seed_round(session)
        await seed_case(session)
        await seed_publication_event(session)
        # Deliberately seed duplicate references through SQL before any event
        # append; this does not change the immutable source after publication.
        source = await session.get(PublicationEvent, 753)
        assert source is not None
        reference = {"kind": "listing", "id": 18, "revision": 1, "field_path": None}
        raw = {**source.payload, "checked_dependency_refs": [reference, reference]}
        await session.execute(text("UPDATE investor_publication_event SET payload=:value WHERE sequence=753"),
                              {"value": json.dumps(raw)})
        body = event_request("recommendation.validity_changed")
        body["payload"]["exact_source_refs"] = [reference]
        assert (await append_event(session, **body)).event_type == "recommendation.validity_changed"
