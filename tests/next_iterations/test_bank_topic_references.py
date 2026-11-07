"""BT-013 real local PDF + synthetic owner-scoped rows; no production import or account."""

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from tests.next_iterations.test_bank_decomposition import BASELINE, IDS, PERIOD
from tests.next_iterations.test_bank_decomposition_proofs import decomposition_fact
from tests.next_iterations.test_bank_nii_proofs import MANIFEST, ROOT, make_fact

from investment_agent.bank_topics.contracts import FactBinding, TopicFactRevision, TopicRef
from investment_agent.bank_topics.contracts import TopicRevision as RevisionDTO
from investment_agent.bank_topics.decomposition import calculate_decomposition
from investment_agent.bank_topics.facts import FactObservation, bank_content_hash
from investment_agent.bank_topics.references import BankTopicReferenceResolver, publication_upper
from investment_agent.evidence.contracts import Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.bank_proofs import checked_bank_locator
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.evidence import peer_field_evidence
from investment_agent.industry_peers.originals import AnnualOriginal
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models import Base
from investment_agent.storage.models.bank_topics import TopicCase
from investment_agent.storage.models.bank_topics import TopicFactRevision as FactRow
from investment_agent.storage.models.bank_topics import TopicRevision as RevisionRow
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.bank_topics import BankTopicReferenceError

AS_OF = datetime(2026, 11, 7, tzinfo=UTC)
CHECKED = datetime(2026, 11, 6, 18, 29, 12, tzinfo=UTC)


def fact_row(fact: TopicFactRevision) -> FactRow:
    payload = fact.model_dump(mode="json")
    columns = {key: value for key, value in payload.items() if key not in {"timing", "unknown_reason"}}
    columns.update(reason=fact.unknown_reason, content_hash=bank_content_hash(fact), created_at=CHECKED,
                   published_at=fact.timing.published_at, published_date=fact.timing.published_date,
                   published_precision=fact.timing.published_precision, acquired_at=fact.timing.acquired_at,
                   checked_at=fact.timing.checked_at, review_actor=fact.timing.review_actor)
    return FactRow(**columns)


def parent_row(parent: RevisionDTO) -> RevisionRow:
    payload = parent.model_dump(mode="json")
    payload.update(as_of=parent.as_of, content_hash=bank_content_hash(parent), created_at=CHECKED, actor="synthetic-binding-test")
    return RevisionRow(**payload)


@dataclass
class SourceData:
    case_id: UUID
    parent: RevisionDTO
    facts: dict[str, TopicFactRevision]
    original_id: UUID
    peer_id: UUID
    evidence_ref: ObjectRef


@dataclass
class Fixture:
    session: AsyncSession
    resolver: BankTopicReferenceResolver
    data: SourceData

    def ref(self, stable_id: str) -> TopicRef:
        return TopicRef(kind="topic_revision", id=self.data.parent.id, revision=1, field_path=f"facts.{stable_id}")

    async def observation(self, stable_id: str) -> FactObservation:
        return await self.resolver.resolve_fact("local_owner", self.ref(stable_id), listing_id=18, as_of=AS_OF)

    async def new_fact(self, fact: TopicFactRevision, *, bad_fact_hash: bool = False, bad_parent_hash: bool = False) -> TopicRef:
        fact = fact.model_copy(update={"id": uuid4(), "revision": 2})
        row = fact_row(fact)
        if bad_fact_hash:
            row.content_hash = "a" * 64
        binding = FactBinding.model_validate({"stable_fact_id": fact.stable_fact_id, "metric": fact.metric,
            "source": {"binding_kind": "topic_fact", "fact_ref": {"id": str(fact.id), "revision": 2}}})
        parent = self.data.parent.model_copy(update={"id": uuid4(), "revision": 2,
            "fact_bindings": tuple(binding if item.stable_fact_id == fact.stable_fact_id else item for item in self.data.parent.fact_bindings)})
        parent_record = parent_row(parent)
        if bad_parent_hash:
            parent_record.content_hash = "b" * 64
        case = await self.session.get(TopicCase, str(self.data.case_id))
        assert case is not None
        case.head_revision = 2
        self.session.add_all([row, parent_record])
        await self.session.flush()
        return TopicRef(kind="topic_revision", id=parent.id, revision=1, field_path=f"facts.{fact.stable_fact_id}")


@pytest.fixture(scope="module")
async def database(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[tuple[AsyncEngine, SourceData]]:
    database_path = tmp_path_factory.mktemp("bank-ref") / "sources.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    body = (ROOT / str(MANIFEST["source"]["path"])).read_bytes()
    case_id, original_id, item_id, peer_id = uuid4(), uuid4(), uuid4(), uuid4()
    original_ref = ObjectRef(kind="disclosure_original", id=original_id, revision=1)
    url = MANIFEST["source"]["url"]
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        metadata = {"listing_id": 18, "ticker": "600036.SH", "disclosure_id": "1225047590", "title": "招商银行股份有限公司2025年度报告",
                    "source_url": url, "resolved_url": url, "first_published_at": "2026-03-27T16:00:00+00:00",
                    "acquired_at": MANIFEST["source"]["acquired_at"], "available_from": MANIFEST["source"]["acquired_at"]}
        session.add_all([Market(code="SSE", name="synthetic", timezone="Asia/Shanghai", currency="CNY"),
            Company(id=18, canonical_name="招商银行"), Listing(id=18, company_id=18, market_code="SSE", ticker="600036"),
            DisclosureBlob(content_hash=MANIFEST["source"]["sha256"], body=body),
            DisclosureItem(id=str(item_id), owner_id="local_owner", listing_id=18, identity_hash="a" * 64, payload=metadata),
            DisclosureOriginal(id=str(original_id), owner_id="local_owner", item_id=str(item_id), content_hash=MANIFEST["source"]["sha256"], payload=metadata),
            TopicCase(id=str(case_id), owner_id="local_owner", listing_id=18, template_key="bank_nii_sustainability_v1",
                      base_period=date(2025, 12, 31), head_revision=1, control_revision=1, state="ready", created_at=CHECKED)])
        facts = {}
        bindings = []
        for fact_id in IDS:
            fact = make_fact(fact_id) if fact_id in IDS[:4] else decomposition_fact(fact_id)
            assert fact.proof is not None
            fact = fact.model_copy(update={"case_id": case_id, "source_refs": (original_ref,), "verification": "verified",
                "proof": fact.proof.model_copy(update={"original_ref": original_ref}),
                "timing": fact.timing.model_copy(update={"checked_at": CHECKED, "review_actor": "synthetic-binding-test"})})
            facts[fact_id] = fact
            session.add(fact_row(fact))
            bindings.append(FactBinding.model_validate({"stable_fact_id": fact_id, "metric": fact.metric, "source": {
                "binding_kind": "topic_fact", "fact_ref": {"id": str(fact.id), "revision": 1}}}))
        # The legacy NIM uses its existing v1 rule/page16 and annual_ratio basis,
        # rather than pretending the new topic rule is registered in bank-v2.
        audit = next(record for record in json.loads((ROOT / "tests/fixtures/bank_proofs_v2/audit-manifest.json").read_text())["records"]
                     if record["code"] == "600036.SH" and record["metric"] == "net_interest_margin")
        proof = {**audit, "original_ref": original_ref.model_dump(mode="json"), "pdf_hash": MANIFEST["source"]["sha256"],
                 "audit_record": audit, "report_identity": {"code": "600036.SH", "period": "2025-12-31", "disclosure_id": "1225047590"}}
        original = AnnualOriginal("600036.SH", date(2025, 12, 31), body,
            {"chosen": {"announcementId": "1225047590", "announcementTitle": metadata["title"], "secName": "招商银行"}, "url": url},
            MANIFEST["source"]["sha256"], datetime.fromisoformat(metadata["first_published_at"]))
        proof.update(checked_bank_locator(original, "net_interest_margin", proof))
        field = PeerAnnualField(metric="net_interest_margin", value=Decimal("0.0187"), unit="fraction",
            period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual_ratio"),
            raw_value="1.87", raw_unit="percent", disclosed_on=date(2026, 3, 28), acquired_at=datetime.fromisoformat(audit["acquired_at"]), verification=PeerFieldVerification.VERIFIED)
        peer = PeerAnnualSnapshot(id=str(peer_id), company_id=18, listing_id=18, report_period=date(2025, 12, 31),
            provider="synthetic-audited", adapter_version="bank-original/1", captured_at=CHECKED,
            content_hash="c" * 64, semantic_hash="c" * 64, fields={"net_interest_margin": field.model_dump(mode="json")}, verification={"net_interest_margin": proof})
        session.add(peer)
        await session.flush()
        record = peer_field_evidence(peer, "net_interest_margin").record
        assert record is not None
        evidence_ref = await EvidenceService(session).save_evidence("local_owner", record, uuid4())
        for stable_id, external in (("nim_peer", ObjectRef(kind="peer_annual_snapshot", id=peer_id, revision=1, field_path="fields.net_interest_margin")), ("nim_evidence", evidence_ref)):
            bindings.append(FactBinding.model_validate({"stable_fact_id": stable_id, "metric": "nim", "source": {"binding_kind": "existing_fact", "source_ref": external.model_dump(mode="json")}}))
        parent = RevisionDTO(id=uuid4(), case_id=case_id, revision=1, question="净息差下降时净利息收入为何仍增长？", period=PERIOD,
            baseline_period=BASELINE, as_of=AS_OF, fact_bindings=tuple(bindings), decomposition=None, explanations=(), condition_refs=(), content_hash="0" * 64, supersedes_ref=None)
        session.add(parent_row(parent))
        await session.commit()
    yield engine, SourceData(case_id, parent, facts, original_id, peer_id, evidence_ref)
    await engine.dispose()


@pytest.fixture
async def fixture(database: tuple[AsyncEngine, SourceData]) -> AsyncIterator[Fixture]:
    engine, data = database
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield Fixture(session, BankTopicReferenceResolver(session), data)
        await session.rollback()


async def test_local_facts_rederive_bytes_hashes_and_calculate_in_one_listing(fixture: Fixture) -> None:
    observations = [await fixture.observation(fact_id) for fact_id in IDS]
    assert all(item.proof_checked and item.verification == "verified" for item in observations)
    assert all(item.basis_kind == "topic_fact" for item in observations)
    result = calculate_decomposition(observations, period=PERIOD, baseline_period=BASELINE)
    assert result.residual.value == 0 and result.delta_nim_bp.value == -11


@pytest.mark.parametrize("stable_id", ["nim_peer", "nim_evidence"])
async def test_legacy_peer_and_saved_evidence_stay_external_and_reuse_old_proof_rules(fixture: Fixture, stable_id: str) -> None:
    observation = await fixture.observation(stable_id)
    assert observation.value == Decimal("0.0187") and observation.proof_checked
    assert observation.basis_kind == ("peer_field" if stable_id == "nim_peer" else "evidence")
    assert observation.source_period_basis == "annual_ratio"
    assert any(ref.kind == "peer_annual_snapshot" for ref in observation.source_refs)
    base = [await fixture.observation(fact_id) for fact_id in IDS if fact_id != "nim_2025"]
    assert calculate_decomposition([*base, observation], period=PERIOD, baseline_period=BASELINE).delta_nim_bp.value == -11


@pytest.mark.parametrize("change", ["fact_hash", "parent_hash", "original_hash", "excerpt", "unit", "topic_cycle"])
async def test_payload_original_locator_units_and_cycles_cannot_self_certify(fixture: Fixture, change: str) -> None:
    fact = fixture.data.facts["nii_2025"]
    proof = fact.proof
    assert proof is not None
    if change == "original_hash":
        fact = fact.model_copy(update={"proof": proof.model_copy(update={"pdf_sha256": "f" * 64})})
    elif change == "excerpt":
        fact = fact.model_copy(update={"proof": proof.model_copy(update={"excerpt_hash": "f" * 64})})
    elif change == "unit":
        fact = fact.model_copy(update={"raw_unit": "percent"})
    elif change == "topic_cycle":
        fact = fact.model_copy(update={"source_refs": (*fact.source_refs, ObjectRef.model_validate(fixture.ref("nii_2025").model_dump()))})
    reference = await fixture.new_fact(fact, bad_fact_hash=change == "fact_hash", bad_parent_hash=change == "parent_hash")
    with pytest.raises(BankTopicReferenceError):
        await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)


@pytest.mark.parametrize("kind", ["original_owner", "item_listing", "blob_bytes"])
async def test_actual_owner_listing_and_blob_bytes_are_reloaded(fixture: Fixture, kind: str) -> None:
    original = await fixture.session.get(DisclosureOriginal, str(fixture.data.original_id))
    assert original is not None
    if kind == "original_owner":
        await fixture.session.execute(update(DisclosureOriginal).where(DisclosureOriginal.id == original.id).values(owner_id="foreign_test_owner"))
    elif kind == "item_listing":
        await fixture.session.execute(update(DisclosureItem).where(DisclosureItem.id == original.item_id).values(listing_id=19))
    else:
        await fixture.session.execute(update(DisclosureBlob).where(DisclosureBlob.content_hash == original.content_hash).values(body=b"%PDF-tampered"))
    with pytest.raises(BankTopicReferenceError):
        await fixture.observation("nii_2025")


@pytest.mark.parametrize("verification", ["candidate", "pending_review", "scope_conflict", "source_unavailable"])
async def test_stored_candidate_or_conflict_never_becomes_a_usable_verified_fact(fixture: Fixture, verification: str) -> None:
    fact = fixture.data.facts["nii_2025"].model_copy(update={"verification": verification})
    reference = await fixture.new_fact(fact)
    observation = await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)
    assert observation.verification == verification
    others = [await fixture.observation(fact_id) for fact_id in IDS if fact_id != "nii_2025"]
    result = calculate_decomposition([observation, *others], period=PERIOD, baseline_period=BASELINE)
    assert result.delta_nii.value is None and result.coverage == "partial"


async def test_candidate_missing_proof_stays_an_explicit_gap(fixture: Fixture) -> None:
    fact = fixture.data.facts["nii_2025"].model_copy(update={"verification": "candidate", "proof": None})
    reference = await fixture.new_fact(fact)
    observation = await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)
    assert observation.verification == "candidate" and observation.value is None
    assert observation.unknown_reason == "needs_original_proof" and not observation.proof_checked


@pytest.mark.parametrize("field", ["acquired_at", "checked_at"])
async def test_future_source_or_review_time_is_not_available_at_cutoff(fixture: Fixture, field: str) -> None:
    fact = fixture.data.facts["nii_2025"]
    fact = fact.model_copy(update={"timing": fact.timing.model_copy(update={field: AS_OF + timedelta(seconds=1)})})
    reference = await fixture.new_fact(fact)
    observation = await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)
    assert observation.value is None and observation.unknown_reason == "not_visible_as_of"
    assert not observation.proof_checked


async def test_unknown_publication_time_remains_unknown(fixture: Fixture) -> None:
    fact = fixture.data.facts["nii_2025"]
    fact = fact.model_copy(update={"timing": fact.timing.model_copy(update={"published_precision": "unknown", "published_date": None})})
    reference = await fixture.new_fact(fact)
    observation = await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)
    assert observation.value is None and observation.unknown_reason == "unknown_publication_time"


def test_date_precision_publication_upper_is_shanghai_day_end() -> None:
    timing = make_fact("nii_2025").timing
    upper = publication_upper(timing)
    assert upper == datetime(2026, 3, 28, 15, 59, 59, 999999, tzinfo=UTC)
    assert upper > datetime(2026, 3, 28, 0, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["owner", "listing", "whole_revision", "check", "unknown_field"])
async def test_exact_kind_field_owner_and_listing_are_required(fixture: Fixture, bad: str) -> None:
    reference = fixture.ref("nii_2025")
    owner, listing_id = "local_owner", 18
    if bad == "owner":
        owner = "foreign_test_owner"
    elif bad == "listing":
        listing_id = 19
    elif bad == "whole_revision":
        reference = reference.model_copy(update={"field_path": None})
    elif bad == "check":
        reference = TopicRef(kind="topic_check", id=uuid4(), revision=1)
    else:
        reference = reference.model_copy(update={"field_path": "facts.missing"})
    with pytest.raises(BankTopicReferenceError):
        await fixture.resolver.resolve_fact(owner, reference, listing_id=listing_id, as_of=AS_OF)


async def test_legacy_peer_rechecks_actual_original_bytes_and_saved_evidence_payload(fixture: Fixture) -> None:
    row = await fixture.session.get(DisclosureOriginal, str(fixture.data.original_id))
    assert row is not None
    await fixture.session.execute(update(DisclosureBlob).where(DisclosureBlob.content_hash == row.content_hash).values(body=b"%PDF-bad"))
    with pytest.raises(BankTopicReferenceError):
        await fixture.observation("nim_peer")
    with pytest.raises(BankTopicReferenceError):
        await fixture.observation("nim_evidence")


async def test_existing_evidence_cannot_change_value_behind_a_verified_flag(fixture: Fixture) -> None:
    from investment_agent.storage.models.evidence import SavedEvidence

    row = await fixture.session.get(SavedEvidence, (str(fixture.data.evidence_ref.id), 1))
    assert row is not None
    payload = deepcopy(row.payload)
    payload["value"]["value"] = "0.9"
    await fixture.session.execute(update(SavedEvidence).where(SavedEvidence.id == row.id).values(payload=payload))
    with pytest.raises(BankTopicReferenceError):
        await fixture.observation("nim_evidence")


async def test_pending_peer_recheck_is_not_upgraded_even_when_old_bytes_still_match(fixture: Fixture) -> None:
    row = await fixture.session.get(PeerAnnualSnapshot, str(fixture.data.peer_id))
    assert row is not None
    fields = deepcopy(row.fields)
    fields["net_interest_margin"].update(value="0.019", raw_value="1.9", verification="candidate")
    fixture.session.add(PeerAnnualSnapshot(id=str(uuid4()), company_id=18, listing_id=18, report_period=row.report_period,
        provider=row.provider, adapter_version=row.adapter_version, captured_at=CHECKED,
        content_hash="d" * 64, semantic_hash="d" * 64, fields=fields,
        verification={"net_interest_margin": {"accounting_scope": "group_consolidated"}}))
    await fixture.session.flush()
    observation = await fixture.observation("nim_peer")
    assert observation.verification in {"pending_review", "conflict"}
    assert observation.unknown_reason == "source_conflict"


async def test_batch_is_bounded_and_topics_remain_outside_formal_evidence_admission(fixture: Fixture) -> None:
    with pytest.raises(BankTopicReferenceError):
        await fixture.resolver.resolve_many("local_owner", [fixture.ref("nii_2025")] * 101, listing_id=18, as_of=AS_OF)
    with pytest.raises(InvestorError) as caught:
        await EvidenceService(fixture.session).get_evidence("local_owner", ObjectRef.model_validate(fixture.ref("nii_2025").model_dump()), AS_OF)
    assert caught.value.detail.code == "invalid_reference"


def test_new_payload_hash_normalizes_typed_values_without_guessing_raw_strings() -> None:
    fact = make_fact("nim_2025")
    assert bank_content_hash(fact) == bank_content_hash(fact.model_copy(update={"value": Decimal("0.01870")}))
    assert bank_content_hash({"raw": "1.00"}) != bank_content_hash({"raw": "1"})
    with pytest.raises(ValueError):
        bank_content_hash({"value": Decimal("1e1000000")})
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        bank_content_hash(cyclic)


@pytest.mark.parametrize("saved_evidence", [False, True])
async def test_real_candidate_peer_or_evidence_chain_remains_candidate(fixture: Fixture, saved_evidence: bool) -> None:
    source = await fixture.session.get(PeerAnnualSnapshot, str(fixture.data.peer_id))
    assert source is not None
    fields = deepcopy(source.fields)
    fields["net_interest_margin"]["verification"] = "candidate"
    peer = PeerAnnualSnapshot(id=str(uuid4()), company_id=18, listing_id=18, report_period=source.report_period,
        provider="synthetic-candidate", adapter_version=source.adapter_version, captured_at=CHECKED,
        content_hash="e" * 64, semantic_hash="e" * 64, fields=fields,
        verification={"net_interest_margin": {"accounting_scope": "group_consolidated"}})
    fixture.session.add(peer)
    await fixture.session.flush()
    external = ObjectRef(kind="peer_annual_snapshot", id=UUID(peer.id), revision=1, field_path="fields.net_interest_margin")
    if saved_evidence:
        materialized = peer_field_evidence(peer, "net_interest_margin").record
        assert materialized is not None and materialized.verification == "candidate"
        external = await EvidenceService(fixture.session).save_evidence("local_owner", materialized, uuid4())
    binding = FactBinding.model_validate({"stable_fact_id": "candidate_nim", "metric": "nim", "source": {
        "binding_kind": "existing_fact", "source_ref": external.model_dump(mode="json")}})
    parent = fixture.data.parent.model_copy(update={"id": uuid4(), "revision": 2,
        "fact_bindings": (*fixture.data.parent.fact_bindings, binding)})
    case = await fixture.session.get(TopicCase, str(fixture.data.case_id))
    assert case is not None
    case.head_revision = 2
    fixture.session.add(parent_row(parent))
    await fixture.session.flush()
    reference = TopicRef(kind="topic_revision", id=parent.id, revision=1, field_path="facts.candidate_nim")
    observation = await fixture.resolver.resolve_fact("local_owner", reference, listing_id=18, as_of=AS_OF)
    assert observation.verification == "candidate" and not observation.proof_checked
    assert observation.basis_kind == ("evidence" if saved_evidence else "peer_field")


async def test_resolver_is_read_only_and_does_not_autoflush_or_copy_existing_facts(fixture: Fixture) -> None:
    from sqlalchemy import event, func, select

    before = await fixture.session.scalar(select(func.count()).select_from(FactRow))
    pending = fact_row(fixture.data.facts["nii_2025"].model_copy(update={"id": uuid4(), "revision": 99}))
    fixture.session.add(pending)
    connection = await fixture.session.connection()
    statements: list[str] = []
    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)
    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        await fixture.observation("nii_2025")
        await fixture.observation("nim_peer")
        await fixture.observation("nim_evidence")
        assert pending in fixture.session.new
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
    fixture.session.expunge(pending)
    assert await fixture.session.scalar(select(func.count()).select_from(FactRow)) == before == 7
