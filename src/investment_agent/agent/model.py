"""Independent, immutable configuration and native Pydantic AI model factory.

Credentials are resolved by the settings loader, never copied into snapshots.
No provider fallback, task scheduling, business tool loop or publication policy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_ai import Agent, DeferredToolRequests, NativeOutput, PromptedOutput, ToolOutput
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile, openai_model_profile
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import AbstractToolset

from investment_agent.config import Settings
from investment_agent.domain.llm_catalog import ModelRef
from investment_agent.settings.resolution import ModelUnavailableError, resolve_slot

# Resolved invocation parameters. These are not settings keys.
RESOLVED_KEYS = frozenset({
    "agent_provider", "agent_base_url", "agent_model", "agent_profile", "agent_profile_model",
    "agent_output_mode", "agent_supports_json_schema", "agent_supports_json_object",
    "agent_tool_choice_required", "agent_thinking", "agent_timeout_seconds",
    "agent_temperature", "agent_max_tokens", "agent_retries",
})


class AgentConfigurationError(ValueError):
    """Safe, actionable configuration error without endpoint or credential content."""


class AgentModelUnavailableError(AgentConfigurationError):
    """The investor slot points at a model that is missing or disabled."""


def validate_agent_values(
    values: dict[str, Any], *, label: str = "投资 Agent / 主模型"
) -> None:
    """Validate resolved invocation values without opening a client."""

    if not values["agent_base_url"] or not values["agent_model"]:
        raise AgentConfigurationError(f"{label}：未选择模型")
    url = urlsplit(values["agent_base_url"])
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise AgentConfigurationError(
            "Agent endpoint must be an HTTP(S) URL without credentials, query or fragment"
        )
    if (
        values["agent_output_mode"] == "native"
        and not values["agent_supports_json_schema"]
    ):
        raise AgentConfigurationError(
            "Native output requires explicitly verified json_schema support"
        )
    if (
        values["agent_output_mode"] == "prompted"
        and not values["agent_supports_json_object"]
    ):
        raise AgentConfigurationError(
            "Prompted output requires explicitly verified json_object support"
        )


class AgentModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=0)
    values_json: str = Field(repr=False)
    selection: ModelRef | None = None
    thinking_ref: str = ""

    @field_validator("values_json")
    @classmethod
    def _validate_values(cls, value: str) -> str:
        raw = json.loads(value)
        if not isinstance(raw, dict) or set(raw) != RESOLVED_KEYS:
            raise ValueError("Agent configuration keys are incomplete or unsupported")
        # Shape only: these names are the resolved model, not stored settings.
        return json.dumps({key: raw[key] for key in sorted(RESOLVED_KEYS)}, sort_keys=True, separators=(",", ":"))

    @property
    def values(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.values_json))
    credential: SecretStr = Field(exclude=True, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, revision: int) -> AgentModelConfig:
        from investment_agent.settings.agents import parse_model_slots

        slots = parse_model_slots(settings.model_slots).get("investor", {})
        thinking_ref = slots.get("thinking") or ""
        if thinking_ref:
            try:
                resolve_slot(settings, "investor", "thinking")
            except ModelUnavailableError as exc:
                raise AgentModelUnavailableError(str(exc)) from None
        if not slots.get("main"):
            values = {
                "agent_provider": "openai", "agent_base_url": "", "agent_model": "",
                "agent_profile": "openai", "agent_profile_model": "", "agent_output_mode": "prompted",
                "agent_supports_json_schema": False, "agent_supports_json_object": True,
                "agent_tool_choice_required": False, "agent_thinking": "auto",
                "agent_timeout_seconds": 120.0, "agent_temperature": None, "agent_max_tokens": None,
                "agent_retries": 1,
            }
            return cls(revision=revision, values_json=json.dumps(values), credential=SecretStr(""),
                       selection=None, thinking_ref=thinking_ref)
        try:
            resolved = resolve_slot(settings, "investor", "main")
        except ModelUnavailableError as exc:
            raise AgentModelUnavailableError(str(exc)) from None
        return cls(
            revision=revision, values_json=json.dumps(resolved.agent_values()),
            credential=SecretStr(resolved.provider.api_key or ""),
            selection=resolved.selection, thinking_ref=thinking_ref,
        )

    @property
    def version(self) -> str:
        body = json.dumps(self.values, sort_keys=True, separators=(",", ":"))
        if self.selection:
            body += json.dumps(self.selection.model_dump(), sort_keys=True, separators=(",", ":"))
        if self.thinking_ref:
            body += self.thinking_ref
        return hashlib.sha256(body.encode()).hexdigest()

    @classmethod
    def from_values(
        cls,
        values: dict[str, Any],
        *,
        credential: str,
        selection: ModelRef | None = None,
    ) -> AgentModelConfig:
        """Build a config from already-resolved invocation values.

        Used by variable-length consumers (review seats) that resolve their own
        ``provider/model`` reference; the investor slots keep using
        :meth:`from_settings`.
        """

        ordered = {key: values[key] for key in sorted(RESOLVED_KEYS)}
        return cls(
            revision=0,
            values_json=json.dumps(
                ordered, sort_keys=True, separators=(",", ":")
            ),
            credential=SecretStr(credential),
            selection=selection,
        )

    def validate_ready(self) -> None:
        validate_agent_values(self.values)
        if not self.credential.get_secret_value():
            raise AgentConfigurationError("投资 Agent / 主模型：未配置 API Key")

    def snapshot(self) -> dict[str, Any]:
        # Never expose a potentially credential-bearing endpoint in audit/public data.
        values = {k: v for k, v in self.values.items() if k != "agent_base_url"}
        values["endpoint_hash"] = hashlib.sha256(self.values["agent_base_url"].encode()).hexdigest()
        snapshot = {"revision": self.revision, "version": self.version, "values": values}
        if self.selection:
            snapshot["selection"] = self.selection.model_dump()
        if self.thinking_ref:
            snapshot["thinking_ref"] = self.thinking_ref
        return snapshot

    def profile(self) -> OpenAIModelProfile:
        v = self.values
        profile = cast(OpenAIModelProfile, dict(openai_model_profile(v["agent_profile_model"] or v["agent_model"]) or {}))
        profile["supports_json_schema_output"] = v["agent_supports_json_schema"]
        profile["supports_json_object_output"] = v["agent_supports_json_object"]
        profile["openai_supports_tool_choice_required"] = v["agent_tool_choice_required"]
        if v["agent_profile"] == "deepseek":
            profile["openai_chat_thinking_field"] = "reasoning_content"
            profile["openai_chat_send_back_thinking_parts"] = "field"
        return profile


@dataclass
class HTTPTrace:
    """Metadata only; request/response bodies and reasoning never escape the client."""

    before_request: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    reserve_request: Callable[[httpx.Request], Awaitable[None]] | None = field(default=None, repr=False)
    request_sizes: list[dict[str, Any]] = field(default_factory=list)
    guard_error: str | None = None
    attempts: int = 0
    responses: list[dict[str, Any]] = field(default_factory=list)

    async def request(self, request: httpx.Request) -> None:
        try:
            if self.reserve_request is not None:
                await self.reserve_request(request)
            if self.before_request is not None:
                await self.before_request()
        except Exception as exc:
            self.guard_error = type(exc).__name__
            raise
        self.attempts += 1

    async def response(self, response: httpx.Response) -> None:
        # Streaming events remain framework-owned: never consume/buffer SSE here.
        if "text/event-stream" in response.headers.get("content-type", ""):
            self.responses.append({"status": response.status_code, "usage": None, "streaming": True})
            return
        await response.aread()
        usage: dict[str, Any] | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                raw = response.json().get("usage")
                if isinstance(raw, dict):
                    usage = {k: raw[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if type(raw.get(k)) is int and raw[k] >= 0}
            except (ValueError, AttributeError):
                pass
        self.responses.append({"status": response.status_code, "usage": usage})


def build_agent(
    config: AgentModelConfig,
    *,
    output_type: type[BaseModel],
    toolsets: list[AbstractToolset[Any]],
    instructions: str,
    model: Model | None = None,
) -> Agent[Any, Any]:
    """Assemble framework-native tools and validated output; models may be injected for tests."""
    v = config.values
    mode = v["agent_output_mode"]
    output: Any = {"prompted": PromptedOutput, "tool": ToolOutput, "native": NativeOutput}[mode](output_type)
    settings = OpenAIChatModelSettings(timeout=v["agent_timeout_seconds"])
    if v["agent_temperature"] is not None:
        settings["temperature"] = v["agent_temperature"]
    if v["agent_max_tokens"] is not None:
        settings["max_tokens"] = v["agent_max_tokens"]
    if v["agent_thinking"] != "auto":
        settings["extra_body"] = {"thinking": {"type": v["agent_thinking"]}}
    if model is None:
        raise AgentConfigurationError("A model must be supplied by the managed run adapter")
    return Agent(model, instructions=instructions, toolsets=toolsets, output_type=[output, DeferredToolRequests],
                 model_settings=settings, retries=v["agent_retries"])


def _openai_client(
    values: dict[str, Any],
    credential: str,
    trace: HTTPTrace,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncOpenAI:
    http = httpx.AsyncClient(
        transport=transport,
        timeout=values["agent_timeout_seconds"],
        event_hooks={"request": [trace.request], "response": [trace.response]},
    )
    return AsyncOpenAI(
        base_url=values["agent_base_url"],
        api_key=credential,
        http_client=http,
        max_retries=0,
    )


def _model_from_config(
    config: AgentModelConfig, trace: HTTPTrace, transport: httpx.AsyncBaseTransport | None
) -> tuple[OpenAIChatModel, AsyncOpenAI]:
    v = config.values
    client = _openai_client(
        v, config.credential.get_secret_value(), trace, transport
    )
    provider = (
        DeepSeekProvider(openai_client=client)
        if v["agent_provider"] == "deepseek"
        else OpenAIProvider(openai_client=client)
    )
    return (
        OpenAIChatModel(v["agent_model"], provider=provider, profile=config.profile()),
        client,
    )


def model_from_values(
    values: dict[str, Any],
    *,
    credential: str,
    trace: HTTPTrace,
    transport: httpx.AsyncBaseTransport | None = None,
    label: str = "模型",
) -> tuple[OpenAIChatModel, AsyncOpenAI]:
    """Build a native model from resolved ``agent_values()`` (no fallback).

    The review-seat runner resolves its own ``provider/model`` reference and
    calls this builder; the investor path keeps :func:`create_model`.
    """

    validate_agent_values(values, label=label)
    if not credential:
        raise AgentConfigurationError(f"{label}：未配置 API Key")
    config = AgentModelConfig.from_values(values, credential=credential)
    return _model_from_config(config, trace, transport)


def create_model(config: AgentModelConfig, trace: HTTPTrace, *, transport: httpx.AsyncBaseTransport | None = None) -> tuple[OpenAIChatModel, AsyncOpenAI]:
    config.validate_ready()
    return _model_from_config(config, trace, transport)
