"""Quality flags mirror bootstrap/runtime and register without enabling work."""

from investment_agent.config import Settings
from investment_agent.settings.registry import ENV_KEYS, SPECS_BY_KEY, RuntimeSettings
from investment_agent.storage.models import Base


def test_quality_flags_are_independent_default_off_live_controls():
    for key in ("research_quality_enabled", "research_quality_execution_enabled"):
        assert Settings.model_fields[key].default is False
        assert getattr(RuntimeSettings(), key) is False
        assert key in ENV_KEYS
        assert SPECS_BY_KEY[key].apply == "live"
        assert not SPECS_BY_KEY[key].secret
    runtime = RuntimeSettings(research_quality_enabled=True)
    assert runtime.research_quality_execution_enabled is False
    runtime = RuntimeSettings(research_quality_execution_enabled=True)
    assert runtime.research_quality_enabled is False


def test_quality_registration_keeps_existing_modules():
    assert {
        "research_experiment", "research_trial", "research_experiment_task",
        "research_assessment", "research_experiment_receipt",
        "investor_method_artifact", "portfolio_stress_head", "topic_case",
    } <= set(Base.metadata.tables)
    assert any(name.startswith("simulation_") for name in Base.metadata.tables)
