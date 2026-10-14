"""The six review failures exercised through actual submission/read paths."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from investment_agent.api.routes.research import research_single_stock
from investment_agent.api.schemas.research import SingleStockResearchRequest
from investment_agent.cli.commands.peer_annual import (
    PeerAnnualImportError,
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.config import Settings
from investment_agent.industry_peers.artifacts import (
    PeerBatchMapping,
    register_batch_commit,
    scan_committed_batch_mappings,
    select_current_mapping,
    write_batch_mapping,
)
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.resolver import PeerContextResolver
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import NumericNode
from investment_agent.investor.enums import NumericRole
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.support.peer_world import (
    AUDIT_PATH,
    SAMPLE_PATH,
    DirectCtx,
    FakeAnnualSource,
    claim_job,
    happy_companies,
    make_handler,
    make_settings,
    read_mapping,
    seed_collection_run,
    seed_listings,
    seed_roster,
    store_standard,
)
from tests.support.peer_world import (
    golden as golden,
)
from tests.support.peer_world import (
    peer_db as peer_db,  # noqa: F401  (pytest fixture)
)
from tests.support.peer_world import (
    synthetic as synthetic,
)


async def test_rolled_back_mapping_is_never_published(db_session, synthetic, tmp_path):
    await store_standard(db_session, synthetic)
    first = await submit_offline_batch(db_session, data_dir=tmp_path)
    await db_session.commit()
    await db_session.execute(text("BEGIN IMMEDIATE"))
    abandoned = await submit_offline_batch(
        db_session, data_dir=tmp_path, tickers=["600001.SH"], batch_run_id="rolled-back"
    )
    await db_session.rollback()
    assert abandoned.path.exists()  # a complete, hash-valid but uncommitted file
    current = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert current.batch_run_id == first.mapping.batch_run_id
    history = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=tmp_path, batch_run_id="rolled-back"
    )
    assert history.status.value == "missing"


async def test_real_handler_keeps_the_queued_generation(peer_db):
    async with session_scope() as session:
        cohort = await seed_roster(session, (("600001", "目标"), ("600002", "同行")))
    queued = await seed_collection_run(cohort, peer_db)
    assert queued.payload.generation == 1
    newer = PeerBatchMapping.create(
        batch_run_id="newer-generation",
        cohort_ref=queued.payload.cohort_ref(),
        cohort_content_hash=cohort.content_hash,
        report_period=queued.payload.report_period,
        provider=queued.payload.provider,
        adapter_version=queued.payload.adapter_version,
        generation=2,
        status="success",
        submitted_at=datetime.now(UTC),
    )
    async with session_scope() as session:
        await register_batch_commit(session, newer)
        write_batch_mapping(peer_db, newer)
    job = await claim_job(queued.run.id)
    await make_handler(
        make_settings(peer_db), FakeAnnualSource(happy_companies(["600001", "600002"]))
    ).execute(job, DirectCtx())
    old = await read_mapping(peer_db, queued.run.id)
    assert old.generation == 1
    async with session_scope() as session:
        current = select_current_mapping(
            list((await scan_committed_batch_mappings(session, peer_db)).valid)
        )
    assert current.batch_run_id == newer.batch_run_id


@pytest.mark.parametrize("part", ["header", "excerpt", "value"])
async def test_original_tampering_is_refused_before_writes(db_session, part):
    sample = load_sample_json(SAMPLE_PATH)
    await seed_listings(db_session, tuple((c["code"], c["name"]) for c in sample["companies"]))
    await db_session.commit()
    audit = load_audit_json(AUDIT_PATH)
    record = audit[0]
    if part == "header":
        record["matched_row"]["alias"] = "母公司季度错误表头"
    elif part == "excerpt":
        record["matched_row"]["excerpt"] = "并不存在于原件的片段"
    else:
        changed = record["api_value"] + 1
        sample["companies"][0]["营业收入"] = changed
        record["api_value"] = changed
        record["matched_row"]["raw_number"] = str(changed)
        record["matched_row"]["parsed_first_value"] = changed
    with pytest.raises(PeerAnnualImportError, match="original verification failed"):
        await import_peer_annual_snapshots(
            db_session, sample=sample, audit=audit, acquired_at=datetime.now(UTC)
        )
    assert not (await db_session.scalars(select(PeerAnnualSnapshot))).all()


async def test_missing_originals_stay_candidate(db_session, tmp_path):
    sample = load_sample_json(SAMPLE_PATH)
    await seed_listings(db_session, tuple((c["code"], c["name"]) for c in sample["companies"]))
    await import_peer_annual_snapshots(
        db_session,
        sample=sample,
        audit=load_audit_json(AUDIT_PATH),
        acquired_at=datetime.now(UTC),
        originals_root=tmp_path,
    )
    rows = (await db_session.scalars(select(PeerAnnualSnapshot))).all()
    assert all(f["verification"] == "candidate" for r in rows for f in r.fields.values())


async def test_real_golden_outputs_and_loss_exclusions_are_citable(db_session, golden, tmp_path):
    await submit_offline_batch(db_session, data_dir=tmp_path)
    await db_session.commit()
    for ticker in ("600519.SH", "000799.SZ"):
        result = await get_industry_peer_context(db_session, ticker=ticker, data_dir=tmp_path)
        binding = peer_context_binding_from(result)
        resolver = PeerContextResolver(db_session, binding=binding, data_dir=tmp_path)
        for entry in binding.metric_calculation_refs:
            validated = await resolver.verify(entry.ref, require_verified_inputs=True)
            assert validated.inputs_verified
            for key in validated.record.results:
                node = NumericNode(
                    type="numeric_claim",
                    display_format="decimal",
                    role=NumericRole.OUTPUT,
                    ref=entry.ref.model_copy(update={"field_path": f"results.{key}"}),
                )
                output = await resolver.numeric_output(node)
                assert output.text
        cash = next(x for x in result.metrics if x.metric_key == "cfo_parent_profit")
        if ticker == "600519.SH":
            assert cash.valid_peer_count == 15
        else:
            assert cash.target_value is None and cash.ascending_position is None


async def test_removed_target_uses_current_membership_but_retains_history(
    db_session, synthetic, tmp_path
):
    await store_standard(db_session, synthetic)
    first = await submit_offline_batch(db_session, data_dir=tmp_path)
    await db_session.commit()
    cohort = (await db_session.scalars(select(PeerCohortSnapshot))).one()
    cutoff = datetime.now(UTC)
    entries = [
        e for e in cohort.payload["entries"] if e["company_id"] != synthetic["ids"]["600001.SH"]
    ]
    db_session.add(
        PeerCohortSnapshot(
            id=str(uuid4()),
            cohort_key=cohort.cohort_key,
            revision=2,
            observed_on=cohort.observed_on,
            acquired_at=cutoff + timedelta(seconds=1),
            verification="candidate",
            inclusion_rule_version=cohort.inclusion_rule_version,
            content_hash="new-roster",
            payload={**cohort.payload, "entries": entries},
        )
    )
    await db_session.commit()
    current = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert current.status.value == "not_supported"
    old = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=tmp_path, batch_run_id=first.mapping.batch_run_id
    )
    assert old.batch_run_id == first.mapping.batch_run_id
    at_cutoff = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=tmp_path, cutoff=cutoff
    )
    assert at_cutoff.batch_run_id == first.mapping.batch_run_id


async def test_research_post_freezes_binding_and_replays_it(db_session, golden, tmp_path):
    first = await submit_offline_batch(db_session, data_dir=tmp_path)
    await db_session.commit()
    settings = Settings(
        _env_file=None,
        api_key="review-offline-key",
        data_dir=tmp_path,
        industry_peer_context_enabled=True,
    )
    body = SingleStockResearchRequest(ticker="600519.SH", provider="snapshot")
    accepted = await research_single_stock(body, settings, idempotency_key="peer-review-repeat")
    saved = await PipelineRunRepository(db_session).get(accepted.run_id)
    original = dict(saved.request_json)
    assert original["industry_peer_context"]["batch_run_id"] == first.mapping.batch_run_id
    await submit_offline_batch(db_session, data_dir=tmp_path, tickers=["600519.SH"])
    await db_session.commit()
    replay = await research_single_stock(body, settings, idempotency_key="peer-review-repeat")
    assert replay.run_id == accepted.run_id and not replay.created
    db_session.expire_all()
    assert (await PipelineRunRepository(db_session).get(replay.run_id)).request_json == original


@pytest.mark.parametrize("change", ["original_bytes", "field_revision"])
async def test_raw_annual_quote_keeps_original_and_revision_checks(
    db_session, golden, tmp_path, change
):
    from sqlalchemy import update

    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.foundations import InvestorError
    from investment_agent.investor.publication_numbers import NumericResolver
    from investment_agent.storage.models.disclosure import DisclosureBlob
    from investment_agent.storage.models.evidence import SavedEvidence

    await submit_offline_batch(db_session, data_dir=tmp_path)
    await db_session.commit()
    row = (await db_session.scalars(select(PeerAnnualSnapshot))).first()
    source_id = row.id
    proof_hash = row.verification["operating_revenue"]["pdf_hash"]
    evidence_rows = (await db_session.scalars(select(SavedEvidence))).all()
    fact = next(
        r
        for r in evidence_rows
        if r.payload["source_ref"]["id"] == source_id
        and r.payload["locator"] == "fields.operating_revenue"
    )
    ref = ObjectRef(kind="evidence", id=fact.id, revision=fact.revision)
    subject = ObjectRef.model_validate(fact.payload["subject_ref"])
    resolver = NumericResolver(EvidenceService(db_session), subject, datetime.now(UTC))
    assert (await resolver.fact(ref, require_verified=True)).verification == "verified"
    if change == "original_bytes":
        await db_session.execute(
            update(DisclosureBlob)
            .where(DisclosureBlob.content_hash == proof_hash)
            .values(body=b"corrupt original")
        )
    else:
        from decimal import Decimal

        fields = dict(row.fields)
        fields["operating_revenue"] = {
            **fields["operating_revenue"],
            "value": str(Decimal(fields["operating_revenue"]["value"]) + 1),
            "verification": "candidate",
        }
        db_session.add(
            PeerAnnualSnapshot(
                id=str(uuid4()),
                company_id=row.company_id,
                listing_id=row.listing_id,
                report_period=row.report_period,
                provider=row.provider,
                adapter_version=row.adapter_version,
                captured_at=datetime.now(UTC),
                content_hash="changed-content",
                semantic_hash="changed-semantics",
                fields=fields,
                verification=dict(row.verification),
            )
        )
    await db_session.commit()
    db_session.expire_all()
    with pytest.raises(InvestorError):
        await NumericResolver(EvidenceService(db_session), subject, datetime.now(UTC)).fact(
            ref, require_verified=True
        )
