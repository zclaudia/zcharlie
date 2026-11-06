"""Config-document endpoints (config-docs-plan §4.5).

* ``GET /config-docs`` - per kind revision, edit metadata and baseline drift;
* ``GET /config-docs/{kind}`` - the effective raw document plus the baseline
  file's raw document (the UI diffs them client-side) and edit metadata;
* ``PATCH /config-docs/{kind}`` - ``{value, note, expected_revision}``: the
  kind's own parser validates the whole document (CD3), ``422`` carries the
  parser's message, ``409`` is a stale ``expected_revision``;
* ``GET /config-docs/{kind}/history`` - full-document change rows, newest first;
* ``POST /config-docs/{kind}/baseline-import`` - the explicit re-import of the
  baseline file as one audited write;
* ``POST /config-docs/industry-groups/coverage-check`` - the shared coverage
  check of the factor tests (WP3, config-docs-plan §4.5 / §6) over the current
  CSI 300 universe; read-only, optionally over a posted draft document.

Auth: ``X-API-Key`` or the web session; cookie writes need the same-origin
proof of :func:`verify_api_key_or_session`. Actor attribution mirrors the
settings PATCH (``api`` for a key, ``web`` for a session); every write carries
the mandatory note (CD7). Reads before the one-time import fall back to the
baseline file, so the page works on a fresh or unmigrated database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.config_docs import (
    ConfigDocBaselineImportRequest,
    ConfigDocChangeOut,
    ConfigDocCoverageCheckRequest,
    ConfigDocHistoryOut,
    ConfigDocListOut,
    ConfigDocOut,
    ConfigDocPatchRequest,
    ConfigDocSummaryOut,
    CoverageGapOut,
    IndustryGroupsCoverageOut,
)
from investment_agent.clock import shanghai_today
from investment_agent.config_docs import registry
from investment_agent.config_docs.registry import ConfigDocSpec
from investment_agent.config_docs.store import (
    ConfigDocConflictError,
    ConfigDocStore,
    ConfigDocValidationError,
)
from investment_agent.factors.industry_groups import check_industry_groups_coverage
from investment_agent.providers.shenwan_directory import default_shenwan_directory
from investment_agent.snapshots.shenwan_refresh import csi300_coverage_members
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.rules import RuleTemplateRecord

router = APIRouter(
    prefix="/config-docs",
    tags=["config-docs"],
    dependencies=[Depends(verify_api_key_or_session)],
)


def _spec(kind: str) -> ConfigDocSpec:
    """The spec of ``kind``; an unregistered kind is a 404, not a 500."""

    try:
        return registry.spec_for(kind)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"unknown config document kind {kind!r}"
        ) from None


def _as_utc(value: datetime) -> datetime:
    # Deliberately not domain.time.as_utc: keeps a stored aware value's offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _baseline_value(spec: ConfigDocSpec) -> dict[str, Any] | None:
    """The baseline file's raw document; ``None`` when unreadable or not a mapping."""

    try:
        return registry.baseline_document(spec)
    except Exception:
        return None


async def _detail(session: AsyncSession, kind: str) -> ConfigDocOut:
    """The GET / PATCH / baseline-import response body for one kind."""

    spec = _spec(kind)
    store = ConfigDocStore(session)
    value, row = await store.get(spec.kind)
    drifted = bool((await store.baseline_status())[spec.kind]["drifted"])
    return ConfigDocOut(
        kind=spec.kind,
        title=spec.title,
        value=value,
        baseline_value=_baseline_value(spec),
        revision=await store.revision(spec.kind),
        updated_at=_as_utc(row.updated_at) if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
        imported_at=_as_utc(row.imported_at) if row is not None and row.imported_at else None,
        drifted=drifted,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("", response_model=ConfigDocListOut)
async def list_config_docs() -> ConfigDocListOut:
    async with session_scope() as session:
        store = ConfigDocStore(session)
        status = await store.baseline_status()
        items: list[ConfigDocSummaryOut] = []
        for spec in registry.KINDS:
            entry = status[spec.kind]
            _value, row = await store.get(spec.kind)
            items.append(
                ConfigDocSummaryOut(
                    kind=spec.kind,
                    title=spec.title,
                    revision=await store.revision(spec.kind),
                    updated_at=_as_utc(row.updated_at) if row is not None else None,
                    updated_by=row.updated_by if row is not None else None,
                    imported=bool(entry["imported"]),
                    drifted=bool(entry["drifted"]),
                )
            )
        return ConfigDocListOut(items=items)


@router.get("/{kind}", response_model=ConfigDocOut)
async def get_config_doc(kind: str) -> ConfigDocOut:
    async with session_scope() as session:
        return await _detail(session, kind)


def _validation_failed(spec: ConfigDocSpec, exc: ConfigDocValidationError) -> HTTPException:
    """The 422 mirroring the settings PATCH: one issue keyed by the kind."""

    return HTTPException(
        status_code=422,
        detail={
            "message": "config document validation failed",
            "issues": [{"key": spec.kind, "messages": [str(exc)]}],
        },
    )


async def _guard_industry_group_removal(
    session: AsyncSession, store: ConfigDocStore, value: dict[str, Any]
) -> None:
    """WP3 rename guard: a group id a stored rule template references must stay.

    Group ids are referenced from two sides. The document's own mappings
    (``codes`` / ``legacy_name_rules`` / ``shenwan``) are rejected by
    ``parse_industry_groups`` itself (CD3). The ``applicable_groups`` of
    stored rule template versions is the other side: those rows are
    immutable, so no document write can rewrite them — a document that drops
    such an id is rejected here with the standard 422 issue shape before
    anything is written. Renaming an id therefore means: add the new id,
    reassign the mappings, draft template versions against it, then remove
    the old id.
    """

    current, _row = await store.get("industry_groups")
    current_groups = current.get("groups") if isinstance(current, dict) else None
    new_groups = value.get("groups")
    removed = set(current_groups or {}) - set(new_groups or {})
    if not removed:
        return
    referencing: dict[str, set[str]] = {}
    rows = (
        await session.execute(select(RuleTemplateRecord.id, RuleTemplateRecord.definition))
    ).all()
    for template_id, definition in rows:
        applicable = definition.get("applicable_groups") if definition else None
        if not isinstance(applicable, list):
            continue
        for group in applicable:
            if isinstance(group, str) and group in removed:
                referencing.setdefault(group, set()).add(str(template_id))
    if not referencing:
        return
    messages = [
        f"分组 {group} 被模板 {'、'.join(sorted(ids))} 的 applicable_groups 引用，"
        "不能移除（模板版本不可变；请先为相关模板起草引用新分组的新版本）。"
        for group, ids in sorted(referencing.items())
    ]
    raise HTTPException(
        status_code=422,
        detail={
            "message": "config document validation failed",
            "issues": [{"key": "industry_groups", "messages": messages}],
        },
    )


@router.patch("/{kind}", response_model=ConfigDocOut)
async def patch_config_doc(
    kind: str,
    body: ConfigDocPatchRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ConfigDocOut:
    spec = _spec(kind)
    actor = "api" if x_api_key is not None else "web"
    # The settings PATCH mechanism: trim the note, let the blank reach the store.
    note = body.note.strip() if body.note and body.note.strip() else ""
    try:
        async with session_scope() as session:
            store = ConfigDocStore(session)
            if spec.kind == "industry_groups":
                await _guard_industry_group_removal(session, store, body.value)
            await store.put(
                spec.kind,
                body.value,
                actor=actor,
                note=note,
                expected_revision=body.expected_revision,
            )
    except ConfigDocConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConfigDocValidationError as exc:
        raise _validation_failed(spec, exc) from exc
    async with session_scope() as session:
        return await _detail(session, spec.kind)


@router.get("/{kind}/history", response_model=ConfigDocHistoryOut)
async def config_doc_history(
    kind: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ConfigDocHistoryOut:
    spec = _spec(kind)
    async with session_scope() as session:
        rows = await ConfigDocStore(session).history(spec.kind, limit)
        return ConfigDocHistoryOut(
            items=[
                ConfigDocChangeOut(
                    id=row.id,
                    old_value=row.old_value_json,
                    new_value=row.new_value_json,
                    changed_at=_as_utc(row.changed_at),
                    actor=row.actor,
                    note=row.note,
                )
                for row in rows
            ]
        )


@router.post("/{kind}/baseline-import", response_model=ConfigDocOut)
async def import_config_doc_baseline(
    kind: str,
    body: ConfigDocBaselineImportRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ConfigDocOut:
    """Overwrite the stored document with the baseline file (the 从基线导入 action)."""

    spec = _spec(kind)
    if _baseline_value(spec) is None:
        raise HTTPException(status_code=422, detail="the baseline file is unreadable or invalid")
    actor = "api" if x_api_key is not None else "web"
    # The settings PATCH mechanism: trim the note, let the blank reach the store.
    note = body.note.strip() if body.note and body.note.strip() else ""
    try:
        async with session_scope() as session:
            store = ConfigDocStore(session)
            if spec.kind == "industry_groups":
                await _guard_industry_group_removal(
                    session, store, registry.baseline_document(spec)
                )
            await store.import_baseline(spec.kind, actor=actor, note=note, force=True)
    except ConfigDocValidationError as exc:
        raise _validation_failed(spec, exc) from exc
    async with session_scope() as session:
        return await _detail(session, spec.kind)


# --------------------------------------------------------------------------- #
# industry-groups coverage check (WP3, §4.5 / §6)
# --------------------------------------------------------------------------- #


@router.post("/industry-groups/coverage-check", response_model=IndustryGroupsCoverageOut)
async def industry_groups_coverage_check(
    body: ConfigDocCoverageCheckRequest | None = None,
) -> IndustryGroupsCoverageOut:
    """The coverage contract of the factor tests, over the live universe.

    Read-only: the current CSI 300 membership and both classification systems
    as of today (Asia/Shanghai), the Shenwan directory file, and either the
    posted draft document (``{"value": ...}`` — parse-validated first, so a
    broken draft is a 422 like a save) or the effective stored document. No
    database writes; the same router auth as every other endpoint.
    """

    spec = _spec("industry_groups")
    value = body.value if body is not None else None
    async with session_scope() as session:
        store = ConfigDocStore(session)
        if value is None:
            value, _row = await store.get(spec.kind)
        try:
            mapping = spec.parse(value)
        except ValueError as exc:
            raise _validation_failed(spec, ConfigDocValidationError(str(exc))) from exc
        directory = default_shenwan_directory()
        as_of = shanghai_today()
        members = await csi300_coverage_members(session, as_of, directory)
        coverage = check_industry_groups_coverage(mapping, directory=directory, members=members)
        return IndustryGroupsCoverageOut(
            ok=coverage.ok,
            unmapped_csrc=[
                CoverageGapOut(code=gap.code, name=gap.name, tickers=list(gap.tickers))
                for gap in coverage.unmapped_csrc
            ],
            missing_level1_defaults=[
                CoverageGapOut(code=gap.code, name=gap.name, tickers=list(gap.tickers))
                for gap in coverage.missing_level1_defaults
            ],
            invalid_overrides=[
                CoverageGapOut(code=gap.code, name=gap.name, tickers=list(gap.tickers))
                for gap in coverage.invalid_overrides
            ],
            member_count=coverage.member_count,
            csrc_code_count=coverage.csrc_code_count,
            summary=coverage.summary,
            as_of=as_of.isoformat(),
        )
