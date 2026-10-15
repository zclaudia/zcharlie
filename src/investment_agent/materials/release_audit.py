"""Read-only release and restore integrity audit for research materials."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.evidence.adapters._fields import EVIDENCE_FULL_FIELDS
from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
from investment_agent.materials.archive import ArchiveIntegrityError, verify_capture
from investment_agent.materials.context import (
    ANNUAL_FACTS,
    REVIEWED_ANNUAL_SOURCE,
    _valid_review_envelope,
    online_refresh_enabled,
)
from investment_agent.materials.contracts import fact_from_payload
from investment_agent.materials.evidence import _content_hash as _material_evidence_hash
from investment_agent.materials.evidence import dataset_facts, material_evidence
from investment_agent.materials.investor_bridge import (
    MATERIAL_CALCULATION_METHOD,
    material_calculation_input_hash,
)
from investment_agent.storage.models import Base  # noqa: F401 - register project models
from investment_agent.storage.models.research_material import ResearchDataset

_REPORT_SCHEMA_VERSION = 1
_HEX = re.compile(r"^[0-9a-f]{64}$")


def audit_release(database: Path, data_dir: Path, *, require_m1: bool = False) -> dict[str, Any]:
    """Audit one restored database and its local materials without writing either."""

    database = Path(database).expanduser().absolute()
    data_dir = Path(data_dir).expanduser().absolute()
    issues: list[str] = []
    report: dict[str, Any] = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "ok": False,
        "issues": issues,
        "mode": "offline_read_only",
        "code": _code_state(),
        "database": {"path": str(database), "migration": {"expected_heads": _migration_heads(), "actual_heads": []}},
        "materials": {"dataset_count": 0, "reviewed_dataset_count": 0, "valid_review_count": 0,
                      "identity_coverage": [], "calculation_count": 0, "valid_calculation_count": 0,
                      "raw_artifact_gaps": [], "missing_values": []},
        "source_switches": {
            "online_refresh_enabled": online_refresh_enabled(),
            "production_readiness": False,
        },
        "readiness": "offline integrity only; no online or production readiness claim",
    }
    if not database.is_file():
        issues.append("database does not exist or is not a regular file; no database was created")
        return report
    if database.is_symlink():
        issues.append("database path is a symbolic link")
        return report
    if data_dir.is_symlink():
        issues.append("data directory is a symbolic link")
        return report
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        issues.append(f"database cannot be opened read-only: {type(exc).__name__}")
        return report

    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        report["database"]["integrity_check"] = [row[0] for row in integrity]
        if len(integrity) != 1 or integrity[0][0] != "ok":
            issues.append("SQLite integrity_check failed")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        report["database"]["foreign_key_violations"] = len(foreign_keys)
        if foreign_keys:
            issues.append(f"SQLite foreign_key_check found {len(foreign_keys)} violation(s)")

        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "alembic_version" not in tables:
            issues.append("database has no alembic_version table")
        else:
            actual = sorted(str(row[0]) for row in connection.execute("SELECT version_num FROM alembic_version"))
            report["database"]["migration"]["actual_heads"] = actual
            expected = report["database"]["migration"]["expected_heads"]
            if actual != expected:
                issues.append(f"database migration head mismatch: expected {expected}, found {actual}")

        required_tables = {"research_dataset", "company", "listing", "investor_evidence", "investor_calculation"}
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            issues.append(f"database is missing required table(s): {', '.join(missing_tables)}")
            return report
        if online_refresh_enabled():
            issues.append("online material refresh is enabled; offline release audit refuses this state")

        datasets = [_dataset_from_row(row) for row in connection.execute("SELECT * FROM research_dataset ORDER BY id")]
        companies = {int(row["id"]): dict(row) for row in connection.execute("SELECT * FROM company")}
        listings = {int(row["id"]): dict(row) for row in connection.execute("SELECT * FROM listing")}
        report["materials"]["dataset_count"] = len(datasets)
        _audit_dataset_artifacts(datasets, data_dir, issues, report["materials"]["raw_artifact_gaps"])

        identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        company_ids_by_key: dict[str, set[int]] = {}
        for listing in listings.values():
            company = companies.get(int(listing["company_id"]))
            if company is None:
                continue
            for identity in identities:
                if (listing["market_code"], listing["ticker"]) == (identity.market_code, identity.ticker):
                    if company["canonical_name"] != identity.company_name:
                        issues.append(f"controlled identity mismatch for {identity.company_key}: database company name differs")
                    company_ids_by_key.setdefault(identity.company_key, set()).add(int(company["id"]))
        report["materials"]["identity_coverage"] = _coverage(identities, company_ids_by_key)
        reviewed = [row for row in datasets if row.source == REVIEWED_ANNUAL_SOURCE]
        report["materials"]["reviewed_dataset_count"] = len(reviewed)
        valid_reviewed: list[ResearchDataset] = []
        review_receipts: dict[int, Any] = {}
        company_name_to_identity = {identity.company_name: identity for identity in identities}
        for dataset in reviewed:
            company = companies.get(dataset.subject_id)
            review_identity = company_name_to_identity.get(company["canonical_name"]) if company else None
            expected_key = review_identity.company_key if review_identity else None
            receipt = _valid_review_envelope(dataset, expected_company_key=expected_key)
            if dataset.kind != ANNUAL_FACTS or dataset.subject_kind != "company" or dataset.schema_version != 1:
                issues.append(f"reviewed dataset {dataset.id} has an unexpected kind, subject, or schema")
            if not dataset.raw_ref:
                issues.append(f"reviewed dataset {dataset.id} is missing its local evidence index raw_ref")
            if receipt is None:
                issues.append(f"reviewed dataset {dataset.id} has an invalid receipt or company identity binding")
                continue
            if review_identity is None:
                issues.append(f"reviewed dataset {dataset.id} is not bound to a controlled company identity")
                continue
            try:
                _audit_review_originals(dataset, data_dir)
            except (OSError, ValueError, TypeError) as exc:
                issues.append(f"reviewed dataset {dataset.id} original check failed: {exc}")
                continue
            valid_reviewed.append(dataset)
            review_receipts[dataset.id] = receipt
        report["materials"]["valid_review_count"] = len(valid_reviewed)
        current_reviewed = _current_reviewed(valid_reviewed, review_receipts)

        evidence_map = _audit_saved_evidence(connection, datasets, listings, issues)
        valid_calcs, current_calcs, valid_calc_by_company = _audit_calculations(
            connection, evidence_map, listings, current_reviewed, issues
        )
        report["materials"]["calculation_count"] = _count(connection, "investor_calculation")
        report["materials"]["valid_calculation_count"] = valid_calcs
        report["materials"]["current_reviewed_calculation_count"] = current_calcs
        report["materials"]["missing_values"] = _missing_value_summary(current_reviewed)
        _enrich_coverage(
            report["materials"]["identity_coverage"], identities, current_reviewed,
            valid_calc_by_company,
        )
        if "research_material_acquisition" in tables:
            _audit_archives(connection, datasets, data_dir, issues)
        if require_m1:
            for identity in identities:
                matching_company_ids = company_ids_by_key.get(identity.company_key, set())
                reviewed_ids = {dataset.subject_id for dataset in current_reviewed}
                if not matching_company_ids or not any(cid in reviewed_ids for cid in matching_company_ids):
                    issues.append(f"M1 requires a valid reviewed annual dataset for {identity.company_key}")
                if not matching_company_ids or not any(
                    valid_calc_by_company.get(cid, 0) > 0 for cid in matching_company_ids
                ):
                    issues.append(f"M1 requires at least one valid saved calculation for {identity.company_key}")
            if len({identity.company_key for identity in identities}) != 8:
                issues.append("controlled M1 identity list does not contain exactly eight company identities")
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as exc:
        issues.append(f"audit could not complete: {type(exc).__name__}: {exc}")
    finally:
        connection.close()
    report["ok"] = not issues
    return report


def _dataset_from_row(row: sqlite3.Row) -> ResearchDataset:
    data = dict(row)
    for name in ("payload",):
        data[name] = json.loads(data[name]) if isinstance(data[name], str) else data[name]
    for name in ("captured_at", "source_fetched_at", "created_at", "updated_at"):
        if data.get(name) is not None and isinstance(data[name], str):
            data[name] = datetime.fromisoformat(data[name])
    if data.get("period_end"):
        data["period_end"] = date.fromisoformat(data["period_end"])
    return ResearchDataset(**data)


def _audit_dataset_artifacts(
    datasets: list[ResearchDataset], data_dir: Path, issues: list[str], gaps: list[dict[str, Any]]
) -> None:
    for row in datasets:
        if not row.raw_ref:
            digest = hashlib.sha256(json.dumps(
                row.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")).hexdigest()
            if digest != row.content_hash:
                issues.append(f"research_dataset {row.id} payload-only content_hash does not match")
            else:
                gaps.append({"dataset_id": row.id, "reason": "no raw artifact was stored for this dataset"})
            continue
        try:
            target = _safe_data_path(data_dir, row.raw_ref)
            body = target.read_bytes()
            content_digest = hashlib.sha256(json.dumps(row.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
            content_digest.update(b"\x00")
            content_digest.update(body)
            if content_digest.hexdigest() != row.content_hash:
                issues.append(f"research_dataset {row.id} raw artifact/payload does not match content_hash")
        except (OSError, ValueError) as exc:
            issues.append(f"research_dataset {row.id} raw_ref is missing or unsafe: {exc}")


def _audit_review_originals(dataset: ResearchDataset, data_dir: Path) -> None:
    review = dataset.payload.get("review")
    documents = review.get("documents") if isinstance(review, dict) else None
    if not isinstance(documents, list) or not documents:
        raise ValueError("receipt has no reviewed documents")
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("review document entry is malformed")
        raw_ref, digest = document.get("raw_ref"), document.get("sha256")
        if not isinstance(raw_ref, str) or not _HEX.fullmatch(str(digest)):
            raise ValueError("review document path or SHA-256 is malformed")
        target = _safe_data_path(data_dir, raw_ref)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"reviewed original is missing or SHA-256 mismatched: {raw_ref}")


def _safe_data_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("path is not a safe relative POSIX path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path is absolute or traverses outside data_dir")
    if root.is_symlink():
        raise ValueError("data_dir cannot be a symbolic link")
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("path contains a symbolic link")
    if not target.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise ValueError("path escapes data_dir")
    if not target.is_file():
        raise ValueError("path is not a regular file")
    return target


def _audit_saved_evidence(
    connection: sqlite3.Connection,
    datasets: list[ResearchDataset],
    listings: dict[int, dict[str, Any]],
    issues: list[str],
) -> dict[tuple[str, int], EvidenceRecord]:
    evidence_map: dict[tuple[str, int], EvidenceRecord] = {}
    for row in connection.execute("SELECT id, revision, owner_id, payload FROM investor_evidence"):
        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            record = EvidenceRecord.model_validate(payload)
            if str(record.id) != row["id"] or record.revision != row["revision"]:
                raise ValueError("payload id/revision does not match saved key")
            key = (str(row["id"]), int(row["revision"]))
            valid_record = row["owner_id"] == "local_owner"
            if record.source_kind == "research_dataset":
                ref = record.source_ref
                matched_dataset = next((item for item in datasets if item.id == ref.id), None) if isinstance(ref.id, int) else None
                expected: EvidenceRecord | None = None
                if (
                    matched_dataset is not None
                    and ref.revision == matched_dataset.revision
                    and record.subject_ref.kind == "listing"
                    and isinstance(record.subject_ref.id, int)
                    and record.subject_ref.id in listings
                    and int(listings[record.subject_ref.id]["company_id"]) == matched_dataset.subject_id
                ):
                    rematerialized, _ = material_evidence(
                        matched_dataset,
                        subject_ref=record.subject_ref,
                        created_at=record.created_at,
                    )
                    matches = [item for item in rematerialized if item.content_hash == record.content_hash]
                    expected = matches[0] if len(matches) == 1 else None
                if matched_dataset is None or ref.revision != matched_dataset.revision or expected is None:
                    issues.append(f"saved evidence {row['id']} has an unresolved dataset/fact reference")
                    valid_record = False
                else:
                    mismatched = [
                        field for field in EVIDENCE_FULL_FIELDS
                        if getattr(expected, field) != getattr(record, field)
                    ]
                    if mismatched:
                        issues.append(
                            f"saved evidence {row['id']} does not match its dataset version fields: "
                            + ", ".join(mismatched)
                        )
                        valid_record = False
                    elif expected.content_hash != _material_evidence_hash(
                        matched_dataset,
                        fact_from_payload(_find_fact(matched_dataset, record)),
                    ):
                        issues.append(f"saved evidence {row['id']} fact hash cannot be recomputed")
                        valid_record = False
            elif record.source_ref.kind == "research_dataset":
                issues.append(f"saved evidence {row['id']} dataset source is mislabeled")
                valid_record = False
            if valid_record:
                evidence_map[key] = record
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"saved evidence {row['id']} is malformed: {exc}")
    return evidence_map


def _find_fact(dataset: ResearchDataset, evidence: EvidenceRecord) -> dict[str, Any]:
    for raw in dataset.payload.get("facts", []):
        fact = fact_from_payload(raw)
        if _material_evidence_hash(dataset, fact) == evidence.content_hash:
            if isinstance(raw, dict):
                return raw
            raise ValueError("dataset fact entry is not an object")
    raise ValueError("evidence hash does not identify a dataset fact")


def _audit_calculations(connection: sqlite3.Connection, evidence: dict[tuple[str, int], EvidenceRecord],
                        listings: dict[int, dict[str, Any]], current_reviewed: list[ResearchDataset],
                        issues: list[str]) -> tuple[int, int, dict[int, int]]:
    valid_count = 0
    current_count = 0
    by_company: dict[int, int] = {}
    current_ids = {row.id for row in current_reviewed}
    for row in connection.execute("SELECT id, revision, owner_id, payload FROM investor_calculation"):
        try:
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            record = CalculationRecord.model_validate(payload)
            if str(record.id) != row["id"] or record.revision != row["revision"]:
                raise ValueError("payload id/revision does not match saved key")
            if record.method != MATERIAL_CALCULATION_METHOD:
                continue
            if row["owner_id"] != "local_owner":
                issues.append(f"saved material calculation {row['id']} is outside local_owner scope")
                continue
            if record.status.value != "ok" or not record.applicable or not record.results:
                continue
            refs: list[EvidenceRecord] = []
            malformed = False
            for ref in record.fact_refs:
                saved = evidence.get((str(ref.id), ref.revision)) if ref.kind == "evidence" else None
                if saved is None or saved.verification != "verified":
                    malformed = True
                    break
                refs.append(saved)
            if malformed or not refs:
                issues.append(f"saved calculation {row['id']} has unresolved or non-evidence fact_refs")
                continue
            if record.method == MATERIAL_CALCULATION_METHOD and record.input_hash != material_calculation_input_hash(
                refs, method=record.method, method_version=record.method_version
            ):
                issues.append(f"saved calculation {row['id']} input_hash does not bind its exact fact_refs")
                continue
            subject_ids: set[int] = set()
            listing_ids: set[int] = set()
            for item in refs:
                if item.subject_ref.kind != "listing" or not isinstance(item.subject_ref.id, int):
                    malformed = True
                    break
                listing = listings.get(item.subject_ref.id)
                if listing is None:
                    malformed = True
                    break
                subject_ids.add(int(listing["company_id"]))
                listing_ids.add(item.subject_ref.id)
            if malformed or len(subject_ids) != 1 or len(listing_ids) != 1:
                issues.append(f"saved calculation {row['id']} references facts from inconsistent company subjects")
                continue
            if any(
                item.source_kind != "research_dataset"
                or item.source_ref.kind != "research_dataset"
                or not isinstance(item.source_ref.id, int)
                or item.source_ref.id not in current_ids
                for item in refs
            ):
                # Historical/provider calculations can remain valid records, but
                # they do not satisfy this M1 reviewed-current-version gate.
                valid_count += 1
                continue
            valid_count += 1
            current_count += 1
            company_id = next(iter(subject_ids))
            by_company[company_id] = by_company.get(company_id, 0) + 1
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(f"saved calculation {row['id']} is malformed: {exc}")
    return valid_count, current_count, by_company


def _missing_value_summary(datasets: list[ResearchDataset]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dataset in datasets:
        try:
            missing = [
                {"metric": fact.metric, "period_end": fact.period.end.isoformat(), "reason": fact.value.missing_reason}
                for fact in dataset_facts(dataset) if fact.value.raw_value is None
            ]
            result.extend({"dataset_id": dataset.id, **item} for item in missing)
        except (ValueError, TypeError):
            continue
    return result


def _audit_archives(connection: sqlite3.Connection, datasets: list[ResearchDataset], data_dir: Path,
                    issues: list[str]) -> None:
    by_id = {row.id: row for row in datasets}
    for row in connection.execute("SELECT * FROM research_material_acquisition"):
        try:
            verify_capture(data_dir, row["archive_ref"], row["manifest_sha256"])
            links = json.loads(row["dataset_links"]) if isinstance(row["dataset_links"], str) else row["dataset_links"]
            for link in links:
                dataset = by_id.get(int(link["id"]))
                if dataset is None or dataset.revision != link["revision"] or dataset.content_hash != link["content_hash"]:
                    raise ValueError("acquisition dataset link does not match a saved dataset version")
                if dataset.subject_kind != "company" or dataset.subject_id != row["subject_id"]:
                    raise ValueError("acquisition dataset link has a different company subject")
        except (ArchiveIntegrityError, OSError, ValueError, TypeError, KeyError) as exc:
            issues.append(f"material acquisition {row['id']} archive/link check failed: {exc}")


def _coverage(identities: tuple[Any, ...], company_ids: dict[str, set[int]]) -> list[dict[str, Any]]:
    return [{"company_key": item.company_key, "company_name": item.company_name,
             "database_company_ids": sorted(company_ids.get(item.company_key, set())),
             "covered": bool(company_ids.get(item.company_key))} for item in identities]


def _current_reviewed(
    datasets: list[ResearchDataset], receipts: dict[int, Any]
) -> list[ResearchDataset]:
    """Mirror context selection: keep the newest row per period, then latest receipt."""
    newest: dict[tuple[int, date | None], ResearchDataset] = {}
    for row in datasets:
        key = (row.subject_id, row.period_end)
        previous = newest.get(key)
        if previous is None or (row.revision, row.id) > (previous.revision, previous.id):
            newest[key] = row
    by_company: dict[int, list[ResearchDataset]] = {}
    for row in newest.values():
        by_company.setdefault(row.subject_id, []).append(row)
    current: list[ResearchDataset] = []
    for company_rows in by_company.values():
        latest = max(
            company_rows,
            key=lambda row: (
                receipts[row.id].latest_period_end,
                receipts[row.id].checked_on,
                row.revision,
                row.id,
            ),
        )
        current.append(latest)
    return current


def _enrich_coverage(
    coverage: list[dict[str, Any]], identities: tuple[Any, ...], reviewed: list[ResearchDataset],
    calculations_by_company: dict[int, int],
) -> None:
    for item in coverage:
        company_ids = set(item["database_company_ids"])
        item["reviewed_dataset_ids"] = sorted(
            row.id for row in reviewed if row.subject_id in company_ids
        )
        item["valid_saved_calculation_count"] = sum(
            calculations_by_company.get(company_id, 0) for company_id in company_ids
        )
        item["reviewed"] = bool(item["reviewed_dataset_ids"])
        item["has_saved_calculation"] = item["valid_saved_calculation_count"] > 0


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _migration_heads() -> list[str]:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


def _code_state() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip())
        return {"head": head, "branch": branch or None, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"head": None, "branch": None, "dirty": None}
