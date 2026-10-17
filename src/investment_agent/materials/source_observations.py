"""Offline audit of HK2-06 source observation evidence.

This module only reads explicit JSON ledgers, reports, review citations and
content-addressed archives. It never loads application settings, a database,
environment files, or a network client. A positive result is a technical review
candidate; human source approval and authenticity review remain separate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.archive import (
    ArchiveIntegrityError,
    identity_snapshot,
    verify_capture,
)
from investment_agent.materials.contracts import FactPeriod, PeriodBasis, RawPayload
from investment_agent.materials.hk_native import HkNativeAdapter
from investment_agent.materials.us_sec import SecFactsAdapter

SEC_SOURCE = "sec.companyfacts.v1"
EASTMONEY_SOURCE = "eastmoney.hk.native.v1"
SOURCE_COMPANIES: dict[str, tuple[str, ...]] = {
    SEC_SOURCE: (
        "apple",
        "costco",
        "jpmorgan-chase",
        "exxon-mobil",
        "tsmc",
        "hsbc-holdings",
    ),
    EASTMONEY_SOURCE: ("clp-holdings", "hsbc-holdings", "tencent-holdings"),
}
_IDENTITIES = {item.company_key: item for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRILLS = (
    "shutdown",
    "inflight_stop",
    "restart_resume",
    "old_versions_readable",
    "offline_replay",
    "probe_stop_withdrawal",
)


class ObservationAuditError(ValueError):
    """The explicit offline evidence inputs are invalid or unsafe."""


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a local regular file."""

    _reject_symlink_path(path)
    if not path.is_file():
        raise ObservationAuditError(f"evidence file does not exist: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_observations(ledger_path: Path, evidence_root: Path) -> dict[str, Any]:
    """Audit explicit evidence and report readiness independently per source."""

    ledger_path = _checked_existing_file(ledger_path)
    evidence_root = _checked_existing_dir(evidence_root)
    ledger = _read_json(ledger_path)
    if type(ledger.get("schema_version")) is not int or ledger.get("schema_version") != 1:
        raise ObservationAuditError("ledger schema_version must be 1")
    source = ledger.get("source")
    if not isinstance(source, str) or source not in SOURCE_COMPANIES:
        raise ObservationAuditError("ledger source is not an HK2-06 controlled source")
    adapter = ledger.get("adapter_version")
    current_adapter_version = _current_adapter_version(source)
    if adapter != current_adapter_version:
        raise ObservationAuditError("ledger adapter_version is not the current adapter")
    reports = ledger.get("observation_reports")
    reviews = ledger.get("reviews", [])
    if not isinstance(reports, list) or not all(isinstance(x, str) for x in reports):
        raise ObservationAuditError("observation_reports must be a list of relative paths")
    if not isinstance(reviews, list) or not all(isinstance(x, dict) for x in reviews):
        raise ObservationAuditError("reviews must be a list of objects")

    review_by_hash: dict[str, dict[str, Any]] = {}
    review_issues: list[str] = []
    for review in reviews:
        report_hash = review.get("report_sha256")
        if not isinstance(report_hash, str) or not _SHA256.fullmatch(report_hash):
            review_issues.append("review has an invalid report_sha256")
            continue
        if report_hash in review_by_hash:
            review_issues.append("duplicate review for a report")
            continue
        review_by_hash[report_hash] = review

    report_results: list[dict[str, Any]] = []
    seen_report_hashes: set[str] = set()
    used_review_hashes: set[str] = set()
    for relative in reports:
        report_path = _safe_relative_file(evidence_root, relative)
        digest = sha256_file(report_path)
        duplicate_report = digest in seen_report_hashes
        seen_report_hashes.add(digest)
        try:
            report = _read_json(report_path)
            result = _audit_report(report, source, adapter, report_path.parent)
        except (ObservationAuditError, ArchiveIntegrityError, OSError, ValueError) as exc:
            report = {}
            result = {
                "passed": False,
                "observed_at": None,
                "issues": [str(exc)],
                "observed_date": None,
            }
        if duplicate_report:
            result["passed"] = False
            result["issues"].append("duplicate observation report content cannot add an observation")
        review = review_by_hash.get(digest)
        if review is not None:
            used_review_hashes.add(digest)
        review_time = _parse_aware(review.get("reviewed_at")) if review is not None else None
        report_time = _parse_aware(result.get("observed_at"))
        review_time_issue = (
            "manual review predates its observation report"
            if review_time is not None and report_time is not None and review_time < report_time
            else None
        )
        review_result = _audit_review(review, evidence_root, report.get("periods")) if review is not None else {
            "ready": False,
            "issues": ["manual review declaration is missing"],
        }
        if review_time_issue:
            review_result["ready"] = False
            raw_review_issues = review_result.get("issues", [])
            review_issue_list = list(raw_review_issues) if isinstance(raw_review_issues, list) else []
            review_issue_list.append(review_time_issue)
            review_result["issues"] = review_issue_list
        result.update(
            {
                "report": relative,
                "report_sha256": digest,
                "mode": report.get("mode"),
                "status": report.get("status"),
                "manual_review": review_result,
            }
        )
        report_results.append(result)
    if set(review_by_hash) - used_review_hashes:
        review_issues.append("one or more review declarations are not bound to a ledger report")

    # A malformed report with the correct source ledger cannot be safely sorted;
    # fail closed for this audit window rather than silently selecting older runs.
    invalid_report = any(
        not isinstance(r.get("mode"), str)
        or r.get("mode") not in {"real", "simulation", "dry_run"}
        for r in report_results
    )
    invalid_real_time = any(
        result.get("mode") == "real" and result.get("observed_at") is None
        for result in report_results
    )
    real_reports = [r for r in report_results if r.get("mode") == "real"]
    real_reports.sort(
        key=lambda result: _parse_aware(result.get("observed_at"))
        or datetime.min.replace(tzinfo=UTC)
    )
    streak_dates: list[str] = []
    qualifying_reports: list[dict[str, Any]] = []
    last_date: str | None = None
    for result in real_reports:
        observed_date = result.get("observed_date")
        if not result.get("passed"):
            streak_dates = []
            qualifying_reports = []
            last_date = None
        elif isinstance(observed_date, str) and observed_date != last_date:
            streak_dates.append(observed_date)
            qualifying_reports.append(result)
            last_date = observed_date
        elif isinstance(observed_date, str) and observed_date == last_date and qualifying_reports:
            # Same-day retries can replace that day's report for review purposes,
            # but never add another qualifying date.
            qualifying_reports[-1] = result
    report_streak_ready = len(streak_dates) >= 3 and not invalid_real_time and not invalid_report
    latest_three = qualifying_reports[-3:]
    manual_ready = (
        len(latest_three) == 3
        and all(r.get("passed") and r.get("manual_review", {}).get("ready") for r in latest_three)
        and len({r.get("observed_date") for r in latest_three}) == 3
    )
    if review_issues:
        manual_ready = False
    candidate = report_streak_ready and manual_ready
    pending_reasons: list[str] = []
    if not real_reports:
        pending_reasons.append("no real observations recorded")
    if not report_streak_ready:
        pending_reasons.append("fewer than three latest consecutive qualifying real observation dates")
    if invalid_report:
        pending_reasons.append("one or more ledger reports has an invalid mode or malformed structure")
    if not manual_ready:
        pending_reasons.append("three latest observations lack complete human review and drill citations")
    pending_reasons.extend(review_issues)
    return {
        "schema_version": 1,
        "source": source,
        "adapter_version": adapter,
        "automatic_evidence": {
            "real_observations": len(real_reports),
            "latest_qualifying_dates": streak_dates[-3:],
            "ready": report_streak_ready,
            "invalid_real_timestamp": invalid_real_time,
            "invalid_report": invalid_report,
        },
        "manual_review": {"ready": manual_ready, "review_issues": review_issues},
        "reports": report_results,
        "decision": "technical_review_candidate" if candidate else "pending",
        "source_approval": "not_changed",
        "global_enabled": False,
        "pending_reasons": pending_reasons,
    }


def _audit_report(
    report: dict[str, Any], source: str, adapter: str, archive_root: Path
) -> dict[str, Any]:
    issues: list[str] = []
    if type(report.get("schema_version")) is not int or report.get("schema_version") != 1:
        issues.append("report schema_version must be 1")
    if report.get("source") != source:
        issues.append("report source does not match ledger")
    if report.get("adapter_version") != adapter:
        issues.append("report adapter_version does not match current adapter")
    mode = report.get("mode")
    if not isinstance(mode, str) or mode not in {"real", "simulation", "dry_run"}:
        issues.append("report mode is invalid")
    if mode == "real" and report.get("subject_namespace") != "isolated_probe_memory":
        issues.append("real report is not marked as using isolated probe-memory identities")
    observed = _parse_aware(report.get("observed_at"))
    observed_date = observed.astimezone(_SHANGHAI).date().isoformat() if observed else None
    report_periods = _parse_periods(report.get("periods"), source)
    if source == EASTMONEY_SOURCE and not report_periods:
        issues.append("Eastmoney report must carry explicit annual period spans")
    if source == EASTMONEY_SOURCE and not _complete_hk_annual_span(report_periods):
        issues.append("Eastmoney report must cover four consecutive complete annual periods")
    if observed is None:
        issues.append("observed_at must be an aware ISO timestamp")
    elif observed > datetime.now(UTC):
        issues.append("observed_at is in the future")
    companies = report.get("company_keys")
    expected_companies = SOURCE_COMPANIES[source]
    if (
        not isinstance(companies, list)
        or not all(isinstance(company, str) for company in companies)
        or set(companies) != set(expected_companies)
        or len(companies) != len(expected_companies)
    ):
        issues.append("company_keys do not exactly cover the source's controlled range")
    decisions = report.get("source_decisions")
    if not isinstance(decisions, dict) or set(decisions) != set(expected_companies):
        issues.append("source_decisions do not cover the full controlled range")
    elif any(
        not isinstance(decisions.get(company), dict)
        or decisions[company].get("source") != source
        or decisions[company].get("bound") is not True
        or decisions[company].get("licensing") != "approved"
        for company in expected_companies
    ):
        issues.append("one or more source decisions lacks an approved, bound licence")
    if report.get("operational_refresh_enabled") is not False or report.get("global_refresh_enabled") is not False:
        issues.append("isolated observation must leave normal source/global refresh disabled")
    archives = report.get("archives")
    if not isinstance(archives, list):
        issues.append("archives must be a list")
        archives = []
    archive_companies: list[str] = []
    manifest_hashes: set[str] = set()
    response_count = 0
    response_refs: set[tuple[str, str, str]] = set()
    for entry in archives:
        if not isinstance(entry, dict):
            issues.append("archive entry must be an object")
            continue
        company_key = entry.get("company_key")
        ref = entry.get("manifest_ref")
        expected_hash = entry.get("manifest_sha256")
        if not isinstance(company_key, str) or company_key not in expected_companies:
            issues.append("archive has an uncontrolled company_key")
            continue
        archive_companies.append(company_key)
        if not isinstance(ref, str) or not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            issues.append(f"archive reference/hash is malformed for {company_key}")
            continue
        if expected_hash in manifest_hashes:
            issues.append("duplicate archive manifest hash cannot count as multiple companies/observations")
            continue
        manifest_hashes.add(expected_hash)
        capture = verify_capture(archive_root, ref, expected_sha256=expected_hash)
        if capture.source != source or capture.adapter_version != adapter:
            issues.append(f"archive identity/version mismatch for {company_key}")
        if capture.identity.get("company_key") != company_key:
            issues.append(f"archive company identity mismatch for {company_key}")
        identity = _IDENTITIES.get(company_key)
        identity_verified = identity is not None and capture.identity == identity_snapshot(identity)
        if not identity_verified:
            issues.append(f"archive controlled identity does not match pilot list for {company_key}")
        structural_capture_problems = tuple(
            problem
            for problem in capture.problems
            if not identity_verified or not _capture_problem_is_informational(company_key, problem)
        )
        if structural_capture_problems:
            issues.append(f"archive has adapter problems for {company_key}")
        capture_periods = _parse_periods(list(capture.periods), source)
        if source == EASTMONEY_SOURCE and capture_periods != report_periods:
            issues.append(f"archive period span mismatch for {company_key}")
        expected_requests = tuple(capture.expected_requests)
        responses = capture.responses
        if not expected_requests or len(set(expected_requests)) != len(expected_requests):
            issues.append(f"archive expected request list is absent/duplicated for {company_key}")
        company_attempt_rows = [
            item
            for item in report.get("requests", [])
            if isinstance(item, dict) and item.get("company_key") == company_key
        ] if isinstance(report.get("requests"), list) else []
        if any(not isinstance(item.get("name"), str) for item in company_attempt_rows):
            issues.append(f"request name is malformed for {company_key}")
        logical_attempt_names = {
            item["name"] for item in company_attempt_rows if isinstance(item.get("name"), str)
        }
        if set(expected_requests) != logical_attempt_names:
            issues.append(f"archive request set is incomplete or has no issued attempt for {company_key}")
        response_count += len(responses)
        for response in responses:
            response_refs.add(
                (company_key, response.name, hashlib.sha256(response.content).hexdigest())
            )
            if response.http_status < 200 or response.http_status >= 300 or not response.parse_eligible:
                issues.append(f"archive contains unsuccessful/unparseable HTTP response for {company_key}")
            if response.fetched_at is None or observed is None:
                issues.append(f"archive response has no provable fetched_at for {company_key}")
            elif response.fetched_at.astimezone(_SHANGHAI).date().isoformat() != observed_date:
                issues.append(f"archive response was not fetched on observation date for {company_key}")
        if identity is not None and capture.subject_id <= 0:
            issues.append(f"archive has no isolated probe subject id for {company_key}")
        elif identity is not None:
            request_name_by_archive: dict[str, str] = {}
            if isinstance(report.get("requests"), list):
                for item in report["requests"]:
                    if (
                        isinstance(item, dict)
                        and item.get("company_key") == company_key
                        and isinstance(item.get("response_archive_name"), str)
                        and isinstance(item.get("name"), str)
                    ):
                        request_name_by_archive[item["response_archive_name"]] = item["name"]
            raw_payloads = tuple(
                RawPayload(
                    name=request_name_by_archive.get(response.name, response.name),
                    content=response.content,
                    media_type=response.media_type,
                    fetched_at=response.fetched_at or observed or datetime.now(UTC),
                    source_url=response.source_url,
                    http_status=response.http_status,
                )
                for response in responses
            )
            if source == SEC_SOURCE:
                parsed = SecFactsAdapter(subject_id=capture.subject_id).parse(
                    identity, raw=raw_payloads, periods=capture_periods
                )
                structural_problems = parsed.problems
            else:
                hk_parsed = HkNativeAdapter().parse(
                    identity, raw=raw_payloads, periods=capture_periods, subject_id=capture.subject_id
                ) if capture_periods else None
                if hk_parsed is None:
                    issues.append(f"current adapter cannot parse missing period span for {company_key}")
                    continue
                parsed = hk_parsed
                structural_problems = tuple(
                    problem
                    for problem in parsed.problems
                    if not problem.startswith("unmapped_items:") and not problem.startswith("unmapped:")
                )
            if not parsed.drafts:
                issues.append(f"current adapter produced no parsed facts for {company_key}")
            if structural_problems:
                issues.append(f"current adapter reported response structure/parse drift for {company_key}")
            if "parsed_facts" in report and not _reported_facts_match(
                report["parsed_facts"], company_key, parsed.drafts
            ):
                issues.append(f"report parsed_facts do not match current adapter output for {company_key}")
    if sorted(archive_companies) != sorted(expected_companies):
        issues.append("archives do not exactly cover every controlled company once")
    requests_total = _nonnegative_int(report.get("requests_total"))
    succeeded = _nonnegative_int(report.get("requests_succeeded"))
    failed = _nonnegative_int(report.get("requests_failed"))
    attempts = report.get("requests")
    if None in (requests_total, succeeded, failed):
        issues.append("request counters must be nonnegative integers")
    else:
        assert requests_total is not None and succeeded is not None and failed is not None
        if not isinstance(attempts, list) or len(attempts) != requests_total:
            issues.append("request attempt ledger does not match requests_total")
        if requests_total != succeeded + failed or succeeded != response_count or failed != 0:
            issues.append("request counters do not describe only complete successful archived responses")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                issues.append("request attempt must be an object")
                continue
            status = attempt.get("status")
            status_code = attempt.get("http_status")
            logical_name = attempt.get("name")
            numeric_status = _nonnegative_int(status_code)
            company_key = attempt.get("company_key")
            response_name = attempt.get("response_archive_name")
            response_hash = attempt.get("response_sha256")
            retry_attempt = _nonnegative_int(attempt.get("retry_attempt"))
            expected_archive_name = (
                f"{logical_name}__attempt_{retry_attempt}"
                if isinstance(logical_name, str) and retry_attempt is not None
                else None
            )
            started_at = _parse_aware(attempt.get("started_at"))
            finished_at = _parse_aware(attempt.get("finished_at"))
            attempt_duration = attempt.get("duration_seconds")
            if (
                not isinstance(status, str)
                or not status.startswith("http_2")
                or numeric_status is None
                or not 200 <= numeric_status < 300
                or attempt.get("request_sent") is not True
                or started_at is None
                or finished_at is None
                or finished_at < started_at
                or isinstance(attempt_duration, bool)
                or not isinstance(attempt_duration, (int, float))
                or not math.isfinite(attempt_duration)
                or attempt_duration < 0
                or attempt.get("user_agent") != report.get("user_agent")
                or (observed is not None and started_at.astimezone(_SHANGHAI).date().isoformat() != observed_date)
                or (observed is not None and finished_at.astimezone(_SHANGHAI).date().isoformat() != observed_date)
                or not isinstance(company_key, str)
                or not isinstance(logical_name, str)
                or retry_attempt is None
                or retry_attempt < 1
                or not isinstance(response_name, str)
                or response_name != expected_archive_name
                or not isinstance(response_hash, str)
                or (company_key, response_name, response_hash) not in response_refs
            ):
                issues.append("request attempt is unsuccessful or does not bind to an archived response")
    duration = report.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        issues.append("duration_seconds must be nonnegative")
    user_agent = report.get("user_agent")
    if not isinstance(user_agent, str) or not user_agent.strip():
        issues.append("report has no actual User-Agent")
    elif source == SEC_SOURCE:
        if not re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", user_agent, re.I):
            issues.append("SEC User-Agent does not contain a contact email")
        if re.search(
            r"(?i)(example\.|\.invalid\b|placeholder|contact\s+in\s+config|your[-_ ]?email|noreply@example)",
            user_agent,
        ):
            issues.append("SEC User-Agent contains a placeholder contact")
    min_interval = report.get("min_interval_seconds")
    if (
        isinstance(min_interval, bool)
        or not isinstance(min_interval, (int, float))
        or not math.isfinite(min_interval)
        or min_interval <= 0
        or (source == SEC_SOURCE and min_interval < 0.1)
    ):
        issues.append("min_interval_seconds is missing or below the source's documented floor")
    if report.get("status") != "passed":
        issues.append("report status is not passed")
    if report.get("passed_scope") != "single_probe_transport_and_parse_only":
        issues.append("report does not limit its passed scope to single-probe transport and parse")
    if report.get("passed_implies_technical_or_licensing_acceptance") is not False:
        issues.append("report does not explicitly separate probe pass from acceptance")
    report_issues = report.get("issues", [])
    if not isinstance(report_issues, list) or any(
        not isinstance(issue, str) or not _informational_issue(issue)
        for issue in report_issues
    ):
        issues.append("report declares an unresolved issue")
    return {
        "passed": not issues and mode == "real",
        "observed_at": observed.isoformat() if observed else None,
        "observed_date": observed_date,
        "issues": issues,
    }


def _audit_review(
    review: dict[str, Any], evidence_root: Path, observed_periods: Any
) -> dict[str, Any]:
    issues: list[str] = []
    reviewer = review.get("reviewer")
    reviewed_at = _parse_aware(review.get("reviewed_at"))
    if not isinstance(reviewer, str) or not reviewer.strip():
        issues.append("manual reviewer identity is missing")
    if reviewed_at is None or reviewed_at > datetime.now(UTC):
        issues.append("reviewed_at must be an aware, non-future ISO timestamp")
    annual = review.get("annual_comparison")
    allowed_annual = {"matched", "revision_reviewed", "unreviewed"}
    if (
        not isinstance(annual, dict)
        or not isinstance(annual.get("status"), str)
        or annual.get("status") not in allowed_annual
    ):
        issues.append("annual_comparison status is missing or invalid")
    elif annual.get("status") == "unreviewed":
        issues.append("annual comparison is unreviewed")
    elif not _citation_ok(annual.get("citation"), evidence_root):
        issues.append("annual comparison citation is missing, unsafe, or hash-mismatched")
    elif _canonical_json(annual.get("expected_periods")) != _canonical_json(observed_periods):
        issues.append("annual comparison expected_periods do not match the captured period span")
    scope = review.get("source_scope_review")
    if not isinstance(scope, dict) or scope.get("status") != "approved":
        issues.append("source scope/licence review is pending")
    elif not _citation_ok(scope.get("citation"), evidence_root):
        issues.append("source scope/licence citation is missing, unsafe, or hash-mismatched")
    drills = review.get("drills")
    if not isinstance(drills, dict):
        issues.append("drill declarations are missing")
        drills = {}
    for name in _DRILLS:
        drill = drills.get(name)
        if not isinstance(drill, dict) or drill.get("status") != "passed":
            issues.append(f"{name} drill is pending")
        elif not _citation_ok(drill.get("citation"), evidence_root):
            issues.append(f"{name} citation is missing, unsafe, or hash-mismatched")
    return {"ready": not issues, "issues": issues}


def _citation_ok(value: Any, evidence_root: Path) -> bool:
    if not isinstance(value, dict):
        return False
    path, digest = value.get("path"), value.get("sha256")
    if not isinstance(path, str) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return False
    try:
        return sha256_file(_safe_relative_file(evidence_root, path)) == digest
    except (ObservationAuditError, OSError):
        return False


def _reported_facts_match(reported: Any, company_key: str, drafts: Any) -> bool:
    if not isinstance(reported, list):
        return False
    matches = [item for item in reported if isinstance(item, dict) and item.get("company_key") == company_key]
    if len(matches) != 1 or not isinstance(matches[0].get("facts"), list):
        return False
    actual: list[Any] = []
    for draft in drafts:
        payload = getattr(draft, "payload", None)
        if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
            return False
        actual.extend(payload["facts"])
    return _canonical_json(actual) == _canonical_json(matches[0]["facts"])


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_relative_file(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not relative or ".." in posix.parts or "\\" in relative:
        raise ObservationAuditError("evidence path must be a safe relative path")
    candidate = root.joinpath(*posix.parts)
    _reject_symlink_path(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ObservationAuditError("evidence path does not exist") from exc
    if root not in resolved.parents:
        raise ObservationAuditError("evidence path escaped evidence-root")
    if not resolved.is_file():
        raise ObservationAuditError("evidence path is not a regular file")
    return resolved


def _checked_existing_file(path: Path) -> Path:
    _reject_symlink_path(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ObservationAuditError(f"input file does not exist: {path}") from exc
    if not resolved.is_file():
        raise ObservationAuditError(f"input is not a file: {path}")
    return resolved


def _checked_existing_dir(path: Path) -> Path:
    _reject_symlink_path(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ObservationAuditError(f"evidence-root does not exist: {path}") from exc
    if not resolved.is_dir():
        raise ObservationAuditError("evidence-root is not a directory")
    return resolved


def _reject_symlink_path(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        if current.is_symlink():
            raise ObservationAuditError(f"symlink path is not allowed: {current}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObservationAuditError(f"invalid JSON evidence file: {path}") from exc
    if not isinstance(value, dict):
        raise ObservationAuditError(f"JSON evidence must be an object: {path}")
    return value


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _parse_periods(value: Any, source: str) -> tuple[FactPeriod, ...]:
    if source == SEC_SOURCE and value == []:
        return ()
    if not isinstance(value, list):
        return ()
    periods: list[FactPeriod] = []
    try:
        for item in value:
            if not isinstance(item, dict):
                return ()
            start_raw, end_raw, basis_raw = item.get("start"), item.get("end"), item.get("basis")
            if not isinstance(end_raw, str) or not isinstance(basis_raw, str):
                return ()
            start = date.fromisoformat(start_raw) if isinstance(start_raw, str) else None
            if start is not None and start.isoformat() != start_raw:
                return ()
            end = date.fromisoformat(end_raw)
            if end.isoformat() != end_raw:
                return ()
            basis = PeriodBasis(basis_raw)
            label = item.get("label")
            days = item.get("days")
            if label is not None and not isinstance(label, str):
                return ()
            if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days <= 0):
                return ()
            periods.append(FactPeriod(start=start, end=end, basis=basis, label=label, days=days))
    except (TypeError, ValueError):
        return ()
    return tuple(periods)


def _complete_hk_annual_span(periods: tuple[FactPeriod, ...]) -> bool:
    if len(periods) != 4:
        return False
    for index, period in enumerate(periods):
        if (
            period.basis is not PeriodBasis.ANNUAL
            or period.start != date(period.end.year, 1, 1)
            or period.end != date(period.end.year, 12, 31)
            or (index > 0 and period.end.year != periods[index - 1].end.year + 1)
        ):
            return False
    return True


def _informational_issue(issue: str) -> bool:
    return issue.startswith(("unmapped_items:", "unmapped:"))


def _capture_problem_is_informational(company_key: str, problem: str) -> bool:
    """Accept known informational categories only under the verified company key."""

    prefix = f"{company_key}:"
    if not problem.startswith(prefix):
        return False
    return _informational_issue(problem[len(prefix) :])


def _current_adapter_version(source: str) -> str:
    if source == SEC_SOURCE:
        return SecFactsAdapter().adapter_version
    if source == EASTMONEY_SOURCE:
        return HkNativeAdapter().adapter_version
    raise ObservationAuditError("source adapter is not controlled")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
