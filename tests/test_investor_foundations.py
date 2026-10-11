"""AG04 state, reference, concurrency and authentication acceptance."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from investment_agent.investor.common import Mutation, ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization, ResearchPlan
from investment_agent.investor.foundations import CreateCase, InvestorError, InvestorService
from investment_agent.storage.models.investor import MandateRevision
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.user_domain import WatchlistItem


def mandate() -> Mandate:
    unknown = Quantity(value=None, unit="CNY", unknown_reason="budget not provided")
    return Mandate(
        id=uuid4(),
        revision=1,
        created_at=datetime.now(UTC),
        actor="untrusted_client",
        origin_ref=None,
        goals=(),
        scope_refs=(),
        hard_constraints=(),
        unknowns=("investment horizon",),
        authorization=ResearchAuthorization(
            daily_budget=unknown,
            per_task_budget=unknown,
            max_leads_per_batch=0,
            max_new_tasks_per_batch=0,
            source_refs=(),
        ),
    )


async def listing(session):
    company = Company(canonical_name="Test company")
    session.add(company)
    await session.flush()
    row = Listing(company_id=company.id, market_code="NASDAQ", ticker="TEST", is_primary=True)
    session.add(row)
    await session.flush()
    return row


@pytest.mark.asyncio
async def test_mandate_revisions_replay_conflict_and_immutable(db_session):
    svc = InvestorService(db_session)
    candidate = mandate()
    key = uuid4()
    saved = await svc.create_mandate("local_owner", candidate, key)
    assert saved.id != candidate.id and saved.actor == "user"
    assert await svc.create_mandate("local_owner", candidate, key) == saved
    changed = saved.model_copy(update={"revision": 2, "goals": ("preserve capital",)})
    mutation = Mutation(expected_revision=1, idempotency_key=uuid4())
    revised = await svc.revise_mandate("local_owner", changed, mutation)
    assert await svc.revise_mandate("local_owner", changed, mutation) == revised
    assert await svc.get_mandate("local_owner", 1) == saved
    assert (await svc.get_mandate("local_owner")).authorization.daily_budget.value is None
    with pytest.raises(InvestorError, match="revision changed"):
        await svc.revise_mandate(
            "local_owner", changed, mutation.model_copy(update={"idempotency_key": uuid4()})
        )
    with pytest.raises(InvestorError, match="different request"):
        await svc.create_mandate(
            "local_owner", candidate.model_copy(update={"goals": ("changed",)}), key
        )
    row = await db_session.scalar(select(MandateRevision).where(MandateRevision.revision == 1))
    assert row is not None
    with pytest.raises(ValueError, match="immutable"):
        async with db_session.begin_nested():
            row.payload = revised.model_dump(mode="json")
            await db_session.flush()


@pytest.mark.asyncio
async def test_case_unique_source_pin_plan_and_atomic_conflict(db_session, seeded_market):
    row = await listing(db_session)
    watch = WatchlistItem(listing_id=row.id, added_at=datetime.now(UTC))
    db_session.add(watch)
    await db_session.flush()
    svc = InvestorService(db_session)
    body = CreateCase(
        listing_ref=ObjectRef(kind="listing", id=row.id, revision=1),
        question="Why is cash flow stable?",
        idempotency_key=uuid4(),
        source_refs=(ObjectRef(kind="watchlist_item", id=watch.id, revision=1),),
    )
    case = await svc.create_case("local_owner", body)
    assert await svc.create_case("local_owner", body) == case
    assert (
        await svc.create_case(
            "local_owner", body.model_copy(update={"idempotency_key": uuid4(), "pinned": True})
        )
        == case
    )
    detail = await svc.detail("local_owner", case.id)
    assert len(detail.episodes) == 1 and detail.source_refs == body.source_refs
    assert case.current_thesis_ref is None and case.current_recommendation_ref is None
    pinned = await svc.set_pinned(
        "local_owner", case.id, True, Mutation(expected_revision=1, idempotency_key=uuid4())
    )
    episode = detail.episodes[0]
    budget = await svc.create_mandate("local_owner", mandate(), uuid4())
    plan = ResearchPlan(
        id=uuid4(),
        revision=1,
        created_at=datetime.now(UTC),
        actor="client",
        origin_ref=None,
        episode_ref=ObjectRef(kind="episode", id=episode.id, revision=1),
        question="Check cash",
        completion_criteria=("cash reconciliation",),
        hypothesis_refs=(),
        evidence_steps=("read report",),
        stop_conditions=("missing report",),
        budget_ref=ObjectRef(kind="mandate", id=budget.id, revision=1),
    )
    saved = await svc.save_plan(
        "local_owner",
        case.id,
        plan,
        Mutation(expected_revision=pinned.revision, idempotency_key=uuid4()),
    )
    assert saved.actor == "user"
    with pytest.raises(InvestorError, match="revision changed"):
        await svc.set_pinned(
            "local_owner", case.id, False, Mutation(expected_revision=1, idempotency_key=uuid4())
        )
    detail = await svc.detail("local_owner", case.id)
    assert detail.case.pinned and len(detail.plans) == 1 and len(detail.episodes) == 2
    with pytest.raises(InvestorError, match="real listing"):
        await svc.create_case(
            "local_owner",
            body.model_copy(
                update={
                    "listing_ref": ObjectRef(kind="listing", id=99999, revision=1),
                    "idempotency_key": uuid4(),
                }
            ),
        )


@pytest.mark.asyncio
async def test_api_auth_contract_replay_and_conflict(api_client):
    headers = {"X-API-Key": "secret"}
    assert api_client.get("/investor/mandate").status_code == 401
    assert api_client.get("/investor/mandate", headers=headers).status_code == 404
    body = {"candidate": mandate().model_dump(mode="json"), "idempotency_key": str(uuid4())}
    response = api_client.post("/investor/mandate", json=body, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["actor"] == "user"
    assert (
        api_client.post("/investor/mandate", json=body, headers=headers).json() == response.json()
    )
    body["idempotency_key"] = str(uuid4())
    assert api_client.post("/investor/mandate", json=body, headers=headers).status_code == 409
    assert api_client.get("/investor/cases", headers=headers).json() == []
    assert api_client.get("/investor/cases?limit=0", headers=headers).status_code == 422


@pytest.mark.asyncio
async def test_independent_sessions_races_rollback_and_identity_map(tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.storage.database import configure_sqlite_engine
    from investment_agent.storage.models.base import Base
    from investment_agent.storage.models.reference import Market
    from investment_agent.storage.repositories.investor import InvestorRepository

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'races.db'}")
    configure_sqlite_engine(engine)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with sessions() as session:
            session.add(Market(code="NASDAQ", name="NASDAQ", timezone="UTC", currency="USD"))
            await session.flush()
            security = await listing(session)
            listing_id = security.id
            await session.commit()
        body = CreateCase(
            listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
            question="Research cash",
            idempotency_key=uuid4(),
        )
        barrier = asyncio.Barrier(2)
        original = InvestorRepository.cases

        async def blocked_cases(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            await barrier.wait()
            return result

        monkeypatch.setattr(InvestorRepository, "cases", blocked_cases)

        async def create():
            async with sessions() as session:
                try:
                    result = await InvestorService(session).create_case("local_owner", body)
                    await session.commit()
                    return result
                except InvestorError as exc:
                    await session.rollback()
                    return exc

        outcomes = await asyncio.gather(create(), create())
        assert sum(isinstance(item, InvestorError) for item in outcomes) == 1
        assert (
            next(item for item in outcomes if isinstance(item, InvestorError)).detail.code
            == "revision_conflict"
        )
        monkeypatch.setattr(InvestorRepository, "cases", original)
        async with sessions() as session:
            case = await InvestorService(session).create_case("local_owner", body)
            await session.commit()
        # Explicit outer rollback also rolls back a released service savepoint.
        async with sessions() as session:
            await InvestorService(session).set_pinned(
                "local_owner", case.id, True, Mutation(expected_revision=1, idempotency_key=uuid4())
            )
            await session.rollback()
        async with sessions() as left, sessions() as right:
            a, b = InvestorService(left), InvestorService(right)
            assert (await a.get_case("local_owner", case.id)).pinned is False
            assert (await b.get_case("local_owner", case.id)).revision == 1
            pinned = await a.set_pinned(
                "local_owner", case.id, True, Mutation(expected_revision=1, idempotency_key=uuid4())
            )
            await left.commit()
            # Right retains an ORM identity but refreshes before applying its mutation.
            with pytest.raises(InvestorError):
                await b.set_pinned(
                    "local_owner",
                    case.id,
                    False,
                    Mutation(expected_revision=1, idempotency_key=uuid4()),
                )
            await right.rollback()
            assert await b.get_case("local_owner", case.id) == pinned
        barrier = asyncio.Barrier(2)
        advance = InvestorRepository.advance_case

        async def blocked_advance(self, *args, **kwargs):
            await barrier.wait()
            return await advance(self, *args, **kwargs)

        monkeypatch.setattr(InvestorRepository, "advance_case", blocked_advance)

        async def pin_concurrently(value):
            async with sessions() as session:
                try:
                    result = await InvestorService(session).set_pinned(
                        "local_owner",
                        case.id,
                        value,
                        Mutation(expected_revision=2, idempotency_key=uuid4()),
                    )
                    await session.commit()
                    return result
                except InvestorError as exc:
                    await session.rollback()
                    return exc

        raced = await asyncio.gather(pin_concurrently(True), pin_concurrently(False))
        assert sum(isinstance(item, InvestorError) for item in raced) == 1
        assert next(item for item in raced if not isinstance(item, InvestorError)).revision == 3
        monkeypatch.setattr(InvestorRepository, "advance_case", advance)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_source_cas_and_status_filter_before_cursor_limit(db_session, seeded_market):
    from investment_agent.investor.enums import CaseStatus
    from investment_agent.storage.models.investor import InvestorCase

    svc = InvestorService(db_session)
    rows = []
    for n in range(3):
        company = Company(canonical_name=f"Company {n}")
        db_session.add(company)
        await db_session.flush()
        security = Listing(
            company_id=company.id, market_code="NASDAQ", ticker=f"T{n}", is_primary=True
        )
        db_session.add(security)
        await db_session.flush()
        case = await svc.create_case(
            "local_owner",
            CreateCase(
                listing_ref=ObjectRef(kind="listing", id=security.id, revision=1),
                question="Research",
                idempotency_key=uuid4(),
            ),
        )
        row = await db_session.get(InvestorCase, str(case.id))
        rows.append(row)
    rows.sort(key=lambda row: row.id)
    # Seed later lifecycle states without exposing an AG14 action API.
    rows[-1].payload = {**rows[-1].payload, "status": CaseStatus.TRACKING.value}
    await db_session.flush()
    matching = await svc.repository.cases("local_owner", limit=1, status="tracking")
    assert [row.id for row in matching] == [rows[-1].id]
    assert (
        await svc.repository.cases("local_owner", after=UUID(rows[-1].id), status="tracking") == []
    )
    watch = WatchlistItem(listing_id=rows[0].listing_id, added_at=datetime.now(UTC))
    db_session.add(watch)
    await db_session.flush()
    ref = ObjectRef(kind="watchlist_item", id=watch.id, revision=1)
    mutation = Mutation(expected_revision=1, idempotency_key=uuid4())
    case_id = UUID(rows[0].id)
    result = await svc.link_source("local_owner", case_id, ref, mutation)
    assert result.revision == 2
    assert (await svc.get_case("local_owner", case_id, 1)).revision == 1
    assert (await svc.get_case("local_owner", case_id, 1)).pinned is False
    assert await svc.link_source("local_owner", case_id, ref, mutation) == result
    assert (await svc.detail("local_owner", case_id)).source_refs == (ref,)
    with pytest.raises(InvestorError):
        await svc.link_source(
            "local_owner", case_id, ref, Mutation(expected_revision=1, idempotency_key=uuid4())
        )


@pytest.mark.asyncio
async def test_investor_cookie_csrf(api_client):
    from investment_agent.web.session import SESSION_COOKIE_NAME, create_session_token

    token = create_session_token(api_key="secret", ttl_minutes=15)
    api_client.cookies.set(SESSION_COOKIE_NAME, token)
    assert api_client.get("/investor/cases").status_code == 200
    body = {"candidate": mandate().model_dump(mode="json"), "idempotency_key": str(uuid4())}
    assert (
        api_client.post(
            "/investor/mandate", json=body, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        api_client.post(
            "/investor/mandate", json=body, headers={"Origin": "http://testserver"}
        ).status_code
        == 201
    )
