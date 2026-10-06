"""Industry -> industry group mapping (P1-B3, decision D7).

The mapping lives in ``config/industry_groups.yaml`` (versioned). Factor
applicability and rule-template applicability reference the groups defined
there; free-text industry keywords are not used by the factor layer.

**Shenwan 2021 first** (:meth:`IndustryGroupMap.resolve_classification`):
the ``sw2021`` classification valid on ``as_of`` decides - a level-3
override (``matched_by="sw_l3"``), else a level-2 override (``sw_l2``), else
the level-1 default (``sw_l1``). Only a code of the 2021 era that is listed
in the Shenwan directory counts (:func:`usable_sw_code`). Without one the
CSRC table below is the fallback and the match carries
``fallback=True`` (factor flag ``industry_fallback``); when CSRC has no
mapping either, the group is ``other`` with ``matched_by="fallback"``.

CSRC resolution order for one classification ``(code, name)``:

1. ``code`` (e.g. ``J66``) in ``codes``;
2. a code embedded at the start of ``name`` (``"J66货币金融服务"``);
3. the first ``legacy_name_rules`` substring hit on ``name`` (pre-2012
   code-less labels; the most specific "-" segment is tried first);
4. otherwise ``other`` with ``matched_by="fallback"`` - an explicit,
   visible fallback, never a silent default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    ShenwanDirectory,
    code_level,
    is_sw2021_interval,
    is_sw_code,
    level_codes,
)

DEFAULT_INDUSTRY_GROUPS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "industry_groups.yaml"
)
OTHER_GROUP = "other"
FINANCIAL_GROUPS = frozenset({"banking", "insurance", "broker"})

MatchedBy = Literal["sw_l1", "sw_l2", "sw_l3", "code", "legacy_name", "fallback"]
FLAG_INDUSTRY_FALLBACK = "industry_fallback"

_CODE = re.compile(r"^[A-S]\d{2}$")
_LEADING_CODE = re.compile(r"^(?P<code>[A-S]\d{2})")


@dataclass(frozen=True)
class IndustryGroupMatch:
    group: str
    matched_by: MatchedBy
    code: str | None
    system: str = "csrc"
    fallback: bool = False
    """True when the CSRC table decided because no usable Shenwan code exists."""

    def describe(self) -> dict[str, str | bool | None]:
        return {
            "system": self.system,
            "code": self.code,
            "group": self.group,
            "matched_by": self.matched_by,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class ShenwanGroupMap:
    """Shenwan 2021 code -> group: level-1 defaults plus level-2/3 overrides."""

    level1: dict[str, str]
    """Level-1 code (``340000``) -> group."""
    overrides: dict[str, str] = field(default_factory=dict)
    """Level-2 (``490100``) or level-3 (``630601``) code -> group."""

    def resolve(self, code: str | None) -> IndustryGroupMatch | None:
        if code is None or not is_sw_code(code):
            return None
        l1, l2, l3 = level_codes(code)
        if code_level(code) == 3 and l3 in self.overrides:
            return IndustryGroupMatch(self.overrides[l3], "sw_l3", code, SW2021_SYSTEM)
        if code_level(code) >= 2 and l2 in self.overrides:
            return IndustryGroupMatch(self.overrides[l2], "sw_l2", code, SW2021_SYSTEM)
        if l1 in self.level1:
            return IndustryGroupMatch(self.level1[l1], "sw_l1", code, SW2021_SYSTEM)
        return None


def usable_sw_code(
    code: str | None,
    valid_from: date | None,
    valid_to: date | None,
    directory: ShenwanDirectory,
    name: str | None = None,
) -> str | None:
    """The stored ``sw2021`` code if it may drive the group, else ``None``.

    Pre-2021 intervals (ended on / before 2021-07-30) carry old-standard
    codes and codes absent from the directory are unknown: both fall back.
    The sync stores such codes with the code itself as ``name`` (it judges
    the era on the full history, see ``snapshots.shenwan.sw2021_points``), so a stored
    row named by its code is not usable either.
    """

    if code is None or valid_from is None or not is_sw_code(code):
        return None
    if name is not None and name == code:
        return None
    if not is_sw2021_interval(valid_from, valid_to):
        return None
    return code if directory.get(code) is not None else None


@dataclass(frozen=True)
class IndustryGroupMap:
    version: str
    system: str
    groups: dict[str, str]
    """Group name -> description."""
    codes: dict[str, str]
    """CSRC division code -> group (the fallback table)."""
    legacy_name_rules: tuple[tuple[str, str], ...]
    """``(substring, group)`` in priority order."""
    shenwan: ShenwanGroupMap | None = None

    def resolve_classification(
        self,
        *,
        sw_code: str | None,
        csrc_code: str | None,
        csrc_name: str | None = None,
    ) -> IndustryGroupMatch:
        """Shenwan first (``sw_code`` already vetted by :func:`usable_sw_code`), then CSRC."""

        if sw_code is not None and self.shenwan is not None:
            match = self.shenwan.resolve(sw_code)
            if match is not None:
                return match
        csrc = self.resolve(csrc_code, csrc_name)
        if csrc.matched_by == "fallback":
            return csrc  # neither system classifies it: ``other`` / ``fallback``
        return replace(csrc, fallback=True)

    def resolve(self, code: str | None, name: str | None = None) -> IndustryGroupMatch:
        normalized = (code or "").strip().upper() or None
        if normalized is None and name:
            leading = _LEADING_CODE.match(name.strip())
            normalized = leading.group("code") if leading else None
        if normalized is not None and normalized in self.codes:
            return IndustryGroupMatch(self.codes[normalized], "code", normalized)
        label = (name or "").strip()
        # Hierarchical labels ("金融、保险业-证券、期货业"): the most specific
        # segment decides; broader segments are only consulted when it has no hit.
        for segment in reversed([part for part in label.split("-") if part.strip()]):
            for needle, group in self.legacy_name_rules:
                if needle in segment:
                    return IndustryGroupMatch(group, "legacy_name", normalized)
        return IndustryGroupMatch(OTHER_GROUP, "fallback", normalized)

    def group_for(self, code: str | None, name: str | None = None) -> str:
        return self.resolve(code, name).group


def parse_industry_groups(raw: object) -> IndustryGroupMap:
    if not isinstance(raw, dict):
        raise ValueError("industry groups config must be a mapping")
    version = str(raw.get("version") or "").strip()
    if not version:
        raise ValueError("industry groups config needs a version")
    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, dict) or OTHER_GROUP not in groups_raw:
        raise ValueError("industry groups config must define groups including 'other'")
    groups = {str(name): str(description) for name, description in groups_raw.items()}

    codes: dict[str, str] = {}
    for code, group in (raw.get("codes") or {}).items():
        key = str(code).strip().upper()
        if _CODE.fullmatch(key) is None:
            raise ValueError(f"invalid CSRC division code {code!r}")
        if group not in groups:
            raise ValueError(f"code {key} maps to unknown group {group!r}")
        codes[key] = str(group)

    rules: list[tuple[str, str]] = []
    for rule in raw.get("legacy_name_rules") or []:
        if not isinstance(rule, dict) or not rule.get("contains"):
            raise ValueError(f"invalid legacy name rule {rule!r}")
        group = str(rule.get("group"))
        if group not in groups:
            raise ValueError(f"legacy rule {rule!r} maps to unknown group {group!r}")
        rules.append((str(rule["contains"]), group))

    return IndustryGroupMap(
        version=version,
        system=str(raw.get("system") or "csrc"),
        groups=groups,
        codes=codes,
        legacy_name_rules=tuple(rules),
        shenwan=_parse_shenwan(raw.get("shenwan"), groups),
    )


def _parse_shenwan(raw: object, groups: dict[str, str]) -> ShenwanGroupMap | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("shenwan section must be a mapping")
    if str(raw.get("system") or SW2021_SYSTEM) != SW2021_SYSTEM:
        raise ValueError(f"shenwan section system must be {SW2021_SYSTEM!r}")

    def table(name: str, levels: set[int]) -> dict[str, str]:
        result: dict[str, str] = {}
        for code, group in (raw.get(name) or {}).items():
            key = str(code).strip()
            if not is_sw_code(key) or code_level(key) not in levels:
                raise ValueError(f"invalid Shenwan {name} code {code!r}")
            if group not in groups:
                raise ValueError(f"Shenwan code {key} maps to unknown group {group!r}")
            result[key] = str(group)
        return result

    level1 = table("level1", {1})
    if not level1:
        raise ValueError("shenwan section needs level1 mappings")
    overrides = table("overrides", {2, 3})
    orphans = [code for code in overrides if level_codes(code)[0] not in level1]
    if orphans:
        raise ValueError(f"Shenwan overrides without a level-1 default: {orphans}")
    return ShenwanGroupMap(level1=level1, overrides=overrides)


def load_industry_groups(path: Path | str | None = None) -> IndustryGroupMap:
    target = Path(path) if path is not None else DEFAULT_INDUSTRY_GROUPS_PATH
    return parse_industry_groups(yaml.safe_load(target.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default_industry_groups() -> IndustryGroupMap:
    return load_industry_groups()


def industry_group_for(code: str | None, name: str | None = None) -> str:
    """Industry group of a CSRC classification; ``other`` when unmapped."""

    return default_industry_groups().group_for(code, name)
