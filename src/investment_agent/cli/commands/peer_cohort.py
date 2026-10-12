"""``ia peers``: offline administration of the industry & peer reference (WP2).

First command: import the fixed run-02 baijiu roster (``sample.json``) as one
immutable :class:`~investment_agent.storage.models.peer.PeerCohortSnapshot`
(``cohort_key=baijiu_current``). The import is strictly offline: identity
resolution reads the existing company/listing directory only, the roster is
never guessed from names, and one unresolvable code stops the whole batch
before anything is written.

Snapshot content (design §名单快照; payload JSON schema ``peer_cohort_snapshot/1``):

* ``classification`` - the Shenwan 2021 system and baijiu code ``340501``;
  per-company classification evidence (start/update times) is *not provided*
  by the sample file and stays explicitly marked as such.
* ``index_code`` ``851251.SI`` - fixed by the technical design; the sample
  file itself does not carry the index code (recorded in ``index_code_source``).
* ``source`` - offline file locator (repo-relative when inside the checkout),
  the file's sha256, the source ``captured_at`` (roster observation, never an
  official constituent effective date) and the file's own membership semantics
  / selection wording.
* ``entries`` - one per roster company: resolved ``company_id`` and
  representative A-share ``listing_id``, ticker, directory name (with its
  source label), classification-list membership, and explicit
  ``not_provided_by_source`` markers for listing status and comparability
  flags (ST / mixed business) the sample file does not carry.
* ``included`` / ``excluded`` - the first phase includes all roster members;
  classification-only differences are recorded as excluded with the reason the
  source supports, without inventing further justification.

Versioning and idempotency: ``content_hash`` is the sha256 over the canonical
(sort-keys, no spaces, UTF-8) payload JSON. The payload deliberately excludes
the import time - ``acquired_at`` is a column holding the actual import time -
so re-importing identical content finds the saved version by hash and appends
no revision, while any content change appends ``max(revision) + 1`` next to the
untouched older rows. The roster is not registered as a screening universe or
watchlist entry; no official-constituent verification is performed here, so an
imported roster stays ``candidate``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.cli.commands import peer_annual as peer_annual_cmd
from investment_agent.config import Settings
from investment_agent.domain.a_share import parse_a_share_ticker
from investment_agent.industry_peers.batch import DEFAULT_COHORT_KEY, submit_offline_batch
from investment_agent.providers.shenwan_directory import SW2021_SYSTEM
from investment_agent.storage.database import run_with_engine, session_scope
from investment_agent.storage.models.peer import PeerCohortSnapshot
from investment_agent.storage.models.reference import Company
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerCohortSnapshotRepository

app = typer.Typer(help="Industry & peer reference (offline roster administration)")

COHORT_KEY = "baijiu_current"
"""Stable roster name of the first cohort (design §名单快照)."""

SAMPLE_ROSTER_FIELD = "baijiu"
SAMPLE_CLASSIFICATION_FIELD = "baijiu_classification_codes"
"""The sample file's roster list and its Shenwan baijiu classification list."""

CLASSIFICATION_CODE = "340501"
"""Shenwan 2021 baijiu III code (``config/shenwan_2021.yaml``: 白酒Ⅲ)."""

CLASSIFICATION_VERSION = "2021"
"""Shenwan standard version the code directory was generated from."""

INDEX_CODE = "851251.SI"
"""Baijiu index code, fixed by the technical design (the sample file has no index field)."""

INCLUSION_RULE_VERSION = "run-02/1"
"""Rules that produced the roster: current baijiu index constituents from the
run-02 evidence, classification-only members excluded; version 1."""

IMPORTER_VERIFICATION = "candidate"
"""This importer performs no official constituent verification, so an imported
roster can never claim ``verified`` (run-02: the official constituent endpoint
was unavailable)."""

PAYLOAD_SCHEMA = "peer_cohort_snapshot/1"
NOT_PROVIDED = "not_provided_by_source"
"""Explicit marker for roster facts the sample file does not carry."""

DEFAULT_SAMPLE = Path("research/industry-data-validation/evidence/run-02/sample.json")
_REPO_ROOT = Path(__file__).resolve().parents[4]

_PAYLOAD_SCHEMA_KEY = "schema"


class PeerIdentityError(ValueError):
    """Roster codes the existing directory cannot resolve; the batch stopped."""


@dataclass(frozen=True)
class SampleRoster:
    """The validated parts of a run-02 style sample file this importer needs."""

    roster: list[str]
    classification_codes: list[str]
    captured_at: str
    """Raw source capture timestamp (aware ISO); roster observation time."""
    membership_semantics: str | None
    selection: str | None
    other_sample_fields: list[str]
    """Other industry samples carried by the file (bank, pv, ...) - not this cohort."""


@dataclass(frozen=True)
class CohortImportResult:
    snapshot: PeerCohortSnapshot
    created: bool
    """``False`` when identical content was already saved (no new revision)."""


def payload_sha256(payload: dict[str, Any]) -> str:
    """sha256 over the canonical payload JSON (sort keys, no whitespace)."""

    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _locator(path: Path) -> str:
    """Repo-relative source path when the file lives in the checkout (stable
    across machines, so the content hash does not move with the clone root);
    the absolute path otherwise."""

    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT))
    except ValueError:
        return str(resolved)


def _ticker_list(data: dict[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(t, str) for t in value):
        raise ValueError(f"{path}: {key!r} must be a non-empty list of ticker strings")
    invalid = sorted(t for t in value if parse_a_share_ticker(t) is None)
    if invalid:
        raise ValueError(
            f"{path}: {key!r} holds non-canonical A-share tickers: {', '.join(invalid)}"
        )
    duplicates = sorted({t for t in value if value.count(t) > 1})
    if duplicates:
        raise ValueError(f"{path}: {key!r} repeats tickers: {', '.join(duplicates)}")
    return list(value)


def _is_ticker_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and parse_a_share_ticker(item) is not None for item in value)
    )


def load_sample(path: Path) -> tuple[SampleRoster, str]:
    """Read and validate a run-02 style sample file; return it with its sha256."""

    raw = path.read_bytes()
    source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    roster = _ticker_list(data, SAMPLE_ROSTER_FIELD, path)
    classification_codes = _ticker_list(data, SAMPLE_CLASSIFICATION_FIELD, path)
    captured_at = data.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise ValueError(f"{path}: 'captured_at' must be an ISO timestamp string")
    parsed = _parse_captured_at(captured_at, path)
    if parsed.tzinfo is None:
        raise ValueError(f"{path}: 'captured_at' must carry a timezone offset")
    semantics = data.get("membership_semantics")
    selection = data.get("selection")
    # Other industry samples are ticker lists (bank, pv, ...). Difference lists
    # are reported separately and period strings are not tickers.
    other = sorted(
        key
        for key, value in data.items()
        if key not in (SAMPLE_ROSTER_FIELD, SAMPLE_CLASSIFICATION_FIELD)
        and key not in ("index_only", "classification_only")
        and _is_ticker_list(value)
    )
    return (
        SampleRoster(
            roster=roster,
            classification_codes=classification_codes,
            captured_at=captured_at,
            membership_semantics=semantics if isinstance(semantics, str) else None,
            selection=selection if isinstance(selection, str) else None,
            other_sample_fields=other,
        ),
        source_sha256,
    )


def _parse_captured_at(raw: str, path: Path) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{path}: 'captured_at' is not an ISO timestamp: {raw!r}") from exc


async def _directory_identity(session: AsyncSession, ticker: str) -> tuple[Any, str] | None:
    """``(listing, directory name)`` for a canonical ticker, or ``None``.

    Strictly a lookup in the existing directory: no creation, no name matching.
    """

    if parse_a_share_ticker(ticker) is None:
        return None
    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        return None
    company = await session.get(Company, listing.company_id)
    if company is None or not company.canonical_name:
        return None
    return listing, company.canonical_name


async def resolve_roster_identities(
    session: AsyncSession, tickers: Sequence[str]
) -> dict[str, tuple[Any, str]]:
    """Resolve every roster code before anything is written (all-or-nothing).

    Every missing code is collected into one :class:`PeerIdentityError`; the
    import never skips an entry and never merges companies by name.
    """

    resolved: dict[str, tuple[Any, str]] = {}
    missing: list[str] = []
    for ticker in tickers:
        identity = await _directory_identity(session, ticker)
        if identity is None:
            missing.append(ticker)
            continue
        resolved[ticker] = identity
    if missing:
        raise PeerIdentityError(
            "roster codes without a directory identity; import stopped, nothing written: "
            + ", ".join(missing)
        )
    return resolved


def build_cohort_payload(
    *,
    cohort_key: str,
    sample: SampleRoster,
    source_sha256: str,
    source_path: str,
    resolved: dict[str, tuple[Any, str]],
    excluded_identities: dict[str, tuple[Any, str] | None],
) -> dict[str, Any]:
    """The immutable roster JSON (version identity; import time stays out)."""

    code_set = set(sample.classification_codes)
    entries: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    for ticker in sample.roster:
        listing, name = resolved[ticker]
        entries.append(
            {
                "ticker": ticker,
                "company_id": listing.company_id,
                "listing_id": listing.id,
                "market_code": listing.market_code,
                "name": name,
                "name_source": "local_directory.company.canonical_name",
                "classification_member": ticker in code_set,
                "classification_evidence": NOT_PROVIDED,
                "listing_status": None,
                "listing_status_evidence": NOT_PROVIDED,
                "comparability_flags": None,
                "comparability_flags_evidence": NOT_PROVIDED,
            }
        )
        included.append(
            {
                "ticker": ticker,
                "company_id": listing.company_id,
                "reason": (
                    "source roster member (sample.json 'baijiu': current baijiu index "
                    "constituents); the first phase includes the whole roster"
                ),
            }
        )
    index_only = sorted(set(sample.roster) - code_set)
    classification_only = sorted(code_set - set(sample.roster))
    excluded: list[dict[str, Any]] = []
    for ticker in classification_only:
        identity = excluded_identities.get(ticker)
        excluded.append(
            {
                "ticker": ticker,
                "company_id": identity[0].company_id if identity is not None else None,
                "reason": (
                    "classification_only: only in the sample's Shenwan baijiu classification "
                    "list ('baijiu_classification_codes'), not in the index constituent list "
                    "('baijiu'); the source gives no further basis, so the entry stays outside "
                    "the confirmed sample"
                ),
            }
        )
    notes = [
        "observed_on is the source capture date as recorded; it is never an official "
        "constituent effective date",
        "classification and listing-status publication times are not provided by the "
        "source; their precision stays unknown instead of being faked",
        "this importer performs no official constituent verification; the roster stays "
        "candidate until a controlled verification exists",
    ]
    if sample.other_sample_fields:
        notes.append(
            "the source file also carries non-cohort samples ("
            + ", ".join(sample.other_sample_fields)
            + "); they are not part of this roster"
        )
    return {
        _PAYLOAD_SCHEMA_KEY: PAYLOAD_SCHEMA,
        "cohort_key": cohort_key,
        "classification": {
            "system": SW2021_SYSTEM,
            "version": CLASSIFICATION_VERSION,
            "code": CLASSIFICATION_CODE,
            "per_company_evidence": NOT_PROVIDED,
        },
        "index_code": INDEX_CODE,
        "index_code_source": (
            "fixed by the technical design (名单快照); the sample file does not carry "
            "the index code"
        ),
        "source": {
            "kind": "offline_evidence_file",
            "path": source_path,
            "sha256": source_sha256,
            "captured_at": sample.captured_at,
            "membership_semantics": sample.membership_semantics,
            "selection": sample.selection,
        },
        "entries": entries,
        "included": included,
        "excluded": excluded,
        "source_differences": {
            "index_only": index_only,
            "classification_only": classification_only,
        },
        "notes": notes,
    }


async def import_sample_roster(
    session: AsyncSession,
    *,
    sample_path: Path,
    cohort_key: str = COHORT_KEY,
    acquired_at: datetime | None = None,
) -> CohortImportResult:
    """Import one roster version from a run-02 style sample file.

    All-or-nothing: identity resolution covers every roster code before any
    write. ``content_hash`` covers the canonical payload - which excludes the
    import time - so identical content re-imports as the saved revision while
    changed content appends the next revision beside the untouched old rows.
    ``acquired_at`` defaults to the actual import time and is injectable for
    tests. Nothing here registers a screening universe or watchlist entry.
    """

    sample, source_sha256 = load_sample(sample_path)
    observed_at = _parse_captured_at(sample.captured_at, sample_path)
    resolved = await resolve_roster_identities(session, sample.roster)
    # Difference entries are excluded, so an unresolvable one is recorded as
    # null identity instead of failing the batch.
    difference_tickers = set(sample.classification_codes) - set(sample.roster)
    excluded_identities = {
        ticker: await _directory_identity(session, ticker) for ticker in sorted(difference_tickers)
    }
    payload = build_cohort_payload(
        cohort_key=cohort_key,
        sample=sample,
        source_sha256=source_sha256,
        source_path=_locator(sample_path),
        resolved=resolved,
        excluded_identities=excluded_identities,
    )
    content_hash = payload_sha256(payload)
    repo = PeerCohortSnapshotRepository(session)
    existing = await repo.by_content_hash(cohort_key=cohort_key, content_hash=content_hash)
    if existing is not None:
        return CohortImportResult(snapshot=existing, created=False)
    row = PeerCohortSnapshot(
        id=str(uuid.uuid4()),
        cohort_key=cohort_key,
        revision=await repo.next_revision(cohort_key),
        observed_on=observed_at.date(),
        acquired_at=acquired_at or datetime.now(UTC),
        verification=IMPORTER_VERIFICATION,
        inclusion_rule_version=INCLUSION_RULE_VERSION,
        content_hash=content_hash,
        payload=payload,
    )
    return CohortImportResult(snapshot=await repo.add(row), created=True)


@app.command("import-roster")
def import_roster(
    sample_path: Path = typer.Argument(  # noqa: B008
        _REPO_ROOT / DEFAULT_SAMPLE,
        exists=True,
        dir_okay=False,
        readable=True,
        help="run-02 style sample file (default: the committed run-02 evidence)",
    ),
    cohort_key: str = typer.Option(
        COHORT_KEY, "--cohort-key", help="Stable roster name the snapshot is filed under"
    ),
) -> None:
    """Import the fixed sample roster as one immutable cohort snapshot (WP2).

    Resolves every roster code against the existing directory first: one
    unresolvable code stops the whole batch with exit 1 and writes nothing.
    Re-importing identical content never appends a revision; changed content
    appends the next revision and keeps the old rows readable. No network
    calls; the roster is not registered as a screening pool or watchlist.
    """

    settings = Settings()  # type: ignore[call-arg]

    async def run() -> dict[str, Any]:
        async with session_scope() as session:
            result = await import_sample_roster(
                session, sample_path=sample_path, cohort_key=cohort_key
            )
            row = result.snapshot
            return {
                "cohort_key": row.cohort_key,
                "revision": row.revision,
                "snapshot_id": row.id,
                "created": result.created,
                "observed_on": row.observed_on.isoformat(),
                "verification": row.verification,
                "inclusion_rule_version": row.inclusion_rule_version,
                "entries": len(row.payload["entries"]),
                "included": len(row.payload["included"]),
                "excluded": [item["ticker"] for item in row.payload["excluded"]],
                "content_hash": row.content_hash,
                "source_sha256": row.payload["source"]["sha256"],
            }

    try:
        summary = run_with_engine(settings.database_url, run)
    except PeerIdentityError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (ValueError, OSError) as exc:
        typer.echo(f"roster import failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    if not summary["created"]:
        typer.echo(
            f"identical content already saved as revision {summary['revision']}; "
            "no new revision appended"
        )
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))


@app.command("submit-offline-batch")
def submit_offline_batch_command(
    cohort_key: str = typer.Option(
        DEFAULT_COHORT_KEY, "--cohort-key", help="Cohort roster the batch is built against"
    ),
    ticker: list[str] = typer.Option(  # noqa: B008
        [],
        "--ticker",
        help="Target ticker(s); repeat for several. Default: every roster member is a target.",
    ),
    report_period: str = typer.Option(
        "",
        "--report-period",
        help="Batch period as YYYY-MM-DD (default: the latest period with imported snapshots)",
    ),
    provider: str = typer.Option(
        peer_annual_cmd.PROVIDER, "--provider", help="Source scope the batch freezes inputs in"
    ),
    adapter_version: str = typer.Option(
        peer_annual_cmd.ADAPTER_VERSION,
        "--adapter-version",
        help="Adapter scope the batch freezes inputs in",
    ),
    batch_run_id: str = typer.Option(
        "", "--batch-run-id", help="Batch identity (default: a generated peer-offline-... id)"
    ),
) -> None:
    """Freeze, compute, save and submit one offline peer batch (WP6).

    Reads the imported roster and annual snapshots, freezes the batch inputs
    (conflicted fields excluded through the WP4 conflict query), materializes
    the field evidence that has a date basis, saves the three metric records
    per target through the WP5 idempotent path, and writes the batch mapping
    artifact under ``<DATA_DIR>/research-runs/<batch_run_id>/``. No HTTP, no
    worker, no upstream request. Identical inputs already submitted in the
    same scope are reused: no new generation, no new artifact.
    """

    period: date | None = None
    if report_period:
        try:
            period = date.fromisoformat(report_period)
        except ValueError as exc:
            typer.echo(f"--report-period must be YYYY-MM-DD: {report_period!r}", err=True)
            raise typer.Exit(1) from exc
    for item in ticker:
        if parse_a_share_ticker(item.upper()) is None:
            typer.echo(f"--ticker must be a canonical A-share code, got {item!r}", err=True)
            raise typer.Exit(1)
    settings = Settings()  # type: ignore[call-arg]

    async def run() -> dict[str, Any]:
        async with session_scope() as session:
            submission = await submit_offline_batch(
                session,
                data_dir=settings.data_dir,
                cohort_key=cohort_key,
                tickers=[item.upper() for item in ticker] or None,
                provider=provider,
                adapter_version=adapter_version,
                report_period=period,
                batch_run_id=batch_run_id or None,
            )
            mapping = submission.mapping
            return {
                "batch_run_id": mapping.batch_run_id,
                "created": submission.created,
                "generation": mapping.generation,
                "status": mapping.status,
                "cohort_ref": mapping.cohort_ref.model_dump(mode="json"),
                "report_period": mapping.report_period.isoformat(),
                "provider": mapping.provider,
                "adapter_version": mapping.adapter_version,
                "targets": len(mapping.targets),
                "saved_records": submission.saved_records,
                "reused_records": submission.reused_records,
                "gap_metrics": submission.gap_metrics,
                "frozen_companies": submission.frozen_companies,
                "target_gaps": [gap.ticker for gap in mapping.manifest.target_gaps],
                "missing_companies": [item.ticker for item in mapping.manifest.missing_companies],
                "artifact": str(submission.path),
                "submitted_at": mapping.submitted_at.isoformat(),
            }

    try:
        summary = run_with_engine(settings.database_url, run)
    except ValueError as exc:
        typer.echo(f"offline batch submission failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    if not summary["created"]:
        typer.echo(
            f"identical inputs already submitted as batch {summary['batch_run_id']} "
            f"(generation {summary['generation']}); reused, no new generation"
        )
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
