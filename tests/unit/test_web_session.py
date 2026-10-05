"""Signed web session token unit tests."""


import pytest

from investment_agent.web.session import (
    SESSION_COOKIE_NAME,
    create_session_token,
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
        "v1.9999999999.abc." + token.split(".")[-1],
        token.replace("v1.", "v2.", 1),
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
