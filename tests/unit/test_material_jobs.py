"""Material collection job: payload, idempotency key, enqueueing, fetcher (plan 06).

Everything here is offline. The payload / key / enqueue tests use an in-memory
session (no run is executed), and the fetcher tests drive
``httpx.MockTransport`` instead of a socket, so the module never dials out.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
    ControlledIdentity,
    load_pilot_identities,
    source_symbol,
)
from investment_agent.materials import handlers, jobs
from investment_agent.materials.contracts import UpstreamRequest
from investment_agent.materials.handlers import (
    HttpxMaterialFetcher,
    MaterialCollectionHandler,
    default_adapters,
    is_informational_problem,
)
from investment_agent.materials.jobs import (
    MATERIAL_COLLECTION,
    MATERIAL_SOURCES,
    MaterialCollectionPayload,
    enqueue_material_job,
    material_idempotency_key,
)
from investment_agent.providers.errors import ProviderDataError, ProviderTransientError
from investment_agent.research.worker import default_job_handlers
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
)

HK_SOURCE = SOURCE_EASTMONEY_HK
SEC_SOURCE = SOURCE_SEC_COMPANYFACTS
BATCH = "batch-1"


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
    )


def _payload(**overrides: Any) -> MaterialCollectionPayload:
    data: dict[str, Any] = {"listing_id": 5, "refresh_batch": BATCH}
    data.update(overrides)
    return MaterialCollectionPayload(**data)


def _identity(company_key: str = "hsbc-holdings") -> ControlledIdentity:
    return next(
        identity
        for identity in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if identity.company_key == company_key
    )


# --- the frozen contract ----------------------------------------------------


def test_kind_and_sources_are_the_frozen_contract() -> None:
    assert MATERIAL_COLLECTION == "material_collection"
    assert MATERIAL_SOURCES == (SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS)
    assert MaterialCollectionHandler.kind == MATERIAL_COLLECTION


def test_online_refresh_is_off_and_the_handler_is_registered(tmp_path: Path) -> None:
    assert handlers.ONLINE_REFRESH_ENABLED is False
    assert handlers.USER_AGENT == "zcharlie-research/0.1 (personal research)"

    registered = [
        handler
        for handler in default_job_handlers(_settings(tmp_path))
        if handler.kind == MATERIAL_COLLECTION
    ]
    assert len(registered) == 1
    assert isinstance(registered[0], MaterialCollectionHandler)


def test_default_adapters_cover_the_frozen_sources() -> None:
    adapters = default_adapters()
    assert set(adapters) == set(MATERIAL_SOURCES)
    for source in MATERIAL_SOURCES:
        assert adapters[source].source == source
        assert adapters[source].adapter_version


def test_the_handler_wires_a_default_fetcher_and_the_real_adapters(tmp_path: Path) -> None:
    handler = MaterialCollectionHandler(settings=_settings(tmp_path))
    assert isinstance(handler.fetcher, HttpxMaterialFetcher)
    assert set(handler._adapter_map()) == set(MATERIAL_SOURCES)


def test_informational_problem_classification() -> None:
    # Counters: reported, but they never mean the material is unusable.
    for problem in (
        "unmapped_items: income/124",
        "unmapped: 5",
        "unmapped_concepts: us-gaap/12",
        "unmapped_unit_skipped: TWD/3",
    ):
        assert is_informational_problem(problem) is True
    # Findings: the source is marked structural_failure and is not retried.
    for problem in (
        "pagination_truncated: income",
        "missing_raw: balance",
        "not_json: cashflow",
        "upstream_success_false: income",
        "",
    ):
        assert is_informational_problem(problem) is False


# --- payload validation -----------------------------------------------------


def test_payload_defaults_are_frozen_and_complete() -> None:
    payload = _payload()
    assert payload.listing_id == 5
    assert payload.sources == MATERIAL_SOURCES
    assert payload.periods == ()
    assert payload.adapter_versions == {}
    assert payload.reason == "user_refresh"

    with pytest.raises(ValidationError):
        payload.listing_id = 6  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _payload(unknown_field=1)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"listing_id": 0}, "listing_id"),
        ({"listing_id": -3}, "listing_id"),
        ({"sources": ()}, "at least one material source"),
        ({"sources": ("yahoo.chart.v1",)}, "unknown material sources"),
        ({"refresh_batch": ""}, "refresh_batch"),
        ({"periods": ("20241231",)}, "canonical"),
        ({"periods": ("2024-13-01",)}, "not an ISO date"),
        ({"adapter_versions": {"yahoo.chart.v1": "1"}}, "unknown sources"),
        ({"adapter_versions": {SOURCE_SEC_COMPANYFACTS: " "}}, "non-empty"),
        ({"reason": ""}, "reason"),
    ],
)
def test_payload_rejects_invalid_input(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _payload(**overrides)


def test_payload_canonicalises_sources_periods_and_adapter_versions() -> None:
    payload = _payload(
        sources=(SOURCE_SEC_COMPANYFACTS, SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS),
        periods=("2024-12-31", "2021-12-31", "2024-12-31"),
        adapter_versions={SOURCE_SEC_COMPANYFACTS: "1", SOURCE_EASTMONEY_HK: "1"},
    )
    assert payload.sources == MATERIAL_SOURCES
    assert payload.periods == ("2021-12-31", "2024-12-31")
    assert list(payload.adapter_versions) == [SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS]


# --- idempotency key --------------------------------------------------------


def test_idempotency_key_is_stable_and_field_sensitive() -> None:
    key = material_idempotency_key(_payload())
    assert key == f"material_collection:5:{HK_SOURCE}+{SEC_SOURCE}:-:{BATCH}"
    assert material_idempotency_key(_payload()) == key
    assert material_idempotency_key(_payload(listing_id=6)) != key
    assert material_idempotency_key(_payload(periods=("2024-12-31",))) != key
    assert material_idempotency_key(_payload(sources=(HK_SOURCE,))) != key
    assert material_idempotency_key(_payload(refresh_batch="batch-2")) != key
    # the licensed adapter versions are not part of the batch identity (plan 06)
    assert material_idempotency_key(_payload(adapter_versions={HK_SOURCE: "1"})) == key


def test_long_idempotency_keys_collapse_to_a_sha1() -> None:
    periods = tuple(f"{year:04d}-12-31" for year in range(1990, 2026))
    key = material_idempotency_key(_payload(periods=periods))
    assert key.startswith(f"{MATERIAL_COLLECTION}:")
    assert len(key) == len(f"{MATERIAL_COLLECTION}:") + 40
    assert len(key) <= jobs.MAX_IDEMPOTENCY_KEY_LENGTH
    assert material_idempotency_key(_payload(periods=periods)) == key
    assert material_idempotency_key(_payload(periods=periods[:-1])) != key
    assert material_idempotency_key(_payload(periods=periods[1:])) != key


# --- enqueueing -------------------------------------------------------------


async def test_enqueue_is_idempotent_and_creates_no_universe_row(db_session) -> None:
    payload = _payload()
    first, created = await enqueue_material_job(db_session, payload, triggered_by="test")

    assert created is True
    assert first.kind == MATERIAL_COLLECTION
    assert first.status == "queued"
    assert first.universe_code is None
    assert first.listing_id == payload.listing_id
    assert first.max_attempts == 3
    assert first.request_json == payload.model_dump(mode="json")
    assert MaterialCollectionPayload.model_validate(first.request_json) == payload

    again, created_again = await enqueue_material_job(db_session, payload, triggered_by="test")
    assert created_again is False
    assert again.id == first.id

    total = (
        await db_session.execute(select(func.count()).select_from(PipelineRun))
    ).scalar_one()
    assert total == 1


async def test_enqueue_accepts_an_attempt_budget(db_session) -> None:
    run, _ = await enqueue_material_job(
        db_session, _payload(), triggered_by="test", max_attempts=5
    )
    assert run.max_attempts == 5


async def test_one_key_never_stands_for_two_requests(db_session) -> None:
    await enqueue_material_job(db_session, _payload(), triggered_by="test")
    with pytest.raises(IdempotencyPayloadMismatchError):
        # ``reason`` is outside the key, so the key matches but the request does not.
        await enqueue_material_job(
            db_session, _payload(reason="scheduled"), triggered_by="test"
        )


# --- the fetcher ------------------------------------------------------------


def _request(
    name: str = "income",
    *,
    url: str = "https://datacenter.eastmoney.com/securities/api/data/v1/get",
) -> UpstreamRequest:
    return UpstreamRequest(name=name, url=url, params=(("pageNumber", "1"), ("pageSize", "500")))


def _sec_request() -> UpstreamRequest:
    return UpstreamRequest(
        name="companyfacts.json",
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
    )


def _fetcher(handler: Any, **kwargs: Any) -> HttpxMaterialFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxMaterialFetcher(client=client, **kwargs)


async def test_fetcher_returns_the_raw_response_with_its_provenance() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "result": {"data": []}})

    raw = await _fetcher(handler).fetch(_request(), identity=_identity())

    assert raw.name == "income"
    assert raw.media_type == "application/json"
    assert raw.http_status == 200
    assert json.loads(raw.content) == {"success": True, "result": {"data": []}}
    assert raw.source_url.startswith("https://datacenter.eastmoney.com/")
    assert raw.fetched_at.tzinfo is not None
    assert abs((datetime.now(UTC) - raw.fetched_at).total_seconds()) < 30

    (sent,) = seen
    assert sent.headers["User-Agent"] == handlers.USER_AGENT
    assert sent.headers["Accept"] == "application/json"
    assert sent.url.params["pageNumber"] == "1"
    assert sent.url.params["pageSize"] == "500"


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_fetcher_maps_retryable_answers_to_transient(status: int) -> None:
    fetcher = _fetcher(lambda request: httpx.Response(status, text="rate limited"))
    with pytest.raises(ProviderTransientError, match=f"HTTP {status}"):
        await fetcher.fetch(_request(), identity=_identity())


async def test_fetcher_maps_transport_failures_to_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderTransientError, match="ConnectError"):
        await _fetcher(handler).fetch(_request(), identity=_identity())


@pytest.mark.parametrize("status", [400, 403, 404, 422])
async def test_fetcher_maps_terminal_client_errors_to_data_error(status: int) -> None:
    fetcher = _fetcher(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(ProviderDataError, match=f"HTTP {status}"):
        await fetcher.fetch(_request(), identity=_identity())


async def test_fetcher_rejects_a_body_that_is_not_json() -> None:
    fetcher = _fetcher(lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ProviderDataError, match="not JSON"):
        await fetcher.fetch(_request(), identity=_identity())


def test_fetcher_keeps_one_limiter_per_host_with_the_declared_interval() -> None:
    fetcher = HttpxMaterialFetcher()
    eastmoney, sec = _request(), _sec_request()

    assert fetcher.limiter_for(eastmoney) is fetcher.limiter_for(eastmoney)
    assert fetcher.limiter_for(eastmoney) is not fetcher.limiter_for(sec)
    assert fetcher.limiter_for(eastmoney).rate_per_minute == (
        60.0 / handlers.MIN_INTERVAL[HK_SOURCE]
    )
    assert fetcher.limiter_for(sec).rate_per_minute == (
        60.0 / handlers.MIN_INTERVAL[SEC_SOURCE]
    )
    assert fetcher.min_interval_for(eastmoney) == handlers.MIN_INTERVAL[HK_SOURCE]
    assert fetcher.min_interval_for(sec) == handlers.MIN_INTERVAL[SEC_SOURCE]
    unknown = UpstreamRequest(name="x", url="https://example.invalid/x")
    assert fetcher.min_interval_for(unknown) == handlers.DEFAULT_MIN_INTERVAL
    assert fetcher.limiter_for(unknown).rate_per_minute == 60.0 / handlers.DEFAULT_MIN_INTERVAL


# --- adapter wiring ---------------------------------------------------------


def test_the_real_adapters_are_reachable_through_the_lazy_assembly() -> None:
    adapters = default_adapters()
    identity = _identity()
    # The HK adapter builds its three bounded requests from the checked binding.
    requests = adapters[HK_SOURCE].requests(identity, periods=())
    assert [request.name for request in requests] == ["income", "balance", "cashflow"]
    assert source_symbol(identity, HK_SOURCE) == "00005.HK"
    # The SEC adapter's first round is CompanyFacts + Submissions.
    sec_requests = adapters[SEC_SOURCE].requests(identity, periods=())
    assert [request.name for request in sec_requests] == ["companyfacts.json", "submissions.json"]
    assert source_symbol(identity, SEC_SOURCE) == "1089113"


def test_requested_periods_carry_only_the_end_date() -> None:
    payload = _payload(periods=("2024-12-31",))
    (period,) = handlers._requested_periods(payload)
    assert period.end == date(2024, 12, 31)
    assert period.start is None
    assert period.basis.value == "annual"
    assert handlers._requested_periods(_payload()) == ()
