"""AG25 official protocol fixtures; no live acquisition or model calls."""

import hashlib
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.evidence.disclosure_http import (
    DisclosureError,
    OfficialHTTPS,
    PinnedHTTPS,
    SourceResponse,
    public_targets,
    validate_url,
)
from investment_agent.evidence.disclosures import DisclosureService
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
    DisclosureReceipt,
)
from tests.support.disclosure import (  # noqa: F401
    NOW,
    PDF,
    PUBLISHED,
    URL,
    FixtureHTTP,
    discovery,
    obtained,
    seed,
)


async def test_original_replay_hash_versions_pit_and_untrusted_title(db_session):
    service, http, request = await obtained(db_session)
    operation = uuid4()
    result = await service.execute(operation, request)
    assert result["status"] == "ok"
    original = result["original"]
    assert original["content_hash"] == hashlib.sha256(PDF).hexdigest()
    assert original["verified_correction_refs"] == []
    calls = len(http.calls)
    assert await service.execute(operation, request) == result
    assert len(http.calls) == calls
    repeat = await service.execute(uuid4(), request)
    assert repeat["reused"] and repeat["original"]["id"] == original["id"]
    with pytest.raises(DisclosureError, match="not_visible"):
        await service.read(UUID(original["id"]), NOW)
    _, body = await service.read(UUID(original["id"]), NOW, historical_reconstruction=True)
    assert body == PDF
    visible = await service.metadata(request.listing_id, NOW)
    assert not visible["items"] and not visible["originals"]
    historical = await service.metadata(request.listing_id, NOW, historical_reconstruction=True)
    assert historical["originals"][0]["historical_reconstruction"]
    evidence_ref = ObjectRef.model_validate(original["evidence_ref"])
    evidence = await EvidenceService(db_session).get_evidence(
        "local_owner", evidence_ref, datetime.now(UTC)
    )
    assert evidence.data.verification == "candidate" and evidence.data.value is None
    assert (
        await EvidenceService(db_session).get_evidence("local_owner", evidence_ref, NOW)
    ).status == "missing"
    http.body = b"%PDF-1.7\nCORRECTED\n%%EOF"
    corrected = await service.execute(uuid4(), request)
    assert corrected["original"]["id"] != original["id"]
    assert corrected["original"]["prior_original_refs"] == [original["id"]]
    assert corrected["original"]["verified_correction_refs"] == []
    assert (await service.read(UUID(original["id"]), datetime.now(UTC)))[1] == PDF
    assert await db_session.scalar(select(func.count()).select_from(DisclosureBlob)) == 2
    assert (
        await DisclosureService(db_session, "other_owner").metadata(
            request.listing_id, datetime.now(UTC)
        )
    )["originals"] == []
    with pytest.raises(DisclosureError, match="not_found"):
        await DisclosureService(db_session, "other_owner").read(
            UUID(original["id"]), datetime.now(UTC)
        )


@pytest.mark.parametrize("failure", ["http_timeout", "response_too_large", "http_status_403"])
async def test_failed_fetch_durable_reason_no_acquired_material(db_session, failure):
    service, http, request = await obtained(db_session)
    http.failure = failure
    operation = uuid4()
    result = await service.execute(operation, request)
    assert result["status"] == "error" and result["reason"] == failure
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0
    assert await db_session.scalar(select(func.count()).select_from(DisclosureBlob)) == 0
    http.failure = None
    assert await service.execute(operation, request) == result
    assert await db_session.get(DisclosureReceipt, str(operation)) is not None


@pytest.mark.parametrize(
    "change,reason",
    [
        ("duplicate_issuer", "ambiguous"),
        ("wrong_code", "identity_mismatch"),
        ("wrong_issuer", "identity_mismatch"),
        ("url", "unsupported_source_url"),
        ("unknown_time", "invalid_disclosure_identity_or_time"),
    ],
)
async def test_source_identity_fail_closed(db_session, change, reason):
    listing_id = await seed(db_session)
    http = FixtureHTTP()
    if change == "duplicate_issuer":
        http.directory *= 2
    if change == "wrong_code":
        http.items[0]["secCode"] = "600001"
    if change == "wrong_issuer":
        http.items[0]["orgId"] = "other"
    if change == "url":
        http.items[0]["adjunctUrl"] = "https://private.invalid/file.pdf"
    if change == "unknown_time":
        http.items[0]["announcementTime"] = None
    result = await DisclosureService.for_tests(db_session, http).execute(
        uuid4(), discovery(listing_id)
    )
    assert result["status"] == "error" and reason in result["reason"]
    assert await db_session.scalar(select(func.count()).select_from(DisclosureItem)) == 0


@pytest.mark.parametrize(
    "body,mime",
    [
        (b"<html>login</html>", "application/pdf"),
        (PDF, "text/html"),
        (b"%PDF-1.7 incomplete", "application/pdf"),
        (PDF, "application/octet-stream"),
    ],
)
async def test_mime_signature_failure(db_session, body, mime):
    service, http, request = await obtained(db_session)
    http.body, http.mime = body, mime
    result = await service.execute(uuid4(), request)
    assert result["reason"] == "invalid_original_mime_or_signature"


async def test_html_original_bytes_remain_immutable(db_session):
    http = FixtureHTTP()
    http.items[0]["adjunctUrl"] = "finalpage/2026-10-09/12345.html"
    service, http, request = await obtained(db_session, http)
    http.body, http.mime = b'<!doctype html><script>fetch("/admin")</script>', "text/html"
    result = await service.execute(uuid4(), request)
    assert (await service.read(UUID(result["original"]["id"]), datetime.now(UTC)))[1] == http.body


@pytest.mark.parametrize(
    "url",
    [
        "http://static.cninfo.com.cn/finalpage/2026-10-09/12345.PDF",
        "https://user:pw@static.cninfo.com.cn/finalpage/2026-10-09/12345.PDF",
        "https://static.cninfo.com.cn:444/finalpage/2026-10-09/12345.PDF",
        "https://static.cninfo.com.cn.evil.test/finalpage/2026-10-09/12345.PDF",
        "https://static.cninfo.com.cn/finalpage/../12345.PDF",
        "https://static.cninfo.com.cn/finalpage/2026-10-09/12345.PDF?url=http://localhost",
        "https://127.0.0.1/file.PDF",
    ],
)
def test_allowlisted_url_only(url):
    with pytest.raises(DisclosureError):
        validate_url(url, "original")


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["8.8.8.8", "10.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["192.0.2.1"],
        ["::ffff:8.8.8.8"],
    ],
)
def test_private_reserved_and_mixed_dns(addresses):
    with pytest.raises(DisclosureError, match="non_public"):
        public_targets(
            "official.test",
            lambda *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
            ],
        )


async def test_every_redirect_hop_rechecks_dns_and_never_second_resolution(monkeypatch):
    targets, calls = iter([("8.8.8.8",), ("1.1.1.1",)]), []
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets", lambda host: next(targets)
    )

    def read(host, path, target, url, body, max_bytes, deadline):
        calls.append(target)
        return (
            SourceResponse(302, {"location": URL.replace("12345", "67890")}, b"", url)
            if len(calls) == 1
            else SourceResponse(200, {"content-type": "application/pdf"}, PDF, url)
        )

    monkeypatch.setattr(OfficialHTTPS, "_read", staticmethod(read))
    assert (await OfficialHTTPS().request(URL, "original")).body == PDF
    assert calls == ["8.8.8.8", "1.1.1.1"]
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets",
        lambda host: (_ for _ in ()).throw(DisclosureError("non_public_or_mixed_dns")),
    )
    with pytest.raises(DisclosureError):
        await OfficialHTTPS().request(URL, "original")


async def test_unsafe_redirect_and_timeout(monkeypatch):
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets", lambda host: ("8.8.8.8",)
    )
    monkeypatch.setattr(
        OfficialHTTPS,
        "_read",
        staticmethod(
            lambda *args: SourceResponse(302, {"location": "https://localhost/private"}, b"", URL)
        ),
    )
    with pytest.raises(DisclosureError, match="unsupported_source_url"):
        await OfficialHTTPS().request(URL, "original")

    def timeout(*args):
        raise TimeoutError()

    monkeypatch.setattr(OfficialHTTPS, "_read", staticmethod(timeout))
    with pytest.raises(DisclosureError, match="http_timeout"):
        await OfficialHTTPS().request(URL, "original")


def test_socket_connect_is_pinned_and_retains_tls_sni(monkeypatch):
    connected, wrapped = [], []

    class Raw:
        def settimeout(self, t):
            pass

        def connect(self, address):
            connected.append(address)

        def do_handshake(self):
            pass

        def close(self):
            pass

    class TLS:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            wrapped.append(server_hostname)
            return raw

    monkeypatch.setattr(socket, "socket", lambda *a: Raw())
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.ssl.create_default_context", lambda: TLS()
    )
    connection = PinnedHTTPS("static.cninfo.com.cn", "8.8.8.8", 2)
    connection.connect()
    assert connected == [("8.8.8.8", 443)] and wrapped == ["static.cninfo.com.cn"]


async def test_fetch_publication_cutoff_owner_and_size(db_session):
    service, http, request = await obtained(db_session)
    result = await service.execute(
        uuid4(), request.model_copy(update={"as_of": PUBLISHED - timedelta(seconds=1)})
    )
    assert result["reason"] == "publication_after_cutoff"
    result = await DisclosureService.for_tests(db_session, http, "other_owner").execute(
        uuid4(), request
    )
    assert result["reason"] == "disclosure_not_found"
    http.body = b"%PDF-1.7\n" + b"a" * 2048 + b"%%EOF"
    result = await service.execute(uuid4(), request.model_copy(update={"max_bytes": 1024}))
    assert result["reason"] == "response_too_large"


async def test_original_metadata_and_bytes_rollback_together(db_session):
    service, http, request = await obtained(db_session)
    async with db_session.begin_nested() as transaction:
        result = await service.execute(uuid4(), request)
        assert result["status"] == "ok"
        await transaction.rollback()
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0
    assert await db_session.scalar(select(func.count()).select_from(DisclosureBlob)) == 0


async def test_duplicate_disclosure_id_and_same_hash_not_corroboration(db_session):
    service, http, request = await obtained(db_session)
    first = await service.execute(uuid4(), request)
    http.items[0]["announcementId"] = "98765"
    http.items[0]["adjunctUrl"] = "finalpage/2026-10-09/98765.PDF"
    second = await service.execute(uuid4(), discovery(request.listing_id))
    duplicate = await service.execute(
        uuid4(), request.model_copy(update={"item_id": UUID(second["items"][0]["id"])})
    )
    assert duplicate["original"]["id"] == first["original"]["id"]
    assert duplicate["duplicate_hash_is_not_independent_corroboration"]
    assert await db_session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 1


@pytest.mark.parametrize("phase", ["headers", "stream"])
async def test_total_deadline_aborts_slow_header_and_body_socket(monkeypatch, phase):
    import asyncio
    import threading
    import time

    stopped = threading.Event()

    class Connection:
        def __init__(self, host, target, timeout):
            self.deadline, self.sock, self.active_socket = time.monotonic() + timeout, self, self

        def remaining(self):
            return self.deadline - time.monotonic()

        def settimeout(self, value):
            pass

        def request(self, *args, **kwargs):
            pass

        def abort(self):
            stopped.set()

        def close(self):
            stopped.set()

        def getresponse(self):
            if phase == "headers":
                stopped.wait(1)
                raise OSError("socket closed at total deadline")
            return self

        status = 200

        def getheaders(self):
            return [("content-type", "application/pdf")]

        def read1(self, count):
            stopped.wait(1)
            raise OSError("stream closed at total deadline")

    monkeypatch.setattr("investment_agent.evidence.disclosure_http.PinnedHTTPS", Connection)
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets", lambda host: ("8.8.8.8",)
    )
    started = time.monotonic()
    with pytest.raises(DisclosureError, match="http_timeout"):
        await OfficialHTTPS().request(URL, "original", timeout_seconds=0.05)
    assert time.monotonic() - started < 0.5
    assert await asyncio.to_thread(stopped.wait, 0.5)


@pytest.mark.parametrize("kind", ["declared_size", "stream_size", "incomplete", "encoding"])
async def test_streamed_response_bounds_and_incomplete_content(monkeypatch, kind):
    class Connection:
        sock = active_socket = None

        def __init__(self, *args):
            self.parts = iter([b"a" * (2048 if kind == "stream_size" else 5), b""])

        def request(self, *args, **kwargs):
            pass

        def abort(self):
            pass

        def close(self):
            pass

        def getresponse(self):
            return self

        status = 200

        def getheaders(self):
            return (
                [("content-length", "2048" if kind == "declared_size" else "1024")]
                if kind in {"declared_size", "incomplete"}
                else [("content-encoding", "gzip" if kind == "encoding" else "identity")]
            )

        def read1(self, size):
            return next(self.parts)

    monkeypatch.setattr("investment_agent.evidence.disclosure_http.PinnedHTTPS", Connection)
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets", lambda host: ("8.8.8.8",)
    )
    reason = (
        "response_too_large"
        if "size" in kind
        else "incomplete_response"
        if kind == "incomplete"
        else "unsupported_content_encoding"
    )
    with pytest.raises(DisclosureError, match=reason):
        await OfficialHTTPS().request(URL, "original", max_bytes=1024)


async def test_rebinding_and_redirect_budget_stop_before_next_socket(monkeypatch):
    reads, resolutions = [], []

    def targets(host):
        resolutions.append(host)
        if len(resolutions) == 2:
            raise DisclosureError("non_public_or_mixed_dns")
        return ("8.8.8.8",)

    def read(*args):
        reads.append(args[2])
        return SourceResponse(302, {"location": URL.replace("12345", "98765")}, b"", args[3])

    monkeypatch.setattr("investment_agent.evidence.disclosure_http.public_targets", targets)
    monkeypatch.setattr(OfficialHTTPS, "_read", staticmethod(read))
    with pytest.raises(DisclosureError, match="non_public"):
        await OfficialHTTPS().request(URL, "original")
    assert len(reads) == 1 and len(resolutions) == 2
    reads.clear()
    monkeypatch.setattr(
        "investment_agent.evidence.disclosure_http.public_targets", lambda host: ("8.8.8.8",)
    )
    with pytest.raises(DisclosureError, match="redirect_limit"):
        await OfficialHTTPS().request(URL, "original")
    assert len(reads) == 3
