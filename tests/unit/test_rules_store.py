"""P1-C4: rule store - template sync/versioning and instance lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from investment_agent.rules.schema import RuleInstance, canonical_hash
from investment_agent.rules.store import (
    RuleStore,
    RuleStoreError,
    instance_from_record,
    template_from_record,
)
from investment_agent.rules.validation import RuleValidationError, parse_template
from investment_agent.storage.models.rules import RuleTemplateRecord
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._rules_support import CONTEXT, instance_data, template_data

T0 = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _template(**overrides: object):
    return parse_template(template_data(**overrides), context=CONTEXT)


async def test_template_sync_is_idempotent_and_versions_by_hash(db_session) -> None:
    store = RuleStore(db_session, context=CONTEXT)
    first = await store.sync_templates([(None, _template())])
    assert [(o.template_id, o.version, o.action) for o in first] == [("quality_test", 1, "created")]
    again = await store.sync_templates([(None, _template())])
    assert [(o.version, o.action) for o in again] == [(1, "unchanged")]

    changed = template_data()
    changed["params"]["roe_min"] = 0.18
    outcome = await store.sync_templates([(None, parse_template(changed, context=CONTEXT))])
    assert [(o.version, o.action) for o in outcome] == [(2, "new_version")]

    records = await store.template_records("quality_test")
    assert [record.version for record in records] == [1, 2]
    # v1 is untouched; each version keeps its own content and hash.
    v1 = template_from_record(records[0])
    assert v1.params["roe_min"] == 0.15
    assert records[0].hash == canonical_hash(_template())
    assert "version" not in records[0].definition
    latest = await store.latest_templates()
    assert [(t.id, t.version, t.params["roe_min"]) for t in latest] == [("quality_test", 2, 0.18)]
    assert (await store.template_by_ref("quality_test@1")) == v1
    assert (await store.template_by_ref("quality_test")).version == 2  # type: ignore[union-attr]
    assert await store.template_by_ref("quality_test@9") is None

    # Reverting the file content is a new version too (history is append-only).
    reverted = await store.sync_templates([(None, _template())])
    assert [(o.version, o.action) for o in reverted] == [(3, "new_version")]
    count = len(list((await db_session.execute(select(RuleTemplateRecord))).scalars()))
    assert count == 3


async def test_sync_rejects_invalid_template(db_session) -> None:
    bad = _template().model_copy(update={"applicable_groups": ["fintech"]})
    with pytest.raises(RuleValidationError) as info:
        await RuleStore(db_session, context=CONTEXT).sync_templates([(None, bad)])
    assert info.value.codes == {"unknown_industry_group"}


async def _ready(db_session) -> RuleStore:
    await ListingRepository(db_session).ensure_a_share_listing("600519.SH", company_name="茅台")
    store = RuleStore(db_session, context=CONTEXT)
    await store.sync_templates([(None, _template())])
    return store


async def test_instance_lifecycle_draft_active_retired(db_session) -> None:
    store = await _ready(db_session)
    instance = RuleInstance.model_validate(instance_data(status="active", approved_by="x"))
    draft = await store.create_draft(instance, now=T0)
    assert (draft.version, draft.status, draft.approved_by) == (1, "draft", None)
    assert draft.template_id == "quality_test" and draft.template_version == 1
    assert draft.hash == canonical_hash(instance)

    active = await store.approve(draft.id, approved_by="user", now=T0)
    assert (active.status, active.approved_by) == ("active", "user")
    with pytest.raises(RuleStoreError):
        await store.approve(draft.id, approved_by="user")

    # Editing an active rule creates a new draft version; v1 stays active.
    edited = await store.edit(
        draft.id, RuleInstance.model_validate(instance_data(params={"pe_pct_max": 0.25})), now=T0
    )
    assert (edited.version, edited.status) == (2, "draft")
    assert (await store.active_instance(draft.listing_id, "candidate")).id == draft.id  # type: ignore[union-attr]

    # A second edit supersedes the pending draft.
    edited_again = await store.edit(
        edited.id, RuleInstance.model_validate(instance_data(params={"pe_pct_max": 0.2})), now=T0
    )
    assert edited_again.version == 3
    assert (await store.get_instance(edited.id)).status == "retired"  # type: ignore[union-attr]

    # Approving v3 retires the active v1.
    await store.approve(edited_again.id, approved_by="user", now=T0)
    statuses = {r.version: r.status for r in await store.instances(symbol="600519.sh")}
    assert statuses == {1: "retired", 2: "retired", 3: "active"}
    assert (await store.get_instance(draft.id)).retired_at is not None  # type: ignore[union-attr]
    restored = instance_from_record(edited_again)
    assert restored.params == {"pe_pct_max": 0.2}
    assert (restored.version, restored.status) == (3, "active")

    with pytest.raises(RuleStoreError):
        await store.edit(edited.id, RuleInstance.model_validate(instance_data()))
    with pytest.raises(RuleStoreError):
        await store.edit(
            edited_again.id,
            RuleInstance.model_validate(instance_data(scope="holding", zones={})),
        )
    retired = await store.retire(edited_again.id, now=T0)
    assert retired.status == "retired"
    assert await store.active_instance(draft.listing_id, "candidate") is None


async def test_instance_draft_is_validated_against_its_template_version(db_session) -> None:
    store = await _ready(db_session)
    with pytest.raises(RuleValidationError) as info:
        await store.create_draft(
            RuleInstance.model_validate(instance_data(params={"pe_pct_max": 0.95}))
        )
    assert info.value.codes == {"param_out_of_range"}
    with pytest.raises(RuleStoreError):
        await store.create_draft(
            RuleInstance.model_validate(instance_data(template_ref="quality_test@4"))
        )
    with pytest.raises(RuleStoreError):
        await store.create_draft(RuleInstance.model_validate(instance_data(symbol="000001.SZ")))
    # Scopes version independently.
    holding = await store.create_draft(
        RuleInstance.model_validate(instance_data(scope="holding", params={}, zones={}))
    )
    assert holding.version == 1
