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
from investment_agent.settings.registry import ENV_KEYS, RuntimeSettings


class AgentConfigurationError(ValueError):
    """Safe, actionable configuration error without endpoint or credential content."""


class AgentModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=0)
    values_json: str = Field(repr=False)
    selection: ModelRef | None = None

    @field_validator("values_json")
    @classmethod
    def _validate_values(cls, value: str) -> str:
        raw = json.loads(value)
        expected = {k for k in ENV_KEYS if k.startswith("agent_") and k != "agent_api_key"}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("Agent configuration keys are incomplete or unsupported")
        validated = RuntimeSettings.model_validate(raw).model_dump(mode="json", include=expected)
        return json.dumps(validated, sort_keys=True, separators=(",", ":"))

    @property
    def values(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.values_json))
    credential: SecretStr = Field(exclude=True, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings, revision: int) -> AgentModelConfig:
        from investment_agent.domain.llm_catalog import parse_catalog
        from investment_agent.settings.llm_catalog import resolve_catalog_settings

        settings = resolve_catalog_settings(settings)
        ref = parse_catalog(settings.llm_catalog).bindings.get("agent")
        values = settings.model_dump(mode="json", include={
            k for k in ENV_KEYS if k.startswith("agent_") and k != "agent_api_key"
        })
        return cls(revision=revision, values_json=json.dumps(values), credential=SecretStr(settings.agent_api_key),
                   selection=ref)

    @property
    def version(self) -> str:
        body = json.dumps(self.values, sort_keys=True, separators=(",", ":"))
        if self.selection:
            body += json.dumps(self.selection.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def validate_ready(self) -> None:
        v = self.values
        if not v["agent_base_url"] or not v["agent_model"] or not self.credential.get_secret_value():
            raise AgentConfigurationError("Agent model is unconfigured: set endpoint, model and agent_api_key")
        url = urlsplit(v["agent_base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise AgentConfigurationError("Agent endpoint must be an HTTP(S) URL without credentials, query or fragment")
        if v["agent_output_mode"] == "native" and not v["agent_supports_json_schema"]:
            raise AgentConfigurationError("Native output requires explicitly verified json_schema support")
        if v["agent_output_mode"] == "prompted" and not v["agent_supports_json_object"]:
            raise AgentConfigurationError("Prompted output requires explicitly verified json_object support")

    def snapshot(self) -> dict[str, Any]:
        # Never expose a potentially credential-bearing endpoint in audit/public data.
        values = {k: v for k, v in self.values.items() if k != "agent_base_url"}
        values["endpoint_hash"] = hashlib.sha256(self.values["agent_base_url"].encode()).hexdigest()
        snapshot = {"revision": self.revision, "version": self.version, "values": values}
        if self.selection:
            snapshot["selection"] = self.selection.model_dump()
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


def create_model(config: AgentModelConfig, trace: HTTPTrace, *, transport: httpx.AsyncBaseTransport | None = None) -> tuple[OpenAIChatModel, AsyncOpenAI]:
    config.validate_ready()
    v = config.values
    http = httpx.AsyncClient(transport=transport, timeout=v["agent_timeout_seconds"], event_hooks={"request": [trace.request], "response": [trace.response]})
    client = AsyncOpenAI(base_url=v["agent_base_url"], api_key=config.credential.get_secret_value(), http_client=http, max_retries=0)
    provider = DeepSeekProvider(openai_client=client) if v["agent_provider"] == "deepseek" else OpenAIProvider(openai_client=client)
    return OpenAIChatModel(v["agent_model"], provider=provider, profile=config.profile()), client
