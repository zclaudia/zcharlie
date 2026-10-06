"""CSRC industry -> industry group mapping (P1-B3).

The mapping lives in ``config/industry_groups.yaml`` (versioned). Factor
applicability and rule-template applicability reference the groups defined
there; free-text industry keywords are not used by the factor layer.

Resolution order for one classification ``(code, name)``:

1. ``code`` (e.g. ``J66``) in ``codes``;
2. a code embedded at the start of ``name`` (``"J66货币金融服务"``);
3. the first ``legacy_name_rules`` substring hit on ``name`` (pre-2012
   code-less labels; the most specific "-" segment is tried first);
4. otherwise ``other`` with ``matched_by="fallback"`` - an explicit,
   visible fallback, never a silent default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

DEFAULT_INDUSTRY_GROUPS_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "industry_groups.yaml"
)
OTHER_GROUP = "other"
FINANCIAL_GROUPS = frozenset({"banking", "insurance", "broker"})

MatchedBy = Literal["code", "legacy_name", "fallback"]

_CODE = re.compile(r"^[A-S]\d{2}$")
_LEADING_CODE = re.compile(r"^(?P<code>[A-S]\d{2})")


@dataclass(frozen=True)
class IndustryGroupMatch:
    group: str
    matched_by: MatchedBy
    code: str | None


@dataclass(frozen=True)
class IndustryGroupMap:
    version: str
    system: str
    groups: dict[str, str]
    """Group name -> description."""
    codes: dict[str, str]
    """CSRC division code -> group."""
    legacy_name_rules: tuple[tuple[str, str], ...]
    """``(substring, group)`` in priority order."""

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
    )


def load_industry_groups(path: Path | str | None = None) -> IndustryGroupMap:
    target = Path(path) if path is not None else DEFAULT_INDUSTRY_GROUPS_PATH
    return parse_industry_groups(yaml.safe_load(target.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def default_industry_groups() -> IndustryGroupMap:
    return load_industry_groups()


def industry_group_for(code: str | None, name: str | None = None) -> str:
    """Industry group of a CSRC classification; ``other`` when unmapped."""

    return default_industry_groups().group_for(code, name)
