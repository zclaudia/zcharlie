import asyncio
import pytest

from investment_agent.sources.retry import retry_async, RateLimiter


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_failure():
    calls = {"n": 0}

    @retry_async(attempts=3, base_delay=0.01)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_gives_up_after_max_attempts():
    @retry_async(attempts=2, base_delay=0.01)
    async def always_fail():
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        await always_fail()


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_on_value_error():
    calls = {"n": 0}

    @retry_async(attempts=3, base_delay=0.01, retry_on=(ConnectionError,))
    async def bad_input():
        calls["n"] += 1
        raise ValueError("4xx")

    with pytest.raises(ValueError):
        await bad_input()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_rate_limiter_caps_concurrency():
    limiter = RateLimiter(max_concurrent=2)
    in_flight = {"current": 0, "max": 0}

    async def worker():
        async with limiter:
            in_flight["current"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["current"])
            await asyncio.sleep(0.05)
            in_flight["current"] -= 1

    await asyncio.gather(*[worker() for _ in range(5)])
    assert in_flight["max"] == 2
