"""Authenticated simulation workbench; HTTP never executes a model or collector."""

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import select, update
from starlette.responses import JSONResponse

from investment_agent.agent.model import AgentConfigurationError
from investment_agent.api.auth import get_settings, verify_api_key_or_session
from investment_agent.api.schemas.simulation import (
    CorporateActionImport,
    CreateSimulation,
    DetailOut,
    EvidenceImport,
    ExperimentOut,
    HttpID,
    ParticipantOut,
    QueuedOut,
    RevisionWrite,
    RoundOut,
)
from investment_agent.clock import SHANGHAI_TZ
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.domain.llm_catalog import CatalogError, parse_catalog
from investment_agent.domain.time import as_utc
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    UnknownTradingCalendarYearError,
    load_trading_calendar,
)
from investment_agent.settings.loader import (
    DEFAULT_APP_YAML,
    configured_calendar_path,
    resolved_calendar_path,
)
from investment_agent.settings.resolution import ModelUnavailableError
from investment_agent.simulation.artifacts import safe_export, secret_key
from investment_agent.simulation.contracts import (
    CostRecord,
    ExperimentConfig,
    ParticipantConfig,
    RulePackage,
    SimulationAccount,
    SlippageConfig,
)
from investment_agent.simulation.data import content_hash, execution_eligibility, next_trading_day
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.ledger import CorporateAction
from investment_agent.simulation.risk import stable_id
from investment_agent.simulation.runtime import (
    builtin_participant,
    freeze_experiment_models,
    validate_experiment_models,
)
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.simulation import (
    SimulationCostRecord,
    SimulationExecutionEvidenceRecord,
    SimulationExperimentRecord,
    SimulationRequestRecord,
)
from investment_agent.storage.repositories.financial_snapshot import effective_available_from
from investment_agent.storage.repositories.pit import end_of_business_day_utc


class SimulationRoute(APIRoute):
    """Validation errors may contain accidentally submitted secrets in input/ctx."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "invalid_input",
                            "message": "模拟请求参数未通过校验，请核对字段和取值。",
                        }
                    },
                )

        return safe_handler


router = APIRouter(
    prefix="/api/simulations",
    tags=["simulations"],
    route_class=SimulationRoute,
    dependencies=[Depends(verify_api_key_or_session)],
)
SettingsDep = Annotated[Settings, Depends(get_settings)]
ExperimentID = Annotated[
    str, PathParam(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
]
_OMIT = {
    "model_snapshot",
    "artifact_path",
    "raw_ref",
    "_service",
    "agent_base_url",
    "base_url",
    "endpoint_hash",
    "http_requests",
    "http_responses",
    "private_messages",
}
_ERROR_STATUS = {
    "not_found": 404,
    "scope_mismatch": 404,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "invalid_transition": 409,
    "reconciliation_failed": 409,
    "incomplete_data": 409,
    "settlement_blocked": 409,
    "future_evidence": 422,
    "deadline_passed": 409,
}


def _now() -> datetime:
    return datetime.now(UTC)


def public(value: Any) -> Any:
    """Explicitly omit internal model snapshots, endpoints and artifact paths."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(k): public(v)
            for k, v in value.items()
            if str(k) not in _OMIT and not secret_key(str(k))
        }
    if isinstance(value, (list, tuple)):
        return [public(item) for item in value]
    return safe_export(value)


def _error(exc: SimulationError) -> HTTPException:
    # Codes are useful recovery state; provider exception text is never an HTTP response.
    return HTTPException(
        _ERROR_STATUS.get(exc.code, 422),
        detail={
            "code": exc.code,
            "message": "模拟操作未完成，请核对实验状态、版本和数据门槛。",
            "retryable": exc.retryable,
        },
    )


def _calendar(settings: Settings) -> TradingCalendar:
    try:
        return load_trading_calendar(
            resolved_calendar_path(
                configured_calendar_path(DEFAULT_APP_YAML), data_dir=Path(settings.data_dir)
            )
        )
    except (OSError, ValueError, yaml.YAMLError):
        raise HTTPException(
            409,
            detail={
                "code": "calendar_unavailable",
                "message": "交易日历不可用，请在数据健康页检查。",
            },
        ) from None


async def service(settings: SettingsDep) -> AsyncIterator[SimulationService]:
    try:
        async with session_scope() as session:
            yield SimulationService(
                session, data_dir=Path(settings.data_dir), calendar=_calendar(settings), now=_now
            )
    except SimulationError as exc:
        raise _error(exc) from None
    except UnknownTradingCalendarYearError:
        raise HTTPException(
            409, detail={"code": "calendar_unreviewed", "message": "交易日历未覆盖所选日期。"}
        ) from None
    except (ModelUnavailableError, AgentConfigurationError, CatalogError):
        raise HTTPException(
            422, detail={"code": "model_unavailable", "message": "所选模型不可用或尚未配置。"}
        ) from None
    except ValueError:
        raise HTTPException(
            422, detail={"code": "invalid_config", "message": "模拟参数或证据未通过校验。"}
        ) from None


Service = Annotated[SimulationService, Depends(service)]


def _key(body_key: str | None, header: str | None) -> str:
    if body_key and header and body_key != header:
        raise HTTPException(
            422, detail={"code": "idempotency_conflict", "message": "请求头与正文幂等键不一致。"}
        )
    key = body_key or header or ""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", key):
        raise HTTPException(
            422, detail={"code": "idempotency_required", "message": "请提供 Idempotency-Key。"}
        )
    return key


async def _check_http_result(
    svc: SimulationService,
    result: Any,
    *,
    kind: str,
    experiment_id: str | None,
    participant_id: str | None,
    key: str,
) -> None:
    if not isinstance(result, dict):
        raise SimulationError("reconciliation_failed", "invalid cached HTTP result shape")
    if kind == "experiment":
        value = ExperimentOut.model_validate(result)
        expected_id = experiment_id or stable_id("experiment", key)
        authoritative = await svc.get(expected_id)
        if value.id != expected_id or value.config_hash != authoritative.config_hash:
            raise SimulationError("scope_mismatch", "cached experiment differs from request scope")
    elif kind == "queued":
        value_job = QueuedOut.model_validate(result)
        job = await svc.session.get(PipelineRun, value_job.run_id)
        if (
            job is None
            or value_job.id != str(job.id)
            or job.kind != "simulation_cycle"
            or job.request_json.get("experiment_id") != experiment_id
        ):
            raise SimulationError("scope_mismatch", "cached task differs from request scope")
    elif kind == "evidence":
        proof = await svc.session.get(SimulationExecutionEvidenceRecord, result.get("id"))
        if (
            proof is None
            or proof.experiment_id != experiment_id
            or proof.source_kind != "user"
            or result.get("source_kind") != "user"
        ):
            raise SimulationError(
                "scope_mismatch", "cached execution proof differs from request scope"
            )
    elif kind == "account":
        value_account = SimulationAccount.model_validate(result)
        if experiment_id is None or participant_id is None:
            raise SimulationError("scope_mismatch", "HTTP account scope is missing")
        authoritative_account = await svc.account(experiment_id, participant_id)
        if any(
            getattr(value_account, field) != getattr(authoritative_account, field)
            for field in ("experiment_id", "participant_id", "account_id", "initial_cash")
        ):
            raise SimulationError("scope_mismatch", "cached account differs from request scope")


async def _write(
    svc: SimulationService,
    *,
    scope: str,
    key: str,
    body: object,
    operation: Callable[[], Awaitable[Any]],
    result_kind: Literal["experiment", "queued", "evidence", "account"] = "experiment",
    experiment_id: str | None = None,
    participant_id: str | None = None,
) -> Any:
    # HTTP replay uses the friendly request, so a later catalog edit cannot turn
    # an already committed create retry into a different model snapshot.
    await svc.session.execute(
        update(SimulationExperimentRecord)
        .values(revision=SimulationExperimentRecord.revision)
        .execution_options(synchronize_session=False)
    )
    digest = content_hash(body)
    previous = await svc.session.get(SimulationRequestRecord, (scope, key))
    if previous is not None:
        if previous.input_hash != digest:
            raise SimulationError("idempotency_conflict", "request key reused with different body")
        if previous.result_id != str(previous.payload.get("id", "result")):
            raise SimulationError("reconciliation_failed", "HTTP result index differs from payload")
        await _check_http_result(
            svc,
            previous.payload,
            kind=result_kind,
            experiment_id=experiment_id,
            participant_id=participant_id,
            key=key,
        )
        return public(previous.payload)
    result = public(await operation())
    await _check_http_result(
        svc,
        result,
        kind=result_kind,
        experiment_id=experiment_id,
        participant_id=participant_id,
        key=key,
    )
    svc.session.add(
        SimulationRequestRecord(
            scope=scope,
            idempotency_key=key,
            input_hash=digest,
            result_id=str(result.get("id", "result")) if isinstance(result, dict) else "result",
            payload={"items": result} if isinstance(result, list) else result,
        )
    )
    await svc.session.flush()
    return result


def _friendly_config(
    body: CreateSimulation, settings: Settings, calendar_version: str
) -> ExperimentConfig:
    rule_values = {
        "version": "experimental-mainboard-v1",
        "fee_schedule": body.fees.schedule(),
        "slippage": SlippageConfig(
            version="experimental-slippage-v1", adverse_rate=body.slippage_rate
        ),
        "calendar_version": calendar_version,
        "max_position_weight": body.max_position_weight,
        "max_historical_turnover_ratio": body.max_historical_turnover_ratio,
    }
    rules = RulePackage.model_validate({"content_hash": "0" * 64, **rule_values})
    rules = RulePackage.model_validate(
        rules.model_dump()
        | {"content_hash": content_hash(rules.model_dump(mode="json", exclude={"content_hash"}))}
    )
    participants = [
        ParticipantConfig(name="现金基准", kind="cash", baseline_version="cash-v1"),
        ParticipantConfig(
            name="等权买入持有", kind="equal_weight", baseline_version="equal-weight-v1"
        ),
    ]
    participants.extend(
        builtin_participant(
            name=a.name, provider_ref=a.provider_ref, model_ref=a.model_ref, budget=a.budget
        )
        for a in body.agents
    )
    config = ExperimentConfig(
        name=body.name,
        universe=body.universe,
        start_date=body.start_date,
        end_date=body.end_date,
        initial_cash=body.initial_cash,
        rebalance_frequency=body.rebalance_frequency,
        mode=body.mode,
        rules=rules,
        participants=tuple(participants),
    )
    return freeze_experiment_models(config, settings)


@router.get("/options")
async def options(settings: SettingsDep) -> dict[str, Any]:
    models = []
    try:
        for provider in parse_catalog(settings.llm_catalog).providers:
            for model in provider.models:
                models.append(
                    {
                        "provider_ref": provider.id,
                        "model_ref": model.id,
                        "name": f"{provider.name} / {model.name}",
                        "available": model.enabled and bool(provider.api_key),
                    }
                )
    except CatalogError:
        pass
    try:
        calendar = _calendar(settings)
        # Leave both a reviewed execution session and its next T+1 sellable
        # session after the last observation date.
        sessions: list[date] = []
        for year in sorted((item.year for item in calendar.years), reverse=True):
            day = date(year, 12, 31)
            while day.year == year and len(sessions) < 3:
                if calendar.is_trading_day(day):
                    sessions.append(day)
                day -= timedelta(days=1)
            if len(sessions) == 3:
                break
        calendar_info = {
            "id": calendar.calendar_id,
            "reviewed_years": [y.year for y in calendar.years],
            "available": True,
            "max_observation_date": str(sessions[2]) if len(sessions) == 3 else None,
        }
    except HTTPException:
        calendar_info = {
            "id": None,
            "reviewed_years": [],
            "available": False,
            "max_observation_date": None,
        }
    return {
        "models": models,
        "calendar": calendar_info,
        "security_scope": "cn_mainboard_common_a",
        "execution_model": "daily_open_v1",
        "fee_policy": "experimental_user_schedule",
        "requires_trusted_session_evidence": True,
        "budget_policy": {
            "currency": "USD",
            "unknown_prices": "conservative_reservation_not_billed_cost",
            "optional_unit_prices": ["input_price_per_million", "output_price_per_million"],
        },
    }


@router.get("/options/stocks")
async def stock_options(
    q: Annotated[str, Query(max_length=64)] = "", limit: Annotated[int, Query(ge=1, le=100)] = 30
) -> list[dict[str, Any]]:
    async with session_scope() as session:
        query = select(Listing, Company).join(Company, Listing.company_id == Company.id)
        if q.strip():
            query = query.where(
                Listing.ticker.contains(q.strip(), autoescape=True)
                | Company.canonical_name.contains(q.strip(), autoescape=True)
            )
        rows = (await session.execute(query.order_by(Listing.ticker).limit(limit * 4))).all()
        result = []
        for listing, company in rows:
            ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
            if not re.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)", ticker):
                continue
            result.append(
                {
                    "listing_id": listing.id,
                    "ticker": ticker,
                    "name": company.canonical_name,
                    "eligibility": "requires_session_evidence",
                }
            )
        return result[:limit]


@router.get("", response_model=list[ExperimentOut])
async def list_simulations(svc: Service) -> Any:
    return public(await svc.list())


@router.post("", status_code=201, response_model=ExperimentOut)
async def create_simulation(
    body: CreateSimulation,
    svc: Service,
    settings: SettingsDep,
    key_header: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> Any:
    key = _key(body.idempotency_key, key_header)

    async def create() -> Any:
        # Dates may fall on holidays/weekends. Conservatively require the two
        # verified sessions strictly following the supplied season end.
        execution_day = next_trading_day(svc.calendar, body.end_date)
        next_trading_day(svc.calendar, execution_day)
        return await svc.create(
            _friendly_config(body, settings, svc.calendar.calendar_id), idempotency_key=key
        )

    return await _write(
        svc,
        scope="http:simulation:create",
        key=key,
        body=body.model_dump(mode="json", exclude={"idempotency_key"}),
        operation=create,
    )


@router.get("/{experiment_id}", response_model=DetailOut)
async def detail(experiment_id: ExperimentID, svc: Service) -> Any:
    return public(await svc.detail(experiment_id))


async def mutate(
    experiment_id: ExperimentID,
    action: Annotated[str, PathParam(pattern=r"^(start|pause|end|run-now|data-refresh)$")],
    body: RevisionWrite,
    svc: Service,
    settings: SettingsDep,
    key_header: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> Any:
    key = _key(body.idempotency_key, key_header)

    async def perform() -> Any:
        experiment = await svc.get(experiment_id)
        if experiment.revision != body.expected_revision:
            raise SimulationError("revision_conflict", "experiment revision changed")
        if action == "start":
            validate_experiment_models(experiment.config, settings)
        from investment_agent.simulation.jobs import wake_experiment

        if action in {"start", "pause", "end"}:
            updated = await getattr(svc, action)(
                experiment_id, expected_revision=body.expected_revision, idempotency_key=key
            )
            if action == "start":
                await wake_experiment(svc.session, experiment_id=experiment_id, now=_now())
            return updated

        if action == "run-now" and experiment.status != "running":
            raise SimulationError("invalid_transition", "only running experiment can decide")
        job, created = await wake_experiment(
            svc.session,
            experiment_id=experiment_id,
            now=_now(),
            force_collection=action == "data-refresh",
        )
        return {"id": str(job.id), "run_id": job.id, "created": created, "status": "queued"}

    return await _write(
        svc,
        scope=f"http:simulation:{experiment_id}:{action}",
        key=key,
        body=body.model_dump(mode="json", exclude={"idempotency_key"}),
        operation=perform,
        experiment_id=experiment_id,
        result_kind="experiment" if action in {"start", "pause", "end"} else "queued",
    )


@router.get("/{experiment_id}/participants", response_model=list[ParticipantOut])
async def participants(experiment_id: ExperimentID, svc: Service) -> Any:
    return public(await svc.participants(experiment_id))


@router.get("/{experiment_id}/participants/{participant_id}")
async def participant(
    experiment_id: ExperimentID, participant_id: ExperimentID, svc: Service
) -> Any:
    choices = await svc.participants(experiment_id)
    selected = next((p for p in choices if p.id == participant_id), None)
    if selected is None:
        raise SimulationError("not_found", "participant missing")
    return {
        "participant": public(selected),
        "account": public(await svc.account(experiment_id, participant_id)),
    }


@router.get("/{experiment_id}/participants/{participant_id}/account")
async def account(experiment_id: ExperimentID, participant_id: ExperimentID, svc: Service) -> Any:
    return public(await svc.account(experiment_id, participant_id))


@router.get("/{experiment_id}/rounds", response_model=list[RoundOut])
async def rounds(experiment_id: ExperimentID, svc: Service) -> Any:
    return public(await svc.rounds(experiment_id))


@router.get("/{experiment_id}/rounds/{round_id}", response_model=RoundOut)
async def round_detail(experiment_id: ExperimentID, round_id: ExperimentID, svc: Service) -> Any:
    return public(await svc.get_round(experiment_id, round_id))


async def records(
    experiment_id: ExperimentID,
    collection: Annotated[
        str, PathParam(pattern=r"^(decisions|orders|fills|nav|reviews|leaderboard)$")
    ],
    svc: Service,
    participant_id: Annotated[HttpID | None, Query()] = None,
) -> Any:
    if collection == "leaderboard":
        return public(await svc.leaderboard(experiment_id))
    return public(await getattr(svc, collection)(experiment_id, participant_id))


@router.get("/{experiment_id}/availability")
async def availability(
    experiment_id: ExperimentID, svc: Service, trade_date: date | None = None
) -> dict[str, Any]:
    experiment = await svc.get(experiment_id)
    now = _now()
    day = trade_date or now.astimezone(SHANGHAI_TZ).date()
    try:
        trading = svc.calendar.is_trading_day(day)
    except UnknownTradingCalendarYearError:
        return {
            "trade_date": str(day),
            "ready": False,
            "missing": ["calendar_unreviewed"],
            "securities": [],
        }
    missing = [] if trading else ["not_trading_day"]
    if not experiment.config.start_date <= day <= experiment.config.end_date:
        missing.append("outside_experiment_season")
    try:
        execution_day = next_trading_day(svc.calendar, day)
    except UnknownTradingCalendarYearError:
        missing.append("execution_calendar_unreviewed")
    else:
        try:
            next_trading_day(svc.calendar, execution_day)
        except UnknownTradingCalendarYearError:
            missing.append("sellable_calendar_unreviewed")
    if now < datetime.combine(day, time(15), SHANGHAI_TZ):
        missing.append("session_not_closed")
    securities = []
    for listing_id in experiment.config.universe:
        proof = await execution_eligibility(
            svc.session,
            experiment_id=experiment_id,
            listing_id=listing_id,
            trade_date=day,
            cutoff=now,
        )
        bar = await svc.session.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.listing_id == listing_id,
                MarketSnapshot.trade_date == day,
                MarketSnapshot.adjust == "none",
                MarketSnapshot.captured_at <= now,
                MarketSnapshot.is_backfill.is_(False),
            )
            .order_by(MarketSnapshot.version.desc())
            .limit(1)
        )
        gaps = list(proof.missing) + ([] if bar else ["raw_market_missing"])
        if bar is not None and (
            not all(v.is_finite() for v in (bar.open, bar.high, bar.low, bar.close, bar.volume))
            or bar.trade_status not in {0, 1}
            or bar.is_st is None
            or min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.volume < 0
            or (bar.amount is not None and (not bar.amount.is_finite() or bar.amount < 0))
            or bar.high < max(bar.open, bar.close)
            or bar.low > min(bar.open, bar.close)
        ):
            gaps.append("raw_market_invalid")
        if (
            bar is not None
            and bar.amount is None
            and not (bar.trade_status == 0 and bar.volume.is_finite() and bar.volume == 0)
        ):
            gaps.append("historical_turnover_missing")
        if (
            proof.evidence is not None
            and proof.evidence.rule_version != experiment.config.rules.version
        ):
            gaps.append("reservation_rule_mismatch")
        if bar is not None and as_utc(bar.captured_at) < datetime.combine(
            day, time(15), SHANGHAI_TZ
        ):
            gaps.append("raw_market_not_final")
        if bar is not None and proof.evidence is not None and bar.is_st != proof.evidence.is_st:
            gaps.append("security_status_conflict")
        financials = (
            await svc.session.scalars(
                select(FinancialSnapshot).where(
                    FinancialSnapshot.listing_id == listing_id,
                    FinancialSnapshot.captured_at <= now,
                    FinancialSnapshot.report_period <= day,
                    FinancialSnapshot.availability != "unknown",
                    FinancialSnapshot.available_from.is_not(None),
                )
            )
        ).all()
        financial = next(
            (
                row
                for row in financials
                if (effective := effective_available_from(row.fields, row.available_from))
                is not None
                and end_of_business_day_utc(effective) <= now
            ),
            None,
        )
        if financial is None:
            gaps.append("financial_missing")
        securities.append(
            {
                "listing_id": listing_id,
                "ready": not gaps,
                "missing": gaps,
                "evidence_id": proof.evidence_id,
                "evidence_hash": proof.evidence_hash,
            }
        )
    round_gaps = [
        {
            "round_id": item.id,
            "status": item.status,
            "missing": list(await svc.round_data_gaps(experiment_id, item.id)),
        }
        for item in await svc.rounds(experiment_id)
    ]
    return {
        "trade_date": str(day),
        "ready": not missing and all(s["ready"] for s in securities),
        "missing": missing,
        "securities": securities,
        "rounds": round_gaps,
    }


@router.get("/{experiment_id}/export")
async def export(experiment_id: ExperimentID, svc: Service) -> Response:
    payload = public(await svc.export(experiment_id))
    payload["costs"] = await _cost_records(svc, experiment_id)
    payload["resource_policy"] = {
        "unknown_prices": "conservative_reservation_not_billed_cost",
        "returned_model": "provider_reported_identifier_when_available",
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="simulation-{experiment_id}.json"'},
    )


async def _cost_records(
    svc: SimulationService,
    experiment_id: str,
    participant_id: str | None = None,
) -> list[dict[str, Any]]:
    await svc.get(experiment_id)
    query = select(SimulationCostRecord).where(SimulationCostRecord.experiment_id == experiment_id)
    if participant_id is not None:
        await svc.account(experiment_id, participant_id)
        query = query.where(SimulationCostRecord.participant_id == participant_id)
    rows = (await svc.session.scalars(query.order_by(SimulationCostRecord.id))).all()
    records = []
    for row in rows:
        try:
            cost = CostRecord.model_validate(row.payload)
        except ValidationError:
            raise SimulationError("reconciliation_failed", "invalid stored cost contract") from None
        if any(
            getattr(row, field) != getattr(cost, field)
            for field in (
                "id",
                "experiment_id",
                "participant_id",
                "account_id",
                "round_id",
                "call_key",
                "status",
            )
        ):
            raise SimulationError("reconciliation_failed", "cost index differs from payload")
        account = await svc.account(experiment_id, cost.participant_id)
        await svc.get_round(experiment_id, cost.round_id)
        if (
            cost.experiment_id != experiment_id
            or cost.account_id != account.account_id
            or (participant_id is not None and cost.participant_id != participant_id)
        ):
            raise SimulationError("scope_mismatch", "cost belongs to another account")
        records.append(public(cost))
    return records


@router.get("/{experiment_id}/costs")
async def costs(
    experiment_id: ExperimentID,
    svc: Service,
    participant_id: Annotated[HttpID | None, Query()] = None,
) -> Any:
    return {
        "records": await _cost_records(svc, experiment_id, participant_id),
        "unknown_prices": "conservative_reservation_not_billed_cost",
    }


@router.post("/{experiment_id}/execution-evidence", status_code=201)
async def evidence_import(
    experiment_id: ExperimentID,
    body: EvidenceImport,
    svc: Service,
    key_header: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> Any:
    key = _key(body.idempotency_key, key_header)

    async def perform() -> Any:
        identity = await svc.ingest_execution_evidence(
            experiment_id, body.evidence, source_kind="user", source_ref=body.source_ref
        )
        return {"id": identity, "source_kind": "user"}

    return await _write(
        svc,
        scope=f"http:simulation:{experiment_id}:evidence",
        key=key,
        body=body.model_dump(mode="json", exclude={"idempotency_key"}),
        operation=perform,
        experiment_id=experiment_id,
        result_kind="evidence",
    )


@router.post("/{experiment_id}/participants/{participant_id}/corporate-actions")
async def corporate_action(
    experiment_id: ExperimentID,
    participant_id: ExperimentID,
    body: CorporateActionImport,
    svc: Service,
    key_header: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> Any:
    key = _key(body.idempotency_key, key_header)

    async def perform() -> Any:
        action = CorporateAction.model_validate(body.action.model_dump() | {"confirmed_at": _now()})
        return await svc.corporate_action(
            experiment_id,
            participant_id,
            action,
            expected_revision=body.expected_revision,
            idempotency_key=key,
            entitlement_date=body.entitlement_date,
            cash_per_share=body.cash_per_share,
        )

    return await _write(
        svc,
        scope=f"http:simulation:{experiment_id}:{participant_id}:corporate-action",
        key=key,
        body=body.model_dump(mode="json", exclude={"idempotency_key"}),
        operation=perform,
        experiment_id=experiment_id,
        participant_id=participant_id,
        result_kind="account",
    )


# Register bounded catch-all paths last so validation cannot shadow explicit
# availability/export/evidence endpoints of the same path length.
router.add_api_route(
    "/{experiment_id}/{action}", mutate, methods=["POST"], response_model=ExperimentOut | QueuedOut
)
router.add_api_route("/{experiment_id}/{collection}", records, methods=["GET"])
