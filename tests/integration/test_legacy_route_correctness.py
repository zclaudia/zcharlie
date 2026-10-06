"""Legacy /pipelines, /attribute, /listings, /reports, /admin correctness:
pre-created run ids are the ones the pipelines finish, bounded query limits,
ticker validation."""

import asyncio
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from investment_agent.api.routes import attribution as attribution_routes
from investment_agent.api.routes import pipelines as pipeline_routes
from investment_agent.config import Settings, load_app_yaml
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Market
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

H = {"X-API-Key": "secret"}
ROOT = Path(__file__).resolve().parents[2]


def _runs() -> list[PipelineRun]:
    async def load() -> list[PipelineRun]:
        async with session_scope() as session:
            return list((await session.execute(select(PipelineRun))).scalars().all())

    return asyncio.run(load())


def _seed_aapl() -> None:
    async def seed() -> None:
        async with session_scope() as session:
            session.add(
                Market(code="NASDAQ", name="NASDAQ", timezone="America/New_York", currency="USD")
            )
            await ListingRepository(session).upsert_listing(
                market_code="NASDAQ", ticker="AAPL", company_name="Apple"
            )

    asyncio.run(seed())


class _FakePipeline:
    seen: list[dict[str, object]] = []

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def run(self, *args: object, run_id: int | None = None, **kwargs: object) -> int:
        assert run_id is not None
        _FakePipeline.seen.append({"args": args, "kwargs": kwargs, "run_id": run_id})
        async with session_scope() as session:
            await PipelineRunRepository(session).mark_success(run_id)
        return run_id


@pytest.fixture
def fake_pipelines(monkeypatch):
    _FakePipeline.seen = []
    monkeypatch.setattr(pipeline_routes, "DiscoveryPipeline", _FakePipeline)
    monkeypatch.setattr(pipeline_routes, "load_all_strategies", lambda **_k: {})
    monkeypatch.setattr(attribution_routes, "AttributionPipeline", _FakePipeline)
    monkeypatch.setattr(attribution_routes, "AttributionAgent", lambda **_k: object())
    return _FakePipeline


@pytest.mark.parametrize("is_async", ["true", "false"])
def test_discovery_route_returns_the_run_the_pipeline_finishes(
    api_client, fake_pipelines, is_async
):
    r = api_client.post(
        f"/pipelines/discovery/run?async={is_async}",
        json={"universe_code": "NASDAQ_100", "as_of": "2026-10-05"},
        headers=H,
    )
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    runs = _runs()
    assert [run.id for run in runs] == [run_id]
    assert runs[0].status == "success"
    assert fake_pipelines.seen[0]["run_id"] == run_id
    assert fake_pipelines.seen[0]["args"] == ("NASDAQ_100", date(2026, 10, 5))


@pytest.mark.parametrize("is_async", ["true", "false"])
def test_attribution_route_returns_the_run_the_pipeline_finishes(
    api_client, fake_pipelines, is_async
):
    _seed_aapl()
    r = api_client.post(f"/attribute/AAPL?async={is_async}", json={}, headers=H)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    runs = _runs()
    assert [run.id for run in runs] == [run_id]
    assert runs[0].status == "success"
    assert fake_pipelines.seen[0]["run_id"] == run_id


def test_discovery_setup_failure_marks_precreated_run_failed(api_client, monkeypatch):
    def boom(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("strategy load failed")

    monkeypatch.setattr(pipeline_routes, "load_all_strategies", boom)

    async def scenario() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(kind="discovery")
            run_id = run.id
        with pytest.raises(RuntimeError):
            await pipeline_routes._run_discovery("NASDAQ_100", date(2026, 10, 5), run_id)
        return run_id

    run_id = asyncio.run(scenario())
    (run,) = _runs()
    assert run.id == run_id
    assert run.status == "failed"
    assert "strategy load failed" in (run.error_summary or "")


def test_real_discovery_pipeline_adopts_given_run_id(api_client, monkeypatch):
    from investment_agent.pipelines import discovery as discovery_module

    async def fail_ingestion(*_a: object, **_k: object) -> None:
        raise RuntimeError("no data")

    monkeypatch.setattr(discovery_module.steps, "step1_data_ingestion", fail_ingestion)
    pipeline = discovery_module.DiscoveryPipeline(
        data_source=object(),  # type: ignore[arg-type]
        strategies={},
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        app_config=load_app_yaml(ROOT / "config" / "app.yaml"),
    )

    async def scenario() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(kind="discovery")
            run_id = run.id
        with pytest.raises(RuntimeError):
            await pipeline.run("NASDAQ_100", date(2026, 10, 5), run_id=run_id)
        return run_id

    run_id = asyncio.run(scenario())
    (run,) = _runs()
    assert run.id == run_id
    assert run.status == "failed"


def test_real_attribution_pipeline_adopts_given_run_id(api_client, monkeypatch):
    from investment_agent.attribution import pipeline as attribution_pipeline_module

    _seed_aapl()

    async def fail_ingestion(*_a: object, **_k: object) -> None:
        raise RuntimeError("no data")

    monkeypatch.setattr(
        attribution_pipeline_module.steps, "attr_step1_data_ingestion", fail_ingestion
    )
    pipeline = attribution_pipeline_module.AttributionPipeline(
        data_source=object(),  # type: ignore[arg-type]
        agent=object(),  # type: ignore[arg-type]
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )

    async def scenario() -> int:
        async with session_scope() as session:
            run = await PipelineRunRepository(session).create(kind="attribution")
            run_id = run.id
        with pytest.raises(RuntimeError):
            await pipeline.run("AAPL", date(2026, 10, 5), run_id=run_id)
        async with session_scope() as session:
            count = (await session.execute(select(func.count(PipelineRun.id)))).scalar_one()
        assert count == 1
        return run_id

    run_id = asyncio.run(scenario())
    (run,) = _runs()
    assert run.id == run_id
    assert run.status == "failed"


# ---- bounded limits --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/pipelines?limit={}",
        "/admin/llm-calls?limit={}",
        "/reports/discovery?limit={}",
        "/reports/attribution?limit={}",
        "/listings/AAPL/scores?limit={}",
    ],
)
def test_query_limits_are_bounded(api_client, path):
    for bad in (0, -1, 100_000):
        assert api_client.get(path.format(bad), headers=H).status_code == 422, (path, bad)


def test_reports_listing_respects_limit_across_days(api_client, tmp_path):
    for day, run_ids in (("2026-10-01", (1, 2)), ("2026-10-02", (3,))):
        folder = tmp_path / "reports" / day
        folder.mkdir(parents=True)
        for run_id in run_ids:
            (folder / f"discovery_{run_id}.md").write_text("x")
    (tmp_path / "reports" / "not-a-date").mkdir()
    r = api_client.get("/reports/discovery?limit=2", headers=H)
    assert r.status_code == 200
    assert [row["run_id"] for row in r.json()] == [3, 2]


# ---- ticker validation -----------------------------------------------------


@pytest.mark.parametrize("ticker", ["AAPL", "BRK.B", "BRK-B", "600519.SH"])
def test_listing_path_accepts_valid_tickers(api_client, ticker):
    assert api_client.get(f"/listings/{ticker}", headers=H).status_code == 404


@pytest.mark.parametrize("ticker", ["A$PL", "TOO-LONG-TICKER-XYZ", "A..B", "%20AAPL"])
def test_listing_and_attribution_paths_reject_bad_tickers(api_client, ticker):
    assert api_client.get(f"/listings/{ticker}", headers=H).status_code == 422
    assert api_client.get(f"/attribute/{ticker}/latest", headers=H).status_code == 422
    assert api_client.post(f"/attribute/{ticker}", json={}, headers=H).status_code == 422


def test_watchlist_add_validates_ticker_and_note(api_client):
    for bad in ("A$PL", "<script>", "X" * 20):
        r = api_client.post("/watchlist", json={"ticker": bad}, headers=H)
        assert r.status_code == 422, bad
    r = api_client.post("/watchlist", json={"ticker": "AAPL", "note": "n" * 501}, headers=H)
    assert r.status_code == 422
    # Valid shape passes validation (unknown listing -> 404).
    r = api_client.post("/watchlist", json={"ticker": "brk.b", "note": "ok"}, headers=H)
    assert r.status_code == 404
