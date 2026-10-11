"""Offline AG02 probes: no production adapter or network credentials."""

import httpx
import pytest
from tools.spike_pydantic_ai import CapturedRequest, Spike, run_error_probes
from tools.spike_tool_calling import Spike as SDKSpike

from investment_agent.config import LLMTierConfig


@pytest.mark.asyncio
async def test_native_v2_validation_and_failures_are_bounded() -> None:
    reports = {probe.name: probe for probe in await run_error_probes()}
    assert len(reports) == 9
    assert all(probe.status == "pass" for probe in reports.values())
    for name in ("wrong_arguments_exhausted", "missing_argument_exhausted", "output_exhausted"):
        extra = reports[name].extra
        assert extra["model_attempts"] == 2
        assert extra["final_output"] is None
        assert extra["executed_tools"] == []
        assert extra["exception"] == "UnexpectedModelBehavior"
    for name in ("wrong_arguments_repaired", "missing_argument_repaired"):
        assert reports[name].extra["executed_tools"] == ["600519"]
        assert reports[name].extra["retry_prompts"] == 1
    missing = reports["missing_data"]
    assert missing.extra["final_output"] == "missing data; no value published"
    assert missing.input_tokens == 20  # explicitly synthetic fixture usage
    assert reports["http_failure"].extra["model_attempts"] == 1
    assert reports["http_failure"].requests == 0  # framework usage excludes failed HTTP
    assert reports["tool_request_failure"].extra["model_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("framework", [True, False])
async def test_spike_request_cap_blocks_before_network(framework: bool) -> None:
    config = LLMTierConfig(base_url="https://fixture.invalid/v1", api_key="fixture", model="fixture")
    spike = Spike(config, max_requests=1) if framework else SDKSpike(config, max_requests=1)
    request = httpx.Request("POST", "https://fixture.invalid/v1", json={"messages": []})
    hook = spike._capture if framework else spike._count_request
    try:
        await hook(request)
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await hook(request)
        assert spike.total_requests == 1
    finally:
        if isinstance(spike, SDKSpike):
            await spike.client.close()


def test_request_capture_excludes_contents_and_credentials() -> None:
    captured = CapturedRequest.from_body({
        "model": "fixture", "secret": "must disappear",
        "messages": [
            {"role": "assistant", "reasoning_content": "private reasoning", "content": "private text"},
            {"role": "tool", "content": "private evidence"},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"sensitive": "value"}},
    })
    assert captured.assistant_reasoning_fields == ["reasoning_content"]
    assert captured.tool_messages == 1
    assert captured.response_format == "json_schema"
    assert "private" not in repr(captured)
    assert "must disappear" not in repr(captured)


@pytest.mark.asyncio
async def test_unknown_selection_and_cap_are_explicit() -> None:
    config = LLMTierConfig(base_url="https://fixture.invalid/v1", api_key="fixture", model="fixture")
    spike = Spike(config, max_requests=1)
    with pytest.raises(ValueError, match="unknown probe"):
        await spike.run({"typo"})
    spike.total_requests = 1
    reports = await spike.run({"tool_call_basic", "output_prompted"})
    assert len(reports) == 2
    assert all(p.status == "skipped" and "cap exhausted" in p.detail for p in reports)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,probe_name,expected", [
    (400, "output_native", "unsupported"),
    (400, "tool_call_basic", "error"),
    (401, "output_native", "error"),
    (429, "output_native", "error"),
    (503, "output_native", "error"),
])
async def test_http_failure_classification_is_sanitized(monkeypatch: pytest.MonkeyPatch, status: int, probe_name: str, expected: str) -> None:
    from pydantic_ai import ModelHTTPError

    config = LLMTierConfig(base_url="https://fixture.invalid/v1", api_key="fixture", model="fixture")
    spike = Spike(config)

    async def fail(probe: object) -> None:
        raise ModelHTTPError(status, "fixture", "private endpoint and secret")

    monkeypatch.setattr(spike, "p_" + probe_name, fail)
    # Probe names come from callable __name__ in the original harness.
    fail.__name__ = "p_" + probe_name
    report = (await spike.run({probe_name}))[0]
    assert report.status == expected
    assert f"HTTP {status}" in report.detail
    assert "private" not in report.detail
