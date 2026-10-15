"""Explicit offline actions expose incomplete coverage without changing GET semantics."""

import asyncio
import json

from typer.testing import CliRunner

from investment_agent.cli.main import app


def _invoke(*args):
    return CliRunner().invoke(app, ["materials", *args])


def test_prepare_requires_timezone_before_opening_database():
    result = _invoke("prepare-publication", "--listing-id", "1", "--as-of", "2026-10-15")
    assert result.exit_code == 2
    assert "timezone" in result.output


async def test_prepare_command_commits_and_reuses_exact_refs(api_client, tmp_path):
    from datetime import UTC, datetime

    from investment_agent.storage.database import session_scope
    from tests.unit.test_material_publication_prepare import _four_year_dataset

    async with session_scope() as session:
        listing, dataset = await _four_year_dataset(session, tmp_path)
        listing_id, dataset_id = listing.id, dataset.id
    args = (
        "prepare-publication", "--listing-id", str(listing_id),
        "--dataset-id", str(dataset_id), "--as-of", datetime.now(UTC).isoformat(),
    )
    first = await asyncio.to_thread(_invoke, *args)
    assert first.exit_code == 2, first.output  # oldest ROE has no beginning equity
    report = json.loads(first.output)
    assert len({metric["key"] for metric in report["metrics"]}) == 6
    prepared = [metric for metric in report["metrics"] if metric["calculation_ref"]]
    assert len({metric["key"] for metric in prepared}) == 6
    assert all(metric["fact_refs"] for metric in prepared)
    second = await asyncio.to_thread(_invoke, *args)
    assert second.exit_code == 2, second.output
    assert json.loads(second.output) == report


def test_verify_reports_missing_companies_without_database_or_network(tmp_path, monkeypatch):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("offline evidence verifier cannot contact network")

    monkeypatch.setattr(httpx.Client, "get", forbidden)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "as_of": "2026-10-15", "companies": []}))
    result = _invoke("verify-annual", "--manifest", str(manifest), "--source-root", str(tmp_path))
    assert result.exit_code == 2, result.output
    report = json.loads(result.output)
    assert report["complete"] is False
    assert len(report["companies"]) == 8
    assert all(item["status"] == "partial" for item in report["companies"])


def test_verify_invalid_cutoff_is_cli_error(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    result = _invoke(
        "verify-annual", "--manifest", str(manifest), "--source-root", str(tmp_path),
        "--as-of", "not-a-date",
    )
    assert result.exit_code == 2
    assert "--as-of" in result.output
