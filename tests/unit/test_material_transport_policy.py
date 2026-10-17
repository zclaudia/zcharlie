from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    ControlledIdentity,
    load_pilot_identities,
)
from investment_agent.materials.contracts import UpstreamRequest
from investment_agent.materials.handlers import (
    MAX_RETRY_AFTER_SECONDS,
    HttpxMaterialFetcher,
    _retry_after_seconds,
)
from investment_agent.providers.errors import ProviderTransientError


def _identity() -> ControlledIdentity:
    return next(
        item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if item.company_key == "hsbc-holdings"
    )


def _request() -> UpstreamRequest:
    return UpstreamRequest(name="income", url="https://datacenter.eastmoney.com/api/data")


async def test_fetcher_sets_ua_timeout_and_checks_policy_after_limiter() -> None:
    seen: list[httpx.Request] = []
    guards: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    fetcher = HttpxMaterialFetcher(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)), timeout=7.5
    )
    limiter = fetcher.limiter_for(_request())
    async def acquire() -> None:
        guards.append("limited")
    limiter.acquire = acquire  # type: ignore[method-assign]
    async def guard(request: UpstreamRequest) -> None:
        assert request.name == "income"
        guards.append("policy")
    fetcher.before_send = guard

    await fetcher.fetch(_request(), identity=_identity())

    assert guards == ["limited", "policy"]
    (request,) = seen
    assert request.headers["User-Agent"]
    assert request.extensions["timeout"]["read"] == 7.5


@pytest.mark.parametrize("wire_format", ["seconds", "http-date"])
async def test_429_honors_retry_after_with_a_hard_wait_cap(wire_format: str) -> None:
    waits: list[float] = []
    retry_after = "30"
    if wire_format == "http-date":
        target = datetime.now(UTC) + timedelta(seconds=30)
        retry_after = target.strftime("%a, %d %b %Y %H:%M:%S GMT")

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": retry_after}, text="busy")
        )
    )
    fetcher = HttpxMaterialFetcher(client=client, sleep=sleep)
    with pytest.raises(ProviderTransientError):
        await fetcher.fetch(_request(), identity=_identity())
    assert waits == [MAX_RETRY_AFTER_SECONDS]


def test_retry_after_parses_both_wire_formats() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _retry_after_seconds("3", now=now) == 3
    future = (now + timedelta(seconds=12)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert _retry_after_seconds(future, now=now) == 12
    assert _retry_after_seconds("invalid", now=now) == 0
