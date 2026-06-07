
from investment_agent.config import Settings, load_app_yaml


def test_settings_loads_env_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "sk-1")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "sk-2")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    s = Settings()
    assert s.app_env == "dev"
    assert s.api_key == "test-key"
    assert s.llm_tier2.model == "deepseek-chat"
    assert s.yfinance_max_concurrent == 10


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
    assert cfg.auxiliary_tracking.strength_threshold == 0.5
