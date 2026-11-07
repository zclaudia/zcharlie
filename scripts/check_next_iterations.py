"""Run reviewed, explicit offline test scopes; never discovers the whole app."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))
import next_iterations_offline as offline_guard  # type: ignore[import-not-found]  # noqa: E402

# Extend only after reviewing each test's I/O and task ownership. Missing scopes
# are errors, not a successful empty test run. No directory-wide globbing.
SCOPES: dict[str, tuple[str, ...]] = {
    "BASE-03": (
        "tests/next_iterations/test_offline_guard.py",
        "tests/unit/test_next_iterations_guard_probe.py",
        "tests/integration/test_next_iterations_guard_probe.py",
    ),
    "BT-009": ("tests/next_iterations/test_bank_topic_fixture_integrity.py",),
    "BT-001-005": (
        "tests/next_iterations/test_bank_topic_contracts.py",
        "tests/next_iterations/test_bank_condition_contracts.py",
        "tests/next_iterations/test_bank_topic_models.py",
        "tests/next_iterations/test_bank_condition_models.py",
        "tests/next_iterations/test_bank_topic_ref_kinds.py",
    ),
    "BT-foundation": (
        "tests/next_iterations/test_bank_topic_contracts.py",
        "tests/next_iterations/test_bank_condition_contracts.py",
        "tests/next_iterations/test_bank_topic_models.py",
        "tests/next_iterations/test_bank_condition_models.py",
        "tests/next_iterations/test_bank_topic_ref_kinds.py",
        "tests/next_iterations/test_bank_topic_repository.py",
        "tests/next_iterations/test_bank_topic_mutations.py",
        "tests/next_iterations/test_bank_topic_migration.py",
        "tests/next_iterations/test_bank_topic_fixture_integrity.py",
        "tests/next_iterations/test_bank_nii_proofs.py",
        "tests/next_iterations/test_bank_decomposition_proofs.py",
        "tests/next_iterations/test_bank_decomposition.py",
        "tests/next_iterations/test_bank_topic_references.py",
    ),
    "PT-foundation": (
        "tests/portfolio_stress/test_stress_contracts.py",
        "tests/portfolio_stress/test_stress_models.py",
        "tests/portfolio_stress/test_stress_migrations.py",
        "tests/portfolio_stress/test_method_isolation.py",
    ),
    "PT-formal-regression": (
        "tests/test_investor_publication.py",
        "tests/test_portfolio_plans.py",
    ),
    "IT-001": ("tests/inbox/test_contracts.py",),
    "IT-002": ("tests/inbox/test_models.py",),
    "QT-001-002": (
        "tests/unit/test_quality_contracts.py",
        "tests/unit/test_quality_dataset.py",
    ),
    "QT-storage": (
        "tests/unit/test_quality_models.py",
        "tests/unit/test_quality_repository.py",
        "tests/unit/test_quality_settings.py",
        "tests/integration/test_quality_storage.py",
        "tests/integration/test_quality_migrations.py",
    ),
    "EV-06-09": (
        "tests/next_iterations/test_event_receipts.py",
        "tests/next_iterations/test_event_delivery_unit.py",
        "tests/next_iterations/test_event_maintenance.py",
        "tests/next_iterations/test_event_runtime_registration.py",
    ),
    "EV-04-05": (
        "tests/next_iterations/test_event_append.py",
        "tests/next_iterations/test_event_resolvers.py",
    ),
    "EV-01-03": (
        "tests/next_iterations/test_event_contracts.py",
        "tests/next_iterations/test_event_models.py",
        "tests/next_iterations/test_event_migrations.py",
    ),
    "QT-G0": (
        "tests/unit/test_quality_entry.py",
        "tests/unit/test_quality_entry_wiring.py",
        "tests/integration/test_quality_entry_acceptance.py",
        "tests/unit/test_quality_entry_dom.mjs",
    ),
    "PK": ("tests/next_iterations/test_package_assets.py",),
    "EV": (), "QT": (), "IT": (), "BT": (), "PT": (), "SV": (), "XA": (),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", required=True, choices=SCOPES)
    parser.add_argument("--offline", action="store_true", required=True)
    options = parser.parse_args()
    files = SCOPES[options.scope]
    if not files:
        parser.error(f"scope {options.scope} has no reviewed implemented tests yet")
    for filename in files:
        path = (ROOT / filename).resolve()
        if (not path.is_relative_to(ROOT) or not path.is_file()
                or path.suffix not in {".py", ".mjs"}
                or any(part in {"browser", "e2e", "network"} for part in path.parts)):
            parser.error(f"invalid offline allowlist entry: {filename}")
    env = offline_guard.offline_environment()
    print(f"Offline scope {options.scope}: {len(files)} explicit files", flush=True)
    # -p is essential: it applies to sibling unit/integration and old regressions,
    # before their conftests and imports, rather than just next_iterations/.
    python_files = [filename for filename in files if filename.endswith(".py")]
    node_files = [filename for filename in files if filename.endswith(".mjs")]
    if python_files:
        command = [sys.executable, "-m", "pytest", "-p", "next_iterations_offline", *python_files,
                   "-m", "not network", "-q"]
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if result.returncode:
            return result.returncode
    if node_files:
        # Node 22 uses the experimental spelling. Avoid node:test's process
        # spawn mode: the shared bootstrap intentionally prohibits subprocesses.
        # Node synthesizes a passing file-named test for an empty .mjs. Count
        # only declared, descriptively named tests, excluding those file labels.
        file_labels = {label for filename in node_files
                       for label in (filename, str(ROOT / filename), Path(filename).name)}
        command = ["node", "--test", "--experimental-test-isolation=none",
                   "--test-reporter=tap", *node_files]
        node_result = subprocess.run(command, cwd=ROOT, env=env, check=False,
                                     capture_output=True, text=True)
        print(node_result.stdout, end="")
        print(node_result.stderr, end="", file=sys.stderr)
        if node_result.returncode:
            return node_result.returncode
        count = re.search(r"^# tests (\d+)$", node_result.stdout, re.MULTILINE)
        passed = re.search(r"^# pass (\d+)$", node_result.stdout, re.MULTILINE)
        placeholders = sum(label in file_labels for label in
                           re.findall(r"^# Subtest: (.+)$", node_result.stdout, re.MULTILINE))
        if (count is None or passed is None or int(count.group(1)) - placeholders <= 0
                or int(passed.group(1)) - placeholders <= 0):
            print("offline Node allowlist collected zero tests", file=sys.stderr)
            return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
