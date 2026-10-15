"""Replay one immutable material acquisition using only its local archive."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import ControlledIdentity, source_symbol
from investment_agent.materials.archive import (
    ArchivedCapture,
    ArchiveIntegrityError,
    DatasetLink,
    identity_snapshot,
    record_acquisition,
    verify_capture,
)
from investment_agent.materials.contracts import (
    DatasetDraft,
    FactPeriod,
    MaterialAdapter,
    ParseResult,
    PeriodBasis,
    RawPayload,
)
from investment_agent.materials.offline_import import _LocatedRaw, _prepare_draft
from investment_agent.materials.store import MaterialStore, SavedDataset, coalesce_annual_tables
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.reference import Listing


class ReplayError(ValueError):
    """The requested acquisition cannot be safely replayed with current code."""


@dataclass(frozen=True)
class ReplayReport:
    """Results and gaps from replaying one acquisition."""

    acquisition_id: int
    saved: tuple[SavedDataset, ...]
    problems: tuple[str, ...]
    skipped: tuple[str, ...]
    informational: tuple[str, ...] = ()


async def replay_acquisition(
    session: AsyncSession,
    *,
    acquisition_id: int,
    data_dir: Path,
    identities: Sequence[ControlledIdentity],
    adapters: Mapping[str, MaterialAdapter],
) -> ReplayReport:
    """Verify and parse a database-linked acquisition without touching the network.

    The archive reference and digest come from the immutable database row. The
    current controlled identity and adapter must still match the saved snapshot
    and version before any response body is passed to a parser.
    """

    row = await session.get(ResearchMaterialAcquisition, acquisition_id)
    if row is None:
        raise ReplayError(f"acquisition {acquisition_id} does not exist")
    try:
        capture = verify_capture(
            Path(data_dir), row.archive_ref, expected_sha256=row.manifest_sha256
        )
    except ArchiveIntegrityError as exc:
        raise ReplayError(str(exc)) from exc
    if (
        capture.source != row.source
        or capture.subject_id != row.subject_id
        or capture.adapter_version != row.adapter_version
        or capture.manifest_sha256 != row.manifest_sha256
        or capture.manifest_ref != row.archive_ref
        or capture.identity != row.identity_snapshot
        or list(capture.periods) != row.periods
        or list(capture.expected_requests) != row.expected_requests
        or list(capture.problems) != row.problems
    ):
        raise ReplayError("acquisition row does not match its verified archive manifest")

    identity = next(
        (item for item in identities if item.company_key == capture.identity.get("company_key")),
        None,
    )
    if identity is None or identity_snapshot(identity) != capture.identity:
        raise ReplayError("the archived identity no longer matches the controlled pilot list")
    if source_symbol(identity, capture.source) is None:
        raise ReplayError("the archived source is no longer bound to the controlled identity")
    adapter = adapters.get(capture.source)
    if adapter is None:
        raise ReplayError(f"no current adapter is registered for {capture.source!r}")
    if adapter.adapter_version != capture.adapter_version:
        raise ReplayError(
            f"adapter version mismatch: archive={capture.adapter_version!r}, "
            f"current={adapter.adapter_version!r}"
        )
    listing = await session.scalar(
        select(Listing).where(
            Listing.company_id == capture.subject_id,
            Listing.market_code == identity.market_code,
            Listing.ticker == identity.ticker,
        )
    )
    if listing is None:
        raise ReplayError("archived subject does not own the controlled listing")

    periods = tuple(_period_from_snapshot(item) for item in capture.periods)
    replayed_at = datetime.now(UTC)
    acquisition_captured_at = row.captured_at
    if acquisition_captured_at.tzinfo is None or acquisition_captured_at.utcoffset() is None:
        acquisition_captured_at = acquisition_captured_at.replace(tzinfo=UTC)
    else:
        acquisition_captured_at = acquisition_captured_at.astimezone(UTC)
    bodies = {item.name: item for item in capture.responses}
    expected = set(capture.expected_requests)
    if set(bodies) - expected:
        raise ReplayError("archive contains responses not named by its expected request list")

    raws: list[RawPayload] = []
    located: list[_LocatedRaw] = []
    seen: set[str] = set()
    problems: list[str] = list(capture.problems)
    informational: list[str] = []
    result: ParseResult | None = None
    for _ in range(3):
        requests = tuple(
            request for request in adapter.requests(identity, periods=periods)
            if request.name not in seen
        )
        if not requests:
            break
        for request in requests:
            seen.add(request.name)
            if request.name not in expected:
                problems.append(f"archive is missing expected request record: {request.name}")
                continue
            item = bodies.get(request.name)
            if item is None:
                problems.append(f"archive is missing response body: {request.name}")
                continue
            if not item.parse_eligible:
                problems.append(f"archive response is marked not parse eligible: {item.name}")
                continue
            if not _response_usable(item.http_status):
                problems.append(f"archive response has unsuccessful HTTP status: {item.name}")
                continue
            payload = RawPayload(
                name=item.name,
                content=item.content,
                media_type=item.media_type,
                fetched_at=item.fetched_at or replayed_at,
                source_url=item.source_url,
                http_status=item.http_status,
            )
            raws.append(payload)
            located.append(_LocatedRaw(
                request_name=item.name,
                media_type=item.media_type,
                path=Path(item.name),
                content=item.content,
                fetched_at=item.fetched_at,
                source_url=item.source_url,
                http_status=item.http_status,
            ))
        if not raws:
            continue
        result = adapter.parse(
            identity,
            raw=tuple(raws),
            periods=periods,
            subject_id=capture.subject_id,
        )

    unexpected_expected = expected - seen
    if unexpected_expected:
        problems.append(f"archive contains undiscovered expected requests: {sorted(unexpected_expected)}")
    still_requested = [
        request.name for request in adapter.requests(identity, periods=periods)
        if request.name not in seen
    ]
    if still_requested:
        problems.append(f"adapter discovery exceeded 3 rounds: {still_requested}")
    if result is None:
        problems.append("archive has no usable responses for the current adapter")
        drafts: tuple[DatasetDraft, ...] = ()
    else:
        for problem in result.problems:
            if problem.startswith(("unmapped_items:", "unmapped")):
                informational.append(problem)
            else:
                problems.append(problem)
        drafts = tuple(
            _prepare_draft(
                draft, company_id=capture.subject_id, raws=located, captured_at=replayed_at
            )
            for draft in coalesce_annual_tables(result.drafts)
        )
        if not result.drafts and not result.problems:
            problems.append("the adapter produced no dataset")

    expected_links = tuple(
        DatasetLink(
            id=int(item["id"]), revision=int(item["revision"]),
            content_hash=str(item["content_hash"]),
        )
        for item in row.dataset_links
    )
    archived = ArchivedCapture(
        manifest_ref=capture.manifest_ref,
        manifest_sha256=capture.manifest_sha256,
        captured_at=acquisition_captured_at,
        source=capture.source,
        subject_id=capture.subject_id,
        adapter_version=capture.adapter_version,
        identity=capture.identity,
        periods=capture.periods,
        expected_requests=capture.expected_requests,
        problems=capture.problems,
    )
    # A parser that changed without a version bump must not leave newly flushed
    # datasets in the caller's transaction when replay rejects its output.
    async with session.begin_nested():
        store = MaterialStore(session, data_dir=Path(data_dir))
        saved = tuple([await store.save(draft) for draft in drafts])
        links = tuple(
            DatasetLink(id=item.id, revision=item.revision, content_hash=item.content_hash)
            for item in saved
        )
        if tuple(sorted(links, key=lambda item: item.id)) != tuple(
            sorted(expected_links, key=lambda item: item.id)
        ):
            raise ReplayError("replayed dataset links do not match the acquisition's trusted links")
        await record_acquisition(session, archived, datasets=links)
    skipped = (f"{identity.company_key}/{capture.source}",) if problems else ()
    return ReplayReport(
        acquisition_id=row.id,
        saved=saved,
        problems=tuple(dict.fromkeys(problems)),
        skipped=skipped,
        informational=tuple(dict.fromkeys(informational)),
    )


def _period_from_snapshot(value: Mapping[str, object]) -> FactPeriod:
    """Restore an explicitly archived period without deriving any dates."""

    try:
        raw_start, raw_end = value["start"], value["end"]
        start = date.fromisoformat(raw_start) if isinstance(raw_start, str) else None
        if not isinstance(raw_end, str):
            raise ValueError("period end is missing")
        end = date.fromisoformat(raw_end)
        basis = PeriodBasis(str(value["basis"]))
        label = value["label"]
        days = value["days"]
        if label is not None and not isinstance(label, str):
            raise ValueError("period label is invalid")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int)):
            raise ValueError("period day count is invalid")
        return FactPeriod(start=start, end=end, basis=basis, label=label, days=days)
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError("archive has an invalid period snapshot") from exc


def _response_usable(status: int) -> bool:
    """Reject known unsuccessful responses; status 0 means legacy/unknown."""

    return status == 0 or 200 <= status < 300
