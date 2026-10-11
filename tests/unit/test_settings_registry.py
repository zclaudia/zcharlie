"""Runtime settings registry: keys, bounds mirrored from ``Settings``, schedule checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investment_agent.config import (
    CandidateScheduleConfig,
    SchedulerConfig,
    ScreenScheduleConfig,
    Settings,
    SnapshotScheduleConfig,
    VerificationScheduleConfig,
)
from investment_agent.settings.registry import (
    ENV_KEYS,
    GROUPS,
    SECRET_KEYS,
    SPECS,
    SPECS_BY_KEY,
    RuntimeSettings,
    defaults,
    flatten,
    unflatten,
)


def test_specs_are_unique_and_complete() -> None:
    assert len(SPECS_BY_KEY) == len(SPECS)
    assert set(SECRET_KEYS) == {"llm_tier3_api_key", "ifind_proxy_api_key", "gateway_credential"}
    for spec in SPECS:
        assert spec.group in GROUPS
        assert spec.label and spec.help
        assert (spec.source == "app_yaml") == (spec.yaml_path is not None)
        assert (spec.source == "app_yaml") == spec.key.startswith("schedule.")
    # Every leaf of RuntimeSettings is registered, and nothing else.
    assert set(flatten(RuntimeSettings())) == set(SPECS_BY_KEY)
    assert unflatten(defaults()) == RuntimeSettings().model_dump(mode="json")


def test_env_keys_mirror_settings_fields() -> None:
    runtime_fields = RuntimeSettings.model_fields
    for key in ENV_KEYS:
        mine, theirs = runtime_fields[key], Settings.model_fields[key]
        assert mine.annotation == theirs.annotation, key
        assert mine.metadata == theirs.metadata, key
        if key != "llm_tier3_api_key":  # required in Settings, "" = unset here
            assert mine.default == theirs.default, key
    bootstrap = {
        "app_env", "log_level", "data_dir", "api_key", "database_url",
        "web_session_ttl_minutes", "web_session_secret", "web_cookie_secure",
        "web_public_origin", "settings_encryption_key",
    }
    assert set(Settings.model_fields) == bootstrap | set(ENV_KEYS)


def test_schedule_defaults_match_app_yaml_models() -> None:
    schedule = RuntimeSettings().schedule
    assert schedule.watchlist.cron == SchedulerConfig().watchlist_refresh_cron
    assert schedule.watchlist.timezone == SchedulerConfig().watchlist_refresh_timezone
    assert schedule.snapshot.model_dump() == SnapshotScheduleConfig().model_dump()
    assert schedule.screen.model_dump() == ScreenScheduleConfig().model_dump()
    assert schedule.candidates.model_dump() == CandidateScheduleConfig().model_dump()
    assert schedule.verification.model_dump() == VerificationScheduleConfig().model_dump()
    assert isinstance(schedule.screen, ScreenScheduleConfig)  # usable by the scheduler


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("candidate_expiry_days", 0),
        ("candidate_expiry_days", 366),
        ("ifind_budget_window_hours", 0),
        ("snapshot_completeness_threshold", 1.5),
        ("llm_tier3_thinking", "sometimes"),
        ("schedule.screen.cron", "0 20 * * 9"),
        ("schedule.screen.cron", "not a cron"),
        ("schedule.watchlist.cron", "61 19 * * 1-5"),
        ("schedule.verification.timezone", "Mars/Olympus"),
        ("schedule.snapshot.universes", ["CSI300", "NASDAQ"]),
        ("schedule.snapshot.universes", []),
        ("schedule.screen.hit_cap", 0),
    ],
)
def test_invalid_values_rejected_at_their_key(key: str, value: object) -> None:
    with pytest.raises(ValidationError) as excinfo:
        RuntimeSettings.model_validate(unflatten({key: value}))
    locs = {".".join(str(part) for part in error["loc"]) for error in excinfo.value.errors()}
    assert locs == {key}


def test_crontab_weekdays_and_null_crons_accepted() -> None:
    runtime = RuntimeSettings.model_validate(
        unflatten(
            {
                "schedule.screen.cron": "0 20 * * 7",  # 7 = Sunday in crontab
                "schedule.watchlist.cron": "30 19 * * 1-5",
                "schedule.snapshot.industry_sync_cron": None,
            }
        )
    )
    assert runtime.schedule.screen.cron == "0 20 * * 7"
    assert runtime.schedule.snapshot.industry_sync_cron is None
