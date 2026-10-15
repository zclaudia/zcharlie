"""Independent root regression cases discovered during acceptance review."""

import pytest

from investment_agent.industry_peers.bank_proofs import checked_bank_locator
from tests.unit.test_bank_proofs import _proof


@pytest.mark.parametrize(
    ("metric", "label", "bank_value", "group_value", "parsed"),
    [
        ("roe_weighted_annual", "加权平均净资产收益率", "9.45", "8.88", "0.0945"),
        ("net_interest_margin", "净利息收益率", "1.28", "1.87", "0.0128"),
    ],
)
def test_declared_group_column_cannot_override_actual_bank_first_columns(
    metric, label, bank_value, group_value, parsed,
):
    text = (
        "主要财务指标 本行 2025年度 2024年度 本集团 2025年度 2024年度 "
        f"{label} {bank_value}% 10.20% {group_value}% 8.80%"
    )
    original, proof = _proof(
        metric, text, current_column="本集团 2025年度", header=label,
        raw_value=bank_value, parsed_value=parsed,
    )
    with pytest.raises(ValueError):
        checked_bank_locator(original, metric, proof)


def test_unrelated_thousand_unit_does_not_change_a_million_statement():
    text = (
        "合并资产负债表 单位：百万元 本集团 2025年12月31日 2024年12月31日 "
        "归属于母公司股东权益合计 337,961,238 300,000,000 附注：其他资料单位千元"
    )
    original, proof = _proof("equity_parent", text)
    with pytest.raises(ValueError):
        checked_bank_locator(original, "equity_parent", proof)


def test_parent_label_cannot_hide_a_common_share_only_qualifier():
    text = (
        "合并资产负债表 单位：千元 本集团 2025年12月31日 2024年12月31日 "
        "归属于母公司股东的权益（普通股） 337,961,238 300,000,000"
    )
    original, proof = _proof("equity_parent", text, header="归属于母公司股东的权益")
    with pytest.raises(ValueError):
        checked_bank_locator(original, "equity_parent", proof)


@pytest.mark.parametrize("label", [
    "加权平均净资产收益率（扣除非经常性损益）",
    "扣除非经常性损益后加权平均净资产收益率",
])
def test_weighted_roe_label_cannot_hide_a_nonrecurring_deduction_qualifier(label):
    text = (
        "主要财务指标 集团口径 2025年度 2024年度 "
        f"{label} 9.45% 10.20%"
    )
    original, proof = _proof("roe_weighted_annual", text)
    with pytest.raises(ValueError):
        checked_bank_locator(original, "roe_weighted_annual", proof)
