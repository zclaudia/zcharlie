import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from investment_agent.cli.commands.bank_rollover import app


def test_rollover_cli_exposes_offline_review_and_explicit_acquire():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("acquire", "prepare", "verify", "reviews", "resolve"):
        assert command in result.stdout
    reviews_help = runner.invoke(app, ["reviews", "--help"])
    assert reviews_help.exit_code == 0
    assert "list" in reviews_help.stdout


def test_resolve_requires_explicit_review_audit_and_decision():
    runner = CliRunner()
    result = runner.invoke(app, ["resolve", "review-1", "--help"])
    assert result.exit_code == 0
    assert "--audit" in result.stdout
    assert "--decision" in result.stdout


def test_resolve_cli_dispatches_audit_and_decision_to_review_service(tmp_path, monkeypatch):
    from investment_agent.cli.commands import bank_rollover

    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({"schema": "bank-annual-review-audit/2", "fields": {}}))
    calls = {}

    @asynccontextmanager
    async def session_scope():
        yield object()

    async def resolver(session, **kwargs):
        calls.update(kwargs)
        return {"event_id": "event-1", "decision": kwargs["decision"]}

    monkeypatch.setattr(
        bank_rollover, "bootstrap_settings", lambda: SimpleNamespace(database_url="sqlite://")
    )
    monkeypatch.setattr(bank_rollover, "session_scope", session_scope)
    monkeypatch.setattr(bank_rollover, "run_with_engine", lambda _url, main: asyncio.run(main()))
    monkeypatch.setattr(bank_rollover, "resolve_bank_annual_review", resolver)
    result = CliRunner().invoke(
        app,
        ["resolve", "review-42", "--audit", str(audit_path), "--decision", "reject_supplier"],
    )
    assert result.exit_code == 0, result.exception
    assert calls["review_id_value"] == "review-42"
    assert calls["audit"]["schema"] == "bank-annual-review-audit/2"
    assert calls["decision"] == "reject_supplier"


def test_verify_does_not_create_ghost_review_against_online_candidate(tmp_path, monkeypatch):
    from investment_agent.cli.commands import bank_rollover

    period = date(2024, 12, 31)
    stamp = datetime(2025, 5, 1, tzinfo=UTC)
    listing = SimpleNamespace(id=7, company_id=70, ticker="601398.SH")
    cohort = SimpleNamespace(payload={})
    trusted = SimpleNamespace(
        id="verified-A",
        content_hash="a" * 64,
        fields={
            metric: {"value": "0.01"}
            for metric in (
                "roe_weighted_annual",
                "net_interest_margin",
                "npl_ratio",
                "provision_coverage",
                "equity_parent",
            )
        },
    )
    online_candidate = SimpleNamespace(id="supplier-B", fields={"npl_ratio": {"value": "0.02"}})
    field_values = {
        metric: {"value": "0.01", "verification": "verified", "period": {}}
        for metric in bank_rollover.FIELDS
    }
    proofs = {metric: {"status": "verified"} for metric in bank_rollover.FIELDS}
    snapshot = SimpleNamespace(
        id="new-A",
        fields=field_values,
        verification=proofs,
        semantic_hash="semantic",
    )
    calls = {"reviews": []}

    class Session:
        async def get(self, *_args):
            return None

        def add(self, _row):
            pass

        async def flush(self):
            pass

    @asynccontextmanager
    async def session_scope():
        yield Session()

    async def eligible(_session, _cohort, _listing_id, _period):
        return listing, cohort

    async def prior(*_args, **_kwargs):
        return online_candidate

    async def trusted_baseline(*_args, **kwargs):
        calls.setdefault("baseline_metrics", []).append(kwargs["metric"])
        return trusted

    async def build(*_args, **_kwargs):
        return snapshot, proofs

    async def append(_session, *, event, **_kwargs):
        calls["reviews"].append(event)
        return "event"

    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            {
                "schema": "bank-annual-review-audit/2",
                "listing_id": listing.id,
                "code": listing.ticker,
                "report_period": period.isoformat(),
                "fields": {},
            }
        )
    )
    monkeypatch.setattr(
        bank_rollover, "bootstrap_settings", lambda: SimpleNamespace(database_url="sqlite://")
    )
    monkeypatch.setattr(bank_rollover, "session_scope", session_scope)
    monkeypatch.setattr(bank_rollover, "run_with_engine", lambda _url, main: asyncio.run(main()))
    monkeypatch.setattr(bank_rollover, "_eligible", eligible)
    monkeypatch.setattr(bank_rollover, "latest_same_year_snapshot", prior)
    monkeypatch.setattr(bank_rollover, "_verified_review_baseline", trusted_baseline)
    monkeypatch.setattr(bank_rollover, "build_rollover_snapshot", build)
    monkeypatch.setattr(bank_rollover, "append_review_event", append)

    result = CliRunner().invoke(
        app,
        [
            "verify",
            "--cohort",
            "banks",
            "--listing-id",
            str(listing.id),
            "--period",
            period.isoformat(),
            "--audit",
            str(audit),
            "--captured-at",
            stamp.isoformat(),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert calls["baseline_metrics"] == list(bank_rollover.FIELDS)
    assert calls["reviews"] == []
