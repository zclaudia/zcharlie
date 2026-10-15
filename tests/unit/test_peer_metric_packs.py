from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from investment_agent.industry_peers.artifacts import (
    MetricMappingEntry,
    PeerBatchMapping,
    TargetMapping,
    body_sha256,
    read_batch_mapping,
    write_batch_mapping,
)
from investment_agent.industry_peers.calculations import (
    BatchCompany,
    BatchField,
    FrozenBatch,
    _peer_details,
    build_peer_metric_record,
    compute_peer_metric,
)
from investment_agent.industry_peers.metric_packs import (
    BAIJIU_PACK,
    BANK_PACK,
    metric_keys_for_pack,
)
from investment_agent.investor.common import ObjectRef

COHORT = ObjectRef(kind="peer_cohort_snapshot", id=UUID("00000000-0000-0000-0000-000000000101"), revision=1)


def test_bank_direct_ratios_calculate_without_equity_applicability():
    companies = []
    for company_id in range(1, 7):
        fields = {
            name: BatchField(value=Decimal(company_id) / Decimal(100), source_semantic=name)
            for name in ("net_interest_margin", "npl_ratio", "provision_coverage")
        }
        fields["roe_weighted_annual"] = BatchField(value=Decimal("0.1"), source_semantic="roe_weighted_annual")
        fields["equity_parent"] = BatchField.missing("bank adapter does not provide parent equity")
        companies.append(BatchCompany(company_id, company_id, f"{company_id:06}.SH", f"C{company_id}", True, date(2025, 12, 31), fields))
    batch = FrozenBatch(1, date(2025, 12, 31), COHORT, "a" * 64, tuple(companies), metric_pack=BANK_PACK)

    outcome = compute_peer_metric(batch, "net_interest_margin")
    assert outcome.peer_median == Decimal("0.04")
    assert outcome.valid_peer_count == 5
    built = build_peer_metric_record(batch, "net_interest_margin", evidence_refs={}, as_of=datetime(2026, 1, 1, tzinfo=UTC), require_fact_refs=False)
    assert built.record.details["metric_pack"] == BANK_PACK
    for unsupported in ("future_annual_v2", {"invalid": True}):
        unreadable = built.record.model_copy(
            update={"details": {**built.record.details, "metric_pack": unsupported}}
        )
        assert _peer_details(unreadable) is None
    with pytest.raises(ValueError, match="not in"):
        compute_peer_metric(batch, "contract_revenue")


def test_bank_roe_requires_parent_equity_and_unknown_packs_fail_closed():
    company = BatchCompany(
        1, 1, "000001.SZ", "Bank", True, date(2025, 12, 31),
        {"roe_weighted_annual": BatchField(value=Decimal("0.1")), "equity_parent": BatchField.missing("unavailable")},
    )
    batch = FrozenBatch(1, date(2025, 12, 31), COHORT, "b" * 64, (company,), metric_pack=BANK_PACK)
    roe = compute_peer_metric(batch, "roe")
    assert "no usable" in roe.reason
    with pytest.raises(ValueError, match="unknown peer metric pack"):
        metric_keys_for_pack("invented_pack")


def test_old_mapping_json_without_metric_pack_keeps_legacy_hash(tmp_path):
    keys = metric_keys_for_pack(BAIJIU_PACK)
    target = TargetMapping(
        company_id=1, listing_id=1, ticker="000001.SZ", name="A", report_period=date(2025, 12, 31),
        metric_calculation_refs=tuple(MetricMappingEntry(metric_key=k, state="gap", gap_reason="missing") for k in keys),
    )
    mapping = PeerBatchMapping.create(
        batch_run_id="legacy", cohort_ref=COHORT, cohort_content_hash="c" * 64,
        report_period=date(2025, 12, 31), provider="offline", adapter_version="v1", generation=1,
        status="partial", submitted_at="2026-01-01T00:00:00Z", targets=(target,),
    )
    new_json = mapping.model_dump(mode="json", by_alias=True)
    assert new_json["metric_pack"] == BAIJIU_PACK
    assert new_json["targets"][0]["metric_pack"] == BAIJIU_PACK
    old_json = new_json
    old_json.pop("metric_pack")
    for item in old_json["targets"]:
        item.pop("metric_pack")
    old_json["content_hash"] = ""
    body = {k: v for k, v in old_json.items() if k != "content_hash"}
    old_json["content_hash"] = body_sha256(body)
    restored = PeerBatchMapping.from_stored(old_json)
    assert restored.metric_pack == BAIJIU_PACK
    assert body_sha256(restored.body()) == restored.content_hash
    write_batch_mapping(tmp_path, restored)
    roundtrip = read_batch_mapping(tmp_path / "research-runs" / "legacy" / "peer-batch-mapping.json")
    assert roundtrip.content_hash == restored.content_hash
    assert "metric_pack" not in roundtrip.body()
    assert "metric_pack" not in roundtrip.body()["targets"][0]


def test_bank_bindings_reject_white_wine_reference_sets():
    from datetime import datetime

    from investment_agent.industry_peers.contracts import PeerContextBinding, PeerMetricBinding

    common = {
        "target": "000001.SZ",
        "cohort_ref": COHORT,
        "batch_run_id": "bank-batch",
        "report_period": date(2025, 12, 31),
        "cutoff": datetime(2026, 1, 1, tzinfo=UTC),
    }
    baijiu_refs = tuple(PeerMetricBinding(metric_key=key, state="gap", gap_reason="missing") for key in metric_keys_for_pack(BAIJIU_PACK))
    with pytest.raises(ValueError, match="fixed metric keys"):
        PeerContextBinding(**common, metric_pack=BANK_PACK, metric_calculation_refs=baijiu_refs)

    bank_refs = tuple(PeerMetricBinding(metric_key=key, state="gap", gap_reason="missing") for key in metric_keys_for_pack(BANK_PACK))
    binding = PeerContextBinding(**common, metric_pack=BANK_PACK, metric_calculation_refs=bank_refs)
    assert binding.metric_pack == BANK_PACK
