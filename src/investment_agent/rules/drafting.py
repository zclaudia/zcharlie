"""``rule_draft`` queue job (P1-C5): LLM drafting of a candidate rule instance.

**Job contract** (the candidate lifecycle, P1-D1, enqueues it):

* kind ``rule_draft``; idempotency key
  ``rule_draft:{candidate_id}:{template_id}@{template_version}``
  (:func:`rule_draft_idempotency_key`);
* payload :class:`RuleDraftPayload` - ``candidate_id`` (``0`` = a manual
  CLI draft without a candidate), ``ticker``, ``template_id``,
  ``template_version``, ``as_of``, ``screen_result_id`` (optional) and
  ``preference_notes`` (the user's recent dismiss / reject reasons).

**One run** (:func:`draft_rule`):

1. load the exact template version from the rule store, the stock's
   point-in-time factor values (:func:`load_stock_context`: every factor of
   the default registry with value, quality, flags and unit, plus industry
   group and Shenwan 2021 chain) and, when given, the screen result's
   reasons;
2. ask the tier-3 LLM (OpenAI-compatible client, JSON output with the
   schema in the prompt - the explanation agent's PromptedOutput pattern)
   for a :class:`~investment_agent.rules.draft_policy.RuleDraftOutput`;
   every call is logged in ``llm_call`` with purpose ``rule_draft``;
3. validate (:func:`~investment_agent.rules.draft_policy.validate_draft`:
   params exist and lie in ``param_ranges``, rationale cites only non-null
   input factors, no trading-instruction wording, full P1-C1 instance
   validation). On failure the LLM gets **one** retry with every issue fed
   back; a second failure stores nothing;
4. store a ``draft`` (scope ``candidate``, ``drafted_by = llm:<model>``,
   ``draft_rationale`` = JSON of per-param reasons, overall rationale and
   watch points) through :class:`~investment_agent.rules.store.RuleStore`.
   Idempotent: an existing draft with the same content hash for the same
   listing / scope / template version is returned instead of a new version,
   and a retried run that already stored its draft reuses it without
   another LLM call.

**Outcomes.** Stored (or reused) -> run ``success`` with
``metadata.rule_instance_id``. Draft unusable after the retry -> run
``partial`` with ``error_summary = "draft_unavailable: ..."`` (the
candidate shows "草稿不可用" and the user may write the rule by hand). LLM
timeouts / rate limits / 5xx raise, so the worker retries the run under the
shared retry policy; terminal errors (auth, unknown template, no listing,
no data) fail the run.

**Completion seam.** After every terminal outcome the handler calls
:func:`notify_draft_result` (from ``finalize``, so worker-settled failures
are covered too). It resolves ``mark_draft_result`` lazily from
``investment_agent.candidates.lifecycle`` (lazily, because the candidates
package imports the rule store) and calls ``mark_draft_result(candidate_id,
*, rule_instance_id, failed, error)``, which opens its own session.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.domain.protocols import LLMClient
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.registry import FactorRegistry, FactorValue
from investment_agent.factors.view import SnapshotView, load_snapshot_view
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.llm.openai_compatible import OpenAICompatibleClient
from investment_agent.rules.draft_policy import (
    DRAFT_SCOPE,
    DraftIssue,
    RuleDraftOutput,
    feedback_message,
    system_prompt,
    validate_draft,
)
from investment_agent.rules.schema import TEMPLATE_ID, RuleInstance, RuleTemplate, canonical_hash
from investment_agent.rules.store import RuleStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.rules import RuleInstanceRecord, ScreenResultRecord
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

RULE_DRAFT = "rule_draft"
DRAFT_PURPOSE = "rule_draft"
MAX_DRAFT_ATTEMPTS = 2
"""One LLM call plus one retry with the validation issues fed back."""
MAX_PREFERENCE_NOTES = 20
MAX_NOTE_CHARS = 500
MAX_ERROR_CHARS = 1000

ClientFactory = Callable[[AsyncSession], LLMClient]


class RuleDraftPayload(BaseModel):
    """Queue payload of a ``rule_draft`` run (fixed contract, see module doc)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: int = Field(ge=0)
    """``0``: manual draft (``ia rules draft``) without a candidate to notify."""
    ticker: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    template_id: str = Field(pattern=TEMPLATE_ID)
    template_version: int = Field(ge=1)
    as_of: date
    screen_result_id: int | None = None
    preference_notes: list[str] = Field(default_factory=list)

    @property
    def template_ref(self) -> str:
        return f"{self.template_id}@{self.template_version}"


def rule_draft_idempotency_key(payload: RuleDraftPayload) -> str:
    return f"{RULE_DRAFT}:{payload.candidate_id}:{payload.template_ref}"


async def enqueue_rule_draft(
    session: AsyncSession,
    payload: RuleDraftPayload,
    *,
    triggered_by: str,
    key: str | None = None,
    max_attempts: int = 3,
) -> tuple[PipelineRun, bool]:
    listing = await ListingRepository(session).get_a_share_listing(payload.ticker)
    return await PipelineRunRepository(session).enqueue(
        kind=RULE_DRAFT,
        idempotency_key=key or rule_draft_idempotency_key(payload),
        triggered_by=triggered_by,
        listing_id=listing.id if listing is not None else None,
        request_json=payload.model_dump(mode="json"),
        max_attempts=max_attempts,
    )


class RuleDraftInputError(RuntimeError):
    """Terminal: the run's inputs do not exist (template, listing, screen result)."""


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Segment:
    """One 主营构成 line of the latest report period (mrd-intake C3; a fact, not a factor)."""

    report_period: date
    kind: str
    name: str
    revenue: float | None
    """CNY."""
    share: float | None
    """Fraction of main-business revenue."""
    margin: float | None
    """Gross margin, fraction."""
    profit: float | None = None


MAX_SEGMENTS = 5
"""Top segments by revenue of the latest period handed to the drafting prompt."""


@dataclass(frozen=True)
class StockContext:
    """Point-in-time factor values and industry classification of one stock."""

    factors: Mapping[str, FactorValue]
    industry: Mapping[str, Any] = field(default_factory=dict)
    segments: tuple[Segment, ...] = ()
    """Latest-period 主营构成, revenue top :data:`MAX_SEGMENTS`; empty when unknown."""


SegmentLoader = Callable[[AsyncSession, str, date], Awaitable[tuple[Segment, ...]]]


async def load_segments(session: AsyncSession, ticker: str, as_of: date) -> tuple[Segment, ...]:
    """Top :data:`MAX_SEGMENTS` 主营构成 lines of the latest period public on ``as_of``.

    Point in time: only captures made by the end of ``as_of`` (Asia/Shanghai)
    and report periods ending on or before ``as_of``.
    """

    from investment_agent.clock import SHANGHAI_TZ
    from investment_agent.storage.repositories.earnings_events import EarningsEventRepository

    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        return ()
    known_by = datetime.combine(as_of, time.max, tzinfo=SHANGHAI_TZ)
    latest = await EarningsEventRepository(session).latest_segments(
        listing.id, as_of=as_of, known_by=known_by
    )
    if latest is None:
        return ()
    return tuple(
        Segment(
            report_period=latest.report_period,
            kind=latest.kind,
            name=row.name,
            revenue=None if row.revenue is None else float(row.revenue),
            share=None if row.share is None else float(row.share),
            margin=None if row.margin is None else float(row.margin),
            profit=None if row.profit is None else float(row.profit),
        )
        for row in latest.rows[:MAX_SEGMENTS]
    )


async def load_stock_context(
    session: AsyncSession,
    ticker: str,
    as_of: date,
    *,
    segment_loader: SegmentLoader = load_segments,
) -> StockContext:
    """Every registered factor on ``as_of`` plus industry group, Shenwan chain and
    the latest 主营构成 (facts only)."""

    view = await load_snapshot_view(session, ticker, as_of)
    return StockContext(
        factors=DEFAULT_REGISTRY.compute_all(view),
        industry=industry_context(view),
        segments=await segment_loader(session, ticker, as_of),
    )


def industry_context(view: SnapshotView) -> dict[str, Any]:
    """Industry group, CSRC code and the Shenwan chain of a snapshot view."""

    from investment_agent.providers.shenwan_directory import default_shenwan_directory

    industry: dict[str, Any] = {
        "group": view.industry_group,
        "groups_version": view.industry_groups_version,
        "csrc": {"code": view.industry_code, "name": view.industry_name},
    }
    if view.industry_match is not None:
        industry["match"] = view.industry_match.describe()
    if view.sw_code is not None:
        directory = default_shenwan_directory()
        industry["shenwan"] = {
            "code": view.sw_code,
            "name": directory.name_of(view.sw_code),
            "chain": [
                {"level": item.level, "code": item.code, "name": item.name}
                for item in directory.chain(view.sw_code)
            ],
        }
    return industry


StockLoader = Callable[[AsyncSession, str, date], Awaitable[StockContext]]


def _number(value: float | None) -> float | None:
    if value is None:
        return None
    return float(f"{value:.6g}")


def factor_input(
    factors: Mapping[str, FactorValue], registry: FactorRegistry = DEFAULT_REGISTRY
) -> dict[str, dict[str, Any]]:
    """Factors as the LLM sees them: value, quality, flags, unit, description."""

    result: dict[str, dict[str, Any]] = {}
    for name in sorted(factors):
        item = factors[name]
        entry: dict[str, Any] = {
            "value": _number(item.value),
            "quality": item.quality,
            "flags": list(item.flags),
        }
        if name in registry:
            meta = registry.get(name)
            entry["unit"] = meta.unit
            entry["description"] = meta.description
        result[name] = entry
    return result


def segments_input(segments: Sequence[Segment]) -> dict[str, Any] | None:
    """The ``facts.business_segments`` paragraph of the prompt (``None`` without data)."""

    if not segments:
        return None
    head = segments[0]
    return {
        "report_period": head.report_period.isoformat(),
        "kind": head.kind,
        "note": (
            "最新报告期主营构成（东方财富，按收入前 5；收入单位 CNY，占比与毛利率为小数）。"
            "仅作事实背景，不是因子，不能作为 cited_factors。"
        ),
        "items": [
            {
                "name": item.name,
                "revenue": _number(item.revenue),
                "share": _number(item.share),
                "margin": _number(item.margin),
            }
            for item in segments[:MAX_SEGMENTS]
        ],
    }


def template_input(template: RuleTemplate) -> dict[str, Any]:
    data = template.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {
        "ref": template.ref,
        **{
            key: data[key]
            for key in (
                "id",
                "version",
                "name",
                "description",
                "calibration_status",
                "applicable_groups",
                "screen",
                "candidate_zones",
                "holding_zones",
                "params",
                "param_ranges",
                "param_meta",
                "thesis",
                "fits",
                "avoids",
                "risks",
                "falsification",
                "horizon",
            )
            if key in data
        },
    }


def build_draft_input(
    *,
    payload: RuleDraftPayload,
    template: RuleTemplate,
    stock: StockContext,
    screen: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The user message (JSON) of a drafting call."""

    notes = [note.strip()[:MAX_NOTE_CHARS] for note in payload.preference_notes if note.strip()]
    group = stock.industry.get("group")
    message: dict[str, Any] = {
        "task": "rule_draft",
        "scope": DRAFT_SCOPE,
        "stock": {
            "ticker": payload.ticker,
            "as_of": payload.as_of.isoformat(),
            "industry": dict(stock.industry),
            "template_applies_to_group": group in template.applicable_groups,
        },
        "template": template_input(template),
        "factors": factor_input(stock.factors),
        "screen_result": dict(screen) if screen is not None else None,
        "preference_notes": notes[-MAX_PREFERENCE_NOTES:],
    }
    facts = segments_input(stock.segments)
    if facts is not None:
        # Facts paragraph (mrd-intake C3): descriptive only, never a scoring input.
        message["facts"] = {"business_segments": facts}
    return message


async def screen_context(
    session: AsyncSession, payload: RuleDraftPayload
) -> dict[str, Any] | None:
    if payload.screen_result_id is None:
        return None
    record = await session.get(ScreenResultRecord, payload.screen_result_id)
    if record is None:
        raise RuleDraftInputError(f"no screen_result {payload.screen_result_id}")
    if record.ticker != payload.ticker:
        raise RuleDraftInputError(
            f"screen_result {record.id} is for {record.ticker}, not {payload.ticker}"
        )
    return {
        "id": record.id,
        "as_of": record.as_of.isoformat(),
        "template_ref": f"{record.template_id}@{record.template_version}",
        "outcome": record.outcome,
        "rank": record.rank,
        "strength": record.strength,
        "reasons": list(record.reasons),
    }


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #


@dataclass
class DraftResult:
    status: Literal["drafted", "reused", "unavailable"]
    template_ref: str
    rule_instance_id: int | None = None
    attempts: int = 0
    model: str | None = None
    issues: list[list[str]] = field(default_factory=list)
    """Issues of each failed attempt (strings), in order."""

    @property
    def ok(self) -> bool:
        return self.rule_instance_id is not None

    @property
    def error(self) -> str | None:
        if self.ok:
            return None
        last = self.issues[-1] if self.issues else []
        text = f"draft_unavailable after {self.attempts} attempt(s): " + "; ".join(last)
        return text[:MAX_ERROR_CHARS]

    def metadata(self) -> dict[str, object]:
        return {
            "draft_status": self.status,
            "template_ref": self.template_ref,
            "rule_instance_id": self.rule_instance_id,
            "llm_attempts": self.attempts,
            "model": self.model,
            "validation_issues": self.issues,
        }


def default_client_factory(settings: Settings) -> ClientFactory:
    def build(session: AsyncSession) -> LLMClient:
        return OpenAICompatibleClient(tier_name="tier3", config=settings.llm_rule_draft, session=session)

    return build


async def _complete(
    factory: ClientFactory, *, system: str, user: str, run_id: int | None
) -> tuple[RuleDraftOutput | None, str | None, list[DraftIssue]]:
    """One logged LLM call: ``(output, model, malformed-output issues)``.

    The call runs in its own session so its ``llm_call`` row commits even
    when the call fails; transport / API errors are re-raised afterwards.
    """

    failure: Exception | None = None
    output: RuleDraftOutput | None = None
    model: str | None = None
    issues: list[DraftIssue] = []
    async with session_scope() as session:
        client = factory(session)
        try:
            result = await client.complete(
                system_prompt=system,
                user_message=user,
                output_model=RuleDraftOutput,
                purpose=DRAFT_PURPOSE,
                pipeline_run_id=run_id,
            )
        except ValidationError as exc:
            issues.append(
                DraftIssue("malformed_output", "$", _short(exc, "output is not valid JSON"))
            )
        except Exception as exc:
            failure = exc
        else:
            model = result.model
            if isinstance(result.output, RuleDraftOutput):
                output = result.output
            else:
                issues.append(DraftIssue("malformed_output", "$", "no structured output"))
    if failure is not None:
        raise failure
    return output, model, issues


def _short(exc: ValidationError, fallback: str) -> str:
    parts = [
        f"{'.'.join(str(p) for p in error['loc']) or '$'}: {error['msg']}"
        for error in exc.errors()[:8]
    ]
    return "; ".join(parts) or fallback


def _rationale_json(record: RuleInstanceRecord) -> dict[str, Any]:
    try:
        data = json.loads(record.draft_rationale or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _drafts_for(
    session: AsyncSession, symbol: str, template: RuleTemplate
) -> list[RuleInstanceRecord]:
    stmt = (
        select(RuleInstanceRecord)
        .where(
            RuleInstanceRecord.symbol == symbol,
            RuleInstanceRecord.scope == DRAFT_SCOPE,
            RuleInstanceRecord.template_id == template.id,
            RuleInstanceRecord.template_version == template.version,
            RuleInstanceRecord.status == "draft",
        )
        .order_by(RuleInstanceRecord.version.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def _store(
    instance: RuleInstance, template: RuleTemplate, *, now: datetime | None
) -> tuple[int, bool]:
    """``(record id, reused)``: an equivalent open draft is returned as is."""

    digest = canonical_hash(instance)
    async with session_scope() as session:
        for record in await _drafts_for(session, instance.symbol, template):
            if record.hash == digest:
                return record.id, True
        record = await RuleStore(session).create_draft(instance, now=now)
        return record.id, False


async def draft_rule(
    payload: RuleDraftPayload,
    *,
    settings: Settings | None = None,
    run_id: int | None = None,
    client_factory: ClientFactory | None = None,
    stock_loader: StockLoader | None = None,
    now: datetime | None = None,
) -> DraftResult:
    """Draft, validate (one retry with feedback) and store; see module doc."""

    if client_factory is None:
        if settings is None:
            raise ValueError("draft_rule needs settings or a client_factory")
        client_factory = default_client_factory(settings)
    loader = stock_loader or load_stock_context
    async with session_scope() as session:
        template = await RuleStore(session).get_template(
            payload.template_id, payload.template_version
        )
        if template is None:
            raise RuleDraftInputError(
                f"unknown template {payload.template_ref}; run `ia rules sync`"
            )
        if run_id is not None:
            # A retried attempt of a run that already stored its draft.
            for record in await _drafts_for(session, payload.ticker, template):
                if _rationale_json(record).get("pipeline_run_id") == run_id:
                    return DraftResult("reused", template.ref, record.id)
        try:
            stock = await loader(session, payload.ticker, payload.as_of)
        except LookupError as exc:
            raise RuleDraftInputError(str(exc)) from exc
        screen = await screen_context(session, payload)
    draft_input = build_draft_input(payload=payload, template=template, stock=stock, screen=screen)
    base_message = json.dumps(draft_input, ensure_ascii=False, sort_keys=True)
    system = system_prompt()
    result = DraftResult("unavailable", template.ref)
    feedback: list[DraftIssue] = []
    for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
        result.attempts = attempt
        user = base_message + (feedback_message(feedback) if feedback else "")
        output, model, issues = await _complete(client_factory, system=system, user=user, run_id=run_id)
        result.model = model or result.model
        instance: RuleInstance | None = None
        if output is not None:
            drafted_by = f"llm:{model or 'unknown'}"
            instance, issues = validate_draft(
                output,
                template=template,
                factors=stock.factors,
                symbol=payload.ticker,
                drafted_by=drafted_by,
                extra={
                    "candidate_id": payload.candidate_id or None,
                    "as_of": payload.as_of.isoformat(),
                    "screen_result_id": payload.screen_result_id,
                    "pipeline_run_id": run_id,
                    "model": model,
                    "llm_attempts": attempt,
                },
            )
        if instance is not None:
            record_id, reused = await _store(instance, template, now=now)
            result.rule_instance_id = record_id
            result.status = "reused" if reused else "drafted"
            log.info(
                "rule_draft.stored",
                ticker=payload.ticker,
                template=template.ref,
                rule_instance_id=record_id,
                reused=reused,
                attempts=attempt,
            )
            return result
        result.issues.append([str(issue) for issue in issues])
        feedback = issues
        log.warning(
            "rule_draft.invalid",
            ticker=payload.ticker,
            template=template.ref,
            attempt=attempt,
            issues=[issue.code for issue in issues],
        )
    return result


# --------------------------------------------------------------------------- #
# Completion seam
# --------------------------------------------------------------------------- #

_HOOK_MODULES = ("investment_agent.candidates.lifecycle",)


def _resolve_mark_draft_result() -> Callable[..., Any] | None:
    for module_name in _HOOK_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Only "the candidate module does not exist (yet)" is expected;
            # a missing dependency *inside* it must surface.
            if exc.name is not None and module_name.startswith(exc.name):
                continue
            raise
        hook: object = getattr(module, "mark_draft_result", None)
        if callable(hook):
            return hook
    return None


async def notify_draft_result(
    candidate_id: int,
    *,
    rule_instance_id: int | None,
    failed: bool,
    error: str | None,
) -> bool:
    """Tell the candidate lifecycle a draft finished; ``True`` when delivered.

    Resolves ``mark_draft_result`` from ``investment_agent.candidates.lifecycle``
    lazily (the candidates package imports the rule store) and calls ``mark_draft_result(candidate_id, *, rule_instance_id, failed,
    error)``; a coroutine result is awaited. ``candidate_id == 0`` (manual
    draft) and a missing hook are logged and skipped.
    """

    if candidate_id <= 0:
        return False
    hook = _resolve_mark_draft_result()
    if hook is None:
        log.info(
            "rule_draft.notify.unwired",
            candidate_id=candidate_id,
            rule_instance_id=rule_instance_id,
            failed=failed,
        )
        return False
    outcome = hook(candidate_id, rule_instance_id=rule_instance_id, failed=failed, error=error)
    if inspect.isawaitable(outcome):
        await outcome
    return True


# --------------------------------------------------------------------------- #
# Job handler
# --------------------------------------------------------------------------- #


class RuleDraftHandler(BaseJobHandler):
    kind = RULE_DRAFT

    def __init__(
        self,
        *,
        settings: Settings,
        client_factory: ClientFactory | None = None,
        stock_loader: StockLoader | None = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory
        self.stock_loader = stock_loader

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        payload = RuleDraftPayload.model_validate(job.request_json)

        async def work() -> DraftResult:
            return await draft_rule(
                payload,
                settings=self.settings,
                run_id=job.run_id,
                client_factory=self.client_factory,
                stock_loader=self.stock_loader,
            )

        result = await ctx.run_with_heartbeat(job.lease, work)
        metadata = {
            "ticker": payload.ticker,
            "candidate_id": payload.candidate_id,
            "as_of": payload.as_of.isoformat(),
            **result.metadata(),
        }
        async with session_scope() as session:
            runs = PipelineRunRepository(session)
            if result.ok:
                await runs.mark_success(job.run_id, metadata, lease=job.lease)
            else:
                await runs.mark_partial(
                    job.run_id, result.error or "draft_unavailable", metadata, lease=job.lease
                )
        log.info(
            "rule_draft.run.finished",
            run_id=job.run_id,
            ticker=payload.ticker,
            template=result.template_ref,
            status=result.status,
            attempts=result.attempts,
        )
        return "completed" if result.ok else "partial"

    async def finalize(self, job: ClaimedJob) -> None:
        """Notify the candidate of a terminal outcome (success / partial / failed)."""

        try:
            async with session_scope() as session:
                run = await PipelineRunRepository(session).get(job.run_id)
                if run is None or run.status not in {"success", "partial", "failed"}:
                    return  # retry scheduled or lease lost: not terminal for us
                status, error = run.status, run.error_summary
                metadata = dict(run.metadata_json or {})
            raw_candidate = job.request_json.get("candidate_id")
            if not isinstance(raw_candidate, int):
                return
            rule_id = metadata.get("rule_instance_id")
            await notify_draft_result(
                raw_candidate,
                rule_instance_id=rule_id if isinstance(rule_id, int) else None,
                failed=status != "success",
                error=None if status == "success" else error,
            )
        except Exception:
            log.exception("rule_draft.notify_failed", run_id=job.run_id)


__all__ = [
    "DRAFT_PURPOSE",
    "MAX_DRAFT_ATTEMPTS",
    "RULE_DRAFT",
    "DraftResult",
    "RuleDraftHandler",
    "RuleDraftInputError",
    "RuleDraftPayload",
    "StockContext",
    "build_draft_input",
    "draft_rule",
    "enqueue_rule_draft",
    "load_stock_context",
    "notify_draft_result",
    "rule_draft_idempotency_key",
]
