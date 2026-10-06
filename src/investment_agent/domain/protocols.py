"""Core protocols for pluggable extension points."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CompletionResult(BaseModel):
    text: str
    output: Any | None = None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str
    provider: str


@runtime_checkable
class LLMClient(Protocol):
    tier_name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: type[BaseModel] | None = None,
        tools: list[Any] | None = None,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> CompletionResult: ...


# === Rendering / Notification ===


class RenderedContent(BaseModel):
    format: str  # 'markdown' | 'html' | 'json'
    body: str
    metadata: dict[str, object] = Field(default_factory=dict)


class NotificationResult(BaseModel):
    notifier_name: str
    success: bool
    target: str = ""
    error: str | None = None


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(
        self, content: RenderedContent, channel_metadata: dict[str, object]
    ) -> NotificationResult: ...
