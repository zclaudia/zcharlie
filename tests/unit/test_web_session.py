"""Signed web session token unit tests."""


import pytest

from investment_agent.web.session import (
    SESSION_COOKIE_NAME,
    create_session_token,
    read_not_before_us,
    revoke_sessions,
    session_is_valid,
    signing_material,
    verify_session_token,
)


def test_cookie_name_is_stable():
    assert SESSION_COOKIE_NAME == "zcharlie_session"


def test_roundtrip_token_verifies():
    token = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    assert verify_session_token(token, api_key="secret", now=1005.0)


def test_token_never_contains_api_key():
    token = create_session_token(api_key="super-secret-key", ttl_minutes=10)
    assert "super-secret-key" not in token
    assert "super" not in token.replace("super-secret-key", "")


def test_token_rejects_wrong_key():
    token = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    assert not verify_session_token(token, api_key="other", now=1005.0)


def test_token_expires():
    token = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    assert not verify_session_token(token, api_key="secret", now=1000.0 + 601)


def test_tampered_token_rejected():
    token = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    tampered_variants = [
        token + "x",
        token[:-1],
        "v2.1000000000.9999999999.abc." + token.split(".")[-1],
        token.replace("v2.", "v1.", 1),
        "",
        "garbage",
    ]
    for variant in tampered_variants:
        assert not verify_session_token(variant, api_key="secret", now=1005.0)


def test_tokens_are_unique_per_issue():
    first = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    second = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    assert first != second


def test_nonpositive_ttl_rejected():
    with pytest.raises(ValueError):
        create_session_token(api_key="secret", ttl_minutes=0)


def test_malformed_expiry_rejected():
    assert not verify_session_token("v1.not-a-number.abc.deadbeef", api_key="secret")
    assert not verify_session_token("v2.1.not-a-number.abc.deadbeef", api_key="secret")


def test_legacy_v1_tokens_rejected():
    # v1 tokens carry no issued-at and cannot be revoked; they force a re-login.
    assert not verify_session_token("v1.9999999999.abc.deadbeef", api_key="secret")


def test_not_before_revokes_older_tokens():
    token = create_session_token(api_key="secret", ttl_minutes=10, now=1000.0)
    assert verify_session_token(token, api_key="secret", now=1005.0, not_before_us=1_000_000_000)
    assert not verify_session_token(
        token, api_key="secret", now=1005.0, not_before_us=1_000_000_001
    )


def test_revoke_sessions_writes_not_before_file(tmp_path):
    assert read_not_before_us(tmp_path) is None
    cutoff = revoke_sessions(tmp_path, now=1234.5)
    assert cutoff == 1_234_500_001
    assert read_not_before_us(tmp_path) == 1_234_500_001
    (tmp_path / "web_session_not_before").write_text("garbage")
    assert read_not_before_us(tmp_path) is None


class _FakeSettings:
    def __init__(self, data_dir, api_key="secret", web_session_secret=""):
        self.data_dir = data_dir
        self.api_key = api_key
        self.web_session_secret = web_session_secret


def test_dedicated_session_secret_takes_precedence(tmp_path):
    plain = _FakeSettings(tmp_path)
    dedicated = _FakeSettings(tmp_path, web_session_secret="separate-session-secret")
    assert signing_material(plain) == "secret"
    assert signing_material(dedicated) == "separate-session-secret"
    token = create_session_token(api_key=signing_material(dedicated), ttl_minutes=10)
    assert session_is_valid(token, dedicated)
    assert not session_is_valid(token, plain)


def test_session_is_valid_honours_revocation(tmp_path):
    settings = _FakeSettings(tmp_path)
    token = create_session_token(api_key="secret", ttl_minutes=10)
    assert session_is_valid(token, settings)
    revoke_sessions(tmp_path)
    assert not session_is_valid(token, settings)
    assert not session_is_valid("", settings)
