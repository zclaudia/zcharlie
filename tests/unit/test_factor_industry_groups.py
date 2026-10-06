"""CSRC industry -> industry group mapping (P1-B3)."""

import json
from pathlib import Path

import pytest

from investment_agent.factors.industry_groups import (
    FINANCIAL_GROUPS,
    OTHER_GROUP,
    default_industry_groups,
    industry_group_for,
    parse_industry_groups,
)
from investment_agent.providers.baostock_reference import parse_csrc_industry

CSI300_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "factors" / "csi300_industry_2026-10-05.json"
)


def _csi300_members() -> list[dict[str, str]]:
    payload = json.loads(CSI300_FIXTURE.read_text(encoding="utf-8"))
    members = payload["members"]
    assert len(members) == 300
    return members


def test_every_csi300_csrc_code_is_mapped_explicitly():
    mapping = default_industry_groups()
    unmapped: dict[str, list[str]] = {}
    codes: set[str] = set()
    for member in _csi300_members():
        code, name = parse_csrc_industry(member["industry"])
        assert code is not None, member
        codes.add(code)
        match = mapping.resolve(code, name)
        if match.matched_by != "code":
            unmapped.setdefault(code, []).append(member["name"])
    assert not unmapped, f"CSRC codes without an explicit industry group: {unmapped}"
    assert len(codes) == 41


def test_config_is_versioned_and_groups_are_known():
    mapping = default_industry_groups()
    assert mapping.version
    assert mapping.system == "csrc"
    assert set(mapping.groups) >= FINANCIAL_GROUPS
    assert set(mapping.codes.values()) <= set(mapping.groups)
    assert OTHER_GROUP in mapping.groups


@pytest.mark.parametrize(
    ("code", "name", "group"),
    [
        ("J66", "货币金融服务", "banking"),
        ("J67", "资本市场服务", "broker"),
        ("J68", "保险业", "insurance"),
        ("C15", "酒、饮料和精制茶制造业", "consumer"),
        ("C38", "电气机械和器材制造业", "growth_manufacturing"),
        ("D44", "电力、热力生产和供应业", "utility"),
        ("B06", "煤炭开采和洗选业", "cyclical"),
        ("I65", "软件和信息技术服务业", "venture"),
        ("C27", "医药制造业", "healthcare"),
        ("K70", "房地产业", "real_estate"),
        ("J69", "其他金融业", "other"),
    ],
)
def test_known_codes(code: str, name: str, group: str):
    assert industry_group_for(code, name) == group
    assert default_industry_groups().resolve(code, name).matched_by == "code"


def test_code_embedded_in_name_and_lowercase_code():
    assert industry_group_for(None, "J66货币金融服务") == "banking"
    assert industry_group_for("j66", None) == "banking"


@pytest.mark.parametrize(
    ("label", "group"),
    [
        ("金融、保险业-银行业", "banking"),
        ("金融、保险业-保险业", "insurance"),
        ("金融、保险业-证券、期货业", "broker"),
        ("制造业-食品、饮料-饮料制造业", "consumer"),
        ("电力、煤气及水的生产和供应业", "utility"),
        ("制造业-医药、生物制品", "healthcare"),
        ("采掘业-煤炭采选业", "cyclical"),
        ("制造业-交通运输设备制造业", "growth_manufacturing"),
        ("房地产业", "real_estate"),
        ("综合类", "other"),
    ],
)
def test_pre_2012_codeless_labels_match_by_name(label: str, group: str):
    match = default_industry_groups().resolve(None, label)
    assert match.group == group
    assert match.matched_by == "legacy_name"


def test_unknown_code_falls_back_to_other_visibly():
    match = default_industry_groups().resolve("Z99", "未知行业")
    assert match.group == OTHER_GROUP
    assert match.matched_by == "fallback"
    assert industry_group_for(None, None) == OTHER_GROUP


def test_parser_rejects_bad_configs():
    base = {"version": "1", "groups": {"other": "x", "banking": "y"}}
    with pytest.raises(ValueError, match="version"):
        parse_industry_groups({**base, "version": ""})
    with pytest.raises(ValueError, match="unknown group"):
        parse_industry_groups({**base, "codes": {"J66": "bank"}})
    with pytest.raises(ValueError, match="invalid CSRC"):
        parse_industry_groups({**base, "codes": {"J6": "banking"}})
    with pytest.raises(ValueError, match="including 'other'"):
        parse_industry_groups({"version": "1", "groups": {"banking": "y"}})
    with pytest.raises(ValueError, match="legacy"):
        parse_industry_groups({**base, "legacy_name_rules": [{"group": "banking"}]})
