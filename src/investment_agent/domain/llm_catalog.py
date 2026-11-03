"""Provider and model catalog. It does not record which agent or task uses a model.

A model is addressed by a stable ``model_ref`` of ``provider_id/model_id``.
Callers that need a model keep that reference themselves.
"""

from __future__ import annotations

from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Capability = Literal["thinking", "tools", "json_schema", "json_object", "tool_choice"]
CAPABILITIES: tuple[Capability, ...] = ("thinking", "tools", "json_schema", "json_object", "tool_choice")


class CatalogError(ValueError):
    """Safe configuration error containing no credentials or endpoint content."""


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LLMModel(CatalogModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    capabilities: list[Capability] = Field(default_factory=list)
    context_length: int | None = Field(default=None, gt=0, le=2_000_000)
    thinking: Literal["auto", "enabled", "disabled"] = "auto"
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    profile: Literal["openai", "deepseek"] = "openai"
    profile_model: str = Field(default="", max_length=200)
    output_mode: Literal["prompted", "tool", "native"] = "prompted"
    supports_json_schema: bool = False
    supports_json_object: bool = True
    tool_choice_required: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    retries: int = Field(default=1, ge=0, le=5)

    @model_validator(mode="after")
    def capabilities_match_invocation(self) -> Self:
        caps = set(self.capabilities)
        if self.supports_json_object:
            caps.add("json_object")
        else:
            caps.discard("json_object")
        if self.supports_json_schema:
            caps.add("json_schema")
        else:
            caps.discard("json_schema")
        if self.tool_choice_required:
            caps.add("tool_choice")
        else:
            caps.discard("tool_choice")
        self.capabilities = [name for name in CAPABILITIES if name in caps]
        if self.output_mode == "native" and not self.supports_json_schema:
            raise ValueError("Native output requires JSON Schema support")
        if self.output_mode == "prompted" and not self.supports_json_object:
            raise ValueError("Prompted output requires JSON Object support")
        return self


class LLMProvider(CatalogModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["openai", "deepseek"] = "openai"
    base_url: str = Field(min_length=1, max_length=500)
    # None on an update preserves the saved credential; "" explicitly clears it.
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    models: list[LLMModel] = Field(default_factory=list, max_length=100)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or url.query or url.fragment):
            raise ValueError("Use an HTTP(S) endpoint without credentials, query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("Model IDs must be unique within a provider")
        return self


class ModelRef(CatalogModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
    provider_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=100)

    @property
    def ref(self) -> str:
        return model_ref(self.provider_id, self.model_id)


class LLMCatalog(CatalogModel):
    providers: list[LLMProvider] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_providers(self) -> Self:
        if len({provider.id for provider in self.providers}) != len(self.providers):
            raise ValueError("Provider IDs must be unique")
        return self

    def resolve(self, ref: ModelRef) -> tuple[LLMProvider, LLMModel]:
        for provider in self.providers:
            if provider.id == ref.provider_id:
                for model in provider.models:
                    if model.id == ref.model_id:
                        return provider, model
        raise CatalogError("模型不可用")

    def resolve_ref(self, ref: str) -> tuple[LLMProvider, LLMModel]:
        provider_id, model_id = split_model_ref(ref)
        return self.resolve(ModelRef(provider_id=provider_id, model_id=model_id))

    def public(self) -> dict[str, Any]:
        result = self.model_dump(mode="json", exclude={"providers": {"__all__": {"api_key"}}})
        for item, provider in zip(result["providers"], self.providers, strict=True):
            item["api_key"] = None
            item["configured"] = bool(provider.api_key)
            key = provider.api_key or ""
            item["hint"] = key[-4:] if len(key) >= 8 else ""
        return result


def model_ref(provider_id: str, model_id: str) -> str:
    return f"{provider_id}/{model_id}"


def split_model_ref(ref: str) -> tuple[str, str]:
    provider_id, sep, model_id = ref.partition("/")
    if not sep or "/" in model_id or not provider_id or not model_id:
        raise CatalogError("模型不可用")
    return provider_id, model_id


def parse_catalog(value: str) -> LLMCatalog:
    try:
        return LLMCatalog.model_validate_json(value) if value else LLMCatalog()
    except ValidationError as exc:
        # Pydantic errors contain the input; never forward them or their repr.
        messages = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        raise CatalogError(f"Invalid LLM catalog: {messages}") from None


def merge_catalog(candidate: str, current: str) -> str:
    """Canonicalize an update while keeping credentials omitted by the editor."""
    if not candidate:
        return ""
    incoming = parse_catalog(candidate)
    saved = {provider.id: provider for provider in parse_catalog(current).providers}
    for provider in incoming.providers:
        if provider.api_key is None:
            previous = saved.get(provider.id)
            provider.api_key = previous.api_key if previous else ""
    return incoming.model_dump_json()


def validate_catalog(value: str) -> str:
    return parse_catalog(value).model_dump_json() if value else ""
