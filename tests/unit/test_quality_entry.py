"""Synthetic local SQLite checks of G0's material and authority boundary."""

import json
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.api.schemas.research_entry import ResearchEntryInput
from investment_agent.config import Settings
from investment_agent.evidence.snapshots import materialize
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import MethodBinding
from investment_agent.research_quality.entry import ResearchEntryService
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import AgentTask, BudgetEntry, ExecutionGrantRow
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.investor import MethodDefaultHead, TaskMethodBinding
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market

AT = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
REF = ObjectRef(kind="listing", id=18, revision=1)


def request(**updates):
    return ResearchEntryInput.model_validate(
        {
            "listing_ref": REF,
            "template_key": "company_research",
            "question": "商业驱动与反证？",
            **updates,
        }
    )


def settings(tmp_path, **updates):
    return Settings(
        _env_file=None, api_key="inert", data_dir=tmp_path, investor_agent_enabled=True, **updates
    )


@pytest.fixture
async def local_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'entry.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Market(code="SSE", name="synthetic", timezone="Asia/Shanghai", currency="CNY"),
                Company(id=18, canonical_name="合成公司"),
                Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


async def add_snapshot(session, **updates):
    values = dict(
        listing_id=18,
        report_period=date(2025, 12, 31),
        period_kind="annual",
        fields={"revenue_main": 1000, "net_profit_parent": 50, "operating_cash_flow": 40},
        provider="akshare",
        captured_at=AT - timedelta(days=1),
        availability="actual",
        available_from=date(2026, 3, 1),
        version=1,
        is_backfill=False,
    )
    values.update(updates)
    row = FinancialSnapshot(**values)
    session.add(row)
    await session.flush()
    return row


@pytest.mark.parametrize(
    "updates",
    [
        {"owner": "local_owner"},
        {"template_key": "unknown"},
        {"question": "  "},
        {"listing_ref": {"kind": "listing", "id": True, "revision": 1}},
        {"listing_ref": {"kind": "listing", "id": 18, "revision": 2}},
        {"listing_ref": {"kind": "listing", "id": 18, "revision": 1, "field_path": "name"}},
    ],
)
def test_request_rejects_authority_or_ambiguous_identity(updates):
    with pytest.raises(ValidationError):
        request(**updates)


async def test_preflight_reads_candidates_and_default_method_without_writing(
    local_session, tmp_path
):
    session = local_session
    await add_snapshot(session)
    method = MethodBinding(method_ref=ObjectRef(kind="method", id=uuid4(), revision=1))
    session.add(
        MethodDefaultHead(
            owner_id="local_owner",
            channel="company-research",
            revision=2,
            payload=method.model_dump(mode="json"),
        )
    )
    await session.commit()
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(session.bind.sync_engine, "before_cursor_execute", record)
    try:
        response = await ResearchEntryService(session, settings(tmp_path)).preflight(
            request(), now=AT
        )
    finally:
        event.remove(session.bind.sync_engine, "before_cursor_execute", record)
    assert response.coverage == "partial"
    assert response.calculability == "inputs_visible"
    assert (
        response.method.method_ref == method.method_ref
    )  # default fallback, no candidate required
    assert response.preflight_at == AT
    assert all(item.verification == "candidate" for item in response.materials)
    assert "verified_evidence_missing" in {gap.code for gap in response.gaps}
    assert response.model.availability == "unavailable"
    assert not any(
        sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements
    )
    for model in (AgentTask, BudgetEntry, ExecutionGrantRow, TaskMethodBinding):
        assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_preflight_obeys_acquisition_publication_and_owner_boundaries(
    local_session, tmp_path
):
    session = local_session
    eligible = await add_snapshot(session)
    # Newer, backfilled source is unavailable at this instant; no day-end shortcut.
    await add_snapshot(
        session,
        captured_at=AT + timedelta(seconds=1),
        version=2,
        is_backfill=True,
        fields={"revenue_main": 99999},
    )
    await add_snapshot(
        session,
        captured_at=AT - timedelta(hours=1),
        version=3,
        available_from=date(2026, 11, 8),
        fields={"revenue_main": 88888},
    )
    record = materialize(eligible, "revenue_main")
    for owner, subject, time in [
        ("foreign", REF, AT - timedelta(days=1)),
        ("local_owner", ObjectRef(kind="listing", id=19, revision=1), AT - timedelta(days=1)),
        ("local_owner", REF, AT + timedelta(seconds=1)),
    ]:
        saved = record.model_copy(
            update={
                "id": uuid4(),
                "verification": "verified",
                "subject_ref": subject,
                "created_at": time,
                "acquired_at": time,
            }
        )
        session.add(
            SavedEvidence(
                id=str(saved.id), revision=1, owner_id=owner, payload=saved.model_dump(mode="json")
            )
        )
    await session.commit()
    response = await ResearchEntryService(session, settings(tmp_path)).preflight(request(), now=AT)
    assert {item.ref.id for item in response.materials} == {eligible.id}
    assert "verified_evidence_missing" in {gap.code for gap in response.gaps}


async def test_visible_verified_evidence_is_separate_from_snapshot_candidates(
    local_session, tmp_path
):
    row = await add_snapshot(local_session)
    record = materialize(row, "revenue_main").model_copy(
        update={"id": uuid4(), "verification": "verified"}
    )
    local_session.add(
        SavedEvidence(
            id=str(record.id),
            revision=1,
            owner_id="local_owner",
            payload=record.model_dump(mode="json"),
        )
    )
    await local_session.commit()
    response = await ResearchEntryService(local_session, settings(tmp_path)).preflight(
        request(), now=AT
    )
    assert len([item for item in response.materials if item.verification == "verified"]) == 1
    assert "verified_evidence_missing" not in {gap.code for gap in response.gaps}
    assert response.coverage == "partial"


async def test_disabled_and_missing_material_paths_are_explicit(local_session, tmp_path):
    base = settings(tmp_path).model_copy(update={"investor_agent_enabled": False})
    with pytest.raises(InvestorError) as caught:
        await ResearchEntryService(local_session, base).preflight(request(), now=AT)
    assert caught.value.detail.code == "feature_disabled"
    result = await ResearchEntryService(local_session, settings(tmp_path)).preflight(
        request(), now=AT
    )
    assert result.coverage == "missing"
    assert result.calculability == "missing_inputs"
    assert result.material_refs == ()


async def test_model_summary_uses_current_slot_and_never_exposes_config(local_session, tmp_path):
    catalog = {
        "providers": [
            {
                "id": "synthetic",
                "name": "Synthetic",
                "base_url": "https://secret-endpoint.invalid/v1",
                "api_key": "inert-sensitive-value",
                "models": [
                    {
                        "id": "main",
                        "name": "Main",
                        "model": "synthetic-model",
                        "capabilities": ["tools"],
                    }
                ],
            }
        ]
    }
    base = settings(
        tmp_path,
        llm_catalog=json.dumps(catalog),
        model_slots=json.dumps({"investor": {"main": "synthetic/main"}}),
    )
    result = await ResearchEntryService(local_session, base).preflight(request(), now=AT)
    assert result.model.availability == "available"
    assert result.model.model == "synthetic-model"
    serialized = result.model_dump_json()
    for forbidden in (
        "secret-endpoint",
        "inert-sensitive-value",
        "base_url",
        "credential",
        "values_json",
    ):
        assert forbidden not in serialized
