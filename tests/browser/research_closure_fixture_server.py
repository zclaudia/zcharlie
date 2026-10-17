"""Local synthetic HTTP fixture for the P3 closure stock-page browser flow.

One account, one bound subject (600801: published coverage, then a late
critical rule so the current-limits recheck shows REVIEW_REQUIRED) and one
legacy subject (600802: no binding row at all). Everything runs on the real
publication chain; the fixture only seeds synthetic rows and drains workers.
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import Depends

sys.path.insert(0, str(Path.cwd()))

from tests.integration.test_coverage_prepare import _drain_workers
from tests.integration.test_publication_closure import (
    _open_application_refs,
    _proposal,
    _publish,
    _publish_action,
    _reviewed,
)
from tests.integration.test_rule_coverage_limits import critical_rule, run_listing_evaluation
from tests.support.account_fit_db import make_inputs, make_trusted_rule
from tests.support.account_fit_flow import persist_inputs
from tests.support.research_closure import (
    activate_rule,
    admit_research,
    business_input,
    claim_task,
    commit_progress,
    metric_rule,
    requirement_rule,
    run_prepare,
    seed_company_view,
    set_runtime,
)

from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.deps import session_service
from investment_agent.api.routes import investment_assessments
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.publication import RuleReviewItem
from investment_agent.portfolio.calculations import TrustedTradingRule
from investment_agent.portfolio.suitability_inputs import SuitabilityInputService
from investment_agent.portfolio.suitability_jobs import AccountFitHandler
from investment_agent.research.worker import ResearchWorker
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import Base

FIXTURE_KEY = "research-closure-synthetic-test-key-2026"
BOUND_LISTING = 600801
LEGACY_LISTING = 600802
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000101")

CHAIN: dict[str, object] = {"done": False}


class SyntheticRules:
    async def resolve(self, listing_ref: ObjectRef, as_of: datetime) -> TrustedTradingRule:
        return make_trusted_rule(listing_ref)


async def run_closure() -> dict[str, object]:
    """Publish once through the real chain, then activate a late critical rule."""

    if CHAIN["done"]:
        return dict(CHAIN)
    listing_ref = ObjectRef(kind="listing", id=BOUND_LISTING, revision=1)
    await set_runtime(True)
    # set_runtime only writes the two P3 keys; mirror the real one-time import
    # so the stored runtime keeps the fixture's account-fit flags instead of
    # falling back to code defaults after import.
    from investment_agent.settings.loader import env_import_values, store_for

    base = Settings(_env_file=None)
    async with session_scope() as session:
        await store_for(session, base).apply(
            {**env_import_values(base), "supplemental_rules_enabled": True,
             "investor_agent_enabled": True},
            actor="research-closure-fixture",
        )
    async with session_scope() as session:
        await activate_rule(session, requirement_rule(listing_ref))
        await activate_rule(session, metric_rule(listing_ref))
        case, evidence_ref = await seed_company_view(session, listing_ref)
        task = await admit_research(session, listing_ref)
        claim = await claim_task(session, task)
        await commit_progress(session, task, claim)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
        task_id = str(task.id)
    await run_prepare(claim, business_input(case, evidence_ref))
    assert await _drain_workers() >= 1
    requirement_ref, metric_ref = await _open_application_refs()
    proposals, frozen = _proposal(requirement_ref)
    result = await run_prepare(claim, business_input(
        case, evidence_ref, question_scope="After evaluation",
        closure_proposals=proposals))
    candidate, semantic = await _reviewed(claim, result, rule_reviews=(
        RuleReviewItem(application_ref=requirement_ref,
                       disposition="resolved_with_closure",
                       proposal_hash=frozen.proposal_hash),
        RuleReviewItem(application_ref=metric_ref, disposition="no_action_required"),
    ))
    operation = await _publish_action(claim, case_ref, candidate, semantic)
    published = await _publish(claim, operation, candidate, semantic)
    assert published.published is True, published.check.field_errors

    # A critical rule activated after publication must limit the old verdict.
    async with session_scope() as session:
        fresh = await activate_rule(session, critical_rule(listing_ref))
    await run_listing_evaluation(listing_ref)

    CHAIN.update({
        "done": True,
        "bound_listing": BOUND_LISTING,
        "legacy_listing": LEGACY_LISTING,
        "account_id": str(ACCOUNT_ID),
        "task_id": task_id,
    })
    # Resolve the exact published manifest from the recommendation dependency.
    async with session_scope() as session:
        from investment_agent.storage.repositories.investment_assessment import (
            InvestmentAssessmentRepository,
        )
        from investment_agent.storage.repositories.supplemental_rules import (
            SupplementalRulesRepository,
        )

        repo = InvestmentAssessmentRepository(session)
        manifest = await repo.latest_published_rule_coverage("local_owner", BOUND_LISTING)
        assert manifest is not None
        limits = await repo.published_rule_coverage_limits("local_owner", manifest)
        binding = await SupplementalRulesRepository(session).get_research_rule_binding(
            "local_owner", manifest.binding_ref
        )
        assert binding is not None
    CHAIN["published_coverage_id"] = str(manifest.id)
    CHAIN["binding_id"] = str(manifest.binding_ref.id)
    CHAIN["manifest_hash"] = manifest.manifest_hash
    CHAIN["rule_set_hash"] = binding.rule_set_hash
    CHAIN["critical_rule_id"] = str(fresh.id)
    assert limits and limits[0].startswith("rule_coverage_pending:"), limits
    CHAIN["limit_reason"] = limits[0]
    return dict(CHAIN)


async def main() -> None:
    workdir = await asyncio.to_thread(Path(os.environ["RESEARCH_CLOSURE_FIXTURE_DIR"]).resolve)
    if not str(workdir).startswith(("/private/var/folders/", "/tmp/")):
        raise RuntimeError("research-closure fixture must use an OS temporary directory")
    if await asyncio.to_thread(lambda: any(workdir.iterdir())):
        raise RuntimeError("research-closure fixture refuses a non-empty directory")
    db_path = workdir / "research-closure.sqlite"
    database_url = f"sqlite+aiosqlite:///{db_path}"
    os.environ.update(
        API_KEY=FIXTURE_KEY,
        DATA_DIR=str(workdir),
        DATABASE_URL=database_url,
        APP_ENV="dev",
        INVESTOR_AGENT_ENABLED="true",
        PORTFOLIO_LEDGER_ENABLED="true",
        ACCOUNT_FIT_ENABLED="true",
        SUPPLEMENTAL_RULES_ENABLED="true",
    )
    Settings.model_config["env_file"] = None
    settings = Settings(
        _env_file=None,
        api_key=FIXTURE_KEY,
        database_url=database_url,
        data_dir=workdir,
        supplemental_rules_enabled=True,
        account_fit_enabled=True,
        portfolio_ledger_enabled=True,
        investor_agent_enabled=True,
        investor_agent_task_tokens=200000,
    )
    engine = init_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    inputs = make_inputs(
        target_listing=BOUND_LISTING,
        extra_listings=(LEGACY_LISTING,),
        cash="200000",
        prices={BOUND_LISTING: "1500", LEGACY_LISTING: "1000"},
        rules=(BOUND_LISTING,),
    )
    async with session_scope() as session:
        await persist_inputs(session, inputs)
        await RuleStore(session).sync_templates(load_templates())

    app = create_app(settings=settings)
    app.dependency_overrides[investment_assessments.service] = session_service(
        lambda session: SuitabilityInputService(session, rules=SyntheticRules())
    )
    worker = ResearchWorker(
        settings=settings,
        worker_id="research-closure-browser",
        handlers=[AccountFitHandler(settings=settings)],
    )

    @app.get("/fixture/ids", dependencies=[Depends(verify_api_key_or_session)])
    async def fixture_ids() -> dict[str, object]:
        return {
            "account_id": str(ACCOUNT_ID),
            "bound_listing": BOUND_LISTING,
            "legacy_listing": LEGACY_LISTING,
        }

    @app.post("/fixture/closure", dependencies=[Depends(verify_api_key_or_session)])
    async def fixture_closure() -> dict[str, object]:
        return await run_closure()

    @app.post("/fixture/run", dependencies=[Depends(verify_api_key_or_session)])
    async def run_jobs() -> dict[str, int]:
        completed = 0
        for _ in range(20):
            if not await worker.run_once():
                break
            completed += 1
        return {"completed": completed}

    @asynccontextmanager
    async def isolated_lifespan(_app):
        yield

    app.router.lifespan_context = isolated_lifespan
    config = uvicorn.Config(
        app, host="127.0.0.1", port=int(os.environ["RESEARCH_CLOSURE_FIXTURE_PORT"]),
        lifespan="off", log_level="warning",
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
