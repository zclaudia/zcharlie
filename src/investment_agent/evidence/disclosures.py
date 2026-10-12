"""Bounded CNInfo discovery and immutable originals, never verified financial facts."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    MAX_BYTES,
    QUERY,
    DisclosureError,
    OfficialHTTPS,
    SourceResponse,
    validate_url,
)
from investment_agent.evidence.snapshots import digest
from investment_agent.investor.common import Contract, ObjectRef, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
    DisclosureReceipt,
)
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Listing

SOURCE: Literal["cninfo.v1"] = "cninfo.v1"


class DisclosureAccess(Contract):
    listing_id: int = Field(ge=1)
    source: Literal["cninfo.v1"] = "cninfo.v1"
    max_discoveries: int = Field(default=1, ge=0, le=3)
    max_fetches: int = Field(default=3, ge=0, le=10)
    max_parses: int = Field(default=2, ge=0, le=3)
    max_bytes_per_response: int = Field(default=16 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024)


class DisclosurePermission(DisclosureAccess):
    cutoff: datetime


class DiscoveryRequest(Contract):
    listing_id: int = Field(ge=1)
    source: Literal["cninfo.v1"] = SOURCE
    start: AwareDatetime
    as_of: AwareDatetime
    limit: int = Field(default=10, ge=1, le=20)
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=MAX_BYTES)

    @model_validator(mode="after")
    def bounded_dates(self) -> "DiscoveryRequest":
        if self.start > self.as_of or (self.as_of - self.start).days > 366:
            raise ValueError("discovery window must be ordered and at most 366 days")
        return self


class FetchRequest(Contract):
    listing_id: int = Field(ge=1)
    source: Literal["cninfo.v1"] = SOURCE
    item_id: UUID
    as_of: AwareDatetime
    max_bytes: int = Field(default=MAX_BYTES, ge=1024, le=MAX_BYTES)


class DisclosureHTTP(Protocol):
    async def request(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int = MAX_BYTES,
        timeout_seconds: float = 15,
    ) -> SourceResponse: ...


def json_material(response: SourceResponse) -> dict[str, Any]:
    if response.headers.get("content-type", "").split(";")[0].lower() not in {
        "application/json",
        "text/json",
    }:
        raise DisclosureError("invalid_json_mime")
    try:
        material = json.loads(response.body)
    except (ValueError, UnicodeError) as exc:
        raise DisclosureError("invalid_source_json") from exc
    if not isinstance(material, dict):
        raise DisclosureError("invalid_source_schema")
    return material


def original_mime(response: SourceResponse) -> str:
    mime = response.headers.get("content-type", "").split(";")[0].lower().strip()
    if (
        mime == "application/pdf"
        and response.body.startswith(b"%PDF-")
        and b"%%EOF" in response.body[-4096:]
    ):
        return mime
    if mime == "text/html" and re.match(
        rb"\s*(?:<!doctype\s+html|<html(?:\s|>))", response.body[:1024], re.I
    ):
        # Preserve bytes including scripts; readback is attachment + CSP sandbox,
        # never rendered/executed on the application origin.
        return mime
    raise DisclosureError("invalid_original_mime_or_signature")


def fetch_result(
    original: dict[str, Any], request: FetchRequest, *, reused: bool
) -> dict[str, Any]:
    readable_after = max(
        datetime.fromisoformat(original[field])
        for field in ("first_published_at", "acquired_at", "available_from")
    )
    visible = readable_after <= request.as_of
    return {
        "status": "ok",
        "original": original,
        "reused": reused,
        "verification": "unverified",
        "duplicate_hash_is_not_independent_corroboration": reused,
        "evidence_availability": {
            "status": "ok" if visible else "missing",
            "verification": "candidate",
            "reason": "known_at_requested_cutoff" if visible else "acquired_after_requested_cutoff",
            "requested_cutoff": request.as_of.isoformat(),
            "acquired_at": original["acquired_at"],
            "readable_after": readable_after.isoformat(),
            "requires_new_message": not visible,
            "policy": "Unparsed original only. This task cutoff stays fixed; use a new authenticated message with a later cutoff for parsing/evidence access, or explicit historical reconstruction.",
        },
    }


class DisclosureService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        self.session, self.owner = session, owner
        self.http: DisclosureHTTP = OfficialHTTPS()
        self.active_request: tuple[UUID, Contract] | None = None

    @classmethod
    def for_tests(
        cls, session: AsyncSession, http: DisclosureHTTP, owner: str = "local_owner"
    ) -> "DisclosureService":
        service = cls(session, owner)
        service.http = http
        return service

    async def _listing(self, listing_id: int) -> Listing:
        listing = await self.session.get(Listing, listing_id)
        # Minimal actual supported adapter: Shenzhen A-shares on CNInfo only.
        # Directory has Shanghai rows too; never infer its exchange from a code.
        if (
            listing is None
            or listing.market_code != "SZSE"
            or not re.fullmatch(r"\d{6}", listing.ticker)
        ):
            raise DisclosureError("unsupported_or_missing_listing")
        return listing

    async def replay(self, operation: UUID, request: Contract) -> dict[str, Any] | None:
        """Public idempotent-replay lookup: the saved receipt for ``operation``,
        or ``None``; mismatched owners or requests raise ``idempotency_mismatch``.

        Routes call this (never a private helper) to honour a saved receipt even
        when the feature has since been switched off."""
        receipt = await self.session.get(DisclosureReceipt, str(operation))
        if receipt:
            if receipt.owner_id != self.owner or receipt.request_hash != digest(
                request.model_dump(mode="json")
            ):
                raise InvestorError(
                    "idempotency_mismatch", "Disclosure operation differs from its saved request"
                )
            return receipt.payload
        return None

    async def locked_replay(self) -> dict[str, Any] | None:
        # SQLite lazy BEGIN starts at DML, after all bounded network I/O. This
        # reservation serializes independent processes before dedup reads.
        await self.session.execute(
            update(DisclosureReceipt)
            .where(DisclosureReceipt.operation_id == "")
            .values(owner_id=self.owner)
        )
        if self.active_request is not None:
            return await self.replay(*self.active_request)
        return None

    async def execute(
        self, operation: UUID, request: DiscoveryRequest | FetchRequest
    ) -> dict[str, Any]:
        replay = await self.replay(operation, request)
        if replay is not None:
            return replay
        self.active_request = (operation, request)
        try:
            result = (
                await self.discover(request)
                if isinstance(request, DiscoveryRequest)
                else await self.fetch(request)
            )
        except DisclosureError as exc:
            result = {
                "status": "error",
                "reason": str(exc),
                "source": SOURCE,
                "verification": "unverified",
            }
        finally:
            self.active_request = None
        await self.session.execute(
            insert(DisclosureReceipt)
            .values(
                operation_id=str(operation),
                owner_id=self.owner,
                request_hash=digest(request.model_dump(mode="json")),
                payload=result,
            )
            .on_conflict_do_nothing()
        )
        replay = await self.replay(operation, request)
        assert replay is not None
        return replay

    async def discover(self, request: DiscoveryRequest) -> dict[str, Any]:
        listing = await self._listing(request.listing_id)
        directory_response = await self.http.request(
            DIRECTORY, "directory", max_bytes=request.max_bytes
        )
        if len(directory_response.body) > request.max_bytes:
            raise DisclosureError("response_too_large")
        directory = json_material(directory_response).get("stockList")
        if not isinstance(directory, list):
            raise DisclosureError("invalid_identity_directory")
        matches = [
            row for row in directory if isinstance(row, dict) and row.get("code") == listing.ticker
        ]
        # Do not collapse duplicate/ambiguous mappings or match a company name.
        if len(matches) != 1 or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", str(matches[0].get("orgId", ""))
        ):
            raise DisclosureError("ambiguous_or_missing_official_issuer")
        issuer = str(matches[0]["orgId"])
        dates = [
            t.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            for t in (request.start, request.as_of)
        ]
        body = urlencode(
            {
                "pageNum": 1,
                "pageSize": request.limit,
                "column": "szse",
                "tabName": "fulltext",
                "stock": f"{listing.ticker},{issuer}",
                "seDate": "~".join(dates),
                "sortName": "time",
                "sortType": "desc",
                "plate": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "isHLtitle": "false",
            }
        ).encode()
        response = await self.http.request(
            QUERY, "query", body=body, max_bytes=min(request.max_bytes, 2 * 1024 * 1024)
        )
        if len(response.body) > min(request.max_bytes, 2 * 1024 * 1024):
            raise DisclosureError("response_too_large")
        items = json_material(response).get("announcements")
        if items is None:
            items = []
        if not isinstance(items, list) or len(items) > request.limit:
            raise DisclosureError("invalid_discovery_schema_or_count")
        acquired = datetime.now(UTC)
        saved, candidates = [], []
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("secCode") != listing.ticker
                or str(item.get("orgId")) != issuer
            ):
                raise DisclosureError("source_identity_mismatch")
            disclosure_id, millis = item.get("announcementId"), item.get("announcementTime")
            if (
                not isinstance(disclosure_id, str)
                or not re.fullmatch(r"\d{1,32}", disclosure_id)
                or not isinstance(millis, int)
                or isinstance(millis, bool)
            ):
                raise DisclosureError("invalid_disclosure_identity_or_time")
            try:
                published = datetime.fromtimestamp(millis / 1000, UTC)
            except (ValueError, OverflowError, OSError) as exc:
                raise DisclosureError("invalid_publication_time") from exc
            if not request.start <= published <= request.as_of:
                continue
            adjunct = item.get("adjunctUrl")
            if not isinstance(adjunct, str):
                raise DisclosureError("missing_original_locator")
            url = "https://static.cninfo.com.cn/" + adjunct
            validate_url(url, "original")
            title = item.get("announcementTitle")
            if not isinstance(title, str) or len(title) > 2000:
                raise DisclosureError("invalid_disclosure_title")
            identity = {
                "source": SOURCE,
                "listing_id": listing.id,
                "ticker": f"{listing.ticker}.SZ",
                "exchange": "SZSE",
                "issuer_id": issuer,
                "disclosure_id": disclosure_id,
                "first_published_at": published.isoformat(),
                "source_url": url,
            }
            identity_hash = digest(identity)
            metadata = {
                **identity,
                "id": str(uuid4()),
                "title": title,
                "discovered_at": acquired.isoformat(),
                "identity_directory_hash": hashlib.sha256(directory_response.body).hexdigest(),
                "correction_lead": any(term in title for term in ("更正", "修订", "补充")),
                "verified_correction_refs": [],
                "verification": "unverified",
                "historical_reconstruction": request.as_of < acquired,
            }
            candidates.append((identity_hash, metadata))
        # Validate the complete bounded page before any write. A harmless no-op
        # DML obtains SQLite's writer reservation after the network phase,
        # avoiding stale read-to-write savepoint upgrades across processes.
        replay = await self.locked_replay()
        if replay is not None:
            return replay
        for identity_hash, metadata in candidates:
            await self.session.execute(
                insert(DisclosureItem)
                .values(
                    id=metadata["id"],
                    owner_id=self.owner,
                    listing_id=listing.id,
                    identity_hash=identity_hash,
                    payload=metadata,
                )
                .on_conflict_do_nothing()
            )
            row = await self.session.scalar(
                select(DisclosureItem).where(
                    DisclosureItem.owner_id == self.owner,
                    DisclosureItem.identity_hash == identity_hash,
                )
            )
            assert row is not None
            saved.append(row.payload)
        return {
            "status": "ok",
            "source": SOURCE,
            "items": saved,
            "truncated": bool(json_material(response).get("hasMore")),
            "verification": "unverified",
            "untrusted_source_data": True,
            "coverage": "one bounded official page; no verified financial facts",
        }

    async def item(self, item_id: UUID, listing_id: int | None = None) -> DisclosureItem:
        row = await self.session.get(DisclosureItem, str(item_id))
        if (
            row is None
            or row.owner_id != self.owner
            or (listing_id is not None and listing_id != row.listing_id)
        ):
            raise DisclosureError("disclosure_not_found")
        return row

    async def fetch(self, request: FetchRequest) -> dict[str, Any]:
        await self._listing(request.listing_id)
        item = await self.item(request.item_id, request.listing_id)
        if datetime.fromisoformat(item.payload["first_published_at"]) > request.as_of:
            raise DisclosureError("publication_after_cutoff")
        validate_url(item.payload["source_url"], "original")
        response = await self.http.request(
            item.payload["source_url"], "original", max_bytes=request.max_bytes
        )
        if len(response.body) > request.max_bytes:
            raise DisclosureError("response_too_large")
        mime = original_mime(response)
        content_hash = hashlib.sha256(response.body).hexdigest()
        now = datetime.now(UTC)
        replay = await self.locked_replay()
        if replay is not None:
            return replay
        prior = (
            await self.session.scalars(
                select(DisclosureOriginal)
                .where(
                    DisclosureOriginal.owner_id == self.owner, DisclosureOriginal.item_id == item.id
                )
                .order_by(
                    DisclosureOriginal.payload["acquired_at"].as_string().desc(),
                    DisclosureOriginal.id.desc(),
                )
                .limit(1)
            )
        ).all()
        identical = await self.session.scalar(
            select(DisclosureOriginal)
            .join(DisclosureItem)
            .where(
                DisclosureOriginal.owner_id == self.owner,
                DisclosureItem.listing_id == item.listing_id,
                DisclosureOriginal.content_hash == content_hash,
            )
            .limit(1)
        )
        if identical is not None:
            return fetch_result(identical.payload, request, reused=True)
        original_id, evidence_id = uuid4(), uuid4()
        original = {
            "id": str(original_id),
            "item_id": item.id,
            "listing_id": item.listing_id,
            "source": SOURCE,
            "disclosure_id": item.payload["disclosure_id"],
            "issuer_id": item.payload["issuer_id"],
            "ticker": item.payload["ticker"],
            "exchange": "SZSE",
            "source_url": item.payload["source_url"],
            "resolved_url": response.url,
            "first_published_at": item.payload["first_published_at"],
            "acquired_at": now.isoformat(),
            "available_from": now.isoformat(),
            "content_hash": content_hash,
            "mime": mime,
            "size": len(response.body),
            "verification": "candidate",
            "prior_original_refs": [r.id for r in prior],
            "verified_correction_refs": [],
            "historical_reconstruction": request.as_of < now,
            "evidence_ref": ObjectRef(kind="evidence", id=evidence_id, revision=1).model_dump(
                mode="json"
            ),
        }
        await self.session.execute(
            insert(DisclosureBlob)
            .values(content_hash=content_hash, body=response.body)
            .on_conflict_do_nothing()
        )
        inserted = await self.session.scalar(
            insert(DisclosureOriginal)
            .values(
                id=str(original_id),
                owner_id=self.owner,
                item_id=item.id,
                content_hash=content_hash,
                payload=original,
            )
            .on_conflict_do_nothing()
            .returning(DisclosureOriginal.id)
        )
        if inserted is None:
            duplicate = await self.session.scalar(
                select(DisclosureOriginal).where(
                    DisclosureOriginal.owner_id == self.owner,
                    DisclosureOriginal.item_id == item.id,
                    DisclosureOriginal.content_hash == content_hash,
                )
            )
            assert duplicate is not None
            return fetch_result(duplicate.payload, request, reused=True)
        evidence = EvidenceRecord(
            id=evidence_id,
            revision=1,
            created_at=now,
            actor="disclosure_adapter",
            origin_ref=None,
            source_ref=ObjectRef(kind="disclosure_original", id=original_id, revision=1),
            source_kind="document",
            locator=f"/investor/disclosures/originals/{original_id}/download",
            content_hash=content_hash,
            subject_ref=ObjectRef(kind="listing", id=item.listing_id, revision=1),
            period=None,
            published_at=datetime.fromisoformat(item.payload["first_published_at"]),
            acquired_at=now,
            available_from=now,
            verification="candidate",
            conflict="none",
            text="Unparsed official original: " + item.payload["title"],
        )
        self.session.add(
            SavedEvidence(
                id=str(evidence_id),
                revision=1,
                owner_id=self.owner,
                payload=evidence.model_dump(mode="json"),
            )
        )
        await self.session.flush()
        return fetch_result(original, request, reused=False)

    async def metadata(
        self, listing_id: int, as_of: datetime, *, historical_reconstruction: bool = False
    ) -> dict[str, Any]:
        cutoff = utc_timestamp(as_of)
        items = (
            await self.session.scalars(
                select(DisclosureItem)
                .where(
                    DisclosureItem.owner_id == self.owner, DisclosureItem.listing_id == listing_id
                )
                .limit(100)
            )
        ).all()
        visible = [
            r.payload
            for r in items
            if datetime.fromisoformat(r.payload["first_published_at"]) <= cutoff
            and (
                historical_reconstruction
                or datetime.fromisoformat(r.payload["discovered_at"]) <= cutoff
            )
        ]
        originals = (
            await self.session.scalars(
                select(DisclosureOriginal)
                .join(DisclosureItem)
                .where(
                    DisclosureOriginal.owner_id == self.owner,
                    DisclosureItem.listing_id == listing_id,
                )
                .limit(100)
            )
        ).all()
        return {
            "items": visible,
            "originals": [
                r.payload
                for r in originals
                if datetime.fromisoformat(r.payload["first_published_at"]) <= cutoff
                and (
                    historical_reconstruction
                    or max(
                        datetime.fromisoformat(r.payload["acquired_at"]),
                        datetime.fromisoformat(r.payload["available_from"]),
                    )
                    <= cutoff
                )
            ],
            "historical_reconstruction": historical_reconstruction,
            "verification": "unverified",
            "limit": 100,
        }

    async def read(
        self, original_id: UUID, as_of: datetime, *, historical_reconstruction: bool = False
    ) -> tuple[dict[str, Any], bytes]:
        row = await self.session.get(DisclosureOriginal, str(original_id))
        if row is None or row.owner_id != self.owner:
            raise DisclosureError("original_not_found")
        payload = row.payload
        if datetime.fromisoformat(payload["first_published_at"]) > as_of or (
            not historical_reconstruction
            and max(
                datetime.fromisoformat(payload["acquired_at"]),
                datetime.fromisoformat(payload["available_from"]),
            )
            > as_of
        ):
            raise DisclosureError("original_not_visible_at_cutoff")
        blob = await self.session.get(DisclosureBlob, row.content_hash)
        if blob is None or hashlib.sha256(blob.body).hexdigest() != row.content_hash:
            raise DisclosureError("original_integrity_failure")
        return payload, blob.body
