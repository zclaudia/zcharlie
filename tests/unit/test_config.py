from investment_agent.config import Settings, load_app_yaml


def test_settings_loads_env_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "sk-1")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "sk-2")
    monkeypatch.setenv("LLM_TIER2_MODEL", "deepseek-chat")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_env == "dev"
    assert s.api_key == "test-key"
    assert s.llm_tier2.model == "deepseek-chat"
    assert s.yfinance_max_concurrent == 10
    assert s.ifind_proxy_url == "http://127.0.0.1:7100"


def test_app_yaml_loads(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(
        "universes:\n"
        "  - code: NASDAQ_100\n"
        "    refresh_membership_freq: weekly\n"
        "enabled_strategies: [value]\n"
        "pipeline:\n"
        "  tier2_score_threshold: 40\n"
        "  tier3_top_n: 10\n"
        "  deep_analysis_cache_days: 90\n"
        "scheduler:\n"
        "  discovery_cron: '30 16 * * 1-5'\n"
        "  discovery_timezone: America/New_York\n"
        "auxiliary_tracking:\n"
        "  strength_threshold: 0.5\n"
        "  max_per_company: 5\n"
        "  total_cap_multiplier: 2\n"
    )
    cfg = load_app_yaml(p)
    assert cfg.pipeline.tier2_score_threshold == 40
    assert cfg.scheduler.discovery_timezone == "America/New_York"
    assert cfg.scheduler.watchlist_refresh_cron is None
    assert cfg.scheduler.watchlist_refresh_timezone == "Asia/Shanghai"
    assert cfg.auxiliary_tracking.strength_threshold == 0.5


def _env(monkeypatch, tmp_path, **values):
    monkeypatch.setenv("LLM_TIER2_API_KEY", "sk-1")
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
