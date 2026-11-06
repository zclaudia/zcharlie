"""industry_groups config document (WP3): parse guards, put round-trip, the
shared coverage check, live apply through the loader, and the PIT invariant of
config-docs-plan §6 (a mapping change never rewrites stored rows)."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest

from investment_agent.config_docs import loader, registry
from investment_agent.config_docs.store import (
    ConfigDocConflictError,
    ConfigDocStore,
    ConfigDocValidationError,
)
from investment_agent.factors.industry_groups import (
    CoverageMember,
    check_industry_groups_coverage,
    default_industry_groups,
    industry_group_for,
    parse_industry_groups,
)
from investment_agent.factors.snapshot_model import SnapshotView
from investment_agent.providers.shenwan_directory import (
    ShenwanIndustry,
    build_directory,
    default_shenwan_directory,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.rules import RuleTemplateRecord, ScreenResultRecord

KIND = "industry_groups"


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader cache is process-global: keep it (and its broken flag) clean."""

    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


def _baseline_document() -> dict[str, Any]:
    return registry.baseline_document(registry.spec_for(KIND))


# --- parse guards ----------------------------------------------------------------


def test_parse_rejects_bad_maps() -> None:
    base = _baseline_document()
    broken = copy.deepcopy(base)
    broken["codes"]["J6X"] = "banking"  # not 门类字母 + 两位数字
    with pytest.raises(ValueError, match="invalid CSRC"):
        parse_industry_groups(broken)

    unknown_group = copy.deepcopy(base)
    unknown_group["codes"]["J66"] = "nope"
    with pytest.raises(ValueError, match="unknown group"):
        parse_industry_groups(unknown_group)

    missing_other = copy.deepcopy(base)
    del missing_other["groups"]["other"]
    with pytest.raises(ValueError, match="including 'other'"):
        parse_industry_groups(missing_other)


def test_baseline_document_parses_and_is_versioned() -> None:
    mapping = parse_industry_groups(_baseline_document())
    assert mapping.version and mapping.system == "csrc"
    assert mapping.shenwan is not None and mapping.shenwan.level1


# --- put round-trip ----------------------------------------------------------------


async def test_put_round_trip_with_history_and_conflict(db_session) -> None:
    store = ConfigDocStore(db_session)
    document = copy.deepcopy(_baseline_document())
    document["version"] = "test-ig-2"
    assert await store.put(KIND, document, actor="ui", note="调整 J66") == 1
    value, row = await store.get(KIND)
    assert value == document and row is not None
    (change,) = await store.history(KIND)
    assert (change.actor, change.note) == ("ui", "调整 J66")
    assert change.old_value_json is None and change.new_value_json["version"] == "test-ig-2"

    stale = copy.deepcopy(document)
    stale["version"] = "test-ig-3"
    with pytest.raises(ConfigDocConflictError):
        await store.put(KIND, stale, actor="ui", note="stale", expected_revision=0)
    assert await store.put(KIND, stale, actor="ui", note="fresh", expected_revision=1) == 2


async def test_put_rejects_an_invalid_document_and_writes_nothing(db_session) -> None:
    store = ConfigDocStore(db_session)
    broken = copy.deepcopy(_baseline_document())
    broken["codes"]["J66"] = "ghost"
    with pytest.raises(ConfigDocValidationError, match="unknown group"):
        await store.put(KIND, broken, actor="ui", note="bad")
    value, row = await store.get(KIND)
    assert row is None and value == _baseline_document()


# --- the shared coverage check -----------------------------------------------------


def _directory() -> Any:
    return build_directory(
        [
            ShenwanIndustry(code="110000", name="农林牧渔", level=1),
            ShenwanIndustry(code="220000", name="采掘", level=1),
        ],
        version="test-dir",
    )


def _ok_document() -> dict[str, Any]:
    return {
        "version": "cov-1",
        "system": "csrc",
        "groups": {"other": "兜底", "farming": "农业", "mining": "采掘"},
        "codes": {"A01": "farming", "B06": "mining"},
        "legacy_name_rules": [],
        "shenwan": {"level1": {"110000": "farming", "220000": "mining"}, "overrides": {}},
    }


def test_coverage_ok_passes() -> None:
    coverage = check_industry_groups_coverage(
        parse_industry_groups(_ok_document()),
        directory=_directory(),
        members=[
            CoverageMember(ticker="600000.SH", csrc_code="A01", csrc_name="农业"),
            CoverageMember(ticker="000001.SZ", csrc_code="B06", csrc_name="采掘"),
        ],
    )
    assert coverage.ok
    assert coverage.unmapped_csrc == ()
    assert coverage.missing_level1_defaults == ()
    assert coverage.invalid_overrides == ()
    assert (coverage.member_count, coverage.csrc_code_count) == (2, 2)
    assert "覆盖完整" in coverage.summary


def test_coverage_reports_each_gap_kind() -> None:
    missing_doc = _ok_document()
    missing_doc["shenwan"] = {"level1": {"110000": "farming"}, "overrides": {}}
    coverage = check_industry_groups_coverage(
        parse_industry_groups(missing_doc),
        directory=_directory(),
        members=[
            # Z99 没有显式映射（也不是 legacy 命中）→ unmapped；两次出现合并为一个缺口。
            CoverageMember(ticker="600000.SH", csrc_code="Z99", csrc_name="未知行业"),
            CoverageMember(ticker="000001.SZ", csrc_code="Z99", csrc_name="未知行业"),
            # legacy_name 命中不算显式映射。
            CoverageMember(ticker="600002.SH", csrc_code=None, csrc_name="农林牧渔"),
        ],
    )
    assert not coverage.ok
    gap, legacy = coverage.unmapped_csrc
    assert gap.code == "Z99" and gap.tickers == ("000001.SZ", "600000.SH")
    assert legacy.code == "农林牧渔"
    [missing] = coverage.missing_level1_defaults
    assert missing.code == "220000" and missing.name == "采掘"
    assert coverage.invalid_overrides == ()
    assert "覆盖不完整" in coverage.summary

    # 覆写码的一级有默认、但码本身不在目录里 → invalid_overrides。
    invalid_doc = _ok_document()
    invalid_doc["shenwan"] = {
        "level1": {"110000": "farming", "220000": "mining"},
        "overrides": {"220101": "mining"},
    }
    coverage = check_industry_groups_coverage(
        parse_industry_groups(invalid_doc), directory=_directory(), members=[]
    )
    assert coverage.missing_level1_defaults == ()
    [invalid] = coverage.invalid_overrides
    assert invalid.code == "220101"
    assert not coverage.ok


def test_coverage_without_a_shenwan_section_reports_every_level1() -> None:
    document = _ok_document()
    del document["shenwan"]
    coverage = check_industry_groups_coverage(
        parse_industry_groups(document), directory=_directory(), members=[]
    )
    assert [gap.code for gap in coverage.missing_level1_defaults] == ["110000", "220000"]


def test_the_real_baseline_passes_with_the_fixture_universe() -> None:
    """端到端口径：真实基线 + 测试固定成分快照（endpoint 用 DB 宇宙走同一条路）。"""

    from tests.unit.test_factor_industry_groups import _csi300_coverage_members

    coverage = check_industry_groups_coverage(
        default_industry_groups(),
        directory=default_shenwan_directory(),
        members=_csi300_coverage_members(),
    )
    assert coverage.ok and coverage.csrc_code_count == 41


# --- live apply through the loader (§4.4) -------------------------------------------


async def test_live_apply_via_loader_after_put_and_refresh(db_session) -> None:
    assert industry_group_for("J66", "货币金融服务") == "banking"
    await loader.ensure_imported(db_session)
    assert default_industry_groups().group_for("J66", "货币金融服务") == "banking"

    document = copy.deepcopy(_baseline_document())
    document["version"] = "test-ig-live"
    document["codes"]["J66"] = "other"
    await ConfigDocStore(db_session).put(KIND, document, actor="ui", note="改 J66")

    # Same process: the write refreshed the cache (live, §3.1).
    assert default_industry_groups().group_for("J66", "货币金融服务") == "other"

    # Another process: the worker / scheduler refresh path sees it too.
    loader.invalidate()
    assert await loader.refresh_if_changed(db_session) is True
    assert default_industry_groups().group_for("J66", "货币金融服务") == "other"
    assert await loader.refresh_if_changed(db_session) is False


def test_cold_cache_falls_back_to_the_baseline_file() -> None:
    loader.invalidate()
    assert default_industry_groups().group_for("J66", "货币金融服务") == "banking"


# --- PIT invariant (§6): stored rows keep their group -------------------------------


async def test_mapping_change_does_not_rewrite_stored_rows(db_session) -> None:
    """改映射只影响未来计算：已存的 screen_result 行保持写入时的分组。"""

    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(db_session).ensure_a_share_listing("600519.SH")
    run = PipelineRun(
        kind="screen_run",
        triggered_by="test",
        started_at=datetime.now(UTC),
        status="success",
    )
    db_session.add(run)
    db_session.add(
        RuleTemplateRecord(
            id="tmpl_conglomerate",
            version=1,
            name="综合模板",
            definition={"applicable_groups": ["other"]},
            hash="pit-test",
            calibration_status="calibrated",
        )
    )
    await db_session.flush()

    # doc v1: J66 -> banking. The computation freezes the group into the row
    # exactly like rules.screen_job.load_members does.
    view = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2026, 9, 30),
        industry_code="J66",
        industry_name="货币金融服务",
    )
    assert view.industry_group == "banking"
    stored = ScreenResultRecord(
        screen_run_id=run.id,
        as_of=date(2026, 9, 30),
        template_id="tmpl_conglomerate",
        template_version=1,
        listing_id=listing.id,
        ticker="600519.SH",
        outcome="hit",
        hit=True,
        reasons=[],
        context={
            "provider": view.provider,
            "as_of": view.as_of.isoformat(),
            "industry_group": view.industry_group,
            "industry": view.industry_match.describe() if view.industry_match else None,
        },
    )
    db_session.add(stored)
    await db_session.flush()
    stored_id = stored.id
    db_session.expunge_all()

    # Edit the mapping through the config-doc store (live apply).
    document = copy.deepcopy(_baseline_document())
    document["version"] = "test-ig-pit"
    document["codes"]["J66"] = "other"
    await ConfigDocStore(db_session).put(KIND, document, actor="ui", note="J66 改指 other")

    # New computations use the new mapping…
    assert default_industry_groups().group_for("J66", "货币金融服务") == "other"
    fresh = SnapshotView.build(
        ticker="600519.SH",
        as_of=date(2026, 9, 30),
        industry_code="J66",
        industry_name="货币金融服务",
    )
    assert fresh.industry_group == "other"

    # …while the stored row keeps the group it was written with.
    row = await db_session.get(ScreenResultRecord, stored_id)
    assert row is not None
    assert row.context["industry_group"] == "banking"
    assert row.context["industry"]["group"] == "banking"
