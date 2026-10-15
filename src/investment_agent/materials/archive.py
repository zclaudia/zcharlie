"""Content-addressed archives for immutable research-material observations.

An archive contains exact response bytes and a canonical JSON manifest. Both
are written before a database reference is created. The manifest hash is the
deduplication key; it contains evidence timestamps but excludes the current
import/capture clock so replaying the same offline evidence remains idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import ControlledIdentity
from investment_agent.materials.contracts import FactPeriod
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.research_material import ResearchDataset

ARCHIVE_ROOT = Path("research-material") / "archive"
ARCHIVE_SCHEMA_VERSION = 1
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s\]\[<>\"']+", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth(?:orization)?|password|secret|signature|token|key)\b"
    r"\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


class ArchiveIntegrityError(ValueError):
    """An archive file is missing, unsafe, or does not match its digest."""


@dataclass(frozen=True)
class ResponseEvidence:
    """One exact upstream response and its safe acquisition metadata."""

    name: str
    content: bytes
    source_url: str
    media_type: str
    http_status: int
    fetched_at: datetime | None
    parse_eligible: bool = True


@dataclass(frozen=True)
class ArchiveCapture:
    """Inputs and observations from one adapter run."""

    identity: Mapping[str, object]
    source: str
    subject_id: int
    adapter_version: str
    periods: tuple[Mapping[str, object], ...]
    expected_requests: tuple[str, ...]
    responses: tuple[ResponseEvidence, ...]
    problems: tuple[str, ...] = ()
    captured_at: datetime | None = None


@dataclass(frozen=True)
class ArchivedCapture:
    """Stable reference returned after all archive files are durable."""

    manifest_ref: str
    manifest_sha256: str
    captured_at: datetime
    source: str
    subject_id: int
    adapter_version: str
    identity: dict[str, Any]
    periods: tuple[dict[str, Any], ...]
    expected_requests: tuple[str, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True)
class DatasetLink:
    """Immutable snapshot of a saved dataset row linked to an observation."""

    id: int
    revision: int
    content_hash: str


@dataclass(frozen=True)
class VerifiedCapture:
    """Manifest metadata plus all integrity-checked original responses."""

    source: str
    subject_id: int
    adapter_version: str
    identity: dict[str, Any]
    periods: tuple[dict[str, Any], ...]
    expected_requests: tuple[str, ...]
    responses: tuple[ResponseEvidence, ...]
    problems: tuple[str, ...]
    manifest_sha256: str
    manifest_ref: str


def identity_snapshot(identity: ControlledIdentity) -> dict[str, object]:
    """Serialize controlled identity fields without any database or file paths."""

    return cast(dict[str, object], _sanitize_metadata_strings(
        {
            "market_code": identity.market_code,
            "ticker": identity.ticker,
            "company_key": identity.company_key,
            "company_name": identity.company_name,
            "company_name_zh": identity.company_name_zh,
            "security_type": identity.security_type.value,
            "quote_currency": identity.quote_currency,
            "report_currency": identity.report_currency,
            "fiscal_year_end_note": identity.fiscal_year_end_note,
            "sources": [
                {"source": binding.source, "symbol": binding.symbol, "note": binding.note}
                for binding in identity.sources
            ],
            "valid_from": identity.valid_from.isoformat(),
            "valid_to": identity.valid_to.isoformat() if identity.valid_to else None,
            "ads_ratio": identity.ads_ratio,
            "notes": identity.notes,
        }
    ))


def period_snapshot(period: FactPeriod) -> dict[str, object]:
    """Serialize a material period without deriving or rounding its boundaries."""

    return cast(dict[str, object], _sanitize_metadata_strings(
        {
            "start": period.start.isoformat() if period.start else None,
            "end": period.end.isoformat(),
            "basis": period.basis.value,
            "label": period.label,
            "days": period.days,
        }
    ))


def archive_capture(data_dir: Path, capture: ArchiveCapture) -> ArchivedCapture:
    """Validate and atomically archive response bodies and their manifest."""

    if not capture.source or len(capture.source) > 64:
        raise ValueError("source must be a non-empty string of at most 64 characters")
    if not capture.adapter_version or len(capture.adapter_version) > 32:
        raise ValueError("adapter_version must be a non-empty string of at most 32 characters")
    _positive_id(capture.subject_id, "subject_id")
    captured_at = _aware(capture.captured_at or datetime.now(UTC), "captured_at")
    identity = _sanitize_metadata_strings(_json_value(capture.identity))
    periods = tuple(_sanitize_metadata_strings(_json_value(p)) for p in capture.periods)
    if not isinstance(identity, dict) or any(not isinstance(p, dict) for p in periods):
        raise ValueError("identity and period snapshots must be JSON objects")
    expected = tuple(_nonempty_text(n, "expected request name") for n in capture.expected_requests)
    if len(expected) != len(set(expected)):
        raise ValueError("expected request names must be unique")
    responses = tuple(capture.responses)
    names: set[str] = set()
    root = Path(data_dir)
    source_segment = _safe_source(capture.source)
    manifest_responses: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response.content, bytes):
            raise ValueError("response content must be bytes")
        name = _nonempty_text(response.name, "response name")
        if name in names:
            raise ValueError(f"duplicate response name: {name}")
        names.add(name)
        if isinstance(response.http_status, bool) or not 0 <= response.http_status <= 599:
            raise ValueError("http_status must be between 0 and 599")
        if not isinstance(response.parse_eligible, bool):
            raise ValueError("parse_eligible must be a boolean")
        fetched_at = _aware(response.fetched_at, "response fetched_at") if response.fetched_at else None
        digest = _sha256(response.content)
        body_ref = (ARCHIVE_ROOT / source_segment / "bodies" / digest[:2] / f"{digest}.bin").as_posix()
        body_path = _safe_target(root, body_ref)
        _write_content_addressed(body_path, response.content)
        manifest_responses.append(
            {
                "name": name,
                "source_url": sanitize_url(response.source_url),
                "media_type": _nonempty_text(response.media_type, "media_type"),
                "http_status": response.http_status,
                "fetched_at": _datetime_json(fetched_at),
                "parse_eligible": response.parse_eligible
                and (response.http_status == 0 or 200 <= response.http_status < 300),
                "sha256": digest,
                "bytes": len(response.content),
                "path": body_ref,
            }
        )

    manifest = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "identity": identity,
        "source": capture.source,
        "subject_id": capture.subject_id,
        "adapter_version": capture.adapter_version,
        "periods": list(periods),
        "expected_requests": list(expected),
        "responses": manifest_responses,
        "problems": [_redact_text(p) for p in capture.problems],
    }
    manifest_bytes = _canonical_json(manifest)
    manifest_hash = _sha256(manifest_bytes)
    manifest_ref = (
        ARCHIVE_ROOT / source_segment / "manifests" / manifest_hash[:2] / f"{manifest_hash}.json"
    ).as_posix()
    _write_content_addressed(_safe_target(root, manifest_ref), manifest_bytes)
    return ArchivedCapture(
        manifest_ref=manifest_ref,
        manifest_sha256=manifest_hash,
        captured_at=captured_at,
        source=capture.source,
        subject_id=capture.subject_id,
        adapter_version=capture.adapter_version,
        identity=identity,
        periods=periods,
        expected_requests=expected,
        problems=tuple(_redact_text(p) for p in capture.problems),
    )


def verify_capture(
    data_dir: Path,
    manifest_ref: str,
    expected_sha256: str | None = None,
) -> VerifiedCapture:
    """Read an archive safely and verify manifest, body hashes, and lengths."""

    target = _safe_target(Path(data_dir), manifest_ref)
    try:
        manifest_bytes = target.read_bytes()
    except OSError as exc:
        raise ArchiveIntegrityError("archive manifest cannot be read") from exc
    manifest_hash = _sha256(manifest_bytes)
    match = re.fullmatch(r"research-material/archive/[^/]+/manifests/[0-9a-f]{2}/([0-9a-f]{64})\.json", manifest_ref)
    if match is None or match.group(1) != manifest_hash:
        raise ArchiveIntegrityError("archive manifest path or SHA-256 does not match")
    if expected_sha256 is not None and (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or expected_sha256 != manifest_hash
    ):
        raise ArchiveIntegrityError("archive manifest does not match the trusted SHA-256")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveIntegrityError("archive manifest is not valid JSON") from exc
    schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != ARCHIVE_SCHEMA_VERSION
    ):
        raise ArchiveIntegrityError("unsupported archive manifest schema")
    try:
        source = _nonempty_text(manifest["source"], "manifest source")
        if PurePosixPath(manifest_ref).parts[2] != _safe_source(source):
            raise ValueError("manifest path source does not match its source field")
        subject_id = manifest["subject_id"]
        _positive_id(subject_id, "manifest subject_id")
        adapter_version = _nonempty_text(manifest["adapter_version"], "manifest adapter_version")
        identity = manifest["identity"]
        periods = manifest["periods"]
        expected = manifest["expected_requests"]
        problems = manifest["problems"]
        raw_responses = manifest["responses"]
        if (
            not isinstance(identity, dict)
            or not isinstance(periods, list)
            or not all(isinstance(p, dict) for p in periods)
        ):
            raise ValueError("invalid identity or periods")
        if (
            not isinstance(expected, list)
            or not all(isinstance(n, str) for n in expected)
            or not isinstance(problems, list)
            or not all(isinstance(p, str) for p in problems)
        ):
            raise ValueError("invalid expected requests or problems")
        if not isinstance(raw_responses, list):
            raise ValueError("invalid responses")
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveIntegrityError("archive manifest has an invalid shape") from exc

    responses: list[ResponseEvidence] = []
    seen: set[str] = set()
    for raw in raw_responses:
        try:
            name = _nonempty_text(raw["name"], "response name")
            if name in seen:
                raise ValueError("duplicate response")
            seen.add(name)
            ref = raw["path"]
            digest = raw["sha256"]
            byte_count = raw["bytes"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid body digest")
            expected_body_ref = (
                ARCHIVE_ROOT / _safe_source(source) / "bodies" / digest[:2] / f"{digest}.bin"
            ).as_posix()
            if ref != expected_body_ref:
                raise ValueError("body path does not match its source and content hash")
            if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
                raise ValueError("invalid body length")
            body = _safe_target(Path(data_dir), ref).read_bytes()
            if len(body) != byte_count or _sha256(body) != digest:
                raise ArchiveIntegrityError(f"response body integrity check failed: {name}")
            fetched_raw = raw["fetched_at"]
            fetched_at = _datetime_from_json(fetched_raw) if fetched_raw is not None else None
            status = raw["http_status"]
            if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 599:
                raise ValueError("invalid HTTP status")
            source_url = raw["source_url"]
            parse_eligible = raw["parse_eligible"]
            if not isinstance(parse_eligible, bool):
                raise ValueError("invalid parse_eligible flag")
            if status != 0 and not 200 <= status < 300 and parse_eligible:
                raise ValueError("non-success response cannot be parse eligible")
            media_type = _nonempty_text(raw["media_type"], "media_type")
            if not isinstance(source_url, str) or sanitize_url(source_url) != source_url:
                raise ValueError("source URL is not sanitized")
        except ArchiveIntegrityError:
            raise
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise ArchiveIntegrityError("archive response entry is invalid or unreadable") from exc
        responses.append(
            ResponseEvidence(name, body, source_url, media_type, status, fetched_at, parse_eligible)
        )
    return VerifiedCapture(
        source=source,
        subject_id=subject_id,
        adapter_version=adapter_version,
        identity=identity,
        periods=tuple(periods),
        expected_requests=tuple(expected),
        responses=tuple(responses),
        problems=tuple(problems),
        manifest_sha256=manifest_hash,
        manifest_ref=manifest_ref,
    )


async def record_acquisition(
    session: AsyncSession,
    archived: ArchivedCapture,
    *,
    datasets: Sequence[DatasetLink] = (),
) -> ResearchMaterialAcquisition:
    """Append or reuse an acquisition observation in the caller's transaction.

    This method never commits. Identical archive evidence resolves to the first
    observation even if a later offline import runs at a different wall time.
    """

    if len(archived.manifest_sha256) != 64 or not re.fullmatch(r"[0-9a-f]{64}", archived.manifest_sha256):
        raise ValueError("manifest_sha256 must be a lowercase SHA-256 digest")
    _positive_id(archived.subject_id, "subject_id")
    links = await _validate_dataset_links(session, archived, datasets)
    existing = await session.scalar(
        select(ResearchMaterialAcquisition).where(
            ResearchMaterialAcquisition.manifest_sha256 == archived.manifest_sha256
        )
    )
    if existing is not None:
        if existing.archive_ref != archived.manifest_ref:
            raise ArchiveIntegrityError("manifest hash already refers to a different archive path")
        if existing.subject_id != archived.subject_id or existing.source != archived.source:
            raise ArchiveIntegrityError("manifest hash already refers to a different acquisition identity")
        if existing.dataset_links != links:
            raise ValueError("the same acquisition cannot be linked to a different dataset set")
        return existing
    row = ResearchMaterialAcquisition(
        source=archived.source,
        subject_id=archived.subject_id,
        adapter_version=archived.adapter_version,
        captured_at=_aware(archived.captured_at, "captured_at"),
        schema_version=ARCHIVE_SCHEMA_VERSION,
        identity_snapshot=archived.identity,
        periods=list(archived.periods),
        expected_requests=list(archived.expected_requests),
        problems=list(archived.problems),
        archive_ref=archived.manifest_ref,
        manifest_sha256=archived.manifest_sha256,
        dataset_links=links,
    )
    session.add(row)
    await session.flush()
    return row


async def _validate_dataset_links(
    session: AsyncSession,
    archived: ArchivedCapture,
    datasets: Sequence[DatasetLink],
) -> list[dict[str, Any]]:
    allowed_sources = {archived.source}
    if archived.source == "sec.companyfacts.v1":
        allowed_sources.add("sec.submissions.v1")
    normalized: dict[int, DatasetLink] = {}
    for link in datasets:
        if (
            isinstance(link.id, bool)
            or link.id <= 0
            or isinstance(link.revision, bool)
            or link.revision <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", link.content_hash)
        ):
            raise ValueError("dataset links require positive IDs/revisions and SHA-256 hashes")
        previous = normalized.get(link.id)
        if previous is not None and previous != link:
            raise ValueError(f"dataset {link.id} is linked with conflicting version details")
        normalized[link.id] = link
    if not normalized:
        return []
    rows = (
        await session.execute(
            select(ResearchDataset).where(ResearchDataset.id.in_(tuple(normalized)))
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    output: list[dict[str, Any]] = []
    for link in sorted(normalized.values(), key=lambda item: item.id):
        row = by_id.get(link.id)
        if row is None:
            raise ValueError(f"dataset {link.id} does not exist")
        if row.subject_kind != "company":
            raise ValueError(f"dataset {link.id} is not a company dataset")
        if row.subject_id != archived.subject_id:
            raise ValueError(f"dataset {link.id} belongs to a different subject")
        if row.source not in allowed_sources:
            raise ValueError(f"dataset {link.id} source {row.source!r} is unrelated to {archived.source!r}")
        if row.adapter_version != archived.adapter_version:
            raise ValueError(f"dataset {link.id} uses a different adapter version")
        if row.revision != link.revision or row.content_hash != link.content_hash:
            raise ValueError(f"dataset {link.id} version details do not match the saved row")
        output.append({"id": link.id, "revision": link.revision, "content_hash": link.content_hash})
    return output


def sanitize_url(url: str) -> str:
    """Remove URL credentials, fragments, and sensitive query parameter values."""

    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return "[invalid-url]"
        hostname = parts.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = f":{parts.port}" if parts.port is not None else ""
        netloc = hostname + port
        query = urlencode(
            [(key, "[REDACTED]" if _is_sensitive_query_key(key) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        )
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))
    except (TypeError, ValueError):
        return "[invalid-url]"


def _safe_target(data_dir: Path, relative: str) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise ArchiveIntegrityError("archive path is not a safe relative POSIX path")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveIntegrityError("archive path escapes the data directory")
    if path.parts[:2] != ARCHIVE_ROOT.parts:
        raise ArchiveIntegrityError("archive path is outside research-material/archive")
    root = Path(data_dir).absolute()
    target = root.joinpath(*path.parts)
    current = root
    try:
        if root.is_symlink():
            raise ArchiveIntegrityError("data directory cannot be a symlink")
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ArchiveIntegrityError("archive path contains a symbolic link")
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ArchiveIntegrityError:
        raise
    except (OSError, ValueError) as exc:
        raise ArchiveIntegrityError("archive path escapes the data directory") from exc
    return target


def _write_content_addressed(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise ArchiveIntegrityError("existing archive content cannot be read") from exc
        if existing != content:
            raise ArchiveIntegrityError("existing content-addressed archive file is corrupt")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ArchiveIntegrityError(
                    "concurrent archive file has unexpected content"
                ) from None
        _fsync_directory(target.parent)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("non-finite floats are not valid archive metadata")
        return value
    if isinstance(value, datetime):
        return _datetime_json(_aware(value, "archive timestamp"))
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("archive object keys must be strings")
        return {k: _json_value(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()  # dates and enums with simple ISO representations
    raise ValueError(f"unsupported archive metadata type: {type(value).__name__}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_json(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z") if value else None


def _datetime_from_json(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware(parsed, "manifest timestamp")


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_id(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _safe_source(source: str) -> str:
    segment = _SAFE_SEGMENT.sub("_", source).strip("._")
    if not segment or segment != source:
        raise ValueError("source must contain only letters, digits, dot, underscore, or hyphen")
    return segment


def _redact_text(value: str) -> str:
    urls: list[str] = []

    def hold_url(match: re.Match[str]) -> str:
        urls.append(sanitize_url(match.group(0).rstrip(".,)")))
        return f"\x00ARCHIVE_URL_{len(urls) - 1}\x00"

    safe = _URL_IN_TEXT.sub(hold_url, value)
    safe = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", safe)
    safe = _BEARER_SECRET.sub("Bearer [REDACTED]", safe)
    for index, url in enumerate(urls):
        safe = safe.replace(f"\x00ARCHIVE_URL_{index}\x00", url)
    return safe


def sanitize_diagnostic(text: str) -> str:
    """Redact secrets and sensitive URL components from human-readable diagnostics."""

    return _redact_text(text)


def _sanitize_metadata_strings(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_sanitize_metadata_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_metadata_strings(item) for key, item in value.items()}
    return value


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability barrier for the directory entry created by link()."""

    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized == "key" or any(
        token in normalized
        for token in (
            "apikey", "accesskey", "token", "authorization", "bearer", "credential",
            "csrf", "cookie", "password", "passwd", "secret", "session", "signature", "privatekey",
        )
    ) or normalized == "sig"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
