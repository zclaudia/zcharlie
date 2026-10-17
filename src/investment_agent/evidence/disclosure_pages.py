"""Narrow, typed CNInfo page protocol for bounded P4 background polling.

This adapter deliberately has no production protocol defaults: source fields,
ordering and pagination semantics must be frozen from an independently reviewed
S0 snapshot before a poll may be admitted. Tests may inject a protocol through
the explicit ``for_tests`` constructor only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    MAX_BYTES,
    QUERY,
    DisclosureError,
    OfficialHTTPS,
    SourceResponse,
    validate_url,
)
from investment_agent.investor.common import Contract, ObjectRef

SOURCE: Literal["cninfo.v1"] = "cninfo.v1"
PAGE_SIZE_MAX = 20
MAX_WINDOW_DAYS = 31


class CNInfoPageProtocolV1(Contract):
    """S0-frozen source field map; ``validated`` is false by default."""

    schema_version: Literal[1] = 1
    validated: bool = False
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_sort_name: str
    query_sort_type: Literal["desc"]
    stable_tie_break_order: Literal["asc", "desc"]
    native_id_field: str
    issuer_code_field: str
    issuer_id_field: str
    published_millis_field: str
    title_field: str
    locator_field: str
    document_type_field: str
    page_number_field: str | None
    page_size_field: str | None
    has_more_field: str
    total_field: str | None
    stable_tie_break_field: str
    annual_full_text_type_values: tuple[str, ...] = Field(min_length=1, max_length=32)
    correction_type_values: tuple[str, ...] = Field(min_length=1, max_length=32)
    supplement_type_values: tuple[str, ...] = Field(min_length=1, max_length=32)
    excluded_type_values: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def fields_are_distinct(self) -> CNInfoPageProtocolV1:
        names = [
            self.native_id_field,
            self.issuer_code_field,
            self.issuer_id_field,
            self.published_millis_field,
            self.title_field,
            self.locator_field,
            self.document_type_field,
            self.has_more_field,
        ]
        if any(not name or len(name) > 80 for name in names):
            raise ValueError("CNInfo protocol field names must be bounded")
        if len(set(names)) != len(names):
            raise ValueError("CNInfo protocol fields must be distinct")
        if not self.stable_tie_break_field or len(self.stable_tie_break_field) > 80:
            raise ValueError("CNInfo stable tie-break field must be bounded")
        categories = (
            *self.annual_full_text_type_values,
            *self.correction_type_values,
            *self.supplement_type_values,
            *self.excluded_type_values,
        )
        if any(not isinstance(value, str) or not value or len(value) > 128 for value in categories):
            raise ValueError("CNInfo document type values must be bounded strings")
        if len(set(categories)) != len(categories):
            raise ValueError("CNInfo document type allowlists must be disjoint")
        return self


class DisclosurePageRequestV1(Contract):
    listing_id: int = Field(ge=1)
    source: Literal["cninfo.v1"] = SOURCE
    authorization_id: UUID
    authorization_revision: int = Field(ge=1)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_ref: ObjectRef
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_start: AwareDatetime
    window_end: AwareDatetime
    page_num: int = Field(ge=1)
    page_size: int = Field(default=20, ge=1, le=PAGE_SIZE_MAX)
    max_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=2 * 1024 * 1024)
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bounded_window(self) -> DisclosurePageRequestV1:
        if (
            self.listing_ref.kind != "listing"
            or self.listing_ref.id != self.listing_id
            or self.listing_ref.revision != 1
        ):
            raise ValueError("page request must bind its exact listing")
        if self.window_start >= self.window_end:
            raise ValueError("disclosure window must be ordered")
        if self.window_end - self.window_start > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError("disclosure page window exceeds 31 days")
        return self


class DisclosureBackgroundFetchV1(Contract):
    listing_id: int = Field(ge=1)
    source: Literal["cninfo.v1"] = SOURCE
    authorization_id: UUID
    authorization_revision: int = Field(ge=1)
    authorization_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    listing_ref: ObjectRef
    window_start: AwareDatetime
    window_end: AwareDatetime
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    native_announcement_id: str = Field(pattern=r"^\d{1,32}$")
    locator: str = Field(min_length=1, max_length=2048)
    max_bytes: int = Field(default=MAX_BYTES, ge=1024, le=MAX_BYTES)
    protocol_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_listing(self) -> DisclosureBackgroundFetchV1:
        if (
            self.listing_ref.kind != "listing"
            or self.listing_ref.id != self.listing_id
            or self.listing_ref.revision != 1
        ):
            raise ValueError("fetch request must bind its exact listing")
        if self.window_end <= self.window_start:
            raise ValueError("fetch window must be positive")
        try:
            validate_url(self.locator, "original")
        except DisclosureError as exc:
            raise ValueError("fetch locator must be an exact allowed HTTPS original URL") from exc
        return self


class CNInfoDiscoveredDocument(Contract):
    native_announcement_id: str
    ticker: str
    issuer_id: str
    published_at: AwareDatetime
    title: str
    locator: str
    source_type: str | None
    published_raw_millis: int
    stable_tie_break_value: str
    published_precision: Literal["millisecond"] = "millisecond"
    classification: Literal[
        "annual_report_full",
        "annual_report_correction",
        "annual_report_supplement",
        "unknown",
        "excluded",
    ]
    identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CNInfoPage(Contract):
    request: DisclosurePageRequestV1
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_bytes: int = Field(ge=1)
    page_num: int = Field(ge=1)
    has_more: bool
    total: int | None = Field(default=None, ge=0)
    returned_count: int = Field(ge=0, le=PAGE_SIZE_MAX)
    documents: tuple[CNInfoDiscoveredDocument, ...] = Field(max_length=PAGE_SIZE_MAX)
    acquired_at: AwareDatetime
    final_url: str


def json_material(response: SourceResponse) -> dict[str, Any]:
    if response.headers.get("content-type", "").split(";", 1)[0].lower() not in {
        "application/json",
        "text/json",
    }:
        raise DisclosureError("invalid_json_mime")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DisclosureError("invalid_source_json") from exc
    if not isinstance(value, dict):
        raise DisclosureError("invalid_source_schema")
    return value


class DisclosurePageService:
    """Bounded page request/parser. Authorization and durable attempt state live above."""

    def __init__(self, *, registry: Any | None = None) -> None:
        from investment_agent.evidence.disclosure_profiles import SERVER_PROFILE_REGISTRY

        self.protocol: CNInfoPageProtocolV1 | None = None
        self.http = OfficialHTTPS()
        # A default registry is deliberately empty. Live protocol/profile values
        # are loaded only by trusted server bootstrap code.
        self.registry = registry if registry is not None else SERVER_PROFILE_REGISTRY
        self._testing = False
        self._clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @classmethod
    def for_tests(
        cls,
        *,
        protocol: CNInfoPageProtocolV1,
        http: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> DisclosurePageService:
        service = cls()
        service.protocol = protocol
        service.http = http
        service._testing = True
        if clock is not None:
            service._clock = clock
        return service

    def _protocol(
        self,
        request: DisclosurePageRequestV1 | DisclosureBackgroundFetchV1,
        *,
        purpose: Literal["query", "original"],
    ) -> CNInfoPageProtocolV1:
        protocol: CNInfoPageProtocolV1 | None
        if not self._testing:
            from investment_agent.evidence.disclosure_profiles import SERVER_PROFILE_REGISTRY

            if self.registry is not SERVER_PROFILE_REGISTRY:
                raise DisclosureError("non_authoritative_profile_registry")
            entry = self.registry.require_approved_contract(
                request.contract_hash,
                now=self._clock(),
                protocol_hash=request.protocol_hash,
            )
            protocol = entry.profile.protocol
            if purpose not in entry.approval.purpose_scopes:
                raise DisclosureError("source_profile_purpose_unapproved")
        else:
            protocol = self.protocol
            if protocol is None:
                raise DisclosureError("source_protocol_unvalidated")
        if protocol is None or protocol.protocol_hash != request.protocol_hash:
            raise DisclosureError("source_protocol_unvalidated")
        if (
            request.contract_hash == "0" * 64
            or request.attempt_scope_hash == "0" * 64
            or request.source_scope_hash == "0" * 64
        ):
            raise DisclosureError("source_contract_unbound")
        return protocol

    def require_request_approval(
        self,
        request: DisclosurePageRequestV1 | DisclosureBackgroundFetchV1,
        *,
        purpose: Literal["query", "original"],
    ) -> CNInfoPageProtocolV1:
        """Re-read the current profile registry before reads and post-hop processing."""
        return self._protocol(request, purpose=purpose)

    async def request_one_hop(
        self,
        url: str,
        purpose: Literal["directory", "query", "original"],
        *,
        body: bytes | None = None,
        max_bytes: int,
    ) -> SourceResponse:
        """Issue exactly one HTTP request; redirects are returned for next-step authorization."""
        validate_url(url, purpose)
        request_hop = getattr(self.http, "request_hop", None)
        if request_hop is not None:
            return cast(
                SourceResponse,
                await request_hop(url, purpose, body=body, max_bytes=max_bytes),
            )
        # Test transports are explicitly supplied through for_tests. Production
        # OfficialHTTPS has a single-hop entry point and never hides redirect IO.
        if isinstance(self.http, OfficialHTTPS):
            return await self.http.request_one_hop(
                url,
                purpose,
                body=body,
                max_bytes=max_bytes,
            )
        raise DisclosureError("single_hop_transport_required")

    async def directory(self, *, max_bytes: int = 8 * 1024 * 1024) -> SourceResponse:
        return await self.request_one_hop(DIRECTORY, "directory", max_bytes=max_bytes)

    @staticmethod
    def issuer_from_directory(response: SourceResponse, ticker: str) -> str:
        if response.status != 200:
            raise DisclosureError(f"http_status_{response.status}")
        directory = json_material(response).get("stockList")
        if not isinstance(directory, list):
            raise DisclosureError("invalid_identity_directory")
        matches = [row for row in directory if isinstance(row, dict) and row.get("code") == ticker]
        if len(matches) != 1:
            raise DisclosureError("ambiguous_or_missing_official_issuer")
        issuer = matches[0].get("orgId")
        if not isinstance(issuer, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", issuer):
            raise DisclosureError("ambiguous_or_missing_official_issuer")
        return issuer

    @staticmethod
    def next_redirect_url(
        response: SourceResponse,
        purpose: Literal["directory", "query", "original"],
        *,
        redirect_count: int,
        body_present: bool = False,
    ) -> str | None:
        """Return one already-validated redirect target for a separately authorized hop."""
        if response.status not in {301, 302, 303, 307, 308}:
            return None
        if redirect_count >= 2:
            raise DisclosureError("redirect_limit")
        if body_present and response.status not in {307, 308}:
            raise DisclosureError("redirect_limit_or_method_change")
        location = response.headers.get("location")
        if not isinstance(location, str) or not location:
            raise DisclosureError("invalid_redirect_location")
        from urllib.parse import urljoin

        target = urljoin(response.url, location)
        validate_url(target, purpose)
        return target

    async def page(
        self, request: DisclosurePageRequestV1, *, issuer_id: str, ticker: str
    ) -> CNInfoPage:
        protocol = self._protocol(request, purpose="query")
        url, purpose, body = page_url_body(
            request,
            issuer_id=issuer_id,
            ticker=ticker,
            protocol=protocol,
        )
        response = await self.request_one_hop(
            url,
            purpose,
            body=body,
            max_bytes=request.max_bytes,
        )
        # A response received after profile revocation remains quarantined and
        # cannot be parsed into normal page or coverage state.
        current_protocol = self._protocol(request, purpose="query")
        if current_protocol.protocol_hash != protocol.protocol_hash:
            raise DisclosureError("source_profile_changed_during_hop")
        return parse_page_response(
            request,
            response,
            issuer_id=issuer_id,
            ticker=ticker,
            protocol=protocol,
            now=self._clock(),
        )


def page_url_body(
    request: DisclosurePageRequestV1,
    *,
    issuer_id: str,
    ticker: str,
    protocol: CNInfoPageProtocolV1,
) -> tuple[str, Literal["query"], bytes]:
    if not re.fullmatch(r"\d{6}", ticker) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", issuer_id):
        raise DisclosureError("unsupported_or_missing_listing")
    start_day = request.window_start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    end_day = request.window_end.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    params = {
        "pageNum": str(request.page_num),
        "pageSize": str(request.page_size),
        "column": "szse",
        "tabName": "fulltext",
        "stock": f"{ticker},{issuer_id}",
        "seDate": f"{start_day}~{end_day}",
        "sortName": protocol.query_sort_name,
        "sortType": protocol.query_sort_type,
        "plate": "",
        "searchkey": "",
        "secid": "",
        "category": "",
        "trade": "",
        "isHLtitle": "false",
    }
    return QUERY, "query", urlencode(params).encode()


def parse_page_response(
    request: DisclosurePageRequestV1,
    response: SourceResponse,
    *,
    issuer_id: str,
    ticker: str,
    protocol: CNInfoPageProtocolV1,
    now: datetime | None = None,
) -> CNInfoPage:
    """Pure parser: caller stores the raw response before committing this page."""
    if response.status != 200:
        raise DisclosureError(f"http_status_{response.status}")
    material = json_material(response)
    rows = material.get("announcements")
    if not isinstance(rows, list) or len(rows) > request.page_size:
        raise DisclosureError("invalid_discovery_schema_or_count")
    raw_page_num = material.get(protocol.page_number_field) if protocol.page_number_field else None
    raw_page_size = material.get(protocol.page_size_field) if protocol.page_size_field else None
    if (
        protocol.page_number_field is not None
        and (type(raw_page_num) is not int or raw_page_num != request.page_num)
    ) or (
        protocol.page_size_field is not None
        and (type(raw_page_size) is not int or raw_page_size != request.page_size)
    ):
        raise DisclosureError("source_page_number_mismatch")
    more = material.get(protocol.has_more_field)
    if not isinstance(more, bool):
        raise DisclosureError("source_has_more_unknown")
    total = material.get(protocol.total_field) if protocol.total_field else None
    if total is not None and (type(total) is not int or total < 0):
        raise DisclosureError("invalid_source_total")
    import hashlib

    from investment_agent.evidence.snapshots import digest

    parsed_now = (now or datetime.now(UTC)).astimezone(UTC)
    documents: list[CNInfoDiscoveredDocument] = []
    sort_keys: list[tuple[int, str]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise DisclosureError("invalid_discovery_schema")
        native_id = row.get(protocol.native_id_field)
        code = row.get(protocol.issuer_code_field)
        issuer = row.get(protocol.issuer_id_field)
        millis = row.get(protocol.published_millis_field)
        title = row.get(protocol.title_field)
        locator_part = row.get(protocol.locator_field)
        if (
            not isinstance(native_id, str)
            or not re.fullmatch(r"\d{1,32}", native_id)
            or not isinstance(code, str)
            or code != ticker
            or not isinstance(issuer, str)
            or issuer != issuer_id
            or type(millis) is not int
            or millis < 0
            or not isinstance(title, str)
            or len(title) > 2000
            or not isinstance(locator_part, str)
            or not locator_part
        ):
            raise DisclosureError("source_identity_or_schema_mismatch")
        if native_id in seen_ids:
            raise DisclosureError("duplicate_native_identity_in_page")
        seen_ids.add(native_id)
        try:
            published = datetime.fromtimestamp(millis / 1000, UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise DisclosureError("invalid_publication_time") from exc
        tie = row.get(protocol.stable_tie_break_field)
        if not isinstance(tie, str) or not tie or len(tie) > 128:
            raise DisclosureError("source_stable_tiebreak_unknown")
        sort_keys.append((millis, tie))
        if not request.window_start <= published <= request.window_end:
            continue
        if locator_part.startswith("https://"):
            url = locator_part
        else:
            if not locator_part.startswith("/finalpage/"):
                locator_part = "/" + locator_part.lstrip("/")
            url = "https://static.cninfo.com.cn" + locator_part
        validate_url(url, "original")
        raw_type = row.get(protocol.document_type_field)
        source_type = raw_type if isinstance(raw_type, str) else None
        classification: Literal[
            "annual_report_full",
            "annual_report_correction",
            "annual_report_supplement",
            "unknown",
            "excluded",
        ]
        if source_type in protocol.annual_full_text_type_values:
            classification = "annual_report_full"
        elif source_type in protocol.correction_type_values:
            classification = "annual_report_correction"
        elif source_type in protocol.supplement_type_values:
            classification = "annual_report_supplement"
        elif source_type in protocol.excluded_type_values:
            classification = "excluded"
        else:
            classification = "unknown"
        if classification != "unknown" and urlsplit(url).path.lower().endswith(".html"):
            classification = "excluded"
        documents.append(
            CNInfoDiscoveredDocument(
                native_announcement_id=native_id,
                ticker=ticker,
                issuer_id=issuer,
                published_at=published,
                title=title,
                locator=url,
                source_type=source_type,
                published_raw_millis=millis,
                stable_tie_break_value=tie,
                classification=classification,
                identity_hash=digest(
                    {
                        "source": SOURCE,
                        "listing_id": request.listing_id,
                        "ticker": ticker,
                        "issuer_id": issuer_id,
                        "native_announcement_id": native_id,
                    }
                ),
            )
        )

    def ordered_before(left: tuple[int, str], right: tuple[int, str]) -> bool:
        if left[0] != right[0]:
            return left[0] > right[0]
        return (
            left[1] > right[1] if protocol.stable_tie_break_order == "desc" else left[1] < right[1]
        )

    if any(
        not ordered_before(left, right)
        for left, right in zip(sort_keys, sort_keys[1:], strict=False)
    ):
        raise DisclosureError("source_page_order_drift")
    return CNInfoPage(
        request=request,
        response_hash=hashlib.sha256(response.body).hexdigest(),
        response_bytes=len(response.body),
        page_num=request.page_num,
        has_more=more,
        total=total,
        returned_count=len(rows),
        documents=tuple(documents),
        acquired_at=parsed_now,
        final_url=response.url,
    )


def validate_pdf(body: bytes, mime: str) -> str:
    """Validate the B source's PDF-only rule and return its SHA-256 hash."""
    if mime.split(";", 1)[0].strip().lower() != "application/pdf":
        raise DisclosureError("unsupported_original_type")
    if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-4096:]:
        raise DisclosureError("invalid_original_mime_or_signature")
    import hashlib

    return hashlib.sha256(body).hexdigest()
