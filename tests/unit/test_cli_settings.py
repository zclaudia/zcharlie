"""``ia settings`` list / get / set / import / history (offline)."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from investment_agent.storage.database import init_engine
from investment_agent.storage.models.base import Base

SECRET = "sk-cli-secret-9876"


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.chdir(tmp_path)  # no stray .env / config/app.yaml
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def _invoke(*args: str, input: str | None = None):
    from investment_agent.cli.main import app

    return CliRunner().invoke(app, ["settings", *args], input=input)


def test_settings_help_lists_commands() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    for command in ("list", "get", "set", "import", "history"):
        assert command in result.output


def test_set_get_list_history(cli_db) -> None:
    result = _invoke("set", "candidate_expiry_days", "21", "--note", "test")
    assert result.exit_code == 0, result.output
    assert "Updated candidate_expiry_days" in result.output
    assert _invoke("get", "candidate_expiry_days").output.strip() == "21"

    result = _invoke("set", "schedule.screen.universes", "CSI300,SSE50")
    assert result.exit_code == 0, result.output
    assert _invoke("get", "schedule.screen.universes").output.strip() == '["CSI300", "SSE50"]'
    assert _invoke("set", "schedule.screen.cron", "off").exit_code == 0
    assert _invoke("get", "schedule.screen.cron").output.strip() == "null"

    result = _invoke("set", "candidate_expiry_days", "0")
    assert result.exit_code == 1
    assert "candidate_expiry_days" in result.output
    assert _invoke("set", "no_such_key", "1").exit_code == 2

    listing = _invoke("list")
    assert listing.exit_code == 0
    assert "candidate_expiry_days" in listing.output and "未设置" in listing.output

    history = _invoke("history")
    assert history.exit_code == 0
    assert "candidate_expiry_days" in history.output


def test_secret_set_from_hidden_prompt_is_never_printed(cli_db) -> None:
    result = _invoke("set", "llm_tier3_api_key", "-", input=f"{SECRET}\n")
    assert result.exit_code == 0, result.output
    for args in (("get", "llm_tier3_api_key"), ("list",), ("history",)):
        output = _invoke(*args).output
        assert SECRET not in output
    assert "9876" in _invoke("get", "llm_tier3_api_key").output


def test_secret_set_without_key_is_refused(cli_db, monkeypatch) -> None:
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "")
    result = _invoke("set", "ifind_proxy_api_key", SECRET)
    assert result.exit_code == 1
    assert "SETTINGS_ENCRYPTION_KEY" in result.output


def test_import_once_then_force(cli_db, monkeypatch) -> None:
    monkeypatch.setenv("CANDIDATE_DISMISS_COOLDOWN_DAYS", "45")
    result = _invoke("import")
    assert result.exit_code == 0, result.output
    assert "candidate_dismiss_cooldown_days" in result.output
    assert _invoke("get", "candidate_dismiss_cooldown_days").output.strip() == "45"
    assert "Already imported" in _invoke("import").output
    assert _invoke("import", "--force").exit_code == 0
