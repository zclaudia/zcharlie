"""One native Agent run with versioned private history and safe durable audit.

AG08 supplies trusted deps, toolsets, UsageLimits and cancellation; this module
never emits public task events or considers model output a published opinion.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic_ai import CancellationToken, DeferredToolResults, RunContext
from pydantic_ai.agent import EventStreamHandler
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.context import project_history, reusable_context_sources
from investment_agent.agent.model import AgentModelConfig, HTTPTrace, build_agent, create_model
from investment_agent.investor.common import FieldError
from investment_agent.storage.models.agent import AgentModelRun

FRAMEWORK_VERSION: Literal["2.31.1"] = "2.31.1"


class PrivateHistory(BaseModel):
    """Private checkpoint material: may contain reasoning and tool evidence.

    Persist through TaskService.checkpoint only; never serialize as a TaskEvent.
    Provider/config changes require explicit reconstruction from business state.
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    framework: Literal["pydantic-ai"] = "pydantic-ai"
    framework_version: Literal["2.31.1"] = FRAMEWORK_VERSION
    config_version: str
    messages_json: str

    def messages(self, config: AgentModelConfig) -> list[ModelMessage]:
        if config.version != self.config_version:
            raise ValueError("History configuration changed; reconstruct context from business state")
        return ModelMessagesTypeAdapter.validate_json(self.messages_json)


class RunOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    call_id: UUID
    output: Any
    history: PrivateHistory
    usage: dict[str, Any] | None


class AgentRunError(RuntimeError):
    """Only safe classification and correlation ID cross the application boundary.

    ``transient`` marks connection/timeout-class provider failures that the
    shared worker retry policy would retry; the executor converts them into a
    worker retry instead of a permanent task failure.
    """

    def __init__(self, call_id: UUID, code: str, *, field_errors: tuple[FieldError, ...] = (),
                 transient: bool = False) -> None:
        self.call_id = call_id
        self.code = code
        self.field_errors = field_errors
        self.transient = transient
        super().__init__(f"Agent run {call_id}: {code}")


def _undelivered_request(exc: BaseException | None) -> bool:
    """Whether a failed request never reached the provider.

    Only connection-establishment failures prove no request bytes were
    delivered, so nothing can have been consumed. Read/write failures after
    connecting stay fail-closed (persisted as unknown usage).
    """
    current = exc
    for _ in range(5):
        if current is None:
            return False
        if isinstance(current, httpx.ConnectError | httpx.ConnectTimeout):
            return True
        current = current.__cause__ or current.__context__
    return False


class RequestLifecycle(Protocol):
    async def reserve(self, estimated_input: int) -> UUID: ...
    async def sent(self, reservation: UUID) -> None: ...
    async def observe_tools(self, count: int) -> None: ...
    async def settle(self, reservation: UUID, usage: dict[str, int] | None, *, sent: bool) -> None: ...


class _BudgetedModel(WrapperModel):
    """Native model delegation, not a tool loop. Every framework retry enters here.

    HTTPTrace establishes certainty independently from framework default-zero
    usage. The send hook rechecks policy after encoding and before actual I/O.
    """

    def __init__(self, wrapped: Model, lifecycle: RequestLifecycle, trace: HTTPTrace,
                 injected: bool, known_sources: tuple[dict[str, Any], ...] = ()) -> None:
        super().__init__(wrapped)
        self.lifecycle, self.trace, self.injected = lifecycle, trace, injected
        self.active: UUID | None = None
        self.fixture_usage: list[dict[str, int] | None] = []
        self.projection_sizes: dict[str, int] = {}
        self.validation_repairs: list[dict[str, Any]] = []
        self.known_sources = known_sources

    async def before_send(self) -> None:
        assert self.active is not None
        await self.lifecycle.sent(self.active)

    async def reserve_wire(self, request: httpx.Request) -> None:
        # Reserve the actual serialized payload, not framework repr/schema copies.
        # UTF-8 bytes remain a conservative estimate, NOT a guessed tokenizer ratio.
        body = json.loads(request.content)
        sizes = {key + "_bytes": len(json.dumps(body.get(key, []), ensure_ascii=False, separators=(",", ":")).encode())
                 for key in ("messages", "tools", "response_format")}
        estimate = len(request.content)
        diagnostic = {"basis": "wire_utf8_bytes", "input_estimate": estimate, **sizes, **self.projection_sizes, "admitted": False}
        self.trace.request_sizes.append(diagnostic)
        self.active = await self.lifecycle.reserve(max(1, estimate))
        diagnostic["admitted"] = True

    async def _begin(self, messages: list[ModelMessage], params: ModelRequestParameters) -> UUID | None:
        latest = messages[-1] if messages else None
        feedback = [p for p in latest.parts if isinstance(p, RetryPromptPart)] if isinstance(latest, ModelRequest) else []
        if feedback:
            for part in feedback:
                errors = []
                for error in part.content if isinstance(part.content, list) else []:
                    # Persist schema locations/types only, never input values,
                    # model drafts, source excerpts or provider error bodies.
                    location = [v if isinstance(v, int) or isinstance(v, str) and v.isascii() and v.replace("_", "").isalnum() else "<dynamic>"
                                for v in error.get("loc", ())]
                    errors.append({"type": error.get("type"), "loc": location})
                self.validation_repairs.append({"tool": part.tool_name, "errors": errors[:20]})
            repair = getattr(self.lifecycle, "validation_retry", None)
            if repair is not None:
                repair(part.tool_name)
        if not self.injected:
            # Encoding happens inside the SDK. Reserve at the HTTP hook before I/O.
            return None
        # Byte length deliberately overestimates ordinary text; providers can
        # still exceed estimates, and settlement always records actual overage.
        exclude = {i: {"instructions"} for i, message in enumerate(messages) if isinstance(message, ModelRequest)} if params.instruction_parts is not None else None
        # The SDK prioritizes structured instruction_parts over every historical
        # message.instructions. Count that text once; retain the direct-call fallback.
        estimate = len(ModelMessagesTypeAdapter.dump_json(messages, exclude=exclude)) + len(TypeAdapter(ModelRequestParameters).dump_json(params))
        reservation = await self.lifecycle.reserve(max(1, estimate))
        self.active = reservation
        return reservation

    async def _settle(self, reservation: UUID | None, response: ModelResponse | None,
                      attempts: int, responses: int, sent: bool) -> None:
        reservation = reservation or self.active
        if reservation is None:
            return  # encoding/preflight failed without a reservation or network send
        usage: dict[str, int] | None = None
        metadata = self.trace.responses[responses:]
        if self.injected and response is not None:
            usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        elif (self.trace.attempts - attempts == 1 and len(metadata) == 1 and
              metadata[0]["usage"] is not None and
              {"prompt_tokens", "completion_tokens"} <= metadata[0]["usage"].keys()):
            usage = {"input_tokens": metadata[0]["usage"]["prompt_tokens"],
                     "output_tokens": metadata[0]["usage"]["completion_tokens"]}
        if self.injected and sent:
            self.fixture_usage.append(usage)
        await self.lifecycle.settle(reservation, usage, sent=sent)
        self.active = None
        if response is not None:
            count = sum(isinstance(part, ToolCallPart) and part.tool_name != "final_result" for part in response.parts)
            await self.lifecycle.observe_tools(count)

    @staticmethod
    def _delivered(sent: bool, attempts_now: int, attempts_baseline: int,
                   failure: BaseException | None) -> bool:
        """Whether the request actually went out, so its usage is owed.

        A request whose connection never established delivered nothing and
        cannot have been consumed: its reservation settles released instead of
        blocking the ledger as unknown. Later-stage transport failures keep the
        conservative unknown outcome.
        """
        return sent or (attempts_now > attempts_baseline and not _undelivered_request(failure))

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        reservation = await self._begin(messages, model_request_parameters)
        attempts, responses = self.trace.attempts, len(self.trace.responses)
        result, sent, failure = None, False, None
        try:
            if self.injected:
                await self.before_send()
                sent = True
            projected = messages if self.injected else project_history(messages, known_sources=self.known_sources)
            self.projection_sizes = {"native_history_bytes": len(ModelMessagesTypeAdapter.dump_json(messages)),
                                     "projected_history_bytes": len(ModelMessagesTypeAdapter.dump_json(projected))}
            result = await self.wrapped.request(projected, model_settings, model_request_parameters)
            return result
        except BaseException as exc:
            failure = exc
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await self._settle(reservation, result, attempts, responses,
                                   self._delivered(sent, self.trace.attempts, attempts, failure))

    @asynccontextmanager
    async def request_stream(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                             model_request_parameters: ModelRequestParameters,
                             run_context: RunContext[Any] | None = None) -> AsyncIterator[StreamedResponse]:
        reservation = await self._begin(messages, model_request_parameters)
        attempts, responses = self.trace.attempts, len(self.trace.responses)
        sent, failure = False, None
        try:
            if self.injected:
                await self.before_send()
                sent = True
            projected = messages if self.injected else project_history(messages, known_sources=self.known_sources)
            try:
                async with self.wrapped.request_stream(projected, model_settings, model_request_parameters, run_context) as stream:
                    yield stream
            except BaseException as exc:
                failure = exc
                raise
        finally:
            # No independent production streaming usage observer yet: unknown
            # is persisted and blocks subsequent requests/tools.
            with anyio.CancelScope(shield=True):
                await self._settle(reservation, None, attempts, responses,
                                   self._delivered(sent, self.trace.attempts, attempts, failure))


async def run_agent(
    config: AgentModelConfig,
    *,
    session: AsyncSession,
    output_type: type[BaseModel],
    toolsets: list[AbstractToolset[Any]],
    instructions: str,
    prompt: str | None,
    deps: Any = None,
    history: PrivateHistory | None = None,
    deferred_tool_results: DeferredToolResults | None = None,
    usage_limits: UsageLimits | None = None,
    cancellation_token: CancellationToken | None = None,
    pipeline_run_id: int | None = None,
    task_id: UUID | None = None,
    attempt_id: UUID | None = None,
    model: Model | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    before_model_request: Callable[[], Awaitable[None]] | None = None,
    event_stream_handler: EventStreamHandler[Any] | None = None,
    request_lifecycle: RequestLifecycle | None = None,
    timeout_seconds: float | None = None,
    output_validator: Callable[[RunContext[Any], Any], Awaitable[Any]] | None = None,
) -> RunOutcome:
    """Caller owns audit transaction; persist even when catching AgentRunError.

    Transport retries are disabled. Framework retries remain bounded; HTTP
    attempts and completed framework requests are separately recorded. Failed
    calls may have partial usage; missing usage is unknown, never priced at zero.
    """
    call_id = uuid4()
    started = datetime.now(UTC)
    clock = time.monotonic()
    trace = HTTPTrace(before_request=before_model_request)
    usage = RunUsage()
    client = None
    result = None
    failure: str | None = None
    field_errors: tuple[FieldError, ...] = ()
    transient = False
    cancelled = False
    injected = model is not None
    guarded: _BudgetedModel | None = None
    try:
        config.validate_ready()
        messages = history.messages(config) if history is not None else None
        if model is None:
            model, client = create_model(config, trace, transport=transport)
        if request_lifecycle is not None:
            guarded = _BudgetedModel(model, request_lifecycle, trace, injected,
                                     known_sources=reusable_context_sources(instructions))
            async def before_send() -> None:
                if before_model_request is not None:
                    await before_model_request()
                await guarded.before_send()
            trace.before_request = before_send
            trace.reserve_request = guarded.reserve_wire if not injected else None
            model = guarded
        agent = build_agent(config, output_type=output_type, toolsets=toolsets, instructions=instructions, model=model)
        if output_validator is not None:
            agent.output_validator(output_validator)
        guidance = getattr(request_lifecycle, "guidance", None)
        if guidance is not None:
            @agent.instructions
            async def resource_guidance() -> str:
                return str(await guidance())
        async with asyncio.timeout(timeout_seconds):
            result = await agent.run(prompt, deps=deps, message_history=messages,
                                 deferred_tool_results=deferred_tool_results,
                                 usage=usage, usage_limits=usage_limits,
                                 cancellation_token=cancellation_token,
                                 event_stream_handler=event_stream_handler)
    except asyncio.CancelledError:
        cancelled = True
        failure = "cancelled"
    except Exception as exc:
        # Deliberately omit str(exc), provider bodies, URLs, prompts and tool arguments.
        from investment_agent.agent.execution import ResearchValidationError
        if isinstance(exc, ResearchValidationError):
            failure = exc.code
            field_errors = exc.field_errors
        else:
            failure = trace.guard_error or type(exc).__name__
            # Keep the shared worker retry classification: connection/timeout
            # failures must reach the worker retry path instead of failing the
            # task permanently. HTTP status retries stay executor-owned.
            from investment_agent.research.retry_policy import is_transient_error
            transient = (trace.guard_error is None and not isinstance(exc, ModelHTTPError)
                         and is_transient_error(exc))
    finally:
        if client is not None:
            await client.close()
    # FunctionModel is a deterministic fixture; production certainty comes from
    # actual response metadata rather than RunUsage's default-zero fields.
    fixture_known = injected and (result is not None if guarded is None else bool(guarded.fixture_usage) and all(u is not None for u in guarded.fixture_usage))
    known = fixture_known or bool(trace.responses) and all(
        r["usage"] is not None and {"prompt_tokens", "completion_tokens"} <= r["usage"].keys()
        for r in trace.responses
    ) and len(trace.responses) == trace.attempts
    fixture_input = sum(u["input_tokens"] for u in guarded.fixture_usage if u is not None) if guarded is not None else usage.input_tokens
    fixture_output = sum(u["output_tokens"] for u in guarded.fixture_usage if u is not None) if guarded is not None else usage.output_tokens
    usage_data = {
        "input_tokens": fixture_input if injected else sum(r["usage"]["prompt_tokens"] for r in trace.responses),
        "output_tokens": fixture_output if injected else sum(r["usage"]["completion_tokens"] for r in trace.responses),
    } if known else None
    row = AgentModelRun(
        id=str(call_id), pipeline_run_id=pipeline_run_id,
        task_id=str(task_id) if task_id else None,
        attempt_id=str(attempt_id) if attempt_id else None,
        config_version=config.version, config_snapshot={**config.snapshot(), "request_sizes": trace.request_sizes,
            "validation_repairs": guarded.validation_repairs if guarded else []},
        framework_version=FRAMEWORK_VERSION,
        started_at=started, latency_ms=int((time.monotonic() - clock) * 1000),
        success=result is not None, error_code=failure,
        framework_requests=usage.requests, http_attempts=None if injected else trace.attempts,
        http_responses=trace.responses, usage=usage_data,
        usage_unknown_reason=None if known else ("streaming_usage_not_observed" if any(r.get("streaming") for r in trace.responses) else "provider_usage_missing_or_incomplete"),
        cost_usd=None, cost_unknown_reason="verified_price_unavailable",
    )
    session.add(row)
    await session.flush()
    if cancelled:
        raise asyncio.CancelledError
    if result is None:
        raise AgentRunError(call_id, failure or "unknown_failure", field_errors=field_errors,
                            transient=transient) from None
    return RunOutcome(call_id=call_id, output=result.output, usage=usage_data,
                      history=PrivateHistory(config_version=config.version,
                                             messages_json=result.all_messages_json().decode()))
