"""SV-01 trusted grant owner reaches the real final document save boundary."""

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from tests.support.execution import execution_db as execution_db
from tests.support.originals import original, pdf
from tests.test_document_native import setup

from investment_agent.agent import document_tools
from investment_agent.agent.tools import TrustedTools
from investment_agent.evidence.disclosure_http import DisclosureError
from investment_agent.evidence.documents import OriginalRequest, readback, save_candidates, stage
from investment_agent.evidence.original_parser import extract
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ToolAction
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.research_events import ResearchDomainEvent


async def test_legacy_default_and_explicit_owner_keep_same_saved_refs(db_session):
    payload, body, request = await original(db_session)
    parsed = extract(body, payload["mime"])
    first = await save_candidates(db_session, payload, body, parsed, request)
    second = await save_candidates(db_session, payload, body, parsed, request, owner="local_owner")
    assert first["parse_artifact_ref"] == second["parse_artifact_ref"]
    assert first["fact_refs"] == second["fact_refs"]
    assert {row.owner_id for row in await db_session.scalars(select(SavedEvidence))} == {"local_owner"}
    assert await db_session.scalar(select(func.count()).select_from(ResearchDomainEvent)) == 0


async def test_save_refreshes_original_owner_before_any_new_evidence(db_session):
    payload, body, request = await original(db_session)
    await stage(db_session, request)
    before = await db_session.scalar(select(func.count()).select_from(SavedEvidence))
    await db_session.execute(update(DisclosureOriginal).where(
        DisclosureOriginal.id == str(request.original_id),
    ).values(owner_id="other_owner").execution_options(synchronize_session=False))
    with pytest.raises(DisclosureError, match="original_not_found"):
        await save_candidates(db_session, payload, body, extract(body, payload["mime"]), request)
    assert await db_session.scalar(select(func.count()).select_from(SavedEvidence)) == before


@pytest.mark.parametrize("kind", ["manifest", "fact", "page"])
async def test_preexisting_foreign_evidence_is_never_reused_or_returned(db_session, kind):
    if kind == "page":
        payload, body, request = await original(db_session, body=pdf(("Security code: 000001",)), mime="application/pdf")
        parsed = extract(body, payload["mime"], page_start=1, page_count=1)
    else:
        payload, body, request = await original(db_session)
        parsed = extract(body, payload["mime"])
    first = await save_candidates(db_session, payload, body, parsed, request)
    ref = first["parse_artifact_ref"] if kind == "manifest" else first["fact_refs" if kind == "fact" else "text_refs"][0]
    await db_session.get(SavedEvidence, (ref["id"], 1))
    await db_session.execute(update(SavedEvidence).where(
        SavedEvidence.id == ref["id"], SavedEvidence.revision == 1,
    ).values(owner_id="other_owner").execution_options(synchronize_session=False))
    with pytest.raises(DisclosureError, match="original_scope_mismatch"):
        await save_candidates(db_session, payload, body, parsed, request)


async def test_model_cannot_supply_owner_and_readback_never_saves(db_session):
    payload, body, request = await original(db_session)
    for key in ("owner", "owner_id"):
        with pytest.raises(ValidationError):
            OriginalRequest.model_validate({**request.model_dump(), key: "local_owner"})
    before = await db_session.scalar(select(func.count()).select_from(SavedEvidence))
    assert readback(payload, extract(body, payload["mime"]), request)["status"] == "ok"
    assert await db_session.scalar(select(func.count()).select_from(SavedEvidence)) == before
    assert await db_session.scalar(select(func.count()).select_from(ResearchDomainEvent)) == 0


@pytest.mark.usefixtures("execution_db")
async def test_native_final_save_receives_live_permission_owner(monkeypatch):
    _, task, context, request = await setup()
    owners = []
    original_save = document_tools.save_candidates

    async def local_parse(body, mime, **window):
        return extract(body, mime, **window)

    async def observed_save(*args, **kwargs):
        assert "owner" in kwargs  # An implicit local_owner default is insufficient.
        owners.append(kwargs["owner"])
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(document_tools, "parse_staged", local_parse)
    monkeypatch.setattr(document_tools, "save_candidates", observed_save)
    tools = TrustedTools(context, document_tools.registrations())
    saved = await tools.call(tools.registrations["parse_disclosure_original"], request)
    assert owners == ["local_owner"] and saved["facts"][0]["verification"] == "verified"
    async with session_scope() as session:
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        assert action is not None and action.state == "committed" and action.result == saved
        assert await session.scalar(select(func.count()).select_from(ResearchDomainEvent)) == 0


@pytest.mark.usefixtures("execution_db")
async def test_owner_change_during_parse_blocks_final_save(monkeypatch):
    _, task, context, request = await setup()
    calls: list[Any] = []

    async def changing_parse(body, mime, **window):
        parsed = extract(body, mime, **window)
        async with session_scope() as session:
            await session.execute(update(DisclosureOriginal).where(
                DisclosureOriginal.id == str(request.original_id),
            ).values(owner_id="other_owner"))
        return parsed

    async def must_not_save(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("save reached after original ownership changed")

    monkeypatch.setattr(document_tools, "parse_staged", changing_parse)
    monkeypatch.setattr(document_tools, "save_candidates", must_not_save)
    tools = TrustedTools(context, document_tools.registrations())
    with pytest.raises(DisclosureError, match="original_not_found"):
        await tools.call(tools.registrations["parse_disclosure_original"], request)
    assert not calls
    async with session_scope() as session:
        action = await session.scalar(select(ToolAction).where(ToolAction.task_id == str(task.id)))
        assert action is not None and action.result is None
        assert await session.scalar(select(func.count()).select_from(SavedEvidence)) == 1
        original_row = await session.get(DisclosureOriginal, str(UUID(str(request.original_id))))
        assert original_row is not None and original_row.owner_id == "other_owner"
