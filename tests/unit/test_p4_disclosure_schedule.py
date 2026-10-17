"""Fixed-window scheduling does not turn missed slots into repeated polls."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationV1,
)
from investment_agent.supplemental_rules.disclosure_schedule import (
    DisclosureScheduleV1,
    select_disclosure_window,
)


def _grant(now, **overrides):
    return BackgroundDisclosureAuthorizationV1.model_validate({
        "contract_hash": "a" * 64, "listing_refs": [{"kind": "listing", "id": 1, "revision": 1}],
        "doc_type_allowlist": ["annual_report_full"], "history_start": now - timedelta(days=100),
        "history_end": now + timedelta(days=1), "expires_at": now + timedelta(days=7),
        "min_poll_interval_seconds": 21600, "min_http_interval_seconds": 5,
        "max_staleness_seconds": 86400, "per_poll_query_http": 4, "per_poll_fetch": 3,
        "per_poll_http": 8, "per_poll_response_bytes": 64 * 1024 * 1024,
        "grant_daily_http": 24, "grant_daily_response_bytes": 128 * 1024 * 1024,
        "storage_bytes_cap": 512 * 1024 * 1024, "authorization_id": uuid4(),
        "revision": 1, "created_at": now, "actor": "api", "status": "active",
        "authorization_hash": "b" * 64, **overrides,
    })


def test_backfill_and_recent_windows_are_bounded_and_overlap_is_explicit():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    grant = _grant(now)
    state = DisclosureScheduleV1(basis_hash="c" * 64,
        listing_ref=ObjectRef(kind="listing", id=1, revision=1), backfill_after=grant.history_start)
    assert select_disclosure_window(state, grant, now) == (
        "backfill", grant.history_start, grant.history_start + timedelta(days=31))
    recent = state.model_copy(update={"next_lane": "recent"})
    assert select_disclosure_window(recent, grant, now) == ("recent", now - timedelta(hours=72), now)
    narrowed = state.model_copy(update={"window_days": 1})
    assert select_disclosure_window(narrowed, grant, now)[2] == grant.history_start + timedelta(days=1)
    no_overlap = _grant(now, allow_overlap=False, history_end=now)
    assert select_disclosure_window(recent.model_copy(update={"recent_end": now}), no_overlap, now) is None


def test_deep_sweep_requires_grant_and_does_not_pass_history_or_current_time():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    grant = _grant(now, allow_deep_sweep=True, history_end=now - timedelta(hours=2))
    state = DisclosureScheduleV1(basis_hash="c" * 64,
        listing_ref=ObjectRef(kind="listing", id=1, revision=1), backfill_after=grant.history_end,
        next_lane="recent", deep_next_at=now - timedelta(days=1))
    assert select_disclosure_window(state, grant, now) == (
        "deep_sweep", grant.history_end - timedelta(days=31), grant.history_end)
    without = _grant(now, allow_deep_sweep=False)
    assert select_disclosure_window(state, without, now)[0] == "recent"
