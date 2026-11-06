"""Design §10.3: only the review path reads the external review lead."""

from __future__ import annotations

from investment_agent.agent.research import assemble
from investment_agent.review_panel.agent_hint import (
    REVIEW_LEAD_INSTRUCTION,
    REVIEW_LEAD_SECTION_TITLE,
)
from investment_agent.storage.database import session_scope
from tests.support.conversations import (
    base,
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import conversation_db as conversation_db
from tests.support.publication import fixture
from tests.support.review_panel import add_completed_round, divergent_opinions

CONTEXT_SEPARATOR = "\nTrusted current context:\n"


async def _assembled(accepted, settings):
    owned = await claim(accepted.task)
    async with session_scope() as session:
        rendered, _, _ = await assemble(session, owned, settings, {})
    return rendered


async def _submit(kind: str, subject, *, enabled: bool):  # noqa: ANN001
    conversation = await new_conversation()
    settings = base(review_panel_enabled=enabled)
    accepted = await submit(
        conversation,
        message(text="复核观点", context_refs=(subject,), research_kind=kind),
        settings=settings,
    )
    return await _assembled(accepted, settings)


async def test_review_path_appends_the_non_evidence_section(conversation_db) -> None:
    _, _, subject = await fixture()
    async with session_scope() as session:
        await add_completed_round(session, subject.id, divergent_opinions())

    rendered = await _submit("review", subject, enabled=True)

    head, _ = rendered.split(CONTEXT_SEPARATOR, 1)
    assert head  # instructions unchanged in front of the trusted context
    section_start = rendered.index(REVIEW_LEAD_SECTION_TITLE)
    assert section_start > rendered.index(CONTEXT_SEPARATOR)
    section = rendered[section_start:]
    assert REVIEW_LEAD_INSTRUCTION in section
    assert '"kind":"external_review_lead"' in section
    assert '"not_evidence":true' in section
    assert "估值水平" in section
    # The lead section carries no citable source refs.
    assert "base:" not in section
    assert "evidence:" not in section


async def test_non_review_paths_never_include_the_section(conversation_db) -> None:
    _, _, subject = await fixture()
    async with session_scope() as session:
        await add_completed_round(session, subject.id, divergent_opinions())

    for kind in ("company_research", "explanation", "local_check"):
        rendered = await _submit(kind, subject, enabled=True)
        assert REVIEW_LEAD_SECTION_TITLE not in rendered


async def test_review_path_without_the_feature_flag_or_round_has_no_section(
    conversation_db,
) -> None:
    _, _, subject = await fixture()
    async with session_scope() as session:
        await add_completed_round(session, subject.id, divergent_opinions())

    disabled = await _submit("review", subject, enabled=False)
    assert REVIEW_LEAD_SECTION_TITLE not in disabled

    from investment_agent.investor.common import ObjectRef
    from investment_agent.storage.repositories.listing import ListingRepository

    async with session_scope() as session:
        fresh = await ListingRepository(session).ensure_a_share_listing(
            "000001.SZ", company_name="平安银行"
        )
        fresh_subject = ObjectRef(kind="listing", id=fresh.id, revision=1)
    empty = await _submit("review", fresh_subject, enabled=True)
    assert REVIEW_LEAD_SECTION_TITLE not in empty
