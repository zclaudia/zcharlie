"""AG24 isolated HTTP + file SQLite + native FunctionModel/worker fixture."""
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

sys.path.insert(0, str(Path.cwd()))
import uvicorn
from fastapi import Depends  # noqa: E402
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.usage import RequestUsage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from tests.test_investor_foundations import mandate
from tests.test_portfolio_calculations import action, rule  # noqa: E402

from investment_agent.agent.executor import AgentExecutor  # noqa: E402
from investment_agent.agent.job import AgentJobHandler  # noqa: E402
from investment_agent.agent.research import (  # noqa: E402
    ResearchOutput,
    ResearchPolicy,
    ResearchProgress,
    registrations,
)
from investment_agent.api.app import create_app  # noqa: E402
from investment_agent.api.auth import verify_api_key_or_session  # noqa: E402
from investment_agent.config import Settings  # noqa: E402
from investment_agent.investor.common import ObjectRef  # noqa: E402
from investment_agent.investor.contracts import PortfolioConstraints, TextNode  # noqa: E402
from investment_agent.investor.foundations import InvestorService  # noqa: E402
from investment_agent.portfolio.calculation_contracts import (  # noqa: E402
    PortfolioCalculationRequest,
    Scenario,
)
from investment_agent.portfolio.calculations import (  # noqa: E402
    PortfolioCalculationService,
    TrustedTradingRule,
)
from investment_agent.portfolio.plan_contracts import (  # noqa: E402
    OptionInput,
    PlanInput,
    PlanSemanticInput,
    PortfolioAction,
)
from investment_agent.portfolio.plans import PortfolioPlanService
from investment_agent.portfolio.service import PortfolioService  # noqa: E402
from investment_agent.research.worker import ResearchWorker  # noqa: E402
from investment_agent.storage.database import init_engine, session_scope  # noqa: E402
from investment_agent.storage.models import (  # noqa: E402,F401
    discovery,
    monitoring,
    portfolio,
    portfolio_plan,
    relationship,
    zones,
)
from investment_agent.storage.models.base import Base  # noqa: E402
from investment_agent.storage.models.pit_snapshot import (  # noqa: E402
    IndustryClassification,
    MarketSnapshot,
)
from investment_agent.storage.models.reference import Company, Listing, Market  # noqa: E402


def _holding_rule_handler():
    """A ``RuleDraftHandler`` whose client returns a fixed, valid parameter set."""

    import json

    from investment_agent.rules.drafting import RuleDraftHandler

    class _StubClient:
        async def complete(self, *, system_prompt, user_message, output_model, purpose,
                           pipeline_run_id=None):
            data, _ = json.JSONDecoder().raw_decode(user_message)
            params = data["template"]["params"]
            key = next(
                name
                for name in sorted(params)
                if name.startswith("add_") or name.startswith("buy_")
            )
            bounds = data["template"]["param_ranges"][key]
            cited = next(
                (name for name in ("pe_percentile_5y", "pb_percentile_5y", "roe")
                 if data["factors"].get(name, {}).get("value") is not None),
                None,
            )
            # Without a citable factor the stub keeps every default: an
            # adjustment must cite a factor the input actually carries.
            output = {
                "params": (
                    {key: round((bounds["min"] + params[key]) / 2, 4)} if cited else {}
                ),
                "rationale": [
                    {
                        "param": key,
                        "choice": "adjusted" if cited else "default",
                        "reason": (
                            "按输入因子显示的位置收紧区间参数（fixture 固定草案）。"
                            if cited
                            else "fixture 无可用因子，沿用模板默认值。"
                        ),
                        "cited_factors": [cited] if cited else [],
                    }
                ],
                "overall_rationale": "fixture 固定草案：沿用模板条件，参数按上述取值。",
                "watch_points": [],
            }
            return SimpleNamespace(
                model="fixture-stub",
                text="",
                output=output_model.model_validate(output),
            )

    def build(session=None):  # noqa: ANN001, ARG001
        return _StubClient()

    return RuleDraftHandler(settings=Settings(_env_file=None, api_key="ag24-fixture-key"), client_factory=build)


async def main():
    target = await asyncio.to_thread(Path(os.environ['AG24_FIXTURE_DIR']).resolve)
    if not str(target).startswith(('/private/var/folders/', '/tmp/')):
        raise RuntimeError('fixture requires system temp directory')
    db = target / 'portfolio.db'
    if await asyncio.to_thread(db.exists):
        raise RuntimeError('fixture refuses existing DB')
    os.environ.update(API_KEY='ag24-fixture-key', LLM_TIER3_API_KEY='inert', DATA_DIR=str(target),
        DATABASE_URL=f'sqlite+aiosqlite:///{db}', INVESTOR_AGENT_ENABLED='true',
        PORTFOLIO_LEDGER_ENABLED='false' if os.environ.get('AG24_DISABLED') == 'true' else 'true',
        AGENT_API_KEY='inert', AGENT_BASE_URL='https://fixture.invalid/v1', AGENT_MODEL='fixture',
        INVESTOR_AGENT_TASK_TOKENS='300000', INVESTOR_AGENT_DAILY_TOKENS='1500000')
    Settings.model_config['env_file'] = None
    settings = Settings(_env_file=None, api_key='ag24-fixture-key', investor_agent_enabled=True,
        portfolio_ledger_enabled=os.environ.get('AG24_DISABLED') != 'true', agent_api_key='inert',
        agent_base_url='https://fixture.invalid/v1', agent_model='fixture', agent_max_tokens=100,
        investor_agent_task_tokens=300000, investor_agent_daily_tokens=1500000)
    engine = init_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as session:
        session.add(Market(code='SSE', name='fixture', timezone='Asia/Shanghai', currency='CNY'))
        company = Company(canonical_name='组合证券 <img src=x onerror=alert(1)>')
        session.add(company)
        await session.flush()
        session.add(Listing(id=77, company_id=company.id, market_code='SSE', ticker='600001'))
        session.add(MarketSnapshot(listing_id=77, trade_date=now.date(), open=Decimal(10), high=Decimal(10), low=Decimal(10), close=Decimal(10), volume=Decimal(1), adjust='none', provider='fixture', captured_at=now, version=1))
        session.add(IndustryClassification(listing_id=77, system='CSRC', code='fixture-sector', name='Fixture', valid_from=now.date(), source='fixture', captured_at=now))
    app = create_app(settings=settings)
    class FixtureRules:
        async def resolve(self, ref, as_of):
            return TrustedTradingRule(rule=rule(ref), market_code='SSE', valid_from=(as_of-timedelta(days=30)).date(), available_from=as_of-timedelta(days=30), trusted_source='isolated fixture resolver; not a production exchange source', source_version='ag24-fixture-v1')

    # This constructor injection exists only in this isolated OS fixture process.
    # Native tools and completion both rebuild using the same explicit resolver.
    original_init = PortfolioCalculationService.__init__
    def fixture_init(self, session, owner='local_owner', rules=None):
        original_init(self, session, owner, rules or FixtureRules())
    PortfolioCalculationService.__init__ = fixture_init
    candidate = None
    request = None
    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        assert candidate and request
        returns = [p for m in messages for p in getattr(m, 'parts', ()) if isinstance(p, ToolReturnPart)]
        names = [p.tool_name for p in returns]
        progress = ResearchProgress(question='Review confirmed account constraints', kind='portfolio_action', content=(TextNode(type='text', text='Business evidence unavailable; wait without increasing risk.'),), answered=True, gaps=('Company evidence unavailable',), completed_steps=('Confirmed account exposure and typed constraint checked',))
        usage = RequestUsage(input_tokens=20, output_tokens=10)
        if 'get_portfolio_context' not in names:
            return ModelResponse([ToolCallPart('get_portfolio_context', {'business': {'snapshot_ref': candidate.snapshot_ref.model_dump(mode='json'), 'account_id': str(candidate.account_id)}}, tool_call_id='read')], usage=usage)
        if 'save_research_progress' not in names:
            return ModelResponse([ToolCallPart('save_research_progress', {'business': progress.model_dump(mode='json')}, tool_call_id='progress')], usage=usage)
        if 'calculate_portfolio_options' not in names:
            business = request.model_dump(mode='json')
            business.pop('operation_id')
            return ModelResponse([ToolCallPart('calculate_portfolio_options', {'business': business}, tool_call_id='calc')], usage=usage)
        returned = next(p for p in returns if p.tool_name == 'calculate_portfolio_options')
        calculation_ref = ObjectRef.model_validate(returned.content['calculation_ref'])
        options = tuple(o.model_copy(update={'actions':tuple(a.model_copy(update={'target_ref':a.target_ref.model_copy(update={'id':calculation_ref.id}) if a.target_ref else None}) for a in o.actions)}) for o in candidate.options)
        built = candidate.model_copy(update={'calculation_ref':calculation_ref,'options':options})
        if 'prepare_portfolio_plan' not in names:
            return ModelResponse([ToolCallPart('prepare_portfolio_plan', {'business': built.model_dump(mode='json')}, tool_call_id='prepare')], usage=usage)
        prepared = next(p for p in returns if p.tool_name == 'prepare_portfolio_plan')
        review = PlanSemanticInput(preparation_ref=ObjectRef.model_validate(prepared.content['preparation_ref']), assessments={str(a.id): ('Confirmed account concentration is excessive', 'Business evidence remains unavailable', 'Reduced confirmed concentration while retaining business research gaps') for o in built.options for a in o.actions}, checked_sources=(built.snapshot_ref, built.mandate_ref, built.calculation_ref, *((request.purpose_ref,) if request.purpose_ref else ())), conclusion='pass')
        return ModelResponse([TextPart(ResearchOutput(progress=progress, portfolio_review=review).model_dump_json())], usage=usage)

    executor = AgentExecutor(settings, model=FunctionModel(model), registrations=registrations(), specification=ResearchPolicy(settings).specification())
    # Holding rule drafting (WP8) runs against a stub client: the fixture never
    # calls a model, it only needs the run to store a draft the page can review.
    async with session_scope() as session:
        from investment_agent.rules.store import RuleStore
        from investment_agent.rules.templates import load_templates
        await RuleStore(session).sync_templates(load_templates())
    worker = ResearchWorker(settings=settings, worker_id='ag24-browser', handlers=[
        AgentJobHandler('agent_turn', executor), _holding_rule_handler(),
    ])

    @app.post('/fixture/portfolio/{account_id}', dependencies=[Depends(verify_api_key_or_session)])
    async def prepare(account_id: UUID):
        nonlocal candidate, request
        async with session_scope() as session:
            svc = PortfolioService(session)
            snapshot = await svc.snapshot(account_id)
            current = await PortfolioPlanService(session).current(account_id)
            previous = ObjectRef(kind='portfolio_plan',id=current.plan.id,revision=current.plan.revision) if current else None
            try:
                m = await InvestorService(session).get_mandate('local_owner')
            except Exception:
                m = await InvestorService(session).create_mandate('local_owner', mandate().model_copy(update={'scope_refs':(ObjectRef(kind='listing',id=77,revision=1),), 'hard_constraints':(), 'portfolio_constraints':PortfolioConstraints(account_id=account_id,max_single_weight=Decimal('.3'))}), uuid4())
            mref = ObjectRef(kind='mandate',id=m.id,revision=m.revision)
            from investment_agent.portfolio.purposes import PurposeService
            purposes = await PurposeService(session).current(account_id)
            purpose_ref = purposes.version.reference if purposes else None
            purpose_id = purposes.version.purposes[-1].id if purposes else None
            request = PortfolioCalculationRequest(purpose_ref=purpose_ref,operation_id=uuid4(),account_id=account_id,snapshot_ref=snapshot.reference,mandate_ref=mref,scenarios=tuple(Scenario(name=f'减仓情景{index}',actions=(action(ObjectRef(kind='listing',id=77,revision=1),direction='sell',purpose_id=purpose_id,quantity=Decimal(quantity),fixed_fee=Decimal(fee)),)) for index,quantity,fee in ((1,'10','1'),(2,'20','2'))))
            calc = await PortfolioCalculationService(session).save(request)
            ref = ObjectRef(kind='listing',id=77,revision=1)
            candidate = PlanInput(account_id=account_id,snapshot_ref=snapshot.reference,mandate_ref=mref,calculation_ref=ObjectRef(kind='calculation',id=calc.id,revision=1),previous_ref=previous,options=tuple(OptionInput(id=uuid4(),scenario_index=index,actions=(PortfolioAction(id=uuid4(),listing_ref=ref,stance='reduce',target_ref=ObjectRef(kind='calculation',id=calc.id,revision=1,field_path=f'results.scenario_{index}_action_0_quantity'),basis='portfolio_constraint',constraint='max_single_weight',research_gap='Business evidence remains unavailable'),)) for index in (1,2)),question_scope='Whole confirmed account alternatives')
        return {'prepared':True,'fact_time':now.isoformat()}

    @app.post('/fixture/valuate', dependencies=[Depends(verify_api_key_or_session)])
    async def valuate():
        """Stored valuations for the fixture accounts (reads no longer compute)."""
        from investment_agent.portfolio.valuation import ValuationService
        from investment_agent.storage.models.portfolio import PortfolioAccount
        async with session_scope() as session:
            accounts = list((await session.scalars(select(PortfolioAccount.id))).all())
        for account_id in accounts:
            async with session_scope() as session:
                await ValuationService(session).ensure(UUID(account_id), is_trading_day=lambda day: True)
        return {'valuated':len(accounts)}

    @app.post('/fixture/run', dependencies=[Depends(verify_api_key_or_session)])
    async def run():
        for _ in range(6):
            if not await worker.run_once():
                break
        return {'calls':calls}

    @app.post('/fixture/writes/{enabled}', dependencies=[Depends(verify_api_key_or_session)])
    async def writes(enabled: bool):
        os.environ['PORTFOLIO_LEDGER_ENABLED'] = 'true' if enabled else 'false'
        return {'enabled':enabled}

    @app.get('/fixture/state', dependencies=[Depends(verify_api_key_or_session)])
    async def state():
        return {'calls':calls,'fact_time':now.isoformat()}

    await uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=int(os.environ['AG24_FIXTURE_PORT']),lifespan='off',log_level='warning')).serve()


if __name__ == '__main__':
    asyncio.run(main())
