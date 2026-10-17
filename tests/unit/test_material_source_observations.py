from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tools.hk206_source_acceptance import main as acceptance_main

from investment_agent.domain.security import load_pilot_identities
from investment_agent.materials import source_observations
from investment_agent.materials.archive import (
    ArchiveCapture,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
)
from investment_agent.materials.source_observations import (
    ObservationAuditError,
    audit_observations,
)
from investment_agent.materials.us_sec import SecFactsAdapter
from tests.unit import test_material_source_probe as probe_fixtures

SOURCE = "sec.companyfacts.v1"
COMPANIES = (
    "apple",
    "costco",
    "jpmorgan-chase",
    "exxon-mobil",
    "tsmc",
    "hsbc-holdings",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_report(
    root: Path, day: int, *, minute: int = 0, utc_hour: int = 10,
    mode: str = "real", status: str = "passed"
) -> Path:
    observed = datetime(2026, 10, day, utc_hour, minute, tzinfo=UTC)
    identities = {item.company_key: item for item in load_pilot_identities(
        Path(__file__).resolve().parents[2] / "config/research_material/pilot_list.yaml"
    )}
    archives = []
    responses_total = 0
    attempts: list[dict[str, object]] = []
    report_root = root / "reports"
    report_root.mkdir(parents=True, exist_ok=True)
    for company_index, company_key in enumerate(COMPANIES):
        identity = identities[company_key]
        responses = tuple(
            ResponseEvidence(
                name=f"{name}__attempt_1",
                content=json.dumps({"fixture": name, "day": day}).encode(),
                source_url="https://example.invalid/fixture",
                media_type="application/json",
                http_status=200,
                fetched_at=observed,
            )
            for name in ("companyfacts.json", "submissions.json")
        )
        archived = archive_capture(
            report_root,
            ArchiveCapture(
                identity=identity_snapshot(identity),
                source=SOURCE,
                subject_id=10_000 + day * 10 + company_index,
                adapter_version="1",
                periods=(),
                expected_requests=("companyfacts.json", "submissions.json"),
                responses=responses,
                captured_at=observed,
            ),
        )
        archives.append(
            {
                "company_key": company_key,
                "manifest_ref": archived.manifest_ref,
                "manifest_sha256": archived.manifest_sha256,
            }
        )
        attempts.extend(
            {
                "company_key": company_key,
                "name": response.name.removesuffix("__attempt_1"),
                "status": "http_200",
                "http_status": 200,
                "retry_attempt": 1,
                "request_sent": True,
                "started_at": observed.isoformat(),
                "finished_at": (observed + timedelta(seconds=1)).isoformat(),
                "duration_seconds": 1.0,
                "user_agent": "ZCharlie Research/1 (contact: research-contact@zcharlie.co)",
                "response_archive_name": response.name,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
            }
            for response in responses
        )
        responses_total += len(responses)
    report = {
        "schema_version": 1,
        "source": SOURCE,
        "adapter_version": "1",
        "mode": mode,
        "subject_namespace": "isolated_probe_memory",
        "source_decisions": {
            company: {
                "source": SOURCE,
                "bound": True,
                "licensing": "approved",
            }
            for company in COMPANIES
        },
        "operational_refresh_enabled": False,
        "global_refresh_enabled": False,
        "observed_at": observed.isoformat(),
        "company_keys": list(COMPANIES),
        "planned_company_keys": [],
        "requests_total": responses_total,
        "requests_succeeded": responses_total,
        "requests_failed": 0,
        "requests": attempts,
        "duration_seconds": 1.2,
        "user_agent": "ZCharlie Research/1 (contact: research-contact@zcharlie.co)",
        "min_interval_seconds": 0.5,
        "archives": archives,
        "periods": [],
        "period_selection": "adapter_default_latest_fiscal_years",
        "status": status,
        "passed_scope": "single_probe_transport_and_parse_only",
        "passed_implies_technical_or_licensing_acceptance": False,
        "issues": [],
    }
    path = root / "reports" / f"sec-{day}-{minute}-{mode}-{status}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


def _add_review(path: Path, root: Path) -> dict[str, object]:
    citation_path = root / "reviews" / "human-review.md"
    citation_path.parent.mkdir(parents=True, exist_ok=True)
    if not citation_path.exists():
        citation_path.write_text("local reviewed evidence\n", encoding="utf-8")
    citation = {"path": "reviews/human-review.md", "sha256": _sha(citation_path)}
    report = json.loads(path.read_text(encoding="utf-8"))
    return {
        "report_sha256": _sha(path),
        "reviewer": "manual-reviewer",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "annual_comparison": {
            "status": "matched",
            "expected_periods": report["periods"],
            "citation": citation,
        },
        "source_scope_review": {"status": "approved", "citation": citation},
        "drills": {
            name: {"status": "passed", "citation": citation}
            for name in (
                "shutdown",
                "inflight_stop",
                "restart_resume",
                "old_versions_readable",
                "offline_replay",
                "probe_stop_withdrawal",
            )
        },
    }


def _ledger(
    root: Path,
    reports: list[Path],
    reviews: list[dict[str, object]],
    *,
    source: str = SOURCE,
) -> Path:
    ledger = {
        "schema_version": 1,
        "source": source,
        "adapter_version": source_observations._current_adapter_version(source),
        "observation_reports": [path.relative_to(root).as_posix() for path in reports],
        "reviews": reviews,
    }
    path = root / "ledger.json"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    return path


def _fake_parse(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(drafts=(object(),), problems=())


def test_three_real_full_scope_days_are_only_a_manual_review_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [_make_report(tmp_path, day) for day in (10, 11, 12)]
    reviews = [_add_review(path, tmp_path) for path in reports]
    result = audit_observations(_ledger(tmp_path, reports, reviews), tmp_path)

    assert result["decision"] == "technical_review_candidate", result
    assert result["source_approval"] == "not_changed"
    assert result["global_enabled"] is False
    assert result["automatic_evidence"]["ready"] is True
    assert result["manual_review"]["ready"] is True


def test_simulation_does_not_fill_a_missing_real_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [
        _make_report(tmp_path, 10),
        _make_report(tmp_path, 11, mode="simulation"),
        _make_report(tmp_path, 12),
    ]
    result = audit_observations(_ledger(tmp_path, reports, []), tmp_path)

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False


def test_latest_real_failure_resets_streak_instead_of_selecting_old_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [_make_report(tmp_path, day) for day in (10, 11, 12)]
    failed = _make_report(tmp_path, 13, status="failed")
    reports.append(failed)
    reviews = [_add_review(path, tmp_path) for path in reports]

    result = audit_observations(_ledger(tmp_path, reports, reviews), tmp_path)

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False
    assert result["automatic_evidence"]["latest_qualifying_dates"] == []


def test_same_day_reports_do_not_add_an_observation_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [
        _make_report(tmp_path, 10),
        _make_report(tmp_path, 10, minute=5),
        _make_report(tmp_path, 11),
    ]
    reviews = [_add_review(path, tmp_path) for path in reports]

    result = audit_observations(_ledger(tmp_path, reports, reviews), tmp_path)

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["latest_qualifying_dates"] == [
        "2026-10-10",
        "2026-10-11",
    ]


def test_observation_date_uses_shanghai_day_across_utc_midnight_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10, utc_hour=18)

    result = audit_observations(_ledger(tmp_path, [report], []), tmp_path)

    assert result["reports"][0]["observed_date"] == "2026-10-11"
    assert result["reports"][0]["passed"] is True


def test_review_must_not_predate_observation_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10)
    review = _add_review(report, tmp_path)
    review["reviewed_at"] = "2026-10-10T09:00:00+00:00"

    result = audit_observations(_ledger(tmp_path, [report], [review]), tmp_path)

    assert result["decision"] == "pending"
    assert result["reports"][0]["manual_review"]["ready"] is False
    assert "manual review predates its observation report" in result["reports"][0]["manual_review"]["issues"]


def test_nonfinite_duration_is_not_valid_real_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [_make_report(tmp_path, day) for day in (10, 11, 12)]
    latest_payload = json.loads(reports[-1].read_text(encoding="utf-8"))
    latest_payload["duration_seconds"] = float("nan")
    reports[-1].write_text(json.dumps(latest_payload), encoding="utf-8")
    reviews = [_add_review(path, tmp_path) for path in reports]

    result = audit_observations(_ledger(tmp_path, reports, reviews), tmp_path)

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is False
    assert "duration_seconds must be nonnegative" in result["reports"][-1]["issues"]


def test_malformed_unhashable_company_keys_fail_closed_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["company_keys"] = [[]]
    report.write_text(json.dumps(payload), encoding="utf-8")

    result = audit_observations(_ledger(tmp_path, [report], []), tmp_path)

    assert result["decision"] == "pending"
    assert any("company_keys" in issue for issue in result["reports"][0]["issues"])


def test_malformed_ledger_source_is_a_clean_input_error(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger-malformed.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": [],
                "adapter_version": "1",
                "observation_reports": [],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ObservationAuditError, match="controlled source"):
        audit_observations(ledger, tmp_path)


def test_ledger_version_is_checked_against_current_adapter_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, [], [])
    monkeypatch.setattr(
        source_observations,
        "SecFactsAdapter",
        lambda: SimpleNamespace(adapter_version="next-version"),
    )

    with pytest.raises(ObservationAuditError, match="not the current adapter"):
        audit_observations(ledger, tmp_path)


def test_only_verified_company_prefix_with_known_unmapped_category_is_informational() -> None:
    assert source_observations._capture_problem_is_informational(
        "clp-holdings", "clp-holdings:unmapped_items:unknown-label"
    )
    assert not source_observations._capture_problem_is_informational(
        "clp-holdings", "other-company:unmapped_items:unknown-label"
    )
    assert not source_observations._capture_problem_is_informational(
        "clp-holdings", "clp-holdings:structure_has_unmapped_items:unknown-label"
    )


def test_old_adapter_report_is_rejected_against_current_ledger_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["adapter_version"] = "older-version"
    report.write_text(json.dumps(payload), encoding="utf-8")

    result = audit_observations(_ledger(tmp_path, [report], []), tmp_path)

    assert result["reports"][0]["passed"] is False
    assert "report adapter_version does not match current adapter" in result["reports"][0]["issues"]


def test_unreviewed_or_tampered_citation_cannot_become_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    reports = [_make_report(tmp_path, day) for day in (10, 11, 12)]
    reviews = [_add_review(path, tmp_path) for path in reports]
    citation = tmp_path / "reviews/human-review.md"
    citation.write_text("changed after review\n", encoding="utf-8")

    result = audit_observations(_ledger(tmp_path, reports, reviews), tmp_path)

    assert result["decision"] == "pending"
    assert result["automatic_evidence"]["ready"] is True
    assert result["manual_review"]["ready"] is False


def test_old_capture_cannot_be_relabelled_as_a_new_observation_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 12)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["observed_at"] = "2026-10-13T10:00:00+00:00"
    report.write_text(json.dumps(payload), encoding="utf-8")

    result = audit_observations(_ledger(tmp_path, [report], [_add_review(report, tmp_path)]), tmp_path)

    assert result["decision"] == "pending"
    assert any(
        "not fetched on observation date" in issue
        for issue in result["reports"][0]["issues"]
    )


def test_observation_plan_is_disabled_and_contains_no_observation_or_review_records() -> None:
    plan_path = (
        Path(__file__).resolve().parents[2]
        / "config/research_material/observation_plan.yaml"
    )
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))

    assert plan["kind"] == "plan_only"
    assert plan["default_enabled"] is False
    assert all(source["observations"] == [] and source["reviews"] == [] for source in plan["sources"].values())


def test_report_path_escape_and_symlink_are_rejected(tmp_path: Path) -> None:
    escaped = tmp_path.parent / "escaped.json"
    escaped.write_text("{}", encoding="utf-8")
    escaped_ledger = tmp_path / "escaped-ledger.json"
    escaped_ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": SOURCE,
                "adapter_version": "1",
                "observation_reports": ["../escaped.json"],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservationAuditError, match="relative path"):
        audit_observations(escaped_ledger, tmp_path)

    external = tmp_path.parent / "linked-report.json"
    external.write_text("{}", encoding="utf-8")
    link = tmp_path / "report-link.json"
    link.symlink_to(external)
    ledger = tmp_path / "ledger-link.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": SOURCE,
                "adapter_version": "1",
                "observation_reports": ["report-link.json"],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ObservationAuditError, match="symlink"):
        audit_observations(ledger, tmp_path)


def test_cli_refuses_to_overwrite_an_existing_output(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    ledger = _ledger(evidence_root, [], [])
    output = tmp_path / "result.json"
    output.write_text("keep this\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        acceptance_main(
            [
                "--ledger",
                str(ledger),
                "--evidence-root",
                str(evidence_root),
                "--output",
                str(output),
            ]
        )

    assert output.read_text(encoding="utf-8") == "keep this\n"


@pytest.mark.parametrize("malformed_field", ["report_mode", "annual_status"])
def test_cli_malformed_list_values_fail_closed_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    malformed_field: str,
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10)
    review = _add_review(report, tmp_path)
    if malformed_field == "report_mode":
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["mode"] = []
        report.write_text(json.dumps(payload), encoding="utf-8")
        review = _add_review(report, tmp_path)
    else:
        review["annual_comparison"]["status"] = []
    ledger = _ledger(tmp_path, [report], [review])

    assert acceptance_main(
        ["--ledger", str(ledger), "--evidence-root", str(tmp_path)]
    ) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["decision"] == "pending"
    if malformed_field == "report_mode":
        assert output["automatic_evidence"]["invalid_report"] is True
    else:
        assert output["reports"][0]["manual_review"]["ready"] is False


@pytest.mark.parametrize("malformed_name", [[], {}])
def test_cli_malformed_request_name_fails_closed_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    malformed_name: object,
) -> None:
    monkeypatch.setattr(SecFactsAdapter, "parse", _fake_parse)
    report = _make_report(tmp_path, 10)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["requests"][0]["name"] = malformed_name
    report.write_text(json.dumps(payload), encoding="utf-8")
    ledger = _ledger(tmp_path, [report], [])

    assert acceptance_main(
        ["--ledger", str(ledger), "--evidence-root", str(tmp_path)]
    ) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["decision"] == "pending"
    assert any(
        "request name is malformed" in issue for issue in output["reports"][0]["issues"]
    )


def test_cli_reads_two_subdirectory_reports_generated_by_probe_simulation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from investment_agent.domain.security import (
        DEFAULT_PILOT_LIST_PATH,
        SOURCE_EASTMONEY_HK,
        load_pilot_identities,
    )
    from investment_agent.materials import source_probe

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    identities = [
        item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if any(binding.source == SOURCE_EASTMONEY_HK for binding in item.sources)
    ]
    fetcher = probe_fixtures._FakeFetcher(
        probe_fixtures.GOOD_UA, identities[0], include_unmapped=True
    )
    approvals = {
        SOURCE_EASTMONEY_HK: {
            "licensing": {
                "status": "approved",
                "company_keys": [identity.company_key for identity in identities],
                "citation": "synthetic:observation-integration",
            },
            "technical_acceptance": {
                "status": "not_accepted",
                "company_keys": [],
                "reports": [],
            },
        }
    }
    dependencies = source_probe.ProbeDependencies(
        approvals=approvals,
        fetcher_factory=lambda _ua: fetcher,
    )
    reports: list[Path] = []
    for label in ("day-a", "day-b"):
        work_dir = evidence / label
        report = source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=work_dir,
            user_agent=probe_fixtures.GOOD_UA,
            allow_network=True,
            periods=probe_fixtures._hk_periods(),
            dependencies=dependencies,
        )
        assert report["mode"] == "simulation"
        assert report["status"] == "passed"
        reports.append(work_dir / "report.json")
    ledger_path = _ledger(evidence, reports, [], source=SOURCE_EASTMONEY_HK)

    assert acceptance_main(
        ["--ledger", str(ledger_path), "--evidence-root", str(evidence)]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "pending"
    assert output["automatic_evidence"]["real_observations"] == 0
    assert output["automatic_evidence"]["latest_qualifying_dates"] == []
    assert all(item["mode"] == "simulation" for item in output["reports"])
    assert all(item["issues"] == [] for item in output["reports"]), output["reports"]
