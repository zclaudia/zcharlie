"""Single-security material collection handler (plan 06).

``material_collection`` reuses the whole queue machinery
(:mod:`investment_agent.research.worker`: claiming, lease fencing, heartbeats,
retry budget, stale recovery, cancellation) and owns only what is specific to
collecting one security's research material:

1. resolve ``listing -> company -> checked pilot identity``; a listing outside
   the hand-checked list finishes ``partial`` with no upstream call at all;
2. keep the sources the identity binds; an unbound source is recorded as
   ``source_not_bound`` and is never fetched;
3. refuse to fetch while :data:`ONLINE_REFRESH_ENABLED` is false (the default:
   the Eastmoney / Yahoo licences are not confirmed) — zero network calls;
4. otherwise ask the adapter which bounded requests it needs, fetch them
   through the rate-limited, user-agent-carrying fetcher, parse, and store
   every draft through ``MaterialStore`` (immutable, content-hash versioned);
5. commit after every source and record a checkpoint, so a retried attempt
   resumes instead of redoing a source that already finished;
6. structural parse problems (a changed upstream shape, a truncated page) are
   reported per source and never retried blindly; informational counters
   (``unmapped_items:`` and friends) go to the run metadata's
   ``informational`` list and leave the outcome untouched;
7. transient network failures keep the retry budget: the checkpoint is written
   first and the :class:`ProviderTransientError` is re-raised, so the worker
   schedules the retry and the next attempt skips the finished sources;
8. the run is ``completed`` only when every requested source was stored
   cleanly; anything else is ``partial`` and names its reason in the metadata.

The handler never invents data. Adapters build the drafts (including the real
period spans), the store is the single write path, and a source that is not
bound, not covered or not licensed simply produces no material.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    ControlledIdentity,
    find_identity,
    source_symbol,
)
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.materials.contracts import (
    DatasetDraft,
    FactPeriod,
    ParseResult,
    PeriodBasis,
    RawPayload,
    UpstreamRequest,
)
from investment_agent.materials.jobs import (
    MATERIAL_COLLECTION,
    MaterialCollectionPayload,
)
from investment_agent.materials.store import MaterialStore, SavedDataset, coalesce_annual_tables
from investment_agent.providers.errors import ProviderDataError, ProviderTransientError
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

ONLINE_REFRESH_ENABLED = False
"""Whether a run may call upstream at all.

The first phase is deliberately offline: the Eastmoney / Yahoo usage licences
are not confirmed (design §尚未验证能力), so every collection stops after
recording ``online_refresh_disabled``. Turning this on is a review decision,
not a preference — no runtime setting is added on purpose, so the offline
promise cannot be flipped from the settings UI.
"""

USER_AGENT = "zcharlie-research/0.1 (personal research)"
"""The single user agent every material request sends."""

MIN_INTERVAL: Mapping[str, float] = {
    "sec.companyfacts.v1": 0.5,
    "eastmoney.hk.native.v1": 1.0,
}
"""Minimum seconds between two calls to one source (plan 06)."""

DEFAULT_MIN_INTERVAL = 1.0
"""Interval used for an upstream host that no source id claims."""

REQUEST_TIMEOUT_SECONDS = 30.0
"""Per-request timeout of the default fetcher."""

MAX_ADAPTER_PASSES = 2
"""How often an adapter may name further requests after a parse.

``SecFactsAdapter`` is a two-stage builder (CompanyFacts + Submissions, then
the historical shards those files list), so one fetch round is not enough for
it; beyond two rounds a well-behaved adapter only re-asks the same documents.
"""

_SOURCE_HOSTS: Mapping[str, str] = {
    "sec.companyfacts.v1": "data.sec.gov",
    "eastmoney.hk.native.v1": "datacenter.eastmoney.com",
}
"""Source id -> upstream host; used only to give each host its own limiter."""

INFORMATIONAL_PROBLEM_PREFIXES: tuple[str, ...] = ("unmapped_items:", "unmapped")
"""Adapter problem prefixes that are counters, not findings.

Task 03 counts every statement line it did not map (``unmapped_items:
<table>/<n>``) on almost every parse, and task 04 counts unmapped taxonomy
concepts the same way. Neither means the material became unusable, so a
problem carrying one of these prefixes is recorded as informational and leaves
the source's outcome untouched; every other problem is structural and marks
the source ``structural_failure`` (reported, never blindly retried).
"""

SOURCE_SAVED = "saved"
SOURCE_STRUCTURAL_FAILURE = "structural_failure"
SOURCE_DATA_ERROR = "data_error"
SOURCE_NOT_BOUND = "source_not_bound"
SOURCE_ADAPTER_VERSION_MISMATCH = "adapter_version_mismatch"
SOURCE_ONLINE_REFRESH_DISABLED = "online_refresh_disabled"
LISTING_NOT_COVERED = "listing_not_covered"
"""Run metadata vocabulary: per-source status, plus the uncovered listing."""

_FINAL_SOURCE_STATUSES = frozenset(
    {
        SOURCE_SAVED,
        SOURCE_STRUCTURAL_FAILURE,
        SOURCE_DATA_ERROR,
        SOURCE_NOT_BOUND,
        SOURCE_ADAPTER_VERSION_MISMATCH,
    }
)
"""Statuses a reattempt must not redo (a retry resumes past them)."""

SKIPPED_ALREADY_CHECKED = "already_checked"
"""Why a requested source was not touched again in this attempt."""


class MaterialFetcher(Protocol):
    """One bounded upstream call, as an adapter's request describes it.

    ``identity`` is the checked identity the request was built from; the
    default fetcher only passes it through (the adapter already put the
    source-specific code into the URL), but a fetcher that signs requests can
    use it.
    """

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload: ...


class MaterialSourceAdapter(Protocol):
    """Material adapter with a concrete controlled identity annotation.

    Both offline and worker paths supply ``subject_id=company.id``; controlled
    identities contain no database id. This mirrors ``MaterialAdapter``.
    """

    @property
    def source(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]: ...

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult: ...


class HttpxMaterialFetcher:
    """Minimal HTTPS fetcher: one rate limiter per upstream host, one UA.

    Error mapping (plan 06): a connection / timeout failure and HTTP 408, 429
    or 5xx become :class:`ProviderTransientError` (the worker retries within
    its budget); any other 4xx and a body that is not JSON become
    :class:`ProviderDataError` (terminal for that source — retrying cannot fix
    a request the upstream rejected). The response is handed back as the raw
    bytes it was, with its real fetch time and URL.
    """

    def __init__(
        self,
        *,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        user_agent: str = USER_AGENT,
        min_interval: Mapping[str, float] | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self._min_interval = dict(MIN_INTERVAL if min_interval is None else min_interval)
        self._client = client
        self._limiters: dict[str, AsyncRateLimiter] = {}

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload:
        del identity  # the request already carries the source-specific code
        await self.limiter_for(request).acquire()
        if self._client is None:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=self._headers()
            ) as client:
                response = await self._send(client, request)
        else:
            response = await self._send(self._client, request)
        return self._payload(request, response)

    def limiter_for(self, request: UpstreamRequest) -> AsyncRateLimiter:
        """The limiter of ``request``'s upstream host (one per host, shared)."""

        key = urlparse(request.url).netloc
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = AsyncRateLimiter(rate_per_minute=60.0 / self.min_interval_for(request))
            self._limiters[key] = limiter
        return limiter

    def min_interval_for(self, request: UpstreamRequest) -> float:
        """Seconds between two calls to ``request``'s source."""

        host = urlparse(request.url).netloc
        for source, source_host in _SOURCE_HOSTS.items():
            if source_host == host:
                return self._min_interval.get(source, DEFAULT_MIN_INTERVAL)
        return DEFAULT_MIN_INTERVAL

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/json"}

    async def _send(self, client: httpx.AsyncClient, request: UpstreamRequest) -> httpx.Response:
        provider = self._provider(request)
        try:
            response = await client.get(
                request.url, params=list(request.params), headers=self._headers()
            )
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                f"{request.name}: {type(exc).__name__}: {exc}",
                provider=provider,
                error_code="transport",
            ) from exc
        status = response.status_code
        if status in {408, 429} or status >= 500:
            raise ProviderTransientError(
                f"{request.name}: upstream HTTP {status}",
                provider=provider,
                error_code=f"http_{status}",
            )
        if status >= 400:
            raise ProviderDataError(
                f"{request.name}: upstream HTTP {status}",
                provider=provider,
                error_code=f"http_{status}",
            )
        return response

    def _payload(self, request: UpstreamRequest, response: httpx.Response) -> RawPayload:
        content = response.content
        if request.media_type == "application/json":
            try:
                json.loads(content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProviderDataError(
                    f"{request.name}: the response body is not JSON: {exc}",
                    provider=self._provider(request),
                    error_code="not_json",
                ) from exc
        return RawPayload(
            name=request.name,
            content=content,
            media_type=request.media_type,
            fetched_at=datetime.now(UTC),
            source_url=str(response.url),
            http_status=response.status_code,
        )

    def _provider(self, request: UpstreamRequest) -> str:
        host = urlparse(request.url).netloc
        for source, source_host in _SOURCE_HOSTS.items():
            if source_host == host:
                return source
        return host or "unknown"


def default_adapters() -> dict[str, MaterialSourceAdapter]:
    """The production adapter set, keyed by source id.

    Imported inside the function on purpose: the HK and SEC adapters are the
    heavy modules of this package, and a caller (a test, the offline importer,
    a later task) must be able to inject its own mapping without paying for
    them or replacing them.
    """

    from investment_agent.materials.hk_native import HkNativeAdapter
    from investment_agent.materials.us_sec import SecFactsAdapter

    return {
        HkNativeAdapter.source: HkNativeAdapter(),
        SecFactsAdapter.source: SecFactsAdapter(),
    }


def is_informational_problem(problem: str) -> bool:
    """Whether an adapter ``problems`` entry is a counter, not a finding."""

    return problem.startswith(INFORMATIONAL_PROBLEM_PREFIXES)


@dataclass(frozen=True)
class _Subject:
    """The listing this run collects, resolved to its checked identity."""

    listing_id: int
    company_id: int
    identity: ControlledIdentity


class MaterialCollectionHandler(BaseJobHandler):
    """``material_collection``: one security's material, one queue run."""

    kind = MATERIAL_COLLECTION

    def __init__(
        self,
        *,
        settings: Settings,
        fetcher: MaterialFetcher | None = None,
        adapters: Mapping[str, MaterialSourceAdapter] | None = None,
        clock: Callable[[], datetime] | None = None,
        pilot_list_path: Path = DEFAULT_PILOT_LIST_PATH,
    ) -> None:
        self.settings = settings
        self.fetcher: MaterialFetcher = HttpxMaterialFetcher() if fetcher is None else fetcher
        self.pilot_list_path = Path(pilot_list_path)
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._adapters: Mapping[str, MaterialSourceAdapter] | None = (
            None if adapters is None else dict(adapters)
        )

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = MaterialCollectionPayload.model_validate(job.request_json)
        return await ctx.run_with_heartbeat(job.lease, lambda: self._collect(job, payload))

    # --- the run ----------------------------------------------------------

    async def _collect(self, job: ClaimedJob, payload: MaterialCollectionPayload) -> JobOutcome:
        async with session_scope() as session:
            repository = PipelineRunRepository(session)
            state = _RunState(
                listing_id=payload.listing_id,
                requested=payload.sources,
                checked_at=self.clock(),
                previous=await self._previous_metadata(repository, job.run_id),
            )
            subject = await self._resolve_subject(session, payload.listing_id)
            if subject is None:
                state.listing_status = LISTING_NOT_COVERED
                state.note_problem(
                    f"{LISTING_NOT_COVERED}: the listing has no checked HK/US identity "
                    "in the pilot list"
                )
                await self._finish_partial(
                    session, repository, job, state, summary=LISTING_NOT_COVERED
                )
                return "partial"
            state.listing_status = "covered"
            state.subject = _subject_metadata(subject)
            if not ONLINE_REFRESH_ENABLED:
                _note_refresh_disabled(state)
                await self._finish_partial(
                    session, repository, job, state, summary=SOURCE_ONLINE_REFRESH_DISABLED
                )
                return "partial"

            store = MaterialStore(session, data_dir=self.settings.data_dir)
            adapters = self._adapter_map()
            for source in payload.sources:
                if state.is_final(source):
                    # A source that finished in an earlier attempt is not redone.
                    state.skip(source, SKIPPED_ALREADY_CHECKED)
                    continue
                await self._run_source(
                    state=state,
                    source=source,
                    subject=subject,
                    payload=payload,
                    adapters=adapters,
                    store=store,
                )
                await self._checkpoint(session, repository, job, state)
            if self._outcome(state) == "completed":
                await repository.mark_success(job.run_id, lease=job.lease)
                return "completed"
            await self._finish_partial(
                session, repository, job, state, summary=_partial_summary(state)
            )
            return "partial"

    async def _run_source(
        self,
        *,
        state: _RunState,
        source: str,
        subject: _Subject,
        payload: MaterialCollectionPayload,
        adapters: Mapping[str, MaterialSourceAdapter],
        store: MaterialStore,
    ) -> None:
        """Fetch, parse and store one source; a bad answer never raises."""

        if source_symbol(subject.identity, source) is None:
            state.mark(source, SOURCE_NOT_BOUND)
            state.skip(source, SOURCE_NOT_BOUND)
            return
        adapter = adapters.get(source)
        if adapter is None:
            # A wiring defect, not a data problem: no adapter may silently
            # stand in for a requested source.
            raise ValueError(f"no material adapter is wired for source {source!r}")
        declared = payload.adapter_versions.get(source)
        if declared is not None and declared != adapter.adapter_version:
            state.mark(source, SOURCE_ADAPTER_VERSION_MISMATCH)
            state.note_problem(
                f"{source}: adapter_version_mismatch: licensed {declared!r} != "
                f"adapter {adapter.adapter_version!r}"
            )
            return
        try:
            drafts, problems, raw = await self._fetch_and_parse(
                source=source,
                adapter=adapter,
                subject=subject,
                periods=_requested_periods(payload),
                state=state,
            )
        except ProviderTransientError:
            # The caller's checkpoint keeps what already finished; the worker
            # decides whether the retry budget allows another attempt.
            raise
        except ProviderDataError as exc:
            state.mark(source, SOURCE_DATA_ERROR)
            state.note_problem(f"{source}: data_error: {exc}")
            return

        structural = [problem for problem in problems if not is_informational_problem(problem)]
        for problem in problems:
            if is_informational_problem(problem):
                state.note_informational(f"{source}: {problem}")
            else:
                state.note_problem(f"{source}: {problem}")
        if drafts:
            storeable = _attach_raw(coalesce_annual_tables(drafts), raw)
            state.record_saved(source, [await store.save(draft) for draft in storeable])
        elif not structural:
            state.note_problem(
                f"{source}: the adapter produced no dataset and reported no problem; "
                "nothing was stored"
            )
        state.mark(source, SOURCE_STRUCTURAL_FAILURE if structural else SOURCE_SAVED)

    async def _fetch_and_parse(
        self,
        *,
        source: str,
        adapter: MaterialSourceAdapter,
        subject: _Subject,
        periods: Sequence[FactPeriod],
        state: _RunState,
    ) -> tuple[tuple[DatasetDraft, ...], tuple[str, ...], tuple[RawPayload, ...]]:
        """Fetch every request the adapter asks for, then parse the responses.

        An adapter may name further documents only after a parse (the SEC
        submissions shards), so this asks again after each round. Every
        response fetched so far is recorded in the run metadata: when no raw
        artifact is kept (several responses per source cannot be attributed to
        a single dataset) that record is the audit trail of what came back.
        """

        raw: list[RawPayload] = []
        seen: set[str] = set()
        result: ParseResult | None = None
        for _ in range(MAX_ADAPTER_PASSES + 1):
            pending = tuple(
                request
                for request in adapter.requests(subject.identity, periods=periods)
                if request.name not in seen
            )
            if not pending:
                break
            for request in pending:
                fetched = await self.fetcher.fetch(request, identity=subject.identity)
                seen.add(fetched.name)
                raw.append(fetched)
                state.record_responses(source, raw)
            result = adapter.parse(
                subject.identity,
                raw=tuple(raw),
                periods=periods,
                subject_id=subject.company_id,
            )
        if result is None:
            # Nothing was requested (or only already-seen documents): the
            # adapter's own parse is still what reports the problem.
            result = adapter.parse(
                subject.identity,
                raw=tuple(raw),
                periods=periods,
                subject_id=subject.company_id,
            )
        return result.drafts, result.problems, tuple(raw)

    # --- resolution, checkpoints, terminal status -------------------------

    def _adapter_map(self) -> Mapping[str, MaterialSourceAdapter]:
        if self._adapters is None:
            self._adapters = default_adapters()
        return self._adapters

    async def _resolve_subject(self, session: AsyncSession, listing_id: int) -> _Subject | None:
        """``listing -> company -> checked identity``; ``None`` when uncovered.

        The company row is what makes the run storable at all (every dataset of
        this job is company-level), and the identity is the authority for the
        per-source symbols; either missing means the security is outside the
        hand-checked list and no request may be built.
        """

        listing = await session.get(Listing, listing_id)
        if listing is None:
            return None
        company = await session.get(Company, listing.company_id)
        if company is None:
            return None
        identity = find_identity(
            market_code=listing.market_code,
            ticker=listing.ticker,
            path=self.pilot_list_path,
        )
        if identity is None:
            return None
        return _Subject(listing_id=listing.id, company_id=company.id, identity=identity)

    @staticmethod
    async def _previous_metadata(
        repository: PipelineRunRepository, run_id: int
    ) -> Mapping[str, object]:
        """The progress an earlier attempt recorded (empty on a first attempt)."""

        run = await repository.get(run_id)
        metadata = None if run is None else run.metadata_json
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    async def _checkpoint(
        session: AsyncSession,
        repository: PipelineRunRepository,
        job: ClaimedJob,
        state: _RunState,
    ) -> None:
        """Make every finished source durable (rows, raw artifacts, metadata)."""

        await repository.record_metadata(job.run_id, state.as_metadata(), lease=job.lease)
        await session.commit()

    async def _finish_partial(
        self,
        session: AsyncSession,
        repository: PipelineRunRepository,
        job: ClaimedJob,
        state: _RunState,
        *,
        summary: str,
    ) -> None:
        """Record the last progress, then close the run as ``partial``."""

        await self._checkpoint(session, repository, job, state)
        await repository.mark_partial(job.run_id, summary, lease=job.lease)

    @staticmethod
    def _outcome(state: _RunState) -> JobOutcome:
        if state.problems:
            return "partial"
        if all(state.status.get(source) == SOURCE_SAVED for source in state.requested):
            return "completed"
        return "partial"


class _RunState:
    """The run's own progress record, rebuilt from the run metadata.

    Rebuilding it is what makes a retry cheap: a source that reached a final
    status in an earlier attempt is skipped instead of fetched again, while the
    datasets it stored stay exactly as they were written (the store is
    immutable and content-hash versioned).
    """

    def __init__(
        self,
        *,
        listing_id: int,
        requested: Sequence[str],
        checked_at: datetime,
        previous: Mapping[str, object],
    ) -> None:
        self.listing_id = listing_id
        self.requested = tuple(requested)
        self.checked_at = checked_at
        self.listing_status = _text_or_none(previous.get("listing_status")) or "pending"
        self.subject = _mapping_or_none(previous.get("subject"))
        self.status = _string_map(previous.get("source_status"))
        self.saved = _mapping_list(previous.get("saved"))
        self.skipped = _mapping_list(previous.get("skipped"))
        self.responses = _response_map(previous.get("responses"))
        self.problems = _string_list(previous.get("problems"))
        self.informational = _string_list(previous.get("informational"))

    def is_final(self, source: str) -> bool:
        return self.status.get(source) in _FINAL_SOURCE_STATUSES

    def mark(self, source: str, status: str) -> None:
        self.status[source] = status

    def skip(self, source: str, reason: str) -> None:
        entry: dict[str, object] = {"source": source, "reason": reason}
        if entry not in self.skipped:
            self.skipped.append(entry)

    def note_problem(self, problem: str) -> None:
        if problem not in self.problems:
            self.problems.append(problem)

    def note_informational(self, problem: str) -> None:
        if problem not in self.informational:
            self.informational.append(problem)

    def record_saved(self, source: str, saved: Sequence[SavedDataset]) -> None:
        known = {entry.get("dataset_id") for entry in self.saved}
        for outcome in saved:
            if outcome.id in known:
                continue
            self.saved.append(
                {
                    "source": source,
                    "dataset_id": outcome.id,
                    "created": outcome.created,
                    "content_hash": outcome.content_hash,
                }
            )

    def record_responses(self, source: str, raw: Sequence[RawPayload]) -> None:
        self.responses[source] = [_response_record(item) for item in raw]

    def as_metadata(self) -> dict[str, object]:
        """The checkpoint/terminal metadata of this run (values are JSON-safe)."""

        return {
            "listing_id": self.listing_id,
            "listing_status": self.listing_status,
            "subject": self.subject,
            "checked_at": self.checked_at.isoformat(),
            "requested_sources": list(self.requested),
            "checked": [
                source
                for source in self.requested
                if self.status.get(source) in _FINAL_SOURCE_STATUSES
            ],
            "source_status": {
                source: self.status[source] for source in self.requested if source in self.status
            },
            "saved": self.saved,
            "skipped": self.skipped,
            "responses": self.responses,
            "problems": self.problems,
            "informational": self.informational,
        }


# --- helpers ---------------------------------------------------------------


def _attach_raw(
    drafts: Sequence[DatasetDraft], raw: Sequence[RawPayload]
) -> tuple[DatasetDraft, ...]:
    """Attach the response a single-response source produced to every draft.

    The store keeps the response bytes as the version's raw artifact. With one
    response for the source the link is unambiguous; with several the adapter's
    payloads cannot be attributed to one of them without guessing, so no
    artifact is written (the same rule the offline importer applies, and the
    run metadata keeps the response digests instead). ``source_fetched_at``
    stays untouched: on this path the fetch *is* now, and that field belongs to
    the imports of older evidence.
    """

    if len(raw) != 1:
        return tuple(drafts)
    fetched = raw[0]
    return tuple(
        draft
        if draft.raw_bytes is not None
        else replace(
            draft,
            raw_bytes=fetched.content,
            raw_source_url=draft.raw_source_url or fetched.source_url,
        )
        for draft in drafts
    )


def _requested_periods(payload: MaterialCollectionPayload) -> tuple[FactPeriod, ...]:
    """The payload's period ends as adapter periods.

    Only the end date travels: the source states the real span (an issuer's
    52/53-week year is not a calendar year) and no start date is invented here.
    Empty stays empty, which every adapter reads as "your default window".

    The HK adapter (task 03) does ask for a stated span, so an explicit HK
    period end is answered with its ``unsupported_period`` problem instead of a
    fabricated January 1st start: the run reports the gap in its metadata,
    which is the honest outcome while the payload carries ends only.
    """

    return tuple(
        FactPeriod(start=None, end=date.fromisoformat(item), basis=PeriodBasis.ANNUAL)
        for item in payload.periods
    )


def _note_refresh_disabled(state: _RunState) -> None:
    state.note_problem(SOURCE_ONLINE_REFRESH_DISABLED)
    for source in state.requested:
        state.mark(source, SOURCE_ONLINE_REFRESH_DISABLED)
        state.skip(source, SOURCE_ONLINE_REFRESH_DISABLED)


def _partial_summary(state: _RunState) -> str:
    if state.problems:
        detail = "; ".join(state.problems[:3])
        if len(state.problems) > 3:
            detail += f"; +{len(state.problems) - 3} more"
        return f"material collection partial: {detail}"
    broken = ", ".join(
        f"{source}={state.status.get(source, 'pending')}" for source in state.requested
    )
    return f"material collection partial: {broken}"


def _subject_metadata(subject: _Subject) -> dict[str, object]:
    identity = subject.identity
    return {
        "listing_id": subject.listing_id,
        "company_id": subject.company_id,
        "market_code": identity.market_code,
        "ticker": identity.ticker,
        "company_key": identity.company_key,
    }


def _response_record(raw: RawPayload) -> dict[str, object]:
    """What one fetched response was (the audit trail when no raw file is kept)."""

    return {
        "name": raw.name,
        "sha256": hashlib.sha256(raw.content).hexdigest(),
        "bytes": len(raw.content),
        "media_type": raw.media_type,
        "http_status": raw.http_status,
        "source_url": raw.source_url,
        "fetched_at": raw.fetched_at.isoformat(),
    }


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _mapping_or_none(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: value[key] for key in value if isinstance(key, str)}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        {key: item[key] for key in item if isinstance(key, str)}
        for item in value
        if isinstance(item, Mapping)
    ]


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _response_map(value: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: _mapping_list(item)
        for key, item in value.items()
        if isinstance(key, str)
    }
