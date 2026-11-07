import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from investment_agent.api.auth import get_settings
from investment_agent.api.routes import simulation as api
from investment_agent.config import Settings
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.simulation import SimulationExecutionEvidenceRecord
from investment_agent.storage.repositories.listing import ListingRepository

H = {"X-API-Key": "secret"}
NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)
CATALOG = json.dumps(
    {
        "providers": [
            {
                "id": "fixture",
                "name": "Offline fixture",
                "kind": "openai",
                "base_url": "https://private-provider.invalid/v1",
                "api_key": "sk-private-never-returned",
                "models": [{"id": "model", "name": "Fake model", "model": "fake-upstream"}],
            }
        ]
    }
)


@pytest.fixture
async def simulation_client(api_client, monkeypatch):
    monkeypatch.setattr(api, "_now", lambda: NOW)
    settings = Settings(llm_catalog=CATALOG)
    api_client.app.dependency_overrides[get_settings] = lambda: settings
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("600519.SH")
        listing_id = listing.id
    yield api_client, listing_id
    api_client.app.dependency_overrides.clear()


def body(listing_id, **changes):
    return {
        "name": "前瞻实验",
        "universe": [listing_id],
        "start_date": "2026-11-09",
        "end_date": "2026-12-09",
        "agents": [{"name": "模拟模型", "provider_ref": "fixture", "model_ref": "model"}],
        "idempotency_key": "create-one",
    } | changes


def create(client, listing_id, **changes):
    response = client.post("/api/simulations", headers=H, json=body(listing_id, **changes))
    assert response.status_code == 201, response.text
    return response.json()


async def test_authenticated_friendly_create_options_and_safe_export(simulation_client):
    client, listing_id = simulation_client
    assert client.get("/api/simulations").status_code == 401
    options = client.get("/api/simulations/options", headers=H)
    assert options.status_code == 200
    assert options.json()["models"][0]["available"]
    assert "private-provider" not in options.text and "sk-private" not in options.text
    stocks = client.get("/api/simulations/options/stocks?q=600519", headers=H).json()
    assert stocks[0]["listing_id"] == listing_id
    experiment = create(client, listing_id)
    assert len(experiment["config"]["participants"]) == 3
    assert "model_snapshot" not in json.dumps(experiment)
    assert "content_hash" in experiment["config"]["rules"]
    assert client.get("/api/simulations", headers=H).json()[0]["id"] == experiment["id"]
    detail = client.get(f"/api/simulations/{experiment['id']}", headers=H)
    assert detail.status_code == 200
    participant = detail.json()["participants"][0]
    assert (
        client.get(
            f"/api/simulations/{experiment['id']}/participants/{participant['id']}", headers=H
        ).status_code
        == 200
    )
    exported = client.get(f"/api/simulations/{experiment['id']}/export", headers=H)
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith("attachment;")
    assert "sk-private" not in exported.text and "model_snapshot" not in exported.text
    assert "artifact_path" not in exported.text


async def test_replay_body_conflict_and_revision_lifecycle_enqueue_no_calls(
    simulation_client, monkeypatch
):
    client, listing_id = simulation_client

    async def refuse(*args, **kwargs):
        raise AssertionError("HTTP called an external provider")

    from investment_agent.providers.baostock_market import BaostockMarketClient
    from investment_agent.simulation import runtime

    monkeypatch.setattr(BaostockMarketClient, "fetch_daily_bars", refuse)
    monkeypatch.setattr(runtime, "decide_participant", refuse)
    experiment = create(client, listing_id)
    assert create(client, listing_id)["id"] == experiment["id"]
    conflict = client.post("/api/simulations", headers=H, json=body(listing_id, name="changed"))
    assert conflict.status_code == 409
    path = f"/api/simulations/{experiment['id']}"
    start_body = {"expected_revision": 0, "idempotency_key": "start"}
    started = client.post(f"{path}/start", headers=H, json=start_body)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    assert client.post(f"{path}/start", headers=H, json=start_body).json() == started.json()
    assert (
        client.post(
            f"{path}/pause",
            headers=H,
            json={"expected_revision": 0, "idempotency_key": "bad-revision"},
        ).status_code
        == 409
    )
    run = client.post(
        f"{path}/run-now", headers=H, json={"expected_revision": 1, "idempotency_key": "run"}
    )
    assert run.status_code == 200 and run.json()["status"] == "queued"
    async with session_scope() as session:
        jobs = (
            await session.scalars(select(PipelineRun).where(PipelineRun.kind == "simulation_cycle"))
        ).all()
        assert len(jobs) == 1
        assert jobs[0].request_json["experiment_id"] == experiment["id"]
    paused = client.post(
        f"{path}/pause", headers=H, json={"expected_revision": 1, "idempotency_key": "pause"}
    )
    assert paused.json()["status"] == "paused"
    ended = client.post(
        f"{path}/end", headers=H, json={"expected_revision": 2, "idempotency_key": "end"}
    )
    assert ended.json()["status"] == "completed"
    # An old successful mutation replays its original receipt without reopening.
    assert client.post(f"{path}/start", headers=H, json=start_body).json() == started.json()
    assert client.get(path, headers=H).json()["experiment"]["status"] == "completed"
    for action in ("start", "run-now", "data-refresh"):
        response = client.post(
            f"{path}/{action}",
            headers=H,
            json={"expected_revision": 3, "idempotency_key": f"after-end-{action}"},
        )
        assert response.status_code == 409, response.text


async def test_unknown_model_credentials_and_clock_inputs_rejected(simulation_client):
    client, listing_id = simulation_client
    invalid = body(
        listing_id, agents=[{"name": "missing", "provider_ref": "fixture", "model_ref": "missing"}]
    )
    response = client.post("/api/simulations", headers=H, json=invalid)
    assert response.status_code == 422 and "private" not in response.text
    for patch in ({"now": NOW.isoformat()}, {"model_snapshot": {}}, {"api_key": "forbidden"}):
        assert (
            client.post("/api/simulations", headers=H, json=body(listing_id) | patch).status_code
            == 422
        )
    assert (
        client.post(
            "/api/simulations", headers=H, json=body(listing_id, universe=[listing_id] * 101)
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/simulations", headers=H | {"Idempotency-Key": "different"}, json=body(listing_id)
        ).status_code
        == 422
    )


async def test_cookie_csrf_and_scope_boundaries(simulation_client):
    client, listing_id = simulation_client
    client.post("/app/login", data={"api_key": "secret", "next": "/app"}, follow_redirects=False)
    assert client.post("/api/simulations", json=body(listing_id)).status_code == 403
    assert (
        client.post(
            "/api/simulations", headers={"Origin": "https://evil.test"}, json=body(listing_id)
        ).status_code
        == 403
    )
    result = client.post(
        "/api/simulations", headers={"X-Requested-With": "fetch"}, json=body(listing_id)
    )
    assert result.status_code == 201
    identity = result.json()["id"]
    assert client.get(f"/api/simulations/{identity}/participants/other").status_code == 404
    assert client.get(f"/api/simulations/{identity}/orders?participant_id=other").status_code == 404
    assert (
        client.post(
            f"/api/simulations/{identity}/decisions",
            headers={"X-Requested-With": "fetch"},
            json={"expected_revision": 0, "idempotency_key": "manual"},
        ).status_code
        == 422
    )


async def test_data_gaps_and_trusted_user_evidence_import(simulation_client):
    client, listing_id = simulation_client
    identity = create(client, listing_id)["id"]
    path = f"/api/simulations/{identity}"
    gaps = client.get(f"{path}/availability?trade_date=2026-11-06", headers=H)
    assert gaps.status_code == 200, gaps.text
    assert not gaps.json()["ready"]
    assert "execution_evidence_missing" in gaps.json()["securities"][0]["missing"]
    evidence = {
        "listing_id": listing_id,
        "trade_date": "2026-11-06",
        "exchange": "SSE",
        "board": "mainboard",
        "security_type": "ordinary_a",
        "ipo_date": "2000-01-01",
        "ordinary_limit_session": True,
        "reference_price": "10",
        "upper_limit": "11",
        "lower_limit": "9",
        "tick_size": "0.01",
        "lot_size": 100,
        "is_st": False,
        "rule_version": "experimental-mainboard-v1",
        "rule_source": "official:fixture",
        "rule_verified": True,
        "corporate_actions_complete": True,
        "published_at": "2026-11-05T10:00:00Z",
        "captured_at": "2026-11-06T09:00:00Z",
    }
    request = {
        "evidence": evidence,
        "source_ref": "official:fixture",
        "idempotency_key": "evidence",
    }
    assert (
        client.post(
            f"{path}/execution-evidence", headers=H, json=request | {"source_kind": "provider"}
        ).status_code
        == 422
    )
    imported = client.post(f"{path}/execution-evidence", headers=H, json=request)
    assert imported.status_code == 201, imported.text
    assert (
        client.post(f"{path}/execution-evidence", headers=H, json=request).json() == imported.json()
    )
    async with session_scope() as session:
        stored = await session.get(SimulationExecutionEvidenceRecord, imported.json()["id"])
        assert stored.source_kind == "user"
    assert (
        client.post(
            f"{path}/execution-evidence",
            headers=H,
            json=request | {"evidence": evidence | {"lower_limit": "8"}},
        ).status_code
        == 409
    )
    future = client.get(f"{path}/availability?trade_date=2027-01-04", headers=H)
    assert future.status_code == 200


async def test_replay_survives_catalog_change_and_refresh_is_only_queued(simulation_client):
    client, listing_id = simulation_client
    initial = create(client, listing_id)
    client.app.dependency_overrides[get_settings] = lambda: Settings(llm_catalog="")
    assert create(client, listing_id) == initial
    start = client.post(
        f"/api/simulations/{initial['id']}/start",
        headers=H,
        json={"expected_revision": 0, "idempotency_key": "start-unavailable"},
    )
    assert start.status_code == 422
    refreshed = client.post(
        f"/api/simulations/{initial['id']}/data-refresh",
        headers=H,
        json={"expected_revision": 0, "idempotency_key": "refresh"},
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["status"] == "queued"
    async with session_scope() as session:
        job = await session.get(PipelineRun, refreshed.json()["run_id"])
        assert job.request_json["force_collection"] is True


async def test_unknown_calendar_year_and_missing_idempotency_fail_safely(simulation_client):
    client, listing_id = simulation_client
    missing_key = body(listing_id)
    missing_key.pop("idempotency_key")
    assert client.post("/api/simulations", headers=H, json=missing_key).status_code == 422
    invalid = client.post(
        "/api/simulations",
        headers=H,
        json=body(listing_id, start_date="2039-01-03", end_date="2039-02-03"),
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "calendar_unreviewed"


async def test_simulation_validation_never_echoes_secret_and_other_api_keeps_contract(
    simulation_client,
):
    client, listing_id = simulation_client
    secret = "sk-accidentally-submitted-credential"
    response = client.post(
        "/api/simulations", headers=H, json=body(listing_id) | {"api_key": secret}
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"]["code"] == "invalid_input"
    ordinary = client.get("/stocks/search?limit=not-an-integer", headers=H)
    assert ordinary.status_code == 422
    assert isinstance(ordinary.json()["detail"], list)
    assert ordinary.json()["detail"][0]["loc"] == ["query", "limit"]


async def test_http_replay_cannot_return_another_experiment(simulation_client):
    from investment_agent.storage.models.simulation import SimulationRequestRecord

    client, listing_id = simulation_client
    original = create(client, listing_id)
    another = create(client, listing_id, idempotency_key="another-experiment")
    async with session_scope() as session:
        request = await session.get(
            SimulationRequestRecord, ("http:simulation:create", "create-one")
        )
        request.result_id = another["id"]
        request.payload = another
    response = client.post("/api/simulations", headers=H, json=body(listing_id))
    assert response.status_code == 404
    assert another["id"] not in response.text
    assert client.get(f"/api/simulations/{original['id']}", headers=H).status_code == 200


async def test_creation_requires_reviewed_execution_and_t_plus_one_sessions(simulation_client):
    from datetime import date, timedelta

    client, listing_id = simulation_client
    options = client.get("/api/simulations/options", headers=H).json()
    maximum = date.fromisoformat(options["calendar"]["max_observation_date"])
    calendar = api._calendar(Settings())
    execution = api.next_trading_day(calendar, maximum)
    sellable = api.next_trading_day(calendar, execution)
    assert maximum == date(2026, 12, 29)
    assert execution == date(2026, 12, 30)
    assert sellable == date(2026, 12, 31)
    valid = create(
        client, listing_id, end_date=str(maximum), idempotency_key="last-valid-observation"
    )
    assert valid["config"]["end_date"] == str(maximum)
    invalid = client.post(
        "/api/simulations",
        headers=H,
        json=body(
            listing_id,
            start_date=str(maximum - timedelta(days=30)),
            end_date=str(execution),
            idempotency_key="no-execution-coverage",
        ),
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "calendar_unreviewed"
    # The old Dec30 bound had an execution date but no verified T+1 date.
    assert execution > maximum and sellable > execution
    weekend = create(client, listing_id, end_date="2026-12-26", idempotency_key="weekend-end")
    assert weekend["config"]["end_date"] == "2026-12-26"
    assert api.next_trading_day(calendar, date(2026, 12, 26)) == date(2026, 12, 28)


def evidence_body(listing_id, *, day="2026-11-06", rule_version="experimental-mainboard-v1"):
    return {
        "evidence": {
            "listing_id": listing_id,
            "trade_date": day,
            "exchange": "SSE",
            "board": "mainboard",
            "security_type": "ordinary_a",
            "ipo_date": "2000-01-01",
            "ordinary_limit_session": True,
            "reference_price": "10",
            "upper_limit": "11",
            "lower_limit": "9",
            "tick_size": "0.01",
            "lot_size": 100,
            "is_st": False,
            "rule_version": rule_version,
            "rule_source": "official:fixture",
            "rule_verified": True,
            "corporate_actions_complete": True,
            "published_at": f"{day}T08:00:00Z",
            "captured_at": f"{day}T09:00:00Z",
        },
        "source_ref": "official:fixture",
        "idempotency_key": f"proof-{day}-{rule_version}",
    }


async def add_ready_inputs(
    listing_id,
    *,
    backfill=False,
    amount="10000000",
    report_period="2026-06-30",
    available_from="2026-11-05",
    captured_at=NOW,
    trade_date="2026-11-06",
):
    from datetime import date
    from decimal import Decimal

    from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
    from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

    async with session_scope() as session:
        await MarketSnapshotRepository(session).insert(
            listing_id=listing_id,
            trade_date=date.fromisoformat(trade_date),
            open=Decimal(10),
            high=Decimal(11),
            low=Decimal(9),
            close=Decimal(10),
            volume=Decimal(1000),
            amount=Decimal(amount) if amount is not None else None,
            trade_status=1,
            is_st=False,
            provider="fixture",
            captured_at=captured_at,
            is_backfill=backfill,
        )
        await FinancialSnapshotRepository(session).insert(
            listing_id=listing_id,
            report_period=date.fromisoformat(report_period),
            period_kind="semi",
            fields={"revenue": "1"},
            provider="fixture",
            captured_at=captured_at,
            availability="actual",
            available_from=date.fromisoformat(available_from),
        )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("backfill", "raw_market_missing"),
        ("capacity", "historical_turnover_missing"),
        ("rule", "reservation_rule_mismatch"),
        ("future-report", "financial_missing"),
        ("same-day-publish", "financial_missing"),
    ],
)
async def test_availability_uses_same_raw_capacity_rule_and_pit_gates(
    simulation_client, case, expected
):
    client, listing_id = simulation_client
    identity = create(client, listing_id)["id"]
    proof = evidence_body(
        listing_id, rule_version="wrong-rules" if case == "rule" else "experimental-mainboard-v1"
    )
    imported = client.post(f"/api/simulations/{identity}/execution-evidence", headers=H, json=proof)
    assert imported.status_code == 201, imported.text
    await add_ready_inputs(
        listing_id,
        backfill=case == "backfill",
        amount=None if case == "capacity" else "10000000",
        report_period="2026-11-07" if case == "future-report" else "2026-06-30",
        available_from="2026-11-06" if case == "same-day-publish" else "2026-11-05",
    )
    response = client.get(
        f"/api/simulations/{identity}/availability?trade_date=2026-11-06", headers=H
    )
    assert response.status_code == 200, response.text
    assert not response.json()["ready"]
    assert expected in response.json()["securities"][0]["missing"]


async def test_availability_all_required_proofs_ready(simulation_client):
    client, listing_id = simulation_client
    identity = create(client, listing_id, start_date="2026-11-06")["id"]
    imported = client.post(
        f"/api/simulations/{identity}/execution-evidence", headers=H, json=evidence_body(listing_id)
    )
    assert imported.status_code == 201
    await add_ready_inputs(listing_id)
    response = client.get(
        f"/api/simulations/{identity}/availability?trade_date=2026-11-06", headers=H
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready"]
    assert response.json()["securities"][0]["missing"] == []


async def test_cost_payload_index_scope_mismatch_is_not_exported(simulation_client):
    from investment_agent.storage.models.simulation import SimulationCostRecord

    client, listing_id = simulation_client
    identity = create(client, listing_id)["id"]
    participant = client.get(f"/api/simulations/{identity}/participants", headers=H).json()[0]
    payload = {
        "id": "cost-one",
        "experiment_id": identity,
        "participant_id": participant["id"],
        "account_id": participant["account_id"],
        "round_id": "round",
        "call_key": "call",
        "model_ref": "fixture/model",
        "status": "settled",
        "amount": "0.5",
        "estimated": True,
        "currency": "USD",
        "occurred_at": NOW.isoformat(),
    }
    async with session_scope() as session:
        session.add(
            SimulationCostRecord(
                id="cost-one",
                experiment_id=identity,
                participant_id=participant["id"],
                account_id=participant["account_id"],
                round_id="round",
                call_key="call",
                status="unknown",
                payload=payload,
            )
        )
    response = client.get(f"/api/simulations/{identity}/costs", headers=H)
    assert response.status_code == 409, response.text
    assert "fixture/model" not in response.text and '"amount"' not in response.text


async def test_friendly_api_does_not_offer_unconfigurable_method_comparison(simulation_client):
    client, listing_id = simulation_client
    response = client.post(
        "/api/simulations", headers=H, json=body(listing_id, mode="method_comparison")
    )
    assert response.status_code == 422


async def test_estimated_cost_flag_and_known_round_scope_are_preserved(simulation_client):
    from datetime import date

    from investment_agent.simulation.service import SimulationService
    from investment_agent.storage.models.simulation import SimulationCostRecord

    client, listing_id = simulation_client
    experiment = create(client, listing_id, start_date="2026-11-06")
    identity = experiment["id"]
    response = client.post(
        f"/api/simulations/{identity}/start",
        headers=H,
        json={"expected_revision": 0, "idempotency_key": "cost-start"},
    )
    assert response.status_code == 200, response.text
    await add_ready_inputs(listing_id)
    assert (
        client.post(
            f"/api/simulations/{identity}/execution-evidence",
            headers=H,
            json=evidence_body(listing_id),
        ).status_code
        == 201
    )
    async with session_scope() as session:
        svc = SimulationService(
            session,
            data_dir=Settings().data_dir,
            calendar=api._calendar(Settings()),
            now=lambda: NOW,
        )
        round_ = await svc.prepare_round(
            identity,
            observation_date=date(2026, 11, 6),
            cutoff=NOW,
            decision_deadline=datetime(2026, 11, 9, 0, tzinfo=UTC),
            idempotency_key="cost-round",
        )
        participant = next(p for p in await svc.participants(identity) if p.config.kind == "agent")
        payload = {
            "id": "estimated-cost",
            "experiment_id": identity,
            "participant_id": participant.id,
            "account_id": participant.account_id,
            "round_id": round_.id,
            "call_key": "priced-call",
            "model_ref": "fixture/model",
            "status": "settled",
            "amount": "0.5",
            "estimated": True,
            "currency": "USD",
            "occurred_at": NOW.isoformat(),
        }
        session.add(
            SimulationCostRecord(
                id="estimated-cost",
                experiment_id=identity,
                participant_id=participant.id,
                account_id=participant.account_id,
                round_id=round_.id,
                call_key="priced-call",
                status="settled",
                payload=payload,
            )
        )
    response = client.get(
        f"/api/simulations/{identity}/costs?participant_id={participant.id}", headers=H
    )
    assert response.status_code == 200, response.text
    cost = response.json()["records"][0]
    assert cost["estimated"] is True and cost["amount"] == "0.5" and cost["currency"] == "USD"
    exported = client.get(f"/api/simulations/{identity}/export", headers=H)
    assert exported.status_code == 200, exported.text
    assert exported.json()["costs"] == [cost]
    assert (
        exported.json()["resource_policy"]["returned_model"]
        == "provider_reported_identifier_when_available"
    )
    detail = client.get(f"/api/simulations/{identity}", headers=H)
    assert detail.status_code == 200
    assert detail.json()["data_gaps"][round_.id] == []
    assert "_service" not in detail.text and "artifact_path" not in detail.text


async def test_availability_does_not_call_out_of_season_data_ready(simulation_client):
    client, listing_id = simulation_client
    identity = create(client, listing_id)["id"]
    client.post(
        f"/api/simulations/{identity}/execution-evidence", headers=H, json=evidence_body(listing_id)
    )
    await add_ready_inputs(listing_id)
    result = client.get(
        f"/api/simulations/{identity}/availability?trade_date=2026-11-06", headers=H
    )
    assert result.status_code == 200 and not result.json()["ready"]
    assert "outside_experiment_season" in result.json()["missing"]


async def test_corporate_action_proof_fields_reach_scope_checked_service(
    simulation_client, monkeypatch
):
    client, listing_id = simulation_client
    identity = create(client, listing_id)["id"]
    participant = client.get(f"/api/simulations/{identity}/participants", headers=H).json()[0]
    before = client.get(
        f"/api/simulations/{identity}/participants/{participant['id']}/account", headers=H
    ).json()
    current = datetime(2026, 11, 9, 10, tzinfo=UTC)
    monkeypatch.setattr(api, "_now", lambda: current)
    request = {
        "expected_revision": before["revision"],
        "idempotency_key": "unearned-income",
        "action": {
            "id": "dividend-one",
            "listing_id": listing_id,
            "kind": "cash_entitlement",
            "economic_at": current.isoformat(),
            "source_ref": "official:dividend",
            "verified": True,
            "amount": "10",
            "entitlement_ref": "record-date",
        },
        "entitlement_date": "2026-11-09",
        "cash_per_share": "0.1",
    }
    response = client.post(
        f"/api/simulations/{identity}/participants/{participant['id']}/corporate-actions",
        headers=H,
        json=request,
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "reconciliation_failed"
    assert (
        client.get(
            f"/api/simulations/{identity}/participants/{participant['id']}/account", headers=H
        ).json()
        == before
    )


async def test_suspended_zero_volume_missing_amount_is_known_zero_capacity(simulation_client):
    from decimal import Decimal

    from investment_agent.storage.models.pit_snapshot import MarketSnapshot

    client, listing_id = simulation_client
    identity = create(client, listing_id, start_date="2026-11-06")["id"]
    client.post(
        f"/api/simulations/{identity}/execution-evidence", headers=H, json=evidence_body(listing_id)
    )
    await add_ready_inputs(listing_id, amount=None)
    async with session_scope() as session:
        row = await session.scalar(
            select(MarketSnapshot).where(MarketSnapshot.listing_id == listing_id)
        )
        row.trade_status = 0
        row.volume = Decimal(0)
    response = client.get(
        f"/api/simulations/{identity}/availability?trade_date=2026-11-06", headers=H
    )
    assert response.status_code == 200, response.text
    assert response.json()["ready"]
    assert response.json()["securities"][0]["missing"] == []


@pytest.mark.parametrize(
    "day,expected_gap",
    [
        ("2026-12-29", None),
        ("2026-12-30", "sellable_calendar_unreviewed"),
        ("2026-12-31", "execution_calendar_unreviewed"),
    ],
)
async def test_availability_requires_both_following_sessions_even_with_complete_data(
    simulation_client,
    monkeypatch,
    day,
    expected_gap,
):
    client, listing_id = simulation_client
    identity = create(client, listing_id, end_date="2026-12-29")["id"]
    current = datetime.fromisoformat(f"{day}T10:00:00+00:00")
    monkeypatch.setattr(api, "_now", lambda: current)
    await add_ready_inputs(listing_id, captured_at=current, trade_date=day)
    imported = client.post(
        f"/api/simulations/{identity}/execution-evidence",
        headers=H,
        json=evidence_body(listing_id, day=day),
    )
    assert imported.status_code == 201, imported.text
    result = client.get(f"/api/simulations/{identity}/availability?trade_date={day}", headers=H)
    assert result.status_code == 200, result.text
    assert result.json()["securities"][0]["ready"]
    if expected_gap is None:
        assert result.json()["ready"]
    else:
        assert not result.json()["ready"]
        assert expected_gap in result.json()["missing"]
