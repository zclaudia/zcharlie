"""SettingsStore / loader: validation, history, revision, secrets at rest, import."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from structlog.testing import capture_logs

from investment_agent.config import Settings
from investment_agent.settings.crypto import (
    KEY_GENERATOR_COMMAND,
    EncryptionKeyError,
    SecretCipher,
    SecretDecryptError,
)
from investment_agent.settings.loader import (
    ensure_imported,
    env_import_values,
    load_settings,
    yaml_import_values,
)
from investment_agent.settings.store import SettingsStore, SettingsValidationError
from investment_agent.storage.models.app_settings import AppSetting, AppSettingMeta

KEY = Fernet.generate_key().decode()
SECRET = "sk-live-abcdefgh1234"


def _settings(**values: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="x" * 32,
        llm_tier3_api_key=values.pop("llm_tier3_api_key", "env-llm-key"),
        settings_encryption_key=values.pop("settings_encryption_key", KEY),
        **values,
    )


def _bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("API_KEY", "LLM_TIER3_API_KEY", "CANDIDATE_EXPIRY_DAYS", "IFIND_BUDGET_PER_5H"):
        monkeypatch.delenv(name, raising=False)


# --- crypto -----------------------------------------------------------------


def test_cipher_round_trip_and_key_errors() -> None:
    cipher = SecretCipher(KEY)
    token = cipher.encrypt(SECRET)
    assert SECRET not in token
    assert cipher.decrypt(token) == SECRET
    for bad in ("", "  ", "not-a-fernet-key"):
        with pytest.raises(EncryptionKeyError) as excinfo:
            SecretCipher(bad)
        assert KEY_GENERATOR_COMMAND in str(excinfo.value)
    with pytest.raises(SecretDecryptError):
        SecretCipher(Fernet.generate_key().decode()).decrypt(token)


# --- store --------------------------------------------------------------------


async def test_load_defaults_on_empty_store(db_session) -> None:
    store = SettingsStore(db_session)
    runtime = await store.load()
    assert runtime.candidate_expiry_days == 14
    assert runtime.schedule.screen.cron == "0 20 * * sun"
    assert await store.revision() == 0
    assert await store.imported_at() is None


async def test_apply_validates_writes_history_and_bumps_revision(db_session) -> None:
    store = SettingsStore(db_session)
    changed = await store.apply(
        {"candidate_expiry_days": "21", "schedule.screen.cron": "0 21 * * sun"},
        actor="cli",
        note="longer review window",
    )
    assert sorted(changed) == ["candidate_expiry_days", "schedule.screen.cron"]
    runtime = await store.load()
    assert runtime.candidate_expiry_days == 21  # coerced from the CLI string
    assert runtime.schedule.screen.cron == "0 21 * * sun"
    assert await store.revision() == 1

    history = await store.history()
    assert [(row.key, row.old_value_json, row.new_value_json) for row in history] == [
        ("schedule.screen.cron", "0 20 * * sun", "0 21 * * sun"),
        ("candidate_expiry_days", 14, 21),
    ]
    assert {row.actor for row in history} == {"cli"}
    assert {row.note for row in history} == {"longer review window"}

    # Same values again: no change, no history, no revision bump.
    assert await store.apply({"candidate_expiry_days": 21}, actor="cli") == []
    assert await store.revision() == 1
    assert len(await store.history()) == 2


async def test_apply_pins_default_without_history(db_session) -> None:
    store = SettingsStore(db_session)
    assert await store.apply({"candidate_expiry_days": 14}, actor="import") == []
    assert await store.revision() == 0
    assert await store.history() == []
    described = {item["key"]: item for item in await store.describe()}
    assert described["candidate_expiry_days"]["stored"] is True
    assert described["candidate_expiry_days"]["is_default"] is True


async def test_apply_rejects_with_per_key_issues_and_writes_nothing(db_session) -> None:
    store = SettingsStore(db_session)
    with pytest.raises(SettingsValidationError) as excinfo:
        await store.apply(
            {
                "candidate_expiry_days": 0,
                "schedule.screen.cron": "0 20 * * 9",
                "candidate_expiry_days_typo": 3,
            },
            actor="cli",
        )
    assert set(excinfo.value.issues) == {"candidate_expiry_days_typo"}
    with pytest.raises(SettingsValidationError) as excinfo:
        await store.apply(
            {"candidate_expiry_days": 0, "schedule.screen.cron": "0 20 * * 9"}, actor="cli"
        )
    assert set(excinfo.value.issues) == {"candidate_expiry_days", "schedule.screen.cron"}
    assert (await db_session.execute(select(AppSetting))).scalars().all() == []
    assert await store.revision() == 0


async def test_load_falls_back_to_default_for_a_stored_value_now_invalid(db_session) -> None:
    store = SettingsStore(db_session)
    await store.apply({"candidate_expiry_days": 30, "ifind_budget_per_5h": 200}, actor="cli")
    row = await db_session.get(AppSetting, "candidate_expiry_days")
    row.value_json = 9999  # e.g. a bound tightened by a later release
    await db_session.flush()
    with capture_logs() as logs:
        runtime = await store.load()
    assert runtime.candidate_expiry_days == 14
    assert runtime.ifind_budget_per_5h == 200
    assert any(entry["event"] == "settings.invalid_stored_values" for entry in logs)


async def test_secret_encrypted_at_rest_and_masked_everywhere(db_session) -> None:
    store = SettingsStore(db_session, encryption_key=KEY)
    assert await store.apply({"llm_tier3_api_key": SECRET}, actor="ui") == ["llm_tier3_api_key"]
    assert (await store.load()).llm_tier3_api_key == SECRET

    raw = (await db_session.execute(text("SELECT * FROM app_setting"))).all()
    changes = (await db_session.execute(text("SELECT * FROM app_setting_change"))).all()
    assert SECRET not in repr(raw) + repr(changes)
    row = await db_session.get(AppSetting, "llm_tier3_api_key")
    assert row.value_json is None and row.secret_hint == "1234"

    (change,) = await store.history()
    assert (change.old_value_json, change.new_value_json) == (None, "***1234")
    described = {item["key"]: item for item in await store.describe()}
    item = described["llm_tier3_api_key"]
    assert "value" not in item
    assert (item["configured"], item["hint"], item["decrypt_error"]) == (True, "1234", None)

    # Empty string clears; no key needed for that.
    cleared = SettingsStore(db_session)
    assert await cleared.apply({"llm_tier3_api_key": ""}, actor="ui") == ["llm_tier3_api_key"]
    assert (await cleared.load()).llm_tier3_api_key == ""
    assert (await cleared.history())[0].new_value_json is None


async def test_secret_write_without_key_fails_cleanly(db_session) -> None:
    store = SettingsStore(db_session)
    with pytest.raises(EncryptionKeyError):
        await store.apply(
            {"ifind_proxy_api_key": SECRET, "candidate_expiry_days": 20}, actor="ui"
        )
    assert (await db_session.execute(select(AppSetting))).scalars().all() == []


@pytest.mark.parametrize("read_key", ["", Fernet.generate_key().decode()])
async def test_unreadable_secret_resolves_empty_and_keeps_others(db_session, read_key) -> None:
    await SettingsStore(db_session, encryption_key=KEY).apply(
        {"llm_tier3_api_key": SECRET, "candidate_expiry_days": 21}, actor="ui"
    )
    store = SettingsStore(db_session, encryption_key=read_key)
    with capture_logs() as logs:
        runtime = await store.load()
    assert runtime.llm_tier3_api_key == ""
    assert runtime.candidate_expiry_days == 21
    assert any(entry["event"] == "settings.secret_unreadable" for entry in logs)
    described = {item["key"]: item for item in await store.describe()}
    assert described["llm_tier3_api_key"]["configured"] is True
    assert described["llm_tier3_api_key"]["decrypt_error"]


async def test_rewriting_an_unreadable_secret_with_a_new_key(db_session) -> None:
    await SettingsStore(db_session, encryption_key=KEY).apply(
        {"llm_tier3_api_key": SECRET}, actor="ui"
    )
    new_key = Fernet.generate_key().decode()
    store = SettingsStore(db_session, encryption_key=new_key)
    assert await store.apply({"llm_tier3_api_key": SECRET}, actor="ui") == ["llm_tier3_api_key"]
    assert (await store.load()).llm_tier3_api_key == SECRET


# --- import / loader ----------------------------------------------------------------


def _app_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "app.yaml"
    path.write_text(body)
    return path


def test_env_import_values_are_explicit_only(monkeypatch) -> None:
    _bootstrap_env(monkeypatch)
    monkeypatch.setenv("CANDIDATE_EXPIRY_DAYS", "21")
    values = env_import_values(_settings())
    assert values == {"candidate_expiry_days": 21, "llm_tier3_api_key": "env-llm-key"}


def test_yaml_import_values(tmp_path) -> None:
    path = _app_yaml(
        tmp_path,
        "scheduler:\n"
        "  watchlist_refresh_cron: '30 19 * * 1-5'\n"
        "  a_share_calendar_path: config/calendars/a_share.yaml\n"
        "screen:\n"
        "  universes: [CSI300, SSE50]\n"
        "  cron: '0 20 * * 0'\n",
    )
    assert yaml_import_values(path) == {
        "schedule.watchlist.cron": "30 19 * * 1-5",
        "schedule.screen.universes": ["CSI300", "SSE50"],
        "schedule.screen.cron": "0 20 * * 0",
        # Absent sections are disabled by the scheduler today: import them so.
        "schedule.snapshot.universe_sync_cron": None,
        "schedule.snapshot.industry_sync_cron": None,
        "schedule.snapshot.market_snapshot_cron": None,
        "schedule.snapshot.close_quote_cron": None,
        "schedule.snapshot.close_quote_retry_cron": None,
        "schedule.snapshot.financial_weekly_cron": None,
        "schedule.snapshot.financial_deadline_cron": None,
        "schedule.candidates.expiry_sweep_cron": None,
        "schedule.verification.dispatch_cron": None,
        "schedule.verification.calibration_cron": None,
    }
    assert yaml_import_values(tmp_path / "missing.yaml") == {}


def test_repo_app_yaml_imports_cleanly() -> None:
    from investment_agent.settings.registry import RuntimeSettings, defaults, unflatten

    values = yaml_import_values(Path(__file__).resolve().parents[2] / "config" / "app.yaml")
    assert values["schedule.screen.cron"] == "0 20 * * 0"
    RuntimeSettings.model_validate(unflatten({**defaults(), **values}))


async def test_ensure_imported_once_and_force(db_session, tmp_path, monkeypatch) -> None:
    _bootstrap_env(monkeypatch)
    monkeypatch.setenv("IFIND_BUDGET_PER_5H", "120")
    yaml_path = _app_yaml(tmp_path, "scheduler:\n  watchlist_refresh_cron: '30 19 * * 1-5'\n")
    settings = _settings()

    imported = await ensure_imported(db_session, settings, yaml_path)
    assert imported is not None
    assert {"ifind_budget_per_5h", "llm_tier3_api_key", "schedule.watchlist.cron"} <= set(
        imported
    )
    store = SettingsStore(db_session, encryption_key=KEY)
    runtime = await store.load()
    assert runtime.ifind_budget_per_5h == 120
    assert runtime.llm_tier3_api_key == "env-llm-key"
    assert runtime.schedule.watchlist.cron == "30 19 * * 1-5"
    assert runtime.candidate_expiry_days == 14  # not set in env: not imported
    assert await db_session.get(AppSetting, "candidate_expiry_days") is None
    assert {row.actor for row in await store.history(200)} == {"import"}
    meta = await db_session.get(AppSettingMeta, 1)
    assert meta.imported_at is not None
    revision = await store.revision()

    # Second start: nothing imported; env drift is only warned about.
    monkeypatch.setenv("IFIND_BUDGET_PER_5H", "90")
    with capture_logs() as logs:
        assert await ensure_imported(db_session, _settings(), yaml_path) is None
    assert (await store.load()).ifind_budget_per_5h == 120
    assert await store.revision() == revision
    (drift,) = [entry for entry in logs if entry["event"] == "settings.env_ignored"]
    assert drift["keys"] == ["ifind_budget_per_5h"]

    # --force re-imports the current env values.
    assert await ensure_imported(db_session, _settings(), yaml_path, force=True) == [
        "ifind_budget_per_5h"
    ]
    assert (await store.load()).ifind_budget_per_5h == 90


async def test_import_without_key_skips_secrets_and_loader_keeps_env(
    db_session, tmp_path, monkeypatch
) -> None:
    _bootstrap_env(monkeypatch)
    monkeypatch.setenv("CANDIDATE_EXPIRY_DAYS", "21")
    settings = _settings(settings_encryption_key="")
    with capture_logs() as logs:
        imported = await ensure_imported(db_session, settings, tmp_path / "none.yaml")
    assert imported == ["candidate_expiry_days"]
    assert any(entry["event"] == "settings.import_skipped_secrets" for entry in logs)
    assert await db_session.get(AppSetting, "llm_tier3_api_key") is None

    effective = await load_settings(db_session, base=settings)
    assert effective.candidate_expiry_days == 21
    assert effective.llm_tier3_api_key == "env-llm-key"  # no stored row: env fallback


async def test_load_settings_overlay(db_session, tmp_path, monkeypatch) -> None:
    _bootstrap_env(monkeypatch)
    base = _settings(candidate_expiry_days=40, ifind_proxy_api_key="env-ifind-key")

    # Before the import the env Settings is returned unchanged.
    assert await load_settings(db_session, base=base) is base

    await ensure_imported(db_session, base, tmp_path / "none.yaml")
    store = SettingsStore(db_session, encryption_key=KEY)
    await store.apply(
        {"candidate_expiry_days": 7, "ifind_proxy_api_key": "", "llm_tier3_model": "m2"},
        actor="ui",
    )
    effective = await load_settings(db_session, base=base)
    assert effective.candidate_expiry_days == 7
    assert effective.ifind_proxy_api_key == ""  # cleared in the DB wins over env
    assert effective.llm_tier3.model == "m2"
    assert effective.api_key == base.api_key  # bootstrap layer untouched
    assert effective.database_url == base.database_url


async def test_import_drops_invalid_values(db_session, tmp_path, monkeypatch) -> None:
    _bootstrap_env(monkeypatch)
    yaml_path = _app_yaml(
        tmp_path,
        "scheduler:\n  watchlist_refresh_cron: '30 19 * * 9'\n"
        "screen:\n  cron: '0 21 * * sun'\n",
    )
    with capture_logs() as logs:
        imported = await ensure_imported(db_session, _settings(), yaml_path)
    assert "schedule.watchlist.cron" not in imported
    assert "schedule.screen.cron" in imported
    assert any(entry["event"] == "settings.import_skipped_invalid" for entry in logs)


async def test_concurrent_ensure_imported_imports_once(tmp_path, monkeypatch) -> None:
    import asyncio

    from investment_agent.storage.database import engine_lifespan, session_scope

    _bootstrap_env(monkeypatch)
    monkeypatch.setenv("CANDIDATE_EXPIRY_DAYS", "21")
    settings = _settings()
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}") as engine:
        async with engine.begin() as conn:
            await conn.run_sync(AppSettingMeta.metadata.create_all)
        async with session_scope() as session:
            session.add(AppSettingMeta(id=1, revision=0, imported_at=None))  # as migration 021

        async def start() -> list[str] | None:
            async with session_scope() as session:
                return await ensure_imported(session, settings, tmp_path / "none.yaml")

        results = await asyncio.gather(*(start() for _ in range(4)))
        assert sorted(result is None for result in results) == [False, True, True, True]
        async with session_scope() as session:
            store = SettingsStore(session, encryption_key=KEY)
            assert (await store.load()).candidate_expiry_days == 21
            assert len(await store.history(100)) == len(next(r for r in results if r))
