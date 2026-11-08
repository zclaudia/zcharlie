"""P1-C4: rule store - template baseline sync / authoring and instance lifecycle."""

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


async def test_template_sync_imports_missing_ids_only(db_session) -> None:
    """CD5/CD6: a file only seeds an empty store; it never appends over one."""
    store = RuleStore(db_session, context=CONTEXT)
    first = await store.sync_templates([(None, _template())])
    assert [(o.template_id, o.version, o.action) for o in first] == [("quality_test", 1, "created")]
    again = await store.sync_templates([(None, _template())])
    assert [(o.version, o.action) for o in again] == [(1, "unchanged")]

    # A changed file does not become a version: the store wins, the drift is
    # surfaced through file_status (baseline_changed) instead.
    changed = template_data()
    changed["params"]["roe_min"] = 0.18
    skipped = await store.sync_templates([(None, parse_template(changed, context=CONTEXT))])
    assert [(o.version, o.action) for o in skipped] == [(1, "skipped")]

    records = await store.template_records("quality_test")
    assert [record.version for record in records] == [1]
    v1 = template_from_record(records[0])
    assert v1.params["roe_min"] == 0.15
    assert records[0].hash == canonical_hash(_template())
    assert "version" not in records[0].definition
    latest = await store.latest_templates()
    assert [(t.id, t.version, t.params["roe_min"]) for t in latest] == [("quality_test", 1, 0.15)]
    assert (await store.template_by_ref("quality_test@1")) == v1
    assert (await store.template_by_ref("quality_test")).version == 1  # type: ignore[union-attr]
    assert await store.template_by_ref("quality_test@9") is None


async def test_file_sync_never_overwrites_a_db_authored_version(db_session) -> None:
    """The DB authoring seat (create_version) wins: a later file sync - even
    with the old file content - never replaces or shadows the authored
    current version (CD6)."""
    store = RuleStore(db_session, context=CONTEXT)
    await store.sync_templates([(None, _template())])

    authored = template_data()
    authored["params"]["roe_min"] = 0.2
    record, action = await store.create_version(
        parse_template(authored, context=CONTEXT), actor="web", note="收紧 ROE 下限"
    )
    assert (record.version, action) == (2, "new_version")
    assert (record.created_by, record.note) == ("web", "收紧 ROE 下限")

    # The unchanged file (still v1 content) must not resurrect itself.
    stale = await store.sync_templates([(None, _template())])
    assert [(o.version, o.action) for o in stale] == [(2, "skipped")]
    # Nor may an edited file append a version.
    edited = template_data()
    edited["params"]["roe_min"] = 0.25
    also_skipped = await store.sync_templates([(None, parse_template(edited, context=CONTEXT))])
    assert [(o.version, o.action) for o in also_skipped] == [(2, "skipped")]

    records = await store.template_records("quality_test")
    assert [r.version for r in records] == [1, 2]
    current = await store.latest_template_record("quality_test")
    assert current is not None and current.version == 2  # type: ignore[union-attr]
    assert template_from_record(current).params["roe_min"] == 0.2  # type: ignore[arg-type]
    assert current.created_by == "web"  # type: ignore[union-attr]

    # The old version is untouched; re-authoring identical content is a no-op.
    unchanged, action = await store.create_version(
        parse_template(authored, context=CONTEXT), actor="api", note="重复提交"
    )
    assert (unchanged.version, action) == (2, "unchanged")
    count = len(list((await db_session.execute(select(RuleTemplateRecord))).scalars()))
    assert count == 2


async def test_activate_restores_an_old_version_as_the_next_one(db_session) -> None:
    """Activation is append-only rollback: the old content becomes max(version)."""
    store = RuleStore(db_session, context=CONTEXT)
    await store.sync_templates([(None, _template())])
    edited = template_data()
    edited["params"]["roe_min"] = 0.2
    await store.create_version(parse_template(edited, context=CONTEXT), actor="web", note="调整")

    activated, action = await store.activate_template(
        "quality_test", 1, actor="web", note="回滚到 v1"
    )
    assert (activated.version, action) == (3, "new_version")
    assert activated.hash == canonical_hash(_template())
    assert (activated.created_by, activated.note) == ("web", "回滚到 v1")
    latest = await store.latest_templates()
    assert latest[0].version == 3 and latest[0].params["roe_min"] == 0.15

    # Activating the current version is a no-op.
    same, action = await store.activate_template("quality_test", 3, actor="web", note="再激活")
    assert (same.version, action) == (3, "unchanged")
    with pytest.raises(RuleStoreError):
        await store.activate_template("quality_test", 9, actor="web", note="nope")


async def test_sync_rejects_invalid_template(db_session) -> None:
    bad = _template().model_copy(update={"applicable_groups": ["fintech"]})
    with pytest.raises(RuleValidationError) as info:
        await RuleStore(db_session, context=CONTEXT).sync_templates([(None, bad)])
    assert info.value.codes == {"unknown_industry_group"}
    # The authoring seat runs the same validation.
    with pytest.raises(RuleValidationError) as authored:
        await RuleStore(db_session, context=CONTEXT).create_version(bad, actor="web", note="x")
    assert authored.value.codes == {"unknown_industry_group"}


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


async def test_defaults_only_llm_draft_cannot_store_an_override(db_session) -> None:
    """Defense in depth for review P1-8: an LLM row marked ``defaults_only``
    may not carry numeric overrides even if a caller built one."""

    import json

    store = await _ready(db_session)
    rationale = json.dumps({"schema": "rule_draft.v2", "override_policy": "defaults_only"})
    with pytest.raises(RuleStoreError):
        await store.create_draft(
            RuleInstance.model_validate(
                instance_data(
                    params={"pe_pct_max": 0.25},
                    drafted_by="llm:stub-model",
                    draft_rationale=rationale,
                )
            )
        )
    # The same shape without the policy marker (legacy / manual rows) stores.
    legacy = await store.create_draft(
        RuleInstance.model_validate(
            instance_data(
                params={"pe_pct_max": 0.25},
                drafted_by="llm:legacy",
                draft_rationale="plain text rationale",
            )
        )
    )
    assert legacy.status == "draft"
    # A human edit of an LLM draft keeps the rationale but is a deliberate
    # action; it is not blocked by the draft-time policy.
    edited = await store.edit(
        legacy.id,
        RuleInstance.model_validate(
            instance_data(
                params={"pe_pct_max": 0.2},
                drafted_by="user",
                draft_rationale=rationale,
            )
        ),
    )
    assert edited.status == "draft"


async def test_defaults_only_draft_without_overrides_stores(db_session) -> None:
    import json

    store = await _ready(db_session)
    rationale = json.dumps({"override_policy": "defaults_only"})
    draft = await store.create_draft(
        RuleInstance.model_validate(
            instance_data(
                params={}, drafted_by="llm:stub-model", draft_rationale=rationale
            )
        )
    )
    assert draft.status == "draft" and instance_from_record(draft).params == {}
    # The human approve gate is unchanged: a stored draft is still not active.
    assert await store.active_instance(draft.listing_id, "candidate") is None
    await store.approve(draft.id, approved_by="user", now=T0)
    assert await store.active_instance(draft.listing_id, "candidate") is not None


async def test_existing_active_instance_is_never_rewritten(db_session) -> None:
    """The policy applies to new LLM drafts; stored active rows keep their
    definition (no retroactive normalization of a previously approved rule)."""

    import json

    store = await _ready(db_session)
    legacy = await store.create_draft(
        RuleInstance.model_validate(
            instance_data(
                params={"pe_pct_max": 0.25},
                drafted_by="llm:legacy",
                draft_rationale=json.dumps({"schema": "rule_draft.v1"}),
            )
        )
    )
    active = await store.approve(legacy.id, approved_by="user", now=T0)
    before = dict(active.definition)

    # A new defaults-only draft cannot store an override, and approving it
    # (the normal lifecycle) leaves the old row's own definition untouched.
    newer = await store.create_draft(
        RuleInstance.model_validate(
            instance_data(
                params={},
                drafted_by="llm:stub-model",
                draft_rationale=json.dumps({"override_policy": "defaults_only"}),
            )
        ),
        now=T0,
    )
    await store.approve(newer.id, approved_by="user", now=T0)
    retired = await store.get_instance(legacy.id)
    assert retired is not None
    assert (retired.status, retired.definition) == ("retired", before)
    assert instance_from_record(retired).params == {"pe_pct_max": 0.25}
