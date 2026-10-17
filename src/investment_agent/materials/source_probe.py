"""Isolated, one-shot preflight/probe for the checked HK/SEC material sources.

This tool is deliberately separate from the online refresh handler. Its normal
mode is a zero-network dry run. A one-shot network run still needs an approved
licence in the fixed controlled policy and a checked pilot identity binding;
technical acceptance, operational enablement, and the global refresh switch
remain untouched and are reported as independent controls.

Probe artifacts are local evidence only. The application's ``archive_capture``
requires a ``Company.id``; this probe creates that identity in a command-local
in-memory SQLite database, never in an application database, then passes the
new work directory as the archive root. The report marks the ID namespace as
isolated, and it exports canonical fact payloads rather than dataset wrappers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from investment_agent.config import Settings
from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
    ControlledIdentity,
    load_pilot_identities,
    source_symbol,
)
from investment_agent.materials.archive import (
    ArchiveCapture,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
    period_snapshot,
)
from investment_agent.materials.contracts import (
    FactPeriod,
    PeriodBasis,
    RawPayload,
    UpstreamRequest,
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.handlers import (
    MIN_INTERVAL,
    USER_AGENT,
    HttpxMaterialFetcher,
    _MaterialDataError,
    _MaterialTransientError,
    _SourcePolicyDeniedError,
    is_informational_problem,
)
from investment_agent.materials.hk_native import HkNativeAdapter
from investment_agent.materials.source_policy import SourceDecision, evaluate_source_policy
from investment_agent.materials.us_sec import SecFactsAdapter
from investment_agent.providers.errors import ProviderDataError, ProviderTransientError

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3
STOP_FILENAME = "STOP"
_PLACEHOLDER_UA = re.compile(
    r"(?i)(example(?:\.[a-z]{2,})?|\.test\b|\.invalid\b|invalid|placeholder|contact\s+in\s+config|your[-_ ]?email|"
    r"you@example|noreply@example|test@example|\bchangeme\b|\bfoo@|\bbar@)"
)
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}(?![\w.-])")
_PROBE_BLOCKING_POLICY_REASONS = frozenset(
    {
        "invalid_policy_schema",
        "unknown_source",
        "identity_not_bound",
        "licensing_unknown",
        "licensing_scope_mismatch",
        "licensing_citation_missing_or_invalid",
    }
)


@dataclass(frozen=True)
class ProbeDependencies:
    """Explicit Python-only test dependencies; never exposed by the CLI."""

    approvals: Mapping[str, object] | None = None
    fetcher_factory: Callable[[str], Any] | None = None
    policy_evaluator: Callable[..., SourceDecision] | None = None
    pilot_list_path: Path = DEFAULT_PILOT_LIST_PATH


class _ProbeStopRequestedError(RuntimeError):
    """The isolated work directory contains its one-shot stop signal."""


def validate_user_agent(value: str) -> str:
    """Require a product/organization identity and a real contact email."""

    user_agent = value.strip()
    if not user_agent or len(user_agent) > 256:
        raise ValueError("user agent must be a non-empty string of at most 256 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
        raise ValueError("user agent must not contain control characters")
    if _PLACEHOLDER_UA.search(user_agent):
        raise ValueError("user agent contains a placeholder contact or example domain")
    if not _EMAIL.search(user_agent):
        raise ValueError("user agent must include a valid contact email address")
    if not re.search(r"[A-Za-z][A-Za-z0-9._ -]{1,48}", user_agent.split("(", 1)[0]):
        raise ValueError("user agent must identify a product or organization")
    return user_agent


def _new_work_dir(path: Path) -> Path:
    raw = Path(path)
    if raw.exists() or raw.is_symlink():
        raise FileExistsError(f"work directory must not already exist: {raw}")
    absolute = raw.absolute()
    for parent_part in (absolute.parent, *absolute.parent.parents):
        if parent_part.is_symlink():
            raise ValueError(f"work directory parent must not contain symlinks: {parent_part}")
    parent = absolute.parent.resolve(strict=True)
    target = parent / raw.name
    if target.exists() or target.is_symlink() or target == parent:
        raise FileExistsError(f"work directory must be a new child directory: {target}")
    target.mkdir(mode=0o700)
    return target


def _adapter(source: str) -> HkNativeAdapter | SecFactsAdapter:
    if source == SOURCE_EASTMONEY_HK:
        return HkNativeAdapter()
    if source == SOURCE_SEC_COMPANYFACTS:
        return SecFactsAdapter()
    raise ValueError(f"unknown source {source!r}; choose one of the controlled HK/SEC sources")


def _settings() -> Settings:
    # model_construct avoids both environment/dotenv loading and the runtime
    # settings/database loader. Probe gates stay explicitly disabled.
    return Settings.model_construct(
        api_key="source-probe-offline-6d0a853bce2a4b65b5412e9e",
        materials_sec_refresh_enabled=False,
        materials_hk_refresh_enabled=False,
    )


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decision(
    source: str,
    identity: ControlledIdentity,
    adapter_version: str,
    dependencies: ProbeDependencies,
) -> SourceDecision:
    evaluator = dependencies.policy_evaluator or evaluate_source_policy
    return evaluator(
        source,
        identity,
        settings=_settings(),
        adapter_version=adapter_version,
        global_enabled=False,
        approvals=dependencies.approvals,
    )


def _licensed(decision: SourceDecision) -> bool:
    return (
        decision.bound
        and decision.licensing == "approved"
        and not _PROBE_BLOCKING_POLICY_REASONS.intersection(decision.reason_codes)
    )


def _decision_issue(identity: ControlledIdentity, decision: SourceDecision) -> list[str]:
    if _licensed(decision):
        return []
    reasons = ",".join(
        reason
        for reason in decision.reason_codes
        if reason in _PROBE_BLOCKING_POLICY_REASONS
    ) or "licensing_not_approved_or_unbound"
    return [f"{identity.company_key}:probe_blocked:{reasons}"]


async def _isolated_company_ids(
    identities: Sequence[ControlledIdentity],
) -> tuple[Any, Any, dict[str, int]]:
    """Create a command-local in-memory DB to satisfy archive_capture IDs."""

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.storage.models.base import Base
    from investment_agent.storage.models.reference import Company, Listing, Market

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        tables = cast(
            list[Table], [Market.__table__, Company.__table__, Listing.__table__]
        )
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    company_ids: dict[str, int] = {}
    markets: set[str] = set()
    async with sessions() as session:
        for identity in identities:
            if identity.market_code not in markets:
                from investment_agent.domain.security import MARKETS

                market = MARKETS[identity.market_code]
                session.add(
                    Market(
                        code=market.code,
                        name=market.name,
                        timezone=market.timezone,
                        currency=market.quote_currency,
                    )
                )
                markets.add(identity.market_code)
            company = Company(canonical_name=identity.company_name)
            session.add(company)
            await session.flush()
            session.add(
                Listing(
                    company_id=company.id,
                    market_code=identity.market_code,
                    ticker=identity.ticker,
                    is_primary=True,
                )
            )
            company_ids[identity.company_key] = company.id
        await session.commit()
    return engine, sessions, company_ids


def _periods(
    source: str, requested: tuple[FactPeriod, ...] | None = None
) -> tuple[FactPeriod, ...]:
    return tuple(requested or ()) if source == SOURCE_EASTMONEY_HK else ()


def parse_period_range(value: str) -> FactPeriod:
    """Parse an explicit ``YYYY-MM-DD/YYYY-MM-DD`` annual-span argument."""

    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError("period must be YYYY-MM-DD/YYYY-MM-DD")
    try:
        start, end = (date.fromisoformat(part) for part in parts)
    except ValueError as exc:
        raise ValueError("period endpoints must be ISO dates in YYYY-MM-DD/YYYY-MM-DD form") from exc
    if start.isoformat() != parts[0] or end.isoformat() != parts[1]:
        raise ValueError("period endpoints must use canonical YYYY-MM-DD form")
    return FactPeriod(start=start, end=end, basis=PeriodBasis.ANNUAL)


def _validated_periods(
    source: str, periods: tuple[FactPeriod, ...] | None
) -> tuple[FactPeriod, ...]:
    if source == SOURCE_SEC_COMPANYFACTS:
        if periods is not None:
            raise ValueError("SEC uses the current adapter's selected annual window; --period is not accepted")
        return ()
    if source != SOURCE_EASTMONEY_HK:
        raise ValueError(f"unknown source {source!r}")
    spans = tuple(periods or ())
    if not spans:
        return ()
    if len(spans) != 4:
        raise ValueError("HK requires exactly four consecutive complete annual spans")
    for index, span in enumerate(spans):
        if (
            span.basis is not PeriodBasis.ANNUAL
            or span.start is None
            or span.start != date(span.end.year, 1, 1)
            or span.end != date(span.end.year, 12, 31)
            or (index > 0 and span.end.year != spans[index - 1].end.year + 1)
        ):
            raise ValueError("HK requires four consecutive complete Jan 1–Dec 31 annual spans")
    return spans


def _stop_requested(work_dir: Path) -> bool:
    signal = work_dir / STOP_FILENAME
    return signal.exists() or signal.is_symlink()


async def _fetch_one(
    fetcher: HttpxMaterialFetcher,
    request: UpstreamRequest,
    *,
    identity: ControlledIdentity,
    user_agent: str,
    work_dir: Path,
    before_send: Callable[[UpstreamRequest], Awaitable[object]],
    ledger: list[dict[str, object]],
    responses: list[ResponseEvidence],
    stop_source: list[bool],
) -> RawPayload | None:
    del user_agent  # the fetcher's configured UA is authoritative and recorded below
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if _stop_requested(work_dir):
            stop_source[0] = True
            return None
        started = datetime.now(UTC)
        monotonic_started = time.monotonic()
        row: dict[str, object] = {
            "name": request.name,
            "status": "started",
            "started_at": started.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "retry_attempt": attempt,
            "request_sent": False,
            "http_status": None,
            "user_agent": fetcher.user_agent,
            # HttpxMaterialFetcher does not expose limiter wait; unknown is kept null.
            "limiter_wait_seconds": None,
            "response_sha256": None,
        }
        ledger.append(row)
        fetcher.before_send = before_send
        try:
            raw = await fetcher.fetch(request, identity=identity)
        except _SourcePolicyDeniedError:
            ledger.remove(row)
            return None
        except _ProbeStopRequestedError:
            ledger.remove(row)
            stop_source[0] = True
            return None
        except (_MaterialTransientError, _MaterialDataError) as exc:
            raw = exc.raw_payload
            row["request_sent"] = True
            response_name = f"{request.name}__attempt_{attempt}"
            responses.append(
                ResponseEvidence(
                    name=response_name,
                    content=raw.content,
                    source_url=raw.source_url,
                    media_type=raw.media_type,
                    http_status=raw.http_status,
                    fetched_at=raw.fetched_at,
                    parse_eligible=False,
                )
            )
            row.update(
                status=("http_2xx" if 200 <= raw.http_status < 300 else f"http_{raw.http_status}"),
                http_status=raw.http_status,
                response_sha256=_sha256(raw.content),
                response_archive_name=response_name,
                error_code=getattr(exc, "error_code", "provider_error"),
            )
            if raw.http_status in {401, 403, 429}:
                stop_source[0] = True
                row["status"] = f"terminal_http_{raw.http_status}_stop_source"
                break
            if isinstance(exc, _MaterialTransientError) and attempt < MAX_ATTEMPTS:
                row["status"] = f"retry_http_{raw.http_status}"
            else:
                break
        except ProviderTransientError as exc:
            row["request_sent"] = True
            row.update(status="transport_error", error_code=getattr(exc, "error_code", "transport"))
            if attempt < MAX_ATTEMPTS:
                row["status"] = "retry_transport_error"
            else:
                break
        except ProviderDataError as exc:
            row["request_sent"] = True
            row.update(status="data_error", error_code=getattr(exc, "error_code", "data_error"))
            break
        else:
            row["request_sent"] = True
            response_name = f"{request.name}__attempt_{attempt}"
            responses.append(
                ResponseEvidence(
                    name=response_name,
                    content=raw.content,
                    source_url=raw.source_url,
                    media_type=raw.media_type,
                    http_status=raw.http_status,
                    fetched_at=raw.fetched_at,
                    parse_eligible=True,
                )
            )
            row.update(
                status=("http_2xx" if 200 <= raw.http_status < 300 else f"http_{raw.http_status}"),
                http_status=raw.http_status,
                response_sha256=_sha256(raw.content),
                response_archive_name=response_name,
            )
            return raw
        finally:
            finished = datetime.now(UTC)
            row["finished_at"] = finished.isoformat()
            row["duration_seconds"] = round(time.monotonic() - monotonic_started, 6)
    return None


async def _run_real(
    *,
    source: str,
    identities: Sequence[ControlledIdentity],
    adapter_version: str,
    user_agent: str,
    work_dir: Path,
    dependencies: ProbeDependencies,
    periods: tuple[FactPeriod, ...],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
    list[dict[str, object]],
    list[str],
]:
    if any(
        not _licensed(_decision(source, identity, adapter_version, dependencies))
        for identity in identities
    ):
        return [], [], [], [], []
    if _stop_requested(work_dir):
        return [], [], ["stop_signal_before_request:source_stopped"], [], []
    engine, _, company_ids = await _isolated_company_ids(identities)
    try:
        return await _run_real_collect(
            source=source,
            identities=identities,
            adapter_version=adapter_version,
            user_agent=user_agent,
            work_dir=work_dir,
            dependencies=dependencies,
            company_ids=company_ids,
            periods=periods,
        )
    finally:
        await engine.dispose()


async def _run_real_collect(
    *,
    source: str,
    identities: Sequence[ControlledIdentity],
    adapter_version: str,
    user_agent: str,
    work_dir: Path,
    dependencies: ProbeDependencies,
    company_ids: Mapping[str, int],
    periods: tuple[FactPeriod, ...],
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
    list[dict[str, object]],
    list[str],
]:
    ledger: list[dict[str, object]] = []
    archives: list[dict[str, object]] = []
    parsed_facts: list[dict[str, object]] = []
    issues: list[str] = []
    informational: list[str] = []
    adapter_factory = _adapter
    terminate_source = False
    fetcher = (
        HttpxMaterialFetcher(user_agent=user_agent)
        if dependencies.fetcher_factory is None
        else dependencies.fetcher_factory(user_agent)
    )
    for identity in identities:
        if terminate_source:
            break
        if _stop_requested(work_dir):
            issues.append("stop_signal_before_request:source_stopped")
            break
        decision = _decision(source, identity, adapter_version, dependencies)
        if not _licensed(decision):
            issues.extend(_decision_issue(identity, decision))
            continue
        adapter = adapter_factory(source)
        raw_by_name: dict[str, RawPayload] = {}
        responses: list[ResponseEvidence] = []
        request_names: list[str] = []
        attempted_names: set[str] = set()
        period_list = _periods(source, periods)
        blocked = False
        stop_source = [False]

        async def ensure_policy(
            _: UpstreamRequest, checked_identity: ControlledIdentity = identity
        ) -> None:
            nonlocal blocked
            if _stop_requested(work_dir):
                blocked = True
                issue = "stop_signal_after_limiter_wait:source_stopped"
                if issue not in issues:
                    issues.append(issue)
                raise _ProbeStopRequestedError(issue)
            latest = _decision(source, checked_identity, adapter_version, dependencies)
            if not _licensed(latest):
                blocked = True
                issue = f"{checked_identity.company_key}:source_policy_closed_before_send"
                if issue not in issues:
                    issues.append(issue)
                raise _SourcePolicyDeniedError(
                    f"{checked_identity.company_key}: source licence/binding closed before send"
                )

        current_requests = adapter.requests(identity, periods=period_list)
        # SEC is two-pass: parse CompanyFacts + Submissions, then fetch every
        # shard named by Submissions. HK has exactly the three checked tables.
        for pass_index in range(2):
            for request in current_requests:
                request_names.append(request.name)
                attempted_names.add(request.name)
                latest = _decision(source, identity, adapter_version, dependencies)
                if not _licensed(latest):
                    issues.extend(_decision_issue(identity, latest))
                    blocked = True
                    break
                ledger_start = len(ledger)
                fetched = await _fetch_one(
                    fetcher,
                    request,
                    identity=identity,
                    user_agent=user_agent,
                    work_dir=work_dir,
                    before_send=ensure_policy,
                    ledger=ledger,
                    responses=responses,
                    stop_source=stop_source,
                )
                for request_row in ledger[ledger_start:]:
                    request_row["company_key"] = identity.company_key
                if fetched is not None:
                    raw_by_name[request.name] = fetched
                elif blocked:
                    break
                if stop_source[0]:
                    issues.append(
                        f"{identity.company_key}:terminal_http_status_stopped_source"
                    )
                    terminate_source = True
                    break
            if blocked:
                terminate_source = True
            if blocked or terminate_source:
                break
            parsed = adapter.parse(
                identity,
                raw=tuple(raw_by_name.values()),
                periods=period_list,
                subject_id=company_ids[identity.company_key],
            )
            next_requests = adapter.requests(identity, periods=period_list)
            missing = [
                req for req in next_requests
                if req.name not in raw_by_name and req.name not in attempted_names
            ]
            if not missing or pass_index == 1:
                for problem in parsed.problems:
                    if is_informational_problem(problem):
                        informational.append(f"{identity.company_key}:{problem}")
                    else:
                        issues.append(f"{identity.company_key}:{problem}")
                parsed_facts.append(
                    {
                        "company_key": identity.company_key,
                        "facts": [
                            fact_to_payload(fact_from_payload(entry))
                            for draft in parsed.drafts
                            for entry in draft.payload.get("facts", [])
                        ],
                    }
                )
                break
            current_requests = tuple(missing)

        # One manifest per company preserves failed response bodies and hashes
        # every successful body. The report points to the manifest digest.
        company_problems = [issue for issue in issues if issue.startswith(f"{identity.company_key}:")]
        company_problems.extend(
            issue for issue in informational if issue.startswith(f"{identity.company_key}:")
        )
        try:
            archived = archive_capture(
                work_dir,
                ArchiveCapture(
                    identity=identity_snapshot(identity),
                    source=source,
                    subject_id=company_ids[identity.company_key],
                    adapter_version=adapter_version,
                    periods=tuple(period_snapshot(item) for item in period_list),
                    expected_requests=tuple(dict.fromkeys(request_names)),
                    responses=tuple(responses),
                    problems=tuple(company_problems),
                    captured_at=datetime.now(UTC),
                ),
            )
            ref, manifest_hash = archived.manifest_ref, archived.manifest_sha256
        except OSError as exc:
            issues.append(f"{identity.company_key}:archive_write_failed:{type(exc).__name__}")
            continue
        archives.append(
            {"company_key": identity.company_key, "manifest_ref": ref, "manifest_sha256": manifest_hash}
        )
    return ledger, archives, issues, parsed_facts, informational


def run_source_probe(
    *,
    source: str,
    work_dir: Path,
    user_agent: str | None = None,
    allow_network: bool = False,
    periods: tuple[FactPeriod, ...] | None = None,
    dependencies: ProbeDependencies | None = None,
) -> dict[str, object]:
    """Run a dry preflight by default, or one isolated, licence-gated probe.

    Test-only dependency injection always makes the report ``simulation``.
    The CLI does not expose injection and therefore cannot label mocked traffic
    as a real network run.
    """

    ua_candidate = USER_AGENT if user_agent is None else user_agent
    ua_issue: str | None = None
    try:
        ua = validate_user_agent(ua_candidate)
    except ValueError as exc:
        ua = ua_candidate.strip()
        ua_issue = f"user_agent_invalid:{exc}"
    adapter = _adapter(source)
    explicit_periods = _validated_periods(source, periods)
    hk_window_missing = source == SOURCE_EASTMONEY_HK and not explicit_periods
    active_dependencies = dependencies or ProbeDependencies()
    simulated = dependencies is not None
    output_dir = _new_work_dir(Path(work_dir))
    identities = tuple(
        item
        for item in load_pilot_identities(active_dependencies.pilot_list_path)
        if source_symbol(item, source) is not None
    )
    if not identities:
        raise ValueError(f"controlled pilot list has no identities bound to {source!r}")
    if hk_window_missing:
        from investment_agent.materials.hk_native import REPORTS

        planned_requests = [
            {"company_key": identity.company_key, "request_names": list(REPORTS)}
            for identity in identities
        ]
    else:
        planned_requests = [
            {
                "company_key": identity.company_key,
                "request_names": [
                    request.name
                    for request in _adapter(source).requests(
                        identity, periods=_periods(source, explicit_periods)
                    )
                ],
            }
            for identity in identities
        ]

    issues: list[str] = []
    if ua_issue is not None:
        issues.append(ua_issue)
    decisions: dict[str, dict[str, object]] = {}
    for identity in identities:
        decision = _decision(source, identity, adapter.adapter_version, active_dependencies)
        decisions[identity.company_key] = decision.as_dict()
        issues.extend(_decision_issue(identity, decision))
    if hk_window_missing:
        issues.append("hk_annual_spans_required:provide_four_consecutive_complete_years")
    started = time.monotonic()
    mode = "simulation" if simulated else "real" if allow_network else "dry_run"
    ledger: list[dict[str, object]] = []
    archives: list[dict[str, object]] = []
    facts: list[dict[str, object]] = []
    informational: list[str] = []
    if (
        allow_network
        and not hk_window_missing
        and ua_issue is None
        and not (simulated and active_dependencies.fetcher_factory is None)
    ):
        ledger, archives, run_issues, facts, informational = asyncio.run(
            _run_real(
                source=source,
                identities=identities,
                adapter_version=adapter.adapter_version,
                user_agent=ua,
                work_dir=output_dir,
                dependencies=active_dependencies,
                periods=explicit_periods,
            )
        )
        issues.extend(run_issues)

    duration = round(time.monotonic() - started, 6)
    requests_succeeded = sum(
        1 for row in ledger if row.get("status") == "http_2xx"
    )
    requests_total = len(ledger)
    requests_failed = requests_total - requests_succeeded
    if not allow_network:
        status = "dry_run"
    elif ua_issue is not None:
        status = "blocked"
    elif simulated and active_dependencies.fetcher_factory is None:
        status = "blocked"
        issues.append("simulation_fetcher_required:injected_dependencies_cannot_fall_back_to_network")
    elif hk_window_missing or not requests_total:
        status = "blocked"
    elif issues or requests_failed:
        status = "failed"
    else:
        status = "passed"
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "adapter_version": adapter.adapter_version,
        "mode": mode,
        "observed_at": datetime.now(UTC).isoformat(),
        "company_keys": sorted(
            {str(row["company_key"]) for row in ledger if isinstance(row.get("company_key"), str)}
        ),
        "planned_company_keys": [item.company_key for item in identities],
        "planned_requests": planned_requests,
        "requests_total": requests_total,
        "requests_succeeded": requests_succeeded,
        "requests_failed": requests_failed,
        "duration_seconds": duration,
        "user_agent": ua,
        "min_interval_seconds": float(MIN_INTERVAL[source]),
        "archives": archives,
        "status": status,
        "issues": sorted(set(issues)),
        "informational": sorted(set(informational)),
        "requests": ledger,
        "parsed_facts": facts,
        "periods": [period_snapshot(item) for item in explicit_periods],
        "period_selection": (
            "adapter_default_latest_fiscal_years"
            if source == SOURCE_SEC_COMPANYFACTS
            else "explicit_complete_hk_annual_spans"
        ),
        "subject_namespace": "isolated_probe_memory",
        "passed_scope": "single_probe_transport_and_parse_only",
        "passed_implies_technical_or_licensing_acceptance": False,
        "source_decisions": decisions,
        "operational_refresh_enabled": False,
        "global_refresh_enabled": False,
        "single_process_serial_exclusive_required": True,
        "multi_process_global_rate_limit_verified": False,
        "technical_acceptance_reviewed": False,
        "annual_comparison_reviewed": False,
        "recovery_drill_reviewed": False,
        "preflight_notes": [
            "Run one probe process at a time; the in-process limiter cannot prove a multi-machine global limit.",
            "Before real use, verify the source's rate-limit report and policy scope.",
            "Annual comparison and recovery-drill review remain unverified.",
        ],
    }
    _write_exclusive(output_dir / "report.json", _json_bytes(report))
    return report


__all__ = ["ProbeDependencies", "run_source_probe", "validate_user_agent"]
