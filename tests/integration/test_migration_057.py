"""Alembic 057 converts stored legacy model settings and leaves an empty database alone."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet

from investment_agent.settings.crypto import SecretCipher
from tests.integration.test_migration_046 import _config

ROOT = Path(__file__).resolve().parents[2]


def test_057_follows_056():
    assert ScriptDirectory(str(ROOT / "alembic")).get_revision("057").down_revision == "056"


def test_empty_database_upgrade_adds_no_model_rows(tmp_path, monkeypatch):
    database = tmp_path / "empty.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        head = ScriptDirectory(str(ROOT / "alembic")).get_current_head()
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        rows = connection.execute("SELECT key FROM app_setting").fetchall()
    assert rows == []
    command.downgrade(config, "056")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT key FROM app_setting").fetchall() == []


def test_legacy_rows_become_catalog_and_slots_then_disappear(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    cipher = SecretCipher(key)
    database = tmp_path / "legacy.db"
    config = _config(database, monkeypatch)
    command.upgrade(config, "056")
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO app_setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("agent_base_url", json.dumps("https://agent.invalid/v1"), now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("agent_model", json.dumps("agent-model"), now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, ciphertext, secret_hint, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            ("agent_api_key", cipher.encrypt("agent-secret-value"), "alue", now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("llm_tier3_base_url", json.dumps("https://research.invalid/v1"), now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("llm_tier3_model", json.dumps("explain-model"), now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, ciphertext, secret_hint, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            ("llm_tier3_api_key", cipher.encrypt("research-secret-value"), "alue", now, "test"),
        )
        connection.execute(
            "INSERT INTO app_setting (key, value_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            ("llm_rule_draft_thinking", json.dumps("enabled"), now, "test"),
        )
    command.upgrade(config, "057")
    with sqlite3.connect(database) as connection:
        keys = {row[0] for row in connection.execute("SELECT key FROM app_setting")}
        assert "agent_api_key" not in keys and "llm_tier3_api_key" not in keys
        assert "agent_model" not in keys and "llm_rule_draft_thinking" not in keys
        assert {"llm_catalog", "model_slots"} <= keys
        catalog_row = connection.execute(
            "SELECT ciphertext, value_json FROM app_setting WHERE key = 'llm_catalog'"
        ).fetchone()
        slots_row = connection.execute("SELECT value_json FROM app_setting WHERE key = 'model_slots'").fetchone()
    assert catalog_row[1] in (None, "null")
    catalog = json.loads(cipher.decrypt(catalog_row[0]))
    assert "agent-secret-value" not in catalog_row[0]
    providers = {item["base_url"]: item for item in catalog["providers"]}
    assert providers["https://agent.invalid/v1"]["api_key"] == "agent-secret-value"
    assert providers["https://research.invalid/v1"]["api_key"] == "research-secret-value"
    slots = json.loads(slots_row[0])
    if isinstance(slots, str):
        slots = json.loads(slots)
    assert slots["investor"]["main"]
    assert slots["research"]["explain"]
    assert slots["rule_draft"]["main"]
    assert "bindings" not in catalog
