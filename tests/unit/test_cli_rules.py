"""``ia rules`` validate / sync / list / show (offline)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from investment_agent.storage.database import init_engine
from investment_agent.storage.models.base import Base
from tests.unit._rules_support import instance_data, template_data


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("COLUMNS", "200")

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _write(path: Path, data: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_validate_template_file(tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    good = _write(tmp_path / "good.yaml", template_data())
    result = CliRunner().invoke(app, ["rules", "validate", str(good)])
    assert result.exit_code == 0, result.output
    assert "Valid template" in result.output

    data = template_data()
    data["holding_zones"]["sell"] = {"any": [{"factor": "roe", "op": "<", "value": "low"}]}
    bad = _write(tmp_path / "bad.yaml", data)
    result = CliRunner().invoke(app, ["rules", "validate", str(bad)])
    assert result.exit_code == 1
    assert "zone_not_allowed" in result.output


def test_sync_list_show_and_validate_instance(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    directory = tmp_path / "templates"
    directory.mkdir()
    _write(directory / "quality_test.yaml", template_data())
    runner = CliRunner()
    first = runner.invoke(app, ["rules", "sync", "--dir", str(directory)])
    assert first.exit_code == 0, first.output
    assert "created" in first.output
    second = runner.invoke(app, ["rules", "sync", "--dir", str(directory)])
    assert "unchanged" in second.output

    listed = runner.invoke(app, ["rules", "list"])
    assert listed.exit_code == 0
    assert "quality_test" in listed.output

    shown = runner.invoke(app, ["rules", "show", "quality_test@1"])
    assert shown.exit_code == 0
    assert "# template quality_test@1" in shown.output
    assert "pe_pct_max" in shown.output
    assert runner.invoke(app, ["rules", "show", "nope"]).exit_code == 1

    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps(instance_data()), encoding="utf-8")
    result = runner.invoke(app, ["rules", "validate", str(instance)])
    assert result.exit_code == 0, result.output
    assert "Valid instance" in result.output
    instance.write_text(json.dumps(instance_data(params={"pe_pct_max": 2})), encoding="utf-8")
    result = runner.invoke(app, ["rules", "validate", str(instance)])
    assert result.exit_code == 1
    assert "param_out_of_range" in result.output


def test_sync_reports_invalid_files(cli_db, tmp_path: Path) -> None:
    from investment_agent.cli.main import app

    directory = tmp_path / "templates"
    directory.mkdir()
    _write(directory / "a.yaml", template_data())
    _write(directory / "b.yaml", template_data())  # duplicate id
    result = CliRunner().invoke(app, ["rules", "sync", "--dir", str(directory)])
    assert result.exit_code == 1
    assert "also defined" in result.output
