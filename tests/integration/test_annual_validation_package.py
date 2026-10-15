from __future__ import annotations

import json
from pathlib import Path

from investment_agent.materials.annual_validation import verify_annual_package
from tests.unit.test_annual_validation import _make_package


def test_offline_package_verification_is_relocatable_and_company_isolated(tmp_path: Path) -> None:
    manifest_path, source_root, manifest = _make_package(tmp_path)
    relocated = tmp_path / "copied-package"
    relocated.mkdir()
    copied_root = relocated / "raw"
    copied_root.mkdir()
    for path in source_root.rglob("*"):
        if path.is_file():
            target = copied_root / path.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    copied_manifest = relocated / "annual.json"
    copied_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    complete = verify_annual_package(copied_manifest, copied_root)
    assert complete["complete"] is True
    json.dumps(complete)

    # Corrupt one saved SEC shard. Verification stays offline and reports the
    # affected company while retaining independent results for the other seven.
    apple = next(item for item in manifest["companies"] if item["company_key"] == "apple")
    shard = next(item for item in apple["documents"] if item["role"] == "sec_shard")
    (copied_root / shard["path"]).write_bytes(b"{broken")
    partial = verify_annual_package(copied_manifest, copied_root)
    by_key = {item["company_key"]: item for item in partial["companies"]}

    assert partial["complete"] is False
    assert by_key["apple"]["status"] == "partial"
    assert by_key["costco"]["status"] == "complete"
