"""/rule-templates: read-only view of the stored template versions.

The page shows what the weekly screen uses - the versions in the rule store
- and flags a template file that changed since the last sync
(``file_status``), so a deploy that is not yet synced is visible.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.rule_templates import (
    FileStatus,
    TemplateDetailOut,
    TemplateSummaryOut,
    TemplateVersionOut,
)
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.rules.schema import canonical_hash
from investment_agent.rules.templates import load_template_file, template_files
from investment_agent.rules.validation import RuleValidationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import RuleTemplateRecord

# P2 position factors are not in the registry yet (holding zones only).
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
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _file_hashes() -> tuple[dict[str, str], set[str]]:
    """``(id -> content hash of its file, ids whose file does not load)``.

    A file that fails to parse is keyed by its stem (template files are
    named after their id); the lenient runtime loader is used, as in sync.
    """

    hashes: dict[str, str] = {}
    invalid: set[str] = set()
    for path in template_files():
        try:
            template = load_template_file(path)
        except (RuleValidationError, ValueError, OSError):
            invalid.add(path.stem)
            continue
        hashes[template.id] = canonical_hash(template)
    return hashes, invalid


def _file_status(
    record: RuleTemplateRecord, hashes: dict[str, str], invalid: set[str]
) -> FileStatus:
    if record.id in invalid:
        return "file_invalid"
    digest = hashes.get(record.id)
    if digest is None:
        return "file_missing"
    return "synced" if digest == record.hash else "pending_sync"


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
    }


@router.get("", response_model=list[TemplateSummaryOut])
async def list_templates() -> list[TemplateSummaryOut]:
    """The latest stored version of every template, sorted by id."""

    hashes, invalid = _file_hashes()
    async with session_scope() as session:
        grouped = await _records(session)
        counts = await _open_candidate_counts(session)
    return [
        TemplateSummaryOut(
            **_summary(
                versions[-1],
                version_count=len(versions),
                open_candidates=counts.get(template_id, 0),
                file_status=_file_status(versions[-1], hashes, invalid),
            )
        )
        for template_id, versions in sorted(grouped.items())
    ]


@router.get("/{template_id}", response_model=TemplateDetailOut)
async def get_template(
    template_id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> TemplateDetailOut:
    """One stored version (default: latest) with its version history."""

    async with session_scope() as session:
        stmt = (
            select(RuleTemplateRecord)
            .where(RuleTemplateRecord.id == template_id)
            .order_by(RuleTemplateRecord.version)
        )
        versions = list((await session.execute(stmt)).scalars())
        if not versions:
            raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
        record = next((item for item in versions if item.version == version), None)
        if version is None:
            record = versions[-1]
        if record is None:
            raise HTTPException(
                status_code=404, detail=f"Template {template_id}@{version} not found"
            )
        counts = await _open_candidate_counts(session)
    hashes, invalid = _file_hashes()
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
            file_status=_file_status(versions[-1], hashes, invalid),
        ),
        latest_version=versions[-1].version,
        definition=definition,
        versions=[
            TemplateVersionOut(
                version=item.version,
                hash=item.hash,
                calibration_status=item.calibration_status,
                created_at=_as_utc(item.created_at),
            )
            for item in reversed(versions)
        ],
        factor_units=factor_units,
    )
