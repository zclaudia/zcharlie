"""QT-016 direct current-source checks, without the events system."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from investment_agent.evidence.disclosures import DisclosureService, FetchRequest
from investment_agent.evidence.documents import save_candidates
from investment_agent.evidence.original_parser import parse_original
from investment_agent.evidence.queries import SecurityIdentity
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.research_quality.contracts import QualityCaseSaved
from investment_agent.research_quality.envelope import build_envelope
from investment_agent.research_quality.refs import QualityArtifactRef
from investment_agent.research_quality.source_validity import (
    SourceValidityError,
    SourceValidityService,
)
from investment_agent.storage.database import engine_lifespan
from investment_agent.storage.models import Base
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.disclosure import PUBLISHED, FixtureHTTP, discovery
from tests.support.originals import html, original, snapshot
from tests.support.quality_dataset import load_dataset


@pytest_asyncio.fixture
async def source_sessions(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'quality-sources.db'}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)


async def seed_sources(session):
    recipe = load_dataset()[0]
    session.add(Market(code="SSE", name="Synthetic", timezone="Asia/Shanghai", currency="CNY"))
    session.add(Company(id=180001, canonical_name="Synthetic"))
    session.add(Listing(id=180001, company_id=180001, market_code="SSE", ticker="600001"))
    for row in {item.snapshot.id: item.snapshot for item in recipe.materials}.values():
        session.add(FinancialSnapshot(**row.model_dump()))
    await session.flush()
    refs = tuple(
        [
            await EvidenceService(session).save_evidence("local_owner", item.evidence, uuid4())
            for item in recipe.materials
        ]
    )
    case = QualityCaseSaved(
        case_ref=QualityArtifactRef(kind="quality_case", id=uuid4(), revision=1),
        listing_ref=recipe.listing_ref,
        question=recipe.question,
        cutoff=recipe.cutoff,
        material_mode="observed_materials",
        material_refs=refs,
    )
    identity = SecurityIdentity(
        listing_ref=recipe.listing_ref,
        ticker="600001.SH",
        market="SSE",
        company_name="Synthetic",
        currency="CNY",
    )
    return case, identity


async def freeze_case(session, case, identity):
    service = SourceValidityService(session)
    entries = tuple(
        [
            await service.resolve(ref, case.listing_ref, case.cutoff, case.material_mode)
            for ref in case.material_refs
        ]
    )
    return build_envelope(case, identity, entries)


@pytest.mark.asyncio
async def test_exact_native_saved_evidence_and_current_owner_loss(source_sessions):
    async with source_sessions() as session, session.begin():
        case, identity = await seed_sources(session)
        frozen = await freeze_case(session, case, identity)
        assert all(item.verification == "verified" for item in frozen.materials)
        service = SourceValidityService(session)
        await service.validate(frozen)
        await session.execute(
            update(SavedEvidence)
            .where(SavedEvidence.id == str(case.material_refs[0].id))
            .values(owner_id="other")
        )
        with pytest.raises(SourceValidityError) as error:
            await service.validate(frozen)
        assert error.value.code == "source_unavailable"


@pytest.mark.asyncio
async def test_snapshot_field_revision_hash_and_candidate_state_are_real(source_sessions):
    async with source_sessions() as session, session.begin():
        case, identity = await seed_sources(session)
        recipe = load_dataset()[0].materials[0]
        ref = recipe.evidence.source_ref
        service = SourceValidityService(session)
        first = await service.resolve(ref, case.listing_ref, case.cutoff)
        envelope = build_envelope(
            case.model_copy(update={"material_refs": (ref,)}), identity, (first,)
        )
        row = await session.get(FinancialSnapshot, ref.id)
        changed = dict(row.fields, verification={})
        await session.execute(
            update(FinancialSnapshot).where(FinancialSnapshot.id == ref.id).values(fields=changed)
        )
        current = await service.resolve(ref, case.listing_ref, case.cutoff)
        assert current.verification == "candidate"
        with pytest.raises(SourceValidityError) as error:
            await service.validate(envelope)
        assert error.value.code == "source_hash_changed"
        for bad in (
            ref.model_copy(update={"revision": 999}),
            ref.model_copy(update={"field_path": "fields.invented"}),
        ):
            with pytest.raises(SourceValidityError):
                await service.resolve(bad, case.listing_ref, case.cutoff)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["published_at", "acquired_at", "available_from"])
async def test_native_saved_read_refuses_each_late_time(source_sessions, field):
    async with source_sessions() as session, session.begin():
        case, _ = await seed_sources(session)
        ref = case.material_refs[0]
        row = await session.get(SavedEvidence, (str(ref.id), ref.revision))
        changed = dict(row.payload, **{field: (case.cutoff + timedelta(seconds=1)).isoformat()})
        await session.execute(
            update(SavedEvidence).where(SavedEvidence.id == row.id).values(payload=changed)
        )
        with pytest.raises(SourceValidityError):
            await SourceValidityService(session).resolve(ref, case.listing_ref, case.cutoff)


@pytest.mark.asyncio
async def test_native_verified_correction_stops_original_and_copied_fact(source_sessions):
    async with source_sessions() as session, session.begin():
        payload, body, request = await original(session)
        await snapshot(session, request.listing_id)
        first = await save_candidates(
            session, payload, body, parse_original(body, "text/html"), request
        )
        fact = ObjectRef.model_validate(first["fact_refs"][0])
        listing = ObjectRef(kind="listing", id=request.listing_id, revision=1)
        cutoff = datetime.now(UTC)
        source = SourceValidityService(session)
        before = await source.resolve(fact, listing, cutoff)
        assert before.verification == "verified"
        original_ref = ObjectRef(kind="disclosure_original", id=request.original_id, revision=1)
        blob = await source.resolve(original_ref, listing, cutoff)
        assert blob.blob and "body" not in blob.payload
        http = FixtureHTTP()
        http.items[0].update(
            announcementId="67890",
            announcementTime=int((PUBLISHED + timedelta(days=1)).timestamp() * 1000),
            adjunctUrl="finalpage/2026-10-09/67890.html",
        )
        http.body, http.mime = (
            html(value="20,000.00", correction="<p>本公告更正原公告编号：12345</p>"),
            "text/html",
        )
        service = DisclosureService.for_tests(session, http)
        found = await service.execute(uuid4(), discovery(request.listing_id))
        fetched = await service.execute(
            uuid4(),
            FetchRequest(
                listing_id=request.listing_id,
                item_id=UUID(found["items"][0]["id"]),
                as_of=datetime.now(UTC),
            ),
        )
        await snapshot(session, request.listing_id, value=200000000)
        newer = fetched["original"]
        req = request.model_copy(
            update={"original_id": UUID(newer["id"]), "as_of": datetime.now(UTC)}
        )
        result = await save_candidates(
            session, newer, http.body, parse_original(http.body, "text/html"), req
        )
        assert result["facts"][0]["verification"] == "verified"
        assert result["facts"][0]["correction_refs"] == [fact.model_dump(mode="json")]
        for ref in (fact, original_ref):
            with pytest.raises(SourceValidityError) as error:
                await source.resolve(ref, listing, cutoff)
            assert error.value.code == "superseded_source"
