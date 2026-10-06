from investment_agent.config import Settings, load_app_yaml


def test_settings_loads_env_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "sk-2")
    monkeypatch.setenv("LLM_TIER3_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env == "dev"
    assert s.api_key == "test-key"
    assert s.llm_tier3.model == "deepseek-v4-flash"
    assert s.ifind_proxy_url == "http://127.0.0.1:7100"


def test_settings_propagate_llm_request_timeout():
    from investment_agent.llm.openai_compatible import OpenAICompatibleClient

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="t",
        llm_tier3_api_key="t",
        llm_request_timeout_seconds=33.0,
    )
    assert settings.llm_tier3.timeout_seconds == 33.0
    client = OpenAICompatibleClient(
        tier_name="tier3", config=settings.llm_tier3, session=None  # type: ignore[arg-type]
    )
    assert client._client.timeout == 33.0


def test_app_yaml_loads(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(
        "scheduler:\n"
        "  watchlist_refresh_cron: '30 15 * * 1-5'\n"
    )
    cfg = load_app_yaml(p)
    assert cfg.scheduler.watchlist_refresh_cron == "30 15 * * 1-5"
    assert cfg.scheduler.watchlist_refresh_timezone == "Asia/Shanghai"
    assert cfg.scheduler.a_share_calendar_path == "config/calendars/a_share.yaml"


def test_app_yaml_tolerates_retired_legacy_keys(tmp_path):
    """An app.yaml from before the legacy removal still loads (keys ignored)."""
    p = tmp_path / "app.yaml"
    p.write_text(
        "universes:\n"
        "  - code: NASDAQ_100\n"
        "enabled_strategies: [value]\n"
        "pipeline:\n"
        "  tier2_score_threshold: 40\n"
        "scheduler:\n"
        "  discovery_cron: '30 16 * * 1-5'\n"
        "  discovery_timezone: America/New_York\n"
        "auxiliary_tracking:\n"
        "  strength_threshold: 0.5\n"
    )
    cfg = load_app_yaml(p)
    assert cfg.scheduler.watchlist_refresh_cron is None


def test_shipped_app_yaml_loads():
    from pathlib import Path

    cfg = load_app_yaml(Path(__file__).parents[2] / "config" / "app.yaml")
    assert cfg.scheduler.watchlist_refresh_cron is not None


def _env(monkeypatch, tmp_path, **values):
    monkeypatch.setenv("LLM_TIER3_API_KEY", "sk-2")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_empty_api_key_rejected(monkeypatch, tmp_path):
    import pytest
    from pydantic import ValidationError

    for blank in ("", "   "):
        _env(monkeypatch, tmp_path, API_KEY=blank)
        with pytest.raises(ValidationError, match="must not be empty"):
            Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_rejects_short_or_placeholder_api_key(monkeypatch, tmp_path):
    import pytest
    from pydantic import ValidationError

    for weak in ("short-key", "change-me-" + "x" * 40):
        _env(monkeypatch, tmp_path, APP_ENV="prod", API_KEY=weak)
        with pytest.raises(ValidationError, match="too weak"):
            Settings(_env_file=None)  # type: ignore[call-arg]


def test_prod_accepts_strong_api_key(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, APP_ENV="prod", API_KEY="Z" * 43)
    assert Settings(_env_file=None).app_env == "prod"  # type: ignore[call-arg]


def test_dev_weak_api_key_warns_once(monkeypatch, tmp_path, caplog):
    import investment_agent.config as config_module

    monkeypatch.setattr(config_module, "_weak_key_warned", False)
    _env(monkeypatch, tmp_path, API_KEY="change-me-dev-key")
    with caplog.at_level("WARNING", logger="investment_agent.config"):
        Settings(_env_file=None)  # type: ignore[call-arg]
        Settings(_env_file=None)  # type: ignore[call-arg]
    warnings = [r for r in caplog.records if "API_KEY is weak" in r.getMessage()]
    assert len(warnings) == 1


def test_web_security_settings_defaults(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, API_KEY="test-key")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.web_cookie_secure is False
    assert s.web_session_secret == ""
    assert s.web_public_origin == ""
