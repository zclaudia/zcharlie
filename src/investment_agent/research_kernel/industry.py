"""Industry inputs of the research kernel: industry groups, legacy keywords as fallback.

The kernel's strategy classification (:func:`scoring.classify_stock`) and
company profile (:func:`company_profile.classify_company_profile`) read an
:class:`IndustryBasis` instead of matching keywords themselves (P1-B4).

Resolution of one canonical stock item (:func:`resolve_industry_basis`):

1. **User text overrides.** A non-empty ``industry`` entered by the user
   (watchlist / command line; ``field_sources.industry.provider == "manual"``
   or no provenance, as in every iFinD bundle) decides:

   a. text equal to an industry group id of ``config/industry_groups.yaml``
      (``banking``, ``utility`` ...) -> that group (``matched_by="group_id"``);
   b. otherwise the **legacy keyword mapping** (the pre-P1 keyword tables,
      kept verbatim for backward compatibility; ``matched_by="legacy_keywords"``).

   Exact Shenwan names (``"电力设备"``, ``"银行"``) are *not* mapped to their
   group here: doing so changes golden-corpus classifications (decision D3,
   see ``docs/product/observation-snapshot-mode.md``) and is held for a
   scoring version bump.
2. **No user text** -> ``industry_group`` of the item (Shenwan 2021 first,
   CSRC fallback, resolved by the factor layer on ``as_of``;
   ``matched_by="industry_group"``). Snapshot-mode bundles carry it; their
   ``industry`` is then the Shenwan / CSRC name for display only.
3. Data-derived text without a group -> legacy keywords; nothing -> no
   industry traits (``general`` profile).

An industry group implies a fixed set of kernel traits (:data:`GROUP_TRAITS`)
equivalent to what the keyword tables expressed for that kind of business.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from investment_agent.factors.industry_groups import default_industry_groups

Trait = Literal["dividend", "growth", "cycle", "venture"]
Sector = Literal["banking", "insurance", "manufacturing_growth", "public_utility", "general"]
MatchedBy = Literal["group_id", "legacy_keywords", "industry_group", "none"]

# --------------------------------------------------------------------------- #
# Legacy keyword tables (verbatim from scoring v1.3; backward compatibility).
# --------------------------------------------------------------------------- #

CYCLE_INDUSTRY_KEYWORDS = ("有色", "煤炭", "石油", "化工", "钢铁", "航运", "能源", "矿")
VENTURE_INDUSTRY_KEYWORDS = ("半导体", "AI", "机器人", "创新药", "计算机", "电子")
GROWTH_INDUSTRY_KEYWORDS = ("电力设备", "新能源", "自动化", "汽车零部件", "医药", "高端制造")
DIVIDEND_INDUSTRY_KEYWORDS = ("公用事业", "银行", "运营商", "电力", "高速")

INSURANCE_KEYWORDS = ("保险", "平安", "太保", "人寿", "新华")
BANKING_KEYWORDS = ("银行",)
PUBLIC_UTILITY_KEYWORDS = ("公用事业", "电力", "水电", "核电", "高速", "运营商")
MANUFACTURING_GROWTH_KEYWORDS = (
    "汽车零部件",
    "电力设备",
    "新能源",
    "自动化",
    "高端制造",
    "智能制造",
)

_TRAIT_KEYWORDS: tuple[tuple[Trait, tuple[str, ...]], ...] = (
    ("dividend", DIVIDEND_INDUSTRY_KEYWORDS),
    ("growth", GROWTH_INDUSTRY_KEYWORDS),
    ("cycle", CYCLE_INDUSTRY_KEYWORDS),
    ("venture", VENTURE_INDUSTRY_KEYWORDS),
)
# Profile precedence of the legacy tables (first hit wins).
_SECTOR_KEYWORDS: tuple[tuple[Sector, tuple[str, ...]], ...] = (
    ("banking", BANKING_KEYWORDS),
    ("insurance", INSURANCE_KEYWORDS),
    ("manufacturing_growth", MANUFACTURING_GROWTH_KEYWORDS),
    ("public_utility", PUBLIC_UTILITY_KEYWORDS),
)

# --------------------------------------------------------------------------- #
# Industry group -> kernel traits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GroupTraits:
    traits: frozenset[Trait]
    sector: Sector


GROUP_TRAITS: Mapping[str, GroupTraits] = {
    "banking": GroupTraits(frozenset({"dividend"}), "banking"),
    "insurance": GroupTraits(frozenset(), "insurance"),
    "broker": GroupTraits(frozenset(), "general"),
    "utility": GroupTraits(frozenset({"dividend"}), "public_utility"),
    "cyclical": GroupTraits(frozenset({"cycle"}), "general"),
    "growth_manufacturing": GroupTraits(frozenset({"growth"}), "manufacturing_growth"),
    "venture": GroupTraits(frozenset({"venture"}), "general"),
    "healthcare": GroupTraits(frozenset({"growth"}), "general"),
    "consumer": GroupTraits(frozenset(), "general"),
    "real_estate": GroupTraits(frozenset(), "general"),
    "other": GroupTraits(frozenset(), "general"),
}


@dataclass(frozen=True)
class IndustryBasis:
    """What the kernel's industry-dependent rules read."""

    traits: frozenset[Trait]
    sector: Sector
    matched_by: MatchedBy
    group: str | None = None

    def has(self, trait: Trait) -> bool:
        return trait in self.traits


def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = text.lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def legacy_keyword_basis(stock: Mapping[str, Any]) -> IndustryBasis:
    """The pre-P1 keyword classification (traits on ``industry``; the profile
    sector on symbol + name + industry + market, as before)."""

    industry = str(stock.get("industry", "") or "")
    traits = frozenset(trait for trait, words in _TRAIT_KEYWORDS if _has_any(industry, words))
    profile_text = " ".join(
        str(stock.get(key) or "") for key in ("symbol", "name", "industry", "market")
    )
    sector: Sector = next(
        (sector for sector, words in _SECTOR_KEYWORDS if _has_any(profile_text, words)),
        "general",
    )
    return IndustryBasis(traits, sector, "legacy_keywords")


def group_basis(group: str, matched_by: MatchedBy) -> IndustryBasis:
    mapped = GROUP_TRAITS.get(group, GROUP_TRAITS["other"])
    return IndustryBasis(mapped.traits, mapped.sector, matched_by, group)


def _is_user_text(stock: Mapping[str, Any]) -> bool:
    """``industry`` was entered by the user (provenance ``manual``, or no provenance)."""

    sources = stock.get("field_sources")
    source = sources.get("industry") if isinstance(sources, Mapping) else None
    if not isinstance(source, Mapping):
        return True
    return source.get("provider") == "manual"


def resolve_industry_basis(stock: Mapping[str, Any]) -> IndustryBasis:
    """User text (group id, else legacy keywords) > item ``industry_group`` > none."""

    text = str(stock.get("industry") or "").strip()
    group = stock.get("industry_group")
    has_group = isinstance(group, str) and bool(group)
    if text and (_is_user_text(stock) or not has_group):
        if text in default_industry_groups().groups:
            return group_basis(text, "group_id")
        return legacy_keyword_basis(stock)
    if has_group:
        assert isinstance(group, str)
        return group_basis(group, "industry_group")
    return IndustryBasis(frozenset(), "general", "none")
