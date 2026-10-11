"""AG05 regression: immutable source identity/time/value and controlled queue boundaries."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from investment_agent.evidence.contracts import (
    CalculationAssumption,
    CalculationRecord,
    EvidenceRecord,
)
from investment_agent.evidence.queries import RefreshService, SnapshotQueryService
from investment_agent.evidence.snapshots import materialize
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import CreateCase, InvestorError, InvestorService
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PERIOD = date(2025, 12, 31)


async def seed(session):
    repo = ListingRepository(session)
    first = await repo.ensure_a_share_listing("600519.SH", company_name="贵州茅台")
    second = await repo.ensure_a_share_listing("000001.SZ", company_name="平安银行")
    row = await FinancialSnapshotRepository(session).insert(
        listing_id=first.id,
        report_period=PERIOD,
        period_kind="annual",
        fields={"net_profit": 123456789, "roe": 0.20},
        provider="baostock",
        captured_at=AS_OF - timedelta(days=20),
        availability="actual",
        available_from=date(2026, 4, 17),
    )
    return first, second, row


@pytest.mark.asyncio
async def test_persist_replay_and_source_validation(db_session):
    first, second, row = await seed(db_session)
    record = materialize(row, "net_profit_total")
    store = EvidenceService(db_session)
    key = uuid4()
    ref = await store.save_evidence("local_owner", record, key)
    assert ref.id != record.id
    assert ref == await store.save_evidence("local_owner", record, key)
    result = await store.get_evidence("local_owner", ref, datetime.now(UTC) + timedelta(seconds=1))
    assert result.data.value.value == Decimal("123456789")
    assert result.data.period.end == PERIOD
    assert result.data.source_ref.revision == row.version
    assert result.data.subject_ref.id == first.id
    assert result.data.actor == "snapshot_adapter"
    for update in (
        {"subject_ref": ObjectRef(kind="listing", id=second.id, revision=1)},
        {"period": record.period.model_copy(update={"end": date(2024, 12, 31)})},
        {"value": Quantity(value="9", unit="CNY", currency="CNY")},
        {"value": Quantity(value="123456789", unit="万元", currency="CNY")},
        {"conflict": "resolved"},
        {"available_from": AS_OF},
        {"source_ref": record.source_ref.model_copy(update={"revision": 99})},
    ):
        with pytest.raises(InvestorError):
            await store.save_evidence("local_owner", record.model_copy(update=update), uuid4())
    with pytest.raises(InvestorError, match="different request"):
        await store.save_evidence("local_owner", record.model_copy(update={"text": "altered"}), key)
    assert (
        await store.get_evidence("local_owner", ref, AS_OF, visible_at=AS_OF)
    ).status == "missing"
    assert (
        await store.get_evidence(
            "local_owner", ref.model_copy(update={"revision": 2}), datetime.now(UTC)
        )
    ).status == "missing"
    with pytest.raises(InvestorError, match="another subject"):
        await store.get_evidence(
            "local_owner",
            ref,
            datetime.now(UTC),
            subject_ref=ObjectRef(kind="listing", id=second.id, revision=1),
        )
    saved = await db_session.get(SavedEvidence, (str(ref.id), 1))
    with pytest.raises(ValueError, match="immutable"):
        async with db_session.begin_nested():
            saved.payload = {**saved.payload, "text": "overwrite"}
            await db_session.flush()


@pytest.mark.asyncio
async def test_pit_missing_conflict_stale_and_comparison(db_session):
    first, second, row = await seed(db_session)
    query = SnapshotQueryService(db_session)
    identity = await query.resolve_security("600519.SH", AS_OF)
    assert identity.data.company_name == "贵州茅台"
    assert (await query.resolve_security("000002.SZ", AS_OF)).status == "missing"
    assert (await query.get_snapshot("600519.SH", "net_profit_total", AS_OF)).status == "ok"
    unknown = await query.get_snapshot("600519.SH", "operating_cash_flow", AS_OF)
    assert unknown.status == "missing" and unknown.data[0].value.value is None
    assert unknown.data[0].value.unknown_reason
    assert (
        await query.get_snapshot("600519.SH", "roe", AS_OF, max_age=timedelta(days=1))
    ).status == "stale"
    repo = FinancialSnapshotRepository(db_session)
    await repo.insert(
        listing_id=first.id,
        report_period=PERIOD,
        period_kind="annual",
        fields={"roe": 0.9},
        provider="ifind",
        captured_at=AS_OF + timedelta(days=1),
        availability="actual",
        available_from=date(2026, 4, 17),
        is_backfill=True,
    )
    before = await query.get_snapshot("600519.SH", "roe", AS_OF)
    assert before.status == "ok" and len(before.data) == 1
    after = await query.get_snapshot("600519.SH", "roe", AS_OF + timedelta(days=2))
    assert after.status == "conflict" and len(after.data) == 2
    store = EvidenceService(db_session)
    selected = await store.save_evidence("local_owner", after.data[0], uuid4())
    cutoff = AS_OF + timedelta(days=2)
    conflicting = CalculationRecord(
        id=uuid4(),
        revision=1,
        created_at=cutoff,
        actor="ignored",
        origin_ref=None,
        method="future_AG10",
        method_version="1",
        fact_refs=(selected,),
        assumption_refs=(),
        input_hash="input",
        as_of=cutoff,
        applicable=True,
        status=CalculationStatus.OK,
        results={"selected": Quantity(value="0.2", unit="fraction")},
    )
    with pytest.raises(InvestorError, match="cross-source conflict"):
        await store.save_calculation("local_owner", conflicting, uuid4())
    await repo.insert(
        listing_id=second.id,
        report_period=date(2025, 6, 30),
        period_kind="semi",
        fields={"roe": 0.1},
        provider="baostock",
        captured_at=AS_OF,
        availability="approx",
        available_from=date(2025, 8, 31),
    )
    comparison = await query.compare_stocks(
        ("600519.SH", "000001.SZ"), "roe", AS_OF, report_period=PERIOD
    )
    assert comparison.status == "missing"  # never substitute another company's latest period
    approx = await query.get_snapshot("000001.SZ", "roe", AS_OF)
    assert "availability=approx" in approx.warnings[0]
    with pytest.raises(InvestorError, match="registered"):
        await query.get_snapshot("600519.SH", "fcfe", AS_OF)
    key = uuid4()
    captured = await query.capture_snapshot("local_owner", "600519.SH", "roe", AS_OF, key)
    assert captured == await query.capture_snapshot("local_owner", "600519.SH", "roe", AS_OF, key)
    with pytest.raises(InvestorError, match="different request"):
        await query.capture_snapshot("local_owner", "600519.SH", "net_profit_total", AS_OF, key)


def report(listing):
    ref = ObjectRef(kind="listing", id=listing.id, revision=1)
    return EvidenceRecord(
        id=uuid4(),
        revision=99,
        created_at=AS_OF,
        actor="model says verified",
        origin_ref=None,
        source_ref=ref,
        source_kind="user_report",
        locator="untrusted",
        content_hash="untrusted",
        subject_ref=ref,
        period=None,
        published_at=AS_OF,
        acquired_at=AS_OF,
        available_from=AS_OF,
        verification="user_report",
        conflict="none",
        text="用户提供尚未核验的材料",
    )


@pytest.mark.asyncio
async def test_user_capture_and_calculation_roles(db_session):
    first, _, _ = await seed(db_session)
    store = EvidenceService(db_session)
    candidate = report(first)
    before = datetime.now(UTC)
    ref = await store.save_evidence("local_owner", candidate, uuid4())
    saved = (await store.get_evidence("local_owner", ref, datetime.now(UTC))).data
    assert saved.verification == "user_report" and saved.actor == "user" and saved.revision == 1
    assert saved.acquired_at >= before and saved.available_from >= before
    assert saved.source_ref.kind == "evidence" and saved.source_ref.id == ref.id
    assert saved.source_ref.field_path == "text" and saved.content_hash != "untrusted"
    assert (await store.get_evidence("local_owner", ref, AS_OF)).status == "missing"
    only_value = candidate.model_copy(
        update={"text": None, "value": Quantity(value="3", unit="user_declared")}
    )
    ref2 = await store.save_evidence("local_owner", only_value, uuid4())
    saved2 = (await store.get_evidence("local_owner", ref2, datetime.now(UTC))).data
    assert saved2.source_ref.field_path == "value" and saved2.locator.endswith("value")
    now = datetime.now(UTC)
    calc = CalculationRecord(
        id=uuid4(),
        revision=1,
        created_at=now,
        actor="client",
        origin_ref=None,
        method="stub",
        method_version="1",
        fact_refs=(),
        assumption_refs=(ref,),
        input_hash="input",
        as_of=now,
        applicable=False,
        status=CalculationStatus.MISSING_INPUTS,
        results={},
        assumptions=(
            CalculationAssumption(
                name="growth",
                value=Quantity(value="0.02", unit="fraction"),
                provenance="method_default",
            ),
        ),
    )
    calc_ref = await store.save_calculation("local_owner", calc, uuid4())
    result = await store.get_calculation("local_owner", calc_ref, datetime.now(UTC))
    assert result.status == "missing" and result.data.status == CalculationStatus.MISSING_INPUTS
    assert result.data.assumptions[0].provenance == "method_default"
    with pytest.raises(InvestorError, match="facts"):
        await store.save_calculation(
            "local_owner", calc.model_copy(update={"fact_refs": (ref,)}), uuid4()
        )


@pytest.mark.asyncio
async def test_refresh_only_queues_authorized_whole_scope(db_session):
    first, _, _ = await seed(db_session)
    db_session.add(WatchlistItem(listing_id=first.id, enabled=True, added_at=datetime.now(UTC)))
    await db_session.flush()
    with pytest.raises(InvestorError, match="scope"):
        await RefreshService(db_session).request_data_refresh(
            "local_owner", "600519.SH", PERIOD, uuid4()
        )
    svc = RefreshService(db_session, authorized_scope="watchlist")
    key = uuid4()
    result = await svc.request_data_refresh("local_owner", "600519.SH", PERIOD, key)
    replay = await svc.request_data_refresh("local_owner", "600519.SH", PERIOD, key)
    assert result.status == "pending" and result.dependency_id == replay.dependency_id
    runs = list(await db_session.scalars(select(PipelineRun)))
    assert len(runs) == 1 and runs[0].kind == "market_snapshot" and runs[0].status == "queued"
    assert runs[0].request_json["scope"] == "watchlist"
    with pytest.raises(InvestorError, match="different"):
        await svc.request_data_refresh(
            "local_owner", "600519.SH", PERIOD, key, report_period=PERIOD
        )
    with pytest.raises(InvestorError, match="different"):
        await svc.request_data_refresh("local_owner", "600519.SH", date(2025, 12, 30), key)
    financial = await svc.request_data_refresh(
        "local_owner", "600519.SH", date(2026, 10, 1), uuid4(), report_period=PERIOD
    )
    assert financial.status == "pending"
    assert len(list(await db_session.scalars(select(FinancialSnapshot)))) == 1


@pytest.mark.asyncio
async def test_case_artifacts_require_same_subject(db_session):
    first, second, row = await seed(db_session)
    from investment_agent.investor.common import Mutation

    svc = InvestorService(db_session)
    case = await svc.create_case(
        "local_owner",
        CreateCase(
            listing_ref=ObjectRef(kind="listing", id=first.id, revision=1),
            question="Cash",
            idempotency_key=uuid4(),
        ),
    )
    evidence = EvidenceService(db_session)
    ref = await evidence.save_evidence("local_owner", materialize(row, "roe"), uuid4())
    episode = await svc.attach_artifacts(
        "local_owner", case.id, (ref,), Mutation(expected_revision=1, idempotency_key=uuid4())
    )
    assert episode.artifact_refs == (ref,)
    wrong = await evidence.save_evidence("local_owner", report(second), uuid4())
    with pytest.raises(InvestorError, match="in this case"):
        await svc.attach_artifacts(
            "local_owner", case.id, (wrong,), Mutation(expected_revision=2, idempotency_key=uuid4())
        )


@pytest.mark.asyncio
async def test_saved_events_are_traceable_forecasts_and_appointments(db_session):
    from investment_agent.storage.models.earnings_events import (
        DisclosureScheduleRecord,
        EarningsForecastRecord,
    )

    first, _, _ = await seed(db_session)
    forecast = EarningsForecastRecord(
        listing_id=first.id,
        report_period=PERIOD,
        notice_date=date(2026, 1, 20),
        forecast_type="预增",
        indicator="归母净利润",
        profit_low=Decimal("10000"),
        profit_high=None,
        yoy_low=None,
        yoy_high=None,
        content="预计增长",
        provider="akshare",
        captured_at=AS_OF,
    )
    schedule = DisclosureScheduleRecord(
        listing_id=first.id,
        report_period=PERIOD,
        first_date=date(2026, 4, 1),
        scheduled_date=date(2026, 4, 20),
        actual_date=None,
        provider="akshare",
        captured_at=AS_OF,
    )
    db_session.add_all([forecast, schedule])
    await db_session.flush()
    svc = SnapshotQueryService(db_session)
    source = ObjectRef(
        kind="earnings_forecast", id=forecast.id, revision=1, field_path="profit_low"
    )
    assert (await svc.get_event(source, AS_OF - timedelta(seconds=1))).status == "missing"
    result = await svc.get_event(source, AS_OF)
    assert result.data.value.value == Decimal("10000") and result.data.value.unit == "CNY"
    assert "forecast" in result.data.text and result.data.verification == "candidate"
    store = EvidenceService(db_session)
    saved = await store.save_evidence("local_owner", result.data, uuid4())
    back = (await store.get_evidence("local_owner", saved, datetime.now(UTC))).data
    assert back.source_ref == source and back.subject_ref.id == first.id
    assert (
        await svc.get_event(source.model_copy(update={"field_path": "profit_high"}), AS_OF)
    ).status == "missing"
    appointment = await svc.get_event(
        ObjectRef(kind="disclosure_schedule", id=schedule.id, revision=1), AS_OF
    )
    assert "appointment" in appointment.data.text and appointment.data.acquired_at == AS_OF
    with pytest.raises(InvestorError):
        await store.save_evidence(
            "local_owner", result.data.model_copy(update={"source_ref": back.subject_ref}), uuid4()
        )


@pytest.mark.asyncio
async def test_evidence_api_auth_saved_value_and_visibility(api_client):
    from investment_agent.storage.database import session_scope

    async with session_scope() as session:
        _, _, row = await seed(session)
        ref = await EvidenceService(session).save_evidence(
            "local_owner", materialize(row, "roe"), uuid4()
        )
    url = f"/investor/evidence/{ref.id}"
    assert api_client.get(url).status_code == 401
    response = api_client.get(url, headers={"X-API-Key": "secret"})
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["value"]["value"] == "0.2" and body["source_ref"]["kind"] == "financial_snapshot"
    assert body["locator"] == "fields.roe" and body["content_hash"]
    assert (
        api_client.get(
            url, params={"visible_at": "2026-10-01T00:00:00"}, headers={"X-API-Key": "secret"}
        ).status_code
        == 422
    )
    assert (
        api_client.get(
            url,
            params={"as_of": AS_OF.isoformat(), "visible_at": AS_OF.isoformat()},
            headers={"X-API-Key": "secret"},
        ).status_code
        == 404
    )
    assert (
        api_client.get(
            url, params={"as_of": "2026-10-01T00:00:00"}, headers={"X-API-Key": "secret"}
        ).status_code
        == 422
    )
    assert (
        api_client.get(url, params={"revision": 2}, headers={"X-API-Key": "secret"}).status_code
        == 404
    )
    assert api_client.post(url, headers={"X-API-Key": "secret"}).status_code == 405


@pytest.mark.asyncio
async def test_mcp_database_refuses_evidence_writes(tmp_path):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.config import Settings
    from investment_agent.mcp_server.readonly import readonly_engine, readonly_sessions
    from investment_agent.mcp_server.server import TOOL_NAMES
    from investment_agent.storage.models.base import Base

    path = tmp_path / "readonly-evidence.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            first, _, _ = await seed(session)
            candidate = report(first)
            await session.commit()
    finally:
        await engine.dispose()
    ro = readonly_engine(Settings(api_key="inert", database_url=f"sqlite+aiosqlite:///{path}"))
    try:
        async with readonly_sessions(ro)() as session:
            with pytest.raises(OperationalError, match="readonly"):
                await EvidenceService(session).save_evidence("local_owner", candidate, uuid4())
    finally:
        await ro.dispose()
    assert "request_data_refresh" not in TOOL_NAMES and "save_evidence" not in TOOL_NAMES


@pytest.mark.asyncio
async def test_research_cutoff_and_object_visibility_are_separate(db_session):
    _, _, row = await seed(db_session)
    store = EvidenceService(db_session)
    ref = await store.save_evidence("local_owner", materialize(row, "roe"), uuid4())
    evidence = await store.get_evidence("local_owner", ref, AS_OF)
    assert (
        evidence.status == "ok"
    )  # acquired input predates cutoff despite later evidence materialization
    record = CalculationRecord(
        id=uuid4(),
        revision=1,
        created_at=AS_OF,
        actor="ignored",
        origin_ref=None,
        method="future_AG10",
        method_version="1",
        fact_refs=(ref,),
        assumption_refs=(),
        input_hash="opaque_method_hash",
        as_of=AS_OF,
        applicable=True,
        status=CalculationStatus.OK,
        results={"copied_input": Quantity(value="0.2", unit="fraction")},
    )
    calculation = await store.save_calculation("local_owner", record, uuid4())
    same_task = await store.get_calculation("local_owner", calculation, AS_OF)
    assert same_task.status == "ok" and same_task.data.created_at > AS_OF
    assert (
        await store.get_calculation("local_owner", calculation, AS_OF, visible_at=AS_OF)
    ).status == "missing"
    assert (
        await store.get_calculation("local_owner", calculation, AS_OF - timedelta(seconds=1))
    ).status == "missing"
    with pytest.raises(InvestorError, match="not visible"):
        await store.save_calculation(
            "local_owner",
            record.model_copy(
                update={"as_of": row.captured_at.replace(tzinfo=UTC) - timedelta(seconds=1)}
            ),
            uuid4(),
        )
