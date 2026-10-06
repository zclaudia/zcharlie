"""Shenwan 2021 -> industry group mapping with CSRC fallback (P1-B3, D7)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.industry_groups import (
    FLAG_INDUSTRY_FALLBACK,
    OTHER_GROUP,
    default_industry_groups,
    parse_industry_groups,
    usable_sw_code,
)
from investment_agent.factors.view import SnapshotView, load_snapshot_view
from investment_agent.providers.akshare_shenwan import parse_history
from investment_agent.providers.shenwan_directory import (
    code_level,
    default_shenwan_directory,
    level_codes,
)
from investment_agent.snapshots.shenwan import apply_sw2021_history
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._shenwan_fake import CAPTURED_AT, fake_shenwan_client, history_table

CSI300_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "factors" / "csi300_industry_2026-10-05.json"
)


def _csi300_current_codes() -> dict[str, str]:
    members = json.loads(CSI300_FIXTURE.read_text(encoding="utf-8"))["members"]
    history = parse_history(history_table(), captured_at=CAPTURED_AT)
    codes = {}
    for member in members:
        ticker = f"{member['code'][3:]}.{member['code'][:2].upper()}"
        codes[ticker] = history.changes[ticker][-1].code
    assert len(codes) == 300
    return codes


def test_every_csi300_shenwan_level1_and_override_is_mapped():
    mapping = default_industry_groups()
    assert mapping.shenwan is not None
    directory = default_shenwan_directory()
    codes = _csi300_current_codes()
    level1 = {level_codes(code)[0] for code in codes.values()}
    unmapped = sorted(code for code in level1 if code not in mapping.shenwan.level1)
    assert not unmapped, f"Shenwan level-1 industries without a group: {unmapped}"
    assert len(level1) == 26
    # every override names a real 2021 code, and every CSI 300 member resolves via Shenwan
    for code in mapping.shenwan.overrides:
        assert directory.get(code) is not None, code
    for ticker, code in codes.items():
        match = mapping.resolve_classification(
            sw_code=usable_sw_code(code, date(2021, 7, 30), None, directory),
            csrc_code=None,
        )
        assert match.matched_by in {"sw_l1", "sw_l2", "sw_l3"}, (ticker, code)
        assert not match.fallback


def test_every_2021_level1_is_mapped_and_versioned():
    mapping = default_industry_groups()
    assert mapping.shenwan is not None
    directory = default_shenwan_directory()
    assert set(mapping.shenwan.level1) == set(directory.codes(1))
    assert set(mapping.shenwan.level1.values()) | set(mapping.shenwan.overrides.values()) <= set(
        mapping.groups
    )
    assert mapping.version != "2026-10-06.1"  # bumped with the Shenwan section
    assert all(code_level(code) in {2, 3} for code in mapping.shenwan.overrides)


@pytest.mark.parametrize(
    ("ticker", "group", "matched_by"),
    [
        ("600519.SH", "consumer", "sw_l1"),  # 白酒Ⅲ
        ("601398.SH", "banking", "sw_l1"),  # 工商银行
        ("601318.SH", "insurance", "sw_l2"),  # 中国平安: 非银金融 > 保险Ⅱ
        ("600030.SH", "broker", "sw_l1"),  # 中信证券: 非银金融 default
        ("601012.SH", "growth_manufacturing", "sw_l1"),  # 隆基绿能: 电力设备 > 光伏设备
        ("002202.SZ", "growth_manufacturing", "sw_l1"),  # 金风科技: 电力设备 > 风电设备
        ("600900.SH", "utility", "sw_l1"),  # 长江电力
        ("600941.SH", "utility", "sw_l2"),  # 中国移动: 通信 > 通信服务
        ("000063.SZ", "growth_manufacturing", "sw_l1"),  # 中兴通讯: 通信 > 通信设备
        ("002415.SZ", "growth_manufacturing", "sw_l2"),  # 海康威视: 计算机 > 计算机设备
        ("688111.SH", "venture", "sw_l1"),  # 金山办公: 计算机 > 软件开发
        ("002558.SZ", "venture", "sw_l2"),  # 巨人网络: 传媒 > 游戏Ⅱ
        ("002027.SZ", "consumer", "sw_l1"),  # 分众传媒: 传媒 > 广告营销
        ("601006.SH", "utility", "sw_l2"),  # 大秦铁路: 交通运输 > 铁路公路
        ("601919.SH", "cyclical", "sw_l1"),  # 中远海控: 交通运输 > 航运港口
        ("002352.SZ", "consumer", "sw_l3"),  # 顺丰控股: 交通运输 > 物流 > 快递
        ("001979.SZ", "real_estate", "sw_l1"),  # 招商蛇口
        ("603259.SH", "healthcare", "sw_l1"),  # 药明康德
        ("600276.SH", "healthcare", "sw_l1"),  # 恒瑞医药
        ("600309.SH", "cyclical", "sw_l1"),  # 万华化学
        ("000333.SZ", "consumer", "sw_l1"),  # 美的集团: 家用电器
        ("002050.SZ", "growth_manufacturing", "sw_l2"),  # 三花智控: 家电零部件Ⅱ
        ("300015.SZ", "healthcare", "sw_l1"),  # 爱尔眼科: 医药生物 > 医疗服务
    ],
)
def test_representative_members(ticker: str, group: str, matched_by: str):
    codes = _csi300_current_codes()
    match = default_industry_groups().resolve_classification(
        sw_code=codes[ticker], csrc_code=None
    )
    assert (match.group, match.matched_by) == (group, matched_by), codes[ticker]
    assert match.system == "sw2021"


def test_csrc_fallback_and_neither():
    mapping = default_industry_groups()
    fallback = mapping.resolve_classification(sw_code=None, csrc_code="J66")
    assert (fallback.group, fallback.matched_by, fallback.fallback) == ("banking", "code", True)
    assert fallback.system == "csrc"
    neither = mapping.resolve_classification(sw_code=None, csrc_code=None)
    assert (neither.group, neither.matched_by, neither.fallback) == (OTHER_GROUP, "fallback", False)
    # an unknown Shenwan level 1 also falls back to CSRC
    unknown = mapping.resolve_classification(sw_code="990101", csrc_code="C15")
    assert (unknown.group, unknown.fallback) == ("consumer", True)


def test_usable_sw_code_rejects_legacy_intervals_and_unknown_codes():
    directory = default_shenwan_directory()
    assert usable_sw_code("340501", date(2021, 7, 30), None, directory) == "340501"
    # 600519 before 2021-07-30: SW2014 code 340301, interval ended on the cutover
    assert usable_sw_code("340301", date(2001, 7, 31), date(2021, 7, 30), directory) is None
    assert usable_sw_code("999999", date(2021, 7, 30), None, directory) is None
    assert usable_sw_code(None, None, None, directory) is None
    # an open interval the sync could not name (stored name == code) is not usable
    assert usable_sw_code("340301", date(2001, 7, 31), None, directory, "340301") is None


def test_parser_validates_the_shenwan_section():
    base = {"version": "1", "groups": {"other": "x", "banking": "y"}}
    ok = parse_industry_groups(
        {**base, "shenwan": {"level1": {"480000": "banking"}, "overrides": {"480100": "other"}}}
    )
    assert ok.shenwan is not None and ok.shenwan.resolve("480101").matched_by == "sw_l2"
    with pytest.raises(ValueError, match="unknown group"):
        parse_industry_groups({**base, "shenwan": {"level1": {"480000": "bank"}}})
    with pytest.raises(ValueError, match="invalid Shenwan level1"):
        parse_industry_groups({**base, "shenwan": {"level1": {"480100": "banking"}}})
    with pytest.raises(ValueError, match="without a level-1"):
        parse_industry_groups(
            {**base, "shenwan": {"level1": {"480000": "banking"}, "overrides": {"490100": "other"}}}
        )
    with pytest.raises(ValueError, match="needs level1"):
        parse_industry_groups({**base, "shenwan": {"level1": {}}})


def test_view_flags_csrc_fallback_on_every_factor():
    view = SnapshotView.build(ticker="600519.SH", as_of=date(2026, 9, 30), industry_code="C15")
    assert view.industry_group == "consumer" and view.industry_fallback
    value = DEFAULT_REGISTRY.get("roe").compute(view)
    assert FLAG_INDUSTRY_FALLBACK in value.flags
    assert value.inputs_ref["industry"] == {
        "system": "csrc",
        "code": "C15",
        "group": "consumer",
        "matched_by": "code",
        "fallback": True,
    }

    shenwan = SnapshotView.build(
        ticker="601318.SH", as_of=date(2026, 9, 30), industry_code="J68", sw_code="490201"
    )
    assert shenwan.industry_group == "insurance" and not shenwan.industry_fallback
    value = DEFAULT_REGISTRY.get("roe").compute(shenwan)
    assert FLAG_INDUSTRY_FALLBACK not in value.flags
    assert value.inputs_ref["industry"]["matched_by"] == "sw_l2"


async def test_loaded_view_uses_shenwan_as_of_and_falls_back_before_2021(db_session):
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("600519.SH")
        scope = {"600519.SH": listing.id}
    history = await fake_shenwan_client().fetch_industry_history()
    await apply_sw2021_history(history, scope=scope, as_of=date(2026, 10, 6), lease=None)

    async with session_scope() as session:
        after = await load_snapshot_view(session, "600519.SH", date(2026, 9, 30))
        before = await load_snapshot_view(session, "600519.SH", date(2021, 6, 30))
    assert (after.industry_group, after.sw_code, after.industry_fallback) == (
        "consumer",
        "340501",
        False,
    )
    assert after.industry_match is not None and after.industry_match.matched_by == "sw_l1"
    # legacy SW2014 interval, no CSRC row stored -> other / fallback (no Shenwan decision)
    assert before.sw_code is None
    assert before.industry_match is not None and before.industry_match.matched_by == "fallback"
    assert before.industry_group == OTHER_GROUP
