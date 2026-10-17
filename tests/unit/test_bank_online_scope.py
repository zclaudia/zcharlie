"""An online source is an explicit immutable bank-roster binding."""

import pytest

from investment_agent.industry_peers.metric_packs import (
    BAIJIU_PACK,
    BANK_PACK,
    cohort_read_scope,
)


def test_old_rosters_keep_their_offline_scope() -> None:
    assert cohort_read_scope({}) == ("eastmoney", "offline-run02-v1")
    assert cohort_read_scope({"metric_pack": BANK_PACK}) == ("eastmoney", "offline-run02-v1")


def test_bank_online_scope_requires_an_explicit_binding() -> None:
    assert cohort_read_scope(
        {
            "metric_pack": BANK_PACK,
            "read_scope": {"provider": "eastmoney", "adapter_version": "bank-annual-v1"},
        }
    ) == ("eastmoney", "bank-annual-v1")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "metric_pack": BAIJIU_PACK,
            "read_scope": {"provider": "eastmoney", "adapter_version": "bank-annual-v1"},
        },
        {
            "metric_pack": BANK_PACK,
            "read_scope": {"provider": "rival", "adapter_version": "bank-annual-v1"},
        },
        {
            "metric_pack": BANK_PACK,
            "read_scope": {"provider": "eastmoney", "adapter_version": "future-v2"},
        },
        {"metric_pack": BANK_PACK, "read_scope": {"provider": "eastmoney"}},
        {"metric_pack": BANK_PACK, "read_scope": []},
    ],
)
def test_unregistered_or_cross_pack_source_switch_is_rejected(payload: dict) -> None:
    with pytest.raises(ValueError, match="read_scope"):
        cohort_read_scope(payload)
