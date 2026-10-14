"""The shared industry-peer test world (``tests/support/__init__.py`` policy).

The peer test modules used to import builders, constants and pytest fixtures
from one another (``test_peer_service`` served as an accidental support
module). This module is their single shared home:

* **run-02 golden evidence** - the three files the peer suite reads are
  tracked under ``tests/fixtures/peer_run02`` (byte-identical copies of the
  untracked ``research/industry-data-validation/evidence/run-02`` originals),
  so a clean checkout can run the cluster. Never add a skip on a missing
  file: absence must fail loudly.
* **synthetic worlds** - the dated synthetic cohort (service/resolver/wp13
  tests) and the offline collection doubles (jobs/review tests).
* **web page helpers** - the shipped ``industry_peers.js`` path and its
  label-map parser.
* **AST source guards** - structural checks over a module's parse tree
  (imports, identifiers, string constants), so a comment or an unrelated
  string can neither satisfy nor fail them.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import ExecutionGrant, admit
from investment_agent.agent.tasks import TaskService
from investment_agent.cli.commands.peer_annual import (
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.evidence.contracts import CalculationRecord, Period
from investment_agent.industry_peers.artifacts import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_PROVIDER,
    BatchManifest,
    MetricMappingEntry,
    PeerBatchMapping,
    TargetMapping,
    mapping_artifact_path,
    read_batch_mapping,
    register_batch_commit,
    write_batch_mapping,
)
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.calculations import ROE
from investment_agent.industry_peers.contracts import (
    BINDING_METRIC_KEYS,
    PeerAnnualField,
    PeerContextBinding,
    PeerFieldVerification,
    PeerMetricBinding,
)
from investment_agent.industry_peers.jobs import (
    PEER_COLLECT_KIND,
    PeerCollectHandler,
    enqueue_peer_collect,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Claim,
    CompanyRecommendation,
    Dependency,
    DimensionAssessment,
    Mandate,
    NumericNode,
    ResearchAuthorization,
    TextNode,
    ThesisRevision,
)
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import CreateCase, InvestorService
from investment_agent.investor.publication import FIXED_METHOD, PublicationCandidate
from investment_agent.jobs.base import ClaimedJob
from investment_agent.providers.eastmoney_annual import PROVIDER, RawAnnualResponse
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import (
    PeerAnnualSnapshotRepository,
    PeerCohortSnapshotRepository,
)
from investment_agent.storage.repositories.pipeline_run import (
    PipelineRunRepository,
    RunLease,
)
from tests.support.conversations import base
from tests.support.peer_originals import synthetic_original_proofs
from tests.support.publication import metadata
from tests.support.research import full_progress

# --- run-02 golden evidence (tracked under tests/fixtures) ---------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "peer_run02"
"""Byte-identical copies of the run-02 evidence; the ``research/`` originals
are untracked local artifacts and must never be the test input."""

RUN02_SAMPLE = RUN02_FIXTURES / "sample.json"
DERIVED = RUN02_FIXTURES / "derived"
SAMPLE_PATH = DERIVED / "baijiu-peer-sample.json"
AUDIT_PATH = DERIVED / "baijiu-official-audit.json"

# --- synthetic world (dated fields, from the service/resolver fixtures) --------

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
LATER_ACQUIRED_AT = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
NOTICE_DATE = date(2026, 4, 25)
PERIOD_END = date(2025, 12, 31)
PREVIOUS_PERIOD = date(2024, 12, 31)
OWNER = "local_owner"
MAOTAI = "600519.SH"
ADAPTER = "offline-run02-v1"
"""The synthetic fixture freezes in the service's default scope, so reads
with the default selection work; the ``synthetic-rival`` provider used by
single tests creates conflicts."""
TARGET = "600001.SH"
PEER = "600002.SH"
GAP_PEER = "600003.SH"
FEATURE = dict(industry_peer_context_enabled=True)

FIELD_KINDS: dict[str, tuple[str, str, str | None]] = {
    "operating_revenue": ("flow", "CNY", "CNY"),
    "net_profit_parent": ("flow", "CNY", "CNY"),
    "operating_cash_flow": ("flow", "CNY", "CNY"),
    "equity_parent": ("stock", "CNY", "CNY"),
    "contract_liabilities": ("stock", "CNY", "CNY"),
    "roe_weighted_annual": ("ratio", "fraction", None),
}
SYNTHETIC_ROSTER: tuple[tuple[str, str], ...] = (
    ("600001.SH", "目标公司"),
    ("600002.SH", "同行二"),
    ("600003.SH", "同行三"),
    ("600004.SH", "同行四"),
    ("600005.SH", "同行五"),
    ("600006.SH", "同行六"),
    ("600007.SH", "同行七"),
)
# target 0.30 ROE / 0.15 contract-revenue / 0.75 cfo; peers 0.05..0.35, 0.10, 0.5..1.0
SYNTHETIC_VALUES: tuple[dict[str, str], ...] = (
    {
        "roe_weighted_annual": "0.30",
        "equity_parent": "500",
        "contract_liabilities": "15",
        "operating_revenue": "100",
        "operating_cash_flow": "75",
        "net_profit_parent": "100",
    },
) + tuple(
    {
        "roe_weighted_annual": roe,
        "equity_parent": "100",
        "contract_liabilities": "10",
        "operating_revenue": "100",
        "operating_cash_flow": cfo,
        "net_profit_parent": "100",
    }
    for roe, cfo in zip(
        ("0.05", "0.10", "0.15", "0.20", "0.25", "0.35"),
        ("50", "60", "70", "80", "90", "100"),
        strict=True,
    )
)


def synthetic_field(metric: str, value: str, *, period_end: date = PERIOD_END) -> PeerAnnualField:
    kind, unit, currency = FIELD_KINDS[metric]
    if kind == "flow":
        period = Period(
            start=date(period_end.year, 1, 1), end=period_end, basis="period_to_date/annual"
        )
        raw_unit = "CNY"
    elif kind == "stock":
        period = Period(start=period_end, end=period_end, basis="point_in_time")
        raw_unit = "CNY"
    else:
        period = Period(start=date(period_end.year, 1, 1), end=period_end, basis="annual_ratio")
        raw_unit = "percent"
    saved = Decimal(value)
    raw_value = str(saved * 100) if metric == "roe_weighted_annual" else value
    return PeerAnnualField(
        metric=metric,
        value=saved,
        raw_value=raw_value,
        unit=unit,
        currency=currency,
        period=period,
        source_field="SYNTHETIC",
        raw_unit=raw_unit,
        disclosed_on=NOTICE_DATE,
        acquired_at=ACQUIRED_AT,
        verification=PeerFieldVerification.VERIFIED,
    )


def synthetic_proof(metric: str, value: str) -> dict[str, Any]:
    excerpt = f"synthetic {metric} {value}"
    return {
        "metric": metric,
        "status": "verified",
        "original_ref": None,
        "pdf_hash": None,
        "report_identity": None,
        "pdf_page": 3,
        "header": metric,
        "raw_value": value,
        "raw_unit": FIELD_KINDS[metric][1],
        "parsed_value": value,
        "period": PERIOD_END.isoformat(),
        "accounting_scope": None,
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "checked_at": None,
        "checked_by": None,
        "check_rule_version": None,
        "audit_record": {
            "source": "synthetic-fixture",
            "index": 0,
            "code": "synthetic",
            "field": metric,
            "api_key": "SYNTHETIC",
        },
    }


async def store_snapshot(
    session: Any,
    company_id: int,
    listing_id: int,
    values: dict[str, str],
    *,
    semantic_hash: str,
    provider: str = PROVIDER,
    adapter_version: str = ADAPTER,
    report_period: date = PERIOD_END,
    captured_at: datetime = ACQUIRED_AT,
) -> PeerAnnualSnapshot:
    """Append one snapshot version exactly as the WP3 importer would store it."""

    fields = {
        metric: synthetic_field(metric, value, period_end=report_period)
        for metric, value in values.items()
    }
    listing = await session.get(Listing, listing_id)
    ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    proofs = await synthetic_original_proofs(session, ticker, listing_id, fields, captured_at)
    return await PeerAnnualSnapshotRepository(session).add(
        PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=company_id,
            listing_id=listing_id,
            report_period=report_period,
            provider=provider,
            adapter_version=adapter_version,
            captured_at=captured_at,
            content_hash=semantic_hash,
            semantic_hash=semantic_hash,
            fields={
                metric: synthetic_field(metric, value, period_end=report_period).model_dump(
                    mode="json"
                )
                for metric, value in values.items()
            },
            verification=proofs,
        )
    )


async def seed_listings(
    session: Any, roster: tuple[tuple[str, str], ...]
) -> dict[str, tuple[int, int]]:
    """Create the directory listings; returns ``code -> (company_id, listing_id)``."""

    listing_repo = ListingRepository(session)
    ids: dict[str, tuple[int, int]] = {}
    for code, name in roster:
        listing = await listing_repo.ensure_a_share_listing(code, company_name=name)
        ids[code] = (listing.company_id, listing.id)
    return ids


async def seed_cohort(session: Any, identities: dict[str, tuple[int, int]]) -> None:
    """One roster version over the synthetic members (the default cohort key)."""

    entries = [
        {
            "ticker": code,
            "company_id": company_id,
            "listing_id": listing_id,
            "name": name,
        }
        for code, name in SYNTHETIC_ROSTER
        for company_id, listing_id in [identities[code]]
    ]
    await PeerCohortSnapshotRepository(session).add(
        PeerCohortSnapshot(
            id=str(uuid4()),
            cohort_key="baijiu_current",
            revision=1,
            observed_on=PERIOD_END,
            acquired_at=ACQUIRED_AT,
            verification="candidate",
            inclusion_rule_version="test/1",
            content_hash="c" * 64,
            payload={"schema": "test-roster/1", "entries": entries},
        )
    )


@pytest_asyncio.fixture
async def synthetic(db_session):
    """Directory listings plus one roster over the synthetic members.

    Snapshots are *not* stored here: each test stores exactly the versions of
    its scenario (a second disagreeing version is an honest re-check trigger,
    so scenario data and base data must not both exist unless a test wants
    that).
    """

    identities = await seed_listings(db_session, SYNTHETIC_ROSTER)
    await seed_cohort(db_session, identities)
    await db_session.commit()
    return {
        "ids": {code: company_id for code, (company_id, _listing) in identities.items()},
        "listings": {code: listing_id for code, (_company, listing_id) in identities.items()},
    }


async def store_standard(
    session: Any,
    synthetic: dict[str, Any],
    *,
    target_overrides: dict[str, str] | None = None,
    without_roe: set[str] = frozenset(),
    captured_at: datetime = ACQUIRED_AT,
) -> None:
    """The seven standard 2025 versions, with per-test scenario tweaks."""

    for index, ((code, _name), values) in enumerate(
        zip(SYNTHETIC_ROSTER, SYNTHETIC_VALUES, strict=True)
    ):
        if code in without_roe:
            values = {key: item for key, item in values.items() if key != "roe_weighted_annual"}
        if code == "600001.SH" and target_overrides:
            values = {**values, **target_overrides}
        await store_snapshot(
            session,
            synthetic["ids"][code],
            synthetic["listings"][code],
            values,
            semantic_hash=f"{index + 1:02d}" * 32,
            captured_at=captured_at,
        )
    await session.commit()


@pytest_asyncio.fixture
async def golden(db_session):
    """run-02 fully imported: roster (WP2) and annual facts with proofs (WP3)."""

    sample = load_sample_json(SAMPLE_PATH)
    listing_repo = ListingRepository(db_session)
    for entry in sample["companies"]:
        await listing_repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
    roster = await import_sample_roster(
        db_session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT
    )
    await import_peer_annual_snapshots(
        db_session,
        sample=sample,
        audit=load_audit_json(AUDIT_PATH),
        acquired_at=ACQUIRED_AT,
    )
    await db_session.commit()
    return roster.snapshot


async def seed_world(session, tmp_path, *, values=None):
    """Roster, snapshots and one submitted batch over the synthetic roster."""

    identities = await seed_listings(session, SYNTHETIC_ROSTER)
    await seed_cohort(session, identities)
    synthetic = {
        "ids": {code: company for code, (company, _listing) in identities.items()},
        "listings": {code: listing for code, (_company, listing) in identities.items()},
    }
    if values is None:
        await store_standard(session, synthetic, captured_at=datetime.now(UTC) - timedelta(hours=3))
    else:
        for (code, _name), entry in zip(SYNTHETIC_ROSTER, values, strict=True):
            await store_snapshot(
                session,
                synthetic["ids"][code],
                synthetic["listings"][code],
                entry,
                semantic_hash=f"fixture-{code}",
                captured_at=datetime.now(UTC) - timedelta(hours=3),
            )
        await session.commit()
    submission = await submit_offline_batch(
        session,
        data_dir=tmp_path,
        tickers=[TARGET],
        provider=DEFAULT_PROVIDER,
        adapter_version=DEFAULT_ADAPTER_VERSION,
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await session.commit()
    return synthetic, submission


def listing_ref(synthetic: dict, code: str) -> ObjectRef:
    return ObjectRef(kind="listing", id=synthetic["listings"][code], revision=1)


async def metric_refs(submission, synthetic) -> dict[str, ObjectRef]:
    """The submitted metric references of the batch's one target."""

    target_entry = submission.mapping.target_for(synthetic["ids"][TARGET])
    return {
        entry.metric_key: entry.ref
        for entry in target_entry.metric_calculation_refs
        if entry.ref
    }


async def record_of(session, ref: ObjectRef) -> CalculationRecord:
    row = await session.get(SavedCalculation, (str(ref.id), ref.revision))
    assert row is not None
    return CalculationRecord.model_validate(row.payload)


def make_binding(
    *,
    record: CalculationRecord,
    batch_run_id: str,
    refs_by_metric: dict[str, ObjectRef],
    cutoff: datetime,
) -> PeerContextBinding:
    """A binding over one record's own cohort identity (the craft seam)."""

    entries = tuple(
        PeerMetricBinding(
            metric_key=key,
            state="saved" if key in refs_by_metric else "gap",
            ref=refs_by_metric.get(key),
            gap_reason=None if key in refs_by_metric else "fixture gap",
        )
        for key in sorted(BINDING_METRIC_KEYS)
    )
    return PeerContextBinding(
        target=TARGET,
        cohort_ref=ObjectRef.model_validate(record.details["cohort"]["ref"]),
        batch_run_id=batch_run_id,
        report_period=PERIOD_END,
        cutoff=cutoff,
        metric_calculation_refs=entries,
    )


async def write_craft_mapping(
    tmp_path,
    session=None,
    *,
    batch_run_id: str,
    record: CalculationRecord,
    target_company_id: int,
    target_listing_id: int,
    refs_by_metric: dict[str, ObjectRef],
    submitted_at: datetime,
) -> PeerBatchMapping:
    """A hand-submitted mapping artifact for negative/reuse scenarios."""

    entries = tuple(
        MetricMappingEntry(
            metric_key=key,
            state="reused" if key in refs_by_metric else "gap",
            ref=refs_by_metric.get(key),
            gap_reason=None if key in refs_by_metric else "fixture gap",
        )
        for key in sorted(BINDING_METRIC_KEYS)
    )
    mapping = PeerBatchMapping.create(
        batch_run_id=batch_run_id,
        cohort_ref=ObjectRef.model_validate(record.details["cohort"]["ref"]),
        cohort_content_hash=record.details["cohort"]["content_hash"],
        report_period=PERIOD_END,
        provider=DEFAULT_PROVIDER,
        adapter_version=DEFAULT_ADAPTER_VERSION,
        generation=9,
        status="success",
        submitted_at=submitted_at,
        targets=(
            TargetMapping(
                company_id=target_company_id,
                listing_id=target_listing_id,
                ticker=TARGET,
                name="目标公司",
                report_period=PERIOD_END,
                metric_calculation_refs=entries,
            ),
        ),
        manifest=BatchManifest(),
    )
    if session is None:
        async with session_scope() as owned:
            await register_batch_commit(owned, mapping)
    else:
        await register_batch_commit(session, mapping)
    write_batch_mapping(tmp_path, mapping)
    return mapping


async def clone_calculation(session, ref: ObjectRef, mutate, *, revision: int = 2) -> ObjectRef:
    """Same record identity, next revision, mutated payload (craft seam).

    Keeping the id/input-hash derivation intact lets a mutation exercise its
    specific checklist item instead of failing the earlier identity guard.
    """

    row = await session.get(SavedCalculation, (str(ref.id), ref.revision))
    assert row is not None
    data = CalculationRecord.model_validate(row.payload).model_dump(mode="json")
    clone = CalculationRecord.model_validate(mutate(data))
    session.add(
        SavedCalculation(
            id=str(ref.id),
            revision=revision,
            owner_id="local_owner",
            payload=clone.model_dump(mode="json"),
        )
    )
    await session.flush()
    return ref.model_copy(update={"revision": revision})


def output_node(ref: ObjectRef, key: str = "peer_median") -> NumericNode:
    return NumericNode(
        type="numeric_claim",
        ref=ref.model_copy(update={"field_path": f"results.{key}"}),
        role=NumericRole.OUTPUT,
        display_format="decimal" if key in {"valid_peer_count", "candidate_peer_count"} else "percent",
    )


def full_progress_for(**updates):
    dimensions = tuple(
        DimensionAssessment(
            dimension=name,
            status="missing",
            applicable=True,
            reason="Report detail remains unavailable",
            refs=(),
        )
        for name in DIMENSIONS
    )
    return full_progress(dimensions, **updates)


async def peer_candidate(session, synthetic, refs, task_id):
    """A bounded company view whose critical inference cites the peer median."""

    target = listing_ref(synthetic, TARGET)
    service = InvestorService(session)
    unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
    mandate = await service.create_mandate(
        "local_owner",
        Mandate(
            **metadata(),
            goals=(),
            scope_refs=(target,),
            hard_constraints=(),
            unknowns=(),
            authorization=ResearchAuthorization(
                daily_budget=unknown,
                per_task_budget=unknown,
                max_leads_per_batch=0,
                max_new_tasks_per_batch=0,
                source_refs=(),
            ),
        ),
        uuid4(),
    )
    case = await service.create_case(
        "local_owner",
        CreateCase(listing_ref=target, question="Peer reference view", idempotency_key=uuid4()),
    )
    as_of = datetime.now(UTC) - timedelta(hours=1)
    roe_ref = refs[ROE]
    claim = Claim(
        id=uuid4(),
        kind="inference",
        text=(
            TextNode(type="text", text="The roster statistics describe the margin position"),
            output_node(roe_ref),
        ),
        materiality="critical",
        support_refs=(roe_ref,),
        opposition_refs=(),
        assumption_refs=(),
        verification="verified",
        impact_if_wrong="The margin comparison must be reconsidered",
    )
    dimensions = tuple(
        DimensionAssessment(
            dimension=name,
            status="missing",
            applicable=True,
            reason="Report detail remains unavailable",
            refs=(),
        )
        for name in DIMENSIONS
    )
    meta = {**metadata(), "origin_ref": ObjectRef(kind="task", id=task_id, revision=1)}
    thesis = ThesisRevision(
        **meta,
        case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision),
        previous_ref=None,
        change_reason="Initial review",
        question_scope="Peer-referenced margin position",
        assessed_dimensions=dimensions,
        claims=(claim,),
        assumptions=(),
        counter_case=(TextNode(type="text", text="Roster statistics may not generalize"),),
        evidence_set_hash="peer-material",
        method_ref=FIXED_METHOD,
        as_of=as_of,
    )
    recommendation = CompanyRecommendation(
        **meta,
        scope="company_view",
        case_ids=(case.id,),
        thesis_revision_refs=(ObjectRef(kind="thesis", id=thesis.id, revision=thesis.revision),),
        as_of=as_of,
        question_scope=thesis.question_scope,
        assessed_dimensions=dimensions,
        sufficiency="bounded",
        claims=(claim,),
        counter_case=thesis.counter_case,
        assumptions=(),
        gaps=("Company report detail remains unavailable",),
        conditions=(),
        valid_until=as_of + timedelta(days=14),
        next_review_at=as_of + timedelta(days=7),
        monitor_refs=(),
        dependency_refs=(Dependency(ref=roe_ref, materiality="critical", fields=("value",)),),
        mandate_revision_id=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision),
        method_revision_id=FIXED_METHOD,
        provenance=(roe_ref,),
        business_view="undetermined",
        price_view="undetermined",
        conditional_actions=(),
    )
    return PublicationCandidate(thesis=thesis, recommendation=recommendation)


async def prepared_with_peer(candidate, review, binding):
    """Admit the publishing task with the trusted peer binding in its grant."""

    async with session_scope() as session:
        mandate = await InvestorService(session).get_mandate("local_owner")
        case = await InvestorService(session).get_case("local_owner", candidate.thesis.case_ref.id)
        grant = ExecutionGrant(
            mode="interactive",
            source="authenticated_message",
            prompt="Publish the peer-referenced view",
            tool_names=("publish_recommendation", "extend_recommendation"),
            scope_refs=(candidate.thesis.case_ref, case.listing_ref),
            mandate_id=mandate.id,
            peer_context=binding,
        )
        task = await admit(
            session,
            base=base(),
            input_key=str(uuid4()),
            binding=ContextBinding(
                context_refs=(candidate.thesis.case_ref,),
                base_refs=(candidate.thesis.case_ref,),
                as_of=datetime.now(UTC),
            ),
            grant=grant,
        )
        row = await session.get(AgentTask, str(task.id))
        assert row is not None
        run = await PipelineRunRepository(session).claim_next(
            worker_id="fixture", run_id=row.run_id
        )
        claim = await TaskService(session).claim(
            task.id, RunLease(run.id, "fixture", run.attempt_count)
        )
        action = await TaskService(session).prepare_action(
            claim,
            intent_key=f"publish:{candidate.thesis.case_ref.id}",
            parameters={
                "candidate": candidate.model_dump(mode="json"),
                "semantic": review.model_dump(mode="json"),
            },
        )
    return claim, UUID(action.operation_id), review


# --- offline collection doubles (jobs / review-regression fixtures) -------------

COLLECT_PERIOD = date(2025, 12, 31)
FILING_NOTICE_DATE = "2026-04-25 00:00:00"
FILING_UPDATE_DATE = "2026-04-30 00:00:00"
COLLECT_ADAPTER = "1.0"

COLLECT_ROSTER: tuple[tuple[str, str], ...] = (
    ("600001", "目标公司"),
    ("600002", "同行二"),
    ("600003", "同行三"),
    ("600004", "同行四"),
    ("600005", "同行五"),
    ("600006", "同行六"),
    ("600007", "同行七"),
)


class NoSleep:
    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __init__(self) -> None:
        self.slept: list[float] = []


class DirectCtx:
    """A JobContext stand-in: no heartbeats, work runs inline."""

    worker_id = "test-worker"

    async def run_with_heartbeat(self, lease: RunLease, work: Any) -> Any:
        return await work()


_OPERATION_BY_PATH = {
    "/PC_HSF10/NewFinanceAnalysis/Index": "f10_ctype",
    "/PC_HSF10/NewFinanceAnalysis/lrbAjaxNew": "f10_income",
    "/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew": "f10_balance",
    "/PC_HSF10/NewFinanceAnalysis/xjllbAjaxNew": "f10_cashflow",
    "/securities/api/data/v1/get": "main_fina",
}


def _code_of(params: dict[str, str]) -> str:
    match = re.search(r"\d{6}", json.dumps(params))
    assert match is not None, f"no company code in {params}"
    return match.group(0)


class FakeAnnualSource:
    """Answers every operation from a per-company body table; counts fetches.

    ``failures`` maps ``(code, operation)`` to a list of exceptions raised on
    consecutive fetches of that request (transient simulation); each raised
    entry is consumed, so a later attempt of the same request succeeds.
    """

    def __init__(
        self,
        companies: dict[str, dict[str, bytes]],
        *,
        failures: dict[tuple[str, str], list[BaseException]] | None = None,
    ) -> None:
        self.companies = companies
        self.failures = failures or {}
        self.fetches: list[tuple[str, str]] = []

    async def fetch(self, url: str, params: dict[str, str]) -> RawAnnualResponse:
        operation = _OPERATION_BY_PATH[urlsplit(url).path]
        code = _code_of(params)
        self.fetches.append((code, operation))
        pending = self.failures.get((code, operation))
        if pending:
            raise pending.pop(0)
        return RawAnnualResponse(status=200, body=self.companies[code][operation])

    def fetch_counts(self, code: str) -> int:
        return sum(1 for fetched, _ in self.fetches if fetched == code)


def _row(
    code: str,
    values: dict[str, Any],
    *,
    notice: str = FILING_NOTICE_DATE,
    update: str = FILING_UPDATE_DATE,
):
    row: dict[str, Any] = {
        "SECUCODE": f"{code}.SH",
        "REPORT_DATE": "2025-12-31 00:00:00",
        "NOTICE_DATE": notice,
        "UPDATE_DATE": update,
        "CURRENCY": "CNY",
    }
    row.update(values)
    return row


def _statement_body(row: dict[str, Any]) -> bytes:
    return json.dumps({"version": "4.0", "data": [row], "success": True}).encode("utf-8")


def _main_body(row: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": "4.0",
            "success": True,
            "result": {"pages": 1, "pageNo": 1, "pageSize": 100, "total": 1, "data": [row]},
        }
    ).encode("utf-8")


def ctype_page(ctype: str | None = "4") -> bytes:
    if ctype is None:
        return b"<!DOCTYPE html><html><body><p>no metadata</p></body></html>"
    return (
        "<!DOCTYPE html><html><head></head><body>"
        f'<input type="hidden" id="hidctype" value="{ctype}" />'
        "</body></html>"
    ).encode()


def company_bodies(code: str, *, ctype: str | None = "4") -> dict[str, bytes]:
    """The five happy answers of one company (ctype page, three tables, main)."""

    income = _row(
        code,
        {
            "OPERATE_INCOME": 25731010647.32,
            "TOTAL_OPERATE_INCOME": 25898434324.18,
            "PARENT_NETPROFIT": 86228146421.62,
        },
    )
    balance = _row(
        code,
        {"TOTAL_PARENT_EQUITY": 200456087654.32, "CONTRACT_LIAB": 32338664843.91},
    )
    cash = _row(code, {"NETCASH_OPERATE": 92435296907.06})
    main = _row(code, {"ROEJQ": 8.62})
    return {
        "f10_ctype": ctype_page(ctype),
        "f10_income": _statement_body(income),
        "f10_balance": _statement_body(balance),
        "f10_cashflow": _statement_body(cash),
        "main_fina": _main_body(main),
    }


def happy_companies(codes: list[str]) -> dict[str, dict[str, bytes]]:
    return {code: company_bodies(code) for code in codes}


def make_settings(tmp_path: Path, *, retries: int = 2) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
        peer_collect_min_interval_seconds=0,
        peer_collect_retries=retries,
    )


async def seed_roster(
    session: Any,
    tickers: tuple[tuple[str, str], ...] = COLLECT_ROSTER,
    *,
    cohort_key: str = "baijiu_current",
    revision: int | None = None,
) -> PeerCohortSnapshot:
    """Directory listings plus one roster version over the given members."""

    repo = ListingRepository(session)
    entries = []
    for code, name in tickers:
        listing = await repo.ensure_a_share_listing(f"{code}.SH", company_name=name)
        entries.append(
            {
                "ticker": f"{code}.SH",
                "company_id": listing.company_id,
                "listing_id": listing.id,
                "name": name,
            }
        )
    cohort_repo = PeerCohortSnapshotRepository(session)
    return await cohort_repo.add(
        PeerCohortSnapshot(
            id=str(uuid4()),
            cohort_key=cohort_key,
            revision=(
                revision
                if revision is not None
                else await cohort_repo.next_revision(cohort_key)
            ),
            observed_on=COLLECT_PERIOD,
            acquired_at=datetime.now(UTC),
            verification="candidate",
            inclusion_rule_version="test/1",
            content_hash="c" * 64,
            payload={"schema": "test-roster/1", "entries": entries},
        )
    )


async def seed_collection_run(
    cohort: PeerCohortSnapshot,
    tmp_path: Path,
    *,
    mode: str = "full",
    provider: str = PROVIDER,
    adapter_version: str = COLLECT_ADAPTER,
    report_period: date = COLLECT_PERIOD,
) -> Any:
    async with session_scope() as session:
        return await enqueue_peer_collect(
            session,
            report_period=report_period,
            mode=mode,  # type: ignore[arg-type]
            triggered_by="test",
            data_dir=tmp_path,
            cohort_row=cohort,
            provider=provider,
            adapter_version=adapter_version,
        )


async def claim_job(run_id: int, worker_id: str = "test-worker") -> ClaimedJob:
    async with session_scope() as session:
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kinds=[PEER_COLLECT_KIND], run_id=run_id
        )
        assert claimed is not None
        return ClaimedJob(
            run_id=claimed.id,
            kind=claimed.kind,
            lease=RunLease(
                run_id=claimed.id, worker_id=worker_id, attempt=claimed.attempt_count
            ),
            request_json=dict(claimed.request_json),
        )


def make_handler(settings: Settings, source: FakeAnnualSource) -> PeerCollectHandler:
    return PeerCollectHandler(
        settings=settings,
        source=source,
        retry_backoff_seconds=0.0,
        sleep=NoSleep(),
    )


async def read_mapping(tmp_path: Path, run_id: int) -> PeerBatchMapping:
    return read_batch_mapping(mapping_artifact_path(tmp_path, str(run_id)))


@pytest.fixture
async def peer_db(db_session, tmp_path):
    """The shared in-memory engine plus the test's data directory."""
    del db_session  # initializes the engine and the schema
    return tmp_path


# --- shipped web module helpers --------------------------------------------------


WEB_STATIC = REPO_ROOT / "src" / "investment_agent" / "web" / "static"
PEERS_JS = WEB_STATIC / "industry_peers.js"


def labels_object(source: str, name: str) -> dict[str, str]:
    match = re.search(rf"const {name} = Object\.freeze\(\{{(.*?)\}}\);", source, re.DOTALL)
    assert match, name
    return {key: value for key, value in re.findall(r"(\w+):\s*\"([^\"]+)\"", match.group(1))}


# --- AST source guards (replace raw source-text assertions) ---------------------


def module_ast(module: ModuleType) -> ast.Module:
    """The parsed syntax tree of an imported module's own source file."""

    file = module.__file__
    assert file is not None, f"{module.__name__} has no source file"
    return ast.parse(Path(file).read_text(encoding="utf-8"), filename=file)


def imported_module_paths(module: ModuleType) -> frozenset[str]:
    """Every absolute module path the imports name, relative ones resolved.

    ``import httpx`` -> ``httpx``; ``from a.b import c`` ->
    ``{"a.b", "a.b.c"}``; ``from ..storage import database`` inside
    ``investment_agent.industry_peers.service`` ->
    ``investment_agent.storage.database``. Imported names are included as
    sub-paths so a caller can assert on the module or on the symbol.
    """

    tree = module_ast(module)
    package = module.__package__ or module.__name__.rpartition(".")[0]
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_path = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                base = parts[: max(len(parts) - (node.level - 1), 0)] if parts else []
                module_path = ".".join((*base, module_path) if module_path else base)
            if module_path:
                paths.add(module_path)
                paths.update(
                    f"{module_path}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
    return frozenset(paths)


def identifier_references(tree: ast.Module) -> frozenset[str]:
    """Every identifier the module's code names, nested scopes included.

    Names, attribute accesses, definition and parameter names, and import
    bindings (aliases included) - so a direct call, an attribute access, a
    local function or a from-import all count as a reference.
    """

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.asname or alias.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.asname or alias.name)
    return frozenset(found)


def string_constants(tree: ast.Module) -> frozenset[str]:
    """Every string literal the module contains (docstrings included)."""

    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
