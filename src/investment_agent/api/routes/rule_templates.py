"""/rule-templates: the stored template versions and the authoring seat (WP4).

Reads show what the weekly screen uses - the versions in the rule store -
and compare the *current* stored version with the mirror baseline file
(``file_status``, CD6): the DB is the source of truth, a differing file is
surfaced as ``baseline_changed`` drift, never silently imported.

Writes are the template authoring seat (config-docs-plan §4.5 / CD6):

* ``POST /{id}/validate`` - dry-run of the new-version validation (the 校验
  button); returns every issue without writing;
* ``POST /{id}/versions`` - the next immutable version of a full template
  document (the same validation as sync time, including ``applicable_groups``
  against the DB industry groups);
* ``POST /{id}/activate`` - make an existing version the current one
  (append-only rollback: its content is re-stored as the next version);
* ``POST /{id}/baseline-import`` - pull the baseline file's content in as a
  new version (the explicit 从基线导入), recording the file hash.

Every write carries a mandatory note and an actor (``api`` for a key,
``web`` for a session — the config-docs PATCH attribution, CD7).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.rule_templates import (
    FileStatus,
    RuleIssueOut,
    TemplateActivateRequest,
    TemplateBaselineImportRequest,
    TemplateDetailOut,
    TemplateSummaryOut,
    TemplateValidateOut,
    TemplateValidateRequest,
    TemplateVersionCreateRequest,
    TemplateVersionOut,
    TemplateWriteOut,
)
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.rules.schema import RuleTemplate, canonical_hash
from investment_agent.rules.store import RuleStore, RuleStoreError
from investment_agent.rules.templates import load_template_file, template_files
from investment_agent.rules.validation import (
    RuleIssue,
    RuleValidationError,
    parse_template,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import RuleTemplateRecord

# Position factors are derived from a holding's daily valuation by the zone
# detector, so they are not in the factor registry; their units for rendering.
PENDING_FACTOR_UNITS: dict[str, str] = {
    "position_weight": "fraction",
    "gain_vs_cost": "fraction",
    "holding_days": "days",
}

router = APIRouter(
    prefix="/rule-templates",
    tags=["rule-templates"],
    dependencies=[Depends(verify_api_key_or_session)],
)


def _as_utc(value: datetime) -> datetime:
    # Deliberately not domain.time.as_utc: keeps a stored aware value's offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _actor(x_api_key: str | None) -> str:
    """The config-docs attribution: ``api`` for a key, ``web`` for a session."""

    return "api" if x_api_key is not None else "web"


def _note(body_note: str) -> str:
    # The settings PATCH mechanism: trim, and let the blank reach the 422.
    return body_note.strip() if body_note and body_note.strip() else ""


def _baseline_files() -> dict[str, tuple[str, RuleIssue | None]]:
    """``id -> (content hash of its file, load error)`` of the baseline tree.

    A file that fails to parse is keyed by its stem (template files are
    named after their id); the lenient runtime loader is used, as in sync.
    """

    entries: dict[str, tuple[str, RuleIssue | None]] = {}
    for path in template_files():
        try:
            template = load_template_file(path)
        except RuleValidationError as exc:
            entries[path.stem] = ("", exc.issues[0] if exc.issues else RuleIssue("schema", "$", str(exc)))
            continue
        except (ValueError, OSError) as exc:
            entries[path.stem] = ("", RuleIssue("schema", "$", str(exc)))
            continue
        entries[template.id] = (canonical_hash(template), None)
    return entries


def _file_status(
    latest: RuleTemplateRecord,
    versions: list[RuleTemplateRecord],
    files: dict[str, tuple[str, RuleIssue | None]],
) -> tuple[FileStatus, int | None]:
    """CD6: the current stored version vs the mirror baseline file.

    Returns the status plus ``baseline_version`` — the highest stored version
    whose content hash matches the file (``None`` when the file content
    matches no stored version). ``synced`` means the file *is* the current
    version; anything else keeps the store untouched until an explicit
    从基线导入.
    """

    entry = files.get(latest.id)
    if entry is None:
        return "file_missing", None
    digest, error = entry
    if error is not None:
        return "file_invalid", None
    if digest == latest.hash:
        return "synced", latest.version
    matching = [item.version for item in versions if item.hash == digest]
    return "baseline_changed", max(matching) if matching else None


def _count_conditions(node: Any) -> int:
    if isinstance(node, dict):
        if "factor" in node:
            return 1
        return sum(_count_conditions(child) for key in ("all", "any") for child in node.get(key) or [])
    return 0


def _factors(node: Any) -> list[str]:
    if isinstance(node, dict):
        if isinstance(node.get("factor"), str):
            return [node["factor"]]
        return [name for key in ("all", "any") for child in node.get(key) or [] for name in _factors(child)]
    return []


async def _open_candidate_counts(session: AsyncSession) -> Counter[str]:
    """Open candidates per template id (a candidate counts once per template)."""

    counts: Counter[str] = Counter()
    stmt = select(CandidateRecord.sources).where(CandidateRecord.status == "open")
    for (sources,) in (await session.execute(stmt)).all():
        ids = {item.get("template_id") for item in sources or [] if isinstance(item, dict)}
        counts.update(template_id for template_id in ids if isinstance(template_id, str))
    return counts


async def _records(session: AsyncSession) -> dict[str, list[RuleTemplateRecord]]:
    stmt = select(RuleTemplateRecord).order_by(RuleTemplateRecord.id, RuleTemplateRecord.version)
    grouped: dict[str, list[RuleTemplateRecord]] = {}
    for record in (await session.execute(stmt)).scalars():
        grouped.setdefault(record.id, []).append(record)
    return grouped


def _summary(
    record: RuleTemplateRecord,
    *,
    version_count: int,
    open_candidates: int,
    file_status: FileStatus,
    baseline_version: int | None,
) -> dict[str, Any]:
    definition: dict[str, Any] = dict(record.definition or {})
    return {
        "id": record.id,
        "version": record.version,
        "ref": f"{record.id}@{record.version}",
        "name": record.name,
        "description": definition.get("description"),
        "thesis": definition.get("thesis"),
        "calibration_status": record.calibration_status,
        "applicable_groups": list(definition.get("applicable_groups") or []),
        "screen_condition_count": _count_conditions(definition.get("screen")),
        "param_count": len(definition.get("params") or {}),
        "version_count": version_count,
        "synced_at": _as_utc(record.created_at),
        "open_candidates": open_candidates,
        "file_status": file_status,
        "baseline_version": baseline_version,
    }


async def _detail(
    session: AsyncSession,
    template_id: str,
    version: int | None,
) -> TemplateDetailOut:
    """The GET / write response body: one stored version with its history."""

    stmt = (
        select(RuleTemplateRecord)
        .where(RuleTemplateRecord.id == template_id)
        .order_by(RuleTemplateRecord.version)
    )
    versions = list((await session.execute(stmt)).scalars())
    if not versions:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    record = versions[-1] if version is None else next(
        (item for item in versions if item.version == version), None
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id}@{version} not found")
    counts = await _open_candidate_counts(session)
    files = _baseline_files()
    file_status, baseline_version = _file_status(versions[-1], versions, files)
    definition: dict[str, Any] = dict(record.definition or {})
    trees: list[Any] = [definition.get("screen")]
    for key in ("candidate_zones", "holding_zones"):
        trees.extend((definition.get(key) or {}).values())
    factor_units: dict[str, str] = {}
    for name in dict.fromkeys(name for tree in trees for name in _factors(tree)):
        if name in DEFAULT_REGISTRY:
            factor_units[name] = DEFAULT_REGISTRY.get(name).unit
        elif name in PENDING_FACTOR_UNITS:
            factor_units[name] = PENDING_FACTOR_UNITS[name]
    return TemplateDetailOut(
        **_summary(
            record,
            version_count=len(versions),
            open_candidates=counts.get(template_id, 0),
            file_status=file_status,
            baseline_version=baseline_version,
        ),
        latest_version=versions[-1].version,
        definition=definition,
        versions=[
            TemplateVersionOut(
                version=item.version,
                hash=item.hash,
                calibration_status=item.calibration_status,
                created_at=_as_utc(item.created_at),
                created_by=item.created_by,
                note=item.note,
            )
            for item in reversed(versions)
        ],
        factor_units=factor_units,
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[TemplateSummaryOut])
async def list_templates() -> list[TemplateSummaryOut]:
    """The latest stored version of every template, sorted by id."""

    files = _baseline_files()
    async with session_scope() as session:
        grouped = await _records(session)
        counts = await _open_candidate_counts(session)
    summaries: list[TemplateSummaryOut] = []
    for template_id, versions in sorted(grouped.items()):
        file_status, baseline_version = _file_status(versions[-1], versions, files)
        summaries.append(
            TemplateSummaryOut(
                **_summary(
                    versions[-1],
                    version_count=len(versions),
                    open_candidates=counts.get(template_id, 0),
                    file_status=file_status,
                    baseline_version=baseline_version,
                )
            )
        )
    return summaries


@router.get("/{template_id}", response_model=TemplateDetailOut)
async def get_template(
    template_id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> TemplateDetailOut:
    """One stored version (default: latest) with its version history."""

    async with session_scope() as session:
        return await _detail(session, template_id, version)


# --------------------------------------------------------------------------- #
# Authoring (WP4, CD6 / CD7)
# --------------------------------------------------------------------------- #


def _issues(exc: RuleValidationError) -> list[RuleIssueOut]:
    return [
        RuleIssueOut(code=item.code, path=item.path, message=item.message) for item in exc.issues
    ]


def _issue(exc: RuleValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "message": "template validation failed",
            "issues": [item.model_dump() for item in _issues(exc)],
        },
    )


def _parse_document(template_id: str, document: dict[str, Any]) -> RuleTemplate:
    """Validate a posted template document exactly like a file at sync time.

    The document's ``id`` must name the addressed template (an authored
    version never renames or forks it); ``version`` is informational and
    ignored. Returns the parsed :class:`RuleTemplate` or raises
    :class:`RuleValidationError` with every issue. The validation context
    is the default (DB-backed) one of sync time.
    """

    doc_id = document.get("id")
    if doc_id != template_id:
        raise RuleValidationError(
            [
                RuleIssue(
                    "schema", "id", f"document id {doc_id!r} does not name template {template_id!r}"
                )
            ],
            source=template_id,
        )
    return parse_template({**document, "id": template_id}, source=template_id)


async def _require_template(session: AsyncSession, template_id: str) -> None:
    latest = await RuleStore(session).latest_template_record(template_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")


async def _write_response(
    template_id: str, record: RuleTemplateRecord, action: str
) -> TemplateWriteOut:
    async with session_scope() as session:
        detail = await _detail(session, template_id, None)
    return TemplateWriteOut(
        action=action,  # type: ignore[arg-type]
        version=record.version,
        hash=record.hash,
        created_by=record.created_by,
        note=record.note,
        detail=detail,
    )


@router.post("/{template_id}/validate", response_model=TemplateValidateOut)
async def validate_template_version(
    template_id: str,
    body: TemplateValidateRequest,
) -> TemplateValidateOut:
    """Dry-run of the new-version validation (the 校验 button); no write.

    ``200`` with ``ok=false`` and every issue; only an unknown template id
    is a 404.
    """

    async with session_scope() as session:
        await _require_template(session, template_id)
        try:
            _parse_document(template_id, body.document)
        except RuleValidationError as exc:
            return TemplateValidateOut(ok=False, issues=_issues(exc))
        return TemplateValidateOut(ok=True)


@router.post("/{template_id}/versions", response_model=TemplateWriteOut)
async def create_template_version(
    template_id: str,
    body: TemplateVersionCreateRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> TemplateWriteOut:
    """Draft the next immutable version from a full template document.

    ``422`` carries every validation issue (per code / path); a note is
    mandatory. A document whose content equals the current version writes
    nothing (``unchanged``).
    """

    if not _note(body.note):
        raise HTTPException(status_code=422, detail="a change note is required")
    async with session_scope() as session:
        await _require_template(session, template_id)
        try:
            template = _parse_document(template_id, body.document)
        except RuleValidationError as exc:
            raise _issue(exc) from exc
        try:
            record, action = await RuleStore(session).create_version(
                template, actor=_actor(x_api_key), note=_note(body.note)
            )
        except RuleValidationError as exc:
            raise _issue(exc) from exc
    return await _write_response(template_id, record, action)


@router.post("/{template_id}/activate", response_model=TemplateWriteOut)
async def activate_template_version(
    template_id: str,
    body: TemplateActivateRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> TemplateWriteOut:
    """Make an existing version the current one (append-only rollback)."""

    if not _note(body.note):
        raise HTTPException(status_code=422, detail="a change note is required")
    async with session_scope() as session:
        await _require_template(session, template_id)
        try:
            record, action = await RuleStore(session).activate_template(
                template_id, body.version, actor=_actor(x_api_key), note=_note(body.note)
            )
        except RuleStoreError as exc:
            raise HTTPException(
                status_code=404, detail=f"Template {template_id}@{body.version} not found"
            ) from exc
        except RuleValidationError as exc:
            raise _issue(exc) from exc
    return await _write_response(template_id, record, action)


def _baseline_template(template_id: str) -> tuple[Path, RuleTemplate] | None:
    """``(path, parsed template)`` of the template's baseline file.

    ``None`` when no file is named after the id; a file that does not load is
    a 422 (the same lenient loader as sync).
    """

    for path in template_files():
        if path.stem != template_id:
            continue
        try:
            return path, load_template_file(path)
        except (RuleValidationError, ValueError, OSError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"the baseline file of {template_id} does not load: {exc}",
            ) from exc
    return None


@router.post("/{template_id}/baseline-import", response_model=TemplateWriteOut)
async def import_template_baseline(
    template_id: str,
    body: TemplateBaselineImportRequest,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> TemplateWriteOut:
    """Pull the baseline file's content in as a new version (从基线导入).

    The new version's hash is the file's content hash; nothing is written
    when the file already *is* the current version (``unchanged``). A file
    matching an older stored version is re-stored as the next version, so
    the import always leaves the baseline effective.
    """

    if not _note(body.note):
        raise HTTPException(status_code=422, detail="a change note is required")
    async with session_scope() as session:
        await _require_template(session, template_id)
    found = _baseline_template(template_id)
    if found is None or found[1].id != template_id:
        raise HTTPException(
            status_code=422, detail=f"no baseline file defines template {template_id}"
        )
    path, template = found
    async with session_scope() as session:
        try:
            record, action = await RuleStore(session).create_version(
                template,
                actor=_actor(x_api_key),
                note=_note(body.note),
                source_path=str(path),
            )
        except RuleValidationError as exc:
            raise _issue(exc) from exc
    return await _write_response(template_id, record, action)
