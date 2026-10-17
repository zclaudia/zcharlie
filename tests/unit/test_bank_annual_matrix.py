from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from investment_agent.industry_peers.bank_annual_matrix import (
    _local_file,
    load_reviewed_bank_bundle,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "bank_fy2024"
BUNDLE_NAMES = ("icbc.json", "large.json", "regional.json", "scope.json")


def _fixture_paths() -> list[Path]:
    return [FIXTURE_ROOT / name for name in BUNDLE_NAMES]


def _copy_bundles(tmp_path: Path) -> list[Path]:
    paths = []
    for index, source in enumerate(_fixture_paths()):
        destination = tmp_path / f"bundle-{index}.json"
        shutil.copyfile(source, destination)
        paths.append(destination)
    return paths


def _read_bundle(path: Path) -> dict:
    return json.loads(path.read_text())


def _write_bundle(path: Path, bundle: dict) -> None:
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2))


def test_load_reviewed_bank_bundle_accepts_50_independent_slots() -> None:
    bundle = load_reviewed_bank_bundle(_fixture_paths())

    slots = {(record["code"], record["metric"]) for record in bundle["records"]}
    assert len(bundle["records"]) == 50
    assert len(slots) == 50
    assert len(bundle["sources"]) == 10


def test_load_reviewed_bank_bundle_rejects_too_few_banks() -> None:
    with pytest.raises(ValueError, match="exactly ten banks and fifty unique slots"):
        load_reviewed_bank_bundle(_fixture_paths()[:3])


def test_load_reviewed_bank_bundle_rejects_duplicate_slot(tmp_path: Path) -> None:
    paths = _copy_bundles(tmp_path)
    bundle = _read_bundle(paths[0])
    bundle["records"][1] = bundle["records"][0].copy()
    _write_bundle(paths[0], bundle)

    with pytest.raises(ValueError, match="exactly ten banks and fifty unique slots"):
        load_reviewed_bank_bundle(paths)


def test_load_reviewed_bank_bundle_rejects_wrong_report_year(tmp_path: Path) -> None:
    paths = _copy_bundles(tmp_path)
    bundle = _read_bundle(paths[0])
    bundle["records"][0]["period"] = "2025-12-31"
    _write_bundle(paths[0], bundle)

    with pytest.raises(ValueError, match="exactly ten banks and fifty unique slots"):
        load_reviewed_bank_bundle(paths)


def test_load_reviewed_bank_bundle_rejects_unsupported_status(tmp_path: Path) -> None:
    paths = _copy_bundles(tmp_path)
    bundle = _read_bundle(paths[0])
    bundle["records"][0]["status"] = "excluded"
    _write_bundle(paths[0], bundle)

    with pytest.raises(ValueError, match="invalid bank evidence status"):
        load_reviewed_bank_bundle(paths)


@pytest.mark.parametrize("missing_field", ["reason", "remediation"])
def test_load_reviewed_bank_bundle_requires_reason_and_remediation_for_unresolved_fields(
    tmp_path: Path, missing_field: str
) -> None:
    paths = _copy_bundles(tmp_path)
    target = paths[2]
    bundle = _read_bundle(target)
    record = next(item for item in bundle["records"] if item["status"] != "verified")
    record[missing_field] = None
    if missing_field == "reason":
        record.pop("candidate_reason", None)
    _write_bundle(target, bundle)

    with pytest.raises(ValueError, match="explicit reason and remedy"):
        load_reviewed_bank_bundle(paths)


def test_local_file_rejects_path_outside_explicit_evidence_root(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"

    with pytest.raises(ValueError, match="escapes the explicitly selected data root"):
        _local_file(evidence_root, "../outside.pdf")
