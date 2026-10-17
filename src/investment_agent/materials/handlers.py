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
   through the rate-limited, user-agent-carrying fetcher, archive every source
   response, parse, and store every draft through ``MaterialStore``;
5. commit after every source and record a checkpoint, so a retried attempt
   resumes instead of redoing a source that already finished;
6. structural parse problems (a changed upstream shape, a truncated page) are
   reported per source and never retried blindly; informational counters
   (``unmapped_items:`` and friends) go to the run metadata's
   ``informational`` list and leave the outcome untouched;
7. transient network failures keep the retry budget: received responses and
   their acquisition record are checkpointed first, then the
   :class:`ProviderTransientError` is re-raised;
8. the run is ``completed`` only when every requested source was stored
   cleanly; anything else is ``partial`` and names its reason in the metadata.

The handler never invents data. Adapters build the drafts (including the real
period spans), the store is the single write path, and a source that is not
bound, not covered or not licensed simply produces no material.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
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
)
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.materials.archive import (
    ArchiveCapture,
    DatasetLink,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
    period_snapshot,
    record_acquisition,
    sanitize_url,
)
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
from investment_agent.materials.source_policy import (
    SourceDecision,
    evaluate_source_policy,
)
from investment_agent.materials.store import MaterialStore, SavedDataset, coalesce_annual_tables
from investment_agent.providers.errors import ProviderDataError, ProviderTransientError
from investment_agent.snapshots.throttle import AsyncRateLimiter
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

ONLINE_REFRESH_ENABLED = False
"""Global safety gate, independent of per-source approval and ops settings.

Production remains offline. Source policy must additionally approve the issuer
scope, adapter version, licensing and technical evidence at every request.
Ordinary runtime settings cannot change this gate or grant approval.
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

MAX_RETRY_AFTER_SECONDS = 5.0
"""Maximum time spent honoring one server-provided 429 delay."""

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
SOURCE_TRANSIENT_ERROR = "transient_error"
SOURCE_NOT_BOUND = "source_not_bound"
SOURCE_ADAPTER_VERSION_MISMATCH = "adapter_version_mismatch"
SOURCE_ONLINE_REFRESH_DISABLED = "online_refresh_disabled"
SOURCE_POLICY_DISABLED = "source_policy_disabled"
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
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self._min_interval = dict(MIN_INTERVAL if min_interval is None else min_interval)
        self._client = client
        self._sleep = sleep
        self._limiters: dict[str, AsyncRateLimiter] = {}

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload:
        del identity  # the request already carries the source-specific code
        await self.limiter_for(request).acquire()
        # The handler installs this guard for the active source/run. Keeping
        # it after the limiter means a policy change during rate-limit waiting
        # blocks the request before it leaves the process.
        if self.before_send is not None:
            await self.before_send(request)
        if self._client is None:
            async with httpx.AsyncClient(
                timeout=self.timeout, headers=self._headers()
            ) as client:
                response = await self._send(client, request)
        else:
            response = await self._send(self._client, request)
        raw = self._payload(request, response)
        status = response.status_code
        if status in {408, 429} or status >= 500:
            if status == 429:
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                if delay > 0:
                    await self._sleep(min(delay, MAX_RETRY_AFTER_SECONDS))
            raise _MaterialTransientError(
                f"{request.name}: upstream HTTP {status}",
                provider=self._provider(request),
                error_code=f"http_{status}",
                raw_payload=raw,
                parse_eligible=False,
            )
        if status >= 400:
            raise _MaterialDataError(
                f"{request.name}: upstream HTTP {status}",
                provider=self._provider(request),
                error_code=f"http_{status}",
                raw_payload=raw,
                parse_eligible=False,
            )
        if request.media_type == "application/json":
            try:
                json.loads(raw.content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise _MaterialDataError(
                    f"{request.name}: the response body is not JSON: {exc}",
                    provider=self._provider(request),
                    error_code="not_json",
                    raw_payload=raw,
                    parse_eligible=False,
                ) from exc
        return raw

    def limiter_for(self, request: UpstreamRequest) -> AsyncRateLimiter:
        """The limiter of ``request``'s upstream host (one per host, shared)."""

        key = urlparse(request.url).netloc
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = AsyncRateLimiter(rate_per_minute=60.0 / self.min_interval_for(request))
            self._limiters[key] = limiter
        return limiter

    before_send: Callable[[UpstreamRequest], Awaitable[object]] | None = None

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
                request.url, params=list(request.params), headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                f"{request.name}: {type(exc).__name__}: {exc}",
                provider=provider,
                error_code="transport",
            ) from exc
        return response

    def _payload(self, request: UpstreamRequest, response: httpx.Response) -> RawPayload:
        content = response.content
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


class _MaterialTransientError(ProviderTransientError):
    """Retryable material error that retains the response body when present."""

    def __init__(
        self, message: str, *, provider: str, error_code: str,
        raw_payload: RawPayload, parse_eligible: bool,
    ):
        super().__init__(message, provider=provider, error_code=error_code)
        self.raw_payload = raw_payload
        self.parse_eligible = parse_eligible


class _MaterialDataError(ProviderDataError):
    """Terminal material error that retains the response body for archiving."""

    def __init__(
        self, message: str, *, provider: str, error_code: str,
        raw_payload: RawPayload, parse_eligible: bool,
    ):
        super().__init__(message, provider=provider, error_code=error_code)
        self.raw_payload = raw_payload
        self.parse_eligible = parse_eligible


class _SourcePolicyDeniedError(RuntimeError):
    """A runtime policy change closed a source before its next request."""


class _IdentityCompanyMismatchError(RuntimeError):
    """The listing's DB company does not match its controlled identity."""


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


def _retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float:
    """Parse either Retry-After form; malformed or past values mean no wait."""

    if not value:
        return 0.0
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return max(0.0, (target.astimezone(UTC) - current).total_seconds())


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
        policy_evaluator: Callable[..., SourceDecision] = evaluate_source_policy,
        approvals: Mapping[str, object] | None = None,
    ) -> None:
        self.settings = settings
        self.fetcher: MaterialFetcher = HttpxMaterialFetcher() if fetcher is None else fetcher
        self.pilot_list_path = Path(pilot_list_path)
        self.clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self.policy_evaluator = policy_evaluator
        self.approvals = approvals
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
            try:
                subject = await self._resolve_subject(session, payload.listing_id)
            except _IdentityCompanyMismatchError as exc:
                state.listing_status = "identity_company_mismatch"
                state.note_problem(str(exc))
                await self._finish_partial(
                    session, repository, job, state,
                    summary="identity_company_mismatch",
                )
                return "partial"
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
            store = MaterialStore(session, data_dir=self.settings.data_dir)
            adapters = self._adapter_map()
            transient: ProviderTransientError | None = None
            for source in payload.sources:
                if state.is_final(source):
                    # A source that finished in an earlier attempt is not redone.
                    state.skip(source, SKIPPED_ALREADY_CHECKED)
                    continue
                try:
                    await self._run_source(
                        session=session,
                        state=state,
                        source=source,
                        subject=subject,
                        payload=payload,
                        adapters=adapters,
                        store=store,
                    )
                except _SourcePolicyDeniedError:
                    await self._checkpoint(session, repository, job, state)
                    continue
                except ProviderTransientError as exc:
                    # A retry must not lose already received responses from a
                    # partially completed source attempt.
                    await self._checkpoint(session, repository, job, state)
                    transient = transient or exc
                    continue
                await self._checkpoint(session, repository, job, state)
            if transient is not None:
                # Other permitted sources have had their turn and their state
                # is durable. Let the existing worker spend its retry budget.
                raise transient
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
        session: AsyncSession,
        state: _RunState,
        source: str,
        subject: _Subject,
        payload: MaterialCollectionPayload,
        adapters: Mapping[str, MaterialSourceAdapter],
        store: MaterialStore,
    ) -> None:
        """Fetch, parse and store one source; a bad answer never raises."""

        adapter = adapters.get(source)
        if adapter is None:
            # A wiring defect, not a data problem: no adapter may silently
            # stand in for a requested source.
            raise ValueError(f"no material adapter is wired for source {source!r}")
        declared = payload.adapter_versions.get(source)
        if declared != adapter.adapter_version:
            decision = await self._record_source_decision(
                session=session, source=source, adapter=adapter, subject=subject,
                state=state,
            )
            state.mark(source, SOURCE_ADAPTER_VERSION_MISMATCH)
            state.note_problem(
                f"{source}: adapter_version_missing_or_mismatch: payload {declared!r} != "
                f"adapter {adapter.adapter_version!r}"
            )
            record = decision.as_dict()
            record.update({
                "enabled": False,
                "reason": "payload_adapter_version_missing_or_mismatch",
                "reason_codes": [
                    *record["reason_codes"],
                    "payload_adapter_version_missing_or_mismatch",
                ],
                "payload_adapter_version": declared,
                "adapter_version": adapter.adapter_version,
            })
            state.source_decisions[source] = record
            return
        state.begin_source(source)
        try:
            await self._require_source_enabled(
                session=session, source=source, adapter=adapter, subject=subject,
                state=state,
            )
        except _SourcePolicyDeniedError:
            return
        fetcher_before_send = getattr(self.fetcher, "before_send", None)
        if hasattr(self.fetcher, "before_send"):
            async def before_send(_request: UpstreamRequest) -> None:
                if fetcher_before_send is not None:
                    await fetcher_before_send(_request)
                await self._require_source_enabled(
                    session=session, source=source, adapter=adapter, subject=subject,
                    state=state,
                )
            self.fetcher.before_send = before_send
        try:
            drafts, problems, raw = await self._fetch_and_parse(
                source=source,
                adapter=adapter,
                subject=subject,
                session=session,
                periods=_requested_periods(payload),
                state=state,
            )
        except _SourcePolicyDeniedError as exc:
            await self._archive_source(
                session=session, state=state, source=source, subject=subject,
                adapter=adapter, periods=_requested_periods(payload),
                problems=(f"{source}: policy_changed_during_run: {exc}",),
            )
            return
        except ProviderTransientError as exc:
            if hasattr(self.fetcher, "before_send"):
                self.fetcher.before_send = fetcher_before_send
            carried = getattr(exc, "raw_payload", None)
            if isinstance(carried, RawPayload):
                state.record_payload(source, carried)
                state.mark_parse_ineligible(source, carried.name)
                state.record_responses(source, state.payloads[source])
            state.mark(source, SOURCE_TRANSIENT_ERROR)
            state.note_problem(
                f"{source}: transient_error: {exc.error_code or type(exc).__name__}"
            )
            await self._archive_source(
                session=session,
                state=state,
                source=source,
                subject=subject,
                adapter=adapter,
                periods=_requested_periods(payload),
                problems=(f"{source}: transient_error: {exc.error_code or type(exc).__name__}",),
            )
            # The caller's checkpoint keeps what already finished; the worker
            # decides whether the retry budget allows another attempt.
            raise
        except ProviderDataError as exc:
            if hasattr(self.fetcher, "before_send"):
                self.fetcher.before_send = fetcher_before_send
            carried = getattr(exc, "raw_payload", None)
            if isinstance(carried, RawPayload):
                state.record_payload(source, carried)
                state.mark_parse_ineligible(source, carried.name)
                state.record_responses(source, state.payloads[source])
            await self._archive_source(
                session=session,
                state=state,
                source=source,
                subject=subject,
                adapter=adapter,
                periods=_requested_periods(payload),
                problems=(f"{source}: data_error: {exc.error_code or type(exc).__name__}",),
            )
            state.mark(source, SOURCE_DATA_ERROR)
            state.note_problem(f"{source}: data_error: {exc.error_code or type(exc).__name__}")
            return
        finally:
            if hasattr(self.fetcher, "before_send"):
                self.fetcher.before_send = fetcher_before_send

        structural = [problem for problem in problems if not is_informational_problem(problem)]
        capture_problems = list(problems)
        for problem in problems:
            if is_informational_problem(problem):
                state.note_informational(f"{source}: {problem}")
            else:
                state.note_problem(f"{source}: {problem}")
        saved: list[SavedDataset] = []
        if drafts:
            storeable = _attach_raw(coalesce_annual_tables(drafts), raw)
            saved = [await store.save(draft) for draft in storeable]
            state.record_saved(source, saved)
        elif not structural:
            no_dataset = f"{source}: no_dataset: adapter produced no dataset or problem"
            state.note_problem(no_dataset)
            capture_problems.append(no_dataset)
        state.mark(source, SOURCE_STRUCTURAL_FAILURE if structural else SOURCE_SAVED)
        await self._archive_source(
            session=session,
            state=state,
            source=source,
            subject=subject,
            adapter=adapter,
            periods=_requested_periods(payload),
            problems=tuple(capture_problems),
            datasets=saved,
        )

    async def _archive_source(
        self,
        *,
        session: AsyncSession,
        state: _RunState,
        source: str,
        subject: _Subject,
        adapter: MaterialSourceAdapter,
        periods: Sequence[FactPeriod],
        problems: Sequence[str],
        datasets: Sequence[SavedDataset] = (),
    ) -> None:
        """Persist the immutable capture before the source checkpoint commits."""

        archived = archive_capture(
            self.settings.data_dir,
            ArchiveCapture(
                identity=identity_snapshot(subject.identity),
                subject_id=subject.company_id,
                source=source,
                adapter_version=adapter.adapter_version,
                periods=tuple(period_snapshot(period) for period in periods),
                expected_requests=tuple(state.expected_requests.get(source, ())),
                responses=tuple(
                    ResponseEvidence(
                        name=raw.name,
                        content=raw.content,
                        source_url=raw.source_url,
                        media_type=raw.media_type,
                        http_status=raw.http_status,
                        fetched_at=raw.fetched_at,
                        parse_eligible=raw.name not in state.parse_ineligible.get(source, set()),
                    )
                    for raw in state.payloads.get(source, ())
                ),
                problems=tuple(problems),
            ),
        )
        record = await record_acquisition(
            session,
            archived,
            datasets=tuple(
                DatasetLink(item.id, item.revision, item.content_hash) for item in datasets
            ),
        )
        state.record_acquisition(source, record.id, archived.manifest_ref, archived.manifest_sha256)

    async def _fetch_and_parse(
        self,
        *,
        source: str,
        adapter: MaterialSourceAdapter,
        subject: _Subject,
        session: AsyncSession,
        periods: Sequence[FactPeriod],
        state: _RunState,
    ) -> tuple[tuple[DatasetDraft, ...], tuple[str, ...], tuple[RawPayload, ...]]:
        """Fetch every request the adapter asks for, then parse the responses.

        An adapter may name further documents only after a parse (the SEC
        submissions shards), so this asks again after each round. Every pending
        request name is recorded before fetching; captured response bytes are
        kept for the source acquisition even when several responses have no
        single-dataset attribution.
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
                state.record_expected_request(source, request.name)
            for request in pending:
                await self._require_source_enabled(
                    session=session, source=source, adapter=adapter, subject=subject,
                    state=state,
                )
                fetched = await self.fetcher.fetch(request, identity=subject.identity)
                seen.add(fetched.name)
                raw.append(fetched)
                state.record_payload(source, fetched)
                state.record_responses(source, raw)
            result = adapter.parse(
                subject.identity,
                raw=tuple(raw),
                periods=periods,
                subject_id=subject.company_id,
            )
        remaining = tuple(
            request
            for request in adapter.requests(subject.identity, periods=periods)
            if request.name not in seen
        )
        for request in remaining:
            state.record_expected_request(source, request.name)
        if result is None:
            # Nothing was requested (or only already-seen documents): the
            # adapter's own parse is still what reports the problem.
            result = adapter.parse(
                subject.identity,
                raw=tuple(raw),
                periods=periods,
                subject_id=subject.company_id,
            )
        if remaining:
            result = replace(
                result,
                problems=(
                    *result.problems,
                    "request_limit_exceeded: adapter named additional requests beyond the pass limit",
                ),
            )
        return result.drafts, result.problems, tuple(raw)

    async def _require_source_enabled(
        self,
        *,
        session: AsyncSession,
        source: str,
        adapter: MaterialSourceAdapter,
        subject: _Subject,
        state: _RunState,
    ) -> None:
        """Reload effective settings and evaluate every gate before a request."""

        decision = await self._record_source_decision(
            session=session, source=source, adapter=adapter, subject=subject,
            state=state,
        )
        if decision.enabled:
            return
        reason = decision.reason or SOURCE_POLICY_DISABLED
        state.mark(source, SOURCE_POLICY_DISABLED)
        state.note_problem(f"{source}: {reason}")
        state.skip(source, reason)
        raise _SourcePolicyDeniedError(reason)

    async def _record_source_decision(
        self,
        *,
        session: AsyncSession,
        source: str,
        adapter: MaterialSourceAdapter,
        subject: _Subject,
        state: _RunState,
    ) -> SourceDecision:
        from investment_agent.settings.loader import load_settings

        effective = await load_settings(session, base=self.settings)
        decision = self.policy_evaluator(
            source,
            subject.identity,
            settings=effective,
            adapter_version=adapter.adapter_version,
            global_enabled=ONLINE_REFRESH_ENABLED,
            approvals=self.approvals,
        )
        state.source_decisions[source] = decision.as_dict()
        return decision

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
        if company.canonical_name != identity.company_name:
            raise _IdentityCompanyMismatchError(
                f"identity_company_mismatch: listing {listing.id} resolves to "
                f"{company.canonical_name!r}, while controlled identity "
                f"{identity.company_key!r} names {identity.company_name!r}"
            )
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
        self.acquisitions = _mapping_list(previous.get("acquisitions"))
        self.expected_requests: dict[str, list[str]] = {}
        self.payloads: dict[str, list[RawPayload]] = {}
        self.parse_ineligible: dict[str, set[str]] = {}
        self.problems = _string_list(previous.get("problems"))
        self.informational = _string_list(previous.get("informational"))
        source_decisions = previous.get("source_decisions")
        self.source_decisions: dict[str, dict[str, object]] = (
            {
                key: dict(value)
                for key, value in source_decisions.items()
                if isinstance(key, str) and isinstance(value, Mapping)
            }
            if isinstance(source_decisions, Mapping)
            else {}
        )

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

    def begin_source(self, source: str) -> None:
        self.problems = [
            problem for problem in self.problems
            if not problem.startswith(f"{source}: transient_error:")
        ]
        self.expected_requests[source] = []
        self.payloads[source] = []
        self.parse_ineligible[source] = set()

    def record_expected_request(self, source: str, name: str) -> None:
        names = self.expected_requests.setdefault(source, [])
        if name not in names:
            names.append(name)

    def record_payload(self, source: str, raw: RawPayload) -> None:
        payloads = self.payloads.setdefault(source, [])
        if all(item.name != raw.name for item in payloads):
            payloads.append(raw)

    def mark_parse_ineligible(self, source: str, name: str) -> None:
        self.parse_ineligible.setdefault(source, set()).add(name)

    def record_acquisition(
        self, source: str, acquisition_id: int, manifest_ref: str, manifest_sha256: str
    ) -> None:
        row = {
            "source": source,
            "acquisition_id": acquisition_id,
            "manifest_ref": manifest_ref,
            "manifest_sha256": manifest_sha256,
        }
        if row not in self.acquisitions:
            self.acquisitions.append(row)

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
            "source_decisions": {
                source: self.source_decisions[source]
                for source in self.requested if source in self.source_decisions
            },
            "saved": self.saved,
            "skipped": self.skipped,
            "responses": self.responses,
            "acquisitions": self.acquisitions,
            "problems": self.problems,
            "informational": self.informational,
        }


# --- helpers ---------------------------------------------------------------


def _attach_raw(
    drafts: Sequence[DatasetDraft], raw: Sequence[RawPayload]
) -> tuple[DatasetDraft, ...]:
    """Attach the response a single-response source produced to every draft.

    The store may keep a single-response artifact on a dataset for compatibility.
    Every response, including multi-response sets and failures, is separately
    retained by the source acquisition archive. ``source_fetched_at`` stays
    untouched: this path's archive records each real response fetch time.
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

    The refresh API rejects explicit HK ends before enqueue. An older or
    internally enqueued payload still reaches the adapter's unsupported_period
    fallback, never a fabricated January 1st start.
    """

    return tuple(
        FactPeriod(start=None, end=date.fromisoformat(item), basis=PeriodBasis.ANNUAL)
        for item in payload.periods
    )


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
        "source_url": sanitize_url(raw.source_url),
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
