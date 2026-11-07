"""Selected-model simulation decisions over frozen public and own-account facts.

No live tools, portfolio writers, investor-slot fallback, provider retries or
provider-controlled identity fields are exposed by this runtime.
"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.model import AgentModelConfig
from investment_agent.agent.runtime import AgentRunError, RunOutcome, run_agent
from investment_agent.clock import SHANGHAI_TZ
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, Text, utc_timestamp
from investment_agent.scheduler.trading_calendar import TradingCalendar
from investment_agent.settings.loader import load_settings
from investment_agent.settings.resolution import resolve_model_ref
from investment_agent.simulation.budget import SessionFactory, SimulationBudget
from investment_agent.simulation.contracts import (
    AccountScope,
    ConfigEntry,
    Decision,
    ExperimentConfig,
    OrderIntent,
    ParticipantConfig,
)
from investment_agent.simulation.data import canonical_json
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.service import SimulationService
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import LeaseLostError, current_lease

SIMULATION_METHOD_REF = "simulation-long-only-v1"
SIMULATION_PROMPT_VERSION = "simulation-decide-v1"
SIMULATION_METHOD = "Use only frozen supplied facts. Compare the investment thesis with counter-evidence. Respect cash, lot sellability, universe, fees and price protection. Submit fixed share quantities or deliberately hold. State review conditions and an observation horizon. Never claim a fill or compute official account returns."
SIMULATION_PROMPT = "You manage one isolated virtual account in a daily-forward simulation. The server binds all identities and revisions. Other participants' work is unavailable. Return one structured decision; active hold is distinct from failures. Cite only provided evidence IDs. You have no tools and cannot obtain new information."


async def _check_worker_lease(session: AsyncSession, pipeline_run_id: int | None) -> None:
    if pipeline_run_id is None:
        return
    lease = current_lease(pipeline_run_id)
    if lease is None:
        raise LeaseLostError(pipeline_run_id, "simulation worker no longer owns this execution")
    fenced = await session.execute(update(PipelineRun).where(
        PipelineRun.id == pipeline_run_id, PipelineRun.status == "running",
        PipelineRun.worker_id == lease.worker_id, PipelineRun.attempt_count == lease.attempt,
    ).values(heartbeat_at=PipelineRun.heartbeat_at).returning(PipelineRun.id))
    if fenced.scalar_one_or_none() is None:
        raise LeaseLostError(pipeline_run_id, "simulation worker no longer owns this execution")


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def builtin_participant(
    *, name: str, provider_ref: str, model_ref: str, **values: object
) -> ParticipantConfig:
    """Friendly builder for UI/API callers; no manual method/prompt hashes."""
    return ParticipantConfig.model_validate(
        dict(
            name=name,
            kind="agent",
            provider_ref=provider_ref,
            model_ref=model_ref,
            method_ref=SIMULATION_METHOD_REF,
            method_hash=text_hash(SIMULATION_METHOD),
            prompt_version=SIMULATION_PROMPT_VERSION,
            prompt_hash=text_hash(SIMULATION_PROMPT),
        )
        | values
    )


def participant_model_ref(config: ParticipantConfig) -> str:
    if config.kind != "agent" or config.provider_ref is None or config.model_ref is None:
        raise SimulationError("invalid_config", "participant has no selected model")
    reference = (
        config.model_ref if "/" in config.model_ref else f"{config.provider_ref}/{config.model_ref}"
    )
    if reference.split("/", 1)[0] != config.provider_ref:
        raise SimulationError("invalid_config", "provider and model references differ")
    return reference


def resolve_participant(config: ParticipantConfig, settings: Settings) -> AgentModelConfig:
    reference = participant_model_ref(config)
    resolved = resolve_model_ref(settings, reference, required=(), label="模拟参赛模型")
    values = resolved.agent_values() | {
        "agent_retries": config.budget.format_repair_attempts,
        "agent_max_tokens": min(resolved.model.max_tokens or 2048, 8192),
        "agent_timeout_seconds": min(resolved.model.timeout_seconds, config.budget.timeout_seconds),
    }
    model = AgentModelConfig.from_values(
        values, credential=resolved.provider.api_key or "", selection=resolved.selection
    )
    model.validate_ready()
    return model


def freeze_participant(config: ParticipantConfig, settings: Settings) -> ParticipantConfig:
    if config.kind != "agent":
        return config
    if config.tools:
        raise SimulationError("invalid_config", "simulation tools are not enabled in this protocol")
    entries = {entry.key: entry.value for entry in config.model_snapshot}
    if set(entries) - {"resolved_model", "method_text", "prompt_text"}:
        raise SimulationError("invalid_config", "unsupported simulation snapshot field")
    method = entries.get(
        "method_text", SIMULATION_METHOD if config.method_ref == SIMULATION_METHOD_REF else ""
    )
    prompt = entries.get(
        "prompt_text",
        SIMULATION_PROMPT if config.prompt_version == SIMULATION_PROMPT_VERSION else "",
    )
    if (
        not method
        or text_hash(method) != config.method_hash
        or not prompt
        or text_hash(prompt) != config.prompt_hash
    ):
        raise SimulationError(
            "invalid_config", "method or prompt text does not match frozen reference hash"
        )
    snapshot = canonical_json(resolve_participant(config, settings).snapshot())
    if "resolved_model" in entries and entries["resolved_model"] != snapshot:
        raise SimulationError(
            "invalid_config",
            "selected model configuration changed; create a new participant version",
        )
    return ParticipantConfig.model_validate(
        config.model_dump()
        | {
            "model_snapshot": (
                ConfigEntry(key="resolved_model", value=snapshot),
                ConfigEntry(key="method_text", value=method),
                ConfigEntry(key="prompt_text", value=prompt),
            )
        }
    )


def freeze_experiment_models(config: ExperimentConfig, settings: Settings) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        config.model_dump()
        | {
            "participants": tuple(
                freeze_participant(participant, settings) for participant in config.participants
            ),
        }
    )


def validate_experiment_models(config: ExperimentConfig, settings: Settings) -> None:
    for participant in config.participants:
        if participant.kind == "agent" and freeze_participant(participant, settings) != participant:
            raise SimulationError(
                "invalid_config", "participant model snapshot was not frozen before creation"
            )


class DecisionOutput(Contract):
    action: Literal["hold", "trade"]
    orders: tuple[OrderIntent, ...] = Field(default=(), max_length=100)
    rationale: Text
    counter_evidence: tuple[Text, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    expected_holding_days: int = Field(gt=0, le=3650)
    review_conditions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def valid_action(self) -> Self:
        if (self.action == "trade") != bool(self.orders):
            raise ValueError("trade requires orders; hold must have none")
        if len({order.listing_id for order in self.orders}) != len(self.orders):
            raise ValueError("one direction per listing")
        return self


async def _audited_run(sessions: SessionFactory, **arguments: Any) -> RunOutcome:
    failure: BaseException | None = None
    result: RunOutcome | None = None
    async with sessions() as audit_session:
        try:
            result = await run_agent(session=audit_session, **arguments)
        except (AgentRunError, asyncio.CancelledError) as error:
            # run_agent flushes its safe audit before raising. Leave this
            # context successfully so the failure audit is committed as well.
            failure = error
    if failure is not None:
        raise failure
    assert result is not None
    return result


async def decide_participant(
    experiment_id: str,
    round_id: str,
    participant_id: str,
    *,
    settings: Settings,
    calendar: TradingCalendar,
    sessions: SessionFactory = session_scope,
    model: Model | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    settings_loader: Callable[[], Awaitable[Settings]] | None = None,
    pipeline_run_id: int | None = None,
) -> Decision:
    async def live_settings() -> Settings:
        return await settings_loader() if settings_loader else await load_settings(base=settings)

    def service(session: AsyncSession) -> SimulationService:
        return SimulationService(session, data_dir=settings.data_dir, calendar=calendar, now=now)

    async with sessions() as session:
        svc = service(session)
        participants = await svc.participants(experiment_id)
        participant = next((item for item in participants if item.id == participant_id), None)
        if participant is None or participant.config.kind != "agent":
            raise SimulationError(
                "scope_mismatch", "selected simulation participant is not an agent"
            )
        account = await svc.account(experiment_id, participant_id)
        previous = [
            d for d in await svc.decisions(experiment_id, participant_id) if d.round_id == round_id
        ]
        runtime_key = f"runtime:{round_id}:{participant_id}"
        replay = next((item for item in previous if item.idempotency_key == runtime_key), None)
        if replay is not None:
            return replay
        round_ = await svc.get_round(experiment_id, round_id)
    scope = AccountScope(
        experiment_id=experiment_id, participant_id=participant_id, account_id=account.account_id
    )
    config = participant.config
    revision = max((item.revision for item in previous), default=-1) + 1
    identity = str(uuid5(NAMESPACE_URL, runtime_key))

    async def fail(code: str, status: str = "failed") -> Decision:
        async with sessions() as session:
            await _check_worker_lease(session, pipeline_run_id)
            svc = service(session)
            current = await svc.account(experiment_id, participant_id)
            failure = Decision.model_validate(
                scope.model_dump()
                | dict(
                    id=identity,
                    round_id=round_id,
                    revision=revision,
                    account_revision=current.revision,
                    status=status,
                    action="no_submission",
                    rationale="Runtime did not submit a trading decision.",
                    failure_code=code,
                    submitted_at=utc_timestamp(now()),
                    idempotency_key=runtime_key,
                )
            )
            return await svc.submit_decision(experiment_id, round_id, participant_id, failure)

    async def guard() -> None:
        async with sessions() as session:
            await _check_worker_lease(session, pipeline_run_id)
            svc = service(session)
            experiment = await svc.get(experiment_id)
            current = await svc.get_round(experiment_id, round_id)
            if experiment.status != "running":
                raise SimulationError("invalid_transition", "simulation experiment is not running")
            if utc_timestamp(now()).astimezone(SHANGHAI_TZ).date() > experiment.config.end_date:
                raise SimulationError("deadline_passed", "simulation decision season has ended")
            if (
                current.status != "collecting_decisions"
                or utc_timestamp(now()) >= current.decision_deadline
            ):
                raise SimulationError("deadline_passed", "simulation decision window closed")
        current_config = freeze_participant(config, await live_settings())
        if current_config != config:
            raise SimulationError("invalid_config", "simulation participant is not frozen")

    budget = SimulationBudget(
        scope=scope,
        round_id=round_id,
        config=config.budget,
        model_ref=participant_model_ref(config),
        guard=guard,
        sessions=sessions,
        now=now,
    )
    previous_costs = await budget.costs()
    if previous_costs:
        uncertain = any(cost.status in {"reserved", "unknown"} for cost in previous_costs)
        return await fail("unknown_call_recovery" if uncertain else "model_output_not_committed",
                          "unknown" if uncertain else "failed")
    try:
        await guard()
        resolved = resolve_participant(config, await live_settings())
        budget.output_tokens_bound = int(resolved.values["agent_max_tokens"])
        async with sessions() as session:
            context = await service(session).private_context(
                experiment_id, round_id, participant_id
            )
        entries = {entry.key: entry.value for entry in config.model_snapshot}
        prompt_context = context.model_dump(mode="json")
        if config.memory_policy == "none":
            prompt_context["matured_reviews"] = []
        # The audit session is untouched until run_agent's final audit flush.
        # Request admission/settlement use independent committed short sessions.
        outcome = await _audited_run(
            sessions,
            config=resolved,
            output_type=DecisionOutput,
            toolsets=[],
            instructions=entries["prompt_text"] + "\n\n" + entries["method_text"],
            prompt=canonical_json(prompt_context),
            model=model,
            request_lifecycle=budget,
            timeout_seconds=min(
                config.budget.timeout_seconds,
                max(0.001, (round_.decision_deadline - utc_timestamp(now())).total_seconds()),
            ),
            usage_limits=UsageLimits(request_limit=config.budget.max_calls_per_round),
            pipeline_run_id=pipeline_run_id,
        )
        await guard()
        output = DecisionOutput.model_validate(outcome.output)
        allowed = {item.id for item in context.common.manifest.evidence}
        if not set(output.evidence_refs) <= allowed:
            return await fail("evidence_outside_manifest", "invalid")
        if budget.overrun:
            return await fail("budget_estimate_overrun")
        decision = Decision.model_validate(
            scope.model_dump()
            | output.model_dump()
            | dict(
                id=identity,
                round_id=round_id,
                revision=revision,
                account_revision=account.revision,
                status="submitted",
                submitted_at=utc_timestamp(now()),
                idempotency_key=runtime_key,
            )
        )
        async with sessions() as session:
            await _check_worker_lease(session, pipeline_run_id)
            return await service(session).submit_decision(
                experiment_id, round_id, participant_id, decision
            )
    except AgentRunError as exc:
        if budget.overrun:
            return await fail("budget_estimate_overrun")
        costs = await budget.costs()
        if budget.stop_reason:
            return await fail(
                budget.stop_reason,
                "invalid" if budget.stop_reason == "invalid_decision" else
                "timeout" if budget.stop_reason == "deadline_passed" else "failed",
            )
        if any(cost.status == "unknown" for cost in costs):
            return await fail("unknown_call", "unknown")
        if exc.code in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}:
            return await fail("model_timeout", "timeout")
        if exc.code in {"UnexpectedModelBehavior", "ValidationError"}:
            return await fail("invalid_output", "invalid")
        return await fail("model_failed")
    except SimulationError as exc:
        return await fail(exc.code, "timeout" if exc.code == "deadline_passed" else "failed")
    except (ValueError, KeyError):
        return await fail("invalid_config", "invalid")


class ReviewOutput(Contract):
    findings: Text
    counter_evidence: tuple[Text, ...] = ()
    improvement_proposals: tuple[Text, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    nav_refs: tuple[str, ...] = Field(min_length=1)


async def review_participants(
    experiment_id: str,
    *,
    settings: Settings,
    calendar: TradingCalendar,
    sessions: SessionFactory = session_scope,
    model: Model | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    settings_loader: Callable[[], Awaitable[Settings]] | None = None,
    pipeline_run_id: int | None = None,
) -> tuple[str, ...]:
    """Append grounded qualitative learning only after the observation matures."""
    from investment_agent.simulation.contracts import SimulationReview

    def service(session: AsyncSession) -> SimulationService:
        return SimulationService(session, data_dir=settings.data_dir, calendar=calendar, now=now)

    async with sessions() as session:
        await _check_worker_lease(session, pipeline_run_id)
        svc = service(session)
        generated = await svc.generate_reviews(experiment_id)
        if (await svc.get(experiment_id)).status != "running":
            return ("observing_inactive_experiment",)
        participants = {item.id: item for item in await svc.participants(experiment_id)}
        decisions = {item.id: item for item in await svc.decisions(experiment_id)}
        all_reviews = await svc.reviews(experiment_id)
        rounds = await svc.rounds(experiment_id)
    outcomes = []
    for observation in generated:
        participant = participants[observation.participant_id]
        if participant.config.kind != "agent" or observation.status != "matured":
            continue
        completed = [
            item
            for item in all_reviews
            if item.decision_id == observation.decision_id
            and item.findings.startswith(("Model review:", "AI review unavailable:"))
        ]
        if completed:
            continue
        decision = decisions[observation.decision_id]
        config = participant.config
        scope = AccountScope(
            experiment_id=experiment_id,
            participant_id=participant.id,
            account_id=participant.account_id,
        )

        async def guard(config: ParticipantConfig = config) -> None:
            async with sessions() as session:
                await _check_worker_lease(session, pipeline_run_id)
                experiment = await service(session).get(experiment_id)
                if experiment.status != "running":
                    raise SimulationError(
                        "invalid_transition", "simulation experiment is not running"
                    )
            if utc_timestamp(now()).astimezone(SHANGHAI_TZ).date() > experiment.config.end_date:
                raise SimulationError("deadline_passed", "simulation review season has ended")
            live = (
                await settings_loader() if settings_loader else await load_settings(base=settings)
            )
            if freeze_participant(config, live) != config:
                raise SimulationError("invalid_config", "review selected model changed")

        budget = SimulationBudget(
            scope=scope,
            round_id=decision.round_id,
            config=config.budget,
            model_ref=participant_model_ref(config),
            guard=guard,
            sessions=sessions,
            now=now,
            purpose=f"review:{decision.id}",
        )
        failure = None
        result = None
        async with sessions() as session:
            svc = service(session)
            navs = tuple(
                item
                for item in await svc.nav(experiment_id, participant.id)
                if item.id in observation.nav_refs and item.created_at <= utc_timestamp(now())
            )
            known_rounds = [item for item in rounds if item.cutoff <= utc_timestamp(now())]
            common = [
                (await svc.common_facts(experiment_id, item.id)).payload() for item in known_rounds
            ]
        allowed_refs = {ref.id for item in known_rounds for ref in item.manifest.evidence}
        allowed_navs = {item.id for item in navs}
        try:
            await guard()
            costs = await budget.costs()
            # Any earlier uncertain request blocks another paid attempt. A
            # successful decision request alone does not block a priced review.
            if any(
                item.status in {"reserved", "unknown"}
                or item.call_key.startswith(f"review:{decision.id}:")
                for item in costs
            ):
                raise SimulationError("unknown_call", "prior call cost or delivery is uncertain")
            live = (
                await settings_loader() if settings_loader else await load_settings(base=settings)
            )
            resolved = resolve_participant(config, live)
            budget.output_tokens_bound = int(resolved.values["agent_max_tokens"])
            result = await _audited_run(
                sessions,
                config=resolved,
                output_type=ReviewOutput,
                toolsets=[],
                instructions=(
                    "Review only the supplied own decision, frozen common facts and deterministic NAV references. "
                    "Give qualitative findings, counter-evidence and testable improvement proposals. "
                    "Do not recalculate returns or quote numerical performance in narrative. "
                    "Preserve the original reason and horizon. Cite only supplied evidence/nav IDs. "
                    "A short price move does not falsify a long business hypothesis. "
                    "Method changes are proposals; never alter frozen rules or prompts."
                ),
                prompt=canonical_json(
                    {
                        "decision": decision.model_dump(mode="json"),
                        "observation": observation.model_dump(mode="json"),
                        "nav": [item.model_dump(mode="json") for item in navs],
                        "common": common,
                    }
                ),
                model=model,
                request_lifecycle=budget,
                timeout_seconds=config.budget.timeout_seconds,
                usage_limits=UsageLimits(request_limit=config.budget.max_calls_per_round),
                pipeline_run_id=pipeline_run_id,
            )
            await guard()
            output = ReviewOutput.model_validate(result.output)
            if (
                not set(output.evidence_refs) <= allowed_refs
                or not set(output.nav_refs) <= allowed_navs
            ):
                raise SimulationError("future_evidence", "model review cited an unprovided fact")
            # Numbers are displayed from referenced NAVs, never generated text.
            narrative = " ".join(
                (output.findings, *output.counter_evidence, *output.improvement_proposals)
            )
            chinese_quantity = re.search(
                r"百分之[零〇一二两三四五六七八九十百千万亿]+|[零〇一二两三四五六七八九十百千万亿]+(?:成|倍|元|股|个百分点)",
                narrative,
            )
            if any(character.isdigit() for character in narrative) or chinese_quantity:
                raise SimulationError(
                    "invalid_decision",
                    "review narrative must leave numerical results to NAV references",
                )
            if budget.overrun:
                raise SimulationError(
                    "budget_exhausted", "review charge exceeded reserved estimate"
                )
        except AgentRunError:
            failure = (
                "budget_estimate_overrun"
                if budget.overrun
                else (budget.stop_reason or "model_failed_or_unknown")
            )
        except (SimulationError, ValueError) as error:
            failure = (
                "budget_estimate_overrun"
                if budget.overrun
                else error.code
                if isinstance(error, SimulationError)
                else "invalid_output"
            )
        async with sessions() as session:
            await _check_worker_lease(session, pipeline_run_id)
            svc = service(session)
            previous = [
                item
                for item in await svc.reviews(experiment_id, participant.id)
                if item.decision_id == decision.id
            ]
            version = max(item.version for item in previous) + 1
            body = observation.model_dump() | {
                "version": version,
                "available_at": utc_timestamp(now()),
                "id": str(uuid5(NAMESPACE_URL, f"model-review:{decision.id}:{version}")),
            }
            if failure:
                body |= {
                    "status": "failed",
                    "memory_eligible": False,
                    "findings": f"AI review unavailable: {failure}",
                    "improvement_proposals": (),
                }
            else:
                body |= {
                    "status": "matured",
                    "memory_eligible": config.memory_policy == "matured_own_reviews",
                    "findings": "Model review: "
                    + output.findings
                    + (
                        " Counter-evidence: " + "; ".join(output.counter_evidence)
                        if output.counter_evidence
                        else ""
                    ),
                    "improvement_proposals": output.improvement_proposals,
                    "evidence_refs": output.evidence_refs,
                    "nav_refs": output.nav_refs,
                }
            await svc.save_model_review(
                experiment_id, participant.id, SimulationReview.model_validate(body)
            )
        outcomes.append(failure or "matured_model_review")
    return tuple(outcomes)
